"""Restricted target-side migration agent."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core.types import canonical_json, content_hash

from .trust import TargetAttestation, TargetChallenge

_ARTIFACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_FORBIDDEN = frozenset(
    {"secret", "token", "password", "private_key", "api_key", "bearer", "credential"}
)
_AUTH_SCHEME = "Noyra-HMAC"
_AUTH_CLOCK_SKEW_SECONDS = 300
_AUTH_NONCE_TTL_SECONDS = 600
_AUTH_NONCE_LIMIT = 4096
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")


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


@dataclass(frozen=True)
class RestoreReport:
    target_id: str
    generation: int
    artifact_id: str
    manifest_digest: str
    status: str
    subject_id: str | None = None
    event_chain_tip: str | None = None


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
        data_root: Path | str | None = None,
        require_encrypted_storage: bool = True,
        session_token: str | None = None,
        max_incoming_bytes: int = 64 * 1024 * 1024,
        max_incoming_files: int = 128,
        artifact_ttl_seconds: int = 7 * 24 * 60 * 60,
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
        self.target_id = target_id
        self.key_fingerprint = key_fingerprint
        self.generation = generation
        self.public_key = public_key
        self._signing_key = signing_key
        self.require_encrypted_storage = require_encrypted_storage
        self._session_token = session_token
        self.max_incoming_bytes = max_incoming_bytes
        self.max_incoming_files = max_incoming_files
        self.artifact_ttl_seconds = artifact_ttl_seconds
        self._io_lock = threading.RLock()
        self.data_root = self._prepare_root(data_root)
        self.host_identity = hashlib.sha256(
            f"noyra-target-host-v1\n{target_id}\n{key_fingerprint}\n{generation}".encode()
        ).hexdigest()

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

    def sign_recovery_proof(self, signing_bytes: bytes) -> str:
        """Sign a source-provided recovery proof without exposing the key."""
        if self._signing_key is None:
            raise ValueError("target signing identity is not configured")
        if not isinstance(signing_bytes, bytes) or not signing_bytes:
            raise ValueError("recovery proof bytes are required")
        return base64.urlsafe_b64encode(self._signing_key.sign(signing_bytes)).decode()

    def receive(self, manifest: Mapping[str, Any]) -> ReceiveReceipt:
        values = self._validate_manifest(manifest)
        artifact_id = values["artifact_id"]
        digest = content_hash(values)
        manifest_path: str | None = None
        if self.data_root is not None:
            encoded = canonical_json(values).encode("utf-8")
            with self._io_lock:
                self.cleanup_expired()
                incoming = self.data_root / "incoming"
                incoming.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._assert_private_directory(incoming)
                file_count, byte_count = self._incoming_usage(incoming)
                if file_count >= self.max_incoming_files or (
                    byte_count + len(encoded) > self.max_incoming_bytes
                ):
                    raise ValueError("migration incoming quota exceeded")
                path = incoming / f"{artifact_id}.json"
                if path.exists() or path.is_symlink():
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
        return ReceiveReceipt(artifact_id, digest, values["byte_size"], "received", manifest_path)

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
        if not isinstance(body, bytes) or len(body) > 1_000_000:
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
        for path in incoming.iterdir():
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
        self, receipt: ReceiveReceipt, *, expected_digest: str | None = None
    ) -> RestoreReport:
        if not isinstance(receipt, ReceiveReceipt) or receipt.status != "received":
            raise ValueError("migration receipt is invalid")
        if expected_digest is not None and receipt.manifest_digest != expected_digest:
            raise ValueError("migration manifest digest mismatch")
        values = self._read_manifest(receipt)
        if content_hash(values) != receipt.manifest_digest:
            raise ValueError("migration manifest digest mismatch")
        subject_id = values.get("subject_id")
        tip = values.get("event_chain_tip")
        if subject_id is not None and (
            not isinstance(subject_id, str)
            or not re.fullmatch(r"Noyra-[A-Za-z0-9_-]{1,120}", subject_id)
        ):
            raise ValueError("migration subject identity is invalid")
        if tip is not None and (not isinstance(tip, str) or not re.fullmatch(r"[0-9a-f]{64}", tip)):
            raise ValueError("migration event-chain tip is invalid")
        return RestoreReport(
            self.target_id,
            self.generation,
            receipt.artifact_id,
            receipt.manifest_digest,
            "restored",
            subject_id,
            tip,
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
            )
        if not isinstance(report, RestoreReport) or report.status != "restored":
            raise ValueError("migration restore report is invalid")
        if report.target_id != self.target_id or report.generation != self.generation:
            raise ValueError("migration restore target identity mismatch")
        if expected_digest is not None and report.manifest_digest != expected_digest:
            raise ValueError("migration manifest digest mismatch")
        checks = {
            "target_identity": report.target_id == self.target_id,
            "generation": report.generation == self.generation,
            "manifest": bool(re.fullmatch(r"[0-9a-f]{64}", report.manifest_digest)),
            "host_binding": True,
        }
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

    @classmethod
    def _validate_manifest(cls, manifest: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(manifest, Mapping) or len(manifest) > 32:
            raise ValueError("migration manifest is invalid")
        if any(cls._contains_forbidden_key(key, value) for key, value in manifest.items()):
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
