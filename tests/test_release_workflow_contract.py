from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
MANIFEST = (ROOT / "scripts" / "build-release-evidence.py").read_text(encoding="utf-8")


def test_release_workflow_uses_job_scoped_least_privilege_permissions() -> None:
    assert re.search(r"^permissions:\s*\{\}\s*$", WORKFLOW, re.MULTILINE)
    assert re.search(r"quality-gate:\n(?:.*\n){0,8}\s+permissions:\n\s+contents: read", WORKFLOW)
    assert re.search(
        r"release:\n(?:.*\n){0,8}\s+permissions:\n"
        r"\s+contents: write\n\s+actions: read\n\s+id-token: write\n"
        r"\s+attestations: write",
        WORKFLOW,
    )
    assert "environment: production" in WORKFLOW


def test_release_artifact_declares_schema_integrity_and_external_evidence() -> None:
    for marker in (
        "release-evidence.json",
        "EXPECTED_SHA",
        "external-gates.json",
        "SHA256SUMS.sig",
    ):
        assert marker in WORKFLOW, marker
    for marker in (
        '"schema_version"',
        '"integrity_registry_version"',
        '"external_gates_required": True',
    ):
        assert marker in MANIFEST, marker
