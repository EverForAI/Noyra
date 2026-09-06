from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import threading
import time
import zlib
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import Database, EventStore, IdentityStore, StorageLifecycleManager, StorageQuota
from noyra.core.archive import (
    ArchiveTransferQueue,
    CloudArchiveCoordinator,
    resolve_subject_archive_root,
)
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.integrity import IntegrityAuditLimits, IntegrityRegistry
from noyra.core.storage import StorageLayout, StorageUsage, StorageUsageScanner
from noyra.core.types import canonical_json, content_hash
from noyra.world import FetchedDocument, ObservationStore, SourceRegistry
from noyra.world.observation_archive import ObservationContentArchive


def _insert_committed_event_staging_manifest(
    database: Database,
    subject_id: str,
    *,
    segment_id: str = "integrity-event-segment",
    finalized_at: str = "2026-08-19T00:00:00.000+00:00",
) -> tuple[str, str]:
    event = EventStore(database).append(
        subject_id,
        "gate2_integrity_staging",
        "gate2-test",
        {"staging": "integrity"},
        occurred_at="2026-08-18T00:00:00.000+00:00",
    )
    source_state = [
        {
            "event_id": event.event_id,
            "occurred_at": "2026-08-18T00:00:00.000+00:00",
            "payload_hash": event.payload_hash,
        }
    ]
    object_key = f"events/{segment_id}.json.zlib.enc"
    source_json = canonical_json(source_state)
    source_hash = content_hash(source_state)
    plaintext_hash = hashlib.sha256(b"compressed-event-payload").hexdigest()
    key_fingerprint = "a" * 64
    manifest_id = f"manifest-{segment_id}"
    with database.transaction() as connection:
        connection.execute(
            "UPDATE events SET payload_json = '{}', payload_archive_key = ?, "
            "payload_archived_at = ? WHERE event_id = ? AND subject_id = ?",
            (object_key, finalized_at, event.event_id, subject_id),
        )
        connection.execute(
            """INSERT INTO event_payload_segments(
                segment_id, subject_id, object_key, first_occurred_at,
                last_occurred_at, event_count, compressed_hash, archive_format,
                encryption_key_id, encryption_key_fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
            (
                segment_id,
                subject_id,
                object_key,
                source_state[0]["occurred_at"],
                source_state[0]["occurred_at"],
                plaintext_hash,
                "noyra-event-payload-segment-v1",
                "key-1",
                key_fingerprint,
                finalized_at,
            ),
        )
        connection.execute(
            """INSERT INTO archive_staging_manifests(
                manifest_id, subject_id, archive_kind, segment_id, object_key,
                source_state_json, source_state_hash, item_count, first_item_at,
                last_item_at, plaintext_hash, archive_format, encryption_key_id,
                encryption_key_fingerprint, stored_byte_size, stored_hash, status,
                last_error, created_at, updated_at, finalized_at
            ) VALUES (?, ?, 'event_payload', ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?,
                      24, ?, 'committed', NULL, ?, ?, ?)""",
            (
                manifest_id,
                subject_id,
                segment_id,
                object_key,
                source_json,
                source_hash,
                source_state[0]["occurred_at"],
                source_state[0]["occurred_at"],
                plaintext_hash,
                "noyra-event-payload-segment-v1",
                "key-1",
                key_fingerprint,
                hashlib.sha256(b"stored-event-payload").hexdigest(),
                finalized_at,
                finalized_at,
                finalized_at,
            ),
        )
    return manifest_id, event.event_id


def _configure_archive_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"g" * 32).decode("ascii"),
    )
    monkeypatch.delenv("NOYRA_ARCHIVE_KEYRING_PATH", raising=False)


def _materialize_v42_database(path: Path) -> tuple[str, str]:
    """Create a real v42 queue shape without relying on a historical fixture."""
    database = Database(path)
    subject_id = "Noyra-v42-migration"
    IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
    transfer_id = "transfer-v42-uploading"
    with database.transaction() as connection:
        for trigger in (
            "validate_archive_transfer_claim_insert",
            "validate_archive_transfer_claim_update",
            "validate_archive_transfer_identity_immutable",
            "validate_archive_transfer_transition",
            "prevent_archive_transfer_delete",
            "validate_training_record_event_subject",
        ):
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        connection.execute("DROP INDEX IF EXISTS idx_archive_transfer_lease")
        connection.execute("DROP INDEX IF EXISTS idx_archive_transfer_due")
        connection.execute(
            "ALTER TABLE archive_transfer_queue RENAME TO archive_transfer_queue_v43"
        )
        connection.executescript(
            """
CREATE TABLE archive_transfer_queue (
    transfer_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    storage_class TEXT NOT NULL CHECK (storage_class IN ('cold', 'cloud')),
    object_key TEXT NOT NULL,
    payload_path TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'uploading', 'uploaded', 'failed', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, object_key)
);
CREATE INDEX idx_archive_transfer_due
    ON archive_transfer_queue(subject_id, status, next_attempt_at);
CREATE TRIGGER prevent_archive_transfer_delete
BEFORE DELETE ON archive_transfer_queue BEGIN
    SELECT RAISE(ABORT, 'archive transfer records cannot be deleted');
END;
DROP TABLE archive_transfer_queue_v43;
DROP TABLE archive_staging_manifests;
"""
        )
        connection.execute("DROP TRIGGER validate_public_post_moderation_transition")
        connection.execute("DROP TRIGGER validate_public_post_identity_insert")
        connection.execute("DROP TRIGGER prevent_public_post_identity_update")
        connection.execute("DROP INDEX uq_public_post_moderation_revision")
        connection.execute("DROP INDEX uq_public_post_moderation_idempotency")
        connection.execute("DROP INDEX idx_public_post_rate_events_subject_time")
        connection.execute("DROP INDEX idx_public_post_captcha_issue_subject_time")
        connection.execute("ALTER TABLE public_post_moderation_events DROP COLUMN idempotency_key")
        connection.execute(
            "ALTER TABLE public_post_moderation_events DROP COLUMN previous_event_id"
        )
        connection.execute("ALTER TABLE public_post_moderation_events DROP COLUMN revision")
        connection.execute("ALTER TABLE public_posts DROP COLUMN author_provenance")
        connection.execute("ALTER TABLE public_posts DROP COLUMN identity_hash")
        connection.execute(
            "ALTER TABLE public_post_controls DROP COLUMN captcha_global_rate_per_minute"
        )
        connection.execute(
            "ALTER TABLE public_post_controls DROP COLUMN captcha_issue_limit_per_hour"
        )
        connection.execute("ALTER TABLE public_post_controls DROP COLUMN storage_cap_bytes")
        connection.execute("ALTER TABLE interaction_transports DROP COLUMN endpoint_digest")
        connection.execute("ALTER TABLE interaction_transports DROP COLUMN endpoint_contract")
        connection.execute(
            """INSERT INTO archive_transfer_queue(
                   transfer_id, subject_id, storage_class, object_key, payload_path,
                   byte_size, content_hash, status, attempts, next_attempt_at,
                   last_error, created_at, updated_at
               ) VALUES (?, ?, 'cloud', ?, ?, 7, ?, 'uploading', 1, ?, NULL, ?, ?)""",
            (
                transfer_id,
                subject_id,
                "events/v42-segment.enc",
                str(path.parent / "training_raw" / "v42-segment.enc"),
                hashlib.sha256(b"v42-row").hexdigest(),
                "2026-08-19T00:00:00.000+00:00",
                "2026-08-19T00:00:00.000+00:00",
                "2026-08-19T00:00:00.000+00:00",
            ),
        )
        connection.execute("UPDATE schema_meta SET value = '42' WHERE key = 'schema_version'")
    return subject_id, transfer_id


def test_v42_to_v43_migration_adds_fencing_columns_and_preserves_uploading_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "noyra.sqlite3"
    subject_id, transfer_id = _materialize_v42_database(path)

    database = Database(path)

    with database.connection() as connection:
        version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        )
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(archive_transfer_queue)")
        }
        transfer = connection.execute(
            "SELECT status, claim_token, lease_owner, lease_expires_at "
            "FROM archive_transfer_queue WHERE transfer_id = ? AND subject_id = ?",
            (transfer_id, subject_id),
        ).fetchone()
        staging_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'archive_staging_manifests'"
        ).fetchone()
    assert version == CURRENT_SCHEMA_VERSION
    assert {"claim_token", "lease_owner", "lease_expires_at"} <= columns
    assert transfer is not None
    assert tuple(transfer) == ("uploading", None, None, None)
    assert staging_exists is not None

    with database.transaction() as connection:
        connection.execute(
            "UPDATE archive_transfer_queue SET status = 'failed', last_error = ?, "
            "next_attempt_at = ?, updated_at = ? WHERE transfer_id = ?",
            (
                "expired_upload_lease_recovered",
                "2026-08-19T00:01:00.000+00:00",
                "2026-08-19T00:01:00.000+00:00",
                transfer_id,
            ),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="claim does not match status"),
        database.transaction() as connection,
    ):
        connection.execute(
            "UPDATE archive_transfer_queue SET status = 'uploading' WHERE transfer_id = ?",
            (transfer_id,),
        )
    assert not list(tmp_path.glob("noyra.sqlite3.pre-migration-v*.bak"))


def test_v43_migration_replaces_a_same_named_stale_security_trigger(tmp_path: Path) -> None:
    path = tmp_path / "noyra.sqlite3"
    subject_id, _ = _materialize_v42_database(path)
    # sqlite3.Connection.__enter__ commits/rolls back but does not close the
    # handle.  Close it explicitly before Database.initialize switches the
    # migrated file back to WAL on Windows.
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
CREATE TRIGGER validate_archive_transfer_claim_insert
AFTER INSERT ON archive_transfer_queue BEGIN
    SELECT 1;
END;
"""
        )

    database = Database(path)
    with (
        pytest.raises(sqlite3.IntegrityError, match="claim does not match status"),
        database.transaction() as connection,
    ):
        connection.execute(
            """INSERT INTO archive_transfer_queue(
                   transfer_id, subject_id, storage_class, object_key, payload_path,
                   byte_size, content_hash, status, attempts, next_attempt_at,
                   last_error, created_at, updated_at
               ) VALUES ('stale-trigger-test', ?, 'cloud', 'events/stale.enc', ?, 1, ?,
                         'uploading', 0, ?, NULL, ?, ?)""",
            (
                subject_id,
                str(tmp_path / "training_raw" / "stale.enc"),
                hashlib.sha256(b"x").hexdigest(),
                "2026-08-19T00:00:00.000+00:00",
                "2026-08-19T00:00:00.000+00:00",
                "2026-08-19T00:00:00.000+00:00",
            ),
        )


def test_cleanup_failure_after_v43_migration_never_rolls_back_or_restores_missing_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "noyra.sqlite3"
    _materialize_v42_database(path)

    def fail_after_deleting_current_backup(database: Database) -> None:
        backup = database.path.with_name(f"{database.path.name}.pre-migration-v42.bak")
        assert backup.is_file()
        backup.unlink()
        raise RuntimeError("injected post-migration cleanup failure")

    with (
        patch.object(
            Database,
            "_cleanup_migration_backups",
            autospec=True,
            side_effect=fail_after_deleting_current_backup,
        ) as cleanup,
        patch.object(Database, "_restore_migration_backup", autospec=True) as restore,
        pytest.raises(RuntimeError, match="post-migration cleanup failure"),
    ):
        Database(path)
    cleanup.assert_called_once()
    restore.assert_not_called()

    with sqlite3.connect(path) as connection:
        version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        )
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(archive_transfer_queue)")
        }
        staging_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'archive_staging_manifests'"
        ).fetchone()
    assert version == CURRENT_SCHEMA_VERSION
    assert {"claim_token", "lease_owner", "lease_expires_at"} <= columns
    assert staging_exists is not None
    assert not path.with_name(f"{path.name}.pre-migration-v42.bak").exists()

    # A subsequent startup sees the authoritative v43 database and succeeds;
    # it does not need the already-cleaned rollback image.
    Database(path)


def test_migration_backup_unlink_failure_is_best_effort_and_keeps_v43_authoritative(
    tmp_path: Path,
) -> None:
    path = tmp_path / "noyra.sqlite3"
    _materialize_v42_database(path)
    backup = path.with_name(f"{path.name}.pre-migration-v42.bak")
    original_unlink = Path.unlink

    def reject_backup_unlink(candidate: Path, missing_ok: bool = False) -> None:
        if candidate == backup:
            raise PermissionError("injected migration backup cleanup denial")
        original_unlink(candidate, missing_ok=missing_ok)

    with (
        patch.object(Path, "unlink", autospec=True, side_effect=reject_backup_unlink),
        patch.object(Database, "_restore_migration_backup", autospec=True) as restore,
    ):
        database = Database(path)

    restore.assert_not_called()
    assert backup.is_file()
    with database.connection() as connection:
        version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        )
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    assert version == CURRENT_SCHEMA_VERSION
    assert quick_check == "ok"

    # Once the filesystem failure clears, ordinary startup removes the stale
    # rollback image without requiring a second migration.
    Database(path)
    assert not backup.exists()


def test_uploaded_payload_cleanup_cannot_delete_a_concurrent_reenqueue(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-queue-cleanup-fence"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    queue = ArchiveTransferQueue(database, subject_id, tmp_path / "training_raw")
    object_key = "snapshots/cleanup-fence.zlib"
    old_payload = b"old-uploaded-payload"
    new_payload = old_payload
    transfer_id = queue.enqueue(object_key, old_payload)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE archive_transfer_queue SET status = 'uploading', "
            "claim_token = ?, lease_owner = ?, lease_expires_at = ? "
            "WHERE transfer_id = ?",
            (
                "cleanup-test-claim",
                "cleanup-test-owner",
                "2099-01-01T00:00:00.000+00:00",
                transfer_id,
            ),
        )
        connection.execute(
            "UPDATE archive_transfer_queue SET status = 'uploaded', "
            "claim_token = NULL, lease_owner = NULL, lease_expires_at = NULL "
            "WHERE transfer_id = ?",
            (transfer_id,),
        )

    cleanup_locked = threading.Event()
    cleanup_release = threading.Event()
    original_transaction = database.transaction

    @contextmanager
    def pause_cleanup_transaction() -> Iterator[Any]:
        with original_transaction() as connection:
            cleanup_locked.set()
            assert cleanup_release.wait(5)
            yield connection

    cleanup_result: list[int] = []
    enqueue_done = threading.Event()
    enqueue_error: list[BaseException] = []

    with patch.object(database, "transaction", pause_cleanup_transaction):
        cleanup = threading.Thread(
            target=lambda: cleanup_result.append(queue._cleanup_uploaded_payloads(limit=1))
        )
        cleanup.start()
        assert cleanup_locked.wait(5)

        def reenqueue() -> None:
            try:
                queue.enqueue(object_key, new_payload)
            except BaseException as error:
                enqueue_error.append(error)
            finally:
                enqueue_done.set()

        writer = threading.Thread(target=reenqueue)
        writer.start()
        time.sleep(0.05)
        assert not enqueue_done.is_set()
        cleanup_release.set()
        cleanup.join(5)
        writer.join(5)

    assert not enqueue_error
    assert cleanup_result == [1]
    with database.connection() as connection:
        row = connection.execute(
            "SELECT status, payload_path, content_hash FROM archive_transfer_queue "
            "WHERE transfer_id = ?",
            (transfer_id,),
        ).fetchone()
    assert row["status"] == "queued"
    payload_path = Path(str(row["payload_path"]))
    assert payload_path.read_bytes() == new_payload
    assert row["content_hash"] == hashlib.sha256(new_payload).hexdigest()


def test_pressure_reconciliation_garbage_collects_archive_orphans_but_preserves_live_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_archive_key(monkeypatch)
    layout = StorageLayout.create(tmp_path)
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-gate2-orphan-gc"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    archive = EventPayloadArchive(
        database,
        layout.subject / "cold",
        subject_id=subject_id,
    )
    live_key = "events/live-prepared.json.zlib.enc"
    archive.provider.put(live_key, b"live")
    archive.staging.prepare(
        subject_id,
        archive_kind="event_payload",
        segment_id="event-segment-live-prepared",
        object_key=live_key,
        source_state=[
            {
                "event_id": "event-live",
                "occurred_at": "2026-01-01T00:00:00.000+00:00",
                "payload_hash": "a" * 64,
            }
        ],
        item_count=1,
        first_item_at="2026-01-01T00:00:00.000+00:00",
        last_item_at="2026-01-01T00:00:00.000+00:00",
        plaintext_hash=hashlib.sha256(b"live").hexdigest(),
        archive_format=archive.format,
        encryption_key_id=str(archive.provider.key_id),
        encryption_key_fingerprint=str(archive.provider.key_fingerprint),
    )
    orphan_key = "events/orphan.json.zlib.enc"
    archive.provider.put(orphan_key, b"orphan")
    temporary = archive.root / "events" / ".awrite_abandoned.tmp"
    temporary.write_bytes(b"partial")
    old = time.time() - 7_200
    os.utime(archive.provider._path(live_key), (old, old))
    os.utime(archive.provider._path(orphan_key), (old, old))
    os.utime(temporary, (old, old))

    manager = StorageLifecycleManager(
        database,
        subject_id,
        layout,
        StorageQuota(),
        minimum_free_bytes=10_000_000,
    )
    manager.scanner.scan = lambda: StorageUsage(1, 0, 0, 1)  # type: ignore[method-assign]

    result = manager.reconcile_archives(limit=8)

    assert result["event_orphans_removed"] == 2
    assert archive.provider.exists(live_key)
    assert not archive.provider.exists(orphan_key)
    assert not temporary.exists()
    with database.connection() as connection:
        status = connection.execute(
            "SELECT status FROM archive_staging_manifests WHERE segment_id = ?",
            ("event-segment-live-prepared",),
        ).fetchone()[0]
    assert status == "prepared"


def test_cloud_tick_removes_unreferenced_queue_payloads_without_provider(tmp_path: Path) -> None:
    layout = StorageLayout.create(tmp_path)
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-gate2-queue-orphan-gc"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    coordinator = CloudArchiveCoordinator(database, subject_id, layout.training_raw)
    coordinator.keyring = None
    referenced_id = coordinator.queue.enqueue("snapshots/referenced.zlib", b"referenced")
    with database.connection() as connection:
        referenced_path = Path(
            connection.execute(
                "SELECT payload_path FROM archive_transfer_queue WHERE transfer_id = ?",
                (referenced_id,),
            ).fetchone()[0]
        )
    queue_root = referenced_path.parents[1]
    orphan = queue_root / "snapshots" / "orphan.zlib"
    orphan.write_bytes(b"orphan")
    temporary = queue_root / "snapshots" / ".qwrite_abandoned.tmp"
    temporary.write_bytes(b"partial")
    old = time.time() - 7_200
    for path in (referenced_path, orphan, temporary):
        os.utime(path, (old, old))

    result = coordinator.tick(garbage_collect_local=True)

    assert result["garbage_collected"] == 2
    assert referenced_path.is_file()
    assert not orphan.exists()
    assert not temporary.exists()


def test_event_archive_rejects_oversized_source_before_provider_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_archive_key(monkeypatch)
    monkeypatch.setattr("noyra.core.event_archive.MAX_EVENT_ARCHIVE_SOURCE_BYTES", 32)
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-gate2-event-source-budget"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    EventStore(database).append(
        subject_id,
        "oversized_event",
        "gate2",
        {"payload": "x" * 256},
        occurred_at="2020-01-01T00:00:00+00:00",
    )
    archive = EventPayloadArchive(
        database,
        tmp_path / "subject" / "cold",
        subject_id=subject_id,
    )
    with (
        patch.object(archive.provider, "put") as put,
    ):
        assert archive.archive_cold(subject_id, older_than_days=30) == 0
    put.assert_not_called()


def test_oversized_event_does_not_starve_a_later_bounded_archive_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_archive_key(monkeypatch)
    monkeypatch.setattr("noyra.core.event_archive.MAX_EVENT_ARCHIVE_SOURCE_BYTES", 64)
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-gate2-event-source-skip"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    oversized = EventStore(database).append(
        subject_id,
        "oversized_event",
        "gate2",
        {"payload": "x" * 256},
        occurred_at="2020-01-01T00:00:00+00:00",
    )
    bounded = EventStore(database).append(
        subject_id,
        "bounded_event",
        "gate2",
        {"ok": True},
        occurred_at="2020-01-02T00:00:00+00:00",
    )
    archive = EventPayloadArchive(
        database,
        tmp_path / "subject" / "cold",
        subject_id=subject_id,
    )

    assert archive.archive_cold(subject_id, older_than_days=30) == 1

    with database.connection() as connection:
        rows = {
            str(row["event_id"]): row["payload_archive_key"]
            for row in connection.execute(
                "SELECT event_id, payload_archive_key FROM events WHERE event_id IN (?, ?)",
                (oversized.event_id, bounded.event_id),
            ).fetchall()
        }
    assert rows[oversized.event_id] is None
    assert rows[bounded.event_id] is not None


def test_observation_archive_rejects_oversized_source_before_provider_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_archive_key(monkeypatch)
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-gate2-observation-source-budget"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    source = SourceRegistry(database).register(
        subject_id,
        "Oversized source",
        "https://example.com/gate2/oversized",
        "news",
        trust_score=0.8,
        status="active",
        reason="gate2 source budget",
    )
    content = "x" * 256
    ObservationStore(database).record(
        subject_id,
        source.source_id,
        FetchedDocument(
            url=source.url,
            title="Oversized observation",
            content=content,
            content_hash=content_hash(content),
            media_type="text/plain",
            injection_signals=(),
            etag=None,
            last_modified=None,
            fetched_at="2020-01-01T00:00:00+00:00",
        ),
    )
    archive = ObservationContentArchive(
        database,
        tmp_path / "subject" / "cold",
        subject_id=subject_id,
    )
    monkeypatch.setattr(archive, "max_source_bytes", 32)
    with (
        patch.object(archive.provider, "put") as put,
    ):
        assert archive.archive_cold(subject_id, older_than_days=30) == 0
    put.assert_not_called()


@pytest.mark.parametrize(
    "object_key",
    ["events/../same", "./same", "events//same", "/absolute", "events\\same"],
)
def test_archive_queue_rejects_noncanonical_object_keys(
    object_key: str,
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-queue-canonical-key"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    queue = ArchiveTransferQueue(database, subject_id, tmp_path / "training_raw")

    with pytest.raises(ValueError, match="canonical relative POSIX path"):
        queue.enqueue(object_key, b"payload")

    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM archive_transfer_queue WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()[0]
            == 0
        )


def test_subject_keyed_archives_remain_readable_through_shared_stores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_archive_key(monkeypatch)
    database = Database(tmp_path / "noyra.sqlite3")
    identities = IdentityStore(database)
    event_ids: dict[str, str] = {}
    observation_ids: dict[str, str] = {}
    expected_observations: dict[str, str] = {}
    resolved_roots: dict[str, Path] = {}

    for index, subject_id in enumerate(("Noyra-archive-alpha", "Noyra-archive-beta"), start=1):
        identities.ensure(subject_id, content_hash({"seed": subject_id}))
        event = EventStore(database).append(
            subject_id,
            "gate2_archive_subject",
            "gate2-test",
            {"subject": subject_id},
            occurred_at="2020-01-01T00:00:00+00:00",
        )
        source = SourceRegistry(database).register(
            subject_id,
            f"Gate 2 source {index}",
            f"https://example.com/gate2/archive/{index}",
            "news",
            trust_score=0.8,
            status="active",
            reason="subject-keyed archive test",
        )
        content = f"archived observation for {subject_id}"
        observation = ObservationStore(database).record(
            subject_id,
            source.source_id,
            FetchedDocument(
                url=source.url,
                title=f"Observation {index}",
                content=content,
                content_hash=content_hash(content),
                media_type="text/plain",
                injection_signals=(),
                etag=None,
                last_modified=None,
                fetched_at="2020-01-01T00:00:00+00:00",
            ),
        )[0]
        event_archive = EventPayloadArchive(
            database,
            tmp_path / "subject" / "cold",
            subject_id=subject_id,
        )
        observation_archive = ObservationContentArchive(
            database,
            tmp_path / "subject" / "cold",
            subject_id=subject_id,
        )
        assert event_archive.archive_cold(subject_id, older_than_days=30) >= 1
        assert observation_archive.archive_cold(subject_id, older_than_days=30) == 1
        expected_root = tmp_path / "subject" / identities.storage_key(subject_id) / "cold"
        assert event_archive.root == expected_root
        assert observation_archive.root == expected_root
        resolved_roots[subject_id] = expected_root
        event_ids[subject_id] = event.event_id
        observation_ids[subject_id] = observation.observation_id
        expected_observations[subject_id] = content

    assert len(set(resolved_roots.values())) == 2
    events = EventStore(database)
    observations = ObservationStore(database)
    for subject_id in ("Noyra-archive-alpha", "Noyra-archive-beta", "Noyra-archive-alpha"):
        assert events.get(event_ids[subject_id]).payload == {"subject": subject_id}
        assert (
            observations.get(observation_ids[subject_id], subject_id=subject_id).content
            == expected_observations[subject_id]
        )


def test_shared_legacy_archive_fails_closed_and_is_counted_as_shared_usage(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    identities = IdentityStore(database)
    for subject_id in ("Noyra-legacy-alpha", "Noyra-legacy-beta"):
        identities.ensure(subject_id, content_hash({"seed": subject_id}))
    legacy = tmp_path / "subject" / "cold"
    legacy.mkdir(parents=True)
    payload = b"shared legacy archive bytes"
    (legacy / "segment.enc").write_bytes(payload)

    with pytest.raises(IntegrityError, match="ambiguous"):
        resolve_subject_archive_root(
            database,
            "Noyra-legacy-alpha",
            legacy,
            create=False,
        )

    usage = StorageUsageScanner(tmp_path, database, "Noyra-legacy-alpha").scan()
    assert usage.local_archive_bytes == 0
    assert usage.shared_bytes >= len(payload)


def test_arbitrary_archive_root_is_rejected_for_multi_subject_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_archive_key(monkeypatch)
    database = Database(tmp_path / "noyra.sqlite3")
    for subject_id in ("Noyra-arbitrary-root-a", "Noyra-arbitrary-root-b"):
        IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))

    with pytest.raises(IntegrityError, match="not subject-scoped"):
        EventPayloadArchive(
            database,
            tmp_path / "shared-arbitrary-archive",
            subject_id="Noyra-arbitrary-root-a",
        )


def test_scoped_training_usage_counts_unassigned_training_files(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-training-shared-usage"
    IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
    layout = StorageLayout.create(tmp_path)
    unassigned = layout.training_raw / "unassigned-training.bin"
    unassigned.write_bytes(b"shared training bytes")

    usage = StorageUsageScanner(tmp_path, database, subject_id).scan()

    assert usage.training_bytes >= unassigned.stat().st_size
    assert usage.shared_bytes >= unassigned.stat().st_size


def test_event_staged_payload_revalidates_embedded_payload_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_archive_key(monkeypatch)
    database = Database(tmp_path / "noyra.sqlite3")
    archive = EventPayloadArchive(database, tmp_path / "archive")
    subject_id = "Noyra-staged-event"
    source_state = [
        {
            "event_id": "evt_staged",
            "occurred_at": "2020-01-01T00:00:00+00:00",
            "payload_hash": content_hash({"expected": True}),
        }
    ]
    body = {
        "format": archive.format,
        "subject_id": subject_id,
        "events": [
            {
                "event_id": "evt_staged",
                "payload_json": canonical_json({"tampered": True}),
                "payload_hash": source_state[0]["payload_hash"],
            }
        ],
    }
    compressed = zlib.compress(canonical_json(body).encode("utf-8"))
    manifest = {
        "plaintext_hash": hashlib.sha256(compressed).hexdigest(),
        "source_state_json": canonical_json(source_state),
        "source_state_hash": content_hash(source_state),
        "archive_format": archive.format,
        "subject_id": subject_id,
        "item_count": 1,
        "first_item_at": source_state[0]["occurred_at"],
        "last_item_at": source_state[0]["occurred_at"],
    }

    with pytest.raises(IntegrityError, match="payload hash mismatch"):
        archive._verify_staged_payload(manifest, compressed)


def test_observation_staged_payload_revalidates_source_state_and_content_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_archive_key(monkeypatch)
    database = Database(tmp_path / "noyra.sqlite3")
    archive = ObservationContentArchive(database, tmp_path / "archive")
    subject_id = "Noyra-staged-observation"
    source_state = [
        {
            "observation_id": "obs_staged",
            "fetched_at": "2020-01-01T00:00:00+00:00",
            "content_hash": content_hash("expected"),
        }
    ]
    body = {
        "format": archive.format,
        "subject_id": subject_id,
        "observations": [
            {
                "observation_id": "obs_staged",
                "content": "tampered",
                "content_hash": source_state[0]["content_hash"],
            }
        ],
    }
    compressed = zlib.compress(canonical_json(body).encode("utf-8"))
    manifest = {
        "plaintext_hash": hashlib.sha256(compressed).hexdigest(),
        "source_state_json": canonical_json(source_state),
        "source_state_hash": content_hash([{"different": True}]),
        "archive_format": archive.format,
        "subject_id": subject_id,
        "item_count": 1,
        "first_item_at": source_state[0]["fetched_at"],
        "last_item_at": source_state[0]["fetched_at"],
    }

    with pytest.raises(IntegrityError, match="source state hash mismatch"):
        archive._verify_staged_payload(manifest, compressed)
    manifest["source_state_hash"] = content_hash(source_state)
    with pytest.raises(IntegrityError, match="content hash mismatch"):
        archive._verify_staged_payload(manifest, compressed)


@pytest.mark.parametrize("archive_kind", ["event", "observation"])
def test_staged_archives_reject_unsupported_format_and_wrong_time_bounds(
    archive_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_archive_key(monkeypatch)
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = f"Noyra-staged-metadata-{archive_kind}"
    if archive_kind == "event":
        archive: Any = EventPayloadArchive(database, tmp_path / "archive")
        source_state = [
            {
                "event_id": "evt_metadata",
                "occurred_at": "2020-01-01T00:00:00+00:00",
                "payload_hash": content_hash({"ok": True}),
            }
        ]
        body = {
            "format": archive.format,
            "subject_id": subject_id,
            "events": [
                {
                    "event_id": "evt_metadata",
                    "payload_json": canonical_json({"ok": True}),
                    "payload_hash": source_state[0]["payload_hash"],
                }
            ],
        }
        timestamp = str(source_state[0]["occurred_at"])
    else:
        archive = ObservationContentArchive(database, tmp_path / "archive")
        source_state = [
            {
                "observation_id": "obs_metadata",
                "fetched_at": "2020-01-01T00:00:00+00:00",
                "content_hash": content_hash("ok"),
            }
        ]
        body = {
            "format": archive.format,
            "subject_id": subject_id,
            "observations": [
                {
                    "observation_id": "obs_metadata",
                    "content": "ok",
                    "content_hash": source_state[0]["content_hash"],
                }
            ],
        }
        timestamp = str(source_state[0]["fetched_at"])
    compressed = zlib.compress(canonical_json(body).encode("utf-8"))
    manifest = {
        "plaintext_hash": hashlib.sha256(compressed).hexdigest(),
        "source_state_json": canonical_json(source_state),
        "source_state_hash": content_hash(source_state),
        "archive_format": "unsupported-format-v999",
        "subject_id": subject_id,
        "item_count": 1,
        "first_item_at": timestamp,
        "last_item_at": timestamp,
    }

    with pytest.raises(IntegrityError, match=r"metadata mismatch|unsupported"):
        archive._verify_staged_payload(manifest, compressed)

    manifest["archive_format"] = archive.format
    manifest["first_item_at"] = "2019-01-01T00:00:00+00:00"
    with pytest.raises(IntegrityError, match="time bounds mismatch"):
        archive._verify_staged_payload(manifest, compressed)


def test_event_reconcile_never_deletes_object_committed_by_concurrent_finalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_archive_key(monkeypatch)
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-staging-cas-event"
    IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
    event = EventStore(database).append(
        subject_id,
        "gate2_staging_cas",
        "gate2-test",
        {"durable": True},
        occurred_at="2020-01-01T00:00:00+00:00",
    )
    archive = EventPayloadArchive(
        database,
        tmp_path / "subject" / "cold",
        subject_id=subject_id,
    )
    original_finalize = archive._finalize_staged_manifest

    def interrupt_after_store(_: str, __: str) -> int:
        raise IntegrityError("simulated interruption after provider storage")

    monkeypatch.setattr(archive, "_finalize_staged_manifest", interrupt_after_store)
    with pytest.raises(IntegrityError, match="simulated interruption"):
        archive.archive_cold(subject_id, older_than_days=30)
    with database.connection() as connection:
        manifest = connection.execute(
            "SELECT manifest_id, object_key, status FROM archive_staging_manifests "
            "WHERE subject_id = ? AND archive_kind = 'event_payload'",
            (subject_id,),
        ).fetchone()
    assert manifest is not None and manifest["status"] == "stored"
    object_path = archive.root / str(manifest["object_key"])
    assert object_path.is_file()

    original_verify = archive._verify_staged_payload

    def finalize_then_report_stale(row: object, payload: bytes) -> None:
        original_verify(row, payload)
        original_finalize(str(manifest["manifest_id"]), subject_id)
        raise IntegrityError("stale replay verification result")

    monkeypatch.setattr(archive, "_finalize_staged_manifest", original_finalize)
    monkeypatch.setattr(archive, "_verify_staged_payload", finalize_then_report_stale)
    assert archive.reconcile_staging(subject_id) == {"finalized": 0, "abandoned": 0}

    assert object_path.is_file()
    assert EventStore(database).get(event.event_id).payload == {"durable": True}
    with database.connection() as connection:
        status = connection.execute(
            "SELECT status FROM archive_staging_manifests WHERE manifest_id = ?",
            (manifest["manifest_id"],),
        ).fetchone()[0]
    assert status == "committed"


def test_observation_reconcile_never_deletes_object_committed_by_concurrent_finalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_archive_key(monkeypatch)
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-staging-cas-observation"
    IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
    source = SourceRegistry(database).register(
        subject_id,
        "Staging CAS source",
        "https://example.com/gate2/staging-cas",
        "news",
        trust_score=0.8,
        status="active",
        reason="staging CAS test",
    )
    content = "durable observation content"
    observation = ObservationStore(database).record(
        subject_id,
        source.source_id,
        FetchedDocument(
            url=source.url,
            title="Staging CAS observation",
            content=content,
            content_hash=content_hash(content),
            media_type="text/plain",
            injection_signals=(),
            etag=None,
            last_modified=None,
            fetched_at="2020-01-01T00:00:00+00:00",
        ),
    )[0]
    archive = ObservationContentArchive(
        database,
        tmp_path / "subject" / "cold",
        subject_id=subject_id,
    )
    original_finalize = archive._finalize_staged_manifest

    def interrupt_after_store(_: str, __: str) -> int:
        raise IntegrityError("simulated interruption after provider storage")

    monkeypatch.setattr(archive, "_finalize_staged_manifest", interrupt_after_store)
    with pytest.raises(IntegrityError, match="simulated interruption"):
        archive.archive_cold(subject_id, older_than_days=30)
    with database.connection() as connection:
        manifest = connection.execute(
            "SELECT manifest_id, object_key, status FROM archive_staging_manifests "
            "WHERE subject_id = ? AND archive_kind = 'observation_content'",
            (subject_id,),
        ).fetchone()
    assert manifest is not None and manifest["status"] == "stored"
    object_path = archive.root / str(manifest["object_key"])
    assert object_path.is_file()

    original_verify = archive._verify_staged_payload

    def finalize_then_report_stale(row: object, payload: bytes) -> None:
        original_verify(row, payload)
        original_finalize(str(manifest["manifest_id"]), subject_id)
        raise IntegrityError("stale replay verification result")

    monkeypatch.setattr(archive, "_finalize_staged_manifest", original_finalize)
    monkeypatch.setattr(archive, "_verify_staged_payload", finalize_then_report_stale)
    assert archive.reconcile_staging(subject_id) == {"finalized": 0, "abandoned": 0}

    assert object_path.is_file()
    assert (
        ObservationStore(database).get(observation.observation_id, subject_id=subject_id).content
        == content
    )
    with database.connection() as connection:
        status = connection.execute(
            "SELECT status FROM archive_staging_manifests WHERE manifest_id = ?",
            (manifest["manifest_id"],),
        ).fetchone()[0]
    assert status == "committed"


def test_integrity_registry_validates_committed_archive_staging_ledger(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-integrity-staging-valid"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    _insert_committed_event_staging_manifest(database, subject_id)

    report = IntegrityRegistry().run(
        database,
        subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.archive_dead_letter",),
    )

    assert report.status == "ok"
    result = report.checks[0]
    assert result.details["staging_manifests"] == 1
    assert result.details["staging_committed"] == 1
    assert result.details["staging_live"] == 0


def test_integrity_registry_rejects_archive_staging_source_hash_tampering(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-integrity-staging-tamper"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    manifest_id, _ = _insert_committed_event_staging_manifest(database, subject_id)
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER validate_archive_staging_identity_immutable")
        connection.execute(
            "UPDATE archive_staging_manifests SET source_state_hash = ? WHERE manifest_id = ?",
            ("b" * 64, manifest_id),
        )

    report = IntegrityRegistry().run(
        database,
        subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.archive_dead_letter",),
    )

    assert report.status == "corrupt"
    assert report.p0 == ("core.archive_dead_letter:integrity_error",)


def test_archive_staging_integrity_is_subject_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    healthy_subject = "Noyra-integrity-staging-healthy"
    damaged_subject = "Noyra-integrity-staging-damaged"
    for subject_id in (healthy_subject, damaged_subject):
        IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    manifest_id, _ = _insert_committed_event_staging_manifest(database, damaged_subject)
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER validate_archive_staging_identity_immutable")
        connection.execute(
            "UPDATE archive_staging_manifests SET source_state_hash = ? WHERE manifest_id = ?",
            ("f" * 64, manifest_id),
        )

    healthy = IntegrityRegistry().run(
        database,
        healthy_subject,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.archive_dead_letter",),
    )
    damaged = IntegrityRegistry().run(
        database,
        damaged_subject,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.archive_dead_letter",),
    )

    assert healthy.status == "ok"
    assert healthy.checks[0].details["staging_manifests"] == 0
    assert damaged.status == "corrupt"
    assert damaged.p0 == ("core.archive_dead_letter:integrity_error",)


def test_integrity_registry_rejects_committed_archive_pointer_mismatch(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-integrity-staging-pointer"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    _, event_id = _insert_committed_event_staging_manifest(database, subject_id)
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_event_immutable_update")
        connection.execute(
            "UPDATE events SET payload_archived_at = ? WHERE event_id = ?",
            ("2026-08-19T00:01:00.000+00:00", event_id),
        )

    report = IntegrityRegistry().run(
        database,
        subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.archive_dead_letter",),
    )

    assert report.status == "corrupt"
    assert report.p0 == ("core.archive_dead_letter:integrity_error",)


def test_integrity_registry_bounds_archive_staging_rows(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-integrity-staging-budget"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    for index in range(2):
        event_id = f"budget-event-{index}"
        source_state = [
            {
                "event_id": event_id,
                "occurred_at": f"2026-08-18T00:0{index}:00.000+00:00",
                "payload_hash": "c" * 64,
            }
        ]
        now = f"2026-08-19T00:0{index}:00.000+00:00"
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO archive_staging_manifests(
                    manifest_id, subject_id, archive_kind, segment_id, object_key,
                    source_state_json, source_state_hash, item_count, first_item_at,
                    last_item_at, plaintext_hash, archive_format, encryption_key_id,
                    encryption_key_fingerprint, stored_byte_size, stored_hash, status,
                    last_error, created_at, updated_at, finalized_at
                ) VALUES (?, ?, 'event_payload', ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?,
                          NULL, NULL, 'prepared', NULL, ?, ?, NULL)""",
                (
                    f"budget-manifest-{index}",
                    subject_id,
                    f"budget-segment-{index}",
                    f"events/budget-segment-{index}.json.zlib.enc",
                    canonical_json(source_state),
                    content_hash(source_state),
                    source_state[0]["occurred_at"],
                    source_state[0]["occurred_at"],
                    "d" * 64,
                    "noyra-event-payload-segment-v1",
                    "key-1",
                    "e" * 64,
                    now,
                    now,
                ),
            )

    report = IntegrityRegistry().run(
        database,
        subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        limits=IntegrityAuditLimits(
            max_rows_per_check=1,
            max_bytes_per_check=64_000,
            max_value_bytes=64_000,
        ),
        check_ids=("core.archive_dead_letter",),
    )

    assert report.status == "degraded"
    assert report.p1 == ("core.archive_dead_letter:row_limit",)


def test_integrity_registry_bounds_archive_staging_bytes(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-integrity-staging-byte-budget"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    for index in range(4):
        _insert_committed_event_staging_manifest(
            database,
            subject_id,
            segment_id=f"integrity-byte-segment-{index}",
            finalized_at=f"2026-08-19T00:0{index}:00.000+00:00",
        )

    report = IntegrityRegistry().run(
        database,
        subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        limits=IntegrityAuditLimits(
            max_rows_per_check=10_000,
            max_bytes_per_check=2_048,
            max_value_bytes=2_048,
        ),
        check_ids=("core.archive_dead_letter",),
    )

    assert report.status == "degraded"
    assert report.p1 == ("core.archive_dead_letter:byte_limit",)
