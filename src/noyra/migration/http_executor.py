"""Authenticated HTTPS migration executor.

The cutover coordinator only commits after this boundary has completed every
external side effect: source fencing, bounded chunk transfer, target restore,
health validation and target activation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from noyra.core.types import canonical_json, content_hash

from .executor import MigrationExecutionError, MigrationExecutionReceipt
from .manager import MigrationTask

MAX_RESPONSE_BYTES = 1_000_000
MAX_CHUNK_BYTES = 4 * 1024 * 1024
_SIGNATURE_ERROR = (ValueError, TypeError, InvalidSignature, binascii.Error)


@dataclass(frozen=True)
class ArtifactBundle:
    path: Path
    manifest: dict[str, Any]


class SQLiteArtifactProvider:
    """Create a consistent SQLite snapshot for a proof-bound transfer."""

    def __init__(self, database_path: Path | str, output_root: Path | str):
        self.database_path = Path(database_path).resolve()
        self.output_root = Path(output_root).resolve()

    def __call__(self, task: MigrationTask, proof: Mapping[str, object]) -> ArtifactBundle:
        raw_manifest = proof.get("manifest")
        if not isinstance(raw_manifest, Mapping):
            raise MigrationExecutionError("artifact_manifest_required")
        manifest = dict(raw_manifest)
        if manifest.get("artifact_id") != proof.get("artifact_id"):
            raise MigrationExecutionError("artifact_id_mismatch")
        if (
            manifest.get("subject_id") != task.subject_id
            or manifest.get("artifact_format") != "sqlite"
        ):
            raise MigrationExecutionError("artifact_manifest_invalid")
        artifact_id = str(manifest["artifact_id"])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", artifact_id):
            raise MigrationExecutionError("artifact_id_invalid")
        self.output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = self.output_root / f"{artifact_id}.sqlite"
        if destination.exists() and destination.is_symlink():
            raise MigrationExecutionError("artifact_output_invalid")
        temporary = self.output_root / f".{artifact_id}.sqlite"
        temporary.unlink(missing_ok=True)
        try:
            with (
                sqlite3.connect(self.database_path) as source,
                sqlite3.connect(temporary) as target,
            ):
                source.backup(target)
            temporary.replace(destination)
        except (OSError, sqlite3.DatabaseError) as error:
            temporary.unlink(missing_ok=True)
            raise MigrationExecutionError("artifact_snapshot_failed") from error
        actual = destination.read_bytes()
        actual_digest = hashlib.sha256(actual).hexdigest()
        if len(actual) != manifest.get("byte_size") or actual_digest != manifest.get(
            "artifact_sha256"
        ):
            raise MigrationExecutionError("artifact_snapshot_mismatch")
        return ArtifactBundle(destination, manifest)


class HTTPTransport(Protocol):
    def request(self, url: str, body: dict[str, Any], token: str) -> dict[str, Any]: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class UrllibHTTPTransport:
    def __init__(self, *, timeout_seconds: float = 15.0):
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("migration HTTP timeout is invalid")
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(_NoRedirect)

    def request(self, url: str, body: dict[str, Any], token: str) -> dict[str, Any]:
        encoded = canonical_json(body).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(18)
        signing_bytes = f"noyra-migration-agent-v1\n{timestamp}\n{nonce}\n{digest}".encode()
        signature = base64.urlsafe_b64encode(
            hmac.new(token.encode("utf-8"), signing_bytes, hashlib.sha256).digest()
        ).decode("ascii")
        request = Request(
            url,
            data=encoded,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Noyra-HMAC {signature}",
                "X-Noyra-Timestamp": timestamp,
                "X-Noyra-Nonce": nonce,
                "X-Noyra-Body-SHA256": digest,
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                if response.status < 200 or response.status >= 300:
                    raise MigrationExecutionError("target_http_status")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise MigrationExecutionError("target_http_status") from error
        except (TimeoutError, URLError, OSError) as error:
            raise MigrationExecutionError("target_http_unavailable") from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MigrationExecutionError("target_response_too_large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise MigrationExecutionError("target_response_invalid") from error
        if not isinstance(value, dict):
            raise MigrationExecutionError("target_response_invalid")
        return value


class HTTPMigrationExecutor:
    def __init__(
        self,
        *,
        target_resolver: Callable[[MigrationTask], Mapping[str, Any]],
        token_resolver: Callable[[MigrationTask], str],
        artifact_resolver: Callable[[MigrationTask, Mapping[str, object]], ArtifactBundle],
        source_fence: Callable[[MigrationTask, str], str],
        source_unfence: Callable[[MigrationTask, str], None],
        transport: HTTPTransport | None = None,
        chunk_bytes: int = 1024 * 1024,
    ) -> None:
        if not 4096 <= chunk_bytes <= MAX_CHUNK_BYTES:
            raise ValueError("migration chunk size is invalid")
        self.target_resolver = target_resolver
        self.token_resolver = token_resolver
        self.artifact_resolver = artifact_resolver
        self.source_fence = source_fence
        self.source_unfence = source_unfence
        self.transport = transport or UrllibHTTPTransport()
        self.chunk_bytes = chunk_bytes
        self._active: dict[str, tuple[Mapping[str, Any], str, str]] = {}

    def execute(
        self,
        task: MigrationTask,
        *,
        proof: Mapping[str, object],
        source_epoch: str,
    ) -> MigrationExecutionReceipt:
        if source_epoch != task.source_epoch:
            raise MigrationExecutionError("source_epoch_mismatch")
        target = self._target(task)
        token = self._token(task)
        source_fence_digest = self.source_fence(task, source_epoch)
        if not self._digest(source_fence_digest):
            raise MigrationExecutionError("source_fence_proof_invalid")
        try:
            # Acquire the durable source fence before snapshotting.  A
            # snapshot taken first can race a final source write and leave
            # the target with state that is not covered by the fence proof.
            bundle = self.artifact_resolver(task, proof)
            path = bundle.path.expanduser()
            if path.is_symlink() or not path.is_file():
                raise MigrationExecutionError("artifact_unavailable")
            manifest = self._manifest(bundle, task, proof)
            manifest_digest = content_hash(manifest)
            if manifest_digest != proof.get("manifest_digest"):
                raise MigrationExecutionError("artifact_manifest_mismatch")
            if path.stat().st_size != manifest["byte_size"]:
                raise MigrationExecutionError("artifact_size_mismatch")
            final = self._send_chunks(target, token, manifest, manifest_digest, path)
            restore = self._request(
                target,
                token,
                "/v1/restore",
                {
                    **final,
                    "task_id": task.task_id,
                    "expected_digest": manifest_digest,
                },
            )
            health_with_signature = self._request(
                target,
                token,
                "/v1/health",
                {
                    **restore,
                    "expected_digest": manifest_digest,
                    "task_id": task.task_id,
                    "subject_id": task.subject_id,
                    "source_epoch": source_epoch,
                    "restore_report_digest": content_hash(restore),
                },
            )
            target_signature = health_with_signature.pop("target_signature", None)
            self._validate_health(health_with_signature, task, manifest_digest)
            self._verify_target_signature(
                target,
                task,
                manifest_digest,
                manifest["artifact_id"],
                restore,
                health_with_signature,
                target_signature,
            )
            activation = self._request(
                target,
                token,
                "/v1/activate",
                {
                    "task_id": task.task_id,
                    "subject_id": task.subject_id,
                    "target_id": task.target_id,
                    "source_epoch": source_epoch,
                    "manifest_digest": manifest_digest,
                    "artifact_id": manifest["artifact_id"],
                    "health_report_digest": content_hash(health_with_signature),
                    "source_fence_digest": source_fence_digest,
                },
            )
            activation = self._verify_activation_receipt(
                target,
                task,
                manifest_digest,
                str(manifest["artifact_id"]),
                content_hash(health_with_signature),
                source_fence_digest,
                activation,
            )
            activation_digest = content_hash(activation)
            self._active[task.task_id] = (target, token, source_epoch)
            return MigrationExecutionReceipt(
                task.task_id,
                task.subject_id,
                task.target_id,
                source_epoch,
                manifest_digest,
                str(manifest["artifact_id"]),
                content_hash(restore),
                content_hash(health_with_signature),
                source_fence_digest,
                activation_digest,
            )
        except Exception:
            try:
                self.source_unfence(task, source_epoch)
            except Exception as error:
                raise MigrationExecutionError("source_unfence_failed") from error
            raise

    def rollback(
        self,
        task: MigrationTask,
        *,
        receipt: MigrationExecutionReceipt,
        reason: str,
    ) -> None:
        del reason
        target, token, source_epoch = self._active.pop(
            task.task_id,
            (self._target(task), self._token(task), task.source_epoch),
        )
        try:
            result = self._request(
                target,
                token,
                "/v1/deactivate",
                {
                    "task_id": receipt.task_id,
                    "target_id": receipt.target_id,
                    "source_epoch": source_epoch,
                    "manifest_digest": receipt.manifest_digest,
                },
            )
            if result.get("status") not in {"deactivated", "inactive"}:
                raise MigrationExecutionError("target_deactivation_failed")
            self.source_unfence(task, source_epoch)
        except MigrationExecutionError:
            raise
        except Exception as error:
            raise MigrationExecutionError("migration_rollback_failed") from error

    def _send_chunks(
        self,
        target: Mapping[str, Any],
        token: str,
        manifest: Mapping[str, Any],
        manifest_digest: str,
        path: Path,
    ) -> dict[str, Any]:
        chunk_count = (manifest["byte_size"] + self.chunk_bytes - 1) // self.chunk_bytes
        final: dict[str, Any] | None = None
        with path.open("rb") as stream:
            for index in range(chunk_count):
                chunk = stream.read(self.chunk_bytes)
                response = self._request(
                    target,
                    token,
                    "/v1/receive-chunk",
                    {
                        "manifest": dict(manifest),
                        "manifest_digest": manifest_digest,
                        "chunk_index": index,
                        "chunk_count": chunk_count,
                        "chunk_bytes": self.chunk_bytes,
                        "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
                        "chunk_b64": base64.b64encode(chunk).decode("ascii"),
                    },
                )
                if response.get("manifest_digest") != manifest_digest:
                    raise MigrationExecutionError("target_chunk_manifest_mismatch")
                if response.get("complete") is True:
                    final = response
        if final is None:
            raise MigrationExecutionError("target_transfer_incomplete")
        required = (
            "artifact_id",
            "byte_size",
            "manifest_path",
            "artifact_path",
            "artifact_sha256",
        )
        if (
            any(key not in final for key in required)
            or final["artifact_id"] != manifest["artifact_id"]
        ):
            raise MigrationExecutionError("target_transfer_receipt_invalid")
        return {key: final[key] for key in required}

    def _request(
        self, target: Mapping[str, Any], token: str, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        endpoint = str(target["endpoint"]).rstrip("/") + path
        return self.transport.request(endpoint, body, token)

    def _target(self, task: MigrationTask) -> Mapping[str, Any]:
        value = self.target_resolver(task)
        if not isinstance(value, Mapping):
            raise MigrationExecutionError("target_registration_invalid")
        endpoint = str(value.get("endpoint", ""))
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
        ):
            raise MigrationExecutionError("target_endpoint_not_https")
        if value.get("target_id") != task.target_id:
            raise MigrationExecutionError("target_registration_mismatch")
        return value

    def _token(self, task: MigrationTask) -> str:
        value = self.token_resolver(task)
        if (
            not isinstance(value, str)
            or not 32 <= len(value) <= 256
            or any(c.isspace() for c in value)
        ):
            raise MigrationExecutionError("target_auth_token_unavailable")
        return value

    @staticmethod
    def _manifest(
        bundle: ArtifactBundle, task: MigrationTask, proof: Mapping[str, object]
    ) -> dict[str, Any]:
        manifest = dict(bundle.manifest)
        manifest.setdefault("artifact_id", proof.get("artifact_id"))
        manifest.setdefault("subject_id", task.subject_id)
        manifest.setdefault("artifact_sha256", hashlib.sha256(bundle.path.read_bytes()).hexdigest())
        manifest.setdefault("byte_size", bundle.path.stat().st_size)
        if manifest.get("artifact_id") != proof.get("artifact_id"):
            raise MigrationExecutionError("artifact_id_mismatch")
        return manifest

    @staticmethod
    def _validate_health(value: Mapping[str, Any], task: MigrationTask, digest: str) -> None:
        if (
            value.get("target_id") != task.target_id
            or value.get("manifest_digest") != digest
            or value.get("status") != "healthy"
        ):
            raise MigrationExecutionError("target_health_invalid")
        checks = value.get("checks")
        if (
            not isinstance(checks, Mapping)
            or not checks
            or not all(item is True for item in checks.values())
        ):
            raise MigrationExecutionError("target_health_checks_failed")

    @staticmethod
    def _verify_target_signature(
        target: Mapping[str, Any],
        task: MigrationTask,
        digest: str,
        artifact_id: str,
        restore: Mapping[str, Any],
        health: Mapping[str, Any],
        signature: Any,
    ) -> None:
        if not isinstance(signature, str):
            raise MigrationExecutionError("target_health_signature_missing")
        payload = {
            "task_id": task.task_id,
            "subject_id": task.subject_id,
            "target_id": task.target_id,
            "source_epoch": task.source_epoch,
            "manifest_digest": digest,
            "artifact_id": artifact_id,
            "restore_report_digest": content_hash(restore),
            "health_report_digest": content_hash(health),
        }
        try:
            encoded_key = str(target["public_key"])
            raw_key = base64.urlsafe_b64decode(encoded_key + "=" * (-len(encoded_key) % 4))
            raw_sig = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
            Ed25519PublicKey.from_public_bytes(raw_key).verify(
                raw_sig, canonical_json(payload).encode("utf-8")
            )
        except _SIGNATURE_ERROR as error:
            raise MigrationExecutionError("target_health_signature_invalid") from error

    @staticmethod
    def _verify_activation_receipt(
        target: Mapping[str, Any],
        task: MigrationTask,
        manifest_digest: str,
        artifact_id: str,
        health_report_digest: str,
        source_fence_digest: str,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(receipt, Mapping) or not isinstance(
            receipt.get("target_signature"), str
        ):
            raise MigrationExecutionError("target_activation_signature_missing")
        required = {
            "task_id": task.task_id,
            "subject_id": task.subject_id,
            "target_id": task.target_id,
            "source_epoch": task.source_epoch,
            "manifest_digest": manifest_digest,
            "artifact_id": artifact_id,
            "health_report_digest": health_report_digest,
            "source_fence_digest": source_fence_digest,
            "status": "active",
            "service_unit": "noyra.service",
        }
        if any(receipt.get(key) != value for key, value in required.items()):
            raise MigrationExecutionError("target_activation_receipt_invalid")
        if not isinstance(receipt.get("active_database_sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", receipt["active_database_sha256"]
        ):
            raise MigrationExecutionError("target_activation_receipt_invalid")
        activated_at = receipt.get("activated_at")
        if not isinstance(activated_at, str):
            raise MigrationExecutionError("target_activation_receipt_invalid")
        try:
            timestamp = datetime.fromisoformat(activated_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise MigrationExecutionError("target_activation_receipt_invalid") from error
        if timestamp.tzinfo is None:
            raise MigrationExecutionError("target_activation_receipt_invalid")
        receipt_fields = {*required, "active_database_sha256", "activated_at", "target_signature"}
        if set(receipt) != receipt_fields:
            raise MigrationExecutionError("target_activation_receipt_invalid")
        signed = {key: value for key, value in receipt.items() if key != "target_signature"}
        try:
            encoded_key = str(target["public_key"])
            raw_key = base64.urlsafe_b64decode(encoded_key + "=" * (-len(encoded_key) % 4))
            raw_signature = base64.urlsafe_b64decode(
                str(receipt["target_signature"])
                + "=" * (-len(str(receipt["target_signature"])) % 4)
            )
            Ed25519PublicKey.from_public_bytes(raw_key).verify(
                raw_signature, canonical_json(signed).encode("utf-8")
            )
        except _SIGNATURE_ERROR as error:
            raise MigrationExecutionError("target_activation_signature_invalid") from error
        return dict(receipt)

    @staticmethod
    def _digest(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )


__all__ = [
    "ArtifactBundle",
    "HTTPMigrationExecutor",
    "HTTPTransport",
    "SQLiteArtifactProvider",
    "UrllibHTTPTransport",
]
