from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from noyra.core import (
    Database,
    ExportControl,
    ExportJobManager,
    IdentityStore,
    SubjectKernel,
    TrainingStore,
)
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.export_jobs import ExportKind
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.training_export import TrainingDatasetExporter
from noyra.core.types import content_hash, utc_now
from noyra.service import NoyraService, ServiceSettings


def _database(root: Path, *subjects: str) -> Database:
    database = Database(root / "noyra.sqlite3")
    identities = IdentityStore(database)
    training = TrainingStore(database)
    for subject_id in subjects:
        identities.ensure(subject_id, content_hash({"subject_id": subject_id}))
        training.ensure_policy(subject_id)
    return database


def _worker_for_export(
    database: Database, kind: ExportKind
) -> Callable[[str, ExportKind, Path, ExportControl], tuple[str, str]]:
    runtime = RuntimeLogExporter(database)
    training = TrainingDatasetExporter(database)

    def worker(
        subject_id: str,
        export_kind: str,
        target: Path,
        control: ExportControl,
    ) -> tuple[str, str]:
        assert export_kind == kind
        if kind == "runtime":
            artifact = runtime.export_to_path(
                subject_id,
                actor="p106-test",
                target=target,
                control=control,
            )
            return artifact.filename, artifact.sha256
        training_artifact = training.export_to_path(
            subject_id,
            actor="p106-test",
            target=target,
            control=control,
        )
        return training_artifact.filename, training_artifact.sha256

    return worker


def _success_counts(database: Database, subject_id: str, kind: ExportKind) -> tuple[int, int, int]:
    with database.connection() as connection:
        job_success = int(
            connection.execute(
                "SELECT COUNT(*) FROM export_jobs WHERE subject_id = ? AND status = 'completed'",
                (subject_id,),
            ).fetchone()[0]
        )
        runtime_audits = int(
            connection.execute(
                "SELECT COUNT(*) FROM audit_records "
                "WHERE subject_id = ? AND action = 'runtime_exported'",
                (subject_id,),
            ).fetchone()[0]
        )
        training_audits = int(
            connection.execute(
                "SELECT COUNT(*) FROM audit_records "
                "WHERE subject_id = ? AND action = 'training_exported'",
                (subject_id,),
            ).fetchone()[0]
        )
        training_exports = int(
            connection.execute(
                "SELECT COUNT(*) FROM training_exports WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()[0]
        )
    if kind == "runtime":
        return job_success, runtime_audits, training_exports
    return job_success, training_audits, training_exports


@pytest.mark.parametrize("kind", ["runtime", "training"])
def test_cancel_before_publication_has_no_domain_success_or_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: ExportKind
) -> None:
    """Cancellation linearizes before publication and suppresses every success record."""

    subject_id = "Noyra-p106-cancel"
    database = _database(tmp_path, subject_id)
    export_root = tmp_path / "exports" / "jobs"
    publication_entered = threading.Event()
    release_publication = threading.Event()
    original_publication = ExportControl.publication

    @contextlib.contextmanager
    def gated_publication(control: ExportControl) -> Iterator[Any]:
        publication_entered.set()
        assert release_publication.wait(5), "test publication gate was not released"
        with original_publication(control) as connection:
            yield connection

    monkeypatch.setattr(ExportControl, "publication", gated_publication)
    manager = ExportJobManager(database, export_root, _worker_for_export(database, kind))
    manager.start_after_ownership(subject_id)
    try:
        job = manager.create(subject_id, kind)
        assert publication_entered.wait(10)

        cancelled = manager.cancel(job.job_id, subject_id)
        assert cancelled.status == "cancelled"
        release_publication.set()
        manager.futures[job.job_id].result(timeout=10)

        final = manager.get(job.job_id, subject_id=subject_id)
        assert final.status == "cancelled"
        assert final.artifact_path is None
        assert not list(export_root.glob("*.zip"))
        assert not list(export_root.glob(".*.tmp"))
        assert not list(export_root.glob(".*.pending"))

        completed_jobs, success_audits, training_exports = _success_counts(
            database, subject_id, kind
        )
        assert completed_jobs == 0
        assert success_audits == 0
        assert training_exports == 0
    finally:
        release_publication.set()
        manager.close()


@pytest.mark.parametrize("kind", ["runtime", "training"])
def test_publication_wins_and_cancel_after_commit_cannot_revoke_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: ExportKind
) -> None:
    """A cancellation racing a publication transaction observes the completed winner."""

    subject_id = "Noyra-p106-publish"
    database = _database(tmp_path, subject_id)
    export_root = tmp_path / "exports" / "jobs"
    publication_entered = threading.Event()
    release_publication = threading.Event()
    cancel_started = threading.Event()
    cancel_finished = threading.Event()
    cancel_result: list[BaseException | Any] = []
    original_publication = ExportControl.publication

    @contextlib.contextmanager
    def gated_publication(control: ExportControl) -> Iterator[Any]:
        with original_publication(control) as connection:
            publication_entered.set()
            assert release_publication.wait(5), "test publication gate was not released"
            yield connection

    monkeypatch.setattr(ExportControl, "publication", gated_publication)
    manager = ExportJobManager(database, export_root, _worker_for_export(database, kind))
    manager.start_after_ownership(subject_id)
    try:
        job = manager.create(subject_id, kind)
        assert publication_entered.wait(10)

        def cancel() -> None:
            cancel_started.set()
            try:
                cancel_result.append(manager.cancel(job.job_id, subject_id))
            except BaseException as error:  # pragma: no cover - asserted below
                cancel_result.append(error)
            finally:
                cancel_finished.set()

        canceller = threading.Thread(target=cancel, name="p106-canceller")
        canceller.start()
        assert cancel_started.wait(2)
        assert not cancel_finished.wait(0.1)
        release_publication.set()

        manager.futures[job.job_id].result(timeout=15)
        assert cancel_finished.wait(5)
        canceller.join(timeout=5)
        assert len(cancel_result) == 1
        assert isinstance(cancel_result[0], RuntimeError)
        assert "not cancellable" in str(cancel_result[0])

        final = manager.get(job.job_id, subject_id=subject_id)
        assert final.status == "completed"
        assert final.artifact_path is not None
        assert Path(final.artifact_path).is_file()
        completed_jobs, success_audits, training_exports = _success_counts(
            database, subject_id, kind
        )
        assert completed_jobs == 1
        assert success_audits == 1
        assert training_exports == (1 if kind == "training" else 0)
    finally:
        release_publication.set()
        manager.close()


@pytest.mark.parametrize("kind", ["runtime", "training"])
def test_job_target_symlink_cannot_escape_export_root(tmp_path: Path, kind: ExportKind) -> None:
    """A target swap cannot redirect staging, publication, or cleanup outside the job root."""

    subject_id = "Noyra-p106-target-link"
    database = _database(tmp_path, subject_id)
    export_root = tmp_path / "exports" / "jobs"
    outside = tmp_path / "outside-preserved.zip"
    outside.write_bytes(b"outside content must survive")
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks are unavailable on this host: {error}")
    finally:
        probe.unlink(missing_ok=True)

    exporter_worker = _worker_for_export(database, kind)

    def linked_worker(
        worker_subject: str,
        export_kind: ExportKind,
        target: Path,
        control: ExportControl,
    ) -> tuple[str, str]:
        target.symlink_to(outside)
        return exporter_worker(worker_subject, export_kind, target, control)

    manager = ExportJobManager(database, export_root, linked_worker)
    manager.start_after_ownership(subject_id)
    try:
        job = manager.create(subject_id, kind)
        manager.futures[job.job_id].result(timeout=10)

        final = manager.get(job.job_id, subject_id=subject_id)
        assert final.status == "failed"
        assert final.artifact_path is None
        assert outside.read_bytes() == b"outside content must survive"
        assert not (export_root / f"{job.job_id}.zip").exists()
        completed_jobs, success_audits, training_exports = _success_counts(
            database, subject_id, kind
        )
        assert completed_jobs == 0
        assert success_audits == 0
        assert training_exports == 0
    finally:
        manager.close()


def test_manager_shutdown_waits_for_writer_before_subject_lock_is_released(tmp_path: Path) -> None:
    """A second kernel cannot acquire ownership while a cancelled writer is still running."""

    subject_id = "Noyra-p106-shutdown"
    database_path = tmp_path / "noyra.sqlite3"
    kernel = SubjectKernel(database_path, subject_id, content_hash({"subject_id": subject_id}))
    kernel.boot()
    started = threading.Event()
    release_worker = threading.Event()

    def worker(
        _subject_id: str,
        _kind: str,
        target: Path,
        _control: ExportControl,
    ) -> tuple[str, str]:
        started.set()
        assert release_worker.wait(10), "test worker was not released"
        target.write_bytes(b"cancelled worker")
        return "cancelled.zip", "digest"

    manager = ExportJobManager(kernel.database, tmp_path / "exports", worker)
    manager.start_after_ownership(subject_id)
    kernel2: SubjectKernel | None = None
    close_finished = threading.Event()
    close_failures: list[BaseException] = []
    try:
        job = manager.create(subject_id, "runtime")
        assert started.wait(5)
        close_thread = threading.Thread(
            target=lambda: _close_manager_then_kernel(
                manager, kernel, close_finished, close_failures
            ),
            name="p106-manager-close",
        )
        close_thread.start()
        assert not close_finished.wait(0.2)

        kernel2 = SubjectKernel(database_path, subject_id, content_hash({"subject_id": subject_id}))
        with pytest.raises(RuntimeOwnershipError):
            kernel2.boot()

        release_worker.set()
        assert close_finished.wait(10)
        close_thread.join(timeout=2)
        assert not close_failures
        assert manager.get(job.job_id, subject_id=subject_id).status == "cancelled"

        assert not kernel.process_lock.held
        kernel2.boot()
    finally:
        release_worker.set()
        if not close_finished.is_set():
            manager.close()
        if kernel.process_lock.held:
            kernel.close()
        if kernel2 is not None and kernel2.process_lock.held:
            kernel2.close()


def _close_manager_then_kernel(
    manager: ExportJobManager,
    kernel: SubjectKernel,
    finished: threading.Event,
    failures: list[BaseException],
) -> None:
    try:
        manager.close()
        kernel.close()
    except BaseException as error:  # pragma: no cover - surfaced below
        failures.append(error)
    finally:
        finished.set()


def _insert_job(
    database: Database,
    job_id: str,
    subject_id: str,
    status: str,
    *,
    artifact_path: str | None = None,
) -> None:
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO export_jobs(job_id, subject_id, export_kind, status, artifact_path, "
            "created_at) VALUES (?, ?, 'runtime', ?, ?, ?)",
            (job_id, subject_id, status, artifact_path, utc_now()),
        )


def test_recovery_is_explicit_subject_owned_and_scoped_to_interrupted_files(
    tmp_path: Path,
) -> None:
    """Recovery does not run at manager construction and never removes other subjects' files."""

    subject_a = "Noyra-p106-recovery-a"
    subject_b = "Noyra-p106-recovery-b"
    database = _database(tmp_path, subject_a, subject_b)
    export_root = tmp_path / "exports" / "jobs"
    export_root.mkdir(parents=True)

    running_a = "export-job-recovery-a-running"
    queued_a = "export-job-recovery-a-queued"
    running_b = "export-job-recovery-b-running"
    completed_a = "export-job-recovery-a-completed"
    outside = tmp_path / "outside-preserve.zip"
    outside.write_bytes(b"outside")
    _insert_job(database, running_a, subject_a, "running", artifact_path=str(outside))
    _insert_job(database, queued_a, subject_a, "queued")
    _insert_job(database, running_b, subject_b, "running")
    _insert_job(
        database,
        completed_a,
        subject_a,
        "completed",
        artifact_path=str(export_root / f"{completed_a}.zip"),
    )

    interrupted_a_target = export_root / f"{running_a}.zip"
    interrupted_a_target.write_bytes(b"interrupted-a")
    interrupted_a_pending = export_root / f".{running_a}.zip.pending_fixture.pending"
    interrupted_a_pending.write_bytes(b"pending-a")
    queued_a_tmp = export_root / f".{queued_a}.zip.export_fixture.tmp"
    queued_a_tmp.write_bytes(b"tmp-a")
    interrupted_b_target = export_root / f"{running_b}.zip"
    interrupted_b_target.write_bytes(b"interrupted-b")
    completed_target = export_root / f"{completed_a}.zip"
    completed_target.write_bytes(b"completed")
    unrelated = export_root / "unrelated.zip"
    unrelated.write_bytes(b"unrelated")

    manager = ExportJobManager(database, export_root, lambda *_args: ("unused.zip", "unused"))
    try:
        # Construction alone is not an ownership event and must not mutate state/files.
        assert interrupted_a_target.exists()
        assert interrupted_a_pending.exists()
        assert interrupted_b_target.exists()
        with database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT status FROM export_jobs WHERE job_id = ?", (running_a,)
                ).fetchone()[0]
                == "running"
            )

        manager.start_after_ownership(subject_a)
        assert not interrupted_a_target.exists()
        assert not interrupted_a_pending.exists()
        assert not queued_a_tmp.exists()
        assert interrupted_b_target.exists()
        assert completed_target.exists()
        assert outside.exists()
        assert unrelated.exists()
        with database.connection() as connection:
            statuses = {
                row["job_id"]: row["status"]
                for row in connection.execute(
                    "SELECT job_id, status FROM export_jobs WHERE subject_id = ?", (subject_a,)
                ).fetchall()
            }
        assert statuses[running_a] == "failed"
        assert statuses[queued_a] == "failed"
        assert statuses[completed_a] == "completed"

        manager.start_after_ownership(subject_b)
        assert not interrupted_b_target.exists()
        with database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT status FROM export_jobs WHERE job_id = ?", (running_b,)
                ).fetchone()[0]
                == "failed"
            )
    finally:
        manager.close()


def test_unowned_second_service_does_not_recover_before_lock_failure(
    tmp_path: Path,
) -> None:
    """A service that loses the subject lock cannot change jobs or cleanup files."""

    subject_id = "Noyra-p106-second-service"
    settings = ServiceSettings(
        data_dir=tmp_path / "data",
        subject_id=subject_id,
        genesis_hash=content_hash({"subject_id": subject_id}),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr("p106-test-admin-token-with-sufficient-entropy"),
        minimum_free_storage_bytes=10_000_000,
    )
    key_dir = settings.data_dir / "secrets" / "common-knowledge"
    key_dir.mkdir(parents=True)
    (key_dir / "common-knowledge-ed25519.key").write_bytes(b"K" * 32)
    first = NoyraService(settings)
    second: NoyraService | None = None
    job_id = "export-job-second-service-running"
    try:
        first.boot()
        export_root = settings.data_dir / "exports" / "jobs"
        target = export_root / f"{job_id}.zip"
        pending = export_root / f".{job_id}.zip.pending_fixture.pending"
        target.write_bytes(b"first service owns this target")
        pending.write_bytes(b"first service owns this pending file")
        _insert_job(first.kernel.database, job_id, subject_id, "running")

        second = NoyraService(settings)
        with first.kernel.database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT status FROM export_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()[0]
                == "running"
            )
        assert target.exists()
        assert pending.exists()

        with pytest.raises(RuntimeOwnershipError):
            second.boot()

        # The failed boot must not have reached recovery, so the first owner's
        # durable status and files are unchanged.
        with first.kernel.database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT status FROM export_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()[0]
                == "running"
            )
        assert target.exists()
        assert pending.exists()
    finally:
        if second is not None:
            second.http.close()
            second.kernel.close()
        first.http.close()
        first.kernel.close()


def test_snapshot_backup_checkpoint_cleans_partial_image(tmp_path: Path) -> None:
    """A cancellation raised by SQLite backup progress leaves no derived snapshot files."""

    database = _database(tmp_path, "Noyra-p106-snapshot")
    calls = 0

    def checkpoint() -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise RuntimeError("snapshot export cancelled")

    with (
        pytest.raises(RuntimeError, match="snapshot export cancelled"),
        database.read_snapshot(checkpoint=checkpoint),
    ):
        pass

    assert calls >= 2
    assert not list(tmp_path.glob(f".{database.path.name}.snapshot_*.sqlite"))
    assert not list(tmp_path.glob(f".{database.path.name}.snapshot_*.sqlite-*"))
    assert not list(tmp_path.glob(f".{database.path.name}.snapshot_*.sqlite.lock"))
