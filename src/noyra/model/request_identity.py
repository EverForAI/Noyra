"""Stable business-round identities, independent of budgets, clocks and routing."""

from __future__ import annotations

import base64
import binascii

from noyra.core.database import Database

from .errors import ModelCallStateError

# Read-only scheduling view of the existing dispatch fence. Alias c is a
# model_calls row; the caller binds require_prepared_authorization for routed
# gateways. Never change a call/attempt or release an unknown reservation here.
UNRUNNABLE_BUSINESS_CALL_SQL = """(
 c.status IN ('unknown','executing') OR
 (c.status='prepared' AND EXISTS (
  SELECT 1 FROM audit_records a INDEXED BY idx_audit_subject_action_call
  WHERE a.subject_id=c.subject_id AND a.action='model_request_context_conflict'
  AND json_valid(a.payload_json) AND json_extract(a.payload_json,'$.call_id')=c.call_id)) OR
 (c.status='prepared' AND :require_prepared_authorization AND NOT EXISTS (
  SELECT 1 FROM audit_records a INDEXED BY idx_audit_subject_action_call
  WHERE a.subject_id=c.subject_id AND a.action='model_unknown_retry_authorized'
  AND json_valid(a.payload_json) AND json_extract(a.payload_json,'$.call_id')=c.call_id)) OR
 (c.status='failed' AND EXISTS (
  SELECT 1 FROM audit_records a INDEXED BY idx_audit_subject_action_call
  WHERE a.subject_id=c.subject_id AND a.action='model_unknown_retry_authorized'
  AND json_valid(a.payload_json) AND json_extract(a.payload_json,'$.call_id')=c.call_id)
  AND NOT EXISTS (
   SELECT 1 FROM audit_records a INDEXED BY idx_audit_subject_action_call
   WHERE a.subject_id=c.subject_id AND a.action='model_unknown_retry_closed'
   AND json_valid(a.payload_json) AND json_extract(a.payload_json,'$.call_id')=c.call_id)
  AND NOT (c.error_code='operator_cancelled' AND EXISTS (
   SELECT 1 FROM audit_records a INDEXED BY idx_audit_subject_action_call
   WHERE a.subject_id=c.subject_id AND a.action='model_unknown_retry_cancelled'
   AND json_valid(a.payload_json) AND json_extract(a.payload_json,'$.call_id')=c.call_id)))
)"""


def is_business_round(purpose: str) -> bool:
    parts = purpose.split(":")
    return (
        len(parts) == 2 and parts[0] in {"world_cognition", "sleep_reflection"} and bool(parts[1])
    ) or (
        len(parts) == 3
        and parts[0] in {"interaction_cognition", "intrinsic_thought"}
        and bool(parts[1])
        and parts[2].isdigit()
    )


def logical_key_from_route(physical_key: str, pool: str) -> str:
    marker = f":pool:{pool}:group:"
    head, separator, _tail = physical_key.rpartition(marker)
    if not separator:
        return physical_key  # Unrouted/legacy gateway.
    if head.startswith("noyra-route-v2:"):
        encoded = head.removeprefix("noyra-route-v2:")
        try:
            value = base64.b64decode(encoded, altchars=b"-_", validate=True).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error):
            raise ModelCallStateError("invalid routed logical identity") from None
        if base64.urlsafe_b64encode(value.encode()).decode("ascii") != encoded:
            raise ModelCallStateError("noncanonical routed logical identity")
        return value
    return head


def business_request_key(database: Database, subject_id: str, purpose: str) -> str:
    """Continue unresolved requests; only settled attempts advance a generation.

    Existing keys are read from the durable ledger, including pre-upgrade keys.
    A day change, other jobs' calls or a wait deadline cannot change the identity
    of an unresolved request. The router additionally fences the whole round.
    """
    if not is_business_round(purpose):
        raise ValueError("a supported business round is required")
    with database.connection() as connection:
        pending = connection.execute(
            "SELECT idempotency_key, resource_pool FROM model_calls "
            "WHERE subject_id=? AND purpose=? AND status IN ('prepared','executing','unknown') "
            "ORDER BY rowid LIMIT 1",
            (subject_id, purpose),
        ).fetchone()
        if pending is not None:
            return logical_key_from_route(
                str(pending["idempotency_key"]), str(pending["resource_pool"])
            )
        settled = connection.execute(
            "SELECT COUNT(*) FROM model_calls WHERE subject_id=? AND purpose=? "
            "AND status IN ('succeeded','failed')",
            (subject_id, purpose),
        ).fetchone()[0]
    return f"business-round-v1:{purpose}:{int(settled) + 1}"
