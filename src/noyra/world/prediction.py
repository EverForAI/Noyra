from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_bool,
    strict_finite_float,
    strict_json_loads,
    utc_now,
)
from noyra.mind.causal import CausalStore

from .errors import WorldStateConflictError
from .types import PredictionProposal, PredictionRecord


class PredictionStore:
    def __init__(self, database: Database, *, clock: Callable[[], str] = utc_now):
        self.database = database
        self.causal = CausalStore(database)
        self._clock = clock

    def create(
        self,
        subject_id: str,
        proposal: PredictionProposal,
        *,
        evidence_observation_ids: tuple[str, ...],
        rationale: str = "forecast from current evidence",
        idempotency_key: str | None = None,
    ) -> PredictionRecord:
        target = self._parse_time(proposal.target_at)
        target_text = target.astimezone(UTC).isoformat(timespec="milliseconds")
        now_text = self._clock()
        if target <= self._parse_time(now_text):
            raise ValueError("prediction target must be in the future")
        self._validate_rationale(rationale)
        with self.database.transaction() as connection:
            evidence = self._validate_evidence(connection, subject_id, evidence_observation_ids)
            resolved_key = idempotency_key or content_hash(
                {
                    "statement": proposal.statement,
                    "target_at": target_text,
                    "evidence": list(evidence),
                }
            )
            if not resolved_key.strip() or len(resolved_key) > 256:
                raise ValueError("prediction idempotency key is invalid")
            existing = connection.execute(
                "SELECT * FROM predictions WHERE subject_id = ? AND idempotency_key = ?",
                (subject_id, resolved_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["statement_hash"] != content_hash(proposal.statement)
                    or float(existing["probability"]) != proposal.probability
                    or existing["target_at"] != target_text
                    or existing["resolution_criteria"] != proposal.resolution_criteria
                ):
                    raise WorldStateConflictError(
                        "prediction idempotency key identifies different content"
                    )
                return self._from_row(existing)
            prediction_id = new_id("pred")
            state_hash = self._state_hash(
                proposal.statement,
                proposal.probability,
                target_text,
                proposal.resolution_criteria,
                "open",
                None,
                None,
            )
            connection.execute(
                """INSERT INTO predictions(
                    prediction_id, subject_id, idempotency_key, statement, statement_hash,
                    probability,
                    target_at, resolution_criteria, status, outcome, brier_score,
                    state_hash, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL, ?, ?, NULL)""",
                (
                    prediction_id,
                    subject_id,
                    resolved_key,
                    proposal.statement,
                    content_hash(proposal.statement),
                    proposal.probability,
                    target_text,
                    proposal.resolution_criteria,
                    state_hash,
                    now_text,
                ),
            )
            self._insert_review(
                connection,
                prediction_id,
                None,
                evidence,
                rationale,
                "open",
                None,
                now_text,
            )
            self._link_evidence(
                connection,
                subject_id,
                evidence,
                prediction_id,
                proposal.probability,
            )
            return self._load_connection(connection, prediction_id)

    def resolve(
        self,
        prediction_id: str,
        *,
        outcome: bool,
        evidence_observation_ids: tuple[str, ...],
        rationale: str,
    ) -> PredictionRecord:
        self._validate_rationale(rationale)
        with self.database.transaction() as connection:
            return self._resolve_connection(
                connection,
                prediction_id,
                outcome=outcome,
                evidence_observation_ids=evidence_observation_ids,
                rationale=rationale,
            )

    def _resolve_connection(
        self,
        connection: Any,
        prediction_id: str,
        *,
        outcome: bool,
        evidence_observation_ids: tuple[str, ...],
        rationale: str,
    ) -> PredictionRecord:
        self._validate_rationale(rationale)
        now_text = self._clock()
        row = self._get_row(connection, prediction_id)
        if row["status"] != "open":
            raise WorldStateConflictError("prediction is already finalized")
        if not outcome and self._parse_time(now_text) < self._parse_time(row["target_at"]):
            raise WorldStateConflictError(
                "a false outcome cannot be resolved before its target time"
            )
        evidence = self._validate_evidence(
            connection, row["subject_id"], evidence_observation_ids, require_analyzed=True
        )
        numeric_outcome = int(outcome)
        brier = (float(row["probability"]) - numeric_outcome) ** 2
        state_hash = self._state_hash(
            row["statement"],
            float(row["probability"]),
            row["target_at"],
            row["resolution_criteria"],
            "resolved",
            outcome,
            brier,
        )
        connection.execute(
            """UPDATE predictions SET status = 'resolved', outcome = ?, brier_score = ?,
                state_hash = ?, resolved_at = ? WHERE prediction_id = ?""",
            (numeric_outcome, brier, state_hash, now_text, prediction_id),
        )
        self._insert_review(
            connection,
            prediction_id,
            outcome,
            evidence,
            rationale,
            "resolved",
            brier,
            now_text,
        )
        self._link_evidence(connection, row["subject_id"], evidence, prediction_id, 1)
        return self._load_connection(connection, prediction_id)

    def cancel(
        self,
        prediction_id: str,
        *,
        evidence_observation_ids: tuple[str, ...],
        rationale: str,
    ) -> PredictionRecord:
        self._validate_rationale(rationale)
        now_text = self._clock()
        with self.database.transaction() as connection:
            row = self._get_row(connection, prediction_id)
            if row["status"] != "open":
                raise WorldStateConflictError("prediction is already finalized")
            evidence = self._validate_evidence(
                connection, row["subject_id"], evidence_observation_ids
            )
            state_hash = self._state_hash(
                row["statement"],
                float(row["probability"]),
                row["target_at"],
                row["resolution_criteria"],
                "cancelled",
                None,
                None,
            )
            connection.execute(
                """UPDATE predictions SET status = 'cancelled', state_hash = ?,
                    resolved_at = ? WHERE prediction_id = ?""",
                (state_hash, now_text, prediction_id),
            )
            self._insert_review(
                connection,
                prediction_id,
                None,
                evidence,
                rationale,
                "cancelled",
                None,
                now_text,
            )
            return self._load_connection(connection, prediction_id)

    def get(self, prediction_id: str) -> PredictionRecord:
        with self.database.connection() as connection:
            return self._load_connection(connection, prediction_id)

    def due(self, subject_id: str, *, at: str | None = None) -> list[PredictionRecord]:
        timestamp = (
            self._parse_time(at or self._clock()).astimezone(UTC).isoformat(timespec="milliseconds")
        )
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM predictions WHERE subject_id = ? AND status = 'open' "
                "AND target_at <= ? ORDER BY target_at, prediction_id",
                (subject_id, timestamp),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def reviews(self, prediction_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            self._get_row(connection, prediction_id)
            rows = connection.execute(
                "SELECT * FROM prediction_reviews WHERE prediction_id = ? ORDER BY created_at",
                (prediction_id,),
            ).fetchall()
        for row in rows:
            evidence = self._review_evidence_ids(
                row["evidence_observation_ids_json"], str(row["review_id"])
            )
            expected = self._review_hash(
                bool(row["outcome"]) if row["outcome"] is not None else None,
                evidence,
                row["rationale"],
                row["resulting_status"],
                float(row["brier_score"]) if row["brier_score"] is not None else None,
            )
            if expected != row["state_hash"]:
                raise IntegrityError(f"prediction review hash mismatch: {row['review_id']}")
        return [dict(row) for row in rows]

    @staticmethod
    def _review_evidence_ids(value: Any, review_id: str) -> tuple[str, ...]:
        if not isinstance(value, str):
            raise IntegrityError(f"prediction review evidence is invalid: {review_id}")
        try:
            decoded = strict_json_loads(value)
        except (TypeError, ValueError, UnicodeError) as error:
            raise IntegrityError(f"prediction review evidence is invalid: {review_id}") from error
        if (
            not isinstance(decoded, list)
            or not decoded
            or len(decoded) > 64
            or not all(isinstance(item, str) and item for item in decoded)
            or len(set(decoded)) != len(decoded)
        ):
            raise IntegrityError(f"prediction review evidence is invalid: {review_id}")
        return tuple(decoded)

    @staticmethod
    def _validate_evidence(
        connection: Any,
        subject_id: str,
        observation_ids: tuple[str, ...],
        *,
        require_analyzed: bool = False,
    ) -> tuple[str, ...]:
        ids = tuple(dict.fromkeys(observation_ids))
        if not ids or len(ids) > 64:
            raise ValueError("prediction requires 1-64 evidence observations")
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT observation_id, status FROM observations WHERE subject_id = ? "
            f"AND observation_id IN ({placeholders})",
            (subject_id, *ids),
        ).fetchall()
        if {row["observation_id"] for row in rows} != set(ids):
            raise WorldStateConflictError(
                "prediction evidence is missing or belongs to another subject"
            )
        if require_analyzed and any(row["status"] != "analyzed" for row in rows):
            raise WorldStateConflictError("prediction resolution requires analyzed evidence")
        return ids

    def _link_evidence(
        self,
        connection: Any,
        subject_id: str,
        evidence: tuple[str, ...],
        prediction_id: str,
        strength: float,
    ) -> None:
        for observation_id in evidence:
            self.causal._add_connection(
                connection,
                subject_id,
                "observation",
                observation_id,
                "informs_prediction",
                "prediction",
                prediction_id,
                strength=strength,
                metadata={},
            )

    @classmethod
    def _insert_review(
        cls,
        connection: Any,
        prediction_id: str,
        outcome: bool | None,
        evidence: tuple[str, ...],
        rationale: str,
        status: str,
        brier: float | None,
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO prediction_reviews(
                review_id, prediction_id, outcome, evidence_observation_ids_json,
                rationale, resulting_status, brier_score, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("prev"),
                prediction_id,
                int(outcome) if outcome is not None else None,
                canonical_json(list(evidence)),
                rationale,
                status,
                brier,
                cls._review_hash(outcome, evidence, rationale, status, brier),
                created_at,
            ),
        )

    @staticmethod
    def _state_hash(
        statement: str,
        probability: float,
        target_at: str,
        criteria: str,
        status: str,
        outcome: bool | None,
        brier: float | None,
    ) -> str:
        return content_hash(
            {
                "statement": statement,
                "probability": float(probability),
                "target_at": target_at,
                "resolution_criteria": criteria,
                "status": status,
                "outcome": outcome,
                "brier_score": float(brier) if brier is not None else None,
            }
        )

    @staticmethod
    def _review_hash(
        outcome: bool | None,
        evidence: tuple[str, ...],
        rationale: str,
        status: str,
        brier: float | None,
    ) -> str:
        return content_hash(
            {
                "outcome": outcome,
                "evidence": list(evidence),
                "rationale": rationale,
                "status": status,
                "brier_score": float(brier) if brier is not None else None,
            }
        )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("prediction time must be ISO-8601") from error
        if parsed.tzinfo is None:
            raise ValueError("prediction time must include a timezone")
        return parsed

    @staticmethod
    def _validate_rationale(rationale: str) -> None:
        if not rationale.strip() or len(rationale) > 10_000:
            raise ValueError("prediction rationale is invalid")

    @staticmethod
    def _get_row(connection: Any, prediction_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM predictions WHERE prediction_id = ?", (prediction_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"prediction not found: {prediction_id}")
        return row

    @classmethod
    def _load_connection(cls, connection: Any, prediction_id: str) -> PredictionRecord:
        return cls._from_row(cls._get_row(connection, prediction_id))

    @classmethod
    def _from_row(cls, row: Any) -> PredictionRecord:
        prediction_id = row["prediction_id"]
        if content_hash(row["statement"]) != row["statement_hash"]:
            raise IntegrityError(f"prediction statement hash mismatch: {prediction_id}")
        try:
            probability = strict_finite_float(row["probability"])
            outcome = strict_bool(row["outcome"]) if row["outcome"] is not None else None
            brier = (
                strict_finite_float(row["brier_score"]) if row["brier_score"] is not None else None
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(
                f"prediction durable values are invalid: {prediction_id}"
            ) from error
        if not 0.0 <= probability <= 1.0 or (brier is not None and not 0.0 <= brier <= 1.0):
            raise IntegrityError(f"prediction durable values are invalid: {prediction_id}")
        status = row["status"]
        resolved_at = row["resolved_at"]
        if status == "open":
            lifecycle_valid = outcome is None and brier is None and resolved_at is None
        elif status == "resolved":
            if outcome is None or brier is None or resolved_at is None:
                lifecycle_valid = False
            else:
                expected_brier = (probability - int(outcome)) ** 2
                lifecycle_valid = abs(brier - expected_brier) <= 1e-12
        elif status == "cancelled":
            lifecycle_valid = outcome is None and brier is None and resolved_at is not None
        else:
            lifecycle_valid = False
        if not lifecycle_valid:
            raise IntegrityError(f"prediction lifecycle is invalid: {prediction_id}")
        expected = cls._state_hash(
            row["statement"],
            probability,
            row["target_at"],
            row["resolution_criteria"],
            status,
            outcome,
            brier,
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"prediction state hash mismatch: {prediction_id}")
        return PredictionRecord(
            prediction_id,
            row["subject_id"],
            row["statement"],
            probability,
            row["target_at"],
            row["resolution_criteria"],
            status,
            outcome,
            brier,
            row["state_hash"],
            row["created_at"],
            resolved_at,
        )
