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
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.erasure import ConversationErasure
from noyra.model.ledger import ModelLedger
from noyra.service import NoyraHTTPServer, ServiceSettings


def test_assessment_has_no_intent_marker_or_content_and_does_not_enable_deletion(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    window = ConversationStore(db).create("owner", "private title")
    message = InteractionStore(db).receive(
        "owner", "web", "web-user", "private raw text", conversation_id=window["conversation_id"]
    )
    erasure = ConversationErasure(db)
    with db.read_transaction() as connection:
        before = list(connection.iterdump())
    result = erasure.assess("owner", window["conversation_id"])
    assert result["status"] == "full_contract_pending"
    assert result["deletion_available"] is False
    assert result["counts"]["messages"] == 1
    assert result["coverage"] == "first_prototype_blocker"
    assert "private raw text" not in json.dumps(result)
    assert "private title" not in json.dumps(result)
    assert not erasure.root.exists()
    with db.read_transaction() as connection:
        assert list(connection.iterdump()) == before
    assert InteractionStore(db).get(message.interaction_id).content == "private raw text"
    with pytest.raises(NotFoundError):
        erasure.assess("other", window["conversation_id"])


def test_assessment_reports_opaque_provenance_without_accepting_or_hiding_window(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    window = ConversationStore(db).create("owner", "private title")
    ModelLedger(db).prepare_call(
        "owner", "mock", "mock", "sleep_reflection:legacy", content_hash("input"), "legacy"
    )
    erasure = ConversationErasure(db)
    result = erasure.assess("owner", window["conversation_id"])
    assert result["status"] == "review_required"
    assert result["reason_code"] == "legacy_private_provenance_review_required"
    assert result["counts"] is None
    assert result["deletion_available"] is False
    assert not erasure.root.exists()
    assert erasure.get("owner", window["conversation_id"]) is None
    assert ConversationStore(db).list("owner")["items"][0]["status"] == "active"


def test_retired_erasure_http_routes_cannot_mutate_chat(tmp_path: Path) -> None:
    token = "synthetic-erasure-assessment-token"
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="assessment-http",
        genesis_hash=content_hash("assessment"),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr(token),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    window = server.conversations.create(kernel.subject_id, "private HTTP title")
    server.start()
    try:
        base = f"http://127.0.0.1:{server.address[1]}"
        path = f"/api/v1/admin/conversations/{window['conversation_id']}/erasure-assessment"

        def call(
            route: str, *, auth: bool = True, method: str = "GET"
        ) -> tuple[int, dict[str, Any]]:
            headers = {"Authorization": f"Bearer {token}"} if auth else {}
            if method in {"POST", "DELETE"}:
                headers["Content-Type"] = "application/json"
            req = Request(
                base + route,
                headers=headers,
                method=method,
                data=b"{}" if method in {"POST", "DELETE"} else None,
            )
            try:
                reply = urlopen(req, timeout=10)
            except HTTPError as error:
                reply = error
            with reply:
                raw = reply.read()
                return reply.status, (
                    json.loads(raw)
                    if reply.headers.get_content_type() == "application/json"
                    else {}
                )

        with kernel.database.read_transaction() as connection:
            before = list(connection.iterdump())
        assert call(path, auth=False)[0] == 404
        status, body = call(path)
        assert status == 404 and body["error"] == "not_found"
        assert "private HTTP title" not in json.dumps(body)
        assert call(path.replace("/api/v1/", "/api/"))[0] == 404
        for suffix in ("?limit=1", "?bad=1", "?limit=1&limit=2"):
            assert call(path + suffix)[0] == 404
        assert call(path.replace(window["conversation_id"], "missing"))[0] == 404
        assert call(path, method="POST")[0] in {400, 404, 405}
        conversation_path = path.removesuffix("/erasure-assessment")
        assert call(conversation_path, method="DELETE")[0] == 501
        for operation in ("delete", "erase", "erasure"):
            assert call(conversation_path + "/" + operation, method="POST")[0] == 400
        assert not (tmp_path / "privacy-deletions").exists()
        with kernel.database.read_transaction() as connection:
            assert list(connection.iterdump()) == before
    finally:
        server.close()
        kernel.close()
