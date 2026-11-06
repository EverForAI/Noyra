"""Descriptor and original-path checks for administrative backup-key rotation.

This boundary trusts the current account and platform administrators, not other
local writers. It does not grant rotation authority to read-only service users.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO


class BackupRotationPathError(ValueError):
    """The original rotation path or one of its identities is unsafe."""


def _effective_uid() -> int:
    function = getattr(os, "geteuid", None)
    if not callable(function):
        raise BackupRotationPathError("backup rotation owner probe is unavailable")
    return int(function())


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    if not metadata.st_ino:
        raise BackupRotationPathError("backup rotation file identity is unavailable")
    return metadata.st_dev, metadata.st_ino


def _revision(metadata: os.stat_result) -> tuple[int, ...]:
    # Windows path and descriptor ctime can have different meanings (notably
    # after replacement). Use inode/size/mtime there and re-probe ACLs separately.
    return (
        *_identity(metadata),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns if os.name != "nt" else 0,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
    )


def _metadata(path: Path, *, directory: bool = False) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as error:
        raise BackupRotationPathError("backup rotation path metadata is unavailable") from error
    if stat.S_ISLNK(value.st_mode) or (
        getattr(value, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise BackupRotationPathError("backup rotation path contains a symlink or reparse point")
    if directory:
        if not stat.S_ISDIR(value.st_mode):
            raise BackupRotationPathError("backup rotation ancestor is not a directory")
    elif not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise BackupRotationPathError("backup rotation requires a regular, non-hard-linked file")
    _identity(value)
    return value


def _windows_permissions(target: Path, parents: tuple[Path, ...], lock: Path | None) -> None:
    # One bounded, non-recursive ACL probe. Public reads on OS ancestors are
    # normal; the immediate secret directory must be private. Delete-child on
    # an ancestor can replace an existing protected child, unlike create-only.
    records = [{"Path": str(target), "Scope": "private"}]
    records.extend(
        {"Path": str(path), "Scope": "private" if i == 0 else "ancestor"}
        for i, path in enumerate(parents)
    )
    if lock is not None:
        records.append({"Path": str(lock), "Scope": "lock"})
    encoded = base64.b64encode(json.dumps(records).encode("utf-8")).decode("ascii")
    script = f"""
$ErrorActionPreference = 'Stop'
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @($current, 'S-1-5-18', 'S-1-5-32-544')
$owners = @($allowed)
try {{
  $account = New-Object System.Security.Principal.NTAccount('NT SERVICE', 'TrustedInstaller')
  $installer = $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
  $owners += $installer
}} catch {{}}
$records = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')) |
  ConvertFrom-Json
foreach ($record in $records) {{
  $item = Get-Item -Force -LiteralPath $record.Path
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{
    throw 'backup rotation path contains a reparse point'
  }}
  $acl = $item.GetAccessControl()
  $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
  $trusted = if ($record.Scope -eq 'ancestor') {{ $owners }} else {{ $allowed }}
  if ($trusted -notcontains $owner) {{ throw 'backup rotation path has an untrusted owner' }}
  foreach ($entry in $acl.Access) {{
    if ($entry.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {{
      continue
    }}
    $inheritOnly = [System.Security.AccessControl.PropagationFlags]::InheritOnly
    if (($entry.PropagationFlags -band $inheritOnly) -ne 0) {{ continue }}
    $sid = $entry.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
    if ($trusted -contains $sid) {{ continue }}
    $rights = [long]$entry.FileSystemRights
    if ($record.Scope -eq 'private') {{ throw 'backup rotation private path grants other access' }}
    # DELETE, DELETE_CHILD, WRITE_DAC, WRITE_OWNER, generic WRITE/ALL.
    $unsafe = 0x500D0040L
    if ($record.Scope -eq 'lock') {{ $unsafe = $unsafe -bor 0x00000116L }}
    if (($rights -band $unsafe) -ne 0) {{ throw 'backup rotation path grants other mutation' }}
  }}
}}
[pscustomobject]@{{Ready=$true}} | ConvertTo-Json -Compress
"""
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=60, check=False)
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise BackupRotationPathError("backup rotation ACL probe is unavailable") from error
    if not isinstance(payload, dict) or payload.get("Ready") is not True:
        # Never forward PowerShell's echoed command (it contains local paths).
        raise BackupRotationPathError("backup rotation path ACL is unsafe or unavailable")


class BackupRotationBoundary:
    """Retain the raw path, checked ancestors and the exact source revision."""

    def __init__(self, path: Path | str, *, permission_error: Callable[[Path], str | None]):
        self.raw = Path(path).expanduser().absolute()
        self.parents = tuple(self.raw.parents)
        self._permission_error = permission_error
        self._lock_handle: BinaryIO | None = None
        self._parents = self._directories()
        self._source = _metadata(self.raw)
        self._check_key_permissions()
        # Resolve only AFTER the original components (including link/..) were
        # examined. Keep validating the original spelling at every boundary.
        try:
            self.target = self.raw.resolve(strict=True)
        except OSError as error:
            raise BackupRotationPathError("backup rotation target is unavailable") from error
        self.lock_path = self.target.with_name(self.target.name + ".rotation.lock")
        self.validate()

    def _directories(self) -> tuple[tuple[int, ...], ...]:
        result = []
        for index, path in enumerate(self.parents):
            value = _metadata(path, directory=True)
            if os.name == "posix":
                if value.st_uid not in {0, _effective_uid()}:
                    raise BackupRotationPathError("backup rotation ancestor has an untrusted owner")
                if value.st_mode & 0o022 and not (index > 0 and value.st_mode & stat.S_ISVTX):
                    raise BackupRotationPathError("backup rotation ancestor is writable by others")
            result.append((*_identity(value), value.st_mode, value.st_uid, value.st_gid))
        return tuple(result)

    def _check_key_permissions(self) -> None:
        if os.name == "nt":
            return
        error = self._permission_error(self.raw)
        if error is not None:
            raise BackupRotationPathError(error)
        if os.name != "posix" or self._source.st_uid != _effective_uid():
            raise BackupRotationPathError("backup rotation requires the keyring owner")

    def validate(self, *, audit_windows: bool = True) -> None:
        if self._directories() != self._parents or _revision(_metadata(self.raw)) != _revision(
            self._source
        ):
            raise BackupRotationPathError("backup rotation original path identity changed")
        self._check_key_permissions()
        if _identity(_metadata(self.target)) != _identity(self._source):
            raise BackupRotationPathError("backup rotation resolved target identity changed")
        lock = None
        if self._lock_handle is not None:
            lock = self.lock_path
            self._check_lock(_metadata(lock))
            if _identity(os.fstat(self._lock_handle.fileno())) != _identity(_metadata(lock)):
                raise BackupRotationPathError("backup rotation lock identity changed")
        if os.name == "nt" and audit_windows:
            _windows_permissions(self.raw, self.parents, lock)

    @staticmethod
    def _check_lock(value: os.stat_result) -> None:
        if os.name == "posix" and (value.st_uid != _effective_uid() or value.st_mode & 0o022):
            raise BackupRotationPathError("backup rotation lock owner or permissions are unsafe")

    def open_lock(self, path: Path) -> BinaryIO:
        self.validate(audit_windows=False)
        if path != self.lock_path:
            raise BackupRotationPathError("backup rotation lock path differs")
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            expected = None
            try:
                descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                expected = _metadata(path)
                self._check_lock(expected)
                descriptor = os.open(path, flags | getattr(os, "O_NONBLOCK", 0))
            try:
                opened = os.fstat(descriptor)
                current = _metadata(path)
                self._check_lock(current)
                if _identity(current) != _identity(opened) or (
                    expected is not None and _identity(opened) != _identity(expected)
                ):
                    raise BackupRotationPathError("backup rotation lock changed while opening")
                self.validate(audit_windows=False)
                if os.name == "nt":
                    _windows_permissions(self.raw, self.parents, path)
                handle = os.fdopen(descriptor, "r+b")
            except BaseException:
                os.close(descriptor)
                raise
        except OSError as error:
            raise BackupRotationPathError("backup rotation lock cannot be opened safely") from error
        self._lock_handle = handle
        return handle

    def read(self, max_bytes: int) -> bytes:
        self.validate(audit_windows=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(self.raw, flags | getattr(os, "O_NONBLOCK", 0))
            try:
                if _revision(os.fstat(descriptor)) != _revision(self._source):
                    raise BackupRotationPathError("backup rotation source changed before reading")
                chunks = []
                remaining = max_bytes + 1
                while remaining:
                    chunk = os.read(descriptor, min(remaining, 64 * 1024))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if _revision(os.fstat(descriptor)) != _revision(self._source):
                    raise BackupRotationPathError("backup rotation source changed while reading")
            finally:
                os.close(descriptor)
        except OSError as error:
            raise BackupRotationPathError("backup rotation source cannot be read safely") from error
        self.validate(audit_windows=False)
        return b"".join(chunks)

    def prepare_temporary(self, descriptor: int) -> None:
        if os.name == "posix":
            current = os.fstat(descriptor)
            if (current.st_uid, current.st_gid) != (self._source.st_uid, self._source.st_gid):
                chown = getattr(os, "fchown", None)
                if not callable(chown):
                    raise BackupRotationPathError("backup rotation descriptor chown is unavailable")
                chown(descriptor, self._source.st_uid, self._source.st_gid)
            chmod = getattr(os, "fchmod", None)
            if not callable(chmod):
                raise BackupRotationPathError("backup rotation descriptor chmod is unavailable")
            chmod(descriptor, stat.S_IMODE(self._source.st_mode) & 0o777)

    def before_replace(self, temporary: Path, expected: tuple[int, int]) -> None:
        self.validate()
        if _identity(_metadata(temporary)) != expected:
            raise BackupRotationPathError("backup rotation temporary file identity changed")

    def cleanup_temporary(self, temporary: Path, expected: tuple[int, int]) -> None:
        # Never unlink through an ancestor that failed the pinned identity check.
        try:
            if self._directories() == self._parents and _identity(_metadata(temporary)) == expected:
                temporary.unlink()
        except (OSError, BackupRotationPathError):
            return
