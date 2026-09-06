from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash, strict_bool, strict_finite_float, strict_int

from .genesis import GenesisProtocol
from .observation_archive import ObservationContentArchive
from .prediction import PredictionStore
from .source import SourceRegistry
from .store import ObservationStore, WorldClaimStore


def _world_int(value: object, context: str) -> int:
    try:
        return strict_int(value)
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"{context} is invalid") from error


def _world_float(value: object, context: str) -> float:
    try:
        return strict_finite_float(value)
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"{context} is invalid") from error


class WorldIntegrity:
    def __init__(
        self,
        database: Database,
        *,
        observation_archive_root: Path | str | None = None,
        archive_max_object_bytes: int = 16_000_000,
        archive_max_total_bytes: int = 64_000_000,
        archive_checkpoint: Callable[[], None] | None = None,
        archive_consume_bytes: Callable[[int], None] | None = None,
    ):
        self.database = database
        self.observation_archive_root = (
            Path(observation_archive_root).resolve()
            if observation_archive_root is not None
            else database.path.parent / "subject" / "cold"
        )
        self.archive_max_object_bytes = archive_max_object_bytes
        self.archive_max_total_bytes = archive_max_total_bytes
        self.archive_checkpoint = archive_checkpoint
        self.archive_consume_bytes = archive_consume_bytes

    def verify(self, subject_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.database.read_transaction() as connection:
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise IntegrityError("world state contains broken foreign keys")

            archive_present = (
                connection.execute(
                    """SELECT 1 FROM observation_content_segments s
                       WHERE s.subject_id = ? OR EXISTS (
                           SELECT 1 FROM observations o
                           WHERE o.subject_id = ? AND o.content_archive_key = s.object_key
                       ) LIMIT 1""",
                    (subject_id, subject_id),
                ).fetchone()
                is not None
                or connection.execute(
                    "SELECT 1 FROM observations WHERE subject_id = ? "
                    "AND content_archive_key IS NOT NULL LIMIT 1",
                    (subject_id,),
                ).fetchone()
                is not None
            )
            archived_contents: dict[str, str] = {}
            if archive_present:
                try:
                    archive = ObservationContentArchive(
                        self.database,
                        self.observation_archive_root,
                        create_root=False,
                        cache_cloud_restores=False,
                        subject_id=subject_id,
                    )
                except (OSError, ValueError) as error:
                    raise IntegrityError(
                        "observation archive key is unavailable or invalid"
                    ) from error
                segment_count, archived_contents = archive.verify_integrity(
                    subject_id,
                    connection=connection,
                    max_object_bytes=self.archive_max_object_bytes,
                    max_total_bytes=self.archive_max_total_bytes,
                    checkpoint=self.archive_checkpoint,
                    consume_bytes=self.archive_consume_bytes,
                )
            else:
                segment_count = 0
            counts["observation_content_segments"] = segment_count

            sources = connection.execute(
                "SELECT * FROM world_sources WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for row in sources:
                SourceRegistry._from_row(row)
                revisions = connection.execute(
                    "SELECT * FROM world_source_revisions WHERE source_id = ? "
                    "ORDER BY revision_number",
                    (row["source_id"],),
                ).fetchall()
                if len(revisions) != _world_int(
                    row["current_revision"], f"world source revision {row['source_id']}"
                ):
                    raise IntegrityError(f"world source revision mismatch: {row['source_id']}")
                for number, revision in enumerate(revisions, 1):
                    if _world_int(
                        revision["revision_number"],
                        f"world source revision {row['source_id']}",
                    ) != number or revision["state_hash"] != SourceRegistry._revision_hash(
                        _world_float(
                            revision["trust_score"],
                            f"world source trust score {row['source_id']}",
                        ),
                        revision["status"],
                    ):
                        raise IntegrityError(f"world source revision mismatch: {row['source_id']}")
                latest = revisions[-1]
                if (
                    _world_float(
                        latest["trust_score"], f"world source trust score {row['source_id']}"
                    )
                    != _world_float(
                        row["trust_score"], f"world source trust score {row['source_id']}"
                    )
                    or latest["status"] != row["status"]
                ):
                    raise IntegrityError(f"world source current state mismatch: {row['source_id']}")
            counts["world_sources"] = len(sources)

            observations = connection.execute(
                "SELECT * FROM observations WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for row in observations:
                if row["content_archive_key"] is None:
                    if row["content_archived_at"] is not None:
                        raise IntegrityError(
                            f"observation archive marker mismatch: {row['observation_id']}"
                        )
                    materialized = row
                else:
                    content = archived_contents.get(str(row["observation_id"]))
                    if content is None:
                        raise IntegrityError(
                            f"observation archive content is missing: {row['observation_id']}"
                        )
                    materialized = dict(row)
                    materialized["content"] = content
                ObservationStore._from_row(materialized)
                source = connection.execute(
                    "SELECT subject_id, url FROM world_sources WHERE source_id = ?",
                    (row["source_id"],),
                ).fetchone()
                event = connection.execute(
                    "SELECT subject_id, event_type FROM events WHERE event_id = ?",
                    (row["event_id"],),
                ).fetchone()
                if (
                    source is None
                    or source["subject_id"] != subject_id
                    or source["url"] != row["canonical_url"]
                    or event is None
                    or event["subject_id"] != subject_id
                    or event["event_type"] != "world_observation"
                ):
                    raise IntegrityError(
                        f"observation provenance mismatch: {row['observation_id']}"
                    )
                transitions = connection.execute(
                    "SELECT * FROM observation_status_transitions "
                    "WHERE observation_id = ? ORDER BY rowid",
                    (row["observation_id"],),
                ).fetchall()
                expected_from: str | None = None
                for transition in transitions:
                    if transition["from_status"] != expected_from or transition[
                        "state_hash"
                    ] != ObservationStore._transition_hash(
                        transition["from_status"],
                        transition["to_status"],
                        transition["reason"],
                    ):
                        raise IntegrityError(
                            f"observation transition mismatch: {row['observation_id']}"
                        )
                    expected_from = transition["to_status"]
                if not transitions or expected_from != row["status"]:
                    raise IntegrityError(
                        f"observation current status mismatch: {row['observation_id']}"
                    )
            counts["observations"] = len(observations)

            claims = connection.execute(
                "SELECT * FROM world_claims WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for row in claims:
                WorldClaimStore._from_row(row)
                revisions = connection.execute(
                    "SELECT * FROM world_claim_revisions WHERE claim_id = ? "
                    "ORDER BY revision_number",
                    (row["claim_id"],),
                ).fetchall()
                if len(revisions) != _world_int(
                    row["current_revision"], f"world claim revision {row['claim_id']}"
                ):
                    raise IntegrityError(f"world claim revision mismatch: {row['claim_id']}")
                for number, revision in enumerate(revisions, 1):
                    evidence = self._evidence_ids(
                        revision["evidence_observation_ids_json"],
                        f"world claim revision {revision['revision_id']}",
                    )
                    self._verify_observation_subject(connection, subject_id, evidence)
                    if (
                        _world_int(
                            revision["revision_number"],
                            f"world claim revision {row['claim_id']}",
                        )
                        != number
                        or revision["proposition_hash"] != content_hash(revision["proposition"])
                        or revision["state_hash"]
                        != WorldClaimStore._state_hash(
                            revision["proposition"],
                            _world_float(
                                revision["confidence"],
                                f"world claim confidence {row['claim_id']}",
                            ),
                            revision["status"],
                        )
                    ):
                        raise IntegrityError(f"world claim revision mismatch: {row['claim_id']}")
                latest = revisions[-1]
                if (
                    latest["proposition"] != row["proposition"]
                    or _world_float(
                        latest["confidence"], f"world claim confidence {row['claim_id']}"
                    )
                    != _world_float(row["confidence"], f"world claim confidence {row['claim_id']}")
                    or latest["status"] != row["status"]
                ):
                    raise IntegrityError(f"world claim current state mismatch: {row['claim_id']}")
            counts["world_claims"] = len(claims)

            predictions = connection.execute(
                "SELECT * FROM predictions WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for row in predictions:
                PredictionStore._from_row(row)
            counts["predictions"] = len(predictions)

            reviews = connection.execute(
                """SELECT prediction_reviews.* FROM prediction_reviews
                   JOIN predictions ON predictions.prediction_id = prediction_reviews.prediction_id
                   WHERE predictions.subject_id = ?""",
                (subject_id,),
            ).fetchall()
            for row in reviews:
                evidence = self._evidence_ids(
                    row["evidence_observation_ids_json"],
                    f"prediction review {row['review_id']}",
                )
                self._verify_observation_subject(connection, subject_id, evidence)
                try:
                    outcome = strict_bool(row["outcome"]) if row["outcome"] is not None else None
                except (TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"prediction review outcome is invalid: {row['review_id']}"
                    ) from error
                expected = PredictionStore._review_hash(
                    outcome,
                    evidence,
                    row["rationale"],
                    row["resulting_status"],
                    (
                        _world_float(row["brier_score"], f"prediction review {row['review_id']}")
                        if row["brier_score"] is not None
                        else None
                    ),
                )
                if expected != row["state_hash"]:
                    raise IntegrityError(f"prediction review mismatch: {row['review_id']}")
            counts["prediction_reviews"] = len(reviews)
            for prediction in predictions:
                history = connection.execute(
                    "SELECT * FROM prediction_reviews WHERE prediction_id = ? ORDER BY rowid",
                    (prediction["prediction_id"],),
                ).fetchall()
                if (
                    not history
                    or history[0]["resulting_status"] != "open"
                    or history[-1]["resulting_status"] != prediction["status"]
                    or len(history) != (1 if prediction["status"] == "open" else 2)
                ):
                    raise IntegrityError(
                        f"prediction review history mismatch: {prediction['prediction_id']}"
                    )

            runs = connection.execute(
                "SELECT * FROM genesis_runs WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for row in runs:
                GenesisProtocol._from_row(row)
                transitions = connection.execute(
                    "SELECT * FROM genesis_transitions WHERE run_id = ? ORDER BY rowid",
                    (row["run_id"],),
                ).fetchall()
                expected_status = "created"
                for transition in transitions:
                    if transition["from_status"] != expected_status or transition[
                        "state_hash"
                    ] != GenesisProtocol._transition_hash(
                        transition["from_status"],
                        transition["to_status"],
                        transition["reason"],
                    ):
                        raise IntegrityError(f"genesis transition mismatch: {row['run_id']}")
                    expected_status = transition["to_status"]
                if expected_status != row["status"]:
                    raise IntegrityError(f"genesis current status mismatch: {row['run_id']}")
                cycles = connection.execute(
                    "SELECT * FROM genesis_cycles WHERE run_id = ? ORDER BY cycle_number",
                    (row["run_id"],),
                ).fetchall()
                for cycle in cycles:
                    record = GenesisProtocol._cycle_from_row(cycle)
                    self._verify_entity_subject(
                        connection,
                        "observations",
                        "observation_id",
                        subject_id,
                        record.observation_ids,
                    )
                    self._verify_entity_subject(
                        connection, "appraisals", "appraisal_id", subject_id, record.appraisal_ids
                    )
                    self._verify_entity_subject(
                        connection,
                        "predictions",
                        "prediction_id",
                        subject_id,
                        record.prediction_ids,
                    )
                    self._verify_entity_subject(
                        connection, "goals", "goal_id", subject_id, record.goal_ids
                    )
                if len(cycles) != _world_int(
                    row["completed_cycles"], f"genesis cycle count {row['run_id']}"
                ):
                    raise IntegrityError(f"genesis cycle count mismatch: {row['run_id']}")
                if _world_int(row["version"], f"genesis version {row['run_id']}") != 1 + len(
                    transitions
                ) + len(cycles):
                    raise IntegrityError(f"genesis version history mismatch: {row['run_id']}")
            counts["genesis_runs"] = len(runs)
            counts["genesis_cycles"] = sum(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM genesis_cycles WHERE run_id = ?",
                        (row["run_id"],),
                    ).fetchone()[0]
                )
                for row in runs
            )
        return counts

    @staticmethod
    def _evidence_ids(value: object, context: str) -> tuple[str, ...]:
        return PredictionStore._review_evidence_ids(value, context)

    @classmethod
    def _verify_observation_subject(
        cls, connection: Any, subject_id: str, values: tuple[str, ...]
    ) -> None:
        cls._verify_entity_subject(connection, "observations", "observation_id", subject_id, values)

    @staticmethod
    def _verify_entity_subject(
        connection: Any,
        table: str,
        key: str,
        subject_id: str,
        values: tuple[str, ...],
    ) -> None:
        if not values:
            return
        placeholders = ",".join("?" for _ in values)
        rows = connection.execute(
            f"SELECT {key} FROM {table} WHERE subject_id = ? AND {key} IN ({placeholders})",
            (subject_id, *values),
        ).fetchall()
        if {item[key] for item in rows} != set(values):
            raise IntegrityError(f"{table} references cross a subject boundary")
