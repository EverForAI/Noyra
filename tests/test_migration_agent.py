from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from noyra.core.at_rest import VolumeEncryptionStatus
from noyra.core.types import canonical_json, content_hash
from noyra.migration.agent import (
    MigrationAgent,
    RestoreReport,
    TargetHealthReport,
)
from noyra.migration.bundle import BUNDLE_FORMAT, encrypt_bundle
from noyra.migration.recovery import RecoveryCoordinator, RecoveryRequest
from noyra.migration.trust import (
    TargetChallenge,
    create_recipient_pop_challenge,
    verify_recipient_pop,
)


@pytest.fixture(autouse=True)
def _test_volume_is_encrypted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "noyra.core.at_rest.VolumeEncryptionProbe.probe",
        lambda self, root, *, backend, attestation_path: VolumeEncryptionStatus(
            True, "test", "verified fixture volume", str(root)
        ),
    )


def _auth_headers(token: str, body: bytes, *, timestamp: int, nonce: str) -> dict[str, str]:
    digest = hashlib.sha256(body).hexdigest()
    signing_bytes = f"noyra-migration-agent-v1\n{timestamp}\n{nonce}\n{digest}".encode()
    signature = base64.urlsafe_b64encode(
        hmac.new(token.encode(), signing_bytes, hashlib.sha256).digest()
    ).decode()
    return {
        "Authorization": f"Noyra-HMAC {signature}",
        "X-Noyra-Timestamp": str(timestamp),
        "X-Noyra-Nonce": nonce,
        "X-Noyra-Body-SHA256": digest,
    }


def _disabled_activation_binding(
    agent: MigrationAgent, *, task_id: str, manifest_digest: str, artifact_id: str
) -> dict[str, object]:
    assert agent.recipient_key_fingerprint is not None
    binding = agent.binding_proof(
        {
            "task_id": task_id,
            "subject_id": "Noyra-0001",
            "target_id": agent.target_id,
            "source_epoch": "runtime-4",
            "manifest_digest": manifest_digest,
            "artifact_id": artifact_id,
            "recipient_key_fingerprint": agent.recipient_key_fingerprint,
            "credential_binding": {"references": {}, "fingerprints": {}},
            "wallet_binding": {"mode": "disabled"},
        }
    )
    return {
        "recipient_key_fingerprint": agent.recipient_key_fingerprint,
        "target_volume_proof_digest": content_hash(
            {
                "task_id": task_id,
                "manifest_digest": manifest_digest,
                "proof": binding["target_volume_proof"],
            }
        ),
        "credential_binding_digest": content_hash(
            {
                "task_id": task_id,
                "manifest_digest": manifest_digest,
                "binding": binding["credential_binding"],
            }
        ),
        "signer_binding_digest": None,
        "wallet_mode": "disabled",
        "wallet_proof_digest": None,
    }


def test_agent_accepts_bounded_secret_free_manifest() -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64)
    receipt = agent.receive({"artifact_id": "artifact-1", "byte_size": 12, "schema_version": 73})
    assert agent.validate(receipt, expected_digest=receipt.manifest_digest)["status"] == "healthy"


def test_agent_rejects_secret_fields() -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64)
    with pytest.raises(ValueError, match="secret"):
        agent.receive({"artifact_id": "artifact-1", "byte_size": 1, "api_key": "secret"})


def test_agent_rejects_nested_secret_fields() -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64)
    with pytest.raises(ValueError, match="secret"):
        agent.receive({"artifact_id": "artifact-1", "byte_size": 1, "credentials": {"token": "x"}})


def test_agent_challenge_is_signed_by_host_bound_target_key(tmp_path: Any) -> None:
    private = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode()
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint=hashlib.sha256(private.public_key().public_bytes_raw()).hexdigest(),
        signing_key=private,
        public_key=public,
        data_root=tmp_path,
    )
    challenge = TargetChallenge(
        nonce="nonce-1",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        source_epoch="source-1",
    )

    attestation = agent.challenge(challenge)

    assert attestation.target_id == "target-1"
    assert attestation.public_key == public
    assert agent.enroll({"subject_id": "Noyra-0001"}).host_identity
    assert attestation.verify().target_id == "target-1"


def test_agent_recipient_pop_requires_private_key_and_is_one_time(tmp_path: Any) -> None:
    signing = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate()
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint=hashlib.sha256(signing.public_key().public_bytes_raw()).hexdigest(),
        signing_key=signing,
        recipient_private_key=recipient,
        data_root=tmp_path,
    )
    challenge = create_recipient_pop_challenge(
        "target-1", recipient.public_key(), source_epoch="source-1"
    )
    proof = agent.recipient_pop(challenge)
    verify_recipient_pop(
        challenge,
        proof,
        target_public_key=base64.urlsafe_b64encode(signing.public_key().public_bytes_raw()).decode(
            "ascii"
        ),
    )
    with pytest.raises(ValueError, match="already consumed"):
        agent.recipient_pop(challenge)


def test_agent_persists_and_restores_recipient_encrypted_bundle(tmp_path: Any) -> None:
    signing = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate()
    data_root = tmp_path / "incoming-root"
    restore_root = tmp_path / "restore-root"
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint=hashlib.sha256(signing.public_key().public_bytes_raw()).hexdigest(),
        signing_key=signing,
        recipient_private_key=recipient,
        data_root=data_root,
        restore_root=restore_root,
    )
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE runtime_state(subject_id TEXT NOT NULL)")
        connection.execute("INSERT INTO runtime_state VALUES ('Noyra-0001')")
    encrypted = tmp_path / "payload.bundle"
    manifest = encrypt_bundle(
        source,
        encrypted,
        recipient_public_key=recipient.public_key(),
        context={
            "task_id": "task-bundle-1",
            "target_id": "target-1",
            "source_epoch": "source-1",
            "artifact_id": "bundle-1",
            "subject_id": "Noyra-0001",
            "schema_version": "79",
            "artifact_format": BUNDLE_FORMAT,
        },
    )
    manifest = {
        **manifest,
        "byte_size": manifest["ciphertext_size"],
        "artifact_sha256": manifest["ciphertext_sha256"],
    }
    receipt = agent.receive(manifest, artifact=encrypted.read_bytes())
    report = agent.restore(receipt, task_id="task-bundle-1")
    assert report.status == "restored"
    assert report.restore_path is not None
    assert Path(report.restore_path).is_file()


def test_agent_signs_recovery_proof_without_exposing_private_key() -> None:
    private = Ed25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes_raw()
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint=hashlib.sha256(public_bytes).hexdigest(),
        signing_key=private,
    )
    request = RecoveryRequest(
        task_id="recovery-task-1",
        standby_target_id="target-1",
        verified_backup_id="backup-1",
        source_failure_evidence="source unavailable",
        manifest_digest="a" * 64,
        restore_report_digest="b" * 64,
        health_report_digest="c" * 64,
    )

    encoded = agent.sign_recovery_proof(RecoveryCoordinator.signing_bytes(request))
    signature = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    private.public_key().verify(signature, RecoveryCoordinator.signing_bytes(request))
    assert "private" not in encoded.casefold()


def test_agent_rejects_recovery_signing_without_key() -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64)
    with pytest.raises(ValueError, match="signing identity"):
        agent.sign_recovery_proof(b"proof")


def test_target_activation_fails_closed_without_a_runtime_handoff_controller(
    tmp_path: Any,
) -> None:
    private = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes_raw()
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint=hashlib.sha256(public_bytes).hexdigest(),
        signing_key=private,
        recipient_private_key=recipient,
        data_root=tmp_path,
    )
    request: dict[str, object] = {
        "task_id": "task-activation-1",
        "subject_id": "Noyra-0001",
        "target_id": "target-1",
        "source_epoch": "runtime-4",
        "manifest_digest": "a" * 64,
        "artifact_id": "artifact-1",
        "artifact_sha256": "e" * 64,
        "health_report_digest": "b" * 64,
        "source_fence_digest": "d" * 64,
    }
    request.update(
        _disabled_activation_binding(
            agent, task_id="task-activation-1", manifest_digest="a" * 64, artifact_id="artifact-1"
        )
    )

    with pytest.raises(ValueError, match="runtime activation controller"):
        agent.activate(request)


def test_target_activation_returns_agent_signed_service_receipt(tmp_path: Any) -> None:
    private = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes_raw()

    class ActivationController:
        def activate(self, request: Any) -> dict[str, Any]:
            return {
                **dict(request),
                "status": "active",
                "service_unit": "noyra.service",
                "active_database_sha256": "c" * 64,
                "activated_at": "2026-10-03T00:00:00+00:00",
            }

    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint=hashlib.sha256(public_bytes).hexdigest(),
        signing_key=private,
        recipient_private_key=recipient,
        data_root=tmp_path,
        activation_controller=ActivationController(),
    )
    request: dict[str, object] = {
        "task_id": "task-activation-1",
        "subject_id": "Noyra-0001",
        "target_id": "target-1",
        "source_epoch": "runtime-4",
        "manifest_digest": "a" * 64,
        "artifact_id": "artifact-1",
        "artifact_sha256": "e" * 64,
        "health_report_digest": "b" * 64,
        "source_fence_digest": "d" * 64,
    }
    request.update(
        _disabled_activation_binding(
            agent, task_id="task-activation-1", manifest_digest="a" * 64, artifact_id="artifact-1"
        )
    )

    receipt = agent.activate(request)

    signature = base64.urlsafe_b64decode(
        receipt["target_signature"] + "=" * (-len(receipt["target_signature"]) % 4)
    )
    private.public_key().verify(
        signature,
        canonical_json(
            {key: value for key, value in receipt.items() if key != "target_signature"}
        ).encode(),
    )
    assert receipt["status"] == "active"
    assert receipt["service_unit"] == "noyra.service"
    assert receipt["active_database_sha256"] == "c" * 64


def test_target_activation_requires_a_source_fence_digest(tmp_path: Any) -> None:
    private = Ed25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes_raw()

    class ActivationController:
        def activate(self, request: Any) -> dict[str, Any]:
            return {
                **dict(request),
                "status": "active",
                "service_unit": "noyra.service",
                "active_database_sha256": "c" * 64,
                "activated_at": "2026-10-03T00:00:00+00:00",
            }

    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint=hashlib.sha256(public_bytes).hexdigest(),
        signing_key=private,
        data_root=tmp_path,
        activation_controller=ActivationController(),
    )
    request = {
        "task_id": "task-activation-1",
        "subject_id": "Noyra-0001",
        "target_id": "target-1",
        "source_epoch": "runtime-4",
        "manifest_digest": "a" * 64,
        "artifact_id": "artifact-1",
        "health_report_digest": "b" * 64,
    }

    with pytest.raises(ValueError, match="migration activation request is invalid"):
        agent.activate(request)


def test_agent_persists_manifest_inside_private_root_and_restores_it(tmp_path: Any) -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64, data_root=tmp_path)
    database_path = tmp_path / "fixture.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE runtime_state(subject_id TEXT NOT NULL)")
        connection.execute("INSERT INTO runtime_state(subject_id) VALUES (?)", ("Noyra-0001",))
    artifact = database_path.read_bytes()
    manifest = {
        "artifact_id": "artifact-1",
        "byte_size": len(artifact),
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "artifact_format": "sqlite",
        "schema_version": 75,
        "subject_id": "Noyra-0001",
        "event_chain_tip": "b" * 64,
    }

    receipt = agent.receive(manifest, artifact=artifact)
    agent.restore_root = (tmp_path / "restored").resolve()
    agent.restore_root.mkdir(mode=0o700)
    agent._assert_private_directory(agent.restore_root)
    report = agent.restore(receipt, task_id="task-restore-1")
    health = agent.validate(report)

    manifest_path = tmp_path / "incoming" / "artifact-1.json"
    assert manifest_path.is_file()
    if __import__("os").name != "nt":
        assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert json.loads(manifest_path.read_text()) == manifest
    assert isinstance(report, RestoreReport)
    assert report.status == "restored"
    assert isinstance(health, TargetHealthReport)
    assert health["status"] == "healthy"
    assert health["manifest_digest"] == receipt.manifest_digest


def test_agent_restores_each_migration_task_to_its_own_directory(tmp_path: Any) -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64, data_root=tmp_path)
    database_path = tmp_path / "fixture.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE runtime_state(subject_id TEXT NOT NULL)")
        connection.execute("INSERT INTO runtime_state(subject_id) VALUES (?)", ("Noyra-0001",))
    artifact = database_path.read_bytes()
    receipt = agent.receive(
        {
            "artifact_id": "artifact-1",
            "byte_size": len(artifact),
            "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
            "artifact_format": "sqlite",
            "subject_id": "Noyra-0001",
        },
        artifact=artifact,
    )
    agent.restore_root = (tmp_path / "restored").resolve()
    agent.restore_root.mkdir(mode=0o700)

    first = agent.restore(receipt, task_id="task-restore-1")
    second = agent.restore(receipt, task_id="task-restore-2")

    assert first.restore_path == str(agent.restore_root / "task-restore-1" / "noyra.sqlite3")
    assert second.restore_path == str(agent.restore_root / "task-restore-2" / "noyra.sqlite3")
    assert Path(first.restore_path).is_file()
    assert Path(second.restore_path).is_file()


def test_agent_rejects_manifest_path_traversal_and_restore_digest_mismatch(tmp_path: Any) -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64, data_root=tmp_path)
    with pytest.raises(ValueError, match="path"):
        agent.receive({"artifact_id": "../escape", "byte_size": 1})
    receipt = agent.receive(
        {"artifact_id": "artifact-1", "byte_size": 1, "artifact_format": "sqlite"}, artifact=b"x"
    )
    with pytest.raises(ValueError, match="digest"):
        agent.restore(receipt, expected_digest="0" * 64)


def test_agent_rejects_receipt_path_for_another_artifact(tmp_path: Any) -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64, data_root=tmp_path)
    first = agent.receive(
        {"artifact_id": "artifact-1", "byte_size": 1, "artifact_format": "sqlite"}, artifact=b"x"
    )
    second = agent.receive(
        {"artifact_id": "artifact-2", "byte_size": 1, "artifact_format": "sqlite"}, artifact=b"x"
    )
    forged = type(first)(
        second.artifact_id,
        first.manifest_digest,
        first.byte_size,
        first.status,
        first.manifest_path,
    )
    with pytest.raises(ValueError, match="path"):
        agent.restore(forged)


def test_agent_http_authentication_is_signed_and_replay_safe(tmp_path: Any) -> None:
    token = "session-" + "a" * 32
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint="a" * 64,
        data_root=tmp_path,
        session_token=token,
    )
    body = b'{"operation":"enroll"}'
    headers = _auth_headers(token, body, timestamp=1_000, nonce="nonce-000000000001")
    agent.authenticate_request(headers, body, now=1_000)
    with pytest.raises(ValueError, match="already used"):
        agent.authenticate_request(headers, body, now=1_000)
    tampered_headers = _auth_headers(
        token, b"tampered", timestamp=1_001, nonce="nonce-000000000002"
    )
    with pytest.raises(ValueError, match="body digest"):
        agent.authenticate_request(tampered_headers, body, now=1_001)
    with pytest.raises(ValueError, match="stale"):
        agent.authenticate_request(
            _auth_headers(token, body, timestamp=1_000, nonce="nonce-000000000003"),
            body,
            now=1_400,
        )


def test_agent_incoming_quota_and_ttl_cleanup_are_durable(tmp_path: Any) -> None:
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint="a" * 64,
        data_root=tmp_path,
        max_incoming_bytes=200,
        max_incoming_files=1,
        artifact_ttl_seconds=60,
    )
    first = agent.receive(
        {"artifact_id": "artifact-1", "byte_size": 1, "artifact_format": "sqlite"}, artifact=b"x"
    )
    with pytest.raises(ValueError, match="quota"):
        agent.receive(
            {"artifact_id": "artifact-2", "byte_size": 1, "artifact_format": "sqlite"},
            artifact=b"x",
        )
    assert first.manifest_path is not None
    os.utime(first.manifest_path, (0, 0))
    assert agent.cleanup_expired(now=100) == 1
    agent.receive(
        {"artifact_id": "artifact-2", "byte_size": 1, "artifact_format": "sqlite"}, artifact=b"x"
    )


def test_agent_chunked_receive_is_resumable_idempotent_and_bound_to_manifest(tmp_path: Any) -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64, data_root=tmp_path)
    artifact = (b"chunked migration payload" * 700)[:12_000]
    manifest = {
        "artifact_id": "chunked-1",
        "byte_size": len(artifact),
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "artifact_format": "sqlite",
        "schema_version": 75,
        "subject_id": "Noyra-0001",
    }
    chunk_bytes = 4096
    chunks = [
        artifact[offset : offset + chunk_bytes] for offset in range(0, len(artifact), chunk_bytes)
    ]
    count = len(chunks)

    first = agent.receive_chunk(
        manifest,
        chunk_index=1,
        chunk_count=count,
        chunk_bytes=chunk_bytes,
        chunk_sha256=hashlib.sha256(chunks[1]).hexdigest(),
        chunk=chunks[1],
    )
    assert first.complete is False
    duplicate = agent.receive_chunk(
        manifest,
        chunk_index=1,
        chunk_count=count,
        chunk_bytes=chunk_bytes,
        chunk_sha256=hashlib.sha256(chunks[1]).hexdigest(),
        chunk=chunks[1],
    )
    assert duplicate.received_chunks == first.received_chunks
    with pytest.raises(ValueError, match="conflicts"):
        agent.receive_chunk(
            manifest,
            chunk_index=1,
            chunk_count=count,
            chunk_bytes=chunk_bytes,
            chunk_sha256=hashlib.sha256(b"different").hexdigest(),
            chunk=b"different",
        )

    result = None
    for index, payload in enumerate(chunks):
        result = agent.receive_chunk(
            manifest,
            chunk_index=index,
            chunk_count=count,
            chunk_bytes=chunk_bytes,
            chunk_sha256=hashlib.sha256(payload).hexdigest(),
            chunk=payload,
        )
    assert result is not None and result.complete is True
    assert result.artifact_path is not None and Path(result.artifact_path).read_bytes() == artifact


def test_agent_requires_live_encrypted_volume_check_before_persisting(tmp_path: Any) -> None:
    from noyra.core.at_rest import VolumeEncryptionStatus

    class Probe:
        def __init__(self) -> None:
            self.paths: list[Path] = []

        def probe(
            self, root: Path | str, *, backend: str, attestation_path: Path | None
        ) -> VolumeEncryptionStatus:
            del backend, attestation_path
            self.paths.append(Path(root))
            return VolumeEncryptionStatus(False, "test", "unencrypted")

    probe = Probe()
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint="a" * 64,
        data_root=tmp_path,
        volume_probe=probe,
    )
    with pytest.raises(ValueError, match="encrypted volume"):
        agent.receive({"artifact_id": "artifact-1", "byte_size": 1}, artifact=b"x")
    assert probe.paths == [tmp_path.resolve()]
    assert not (tmp_path / "incoming").exists()


def test_agent_rechecks_restore_volume_and_cannot_disable_requirement(tmp_path: Any) -> None:
    from noyra.core.at_rest import VolumeEncryptionStatus

    class Probe:
        def __init__(self) -> None:
            self.encrypted = True
            self.paths: list[Path] = []

        def probe(
            self, root: Path | str, *, backend: str, attestation_path: Path | None
        ) -> VolumeEncryptionStatus:
            del backend, attestation_path
            self.paths.append(Path(root))
            return VolumeEncryptionStatus(self.encrypted, "test", "test status")

    probe = Probe()
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint="a" * 64,
        data_root=tmp_path,
        volume_probe=probe,
    )
    receipt = agent.receive({"artifact_id": "artifact-1", "byte_size": 1}, artifact=b"x")
    probe.encrypted = False
    agent.restore_root = (tmp_path / "restore").resolve()
    with pytest.raises(ValueError, match="encrypted volume"):
        agent.restore(receipt, task_id="task-restore-1")
    assert not (agent.restore_root / "task-restore-1").exists()
    with pytest.raises(ValueError, match="cannot be disabled"):
        MigrationAgent(
            target_id="target-2",
            key_fingerprint="b" * 64,
            data_root=tmp_path / "other",
            require_encrypted_storage=False,
            volume_probe=probe,
        )


def test_agent_checks_encrypted_volume_before_activation_side_effect(tmp_path: Any) -> None:
    from noyra.core.at_rest import VolumeEncryptionStatus

    class Probe:
        def probe(
            self, root: Path | str, *, backend: str, attestation_path: Path | None
        ) -> VolumeEncryptionStatus:
            del root, backend, attestation_path
            return VolumeEncryptionStatus(False, "test", "unencrypted")

    class Controller:
        called = False

        def activate(self, request: dict[str, Any]) -> dict[str, Any]:
            del request
            self.called = True
            return {}

    private = Ed25519PrivateKey.generate()
    controller = Controller()
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint=hashlib.sha256(private.public_key().public_bytes_raw()).hexdigest(),
        signing_key=private,
        data_root=tmp_path,
        activation_controller=controller,
        volume_probe=Probe(),
    )
    request = {
        "task_id": "task-activate-1",
        "subject_id": "Noyra-0001",
        "target_id": "target-1",
        "source_epoch": "runtime-1",
        "manifest_digest": "a" * 64,
        "artifact_id": "artifact-1",
        "artifact_sha256": "b" * 64,
        "health_report_digest": "c" * 64,
        "source_fence_digest": "d" * 64,
        "recipient_key_fingerprint": "c" * 64,
        "target_volume_proof_digest": "e" * 64,
        "credential_binding_digest": "f" * 64,
        "signer_binding_digest": None,
        "wallet_mode": "disabled",
        "wallet_proof_digest": None,
    }
    with pytest.raises(ValueError, match="encrypted volume"):
        agent.activate(request)
    assert controller.called is False
