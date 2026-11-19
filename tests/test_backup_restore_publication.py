from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from noyra.core import at_rest
from noyra.core.at_rest import EncryptedBackupManager
from test_m42_p2_14_at_rest import _keyring, _runtime


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> tuple[EncryptedBackupManager, Path]:
    root = tmp_path_factory.mktemp("restore-archive")
    data = root / "source"
    data.mkdir()
    _runtime(data)
    (data / "subject" / "empty").mkdir()
    manager = EncryptedBackupManager(data, _keyring(root), max_total_bytes=100_000_000)
    backup = root / "source.noyra-backup"
    manager.create(backup)
    return manager, backup


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture(scope="module")
def restored_files(
    archive: tuple[EncryptedBackupManager, Path], tmp_path_factory: pytest.TempPathFactory
) -> dict[str, str]:
    manager, backup = archive
    target = tmp_path_factory.mktemp("restore-reference") / "target"
    return tree_hashes(manager.restore(backup, target))


def test_restore_syncs_nested_directories_before_publication(
    archive: tuple[EncryptedBackupManager, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, backup = archive
    target = tmp_path / "target"
    events: list[Path] = []
    sync = at_rest._fsync_directory

    def record(path: Path) -> None:
        # Calls before rename must describe the still-unpublished tree.
        if path.name == "restored":
            assert not target.exists()
        events.append(path)
        sync(path)

    monkeypatch.setattr(at_rest, "_fsync_directory", record)
    manager.restore(backup, target)
    staged_roots = [path for path in events if path.name == "restored"]
    assert len(staged_roots) == 1
    staged = staged_roots[0]
    for child in ["secrets/models", "subject/cold", "subject/empty"]:
        assert staged / child in events
        assert events.index(staged / child) < events.index(staged)
    assert events[-1] == target.parent


@pytest.mark.skipif(os.name != "posix", reason="POSIX atomic empty-directory replacement")
def test_posix_restore_replaces_empty_target_without_removing_it_first(
    archive: tuple[EncryptedBackupManager, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, backup = archive
    target = tmp_path / "target"
    target.mkdir()
    remove = Path.rmdir

    def refuse_gap(path: Path) -> None:
        assert path != target, "restore must atomically replace the empty target"
        remove(path)

    monkeypatch.setattr(Path, "rmdir", refuse_gap)
    manager.restore(backup, target)
    assert (target / "subject/cold/segment.bin").read_bytes() == b"private-archive-segment"
    assert backup.is_file()


def test_restore_flushes_final_files_after_privacy_replay_and_permission_hardening(
    archive: tuple[EncryptedBackupManager, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.interaction import erasure

    manager, backup = archive
    target = tmp_path / "target"
    opened: dict[int, Path] = {}
    flushed: set[Path] = set()
    staged_root: Path | None = None
    open_file, close_file, fsync = os.open, os.close, os.fsync
    harden, replay = at_rest._harden_tree, erasure.replay_privacy_deletions

    def replay_and_change(current: Path, restored: Path) -> None:
        replay(current, restored)
        (restored / "subject/cold/segment.bin").write_bytes(b"replayed-state")
        (restored / "replayed").mkdir()
        (restored / "replayed/marker").write_bytes(b"replayed-marker")

    def harden_and_record(root: Path) -> None:
        nonlocal staged_root
        harden(root)
        staged_root = root

    def record_open(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        descriptor = open_file(path, flags, mode, dir_fd=dir_fd)
        opened[descriptor] = Path(path)
        return descriptor

    def record_close(descriptor: int) -> None:
        opened.pop(descriptor, None)
        close_file(descriptor)

    def record_sync(descriptor: int) -> None:
        if staged_root is not None and descriptor in opened:
            flushed.add(opened[descriptor])
        fsync(descriptor)

    monkeypatch.setattr(erasure, "replay_privacy_deletions", replay_and_change)
    monkeypatch.setattr(at_rest, "_harden_tree", harden_and_record)
    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", record_close)
    monkeypatch.setattr(os, "fsync", record_sync)
    manager.restore(backup, target)
    relative_files = {path.relative_to(target) for path in target.rglob("*") if path.is_file()}
    assert staged_root is not None
    assert relative_files <= {
        path.relative_to(staged_root) for path in flushed if path.is_relative_to(staged_root)
    }
    assert (target / "subject/cold/segment.bin").read_bytes() == b"replayed-state"


@pytest.mark.parametrize("preexisting", [False, True], ids=["absent", "empty"])
@pytest.mark.parametrize("stage", ["file-sync", "tree-sync", "move", "parent-sync"])
def test_restore_failure_preserves_encrypted_input_and_retry(
    archive: tuple[EncryptedBackupManager, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preexisting: bool,
    stage: str,
) -> None:
    manager, backup = archive
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    target = tmp_path / "target"
    if preexisting:
        target.mkdir()
    sync, replace = at_rest._fsync_directory, Path.replace
    sync_tree, fsync = at_rest._sync_restore_tree, os.fsync
    syncing_tree = False

    def interrupt_file_sync(descriptor: int) -> None:
        if syncing_tree and stage == "file-sync":
            raise OSError("synthetic restore persistence failure")
        fsync(descriptor)

    def record_tree_sync(path: Path) -> None:
        nonlocal syncing_tree
        syncing_tree = True
        try:
            sync_tree(path)
        finally:
            syncing_tree = False

    def interrupt_sync(path: Path) -> None:
        if (stage == "tree-sync" and path.name == "restored") or (
            stage == "parent-sync" and path == target.parent
        ):
            raise OSError("synthetic restore persistence failure")
        sync(path)

    def interrupt_move(source: Path, destination: Path) -> Path:
        if destination == target:
            raise OSError("synthetic restore persistence failure")
        return replace(source, destination)

    with monkeypatch.context() as injection:
        injection.setattr(at_rest, "_fsync_directory", interrupt_sync)
        injection.setattr(at_rest, "_sync_restore_tree", record_tree_sync)
        injection.setattr(os, "fsync", interrupt_file_sync)
        if stage == "move":
            if os.name == "nt":
                injection.setattr(at_rest, "_windows_move_restored_directory", interrupt_move)
            else:
                injection.setattr(Path, "replace", interrupt_move)
        with pytest.raises(OSError, match="synthetic restore persistence failure"):
            manager.restore(backup, target)
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == digest
    assert not list(tmp_path.glob(".noyra-restore-*"))
    if stage == "parent-sync":
        assert (target / "noyra.sqlite3").is_file()
        with pytest.raises(FileExistsError):
            manager.restore(backup, target)
    elif preexisting:
        assert target.is_dir() and not any(target.iterdir())
    else:
        assert not target.exists()
    retry = manager.restore(backup, tmp_path / "retry")
    assert (retry / "subject/cold/segment.bin").read_bytes() == b"private-archive-segment"


def test_restore_syncs_new_parent_entries_toward_existing_ancestor(
    archive: tuple[EncryptedBackupManager, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, backup = archive
    target = tmp_path / "new/child/target"
    events: list[Path] = []
    sync = at_rest._fsync_directory

    def record(path: Path) -> None:
        events.append(path)
        sync(path)

    monkeypatch.setattr(at_rest, "_fsync_directory", record)
    manager.restore(backup, target)
    assert events[-3:] == [target.parent, target.parent.parent, tmp_path]


@pytest.mark.skipif(os.name != "nt", reason="Native Windows directory movement")
def test_windows_move_keeps_existing_directory_and_reports_native_failure(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "payload").write_bytes(b"retained")
    with pytest.raises(OSError):
        at_rest._windows_move_restored_directory(source, target)
    assert (source / "payload").read_bytes() == b"retained"
    assert target.is_dir() and not any(target.iterdir())
    target.rmdir()
    at_rest._windows_move_restored_directory(source, target)
    assert not source.exists()
    assert (target / "payload").read_bytes() == b"retained"


@pytest.mark.parametrize("preexisting", [False, True], ids=["absent", "empty"])
@pytest.mark.parametrize("stage", ["before-move", "after-move"])
def test_native_process_exit_keeps_backup_and_complete_staging_or_target(
    archive: tuple[EncryptedBackupManager, Path],
    restored_files: dict[str, str],
    tmp_path: Path,
    preexisting: bool,
    stage: str,
) -> None:
    manager, backup = archive
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    target = tmp_path / "target"
    if preexisting:
        target.mkdir()
    code = """
import os, sys
from pathlib import Path
from unittest.mock import patch
from noyra.core import at_rest
from noyra.core.at_rest import EncryptedBackupManager
target = Path(sys.argv[4])
native = at_rest._windows_move_restored_directory if os.name == 'nt' else Path.replace
def interrupt(source, destination):
    if destination == target and sys.argv[5] == 'before-move':
        os._exit(87)
    result = native(source, destination)
    if destination == target:
        os._exit(87)
    return result
owner, method = (
    (at_rest, '_windows_move_restored_directory') if os.name == 'nt' else (Path, 'replace')
)
with patch.object(owner, method, interrupt):
    EncryptedBackupManager(sys.argv[1], sys.argv[2]).restore(sys.argv[3], target)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(manager.data_root),
            str(manager.keyring_path),
            str(backup),
            str(target),
            stage,
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 87, result.stderr
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == digest
    stages = list(tmp_path.glob(".noyra-restore-*"))
    assert len(stages) == 1  # os._exit bypasses finally; it does not delete staging.
    complete = target if stage == "after-move" else stages[0] / "restored"
    assert (complete / "subject/cold/segment.bin").read_bytes() == b"private-archive-segment"
    assert tree_hashes(complete) == restored_files
    manager._verify_database(complete / "noyra.sqlite3")
    if stage == "before-move":
        assert target.exists() == (preexisting and os.name == "posix")
    retry = manager.restore(backup, tmp_path / "retry")
    assert (retry / "noyra.sqlite3").is_file()
