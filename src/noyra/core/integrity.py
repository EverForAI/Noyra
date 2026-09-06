from __future__ import annotations

import base64
import heapq
import json
import multiprocessing
import os
import shutil
import sqlite3
import stat
import threading
import time
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .archive import (
    ARCHIVE_KEYRING_FORMAT,
    MAX_ARCHIVE_KEYS,
    ArchiveKeyring,
    ArchiveReplicaLedger,
)
from .database import Database
from .errors import (
    ArchiveKeyUnavailableError,
    ArchiveUnavailableError,
    IntegrityError,
    PayloadLimitError,
)
from .event_archive import EventArchiveVerificationLimits
from .events import EventStore
from .resilience import LongRunResilience
from .secret_cleanup import SecretCleanupQueue
from .snapshots import SnapshotStore
from .storage import StorageLayout, storage_usage_sample_state_hash
from .types import (
    canonical_json,
    content_hash,
    new_id,
    strict_bool,
    strict_int,
    strict_json_loads,
    utc_now,
)

if TYPE_CHECKING:
    from .runtime import SubjectKernel


IntegrityProfile = Literal["startup_light", "periodic_deep", "manual"]
IntegrityPolicyMode = Literal["off", "alert", "pause"]
IntegrityStatus = Literal["ok", "degraded", "corrupt", "incomplete"]
IntegritySeverity = Literal["none", "p1", "p0"]

INTEGRITY_REGISTRY_VERSION = "noyra-integrity-registry/v2"
INTEGRITY_REPORT_FORMAT_VERSION = "noyra-integrity-report/v1"
INTEGRITY_WATCHDOG_STATE_VERSION = "noyra-integrity-watchdog-state/v2"
INTEGRITY_REPORT_RETENTION = 2_016
INTEGRITY_SHARD_RETRY_LIMIT = 2
INTEGRITY_PROCESS_POLL_SECONDS = 0.05

_INTEGRITY_PROFILES = frozenset({"startup_light", "periodic_deep", "manual"})
_INTEGRITY_POLICY_MODES = frozenset({"off", "alert", "pause"})
_INTEGRITY_STATUSES = frozenset({"ok", "degraded", "corrupt", "incomplete"})
_INTEGRITY_SEVERITIES = frozenset({"none", "p1", "p0"})
_INTEGRITY_META_CHECK_IDS = frozenset({"registry.snapshot", "registry.execution"})


class _IntegrityBudgetExceeded(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class IntegrityAuditShutdown(RuntimeError):
    """Cooperative service shutdown, not an integrity finding."""


def _is_sqlite_corruption(error: BaseException) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    corruption_codes = {
        getattr(sqlite3, "SQLITE_CORRUPT", 11),
        getattr(sqlite3, "SQLITE_NOTADB", 26),
    }
    if code in corruption_codes:
        return True
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "database disk image is malformed",
            "file is not a database",
            "database corruption",
            "malformed database schema",
        )
    )


@dataclass(frozen=True)
class IntegrityAuditLimits:
    max_rows_per_check: int = 20_000
    max_bytes_per_check: int = 64_000_000
    max_value_bytes: int = 16_000_000
    max_files_per_check: int = 50_000
    max_detail_bytes: int = 32_000
    sqlite_progress_ops: int = 1_000

    def __post_init__(self) -> None:
        if any(
            value < 1
            for value in (
                self.max_rows_per_check,
                self.max_bytes_per_check,
                self.max_value_bytes,
                self.max_files_per_check,
                self.max_detail_bytes,
                self.sqlite_progress_ops,
            )
        ):
            raise ValueError("integrity audit limits must be positive")
        if self.max_value_bytes > self.max_bytes_per_check:
            raise ValueError("integrity value limit cannot exceed the per-check byte limit")


@dataclass
class _IntegrityBudget:
    limits: IntegrityAuditLimits
    checkpoint: Callable[[], None]
    rows: int = 0
    bytes: int = 0
    files: int = 0
    external_bytes: int = 0

    def consume_row(self, row: Any) -> int:
        self.checkpoint()
        self.rows += 1
        size = self._row_bytes(row)
        if size > self.limits.max_value_bytes:
            raise _IntegrityBudgetExceeded("value_byte_limit")
        self.bytes += size
        return size

    def consume_file(self, size: int) -> None:
        self.consume_file_entry()
        self.consume_bytes(size)

    def consume_file_entry(self) -> None:
        self.checkpoint()
        self.files += 1
        if self.files > self.limits.max_files_per_check:
            raise _IntegrityBudgetExceeded("file_limit")

    def consume_bytes(self, size: int) -> None:
        self.checkpoint()
        consumed = max(0, size)
        self.bytes += consumed
        self.external_bytes += consumed
        if self.external_bytes > self.limits.max_bytes_per_check:
            raise _IntegrityBudgetExceeded("byte_limit")

    @staticmethod
    def _row_bytes(row: Any) -> int:
        if isinstance(row, sqlite3.Row):
            values = tuple(row)
        elif isinstance(row, Mapping):
            values = tuple(row.values())
        elif isinstance(row, (tuple, list)):
            values = tuple(row)
        else:
            values = (row,)
        total = 0
        for value in values:
            if value is None:
                continue
            if isinstance(value, bytes):
                total += len(value)
            elif isinstance(value, str):
                total += len(value.encode("utf-8"))
            elif isinstance(value, (int, float, bool)):
                total += 8
            else:
                total += len(repr(value).encode("utf-8"))
        return total


class _BudgetedCursor:
    def __init__(self, cursor: sqlite3.Cursor, budget: _IntegrityBudget):
        self._cursor = cursor
        self._budget = budget

    def fetchone(self) -> Any | None:
        row = self._cursor.fetchone()
        if row is not None:
            self._budget.consume_row(row)
        return row

    def fetchmany(self, size: int | None = None) -> list[Any]:
        requested = self._cursor.arraysize if size is None else max(0, size)
        rows: list[Any] = []
        materialized_bytes = 0
        while len(rows) < requested:
            row = self._cursor.fetchone()
            if row is None:
                break
            materialized_bytes += self._budget.consume_row(row)
            rows.append(row)
            self._check_materialized(len(rows), materialized_bytes)
        return rows

    def fetchall(self) -> list[Any]:
        rows: list[Any] = []
        materialized_bytes = 0
        while True:
            row = self._cursor.fetchone()
            if row is None:
                return rows
            materialized_bytes += self._budget.consume_row(row)
            rows.append(row)
            self._check_materialized(len(rows), materialized_bytes)

    def __iter__(self) -> Iterator[Any]:
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def _check_materialized(self, rows: int, size: int) -> None:
        if rows > self._budget.limits.max_rows_per_check:
            raise _IntegrityBudgetExceeded("row_limit")
        if size > self._budget.limits.max_bytes_per_check:
            raise _IntegrityBudgetExceeded("byte_limit")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _BudgetedConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        budget: _IntegrityBudget,
        checkpoint: Callable[[], None],
    ):
        self._connection = connection
        self._budget = budget
        self._checkpoint = checkpoint

    def execute(self, sql: str, parameters: Any = ()) -> _BudgetedCursor:
        self._checkpoint()
        return _BudgetedCursor(self._connection.execute(sql, parameters), self._budget)

    def cursor(self) -> _BudgetedCursor:
        self._checkpoint()
        return _BudgetedCursor(self._connection.cursor(), self._budget)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _ReadOnlyDatabase(Database):
    """Existing-database facade backed by an OS-enforced read-only SQLite URI."""

    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.as_uri()}?mode=ro",
            timeout=30,
            check_same_thread=False,
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


class _SnapshotDatabase:
    """Database-compatible facade that never escapes the orchestrator snapshot."""

    def __init__(self, source: Database, connection: _BudgetedConnection):
        self.path = source.path
        self._connection = connection

    @contextmanager
    def connection(self) -> Iterator[_BudgetedConnection]:
        yield self._connection

    @contextmanager
    def read_transaction(self) -> Iterator[_BudgetedConnection]:
        yield self._connection

    @contextmanager
    def read_snapshot(self, **_: Any) -> Iterator[_BudgetedConnection]:
        yield self._connection

    @contextmanager
    def transaction(self) -> Iterator[_BudgetedConnection]:
        raise sqlite3.OperationalError("integrity snapshots are query-only")
        yield self._connection


@dataclass(frozen=True)
class IntegrityFinding:
    check_id: str
    check_version: int
    severity: Literal["p0", "p1"]
    reason_code: str


@dataclass(frozen=True)
class IntegrityCheckOutcome:
    status: IntegrityStatus = "ok"
    severity: IntegritySeverity = "none"
    reason_code: str = "ok"
    details: Mapping[str, Any] = field(default_factory=dict)
    findings: tuple[tuple[Literal["p0", "p1"], str], ...] = ()

    def __post_init__(self) -> None:
        if self.status == "ok" and self.severity != "none":
            raise ValueError("an ok integrity outcome cannot have a finding severity")
        if self.status != "ok" and self.severity == "none":
            raise ValueError("a non-ok integrity outcome requires a finding severity")


IntegrityRunner = Callable[["IntegrityContext"], object]


@dataclass(frozen=True)
class IntegrityCheckSpec:
    check_id: str
    version: int
    domain: str
    profiles: frozenset[IntegrityProfile]
    runner: IntegrityRunner
    tier: Literal["light", "deep"] = "deep"

    def __post_init__(self) -> None:
        if not self.check_id.strip() or not self.domain.strip() or self.version < 1:
            raise ValueError("integrity check identity is invalid")
        if not self.profiles:
            raise ValueError("integrity checks require at least one execution profile")


@dataclass(frozen=True)
class IntegrityCheckResult:
    id: str
    version: int
    domain: str
    tier: str
    status: IntegrityStatus
    severity: IntegritySeverity
    reason_code: str
    duration_ms: int
    rows_examined: int
    bytes_examined: int
    details: Mapping[str, Any]


@dataclass(frozen=True)
class IntegrityContext:
    database: Database
    connection: _BudgetedConnection
    source_database: Database
    subject_id: str
    layout: StorageLayout
    limits: IntegrityAuditLimits
    budget: _IntegrityBudget
    checkpoint: Callable[[], None]


@dataclass(frozen=True)
class IntegrityReport:
    format_version: str
    registry_version: str
    run_id: str
    subject_id: str
    profile: IntegrityProfile
    policy_mode: IntegrityPolicyMode
    started_at: str
    completed_at: str
    status: IntegrityStatus
    snapshot: Mapping[str, Any]
    selection: Mapping[str, Any]
    checks: tuple[IntegrityCheckResult, ...]
    findings: tuple[IntegrityFinding, ...]
    summary: Mapping[str, Any]
    action: Mapping[str, Any]
    deferred_coverage: tuple[Mapping[str, str], ...]

    @property
    def p0(self) -> tuple[str, ...]:
        return tuple(
            f"{finding.check_id}:{finding.reason_code}"
            for finding in self.findings
            if finding.severity == "p0"
        )

    @property
    def p1(self) -> tuple[str, ...]:
        return tuple(
            f"{finding.check_id}:{finding.reason_code}"
            for finding in self.findings
            if finding.severity == "p1"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "registry_version": self.registry_version,
            "run_id": self.run_id,
            "subject_id": self.subject_id,
            "profile": self.profile,
            "policy_mode": self.policy_mode,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "snapshot": dict(self.snapshot),
            "selection": dict(self.selection),
            "checks": [asdict(check) for check in self.checks],
            "findings": [asdict(finding) for finding in self.findings],
            "summary": dict(self.summary),
            "action": dict(self.action),
            "deferred_coverage": [dict(item) for item in self.deferred_coverage],
        }


class IntegrityRegistry:
    version = INTEGRITY_REGISTRY_VERSION
    report_format_version = INTEGRITY_REPORT_FORMAT_VERSION

    def __init__(self, checks: Sequence[IntegrityCheckSpec] | None = None):
        self._is_default_inventory = checks is None
        self.checks = tuple(checks or _default_checks())
        ids = [check.check_id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("integrity registry check ids must be unique")
        self.deferred_coverage: tuple[Mapping[str, str], ...] = (
            {
                "domain": "cognition.project_artifact_semantics",
                "owner": "P1-12",
                "reason": "artifact validators and evidence binding remain a separate Gate C issue",
            },
            {
                "domain": "storage.archive_key_history_cloud_restore",
                "owner": "P1-11",
                "reason": "keyring history and cloud read-through restore are not yet implemented",
            },
            {
                "domain": "knowledge.runtime_application",
                "owner": "P2-09/P3-02",
                "reason": "signed package integrity is checked; cognition integration remains open",
            },
        )

    @classmethod
    def default(cls, *_: Any, **__: Any) -> IntegrityRegistry:
        return cls()

    def profile_checks(self, profile: IntegrityProfile) -> tuple[IntegrityCheckSpec, ...]:
        return tuple(check for check in self.checks if profile in check.profiles)

    def run(
        self,
        database: Database,
        subject_id: str,
        data_root: StorageLayout | Path | str,
        *,
        profile: IntegrityProfile,
        policy_mode: IntegrityPolicyMode,
        deadline_seconds: float,
        limits: IntegrityAuditLimits | None = None,
        check_ids: Sequence[str] | None = None,
        selection: Mapping[str, Any] | None = None,
        checkpoint: Callable[[], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        _progress: Callable[[str, object], None] | None = None,
    ) -> IntegrityReport:
        if deadline_seconds <= 0:
            raise ValueError("integrity audit deadline must be positive")
        audit_limits = limits or IntegrityAuditLimits()
        available = self.profile_checks(profile)
        by_id = {check.check_id: check for check in available}
        if check_ids is None:
            selected = available
        else:
            missing = tuple(check_id for check_id in check_ids if check_id not in by_id)
            if missing:
                raise ValueError(f"integrity check is unavailable for {profile}: {missing[0]}")
            selected = tuple(by_id[check_id] for check_id in check_ids)
        started_at = utc_now()
        started = monotonic()
        deadline = started + deadline_seconds
        layout = (
            data_root if isinstance(data_root, StorageLayout) else StorageLayout.create(data_root)
        )
        results: list[IntegrityCheckResult] = []
        findings: list[IntegrityFinding] = []
        snapshot: dict[str, Any] = {}
        snapshot_interrupted: list[BaseException] = []

        def snapshot_checkpoint() -> None:
            if monotonic() >= deadline:
                raise TimeoutError("integrity audit deadline exceeded")
            if checkpoint is not None:
                checkpoint()

        def snapshot_progress() -> int:
            if snapshot_interrupted:
                return 1
            try:
                snapshot_checkpoint()
            except BaseException as error:
                snapshot_interrupted.append(error)
                return 1
            return 0

        try:
            with database.read_transaction() as raw_connection:
                raw_connection.execute("PRAGMA query_only = ON")
                remaining_ms = max(1, min(30_000, round((deadline - monotonic()) * 1_000)))
                raw_connection.execute(f"PRAGMA busy_timeout = {remaining_ms}")
                # SQLite applies SQLITE_LIMIT_LENGTH while preparing a query and
                # may materialize the schema SQL as part of that preparation.
                # Load trusted schema metadata while the value limit is not yet
                # active, but retain the snapshot progress/deadline guard.
                raw_connection.set_progress_handler(
                    snapshot_progress, audit_limits.sqlite_progress_ops
                )
                try:
                    snapshot_checkpoint()
                    raw_connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type IN ('table', 'index', 'trigger', 'view')"
                    ).fetchall()
                    with _sqlite_length_limit(raw_connection, audit_limits.max_value_bytes):
                        snapshot_checkpoint()
                        snapshot = self._snapshot_marker(raw_connection, subject_id)
                        snapshot_checkpoint()
                        if _progress is not None:
                            _progress("snapshot", dict(snapshot))
                except sqlite3.OperationalError as error:
                    if snapshot_interrupted:
                        raise snapshot_interrupted[0] from error
                    raise
                finally:
                    raw_connection.set_progress_handler(None, 0)
                for spec in selected:
                    if _progress is not None:
                        _progress(
                            "check_started",
                            {
                                "id": spec.check_id,
                                "version": spec.version,
                                "domain": spec.domain,
                                "tier": spec.tier,
                            },
                        )
                    result, check_findings = self._execute_check(
                        spec,
                        database,
                        raw_connection,
                        subject_id,
                        layout,
                        audit_limits,
                        deadline,
                        checkpoint,
                        monotonic,
                    )
                    results.append(result)
                    findings.extend(check_findings)
                    if _progress is not None:
                        _progress("check_result", (result, check_findings))
                    if result.reason_code in {"timeout", "cancelled"}:
                        break
        except IntegrityAuditShutdown:
            raise
        except Exception as error:
            if isinstance(error, IntegrityError):
                status: IntegrityStatus = "corrupt"
                severity: IntegritySeverity = "p0"
                reason_code = "integrity_error"
            elif isinstance(error, TimeoutError):
                status = "incomplete"
                severity = "p1"
                reason_code = "timeout"
            elif isinstance(error, InterruptedError):
                status = "incomplete"
                severity = "p1"
                reason_code = "cancelled"
            elif isinstance(error, sqlite3.DatabaseError) and _is_sqlite_corruption(error):
                status = "corrupt"
                severity = "p0"
                reason_code = "integrity_error"
            else:
                status = "incomplete"
                severity = "p1"
                reason_code = "snapshot_unavailable"
            result = IntegrityCheckResult(
                id="registry.snapshot",
                version=1,
                domain="core",
                tier="light",
                status=status,
                severity=severity,
                reason_code=reason_code,
                duration_ms=max(0, round((monotonic() - started) * 1_000)),
                rows_examined=0,
                bytes_examined=0,
                details={"error_type": type(error).__name__},
            )
            results.append(result)
            findings.append(
                IntegrityFinding(
                    result.id,
                    result.version,
                    cast(Literal["p0", "p1"], severity),
                    result.reason_code,
                )
            )
        completed_at = utc_now()
        status = _report_status(results)
        summary = {
            "checks": len(results),
            "ok": sum(result.status == "ok" for result in results),
            "p0": sum(finding.severity == "p0" for finding in findings),
            "p1": sum(finding.severity == "p1" for finding in findings),
            "rows_examined": sum(result.rows_examined for result in results),
            "bytes_examined": sum(result.bytes_examined for result in results),
        }
        return IntegrityReport(
            format_version=self.report_format_version,
            registry_version=self.version,
            run_id=new_id("integrity-run"),
            subject_id=subject_id,
            profile=profile,
            policy_mode=policy_mode,
            started_at=started_at,
            completed_at=completed_at,
            status=status,
            snapshot=snapshot,
            selection=dict(selection or {"check_ids": [check.check_id for check in selected]}),
            checks=tuple(results),
            findings=tuple(findings),
            summary=summary,
            action={"attempted": False, "result": "pending_policy"},
            deferred_coverage=self.deferred_coverage,
        )

    def run_isolated(
        self,
        database: Database,
        subject_id: str,
        data_root: Path | str,
        *,
        profile: IntegrityProfile,
        policy_mode: IntegrityPolicyMode,
        deadline_seconds: float,
        limits: IntegrityAuditLimits | None = None,
        check_ids: Sequence[str] | None = None,
        selection: Mapping[str, Any] | None = None,
        checkpoint: Callable[[], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> IntegrityReport:
        """Run the built-in registry in a terminable read-only child process."""
        if type(self) is not IntegrityRegistry or not self._is_default_inventory:
            raise TypeError("only the built-in integrity registry can run in isolation")
        if deadline_seconds <= 0:
            raise ValueError("integrity audit deadline must be positive")
        audit_limits = limits or IntegrityAuditLimits()
        available = self.profile_checks(profile)
        by_id = {check.check_id: check for check in available}
        if check_ids is None:
            selected = available
        else:
            missing = tuple(check_id for check_id in check_ids if check_id not in by_id)
            if missing:
                raise ValueError(f"integrity check is unavailable for {profile}: {missing[0]}")
            selected = tuple(by_id[check_id] for check_id in check_ids)
        selection_payload = dict(selection or {"check_ids": [check.check_id for check in selected]})
        started_at = utc_now()
        wall_monotonic = time.monotonic
        started = wall_monotonic()
        deadline = started + deadline_seconds
        process_context = multiprocessing.get_context("spawn")
        receiver, sender = process_context.Pipe(duplex=False)
        process = process_context.Process(
            target=_run_isolated_registry_worker,
            args=(
                sender,
                str(database.path),
                subject_id,
                str(Path(data_root).resolve()),
                profile,
                policy_mode,
                deadline_seconds,
                audit_limits,
                None if check_ids is None else tuple(check_ids),
                selection_payload,
            ),
            name="noyra-integrity-audit",
        )
        snapshot: dict[str, Any] = {}
        completed_results: list[IntegrityCheckResult] = []
        completed_findings: list[IntegrityFinding] = []
        current_check: Mapping[str, Any] | None = None
        current_check_started = started

        def failure_report(reason_code: str, error_type: str) -> IntegrityReport:
            return self._execution_failure_report(
                subject_id,
                profile,
                policy_mode,
                selection_payload,
                started_at,
                started,
                wall_monotonic,
                snapshot=snapshot,
                completed_results=completed_results,
                completed_findings=completed_findings,
                current_check=current_check,
                current_check_started=current_check_started,
                reason_code=reason_code,
                error_type=error_type,
            )

        try:
            try:
                process.start()
            except (OSError, RuntimeError) as error:
                return failure_report("checker_runtime", type(error).__name__)
            finally:
                sender.close()

            while True:
                if checkpoint is not None:
                    checkpoint()
                remaining = deadline - wall_monotonic()
                if remaining <= 0:
                    return failure_report("hard_timeout", "TimeoutError")
                if receiver.poll(min(INTEGRITY_PROCESS_POLL_SECONDS, remaining)):
                    try:
                        kind, payload = receiver.recv()
                    except (EOFError, OSError):
                        kind, payload = "error", "ChildProcessError"
                    if wall_monotonic() >= deadline:
                        return failure_report("hard_timeout", "TimeoutError")
                    if kind == "snapshot" and isinstance(payload, Mapping):
                        snapshot = dict(payload)
                        continue
                    if kind == "check_started" and isinstance(payload, Mapping):
                        current_check = dict(payload)
                        current_check_started = wall_monotonic()
                        continue
                    if kind == "check_result" and (
                        isinstance(payload, tuple)
                        and len(payload) == 2
                        and isinstance(payload[0], IntegrityCheckResult)
                        and isinstance(payload[1], tuple)
                        and all(isinstance(item, IntegrityFinding) for item in payload[1])
                    ):
                        completed_results.append(payload[0])
                        completed_findings.extend(payload[1])
                        current_check = None
                        current_check_started = wall_monotonic()
                        continue
                    if kind == "report" and isinstance(payload, IntegrityReport):
                        if (
                            payload.registry_version == self.version
                            and payload.subject_id == subject_id
                            and payload.profile == profile
                            and payload.policy_mode == policy_mode
                        ):
                            return payload
                        kind, payload = "error", "InvalidIntegrityReport"
                    return failure_report("checker_runtime", str(payload))
                if not process.is_alive():
                    process.join()
                    if receiver.poll():
                        continue
                    return failure_report("checker_runtime", f"ChildProcessExit{process.exitcode}")
        finally:
            receiver.close()
            if process.pid is not None:
                process.join(timeout=0.2)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
                if not process.is_alive():
                    process.close()
            else:
                process.close()

    def _execution_failure_report(
        self,
        subject_id: str,
        profile: IntegrityProfile,
        policy_mode: IntegrityPolicyMode,
        selection: Mapping[str, Any],
        started_at: str,
        started: float,
        monotonic: Callable[[], float],
        *,
        snapshot: Mapping[str, Any],
        completed_results: Sequence[IntegrityCheckResult],
        completed_findings: Sequence[IntegrityFinding],
        current_check: Mapping[str, Any] | None,
        current_check_started: float,
        reason_code: str,
        error_type: str,
    ) -> IntegrityReport:
        now = monotonic()
        if current_check is None:
            check_id = "registry.execution"
            check_version = 1
            domain = "core"
            tier = "light" if profile == "startup_light" else "deep"
            duration_ms = max(0, round((now - started) * 1_000))
        else:
            check_id = str(current_check["id"])
            check_version = int(current_check["version"])
            domain = str(current_check["domain"])
            tier = str(current_check["tier"])
            duration_ms = max(0, round((now - current_check_started) * 1_000))
        result = IntegrityCheckResult(
            id=check_id,
            version=check_version,
            domain=domain,
            tier=tier,
            status="incomplete",
            severity="p1",
            reason_code=reason_code,
            duration_ms=duration_ms,
            rows_examined=0,
            bytes_examined=0,
            details={"error_type": error_type},
        )
        finding = IntegrityFinding(result.id, result.version, "p1", result.reason_code)
        results = (*completed_results, result)
        findings = (*completed_findings, finding)
        return IntegrityReport(
            format_version=self.report_format_version,
            registry_version=self.version,
            run_id=new_id("integrity-run"),
            subject_id=subject_id,
            profile=profile,
            policy_mode=policy_mode,
            started_at=started_at,
            completed_at=utc_now(),
            status=_report_status(results),
            snapshot=dict(snapshot),
            selection=dict(selection),
            checks=results,
            findings=findings,
            summary={
                "checks": len(results),
                "ok": sum(item.status == "ok" for item in results),
                "p0": sum(item.severity == "p0" for item in findings),
                "p1": sum(item.severity == "p1" for item in findings),
                "rows_examined": sum(item.rows_examined for item in results),
                "bytes_examined": sum(item.bytes_examined for item in results),
            },
            action={"attempted": False, "result": "pending_policy"},
            deferred_coverage=self.deferred_coverage,
        )

    def _execute_check(
        self,
        spec: IntegrityCheckSpec,
        source_database: Database,
        raw_connection: sqlite3.Connection,
        subject_id: str,
        layout: StorageLayout,
        limits: IntegrityAuditLimits,
        deadline: float,
        external_checkpoint: Callable[[], None] | None,
        monotonic: Callable[[], float],
    ) -> tuple[IntegrityCheckResult, tuple[IntegrityFinding, ...]]:
        started = monotonic()
        interrupted: list[BaseException] = []

        def checkpoint() -> None:
            if monotonic() >= deadline:
                raise TimeoutError("integrity audit deadline exceeded")
            if external_checkpoint is not None:
                external_checkpoint()

        def progress() -> int:
            if interrupted:
                return 1
            try:
                checkpoint()
            except BaseException as error:
                interrupted.append(error)
                return 1
            return 0

        budget = _IntegrityBudget(limits, checkpoint)
        connection = _BudgetedConnection(raw_connection, budget, checkpoint)
        snapshot_database = cast(Database, _SnapshotDatabase(source_database, connection))
        context = IntegrityContext(
            database=snapshot_database,
            connection=connection,
            source_database=snapshot_database,
            subject_id=subject_id,
            layout=layout,
            limits=limits,
            budget=budget,
            checkpoint=checkpoint,
        )
        remaining_ms = max(1, min(30_000, round((deadline - monotonic()) * 1_000)))
        raw_connection.execute(f"PRAGMA busy_timeout = {remaining_ms}")
        raw_connection.set_progress_handler(progress, limits.sqlite_progress_ops)
        try:
            checkpoint()
            raw_outcome = spec.runner(context)
            checkpoint()
            outcome = (
                raw_outcome
                if isinstance(raw_outcome, IntegrityCheckOutcome)
                else IntegrityCheckOutcome(details=_mapping_detail(raw_outcome))
            )
        except _IntegrityBudgetExceeded as error:
            outcome = IntegrityCheckOutcome("degraded", "p1", error.reason_code)
        except TimeoutError:
            outcome = IntegrityCheckOutcome("incomplete", "p1", "timeout")
        except IntegrityAuditShutdown:
            raise
        except InterruptedError:
            outcome = IntegrityCheckOutcome("incomplete", "p1", "cancelled")
        except (ArchiveKeyUnavailableError, ArchiveUnavailableError):
            outcome = IntegrityCheckOutcome("degraded", "p1", "resource_unavailable")
        except PayloadLimitError:
            outcome = IntegrityCheckOutcome("degraded", "p1", "resource_limit")
        except IntegrityError as error:
            outcome = IntegrityCheckOutcome(
                "corrupt", "p0", "integrity_error", {"error_type": type(error).__name__}
            )
        except sqlite3.DataError as error:
            outcome = IntegrityCheckOutcome(
                "degraded", "p1", "value_byte_limit", {"error_type": type(error).__name__}
            )
        except sqlite3.OperationalError as error:
            if interrupted:
                if isinstance(interrupted[0], IntegrityAuditShutdown):
                    raise interrupted[0] from error
                if isinstance(interrupted[0], TimeoutError):
                    reason = "timeout"
                elif isinstance(interrupted[0], InterruptedError):
                    reason = "cancelled"
                else:
                    reason = "checker_runtime"
                outcome = IntegrityCheckOutcome("incomplete", "p1", reason)
            elif _is_sqlite_corruption(error):
                outcome = IntegrityCheckOutcome(
                    "corrupt", "p0", "integrity_error", {"error_type": type(error).__name__}
                )
            else:
                outcome = IntegrityCheckOutcome(
                    "incomplete", "p1", "checker_runtime", {"error_type": type(error).__name__}
                )
        except sqlite3.DatabaseError as error:
            if _is_sqlite_corruption(error):
                outcome = IntegrityCheckOutcome(
                    "corrupt", "p0", "integrity_error", {"error_type": type(error).__name__}
                )
            else:
                outcome = IntegrityCheckOutcome(
                    "incomplete", "p1", "checker_runtime", {"error_type": type(error).__name__}
                )
        except OSError as error:
            outcome = IntegrityCheckOutcome(
                "degraded", "p1", "resource_unavailable", {"error_type": type(error).__name__}
            )
        except Exception as error:
            outcome = IntegrityCheckOutcome(
                "incomplete", "p1", "checker_runtime", {"error_type": type(error).__name__}
            )
        finally:
            with suppress(sqlite3.Error):
                raw_connection.set_progress_handler(None, 0)
        duration_ms = max(0, round((monotonic() - started) * 1_000))
        details = _bounded_details(outcome.details, limits.max_detail_bytes)
        result = IntegrityCheckResult(
            id=spec.check_id,
            version=spec.version,
            domain=spec.domain,
            tier=spec.tier,
            status=outcome.status,
            severity=outcome.severity,
            reason_code=outcome.reason_code,
            duration_ms=duration_ms,
            rows_examined=budget.rows,
            bytes_examined=budget.bytes,
            details=details,
        )
        finding_pairs = outcome.findings
        if not finding_pairs and outcome.severity != "none":
            finding_pairs = ((outcome.severity, outcome.reason_code),)
        check_findings = tuple(
            IntegrityFinding(spec.check_id, spec.version, severity, reason)
            for severity, reason in finding_pairs
        )
        return result, check_findings

    @staticmethod
    def _snapshot_marker(connection: sqlite3.Connection, subject_id: str) -> dict[str, Any]:
        marker = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE subject_id = ?", (subject_id,)
            ).fetchone()[0]
        )
        return {
            "schema_version": (
                None if marker is None else _durable_int(marker[0], "schema version is invalid")
            ),
            "data_version": int(connection.execute("PRAGMA data_version").fetchone()[0]),
            "database_bytes": page_count * page_size,
            "event_count": event_count,
        }


def _run_isolated_registry_worker(
    sender: Any,
    database_path: str,
    subject_id: str,
    data_root: str,
    profile: IntegrityProfile,
    policy_mode: IntegrityPolicyMode,
    deadline_seconds: float,
    limits: IntegrityAuditLimits,
    check_ids: tuple[str, ...] | None,
    selection: Mapping[str, Any],
) -> None:
    try:
        database = _ReadOnlyDatabase(database_path)
        root = Path(data_root).resolve()
        layout = StorageLayout(
            root=root,
            subject=root / "subject",
            training_raw=root / "training_raw",
            workspace=root / "workspace",
            cache=root / "cache",
            exports=root / "exports",
        )

        def progress(kind: str, payload: object) -> None:
            sender.send((kind, payload))

        report = IntegrityRegistry().run(
            database,
            subject_id,
            layout,
            profile=profile,
            policy_mode=policy_mode,
            deadline_seconds=deadline_seconds,
            limits=limits,
            check_ids=check_ids,
            selection=selection,
            _progress=progress,
        )
        sender.send(("report", report))
    except BaseException as error:
        with suppress(OSError):
            sender.send(("error", type(error).__name__))
    finally:
        sender.close()


@contextmanager
def _sqlite_length_limit(connection: sqlite3.Connection, maximum: int) -> Iterator[None]:
    setlimit = getattr(connection, "setlimit", None)
    if setlimit is None:
        yield
        return
    previous = setlimit(sqlite3.SQLITE_LIMIT_LENGTH, maximum)
    try:
        yield
    finally:
        setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous)


def _mapping_detail(value: object) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)):
        return {"value": value}
    if isinstance(value, (tuple, list)):
        return {"items": list(value)}
    return {"value_type": type(value).__name__}


def _bounded_details(details: Mapping[str, Any], maximum: int) -> Mapping[str, Any]:
    try:
        payload = canonical_json(dict(details)).encode("utf-8")
    except (TypeError, ValueError):
        return {"detail_status": "unserializable"}
    if len(payload) <= maximum:
        return dict(details)
    return {
        "detail_status": "truncated",
        "original_bytes": len(payload),
        "detail_hash": content_hash(dict(details)),
    }


def _report_status(results: Sequence[IntegrityCheckResult]) -> IntegrityStatus:
    if any(result.severity == "p0" for result in results):
        return "corrupt"
    if any(result.status == "incomplete" for result in results):
        return "incomplete"
    if any(result.severity == "p1" for result in results):
        return "degraded"
    return "ok"


_STARTUP_PROFILES: frozenset[IntegrityProfile] = frozenset(
    {"startup_light", "periodic_deep", "manual"}
)
_DEEP_PROFILES: frozenset[IntegrityProfile] = frozenset({"periodic_deep", "manual"})

_CORE_AUDIT_ACTIONS = frozenset(
    {
        "integrity_safe_pause_fallback",
        "model_unknown_reconciled",
        "model_unknown_retry_authorized",
        "model_unknown_retry_cancelled",
        "runtime_exported",
        "training_export_aborted",
        "training_exported",
        "training_policy_updated",
    }
)
_TRAINING_POLICY_FIELDS = (
    "record_enabled",
    "export_enabled",
    "include_private_psychology",
    "include_conversations",
    "include_model_io",
    "include_external_actions",
    "include_workspace",
)
_INITIAL_TRAINING_POLICY = {
    "record_enabled": True,
    "export_enabled": True,
    "include_private_psychology": False,
    "include_conversations": False,
    "include_model_io": False,
    "include_external_actions": True,
    "include_workspace": False,
}


def _default_checks() -> tuple[IntegrityCheckSpec, ...]:
    return (
        IntegrityCheckSpec(
            "core.sqlite_quick_check", 1, "core", _STARTUP_PROFILES, _check_sqlite, "light"
        ),
        IntegrityCheckSpec(
            "core.foreign_keys", 1, "core", _STARTUP_PROFILES, _check_foreign_keys, "light"
        ),
        IntegrityCheckSpec(
            "core.identity_continuity",
            2,
            "core",
            _STARTUP_PROFILES,
            _check_identity_continuity,
            "light",
        ),
        IntegrityCheckSpec(
            "core.event_chain_tail",
            1,
            "core",
            _STARTUP_PROFILES,
            _check_event_chain_tail,
            "light",
        ),
        IntegrityCheckSpec("core.event_payloads", 1, "core", _DEEP_PROFILES, _check_event_payloads),
        IntegrityCheckSpec("core.event_chain", 1, "core", _DEEP_PROFILES, _check_event_chain),
        IntegrityCheckSpec(
            "core.snapshot_archives", 1, "core", _DEEP_PROFILES, _check_snapshot_archives
        ),
        IntegrityCheckSpec(
            "core.archive_dead_letter", 2, "core", _DEEP_PROFILES, _check_archive_dead_letter
        ),
        IntegrityCheckSpec(
            "core.event_causal_order", 1, "core", _DEEP_PROFILES, _check_causal_order
        ),
        IntegrityCheckSpec(
            "core.storage_boundary", 1, "core", _DEEP_PROFILES, _check_storage_boundary
        ),
        IntegrityCheckSpec("core.actions", 1, "core", _DEEP_PROFILES, _check_actions),
        IntegrityCheckSpec("mind.state", 1, "mind", _DEEP_PROFILES, _check_mind),
        IntegrityCheckSpec("mind.memory_blocks", 1, "mind", _DEEP_PROFILES, _check_memory_blocks),
        IntegrityCheckSpec("mind.entities", 1, "mind", _DEEP_PROFILES, _check_entities),
        IntegrityCheckSpec(
            "mind.memory_lifecycle", 1, "mind", _DEEP_PROFILES, _check_memory_lifecycle
        ),
        IntegrityCheckSpec(
            "mind.memory_embeddings", 1, "mind", _DEEP_PROFILES, _check_memory_embeddings
        ),
        IntegrityCheckSpec("sleep.state", 1, "sleep", _DEEP_PROFILES, _check_sleep),
        IntegrityCheckSpec(
            "interaction.state", 1, "interaction", _DEEP_PROFILES, _check_interaction
        ),
        IntegrityCheckSpec(
            "interaction.transport", 1, "interaction", _DEEP_PROFILES, _check_transport
        ),
        IntegrityCheckSpec("capability.state", 1, "capability", _DEEP_PROFILES, _check_capability),
        IntegrityCheckSpec("wallet.state", 1, "wallet", _DEEP_PROFILES, _check_wallet),
        IntegrityCheckSpec("world.state", 1, "world", _DEEP_PROFILES, _check_world),
        IntegrityCheckSpec("learning.outcomes", 1, "learning", _DEEP_PROFILES, _check_outcomes),
        IntegrityCheckSpec(
            "cognition.consciousness",
            1,
            "cognition",
            _DEEP_PROFILES,
            _check_consciousness,
        ),
        IntegrityCheckSpec(
            "cognition.action_deliberation",
            1,
            "cognition",
            _DEEP_PROFILES,
            _check_action_deliberation,
        ),
        IntegrityCheckSpec(
            "cognition.project_executions",
            1,
            "cognition",
            _DEEP_PROFILES,
            _check_project_executions,
        ),
        IntegrityCheckSpec(
            "cognition.goal_governance",
            1,
            "cognition",
            _DEEP_PROFILES,
            _check_goal_governance,
        ),
        IntegrityCheckSpec(
            "cognition.metacognition",
            1,
            "cognition",
            _DEEP_PROFILES,
            _check_metacognition,
        ),
        IntegrityCheckSpec(
            "cognition.motivation", 1, "cognition", _DEEP_PROFILES, _check_motivation
        ),
        IntegrityCheckSpec(
            "cognition.autonomous_projects",
            1,
            "cognition",
            _DEEP_PROFILES,
            _check_autonomous_projects,
        ),
        IntegrityCheckSpec("cognition.research", 1, "cognition", _DEEP_PROFILES, _check_research),
        IntegrityCheckSpec(
            "cognition.self_model", 1, "cognition", _DEEP_PROFILES, _check_self_model
        ),
        IntegrityCheckSpec(
            "cognition.self_modification",
            1,
            "cognition",
            _DEEP_PROFILES,
            _check_self_modification,
        ),
        IntegrityCheckSpec("cognition.thought", 1, "cognition", _DEEP_PROFILES, _check_thought),
        IntegrityCheckSpec("model.ledger", 1, "model", _DEEP_PROFILES, _check_model_ledger),
        IntegrityCheckSpec("model.resources", 1, "model", _DEEP_PROFILES, _check_model_resources),
        IntegrityCheckSpec(
            "model.embedding_resources",
            1,
            "model",
            _DEEP_PROFILES,
            _check_embedding_resources,
        ),
        IntegrityCheckSpec(
            "knowledge.common", 1, "knowledge", _DEEP_PROFILES, _check_common_knowledge
        ),
    )


def _required_row(cursor: _BudgetedCursor) -> Any:
    row = cursor.fetchone()
    if row is None:
        raise IntegrityError("integrity query returned no row")
    return row


def _durable_int(value: object, message: str) -> int:
    try:
        return strict_int(value)
    except (TypeError, ValueError) as error:
        raise IntegrityError(message) from error


def _provenance_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntegrityError(f"{context} is invalid")
    return value


def _provenance_int(value: object, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise IntegrityError(f"{context} is invalid")
    return value


def _provenance_bool(value: object, context: str) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IntegrityError(f"{context} is invalid")
    try:
        return strict_bool(value)
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"{context} is invalid") from error


def _provenance_json_bool(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise IntegrityError(f"{context} is invalid")
    return value


def _provenance_hash(value: object, context: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise IntegrityError(f"{context} is invalid")
    return value


def _provenance_time(value: object, context: str) -> str:
    timestamp = _provenance_text(value, context)
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as error:
        raise IntegrityError(f"{context} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntegrityError(f"{context} is invalid")
    return timestamp


def _provenance_object(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise IntegrityError(f"{context} JSON is invalid")
    try:
        parsed = strict_json_loads(value)
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"{context} JSON is invalid") from error
    if not isinstance(parsed, dict) or canonical_json(parsed) != value:
        raise IntegrityError(f"{context} JSON is invalid")
    return {str(key): item for key, item in parsed.items()}


def _provenance_keys(payload: Mapping[str, Any], expected: set[str], context: str) -> None:
    if set(payload) != expected:
        raise IntegrityError(f"{context} payload is invalid")


def _check_sqlite(context: IntegrityContext) -> IntegrityCheckOutcome:
    result = str(_required_row(context.connection.execute("PRAGMA quick_check(1)"))[0])
    if result != "ok":
        return IntegrityCheckOutcome("corrupt", "p0", "sqlite_quick_check", {"result": result})
    return IntegrityCheckOutcome(details={"result": "ok"})


def _check_foreign_keys(context: IntegrityContext) -> IntegrityCheckOutcome:
    failure = context.connection.execute("PRAGMA foreign_key_check").fetchone()
    if failure is not None:
        return IntegrityCheckOutcome(
            "corrupt",
            "p0",
            "foreign_key_violation",
            {"table": str(failure[0]), "rowid": failure[1]},
        )
    return IntegrityCheckOutcome(details={"result": "ok"})


def _check_identity_continuity(context: IntegrityContext) -> Mapping[str, Any]:
    from .identity import validate_subject_storage_key
    from .lifecycle import ALLOWED_TRANSITIONS

    identity = context.connection.execute(
        "SELECT * FROM subject_identity WHERE subject_id = ?", (context.subject_id,)
    ).fetchone()
    runtime = context.connection.execute(
        "SELECT * FROM runtime_state WHERE subject_id = ?", (context.subject_id,)
    ).fetchone()
    if identity is None or runtime is None:
        raise IntegrityError("subject identity or runtime state is missing")
    storage = context.connection.execute(
        "SELECT storage_key FROM subject_storage_keys WHERE subject_id = ?",
        (context.subject_id,),
    ).fetchone()
    if storage is None:
        raise IntegrityError("subject storage key is missing")
    storage_key = validate_subject_storage_key(str(storage["storage_key"]))
    if storage_key.casefold() == context.subject_id.casefold():
        raise IntegrityError("subject storage key is controlled by the logical subject id")
    runtime_version = _durable_int(runtime["version"], "runtime state version is invalid")
    if runtime["state"] not in ALLOWED_TRANSITIONS or runtime_version < 0:
        raise IntegrityError("runtime state is invalid")
    state_version = _durable_int(identity["state_version"], "identity state version is invalid")
    snapshots = context.connection.execute(
        "SELECT * FROM state_snapshots WHERE subject_id = ? "
        "ORDER BY state_version DESC, created_at DESC, snapshot_id DESC LIMIT 2",
        (context.subject_id,),
    ).fetchmany(2)
    if state_version == 0:
        if identity["last_checkpoint"] is not None or snapshots:
            raise IntegrityError("version-zero identity has checkpoint evidence")
        return {
            "state_version": 0,
            "snapshot_count": 0,
            "lifecycle": runtime["state"],
            "storage_key_format": "subject-random-v1",
        }
    if identity["last_checkpoint"] is None or not snapshots:
        raise IntegrityError("versioned identity has no current checkpoint")
    latest = SnapshotStore._from_row(snapshots[0])
    if latest.snapshot_id != identity["last_checkpoint"] or latest.state_version != state_version:
        raise IntegrityError("identity and latest checkpoint disagree")
    if (
        len(snapshots) == 2
        and _durable_int(snapshots[1]["state_version"], "snapshot state version is invalid")
        >= state_version
    ):
        raise IntegrityError("snapshot ordering is invalid")
    return {
        "state_version": state_version,
        "snapshot_id": latest.snapshot_id,
        "lifecycle": runtime["state"],
        "storage_key_format": "subject-random-v1",
    }


def _check_event_chain_tail(context: IntegrityContext) -> Mapping[str, Any]:
    event_count = int(
        _required_row(
            context.connection.execute(
                "SELECT COUNT(*) FROM events WHERE subject_id = ?", (context.subject_id,)
            )
        )[0]
    )
    chain_count = int(
        _required_row(
            context.connection.execute(
                "SELECT COUNT(*) FROM event_chain_roots WHERE subject_id = ?",
                (context.subject_id,),
            )
        )[0]
    )
    if event_count != chain_count:
        raise IntegrityError("event chain coverage mismatch")
    if event_count == 0:
        return {"event_count": 0, "root_hash": None}
    rows = context.connection.execute(
        "SELECT r.sequence_number, r.event_id AS root_event_id, r.previous_root_hash, "
        "r.root_hash, e.* FROM event_chain_roots r JOIN events e "
        "ON e.event_id = r.event_id AND e.subject_id = r.subject_id "
        "WHERE r.subject_id = ? ORDER BY r.sequence_number DESC LIMIT 2",
        (context.subject_id,),
    ).fetchmany(2)
    tail_sequence = (
        _durable_int(rows[0]["sequence_number"], "event chain tail sequence is invalid")
        if rows
        else None
    )
    if tail_sequence != event_count:
        raise IntegrityError("event chain tail sequence is invalid")
    previous = None if event_count == 1 else str(rows[1]["root_hash"])
    latest = rows[0]
    expected = EventStore._chain_hash(latest, event_count, previous)
    if (
        latest["root_event_id"] != latest["event_id"]
        or latest["previous_root_hash"] != previous
        or latest["root_hash"] != expected
    ):
        raise IntegrityError("event chain tail hash is invalid")
    return {"event_count": event_count, "root_hash": expected}


def _check_event_payloads(context: IntegrityContext) -> IntegrityCheckOutcome:
    watchdog = object.__new__(LongRunResilience)
    watchdog.database = context.database
    watchdog.subject_id = context.subject_id
    watchdog.layout = context.layout
    watchdog.events = EventStore(context.database)
    watchdog.snapshots = SnapshotStore(context.database)
    max_bytes = context.limits.max_bytes_per_check
    segment_bytes = min(64_000_000, max_bytes)
    watchdog.archive_verification_limits = EventArchiveVerificationLimits(
        max_segment_decompressed_bytes=segment_bytes,
        max_total_decompressed_bytes=2**63 - 1,
        max_segments=2**31 - 1,
        max_events=2**63 - 1,
        max_events_per_segment=5_000,
        max_hot_event_payload_bytes=min(2_000_000, context.limits.max_value_bytes),
        max_hot_event_bytes=2**63 - 1,
    )
    watchdog.archive_verification_checkpoint = context.checkpoint
    checks: dict[str, str] = {}
    p0: list[str] = []
    p1: list[str] = []
    event_count = int(
        _required_row(
            context.connection.execute(
                "SELECT COUNT(*) FROM events WHERE subject_id = ?", (context.subject_id,)
            )
        )[0]
    )
    watchdog._audit_event_integrity(context.connection, checks, p0, p1, event_count)
    details = {
        "event_count": event_count,
        "event_hashes": checks.get("event_hashes"),
        "archive": checks.get("event_archive_integrity"),
        "p0_count": len(p0),
        "p1_count": len(p1),
        "sample": (p0 + p1)[:32],
    }
    if p0:
        findings: tuple[tuple[Literal["p0", "p1"], str], ...] = tuple(
            ("p0", reason) for reason in p0[:128]
        )
        return IntegrityCheckOutcome("corrupt", "p0", "event_corruption", details, findings)
    if p1:
        findings = tuple(("p1", reason) for reason in p1[:128])
        return IntegrityCheckOutcome(
            "degraded", "p1", "event_verification_degraded", details, findings
        )
    return IntegrityCheckOutcome(details=details)


def _check_event_chain(context: IntegrityContext) -> object:
    return EventStore(context.database).verify_chain(context.subject_id)


def _check_snapshot_archives(context: IntegrityContext) -> Mapping[str, Any]:
    rows = context.connection.execute(
        "SELECT * FROM snapshot_archives WHERE subject_id = ?", (context.subject_id,)
    )
    archive_count = 0
    verified_entries = 0
    for row in rows:
        context.checkpoint()
        archive_count += 1
        try:
            compressed = bytes(row["compressed_payload"])
        except (TypeError, ValueError) as error:
            raise IntegrityError("snapshot archive compressed payload is invalid") from error
        snapshot_count = _durable_int(row["snapshot_count"], "snapshot archive count is invalid")
        if snapshot_count < 1:
            raise IntegrityError("snapshot archive count is invalid")
        if content_hash({"compressed_hex": compressed.hex()}) != row["compressed_hash"]:
            raise IntegrityError("snapshot archive compressed hash mismatch")
        remaining = context.limits.max_bytes_per_check - context.budget.external_bytes
        if remaining <= 0:
            raise _IntegrityBudgetExceeded("byte_limit")
        decompressor = zlib.decompressobj()
        try:
            decoded = decompressor.decompress(compressed, remaining + 1)
        except zlib.error as error:
            raise IntegrityError("snapshot archive compression stream is invalid") from error
        if decompressor.unconsumed_tail or len(decoded) > remaining:
            raise _IntegrityBudgetExceeded("byte_limit")
        if not decompressor.eof or decompressor.unused_data:
            raise IntegrityError("snapshot archive compression stream is incomplete")
        try:
            decoded += decompressor.flush(max(1, remaining + 1 - len(decoded)))
        except zlib.error as error:
            raise IntegrityError("snapshot archive compression stream is invalid") from error
        if len(decoded) > remaining:
            raise _IntegrityBudgetExceeded("byte_limit")
        context.budget.consume_bytes(len(decoded))
        try:
            payload = strict_json_loads(decoded)
        except (UnicodeError, ValueError, TypeError) as error:
            raise IntegrityError("snapshot archive payload is invalid") from error
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or (
            content_hash(payload) != row["payload_hash"] or len(entries) != snapshot_count
        ):
            raise IntegrityError("snapshot archive payload mismatch")
        if len(entries) > context.limits.max_rows_per_check:
            raise _IntegrityBudgetExceeded("row_limit")
        state: dict[str, Any] | None = None
        for index, entry in enumerate(entries):
            context.checkpoint()
            if not isinstance(entry, dict):
                raise IntegrityError("snapshot archive entry is invalid")
            if index == 0:
                base = entry.get("base_state")
                if not isinstance(base, dict):
                    raise IntegrityError("snapshot archive base state is invalid")
                state = base
            else:
                assert state is not None
                try:
                    state = SnapshotStore._apply_delta(state, entry.get("delta"))
                except (KeyError, TypeError, ValueError) as error:
                    raise IntegrityError("snapshot archive delta is invalid") from error
            if content_hash(state) != entry.get("state_hash"):
                raise IntegrityError("snapshot archive state hash mismatch")
            verified_entries += 1
        del compressed, decoded, payload, entries, state
    return {"snapshot_archives": archive_count, "snapshot_entries": verified_entries}


def _archive_optional_time(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _provenance_time(value, context)


def _archive_budget_checkpoint(context: IntegrityContext) -> None:
    context.checkpoint()
    if context.budget.rows > context.limits.max_rows_per_check:
        raise _IntegrityBudgetExceeded("row_limit")
    if context.budget.bytes > context.limits.max_bytes_per_check:
        raise _IntegrityBudgetExceeded("byte_limit")


def _archive_key_metadata(value: object) -> list[dict[str, str]]:
    if not isinstance(value, str):
        raise IntegrityError("archive keyring metadata JSON is invalid")
    try:
        parsed = strict_json_loads(value)
    except (TypeError, ValueError) as error:
        raise IntegrityError("archive keyring metadata JSON is invalid") from error
    if (
        not isinstance(parsed, list)
        or not parsed
        or len(parsed) > MAX_ARCHIVE_KEYS
        or canonical_json(parsed) != value
    ):
        raise IntegrityError("archive keyring metadata JSON is invalid")

    metadata: list[dict[str, str]] = []
    key_ids: set[str] = set()
    active_count = 0
    for entry in parsed:
        if not isinstance(entry, dict) or set(entry) != {"key_id", "fingerprint", "status"}:
            raise IntegrityError("archive keyring metadata entry is invalid")
        key_id = entry["key_id"]
        fingerprint = entry["fingerprint"]
        status = entry["status"]
        if not isinstance(key_id, str) or not key_id.strip() or key_id in key_ids:
            raise IntegrityError("archive keyring key ID is invalid")
        try:
            ArchiveKeyring._validate_key_id(key_id)
        except ValueError as error:
            raise IntegrityError("archive keyring key ID is invalid") from error
        if status not in {"active", "retired"}:
            raise IntegrityError("archive keyring key status is invalid")
        _provenance_hash(fingerprint, "archive keyring fingerprint")
        key_ids.add(key_id)
        active_count += int(status == "active")
        metadata.append({"key_id": key_id, "fingerprint": fingerprint, "status": status})
    if active_count != 1:
        raise IntegrityError("archive keyring active key count is invalid")
    if metadata != sorted(metadata, key=lambda entry: entry["key_id"]):
        raise IntegrityError("archive keyring key metadata ordering is invalid")
    return metadata


def _verify_archive_keyring_revisions(context: IntegrityContext) -> tuple[int, int]:
    """Verify the global keyring history and per-subject catch-up state.

    Keyring generations are global, while a subject may be asleep when one or
    more rotations occur.  Consequently a subject's rows are *not* required to
    start at generation one or to contain every intermediate generation.  The
    global set of generations must still be contiguous, and every subject row
    must agree with the single metadata snapshot recorded for that generation.
    """

    subject_revision_count = 0
    subject_key_count = 0
    historical_fingerprints: dict[str, str] = {}
    generation_states: dict[int, tuple[str, str, str | None]] = {}
    subject_generations: dict[str, list[int]] = {}
    subject_created_at: dict[str, datetime] = {}
    rows = context.connection.execute(
        "SELECT r.*, owner.subject_id AS owner_subject_id "
        "FROM archive_keyring_revisions r "
        "LEFT JOIN subject_identity owner ON owner.subject_id = r.subject_id "
        "ORDER BY r.generation, r.subject_id, r.revision_id"
    )
    for row in rows:
        _archive_budget_checkpoint(context)
        revision_id = _provenance_text(row["revision_id"], "archive keyring revision ID")
        subject_id = _provenance_text(row["subject_id"], "archive keyring revision subject ID")
        if row["owner_subject_id"] != subject_id:
            raise IntegrityError(f"archive keyring revision owner is invalid: {revision_id}")
        generation = _durable_int(row["generation"], "archive keyring generation is invalid")
        if generation < 1:
            raise IntegrityError("archive keyring generation is invalid")
        # Keep the integrity checker and the runtime archive reader on the same
        # authenticated revision contract.  The canonical helper additionally
        # enforces canonical JSON, the key-count bound, and exactly one active key.
        validated_generation, validated_metadata = ArchiveKeyring._validated_revision(row)
        if validated_generation != generation:
            raise IntegrityError("archive keyring generation is invalid")
        metadata = list(validated_metadata)
        canonical_metadata = _archive_key_metadata(row["key_metadata_json"])
        if canonical_metadata != metadata:
            raise IntegrityError("archive keyring metadata JSON is invalid")
        if row["format"] != ARCHIVE_KEYRING_FORMAT:
            raise IntegrityError("archive keyring revision format is invalid")
        active_key_id = _provenance_text(row["active_key_id"], "archive keyring active key ID")
        legacy_key_id = row["legacy_key_id"]
        if legacy_key_id is not None:
            legacy_key_id = _provenance_text(legacy_key_id, "archive keyring legacy key ID")
        by_id = {entry["key_id"]: entry for entry in metadata}
        if active_key_id not in by_id or by_id[active_key_id]["status"] != "active":
            raise IntegrityError("archive keyring active key metadata is inconsistent")
        if legacy_key_id is not None and legacy_key_id not in by_id:
            raise IntegrityError("archive keyring legacy key metadata is inconsistent")

        metadata_hash = _provenance_hash(row["metadata_hash"], "archive keyring metadata hash")
        if metadata_hash != content_hash(metadata):
            raise IntegrityError("archive keyring metadata hash mismatch")
        state = (metadata_hash, active_key_id, legacy_key_id)
        previous_state = generation_states.setdefault(generation, state)
        if previous_state != state:
            raise IntegrityError("archive keyring generation has conflicting global metadata")

        for entry in metadata:
            previous = historical_fingerprints.setdefault(entry["key_id"], entry["fingerprint"])
            if previous != entry["fingerprint"]:
                raise IntegrityError("archive key ID has conflicting historical fingerprints")
        expected_state_hash = content_hash(
            {
                "generation": generation,
                "format": ARCHIVE_KEYRING_FORMAT,
                "active_key_id": active_key_id,
                "legacy_key_id": legacy_key_id,
                "key_metadata": metadata,
            }
        )
        if _provenance_hash(row["state_hash"], "archive keyring state hash") != expected_state_hash:
            raise IntegrityError("archive keyring state hash mismatch")

        created_at = datetime.fromisoformat(
            _provenance_time(row["created_at"], "archive keyring revision created at")
        )
        previous_created_at = subject_created_at.get(subject_id)
        if previous_created_at is not None and created_at < previous_created_at:
            raise IntegrityError("archive keyring revision chronology is invalid")
        subject_created_at[subject_id] = created_at
        subject_generations.setdefault(subject_id, []).append(generation)
        if subject_id == context.subject_id:
            subject_revision_count += 1
            subject_key_count += len(metadata)

    generations = sorted(generation_states)
    for previous_generation, current_generation in pairwise(generations):
        if current_generation != previous_generation + 1:
            raise IntegrityError("archive keyring global generation history has a gap")
    for subject_generations_for_subject in subject_generations.values():
        for previous_generation, current_generation in pairwise(subject_generations_for_subject):
            if current_generation <= previous_generation:
                raise IntegrityError("archive keyring subject generation history is invalid")
    return subject_revision_count, subject_key_count


def _archive_replica_state_hash(row: Any) -> str:
    byte_size = _durable_int(row["byte_size"], "archive replica byte size is invalid")
    if byte_size < 1:
        raise IntegrityError("archive replica byte size is invalid")
    object_kind = row["object_kind"]
    replica_type = row["replica_type"]
    state = row["state"]
    if object_kind not in {"event_segment", "observation_segment"}:
        raise IntegrityError("archive replica object kind is invalid")
    if replica_type not in {"local", "cloud"}:
        raise IntegrityError("archive replica type is invalid")
    if state not in ArchiveReplicaLedger._TRANSITIONS:
        raise IntegrityError("archive replica state is invalid")
    return ArchiveReplicaLedger._state_hash(
        _provenance_text(row["subject_id"], "archive replica subject ID"),
        _provenance_text(row["object_key"], "archive replica object key"),
        object_kind,
        replica_type,
        _provenance_text(row["provider_id"], "archive replica provider ID"),
        _provenance_hash(row["ciphertext_hash"], "archive replica ciphertext hash"),
        byte_size,
        state,
        verified_at=_archive_optional_time(row["verified_at"], "archive replica verified at"),
        last_accessed_at=_archive_optional_time(
            row["last_accessed_at"], "archive replica last accessed at"
        ),
        removed_at=_archive_optional_time(row["removed_at"], "archive replica removed at"),
        restored_at=_archive_optional_time(row["restored_at"], "archive replica restored at"),
        last_error_code=(
            None
            if row["last_error_code"] is None
            else _provenance_text(row["last_error_code"], "archive replica error code")
        ),
    )


def _verify_archive_replicas(context: IntegrityContext) -> tuple[int, int]:
    replica_fields = (
        "subject_id",
        "object_key",
        "object_kind",
        "replica_type",
        "provider_id",
        "ciphertext_hash",
        "byte_size",
    )
    mirrored_fields = (
        *replica_fields,
        "state",
        "verified_at",
        "last_accessed_at",
        "removed_at",
        "restored_at",
        "last_error_code",
        "state_hash",
    )
    replicas: dict[str, dict[str, Any]] = {}
    rows = context.connection.execute(
        "SELECT * FROM archive_object_replicas WHERE subject_id = ? ORDER BY replica_id",
        (context.subject_id,),
    )
    for row in rows:
        _archive_budget_checkpoint(context)
        replica_id = _provenance_text(row["replica_id"], "archive replica ID")
        current_revision = _durable_int(
            row["current_revision"], "archive replica current revision is invalid"
        )
        if current_revision < 1 or replica_id in replicas:
            raise IntegrityError("archive replica current revision is invalid")
        if _provenance_hash(row["state_hash"], "archive replica state hash") != (
            _archive_replica_state_hash(row)
        ):
            raise IntegrityError("archive replica state hash mismatch")
        created_at = _provenance_time(row["created_at"], "archive replica created at")
        updated_at = _provenance_time(row["updated_at"], "archive replica updated at")
        if datetime.fromisoformat(updated_at) < datetime.fromisoformat(created_at):
            raise IntegrityError("archive replica chronology is invalid")
        for timestamp_field in (
            "verified_at",
            "last_accessed_at",
            "removed_at",
            "restored_at",
        ):
            timestamp = row[timestamp_field]
            if timestamp is not None and datetime.fromisoformat(
                str(timestamp)
            ) > datetime.fromisoformat(updated_at):
                raise IntegrityError("archive replica state chronology is invalid")
        replicas[replica_id] = {
            **{field: row[field] for field in mirrored_fields},
            "current_revision": current_revision,
            "revision_count": 0,
            "previous_state": None,
            "created_at": created_at,
            "updated_at": updated_at,
            "previous_revision_created_at": None,
        }

    revision_count = 0
    revisions = context.connection.execute(
        "SELECT * FROM archive_object_replica_revisions WHERE subject_id = ? "
        "ORDER BY replica_id, revision_number",
        (context.subject_id,),
    )
    for row in revisions:
        _archive_budget_checkpoint(context)
        _provenance_text(row["revision_id"], "archive replica revision ID")
        replica_id = _provenance_text(row["replica_id"], "archive replica revision parent ID")
        parent = replicas.get(replica_id)
        if parent is None:
            raise IntegrityError("archive replica revision parent is missing")
        revision_number = _durable_int(
            row["revision_number"], "archive replica revision number is invalid"
        )
        expected_revision = int(parent["revision_count"]) + 1
        if revision_number != expected_revision:
            raise IntegrityError("archive replica revision sequence is invalid")
        for column in replica_fields:
            if row[column] != parent[column]:
                raise IntegrityError("archive replica revision immutable metadata mismatch")
        state_hash = _provenance_hash(row["state_hash"], "archive replica revision state hash")
        if state_hash != _archive_replica_state_hash(row):
            raise IntegrityError("archive replica revision state hash mismatch")
        previous_state = parent["previous_state"]
        if (
            previous_state is not None
            and row["state"] not in ArchiveReplicaLedger._TRANSITIONS[previous_state]
        ):
            raise IntegrityError("archive replica revision transition is invalid")
        _provenance_text(row["reason"], "archive replica revision reason")
        created_at = _provenance_time(row["created_at"], "archive replica revision created at")
        previous_created_at = parent["previous_revision_created_at"]
        if previous_created_at is not None and datetime.fromisoformat(
            created_at
        ) < datetime.fromisoformat(previous_created_at):
            raise IntegrityError("archive replica revision chronology is invalid")
        if revision_number == 1 and created_at != parent["created_at"]:
            raise IntegrityError("archive replica initial revision chronology mismatch")
        parent["revision_count"] = revision_number
        parent["previous_state"] = row["state"]
        parent["previous_revision_created_at"] = created_at
        if revision_number == parent["current_revision"]:
            for column in mirrored_fields:
                if row[column] != parent[column]:
                    raise IntegrityError("archive replica current revision mirror mismatch")
            if created_at != parent["updated_at"]:
                raise IntegrityError("archive replica current revision chronology mismatch")
        revision_count += 1

    for parent in replicas.values():
        if parent["revision_count"] != parent["current_revision"]:
            raise IntegrityError("archive replica revision coverage mismatch")
    return len(replicas), revision_count


def _verify_storage_usage_samples(context: IntegrityContext) -> int:
    count = 0
    nonnegative_fields = (
        "subject_bytes",
        "effective_subject_bytes",
        "database_bytes",
        "database_reclaimable_bytes",
        "wal_bytes",
        "local_archive_bytes",
        "cloud_staging_bytes",
        "exports_bytes",
        "training_bytes",
        "workspace_bytes",
        "free_bytes",
    )
    rows = context.connection.execute(
        "SELECT * FROM storage_usage_samples WHERE subject_id = ? ORDER BY created_at, sample_id",
        (context.subject_id,),
    )
    for row in rows:
        _archive_budget_checkpoint(context)
        _provenance_text(row["sample_id"], "storage usage sample ID")
        for column in nonnegative_fields:
            if _durable_int(row[column], f"storage usage sample {column} is invalid") < 0:
                raise IntegrityError(f"storage usage sample {column} is invalid")
        for column in ("subject_quota_bytes", "minimum_free_bytes"):
            if _durable_int(row[column], f"storage usage sample {column} is invalid") < 1:
                raise IntegrityError(f"storage usage sample {column} is invalid")
        subject_bytes = _durable_int(
            row["subject_bytes"], "storage usage sample subject bytes is invalid"
        )
        database_bytes = _durable_int(
            row["database_bytes"], "storage usage sample database bytes is invalid"
        )
        reclaimable_bytes = _durable_int(
            row["database_reclaimable_bytes"],
            "storage usage sample database reclaimable bytes is invalid",
        )
        effective_subject_bytes = _durable_int(
            row["effective_subject_bytes"],
            "storage usage sample effective subject bytes is invalid",
        )
        accounted_subject_bytes = (
            database_bytes
            + _durable_int(row["wal_bytes"], "storage usage sample WAL bytes is invalid")
            + _durable_int(
                row["local_archive_bytes"],
                "storage usage sample local archive bytes is invalid",
            )
            + _durable_int(row["exports_bytes"], "storage usage sample export bytes is invalid")
        )
        if (
            reclaimable_bytes > database_bytes
            or effective_subject_bytes != max(0, subject_bytes - reclaimable_bytes)
            or accounted_subject_bytes > subject_bytes
        ):
            raise IntegrityError("storage usage sample byte accounting is invalid")
        _provenance_time(row["created_at"], "storage usage sample created at")
        if _provenance_hash(row["state_hash"], "storage usage sample state hash") != (
            storage_usage_sample_state_hash(row)
        ):
            raise IntegrityError("storage usage sample state hash mismatch")
        count += 1
    return count


_ARCHIVE_STAGING_KINDS: Mapping[str, str] = {
    "event_payload": "noyra-event-payload-segment-v1",
    "observation_content": "noyra-observation-content-segment-v1",
}


def _archive_staging_source_state(
    context: IntegrityContext,
    row: Any,
    *,
    archive_kind: str,
    manifest_id: str,
    item_count: int,
) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    """Validate the immutable source selection without reading provider bytes.

    The staging manifest is the durable boundary between a provider write and
    the short SQLite finalize transaction.  Its source selection must remain a
    canonical, bounded ledger even when the provider object is unavailable.
    """
    raw_source = row["source_state_json"]
    if not isinstance(raw_source, str) or not raw_source:
        raise IntegrityError(f"archive staging source state is invalid: {manifest_id}")
    try:
        parsed = strict_json_loads(raw_source)
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"archive staging source state is invalid: {manifest_id}") from error
    if canonical_json(parsed) != raw_source or not isinstance(parsed, list):
        raise IntegrityError(f"archive staging source state is not canonical: {manifest_id}")
    if len(parsed) != item_count:
        raise IntegrityError(f"archive staging source count mismatch: {manifest_id}")
    if item_count > context.limits.max_rows_per_check:
        raise _IntegrityBudgetExceeded("row_limit")
    source_hash = _provenance_hash(
        row["source_state_hash"], f"archive staging source hash: {manifest_id}"
    )
    if source_hash != content_hash(parsed):
        raise IntegrityError(f"archive staging source hash mismatch: {manifest_id}")

    if archive_kind == "event_payload":
        expected_keys = {"event_id", "occurred_at", "payload_hash"}
        id_field = "event_id"
        time_field = "occurred_at"
        hash_field = "payload_hash"
    else:
        expected_keys = {"observation_id", "fetched_at", "content_hash"}
        id_field = "observation_id"
        time_field = "fetched_at"
        hash_field = "content_hash"

    normalized: list[dict[str, str]] = []
    by_id: dict[str, dict[str, str]] = {}
    times: list[str] = []
    parsed_times: list[datetime] = []
    for item in parsed:
        context.checkpoint()
        if (
            not isinstance(item, dict)
            or set(item) != expected_keys
            or not all(isinstance(item.get(field), str) for field in expected_keys)
        ):
            raise IntegrityError(f"archive staging source item is invalid: {manifest_id}")
        identifier = str(item[id_field])
        if not identifier.strip() or identifier in by_id:
            raise IntegrityError(f"archive staging source IDs are invalid: {manifest_id}")
        timestamp = _provenance_time(
            item[time_field], f"archive staging source time: {manifest_id}"
        )
        digest = _provenance_hash(item[hash_field], f"archive staging source hash: {manifest_id}")
        normalized_item = {
            id_field: identifier,
            time_field: timestamp,
            hash_field: digest,
        }
        normalized.append(normalized_item)
        by_id[identifier] = normalized_item
        times.append(timestamp)
        parsed_times.append(datetime.fromisoformat(timestamp))

    if not times or times != sorted(times) or parsed_times != sorted(parsed_times):
        raise IntegrityError(f"archive staging source chronology is invalid: {manifest_id}")
    first_item_at = _provenance_time(
        row["first_item_at"], f"archive staging first item time: {manifest_id}"
    )
    last_item_at = _provenance_time(
        row["last_item_at"], f"archive staging last item time: {manifest_id}"
    )
    if first_item_at != times[0] or last_item_at != times[-1]:
        raise IntegrityError(f"archive staging source time bounds mismatch: {manifest_id}")
    return normalized, by_id


def _verify_archive_staging_pointers(
    context: IntegrityContext,
    row: Any,
    *,
    archive_kind: str,
    manifest_id: str,
    subject_id: str,
    segment_id: str,
    object_key: str,
    source_by_id: dict[str, dict[str, str]],
    item_count: int,
    archive_format: str,
    encryption_key_id: str,
    encryption_key_fingerprint: str,
    finalized_at: str,
) -> None:
    """Reconcile a committed manifest with its DB segment and source pointers."""
    if archive_kind == "event_payload":
        segment_rows = context.connection.execute(
            "SELECT * FROM event_payload_segments WHERE segment_id = ? OR object_key = ?",
            (segment_id, object_key),
        )
        segments = segment_rows.fetchmany(2)
        if len(segments) != 1:
            raise IntegrityError(
                f"archive staging committed segment ownership mismatch: {manifest_id}"
            )
        segment = segments[0]
        if (
            segment["subject_id"] != subject_id
            or segment["segment_id"] != segment_id
            or segment["object_key"] != object_key
            or segment["first_occurred_at"] != row["first_item_at"]
            or segment["last_occurred_at"] != row["last_item_at"]
            or segment["event_count"] != item_count
            or segment["compressed_hash"] != row["plaintext_hash"]
            or segment["archive_format"] != archive_format
            or segment["encryption_key_id"] != encryption_key_id
            or segment["encryption_key_fingerprint"] != encryption_key_fingerprint
            or segment["created_at"] != finalized_at
        ):
            raise IntegrityError(
                f"archive staging committed segment metadata mismatch: {manifest_id}"
            )
        pointers = context.connection.execute(
            "SELECT event_id, occurred_at, payload_hash, payload_archive_key, "
            "payload_archived_at, payload_json FROM events "
            "WHERE subject_id = ? AND payload_archive_key = ? "
            "ORDER BY occurred_at, event_id",
            (subject_id, object_key),
        )
        seen: set[str] = set()
        for pointer in pointers:
            context.checkpoint()
            event_id = _provenance_text(pointer["event_id"], "archive staging event pointer ID")
            if event_id in seen:
                raise IntegrityError(f"archive staging event pointer is duplicated: {manifest_id}")
            seen.add(event_id)
            expected = source_by_id.get(event_id)
            if (
                expected is None
                or pointer["occurred_at"] != expected["occurred_at"]
                or pointer["payload_hash"] != expected["payload_hash"]
                or pointer["payload_archive_key"] != object_key
                or pointer["payload_json"] != "{}"
                or pointer["payload_archived_at"] != finalized_at
            ):
                raise IntegrityError(f"archive staging event pointer mismatch: {manifest_id}")
        if seen != set(source_by_id) or len(seen) != item_count:
            raise IntegrityError(f"archive staging event pointer count mismatch: {manifest_id}")
        return

    segment_rows = context.connection.execute(
        "SELECT * FROM observation_content_segments WHERE segment_id = ? OR object_key = ?",
        (segment_id, object_key),
    )
    segments = segment_rows.fetchmany(2)
    if len(segments) != 1:
        raise IntegrityError(f"archive staging committed segment ownership mismatch: {manifest_id}")
    segment = segments[0]
    if (
        segment["subject_id"] != subject_id
        or segment["segment_id"] != segment_id
        or segment["object_key"] != object_key
        or segment["first_fetched_at"] != row["first_item_at"]
        or segment["last_fetched_at"] != row["last_item_at"]
        or segment["observation_count"] != item_count
        or segment["compressed_hash"] != row["plaintext_hash"]
        or segment["archive_format"] != archive_format
        or segment["encryption_key_id"] != encryption_key_id
        or segment["encryption_key_fingerprint"] != encryption_key_fingerprint
        or segment["created_at"] != finalized_at
    ):
        raise IntegrityError(f"archive staging committed segment metadata mismatch: {manifest_id}")
    pointers = context.connection.execute(
        "SELECT observation_id, fetched_at, content_hash, content_archive_key, "
        "content_archived_at, content FROM observations "
        "WHERE subject_id = ? AND content_archive_key = ? ORDER BY fetched_at, observation_id",
        (subject_id, object_key),
    )
    seen = set()
    for pointer in pointers:
        context.checkpoint()
        observation_id = _provenance_text(
            pointer["observation_id"], "archive staging observation pointer ID"
        )
        if observation_id in seen:
            raise IntegrityError(
                f"archive staging observation pointer is duplicated: {manifest_id}"
            )
        seen.add(observation_id)
        expected = source_by_id.get(observation_id)
        if (
            expected is None
            or pointer["fetched_at"] != expected["fetched_at"]
            or pointer["content_hash"] != expected["content_hash"]
            or pointer["content_archive_key"] != object_key
            or pointer["content"] != ""
            or pointer["content_archived_at"] != finalized_at
        ):
            raise IntegrityError(f"archive staging observation pointer mismatch: {manifest_id}")
    if seen != set(source_by_id) or len(seen) != item_count:
        raise IntegrityError(f"archive staging observation pointer count mismatch: {manifest_id}")


def _verify_archive_staging_manifests(context: IntegrityContext) -> Mapping[str, int]:
    """Verify the DB-side two-phase archive staging ledger.

    This deliberately does not read provider objects.  Provider bytes are
    verified by the archive-specific deep checks; this registry check protects
    the SQLite half of the publication protocol and remains bounded by the
    common integrity row/value budgets.
    """
    manifests = 0
    committed = 0
    live = 0
    rows = context.connection.execute(
        "SELECT m.*, owner.subject_id AS owner_subject_id "
        "FROM archive_staging_manifests m "
        "LEFT JOIN subject_identity owner ON owner.subject_id = m.subject_id "
        "WHERE m.subject_id = ? ORDER BY m.rowid",
        (context.subject_id,),
    )
    for row in rows:
        _archive_budget_checkpoint(context)
        manifests += 1
        manifest_id = _provenance_text(row["manifest_id"], "archive staging manifest ID")
        subject_id = _provenance_text(row["subject_id"], "archive staging subject ID")
        if subject_id != context.subject_id or row["owner_subject_id"] != subject_id:
            raise IntegrityError(f"archive staging subject ownership mismatch: {manifest_id}")
        archive_kind = row["archive_kind"]
        if archive_kind not in _ARCHIVE_STAGING_KINDS:
            raise IntegrityError(f"archive staging kind is invalid: {manifest_id}")
        expected_format = _ARCHIVE_STAGING_KINDS[str(archive_kind)]
        archive_format = _provenance_text(row["archive_format"], "archive staging format")
        if archive_format != expected_format:
            raise IntegrityError(f"archive staging format is invalid: {manifest_id}")
        segment_id = _provenance_text(row["segment_id"], "archive staging segment ID")
        if (
            not segment_id.strip()
            or segment_id != segment_id.strip()
            or segment_id in {".", ".."}
            or any(character in segment_id for character in "/\\:")
        ):
            raise IntegrityError(f"archive staging segment ID is invalid: {manifest_id}")
        object_key = _provenance_text(row["object_key"], "archive staging object key")
        prefix = "events" if archive_kind == "event_payload" else "observations"
        if object_key != f"{prefix}/{segment_id}.json.zlib.enc":
            raise IntegrityError(f"archive staging object key is invalid: {manifest_id}")
        encryption_key_id = _provenance_text(
            row["encryption_key_id"], "archive staging encryption key ID"
        )
        try:
            ArchiveKeyring._validate_key_id(encryption_key_id)
        except ValueError as error:
            raise IntegrityError(
                f"archive staging encryption key ID is invalid: {manifest_id}"
            ) from error
        encryption_key_fingerprint = _provenance_hash(
            row["encryption_key_fingerprint"],
            f"archive staging encryption key fingerprint: {manifest_id}",
        )
        item_count = _durable_int(row["item_count"], "archive staging item count")
        if item_count < 1:
            raise IntegrityError(f"archive staging item count is invalid: {manifest_id}")
        _provenance_hash(row["plaintext_hash"], f"archive staging plaintext hash: {manifest_id}")
        _source_state, source_by_id = _archive_staging_source_state(
            context,
            row,
            archive_kind=str(archive_kind),
            manifest_id=manifest_id,
            item_count=item_count,
        )
        created_at = _provenance_time(row["created_at"], "archive staging created at")
        updated_at = _provenance_time(row["updated_at"], "archive staging updated at")
        created_dt = datetime.fromisoformat(created_at)
        updated_dt = datetime.fromisoformat(updated_at)
        if updated_dt < created_dt:
            raise IntegrityError(f"archive staging chronology is invalid: {manifest_id}")
        source_last_dt = datetime.fromisoformat(str(row["last_item_at"]))
        if source_last_dt > created_dt:
            raise IntegrityError(f"archive staging source chronology is invalid: {manifest_id}")
        status = row["status"]
        if status not in {"prepared", "stored", "committed", "abandoned", "removed"}:
            raise IntegrityError(f"archive staging status is invalid: {manifest_id}")
        finalized = row["finalized_at"]
        finalized_at = (
            None
            if finalized is None
            else _provenance_time(finalized, "archive staging finalized at")
        )
        if finalized_at is not None and datetime.fromisoformat(finalized_at) < created_dt:
            raise IntegrityError(f"archive staging finalized chronology is invalid: {manifest_id}")
        stored_size = row["stored_byte_size"]
        stored_hash = row["stored_hash"]
        if (stored_size is None) != (stored_hash is None):
            raise IntegrityError(f"archive staging storage metadata is incomplete: {manifest_id}")
        if stored_size is not None:
            stored_size = _durable_int(stored_size, "archive staging stored byte size")
            if stored_size < 0:
                raise IntegrityError(f"archive staging stored byte size is invalid: {manifest_id}")
            _provenance_hash(stored_hash, f"archive staging stored hash: {manifest_id}")
        if status in {"prepared", "stored", "abandoned"} and finalized_at is not None:
            raise IntegrityError(f"archive staging status chronology is invalid: {manifest_id}")
        if status in {"committed", "removed"} and finalized_at is None:
            raise IntegrityError(f"archive staging finalization metadata is missing: {manifest_id}")
        if status in {"stored", "committed"} and stored_size is None:
            raise IntegrityError(f"archive staging storage metadata is missing: {manifest_id}")
        if status == "prepared" and stored_size is not None:
            raise IntegrityError(
                f"archive staging prepared storage metadata is invalid: {manifest_id}"
            )
        if status in {"prepared", "stored", "committed"} and row["last_error"] is not None:
            raise IntegrityError(f"archive staging error metadata is invalid: {manifest_id}")
        if status in {"abandoned", "removed"} and (
            not isinstance(row["last_error"], str) or not row["last_error"].strip()
        ):
            raise IntegrityError(f"archive staging error metadata is missing: {manifest_id}")
        if status in {"committed", "removed"}:
            assert finalized_at is not None
            if finalized_at != updated_at:
                raise IntegrityError(
                    f"archive staging finalization chronology is invalid: {manifest_id}"
                )
        if status == "committed":
            committed += 1
            assert finalized_at is not None
            _verify_archive_staging_pointers(
                context,
                row,
                archive_kind=str(archive_kind),
                manifest_id=manifest_id,
                subject_id=subject_id,
                segment_id=segment_id,
                object_key=object_key,
                source_by_id=source_by_id,
                item_count=item_count,
                archive_format=archive_format,
                encryption_key_id=encryption_key_id,
                encryption_key_fingerprint=encryption_key_fingerprint,
                finalized_at=finalized_at,
            )
        else:
            live += int(status in {"prepared", "stored"})
            # A segment/pointer published before the manifest CAS is a split
            # commit and must not be silently replayed.
            table = (
                "event_payload_segments"
                if archive_kind == "event_payload"
                else "observation_content_segments"
            )
            if (
                context.connection.execute(
                    f"SELECT 1 FROM {table} WHERE segment_id = ? OR object_key = ? LIMIT 1",
                    (segment_id, object_key),
                ).fetchone()
                is not None
            ):
                raise IntegrityError(f"archive staging unpublished segment pointer: {manifest_id}")
    return {"staging_manifests": manifests, "staging_committed": committed, "staging_live": live}


def _check_archive_dead_letter(context: IntegrityContext) -> IntegrityCheckOutcome:
    keyring_revisions, key_metadata_entries = _verify_archive_keyring_revisions(context)
    archive_replicas, replica_revisions = _verify_archive_replicas(context)
    usage_samples = _verify_storage_usage_samples(context)
    staging = _verify_archive_staging_manifests(context)
    count = int(
        _required_row(
            context.connection.execute(
                "SELECT COUNT(*) FROM archive_transfer_queue "
                "WHERE subject_id = ? AND status = 'dead'",
                (context.subject_id,),
            )
        )[0]
    )
    _archive_budget_checkpoint(context)
    details = {
        "dead_transfers": count,
        "keyring_revisions": keyring_revisions,
        "key_metadata_entries": key_metadata_entries,
        "archive_replicas": archive_replicas,
        "replica_revisions": replica_revisions,
        "storage_usage_samples": usage_samples,
        **staging,
    }
    if count:
        return IntegrityCheckOutcome("degraded", "p1", "archive_dead_letter", details)
    return IntegrityCheckOutcome(details=details)


def _check_causal_order(context: IntegrityContext) -> IntegrityCheckOutcome:
    anomalies = EventStore(context.database).causal_anomalies(context.subject_id, limit=32)
    if anomalies:
        return IntegrityCheckOutcome(
            "degraded",
            "p1",
            "causal_order_anomaly",
            {"count": len(anomalies), "sample": anomalies[:8]},
        )
    return IntegrityCheckOutcome(details={"count": 0})


def _check_storage_boundary(context: IntegrityContext) -> Mapping[str, Any]:
    subject_bytes = 0
    roots = (
        context.layout.subject,
        context.layout.exports,
        context.layout.root / "secrets",
    )
    for root in roots:
        subject_bytes += _bounded_tree_size(root, context)
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{context.source_database.path}{suffix}")
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            context.budget.consume_file_entry()
            subject_bytes += metadata.st_size
    return {
        "subject_bytes": subject_bytes,
        "free_bytes": shutil.disk_usage(context.layout.root).free,
        "files_examined": context.budget.files,
    }


def _bounded_tree_size(root: Path, context: IntegrityContext) -> int:
    try:
        root_metadata = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return 0
    if stat.S_ISLNK(root_metadata.st_mode):
        return 0
    total = 0
    stack = [root]
    context.budget.consume_file_entry()
    while stack:
        context.checkpoint()
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                context.checkpoint()
                context.budget.consume_file_entry()
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    return total


def _verify_core_provenance(context: IntegrityContext) -> Mapping[str, int]:
    from .storage import TrainingStore

    subject_id = context.subject_id
    policy_cursor = context.connection.execute(
        "SELECT * FROM training_policies WHERE subject_id = ?", (subject_id,)
    )
    policy = policy_cursor.fetchone()
    if policy is None or policy_cursor.fetchone() is not None:
        raise IntegrityError("training policy is missing or duplicated")
    if _provenance_text(policy["subject_id"], "training policy subject") != subject_id:
        raise IntegrityError("training policy ownership is invalid")
    policy_flags = {
        field: _provenance_bool(policy[field], f"training policy {field}")
        for field in _TRAINING_POLICY_FIELDS
    }
    policy_version = _provenance_int(policy["policy_version"], "training policy version", minimum=1)
    policy_updated_at = _provenance_time(policy["updated_at"], "training policy updated_at")

    record_rows = context.connection.execute(
        "SELECT r.*, e.subject_id AS event_subject_id, "
        "e.event_type AS event_record_kind, e.privacy_level AS event_privacy_level, "
        "e.payload_hash AS event_source_hash, e.occurred_at AS event_created_at, "
        "e.observed_at AS event_observed_at "
        "FROM training_records r LEFT JOIN events e ON e.event_id = r.event_id "
        "WHERE r.subject_id = ? OR e.subject_id = ? "
        "ORDER BY r.created_at, r.record_id",
        (subject_id, subject_id),
    )
    record_count = 0
    for row in record_rows:
        record_count += 1
        record_id = _provenance_text(row["record_id"], "training record id")
        context_label = f"training record {record_id}"
        row_subject = _provenance_text(row["subject_id"], f"{context_label} subject")
        event_id = _provenance_text(row["event_id"], f"{context_label} event")
        record_kind = _provenance_text(row["record_kind"], f"{context_label} record kind")
        privacy_level = _provenance_text(row["privacy_level"], f"{context_label} privacy level")
        eligibility = _provenance_text(row["eligibility"], f"{context_label} eligibility")
        redaction_status = _provenance_text(
            row["redaction_status"], f"{context_label} redaction status"
        )
        source_hash = _provenance_hash(row["source_hash"], f"{context_label} source hash")
        consent_version = _provenance_int(
            row["consent_version"], f"{context_label} consent version", minimum=1
        )
        created_at = _provenance_time(row["created_at"], f"{context_label} created_at")
        updated_at = _provenance_time(row["updated_at"], f"{context_label} updated_at")
        event_observed_at = _provenance_time(
            row["event_observed_at"], f"{context_label} event observed_at"
        )
        expected = TrainingStore._classification(record_kind, privacy_level, policy_flags)
        if (
            row_subject != subject_id
            or row["event_subject_id"] != subject_id
            or row["event_record_kind"] != record_kind
            or row["event_privacy_level"] != privacy_level
            or row["event_source_hash"] != source_hash
            or row["event_created_at"] != created_at
            or consent_version != policy_version
            or (eligibility, redaction_status) != expected
            or datetime.fromisoformat(updated_at) < datetime.fromisoformat(created_at)
            or datetime.fromisoformat(updated_at) < datetime.fromisoformat(event_observed_at)
        ):
            raise IntegrityError(f"{context_label} ownership or state is invalid")
        if not event_id:
            raise IntegrityError(f"{context_label} event is invalid")

    export_rows = context.connection.execute(
        "SELECT * FROM training_exports WHERE subject_id = ? ORDER BY created_at, export_id",
        (subject_id,),
    )
    exports: dict[str, dict[str, Any]] = {}
    export_count = 0
    for row in export_rows:
        export_count += 1
        export_id = _provenance_text(row["export_id"], "training export id")
        context_label = f"training export {export_id}"
        row_subject = _provenance_text(row["subject_id"], f"{context_label} subject")
        export_format = _provenance_text(row["format"], f"{context_label} format")
        manifest_hash = _provenance_hash(row["manifest_hash"], f"{context_label} manifest hash")
        row_count = _provenance_int(row["row_count"], f"{context_label} row count")
        byte_size = _provenance_int(row["byte_size"], f"{context_label} byte size")
        consent_version = _provenance_int(
            row["consent_version"], f"{context_label} consent version", minimum=1
        )
        created_at = _provenance_time(row["created_at"], f"{context_label} created_at")
        if (
            row_subject != subject_id
            or export_format != "jsonl-zip"
            or consent_version > policy_version
        ):
            raise IntegrityError(f"{context_label} ownership or state is invalid")
        exports[export_id] = {
            "manifest_hash": manifest_hash,
            "row_count": row_count,
            "byte_size": byte_size,
            "consent_version": consent_version,
            "created_at": created_at,
        }

    audit_rows = context.connection.execute(
        "SELECT a.*, referenced_call.subject_id AS referenced_call_subject_id, "
        "referenced_export.subject_id AS referenced_export_subject_id "
        "FROM audit_records a "
        "LEFT JOIN model_calls referenced_call ON "
        "a.action IN ('model_unknown_retry_authorized', 'model_unknown_retry_cancelled', "
        "'model_unknown_reconciled') "
        "AND referenced_call.call_id = CASE "
        "WHEN typeof(a.payload_json) = 'text' AND json_valid(a.payload_json) "
        "THEN json_extract(a.payload_json, '$.call_id') END "
        "LEFT JOIN training_exports referenced_export ON "
        "a.action IN ('training_exported', 'training_export_aborted') "
        "AND referenced_export.export_id = CASE "
        "WHEN typeof(a.payload_json) = 'text' AND json_valid(a.payload_json) "
        "THEN json_extract(a.payload_json, '$.export_id') END "
        "WHERE a.subject_id = ? OR referenced_call.subject_id = ? "
        "OR referenced_export.subject_id = ? "
        "ORDER BY a.occurred_at, a.audit_id",
        (subject_id, subject_id, subject_id),
    )
    policy_updates: list[dict[str, Any]] = []
    export_audits: dict[str, int] = {}
    audit_count = 0
    for row in audit_rows:
        audit_count += 1
        audit_id = _provenance_text(row["audit_id"], "audit record id")
        context_label = f"audit record {audit_id}"
        row_subject = _provenance_text(row["subject_id"], f"{context_label} subject")
        action = _provenance_text(row["action"], f"{context_label} action")
        _provenance_text(row["actor"], f"{context_label} actor")
        payload = _provenance_object(row["payload_json"], f"{context_label} payload")
        occurred_at = _provenance_time(row["occurred_at"], f"{context_label} occurred_at")
        if row_subject != subject_id or action not in _CORE_AUDIT_ACTIONS:
            raise IntegrityError(f"{context_label} ownership or action is invalid")

        if action == "integrity_safe_pause_fallback":
            _provenance_keys(payload, {"from", "to", "reason", "version"}, context_label)
            if (
                payload["from"] != "active"
                or payload["to"] != "paused"
                or not isinstance(payload["reason"], str)
                or not payload["reason"].strip()
            ):
                raise IntegrityError(f"{context_label} payload is invalid")
            _provenance_int(payload["version"], f"{context_label} version", minimum=1)
        elif action in {"model_unknown_retry_authorized", "model_unknown_retry_cancelled"}:
            _provenance_keys(payload, {"call_id", "reason"}, context_label)
            _provenance_text(payload["call_id"], f"{context_label} call id")
            _provenance_text(payload["reason"], f"{context_label} reason")
            if row["referenced_call_subject_id"] != subject_id:
                raise IntegrityError(f"{context_label} model call ownership is invalid")
        elif action == "model_unknown_reconciled":
            _provenance_keys(payload, {"call_id", "outcome", "reason"}, context_label)
            _provenance_text(payload["call_id"], f"{context_label} call id")
            _provenance_text(payload["reason"], f"{context_label} reason")
            if (
                payload["outcome"] not in {"succeeded", "failed"}
                or row["referenced_call_subject_id"] != subject_id
            ):
                raise IntegrityError(f"{context_label} model call ownership is invalid")
        elif action == "runtime_exported":
            _provenance_keys(
                payload,
                {"export_id", "archive_sha256", "table_count", "row_count"},
                context_label,
            )
            _provenance_text(payload["export_id"], f"{context_label} export id")
            _provenance_hash(payload["archive_sha256"], f"{context_label} archive hash")
            _provenance_int(payload["table_count"], f"{context_label} table count")
            _provenance_int(payload["row_count"], f"{context_label} row count")
        elif action == "training_export_aborted":
            _provenance_keys(
                payload,
                {
                    "export_id",
                    "reason",
                    "lease_policy_version",
                    "current_policy_version",
                    "staged_disposition",
                },
                context_label,
            )
            _provenance_text(payload["export_id"], f"{context_label} export id")
            _provenance_int(
                payload["lease_policy_version"],
                f"{context_label} lease policy version",
                minimum=1,
            )
            current_version = payload["current_policy_version"]
            if current_version is not None:
                _provenance_int(
                    current_version,
                    f"{context_label} current policy version",
                    minimum=1,
                )
            if (
                payload["reason"] != "training_policy_changed_before_publication"
                or payload["staged_disposition"] not in {"destroyed", "cleanup_failed"}
                or row["referenced_export_subject_id"] is not None
            ):
                raise IntegrityError(f"{context_label} aborted export state is invalid")
        elif action == "training_exported":
            _provenance_keys(
                payload,
                {"export_id", "archive_sha256", "row_count", "consent_version"},
                context_label,
            )
            export_id = _provenance_text(payload["export_id"], f"{context_label} export id")
            _provenance_hash(payload["archive_sha256"], f"{context_label} archive hash")
            row_count = _provenance_int(payload["row_count"], f"{context_label} row count")
            consent_version = _provenance_int(
                payload["consent_version"],
                f"{context_label} consent version",
                minimum=1,
            )
            export = exports.get(export_id)
            if (
                row["referenced_export_subject_id"] != subject_id
                or export is None
                or export["row_count"] != row_count
                or export["consent_version"] != consent_version
                or export["created_at"] != occurred_at
            ):
                raise IntegrityError(f"{context_label} training export linkage is invalid")
            export_audits[export_id] = export_audits.get(export_id, 0) + 1
        else:
            _provenance_keys(
                payload,
                {
                    "reason",
                    "changed",
                    "before",
                    "after",
                    "previous_policy_version",
                    "policy_version",
                    "effective_at",
                },
                context_label,
            )
            _provenance_text(payload["reason"], f"{context_label} reason")
            if not isinstance(payload["changed"], dict) or not payload["changed"]:
                raise IntegrityError(f"{context_label} policy change is invalid")
            changes = {str(key): value for key, value in payload["changed"].items()}
            if set(changes) - set(_TRAINING_POLICY_FIELDS):
                raise IntegrityError(f"{context_label} policy change is invalid")
            for field, value in changes.items():
                _provenance_json_bool(value, f"{context_label} changed {field}")
            if not isinstance(payload["before"], dict) or not isinstance(payload["after"], dict):
                raise IntegrityError(f"{context_label} policy snapshots are invalid")
            before = {str(key): value for key, value in payload["before"].items()}
            after = {str(key): value for key, value in payload["after"].items()}
            snapshot_keys = set(_TRAINING_POLICY_FIELDS) | {"policy_version", "updated_at"}
            _provenance_keys(before, snapshot_keys, f"{context_label} before")
            _provenance_keys(after, snapshot_keys, f"{context_label} after")
            for field in _TRAINING_POLICY_FIELDS:
                _provenance_json_bool(before[field], f"{context_label} before {field}")
                _provenance_json_bool(after[field], f"{context_label} after {field}")
            previous_version = _provenance_int(
                payload["previous_policy_version"],
                f"{context_label} previous policy version",
                minimum=1,
            )
            next_version = _provenance_int(
                payload["policy_version"],
                f"{context_label} policy version",
                minimum=2,
            )
            if (
                _provenance_int(
                    before["policy_version"],
                    f"{context_label} before policy version",
                    minimum=1,
                )
                != previous_version
                or _provenance_int(
                    after["policy_version"],
                    f"{context_label} after policy version",
                    minimum=2,
                )
                != next_version
                or next_version != previous_version + 1
            ):
                raise IntegrityError(f"{context_label} policy versions are invalid")
            before_updated_at = _provenance_time(
                before["updated_at"], f"{context_label} before updated_at"
            )
            after_updated_at = _provenance_time(
                after["updated_at"], f"{context_label} after updated_at"
            )
            effective_at = _provenance_time(
                payload["effective_at"], f"{context_label} effective_at"
            )
            if after_updated_at != effective_at or occurred_at != effective_at:
                raise IntegrityError(f"{context_label} policy timestamps are invalid")
            policy_updates.append(
                {
                    "previous_version": previous_version,
                    "next_version": next_version,
                    "changes": changes,
                    "before": before,
                    "after": after,
                    "before_updated_at": before_updated_at,
                    "after_updated_at": after_updated_at,
                }
            )

    if any(export_audits.get(export_id, 0) != 1 for export_id in exports):
        raise IntegrityError("training export audit history is invalid")

    policy_updates.sort(key=lambda item: int(item["previous_version"]))
    tracked_flags = dict(_INITIAL_TRAINING_POLICY)
    tracked_version = 1
    tracked_updated_at = (
        policy_updated_at if not policy_updates else str(policy_updates[0]["before_updated_at"])
    )
    for update in policy_updates:
        before = update["before"]
        after = update["after"]
        if (
            update["previous_version"] != tracked_version
            or before["policy_version"] != tracked_version
            or before["updated_at"] != tracked_updated_at
            or any(before[field] != tracked_flags[field] for field in _TRAINING_POLICY_FIELDS)
        ):
            raise IntegrityError("training policy audit chain is invalid")
        expected_flags = dict(tracked_flags)
        expected_flags.update(update["changes"])
        if any(after[field] != expected_flags[field] for field in _TRAINING_POLICY_FIELDS):
            raise IntegrityError("training policy audit chain is invalid")
        tracked_flags = expected_flags
        tracked_version = int(update["next_version"])
        tracked_updated_at = str(update["after_updated_at"])
    if (
        tracked_version != policy_version
        or tracked_updated_at != policy_updated_at
        or tracked_flags != policy_flags
    ):
        raise IntegrityError("training policy current state does not match its audit history")

    return {
        "audit_records": audit_count,
        "training_policies": 1,
        "training_records": record_count,
        "training_exports": export_count,
    }


def _check_actions(context: IntegrityContext) -> object:
    from .actions import ActionLedger

    details = ActionLedger(context.database).verify_integrity(context.subject_id)
    return {**details, **_verify_core_provenance(context)}


def _check_mind(context: IntegrityContext) -> object:
    from noyra.mind.engine import MindEngine

    return MindEngine(context.database).verify_integrity(context.subject_id)


def _check_memory_blocks(context: IntegrityContext) -> object:
    from noyra.mind.blocks import MemoryBlockStore

    return MemoryBlockStore(context.database).verify_integrity(context.subject_id)


def _check_entities(context: IntegrityContext) -> object:
    from noyra.mind.entities import EntityStore

    return EntityStore(context.database).verify_integrity(context.subject_id)


def _check_memory_lifecycle(context: IntegrityContext) -> object:
    from noyra.mind.memory import MemoryStore

    return MemoryStore(context.database).verify_lifecycle_integrity(context.subject_id)


def _check_memory_embeddings(context: IntegrityContext) -> object:
    from noyra.mind.retrieval import MemoryEmbeddingIndex

    index = object.__new__(MemoryEmbeddingIndex)
    index.database = context.database
    index.provider = cast(Any, None)
    index.clock = utc_now
    return index.verify_integrity(context.subject_id)


def _check_sleep(context: IntegrityContext) -> object:
    from noyra.sleep.integrity import SleepIntegrity

    return SleepIntegrity(context.database).verify(context.subject_id)


def _check_interaction(context: IntegrityContext) -> object:
    from noyra.interaction.integrity import InteractionIntegrity

    return InteractionIntegrity(context.database).verify(context.subject_id)


def _read_bounded_file(
    path: Path,
    context: IntegrityContext,
    *,
    maximum: int | None = None,
    oversize_is_corruption: bool = False,
) -> bytes:
    context.checkpoint()
    value_limit = context.limits.max_value_bytes
    if maximum is not None:
        value_limit = min(value_limit, max(1, maximum))
    remaining = context.limits.max_bytes_per_check - context.budget.external_bytes
    if remaining <= 0:
        raise _IntegrityBudgetExceeded("byte_limit")
    read_limit = min(value_limit, remaining)
    context.budget.consume_file_entry()
    with path.open("rb") as stream:
        raw = stream.read(read_limit + 1)
    if len(raw) > read_limit:
        if read_limit < value_limit:
            raise _IntegrityBudgetExceeded("byte_limit")
        if oversize_is_corruption:
            raise IntegrityError("integrity secret file has an invalid size")
        raise _IntegrityBudgetExceeded("value_byte_limit")
    context.budget.consume_bytes(len(raw))
    return raw


def _check_secret_file_intents(context: IntegrityContext) -> Mapping[str, Any]:
    """Verify the durable secret journal's ownership and state contract."""
    specs = {
        "transport": ("interaction_transports", "transport_id", "secret_reference", "revoked"),
        "search": ("search_provider_configs", "config_id", "key_reference", "revoked"),
        "cognitive": ("cognitive_resource_keys", "key_id", "key_reference", "revoked"),
        "embedding": ("embedding_resources", "config_id", "__derived__", "revoked"),
    }
    rows = context.connection.execute(
        "SELECT * FROM secret_file_intents ORDER BY subject_id, intent_id"
    )
    subject_count = 0
    intent_reference_owners: dict[str, tuple[str, str, str]] = {}
    for row in rows:
        _archive_budget_checkpoint(context)
        intent_id = _provenance_text(row["intent_id"], "secret intent ID")
        subject_id = _provenance_text(row["subject_id"], "secret intent subject ID")
        resource_type = row["resource_type"]
        if resource_type not in specs:
            raise IntegrityError(f"secret intent resource type is invalid: {intent_id}")
        resource_id = _provenance_text(row["resource_id"], "secret intent resource ID")
        reference = row["secret_reference"]
        if not isinstance(reference, str):
            raise IntegrityError(f"secret intent reference is invalid: {intent_id}")
        try:
            SecretCleanupQueue._validate_intent_key(resource_type, resource_id, reference)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"secret intent reference is invalid: {intent_id}") from error
        intent_owner = (subject_id, str(resource_type), resource_id)
        prior_intent_owner = intent_reference_owners.setdefault(reference, intent_owner)
        if prior_intent_owner != intent_owner:
            raise IntegrityError(f"secret intent reference aliases another intent: {intent_id}")
        operation = row["operation"]
        state = row["state"]
        if operation not in {"create", "delete"} or state not in {
            "prepared",
            "file_ready",
            "committed",
            "pending",
            "failed",
            "removed",
        }:
            raise IntegrityError(f"secret intent operation or state is invalid: {intent_id}")
        if operation == "create" and state == "pending":
            raise IntegrityError(f"create secret intent state is invalid: {intent_id}")
        if operation == "delete" and state in {"file_ready", "committed"}:
            raise IntegrityError(f"delete secret intent state is invalid: {intent_id}")
        fingerprint = row["fingerprint"]
        if fingerprint is not None:
            _provenance_hash(fingerprint, "secret intent fingerprint")
        table, id_column, reference_column, revoked_status = specs[resource_type]
        resource = context.connection.execute(
            f"SELECT subject_id, status, "
            f"{reference_column if reference_column != '__derived__' else id_column} AS ref_value "
            f"FROM {table} WHERE {id_column} = ?",
            (resource_id,),
        ).fetchone()
        if resource is not None and resource["subject_id"] != subject_id:
            raise IntegrityError(f"secret intent crosses subject boundary: {intent_id}")
        expected_reference = (
            f"{resource_id}.key"
            if reference_column == "__derived__"
            else (None if resource is None else resource["ref_value"])
        )
        if resource is not None and expected_reference != reference:
            raise IntegrityError(f"secret intent resource reference mismatch: {intent_id}")
        owner_queries = (
            (
                "transport",
                "SELECT transport_id AS resource_id, subject_id, status "
                "FROM interaction_transports WHERE secret_reference = ?",
            ),
            (
                "search",
                "SELECT config_id AS resource_id, subject_id, status "
                "FROM search_provider_configs WHERE key_reference = ?",
            ),
            (
                "cognitive",
                "SELECT key_id AS resource_id, subject_id, status "
                "FROM cognitive_resource_keys WHERE key_reference = ?",
            ),
            (
                "embedding",
                "SELECT config_id AS resource_id, subject_id, status "
                "FROM embedding_resources WHERE config_id || '.key' = ?",
            ),
        )
        for owner_type, owner_query in owner_queries:
            owner_rows = context.connection.execute(owner_query, (reference,))
            for owner in owner_rows:
                if (
                    owner_type != resource_type
                    or str(owner["resource_id"]) != resource_id
                    or str(owner["subject_id"]) != subject_id
                ):
                    raise IntegrityError(
                        f"secret intent reference aliases another resource: {intent_id}"
                    )
        if operation == "delete" and resource is not None and resource["status"] != revoked_status:
            raise IntegrityError(f"delete intent targets active resource: {intent_id}")
        if operation == "create" and state == "committed" and resource is None:
            raise IntegrityError(f"committed create intent has no resource: {intent_id}")
        if operation == "create" and state == "removed" and resource is not None:
            raise IntegrityError(f"removed create intent has a resource: {intent_id}")
        if subject_id == context.subject_id:
            subject_count += 1
    return {"secret_file_intents": subject_count}


def _check_transport(context: IntegrityContext) -> Mapping[str, Any]:
    from noyra.interaction.transport import (
        DeliveryDispatcher,
        TransportChannel,
        TransportInput,
        TransportStore,
    )

    intent_details = _check_secret_file_intents(context)

    store = object.__new__(TransportStore)
    store.database = context.database
    store.secret_dir = (context.layout.root / "secrets" / "transports").resolve()
    rows = context.connection.execute(
        "SELECT * FROM interaction_transports WHERE subject_id = ? ORDER BY channel, label",
        (context.subject_id,),
    )
    transport_count = 0
    for row in rows:
        transport_count += 1
        try:
            store._record(row)
        except IntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError("transport durable state is invalid") from error
        if row["status"] == "revoked":
            continue
        reference = row["secret_reference"]
        transport_id = str(row["transport_id"])
        channel = str(row["channel"])
        if not isinstance(reference, str) or reference != f"{transport_id}.json":
            raise IntegrityError(
                f"transport secret reference is invalid: {transport_id} ({channel})"
            )
        try:
            secret_path = store._secret_path(reference)
        except ValueError as error:
            raise IntegrityError("transport secret reference is invalid") from error
        raw = _read_bounded_file(secret_path, context)
        try:
            payload = strict_json_loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError) as error:
            raise IntegrityError("transport secret payload is invalid") from error
        endpoint = payload.get("endpoint") if isinstance(payload, dict) else None
        credentials = payload.get("credentials") if isinstance(payload, dict) else None
        if not isinstance(endpoint, str) or not isinstance(credentials, dict):
            raise IntegrityError("transport secret payload is invalid")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in credentials.items()
        ):
            raise IntegrityError("transport secret credentials are invalid")
        if row["endpoint_contract"] == "legacy_origin":
            try:
                legacy_origin = TransportStore._legacy_endpoint_origin(endpoint, channel)
            except (TypeError, ValueError) as error:
                raise IntegrityError(
                    f"legacy transport secret endpoint is invalid: {transport_id} ({channel})"
                ) from error
            if not isinstance(row["endpoint"], str) or legacy_origin != row["endpoint"]:
                raise IntegrityError(
                    f"legacy transport secret endpoint origin mismatch: {transport_id} ({channel})"
                )
            # Current policy incompatibilities are recoverable at the
            # post-integrity migration boundary, where this single transport
            # is atomically revoked and its secret is scheduled for deletion.
            continue
        try:
            normalized_endpoint = TransportInput.normalize_endpoint(endpoint)
            TransportInput(
                channel=cast(TransportChannel, str(row["channel"])),
                label="integrity endpoint validation",
                endpoint=normalized_endpoint,
            )
        except ValueError as error:
            raise IntegrityError("transport secret endpoint is invalid") from error
        if not TransportStore._endpoint_matches_durable(
            str(row["endpoint"]),
            normalized_endpoint,
            str(row["endpoint_contract"]),
            row["endpoint_digest"],
        ):
            raise IntegrityError("transport secret endpoint does not match durable state")
    deliveries = context.connection.execute(
        "SELECT d.*, i.subject_id AS interaction_subject_id, "
        "t.subject_id AS transport_subject_id FROM interaction_deliveries d "
        "LEFT JOIN interactions i ON i.interaction_id = d.interaction_id "
        "LEFT JOIN interaction_transports t ON t.transport_id = d.transport_id "
        "WHERE d.subject_id = ? ORDER BY d.created_at, d.delivery_id",
        (context.subject_id,),
    )
    delivery_states = {"queued", "sending", "delivered", "failed", "unknown", "cancelled"}
    delivery_count = 0
    for delivery in deliveries:
        delivery_count += 1
        try:
            attempts = strict_int(delivery["attempts"])
        except (TypeError, ValueError) as error:
            raise IntegrityError(
                f"transport delivery attempts are invalid: {delivery['delivery_id']}"
            ) from error
        if (
            attempts < 0
            or delivery["status"] not in delivery_states
            or delivery["interaction_subject_id"] != context.subject_id
            or delivery["transport_subject_id"] != context.subject_id
            or any(
                not isinstance(delivery[field], str) or not delivery[field]
                for field in (
                    "delivery_id",
                    "interaction_id",
                    "transport_id",
                    "idempotency_key",
                    "created_at",
                    "updated_at",
                )
            )
        ):
            raise IntegrityError(
                f"transport delivery durable state is invalid: {delivery['delivery_id']}"
            )
    reconciliations = context.connection.execute(
        "SELECT r.*, d.status AS delivery_status, d.subject_id AS delivery_subject_id "
        "FROM interaction_delivery_reconciliations r "
        "LEFT JOIN interaction_deliveries d ON d.delivery_id = r.delivery_id "
        "WHERE r.subject_id = ? OR d.subject_id = ? ORDER BY r.delivery_id, r.sequence",
        (context.subject_id, context.subject_id),
    )
    sequences: dict[str, int] = {}
    terminal: set[str] = set()
    reconciliation_count = 0
    for row in reconciliations:
        reconciliation_count += 1
        record = DeliveryDispatcher._reconciliation_record(row)
        if (
            record.subject_id != context.subject_id
            or row["delivery_subject_id"] != context.subject_id
            or row["delivery_status"] != "unknown"
        ):
            raise IntegrityError("delivery reconciliation ownership or baseline is invalid")
        expected_sequence = sequences.get(record.delivery_id, 0) + 1
        if record.sequence != expected_sequence or record.delivery_id in terminal:
            raise IntegrityError("delivery reconciliation sequence is invalid")
        sequences[record.delivery_id] = record.sequence
        if record.outcome in {"delivered", "failed", "cancelled"}:
            terminal.add(record.delivery_id)
    return {
        **intent_details,
        "interaction_transports": transport_count,
        "interaction_deliveries": delivery_count,
        "interaction_delivery_reconciliations": reconciliation_count,
    }


def _check_capability(context: IntegrityContext) -> object:
    from noyra.capability.integrity import CapabilityIntegrity

    return CapabilityIntegrity(context.database).verify(context.subject_id)


def _check_wallet(context: IntegrityContext) -> object:
    from noyra.wallet import (
        WalletEconomyStore,
        WalletPaymentExecutionEngine,
        WalletRewardWorkflow,
        WalletStore,
    )

    details = WalletStore(context.database).verify_integrity(context.subject_id)
    details.update(WalletEconomyStore(context.database).verify_integrity(context.subject_id))
    details.update(
        WalletPaymentExecutionEngine.verify_database_integrity(context.database, context.subject_id)
    )
    details.update(WalletRewardWorkflow(context.database).verify_integrity(context.subject_id))
    return details


def _check_world(context: IntegrityContext) -> object:
    from noyra.world.integrity import WorldIntegrity

    return WorldIntegrity(
        context.database,
        observation_archive_root=context.layout.subject / "cold",
        archive_max_object_bytes=context.limits.max_value_bytes,
        archive_max_total_bytes=context.limits.max_bytes_per_check,
        archive_checkpoint=context.checkpoint,
        archive_consume_bytes=context.budget.consume_bytes,
    ).verify(context.subject_id)


def _check_outcomes(context: IntegrityContext) -> object:
    from noyra.learning.outcomes import OutcomeEvaluator

    return OutcomeEvaluator(context.database, context.subject_id).verify_integrity()


def _blank_subject_instance(cls: type[Any], context: IntegrityContext) -> Any:
    instance = object.__new__(cls)
    instance.database = context.database
    instance.subject_id = context.subject_id
    return instance


def _check_consciousness(context: IntegrityContext) -> object:
    from noyra.cognition.consciousness import ConsciousnessFrameStore

    return ConsciousnessFrameStore(context.database, context.subject_id).verify_integrity()


def _check_action_deliberation(context: IntegrityContext) -> object:
    from noyra.cognition.deliberation import ActionDeliberation

    return _blank_subject_instance(ActionDeliberation, context).verify_integrity()


def _check_project_executions(context: IntegrityContext) -> object:
    from noyra.cognition.execution import ProjectExecutionLedger

    return ProjectExecutionLedger(context.database, context.subject_id).verify_integrity()


def _check_goal_governance(context: IntegrityContext) -> object:
    from noyra.cognition.governance import GoalGovernance

    return _blank_subject_instance(GoalGovernance, context).verify_integrity()


def _check_metacognition(context: IntegrityContext) -> object:
    from noyra.cognition.metacognition import MetacognitiveControl

    return _blank_subject_instance(MetacognitiveControl, context).verify_integrity()


def _check_motivation(context: IntegrityContext) -> object:
    from noyra.cognition.motivation import MotivationDevelopment

    return _blank_subject_instance(MotivationDevelopment, context).verify_integrity()


def _check_autonomous_projects(context: IntegrityContext) -> object:
    from noyra.cognition.projects import AutonomousProjectManager

    return _blank_subject_instance(AutonomousProjectManager, context).verify_integrity()


def _check_research(context: IntegrityContext) -> object:
    from noyra.cognition.research import AutonomousResearch

    return _blank_subject_instance(AutonomousResearch, context).verify_integrity()


def _check_self_model(context: IntegrityContext) -> object:
    from noyra.cognition.self_model import OperationalSelfModel

    return _blank_subject_instance(OperationalSelfModel, context).verify_integrity()


def _check_self_modification(context: IntegrityContext) -> object:
    from noyra.cognition.self_modification import ControlledSelfModification

    return _blank_subject_instance(ControlledSelfModification, context).verify_integrity()


def _check_thought(context: IntegrityContext) -> object:
    from noyra.cognition.thought import IntrinsicThought

    return _blank_subject_instance(IntrinsicThought, context).verify_integrity()


def _check_model_ledger(context: IntegrityContext) -> Mapping[str, Any]:
    from noyra.core.payload_codec import decompress_text
    from noyra.model.ledger import ATTEMPT_STATES, ModelLedger

    calls = context.connection.execute(
        "SELECT * FROM model_calls WHERE subject_id = ?", (context.subject_id,)
    )
    attempts = context.connection.execute(
        "SELECT a.* FROM model_attempts a WHERE a.subject_id = ?", (context.subject_id,)
    )
    call_records: dict[str, Any] = {}
    for row in calls:
        call_id = str(row["call_id"])
        raw_request = row["request_json"]
        if raw_request is not None and not isinstance(raw_request, str):
            raise IntegrityError(f"model request payload is invalid: {call_id}")
        raw_response = row["response_json"]
        if raw_response is not None and not isinstance(raw_response, str):
            raise IntegrityError(f"model response payload is invalid: {call_id}")
        try:
            request_json = decompress_text(raw_request)
            request = strict_json_loads(request_json) if request_json else None
        except (IntegrityError, PayloadLimitError):
            raise
        except (UnicodeError, ValueError, TypeError) as error:
            raise IntegrityError(f"model request payload is invalid: {call_id}") from error
        if request is not None and (
            not isinstance(request, dict) or content_hash(request) != row["request_hash"]
        ):
            raise IntegrityError(f"model request integrity failure: {call_id}")
        try:
            call_record = ModelLedger._call_from_row(row)
        except IntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(f"model call durable state is invalid: {call_id}") from error
        call_records[call_id] = call_record
    attempts_by_call: dict[str, list[Any]] = {}
    for row in attempts:
        attempt_id = str(row["attempt_id"])
        if row["status"] not in ATTEMPT_STATES:
            raise IntegrityError("model attempt ownership or status is invalid")
        try:
            attempt_record = ModelLedger._attempt_from_row(row)
        except IntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(f"model attempt durable state is invalid: {attempt_id}") from error
        if attempt_record.call_id not in call_records:
            raise IntegrityError("model attempt ownership or status is invalid")
        attempts_by_call.setdefault(attempt_record.call_id, []).append(attempt_record)

    for call_id, call_record in call_records.items():
        call_attempts = sorted(
            attempts_by_call.get(call_id, ()), key=lambda item: item.attempt_number
        )
        numbers = [attempt.attempt_number for attempt in call_attempts]
        if numbers != list(range(1, len(call_attempts) + 1)):
            raise IntegrityError(f"model attempt sequence is invalid: {call_id}")
        active = [
            attempt for attempt in call_attempts if attempt.status in {"authorized", "executing"}
        ]
        if len(active) > 1 or (active and active[0] is not call_attempts[-1]):
            raise IntegrityError(f"model attempt lifecycle is invalid: {call_id}")
        if active and call_record.status != "executing":
            raise IntegrityError(f"model call-attempt state is invalid: {call_id}")
        if call_record.status == "executing" and not call_attempts:
            raise IntegrityError(f"executing model call has no attempt: {call_id}")

    return {
        "model_calls": len(call_records),
        "model_attempts": sum(len(items) for items in attempts_by_call.values()),
    }


def _check_model_resources(context: IntegrityContext) -> object:
    from noyra.model.resources import CognitiveResourceStore

    store = object.__new__(CognitiveResourceStore)
    store.database = context.database
    store.secret_dir = (context.layout.root / "secrets" / "models").resolve()
    details = store.verify_integrity(context.subject_id)
    routing_details = store.verify_routing_integrity(context.subject_id)
    rows = context.connection.execute(
        "SELECT * FROM cognitive_resource_keys WHERE subject_id = ? AND status != 'revoked'",
        (context.subject_id,),
    )
    verified_secrets = 0
    for row in rows:
        context.checkpoint()
        try:
            path = store._secret_path(str(row["key_reference"]))
        except ValueError as error:
            raise IntegrityError("model resource secret reference is invalid") from error
        raw = _read_bounded_file(path, context)
        try:
            value = raw.decode("utf-8")
        except UnicodeError as error:
            raise IntegrityError("model resource secret is invalid") from error
        if content_hash({"api_key": value}) != row["key_fingerprint"]:
            raise IntegrityError("model resource secret fingerprint mismatch")
        verified_secrets += 1
    return {**details, **routing_details, "verified_secrets": verified_secrets}


def _check_embedding_resources(context: IntegrityContext) -> Mapping[str, Any]:
    from noyra.model.embedding_ledger import EmbeddingLedger
    from noyra.model.embedding_resources import EmbeddingResourceStore

    store = object.__new__(EmbeddingResourceStore)
    store.database = context.database
    store.secret_dir = (context.layout.root / "secrets" / "embedding").resolve()
    rows = context.connection.execute(
        "SELECT * FROM embedding_resources WHERE subject_id = ? "
        "ORDER BY created_at DESC, config_id DESC",
        (context.subject_id,),
    )
    verified_secrets = 0
    resource_count = 0
    for row in rows:
        context.checkpoint()
        record = store._from_row(row)
        resource_count += 1
        if record.status == "revoked":
            continue
        path = store._secret_path(record.config_id)
        raw = _read_bounded_file(path, context)
        try:
            value = raw.decode("utf-8")
        except UnicodeError as error:
            raise IntegrityError("embedding resource secret is invalid") from error
        if content_hash({"api_key": value}) != record.key_fingerprint:
            raise IntegrityError("embedding resource secret fingerprint mismatch")
        verified_secrets += 1
    ledger_details = EmbeddingLedger(context.database).verify_integrity(
        context.subject_id,
        connection=context.connection,
    )
    return {
        "embedding_resources": resource_count,
        "verified_secrets": verified_secrets,
        **ledger_details,
    }


def _check_common_knowledge(context: IntegrityContext) -> Mapping[str, Any]:
    from noyra.knowledge.common import CommonKnowledgePeerInput, CommonKnowledgeStore
    from noyra.knowledge.sync import COMMON_KNOWLEDGE_SYNC_PROTOCOL

    package_rows = context.connection.execute(
        "SELECT DISTINCT p.* FROM common_knowledge_packages p "
        "LEFT JOIN common_knowledge_imports i ON i.package_id = p.package_id "
        "WHERE p.publisher_subject_id = ? OR i.subject_id = ? ORDER BY p.package_id",
        (context.subject_id, context.subject_id),
    )
    trusted_rows = context.connection.execute("SELECT * FROM common_knowledge_trusted_keys")
    trusted_count = 0
    trusted = {
        str(row["key_id"]): (str(row["public_key"]), str(row["status"])) for row in trusted_rows
    }
    trusted_count = len(trusted)
    key_directory = context.layout.root / "secrets" / "common-knowledge"
    own_public_key: str | None = None
    own_key_id: str | None = None
    own_package = context.connection.execute(
        "SELECT 1 FROM common_knowledge_packages WHERE publisher_subject_id = ? LIMIT 1",
        (context.subject_id,),
    ).fetchone()
    if key_directory.is_dir() or own_package is not None:
        raw_key = _read_bounded_file(
            key_directory / "common-knowledge-ed25519.key",
            context,
            maximum=32,
            oversize_is_corruption=True,
        )
        if len(raw_key) != 32:
            raise IntegrityError("common knowledge signing key is invalid")
        private_key = Ed25519PrivateKey.from_private_bytes(raw_key)
        public = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        own_public_key = base64.urlsafe_b64encode(public).decode("ascii")
        own_key_id = content_hash(own_public_key)[:32]
    verified = 0
    package_count = 0
    for row in package_rows:
        package_count += 1
        if not isinstance(row["payload_json"], str):
            raise IntegrityError("common knowledge payload is invalid")
        try:
            payload = strict_json_loads(row["payload_json"])
        except (UnicodeError, ValueError, TypeError) as error:
            raise IntegrityError("common knowledge payload is invalid") from error
        if not isinstance(payload, dict) or content_hash(payload) != row["payload_hash"]:
            raise IntegrityError("common knowledge payload hash mismatch")
        if row["publisher_subject_id"] == context.subject_id:
            if own_public_key is None or own_key_id is None:
                raise IntegrityError("common knowledge signing key is unavailable")
            public_key: str = own_public_key
            if row["key_id"] != own_key_id:
                raise IntegrityError("common knowledge publisher key id mismatch")
        else:
            candidate = trusted.get(str(row["key_id"]))
            if candidate is None:
                raise IntegrityError("common knowledge trusted key is missing")
            public_key = candidate[0]
            if content_hash(public_key)[:32] != row["key_id"]:
                raise IntegrityError("common knowledge trusted key id mismatch")
        body = {
            "package_id": row["package_id"],
            "publisher_subject_id": row["publisher_subject_id"],
            "scope": row["scope"],
            "title": row["title"],
            "summary": row["summary"],
            "payload": payload,
            "payload_hash": row["payload_hash"],
            "key_id": row["key_id"],
            "version": _durable_int(row["version"], "common knowledge version is invalid"),
            "created_at": row["created_at"],
        }
        CommonKnowledgeStore._verify(public_key, body, str(row["signature"]))
        verified += 1
    import_rows = context.connection.execute(
        "SELECT i.*, p.*, provenance.provenance_id, provenance.envelope_json, "
        "provenance.envelope_hash, provenance.payload_hash AS provenance_payload_hash, "
        "provenance.signer_key_id, provenance.signature AS provenance_signature "
        "FROM common_knowledge_imports i "
        "JOIN common_knowledge_packages p ON p.package_id = i.package_id "
        "LEFT JOIN common_knowledge_import_provenance provenance "
        "ON provenance.import_id = i.import_id "
        "WHERE i.subject_id = ? ORDER BY i.imported_at, i.import_id",
        (context.subject_id,),
    )
    verified_imports = 0
    for row in import_rows:
        if row["provenance_id"] is None:
            if row["status"] == "accepted":
                raise IntegrityError("accepted common knowledge provenance is missing")
            continue
        if not isinstance(row["payload_json"], str):
            raise IntegrityError("common knowledge payload is invalid")
        payload = strict_json_loads(row["payload_json"])
        expected_envelope = {
            "package_id": row["package_id"],
            "publisher_subject_id": row["publisher_subject_id"],
            "scope": row["scope"],
            "title": row["title"],
            "summary": row["summary"],
            "payload": payload,
            "payload_hash": row["payload_hash"],
            "key_id": row["key_id"],
            "version": _durable_int(row["version"], "common knowledge version is invalid"),
            "created_at": row["created_at"],
            "signature": row["signature"],
        }
        if (
            row["envelope_json"] != canonical_json(expected_envelope)
            or row["envelope_hash"] != content_hash(expected_envelope)
            or row["provenance_payload_hash"] != row["payload_hash"]
            or row["signer_key_id"] != row["key_id"]
            or row["provenance_signature"] != row["signature"]
        ):
            raise IntegrityError("common knowledge import provenance mismatch")
        if row["status"] == "accepted":
            trusted_key = trusted.get(str(row["key_id"]))
            if row["publisher_subject_id"] != context.subject_id and (
                trusted_key is None or trusted_key[1] != "active"
            ):
                raise IntegrityError("accepted common knowledge key is revoked")
        verified_imports += 1
    invalid_import = context.connection.execute(
        "SELECT i.import_id FROM common_knowledge_imports i "
        "JOIN common_knowledge_packages p ON p.package_id = i.package_id "
        "WHERE i.subject_id = ? AND i.status = 'accepted' AND p.status != 'published' LIMIT 1",
        (context.subject_id,),
    ).fetchone()
    if invalid_import is not None:
        raise IntegrityError("accepted common knowledge is not published")

    # V41 keeps the synchronization and evaluation workflow as append-only,
    # subject-owned evidence.  Validate every durable hash and signature that
    # is present, while allowing legacy packages to be lazily backfilled by the
    # store on their next publication/discovery operation.
    version_rows = context.connection.execute(
        "SELECT * FROM common_knowledge_versions WHERE subject_id = ? "
        "ORDER BY publisher_subject_id, series_id, package_version, package_id",
        (context.subject_id,),
    )
    verified_versions = 0
    for row in version_rows:
        package = context.connection.execute(
            "SELECT package_id, publisher_subject_id, version, created_at "
            "FROM common_knowledge_packages WHERE package_id = ?",
            (row["package_id"],),
        ).fetchone()
        if package is None:
            raise IntegrityError("common knowledge version package is missing")
        package_version = _durable_int(package["version"], "common knowledge version is invalid")
        version = _durable_int(row["package_version"], "common knowledge version is invalid")
        publisher = str(row["publisher_subject_id"])
        series = str(row["series_id"])
        if (
            not publisher.strip()
            or not series.strip()
            or package["publisher_subject_id"] != publisher
            or package_version != version
            or row["created_at"] != package["created_at"]
        ):
            raise IntegrityError("common knowledge version metadata mismatch")
        predecessor_id = row["predecessor_package_id"]
        if predecessor_id is not None:
            predecessor = context.connection.execute(
                "SELECT publisher_subject_id, series_id, package_version "
                "FROM common_knowledge_versions WHERE subject_id = ? AND package_id = ?",
                (context.subject_id, predecessor_id),
            ).fetchone()
            if (
                predecessor is None
                or predecessor["publisher_subject_id"] != publisher
                or predecessor["series_id"] != series
                or _durable_int(
                    predecessor["package_version"],
                    "common knowledge predecessor version is invalid",
                )
                >= version
            ):
                raise IntegrityError("common knowledge version predecessor is invalid")
        values = {
            "package_id": row["package_id"],
            "subject_id": row["subject_id"],
            "publisher_subject_id": publisher,
            "series_id": series,
            "package_version": version,
            "predecessor_package_id": predecessor_id,
            "created_at": row["created_at"],
        }
        if row["state_hash"] != content_hash(values):
            raise IntegrityError("common knowledge version state hash mismatch")
        verified_versions += 1

    sync_event_rows = context.connection.execute(
        "SELECT * FROM common_knowledge_sync_events WHERE subject_id = ? "
        "ORDER BY sequence, event_id",
        (context.subject_id,),
    )
    verified_sync_events = 0
    expected_sequence = 1
    for row in sync_event_rows:
        sequence = _durable_int(row["sequence"], "common knowledge sync event sequence is invalid")
        if sequence != expected_sequence:
            raise IntegrityError("common knowledge sync event sequence mismatch")
        expected_sequence += 1
        try:
            event_document = strict_json_loads(row["event_json"])
        except (TypeError, ValueError, UnicodeError) as error:
            raise IntegrityError("common knowledge sync event JSON is invalid") from error
        if not isinstance(event_document, dict):
            raise IntegrityError("common knowledge sync event JSON is invalid")
        required_event_fields = {
            "protocol",
            "event_id",
            "publisher_subject_id",
            "sequence",
            "event_type",
            "package_id",
            "series_id",
            "package_version",
            "predecessor_package_id",
            "envelope",
            "envelope_hash",
            "key_id",
            "occurred_at",
        }
        if set(event_document) != required_event_fields:
            raise IntegrityError("common knowledge sync event fields are invalid")
        event_body = {field: event_document[field] for field in required_event_fields}
        if (
            event_document["event_id"] != row["event_id"]
            or event_document["publisher_subject_id"] != row["publisher_subject_id"]
            or event_document["sequence"] != row["sequence"]
            or event_document["event_type"] != row["event_type"]
            or event_document["package_id"] != row["package_id"]
            or event_document["key_id"] != row["key_id"]
            or event_document["occurred_at"] != row["occurred_at"]
            or event_document["protocol"] != COMMON_KNOWLEDGE_SYNC_PROTOCOL
            or event_document["publisher_subject_id"] != context.subject_id
            or content_hash(event_body) != row["event_hash"]
        ):
            raise IntegrityError("common knowledge sync event metadata mismatch")
        if own_public_key is None or own_key_id is None or row["key_id"] != own_key_id:
            raise IntegrityError("common knowledge sync event signing key mismatch")
        envelope = event_document["envelope"]
        if (
            not isinstance(envelope, dict)
            or content_hash(envelope) != event_document["envelope_hash"]
        ):
            raise IntegrityError("common knowledge sync event envelope hash mismatch")
        try:
            _proposal, package_body, _verified_envelope, _envelope_json, _envelope_hash = (
                CommonKnowledgeStore._normalize_envelope(envelope)
            )
        except (TypeError, ValueError, IntegrityError) as error:
            raise IntegrityError("common knowledge sync event envelope is invalid") from error
        package = context.connection.execute(
            "SELECT publisher_subject_id, key_id, version, status FROM common_knowledge_packages "
            "WHERE package_id = ?",
            (row["package_id"],),
        ).fetchone()
        if package is None:
            raise IntegrityError("common knowledge sync event package is missing")
        if (
            package_body["package_id"] != row["package_id"]
            or package_body["publisher_subject_id"] != context.subject_id
            or package_body["key_id"] != row["key_id"]
            or package_body["version"]
            != _durable_int(
                event_document["package_version"],
                "common knowledge sync event version is invalid",
            )
            or package["publisher_subject_id"] != context.subject_id
            or package["key_id"] != row["key_id"]
            or _durable_int(package["version"], "common knowledge package version is invalid")
            != package_body["version"]
        ):
            raise IntegrityError("common knowledge sync event package mismatch")
        predecessor = event_document["predecessor_package_id"]
        if predecessor is not None and not isinstance(predecessor, str):
            raise IntegrityError("common knowledge sync event predecessor is invalid")
        if event_document["event_type"] == "revoked" and package["status"] != "revoked":
            raise IntegrityError("common knowledge revoked event has a live package")
        CommonKnowledgeStore._verify(own_public_key, package_body, str(envelope["signature"]))
        CommonKnowledgeStore._verify(own_public_key, event_body, str(row["signature"]))
        verified_sync_events += 1

    peer_rows = context.connection.execute(
        "SELECT * FROM common_knowledge_peers WHERE subject_id = ? ORDER BY peer_id",
        (context.subject_id,),
    )
    verified_peers = 0
    verified_remote_events = 0
    for peer in peer_rows:
        try:
            peer_input = CommonKnowledgePeerInput(
                label=peer["label"],
                endpoint=peer["endpoint"],
                publisher_subject_id=peer["publisher_subject_id"],
                public_key=peer["public_key"],
                sync_interval_seconds=peer["sync_interval_seconds"],
            )
        except (TypeError, ValueError) as error:
            raise IntegrityError("common knowledge peer configuration is invalid") from error
        if (
            peer["subject_id"] != context.subject_id
            or peer["status"] not in {"active", "disabled"}
            or _durable_int(peer["cursor"], "common knowledge peer cursor is invalid") < 0
            or content_hash(peer_input.public_key)[:32] != peer["key_id"]
            or peer["state_hash"] != content_hash(CommonKnowledgeStore._peer_state_values(peer))
        ):
            raise IntegrityError("common knowledge peer state is invalid")
        trusted_peer_key = trusted.get(str(peer["key_id"]))
        if trusted_peer_key is None or trusted_peer_key[0] != peer_input.public_key:
            raise IntegrityError("common knowledge peer trusted key is missing")
        remote_rows = context.connection.execute(
            "SELECT * FROM common_knowledge_remote_events WHERE peer_id = ? "
            "AND subject_id = ? ORDER BY sequence, event_id",
            (peer["peer_id"], context.subject_id),
        )
        remote_sequence = 1
        last_remote_sequence: int | None = None
        for remote in remote_rows:
            sequence = _durable_int(
                remote["sequence"], "common knowledge remote event sequence is invalid"
            )
            if sequence != remote_sequence:
                raise IntegrityError("common knowledge remote event sequence mismatch")
            remote_sequence += 1
            last_remote_sequence = sequence
            try:
                remote_document = strict_json_loads(remote["event_json"])
            except (TypeError, ValueError, UnicodeError) as error:
                raise IntegrityError("common knowledge remote event JSON is invalid") from error
            if not isinstance(remote_document, dict):
                raise IntegrityError("common knowledge remote event JSON is invalid")
            if not all(
                field in remote_document
                for field in (
                    "protocol",
                    "event_id",
                    "publisher_subject_id",
                    "sequence",
                    "event_type",
                    "package_id",
                    "series_id",
                    "package_version",
                    "predecessor_package_id",
                    "envelope",
                    "envelope_hash",
                    "key_id",
                    "occurred_at",
                    "event_hash",
                    "signature",
                )
            ):
                raise IntegrityError("common knowledge remote event fields are invalid")
            event_fields = (
                "protocol",
                "event_id",
                "publisher_subject_id",
                "sequence",
                "event_type",
                "package_id",
                "series_id",
                "package_version",
                "predecessor_package_id",
                "envelope",
                "envelope_hash",
                "key_id",
                "occurred_at",
            )
            remote_body = {field: remote_document[field] for field in event_fields}
            if (
                remote_document["event_id"] != remote["event_id"]
                or remote_document["publisher_subject_id"] != remote["publisher_subject_id"]
                or remote_document["sequence"] != remote["sequence"]
                or remote_document["event_type"] != remote["event_type"]
                or remote_document["package_id"] != remote["package_id"]
                or remote_document["event_hash"] != remote["event_hash"]
                or remote_document["signature"] != remote["signature"]
                or remote_document["publisher_subject_id"] != peer["publisher_subject_id"]
                or remote_document["key_id"] != peer["key_id"]
                or remote["publisher_subject_id"] != peer["publisher_subject_id"]
                or remote["subject_id"] != context.subject_id
                or content_hash(remote_body) != remote["event_hash"]
            ):
                raise IntegrityError("common knowledge remote event metadata mismatch")
            envelope = remote_document["envelope"]
            if (
                not isinstance(envelope, dict)
                or content_hash(envelope) != remote_document["envelope_hash"]
            ):
                raise IntegrityError("common knowledge remote event envelope hash mismatch")
            try:
                _proposal, package_body, _verified_envelope, _envelope_json, _envelope_hash = (
                    CommonKnowledgeStore._normalize_envelope(envelope)
                )
            except (TypeError, ValueError, IntegrityError) as error:
                raise IntegrityError("common knowledge remote event envelope is invalid") from error
            if (
                package_body["package_id"] != remote["package_id"]
                or package_body["publisher_subject_id"] != peer["publisher_subject_id"]
                or package_body["key_id"] != peer["key_id"]
                or package_body["version"]
                != _durable_int(
                    remote_document["package_version"],
                    "common knowledge remote event version is invalid",
                )
            ):
                raise IntegrityError("common knowledge remote event package mismatch")
            package = context.connection.execute(
                "SELECT publisher_subject_id, key_id, version FROM common_knowledge_packages "
                "WHERE package_id = ?",
                (remote["package_id"],),
            ).fetchone()
            if package is None or package["publisher_subject_id"] != peer["publisher_subject_id"]:
                raise IntegrityError("common knowledge remote event package is missing")
            CommonKnowledgeStore._verify(
                peer_input.public_key, package_body, str(envelope["signature"])
            )
            CommonKnowledgeStore._verify(
                peer_input.public_key, remote_body, str(remote_document["signature"])
            )
            verified_remote_events += 1
        if (
            last_remote_sequence is not None
            and _durable_int(peer["cursor"], "common knowledge peer cursor is invalid")
            < last_remote_sequence
        ):
            raise IntegrityError("common knowledge peer cursor is behind remote events")
        verified_peers += 1

    sync_run_rows = context.connection.execute(
        "SELECT * FROM common_knowledge_sync_runs WHERE subject_id = ? "
        "ORDER BY occurred_at, sync_id",
        (context.subject_id,),
    )
    verified_sync_runs = 0
    for run in sync_run_rows:
        peer = context.connection.execute(
            "SELECT 1 FROM common_knowledge_peers WHERE peer_id = ? AND subject_id = ?",
            (run["peer_id"], context.subject_id),
        ).fetchone()
        if peer is None:
            raise IntegrityError("common knowledge sync run peer is missing")
        cursor_before = _durable_int(
            run["cursor_before"], "common knowledge sync run cursor is invalid"
        )
        cursor_after = _durable_int(
            run["cursor_after"], "common knowledge sync run cursor is invalid"
        )
        discovered = _durable_int(run["discovered"], "common knowledge sync run count is invalid")
        imported_count = _durable_int(run["imported"], "common knowledge sync run count is invalid")
        revoked_count = _durable_int(run["revoked"], "common knowledge sync run count is invalid")
        if (
            cursor_before < 0
            or cursor_after < cursor_before
            or imported_count + revoked_count > discovered
            or run["outcome"] not in {"succeeded", "partial", "unchanged", "failed"}
            or (run["outcome"] != "failed" and run["error_code"] is not None)
            or (run["outcome"] == "failed" and not str(run["error_code"] or "").strip())
        ):
            raise IntegrityError("common knowledge sync run metadata is invalid")
        values = {
            "sync_id": run["sync_id"],
            "peer_id": run["peer_id"],
            "subject_id": run["subject_id"],
            "outcome": run["outcome"],
            "cursor_before": cursor_before,
            "cursor_after": cursor_after,
            "discovered": discovered,
            "imported": imported_count,
            "revoked": revoked_count,
            "error_code": run["error_code"],
            "occurred_at": run["occurred_at"],
        }
        if run["state_hash"] != content_hash(values):
            raise IntegrityError("common knowledge sync run state hash mismatch")
        verified_sync_runs += 1

    evaluation_rows = context.connection.execute(
        "SELECT * FROM common_knowledge_evaluation_events WHERE subject_id = ? "
        "ORDER BY evaluation_id, sequence",
        (context.subject_id,),
    )
    evaluations: dict[str, list[Any]] = {}
    verified_evaluation_events = 0
    for event in evaluation_rows:
        package = context.connection.execute(
            "SELECT 1 FROM common_knowledge_imports WHERE package_id = ? AND subject_id = ?",
            (event["package_id"], context.subject_id),
        ).fetchone()
        if package is None:
            raise IntegrityError("common knowledge evaluation import is missing")
        sequence = _durable_int(
            event["sequence"], "common knowledge evaluation sequence is invalid"
        )
        if sequence not in {1, 2} or event["status"] not in {"requested", "accepted", "rejected"}:
            raise IntegrityError("common knowledge evaluation metadata is invalid")
        values = {
            "evaluation_event_id": event["evaluation_event_id"],
            "evaluation_id": event["evaluation_id"],
            "package_id": event["package_id"],
            "subject_id": event["subject_id"],
            "sequence": sequence,
            "status": event["status"],
            "envelope_hash": event["envelope_hash"],
            "reason": event["reason"],
            "occurred_at": event["occurred_at"],
        }
        if event["state_hash"] != content_hash(values) or not str(event["reason"]).strip():
            raise IntegrityError("common knowledge evaluation state hash mismatch")
        provenance = context.connection.execute(
            "SELECT envelope_hash FROM common_knowledge_import_provenance "
            "WHERE package_id = ? AND subject_id = ?",
            (event["package_id"], context.subject_id),
        ).fetchone()
        if provenance is None or provenance["envelope_hash"] != event["envelope_hash"]:
            raise IntegrityError("common knowledge evaluation provenance mismatch")
        evaluations.setdefault(str(event["evaluation_id"]), []).append(event)
        verified_evaluation_events += 1
    for _evaluation_id, events in evaluations.items():
        if len(events) not in {1, 2} or int(events[0]["sequence"]) != 1:
            raise IntegrityError("common knowledge evaluation sequence mismatch")
        if events[0]["status"] != "requested":
            raise IntegrityError("common knowledge evaluation request is invalid")
        if len(events) == 2:
            if int(events[1]["sequence"]) != 2 or events[1]["status"] not in {
                "accepted",
                "rejected",
            }:
                raise IntegrityError("common knowledge evaluation terminal event is invalid")
            if events[1]["package_id"] != events[0]["package_id"]:
                raise IntegrityError("common knowledge evaluation package changed")

    return {
        "packages": package_count,
        "verified_signatures": verified,
        "verified_import_provenance": verified_imports,
        "trusted_keys": trusted_count,
        "verified_versions": verified_versions,
        "verified_sync_events": verified_sync_events,
        "verified_peers": verified_peers,
        "verified_remote_events": verified_remote_events,
        "verified_sync_runs": verified_sync_runs,
        "verified_evaluation_events": verified_evaluation_events,
    }


class IntegrityRuntimeController:
    """Persisted startup/periodic scheduler and alert-to-safe-pause policy."""

    def __init__(
        self,
        kernel: SubjectKernel,
        data_root: Path | str,
        *,
        policy_mode: IntegrityPolicyMode = "alert",
        interval_seconds: float = 300,
        retry_seconds: float = 60,
        startup_deadline_seconds: float = 10,
        periodic_deadline_seconds: float = 30,
        limits: IntegrityAuditLimits | None = None,
        registry: IntegrityRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        checkpoint: Callable[[], None] | None = None,
        report_retention: int = INTEGRITY_REPORT_RETENTION,
    ):
        if policy_mode not in {"off", "alert", "pause"}:
            raise ValueError("integrity policy mode is invalid")
        if (
            min(
                interval_seconds, retry_seconds, startup_deadline_seconds, periodic_deadline_seconds
            )
            <= 0
        ):
            raise ValueError("integrity scheduler intervals must be positive")
        if report_retention < 1:
            raise ValueError("integrity report retention must be positive")
        self.kernel = kernel
        self.layout = StorageLayout.create(data_root)
        self.policy_mode = policy_mode
        self.interval_seconds = interval_seconds
        self.retry_seconds = retry_seconds
        self.startup_deadline_seconds = startup_deadline_seconds
        self.periodic_deadline_seconds = periodic_deadline_seconds
        self.limits = limits or IntegrityAuditLimits()
        self._isolate_registry = registry is None
        self.registry = registry or IntegrityRegistry()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._checkpoint = checkpoint
        self.report_retention = report_retention
        self._lock = threading.Lock()
        subject_key = content_hash({"subject_id": self.kernel.subject_id})[:32]
        self._directory = self.layout.subject / "integrity" / subject_key
        self._directory.mkdir(parents=True, exist_ok=True)
        self._state_path = self._directory / "watchdog-state.json"
        self._latest_path = self._directory / "latest-report.json"
        self._state = self._load_state()
        self._last_report = self._load_latest_report()
        if self._last_report is not None:
            if not self._active_entries() and self._last_report.findings:
                self._seed_active_findings(self._last_report)
            self._last_report = self._with_active_findings(self._last_report)

    @property
    def latest_report(self) -> IntegrityReport | None:
        return self._last_report

    def run_startup(self) -> IntegrityReport | None:
        if self.policy_mode == "off":
            return None
        with self._lock:
            self._require_ownership()
            upgrade_pending = bool(self._state.get("registry_upgrade_pending", False))
            profile: IntegrityProfile = "manual" if upgrade_pending else "startup_light"
            checks = self.registry.profile_checks(profile)
            report = self._run_registry(
                self.kernel.database,
                self.kernel.subject_id,
                self.layout.root,
                profile=profile,
                policy_mode=self.policy_mode,
                deadline_seconds=self.startup_deadline_seconds,
                limits=self.limits,
                selection={
                    "shard_index": 0,
                    "shard_count": 1,
                    "check_ids": [check.check_id for check in checks],
                    "next_cursor": int(self._state["next_cursor"]),
                },
                checkpoint=self._checkpoint,
                monotonic=self._monotonic,
            )
            if (
                upgrade_pending
                and len(report.checks) == len(checks)
                and all(result.status != "incomplete" for result in report.checks)
            ):
                self._state["active_findings"] = {}
                self._state["registry_upgrade_pending"] = False
            incomplete = any(result.status == "incomplete" for result in report.checks)
            delay = self.retry_seconds if incomplete else self.interval_seconds
            self._state["next_due_at"] = self._format_time(self._now() + timedelta(seconds=delay))
            return self._commit_report(report)

    def run_periodic_if_due(self, *, force: bool = False) -> IntegrityReport | None:
        if self.policy_mode == "off":
            return None
        with self._lock:
            self._require_ownership()
            pending = self._apply_pending_pause()
            if pending is not None:
                return pending
            if not force and self.seconds_until_due() > 0:
                return None
            checks = self.registry.profile_checks("periodic_deep")
            if not checks:
                raise RuntimeError("periodic integrity registry is empty")
            cursor = int(self._state["next_cursor"]) % len(checks)
            selected = checks[cursor]
            next_cursor = (cursor + 1) % len(checks)
            report = self._run_registry(
                self.kernel.database,
                self.kernel.subject_id,
                self.layout.root,
                profile="periodic_deep",
                policy_mode=self.policy_mode,
                deadline_seconds=self.periodic_deadline_seconds,
                limits=self.limits,
                check_ids=(selected.check_id,),
                selection={
                    "shard_index": cursor,
                    "shard_count": len(checks),
                    "check_ids": [selected.check_id],
                    "next_cursor": next_cursor,
                },
                checkpoint=self._checkpoint,
                monotonic=self._monotonic,
            )
            if any(result.status == "incomplete" for result in report.checks):
                retries = int(self._state.get("incomplete_retries", 0)) + 1
                advance = retries >= INTEGRITY_SHARD_RETRY_LIMIT
                self._state["incomplete_retries"] = 0 if advance else retries
                self._state["next_cursor"] = next_cursor if advance else cursor
                delay = self.retry_seconds
                report = replace(
                    report,
                    selection={
                        **dict(report.selection),
                        "incomplete_retry": retries,
                        "next_cursor": self._state["next_cursor"],
                    },
                )
            else:
                self._state["next_cursor"] = next_cursor
                self._state["incomplete_retries"] = 0
                delay = self.interval_seconds
            self._state["next_due_at"] = self._format_time(self._now() + timedelta(seconds=delay))
            return self._commit_report(report)

    def _run_registry(
        self,
        database: Database,
        subject_id: str,
        data_root: Path | str,
        **kwargs: Any,
    ) -> IntegrityReport:
        runner = self.registry.run_isolated if self._isolate_registry else self.registry.run
        return runner(database, subject_id, data_root, **kwargs)

    def seconds_until_due(self) -> float:
        if self.policy_mode == "off":
            return self.interval_seconds
        raw = self._state.get("next_due_at")
        if not isinstance(raw, str) or not raw:
            return 0.0
        try:
            due = datetime.fromisoformat(raw).astimezone(UTC)
        except ValueError:
            return 0.0
        return max(0.0, (due - self._now()).total_seconds())

    def summary(self) -> dict[str, Any]:
        active = self._active_entries()
        findings = self._active_findings(active)
        blocking = bool(findings) and self.policy_mode == "pause"
        lifecycle = self._lifecycle_state() if blocking else None
        return {
            "registry_version": self.registry.version,
            "policy_mode": self.policy_mode,
            "status": self._active_status(active),
            "last_completed_at": self._state.get("last_completed_at"),
            "next_due_at": self._state.get("next_due_at"),
            "next_cursor": int(self._state.get("next_cursor", 0)),
            "pause_pending": bool(
                self._state.get("pause_pending", False) or (blocking and lifecycle != "paused")
            ),
            "p0": sum(finding.severity == "p0" for finding in findings),
            "p1": sum(finding.severity == "p1" for finding in findings),
        }

    def _commit_report(self, report: IntegrityReport) -> IntegrityReport:
        report = self._reconcile_active_findings(report)
        blocking = bool(report.findings)
        self._state["pause_pending"] = bool(blocking and self.policy_mode == "pause")
        self._persist(report)
        final = self._apply_policy(report)
        event_recorded = self._record_event(final)
        final = replace(final, action={**dict(final.action), "event_recorded": event_recorded})
        self._state["last_action"] = dict(final.action)
        self._persist(final)
        self._last_report = final
        return final

    def _reconcile_active_findings(self, report: IntegrityReport) -> IntegrityReport:
        active = self._active_entries()
        current_findings: dict[str, list[IntegrityFinding]] = {}
        for finding in report.findings:
            current_findings.setdefault(finding.check_id, []).append(finding)
        result_ids = {result.id for result in report.checks}
        for meta_check_id in _INTEGRITY_META_CHECK_IDS:
            if meta_check_id not in result_ids:
                active.pop(meta_check_id, None)
        for result in report.checks:
            if result.status == "ok":
                active.pop(result.id, None)
                continue
            findings = current_findings.get(result.id) or [
                IntegrityFinding(
                    result.id,
                    result.version,
                    cast(Literal["p0", "p1"], result.severity),
                    result.reason_code,
                )
            ]
            existing = active.get(result.id)
            existing_result = None if existing is None else existing.get("result")
            if (
                isinstance(existing_result, Mapping)
                and existing_result.get("severity") == "p0"
                and result.severity != "p0"
            ):
                continue
            active[result.id] = {
                "result": asdict(result),
                "findings": [asdict(finding) for finding in findings],
                "run_id": report.run_id,
                "completed_at": report.completed_at,
            }
        self._state["active_findings"] = active
        return self._with_active_findings(report)

    def _seed_active_findings(self, report: IntegrityReport) -> None:
        results = {result.id: result for result in report.checks}
        grouped: dict[str, list[IntegrityFinding]] = {}
        for finding in report.findings:
            grouped.setdefault(finding.check_id, []).append(finding)
        active: dict[str, dict[str, Any]] = {}
        for check_id, findings in grouped.items():
            result = results.get(check_id)
            if result is None:
                severity: IntegritySeverity = (
                    "p0" if any(finding.severity == "p0" for finding in findings) else "p1"
                )
                status: IntegrityStatus = (
                    "corrupt"
                    if severity == "p0"
                    else ("incomplete" if report.status == "incomplete" else "degraded")
                )
                result = IntegrityCheckResult(
                    id=check_id,
                    version=max(finding.check_version for finding in findings),
                    domain="persisted",
                    tier="deep",
                    status=status,
                    severity=severity,
                    reason_code=findings[0].reason_code,
                    duration_ms=0,
                    rows_examined=0,
                    bytes_examined=0,
                    details={},
                )
            active[check_id] = {
                "result": asdict(result),
                "findings": [asdict(finding) for finding in findings],
                "run_id": report.run_id,
                "completed_at": report.completed_at,
            }
        self._state["active_findings"] = active
        self._state["pause_pending"] = bool(active and self.policy_mode == "pause")

    def _with_active_findings(self, report: IntegrityReport) -> IntegrityReport:
        active = self._active_entries()
        findings = self._active_findings(active)
        summary = {
            **dict(report.summary),
            "p0": sum(finding.severity == "p0" for finding in findings),
            "p1": sum(finding.severity == "p1" for finding in findings),
            "active_checks": len(active),
        }
        return replace(
            report,
            status=self._active_status(active),
            findings=findings,
            summary=summary,
        )

    def _active_entries(self) -> dict[str, dict[str, Any]]:
        raw = self._state.get("active_findings")
        if not isinstance(raw, dict):
            return {}
        return {
            str(check_id): dict(entry)
            for check_id, entry in raw.items()
            if isinstance(check_id, str) and isinstance(entry, Mapping)
        }

    def _active_findings(
        self, entries: Mapping[str, Mapping[str, Any]] | None = None
    ) -> tuple[IntegrityFinding, ...]:
        findings: list[IntegrityFinding] = []
        active = self._active_entries() if entries is None else entries
        for _check_id, entry in sorted(active.items()):
            raw_findings = entry.get("findings")
            if not isinstance(raw_findings, list):
                continue
            for item in raw_findings:
                if not isinstance(item, Mapping):
                    continue
                findings.append(
                    IntegrityFinding(
                        check_id=str(item["check_id"]),
                        check_version=int(item["check_version"]),
                        severity=cast(Literal["p0", "p1"], item["severity"]),
                        reason_code=str(item["reason_code"]),
                    )
                )
        return tuple(findings)

    def _active_status(
        self, entries: Mapping[str, Mapping[str, Any]] | None = None
    ) -> IntegrityStatus:
        active = self._active_entries() if entries is None else entries
        statuses = {
            str(result.get("status"))
            for entry in active.values()
            if isinstance((result := entry.get("result")), Mapping)
        }
        severities = {
            str(result.get("severity"))
            for entry in active.values()
            if isinstance((result := entry.get("result")), Mapping)
        }
        if "corrupt" in statuses or "p0" in severities:
            return "corrupt"
        if "incomplete" in statuses:
            return "incomplete"
        if statuses:
            return "degraded"
        return "ok"

    def _apply_policy(self, report: IntegrityReport) -> IntegrityReport:
        before = self._lifecycle_state()
        if not report.findings:
            action = {
                "attempted": False,
                "result": "clean",
                "lifecycle_before": before,
                "lifecycle_after": before,
            }
            return replace(report, action=action)
        if self.policy_mode == "alert":
            action = {
                "attempted": False,
                "result": "alert_recorded",
                "lifecycle_before": before,
                "lifecycle_after": before,
            }
            return replace(report, action=action)
        if before == "unknown":
            self._state["pause_pending"] = True
            action = {
                "attempted": False,
                "result": "pause_failed",
                "error_type": "LifecycleStateUnavailable",
                "lifecycle_before": before,
                "lifecycle_after": before,
            }
            return replace(report, action=action)
        if before == "paused":
            self._state["pause_pending"] = False
            action = {
                "attempted": False,
                "result": "already_paused",
                "lifecycle_before": before,
                "lifecycle_after": before,
            }
            return replace(report, action=action)
        if before != "active":
            self._state["pause_pending"] = True
            action = {
                "attempted": False,
                "result": "pause_deferred",
                "lifecycle_before": before,
                "lifecycle_after": before,
            }
            return replace(report, action=action)
        try:
            paused = self.kernel.safe_pause("integrity policy requested safe pause")
        except Exception as error:
            self._state["pause_pending"] = True
            action = {
                "attempted": True,
                "result": "pause_failed",
                "error_type": type(error).__name__,
                "lifecycle_before": before,
                "lifecycle_after": self._lifecycle_state(),
            }
            return replace(report, action=action)
        after = self._lifecycle_state()
        pause_lost = after != "paused"
        self._state["pause_pending"] = pause_lost
        action = {
            "attempted": True,
            "result": "pause_lost" if pause_lost else "paused",
            "lifecycle_before": before,
            "lifecycle_after": after,
            "pause_result_state": paused.state,
        }
        return replace(report, action=action)

    def _apply_pending_pause(self) -> IntegrityReport | None:
        if self.policy_mode != "pause":
            return None
        active = self._active_entries()
        if not active:
            if self._state.get("pause_pending"):
                self._state["pause_pending"] = False
                self._persist_state()
            return None
        lifecycle = self._lifecycle_state()
        if lifecycle != "active":
            pending = lifecycle != "paused"
            if self._state.get("pause_pending") != pending:
                self._state["pause_pending"] = pending
                self._persist_state()
            return None
        if self._last_report is None:
            self._state["pause_pending"] = True
            self._persist_state()
            return None
        self._state["pause_pending"] = True
        updated = self._apply_policy(self._with_active_findings(self._last_report))
        updated = replace(
            updated,
            action={**dict(updated.action), "event_recorded": self._record_event(updated)},
        )
        self._persist(updated)
        self._last_report = updated
        return updated

    def _record_event(self, report: IntegrityReport) -> bool:
        try:
            EventStore(self.kernel.database).append(
                self.kernel.subject_id,
                "integrity_watchdog_report",
                "resilience_watchdog",
                {
                    "run_id": report.run_id,
                    "registry_version": report.registry_version,
                    "profile": report.profile,
                    "status": report.status,
                    "p0": report.summary["p0"],
                    "p1": report.summary["p1"],
                    "check_ids": [check.id for check in report.checks],
                    "action": dict(report.action),
                },
                privacy_level="private",
            )
        except Exception:
            return False
        return True

    def _persist(self, report: IntegrityReport) -> None:
        payload = report.to_dict()
        report_path = self._directory / f"{report.run_id}.json"
        _atomic_json(report_path, payload)
        _atomic_json(self._latest_path, payload)
        self._prune_report_history()
        self._state.update(
            {
                "format_version": INTEGRITY_WATCHDOG_STATE_VERSION,
                "registry_version": self.registry.version,
                "subject_id": self.kernel.subject_id,
                "last_run_id": report.run_id,
                "last_status": report.status,
                "last_completed_at": report.completed_at,
                "last_summary": dict(report.summary),
            }
        )
        self._persist_state()

    def _persist_state(self) -> None:
        _atomic_json(self._state_path, self._state)

    def _prune_report_history(self) -> None:
        retained: list[tuple[int, str, Path]] = []
        with os.scandir(self._directory) as entries:
            for entry in entries:
                if (
                    not entry.name.startswith("integrity-run_")
                    or not entry.name.endswith(".json")
                    or not entry.is_file(follow_symlinks=False)
                ):
                    continue
                path = Path(entry.path)
                key = (entry.stat(follow_symlinks=False).st_mtime_ns, entry.name, path)
                if len(retained) < self.report_retention:
                    heapq.heappush(retained, key)
                    continue
                if key[:2] > retained[0][:2]:
                    _, _, expired = heapq.heapreplace(retained, key)
                else:
                    expired = path
                expired.unlink(missing_ok=True)
                expired.with_suffix(expired.suffix + ".sha256").unlink(missing_ok=True)

    def _load_state(self) -> dict[str, Any]:
        default = {
            "format_version": INTEGRITY_WATCHDOG_STATE_VERSION,
            "registry_version": self.registry.version,
            "subject_id": self.kernel.subject_id,
            "next_cursor": 0,
            "incomplete_retries": 0,
            "next_due_at": None,
            "pause_pending": False,
            "registry_upgrade_pending": False,
            "last_status": "unknown",
            "last_completed_at": None,
            "last_summary": {"p0": 0, "p1": 0},
            "active_findings": {},
        }
        payload = _read_hashed_json(self._state_path)
        if (
            not isinstance(payload, dict)
            or payload.get("format_version") != INTEGRITY_WATCHDOG_STATE_VERSION
            or payload.get("subject_id") != self.kernel.subject_id
        ):
            return default
        registry_mismatch = payload.get("registry_version") != self.registry.version
        try:
            payload["next_cursor"] = max(0, int(payload.get("next_cursor", 0)))
            payload["incomplete_retries"] = max(0, int(payload.get("incomplete_retries", 0)))
        except (TypeError, ValueError):
            return default
        active = self._validate_active_entries(
            payload.get("active_findings"), allow_unknown=registry_mismatch
        )
        if active is None:
            return default
        payload["active_findings"] = active
        if not isinstance(payload.get("pause_pending"), bool):
            return default
        if not isinstance(payload.get("registry_upgrade_pending", False), bool):
            return default
        last_status = payload.get("last_status", "unknown")
        if last_status not in {*_INTEGRITY_STATUSES, "unknown"}:
            return default
        next_due_at = payload.get("next_due_at")
        if next_due_at is not None and not isinstance(next_due_at, str):
            return default
        if self.policy_mode != "pause":
            payload["pause_pending"] = False
        if registry_mismatch:
            payload["registry_version"] = self.registry.version
            payload["registry_upgrade_pending"] = True
            payload["next_cursor"] = 0
            payload["incomplete_retries"] = 0
            payload["next_due_at"] = None
            payload["pause_pending"] = bool(active and self.policy_mode == "pause")
        return {**default, **payload}

    def _load_latest_report(self) -> IntegrityReport | None:
        payload = _read_hashed_json(self._latest_path)
        if (
            not isinstance(payload, dict)
            or payload.get("format_version") != INTEGRITY_REPORT_FORMAT_VERSION
            or payload.get("registry_version") != self.registry.version
            or payload.get("subject_id") != self.kernel.subject_id
            or payload.get("profile") not in _INTEGRITY_PROFILES
            or payload.get("policy_mode") not in _INTEGRITY_POLICY_MODES
            or payload.get("status") not in _INTEGRITY_STATUSES
        ):
            return None
        try:
            raw_checks = payload["checks"]
            raw_findings = payload["findings"]
            raw_deferred = payload["deferred_coverage"]
            if not isinstance(raw_checks, list) or not isinstance(raw_findings, list):
                return None
            if not isinstance(raw_deferred, list):
                return None
            if (
                len(raw_checks) > len(self.registry.checks) + len(_INTEGRITY_META_CHECK_IDS)
                or len(raw_findings) > 4_096
            ):
                return None
            checks = tuple(self._check_result_from_payload(item) for item in raw_checks)
            findings = tuple(self._finding_from_payload(item) for item in raw_findings)
            summary = dict(cast(Mapping[str, Any], payload["summary"]))
            if (
                int(summary["checks"]) != len(checks)
                or int(summary["ok"]) != sum(check.status == "ok" for check in checks)
                or int(summary["p0"]) != sum(finding.severity == "p0" for finding in findings)
                or int(summary["p1"]) != sum(finding.severity == "p1" for finding in findings)
            ):
                return None
            status = cast(IntegrityStatus, payload["status"])
            if findings and any(finding.severity == "p0" for finding in findings):
                if status != "corrupt":
                    return None
            elif findings:
                if status not in {"degraded", "incomplete"}:
                    return None
            elif status != "ok":
                return None
            report = IntegrityReport(
                format_version=INTEGRITY_REPORT_FORMAT_VERSION,
                registry_version=self.registry.version,
                run_id=str(payload["run_id"]),
                subject_id=self.kernel.subject_id,
                profile=cast(IntegrityProfile, payload["profile"]),
                policy_mode=cast(IntegrityPolicyMode, payload["policy_mode"]),
                started_at=str(payload["started_at"]),
                completed_at=str(payload["completed_at"]),
                status=status,
                snapshot=dict(cast(Mapping[str, Any], payload["snapshot"])),
                selection=dict(cast(Mapping[str, Any], payload["selection"])),
                checks=checks,
                findings=findings,
                summary=summary,
                action=dict(cast(Mapping[str, Any], payload["action"])),
                deferred_coverage=tuple(
                    dict(cast(Mapping[str, str], item)) for item in raw_deferred
                ),
            )
            if not report.run_id or not report.started_at or not report.completed_at:
                return None
            return report
        except (KeyError, TypeError, ValueError):
            return None

    def _validate_active_entries(
        self, value: object, *, allow_unknown: bool = False
    ) -> dict[str, dict[str, Any]] | None:
        maximum = (
            256 if allow_unknown else len(self.registry.checks) + len(_INTEGRITY_META_CHECK_IDS)
        )
        if not isinstance(value, dict) or len(value) > maximum:
            return None
        allowed_ids = {check.check_id for check in self.registry.checks} | set(
            _INTEGRITY_META_CHECK_IDS
        )
        active: dict[str, dict[str, Any]] = {}
        try:
            for check_id, raw_entry in value.items():
                if (not allow_unknown and check_id not in allowed_ids) or not isinstance(
                    raw_entry, Mapping
                ):
                    return None
                result = self._check_result_from_payload(raw_entry["result"])
                raw_findings = raw_entry["findings"]
                if (
                    result.id != check_id
                    or result.status == "ok"
                    or result.severity not in {"p0", "p1"}
                    or not isinstance(raw_findings, list)
                    or not raw_findings
                    or len(raw_findings) > 128
                ):
                    return None
                findings = [self._finding_from_payload(item) for item in raw_findings]
                if any(finding.check_id != check_id for finding in findings):
                    return None
                run_id = raw_entry["run_id"]
                completed_at = raw_entry["completed_at"]
                if not isinstance(run_id, str) or not run_id:
                    return None
                if not isinstance(completed_at, str) or not completed_at:
                    return None
                active[check_id] = {
                    "result": asdict(result),
                    "findings": [asdict(finding) for finding in findings],
                    "run_id": run_id,
                    "completed_at": completed_at,
                }
        except (KeyError, TypeError, ValueError):
            return None
        return active

    @staticmethod
    def _check_result_from_payload(value: object) -> IntegrityCheckResult:
        if not isinstance(value, Mapping):
            raise TypeError("integrity check result must be an object")
        result = IntegrityCheckResult(
            id=str(value["id"]),
            version=int(value["version"]),
            domain=str(value["domain"]),
            tier=str(value["tier"]),
            status=cast(IntegrityStatus, value["status"]),
            severity=cast(IntegritySeverity, value["severity"]),
            reason_code=str(value["reason_code"]),
            duration_ms=int(value["duration_ms"]),
            rows_examined=int(value["rows_examined"]),
            bytes_examined=int(value["bytes_examined"]),
            details=dict(cast(Mapping[str, Any], value["details"])),
        )
        if (
            not result.id
            or result.version < 1
            or not result.domain
            or result.tier not in {"light", "deep"}
            or result.status not in _INTEGRITY_STATUSES
            or result.severity not in _INTEGRITY_SEVERITIES
            or not result.reason_code
            or min(result.duration_ms, result.rows_examined, result.bytes_examined) < 0
            or (result.status == "ok") != (result.severity == "none")
        ):
            raise ValueError("integrity check result is invalid")
        return result

    @staticmethod
    def _finding_from_payload(value: object) -> IntegrityFinding:
        if not isinstance(value, Mapping):
            raise TypeError("integrity finding must be an object")
        finding = IntegrityFinding(
            check_id=str(value["check_id"]),
            check_version=int(value["check_version"]),
            severity=cast(Literal["p0", "p1"], value["severity"]),
            reason_code=str(value["reason_code"]),
        )
        if (
            not finding.check_id
            or finding.check_version < 1
            or finding.severity not in {"p0", "p1"}
            or not finding.reason_code
        ):
            raise ValueError("integrity finding is invalid")
        return finding

    def _require_ownership(self) -> None:
        if not self.kernel.process_lock.held:
            raise RuntimeError("integrity runtime requires subject ownership")

    def _lifecycle_state(self) -> str:
        try:
            return self.kernel.lifecycle.current().state
        except Exception:
            return "unknown"

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("integrity scheduler clock requires a timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _format_time(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    temporary = path.with_name(f".{new_id('integrity-write')}.tmp")
    digest_path = path.with_suffix(path.suffix + ".sha256")
    digest_temporary = digest_path.with_name(f".{new_id('integrity-digest')}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        with digest_temporary.open("x", encoding="ascii") as stream:
            stream.write(content_hash(dict(payload)))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.replace(digest_temporary, digest_path)
    finally:
        temporary.unlink(missing_ok=True)
        digest_temporary.unlink(missing_ok=True)


def _read_hashed_json(path: Path) -> object | None:
    digest_path = path.with_suffix(path.suffix + ".sha256")
    try:
        raw = _read_bounded_bytes(path, 4_000_000)
        digest_raw = _read_bounded_bytes(digest_path, 128)
        payload = strict_json_loads(raw)
        expected = digest_raw.decode("ascii").strip()
        if len(expected) != 64:
            return None
        int(expected, 16)
    except (OSError, RecursionError, UnicodeError, ValueError, TypeError):
        return None
    try:
        matches = isinstance(payload, dict) and content_hash(payload) == expected
    except (RecursionError, TypeError, ValueError):
        return None
    if not matches:
        return None
    return cast(object, payload)


def _read_bounded_bytes(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("bounded integrity file exceeds its size limit")
    return raw
