import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


def _helper() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "deployment-maintenance.py"
    spec = importlib.util.spec_from_file_location("deployment_maintenance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
    helper = _helper()
    releases, backups = tmp_path / "releases", tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    for index in range(8):
        release = releases / f"release-{index}"
        release.mkdir()
        (release / helper.MARKER).write_text(json.dumps({"format": "noyra-verified-install/v1"}))
        backup = backups / f"noyra-release-{index}.noyra-backup"
        backup.write_bytes(b"backup")
        backup.with_name(backup.name + ".verified.json").write_text(
            json.dumps({"restore_verified": True, "sha256": helper.file_hash(backup)})
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


def test_installer_preflights_before_stopping_and_gc_follows_readiness() -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts" / "install-ubuntu.sh").read_text()
    assert source.index('deployment-maintenance.py" preflight') < source.index(
        "\nstop_old_service\n", source.index('deployment-maintenance.py" preflight')
    )
    assert source.index('deployment-maintenance.py" verify-backup') < source.index(
        'staging="$RELEASES_DIR/.staging-'
    )
    assert source.index("if start_and_check; then") < source.index('deployment-maintenance.py" gc')
