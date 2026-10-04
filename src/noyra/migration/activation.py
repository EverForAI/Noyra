"""Authenticated bridge and fixed-path systemd handoff for migration targets."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from noyra.core.database import Database
from noyra.core.types import canonical_json, content_hash

from .fencing import EpochLease
from .manager import MigrationManager
from .policy import MigrationStore

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}\Z")
_TARGET_ID = re.compile(r"[A-Za-z0-9_-]{3,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ACTIVATION_REQUEST_BYTES = 64 * 1024


class TargetActivationError(RuntimeError):
    """A bounded target activation failure with a non-sensitive error code."""

    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class TargetActivationBackend(Protocol):
    def activate(self, request: Mapping[str, Any]) -> dict[str, Any]: ...

    def deactivate(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class ActivationSystemd(Protocol):
    def stop(self) -> None: ...

    def start(self) -> None: ...

    def daemon_reload(self) -> None: ...

    def is_active(self) -> bool: ...

    def wait_ready(self, subject_id: str, target_id: str | None, timeout: float) -> bool: ...


class _Systemd:
    def __init__(self, data_root: Path):
        self.data_root = data_root

    @staticmethod
    def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["/usr/bin/systemctl", *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TargetActivationError("systemd_unavailable") from error
        return result

    def stop(self) -> None:
        if self._run("stop", "noyra.service").returncode:
            raise TargetActivationError("service_stop_failed")

    def start(self) -> None:
        if self._run("start", "noyra.service").returncode:
            raise TargetActivationError("service_start_failed")

    def daemon_reload(self) -> None:
        if self._run("daemon-reload").returncode:
            raise TargetActivationError("systemd_reload_failed")

    def is_active(self) -> bool:
        return self._run("is-active", "--quiet", "noyra.service").returncode == 0

    def wait_ready(self, subject_id: str, target_id: str | None, timeout: float) -> bool:
        port = _configured_port(Path("/etc/noyra/noyra.env"))
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self.is_active():
                try:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/health/ready",
                        headers={"Cache-Control": "no-cache"},
                    )
                    with urllib.request.urlopen(request, timeout=2) as response:
                        payload = json.loads(response.read(64 * 1024))
                    if (
                        response.status == 200
                        and isinstance(payload, dict)
                        and payload.get("status") == "ok"
                        and payload.get("subject_id") == subject_id
                        and payload.get("migration_target_id") == target_id
                    ):
                        return True
                except (OSError, TimeoutError, urllib.error.URLError, ValueError):
                    pass
            time.sleep(0.5)
        return False


class TargetRuntimeActivator:
    """Switch the fixed local Noyra database under a root-owned systemd boundary."""

    def __init__(
        self,
        data_root: Path | str = "/var/lib/noyra",
        config_root: Path | str = "/etc/noyra",
        *,
        systemd: ActivationSystemd | None = None,
        ready_timeout_seconds: float = 90,
        state_root: Path | str | None = None,
    ):
        self.data_root = Path(data_root).resolve()
        self.config_root = Path(config_root).resolve()
        self.systemd = systemd or _Systemd(self.data_root)
        self.ready_timeout_seconds = ready_timeout_seconds
        self.agent_root = self.data_root / "migration-agent"
        self.state_root = (
            Path(state_root).resolve()
            if state_root is not None
            else self.data_root / "migration" / "target-activation" / "state"
        )
        self.rollback_root = self.state_root / "rollback"
        self.activation_root = self.state_root / "activations"
        self.dropin_dir = Path("/etc/systemd/system/noyra.service.d")
        if str(config_root) != "/etc/noyra":
            self.dropin_dir = self.config_root.parent / "systemd" / "noyra.service.d"

    def activate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        values = _validate_activation_request(request)
        self._ensure_dirs()
        target_id = values["target_id"]
        task_id = values["task_id"]
        restored = self.agent_root / "restored" / task_id / "noyra.sqlite3"
        _reject_symlink_parents(restored, self.agent_root / "restored")
        self._validate_restored_database(restored, values)
        active_database = self.data_root / "noyra.sqlite3"
        if active_database.is_symlink() or not active_database.is_file():
            raise TargetActivationError("current_runtime_database_unavailable")
        if not self.systemd.is_active():
            raise TargetActivationError("current_runtime_not_active")
        marker = self.activation_root / f"{task_id}.json"
        if marker.exists():
            existing = _read_json(marker, "activation_record_invalid")
            if existing.get("status") == "active" and _request_binding_matches(existing, values):
                return {key: existing[key] for key in _ACTIVATION_RECEIPT_KEYS}
            raise TargetActivationError("activation_conflicts_with_existing_task")

        rollback = self.rollback_root / task_id
        if rollback.exists() or rollback.is_symlink():
            raise TargetActivationError("rollback_record_already_exists")
        rollback.mkdir(mode=0o700, parents=True)
        _secure_root_directory(rollback, "rollback_directory_invalid")
        dropin = self.dropin_dir / "migration-target.conf"
        _reject_symlink_parents(dropin, self.dropin_dir.parent)
        old_dropin: bytes | None = None
        if dropin.exists():
            if dropin.is_symlink() or not dropin.is_file():
                raise TargetActivationError("migration_target_dropin_invalid")
            old_dropin = dropin.read_bytes()
            _atomic_write(rollback / "previous-target.conf", old_dropin, 0o600)
        previous_runtime_owner = self._current_runtime_owner(active_database)
        journal = {
            **values,
            "status": "activating",
            "previous_runtime_owner": previous_runtime_owner,
            "previous_dropin_present": old_dropin is not None,
            "previous_subject_id": self._active_subject_id(active_database),
            "previous_database_sha256": _sha256_file(active_database),
        }
        _atomic_json(marker, journal, 0o600)
        staged = self.state_root / "staged" / task_id / "noyra.sqlite3"
        _secure_root_directory(staged.parent, "activation_staging_directory_invalid", create=True)
        cutover_started = False
        try:
            _copy_private(restored, staged)
            staged_digest = _sha256_file(staged)
            if staged_digest != values["artifact_sha256"]:
                raise TargetActivationError("restored_artifact_digest_mismatch")
            journal = {
                **journal,
                "staged_database_sha256": staged_digest,
                "root_staged_database_sha256": staged_digest,
            }
            _atomic_json(marker, journal, 0o600)
            self._validate_restored_database(staged, values)
            self._finalize_target_database(staged, values)
            cutover_started = True
            self.systemd.stop()
            self._move_database_set(active_database, rollback)
            os.replace(staged, active_database)
            _chown_service(active_database)
            os.chmod(active_database, 0o600)
            self._write_target_dropin(target_id)
            self.systemd.daemon_reload()
            self.systemd.start()
            if not self.systemd.wait_ready(
                values["subject_id"], target_id, self.ready_timeout_seconds
            ):
                raise TargetActivationError("target_runtime_readiness_failed")
            result = {
                **journal,
                "status": "active",
                "service_unit": "noyra.service",
                "previous_subject_id": journal["previous_subject_id"],
                "previous_dropin_present": journal["previous_dropin_present"],
                "active_database_sha256": _sha256_file(active_database),
                "activated_at": _now(),
            }
            _atomic_json(
                self.state_root / "current.json",
                {
                    "task_id": task_id,
                    "target_id": target_id,
                    "active_database_sha256": result["active_database_sha256"],
                },
                0o600,
            )
            _atomic_json(marker, result, 0o600)
            return {key: result[key] for key in _ACTIVATION_RECEIPT_KEYS}
        except Exception as error:
            if not cutover_started:
                _atomic_json(
                    marker,
                    {
                        **journal,
                        "status": "failed",
                        "error_code": (
                            error.error_code
                            if isinstance(error, TargetActivationError)
                            else "target_activation_failed"
                        ),
                    },
                    0o600,
                )
                if isinstance(error, TargetActivationError):
                    raise
                raise TargetActivationError("target_activation_failed") from error
            rollback_error = self._restore_previous_runtime(
                active_database, rollback, marker, old_dropin
            )
            if rollback_error:
                _atomic_json(
                    marker,
                    {**journal, "status": "recovery_required", "error_code": rollback_error},
                    0o600,
                )
                raise TargetActivationError("target_activation_recovery_required") from error
            if isinstance(error, TargetActivationError):
                raise
            raise TargetActivationError("target_activation_failed") from error

    def deactivate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        values = _validate_deactivation_request(request)
        self._ensure_dirs()
        task_id = values["task_id"]
        marker = self.activation_root / f"{task_id}.json"
        if not marker.exists():
            return {"status": "inactive", "task_id": task_id}
        state = _read_json(marker, "activation_record_invalid")
        if state.get("status") == "deactivated" and _request_binding_matches(state, values):
            return {"status": "deactivated", "task_id": task_id}
        if state.get("status") != "active" or not _request_binding_matches(state, values):
            raise TargetActivationError("activation_record_binding_invalid")
        rollback = self.rollback_root / task_id
        active_database = self.data_root / "noyra.sqlite3"
        current_path = self.state_root / "current.json"
        current = _read_json(current_path, "active_runtime_ownership_invalid")
        # The database changes during normal operation, so runtime ownership is
        # checked against the committed task identity rather than a stale hash.
        if (
            current.get("task_id") != task_id
            or current.get("target_id") != values["target_id"]
            or not active_database.is_file()
            or not self._database_owns_runtime(active_database, task_id, values["target_id"])
        ):
            raise TargetActivationError("active_runtime_ownership_mismatch")
        if not (rollback / "noyra.sqlite3").is_file():
            raise TargetActivationError("rollback_database_unavailable")
        try:
            _atomic_json(marker, {**state, "status": "deactivating"}, 0o600)
            self.systemd.stop()
            self._move_database_set(active_database, self.state_root / "failed-target" / task_id)
            self._move_database_set(rollback / "noyra.sqlite3", self.data_root)
            _chown_service(active_database)
            os.chmod(active_database, 0o600)
            self._restore_dropin(rollback, bool(state.get("previous_dropin_present")))
            self.systemd.daemon_reload()
            self.systemd.start()
            if not self.systemd.wait_ready(
                str(state["previous_subject_id"]),
                _previous_target_id(rollback, bool(state.get("previous_dropin_present"))),
                self.ready_timeout_seconds,
            ):
                raise TargetActivationError("rollback_runtime_readiness_failed")
            self._restore_current_runtime_owner(state.get("previous_runtime_owner"))
            _atomic_json(
                marker, {**state, "status": "deactivated", "deactivated_at": _now()}, 0o600
            )
            return {"status": "deactivated", "task_id": task_id}
        except TargetActivationError as error:
            _atomic_json(
                marker,
                {**state, "status": "deactivating", "error_code": error.error_code},
                0o600,
            )
            raise
        except Exception as error:
            _atomic_json(
                marker,
                {
                    **state,
                    "status": "deactivating",
                    "error_code": "target_deactivation_failed",
                },
                0o600,
            )
            raise TargetActivationError("target_deactivation_failed") from error

    def recover_incomplete(self) -> int:
        """Restore the former DB before normal service startup after a crash mid-cutover."""
        self._ensure_dirs()
        recovered = 0
        for marker in sorted(self.activation_root.glob("*.json")):
            if marker.is_symlink() or not marker.is_file():
                raise TargetActivationError("activation_record_invalid")
            value = _read_json(marker, "activation_record_invalid")
            if value.get("status") == "deactivated":
                current_path = self.state_root / "current.json"
                if current_path.exists():
                    current = _read_json(current_path, "active_runtime_ownership_invalid")
                    if current.get("task_id") == value.get("task_id"):
                        current_path.unlink(missing_ok=True)
                continue
            if value.get("status") not in {"activating", "deactivating", "recovery_required"}:
                continue
            task_id = str(value.get("task_id", ""))
            if not _SAFE_ID.fullmatch(task_id) or marker.name != f"{task_id}.json":
                raise TargetActivationError("activation_record_binding_invalid")
            rollback = self.rollback_root / task_id
            backup_database = rollback / "noyra.sqlite3"
            active_database = self.data_root / "noyra.sqlite3"
            if (
                value.get("status") == "deactivating"
                and active_database.is_file()
                and _sha256_file(active_database) == value.get("previous_database_sha256")
            ):
                if self.systemd.is_active():
                    self.systemd.stop()
                self._restore_dropin(rollback, bool(value.get("previous_dropin_present")))
                self.systemd.daemon_reload()
                self._restore_current_runtime_owner(value.get("previous_runtime_owner"))
                _atomic_json(
                    marker, {**value, "status": "recovered", "recovered_at": _now()}, 0o600
                )
                recovered += 1
                continue
            if (
                value.get("status") == "activating"
                and active_database.is_file()
                and _sha256_file(active_database) == value.get("previous_database_sha256")
                and not backup_database.exists()
            ):
                _atomic_json(
                    marker,
                    {**value, "status": "recovered", "recovered_at": _now()},
                    0o600,
                )
                recovered += 1
                continue
            if not backup_database.is_file() or backup_database.is_symlink():
                raise TargetActivationError("rollback_database_unavailable")
            if self.systemd.is_active():
                self.systemd.stop()
            if active_database.exists():
                failed = self.state_root / "failed-target" / task_id
                self._move_database_set(active_database, failed)
            self._move_database_set(backup_database, self.data_root)
            _chown_service(active_database)
            os.chmod(active_database, 0o600)
            self._restore_dropin(rollback, bool(value.get("previous_dropin_present")))
            self.systemd.daemon_reload()
            self._restore_current_runtime_owner(value.get("previous_runtime_owner"))
            _atomic_json(marker, {**value, "status": "recovered", "recovered_at": _now()}, 0o600)
            recovered += 1
        return recovered

    def _validate_restored_database(self, path: Path, request: Mapping[str, Any]) -> None:
        if path.is_symlink() or not path.is_file():
            raise TargetActivationError("restored_database_unavailable")
        try:
            with closing(
                sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            ) as connection:
                connection.row_factory = sqlite3.Row
                check = connection.execute("PRAGMA quick_check").fetchone()
                subject = connection.execute(
                    "SELECT subject_id FROM runtime_state LIMIT 1"
                ).fetchone()
                task = connection.execute(
                    "SELECT subject_id,target_id,status,manifest_digest,artifact_id,"
                    "target_epoch_id,"
                    "source_epoch "
                    "FROM migration_tasks WHERE task_id=?",
                    (request["task_id"],),
                ).fetchone()
                epoch = (
                    None
                    if task is None
                    else connection.execute(
                        "SELECT * FROM migration_epochs WHERE epoch_id=?",
                        (task["target_epoch_id"],),
                    ).fetchone()
                )
        except (OSError, sqlite3.Error) as error:
            raise TargetActivationError("restored_database_invalid") from error
        if (
            check is None
            or check[0] != "ok"
            or subject is None
            or subject[0] != request["subject_id"]
        ):
            raise TargetActivationError("restored_database_identity_invalid")
        if (
            task is None
            or task["subject_id"] != request["subject_id"]
            or task["target_id"] != request["target_id"]
            or task["source_epoch"] != request["source_epoch"]
            or task["status"] not in {"validating", "cutover"}
            or task["manifest_digest"] != request["manifest_digest"]
            or task["artifact_id"] != request["artifact_id"]
            or epoch is None
            or epoch["target_id"] != request["target_id"]
            or epoch["status"] != "active"
            or epoch["state_hash"] != EpochLease._state_hash(epoch)
            or content_hash(
                {
                    "task_id": request["task_id"],
                    "source_epoch": request["source_epoch"],
                    "epoch_id": epoch["epoch_id"],
                    "epoch_number": int(epoch["epoch_number"]),
                    "status": epoch["status"],
                }
            )
            != request["source_fence_digest"]
        ):
            raise TargetActivationError("restored_migration_epoch_invalid")

    def _finalize_target_database(self, path: Path, request: Mapping[str, Any]) -> None:
        database = Database(path, initialize=False)
        manager = MigrationManager(database, MigrationStore(database))
        with database.transaction() as connection:
            task = connection.execute(
                "SELECT status,target_epoch_id FROM migration_tasks WHERE task_id=?",
                (request["task_id"],),
            ).fetchone()
            if task is None or task["status"] not in {"validating", "cutover"}:
                raise TargetActivationError("restored_migration_task_changed")
            if task["status"] == "validating":
                manager.transition_task_in_transaction(
                    connection,
                    request["task_id"],
                    "cutover",
                    actor="migration-target",
                    expected_status="validating",
                )
            manager.transition_task_in_transaction(
                connection,
                request["task_id"],
                "committed",
                actor="migration-target",
                expected_status="cutover",
            )
            epoch_row = connection.execute(
                "SELECT subject_id,target_id,epoch_id,epoch_number FROM migration_epochs "
                "WHERE epoch_id=? AND status='active'",
                (task["target_epoch_id"],),
            ).fetchone()
            if epoch_row is None or epoch_row["target_id"] != request["target_id"]:
                raise TargetActivationError("restored_migration_epoch_changed")
            EpochLease(
                database,
                epoch_row["subject_id"],
                epoch_row["target_id"],
                epoch_row["epoch_id"],
                int(epoch_row["epoch_number"]),
            ).complete_in_transaction(connection, "migration-target")
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            check = connection.execute("PRAGMA quick_check").fetchone()
        if check is None or check[0] != "ok":
            raise TargetActivationError("target_database_finalize_failed")

    def _restore_previous_runtime(
        self,
        active_database: Path,
        rollback: Path,
        marker: Path,
        old_dropin: bytes | None,
    ) -> str | None:
        try:
            if self.systemd.is_active():
                self.systemd.stop()
            backup_database = rollback / "noyra.sqlite3"
            if backup_database.exists():
                failed = self.state_root / "failed-target" / rollback.name
                self._move_database_set(active_database, failed)
                self._move_database_set(backup_database, self.data_root)
                _chown_service(active_database)
                os.chmod(active_database, 0o600)
            self._restore_dropin(rollback, old_dropin is not None)
            self.systemd.daemon_reload()
            self.systemd.start()
            if not self.systemd.wait_ready(
                str(_read_json(marker, "activation_record_invalid").get("previous_subject_id")),
                _previous_target_id(rollback, old_dropin is not None),
                self.ready_timeout_seconds,
            ):
                return "previous_runtime_readiness_failed"
            previous = _read_json(marker, "activation_record_invalid")
            self._restore_current_runtime_owner(previous.get("previous_runtime_owner"))
            _atomic_json(
                marker,
                {**previous, "status": "failed_reverted", "error_code": "target_activation_failed"},
                0o600,
            )
            return None
        except Exception:
            return "previous_runtime_restore_failed"

    def _write_target_dropin(self, target_id: str) -> None:
        if not _TARGET_ID.fullmatch(target_id):
            raise TargetActivationError("target_id_invalid")
        _reject_symlink_parents(self.dropin_dir / "migration-target.conf", self.dropin_dir.parent)
        self.dropin_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        _atomic_write(
            self.dropin_dir / "migration-target.conf",
            f"[Service]\nEnvironment=NOYRA_MIGRATION_TARGET_ID={target_id}\n".encode(),
            0o644,
        )

    def _restore_dropin(self, rollback: Path, present: bool) -> None:
        path = self.dropin_dir / "migration-target.conf"
        if present:
            saved = rollback / "previous-target.conf"
            if saved.is_symlink() or not saved.is_file():
                raise TargetActivationError("previous_target_configuration_unavailable")
            self.dropin_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
            _atomic_write(path, saved.read_bytes(), 0o644)
        elif path.exists():
            if path.is_symlink() or not path.is_file():
                raise TargetActivationError("migration_target_dropin_invalid")
            path.unlink()

    def _move_database_set(self, source: Path, destination_root: Path) -> None:
        destination_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private_directory(destination_root, "database_handoff_directory_invalid")
        for suffix in ("", "-wal", "-shm"):
            current = Path(f"{source}{suffix}")
            if not current.exists():
                continue
            if current.is_symlink() or not current.is_file():
                raise TargetActivationError("database_handoff_file_invalid")
            destination = destination_root / current.name
            if destination.exists() or destination.is_symlink():
                raise TargetActivationError("database_handoff_destination_exists")
            os.replace(current, destination)

    def _active_subject_id(self, database_path: Path) -> str:
        try:
            with closing(
                sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
            ) as connection:
                row = connection.execute("SELECT subject_id FROM runtime_state LIMIT 1").fetchone()
        except sqlite3.Error as error:
            raise TargetActivationError("current_runtime_database_invalid") from error
        if row is None or not isinstance(row[0], str):
            raise TargetActivationError("current_runtime_subject_unavailable")
        return row[0]

    def _ensure_dirs(self) -> None:
        for path, code in (
            (self.state_root, "activation_state_directory_invalid"),
            (self.rollback_root, "rollback_directory_invalid"),
            (self.activation_root, "activation_directory_invalid"),
        ):
            _secure_root_directory(path, code, create=True, trusted_root=self.data_root)

    def _current_runtime_owner(self, active_database: Path) -> dict[str, str] | None:
        path = self.state_root / "current.json"
        if not path.exists():
            if path.is_symlink():
                raise TargetActivationError("active_runtime_ownership_invalid")
            return None
        current = _read_json(path, "active_runtime_ownership_invalid")
        task_id = current.get("task_id")
        target_id = current.get("target_id")
        database_digest = current.get("active_database_sha256")
        if (
            not isinstance(task_id, str)
            or not _SAFE_ID.fullmatch(task_id)
            or not isinstance(target_id, str)
            or not _TARGET_ID.fullmatch(target_id)
            or not isinstance(database_digest, str)
            or not _DIGEST.fullmatch(database_digest)
            or active_database.is_symlink()
            or not active_database.is_file()
            or not self._database_owns_runtime(active_database, task_id, target_id)
        ):
            raise TargetActivationError("active_runtime_ownership_mismatch")
        return {
            "task_id": task_id,
            "target_id": target_id,
            "active_database_sha256": database_digest,
        }

    def _restore_current_runtime_owner(self, owner: Any) -> None:
        path = self.state_root / "current.json"
        if owner is None:
            path.unlink(missing_ok=True)
            return
        if not isinstance(owner, dict):
            raise TargetActivationError("active_runtime_ownership_invalid")
        task_id = owner.get("task_id")
        target_id = owner.get("target_id")
        database_digest = owner.get("active_database_sha256")
        if (
            not isinstance(task_id, str)
            or not _SAFE_ID.fullmatch(task_id)
            or not isinstance(target_id, str)
            or not _TARGET_ID.fullmatch(target_id)
            or not isinstance(database_digest, str)
            or not _DIGEST.fullmatch(database_digest)
        ):
            raise TargetActivationError("active_runtime_ownership_invalid")
        active_database = self.data_root / "noyra.sqlite3"
        if (
            active_database.is_symlink()
            or not active_database.is_file()
            or not self._database_owns_runtime(active_database, task_id, target_id)
        ):
            raise TargetActivationError("active_runtime_ownership_mismatch")
        _atomic_json(path, owner, 0o600)

    @staticmethod
    def _database_owns_runtime(database_path: Path, task_id: str, target_id: str) -> bool:
        try:
            with closing(
                sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
            ) as connection:
                row = connection.execute(
                    "SELECT target_id,status FROM migration_tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise TargetActivationError("active_runtime_database_invalid") from error
        return row is not None and row[0] == target_id and row[1] == "committed"


class TargetActivationBridge:
    """Submit an agent-signed fixed-path request and await the root runner result."""

    def __init__(
        self,
        request_root: Path | str,
        status_root: Path | str,
        signing_key: Ed25519PrivateKey,
        *,
        timeout_seconds: float = 180,
        poll_seconds: float = 0.25,
    ):
        self.request_root = Path(request_root)
        self.status_root = Path(status_root)
        self.signing_key = signing_key
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds

    def activate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._submit(request, "activate")

    def deactivate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._submit(request, "deactivate")

    def _submit(self, request: Mapping[str, Any], operation: str) -> dict[str, Any]:
        task_id = request.get("task_id")
        if not isinstance(task_id, str) or not _SAFE_ID.fullmatch(task_id):
            raise TargetActivationError("activation_request_invalid")
        _private_directory(self.request_root, "activation_request_directory_invalid", create=True)
        _private_directory(self.status_root, "activation_status_directory_invalid", create=True)
        request_path = self.request_root / f"{task_id}.{operation}.json"
        status_path = self.status_root / f"{task_id}.{operation}.json"
        if request_path.exists() or request_path.is_symlink():
            raise TargetActivationError("activation_request_already_pending")
        payload = dict(request)
        signature = base64.urlsafe_b64encode(
            self.signing_key.sign(canonical_json(payload).encode())
        ).decode()
        request_digest = content_hash(payload)
        if status_path.is_symlink():
            raise TargetActivationError("activation_status_invalid")
        if status_path.is_file():
            previous = _read_json(status_path, "activation_status_invalid")
            if previous.get("request_digest") != request_digest:
                raise TargetActivationError("activation_request_conflicts_with_existing_task")
            return self._status_result(previous, operation)
        _atomic_json(
            request_path,
            {"payload": payload, "signature": signature, "request_digest": request_digest},
            0o600,
        )
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if status_path.is_symlink():
                raise TargetActivationError("activation_status_invalid")
            if status_path.is_file():
                result = _read_json(status_path, "activation_status_invalid")
                if result.get("request_digest") != request_digest:
                    raise TargetActivationError("activation_status_binding_invalid")
                return self._status_result(result, operation)
            time.sleep(self.poll_seconds)
        raise TargetActivationError("activation_runner_timeout")

    @staticmethod
    def _status_result(result: Mapping[str, Any], operation: str) -> dict[str, Any]:
        if result.get("status") != ("active" if operation == "activate" else "deactivated"):
            raise TargetActivationError(str(result.get("error_code", "activation_failed")))
        return {key: value for key, value in result.items() if key != "request_digest"}


def run_target_activation_requests(
    *,
    root: Path | str = "/var/lib/noyra/migration/target-activation",
    identity_file: Path | str = "/etc/noyra/migration/identity.json",
    data_root: Path | str = "/var/lib/noyra",
    config_root: Path | str = "/etc/noyra",
    systemd: ActivationSystemd | None = None,
) -> int:
    """Process queued, signed target activation controls as root."""
    root_path = Path(root)
    requests = root_path / "requests"
    statuses = root_path / "status"
    _private_directory(requests, "activation_request_directory_invalid", create=True)
    _private_directory(statuses, "activation_status_directory_invalid", create=True)
    identity = _read_identity(Path(identity_file))
    target_id = identity.get("target_id")
    public_value = identity.get("public_key")
    if not isinstance(target_id, str) or not _TARGET_ID.fullmatch(target_id):
        raise TargetActivationError("migration_target_identity_invalid")
    if not isinstance(public_value, str):
        raise TargetActivationError("migration_target_identity_invalid")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.urlsafe_b64decode(public_value + "=" * (-len(public_value) % 4))
        )
    except (ValueError, TypeError, binascii.Error) as error:
        raise TargetActivationError("migration_target_identity_invalid") from error
    activator = TargetRuntimeActivator(data_root, config_root, systemd=systemd)
    processed = 0
    for path in sorted(requests.glob("*.json"))[:64]:
        match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9_.:-]{2,127})\.(activate|deactivate)\.json", path.name
        )
        if match is None:
            continue
        task_id, operation = match.groups()
        status_path = statuses / f"{task_id}.{operation}.json"
        request_digest = content_hash({"invalid_request": path.name})
        try:
            if path.is_symlink() or not path.is_file():
                raise TargetActivationError("activation_request_file_invalid")
            envelope = _read_json(
                path, "activation_request_invalid", limit=_MAX_ACTIVATION_REQUEST_BYTES
            )
            payload = envelope.get("payload")
            signature = envelope.get("signature")
            if not isinstance(payload, dict) or not isinstance(signature, str):
                raise TargetActivationError("activation_request_invalid")
            request_digest = content_hash(payload)
            if (
                payload.get("task_id") != task_id
                or payload.get("target_id") != target_id
                or envelope.get("request_digest") != request_digest
            ):
                raise TargetActivationError("activation_request_binding_invalid")
            try:
                raw_signature = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
                public_key.verify(raw_signature, canonical_json(payload).encode())
            except (ValueError, TypeError, InvalidSignature, binascii.Error) as error:
                raise TargetActivationError("activation_request_signature_invalid") from error
            result = (
                activator.activate(payload)
                if operation == "activate"
                else activator.deactivate(payload)
            )
        except TargetActivationError as error:
            result = {
                "status": "failed",
                "task_id": task_id,
                "target_id": target_id,
                "request_digest": request_digest,
                "error_code": error.error_code,
            }
        except Exception:
            result = {
                "status": "failed",
                "task_id": task_id,
                "target_id": target_id,
                "request_digest": request_digest,
                "error_code": "activation_runner_failed",
            }
        result.setdefault("request_digest", request_digest)
        _atomic_json(status_path, result, 0o640, group="noyra")
        path.unlink(missing_ok=True)
        processed += 1
    return processed


def recover_incomplete_target_activations(
    *,
    data_root: Path | str = "/var/lib/noyra",
    config_root: Path | str = "/etc/noyra",
    systemd: ActivationSystemd | None = None,
) -> int:
    return TargetRuntimeActivator(data_root, config_root, systemd=systemd).recover_incomplete()


_ACTIVATION_RECEIPT_KEYS = (
    "task_id",
    "subject_id",
    "target_id",
    "source_epoch",
    "manifest_digest",
    "artifact_id",
    "artifact_sha256",
    "health_report_digest",
    "source_fence_digest",
    "status",
    "service_unit",
    "active_database_sha256",
    "activated_at",
)


def _validate_activation_request(request: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "task_id",
        "subject_id",
        "target_id",
        "source_epoch",
        "manifest_digest",
        "artifact_id",
        "artifact_sha256",
        "health_report_digest",
        "source_fence_digest",
    }
    if set(request) != required:
        raise TargetActivationError("activation_request_invalid")
    values = dict(request)
    for key in ("task_id", "source_epoch"):
        if not isinstance(values[key], str) or not _SAFE_ID.fullmatch(values[key]):
            raise TargetActivationError("activation_request_invalid")
    if not isinstance(values["subject_id"], str) or not re.fullmatch(
        r"Noyra-[A-Za-z0-9_-]{1,120}", values["subject_id"]
    ):
        raise TargetActivationError("activation_request_invalid")
    if not isinstance(values["target_id"], str) or not _TARGET_ID.fullmatch(values["target_id"]):
        raise TargetActivationError("activation_request_invalid")
    if not isinstance(values["artifact_id"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", values["artifact_id"]
    ):
        raise TargetActivationError("activation_request_invalid")
    for key in (
        "manifest_digest",
        "artifact_sha256",
        "health_report_digest",
        "source_fence_digest",
    ):
        if not isinstance(values[key], str) or not _DIGEST.fullmatch(values[key]):
            raise TargetActivationError("activation_request_invalid")
    return values


def _validate_deactivation_request(request: Mapping[str, Any]) -> dict[str, Any]:
    required = {"task_id", "target_id", "source_epoch", "manifest_digest"}
    if set(request) != required:
        raise TargetActivationError("deactivation_request_invalid")
    values = dict(request)
    if not isinstance(values["task_id"], str) or not _SAFE_ID.fullmatch(values["task_id"]):
        raise TargetActivationError("deactivation_request_invalid")
    if not isinstance(values["target_id"], str) or not _TARGET_ID.fullmatch(values["target_id"]):
        raise TargetActivationError("deactivation_request_invalid")
    if not isinstance(values["source_epoch"], str) or not _SAFE_ID.fullmatch(
        values["source_epoch"]
    ):
        raise TargetActivationError("deactivation_request_invalid")
    if not isinstance(values["manifest_digest"], str) or not _DIGEST.fullmatch(
        values["manifest_digest"]
    ):
        raise TargetActivationError("deactivation_request_invalid")
    return values


def _request_binding_matches(record: Mapping[str, Any], request: Mapping[str, Any]) -> bool:
    return all(record.get(key) == request.get(key) for key in request)


def _read_identity(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise TargetActivationError("migration_target_identity_unavailable")
    value = _read_json(path, "migration_target_identity_invalid")
    if not isinstance(value, dict):
        raise TargetActivationError("migration_target_identity_invalid")
    return value


def _read_json(
    path: Path, error_code: str, *, limit: int = _MAX_ACTIVATION_REQUEST_BYTES
) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise TargetActivationError(error_code)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise TargetActivationError("activation_request_too_large")
        value = json.loads(raw)
    except TargetActivationError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise TargetActivationError(error_code) from error
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    if not isinstance(value, dict):
        raise TargetActivationError(error_code)
    return value


def _private_directory(path: Path, error_code: str, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o750, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise TargetActivationError(error_code)
    _reject_symlink_parents(path, Path(path.anchor))


def _reject_symlink_parents(path: Path, boundary: Path) -> None:
    try:
        relative = path.absolute().relative_to(boundary.absolute())
    except ValueError as error:
        raise TargetActivationError("activation_path_invalid") from error
    current = boundary
    ancestors = [current]
    while current.parent != current:
        current = current.parent
        ancestors.append(current)
    if any(directory.is_symlink() for directory in ancestors):
        raise TargetActivationError("activation_path_invalid")
    current = boundary
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise TargetActivationError("activation_path_invalid")


def _secure_root_directory(
    path: Path,
    error_code: str,
    *,
    create: bool = False,
    trusted_root: Path | None = None,
) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise TargetActivationError(error_code)
    if os.name != "nt":
        directories = [path]
        parent = path.parent
        while trusted_root is not None and parent != trusted_root.parent:
            directories.append(parent)
            if parent == trusted_root:
                break
            parent = parent.parent
        if trusted_root is not None and trusted_root not in directories:
            raise TargetActivationError(error_code)
        for directory in directories:
            if directory.is_symlink() or not directory.is_dir():
                raise TargetActivationError(error_code)
            metadata = directory.stat(follow_symlinks=False)
            if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise TargetActivationError(error_code)


def _atomic_json(
    path: Path, value: Mapping[str, Any], mode: int, *, group: str | None = None
) -> None:
    _private_directory(path.parent, "activation_directory_invalid", create=True)
    _atomic_write(path, json.dumps(value, sort_keys=True, separators=(",", ":")).encode(), mode)
    if group is not None and os.name != "nt":
        import grp

        group_id = int(cast(Any, grp).getgrnam(group).gr_gid)
        cast(Any, os).chown(path, 0, group_id)
        os.chmod(path, mode)


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    _private_directory(path.parent, "activation_directory_invalid", create=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise TargetActivationError("activation_path_invalid")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        with suppress_oserror():
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
        raise


class suppress_oserror:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> bool:
        return True


def _copy_private(source: Path, destination: Path) -> None:
    if (
        source.is_symlink()
        or not source.is_file()
        or destination.exists()
        or destination.is_symlink()
    ):
        raise TargetActivationError("activation_database_copy_invalid")
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    os.chmod(destination, 0o600)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _service_uid() -> int:
    if os.name == "nt":
        return os.getuid() if hasattr(os, "getuid") else 0
    import pwd

    return int(cast(Any, pwd).getpwnam("noyra").pw_uid)


def _service_gid() -> int:
    if os.name == "nt":
        return os.getgid() if hasattr(os, "getgid") else 0
    import grp

    return int(cast(Any, grp).getgrnam("noyra").gr_gid)


def _chown_service(path: Path) -> None:
    if os.name != "nt":
        cast(Any, os).chown(path, _service_uid(), _service_gid())


def _configured_port(path: Path) -> int:
    if path.is_symlink() or not path.is_file():
        return 8765
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("NOYRA_PORT="):
                value = line.partition("=")[2].strip().strip("'\"")
                if value.isdigit() and 1 <= int(value) <= 65535:
                    return int(value)
                break
    except (OSError, UnicodeError):
        pass
    return 8765


def _previous_target_id(rollback: Path, present: bool) -> str | None:
    if not present:
        return None
    path = rollback / "previous-target.conf"
    if path.is_symlink() or not path.is_file():
        return None
    match = re.search(
        r"^Environment=NOYRA_MIGRATION_TARGET_ID=([A-Za-z0-9_-]{3,128})$", path.read_text()
    )
    return None if match is None else match.group(1)


def _now() -> str:
    return datetime.now(UTC).isoformat()
