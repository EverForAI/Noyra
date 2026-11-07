"""Private, versioned rejection receipts; never infer old decisions from current state."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.types import canonical_json, content_hash, strict_json_loads
from noyra.model.ledger import ModelLedger

RECEIPT_FORMAT = "noyra-intrinsic-decision/v1"
REJECTION_CODES = {
    "thought repeats an existing result": "thought_duplicate_result",
    "thought goal daily limit reached": "thought_goal_daily_limit",
    "thought goal lacks current affect support": "thought_affect_missing",
    "thought goal motive is too weak": "thought_affect_weak",
    "thought agenda no longer executable": "thought_agenda_changed",
    "goal decision is unavailable or duplicated": "goal_unavailable_or_duplicate",
    "goal decision cites unavailable events": "goal_evidence_unavailable",
    "goal decision changes weight too abruptly": "goal_weight_delta",
    "goal decision disposition has no state change": "goal_no_state_change",
    "goal decision transition is not allowed": "goal_transition_disallowed",
    "every active goal requires an explicit decision": "goal_active_decision_missing",
    "goal proposal exceeds the active-goal limit": "goal_active_limit",
    "focus must identify an active goal": "goal_focus_inactive",
    "focus goal requires an explicit decision": "goal_focus_decision_missing",
    "a proposal without active goals cannot set focus": "goal_focus_without_active_goal",
    "duplicate value development": "value_duplicate",
    "value already exists": "value_already_exists",
    "new value starts too strongly": "value_initial_strength",
    "unknown value revision": "value_unavailable",
    "value title cannot be replaced": "value_title_changed",
    "value weight changed too quickly": "value_weight_delta",
    "value confidence changed too quickly": "value_confidence_delta",
    "mission cites unavailable values": "mission_values_unavailable",
    "mission lacks enough distinct values": "mission_values_insufficient",
    "mission formation requires repeated sleep": "mission_sleep_insufficient",
    "mission candidate starts too strongly": "mission_initial_strength",
    "unknown mission revision": "mission_unavailable",
    "mission commitment changed too quickly": "mission_commitment_delta",
    "mission confidence changed too quickly": "mission_confidence_delta",
    "mission adoption requires more sleep": "mission_adoption_sleep_insufficient",
    "adopted mission lacks support": "mission_adoption_support_insufficient",
    "value cites unavailable evidence": "value_evidence_unavailable",
    "value requires durable interpreted evidence": "value_interpreted_evidence_missing",
    "mission cites unavailable evidence": "mission_evidence_unavailable",
}


class DecisionValidationError(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.code = REJECTION_CODES.get(message, "validation_contract_rejected")
        if message.startswith("thought cites unavailable "):
            self.code = "thought_evidence_unavailable"


def rejection_receipt(
    *,
    validator: str,
    call_id: str,
    error: Exception,
    context_hash: str,
    context_version: str,
    snapshot: dict[str, Any],
    proposal: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "format": RECEIPT_FORMAT,
        "validator_version": validator + "/v1",
        "model_call_id": call_id,
        "reason_code": getattr(
            error,
            "code",
            "proposal_schema_invalid"
            if isinstance(error, ValidationError)
            else "validation_state_conflict",
        ),
        "context_hash": context_hash,
        "context_role": "validation_time",
        "context_version": context_version,
        "validation_context": snapshot,
        "validation_context_hash": content_hash(snapshot),
        "proposal_hash": None if proposal is None else content_hash(proposal),
    }


def rejection_event_id(subject_id: str, call_id: str, validator: str) -> str:
    return "evt_decision_" + content_hash([subject_id, call_id, validator])


def append_rejection(
    connection: Any, database: Database, subject_id: str, payload: dict[str, Any], now: str
) -> None:
    # Keep the first validation receipt for a call. Recovery must not rewrite
    # that evidence with a later context or generate duplicate feedback events.
    event_id = rejection_event_id(
        subject_id, str(payload["model_call_id"]), str(payload["validator_version"])
    )
    existing = connection.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone()
    if existing is not None:
        return
    EventStore(database)._append_connection(
        connection,
        subject_id,
        "intrinsic_decision_rejected",
        "subject_supervisor",
        payload,
        privacy_level="private",
        causal_parent_ids=(),
        occurred_at=now,
        event_id=event_id,
    )


def unsettled_decision(
    database: Database, subject_id: str, family: str, purpose: str
) -> str | None:
    """Do not turn a context/day change into permission to repeat uncertain work."""
    with database.connection() as connection:
        uncertain = connection.execute(
            "SELECT 1 FROM model_calls WHERE subject_id=? AND purpose LIKE ? "
            "AND status IN ('unknown','executing') LIMIT 1",
            (subject_id, family + ":%"),
        ).fetchone()
        if uncertain is not None:
            return "waiting_reconciliation"
        if family == "motivation_development":
            earlier = connection.execute(
                "SELECT 1 FROM model_calls c WHERE c.subject_id=? AND c.purpose LIKE ? "
                "AND c.purpose!=? AND c.status IN ('prepared','succeeded') "
                "AND NOT EXISTS(SELECT 1 FROM motivation_reviews r WHERE r.subject_id=c.subject_id "
                "AND r.model_call_id=c.call_id) LIMIT 1",
                (subject_id, family + ":%", purpose),
            ).fetchone()
            if earlier is not None:
                return "waiting_previous_context"
    return None


def replay_motivation_validation(receipt: dict[str, Any], proposal: dict[str, Any]) -> str:
    """Re-evaluate only the recorded v1 policy and evidence, with no live-state reads."""
    from .motivation import (
        MissionCandidateRecord,
        MotivationDevelopment,
        MotivationDevelopmentValidationError,
        ValueProfileRecord,
        _MotivationContext,
    )
    from .settings import CognitionSettings
    from .types import MotivationDevelopmentProposal

    if receipt.get("validator_version") != "motivation/v1":
        return "validator_version_unavailable"
    snapshot = receipt["validation_context"]
    policy = snapshot["policy"]
    # Instantiate only the pure validation surface; there is deliberately no
    # database or gateway on this object, so it cannot replay an external call.
    validator = object.__new__(MotivationDevelopment)
    setting_keys = (
        "minimum_mission_value_count",
        "minimum_mission_sleep_count",
        "minimum_mission_adoption_sleep_count",
        "max_value_weight_delta",
    )
    validator.settings = CognitionSettings(**{key: policy[key] for key in setting_keys})
    if validator._validation_policy() != policy:
        return "validator_version_unavailable"
    context = _MotivationContext(
        "",
        receipt["context_hash"],
        frozenset(snapshot["event_ids"]),
        frozenset(snapshot["memory_ids"]),
        frozenset(snapshot["belief_ids"]),
        frozenset(snapshot["goal_ids"]),
        frozenset(snapshot["relationship_ids"]),
        {key: ValueProfileRecord(**value) for key, value in snapshot["values"].items()},
        {key: MissionCandidateRecord(**value) for key, value in snapshot["missions"].items()},
        snapshot["completed_sleep_count"],
    )
    try:
        validator._validate(
            MotivationDevelopmentProposal.model_validate_json(canonical_json(proposal)), context
        )
    except MotivationDevelopmentValidationError as error:
        return error.code
    return "accepted_by_recorded_validator"


def diagnose_motivation_review(
    database: Database, subject_id: str, review_id: str
) -> dict[str, Any]:
    """Read historical facts only. No gateway, request replay or current-state validation."""
    from .motivation import MotivationDevelopment

    with database.read_transaction() as connection:
        review = connection.execute(
            "SELECT * FROM motivation_reviews WHERE subject_id=? AND review_id=?",
            (subject_id, review_id),
        ).fetchone()
        if review is None:
            raise ValueError("motivation review is unavailable for this subject")
        record = MotivationDevelopment._review_from_row(review)
        row = connection.execute(
            "SELECT request_json,capture_policy_version FROM model_calls "
            "WHERE subject_id=? AND call_id=?",
            (subject_id, review["model_call_id"]),
        ).fetchone()
        event = connection.execute(
            "SELECT * FROM events WHERE subject_id=? AND event_id=?",
            (subject_id, rejection_event_id(subject_id, record.model_call_id, "motivation/v1")),
        ).fetchone()
        events = EventStore(database)
        if event is not None and event["payload_archive_key"] is not None:
            from noyra.core.event_archive import EventPayloadArchive

            events._archive = EventPayloadArchive(
                database,
                database.path.parent / "subject" / "cold",
                subject_id=subject_id,
                create_root=False,
                read_only_root=True,
                cache_cloud_restores=False,
            )
            events._archive_subject_id = subject_id
        receipt = None if event is None else events.payload_from_row(event, connection=connection)
        if receipt is not None and content_hash(receipt) != event["payload_hash"]:
            raise IntegrityError("historical rejection receipt hash mismatch")
    call = ModelLedger(database).get_call(record.model_call_id)
    if call.subject_id != subject_id or call.status != "succeeded":
        raise IntegrityError("motivation review model evidence is invalid")
    if receipt is not None and (
        receipt.get("format") != RECEIPT_FORMAT
        or content_hash(receipt.get("validation_context")) != receipt.get("validation_context_hash")
        or receipt.get("model_call_id") != call.call_id
        or receipt.get("proposal_hash") != review["proposal_hash"]
        or receipt.get("context_hash") != review["source_state_hash"]
    ):
        raise IntegrityError("historical decision context is invalid")
    proposal = strict_json_loads(review["proposal_json"])
    response_content = (call.response or {}).get("content")
    if not isinstance(response_content, str) or strict_json_loads(response_content) != proposal:
        raise IntegrityError("motivation historical response disagrees with its review")
    replay = None
    if receipt is not None:
        try:
            replay = replay_motivation_validation(receipt, proposal)
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError("historical validation snapshot is incomplete") from error
    captured = row is not None and row["request_json"] is not None
    diagnosis = (
        "not_rejected"
        if record.status != "rejected"
        else "recorded_rejection"
        if receipt is not None
        else "historical_evidence_insufficient"
    )
    return {
        "review_id": review_id,
        "status": record.status,
        "model_call_id": call.call_id,
        "request_captured": captured,
        "response_available": call.response is not None,
        "capture_policy_version": None if row is None else row["capture_policy_version"],
        "diagnosis": diagnosis,
        "offline_validation": replay,
        "matches_recorded_reason": None if receipt is None else replay == receipt["reason_code"],
        "reason_codes": [] if receipt is None else [receipt["reason_code"]],
        "validator_versions": [] if receipt is None else [receipt["validator_version"]],
        "context_hashes": [] if receipt is None else [receipt["context_hash"]],
        "limitation": (
            "original_validation_context_and_policy_not_recorded"
            if diagnosis == "historical_evidence_insufficient"
            else "recorded_validator_implementation_unavailable"
            if replay == "validator_version_unavailable"
            else None
        ),
    }
