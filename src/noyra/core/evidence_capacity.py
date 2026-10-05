"""Admission limits for evidence that cannot safely be deleted by age."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from typing import Any

from .errors import IntegrityError, InvalidTransitionError
from .evidence_schema import TABLES_V80

EVIDENCE_TABLES = TABLES_V80
DEFAULT_EVIDENCE_ROWS = 250_000

# Persistent idempotency and causal evidence is deliberately retained. The
# admission boundary, not age-based deletion, bounds these tables.
EVIDENCE_LIFECYCLE = {
    "embedding_usage_entries": (
        "embedding_usage_entries",
        "permanent embedding budgets and recovery",
    ),
    "embedding_circuit_transitions": (
        "embedding_usage_entries",
        "at most three transitions per admitted embedding, including recovery",
    ),
    "model_calls": ("model_calls", "permanent request idempotency, budgets and causal references"),
    "model_attempts": ("model_attempts", "reserved and settled model usage; unknown-call recovery"),
    "actions": ("actions", "permanent action idempotency and external-effect recovery"),
    "behavior_logs": ("actions", "one terminal behavior record per action; revision provenance"),
    "research_search_runs": (
        "model_calls",
        "one record per planner call; project and source provenance",
    ),
    "action_deliberation_runs": (
        "model_calls",
        "one record per deliberation call; permanent result identity",
    ),
}


def evidence_row_limit(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    value = env.get("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", str(DEFAULT_EVIDENCE_ROWS))
    try:
        limit = int(value)
    except (ValueError, TypeError):
        raise ValueError("evidence row limit must be an integer") from None
    if not 1 <= limit <= 10_000_000:
        raise ValueError("evidence row limit must be between 1 and 10000000")
    return limit


def _limits() -> dict[str, int]:
    limit = evidence_row_limit()
    # Initial circuit + probe + terminal/recovery transitions are reserved by
    # usage admission. Completion must never be blocked at the capacity limit.
    return {
        table: limit * (3 if table == "embedding_circuit_transitions" else 1)
        for table in EVIDENCE_TABLES
    }


def evidence_counts(connection: Any, subject_id: str) -> dict[str, int]:
    version = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if version is not None and int(version[0]) < 80:
        # Historical migration fixtures only; production always migrates first.
        return {
            table: int(
                connection.execute(
                    f"SELECT count(*) FROM {table} WHERE subject_id=?", (subject_id,)
                ).fetchone()[0]
            )
            for table in EVIDENCE_TABLES
        }
    rows = connection.execute(
        "SELECT table_name,row_count FROM evidence_row_counts WHERE subject_id=?", (subject_id,)
    ).fetchall()
    counts = {str(row[0]): row[1] for row in rows}
    if set(counts) != set(EVIDENCE_TABLES) or any(
        type(value) is not int or value < 0 for value in counts.values()
    ):
        # Preserve the writer's foreign-key error contract for absent subjects.
        # An existing identity without its counters is instead durable corruption;
        # neither case may create counters or admit work implicitly.
        if (
            connection.execute(
                "SELECT 1 FROM subject_identity WHERE subject_id=?", (subject_id,)
            ).fetchone()
            is None
        ):
            raise sqlite3.IntegrityError("evidence subject does not exist")
        raise IntegrityError("evidence counter contract is incomplete")
    return counts


def require_evidence_capacity(connection: Any, subject_id: str, table: str) -> None:
    """Call inside the writer transaction, after any idempotent replay lookup."""
    if table not in EVIDENCE_TABLES:
        raise ValueError("unknown evidence table")
    counts, limits = evidence_counts(connection, subject_id), _limits()
    protected = {table} | {
        name for name, (source, _) in EVIDENCE_LIFECYCLE.items() if source == table
    }
    if any(counts[name] >= limits[name] for name in protected):
        raise InvalidTransitionError("evidence_capacity_reached")


def evidence_capacity_status(connection: sqlite3.Connection, subject_id: str) -> dict[str, Any]:
    counts, limits = evidence_counts(connection, subject_id), _limits()
    return {
        "limit_per_table": evidence_row_limit(),
        "limits": limits,
        "counts": counts,
        "blocked": any(value >= limits[table] for table, value in counts.items()),
        "policy": "preserve_required_evidence_stop_new_work",
        "lifecycle": {
            table: {
                "action": "preserve_bounded",
                "admission_source": source,
                "reason": reason,
                "recovery": "authenticated_backup",
                "historical_export": "runtime_export",
                "count_is_lower_bound": False,
            }
            for table, (source, reason) in EVIDENCE_LIFECYCLE.items()
        },
    }
