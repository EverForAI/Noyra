"""Cryptographic enrollment evidence for migration targets."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from noyra.core.types import canonical_json


def normalize_endpoint(endpoint: str) -> str:
    """Return the canonical HTTPS endpoint covered by enrollment evidence."""
    if not isinstance(endpoint, str) or len(endpoint) > 512:
        raise ValueError("target endpoint is invalid")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("target endpoint must be HTTPS")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("target endpoint port is invalid") from error
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = f"[{host}]"
    port_part = f":{port}" if port is not None and port != 443 else ""
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    return f"https://{host}{port_part}{path}"


def _decode(value: str, label: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    raw = value.strip()
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError) as error:
        raise ValueError(f"{label} is invalid") from error
    if len(decoded) != 32:
        raise ValueError(f"{label} must contain a 32-byte Ed25519 key")
    return decoded


def _parse_expiry(value: str) -> datetime:
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError("challenge expiry is invalid") from error
    if expiry.tzinfo is None:
        raise ValueError("challenge expiry must include timezone")
    return expiry.astimezone(UTC)


@dataclass(frozen=True)
class TargetChallenge:
    nonce: str
    expires_at: str
    source_epoch: str
    target_id: str | None = None
    endpoint_origin: str | None = None

    def __post_init__(self) -> None:
        if self.target_id is not None and (
            not isinstance(self.target_id, str) or not self.target_id
        ):
            raise ValueError("challenge target id is invalid")
        if self.endpoint_origin is not None and (
            not isinstance(self.endpoint_origin, str) or not self.endpoint_origin
        ):
            raise ValueError("challenge endpoint origin is invalid")

    def signing_bytes(self) -> bytes:
        if not self.nonce or len(self.nonce) > 256 or not self.source_epoch:
            raise ValueError("challenge fields are invalid")
        target_id = self.target_id or ""
        endpoint_origin = self.endpoint_origin or ""
        if self.target_id is None and self.endpoint_origin is None:
            return (
                f"noyra-target-attestation-v1\n{self.nonce}\n{self.expires_at}\n{self.source_epoch}"
            ).encode()
        return (
            "noyra-target-attestation-v2\n"
            f"{target_id}\n{endpoint_origin}\n{self.nonce}\n{self.expires_at}\n{self.source_epoch}"
        ).encode()

    def ensure_fresh(self, *, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        expiry = _parse_expiry(self.expires_at)
        if expiry <= current:
            raise ValueError("challenge is expired")
        if (expiry - current).total_seconds() > 900:
            raise ValueError("challenge expiry window is too long")


@dataclass(frozen=True)
class TrustEvidence:
    target_id: str
    key_fingerprint: str
    source_epoch: str
    verified_at: str


@dataclass(frozen=True)
class RecipientPoPChallenge:
    """One-time encrypted proof-of-possession challenge for a target."""

    target_id: str
    source_epoch: str
    expires_at: str
    pop_nonce: str
    recipient_key_fingerprint: str
    ephemeral_public_key: str
    salt: str
    nonce: str
    ciphertext: str

    def to_dict(self) -> dict[str, str]:
        return {
            "target_id": self.target_id,
            "source_epoch": self.source_epoch,
            "expires_at": self.expires_at,
            "pop_nonce": self.pop_nonce,
            "recipient_key_fingerprint": self.recipient_key_fingerprint,
            "ephemeral_public_key": self.ephemeral_public_key,
            "salt": self.salt,
            "nonce": self.nonce,
            "ciphertext": self.ciphertext,
        }


@dataclass(frozen=True)
class RecipientPoPProof:
    target_id: str
    source_epoch: str
    expires_at: str
    pop_nonce: str
    recipient_key_fingerprint: str
    signature: str

    def signing_bytes(self) -> bytes:
        values = {
            "target_id": self.target_id,
            "source_epoch": self.source_epoch,
            "expires_at": self.expires_at,
            "pop_nonce": self.pop_nonce,
            "recipient_key_fingerprint": self.recipient_key_fingerprint,
        }
        return b"noyra-recipient-pop-v1\n" + canonical_json(values).encode("utf-8")

    def to_dict(self) -> dict[str, str]:
        return {**self.__dict__}


def create_recipient_pop_challenge(
    target_id: str,
    recipient_public_key: X25519PublicKey,
    *,
    source_epoch: str,
    ttl_seconds: int = 300,
) -> RecipientPoPChallenge:
    """Encrypt a fresh challenge so only the enrolled recipient key can read it."""
    if not isinstance(target_id, str) or not 3 <= len(target_id) <= 128:
        raise ValueError("recipient proof target id is invalid")
    if not isinstance(source_epoch, str) or not source_epoch or len(source_epoch) > 128:
        raise ValueError("recipient proof source epoch is invalid")
    if not isinstance(recipient_public_key, X25519PublicKey):
        raise ValueError("recipient public key is invalid")
    if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= 900:
        raise ValueError("recipient proof expiry is invalid")
    expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(
        timespec="milliseconds"
    )
    pop_nonce = secrets.token_urlsafe(32)
    recipient_raw = recipient_public_key.public_bytes_raw()
    fingerprint = hashlib.sha256(recipient_raw).hexdigest()
    ephemeral = X25519PrivateKey.generate()
    salt = os.urandom(32)
    nonce = os.urandom(12)
    payload = {
        "target_id": target_id,
        "source_epoch": source_epoch,
        "expires_at": expires_at,
        "pop_nonce": pop_nonce,
        "recipient_key_fingerprint": fingerprint,
    }
    key = _pop_key(ephemeral.exchange(recipient_public_key), salt, target_id, source_epoch)
    ciphertext = AESGCM(key).encrypt(nonce, canonical_json(payload).encode("utf-8"), None)
    return RecipientPoPChallenge(
        target_id,
        source_epoch,
        expires_at,
        pop_nonce,
        fingerprint,
        _encode(ephemeral.public_key().public_bytes_raw()),
        _encode(salt),
        _encode(nonce),
        _encode(ciphertext),
    )


def open_recipient_pop_challenge(
    challenge: RecipientPoPChallenge | Mapping[str, Any],
    recipient_private_key: X25519PrivateKey,
) -> RecipientPoPProof:
    """Decrypt and validate a challenge on the target before signing it."""
    if isinstance(challenge, Mapping):
        try:
            challenge = RecipientPoPChallenge(
                target_id=str(challenge["target_id"]),
                source_epoch=str(challenge["source_epoch"]),
                expires_at=str(challenge["expires_at"]),
                pop_nonce=str(challenge["pop_nonce"]),
                recipient_key_fingerprint=str(challenge["recipient_key_fingerprint"]),
                ephemeral_public_key=str(challenge["ephemeral_public_key"]),
                salt=str(challenge["salt"]),
                nonce=str(challenge["nonce"]),
                ciphertext=str(challenge["ciphertext"]),
            )
        except (KeyError, TypeError) as error:
            raise ValueError("recipient proof challenge is invalid") from error
    if not isinstance(challenge, RecipientPoPChallenge) or not isinstance(
        recipient_private_key, X25519PrivateKey
    ):
        raise ValueError("recipient proof challenge is invalid")
    expiry = _parse_expiry(challenge.expires_at)
    if expiry <= datetime.now(UTC):
        raise ValueError("recipient proof challenge is expired")
    recipient_fingerprint = hashlib.sha256(
        recipient_private_key.public_key().public_bytes_raw()
    ).hexdigest()
    if recipient_fingerprint != challenge.recipient_key_fingerprint:
        raise ValueError("recipient proof key fingerprint mismatch")
    try:
        ephemeral = X25519PublicKey.from_public_bytes(
            _decode_fixed(challenge.ephemeral_public_key, 32)
        )
        salt = _decode_fixed(challenge.salt, 32)
        nonce = _decode_fixed(challenge.nonce, 12)
        ciphertext = _decode_any(challenge.ciphertext)
        key = _pop_key(
            recipient_private_key.exchange(ephemeral),
            salt,
            challenge.target_id,
            challenge.source_epoch,
        )
        payload = json.loads(AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8"))
    except (ValueError, TypeError, InvalidTag, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("recipient proof challenge authentication failed") from error
    if payload != {
        "target_id": challenge.target_id,
        "source_epoch": challenge.source_epoch,
        "expires_at": challenge.expires_at,
        "pop_nonce": challenge.pop_nonce,
        "recipient_key_fingerprint": challenge.recipient_key_fingerprint,
    }:
        raise ValueError("recipient proof challenge binding mismatch")
    return RecipientPoPProof(
        challenge.target_id,
        challenge.source_epoch,
        challenge.expires_at,
        challenge.pop_nonce,
        challenge.recipient_key_fingerprint,
        "",
    )


def verify_recipient_pop(
    challenge: RecipientPoPChallenge,
    proof: RecipientPoPProof | Mapping[str, Any],
    *,
    target_public_key: str,
) -> None:
    if isinstance(proof, Mapping):
        try:
            proof = RecipientPoPProof(
                target_id=str(proof["target_id"]),
                source_epoch=str(proof["source_epoch"]),
                expires_at=str(proof["expires_at"]),
                pop_nonce=str(proof["pop_nonce"]),
                recipient_key_fingerprint=str(proof["recipient_key_fingerprint"]),
                signature=str(proof["signature"]),
            )
        except (KeyError, TypeError) as error:
            raise ValueError("recipient proof is invalid") from error
    if not isinstance(proof, RecipientPoPProof) or not proof.signature:
        raise ValueError("recipient proof is invalid")
    if proof.to_dict() and any(
        getattr(proof, name) != getattr(challenge, name)
        for name in (
            "target_id",
            "source_epoch",
            "expires_at",
            "pop_nonce",
            "recipient_key_fingerprint",
        )
    ):
        raise ValueError("recipient proof binding mismatch")
    if _parse_expiry(proof.expires_at) <= datetime.now(UTC):
        raise ValueError("recipient proof is expired")
    try:
        public_bytes = _decode_any(target_public_key)
        if len(public_bytes) != 32:
            raise ValueError("target public key is invalid")
        public = Ed25519PublicKey.from_public_bytes(public_bytes)
        public.verify(_decode_any(proof.signature), proof.signing_bytes())
    except (ValueError, TypeError, InvalidSignature) as error:
        raise ValueError("recipient proof signature is invalid") from error


def _pop_key(shared: bytes, salt: bytes, target_id: str, source_epoch: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"noyra-recipient-pop-v1\n" + f"{target_id}\n{source_epoch}".encode(),
    ).derive(shared)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_fixed(value: str, length: int) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise ValueError("recipient proof encoding is invalid")
    try:
        raw = base64.b64decode(
            value.encode("ascii") + b"=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError, UnicodeError, binascii.Error) as error:
        raise ValueError("recipient proof encoding is invalid") from error
    if len(raw) != length or _encode(raw) != value:
        raise ValueError("recipient proof encoding is invalid")
    return raw


def _decode_any(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("recipient proof encoding is invalid")
    raw_value = value.rstrip("=")
    try:
        raw = base64.b64decode(
            raw_value.encode("ascii") + b"=" * (-len(raw_value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError, UnicodeError, binascii.Error) as error:
        raise ValueError("recipient proof encoding is invalid") from error
    if _encode(raw) != raw_value:
        raise ValueError("recipient proof encoding is invalid")
    return raw


@dataclass(frozen=True)
class TargetAttestation:
    target_id: str
    public_key: str
    challenge: TargetChallenge
    signature: str

    def verify(self) -> TrustEvidence:
        self.challenge.ensure_fresh()
        if not self.target_id or len(self.target_id) > 128:
            raise ValueError("target id is invalid")
        public_bytes = _decode(self.public_key, "target public key")
        try:
            signature = base64.urlsafe_b64decode(self.signature + "=" * (-len(self.signature) % 4))
            Ed25519PublicKey.from_public_bytes(public_bytes).verify(
                signature, self.challenge.signing_bytes()
            )
        except (ValueError, TypeError, InvalidSignature) as error:
            raise ValueError("target signature is invalid") from error
        import hashlib

        return TrustEvidence(
            target_id=self.target_id,
            key_fingerprint=hashlib.sha256(public_bytes).hexdigest(),
            source_epoch=self.challenge.source_epoch,
            verified_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        )


@dataclass(frozen=True)
class TrustDecision:
    accepted: bool
    reasons: tuple[str, ...] = ()


def evaluate_target(
    candidate: Any,
    *,
    allowed_target_ids: tuple[str, ...] = (),
    allowed_regions: tuple[str, ...] = (),
    min_free_bytes: int = 0,
    required_release_sha: str | None = None,
    minimum_trust_level: int = 1,
) -> TrustDecision:
    """Apply deterministic hard gates; cognition cannot override these reasons."""
    reasons: list[str] = []
    if getattr(candidate, "status", None) != "active":
        reasons.append("target_not_active")
    if allowed_target_ids and getattr(candidate, "target_id", None) not in allowed_target_ids:
        reasons.append("target_not_allowlisted")
    if allowed_regions and getattr(candidate, "region", None) not in allowed_regions:
        reasons.append("region_not_allowlisted")
    if not bool(getattr(candidate, "encrypted_volume", False)):
        reasons.append("target_volume_not_encrypted")
    if int(getattr(candidate, "free_bytes", 0)) < min_free_bytes:
        reasons.append("target_storage_insufficient")
    if required_release_sha and getattr(candidate, "release_sha", None) != required_release_sha:
        reasons.append("release_incompatible")
    if int(getattr(candidate, "trust_level", 0)) < minimum_trust_level:
        reasons.append("trust_level_insufficient")
    return TrustDecision(accepted=not reasons, reasons=tuple(reasons))
