import importlib.util
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


def _helper(*, root_markers: bool = False) -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "deployment-maintenance.py"
    spec = importlib.util.spec_from_file_location("deployment_maintenance", path)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if root_markers and os.name == "posix":
        original = module.read_marker

        def read_test_marker(path: Path) -> Any:
            try:
                actual = path.lstat()
            except OSError:
                return original(path)
            root_stat = os.stat_result(
                (
                    actual.st_mode & ~0o022,
                    actual.st_ino,
                    actual.st_dev,
                    actual.st_nlink,
                    0,
                    actual.st_gid,
                    actual.st_size,
                    actual.st_atime,
                    actual.st_mtime,
                    actual.st_ctime,
                )
            )
            with patch.object(Path, "lstat", return_value=root_stat):
                return original(path)

        module.read_marker = read_test_marker
    return module


def test_volume_preflight_groups_requirements_on_same_device(tmp_path: Path) -> None:
    helper = _helper()
    for name in ("releases", "backups", "data"):
        (tmp_path / name).mkdir()
    with (
        patch.object(helper.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1})()),
        pytest.raises(ValueError, match="headroom"),
    ):
        helper.preflight(tmp_path / "releases", tmp_path / "backups", tmp_path / "data")


def test_gc_ignores_unmanaged_releases_and_requires_verified_replacement(tmp_path: Path) -> None:
    helper = _helper(root_markers=True)
    releases, backups = tmp_path / "releases", tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    for index in range(8):
        release = releases / f"release-{index}"
        release.mkdir()
        (release / helper.MARKER).write_text(
            json.dumps({"format": "noyra-verified-install/v1", "readiness": "passed"})
        )
        backup = backups / f"noyra-release-{index}.noyra-backup"
        backup.write_bytes(b"backup")
        backup.with_name(backup.name + ".verified.json").write_text(
            json.dumps(
                {
                    "format": "noyra-verified-upgrade-backup/v1",
                    "backup": backup.name,
                    "restore_verified": True,
                    "sha256": helper.file_hash(backup),
                }
            )
        )
    unmanaged = releases / "unmanaged"
    unmanaged.mkdir()
    plan = helper.gc_plan(releases, backups, keep=3)
    assert "unmanaged" not in plan["releases"]
    assert len(plan["backups"]) == 5
    newest = max(
        backups.glob("*.noyra-backup"), key=lambda path: (path.stat().st_mtime_ns, path.name)
    )
    newest.write_bytes(b"tampered")
    assert helper.gc_plan(releases, backups, keep=3)["backups"] == []


def _releases(tmp_path: Path) -> tuple[Any, Path, Path]:
    helper = _helper(root_markers=True)
    releases, backups = tmp_path / "releases", tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    for index in range(8):
        release = releases / f"release-{index}"
        release.mkdir()
        (release / helper.MARKER).write_text(
            json.dumps({"format": "noyra-verified-install/v1", "readiness": "passed"})
        )
        # Filesystem timestamp precision must not decide which fixtures are old.
        os.utime(release, (index + 1, index + 1))
    return helper, releases, backups


def test_gc_protects_current_previous_and_process_references(tmp_path: Path) -> None:
    helper, releases, backups = _releases(tmp_path)
    try:
        (tmp_path / "current").symlink_to(releases / "release-0", target_is_directory=True)
        (tmp_path / "previous").symlink_to(releases / "release-1", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink permission unavailable")
    proc = tmp_path / "proc" / "123"
    proc.mkdir(parents=True)
    (proc / "maps").write_text(f"12345 {releases / 'release-2'}/library.so\n", encoding="utf-8")
    (proc / "cmdline").write_bytes(str(releases / "release-3" / "python").encode() + b"\0")
    plan = helper.gc_plan(releases, backups, keep=3, proc_root=proc.parent)
    assert plan["releases"] == ["release-4"]


@pytest.mark.parametrize("reference", ["maps", "cmdline"])
def test_gc_defers_release_deletion_when_process_reference_is_oversized(
    tmp_path: Path, reference: str
) -> None:
    helper, releases, backups = _releases(tmp_path)
    proc = tmp_path / "proc" / "123"
    proc.mkdir(parents=True)
    (proc / "maps").write_bytes(b"")
    (proc / "cmdline").write_bytes(b"")
    (proc / reference).write_bytes(b"x" * (helper.PROCESS_REFERENCE_BYTES + 1))
    assert helper.gc_plan(releases, backups, keep=3, proc_root=proc.parent)["releases"] == []


def test_gc_ignores_incomplete_markers(tmp_path: Path) -> None:
    helper, releases, backups = _releases(tmp_path)
    (releases / "release-0" / helper.MARKER).write_text(
        json.dumps({"format": "noyra-verified-install/v1", "readiness": "failed"})
    )
    for index, changes in enumerate(
        ({"format": "other"}, {"backup": "different"}, {"sha256": "not-a-digest"})
    ):
        backup = backups / f"noyra-invalid-{index}.noyra-backup"
        backup.write_bytes(b"backup")
        marker = {
            "format": "noyra-verified-upgrade-backup/v1",
            "backup": backup.name,
            "restore_verified": True,
            "sha256": helper.file_hash(backup),
            **changes,
        }
        backup.with_name(backup.name + ".verified.json").write_text(json.dumps(marker))
    plan = helper.gc_plan(releases, backups, keep=3, proc_root=tmp_path / "no-proc")
    assert "release-0" not in plan["releases"]
    assert plan["backups"] == []


def test_gc_refuses_pointer_outside_releases(tmp_path: Path) -> None:
    helper, releases, backups = _releases(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (tmp_path / "current").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink permission unavailable")
    with pytest.raises(ValueError, match="unsafe_release_pointer"):
        helper.gc_plan(releases, backups, proc_root=tmp_path / "no-proc")


@pytest.mark.skipif(os.name != "posix", reason="POSIX marker ownership contract")
def test_marker_rejects_unprivileged_owner_and_group_write(tmp_path: Path) -> None:
    helper = _helper()
    marker = tmp_path / "marker.json"
    marker.write_text(json.dumps({"format": "noyra-verified-install/v1"}))
    actual = marker.stat()
    for owner, mode in ((1001, 0o100644), (0, 0o100664)):
        metadata = os.stat_result(
            (
                mode,
                actual.st_ino,
                actual.st_dev,
                actual.st_nlink,
                owner,
                actual.st_gid,
                actual.st_size,
                actual.st_atime,
                actual.st_mtime,
                actual.st_ctime,
            )
        )
        with patch.object(Path, "lstat", return_value=metadata):
            assert helper.read_marker(marker) is None


def test_installer_preflights_before_stopping_and_gc_follows_readiness() -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts" / "install-ubuntu.sh").read_text()
    assert source.index('deployment-maintenance.py" preflight') < source.index(
        "\nstop_old_service\n", source.index('deployment-maintenance.py" preflight')
    )
    assert source.index('deployment-maintenance.py" verify-backup') < source.index(
        'staging="$RELEASES_DIR/.staging-'
    )
    assert source.index("if start_and_check; then") < source.index('deployment-maintenance.py" gc')
