from __future__ import annotations

import pytest

from noyra.migration.transfer import EncryptedTransferSession, TransferSession


def test_transfer_can_resume_and_verify_chunks(tmp_path) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"noyra" * 100_000)
    destination = tmp_path / "received.bin"
    session = TransferSession(chunk_bytes=4096)
    receipt = session.send(source, destination, stop_after_chunks=3)
    assert receipt.complete is False
    resumed = session.resume(receipt)
    session.verify(resumed)
    assert destination.read_bytes() == source.read_bytes()


def test_transfer_rejects_corrupt_chunk_and_unsafe_paths(tmp_path) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"safe payload")
    destination = tmp_path / "received.bin"
    session = TransferSession(chunk_bytes=4096)
    receipt = session.send(source, destination)
    receipt = receipt.__class__(**{**receipt.__dict__, "artifact_hash": "0" * 64})
    with pytest.raises(ValueError, match="hash"):
        session.verify(receipt)
    with pytest.raises(ValueError, match="destination"):
        session.send(source, tmp_path / ".." / "outside.bin")


def test_resume_rejects_changed_source_or_destination_prefix(tmp_path) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"noyra" * 100_000)
    destination = tmp_path / "received.bin"
    session = TransferSession(chunk_bytes=4096)
    receipt = session.send(source, destination, stop_after_chunks=3)
    source.write_bytes(b"changed" * 100_000)
    with pytest.raises(ValueError, match="prefix"):
        session.resume(receipt)


def test_transfer_rejects_symlink_source_and_destination(tmp_path) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"safe")
    source_link = tmp_path / "source-link"
    destination_link = tmp_path / "destination-link"
    try:
        source_link.symlink_to(source)
        destination_link.symlink_to(tmp_path / "new-file")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    session = TransferSession(chunk_bytes=4096)
    with pytest.raises(ValueError, match="symlink"):
        session.send(source_link, tmp_path / "received.bin")
    with pytest.raises(ValueError, match="symlink"):
        session.send(source, destination_link)


def test_encrypted_transfer_binds_manifest_and_round_trips(tmp_path) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"private migration payload" * 1000)
    encrypted = tmp_path / "artifact.enc"
    restored = tmp_path / "restored.bin"
    key = b"k" * 32
    session = EncryptedTransferSession(key, chunk_bytes=4096)
    receipt = session.send(source, encrypted, manifest_digest="a" * 64)
    assert encrypted.read_bytes() != source.read_bytes()
    session.receive(receipt, restored, manifest_digest="a" * 64)
    assert restored.read_bytes() == source.read_bytes()


def test_encrypted_transfer_rejects_wrong_key_or_manifest(tmp_path) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"payload")
    encrypted = tmp_path / "artifact.enc"
    restored = tmp_path / "restored.bin"
    receipt = EncryptedTransferSession(b"k" * 32, chunk_bytes=4096).send(
        source, encrypted, manifest_digest="b" * 64
    )
    with pytest.raises(ValueError, match="manifest"):
        EncryptedTransferSession(b"k" * 32, chunk_bytes=4096).receive(
            receipt, restored, manifest_digest="c" * 64
        )
    with pytest.raises(ValueError, match="decrypt"):
        EncryptedTransferSession(b"x" * 32, chunk_bytes=4096).receive(
            receipt, restored, manifest_digest="b" * 64
        )
