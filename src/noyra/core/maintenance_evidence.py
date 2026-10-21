"""Exact POSIX boundary for machine-local privileged GC evidence."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

EVIDENCE_DIRECTORY = ".noyra-maintenance"
EVIDENCE_NAME = re.compile(
    r"\.[A-Za-z0-9._-]+\.backup-retention-[A-Za-z0-9_]+\.(?:manifest|index)\Z"
)
TEMP_EVIDENCE_NAME = re.compile(
    r"\.\.[A-Za-z0-9._-]+\.backup-retention-[A-Za-z0-9_]+\.(?:manifest|index)\.[a-z0-9_]{8}\Z"
)


def validate_evidence_file(path: Path, device: int, *, owner: int = 0) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_dev != device
        or getattr(metadata, "st_file_attributes", 0) & 0x400
        or (
            os.name == "posix"
            and (metadata.st_uid != owner or stat.S_IMODE(metadata.st_mode) != 0o600)
        )
    ):
        raise ValueError("backup_retention_evidence_file_unsafe")


def validate_evidence_directory(data_root: Path, *, owner: int = 0) -> None:
    root = data_root / EVIDENCE_DIRECTORY
    data = data_root.stat()
    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != data.st_dev
        or getattr(metadata, "st_file_attributes", 0) & 0x400
        or (
            os.name == "posix"
            and (
                metadata.st_uid != owner
                or metadata.st_gid != data.st_gid
                or stat.S_IMODE(metadata.st_mode) != 0o750
            )
        )
    ):
        raise ValueError("backup_retention_evidence_directory_unsafe")
    for child in root.iterdir():
        if not (EVIDENCE_NAME.fullmatch(child.name) or TEMP_EVIDENCE_NAME.fullmatch(child.name)):
            raise ValueError("backup_retention_evidence_layout_invalid")
        try:
            validate_evidence_file(child, data.st_dev, owner=owner)
        except FileNotFoundError:
            continue  # Atomic publication/unlink may race inspection.


def privileged_maintenance_entry(data_root: Path, path: Path) -> bool:
    if os.name != "posix":
        return False
    if path == data_root / EVIDENCE_DIRECTORY:
        validate_evidence_directory(data_root)
        return True
    # A precise compatibility exception until root relocates old evidence.
    if EVIDENCE_NAME.fullmatch(path.name) or TEMP_EVIDENCE_NAME.fullmatch(path.name):
        validate_evidence_file(path, data_root.stat().st_dev)
        return True
    return False
