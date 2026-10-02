from __future__ import annotations

import base64
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from scripts.prepare_external_gates import decode_external_gates_bundle
from scripts.verify_external_gates import _canonical_payload, validate_external_gates
from scripts.verify_external_gates import main as verify_external_gates_main

from noyra.service import _wallet_automation_from_env

_SIGNING_KEY = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("23" * 32))
_PUBLIC_KEY = base64.b64encode(
    _SIGNING_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
).decode("ascii")
_GATE_IDS = (
    "ubuntu_systemd",
    "encrypted_volume",
    "backup_restore",
    "migration_fence",
    "signer_kms",
    "reorg_nonce",
    "https_proxy",
    "soak",
)


def _sign(record: dict[str, Any]) -> dict[str, Any]:
    signature = _SIGNING_KEY.sign(_canonical_payload(record))
    record["signature"] = {
        "algorithm": "ed25519",
        "key_id": "release-key",
        "value": base64.b64encode(signature).decode("ascii"),
    }
    return record


def _record() -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    gates = [
        {
            "id": gate_id,
            "status": "passed",
            "executed_by": "operator@example.test",
            "reviewed_by": "reviewer@example.test",
            "started_at": (now - timedelta(minutes=1)).isoformat(),
            "finished_at": now.isoformat(),
            "evidence_refs": [f"evidence/{gate_id}.json"],
            "failure_reason": None,
        }
        for gate_id in _GATE_IDS
    ]
    return _sign(
        {
            "format": "noyra-external-gates/v1",
            "schema_version": 1,
            "commit_sha": "a" * 40,
            "status": "passed",
            "reviewed_at": now.isoformat(),
            "reviewer": {"id": "reviewer@example.test", "role": "release-reviewer"},
            "gates": gates,
        }
    )


def test_external_gate_schema_requires_fixed_reviewed_gate_set() -> None:
    record = _record()
    assert (
        validate_external_gates(
            record,
            expected_sha="a" * 40,
            now=datetime.now(UTC),
            public_key=_PUBLIC_KEY,
        )
        == ()
    )
    record["gates"] = list(record["gates"][:-1])
    _sign(record)
    errors = validate_external_gates(
        record,
        expected_sha="a" * 40,
        now=datetime.now(UTC),
        public_key=_PUBLIC_KEY,
    )
    assert "gate_ids" in errors


def test_external_gate_rejects_stale_or_unreviewed_evidence() -> None:
    record = _record()
    old = (datetime.now(UTC) - timedelta(hours=73)).replace(microsecond=0).isoformat()
    record["reviewed_at"] = old
    record["reviewer"] = {"id": "", "role": "release-reviewer"}
    _sign(record)
    errors = validate_external_gates(
        record,
        expected_sha="a" * 40,
        now=datetime.now(UTC),
        public_key=_PUBLIC_KEY,
    )
    assert {"reviewed_at", "reviewer"} <= set(errors)


def test_external_gate_rejects_wrong_sha_and_wrong_gate_reviewer() -> None:
    record = _record()
    record["gates"][0]["reviewed_by"] = "operator@example.test"
    _sign(record)

    errors = validate_external_gates(
        record,
        expected_sha="b" * 40,
        now=datetime.now(UTC),
        public_key=_PUBLIC_KEY,
    )

    assert "commit_sha" in errors
    assert "gate_reviewer:ubuntu_systemd" in errors


def test_external_gate_rejects_invalid_time_ordering() -> None:
    record = _record()
    record["gates"][0]["finished_at"] = record["gates"][0]["started_at"]
    _sign(record)

    errors = validate_external_gates(
        record,
        expected_sha="a" * 40,
        now=datetime.now(UTC),
        public_key=_PUBLIC_KEY,
    )

    assert "gate_window:ubuntu_systemd" in errors


def test_external_gate_invalid_signature_is_a_validation_error() -> None:
    record = _record()
    record["signature"]["value"] = base64.b64encode(bytes(64)).decode("ascii")

    errors = validate_external_gates(
        record,
        expected_sha="a" * 40,
        now=datetime.now(UTC),
        public_key=_PUBLIC_KEY,
    )

    assert "signature" in errors


def test_external_gate_rejects_secret_shaped_fields() -> None:
    record = _record()
    record["gates"][0]["api_key"] = "do-not-persist"
    _sign(record)

    errors = validate_external_gates(
        record,
        expected_sha="a" * 40,
        now=datetime.now(UTC),
        public_key=_PUBLIC_KEY,
    )

    assert "secret_field:gates[0].api_key" in errors


def test_external_gate_bundle_preparation_checks_sha_and_signature() -> None:
    encoded = base64.b64encode(json.dumps(_record()).encode("utf-8")).decode("ascii")

    assert (
        decode_external_gates_bundle(
            encoded,
            expected_sha="a" * 40,
            public_key=_PUBLIC_KEY,
        )["commit_sha"]
        == "a" * 40
    )
    with pytest.raises(ValueError, match="commit_sha"):
        decode_external_gates_bundle(
            encoded,
            expected_sha="b" * 40,
            public_key=_PUBLIC_KEY,
        )
    with pytest.raises(ValueError, match="bundle_base64"):
        decode_external_gates_bundle(
            "not base64!",
            expected_sha="a" * 40,
            public_key=_PUBLIC_KEY,
        )


def test_external_gate_cli_requires_configured_signature_verification_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "external-gates.json"
    path.write_text(json.dumps(_record()), encoding="utf-8")
    monkeypatch.delenv("EXTERNAL_GATES_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_external_gates.py", "--path", str(path), "--expected-sha", "a" * 40],
    )

    assert verify_external_gates_main() == 1
    assert "public_key_missing" in capsys.readouterr().out


def test_external_gate_cli_distinguishes_missing_and_invalid_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EXTERNAL_GATES_PUBLIC_KEY", _PUBLIC_KEY)
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_external_gates.py", "--path", str(missing), "--expected-sha", "a" * 40],
    )
    assert verify_external_gates_main() == 1
    assert "missing_artifact" in capsys.readouterr().out

    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_external_gates.py", "--path", str(invalid), "--expected-sha", "a" * 40],
    )
    assert verify_external_gates_main() == 1
    assert "invalid_json" in capsys.readouterr().out


def test_external_gate_workflow_is_separate_protected_and_sha_bound() -> None:
    root = Path(__file__).resolve().parents[1]
    evidence_workflow = (root / ".github/workflows/external-gates.yml").read_text(encoding="utf-8")
    release_workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "name: external-gates" in evidence_workflow
    assert "workflow_dispatch:" in evidence_workflow
    assert "environment: external-gates" in evidence_workflow
    assert "scripts.prepare_external_gates" in evidence_workflow
    assert "actions/upload-artifact" in evidence_workflow
    assert "release_sha:" in evidence_workflow
    assert "ref: ${{ inputs.release_sha }}" in evidence_workflow
    assert 'test "${GITHUB_SHA}" = "${EXPECTED_SHA}"' in evidence_workflow
    assert "external-gates-${{ inputs.release_sha }}" in evidence_workflow
    assert '"${EXPECTED_SHA}"' in evidence_workflow
    assert "gh run list" in release_workflow
    assert '--commit "$EXPECTED_SHA"' in release_workflow
    assert "actions/download-artifact" in release_workflow
    assert "run-id: ${{ steps.external-gates-run.outputs.run_id }}" in release_workflow
    assert "EXTERNAL_GATES_RUN_ID" not in release_workflow


def test_production_wallet_automation_requires_explicit_external_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOYRA_PROFILE", "production")
    monkeypatch.setenv("NOYRA_WALLET_AUTOMATION_ENABLED", "true")
    monkeypatch.setenv("NOYRA_WALLET_AUTOMATION_NETWORK_ID", "network")
    monkeypatch.setenv("NOYRA_WALLET_AUTOMATION_ASSET_ID", "asset")
    monkeypatch.setenv("NOYRA_WALLET_AUTOMATION_REWARD_AMOUNT", "1")
    monkeypatch.setenv("NOYRA_WALLET_AUTOMATION_ACCEPTANCE_CRITERIA", '["done"]')
    with pytest.raises(ValueError, match="external gate"):
        _wallet_automation_from_env()
