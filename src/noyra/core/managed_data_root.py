"""Fail closed before managed Windows launches can create a subject in the wrong directory."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

_MANAGED = ("NOYRA_MANAGED_INSTALL_ROOT", "NOYRA_MANAGED_DATA_ROOT", "NOYRA_MANAGED_INITIALIZE")
# The parent publishes its PID record after spawning. Both files may appear while
# a deliberately new child is still validating its initially empty directory.
_BOOTSTRAP_FILES = frozenset({".noyra-lifecycle.lock", "noyra.pid"})


def _directory_path(raw: str, label: str) -> Path:
    path = Path(raw)
    if not raw or not path.is_absolute() or path.parent == path:
        raise ValueError(f"managed_data_root: {label} must be an absolute non-root directory")
    if os.name == "nt" and (
        raw.startswith(("\\\\?\\", "\\\\.\\"))
        or any(part.endswith((".", " ")) or ":" in part for part in path.parts[1:])
    ):
        raise ValueError(f"managed_data_root: {label} contains an ambiguous Windows path")
    for component in (path, *path.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError(
                f"managed_data_root: {label} contains a non-directory or filesystem link"
            )
    return path.resolve()


def managed_data_root(
    *, environ: Mapping[str, str] | None = None, configured: Path | None = None
) -> Path | None:
    """Check the lifecycle contract without creating directories or opening SQLite.

    Unmanaged source, Linux and container launches retain their existing settings.
    A managed launch cannot silently fall back to a relative path or a new DB.
    """
    environment = os.environ if environ is None else environ
    if not any(name in environment for name in _MANAGED):
        return None
    if any(not environment.get(name) for name in (*_MANAGED, "NOYRA_DATA_DIR")):
        raise ValueError("managed_data_root: managed launch contract is incomplete")
    install = _directory_path(environment[_MANAGED[0]], "installation root")
    root = _directory_path(environment[_MANAGED[1]], "data root")
    selected = _directory_path(environment["NOYRA_DATA_DIR"], "NOYRA_DATA_DIR")
    if root != selected or (
        configured is not None and _directory_path(str(configured), "configured data root") != root
    ):
        raise ValueError("managed_data_root: runtime data root differs from lifecycle DataRoot")
    if root.is_relative_to(install) or install.is_relative_to(root):
        raise ValueError("managed_data_root: installation and data roots must be disjoint")
    initialization = environment[_MANAGED[2]]
    if initialization not in {"0", "1"}:
        raise ValueError("managed_data_root: initialization permission must be 0 or 1")
    database = root / "noyra.sqlite3"
    try:
        metadata = database.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size == 0
            or bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
        ):
            raise ValueError("managed_data_root: existing database is empty or not a regular file")
        # SQLite schema, subject identity, continuity and ownership remain checked
        # by the existing kernel. This guard never repairs or initializes that DB.
        if initialization == "1":
            raise ValueError(
                "managed_data_root: InitializeDataRoot cannot select an existing database"
            )
        return root
    if initialization != "1":
        raise ValueError(
            "managed_data_root: database missing; "
            "review existing data before explicit initialization"
        )
    if root.exists():
        for entry in root.iterdir():
            metadata = entry.lstat()
            if (
                entry.name not in _BOOTSTRAP_FILES
                or not stat.S_ISREG(metadata.st_mode)
                or bool(
                    getattr(metadata, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
            ):
                raise ValueError(
                    "managed_data_root: initialization requires a directory without data"
                )
    return root
