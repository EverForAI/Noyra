from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def strict_json_loads(value: str | bytes | bytearray) -> Any:
    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON value is invalid: {token}")

    return json.loads(value, parse_constant=reject_constant)


def strict_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid integers")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("value is not an exact finite integer")
        return int(value)
    if isinstance(value, str):
        return int(value.strip(), 10)
    raise TypeError("value is not an integer-compatible SQLite scalar")


def strict_bool(value: Any) -> bool:
    parsed = strict_int(value)
    if parsed not in {0, 1}:
        raise ValueError("boolean flags must be exactly zero or one")
    return bool(parsed)


def strict_finite_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError("value is not a numeric SQLite scalar")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite numeric values are invalid")
    return parsed


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class SubjectIdentity:
    subject_id: str
    project_name: str
    genesis_hash: str
    personal_name: str | None
    identity_status: str
    created_at: str
    updated_at: str
    state_version: int
    model_name: str | None
    last_checkpoint: str | None
    origin_subject_id: str | None
    branch_reason: str | None


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    subject_id: str
    event_type: str
    source: str
    occurred_at: str
    observed_at: str
    payload: dict[str, Any]
    payload_hash: str
    privacy_level: str
    causal_parent_ids: tuple[str, ...]
    processing_status: str


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: str
    subject_id: str
    state_version: int
    state: dict[str, Any]
    state_hash: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class RuntimeState:
    subject_id: str
    state: str
    reason: str
    version: int
    changed_at: str


@dataclass(frozen=True)
class ActionRecord:
    action_id: str
    subject_id: str
    goal_id: str | None
    project_id: str | None
    phase_id: str | None
    strategy_id: str | None
    action_type: str
    tool: str
    target: str
    input_hash: str
    idempotency_key: str
    expected_outcome: str
    side_effect: bool
    status: str
    retry_count: int
    resource_cost: dict[str, Any]
    result: dict[str, Any] | None
    prepared_at: str
    started_at: str | None
    completed_at: str | None
