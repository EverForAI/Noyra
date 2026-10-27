from __future__ import annotations

import runpy
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def test_quality_workflow_scopes_pushes_and_cancels_stale_runs() -> None:
    trigger_block = CI[CI.index("on:\n") : CI.index("\npermissions:")]
    assert "  push:\n    branches:\n      - main" in trigger_block
    assert "  pull_request:\n    branches:\n      - main" in trigger_block
    assert "  push:\n\n" not in trigger_block
    assert "concurrency:\n" in trigger_block
    assert (
        "group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"
        in trigger_block
    )
    assert "cancel-in-progress: true" in trigger_block


def test_ci_cloud_profile_installs_cloud_lock_and_runs_security_tests_without_skips() -> None:
    start = CI.index("  cloud-profile:")
    section = CI[start:]
    assert (
        "--require-hashes --requirement requirements-dev.lock --requirement requirements-cloud.lock"
        in section
    )
    assert "tests/test_runtime_logging.py tests/test_cloud_dependency_compatibility.py" in section
    assert "cloud-security.xml" in section
    assert "python scripts/verify-cloud-security-results.py --path cloud-security.xml" in section
    assert "continue-on-error" not in section
    assert section.index("python -m pip check") < section.index(
        "Install locked cloud security test dependencies"
    )


def test_release_quality_gate_runs_the_same_locked_cloud_security_contract() -> None:
    gate = RELEASE[RELEASE.index("  quality-gate:") : RELEASE.index("\n  release:")]
    assert "requirements-cloud.lock" in gate
    assert "tests/test_runtime_logging.py tests/test_cloud_dependency_compatibility.py" in gate
    assert "python scripts/verify-cloud-security-results.py --path cloud-security.xml" in gate
    assert "--require-hashes --requirement requirements-cloud.lock" in gate
    assert "continue-on-error" not in gate
    assert "needs: quality-gate" in RELEASE


@pytest.mark.parametrize(
    "damage",
    ["none", "missing", "empty", "skipped", "failure", "error", "duplicate", "collection"],
)
def test_cloud_evidence_verifier_requires_actual_sdk_execution(
    tmp_path: Path,
    damage: str,
) -> None:
    script = ROOT / "scripts" / "verify-cloud-security-results.py"
    required = runpy.run_path(str(script))["REQUIRED_CASES"]
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests="3", skipped="0", failures="0", errors="0")
    cases = [
        ET.SubElement(suite, "testcase", classname=module, name=name)
        for module, name in sorted(required)
    ]
    if damage == "missing":
        suite.remove(cases[0])
    elif damage == "empty":
        suite.clear()
    elif damage in {"skipped", "failure", "error"}:
        ET.SubElement(cases[0], damage)
    elif damage == "duplicate":
        ET.SubElement(suite, "testcase", attrib=cases[0].attrib)
    elif damage == "collection":
        suite.set("errors", "1")
    path = tmp_path / "result.xml"
    ET.ElementTree(root).write(path)
    result = subprocess.run(
        [sys.executable, str(script), "--path", str(path)],
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) == (damage == "none")
