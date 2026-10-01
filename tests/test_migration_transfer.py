from __future__ import annotations

import pytest

from noyra.migration.transfer import TransferSession


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
