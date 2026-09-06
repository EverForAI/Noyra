from __future__ import annotations

import hashlib
import json
import zlib
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from noyra.core.admission import assert_current_lease, current_commit_scope
from noyra.core.archive import (
    ArchiveKeyring,
    ArchiveProvider,
    ArchiveReplicaLedger,
    ArchiveReplicaResolver,
    ArchiveStagingLedger,
    LocalArchiveProvider,
    resolve_subject_archive_root,
)
from noyra.core.database import Database
from noyra.core.errors import (
    ArchiveUnavailableError,
    IntegrityError,
    PayloadLimitError,
)
from noyra.core.payload_codec import decompress_bytes
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_int,
    strict_json_loads,
    utc_now,
)


class _ObservationArchiveSourceConflict(IntegrityError):
    """Selected observation rows changed before staged publication."""


class ObservationContentArchive:
    """Encrypted cold segments for large fetched observation bodies."""

    format = "noyra-observation-content-segment-v1"
    max_staged_decompressed_bytes = 64_000_000
    max_source_bytes = max_staged_decompressed_bytes - 2_000_000

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

    def archive_cold(self, subject_id: str, *, older_than_days: int = 90, limit: int = 200) -> int:
        if older_than_days < 1:
            raise ValueError("observation archive retention must be at least one day")
        self.keyring.record_revision(self.database, subject_id)
        self.reconcile_staging(subject_id, limit=8)
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(
            timespec="milliseconds"
        )
        bounded = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            candidates = connection.execute(
                "SELECT observation_id, length(CAST(content AS BLOB)) AS content_bytes "
                "FROM observations WHERE subject_id = ? AND content_archive_key IS NULL "
                "AND fetched_at < ? AND length(CAST(content AS BLOB)) <= ? "
                "ORDER BY fetched_at, observation_id LIMIT ?",
                (subject_id, cutoff, self.max_source_bytes, bounded),
            ).fetchall()
            rows: list[Any] = []
            source_bytes = 0
            for candidate in candidates:
                try:
                    content_bytes = int(candidate["content_bytes"])
                except (TypeError, ValueError) as error:
                    raise IntegrityError(
                        "observation archive source content size is invalid"
                    ) from error
                if rows and source_bytes + content_bytes > self.max_source_bytes:
                    break
                row = connection.execute(
                    "SELECT observation_id, fetched_at, content, content_hash, "
                    "length(CAST(content AS BLOB)) AS content_bytes "
                    "FROM observations WHERE subject_id = ? AND observation_id = ? "
                    "AND content_archive_key IS NULL AND fetched_at < ? "
                    "AND length(CAST(content AS BLOB)) = ? "
                    "AND length(CAST(content AS BLOB)) <= ?",
                    (
                        subject_id,
                        candidate["observation_id"],
                        cutoff,
                        content_bytes,
                        self.max_source_bytes - source_bytes,
                    ),
                ).fetchone()
                if row is None:
                    continue
                raw_content = row["content"]
                if not isinstance(raw_content, str) or int(row["content_bytes"]) != content_bytes:
                    raise IntegrityError("observation archive source changed during bounded read")
                rows.append(row)
                source_bytes += content_bytes
        if not rows:
            return 0
        segment_id = new_id("observation-segment")
        object_key = f"observations/{segment_id}.json.zlib.enc"
        body = {
            "format": self.format,
            "subject_id": subject_id,
            "observations": [
                {
                    "observation_id": row["observation_id"],
                    "content": row["content"],
                    "content_hash": row["content_hash"],
                }
                for row in rows
            ],
        }
        encoded = canonical_json(body).encode("utf-8")
        if len(encoded) > self.max_staged_decompressed_bytes:
            raise PayloadLimitError("observation archive staged payload exceeds the size limit")
        compressed = zlib.compress(encoded, level=9)
        source_state = [
            {
                "observation_id": str(row["observation_id"]),
                "fetched_at": str(row["fetched_at"]),
                "content_hash": str(row["content_hash"]),
            }
            for row in rows
        ]
        manifest_id = self.staging.prepare(
            subject_id,
            archive_kind="observation_content",
            segment_id=segment_id,
            object_key=object_key,
            source_state=source_state,
            item_count=len(rows),
            first_item_at=str(rows[0]["fetched_at"]),
            last_item_at=str(rows[-1]["fetched_at"]),
            plaintext_hash=hashlib.sha256(compressed).hexdigest(),
            archive_format=self.format,
            encryption_key_id=str(self.provider.key_id),
            encryption_key_fingerprint=str(self.provider.key_fingerprint),
        )
        assert_current_lease()
        digest = self.provider.put(object_key, compressed)
        restored = self.provider.get(object_key)
        if restored != compressed or hashlib.sha256(restored).hexdigest() != digest:
            raise IntegrityError("observation archive verification failed")
        manifest = self.staging.get(manifest_id)
        if manifest is None:
            raise IntegrityError("observation archive staging manifest is missing")
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
        except _ObservationArchiveSourceConflict:
            if self.staging.mark_abandoned(manifest_id, "source_changed_during_finalize"):
                with suppress(OSError, ValueError):
                    self.provider.delete(object_key)
                self.staging.mark_removed(manifest_id)
            elif (current := self.staging.get(manifest_id)) is not None and str(
                current["status"]
            ) == "committed":
                return len(rows)
            raise IntegrityError("observation changed during content archival") from None
        return len(rows)

    def _finalize_staged_manifest(self, manifest_id: str, subject_id: str) -> int:
        manifest = self.staging.get(manifest_id)
        if manifest is None or str(manifest["subject_id"]) != subject_id:
            raise IntegrityError("observation archive staging manifest is missing")
        if manifest["status"] == "committed":
            return int(manifest["item_count"])
        if manifest["status"] != "stored":
            raise IntegrityError("observation archive staging manifest is not stored")
        try:
            source_state = json.loads(str(manifest["source_state_json"]))
        except (TypeError, ValueError) as error:
            raise IntegrityError("observation archive staging source state is invalid") from error
        if not isinstance(source_state, list) or len(source_state) != int(manifest["item_count"]):
            raise IntegrityError("observation archive staging source state is invalid")
        if content_hash(source_state) != str(manifest["source_state_hash"]):
            raise IntegrityError("observation archive staging source state hash mismatch")
        self._validate_staging_source_bounds(manifest, source_state)
        with current_commit_scope(), self.database.transaction() as connection:
            current_manifest = connection.execute(
                "SELECT * FROM archive_staging_manifests WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchone()
            if current_manifest is None or current_manifest["status"] != "stored":
                raise IntegrityError("observation archive staging manifest changed")
            if content_hash(source_state) != str(current_manifest["source_state_hash"]):
                raise IntegrityError("observation archive staging source state hash mismatch")
            for item in source_state:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"observation_id", "fetched_at", "content_hash"}
                    or not all(isinstance(item.get(field), str) for field in item)
                ):
                    raise IntegrityError("observation archive staging source item is invalid")
                row = connection.execute(
                    "SELECT observation_id, fetched_at, content_hash, content_archive_key, "
                    "content FROM observations WHERE subject_id = ? AND observation_id = ?",
                    (subject_id, item.get("observation_id")),
                ).fetchone()
                if row is None or row["content_archive_key"] is not None:
                    raise _ObservationArchiveSourceConflict()
                if str(row["fetched_at"]) != str(item.get("fetched_at")) or str(
                    row["content_hash"]
                ) != str(item.get("content_hash")):
                    raise _ObservationArchiveSourceConflict()
                if (
                    not isinstance(row["content"], str)
                    or content_hash(row["content"]) != row["content_hash"]
                ):
                    raise IntegrityError("observation archive source content hash mismatch")
            now = utc_now()
            for item in source_state:
                changed = connection.execute(
                    "UPDATE observations SET content = '', content_archive_key = ?, "
                    "content_archived_at = ? WHERE subject_id = ? AND observation_id = ? "
                    "AND content_archive_key IS NULL",
                    (
                        current_manifest["object_key"],
                        now,
                        subject_id,
                        item["observation_id"],
                    ),
                )
                if changed.rowcount != 1:
                    raise _ObservationArchiveSourceConflict()
            connection.execute(
                """INSERT INTO observation_content_segments(
                        segment_id, subject_id, object_key, first_fetched_at, last_fetched_at,
                        observation_count, compressed_hash, archive_format, encryption_key_id,
                        encryption_key_fingerprint, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                reason="observation archive segment finalized",
            )
            connection.execute(
                "UPDATE archive_staging_manifests SET status = 'committed', "
                "updated_at = ?, finalized_at = ? WHERE manifest_id = ? "
                "AND status = 'stored'",
                (now, now, manifest_id),
            )
        return int(manifest["item_count"])

    def reconcile_staging(self, subject_id: str, *, limit: int = 16) -> dict[str, int]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM archive_staging_manifests WHERE subject_id = ? "
                "AND archive_kind = 'observation_content' AND status IN ('prepared','stored') "
                "ORDER BY updated_at, manifest_id LIMIT ?",
                (subject_id, max(1, min(limit, 100))),
            ).fetchall()
        finalized = abandoned = 0
        for row in rows:
            object_key = str(row["object_key"])
            provider = self._provider_for_manifest(row)
            error_code: str | None
            try:
                stored_size, stored_hash = provider.stored_metadata(
                    object_key,
                    max_bytes=self.max_staged_decompressed_bytes + 1_000_000,
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
            except _ObservationArchiveSourceConflict:
                manifest_id = str(row["manifest_id"])
                if self.staging.mark_abandoned(manifest_id, "source_changed_during_replay"):
                    with suppress(OSError, ValueError):
                        provider.delete(object_key)
                    self.staging.mark_removed(manifest_id)
                    abandoned += 1
            except IntegrityError:
                continue
            else:
                finalized += 1
        return {"finalized": finalized, "abandoned": abandoned}

    def _verify_staged_payload(self, manifest: Any, compressed: bytes) -> None:
        if hashlib.sha256(compressed).hexdigest() != str(manifest["plaintext_hash"]):
            raise IntegrityError("observation archive staged plaintext hash mismatch")
        try:
            body = strict_json_loads(
                decompress_bytes(compressed, max_bytes=self.max_staged_decompressed_bytes)
            )
            source_state = strict_json_loads(str(manifest["source_state_json"]))
        except (OSError, TypeError, ValueError, zlib.error) as error:
            raise IntegrityError("observation archive staged payload is invalid") from error
        if (
            not isinstance(body, dict)
            or str(manifest["archive_format"]) != self.format
            or body.get("format") != self.format
            or body.get("subject_id") != str(manifest["subject_id"])
            or not isinstance(source_state, list)
        ):
            raise IntegrityError("observation archive staged payload metadata mismatch")
        if content_hash(source_state) != str(manifest["source_state_hash"]):
            raise IntegrityError("observation archive staged source state hash mismatch")
        self._validate_staging_source_bounds(manifest, source_state)
        entries = body.get("observations")
        if not isinstance(entries, list) or len(source_state) != int(manifest["item_count"]):
            raise IntegrityError("observation archive staged entries mismatch")
        expected: dict[str, str] = {}
        for item in source_state:
            if (
                not isinstance(item, dict)
                or set(item) != {"observation_id", "fetched_at", "content_hash"}
                or not all(isinstance(item.get(field), str) for field in item)
            ):
                raise IntegrityError("observation archive staged source item is invalid")
            observation_id = str(item["observation_id"])
            if observation_id in expected:
                raise IntegrityError("observation archive staged source IDs are duplicated")
            expected[observation_id] = str(item["content_hash"])
        actual: dict[str, str] = {}
        for item in entries:
            if (
                not isinstance(item, dict)
                or set(item) != {"observation_id", "content", "content_hash"}
                or not isinstance(item.get("observation_id"), str)
                or not isinstance(item.get("content"), str)
                or not isinstance(item.get("content_hash"), str)
            ):
                raise IntegrityError("observation archive staged entry is invalid")
            observation_id = str(item["observation_id"])
            expected_hash = str(item["content_hash"])
            if observation_id in actual:
                raise IntegrityError("observation archive staged entries are duplicated")
            if content_hash(item["content"]) != expected_hash:
                raise IntegrityError("observation archive staged content hash mismatch")
            actual[observation_id] = expected_hash
        if actual != expected:
            raise IntegrityError("observation archive staged entries mismatch")

    def _validate_staging_source_bounds(self, manifest: Any, source_state: list[Any]) -> None:
        if str(manifest["archive_format"]) != self.format or not source_state:
            raise IntegrityError("observation archive staged format is unsupported")
        identifiers: set[str] = set()
        fetched_at: list[str] = []
        for item in source_state:
            if (
                not isinstance(item, dict)
                or set(item) != {"observation_id", "fetched_at", "content_hash"}
                or not all(isinstance(item.get(field), str) for field in item)
            ):
                raise IntegrityError("observation archive staged source item is invalid")
            identifier = str(item["observation_id"])
            if identifier in identifiers:
                raise IntegrityError("observation archive staged source IDs are duplicated")
            identifiers.add(identifier)
            fetched_at.append(str(item["fetched_at"]))
        if fetched_at != sorted(fetched_at) or (
            str(manifest["first_item_at"]) != fetched_at[0]
            or str(manifest["last_item_at"]) != fetched_at[-1]
        ):
            raise IntegrityError("observation archive staged time bounds mismatch")

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
        directory = self.root / "observations"
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

    def load_content(
        self,
        subject_id: str,
        object_key: str,
        observation_id: str,
        *,
        connection: Any | None = None,
        max_compressed_bytes: int = 16_000_000,
        max_decompressed_bytes: int = 64_000_000,
    ) -> str:
        if max_compressed_bytes < 1 or max_decompressed_bytes < 1:
            raise ValueError("observation archive read limits must be positive")
        if connection is None:
            with self.database.connection() as owned_connection:
                return self.load_content(
                    subject_id,
                    object_key,
                    observation_id,
                    connection=owned_connection,
                    max_compressed_bytes=max_compressed_bytes,
                    max_decompressed_bytes=max_decompressed_bytes,
                )
        self.keyring.validate_revision(self.database, subject_id, connection=connection)
        row = connection.execute(
            "SELECT compressed_hash, archive_format, encryption_key_id, "
            "encryption_key_fingerprint FROM observation_content_segments "
            "WHERE subject_id = ? AND object_key = ?",
            (subject_id, object_key),
        ).fetchone()
        if row is None:
            raise IntegrityError("observation archive metadata is missing")
        if row["archive_format"] not in {None, self.format}:
            raise IntegrityError("observation archive format is unsupported")
        provider = self.keyring.provider(
            self.root,
            key_id=row["encryption_key_id"],
            fingerprint=row["encryption_key_fingerprint"],
            create_root=False,
        )
        try:
            compressed = provider.get(object_key, max_bytes=max_compressed_bytes)
        except FileNotFoundError:
            compressed = ArchiveReplicaResolver(
                self.database,
                subject_id,
                cloud_provider=self.cloud_provider,
            ).restore_local(
                object_key,
                provider,
                cache=self.cache_cloud_restores,
                expected_plaintext_hash=str(row["compressed_hash"]),
                max_plaintext_bytes=max_compressed_bytes,
            )
        except (PermissionError, OSError) as error:
            raise ArchiveUnavailableError("observation archive object is unavailable") from error
        if hashlib.sha256(compressed).hexdigest() != row["compressed_hash"]:
            raise IntegrityError("observation archive hash mismatch")
        try:
            body = json.loads(self._decompress_bounded(compressed, max_decompressed_bytes))
        except (OSError, ValueError, zlib.error) as error:
            raise IntegrityError("observation archive is unreadable") from error
        if not isinstance(body, dict) or body.get("subject_id") != subject_id:
            raise IntegrityError("observation archive subject mismatch")
        entries = body.get("observations")
        if not isinstance(entries, list):
            raise IntegrityError("observation archive entries are invalid")
        for item in entries:
            if not isinstance(item, dict):
                raise IntegrityError("observation archive entry is invalid")
            if item.get("observation_id") != observation_id:
                continue
            content = item.get("content")
            expected_hash = item.get("content_hash")
            if not isinstance(content, str) or not isinstance(expected_hash, str):
                raise IntegrityError("observation archive entry fields are invalid")
            if content_hash(content) != expected_hash:
                raise IntegrityError("observation archive content hash mismatch")
            return content
        raise IntegrityError("observation is missing from archive segment")

    def verify_integrity(
        self,
        subject_id: str,
        *,
        connection: Any | None = None,
        max_object_bytes: int = 16_000_000,
        max_total_bytes: int = 64_000_000,
        checkpoint: Callable[[], None] | None = None,
        consume_bytes: Callable[[int], None] | None = None,
    ) -> tuple[int, dict[str, str]]:
        """Verify every owned segment and return materialized observation content."""
        if max_object_bytes < 1 or max_total_bytes < 1:
            raise ValueError("observation archive verification limits must be positive")
        if connection is None:
            with self.database.read_transaction() as owned_connection:
                return self.verify_integrity(
                    subject_id,
                    connection=owned_connection,
                    max_object_bytes=max_object_bytes,
                    max_total_bytes=max_total_bytes,
                    checkpoint=checkpoint,
                    consume_bytes=consume_bytes,
                )

        self.keyring.validate_revision(self.database, subject_id, connection=connection)

        segments = list(
            connection.execute(
                """SELECT s.*, s.rowid AS storage_rowid
                   FROM observation_content_segments s
                   WHERE s.subject_id = ? OR EXISTS (
                       SELECT 1 FROM observations o
                       WHERE o.subject_id = ? AND o.content_archive_key = s.object_key
                   )
                   ORDER BY s.rowid""",
                (subject_id, subject_id),
            )
        )
        archived_rows = list(
            connection.execute(
                "SELECT observation_id, subject_id, content, content_hash, fetched_at, "
                "content_archive_key, content_archived_at FROM observations "
                "WHERE subject_id = ? AND content_archive_key IS NOT NULL ORDER BY rowid",
                (subject_id,),
            )
        )
        expected_keys = {str(row["content_archive_key"]) for row in archived_rows}
        segment_keys: set[str] = set()
        contents: dict[str, str] = {}
        consumed = 0

        for segment in segments:
            if checkpoint is not None:
                checkpoint()
            segment_id = self._required_text(segment["segment_id"], "observation segment id")
            row_subject = self._required_text(
                segment["subject_id"], f"observation segment {segment_id} subject"
            )
            object_key = self._required_text(
                segment["object_key"], f"observation segment {segment_id} object key"
            )
            first_fetched_at = self._timestamp(
                segment["first_fetched_at"], f"observation segment {segment_id} first fetch"
            )
            last_fetched_at = self._timestamp(
                segment["last_fetched_at"], f"observation segment {segment_id} last fetch"
            )
            created_at = self._timestamp(
                segment["created_at"], f"observation segment {segment_id} created time"
            )
            try:
                observation_count = strict_int(segment["observation_count"])
            except (TypeError, ValueError) as error:
                raise IntegrityError(
                    f"observation segment count is invalid: {segment_id}"
                ) from error
            compressed_hash = self._digest(
                segment["compressed_hash"], f"observation segment {segment_id} compressed hash"
            )
            archive_format = segment["archive_format"]
            key_id = segment["encryption_key_id"]
            key_fingerprint = segment["encryption_key_fingerprint"]
            if (
                row_subject != subject_id
                or object_key != f"observations/{segment_id}.json.zlib.enc"
                or observation_count < 1
                or first_fetched_at > last_fetched_at
                or last_fetched_at > created_at
                or archive_format not in {None, self.format}
                or (key_id is not None and (not isinstance(key_id, str) or not key_id.strip()))
                or (
                    key_fingerprint is not None
                    and self._digest(
                        key_fingerprint,
                        f"observation segment {segment_id} key fingerprint",
                    )
                    != key_fingerprint
                )
            ):
                raise IntegrityError(f"observation segment metadata is invalid: {segment_id}")
            provider = self.keyring.provider(
                self.root,
                key_id=key_id,
                fingerprint=key_fingerprint,
                create_root=False,
            )
            owners = connection.execute(
                "SELECT subject_id FROM observation_content_segments WHERE object_key = ?",
                (object_key,),
            ).fetchall()
            if len(owners) != 1 or owners[0]["subject_id"] != subject_id:
                raise IntegrityError(f"observation segment object ownership mismatch: {segment_id}")
            rows = list(
                connection.execute(
                    "SELECT observation_id, subject_id, content, content_hash, fetched_at, "
                    "content_archived_at FROM observations WHERE content_archive_key = ? "
                    "ORDER BY rowid",
                    (object_key,),
                )
            )
            if len(rows) != observation_count:
                raise IntegrityError(f"observation segment count mismatch: {segment_id}")

            remaining = max_total_bytes - consumed
            if remaining < 1:
                raise PayloadLimitError("observation archive verification exceeds its total limit")
            try:
                compressed = provider.get(
                    object_key,
                    max_bytes=min(max_object_bytes, remaining),
                )
            except FileNotFoundError:
                compressed = ArchiveReplicaResolver(
                    self.database,
                    subject_id,
                    cloud_provider=self.cloud_provider,
                ).restore_local(
                    object_key,
                    provider,
                    cache=self.cache_cloud_restores,
                    expected_plaintext_hash=compressed_hash,
                    max_plaintext_bytes=min(max_object_bytes, remaining),
                )
            consumed += len(compressed)
            if consume_bytes is not None:
                consume_bytes(len(compressed))
            if hashlib.sha256(compressed).hexdigest() != compressed_hash:
                raise IntegrityError(f"observation segment hash mismatch: {segment_id}")
            remaining = max_total_bytes - consumed
            decoded = self._decompress_bounded(compressed, min(max_object_bytes, remaining))
            consumed += len(decoded)
            if consume_bytes is not None:
                consume_bytes(len(decoded))
            try:
                body = json.loads(decoded)
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"observation segment is unreadable: {segment_id}") from error
            if (
                not isinstance(body, dict)
                or body.get("format") != self.format
                or body.get("subject_id") != subject_id
                or not isinstance(body.get("observations"), list)
            ):
                raise IntegrityError(f"observation segment body is invalid: {segment_id}")

            entries: dict[str, tuple[str, str]] = {}
            for item in body["observations"]:
                if not isinstance(item, dict) or set(item) != {
                    "observation_id",
                    "content",
                    "content_hash",
                }:
                    raise IntegrityError(f"observation segment entry is invalid: {segment_id}")
                observation_id = self._required_text(
                    item["observation_id"], f"observation segment {segment_id} observation id"
                )
                content = item["content"]
                expected_hash = self._digest(
                    item["content_hash"],
                    f"observation segment {segment_id} content hash",
                )
                if (
                    not isinstance(content, str)
                    or content_hash(content) != expected_hash
                    or observation_id in entries
                ):
                    raise IntegrityError(f"observation segment content is invalid: {segment_id}")
                entries[observation_id] = (content, expected_hash)

            if len(entries) != observation_count:
                raise IntegrityError(f"observation segment count mismatch: {segment_id}")
            fetched_times: list[str] = []
            for row in rows:
                observation_id = self._required_text(
                    row["observation_id"], f"observation segment {segment_id} row id"
                )
                fetched_at = self._timestamp(
                    row["fetched_at"], f"observation segment {segment_id} fetched time"
                )
                archived_at = self._timestamp(
                    row["content_archived_at"],
                    f"observation segment {segment_id} archived time",
                )
                entry = entries.get(observation_id)
                if (
                    row["subject_id"] != subject_id
                    or row["content"] != ""
                    or archived_at != created_at
                    or entry is None
                    or row["content_hash"] != entry[1]
                ):
                    raise IntegrityError(
                        f"observation segment reference mismatch: {observation_id}"
                    )
                fetched_times.append(fetched_at)
                contents[observation_id] = entry[0]
            if min(fetched_times) != first_fetched_at or max(fetched_times) != last_fetched_at:
                raise IntegrityError(f"observation segment range mismatch: {segment_id}")
            segment_keys.add(object_key)

        if segment_keys != expected_keys or len(contents) != len(archived_rows):
            raise IntegrityError("observation archive metadata and references do not match")
        return len(segments), contents

    @staticmethod
    def _required_text(value: object, context: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise IntegrityError(f"{context} is invalid")
        return value

    @classmethod
    def _timestamp(cls, value: object, context: str) -> str:
        text = cls._required_text(value, context)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            raise IntegrityError(f"{context} is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise IntegrityError(f"{context} is invalid")
        return text

    @classmethod
    def _digest(cls, value: object, context: str) -> str:
        text = cls._required_text(value, context)
        if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
            raise IntegrityError(f"{context} is invalid")
        return text

    @staticmethod
    def _decompress_bounded(payload: bytes, maximum: int) -> bytes:
        if maximum < 1:
            raise PayloadLimitError("observation archive decompressed payload exceeds its limit")
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(payload, maximum + 1)
        if len(decoded) > maximum or decompressor.unconsumed_tail:
            raise PayloadLimitError("observation archive decompressed payload exceeds its limit")
        decoded += decompressor.flush(maximum + 1 - len(decoded))
        if len(decoded) > maximum:
            raise PayloadLimitError("observation archive decompressed payload exceeds its limit")
        if not decompressor.eof or decompressor.unused_data:
            raise IntegrityError("observation archive compressed payload is invalid")
        return decoded
