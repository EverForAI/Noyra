"""Cryptographic enrollment evidence for migration targets."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


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

    def signing_bytes(self) -> bytes:
        if not self.nonce or len(self.nonce) > 256 or not self.source_epoch:
            raise ValueError("challenge fields are invalid")
        return (
            f"noyra-target-attestation-v1\n{self.nonce}\n{self.expires_at}\n{self.source_epoch}"
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
