from __future__ import annotations

import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .database import Database
from .errors import NotFoundError
from .types import new_id, utc_now

ExportKind = Literal["runtime", "training"]
ARTIFACT_PRUNING_ERROR = "artifact_pruning"
ARTIFACT_PRUNED_ERROR = "artifact_pruned"


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(metadata.st_mode):
        return True
    return bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


class ExportCancelledError(RuntimeError):
    """Cooperative stop requested before an export was published."""


def retire_completed_export_artifact(
    database: Database,
    *,
    job_id: str,
    subject_id: str,
    artifact_path: str,
    path: Path,
) -> bool:
    """Retire one artifact without leaving an unrecoverable DB/filesystem split.

    SQLite cannot atomically commit a filesystem deletion.  Persisting a
    pruning intent first makes every interruption recoverable: a failed delete
    can be retried, while a crash after deletion leaves an explicit tombstone
    that the next maintenance pass can finalize.
    """
    with database.transaction() as connection:
        claimed = connection.execute(
            "UPDATE export_jobs SET error_code = ? "
            "WHERE job_id = ? AND subject_id = ? AND status = 'completed' "
            "AND artifact_path = ? AND (error_code IS NULL OR error_code = ?)",
            (
                ARTIFACT_PRUNING_ERROR,
                job_id,
                subject_id,
                artifact_path,
                ARTIFACT_PRUNING_ERROR,
            ),
        )
    if claimed.rowcount != 1:
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Keep artifact_path plus the durable pruning intent so a later pass
        # can retry.  Download readers reject this transitional state.
        return False
    with database.transaction() as connection:
        finalized = connection.execute(
            "UPDATE export_jobs SET artifact_path = NULL, byte_size = NULL, "
            "error_code = ? WHERE job_id = ? AND subject_id = ? "
            "AND status = 'completed' AND artifact_path = ? AND error_code = ?",
            (
                ARTIFACT_PRUNED_ERROR,
                job_id,
                subject_id,
                artifact_path,
                ARTIFACT_PRUNING_ERROR,
            ),
        )
    return finalized.rowcount == 1


@dataclass(frozen=True)
class ExportJob:
    job_id: str
    subject_id: str
    export_kind: str
    status: str
    artifact_path: str | None
    filename: str | None
    sha256: str | None
    byte_size: int | None
    error_code: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


class ExportControl:
    """Linearize cancellation and publication for one background export."""

    def __init__(
        self,
        database: Database,
        job_id: str,
        subject_id: str,
        target: Path,
        *,
        artifact_root: Path | None = None,
        max_artifact_bytes: int,
    ):
        self.database = database
        self.job_id = job_id
        self.subject_id = subject_id
        # Keep the job path lexical.  Resolving it here would follow a
        # pre-existing symlink and could move a worker's output outside the
        # export root before the publication checks run.
        self.root = Path(os.path.abspath(artifact_root or target.parent))
        self.target = Path(os.path.abspath(target))
        if self.target.parent != self.root:
            raise ValueError("export artifact path is outside its job root")
        try:
            root_metadata = self.root.lstat()
        except OSError as error:
            raise ValueError("export job root cannot be inspected") from error
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or self._is_link_or_reparse(self.root)
            or self.root.resolve() != self.root
        ):
            raise ValueError("export job root cannot be a symlink or reparse point")
        self._root_identity = (int(root_metadata.st_dev), int(root_metadata.st_ino))
        self.max_artifact_bytes = max_artifact_bytes
        self._cancelled = threading.Event()
        self._decision_lock = threading.Lock()

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        return _path_is_link_or_reparse(path)

    def validate_target(self, artifact_path: Path | str | None = None) -> Path:
        """Validate the lexical job target without following attacker-controlled links."""
        path = self.target if artifact_path is None else Path(os.path.abspath(artifact_path))
        if path != self.target or path.parent != self.root:
            raise RuntimeError("export artifact path is outside its job root")
        try:
            root_metadata = self.root.lstat()
        except OSError as error:
            raise RuntimeError("export job root cannot be inspected") from error
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or self._is_link_or_reparse(self.root)
            or self.root.resolve() != self.root
            or (int(root_metadata.st_dev), int(root_metadata.st_ino)) != self._root_identity
        ):
            raise RuntimeError("export job root changed or is a symlink")
        if self._is_link_or_reparse(path):
            raise RuntimeError("export target cannot be a symlink or reparse point")
        return path

    def checkpoint(self) -> None:
        if self._cancelled.is_set():
            raise ExportCancelledError("export was cancelled before publication")

    def request_stop(self) -> None:
        """Wake cooperative checkpoints even if durable cancellation cannot be recorded."""
        self._cancelled.set()

    def cancel(self, *, error_code: str) -> bool:
        """Cancel before publication, or report that publication already won."""
        with self._decision_lock:
            with self.database.transaction() as connection:
                updated = connection.execute(
                    "UPDATE export_jobs SET status = 'cancelled', completed_at = ?, "
                    "error_code = ? WHERE job_id = ? AND subject_id = ? "
                    "AND status IN ('queued', 'running')",
                    (utc_now(), error_code, self.job_id, self.subject_id),
                )
            if updated.rowcount == 1:
                self._cancelled.set()
                return True
            return False

    @contextmanager
    def publication(self) -> Iterator[sqlite3.Connection]:
        """Hold the cancellation decision lock through the publication transaction."""
        with self._decision_lock:
            self.checkpoint()
            self.validate_target()
            with self.database.transaction() as connection:
                row = connection.execute(
                    "SELECT status FROM export_jobs WHERE job_id = ? AND subject_id = ?",
                    (self.job_id, self.subject_id),
                ).fetchone()
                if row is None:
                    raise RuntimeError("export job disappeared before publication")
                status = str(row["status"])
                if status != "running":
                    if status == "cancelled":
                        self._cancelled.set()
                        raise ExportCancelledError("export was cancelled before publication")
                    raise RuntimeError(f"export job cannot publish from status: {status}")
                yield connection

    def complete(
        self,
        connection: sqlite3.Connection,
        *,
        artifact_path: Path,
        filename: str,
        sha256: str,
        byte_size: int,
    ) -> None:
        """Complete the job inside an active ``publication`` transaction."""
        resolved = self.validate_target(artifact_path)
        if resolved != self.target:
            raise RuntimeError("export artifact path does not match its job target")
        if byte_size < 0 or byte_size > self.max_artifact_bytes:
            raise RuntimeError("export_artifact_too_large")
        if not resolved.is_file() or resolved.stat().st_size != byte_size:
            raise RuntimeError("export artifact metadata does not match the published file")
        updated = connection.execute(
            "UPDATE export_jobs SET status = 'completed', artifact_path = ?, filename = ?, "
            "sha256 = ?, byte_size = ?, completed_at = ?, error_code = NULL "
            "WHERE job_id = ? AND subject_id = ? AND status = 'running'",
            (
                str(resolved),
                filename,
                sha256,
                byte_size,
                utc_now(),
                self.job_id,
                self.subject_id,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("export job lost publication ownership")

    def fail(self, error_code: str) -> None:
        """Record a worker failure only if cancellation/publication has not won."""
        with self._decision_lock, self.database.transaction() as connection:
            connection.execute(
                "UPDATE export_jobs SET status = 'failed', error_code = ?, completed_at = ? "
                "WHERE job_id = ? AND subject_id = ? AND status = 'running'",
                (error_code, utc_now(), self.job_id, self.subject_id),
            )


ExportWorker = Callable[[str, ExportKind, Path, ExportControl], tuple[str, str]]


class ExportJobManager:
    """Bounded background export jobs with owned recovery and cooperative cancellation."""

    def __init__(
        self,
        database: Database,
        root: Path | str,
        worker: ExportWorker,
        *,
        max_workers: int = 2,
        max_pending: int = 8,
    ):
        self.database = database
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.worker = worker
        self.max_pending = max_pending
        self.max_completed_artifacts = 32
        self.max_artifact_bytes = 1_000_000_000
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="noyra-export",
        )
        self._lock = threading.Lock()
        self._closed = False
        self._owned_subjects: set[str] = set()
        self.futures: dict[str, Future[None]] = {}
        self.controls: dict[str, ExportControl] = {}

    def start_after_ownership(self, subject_id: str) -> None:
        """Recover interrupted work only after the caller owns the subject lock."""
        with self._lock:
            if self._closed:
                raise RuntimeError("export job manager is closed")
            if subject_id in self._owned_subjects:
                return
            with self.database.connection() as connection:
                rows = connection.execute(
                    "SELECT job_id FROM export_jobs WHERE subject_id = ? "
                    "AND status IN ('queued', 'running') ORDER BY created_at, job_id",
                    (subject_id,),
                ).fetchall()
            job_ids = tuple(str(row["job_id"]) for row in rows)
            for job_id in job_ids:
                self._cleanup_job_files(job_id, include_target=True)
            if job_ids:
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE export_jobs SET status = 'failed', "
                        "error_code = 'service_restarted', "
                        "artifact_path = NULL, filename = NULL, sha256 = NULL, byte_size = NULL, "
                        "completed_at = ? WHERE subject_id = ? "
                        "AND status IN ('queued', 'running')",
                        (utc_now(), subject_id),
                    )
            self._owned_subjects.add(subject_id)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            controls = tuple(self.controls.values())
            futures = tuple(self.futures.values())
        cancellation_errors: list[Exception] = []
        try:
            for control in controls:
                try:
                    control.cancel(error_code="service_shutdown")
                except Exception as error:
                    control.request_stop()
                    cancellation_errors.append(error)
            for future in futures:
                future.cancel()
        finally:
            # The subject process lock may only be released after every writer exits.
            self.executor.shutdown(wait=True, cancel_futures=True)
        if cancellation_errors:
            raise RuntimeError("one or more export jobs could not record shutdown") from (
                cancellation_errors[0]
            )

    def create(self, subject_id: str, export_kind: ExportKind) -> ExportJob:
        if export_kind not in {"runtime", "training"}:
            raise ValueError("export kind is invalid")
        with self._lock:
            if self._closed:
                raise RuntimeError("export job manager is closed")
            if subject_id not in self._owned_subjects:
                raise RuntimeError("export recovery has not started under subject ownership")
            self._prune_completed_artifacts(subject_id)
            completed_ids = {job_id for job_id, future in self.futures.items() if future.done()}
            self.futures = {
                job_id: future for job_id, future in self.futures.items() if not future.done()
            }
            for job_id in completed_ids:
                self.controls.pop(job_id, None)
            active = sum(not future.done() for future in self.futures.values())
            if active >= self.max_pending:
                raise RuntimeError("export queue is full")
            job_id = new_id("export-job")
            now = utc_now()
            target = self.root / f"{job_id}.zip"
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO export_jobs(job_id, subject_id, export_kind, status, created_at) "
                    "VALUES (?, ?, ?, 'queued', ?)",
                    (job_id, subject_id, export_kind, now),
                )
            control = ExportControl(
                self.database,
                job_id,
                subject_id,
                target,
                artifact_root=self.root,
                max_artifact_bytes=self.max_artifact_bytes,
            )
            self.controls[job_id] = control
            try:
                future = self.executor.submit(
                    self._run,
                    job_id,
                    subject_id,
                    export_kind,
                    control,
                )
            except Exception:
                self.controls.pop(job_id, None)
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE export_jobs SET status = 'failed', error_code = 'submit_failed', "
                        "completed_at = ? WHERE job_id = ? AND status = 'queued'",
                        (utc_now(), job_id),
                    )
                raise
            self.futures[job_id] = future
        return self.get(job_id, subject_id=subject_id)

    def _prune_completed_artifacts(self, subject_id: str) -> None:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT job_id, artifact_path FROM export_jobs WHERE subject_id = ? "
                "AND status = 'completed' AND artifact_path IS NOT NULL "
                "ORDER BY completed_at DESC, job_id DESC",
                (subject_id,),
            ).fetchall()
            pending_rows = connection.execute(
                "SELECT job_id, artifact_path FROM export_jobs WHERE subject_id = ? "
                "AND status = 'completed' AND artifact_path IS NOT NULL "
                "AND error_code = ? ORDER BY completed_at DESC, job_id DESC",
                (subject_id, ARTIFACT_PRUNING_ERROR),
            ).fetchall()
        pending_ids = {str(row["job_id"]) for row in pending_rows}
        rows_to_prune = tuple(pending_rows) + tuple(
            row
            for row in rows[self.max_completed_artifacts :]
            if str(row["job_id"]) not in pending_ids
        )
        for row in rows_to_prune:
            job_id = str(row["job_id"])
            artifact_path = str(row["artifact_path"])
            path = Path(os.path.abspath(artifact_path))
            if (
                path.parent == self.root
                and path.name == f"{job_id}.zip"
                and not _path_is_link_or_reparse(path)
            ):
                retire_completed_export_artifact(
                    self.database,
                    job_id=job_id,
                    subject_id=subject_id,
                    artifact_path=artifact_path,
                    path=path,
                )

    def cancel(self, job_id: str, subject_id: str) -> ExportJob:
        with self._lock:
            if subject_id not in self._owned_subjects:
                raise RuntimeError("export cancellation requires subject ownership")
            control = self.controls.get(job_id)
            future = self.futures.get(job_id)
        if control is None or control.subject_id != subject_id:
            job = self.get(job_id, subject_id=subject_id)
            raise RuntimeError(f"export job is not cancellable: {job.status}")
        if not control.cancel(error_code="cancel_requested"):
            job = self.get(job_id, subject_id=subject_id)
            raise RuntimeError(f"export job is not cancellable: {job.status}")
        if future is not None:
            future.cancel()
        return self.get(job_id, subject_id=subject_id)

    def list(self, subject_id: str, *, limit: int = 100) -> list[ExportJob]:
        bounded = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT job_id FROM export_jobs WHERE subject_id = ? "
                "ORDER BY created_at DESC, job_id DESC LIMIT ?",
                (subject_id, bounded),
            ).fetchall()
        return [self.get(str(row["job_id"]), subject_id=subject_id) for row in rows]

    def download_path(self, job_id: str, *, subject_id: str) -> tuple[ExportJob, Path]:
        job = self.get(job_id, subject_id=subject_id)
        if (
            job.status != "completed"
            or not job.artifact_path
            or job.error_code == ARTIFACT_PRUNING_ERROR
        ):
            raise RuntimeError("export artifact is not ready")
        path = Path(job.artifact_path).resolve()
        if self.root != path.parent or path.name != f"{job_id}.zip":
            raise RuntimeError("export artifact path is invalid")
        if not path.is_file():
            raise FileNotFoundError("export artifact is missing")
        return job, path

    def get(self, job_id: str, *, subject_id: str) -> ExportJob:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM export_jobs WHERE job_id = ? AND subject_id = ?",
                (job_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"export job not found: {job_id}")
        return ExportJob(
            str(row["job_id"]),
            str(row["subject_id"]),
            str(row["export_kind"]),
            str(row["status"]),
            row["artifact_path"],
            row["filename"],
            row["sha256"],
            None if row["byte_size"] is None else int(row["byte_size"]),
            row["error_code"],
            str(row["created_at"]),
            row["started_at"],
            row["completed_at"],
        )

    def _run(
        self,
        job_id: str,
        subject_id: str,
        export_kind: ExportKind,
        control: ExportControl,
    ) -> None:
        started = utc_now()
        with self.database.transaction() as connection:
            updated = connection.execute(
                "UPDATE export_jobs SET status = 'running', started_at = ? "
                "WHERE job_id = ? AND subject_id = ? AND status = 'queued'",
                (started, job_id, subject_id),
            )
            if updated.rowcount != 1:
                return
        target = control.target
        try:
            control.validate_target()
            control.checkpoint()
            filename, digest = self.worker(subject_id, export_kind, target, control)
            job = self.get(job_id, subject_id=subject_id)
            if job.status == "completed":
                return
            if job.status != "running":
                target.unlink(missing_ok=True)
                self._cleanup_job_files(job_id, include_target=False)
                return
            control.checkpoint()
            size = target.stat().st_size
            with control.publication() as connection:
                control.complete(
                    connection,
                    artifact_path=target,
                    filename=filename,
                    sha256=digest,
                    byte_size=size,
                )
        except Exception as error:
            current = self.get(job_id, subject_id=subject_id)
            if current.status == "completed":
                return
            try:
                target.unlink(missing_ok=True)
                self._cleanup_job_files(job_id, include_target=False)
            finally:
                control.fail(type(error).__name__)

    def _cleanup_job_files(self, job_id: str, *, include_target: bool) -> None:
        expected = f"{job_id}.zip"
        if Path(expected).name != expected:
            raise RuntimeError("export job id cannot be mapped to an artifact path")
        hidden_prefix = f".{expected}."
        for candidate in self.root.iterdir():
            name = candidate.name
            is_target = include_target and name == expected
            is_partial = name.startswith(hidden_prefix) and (
                name.endswith(".tmp") or name.endswith(".pending")
            )
            if is_target or is_partial:
                candidate.unlink(missing_ok=True)
