from __future__ import annotations

import base64
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.migration.policy import MigrationStore
from noyra.migration.targets import TargetRegistry


def _registry(tmp_path: Any) -> Any:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-0001", "d" * 64)
    return database, TargetRegistry(database, MigrationStore(database))


def _key_material() -> Any:
    private = Ed25519PrivateKey.generate()
    public = (
        base64.urlsafe_b64encode(private.public_key().public_bytes_raw())
        .decode("ascii")
        .rstrip("=")
    )
    return private, public


def _recipient_material() -> Any:
    private = X25519PrivateKey.generate()
    public = (
        base64.urlsafe_b64encode(private.public_key().public_bytes_raw())
        .decode("ascii")
        .rstrip("=")
    )
    return private, public


def test_register_challenge_and_consume_target(tmp_path: Any) -> None:
    _, registry = _registry(tmp_path)
    private, public = _key_material()
    _, recipient_public = _recipient_material()
    target = registry.register(
        "Noyra-0001",
        target_id="target-1",
        public_key=public,
        recipient_public_key=recipient_public,
        endpoint="https://target.example/migration",
        capabilities={"encrypted_restore": True},
        region="us-east",
        provider="example",
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    challenge = registry.issue_challenge(target.target_id, source_epoch="epoch-1")
    signature = base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode("ascii")
    evidence = registry.attest(target.target_id, challenge, signature, actor="operator")
    assert evidence.target_id == "target-1"
    with pytest.raises(ValueError, match="nonce"):
        registry.attest(target.target_id, challenge, signature, actor="operator")


def test_unencrypted_or_non_https_target_is_rejected(tmp_path: Any) -> None:
    _, registry = _registry(tmp_path)
    _, public = _key_material()
    _, recipient_public = _recipient_material()
    with pytest.raises(ValueError, match="HTTPS"):
        registry.register(
            "Noyra-0001",
            target_id="bad",
            public_key=public,
            recipient_public_key=recipient_public,
            endpoint="http://target.example",
            capabilities={},
            region=None,
            provider=None,
            release_sha="b" * 40,
            os_arch="linux-amd64",
            encrypted_volume=True,
            actor="operator",
        )
    with pytest.raises(ValueError, match="encrypted"):
        registry.register(
            "Noyra-0001",
            target_id="bad2",
            public_key=public,
            recipient_public_key=recipient_public,
            endpoint="https://target.example",
            capabilities={},
            region=None,
            provider=None,
            release_sha="b" * 40,
            os_arch="linux-amd64",
            encrypted_volume=False,
            actor="operator",
        )


def test_revoked_target_cannot_be_attested(tmp_path: Any) -> None:
    _, registry = _registry(tmp_path)
    private, public = _key_material()
    _, recipient_public = _recipient_material()
    target = registry.register(
        "Noyra-0001",
        target_id="target-1",
        public_key=public,
        recipient_public_key=recipient_public,
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="c" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    challenge = registry.issue_challenge(target.target_id, source_epoch="epoch-1")
    signature = base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode("ascii")
    registry.revoke(target.target_id, reason="retired", actor="operator")
    with pytest.raises(ValueError, match="revoked"):
        registry.attest(target.target_id, challenge, signature, actor="operator")


def test_target_attestation_binds_endpoint_origin(tmp_path: Any) -> None:
    _, registry = _registry(tmp_path)
    private, public = _key_material()
    _, recipient_public = _recipient_material()
    target = registry.register(
        "Noyra-0001",
        target_id="target-endpoint-binding",
        public_key=public,
        recipient_public_key=recipient_public,
        endpoint="https://target.example/migration",
        capabilities={},
        region=None,
        provider=None,
        release_sha="e" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    issued = registry.issue_challenge(target.target_id, source_epoch="epoch-1")
    tampered = type(issued)(
        nonce=issued.nonce,
        expires_at=issued.expires_at,
        source_epoch=issued.source_epoch,
        target_id=issued.target_id,
        endpoint_origin="https://attacker.example/migration",
    )
    signature = base64.urlsafe_b64encode(private.sign(tampered.signing_bytes())).decode("ascii")
    with pytest.raises(ValueError, match="endpoint binding"):
        registry.attest(target.target_id, tampered, signature, actor="operator")


def test_target_attestation_rejects_legacy_unbound_challenge(tmp_path: Any) -> None:
    _, registry = _registry(tmp_path)
    private, public = _key_material()
    _, recipient_public = _recipient_material()
    target = registry.register(
        "Noyra-0001",
        target_id="target-bound-challenge",
        public_key=public,
        recipient_public_key=recipient_public,
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="f" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    issued = registry.issue_challenge(target.target_id, source_epoch="epoch-1")
    unbound = type(issued)(
        nonce=issued.nonce,
        expires_at=issued.expires_at,
        source_epoch=issued.source_epoch,
    )
    signature = base64.urlsafe_b64encode(private.sign(unbound.signing_bytes())).decode("ascii")
    with pytest.raises(ValueError, match="target identity"):
        registry.attest(target.target_id, unbound, signature, actor="operator")


def test_target_integrity_detects_key_and_revocation_tampering(tmp_path: Any) -> None:
    database, registry = _registry(tmp_path)
    _, public = _key_material()
    _, recipient_public = _recipient_material()
    target = registry.register(
        "Noyra-0001",
        target_id="target-integrity",
        public_key=public,
        recipient_public_key=recipient_public,
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="d" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    registry.assert_integrity(target.target_id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE migration_targets SET public_key=? WHERE target_id=?",
            ("tampered", target.target_id),
        )
    with pytest.raises(ValueError, match="integrity"):
        registry.assert_integrity(target.target_id)


def test_recipient_key_is_required_for_migration_registration(tmp_path: Any) -> None:
    _, registry = _registry(tmp_path)
    _, public = _key_material()
    with pytest.raises(ValueError, match="recipient public key"):
        registry.register(
            "Noyra-0001",
            target_id="target-without-recipient",
            public_key=public,
            recipient_public_key="",
            endpoint="https://target.example",
            capabilities={},
            region=None,
            provider=None,
            release_sha="e" * 40,
            os_arch="linux-amd64",
            encrypted_volume=True,
            actor="operator",
        )


def test_recipient_key_is_bound_to_target_integrity(tmp_path: Any) -> None:
    database, registry = _registry(tmp_path)
    _, public = _key_material()
    _, recipient_public = _recipient_material()
    target = registry.register(
        "Noyra-0001",
        target_id="target-recipient-integrity",
        public_key=public,
        recipient_public_key=recipient_public,
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="f" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    assert target.recipient_public_key == recipient_public
    assert len(target.recipient_key_fingerprint) == 64
    with database.transaction() as connection:
        connection.execute(
            "UPDATE migration_targets SET recipient_public_key=? WHERE target_id=?",
            (
                recipient_public[:-1] + ("A" if recipient_public[-1] != "A" else "B"),
                target.target_id,
            ),
        )
    with pytest.raises(ValueError, match="integrity"):
        registry.assert_integrity(target.target_id)
