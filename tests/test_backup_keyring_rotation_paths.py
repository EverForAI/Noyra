from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from backup_rotation_helpers import rotate_new_request
from noyra.core.at_rest import BackupKeyring, BackupKeyUnavailableError, _keyring_permission_error
from noyra.core.backup_keyring_paths import BackupRotationBoundary, BackupRotationPathError
from noyra.core.backup_rotation import BackupRotationJournal
from noyra.core.backup_rotation_records import RotationOutcomeUnknownError, sha256
from noyra.core.locking import ProcessLock


def guarded_read(path: Path) -> BackupKeyring:
    """Exercise D4a itself on both platforms, without invoking a legacy writer."""
    try:
        boundary = BackupRotationBoundary(path, permission_error=_keyring_permission_error)
        lock = ProcessLock(boundary.lock_path, opener=boundary.open_lock)
        try:
            lock.acquire()
            return BackupKeyring._from_bytes(boundary.read(1024 * 1024))
        finally:
            lock.release()
    except BackupRotationPathError as error:
        raise BackupKeyUnavailableError(str(error)) from error


def guarded_rewrite(path: Path) -> None:
    """Exercise the existing low-level guarded writer's pre-replace checks."""
    try:
        boundary = BackupRotationBoundary(path, permission_error=_keyring_permission_error)
        lock = ProcessLock(boundary.lock_path, opener=boundary.open_lock)
        try:
            lock.acquire()
            current = BackupKeyring._from_bytes(boundary.read(1024 * 1024))
            current.write(path, create_only=False, _rotation_boundary=boundary)
        finally:
            lock.release()
    except BackupRotationPathError as error:
        raise BackupKeyUnavailableError(str(error)) from error


def _ring(root: Path) -> Path:
    path = root / "keyring.json"
    BackupKeyring.initialize(path)
    return path


def _symlink(alias: Path, target: Path, *, directory: bool = False) -> None:
    try:
        alias.symlink_to(target, target_is_directory=directory)
    except OSError:
        if os.name != "nt" or not directory:
            pytest.skip("native symbolic-link creation is unavailable")
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
            check=True,
            capture_output=True,
        )


def test_raw_symlink_is_rejected_before_key_parsing(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _ring(rotation_directory)
    before = target.read_bytes()
    alias = rotation_directory / "alias.json"
    _symlink(alias, target)

    def must_not_parse(cls: type[BackupKeyring], raw: bytes) -> BackupKeyring:
        raise AssertionError("unsafe original path reached key parser")

    monkeypatch.setattr(BackupKeyring, "_from_bytes", classmethod(must_not_parse), raising=False)
    with pytest.raises(BackupKeyUnavailableError):
        guarded_read(alias)
    assert target.read_bytes() == before
    assert not target.with_name(target.name + ".rotation.lock").exists()


def test_original_ancestor_link_is_rejected(rotation_directory: Path) -> None:
    real = rotation_directory / "real"
    real.mkdir()
    target = _ring(real)
    alias = rotation_directory / "alias"
    _symlink(alias, real, directory=True)
    before = target.read_bytes()
    with pytest.raises(BackupKeyUnavailableError):
        guarded_read(alias / target.name)
    assert target.read_bytes() == before


def test_link_before_parent_traversal_is_not_normalized_away(rotation_directory: Path) -> None:
    target = _ring(rotation_directory)
    child = rotation_directory / "child"
    child.mkdir()
    alias = rotation_directory / "alias"
    _symlink(alias, child, directory=True)
    before = target.read_bytes()
    with pytest.raises(BackupKeyUnavailableError):
        guarded_read(alias / ".." / target.name)
    assert target.read_bytes() == before


def test_keyring_hard_link_is_rejected(rotation_directory: Path) -> None:
    target = _ring(rotation_directory)
    before = target.read_bytes()
    alias = rotation_directory / "hard-link.json"
    os.link(target, alias)
    with pytest.raises(BackupKeyUnavailableError):
        guarded_read(target)
    assert target.read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "hard-link"])
def test_unsafe_lock_never_writes_its_referent(rotation_directory: Path, kind: str) -> None:
    target = _ring(rotation_directory)
    before = target.read_bytes()
    victim = rotation_directory / "synthetic-empty-victim"
    victim.write_bytes(b"")
    lock = target.with_name(target.name + ".rotation.lock")
    if kind == "symlink":
        _symlink(lock, victim)
    else:
        os.link(victim, lock)
    with pytest.raises(BackupKeyUnavailableError):
        guarded_read(target)
    assert victim.read_bytes() == b""
    assert target.read_bytes() == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission boundary")
@pytest.mark.parametrize("unsafe", ["key", "parent", "upper", "lock"])
def test_posix_unsafe_permissions_fail_before_publication(
    rotation_directory: Path, unsafe: str
) -> None:
    parent = rotation_directory / "parent"
    parent.mkdir(mode=0o700)
    target = _ring(parent)
    before = target.read_bytes()
    lock = target.with_name(target.name + ".rotation.lock")
    lock.write_bytes(b"")
    lock.chmod(0o600)
    path = {"key": target, "parent": parent, "upper": rotation_directory, "lock": lock}[unsafe]
    previous = stat.S_IMODE(path.stat().st_mode)
    path.chmod(0o644 if unsafe == "key" else 0o777)
    try:
        with pytest.raises(BackupKeyUnavailableError):
            guarded_read(target)
        assert target.read_bytes() == before
        assert lock.read_bytes() == b""
    finally:
        path.chmod(previous)


@pytest.mark.skipif(os.name != "posix", reason="POSIX special files")
def test_fifo_source_does_not_block_or_get_read(rotation_directory: Path) -> None:
    target = rotation_directory / "fifo"
    mkfifo = getattr(os, "mkfifo", None)
    assert callable(mkfifo)
    mkfifo(target, 0o600)
    with pytest.raises(BackupKeyUnavailableError):
        guarded_read(target)


@pytest.mark.parametrize("change", ["source", "parent", "lock"])
def test_changes_while_acquiring_lock_are_rejected_and_lock_is_released(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    parent = rotation_directory / "parent"
    parent.mkdir()
    target = _ring(parent)
    other = rotation_directory / "other.json"
    replacement = BackupKeyring.initialize(other)
    other_bytes = other.read_bytes()
    original = ProcessLock.acquire
    blocked_by_os = False

    def acquire(self: ProcessLock) -> bool:
        nonlocal blocked_by_os
        result = original(self)
        try:
            if change == "source":
                other.replace(target)
            elif change == "parent":
                parent.rename(rotation_directory / "moved")
                parent.mkdir()
                other.replace(target)
            else:
                new_lock = rotation_directory / "other-lock"
                new_lock.write_bytes(b"")
                new_lock.chmod(0o600)
                new_lock.replace(self.path)
        except PermissionError:
            # Windows can deny rename/delete of the held byte-lock inode or
            # its parent directory. Verify that native barrier, not a skip.
            if os.name != "nt":
                raise
            blocked_by_os = True
        return result

    with monkeypatch.context() as patch:
        patch.setattr(ProcessLock, "acquire", acquire)
        try:
            rotated = guarded_read(target)
        except BackupKeyUnavailableError as error:
            assert not blocked_by_os and "identity changed" in str(error)
        else:
            assert blocked_by_os and change in {"parent", "lock"} and rotated.generation == 1
    if blocked_by_os:
        assert other.read_bytes() == other_bytes
        assert guarded_read(target).generation == 1
    else:
        if change == "lock":
            assert BackupKeyring.from_path(target).generation == 1
        else:
            assert target.read_bytes() == other_bytes
            assert BackupKeyring.from_path(target).active_key_id == replacement.active_key_id
        assert guarded_read(target).generation == 1


def test_source_replaced_between_path_check_and_open_is_not_read(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _ring(rotation_directory)
    other = rotation_directory / "other.json"
    BackupKeyring.initialize(other)
    expected = other.read_bytes()
    original = os.open
    swapped = False

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if (
            Path(path) == target
            and flags & (os.O_WRONLY | os.O_RDWR) == os.O_RDONLY
            and not swapped
        ):
            swapped = True
            other.replace(target)
        return original(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opened)
        with pytest.raises(BackupKeyUnavailableError, match="before reading"):
            guarded_read(target)
    assert swapped and target.read_bytes() == expected
    assert guarded_read(target).generation == 1


def test_source_mutation_during_read_is_rejected(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _ring(rotation_directory)
    original = os.read
    changed = False

    def read(descriptor: int, length: int) -> bytes:
        nonlocal changed
        chunk = original(descriptor, length)
        if not changed:
            changed = True
            with target.open("ab") as stream:
                stream.write(b" ")
        return chunk

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", read)
        with pytest.raises(BackupKeyUnavailableError, match="while reading"):
            guarded_read(target)
    assert BackupKeyring.from_path(target).generation == 1


@pytest.mark.parametrize("change", ["source", "temporary"])
def test_change_before_replace_is_not_published(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from noyra.core.backup_keyring_paths import BackupRotationBoundary

    target = _ring(rotation_directory)
    other = rotation_directory / "other.json"
    BackupKeyring.initialize(other)
    before = target.read_bytes()
    other_bytes = other.read_bytes()
    original = BackupRotationBoundary.before_replace

    def checkpoint(
        self: BackupRotationBoundary, temporary: Path, expected: tuple[int, int]
    ) -> None:
        other.replace(target if change == "source" else temporary)
        original(self, temporary, expected)

    with monkeypatch.context() as patch:
        patch.setattr(BackupRotationBoundary, "before_replace", checkpoint)
        with pytest.raises(BackupKeyUnavailableError, match="identity changed"):
            guarded_rewrite(target)
    assert target.read_bytes() == (other_bytes if change == "source" else before)
    assert guarded_read(target).generation == 1


def test_relative_path_with_ordinary_parent_traversal_still_rotates(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _ring(rotation_directory)
    nested = rotation_directory / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    rotated = guarded_read(Path("..") / target.name)
    assert rotated.generation == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX durable rotation")
def test_rotation_retains_keys_and_lock_inode(rotation_directory: Path) -> None:
    target = _ring(rotation_directory)
    first = BackupKeyring.from_path(target)
    second = rotate_new_request(target)
    lock = target.with_name(target.name + ".rotation.lock")
    inode = lock.stat().st_ino
    third = rotate_new_request(target)
    assert (first.generation, second.generation, third.generation) == (1, 2, 3)
    assert third.resolve(first.active.key_id, first.active.fingerprint).key == first.active.key
    assert third.resolve(second.active.key_id, second.active.fingerprint).key == second.active.key
    assert lock.stat().st_ino == inode


@pytest.mark.skipif(os.name != "posix", reason="POSIX durable rotation")
def test_replace_then_directory_fsync_error_remains_explicit(
    rotation_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _ring(rotation_directory)
    api = BackupRotationJournal(target)
    expected = sha256(target.read_bytes())
    api.prepare(request_id="a" * 32, expected_generation=1, expected_source_hash=expected)

    def failed(path: Path) -> None:
        raise OSError("synthetic directory fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr("noyra.core.backup_rotation_publication._fsync_directory", failed)
        with pytest.raises(RotationOutcomeUnknownError):
            api.resume(request_id="a" * 32)
    assert BackupKeyring.from_path(target).generation == 2
    assert api.resume(request_id="a" * 32).generation == 2


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "geteuid"), reason="POSIX owner")
def test_root_service_group_permissions_survive_rotation() -> None:
    uid = getattr(os, "geteuid", None)
    chown = getattr(os, "chown", None)
    assert callable(uid) and callable(chown)
    if uid() != 0:
        pytest.skip("requires actual root to exercise the service-group layout")
    with tempfile.TemporaryDirectory(prefix="noyra-rotation-service-", dir="/var/tmp") as folder:
        root = Path(folder)
        chown(root, 0, 65534)
        root.chmod(0o750)
        target = _ring(root)
        chown(target, 0, 65534)
        target.chmod(0o640)
        before = BackupKeyring.from_path(target)
        after = rotate_new_request(target)
        metadata = target.stat()
        assert (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) == (
            0,
            65534,
            0o640,
        )
        assert after.resolve(before.active.key_id, before.active.fingerprint)
        source = str(Path(__file__).resolve().parents[1] / "src")
        script = """
import pathlib,sys
sys.path.insert(0,sys.argv[1])
from noyra.core.at_rest import BackupKeyring,validate_keyring_path,_keyring_permission_error
from noyra.core.backup_keyring_paths import BackupRotationBoundary,BackupRotationPathError
assert BackupKeyring.from_path(validate_keyring_path(sys.argv[2])).generation == 2
try:
    BackupRotationBoundary(sys.argv[2],permission_error=_keyring_permission_error)
except BackupRotationPathError as error:
    assert 'requires the keyring owner' in str(error), str(error)
else:
    raise AssertionError('read-only service user must not rotate root-owned keys')
"""
        subprocess.run(
            [sys.executable, "-c", script, source, str(target)],
            check=True,
            capture_output=True,
            user=65534,
            group=65534,
            extra_groups=[],
        )
        assert BackupKeyring.from_path(target).generation == 2


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL")
@pytest.mark.parametrize("scope", ["key", "parent", "ancestor", "lock"])
def test_native_windows_acl_rejects_other_mutation(rotation_directory: Path, scope: str) -> None:
    from noyra.core.at_rest import _powershell_command

    nested = rotation_directory / "nested"
    nested.mkdir()
    target = _ring(nested)
    before = target.read_bytes()
    lock = target.with_name(target.name + ".rotation.lock")
    lock.write_bytes(b"")
    entry = {"key": target, "parent": nested, "ancestor": rotation_directory, "lock": lock}[scope]
    import base64

    encoded = base64.b64encode(str(entry).encode("utf-8")).decode("ascii")
    rights = "ReadData" if scope == "key" else "DeleteSubdirectoriesAndFiles"
    if scope == "lock":
        rights = "WriteData"
    script = f"""
$ErrorActionPreference='Stop'
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))
$item=Get-Item -Force -LiteralPath $p
$acl=$item.GetAccessControl()
$sid=[System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$rights=[System.Security.AccessControl.FileSystemRights]::{rights}
$rule=[System.Security.AccessControl.FileSystemAccessRule]::new(
  $sid, $rights, [System.Security.AccessControl.AccessControlType]::Allow)
$acl.AddAccessRule($rule)
$item.SetAccessControl($acl)
"""
    subprocess.run(_powershell_command(script), check=True, capture_output=True)
    with pytest.raises(BackupKeyUnavailableError, match="ACL"):
        guarded_read(target)
    assert target.read_bytes() == before
    assert lock.read_bytes() == b""


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction")
def test_native_windows_junction_is_rejected(rotation_directory: Path) -> None:
    real = rotation_directory / "real"
    real.mkdir()
    target = _ring(real)
    before = target.read_bytes()
    alias = rotation_directory / "junction"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(real)],
        check=True,
        capture_output=True,
    )
    assert getattr(alias.lstat(), "st_file_attributes", 0) & 0x400
    with pytest.raises(BackupKeyUnavailableError, match="reparse"):
        guarded_read(alias / target.name)
    assert target.read_bytes() == before


def test_lock_replaced_while_opening_is_rejected_before_any_write(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _ring(rotation_directory)
    before = target.read_bytes()
    lock = target.with_name(target.name + ".rotation.lock")
    lock.write_bytes(b"")
    lock.chmod(0o600)
    other = rotation_directory / "other-lock"
    other.write_bytes(b"")
    other.chmod(0o600)
    original = os.open
    swapped = False

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if Path(path) == lock and not flags & os.O_CREAT and not swapped:
            swapped = True
            other.replace(lock)
        return original(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opened)
        with pytest.raises(BackupKeyUnavailableError, match="changed while opening"):
            guarded_read(target)
    assert swapped and target.read_bytes() == before and lock.read_bytes() == b""
    assert guarded_read(target).generation == 1


@pytest.mark.parametrize(
    "payload",
    [b"", b"{}", b"x" * (1024 * 1024 + 1)],
    ids=["empty", "invalid-json", "over-budget"],
)
def test_invalid_key_content_does_not_publish_and_releases_lock(
    rotation_directory: Path, payload: bytes
) -> None:
    target = _ring(rotation_directory)
    valid = target.read_bytes()
    target.write_bytes(payload)
    with pytest.raises(BackupKeyUnavailableError):
        guarded_read(target)
    assert target.read_bytes() == payload
    target.write_bytes(valid)
    assert guarded_read(target).generation == 1


def test_missing_source_does_not_create_directories_or_locks(rotation_directory: Path) -> None:
    target = rotation_directory / "missing" / "keyring.json"
    with pytest.raises(BackupKeyUnavailableError):
        guarded_read(target)
    assert not target.parent.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL probe")
def test_windows_acl_probe_failure_is_closed_and_diagnostic_does_not_echo_command(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _ring(rotation_directory)
    before = target.read_bytes()

    def failed(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["timeout"] == 60
        return subprocess.CompletedProcess(args[0], 1, b"", b"SYNTHETIC_PRIVATE_COMMAND")

    monkeypatch.setattr("noyra.core.backup_keyring_paths.subprocess.run", failed)
    with pytest.raises(BackupKeyUnavailableError, match="ACL") as caught:
        guarded_read(target)
    assert "SYNTHETIC_PRIVATE_COMMAND" not in str(caught.value)
    assert target.read_bytes() == before
    assert not target.with_name(target.name + ".rotation.lock").exists()


def test_temporary_fstat_failure_closes_descriptor_and_keeps_original(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _ring(rotation_directory)
    before = target.read_bytes()
    original_open, original_fstat = os.open, os.fstat
    descriptor: int | None = None
    failed = False

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal descriptor
        result = original_open(path, flags, *args, **kwargs)
        if str(path).endswith(".tmp"):
            descriptor = result
        return result

    def fstat(value: int) -> os.stat_result:
        nonlocal failed
        if value == descriptor and not failed:
            failed = True
            raise OSError("synthetic temporary fstat failure")
        return original_fstat(value)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opened)
        patch.setattr(os, "fstat", fstat)
        with pytest.raises(OSError, match="temporary fstat"):
            guarded_rewrite(target)
    assert failed and descriptor is not None and target.read_bytes() == before
    with pytest.raises(OSError):
        original_fstat(descriptor)
    # No stable identity was obtained: an empty private placeholder is safer
    # than unlinking a path whose inode cannot be proved to be ours.
    leftovers = list(rotation_directory.glob(".keyring.json.*.tmp"))
    assert len(leftovers) == 1 and leftovers[0].read_bytes() == b""
    assert guarded_read(target).generation == 1
