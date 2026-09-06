from __future__ import annotations

import io
import json
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from noyra.core import RuntimeLogExporter, SubjectKernel
from noyra.core.errors import NotFoundError
from noyra.core.types import content_hash
from noyra.knowledge import (
    CommonKnowledgePeerInput,
    CommonKnowledgeProposal,
    CommonKnowledgeStore,
)
from noyra.knowledge.sync import CommonKnowledgeHTTPClient, CommonKnowledgeSyncError


class _PublisherClient:
    def __init__(self, publisher: CommonKnowledgeStore):
        self.publisher = publisher

    def __enter__(self) -> _PublisherClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def discovery(self, *, etag: str | None = None) -> dict[str, Any] | None:
        document = self.publisher.discovery_document()
        return None if etag is not None and etag == document["etag"] else document

    def feed(self, *, cursor: int, limit: int) -> dict[str, Any]:
        return self.publisher.feed_document(cursor=cursor, limit=limit)


class _OfflineClient:
    def __enter__(self) -> _OfflineClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def discovery(self, *, etag: str | None = None) -> None:
        del etag
        raise CommonKnowledgeSyncError("offline")

    def feed(self, *, cursor: int, limit: int) -> dict[str, Any]:
        del cursor, limit
        raise CommonKnowledgeSyncError("offline")


class _TamperedClient(_PublisherClient):
    def __init__(self, publisher: CommonKnowledgeStore, *, tamper: str):
        super().__init__(publisher)
        self.tamper = tamper

    def discovery(self, *, etag: str | None = None) -> dict[str, Any] | None:
        document = super().discovery(etag=etag)
        if document is not None and self.tamper == "discovery":
            document["latest_sequence"] = int(document["latest_sequence"]) + 1
        return document

    def feed(self, *, cursor: int, limit: int) -> dict[str, Any]:
        document = super().feed(cursor=cursor, limit=limit)
        if self.tamper == "event" and document["events"]:
            document["events"][0]["event_hash"] = "tampered"
        return document


def _store(root: Path, subject_id: str) -> tuple[SubjectKernel, CommonKnowledgeStore]:
    data_root = root / subject_id
    data_root.mkdir()
    kernel = SubjectKernel(
        data_root / "noyra.sqlite3",
        subject_id,
        content_hash({"seed": subject_id}),
    )
    store = CommonKnowledgeStore(
        kernel.database,
        subject_id,
        data_root / "secrets" / "common-knowledge",
    )
    return kernel, store


def _proposal(title: str, version: int = 1) -> CommonKnowledgeProposal:
    return CommonKnowledgeProposal(
        scope="protocol",
        title=title,
        summary="A bounded provider-neutral procedure.",
        payload={
            "procedure": ["classify", "verify"],
            "validation": ["one durable record"],
            "tags": ["sync"],
        },
        version=version,
    )


def test_two_nodes_sync_versions_revoke_and_recover_offline(tmp_path: Path) -> None:
    publisher_kernel, publisher = _store(tmp_path, "Publisher-P302")
    reader_kernel, reader = _store(tmp_path, "Reader-P302")
    try:
        first = publisher.publish(_proposal("Version one"), series_id="series-P302")
        peer = reader.register_peer(
            CommonKnowledgePeerInput(
                label="publisher",
                endpoint="https://example.com",
                publisher_subject_id=publisher.subject_id,
                public_key=publisher.public_key,
                sync_interval_seconds=60,
            ),
            actor="operator",
        )
        initial = reader.sync_peer(peer.peer_id, client=_PublisherClient(publisher))
        assert initial.outcome == "succeeded"
        assert initial.cursor_after == 1
        assert reader.review_queue(limit=1)[0]["import_status"] == "quarantined"

        evaluation = reader.begin_evaluation(first.package_id, reason="review version one")
        reader.complete_evaluation(
            evaluation.evaluation_id,
            decision="accepted",
            reason="safe advisory",
        )
        assert reader.usable()

        second = publisher.publish(
            _proposal("Version two", version=2),
            series_id="series-P302",
            supersedes_package_id=first.package_id,
        )
        second_sync = reader.sync_peer(peer.peer_id, client=_PublisherClient(publisher))
        assert second_sync.cursor_after == 2
        with reader_kernel.database.connection() as connection:
            old_import = connection.execute(
                "SELECT status FROM common_knowledge_imports WHERE package_id = ? "
                "AND subject_id = ?",
                (first.package_id, reader.subject_id),
            ).fetchone()
            new_import = connection.execute(
                "SELECT status FROM common_knowledge_imports WHERE package_id = ? "
                "AND subject_id = ?",
                (second.package_id, reader.subject_id),
            ).fetchone()
        assert old_import["status"] == "revoked"
        assert new_import["status"] == "quarantined"

        second_evaluation = reader.begin_evaluation(
            second.package_id,
            reason="review version two",
        )
        reader.complete_evaluation(
            second_evaluation.evaluation_id,
            decision="accepted",
            reason="newer safe advisory",
        )
        before_offline = reader.peer(peer.peer_id).cursor
        with pytest.raises(CommonKnowledgeSyncError, match="offline"):
            reader.sync_peer(peer.peer_id, client=_OfflineClient())
        assert reader.peer(peer.peer_id).cursor == before_offline

        publisher.revoke(second.package_id, reason="publisher correction")
        recovered = reader.sync_peer(peer.peer_id, client=_PublisherClient(publisher))
        assert recovered.revoked == 1
        with reader_kernel.database.connection() as connection:
            revoked = connection.execute(
                "SELECT status FROM common_knowledge_imports WHERE package_id = ? "
                "AND subject_id = ?",
                (second.package_id, reader.subject_id),
            ).fetchone()
            sync_runs = connection.execute(
                "SELECT outcome, cursor_before, cursor_after, error_code "
                "FROM common_knowledge_sync_runs WHERE peer_id = ? ORDER BY occurred_at, sync_id",
                (peer.peer_id,),
            ).fetchall()
        assert revoked["status"] == "revoked"
        assert sync_runs[-2]["outcome"] == "failed"
        assert sync_runs[-2]["cursor_before"] == sync_runs[-2]["cursor_after"]
        assert sync_runs[-1]["outcome"] == "succeeded"
        assert sync_runs[-1]["error_code"] is None
    finally:
        reader_kernel.close()
        publisher_kernel.close()


def test_sync_rejects_tampered_discovery_and_event_without_advancing_cursor(
    tmp_path: Path,
) -> None:
    publisher_kernel, publisher = _store(tmp_path, "Publisher-Tamper")
    reader_kernel, reader = _store(tmp_path, "Reader-Tamper")
    try:
        publisher.publish(_proposal("Tamper target"))
        peer = reader.register_peer(
            CommonKnowledgePeerInput(
                label="publisher",
                endpoint="https://example.com",
                publisher_subject_id=publisher.subject_id,
                public_key=publisher.public_key,
            ),
            actor="operator",
        )
        with pytest.raises(CommonKnowledgeSyncError, match="invalid_evidence"):
            reader.sync_peer(
                peer.peer_id,
                client=_TamperedClient(publisher, tamper="discovery"),
            )
        assert reader.peer(peer.peer_id).cursor == 0
        with pytest.raises(CommonKnowledgeSyncError, match="invalid_evidence"):
            reader.sync_peer(
                peer.peer_id,
                client=_TamperedClient(publisher, tamper="event"),
            )
        assert reader.peer(peer.peer_id).cursor == 0
        with reader_kernel.database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM common_knowledge_remote_events WHERE peer_id = ?",
                    (peer.peer_id,),
                ).fetchone()[0]
                == 0
            )
    finally:
        reader_kernel.close()
        publisher_kernel.close()


def test_peer_ingress_rejects_private_endpoints_and_invalid_subject_ids() -> None:
    with pytest.raises(ValueError):
        CommonKnowledgePeerInput(
            label="local",
            endpoint="https://127.0.0.1",
            publisher_subject_id="Publisher-P302",
            public_key="key",
        )
    with pytest.raises(ValueError):
        CommonKnowledgePeerInput(
            label="invalid",
            endpoint="https://example.com",
            publisher_subject_id="bad subject",
            public_key="key",
        )


def test_evaluation_is_explicit_private_safe_and_append_only(tmp_path: Path) -> None:
    publisher_kernel, publisher = _store(tmp_path, "Publisher-Eval")
    reader_kernel, reader = _store(tmp_path, "Reader-Eval")
    try:
        package = publisher.publish(_proposal("Explicit evaluation"))
        peer = reader.register_peer(
            CommonKnowledgePeerInput(
                label="publisher",
                endpoint="https://example.com",
                publisher_subject_id=publisher.subject_id,
                public_key=publisher.public_key,
            ),
            actor="operator",
        )
        reader.sync_peer(peer.peer_id, client=_PublisherClient(publisher))
        tables = (
            "subject_identity",
            "memories",
            "goals",
            "mission_candidates",
            "autonomous_projects",
        )
        before: dict[str, list[dict[str, Any]]] = {}
        with reader_kernel.database.connection() as connection:
            for table in tables:
                before[table] = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
        with pytest.raises(NotFoundError, match="evaluation"):
            reader.complete_evaluation("missing", decision="accepted", reason="no request")
        assert reader.usable() == []
        requested = reader.begin_evaluation(package.package_id, reason="explicit review")
        completed = reader.complete_evaluation(
            requested.evaluation_id,
            decision="accepted",
            reason="reviewed",
        )
        assert completed.status == "accepted"
        assert len(reader.usable()) == 1
        with reader_kernel.database.connection() as connection:
            for table in tables:
                assert [
                    dict(row) for row in connection.execute(f"SELECT * FROM {table}")
                ] == before[table]
            events = connection.execute(
                "SELECT sequence, status FROM common_knowledge_evaluation_events "
                "WHERE evaluation_id = ? ORDER BY sequence",
                (requested.evaluation_id,),
            ).fetchall()
        assert [(row["sequence"], row["status"]) for row in events] == [
            (1, "requested"),
            (2, "accepted"),
        ]
        with (
            pytest.raises(sqlite3.IntegrityError, match="append-only"),
            reader_kernel.database.transaction() as connection,
        ):
            connection.execute(
                "UPDATE common_knowledge_evaluation_events SET reason = 'tampered' "
                "WHERE evaluation_id = ?",
                (requested.evaluation_id,),
            )
    finally:
        reader_kernel.close()
        publisher_kernel.close()


def test_http_client_uses_public_common_knowledge_routes_and_bounds_json() -> None:
    paths: list[str] = []
    encodings: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        encodings.append(request.headers["accept-encoding"])
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            json={"ok": True},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    sync = CommonKnowledgeHTTPClient("https://example.com", client=client)
    try:
        assert sync.discovery() == {"ok": True}
        assert sync.feed(cursor=4, limit=10) == {"ok": True}
    finally:
        client.close()
    assert paths == [
        "/api/common-knowledge/discovery",
        "/api/common-knowledge/feed?cursor=4&limit=10",
    ]
    assert encodings == ["identity", "identity"]


def test_http_client_absolute_deadline_includes_response_headers() -> None:
    def delayed_headers(request: httpx.Request) -> httpx.Response:
        time.sleep(0.1)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"ok": True},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(delayed_headers))
    sync = CommonKnowledgeHTTPClient(
        "https://example.com",
        client=client,
        timeout_seconds=0.02,
    )
    started = time.monotonic()
    try:
        with pytest.raises(CommonKnowledgeSyncError, match="common_knowledge_peer_timeout"):
            sync.discovery()
        assert time.monotonic() - started < 0.1
        # Let the bounded late worker leave the injected client before closing it.
        time.sleep(0.12)
    finally:
        client.close()


def test_runtime_export_keeps_peer_and_evaluation_rows_subject_scoped(tmp_path: Path) -> None:
    publisher_kernel, publisher = _store(tmp_path, "Publisher-Export")
    reader_kernel, reader = _store(tmp_path, "Reader-Export")
    try:
        package = publisher.publish(_proposal("Exported package"))
        peer = reader.register_peer(
            CommonKnowledgePeerInput(
                label="publisher",
                endpoint="https://example.com",
                publisher_subject_id=publisher.subject_id,
                public_key=publisher.public_key,
            ),
            actor="operator",
        )
        reader.sync_peer(peer.peer_id, client=_PublisherClient(publisher))
        evaluation = reader.begin_evaluation(package.package_id, reason="export test")
        reader.complete_evaluation(
            evaluation.evaluation_id, decision="rejected", reason="not needed"
        )
        artifact = RuntimeLogExporter(reader_kernel.database).export(
            reader.subject_id,
            actor="test",
        )
        with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
            peer_rows = [
                json.loads(line)
                for line in archive.read("tables/common_knowledge_peers.jsonl").splitlines()
            ]
            evaluation_rows = [
                json.loads(line)
                for line in archive.read(
                    "tables/common_knowledge_evaluation_events.jsonl"
                ).splitlines()
            ]
        assert {row["subject_id"] for row in peer_rows} == {reader.subject_id}
        assert {row["subject_id"] for row in evaluation_rows} == {reader.subject_id}
    finally:
        reader_kernel.close()
        publisher_kernel.close()
