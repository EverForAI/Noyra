from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

import test_autonomous_projects as projects_base
from noyra.core import Database, EventStore
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.types import canonical_json, content_hash
from noyra.interaction import (
    DeliveryDispatcher,
    DeliveryOutcomeUnknown,
    InboundEnvelope,
    InboundStore,
    InteractionStore,
    TransportInput,
    TransportStore,
)
from noyra.interaction.assistance import AssistanceStore
from noyra.interaction.assistance_routes import AssistanceRoutes
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.integrity import InteractionIntegrity
from noyra.interaction.participants import ParticipantStore
from noyra.interaction.types import InteractionDecision
from test_c0_assistance import case as case
from test_c0_assistance import help_fixture


async def external_help(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
    channel: Literal["email", "webhook"] = "webhook",
    *,
    unknown: bool = False,
    fail_once: bool = False,
) -> Any:
    manager, worker, store = await help_fixture(case)
    database = case.kernel.database
    transports = TransportStore(database, database.path.parent / "external-help-secrets")
    transport = transports.configure(
        case.subject_id,
        TransportInput(
            channel=channel,
            label="External help",
            endpoint="smtps://mail.example.com:465"
            if channel == "email"
            else "https://help.example.com/send",
            credentials={"from_address": SecretStr("noyra@example.com")},
            settings={"account_id": "noyra@example.com", "recipient": "helper@example.com"},
        ),
        actor="operator",
    )
    assert manager._help_wait(manager.projects()[0]) is True
    request = store.pending()[0]
    request_id = str(request["request_id"])
    with database.connection() as connection:
        root_id = str(
            connection.execute(
                "SELECT interaction_id FROM interactions WHERE subject_id=? AND idempotency_key=?",
                (case.subject_id, "project-assistance:" + request_id),
            ).fetchone()[0]
        )
    sends: list[dict[str, Any]] = []

    def smtp_send(
        endpoint: str,
        credentials: dict[str, Any],
        recipient: str,
        content: str,
        key: str,
        authorize_send: Any = None,
        **kwargs: Any,
    ) -> str:
        if authorize_send is not None:
            authorize_send()
        sends.append({"recipient": recipient, "content": content, **kwargs})
        reference = DeliveryDispatcher._smtp_message_id(key)
        if unknown:
            raise DeliveryOutcomeUnknown("receipt unavailable", provider_message_id=reference)
        return reference

    def handle(request: httpx.Request) -> httpx.Response:
        sends.append(json.loads(request.content))
        if fail_once and len(sends) == 1:
            raise httpx.ConnectError("No request reached the peer", request=request)
        return httpx.Response(200, json={"message_id": "help-ref-" + str(len(sends))})

    monkeypatch.setattr(DeliveryDispatcher, "_send_email", staticmethod(smtp_send))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    dispatcher = DeliveryDispatcher(database, transports, client=client)
    if unknown and channel == "webhook":

        async def uncertain_send(*args: Any, **kwargs: Any) -> str:
            raise DeliveryOutcomeUnknown("receipt unavailable", provider_message_id="help-unknown")

        monkeypatch.setattr(dispatcher, "_send", uncertain_send)
    try:
        deliveries = await dispatcher.deliver_pending(case.subject_id)
    finally:
        await dispatcher.aclose()
        await client.aclose()
    assert len(deliveries) == 1
    assert deliveries[0].status == (
        "unknown" if unknown else "failed" if fail_once else "delivered"
    )
    worker.expression.gateway = case.gateway(
        [
            {"disposition": "continue", "reason": "This source is enough for project review."},
        ]
    )[0]
    return SimpleNamespace(
        manager=manager,
        worker=worker,
        store=store,
        transports=transports,
        transport=transport,
        request_id=request_id,
        root_id=root_id,
        delivery=deliveries[0],
        sends=sends,
    )


def reply(contact: Any, number: int = 1, **changes: Any) -> InboundEnvelope:
    values = {
        "channel": contact.transport.channel,
        "transport_id": contact.transport.transport_id,
        "provider_event_id": f"<help-reply-{number}@helper.example.com>",
        "external_account_id": "noyra@example.com",
        "external_sender_id": "helper@example.com",
        "conversation_id": "helper@example.com",
        "content": "Use the public results index.",
        "thread_id": contact.delivery.provider_message_id,
    }
    values.update(changes)
    return InboundEnvelope.model_validate(values)


def revise_destination(database: Database, transport_id: str, **changes: str) -> None:
    # Simulate a valid operator configuration revision with its complete hash,
    # rather than bypassing TransportStore's integrity validation.
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM interaction_transports WHERE transport_id=?", (transport_id,)
        ).fetchone()
        config = dict(TransportStore._config(row))
        config.update(changes)
        state = {
            key: row[key]
            for key in (
                "transport_id",
                "subject_id",
                "channel",
                "label",
                "endpoint",
                "endpoint_contract",
                "endpoint_digest",
                "secret_reference",
                "status",
            )
        }
        state["config"] = config
        connection.execute(
            "UPDATE interaction_transports SET config_json=?,state_hash=? WHERE transport_id=?",
            (canonical_json(config), content_hash(state), transport_id),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["email", "webhook"])
async def test_external_help_reply_reaches_consideration_without_operator_history(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
    channel: Literal["email", "webhook"],
) -> None:
    database = case.kernel.database
    operator = InteractionStore(database).receive(
        case.subject_id, "web", "web-user", "Private operator conversation must stay private."
    )
    contact = await external_help(case, monkeypatch, channel)
    assert contact.store.public() == []  # No public publication is required for direct delivery.
    inbound = InboundStore(Database(database.path, initialize=False))
    message = inbound.ingest(reply(contact))
    duplicate = inbound.ingest(reply(contact))
    assert duplicate.interaction_id == message.interaction_id
    assert duplicate.duplicate is True
    incoming = InteractionStore(database).get(message.interaction_id)
    assert incoming.status == "offered"
    assert incoming.related_interaction_id is None
    assert (
        ConversationStore(database).authorized_history(case.subject_id, incoming.interaction_id)
        == []
    )
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM private_conversation_messages WHERE interaction_id=?",
                (incoming.interaction_id,),
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM participant_message_links WHERE interaction_id=?",
                (contact.root_id,),
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='assistance_reply'",
            ).fetchone()[0]
            == 1
        )
    assert contact.store.replies(contact.request_id) == []
    assert contact.manager._help_wait(contact.manager.projects()[0]) is True
    InteractionStore(database).decide(
        incoming.interaction_id,
        InteractionDecision(disposition="accepted", rationale="Consider this suggested source"),
    )
    assert await contact.worker.run_due() == "assistance_reply_considered"
    assert await contact.worker.run_due() is None
    assert contact.manager._help_wait(contact.manager.projects()[0]) is False
    # Assistance only unlocks review; the blocked project is not silently activated.
    assert contact.manager.projects()[0].status == "blocked"
    assert InteractionStore(database).get(operator.interaction_id).content.startswith("Private")
    InteractionIntegrity(database).verify(case.subject_id)
    EventStore(database).verify_chain(case.subject_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["account", "sender", "transport", "reference", "missing"])
async def test_external_help_identity_mismatch_never_links_a_request(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    contact = await external_help(case, monkeypatch)
    changes: dict[str, Any] = {}
    if mismatch == "account":
        changes["external_account_id"] = "other@example.com"
    elif mismatch == "sender":
        changes.update(
            external_sender_id="stranger@example.com", conversation_id="stranger@example.com"
        )
    elif mismatch == "transport":
        alternate = contact.transports.configure(
            case.subject_id,
            TransportInput(
                channel="webhook",
                label="Other source",
                endpoint="https://other.example.com/send",
                settings={"account_id": "noyra@example.com"},
            ),
            actor="operator",
        )
        changes["transport_id"] = alternate.transport_id
    else:
        changes["thread_id"] = None if mismatch == "missing" else "wrong-ref"
    result = InboundStore(case.kernel.database).ingest(reply(contact, **changes))
    InteractionStore(case.kernel.database).decide(
        result.interaction_id,
        InteractionDecision(disposition="accepted", rationale="Ordinary invitation"),
    )
    with case.kernel.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='assistance_reply'",
            ).fetchone()[0]
            == 0
        )
    assert contact.store.replies(contact.request_id) == []
    assert contact.manager._help_wait(contact.manager.projects()[0]) is True


@pytest.mark.asyncio
async def test_external_help_old_reply_keeps_its_frozen_route_after_recipient_change(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact = await external_help(case, monkeypatch)
    # The operational recipient can change; it is not historical delivery evidence.
    database = case.kernel.database
    revise_destination(database, contact.transport.transport_id, recipient="new-helper@example.com")
    received = InboundStore(database).ingest(reply(contact))
    with database.connection() as connection:
        evidence = connection.execute(
            "SELECT * FROM events WHERE event_type='assistance_reply'",
        ).fetchone()
        assert (
            EventStore(database).payload_from_row(evidence, connection=connection)["request_id"]
            == contact.request_id
        )
        route = AssistanceRoutes(database, case.subject_id).read(
            connection, contact.delivery.delivery_id
        )
        assert route is not None and route["recipient"] == "helper@example.com"
    impostor = InboundStore(database).ingest(
        reply(
            contact,
            2,
            external_sender_id="new-helper@example.com",
            conversation_id="new-helper@example.com",
        )
    )
    assert ParticipantStore(database).history(case.subject_id, impostor.interaction_id) == []
    assert InteractionStore(database).get(received.interaction_id).status == "offered"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["email", "webhook"])
async def test_unknown_help_receipt_can_link_but_is_never_resent_or_settled(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
    channel: Literal["email", "webhook"],
) -> None:
    contact = await external_help(case, monkeypatch, channel, unknown=True)
    accepted = InboundStore(case.kernel.database).ingest(reply(contact))
    with case.kernel.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='assistance_reply'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT status FROM interaction_deliveries WHERE delivery_id=?",
                (contact.delivery.delivery_id,),
            ).fetchone()[0]
            == "unknown"
        )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("unknown resent"))
    )
    dispatcher = DeliveryDispatcher(case.kernel.database, contact.transports, client=client)
    try:
        assert await dispatcher.deliver_pending(case.subject_id) == []
    finally:
        await dispatcher.aclose()
        await client.aclose()
    assert InteractionStore(case.kernel.database).get(accepted.interaction_id).status == "offered"


@pytest.mark.asyncio
async def test_external_help_reply_link_failure_rolls_back_and_retry_is_idempotent(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact = await external_help(case, monkeypatch)
    inbound = InboundStore(case.kernel.database)
    envelope = reply(contact)
    with (
        patch.object(
            AssistanceStore,
            "_put_reply_index_connection",
            side_effect=RuntimeError("link interrupted"),
        ),
        pytest.raises(RuntimeError, match="link interrupted"),
    ):
        inbound.ingest(envelope)
    with case.kernel.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM interactions WHERE direction='incoming'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='assistance_reply'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM participant_message_links").fetchone()[0] == 0
        )
    received = inbound.ingest(envelope)
    duplicate = inbound.ingest(envelope)
    assert duplicate.interaction_id == received.interaction_id
    with case.kernel.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='assistance_reply'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["rejected", "silent"])
async def test_declined_external_help_reply_does_not_unlock_project(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
    disposition: Literal["rejected", "silent"],
) -> None:
    contact = await external_help(case, monkeypatch)
    received = InboundStore(case.kernel.database).ingest(reply(contact))
    InteractionStore(case.kernel.database).decide(
        received.interaction_id,
        InteractionDecision(disposition=disposition, rationale="Do not use this suggestion"),
    )
    contact.worker.expression.gateway = case.gateway(
        [{"approved": False, "reason": "Keep this request on the direct channel."}]
    )[0]
    assert await contact.worker.run_due() == "assistance_publication_declined"
    with case.kernel.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM events WHERE event_type='assistance_reply_considered'"
            ).fetchone()
            is None
        )
    assert contact.manager._help_wait(contact.manager.projects()[0]) is True


@pytest.mark.asyncio
async def test_external_help_route_and_reply_index_survive_real_cold_archiving(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact = await external_help(case, monkeypatch)
    database = case.kernel.database
    key = base64.urlsafe_b64encode(bytes(range(32))).decode()
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", key)
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID", "ar04-cold")

    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> FutureDateTime:
            future = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(future.timestamp(), tz)

    archive = EventPayloadArchive(
        database, database.path.parent / "subject" / "cold", subject_id=case.subject_id
    )
    with patch("noyra.core.event_archive.datetime", FutureDateTime):
        assert archive.archive_cold(case.subject_id, older_than_days=90) > 0
    with database.connection() as connection:
        event = connection.execute(
            "SELECT payload_json,payload_archive_key FROM events WHERE event_id=?",
            (AssistanceRoutes.event_id(contact.delivery.delivery_id),),
        ).fetchone()
        assert event[0] == "{}" and event[1] is not None
    received = InboundStore(Database(database.path, initialize=False)).ingest(reply(contact))
    InteractionStore(database).decide(
        received.interaction_id,
        InteractionDecision(disposition="accepted", rationale="Consider this source"),
    )
    with patch("noyra.core.event_archive.datetime", FutureDateTime):
        assert archive.archive_cold(case.subject_id, older_than_days=90) > 0
    assert await contact.worker.run_due() == "assistance_reply_considered"
    assert contact.manager._help_wait(contact.manager.projects()[0]) is False
    EventStore(database).verify_chain(case.subject_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["email", "webhook"])
async def test_external_help_three_rounds_keep_request_and_private_history(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
    channel: Literal["email", "webhook"],
) -> None:
    contact = await external_help(case, monkeypatch, channel)
    database = case.kernel.database
    contact.worker.expression.gateway = case.gateway(
        [
            {"disposition": disposition, "reason": "A clarification is still needed."}
            for disposition in ("wait", "wait", "continue")
        ]
    )[0]
    expected: list[str] = []
    for number in range(1, 4):
        text = f"Here is clarification number {number}."
        received = InboundStore(Database(database.path, initialize=False)).ingest(
            reply(contact, number, content=text)
        )
        history = ParticipantStore(database).history(case.subject_id, received.interaction_id)
        assert [item["content"] for item in history] == expected
        assert (
            ConversationStore(database).authorized_history(case.subject_id, received.interaction_id)
            == []
        )
        expected.append(text)
        InteractionStore(database).decide(
            received.interaction_id,
            InteractionDecision(disposition="accepted", rationale="Consider the clarification"),
        )
        assert await contact.worker.run_due() == "assistance_reply_considered"
        assert contact.manager._help_wait(contact.manager.projects()[0]) is (number < 3)
        if number == 3:
            break
        outgoing = InteractionStore(database).send(
            case.subject_id,
            channel,
            "helper@example.com",
            f"Please clarify point {number}.",
            related_interaction_id=received.interaction_id,
            idempotency_key=f"help-followup-{number}",
        )
        expected.append(outgoing.content)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _, round_number=number: httpx.Response(
                    200, json={"message_id": "followup-" + str(round_number)}
                )
            )
        )
        dispatcher = DeliveryDispatcher(database, contact.transports, client=client)
        try:
            deliveries = await dispatcher.deliver_pending(case.subject_id)
        finally:
            await dispatcher.aclose()
            await client.aclose()
        assert len(deliveries) == 1 and deliveries[0].status == "delivered"
        contact.delivery = deliveries[0]
    with database.connection() as connection:
        linked = connection.execute(
            "SELECT * FROM events WHERE event_type='assistance_reply'"
        ).fetchall()
        assert len(linked) == 3
        for row in linked:
            assert (
                EventStore(database).payload_from_row(row, connection=connection)["request_id"]
                == contact.request_id
            )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='assistance_reply_considered'"
            ).fetchone()[0]
            == 3
        )
    InteractionIntegrity(database).verify(case.subject_id)
    EventStore(database).verify_chain(case.subject_id)


@pytest.mark.asyncio
async def test_known_failed_help_delivery_retries_same_destination_after_settings_change(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact = await external_help(case, monkeypatch, fail_once=True)
    database = case.kernel.database
    revise_destination(database, contact.transport.transport_id, recipient="new-helper@example.com")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE interaction_deliveries SET next_retry_at=NULL WHERE delivery_id=?",
            (contact.delivery.delivery_id,),
        )
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"message_id": "retry-delivered"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    dispatcher = DeliveryDispatcher(database, contact.transports, client=client)
    try:
        delivered = await dispatcher.deliver_pending(case.subject_id)
    finally:
        await dispatcher.aclose()
        await client.aclose()
    assert len(delivered) == 1 and delivered[0].status == "delivered", [
        (d.status, d.last_error) for d in delivered
    ]
    assert len(requests) == 1
    assert requests[0]["conversation_id"] == "helper@example.com"
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='assistance_delivery_route'"
            ).fetchone()[0]
            == 1
        )
    contact.delivery = delivered[0]
    incoming = InboundStore(database).ingest(reply(contact))
    InteractionStore(database).decide(
        incoming.interaction_id,
        InteractionDecision(disposition="accepted", rationale="Consider this answer"),
    )
    assert len(contact.store.replies(contact.request_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["legacy_missing", "ambiguous", "revoked", "account_changed"])
async def test_unproven_or_revoked_external_help_delivery_does_not_gain_linkage(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    contact = await external_help(case, monkeypatch)
    database = case.kernel.database
    if damage == "legacy_missing":
        # A real legacy delivery has no route event. Preserve existing events
        # and give the fixture its own exact reference, not a mocked reader.
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO interaction_deliveries SELECT ?,interaction_id,subject_id,"
                "transport_id,?,'delivered',?,attempts,last_error,next_retry_at,"
                "created_at,updated_at "
                "FROM interaction_deliveries WHERE delivery_id=?",
                (
                    "delivery_legacy_help",
                    "legacy-help",
                    "legacy-help-ref",
                    contact.delivery.delivery_id,
                ),
            )
        contact.delivery = SimpleNamespace(provider_message_id="legacy-help-ref")
    elif damage == "ambiguous":
        other = InteractionStore(database).send(
            case.subject_id,
            "webhook",
            "helper@example.com",
            "An unrelated message.",
            idempotency_key="ambiguous-help-receipt",
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"message_id": contact.delivery.provider_message_id}
                )
            )
        )
        dispatcher = DeliveryDispatcher(database, contact.transports, client=client)
        try:
            assert (await dispatcher.deliver_pending(case.subject_id))[0].status == "delivered"
        finally:
            await dispatcher.aclose()
            await client.aclose()
        assert other.interaction_id != contact.root_id
    elif damage == "revoked":
        InboundStore(database).bind(
            case.subject_id,
            contact.transport.transport_id,
            external_account_id="noyra@example.com",
            external_sender_id="helper@example.com",
            role="participant",
            label="Helper",
        )
        with database.transaction() as connection:
            connection.execute(
                "UPDATE interaction_bindings SET status='revoked' WHERE transport_id=?",
                (contact.transport.transport_id,),
            )
        with pytest.raises(PermissionError):
            InboundStore(database).ingest(reply(contact))
        return
    else:
        revise_destination(
            database, contact.transport.transport_id, account_id="different-receiver@example.com"
        )
    received = InboundStore(database).ingest(reply(contact))
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM events WHERE event_type='assistance_reply'"
            ).fetchone()
            is None
        )
    assert ParticipantStore(database).history(case.subject_id, received.interaction_id) == []


@pytest.mark.asyncio
async def test_same_correspondent_new_help_request_keeps_independent_private_history(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact = await external_help(case, monkeypatch)
    database = case.kernel.database
    first = InboundStore(database).ingest(reply(contact, content="Answer to the first question."))
    InteractionStore(database).decide(
        first.interaction_id,
        InteractionDecision(disposition="accepted", rationale="Consider this first answer"),
    )
    assert await contact.worker.run_due() == "assistance_reply_considered"
    project = contact.manager.projects()[0]
    phase = contact.manager.phases(project.project_id)[0]
    with database.transaction() as connection:
        contact.manager._create_assistance_request_connection(
            connection,
            project,
            phase,
            SimpleNamespace(
                help_title="A different question",
                help_description="Private background",
                help_public_summary="Please explain a different public fact.",
            ),
            case.clock_value,
        )
    assert contact.manager._help_wait(contact.manager.projects()[0]) is True
    new_request = str(contact.store.pending()[0]["request_id"])
    assert new_request != contact.request_id
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"message_id": "second-help-root"})
        )
    )
    dispatcher = DeliveryDispatcher(database, contact.transports, client=client)
    try:
        sent = await dispatcher.deliver_pending(case.subject_id)
    finally:
        await dispatcher.aclose()
        await client.aclose()
    assert len(sent) == 1 and sent[0].status == "delivered"
    second = InboundStore(database).ingest(
        reply(
            contact,
            2,
            thread_id=sent[0].provider_message_id,
            content="Answer to the second question.",
        )
    )
    assert ParticipantStore(database).history(case.subject_id, second.interaction_id) == []
    with database.connection() as connection:
        link = connection.execute(
            "SELECT * FROM events WHERE event_id=?",
            ("evt_help_reply_" + content_hash(second.interaction_id),),
        ).fetchone()
        assert (
            EventStore(database).payload_from_row(link, connection=connection)["request_id"]
            == new_request
        )
    # A late first-request answer must not be attributed to the new request.
    late = InboundStore(database).ingest(reply(contact, 3, content="Late first-question detail."))
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM events WHERE event_id=?",
                ("evt_help_reply_" + content_hash(late.interaction_id),),
            ).fetchone()
            is None
        )
    assert (
        ParticipantStore(database).history(case.subject_id, late.interaction_id)[0]["content"]
        == "Answer to the first question."
    )
    InteractionIntegrity(database).verify(case.subject_id)
    contact.manager.verify_integrity()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
async def test_reconciled_failed_help_delivery_cannot_parent_a_late_reply(
    case: projects_base.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Literal["failed", "cancelled"],
) -> None:
    contact = await external_help(case, monkeypatch, "webhook", unknown=True)
    dispatcher = DeliveryDispatcher(case.kernel.database, contact.transports)
    dispatcher.reconcile_unknown(
        contact.delivery.delivery_id,
        outcome,
        actor="operator",
        reason="Provider confirmed no delivery",
    )
    incoming = InboundStore(case.kernel.database).ingest(reply(contact))
    record = InteractionStore(case.kernel.database).get(incoming.interaction_id)
    assert record.status == "offered"
    assert record.related_interaction_id is None
    assert contact.store.replies(contact.request_id) == []
    with case.kernel.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='assistance_reply'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT status FROM autonomous_project_assistance_requests WHERE request_id=?",
                (contact.request_id,),
            ).fetchone()[0]
            == "open"
        )
    assert contact.manager._help_wait(contact.manager.projects()[0]) is True
    contact.manager.verify_integrity()
    InteractionIntegrity(case.kernel.database).verify(case.subject_id)
