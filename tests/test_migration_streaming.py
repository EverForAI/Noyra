from __future__ import annotations

import io
import tracemalloc
from pathlib import Path
from typing import BinaryIO

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.core.types import content_hash
from noyra.migration.bundle import decrypt_bundle, encrypt_bundle_stream
from noyra.migration.subject_payload import create_payload, prepare_payload, restore_payload

CONTEXT = {
    "task_id": "synthetic-task",
    "target_id": "synthetic-target",
    "source_epoch": "synthetic-epoch",
}


def test_repeatable_payload_preserves_complete_v1_tree_and_rpc_bytes(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("synthetic-stream", content_hash("synthetic-stream"))
    (tmp_path / "workspace/空目录").mkdir(parents=True)
    (tmp_path / "workspace/证据.txt").write_text("synthetic UTF-8 data", encoding="utf-8")
    key = tmp_path / "synthetic-key.json"
    password = tmp_path / "synthetic-password"
    key.write_bytes(b"synthetic keystore")
    password.write_bytes(b"synthetic password")
    payload = prepare_payload(
        database.path,
        tmp_path,
        "synthetic-stream",
        local_wallet=(key, password, b'{"1":"https://rpc.example"}'),
    )
    first, second = io.BytesIO(), io.BytesIO()
    payload.write_to(first)
    payload.write_to(second)
    assert first.getvalue() == second.getvalue()
    file_tar = tmp_path / "compatibility.tar"
    inventory = create_payload(
        database.path,
        tmp_path,
        file_tar,
        "synthetic-stream",
        local_wallet=(key, password, b'{"1":"https://rpc.example"}'),
    )
    assert file_tar.read_bytes() == first.getvalue()
    assert inventory == payload.inventory
    restored = restore_payload(file_tar, tmp_path / "restored", "synthetic-stream")
    assert (restored.parent / "workspace/空目录").is_dir()
    assert (restored.parent / "workspace/证据.txt").read_text(
        encoding="utf-8"
    ) == "synthetic UTF-8 data"
    assert (
        restored.parent / "migration-wallet/rpc.json"
    ).read_bytes() == b'{"1":"https://rpc.example"}'


@pytest.mark.parametrize("change", ["same-size", "shorter", "longer"])
def test_changed_second_pass_is_never_published(tmp_path: Path, change: str) -> None:
    count = 0

    def emit(output: BinaryIO) -> None:
        nonlocal count
        count += 1
        output.write(
            b"original"
            if count == 1
            else {"same-size": b"modified", "shorter": b"short", "longer": b"longer new content"}[
                change
            ]
        )

    recipient = X25519PrivateKey.generate()
    with pytest.raises(ValueError, match="source changed"):
        encrypt_bundle_stream(
            emit,
            tmp_path / "artifact.bundle",
            recipient_public_key=recipient.public_key(),
            context=CONTEXT,
        )
    assert count == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("phase", [1, 2])
def test_emission_failure_leaves_no_published_or_partial_artifact(
    tmp_path: Path, phase: int
) -> None:
    count = 0

    def emit(output: BinaryIO) -> None:
        nonlocal count
        count += 1
        output.write(b"synthetic data")
        if count == phase:
            raise OSError("synthetic emission fault")

    with pytest.raises(OSError, match="synthetic emission fault"):
        encrypt_bundle_stream(
            emit,
            tmp_path / "artifact.bundle",
            recipient_public_key=X25519PrivateKey.generate().public_key(),
            context=CONTEXT,
        )
    assert count == phase
    assert list(tmp_path.iterdir()) == []


def test_measurement_enforces_existing_size_cap_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import noyra.migration.bundle as module

    monkeypatch.setattr(module, "_MAX_BUNDLE_BYTES", 1024)

    def emit(output: BinaryIO) -> None:
        output.write(b"x" * 1025)

    with pytest.raises(ValueError, match="size limit"):
        encrypt_bundle_stream(
            emit,
            tmp_path / "artifact.bundle",
            recipient_public_key=X25519PrivateKey.generate().public_key(),
            context=CONTEXT,
        )
    assert list(tmp_path.iterdir()) == []


def test_stream_round_trip_does_not_buffer_the_entire_payload(tmp_path: Path) -> None:
    recipient = X25519PrivateKey.generate()
    chunk = b"synthetic-streaming-data" * 4096
    parts = 400

    def emit(output: BinaryIO) -> None:
        for _ in range(parts):
            output.write(chunk)

    encrypted = tmp_path / "artifact.bundle"
    tracemalloc.start()
    try:
        manifest = encrypt_bundle_stream(
            emit, encrypted, recipient_public_key=recipient.public_key(), context=CONTEXT
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert manifest["plaintext_size"] == len(chunk) * parts
    assert peak < 16 * 1024 * 1024
    assert manifest["ciphertext_size"] == manifest["plaintext_size"] + 16
    restored = tmp_path / "restored"
    decrypt_bundle(
        encrypted,
        restored,
        recipient_private_key=recipient,
        manifest=manifest,
        expected_context=CONTEXT,
    )
    with restored.open("rb") as stream:
        for _ in range(parts):
            assert stream.read(len(chunk)) == chunk
        assert stream.read(1) == b""


def test_source_file_change_between_passes_aborts_the_encrypted_artifact(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entry = workspace / "evidence.txt"
    entry.write_bytes(b"original")
    plan = prepare_payload(database.path, tmp_path, "synthetic-stream")
    calls = 0

    def emit(output: BinaryIO) -> None:
        nonlocal calls
        calls += 1
        plan.write_to(output)
        if calls == 1:
            entry.write_bytes(b"modified")

    with pytest.raises(ValueError, match="files changed"):
        encrypt_bundle_stream(
            emit,
            tmp_path / "artifact.bundle",
            recipient_public_key=X25519PrivateKey.generate().public_key(),
            context=CONTEXT,
        )
    assert calls == 2
    assert not (tmp_path / "artifact.bundle").exists()
    assert not list(tmp_path.glob("*.tmp"))
