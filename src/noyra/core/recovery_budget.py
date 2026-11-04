"""Temporary model-attempt admission for recovery from verified project replay.

Reservations use the existing workflow history and the caller's ledger transaction.
They are deliberately conservative (no refunds); permanent budgets remain authoritative.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import IntegrityError
from .types import content_hash

RECOVERY_HOURLY_ATTEMPTS = 6
RECOVERY_DAILY_ATTEMPTS = 24
RECOVERY_DAILY_INPUT_TOKENS = 400_000
RECOVERY_DAILY_OUTPUT_TOKENS = 80_000
RECOVERY_MAX_RESERVATIONS = 128
EXIT_CONDITIONS = frozenset(
    {
        "project_coordination",
        "source_polling",
        "observation_backlog",
        "recent_model_failures",
        "wall_clock",
        "continuous_health",
    }
)


def recovery_high_water(state: dict[str, Any]) -> datetime:
    values = [state["checked_at"], *(r["reserved_at"] for r in state["reservations"])]
    if state.get("clock_high_water_at") is not None:
        values.append(state["clock_high_water_at"])
    return max(parse_time(value) for value in values)


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
    if state.get("clock_high_water_at") is not None:
        parse_time(state["clock_high_water_at"])
    if state.get("observed_at") is not None:
        parse_time(state["observed_at"])
    if state.get("clock_status", "normal") not in {"normal", "wall_clock_behind_history"}:
        raise IntegrityError("autonomy recovery clock status is invalid")
    conditions = state.get("exit_conditions", [])
    if not isinstance(conditions, list) or len(conditions) > len(EXIT_CONDITIONS):
        raise IntegrityError("autonomy recovery exit conditions are invalid")
    seen = set()
    for condition in conditions:
        if (
            not isinstance(condition, dict)
            or set(condition) != {"code", "satisfied", "observed_at", "unmet_since"}
            or not isinstance(condition.get("code"), str)
            or condition["code"] not in EXIT_CONDITIONS
            or condition["code"] in seen
            or type(condition.get("satisfied")) is not bool
        ):
            raise IntegrityError("autonomy recovery exit condition is invalid")
        seen.add(condition["code"])
        parse_time(condition["observed_at"])
        if condition["unmet_since"] is not None:
            parse_time(condition["unmet_since"])
    if state.get("last_health_evaluated_at") is not None:
        parse_time(state["last_health_evaluated_at"])
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
    from noyra.autonomy.workflow import DurableWorkflowStore

    from .checkpoint_clock import append_checkpoint

    key = workflow_key(subject_id)
    previous = connection.execute(
        "SELECT * FROM workflow_checkpoints WHERE workflow_id=? "
        "ORDER BY checkpoint_version DESC LIMIT 1",
        (key,),
    ).fetchone()
    if previous is not None:
        record = DurableWorkflowStore._from_row(previous)
        if (
            record.subject_id != subject_id
            or record.workflow_type != "autonomy_recovery"
            or record.status != "running"
        ):
            raise IntegrityError("autonomy recovery checkpoint owner is invalid")
    watermark = max(parse_time(now), recovery_high_water(state))
    if previous is not None:
        watermark = max(watermark, parse_time(record.created_at), recovery_high_water(record.state))
    state["clock_high_water_at"] = watermark.isoformat()
    append_checkpoint(
        connection,
        database_for_connection(connection),
        subject_id=subject_id,
        workflow_id=key,
        workflow_type="autonomy_recovery",
        version=1 if previous is None else int(previous["checkpoint_version"]) + 1,
        status="running",
        state=state,
        reason=state["reason"],
        observed_at=now,
        previous=previous,
    )


def database_for_connection(connection: sqlite3.Connection) -> Any:
    # Database is needed only for archive-aware clock receipts; use the already
    # open database identity without opening a connection or initializing schema.
    from pathlib import Path

    from .database import Database

    location = next(
        (row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"), ""
    )
    if not location:
        raise IntegrityError("persistent recovery database identity is unavailable")
    return Database(Path(location), initialize=False)


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
    if instant < recovery_high_water(state):
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
    state.update(reservations=reservations, checked_at=now, clock_high_water_at=now)
    write_recovery(connection, subject_id, state, now)
