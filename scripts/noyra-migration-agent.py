#!/usr/bin/env python3
"""Restricted target-side migration agent and loopback HTTP service."""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from noyra.core.at_rest import EncryptedBackupManager
from noyra.core.types import canonical_json, content_hash
from noyra.migration.activation import TargetActivationBridge
from noyra.migration.agent import AgentAuthenticationError, MigrationAgent

MAX_BODY = 8 * 1024 * 1024
IDENTITY_KEYS = frozenset(
    {
        "target_id",
        "key_fingerprint",
        "generation",
        "public_key",
        "private_key",
        "recipient_private_key",
        "recipient_public_key",
        "recipient_key_fingerprint",
        "session_token",
        "credential_references",
        "credential_fingerprints",
        "signer_id",
        "wallet_address",
    }
)


def _read_identity_json(path: Path, *, limit: int = MAX_BODY) -> dict[str, Any]:
    if os.name == "nt" and path.is_symlink():
        raise ValueError("identity file must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise ValueError("identity file cannot be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("identity file must be a regular file")
        if metadata.st_nlink != 1:
            raise ValueError("identity file must not have hard links")
        _validate_identity_metadata(metadata)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
    finally:
        os.close(descriptor)
    if len(raw) > limit:
        raise ValueError("identity or request is too large")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON object is required")
    return value


def _read_body(stream: Any, length: int) -> bytes:
    if length < 0 or length > MAX_BODY:
        raise ValueError("request body is too large")
    value = stream.read(length)
    if len(value) != length:
        raise ValueError("request body is incomplete")
    return cast(bytes, value)


def _decode_body(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON object is required")
    return value


def _read_stdin() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise ValueError("request body is too large")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON object is required")
    return value


def _private_key(value: str | None) -> Ed25519PrivateKey | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("private key is invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (TypeError, ValueError) as error:
        raise ValueError("private key is invalid") from error
    if len(raw) != 32:
        raise ValueError("private key is invalid")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _recipient_private_key(value: str | None) -> X25519PrivateKey | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("recipient private key is invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (TypeError, ValueError) as error:
        raise ValueError("recipient private key is invalid") from error
    if len(raw) != 32:
        raise ValueError("recipient private key is invalid")
    return X25519PrivateKey.from_private_bytes(raw)


def _assert_identity_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("identity file must be a regular file")
    metadata = path.stat()
    if metadata.st_nlink != 1:
        raise ValueError("identity file must not have hard links")
    _validate_identity_metadata(metadata)


def _validate_identity_metadata(metadata: os.stat_result) -> None:
    if os.name == "nt":
        return
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o037:
        raise ValueError("identity file must not be group or world accessible")
    groups = {os.getegid(), *os.getgroups()}  # type: ignore[attr-defined]
    if metadata.st_uid != os.geteuid() and not (  # type: ignore[attr-defined]
        metadata.st_uid == 0 and metadata.st_gid in groups and mode & 0o040
    ):
        raise ValueError("identity file owner is invalid")


def load_agent(
    identity_file: Path,
    data_root: Path,
    *,
    restore_root: Path | None = None,
    backup_keyring: Path | None = None,
    activation_request_root: Path = Path("/var/lib/noyra/migration/target-activation/requests"),
    activation_status_root: Path = Path("/var/lib/noyra/migration/target-activation/status"),
) -> MigrationAgent:
    identity = _read_identity_json(identity_file)
    if set(identity) - IDENTITY_KEYS:
        raise ValueError("identity file contains unknown fields")
    configured_restore_root = restore_root
    backup_manager = (
        EncryptedBackupManager(configured_restore_root, backup_keyring)
        if configured_restore_root is not None and backup_keyring is not None
        else None
    )
    signing_key = _private_key(identity.get("private_key"))
    activation_controller = (
        TargetActivationBridge(
            activation_request_root,
            activation_status_root,
            signing_key,
        )
        if signing_key is not None
        else None
    )
    return MigrationAgent(
        target_id=identity["target_id"],
        key_fingerprint=identity["key_fingerprint"],
        generation=identity.get("generation", 1),
        public_key=identity.get("public_key"),
        signing_key=signing_key,
        recipient_private_key=_recipient_private_key(identity.get("recipient_private_key")),
        recipient_public_key=identity.get("recipient_public_key"),
        recipient_key_fingerprint=identity.get("recipient_key_fingerprint"),
        data_root=data_root,
        session_token=identity.get("session_token"),
        restore_root=configured_restore_root,
        backup_manager=backup_manager,
        activation_controller=activation_controller,
        credential_references=identity.get("credential_references"),
        credential_fingerprints=identity.get("credential_fingerprints"),
        signer_id=identity.get("signer_id"),
        wallet_address=identity.get("wallet_address"),
    )


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def dispatch(agent: MigrationAgent, operation: str, payload: dict[str, Any]) -> Any:
    if (
        operation in {"receive", "restore"}
        and (agent.data_root is not None or agent.restore_root is not None)
        and agent._recipient_private_key is None
    ):
        raise ValueError(
            "recipient-encrypted migration bundle support is unavailable; "
            "persistent receive and restore are disabled"
        )
    if operation == "enroll":
        return asdict(agent.enroll(payload or None))
    if operation == "challenge":
        attestation = agent.challenge(payload)
        result = asdict(attestation)
        result["challenge"] = asdict(attestation.challenge)
        return result
    if operation == "recipient-pop":
        return asdict(agent.recipient_pop(payload))
    if operation == "bindings":
        return agent.binding_proof(payload)
    if operation == "preflight":
        manifest = payload.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("migration manifest is invalid")
        return agent.preflight(manifest)
    if operation == "receive":
        if "chunk_index" in payload:
            encoded_chunk = payload.pop("chunk_b64", None)
            if not isinstance(encoded_chunk, str):
                raise ValueError("chunk_b64 must be a string")
            try:
                chunk = base64.b64decode(encoded_chunk, validate=True)
            except (ValueError, TypeError) as error:
                raise ValueError("chunk_b64 is invalid") from error
            return asdict(
                agent.receive_chunk(
                    payload.pop("manifest"),
                    chunk_index=payload.pop("chunk_index"),
                    chunk_count=payload.pop("chunk_count"),
                    chunk_bytes=payload.pop("chunk_bytes"),
                    chunk_sha256=payload.pop("chunk_sha256"),
                    chunk=chunk,
                )
            )
        encoded_artifact = payload.pop("artifact_b64", None)
        artifact = None
        if encoded_artifact is not None:
            if not isinstance(encoded_artifact, str):
                raise ValueError("artifact_b64 must be a string")
            try:
                artifact = base64.b64decode(encoded_artifact, validate=True)
            except (ValueError, TypeError) as error:
                raise ValueError("artifact_b64 is invalid") from error
        return asdict(agent.receive(payload, artifact=artifact))
    if operation == "restore":
        from noyra.migration.agent import ReceiveReceipt

        task_id = payload.pop("task_id", None)
        expected_digest = payload.pop("expected_digest", None)
        return asdict(
            agent.restore(
                ReceiveReceipt(**payload),
                expected_digest=expected_digest,
                task_id=task_id,
            )
        )
    if operation == "health":
        from noyra.migration.agent import RestoreReport

        task_id = payload.pop("task_id", None)
        subject_id = payload.pop("subject_id", None)
        source_epoch = payload.pop("source_epoch", None)
        restore_report_digest = payload.pop("restore_report_digest", None)
        expected_digest = payload.pop("expected_digest", None)
        payload.pop("checks", None)
        report = RestoreReport(**payload)
        health = agent.validate(report, expected_digest=expected_digest).to_dict()
        if all(
            isinstance(value, str) and value
            for value in (task_id, subject_id, source_epoch, restore_report_digest)
        ):
            signing_payload = {
                "task_id": task_id,
                "subject_id": subject_id,
                "target_id": report.target_id,
                "source_epoch": source_epoch,
                "manifest_digest": report.manifest_digest,
                "artifact_id": report.artifact_id,
                "restore_report_digest": restore_report_digest,
                "health_report_digest": content_hash(health),
            }
            if agent._signing_key is None:
                raise ValueError("target signing identity is not configured")
            health["target_signature"] = agent.sign_recovery_proof(
                canonical_json(signing_payload).encode()
            )
        return health
    if operation == "activate":
        return agent.activate(payload)
    if operation == "deactivate":
        return agent.deactivate(payload)
    raise ValueError("unsupported migration operation")


class Handler(BaseHTTPRequestHandler):
    server_version = "NoyraMigrationAgent/1"
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        operations = {
            "/v1/enroll": "enroll",
            "/v1/challenge": "challenge",
            "/v1/recipient-pop": "recipient-pop",
            "/v1/bindings": "bindings",
            "/v1/preflight": "preflight",
            "/v1/receive": "receive",
            "/v1/receive-chunk": "receive",
            "/v1/restore": "restore",
            "/v1/health": "health",
            "/v1/activate": "activate",
            "/v1/deactivate": "deactivate",
        }
        operation = operations.get(self.path)
        if operation is None:
            self._reply(404, {"error": "unknown migration endpoint"})
            return
        try:
            self.connection.settimeout(10)
            if self.headers.get_content_type() != "application/json":
                raise ValueError("content type must be application/json")
            length = int(self.headers.get("Content-Length", "-1"))
            raw_body = _read_body(self.rfile, length)
            self.server.agent.authenticate_request(self.headers, raw_body)  # type: ignore[attr-defined]
            payload = _decode_body(raw_body)
            result = dispatch(self.server.agent, operation, payload)  # type: ignore[attr-defined]
        except AgentAuthenticationError:
            self._reply(401, {"error": "unauthorized"})
            return
        except (ValueError, KeyError, TypeError) as error:
            self._reply(400, {"error": str(error)})
            return
        self._reply(200, result)

    def do_GET(self) -> None:
        if self.path != "/v1/health":
            self._reply(404, {"error": "unknown migration endpoint"})
            return
        agent = self.server.agent  # type: ignore[attr-defined]
        try:
            agent.authenticate_request(self.headers, b"")
        except AgentAuthenticationError:
            self._reply(401, {"error": "unauthorized"})
            return
        self._reply(200, {"status": "ready", "target_id": agent.target_id})

    def log_message(self, *_args: Any) -> None:
        return

    def _reply(self, status: int, value: Any) -> None:
        body = _json(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Noyra restricted migration target agent")
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=Path("/var/lib/noyra/migration-agent"))
    parser.add_argument(
        "--restore-root", type=Path, default=None, help="private root for restored runtime data"
    )
    parser.add_argument("--backup-keyring", type=Path, default=None)
    parser.add_argument(
        "--activation-request-root",
        type=Path,
        default=Path("/var/lib/noyra/migration/target-activation/requests"),
    )
    parser.add_argument(
        "--activation-status-root",
        type=Path,
        default=Path("/var/lib/noyra/migration/target-activation/status"),
    )
    parser.add_argument("--listen", default="127.0.0.1:8876")
    parser.add_argument(
        "operation",
        nargs="?",
        choices=(
            "enroll",
            "challenge",
            "recipient-pop",
            "bindings",
            "preflight",
            "receive",
            "restore",
            "health",
            "activate",
            "deactivate",
        ),
    )
    args = parser.parse_args(argv)
    agent = load_agent(
        args.identity_file,
        args.data_root,
        restore_root=args.restore_root,
        backup_keyring=args.backup_keyring,
        activation_request_root=args.activation_request_root,
        activation_status_root=args.activation_status_root,
    )
    if args.operation:
        payload = _read_stdin()
        sys.stdout.write(
            json.dumps(dispatch(agent, args.operation, payload), sort_keys=True) + "\n"
        )
        return 0
    host, separator, raw_port = args.listen.rpartition(":")
    if separator != ":" or host not in {"127.0.0.1", "::1"}:
        raise ValueError("agent must bind to loopback")
    port = int(raw_port)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.timeout = 5
    server.agent = agent  # type: ignore[attr-defined]
    server.serve_forever()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"migration agent refused to start: {error}", file=sys.stderr)
        raise SystemExit(78) from error
