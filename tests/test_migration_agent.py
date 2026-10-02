from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.migration.agent import MigrationAgent, RestoreReport, TargetHealthReport
from noyra.migration.recovery import RecoveryCoordinator, RecoveryRequest
from noyra.migration.trust import TargetChallenge


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


def test_agent_challenge_is_signed_by_host_bound_target_key(tmp_path) -> None:
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


def test_agent_persists_manifest_inside_private_root_and_restores_it(tmp_path) -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64, data_root=tmp_path)
    manifest = {
        "artifact_id": "artifact-1",
        "byte_size": 12,
        "schema_version": 75,
        "subject_id": "Noyra-0001",
        "event_chain_tip": "b" * 64,
    }

    receipt = agent.receive(manifest)
    report = agent.restore(receipt)
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


def test_agent_rejects_manifest_path_traversal_and_restore_digest_mismatch(tmp_path) -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64, data_root=tmp_path)
    with pytest.raises(ValueError, match="path"):
        agent.receive({"artifact_id": "../escape", "byte_size": 1})
    receipt = agent.receive({"artifact_id": "artifact-1", "byte_size": 1})
    with pytest.raises(ValueError, match="digest"):
        agent.restore(receipt, expected_digest="0" * 64)


def test_agent_rejects_receipt_path_for_another_artifact(tmp_path) -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64, data_root=tmp_path)
    first = agent.receive({"artifact_id": "artifact-1", "byte_size": 1})
    second = agent.receive({"artifact_id": "artifact-2", "byte_size": 1})
    forged = type(first)(
        second.artifact_id,
        first.manifest_digest,
        first.byte_size,
        first.status,
        first.manifest_path,
    )
    with pytest.raises(ValueError, match="path"):
        agent.restore(forged)


def test_agent_http_authentication_is_signed_and_replay_safe(tmp_path) -> None:
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


def test_agent_incoming_quota_and_ttl_cleanup_are_durable(tmp_path) -> None:
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint="a" * 64,
        data_root=tmp_path,
        max_incoming_bytes=200,
        max_incoming_files=1,
        artifact_ttl_seconds=60,
    )
    first = agent.receive({"artifact_id": "artifact-1", "byte_size": 1})
    with pytest.raises(ValueError, match="quota"):
        agent.receive({"artifact_id": "artifact-2", "byte_size": 1})
    assert first.manifest_path is not None
    os.utime(first.manifest_path, (0, 0))
    assert agent.cleanup_expired(now=100) == 1
    agent.receive({"artifact_id": "artifact-2", "byte_size": 1})
