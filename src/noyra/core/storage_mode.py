"""Safe storage contracts for native deployments.

The native service uses a stable data path, but that path may be an ordinary
directory on the system filesystem or a separately mounted filesystem.  This
module keeps that distinction explicit so a missing mount can never be
silently replaced by a fresh empty database.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast


class StorageMode(StrEnum):
    SYSTEM_DISK = "system_disk"
    DEDICATED_MOUNT = "dedicated_mount"
    UNAVAILABLE = "unavailable"


STORAGE_MARKER_NAME = ".noyra-storage.json"
STORAGE_MARKER_FORMAT = "noyra-storage/v1"
STORAGE_IDENTITY_PATH = Path("/etc/noyra/storage-identity.conf")
STORAGE_IDENTITY_FORMAT = "noyra-storage-identity/v1"
BACKUP_STORAGE_IDENTITY_PATH = Path("/etc/noyra/backup-storage-identity.conf")
BACKUP_STORAGE_IDENTITY_FORMAT = "noyra-backup-storage-identity/v1"


@dataclass(frozen=True)
class StorageLayout:
    """Non-secret storage identity and readiness information."""

    data_root: Path
    backup_root: Path
    upgrade_root: Path
    mode: StorageMode
    data_device: str | None
    backup_device: str | None
    writable: bool
    reason_code: str | None = None
    backup_reason: str | None = None

    @property
    def runtime_ready(self) -> bool:
        """Whether the data root is safe and writable for the running service."""

        return self.mode is not StorageMode.UNAVAILABLE and self.writable

    @property
    def backup_ready(self) -> bool:
        """Whether the configured backup root exists as a usable directory.

        Backup writes are performed by the privileged maintenance boundary, so
        this check intentionally validates path identity and presence without
        requiring the unprivileged service account to write a root-owned path.
        """

        return self.backup_reason is None and self.backup_root.exists() and self.backup_root.is_dir()

    @property
    def backup_reason_code(self) -> str | None:
        if self.backup_ready:
            return None
        return self.backup_reason or "backup_path_missing"

    def public(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "data_device": self.data_device,
            "backup_device": self.backup_device,
            "writable": self.writable,
            "reason_code": self.reason_code,
            "runtime_ready": self.runtime_ready,
            "backup_ready": self.backup_ready,
            "backup_reason_code": self.backup_reason_code,
        }


@dataclass(frozen=True)
class StorageHeadroom:
    available_bytes: int
    runtime_bytes: int
    backup_bytes: int
    upgrade_bytes: int

    @property
    def required_bytes(self) -> int:
        return self.runtime_bytes + self.backup_bytes + self.upgrade_bytes

    @property
    def sufficient(self) -> bool:
        return self.available_bytes >= self.required_bytes

    def public(self) -> dict[str, int | bool]:
        return {
            "available_bytes": self.available_bytes,
            "required_bytes": self.required_bytes,
            "runtime_bytes": self.runtime_bytes,
            "backup_bytes": self.backup_bytes,
            "upgrade_bytes": self.upgrade_bytes,
            "sufficient": self.sufficient,
        }


class StorageUnavailableError(RuntimeError):
    """The configured data path is unsafe or its expected mount is absent."""

    code = "storage_unavailable"


def _reject_reparse_path(path: Path) -> None:
    current = path
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if current.parent == current:
                break
            current = current.parent
            continue
        except OSError as error:
            raise StorageUnavailableError("storage_metadata_unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise StorageUnavailableError("storage_symlink_rejected")
        if not stat.S_ISDIR(metadata.st_mode):
            raise StorageUnavailableError("storage_path_not_directory")
        if current.parent == current:
            break
        current = current.parent


def _findmnt(path: Path) -> tuple[str | None, bool]:
    """Return the mounted source and whether ``path`` is a mountpoint."""
    if os.name != "posix":
        return None, os.path.ismount(path)
    try:
        result = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE,TARGET", "--target", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, os.path.ismount(path)
    if result.returncode != 0:
        return None, os.path.ismount(path)
    fields = result.stdout.strip().split(None, 1)
    if len(fields) != 2:
        return None, os.path.ismount(path)
    source, target = fields
    return source, Path(target).resolve() == path.resolve()


def _read_marker(data_root: Path) -> dict[str, Any] | None:
    marker = data_root / STORAGE_MARKER_NAME
    if marker.is_symlink():
        raise StorageUnavailableError("storage_marker_unsafe")
    if not marker.exists():
        return None
    if not marker.is_file() or marker.stat().st_nlink != 1:
        raise StorageUnavailableError("storage_marker_unsafe")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StorageUnavailableError("storage_marker_invalid") from error
    if not isinstance(payload, dict) or payload.get("format") != STORAGE_MARKER_FORMAT:
        raise StorageUnavailableError("storage_marker_invalid")
    if payload.get("mode") not in {
        StorageMode.SYSTEM_DISK.value,
        StorageMode.DEDICATED_MOUNT.value,
    }:
        raise StorageUnavailableError("storage_marker_invalid")
    return payload


def _native_mount_config(data_root: Path) -> bool:
    """Recognize the legacy native encrypted-image configuration.

    The configuration lives outside the mount, so an unmounted data volume
    cannot be mistaken for a fresh system-disk directory after a reboot.  The
    service account only needs to inspect the trusted file's metadata; the
    root-only unlock helper remains responsible for parsing its contents.
    """
    if data_root != Path("/var/lib/noyra") or os.name != "posix":
        return False
    config = Path("/etc/noyra/storage.conf")
    try:
        metadata = config.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise StorageUnavailableError("storage_config_unreadable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise StorageUnavailableError("storage_config_unsafe")
    if metadata.st_nlink != 1:
        raise StorageUnavailableError("storage_config_unsafe")
    return True


def _external_mount_identity(data_root: Path) -> dict[str, str] | None:
    """Read the root-owned mount identity that survives an unmounted volume."""
    path = STORAGE_IDENTITY_PATH
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StorageUnavailableError("storage_identity_unreadable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (os.name == "posix" and (metadata.st_uid != 0 or metadata.st_mode & 0o022))
    ):
        raise StorageUnavailableError("storage_identity_unsafe")
    try:
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or not key or key in values or not value:
                raise ValueError("invalid storage identity")
            values[key] = value
    except (OSError, UnicodeError, ValueError) as error:
        raise StorageUnavailableError("storage_identity_invalid") from error
    expected = {
        "NOYRA_STORAGE_IDENTITY_FORMAT",
        "NOYRA_STORAGE_DATA_DIR",
        "NOYRA_STORAGE_MODE",
        "NOYRA_STORAGE_SOURCE",
    }
    if (
        set(values) != expected
        or values["NOYRA_STORAGE_IDENTITY_FORMAT"] != STORAGE_IDENTITY_FORMAT
    ):
        raise StorageUnavailableError("storage_identity_invalid")
    if values["NOYRA_STORAGE_MODE"] != StorageMode.DEDICATED_MOUNT.value:
        raise StorageUnavailableError("storage_identity_invalid")
    identity_data_root = Path(values["NOYRA_STORAGE_DATA_DIR"])
    if not identity_data_root.is_absolute():
        raise StorageUnavailableError("storage_identity_invalid")
    if identity_data_root.resolve() != data_root.resolve():
        return None
    return {
        "source": values["NOYRA_STORAGE_SOURCE"],
        "data_dir": values["NOYRA_STORAGE_DATA_DIR"],
    }


def _external_backup_mount_identity(backup_root: Path) -> dict[str, str] | None:
    """Read the optional root-owned identity for a dedicated backup mount."""
    path = BACKUP_STORAGE_IDENTITY_PATH
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StorageUnavailableError("backup_storage_identity_unreadable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (os.name == "posix" and (metadata.st_uid != 0 or metadata.st_mode & 0o022))
    ):
        raise StorageUnavailableError("backup_storage_identity_unsafe")
    try:
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or not key or key in values or not value:
                raise ValueError("invalid backup storage identity")
            values[key] = value
    except (OSError, UnicodeError, ValueError) as error:
        raise StorageUnavailableError("backup_storage_identity_invalid") from error
    expected = {
        "NOYRA_BACKUP_STORAGE_IDENTITY_FORMAT",
        "NOYRA_BACKUP_STORAGE_DIR",
        "NOYRA_BACKUP_STORAGE_MODE",
        "NOYRA_BACKUP_STORAGE_SOURCE",
    }
    if (
        set(values) != expected
        or values["NOYRA_BACKUP_STORAGE_IDENTITY_FORMAT"] != BACKUP_STORAGE_IDENTITY_FORMAT
        or values["NOYRA_BACKUP_STORAGE_MODE"] != StorageMode.DEDICATED_MOUNT.value
    ):
        raise StorageUnavailableError("backup_storage_identity_invalid")
    identity_root = Path(values["NOYRA_BACKUP_STORAGE_DIR"])
    if not identity_root.is_absolute():
        raise StorageUnavailableError("backup_storage_identity_invalid")
    if identity_root.resolve() != backup_root.resolve():
        return None
    return {"source": values["NOYRA_BACKUP_STORAGE_SOURCE"]}


def _fstab_expects_mount(data_root: Path) -> bool:
    """Return whether fstab declares ``data_root`` as a mount target.

    ``findmnt --fstab --target`` also reports the filesystem root for an
    arbitrary path.  Compare the returned target explicitly so a normal
    system-disk install is never classified as a missing dedicated volume.
    """
    if os.name != "posix":
        return False
    try:
        result = subprocess.run(
            ["findmnt", "--fstab", "-n", "-o", "TARGET", "--target", str(data_root)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    target = result.stdout.strip().splitlines()
    return len(target) == 1 and Path(target[0]).resolve() == data_root.resolve()


def _explicit_mount_expectation() -> bool:
    """Read an optional root-owned service declaration for custom mounts."""
    value = os.environ.get("NOYRA_EXPECTED_STORAGE_MODE", "").strip().casefold()
    if value in {"", StorageMode.SYSTEM_DISK.value}:
        return False
    if value == StorageMode.DEDICATED_MOUNT.value:
        return True
    raise StorageUnavailableError("storage_mode_invalid")


def detect_storage_layout(
    data_root: Path | str = "/var/lib/noyra",
    backup_root: Path | str = "/var/backups/noyra",
) -> StorageLayout:
    """Inspect storage without creating files or changing permissions."""
    data = Path(data_root).expanduser()
    backup = Path(backup_root).expanduser()
    for path in (data, backup):
        _reject_reparse_path(path)
        if path is data and (not path.exists() or not path.is_dir()):
            return StorageLayout(
                data,
                backup,
                data / "upgrade",
                StorageMode.UNAVAILABLE,
                None,
                None,
                False,
                "storage_path_missing",
            )
    if not backup.exists() or not backup.is_dir():
        backup_device = None
        backup_is_mount = False
    else:
        backup_device, backup_is_mount = _findmnt(backup)
    marker = _read_marker(data)
    external_identity = _external_mount_identity(data)
    backup_identity = _external_backup_mount_identity(backup)
    data_device, data_is_mount = _findmnt(data)
    dedicated_expected = (
        bool(marker and marker.get("mode") == StorageMode.DEDICATED_MOUNT.value)
        or _native_mount_config(data)
        or external_identity is not None
        or _fstab_expects_mount(data)
        or _explicit_mount_expectation()
    )
    if dedicated_expected and not data_is_mount:
        return StorageLayout(
            data,
            backup,
            data / "upgrade",
            StorageMode.UNAVAILABLE,
            data_device,
            backup_device,
            False,
            "storage_mount_missing",
        )
    if external_identity is not None and (
        not data_is_mount or data_device != external_identity["source"]
    ):
        return StorageLayout(
            data,
            backup,
            data / "upgrade",
            StorageMode.UNAVAILABLE,
            data_device,
            backup_device,
            False,
            "storage_mount_identity_mismatch",
        )
    backup_reason: str | None = None
    if backup_identity is not None and not backup_is_mount:
        backup_reason = "backup_mount_missing"
    elif backup_identity is not None and backup_device != backup_identity["source"]:
        backup_reason = "backup_mount_identity_mismatch"
    elif _fstab_expects_mount(backup) and not backup_is_mount:
        backup_reason = "backup_mount_missing"
    mode = StorageMode.DEDICATED_MOUNT if data_is_mount else StorageMode.SYSTEM_DISK
    try:
        writable = os.access(data, os.W_OK | os.X_OK)
    except OSError:
        writable = False
    return StorageLayout(
        data,
        backup,
        data / "upgrade",
        mode,
        data_device,
        backup_device,
        writable,
        ("backup_path_missing" if not backup.exists() else None)
        if writable
        else "storage_not_writable",
        backup_reason,
    )


def _install_dir(path: Path, *, uid: int, gid: int, mode: int) -> None:
    _reject_reparse_path(path)
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise StorageUnavailableError("storage_path_not_directory")
    else:
        path.mkdir(parents=True, mode=mode)
    geteuid = cast(Any, getattr(os, "geteuid", None))
    chown = cast(Any, getattr(os, "chown", None))
    if os.name == "posix" and callable(geteuid) and geteuid() == 0 and callable(chown):
        chown(path, uid, gid)
    path.chmod(mode)


def ensure_system_disk_layout(
    data_root: Path | str = "/var/lib/noyra",
    backup_root: Path | str = "/var/backups/noyra",
    *,
    service_uid: int | None = None,
    service_gid: int | None = None,
) -> StorageLayout:
    """Create only missing native directories; refuse links and files.

    The caller must be root on POSIX. Existing contents are never removed or
    replaced. The function is intentionally separate from runtime detection.
    """
    if os.name == "posix" and getattr(os, "geteuid", lambda: 1)() != 0:
        raise PermissionError("system disk layout requires root")
    data = Path(data_root).expanduser()
    backup = Path(backup_root).expanduser()
    if service_uid is None or service_gid is None:
        try:
            pwd_module = importlib.import_module("pwd")
            getpwnam = cast(Any, pwd_module.getpwnam)
            account = getpwnam("noyra")
            service_uid = account.pw_uid
            service_gid = account.pw_gid
        except (ImportError, KeyError):
            getuid = cast(Any, getattr(os, "getuid", None))
            getgid = cast(Any, getattr(os, "getgid", None))
            service_uid = getuid() if os.name == "posix" and callable(getuid) else 0
            service_gid = getgid() if os.name == "posix" and callable(getgid) else 0
    _install_dir(data, uid=service_uid, gid=service_gid, mode=0o700)
    _install_dir(backup, uid=0, gid=0, mode=0o700)
    _install_dir(data / "upgrade", uid=0, gid=service_gid, mode=0o750)
    _install_dir(data / "upgrade" / "requests", uid=service_uid, gid=service_gid, mode=0o700)
    _install_dir(data / "upgrade" / "processing", uid=0, gid=0, mode=0o700)
    return detect_storage_layout(data, backup)


def check_headroom(
    path: Path | str,
    *,
    runtime_bytes: int,
    backup_bytes: int,
    upgrade_bytes: int,
) -> StorageHeadroom:
    if min(runtime_bytes, backup_bytes, upgrade_bytes) < 0:
        raise ValueError("headroom estimates must be non-negative")
    available = int(shutil.disk_usage(Path(path)).free)
    return StorageHeadroom(available, runtime_bytes, backup_bytes, upgrade_bytes)
