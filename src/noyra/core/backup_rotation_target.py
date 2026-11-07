"""Same-directory target staging for one previously prepared rotation."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .at_rest import _fsync_directory, _keyring_permission_error, _write_descriptor
from .backup_keyring_paths import (
    BackupRotationBoundary,
    BackupRotationLocation,
    _effective_uid,
    _identity,
    _metadata,
    _revision,
)
from .backup_rotation_records import (
    MAX_KEYRING_BYTES,
    RotationConflictError,
    RotationIntent,
    sha256,
)


def staging_path(location: BackupRotationLocation, intent: RotationIntent) -> Path:
    token = sha256((intent.slot_id + intent.request_id).encode("ascii"))
    return location.target.with_name(".noyra-rotation-" + token + ".publish.tmp")


def _inspect(
    location: BackupRotationLocation, intent: RotationIntent
) -> tuple[os.stat_result, bytes] | None:
    location.validate()
    path = staging_path(location, intent)
    expected = _metadata(path)
    error = _keyring_permission_error(path)
    if error is not None:
        raise RotationConflictError("rotation target staging permissions conflict")
    if not 0 <= expected.st_size <= MAX_KEYRING_BYTES:
        raise RotationConflictError("rotation target staging exceeds its byte budget")
    descriptor = os.open(
        path, os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0)) | int(getattr(os, "O_NONBLOCK", 0))
    )
    try:
        if _revision(os.fstat(descriptor)) != _revision(expected):
            raise RotationConflictError("rotation target stage changed before reading")
        chunks: list[bytes] = []
        remaining = MAX_KEYRING_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if _revision(os.fstat(descriptor)) != _revision(expected):
            raise RotationConflictError("rotation target stage changed while reading")
    finally:
        os.close(descriptor)
    location.validate()
    if _revision(_metadata(path)) != _revision(expected):
        raise RotationConflictError("rotation target stage changed after reading")
    raw = b"".join(chunks)
    if not intent.target.startswith(raw):
        raise RotationConflictError("rotation target staging content conflicts")
    return expected, raw


def inspect(
    location: BackupRotationLocation, intent: RotationIntent
) -> tuple[os.stat_result, bytes] | None:
    path = staging_path(location, intent)
    location.validate()
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or getattr(value, "st_file_attributes", 0) & 0x400
        or value.st_uid != _effective_uid()
        or value.st_dev != location.target.parent.stat().st_dev
    ):
        raise RotationConflictError("rotation target staging identity or owner conflicts")
    return _inspect(location, intent)


def prepare(
    location: BackupRotationLocation, pinned: BackupRotationBoundary, intent: RotationIntent
) -> tuple[Path, tuple[int, int]]:
    stage = staging_path(location, intent)
    previous = inspect(location, intent)
    complete = previous is not None and previous[1] == intent.target
    flags = os.O_RDONLY if complete else os.O_RDWR
    flags |= int(getattr(os, "O_NOFOLLOW", 0)) | int(getattr(os, "O_NONBLOCK", 0))
    if previous is None:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = os.open(stage, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(_metadata(stage))
            or (previous is not None and _revision(opened) != _revision(previous[0]))
        ):
            raise RotationConflictError("rotation target staging identity changed before writing")
        if previous is None:
            chmod = getattr(os, "fchmod", None)
            assert callable(chmod)
            chmod(descriptor, 0o600)
        location.validate()
        pinned.validate()
        if not complete:
            os.ftruncate(descriptor, 0)
            _write_descriptor(descriptor, intent.target)
        os.fsync(descriptor)
        # Apply the pinned source owner/group/mode only after complete bytes
        # exist. The same-parent rename publishes exactly this descriptor.
        pinned.prepare_temporary(descriptor)
        os.fsync(descriptor)
        identity = _identity(os.fstat(descriptor))
    finally:
        os.close(descriptor)
    result = inspect(location, intent)
    if result is None or _identity(result[0]) != identity or result[1] != intent.target:
        raise RotationConflictError("rotation target staging readback conflicts")
    _fsync_directory(location.target.parent)
    return stage, identity


def remove(location: BackupRotationLocation, intent: RotationIntent) -> None:
    previous = inspect(location, intent)
    if previous is None:
        return
    location.validate()
    path = staging_path(location, intent)
    if _revision(_metadata(path)) != _revision(previous[0]):
        raise RotationConflictError("rotation target staging changed before cleanup")
    path.unlink()
    _fsync_directory(location.target.parent)
