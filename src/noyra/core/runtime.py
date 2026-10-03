from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from .actions import ActionLedger
from .admission import RuntimeAdmissionGate
from .database import Database
from .errors import IdentityConflictError, IntegrityError, NotFoundError, RuntimeOwnershipError
from .events import EventStore
from .identity import IdentityStore, validate_subject_id
from .lifecycle import LifecycleManager
from .locking import ProcessLock
from .snapshots import SnapshotStore
from .types import ActionRecord, RuntimeState, SnapshotRecord, SubjectIdentity, utc_now


def _migration_fence_active(fence_root: Path | None, subject_id: str) -> bool:
    """Return whether the root-owned migration fence blocks this subject.

    Fence markers are deliberately outside SQLite so a root migration runner
    can stop a source before the service can write another database row. Any
    malformed marker fails closed; an absent fence directory means the
    migration runner has not been installed.
    """
    if fence_root is None:
        return False
    fences = fence_root / "fences"
    if not fences.exists():
        return False
    if fences.is_symlink() or not fences.is_dir():
        return True
    try:
        markers = tuple(fences.glob("*.json"))
    except OSError:
        return True
    for marker in markers:
        if marker.is_symlink() or not marker.is_file():
            return True
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return True
        if not isinstance(value, dict):
            return True
        if value.get("status") == "active" and value.get("subject_id") == subject_id:
            return True
    return False


def _migration_epoch_guard(
    connection: Any, subject_id: str, fence_root: Path | None = None
) -> None:
    """Reject a source mutation while a target or completed epoch owns it.

    The check runs inside the caller's ``BEGIN IMMEDIATE`` transaction.  This
    makes epoch acquisition and source writes serialize at the SQLite writer
    boundary instead of relying on a check performed before the write.
    """
    row = connection.execute(
        "SELECT 1 FROM migration_epochs WHERE subject_id=? "
        "AND status IN ('active','completed') LIMIT 1",
        (subject_id,),
    ).fetchone()
    if row is not None:
        raise RuntimeOwnershipError("runtime ownership is fenced by a migration epoch")
    if _migration_fence_active(fence_root, subject_id):
        raise RuntimeOwnershipError("runtime mutation is blocked by a migration fence")


def _no_active_migration_epoch(
    database: Database, subject_id: str, fence_root: Path | None = None
) -> bool:
    """Fail closed when a durable target epoch owns this subject."""
    try:
        with database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM migration_epochs WHERE subject_id=? "
                "AND status IN ('active','completed') LIMIT 1",
                (subject_id,),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT 1 FROM migration_tasks t JOIN migration_epochs e "
                    "ON t.target_epoch_id=e.epoch_id WHERE t.subject_id=? "
                    "AND t.status='rolling_back' AND e.status='revoked' LIMIT 1",
                    (subject_id,),
                ).fetchone()
    except sqlite3.Error:
        return False
    return row is None and not _migration_fence_active(fence_root, subject_id)


class SubjectKernel:
    """The model-free, restart-safe kernel shared by every future cognition layer."""

    def __init__(
        self,
        database_path: Path | str,
        subject_id: str,
        genesis_hash: str,
        *,
        allow_subject_creation: bool = True,
        process_lock: ProcessLock | None = None,
        defer_preflight: bool = False,
        migration_fence_root: Path | str | None = None,
    ):
        validate_subject_id(subject_id)
        resolved_database_path = Path(database_path).resolve()
        self._migration_fence_root = (
            Path(migration_fence_root).resolve() if migration_fence_root is not None else None
        )
        self.process_lock = process_lock or ProcessLock(f"{resolved_database_path}.lock")
        self._genesis_hash = genesis_hash
        self._runtime_ready = False
        direct_probe = False
        if process_lock is None and not self.process_lock.held:
            # Hold the probe for the complete durable initialization.  The
            # previous implementation released it before Database() and
            # identity setup finished, allowing a second process to enter the
            # same database during construction.  Direct construction remains
            # a preflight helper, so the probe is released once initialization
            # has completed and normal ownership is acquired by boot().
            probe = ProcessLock(self.process_lock.path)
            try:
                probe.acquire()
            except RuntimeOwnershipError:
                self._preflight_only = True
            else:
                self.process_lock = probe
                direct_probe = True
                self._preflight_only = False
        else:
            self._preflight_only = process_lock is not None and not self.process_lock.held
        self._preflight_deferred = self._preflight_only and defer_preflight
        try:
            if self._preflight_only:
                self.database = Database(resolved_database_path, initialize=False, read_only=True)
            else:
                self.database = Database(resolved_database_path)
            self.subject_id = subject_id
            # Capture the lock object itself instead of ``self``.  Capturing
            # the kernel would create a reference cycle (kernel -> admission
            # -> callback -> kernel), which can leak the lock on Windows.
            process_lock_ref = self.process_lock
            database_ref = self.database
            self.admission = RuntimeAdmissionGate(
                subject_id,
                initially_accepting=False,
                ownership_check=lambda: (
                    process_lock_ref.held
                    and _no_active_migration_epoch(
                        database_ref, subject_id, self._migration_fence_root
                    )
                ),
                control_ownership_check=lambda: process_lock_ref.held,
                migration_clear_check=lambda: _no_active_migration_epoch(
                    database_ref, subject_id, self._migration_fence_root
                ),
            )
            self.identity_store = IdentityStore(self.database)
            self.event_store = EventStore(self.database)
            self.snapshot_store = SnapshotStore(self.database)
            self.action_ledger = ActionLedger(self.database)
            self.lifecycle = LifecycleManager(
                self.database,
                self.event_store,
                subject_id,
                mutation_guard=lambda connection: _migration_epoch_guard(connection, subject_id),
            )
            self.identity: SubjectIdentity | None
            if allow_subject_creation and not self._preflight_only:
                self.identity = self.identity_store.ensure(subject_id, genesis_hash)
                self.lifecycle.ensure_initial()
                self.verify_continuity()
            else:
                self.identity = None
                if not self._preflight_deferred:
                    self._load_preflight_state()
            self.recovered_actions: list[ActionRecord] = []
        except Exception:
            if direct_probe:
                self.process_lock.release()
            raise
        else:
            if direct_probe:
                self.process_lock.release()

    def _load_preflight_state(self) -> None:
        """Load read-only identity diagnostics when a caller explicitly asks for them."""
        with self.database.read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM subject_identity WHERE subject_id = ?", (self.subject_id,)
            ).fetchone()
        if row is None:
            self.identity = None
        else:
            if row["genesis_hash"] != self._genesis_hash or row["project_name"] != "Noyra":
                raise IdentityConflictError(
                    f"identity already exists with different genesis: {self.subject_id}"
                )
            try:
                self.identity = self.identity_store._from_row(row)
            except (IntegrityError, TypeError, ValueError):
                self.identity = None
        if self._preflight_only:
            self.verify_continuity()
        self._preflight_deferred = False

    def boot(self) -> RuntimeState:
        acquired = self.acquire_ownership()
        try:
            if self._preflight_only and self.process_lock.held:
                self.database.promote_to_writable()
                self._preflight_only = False
            self._runtime_ready = True
            return self.recover_after_integrity()
        except Exception:
            self._runtime_ready = False
            if acquired or self.process_lock.held:
                self.process_lock.release()
            raise

    def acquire_ownership(self) -> bool:
        """Acquire exclusive runtime ownership without mutating durable subject state."""
        return self.process_lock.acquire()

    def recover_after_integrity(self) -> RuntimeState:
        """Perform restart recovery only after the caller's startup integrity gate passes."""
        self._require_ownership()
        self._runtime_ready = True
        self.verify_continuity()
        self.identity = self.identity_store.load(self.subject_id)
        self.recovered_actions = self.action_ledger.recover_interrupted(self.subject_id)
        current = self.lifecycle.current()
        if current.state == "booting":
            state = current
        elif current.state == "stopped":
            state = self.lifecycle.transition("booting", "process start", actor="supervisor")
        else:
            state = self.lifecycle.recover_for_restart()
        self._sync_migration_source_epoch(state.version)
        return state

    def orient(self) -> RuntimeState:
        self._require_ownership()
        state = self.lifecycle.transition("orienting", "initial orientation", actor="supervisor")
        self._sync_migration_source_epoch(state.version)
        return state

    def activate(self) -> RuntimeState:
        self._require_ownership()
        state = self.lifecycle.transition("active", "runtime ready", actor="supervisor")
        self._sync_migration_source_epoch(state.version)
        self.admission.open(epoch=state.version)
        return state

    def pause(self, reason: str = "operator pause", *, actor: str = "operator") -> RuntimeState:
        self._require_ownership()
        self.admission.invalidate()
        current = self.lifecycle.current()
        if current.state == "paused":
            return current
        try:
            state = self.lifecycle.transition("paused", reason, actor=actor)
            self._sync_migration_source_epoch(state.version)
            return state
        except Exception:
            # The fencing increment is intentionally retained, but a failed
            # lifecycle transaction must not strand an otherwise active
            # runtime with admission permanently closed.
            with suppress(Exception):
                if self.lifecycle.current().state == "active":
                    self.admission.open()
            raise

    def safe_pause(self, reason: str = "integrity policy requested safe pause") -> RuntimeState:
        self._require_ownership()
        self.admission.quarantine()
        state = self.lifecycle.safe_pause(reason, actor="resilience_watchdog")
        self._sync_migration_source_epoch(state.version)
        return state

    def resume(
        self,
        reason: str = "operator resume",
        *,
        actor: str = "operator",
    ) -> RuntimeState:
        self._require_ownership()
        state = self.lifecycle.transition("active", reason, actor=actor)
        self._sync_migration_source_epoch(state.version)
        self.admission.open(epoch=state.version)
        return state

    def stop(self, reason: str = "operator stop") -> RuntimeState:
        self._require_ownership()
        self.admission.begin_drain()
        try:
            state = self.lifecycle.transition("stopped", reason, actor="operator")
            self._sync_migration_source_epoch(state.version)
            return state
        finally:
            self.close()

    def close(self) -> None:
        self.admission.begin_drain()
        self.admission.wait_for_drain()
        self.process_lock.release()
        self._runtime_ready = False

    def __enter__(self) -> SubjectKernel:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _require_ownership(self) -> None:
        if not self.process_lock.held:
            raise RuntimeOwnershipError("this kernel does not own the subject runtime")
        if not _no_active_migration_epoch(
            self.database, self.subject_id, self._migration_fence_root
        ):
            if _migration_fence_active(self._migration_fence_root, self.subject_id):
                raise RuntimeOwnershipError("runtime ownership is blocked by a migration fence")
            raise RuntimeOwnershipError("runtime ownership is fenced by a migration epoch")

    def _sync_migration_source_epoch(self, version: int) -> None:
        if self._migration_fence_root is None:
            return
        source_root = self._migration_fence_root / "source"
        source_root.mkdir(mode=0o750, parents=True, exist_ok=True)
        if source_root.is_symlink() or not source_root.is_dir():
            raise RuntimeOwnershipError("migration source epoch directory is invalid")
        path = source_root / "epoch"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeOwnershipError("migration source epoch file is invalid")
        value = f"runtime-{int(version)}\n".encode("ascii")
        descriptor, temporary = tempfile.mkstemp(prefix=".epoch.", dir=source_root)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except Exception:
            with suppress(OSError):
                os.close(descriptor)
            Path(temporary).unlink(missing_ok=True)
            raise

    def __del__(self) -> None:
        with suppress(Exception):
            self.admission.begin_drain()
            if self.admission.active_operations == 0:
                self.process_lock.release()

    def checkpoint(
        self, state: dict[str, Any], *, reason: str, model_name: str | None = None
    ) -> SnapshotRecord:
        self._require_ownership()
        if not reason.strip():
            raise ValueError("checkpoint reason is required")
        with self.database.transaction() as connection:
            _migration_epoch_guard(connection, self.subject_id, self._migration_fence_root)
            row = connection.execute(
                "SELECT state_version FROM subject_identity WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"identity not found: {self.subject_id}")
            next_version = int(row["state_version"]) + 1
            snapshot = self.snapshot_store._save_connection(
                connection,
                self.subject_id,
                state,
                state_version=next_version,
                reason=reason,
                snapshot_id=None,
            )
            self.identity_store._update_checkpoint_connection(
                connection,
                self.subject_id,
                next_version,
                snapshot.snapshot_id,
                model_name,
            )
            self.event_store._append_connection(
                connection,
                self.subject_id,
                "state_checkpoint",
                "supervisor",
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "state_version": next_version,
                    "reason": reason,
                },
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=utc_now(),
                event_id=None,
            )
        self.identity = self.identity_store.load(self.subject_id)
        return snapshot

    def verify_continuity(self) -> None:
        with self.database.read_transaction() as connection:
            identity_row = connection.execute(
                "SELECT * FROM subject_identity WHERE subject_id = ?", (self.subject_id,)
            ).fetchone()
            if identity_row is None:
                raise NotFoundError(f"identity not found: {self.subject_id}")
            identity = self.identity_store._from_row(identity_row)
            snapshot_rows = connection.execute(
                "SELECT * FROM state_snapshots WHERE subject_id = ? "
                "ORDER BY state_version DESC, created_at DESC, snapshot_id DESC LIMIT 2",
                (self.subject_id,),
            ).fetchall()
        if identity.state_version == 0:
            if identity.last_checkpoint is not None:
                raise IntegrityError("version-zero identity unexpectedly references a checkpoint")
            if snapshot_rows:
                raise IntegrityError("version-zero identity unexpectedly has snapshots")
            return
        if identity.last_checkpoint is None:
            raise IntegrityError("versioned identity has no checkpoint")
        if not snapshot_rows:
            raise IntegrityError("versioned identity has no stored snapshot")
        latest = self.snapshot_store._from_row(snapshot_rows[0])
        if (
            latest.snapshot_id != identity.last_checkpoint
            or latest.state_version != identity.state_version
        ):
            raise IntegrityError("identity and latest checkpoint do not describe one state")

    def health(self) -> dict[str, Any]:
        if self._preflight_deferred:
            self._load_preflight_state()
        return {
            "subject_id": self.subject_id,
            "identity_status": self.identity_store.load(self.subject_id).identity_status,
            "lifecycle": self.lifecycle.current().__dict__,
            "recoverable_actions": len(self.action_ledger.recoverable(self.subject_id)),
        }
