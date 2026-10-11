from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from noyra.core.admission import OperationInvalidated, RuntimeAdmissionGate, bind_lease
from noyra.core.types import content_hash
from noyra.knowledge import CommonKnowledgePeerInput
from noyra.knowledge.common import CommonKnowledgeSyncResult
from noyra.knowledge.sync import CommonKnowledgeHTTPClient, CommonKnowledgeSyncError
from test_m42_p3_02_common_knowledge_sync import _OfflineClient, _proposal, _PublisherClient, _store


@pytest.mark.parametrize("stage", ["discovery", "feed", "verify", "between_pages"])
@pytest.mark.parametrize("withdraw", ["lease", "key", "peer"])
def test_sync_withdrawal_at_each_wait_preserves_cursor_and_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    withdraw: str,
) -> None:
    pk, publisher = _store(tmp_path, "publisher")
    rk, reader = _store(tmp_path, "reader")
    gate = RuntimeAdmissionGate(reader.subject_id)
    publisher.publish(_proposal("one"))
    publisher.publish(_proposal("two"))
    peer = reader.register_peer(
        CommonKnowledgePeerInput(
            label="peer",
            endpoint="https://example.com",
            publisher_subject_id=publisher.subject_id,
            public_key=publisher.public_key,
        ),
        actor="operator",
    )
    feeds: list[int] = []

    def invalidate() -> None:
        if withdraw == "lease":
            gate.invalidate()
        elif withdraw == "key":
            reader.revoke_trusted_key(peer.key_id, reason="fixture", actor="operator")
        else:
            with rk.database.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM common_knowledge_peers WHERE peer_id=?", (peer.peer_id,)
                ).fetchone()
                state = reader._peer_state_values(row)
                state["status"] = "disabled"
                connection.execute(
                    "UPDATE common_knowledge_peers SET status='disabled', state_hash=? "
                    "WHERE peer_id=?",
                    (content_hash(state), peer.peer_id),
                )

    class Client(_PublisherClient):
        def discovery(self, *, etag: str | None = None) -> dict[str, Any] | None:
            result = super().discovery(etag=etag)
            if stage == "discovery":
                invalidate()
            return result

        def feed(self, *, cursor: int, limit: int) -> dict[str, Any]:
            feeds.append(cursor)
            result = super().feed(cursor=cursor, limit=1)
            if stage == "feed":
                invalidate()
            return result

    original_verify = reader._verify_sync_event
    original_apply = reader._apply_remote_event

    def verify(*args: Any, **kwargs: Any) -> Any:
        result = original_verify(*args, **kwargs)
        if stage == "verify":
            invalidate()
        return result

    def apply(*args: Any, **kwargs: Any) -> int:
        result = original_apply(*args, **kwargs)
        if stage == "between_pages":
            invalidate()
        return result

    monkeypatch.setattr(reader, "_verify_sync_event", verify)
    monkeypatch.setattr(reader, "_apply_remote_event", apply)
    try:
        with (
            gate.operation("sync") as lease,
            bind_lease(lease),
            pytest.raises(
                OperationInvalidated if withdraw == "lease" else CommonKnowledgeSyncError
            ),
        ):
            reader.sync_peer(peer.peer_id, client=Client(publisher))
        expected = 1 if stage == "between_pages" else 0
        assert reader.peer(peer.peer_id).cursor == expected
        assert len(feeds) == (0 if stage == "discovery" else 1)
        with rk.database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM common_knowledge_remote_events"
                ).fetchone()[0]
                == expected
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM common_knowledge_imports").fetchone()[0]
                == expected
            )
        assert not reader.usable()
        if withdraw == "lease":
            monkeypatch.setattr(reader, "_verify_sync_event", original_verify)
            monkeypatch.setattr(reader, "_apply_remote_event", original_apply)
            gate.open()
            with gate.operation("resume") as lease, bind_lease(lease):
                result = reader.sync_peer(peer.peer_id, client=_PublisherClient(publisher))
            assert result.cursor_after == 2
            with rk.database.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM common_knowledge_remote_events"
                    ).fetchone()[0]
                    == 2
                )
    finally:
        rk.close()
        pk.close()


def test_import_and_cursor_are_atomic_and_stale_finish_cannot_regress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pk, publisher = _store(tmp_path, "publisher-atomic")
    rk, reader = _store(tmp_path, "reader-atomic")
    publisher.publish(_proposal("atomic"))
    peer = reader.register_peer(
        CommonKnowledgePeerInput(
            label="peer",
            endpoint="https://example.com",
            publisher_subject_id=publisher.subject_id,
            public_key=publisher.public_key,
        ),
        actor="operator",
    )
    original = reader._insert_version

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic interruption after import")

    monkeypatch.setattr(reader, "_insert_version", fail)
    try:
        with pytest.raises(RuntimeError):
            reader.sync_peer(peer.peer_id, client=_PublisherClient(publisher))
        assert reader.peer(peer.peer_id).cursor == 0
        with rk.database.connection() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM common_knowledge_imports").fetchone()[0]
                == 0
            )
        monkeypatch.setattr(reader, "_insert_version", original)
        assert reader.sync_peer(peer.peer_id, client=_PublisherClient(publisher)).cursor_after == 1
        with rk.database.connection() as connection:
            stale_peer = dict(
                connection.execute(
                    "SELECT * FROM common_knowledge_peers WHERE peer_id=?", (peer.peer_id,)
                ).fetchone()
            )
        stale_peer["completion_generation"] = 0
        with pytest.raises(CommonKnowledgeSyncError, match="cursor_changed"):
            reader._finish_peer_sync(
                CommonKnowledgeSyncResult(peer.peer_id, "unchanged", 0, 0, 0, 0, 0, None),
                error_code=None,
                expected_peer=stale_peer,
            )
        assert reader.peer(peer.peer_id).cursor == 1
    finally:
        rk.close()
        pk.close()


def test_threaded_late_failure_keeps_success_and_its_history(tmp_path: Path) -> None:
    pk, publisher = _store(tmp_path, "publisher-thread")
    rk, reader = _store(tmp_path, "reader-thread")
    peer = reader.register_peer(
        CommonKnowledgePeerInput(
            label="peer",
            endpoint="https://example.com",
            publisher_subject_id=publisher.subject_id,
            public_key=publisher.public_key,
        ),
        actor="operator",
    )
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    class Client(_PublisherClient):
        def discovery(self, *, etag: str | None = None) -> dict[str, Any] | None:
            entered.set()
            assert release.wait(10)
            raise CommonKnowledgeSyncError("common_knowledge_peer_timeout")

    def run() -> None:
        try:
            reader.sync_peer(peer.peer_id, client=Client(publisher))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(10)
        reader.sync_peer(peer.peer_id, client=_PublisherClient(publisher))
        success = reader.peer(peer.peer_id)
        release.set()
        worker.join(10)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], CommonKnowledgeSyncError)
        assert reader.peer(peer.peer_id) == success
        with rk.database.connection() as connection:
            assert [
                row[0]
                for row in connection.execute(
                    "SELECT outcome FROM common_knowledge_sync_runs ORDER BY rowid"
                )
            ] == ["succeeded", "failed"]
    finally:
        release.set()
        worker.join(10)
        rk.close()
        pk.close()


@pytest.mark.parametrize("withdraw", ["lease", "timeout"])
def test_http_send_boundary_and_late_connection_are_fenced(withdraw: str) -> None:
    gate = RuntimeAdmissionGate("reader")
    release = threading.Event()
    finished = threading.Event()
    sent: list[int] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            try:
                if withdraw == "lease":
                    gate.invalidate()
                else:
                    release.wait(2)
                request.extensions["trace"]("http11.send_request_headers.started", {})
                sent.append(1)
                return httpx.Response(200, json={"ok": True})
            finally:
                finished.set()

    with httpx.Client(transport=Transport()) as client:
        sync = CommonKnowledgeHTTPClient("https://example.com", client=client, timeout_seconds=0.1)
        try:
            with (
                gate.operation("sync") as lease,
                bind_lease(lease),
                pytest.raises(
                    OperationInvalidated if withdraw == "lease" else CommonKnowledgeSyncError
                ),
            ):
                sync.discovery()
        finally:
            release.set()
        assert finished.wait(2)
    assert sent == []


def test_sync_due_passes_checkpoint_into_each_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, publisher = _store(tmp_path, "publisher-due")
    rk, reader = _store(tmp_path, "reader-due")
    peer = reader.register_peer(
        CommonKnowledgePeerInput(
            label="peer",
            endpoint="https://example.com",
            publisher_subject_id=publisher.subject_id,
            public_key=publisher.public_key,
        ),
        actor="operator",
    )
    calls: list[int] = []

    def checkpoint() -> None:
        calls.append(1)

    def sync(peer_id: str, **kwargs: Any) -> None:
        assert peer_id == peer.peer_id
        assert kwargs["checkpoint"] is checkpoint
        kwargs["checkpoint"]()

    monkeypatch.setattr(reader, "sync_peer", sync)
    try:
        assert reader.sync_due(checkpoint=checkpoint) == 1
        assert len(calls) >= 4
    finally:
        rk.close()
        pk.close()


def test_stale_sync_failure_cannot_overwrite_a_completed_sync(tmp_path: Path) -> None:
    pk, publisher = _store(tmp_path, "publisher-concurrent")
    rk, reader = _store(tmp_path, "reader-concurrent")
    publisher.publish(_proposal("concurrent"))
    peer = reader.register_peer(
        CommonKnowledgePeerInput(
            label="peer",
            endpoint="https://example.com",
            publisher_subject_id=publisher.subject_id,
            public_key=publisher.public_key,
        ),
        actor="operator",
    )
    completed_peer: list[Any] = []

    class Client(_PublisherClient):
        def discovery(self, *, etag: str | None = None) -> dict[str, Any] | None:
            result = super().discovery(etag=etag)
            reader.sync_peer(peer.peer_id, client=_PublisherClient(publisher))
            completed_peer.append(reader.peer(peer.peer_id))
            return result

    try:
        with pytest.raises(CommonKnowledgeSyncError, match="cursor_changed"):
            reader.sync_peer(peer.peer_id, client=Client(publisher))
        assert reader.peer(peer.peer_id) == completed_peer[0]
        assert reader.peer(peer.peer_id).cursor == 1
        with rk.database.connection() as connection:
            runs = connection.execute(
                "SELECT outcome, cursor_after FROM common_knowledge_sync_runs"
            ).fetchall()
        assert [(run["outcome"], run["cursor_after"]) for run in runs] == [
            ("succeeded", 1),
            ("failed", 0),
        ]
    finally:
        rk.close()
        pk.close()


@pytest.mark.parametrize("primed", [False, True])
@pytest.mark.parametrize("late_failure", [False, True])
@pytest.mark.parametrize("newer_failure", [False, True])
def test_same_cursor_completion_is_fenced_even_with_identical_state_and_clock(
    tmp_path: Path, primed: bool, late_failure: bool, newer_failure: bool
) -> None:
    pk, publisher = _store(tmp_path, "publisher-same-cursor")
    rk, reader = _store(tmp_path, "reader-same-cursor")
    reader.clock = lambda: "2026-10-11T01:00:00.000+00:00"
    peer = reader.register_peer(
        CommonKnowledgePeerInput(
            label="peer",
            endpoint="https://example.com",
            publisher_subject_id=publisher.subject_id,
            public_key=publisher.public_key,
        ),
        actor="operator",
    )
    if primed:
        reader.sync_peer(peer.peer_id, client=_PublisherClient(publisher))
    completed_peer: list[Any] = []

    class Client(_PublisherClient):
        def discovery(self, *, etag: str | None = None) -> dict[str, Any] | None:
            response = super().discovery(etag=etag)
            # An independent store uses the durable completion fence, not a
            # process-local lock. The second sync doesn't advance the cursor.
            other = type(reader)(reader.database, reader.subject_id, reader.key_dir)
            other.clock = reader.clock
            if newer_failure:
                with pytest.raises(CommonKnowledgeSyncError, match="offline"):
                    other.sync_peer(peer.peer_id, client=_OfflineClient())
            else:
                other.sync_peer(peer.peer_id, client=_PublisherClient(publisher))
            completed_peer.append(other.peer(peer.peer_id))
            if late_failure:
                raise CommonKnowledgeSyncError("common_knowledge_peer_timeout")
            return response

    try:
        with pytest.raises(CommonKnowledgeSyncError) as caught:
            reader.sync_peer(peer.peer_id, client=Client(publisher))
        assert caught.value.code == (
            "common_knowledge_peer_timeout"
            if late_failure
            else "common_knowledge_peer_completion_changed"
        )
        assert reader.peer(peer.peer_id) == completed_peer[0]
        assert reader.peer(peer.peer_id).cursor == 0
        assert (reader.peer(peer.peer_id).remote_etag is not None) == (not newer_failure)
        assert reader.peer(peer.peer_id).last_error_code == ("offline" if newer_failure else None)
        with rk.database.connection() as connection:
            runs = connection.execute(
                "SELECT outcome, error_code FROM common_knowledge_sync_runs ORDER BY rowid"
            ).fetchall()
        assert len(runs) == (3 if primed else 2)
        assert runs[-1]["outcome"] == "failed"
        assert runs[-1]["error_code"] == caught.value.code
        # A fresh call after the stale result must still recover normally.
        assert reader.sync_peer(peer.peer_id, client=_PublisherClient(publisher)).outcome == (
            "succeeded" if newer_failure else "unchanged"
        )
    finally:
        rk.close()
        pk.close()


@pytest.mark.parametrize("withdraw", ["endpoint", "key", "disabled", "schedule"])
def test_late_failure_preserves_changed_peer_configuration(tmp_path: Path, withdraw: str) -> None:
    pk, publisher = _store(tmp_path, "publisher-config")
    rk, reader = _store(tmp_path, "reader-config")
    peer = reader.register_peer(
        CommonKnowledgePeerInput(
            label="peer",
            endpoint="https://example.com",
            publisher_subject_id=publisher.subject_id,
            public_key=publisher.public_key,
        ),
        actor="operator",
    )
    changed: list[Any] = []

    class Client(_PublisherClient):
        def discovery(self, *, etag: str | None = None) -> dict[str, Any] | None:
            if withdraw == "key":
                reader.revoke_trusted_key(peer.key_id, reason="fixture", actor="operator")
            else:
                with rk.database.transaction() as connection:
                    row = connection.execute(
                        "SELECT * FROM common_knowledge_peers WHERE peer_id=?", (peer.peer_id,)
                    ).fetchone()
                    state = reader._peer_state_values(row)
                    field, value = {
                        "endpoint": ("endpoint", "https://changed.example"),
                        "disabled": ("status", "disabled"),
                        "schedule": ("sync_interval_seconds", 120),
                    }[withdraw]
                    state[field] = value
                    connection.execute(
                        f"UPDATE common_knowledge_peers SET {field}=?, state_hash=? "
                        "WHERE peer_id=?",
                        (value, content_hash(state), peer.peer_id),
                    )
            changed.append(reader.peer(peer.peer_id))
            raise CommonKnowledgeSyncError("common_knowledge_peer_timeout")

    try:
        with pytest.raises(CommonKnowledgeSyncError, match="peer_timeout"):
            reader.sync_peer(peer.peer_id, client=Client(publisher))
        assert reader.peer(peer.peer_id) == changed[0]
        with rk.database.connection() as connection:
            assert (
                connection.execute("SELECT error_code FROM common_knowledge_sync_runs").fetchone()[
                    0
                ]
                == "common_knowledge_peer_timeout"
            )
    finally:
        rk.close()
        pk.close()
