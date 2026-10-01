#!/usr/bin/env python3
"""Restricted target-side migration agent and loopback HTTP service."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.migration.agent import MigrationAgent

MAX_BODY = 1_000_000
IDENTITY_KEYS = frozenset(
    {"target_id", "key_fingerprint", "generation", "public_key", "private_key"}
)


def _read_json(path: Path, *, limit: int = MAX_BODY) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError("identity or request is too large")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON object is required")
    return value


def _read_body(stream: Any, length: int) -> dict[str, Any]:
    if length < 0 or length > MAX_BODY:
        raise ValueError("request body is too large")
    value = json.loads(stream.read(length).decode("utf-8"))
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


def _assert_identity_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("identity file must be a regular file")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("identity file must not be group or world accessible")


def load_agent(identity_file: Path, data_root: Path) -> MigrationAgent:
    _assert_identity_file(identity_file)
    identity = _read_json(identity_file)
    if set(identity) - IDENTITY_KEYS:
        raise ValueError("identity file contains unknown fields")
    return MigrationAgent(
        target_id=identity["target_id"],
        key_fingerprint=identity["key_fingerprint"],
        generation=identity.get("generation", 1),
        public_key=identity.get("public_key"),
        signing_key=_private_key(identity.get("private_key")),
        data_root=data_root,
    )


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def dispatch(agent: MigrationAgent, operation: str, payload: dict[str, Any]) -> Any:
    if operation == "enroll":
        return asdict(agent.enroll(payload or None))
    if operation == "challenge":
        attestation = agent.challenge(payload)
        result = asdict(attestation)
        result["challenge"] = asdict(attestation.challenge)
        return result
    if operation == "receive":
        return asdict(agent.receive(payload))
    if operation == "restore":
        from noyra.migration.agent import ReceiveReceipt

        expected_digest = payload.pop("expected_digest", None)
        return asdict(agent.restore(ReceiveReceipt(**payload), expected_digest=expected_digest))
    if operation == "health":
        from noyra.migration.agent import RestoreReport

        expected_digest = payload.pop("expected_digest", None)
        payload.pop("checks", None)
        report = RestoreReport(**payload)
        return agent.validate(report, expected_digest=expected_digest).to_dict()
    raise ValueError("unsupported migration operation")


class Handler(BaseHTTPRequestHandler):
    server_version = "NoyraMigrationAgent/1"
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        operations = {
            "/v1/enroll": "enroll",
            "/v1/challenge": "challenge",
            "/v1/receive": "receive",
            "/v1/restore": "restore",
            "/v1/health": "health",
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
            payload = _read_body(self.rfile, length)
            result = dispatch(self.server.agent, operation, payload)  # type: ignore[attr-defined]
        except (ValueError, KeyError, TypeError) as error:
            self._reply(400, {"error": str(error)})
            return
        self._reply(200, result)

    def do_GET(self) -> None:
        if self.path != "/v1/health":
            self._reply(404, {"error": "unknown migration endpoint"})
            return
        agent = self.server.agent  # type: ignore[attr-defined]
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
    parser.add_argument("--listen", default="127.0.0.1:8876")
    parser.add_argument(
        "operation", nargs="?", choices=("enroll", "challenge", "receive", "restore", "health")
    )
    args = parser.parse_args(argv)
    agent = load_agent(args.identity_file, args.data_root)
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
