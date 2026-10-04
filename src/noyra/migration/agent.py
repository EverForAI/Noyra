"""Restricted target-side migration agent."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from noyra.core.at_rest import AtRestConfig, VolumeEncryptionProbe, VolumeEncryptionStatus
from noyra.core.types import canonical_json, content_hash

from .bundle import BUNDLE_FORMAT, decrypt_bundle
from .trust import (
    RecipientPoPChallenge,
    RecipientPoPProof,
    TargetAttestation,
    TargetChallenge,
    open_recipient_pop_challenge,
)


def _file_digest(path: Path, *, chunk_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def _files_equal(path: Path, expected: bytes, *, chunk_bytes: int = 1024 * 1024) -> bool:
    if path.stat().st_size != len(expected):
        return False
    offset = 0
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_bytes):
            if chunk != expected[offset : offset + len(chunk)]:
                return False
            offset += len(chunk)
    return offset == len(expected)


_ARTIFACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_FORBIDDEN = frozenset(
    {"secret", "token", "password", "private_key", "api_key", "bearer", "credential"}
)
_AUTH_SCHEME = "Noyra-HMAC"
_AUTH_CLOCK_SKEW_SECONDS = 300
_AUTH_NONCE_TTL_SECONDS = 600
_AUTH_NONCE_LIMIT = 4096
_AUTH_MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_CHUNKS = 2_000_000
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}\Z")
_SUBJECT = re.compile(r"Noyra-[A-Za-z0-9_-]{1,120}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_REFERENCE = re.compile(r"(?:systemd|kms|secret):[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,511}\Z")
_FILE_REFERENCE = re.compile(r"file:/[A-Za-z0-9_./@:+,-]{1,510}\Z")


def _safe_reference(value: str) -> bool:
    return (
        _REFERENCE.fullmatch(value) is not None
        or (
            _FILE_REFERENCE.fullmatch(value) is not None
            and ".." not in value.removeprefix("file:").split("/")
        )
    )


class MigrationVolumeProbe(Protocol):
    def probe(
        self,
        data_root: Path | str,
        *,
        backend: str,
        attestation_path: Path | None,
    ) -> VolumeEncryptionStatus: ...


class AgentAuthenticationError(ValueError):
    """Raised when an HTTP caller cannot prove possession of the session token."""


@dataclass(frozen=True)
class EnrollmentReceipt:
    target_id: str
    key_fingerprint: str
    generation: int
    host_identity: str = ""


@dataclass(frozen=True)
class ReceiveReceipt:
    artifact_id: str
    manifest_digest: str
    byte_size: int
    status: str
    manifest_path: str | None = None
    artifact_path: str | None = None
    artifact_sha256: str | None = None


@dataclass(frozen=True)
class ChunkReceiveReceipt:
    artifact_id: str
    manifest_digest: str
    byte_size: int
    chunk_index: int
    chunk_count: int
    received_chunks: int
    complete: bool
    manifest_path: str | None = None
    artifact_path: str | None = None
    artifact_sha256: str | None = None


@dataclass(frozen=True)
class RestoreReport:
    target_id: str
    generation: int
    artifact_id: str
    manifest_digest: str
    status: str
    subject_id: str | None = None
    event_chain_tip: str | None = None
    artifact_sha256: str | None = None
    restore_path: str | None = None


@dataclass(frozen=True)
class TargetHealthReport:
    target_id: str
    generation: int
    status: str
    manifest_digest: str
    artifact_id: str
    host_identity: str
    checks: dict[str, bool]

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "generation": self.generation,
            "status": self.status,
            "manifest_digest": self.manifest_digest,
            "artifact_id": self.artifact_id,
            "host_identity": self.host_identity,
            "checks": dict(self.checks),
        }


class MigrationAgent:
    """Handle target enrollment, authenticated manifests, restore and health."""

    def __init__(
        self,
        *,
        target_id: str,
        key_fingerprint: str,
        generation: int = 1,
        signing_key: Ed25519PrivateKey | None = None,
        public_key: str | None = None,
        recipient_private_key: X25519PrivateKey | None = None,
        recipient_public_key: str | None = None,
        recipient_key_fingerprint: str | None = None,
        data_root: Path | str | None = None,
        require_encrypted_storage: bool = True,
        session_token: str | None = None,
        max_incoming_bytes: int = 64 * 1024 * 1024,
        max_incoming_files: int = 128,
        artifact_ttl_seconds: int = 7 * 24 * 60 * 60,
        backup_manager: Any | None = None,
        restore_root: Path | str | None = None,
        activation_controller: Any | None = None,
        volume_probe: MigrationVolumeProbe | None = None,
        credential_references: Mapping[str, str] | None = None,
        credential_fingerprints: Mapping[str, str] | None = None,
        signer_id: str | None = None,
        wallet_address: str | None = None,
    ) -> None:
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", target_id)
            or not re.fullmatch(r"[0-9a-f]{64}", key_fingerprint)
            or type(generation) is not int
            or generation < 1
        ):
            raise ValueError("agent identity is invalid")
        if signing_key is not None and not isinstance(signing_key, Ed25519PrivateKey):
            raise TypeError("agent signing key is invalid")
        if recipient_private_key is not None and not isinstance(
            recipient_private_key, X25519PrivateKey
        ):
            raise TypeError("agent recipient key is invalid")
        if recipient_public_key is None and recipient_private_key is not None:
            recipient_public_key = (
                base64.urlsafe_b64encode(recipient_private_key.public_key().public_bytes_raw())
                .decode("ascii")
                .rstrip("=")
            )
        if recipient_public_key is not None:
            recipient_bytes = self._decode_recipient_public_key(recipient_public_key)
            calculated = hashlib.sha256(recipient_bytes).hexdigest()
            if recipient_key_fingerprint not in {None, calculated}:
                raise ValueError("agent recipient key fingerprint does not match public key")
            recipient_key_fingerprint = calculated
            if recipient_private_key is not None and (
                recipient_private_key.public_key().public_bytes_raw() != recipient_bytes
            ):
                raise ValueError("agent recipient private key does not match public key")
        elif recipient_key_fingerprint is not None or recipient_private_key is not None:
            raise ValueError("agent recipient key identity is incomplete")
        if public_key is None and signing_key is not None:
            public_key = base64.urlsafe_b64encode(
                signing_key.public_key().public_bytes_raw()
            ).decode()
        if public_key is not None:
            public_bytes = self._decode_public_key(public_key)
            if hashlib.sha256(public_bytes).hexdigest() != key_fingerprint:
                raise ValueError("agent key fingerprint does not match public key")
        if session_token is not None and (
            not isinstance(session_token, str)
            or not 32 <= len(session_token) <= 256
            or any(character.isspace() for character in session_token)
        ):
            raise ValueError("agent session token is invalid")
        if type(max_incoming_bytes) is not int or not 1 <= max_incoming_bytes <= 1 << 30:
            raise ValueError("incoming byte quota is invalid")
        if type(max_incoming_files) is not int or not 1 <= max_incoming_files <= 100_000:
            raise ValueError("incoming file quota is invalid")
        if type(artifact_ttl_seconds) is not int or not 60 <= artifact_ttl_seconds <= 90 * 86400:
            raise ValueError("artifact TTL is invalid")
        if require_encrypted_storage is not True:
            raise ValueError("migration encrypted storage requirement cannot be disabled")
        self._validate_binding_configuration(
            credential_references, credential_fingerprints, signer_id, wallet_address
        )
        self.target_id = target_id
        self.key_fingerprint = key_fingerprint
        self.generation = generation
        self.public_key = public_key
        self._signing_key = signing_key
        self.recipient_public_key = recipient_public_key
        self.recipient_key_fingerprint = recipient_key_fingerprint
        self._recipient_private_key = recipient_private_key
        self._recipient_pop_nonces: set[str] = set()
        self.require_encrypted_storage = True
        self._volume_probe = volume_probe or VolumeEncryptionProbe()
        self._session_token = session_token
        self.max_incoming_bytes = max_incoming_bytes
        self.max_incoming_files = max_incoming_files
        self.artifact_ttl_seconds = artifact_ttl_seconds
        self.backup_manager = backup_manager
        self.activation_controller = activation_controller
        self.credential_references = dict(credential_references or {})
        self.credential_fingerprints = dict(credential_fingerprints or {})
        self.signer_id = signer_id
        self.wallet_address = wallet_address
        self.restore_root = (
            Path(restore_root).expanduser().resolve() if restore_root is not None else None
        )
        if self.restore_root is not None:
            if self.restore_root.exists() and self.restore_root.is_symlink():
                raise ValueError("migration restore root cannot be a symlink")
            self.restore_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._assert_private_directory(self.restore_root)
        self._io_lock = threading.RLock()
        self.data_root = self._prepare_root(data_root)
        self.host_identity = hashlib.sha256(
            f"noyra-target-host-v1\n{target_id}\n{key_fingerprint}\n{generation}".encode()
        ).hexdigest()

    @staticmethod
    def _validate_binding_configuration(
        references: Mapping[str, str] | None,
        fingerprints: Mapping[str, str] | None,
        signer_id: str | None,
        wallet_address: str | None,
    ) -> None:
        if references is not None and (
            not isinstance(references, Mapping)
            or any(
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", key)
                or not isinstance(value, str)
                or not 1 <= len(value) <= 512
                or not _safe_reference(value)
                or any(token in key.casefold() for token in _FORBIDDEN)
                for key, value in references.items()
            )
        ):
            raise ValueError("target credential references are invalid")
        if fingerprints is not None and (
            not isinstance(fingerprints, Mapping)
            or any(
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", key)
                or not isinstance(value, str)
                or not _HEX64.fullmatch(value)
                for key, value in fingerprints.items()
            )
        ):
            raise ValueError("target credential fingerprints are invalid")
        if set(references or {}) != set(fingerprints or {}):
            raise ValueError("target credential binding configuration is incomplete")
        if signer_id is not None and (
            not isinstance(signer_id, str) or not _IDENTITY.fullmatch(signer_id)
        ):
            raise ValueError("target signer identity is invalid")
        if wallet_address is not None and (
            not isinstance(wallet_address, str)
            or not 1 <= len(wallet_address) <= 256
            or any(character.isspace() for character in wallet_address)
        ):
            raise ValueError("target wallet address is invalid")

    def enroll(self, request: Mapping[str, Any] | None = None) -> EnrollmentReceipt:
        if request is not None:
            if not isinstance(request, Mapping) or set(request) - {"subject_id", "policy_revision"}:
                raise ValueError("enrollment request is invalid")
            subject_id = request.get("subject_id")
            if not isinstance(subject_id, str) or not re.fullmatch(
                r"Noyra-[A-Za-z0-9_-]{1,120}", subject_id
            ):
                raise ValueError("enrollment subject is invalid")
        return EnrollmentReceipt(
            self.target_id, self.key_fingerprint, self.generation, self.host_identity
        )

    def challenge(self, request: TargetChallenge | Mapping[str, Any]) -> TargetAttestation:
        if self._signing_key is None or self.public_key is None:
            raise ValueError("target signing identity is not configured")
        if isinstance(request, Mapping):
            try:
                request = TargetChallenge(
                    nonce=str(request["nonce"]),
                    expires_at=str(request["expires_at"]),
                    source_epoch=str(request["source_epoch"]),
                    target_id=(str(request["target_id"]) if request.get("target_id") else None),
                    endpoint_origin=(
                        str(request["endpoint_origin"])
                        if request.get("endpoint_origin")
                        else None
                    ),
                )
            except (KeyError, TypeError) as error:
                raise ValueError("challenge request is invalid") from error
        if not isinstance(request, TargetChallenge):
            raise ValueError("challenge request is invalid")
        request.ensure_fresh()
        signature = base64.urlsafe_b64encode(
            self._signing_key.sign(request.signing_bytes())
        ).decode()
        return TargetAttestation(self.target_id, self.public_key, request, signature)

    def recipient_pop(
        self, request: RecipientPoPChallenge | Mapping[str, Any]
    ) -> RecipientPoPProof:
        """Decrypt and sign a one-time recipient-key proof."""
        if self._recipient_private_key is None or self.recipient_key_fingerprint is None:
            raise ValueError("target recipient identity is not configured")
        proof = open_recipient_pop_challenge(request, self._recipient_private_key)
        with self._io_lock:
            if proof.pop_nonce in self._recipient_pop_nonces:
                raise ValueError("recipient proof challenge was already consumed")
            self._recipient_pop_nonces.add(proof.pop_nonce)
        if self._signing_key is None:
            raise ValueError("target signing identity is not configured")
        signature = (
            base64.urlsafe_b64encode(self._signing_key.sign(proof.signing_bytes()))
            .decode("ascii")
            .rstrip("=")
        )
        return RecipientPoPProof(
            proof.target_id,
            proof.source_epoch,
            proof.expires_at,
            proof.pop_nonce,
            proof.recipient_key_fingerprint,
            signature,
        )

    def binding_proof(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Verify target-side bindings and return a signed, non-secret receipt.

        The source sends only references, fingerprints, and a wallet mode.  The
        target compares those values with its protected configuration and signs
        the resulting evidence.  No credential value or private key is accepted
        by this interface.
        """
        required = {
            "task_id",
            "subject_id",
            "target_id",
            "source_epoch",
            "manifest_digest",
            "artifact_id",
            "recipient_key_fingerprint",
            "credential_binding",
            "wallet_binding",
        }
        if not isinstance(request, Mapping) or set(request) != required:
            raise ValueError("migration binding request is invalid")
        task_id = request.get("task_id")
        subject_id = request.get("subject_id")
        target_id = request.get("target_id")
        source_epoch = request.get("source_epoch")
        manifest_digest = request.get("manifest_digest")
        artifact_id = request.get("artifact_id")
        recipient_fingerprint = request.get("recipient_key_fingerprint")
        if (
            not isinstance(task_id, str)
            or not _IDENTITY.fullmatch(task_id)
            or not isinstance(subject_id, str)
            or not _SUBJECT.fullmatch(subject_id)
            or target_id != self.target_id
            or not isinstance(source_epoch, str)
            or not _IDENTITY.fullmatch(source_epoch)
            or not isinstance(manifest_digest, str)
            or not _HEX64.fullmatch(manifest_digest)
            or not isinstance(artifact_id, str)
            or not _ARTIFACT_ID.fullmatch(artifact_id)
            or not isinstance(recipient_fingerprint, str)
            or not _HEX64.fullmatch(recipient_fingerprint)
        ):
            raise ValueError("migration binding request identity is invalid")
        if self._signing_key is None or self.public_key is None:
            raise ValueError("target signing identity is not configured")
        if self.data_root is None:
            raise ValueError("migration binding storage is unavailable")
        if self.recipient_key_fingerprint is None or self._recipient_private_key is None:
            raise ValueError("target recipient identity is not configured")
        if recipient_fingerprint != self.recipient_key_fingerprint:
            raise ValueError("target recipient key fingerprint does not match")
        self._require_encrypted_volume(self.data_root)
        path = self._binding_record_path(task_id)
        with self._io_lock:
            if path.exists():
                existing = self._read_binding_record(path)
                self._assert_binding_request_matches(existing, request)
                return existing
            credential = self._credential_binding_proof(
                request["credential_binding"],
                task_id=task_id,
                target_id=target_id,
                manifest_digest=manifest_digest,
            )
            wallet = self._wallet_binding_proof(
                request["wallet_binding"],
                task_id=task_id,
                target_id=target_id,
                manifest_digest=manifest_digest,
                subject_id=subject_id,
            )
            volume = self._volume_binding_proof(
                task_id=task_id,
                target_id=target_id,
                manifest_digest=manifest_digest,
                recipient_fingerprint=recipient_fingerprint,
            )
            response: dict[str, Any] = {
                "status": "verified",
                "task_id": task_id,
                "subject_id": subject_id,
                "target_id": target_id,
                "source_epoch": source_epoch,
                "manifest_digest": manifest_digest,
                "artifact_id": artifact_id,
                "target_generation": self.generation,
                "target_identity": self.host_identity,
                "recipient_key_fingerprint": recipient_fingerprint,
                "credential_binding": credential,
                "wallet_binding": wallet,
                "target_volume_proof": volume,
            }
            response["target_signature"] = self._sign_binding_record(response)
            self._write_private_json(path, response)
            return response

    def _read_binding_record(self, path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise ValueError("migration binding record is invalid")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError("migration binding record is invalid") from error
        if not isinstance(value, dict):
            raise ValueError("migration binding record is invalid")
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
        if set(value) != required or value.get("status") != "verified":
            raise ValueError("migration binding record is invalid")
        if self._signing_key is None or self.public_key is None:
            raise ValueError("target signing identity is not configured")
        signature = value["target_signature"]
        if not isinstance(signature, str):
            raise ValueError("migration binding record signature is invalid")
        signed = {key: item for key, item in value.items() if key != "target_signature"}
        try:
            public = Ed25519PublicKey.from_public_bytes(self._decode_public_key(self.public_key))
            raw_signature = self._decode_signature(signature)
            public.verify(raw_signature, canonical_json(signed).encode("utf-8"))
        except (ValueError, TypeError, InvalidSignature) as error:
            raise ValueError("migration binding record signature is invalid") from error
        return value

    def _sign_binding_record(self, value: Mapping[str, Any]) -> str:
        if self._signing_key is None:
            raise ValueError("target signing identity is not configured")
        return base64.urlsafe_b64encode(
            self._signing_key.sign(canonical_json(dict(value)).encode("utf-8"))
        ).decode("ascii")

    @staticmethod
    def _decode_signature(value: str) -> bytes:
        if not value or "=" in value.rstrip("="):
            raise ValueError("signature encoding is invalid")
        try:
            raw = base64.b64decode(
                value.encode("ascii"), altchars=b"-_", validate=True
            )
        except (ValueError, TypeError, UnicodeError, binascii.Error) as error:
            raise ValueError("signature encoding is invalid") from error
        if base64.urlsafe_b64encode(raw).decode("ascii") != value:
            raise ValueError("signature encoding is invalid")
        return raw

    @staticmethod
    def _assert_binding_request_matches(
        response: Mapping[str, Any], request: Mapping[str, Any]
    ) -> None:
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
                raise ValueError("migration binding record conflicts with existing task")
        credential = response.get("credential_binding")
        requested_credential = request.get("credential_binding")
        if (
            not isinstance(credential, Mapping)
            or not isinstance(requested_credential, Mapping)
            or credential.get("references") != requested_credential.get("references")
            or credential.get("fingerprints") != requested_credential.get("fingerprints")
        ):
            raise ValueError("migration binding record conflicts with existing task")
        wallet = response.get("wallet_binding")
        requested_wallet = request.get("wallet_binding")
        if not isinstance(wallet, Mapping) or not isinstance(requested_wallet, Mapping):
            raise ValueError("migration binding record conflicts with existing task")
        if wallet.get("mode") != requested_wallet.get("mode"):
            raise ValueError("migration binding record conflicts with existing task")
        for key in ("signer_id", "address"):
            if key in requested_wallet and wallet.get(key) != requested_wallet.get(key):
                raise ValueError("migration binding record conflicts with existing task")
        approval = requested_wallet.get("approval")
        if approval is not None and (
            not isinstance(approval, Mapping)
            or any(
                wallet.get(key) != approval.get(key)
                for key in ("approval_id", "channel_id", "expires_at")
            )
        ):
            raise ValueError("migration binding record conflicts with existing task")

    def _credential_binding_proof(
        self,
        value: Any,
        *,
        task_id: str,
        target_id: str,
        manifest_digest: str,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {"references", "fingerprints"}:
            raise ValueError("target credential binding request is invalid")
        references = value.get("references")
        fingerprints = value.get("fingerprints")
        if (
            not isinstance(references, Mapping)
            or not isinstance(fingerprints, Mapping)
            or set(references) != set(fingerprints)
            or any(
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", key)
                or not isinstance(ref, str)
                or not 1 <= len(ref) <= 512
                or any(character.isspace() for character in ref)
                or not _safe_reference(ref)
                or not isinstance(fingerprints[key], str)
                or not _HEX64.fullmatch(fingerprints[key])
                for key, ref in references.items()
            )
        ):
            raise ValueError("target credential binding request is invalid")
        normalized_references = {str(key): str(value) for key, value in references.items()}
        normalized_fingerprints = {str(key): str(value) for key, value in fingerprints.items()}
        if normalized_references != self.credential_references:
            raise ValueError("target credential references do not match")
        if normalized_fingerprints != self.credential_fingerprints:
            raise ValueError("target credential fingerprints do not match")
        proof = content_hash(
            {
                "task_id": task_id,
                "manifest_digest": manifest_digest,
                "target_id": target_id,
                "target_identity": self.host_identity,
                "references": normalized_references,
                "fingerprints": normalized_fingerprints,
            }
        )
        return {
            "status": "verified",
            "target_id": target_id,
            "target_generation": self.generation,
            "target_identity": self.host_identity,
            "manifest_digest": manifest_digest,
            "references": normalized_references,
            "fingerprints": normalized_fingerprints,
            "availability_proof": proof,
        }

    def _wallet_binding_proof(
        self,
        value: Any,
        *,
        task_id: str,
        target_id: str,
        manifest_digest: str,
        subject_id: str,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("target wallet binding request is invalid")
        mode = value.get("mode")
        if mode == "disabled":
            if set(value) != {"mode"}:
                raise ValueError("target wallet binding request is invalid")
            return {
                "status": "verified",
                "mode": "disabled",
                "target_id": target_id,
                "target_generation": self.generation,
                "target_identity": self.host_identity,
                "manifest_digest": manifest_digest,
            }
        if mode == "external_signer_rebind":
            if set(value) != {"mode", "signer_id", "address"}:
                raise ValueError("target wallet binding request is invalid")
            signer_id = value.get("signer_id")
            address = value.get("address")
            if (
                not isinstance(signer_id, str)
                or signer_id != self.signer_id
                or not isinstance(address, str)
                or self.wallet_address is None
                or address.casefold() != self.wallet_address.casefold()
            ):
                raise ValueError("target signer binding does not match")
            proof = content_hash(
                {
                    "task_id": task_id,
                    "target_id": target_id,
                    "manifest_digest": manifest_digest,
                    "signer_id": signer_id,
                    "address": address,
                    "target_identity": self.host_identity,
                }
            )
            return {
                "status": "verified",
                "mode": mode,
                "target_id": target_id,
                "target_generation": self.generation,
                "target_identity": self.host_identity,
                "manifest_digest": manifest_digest,
                "signer_id": signer_id,
                "address": address,
                "proof_digest": proof,
            }
        if mode != "local_wallet_transfer" or set(value) != {"mode", "address", "approval"}:
            raise ValueError("target wallet binding request is invalid")
        address = value.get("address")
        approval = value.get("approval")
        if (
            not isinstance(address, str)
            or self.wallet_address is None
            or address.casefold() != self.wallet_address.casefold()
            or not isinstance(approval, Mapping)
            or set(approval)
            != {"approval_id", "task_id", "address", "channel_id", "expires_at"}
        ):
            raise ValueError("target local wallet approval is invalid")
        approval_id = approval.get("approval_id")
        approval_task = approval.get("task_id")
        approval_address = approval.get("address")
        channel_id = approval.get("channel_id")
        expires_at = approval.get("expires_at")
        if (
            not isinstance(approval_id, str)
            or not _IDENTITY.fullmatch(approval_id)
            or approval_task != task_id
            or approval_address != address
            or not isinstance(channel_id, str)
            or not _IDENTITY.fullmatch(channel_id)
            or not isinstance(expires_at, str)
        ):
            raise ValueError("target local wallet approval is invalid")
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("target local wallet approval expiry is invalid") from error
        if expiry.tzinfo is None or expiry.astimezone(UTC) <= datetime.now(UTC):
            raise ValueError("target local wallet approval has expired")
        approval_fingerprint = content_hash(
            {
                "approval_id": approval_id,
                "task_id": task_id,
                "address": address,
                "channel_id": channel_id,
                "expires_at": expires_at,
                "target_id": target_id,
                "target_identity": self.host_identity,
            }
        )
        if self.data_root is None:
            raise ValueError("migration binding storage is unavailable")
        marker = self.data_root / "binding-approvals" / f"{approval_fingerprint}.json"
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._assert_private_directory(marker.parent)
        try:
            self._write_private_json(
                marker,
                {
                    "approval_fingerprint": approval_fingerprint,
                    "task_id": task_id,
                    "subject_id": subject_id,
                    "target_id": target_id,
                },
            )
        except FileExistsError as error:
            raise ValueError("local wallet approval was already consumed") from error
        proof = content_hash(
            {
                "task_id": task_id,
                "target_id": target_id,
                "manifest_digest": manifest_digest,
                "address": address,
                "target_identity": self.host_identity,
                "approval_fingerprint": approval_fingerprint,
            }
        )
        return {
            "status": "verified",
            "mode": mode,
            "target_id": target_id,
            "target_generation": self.generation,
            "target_identity": self.host_identity,
            "manifest_digest": manifest_digest,
            "address": address,
            "approval_id": approval_id,
            "approval_fingerprint": approval_fingerprint,
            "channel_id": channel_id,
            "expires_at": expires_at,
            "proof_digest": proof,
        }

    def _volume_binding_proof(
        self,
        *,
        task_id: str,
        target_id: str,
        manifest_digest: str,
        recipient_fingerprint: str,
    ) -> dict[str, Any]:
        if self.data_root is None:
            raise ValueError("migration binding storage is unavailable")
        config = AtRestConfig.from_env(self.data_root)
        proof = content_hash(
            {
                "task_id": task_id,
                "target_id": target_id,
                "manifest_digest": manifest_digest,
                "recipient_key_fingerprint": recipient_fingerprint,
                "target_identity": self.host_identity,
                "target_generation": self.generation,
                "backend": config.volume_backend,
            }
        )
        return {
            "status": "verified",
            "encrypted": True,
            "target_id": target_id,
            "target_generation": self.generation,
            "target_identity": self.host_identity,
            "manifest_digest": manifest_digest,
            "recipient_key_fingerprint": recipient_fingerprint,
            "backend": config.volume_backend,
            "proof_digest": proof,
        }

    def _binding_record_path(self, task_id: str) -> Path:
        if self.data_root is None:
            raise ValueError("migration binding storage is unavailable")
        bindings = self.data_root / "bindings"
        bindings.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._assert_private_directory(bindings)
        return bindings / f"{task_id}.json"

    def sign_recovery_proof(self, signing_bytes: bytes) -> str:
        """Sign a source-provided recovery proof without exposing the key."""
        if self._signing_key is None:
            raise ValueError("target signing identity is not configured")
        if not isinstance(signing_bytes, bytes) or not signing_bytes:
            raise ValueError("recovery proof bytes are required")
        return base64.urlsafe_b64encode(self._signing_key.sign(signing_bytes)).decode()

    def receive(
        self,
        manifest: Mapping[str, Any],
        *,
        artifact: bytes | None = None,
    ) -> ReceiveReceipt:
        if not isinstance(manifest, Mapping):
            raise ValueError("migration manifest is invalid")
        values = self._validate_manifest(manifest)
        artifact_id = values["artifact_id"]
        digest = content_hash(values)
        manifest_path: str | None = None
        artifact_path: str | None = None
        artifact_sha256: str | None = None
        if artifact is not None:
            if not isinstance(artifact, bytes):
                raise ValueError("migration artifact bytes are invalid")
            if len(artifact) != values["byte_size"]:
                raise ValueError("migration artifact byte size mismatch")
            artifact_sha256 = hashlib.sha256(artifact).hexdigest()
            expected_artifact_sha256 = values.get("artifact_sha256")
            if expected_artifact_sha256 is not None and artifact_sha256 != expected_artifact_sha256:
                raise ValueError("migration artifact digest mismatch")
        elif self.data_root is not None:
            raise ValueError("migration artifact bytes are required")
        if self.data_root is not None:
            self._require_encrypted_volume(self.data_root)
            encoded = canonical_json(values).encode("utf-8")
            with self._io_lock:
                self.cleanup_expired()
                incoming = self.data_root / "incoming"
                incoming.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._assert_private_directory(incoming)
                file_count, byte_count = self._incoming_usage(incoming)
                if file_count >= self.max_incoming_files or (
                    byte_count + len(encoded) + (len(artifact) if artifact is not None else 0)
                    > self.max_incoming_bytes
                ):
                    raise ValueError("migration incoming quota exceeded")
                path = incoming / f"{artifact_id}.json"
                binary_path = incoming / f"{artifact_id}.artifact"
                if (
                    path.exists()
                    or path.is_symlink()
                    or binary_path.exists()
                    or binary_path.is_symlink()
                ):
                    raise ValueError("migration artifact already exists")
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                except Exception:
                    with suppress(OSError):
                        os.close(descriptor)
                    path.unlink(missing_ok=True)
                    raise
                manifest_path = str(path)
                if artifact is not None:
                    binary_descriptor = os.open(
                        binary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    try:
                        with os.fdopen(binary_descriptor, "wb") as stream:
                            stream.write(artifact)
                            stream.flush()
                            os.fsync(stream.fileno())
                    except Exception:
                        with suppress(OSError):
                            os.close(binary_descriptor)
                        path.unlink(missing_ok=True)
                        binary_path.unlink(missing_ok=True)
                        raise
                    artifact_path = str(binary_path)
        return ReceiveReceipt(
            artifact_id,
            digest,
            values["byte_size"],
            "received",
            manifest_path,
            artifact_path,
            artifact_sha256,
        )

    def receive_chunk(
        self,
        manifest: Mapping[str, Any],
        *,
        chunk_index: int,
        chunk_count: int,
        chunk_bytes: int,
        chunk_sha256: str,
        chunk: bytes,
    ) -> ChunkReceiveReceipt:
        """Accept one authenticated, resumable artifact chunk."""
        values = self._validate_manifest(manifest)
        if self.data_root is None:
            raise ValueError("migration chunk storage is unavailable")
        self._require_encrypted_volume(self.data_root)
        if type(chunk_index) is not int or chunk_index < 0:
            raise ValueError("migration chunk index is invalid")
        if type(chunk_count) is not int or not 1 <= chunk_count <= MAX_CHUNKS:
            raise ValueError("migration chunk count is invalid")
        if type(chunk_bytes) is not int or not 4096 <= chunk_bytes <= MAX_CHUNK_BYTES:
            raise ValueError("migration chunk size is invalid")
        if values["byte_size"] < 1:
            raise ValueError("migration artifact byte size is invalid")
        expected_count = (values["byte_size"] + chunk_bytes - 1) // chunk_bytes
        if chunk_count != expected_count or chunk_index >= chunk_count:
            raise ValueError("migration chunk layout is invalid")
        if not isinstance(chunk, bytes) or not 1 <= len(chunk) <= chunk_bytes:
            raise ValueError("migration chunk bytes are invalid")
        if not isinstance(chunk_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", chunk_sha256):
            raise ValueError("migration chunk digest is invalid")
        if hashlib.sha256(chunk).hexdigest() != chunk_sha256:
            raise ValueError("migration chunk digest mismatch")
        if "artifact_sha256" not in values:
            raise ValueError("migration artifact digest is required for chunked transfer")
        digest = content_hash(values)
        with self._io_lock:
            self.cleanup_expired()
            incoming = self.data_root / "incoming"
            incoming.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._assert_private_directory(incoming)
            final_manifest = incoming / f"{values['artifact_id']}.json"
            final_artifact = incoming / f"{values['artifact_id']}.artifact"
            if final_manifest.is_file() and final_artifact.is_file():
                return ChunkReceiveReceipt(
                    values["artifact_id"],
                    digest,
                    values["byte_size"],
                    chunk_index,
                    chunk_count,
                    chunk_count,
                    True,
                    str(final_manifest),
                    str(final_artifact),
                    values["artifact_sha256"],
                )
            session = incoming / f".{values['artifact_id']}.chunks"
            if session.exists() and session.is_symlink():
                raise ValueError("migration chunk session is invalid")
            session.mkdir(mode=0o700, exist_ok=True)
            self._assert_private_directory(session)
            metadata_path = session / "manifest.json"
            metadata = {
                "manifest": values,
                "manifest_digest": digest,
                "chunk_count": chunk_count,
                "chunk_bytes": chunk_bytes,
            }
            metadata_bytes = len(canonical_json(metadata).encode("utf-8"))
            if metadata_path.exists():
                try:
                    existing = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError) as error:
                    raise ValueError("migration chunk session metadata is invalid") from error
                if existing != metadata:
                    raise ValueError("migration chunk session binding mismatch")
            else:
                file_count, byte_count = self._incoming_usage(incoming)
                if (
                    file_count + 2 > self.max_incoming_files
                    or byte_count + metadata_bytes + len(chunk) > self.max_incoming_bytes
                ):
                    raise ValueError("migration incoming quota exceeded")
                self._write_private_json(metadata_path, metadata)
            part = session / f"{chunk_index:08d}.part"
            if part.exists():
                if part.is_symlink() or not part.is_file() or not _files_equal(part, chunk):
                    raise ValueError("migration chunk conflicts with an existing chunk")
            else:
                file_count, byte_count = self._incoming_usage(incoming)
                if (
                    file_count + 1 > self.max_incoming_files
                    or byte_count + len(chunk) > self.max_incoming_bytes
                ):
                    raise ValueError("migration incoming quota exceeded")
                self._write_private_bytes(part, chunk)
            parts: list[Path] = []
            for index in range(chunk_count):
                candidate = session / f"{index:08d}.part"
                if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
                    raise ValueError("migration chunk session contains an invalid part")
                if candidate.is_file():
                    parts.append(candidate)
            complete = len(parts) == chunk_count
            if complete:
                temporary = incoming / f".{values['artifact_id']}.artifact"
                artifact = hashlib.sha256()
                total = 0
                try:
                    with temporary.open("wb") as stream:
                        for part_path in parts:
                            with part_path.open("rb") as part_stream:
                                while payload := part_stream.read(1024 * 1024):
                                    artifact.update(payload)
                                    total += len(payload)
                                    stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if (
                        total != values["byte_size"]
                        or artifact.hexdigest() != values["artifact_sha256"]
                    ):
                        raise ValueError("migration artifact digest mismatch")
                    os.replace(temporary, final_artifact)
                    self._write_private_json(final_manifest, values)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
                shutil.rmtree(session)
            return ChunkReceiveReceipt(
                values["artifact_id"],
                digest,
                values["byte_size"],
                chunk_index,
                chunk_count,
                len(parts),
                complete,
                str(final_manifest) if complete else None,
                str(final_artifact) if complete else None,
                values["artifact_sha256"] if complete else None,
            )

    def cleanup_expired(self, *, now: float | None = None) -> int:
        """Delete only expired JSON manifests from the private incoming directory."""
        if self.data_root is None:
            return 0
        incoming = self.data_root / "incoming"
        if not incoming.exists():
            return 0
        self._assert_private_directory(incoming)
        current = time.time() if now is None else now
        removed = 0
        for path in incoming.iterdir():
            if path.is_dir() and path.name.endswith(".chunks"):
                try:
                    if current - path.stat().st_mtime > self.artifact_ttl_seconds:
                        shutil.rmtree(path)
                        removed += 1
                except OSError:
                    continue
                continue
            if path.suffix != ".json":
                continue
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if current - modified <= self.artifact_ttl_seconds:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            if path.suffix == ".json":
                with suppress(OSError):
                    (path.with_suffix(".artifact")).unlink()
            removed += 1
        return removed

    def authenticate_request(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        now: float | None = None,
    ) -> None:
        """Verify a task-independent HMAC request and persist its nonce atomically."""
        if self._session_token is None or self.data_root is None:
            raise AgentAuthenticationError("migration agent HTTP authentication is unavailable")
        if not isinstance(body, bytes) or len(body) > _AUTH_MAX_BODY_BYTES:
            raise AgentAuthenticationError("migration request body is invalid")
        authorization = headers.get("Authorization", "")
        scheme, separator, encoded_signature = authorization.partition(" ")
        timestamp_text = headers.get("X-Noyra-Timestamp", "")
        nonce = headers.get("X-Noyra-Nonce", "")
        body_digest = headers.get("X-Noyra-Body-SHA256", "")
        if (
            scheme != _AUTH_SCHEME
            or not separator
            or not encoded_signature
            or not re.fullmatch(r"[0-9]{1,20}", timestamp_text)
            or not _NONCE.fullmatch(nonce)
            or not re.fullmatch(r"[0-9a-f]{64}", body_digest)
        ):
            raise AgentAuthenticationError("migration request authentication failed")
        try:
            timestamp = int(timestamp_text)
            provided_signature = base64.urlsafe_b64decode(
                encoded_signature + "=" * (-len(encoded_signature) % 4)
            )
        except (TypeError, ValueError, binascii.Error) as error:
            raise AgentAuthenticationError("migration request authentication failed") from error
        current = time.time() if now is None else now
        if abs(current - timestamp) > _AUTH_CLOCK_SKEW_SECONDS:
            raise AgentAuthenticationError("migration request timestamp is stale")
        if not hmac.compare_digest(body_digest, hashlib.sha256(body).hexdigest()):
            raise AgentAuthenticationError("migration request body digest is invalid")
        signing_bytes = (
            f"noyra-migration-agent-v1\n{timestamp_text}\n{nonce}\n{body_digest}".encode()
        )
        expected_signature = hmac.new(
            self._session_token.encode("utf-8"), signing_bytes, hashlib.sha256
        ).digest()
        if len(provided_signature) != len(expected_signature) or not hmac.compare_digest(
            provided_signature, expected_signature
        ):
            raise AgentAuthenticationError("migration request authentication failed")
        with self._io_lock:
            replay_root = self.data_root / "auth-nonces"
            replay_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._assert_private_directory(replay_root)
            self._cleanup_nonces(replay_root, current)
            nonce_path = replay_root / f"{hashlib.sha256(nonce.encode()).hexdigest()}.nonce"
            try:
                descriptor = os.open(nonce_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as error:
                raise AgentAuthenticationError(
                    "migration request nonce was already used"
                ) from error
            try:
                with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                    stream.write(timestamp_text)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                with suppress(OSError):
                    os.close(descriptor)
                nonce_path.unlink(missing_ok=True)
                raise

    @staticmethod
    def _incoming_usage(incoming: Path) -> tuple[int, int]:
        file_count = 0
        byte_count = 0
        for path in incoming.rglob("*"):
            if path.is_symlink():
                raise ValueError("migration incoming directory contains a symlink")
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError as error:
                raise ValueError("migration incoming directory cannot be inspected") from error
            file_count += 1
            byte_count += size
        return file_count, byte_count

    def preflight(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        """Validate target volume and incoming capacity before transfer begins."""
        values = self._validate_manifest(manifest)
        if self.data_root is None:
            raise ValueError("migration preflight storage is unavailable")
        self._require_encrypted_volume(self.data_root)
        incoming = self.data_root / "incoming"
        incoming.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._assert_private_directory(incoming)
        with self._io_lock:
            self.cleanup_expired()
            file_count, byte_count = self._incoming_usage(incoming)
            manifest_bytes = len(canonical_json(values).encode("utf-8"))
            required_bytes = manifest_bytes + values["byte_size"]
            if file_count + 2 > self.max_incoming_files:
                raise ValueError("migration incoming file quota exceeded")
            if byte_count + required_bytes > self.max_incoming_bytes:
                raise ValueError("migration incoming byte quota exceeded")
        return {
            "status": "ready",
            "target_id": self.target_id,
            "artifact_id": values["artifact_id"],
            "byte_size": values["byte_size"],
            "required_bytes": required_bytes,
            "available_bytes": self.max_incoming_bytes - byte_count,
        }

    @staticmethod
    def _cleanup_nonces(replay_root: Path, now: float) -> None:
        paths = list(replay_root.glob("*.nonce"))
        for path in paths:
            try:
                if now - path.stat().st_mtime > _AUTH_NONCE_TTL_SECONDS:
                    path.unlink()
            except OSError:
                continue
        paths = list(replay_root.glob("*.nonce"))
        if len(paths) >= _AUTH_NONCE_LIMIT:
            raise AgentAuthenticationError("migration request replay store is full")

    def restore(
        self,
        receipt: ReceiveReceipt,
        *,
        expected_digest: str | None = None,
        task_id: str | None = None,
    ) -> RestoreReport:
        if not isinstance(receipt, ReceiveReceipt) or receipt.status != "received":
            raise ValueError("migration receipt is invalid")
        if expected_digest is not None and receipt.manifest_digest != expected_digest:
            raise ValueError("migration manifest digest mismatch")
        if self.restore_root is not None and (
            not isinstance(task_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}", task_id)
        ):
            raise ValueError("migration restore task identity is required")
        if self.data_root is not None:
            self._require_encrypted_volume(self.data_root)
        if self.restore_root is not None:
            self._require_encrypted_volume(self.restore_root)
        values = self._read_manifest(receipt)
        if content_hash(values) != receipt.manifest_digest:
            raise ValueError("migration manifest digest mismatch")
        artifact_path = self._artifact_path(receipt, values)
        artifact_sha256 = self._verify_artifact(artifact_path, values)
        subject_id = values.get("subject_id")
        tip = values.get("event_chain_tip")
        if subject_id is not None and (
            not isinstance(subject_id, str)
            or not re.fullmatch(r"Noyra-[A-Za-z0-9_-]{1,120}", subject_id)
        ):
            raise ValueError("migration subject identity is invalid")
        if tip is not None and (not isinstance(tip, str) or not re.fullmatch(r"[0-9a-f]{64}", tip)):
            raise ValueError("migration event-chain tip is invalid")
        restore_values = values
        restore_source = artifact_path
        if values.get("format") == BUNDLE_FORMAT:
            if self._recipient_private_key is None or self.restore_root is None:
                raise ValueError("recipient-encrypted restore is not configured")
            if task_id is None:
                raise ValueError("migration restore task identity is required")
            task_root = self.restore_root / task_id
            task_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._assert_private_directory(task_root)
            plaintext = task_root / ".noyra.sqlite3.bundle-plaintext"
            if plaintext.exists() or plaintext.is_symlink():
                raise ValueError("migration restore target is not empty")
            decrypt_bundle(
                artifact_path,
                plaintext,
                recipient_private_key=self._recipient_private_key,
                manifest=values,
                expected_context={
                    key: str(values[key])
                    for key in (
                        "task_id",
                        "target_id",
                        "source_epoch",
                        "artifact_id",
                        "subject_id",
                        "schema_version",
                        "artifact_format",
                    )
                },
            )
            restore_source = plaintext
            restore_values = {
                **values,
                "artifact_format": "sqlite",
                "byte_size": values["plaintext_size"],
                "artifact_sha256": values["plaintext_sha256"],
            }
        restore_path = self._restore_artifact(restore_source, restore_values, task_id=task_id)
        return RestoreReport(
            self.target_id,
            self.generation,
            receipt.artifact_id,
            receipt.manifest_digest,
            "restored",
            subject_id,
            tip,
            artifact_sha256,
            str(restore_path) if restore_path is not None else None,
        )

    def validate(
        self, report: RestoreReport | ReceiveReceipt, *, expected_digest: str | None = None
    ) -> TargetHealthReport:
        if isinstance(report, ReceiveReceipt):
            if expected_digest is not None and report.manifest_digest != expected_digest:
                raise ValueError("migration manifest digest mismatch")
            report = RestoreReport(
                self.target_id,
                self.generation,
                report.artifact_id,
                report.manifest_digest,
                "restored",
                artifact_sha256=report.artifact_sha256,
                restore_path=report.artifact_path,
            )
        if not isinstance(report, RestoreReport) or report.status != "restored":
            raise ValueError("migration restore report is invalid")
        if report.target_id != self.target_id or report.generation != self.generation:
            raise ValueError("migration restore target identity mismatch")
        if expected_digest is not None and report.manifest_digest != expected_digest:
            raise ValueError("migration manifest digest mismatch")
        if self.data_root is not None and not report.artifact_sha256:
            raise ValueError("migration restore report is missing artifact verification")
        checks = {
            "target_identity": report.target_id == self.target_id,
            "generation": report.generation == self.generation,
            "manifest": bool(re.fullmatch(r"[0-9a-f]{64}", report.manifest_digest)),
            "artifact_bytes": bool(report.artifact_sha256) or self.data_root is None,
            "host_binding": (
                self.data_root is None
                or bool(report.restore_path and self._restore_host_bound(report))
            ),
        }
        if report.restore_path:
            checks["database_quick_check"] = self._database_quick_check(Path(report.restore_path))
            if report.subject_id is not None:
                checks["subject_identity"] = self._database_subject_matches(
                    Path(report.restore_path), report.subject_id
                )
        if not all(checks.values()):
            raise ValueError("target health validation failed")
        return TargetHealthReport(
            self.target_id,
            self.generation,
            "healthy",
            report.manifest_digest,
            report.artifact_id,
            self.host_identity,
            checks,
        )

    def activate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Activate the installed service and return a target-signed receipt."""
        required = {
            "task_id",
            "subject_id",
            "target_id",
            "source_epoch",
            "manifest_digest",
            "artifact_id",
            "artifact_sha256",
            "health_report_digest",
            "source_fence_digest",
            "recipient_key_fingerprint",
            "target_volume_proof_digest",
            "credential_binding_digest",
            "signer_binding_digest",
            "wallet_mode",
            "wallet_proof_digest",
        }
        if set(request) != required or request.get("target_id") != self.target_id:
            raise ValueError("migration activation request is invalid")
        for key in (
            "manifest_digest",
            "artifact_sha256",
            "health_report_digest",
            "source_fence_digest",
            "recipient_key_fingerprint",
            "target_volume_proof_digest",
            "credential_binding_digest",
            "wallet_proof_digest",
        ):
            value = request.get(key)
            if value is None and key in {
                "signer_binding_digest",
                "wallet_proof_digest",
            }:
                continue
            if not isinstance(value, str) or not _HEX64.fullmatch(value):
                raise ValueError("migration activation digest is invalid")
        if request.get("signer_binding_digest") is not None and not _HEX64.fullmatch(
            str(request["signer_binding_digest"])
        ):
            raise ValueError("migration activation digest is invalid")
        if request.get("wallet_mode") not in {
            "external_signer_rebind",
            "local_wallet_transfer",
            "disabled",
        }:
            raise ValueError("migration activation wallet mode is invalid")
        for key in ("task_id", "source_epoch"):
            if not isinstance(request.get(key), str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}", str(request[key])
            ):
                raise ValueError("migration activation identity is invalid")
        if not isinstance(request.get("subject_id"), str) or not re.fullmatch(
            r"Noyra-[A-Za-z0-9_-]{1,120}", str(request["subject_id"])
        ):
            raise ValueError("migration activation identity is invalid")
        if not isinstance(request.get("artifact_id"), str) or not _ARTIFACT_ID.fullmatch(
            str(request["artifact_id"])
        ):
            raise ValueError("migration activation identity is invalid")
        if self.data_root is None:
            raise ValueError("migration activation storage is unavailable")
        self._require_encrypted_volume(self.data_root)
        if self._signing_key is None:
            raise ValueError("target signing identity is not configured")
        if self.activation_controller is None:
            raise ValueError("target runtime activation controller is unavailable")
        binding_path = self._binding_record_path(str(request["task_id"]))
        if not binding_path.is_file() or binding_path.is_symlink():
            raise ValueError("target binding proof is unavailable")
        binding = self._read_binding_record(binding_path)
        if any(
            binding.get(key) != request.get(key)
            for key in (
                "task_id",
                "subject_id",
                "target_id",
                "source_epoch",
                "manifest_digest",
                "artifact_id",
                "recipient_key_fingerprint",
            )
        ):
            raise ValueError("target binding proof context is invalid")
        credential_binding = binding.get("credential_binding")
        wallet_binding = binding.get("wallet_binding")
        volume_binding = binding.get("target_volume_proof")
        if not all(
            isinstance(value, Mapping)
            for value in (credential_binding, wallet_binding, volume_binding)
        ):
            raise ValueError("target binding proof is invalid")
        assert isinstance(credential_binding, Mapping)
        assert isinstance(wallet_binding, Mapping)
        assert isinstance(volume_binding, Mapping)
        if binding.get("recipient_key_fingerprint") != self.recipient_key_fingerprint:
            raise ValueError("target binding recipient key does not match")
        credential_digest = content_hash(
            {
                "task_id": request["task_id"],
                "manifest_digest": request["manifest_digest"],
                "binding": dict(credential_binding),
            }
        )
        volume_digest = content_hash(
            {
                "task_id": request["task_id"],
                "manifest_digest": request["manifest_digest"],
                "proof": dict(volume_binding),
            }
        )
        wallet_mode = request["wallet_mode"]
        wallet_digest = (
            content_hash(
                {
                    "task_id": request["task_id"],
                    "manifest_digest": request["manifest_digest"],
                    "binding": dict(wallet_binding),
                }
            )
            if wallet_mode != "disabled"
            else None
        )
        signer_digest = wallet_digest if wallet_mode == "external_signer_rebind" else None
        if (
            binding.get("status") != "verified"
            or binding.get("target_generation") != self.generation
            or binding.get("target_identity") != self.host_identity
            or credential_digest != request["credential_binding_digest"]
            or volume_digest != request["target_volume_proof_digest"]
            or signer_digest != request.get("signer_binding_digest")
            or wallet_digest != request.get("wallet_proof_digest")
            or wallet_binding.get("mode") != wallet_mode
        ):
            raise ValueError("target binding proof does not match activation")
        if wallet_mode == "local_wallet_transfer":
            expires_at = wallet_binding.get("expires_at")
            if not isinstance(expires_at, str):
                raise ValueError("target local wallet approval expiry is invalid")
            try:
                expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("target local wallet approval expiry is invalid") from error
            if expiry.tzinfo is None or expiry.astimezone(UTC) <= datetime.now(UTC):
                raise ValueError("target local wallet approval has expired")
        active = self.activation_controller.activate(dict(request))
        expected = {
            key: request[key]
            for key in (
                "task_id",
                "subject_id",
                "target_id",
                "source_epoch",
                "manifest_digest",
                "artifact_id",
                "artifact_sha256",
                "health_report_digest",
                "source_fence_digest",
                "recipient_key_fingerprint",
                "target_volume_proof_digest",
                "credential_binding_digest",
                "signer_binding_digest",
                "wallet_mode",
                "wallet_proof_digest",
            )
        }
        if (
            not isinstance(active, Mapping)
            or any(active.get(key) != value for key, value in expected.items())
            or active.get("status") != "active"
            or active.get("service_unit") != "noyra.service"
            or not isinstance(active.get("active_database_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(active["active_database_sha256"]))
        ):
            raise ValueError("target service activation proof is invalid")
        receipt = dict(active)
        receipt.pop("target_signature", None)
        receipt["target_signature"] = base64.urlsafe_b64encode(
            self._signing_key.sign(
                canonical_json(
                    {key: value for key, value in receipt.items() if key != "target_signature"}
                ).encode()
            )
        ).decode()
        activations = self.data_root / "activations"
        activations.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._assert_private_directory(activations)
        path = activations / f"{request['task_id']}.json"
        value = receipt
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValueError("migration activation record is invalid")
            if json.loads(path.read_text(encoding="utf-8")) != value:
                raise ValueError("migration activation conflicts with existing task")
        else:
            self._write_private_json(path, value)
        return value

    def deactivate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        required = {"task_id", "target_id", "source_epoch", "manifest_digest"}
        if set(request) != required or request.get("target_id") != self.target_id:
            raise ValueError("migration deactivation request is invalid")
        if self.data_root is None:
            raise ValueError("migration activation storage is unavailable")
        if self.activation_controller is None:
            raise ValueError("target runtime activation controller is unavailable")
        if not isinstance(request.get("task_id"), str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}", str(request["task_id"])
        ):
            raise ValueError("migration deactivation identity is invalid")
        result = self.activation_controller.deactivate(dict(request))
        if (
            not isinstance(result, Mapping)
            or result.get("status") not in {"deactivated", "inactive"}
            or result.get("task_id") != request["task_id"]
        ):
            raise ValueError("target service deactivation proof is invalid")
        path = self.data_root / "activations" / f"{request['task_id']}.json"
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValueError("migration activation record is invalid")
            path.unlink()
        return {"status": "deactivated", "task_id": request["task_id"]}

    def _read_manifest(self, receipt: ReceiveReceipt) -> dict[str, Any]:
        if self.data_root is None or receipt.manifest_path is None:
            raise ValueError("migration manifest is not persisted")
        path = Path(receipt.manifest_path)
        incoming = (self.data_root / "incoming").resolve()
        if path.is_symlink() or path.resolve().parent != incoming:
            raise ValueError("migration manifest path is outside the private root")
        if path.name != f"{receipt.artifact_id}.json":
            raise ValueError("migration manifest artifact path does not match receipt")
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("migration manifest cannot be read") from error
        if not isinstance(values, dict):
            raise ValueError("migration manifest is invalid")
        return cast(dict[str, Any], values)

    def _artifact_path(self, receipt: ReceiveReceipt, values: Mapping[str, Any]) -> Path:
        if self.data_root is None or receipt.artifact_path is None:
            raise ValueError("migration artifact is not persisted")
        path = Path(receipt.artifact_path)
        incoming = (self.data_root / "incoming").resolve()
        if path.is_symlink() or path.resolve().parent != incoming:
            raise ValueError("migration artifact path is outside the private root")
        if path.name != f"{receipt.artifact_id}.artifact":
            raise ValueError("migration artifact path does not match receipt")
        return path

    @staticmethod
    def _verify_artifact(path: Path, values: Mapping[str, Any]) -> str:
        try:
            stat_result = path.stat()
            if not path.is_file() or stat_result.st_size != values["byte_size"]:
                raise ValueError("migration artifact byte size mismatch")
            digest = _file_digest(path)
        except (OSError, ValueError) as error:
            raise ValueError("migration artifact cannot be read") from error
        expected = values.get("artifact_sha256")
        if expected is not None and digest != expected:
            raise ValueError("migration artifact digest mismatch")
        return digest

    def _restore_artifact(
        self,
        path: Path,
        values: Mapping[str, Any],
        *,
        task_id: str | None,
    ) -> Path | None:
        if self.restore_root is None:
            return None
        if not isinstance(task_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}", task_id
        ):
            raise ValueError("migration restore task identity is required")
        task_root = self.restore_root / task_id
        if task_root.exists() and (task_root.is_symlink() or not task_root.is_dir()):
            raise ValueError("migration restore task directory is invalid")
        task_root.mkdir(mode=0o700, parents=False, exist_ok=True)
        self._assert_private_directory(task_root)
        artifact_format = values.get("artifact_format", "noyra-encrypted-backup")
        if artifact_format == "noyra-encrypted-backup":
            if self.backup_manager is None:
                raise ValueError("encrypted backup restore is not configured")
            try:
                return Path(self.backup_manager.restore(path, task_root))
            except Exception as error:
                raise ValueError("encrypted backup restore failed") from error
        if artifact_format != "sqlite":
            raise ValueError("migration artifact format is unsupported")
        target = task_root / "noyra.sqlite3"
        temporary = task_root / ".noyra.sqlite3.restore"
        if target.exists() or target.is_symlink():
            raise ValueError("migration restore target is not empty")
        try:
            shutil.copyfile(path, temporary)
            connection = sqlite3.connect(temporary)
            try:
                result = connection.execute("PRAGMA quick_check").fetchone()
                if result is None or result[0] != "ok":
                    raise ValueError("restored database failed SQLite quick_check")
            finally:
                connection.close()
            temporary.replace(target)
            os.chmod(target, 0o600)
            return target
        except (OSError, sqlite3.DatabaseError, ValueError) as error:
            temporary.unlink(missing_ok=True)
            raise ValueError("SQLite restore failed") from error

    def _restore_host_bound(self, report: RestoreReport) -> bool:
        return (
            report.restore_path is not None
            and self.restore_root is not None
            and Path(report.restore_path).resolve().is_relative_to(self.restore_root)
        )

    def _require_encrypted_volume(self, path: Path) -> None:
        config = AtRestConfig.from_env(path)
        status = self._volume_probe.probe(
            path,
            backend=config.volume_backend,
            attestation_path=config.attestation_path,
        )
        if not status.encrypted:
            raise ValueError(f"migration encrypted volume requirement failed: {status.detail}")

    @staticmethod
    def _database_quick_check(path: Path) -> bool:
        try:
            with sqlite3.connect(path) as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()
            return result is not None and result[0] == "ok"
        except (OSError, sqlite3.DatabaseError):
            return False

    @staticmethod
    def _database_subject_matches(path: Path, subject_id: str) -> bool:
        try:
            with sqlite3.connect(path) as connection:
                row = connection.execute("SELECT subject_id FROM runtime_state LIMIT 1").fetchone()
            return row is not None and row[0] == subject_id
        except (OSError, sqlite3.DatabaseError):
            return False

    def _validate_manifest(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(manifest, Mapping) or len(manifest) > 32:
            raise ValueError("migration manifest is invalid")
        if any(self._contains_forbidden_key(key, value) for key, value in manifest.items()):
            raise ValueError("migration manifest contains a forbidden secret field")
        try:
            values = json.loads(canonical_json(dict(manifest)))
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("migration manifest contains unsupported data") from error
        if not isinstance(values, dict):
            raise ValueError("migration manifest is invalid")
        if len(canonical_json(values).encode()) > 1_000_000:
            raise ValueError("migration manifest is too large")
        artifact_id = values.get("artifact_id", "")
        byte_size = values.get("byte_size")
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
            raise ValueError("migration artifact path or id is invalid")
        if type(byte_size) is not int or not 0 <= byte_size <= 10 * 1024 * 1024 * 1024:
            raise ValueError("migration artifact metadata is invalid")
        artifact_sha256 = values.get("artifact_sha256")
        if artifact_sha256 is not None and (
            not isinstance(artifact_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256)
        ):
            raise ValueError("migration artifact digest is invalid")
        artifact_format = values.get("artifact_format", "noyra-encrypted-backup")
        if artifact_format == BUNDLE_FORMAT:
            from .bundle import _validate_manifest as validate_bundle_manifest

            try:
                values = validate_bundle_manifest(values)
            except ValueError as error:
                raise ValueError("migration recipient bundle manifest is invalid") from error
            if values.get("artifact_format") != BUNDLE_FORMAT:
                raise ValueError("migration recipient bundle format is invalid")
        elif artifact_format not in {"noyra-encrypted-backup", "sqlite"}:
            raise ValueError("migration artifact format is unsupported")
        if self._recipient_private_key is not None and artifact_format != BUNDLE_FORMAT:
            raise ValueError("recipient-encrypted migration bundle is required")
        return cast(dict[str, Any], values)

    @staticmethod
    def _contains_forbidden_key(key: Any, value: Any) -> bool:
        if not isinstance(key, str):
            return True
        normalized = key.casefold().replace("-", "_")
        if normalized in _FORBIDDEN or any(token in normalized for token in _FORBIDDEN):
            return True
        if isinstance(value, Mapping):
            return any(
                MigrationAgent._contains_forbidden_key(child, item) for child, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(MigrationAgent._contains_forbidden_key("item", item) for item in value)
        return False

    @staticmethod
    def _decode_public_key(value: str) -> bytes:
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, TypeError) as error:
            raise ValueError("agent public key is invalid") from error
        if len(decoded) != 32:
            raise ValueError("agent public key is invalid")
        return decoded

    @staticmethod
    def _decode_recipient_public_key(value: str) -> bytes:
        if not isinstance(value, str) or not value or "=" in value:
            raise ValueError("agent recipient public key is invalid")
        try:
            decoded = base64.b64decode(
                value.encode("ascii") + b"=" * (-len(value) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, TypeError, UnicodeError, binascii.Error) as error:
            raise ValueError("agent recipient public key is invalid") from error
        if (
            len(decoded) != 32
            or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value
        ):
            raise ValueError("agent recipient public key is invalid")
        return decoded

    @staticmethod
    def _prepare_root(data_root: Path | str | None) -> Path | None:
        if data_root is None:
            return None
        root = Path(data_root).expanduser()
        if root.exists() and root.is_symlink():
            raise ValueError("agent data root cannot be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError as error:
            raise ValueError("agent data root permissions cannot be secured") from error
        MigrationAgent._assert_private_directory(root)
        return root.resolve()

    @staticmethod
    def _assert_private_directory(path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise ValueError("agent data root must be a real directory")
        if os.name != "nt":
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                raise ValueError("agent data root must not be group or world accessible")

    @staticmethod
    def _write_private_bytes(path: Path, value: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            with suppress(OSError):
                os.close(descriptor)
            path.unlink(missing_ok=True)
            raise

    @classmethod
    def _write_private_json(cls, path: Path, value: Mapping[str, Any]) -> None:
        cls._write_private_bytes(path, canonical_json(dict(value)).encode("utf-8"))
