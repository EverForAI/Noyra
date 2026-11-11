from __future__ import annotations

import runpy
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD: dict[str, Any] = runpy.run_path(str(ROOT / "scripts/verify-root-permission-results.py"))


def report(
    path: Path,
    *,
    name: str | None = None,
    status: str | None = None,
    copies: int = 1,
    count: str = "1",
) -> Path:
    suite = ET.Element("testsuite", tests=count, skipped="0", failures="0", errors="0")
    for _ in range(copies):
        case = ET.SubElement(
            suite, "testcase", classname=GUARD["REQUIRED"][0], name=name or GUARD["REQUIRED"][1]
        )
        if status:
            ET.SubElement(case, status)
    ET.ElementTree(suite).write(path)
    return path


def test_native_root_case_is_required_and_not_just_a_green_summary(tmp_path: Path) -> None:
    path = report(tmp_path / "result.xml")
    GUARD["verify"](path)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "skipped"},
        {"status": "error"},
        {"status": "failure"},
        {"copies": 0},
        {"copies": 2},
        {"name": "some_other_passing_case"},
        {"count": "0"},
    ],
)
def test_missing_skipped_failed_or_duplicate_native_cases_fail_closed(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        GUARD["verify"](report(tmp_path / "result.xml", **change))


def test_root_case_runs_in_bounded_ubuntu_job_with_retained_evidence() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    section = workflow.split("      - name: Verify native root key rotation", 1)[1]
    section = section.split("      - run: python -m ruff", 1)[0]
    assert "matrix.os == 'ubuntu-24.04' && matrix.python-version == '3.12'" in section
    assert 'sudo "$(command -v python)" -B -m pytest' in section
    assert GUARD["REQUIRED"][1] in section
    assert '--junitxml="${RUNNER_TEMP}/root-permission.xml"' in section
    assert "python scripts/verify-root-permission-results.py --path" in section
    assert "id: root-permission" in section
    assert "if: always()" in section and "if-no-files-found: error" in section
    assert "steps.root-permission.outcome == 'success'" in section
    assert "steps.root-permission.outcome == 'failure'" in section
    assert workflow.index("      - name: Verify native root key rotation") < workflow.index(
        "      - name: Run complete tests in two verified partitions"
    )
    assert "-p no:cacheprovider" in section
