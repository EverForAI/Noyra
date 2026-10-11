from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.errors import NotFoundError
from noyra.core.types import content_hash
from noyra.interaction import (
    InboundEnvelope,
    InboundIdentityObserved,
    InboundStore,
    TransportInput,
    TransportStore,
)
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.integrity import InteractionIntegrity


def setup(tmp_path: Path) -> tuple[SubjectKernel, InboundStore, str]:
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", "identity-owner", content_hash("identity"))
    transport = TransportStore(kernel.database, tmp_path / "secrets").configure(
        kernel.subject_id,
        TransportInput(
            channel="qq",
            label="QQ",
            endpoint="https://api.sgroup.qq.com",
            credentials={"app_id": SecretStr("app"), "app_secret": SecretStr("secret")},
        ),
        actor="operator",
    )
    return kernel, InboundStore(kernel.database), transport.transport_id


def envelope(transport: str, content: str, event: str = "evt") -> InboundEnvelope:
    return InboundEnvelope(
        channel="qq",
        transport_id=transport,
        provider_event_id=event,
        external_account_id="app",
        external_sender_id="me",
        conversation_id="me",
        reply_selector="qq:user",
        content=content,
    )


def verified_route(tmp_path: Path) -> tuple[SubjectKernel, InboundStore, str, str, str]:
    kernel, store, transport = setup(tmp_path)
    challenge = store.issue_identity_challenge(kernel.subject_id, transport)
    store.ingest(envelope(transport, challenge["code"]))
    binding = store.confirm_identity_challenge(kernel.subject_id, challenge["challenge_id"])
    window = ConversationStore(kernel.database).create(kernel.subject_id, "QQ shared")
    return kernel, store, transport, binding, window["conversation_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("withdraw", ["unlink", "disable", "revoke", "transport", "route"])
async def test_web_push_is_new_only_and_cannot_revive_after_withdrawal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, withdraw: str
) -> None:

    from noyra.interaction import DeliveryDispatcher

    kernel, store, transport, binding, window = verified_route(tmp_path)
    subject = kernel.subject_id
    incoming = store.interactions.receive(
        subject, "web", "web-user", "hello", conversation_id=window
    )
    store.interactions.send(
        subject, "web", "web-user", "old reply", related_interaction_id=incoming.interaction_id
    )
    transports = TransportStore(kernel.database, tmp_path / "secrets")
    dispatcher = DeliveryDispatcher(kernel.database, transports)
    store.route_private_chat(subject, binding, window, share_web_history=True)
    assert dispatcher.enqueue_pending(subject) == 0
    store.route_private_chat(
        subject, binding, window, share_web_history=True, push_web_replies=True, expected_revision=1
    )
    reply = store.interactions.send(
        subject, "web", "web-user", "new reply", related_interaction_id=incoming.interaction_id
    )
    assert dispatcher.enqueue_pending(subject) == 1
    assert dispatcher.enqueue_pending(subject) == 0
    assert ConversationStore(kernel.database).messages(subject, window)["items"][-1][
        "delivery_statuses"
    ] == ["queued"]
    if withdraw == "unlink":
        store.unlink_private_chat(subject, binding)
    elif withdraw in {"disable", "revoke"}:
        store.change_binding(
            binding,
            subject,
            status="revoked" if withdraw == "revoke" else "disabled",
            actor="operator",
            reason="test",
        )
    elif withdraw == "transport":
        transports.disable(transport, subject_id=subject, actor="operator", reason="test")
        transports.enable(transport, subject_id=subject, actor="operator", reason="test")
    else:
        store.route_private_chat(
            subject, binding, window, share_web_history=True, push_web_replies=True
        )
    if withdraw == "disable":
        store.change_binding(binding, subject, status="active", actor="operator", reason="test")
    if withdraw != "revoke":
        store.route_private_chat(
            subject, binding, window, share_web_history=True, push_web_replies=True
        )
    called = False

    async def send(*args: Any, **kwargs: Any) -> str:
        nonlocal called
        called = True
        return "simulated"

    monkeypatch.setattr(dispatcher, "_send", send)
    try:
        result = await dispatcher.deliver_pending(subject)
        assert result[0].interaction_id == reply.interaction_id
        assert result[0].status == "failed"
        assert not called
        assert InteractionIntegrity(kernel.database).verify(subject)["interactions"] == 3
    finally:
        await dispatcher.aclose()
        kernel.close()


@pytest.mark.asyncio
async def test_web_push_rechecks_consent_after_token_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from noyra.interaction import DeliveryDispatcher

    kernel, store, _, binding, window = verified_route(tmp_path)
    subject = kernel.subject_id
    store.route_private_chat(
        subject, binding, window, share_web_history=True, push_web_replies=True
    )
    incoming = store.interactions.receive(
        subject, "web", "web-user", "hello", conversation_id=window
    )
    store.interactions.send(
        subject,
        "web",
        "web-user",
        "sensitive reply",
        related_interaction_id=incoming.interaction_id,
    )
    requests: list[str] = []

    def simulate(req: httpx.Request) -> httpx.Response:
        requests.append(str(req.url))
        store.unlink_private_chat(subject, binding)
        return httpx.Response(200, json={"access_token": "simulated", "expires_in": 3600})

    dispatcher = DeliveryDispatcher(
        kernel.database,
        TransportStore(kernel.database, tmp_path / "secrets"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(simulate)),
    )
    try:
        result = await dispatcher.deliver_pending(subject)
        assert result[0].status == "failed"
        assert len(requests) == 1
        assert "messages" not in requests[0]
    finally:
        await dispatcher.aclose()
        kernel.close()


def test_schema89_route_upgrade_defaults_to_no_push_and_preserves_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core import database as schema

    with monkeypatch.context() as historical:
        historical.setattr(schema, "CURRENT_SCHEMA_VERSION", 89)
        kernel, store, transport = setup(tmp_path)
        subject = kernel.subject_id
        challenge = store.issue_identity_challenge(subject, transport)
        store.ingest(envelope(transport, challenge["code"]))
        binding = store.confirm_identity_challenge(subject, challenge["challenge_id"])
        window = ConversationStore(kernel.database).create(subject, "legacy route")
        with kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO private_channel_routes VALUES(?,?,?,?,?)",
                (binding, subject, window["conversation_id"], 1, "2026-01-01"),
            )
        kernel.close()
    database = schema.Database(tmp_path / "noyra.sqlite3")
    with database.connection() as connection:
        row = connection.execute("SELECT * FROM private_channel_routes").fetchone()
        assert row["binding_id"] == binding
        assert row["share_web_history"] == 1
        assert row["push_web_replies"] == 0
        assert row["revision"] == 1
        assert row["push_after_sequence"] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_web_push_refuses_stale_revision_and_duplicate_recipient(tmp_path: Path) -> None:
    import sqlite3

    kernel, store, transport, binding, window = verified_route(tmp_path)
    subject = kernel.subject_id
    store.route_private_chat(
        subject, binding, window, share_web_history=True, push_web_replies=True, expected_revision=0
    )
    with pytest.raises(ValueError, match="revision"):
        store.route_private_chat(
            subject, binding, window, share_web_history=True, expected_revision=0
        )
    challenge = store.issue_identity_challenge(subject, transport)
    store.ingest(
        envelope(transport, challenge["code"], "verify-second").model_copy(
            update={"external_sender_id": "other", "conversation_id": "other"}
        )
    )
    second = store.confirm_identity_challenge(subject, challenge["challenge_id"])
    with pytest.raises(sqlite3.IntegrityError):
        store.route_private_chat(
            subject, second, window, share_web_history=True, push_web_replies=True
        )


def test_confirmation_and_shared_chat_do_not_copy_or_invent_messages(tmp_path: Path) -> None:
    kernel, store, transport = setup(tmp_path)
    subject = kernel.subject_id
    challenge = store.issue_identity_challenge(subject, transport)
    received = store.ingest(envelope(transport, challenge["code"].lower()))
    assert isinstance(received, InboundIdentityObserved)
    assert store.ingest(envelope(transport, challenge["code"].lower())).duplicate
    with kernel.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM interactions").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM interaction_inbound_events").fetchone()[0] == 0
        )
    binding = store.confirm_identity_challenge(subject, challenge["challenge_id"])
    assert binding == store.confirm_identity_challenge(subject, challenge["challenge_id"])
    with pytest.raises(PermissionError):
        store.ingest(envelope(transport, challenge["code"], "replay"))
    windows = ConversationStore(kernel.database)
    window = windows.create(subject, "shared")
    with pytest.raises(ValueError):
        store.route_private_chat(
            subject, binding, window["conversation_id"], share_web_history=False
        )
    store.route_private_chat(subject, binding, window["conversation_id"], share_web_history=True)
    first = store.ingest(envelope(transport, "hello", "chat"))
    assert not isinstance(first, InboundIdentityObserved)
    assert store.ingest(envelope(transport, "hello", "chat")).interaction_id == first.interaction_id
    reply = store.interactions.send(
        subject, "qq", "me", "reply", related_interaction_id=first.interaction_id
    )
    page = windows.messages(subject, window["conversation_id"])
    assert [item["interaction_id"] for item in page["items"]] == [
        first.interaction_id,
        reply.interaction_id,
    ]
    assert all(item["author_verified"] for item in page["items"])
    web = store.interactions.receive(
        subject, "web", "web-user", "next", conversation_id=window["conversation_id"]
    )
    assert len(windows.authorized_history(subject, web.interaction_id)) == 2
    assert InteractionIntegrity(kernel.database).verify(subject)["interactions"] == 3
    store.change_binding(binding, subject, status="revoked", actor="operator", reason="test revoke")
    assert InteractionIntegrity(kernel.database).verify(subject)["interactions"] == 3
    with pytest.raises(PermissionError):
        store.ingest(envelope(transport, "new message", "after-revoke"))


def test_false_external_author_is_rejected_by_integrity(tmp_path: Path) -> None:
    from noyra.core.errors import IntegrityError
    from noyra.interaction.conversations import link_message

    kernel, store, _ = setup(tmp_path)
    window = ConversationStore(kernel.database).create(kernel.subject_id, "test")
    original = store.interactions.receive(kernel.subject_id, "qq", "unverified", "test")
    with kernel.database.transaction() as connection:
        link_message(
            connection,
            kernel.subject_id,
            original.interaction_id,
            conversation_id=window["conversation_id"],
            related_id=None,
            channel="qq",
            counterparty="unverified",
        )
        connection.execute(
            "UPDATE private_conversation_messages SET author_verified=1 WHERE interaction_id=?",
            (original.interaction_id,),
        )
    with pytest.raises(IntegrityError, match="conversation message ownership"):
        InteractionIntegrity(kernel.database).verify(kernel.subject_id)


def test_wrong_guesses_commit_and_fifth_guess_revokes(tmp_path: Path) -> None:
    kernel, store, transport = setup(tmp_path)
    challenge = store.issue_identity_challenge(kernel.subject_id, transport)
    for index in range(5):
        with pytest.raises(PermissionError):
            store.ingest(envelope(transport, "AAAAAAAAAA", str(index)))
    state = store.identity_challenge(kernel.subject_id, challenge["challenge_id"])
    assert state["attempts"] == 5
    assert state["status"] == "revoked"
    with pytest.raises(PermissionError):
        store.ingest(envelope(transport, challenge["code"]))
    assert "code" not in state and "code_hash" not in state


def test_scope_expiry_and_revocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kernel, store, transport = setup(tmp_path)
    subject = kernel.subject_id
    challenge = store.issue_identity_challenge(subject, transport)
    with pytest.raises(NotFoundError):
        store.identity_challenge("other", challenge["challenge_id"])
    group = envelope(transport, challenge["code"]).model_copy(update={"reply_selector": "qq:group"})
    with pytest.raises(PermissionError):
        store.ingest(group)
    with pytest.raises(ValueError):
        store.confirm_identity_challenge(subject, challenge["challenge_id"])
    monkeypatch.setattr(
        "noyra.interaction.inbound.utc_now", lambda: "2099-01-01T00:00:00.000+00:00"
    )
    with pytest.raises(PermissionError):
        store.ingest(envelope(transport, challenge["code"]))
    assert store.identity_challenge(subject, challenge["challenge_id"])["status"] == "expired"


def test_disabled_transport_cannot_observe_or_confirm(tmp_path: Path) -> None:
    kernel, store, transport = setup(tmp_path)
    challenge = store.issue_identity_challenge(kernel.subject_id, transport)
    store.ingest(envelope(transport, challenge["code"]))
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE interaction_transports SET status='disabled' WHERE transport_id=?", (transport,)
        )
    with pytest.raises(ValueError):
        store.confirm_identity_challenge(kernel.subject_id, challenge["challenge_id"])
    with pytest.raises(PermissionError):
        store.ingest(envelope(transport, challenge["code"]))


def test_other_robot_cannot_consume_and_reissue_revokes_old_code(tmp_path: Path) -> None:
    kernel, store, transport = setup(tmp_path)
    second = TransportStore(kernel.database, tmp_path / "secrets").configure(
        kernel.subject_id,
        TransportInput(
            channel="qq",
            label="second",
            endpoint="https://api.sgroup.qq.com",
            credentials={"app_id": SecretStr("app2"), "app_secret": SecretStr("secret2")},
        ),
        actor="operator",
    )
    challenge = store.issue_identity_challenge(kernel.subject_id, transport)
    with pytest.raises(PermissionError):
        store.ingest(envelope(second.transport_id, challenge["code"]))
    assert store.identity_challenge(kernel.subject_id, challenge["challenge_id"])["attempts"] == 0
    store.issue_identity_challenge(kernel.subject_id, transport)
    assert (
        store.identity_challenge(kernel.subject_id, challenge["challenge_id"])["status"]
        == "revoked"
    )


def test_identity_http_auth_and_confirmation_errors(tmp_path: Path) -> None:
    import json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from noyra.service import NoyraHTTPServer, ServiceSettings

    kernel, _, transport = setup(tmp_path)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id=kernel.subject_id,
        genesis_hash=content_hash("identity"),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr("test-identity-token-sufficient-entropy"),
    )
    server = NoyraHTTPServer(kernel, settings)
    server.start()

    def call(
        path: str, payload: dict[str, Any] | None = None, *, auth: bool = True
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer test-identity-token-sufficient-entropy"
        req = Request(
            f"http://127.0.0.1:{server.address[1]}" + path,
            headers=headers,
            data=json.dumps(payload).encode() if payload is not None else None,
        )
        try:
            response = urlopen(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    try:
        namespace = "/api/v1/config/identity-challenges"
        assert call(namespace, {"transport_id": transport}, auth=False)[0] == 401
        code, challenge = call(namespace, {"transport_id": transport})
        assert code == 200
        path = namespace + "/" + challenge["challenge_id"]
        assert call(path, auth=False)[0] == 401
        code, state = call(path)
        assert code == 200 and "code" not in state and "code_hash" not in state
        assert call(path + "/confirm", {})[0] == 409
        server.inbound.ingest(envelope(transport, challenge["code"]))
        assert call(path + "/confirm", {"role": "creator"})[0] == 409
        assert call(path + "/confirm", {})[0] == 200
        binding = call(path + "/confirm", {})[1]["binding_id"]
        window = ConversationStore(kernel.database).create(kernel.subject_id, "HTTP route")
        route_path = "/api/v1/config/inbound-bindings/" + binding + "/route"
        payload = {
            "conversation_id": window["conversation_id"],
            "share_web_history": True,
            "expected_revision": 0,
            "push_web_replies": True,
            "reason": "test",
        }
        assert call(route_path, payload, auth=False)[0] == 401
        assert call(route_path, payload)[0] == 200
        assert call(route_path, payload)[0] == 409
        assert call(route_path.replace("/route", "/unlink"), {"reason": "test"})[0] == 200
    finally:
        server.close()
        kernel.close()


@pytest.mark.asyncio
async def test_revoked_recipient_does_not_receive_queued_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    from noyra.interaction import DeliveryDispatcher

    kernel, store, transport = setup(tmp_path)
    subject = kernel.subject_id
    store.bind(subject, transport, external_account_id="app", external_sender_id="me")
    incoming = store.ingest(envelope(transport, "hello"))
    store.interactions.send(
        subject, "qq", "me", "sensitive reply", related_interaction_id=incoming.interaction_id
    )
    transports = TransportStore(kernel.database, tmp_path / "secrets")
    dispatcher = DeliveryDispatcher(kernel.database, transports)
    called = False

    async def simulated_send(*args: Any, **kwargs: Any) -> str:
        nonlocal called
        called = True
        return "provider-message"

    monkeypatch.setattr(dispatcher, "_send", simulated_send)
    assert dispatcher.enqueue_pending(subject) == 1
    binding = store.list_bindings(subject)[0]
    store.change_binding(
        binding["binding_id"], subject, status="revoked", actor="operator", reason="withdraw"
    )
    try:
        results = await dispatcher.deliver_pending(subject)
        assert len(results) == 1
        assert results[0].status == "failed"
        assert called is False
    finally:
        await dispatcher.aclose()
        kernel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("withdraw_at", ["token", "refresh"])
@pytest.mark.parametrize("withdraw", ["recipient", "transport", "content"])
async def test_delivery_rechecks_authorization_after_token_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, withdraw_at: str, withdraw: str
) -> None:
    import httpx

    from noyra.interaction import DeliveryDispatcher

    kernel, store, transport = setup(tmp_path)
    subject = kernel.subject_id
    store.bind(subject, transport, external_account_id="app", external_sender_id="me")
    incoming = store.ingest(envelope(transport, "hello"))
    outgoing = store.interactions.send(
        subject, "qq", "me", "sensitive reply", related_interaction_id=incoming.interaction_id
    )
    sends = 0
    tokens = 0

    def platform(req: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(401 if withdraw_at == "refresh" else 200, json={"id": "message"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(platform))
    dispatcher = DeliveryDispatcher(
        kernel.database, TransportStore(kernel.database, tmp_path / "secrets"), client=client
    )

    async def credentials(*args: object) -> dict[str, str]:
        nonlocal tokens
        tokens += 1
        if tokens == (2 if withdraw_at == "refresh" else 1):
            if withdraw == "recipient":
                binding = store.list_bindings(subject)[0]
                store.change_binding(
                    binding["binding_id"],
                    subject,
                    status="revoked",
                    actor="operator",
                    reason="withdraw",
                )
            elif withdraw == "transport":
                with kernel.database.transaction() as connection:
                    connection.execute(
                        "UPDATE interaction_transports SET status='disabled' WHERE transport_id=?",
                        (transport,),
                    )
            else:
                window = ConversationStore(kernel.database).create(subject, "withdrawn")
                with kernel.database.transaction() as connection:
                    connection.execute(
                        "INSERT INTO private_conversation_messages "
                        "(interaction_id,conversation_id,subject_id) VALUES(?,?,?)",
                        (outgoing.interaction_id, window["conversation_id"], subject),
                    )
                    connection.execute(
                        "UPDATE private_conversations SET status='deleting' "
                        "WHERE conversation_id=?",
                        (window["conversation_id"],),
                    )
        return {"access_token": "test-token"}

    monkeypatch.setattr(dispatcher, "_provider_credentials", credentials)
    try:
        assert dispatcher.enqueue_pending(subject) == 1
        results = await dispatcher.deliver_pending(subject)
        assert len(results) == 1 and results[0].status == "failed"
        assert sends == (1 if withdraw_at == "refresh" else 0)
    finally:
        await dispatcher.aclose()
        await client.aclose()
        kernel.close()
