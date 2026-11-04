"""Read-only delivery outcomes derived from original rows and immutable reconciliation.

A terminal receipt settles uncertainty for readers, never authorizes another send.
All multi-query entry points require the caller's read/write transaction snapshot.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import IntegrityError
from .types import content_hash, strict_int, strict_json_loads

DELIVERY_EVIDENCE_MAX_BYTES = 16_384
DELIVERY_STATUSES = frozenset({"queued", "sending", "delivered", "failed", "unknown", "cancelled"})
TERMINAL_DELIVERY_OUTCOMES = frozenset({"delivered", "failed", "cancelled"})

# Use this bounded-list predicate only AFTER delivery_status_counts validated
# the subject's receipts in the SAME snapshot. Do not use SQL to trust hashes.
UNRESOLVED_DELIVERY_SQL = """(
 d.status IN ('queued','sending') OR
 (d.status='unknown' AND NOT EXISTS (
  SELECT 1 FROM interaction_delivery_reconciliations r
  WHERE r.delivery_id=d.delivery_id AND r.subject_id=d.subject_id
   AND r.outcome IN ('delivered','failed','cancelled')))
)"""


@dataclass(frozen=True)
class DeliveryReconciliationRecord:
    reconciliation_id: str
    delivery_id: str
    subject_id: str
    sequence: int
    source: str
    outcome: str
    provider_message_id: str | None
    provider_status: str | None
    evidence: Mapping[str, Any]
    evidence_hash: str
    actor: str
    reason: str
    state_hash: str
    created_at: str


def reconciliation_record(row: Any) -> DeliveryReconciliationRecord:
    reconciliation_id = row["reconciliation_id"]
    raw_evidence = row["evidence_json"]
    if not isinstance(raw_evidence, str):
        raise IntegrityError(f"delivery reconciliation evidence is invalid: {reconciliation_id}")
    if len(raw_evidence.encode("utf-8")) > DELIVERY_EVIDENCE_MAX_BYTES:
        raise IntegrityError(f"delivery reconciliation evidence is too large: {reconciliation_id}")
    try:
        evidence = strict_json_loads(raw_evidence)
        sequence = strict_int(row["sequence"])
    except (TypeError, ValueError) as error:
        raise IntegrityError(
            f"delivery reconciliation evidence is invalid: {reconciliation_id}"
        ) from error
    if not isinstance(evidence, dict) or sequence <= 0:
        raise IntegrityError(f"delivery reconciliation is invalid: {reconciliation_id}")
    source = row["source"]
    outcome = row["outcome"]
    provider_message_id = row["provider_message_id"]
    provider_status = row["provider_status"]
    if (
        source not in {"provider", "operator"}
        or outcome not in {"delivered", "failed", "cancelled", "unknown", "unsupported", "error"}
        or (source == "operator" and outcome not in {"delivered", "failed", "cancelled"})
        or (source == "provider" and outcome == "cancelled")
        or not isinstance(row["actor"], str)
        or not row["actor"].strip()
        or not isinstance(row["reason"], str)
        or not row["reason"].strip()
        or not isinstance(row["created_at"], str)
        or not row["created_at"]
        or (
            provider_message_id is not None
            and (not isinstance(provider_message_id, str) or len(provider_message_id) > 512)
        )
        or (
            provider_status is not None
            and (not isinstance(provider_status, str) or len(provider_status) > 256)
        )
    ):
        raise IntegrityError(f"delivery reconciliation is invalid: {reconciliation_id}")
    evidence_hash = content_hash(evidence)
    state = {
        "reconciliation_id": reconciliation_id,
        "delivery_id": row["delivery_id"],
        "subject_id": row["subject_id"],
        "sequence": sequence,
        "source": source,
        "outcome": outcome,
        "provider_message_id": provider_message_id,
        "provider_status": provider_status,
        "evidence_hash": evidence_hash,
        "actor": row["actor"],
        "reason": row["reason"],
        "created_at": row["created_at"],
    }
    if row["evidence_hash"] != evidence_hash or row["state_hash"] != content_hash(state):
        raise IntegrityError(f"delivery reconciliation hash mismatch: {reconciliation_id}")
    return DeliveryReconciliationRecord(
        str(reconciliation_id),
        str(row["delivery_id"]),
        str(row["subject_id"]),
        sequence,
        str(row["source"]),
        str(row["outcome"]),
        row["provider_message_id"],
        row["provider_status"],
        evidence,
        str(row["evidence_hash"]),
        str(row["actor"]),
        str(row["reason"]),
        str(row["state_hash"]),
        str(row["created_at"]),
    )


def verified_reconciliations(
    connection: Any, subject_id: str, *, delivery_id: str | None = None
) -> Iterator[DeliveryReconciliationRecord]:
    # The subject-indexed orphan probe also catches receipts forged to name
    # this subject while their original delivery belongs to another one.
    if delivery_id is None:
        orphan = connection.execute(
            "SELECT 1 FROM interaction_delivery_reconciliations r "
            "LEFT JOIN interaction_deliveries d ON d.delivery_id=r.delivery_id "
            "WHERE r.subject_id=? AND (d.subject_id IS NULL OR d.subject_id!=r.subject_id) "
            "LIMIT 1",
            (subject_id,),
        ).fetchone()
        if orphan is not None:
            raise IntegrityError("delivery reconciliation ownership is invalid")
    where = "d.subject_id=?" if delivery_id is None else "r.delivery_id=?"
    rows = connection.execute(
        "SELECT r.*,d.status AS delivery_status,d.subject_id AS delivery_subject_id "
        "FROM interaction_delivery_reconciliations r "
        "LEFT JOIN interaction_deliveries d ON d.delivery_id=r.delivery_id WHERE "
        + where
        + " ORDER BY r.delivery_id,r.sequence",
        (subject_id if delivery_id is None else delivery_id,),
    )
    prior_id: str | None = None
    sequence = 0
    terminal = False
    for row in rows:
        record = reconciliation_record(row)
        if (
            record.subject_id != subject_id
            or row["delivery_subject_id"] != subject_id
            or row["delivery_status"] != "unknown"
            or (delivery_id is not None and record.delivery_id != delivery_id)
        ):
            raise IntegrityError("delivery reconciliation ownership or baseline is invalid")
        if record.delivery_id != prior_id:
            prior_id, sequence, terminal = record.delivery_id, 0, False
        if terminal or record.sequence != sequence + 1:
            raise IntegrityError("delivery reconciliation sequence is invalid")
        sequence = record.sequence
        terminal = record.outcome in TERMINAL_DELIVERY_OUTCOMES
        yield record


def _terminal_record_from_joined_row(
    row: Any, *, subject_id: str, delivery_id: str
) -> DeliveryReconciliationRecord:
    record = reconciliation_record(row)
    if (
        record.subject_id != subject_id
        or record.delivery_id != delivery_id
        or row["delivery_subject_id"] != subject_id
        or row["delivery_status"] != "unknown"
        or record.sequence != row["reconciliation_count"]
        or record.outcome not in TERMINAL_DELIVERY_OUTCOMES
    ):
        raise IntegrityError("terminal delivery reconciliation ownership or baseline is invalid")
    return record


def terminal_delivery_reconciliation(
    connection: Any, delivery: Any
) -> DeliveryReconciliationRecord | None:
    if delivery["status"] not in DELIVERY_STATUSES:
        raise IntegrityError("delivery status is invalid")
    rows = connection.execute(
        "SELECT r.*,d.status AS delivery_status,d.subject_id AS delivery_subject_id, "
        "(SELECT COUNT(*) FROM interaction_delivery_reconciliations h "
        " WHERE h.delivery_id=r.delivery_id) AS reconciliation_count "
        "FROM interaction_delivery_reconciliations r "
        "JOIN interaction_deliveries d ON d.delivery_id=r.delivery_id "
        "WHERE r.delivery_id=? AND r.outcome IN ('delivered','failed','cancelled') "
        "ORDER BY r.sequence",
        (delivery["delivery_id"],),
    ).fetchall()
    if len(rows) > 1:
        raise IntegrityError("delivery has multiple terminal reconciliations")
    if not rows:
        return None
    return _terminal_record_from_joined_row(
        rows[0], subject_id=str(delivery["subject_id"]), delivery_id=str(delivery["delivery_id"])
    )


def terminal_delivery_reconciliations(
    connection: Any, subject_id: str
) -> Iterator[DeliveryReconciliationRecord]:
    """Yield hash-checked terminal receipts for raw unknown rows.

    The correlated count verifies that the terminal is the final contiguous
    sequence. Full earlier row hashes are checked by history/integrity reads.
    """
    rows = connection.execute(
        "SELECT r.*,d.status AS delivery_status,d.subject_id AS delivery_subject_id, "
        "(SELECT COUNT(*) FROM interaction_delivery_reconciliations h "
        " WHERE h.delivery_id=r.delivery_id) AS reconciliation_count "
        "FROM interaction_delivery_reconciliations r "
        "JOIN interaction_deliveries d ON d.delivery_id=r.delivery_id "
        "WHERE d.subject_id=? AND d.status='unknown' "
        "AND r.outcome IN ('delivered','failed','cancelled') ORDER BY d.delivery_id",
        (subject_id,),
    )
    prior_delivery: str | None = None
    for row in rows:
        record = _terminal_record_from_joined_row(
            row, subject_id=subject_id, delivery_id=str(row["delivery_id"])
        )
        if record.delivery_id == prior_delivery:
            raise IntegrityError("delivery has multiple terminal reconciliations")
        prior_delivery = record.delivery_id
        yield record


def effective_delivery_status(connection: Any, delivery: Any) -> str:
    resolved = terminal_delivery_reconciliation(connection, delivery)
    return str(delivery["status"]) if resolved is None else resolved.outcome


def delivery_status_counts(connection: Any, subject_id: str) -> dict[str, int]:
    """Count verified effective outcomes, preserving the original ledger rows.

    Receipts stream by indexed delivery/sequence; no message bodies, secret
    files, provider calls, or database writes are needed for this projection.
    """
    counts: dict[str, int] = {}
    for row in connection.execute(
        "SELECT status,COUNT(*) AS n FROM interaction_deliveries "
        "WHERE subject_id=? GROUP BY status",
        (subject_id,),
    ):
        if row["status"] not in DELIVERY_STATUSES:
            raise IntegrityError("delivery status is invalid")
        counts[str(row["status"])] = int(row["n"])
    for record in terminal_delivery_reconciliations(connection, subject_id):
        counts["unknown"] = counts.get("unknown", 0) - 1
        if counts["unknown"] < 0:
            raise IntegrityError("delivery reconciliation count is invalid")
        counts[record.outcome] = counts.get(record.outcome, 0) + 1
    return {status: count for status, count in counts.items() if count}
