from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from noyra.core.storage_mode import (
    BACKUP_STORAGE_IDENTITY_FORMAT,
    STORAGE_IDENTITY_FORMAT,
    STORAGE_MARKER_FORMAT,
    STORAGE_MARKER_NAME,
    StorageLayout,
    StorageMode,
    StorageUnavailableError,
    check_headroom,
    detect_storage_layout,
    ensure_system_disk_layout,
)

requires_storage_root = pytest.mark.skipif(
    os.name == "posix" and getattr(os, "geteuid", lambda: -1)() != 0,
    reason="valid root-owned storage identities and provisioning require root",
)


def test_plain_directory_is_system_disk_and_does_not_need_backup_directory(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()

    layout = detect_storage_layout(data, tmp_path / "backups")

    assert layout.mode is StorageMode.SYSTEM_DISK
    assert layout.writable is True
    assert layout.runtime_ready is True
    assert layout.backup_ready is False
    assert layout.reason_code == "backup_path_missing"
    assert layout.backup_reason_code == "backup_path_missing"


def test_backup_directory_is_reported_separately_from_runtime_readiness(tmp_path: Path) -> None:
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir()
    backups.mkdir()

    layout = detect_storage_layout(data, backups)

    assert layout.runtime_ready is True
    assert layout.backup_ready is True
    assert layout.backup_reason_code is None


def _write_backup_identity(path: Path, backup: Path, source: str) -> None:
    path.write_text(
        "\n".join(
            (
                f"NOYRA_BACKUP_STORAGE_IDENTITY_FORMAT={BACKUP_STORAGE_IDENTITY_FORMAT}",
                f"NOYRA_BACKUP_STORAGE_DIR={backup}",
                "NOYRA_BACKUP_STORAGE_MODE=dedicated_mount",
                f"NOYRA_BACKUP_STORAGE_SOURCE={source}",
            )
        )
        + "\n",
        encoding="utf-8",
    )


@requires_storage_root
def test_backup_mount_identity_accepts_expected_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir()
    backups.mkdir()
    identity = tmp_path / "backup-storage-identity.conf"
    _write_backup_identity(identity, backups, "/dev/backup")
    monkeypatch.setattr("noyra.core.storage_mode.BACKUP_STORAGE_IDENTITY_PATH", identity)
    monkeypatch.setattr(
        "noyra.core.storage_mode._findmnt",
        lambda path: ("/dev/backup", True) if path == backups else (None, False),
    )
    monkeypatch.setattr("noyra.core.storage_mode._fstab_expects_mount", lambda path: False)

    layout = detect_storage_layout(data, backups)

    assert layout.backup_ready is True
    assert layout.backup_reason_code is None


@pytest.mark.parametrize(
    ("mounted", "source", "reason"),
    [
        (False, None, "backup_mount_missing"),
        (True, "/dev/other", "backup_mount_identity_mismatch"),
    ],
)
@requires_storage_root
def test_backup_mount_identity_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mounted: bool,
    source: str | None,
    reason: str,
) -> None:
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir()
    backups.mkdir()
    identity = tmp_path / "backup-storage-identity.conf"
    _write_backup_identity(identity, backups, "/dev/expected")
    monkeypatch.setattr("noyra.core.storage_mode.BACKUP_STORAGE_IDENTITY_PATH", identity)
    monkeypatch.setattr(
        "noyra.core.storage_mode._findmnt",
        lambda path: (source, mounted) if path == backups else (None, False),
    )
    monkeypatch.setattr("noyra.core.storage_mode._fstab_expects_mount", lambda path: False)

    layout = detect_storage_layout(data, backups)

    assert layout.backup_ready is False
    assert layout.backup_reason_code == reason


def test_system_disk_backup_directory_without_identity_remains_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir()
    backups.mkdir()
    monkeypatch.setattr(
        "noyra.core.storage_mode.BACKUP_STORAGE_IDENTITY_PATH",
        tmp_path / "missing-identity",
    )
    monkeypatch.setattr("noyra.core.storage_mode._findmnt", lambda path: ("/dev/root", False))
    monkeypatch.setattr("noyra.core.storage_mode._fstab_expects_mount", lambda path: False)

    layout = detect_storage_layout(data, backups)

    assert layout.backup_ready is True


def test_reparse_check_walks_existing_ancestors(tmp_path: Path) -> None:
    data = tmp_path / "link" / "data"
    target = tmp_path / "real"
    target.mkdir()
    try:
        (tmp_path / "link").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(StorageUnavailableError, match="storage_symlink_rejected"):
        detect_storage_layout(data)


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


@requires_storage_root
def test_external_mount_identity_fails_closed_when_mount_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    identity = tmp_path / "storage-identity.conf"
    identity.write_text(
        "\n".join(
            (
                f"NOYRA_STORAGE_IDENTITY_FORMAT={STORAGE_IDENTITY_FORMAT}",
                f"NOYRA_STORAGE_DATA_DIR={data}",
                "NOYRA_STORAGE_MODE=dedicated_mount",
                "NOYRA_STORAGE_SOURCE=/dev/test-data",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("noyra.core.storage_mode.STORAGE_IDENTITY_PATH", identity)
    monkeypatch.setattr("noyra.core.storage_mode._findmnt", lambda path: (None, False))

    layout = detect_storage_layout(data, tmp_path / "backups")

    assert layout.mode is StorageMode.UNAVAILABLE
    assert layout.reason_code == "storage_mount_missing"


@requires_storage_root
def test_external_mount_identity_rejects_wrong_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    identity = tmp_path / "storage-identity.conf"
    identity.write_text(
        "\n".join(
            (
                f"NOYRA_STORAGE_IDENTITY_FORMAT={STORAGE_IDENTITY_FORMAT}",
                f"NOYRA_STORAGE_DATA_DIR={data}",
                "NOYRA_STORAGE_MODE=dedicated_mount",
                "NOYRA_STORAGE_SOURCE=/dev/expected",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("noyra.core.storage_mode.STORAGE_IDENTITY_PATH", identity)
    monkeypatch.setattr("noyra.core.storage_mode._findmnt", lambda path: ("/dev/other", True))

    layout = detect_storage_layout(data, tmp_path / "backups")

    assert layout.mode is StorageMode.UNAVAILABLE
    assert layout.reason_code == "storage_mount_identity_mismatch"


@requires_storage_root
def test_external_mount_identity_rejects_a_different_data_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    other_data = tmp_path / "other-data"
    data.mkdir()
    other_data.mkdir()
    identity = tmp_path / "storage-identity.conf"
    identity.write_text(
        "\n".join(
            (
                f"NOYRA_STORAGE_IDENTITY_FORMAT={STORAGE_IDENTITY_FORMAT}",
                f"NOYRA_STORAGE_DATA_DIR={data}",
                "NOYRA_STORAGE_MODE=dedicated_mount",
                "NOYRA_STORAGE_SOURCE=/dev/expected",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("noyra.core.storage_mode.STORAGE_IDENTITY_PATH", identity)
    monkeypatch.setattr("noyra.core.storage_mode._findmnt", lambda path: ("/dev/root", False))
    monkeypatch.setattr("noyra.core.storage_mode._fstab_expects_mount", lambda path: False)

    with pytest.raises(StorageUnavailableError, match="storage_identity_path_mismatch"):
        detect_storage_layout(other_data, tmp_path / "backups")


@requires_storage_root
def test_backup_mount_identity_rejects_a_different_backup_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    other_backups = tmp_path / "other-backups"
    data.mkdir()
    backups.mkdir()
    other_backups.mkdir()
    identity = tmp_path / "backup-storage-identity.conf"
    _write_backup_identity(identity, backups, "/dev/backup")
    monkeypatch.setattr("noyra.core.storage_mode.BACKUP_STORAGE_IDENTITY_PATH", identity)
    monkeypatch.setattr("noyra.core.storage_mode._findmnt", lambda path: ("/dev/root", False))
    monkeypatch.setattr("noyra.core.storage_mode._fstab_expects_mount", lambda path: False)

    with pytest.raises(StorageUnavailableError, match="backup_storage_identity_path_mismatch"):
        detect_storage_layout(data, other_backups)


def test_storage_check_can_require_backup_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from noyra.__main__ import _storage_check_command

    layout = StorageLayout(
        tmp_path / "data",
        tmp_path / "backups",
        tmp_path / "data" / "upgrade",
        StorageMode.SYSTEM_DISK,
        None,
        None,
        True,
        "backup_path_missing",
    )
    monkeypatch.setattr("noyra.core.storage_mode.detect_storage_layout", lambda *_: layout)

    _storage_check_command(
        ["--data-dir", str(layout.data_root), "--backup-dir", str(layout.backup_root)]
    )
    assert '"backup_ready": false' in capsys.readouterr().out

    with pytest.raises(SystemExit) as error:
        _storage_check_command(
            [
                "--data-dir",
                str(layout.data_root),
                "--backup-dir",
                str(layout.backup_root),
                "--require-backup",
            ]
        )
    assert error.value.code == 78


def test_storage_check_uses_environment_backup_root_when_not_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from noyra.__main__ import _storage_check_command

    data = tmp_path / "data"
    backup = tmp_path / "custom-backups"
    data.mkdir()
    backup.mkdir()
    observed: list[Path] = []
    layout = StorageLayout(
        data, backup, data / "upgrade", StorageMode.SYSTEM_DISK, None, None, True
    )

    def detect(data_root: Path, backup_root: Path) -> StorageLayout:
        observed.append(backup_root)
        return layout

    monkeypatch.setenv("NOYRA_BACKUP_ROOT", str(backup))
    monkeypatch.setattr("noyra.core.storage_mode.detect_storage_layout", detect)
    _storage_check_command(["--data-dir", str(data)])
    capsys.readouterr()
    assert observed == [backup]


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


@requires_storage_root
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


@requires_storage_root
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


@pytest.mark.parametrize("profile", ["production", "development", "public_preview"])
@pytest.mark.parametrize("value", [None, "", "  ", "runtime_only", "recovery_capable"])
def test_storage_profile_resolution_is_shared_across_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: str,
    value: str | None,
) -> None:
    from noyra.__main__ import _storage_check_command
    from noyra.core.storage_mode import resolve_storage_profile

    expected = (
        value.strip()
        if value and value.strip()
        else ("recovery_capable" if profile == "production" else "runtime_only")
    )
    assert resolve_storage_profile(profile, value) == expected
    monkeypatch.setenv("NOYRA_PROFILE", profile)
    if value is None:
        monkeypatch.delenv("NOYRA_STORAGE_PROFILE", raising=False)
    else:
        monkeypatch.setenv("NOYRA_STORAGE_PROFILE", value)
    data = tmp_path / "data"
    data.mkdir()
    layout = StorageLayout(
        data,
        tmp_path / "missing-backups",
        data / "upgrade",
        StorageMode.SYSTEM_DISK,
        None,
        None,
        True,
    )
    monkeypatch.setattr("noyra.core.storage_mode.detect_storage_layout", lambda *_: layout)
    if expected == "recovery_capable":
        with pytest.raises(SystemExit) as error:
            _storage_check_command(["--data-dir", str(data)])
        assert error.value.code == 78
    else:
        _storage_check_command(["--data-dir", str(data)])
    assert f'"storage_profile": "{expected}"' in capsys.readouterr().out


@pytest.mark.skipif(
    os.name != "posix" or getattr(os, "geteuid", lambda: 0)() == 0,
    reason="requires an actual unprivileged POSIX user",
)
@pytest.mark.parametrize("kind", ["data", "backup"])
def test_nonroot_cannot_supply_a_trusted_mount_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    data, backup = tmp_path / "data", tmp_path / "backups"
    data.mkdir()
    backup.mkdir()
    identity = tmp_path / "user-owned-identity.conf"
    _write_backup_identity(identity, backup, "/dev/expected")
    identity.chmod(0o600)
    monkeypatch.setattr(
        "noyra.core.storage_mode.STORAGE_IDENTITY_PATH",
        identity if kind == "data" else tmp_path / "missing-data-identity",
    )
    monkeypatch.setattr(
        "noyra.core.storage_mode.BACKUP_STORAGE_IDENTITY_PATH",
        identity if kind == "backup" else tmp_path / "missing-backup-identity",
    )
    with pytest.raises(StorageUnavailableError, match="storage_identity_unsafe"):
        detect_storage_layout(data, backup)


@pytest.mark.skipif(
    os.name != "posix" or getattr(os, "geteuid", lambda: 0)() == 0,
    reason="requires an actual unprivileged POSIX user",
)
def test_nonroot_provisioning_is_rejected_before_creating_paths(tmp_path: Path) -> None:
    data, backup = tmp_path / "data", tmp_path / "backup"
    with pytest.raises(PermissionError, match="requires root"):
        ensure_system_disk_layout(
            data,
            backup,
            service_uid=getattr(os, "geteuid", lambda: -1)(),
            service_gid=getattr(os, "getegid", lambda: -1)(),
        )
    assert not data.exists() and not backup.exists()
