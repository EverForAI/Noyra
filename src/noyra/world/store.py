from __future__ import annotations

from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.events import EventStore
from noyra.core.types import canonical_json, content_hash, new_id, strict_json_loads, utc_now
from noyra.mind.causal import CausalStore

from .errors import WorldStateConflictError
from .observation_archive import ObservationContentArchive
from .source import canonical_public_url
from .types import ClaimProposal, ClaimRecord, FetchedDocument, ObservationRecord


class ObservationStore:
    def __init__(self, database: Database):
        self.database = database
        self.events = EventStore(database)
        self.causal = CausalStore(database)
        self._content_archive: ObservationContentArchive | None = None
        self._content_archive_subject_id: str | None = None

    def record(
        self, subject_id: str, source_id: str, document: FetchedDocument
    ) -> tuple[ObservationRecord, bool]:
        if len(document.content) > 500_000:
            raise ValueError("observation content exceeds the storage limit")
        with self.database.transaction() as connection:
            source = connection.execute(
                "SELECT * FROM world_sources WHERE source_id = ? AND subject_id = ?",
                (source_id, subject_id),
            ).fetchone()
            if source is None:
                raise NotFoundError("world source is missing or belongs to another subject")
            if source["status"] != "active":
                raise WorldStateConflictError("observation source is not active")
            normalized_document_url = canonical_public_url(document.url)
            if normalized_document_url != document.url or normalized_document_url != source["url"]:
                raise WorldStateConflictError(
                    "fetched document URL must match the canonical registered source"
                )
            existing = connection.execute(
                "SELECT * FROM observations WHERE subject_id = ? AND source_id = ? "
                "AND content_hash = ?",
                (subject_id, source_id, document.content_hash),
            ).fetchone()
            if existing is not None:
                return self._from_row(self._materialize_row(existing)), False
            if content_hash(document.content) != document.content_hash:
                raise IntegrityError("fetched document content hash mismatch")
            event = self.events._append_connection(
                connection,
                subject_id,
                "world_observation",
                "safe_web_reader",
                {
                    "source_id": source_id,
                    "url": document.url,
                    "content_hash": document.content_hash,
                    "media_type": document.media_type,
                    "injection_signals": list(document.injection_signals),
                },
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=document.fetched_at,
                event_id=None,
            )
            observation_id = new_id("obs")
            record_hash = self._record_hash(source_id, event.event_id, document)
            connection.execute(
                """INSERT INTO observations(
                    observation_id, subject_id, source_id, event_id, canonical_url,
                    title, content, content_hash, media_type, injection_signals_json,
                    record_hash,
                    http_etag, http_last_modified, fetched_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')""",
                (
                    observation_id,
                    subject_id,
                    source_id,
                    event.event_id,
                    document.url,
                    document.title,
                    document.content,
                    document.content_hash,
                    document.media_type,
                    canonical_json(list(document.injection_signals)),
                    record_hash,
                    document.etag,
                    document.last_modified,
                    document.fetched_at,
                ),
            )
            now = utc_now()
            connection.execute(
                """INSERT INTO observation_status_transitions(
                    transition_id, observation_id, from_status, to_status,
                    reason, state_hash, created_at
                ) VALUES (?, ?, NULL, 'new', 'observation recorded', ?, ?)""",
                (
                    new_id("ostr"),
                    observation_id,
                    self._transition_hash(None, "new", "observation recorded"),
                    now,
                ),
            )
            self.causal._add_connection(
                connection,
                subject_id,
                "event",
                event.event_id,
                "recorded_as",
                "observation",
                observation_id,
                strength=1,
                metadata={"source_id": source_id},
            )
            row = connection.execute(
                "SELECT * FROM observations WHERE observation_id = ? AND subject_id = ?",
                (observation_id, subject_id),
            ).fetchone()
            return self._from_row(self._materialize_row(row)), True

    def get(self, observation_id: str, *, subject_id: str) -> ObservationRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM observations WHERE observation_id = ? AND subject_id = ?",
                (observation_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"observation not found: {observation_id}")
        return self._from_row(self._materialize_row(row))

    def mark(
        self,
        observation_id: str,
        status: str,
        *,
        reason: str = "observation processing completed",
        subject_id: str,
    ) -> ObservationRecord:
        if status not in {"analyzed", "rejected"}:
            raise ValueError("observation can only be marked analyzed or rejected")
        if not reason.strip() or len(reason) > 10_000:
            raise ValueError("observation status reason is invalid")
        with self.database.transaction() as connection:
            updated = connection.execute(
                "UPDATE observations SET status = ? WHERE observation_id = ? "
                "AND subject_id = ? AND status = 'new'",
                (status, observation_id, subject_id),
            )
            if updated.rowcount != 1:
                raise WorldStateConflictError("observation is missing or already finalized")
            now = utc_now()
            connection.execute(
                """INSERT INTO observation_status_transitions(
                    transition_id, observation_id, from_status, to_status,
                    reason, state_hash, created_at
                ) VALUES (?, ?, 'new', ?, ?, ?, ?)""",
                (
                    new_id("ostr"),
                    observation_id,
                    status,
                    reason,
                    self._transition_hash("new", status, reason),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM observations WHERE observation_id = ? AND subject_id = ?",
                (observation_id, subject_id),
            ).fetchone()
            return self._from_row(self._materialize_row(row))

    def _materialize_row(
        self,
        row: Any,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any] | Any:
        archive_key = row["content_archive_key"]
        if archive_key is None:
            return row
        subject_id = str(row["subject_id"])
        if self._content_archive is None or self._content_archive_subject_id != subject_id:
            try:
                archive = ObservationContentArchive(
                    self.database,
                    self.database.path.parent / "subject" / "cold",
                    create_root=False,
                    subject_id=subject_id,
                )
            except (OSError, ValueError) as error:
                raise IntegrityError("observation archive key is unavailable or invalid") from error
            self._content_archive = archive
            self._content_archive_subject_id = subject_id
        assert self._content_archive is not None
        materialized = dict(row)
        materialized["content"] = self._content_archive.load_content(
            subject_id,
            str(archive_key),
            str(row["observation_id"]),
            connection=connection,
        )
        return materialized

    @staticmethod
    def _from_row(row: Any) -> ObservationRecord:
        if content_hash(row["content"]) != row["content_hash"]:
            raise IntegrityError(f"observation content hash mismatch: {row['observation_id']}")
        raw_signals = row["injection_signals_json"]
        if not isinstance(raw_signals, str):
            raise IntegrityError(f"observation signals invalid: {row['observation_id']}")
        try:
            signals = strict_json_loads(raw_signals)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"observation signals invalid: {row['observation_id']}") from error
        if not isinstance(signals, list) or not all(isinstance(item, str) for item in signals):
            raise IntegrityError(f"observation signals invalid: {row['observation_id']}")
        if ObservationStore._row_hash(row, tuple(signals)) != row["record_hash"]:
            raise IntegrityError(f"observation record hash mismatch: {row['observation_id']}")
        return ObservationRecord(
            row["observation_id"],
            row["subject_id"],
            row["source_id"],
            row["event_id"],
            row["canonical_url"],
            row["title"],
            row["content"],
            row["content_hash"],
            row["media_type"],
            tuple(signals),
            row["fetched_at"],
            row["status"],
        )

    @staticmethod
    def _record_hash(source_id: str, event_id: str, document: FetchedDocument) -> str:
        return content_hash(
            {
                "source_id": source_id,
                "event_id": event_id,
                "canonical_url": document.url,
                "title": document.title,
                "content_hash": document.content_hash,
                "media_type": document.media_type,
                "injection_signals": list(document.injection_signals),
                "etag": document.etag,
                "last_modified": document.last_modified,
                "fetched_at": document.fetched_at,
            }
        )

    @staticmethod
    def _row_hash(row: Any, signals: tuple[str, ...]) -> str:
        return content_hash(
            {
                "source_id": row["source_id"],
                "event_id": row["event_id"],
                "canonical_url": row["canonical_url"],
                "title": row["title"],
                "content_hash": row["content_hash"],
                "media_type": row["media_type"],
                "injection_signals": list(signals),
                "etag": row["http_etag"],
                "last_modified": row["http_last_modified"],
                "fetched_at": row["fetched_at"],
            }
        )

    @staticmethod
    def _transition_hash(from_status: str | None, to_status: str, reason: str) -> str:
        return content_hash({"from_status": from_status, "to_status": to_status, "reason": reason})


class WorldClaimStore:
    def __init__(self, database: Database):
        self.database = database
        self.causal = CausalStore(database)

    def create(
        self,
        subject_id: str,
        proposal: ClaimProposal,
        *,
        evidence_observation_ids: tuple[str, ...],
        reason: str = "claim inferred from observations",
        idempotency_key: str | None = None,
    ) -> ClaimRecord:
        if not reason.strip() or len(reason) > 10_000:
            raise ValueError("world claim reason is invalid")
        with self.database.transaction() as connection:
            evidence = self._validate_evidence(connection, subject_id, evidence_observation_ids)
            resolved_key = idempotency_key or content_hash(
                {"proposition": proposal.proposition, "evidence": list(evidence)}
            )
            if not resolved_key.strip() or len(resolved_key) > 256:
                raise ValueError("world claim idempotency key is invalid")
            existing = connection.execute(
                "SELECT * FROM world_claims WHERE subject_id = ? AND idempotency_key = ?",
                (subject_id, resolved_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["proposition_hash"] != content_hash(proposal.proposition)
                    or float(existing["confidence"]) != proposal.confidence
                ):
                    raise WorldStateConflictError(
                        "world claim idempotency key identifies different content"
                    )
                return self._from_row(existing)
            claim_id = new_id("claim")
            now = utc_now()
            state_hash = self._state_hash(proposal.proposition, proposal.confidence, "active")
            connection.execute(
                """INSERT INTO world_claims(
                    claim_id, subject_id, idempotency_key, proposition,
                    proposition_hash, confidence,
                    status, state_hash, current_revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, 1, ?, ?)""",
                (
                    claim_id,
                    subject_id,
                    resolved_key,
                    proposal.proposition,
                    content_hash(proposal.proposition),
                    proposal.confidence,
                    state_hash,
                    now,
                    now,
                ),
            )
            self._insert_revision(
                connection,
                claim_id,
                1,
                proposal.proposition,
                proposal.confidence,
                "active",
                evidence,
                reason,
                now,
            )
            self._link_evidence(
                connection, subject_id, evidence, claim_id, proposal.confidence, "supports"
            )
            return self._load_connection(connection, claim_id, subject_id=subject_id)

    def revise(
        self,
        claim_id: str,
        proposal: ClaimProposal,
        *,
        subject_id: str,
        status: str,
        evidence_observation_ids: tuple[str, ...],
        reason: str,
        expected_revision: int | None = None,
    ) -> ClaimRecord:
        if status not in {"active", "contested", "retracted"}:
            raise ValueError(f"invalid world claim status: {status}")
        if not reason.strip() or len(reason) > 10_000:
            raise ValueError("world claim reason is invalid")
        with self.database.transaction() as connection:
            row = self._get_row(connection, claim_id, subject_id=subject_id)
            revision = int(row["current_revision"])
            if expected_revision is not None and revision != expected_revision:
                raise WorldStateConflictError("world claim revision changed before update")
            evidence = self._validate_evidence(
                connection, row["subject_id"], evidence_observation_ids
            )
            revision += 1
            now = utc_now()
            state_hash = self._state_hash(proposal.proposition, proposal.confidence, status)
            self._insert_revision(
                connection,
                claim_id,
                revision,
                proposal.proposition,
                proposal.confidence,
                status,
                evidence,
                reason,
                now,
            )
            connection.execute(
                """UPDATE world_claims SET proposition = ?, proposition_hash = ?,
                    confidence = ?, status = ?, state_hash = ?, current_revision = ?,
                    updated_at = ? WHERE claim_id = ? AND subject_id = ?""",
                (
                    proposal.proposition,
                    content_hash(proposal.proposition),
                    proposal.confidence,
                    status,
                    state_hash,
                    revision,
                    now,
                    claim_id,
                    subject_id,
                ),
            )
            self._link_evidence(
                connection,
                row["subject_id"],
                evidence,
                claim_id,
                proposal.confidence,
                "contests" if status == "contested" else "supports",
            )
            return self._load_connection(connection, claim_id, subject_id=subject_id)

    def get(self, claim_id: str, *, subject_id: str) -> ClaimRecord:
        with self.database.connection() as connection:
            return self._load_connection(connection, claim_id, subject_id=subject_id)

    @staticmethod
    def _validate_evidence(
        connection: Any, subject_id: str, observation_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        ids = tuple(dict.fromkeys(observation_ids))
        if not ids or len(ids) > 64:
            raise ValueError("world claim requires 1-64 observations")
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT observation_id FROM observations WHERE subject_id = ? "
            f"AND observation_id IN ({placeholders})",
            (subject_id, *ids),
        ).fetchall()
        if {row["observation_id"] for row in rows} != set(ids):
            raise WorldStateConflictError("claim evidence is missing or belongs to another subject")
        return ids

    def _link_evidence(
        self,
        connection: Any,
        subject_id: str,
        evidence: tuple[str, ...],
        claim_id: str,
        confidence: float,
        relation: str,
    ) -> None:
        for observation_id in evidence:
            self.causal._add_connection(
                connection,
                subject_id,
                "observation",
                observation_id,
                relation,
                "world_claim",
                claim_id,
                strength=confidence,
                metadata={},
            )

    @staticmethod
    def _state_hash(proposition: str, confidence: float, status: str) -> str:
        return content_hash(
            {
                "proposition": proposition,
                "confidence": float(confidence),
                "status": status,
            }
        )

    @classmethod
    def _insert_revision(
        cls,
        connection: Any,
        claim_id: str,
        revision: int,
        proposition: str,
        confidence: float,
        status: str,
        evidence: tuple[str, ...],
        reason: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO world_claim_revisions(
                revision_id, claim_id, revision_number, proposition, proposition_hash,
                confidence, status, evidence_observation_ids_json, reason,
                state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("crev"),
                claim_id,
                revision,
                proposition,
                content_hash(proposition),
                confidence,
                status,
                canonical_json(list(evidence)),
                reason,
                cls._state_hash(proposition, confidence, status),
                created_at,
            ),
        )

    @staticmethod
    def _get_row(connection: Any, claim_id: str, *, subject_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM world_claims WHERE claim_id = ? AND subject_id = ?",
            (claim_id, subject_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"world claim not found: {claim_id}")
        return row

    @classmethod
    def _load_connection(cls, connection: Any, claim_id: str, *, subject_id: str) -> ClaimRecord:
        return cls._from_row(cls._get_row(connection, claim_id, subject_id=subject_id))

    @classmethod
    def _from_row(cls, row: Any) -> ClaimRecord:
        if content_hash(row["proposition"]) != row["proposition_hash"]:
            raise IntegrityError(f"world claim proposition hash mismatch: {row['claim_id']}")
        expected = cls._state_hash(row["proposition"], float(row["confidence"]), row["status"])
        if expected != row["state_hash"]:
            raise IntegrityError(f"world claim state hash mismatch: {row['claim_id']}")
        return ClaimRecord(
            row["claim_id"],
            row["subject_id"],
            row["proposition"],
            row["proposition_hash"],
            float(row["confidence"]),
            row["status"],
            row["state_hash"],
            int(row["current_revision"]),
            row["created_at"],
            row["updated_at"],
        )
