from __future__ import annotations

import builtins
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_finite_float,
    strict_int,
    strict_json_loads,
    utc_now,
)

from .errors import MindStateConflictError
from .types import RelationshipRecord
from .validation import validate_event_ids


class RelationshipStore:
    def __init__(self, database: Database):
        self.database = database

    def ensure(
        self,
        subject_id: str,
        entity_type: str,
        entity_key: str,
        display_name: str,
        *,
        source_event_ids: tuple[str, ...],
        reason: str = "relationship formation",
    ) -> RelationshipRecord:
        if any(not value.strip() for value in (entity_type, entity_key, display_name, reason)):
            raise ValueError("relationship identity and reason cannot be blank")
        with self.database.transaction() as connection:
            return self._ensure_connection(
                connection,
                subject_id,
                entity_type,
                entity_key,
                display_name,
                source_event_ids=source_event_ids,
                reason=reason,
            )

    def _ensure_connection(
        self,
        connection: Any,
        subject_id: str,
        entity_type: str,
        entity_key: str,
        display_name: str,
        *,
        source_event_ids: tuple[str, ...],
        reason: str = "relationship formation",
    ) -> RelationshipRecord:
        if any(not value.strip() for value in (entity_type, entity_key, display_name, reason)):
            raise ValueError("relationship identity and reason cannot be blank")
        row = connection.execute(
            "SELECT * FROM relationships WHERE subject_id = ? AND entity_type = ? "
            "AND entity_key = ?",
            (subject_id, entity_type, entity_key),
        ).fetchone()
        if row is not None:
            if row["display_name"] != display_name:
                raise MindStateConflictError(
                    "relationship key already has a different display name"
                )
            return self._from_row(row)
        sources = validate_event_ids(connection, subject_id, source_event_ids)
        relationship_id = new_id("rel")
        now = utc_now()
        boundaries: dict[str, Any] = {}
        boundaries_hash = content_hash(boundaries)
        state_hash = self._state_hash(0, 0, 0, 0, boundaries)
        connection.execute(
            """INSERT INTO relationships(
                relationship_id, subject_id, entity_type, entity_key, display_name,
                trust, affinity, conflict, familiarity, boundaries_json, boundaries_hash,
                state_hash,
                current_revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?, 1, ?, ?)""",
            (
                relationship_id,
                subject_id,
                entity_type,
                entity_key,
                display_name,
                canonical_json(boundaries),
                boundaries_hash,
                state_hash,
                now,
                now,
            ),
        )
        self._insert_revision(
            connection,
            relationship_id,
            1,
            trust=0,
            affinity=0,
            conflict=0,
            familiarity=0,
            boundaries=boundaries,
            reason=reason,
            sources=sources,
            created_at=now,
        )
        return self._load_connection(connection, relationship_id)

    def revise(
        self,
        relationship_id: str,
        *,
        trust: float,
        affinity: float,
        conflict: float,
        familiarity: float,
        boundaries: dict[str, Any],
        reason: str,
        source_event_ids: tuple[str, ...],
        expected_revision: int | None = None,
    ) -> RelationshipRecord:
        self._validate_dimensions(trust, affinity, conflict, familiarity)
        if not reason.strip():
            raise ValueError("relationship revision reason is required")
        if len(canonical_json(boundaries).encode("utf-8")) > 65_536:
            raise ValueError("relationship boundaries are too large")
        with self.database.transaction() as connection:
            return self._revise_connection(
                connection,
                relationship_id,
                trust=trust,
                affinity=affinity,
                conflict=conflict,
                familiarity=familiarity,
                boundaries=boundaries,
                reason=reason,
                source_event_ids=source_event_ids,
                expected_revision=expected_revision,
            )

    def _revise_connection(
        self,
        connection: Any,
        relationship_id: str,
        *,
        trust: float,
        affinity: float,
        conflict: float,
        familiarity: float,
        boundaries: dict[str, Any],
        reason: str,
        source_event_ids: tuple[str, ...],
        expected_revision: int | None = None,
    ) -> RelationshipRecord:
        self._validate_dimensions(trust, affinity, conflict, familiarity)
        if not reason.strip():
            raise ValueError("relationship revision reason is required")
        if len(canonical_json(boundaries).encode("utf-8")) > 65_536:
            raise ValueError("relationship boundaries are too large")
        row = self._get_row(connection, relationship_id)
        current_revision = int(row["current_revision"])
        if expected_revision is not None and expected_revision != current_revision:
            raise MindStateConflictError("relationship revision changed before update")
        sources = validate_event_ids(connection, row["subject_id"], source_event_ids)
        revision = current_revision + 1
        now = utc_now()
        boundaries_hash = content_hash(boundaries)
        state_hash = self._state_hash(trust, affinity, conflict, familiarity, boundaries)
        self._insert_revision(
            connection,
            relationship_id,
            revision,
            trust=trust,
            affinity=affinity,
            conflict=conflict,
            familiarity=familiarity,
            boundaries=boundaries,
            reason=reason,
            sources=sources,
            created_at=now,
        )
        connection.execute(
            """UPDATE relationships SET
                trust = ?, affinity = ?, conflict = ?, familiarity = ?,
                boundaries_json = ?, boundaries_hash = ?, state_hash = ?,
                current_revision = ?,
                updated_at = ? WHERE relationship_id = ?""",
            (
                trust,
                affinity,
                conflict,
                familiarity,
                canonical_json(boundaries),
                boundaries_hash,
                state_hash,
                revision,
                now,
                relationship_id,
            ),
        )
        return self._load_connection(connection, relationship_id)

    def list(
        self, subject_id: str, *, entity_type: str | None = None
    ) -> builtins.list[RelationshipRecord]:
        with self.database.connection() as connection:
            if entity_type is None:
                rows = connection.execute(
                    "SELECT * FROM relationships WHERE subject_id = ? "
                    "ORDER BY familiarity DESC, affinity DESC, updated_at DESC",
                    (subject_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM relationships WHERE subject_id = ? AND entity_type = ? "
                    "ORDER BY familiarity DESC, affinity DESC, updated_at DESC",
                    (subject_id, entity_type),
                ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, relationship_id: str) -> RelationshipRecord:
        with self.database.connection() as connection:
            return self._load_connection(connection, relationship_id)

    def find(self, subject_id: str, entity_type: str, entity_key: str) -> RelationshipRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM relationships WHERE subject_id = ? AND entity_type = ? "
                "AND entity_key = ?",
                (subject_id, entity_type, entity_key),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"relationship not found: {entity_type}:{entity_key}")
        return self._from_row(row)

    def revisions(self, relationship_id: str) -> builtins.list[dict[str, Any]]:
        with self.database.connection() as connection:
            self._get_row(connection, relationship_id)
            rows = connection.execute(
                "SELECT * FROM relationship_revisions WHERE relationship_id = ? "
                "ORDER BY revision_number",
                (relationship_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _validate_dimensions(
        trust: float, affinity: float, conflict: float, familiarity: float
    ) -> None:
        if not -1 <= trust <= 1 or not -1 <= affinity <= 1:
            raise ValueError("relationship trust and affinity must be negative one to one")
        if not 0 <= conflict <= 1 or not 0 <= familiarity <= 1:
            raise ValueError("relationship conflict and familiarity must be zero to one")

    @staticmethod
    def _insert_revision(
        connection: Any,
        relationship_id: str,
        revision: int,
        *,
        trust: float,
        affinity: float,
        conflict: float,
        familiarity: float,
        boundaries: dict[str, Any],
        reason: str,
        sources: tuple[str, ...],
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO relationship_revisions(
                revision_id, relationship_id, revision_number, trust, affinity,
                conflict, familiarity, boundaries_json, boundaries_hash, state_hash,
                reason, source_event_ids_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("rrev"),
                relationship_id,
                revision,
                trust,
                affinity,
                conflict,
                familiarity,
                canonical_json(boundaries),
                content_hash(boundaries),
                RelationshipStore._state_hash(trust, affinity, conflict, familiarity, boundaries),
                reason,
                canonical_json(list(sources)),
                created_at,
            ),
        )

    @staticmethod
    def _get_row(connection: Any, relationship_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM relationships WHERE relationship_id = ?", (relationship_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"relationship not found: {relationship_id}")
        return row

    @classmethod
    def _load_connection(cls, connection: Any, relationship_id: str) -> RelationshipRecord:
        return cls._from_row(cls._get_row(connection, relationship_id))

    @staticmethod
    def _from_row(row: Any) -> RelationshipRecord:
        relationship_id = row["relationship_id"]
        raw_boundaries = row["boundaries_json"]
        if not isinstance(raw_boundaries, str):
            raise IntegrityError(f"relationship durable state is invalid: {relationship_id}")
        try:
            boundaries = strict_json_loads(raw_boundaries)
            trust = strict_finite_float(row["trust"])
            affinity = strict_finite_float(row["affinity"])
            conflict = strict_finite_float(row["conflict"])
            familiarity = strict_finite_float(row["familiarity"])
            current_revision = strict_int(row["current_revision"])
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(
                f"relationship durable state is invalid: {relationship_id}"
            ) from error
        if not isinstance(boundaries, dict) or content_hash(boundaries) != row["boundaries_hash"]:
            raise IntegrityError(f"relationship boundary integrity failure: {relationship_id}")
        if (
            not -1.0 <= trust <= 1.0
            or not -1.0 <= affinity <= 1.0
            or not 0.0 <= conflict <= 1.0
            or not 0.0 <= familiarity <= 1.0
            or current_revision < 1
        ):
            raise IntegrityError(f"relationship durable state is invalid: {relationship_id}")
        expected_state = RelationshipStore._state_hash(
            trust,
            affinity,
            conflict,
            familiarity,
            boundaries,
        )
        if expected_state != row["state_hash"]:
            raise IntegrityError(f"relationship state integrity failure: {relationship_id}")
        return RelationshipRecord(
            relationship_id=relationship_id,
            subject_id=row["subject_id"],
            entity_type=row["entity_type"],
            entity_key=row["entity_key"],
            display_name=row["display_name"],
            trust=trust,
            affinity=affinity,
            conflict=conflict,
            familiarity=familiarity,
            boundaries=boundaries,
            current_revision=current_revision,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _state_hash(
        trust: float,
        affinity: float,
        conflict: float,
        familiarity: float,
        boundaries: dict[str, Any],
    ) -> str:
        return content_hash(
            {
                "trust": float(trust),
                "affinity": float(affinity),
                "conflict": float(conflict),
                "familiarity": float(familiarity),
                "boundaries": boundaries,
            }
        )
