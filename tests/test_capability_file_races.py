from __future__ import annotations

import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from noyra.capability import CapabilityGrant, CapabilityIntegrity, CapabilityStore, ToolRunner
from noyra.capability.errors import CapabilityDeniedError
from noyra.capability.filesystem import normalized_root
from noyra.capability.types import CapabilityType
from noyra.core import Database, IdentityStore
from noyra.core.types import content_hash


def directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


def remove_link(link: Path) -> None:
    if os.name == "nt":
        link.rmdir()
    else:
        link.unlink()


def runner(tmp_path: Path) -> tuple[ToolRunner, str, Path]:
    db = Database(tmp_path / "db.sqlite3")
    subject = "Noyra-file-race"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    root = (tmp_path / "authorized").resolve()
    root.mkdir()
    for capability in ("filesystem_read", "filesystem_write"):
        CapabilityStore(db).grant(
            subject,
            CapabilityGrant(
                capability_type=capability,
                scope={"root": str(root)},
                issuer="test",
                rate_limit_per_hour=100,
                side_effect=capability == "filesystem_write",
            ),
            actor="operator",
        )
    return ToolRunner(db, max_file_bytes=1024), subject, root


def windows_short_path(path: Path) -> Path:
    import ctypes
    from ctypes import wintypes

    ctypes_api: Any = ctypes
    function = ctypes_api.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    function.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    count = function(str(path), buffer, len(buffer))
    assert 0 < count < len(buffer), ctypes_api.get_last_error()
    short = Path(buffer.value)
    if short == path:
        pytest.skip("test volume does not generate Windows 8.3 aliases")
    return short


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 path compatibility")
@pytest.mark.parametrize("missing_root", [False, True])
def test_short_grant_root_preserves_io_and_persisted_integrity(
    tmp_path: Path, missing_root: bool
) -> None:
    tools, subject, root = runner(tmp_path)
    long = root / "long directory with short alias"
    long.mkdir()
    short = windows_short_path(long)
    if missing_root:
        short = short / "new root"
        long = long / "new root"
    for grant in tools.capabilities.list(subject):
        tools.capabilities.revoke(
            grant.grant_id, subject_id=subject, actor="operator", reason="replace"
        )
        tools.capabilities.grant(
            subject,
            CapabilityGrant(
                capability_type=cast(CapabilityType, grant.capability_type),
                scope={"root": str(short)},
                issuer="test",
                rate_limit_per_hour=1,
                side_effect=grant.side_effect,
            ),
            actor="operator",
        )
    with tools.database.connection() as connection:
        before = [
            tuple(row)
            for row in connection.execute(
                "SELECT grant_id, scope_json, state_hash FROM capability_grants ORDER BY grant_id"
            )
        ]
    # Reload stored scopes rather than relying on the grant-creation objects.
    tools = ToolRunner(tools.database, max_file_bytes=1024)
    assert tools.write_text(subject, short / "file.txt", "expected").status == "succeeded"
    assert tools.read_text(subject, long / "file.txt").content == "expected"
    assert (long / "file.txt").read_text(encoding="utf-8") == "expected"
    with pytest.raises(CapabilityDeniedError):
        tools.write_text(subject, short / "second.txt", "rate limited")
    with tools.database.connection() as connection:
        after = [
            tuple(row)
            for row in connection.execute(
                "SELECT grant_id, scope_json, state_hash FROM capability_grants ORDER BY grant_id"
            )
        ]
    assert after == before
    assert CapabilityIntegrity(tools.database).verify(subject)["capability_uses"] == 2
    assert not (long / "second.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 reparse boundary")
@pytest.mark.parametrize("missing_suffix", [False, True])
def test_short_grant_root_does_not_normalize_away_junction(
    tmp_path: Path, missing_suffix: bool
) -> None:
    tools, subject, root = runner(tmp_path)
    long = root / "long directory with short alias"
    long.mkdir()
    short = windows_short_path(long)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "file.txt"
    victim.write_text("foreign secret", encoding="utf-8")
    link = long / "junction"
    directory_link(link, outside)
    scope = short / "junction"
    if missing_suffix:
        scope /= "missing"
    try:
        with pytest.raises(PermissionError):
            normalized_root(str(scope))
        for grant in tools.capabilities.list(subject):
            tools.capabilities.revoke(
                grant.grant_id, subject_id=subject, actor="operator", reason="replace"
            )
            tools.capabilities.grant(
                subject,
                CapabilityGrant(
                    capability_type=cast(CapabilityType, grant.capability_type),
                    scope={"root": str(scope)},
                    issuer="test",
                    rate_limit_per_hour=1,
                    side_effect=grant.side_effect,
                ),
                actor="operator",
            )
        with pytest.raises(CapabilityDeniedError):
            tools.read_text(subject, scope / "file.txt")
        with pytest.raises(CapabilityDeniedError):
            tools.write_text(subject, scope / "file.txt", "overwrite")
        assert victim.read_text(encoding="utf-8") == "foreign secret"
        assert list(outside.iterdir()) == [victim]
        assert CapabilityIntegrity(tools.database).verify(subject)["capability_uses"] == 0
    finally:
        remove_link(link)


@pytest.mark.parametrize("ancestor", [False, True])
def test_no_temporary_bytes_escape_after_last_path_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestor: bool,
) -> None:
    tools, subject, root = runner(tmp_path)
    parent = root / "nested"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if ancestor:
        (outside / "nested").mkdir()
    swapped = root if ancestor else parent
    displaced = tmp_path / "displaced"
    original_open = os.open
    attempted = False

    def swap_at_create(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal attempted
        if not attempted and flags & os.O_EXCL:
            attempted = True
            try:
                swapped.rename(displaced)
            except PermissionError:
                return original_open(path, flags, *args, **kwargs)
            directory_link(swapped, outside)
            try:
                return original_open(path, flags, *args, **kwargs)
            finally:
                remove_link(swapped)
                displaced.rename(swapped)
        return original_open(path, flags, *args, **kwargs)

    # The old Windows path opener leaks a staged file even when the later
    # publication fails. POSIX must use only descriptor-relative child opens.
    monkeypatch.setattr(os, "open", swap_at_create)
    if os.name == "nt":
        from noyra.cognition.execution import _windows_workspace_api

        api = _windows_workspace_api()
        original_relative = api.open_relative

        def swap_native(parent_handle: int, name: str, **kwargs: Any) -> int:
            nonlocal attempted
            if not attempted and kwargs.get("write"):
                attempted = True
                try:
                    swapped.rename(displaced)
                except PermissionError:
                    return original_relative(parent_handle, name, **kwargs)
                directory_link(swapped, outside)
                try:
                    return original_relative(parent_handle, name, **kwargs)
                finally:
                    remove_link(swapped)
                    displaced.rename(swapped)
            return original_relative(parent_handle, name, **kwargs)

        monkeypatch.setattr(api, "open_relative", swap_native)
    tools.write_text(subject, parent / "result.txt", "private payload")
    assert attempted
    assert not list(outside.rglob("*.tmp*"))
    assert all(not p.is_file() for p in outside.rglob("*"))


@pytest.mark.parametrize("operation", ["read", "write"])
def test_ancestor_link_after_authorization_is_never_followed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    tools, subject, root = runner(tmp_path)
    (root / "nested").mkdir()
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    victim = outside / "nested" / "result.txt"
    victim.write_text("foreign secret", encoding="utf-8")
    displaced = tmp_path / "displaced"
    original_check = tools._revalidate_file_target
    attempted = False

    def swap_after_check(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempted
        result = original_check(*args, **kwargs)
        if not attempted:
            attempted = True
            root.rename(displaced)
            directory_link(root, outside)
        return result

    monkeypatch.setattr(tools, "_revalidate_file_target", swap_after_check)
    try:
        target = root / "nested" / "result.txt"
        result = (
            tools.read_text(subject, target)
            if operation == "read"
            else tools.write_text(subject, target, "overwrite")
        )
        assert attempted
        assert result.status != "succeeded"
        assert result.content is None
        assert victim.read_text(encoding="utf-8") == "foreign secret"
    finally:
        if attempted:
            remove_link(root)
            displaced.rename(root)


def test_nested_atomic_write_bounded_read_and_failed_write_cleanup(tmp_path: Path) -> None:
    tools, subject, root = runner(tmp_path)
    path = root / "new" / "nested" / "text.txt"
    assert tools.write_text(subject, path, "a" * 1024).status == "succeeded"
    assert tools.read_text(subject, path, idempotency_key="before").content == "a" * 1024
    path.write_bytes(b"b" * 1025)
    assert tools.read_text(subject, path, idempotency_key="after").status == "failed"
    assert not list(root.rglob("*.tmp*"))


@pytest.mark.parametrize("fault", ["revoke", "disk_full"])
def test_failed_write_preserves_previous_file_and_removes_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    tools, subject, root = runner(tmp_path)
    path = root / "existing.txt"
    path.write_text("previous", encoding="utf-8")
    original_write = os.write

    def inject(fd: int, data: Any) -> int:
        if fault == "disk_full":
            raise OSError("injected disk full")
        for grant in tools.capabilities.list(subject):
            if grant.capability_type == "filesystem_write":
                tools.capabilities.revoke(
                    grant.grant_id, subject_id=subject, reason="test", actor="operator"
                )
        return original_write(fd, data)

    monkeypatch.setattr(os, "write", inject)
    result = tools.write_text(subject, path, "replacement")
    assert result.status == "unknown"
    assert path.read_text(encoding="utf-8") == "previous"
    assert list(root.iterdir()) == [path]


def test_publication_cannot_follow_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, subject, root = runner(tmp_path)
    parent = root / "nested"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "result.txt"
    victim.write_text("foreign", encoding="utf-8")
    displaced = root / "displaced"
    attempted = False

    def race(call: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal attempted
        attempted = True
        try:
            parent.rename(displaced)
        except PermissionError:
            return call(*args, **kwargs)
        directory_link(parent, outside)
        try:
            return call(*args, **kwargs)
        finally:
            remove_link(parent)
            displaced.rename(parent)

    if os.name == "nt":
        from noyra.cognition.execution import _windows_workspace_api

        api = _windows_workspace_api()
        native_rename = api.rename
        monkeypatch.setattr(api, "rename", lambda *a, **kw: race(native_rename, *a, **kw))
    else:
        posix_replace = os.replace
        monkeypatch.setattr(os, "replace", lambda *a, **kw: race(posix_replace, *a, **kw))
    result = tools.write_text(subject, parent / "result.txt", "private")
    assert attempted and result.status == "succeeded"
    assert victim.read_text(encoding="utf-8") == "foreign"
    assert (parent / "result.txt").read_text(encoding="utf-8") == "private"


@pytest.mark.parametrize("boundary", ["opened", "read"])
def test_read_discards_content_after_grant_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    tools, subject, root = runner(tmp_path)
    target = root / "secret.txt"
    target.write_text("private", encoding="utf-8")
    revoked = False

    def revoke() -> None:
        nonlocal revoked
        if revoked:
            return
        revoked = True
        for grant in tools.capabilities.list(subject):
            if grant.capability_type == "filesystem_read":
                tools.capabilities.revoke(
                    grant.grant_id, subject_id=subject, actor="operator", reason="test"
                )

    if boundary == "opened":
        original_stat = os.fstat

        def after_open(fd: int) -> Any:
            result = original_stat(fd)
            if stat.S_ISREG(result.st_mode):
                revoke()
            return result

        monkeypatch.setattr(os, "fstat", after_open)
    else:
        import noyra.capability.tools as module
        from noyra.capability.filesystem import read_bounded

        original_read = read_bounded

        def after_read(*args: Any, **kwargs: Any) -> bytes:
            result = original_read(*args, **kwargs)
            revoke()
            return result

        monkeypatch.setattr(module, "read_bounded", after_read)
    result = tools.read_text(subject, target)
    assert revoked and result.status == "failed" and result.content is None


def test_equivalent_grant_root_keeps_read_and_write_compatibility(tmp_path: Path) -> None:
    tools, subject, root = runner(tmp_path)
    for grant in tools.capabilities.list(subject):
        tools.capabilities.revoke(
            grant.grant_id, subject_id=subject, actor="operator", reason="replace"
        )
        tools.capabilities.grant(
            subject,
            CapabilityGrant(
                capability_type=cast(CapabilityType, grant.capability_type),
                scope={"root": str(root / ".." / root.name)},
                issuer="test",
                rate_limit_per_hour=1,
                side_effect=grant.side_effect,
            ),
            actor="operator",
        )
    target = root / "file.txt"
    assert tools.write_text(subject, target, "expected").status == "succeeded"
    assert tools.read_text(subject, target).content == "expected"
    with tools.database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM capability_uses").fetchone()[0] == 2


def test_revoked_charged_grant_cannot_fall_back_to_expired_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, subject, root = runner(tmp_path)
    tools.capabilities.grant(
        subject,
        CapabilityGrant(
            capability_type="filesystem_write",
            scope={"root": str(root)},
            issuer="test",
            rate_limit_per_hour=1,
            side_effect=True,
            expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        ),
        actor="operator",
    )
    original = os.write

    def revoke(fd: int, payload: Any) -> int:
        for grant in tools.capabilities.list(subject):
            if grant.capability_type == "filesystem_write" and grant.expires_at is None:
                tools.capabilities.revoke(
                    grant.grant_id, subject_id=subject, actor="operator", reason="test"
                )
        return original(fd, payload)

    monkeypatch.setattr(os, "write", revoke)
    target = root / "file.txt"
    assert tools.write_text(subject, target, "private").status == "unknown"
    assert not target.exists() and not list(root.iterdir())


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission boundary")
@pytest.mark.parametrize(
    ("unsafe", "mode"),
    [(location, mode) for location in ("parent", "ancestor") for mode in (0o777, 0o775, 0o1777)]
    + [("above_grant", 0o777), ("above_grant", 0o775)],
)
def test_posix_write_denies_entry_writable_directories_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str, mode: int
) -> None:
    tools, subject, root = runner(tmp_path)
    parent = root / "nested"
    parent.mkdir()
    checked = parent if unsafe == "parent" else root if unsafe == "ancestor" else tmp_path
    checked.chmod(mode)
    writes: list[int] = []
    original = os.write

    def record(fd: int, payload: Any) -> int:
        writes.append(fd)
        return original(fd, payload)

    monkeypatch.setattr(os, "write", record)
    result = tools.write_text(subject, parent / "new" / "file.txt", "private")
    assert result.status == "unknown"
    assert not writes and not (parent / "new").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission boundary")
def test_posix_write_rejects_foreign_owned_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, subject, root = runner(tmp_path)
    original = os.fstat
    target_inode = root.stat().st_ino

    def foreign_owner(fd: int) -> Any:
        metadata = original(fd)
        if metadata.st_ino != target_inode:
            return metadata
        from types import SimpleNamespace

        return SimpleNamespace(st_uid=987654321, st_mode=metadata.st_mode)

    monkeypatch.setattr(os, "fstat", foreign_owner)
    assert tools.write_text(subject, root / "file.txt", "private").status == "unknown"
    assert not list(root.iterdir())
