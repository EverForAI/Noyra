from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[^\]]+\])?==[^\s;]+(?:\s*;.*)?$")
HASH = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")
ACTION = re.compile(r"uses:\s+[^@\s]+@([0-9a-f]{40})(?:\s+#.*)?$")


def _assert_hashed_lock(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    requirements = 0
    hashes = 0
    pending_hashes = False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash="):
            assert pending_hashes
            assert HASH.fullmatch(line.removesuffix("\\").strip())
            hashes += 1
            continue
        requirement = line.removesuffix("\\").strip()
        assert REQUIREMENT.fullmatch(requirement), (path, line)
        if pending_hashes:
            assert hashes > 0
        requirements += 1
        pending_hashes = line.endswith("\\")
        hashes = 0
    if pending_hashes:
        assert hashes > 0
    assert requirements > 0


def test_runtime_and_profile_locks_are_hash_pinned() -> None:
    _assert_hashed_lock(ROOT / "requirements.lock")
    _assert_hashed_lock(ROOT / "requirements-dev.lock")
    _assert_hashed_lock(ROOT / "requirements-cloud.lock")


def test_ci_and_release_actions_are_immutable_and_install_hashes() -> None:
    for path in (
        ROOT / ".github" / "workflows" / "ci.yml",
        ROOT / ".github" / "workflows" / "release.yml",
    ):
        content = path.read_text(encoding="utf-8")
        action_lines = [line.strip() for line in content.splitlines() if " uses: " in f" {line}"]
        assert action_lines
        assert all(ACTION.search(line) for line in action_lines), path
        assert "--require-hashes" in content


def test_release_bundle_has_reproducible_build_and_attestation_steps() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "build-reproducible-wheel.py").read_text(encoding="utf-8")
    assert "actions/attest-build-provenance" in workflow
    assert "actions/attest-sbom" in workflow
    assert "SHA256SUMS" in workflow
    assert "cosign-linux-amd64" in workflow
    assert "sign-blob" in workflow
    assert "verify-blob" in workflow
    assert "certificate-oidc-issuer" in workflow
    assert "reproducible wheel comparison failed" in script
