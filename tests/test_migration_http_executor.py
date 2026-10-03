from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core.types import canonical_json, content_hash
from noyra.migration.executor import MigrationExecutionError
from noyra.migration.http_executor import (
    ArtifactBundle,
    HTTPMigrationExecutor,
)
from noyra.migration.manager import MigrationTask


class _Transport:
    def __init__(self, private: Ed25519PrivateKey) -> None:
        self.private = private
        self.calls: list[str] = []
        self.activation_body: dict[str, Any] | None = None
        self.activation_receipt: dict[str, Any] | None = None

    def request(self, url: str, body: dict[str, Any], token: str) -> dict[str, Any]:
        del token
        self.calls.append(url)
        if url.endswith("/v1/receive-chunk"):
            return {
                "artifact_id": body["manifest"]["artifact_id"],
                "manifest_digest": body["manifest_digest"],
                "byte_size": body["manifest"]["byte_size"],
                "chunk_index": body["chunk_index"],
                "chunk_count": body["chunk_count"],
                "received_chunks": body["chunk_count"],
                "complete": True,
                "manifest_path": "/target/incoming/artifact.json",
                "artifact_path": "/target/incoming/artifact.artifact",
                "artifact_sha256": body["manifest"]["artifact_sha256"],
            }
        if url.endswith("/v1/restore"):
            return {
                "target_id": "target-1",
                "generation": 1,
                "artifact_id": body["artifact_id"],
                "manifest_digest": body["expected_digest"],
                "status": "restored",
                "subject_id": "Noyra-0001",
                "event_chain_tip": None,
                "artifact_sha256": body["artifact_sha256"],
                "restore_path": "/target/restored/noyra.sqlite3",
            }
        if url.endswith("/v1/health"):
            restore = {
                key: body[key]
                for key in (
                    "target_id",
                    "generation",
                    "artifact_id",
                    "manifest_digest",
                    "status",
                    "subject_id",
                    "event_chain_tip",
                    "artifact_sha256",
                    "restore_path",
                )
            }
            report = {
                "target_id": "target-1",
                "artifact_id": restore["artifact_id"],
                "manifest_digest": restore["manifest_digest"],
                "status": "healthy",
                "host_identity": "host-1",
                "checks": {"database": True, "runtime": True},
            }
            payload = {
                "task_id": "task-1",
                "subject_id": "Noyra-0001",
                "target_id": "target-1",
                "source_epoch": "runtime-1",
                "manifest_digest": report["manifest_digest"],
                "artifact_id": report["artifact_id"],
                "restore_report_digest": content_hash(restore),
                "health_report_digest": content_hash(report),
            }
            return {
                **report,
                "target_signature": base64.urlsafe_b64encode(
                    self.private.sign(canonical_json(payload).encode())
                ).decode(),
            }
        if url.endswith("/v1/activate"):
            self.activation_body = dict(body)
            activation = {
                "task_id": body["task_id"],
                "subject_id": body["subject_id"],
                "target_id": "target-1",
                "source_epoch": body["source_epoch"],
                "manifest_digest": body["manifest_digest"],
                "artifact_id": body["artifact_id"],
                "artifact_sha256": body["artifact_sha256"],
                "health_report_digest": body["health_report_digest"],
                "source_fence_digest": body["source_fence_digest"],
                "status": "active",
                "service_unit": "noyra.service",
                "active_database_sha256": "c" * 64,
                "activated_at": "2026-10-03T00:00:00+00:00",
            }
            activation["target_signature"] = base64.urlsafe_b64encode(
                self.private.sign(canonical_json(activation).encode())
            ).decode()
            self.activation_receipt = activation
            return dict(activation)
        if url.endswith("/v1/deactivate"):
            return {"status": "deactivated"}
        raise AssertionError(url)


def _task() -> MigrationTask:
    return MigrationTask(
        task_id="task-1",
        proposal_id="proposal-1",
        subject_id="Noyra-0001",
        target_id="target-1",
        idempotency_key="idempotency-1",
        source_epoch="runtime-1",
        status="validating",
        policy_revision=1,
        expires_at="2099-01-01T00:00:00+00:00",
    )


def test_http_executor_transfers_restores_health_checks_and_activates(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode()
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"migration payload")
    manifest = {
        "artifact_id": "artifact-1",
        "byte_size": artifact.stat().st_size,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "artifact_format": "sqlite",
        "schema_version": 75,
        "subject_id": "Noyra-0001",
    }
    transport = _Transport(private)
    fenced: list[str] = []
    artifact_seen_fenced: list[bool] = []

    def resolve_artifact(task: MigrationTask, proof: Mapping[str, object]) -> ArtifactBundle:
        del task, proof
        artifact_seen_fenced.append(fenced == ["runtime-1"])
        return ArtifactBundle(artifact, manifest)

    def fence_source(task: MigrationTask, epoch: str) -> str:
        del task
        fenced.append(epoch)
        return "f" * 64

    executor = HTTPMigrationExecutor(
        target_resolver=lambda task: {
            "target_id": task.target_id,
            "endpoint": "https://target.example",
            "public_key": public,
        },
        token_resolver=lambda task: "t" * 32,
        artifact_resolver=resolve_artifact,
        source_fence=fence_source,
        source_unfence=lambda task, epoch: fenced.remove(epoch),
        transport=transport,
        chunk_bytes=4096,
    )
    proof = {
        "manifest_digest": content_hash(manifest),
        "artifact_id": "artifact-1",
        "restore_report": {
            "target_id": "target-1",
            "artifact_id": "artifact-1",
            "manifest_digest": content_hash(manifest),
            "status": "restored",
        },
        "health_report": {
            "target_id": "target-1",
            "artifact_id": "artifact-1",
            "manifest_digest": content_hash(manifest),
            "status": "healthy",
            "checks": {"database": True},
        },
        "target_signature": "x" * 80,
    }
    with pytest.raises(MigrationExecutionError, match="recipient_encrypted_bundle_unavailable"):
        executor.execute(_task(), proof=proof, source_epoch="runtime-1")

    assert fenced == []
    assert artifact_seen_fenced == []
    assert transport.calls == []


def test_http_executor_fails_closed_before_fencing_without_recipient_crypto_contract(
    tmp_path: Path,
) -> None:
    private = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode()
    fenced: list[str] = []

    def fence(task: MigrationTask, epoch: str) -> str:
        del task
        fenced.append(epoch)
        return "f" * 64

    artifact = tmp_path / "artifact.sqlite"
    artifact.write_bytes(b"plaintext sqlite bytes")
    executor = HTTPMigrationExecutor(
        target_resolver=lambda task: {
            "target_id": task.target_id,
            "endpoint": "https://target.example",
            "public_key": public,
        },
        token_resolver=lambda task: "t" * 32,
        artifact_resolver=lambda task, proof: ArtifactBundle(
            artifact,
            {
                "artifact_id": "artifact-1",
                "artifact_format": "sqlite",
                "byte_size": artifact.stat().st_size,
            },
        ),
        source_fence=fence,
        source_unfence=lambda task, epoch: None,
        transport=_Transport(private),
    )
    with pytest.raises(MigrationExecutionError, match="recipient_encrypted_bundle_unavailable"):
        executor.execute(_task(), proof={}, source_epoch="runtime-1")
    assert fenced == []
    assert fenced == []


def test_http_executor_refuses_unsafe_bundle_before_any_target_work(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode()
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"migration payload")
    manifest = {
        "artifact_id": "artifact-1",
        "byte_size": artifact.stat().st_size,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "artifact_format": "sqlite",
        "schema_version": 75,
        "subject_id": "Noyra-0001",
    }

    class UnsignedActivationTransport(_Transport):
        def request(self, url: str, body: dict[str, Any], token: str) -> dict[str, Any]:
            if url.endswith("/v1/activate"):
                return {
                    "task_id": body["task_id"],
                    "subject_id": body["subject_id"],
                    "target_id": body["target_id"],
                    "source_epoch": body["source_epoch"],
                    "manifest_digest": body["manifest_digest"],
                    "artifact_id": body["artifact_id"],
                    "health_report_digest": body["health_report_digest"],
                    "status": "active",
                    "service_unit": "noyra.service",
                    "active_database_sha256": "c" * 64,
                    "activated_at": "2026-10-03T00:00:00+00:00",
                }
            return super().request(url, body, token)

    transport = UnsignedActivationTransport(private)
    fenced: list[str] = []

    def fence(task: MigrationTask, epoch: str) -> str:
        del task
        fenced.append(epoch)
        return "f" * 64

    executor = HTTPMigrationExecutor(
        target_resolver=lambda task: {
            "target_id": task.target_id,
            "endpoint": "https://target.example",
            "public_key": public,
        },
        token_resolver=lambda task: "t" * 32,
        artifact_resolver=lambda task, proof: ArtifactBundle(artifact, manifest),
        source_fence=fence,
        source_unfence=lambda task, epoch: None,
        transport=transport,
        chunk_bytes=4096,
    )
    digest = content_hash(manifest)
    proof = {
        "manifest_digest": digest,
        "artifact_id": "artifact-1",
        "restore_report": {
            "target_id": "target-1",
            "artifact_id": "artifact-1",
            "manifest_digest": digest,
            "status": "restored",
        },
        "health_report": {
            "target_id": "target-1",
            "artifact_id": "artifact-1",
            "manifest_digest": digest,
            "status": "healthy",
            "checks": {"database": True},
        },
        "target_signature": "x" * 80,
    }

    with pytest.raises(MigrationExecutionError, match="recipient_encrypted_bundle_unavailable"):
        executor.execute(_task(), proof=proof, source_epoch="runtime-1")
    assert fenced == []
    assert transport.calls == []
