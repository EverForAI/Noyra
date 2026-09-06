"""Storage boundaries for subject state, training provenance, and archives.

The subject database remains the source of truth.  This module only manages
metadata and policy; derived exports and cloud objects are deliberately kept
outside the identity ledger.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .database import Database
from .errors import IntegrityError, NotFoundError, TrainingPolicyConflictError
from .types import canonical_json, new_id, utc_now


@contextmanager
def _checkpointed_rows(
    connection: Any,
    sql: str,
    parameters: tuple[Any, ...],
    checkpoint: Callable[[], None] | None,
) -> Iterator[Any]:
    """Yield a cursor while keeping SQLite progress checks scoped to it.

    Callers consume the cursor incrementally.  The old helper called
    ``fetchall`` and therefore retained an entire batch even though the
    callers only needed one row at a time for their updates.
    """
    failure: list[BaseException] = []

    def progress() -> int:
        if failure:
            return 1
        try:
            if checkpoint is not None:
                checkpoint()
        except BaseException as error:
            failure.append(error)
            return 1
        return 0

    if checkpoint is not None:
        connection.set_progress_handler(progress, 1_000)
    try:
        yield connection.execute(sql, parameters)
    except sqlite3.OperationalError as error:
        if failure:
            raise failure[0] from error
        raise
    finally:
        if checkpoint is not None:
            connection.set_progress_handler(None, 0)


@dataclass(frozen=True)
class TrainingPolicy:
    subject_id: str
    record_enabled: bool
    export_enabled: bool
    include_private_psychology: bool
    include_conversations: bool
    include_model_io: bool
    include_external_actions: bool
    include_workspace: bool
    policy_version: int
    updated_at: str


@dataclass(frozen=True)
class TrainingRecord:
    record_id: str
    subject_id: str
    event_id: str
    record_kind: str
    privacy_level: str
    eligibility: str
    source_hash: str
    redaction_status: str
    consent_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class StorageArchive:
    archive_id: str
    subject_id: str
    storage_class: str
    object_key: str
    byte_size: int
    content_hash: str
    status: str
    created_at: str
    verified_at: str | None


@dataclass(frozen=True)
class StorageLayout:
    """Cross-platform directory boundary for subject data and workspaces."""

    root: Path
    subject: Path
    training_raw: Path
    workspace: Path
    cache: Path
    exports: Path

    @classmethod
    def create(cls, root: Path | str) -> StorageLayout:
        base = Path(root).expanduser().resolve()
        layout = cls(
            root=base,
            subject=base / "subject",
            training_raw=base / "training_raw",
            workspace=base / "workspace",
            cache=base / "cache",
            exports=base / "exports",
        )
        directories = (
            layout.subject,
            layout.training_raw,
            layout.workspace,
            layout.cache,
            layout.exports,
        )
        for path in directories:
            path.mkdir(parents=True, exist_ok=True)
        return layout

    def path_for(self, category: str, relative: str = "") -> Path:
        roots = {
            "subject": self.subject,
            "training_raw": self.training_raw,
            "workspace": self.workspace,
            "cache": self.cache,
            "exports": self.exports,
        }
        if category not in roots:
            raise ValueError(f"unknown storage category: {category}")
        candidate = (roots[category] / relative).resolve()
        if candidate != roots[category] and roots[category] not in candidate.parents:
            raise ValueError("storage path escapes its category root")
        return candidate


class StorageQuotaLike(Protocol):
    @property
    def subject_bytes(self) -> int: ...

    @property
    def training_bytes(self) -> int: ...

    @property
    def workspace_bytes(self) -> int: ...

    @property
    def warning_ratio(self) -> float: ...


@dataclass(frozen=True)
class StorageUsage:
    """One physical storage observation with its reclaimable SQLite share."""

    subject_bytes: int
    training_bytes: int
    workspace_bytes: int
    free_bytes: int
    effective_subject_bytes: int | None = None
    database_bytes: int = 0
    database_reclaimable_bytes: int = 0
    wal_bytes: int = 0
    local_archive_bytes: int = 0
    cloud_staging_bytes: int = 0
    exports_bytes: int = 0
    database_shm_bytes: int = 0
    database_snapshot_bytes: int = 0
    # Bytes shared by the data root (SQLite/WAL, shared secret material and
    # unscoped legacy directories).  Subject-scoped scans deliberately keep
    # this out of ``subject_bytes`` so one subject cannot consume another's
    # quota by virtue of shared infrastructure.
    shared_bytes: int = 0

    def __post_init__(self) -> None:
        effective = self.effective_subject_bytes
        if effective is None:
            effective = max(0, self.subject_bytes - self.database_reclaimable_bytes)
            object.__setattr__(self, "effective_subject_bytes", effective)
        for name in (
            "subject_bytes",
            "training_bytes",
            "workspace_bytes",
            "free_bytes",
            "effective_subject_bytes",
            "database_bytes",
            "database_reclaimable_bytes",
            "wal_bytes",
            "local_archive_bytes",
            "cloud_staging_bytes",
            "exports_bytes",
            "database_shm_bytes",
            "database_snapshot_bytes",
            "shared_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"storage usage {name} must be a non-negative integer")
        if self.database_reclaimable_bytes > self.database_bytes:
            raise ValueError("database reclaimable bytes exceed the database file size")
        if effective > self.subject_bytes:
            raise ValueError("effective subject bytes exceed physical subject bytes")

    @property
    def physical_database_bytes(self) -> int:
        return (
            self.database_bytes
            + self.wal_bytes
            + self.database_shm_bytes
            + self.database_snapshot_bytes
        )

    @property
    def effective_database_bytes(self) -> int:
        return max(0, self.physical_database_bytes - self.database_reclaimable_bytes)

    @property
    def freelist_bytes(self) -> int:
        return self.database_reclaimable_bytes

    def over_quota(self, quota: StorageQuotaLike) -> tuple[str, ...]:
        over: list[str] = []
        if self.subject_bytes > quota.subject_bytes:
            over.append("subject")
        if self.training_bytes > quota.training_bytes:
            over.append("training")
        if self.workspace_bytes > quota.workspace_bytes:
            over.append("workspace")
        return tuple(over)

    def warnings(self, quota: StorageQuotaLike) -> tuple[str, ...]:
        warnings: list[str] = []
        for name, value, limit in (
            ("subject", self.subject_bytes, quota.subject_bytes),
            ("training", self.training_bytes, quota.training_bytes),
            ("workspace", self.workspace_bytes, quota.workspace_bytes),
        ):
            if value >= limit * quota.warning_ratio:
                warnings.append(name)
        return tuple(warnings)


class StorageUsageScanner:
    """Measure quota domains without following links outside the data root."""

    def __init__(
        self,
        root: Path | str,
        database: Database | None = None,
        subject_id: str | None = None,
    ):
        self.root = Path(root).expanduser().resolve()
        self.database = database
        self.subject_id = subject_id

    def scan(self) -> StorageUsage:
        database_path = (
            self.database.path if self.database is not None else self.root / "noyra.sqlite3"
        )
        database_bytes = self._file_size(database_path)
        wal_bytes = self._file_size(database_path.with_name(database_path.name + "-wal"))
        shm_bytes = self._file_size(database_path.with_name(database_path.name + "-shm"))
        snapshot_bytes = sum(
            self._file_size(path)
            for path in database_path.parent.glob(f".{database_path.name}.snapshot_*.sqlite*")
        )
        reclaimable_bytes = min(database_bytes, self._database_reclaimable_bytes(database_path))
        scoped = self.subject_id is not None
        local_archive_bytes = (
            self._subject_archive_size() if scoped else self._size(self.root / "subject" / "cold")
        )
        exports_bytes = (
            self._subject_exports_size() if scoped else self._size(self.root / "exports")
        )
        cloud_staging_bytes = (
            self._subject_staging_size()
            if scoped
            else self._size(self.root / "training_raw" / "archive_queue")
        )
        training_shared_bytes = self._training_shared_size() if scoped else 0
        if scoped:
            subject_root_bytes = self._subject_root_size()
            subject_bytes = subject_root_bytes + exports_bytes
            shared_bytes = (
                database_bytes
                + wal_bytes
                + shm_bytes
                + snapshot_bytes
                + self._size(self.root / "secrets")
                + self._legacy_unscoped_size()
                + training_shared_bytes
            )
            if self._subject_count() == 1:
                subject_bytes += shared_bytes
        else:
            subject_bytes = (
                self._size(self.root / "subject")
                + exports_bytes
                + self._size(self.root / "secrets")
                + database_bytes
                + wal_bytes
                + shm_bytes
                + snapshot_bytes
            )
            shared_bytes = 0
        subject_reclaimable = reclaimable_bytes if not scoped or self._subject_count() == 1 else 0
        return StorageUsage(
            subject_bytes=subject_bytes,
            effective_subject_bytes=max(0, subject_bytes - subject_reclaimable),
            database_bytes=database_bytes,
            database_reclaimable_bytes=reclaimable_bytes,
            wal_bytes=wal_bytes,
            database_shm_bytes=shm_bytes,
            database_snapshot_bytes=snapshot_bytes,
            local_archive_bytes=local_archive_bytes,
            cloud_staging_bytes=cloud_staging_bytes,
            exports_bytes=exports_bytes,
            training_bytes=(cloud_staging_bytes + training_shared_bytes)
            if scoped
            else self._size(self.root / "training_raw"),
            workspace_bytes=self._subject_workspace_size()
            if scoped
            else self._size(self.root / "workspace"),
            free_bytes=shutil.disk_usage(self.root).free,
            shared_bytes=shared_bytes,
        )

    def _subject_storage_key(self) -> tuple[str, str] | None:
        if self.subject_id is None or self.database is None:
            return None
        from .identity import IdentityStore

        return self.subject_id, IdentityStore(self.database).storage_key(self.subject_id)

    def _subject_count(self) -> int:
        if self.database is None:
            return 1
        try:
            with self.database.connection() as connection:
                return int(
                    connection.execute("SELECT COUNT(*) FROM subject_identity").fetchone()[0]
                )
        except sqlite3.Error:
            return 2

    def _subject_archive_root(self) -> Path | None:
        identity = self._subject_storage_key()
        if identity is None:
            return None
        subject_id, storage_key = identity
        keyed = self._non_mutating_subject_dir(self.root / "subject", storage_key)
        if keyed is not None:
            return keyed / "cold"
        # Older installations used one legacy ``subject/cold`` root.  It is
        # attributable only while this database contains one subject; with
        # multiple subjects it remains shared overhead by design.
        legacy_subject = self.root / "subject" / subject_id
        count = self._subject_count()
        if legacy_subject.is_dir() and not self._is_unsafe_entry(legacy_subject):
            return legacy_subject / "cold"
        legacy = self.root / "subject"
        if not legacy.is_dir() or self._is_unsafe_entry(legacy):
            # Legacy single-subject installations used subject/cold directly.
            return None
        return legacy / "cold" if count == 1 else None

    def _subject_archive_size(self) -> int:
        root = self._subject_archive_root()
        return 0 if root is None else self._size(root)

    def _subject_staging_size(self) -> int:
        identity = self._subject_storage_key()
        if identity is None:
            return 0
        subject_id, storage_key = identity
        root = self._non_mutating_subject_dir(
            self.root / "training_raw" / "archive_queue", storage_key
        )
        if root is None and self._subject_count() == 1:
            legacy = self.root / "training_raw" / "archive_queue" / subject_id
            root = legacy if legacy.is_dir() and not self._is_unsafe_entry(legacy) else None
        return 0 if root is None else self._size(root)

    def _training_shared_size(self) -> int:
        """Count training files that cannot be assigned to this subject.

        Subject-keyed archive queue directories are excluded. Legacy or
        unscoped entries are shared overhead and remain quota-visible instead
        of silently disappearing from a scoped training scan.
        """
        root = self.root / "training_raw"
        if not root.is_dir() or self._is_unsafe_entry(root):
            return 0
        total = 0
        known = self._known_storage_keys()
        queue_root = root / "archive_queue"
        try:
            for child in root.iterdir():
                if child.name == "archive_queue":
                    continue
                total += self._size(child)
            if queue_root.is_dir() and not self._is_unsafe_entry(queue_root):
                for child in queue_root.iterdir():
                    if child.name in known:
                        continue
                    if self._subject_count() == 1 and child.name == self.subject_id:
                        continue
                    total += self._size(child)
        except OSError:
            return total
        return total

    def _subject_exports_size(self) -> int:
        if self.database is None or self.subject_id is None:
            return self._size(self.root / "exports")
        total = 0
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    "SELECT artifact_path FROM export_jobs WHERE subject_id = ? "
                    "AND artifact_path IS NOT NULL",
                    (self.subject_id,),
                )
                exports = (self.root / "exports").resolve()
                for row in rows:
                    try:
                        path = Path(str(row["artifact_path"])).expanduser().resolve()
                        if path != exports and exports in path.parents:
                            total += self._file_size(path)
                    except (OSError, ValueError):
                        continue
        except sqlite3.Error:
            return 0
        return total

    def _legacy_unscoped_size(self) -> int:
        """Account legacy/shared roots as shared overhead, never as a subject."""
        # Workspace is a separate quota domain and is reported through
        # ``workspace_bytes``.  Subject quota only receives unscoped subject
        # data that cannot be assigned to an identity.
        total = 0
        root = self.root / "subject"
        if not root.is_dir():
            return 0
        if self._subject_count() == 1:
            identity = self._subject_storage_key()
            keyed = self._non_mutating_subject_dir(root, identity[1]) if identity else None
            legacy_subject = root / str(self.subject_id)
            if keyed is None and not legacy_subject.is_dir():
                # The whole legacy root belongs to the sole subject.
                return 0
        known = self._known_storage_keys()
        try:
            for child in root.iterdir():
                if child.name in known:
                    continue
                # A legacy single-subject root is attributable elsewhere and
                # must not be counted as shared when it is the only subject.
                if self._subject_count() == 1 and child.name == self.subject_id:
                    continue
                total += self._size(child)
        except OSError:
            return total
        return total

    def _subject_workspace_size(self) -> int:
        identity = self._subject_storage_key()
        if identity is None:
            return self._size(self.root / "workspace")
        subject_id, storage_key = identity
        root = self._non_mutating_subject_dir(self.root / "workspace", storage_key)
        if root is None and self._subject_count() == 1:
            legacy = self.root / "workspace" / subject_id
            root = legacy if legacy.is_dir() and not self._is_unsafe_entry(legacy) else None
        return 0 if root is None else self._size(root)

    def _subject_root_size(self) -> int:
        identity = self._subject_storage_key()
        if identity is None:
            return 0
        subject_id, storage_key = identity
        root = self._non_mutating_subject_dir(self.root / "subject", storage_key)
        if root is None and self._subject_count() == 1:
            legacy = self.root / "subject" / subject_id
            if legacy.is_dir() and not self._is_unsafe_entry(legacy):
                root = legacy
            else:
                legacy_root = self.root / "subject"
                root = legacy_root if legacy_root.is_dir() else None
        return 0 if root is None else self._size(root)

    def _known_storage_keys(self) -> set[str]:
        if self.database is None:
            return set()
        try:
            with self.database.connection() as connection:
                return {
                    str(row[0])
                    for row in connection.execute("SELECT storage_key FROM subject_storage_keys")
                }
        except sqlite3.Error:
            return set()

    @staticmethod
    def _is_unsafe_entry(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            return True
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return (
            stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
            or not stat.S_ISDIR(metadata.st_mode)
            or path.resolve() != path
        )

    def _non_mutating_subject_dir(self, root: Path, storage_key: str) -> Path | None:
        candidate = root / storage_key
        if candidate.is_dir() and not self._is_unsafe_entry(candidate):
            return candidate
        return None

    def _database_reclaimable_bytes(self, path: Path) -> int:
        if not path.is_file():
            return 0
        try:
            if self.database is not None:
                with self.database.connection() as connection:
                    return self._freelist_bytes(connection)
            uri = f"file:{path.as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1) as connection:
                return self._freelist_bytes(connection)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return 0

    @staticmethod
    def _freelist_bytes(connection: sqlite3.Connection) -> int:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        return max(0, page_size * freelist_count)

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            if path.is_symlink() or not path.is_file():
                return 0
            return path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _size(path: Path) -> int:
        try:
            metadata = path.lstat()
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat.S_ISLNK(metadata.st_mode) or bool(
                getattr(metadata, "st_file_attributes", 0) & reparse_flag
            ):
                return 0
            if stat.S_ISREG(metadata.st_mode):
                return int(metadata.st_size)
            if not stat.S_ISDIR(metadata.st_mode):
                return 0
        except (FileNotFoundError, OSError):
            return 0
        total = 0
        stack = [path]
        while stack:
            directory = stack.pop()
            try:
                entries = os.scandir(directory)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(metadata.st_mode) or bool(
                            getattr(metadata, "st_file_attributes", 0) & reparse_flag
                        ):
                            continue
                        if stat.S_ISREG(metadata.st_mode):
                            total += int(metadata.st_size)
                        elif stat.S_ISDIR(metadata.st_mode):
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
        return total


@dataclass(frozen=True)
class StorageUsageTrend:
    sample_count: int
    observed_hours: float
    subject_growth_bytes_per_hour: float
    effective_subject_growth_bytes_per_hour: float
    free_space_change_bytes_per_hour: float
    subject_quota_eta_hours: float | None
    minimum_free_eta_hours: float | None
    forecast_horizon_hours: int


def storage_usage_sample_state_hash(row: Any) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "sample_id": row["sample_id"],
                "subject_id": row["subject_id"],
                "subject_bytes": int(row["subject_bytes"]),
                "effective_subject_bytes": int(row["effective_subject_bytes"]),
                "database_bytes": int(row["database_bytes"]),
                "database_reclaimable_bytes": int(row["database_reclaimable_bytes"]),
                "wal_bytes": int(row["wal_bytes"]),
                "local_archive_bytes": int(row["local_archive_bytes"]),
                "cloud_staging_bytes": int(row["cloud_staging_bytes"]),
                "exports_bytes": int(row["exports_bytes"]),
                "training_bytes": int(row["training_bytes"]),
                "workspace_bytes": int(row["workspace_bytes"]),
                "free_bytes": int(row["free_bytes"]),
                "subject_quota_bytes": int(row["subject_quota_bytes"]),
                "minimum_free_bytes": int(row["minimum_free_bytes"]),
                "created_at": row["created_at"],
            }
        ).encode("utf-8")
    ).hexdigest()


class StorageUsageHistory:
    """Append usage observations and derive a bounded linear forecast."""

    _MAX_SAMPLES = 256
    _MAX_LOOKBACK_HOURS = 24 * 30
    _MAX_FORECAST_HOURS = 24 * 365

    def __init__(self, database: Database, subject_id: str):
        self.database = database
        self.subject_id = subject_id

    def record(
        self,
        usage: StorageUsage,
        quota: StorageQuotaLike,
        minimum_free_bytes: int,
        *,
        created_at: str | None = None,
    ) -> str:
        if minimum_free_bytes < 1:
            raise ValueError("minimum free bytes must be positive")
        observed_at = self._timestamp(created_at)
        sample: dict[str, Any] = {
            "sample_id": new_id("storage-sample"),
            "subject_id": self.subject_id,
            "subject_bytes": usage.subject_bytes,
            "effective_subject_bytes": usage.effective_subject_bytes,
            "database_bytes": usage.database_bytes,
            "database_reclaimable_bytes": usage.database_reclaimable_bytes,
            "wal_bytes": usage.wal_bytes,
            "local_archive_bytes": usage.local_archive_bytes,
            "cloud_staging_bytes": usage.cloud_staging_bytes,
            "exports_bytes": usage.exports_bytes,
            "training_bytes": usage.training_bytes,
            "workspace_bytes": usage.workspace_bytes,
            "free_bytes": usage.free_bytes,
            "subject_quota_bytes": quota.subject_bytes,
            "minimum_free_bytes": minimum_free_bytes,
            "created_at": observed_at,
        }
        state_hash = storage_usage_sample_state_hash(sample)
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO storage_usage_samples(
                    sample_id, subject_id, subject_bytes, effective_subject_bytes,
                    database_bytes, database_reclaimable_bytes, wal_bytes,
                    local_archive_bytes, cloud_staging_bytes, exports_bytes,
                    training_bytes, workspace_bytes, free_bytes, subject_quota_bytes,
                    minimum_free_bytes, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sample["sample_id"],
                    sample["subject_id"],
                    sample["subject_bytes"],
                    sample["effective_subject_bytes"],
                    sample["database_bytes"],
                    sample["database_reclaimable_bytes"],
                    sample["wal_bytes"],
                    sample["local_archive_bytes"],
                    sample["cloud_staging_bytes"],
                    sample["exports_bytes"],
                    sample["training_bytes"],
                    sample["workspace_bytes"],
                    sample["free_bytes"],
                    sample["subject_quota_bytes"],
                    sample["minimum_free_bytes"],
                    state_hash,
                    sample["created_at"],
                ),
            )
        return str(sample["sample_id"])

    def record_if_due(
        self,
        usage: StorageUsage,
        quota: StorageQuotaLike,
        minimum_free_bytes: int,
        *,
        minimum_interval_seconds: int = 3_600,
    ) -> str | None:
        bounded_interval = max(60, min(minimum_interval_seconds, 24 * 60 * 60))
        now = datetime.now(UTC)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT created_at FROM storage_usage_samples WHERE subject_id = ? "
                "ORDER BY created_at DESC, sample_id DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
        if row is not None:
            try:
                last = datetime.fromisoformat(str(row["created_at"]))
            except ValueError:
                last = None
            if (
                last is not None
                and last.tzinfo is not None
                and now - last.astimezone(UTC) < timedelta(seconds=bounded_interval)
            ):
                return None
        return self.record(
            usage,
            quota,
            minimum_free_bytes,
            created_at=now.isoformat(timespec="microseconds"),
        )

    def predict(
        self,
        quota: StorageQuotaLike,
        minimum_free_bytes: int,
        *,
        max_samples: int = 64,
        lookback_hours: int = 24 * 7,
        forecast_horizon_hours: int = 24 * 30,
    ) -> StorageUsageTrend:
        bounded_samples = max(2, min(max_samples, self._MAX_SAMPLES))
        bounded_lookback = max(1, min(lookback_hours, self._MAX_LOOKBACK_HOURS))
        bounded_forecast = max(1, min(forecast_horizon_hours, self._MAX_FORECAST_HOURS))
        points: list[tuple[datetime, float, float, float]] = []
        latest_at: datetime | None = None
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT subject_bytes, effective_subject_bytes, free_bytes, created_at "
                "FROM storage_usage_samples WHERE subject_id = ? "
                "ORDER BY created_at DESC, sample_id DESC LIMIT ?",
                (self.subject_id, bounded_samples),
            )
            for row in rows:
                try:
                    observed_at = datetime.fromisoformat(str(row["created_at"])).astimezone(UTC)
                except (TypeError, ValueError):
                    continue
                if latest_at is None:
                    latest_at = observed_at
                if latest_at - observed_at > timedelta(hours=bounded_lookback):
                    continue
                points.append(
                    (
                        observed_at,
                        float(row["subject_bytes"]),
                        float(row["effective_subject_bytes"]),
                        float(row["free_bytes"]),
                    )
                )
        points.sort(key=lambda item: item[0])
        if not points:
            return StorageUsageTrend(0, 0.0, 0.0, 0.0, 0.0, None, None, bounded_forecast)
        first_at = points[0][0]
        hours = [(point[0] - first_at).total_seconds() / 3600 for point in points]
        subject_slope = self._slope(hours, [point[1] for point in points])
        effective_slope = self._slope(hours, [point[2] for point in points])
        free_slope = self._slope(hours, [point[3] for point in points])
        latest_subject = points[-1][1]
        latest_free = points[-1][3]
        quota_eta = self._eta(
            current=latest_subject,
            boundary=float(quota.subject_bytes),
            rate=subject_slope,
            increasing=True,
            horizon=bounded_forecast,
        )
        free_eta = self._eta(
            current=latest_free,
            boundary=float(minimum_free_bytes),
            rate=free_slope,
            increasing=False,
            horizon=bounded_forecast,
        )
        return StorageUsageTrend(
            sample_count=len(points),
            observed_hours=max(hours),
            subject_growth_bytes_per_hour=subject_slope,
            effective_subject_growth_bytes_per_hour=effective_slope,
            free_space_change_bytes_per_hour=free_slope,
            subject_quota_eta_hours=quota_eta,
            minimum_free_eta_hours=free_eta,
            forecast_horizon_hours=bounded_forecast,
        )

    @staticmethod
    def _timestamp(value: str | None) -> str:
        if value is None:
            return datetime.now(UTC).isoformat(timespec="microseconds")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("storage usage timestamp is invalid") from error
        if parsed.tzinfo is None:
            raise ValueError("storage usage timestamp must include a timezone")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")

    @staticmethod
    def _slope(hours: list[float], values: list[float]) -> float:
        if len(hours) < 2 or hours[-1] <= hours[0]:
            return 0.0
        average_hour = sum(hours) / len(hours)
        average_value = sum(values) / len(values)
        denominator = sum((hour - average_hour) ** 2 for hour in hours)
        if denominator <= 0:
            return 0.0
        return (
            sum(
                (hour - average_hour) * (value - average_value)
                for hour, value in zip(hours, values, strict=True)
            )
            / denominator
        )

    @staticmethod
    def _eta(
        *,
        current: float,
        boundary: float,
        rate: float,
        increasing: bool,
        horizon: int,
    ) -> float | None:
        if (increasing and current >= boundary) or (not increasing and current <= boundary):
            return 0.0
        if (increasing and rate <= 0) or (not increasing and rate >= 0):
            return None
        eta = (boundary - current) / rate
        if eta < 0 or eta > horizon:
            return None
        return eta


class TrainingStore:
    """Owns opt-in policy and immutable provenance for training records."""

    _POLICY_FIELDS = (
        "record_enabled",
        "export_enabled",
        "include_private_psychology",
        "include_conversations",
        "include_model_io",
        "include_external_actions",
        "include_workspace",
    )

    def __init__(self, database: Database):
        self.database = database

    def policy(self, subject_id: str) -> TrainingPolicy:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM training_policies WHERE subject_id = ?", (subject_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"training policy not found: {subject_id}")
        return self._policy(row)

    def ensure_policy(self, subject_id: str) -> TrainingPolicy:
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO training_policies(subject_id, updated_at)
                   VALUES (?, ?)""",
                (subject_id, now),
            )
            row = connection.execute(
                "SELECT * FROM training_policies WHERE subject_id = ?", (subject_id,)
            ).fetchone()
        return self._policy(row)

    def update_policy(
        self,
        subject_id: str,
        *,
        actor: str = "operator",
        reason: str = "training policy updated",
        expected_version: int | None = None,
        **changes: bool,
    ) -> TrainingPolicy:
        allowed = set(self._POLICY_FIELDS)
        if not changes or set(changes) - allowed:
            raise ValueError("at least one valid training policy field is required")
        if any(not isinstance(value, bool) for value in changes.values()):
            raise TypeError("training policy values must be boolean")
        if expected_version is not None and (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise TypeError("expected training policy version must be a positive integer")
        with self.database.transaction() as connection:
            current_row = connection.execute(
                "SELECT * FROM training_policies WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            if current_row is None:
                raise NotFoundError(f"training policy not found: {subject_id}")
            before_version = int(current_row["policy_version"])
            if expected_version is not None and expected_version != before_version:
                raise TrainingPolicyConflictError(
                    f"training policy version changed: expected {expected_version}, "
                    f"found {before_version}"
                )
            values = {name: bool(current_row[name]) for name in self._POLICY_FIELDS}
            values.update(changes)
            now = utc_now()
            updated = connection.execute(
                """UPDATE training_policies SET
                   record_enabled = ?, export_enabled = ?, include_private_psychology = ?,
                   include_conversations = ?, include_model_io = ?, include_external_actions = ?,
                   include_workspace = ?, policy_version = policy_version + 1, updated_at = ?
                   WHERE subject_id = ? AND policy_version = ?""",
                (
                    int(values["record_enabled"]),
                    int(values["export_enabled"]),
                    int(values["include_private_psychology"]),
                    int(values["include_conversations"]),
                    int(values["include_model_io"]),
                    int(values["include_external_actions"]),
                    int(values["include_workspace"]),
                    now,
                    subject_id,
                    before_version,
                ),
            )
            if updated.rowcount != 1:
                raise TrainingPolicyConflictError("training policy changed during update")
            after_row = connection.execute(
                "SELECT * FROM training_policies WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            if after_row is None:
                raise NotFoundError(f"training policy not found: {subject_id}")
            before = {
                **{name: bool(current_row[name]) for name in self._POLICY_FIELDS},
                "policy_version": before_version,
                "updated_at": str(current_row["updated_at"]),
            }
            after = {
                **{name: bool(after_row[name]) for name in self._POLICY_FIELDS},
                "policy_version": int(after_row["policy_version"]),
                "updated_at": str(after_row["updated_at"]),
            }
            connection.execute(
                """INSERT INTO audit_records(
                    audit_id, subject_id, action, actor, payload_json, occurred_at
                ) VALUES (?, ?, 'training_policy_updated', ?, ?, ?)""",
                (
                    new_id("audit"),
                    subject_id,
                    actor,
                    canonical_json(
                        {
                            "reason": reason,
                            "changed": changes,
                            "before": before,
                            "after": after,
                            "previous_policy_version": before_version,
                            "policy_version": int(after_row["policy_version"]),
                            "effective_at": now,
                        }
                    ),
                    now,
                ),
            )
            committed = self._policy(after_row)
        self.backfill_all(subject_id)
        self.reclassify_all(subject_id)
        return committed

    def reclassify(self, subject_id: str, *, limit: int = 100_000) -> int:
        """Apply the current consent policy to existing provenance rows."""
        count, _ = self._reclassify_batch(subject_id, limit=limit)
        return count

    def reclassify_all(
        self,
        subject_id: str,
        *,
        batch_size: int = 1_000,
        checkpoint: Callable[[], None] | None = None,
    ) -> int:
        """Apply the current policy to every provenance row using stable cursors."""
        total = 0
        cursor: tuple[str, str] | None = None
        while True:
            if checkpoint is not None:
                checkpoint()
            count, cursor = self._reclassify_batch(
                subject_id,
                limit=batch_size,
                cursor=cursor,
                checkpoint=checkpoint,
            )
            total += count
            if count == 0:
                return total

    def _reclassify_batch(
        self,
        subject_id: str,
        *,
        limit: int,
        cursor: tuple[str, str] | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> tuple[int, tuple[str, str] | None]:
        bounded = max(1, min(limit, 10_000))
        with self.database.transaction() as connection:
            policy_row = connection.execute(
                "SELECT * FROM training_policies WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            if policy_row is None:
                raise NotFoundError(f"training policy not found: {subject_id}")
            cursor_clause = ""
            cursor_values: tuple[Any, ...] = ()
            if cursor is not None:
                cursor_clause = "AND (r.created_at > ? OR (r.created_at = ? AND r.record_id > ?))"
                cursor_values = (cursor[0], cursor[0], cursor[1])
            count = 0
            next_cursor: tuple[str, str] | None = None
            with _checkpointed_rows(
                connection,
                """SELECT r.record_id, r.created_at, r.event_id, e.event_type, e.privacy_level
                   FROM training_records r JOIN events e ON e.event_id = r.event_id
                   WHERE r.subject_id = ? """
                + cursor_clause
                + " ORDER BY r.created_at, r.record_id LIMIT ?",
                (subject_id, *cursor_values, bounded),
                checkpoint,
            ) as rows:
                for index, row in enumerate(rows, start=1):
                    if checkpoint is not None and index % 64 == 0:
                        checkpoint()
                    eligibility, redaction = self._classification(
                        str(row["event_type"]), str(row["privacy_level"]), policy_row
                    )
                    connection.execute(
                        """UPDATE training_records SET eligibility = ?, redaction_status = ?,
                           consent_version = ?, updated_at = ? WHERE record_id = ?""",
                        (
                            eligibility,
                            redaction,
                            int(policy_row["policy_version"]),
                            utc_now(),
                            row["record_id"],
                        ),
                    )
                    count += 1
                    next_cursor = (str(row["created_at"]), str(row["record_id"]))
        return count, next_cursor

    def backfill(
        self,
        subject_id: str,
        *,
        limit: int = 10_000,
        checkpoint: Callable[[], None] | None = None,
    ) -> int:
        """Classify existing events after a policy change without copying payloads."""
        bounded = max(1, min(limit, 100_000))
        with self.database.transaction() as connection:
            policy_row = connection.execute(
                "SELECT * FROM training_policies WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            if policy_row is None:
                raise NotFoundError(f"training policy not found: {subject_id}")
            created = 0
            with _checkpointed_rows(
                connection,
                """SELECT e.event_id, e.event_type, e.privacy_level, e.payload_hash,
                          e.occurred_at, e.observed_at
                   FROM events e LEFT JOIN training_records r ON r.event_id = e.event_id
                   WHERE e.subject_id = ? AND r.event_id IS NULL
                   ORDER BY e.occurred_at, e.event_id LIMIT ?""",
                (subject_id, bounded),
                checkpoint,
            ) as rows:
                for index, row in enumerate(rows, start=1):
                    if checkpoint is not None and index % 64 == 0:
                        checkpoint()
                    if not bool(policy_row["record_enabled"]):
                        break
                    self._insert_event_record(connection, subject_id, row, policy_row)
                    created += 1
        return created

    def backfill_all(
        self,
        subject_id: str,
        *,
        batch_size: int = 1_000,
        checkpoint: Callable[[], None] | None = None,
    ) -> int:
        """Backfill all missing provenance rows in bounded repeatable batches."""
        total = 0
        while True:
            if checkpoint is not None:
                checkpoint()
            created = self.backfill(
                subject_id,
                limit=batch_size,
                checkpoint=checkpoint,
            )
            total += created
            if created == 0 or created < max(1, min(batch_size, 10_000)):
                return total

    def record_event(
        self,
        subject_id: str,
        event_id: str,
        event_type: str,
        privacy_level: str,
        source_hash: str,
        *,
        occurred_at: str | None = None,
        observed_at: str | None = None,
        connection: Any | None = None,
    ) -> TrainingRecord | None:
        """Record event provenance without duplicating the event payload."""
        if connection is None:
            with self.database.transaction() as transaction:
                return self.record_event(
                    subject_id,
                    event_id,
                    event_type,
                    privacy_level,
                    source_hash,
                    occurred_at=occurred_at,
                    observed_at=observed_at,
                    connection=transaction,
                )
        try:
            # Provenance is a subject-scoped relation, not a caller-supplied
            # label.  Resolve the canonical event row inside the same write
            # transaction and reject every cross-subject or hash/type mismatch
            # before the INSERT trigger is reached.
            event_row = connection.execute(
                "SELECT subject_id, event_type, privacy_level, payload_hash, "
                "occurred_at, observed_at FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if event_row is None:
                raise NotFoundError(f"event not found: {event_id}")
            if (
                str(event_row["subject_id"]) != subject_id
                or str(event_row["event_type"]) != event_type
                or str(event_row["privacy_level"]) != privacy_level
                or str(event_row["payload_hash"]) != source_hash
            ):
                raise IntegrityError("training provenance event subject or content mismatch")
            policy_row = connection.execute(
                "SELECT * FROM training_policies WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            if policy_row is None:
                connection.execute(
                    "INSERT INTO training_policies(subject_id, updated_at) VALUES (?, ?)",
                    (subject_id, utc_now()),
                )
                policy_row = connection.execute(
                    "SELECT * FROM training_policies WHERE subject_id = ?", (subject_id,)
                ).fetchone()
            if not bool(policy_row["record_enabled"]):
                result = None
            else:
                self._insert_event_record(
                    connection,
                    subject_id,
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "privacy_level": privacy_level,
                        "payload_hash": source_hash,
                        "occurred_at": event_row["occurred_at"],
                        "observed_at": event_row["observed_at"],
                    },
                    policy_row,
                )
                row = connection.execute(
                    "SELECT * FROM training_records WHERE subject_id = ? AND event_id = ?",
                    (subject_id, event_id),
                ).fetchone()
                result = self._record(row)
            return result
        except Exception:
            raise

    @staticmethod
    def _insert_event_record(connection: Any, subject_id: str, row: Any, policy_row: Any) -> None:
        event_type = str(row["event_type"])
        privacy_level = str(row["privacy_level"])
        eligibility, redaction = TrainingStore._classification(
            event_type, privacy_level, policy_row
        )
        now = utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO training_records(
               record_id, subject_id, event_id, record_kind, privacy_level, eligibility,
               source_hash, redaction_status, consent_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("train"),
                subject_id,
                row["event_id"],
                event_type,
                privacy_level,
                eligibility,
                row["payload_hash"],
                redaction,
                int(policy_row["policy_version"]),
                row["occurred_at"],
                row["observed_at"] or now,
            ),
        )

    @staticmethod
    def _classification(event_type: str, privacy_level: str, policy_row: Any) -> tuple[str, str]:
        restricted = privacy_level in {"private", "secret"}
        psychology = event_type in {
            "psychological_snapshot",
            "sleep_reflection",
            "metacognitive_outcome",
            "self_model_committed",
            "intrinsic_thought",
        } or event_type.startswith(("affect_", "thought_"))
        conversation = event_type in {"human_message", "interaction_received", "interaction_sent"}
        model_io = event_type.startswith("model_") or event_type in {
            "cognition_proposal",
            "cognition_rejected",
            "self_model_committed",
        }
        external_action = event_type.startswith("action_") or event_type in {
            "external_action",
            "web_action",
            "browser_action",
        }
        workspace = event_type.startswith("workspace_") or event_type in {
            "project_artifact_created",
            "project_artifact_updated",
        }
        private_psychology_excluded = (
            restricted and psychology and not bool(policy_row["include_private_psychology"])
        )
        conversation_excluded = conversation and not bool(policy_row["include_conversations"])
        model_io_excluded = model_io and not bool(policy_row["include_model_io"])
        external_action_excluded = external_action and not bool(
            policy_row["include_external_actions"]
        )
        workspace_excluded = workspace and not bool(policy_row["include_workspace"])
        excluded = (
            private_psychology_excluded
            or conversation_excluded
            or model_io_excluded
            or external_action_excluded
            or workspace_excluded
        )
        explicitly_included_private = privacy_level == "private" and (
            (psychology and bool(policy_row["include_private_psychology"]))
            or (conversation and bool(policy_row["include_conversations"]))
            or (model_io and bool(policy_row["include_model_io"]))
            or (external_action and bool(policy_row["include_external_actions"]))
            or (workspace and bool(policy_row["include_workspace"]))
        )
        if excluded:
            return "excluded", "excluded"
        if explicitly_included_private:
            return "eligible", "redacted"
        return ("restricted", "pending") if restricted else ("eligible", "clear")

    def list_records(
        self, subject_id: str, *, limit: int = 100, eligibility: str | None = None
    ) -> list[TrainingRecord]:
        bounded = max(1, min(limit, 10_000))
        with self.database.connection() as connection:
            if eligibility is None:
                rows = connection.execute(
                    "SELECT * FROM training_records WHERE subject_id = "
                    "? ORDER BY created_at DESC, record_id DESC LIMIT ?",
                    (subject_id, bounded),
                )
            else:
                rows = connection.execute(
                    "SELECT * FROM training_records WHERE subject_id = ? AND eligibility = ? "
                    "ORDER BY created_at DESC, record_id DESC LIMIT ?",
                    (subject_id, eligibility, bounded),
                )
            return [self._record(row) for row in rows]

    @staticmethod
    def _policy(row: Any) -> TrainingPolicy:
        return TrainingPolicy(
            subject_id=row["subject_id"],
            record_enabled=bool(row["record_enabled"]),
            export_enabled=bool(row["export_enabled"]),
            include_private_psychology=bool(row["include_private_psychology"]),
            include_conversations=bool(row["include_conversations"]),
            include_model_io=bool(row["include_model_io"]),
            include_external_actions=bool(row["include_external_actions"]),
            include_workspace=bool(row["include_workspace"]),
            policy_version=int(row["policy_version"]),
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _record(row: Any) -> TrainingRecord:
        return TrainingRecord(
            record_id=row["record_id"],
            subject_id=row["subject_id"],
            event_id=row["event_id"],
            record_kind=row["record_kind"],
            privacy_level=row["privacy_level"],
            eligibility=row["eligibility"],
            source_hash=row["source_hash"],
            redaction_status=row["redaction_status"],
            consent_version=int(row["consent_version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class ArchiveStore:
    """Tracks encrypted local/cloud archives; object content lives elsewhere."""

    def __init__(self, database: Database):
        self.database = database

    def register(
        self,
        subject_id: str,
        *,
        storage_class: str,
        object_key: str,
        byte_size: int,
        payload: bytes,
        status: str = "pending",
    ) -> StorageArchive:
        if storage_class not in {"warm", "cold", "cloud"}:
            raise ValueError("invalid storage class")
        if status not in {"pending", "uploaded", "verified", "failed", "restored"}:
            raise ValueError("invalid archive status")
        if byte_size < 0 or not object_key.strip():
            raise ValueError("archive size and key are required")
        digest = hashlib.sha256(payload).hexdigest()
        if byte_size != len(payload):
            raise ValueError("archive byte size does not match payload")
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO storage_archives(
                   archive_id, subject_id, storage_class, object_key, byte_size, content_hash,
                   status, created_at, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("archive"),
                    subject_id,
                    storage_class,
                    object_key,
                    byte_size,
                    digest,
                    status,
                    now,
                    now if status == "verified" else None,
                ),
            )
            row = connection.execute(
                "SELECT * FROM storage_archives WHERE subject_id = ? AND object_key = ?",
                (subject_id, object_key),
            ).fetchone()
        return self._archive(row)

    def list(self, subject_id: str, *, limit: int = 100) -> list[StorageArchive]:
        bounded = max(1, min(limit, 10_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM storage_archives WHERE subject_id = ? "
                "ORDER BY created_at DESC, archive_id DESC LIMIT ?",
                (subject_id, bounded),
            )
            return [self._archive(row) for row in rows]

    @staticmethod
    def _archive(row: Any) -> StorageArchive:
        return StorageArchive(
            archive_id=row["archive_id"],
            subject_id=row["subject_id"],
            storage_class=row["storage_class"],
            object_key=row["object_key"],
            byte_size=int(row["byte_size"]),
            content_hash=row["content_hash"],
            status=row["status"],
            created_at=row["created_at"],
            verified_at=row["verified_at"],
        )
