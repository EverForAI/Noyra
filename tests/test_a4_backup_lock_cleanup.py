from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from noyra.core.at_rest import BackupKeyring, EncryptedBackupManager
from noyra.core.database import Database
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.locking import ProcessLock


@pytest.fixture
def backup(tmp_path: Path) -> Iterator[EncryptedBackupManager]:
    data = tmp_path / "data"
    data.mkdir()
    Database(data / "noyra.sqlite3")
    keyring = tmp_path / "backup.keys"
    BackupKeyring.initialize(keyring)
    yield EncryptedBackupManager(data, keyring)


def assert_unlocked(backup: EncryptedBackupManager) -> None:
    lock = ProcessLock(backup.data_root / "noyra.sqlite3.lock")
    assert lock.acquire()
    lock.release()


def test_mkdtemp_failure_releases_runtime_lock_even_while_exception_is_retained(
    backup: EncryptedBackupManager,
    tmp_path: Path,
) -> None:
    error = OSError("synthetic full disk")
    with (
        patch("noyra.core.at_rest.tempfile.mkdtemp", side_effect=error),
        pytest.raises(OSError) as retained,
    ):
        backup.create(tmp_path / "failed.backup")
    assert retained.value is error
    assert retained.value.__traceback__ is not None
    assert_unlocked(backup)


@pytest.mark.parametrize("phase", ["clock", "snapshot", "copy", "manifest", "tar", "encrypt"])
def test_every_prepublication_failure_releases_lock_and_removes_plaintext_staging(
    backup: EncryptedBackupManager, tmp_path: Path, phase: str
) -> None:
    error = OSError("synthetic backup phase failure")
    methods = {
        "snapshot": "_snapshot_database",
        "copy": "_copy_persistent_tree",
        "manifest": "_manifest",
        "tar": "_write_tar",
        "encrypt": "_encrypt",
    }
    target = (
        patch("noyra.core.at_rest.utc_now", side_effect=error)
        if phase == "clock"
        else patch.object(backup, methods[phase], side_effect=error)
    )
    output = tmp_path / "failed.backup"
    with target, pytest.raises(OSError) as retained:
        backup.create(output)
    assert retained.value is error and retained.value.__traceback__ is not None
    assert_unlocked(backup)
    assert not output.exists()
    assert list(backup.data_root.glob(".noyra-backup-*")) == []


def test_cleanup_exception_still_releases_runtime_lock(
    backup: EncryptedBackupManager, tmp_path: Path
) -> None:
    error = OSError("synthetic cleanup failure")
    with (
        patch.object(
            backup, "_snapshot_database", side_effect=ValueError("synthetic snapshot fault")
        ),
        patch("noyra.core.at_rest.shutil.rmtree", side_effect=error),
        pytest.raises(OSError) as retained,
    ):
        backup.create(tmp_path / "failed.backup")
    assert retained.value is error
    assert_unlocked(backup)


def test_failed_backup_cannot_release_another_runtime_owner(
    backup: EncryptedBackupManager, tmp_path: Path
) -> None:
    owner = ProcessLock(backup.data_root / "noyra.sqlite3.lock")
    owner.acquire()
    try:
        with (
            patch("noyra.core.at_rest.tempfile.mkdtemp") as staging,
            pytest.raises(RuntimeOwnershipError, match="stop Noyra"),
        ):
            backup.create(tmp_path / "locked.backup")
        staging.assert_not_called()
        assert owner.held
        with pytest.raises(RuntimeOwnershipError):
            assert_unlocked(backup)
    finally:
        owner.release()
    assert_unlocked(backup)


def test_no_partial_backup_on_mkdtemp_failure_and_immediate_retry_can_restore(
    backup: EncryptedBackupManager, tmp_path: Path
) -> None:
    output = tmp_path / "retry.backup"
    with (
        patch("noyra.core.at_rest.tempfile.mkdtemp", side_effect=OSError("synthetic full disk")),
        pytest.raises(OSError) as retained,
    ):
        backup.create(output)
    assert retained.value.__traceback__ is not None
    assert_unlocked(backup)
    assert not output.exists()
    artifact = backup.create(output)
    assert artifact.path == output and output.is_file()
    restored = backup.restore(output, tmp_path / "restored")
    with Database(restored / "noyra.sqlite3", initialize=False).connection() as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert_unlocked(backup)
    assert list(backup.data_root.glob(".noyra-backup-*")) == []
