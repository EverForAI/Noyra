"""Write a secret-free operator recovery manifest."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _path_metadata(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "kind": "symlink"
        if stat.S_ISLNK(metadata.st_mode)
        else "file"
        if stat.S_ISREG(metadata.st_mode)
        else "directory"
        if stat.S_ISDIR(metadata.st_mode)
        else "other",
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "size": metadata.st_size,
    }


def _schema_version(database_path: Path) -> int | None:
    if not database_path.is_file() or database_path.is_symlink():
        return None
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def build_recovery_manifest(
    *,
    release_id: str,
    source_sha: str | None,
    data_dir: Path,
    backup_dir: Path,
    backup_path: Path | None,
    backup_keyring_path: Path,
    credentials_dir: Path,
    server_identity_path: Path,
    database_path: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a manifest containing recovery references but no secret material."""

    root_device = os.stat("/").st_dev
    try:
        storage_mode = "system_disk" if data_dir.stat().st_dev == root_device else "dedicated_mount"
    except FileNotFoundError:
        storage_mode = "unavailable"
    latest_backup = backup_path or backup_dir
    return {
        "format": "noyra-recovery-manifest/v1",
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="milliseconds"),
        "release": {
            "id": release_id,
            "source_sha": source_sha,
            "schema_version": _schema_version(database_path),
        },
        "storage": {
            "mode": storage_mode,
            "data_dir": str(data_dir),
            "backup_dir": str(backup_dir),
        },
        "paths": {
            "backup_keyring": _path_metadata(backup_keyring_path),
            "credentials_directory": _path_metadata(credentials_dir),
            "server_identity": _path_metadata(server_identity_path),
            "database": _path_metadata(database_path),
            "latest_backup_or_directory": _path_metadata(latest_backup),
        },
        "recovery_notes": [
            "Preserve the backup keyring and credential files before reinstalling the OS.",
            "Restore the database only with the matching backup keyring and a verified backup.",
            "Recreate the service credential references after reinstalling the OS.",
        ],
    }


def write_recovery_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Atomically write a root-only JSON manifest without following symlinks."""

    path = Path(path)
    if not path.is_absolute():
        raise ValueError("recovery manifest path must be absolute")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("recovery manifest path must be a regular file")
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    payload = json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
