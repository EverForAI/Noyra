"""Self-validating atomic integrity evidence, with read compatibility for legacy sidecars."""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .types import content_hash, new_id

COMMIT_KEY = "_noyra_integrity_commit"
COMMIT_FORMAT = "noyra-integrity-commit/v1"
MAX_EVIDENCE_BYTES = 8_000_000
EvidenceStatus = Literal["ok", "missing", "corrupt", "evidence_unavailable"]
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvidenceRead:
    status: EvidenceStatus
    payload: dict[str, Any] | None = None
    source: str = "none"
    reason_code: str = "missing"


def _read(path: Path, maximum: int) -> bytes:
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
        raise ValueError("integrity evidence must be an ordinary file")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("integrity evidence must be an ordinary file")
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("integrity evidence exceeds its size limit")
    return raw


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate integrity evidence field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite integrity evidence")


def read_evidence(path: Path) -> EvidenceRead:
    try:
        raw = _read(path, MAX_EVIDENCE_BYTES)
    except FileNotFoundError:
        return EvidenceRead("missing")
    except OSError:
        return EvidenceRead("evidence_unavailable", reason_code="evidence_read_failed")
    except ValueError:
        return EvidenceRead("corrupt", reason_code="evidence_file_invalid")
    try:
        stored = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
        if not isinstance(stored, dict):
            raise ValueError("integrity evidence must be an object")
        if COMMIT_KEY in stored:
            marker = stored.pop(COMMIT_KEY)
            if (
                not isinstance(marker, dict)
                or set(marker) != {"format", "payload_hash"}
                or marker["format"] != COMMIT_FORMAT
                or marker["payload_hash"] != content_hash(stored)
            ):
                raise ValueError("integrity commit digest mismatch")
            return EvidenceRead("ok", stored, "commit", "verified")
        digest = _read(path.with_suffix(path.suffix + ".sha256"), 128).decode("ascii").strip()
        if len(digest) != 64 or content_hash(stored) != digest:
            raise ValueError("legacy integrity checksum mismatch")
        return EvidenceRead("ok", stored, "legacy_sidecar", "verified_legacy_pair")
    except FileNotFoundError:
        return EvidenceRead(
            "evidence_unavailable", source="legacy_sidecar", reason_code="legacy_digest_missing"
        )
    except OSError:
        return EvidenceRead(
            "evidence_unavailable", source="legacy_sidecar", reason_code="legacy_digest_unreadable"
        )
    except (UnicodeError, ValueError, TypeError, RecursionError):
        return EvidenceRead("corrupt", reason_code="evidence_payload_invalid")


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    if COMMIT_KEY in payload:
        raise ValueError("integrity commit marker is reserved")
    stored = dict(payload) | {
        COMMIT_KEY: {"format": COMMIT_FORMAT, "payload_hash": content_hash(dict(payload))}
    }
    raw = json.dumps(stored, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode(
        "utf-8"
    )
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError("integrity evidence exceeds its size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + new_id("integrity-write") + ".tmp")
    digest_path = path.with_suffix(path.suffix + ".sha256")
    digest_temporary = path.with_name("." + new_id("integrity-digest") + ".tmp")
    try:
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
        # A compatibility view for older readers. New readers verify the
        # same-file marker and never depend on this second publication.
        try:
            descriptor = os.open(digest_temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(content_hash(stored))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(digest_temporary, digest_path)
        except OSError as error:
            LOGGER.warning(
                "Legacy integrity digest projection unavailable: %s", type(error).__name__
            )
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        with suppress(OSError):
            digest_temporary.unlink(missing_ok=True)
