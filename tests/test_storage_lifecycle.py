from __future__ import annotations

import asyncio
import base64
import io
import os
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from noyra.core import (
    EventStore,
    ExportJobManager,
    IdentityStore,
    SnapshotStore,
    StorageLayout,
    StorageLifecycleManager,
    StorageQuota,
)
from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.storage import (
    StorageUsage,
    StorageUsageHistory,
    StorageUsageScanner,
    storage_usage_sample_state_hash,
)
from noyra.service import NoyraService


class StorageLifecycleTestCase(unittest.TestCase):
    def test_usage_classifies_database_freelist_wal_cold_staging_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = StorageLayout.create(directory)
            database = Database(Path(directory) / "noyra.sqlite3")
            (layout.subject / "cold").mkdir(parents=True)
            (layout.subject / "cold" / "segment.enc").write_bytes(b"c" * 11)
            (layout.exports / "artifact.zip").write_bytes(b"e" * 13)
            staging = layout.training_raw / "archive_queue" / "subject" / "events"
            staging.mkdir(parents=True)
            (staging / "pending.enc").write_bytes(b"s" * 17)

            with database.connection() as connection:
                connection.execute("CREATE TABLE usage_scan_padding(payload BLOB NOT NULL)")
                connection.executemany(
                    "INSERT INTO usage_scan_padding(payload) VALUES (zeroblob(4096))",
                    [() for _ in range(64)],
                )
                connection.commit()
                connection.execute("DELETE FROM usage_scan_padding")
                connection.commit()
                usage = StorageUsageScanner(layout.root, database).scan()

            self.assertEqual(usage.local_archive_bytes, 11)
            self.assertEqual(usage.exports_bytes, 13)
            self.assertEqual(usage.cloud_staging_bytes, 17)
            self.assertGreater(usage.database_bytes, 0)
            self.assertGreater(usage.database_reclaimable_bytes, 0)
            self.assertGreater(usage.wal_bytes, 0)
            self.assertEqual(usage.freelist_bytes, usage.database_reclaimable_bytes)
            self.assertGreaterEqual(
                usage.physical_database_bytes,
                usage.effective_database_bytes,
            )
            self.assertEqual(
                usage.effective_subject_bytes,
                usage.subject_bytes - usage.database_reclaimable_bytes,
            )

    def test_usage_samples_are_append_only_and_prediction_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "noyra.sqlite3")
            subject_id = "Noyra-storage-trend-test"
            IdentityStore(database).ensure(subject_id, "d" * 64)
            history = StorageUsageHistory(database, subject_id)
            quota = StorageQuota(subject_bytes=1_000)
            first = StorageUsage(
                subject_bytes=100,
                effective_subject_bytes=80,
                database_bytes=50,
                database_reclaimable_bytes=20,
                training_bytes=0,
                workspace_bytes=0,
                free_bytes=1_000,
            )
            second = StorageUsage(
                subject_bytes=200,
                effective_subject_bytes=180,
                database_bytes=50,
                database_reclaimable_bytes=20,
                training_bytes=0,
                workspace_bytes=0,
                free_bytes=900,
            )
            third = StorageUsage(
                subject_bytes=300,
                effective_subject_bytes=280,
                database_bytes=50,
                database_reclaimable_bytes=20,
                training_bytes=0,
                workspace_bytes=0,
                free_bytes=800,
            )
            history.record(first, quota, 100, created_at="2026-08-17T00:00:00+00:00")
            history.record(second, quota, 100, created_at="2026-08-17T01:00:00+00:00")
            history.record(third, quota, 100, created_at="2026-08-17T02:00:00+00:00")

            trend = history.predict(
                quota,
                100,
                max_samples=2,
                lookback_hours=24,
                forecast_horizon_hours=24,
            )
            self.assertEqual(trend.sample_count, 2)
            self.assertAlmostEqual(trend.subject_growth_bytes_per_hour, 100.0)
            self.assertAlmostEqual(trend.effective_subject_growth_bytes_per_hour, 100.0)
            self.assertAlmostEqual(trend.free_space_change_bytes_per_hour, -100.0)
            self.assertAlmostEqual(trend.subject_quota_eta_hours or -1, 7.0)
            self.assertAlmostEqual(trend.minimum_free_eta_hours or -1, 7.0)

            with database.connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM storage_usage_samples WHERE subject_id = ?",
                    (subject_id,),
                ).fetchall()
            self.assertEqual(len(rows), 3)
            self.assertTrue(
                all(row["state_hash"] == storage_usage_sample_state_hash(row) for row in rows)
            )
            with (
                self.assertRaises(sqlite3.IntegrityError),
                database.transaction() as connection,
            ):
                connection.execute(
                    "DELETE FROM storage_usage_samples WHERE subject_id = ?",
                    (subject_id,),
                )

    def test_maintenance_retains_append_only_research_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = StorageLayout.create(directory)
            database = Database(Path(directory) / "noyra.sqlite3")
            subject_id = "Noyra-storage-retains-research-usage"
            IdentityStore(database).ensure(subject_id, "e" * 64)
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO browser_search_reservations("
                    "reservation_id, subject_id, idempotency_key, reserved_at"
                    ") VALUES ('reservation-old', ?, 'old', '2020-01-01T00:00:00+00:00')",
                    (subject_id,),
                )
            with patch.dict(
                os.environ,
                {
                    "NOYRA_ARCHIVE_ENCRYPTION_KEY": "",
                    "NOYRA_ARCHIVE_KEYRING_PATH": "",
                },
            ):
                manager = StorageLifecycleManager(
                    database,
                    subject_id,
                    layout,
                    StorageQuota(),
                    minimum_free_bytes=10_000_000,
                )
                manager.maintain()
                manager.maintain()
            with database.connection() as connection:
                retained = connection.execute(
                    "SELECT COUNT(*) FROM browser_search_reservations "
                    "WHERE reservation_id = 'reservation-old'"
                ).fetchone()[0]
                samples = connection.execute(
                    "SELECT COUNT(*) FROM storage_usage_samples WHERE subject_id = ?",
                    (subject_id,),
                ).fetchone()[0]
            self.assertEqual(retained, 1)
            self.assertEqual(samples, 1)

    def test_active_tick_runs_cloud_gc_before_final_storage_pressure_decision(self) -> None:
        order: list[str] = []

        class Lifecycle:
            def maintain(self, *, defer_pressure_decision: bool = False) -> SimpleNamespace:
                order.append(f"maintain:{defer_pressure_decision}")
                return SimpleNamespace(
                    actions=(),
                    cognition_allowed=False,
                    write_amplification_allowed=False,
                )

            def reassess(self, previous: SimpleNamespace) -> SimpleNamespace:
                del previous
                order.append("reassess")
                return SimpleNamespace(actions=(), cognition_allowed=False)

        class Cloud:
            def tick(
                self,
                *,
                garbage_collect_local: bool = False,
                allow_staging: bool = True,
            ) -> dict[str, int]:
                order.append(f"cloud:{garbage_collect_local}:{allow_staging}")
                return {"garbage_collected": 1}

        service = object.__new__(NoyraService)
        service._integrity_stop = threading.Event()
        service._thread_workers = set()
        service.storage_lifecycle = cast(Any, Lifecycle())
        service.cloud_archives = cast(Any, Cloud())

        self.assertEqual(asyncio.run(service._active_tick()), "storage_pressure")
        self.assertEqual(order, ["maintain:True", "cloud:True:False", "reassess"])
        self.assertEqual(service._thread_workers, set())

    def test_active_tick_disables_cloud_staging_for_noncritical_quota_pressure(self) -> None:
        calls: list[bool] = []

        class Lifecycle:
            def maintain(self, *, defer_pressure_decision: bool = False) -> SimpleNamespace:
                del defer_pressure_decision
                return SimpleNamespace(
                    actions=(),
                    cognition_allowed=True,
                    write_amplification_allowed=False,
                )

            def reassess(self, previous: SimpleNamespace) -> SimpleNamespace:
                return previous

        class Cloud:
            def tick(
                self,
                *,
                garbage_collect_local: bool = False,
                allow_staging: bool = True,
            ) -> dict[str, int]:
                del garbage_collect_local
                calls.append(allow_staging)
                return {"garbage_collected": 0}

        service = object.__new__(NoyraService)
        service._integrity_stop = threading.Event()
        service._thread_workers = set()
        service.storage_lifecycle = cast(Any, Lifecycle())
        service.cloud_archives = cast(Any, Cloud())

        self.assertIsNone(asyncio.run(service._active_tick()))
        self.assertEqual(calls, [False])
        self.assertEqual(service._thread_workers, set())

    def test_active_tick_keeps_pressure_fail_closed_when_cloud_gc_cannot_write(self) -> None:
        order: list[str] = []

        class Lifecycle:
            def maintain(self, *, defer_pressure_decision: bool = False) -> SimpleNamespace:
                order.append(f"maintain:{defer_pressure_decision}")
                return SimpleNamespace(
                    actions=(),
                    cognition_allowed=False,
                    write_amplification_allowed=False,
                )

            def reassess(self, previous: SimpleNamespace) -> SimpleNamespace:
                order.append("reassess")
                return previous

        class Cloud:
            def tick(self, **kwargs: Any) -> dict[str, int]:
                order.append(f"cloud:{kwargs['allow_staging']}")
                raise sqlite3.OperationalError("database or disk is full")

        service = object.__new__(NoyraService)
        service._integrity_stop = threading.Event()
        service._thread_workers = set()
        service.storage_lifecycle = cast(Any, Lifecycle())
        service.cloud_archives = cast(Any, Cloud())

        self.assertEqual(asyncio.run(service._active_tick()), "storage_pressure")
        self.assertEqual(order, ["maintain:True", "cloud:False", "reassess"])
        self.assertEqual(service._thread_workers, set())

    def test_warning_clears_cache_and_compacts_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = StorageLayout.create(directory)
            database = Database(Path(directory) / "noyra.sqlite3")
            subject_id = "Noyra-storage-lifecycle-test"
            IdentityStore(database).ensure(subject_id, "a" * 64)
            snapshots = SnapshotStore(database)
            for version in range(1, 21):
                snapshots.save(
                    subject_id, {"version": version}, state_version=version, reason="test"
                )
            (layout.cache / "temporary.bin").write_bytes(b"x" * 100)
            usage = sum(
                path.stat().st_size
                for path in Path(directory).glob("noyra.sqlite3*")
                if path.is_file()
            )
            manager = StorageLifecycleManager(
                database,
                subject_id,
                layout,
                StorageQuota(
                    subject_bytes=max(10_000_000, usage * 2),
                    training_bytes=1_000_000,
                    workspace_bytes=1_000_000,
                    warning_ratio=0.01,
                ),
                minimum_free_bytes=10_000_000,
            )
            result = manager.maintain()
            self.assertFalse((layout.cache / "temporary.bin").exists())
            self.assertIn("subject", result.warnings)
            self.assertEqual(snapshots.verify_archives(subject_id), 1)

    def test_cold_event_payloads_move_to_encrypted_segments_and_remain_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = StorageLayout.create(directory)
            database = Database(Path(directory) / "noyra.sqlite3")
            subject_id = "Noyra-event-archive-test"
            IdentityStore(database).ensure(subject_id, "b" * 64)
            events = EventStore(database)
            old = events.append(
                subject_id,
                "old_experience",
                "test",
                {"large": "evidence" * 1_000},
                occurred_at="2020-01-01T00:00:00+00:00",
            )
            key = base64.urlsafe_b64encode(b"k" * 32).decode()
            with patch.dict(os.environ, {"NOYRA_ARCHIVE_ENCRYPTION_KEY": key}):
                manager = StorageLifecycleManager(
                    database,
                    subject_id,
                    layout,
                    StorageQuota(),
                    event_payload_retention_days=30,
                    minimum_free_bytes=10_000_000,
                )
                result = manager.maintain()
                restored = events.get(old.event_id)
                event_archive = manager.event_archive
                self.assertIsNotNone(event_archive)
                assert event_archive is not None
                archive_root = event_archive.root
            self.assertIn("event_payloads_archived:1", result.actions)
            self.assertEqual(restored.payload["large"], "evidence" * 1_000)
            with database.connection() as connection:
                row = connection.execute(
                    "SELECT payload_json, payload_archive_key FROM events WHERE event_id = ?",
                    (old.event_id,),
                ).fetchone()
            self.assertEqual(row["payload_json"], "{}")
            self.assertIsNotNone(row["payload_archive_key"])
            self.assertEqual(
                archive_root,
                layout.subject / IdentityStore(database).storage_key(subject_id) / "cold",
            )
            archive_file = next((archive_root / "events").iterdir())
            self.assertNotIn(b"evidence", archive_file.read_bytes())

            with database.connection() as connection:
                segment = connection.execute(
                    "SELECT archive_format, encryption_key_id, encryption_key_fingerprint "
                    "FROM event_payload_segments WHERE subject_id = ?",
                    (subject_id,),
                ).fetchone()
            self.assertEqual(segment["archive_format"], "noyra-event-payload-segment-v1")
            self.assertIsNotNone(segment["encryption_key_id"])
            self.assertIsNotNone(segment["encryption_key_fingerprint"])
            wrong_key = base64.urlsafe_b64encode(b"z" * 32).decode()
            with (
                patch.dict(os.environ, {"NOYRA_ARCHIVE_ENCRYPTION_KEY": wrong_key}),
                self.assertRaises(IntegrityError),
            ):
                EventStore(database).get(old.event_id)
            with (
                patch.dict(os.environ, {"NOYRA_ARCHIVE_ENCRYPTION_KEY": ""}),
                self.assertRaises(IntegrityError),
            ):
                EventStore(database).get(old.event_id)

            with patch.dict(os.environ, {"NOYRA_ARCHIVE_ENCRYPTION_KEY": key}):
                artifact = RuntimeLogExporter(database).export(
                    subject_id,
                    actor="test",
                )
            with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
                events_jsonl = archive.read("tables/events.jsonl").decode("utf-8")
            self.assertIn('"payload_json":"{\\"large\\":', events_jsonl)
            self.assertNotIn('"payload_json":"{}"', events_jsonl)

    def test_subject_quota_prunes_old_export_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = StorageLayout.create(directory)
            database = Database(Path(directory) / "noyra.sqlite3")
            subject_id = "Noyra-export-prune-test"
            IdentityStore(database).ensure(subject_id, "c" * 64)
            jobs_root = layout.exports / "jobs"
            jobs_root.mkdir(parents=True, exist_ok=True)
            artifact = jobs_root / "job_old.zip"
            artifact.write_bytes(b"x" * 10_000)
            with database.transaction() as connection:
                connection.execute(
                    """INSERT INTO export_jobs(
                        job_id, subject_id, export_kind, status, artifact_path,
                        filename, sha256, byte_size, created_at, completed_at
                    ) VALUES (?, ?, 'training', 'completed', ?, ?, ?, ?, ?, ?)""",
                    (
                        "job_old",
                        subject_id,
                        str(artifact),
                        "old.zip",
                        "a" * 64,
                        10_000,
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
            manager = StorageLifecycleManager(
                database,
                subject_id,
                layout,
                StorageQuota(subject_bytes=1, warning_ratio=0.5),
                minimum_free_bytes=10_000_000,
            )
            result = manager.maintain()
            self.assertFalse(artifact.exists())
            self.assertIn("export_artifacts_pruned:1", result.actions)
            with database.connection() as connection:
                row = connection.execute(
                    "SELECT artifact_path, byte_size, error_code FROM export_jobs "
                    "WHERE job_id = 'job_old'"
                ).fetchone()
            self.assertIsNone(row["artifact_path"])
            self.assertIsNone(row["byte_size"])
            self.assertEqual(row["error_code"], "artifact_pruned")

    def test_export_prune_delete_failure_keeps_recoverable_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = StorageLayout.create(directory)
            database = Database(Path(directory) / "noyra.sqlite3")
            subject_id = "Noyra-export-prune-delete-failure"
            IdentityStore(database).ensure(subject_id, "d" * 64)
            artifact = layout.exports / "jobs" / "job_retry.zip"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(b"retry")
            with database.transaction() as connection:
                connection.execute(
                    """INSERT INTO export_jobs(
                        job_id, subject_id, export_kind, status, artifact_path,
                        filename, sha256, byte_size, created_at, completed_at
                    ) VALUES (?, ?, 'runtime', 'completed', ?, ?, ?, ?, ?, ?)""",
                    (
                        "job_retry",
                        subject_id,
                        str(artifact),
                        "retry.zip",
                        "b" * 64,
                        artifact.stat().st_size,
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
            manager = StorageLifecycleManager(
                database,
                subject_id,
                layout,
                StorageQuota(subject_bytes=1, warning_ratio=0.5),
                minimum_free_bytes=10_000_000,
            )

            original_unlink = Path.unlink

            def fail_artifact_unlink(path: Path, *, missing_ok: bool = False) -> None:
                if path == artifact:
                    raise OSError("injected artifact delete failure")
                original_unlink(path, missing_ok=missing_ok)

            with patch.object(Path, "unlink", fail_artifact_unlink):
                self.assertEqual(manager._prune_export_artifacts(force=True), 0)

            self.assertTrue(artifact.exists())
            with database.connection() as connection:
                pending = connection.execute(
                    "SELECT artifact_path, byte_size, error_code FROM export_jobs "
                    "WHERE job_id = 'job_retry'"
                ).fetchone()
            self.assertEqual(pending["artifact_path"], str(artifact))
            self.assertEqual(pending["byte_size"], 5)
            self.assertEqual(pending["error_code"], "artifact_pruning")

            with patch.object(
                manager.scanner,
                "scan",
                return_value=SimpleNamespace(subject_bytes=0),
            ):
                self.assertEqual(manager._prune_export_artifacts(), 1)
            self.assertFalse(artifact.exists())
            with database.connection() as connection:
                finalized = connection.execute(
                    "SELECT artifact_path, byte_size, error_code FROM export_jobs "
                    "WHERE job_id = 'job_retry'"
                ).fetchone()
            self.assertIsNone(finalized["artifact_path"])
            self.assertIsNone(finalized["byte_size"])
            self.assertEqual(finalized["error_code"], "artifact_pruned")

    def test_export_prune_recovers_after_delete_before_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = StorageLayout.create(directory)
            database = Database(Path(directory) / "noyra.sqlite3")
            subject_id = "Noyra-export-prune-finalize-recovery"
            IdentityStore(database).ensure(subject_id, "e" * 64)
            artifact = layout.exports / "jobs" / "job_finalize.zip"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(b"finalize")
            with database.transaction() as connection:
                connection.execute(
                    """INSERT INTO export_jobs(
                        job_id, subject_id, export_kind, status, artifact_path,
                        filename, sha256, byte_size, created_at, completed_at
                    ) VALUES (?, ?, 'runtime', 'completed', ?, ?, ?, ?, ?, ?)""",
                    (
                        "job_finalize",
                        subject_id,
                        str(artifact),
                        "finalize.zip",
                        "c" * 64,
                        artifact.stat().st_size,
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
            manager = StorageLifecycleManager(
                database,
                subject_id,
                layout,
                StorageQuota(subject_bytes=1, warning_ratio=0.5),
                minimum_free_bytes=10_000_000,
            )

            transaction_calls = 0
            original_transaction = database.transaction

            @contextmanager
            def fail_finalize_transaction() -> Any:
                nonlocal transaction_calls
                transaction_calls += 1
                if transaction_calls == 2:
                    raise sqlite3.OperationalError("injected prune finalization failure")
                with original_transaction() as connection:
                    yield connection

            with (
                patch.object(database, "transaction", fail_finalize_transaction),
                self.assertRaisesRegex(sqlite3.OperationalError, "finalization failure"),
            ):
                manager._prune_export_artifacts(force=True)

            self.assertFalse(artifact.exists())
            with database.connection() as connection:
                pending = connection.execute(
                    "SELECT artifact_path, byte_size, error_code FROM export_jobs "
                    "WHERE job_id = 'job_finalize'"
                ).fetchone()
            self.assertEqual(pending["artifact_path"], str(artifact))
            self.assertEqual(pending["byte_size"], 8)
            self.assertEqual(pending["error_code"], "artifact_pruning")

            self.assertEqual(manager._prune_export_artifacts(force=True), 1)
            with database.connection() as connection:
                row = connection.execute(
                    "SELECT artifact_path, byte_size, error_code FROM export_jobs "
                    "WHERE job_id = 'job_finalize'"
                ).fetchone()
            self.assertIsNone(row["artifact_path"])
            self.assertIsNone(row["byte_size"])
            self.assertEqual(row["error_code"], "artifact_pruned")

    def test_export_manager_retries_recent_pending_prune(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = StorageLayout.create(directory)
            database = Database(Path(directory) / "noyra.sqlite3")
            subject_id = "Noyra-export-manager-prune-recovery"
            IdentityStore(database).ensure(subject_id, "f" * 64)
            artifact = layout.exports / "jobs" / "job_recent.zip"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            with database.transaction() as connection:
                connection.execute(
                    """INSERT INTO export_jobs(
                        job_id, subject_id, export_kind, status, artifact_path,
                        filename, sha256, byte_size, error_code, created_at, completed_at
                    ) VALUES (?, ?, 'runtime', 'completed', ?, ?, ?, ?,
                        'artifact_pruning', ?, ?)""",
                    (
                        "job_recent",
                        subject_id,
                        str(artifact),
                        "recent.zip",
                        "d" * 64,
                        7,
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
            manager = ExportJobManager(
                database,
                artifact.parent,
                lambda *_args: ("unused.zip", "unused"),
            )
            try:
                manager._prune_completed_artifacts(subject_id)
            finally:
                manager.close()

            with database.connection() as connection:
                row = connection.execute(
                    "SELECT artifact_path, byte_size, error_code FROM export_jobs "
                    "WHERE job_id = 'job_recent'"
                ).fetchone()
            self.assertIsNone(row["artifact_path"])
            self.assertIsNone(row["byte_size"])
            self.assertEqual(row["error_code"], "artifact_pruned")
