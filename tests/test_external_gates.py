from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from scripts.verify_external_gates import validate_external_gates

from noyra.service import _wallet_automation_from_env


def _record() -> dict[str, object]:
    now = datetime.now(UTC).replace(microsecond=0)
    gates = [
        {
            "id": gate_id,
            "status": "passed",
            "executed_by": "operator@example.test",
            "reviewed_by": "reviewer@example.test",
            "started_at": now.isoformat(),
            "finished_at": now.isoformat(),
            "evidence_refs": [f"evidence/{gate_id}.json"],
            "failure_reason": None,
        }
        for gate_id in (
            "testnet_transfer",
            "signer_faults",
            "signer_isolation",
            "soak",
            "backup_restore",
            "operator_approval",
        )
    ]
    return {
        "format": "noyra-external-gates/v1",
        "schema_version": 1,
        "commit_sha": "a" * 40,
        "status": "passed",
        "reviewed_at": now.isoformat(),
        "reviewer": {"id": "reviewer@example.test", "role": "release-reviewer"},
        "gates": gates,
        "signature": {"algorithm": "ed25519", "key_id": "release-key", "value": ""},
    }


def test_external_gate_schema_requires_fixed_reviewed_gate_set() -> None:
    record = _record()
    assert validate_external_gates(record, expected_sha="a" * 40, now=datetime.now(UTC)) == ()
    record["gates"] = list(record["gates"])[:-1]  # type: ignore[arg-type]
    errors = validate_external_gates(record, expected_sha="a" * 40, now=datetime.now(UTC))
    assert "gate_ids" in errors


def test_external_gate_rejects_stale_or_unreviewed_evidence() -> None:
    record = _record()
    old = (datetime.now(UTC) - timedelta(hours=73)).replace(microsecond=0).isoformat()
    record["reviewed_at"] = old
    record["reviewer"] = {"id": "", "role": "release-reviewer"}
    errors = validate_external_gates(record, expected_sha="a" * 40, now=datetime.now(UTC))
    assert {"reviewed_at", "reviewer"} <= set(errors)


def test_external_gate_signature_is_required_when_verification_key_is_supplied() -> None:
    record = _record()
    errors = validate_external_gates(
        record,
        expected_sha="a" * 40,
        now=datetime.now(UTC),
        public_key="not-a-key",
    )
    assert "signature" in errors


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
