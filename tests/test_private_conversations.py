from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core.errors import NotFoundError
from noyra.core.types import content_hash
from noyra.interaction import InteractionStore
from noyra.interaction.conversations import ConversationConflict, ConversationStore
from noyra.service import NoyraHTTPServer, ServiceSettings


def test_windows_keep_identity_and_reply_links_and_independent_names(tmp_path: Path) -> None:
    db = Database(tmp_path / "windows.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    IdentityStore(db).ensure("other", content_hash("other"))
    windows = ConversationStore(db)
    first = windows.create("owner", "same")
    second = windows.create("owner", "same")
    interactions = InteractionStore(db)
    message = interactions.receive(
        "owner",
        "web",
        "web-user",
        "hello",
        idempotency_key="message-1",
        conversation_id=first["conversation_id"],
    )
    reply = interactions.send(
        "owner", "web", "web-user", "response", related_interaction_id=message.interaction_id
    )
    assert [
        row["interaction_id"]
        for row in windows.messages("owner", first["conversation_id"])["items"]
    ] == [message.interaction_id, reply.interaction_id]
    assert windows.messages("owner", second["conversation_id"])["items"] == []
    assert all(
        row["author_verified"]
        for row in windows.messages("owner", first["conversation_id"])["items"]
    )
    assert [
        row["interaction_id"]
        for row in windows.messages("owner", first["conversation_id"], status="sent")["items"]
    ] == [reply.interaction_id]
    with pytest.raises(ConversationConflict):
        interactions.receive(
            "owner",
            "web",
            "web-user",
            "hello",
            idempotency_key="message-1",
            conversation_id=second["conversation_id"],
        )
    with pytest.raises(NotFoundError):
        windows.messages("other", first["conversation_id"])
    updated = windows.update("owner", first["conversation_id"], expected_revision=1, pinned=True)
    assert updated["revision"] == 2
    with pytest.raises(ConversationConflict):
        windows.update("owner", first["conversation_id"], expected_revision=1, title="overwritten")
    windows.update("owner", first["conversation_id"], expected_revision=2, archived=True)
    with pytest.raises(ConversationConflict):
        interactions.receive(
            "owner", "web", "web-user", "late", conversation_id=first["conversation_id"]
        )
    interactions.send(
        "owner", "web", "web-user", "late reply", related_interaction_id=message.interaction_id
    )


def test_window_history_pages_and_migration_preserve_originals(tmp_path: Path) -> None:
    db = Database(tmp_path / "history.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    interactions = InteractionStore(db)
    records = [interactions.receive("owner", "web", "web-user", f"message {i}") for i in range(5)]
    windows = ConversationStore(db)
    window = windows.list("owner")["items"][0]
    page = windows.messages("owner", window["conversation_id"], limit=2)
    assert len(page["items"]) == 2
    collected = page["items"]
    while page["next_before"]:
        page = windows.messages(
            "owner", window["conversation_id"], limit=2, before=page["next_before"]
        )
        collected = page["items"] + collected
    assert [row["interaction_id"] for row in collected] == [
        record.interaction_id for record in records
    ]
    assert (
        len(windows.messages("owner", window["conversation_id"], query="message 2")["items"]) == 1
    )
    with db.transaction() as connection:
        connection.execute("DELETE FROM private_conversation_messages")
        connection.execute("DELETE FROM private_conversations")
        connection.execute("ALTER TABLE cognitive_resource_groups DROP COLUMN reasoning_effort")
        connection.execute("UPDATE schema_meta SET value='83' WHERE key='schema_version'")
    migrated = ConversationStore(Database(db.path))
    window = migrated.list("owner")["items"][0]
    assert window["title"] == "历史对话"
    assert len(migrated.messages("owner", window["conversation_id"])["items"]) == 5


def test_conversation_http_auth_revision_and_failed_send_rollback(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="http-owner",
        genesis_hash=content_hash("http"),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr("test-token-sufficient-entropy-for-preview"),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    server.start()
    base = f"http://127.0.0.1:{server.address[1]}"

    def call(
        path: str, payload: dict[str, Any] | None = None, *, auth: bool = True
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer test-token-sufficient-entropy-for-preview"
        req = Request(
            base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=headers,
        )
        try:
            response = urlopen(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    try:
        assert call("/api/v1/admin/conversations", auth=False)[0] == 401
        assert call("/api/v1/admin/conversations", {"title": "private"}, auth=False)[0] == 401
        status, row = call("/api/v1/admin/conversations", {"title": "private"})
        assert status == 200
        identifier = row["conversation_id"]
        assert call(f"/api/v1/admin/conversations/{identifier}/messages", auth=False)[0] == 401
        assert (
            call(
                f"/api/v1/admin/conversations/{identifier}",
                {"expected_revision": 1, "title": "new"},
            )[0]
            == 200
        )
        assert (
            call(
                f"/api/v1/admin/conversations/{identifier}",
                {"expected_revision": 1, "title": "stale"},
            )[0]
            == 409
        )
        message = {
            "content": "original",
            "conversation_id": identifier,
            "idempotency_key": "http-message",
        }
        assert call("/api/v1/interactions", message)[0] == 201
        assert call("/api/v1/interactions", message)[0] == 201
        assert call("/api/v1/interactions", {**message, "content": "changed"})[0] == 409
        assert (
            call(
                "/api/v1/interactions",
                {**message, "conversation_id": "missing", "idempotency_key": "missing"},
            )[0]
            == 404
        )
        assert len(server.conversations.messages(settings.subject_id, identifier)["items"]) == 1
    finally:
        server.close()
        kernel.close()


def test_legacy_author_is_not_claimed_as_current_operator(tmp_path: Path) -> None:
    db = Database(tmp_path / "legacy.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("legacy"))
    InteractionStore(db).receive("owner", "web", "web-user", "legacy original")
    with db.transaction() as connection:
        connection.execute("DELETE FROM private_conversation_messages")
        connection.execute("DELETE FROM private_conversations")
        connection.execute("ALTER TABLE cognitive_resource_groups DROP COLUMN reasoning_effort")
        connection.execute("UPDATE schema_meta SET value='83' WHERE key='schema_version'")
    migrated = ConversationStore(Database(db.path))
    window = migrated.list("owner")["items"][0]
    assert (
        migrated.messages("owner", window["conversation_id"])["items"][0]["author_verified"]
        is False
    )
