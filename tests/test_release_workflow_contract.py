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
        r"\s+contents: write\n\s+actions: read\n\s+deployments: read\n\s+id-token: write\n"
        r"\s+attestations: write",
        WORKFLOW,
    )
    assert "environment: production" in WORKFLOW


def test_release_requires_a_protected_external_gates_environment_and_same_sha_run() -> None:
    assert "environments/external-gates" in WORKFLOW
    assert 'rule.get("prevent_self_review") is True' in WORKFLOW
    assert "gh run list" in WORKFLOW
    assert '--commit "$EXPECTED_SHA"' in WORKFLOW
    assert 'latest = max(matching, key=lambda item: int(item["databaseId"]))' in WORKFLOW
    assert 'latest.get("conclusion") != "success"' in WORKFLOW
    assert "EXTERNAL_GATES_RUN_ID" not in WORKFLOW


def test_release_artifact_declares_schema_integrity_and_external_evidence() -> None:
    for marker in (
        "release-evidence.json",
        "EXPECTED_SHA",
        "external-gates.json",
        "EXTERNAL_GATES_PUBLIC_KEY must be configured",
        'cp "$external" dist/external-gates.json',
        "SHA256SUMS.sig",
    ):
        assert marker in WORKFLOW, marker
    for marker in (
        '"schema_version"',
        '"integrity_registry_version"',
        '"external_gates_required": True',
    ):
        assert marker in MANIFEST, marker


def test_missing_external_gate_download_reaches_the_stable_verifier_failure() -> None:
    download_start = WORKFLOW.index(
        "- name: Download independently produced external gate artifact"
    )
    verifier_start = WORKFLOW.index("python scripts/verify_external_gates.py", download_start)
    public_key_preflight_start = WORKFLOW.index(
        "EXTERNAL_GATES_PUBLIC_KEY must be configured", download_start
    )
    download_step = WORKFLOW[download_start : WORKFLOW.index("      - name:", download_start + 1)]

    assert "continue-on-error: true" in download_step
    assert verifier_start < public_key_preflight_start
