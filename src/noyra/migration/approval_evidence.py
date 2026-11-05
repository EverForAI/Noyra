"""Typed provenance over existing immutable migration audit records."""

from __future__ import annotations

from typing import Any, Literal

from noyra.core.types import content_hash, strict_json_loads

from .policy import MigrationStore

ReviewKind = Literal["principal", "legacy_binding", "actor"]
REVIEW_ACTION = "migration_review_identity_attested"


def audit_payload(row: Any) -> dict[str, Any]:
    expected = content_hash(
        {key: row[key] for key in ("subject_id", "action", "actor", "payload_json", "occurred_at")}
    )
    if row["state_hash"] != expected:
        raise ValueError("migration approval audit integrity check failed")
    value = strict_json_loads(row["payload_json"])
    if not isinstance(value, dict):
        raise ValueError("migration approval audit payload is invalid")
    return value


def review_kinds(connection: Any, approval: Any) -> set[str]:
    rows = connection.execute(
        "SELECT * FROM migration_audit_events WHERE subject_id=? AND action=? "
        "AND json_extract(payload_json,'$.approval_id')=?",
        (approval["subject_id"], REVIEW_ACTION, approval["approval_id"]),
    ).fetchall()
    result: set[str] = set()
    for row in rows:
        payload = audit_payload(row)
        if (
            set(payload)
            != {"approval_id", "proposal_id", "identity_binding", "policy_revision", "kind"}
            or any(
                payload[key] != approval[key]
                for key in ("approval_id", "proposal_id", "identity_binding", "policy_revision")
            )
            or payload["kind"] not in {"principal", "legacy_binding", "actor"}
        ):
            raise ValueError("migration review provenance binding mismatch")
        result.add(payload["kind"])
    return result


def record_review(connection: Any, approval: Any, kind: ReviewKind) -> None:
    if kind in review_kinds(connection, approval):
        return
    MigrationStore._append_audit(
        connection,
        approval["subject_id"],
        REVIEW_ACTION,
        approval["actor"],
        {
            key: approval[key]
            for key in ("approval_id", "proposal_id", "identity_binding", "policy_revision")
        }
        | {"kind": kind},
    )
