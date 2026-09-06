from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from noyra.core import Database, EventStore, IdentityStore, StorageUsageScanner, TrainingStore
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.training_export import TrainingDatasetExporter
from noyra.core.types import content_hash


def _database(root: Path, subject_id: str) -> Database:
    database = Database(root / "noyra.sqlite3")
    IdentityStore(database).ensure(subject_id, content_hash({"subject_id": subject_id}))
    TrainingStore(database).ensure_policy(subject_id)
    EventStore(database).append(
        subject_id,
        "p216-export-event",
        "test",
        {"content": "snapshot contract"},
        privacy_level="public",
    )
    return database


def _deny_live_read_transaction(database: Database) -> None:
    def fail() -> None:
        pytest.fail("long-running export must not open a live read transaction")

    database.read_transaction = fail  # type: ignore[assignment]


def test_runtime_export_uses_isolated_backup_and_releases_live_wal(tmp_path: Path) -> None:
    subject_id = "Noyra-p216-runtime"
    database = _database(tmp_path, subject_id)
    exporter = RuntimeLogExporter(database)
    opened: list[str] = []
    original_snapshot = database.read_snapshot

    @contextmanager
    def tracked_snapshot(*, checkpoint: Callable[[], None] | None = None) -> Iterator[Any]:
        opened.append("open")
        with original_snapshot(checkpoint=checkpoint) as connection:
            yield connection
        opened.append("closed")

    database.read_snapshot = tracked_snapshot  # type: ignore[method-assign]
    _deny_live_read_transaction(database)
    target = tmp_path / "exports" / "runtime.zip"

    artifact = exporter.export_to_path(subject_id, actor="test", target=target)

    assert artifact.row_count >= 1
    assert target.is_file()
    assert opened == ["open", "closed"]
    assert not list(tmp_path.glob(".noyra.sqlite3.snapshot_*.sqlite*"))
    with zipfile.ZipFile(target) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["subject_id"] == subject_id
        assert manifest["total_rows"] == manifest["expected_total_rows"]
    with database.connection() as connection:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert int(checkpoint[0]) == 0


def test_training_export_uses_isolated_backup_for_stream_and_legacy_collection(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p216-training"
    database = _database(tmp_path, subject_id)
    exporter = TrainingDatasetExporter(database)
    calls: list[str] = []
    original_snapshot = database.read_snapshot

    @contextmanager
    def tracked_snapshot(*, checkpoint: Callable[[], None] | None = None) -> Iterator[Any]:
        calls.append("open")
        with original_snapshot(checkpoint=checkpoint) as connection:
            yield connection
        calls.append("closed")

    database.read_snapshot = tracked_snapshot  # type: ignore[method-assign]
    _deny_live_read_transaction(database)
    target = tmp_path / "exports" / "training.zip"

    artifact = exporter.export_to_path(subject_id, actor="test", target=target)

    assert artifact.row_count >= 1
    assert target.is_file()
    assert calls == ["open", "closed"]
    with zipfile.ZipFile(target) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["subject_id"] == subject_id
        assert manifest["row_count"] >= 1

    _, _, _, rows, model_io_rows = exporter._collect_data(subject_id)
    assert rows
    assert model_io_rows == []
    assert calls == ["open", "closed", "open", "closed"]
    assert not list(tmp_path.glob(".noyra.sqlite3.snapshot_*.sqlite*"))


def test_snapshot_copy_releases_live_wal_before_long_serialization(tmp_path: Path) -> None:
    subject_id = "Noyra-p216-wal"
    database = _database(tmp_path, subject_id)
    exporter = RuntimeLogExporter(database)
    entered = threading.Event()
    release = threading.Event()
    original_snapshot = database.read_snapshot

    @contextmanager
    def blocked_snapshot(*, checkpoint: Callable[[], None] | None = None) -> Iterator[Any]:
        with original_snapshot(checkpoint=checkpoint) as connection:
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test snapshot was not released")
            yield connection

    database.read_snapshot = blocked_snapshot  # type: ignore[method-assign]
    target = tmp_path / "exports" / "runtime-wal.zip"
    failure: list[BaseException] = []

    def run_export() -> None:
        try:
            exporter.export_to_path(subject_id, actor="test", target=target)
        except BaseException as error:  # pragma: no cover - surfaced below
            failure.append(error)

    worker = threading.Thread(target=run_export)
    worker.start()
    assert entered.wait(5)
    EventStore(database).append(
        subject_id,
        "p216-live-write",
        "test",
        {"content": "write while archive is paused"},
        privacy_level="public",
    )
    with database.connection() as connection:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert int(checkpoint[0]) == 0
    release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert not failure
    with zipfile.ZipFile(target) as archive:
        assert b"p216-live-write" not in archive.read("tables/events.jsonl")


def test_stale_snapshot_is_accounted_and_scavenged(tmp_path: Path) -> None:
    subject_id = "Noyra-p216-cleanup"
    database = _database(tmp_path, subject_id)
    with database.read_snapshot() as connection:
        active = next(tmp_path.glob(".noyra.sqlite3.snapshot_*.sqlite"))
        old = time.time() - (database._SNAPSHOT_STALE_AFTER_SECONDS + 5)
        os.utime(active, (old, old))
        assert database._cleanup_stale_snapshots() == 0
        assert active.exists()
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        assert StorageUsageScanner(tmp_path).scan().subject_bytes >= active.stat().st_size
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden_snapshot_write(id INTEGER)")
    assert not active.exists()

    orphan = tmp_path / ".noyra.sqlite3.snapshot_orphan.sqlite"
    shutil.copy2(database.path, orphan)
    old = time.time() - (database._SNAPSHOT_STALE_AFTER_SECONDS + 5)
    os.utime(orphan, (old, old))
    usage = StorageUsageScanner(tmp_path).scan()
    assert usage.subject_bytes >= orphan.stat().st_size
    with database.read_snapshot() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    assert not orphan.exists()
