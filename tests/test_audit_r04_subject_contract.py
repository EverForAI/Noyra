from __future__ import annotations

import base64
import hashlib
import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.core import identity as identity_module
from noyra.core.at_rest import VolumeEncryptionStatus
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.identity import validate_subject_id
from noyra.core.types import canonical_json
from noyra.migration.activation_contract import (
    ACTIVATION_FORMAT,
    validate_activation_receipt,
    validate_activation_request,
)
from noyra.migration.agent import MigrationAgent, RestoreReport, TargetHealthReport
from noyra.migration.bundle import BUNDLE_FORMAT, encrypt_bundle
from noyra.migration.policy import MigrationPolicy
from noyra.migration.recovery import RecoveryCoordinator
from noyra.service import ServiceSettings

VALID_SUBJECTS = (
    "abc",
    "alice_001",
    "Noyra-0001",
    "Noyra-" + "x" * 120,
    "Noyra-" + "x" * 121,
    "Noyra-" + "x" * 122,
)
INVALID_SUBJECTS: tuple[Any, ...] = (
    None,
    1,
    True,
    "",
    "ab",
    "a" * 129,
    "_abc",
    "-abc",
    "a/b",
    "a\\b",
    "a:b",
    "a.b",
    "abc\n",
    " abc",
    "abc ",
    "中文abc",
    "../alice",
    "con",
    "CON",
    "pRn",
    "aUX",
    "nul",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
)


def _activation_request(subject_id: Any) -> dict[str, Any]:
    return {
        "format": ACTIVATION_FORMAT,
        "task_id": "task-contract-1",
        "subject_id": subject_id,
        "target_id": "target-1",
        "source_epoch": "source-1",
        "manifest_digest": "a" * 64,
        "artifact_id": "artifact-1",
        "artifact_sha256": "b" * 64,
        "restored_database_sha256": "c" * 64,
        "inventory_sha256": None,
        "health_report_digest": "d" * 64,
        "source_fence_digest": "e" * 64,
        "recipient_key_fingerprint": "f" * 64,
        "target_volume_proof_digest": "a" * 64,
        "credential_binding_digest": "b" * 64,
        "signer_binding_digest": None,
        "wallet_mode": "disabled",
        "wallet_proof_digest": None,
    }


def _binding_request(subject_id: Any, recipient: str = "a" * 64) -> dict[str, Any]:
    return {
        "task_id": "task-contract-1",
        "subject_id": subject_id,
        "target_id": "target-1",
        "source_epoch": "source-1",
        "manifest_digest": "a" * 64,
        "artifact_id": "artifact-1",
        "recipient_key_fingerprint": recipient,
        "credential_binding": {"references": {}, "fingerprints": {}},
        "wallet_binding": {"mode": "disabled"},
    }


@pytest.mark.parametrize("subject_id", VALID_SUBJECTS)
def test_legal_subject_preserves_identity_across_enrollment_bundle_restore_and_proofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subject_id: str
) -> None:
    monkeypatch.setattr(
        "noyra.core.at_rest.VolumeEncryptionProbe.probe",
        lambda self, root, *, backend, attestation_path: VolumeEncryptionStatus(
            True, "test", "isolated fixture volume", str(root)
        ),
    )
    database = Database(tmp_path / "source.sqlite3")
    IdentityStore(database).ensure(subject_id, "a" * 64)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO runtime_state(subject_id,state,reason,version,changed_at) "
            "VALUES (?, 'active', 'test', 1, '2026-10-25T00:00:00+00:00')",
            (subject_id,),
        )
    settings = ServiceSettings(data_dir=tmp_path, subject_id=subject_id, genesis_hash="a" * 64)
    assert settings.subject_id == MigrationPolicy.default(subject_id).subject_id == subject_id
    signing = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate()
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint=hashlib.sha256(signing.public_key().public_bytes_raw()).hexdigest(),
        signing_key=signing,
        recipient_private_key=recipient,
        data_root=tmp_path / "agent",
        restore_root=tmp_path / "restored",
    )
    assert agent.enroll({"subject_id": subject_id}).target_id == "target-1"
    context = {
        "task_id": "task-contract-1",
        "target_id": "target-1",
        "source_epoch": "source-1",
        "subject_id": subject_id,
        "artifact_id": "artifact-1",
        "schema_version": str(CURRENT_SCHEMA_VERSION),
        "artifact_format": BUNDLE_FORMAT,
    }
    bundle_path = tmp_path / "bundle.bin"
    manifest = encrypt_bundle(
        database.path, bundle_path, recipient_public_key=recipient.public_key(), context=context
    )
    manifest.update(
        byte_size=manifest["ciphertext_size"],
        artifact_sha256=manifest["ciphertext_sha256"],
    )
    receipt = agent.receive(manifest, artifact=bundle_path.read_bytes())
    restored = agent.restore(receipt, task_id=context["task_id"])
    assert isinstance(restored, RestoreReport)
    assert restored.subject_id == subject_id
    assert restored.restore_path is not None
    with sqlite3.connect(restored.restore_path) as connection:
        assert connection.execute("SELECT subject_id FROM runtime_state").fetchone() == (
            subject_id,
        )
        assert connection.execute("SELECT subject_id FROM subject_identity").fetchone() == (
            subject_id,
        )
    health = agent.validate(restored)
    assert isinstance(health, TargetHealthReport)
    assert health.status == "healthy"
    assert agent.recipient_key_fingerprint is not None
    binding_request = _binding_request(subject_id, agent.recipient_key_fingerprint)
    binding_request["manifest_digest"] = receipt.manifest_digest
    proof = agent.binding_proof(binding_request)
    signed = {key: value for key, value in proof.items() if key != "target_signature"}
    signature = base64.urlsafe_b64decode(proof["target_signature"])
    signing.public_key().verify(signature, canonical_json(signed).encode())
    assert proof["subject_id"] == subject_id
    # Accepting more legal IDs must not permit reusing another subject's proof.
    with pytest.raises(ValueError, match="binding"):
        agent.binding_proof({**binding_request, "subject_id": "other_subject"})
    with pytest.raises(InvalidSignature):
        signing.public_key().verify(
            signature, canonical_json({**signed, "subject_id": "other_subject"}).encode()
        )
    request = _activation_request(subject_id)
    assert validate_activation_request(request) == request
    activation_receipt = {
        **request,
        "status": "active",
        "service_unit": "noyra.service",
        "active_database_sha256": "c" * 64,
        "activated_at": "2026-10-25T00:00:00+00:00",
    }
    validate_activation_receipt(request, activation_receipt)
    with pytest.raises(ValueError, match="binding"):
        validate_activation_receipt(request, {**activation_receipt, "subject_id": "other_subject"})


@pytest.mark.parametrize("subject_id", INVALID_SUBJECTS)
def test_subject_boundaries_continue_to_reject_unsafe_and_reserved_ids(subject_id: Any) -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64)
    with pytest.raises(ValueError):
        validate_subject_id(subject_id)
    with pytest.raises(ValueError, match="subject"):
        agent.enroll({"subject_id": subject_id})
    with pytest.raises(ValueError, match="identity"):
        validate_activation_request(_activation_request(subject_id))
    with pytest.raises(ValueError, match="identity"):
        agent.binding_proof(_binding_request(subject_id))


@pytest.mark.parametrize("subject_id", (*VALID_SUBJECTS, *INVALID_SUBJECTS))
def test_standalone_runner_subject_contract_matches_runtime(subject_id: Any) -> None:
    path = Path(__file__).parents[1] / "scripts/noyra-migration-runner.py"
    spec = importlib.util.spec_from_file_location("subject_contract_runner", path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    try:
        expected = validate_subject_id(subject_id)
    except ValueError:
        with pytest.raises(runner.RunnerError, match="invalid_subject_id"):
            runner._safe_id(subject_id, "subject_id")
    else:
        assert runner._safe_id(subject_id, "subject_id") == expected
        # Generic task IDs deliberately retain their separate wire contract.
        assert runner._safe_id("task.part:1", "task_id") == "task.part:1"


def test_activation_other_identity_and_digest_contracts_are_not_relaxed() -> None:
    request = _activation_request("alice_001")
    for field, value in (
        ("task_id", "../task"),
        ("source_epoch", "source/1"),
        ("target_id", "target:1"),
        ("artifact_id", "artifact/1"),
        ("manifest_digest", "invalid"),
        ("credential_binding_digest", None),
        ("signer_binding_digest", "a" * 64),
    ):
        with pytest.raises(ValueError):
            validate_activation_request({**request, field: value})


@pytest.mark.parametrize("subject_id", INVALID_SUBJECTS)
def test_invalid_subject_is_rejected_before_bundle_output_or_backup_io(
    tmp_path: Path, subject_id: Any
) -> None:
    context = {
        "task_id": "task-1",
        "target_id": "target-1",
        "source_epoch": "source-1",
        "subject_id": subject_id,
    }
    output = tmp_path / "bundle.bin"
    with pytest.raises(ValueError, match=r"subject|context"):
        encrypt_bundle(
            tmp_path / "absent-source",
            output,
            recipient_public_key=X25519PrivateKey.generate().public_key(),
            context=context,
        )
    assert not output.exists()
    database = Mock(spec=Database)
    coordinator = RecoveryCoordinator(database)
    with pytest.raises(ValueError, match="backup identity"):
        coordinator.register_backup(
            subject_id=subject_id,
            backup_id="backup-1",
            backup_path=str(tmp_path / "absent-backup"),
            content_hash_value="a" * 64,
            byte_size=1,
            schema_version=CURRENT_SCHEMA_VERSION,
            genesis_hash="b" * 64,
            key_id="backup-key-1",
            keyring_generation=1,
        )
    database.assert_not_called()
    assert database.mock_calls == []
    if subject_id is not None:
        agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64)
        with pytest.raises(ValueError, match="subject"):
            agent.receive({"artifact_id": "artifact-1", "byte_size": 0, "subject_id": subject_id})


def test_plain_runner_subject_validation_remains_stdlib_only() -> None:
    runner = Path(__file__).parents[1] / "scripts/noyra-migration-runner.py"
    program = (
        "import importlib.util, sys; "
        "spec = importlib.util.spec_from_file_location('runner', sys.argv[1]); "
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "assert module._safe_id('alice_001', 'subject_id') == 'alice_001'; "
        "assert not any(name == 'noyra' or name.startswith('noyra.') for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, str(runner)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_standalone_runner_mirror_tracks_the_complete_subject_contract() -> None:
    path = Path(__file__).parents[1] / "scripts/noyra-migration-runner.py"
    spec = importlib.util.spec_from_file_location("complete_contract_runner", path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert runner._RESERVED_SUBJECT_IDS == identity_module._WINDOWS_RESERVED_SUBJECT_IDS
    # Both validators use fullmatch; normalize only their redundant anchors.
    assert runner.SUBJECT_ID.pattern.removesuffix(r"\Z") == (
        identity_module.SUBJECT_ID_PATTERN.pattern.removeprefix("^").removesuffix("$")
    )
