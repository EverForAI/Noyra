from __future__ import annotations

import base64
import multiprocessing
import os
import struct
import time
from pathlib import Path
from typing import Any

import pytest

from noyra.core import SubjectKernel
from noyra.core.archive import ArchiveKeyring
from noyra.core.deadline import monotonic_time
from noyra.core.errors import ArchiveKeyUnavailableError
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.events import EventStore
from noyra.core.types import content_hash
from noyra.interaction import InteractionStore
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.public_archive import PublicArchive
from noyra.interaction.public_cold_reader import (
    PrivateArchiveReviewReader,
    PublicColdReader,
    _memory_limit,
    archive_keyring_revision,
)


def _blocked_worker(channel: Any, *args: Any) -> None:
    # A frame header without its body models a stalled physical read/IPC.
    if os.name == "posix":
        channel._send(struct.pack("!i", 100))
    time.sleep(60)


def _memory_probe(channel: Any) -> None:
    guard = _memory_limit(256 * 1024 * 1024)
    try:
        block = bytearray(1024 * 1024 * 1024)
        channel.send("unlimited")
        del block
    except MemoryError:
        channel.send("limited")
    finally:
        _ = guard
        channel.close()


def test_partial_ipc_timeout_kills_actual_child_and_releases_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import noyra.interaction.public_cold_reader as module

    monkeypatch.setattr(module, "_worker", _blocked_worker)
    started = monotonic_time()
    reader = PublicColdReader(tmp_path / "db.sqlite3", "subject", ["subject"], [], started + 1.5)
    process = reader.process
    releases = []
    try:
        with pytest.raises(TimeoutError):
            reader.prepare()
        assert monotonic_time() - started < 2.5
    finally:
        reader.close(lambda: releases.append(True))
    reader.close(lambda: releases.append(False))
    assert releases == [True]
    with pytest.raises(ValueError, match="closed"):
        process.is_alive()


def test_os_memory_guard_rejects_oversized_allocation() -> None:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_memory_probe, args=(child,))
    try:
        process.start()
        child.close()
        assert parent.poll(10)
        assert parent.recv() == "limited"
        process.join(timeout=5)
        assert process.exitcode == 0
    finally:
        parent.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()


def test_large_private_cold_payload_returns_only_approved_states_and_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.interaction import public_archive as module

    assert module.MAX_PUBLIC_QUERY_SECONDS == 2.0
    # Content isolation must hold with coverage and a slow spawned interpreter.
    # The blocked-IPC test above independently enforces the physical deadline.
    monkeypatch.setattr(module, "MAX_PUBLIC_QUERY_SECONDS", 10.0)
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"W" * 32).decode())
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID", "public-cold-isolation")
    kernel = SubjectKernel(tmp_path / "db.sqlite3", "cold-resource-test", content_hash("cold"))
    try:
        kernel.boot()
        kernel.orient()
        kernel.activate()
        store = EventStore(kernel.database)
        for index in range(8):
            store.append(
                kernel.subject_id,
                "private_test",
                "test",
                {"private": "DO_NOT_PROJECT" * 100_000, "index": index},
                occurred_at="2020-01-01T00:00:00+00:00",
            )
        public = store.append(
            kernel.subject_id,
            "lifecycle_transition",
            "test",
            {"from": "active", "to": "paused", "private": "HIDDEN"},
            privacy_level="public_status",
            occurred_at="2020-01-01T00:00:01+00:00",
        )
        archive = EventPayloadArchive(
            kernel.database, tmp_path / "subject" / "cold", subject_id=kernel.subject_id
        )
        archive.archive_cold(kernel.subject_id, older_than_days=30)
        before = {
            str(p.relative_to(tmp_path)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in (tmp_path / "subject").rglob("*")
        }
        started = monotonic_time()
        result = PublicArchive(kernel.database).page(
            kernel.subject_id, kind="lifecycle", identifier=public.event_id
        )
        assert result["items"][0]["details"] == {"from": "active", "to": "paused"}
        assert "HIDDEN" not in str(result) and "DO_NOT_PROJECT" not in str(result)
        assert monotonic_time() - started < 11
        after = {
            str(p.relative_to(tmp_path)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in (tmp_path / "subject").rglob("*")
        }
        assert after == before
    finally:
        kernel.close()


def test_private_cold_reader_returns_only_allowlisted_references_without_key_ipc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"P" * 32).decode())
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID", "private-review-isolation")
    kernel = SubjectKernel(
        tmp_path / "private.sqlite3", "private-cold-review", content_hash("cold")
    )
    try:
        kernel.boot()
        kernel.orient()
        kernel.activate()
        window = ConversationStore(kernel.database).create(kernel.subject_id, "PRIVATE-TITLE")
        message = InteractionStore(kernel.database).receive(
            kernel.subject_id,
            "web",
            "web-user",
            "ARCHIVED-PRIVATE-BODY-MUST-NOT-LEAVE-WORKER",
            conversation_id=window["conversation_id"],
        )
        event = EventStore(kernel.database).append(
            kernel.subject_id,
            "interaction_received",
            "human",
            {"interaction_id": message.interaction_id, "private": "ARCHIVED-PRIVATE-BODY"},
            occurred_at="2020-01-01T00:00:00.000+00:00",
        )
        archive = EventPayloadArchive(
            kernel.database, tmp_path / "subject" / "cold", subject_id=kernel.subject_id
        )
        assert archive.archive_cold(kernel.subject_id, older_than_days=30) >= 1
        with kernel.database.read_transaction() as connection:
            row = connection.execute(
                "SELECT e.payload_archive_key,e.payload_hash,e.event_type,s.* "
                "FROM events e JOIN event_payload_segments s "
                "ON s.subject_id=e.subject_id AND s.object_key=e.payload_archive_key "
                "WHERE e.event_id=?",
                (event.event_id,),
            ).fetchone()
            assert row is not None
            metadata = {
                name: row[name]
                for name in (
                    "event_count",
                    "compressed_hash",
                    "archive_format",
                    "encryption_key_id",
                    "encryption_key_fingerprint",
                )
            }
            before = list(connection.iterdump())
            identities = [
                item[0] for item in connection.execute("SELECT subject_id FROM subject_identity")
            ]
            keys = [
                tuple(item)
                for item in connection.execute(
                    "SELECT subject_id,storage_key FROM subject_storage_keys"
                )
            ]
        subject_directory = archive.root.parent
        before_files = {
            str(path.relative_to(tmp_path)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in subject_directory.rglob("*")
            if path.is_file()
        }
        deadline = monotonic_time() + 30
        keyring = ArchiveKeyring.from_env()
        keyring.validate_revision(kernel.database, kernel.subject_id)
        revision = archive_keyring_revision(keyring)
        reader = PrivateArchiveReviewReader(
            kernel.database.path,
            kernel.subject_id,
            identities,
            keys,
            deadline,
            expected_keyring_revision=revision,
        )
        try:
            reader.prepare()
            references, size = reader.load_references(
                row["payload_archive_key"],
                metadata,
                16_000_000,
                {event.event_id: (event.payload_hash, "interaction_received")},
            )
            assert references == {event.event_id: {"interaction_id": message.interaction_id}}
            assert 0 < size <= 16_000_000
            assert not hasattr(reader, "load")
            assert not hasattr(PublicColdReader, "load_references")
        finally:
            reader.close(lambda: None)

        # A keyring change before process startup cannot pass the parent's pin.
        monkeypatch.setenv(
            "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"Q" * 32).decode()
        )
        key_reader = PrivateArchiveReviewReader(
            kernel.database.path,
            kernel.subject_id,
            identities,
            keys,
            monotonic_time() + 30,
            expected_keyring_revision=revision,
        )
        try:
            with pytest.raises(ArchiveKeyUnavailableError):
                key_reader.prepare()
        finally:
            key_reader.close(lambda: None)
        with kernel.database.read_transaction() as connection:
            assert list(connection.iterdump()) == before
        after_files = {
            str(path.relative_to(tmp_path)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in subject_directory.rglob("*")
            if path.is_file()
        }
        assert after_files == before_files
    finally:
        kernel.close()
