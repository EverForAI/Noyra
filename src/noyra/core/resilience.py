from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive import StorageUsageScanner
from .database import Database
from .errors import (
    ArchiveKeyUnavailableError,
    ArchiveUnavailableError,
    IntegrityError,
    PayloadLimitError,
)
from .event_archive import (
    EventArchiveVerification,
    EventArchiveVerificationLimits,
    EventPayloadArchive,
)
from .events import EventStore
from .snapshots import SnapshotStore
from .storage import StorageLayout
from .types import canonical_json, content_hash, strict_json_loads, utc_now


@dataclass(frozen=True)
class ResilienceReport:
    checks: dict[str, str]
    p0: tuple[str, ...]
    p1: tuple[str, ...]
    subject_bytes: int
    event_count: int


class LongRunResilience:
    """Bounded watchdog and audit harness for unattended deployments."""

    def __init__(
        self,
        database: Database,
        subject_id: str,
        data_root: Path | str,
        *,
        archive_verification_limits: EventArchiveVerificationLimits | None = None,
        archive_verification_checkpoint: Callable[[], None] | None = None,
    ):
        self.database = database
        self.subject_id = subject_id
        self.layout = StorageLayout.create(data_root)
        self.events = EventStore(database)
        self.snapshots = SnapshotStore(database)
        self.archive_verification_limits = (
            archive_verification_limits or EventArchiveVerificationLimits()
        )
        self.archive_verification_checkpoint = archive_verification_checkpoint

    async def run_with_recovery(
        self,
        operation: Callable[[], Awaitable[str]],
        *,
        timeout_seconds: float = 300,
        max_failures: int = 3,
    ) -> str:
        failures = 0
        while True:
            try:
                return await asyncio.wait_for(operation(), timeout=timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures += 1
                self.events.append(
                    self.subject_id,
                    "runtime_recovery_failure",
                    "resilience_watchdog",
                    {"attempt": failures, "error_type": type(error).__name__},
                    privacy_level="private",
                )
                if failures >= max(1, max_failures):
                    raise
                await asyncio.sleep(min(60, 2**failures))

    def audit(self) -> ResilienceReport:
        checks: dict[str, str] = {}
        p0: list[str] = []
        p1: list[str] = []
        try:
            with self.database.read_transaction() as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                checks["sqlite_integrity"] = str(integrity)
                if integrity != "ok":
                    p0.append("sqlite_integrity")
                event_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM events WHERE subject_id = ?", (self.subject_id,)
                    ).fetchone()[0]
                )
                foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
                checks["foreign_keys"] = "ok" if not foreign_keys else "failed"
                if foreign_keys:
                    p0.append("foreign_keys")
                identity = connection.execute(
                    "SELECT genesis_hash FROM subject_identity WHERE subject_id = ?",
                    (self.subject_id,),
                ).fetchone()
                checks["identity_continuity"] = "ok" if identity is not None else "failed"
                if identity is None:
                    p0.append("identity_continuity")
                self._audit_event_integrity(connection, checks, p0, p1, event_count)
                dead_archives = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM archive_transfer_queue WHERE subject_id = ? "
                        "AND status = 'dead'",
                        (self.subject_id,),
                    ).fetchone()[0]
                )
                checks["archive_dead_letter"] = str(dead_archives)
                if dead_archives:
                    p1.append("archive_dead_letter")
        except Exception as error:
            if checks.get("sqlite_integrity") == "ok":
                checks["audit_runtime"] = f"failed:{type(error).__name__}"
                p0.append("audit_runtime")
            else:
                checks["sqlite_integrity"] = "failed"
                p0.append("sqlite_integrity")
            event_count = locals().get("event_count", 0)
        try:
            latest = self.snapshots.latest(self.subject_id)
            checks["snapshot_hash"] = "ok"
            if latest.subject_id != self.subject_id:
                p0.append("snapshot_owner")
        except Exception:
            checks["snapshot_hash"] = "missing"
            p1.append("snapshot_hash")
        try:
            archived = self.snapshots.verify_archives(self.subject_id)
            checks["snapshot_archives"] = str(archived)
        except Exception:
            checks["snapshot_archives"] = "failed"
            p0.append("snapshot_archives")
        for name, check in self._domain_checks().items():
            try:
                result = check()
                checks[f"domain:{name}"] = json.dumps(result, sort_keys=True)
            except Exception as error:
                checks[f"domain:{name}"] = f"failed:{type(error).__name__}"
                p0.append(f"domain:{name}")
        anomalies = self.events.causal_anomalies(self.subject_id, limit=32)
        anomaly_sample = [item for index, item in enumerate(anomalies) if index < 8]
        checks["event_causal_anomalies"] = json.dumps(
            {"count": len(anomalies), "sample": anomaly_sample}, sort_keys=True
        )
        if anomalies:
            p1.append("event_causal_anomalies")
        usage = self._subject_bytes()
        checks["storage_boundary"] = "ok" if usage >= 0 else "failed"
        if usage < 0:
            p1.append("storage_boundary")
        return ResilienceReport(checks, tuple(p0), tuple(p1), usage, event_count)

    def _audit_event_integrity(
        self,
        connection: Any,
        checks: dict[str, str],
        p0: list[str],
        p1: list[str],
        event_count: int,
    ) -> None:
        hot_failures: list[str] = []
        segment_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM event_payload_segments WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchone()[0]
        )
        archived_events = int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE subject_id = ? "
                "AND payload_archive_key IS NOT NULL",
                (self.subject_id,),
            ).fetchone()[0]
        )
        verification = EventArchiveVerification(
            status="ok",
            segments=segment_count,
            events=archived_events,
            verified_segments=0,
            verified_events=0,
        )
        archive_state = "ok"
        hot_bytes_row = connection.execute(
            "SELECT COALESCE(MAX(length(CAST(payload_json AS BLOB))), 0), "
            "COALESCE(SUM(length(CAST(payload_json AS BLOB))), 0) FROM events "
            "WHERE subject_id = ? AND payload_archive_key IS NULL",
            (self.subject_id,),
        ).fetchone()
        max_hot_payload_bytes = int(hot_bytes_row[0])
        total_hot_payload_bytes = int(hot_bytes_row[1])
        if (
            event_count > self.archive_verification_limits.max_events
            or max_hot_payload_bytes > self.archive_verification_limits.max_hot_event_payload_bytes
            or total_hot_payload_bytes > self.archive_verification_limits.max_hot_event_bytes
        ):
            archive_state = "degraded:verification_limit"
            verification = EventArchiveVerification(
                status="degraded",
                segments=segment_count,
                events=archived_events,
                verified_segments=0,
                verified_events=0,
                reason="verification_limit",
            )
            p1.append("event_archive:verification_limit")
        else:
            for row in connection.execute(
                "SELECT event_id, payload_json, payload_hash, payload_archive_key, "
                "payload_archived_at FROM events WHERE subject_id = ? ORDER BY event_id",
                (self.subject_id,),
            ):
                event_id = str(row["event_id"])
                if row["payload_archive_key"] is not None:
                    if row["payload_json"] != "{}" or row["payload_archived_at"] is None:
                        hot_failures.append(event_id)
                    continue
                if row["payload_archived_at"] is not None:
                    hot_failures.append(event_id)
                    continue
                if not isinstance(row["payload_json"], str):
                    hot_failures.append(event_id)
                    continue
                try:
                    payload = strict_json_loads(row["payload_json"])
                except (RecursionError, TypeError, ValueError):
                    hot_failures.append(event_id)
                    continue
                try:
                    valid_hash = (
                        isinstance(payload, dict) and content_hash(payload) == row["payload_hash"]
                    )
                except (RecursionError, TypeError, ValueError):
                    valid_hash = False
                if not valid_hash:
                    hot_failures.append(event_id)

        if archive_state == "ok" and (archived_events or segment_count):
            try:
                verification = EventPayloadArchive.verify_subject_integrity(
                    self.database,
                    self.layout.subject / "cold",
                    self.subject_id,
                    connection=connection,
                    limits=self.archive_verification_limits,
                    checkpoint=self.archive_verification_checkpoint,
                )
            except ArchiveKeyUnavailableError:
                archive_state = "degraded:key_unavailable"
                verification = EventArchiveVerification(
                    status="degraded",
                    segments=segment_count,
                    events=archived_events,
                    verified_segments=0,
                    verified_events=0,
                    reason="key_unavailable",
                )
                p1.append("event_archive:key_unavailable")
            except ArchiveUnavailableError:
                archive_state = "degraded:unavailable"
                verification = EventArchiveVerification(
                    status="degraded",
                    segments=segment_count,
                    events=archived_events,
                    verified_segments=0,
                    verified_events=0,
                    reason="temporarily_unavailable",
                )
                p1.append("event_archive:unavailable")
            except PayloadLimitError:
                archive_state = "degraded:verification_limit"
                verification = EventArchiveVerification(
                    status="degraded",
                    segments=segment_count,
                    events=archived_events,
                    verified_segments=0,
                    verified_events=0,
                    reason="verification_limit",
                )
                p1.append("event_archive:verification_limit")
            except IntegrityError as error:
                archive_state = "failed"
                verification = EventArchiveVerification(
                    status="corrupt",
                    segments=segment_count,
                    events=archived_events,
                    verified_segments=0,
                    verified_events=0,
                    reason=type(error).__name__,
                )
                p0.append("event_archive:corrupt")

        for event_id in hot_failures:
            p0.append(f"event_hash:{event_id}")
        if hot_failures or archive_state == "failed":
            checks["event_hashes"] = "failed"
        elif archive_state.startswith("degraded:"):
            checks["event_hashes"] = archive_state
        else:
            checks["event_hashes"] = "ok"
        checks["event_archive_integrity"] = canonical_json(verification.__dict__)

    def _domain_checks(self) -> dict[str, Callable[[], object]]:
        # Imports are local to keep the core package free of initialization cycles.
        from noyra.capability import CapabilityIntegrity
        from noyra.core.actions import ActionLedger
        from noyra.interaction import InteractionIntegrity, TransportStore
        from noyra.learning import OutcomeEvaluator
        from noyra.mind import EntityStore, MemoryBlockStore, MindEngine
        from noyra.sleep import SleepIntegrity
        from noyra.world import WorldIntegrity

        return {
            "event_chain": lambda: self.events.verify_chain(self.subject_id),
            "actions": lambda: ActionLedger(self.database).verify_integrity(self.subject_id),
            "mind": lambda: MindEngine(self.database).verify_integrity(self.subject_id),
            "memory_blocks": lambda: MemoryBlockStore(self.database).verify_integrity(
                self.subject_id
            ),
            "entities": lambda: EntityStore(self.database).verify_integrity(self.subject_id),
            "sleep": lambda: SleepIntegrity(self.database).verify(self.subject_id),
            "interaction": lambda: InteractionIntegrity(self.database).verify(self.subject_id),
            "transport": lambda: TransportStore(
                self.database,
                self.layout.root / "secrets" / "transports",
                repair_on_init=False,
            ).verify_integrity(self.subject_id),
            "capability": lambda: CapabilityIntegrity(self.database).verify(self.subject_id),
            "world": lambda: WorldIntegrity(self.database).verify(self.subject_id),
            "outcomes": lambda: OutcomeEvaluator(self.database, self.subject_id).verify_integrity(),
        }

    def write_report(self, report: ResilienceReport) -> Path:
        payload = {
            "created_at": utc_now(),
            "subject_id": self.subject_id,
            "checks": report.checks,
            "p0": report.p0,
            "p1": report.p1,
            "subject_bytes": report.subject_bytes,
            "event_count": report.event_count,
        }
        target = self.layout.exports / "resilience-report.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        target.with_suffix(".sha256").write_text(content_hash(payload), encoding="ascii")
        return target

    def _subject_bytes(self) -> int:
        return StorageUsageScanner(self.layout.root).scan().subject_bytes
