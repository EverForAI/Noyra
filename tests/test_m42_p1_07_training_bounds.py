from __future__ import annotations

import base64
import errno
import hashlib
import json
import shutil
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import noyra.core.training_export as training_export_module
from noyra.core import (
    Database,
    EventStore,
    ExportControl,
    IdentityStore,
    TrainingDatasetBuilder,
    TrainingDatasetExporter,
    TrainingExportLimitError,
    TrainingExportLimits,
    TrainingStore,
)
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.export_jobs import ExportCancelledError
from noyra.core.types import content_hash
from noyra.model import ModelLedger


def _database(root: Path, subject_id: str) -> Database:
    root.mkdir(parents=True, exist_ok=True)
    database = Database(root / "noyra.sqlite3")
    IdentityStore(database).ensure(subject_id, content_hash({"subject_id": subject_id}))
    TrainingStore(database).ensure_policy(subject_id)
    return database


def _append_events(
    database: Database,
    subject_id: str,
    offsets: list[int],
    *,
    payload_size: int = 0,
) -> list[str]:
    events = EventStore(database)
    start = datetime(2026, 8, 15, 0, 0, tzinfo=UTC)
    event_ids: list[str] = []
    for index, offset in enumerate(offsets):
        event = events.append(
            subject_id,
            "public_observation",
            "p107-test",
            {"index": index, "content": "x" * payload_size},
            privacy_level="public",
            occurred_at=(start + timedelta(seconds=offset)).isoformat(),
            event_id=f"evt-p107-{index:05d}",
        )
        event_ids.append(event.event_id)
    return event_ids


def _dataset_rows(
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    dataset: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for shard in manifest["datasets"][dataset]:
        name = str(shard["file"])
        payload = archive.read(name)
        lines = payload.splitlines()
        assert len(payload) == int(shard["bytes"])
        assert len(lines) == int(shard["rows"])
        assert hashlib.sha256(payload).hexdigest() == shard["sha256"]
        result.extend(json.loads(line) for line in lines)
    return result


def _assert_no_transient_files(exporter: TrainingDatasetExporter, target: Path) -> None:
    assert exporter.work_root.is_dir()
    assert not list(exporter.work_root.iterdir())
    assert not list(target.parent.glob(".*.pending"))


def test_export_query_plans_stream_without_unmanaged_temp_sorts(tmp_path: Path) -> None:
    subject_id = "Noyra-p107-query-plan"
    database = _database(tmp_path, subject_id)
    with database.connection() as connection:
        event_plan = [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + training_export_module._ELIGIBLE_ITEMS_SQL,
                (2_000_000, subject_id, subject_id, 1),
            )
        ]
        model_plan = [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + training_export_module._MODEL_IO_SQL,
                (2_000_000, 2_000_000, subject_id),
            )
        ]

    assert any("idx_events_subject_export_time" in step for step in event_plan)
    assert any("idx_model_calls_subject_export_time" in step for step in model_plan)
    assert all("TEMP B-TREE" not in step for step in (*event_plan, *model_plan))


def test_training_provenance_writes_checkpoint_inside_wal_batches(tmp_path: Path) -> None:
    subject_id = "Noyra-p107-wal-budget"
    database = _database(tmp_path, subject_id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE training_policies SET record_enabled = 0 WHERE subject_id = ?",
            (subject_id,),
        )
    _append_events(database, subject_id, list(range(130)))
    with database.transaction() as connection:
        connection.execute(
            "UPDATE training_policies SET record_enabled = 1 WHERE subject_id = ?",
            (subject_id,),
        )

    checkpoints = 0

    def checkpoint() -> None:
        nonlocal checkpoints
        checkpoints += 1

    store = TrainingStore(database)
    assert store.backfill_all(subject_id, checkpoint=checkpoint) == 130
    assert checkpoints >= 3

    checkpoints = 0
    assert store.reclassify_all(subject_id, checkpoint=checkpoint) == 130
    assert checkpoints >= 4


def test_sqlite_progress_handler_cancels_a_scan_with_no_matching_rows(tmp_path: Path) -> None:
    subject_id = "Noyra-p107-scan-cancel"
    database = _database(tmp_path, subject_id)
    timestamp = "2026-08-15T00:00:00.000+00:00"
    payload_hash = content_hash({})
    event_rows = []
    training_rows = []
    for index in range(2_000):
        event_id = f"evt-p107-scan-{index:05d}"
        event_rows.append(
            (
                event_id,
                subject_id,
                "excluded_event",
                "p107-test",
                timestamp,
                timestamp,
                "{}",
                payload_hash,
                "public",
                "[]",
            )
        )
        training_rows.append(
            (
                f"train-p107-scan-{index:05d}",
                subject_id,
                event_id,
                "excluded_event",
                "public",
                payload_hash,
                timestamp,
                timestamp,
            )
        )
    with database.transaction() as connection:
        connection.executemany(
            "INSERT INTO events(event_id, subject_id, event_type, source, occurred_at, "
            "observed_at, payload_json, payload_hash, privacy_level, causal_parent_ids_json, "
            "processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'recorded')",
            event_rows,
        )
        connection.executemany(
            "INSERT INTO training_records(record_id, subject_id, event_id, record_kind, "
            "privacy_level, eligibility, source_hash, redaction_status, consent_version, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'excluded', ?, 'excluded', 1, ?, ?)",
            training_rows,
        )

    checkpoints = 0

    def cancel_scan() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 2:
            raise ExportCancelledError("cancelled indexed scan")

    exporter = TrainingDatasetExporter(database)
    with database.connection() as connection:
        with pytest.raises(ExportCancelledError, match="cancelled indexed scan"):
            list(
                exporter._iter_eligible_items(
                    subject_id,
                    1,
                    connection=connection,
                    checkpoint=cancel_scan,
                )
            )
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    assert checkpoints == 2

    checkpoints = 0
    with pytest.raises(ExportCancelledError, match="cancelled indexed scan"):
        TrainingStore(database).backfill_all(subject_id, checkpoint=cancel_scan)
    assert checkpoints == 2


def test_data_volume_work_root_disk_dedup_and_success_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "Noyra-p107-dedup"
    data_root = tmp_path / "data-volume"
    database = _database(data_root, subject_id)
    occurred_at = "2026-08-15T00:00:00+00:00"
    events = EventStore(database)
    for suffix in ("a", "b"):
        events.append(
            subject_id,
            "public_observation",
            "p107-test",
            {"content": "same logical event"},
            privacy_level="public",
            occurred_at=occurred_at,
            event_id=f"evt-p107-dedup-{suffix}",
        )

    exporter = TrainingDatasetExporter(database, data_root=data_root)
    temporary_roots: list[Path] = []
    fingerprint_paths: list[Path] = []
    original_temporary_directory = tempfile.TemporaryDirectory
    original_fingerprint_init = training_export_module._DiskFingerprintSet.__init__

    def tracked_temporary_directory(*args: Any, **kwargs: Any) -> Any:
        temporary_roots.append(Path(kwargs["dir"]).resolve())
        return original_temporary_directory(*args, **kwargs)

    def tracked_fingerprint_init(self: Any, path: Path, budget: Any) -> None:
        original_fingerprint_init(self, path, budget)
        fingerprint_paths.append(path)
        assert path.is_file()
        assert self.connection.execute("PRAGMA journal_mode").fetchone()[0] == "off"
        assert self.connection.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert self.connection.execute("PRAGMA cache_size").fetchone()[0] == -2048
        assert self.connection.execute("PRAGMA mmap_size").fetchone()[0] == 0
        assert budget.work_bytes >= path.stat().st_size

    monkeypatch.setattr(
        "noyra.core.training_export.tempfile.TemporaryDirectory",
        tracked_temporary_directory,
    )
    monkeypatch.setattr(
        training_export_module._DiskFingerprintSet,
        "__init__",
        tracked_fingerprint_init,
    )
    target = data_root / "exports" / "training.zip"

    exporter.export_to_path(subject_id, actor="p107-test", target=target)

    assert temporary_roots == [exporter.work_root]
    assert exporter.work_root == (data_root / "exports" / "work").resolve()
    assert len(fingerprint_paths) == 1
    assert exporter.work_root in fingerprint_paths[0].parents
    assert not fingerprint_paths[0].exists()
    with zipfile.ZipFile(target) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        events_rows = _dataset_rows(archive, manifest, "events.jsonl")
        episodes = _dataset_rows(archive, manifest, "episodes.jsonl")
    assert len(events_rows) == 2
    assert len(episodes) == 1
    assert len(episodes[0]["event_ids"]) == 1
    assert manifest["quality"]["event_count"] == 1
    assert manifest["quality"]["duplicate_count"] == 1
    assert manifest["bounds"]["deduplication"] == "sqlite-disk-backed-exact"
    _assert_no_transient_files(exporter, target)


@pytest.mark.parametrize(
    ("max_events", "max_span", "offsets", "expected_sizes"),
    [
        pytest.param(2, 10_000, [0, 1, 2, 3, 4], [2, 2, 1], id="event-count"),
        pytest.param(100, 10, [0, 6, 12], [2, 1], id="elapsed-time"),
    ],
)
def test_episode_assembly_obeys_event_and_elapsed_time_bounds(
    tmp_path: Path,
    max_events: int,
    max_span: int,
    offsets: list[int],
    expected_sizes: list[int],
) -> None:
    subject_id = f"Noyra-p107-episode-{max_events}-{max_span}"
    database = _database(tmp_path, subject_id)
    _append_events(database, subject_id, offsets)
    limits = TrainingExportLimits(
        max_episode_events=max_events,
        max_episode_span_seconds=max_span,
    )
    exporter = TrainingDatasetExporter(database, limits=limits)
    target = tmp_path / "exports" / "episodes.zip"

    exporter.export_to_path(subject_id, actor="p107-test", target=target)

    with zipfile.ZipFile(target) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        episodes = _dataset_rows(archive, manifest, "episodes.jsonl")
    assert [len(item["event_ids"]) for item in episodes] == expected_sizes
    assert all(len(item["event_ids"]) <= max_events for item in episodes)
    for episode in episodes:
        start = datetime.fromisoformat(episode["start_at"])
        end = datetime.fromisoformat(episode["end_at"])
        assert (end - start).total_seconds() <= max_span
    assert manifest["bounds"]["max_episode_events"] == max_events
    assert manifest["bounds"]["max_episode_span_seconds"] == max_span


def test_episode_assembly_is_split_before_serialized_record_limit(tmp_path: Path) -> None:
    subject_id = "Noyra-p107-episode-bytes"
    database = _database(tmp_path, subject_id)
    _append_events(database, subject_id, [0, 1, 2, 3, 4])
    limits = TrainingExportLimits(max_episode_bytes=280)
    exporter = TrainingDatasetExporter(database, limits=limits)
    target = tmp_path / "exports" / "episode-bytes.zip"

    exporter.export_to_path(subject_id, actor="p107-test", target=target)

    with zipfile.ZipFile(target) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        episodes = _dataset_rows(archive, manifest, "episodes.jsonl")
        for shard in manifest["datasets"]["episodes.jsonl"]:
            for line in archive.read(shard["file"]).splitlines():
                assert len(line) + 1 <= limits.max_episode_bytes
    assert [len(item["event_ids"]) for item in episodes] == [2, 2, 1]
    assert manifest["bounds"]["max_episode_bytes"] == limits.max_episode_bytes


def test_jsonl_files_are_sharded_with_verifiable_row_and_byte_manifests(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p107-shards"
    database = _database(tmp_path, subject_id)
    _append_events(database, subject_id, [0, 1, 2, 3, 4], payload_size=128)
    limits = TrainingExportLimits(
        shard_rows=2,
        shard_bytes=2_048,
        max_record_bytes=2_048,
    )
    exporter = TrainingDatasetExporter(database, limits=limits)
    target = tmp_path / "exports" / "sharded.zip"

    exporter.export_to_path(subject_id, actor="p107-test", target=target)

    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        rows = _dataset_rows(archive, manifest, "events.jsonl")
        shards = manifest["datasets"]["events.jsonl"]
        assert [item["file"] for item in shards] == [
            "events.jsonl",
            "events.part-00002.jsonl",
            "events.part-00003.jsonl",
        ]
        for dataset_shards in manifest["datasets"].values():
            for shard in dataset_shards:
                assert shard["file"] in names
                assert int(shard["rows"]) <= limits.shard_rows
                assert int(shard["bytes"]) <= limits.shard_bytes
                assert manifest["files"][shard["file"]]["bytes"] == shard["bytes"]
                assert manifest["files"][shard["file"]]["sha256"] == shard["sha256"]
    assert len(rows) == 5
    assert manifest["bounds"]["max_shard_rows"] == limits.shard_rows
    assert manifest["bounds"]["max_shard_bytes"] == limits.shard_bytes


def test_total_shard_manifest_is_bounded_and_partial_files_are_removed(tmp_path: Path) -> None:
    subject_id = "Noyra-p107-shard-cap"
    database = _database(tmp_path, subject_id)
    _append_events(database, subject_id, [0, 1])
    limits = TrainingExportLimits(shard_rows=1, max_dataset_shards=8)
    exporter = TrainingDatasetExporter(database, limits=limits)
    target = tmp_path / "exports" / "shard-cap.zip"

    with pytest.raises(TrainingExportLimitError, match="dataset shard bound exceeded"):
        exporter.export_to_path(subject_id, actor="p107-test", target=target)

    assert not target.exists()
    _assert_no_transient_files(exporter, target)


@pytest.mark.parametrize("include_model_io", [False, True], ids=["events", "model-io"])
def test_stream_queries_do_not_create_unbounded_sqlite_temp_sort(
    tmp_path: Path,
    include_model_io: bool,
) -> None:
    subject_id = f"Noyra-p107-query-plan-{int(include_model_io)}"
    database = _database(tmp_path, subject_id)
    _append_events(database, subject_id, [0])
    if include_model_io:
        TrainingStore(database).update_policy(subject_id, include_model_io=True)
        ModelLedger(database).prepare_call(
            subject_id,
            "p107-provider",
            "p107-model",
            "p107-purpose",
            "p107-request-hash",
            "p107-idempotency",
            request={"content": "bounded"},
            enforce_training_policy=True,
        )
    exporter = TrainingDatasetExporter(database)
    plans: list[list[str]] = []
    original_snapshot = database.read_snapshot

    class SnapshotProxy:
        def __init__(self, connection: Any):
            self.connection = connection

        def execute(self, sql: str, parameters: Any = ()) -> Any:
            if sql in {
                training_export_module._ELIGIBLE_ITEMS_SQL,
                training_export_module._MODEL_IO_SQL,
            }:
                plan = self.connection.execute(
                    "EXPLAIN QUERY PLAN " + sql,
                    parameters,
                ).fetchall()
                plans.append([str(row[-1]) for row in plan])
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

    @contextmanager
    def tracked_snapshot(*, checkpoint: Any = None) -> Any:
        with original_snapshot(checkpoint=checkpoint) as connection:
            yield SnapshotProxy(connection)

    database.read_snapshot = tracked_snapshot  # type: ignore[method-assign]
    target = tmp_path / "exports" / "query-plan.zip"

    exporter.export_to_path(subject_id, actor="p107-test", target=target)

    assert plans
    assert all("USE TEMP B-TREE" not in detail for plan in plans for detail in plan)


@pytest.mark.parametrize("budget", ["work", "archive", "quota", "free-space"])
def test_disk_budgets_fail_closed_and_remove_partial_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget: str,
) -> None:
    subject_id = f"Noyra-p107-budget-{budget}"
    database = _database(tmp_path, subject_id)
    _append_events(database, subject_id, [0], payload_size=256)
    expected = {
        "work": "work-byte budget exceeded",
        "archive": "archive-byte budget exceeded",
        "quota": "subject quota exceeded",
        "free-space": "minimum free-space reserve reached",
    }[budget]
    if budget == "work":
        limits = TrainingExportLimits(max_work_bytes=64)
    elif budget == "archive":
        limits = TrainingExportLimits(max_archive_bytes=65_536)
    elif budget == "quota":
        limits = TrainingExportLimits(subject_quota_bytes=1)
    else:
        no_space = shutil.disk_usage(tmp_path)._replace(free=0)
        monkeypatch.setattr(
            "noyra.core.training_export.shutil.disk_usage",
            lambda _path: no_space,
        )
        limits = TrainingExportLimits(minimum_free_bytes=1)
    exporter = TrainingDatasetExporter(database, limits=limits)
    target = tmp_path / "exports" / f"{budget}.zip"

    with pytest.raises(TrainingExportLimitError, match=expected):
        exporter.export_to_path(subject_id, actor="p107-test", target=target)

    assert not target.exists()
    _assert_no_transient_files(exporter, target)
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM training_exports WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()[0]
            == 0
        )


def test_oversize_stored_event_is_rejected_before_payload_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "Noyra-p107-event-record"
    database = _database(tmp_path, subject_id)
    _append_events(database, subject_id, [0], payload_size=4_096)
    limits = TrainingExportLimits(max_record_bytes=512, shard_bytes=512)
    exporter = TrainingDatasetExporter(database, limits=limits)
    target = tmp_path / "exports" / "event-record.zip"

    def fail_payload_read(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        pytest.fail("oversize stored event reached payload materialization")

    monkeypatch.setattr(exporter.events, "payload_from_row", fail_payload_read)

    with pytest.raises(TrainingExportLimitError, match="stored-byte bound"):
        exporter.export_to_path(subject_id, actor="p107-test", target=target)

    assert not target.exists()
    _assert_no_transient_files(exporter, target)


def test_compressed_model_payload_is_bounded_before_json_decode(tmp_path: Path) -> None:
    subject_id = "Noyra-p107-model-record"
    database = _database(tmp_path, subject_id)
    TrainingStore(database).update_policy(subject_id, include_model_io=True)
    ledger = ModelLedger(database)
    call, created = ledger.prepare_call(
        subject_id,
        "p107-provider",
        "p107-model",
        "p107-purpose",
        "p107-request-hash",
        "p107-idempotency",
        request={"content": "x" * 8_192},
        enforce_training_policy=True,
    )
    assert created
    with database.transaction() as connection:
        connection.execute(
            "UPDATE model_calls SET status = 'failed', created_at = ?, completed_at = ? "
            "WHERE call_id = ?",
            ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:01+00:00", call.call_id),
        )
    assert ledger.compress_cold_payloads(subject_id, older_than_days=1) == 1
    with database.connection() as connection:
        stored = connection.execute(
            "SELECT request_json FROM model_calls WHERE call_id = ?",
            (call.call_id,),
        ).fetchone()[0]
    assert str(stored).startswith("noyra-zlib-b64:")

    limits = TrainingExportLimits(max_record_bytes=512, shard_bytes=512)
    exporter = TrainingDatasetExporter(database, limits=limits)
    target = tmp_path / "exports" / "model-record.zip"

    with pytest.raises(TrainingExportLimitError, match="model payload exceeds the decompression"):
        exporter.export_to_path(subject_id, actor="p107-test", target=target)

    assert not target.exists()
    _assert_no_transient_files(exporter, target)


def test_cold_archive_segment_decompression_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "Noyra-p107-cold-segment"
    database = _database(tmp_path, subject_id)
    EventStore(database).append(
        subject_id,
        "public_observation",
        "p107-test",
        {"content": "x" * 8_192},
        privacy_level="public",
        occurred_at="2020-01-01T00:00:00+00:00",
    )
    key = base64.urlsafe_b64encode(b"p" * 32).decode("ascii")
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", key)
    archive = EventPayloadArchive(database, tmp_path / "subject" / "cold")
    assert archive.archive_cold(subject_id, older_than_days=1) == 1

    limits = TrainingExportLimits(
        max_archive_segment_bytes=512,
        max_record_bytes=2_048,
        shard_bytes=2_048,
    )
    exporter = TrainingDatasetExporter(database, limits=limits)
    target = tmp_path / "exports" / "cold-segment.zip"

    with pytest.raises(TrainingExportLimitError, match="archive segment exceeds"):
        exporter.export_to_path(subject_id, actor="p107-test", target=target)

    assert not target.exists()
    _assert_no_transient_files(exporter, target)


def test_cancellation_during_streaming_removes_work_and_pending_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "Noyra-p107-cancel"
    database = _database(tmp_path, subject_id)
    _append_events(database, subject_id, [0, 1])
    exporter = TrainingDatasetExporter(database)
    job_root = tmp_path / "exports" / "jobs"
    job_root.mkdir(parents=True)
    target = job_root / "cancelled.zip"
    control = ExportControl(
        database,
        "job-p107-cancel",
        subject_id,
        target,
        artifact_root=job_root,
        max_artifact_bytes=10_000_000,
    )
    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    original_write = training_export_module._JsonlWriter.write
    event_writes = 0

    def block_second_event(self: Any, value: dict[str, Any]) -> None:
        nonlocal event_writes
        if self.name == "events.jsonl":
            event_writes += 1
            if event_writes == 2:
                entered.set()
                assert release.wait(5), "test cancellation gate was not released"
        original_write(self, value)

    def run_export() -> None:
        try:
            exporter.export_to_path(
                subject_id,
                actor="p107-test",
                target=target,
                control=control,
            )
        except BaseException as error:  # pragma: no cover - asserted below
            failures.append(error)

    monkeypatch.setattr(training_export_module._JsonlWriter, "write", block_second_event)
    worker = threading.Thread(target=run_export, name="p107-cancel-stream")
    worker.start()
    assert entered.wait(10)
    assert list(exporter.work_root.glob(".training-export-*.work"))
    control.request_stop()
    release.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ExportCancelledError)
    assert "cancelled" in str(failures[0])

    assert not target.exists()
    _assert_no_transient_files(exporter, target)


def test_disk_full_during_partial_jsonl_write_removes_all_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "Noyra-p107-disk-full"
    database = _database(tmp_path, subject_id)
    _append_events(database, subject_id, [0, 1])
    exporter = TrainingDatasetExporter(database)
    target = tmp_path / "exports" / "disk-full.zip"
    original_write = training_export_module._JsonlWriter.write
    event_writes = 0

    def fail_second_event_write(self: Any, value: dict[str, Any]) -> None:
        nonlocal event_writes
        if self.name == "events.jsonl":
            event_writes += 1
            if event_writes == 2:
                raise OSError(errno.ENOSPC, "injected training export disk full")
        original_write(self, value)

    monkeypatch.setattr(training_export_module._JsonlWriter, "write", fail_second_event_write)

    with pytest.raises(OSError) as failure:
        exporter.export_to_path(subject_id, actor="p107-test", target=target)

    assert failure.value.errno == errno.ENOSPC
    assert event_writes == 2
    assert not target.exists()
    _assert_no_transient_files(exporter, target)


def test_disk_full_while_opening_writers_closes_already_opened_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "Noyra-p107-writer-open"
    database = _database(tmp_path, subject_id)
    _append_events(database, subject_id, [0])
    exporter = TrainingDatasetExporter(database)
    target = tmp_path / "exports" / "writer-open.zip"
    original_open_next = training_export_module._JsonlWriter._open_next
    open_attempts = 0

    def fail_third_open(self: Any) -> None:
        nonlocal open_attempts
        open_attempts += 1
        if open_attempts == 3:
            raise OSError(errno.ENOSPC, "injected writer initialization disk full")
        original_open_next(self)

    monkeypatch.setattr(training_export_module._JsonlWriter, "_open_next", fail_third_open)

    with pytest.raises(OSError) as failure:
        exporter.export_to_path(subject_id, actor="p107-test", target=target)

    assert failure.value.errno == errno.ENOSPC
    assert open_attempts == 3
    assert not target.exists()
    _assert_no_transient_files(exporter, target)


def test_restart_scavenges_only_owned_orphan_work_paths(tmp_path: Path) -> None:
    subject_id = "Noyra-p107-restart"
    database = _database(tmp_path, subject_id)
    exporter = TrainingDatasetExporter(database)
    directory_orphan = exporter.work_root / ".training-export-crashed.work"
    directory_orphan.mkdir()
    (directory_orphan / "private.jsonl").write_text("private", encoding="utf-8")
    file_orphan = exporter.work_root / ".training-bytes-crashed.work"
    file_orphan.write_bytes(b"private")
    unrelated = exporter.work_root / "operator-notes"
    unrelated.mkdir()
    pending_root = tmp_path / "exports" / "direct"
    pending_root.mkdir(parents=True)
    pending_orphan = pending_root / (".training.zip.pending_" + "a" * 32 + ".pending")
    pending_orphan.write_bytes(b"private staged archive")
    unrelated_pending = pending_root / ".operator-notes.pending"
    unrelated_pending.write_bytes(b"keep")

    assert exporter.start_after_ownership() == 3

    assert not directory_orphan.exists()
    assert not file_orphan.exists()
    assert not pending_orphan.exists()
    assert unrelated.is_dir()
    assert unrelated_pending.is_file()
    late_work = exporter.work_root / ".training-export-active.work"
    late_work.mkdir()
    assert exporter.start_after_ownership() == 0
    assert late_work.is_dir()

    with pytest.raises(ValueError, match="managed export root"):
        exporter.export_to_path(
            subject_id,
            actor="p107-test",
            target=tmp_path / "unmanaged" / "training.zip",
        )
    with pytest.raises(ValueError, match="managed export root"):
        TrainingDatasetExporter(
            database,
            work_root=tmp_path / "unmanaged-work",
        )


def test_compatibility_byte_api_rejects_oversize_before_reading_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "Noyra-p107-compat-bytes"
    database = _database(tmp_path, subject_id)
    _append_events(database, subject_id, [0])
    exporter = TrainingDatasetExporter(
        database,
        limits=TrainingExportLimits(max_compatibility_bytes=1),
    )
    original_open = Path.open

    def guarded_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path.name == "training.zip" and mode == "rb":
            pytest.fail("oversize compatibility archive was read into memory")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    with pytest.raises(TrainingExportLimitError, match="too large for the compatibility"):
        exporter.export(subject_id, actor="p107-test")

    assert not list(exporter.work_root.iterdir())
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM training_exports WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_records WHERE subject_id = ? "
                "AND action = 'training_exported'",
                (subject_id,),
            ).fetchone()[0]
            == 0
        )


def test_legacy_builder_and_collector_reject_unbounded_rows(tmp_path: Path) -> None:
    subject_id = "Noyra-p107-legacy"
    database = _database(tmp_path, subject_id)
    event_ids = _append_events(database, subject_id, [0, 1])
    rows = [
        {
            "event_id": event_id,
            "event_type": "public_observation",
            "occurred_at": f"2026-08-15T00:00:0{index}+00:00",
            "payload": {"index": index},
            "source": "p107-test",
        }
        for index, event_id in enumerate(event_ids)
    ]
    limits = TrainingExportLimits(max_legacy_rows=1)

    with pytest.raises(TrainingExportLimitError, match="legacy training builder row bound"):
        TrainingDatasetBuilder(limits).build(rows)

    with pytest.raises(TrainingExportLimitError, match="legacy training builder byte bound"):
        TrainingDatasetBuilder(TrainingExportLimits(max_compatibility_bytes=64)).build(rows)

    exporter = TrainingDatasetExporter(database, limits=limits)
    with pytest.raises(RuntimeError, match="bounded row limit"):
        exporter._collect_data(subject_id)
    assert not list(tmp_path.glob(".noyra.sqlite3.snapshot_*.sqlite*"))
