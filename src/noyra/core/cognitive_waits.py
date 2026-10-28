"""Terminal business ownership of cognitive waits; no model or budget operations.

All mutations receive the caller's BEGIN IMMEDIATE connection.  A cancelled
wait is retained evidence, not a successful model call or released reservation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from typing import Any

from .errors import IntegrityError
from .types import content_hash, new_id

WAIT_FIELDS = (
    "task_id",
    "subject_id",
    "pool",
    "purpose",
    "status",
    "reason_code",
    "retry_count",
    "next_retry_at",
    "first_waited_at",
    "updated_at",
)


def terminal_wait_reason(
    connection: sqlite3.Connection, subject_id: str, purpose: str
) -> str | None:
    kind, separator, parent_id = purpose.partition(":")
    if not separator or not parent_id:
        return None
    if kind == "sleep_reflection":
        row = connection.execute(
            "SELECT status,reflection_event_id FROM sleep_runs WHERE subject_id=? AND sleep_id=?",
            (subject_id, parent_id),
        ).fetchone()
        if row is not None:
            if row["status"] in {"complete", "failed"}:
                return f"parent_sleep_{row['status']}"
            if row["reflection_event_id"] is not None:
                return "sleep_reflection_committed"
    elif kind == "world_cognition":
        row = connection.execute(
            "SELECT status FROM observations WHERE subject_id=? AND observation_id=?",
            (subject_id, parent_id),
        ).fetchone()
        if row is not None and row["status"] in {"analyzed", "rejected"}:
            return f"parent_observation_{row['status']}"
    return None


def insert_wait_revision(
    connection: sqlite3.Connection,
    task_id: str,
    status: str,
    reason_code: str,
    retry_count: int,
    next_retry_at: str,
    now: str,
) -> None:
    payload = {
        "task_id": task_id,
        "status": status,
        "reason_code": reason_code,
        "retry_count": retry_count,
        "next_retry_at": next_retry_at,
        "created_at": now,
    }
    connection.execute(
        "INSERT INTO waiting_cognitive_task_revisions("
        "revision_id,task_id,status,reason_code,retry_count,next_retry_at,state_hash,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            new_id("cwaitrev"),
            task_id,
            status,
            reason_code,
            retry_count,
            next_retry_at,
            content_hash(payload),
            now,
        ),
    )


def validate_wait(connection: sqlite3.Connection, row: Any) -> None:
    """Refuse to overwrite inconsistent evidence, including earlier revisions."""
    payload = {key: row[key] for key in WAIT_FIELDS}
    if row["state_hash"] != content_hash(payload):
        raise IntegrityError("waiting cognitive task hash mismatch")
    # Stream complete evidence in constant memory. An arbitrary history-length
    # cap must not prevent a legitimate business task from reaching its terminal
    # state. Maintenance callers separately enforce a SQL progress budget.
    revisions = connection.execute(
        "SELECT * FROM waiting_cognitive_task_revisions WHERE task_id=? ORDER BY rowid",
        (row["task_id"],),
    )
    first_created_at = None
    previous = None
    for revision in revisions:
        if first_created_at is None:
            first_created_at = revision["created_at"]
        state = {
            key: revision[key]
            for key in (
                "task_id",
                "status",
                "reason_code",
                "retry_count",
                "next_retry_at",
                "created_at",
            )
        }
        if revision["state_hash"] != content_hash(state):
            raise IntegrityError("waiting cognitive task revision hash mismatch")
        status, count = revision["status"], revision["retry_count"]
        if status not in {"waiting", "resolved", "cancelled"} or type(count) is not int:
            raise IntegrityError("invalid waiting cognitive task revision")
        if previous is None:
            valid = status == "waiting" and count == 1
        elif status == "waiting":
            valid = count == previous["retry_count"] + 1
        else:
            valid = (
                previous["status"] == "waiting"
                and count == previous["retry_count"]
                and revision["next_retry_at"] == previous["next_retry_at"]
            )
        if not valid:
            raise IntegrityError("waiting cognitive task revision sequence invalid")
        previous = revision
    if previous is None:
        raise IntegrityError("waiting cognitive task history missing")
    if (
        any(
            row[key] != previous[key]
            for key in ("status", "reason_code", "retry_count", "next_retry_at")
        )
        or row["first_waited_at"] != first_created_at
        or row["updated_at"] != previous["created_at"]
    ):
        raise IntegrityError("waiting cognitive task current state mismatch")


def cancel_terminal_waits(
    connection: sqlite3.Connection,
    subject_id: str,
    purpose: str,
    *,
    now: str,
    task_ids: Collection[str] | None = None,
) -> int:
    """Close only waits with positive terminal-parent evidence in this transaction."""
    reason = terminal_wait_reason(connection, subject_id, purpose)
    if reason is None:
        return 0
    rows = connection.execute(
        "SELECT * FROM waiting_cognitive_tasks WHERE subject_id=? AND purpose=? "
        "AND status='waiting'",
        (subject_id, purpose),
    ).fetchall()
    cancelled = 0
    for row in rows:
        if task_ids is not None and row["task_id"] not in task_ids:
            continue
        validate_wait(connection, row)
        payload = {key: row[key] for key in WAIT_FIELDS}
        payload.update(status="cancelled", reason_code=reason, updated_at=now)
        connection.execute(
            "UPDATE waiting_cognitive_tasks SET status='cancelled',reason_code=?,"
            "updated_at=?,state_hash=? WHERE task_id=? AND status='waiting'",
            (reason, now, content_hash(payload), row["task_id"]),
        )
        insert_wait_revision(
            connection,
            row["task_id"],
            "cancelled",
            reason,
            row["retry_count"],
            row["next_retry_at"],
            now,
        )
        cancelled += 1
    return cancelled


# Indexed parent primary-key lookups. Missing parents are not terminal evidence.
WAIT_JOINS = """
 LEFT JOIN sleep_runs s ON substr(w.purpose,1,17)='sleep_reflection:'
   AND s.sleep_id=substr(w.purpose,18) AND s.subject_id=w.subject_id
 LEFT JOIN observations o ON substr(w.purpose,1,16)='world_cognition:'
   AND o.observation_id=substr(w.purpose,17) AND o.subject_id=w.subject_id
"""
TERMINAL_WAIT = """(
 s.status IN ('complete','failed') OR s.reflection_event_id IS NOT NULL
 OR o.status IN ('analyzed','rejected')
)"""
MISSING_WAIT_PARENT = """(
 (substr(w.purpose,1,17)='sleep_reflection:' AND s.sleep_id IS NULL)
 OR (substr(w.purpose,1,16)='world_cognition:' AND o.observation_id IS NULL)
)"""


def wait_counts(connection: sqlite3.Connection, subject_id: str, pool: str) -> dict[str, int]:
    row = connection.execute(
        "SELECT COUNT(*) AS recorded, "
        f"COALESCE(SUM(CASE WHEN {TERMINAL_WAIT} THEN 1 ELSE 0 END),0) AS terminal, "
        f"COALESCE(SUM(CASE WHEN {MISSING_WAIT_PARENT} THEN 1 ELSE 0 END),0) AS missing "
        f"FROM waiting_cognitive_tasks w {WAIT_JOINS} "
        "WHERE w.subject_id=? AND w.pool=? AND w.status='waiting'",
        (subject_id, pool),
    ).fetchone()
    cancelled = connection.execute(
        "SELECT COUNT(*) FROM waiting_cognitive_tasks "
        "WHERE subject_id=? AND pool=? AND status='cancelled'",
        (subject_id, pool),
    ).fetchone()[0]
    return {
        "pending_wait_count": row["recorded"] - row["terminal"],
        "recorded_wait_count": row["recorded"],
        "terminal_parent_wait_count": row["terminal"],
        "missing_parent_wait_count": row["missing"],
        "cancelled_wait_count": cancelled,
    }
