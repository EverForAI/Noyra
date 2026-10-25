from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
POWERSHELL_7 = shutil.which("pwsh")
WINDOWS_POWERSHELL = shutil.which("powershell")
POWERSHELLS = tuple(filter(None, (POWERSHELL_7,)))


def _run(powershell: str, script: str, *args: str, timeout: int = 300) -> dict[str, Any]:
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "tests/fixtures" / script),
            *args,
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    value: Any = json.loads(result.stdout)
    assert isinstance(value, dict), value
    return value


@pytest.fixture(scope="module")
def signed_fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if os.name != "nt" or POWERSHELL_7 is None:
        pytest.skip("native Windows signing fixture preparation requires PowerShell 7")
    root = tmp_path_factory.mktemp("r03-signed-fixtures")
    result = _run(
        POWERSHELL_7,
        "windows-r03-prepare-fixtures.ps1",
        "-Lifecycle",
        str(ROOT / "deploy/windows/noyra-lifecycle.ps1"),
        "-WorkRoot",
        str(root),
        timeout=60,
    )
    assert result["pin_a"] != result["pin_b"]
    assert len(result["pin_os"]) == 40
    return root / "fixture.json"


@pytest.mark.skipif(
    os.name != "nt" or not POWERSHELLS, reason="native Windows signatures and shared file handles"
)
@pytest.mark.parametrize("powershell", POWERSHELLS, ids=lambda value: Path(value).name)
def test_r03_single_authority_protocol_fault_crash_and_revocation_matrix(
    tmp_path: Path, signed_fixtures: Path, powershell: str
) -> None:
    lifecycle = ROOT / "deploy/windows/noyra-lifecycle.ps1"
    before = hashlib.sha256(lifecycle.read_bytes()).hexdigest()
    result = _run(
        powershell,
        "windows-r03-protocol-regression.ps1",
        "-Lifecycle",
        str(lifecycle),
        "-FixtureJson",
        str(signed_fixtures),
        "-WorkRoot",
        str(tmp_path),
        "-PowerShell",
        powershell,
        timeout=1200,
    )
    assert hashlib.sha256(lifecycle.read_bytes()).hexdigest() == before
    assert result["fault_matrix_cases"] == 108
    assert result["fault_matrix_points_per_policy"] == 18
    assert result["adoption_crash_points"] == 4
    assert result["native_locked_file_cases"] == 10
    assert result["invalid_target_cases"] == 6
    assert result["corrupt_or_unsafe_state_cases"] == 9
    for key in (
        "unchanged_product_r03_reproduced",
        "fault_matrix_revocation_safe",
        "fault_matrix_repeated_recovery_idempotent",
        "missing_authority_never_falls_back",
        "conflicting_and_stale_intents_rejected",
        "same_version_policy_change_not_guessed",
        "post_acceptance_candidate_damage_stays_blocked",
        "recovery_itself_can_crash_and_retry",
        "manifest_and_launcher_signers_cannot_self_authorize",
        "compatibility_mirrors_never_authoritative",
        "real_cross_process_and_legacy_lock_enforced",
        "explicit_development_and_strict_no_downgrade",
        "unchanged_old_entry_bypass_demonstrated",
        "prototype_is_not_deployable_without_entry_gate",
    ):
        assert result[key] is True, (key, result)
    assert result["launchers_executed"] == result["certificates_imported"] == 0
    evidence = os.environ.get("NOYRA_R03_VALIDATION_EVIDENCE_DIR")
    if evidence:
        output = Path(evidence)
        output.mkdir(parents=True, exist_ok=True)
        label = "ps7" if powershell == POWERSHELL_7 else "ps5"
        (output / f"native-{label}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


@pytest.mark.skipif(
    os.name != "nt" or WINDOWS_POWERSHELL is None, reason="Windows PowerShell is unavailable"
)
def test_windows_powershell_host_boundary_is_reported_without_faking_signed_coverage(
    tmp_path: Path, signed_fixtures: Path
) -> None:
    assert WINDOWS_POWERSHELL is not None
    capability = _run(WINDOWS_POWERSHELL, "windows-r03-host-capability.ps1", timeout=30)
    assert capability["declared_signed_release_runtime"] == "PowerShell 7"
    if not capability["exact_product_assembly_available"]:
        metadata = json.loads(signed_fixtures.read_text(encoding="utf-8"))
        install = tmp_path / "install"
        shutil.copytree(metadata["strict_base"], install)
        data = tmp_path / "data"
        data.mkdir()
        original = (install / "current.txt").read_bytes()
        result = subprocess.run(
            [
                WINDOWS_POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(ROOT / "tests/fixtures/windows-r03-protocol-worker.ps1"),
                "-Lifecycle",
                str(ROOT / "deploy/windows/noyra-lifecycle.ps1"),
                "-Root",
                str(install),
                "-Data",
                str(data),
                "-Action",
                "adopt",
                "-Pins",
                ",".join(metadata["pins_a"]),
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        assert result.returncode != 0
        assert "Package manifest signature is invalid" in result.stderr
        assert not (install / "r03-protocol-required").exists()
        assert (install / "current.txt").read_bytes() == original
        capability["signed_adoption_blocked_before_state_mutation"] = True
    capability["full_signed_protocol_matrix_executed_on_this_host"] = False
    evidence = os.environ.get("NOYRA_R03_VALIDATION_EVIDENCE_DIR")
    if evidence:
        (Path(evidence) / "windows-powershell-host-boundary.json").write_text(
            json.dumps(capability, indent=2), encoding="utf-8"
        )
