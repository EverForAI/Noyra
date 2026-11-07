"""Prepare backup rotation evidence without publishing the main keyring."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from .at_rest import BackupKeyMaterial, BackupKeyring, _keyring_permission_error
from .backup_keyring_paths import (
    BackupRotationBoundary,
    BackupRotationLocation,
    BackupRotationPathError,
)
from .backup_rotation_records import (
    DURABILITY_PROFILE,
    MAX_KEYRING_BYTES,
    MAX_OPERATIONS,
    RotationAcceptance,
    RotationConflictError,
    RotationHeader,
    RotationIntent,
    RotationJournalError,
    RotationOutcomeUnknownError,
    RotationPreparationUnknownError,
    RotationUnsupportedError,
    keyring_bytes,
    sha256,
    validate_generation,
    validate_hash,
    validate_id,
)
from .backup_rotation_storage import RotationStorage, supported_preparation
from .locking import ProcessLock

__all__ = [
    "BackupRotationJournal",
    "RotationConflictError",
    "RotationJournalError",
    "RotationOutcomeUnknownError",
    "RotationPreparationUnknownError",
]


@dataclass(frozen=True)
class RotationJournalStatus:
    state: str
    generation: int
    request_id: str
    slot_id: str | None = None
    target_hash: str | None = None
    intent_hash: str | None = None
    durability_confirmed: bool = False
    profile: str | None = None
    target_generation: int | None = None
    observed_keyring_hash: str | None = None


@dataclass(frozen=True)
class _Catalog:
    header: RotationHeader
    raw_header: bytes
    intents: dict[str, tuple[RotationIntent, bytes]] = field(repr=False)
    accepted: frozenset[str]


def _catalog(values: dict[str, bytes], binding: str) -> _Catalog:
    if "header.json" not in values:
        raise RotationJournalError("rotation journal header is missing or incomplete")
    header = RotationHeader.from_bytes(values["header.json"])
    if header.target_binding != binding:
        raise RotationJournalError("rotation journal target binding conflicts")
    intents: dict[str, tuple[RotationIntent, bytes]] = {}
    accepted: set[str] = set()
    for name, raw in values.items():
        if name.endswith(".intent.json"):
            intent = RotationIntent.from_bytes(raw)
            if (
                name != intent.filename
                or intent.slot_id != header.slot_id
                or intent.header_hash != sha256(values["header.json"])
            ):
                raise RotationJournalError("rotation intent filename or slot binding conflicts")
            intents[intent.request_id] = intent, raw
    if len(intents) > MAX_OPERATIONS:
        raise RotationJournalError("rotation journal operation limit reached")
    for name, raw in values.items():
        if name.endswith(".accepted.json"):
            receipt = RotationAcceptance.from_bytes(raw)
            entry = intents.get(receipt.request_id)
            if entry is None or name != entry[0].filename.replace(".intent.", ".accepted."):
                raise RotationJournalError("rotation acceptance has no matching intent")
            intent, source = entry
            if receipt.intent_hash != sha256(source) or receipt.target_hash != intent.target_hash:
                raise RotationJournalError("rotation acceptance binding conflicts")
            accepted.add(receipt.request_id)
    previous: RotationIntent | None = None
    for intent, _ in sorted(intents.values(), key=lambda entry: entry[0].expected_generation):
        if previous is not None and (
            intent.expected_generation != previous.expected_generation + 1
            or intent.source_hash != previous.target_hash
            or previous.request_id not in accepted
        ):
            raise RotationJournalError("rotation journal predecessor chain conflicts")
        previous = intent
    stages = [(name, raw) for name, raw in values.items() if name.endswith(".tmp")]
    if len(stages) > 1:
        raise RotationJournalError("rotation journal has too many publication stages")
    latest = (
        max(intents.values(), key=lambda entry: entry[0].expected_generation)[0]
        if intents
        else None
    )
    for name, raw in stages:
        if latest is None:
            raise RotationJournalError("rotation stage has no matching intent")
        stem = latest.filename.removesuffix(".intent.json")
        if name == stem + ".accept.tmp":
            expected = RotationAcceptance(
                sha256(intents[latest.request_id][1]), latest.target_hash, latest.request_id
            ).to_bytes()
        else:
            raise RotationJournalError("rotation stage request binding conflicts")
        if not expected.startswith(raw):
            raise RotationJournalError("rotation stage content conflicts")
    return _Catalog(header, values["header.json"], intents, frozenset(accepted))


def _target(source: bytes) -> bytes:
    ring = BackupKeyring._from_bytes(source)
    if len(ring._keys) >= 64:
        raise RotationJournalError("backup keyring reached its retained-key limit")
    key = os.urandom(32)
    fingerprint = hashlib.sha256(key).hexdigest()
    active = f"backup-{ring.generation + 1}-{fingerprint[:12]}"
    keys = tuple(
        BackupKeyMaterial(k.key_id, k.key, k.fingerprint, "retired") for k in ring._keys.values()
    )
    return keyring_bytes(
        BackupKeyring(
            generation=ring.generation + 1,
            active_key_id=active,
            keys=(*keys, BackupKeyMaterial(active, key, fingerprint, "active")),
        )
    )


class BackupRotationJournal:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    @staticmethod
    def _binding(location: BackupRotationLocation) -> str:
        return sha256(str(location.target).encode("utf-8"))

    def status(self, *, request_id: str) -> RotationJournalStatus:
        request_id = validate_id(request_id)
        try:
            location = BackupRotationLocation(self.path, permission_error=_keyring_permission_error)
            source = location.read_current(MAX_KEYRING_BYTES)
            ring = BackupKeyring._from_bytes(source)
            storage = RotationStorage(location)
            values = storage.catalog()
            if values is None:
                return RotationJournalStatus(
                    "absent", ring.generation, request_id, observed_keyring_hash=sha256(source)
                )
            catalog = _catalog(values, self._binding(location))
            entry = catalog.intents.get(request_id)
            if entry is None:
                return RotationJournalStatus(
                    "request_missing",
                    ring.generation,
                    request_id,
                    catalog.header.slot_id,
                    observed_keyring_hash=sha256(source),
                )
            intent, raw = entry
            state = "conflict"
            current_hash = sha256(source)
            if current_hash == intent.source_hash:
                state = "conflict" if request_id in catalog.accepted else "prepared_recorded"
            elif current_hash == intent.target_hash:
                state = (
                    "accepted_recorded"
                    if request_id in catalog.accepted
                    else "target_visible_unconfirmed"
                )
            elif (
                request_id in catalog.accepted and ring.generation > intent.expected_generation + 1
            ):
                previous = BackupKeyring._from_bytes(intent.target)
                if all(
                    key.key_id in ring._keys and ring._keys[key.key_id].key == key.key
                    for key in previous._keys.values()
                ):
                    state = "accepted_superseded"
            if location.read_current(MAX_KEYRING_BYTES) != source:
                raise RotationJournalError("backup keyring changed during journal inspection")
            return RotationJournalStatus(
                state,
                ring.generation,
                request_id,
                intent.slot_id,
                intent.target_hash,
                sha256(raw),
                target_generation=intent.expected_generation + 1,
                observed_keyring_hash=sha256(source),
            )
        except (BackupRotationPathError, OSError) as error:
            raise RotationJournalError(
                "rotation journal inspection boundary is unavailable"
            ) from error

    def rotate(
        self,
        *,
        request_id: str,
        expected_generation: int,
        expected_source_hash: str,
    ) -> RotationJournalStatus:
        """Prepare once, or resume the same bound request without reinterpreting it."""
        request_id = validate_id(request_id)
        expected_generation = validate_generation(expected_generation)
        expected_source_hash = validate_hash(expected_source_hash)
        if not supported_preparation():
            raise RotationUnsupportedError("rotation requires POSIX file and directory durability")
        # A read-only lookup may discover a completed or partially published
        # request whose original source is no longer the current keyring.
        # Never replace that source expectation with freshly observed values.
        try:
            location = BackupRotationLocation(self.path, permission_error=_keyring_permission_error)
            values = RotationStorage(location).catalog()
        except (OSError, BackupRotationPathError) as error:
            raise RotationConflictError(
                "rotation request lookup storage is unavailable or unsafe"
            ) from error
        if values:
            catalog = _catalog(values, self._binding(location))
            entry = catalog.intents.get(request_id)
            if entry is not None:
                intent, _ = entry
                if (intent.expected_generation, intent.source_hash) != (
                    expected_generation,
                    expected_source_hash,
                ):
                    raise RotationConflictError(
                        "rotation request is bound to another expected source"
                    )
            else:
                self.prepare(
                    request_id=request_id,
                    expected_generation=expected_generation,
                    expected_source_hash=expected_source_hash,
                )
        else:
            self.prepare(
                request_id=request_id,
                expected_generation=expected_generation,
                expected_source_hash=expected_source_hash,
            )
        return self.resume(request_id=request_id)

    def resume(self, *, request_id: str) -> RotationJournalStatus:
        """Publish only an existing intent; retry never prepares another key."""
        from .backup_rotation_publication import resume

        return resume(self.path, request_id=request_id)

    def prepare(
        self, *, request_id: str, expected_generation: int, expected_source_hash: str
    ) -> RotationJournalStatus:
        request_id = validate_id(request_id)
        expected_generation = validate_generation(expected_generation)
        expected_source_hash = validate_hash(expected_source_hash)
        if not supported_preparation():
            raise RotationUnsupportedError(
                "rotation preparation requires POSIX file and directory durability"
            )
        try:
            boundary = BackupRotationBoundary(self.path, permission_error=_keyring_permission_error)
            lock = ProcessLock(boundary.lock_path, opener=boundary.open_lock)
            try:
                lock.acquire()
                source = boundary.read(MAX_KEYRING_BYTES)
                ring = BackupKeyring._from_bytes(source)
                if (ring.generation, sha256(source)) != (expected_generation, expected_source_hash):
                    raise RotationJournalError("rotation expected source version conflicts")
                if len(ring._keys) >= 64:
                    raise RotationJournalError("backup keyring reached its retained-key limit")
                storage = RotationStorage(boundary, lock)
                values = storage.catalog()
                if values is None or not values:
                    storage.ensure()
                    header = RotationHeader(os.urandom(16).hex(), self._binding(boundary))
                    storage.create("header.json", header.to_bytes(), request_id=request_id)
                    values = storage.catalog()
                assert values is not None
                catalog = _catalog(values, self._binding(boundary))
                storage.ensure()
                storage.confirm("header.json")
                if request_id in catalog.intents:
                    intent, raw = catalog.intents[request_id]
                    if (intent.expected_generation, intent.source_hash) != (
                        expected_generation,
                        expected_source_hash,
                    ):
                        raise RotationJournalError("rotation request ID is bound to another source")
                    if request_id in catalog.accepted:
                        raise RotationJournalError(
                            "accepted rotation source regressed; refusing prepare"
                        )
                else:
                    if any(name.endswith(".tmp") for name in values):
                        raise RotationJournalError("rotation publication recovery is still pending")
                    if set(catalog.intents) - catalog.accepted:
                        raise RotationJournalError("another rotation request is still pending")
                    if len(catalog.intents) >= MAX_OPERATIONS:
                        raise RotationJournalError("rotation journal operation limit reached")
                    if catalog.intents:
                        last = max(
                            catalog.intents.values(), key=lambda entry: entry[0].expected_generation
                        )[0]
                        if (expected_generation, expected_source_hash) != (
                            last.expected_generation + 1,
                            last.target_hash,
                        ):
                            raise RotationJournalError(
                                "rotation journal predecessor chain conflicts"
                            )
                    target = _target(source)
                    intent = RotationIntent(
                        request_id,
                        catalog.header.slot_id,
                        sha256(catalog.raw_header),
                        expected_generation,
                        sha256(source),
                        sha256(target),
                        source,
                        target,
                    )
                    raw = intent.to_bytes()
                    storage.create(intent.filename, raw, request_id=request_id)
                storage.ensure()
                storage.confirm("header.json")
                storage.confirm(intent.filename)
                boundary.validate()
                verified = storage.catalog()
                if verified is None or storage.read(intent.filename) != raw:
                    raise RotationJournalError("rotation preparation readback conflicts")
                _catalog(verified, self._binding(boundary))
                return RotationJournalStatus(
                    "prepared",
                    ring.generation,
                    request_id,
                    intent.slot_id,
                    intent.target_hash,
                    sha256(raw),
                    True,
                    DURABILITY_PROFILE,
                    intent.expected_generation + 1,
                    sha256(source),
                )
            finally:
                lock.release()
        except RotationPreparationUnknownError:
            raise
        except OSError as error:
            raise RotationPreparationUnknownError(request_id) from error
        except BackupRotationPathError as error:
            raise RotationJournalError("rotation preparation path boundary is unsafe") from error
