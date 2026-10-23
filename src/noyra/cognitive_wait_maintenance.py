"""Audited, bounded terminal-wait repair. Preview is read-only and WAL-aware.

Run ``python -m noyra.cognitive_wait_maintenance --help``. No migration,
provider, wake-up, model reconciliation, or budget settlement is performed.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from noyra.core.cognitive_waits import (
    cancel_terminal_waits,
    terminal_wait_reason,
    validate_wait,
)
from noyra.core.database import Database
from noyra.core.errors import IntegrityError, SchemaContractError
from noyra.core.events import EventStore
from noyra.core.types import content_hash, utc_now

MAX_BATCH = 100
SQL_SECONDS = 2.0


class _MaintenanceDatabase(Database):
    def _connect(self) -> sqlite3.Connection:
        connection = super()._connect()
        connection.execute("PRAGMA busy_timeout=200")
        deadline = time.monotonic() + SQL_SECONDS
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        return connection


@contextmanager
def preview_connection(path: Path) -> Iterator[sqlite3.Connection]:
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("database_not_a_file")
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro", uri=True, timeout=0.2, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=200")
        deadline = time.monotonic() + SQL_SECONDS
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def _validate_parent(connection: sqlite3.Connection, subject_id: str, purpose: str) -> None:
    """Check terminal status evidence, rather than trusting a bare status edit."""
    kind, _, parent_id = purpose.partition(":")
    if kind == "sleep_reflection":
        from noyra.sleep.engine import SLEEP_TRANSITIONS
        from noyra.sleep.integrity import SleepIntegrity

        row = connection.execute(
            "SELECT * FROM sleep_runs WHERE sleep_id=? AND subject_id=?",
            (parent_id, subject_id),
        ).fetchone()
        if row is None:
            raise IntegrityError("sleep parent missing")
        SleepIntegrity._sleep_run_from_row(row)
        transitions = connection.execute(
            "SELECT * FROM sleep_transitions WHERE sleep_id=? ORDER BY rowid LIMIT 9",
            (parent_id,),
        ).fetchall()
        previous = None
        for transition in transitions:
            payload = {key: transition[key] for key in ("from_status", "to_status", "reason")}
            allowed = (
                transition["to_status"] == "winding_down"
                if previous is None
                else transition["to_status"] in SLEEP_TRANSITIONS.get(previous, frozenset())
            )
            if (
                not allowed
                or transition["from_status"] != previous
                or transition["state_hash"] != content_hash(payload)
            ):
                raise IntegrityError("sleep parent transition mismatch")
            previous = transition["to_status"]
        if not transitions or previous != row["status"] or len(transitions) > 8:
            raise IntegrityError("sleep parent state mismatch")
        if row["reflection_event_id"] is not None:
            reflection = connection.execute(
                "SELECT * FROM sleep_reflections WHERE sleep_id=?", (parent_id,)
            ).fetchone()
            if reflection is None or reflection["event_id"] != row["reflection_event_id"]:
                raise IntegrityError("sleep reflection parent mismatch")
            SleepIntegrity._verify_reflection(reflection)
            event = connection.execute(
                "SELECT subject_id,event_type FROM events WHERE event_id=?",
                (row["reflection_event_id"],),
            ).fetchone()
            if (
                event is None
                or event["subject_id"] != subject_id
                or event["event_type"] != "sleep_reflection"
            ):
                raise IntegrityError("sleep reflection event mismatch")
        if row["version"] != len(transitions) + int(row["reflection_event_id"] is not None):
            raise IntegrityError("sleep parent version mismatch")
        if row["status"] in {"deep_sleep", "waking", "complete"}:
            checkpoint = connection.execute(
                "SELECT subject_id FROM state_snapshots WHERE snapshot_id=?",
                (row["checkpoint_id"],),
            ).fetchone()
            if checkpoint is None or checkpoint["subject_id"] != subject_id:
                raise IntegrityError("sleep parent checkpoint mismatch")
    elif kind == "world_cognition":
        from noyra.world.store import ObservationStore

        transitions = connection.execute(
            "SELECT * FROM observation_status_transitions WHERE observation_id=? "
            "ORDER BY rowid LIMIT 3",
            (parent_id,),
        ).fetchall()
        previous = None
        for transition in transitions:
            if transition["from_status"] != previous or transition[
                "state_hash"
            ] != ObservationStore._transition_hash(
                transition["from_status"], transition["to_status"], transition["reason"]
            ):
                raise IntegrityError("observation parent transition mismatch")
            previous = transition["to_status"]
        parent = connection.execute(
            "SELECT status FROM observations WHERE observation_id=? AND subject_id=?",
            (parent_id, subject_id),
        ).fetchone()
        if (
            parent is None
            or len(transitions) != 2
            or transitions[0]["to_status"] != "new"
            or previous != parent["status"]
            or previous not in {"analyzed", "rejected"}
        ):
            raise IntegrityError("observation parent state mismatch")


def inspect_waits(
    connection: sqlite3.Connection,
    path: Path,
    subject_id: str,
    *,
    limit: int = MAX_BATCH,
    after_task_id: str = "",
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
        raise ValueError("invalid_batch_limit")
    if not subject_id.strip() or len(after_task_id) > 200:
        raise ValueError("invalid_subject_or_cursor")
    if (
        connection.execute(
            "SELECT 1 FROM subject_identity WHERE subject_id=?", (subject_id,)
        ).fetchone()
        is None
    ):
        raise ValueError("subject_not_found")
    rows = connection.execute(
        "SELECT * FROM waiting_cognitive_tasks WHERE subject_id=? AND status='waiting' "
        "AND task_id>? ORDER BY task_id LIMIT ?",
        (subject_id, after_task_id, limit + 1),
    ).fetchall()
    candidates = []
    fingerprint = []
    for row in rows[:limit]:
        reason = terminal_wait_reason(connection, subject_id, row["purpose"])
        fingerprint.append((row["task_id"], row["state_hash"], reason))
        if reason is not None:
            validate_wait(connection, row)
            _validate_parent(connection, subject_id, row["purpose"])
            candidates.append(
                {
                    "task_id": row["task_id"],
                    "pool": row["pool"],
                    "purpose": row["purpose"],
                    "cancel_reason": reason,
                }
            )
    plan = {
        "database": str(path.resolve()),
        "subject_id": subject_id,
        "limit": limit,
        "after_task_id": after_task_id,
        "rows": fingerprint,
    }
    return {
        "format": "noyra-terminal-wait-repair/v1",
        "mode": "preview",
        "subject_id": subject_id,
        "scanned": min(limit, len(rows)),
        "eligible": len(candidates),
        "candidates": candidates,
        "next_cursor": rows[limit - 1]["task_id"] if len(rows) > limit else None,
        "plan_hash": content_hash(plan),
        "model_calls_and_budgets_modified": False,
    }


def repair_waits(
    path: Path,
    subject_id: str,
    *,
    expected_plan: str,
    reason: str,
    limit: int = MAX_BATCH,
    after_task_id: str = "",
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", expected_plan):
        raise ValueError("preview_plan_required")
    if not reason.strip() or len(reason) > 500:
        raise ValueError("operator_reason_required")
    path = path.resolve(strict=True)
    # No initialization, migration, immutable reads, accounting bypass, or service startup.
    database = _MaintenanceDatabase(path, initialize=False, strict_schema_contract=True)
    events = EventStore(database)
    with database.transaction() as connection:
        connection.execute("PRAGMA busy_timeout=200")
        deadline = time.monotonic() + SQL_SECONDS
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        plan = inspect_waits(connection, path, subject_id, limit=limit, after_task_id=after_task_id)
        if plan["plan_hash"] != expected_plan:
            raise ValueError("preview_stale_rerun_required")
        now = utc_now()
        cancelled = 0
        for item in plan["candidates"]:
            # A second pool with the same purpose may belong to a later page.
            cancelled += cancel_terminal_waits(
                connection,
                subject_id,
                item["purpose"],
                now=now,
                task_ids=(item["task_id"],),
            )
        event_id = None
        if cancelled:
            event_id = events._append_connection(
                connection,
                subject_id,
                "cognitive_wait_maintenance",
                "operator",
                {
                    "plan_hash": expected_plan,
                    "reason": reason,
                    "cancelled_task_ids": [item["task_id"] for item in plan["candidates"]],
                },
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=now,
                event_id=None,
            ).event_id
        plan.update(mode="applied", cancelled=cancelled, event_id=event_id)
        # Do not interrupt COMMIT after successful, bounded validation and writes.
        connection.set_progress_handler(None, 0)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--limit", type=int, default=MAX_BATCH)
    parser.add_argument("--after-task-id", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-plan", default="")
    parser.add_argument("--reason", default="")
    options = parser.parse_args()
    try:
        if options.apply:
            result = repair_waits(
                options.database,
                options.subject_id,
                expected_plan=options.expect_plan,
                reason=options.reason,
                limit=options.limit,
                after_task_id=options.after_task_id,
            )
        else:
            with preview_connection(options.database) as connection:
                result = inspect_waits(
                    connection,
                    options.database,
                    options.subject_id,
                    limit=options.limit,
                    after_task_id=options.after_task_id,
                )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (OSError, sqlite3.Error, IntegrityError, SchemaContractError, ValueError) as error:
        # Whitelist machine-readable reasons, never raw exception/input content.
        if isinstance(error, SchemaContractError):
            code = "schema_contract_unavailable"
        elif isinstance(error, IntegrityError):
            code = "wait_or_parent_integrity_unavailable"
        elif isinstance(error, sqlite3.Error):
            code = "database_busy_query_budget_or_schema_unavailable"
        elif isinstance(error, ValueError) and str(error) in {
            "invalid_batch_limit",
            "invalid_subject_or_cursor",
            "subject_not_found",
            "preview_plan_required",
            "operator_reason_required",
            "preview_stale_rerun_required",
            "database_not_a_file",
        }:
            code = str(error)
        else:
            code = "database_or_input_unavailable"
        parser.exit(2, json.dumps({"error": code, "batch_committed": False}) + "\n")


if __name__ == "__main__":
    main()
