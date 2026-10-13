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
from noyra.core.deadline import monotonic_time
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.events import EventStore
from noyra.core.types import content_hash
from noyra.interaction.public_archive import PublicArchive
from noyra.interaction.public_cold_reader import PublicColdReader, _memory_limit


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
        assert monotonic_time() - started < 3
        after = {
            str(p.relative_to(tmp_path)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in (tmp_path / "subject").rglob("*")
        }
        assert after == before
    finally:
        kernel.close()
