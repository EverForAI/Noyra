#!/usr/bin/env python3
"""Validate fixed migration receipts at the root-owned execution boundary."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 1_000_000
DATA_ROOT = Path(os.environ.get("NOYRA_MIGRATION_DATA_ROOT", "/var/lib/noyra/migration"))
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN = frozenset(
    {"secret", "token", "password", "private_key", "api_key", "bearer", "credential"}
)


class RunnerError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise RunnerError(f"invalid_{label}")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise RunnerError(f"invalid_{label}")
    return value


def _contains_secret(key: Any, value: Any) -> bool:
    if not isinstance(key, str):
        return True
    normalized = key.casefold().replace("-", "_")
    if normalized in _FORBIDDEN or any(marker in normalized for marker in _FORBIDDEN):
        return True
    if isinstance(value, dict):
        return any(_contains_secret(child, item) for child, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret("item", item) for item in value)
    return False


def _private_regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise RunnerError(f"{label}_unavailable")


def _private_directory(path: Path, label: str, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o750, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RunnerError(f"{label}_directory_invalid")


def _read_request(request_id: str) -> dict[str, Any]:
    requests = DATA_ROOT / "requests"
    _private_directory(DATA_ROOT, "data")
    _private_directory(requests, "request")
    path = requests / f"{request_id}.json"
    _private_regular(path, "request")
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_REQUEST_BYTES:
            raise RunnerError("request_too_large")
        value = json.loads(raw.decode("utf-8"))
    except RunnerError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise RunnerError("request_invalid") from error
    if not isinstance(value, dict) or _contains_secret("request", value):
        raise RunnerError("request_contains_secret_or_is_invalid")
    if value.get("request_id", request_id) != request_id:
        raise RunnerError("request_id_mismatch")
    if value.get("task_id") != request_id:
        raise RunnerError("task_id_mismatch")
    return value


def _canonical_digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _report(
    request: dict[str, Any], key: str, status: str, *, require_checks: bool = False
) -> dict[str, Any]:
    value = request.get(key)
    if not isinstance(value, dict):
        raise RunnerError(f"{key}_missing")
    if value.get("target_id") != request["target_id"]:
        raise RunnerError(f"{key}_target_mismatch")
    if value.get("artifact_id") != request["artifact_id"]:
        raise RunnerError(f"{key}_artifact_mismatch")
    if value.get("manifest_digest") != request["manifest_digest"]:
        raise RunnerError(f"{key}_manifest_mismatch")
    if value.get("status") != status:
        raise RunnerError(f"{key}_status_invalid")
    if require_checks:
        checks = value.get("checks")
        if (
            not isinstance(checks, dict)
            or not checks
            or not all(type(item) is bool and item for item in checks.values())
        ):
            raise RunnerError("health_checks_failed")
        if not isinstance(value.get("host_identity"), str) or not value["host_identity"].strip():
            raise RunnerError("health_host_identity_missing")
    return value


def _fence(request: dict[str, Any]) -> dict[str, Any]:
    value = request.get("fence_proof")
    if not isinstance(value, dict):
        raise RunnerError("fence_proof_missing")
    if (
        value.get("task_id") != request["task_id"]
        or value.get("subject_id") != request["subject_id"]
        or value.get("target_id") != request["target_id"]
        or value.get("source_epoch") != request["source_epoch"]
        or value.get("status") != "active"
    ):
        raise RunnerError("fence_proof_binding_invalid")
    _safe_id(value.get("target_epoch_id"), "target_epoch_id")
    if type(value.get("epoch_number")) is not int or value["epoch_number"] < 1:
        raise RunnerError("fence_proof_epoch_invalid")
    return value


class MigrationExecutor:
    """Perform the bounded local side of a migration operation.

    The runner never promotes caller supplied receipts to ``completed``.  It
    first performs the concrete file/database operation and only then emits a
    report derived from what it observed.  Remote target transfer remains the
    responsibility of the authenticated target agent; this process handles
    only fixed paths below its private data root.
    """

    def __init__(self, data_root: Path):
        self.data_root = data_root

    def _artifact(self, request: dict[str, Any]) -> tuple[Path, bytes]:
        root = self.data_root / "artifacts"
        _private_directory(root, "artifact")
        artifact_id = request["artifact_id"]
        path = root / f"{artifact_id}.bin"
        _private_regular(path, "artifact")
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise RunnerError("artifact_unreadable") from error
        if len(payload) != request["artifact_byte_size"]:
            raise RunnerError("artifact_size_mismatch")
        if hashlib.sha256(payload).hexdigest() != request["artifact_sha256"]:
            raise RunnerError("artifact_digest_mismatch")
        return path, payload

    def _restore_root(self, request: dict[str, Any]) -> Path:
        root = self.data_root / "restored" / str(request["target_id"])
        if root.exists() and root.is_symlink():
            raise RunnerError("restore_root_invalid")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private_directory(root, "restore")
        return root

    def fence(self, request: dict[str, Any]) -> dict[str, Any]:
        source_root = self.data_root / "source"
        _private_directory(source_root, "source")
        epoch_path = source_root / "epoch"
        _private_regular(epoch_path, "source_epoch")
        try:
            source_epoch = epoch_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise RunnerError("source_epoch_unreadable") from error
        if source_epoch != request["source_epoch"]:
            raise RunnerError("source_epoch_mismatch")
        fences = self.data_root / "fences"
        _private_directory(fences, "fence", create=True)
        marker = fences / f"{request['task_id']}.json"
        value = {
            "task_id": request["task_id"],
            "subject_id": request["subject_id"],
            "source_epoch": source_epoch,
            "status": "active",
            "fenced_at": _now(),
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        if marker.exists():
            _private_regular(marker, "fence")
            try:
                existing = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as error:
                raise RunnerError("fence_marker_invalid") from error
            stable_keys = ("task_id", "subject_id", "source_epoch", "status")
            if any(existing.get(key) != value[key] for key in stable_keys):
                raise RunnerError("fence_conflict")
            value = existing
        else:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                with suppress(OSError):
                    os.close(descriptor)
                marker.unlink(missing_ok=True)
                raise
        return {
            **value,
            "target_epoch_id": request["fence_proof"]["target_epoch_id"],
            "epoch_number": request["fence_proof"]["epoch_number"],
        }

    def unfence(self, request: dict[str, Any]) -> dict[str, Any]:
        """Remove a source fence only while the source epoch is unchanged."""
        source_root = self.data_root / "source"
        _private_directory(source_root, "source")
        epoch_path = source_root / "epoch"
        _private_regular(epoch_path, "source_epoch")
        try:
            source_epoch = epoch_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise RunnerError("source_epoch_unreadable") from error
        if source_epoch != request["source_epoch"]:
            raise RunnerError("source_epoch_mismatch")
        fences = self.data_root / "fences"
        if not fences.exists():
            return {
                "status": "completed",
                "operation": "unfence",
                "task_id": request["task_id"],
                "source_epoch": source_epoch,
                "already_inactive": True,
                "completed_at": _now(),
            }
        _private_directory(fences, "fence")
        marker = fences / f"{request['task_id']}.json"
        if not marker.exists():
            return {
                "status": "completed",
                "operation": "unfence",
                "task_id": request["task_id"],
                "source_epoch": source_epoch,
                "already_inactive": True,
                "completed_at": _now(),
            }
        _private_regular(marker, "fence")
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise RunnerError("fence_marker_invalid") from error
        if (
            not isinstance(value, dict)
            or value.get("task_id") != request["task_id"]
            or value.get("subject_id") != request["subject_id"]
            or value.get("source_epoch") != source_epoch
            or value.get("status") != "active"
        ):
            raise RunnerError("fence_conflict")
        try:
            marker.unlink()
        except OSError as error:
            raise RunnerError("fence_remove_failed") from error
        return {
            "status": "completed",
            "operation": "unfence",
            "task_id": request["task_id"],
            "source_epoch": source_epoch,
            "already_inactive": False,
            "completed_at": _now(),
        }

    def restore(self, request: dict[str, Any]) -> dict[str, Any]:
        path, payload = self._artifact(request)
        root = self._restore_root(request)
        artifact_format = request["artifact_format"]
        database = root / "noyra.sqlite3"
        if database.exists() or database.is_symlink():
            raise RunnerError("restore_target_not_empty")
        if artifact_format == "sqlite":
            temporary = root / ".noyra.sqlite3.restore"
            try:
                temporary.write_bytes(payload)
                connection = sqlite3.connect(temporary)
                try:
                    result = connection.execute("PRAGMA quick_check").fetchone()
                finally:
                    connection.close()
                if result is None or result[0] != "ok":
                    raise RunnerError("restore_database_check_failed")
                temporary.replace(database)
                os.chmod(database, 0o600)
            except RunnerError:
                temporary.unlink(missing_ok=True)
                raise
            except (OSError, sqlite3.DatabaseError) as error:
                temporary.unlink(missing_ok=True)
                raise RunnerError("restore_database_failed") from error
        elif artifact_format == "noyra-encrypted-backup":
            keyring = os.environ.get("NOYRA_MIGRATION_BACKUP_KEYRING")
            if not keyring:
                raise RunnerError("backup_keyring_unavailable")
            try:
                from noyra.core.at_rest import EncryptedBackupManager

                EncryptedBackupManager(root, keyring).restore(path, root)
            except Exception as error:
                raise RunnerError("encrypted_backup_restore_failed") from error
        else:
            raise RunnerError("artifact_format_unsupported")
        try:
            with sqlite3.connect(database) as connection:
                row = connection.execute("SELECT subject_id FROM runtime_state LIMIT 1").fetchone()
            if row is None or row[0] != request["subject_id"]:
                raise RunnerError("restore_subject_mismatch")
        except RunnerError:
            raise
        except (OSError, sqlite3.DatabaseError) as error:
            raise RunnerError("restore_database_unreadable") from error
        return {
            "target_id": request["target_id"],
            "artifact_id": request["artifact_id"],
            "manifest_digest": request["manifest_digest"],
            "status": "restored",
            "subject_id": request["subject_id"],
            "restore_path": str(database),
            "artifact_sha256": request["artifact_sha256"],
        }

    def health(
        self, request: dict[str, Any], restore: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        root = self._restore_root(request)
        database = root / "noyra.sqlite3"
        _private_regular(database, "restored_database")
        try:
            connection = sqlite3.connect(database)
            try:
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
                subject = connection.execute(
                    "SELECT subject_id FROM runtime_state LIMIT 1"
                ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.DatabaseError) as error:
            raise RunnerError("health_database_unreadable") from error
        checks = {
            "database_quick_check": quick_check is not None and quick_check[0] == "ok",
            "subject_identity": subject is not None and subject[0] == request["subject_id"],
            "restore_root_binding": root.parent == self.data_root / "restored",
        }
        if not all(checks.values()):
            raise RunnerError("health_checks_failed")
        return {
            "target_id": request["target_id"],
            "artifact_id": request["artifact_id"],
            "manifest_digest": request["manifest_digest"],
            "status": "healthy",
            "host_identity": hashlib.sha256(str(root).encode()).hexdigest(),
            "checks": checks,
        }


def _validate_request(request: dict[str, Any], action: str) -> dict[str, Any]:
    allowed = {
        "request_id",
        "task_id",
        "subject_id",
        "target_id",
        "source_epoch",
        "manifest_digest",
        "artifact_id",
        "restore_report",
        "health_report",
        "fence_proof",
        "artifact_sha256",
        "artifact_byte_size",
        "artifact_format",
        "requested_at",
    }
    if set(request) - allowed:
        raise RunnerError("request_fields_invalid")
    for key in ("task_id", "subject_id", "target_id", "source_epoch", "artifact_id"):
        _safe_id(request.get(key), key)
    _digest(request.get("manifest_digest"), "manifest_digest")
    _digest(request.get("artifact_sha256"), "artifact_sha256")
    if (
        type(request.get("artifact_byte_size")) is not int
        or not 1 <= request["artifact_byte_size"] <= 10_000_000_000
    ):
        raise RunnerError("invalid_artifact_byte_size")
    if request.get("artifact_format") not in {"sqlite", "noyra-encrypted-backup"}:
        raise RunnerError("invalid_artifact_format")
    executor = MigrationExecutor(DATA_ROOT)
    if action == "restore":
        _report(request, "restore_report", "restored")
        _report(request, "health_report", "healthy", require_checks=True)
        _fence(request)
        fence = executor.fence(request)
        restore = executor.restore(request)
        health = executor.health(request, restore)
        return {
            "status": "completed",
            "operation": action,
            "task_id": request["task_id"],
            "subject_id": request["subject_id"],
            "target_id": request["target_id"],
            "source_epoch": request["source_epoch"],
            "target_epoch_id": fence["target_epoch_id"],
            "manifest_digest": request["manifest_digest"],
            "artifact_id": request["artifact_id"],
            "restore_report_digest": _canonical_digest(restore),
            "health_report_digest": _canonical_digest(health),
            "epoch_number": fence["epoch_number"],
            "completed_at": _now(),
        }
    if action == "health":
        _report(request, "health_report", "healthy", require_checks=True)
        health = executor.health(request)
        return {
            "status": "completed",
            "operation": action,
            "task_id": request["task_id"],
            "target_id": request["target_id"],
            "manifest_digest": request["manifest_digest"],
            "artifact_id": request["artifact_id"],
            "health_report_digest": _canonical_digest(health),
            "completed_at": _now(),
        }
    if action == "fence":
        _fence(request)
        fence = executor.fence(request)
        return {
            "status": "completed",
            "operation": action,
            "task_id": request["task_id"],
            "target_id": request["target_id"],
            "source_epoch": request["source_epoch"],
            "target_epoch_id": fence["target_epoch_id"],
            "epoch_number": fence["epoch_number"],
            "completed_at": _now(),
        }
    if action == "unfence":
        _fence(request)
        return executor.unfence(request)
    raise RunnerError("unsupported_operation")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _private_directory(DATA_ROOT, "data")
    _private_directory(path.parent, "status", create=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RunnerError("status_path_invalid")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
        raise


def run_request(request_id: str, action: str) -> dict[str, Any]:
    _safe_id(request_id, "request_id")
    if action == "status":
        _private_directory(DATA_ROOT, "data")
        _private_directory(DATA_ROOT / "status", "status")
        status = DATA_ROOT / "status" / f"{request_id}.json"
        if not status.exists() or status.is_symlink() or not status.is_file():
            return {"status": "not_found"}
        try:
            value = json.loads(status.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise RunnerError("status_invalid") from error
        if not isinstance(value, dict):
            raise RunnerError("status_invalid")
        return value
    request = _read_request(request_id)
    result = _validate_request(request, action)
    _write_json(DATA_ROOT / "status" / f"{request_id}.json", result)
    request_path = DATA_ROOT / "requests" / f"{request_id}.json"
    _private_regular(request_path, "request")
    request_path.unlink()
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 3 or arguments[0] != "--request-id":
        print(
            "usage: noyra-migration-runner.py --request-id SAFE_ID "
            "{status|restore|health|fence|unfence}",
            file=sys.stderr,
        )
        return 2
    request_id, action = arguments[1:]
    try:
        print(json.dumps(run_request(request_id, action), sort_keys=True))
        return 0
    except RunnerError as error:
        with suppress(Exception):
            _write_json(
                DATA_ROOT / "status" / f"{request_id}.json",
                {
                    "status": "failed",
                    "operation": action,
                    "request_id": request_id,
                    "error_code": error.error_code,
                    "updated_at": _now(),
                },
            )
        print(f"migration {action} failed: {error.error_code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
