"""Immutable software identity bindings; legacy observations are not birth proofs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .errors import IntegrityError
from .types import canonical_json, content_hash, strict_json_loads, utc_now

RELATIONSHIP_IDENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS relationship_identity_bindings (
 relationship_id TEXT PRIMARY KEY REFERENCES relationships(relationship_id),
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 entity_type TEXT NOT NULL, entity_key TEXT NOT NULL, relationship_created_at TEXT NOT NULL,
 origin TEXT NOT NULL CHECK(origin IN ('created','legacy_observed')),
 source_event_ids_json TEXT NOT NULL, review_reasons_json TEXT NOT NULL,
 review_social_hashes_json TEXT NOT NULL,
 recorded_at TEXT NOT NULL, state_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relationship_identity_subject
 ON relationship_identity_bindings(subject_id,relationship_id);
CREATE TRIGGER IF NOT EXISTS prevent_relationship_identity_update
BEFORE UPDATE OF relationship_id,subject_id,entity_type,entity_key,created_at ON relationships
WHEN NEW.relationship_id IS NOT OLD.relationship_id OR NEW.subject_id IS NOT OLD.subject_id
 OR NEW.entity_type IS NOT OLD.entity_type OR NEW.entity_key IS NOT OLD.entity_key
 OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'relationship identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_relationship_identity_replace
BEFORE INSERT ON relationships
WHEN EXISTS(SELECT 1 FROM relationships r WHERE r.relationship_id=NEW.relationship_id
 OR (r.subject_id=NEW.subject_id AND r.entity_type=NEW.entity_type AND r.entity_key=NEW.entity_key))
BEGIN SELECT RAISE(ABORT,'relationship identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_relationship_binding_insert
BEFORE INSERT ON relationship_identity_bindings
WHEN EXISTS(SELECT 1 FROM relationship_identity_bindings b
 WHERE b.relationship_id=NEW.relationship_id)
 OR NOT EXISTS(SELECT 1 FROM relationships r WHERE r.relationship_id=NEW.relationship_id
 AND r.subject_id=NEW.subject_id AND r.entity_type=NEW.entity_type AND r.entity_key=NEW.entity_key)
BEGIN SELECT RAISE(ABORT,'relationship binding ownership mismatch'); END;
CREATE TRIGGER IF NOT EXISTS prevent_relationship_binding_update
BEFORE UPDATE ON relationship_identity_bindings
BEGIN SELECT RAISE(ABORT,'relationship binding is immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_relationship_binding_delete
BEFORE DELETE ON relationship_identity_bindings
BEGIN SELECT RAISE(ABORT,'relationship binding is immutable'); END;
"""

_REVIEW_REASONS = frozenset(
    {"source_reference_invalid", "social_snapshot_invalid", "social_identity_conflict"}
)


def _string_list(value: Any) -> list[str]:
    parsed = strict_json_loads(value)
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item for item in parsed
    ):
        raise ValueError("invalid identity evidence list")
    return parsed


def insert_binding(
    connection: Any,
    row: Any,
    *,
    origin: str,
    sources: tuple[str, ...],
    reasons: tuple[str, ...] = (),
    social_hashes: dict[str, str] | None = None,
) -> None:
    if origin not in {"created", "legacy_observed"} or set(reasons) - _REVIEW_REASONS:
        raise ValueError("relationship binding provenance is invalid")
    if len(sources) > 64 or any(not isinstance(item, str) or not item for item in sources):
        raise ValueError("relationship binding sources are invalid")
    if origin == "created" and (not sources or reasons or social_hashes):
        raise ValueError("new relationship binding cannot inherit legacy uncertainty")
    value = {
        "relationship_id": row["relationship_id"],
        "subject_id": row["subject_id"],
        "entity_type": row["entity_type"],
        "entity_key": row["entity_key"],
        "relationship_created_at": row["created_at"],
        "origin": origin,
        "source_event_ids_json": canonical_json(list(sources)),
        "review_reasons_json": canonical_json(sorted(set(reasons))),
        "review_social_hashes_json": canonical_json(social_hashes or {}),
        "recorded_at": utc_now(),
    }
    connection.execute(
        "INSERT INTO relationship_identity_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (*value.values(), content_hash(value)),
    )


def verify_binding(
    connection: Any, row: Any, *, subject_id: str | None = None
) -> tuple[str, tuple[str, ...]]:
    """Verify the stored binding as well as fields supplied by a reader's snapshot."""
    binding = connection.execute(
        "SELECT * FROM relationship_identity_bindings WHERE relationship_id=?",
        (row["relationship_id"],),
    ).fetchone()
    if binding is None:
        raise IntegrityError("relationship identity binding is missing")
    value = dict(binding)
    digest = value.pop("state_hash")
    if any(not isinstance(item, str) or not item for item in value.values()):
        raise IntegrityError("relationship identity evidence has invalid fields")
    try:
        for key in ("recorded_at", "relationship_created_at"):
            if datetime.fromisoformat(value[key]).tzinfo is None:
                raise ValueError("unqualified binding timestamp")
    except ValueError as error:
        raise IntegrityError("relationship identity timestamp is invalid") from error
    try:
        sources = _string_list(value["source_event_ids_json"])
        reasons = _string_list(value["review_reasons_json"])
        social_hashes = strict_json_loads(value["review_social_hashes_json"])
        if not isinstance(social_hashes, dict) or any(
            not isinstance(key, str)
            or not key
            or not isinstance(digest, str)
            or len(digest) != 64
            or set(digest) - set("0123456789abcdef")
            for key, digest in social_hashes.items()
        ):
            raise ValueError("invalid social conflict witnesses")
    except (TypeError, ValueError) as error:
        raise IntegrityError("relationship identity evidence is invalid") from error
    if (
        content_hash(value) != digest
        or value["origin"] not in {"created", "legacy_observed"}
        or set(reasons) - _REVIEW_REASONS
        or len(sources) > 64
        or (value["origin"] == "created" and (not sources or reasons or social_hashes))
        or (social_hashes and not reasons)
        or (subject_id is not None and value["subject_id"] != subject_id)
    ):
        raise IntegrityError("relationship identity evidence mismatch")
    current = connection.execute(
        "SELECT subject_id,entity_type,entity_key,created_at FROM relationships "
        "WHERE relationship_id=?",
        (row["relationship_id"],),
    ).fetchone()
    if current is None or any(
        current[key] != value[key] for key in ("subject_id", "entity_type", "entity_key")
    ):
        raise IntegrityError("relationship identity binding mismatch")
    if current["created_at"] != value["relationship_created_at"]:
        raise IntegrityError("relationship formation time mismatch")
    keys = set(row.keys())
    if any(
        key in keys and row[key] != value[key]
        for key in ("subject_id", "entity_type", "entity_key")
    ):
        raise IntegrityError("relationship reader identity mismatch")
    return value["origin"], tuple(reasons)


def context_rows(connection: Any, rows: Any, subject_id: str) -> list[dict[str, Any]]:
    """Shared guard for bounded direct-SQL cognition readers; no trust promotion."""
    result = []
    for row in rows:
        origin, reasons = verify_binding(connection, row, subject_id=subject_id)
        if reasons:
            continue
        result.append(dict(row) | {"identity_origin": origin, "identity_status": "consistent"})
    return result


def backfill_legacy_bindings(connection: Any) -> None:
    """Called only inside the schema migration transaction, never as startup repair."""
    for row in connection.execute("SELECT * FROM relationships ORDER BY relationship_id"):
        # Existing same-schema bindings are never overwritten or re-attested.
        existing = connection.execute(
            "SELECT 1 FROM relationship_identity_bindings WHERE relationship_id=?",
            (row["relationship_id"],),
        ).fetchone()
        if existing:
            verify_binding(connection, row)
            continue
        reasons: set[str] = set()
        social_hashes: dict[str, str] = {}
        first = connection.execute(
            "SELECT source_event_ids_json FROM relationship_revisions "
            "WHERE relationship_id=? AND revision_number=1",
            (row["relationship_id"],),
        ).fetchone()
        try:
            sources = _string_list(first[0]) if first is not None else []
            if not sources or len(sources) > 64:
                raise ValueError("missing formation sources")
            for source in sources:
                if not connection.execute(
                    "SELECT 1 FROM events WHERE subject_id=? AND event_id=?",
                    (row["subject_id"], source),
                ).fetchone():
                    reasons.add("source_reference_invalid")
        except (TypeError, ValueError):
            sources = []
            reasons.add("source_reference_invalid")
        # Generic/mixed source events do NOT prove wrong identity. Only a
        # later immutable social snapshot can give an explicit conflicting key.
        for run in connection.execute(
            "SELECT * FROM relationship_social_runs WHERE relationship_id=?",
            (row["relationship_id"],),
        ):
            try:
                proposal = strict_json_loads(run["proposal_json"])
                evidence = _string_list(run["evidence_event_ids_json"])
                expected = content_hash(
                    {
                        key: run[key]
                        for key in (
                            "relationship_id",
                            "model_call_id",
                            "interaction_id",
                            "disposition",
                            "channel",
                            "counterparty",
                            "topic",
                            "rationale",
                            "proposal_hash",
                            "created_at",
                        )
                    }
                    | {"evidence_event_ids": evidence}
                )
                if (
                    not isinstance(proposal, dict)
                    or content_hash(proposal) != run["proposal_hash"]
                    or expected != run["state_hash"]
                ):
                    raise ValueError("invalid social snapshot")
            except (TypeError, ValueError):
                reasons.add("social_snapshot_invalid")
                continue
            social_hashes[run["social_id"]] = run["state_hash"]
            if run["subject_id"] != row["subject_id"] or run["counterparty"] != row["entity_key"]:
                reasons.add("social_identity_conflict")
            entity = connection.execute(
                "SELECT entity_type FROM participant_entities WHERE subject_id=? AND entity_id=?",
                (run["subject_id"], run["counterparty"]),
            ).fetchone()
            if entity is not None:
                kind = (
                    "human"
                    if entity[0] == "human"
                    else "agent"
                    if entity[0] in {"agent", "noyra"}
                    else "entity"
                )
                if kind != row["entity_type"]:
                    reasons.add("social_identity_conflict")
        insert_binding(
            connection,
            row,
            origin="legacy_observed",
            sources=tuple(sources),
            reasons=tuple(sorted(reasons)),
            social_hashes=social_hashes if reasons else {},
        )
