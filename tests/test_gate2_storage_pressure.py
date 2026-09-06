from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from noyra.core import (
    CloudArchiveCoordinator,
    IdentityStore,
    SnapshotStore,
    StorageLayout,
    StorageLifecycleManager,
    StorageQuota,
)
from noyra.core.database import Database
from noyra.core.storage import StorageUsage
from noyra.core.types import content_hash


def test_snapshot_compaction_uses_bounded_batches_and_byte_budget() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "gate2-snapshot-bounded"
        IdentityStore(database).ensure(subject_id, "a" * 64)
        store = SnapshotStore(database)
        for version in range(1, 38):
            store.save(
                subject_id,
                {"version": version, "payload": "x" * 80},
                state_version=version,
                reason="gate2",
            )
        checkpoints: list[str] = []
        result = store.compact(
            subject_id,
            keep_recent=5,
            batch_size=4,
            max_batch_bytes=500,
            checkpoint=lambda: checkpoints.append("tick"),
        )
        assert result["archived"] == 32
        assert checkpoints
        with database.connection() as connection:
            archives = connection.execute(
                "SELECT snapshot_count, first_version, last_version FROM snapshot_archives "
                "WHERE subject_id = ? ORDER BY first_version",
                (subject_id,),
            ).fetchall()
            online = connection.execute(
                "SELECT state_version FROM state_snapshots WHERE subject_id = ? "
                "ORDER BY state_version",
                (subject_id,),
            ).fetchall()
        assert len(archives) > 1
        assert all(int(row["snapshot_count"]) <= 4 for row in archives)
        assert [int(row["state_version"]) for row in online] == [33, 34, 35, 36, 37]
        assert store.verify_archives(subject_id) == len(archives)


def test_snapshot_compaction_enforces_total_row_and_byte_budgets() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "gate2-snapshot-total-budgets"
        IdentityStore(database).ensure(subject_id, "d" * 64)
        store = SnapshotStore(database)
        for version in range(1, 13):
            store.save(
                subject_id,
                {"version": version, "payload": "x" * (40 + version)},
                state_version=version,
                reason="gate2",
            )

        row_limited = store.compact(
            subject_id,
            keep_recent=2,
            batch_size=100,
            max_rows=3,
            max_bytes=1_000_000,
        )
        assert row_limited["archived"] == 3
        with database.connection() as connection:
            remaining_after_rows = connection.execute(
                "SELECT COUNT(*) FROM state_snapshots WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()[0]
        assert remaining_after_rows == 9

        with database.connection() as connection:
            next_size = len(
                str(
                    connection.execute(
                        "SELECT state_json FROM state_snapshots WHERE subject_id = ? "
                        "ORDER BY state_version LIMIT 1",
                        (subject_id,),
                    ).fetchone()[0]
                ).encode("utf-8")
            )
        byte_limited = store.compact(
            subject_id,
            keep_recent=2,
            batch_size=100,
            max_rows=100,
            max_bytes=next_size,
        )
        assert byte_limited["archived"] == 1

        blocked = store.compact(
            subject_id,
            keep_recent=2,
            batch_size=100,
            max_rows=100,
            max_bytes=1,
        )
        assert blocked["archived"] == 0


def test_snapshot_hard_byte_budget_checks_sqlite_length_before_loading_text() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "gate2-snapshot-preload-budget"
        IdentityStore(database).ensure(subject_id, "1" * 64)
        store = SnapshotStore(database)
        for version, size in ((1, 1_000_000), (2, 8), (3, 8)):
            store.save(
                subject_id,
                {"version": version, "payload": "x" * size},
                state_version=version,
                reason="gate2",
            )
        statements: list[str] = []
        original_connection = database.connection

        @contextmanager
        def traced_connection() -> Iterator[Any]:
            with original_connection() as connection:
                connection.set_trace_callback(statements.append)
                try:
                    yield connection
                finally:
                    connection.set_trace_callback(None)

        database.connection = traced_connection  # type: ignore[method-assign]
        result = store.compact(
            subject_id,
            keep_recent=2,
            max_rows=10,
            max_bytes=32,
        )

        assert result["archived"] == 0
        assert any("length(CAST(state_json AS BLOB))" in sql for sql in statements)
        assert not any(
            sql.lstrip().startswith("SELECT state_json FROM state_snapshots") for sql in statements
        )


def test_snapshot_batch_byte_budget_blocks_oversized_first_row_before_materialization() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "gate2-snapshot-batch-budget"
        IdentityStore(database).ensure(subject_id, "3" * 64)
        store = SnapshotStore(database)
        for version, size in ((1, 10_000), (2, 8), (3, 8)):
            store.save(
                subject_id,
                {"version": version, "payload": "x" * size},
                state_version=version,
                reason="gate2",
            )
        statements: list[str] = []
        original_connection = database.connection

        @contextmanager
        def traced_connection() -> Iterator[Any]:
            with original_connection() as connection:
                connection.set_trace_callback(statements.append)
                try:
                    yield connection
                finally:
                    connection.set_trace_callback(None)

        database.connection = traced_connection  # type: ignore[method-assign]
        result = store.compact(subject_id, keep_recent=2, max_batch_bytes=32)

        assert result["archived"] == 0
        assert any("length(CAST(state_json AS BLOB))" in sql for sql in statements)
        assert not any(
            "SELECT state_json, length(CAST(state_json AS BLOB))" in sql for sql in statements
        )


def test_snapshot_batch_byte_budget_accumulates_multiple_rows_without_double_counting() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "gate2-snapshot-batch-accumulator"
        IdentityStore(database).ensure(subject_id, "4" * 64)
        store = SnapshotStore(database)
        for version in range(1, 6):
            store.save(
                subject_id,
                {"version": version, "payload": "x" * 64},
                state_version=version,
                reason="gate2",
            )
        with database.connection() as connection:
            row_bytes = int(
                connection.execute(
                    "SELECT length(CAST(state_json AS BLOB)) FROM state_snapshots "
                    "WHERE subject_id = ? ORDER BY state_version LIMIT 1",
                    (subject_id,),
                ).fetchone()[0]
            )

        result = store.compact(
            subject_id,
            keep_recent=2,
            batch_size=10,
            max_rows=2,
            max_batch_bytes=row_bytes * 2 + 1,
        )

        assert result["archived"] == 2


def test_critical_preflight_skips_write_amplifying_stages() -> None:
    with tempfile.TemporaryDirectory() as directory:
        layout = StorageLayout.create(directory)
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "gate2-lifecycle-critical"
        IdentityStore(database).ensure(subject_id, "b" * 64)
        manager = StorageLifecycleManager(
            database,
            subject_id,
            layout,
            StorageQuota(),
            minimum_free_bytes=10_000_000,
        )
        artifact = layout.exports / "critical.zip"
        artifact.write_bytes(b"x" * 64)
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO export_jobs(
                    job_id, subject_id, export_kind, status, artifact_path,
                    filename, sha256, byte_size, created_at, completed_at
                ) VALUES (?, ?, 'runtime', 'completed', ?, ?, ?, ?, ?, ?)""",
                (
                    "gate2-critical-export",
                    subject_id,
                    str(artifact),
                    "critical.zip",
                    "a" * 64,
                    64,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        usage = StorageUsage(
            subject_bytes=1,
            training_bytes=0,
            workspace_bytes=0,
            free_bytes=1,
        )
        manager.scanner.scan = Mock(return_value=usage)  # type: ignore[method-assign]
        event_archive = Mock()
        observation_archive = Mock()
        manager.event_archive = cast(Any, SimpleNamespace(archive_cold=event_archive))
        manager.observation_archive = cast(Any, SimpleNamespace(archive_cold=observation_archive))
        manager.models.compress_cold_payloads = Mock(return_value=0)  # type: ignore[method-assign]
        manager.snapshots.compact = Mock(return_value={"archived": 0, "compressed_bytes": 0})  # type: ignore[method-assign]
        manager.usage_history.record_if_due = Mock(  # type: ignore[method-assign]
            side_effect=AssertionError("critical pressure must not append usage telemetry")
        )
        manager.events.append = Mock(  # type: ignore[method-assign]
            side_effect=AssertionError("critical pressure must not append an event")
        )
        result = manager.maintain()
        event_archive.assert_not_called()
        observation_archive.assert_not_called()
        manager.models.compress_cold_payloads.assert_not_called()
        manager.snapshots.compact.assert_not_called()
        manager.usage_history.record_if_due.assert_not_called()
        manager.events.append.assert_not_called()
        assert not artifact.exists()
        assert result.cognition_allowed is False
        assert result.write_amplification_allowed is False


def test_sqlite_full_during_maintenance_returns_fail_closed_pressure_result() -> None:
    with tempfile.TemporaryDirectory() as directory:
        layout = StorageLayout.create(directory)
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "gate2-sqlite-full"
        IdentityStore(database).ensure(subject_id, "f" * 64)
        manager = StorageLifecycleManager(
            database,
            subject_id,
            layout,
            StorageQuota(),
            minimum_free_bytes=10,
            initialize_archives=False,
        )
        usage = StorageUsage(
            subject_bytes=1,
            training_bytes=0,
            workspace_bytes=0,
            free_bytes=100_000_000,
        )
        manager.scanner.scan = Mock(return_value=usage)  # type: ignore[method-assign]
        manager.usage_history.record_if_due = Mock(  # type: ignore[method-assign]
            side_effect=sqlite3.OperationalError("database or disk is full")
        )

        result = manager.maintain()

        assert result.cognition_allowed is False
        assert result.write_amplification_allowed is False
        assert "storage_pressure_database_io" in result.actions


def test_unrelated_sqlite_operational_error_is_not_hidden() -> None:
    with tempfile.TemporaryDirectory() as directory:
        layout = StorageLayout.create(directory)
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "gate2-sqlite-other-error"
        IdentityStore(database).ensure(subject_id, "0" * 64)
        manager = StorageLifecycleManager(
            database,
            subject_id,
            layout,
            StorageQuota(),
            minimum_free_bytes=10,
            initialize_archives=False,
        )
        usage = StorageUsage(
            subject_bytes=1,
            training_bytes=0,
            workspace_bytes=0,
            free_bytes=100_000_000,
        )
        manager.scanner.scan = Mock(return_value=usage)  # type: ignore[method-assign]
        manager.usage_history.record_if_due = Mock(  # type: ignore[method-assign]
            side_effect=sqlite3.OperationalError("database is malformed")
        )

        with pytest.raises(sqlite3.OperationalError, match="malformed"):
            manager.maintain()


def test_sqlite_ioerr_during_maintenance_event_returns_fail_closed_result() -> None:
    with tempfile.TemporaryDirectory() as directory:
        layout = StorageLayout.create(directory)
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "gate2-sqlite-ioerr"
        IdentityStore(database).ensure(subject_id, "2" * 64)
        manager = StorageLifecycleManager(
            database,
            subject_id,
            layout,
            StorageQuota(subject_bytes=100),
            minimum_free_bytes=10,
            initialize_archives=False,
        )
        usage = StorageUsage(
            subject_bytes=90,
            training_bytes=0,
            workspace_bytes=0,
            free_bytes=100_000_000,
        )
        manager.scanner.scan = Mock(return_value=usage)  # type: ignore[method-assign]
        manager.usage_history.record_if_due = Mock(return_value=None)  # type: ignore[method-assign]
        manager.usage_history.predict = Mock(return_value=manager._empty_trend())  # type: ignore[method-assign]
        manager._log_due = Mock(return_value=True)  # type: ignore[method-assign]
        manager.events.append = Mock(  # type: ignore[method-assign]
            side_effect=sqlite3.OperationalError("disk I/O error")
        )

        result = manager.maintain()

        assert result.cognition_allowed is False
        assert result.write_amplification_allowed is False
        assert "storage_pressure_database_io" in result.actions


def test_noncritical_quota_pressure_blocks_write_amplification() -> None:
    for quota_domain in ("training", "workspace"):
        with tempfile.TemporaryDirectory() as directory:
            layout = StorageLayout.create(directory)
            database = Database(Path(directory) / "noyra.sqlite3")
            subject_id = f"gate2-{quota_domain}-quota-pressure"
            IdentityStore(database).ensure(subject_id, "f" * 64)
            manager = StorageLifecycleManager(
                database,
                subject_id,
                layout,
                StorageQuota(subject_bytes=100, training_bytes=100, workspace_bytes=100),
                minimum_free_bytes=10_000_000,
                initialize_archives=False,
            )
            usage = StorageUsage(
                subject_bytes=1,
                training_bytes=101 if quota_domain == "training" else 1,
                workspace_bytes=101 if quota_domain == "workspace" else 1,
                free_bytes=20_000_000,
            )
            manager.scanner.scan = Mock(return_value=usage)  # type: ignore[method-assign]
            manager.models.compress_cold_payloads = Mock(return_value=0)  # type: ignore[method-assign]
            manager.snapshots.compact = Mock(  # type: ignore[method-assign]
                return_value={"archived": 0, "compressed_bytes": 0}
            )

            result = manager.maintain()

            assert result.over_quota == (quota_domain,)
            assert result.cognition_allowed is True
            assert result.write_amplification_allowed is False
            manager.models.compress_cold_payloads.assert_not_called()
            manager.snapshots.compact.assert_not_called()


def test_cloud_archive_no_staging_mode_does_not_copy_snapshot_payloads() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        layout = StorageLayout.create(root)
        database = Database(root / "noyra.sqlite3")
        subject_id = "gate2-cloud-pressure-no-staging"
        IdentityStore(database).ensure(subject_id, "e" * 64)
        store = SnapshotStore(database)
        for version in range(1, 6):
            store.save(
                subject_id,
                {"version": version, "payload": "pressure" * 20},
                state_version=version,
                reason="gate2",
            )
        assert store.compact(subject_id, keep_recent=1)["archived"] > 0

        class MemoryProvider:
            name = "memory"

            def __init__(self) -> None:
                self.objects: dict[str, bytes] = {}

            def put(self, object_key: str, payload: bytes) -> str:
                self.objects[object_key] = payload
                return hashlib.sha256(payload).hexdigest()

            def get(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
                payload = self.objects[object_key]
                if max_bytes is not None and len(payload) > max_bytes:
                    raise OSError("payload exceeds test limit")
                return payload

            def exists(self, object_key: str) -> bool:
                return object_key in self.objects

        provider = MemoryProvider()
        coordinator = CloudArchiveCoordinator(
            database,
            subject_id,
            layout.training_raw,
            provider,
        )
        coordinator.keyring = None

        result = coordinator.tick(garbage_collect_local=True, allow_staging=False)

        assert result == {
            "uploaded": 0,
            "failed": 0,
            "dead": 0,
            "garbage_collected": 0,
        }
        assert provider.objects == {}
        with database.connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM snapshot_archives WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()[0]
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM storage_archives WHERE subject_id = ?",
                    (subject_id,),
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM archive_transfer_queue WHERE subject_id = ?",
                    (subject_id,),
                ).fetchone()[0]
                == 0
            )


def test_lifecycle_snapshot_compaction_has_per_tick_work_budgets() -> None:
    with tempfile.TemporaryDirectory() as directory:
        layout = StorageLayout.create(directory)
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "Noyra-gate2-compaction-budget"
        IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
        manager = StorageLifecycleManager(database, subject_id, layout, StorageQuota())
        manager.snapshots.compact = Mock(  # type: ignore[method-assign]
            return_value={"archived": 0, "compressed_bytes": 0}
        )

        manager._compact_snapshots(lambda: None)

        manager.snapshots.compact.assert_called_once()
        args, kwargs = manager.snapshots.compact.call_args
        assert args == (subject_id,)
        assert kwargs["keep_recent"] == 16
        assert callable(kwargs["checkpoint"])
        assert kwargs["max_rows"] == 2_048
        assert kwargs["max_bytes"] == 64_000_000


def test_snapshot_compaction_cas_does_not_duplicate_under_concurrent_compactors() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "gate2-snapshot-concurrent"
        IdentityStore(database).ensure(subject_id, "c" * 64)
        for version in range(1, 18):
            SnapshotStore(database).save(
                subject_id,
                {"version": version},
                state_version=version,
                reason="gate2",
            )

        started = Event()
        release = Event()

        class PausingStore(SnapshotStore):
            def _build_archive_payload(self, rows: list[dict[str, Any]], checkpoint: Any) -> Any:
                started.set()
                assert release.wait(5)
                return super()._build_archive_payload(rows, checkpoint)

        first = PausingStore(database)
        second = SnapshotStore(database)
        results: list[dict[str, int]] = []
        first_thread = Thread(
            target=lambda: results.append(first.compact(subject_id, keep_recent=3, batch_size=4))
        )
        second_thread = Thread(
            target=lambda: results.append(second.compact(subject_id, keep_recent=3, batch_size=4))
        )
        first_thread.start()
        assert started.wait(5)
        second_thread.start()
        second_thread.join(10)
        release.set()
        first_thread.join(10)
        assert not first_thread.is_alive()
        assert not second_thread.is_alive()
        assert sum(result["archived"] for result in results) == 14
        with database.connection() as connection:
            archives = connection.execute(
                "SELECT snapshot_count FROM snapshot_archives WHERE subject_id = ?",
                (subject_id,),
            ).fetchall()
            remaining = connection.execute(
                "SELECT COUNT(*) FROM state_snapshots WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()[0]
        assert sum(int(row["snapshot_count"]) for row in archives) == 14
        assert remaining == 3
