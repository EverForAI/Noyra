"""Local retirement of provably unsent, frozen memory-round requests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.types import canonical_json, content_hash
from noyra.model import ModelGateway
from noyra.model.reasoning import ReasoningEffort, normalize_reasoning_effort
from noyra.model.types import CallRecord, CompletionRequest

from .types import SemanticMemoryIntegrationProposal

if TYPE_CHECKING:
    from .memory_rounds import MemoryIntegrationRounds

FORMAT = "semantic-memory-unsent-retirement/v1"
ERROR_CODE = "memory_prepared_context_obsolete"


def configured_request(
    gateway: Any, state: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any] | None:
    # Routed gateways require explicit prepared retry, and arbitrary gateway
    # subclasses cannot attest the direct gateway's current request identity.
    if type(gateway) is not ModelGateway:
        return None
    from .memory_integration import SemanticMemoryIntegrator

    effort = (
        gateway.reasoning_effort
        if gateway.reasoning_capability_verified
        else normalize_reasoning_effort(gateway.model, gateway.reasoning_effort)
    )
    request = CompletionRequest(
        model=gateway.model,
        messages=SemanticMemoryIntegrator._messages(canonical_json(payload)),
        max_output_tokens=state["options"]["max_output_tokens"],
        temperature=state["options"]["temperature"],
        schema_name=SemanticMemoryIntegrationProposal.__name__,
        output_schema=SemanticMemoryIntegrationProposal.model_json_schema(),
        reasoning_effort=cast(ReasoningEffort | None, effort),
    )
    return {
        "provider": gateway.provider.name,
        "model": gateway.model,
        "resource_pool": gateway.resource_pool,
        "resource_group_id": gateway.resource_group_id,
        "request_hash": content_hash(request.model_dump(mode="json")),
    }


def retire_unsent(
    connection: Any,
    rounds: MemoryIntegrationRounds,
    state: dict[str, Any],
    calls: list[CallRecord],
    *,
    requires_explicit_retry: bool,
    reason: str,
    now: str,
    configured: Mapping[str, Any] | None = None,
) -> bool:
    if (
        requires_explicit_retry
        or not calls
        or reason not in {"configuration_changed", "context_changed"}
        or any(
            call.status != "prepared"
            or call.response is not None
            or call.error_code is not None
            or call.completed_at is not None
            or call.usage_estimated
            for call in calls
        )
    ):
        return False
    # Validate everything before updating any call. The caller's writer
    # transaction serializes an external worker authorizing an attempt.
    for call in calls:
        if (
            connection.execute(
                "SELECT 1 FROM model_attempts WHERE call_id=? LIMIT 1", (call.call_id,)
            ).fetchone()
            or connection.execute(
                "SELECT 1 FROM audit_records WHERE subject_id=? "
                "AND action IN ('model_unknown_retry_authorized','model_request_context_conflict') "
                "AND json_valid(payload_json) AND json_extract(payload_json,'$.call_id')=? LIMIT 1",
                (rounds.subject_id, call.call_id),
            ).fetchone()
        ):
            return False
        if reason == "configuration_changed" and (
            configured is None
            or all(getattr(call, key) == value for key, value in configured.items())
        ):
            return False
    now = EventStore._normalize_timestamp(now)
    for call in calls:
        if (
            connection.execute(
                "UPDATE model_calls SET status='failed',error_code=?,completed_at=? "
                "WHERE call_id=? AND subject_id=? AND purpose=? AND request_hash=? "
                "AND status='prepared'",
                (
                    ERROR_CODE,
                    now,
                    call.call_id,
                    rounds.subject_id,
                    rounds.purpose(state),
                    call.request_hash,
                ),
            ).rowcount
            != 1
        ):
            raise IntegrityError("memory unsent retirement phase changed")
        EventStore(rounds.database)._append_connection(
            connection,
            rounds.subject_id,
            "semantic_memory_unsent_retired",
            "memory_integration",
            {
                "format": FORMAT,
                "prepared_hash": rounds.prepared_hash(state),
                "call_id": call.call_id,
                "purpose": call.purpose,
                "request_hash": call.request_hash,
                "idempotency_key": call.idempotency_key,
                "reason": reason,
                "configured_identity_hash": None
                if configured is None
                else content_hash(dict(configured)),
            },
            privacy_level="private",
            causal_parent_ids=("evt_memory_call_bound_" + call.call_id,),
            occurred_at=now,
            event_id="evt_memory_unsent_retired_" + call.call_id,
        )
    return True


def verify_retirement(
    connection: Any, rounds: MemoryIntegrationRounds, state: dict[str, Any], call: CallRecord
) -> None:
    if call.error_code != ERROR_CODE:
        return
    row = connection.execute(
        "SELECT * FROM events WHERE event_id=?", ("evt_memory_unsent_retired_" + call.call_id,)
    ).fetchone()
    if row is None:
        raise IntegrityError("memory unsent retirement receipt is missing")
    value = EventStore(rounds.database).payload_from_row(row, connection=connection)
    if (
        row["subject_id"] != rounds.subject_id
        or row["event_type"] != "semantic_memory_unsent_retired"
        or row["source"] != "memory_integration"
        or row["privacy_level"] != "private"
        or call.status != "failed"
        or call.response is not None
        or call.usage_estimated
        or row["occurred_at"] != call.completed_at
        or not isinstance(value, dict)
        or content_hash(value) != row["payload_hash"]
        or set(value)
        != {
            "format",
            "prepared_hash",
            "call_id",
            "purpose",
            "request_hash",
            "idempotency_key",
            "reason",
            "configured_identity_hash",
        }
        or value["format"] != FORMAT
        or value["prepared_hash"] != rounds.prepared_hash(state)
        or any(
            value[key] != getattr(call, key)
            for key in ("call_id", "purpose", "request_hash", "idempotency_key")
        )
        or value["reason"] not in {"configuration_changed", "context_changed"}
        or (
            value["reason"] == "configuration_changed"
            and not rounds.digest(value["configured_identity_hash"])
        )
        or (value["reason"] == "context_changed" and value["configured_identity_hash"] is not None)
        or connection.execute(
            "SELECT 1 FROM model_attempts WHERE call_id=? LIMIT 1", (call.call_id,)
        ).fetchone()
    ):
        raise IntegrityError("memory unsent retirement receipt is invalid")
