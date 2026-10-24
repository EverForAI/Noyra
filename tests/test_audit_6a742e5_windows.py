from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(os.name != "nt" or POWERSHELL is None, reason="native Windows signature APIs")
def test_windows_saved_trust_rotation_and_damaged_recovery(tmp_path: Path) -> None:
    assert POWERSHELL is not None
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "tests/fixtures/windows-verification-regression.ps1"),
            "-Lifecycle",
            str(ROOT / "deploy/windows/noyra-lifecycle.ps1"),
            "-WorkRoot",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert {
        "omitted_pins_load_saved_policy",
        "damaged_current_payload_rollback",
        "rotation_and_damaged_manifest_rollback",
        "revoked_actual_signer_rejected",
        "strict_receipt_rejects_unsigned_previous",
        "legacy_strict_damaged_current_rollback",
        "legacy_strict_policy_rejects_unsigned_previous",
        "legacy_unsigned_damaged_current_rollback",
        "saved_single_pin_reaches_executable_verification",
        "malformed_recovery_requirement_rejected",
    } <= evidence.keys(), evidence
    assert all(value is True for value in evidence.values()), evidence
    for directory in ("single", "saved-single", "untrusted", "bad-pin", "no-policy", "receipt-dir"):
        assert not (tmp_path / directory / "current.txt").exists()
