"""Atomic abnormal sleep exit; never settles or retries model work."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.types import content_hash

from .errors import SleepStateConflictError

if TYPE_CHECKING:
    from .engine import SleepEngine

FORMAT = "sleep-failure/v1"
REASONS = frozenset({"operator_abort", "persistent_stage_failure"})


def fail_connection(
    engine: SleepEngine,
    connection: Any,
    row: Any,
    reason_code: str,
    actor: str,
    expected_version: int,
) -> None:
    if reason_code not in REASONS or not actor.strip():
        raise ValueError("invalid sleep failure reason or actor")
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError("sleep failure requires a positive expected version")
    from .integrity import SleepIntegrity

    SleepIntegrity(engine.database).verify(engine.subject_id)
    if row["status"] == "failed":
        receipt = verify_failure(engine.database, connection, row)
        if receipt is None or receipt["previous_version"] != expected_version:
            raise SleepStateConflictError("sleep failure snapshot changed")
        return
    if row["status"] in {"complete", "failed"} or row["version"] != expected_version:
        raise SleepStateConflictError("sleep failure snapshot changed")
    engine._require_lifecycle(connection, row)
    fatigue = connection.execute(
        "SELECT * FROM fatigue_states WHERE subject_id=?", (engine.subject_id,)
    ).fetchone()
    if fatigue is None:
        raise IntegrityError("sleep failure has no fatigue state")
    engine.fatigue._from_row(fatigue)
    now = EventStore._normalize_timestamp(engine.clock())
    reason = "sleep_failure:" + reason_code
    engine._transition_run(connection, row, "failed", reason, completed_at=now)
    engine.lifecycle._transition_connection(
        connection, "paused", "sleep interrupted; recovery required", actor=actor
    )
    terminal = engine._get_row(connection, row["sleep_id"])
    engine.events._append_connection(
        connection,
        engine.subject_id,
        "sleep_failed",
        "sleep",
        {
            "format": FORMAT,
            "sleep_id": row["sleep_id"],
            "from_status": row["status"],
            "previous_version": expected_version,
            "previous_state_hash": row["state_hash"],
            "terminal_state_hash": terminal["state_hash"],
            "reason_code": reason_code,
            "completed_at": now,
        },
        privacy_level="private",
        causal_parent_ids=(),
        occurred_at=now,
        event_id="evt_sleep_failed_" + row["sleep_id"],
    )


def verify_failure(database: Any, connection: Any, row: Any) -> dict[str, Any] | None:
    from .engine import SleepEngine

    transition = connection.execute(
        "SELECT reason,from_status FROM sleep_transitions WHERE sleep_id=? "
        "AND to_status='failed' ORDER BY rowid DESC LIMIT 1",
        (row["sleep_id"],),
    ).fetchone()
    event = connection.execute(
        "SELECT * FROM events WHERE event_id=?", ("evt_sleep_failed_" + row["sleep_id"],)
    ).fetchone()
    if event is None:
        if transition is not None and transition["reason"].startswith("sleep_failure:"):
            raise IntegrityError("sleep failure receipt is missing")
        return None  # Historical failed rows without the new protocol remain readable.
    value = EventStore(database).payload_from_row(event, connection=connection)
    if (
        row["status"] != "failed"
        or event["subject_id"] != row["subject_id"]
        or event["event_type"] != "sleep_failed"
        or event["source"] != "sleep"
        or event["privacy_level"] != "private"
        or content_hash(value) != event["payload_hash"]
        or set(value)
        != {
            "format",
            "sleep_id",
            "from_status",
            "previous_version",
            "previous_state_hash",
            "terminal_state_hash",
            "reason_code",
            "completed_at",
        }
        or value["format"] != FORMAT
        or value["sleep_id"] != row["sleep_id"]
        or value["from_status"] not in {"winding_down", "reflective_sleep", "deep_sleep", "waking"}
        or type(value["previous_version"]) is not int
        or value["previous_version"] != row["version"] - 1
        or value["reason_code"] not in REASONS
        or transition is None
        or transition["reason"] != "sleep_failure:" + value["reason_code"]
        or transition["from_status"] != value["from_status"]
        or value["terminal_state_hash"] != row["state_hash"]
        or value["completed_at"] != row["completed_at"]
        or event["occurred_at"] != row["completed_at"]
        or value["previous_state_hash"]
        != SleepEngine._state_hash(
            value["from_status"],
            row["trigger_type"],
            row["trigger_reason"],
            bool(row["emergency"]),
            float(row["pre_sleep_fatigue"]),
            row["wake_after"],
            row["reflection_event_id"],
            row["checkpoint_id"],
            row["version"] - 1,
            None,
        )
    ):
        raise IntegrityError("sleep failure receipt is invalid")
    return value
