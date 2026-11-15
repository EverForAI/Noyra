"""Explicit backup-key administration with bounded, secret-free JSON results."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO

from .core.at_rest import BackupKeyring, BackupKeyUnavailableError
from .core.backup_keyring_paths import BackupRotationPathError
from .core.backup_rotation import BackupRotationJournal, RotationJournalStatus
from .core.backup_rotation_records import (
    RotationOutcomeUnknownError,
    RotationPendingRecoveryUnknownError,
    RotationPreparationUnknownError,
    RotationUnsupportedError,
    validate_generation,
    validate_hash,
    validate_id,
)
from .core.backup_rotation_storage import supported_preparation
from .core.errors import RuntimeOwnershipError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m noyra backup-key")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("init", "status", "prepare", "resume", "rotate", "recover-pending"):
        command = sub.add_parser(action)
        command.add_argument("--path", required=True, type=Path)
        if action not in {"init", "recover-pending"}:
            command.add_argument(
                "--request-id",
                required=True,
                help="Retain this lowercase 128-bit hex ID for all retries",
            )
        if action in {"prepare", "rotate"}:
            command.add_argument("--expected-generation", type=int, required=True)
            command.add_argument("--expected-source-hash", required=True)
        if action == "recover-pending":
            command.add_argument("--expected-pending-fingerprint", required=True)
    return parser


def _emit(value: dict[str, Any], stream: TextIO) -> None:
    print(json.dumps(value, sort_keys=True, ensure_ascii=True), file=stream, flush=True)


def _result(value: RotationJournalStatus) -> dict[str, Any]:
    return {"event": "result", **asdict(value)}


def run(arguments: list[str]) -> None:
    parser = _parser()
    options = parser.parse_args(arguments)
    action: str = options.action
    if action == "init":
        # Preserve the existing initializer and its single metadata response.
        ring = BackupKeyring.initialize(options.path)
        _emit(ring.metadata(), sys.stdout)
        return
    request_id: str | None = None
    try:
        if action != "recover-pending":
            request_id = validate_id(options.request_id)
        if action in {"prepare", "rotate"}:
            validate_generation(options.expected_generation)
            validate_hash(options.expected_source_hash)
        if action == "recover-pending":
            validate_hash(options.expected_pending_fingerprint)
    except BackupKeyUnavailableError:
        parser.error("invalid request ID, expectation or pending fingerprint")
    api = BackupRotationJournal(options.path)
    try:
        # Flush mutation identity before touching storage; a closed stdout must
        # not leave an unreported keyring replacement or cleanup operation.
        if action in {"prepare", "rotate", "resume", "recover-pending"}:
            requested: dict[str, Any] = {
                "event": "request",
                "action": action,
            }
            if request_id is not None:
                requested["request_id"] = request_id
            if action in {"prepare", "rotate"}:
                requested.update(
                    expected_generation=options.expected_generation,
                    expected_source_hash=options.expected_source_hash,
                )
            if action == "recover-pending":
                requested["expected_pending_fingerprint"] = (
                    options.expected_pending_fingerprint
                )
            _emit(requested, sys.stdout)
        if action != "status" and not supported_preparation():
            raise RotationUnsupportedError("required durable publication profile is unavailable")
        if action == "status":
            assert request_id is not None
            result = api.status(request_id=request_id)
        elif action == "recover-pending":
            recovery = api.recover_pending(
                expected_fingerprint=options.expected_pending_fingerprint
            )
            _emit({"event": "result", **asdict(recovery)}, sys.stdout)
            return
        elif action == "prepare":
            assert request_id is not None
            result = api.prepare(
                request_id=request_id,
                expected_generation=options.expected_generation,
                expected_source_hash=options.expected_source_hash,
            )
        elif action == "resume":
            assert request_id is not None
            result = api.resume(request_id=request_id)
        else:
            assert request_id is not None
            # The preparation summary is flushed before the first publication
            # attempt. Existing request replay skips prepare's source-current
            # requirement, but still checks the original expectation binding.
            state = api.status(request_id=request_id)
            if state.state in {"absent", "request_missing"}:
                prepared = api.prepare(
                    request_id=request_id,
                    expected_generation=options.expected_generation,
                    expected_source_hash=options.expected_source_hash,
                )
                _emit({"event": "prepared", **asdict(prepared)}, sys.stdout)
            result = api.rotate(
                request_id=request_id,
                expected_generation=options.expected_generation,
                expected_source_hash=options.expected_source_hash,
            )
        _emit(_result(result), sys.stdout)
    except RotationUnsupportedError:
        _emit(
            {
                "event": "error",
                "code": "unsupported",
                **({"request_id": request_id} if request_id else {}),
                "message": (
                    "required POSIX durability is unavailable; "
                    "no journal mutation attempted"
                ),
            },
            sys.stderr,
        )
        raise SystemExit(5) from None
    except (
        RotationOutcomeUnknownError,
        RotationPreparationUnknownError,
        RotationPendingRecoveryUnknownError,
    ) as error:
        _emit(
            {
                "event": "error",
                "code": "outcome_unknown",
                **({"request_id": request_id} if request_id else {}),
                "message": (
                    "inspect the pending fingerprint before retrying recovery"
                    if isinstance(error, RotationPendingRecoveryUnknownError)
                    else "inspect or resume the same request; do not start another rotation"
                ),
            },
            sys.stderr,
        )
        raise SystemExit(3) from None
    except RuntimeOwnershipError:
        _emit(
            {
                "event": "error",
                "code": "busy",
                **({"request_id": request_id} if request_id else {}),
                "message": "another operation owns the rotation lock; retry the same request",
            },
            sys.stderr,
        )
        raise SystemExit(6) from None
    except (BackupKeyUnavailableError, BackupRotationPathError):
        _emit(
            {
                "event": "error",
                "code": "conflict",
                **({"request_id": request_id} if request_id else {}),
                "message": "stored evidence, source expectation or private storage conflicts",
            },
            sys.stderr,
        )
        raise SystemExit(4) from None
    except OSError:
        # Includes stdout failure after a completed mutation: retain the ID and
        # never claim an I/O exception proves that no side effect occurred.
        _emit(
            {
                "event": "error",
                "code": "outcome_unknown",
                **({"request_id": request_id} if request_id is not None else {}),
                "message": "I/O or response failed; inspect the same retained request",
            },
            sys.stderr,
        )
        raise SystemExit(3) from None
