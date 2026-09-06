from __future__ import annotations

from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_finite_float,
    strict_int,
    utc_now,
)

from .errors import MindStateConflictError
from .types import BeliefRecord
from .validation import validate_event_ids

BELIEF_STATUSES = frozenset({"active", "qualified", "retracted"})


class BeliefStore:
    def __init__(self, database: Database):
        self.database = database

    def create(
        self,
        subject_id: str,
        proposition: str,
        *,
        confidence: float,
        scope: str,
        supporting_event_ids: tuple[str, ...],
        counter_event_ids: tuple[str, ...] = (),
        reason: str = "belief formation",
    ) -> BeliefRecord:
        self._validate(proposition, confidence, scope, "active", reason)
        with self.database.transaction() as connection:
            return self._create_connection(
                connection,
                subject_id,
                proposition,
                confidence=confidence,
                scope=scope,
                supporting_event_ids=supporting_event_ids,
                counter_event_ids=counter_event_ids,
                reason=reason,
            )

    def _create_connection(
        self,
        connection: Any,
        subject_id: str,
        proposition: str,
        *,
        confidence: float,
        scope: str,
        supporting_event_ids: tuple[str, ...],
        counter_event_ids: tuple[str, ...] = (),
        reason: str = "belief formation",
    ) -> BeliefRecord:
        self._validate(proposition, confidence, scope, "active", reason)
        supporting = validate_event_ids(connection, subject_id, supporting_event_ids)
        counter = self._optional_events(connection, subject_id, counter_event_ids)
        belief_id = new_id("bel")
        now = utc_now()
        connection.execute(
            """INSERT INTO beliefs(
                belief_id, subject_id, proposition, proposition_hash, state_hash,
                confidence, scope, status, current_revision, created_at, reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)""",
            (
                belief_id,
                subject_id,
                proposition,
                content_hash(proposition),
                self._state_hash(proposition, confidence, "active", scope),
                confidence,
                scope,
                now,
                now,
            ),
        )
        self._insert_revision(
            connection,
            belief_id,
            1,
            proposition,
            confidence,
            "active",
            supporting,
            counter,
            reason,
            now,
        )
        return self._load_connection(connection, belief_id)

    def revise(
        self,
        belief_id: str,
        *,
        proposition: str,
        confidence: float,
        status: str,
        supporting_event_ids: tuple[str, ...],
        counter_event_ids: tuple[str, ...],
        reason: str,
        expected_revision: int | None = None,
    ) -> BeliefRecord:
        self._validate(proposition, confidence, "existing", status, reason)
        with self.database.transaction() as connection:
            return self._revise_connection(
                connection,
                belief_id,
                proposition=proposition,
                confidence=confidence,
                status=status,
                supporting_event_ids=supporting_event_ids,
                counter_event_ids=counter_event_ids,
                reason=reason,
                expected_revision=expected_revision,
            )

    def _revise_connection(
        self,
        connection: Any,
        belief_id: str,
        *,
        proposition: str,
        confidence: float,
        status: str,
        supporting_event_ids: tuple[str, ...],
        counter_event_ids: tuple[str, ...],
        reason: str,
        expected_revision: int | None = None,
    ) -> BeliefRecord:
        self._validate(proposition, confidence, "existing", status, reason)
        row = self._get_row(connection, belief_id)
        current_revision = int(row["current_revision"])
        if expected_revision is not None and current_revision != expected_revision:
            raise MindStateConflictError("belief revision changed before update")
        supporting = self._optional_events(connection, row["subject_id"], supporting_event_ids)
        counter = self._optional_events(connection, row["subject_id"], counter_event_ids)
        if not supporting and not counter:
            raise ValueError("belief revision requires supporting or counter evidence")
        revision = current_revision + 1
        now = utc_now()
        self._insert_revision(
            connection,
            belief_id,
            revision,
            proposition,
            confidence,
            status,
            supporting,
            counter,
            reason,
            now,
        )
        connection.execute(
            """UPDATE beliefs SET proposition = ?, proposition_hash = ?, state_hash = ?,
                confidence = ?, status = ?, current_revision = ?, reviewed_at = ?
                WHERE belief_id = ?""",
            (
                proposition,
                content_hash(proposition),
                self._state_hash(proposition, confidence, status, row["scope"]),
                confidence,
                status,
                revision,
                now,
                belief_id,
            ),
        )
        return self._load_connection(connection, belief_id)

    def get(self, belief_id: str) -> BeliefRecord:
        with self.database.connection() as connection:
            return self._load_connection(connection, belief_id)

    def list_beliefs(
        self, subject_id: str, *, include_retracted: bool = False
    ) -> list[BeliefRecord]:
        with self.database.connection() as connection:
            if include_retracted:
                rows = connection.execute(
                    "SELECT * FROM beliefs WHERE subject_id = ? ORDER BY reviewed_at DESC",
                    (subject_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM beliefs WHERE subject_id = ? AND status != 'retracted' "
                    "ORDER BY confidence DESC, reviewed_at DESC",
                    (subject_id,),
                ).fetchall()
        return [self._from_row(row) for row in rows]

    def revisions(self, belief_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            self._get_row(connection, belief_id)
            rows = connection.execute(
                "SELECT * FROM belief_revisions WHERE belief_id = ? ORDER BY revision_number",
                (belief_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _validate(
        proposition: str, confidence: float, scope: str, status: str, reason: str
    ) -> None:
        if not proposition.strip() or not scope.strip() or not reason.strip():
            raise ValueError("belief proposition, scope and reason are required")
        if len(proposition) > 20_000 or len(scope) > 1_000 or len(reason) > 10_000:
            raise ValueError("belief fields exceed their storage limits")
        if not 0 <= confidence <= 1:
            raise ValueError("belief confidence must be between zero and one")
        if status not in BELIEF_STATUSES:
            raise ValueError(f"invalid belief status: {status}")

    @staticmethod
    def _optional_events(
        connection: Any, subject_id: str, event_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not event_ids:
            return ()
        return validate_event_ids(connection, subject_id, event_ids)

    @staticmethod
    def _insert_revision(
        connection: Any,
        belief_id: str,
        revision: int,
        proposition: str,
        confidence: float,
        status: str,
        supporting: tuple[str, ...],
        counter: tuple[str, ...],
        reason: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO belief_revisions(
                revision_id, belief_id, revision_number, proposition, proposition_hash, state_hash,
                confidence, status,
                supporting_event_ids_json, counter_event_ids_json, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("brev"),
                belief_id,
                revision,
                proposition,
                content_hash(proposition),
                BeliefStore._revision_hash(proposition, confidence, status),
                confidence,
                status,
                canonical_json(list(supporting)),
                canonical_json(list(counter)),
                reason,
                created_at,
            ),
        )

    @staticmethod
    def _get_row(connection: Any, belief_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM beliefs WHERE belief_id = ?", (belief_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"belief not found: {belief_id}")
        return row

    @classmethod
    def _load_connection(cls, connection: Any, belief_id: str) -> BeliefRecord:
        return cls._from_row(cls._get_row(connection, belief_id))

    @staticmethod
    def _from_row(row: Any) -> BeliefRecord:
        belief_id = row["belief_id"]
        if content_hash(row["proposition"]) != row["proposition_hash"]:
            raise IntegrityError(f"belief proposition hash mismatch: {belief_id}")
        try:
            confidence = strict_finite_float(row["confidence"])
            current_revision = strict_int(row["current_revision"])
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"belief durable state is invalid: {belief_id}") from error
        if not 0.0 <= confidence <= 1.0 or current_revision < 1:
            raise IntegrityError(f"belief durable state is invalid: {belief_id}")
        status = row["status"]
        if not isinstance(status, str) or status not in BELIEF_STATUSES:
            raise IntegrityError(f"belief durable state is invalid: {belief_id}")
        expected_state = BeliefStore._state_hash(
            row["proposition"],
            confidence,
            status,
            row["scope"],
        )
        if expected_state != row["state_hash"]:
            raise IntegrityError(f"belief state hash mismatch: {belief_id}")
        return BeliefRecord(
            belief_id=belief_id,
            subject_id=row["subject_id"],
            proposition=row["proposition"],
            confidence=confidence,
            scope=row["scope"],
            status=status,
            current_revision=current_revision,
            created_at=row["created_at"],
            reviewed_at=row["reviewed_at"],
        )

    @staticmethod
    def _state_hash(proposition: str, confidence: float, status: str, scope: str) -> str:
        return content_hash(
            {
                "proposition": proposition,
                "confidence": float(confidence),
                "status": status,
                "scope": scope,
            }
        )

    @staticmethod
    def _revision_hash(proposition: str, confidence: float, status: str) -> str:
        return content_hash(
            {
                "proposition": proposition,
                "confidence": float(confidence),
                "status": status,
            }
        )
