from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from noyra.migration.bundle import decrypt_bundle, encrypt_bundle


def test_encrypted_migration_bundle_round_trips_to_enrolled_recipient(tmp_path: Path) -> None:
    recipient = X25519PrivateKey.generate()
    source = tmp_path / "source.sqlite"
    encrypted = tmp_path / "bundle.bin"
    restored = tmp_path / "restored.sqlite"
    source.write_bytes(b"consistent sqlite snapshot" * 17)
    context = {
        "task_id": "task-123",
        "target_id": "target-123",
        "source_epoch": "runtime-4",
    }

    manifest = encrypt_bundle(
        source,
        encrypted,
        recipient_public_key=recipient.public_key(),
        context=context,
    )

    decrypt_bundle(
        encrypted,
        restored,
        recipient_private_key=recipient,
        manifest=manifest,
        expected_context=context,
    )

    assert restored.read_bytes() == source.read_bytes()
    assert manifest["format"] == "noyra-migration-bundle/v1"
    assert (
        manifest["recipient_key_fingerprint"]
        == hashlib.sha256(recipient.public_key().public_bytes_raw()).hexdigest()
    )
    assert "private_key" not in manifest


@pytest.mark.parametrize("wrong_context", [{"task_id": "other"}, {"target_id": "other"}])
def test_encrypted_migration_bundle_rejects_changed_context_without_publishing(
    tmp_path: Path, wrong_context: dict[str, str]
) -> None:
    recipient = X25519PrivateKey.generate()
    source = tmp_path / "source.sqlite"
    encrypted = tmp_path / "bundle.bin"
    restored = tmp_path / "restored.sqlite"
    source.write_bytes(b"private migration data")
    context = {"task_id": "task-123", "target_id": "target-123", "source_epoch": "runtime-4"}
    manifest = encrypt_bundle(
        source,
        encrypted,
        recipient_public_key=recipient.public_key(),
        context=context,
    )

    with pytest.raises(ValueError, match="context"):
        decrypt_bundle(
            encrypted,
            restored,
            recipient_private_key=recipient,
            manifest=manifest,
            expected_context={**context, **wrong_context},
        )
    assert not restored.exists()


def test_encrypted_migration_bundle_rejects_wrong_recipient_and_tampering(
    tmp_path: Path,
) -> None:
    recipient = X25519PrivateKey.generate()
    source = tmp_path / "source.sqlite"
    encrypted = tmp_path / "bundle.bin"
    restored = tmp_path / "restored.sqlite"
    source.write_bytes(b"private migration data")
    context = {"task_id": "task-123", "target_id": "target-123", "source_epoch": "runtime-4"}
    manifest = encrypt_bundle(
        source,
        encrypted,
        recipient_public_key=recipient.public_key(),
        context=context,
    )

    with pytest.raises(ValueError, match="recipient"):
        decrypt_bundle(
            encrypted,
            restored,
            recipient_private_key=X25519PrivateKey.generate(),
            manifest=manifest,
            expected_context=context,
        )
    tampered = encrypted.read_bytes()
    encrypted.write_bytes(tampered[:-1] + bytes([tampered[-1] ^ 1]))
    with pytest.raises(ValueError, match=r"authentication|digest"):
        decrypt_bundle(
            encrypted,
            restored,
            recipient_private_key=recipient,
            manifest=manifest,
            expected_context=context,
        )
    assert not restored.exists()


def test_encrypt_does_not_publish_if_source_changes_between_digest_and_encryption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import noyra.migration.bundle as bundle_module

    recipient = X25519PrivateKey.generate()
    source = tmp_path / "source.sqlite"
    encrypted = tmp_path / "bundle.bin"
    source.write_bytes(b"original database snapshot")
    context = {"task_id": "task-123", "target_id": "target-123", "source_epoch": "runtime-4"}
    original_digest = bundle_module._file_digest

    def change_source_after_digest(path: Path) -> str:
        digest = original_digest(path)
        path.write_bytes(b"changed database snapshot")
        return digest

    monkeypatch.setattr(bundle_module, "_file_digest", change_source_after_digest)

    with pytest.raises(ValueError, match="source changed"):
        bundle_module.encrypt_bundle(
            source,
            encrypted,
            recipient_public_key=recipient.public_key(),
            context=context,
        )

    assert not encrypted.exists()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX directory modes")
def test_bundle_refuses_world_accessible_destination_parent(tmp_path: Path) -> None:
    recipient = X25519PrivateKey.generate()
    source = tmp_path / "source.sqlite"
    source.write_bytes(b"database snapshot")
    public_parent = tmp_path / "public"
    public_parent.mkdir(mode=0o755)
    public_parent.chmod(0o755)

    with pytest.raises(ValueError, match="private directory"):
        encrypt_bundle(
            source,
            public_parent / "bundle.bin",
            recipient_public_key=recipient.public_key(),
            context={"task_id": "task-123", "target_id": "target-123", "source_epoch": "runtime-4"},
        )

    assert not (public_parent / "bundle.bin").exists()


def test_bundle_rejects_noncanonical_base64url_manifest_fields(tmp_path: Path) -> None:
    recipient = X25519PrivateKey.generate()
    source = tmp_path / "source.sqlite"
    encrypted = tmp_path / "bundle.bin"
    source.write_bytes(b"database snapshot")
    context = {"task_id": "task-123", "target_id": "target-123", "source_epoch": "runtime-4"}
    manifest = encrypt_bundle(
        source,
        encrypted,
        recipient_public_key=recipient.public_key(),
        context=context,
    )
    # The final base64url character for a 32-byte value contains unused bits.
    # Changing those bits can decode to the same bytes unless canonical form is checked.
    encoded = manifest["ephemeral_public_key"]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last_index = alphabet.index(encoded[-1])
    manifest["ephemeral_public_key"] = encoded[:-1] + alphabet[(last_index & 0b110000) | 1]
    restored = tmp_path / "restored.sqlite"

    with pytest.raises(ValueError, match="ephemeral public key"):
        decrypt_bundle(
            encrypted,
            restored,
            recipient_private_key=recipient,
            manifest=manifest,
            expected_context=context,
        )
    assert not restored.exists()


def test_bundle_rejects_destination_parent_symlink(tmp_path: Path) -> None:
    recipient = X25519PrivateKey.generate()
    source = tmp_path / "source.sqlite"
    source.write_bytes(b"database snapshot")
    private_parent = tmp_path / "private"
    private_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(private_parent, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ValueError, match="symlink"):
        encrypt_bundle(
            source,
            linked_parent / "bundle.bin",
            recipient_public_key=recipient.public_key(),
            context={"task_id": "task-123", "target_id": "target-123", "source_epoch": "runtime-4"},
        )
    assert not (private_parent / "bundle.bin").exists()
