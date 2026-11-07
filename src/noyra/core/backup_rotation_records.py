"""Strict, bounded private backup-rotation records. Snapshots contain secrets."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .at_rest import BackupKeyring, BackupKeyUnavailableError
from .types import canonical_json

HEADER_FORMAT = "noyra-backup-rotation-journal/v1"
INTENT_FORMAT = "noyra-backup-rotation-intent/v1"
RECEIPT_FORMAT = "noyra-backup-rotation-acceptance/v1"
DURABILITY_PROFILE = "posix-file-and-directory-fsync/v1"
MAX_INTENT_BYTES = 2 * 1024 * 1024
MAX_METADATA_BYTES = 4096
MAX_KEYRING_BYTES = 1024 * 1024
MAX_OPERATIONS = 63
MAX_ENTRIES = 1 + 2 * MAX_OPERATIONS


class RotationJournalError(BackupKeyUnavailableError):
    """A fixed diagnostic without key bytes, command text or local paths."""


class RotationPreparationUnknownError(RotationJournalError):
    """Preparation may be visible; the main keyring has not been published."""

    def __init__(self, request_id: str):
        super().__init__(
            "rotation preparation durability is unknown; inspect or retry same request"
        )
        self.request_id = request_id


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def validate_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{32}", value) is None:
        raise RotationJournalError("rotation request or slot ID is invalid")
    return value


def validate_hash(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise RotationJournalError("rotation content hash is invalid")
    return value


def validate_generation(value: object) -> int:
    if type(value) is not int or not 1 <= value < 2**63 - 1:
        raise RotationJournalError("rotation expected generation is invalid")
    return value


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RotationJournalError("rotation record has duplicate fields")
        result[key] = value
    return result


def _decode(raw: bytes, keys: set[str], limit: int) -> dict[str, Any]:
    if not raw or len(raw) > limit:
        raise RotationJournalError("rotation record exceeds its byte budget")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
        if not isinstance(value, dict) or set(value) != keys:
            raise RotationJournalError("rotation record fields are invalid")
        if encode(value) != raw:
            raise RotationJournalError("rotation record encoding is not canonical")
    except (ValueError, UnicodeError, TypeError, RecursionError) as error:
        raise RotationJournalError("rotation record is malformed") from error
    return value


def encode(value: dict[str, Any]) -> bytes:
    return canonical_json(value).encode("utf-8")


def _snapshot(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > 4 * ((MAX_KEYRING_BYTES + 2) // 3):
        raise RotationJournalError("rotation key snapshot is invalid")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
        if base64.b64encode(raw).decode("ascii") != value or len(raw) > MAX_KEYRING_BYTES:
            raise ValueError()
        BackupKeyring._from_bytes(raw)
    except (
        ValueError,
        UnicodeError,
        TypeError,
        KeyError,
        RecursionError,
        BackupKeyUnavailableError,
    ) as error:
        raise RotationJournalError("rotation key snapshot is invalid") from error
    return raw


def keyring_bytes(ring: BackupKeyring) -> bytes:
    return encode(
        {
            "format": "noyra-backup-keyring/v1",
            "generation": ring.generation,
            "active_key_id": ring.active_key_id,
            "keys": [
                {
                    "key_id": key.key_id,
                    "status": key.status,
                    "key_b64": base64.urlsafe_b64encode(key.key).decode("ascii"),
                }
                for key in sorted(ring._keys.values(), key=lambda item: item.key_id)
            ],
        }
    )


@dataclass(frozen=True)
class RotationHeader:
    slot_id: str
    target_binding: str

    def to_bytes(self) -> bytes:
        return encode(
            {
                "format": HEADER_FORMAT,
                "slot_id": validate_id(self.slot_id),
                "target_binding": validate_hash(self.target_binding),
            }
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> RotationHeader:
        value = _decode(raw, {"format", "slot_id", "target_binding"}, MAX_METADATA_BYTES)
        if value["format"] != HEADER_FORMAT:
            raise RotationJournalError("rotation journal format is unsupported")
        return cls(validate_id(value["slot_id"]), validate_hash(value["target_binding"]))


@dataclass(frozen=True)
class RotationIntent:
    request_id: str
    slot_id: str
    header_hash: str
    expected_generation: int
    source_hash: str
    target_hash: str
    source: bytes = field(repr=False)
    target: bytes = field(repr=False)

    @property
    def filename(self) -> str:
        return sha256(validate_id(self.request_id).encode("ascii")) + ".intent.json"

    def metadata(self) -> dict[str, str | int]:
        return {
            "request_id": self.request_id,
            "slot_id": self.slot_id,
            "expected_generation": self.expected_generation,
            "source_hash": self.source_hash,
            "target_generation": self.expected_generation + 1,
            "target_hash": self.target_hash,
        }

    def to_bytes(self) -> bytes:
        value: dict[str, Any] = {
            "format": INTENT_FORMAT,
            **self.metadata(),
            "header_hash": self.header_hash,
            "source_b64": base64.b64encode(self.source).decode("ascii"),
            "target_b64": base64.b64encode(self.target).decode("ascii"),
        }
        raw = encode(value)
        self.from_bytes(raw)
        return raw

    @classmethod
    def from_bytes(cls, raw: bytes) -> RotationIntent:
        value = _decode(
            raw,
            {
                "format",
                "request_id",
                "slot_id",
                "header_hash",
                "expected_generation",
                "source_hash",
                "target_generation",
                "target_hash",
                "source_b64",
                "target_b64",
            },
            MAX_INTENT_BYTES,
        )
        if value["format"] != INTENT_FORMAT:
            raise RotationJournalError("rotation intent format is unsupported")
        generation = validate_generation(value["expected_generation"])
        if (
            type(value["target_generation"]) is not int
            or value["target_generation"] != generation + 1
        ):
            raise RotationJournalError("rotation intent target generation is invalid")
        source, target = _snapshot(value["source_b64"]), _snapshot(value["target_b64"])
        old, new = BackupKeyring._from_bytes(source), BackupKeyring._from_bytes(target)
        if old.generation != generation or new.generation != generation + 1:
            raise RotationJournalError("rotation intent generations conflict")
        if (sha256(source), sha256(target)) != (
            validate_hash(value["source_hash"]),
            validate_hash(value["target_hash"]),
        ):
            raise RotationJournalError("rotation intent snapshot hash conflicts")
        if len(new._keys) != len(old._keys) + 1 or new.active_key_id in old._keys:
            raise RotationJournalError("rotation intent must add exactly one new active key")
        for key in old._keys.values():
            try:
                kept = new.resolve(key.key_id, key.fingerprint)
            except BackupKeyUnavailableError as error:
                raise RotationJournalError("rotation intent changes a retained key") from error
            if kept.key != key.key or kept.status != "retired":
                raise RotationJournalError("rotation intent changes a retained key")
        return cls(
            validate_id(value["request_id"]),
            validate_id(value["slot_id"]),
            validate_hash(value["header_hash"]),
            generation,
            sha256(source),
            sha256(target),
            source,
            target,
        )


@dataclass(frozen=True)
class RotationAcceptance:
    intent_hash: str
    target_hash: str
    request_id: str

    def to_bytes(self) -> bytes:
        return encode(
            {
                "format": RECEIPT_FORMAT,
                "intent_hash": validate_hash(self.intent_hash),
                "target_hash": validate_hash(self.target_hash),
                "request_id": validate_id(self.request_id),
                "profile": DURABILITY_PROFILE,
            }
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> RotationAcceptance:
        value = _decode(
            raw,
            {"format", "intent_hash", "target_hash", "request_id", "profile"},
            MAX_METADATA_BYTES,
        )
        if value["format"] != RECEIPT_FORMAT or value["profile"] != DURABILITY_PROFILE:
            raise RotationJournalError("rotation acceptance profile or format is unsupported")
        return cls(
            validate_hash(value["intent_hash"]),
            validate_hash(value["target_hash"]),
            validate_id(value["request_id"]),
        )
