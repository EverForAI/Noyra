"""Temporary model-attempt admission for recovery from verified project replay.

Reservations use the existing workflow history and the caller's ledger transaction.
They are deliberately conservative (no refunds); permanent budgets remain authoritative.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import IntegrityError
from .types import canonical_json, content_hash

RECOVERY_HOURLY_ATTEMPTS = 6
RECOVERY_DAILY_ATTEMPTS = 24
RECOVERY_DAILY_INPUT_TOKENS = 400_000
RECOVERY_DAILY_OUTPUT_TOKENS = 80_000
RECOVERY_MAX_RESERVATIONS = 128


def workflow_key(subject_id: str) -> str:
    return "autonomy-recovery:" + content_hash({"subject_id": subject_id})


def read_recovery(connection: sqlite3.Connection, subject_id: str) -> dict[str, Any] | None:
    from noyra.autonomy.workflow import DurableWorkflowStore

    row = connection.execute(
        "SELECT * FROM workflow_checkpoints WHERE workflow_id=? "
        "ORDER BY checkpoint_version DESC LIMIT 1",
        (workflow_key(subject_id),),
    ).fetchone()
    if row is None:
        return None
    record = DurableWorkflowStore._from_row(row)
    state = record.state
    if (
        record.subject_id != subject_id
        or record.workflow_type != "autonomy_recovery"
        or record.status != "running"
        or state.get("version") != 1
        or type(state.get("active")) is not bool
        or type(state.get("version")) is not int
        or not isinstance(state.get("incident"), str)
        or len(state["incident"]) != 64
        or state.get("reason") not in {"project_replay_recovery", "normal_operation_restored"}
        or not isinstance(state.get("project_ids"), list)
        or not 1 <= len(state["project_ids"]) <= 8
        or any(not isinstance(x, str) or not x for x in state["project_ids"])
        or len(set(state["project_ids"])) != len(state["project_ids"])
        or not isinstance(state.get("reservations"), list)
        or len(state["reservations"]) > RECOVERY_MAX_RESERVATIONS
    ):
        raise IntegrityError("autonomy recovery control is invalid")
    for field in ("started_at", "checked_at"):
        parse_time(state.get(field))
    for field in ("stable_since", "last_healthy_at"):
        if state.get(field) is not None:
            parse_time(state[field])
    if len({r.get("attempt_id") for r in state["reservations"] if isinstance(r, dict)}) != len(
        state["reservations"]
    ):
        raise IntegrityError("duplicate autonomy recovery reservation")
    for item in state["reservations"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("attempt_id"), str)
            or not isinstance(item.get("call_id"), str)
            or any(
                type(item.get(k)) is not int or item[k] < 0
                for k in ("input_tokens", "output_tokens")
            )
        ):
            raise IntegrityError("autonomy recovery reservation is invalid")
        parse_time(item.get("reserved_at"))
    return dict(state)


def parse_time(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("naive time")
        return result.astimezone(UTC)
    except (ValueError, TypeError) as error:
        raise IntegrityError("autonomy recovery time is invalid") from error


def write_recovery(
    connection: sqlite3.Connection, subject_id: str, state: dict[str, Any], now: str
) -> None:
    key = workflow_key(subject_id)
    previous = connection.execute(
        "SELECT checkpoint_version FROM workflow_checkpoints WHERE workflow_id=? "
        "ORDER BY checkpoint_version DESC LIMIT 1",
        (key,),
    ).fetchone()
    version = 1 if previous is None else int(previous[0]) + 1
    connection.execute(
        "INSERT INTO workflow_checkpoints(workflow_id,subject_id,workflow_type,"
        "checkpoint_version,status,state_json,state_hash,reason,created_at) "
        "VALUES (?,?,'autonomy_recovery',?,'running',?,?,?,?)",
        (
            key,
            subject_id,
            version,
            canonical_json(state),
            content_hash(state),
            state["reason"],
            now,
        ),
    )


def admit_recovery_attempt(
    connection: sqlite3.Connection,
    subject_id: str,
    call_id: str,
    attempt_id: str,
    input_tokens: int,
    output_tokens: int,
    now: str,
) -> None:
    # Import lazily: both the ledger and autonomy components depend on core.
    from noyra.model.errors import BudgetExhaustedError

    state = read_recovery(connection, subject_id)
    if state is None or not state["active"]:
        return
    instant = parse_time(now)
    if instant < parse_time(state["checked_at"]):
        raise BudgetExhaustedError("recovery_clock_wait")
    reservations = [
        r
        for r in state["reservations"]
        if parse_time(r["reserved_at"]) > instant - timedelta(days=1)
    ]
    if any(r["attempt_id"] == attempt_id for r in reservations):
        return
    hour = sum(parse_time(r["reserved_at"]) > instant - timedelta(hours=1) for r in reservations)
    if (
        hour >= RECOVERY_HOURLY_ATTEMPTS
        or len(reservations) >= RECOVERY_DAILY_ATTEMPTS
        or sum(r["input_tokens"] for r in reservations) + input_tokens > RECOVERY_DAILY_INPUT_TOKENS
        or sum(r["output_tokens"] for r in reservations) + output_tokens
        > RECOVERY_DAILY_OUTPUT_TOKENS
    ):
        raise BudgetExhaustedError("autonomy_recovery_budget_exhausted")
    reservations.append(
        {
            "attempt_id": attempt_id,
            "call_id": call_id,
            "reserved_at": now,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    )
    state.update(reservations=reservations, checked_at=now)
    write_recovery(connection, subject_id, state, now)
