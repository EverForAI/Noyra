"""Admission limits for evidence that cannot safely be deleted by age."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from typing import Any

from .errors import InvalidTransitionError

EVIDENCE_TABLES = (
    "model_calls",
    "model_attempts",
    "actions",
    "behavior_logs",
    "research_search_runs",
    "action_deliberation_runs",
)
DEFAULT_EVIDENCE_ROWS = 250_000

# Persistent idempotency and causal evidence is deliberately retained. The
# admission boundary, not age-based deletion, bounds these tables.
EVIDENCE_LIFECYCLE = {
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


def require_evidence_capacity(connection: Any, subject_id: str, table: str) -> None:
    """Call inside the writer transaction, after any idempotent replay lookup."""
    if table not in EVIDENCE_TABLES:
        raise ValueError("unknown evidence table")
    limit = evidence_row_limit()
    protected = [table]
    if table in {"model_calls", "actions"}:
        protected.extend(
            name
            for name, (source, _) in EVIDENCE_LIFECYCLE.items()
            if source == table and name != table
        )
    for protected_table in protected:
        row = connection.execute(
            f"SELECT 1 FROM {protected_table} WHERE subject_id=? LIMIT 1 OFFSET ?",
            (subject_id, limit - 1),
        ).fetchone()
        if row is not None:
            raise InvalidTransitionError("evidence_capacity_reached")


def evidence_capacity_status(connection: sqlite3.Connection, subject_id: str) -> dict[str, Any]:
    limit = evidence_row_limit()
    counts = {
        table: int(
            connection.execute(
                f"SELECT count(*) FROM (SELECT 1 FROM {table} WHERE subject_id=? LIMIT ?)",
                (subject_id, limit),
            ).fetchone()[0]
        )
        for table in EVIDENCE_TABLES
    }
    return {
        "limit_per_table": limit,
        "counts": counts,
        "blocked": any(value >= limit for value in counts.values()),
        "policy": "preserve_required_evidence_stop_new_work",
        "lifecycle": {
            table: {
                "action": "preserve_bounded",
                "admission_source": source,
                "reason": reason,
                "recovery": "authenticated_backup",
                "historical_export": "runtime_export",
                "count_is_lower_bound": counts[table] == limit,
            }
            for table, (source, reason) in EVIDENCE_LIFECYCLE.items()
        },
    }
