"""Lightweight temporal entity graph backed by SQLite.

The graph is a derived semantic structure. Every relation carries source
evidence and can be rebuilt without changing the subject identity ledger.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

from .validation import validate_event_ids


@dataclass(frozen=True)
class EntityRecord:
    entity_id: str
    subject_id: str
    entity_type: str
    canonical_key: str
    display_name: str
    aliases: tuple[str, ...]
    confidence: float
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class EntityRelationRecord:
    relation_id: str
    subject_id: str
    source_entity_id: str
    relation_type: str
    target_entity_id: str
    valid_from: str | None
    valid_until: str | None
    observed_at: str
    confidence: float
    status: str
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class EntityEvidenceLinkRecord:
    link_id: str
    subject_id: str
    entity_id: str
    source_type: str
    source_id: str
    role: str
    confidence: float
    created_at: str


class EntityStore:
    def __init__(self, database: Database):
        self.database = database

    def upsert(
        self,
        subject_id: str,
        entity_type: str,
        canonical_key: str,
        display_name: str,
        *,
        aliases: tuple[str, ...] = (),
        confidence: float = 0.5,
        seen_at: str | None = None,
    ) -> EntityRecord:
        self._validate_text(entity_type, canonical_key, display_name)
        self._validate_confidence(confidence)
        normalized_aliases = tuple(
            dict.fromkeys(alias.strip() for alias in aliases if alias.strip())
        )
        if len(normalized_aliases) > 64:
            raise ValueError("an entity cannot have more than 64 aliases")
        timestamp = seen_at or utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE subject_id = ? AND entity_type = ? "
                "AND canonical_key = ?",
                (subject_id, entity_type, canonical_key),
            ).fetchone()
            if row is None:
                entity_id = new_id("entity")
                connection.execute(
                    """INSERT INTO entities(
                       entity_id, subject_id, entity_type, canonical_key, display_name,
                       aliases_json, confidence, first_seen_at, last_seen_at, state_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entity_id,
                        subject_id,
                        entity_type,
                        canonical_key,
                        display_name,
                        canonical_json(list(normalized_aliases)),
                        confidence,
                        timestamp,
                        timestamp,
                        self._state_hash(
                            entity_id,
                            entity_type,
                            canonical_key,
                            display_name,
                            normalized_aliases,
                            confidence,
                            timestamp,
                            timestamp,
                        ),
                    ),
                )
            else:
                entity_id = str(row["entity_id"])
                previous_aliases = self._aliases(row["aliases_json"])
                merged_aliases = tuple(dict.fromkeys(previous_aliases + normalized_aliases))
                first_seen = min(str(row["first_seen_at"]), timestamp)
                last_seen = max(str(row["last_seen_at"]), timestamp)
                connection.execute(
                    """UPDATE entities SET display_name = ?, aliases_json = ?,
                       confidence = ?, first_seen_at = ?, last_seen_at = ?, state_hash = ?
                       WHERE entity_id = ?""",
                    (
                        display_name,
                        canonical_json(list(merged_aliases)),
                        max(float(row["confidence"]), confidence),
                        first_seen,
                        last_seen,
                        self._state_hash(
                            entity_id,
                            entity_type,
                            canonical_key,
                            display_name,
                            merged_aliases,
                            max(float(row["confidence"]), confidence),
                            first_seen,
                            last_seen,
                        ),
                        entity_id,
                    ),
                )
            return self._load_connection(connection, entity_id)

    def relation(
        self,
        subject_id: str,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
        *,
        source_event_ids: tuple[str, ...],
        confidence: float,
        observed_at: str | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        status: str = "active",
    ) -> EntityRelationRecord:
        self._validate_text(relation_type)
        self._validate_confidence(confidence)
        if status not in {"active", "superseded", "contradicted"}:
            raise ValueError("invalid entity relation status")
        observed = observed_at or utc_now()
        with self.database.transaction() as connection:
            self._entity_subject(connection, subject_id, source_entity_id)
            self._entity_subject(connection, subject_id, target_entity_id)
            events = validate_event_ids(connection, subject_id, source_event_ids)
            relation_id = new_id("relgraph")
            state_hash = content_hash(
                {
                    "source_entity_id": source_entity_id,
                    "relation_type": relation_type,
                    "target_entity_id": target_entity_id,
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                    "observed_at": observed,
                    "confidence": confidence,
                    "status": status,
                    "source_event_ids": list(events),
                }
            )
            connection.execute(
                """INSERT INTO entity_relations(
                   relation_id, subject_id, source_entity_id, relation_type,
                   target_entity_id, valid_from, valid_until, observed_at, confidence,
                   status, source_event_ids_json, state_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    relation_id,
                    subject_id,
                    source_entity_id,
                    relation_type,
                    target_entity_id,
                    valid_from,
                    valid_until,
                    observed,
                    confidence,
                    status,
                    canonical_json(list(events)),
                    state_hash,
                ),
            )
            return self._relation_from_row(
                connection.execute(
                    "SELECT * FROM entity_relations WHERE relation_id = ?", (relation_id,)
                ).fetchone()
            )

    def find(self, subject_id: str, entity_type: str, canonical_key: str) -> EntityRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE subject_id = ? AND entity_type = ? "
                "AND canonical_key = ?",
                (subject_id, entity_type, canonical_key),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"entity not found: {entity_type}:{canonical_key}")
        return self._entity_from_row(row)

    def relations(
        self,
        subject_id: str,
        *,
        entity_id: str | None = None,
        relation_type: str | None = None,
        as_of: str | None = None,
        limit: int = 100,
    ) -> list[EntityRelationRecord]:
        bounded = max(1, min(limit, 2_000))
        clauses = ["subject_id = ?"]
        params: list[Any] = [subject_id]
        if entity_id is not None:
            clauses.append("(source_entity_id = ? OR target_entity_id = ?)")
            params.extend((entity_id, entity_id))
        if relation_type is not None:
            clauses.append("relation_type = ?")
            params.append(relation_type)
        if as_of is not None:
            clauses.extend(
                [
                    "(valid_from IS NULL OR valid_from <= ?)",
                    "(valid_until IS NULL OR valid_until > ?)",
                ]
            )
            params.extend((as_of, as_of))
        params.append(bounded)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM entity_relations WHERE "
                + " AND ".join(clauses)
                + " ORDER BY observed_at DESC, relation_id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._relation_from_row(row) for row in rows]

    def link_evidence(
        self,
        subject_id: str,
        entity_id: str,
        source_type: str,
        source_id: str,
        *,
        role: str,
        confidence: float,
    ) -> EntityEvidenceLinkRecord:
        sources = {
            "memory": ("memories", "memory_id"),
            "belief": ("beliefs", "belief_id"),
            "event": ("events", "event_id"),
            "relationship": ("relationships", "relationship_id"),
            "memory_integration": ("memory_integrations", "integration_id"),
        }
        if source_type not in sources or not source_id.strip() or not role.strip():
            raise ValueError("invalid entity evidence link")
        self._validate_confidence(confidence)
        now = utc_now()
        link_id = new_id("entity-link")
        table, identifier = sources[source_type]
        payload = {
            "entity_id": entity_id,
            "source_type": source_type,
            "source_id": source_id,
            "role": role.strip(),
            "confidence": confidence,
        }
        with self.database.transaction() as connection:
            self._entity_subject(connection, subject_id, entity_id)
            if (
                connection.execute(
                    f'SELECT 1 FROM "{table}" WHERE "{identifier}" = ? AND subject_id = ?',
                    (source_id, subject_id),
                ).fetchone()
                is None
            ):
                raise ValueError("entity evidence source is unavailable")
            connection.execute(
                """INSERT INTO entity_evidence_links(
                    link_id, subject_id, entity_id, source_type, source_id, role,
                    confidence, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    link_id,
                    subject_id,
                    entity_id,
                    source_type,
                    source_id,
                    role.strip(),
                    confidence,
                    content_hash(payload),
                    now,
                ),
            )
        return EntityEvidenceLinkRecord(
            link_id, subject_id, entity_id, source_type, source_id, role.strip(), confidence, now
        )

    def evidence_links(self, subject_id: str, entity_id: str) -> list[EntityEvidenceLinkRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM entity_evidence_links WHERE subject_id = ? AND entity_id = ? "
                "ORDER BY created_at, link_id",
                (subject_id, entity_id),
            ).fetchall()
        records: list[EntityEvidenceLinkRecord] = []
        for row in rows:
            confidence = self._persisted_confidence(
                row["confidence"], f"entity evidence link {row['link_id']} confidence"
            )
            payload = {
                "entity_id": row["entity_id"],
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "role": row["role"],
                "confidence": confidence,
            }
            if content_hash(payload) != row["state_hash"]:
                raise IntegrityError("entity evidence link mismatch")
            records.append(
                EntityEvidenceLinkRecord(
                    row["link_id"],
                    row["subject_id"],
                    row["entity_id"],
                    row["source_type"],
                    row["source_id"],
                    row["role"],
                    confidence,
                    row["created_at"],
                )
            )
        return records

    def verify_integrity(self, subject_id: str) -> dict[str, int]:
        with self.database.read_transaction() as connection:
            entities = connection.execute(
                "SELECT * FROM entities WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            relations = connection.execute(
                "SELECT * FROM entity_relations WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            links = connection.execute(
                "SELECT entity_id FROM entity_evidence_links WHERE subject_id = ?",
                (subject_id,),
            ).fetchall()
        for row in entities:
            self._entity_from_row(row)
        for row in relations:
            self._relation_from_row(row)
        for row in links:
            self.evidence_links(subject_id, str(row["entity_id"]))
        return {"entities": len(entities), "relations": len(relations), "links": len(links)}

    def _load_connection(self, connection: Any, entity_id: str) -> EntityRecord:
        row = connection.execute(
            "SELECT * FROM entities WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"entity not found: {entity_id}")
        return self._entity_from_row(row)

    @staticmethod
    def _entity_subject(connection: Any, subject_id: str, entity_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM entities WHERE entity_id = ? AND subject_id = ?",
            (entity_id, subject_id),
        ).fetchone()
        if row is None:
            raise ValueError("entity is missing or belongs to another subject")

    @staticmethod
    def _entity_from_row(row: Any) -> EntityRecord:
        aliases = EntityStore._aliases(row["aliases_json"])
        confidence = EntityStore._persisted_confidence(
            row["confidence"], f"entity {row['entity_id']} confidence"
        )
        expected = EntityStore._state_hash(
            row["entity_id"],
            row["entity_type"],
            row["canonical_key"],
            row["display_name"],
            aliases,
            confidence,
            row["first_seen_at"],
            row["last_seen_at"],
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"entity state mismatch: {row['entity_id']}")
        return EntityRecord(
            row["entity_id"],
            row["subject_id"],
            row["entity_type"],
            row["canonical_key"],
            row["display_name"],
            aliases,
            confidence,
            row["first_seen_at"],
            row["last_seen_at"],
        )

    @staticmethod
    def _relation_from_row(row: Any) -> EntityRelationRecord:
        context = f"entity relation {row['relation_id']}"
        sources = EntityStore._persisted_json_strings(
            row["source_event_ids_json"], f"{context} source events", allow_empty=False
        )
        confidence = EntityStore._persisted_confidence(row["confidence"], f"{context} confidence")
        status = EntityStore._persisted_relation_status(row["status"], context)
        expected = content_hash(
            {
                "source_entity_id": row["source_entity_id"],
                "relation_type": row["relation_type"],
                "target_entity_id": row["target_entity_id"],
                "valid_from": row["valid_from"],
                "valid_until": row["valid_until"],
                "observed_at": row["observed_at"],
                "confidence": confidence,
                "status": status,
                "source_event_ids": list(sources),
            }
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"entity relation mismatch: {row['relation_id']}")
        return EntityRelationRecord(
            row["relation_id"],
            row["subject_id"],
            row["source_entity_id"],
            row["relation_type"],
            row["target_entity_id"],
            row["valid_from"],
            row["valid_until"],
            row["observed_at"],
            confidence,
            status,
            sources,
        )

    @staticmethod
    def _aliases(value: str) -> tuple[str, ...]:
        return EntityStore._persisted_json_strings(value, "entity aliases", allow_empty=True)

    @staticmethod
    def _persisted_json_strings(value: Any, context: str, *, allow_empty: bool) -> tuple[str, ...]:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"{context} JSON is invalid") from error
        if (
            not isinstance(parsed, list)
            or (not allow_empty and not parsed)
            or not all(isinstance(item, str) for item in parsed)
            or len(set(parsed)) != len(parsed)
        ):
            raise IntegrityError(f"{context} list is invalid")
        return tuple(parsed)

    @staticmethod
    def _persisted_confidence(value: Any, context: str) -> float:
        if isinstance(value, bool):
            raise IntegrityError(f"{context} is invalid")
        try:
            confidence = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"{context} is invalid") from error
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise IntegrityError(f"{context} is invalid")
        return confidence

    @staticmethod
    def _persisted_relation_status(value: Any, context: str) -> str:
        if not isinstance(value, str) or value not in {
            "active",
            "superseded",
            "contradicted",
        }:
            raise IntegrityError(f"{context} status is invalid")
        return value

    @staticmethod
    def _state_hash(
        entity_id: str,
        entity_type: str,
        canonical_key: str,
        display_name: str,
        aliases: tuple[str, ...],
        confidence: float,
        first_seen_at: str,
        last_seen_at: str,
    ) -> str:
        return content_hash(
            {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "canonical_key": canonical_key,
                "display_name": display_name,
                "aliases": list(aliases),
                "confidence": float(confidence),
                "first_seen_at": first_seen_at,
                "last_seen_at": last_seen_at,
            }
        )

    @staticmethod
    def _validate_text(*values: str) -> None:
        if any(not value.strip() for value in values):
            raise ValueError("entity fields cannot be blank")
        if any(len(value) > 512 for value in values):
            raise ValueError("entity fields are too long")

    @staticmethod
    def _validate_confidence(value: float) -> None:
        if not 0 <= value <= 1:
            raise ValueError("entity confidence must be between zero and one")
