"""Authenticated HTTPS migration executor.

The cutover coordinator only commits after this boundary has completed every
external side effect: source fencing, bounded chunk transfer, target restore,
health validation and target activation.
"""

from __future__ import annotations

import base64
import binascii
import gc
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
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.types import canonical_json, content_hash

from .bundle import BUNDLE_FORMAT, encrypt_bundle
from .executor import MigrationExecutionError, MigrationExecutionReceipt
from .manager import MigrationTask
from .trust import (
    RecipientPoPProof,
    create_recipient_pop_challenge,
    verify_recipient_pop,
)

MAX_RESPONSE_BYTES = 1_000_000
MAX_CHUNK_BYTES = 4 * 1024 * 1024
_SIGNATURE_ERROR = (ValueError, TypeError, InvalidSignature, binascii.Error)


@dataclass(frozen=True)
class ArtifactBundle:
    path: Path
    manifest: dict[str, Any]


class SQLiteArtifactProvider:
    """Create a redacted SQLite snapshot and encrypt it to the target key."""

    def __init__(self, database_path: Path | str, output_root: Path | str):
        self.database_path = Path(database_path).resolve()
        self.output_root = Path(output_root).resolve()

    def __call__(self, task: MigrationTask, proof: Mapping[str, object]) -> ArtifactBundle:
        artifact_id = str(proof.get("artifact_id") or f"migration-{task.task_id}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", artifact_id):
            raise MigrationExecutionError("artifact_id_invalid")
        recipient_value = proof.get("recipient_public_key")
        if not isinstance(recipient_value, str):
            raise MigrationExecutionError("recipient_public_key_required")
        try:
            recipient_bytes = base64.urlsafe_b64decode(
                recipient_value + "=" * (-len(recipient_value) % 4)
            )
            recipient_public = X25519PublicKey.from_public_bytes(recipient_bytes)
        except (ValueError, TypeError, binascii.Error) as error:
            raise MigrationExecutionError("recipient_public_key_invalid") from error
        recipient_fingerprint = hashlib.sha256(recipient_bytes).hexdigest()
        if proof.get("recipient_key_fingerprint") != recipient_fingerprint:
            raise MigrationExecutionError("recipient_key_fingerprint_mismatch")
        if proof.get("target_id") not in {None, task.target_id}:
            raise MigrationExecutionError("target_registration_mismatch")
        self.output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = self.output_root / f"{artifact_id}.bundle"
        if destination.exists() and destination.is_symlink():
            raise MigrationExecutionError("artifact_output_invalid")
        if destination.exists():
            raise MigrationExecutionError("artifact_already_exists")
        raw_snapshot = self.output_root / f".{artifact_id}.sqlite"
        raw_snapshot.unlink(missing_ok=True)
        try:
            source = sqlite3.connect(self.database_path)
            target = sqlite3.connect(raw_snapshot)
            try:
                source.backup(target)
                target.execute("PRAGMA foreign_keys=OFF")
                for table in (
                    "search_provider_configs",
                    "cognitive_resource_keys",
                    "interaction_transports",
                    "embedding_resources",
                    "secret_file_intents",
                ):
                    if target.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone():
                        target.execute(f'DELETE FROM "{table}"')
                target.commit()
            finally:
                target.close()
                source.close()
                gc.collect()
            context = {
                "task_id": task.task_id,
                "target_id": task.target_id,
                "source_epoch": task.source_epoch,
                "artifact_id": artifact_id,
                "subject_id": task.subject_id,
                "schema_version": str(CURRENT_SCHEMA_VERSION),
                "artifact_format": BUNDLE_FORMAT,
            }
            encrypted = encrypt_bundle(
                raw_snapshot,
                destination,
                recipient_public_key=recipient_public,
                context=context,
            )
        except (OSError, sqlite3.DatabaseError) as error:
            raw_snapshot.unlink(missing_ok=True)
            raise MigrationExecutionError("artifact_snapshot_failed") from error
        finally:
            try:
                raw_snapshot.unlink(missing_ok=True)
            except OSError as error:
                raise MigrationExecutionError("artifact_snapshot_cleanup_failed") from error
        manifest = {
            **encrypted,
            "byte_size": encrypted["ciphertext_size"],
            "artifact_sha256": encrypted["ciphertext_sha256"],
        }
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
        if not target.get("recipient_public_key") or not target.get("recipient_key_fingerprint"):
            raise MigrationExecutionError("recipient_encrypted_bundle_unavailable")
        self._verify_recipient_key_possession(target, task, token, source_epoch)
        source_fence_digest = self.source_fence(task, source_epoch)
        if not self._digest(source_fence_digest):
            raise MigrationExecutionError("source_fence_proof_invalid")
        try:
            # Acquire the durable source fence before snapshotting.  A
            # snapshot taken first can race a final source write and leave
            # the target with state that is not covered by the fence proof.
            bundle = self.artifact_resolver(
                task,
                {
                    **dict(proof),
                    "recipient_public_key": target["recipient_public_key"],
                    "recipient_key_fingerprint": target["recipient_key_fingerprint"],
                    "target_id": task.target_id,
                },
            )
            path = bundle.path.expanduser()
            if path.is_symlink() or not path.is_file():
                raise MigrationExecutionError("artifact_unavailable")
            manifest = self._manifest(bundle, task, proof)
            manifest_digest = content_hash(manifest)
            expected_manifest_digest = proof.get("manifest_digest")
            if expected_manifest_digest is not None and manifest_digest != expected_manifest_digest:
                raise MigrationExecutionError("artifact_manifest_mismatch")
            if path.stat().st_size != manifest["byte_size"]:
                raise MigrationExecutionError("artifact_size_mismatch")
            binding_proof = self._request_binding_proof(
                target, token, task, manifest_digest, manifest, proof
            )
            if isinstance(proof, dict):
                # Keep the verified, target-signed binding evidence attached to
                # the in-memory execution proof so receipt validation can bind
                # every digest to the exact response that was verified.
                proof.update(binding_proof)
            binding_evidence = self._binding_evidence(
                binding_proof, task, manifest_digest, target
            )
            preflight = self._request(
                target,
                token,
                "/v1/preflight",
                {
                    "manifest": manifest,
                    "manifest_digest": manifest_digest,
                    "task_id": task.task_id,
                    "subject_id": task.subject_id,
                    "source_epoch": source_epoch,
                },
            )
            if preflight.get("status") != "ready":
                raise MigrationExecutionError("target_preflight_failed")
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
                    "artifact_sha256": manifest["artifact_sha256"],
                    "health_report_digest": content_hash(health_with_signature),
                    "source_fence_digest": source_fence_digest,
                    "recipient_key_fingerprint": target["recipient_key_fingerprint"],
                    "target_volume_proof_digest": binding_evidence["volume_digest"],
                    "credential_binding_digest": binding_evidence["credential_digest"],
                    "signer_binding_digest": binding_evidence["signer_digest"],
                    "wallet_mode": binding_evidence["wallet_mode"],
                    "wallet_proof_digest": binding_evidence["wallet_digest"],
                },
            )
            activation = self._verify_activation_receipt(
                target,
                task,
                manifest_digest,
                str(manifest["artifact_id"]),
                str(manifest["artifact_sha256"]),
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
                target["recipient_key_fingerprint"],
                binding_evidence["volume_digest"],
                binding_evidence["credential_digest"],
                binding_evidence["signer_digest"],
                binding_evidence["wallet_mode"],
                binding_evidence["wallet_digest"],
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
            "manifest_digest",
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
        receipt = {key: final[key] for key in required}
        receipt["status"] = "received"
        return receipt

    def _verify_recipient_key_possession(
        self,
        target: Mapping[str, Any],
        task: MigrationTask,
        token: str,
        source_epoch: str,
    ) -> None:
        try:
            recipient_bytes = base64.urlsafe_b64decode(
                str(target["recipient_public_key"])
                + "=" * (-len(str(target["recipient_public_key"])) % 4)
            )
            recipient_public = X25519PublicKey.from_public_bytes(recipient_bytes)
            if hashlib.sha256(recipient_bytes).hexdigest() != target["recipient_key_fingerprint"]:
                raise ValueError("recipient key fingerprint mismatch")
            challenge = create_recipient_pop_challenge(
                task.target_id,
                recipient_public,
                source_epoch=source_epoch,
            )
            response = self._request(
                target,
                token,
                "/v1/recipient-pop",
                challenge.to_dict(),
            )
            proof = RecipientPoPProof(
                target_id=str(response["target_id"]),
                source_epoch=str(response["source_epoch"]),
                expires_at=str(response["expires_at"]),
                pop_nonce=str(response["pop_nonce"]),
                recipient_key_fingerprint=str(response["recipient_key_fingerprint"]),
                signature=str(response["signature"]),
            )
            verify_recipient_pop(
                challenge,
                proof,
                target_public_key=str(target["public_key"]),
            )
        except (KeyError, TypeError, ValueError, binascii.Error) as error:
            raise MigrationExecutionError("recipient_key_possession_failed") from error

    @staticmethod
    def _binding_request(value: Any, *, wallet: bool = False) -> dict[str, Any]:
        if value is None:
            return {"mode": "disabled"} if wallet else {"references": {}, "fingerprints": {}}
        if not isinstance(value, Mapping):
            raise MigrationExecutionError("migration_binding_request_invalid")
        forbidden = {"secret", "token", "password", "private", "api_key", "bearer"}

        def contains_secret(item: Any) -> bool:
            if isinstance(item, Mapping):
                return any(
                    (
                        isinstance(key, str)
                        and (
                            key.casefold() in forbidden
                            or any(part in key.casefold() for part in forbidden)
                        )
                    )
                    or contains_secret(child)
                    for key, child in item.items()
                )
            if isinstance(item, (list, tuple)):
                return any(contains_secret(child) for child in item)
            return False

        if contains_secret(value):
            raise MigrationExecutionError("migration_binding_request_contains_secret")
        if wallet:
            mode = value.get("mode")
            if mode == "disabled":
                if set(value) != {"mode"}:
                    raise MigrationExecutionError("migration_wallet_binding_request_invalid")
                return {"mode": "disabled"}
            if mode == "external_signer_rebind":
                allowed = {"mode", "signer_id", "address"}
                if set(value) != allowed:
                    raise MigrationExecutionError("migration_wallet_binding_request_invalid")
                return {key: value[key] for key in allowed}
            if mode == "local_wallet_transfer":
                allowed = {"mode", "address", "approval"}
                if set(value) != allowed or not isinstance(value.get("approval"), Mapping):
                    raise MigrationExecutionError("migration_wallet_binding_request_invalid")
                return {
                    "mode": mode,
                    "address": value["address"],
                    "approval": dict(value["approval"]),
                }
            raise MigrationExecutionError("migration_wallet_binding_request_invalid")
        allowed = {"references", "fingerprints"}
        if (
            set(value) != allowed
            or not isinstance(value.get("references"), Mapping)
            or not isinstance(value.get("fingerprints"), Mapping)
        ):
            raise MigrationExecutionError("migration_credential_binding_request_invalid")
        return {
            "references": dict(value["references"]),
            "fingerprints": dict(value["fingerprints"]),
        }

    def _request_binding_proof(
        self,
        target: Mapping[str, Any],
        token: str,
        task: MigrationTask,
        manifest_digest: str,
        manifest: Mapping[str, Any],
        proof: Mapping[str, object],
    ) -> dict[str, Any]:
        request = {
            "task_id": task.task_id,
            "subject_id": task.subject_id,
            "target_id": task.target_id,
            "source_epoch": task.source_epoch,
            "manifest_digest": manifest_digest,
            "artifact_id": manifest["artifact_id"],
            "recipient_key_fingerprint": target["recipient_key_fingerprint"],
            "credential_binding": self._binding_request(proof.get("credential_binding")),
            "wallet_binding": self._binding_request(proof.get("wallet_binding"), wallet=True),
        }
        response = self._request(target, token, "/v1/bindings", request)
        self._verify_binding_response(target, task, manifest_digest, request, response)
        return response

    @staticmethod
    def _verify_binding_response(
        target: Mapping[str, Any],
        task: MigrationTask,
        manifest_digest: str,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
    ) -> None:
        required = {
            "status",
            "task_id",
            "subject_id",
            "target_id",
            "source_epoch",
            "manifest_digest",
            "artifact_id",
            "target_generation",
            "target_identity",
            "recipient_key_fingerprint",
            "credential_binding",
            "wallet_binding",
            "target_volume_proof",
            "target_signature",
        }
        if not isinstance(response, Mapping) or set(response) != required:
            raise MigrationExecutionError("target_binding_proof_invalid")
        if response.get("status") != "verified":
            raise MigrationExecutionError("target_binding_proof_unverified")
        for key in (
            "task_id",
            "subject_id",
            "target_id",
            "source_epoch",
            "manifest_digest",
            "artifact_id",
            "recipient_key_fingerprint",
        ):
            if response.get(key) != request.get(key):
                raise MigrationExecutionError("target_binding_proof_context_mismatch")
        generation = response.get("target_generation")
        if type(generation) is not int or generation < 1:
            raise MigrationExecutionError("target_binding_proof_generation_invalid")
        registered_generation = target.get("enrollment_generation")
        if registered_generation is not None and (
            type(registered_generation) is not int or generation != registered_generation
        ):
            raise MigrationExecutionError("target_binding_proof_generation_mismatch")
        identity = response.get("target_identity")
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise MigrationExecutionError("target_binding_proof_identity_invalid")
        try:
            raw_key = HTTPMigrationExecutor._decode_base64_key(str(target["public_key"]))
        except (KeyError, ValueError, TypeError, binascii.Error) as error:
            raise MigrationExecutionError("target_binding_proof_key_invalid") from error
        expected_identity = hashlib.sha256(
            (
                "noyra-target-host-v1\n"
                f"{task.target_id}\n{hashlib.sha256(raw_key).hexdigest()}\n{generation}"
            ).encode()
        ).hexdigest()
        if identity != expected_identity:
            raise MigrationExecutionError("target_binding_proof_identity_mismatch")
        if response.get("recipient_key_fingerprint") != target.get(
            "recipient_key_fingerprint"
        ):
            raise MigrationExecutionError("target_binding_proof_recipient_mismatch")
        signature = response.get("target_signature")
        if not isinstance(signature, str):
            raise MigrationExecutionError("target_binding_proof_signature_missing")
        signed = {key: value for key, value in response.items() if key != "target_signature"}
        try:
            raw_signature = HTTPMigrationExecutor._decode_signature(signature)
            Ed25519PublicKey.from_public_bytes(raw_key).verify(
                raw_signature, canonical_json(signed).encode("utf-8")
            )
        except _SIGNATURE_ERROR as error:
            raise MigrationExecutionError("target_binding_proof_signature_invalid") from error
        credential_request = request["credential_binding"]
        credential_response = response["credential_binding"]
        if (
            not isinstance(credential_request, Mapping)
            or not isinstance(credential_response, Mapping)
            or credential_response.get("references") != credential_request.get("references")
            or credential_response.get("fingerprints") != credential_request.get("fingerprints")
            or credential_response.get("status") != "verified"
            or credential_response.get("target_id") != task.target_id
            or credential_response.get("manifest_digest") != manifest_digest
            or credential_response.get("target_identity") != identity
        ):
            raise MigrationExecutionError("target_credential_binding_mismatch")
        wallet_request = request["wallet_binding"]
        wallet_response = response["wallet_binding"]
        if (
            not isinstance(wallet_request, Mapping)
            or not isinstance(wallet_response, Mapping)
            or wallet_response.get("mode") != wallet_request.get("mode")
            or wallet_response.get("status") != "verified"
            or wallet_response.get("target_id") != task.target_id
            or wallet_response.get("manifest_digest") != manifest_digest
            or wallet_response.get("target_identity") != identity
        ):
            raise MigrationExecutionError("target_wallet_binding_mismatch")
        for key in ("signer_id", "address", "approval"):
            if key in wallet_request and wallet_response.get(key) != wallet_request.get(key):
                raise MigrationExecutionError("target_wallet_binding_mismatch")

    @staticmethod
    def _decode_base64_key(value: str) -> bytes:
        if not value:
            raise ValueError("base64 key encoding is invalid")
        raw_value = value.rstrip("=")
        if "=" in raw_value:
            raise ValueError("base64 key encoding is invalid")
        raw = base64.b64decode(
            raw_value.encode("ascii") + b"=" * (-len(raw_value) % 4),
            altchars=b"-_",
            validate=True,
        )
        if (
            len(raw) != 32
            or base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != raw_value
        ):
            raise ValueError("base64 key encoding is invalid")
        return raw

    @staticmethod
    def _decode_signature(value: str) -> bytes:
        if not value:
            raise ValueError("signature encoding is invalid")
        raw_value = value.rstrip("=")
        if "=" in raw_value:
            raise ValueError("signature encoding is invalid")
        raw = base64.b64decode(
            raw_value.encode("ascii") + b"=" * (-len(raw_value) % 4),
            altchars=b"-_",
            validate=True,
        )
        if len(raw) != 64 or base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != raw_value:
            raise ValueError("signature encoding is invalid")
        return raw

    @staticmethod
    def _binding_evidence(
        proof: Mapping[str, object],
        task: MigrationTask,
        manifest_digest: str,
        target: Mapping[str, Any],
    ) -> dict[str, str | None]:
        def mapping(name: str) -> Mapping[str, Any]:
            value = proof.get(name)
            if not isinstance(value, Mapping):
                raise MigrationExecutionError(f"{name}_proof_required")
            if any(
                isinstance(key, str)
                and any(
                    token in key.casefold() for token in ("secret", "token", "password", "private")
                )
                for key in value
            ):
                raise MigrationExecutionError(f"{name}_proof_contains_secret")
            return value

        credential = mapping("credential_binding")
        wallet = mapping("wallet_binding")
        volume = mapping("target_volume_proof")
        if (
            credential.get("status") != "verified"
            or credential.get("target_id") != task.target_id
            or credential.get("manifest_digest") != manifest_digest
        ):
            raise MigrationExecutionError("credential_binding_unverified")
        if credential.get("target_identity") != volume.get("target_identity"):
            raise MigrationExecutionError("credential_binding_target_mismatch")
        availability = credential.get("availability_proof")
        if not isinstance(availability, str) or not re.fullmatch(r"[0-9a-f]{64}", availability):
            raise MigrationExecutionError("credential_binding_proof_invalid")
        expected_credential = content_hash(
            {
                "task_id": task.task_id,
                "manifest_digest": manifest_digest,
                "target_id": task.target_id,
                "target_identity": credential.get("target_identity"),
                "references": credential.get("references"),
                "fingerprints": credential.get("fingerprints"),
            }
        )
        if availability != expected_credential:
            raise MigrationExecutionError("credential_binding_proof_invalid")
        if (
            volume.get("status") != "verified"
            or volume.get("target_id") != task.target_id
            or volume.get("manifest_digest") != manifest_digest
        ):
            raise MigrationExecutionError("target_volume_proof_unverified")
        if (
            volume.get("encrypted") is not True
            or target.get("recipient_key_fingerprint") is None
            or volume.get("recipient_key_fingerprint") != target.get("recipient_key_fingerprint")
        ):
            raise MigrationExecutionError("target_volume_proof_invalid")
        volume_proof = volume.get("proof_digest")
        if not isinstance(volume_proof, str) or not re.fullmatch(r"[0-9a-f]{64}", volume_proof):
            raise MigrationExecutionError("target_volume_proof_invalid")
        expected_volume = content_hash(
            {
                "task_id": task.task_id,
                "target_id": task.target_id,
                "manifest_digest": manifest_digest,
                "recipient_key_fingerprint": target["recipient_key_fingerprint"],
                "target_identity": volume.get("target_identity"),
                "target_generation": volume.get("target_generation"),
                "backend": volume.get("backend"),
            }
        )
        if volume_proof != expected_volume:
            raise MigrationExecutionError("target_volume_proof_invalid")
        if wallet.get("status") not in {"verified", "committed"}:
            raise MigrationExecutionError("wallet_binding_unverified")
        if (
            wallet.get("target_id") != task.target_id
            or wallet.get("manifest_digest") != manifest_digest
            or wallet.get("target_identity") != volume.get("target_identity")
        ):
            raise MigrationExecutionError("wallet_binding_target_mismatch")
        wallet_mode = wallet.get("mode")
        if wallet_mode not in {"external_signer_rebind", "local_wallet_transfer", "disabled"}:
            raise MigrationExecutionError("wallet_binding_mode_invalid")
        wallet_proof = wallet.get("proof_digest")
        if wallet_mode != "disabled" and (
            not isinstance(wallet_proof, str) or not re.fullmatch(r"[0-9a-f]{64}", wallet_proof)
        ):
            raise MigrationExecutionError("wallet_binding_proof_invalid")
        if wallet_mode == "external_signer_rebind":
            expected_wallet = content_hash(
                {
                    "task_id": task.task_id,
                    "target_id": task.target_id,
                    "manifest_digest": manifest_digest,
                    "signer_id": wallet.get("signer_id"),
                    "address": wallet.get("address"),
                    "target_identity": wallet.get("target_identity"),
                }
            )
            if wallet_proof != expected_wallet:
                raise MigrationExecutionError("wallet_binding_proof_invalid")
        elif wallet_mode == "local_wallet_transfer":
            expected_wallet = content_hash(
                {
                    "task_id": task.task_id,
                    "target_id": task.target_id,
                    "manifest_digest": manifest_digest,
                    "address": wallet.get("address"),
                    "target_identity": wallet.get("target_identity"),
                    "approval_fingerprint": wallet.get("approval_fingerprint"),
                }
            )
            if wallet_proof != expected_wallet:
                raise MigrationExecutionError("wallet_binding_proof_invalid")
        credential_digest = content_hash(
            {
                "task_id": task.task_id,
                "manifest_digest": manifest_digest,
                "binding": dict(credential),
            }
        )
        wallet_digest = (
            content_hash(
                {
                    "task_id": task.task_id,
                    "manifest_digest": manifest_digest,
                    "binding": dict(wallet),
                }
            )
            if wallet_mode != "disabled"
            else None
        )
        return {
            "volume_digest": content_hash(
                {"task_id": task.task_id, "manifest_digest": manifest_digest, "proof": dict(volume)}
            ),
            "credential_digest": credential_digest,
            "signer_digest": wallet_digest if wallet_mode == "external_signer_rebind" else None,
            "wallet_mode": str(wallet_mode),
            "wallet_digest": wallet_digest,
        }

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
        _, digest = _stream_digest(bundle.path)
        manifest.setdefault("artifact_sha256", digest)
        manifest.setdefault("byte_size", bundle.path.stat().st_size)
        if manifest.get("format") != BUNDLE_FORMAT:
            raise MigrationExecutionError("recipient_encrypted_bundle_unavailable")
        if manifest.get("artifact_format") != BUNDLE_FORMAT:
            raise MigrationExecutionError("artifact_manifest_invalid")
        if manifest.get("task_id") != task.task_id or manifest.get("target_id") != task.target_id:
            raise MigrationExecutionError("artifact_manifest_context_mismatch")
        if manifest.get("source_epoch") != task.source_epoch:
            raise MigrationExecutionError("artifact_manifest_context_mismatch")
        if manifest.get("subject_id") != task.subject_id:
            raise MigrationExecutionError("artifact_manifest_subject_mismatch")
        if manifest.get("byte_size") != manifest.get("ciphertext_size"):
            raise MigrationExecutionError("artifact_manifest_size_mismatch")
        if (
            proof.get("artifact_id") is not None
            and manifest.get("artifact_id") != proof.get("artifact_id")
        ):
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
        artifact_sha256: str,
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
            "artifact_sha256": artifact_sha256,
            "health_report_digest": health_report_digest,
            "source_fence_digest": source_fence_digest,
            "recipient_key_fingerprint": target["recipient_key_fingerprint"],
            "target_volume_proof_digest": None,
            "credential_binding_digest": None,
            "signer_binding_digest": None,
            "wallet_mode": None,
            "wallet_proof_digest": None,
            "status": "active",
            "service_unit": "noyra.service",
        }
        for key in ("target_volume_proof_digest", "credential_binding_digest"):
            value = receipt.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise MigrationExecutionError("target_activation_receipt_invalid")
            required[key] = value
        wallet_mode = receipt.get("wallet_mode")
        if wallet_mode not in {"external_signer_rebind", "local_wallet_transfer", "disabled"}:
            raise MigrationExecutionError("target_activation_receipt_invalid")
        required["wallet_mode"] = wallet_mode
        for key in ("signer_binding_digest", "wallet_proof_digest"):
            value = receipt.get(key)
            if value is not None and (
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            ):
                raise MigrationExecutionError("target_activation_receipt_invalid")
            required[key] = value
        if wallet_mode == "external_signer_rebind" and (
            required["signer_binding_digest"] is None
            or required["wallet_proof_digest"] is None
            or required["signer_binding_digest"] != required["wallet_proof_digest"]
        ):
            raise MigrationExecutionError("target_activation_receipt_invalid")
        if wallet_mode == "local_wallet_transfer" and required["wallet_proof_digest"] is None:
            raise MigrationExecutionError("target_activation_receipt_invalid")
        if wallet_mode == "disabled" and any(
            required[key] is not None for key in ("signer_binding_digest", "wallet_proof_digest")
        ):
            raise MigrationExecutionError("target_activation_receipt_invalid")
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


def _stream_digest(path: Path, *, chunk_bytes: int = 1024 * 1024) -> tuple[int, str]:
    """Hash a regular file with bounded memory and return size plus digest."""
    if not path.is_file() or path.is_symlink():
        raise MigrationExecutionError("artifact_output_invalid")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_bytes):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


__all__ = [
    "ArtifactBundle",
    "HTTPMigrationExecutor",
    "HTTPTransport",
    "SQLiteArtifactProvider",
    "UrllibHTTPTransport",
]
