"""Persistent, run/version-scoped failure evidence for sleep advancement."""

from __future__ import annotations

from typing import Any

from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.sleep import SleepEngine, SleepRunRecord
from noyra.sleep.errors import SleepStateConflictError


def failures(engine: SleepEngine, connection: Any, run: SleepRunRecord) -> int:
    row = connection.execute(
        "SELECT * FROM events WHERE subject_id=? AND event_type='sleep_stage_failed' "
        "ORDER BY rowid DESC LIMIT 1",
        (engine.subject_id,),
    ).fetchone()
    if row is None:
        return 0
    payload = engine.events.payload_from_row(row, connection=connection)
    if (
        row["source"] != "sleep"
        or row["privacy_level"] != "private"
        or content_hash(payload) != row["payload_hash"]
        or set(payload) != {"sleep_id", "run_version", "run_state_hash", "count", "error_type"}
        or type(payload["count"]) is not int
        or payload["count"] < 1
        or type(payload["run_version"]) is not int
        or payload["run_version"] < 1
        or not isinstance(payload["sleep_id"], str)
        or not isinstance(payload["error_type"], str)
    ):
        raise IntegrityError("sleep failure counter evidence is invalid")
    if payload["sleep_id"] != run.sleep_id or payload["run_version"] != run.version:
        return 0
    current = engine._get_row(connection, run.sleep_id)
    if current["state_hash"] != payload["run_state_hash"]:
        raise IntegrityError("sleep failure counter context changed")
    return int(payload["count"])


def record_failure(engine: SleepEngine, run: SleepRunRecord, error_type: str) -> None:
    with engine.database.transaction() as connection:
        row = engine._get_row(connection, run.sleep_id)
        if row["status"] != run.status or row["version"] != run.version:
            raise SleepStateConflictError("sleep changed while a stage was running")
        engine._require_lifecycle(connection, row)
        count = failures(engine, connection, run) + 1
        engine.events._append_connection(
            connection,
            engine.subject_id,
            "sleep_stage_failed",
            "sleep",
            {
                "sleep_id": run.sleep_id,
                "run_version": run.version,
                "run_state_hash": row["state_hash"],
                "count": count,
                "error_type": error_type,
            },
            privacy_level="private",
            causal_parent_ids=(),
            occurred_at=engine.clock(),
            event_id=f"evt_sleep_stage_failed_{run.sleep_id}_{run.version}_{count}",
        )
