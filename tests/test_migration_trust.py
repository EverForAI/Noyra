from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from noyra.migration.trust import (
    RecipientPoPProof,
    TargetAttestation,
    TargetChallenge,
    create_recipient_pop_challenge,
    open_recipient_pop_challenge,
    verify_recipient_pop,
)


def test_attestation_accepts_signature_for_fresh_challenge() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    challenge = TargetChallenge(
        nonce="nonce-123",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        source_epoch="epoch-1",
    )
    signature = base64.urlsafe_b64encode(private_key.sign(challenge.signing_bytes())).decode(
        "ascii"
    )

    evidence = TargetAttestation(
        target_id="target-1",
        public_key=base64.urlsafe_b64encode(public_key.public_bytes_raw()).decode("ascii"),
        challenge=challenge,
        signature=signature,
    ).verify()

    assert evidence.target_id == "target-1"
    assert evidence.source_epoch == "epoch-1"


def test_attestation_rejects_bad_signature_and_expired_challenge() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    expired = TargetChallenge(
        nonce="nonce-123",
        expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        source_epoch="epoch-1",
    )
    signature = base64.urlsafe_b64encode(private_key.sign(expired.signing_bytes())).decode("ascii")
    attestation = TargetAttestation(
        target_id="target-1",
        public_key=base64.urlsafe_b64encode(public_key.public_bytes_raw()).decode("ascii"),
        challenge=expired,
        signature=signature,
    )
    with pytest.raises(ValueError, match="expired"):
        attestation.verify()

    fresh = TargetChallenge(
        nonce="nonce-456",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        source_epoch="epoch-1",
    )
    with pytest.raises(ValueError, match="signature"):
        TargetAttestation(
            target_id="target-1",
            public_key=base64.urlsafe_b64encode(public_key.public_bytes_raw()).decode("ascii"),
            challenge=fresh,
            signature=base64.urlsafe_b64encode(b"bad").decode("ascii"),
        ).verify()


def test_recipient_pop_is_encrypted_and_bound_to_recipient_key() -> None:
    target_signer = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate()
    challenge = create_recipient_pop_challenge(
        "target-1", recipient.public_key(), source_epoch="epoch-1"
    )
    unsigned = open_recipient_pop_challenge(challenge, recipient)
    signature = base64.urlsafe_b64encode(target_signer.sign(unsigned.signing_bytes())).decode(
        "ascii"
    )
    proof = RecipientPoPProof(**{**unsigned.to_dict(), "signature": signature})
    verify_recipient_pop(
        challenge,
        proof,
        target_public_key=base64.urlsafe_b64encode(
            target_signer.public_key().public_bytes_raw()
        ).decode("ascii"),
    )
    with pytest.raises(ValueError, match="key fingerprint"):
        open_recipient_pop_challenge(challenge, X25519PrivateKey.generate())


def test_recipient_pop_rejects_tampered_ciphertext() -> None:
    recipient = X25519PrivateKey.generate()
    challenge = create_recipient_pop_challenge(
        "target-1", recipient.public_key(), source_epoch="epoch-1"
    )
    tampered = type(challenge)(
        **{**challenge.to_dict(), "ciphertext": challenge.ciphertext[:-1] + "A"}
    )
    with pytest.raises(ValueError, match="authentication"):
        open_recipient_pop_challenge(tampered, recipient)
