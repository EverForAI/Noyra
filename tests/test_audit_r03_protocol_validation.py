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
    assert result["format"] == "noyra-r03-validation/v2"
    product = result["production"]
    prototype = result["prototype"]
    safety = result["safety"]
    assert product["implementation"] == "production-lifecycle"
    assert prototype["implementation"] == "test-only-protocol"
    assert "fault_matrix_cases" not in product
    assert prototype["fault_matrix_cases"] == 108
    for key in (
        "production_r03_transaction_recovers",
        "production_r03_status_uses_authority",
        "production_r03_revocation_survives_recovery",
        "production_r03_rollback_transaction",
        "production_r03_bootstrap_fails_closed",
    ):
        assert product[key] is True, (key, product)
    assert prototype["legacy_mirror_remains_non_authoritative_during_prepared"] is True
    assert prototype["fault_matrix_points_per_policy"] == 18
    assert prototype["adoption_crash_points"] == 4
    assert prototype["native_locked_file_cases"] == 10
    assert prototype["invalid_target_cases"] == 6
    assert prototype["corrupt_or_unsafe_state_cases"] == 9
    for key in (
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
        "per_selection_policy_and_explicit_strict_enforcement",
        "prototype_is_not_deployable_without_entry_gate",
    ):
        assert prototype[key] is True, (key, prototype)
    assert safety["launchers_executed"] == safety["certificates_imported"] == 0
    evidence = os.environ.get("NOYRA_R03_VALIDATION_EVIDENCE_DIR")
    if evidence:
        Path(evidence).mkdir(parents=True, exist_ok=True)
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
        Path(evidence).mkdir(parents=True, exist_ok=True)
        (Path(evidence) / "windows-powershell-host-boundary.json").write_text(
            json.dumps(capability, indent=2), encoding="utf-8"
        )


@pytest.mark.skipif(
    os.name != "nt" or POWERSHELL_7 is None, reason="native Windows signatures require PowerShell 7"
)
def test_signature_policy_model_matches_real_lifecycle_contract(
    tmp_path: Path, signed_fixtures: Path
) -> None:
    assert POWERSHELL_7 is not None
    result = _run(
        POWERSHELL_7,
        "windows-signature-policy-regression.ps1",
        "-Lifecycle",
        str(ROOT / "deploy/windows/noyra-lifecycle.ps1"),
        "-FixtureJson",
        str(signed_fixtures),
        "-WorkRoot",
        str(tmp_path),
        timeout=240,
    )
    assert result["format"] == "noyra-windows-signature-policy/v1"
    assert result["production"]["implementation"] == "production-lifecycle"
    assert result["prototype"]["implementation"] == "test-only-protocol"
    product = result["production"]["cases"]
    prototype = result["prototype"]["cases"]
    assert len(product) == len(prototype) == 15
    assert product == prototype
    assert product["strict_to_development_saved_pins"]["state"]["strict"] is False
    assert product["explicit_strict_rejects_development"]["accepted"] is False
    assert product["signed_manifest_keeps_strict"]["state"]["strict"] is True
    assert product["signed_metadata_cannot_grant_trust"]["accepted"] is False
    assert product["same_version_policy_change_rejected"]["accepted"] is False
    assert result["safety"] == {"launchers_executed": 0, "certificates_imported": 0}
    evidence = os.environ.get("NOYRA_R03_VALIDATION_EVIDENCE_DIR")
    if evidence:
        destination = Path(evidence)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "signature-policy.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )


@pytest.mark.skipif(
    os.name != "nt" or POWERSHELL_7 is None, reason="native Windows signatures and file sharing"
)
def test_production_signature_policy_survives_acceptance_and_recovery_faults(
    tmp_path: Path, signed_fixtures: Path
) -> None:
    assert POWERSHELL_7 is not None
    result = _run(
        POWERSHELL_7,
        "windows-signature-policy-regression.ps1",
        "-Lifecycle",
        str(ROOT / "deploy/windows/noyra-lifecycle.ps1"),
        "-FixtureJson",
        str(signed_fixtures),
        "-WorkRoot",
        str(tmp_path),
        "-Mode",
        "Faults",
        timeout=240,
    )
    assert result["format"] == "noyra-windows-signature-policy/v1"
    assert result["production"]["implementation"] == "production-lifecycle"
    assert result["prototype"]["cases"] == {}
    assert result["production"]["cases"] == {}
    cases = result["production"]["fault_recovery"]
    assert len(cases) == 6
    for name, case in cases.items():
        before_acceptance = case["stage"] == "authority-before-acceptance"
        strict = case["direction"] == "development-to-signed"
        assert case["recovered"]["strict"] is (not strict if before_acceptance else strict), name
        assert case["recovered"]["phase"] == "ready", name
        if not before_acceptance:
            assert case["recovered"]["current"] == ("b3" if strict else "u1"), name
            assert case["interrupted"]["phase"] == (
                "prepared" if case["stage"] == "receipt-after-acceptance" else "committed"
            ), name
        assert case["repeated_recovery_stable"] and case["data_retained"], name
    assert result["safety"] == {"launchers_executed": 0, "certificates_imported": 0}
    evidence = os.environ.get("NOYRA_R03_VALIDATION_EVIDENCE_DIR")
    if evidence:
        output = Path(evidence)
        output.mkdir(parents=True, exist_ok=True)
        (output / "production-policy-recovery.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )


@pytest.mark.skipif(
    os.name != "nt" or POWERSHELL_7 is None, reason="native Windows signatures require PowerShell 7"
)
def test_product_only_evidence_does_not_claim_the_prototype_matrix(
    tmp_path: Path, signed_fixtures: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert POWERSHELL_7 is not None
    monkeypatch.setenv("R03_PRODUCT_ONLY", "1")
    result = _run(
        POWERSHELL_7,
        "windows-r03-protocol-regression.ps1",
        "-Lifecycle",
        str(ROOT / "deploy/windows/noyra-lifecycle.ps1"),
        "-FixtureJson",
        str(signed_fixtures),
        "-WorkRoot",
        str(tmp_path),
        "-PowerShell",
        POWERSHELL_7,
        timeout=120,
    )
    assert set(result) == {"format", "production", "prototype", "safety"}
    assert result["format"] == "noyra-r03-validation/v2"
    product = result["production"]
    assert product.pop("implementation") == "production-lifecycle"
    assert len(product) == 5 and all(value is True for value in product.values())
    assert all(key.startswith("production_") for key in product)
    assert result["prototype"] == {"implementation": "test-only-protocol"}
    assert result["safety"]["launchers_executed"] == 0
    assert result["safety"]["certificates_imported"] == 0
