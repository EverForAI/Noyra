from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from noyra.model.ledger import ModelLedger
from noyra.world.observation_archive import ObservationContentArchive

from .admission import current_commit_scope
from .archive import ArchiveKeyring, StorageQuota
from .database import Database
from .event_archive import EventPayloadArchive
from .events import EventStore
from .export_jobs import ARTIFACT_PRUNING_ERROR, retire_completed_export_artifact
from .snapshots import SnapshotStore
from .storage import (
    StorageLayout,
    StorageUsage,
    StorageUsageHistory,
    StorageUsageScanner,
    StorageUsageTrend,
)

SNAPSHOT_COMPACTION_ROW_BUDGET = 2_048
SNAPSHOT_COMPACTION_BYTE_BUDGET = 64_000_000
EXPORT_PRUNE_CANDIDATE_BUDGET = 1_000


@dataclass(frozen=True)
class StorageMaintenanceResult:
    usage: StorageUsage
    warnings: tuple[str, ...]
    over_quota: tuple[str, ...]
    actions: tuple[str, ...]
    cognition_allowed: bool
    write_amplification_allowed: bool
    trend: StorageUsageTrend


class StorageLifecycleManager:
    """Apply a deterministic local-first degradation order under disk pressure."""

    def __init__(
        self,
        database: Database,
        subject_id: str,
        layout: StorageLayout,
        quota: StorageQuota,
        *,
        minimum_free_bytes: int = 500_000_000,
        event_payload_retention_days: int = 90,
        research_usage_retention_days: int = 365,
        initialize_archives: bool = True,
    ):
        self.database = database
        self.subject_id = subject_id
        self.layout = layout
        self.quota = quota
        self.minimum_free_bytes = max(10_000_000, minimum_free_bytes)
        self.event_payload_retention_days = max(1, event_payload_retention_days)
        # Kept as a compatibility setting; append-only usage evidence is no longer pruned.
        self.research_usage_retention_days = max(1, research_usage_retention_days)
        self.scanner = StorageUsageScanner(layout.root, database, subject_id)
        self.usage_history = StorageUsageHistory(database, subject_id)
        self.events = EventStore(database)
        self.models = ModelLedger(database)
        self.snapshots = SnapshotStore(database)
        self.event_archive: EventPayloadArchive | None = None
        self.observation_archive: ObservationContentArchive | None = None
        if initialize_archives and ArchiveKeyring.configured():
            self.event_archive = EventPayloadArchive(
                database, layout.subject / "cold", subject_id=subject_id
            )
            self.observation_archive = ObservationContentArchive(
                database, layout.subject / "cold", subject_id=subject_id
            )

    def maintain(
        self,
        *,
        defer_pressure_decision: bool = False,
        checkpoint: Callable[[], None] | None = None,
    ) -> StorageMaintenanceResult:
        """Run one bounded maintenance tick with an emergency admission gate.

        The first filesystem observation is deliberately a read-only preflight.
        If free space is below the safety floor, or this subject is already over
        quota, operations that temporarily increase storage (archive staging,
        model compression, and snapshot compaction) are not admitted.  Only
        cache/export deletion is attempted before a fresh pressure assessment.
        """
        self._checkpoint(checkpoint)
        usage = self.scanner.scan()
        warnings = usage.warnings(self.quota)
        over_quota = usage.over_quota(self.quota)
        pressure_blocked = self._pressure_blocked(usage, over_quota)
        actions: list[str] = []

        # Deletion is the only safe emergency operation.  Do it before any
        # archive/compression stage and re-check admission between deletions.
        if warnings or pressure_blocked:
            self._checkpoint(checkpoint)
            removed = self._clear_cache(checkpoint=checkpoint)
            if removed:
                actions.append(f"cache_cleared:{removed}")
        if pressure_blocked or "subject" in warnings:
            self._checkpoint(checkpoint)
            pruned = self._prune_export_artifacts(
                checkpoint=checkpoint,
                force=pressure_blocked,
            )
            if pruned:
                actions.append(f"export_artifacts_pruned:{pruned}")

        self._checkpoint(checkpoint)
        orphaned = self._garbage_collect_archive_orphans(limit=16)
        if orphaned:
            actions.append(f"archive_orphans_removed:{orphaned}")

        if not pressure_blocked:
            # Every write-amplifying stage gets its own read-only preflight.
            # A previous stage can consume the remaining headroom, so checking
            # only once at the beginning would reintroduce the original ENOSPC
            # failure mode.
            if self.event_archive is not None and self._admit_write_stage(checkpoint):
                archived_events = self.event_archive.archive_cold(
                    self.subject_id,
                    older_than_days=self.event_payload_retention_days,
                    limit=1_000,
                )
                if archived_events:
                    actions.append(f"event_payloads_archived:{archived_events}")
            if self.observation_archive is not None and self._admit_write_stage(checkpoint):
                archived_observations = self.observation_archive.archive_cold(
                    self.subject_id,
                    older_than_days=self.event_payload_retention_days,
                    limit=200,
                )
                if archived_observations:
                    actions.append(f"observation_content_archived:{archived_observations}")
            if self._admit_write_stage(checkpoint):
                compressed_models = self.models.compress_cold_payloads(
                    self.subject_id,
                    older_than_days=self.event_payload_retention_days,
                    limit=500,
                )
                if compressed_models:
                    actions.append(f"model_payloads_compressed:{compressed_models}")
            if "subject" in warnings and self._admit_write_stage(checkpoint):
                compacted = self._compact_snapshots(checkpoint)
                if compacted["archived"]:
                    actions.append(f"snapshots_compacted:{compacted['archived']}")
        return self._assess(
            actions,
            record_sample=not defer_pressure_decision,
            emit_event=not defer_pressure_decision,
            checkpoint=checkpoint,
        )

    def reassess(
        self,
        previous: StorageMaintenanceResult,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> StorageMaintenanceResult:
        """Finalize pressure only after optional cloud upload and local archive GC."""
        return self._assess(
            previous.actions,
            record_sample=True,
            emit_event=True,
            checkpoint=checkpoint,
        )

    def reconcile_archives(self, *, limit: int = 16) -> dict[str, int]:
        """Replay bounded staging manifests when storage admission permits writes."""
        result = {
            "event_finalized": 0,
            "event_abandoned": 0,
            "event_orphans_removed": 0,
            "observation_finalized": 0,
            "observation_abandoned": 0,
            "observation_orphans_removed": 0,
        }
        if self.event_archive is None and self.observation_archive is None:
            return result
        if self.event_archive is not None:
            result["event_orphans_removed"] = self.event_archive.gc_orphans(
                self.subject_id,
                limit=limit,
            )
        if self.observation_archive is not None:
            result["observation_orphans_removed"] = self.observation_archive.gc_orphans(
                self.subject_id,
                limit=limit,
            )
        usage = self.scanner.scan()
        if self._pressure_blocked(usage, usage.over_quota(self.quota)):
            return result
        if self.event_archive is not None:
            event = self.event_archive.reconcile_staging(self.subject_id, limit=limit)
            result["event_finalized"] = event["finalized"]
            result["event_abandoned"] = event["abandoned"]
        if self.observation_archive is not None:
            observation = self.observation_archive.reconcile_staging(
                self.subject_id,
                limit=limit,
            )
            result["observation_finalized"] = observation["finalized"]
            result["observation_abandoned"] = observation["abandoned"]
        return result

    def _assess(
        self,
        actions: list[str] | tuple[str, ...],
        *,
        record_sample: bool,
        emit_event: bool,
        checkpoint: Callable[[], None] | None = None,
    ) -> StorageMaintenanceResult:
        self._checkpoint(checkpoint)
        refreshed = self.scanner.scan()
        warnings = refreshed.warnings(self.quota)
        over_quota = refreshed.over_quota(self.quota)
        critical = refreshed.free_bytes < self.minimum_free_bytes or "subject" in over_quota
        database_exhausted = False
        # Telemetry is nonessential during the hard pressure path.  Avoid all
        # append writes here so SQLITE_FULL/IOERR cannot prevent the caller
        # from receiving a fail-closed storage-pressure result.
        try:
            if record_sample and not critical:
                self._checkpoint(checkpoint)
                self.usage_history.record_if_due(
                    refreshed,
                    self.quota,
                    self.minimum_free_bytes,
                )
            self._checkpoint(checkpoint)
            trend = self.usage_history.predict(self.quota, self.minimum_free_bytes)
            if emit_event and not critical and warnings and self._log_due():
                self.events.append(
                    self.subject_id,
                    "storage_maintenance",
                    "storage_lifecycle",
                    {
                        "warnings": list(warnings),
                        "over_quota": list(over_quota),
                        "actions": list(actions),
                        "free_bytes": refreshed.free_bytes,
                        "subject_bytes": refreshed.subject_bytes,
                        "effective_subject_bytes": refreshed.effective_subject_bytes,
                        "database_bytes": refreshed.database_bytes,
                        "database_reclaimable_bytes": refreshed.database_reclaimable_bytes,
                        "wal_bytes": refreshed.wal_bytes,
                        "local_archive_bytes": refreshed.local_archive_bytes,
                        "cloud_staging_bytes": refreshed.cloud_staging_bytes,
                        "exports_bytes": refreshed.exports_bytes,
                        "trend": trend.__dict__,
                        "cognition_allowed": not critical,
                    },
                    privacy_level="private",
                )
        except sqlite3.OperationalError as error:
            if not self._is_storage_exhaustion(error):
                raise
            # Once SQLite reports FULL/IOERR, further writes are unsafe and a
            # normal maintenance exception could leave the service admitting
            # cognition on an unmeasurable disk.  Return a conservative,
            # fail-closed result and defer telemetry until a later tick.
            critical = True
            database_exhausted = True
            warnings = tuple(dict.fromkeys((*warnings, "database")))
            actions = [*actions, "storage_pressure_database_io"]
            trend = self._empty_trend()
        return StorageMaintenanceResult(
            refreshed,
            warnings,
            over_quota,
            tuple(actions),
            not critical,
            not database_exhausted and not self._pressure_blocked(refreshed, over_quota),
            trend,
        )

    def _admit_write_stage(self, checkpoint: Callable[[], None] | None) -> bool:
        """Return whether a write-amplifying stage may start right now."""
        self._checkpoint(checkpoint)
        usage = self.scanner.scan()
        over_quota = usage.over_quota(self.quota)
        admitted = not self._pressure_blocked(usage, over_quota)
        self._checkpoint(checkpoint)
        return admitted

    def _compact_snapshots(self, checkpoint: Callable[[], None] | None) -> dict[str, int]:
        return self.snapshots.compact(
            self.subject_id,
            keep_recent=16,
            checkpoint=checkpoint,
            max_rows=SNAPSHOT_COMPACTION_ROW_BUDGET,
            max_bytes=SNAPSHOT_COMPACTION_BYTE_BUDGET,
        )

    def _garbage_collect_archive_orphans(self, *, limit: int) -> int:
        removed = 0
        try:
            event_gc = getattr(self.event_archive, "gc_orphans", None)
            if callable(event_gc):
                removed += int(event_gc(self.subject_id, limit=limit))
            observation_gc = getattr(self.observation_archive, "gc_orphans", None)
            if callable(observation_gc):
                removed += int(observation_gc(self.subject_id, limit=limit))
        except sqlite3.OperationalError as error:
            if not self._is_storage_exhaustion(error):
                raise
        return removed

    def _pressure_blocked(self, usage: StorageUsage, over_quota: tuple[str, ...]) -> bool:
        return usage.free_bytes < self.minimum_free_bytes or bool(over_quota)

    def _log_due(self) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT occurred_at FROM events WHERE subject_id = ? "
                "AND event_type = 'storage_maintenance' "
                "ORDER BY occurred_at DESC, event_id DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
        if row is None:
            return True
        last = datetime.fromisoformat(str(row["occurred_at"])).astimezone(UTC)
        return datetime.now(UTC) - last >= timedelta(hours=1)

    def _clear_cache(self, *, checkpoint: Callable[[], None] | None = None) -> int:
        removed = 0
        for path, is_directory in self._iter_cache_entries():
            self._checkpoint(checkpoint)
            if is_directory:
                with current_commit_scope(), suppress(OSError):
                    path.rmdir()
                continue
            with current_commit_scope():
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def _iter_cache_entries(self) -> Iterator[tuple[Path, bool]]:
        """Yield cache files and directories in bounded post-order.

        Cache contents are derived data and may be much larger than the
        maintenance tick's working memory.  A post-order traversal lets the
        caller remove files immediately and then retire empty directories,
        without retaining a complete directory listing.
        """
        stack: list[tuple[Path, bool]] = [(self.layout.cache, False)]
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        while stack:
            root, closing = stack.pop()
            if closing:
                if root != self.layout.cache:
                    yield root, True
                continue
            try:
                entries = os.scandir(root)
            except OSError:
                continue
            if root != self.layout.cache:
                stack.append((root, True))
            with entries:
                for entry in entries:
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(metadata.st_mode) or bool(
                        getattr(metadata, "st_file_attributes", 0) & reparse_flag
                    ):
                        continue
                    path = Path(entry.path)
                    if stat.S_ISREG(metadata.st_mode):
                        yield path, False
                    elif stat.S_ISDIR(metadata.st_mode):
                        stack.append((path, False))

    def _prune_export_artifacts(
        self,
        *,
        checkpoint: Callable[[], None] | None = None,
        force: bool = False,
    ) -> int:
        """Remove oldest completed artifacts until subject storage has headroom."""
        target_bytes = int(self.quota.subject_bytes * 0.75)
        removed = 0
        examined = 0
        # Keep a keyset over the stable ordering instead of materializing every
        # completed export.  The pruning intent changes ``error_code`` and can
        # reorder a row, so advance the cursor before attempting the unlink.
        order_cursor: tuple[int, str, str] | None = None
        while examined < EXPORT_PRUNE_CANDIDATE_BUDGET:
            with self.database.connection() as connection:
                query = (
                    "SELECT job_id, artifact_path, error_code, prune_priority, completed_order "
                    "FROM (SELECT job_id, artifact_path, error_code, "
                    "CASE WHEN error_code = ? THEN 0 ELSE 1 END AS prune_priority, "
                    "COALESCE(completed_at, '') AS completed_order FROM export_jobs "
                    "WHERE subject_id = ? AND status = 'completed' "
                    "AND artifact_path IS NOT NULL) candidates"
                )
                parameters: list[object] = [ARTIFACT_PRUNING_ERROR, self.subject_id]
                if order_cursor is not None:
                    query += (
                        " WHERE prune_priority > ? OR "
                        "(prune_priority = ? AND (completed_order > ? OR "
                        "(completed_order = ? AND job_id > ?)))"
                    )
                    parameters.extend(
                        [
                            order_cursor[0],
                            order_cursor[0],
                            order_cursor[1],
                            order_cursor[1],
                            order_cursor[2],
                        ]
                    )
                query += " ORDER BY prune_priority, completed_order, job_id LIMIT 1"
                row = connection.execute(query, tuple(parameters)).fetchone()
            if row is None:
                break
            examined += 1
            order_cursor = (
                int(row["prune_priority"]),
                str(row["completed_order"]),
                str(row["job_id"]),
            )
            self._checkpoint(checkpoint)
            pending_prune = str(row["error_code"] or "") == ARTIFACT_PRUNING_ERROR
            # Always finish a previously committed pruning intent, even when
            # pressure has since cleared; otherwise downloads would remain
            # blocked forever after a crash between unlink and finalization.
            if (
                not pending_prune
                and not force
                and self.scanner.scan().subject_bytes <= target_bytes
            ):
                break
            artifact_path = str(row["artifact_path"])
            configured = Path(artifact_path).expanduser()
            try:
                with current_commit_scope():
                    safe = self.layout.exports.resolve()
                    if not self._safe_export_entry(
                        configured,
                        allow_missing=pending_prune,
                    ):
                        continue
                    path = configured.resolve(strict=False)
                    if safe not in path.parents or path == safe:
                        continue
                    retired = retire_completed_export_artifact(
                        self.database,
                        job_id=str(row["job_id"]),
                        subject_id=self.subject_id,
                        artifact_path=artifact_path,
                        path=path,
                    )
            except OSError:
                continue
            if not retired:
                continue
            removed += 1
            self._checkpoint(checkpoint)
        return removed

    @staticmethod
    def _safe_export_entry(path: Path, *, allow_missing: bool) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            # Missing is recoverable only after the durable pruning intent was
            # committed.  Otherwise preserve the dangling row for integrity
            # diagnostics rather than silently laundering it as maintenance.
            return allow_missing
        except OSError:
            return False
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return (
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and not bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
        )

    @staticmethod
    def _is_storage_exhaustion(error: sqlite3.OperationalError) -> bool:
        code = getattr(error, "sqlite_errorcode", None)
        if isinstance(code, int) and (code & 0xFF) in {
            sqlite3.SQLITE_FULL,
            sqlite3.SQLITE_IOERR,
        }:
            return True
        message = str(error).casefold()
        return (
            "database or disk is full" in message
            or "disk i/o error" in message
            or "sqlite_full" in message
            or "sqlite_ioerr" in message
        )

    @staticmethod
    def _empty_trend() -> StorageUsageTrend:
        return StorageUsageTrend(0, 0.0, 0.0, 0.0, 0.0, None, None, 24 * 30)

    @staticmethod
    def _checkpoint(checkpoint: Callable[[], None] | None) -> None:
        if checkpoint is not None:
            checkpoint()
