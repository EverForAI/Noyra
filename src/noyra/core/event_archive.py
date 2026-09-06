from __future__ import annotations

import hashlib
import json
import zlib
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .admission import assert_current_lease, current_commit_scope
from .archive import (
    ArchiveKeyring,
    ArchiveProvider,
    ArchiveReplicaLedger,
    ArchiveReplicaResolver,
    ArchiveStagingLedger,
    LocalArchiveProvider,
    resolve_subject_archive_root,
)
from .database import Database
from .errors import (
    ArchiveAuthenticationError,
    ArchiveKeyUnavailableError,
    ArchiveUnavailableError,
    IntegrityError,
    PayloadLimitError,
)
from .payload_codec import decompress_bytes
from .types import canonical_json, content_hash, new_id, strict_json_loads, utc_now

MAX_EVENT_ARCHIVE_SEGMENT_EVENTS = 5_000
EVENT_ARCHIVE_FORMAT = "noyra-event-payload-segment-v1"
MAX_STAGED_EVENT_DECOMPRESSED_BYTES = 64_000_000
# Leave headroom for the segment envelope, IDs, and hashes.  The final
# canonical payload length is checked again before the provider write.
MAX_EVENT_ARCHIVE_SOURCE_BYTES = MAX_STAGED_EVENT_DECOMPRESSED_BYTES - 2_000_000


class _EventArchiveSourceConflict(IntegrityError):
    """Selected event rows changed before a staged archive could be published."""


@dataclass(frozen=True)
class EventArchiveVerification:
    """Reconciled counts for one subject's encrypted event archive."""

    status: str
    segments: int
    events: int
    verified_segments: int
    verified_events: int
    reason: str | None = None


@dataclass(frozen=True)
class EventArchiveVerificationLimits:
    """Resource envelope for one synchronous event-archive verification."""

    max_segment_decompressed_bytes: int = 64_000_000
    max_total_decompressed_bytes: int = 512_000_000
    max_segments: int = 2_048
    max_events: int = 100_000
    max_events_per_segment: int = 5_000
    max_hot_event_payload_bytes: int = 2_000_000
    max_hot_event_bytes: int = 128_000_000

    def __post_init__(self) -> None:
        values = (
            self.max_segment_decompressed_bytes,
            self.max_total_decompressed_bytes,
            self.max_segments,
            self.max_events,
            self.max_events_per_segment,
            self.max_hot_event_payload_bytes,
            self.max_hot_event_bytes,
        )
        if any(value < 1 for value in values):
            raise ValueError("event archive verification limits must be positive")
        if self.max_segment_decompressed_bytes > self.max_total_decompressed_bytes:
            raise ValueError("event archive segment limit cannot exceed the total byte limit")
        if self.max_events_per_segment > self.max_events:
            raise ValueError(
                "event archive segment event limit cannot exceed the total event limit"
            )
        if self.max_events_per_segment > MAX_EVENT_ARCHIVE_SEGMENT_EVENTS:
            raise ValueError("event archive segment event limit exceeds the format limit")
        if self.max_hot_event_payload_bytes > self.max_hot_event_bytes:
            raise ValueError("hot event payload limit cannot exceed the total hot event limit")


@dataclass(frozen=True)
class _LoadedEventArchiveSegment:
    payloads: dict[str, dict[str, Any]]
    decompressed_bytes: int


class EventPayloadArchive:
    """Move cold event payloads to encrypted, verified segments while retaining metadata."""

    def __init__(
        self,
        database: Database,
        root: Path | str,
        *,
        create_root: bool = True,
        keyring: ArchiveKeyring | None = None,
        cloud_provider: ArchiveProvider | None = None,
        cache_cloud_restores: bool = True,
        subject_id: str | None = None,
    ):
        self.database = database
        self.root = (
            Path(root).resolve()
            if subject_id is None
            else resolve_subject_archive_root(database, subject_id, root, create=create_root)
        )
        self.keyring = keyring or ArchiveKeyring.from_env()
        active = self.keyring.active
        self.provider = LocalArchiveProvider(
            self.root,
            encryption_key=active.key,
            key_id=active.key_id,
            create_root=create_root,
        )
        self.cloud_provider = cloud_provider
        self.cache_cloud_restores = cache_cloud_restores
        self.replicas = ArchiveReplicaLedger(database)
        self.staging = ArchiveStagingLedger(database)
        self.format = EVENT_ARCHIVE_FORMAT

    @classmethod
    def verify_subject_integrity(
        cls,
        database: Database,
        root: Path | str,
        subject_id: str,
        *,
        connection: Any | None = None,
        limits: EventArchiveVerificationLimits | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> EventArchiveVerification:
        """Verify every archived event and its segment metadata for one subject."""
        limits = limits or EventArchiveVerificationLimits()
        if connection is None:
            with database.read_transaction() as owned_connection:
                return cls.verify_subject_integrity(
                    database,
                    root,
                    subject_id,
                    connection=owned_connection,
                    limits=limits,
                    checkpoint=checkpoint,
                )
        segment_count, archived_event_count = cls._verify_database_manifest(
            connection,
            subject_id,
            limits,
            checkpoint=checkpoint,
        )
        if segment_count == 0 and archived_event_count == 0:
            return EventArchiveVerification("ok", 0, 0, 0, 0)
        try:
            archive = cls(
                database,
                root,
                create_root=False,
                cache_cloud_restores=False,
                subject_id=subject_id,
            )
        except ValueError as error:
            raise ArchiveKeyUnavailableError(
                "event archive encryption key is unavailable"
            ) from error
        except OSError as error:
            raise ArchiveUnavailableError("event archive root is unavailable") from error
        archive.keyring.validate_revision(database, subject_id, connection=connection)
        return archive._verify_payload_integrity(
            subject_id,
            connection=connection,
            limits=limits,
            segment_count=segment_count,
            archived_event_count=archived_event_count,
        )

    def verify_integrity(
        self,
        subject_id: str,
        *,
        connection: Any | None = None,
        limits: EventArchiveVerificationLimits | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> EventArchiveVerification:
        if connection is None:
            with self.database.read_transaction() as owned_connection:
                return self.verify_integrity(
                    subject_id,
                    connection=owned_connection,
                    limits=limits,
                    checkpoint=checkpoint,
                )

        limits = limits or EventArchiveVerificationLimits()
        segment_count, archived_event_count = self._verify_database_manifest(
            connection,
            subject_id,
            limits,
            checkpoint=checkpoint,
        )
        self.keyring.validate_revision(self.database, subject_id, connection=connection)
        return self._verify_payload_integrity(
            subject_id,
            connection=connection,
            limits=limits,
            segment_count=segment_count,
            archived_event_count=archived_event_count,
        )

    @classmethod
    def _verify_database_manifest(
        cls,
        connection: Any,
        subject_id: str,
        limits: EventArchiveVerificationLimits,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> tuple[int, int]:
        segment_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM event_payload_segments WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()[0]
        )
        if checkpoint is not None:
            checkpoint()
        archived_event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE subject_id = ? "
                "AND payload_archive_key IS NOT NULL",
                (subject_id,),
            ).fetchone()[0]
        )
        if segment_count > limits.max_segments or archived_event_count > limits.max_events:
            raise PayloadLimitError("event archive exceeds the synchronous verification limit")
        missing_segments = int(
            connection.execute(
                "SELECT COUNT(*) FROM events e LEFT JOIN event_payload_segments s "
                "ON s.subject_id = e.subject_id AND s.object_key = e.payload_archive_key "
                "WHERE e.subject_id = ? AND e.payload_archive_key IS NOT NULL "
                "AND s.segment_id IS NULL",
                (subject_id,),
            ).fetchone()[0]
        )
        if missing_segments:
            raise IntegrityError("event archive segment metadata is missing")

        reconciled_events = 0
        reconciled_segments = 0
        segment_rows = connection.execute(
            "SELECT segment_id, object_key, first_occurred_at, last_occurred_at, "
            "event_count, compressed_hash, archive_format, encryption_key_id, created_at, "
            "encryption_key_fingerprint FROM event_payload_segments WHERE subject_id = ? "
            "ORDER BY created_at, segment_id",
            (subject_id,),
        )
        for segment in segment_rows:
            expected_count = cls._validate_verification_manifest(segment, limits)
            row_cursor = connection.execute(
                "SELECT event_id, occurred_at, payload_json, payload_hash, "
                "payload_archive_key, payload_archived_at FROM events "
                "WHERE subject_id = ? AND payload_archive_key = ? "
                "ORDER BY occurred_at, event_id",
                (subject_id, segment["object_key"]),
            )
            rows = row_cursor.fetchmany(MAX_EVENT_ARCHIVE_SEGMENT_EVENTS + 1)
            if expected_count != len(rows):
                raise IntegrityError("event archive manifest event count mismatch")
            if rows and (
                str(segment["first_occurred_at"]) != str(rows[0]["occurred_at"])
                or str(segment["last_occurred_at"]) != str(rows[-1]["occurred_at"])
            ):
                raise IntegrityError("event archive manifest time range mismatch")
            for row in rows:
                if row["payload_json"] != "{}":
                    raise IntegrityError("event archive tombstone metadata is invalid")
                if row["payload_archived_at"] is None:
                    raise IntegrityError("event archive timestamp metadata is missing")
                if str(row["payload_archived_at"]) != str(segment["created_at"]):
                    raise IntegrityError("event archive timestamp metadata is inconsistent")
            reconciled_events += len(rows)
            reconciled_segments += 1

        if reconciled_segments != segment_count or reconciled_events != archived_event_count:
            raise IntegrityError("event archive event reconciliation mismatch")
        return segment_count, archived_event_count

    def _verify_payload_integrity(
        self,
        subject_id: str,
        *,
        connection: Any,
        limits: EventArchiveVerificationLimits,
        segment_count: int,
        archived_event_count: int,
    ) -> EventArchiveVerification:
        verified_events = 0
        verified_segments = 0
        total_decompressed_bytes = 0
        segment_rows = connection.execute(
            "SELECT segment_id, object_key, first_occurred_at, last_occurred_at, "
            "event_count, compressed_hash, archive_format, encryption_key_id, created_at, "
            "encryption_key_fingerprint FROM event_payload_segments WHERE subject_id = ? "
            "ORDER BY created_at, segment_id",
            (subject_id,),
        )
        for segment in segment_rows:
            expected_count = self._validate_verification_manifest(segment, limits)
            loaded = self._load_segment(
                subject_id,
                str(segment["object_key"]),
                connection=connection,
                max_decompressed_bytes=limits.max_segment_decompressed_bytes,
                max_events=limits.max_events_per_segment,
                metadata=segment,
            )
            total_decompressed_bytes += loaded.decompressed_bytes
            if total_decompressed_bytes > limits.max_total_decompressed_bytes:
                raise PayloadLimitError(
                    "event archive exceeds the total synchronous verification byte limit"
                )
            row_cursor = connection.execute(
                "SELECT event_id, payload_hash FROM events WHERE subject_id = ? "
                "AND payload_archive_key = ? ORDER BY occurred_at, event_id",
                (subject_id, segment["object_key"]),
            )
            rows = row_cursor.fetchmany(MAX_EVENT_ARCHIVE_SEGMENT_EVENTS + 1)
            if expected_count != len(rows) or expected_count != len(loaded.payloads):
                raise IntegrityError("event archive manifest event count mismatch")
            row_ids: set[str] = set()
            for row in rows:
                event_id = str(row["event_id"])
                row_ids.add(event_id)
                payload = loaded.payloads.get(event_id)
                if payload is None:
                    raise IntegrityError("event is missing from archive segment")
                if content_hash(payload) != row["payload_hash"]:
                    raise IntegrityError("event archive payload hash mismatch")
            if set(loaded.payloads) != row_ids:
                raise IntegrityError("event archive contains an unexpected event")
            verified_events += len(rows)
            verified_segments += 1

        if verified_segments != segment_count or verified_events != archived_event_count:
            raise IntegrityError("event archive event reconciliation mismatch")
        return EventArchiveVerification(
            status="ok",
            segments=segment_count,
            events=archived_event_count,
            verified_segments=verified_segments,
            verified_events=verified_events,
        )

    def archive_cold(
        self, subject_id: str, *, older_than_days: int = 90, limit: int = 1_000
    ) -> int:
        if older_than_days < 1:
            raise ValueError("event archive retention must be at least one day")
        self.keyring.record_revision(self.database, subject_id)
        # Replay an interrupted provider-write/SQLite-finalize pair before
        # selecting new rows.  This keeps one source event from being moved
        # into multiple segments after a crash.
        self.reconcile_staging(subject_id, limit=8)
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(
            timespec="milliseconds"
        )
        bounded = max(1, min(limit, MAX_EVENT_ARCHIVE_SEGMENT_EVENTS))
        with self.database.connection() as connection:
            candidates = connection.execute(
                "SELECT event_id, length(CAST(payload_json AS BLOB)) AS payload_bytes "
                "FROM events WHERE subject_id = ? AND payload_archive_key IS NULL "
                "AND occurred_at < ? AND length(CAST(payload_json AS BLOB)) <= ? "
                "ORDER BY occurred_at, event_id LIMIT ?",
                (subject_id, cutoff, MAX_EVENT_ARCHIVE_SOURCE_BYTES, bounded),
            )
            rows: list[Any] = []
            source_bytes = 0
            for candidate in candidates:
                try:
                    payload_bytes = int(candidate["payload_bytes"])
                except (TypeError, ValueError) as error:
                    raise IntegrityError("event archive source payload size is invalid") from error
                if rows and source_bytes + payload_bytes > MAX_EVENT_ARCHIVE_SOURCE_BYTES:
                    break
                row = connection.execute(
                    "SELECT event_id, occurred_at, payload_json, payload_hash, "
                    "length(CAST(payload_json AS BLOB)) AS payload_bytes "
                    "FROM events WHERE subject_id = ? AND event_id = ? "
                    "AND payload_archive_key IS NULL AND occurred_at < ? "
                    "AND length(CAST(payload_json AS BLOB)) = ? "
                    "AND length(CAST(payload_json AS BLOB)) <= ?",
                    (
                        subject_id,
                        candidate["event_id"],
                        cutoff,
                        payload_bytes,
                        MAX_EVENT_ARCHIVE_SOURCE_BYTES - source_bytes,
                    ),
                ).fetchone()
                if row is None:
                    continue
                raw_payload = row["payload_json"]
                if not isinstance(raw_payload, str) or int(row["payload_bytes"]) != payload_bytes:
                    raise IntegrityError("event archive source changed during bounded read")
                rows.append(row)
                source_bytes += payload_bytes
        if not rows:
            return 0
        segment_id = new_id("event-segment")
        object_key = f"events/{segment_id}.json.zlib.enc"
        body = {
            "format": self.format,
            "subject_id": subject_id,
            "events": [
                {
                    "event_id": row["event_id"],
                    "payload_json": row["payload_json"],
                    "payload_hash": row["payload_hash"],
                }
                for row in rows
            ],
        }
        encoded = canonical_json(body).encode("utf-8")
        if len(encoded) > MAX_STAGED_EVENT_DECOMPRESSED_BYTES:
            raise PayloadLimitError("event archive staged payload exceeds the size limit")
        compressed = zlib.compress(encoded, level=9)
        source_state = [
            {
                "event_id": str(row["event_id"]),
                "occurred_at": str(row["occurred_at"]),
                "payload_hash": str(row["payload_hash"]),
            }
            for row in rows
        ]
        manifest_id = self.staging.prepare(
            subject_id,
            archive_kind="event_payload",
            segment_id=segment_id,
            object_key=object_key,
            source_state=source_state,
            item_count=len(rows),
            first_item_at=str(rows[0]["occurred_at"]),
            last_item_at=str(rows[-1]["occurred_at"]),
            plaintext_hash=hashlib.sha256(compressed).hexdigest(),
            archive_format=self.format,
            encryption_key_id=str(self.provider.key_id),
            encryption_key_fingerprint=str(self.provider.key_fingerprint),
        )
        # External provider calls stay outside the admission lock.  A stale
        # worker may leave a manifest/object pair, but it must never publish a
        # durable pointer after the epoch changes.
        assert_current_lease()
        digest = self.provider.put(object_key, compressed)
        restored = self.provider.get(object_key)
        if restored != compressed or hashlib.sha256(restored).hexdigest() != digest:
            raise IntegrityError("event payload archive verification failed")
        manifest = self.staging.get(manifest_id)
        if manifest is None:
            raise IntegrityError("event archive staging manifest is missing")
        self._verify_staged_payload(manifest, restored)
        stored_size, stored_hash = self.provider.stored_metadata(object_key)
        self.staging.mark_stored(
            manifest_id,
            byte_size=stored_size,
            stored_hash=stored_hash,
        )
        assert_current_lease()
        try:
            self._finalize_staged_manifest(manifest_id, subject_id)
        except _EventArchiveSourceConflict:
            if self.staging.mark_abandoned(manifest_id, "source_changed_during_finalize"):
                with suppress(OSError, ValueError):
                    self.provider.delete(object_key)
                self.staging.mark_removed(manifest_id)
            elif (current := self.staging.get(manifest_id)) is not None and str(
                current["status"]
            ) == "committed":
                return len(rows)
            raise IntegrityError("event changed during payload archival") from None
        return len(rows)

    def _finalize_staged_manifest(self, manifest_id: str, subject_id: str) -> int:
        manifest = self.staging.get(manifest_id)
        if manifest is None or str(manifest["subject_id"]) != subject_id:
            raise IntegrityError("event archive staging manifest is missing")
        if manifest["status"] == "committed":
            return int(manifest["item_count"])
        if manifest["status"] != "stored":
            raise IntegrityError("event archive staging manifest is not stored")
        try:
            source_state = strict_json_loads(str(manifest["source_state_json"]))
        except (TypeError, ValueError) as error:
            raise IntegrityError("event archive staging source state is invalid") from error
        if not isinstance(source_state, list) or len(source_state) != int(manifest["item_count"]):
            raise IntegrityError("event archive staging source state is invalid")
        if content_hash(source_state) != str(manifest["source_state_hash"]):
            raise IntegrityError("event archive staging source state hash mismatch")
        self._validate_staging_source_bounds(manifest, source_state)
        with current_commit_scope(), self.database.transaction() as connection:
            current_manifest = connection.execute(
                "SELECT * FROM archive_staging_manifests WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchone()
            if current_manifest is None or current_manifest["status"] != "stored":
                raise IntegrityError("event archive staging manifest changed")
            if content_hash(source_state) != str(current_manifest["source_state_hash"]):
                raise IntegrityError("event archive staging source state hash mismatch")
            for item in source_state:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"event_id", "occurred_at", "payload_hash"}
                    or not all(isinstance(item.get(field), str) for field in item)
                ):
                    raise IntegrityError("event archive staging source item is invalid")
                row = connection.execute(
                    "SELECT event_id, occurred_at, payload_hash, payload_archive_key, "
                    "payload_json FROM events WHERE subject_id = ? AND event_id = ?",
                    (subject_id, item.get("event_id")),
                ).fetchone()
                if row is None or row["payload_archive_key"] is not None:
                    raise _EventArchiveSourceConflict()
                if str(row["occurred_at"]) != str(item.get("occurred_at")) or str(
                    row["payload_hash"]
                ) != str(item.get("payload_hash")):
                    raise _EventArchiveSourceConflict()
                try:
                    payload = strict_json_loads(str(row["payload_json"]))
                except (TypeError, ValueError) as error:
                    raise IntegrityError("event archive source payload JSON is invalid") from error
                if not isinstance(payload, dict) or content_hash(payload) != row["payload_hash"]:
                    raise IntegrityError("event archive source payload hash mismatch")
            now = utc_now()
            for item in source_state:
                changed = connection.execute(
                    "UPDATE events SET payload_json = '{}', payload_archive_key = ?, "
                    "payload_archived_at = ? WHERE subject_id = ? AND event_id = ? "
                    "AND payload_archive_key IS NULL",
                    (current_manifest["object_key"], now, subject_id, item["event_id"]),
                )
                if changed.rowcount != 1:
                    raise _EventArchiveSourceConflict()
            connection.execute(
                "INSERT INTO event_payload_segments(segment_id, subject_id, object_key, "
                "first_occurred_at, last_occurred_at, event_count, compressed_hash, "
                "archive_format, encryption_key_id, encryption_key_fingerprint, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    current_manifest["segment_id"],
                    subject_id,
                    current_manifest["object_key"],
                    current_manifest["first_item_at"],
                    current_manifest["last_item_at"],
                    int(current_manifest["item_count"]),
                    current_manifest["plaintext_hash"],
                    current_manifest["archive_format"],
                    current_manifest["encryption_key_id"],
                    current_manifest["encryption_key_fingerprint"],
                    now,
                ),
            )
            self.replicas._ensure_connection(
                connection,
                subject_id,
                current_manifest["object_key"],
                replica_type="local",
                provider_id="local-cold-v1",
                ciphertext_hash=str(current_manifest["stored_hash"]),
                byte_size=int(current_manifest["stored_byte_size"]),
                state="present",
                reason="event archive segment finalized",
            )
            connection.execute(
                "UPDATE archive_staging_manifests SET status = 'committed', "
                "updated_at = ?, finalized_at = ? WHERE manifest_id = ? "
                "AND status = 'stored'",
                (now, now, manifest_id),
            )
        return int(manifest["item_count"])

    def reconcile_staging(self, subject_id: str, *, limit: int = 16) -> dict[str, int]:
        """Replay stored manifests and quarantine/delete incomplete objects."""
        bounded_limit = max(1, min(limit, 100))
        finalized = abandoned = 0
        examined = 0
        order_cursor: tuple[str, str] | None = None
        while examined < bounded_limit:
            with self.database.connection() as connection:
                if order_cursor is None:
                    row = connection.execute(
                        "SELECT * FROM archive_staging_manifests WHERE subject_id = ? "
                        "AND archive_kind = 'event_payload' AND status IN ('prepared','stored') "
                        "ORDER BY updated_at, manifest_id LIMIT 1",
                        (subject_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT * FROM archive_staging_manifests WHERE subject_id = ? "
                        "AND archive_kind = 'event_payload' AND status IN ('prepared','stored') "
                        "AND (updated_at > ? OR (updated_at = ? AND manifest_id > ?)) "
                        "ORDER BY updated_at, manifest_id LIMIT 1",
                        (subject_id, order_cursor[0], order_cursor[0], order_cursor[1]),
                    ).fetchone()
            if row is None:
                break
            examined += 1
            order_cursor = (str(row["updated_at"]), str(row["manifest_id"]))
            object_key = str(row["object_key"])
            provider = self._provider_for_manifest(row)
            error_code: str | None
            try:
                stored_size, stored_hash = provider.stored_metadata(
                    object_key,
                    max_bytes=MAX_STAGED_EVENT_DECOMPRESSED_BYTES + 1_000_000,
                )
                compressed = provider.get(object_key, max_bytes=max(1, stored_size))
                self._verify_staged_payload(row, compressed)
            except FileNotFoundError:
                error_code = "staged_object_missing"
            except PayloadLimitError:
                error_code = "staged_object_oversize"
            except IntegrityError as error:
                error_code = (
                    "staged_object_hash_mismatch"
                    if "hash" in str(error).lower()
                    else "staged_object_format_mismatch"
                )
            except (OSError, ValueError):
                error_code = "staged_object_unreadable"
            else:
                error_code = None
            if error_code is not None:
                manifest_id = str(row["manifest_id"])
                if self.staging.mark_abandoned(manifest_id, error_code):
                    with suppress(OSError, ValueError):
                        provider.delete(object_key)
                    self.staging.mark_removed(manifest_id)
                    abandoned += 1
                continue
            if str(row["status"]) == "prepared":
                try:
                    self.staging.mark_stored(
                        str(row["manifest_id"]), byte_size=stored_size, stored_hash=stored_hash
                    )
                except IntegrityError:
                    continue
            if stored_hash != str(row["stored_hash"] or stored_hash) or stored_size != int(
                row["stored_byte_size"] or stored_size
            ):
                manifest_id = str(row["manifest_id"])
                if self.staging.mark_abandoned(manifest_id, "staged_object_hash_mismatch"):
                    with suppress(OSError, ValueError):
                        provider.delete(object_key)
                    self.staging.mark_removed(manifest_id)
                    abandoned += 1
                continue
            try:
                self._finalize_staged_manifest(str(row["manifest_id"]), subject_id)
            except _EventArchiveSourceConflict:
                manifest_id = str(row["manifest_id"])
                if self.staging.mark_abandoned(manifest_id, "source_changed_during_replay"):
                    with suppress(OSError, ValueError):
                        provider.delete(object_key)
                    self.staging.mark_removed(manifest_id)
                    abandoned += 1
            except IntegrityError:
                # Keep a verified stored manifest for an operator/retry path;
                # only a proven source conflict is destructive.
                continue
            else:
                finalized += 1
        return {"finalized": finalized, "abandoned": abandoned}

    def _verify_staged_payload(self, manifest: Any, compressed: bytes) -> None:
        if hashlib.sha256(compressed).hexdigest() != str(manifest["plaintext_hash"]):
            raise IntegrityError("event archive staged plaintext hash mismatch")
        try:
            body = strict_json_loads(
                decompress_bytes(compressed, max_bytes=MAX_STAGED_EVENT_DECOMPRESSED_BYTES)
            )
            source_state = strict_json_loads(str(manifest["source_state_json"]))
        except (OSError, TypeError, ValueError, zlib.error) as error:
            raise IntegrityError("event archive staged payload is invalid") from error
        if (
            not isinstance(body, dict)
            or str(manifest["archive_format"]) != self.format
            or body.get("format") != self.format
            or body.get("subject_id") != str(manifest["subject_id"])
            or not isinstance(source_state, list)
        ):
            raise IntegrityError("event archive staged payload metadata mismatch")
        if content_hash(source_state) != str(manifest["source_state_hash"]):
            raise IntegrityError("event archive staged source state hash mismatch")
        self._validate_staging_source_bounds(manifest, source_state)
        entries = body.get("events")
        if not isinstance(entries, list) or len(source_state) != int(manifest["item_count"]):
            raise IntegrityError("event archive staged entries mismatch")
        expected: dict[str, str] = {}
        for item in source_state:
            if (
                not isinstance(item, dict)
                or set(item) != {"event_id", "occurred_at", "payload_hash"}
                or not all(isinstance(item.get(field), str) for field in item)
            ):
                raise IntegrityError("event archive staged source item is invalid")
            event_id = str(item["event_id"])
            if event_id in expected:
                raise IntegrityError("event archive staged source IDs are duplicated")
            expected[event_id] = str(item["payload_hash"])
        actual: dict[str, str] = {}
        for item in entries:
            if (
                not isinstance(item, dict)
                or set(item) != {"event_id", "payload_json", "payload_hash"}
                or not all(isinstance(item.get(field), str) for field in item)
            ):
                raise IntegrityError("event archive staged entry is invalid")
            event_id = str(item["event_id"])
            payload_hash = str(item["payload_hash"])
            if event_id in actual:
                raise IntegrityError("event archive staged entries are duplicated")
            try:
                payload = strict_json_loads(str(item["payload_json"]))
            except (TypeError, ValueError) as error:
                raise IntegrityError("event archive staged payload JSON is invalid") from error
            if not isinstance(payload, dict) or content_hash(payload) != payload_hash:
                raise IntegrityError("event archive staged payload hash mismatch")
            actual[event_id] = payload_hash
        if actual != expected:
            raise IntegrityError("event archive staged entries mismatch")

    def _validate_staging_source_bounds(self, manifest: Any, source_state: list[Any]) -> None:
        if str(manifest["archive_format"]) != self.format or not source_state:
            raise IntegrityError("event archive staged format is unsupported")
        identifiers: set[str] = set()
        occurred_at: list[str] = []
        for item in source_state:
            if (
                not isinstance(item, dict)
                or set(item) != {"event_id", "occurred_at", "payload_hash"}
                or not all(isinstance(item.get(field), str) for field in item)
            ):
                raise IntegrityError("event archive staged source item is invalid")
            identifier = str(item["event_id"])
            if identifier in identifiers:
                raise IntegrityError("event archive staged source IDs are duplicated")
            identifiers.add(identifier)
            occurred_at.append(str(item["occurred_at"]))
        if occurred_at != sorted(occurred_at) or (
            str(manifest["first_item_at"]) != occurred_at[0]
            or str(manifest["last_item_at"]) != occurred_at[-1]
        ):
            raise IntegrityError("event archive staged time bounds mismatch")

    def _provider_for_manifest(self, row: Any) -> LocalArchiveProvider:
        return self.keyring.provider(
            self.root,
            key_id=str(row["encryption_key_id"]),
            fingerprint=str(row["encryption_key_fingerprint"]),
            create_root=False,
        )

    def gc_orphans(self, subject_id: str, *, grace_seconds: int = 3_600, limit: int = 100) -> int:
        del subject_id
        cutoff = datetime.now(UTC).timestamp() - max(0, grace_seconds)
        directory = self.root / "events"
        if not directory.is_dir():
            return 0
        removed = 0
        for path in sorted(directory.iterdir()):
            if removed >= max(1, min(limit, 1_000)):
                break
            try:
                metadata = path.lstat()
                if not path.is_file() or path.is_symlink() or metadata.st_mtime > cutoff:
                    continue
            except OSError:
                continue
            temporary = path.name.endswith(".tmp") and path.name.startswith(
                (".awrite_", ".arestore_")
            )
            if temporary:
                with suppress(OSError):
                    path.unlink()
                    removed += 1
                continue
            if not path.name.endswith(".json.zlib.enc"):
                continue
            object_key = path.relative_to(self.root).as_posix()
            if self.staging.referenced(object_key):
                continue
            with suppress(OSError, ValueError):
                self.provider.delete(object_key)
                removed += 1
        return removed

    def load_segment(
        self,
        subject_id: str,
        object_key: str,
        *,
        connection: Any | None = None,
        max_decompressed_bytes: int | None = None,
        max_events: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        if connection is None:
            with self.database.connection() as owned_connection:
                return self.load_segment(
                    subject_id,
                    object_key,
                    connection=owned_connection,
                    max_decompressed_bytes=max_decompressed_bytes,
                    max_events=max_events,
                )
        self.keyring.validate_revision(self.database, subject_id, connection=connection)
        return self._load_segment(
            subject_id,
            object_key,
            connection=connection,
            max_decompressed_bytes=max_decompressed_bytes,
            max_events=max_events,
        ).payloads

    def _load_segment(
        self,
        subject_id: str,
        object_key: str,
        *,
        connection: Any,
        max_decompressed_bytes: int | None,
        max_events: int | None,
        metadata: Any | None = None,
    ) -> _LoadedEventArchiveSegment:
        row = metadata
        if row is None:
            row = connection.execute(
                "SELECT event_count, compressed_hash, archive_format, encryption_key_id, "
                "encryption_key_fingerprint FROM event_payload_segments "
                "WHERE subject_id = ? AND object_key = ?",
                (subject_id, object_key),
            ).fetchone()
        if row is None:
            raise IntegrityError("event payload archive metadata is missing")
        if row["archive_format"] not in {None, self.format}:
            raise IntegrityError("event payload archive format is unsupported")
        stored_limit = (
            None
            if max_decompressed_bytes is None
            else max_decompressed_bytes + 1_024 + max_decompressed_bytes // 1_000
        )
        provider = self._segment_provider(row, object_key, stored_limit=stored_limit)
        try:
            compressed = provider.get(
                object_key,
                max_bytes=stored_limit,
            )
        except FileNotFoundError:
            try:
                compressed = ArchiveReplicaResolver(
                    self.database,
                    subject_id,
                    cloud_provider=self.cloud_provider,
                ).restore_local(
                    object_key,
                    provider,
                    cache=self.cache_cloud_restores,
                    expected_plaintext_hash=str(row["compressed_hash"]),
                    max_plaintext_bytes=stored_limit,
                )
            except ArchiveUnavailableError:
                raise
            except IntegrityError:
                raise
            except Exception as restore_error:
                raise ArchiveUnavailableError(
                    "event payload archive cloud restore failed"
                ) from restore_error
        except (PermissionError, OSError) as error:
            raise ArchiveUnavailableError("event payload archive object is unavailable") from error
        except ValueError as error:
            raise IntegrityError("event payload archive object key is invalid") from error
        if hashlib.sha256(compressed).hexdigest() != row["compressed_hash"]:
            raise IntegrityError("event payload archive hash mismatch")
        try:
            raw = (
                zlib.decompress(compressed)
                if max_decompressed_bytes is None
                else decompress_bytes(compressed, max_bytes=max_decompressed_bytes)
            )
        except zlib.error as error:
            raise IntegrityError("event payload archive is unreadable") from error
        try:
            body = json.loads(raw)
        except PayloadLimitError:
            raise
        except (OSError, RecursionError, ValueError, zlib.error) as error:
            raise IntegrityError("event payload archive is unreadable") from error
        if (
            not isinstance(body, dict)
            or body.get("format") != self.format
            or body.get("subject_id") != subject_id
        ):
            raise IntegrityError("event payload archive subject mismatch")
        events = body.get("events")
        if not isinstance(events, list):
            raise IntegrityError("event payload archive entries are invalid")
        declared_count = row["event_count"]
        if type(declared_count) is not int or declared_count != len(events):
            raise IntegrityError("event payload archive manifest event count mismatch")
        if len(events) > MAX_EVENT_ARCHIVE_SEGMENT_EVENTS:
            raise IntegrityError("event payload archive contains too many events")
        if max_events is not None and len(events) > max_events:
            raise PayloadLimitError("event payload archive exceeds the verification event limit")
        result: dict[str, dict[str, Any]] = {}
        for item in events:
            if not isinstance(item, dict):
                raise IntegrityError("event payload archive entry is invalid")
            event_id = item.get("event_id")
            payload_json = item.get("payload_json")
            payload_hash = item.get("payload_hash")
            if not all(isinstance(value, str) for value in (event_id, payload_json, payload_hash)):
                raise IntegrityError("event payload archive entry fields are invalid")
            event_key = str(event_id)
            raw_payload = str(payload_json)
            expected_hash = str(payload_hash)
            try:
                payload = json.loads(raw_payload)
            except (RecursionError, TypeError, ValueError) as error:
                raise IntegrityError("event payload archive entry is invalid") from error
            try:
                valid_hash = isinstance(payload, dict) and content_hash(payload) == expected_hash
            except (RecursionError, TypeError, ValueError) as error:
                raise IntegrityError("event payload archive entry is invalid") from error
            if not valid_hash:
                raise IntegrityError("event payload archive entry hash mismatch")
            if event_key in result:
                raise IntegrityError("event payload archive contains duplicate event")
            result[event_key] = payload
        return _LoadedEventArchiveSegment(result, len(raw))

    def _segment_provider(
        self,
        row: Any,
        object_key: str,
        *,
        stored_limit: int | None,
    ) -> LocalArchiveProvider:
        try:
            return self.keyring.provider(
                self.root,
                key_id=row["encryption_key_id"],
                fingerprint=row["encryption_key_fingerprint"],
                create_root=False,
            )
        except ArchiveKeyUnavailableError as unavailable:
            material = self.keyring.material_for_key_id(row["encryption_key_id"])
            if material.fingerprint == row["encryption_key_fingerprint"]:
                raise
            probe = LocalArchiveProvider(
                self.root,
                encryption_key=material.key,
                key_id=material.key_id,
                create_root=False,
            )
            try:
                probe.get(object_key, max_bytes=stored_limit)
            except (FileNotFoundError, ArchiveAuthenticationError):
                raise unavailable from None
            except (PermissionError, OSError) as error:
                raise ArchiveUnavailableError(
                    "event payload archive object is unavailable"
                ) from error
            raise IntegrityError(
                "event archive key fingerprint metadata is inconsistent"
            ) from unavailable

    @staticmethod
    def _validate_verification_manifest(
        row: Any,
        limits: EventArchiveVerificationLimits,
    ) -> int:
        segment_id = row["segment_id"]
        object_key = row["object_key"]
        if not isinstance(segment_id, str) or not segment_id.strip():
            raise IntegrityError("event archive segment ID is invalid")
        if object_key != f"events/{segment_id}.json.zlib.enc":
            raise IntegrityError("event archive segment object key is invalid")
        archive_format = row["archive_format"]
        key_id = row["encryption_key_id"]
        fingerprint = row["encryption_key_fingerprint"]
        metadata = (archive_format, key_id, fingerprint)
        if all(value is None for value in metadata):
            pass
        elif any(value is None for value in metadata):
            raise IntegrityError("event archive verification metadata is incomplete")
        elif archive_format != EVENT_ARCHIVE_FORMAT:
            raise IntegrityError("event payload archive format is unsupported")
        if key_id is not None and (not isinstance(key_id, str) or not key_id.strip()):
            raise IntegrityError("event archive key ID metadata is invalid")
        if fingerprint is not None and (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise IntegrityError("event archive key fingerprint metadata is invalid")
        expected_count = row["event_count"]
        if type(expected_count) is not int:
            raise IntegrityError("event archive manifest event count is invalid")
        if expected_count < 1 or expected_count > MAX_EVENT_ARCHIVE_SEGMENT_EVENTS:
            raise IntegrityError("event archive manifest event count is invalid")
        if expected_count > limits.max_events_per_segment:
            raise PayloadLimitError("event archive exceeds the verification event limit")
        if str(row["first_occurred_at"]) > str(row["last_occurred_at"]):
            raise IntegrityError("event archive manifest time range is invalid")
        try:
            created_at = datetime.fromisoformat(str(row["created_at"]))
        except ValueError as error:
            raise IntegrityError("event archive creation timestamp is invalid") from error
        if created_at.tzinfo is None:
            raise IntegrityError("event archive creation timestamp is invalid")
        return expected_count
