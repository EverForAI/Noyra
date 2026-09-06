from __future__ import annotations

from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_finite_float,
    strict_json_loads,
    utc_now,
)

from .errors import CausalValidationError
from .types import CausalLinkRecord

ENTITY_TABLES: dict[str, tuple[str, str]] = {
    "event": ("events", "event_id"),
    "appraisal": ("appraisals", "appraisal_id"),
    "affect_transition": ("affect_transitions", "transition_id"),
    "goal": ("goals", "goal_id"),
    "memory": ("memories", "memory_id"),
    "belief": ("beliefs", "belief_id"),
    "relationship": ("relationships", "relationship_id"),
    "psychological_snapshot": ("psychological_snapshots", "snapshot_id"),
    "observation": ("observations", "observation_id"),
    "world_claim": ("world_claims", "claim_id"),
    "prediction": ("predictions", "prediction_id"),
}


class CausalStore:
    def __init__(self, database: Database):
        self.database = database

    def add(
        self,
        subject_id: str,
        source_type: str,
        source_id: str,
        relation: str,
        target_type: str,
        target_id: str,
        *,
        strength: float,
        metadata: dict[str, Any] | None = None,
    ) -> CausalLinkRecord:
        with self.database.transaction() as connection:
            return self._add_connection(
                connection,
                subject_id,
                source_type,
                source_id,
                relation,
                target_type,
                target_id,
                strength=strength,
                metadata=metadata or {},
            )

    def _add_connection(
        self,
        connection: Any,
        subject_id: str,
        source_type: str,
        source_id: str,
        relation: str,
        target_type: str,
        target_id: str,
        *,
        strength: float,
        metadata: dict[str, Any],
    ) -> CausalLinkRecord:
        if any(
            not value.strip()
            for value in (subject_id, source_type, source_id, relation, target_type, target_id)
        ):
            raise ValueError("causal link fields cannot be blank")
        if not -1 <= strength <= 1:
            raise ValueError("causal strength must be between negative one and one")
        if len(canonical_json(metadata).encode("utf-8")) > 16_384:
            raise ValueError("causal metadata is too large")
        self._validate_entity(connection, subject_id, source_type, source_id)
        self._validate_entity(connection, subject_id, target_type, target_id)
        link_id = new_id("cause")
        created_at = utc_now()
        record_hash = self._record_hash(
            source_type,
            source_id,
            relation,
            target_type,
            target_id,
            strength,
            metadata,
        )
        connection.execute(
            """INSERT INTO causal_links(
                link_id, subject_id, source_type, source_id, relation,
                target_type, target_id, strength, metadata_json, metadata_hash,
                record_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                link_id,
                subject_id,
                source_type,
                source_id,
                relation,
                target_type,
                target_id,
                strength,
                canonical_json(metadata),
                content_hash(metadata),
                record_hash,
                created_at,
            ),
        )
        return CausalLinkRecord(
            link_id,
            subject_id,
            source_type,
            source_id,
            relation,
            target_type,
            target_id,
            strength,
            metadata,
            created_at,
        )

    def links_from(
        self, subject_id: str, source_type: str, source_id: str
    ) -> list[CausalLinkRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM causal_links WHERE subject_id = ? AND source_type = ? "
                "AND source_id = ? ORDER BY created_at, link_id",
                (subject_id, source_type, source_id),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _validate_entity(
        connection: Any, subject_id: str, entity_type: str, entity_id: str
    ) -> None:
        mapping = ENTITY_TABLES.get(entity_type)
        if mapping is None:
            raise CausalValidationError(f"unsupported causal entity type: {entity_type}")
        table, key = mapping
        row = connection.execute(
            f"SELECT 1 FROM {table} WHERE subject_id = ? AND {key} = ?",
            (subject_id, entity_id),
        ).fetchone()
        if row is None:
            raise CausalValidationError("causal entity is missing or belongs to another subject")

    @staticmethod
    def _from_row(row: Any) -> CausalLinkRecord:
        link_id = row["link_id"]
        raw_metadata = row["metadata_json"]
        if not isinstance(raw_metadata, str):
            raise IntegrityError(f"causal metadata is invalid: {link_id}")
        try:
            metadata = strict_json_loads(raw_metadata)
            strength = strict_finite_float(row["strength"])
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"causal durable state is invalid: {link_id}") from error
        if not isinstance(metadata, dict) or content_hash(metadata) != row["metadata_hash"]:
            raise IntegrityError(f"causal metadata is invalid: {link_id}")
        if not -1.0 <= strength <= 1.0:
            raise IntegrityError(f"causal durable state is invalid: {link_id}")
        expected = CausalStore._record_hash(
            row["source_type"],
            row["source_id"],
            row["relation"],
            row["target_type"],
            row["target_id"],
            strength,
            metadata,
        )
        if expected != row["record_hash"]:
            raise IntegrityError(f"causal record is invalid: {link_id}")
        return CausalLinkRecord(
            link_id=link_id,
            subject_id=row["subject_id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            relation=row["relation"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            strength=strength,
            metadata=metadata,
            created_at=row["created_at"],
        )

    @staticmethod
    def _record_hash(
        source_type: str,
        source_id: str,
        relation: str,
        target_type: str,
        target_id: str,
        strength: float,
        metadata: dict[str, Any],
    ) -> str:
        return content_hash(
            {
                "source_type": source_type,
                "source_id": source_id,
                "relation": relation,
                "target_type": target_type,
                "target_id": target_id,
                "strength": float(strength),
                "metadata": metadata,
            }
        )
