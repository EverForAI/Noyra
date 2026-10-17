from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from noyra.core.storage_mode import (
    STORAGE_MARKER_FORMAT,
    STORAGE_MARKER_NAME,
    StorageMode,
    StorageUnavailableError,
    check_headroom,
    detect_storage_layout,
    ensure_system_disk_layout,
)


def test_plain_directory_is_system_disk_and_does_not_need_backup_directory(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()

    layout = detect_storage_layout(data, tmp_path / "backups")

    assert layout.mode is StorageMode.SYSTEM_DISK
    assert layout.writable is True
    assert layout.reason_code == "backup_path_missing"


def test_dedicated_marker_without_mount_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / STORAGE_MARKER_NAME).write_text(
        json.dumps({"format": STORAGE_MARKER_FORMAT, "mode": StorageMode.DEDICATED_MOUNT}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "noyra.core.storage_mode._findmnt",
        lambda path: (None, False),
    )

    layout = detect_storage_layout(data, tmp_path / "backups")

    assert layout.mode is StorageMode.UNAVAILABLE
    assert layout.reason_code == "storage_mount_missing"


def test_fstab_declared_mount_without_mount_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(
        "noyra.core.storage_mode._findmnt",
        lambda path: (None, False),
    )
    monkeypatch.setattr(
        "noyra.core.storage_mode._fstab_expects_mount",
        lambda path: path == data,
    )

    layout = detect_storage_layout(data, tmp_path / "backups")

    assert layout.mode is StorageMode.UNAVAILABLE
    assert layout.reason_code == "storage_mount_missing"


def test_explicit_dedicated_mount_without_mount_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("NOYRA_EXPECTED_STORAGE_MODE", "dedicated_mount")
    monkeypatch.setattr(
        "noyra.core.storage_mode._findmnt",
        lambda path: (None, False),
    )

    layout = detect_storage_layout(data, tmp_path / "backups")

    assert layout.mode is StorageMode.UNAVAILABLE
    assert layout.reason_code == "storage_mount_missing"


def test_invalid_explicit_storage_mode_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("NOYRA_EXPECTED_STORAGE_MODE", "unexpected")

    with pytest.raises(StorageUnavailableError, match="storage_mode_invalid"):
        detect_storage_layout(data, tmp_path / "backups")


def test_marker_symlink_is_rejected(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    target = tmp_path / "marker.json"
    target.write_text("{}", encoding="utf-8")
    try:
        (data / STORAGE_MARKER_NAME).symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(StorageUnavailableError, match="storage_marker_unsafe"):
        detect_storage_layout(data)


def test_ensure_system_disk_layout_preserves_existing_content(tmp_path: Path) -> None:
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir()
    sentinel = data / "noyra.sqlite3"
    sentinel.write_text("keep", encoding="utf-8")

    layout = ensure_system_disk_layout(data, backups, service_uid=0, service_gid=0)

    assert layout.mode is StorageMode.SYSTEM_DISK
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert (data / "upgrade" / "requests").is_dir()
    assert (data / "upgrade" / "processing").is_dir()


def test_ensure_system_disk_layout_rejects_symlink(tmp_path: Path) -> None:
    data = tmp_path / "data"
    target = tmp_path / "outside"
    target.mkdir()
    try:
        data.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(StorageUnavailableError, match="storage_symlink_rejected"):
        ensure_system_disk_layout(data, tmp_path / "backups", service_uid=0, service_gid=0)


def test_headroom_separates_runtime_backup_and_upgrade_estimates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda path: shutil._ntuple_diskusage(total=100, used=40, free=60),
    )

    headroom = check_headroom(
        tmp_path,
        runtime_bytes=10,
        backup_bytes=20,
        upgrade_bytes=30,
    )

    assert headroom.required_bytes == 60
    assert headroom.sufficient is True
    assert headroom.public()["backup_bytes"] == 20


def test_headroom_rejects_negative_estimates(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        check_headroom(tmp_path, runtime_bytes=-1, backup_bytes=0, upgrade_bytes=0)
