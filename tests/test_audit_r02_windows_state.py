from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
POWERSHELLS = tuple(dict.fromkeys(filter(None, (shutil.which("pwsh"), shutil.which("powershell")))))


@pytest.mark.skipif(os.name != "nt" or not POWERSHELLS, reason="native Windows file replacement")
@pytest.mark.parametrize("powershell", POWERSHELLS)
def test_state_file_publish_rejects_containers_and_preserves_failed_replace(
    tmp_path: Path, powershell: str
) -> None:
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "tests/fixtures/windows-state-publish-regression.ps1"),
            "-Lifecycle",
            str(ROOT / "deploy/windows/noyra-lifecycle.ps1"),
            "-WorkRoot",
            str(tmp_path),
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert set(evidence) == {
        "normal_create_and_replace",
        "locked_replace_reports_failure",
        "failed_replace_retains_original",
        "junction_read_rejected",
        "junction_write_rejected",
        "junction_target_unchanged",
        "directory_after_validation_rejected",
        "raced_directory_remains_empty",
    }
    assert all(value is True for value in evidence.values()), evidence
