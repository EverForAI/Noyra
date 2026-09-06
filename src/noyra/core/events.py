from __future__ import annotations

import builtins
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .database import Database
from .errors import EventConflictError, IntegrityError, NotFoundError
from .event_archive import EventPayloadArchive
from .storage import TrainingStore
from .types import (
    EventRecord,
    canonical_json,
    content_hash,
    new_id,
    strict_int,
    strict_json_loads,
    utc_now,
)

PROCESSING_STATES = frozenset({"recorded", "processed", "failed"})


class EventStore:
    def __init__(self, database: Database, *, max_archive_cache_segments: int = 8):
        if max_archive_cache_segments < 1:
            raise ValueError("event archive cache bound must be positive")
        self.database = database
        self.training = TrainingStore(database)
        self._archive: EventPayloadArchive | None = None
        self._archive_subject_id: str | None = None
        # Segment object keys are only unique within a subject.  Include the
        # subject in the cache key so one EventStore can safely serve multiple
        # subjects without reusing another subject's decrypted payload.
        self._archive_cache: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._max_archive_cache_segments = max_archive_cache_segments

    def append(
        self,
        subject_id: str,
        event_type: str,
        source: str,
        payload: dict[str, Any],
        *,
        privacy_level: str = "private",
        causal_parent_ids: Iterable[str] = (),
        occurred_at: str | None = None,
        event_id: str | None = None,
    ) -> EventRecord:
        with self.database.transaction() as connection:
            return self._append_connection(
                connection,
                subject_id,
                event_type,
                source,
                payload,
                privacy_level=privacy_level,
                causal_parent_ids=causal_parent_ids,
                occurred_at=occurred_at,
                event_id=event_id,
            )

    def _append_connection(
        self,
        connection: Any,
        subject_id: str,
        event_type: str,
        source: str,
        payload: dict[str, Any],
        *,
        privacy_level: str,
        causal_parent_ids: Iterable[str],
        occurred_at: str | None,
        event_id: str | None,
    ) -> EventRecord:
        if not event_type.strip() or not source.strip():
            raise ValueError("event_type and source are required")
        if not privacy_level.strip():
            raise ValueError("privacy_level is required")
        event_id = event_id or new_id("evt")
        runtime_timestamp = occurred_at is None
        occurred_at = self._normalize_timestamp(occurred_at or utc_now())
        observed_at = utc_now()
        parents = tuple(causal_parent_ids)
        if len(parents) != len(set(parents)) or event_id in parents:
            raise ValueError("event causal parents must be distinct prior events")
        payload_json = canonical_json(payload)
        payload_hash = content_hash(payload)
        existing = connection.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing is not None:
            if (
                existing["subject_id"] != subject_id
                or existing["event_type"] != event_type
                or existing["source"] != source
                or existing["payload_hash"] != payload_hash
                or existing["privacy_level"] != privacy_level
                or existing["causal_parent_ids_json"] != canonical_json(list(parents))
            ):
                raise EventConflictError(
                    f"event ID already exists with different content: {event_id}"
                )
            self.training.record_event(
                subject_id,
                event_id,
                event_type,
                privacy_level,
                payload_hash,
                occurred_at=existing["occurred_at"],
                observed_at=existing["observed_at"],
                connection=connection,
            )
            return self._from_row(existing)
        for parent_id in parents:
            parent = connection.execute(
                "SELECT subject_id, occurred_at FROM events WHERE event_id = ?", (parent_id,)
            ).fetchone()
            if parent is None:
                raise ValueError(f"event causal parent is unavailable: {parent_id}")
            if parent["subject_id"] != subject_id:
                raise PermissionError("event causal parent belongs to another subject")
            if runtime_timestamp and str(parent["occurred_at"]) > occurred_at:
                raise ValueError("event causal parent occurred_at cannot be after the child event")
        connection.execute(
            """INSERT INTO events(
                event_id, subject_id, event_type, source, occurred_at, observed_at,
                payload_json, payload_hash, privacy_level, causal_parent_ids_json, processing_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'recorded')""",
            (
                event_id,
                subject_id,
                event_type,
                source,
                occurred_at,
                observed_at,
                payload_json,
                payload_hash,
                privacy_level,
                canonical_json(list(parents)),
            ),
        )
        self.training.record_event(
            subject_id,
            event_id,
            event_type,
            privacy_level,
            payload_hash,
            occurred_at=occurred_at,
            observed_at=observed_at,
            connection=connection,
        )
        self._append_chain_connection(connection, subject_id, event_id)
        return EventRecord(
            event_id=event_id,
            subject_id=subject_id,
            event_type=event_type,
            source=source,
            occurred_at=occurred_at,
            observed_at=observed_at,
            payload=payload,
            payload_hash=payload_hash,
            privacy_level=privacy_level,
            causal_parent_ids=parents,
            processing_status="recorded",
        )

    def get(self, event_id: str) -> EventRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"event not found: {event_id}")
        return self._from_row(row)

    def list(
        self, subject_id: str, *, limit: int = 100, status: str | None = None
    ) -> list[EventRecord]:
        limit = max(1, min(limit, 1000))
        with self.database.connection() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM events WHERE subject_id = ? AND processing_status = ? "
                    "ORDER BY occurred_at DESC, event_id DESC LIMIT ?",
                    (subject_id, status, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM events WHERE subject_id = ? "
                    "ORDER BY occurred_at DESC, event_id DESC LIMIT ?",
                    (subject_id, limit),
                ).fetchall()
        return [self._from_row(row) for row in rows]

    def mark_processed(self, event_id: str, status: str = "processed", *, subject_id: str) -> None:
        if status not in PROCESSING_STATES - {"recorded"}:
            raise ValueError(f"invalid event processing status: {status}")
        with self.database.transaction() as connection:
            updated = connection.execute(
                "UPDATE events SET processing_status = ? WHERE event_id = ? AND subject_id = ?",
                (status, event_id, subject_id),
            )
            if updated.rowcount != 1:
                raise NotFoundError(f"event not found: {event_id}")

    def verify_chain(self, subject_id: str) -> dict[str, str | int | None]:
        with self.database.read_transaction() as connection:
            event_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE subject_id = ?", (subject_id,)
                ).fetchone()[0]
            )
            root_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM event_chain_roots WHERE subject_id = ?", (subject_id,)
                ).fetchone()[0]
            )
            if event_count != root_count:
                raise IntegrityError("event chain coverage mismatch")
            rows = connection.execute(
                "SELECT r.sequence_number, r.event_id AS root_event_id, "
                "r.previous_root_hash, r.root_hash, e.* FROM event_chain_roots r "
                "JOIN events e ON e.event_id = r.event_id AND e.subject_id = r.subject_id "
                "WHERE r.subject_id = ? ORDER BY r.sequence_number",
                (subject_id,),
            )
            previous: str | None = None
            verified = 0
            for sequence, row in enumerate(rows, start=1):
                expected = self._chain_hash(row, sequence, previous)
                try:
                    persisted_sequence = strict_int(row["sequence_number"])
                except (TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"event chain sequence is invalid at position {sequence}"
                    ) from error
                if (
                    persisted_sequence != sequence
                    or row["root_event_id"] != row["event_id"]
                    or row["previous_root_hash"] != previous
                    or row["root_hash"] != expected
                ):
                    raise IntegrityError(f"event chain mismatch at sequence {sequence}")
                previous = expected
                verified += 1
        if verified != event_count:
            raise IntegrityError("event chain coverage mismatch")
        return {"event_count": verified, "root_hash": previous}

    def causal_anomalies(
        self, subject_id: str, *, limit: int = 100
    ) -> builtins.list[dict[str, str]]:
        """Report historical causal edges whose parent timestamp is after the child."""
        bounded = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            nonempty = connection.execute(
                "SELECT 1 FROM events WHERE subject_id = ? "
                "AND causal_parent_ids_json != '[]' LIMIT 1",
                (subject_id,),
            ).fetchone()
            if nonempty is None:
                return []
            invalid = connection.execute(
                "SELECT event_id FROM events WHERE subject_id = ? "
                "AND causal_parent_ids_json != '[]' "
                "AND json_valid(causal_parent_ids_json) = 0 LIMIT 1",
                (subject_id,),
            ).fetchone()
            if invalid is not None:
                raise IntegrityError(f"event causal parents are invalid: {invalid['event_id']}")
            rows = connection.execute(
                "SELECT child.event_id, child.occurred_at, parent.event_id AS parent_event_id, "
                "parent.occurred_at AS parent_occurred_at FROM events child "
                "JOIN json_each(child.causal_parent_ids_json) edge "
                "JOIN events parent ON parent.event_id = CAST(edge.value AS TEXT) "
                "AND parent.subject_id = child.subject_id "
                "WHERE child.subject_id = ? AND child.causal_parent_ids_json != '[]' "
                "AND parent.occurred_at > child.occurred_at "
                "ORDER BY child.occurred_at, child.event_id, parent.event_id LIMIT ?",
                (subject_id, bounded),
            ).fetchall()
        return [
            {
                "event_id": str(row["event_id"]),
                "parent_event_id": str(row["parent_event_id"]),
                "event_occurred_at": str(row["occurred_at"]),
                "parent_occurred_at": str(row["parent_occurred_at"]),
            }
            for row in rows
        ]

    @classmethod
    def _append_chain_connection(cls, connection: Any, subject_id: str, event_id: str) -> None:
        event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE subject_id = ?", (subject_id,)
            ).fetchone()[0]
        )
        chain_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM event_chain_roots WHERE subject_id = ?", (subject_id,)
            ).fetchone()[0]
        )
        if chain_count == 0 and event_count > 1:
            events = connection.execute(
                "SELECT * FROM events WHERE subject_id = ? ORDER BY rowid",
                (subject_id,),
            ).fetchall()
            previous: str | None = None
            for sequence, event in enumerate(events, start=1):
                root = cls._chain_hash(event, sequence, previous)
                connection.execute(
                    "INSERT INTO event_chain_roots(event_id, subject_id, sequence_number, "
                    "previous_root_hash, root_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event["event_id"],
                        subject_id,
                        sequence,
                        previous,
                        root,
                        utc_now(),
                    ),
                )
                previous = root
            return
        if chain_count != event_count - 1:
            raise IntegrityError("event chain is incomplete before append")
        previous_row = connection.execute(
            "SELECT root_hash FROM event_chain_roots WHERE subject_id = ? "
            "ORDER BY sequence_number DESC LIMIT 1",
            (subject_id,),
        ).fetchone()
        previous = None if previous_row is None else str(previous_row["root_hash"])
        event = connection.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        root = cls._chain_hash(event, event_count, previous)
        connection.execute(
            "INSERT INTO event_chain_roots(event_id, subject_id, sequence_number, "
            "previous_root_hash, root_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, subject_id, event_count, previous, root, utc_now()),
        )

    @staticmethod
    def _chain_hash(event: Any, sequence: int, previous: str | None) -> str:
        return content_hash(
            {
                "subject_id": event["subject_id"],
                "sequence_number": sequence,
                "previous_root_hash": previous,
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "source": event["source"],
                "occurred_at": event["occurred_at"],
                "observed_at": event["observed_at"],
                "payload_hash": event["payload_hash"],
                "privacy_level": event["privacy_level"],
                "causal_parent_ids_json": event["causal_parent_ids_json"],
            }
        )

    def payload_from_row(
        self,
        row: Any,
        *,
        connection: Any | None = None,
        max_archive_bytes: int | None = None,
    ) -> dict[str, Any]:
        archive_key = row["payload_archive_key"]
        if archive_key is None:
            raw_payload = row["payload_json"]
            if not isinstance(raw_payload, str):
                raise IntegrityError(f"event payload JSON is invalid: {row['event_id']}")
            try:
                payload = strict_json_loads(raw_payload)
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"event payload JSON is invalid: {row['event_id']}") from error
            if not isinstance(payload, dict):
                raise IntegrityError(f"event payload is not an object: {row['event_id']}")
            return payload
        subject_id = str(row["subject_id"])
        key = str(archive_key)
        cache_key = (subject_id, key)
        segment = self._archive_cache.get(cache_key)
        if segment is None:
            if self._archive is None or self._archive_subject_id != subject_id:
                try:
                    archive = EventPayloadArchive(
                        self.database,
                        self.database.path.parent / "subject" / "cold",
                        subject_id=subject_id,
                    )
                except (OSError, ValueError) as error:
                    raise IntegrityError(
                        "event payload archive key is unavailable or invalid"
                    ) from error
                self._archive = archive
                self._archive_subject_id = subject_id
            assert self._archive is not None
            segment = self._archive.load_segment(
                subject_id,
                key,
                connection=connection,
                max_decompressed_bytes=max_archive_bytes,
            )
            if len(self._archive_cache) >= self._max_archive_cache_segments:
                self._archive_cache.pop(next(iter(self._archive_cache)))
            self._archive_cache[cache_key] = segment
        payload = segment.get(str(row["event_id"]))
        if payload is None:
            raise IntegrityError(f"event is missing from archive segment: {row['event_id']}")
        return payload

    def _from_row(self, row: Any) -> EventRecord:
        payload = self.payload_from_row(row)
        raw_parents = row["causal_parent_ids_json"]
        if not isinstance(raw_parents, str):
            raise IntegrityError(f"event causal parents are invalid: {row['event_id']}")
        try:
            parents = strict_json_loads(raw_parents)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"event causal parents are invalid: {row['event_id']}") from error
        if not isinstance(parents, list) or not all(isinstance(item, str) for item in parents):
            raise IntegrityError(f"event causal parents are invalid: {row['event_id']}")
        payload_hash = content_hash(payload)
        if payload_hash != row["payload_hash"]:
            raise IntegrityError(f"event payload hash mismatch: {row['event_id']}")
        processing_status = row["processing_status"]
        if processing_status not in PROCESSING_STATES:
            raise IntegrityError(f"event processing status is invalid: {row['event_id']}")
        return EventRecord(
            event_id=row["event_id"],
            subject_id=row["subject_id"],
            event_type=row["event_type"],
            source=row["source"],
            occurred_at=row["occurred_at"],
            observed_at=row["observed_at"],
            payload=payload,
            payload_hash=row["payload_hash"],
            privacy_level=row["privacy_level"],
            causal_parent_ids=tuple(parents),
            processing_status=processing_status,
        )

    @staticmethod
    def _normalize_timestamp(value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("event occurred_at must be an ISO-8601 timestamp") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("event occurred_at must include a timezone")
        return parsed.astimezone(UTC).isoformat(timespec="milliseconds")
