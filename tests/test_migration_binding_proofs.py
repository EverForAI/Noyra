from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Mapping
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
    ReceiveReceipt,
    RestoreReport,
)
from noyra.migration.executor import MigrationExecutionReceipt
from noyra.migration.http_executor import (
    ArtifactBundle,
    HTTPMigrationExecutor,
    SQLiteArtifactProvider,
)
from noyra.migration.manager import MigrationTask


def _agent(tmp_path: Path) -> tuple[MigrationAgent, Ed25519PrivateKey]:
    signing = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate()

    class ActivationController:
        def activate(self, request: dict[str, Any]) -> dict[str, Any]:
            return {
                **request,
                "status": "active",
                "service_unit": "noyra.service",
                "active_database_sha256": "d" * 64,
                "activated_at": "2026-10-03T00:00:00+00:00",
            }

        def deactivate(self, request: dict[str, Any]) -> dict[str, Any]:
            return {"status": "deactivated", "task_id": request["task_id"]}

    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint=hashlib.sha256(signing.public_key().public_bytes_raw()).hexdigest(),
        signing_key=signing,
        recipient_private_key=recipient,
        data_root=tmp_path / "agent",
        restore_root=tmp_path / "restore",
        credential_references={"model": "systemd:model"},
        credential_fingerprints={"model": "a" * 64},
        signer_id="kms-prod",
        wallet_address="0xabc",
        activation_controller=ActivationController(),
    )
    return agent, signing


def _request(recipient_key_fingerprint: str) -> dict[str, object]:
    return {
        "task_id": "task-bind-1",
        "subject_id": "Noyra-0001",
        "target_id": "target-1",
        "source_epoch": "runtime-1",
        "manifest_digest": "b" * 64,
        "artifact_id": "bundle-1",
        "recipient_key_fingerprint": recipient_key_fingerprint,
        "credential_binding": {
            "references": {"model": "systemd:model"},
            "fingerprints": {"model": "a" * 64},
        },
        "wallet_binding": {
            "mode": "external_signer_rebind",
            "signer_id": "kms-prod",
            "address": "0xabc",
        },
    }


def test_agent_returns_signed_task_bound_binding_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "noyra.core.at_rest.VolumeEncryptionProbe.probe",
        lambda self, root, *, backend, attestation_path: VolumeEncryptionStatus(
            True, "test", "verified fixture volume", str(root)
        ),
    )
    agent, signing = _agent(tmp_path)
    response = agent.binding_proof(_request(str(agent.recipient_key_fingerprint)))

    assert response["status"] == "verified"
    assert response["target_generation"] == agent.generation
    assert response["credential_binding"]["status"] == "verified"
    assert response["wallet_binding"]["status"] == "verified"
    assert response["target_volume_proof"]["encrypted"] is True
    signed = {key: value for key, value in response.items() if key != "target_signature"}
    signature = base64.urlsafe_b64decode(
        str(response["target_signature"]) + "=" * (-len(str(response["target_signature"])) % 4)
    )
    signing.public_key().verify(signature, canonical_json(signed).encode())
    assert "kms-prod" in canonical_json(response)
    assert "private_key" not in canonical_json(response)


def test_binding_proof_retry_is_idempotent_and_record_tampering_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "noyra.core.at_rest.VolumeEncryptionProbe.probe",
        lambda self, root, *, backend, attestation_path: VolumeEncryptionStatus(
            True, "test", "verified fixture volume", str(root)
        ),
    )
    agent, _ = _agent(tmp_path)
    request = _request(str(agent.recipient_key_fingerprint))
    response = agent.binding_proof(request)
    assert agent.binding_proof(request) == response

    record = tmp_path / "agent" / "bindings" / "task-bind-1.json"
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["target_identity"] = "0" * 64
    record.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="signature"):
        agent.binding_proof(request)


def test_binding_proof_rejects_an_unregistered_recipient_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "noyra.core.at_rest.VolumeEncryptionProbe.probe",
        lambda self, root, *, backend, attestation_path: VolumeEncryptionStatus(
            True, "test", "verified fixture volume", str(root)
        ),
    )
    agent, _ = _agent(tmp_path)
    request = _request("c" * 64)
    with pytest.raises(ValueError, match="recipient key fingerprint"):
        agent.binding_proof(request)


def test_agent_consumes_local_wallet_approval_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "noyra.core.at_rest.VolumeEncryptionProbe.probe",
        lambda self, root, *, backend, attestation_path: VolumeEncryptionStatus(
            True, "test", "verified fixture volume", str(root)
        ),
    )
    agent, _ = _agent(tmp_path)
    request = _request(str(agent.recipient_key_fingerprint))
    request["wallet_binding"] = {
        "mode": "local_wallet_transfer",
        "address": "0xabc",
        "approval": {
            "approval_id": "approval-1",
            "task_id": "task-bind-1",
            "address": "0xabc",
            "channel_id": "channel-1",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        },
    }

    response = agent.binding_proof(request)
    assert response["wallet_binding"]["status"] == "verified"
    assert response["wallet_binding"]["approval_fingerprint"]
    assert agent.binding_proof(request) == response
    request["manifest_digest"] = "d" * 64
    with pytest.raises(ValueError, match="conflicts"):
        agent.binding_proof(request)


def test_agent_rejects_binding_context_or_configuration_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "noyra.core.at_rest.VolumeEncryptionProbe.probe",
        lambda self, root, *, backend, attestation_path: VolumeEncryptionStatus(
            True, "test", "verified fixture volume", str(root)
        ),
    )
    agent, _ = _agent(tmp_path)
    request = _request(str(agent.recipient_key_fingerprint))
    request["target_id"] = "other-target"
    with pytest.raises(ValueError, match="binding request"):
        agent.binding_proof(request)

    request = _request(str(agent.recipient_key_fingerprint))
    request["wallet_binding"] = {
        "mode": "external_signer_rebind",
        "signer_id": "wrong-kms",
        "address": "0xabc",
    }
    with pytest.raises(ValueError, match="signer"):
        agent.binding_proof(request)


def test_activation_rejects_a_binding_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "noyra.core.at_rest.VolumeEncryptionProbe.probe",
        lambda self, root, *, backend, attestation_path: VolumeEncryptionStatus(
            True, "test", "verified fixture volume", str(root)
        ),
    )
    agent, _ = _agent(tmp_path)
    request = _request(str(agent.recipient_key_fingerprint))
    binding = agent.binding_proof(request)
    wallet = binding["wallet_binding"]
    volume = binding["target_volume_proof"]
    credential = binding["credential_binding"]
    activation = {
        "task_id": request["task_id"],
        "subject_id": request["subject_id"],
        "target_id": request["target_id"],
        "source_epoch": request["source_epoch"],
        "manifest_digest": request["manifest_digest"],
        "artifact_id": request["artifact_id"],
        "artifact_sha256": "d" * 64,
        "health_report_digest": "e" * 64,
        "source_fence_digest": "f" * 64,
        "recipient_key_fingerprint": request["recipient_key_fingerprint"],
        "target_volume_proof_digest": content_hash(
            {
                "task_id": request["task_id"],
                "manifest_digest": request["manifest_digest"],
                "proof": volume,
            }
        ),
        "credential_binding_digest": content_hash(
            {
                "task_id": request["task_id"],
                "manifest_digest": request["manifest_digest"],
                "binding": credential,
            }
        ),
        "signer_binding_digest": content_hash(
            {
                "task_id": request["task_id"],
                "manifest_digest": request["manifest_digest"],
                "binding": wallet,
            }
        ),
        "wallet_mode": "external_signer_rebind",
        "wallet_proof_digest": content_hash(
            {
                "task_id": request["task_id"],
                "manifest_digest": request["manifest_digest"],
                "binding": wallet,
            }
        ),
    }
    activation["target_volume_proof_digest"] = "0" * 64
    with pytest.raises(ValueError, match="binding proof does not match"):
        agent.activate(activation)


class _AgentTransport:
    def __init__(self, agent: MigrationAgent, signing: Ed25519PrivateKey) -> None:
        self.agent = agent
        self.signing = signing

    def request(self, url: str, body: dict[str, Any], token: str) -> dict[str, Any]:
        del token
        operation = url.rsplit("/", 1)[-1]
        if operation == "recipient-pop":
            proof = self.agent.recipient_pop(body)
            return {
                "target_id": proof.target_id,
                "source_epoch": proof.source_epoch,
                "expires_at": proof.expires_at,
                "pop_nonce": proof.pop_nonce,
                "recipient_key_fingerprint": proof.recipient_key_fingerprint,
                "signature": proof.signature,
            }
        if operation == "bindings":
            return self.agent.binding_proof(body)
        if operation == "preflight":
            return self.agent.preflight(body["manifest"])
        if operation == "receive-chunk":
            receipt = self.agent.receive_chunk(
                body["manifest"],
                chunk_index=body["chunk_index"],
                chunk_count=body["chunk_count"],
                chunk_bytes=body["chunk_bytes"],
                chunk_sha256=body["chunk_sha256"],
                chunk=base64.b64decode(body["chunk_b64"], validate=True),
            )
            return receipt.__dict__.copy()
        if operation == "restore":
            restore_receipt = self.agent.restore(
                ReceiveReceipt(**{key: body[key] for key in ReceiveReceipt.__dataclass_fields__}),
                expected_digest=body["expected_digest"],
                task_id=body["task_id"],
            )
            return restore_receipt.__dict__.copy()
        if operation == "health":
            report = RestoreReport(
                **{key: body[key] for key in RestoreReport.__dataclass_fields__ if key in body}
            )
            health = self.agent.validate(report, expected_digest=body["expected_digest"]).to_dict()
            signed = {
                "task_id": body["task_id"],
                "subject_id": body["subject_id"],
                "target_id": report.target_id,
                "source_epoch": body["source_epoch"],
                "manifest_digest": report.manifest_digest,
                "artifact_id": report.artifact_id,
                "restore_report_digest": body["restore_report_digest"],
                "health_report_digest": content_hash(health),
            }
            health["target_signature"] = base64.urlsafe_b64encode(
                self.signing.sign(canonical_json(signed).encode())
            ).decode()
            return health
        if operation == "activate":
            return self.agent.activate(body)
        if operation == "deactivate":
            return self.agent.deactivate(body)
        raise AssertionError(url)


def test_http_executor_requires_and_records_agent_binding_proofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "noyra.core.at_rest.VolumeEncryptionProbe.probe",
        lambda self, root, *, backend, attestation_path: VolumeEncryptionStatus(
            True, "test", "verified fixture volume", str(root)
        ),
    )
    agent, signing = _agent(tmp_path)
    source_db = tmp_path / "source.sqlite3"
    with sqlite3.connect(source_db) as connection:
        connection.execute("CREATE TABLE runtime_state(subject_id TEXT NOT NULL)")
        connection.execute("INSERT INTO runtime_state VALUES ('Noyra-0001')")
    task = MigrationTask(
        task_id="task-bind-executor",
        proposal_id="proposal-1",
        subject_id="Noyra-0001",
        target_id="target-1",
        idempotency_key="idempotency-1",
        source_epoch="runtime-1",
        status="validating",
        policy_revision=1,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    artifact_provider = SQLiteArtifactProvider(source_db, tmp_path / "outgoing")
    target_public = base64.urlsafe_b64encode(signing.public_key().public_bytes_raw()).decode()
    recipient_public = (
        base64.urlsafe_b64encode(
            agent._recipient_private_key.public_key().public_bytes_raw()  # type: ignore[union-attr]
        )
        .decode()
        .rstrip("=")
    )
    recipient_fingerprint = hashlib.sha256(
        agent._recipient_private_key.public_key().public_bytes_raw()  # type: ignore[union-attr]
    ).hexdigest()
    fenced: list[str] = []

    def fence(_task_value: MigrationTask, epoch: str) -> str:
        fenced.append(epoch)
        return "f" * 64

    def unfence(_task_value: MigrationTask, epoch: str) -> None:
        fenced.remove(epoch)

    def artifact_resolver(task_value: MigrationTask, proof: Mapping[str, object]) -> ArtifactBundle:
        return artifact_provider(task_value, proof)

    executor = HTTPMigrationExecutor(
        target_resolver=lambda task_value: {
            "target_id": task_value.target_id,
            "endpoint": "https://target.example",
            "public_key": target_public,
            "recipient_public_key": recipient_public,
            "recipient_key_fingerprint": recipient_fingerprint,
            "enrollment_generation": 1,
        },
        token_resolver=lambda task_value: "t" * 32,
        artifact_resolver=artifact_resolver,
        source_fence=fence,
        source_unfence=unfence,
        transport=_AgentTransport(agent, signing),
        chunk_bytes=4096,
    )
    receipt = executor.execute(
        task,
        proof={
            "artifact_id": "migration-task-bind-executor",
            "credential_binding": {
                "references": {"model": "systemd:model"},
                "fingerprints": {"model": "a" * 64},
            },
            "wallet_binding": {
                "mode": "external_signer_rebind",
                "signer_id": "kms-prod",
                "address": "0xabc",
            },
        },
        source_epoch=task.source_epoch,
    )
    assert isinstance(receipt, MigrationExecutionReceipt)
    assert receipt.credential_binding_digest
    assert receipt.signer_binding_digest == receipt.wallet_proof_digest
    assert fenced == [task.source_epoch]
