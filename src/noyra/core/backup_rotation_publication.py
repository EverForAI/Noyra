"""Publish one existing POSIX rotation intent; no new keys are allocated here."""

from __future__ import annotations

import os
from pathlib import Path

from . import backup_rotation_target as target_staging
from .at_rest import (
    BackupKeyring,
    BackupKeyUnavailableError,
    _fsync_directory,
    _keyring_permission_error,
)
from .backup_keyring_paths import (
    BackupRotationBoundary,
    BackupRotationLocation,
    BackupRotationPathError,
    _metadata,
    _revision,
)
from .backup_rotation import RotationJournalStatus, _catalog
from .backup_rotation_records import (
    DURABILITY_PROFILE,
    MAX_KEYRING_BYTES,
    RotationAcceptance,
    RotationConflictError,
    RotationIntent,
    RotationOutcomeUnknownError,
    RotationPreparationUnknownError,
    RotationUnsupportedError,
    sha256,
    validate_id,
)
from .backup_rotation_storage import RotationStorage, supported_preparation
from .locking import ProcessLock


def checkpoint(name: str) -> None:
    """A no-op fault boundary; production never executes user-provided hooks."""


def _publish(storage: RotationStorage, intent: RotationIntent) -> None:
    location = storage.location
    pinned = BackupRotationBoundary(location.raw, permission_error=_keyring_permission_error)
    if pinned.read(MAX_KEYRING_BYTES) != intent.source:
        raise RotationConflictError("rotation source changed before publication")
    stage, identity = target_staging.prepare(location, pinned, intent)
    checkpoint("target_stage_synced")
    checkpoint("before_target_replace")
    location.validate()
    pinned.before_replace(stage, identity)
    storage._directory()
    os.replace(stage, location.target)
    checkpoint("target_replaced")
    _fsync_directory(location.target.parent)
    checkpoint("target_parent_synced")
    _fsync_directory(storage.root)
    checkpoint("target_stage_directory_synced")


def _confirm_target(storage: RotationStorage, intent: RotationIntent) -> bytes:
    location = storage.location
    current = location.read_current(MAX_KEYRING_BYTES)
    if current != intent.target:
        raise RotationConflictError("rotation target readback conflicts")
    expected = _metadata(location.raw)
    descriptor = os.open(location.raw, os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0)))
    try:
        if _revision(os.fstat(descriptor)) != _revision(expected):
            raise RotationConflictError("rotation target changed before synchronization")
        os.fsync(descriptor)
        if _revision(os.fstat(descriptor)) != _revision(expected):
            raise RotationConflictError("rotation target changed during synchronization")
    finally:
        os.close(descriptor)
    location.validate()
    if _revision(_metadata(location.raw)) != _revision(expected):
        raise RotationConflictError("rotation target changed after synchronization")
    checkpoint("target_file_synced")
    _fsync_directory(location.target.parent)
    _fsync_directory(storage.root)
    checkpoint("target_confirmed")
    if location.read_current(MAX_KEYRING_BYTES) != intent.target:
        raise RotationConflictError("rotation target changed before acceptance")
    return current


def _receipt(
    storage: RotationStorage, intent: RotationIntent, raw: bytes, values: dict[str, bytes]
) -> None:
    name = intent.filename.replace(".intent.json", ".accepted.json")
    stage = intent.filename.replace(".intent.json", ".accept.tmp")
    expected = RotationAcceptance(sha256(raw), intent.target_hash, intent.request_id).to_bytes()
    if name not in values:
        storage.write_stage(stage, expected)
        checkpoint("acceptance_stage_synced")
        storage.link_acceptance(stage, name, expected)
        checkpoint("acceptance_linked")
    if storage.path(stage).exists():
        stored = storage.read(stage)
        if not expected.startswith(stored):
            raise RotationConflictError("rotation acceptance staging conflicts")
        storage.remove_stage(stage, stored)
    checkpoint("acceptance_alias_removed")
    if storage.read(name) != expected:
        raise RotationConflictError("rotation acceptance readback conflicts")
    storage.confirm(name)
    checkpoint("acceptance_confirmed")


def _run(storage: RotationStorage, request_id: str) -> RotationJournalStatus:
    values = storage.catalog()
    if values is None:
        raise RotationConflictError("rotation request has no preparation journal")
    catalog = _catalog(values, sha256(str(storage.location.target).encode("utf-8")))
    entry = catalog.intents.get(request_id)
    if entry is None:
        raise RotationConflictError("rotation request has no existing intent")
    intent, raw = entry
    current = storage.location.read_current(MAX_KEYRING_BYTES)
    ring = BackupKeyring._from_bytes(current)
    latest = max(catalog.intents.values(), key=lambda item: item[0].expected_generation)[0]
    allowed = (
        {latest.target_hash}
        if latest.request_id in catalog.accepted
        else {
            latest.source_hash,
            latest.target_hash,
        }
    )
    if sha256(current) not in allowed:
        raise RotationConflictError("rotation current keyring conflicts with the recorded chain")
    accepted = request_id in catalog.accepted
    if accepted and sha256(current) == intent.source_hash:
        raise RotationConflictError("accepted rotation source regressed")
    superseded = accepted and ring.generation > intent.expected_generation + 1
    if not superseded and sha256(current) not in {intent.source_hash, intent.target_hash}:
        raise RotationConflictError("rotation current version is neither source nor target")
    # Re-establish every preparation barrier before any target mutation.
    storage.ensure()
    storage.confirm("header.json")
    storage.confirm(intent.filename)
    checkpoint("preparation_confirmed")
    if superseded:
        retained = BackupKeyring._from_bytes(intent.target)
        if any(
            key.key_id not in ring._keys or ring._keys[key.key_id].key != key.key
            for key in retained._keys.values()
        ):
            raise RotationConflictError("later rotation does not retain the accepted keys")
        receipt = intent.filename.replace(".intent.json", ".accepted.json")
        storage.confirm(receipt)
        if storage.location.read_current(MAX_KEYRING_BYTES) != current:
            raise RotationConflictError("rotation changed while confirming earlier acceptance")
        return RotationJournalStatus(
            "accepted_superseded",
            ring.generation,
            request_id,
            intent.slot_id,
            intent.target_hash,
            sha256(raw),
            True,
            DURABILITY_PROFILE,
            intent.expected_generation + 1,
            sha256(current),
        )
    if sha256(current) == intent.source_hash:
        _publish(storage, intent)
    _confirm_target(storage, intent)
    target_staging.remove(storage.location, intent)
    _receipt(storage, intent, raw, values)
    final_values = storage.catalog()
    if final_values is None:
        raise RotationConflictError("rotation acceptance directory disappeared")
    final = _catalog(final_values, sha256(str(storage.location.target).encode("utf-8")))
    if request_id not in final.accepted:
        raise RotationConflictError("rotation acceptance evidence is missing")
    observed = storage.location.read_current(MAX_KEYRING_BYTES)
    if observed != intent.target:
        raise RotationConflictError("rotation target changed before successful response")
    checkpoint("accepted_response")
    return RotationJournalStatus(
        "accepted",
        intent.expected_generation + 1,
        request_id,
        intent.slot_id,
        intent.target_hash,
        sha256(raw),
        True,
        DURABILITY_PROFILE,
        intent.expected_generation + 1,
        sha256(observed),
    )


def resume(path: Path | str, *, request_id: str) -> RotationJournalStatus:
    request_id = validate_id(request_id)
    if not supported_preparation():
        raise RotationUnsupportedError(
            "rotation publication requires POSIX file and directory durability"
        )
    try:
        location = BackupRotationLocation(path, permission_error=_keyring_permission_error)
    except (OSError, BackupRotationPathError) as error:
        raise RotationConflictError("rotation publication path is unavailable or unsafe") from error
    lock = ProcessLock(location.lock_path, opener=location.open_lock)
    try:
        try:
            lock.acquire()
        except (OSError, BackupRotationPathError) as error:
            raise RotationConflictError(
                "rotation publication lock is unavailable or unsafe"
            ) from error
        try:
            return _run(RotationStorage(location, lock), request_id)
        except (OSError, BackupRotationPathError, RotationPreparationUnknownError) as error:
            observed = "unavailable"
            try:
                # This bounded read reports visibility only; it cannot create
                # evidence, erase corruption or authorize an alternate target.
                raw = location.read_current(MAX_KEYRING_BYTES)
                observed = "keyring_visible_generation_" + str(
                    BackupKeyring._from_bytes(raw).generation
                )
            except (OSError, ValueError, BackupRotationPathError, BackupKeyUnavailableError):
                pass
            raise RotationOutcomeUnknownError(request_id, observed_state=observed) from error
        except BackupKeyUnavailableError as error:
            if isinstance(error, RotationConflictError):
                raise
            raise RotationConflictError(
                "rotation publication evidence or storage conflicts"
            ) from error
    finally:
        try:
            lock.release()
        except OSError as error:
            raise RotationOutcomeUnknownError(
                request_id, observed_state="lock_release_failed"
            ) from error
