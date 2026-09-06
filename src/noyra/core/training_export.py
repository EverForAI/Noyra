"""Consent-aware, reproducible training dataset exports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .archive import StorageUsageScanner
from .database import CURRENT_SCHEMA_VERSION, Database
from .errors import PayloadLimitError
from .events import EventStore
from .identity import IdentityStore, validate_subject_id
from .payload_codec import decompress_text
from .redaction import redact_payload
from .storage import TrainingStore
from .subject_storage import SubjectStorageDirectory
from .types import canonical_json, content_hash, new_id, utc_now

if TYPE_CHECKING:
    from .export_jobs import ExportControl


_ELIGIBLE_ITEMS_SQL = """SELECT r.*, e.event_type, e.source, e.occurred_at, e.observed_at,
          CASE
              WHEN e.payload_archive_key IS NOT NULL
                OR length(CAST(e.payload_json AS BLOB)) <= ?
              THEN e.payload_json
          END AS payload_json,
          length(CAST(e.payload_json AS BLOB)) AS payload_stored_bytes,
          e.causal_parent_ids_json,
          e.payload_archive_key, e.payload_archived_at
   FROM events e INDEXED BY idx_events_subject_export_time
   CROSS JOIN training_records r
   WHERE e.subject_id = ? AND r.event_id = e.event_id
     AND r.subject_id = ? AND r.eligibility = 'eligible'
     AND r.redaction_status IN ('clear', 'redacted')
     AND r.consent_version = ?
   ORDER BY e.occurred_at, e.event_id"""

_MODEL_IO_SQL = """SELECT call_id, provider, model, purpose, request_hash,
          CASE WHEN length(CAST(request_json AS BLOB)) <= ? THEN request_json END
              AS request_json,
          length(CAST(request_json AS BLOB)) AS request_stored_bytes,
          CASE WHEN response_json IS NULL OR length(CAST(response_json AS BLOB)) <= ?
              THEN response_json END AS response_json,
          length(CAST(response_json AS BLOB)) AS response_stored_bytes,
          response_hash, status, resource_pool, resource_group_id,
          capture_policy_version, created_at, completed_at
   FROM model_calls INDEXED BY idx_model_calls_subject_export_time
   WHERE subject_id = ? AND request_json IS NOT NULL
   ORDER BY created_at, call_id"""

_PENDING_ARCHIVE_NAME = re.compile(r"^\..+\.pending_[0-9a-f]{32}\.pending$")


@dataclass(frozen=True)
class TrainingExportArtifact:
    filename: str
    content: bytes
    sha256: str
    row_count: int
    export_id: str


class TrainingConsentChangedError(PermissionError):
    """The consent lease changed before a training artifact could be published."""


class TrainingExportLimitError(RuntimeError):
    """A training export crossed an explicit memory, disk, or archive bound."""


@dataclass(frozen=True)
class TrainingExportLimits:
    max_work_bytes: int = 750_000_000
    max_archive_bytes: int = 1_000_000_000
    max_compatibility_bytes: int = 200_000_000
    subject_quota_bytes: int = 2_000_000_000
    minimum_free_bytes: int = 10_000_000
    shard_rows: int = 10_000
    shard_bytes: int = 25_000_000
    max_record_bytes: int = 2_000_000
    max_episode_events: int = 512
    max_episode_span_seconds: int = 21_600
    max_episode_bytes: int = 1_000_000
    max_archive_segment_bytes: int = 64_000_000
    max_dataset_shards: int = 10_000
    max_workspace_files: int = 10_000
    max_workspace_bytes: int = 100_000_000
    max_legacy_rows: int = 100_000

    def __post_init__(self) -> None:
        byte_limits = (
            self.max_work_bytes,
            self.max_archive_bytes,
            self.max_compatibility_bytes,
            self.subject_quota_bytes,
            self.minimum_free_bytes,
            self.shard_bytes,
            self.max_record_bytes,
            self.max_episode_bytes,
            self.max_archive_segment_bytes,
            self.max_workspace_bytes,
        )
        count_limits = (
            self.shard_rows,
            self.max_episode_events,
            self.max_episode_span_seconds,
            self.max_dataset_shards,
            self.max_workspace_files,
            self.max_legacy_rows,
        )
        if min(*byte_limits, *count_limits) < 1:
            raise ValueError("training export limits must be positive")
        if self.max_record_bytes > self.shard_bytes:
            raise ValueError("training export record limit cannot exceed a shard")


@dataclass(frozen=True)
class TrainingDatasetQuality:
    event_count: int
    episode_count: int
    trajectory_count: int
    retrieval_count: int
    goal_progress_count: int
    sleep_integration_count: int
    preference_count: int
    label_count: int
    duplicate_count: int
    split_counts: dict[str, int]


class _TrainingExportBudget:
    """Track logical work bytes and continuously enforce volume headroom."""

    _REFRESH_INTERVAL = 256

    def __init__(
        self,
        data_root: Path,
        work_root: Path,
        limits: TrainingExportLimits,
        checkpoint: Callable[[], None] | None,
        archive_limit_bytes: int | None = None,
    ):
        self.data_root = data_root
        self.work_root = work_root
        self.limits = limits
        self.max_archive_bytes = min(
            limits.max_archive_bytes,
            limits.max_archive_bytes if archive_limit_bytes is None else archive_limit_bytes,
        )
        self.cancel_checkpoint = checkpoint
        self.scanner = StorageUsageScanner(data_root)
        self._file_work_bytes = 0
        self._index_logical_bytes = 0
        self._index_high_water_bytes = 0
        self.archive_bytes = 0
        self.archive_overhead_reserve = 0
        self.shard_count = 0
        self._external_subject_bytes = 0
        self._operations = 0
        self.refresh()

    @property
    def work_bytes(self) -> int:
        return self._file_work_bytes + max(
            self._index_logical_bytes,
            self._index_high_water_bytes,
        )

    def checkpoint(self, *, force: bool = False) -> None:
        if self.cancel_checkpoint is not None:
            self.cancel_checkpoint()
        self._operations += 1
        self._ensure_capacity(0)
        if force or self._operations % self._REFRESH_INTERVAL == 0:
            self.refresh()

    def reserve_work(self, byte_size: int) -> None:
        if byte_size < 0:
            raise ValueError("training export reservation cannot be negative")
        if self.work_bytes + byte_size > self.limits.max_work_bytes:
            raise TrainingExportLimitError("training export work-byte budget exceeded")
        self._ensure_capacity(byte_size)
        self._file_work_bytes += byte_size
        self.checkpoint()

    def before_index_write(self) -> None:
        # A single B-tree insert can split more than one 4 KiB page. Reserve a
        # conservative fixed amount before SQLite is allowed to grow the file.
        reserve = 65_536
        if self.work_bytes + reserve > self.limits.max_work_bytes:
            raise TrainingExportLimitError("training export work-byte budget exceeded")
        self._ensure_capacity(reserve)
        self.checkpoint()

    def account_index_entry(self, path: Path, *, inserted: bool) -> None:
        if inserted:
            self._index_logical_bytes += 128
        self.observe_index(path)

    def observe_index(self, path: Path) -> None:
        actual = 0
        for candidate in path.parent.glob(f"{path.name}*"):
            try:
                if not candidate.is_symlink() and candidate.is_file():
                    actual += candidate.stat().st_size
            except OSError:
                continue
        self._index_high_water_bytes = max(self._index_high_water_bytes, actual)
        if self.work_bytes > self.limits.max_work_bytes:
            raise TrainingExportLimitError("training export work-byte budget exceeded")
        self._ensure_capacity(0)

    def reserve_shard(self) -> None:
        if self.shard_count >= self.limits.max_dataset_shards:
            raise TrainingExportLimitError("training export dataset shard bound exceeded")
        self.shard_count += 1

    def before_archive_write(self, input_bytes: int) -> None:
        if input_bytes < 0:
            raise ValueError("training archive reservation cannot be negative")
        if (
            self.archive_bytes + input_bytes + self.archive_overhead_reserve + 65_536
            > self.max_archive_bytes
        ):
            raise TrainingExportLimitError("training export archive-byte budget exceeded")
        self._ensure_capacity(input_bytes + self.archive_overhead_reserve + 65_536)
        self.checkpoint()

    def reserve_archive_overhead(self, byte_size: int) -> None:
        if byte_size < 0:
            raise ValueError("training archive overhead cannot be negative")
        if self.archive_bytes + byte_size > self.max_archive_bytes:
            raise TrainingExportLimitError("training export archive-byte budget exceeded")
        self.archive_overhead_reserve = byte_size
        self._ensure_capacity(byte_size)
        self.checkpoint()

    def update_archive(self, path: Path, *, force: bool = False) -> int:
        byte_size = path.stat().st_size
        if byte_size > self.max_archive_bytes:
            raise TrainingExportLimitError("training export archive-byte budget exceeded")
        self.archive_bytes = byte_size
        self.checkpoint(force=force)
        return byte_size

    def refresh(self) -> None:
        if self.cancel_checkpoint is not None:
            self.cancel_checkpoint()
        usage = self.scanner.scan()
        if usage.subject_bytes > self.limits.subject_quota_bytes:
            raise TrainingExportLimitError("training export subject quota exceeded")
        derived_bytes = self.work_bytes + self.archive_bytes
        self._external_subject_bytes = max(
            self._external_subject_bytes,
            max(0, usage.subject_bytes - derived_bytes),
        )
        self._ensure_capacity(0)

    def _ensure_capacity(self, extra_bytes: int) -> None:
        projected = (
            self._external_subject_bytes + self.work_bytes + self.archive_bytes + extra_bytes
        )
        if projected > self.limits.subject_quota_bytes:
            raise TrainingExportLimitError("training export subject quota exceeded")
        free_bytes = shutil.disk_usage(self.data_root).free
        if free_bytes - extra_bytes < self.limits.minimum_free_bytes:
            raise TrainingExportLimitError("training export minimum free-space reserve reached")


class _JsonlWriter:
    """Write bounded JSONL shards while retaining only per-shard metadata."""

    def __init__(
        self,
        root: Path,
        name: str,
        budget: _TrainingExportBudget,
        limits: TrainingExportLimits,
    ):
        self.root = root
        self.name = name
        self.budget = budget
        self.limits = limits
        self.row_count = 0
        self._shard_index = 0
        self._shard_rows = 0
        self._shard_bytes = 0
        self._digest = hashlib.sha256()
        self._closed = False
        self._files: dict[str, Path] = {}
        self._shards: list[dict[str, int | str]] = []
        self._stream: Any = None
        self._open_next()

    def write(self, value: dict[str, Any]) -> None:
        payload = (canonical_json(value) + "\n").encode("utf-8")
        if len(payload) > self.limits.max_record_bytes:
            raise TrainingExportLimitError("training export JSONL record is too large")
        if self._shard_rows and (
            self._shard_rows >= self.limits.shard_rows
            or self._shard_bytes + len(payload) > self.limits.shard_bytes
        ):
            self._finish_current()
            self._open_next()
        self.budget.reserve_work(len(payload))
        self._stream.write(payload)
        self._digest.update(payload)
        self._shard_bytes += len(payload)
        self._shard_rows += 1
        self.row_count += 1

    def close(self) -> None:
        if self._closed:
            return
        self._finish_current()
        self._closed = True

    def files(self) -> dict[str, Path]:
        if not self._closed:
            raise RuntimeError("training JSONL writer is still open")
        return dict(self._files)

    def shard_manifest(self) -> list[dict[str, int | str]]:
        if not self._closed:
            raise RuntimeError("training JSONL writer is still open")
        return [dict(item) for item in self._shards]

    def _open_next(self) -> None:
        self.budget.reserve_shard()
        self._shard_index += 1
        path = Path(self.name)
        if self._shard_index == 1:
            shard_name = self.name
        else:
            shard_name = f"{path.stem}.part-{self._shard_index:05d}{path.suffix}"
        shard_path = self.root / shard_name
        self._stream = shard_path.open("xb")
        self._files[shard_name] = shard_path
        self._shard_rows = 0
        self._shard_bytes = 0
        self._digest = hashlib.sha256()

    def _finish_current(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.flush()
            os.fsync(self._stream.fileno())
        finally:
            self._stream.close()
        shard_name = next(reversed(self._files))
        self._shards.append(
            {
                "file": shard_name,
                "rows": self._shard_rows,
                "bytes": self._shard_bytes,
                "sha256": self._digest.hexdigest(),
            }
        )
        self._stream = None


class _DiskFingerprintSet:
    """Disk-backed exact deduplication with a fixed SQLite page cache."""

    def __init__(self, path: Path, budget: _TrainingExportBudget):
        self.path = path
        self.budget = budget
        self.budget.before_index_write()
        self.connection = sqlite3.connect(path)
        try:
            self.connection.execute("PRAGMA page_size = 4096")
            self.connection.execute("PRAGMA journal_mode = OFF")
            self.connection.execute("PRAGMA synchronous = OFF")
            self.connection.execute("PRAGMA temp_store = MEMORY")
            self.connection.execute("PRAGMA cache_size = -2048")
            self.connection.execute("PRAGMA mmap_size = 0")
            max_pages = max(2, budget.limits.max_work_bytes // 4_096)
            self.connection.execute(f"PRAGMA max_page_count = {max_pages}")
            self.connection.execute(
                "CREATE TABLE fingerprints(value BLOB PRIMARY KEY) WITHOUT ROWID"
            )
            self.connection.commit()
            self.budget.account_index_entry(path, inserted=False)
            self.pending = 0
        except BaseException:
            self.connection.close()
            path.unlink(missing_ok=True)
            raise

    def add(self, value: bytes) -> bool:
        self.budget.before_index_write()
        try:
            inserted = self.connection.execute(
                "INSERT OR IGNORE INTO fingerprints(value) VALUES (?)", (value,)
            ).rowcount
        except sqlite3.OperationalError as error:
            if "full" in str(error).casefold():
                raise TrainingExportLimitError(
                    "training export deduplication database is full"
                ) from error
            raise
        if inserted != 1:
            self.budget.account_index_entry(self.path, inserted=False)
            return False
        self.budget.account_index_entry(self.path, inserted=True)
        self.pending += 1
        if self.pending >= 512:
            self.connection.commit()
            self.pending = 0
            self.budget.checkpoint(force=True)
        return True

    def close(self) -> None:
        try:
            self.connection.commit()
            self.budget.observe_index(self.path)
        finally:
            self.connection.close()


class TrainingDatasetBuilder:
    """Build deterministic, leakage-resistant learning views from events."""

    def __init__(self, limits: TrainingExportLimits | None = None):
        self.limits = limits or TrainingExportLimits()

    def build(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if len(rows) > self.limits.max_legacy_rows:
            raise TrainingExportLimitError("legacy training builder row bound exceeded")
        input_bytes = 0
        for row in rows:
            input_bytes += len(canonical_json(row).encode("utf-8"))
            if input_bytes > self.limits.max_compatibility_bytes:
                raise TrainingExportLimitError("legacy training builder byte bound exceeded")
        ordered = sorted(rows, key=lambda item: (str(item["occurred_at"]), str(item["event_id"])))
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        duplicates = 0
        for row in ordered:
            fingerprint = hashlib.sha256(
                canonical_json(
                    {
                        "event_type": row["event_type"],
                        "occurred_at": row["occurred_at"],
                        "payload": row["payload"],
                    }
                ).encode()
            ).hexdigest()
            if fingerprint in seen:
                duplicates += 1
                continue
            seen.add(fingerprint)
            unique.append(row)
        episodes = self._episodes(unique)
        trajectories = [self._trajectory(episode) for episode in episodes]
        trajectories = [item for item in trajectories if item["steps"]]
        retrieval = [
            {
                "event_id": row["event_id"],
                "query": self._text(row["payload"], "query", "topic", "content"),
                "result": self._text(row["payload"], "summary", "result", "content"),
                "source": row["source"],
            }
            for row in unique
            if row["event_type"]
            in {"memory_created", "memory_retrieved", "research_completed", "observation_analyzed"}
        ]
        goal_progress = [
            row
            for row in unique
            if "goal" in str(row["event_type"])
            and any(
                token in str(row["event_type"])
                for token in ("progress", "outcome", "revised", "created")
            )
        ]
        sleep = [row for row in unique if "sleep" in str(row["event_type"])]
        preferences = [
            row
            for row in unique
            if "accepted" in str(row["event_type"]) or "rejected" in str(row["event_type"])
        ]
        labels = [
            {
                "event_id": row["event_id"],
                "origin": self._label_origin(row),
                "event_type": row["event_type"],
            }
            for row in unique
            if self._label_origin(row) is not None
        ]
        split_counts = {"train": 0, "validation": 0, "test": 0}
        for episode in episodes:
            split_counts[self._split(str(episode["episode_id"]))] += 1
        quality = TrainingDatasetQuality(
            event_count=len(unique),
            episode_count=len(episodes),
            trajectory_count=len(trajectories),
            retrieval_count=len(retrieval),
            goal_progress_count=len(goal_progress),
            sleep_integration_count=len(sleep),
            preference_count=len(preferences),
            label_count=len(labels),
            duplicate_count=duplicates,
            split_counts=split_counts,
        )
        return {
            "episodes": episodes,
            "trajectories": trajectories,
            "retrieval": retrieval,
            "goal_progress": goal_progress,
            "sleep_integration": sleep,
            "preferences": preferences,
            "labels": labels,
            "quality": quality,
        }

    def _episodes(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        episodes: list[list[dict[str, Any]]] = []
        previous: datetime | None = None
        episode_start: datetime | None = None
        episode_scalar_bytes = 0
        episode_byte_limit = min(self.limits.max_episode_bytes, self.limits.max_record_bytes)
        for row in rows:
            current: datetime | None
            try:
                current = datetime.fromisoformat(str(row["occurred_at"]))
                if current.tzinfo is None or current.utcoffset() is None:
                    raise ValueError("naive timestamp")
                current = current.astimezone(UTC)
            except ValueError:
                current = previous
            event_id = str(row["event_id"])
            event_type = str(row["event_type"])
            item_scalar_bytes = self._json_scalar_bytes(event_id) + self._json_scalar_bytes(
                event_type
            )
            exceeds_byte_limit = bool(episodes and episodes[-1]) and (
                self._episode_record_bytes(
                    anchor_event_id=str(episodes[-1][0]["event_id"]),
                    start_at=str(episodes[-1][0]["occurred_at"]),
                    end_at=str(row["occurred_at"]),
                    event_count=len(episodes[-1]) + 1,
                    scalar_bytes=episode_scalar_bytes + item_scalar_bytes,
                )
                > episode_byte_limit
            )
            if (
                previous is None
                or current is None
                or (current - previous).total_seconds() > 1_800
                or len(episodes[-1]) >= self.limits.max_episode_events
                or exceeds_byte_limit
                or (
                    episode_start is not None
                    and (current - episode_start).total_seconds()
                    > self.limits.max_episode_span_seconds
                )
            ):
                episodes.append([])
                episode_start = current
                episode_scalar_bytes = 0
            projected_size = self._episode_record_bytes(
                anchor_event_id=event_id if not episodes[-1] else str(episodes[-1][0]["event_id"]),
                start_at=(
                    str(row["occurred_at"])
                    if not episodes[-1]
                    else str(episodes[-1][0]["occurred_at"])
                ),
                end_at=str(row["occurred_at"]),
                event_count=len(episodes[-1]) + 1,
                scalar_bytes=episode_scalar_bytes + item_scalar_bytes,
            )
            if projected_size > episode_byte_limit:
                raise TrainingExportLimitError("training export episode byte bound exceeded")
            episodes[-1].append(row)
            episode_scalar_bytes += item_scalar_bytes
            previous = current
        result: list[dict[str, Any]] = []
        for episode in episodes:
            episode_id = content_hash({"anchor_event_id": episode[0]["event_id"]})[:24]
            result.append(
                {
                    "episode_id": episode_id,
                    "event_ids": [row["event_id"] for row in episode],
                    "start_at": episode[0]["occurred_at"],
                    "end_at": episode[-1]["occurred_at"],
                    "split": TrainingDatasetBuilder._split(episode_id),
                    "event_types": [row["event_type"] for row in episode],
                }
            )
        return result

    @staticmethod
    def _json_scalar_bytes(value: str) -> int:
        return len(canonical_json(value).encode("utf-8"))

    @classmethod
    def _episode_record_bytes(
        cls,
        *,
        anchor_event_id: str,
        start_at: str,
        end_at: str,
        event_count: int,
        scalar_bytes: int,
    ) -> int:
        episode_id = content_hash({"anchor_event_id": anchor_event_id})[:24]
        shell = {
            "episode_id": episode_id,
            "event_ids": [],
            "start_at": start_at,
            "end_at": end_at,
            "split": cls._split(episode_id),
            "event_types": [],
        }
        separators = 2 * max(0, event_count - 1)
        return len(canonical_json(shell).encode("utf-8")) + scalar_bytes + separators + 1

    @staticmethod
    def _trajectory(episode: dict[str, Any]) -> dict[str, Any]:
        return {
            "episode_id": episode["episode_id"],
            "split": episode["split"],
            "steps": [
                {"event_id": event_id, "event_type": event_type}
                for event_id, event_type in zip(
                    episode["event_ids"], episode["event_types"], strict=True
                )
            ],
        }

    @staticmethod
    def _text(payload: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value[:20_000]
        return ""

    @staticmethod
    def _split(identifier: str) -> str:
        bucket = int(hashlib.sha256(identifier.encode()).hexdigest()[:8], 16) % 100
        return "train" if bucket < 80 else ("validation" if bucket < 90 else "test")

    @staticmethod
    def _label_origin(row: dict[str, Any]) -> str | None:
        source = str(row["source"]).casefold()
        event_type = str(row["event_type"]).casefold()
        if "supervisor" in source or "rejected" in event_type:
            return "supervisor_judgment"
        if "model" in source or "cognition" in source:
            return "model_self_assessment"
        if event_type in {"interaction_received", "human_message"}:
            return "human_feedback"
        if "outcome" in event_type or "observation" in event_type or "prediction" in event_type:
            return "later_fact"
        return None


class TrainingDatasetExporter:
    """Exports only policy-eligible event metadata and payloads.

    The event ledger remains authoritative; an export is a derived artifact
    and can be regenerated with a later redaction policy.
    """

    def __init__(
        self,
        database: Database,
        *,
        workspace_root: Path | str | None = None,
        data_root: Path | str | None = None,
        work_root: Path | str | None = None,
        limits: TrainingExportLimits | None = None,
    ):
        self.database = database
        self.training = TrainingStore(database)
        self.events = EventStore(database, max_archive_cache_segments=1)
        self.limits = limits or TrainingExportLimits()
        self.data_root = (
            database.path.parent if data_root is None else Path(data_root).expanduser().resolve()
        )
        if database.path != self.data_root / database.path.name:
            raise ValueError("training export data root must contain the subject database")
        configured_exports_root = self.data_root / "exports"
        configured_exports_root.mkdir(parents=True, exist_ok=True)
        if self._is_link_or_reparse(configured_exports_root):
            raise ValueError("training export managed root cannot be a symlink or reparse point")
        self.exports_root = configured_exports_root.resolve()
        if self.exports_root.parent != self.data_root:
            raise ValueError("training export managed root must stay on the data volume")
        configured_work_root = (
            self.exports_root / "work" if work_root is None else Path(work_root).expanduser()
        )
        resolved_work_root = configured_work_root.resolve()
        if (
            resolved_work_root != self.exports_root
            and self.exports_root not in resolved_work_root.parents
        ):
            raise ValueError("training export work root must stay under the managed export root")
        configured_work_root.mkdir(parents=True, exist_ok=True)
        if self._is_link_or_reparse(configured_work_root):
            raise ValueError("training export work root cannot be a symlink or reparse point")
        self.work_root = configured_work_root.resolve()
        if self.work_root != self.exports_root and self.exports_root not in self.work_root.parents:
            raise ValueError("training export work root must stay under the managed export root")
        exports_metadata = self.exports_root.lstat()
        self._exports_root_identity = (
            int(exports_metadata.st_dev),
            int(exports_metadata.st_ino),
        )
        work_metadata = self.work_root.lstat()
        self._work_root_identity = (int(work_metadata.st_dev), int(work_metadata.st_ino))
        self._recovery_started = False
        configured_workspace = None if workspace_root is None else Path(workspace_root).expanduser()
        self.workspace_root = (
            None if configured_workspace is None else configured_workspace.resolve()
        )
        self._workspace_root_is_link = False
        if configured_workspace is not None:
            try:
                configured_workspace.lstat()
            except FileNotFoundError:
                pass
            except OSError:
                self._workspace_root_is_link = True
            else:
                self._workspace_root_is_link = self._is_link_or_reparse(configured_workspace)
        self.max_rows = self.limits.max_legacy_rows
        self.max_in_memory_bytes = self.limits.max_compatibility_bytes

    def start_after_ownership(self) -> int:
        """Remove crash-orphaned work directories after process-lock acquisition."""
        if self._recovery_started:
            return 0
        self._validate_work_root()
        removed = 0
        for candidate in self.work_root.iterdir():
            name = candidate.name
            owned_name = (
                name.startswith(".training-export-") or name.startswith(".training-bytes-")
            ) and name.endswith(".work")
            if not owned_name:
                continue
            if self._is_link_or_reparse(candidate):
                candidate.unlink(missing_ok=True)
            elif candidate.is_dir() and candidate.resolve().parent == self.work_root:
                shutil.rmtree(candidate)
            elif candidate.is_file():
                candidate.unlink(missing_ok=True)
            else:
                raise RuntimeError("training export orphan path is not safely removable")
            removed += 1
        removed += self._remove_orphaned_pending_archives()
        self._recovery_started = True
        return removed

    def _remove_orphaned_pending_archives(self) -> int:
        exports_root = self.exports_root
        if not exports_root.is_dir() or self._is_link_or_reparse(exports_root):
            return 0
        removed = 0
        for current, directories, filenames in os.walk(
            exports_root,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            directories[:] = [
                name for name in directories if not self._is_link_or_reparse(current_path / name)
            ]
            for filename in filenames:
                if _PENDING_ARCHIVE_NAME.fullmatch(filename) is None:
                    continue
                candidate = current_path / filename
                try:
                    metadata = candidate.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(metadata.st_mode) or self._is_link_or_reparse(candidate):
                    candidate.unlink(missing_ok=True)
                    removed += 1
        return removed

    def export(self, subject_id: str, *, actor: str) -> TrainingExportArtifact:
        """Compatibility API with an explicit bounded in-memory result."""
        validate_subject_id(subject_id)
        self._validate_work_root()
        with tempfile.TemporaryDirectory(
            prefix=".training-bytes-",
            suffix=".work",
            dir=self.work_root,
        ) as directory:
            target = Path(directory) / "training.zip"
            try:
                artifact = self.export_to_path(
                    subject_id,
                    actor=actor,
                    target=target,
                    _archive_limit_bytes=self.limits.max_compatibility_bytes,
                )
            except TrainingExportLimitError as error:
                if "archive-byte budget exceeded" not in str(error):
                    raise
                raise TrainingExportLimitError(
                    "training export is too large for the compatibility byte API"
                ) from error
            if target.stat().st_size > self.limits.max_compatibility_bytes:
                raise TrainingExportLimitError(
                    "training export is too large for the compatibility byte API"
                )
            with target.open("rb") as stream:
                content = stream.read(self.limits.max_compatibility_bytes + 1)
            if len(content) > self.limits.max_compatibility_bytes:
                raise TrainingExportLimitError(
                    "training export is too large for the compatibility byte API"
                )
        return TrainingExportArtifact(
            filename=artifact.filename,
            content=content,
            sha256=artifact.sha256,
            row_count=artifact.row_count,
            export_id=artifact.export_id,
        )

    def export_to_path(
        self,
        subject_id: str,
        *,
        actor: str,
        target: Path | str,
        control: ExportControl | None = None,
        _archive_limit_bytes: int | None = None,
    ) -> TrainingExportArtifact:
        """Stream records and derived views to disk before publishing the ZIP."""
        validate_subject_id(subject_id)
        if _archive_limit_bytes is not None and _archive_limit_bytes < 1:
            raise ValueError("training export archive override must be positive")
        path = Path(control.target) if control is not None else Path(target).resolve()
        if control is not None:
            control.validate_target(path)
        export_root = self.exports_root
        if export_root not in path.parents:
            raise ValueError("training export target must stay under the managed export root")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_work_root()
        export_id = new_id("training-export")
        created_at = utc_now()
        timestamp = created_at.replace(":", "").replace("-", "").replace("+00:00", "Z")
        filename = f"noyra-training-{subject_id}-{timestamp}.zip"
        staged: Path | None = None
        try:
            with tempfile.TemporaryDirectory(
                prefix=".training-export-",
                suffix=".work",
                dir=self.work_root,
            ) as directory:
                work_root = Path(directory)
                budget = _TrainingExportBudget(
                    self.data_root,
                    work_root,
                    self.limits,
                    None if control is None else control.checkpoint,
                    archive_limit_bytes=_archive_limit_bytes,
                )
                budget.checkpoint(force=True)

                def storage_checkpoint() -> None:
                    budget.checkpoint(force=True)

                self.training.backfill_all(subject_id, checkpoint=storage_checkpoint)
                self.training.reclassify_all(subject_id, checkpoint=storage_checkpoint)
                policy = self.training.policy(subject_id)
                if not policy.export_enabled:
                    raise PermissionError("training export is disabled by policy")
                # Keep the live WAL snapshot only for the short backup operation;
                # all row iteration below uses the isolated image while files are
                # streamed and compressed.
                snapshot = self.database.read_snapshot(
                    checkpoint=lambda: budget.checkpoint(force=True)
                )
                with snapshot as snapshot_connection:
                    budget.checkpoint(force=True)
                    file_paths, metadata, row_count, quality, datasets = self._stream_files(
                        subject_id,
                        policy,
                        work_root,
                        snapshot_connection=snapshot_connection,
                        budget=budget,
                    )
                    manifest = self._stream_manifest(
                        policy,
                        export_id,
                        created_at,
                        subject_id,
                        row_count,
                        quality,
                        metadata,
                        datasets,
                        max_archive_bytes=budget.max_archive_bytes,
                    )
                    manifest_path = work_root / "manifest.json"
                    manifest_payload = json.dumps(
                        manifest,
                        ensure_ascii=False,
                        indent=2,
                    ).encode("utf-8")
                    budget.reserve_work(len(manifest_payload))
                    manifest_path.write_bytes(manifest_payload)
                    file_paths["manifest.json"] = manifest_path
                    manifest_hash = hashlib.sha256(manifest_payload).hexdigest()
                    staged, digest, size = self._stage_zip_from_paths(
                        path,
                        file_paths,
                        checkpoint=budget.checkpoint,
                        budget=budget,
                    )
            self._publish_export(
                subject_id,
                actor,
                export_id,
                created_at,
                policy,
                manifest_hash,
                row_count,
                size,
                digest,
                staged,
                path,
                filename=filename,
                control=control,
            )
            staged = None
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
        return TrainingExportArtifact(
            filename=filename,
            content=b"",
            sha256=digest,
            row_count=row_count,
            export_id=export_id,
        )

    def _stream_files(
        self,
        subject_id: str,
        policy: Any,
        work_root: Path,
        *,
        snapshot_connection: Any | None = None,
        budget: _TrainingExportBudget,
    ) -> tuple[
        dict[str, Path],
        dict[str, dict[str, int | str]],
        int,
        TrainingDatasetQuality,
        dict[str, list[dict[str, int | str]]],
    ]:
        names = (
            "events.jsonl",
            "episodes.jsonl",
            "trajectories.jsonl",
            "retrieval.jsonl",
            "goal_progress.jsonl",
            "sleep_integration.jsonl",
            "preferences.jsonl",
            "labels.jsonl",
        )
        writers: dict[str, _JsonlWriter] = {}
        model_writer: _JsonlWriter | None = None
        dedup_path = work_root / ".fingerprints.sqlite3"
        dedup: _DiskFingerprintSet | None = None
        row_count = 0
        unique_count = 0
        duplicates = 0
        event_fields: set[str] = set()
        split_counts = {"train": 0, "validation": 0, "test": 0}
        episode_ids: list[str] = []
        episode_types: list[str] = []
        episode_start: str | None = None
        episode_end: str | None = None
        previous_time: datetime | None = None
        episode_anchor_time: datetime | None = None
        episode_scalar_bytes = 0
        episode_byte_limit = min(self.limits.max_episode_bytes, self.limits.max_record_bytes)

        def flush_episode() -> None:
            nonlocal episode_ids, episode_types, episode_start, episode_end
            nonlocal episode_anchor_time, episode_scalar_bytes
            if not episode_ids or episode_start is None or episode_end is None:
                return
            episode_id = content_hash({"anchor_event_id": episode_ids[0]})[:24]
            split = TrainingDatasetBuilder._split(episode_id)
            episode = {
                "episode_id": episode_id,
                "event_ids": episode_ids,
                "start_at": episode_start,
                "end_at": episode_end,
                "split": split,
                "event_types": episode_types,
            }
            writers["episodes.jsonl"].write(episode)
            writers["trajectories.jsonl"].write(TrainingDatasetBuilder._trajectory(episode))
            split_counts[split] += 1
            episode_ids = []
            episode_types = []
            episode_start = None
            episode_end = None
            episode_anchor_time = None
            episode_scalar_bytes = 0

        active_error: BaseException | None = None
        try:
            for name in names:
                writers[name] = _JsonlWriter(work_root, name, budget, self.limits)
            if policy.include_model_io:
                model_writer = _JsonlWriter(
                    work_root,
                    "model_io.jsonl",
                    budget,
                    self.limits,
                )
            dedup = _DiskFingerprintSet(dedup_path, budget)
            for item in self._iter_eligible_items(
                subject_id,
                policy.policy_version,
                connection=snapshot_connection,
                checkpoint=budget.checkpoint,
            ):
                budget.checkpoint()
                writers["events.jsonl"].write(item)
                row_count += 1
                event_fields.update(item)
                fingerprint = hashlib.sha256(
                    canonical_json(
                        {
                            "event_type": item["event_type"],
                            "occurred_at": item["occurred_at"],
                            "payload": item["payload"],
                        }
                    ).encode("utf-8")
                ).digest()
                if not dedup.add(fingerprint):
                    duplicates += 1
                    continue
                unique_count += 1
                parsed_time = self._event_datetime(item["occurred_at"])
                current_time = parsed_time if parsed_time is not None else previous_time
                event_id = str(item["event_id"])
                event_type = str(item["event_type"])
                item_scalar_bytes = TrainingDatasetBuilder._json_scalar_bytes(
                    event_id
                ) + TrainingDatasetBuilder._json_scalar_bytes(event_type)
                exceeds_byte_limit = bool(episode_ids) and (
                    TrainingDatasetBuilder._episode_record_bytes(
                        anchor_event_id=episode_ids[0],
                        start_at=str(episode_start),
                        end_at=str(item["occurred_at"]),
                        event_count=len(episode_ids) + 1,
                        scalar_bytes=episode_scalar_bytes + item_scalar_bytes,
                    )
                    > episode_byte_limit
                )
                if (
                    previous_time is None
                    or current_time is None
                    or (current_time - previous_time).total_seconds() > 1_800
                    or len(episode_ids) >= self.limits.max_episode_events
                    or exceeds_byte_limit
                    or (
                        episode_anchor_time is not None
                        and (current_time - episode_anchor_time).total_seconds()
                        > self.limits.max_episode_span_seconds
                    )
                ):
                    flush_episode()
                if not episode_ids:
                    episode_start = str(item["occurred_at"])
                    episode_anchor_time = current_time
                episode_end = str(item["occurred_at"])
                projected_size = TrainingDatasetBuilder._episode_record_bytes(
                    anchor_event_id=event_id if not episode_ids else episode_ids[0],
                    start_at=str(episode_start),
                    end_at=episode_end,
                    event_count=len(episode_ids) + 1,
                    scalar_bytes=episode_scalar_bytes + item_scalar_bytes,
                )
                if projected_size > episode_byte_limit:
                    raise TrainingExportLimitError("training export episode byte bound exceeded")
                episode_ids.append(event_id)
                episode_types.append(event_type)
                episode_scalar_bytes += item_scalar_bytes
                previous_time = current_time

                if event_type in {
                    "memory_created",
                    "memory_retrieved",
                    "research_completed",
                    "observation_analyzed",
                }:
                    writers["retrieval.jsonl"].write(
                        {
                            "event_id": item["event_id"],
                            "query": TrainingDatasetBuilder._text(
                                item["payload"], "query", "topic", "content"
                            ),
                            "result": TrainingDatasetBuilder._text(
                                item["payload"], "summary", "result", "content"
                            ),
                            "source": item["source"],
                        }
                    )
                if "goal" in event_type and any(
                    token in event_type for token in ("progress", "outcome", "revised", "created")
                ):
                    writers["goal_progress.jsonl"].write(item)
                if "sleep" in event_type:
                    writers["sleep_integration.jsonl"].write(item)
                if "accepted" in event_type or "rejected" in event_type:
                    writers["preferences.jsonl"].write(item)
                origin = TrainingDatasetBuilder._label_origin(item)
                if origin is not None:
                    writers["labels.jsonl"].write(
                        {
                            "event_id": item["event_id"],
                            "origin": origin,
                            "event_type": event_type,
                        }
                    )
            flush_episode()

            if model_writer is not None:
                for item in self._iter_model_io(
                    subject_id,
                    connection=snapshot_connection,
                    checkpoint=budget.checkpoint,
                ):
                    budget.checkpoint()
                    model_writer.write(item)
        except BaseException as error:
            active_error = error
            raise
        finally:
            close_errors: list[BaseException] = []
            if dedup is not None:
                try:
                    dedup.close()
                except BaseException as error:
                    close_errors.append(error)
            for writer in writers.values():
                try:
                    writer.close()
                except BaseException as error:
                    close_errors.append(error)
            if model_writer is not None:
                try:
                    model_writer.close()
                except BaseException as error:
                    close_errors.append(error)
            if close_errors and active_error is None:
                raise close_errors[0]
        dedup_path.unlink(missing_ok=True)
        budget.checkpoint(force=True)

        quality = TrainingDatasetQuality(
            event_count=unique_count,
            episode_count=writers["episodes.jsonl"].row_count,
            trajectory_count=writers["trajectories.jsonl"].row_count,
            retrieval_count=writers["retrieval.jsonl"].row_count,
            goal_progress_count=writers["goal_progress.jsonl"].row_count,
            sleep_integration_count=writers["sleep_integration.jsonl"].row_count,
            preference_count=writers["preferences.jsonl"].row_count,
            label_count=writers["labels.jsonl"].row_count,
            duplicate_count=duplicates,
            split_counts=split_counts,
        )
        schema = {
            "format": "noyra-training-dataset-v3",
            "event_fields": sorted(event_fields),
            "payload_policy": (
                "eligible events only; credentials and credential-like values redacted"
            ),
            "derived_views": [
                "episodes",
                "trajectories",
                "retrieval",
                "goal_progress",
                "sleep_integration",
                "preferences",
                "labels",
            ],
            "sharding": {
                "manifest_key": "datasets",
                "first_part_compatibility_name": True,
                "max_rows_per_part": self.limits.shard_rows,
                "max_bytes_per_part": self.limits.shard_bytes,
            },
        }
        static_payloads = {
            "schema.json": json.dumps(schema, ensure_ascii=False, indent=2).encode("utf-8"),
            "quality.json": json.dumps(quality.__dict__, ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
            "dataset_card.md": (
                b"# Noyra training dataset\n\n"
                b"This derived package contains policy-eligible runtime events. "
                b"The subject ledger remains the source of truth.\n"
            ),
        }
        static_paths: dict[str, Path] = {}
        for name, payload in static_payloads.items():
            budget.reserve_work(len(payload))
            static_path = work_root / name
            static_path.write_bytes(payload)
            static_paths[name] = static_path

        file_paths: dict[str, Path] = {}
        datasets: dict[str, list[dict[str, int | str]]] = {}
        for name, writer in writers.items():
            file_paths.update(writer.files())
            datasets[name] = writer.shard_manifest()
        file_paths.update(
            {
                "schema.json": static_paths["schema.json"],
                "quality.json": static_paths["quality.json"],
                "dataset_card.md": static_paths["dataset_card.md"],
            }
        )
        if model_writer is not None:
            file_paths.update(model_writer.files())
            datasets["model_io.jsonl"] = model_writer.shard_manifest()
        workspace_paths = (
            self._workspace_paths(
                subject_id,
                work_root / "workspace",
                checkpoint=budget.checkpoint,
                budget=budget,
            )
            if policy.include_workspace
            else {}
        )
        file_paths.update(workspace_paths)
        metadata = {
            name: self._file_metadata(path, checkpoint=budget.checkpoint)
            for name, path in file_paths.items()
        }
        return file_paths, metadata, row_count, quality, datasets

    def _stream_manifest(
        self,
        policy: Any,
        export_id: str,
        created_at: str,
        subject_id: str,
        row_count: int,
        quality: TrainingDatasetQuality,
        metadata: dict[str, dict[str, int | str]],
        datasets: dict[str, list[dict[str, int | str]]],
        *,
        max_archive_bytes: int,
    ) -> dict[str, Any]:
        return {
            "format": "noyra-training-dataset-v3",
            "export_id": export_id,
            "subject_id": subject_id,
            "created_at": created_at,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "consent_version": policy.policy_version,
            "row_count": row_count,
            "files": metadata,
            "datasets": datasets,
            "workspace_included": policy.include_workspace,
            "private_psychology_included": policy.include_private_psychology,
            "conversations_included": policy.include_conversations,
            "model_io_included": policy.include_model_io,
            "external_actions_included": policy.include_external_actions,
            "quality": quality.__dict__,
            "bounds": {
                "deduplication": "sqlite-disk-backed-exact",
                "max_episode_events": self.limits.max_episode_events,
                "max_episode_span_seconds": self.limits.max_episode_span_seconds,
                "max_episode_bytes": min(
                    self.limits.max_episode_bytes,
                    self.limits.max_record_bytes,
                ),
                "max_archive_segment_bytes": self.limits.max_archive_segment_bytes,
                "max_jsonl_record_bytes": self.limits.max_record_bytes,
                "max_shard_rows": self.limits.shard_rows,
                "max_shard_bytes": self.limits.shard_bytes,
                "max_dataset_shards": self.limits.max_dataset_shards,
                "max_work_bytes": self.limits.max_work_bytes,
                "max_archive_bytes": max_archive_bytes,
                "subject_quota_bytes": self.limits.subject_quota_bytes,
                "minimum_free_bytes": self.limits.minimum_free_bytes,
                "max_workspace_files": self.limits.max_workspace_files,
                "max_workspace_bytes": self.limits.max_workspace_bytes,
                "max_compatibility_bytes": self.limits.max_compatibility_bytes,
            },
            "workspace_path_policy": self._workspace_path_policy(subject_id),
            "workspace_files": {
                name: details for name, details in metadata.items() if name.startswith("workspace/")
            },
        }

    def _iter_eligible_items(
        self,
        subject_id: str,
        consent_version: int,
        *,
        connection: Any | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        if connection is None:
            with self.database.read_transaction() as read_connection:
                yield from self._iter_eligible_items(
                    subject_id,
                    consent_version,
                    connection=read_connection,
                    checkpoint=checkpoint,
                )
            return
        with self._bounded_query(
            connection,
            _ELIGIBLE_ITEMS_SQL,
            (
                self.limits.max_record_bytes,
                subject_id,
                subject_id,
                consent_version,
            ),
            checkpoint=checkpoint,
        ) as records:
            for row in records:
                if row["payload_archive_key"] is None and row["payload_json"] is None:
                    raise TrainingExportLimitError(
                        "training export event payload exceeds the stored-byte bound"
                    )
                try:
                    payload = self.events.payload_from_row(
                        row,
                        connection=connection,
                        max_archive_bytes=self.limits.max_archive_segment_bytes,
                    )
                except PayloadLimitError as error:
                    raise TrainingExportLimitError(
                        "training export archive segment exceeds the decompression bound"
                    ) from error
                yield {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "source": row["source"],
                    "occurred_at": row["occurred_at"],
                    "observed_at": row["observed_at"],
                    "causal_parent_ids": json.loads(row["causal_parent_ids_json"]),
                    "payload": self._sanitize_payload(payload),
                    "source_hash": row["source_hash"],
                    "privacy_level": row["privacy_level"],
                    "consent_version": int(row["consent_version"]),
                }

    def _iter_model_io(
        self,
        subject_id: str,
        *,
        connection: Any | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        if connection is None:
            with self.database.read_transaction() as read_connection:
                yield from self._iter_model_io(
                    subject_id,
                    connection=read_connection,
                    checkpoint=checkpoint,
                )
            return
        with self._bounded_query(
            connection,
            _MODEL_IO_SQL,
            (self.limits.max_record_bytes, self.limits.max_record_bytes, subject_id),
            checkpoint=checkpoint,
        ) as rows:
            for call in rows:
                if call["request_json"] is None or (
                    call["response_stored_bytes"] is not None and call["response_json"] is None
                ):
                    raise TrainingExportLimitError(
                        "training export model payload exceeds the stored-byte bound"
                    )
                try:
                    request_json = decompress_text(
                        call["request_json"],
                        max_bytes=self.limits.max_record_bytes,
                    )
                    response_json = decompress_text(
                        call["response_json"],
                        max_bytes=self.limits.max_record_bytes,
                    )
                except PayloadLimitError as error:
                    raise TrainingExportLimitError(
                        "training export model payload exceeds the decompression bound"
                    ) from error
                request = json.loads(request_json) if request_json else {}
                response = json.loads(response_json) if response_json is not None else None
                yield {
                    "call_id": call["call_id"],
                    "provider": call["provider"],
                    "model": call["model"],
                    "purpose": call["purpose"],
                    "request_hash": call["request_hash"],
                    "request": redact_payload(request),
                    "response": redact_payload(response),
                    "response_hash": call["response_hash"],
                    "status": call["status"],
                    "resource_pool": call["resource_pool"],
                    "resource_group_id": call["resource_group_id"],
                    "capture_policy_version": call["capture_policy_version"],
                    "created_at": call["created_at"],
                    "completed_at": call["completed_at"],
                }

    @staticmethod
    @contextmanager
    def _bounded_query(
        connection: Any,
        sql: str,
        parameters: tuple[Any, ...],
        *,
        checkpoint: Callable[[], None] | None,
    ) -> Iterator[Any]:
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

    @staticmethod
    def _event_datetime(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            return parsed.astimezone(UTC)
        except ValueError:
            return None

    def _workspace_paths(
        self,
        subject_id: str,
        target_root: Path,
        *,
        checkpoint: Callable[[], None] | None = None,
        budget: _TrainingExportBudget,
    ) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for name, payload in self._iter_workspace_payloads(subject_id, checkpoint=checkpoint):
            if checkpoint is not None:
                checkpoint()
            relative = Path(name.removeprefix("workspace/"))
            target = target_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            budget.reserve_work(len(payload))
            with target.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            result[name] = target
        return result

    def _iter_workspace_payloads(
        self,
        subject_id: str,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> Iterator[tuple[str, bytes]]:
        root = self._subject_workspace_root(subject_id)
        if root is None:
            return
        allowed = {
            ".py",
            ".md",
            ".txt",
            ".json",
            ".html",
            ".css",
            ".js",
            ".toml",
            ".yaml",
            ".yml",
        }
        blocked_parts = {".git", "secrets", "node_modules", ".venv", "__pycache__"}
        total = 0
        file_count = 0
        root_identity = self._file_identity(root)
        if root_identity is None:
            raise RuntimeError("subject workspace changed during export")
        for path in self._workspace_candidates(root, blocked_parts):
            if checkpoint is not None:
                checkpoint()
            relative = path.relative_to(root)
            if path.suffix.casefold() not in allowed:
                continue
            if any(part.casefold() in blocked_parts for part in relative.parts):
                continue
            payload = self._read_workspace_payload(
                path,
                root,
                root_identity,
                checkpoint=checkpoint,
            )
            if payload is None:
                continue
            if file_count >= self.limits.max_workspace_files:
                raise TrainingExportLimitError("training export workspace file bound exceeded")
            if total + len(payload) > self.limits.max_workspace_bytes:
                raise TrainingExportLimitError("training export workspace byte bound exceeded")
            total += len(payload)
            file_count += 1
            yield "workspace/" + relative.as_posix(), payload

    @classmethod
    def _workspace_candidates(cls, root: Path, blocked_parts: set[str]) -> Iterator[Path]:
        for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                (
                    name
                    for name in directories
                    if name.casefold() not in blocked_parts
                    and not cls._is_link_or_reparse(current_path / name)
                ),
                key=str.casefold,
            )
            for filename in sorted(filenames, key=str.casefold):
                path = current_path / filename
                if cls._is_link_or_reparse(path):
                    continue
                yield path

    @classmethod
    def _read_workspace_payload(
        cls,
        path: Path,
        root: Path,
        root_identity: tuple[int, int, int, int],
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> bytes | None:
        try:
            relative = path.relative_to(root)
            if cls._path_contains_link(root, relative):
                return None
            resolved = path.resolve()
            if root not in resolved.parents:
                return None
            before = cls._file_identity(path)
            if before is None or before[2] > 2_000_000:
                return None
            payload = cls._read_workspace_bytes(
                root,
                relative,
                path,
                before,
                checkpoint=checkpoint,
            )
            if payload is None or len(payload) > 2_000_000:
                return None
            if cls._is_link_or_reparse(path) or path.resolve() != resolved:
                return None
            if cls._file_identity(path) != before:
                return None
            current_root_identity = cls._file_identity(root)
            if current_root_identity is None or current_root_identity[:2] != root_identity[:2]:
                return None
            text = payload.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        cleaned = cls._sanitize_payload({"content": text})["content"]
        if not isinstance(cleaned, str):
            return None
        return cleaned.encode("utf-8")

    @classmethod
    def _read_workspace_bytes(
        cls,
        root: Path,
        relative: Path,
        path: Path,
        expected_identity: tuple[int, int, int, int],
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> bytes | None:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        flags |= int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        if os.name == "posix" and hasattr(os, "O_NOFOLLOW"):
            directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
            root_fd = os.open(root, directory_flags)
            open_fds = [root_fd]
            current_fd = root_fd
        else:
            root_fd = -1
            open_fds = []
            current_fd = -1
        try:
            if current_fd >= 0:
                parts = relative.parts
                for part in parts[:-1]:
                    current_fd = os.open(part, directory_flags, dir_fd=current_fd)
                    open_fds.append(current_fd)
                file_fd = os.open(parts[-1], flags, dir_fd=current_fd)
            else:
                file_fd = os.open(path, flags)
            try:
                opened = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or cls._stat_identity(opened) != expected_identity
                ):
                    return None
                chunks: list[bytes] = []
                remaining = 2_000_001
                while remaining > 0:
                    if checkpoint is not None:
                        checkpoint()
                    chunk = os.read(file_fd, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if cls._stat_identity(os.fstat(file_fd)) != expected_identity:
                    return None
                return b"".join(chunks)
            finally:
                os.close(file_fd)
        finally:
            for descriptor in reversed(open_fds):
                os.close(descriptor)

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, int, int, int] | None:
        try:
            metadata = path.lstat()
        except OSError:
            return None
        return TrainingDatasetExporter._stat_identity(metadata)

    @staticmethod
    def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            return True
        if stat.S_ISLNK(metadata.st_mode):
            return True
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)

    @classmethod
    def _path_contains_link(cls, root: Path, relative: Path) -> bool:
        current = root
        for part in relative.parts:
            current = current / part
            if cls._is_link_or_reparse(current):
                return True
        return False

    def _subject_workspace_root(self, subject_id: str) -> Path | None:
        if self.workspace_root is None or self._workspace_root_is_link:
            if self._workspace_root_is_link:
                raise ValueError("workspace root cannot be a symbolic link or reparse point")
            return None
        identities = IdentityStore(self.database)
        storage_key = identities.storage_key(subject_id)
        try:
            return SubjectStorageDirectory.locate(
                self.workspace_root,
                storage_key,
                legacy_subject_id=subject_id,
                legacy_migration_allowed=identities.legacy_storage_path_is_unambiguous(subject_id),
            )
        except ValueError as error:
            raise ValueError("subject workspace root cannot be safely located") from error

    def _validate_work_root(self) -> None:
        try:
            exports_metadata = self.exports_root.lstat()
            metadata = self.work_root.lstat()
        except OSError as error:
            raise RuntimeError("training export work root cannot be inspected") from error
        if (
            not stat.S_ISDIR(exports_metadata.st_mode)
            or self._is_link_or_reparse(self.exports_root)
            or self.exports_root.resolve() != self.exports_root
            or (int(exports_metadata.st_dev), int(exports_metadata.st_ino))
            != self._exports_root_identity
            or not stat.S_ISDIR(metadata.st_mode)
            or self._is_link_or_reparse(self.work_root)
            or self.work_root.resolve() != self.work_root
            or (int(metadata.st_dev), int(metadata.st_ino)) != self._work_root_identity
            or (
                self.work_root != self.exports_root
                and self.exports_root not in self.work_root.parents
            )
        ):
            raise RuntimeError(
                "training export work root changed or escaped the managed export root"
            )

    def _workspace_path_policy(self, subject_id: str) -> dict[str, Any]:
        storage_key = IdentityStore(self.database).storage_key(subject_id)
        return {
            "subject_id": subject_id,
            "subject_storage_key": storage_key,
            "source_root": "workspace/<subject_storage_key>",
            "archive_root": "workspace/",
            "follow_symlinks": False,
            "reject_reparse_points": True,
            "rename_policy": "identity-and-containment-recheck",
        }

    @staticmethod
    def _file_metadata(
        path: Path, *, checkpoint: Callable[[], None] | None = None
    ) -> dict[str, int | str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if checkpoint is not None:
                    checkpoint()
                digest.update(chunk)
                size += len(chunk)
        return {"bytes": size, "sha256": digest.hexdigest()}

    @staticmethod
    def _stage_zip_from_paths(
        path: Path,
        files: dict[str, Path],
        *,
        checkpoint: Callable[[], None] | None = None,
        budget: _TrainingExportBudget,
    ) -> tuple[Path, str, int]:
        if path.is_symlink():
            raise ValueError("export target cannot be a symlink")
        staged = path.with_name(f".{path.name}.{new_id('pending')}.pending")
        staged.unlink(missing_ok=True)
        try:
            archive_overhead = 4_096 + sum(256 + 2 * len(name.encode("utf-8")) for name in files)
            budget.reserve_archive_overhead(archive_overhead)
            with staged.open("xb") as stream:
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name in sorted(files):
                        if checkpoint is not None:
                            checkpoint()
                        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o600 << 16
                        with archive.open(info, "w") as target, files[name].open("rb") as source:
                            while chunk := source.read(1024 * 1024):
                                if checkpoint is not None:
                                    checkpoint()
                                budget.before_archive_write(len(chunk))
                                target.write(chunk)
                                budget.update_archive(staged)
                stream.flush()
                os.fsync(stream.fileno())
            budget.update_archive(staged, force=True)
            metadata = (
                TrainingDatasetExporter._file_metadata(staged)
                if checkpoint is None
                else TrainingDatasetExporter._file_metadata(staged, checkpoint=checkpoint)
            )
            digest = str(metadata["sha256"])
            size = staged.stat().st_size
            if size > budget.max_archive_bytes:
                raise TrainingExportLimitError("training export archive-byte budget exceeded")
            return staged, digest, size
        except Exception:
            staged.unlink(missing_ok=True)
            raise

    def _collect_data(
        self, subject_id: str
    ) -> tuple[Any, str, str, list[dict[str, Any]], list[dict[str, Any]]]:
        policy = self.training.policy(subject_id)
        self.training.backfill_all(subject_id)
        if not policy.export_enabled:
            raise PermissionError("training export is disabled by policy")
        export_id = new_id("training-export")
        created_at = utc_now()
        rows: list[dict[str, Any]] = []
        model_io_rows: list[dict[str, Any]] = []
        estimated_bytes = 0
        # This compatibility collector is retained for callers that still
        # build the in-memory representation.  It must use the same isolated
        # image as the streamed path so a large legacy read cannot pin WAL.
        with self.database.read_snapshot() as connection:
            records = connection.execute(
                _ELIGIBLE_ITEMS_SQL,
                (
                    self.limits.max_record_bytes,
                    subject_id,
                    subject_id,
                    policy.policy_version,
                ),
            )
            for row in records:
                if len(rows) >= self.max_rows:
                    raise RuntimeError("training export exceeds the bounded row limit")
                if row["payload_archive_key"] is None and row["payload_json"] is None:
                    raise TrainingExportLimitError(
                        "training export event payload exceeds the stored-byte bound"
                    )
                try:
                    payload = self.events.payload_from_row(
                        row,
                        connection=connection,
                        max_archive_bytes=self.limits.max_archive_segment_bytes,
                    )
                except PayloadLimitError as error:
                    raise TrainingExportLimitError(
                        "training export archive segment exceeds the decompression bound"
                    ) from error
                item = {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "source": row["source"],
                    "occurred_at": row["occurred_at"],
                    "observed_at": row["observed_at"],
                    "causal_parent_ids": json.loads(row["causal_parent_ids_json"]),
                    "payload": self._sanitize_payload(payload),
                    "source_hash": row["source_hash"],
                    "privacy_level": row["privacy_level"],
                    "consent_version": int(row["consent_version"]),
                }
                estimated_bytes += len(canonical_json(item).encode("utf-8"))
                if estimated_bytes > self.max_in_memory_bytes:
                    raise RuntimeError("training export exceeds the bounded memory limit")
                rows.append(item)
            if policy.include_model_io:
                model_calls = connection.execute(
                    _MODEL_IO_SQL,
                    (self.limits.max_record_bytes, self.limits.max_record_bytes, subject_id),
                )
                for call in model_calls:
                    if call["request_json"] is None or (
                        call["response_stored_bytes"] is not None and call["response_json"] is None
                    ):
                        raise TrainingExportLimitError(
                            "training export model payload exceeds the stored-byte bound"
                        )
                    try:
                        request_json = decompress_text(
                            call["request_json"],
                            max_bytes=self.limits.max_record_bytes,
                        )
                        response_json = decompress_text(
                            call["response_json"],
                            max_bytes=self.limits.max_record_bytes,
                        )
                    except PayloadLimitError as error:
                        raise TrainingExportLimitError(
                            "training export model payload exceeds the decompression bound"
                        ) from error
                    request = json.loads(request_json) if request_json else {}
                    response = json.loads(response_json) if response_json is not None else None
                    item = {
                        "call_id": call["call_id"],
                        "provider": call["provider"],
                        "model": call["model"],
                        "purpose": call["purpose"],
                        "request_hash": call["request_hash"],
                        "request": redact_payload(request),
                        "response": redact_payload(response),
                        "response_hash": call["response_hash"],
                        "status": call["status"],
                        "resource_pool": call["resource_pool"],
                        "resource_group_id": call["resource_group_id"],
                        "created_at": call["created_at"],
                        "completed_at": call["completed_at"],
                    }
                    estimated_bytes += len(canonical_json(item).encode("utf-8"))
                    if estimated_bytes > self.max_in_memory_bytes:
                        raise RuntimeError("training export exceeds the bounded memory limit")
                    model_io_rows.append(item)
        return policy, export_id, created_at, rows, model_io_rows

    def _publish_export(
        self,
        subject_id: str,
        actor: str,
        export_id: str,
        created_at: str,
        policy: Any,
        manifest_hash: str,
        row_count: int,
        byte_size: int,
        digest: str,
        staged: Path,
        target: Path,
        *,
        filename: str,
        control: ExportControl | None = None,
    ) -> None:
        aborted = False
        published = False
        try:
            publication = (
                control.publication() if control is not None else self.database.transaction()
            )
            with publication as connection:
                current = connection.execute(
                    "SELECT * FROM training_policies WHERE subject_id = ?", (subject_id,)
                ).fetchone()
                consent_fields = TrainingStore._POLICY_FIELDS
                lease_matches = current is not None and all(
                    bool(current[field]) == bool(getattr(policy, field)) for field in consent_fields
                )
                version_matches = current is not None and int(current["policy_version"]) == int(
                    policy.policy_version
                )
                if not lease_matches or not version_matches or not bool(current["export_enabled"]):
                    disposition = "destroyed"
                    try:
                        staged.unlink(missing_ok=True)
                    except OSError:
                        disposition = "cleanup_failed"
                    connection.execute(
                        """INSERT INTO audit_records(
                           audit_id, subject_id, action, actor, payload_json, occurred_at
                        ) VALUES (?, ?, 'training_export_aborted', ?, ?, ?)""",
                        (
                            new_id("audit"),
                            subject_id,
                            actor,
                            canonical_json(
                                {
                                    "export_id": export_id,
                                    "reason": "training_policy_changed_before_publication",
                                    "lease_policy_version": int(policy.policy_version),
                                    "current_policy_version": (
                                        None if current is None else int(current["policy_version"])
                                    ),
                                    "staged_disposition": disposition,
                                }
                            ),
                            utc_now(),
                        ),
                    )
                    aborted = True
                else:
                    if control is not None:
                        control.validate_target(target)
                    elif target.is_symlink():
                        raise ValueError("export target cannot be a symlink")
                    staged.replace(target)
                    published = True
                    connection.execute(
                        """INSERT INTO training_exports(
                           export_id, subject_id, format, manifest_hash, row_count, byte_size,
                           consent_version, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            export_id,
                            subject_id,
                            "jsonl-zip",
                            manifest_hash,
                            row_count,
                            byte_size,
                            policy.policy_version,
                            created_at,
                        ),
                    )
                    if control is not None:
                        control.complete(
                            connection,
                            artifact_path=target,
                            filename=filename,
                            sha256=digest,
                            byte_size=byte_size,
                        )
                    connection.execute(
                        """INSERT INTO audit_records(
                           audit_id, subject_id, action, actor, payload_json, occurred_at
                        ) VALUES (?, ?, 'training_exported', ?, ?, ?)""",
                        (
                            new_id("audit"),
                            subject_id,
                            actor,
                            canonical_json(
                                {
                                    "export_id": export_id,
                                    "archive_sha256": digest,
                                    "row_count": row_count,
                                    "consent_version": int(policy.policy_version),
                                }
                            ),
                            created_at,
                        ),
                    )
        except Exception:
            if published:
                target.unlink(missing_ok=True)
            raise
        if aborted:
            raise TrainingConsentChangedError(
                "training consent changed before artifact publication"
            )

    @staticmethod
    def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
        cleaned = redact_payload(payload)
        if not isinstance(cleaned, dict):
            raise TypeError("sanitized payload is not an object")
        return cleaned
