"""Checkpoint sequence order with explicit, immutable wall-clock exception evidence."""

from __future__ import annotations

from typing import Any

from .database import Database
from .errors import IntegrityError
from .events import EventStore
from .scheduling_clock import aware_time
from .types import canonical_json, content_hash, strict_json_loads

FORMAT = "workflow-clock/v1"
EVENT_TYPE = "workflow_clock_evidence"


def receipt_id(subject: str, workflow: str, version: int) -> str:
    return "evt_workflow_clock_" + content_hash([subject, workflow, version])


def receipt(
    connection: Any,
    row: Any,
    database: Database | None = None,
    *,
    events: EventStore | None = None,
) -> dict[str, Any] | None:
    event = connection.execute(
        "SELECT * FROM events WHERE event_id=?",
        (receipt_id(row["subject_id"], row["workflow_id"], row["checkpoint_version"]),),
    ).fetchone()
    if event is None:
        return None
    if database is not None:
        payload = (events or EventStore(database)).payload_from_row(event, connection=connection)
    else:
        if event["payload_archive_key"] is not None:
            raise IntegrityError("archived checkpoint clock evidence needs its database")
        try:
            payload = strict_json_loads(event["payload_json"])
        except (TypeError, ValueError) as error:
            raise IntegrityError("checkpoint clock evidence is invalid") from error
    if (
        not isinstance(payload, dict)
        or content_hash(payload) != event["payload_hash"]
        or event["subject_id"] != row["subject_id"]
        or event["event_type"] != EVENT_TYPE
        or event["source"] != "checkpoint_clock"
        or event["privacy_level"] != "private"
        or set(payload)
        != {
            "format",
            "mode",
            "row_hash",
            "previous_hash",
            "observed_at",
            "high_water_at",
            "actor",
            "reason",
            "evidence_sha256",
        }
        or payload.get("format") != FORMAT
        or not isinstance(payload.get("mode"), str)
        or payload.get("mode") not in {"rollback", "legacy_acknowledged"}
        or payload.get("row_hash") != content_hash(dict(row))
    ):
        raise IntegrityError("checkpoint clock evidence is invalid")
    aware_time(payload["observed_at"])
    aware_time(payload["high_water_at"])
    if payload["mode"] == "rollback":
        if (
            payload["actor"] != "runtime"
            or payload["reason"] != "wall_clock_regressed"
            or payload["evidence_sha256"] is not None
        ):
            raise IntegrityError("checkpoint rollback evidence is invalid")
    elif (
        not isinstance(payload["actor"], str)
        or not payload["actor"].strip()
        or payload["actor"].casefold().strip() in {"subject", "runtime"}
        or len(payload["actor"]) > 256
        or not isinstance(payload["reason"], str)
        or not payload["reason"].strip()
        or len(payload["reason"]) > 2000
        or not isinstance(payload["evidence_sha256"], str)
        or len(payload["evidence_sha256"]) != 64
        or set(payload["evidence_sha256"]) - set("0123456789abcdef")
    ):
        raise IntegrityError("legacy checkpoint clock acknowledgement is invalid")
    return payload


def append_receipt(
    connection: Any,
    database: Database,
    row: Any,
    previous: Any,
    observed_at: str,
    high_water_at: str,
    *,
    mode: str = "rollback",
    actor: str = "runtime",
    reason: str = "wall_clock_regressed",
    evidence_sha256: str | None = None,
) -> None:
    EventStore(database)._append_connection(
        connection,
        row["subject_id"],
        EVENT_TYPE,
        "checkpoint_clock",
        {
            "format": FORMAT,
            "mode": mode,
            "row_hash": content_hash(dict(row)),
            "previous_hash": None if previous is None else content_hash(dict(previous)),
            "observed_at": observed_at,
            "high_water_at": high_water_at,
            "actor": actor,
            "reason": reason,
            "evidence_sha256": evidence_sha256,
        },
        privacy_level="private",
        causal_parent_ids=(),
        occurred_at=observed_at,
        event_id=receipt_id(row["subject_id"], row["workflow_id"], row["checkpoint_version"]),
    )


def high_water(connection: Any, previous: Any, database: Database | None = None) -> str | None:
    if previous is None:
        return None
    saved = receipt(connection, previous, database)
    return previous["created_at"] if saved is None else str(saved["high_water_at"])


def verify(
    connection: Any,
    row: Any,
    previous: Any,
    watermark: str | None,
    database: Database | None = None,
    *,
    events: EventStore | None = None,
) -> str:
    current = aware_time(row["created_at"])
    before = None if watermark is None else aware_time(watermark)
    saved = receipt(connection, row, database, events=events)
    if saved is not None:
        observed = aware_time(saved["observed_at"])
        high = aware_time(saved["high_water_at"])
        if saved["previous_hash"] != (None if previous is None else content_hash(dict(previous))):
            raise IntegrityError("checkpoint clock previous row mismatch")
        if saved["mode"] == "rollback":
            if before is None or observed >= before or current != before or high != before:
                raise IntegrityError("checkpoint rollback ordering is invalid")
        elif before is None or current >= before or observed != current or high != before:
            raise IntegrityError("legacy checkpoint clock ordering is invalid")
        return str(saved["high_water_at"])
    if before is not None and current < before:
        raise IntegrityError("workflow checkpoint clock requires verified legacy evidence")
    return str(row["created_at"])


def append_checkpoint(
    connection: Any,
    database: Database,
    *,
    subject_id: str,
    workflow_id: str,
    workflow_type: str,
    version: int,
    status: str,
    state: dict[str, Any],
    reason: str,
    observed_at: str,
    previous: Any,
) -> Any:
    watermark = high_water(connection, previous, database)
    aware_time(observed_at)
    logical = (
        observed_at
        if watermark is None or aware_time(observed_at) >= aware_time(watermark)
        else watermark
    )
    connection.execute(
        "INSERT INTO workflow_checkpoints(workflow_id,subject_id,workflow_type,"
        "checkpoint_version,status,state_json,state_hash,reason,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            workflow_id,
            subject_id,
            workflow_type,
            version,
            status,
            canonical_json(state),
            content_hash(state),
            reason,
            logical,
        ),
    )
    row = connection.execute(
        "SELECT * FROM workflow_checkpoints WHERE workflow_id=? AND checkpoint_version=?",
        (workflow_id, version),
    ).fetchone()
    if logical != observed_at:
        append_receipt(connection, database, row, previous, observed_at, logical)
    return row
