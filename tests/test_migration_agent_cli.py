from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core.at_rest import VolumeEncryptionStatus


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


def _module() -> Any:
    path = Path(__file__).parents[1] / "scripts" / "noyra-migration-agent.py"
    spec = importlib.util.spec_from_file_location("noyra_migration_agent_cli", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_identity_and_dispatch_keep_secret_out_of_manifest(tmp_path: Path) -> None:
    module = _module()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    identity = tmp_path / "identity.json"
    identity.write_text(
        json.dumps(
            {
                "target_id": "target-1",
                "key_fingerprint": hashlib.sha256(public).hexdigest(),
                "generation": 1,
                "public_key": base64.urlsafe_b64encode(public).decode(),
                "private_key": base64.urlsafe_b64encode(private.private_bytes_raw()).decode(),
            }
        )
    )
    if os.name != "nt":
        identity.chmod(0o600)
    agent = module.load_agent(identity, tmp_path / "data")
    with pytest.raises(ValueError, match="recipient-encrypted migration bundle"):
        module.dispatch(
            agent,
            "receive",
            {
                "artifact_id": "artifact-1",
                "byte_size": 1,
                "schema_version": 1,
                "artifact_format": "sqlite",
                "artifact_b64": base64.b64encode(b"x").decode(),
            },
        )
    assert not (tmp_path / "data" / "incoming").exists()


@pytest.mark.parametrize(
    "artifact_format", ["sqlite", "noyra-encrypted-backup", "noyra-migration-bundle/v1"]
)
def test_cli_dispatch_fails_closed_until_recipient_decryption_is_integrated(
    tmp_path: Path, artifact_format: str
) -> None:
    module = _module()
    agent = module.MigrationAgent(
        target_id="target-1", key_fingerprint="a" * 64, data_root=tmp_path / "data"
    )

    with pytest.raises(ValueError, match="recipient-encrypted migration bundle"):
        module.dispatch(
            agent,
            "receive",
            {
                "manifest": {
                    "artifact_id": "artifact-1",
                    "byte_size": 1,
                    "artifact_format": artifact_format,
                },
                "chunk_index": 0,
                "chunk_count": 1,
                "chunk_bytes": 4096,
                "chunk_sha256": hashlib.sha256(b"x").hexdigest(),
                "chunk_b64": base64.b64encode(b"x").decode(),
            },
        )
    assert not (tmp_path / "data" / "incoming").exists()


def test_cli_dispatch_fails_closed_on_restore_until_recipient_decryption_is_integrated(
    tmp_path: Path,
) -> None:
    module = _module()
    agent = module.MigrationAgent(
        target_id="target-1", key_fingerprint="a" * 64, data_root=tmp_path / "data"
    )

    with pytest.raises(ValueError, match="recipient-encrypted migration bundle"):
        module.dispatch(agent, "restore", {"artifact_id": "artifact-1"})


def test_cli_preflight_rejects_before_transfer_when_quota_is_insufficient(tmp_path: Path) -> None:
    module = _module()
    agent = module.MigrationAgent(
        target_id="target-1",
        key_fingerprint="a" * 64,
        data_root=tmp_path / "data",
        max_incoming_bytes=128,
    )
    with pytest.raises(ValueError, match="quota"):
        module.dispatch(
            agent,
            "preflight",
            {
                "manifest": {
                    "artifact_id": "artifact-1",
                    "byte_size": 256,
                    "artifact_format": "sqlite",
                }
            },
        )
    assert not (tmp_path / "data" / "incoming" / "artifact-1.artifact").exists()


def test_load_agent_uses_the_fixed_signed_root_activation_bridge(tmp_path: Path) -> None:
    module = _module()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    identity = tmp_path / "identity.json"
    identity.write_text(
        json.dumps(
            {
                "target_id": "target-1",
                "key_fingerprint": hashlib.sha256(public).hexdigest(),
                "generation": 1,
                "public_key": base64.urlsafe_b64encode(public).decode(),
                "private_key": base64.urlsafe_b64encode(private.private_bytes_raw()).decode(),
            }
        )
    )
    if os.name != "nt":
        identity.chmod(0o600)
    request_root = tmp_path / "requests"
    status_root = tmp_path / "status"

    agent = module.load_agent(
        identity,
        tmp_path / "agent-data",
        activation_request_root=request_root,
        activation_status_root=status_root,
    )

    from noyra.migration.activation import TargetActivationBridge

    assert isinstance(agent.activation_controller, TargetActivationBridge)
    assert agent.activation_controller.request_root == request_root
    assert agent.activation_controller.status_root == status_root


def test_identity_file_rejects_hardlinks(tmp_path: Path) -> None:
    module = _module()
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"target_id": "target-1", "key_fingerprint": "a" * 64}))
    identity.chmod(0o600)
    hardlink = tmp_path / "identity-hardlink.json"
    hardlink.hardlink_to(identity)

    with pytest.raises(ValueError, match="hard link"):
        module.load_agent(hardlink, tmp_path / "data")


def test_cli_dispatch_signs_health_and_persists_activation(tmp_path: Path) -> None:
    module = _module()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()

    class ActivationController:
        def activate(self, request: dict[str, Any]) -> dict[str, Any]:
            return {
                **request,
                "status": "active",
                "service_unit": "noyra.service",
                "active_database_sha256": "c" * 64,
                "activated_at": "2026-10-03T00:00:00+00:00",
            }

        def deactivate(self, request: dict[str, Any]) -> dict[str, Any]:
            return {"status": "deactivated", "task_id": request["task_id"]}

    agent = module.MigrationAgent(
        target_id="target-1",
        key_fingerprint=hashlib.sha256(public).hexdigest(),
        signing_key=private,
        data_root=tmp_path / "data",
        restore_root=tmp_path / "restore-root",
        activation_controller=ActivationController(),
    )
    restored_path = tmp_path / "restore-root" / "task-1" / "noyra.sqlite3"
    restored_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(restored_path) as connection:
        connection.execute("CREATE TABLE runtime_state(subject_id TEXT NOT NULL)")
        connection.execute("INSERT INTO runtime_state(subject_id) VALUES (?)", ("Noyra-0001",))
    artifact_bytes = restored_path.read_bytes()
    restore = {
        "target_id": "target-1",
        "generation": 1,
        "artifact_id": "artifact-1",
        "manifest_digest": "a" * 64,
        "status": "restored",
        "subject_id": "Noyra-0001",
        "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "restore_path": str(restored_path),
    }
    health = module.dispatch(
        agent,
        "health",
        {
            **restore,
            "expected_digest": "a" * 64,
            "task_id": "task-1",
            "subject_id": "Noyra-0001",
            "source_epoch": "runtime-1",
            "restore_report_digest": module.content_hash(restore),
        },
    )
    assert isinstance(health["target_signature"], str)
    activation = module.dispatch(
        agent,
        "activate",
        {
            "task_id": "task-1",
            "subject_id": "Noyra-0001",
            "target_id": "target-1",
            "source_epoch": "runtime-1",
            "manifest_digest": "a" * 64,
            "artifact_id": "artifact-1",
            "artifact_sha256": "e" * 64,
            "health_report_digest": module.content_hash(
                {key: value for key, value in health.items() if key != "target_signature"}
            ),
            "source_fence_digest": "d" * 64,
        },
    )
    assert activation["status"] == "active"
    assert activation["source_fence_digest"] == "d" * 64
    signature = base64.urlsafe_b64decode(
        activation["target_signature"] + "=" * (-len(activation["target_signature"]) % 4)
    )
    private.public_key().verify(
        signature,
        module.canonical_json(
            {key: value for key, value in activation.items() if key != "target_signature"}
        ).encode(),
    )
    assert module.dispatch(
        agent,
        "deactivate",
        {
            "task_id": "task-1",
            "target_id": "target-1",
            "source_epoch": "runtime-1",
            "manifest_digest": "a" * 64,
        },
    )["status"] == "deactivated"


def test_http_handler_requires_signed_body_and_rejects_replay(tmp_path: Path) -> None:
    module = _module()
    token = "session-" + "b" * 32
    agent = module.MigrationAgent(
        target_id="target-1",
        key_fingerprint="a" * 64,
        data_root=tmp_path / "data",
        session_token=token,
    )
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    server.agent = agent
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = b'{"subject_id":"Noyra-0001"}'
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST", "/v1/enroll", body=body, headers={"Content-Type": "application/json"}
        )
        assert connection.getresponse().status == 401
        connection.close()

        headers = {"Content-Type": "application/json"}
        headers.update(
            _auth_headers(token, body, timestamp=int(time.time()), nonce="nonce-000000000101")
        )
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", "/v1/enroll", body=body, headers=headers)
        assert connection.getresponse().status == 200
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", "/v1/enroll", body=body, headers=headers)
        assert connection.getresponse().status == 401
        connection.close()

        receive_body = json.dumps(
            {
                "artifact_id": "legacy-artifact",
                "byte_size": 1,
                "artifact_format": "sqlite",
                "artifact_b64": base64.b64encode(b"x").decode(),
            },
            separators=(",", ":"),
        ).encode()
        receive_headers = {"Content-Type": "application/json"}
        receive_headers.update(
            _auth_headers(
                token,
                receive_body,
                timestamp=int(time.time()),
                nonce="nonce-000000000102",
            )
        )
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST", "/v1/receive", body=receive_body, headers=receive_headers
        )
        response = connection.getresponse()
        assert response.status == 400
        assert "recipient-encrypted migration bundle" in response.read().decode()
        connection.close()
        assert not (tmp_path / "data" / "incoming").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
