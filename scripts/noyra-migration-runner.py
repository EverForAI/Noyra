#!/usr/bin/env python3
"""Validate fixed migration receipts at the root-owned execution boundary."""

from __future__ import annotations

import hashlib
import json
import os
import re
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
        "requested_at",
    }
    if set(request) - allowed:
        raise RunnerError("request_fields_invalid")
    for key in ("task_id", "subject_id", "target_id", "source_epoch", "artifact_id"):
        _safe_id(request.get(key), key)
    _digest(request.get("manifest_digest"), "manifest_digest")
    if action == "restore":
        restore = _report(request, "restore_report", "restored")
        health = _report(request, "health_report", "healthy", require_checks=True)
        fence = _fence(request)
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
        health = _report(request, "health_report", "healthy", require_checks=True)
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
        fence = _fence(request)
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
            "usage: noyra-migration-runner.py --request-id SAFE_ID {status|restore|health|fence}",
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
