from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import SecretStr

import test_c0_expression_store as expression_base
from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash, new_id, utc_now
from noyra.interaction import (
    DeliveryDispatcher,
    InboundStore,
    InteractionStore,
    TransportInput,
    TransportRecord,
    TransportStore,
)
from noyra.interaction.integrity import InteractionIntegrity
from noyra.interaction.participants import ParticipantStore, source_namespace
from noyra.interaction.types import InteractionDecision
from test_b1_email_identity import gateway_mail, parse, raw_mail
from test_b1_email_identity import mail_case as mail_case


def configure(
    case: expression_base.ExpressionStoreTestCase, *, label: str = "mail ingress", **settings: Any
) -> TransportRecord:
    return case.transports.configure(
        case.subject,
        TransportInput(
            channel="email",
            label=label,
            endpoint="smtps://mail.example.com:465",
            credentials={
                "from_address": SecretStr("noyra@example.com"),
                "webhook_secret": SecretStr("synthetic-gateway-secret"),
            },
            settings={"account_id": "noyra@example.com", **settings},
        ),
        actor="operator",
    )


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def send(
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
        calls.append({"recipient": recipient, "content": content, **kwargs})
        return DeliveryDispatcher._smtp_message_id(key)

    monkeypatch.setattr(DeliveryDispatcher, "_send_email", staticmethod(send))
    return calls


@pytest.mark.asyncio
async def test_signed_protocol_establishes_private_thread_and_replies_with_original_header(
    mail_case: expression_base.ExpressionStoreTestCase,
    smtp: list[dict[str, Any]],
) -> None:
    case = mail_case
    transport = configure(case)
    interactions = InteractionStore(case.db)
    interactions.send(
        case.subject,
        "email",
        "helper@example.com",
        "Public opening question",
        idempotency_key="expression-message:b1-opening",
    )
    dispatcher = DeliveryDispatcher(case.db, case.transports)
    try:
        first = (await dispatcher.deliver_pending(case.subject))[0]
        raw = raw_mail(
            reference=first.provider_message_id, message_id="<real-incoming@example.com>"
        )
        envelope = parse(gateway_mail(raw), transport.transport_id)
        accepted = InboundStore(case.db).ingest(envelope)
        assert [
            row["content"]
            for row in ParticipantStore(case.db).history(case.subject, accepted.interaction_id)
        ] == ["Public opening question"]
        interactions.decide(
            accepted.interaction_id,
            InteractionDecision(disposition="accepted", rationale="Continue privately"),
        )
        interactions.send(
            case.subject,
            "email",
            "helper@example.com",
            "Private followup",
            related_interaction_id=accepted.interaction_id,
            idempotency_key="b1-followup",
        )
        followup = (await dispatcher.deliver_pending(case.subject))[0]
        assert followup.status == "delivered"
        assert smtp[-1]["thread_id"] == "<real-incoming@example.com>"
        assert smtp[-1]["thread_id"] != envelope.provider_event_id
        reply_raw = raw_mail(
            reference=followup.provider_message_id, message_id="<second@example.com>"
        )
        verified = InboundStore(Database(case.db.path, initialize=False)).ingest(
            parse(
                gateway_mail(reply_raw, event="second", method="mailbox_challenge"),
                transport.transport_id,
            )
        )
        assert "Private followup" in [
            row["content"]
            for row in ParticipantStore(case.db).history(case.subject, verified.interaction_id)
        ]
        forged = InboundStore(case.db).ingest(
            parse(
                raw_mail(reference=followup.provider_message_id, message_id="<forged@example.com>"),
                transport.transport_id,
            )
        )
        assert ParticipantStore(case.db).history(case.subject, forged.interaction_id) == []
        assert interactions.get(forged.interaction_id).status == "offered"
        InteractionIntegrity(case.db).verify(case.subject)
    finally:
        await dispatcher.aclose()


def test_unverified_mail_is_open_but_not_a_stable_identity_or_creator(
    mail_case: expression_base.ExpressionStoreTestCase,
) -> None:
    case = mail_case
    transport = configure(case)
    store = InboundStore(case.db)
    store.bind(
        case.subject,
        transport.transport_id,
        external_account_id="noyra@example.com",
        external_sender_id="helper@example.com",
        role="creator",
    )
    first = store.ingest(parse(raw_mail(message_id="<one@example.com>"), transport.transport_id))
    second = store.ingest(parse(raw_mail(message_id="<two@example.com>"), transport.transport_id))
    assert first.scheduling_priority == second.scheduling_priority == 0
    participants = ParticipantStore(case.db)
    one = participants.for_message(case.subject, first.interaction_id)
    two = participants.for_message(case.subject, second.interaction_id)
    assert one is not None and two is not None and one.entity_id != two.entity_id
    assert InteractionStore(case.db).get(first.interaction_id).status == "offered"
    verified = store.ingest(
        parse(gateway_mail(raw_mail(message_id="<one@example.com>")), transport.transport_id)
    )
    assert verified.scheduling_priority == 100
    with case.db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM private_conversation_messages").fetchone()[0] == 0


def test_verified_mailbox_remains_stable_across_proof_methods_but_not_transports(
    mail_case: expression_base.ExpressionStoreTestCase,
) -> None:
    case = mail_case
    transport = configure(case)
    another = configure(case, label="another-mail")
    entities = []
    for index, (route, method) in enumerate(
        (
            (transport, "provider_mailbox"),
            (transport, "mailbox_challenge"),
            (another, "provider_mailbox"),
        )
    ):
        accepted = InboundStore(case.db).ingest(
            parse(gateway_mail(raw_mail(), event=str(index), method=method), route.transport_id)
        )
        who = ParticipantStore(case.db).for_message(case.subject, accepted.interaction_id)
        assert who is not None
        entities.append(who.entity_id)
    assert entities[0] == entities[1] and entities[1] != entities[2]


def test_reusing_id_with_changed_content_or_proof_remains_an_explicit_conflict(
    mail_case: expression_base.ExpressionStoreTestCase,
) -> None:
    case = mail_case
    transport = configure(case)
    store = InboundStore(case.db)
    original = gateway_mail(raw_mail(), event="reused")
    first = store.ingest(parse(original, transport.transport_id))
    duplicate = store.ingest(parse(original, transport.transport_id))
    assert duplicate.duplicate and duplicate.interaction_id == first.interaction_id
    for body in (
        gateway_mail(raw_mail(text="different body"), event="reused"),
        gateway_mail(raw_mail(), event="reused", method="mailbox_challenge"),
    ):
        with pytest.raises(ValueError, match=r"different (content|evidence)"):
            store.ingest(parse(body, transport.transport_id))
    with case.db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM email_ingress_evidence").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM interaction_inbound_events").fetchone()[0] == 1


def test_same_raw_message_id_different_senders_and_unverified_first_cannot_preempt_verified(
    mail_case: expression_base.ExpressionStoreTestCase,
) -> None:
    case = mail_case
    transport = configure(case)
    store = InboundStore(case.db)
    results = [
        store.ingest(parse(body, transport.transport_id))
        for body in (raw_mail(sender="alice@example.com"), raw_mail(), gateway_mail(raw_mail()))
    ]
    assert len({item.interaction_id for item in results}) == 3


def test_rejected_receive_retries_with_original_evidence_and_no_partial_participant(
    mail_case: expression_base.ExpressionStoreTestCase,
) -> None:
    case = mail_case
    transport = configure(case)
    envelope = parse(gateway_mail(raw_mail()), transport.transport_id)
    with (
        patch.object(
            InteractionStore, "receive", side_effect=RuntimeError("synthetic receive failure")
        ),
        pytest.raises(RuntimeError, match="synthetic receive failure"),
    ):
        InboundStore(case.db).ingest(envelope)
    with case.db.connection() as c:
        assert (
            c.execute("SELECT status FROM interaction_inbound_events").fetchone()[0] == "rejected"
        )
        assert c.execute("SELECT COUNT(*) FROM participant_sources").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM email_ingress_evidence").fetchone()[0] == 1
    accepted = InboundStore(case.db).ingest(envelope)
    duplicate = InboundStore(case.db).ingest(envelope)
    assert accepted.interaction_id == duplicate.interaction_id and duplicate.duplicate
    InteractionIntegrity(case.db).verify(case.subject)


@pytest.mark.asyncio
async def test_parallel_first_replies_commit_one_stable_private_conversation(
    mail_case: expression_base.ExpressionStoreTestCase,
    smtp: list[dict[str, Any]],
) -> None:
    case = mail_case
    transport = configure(case)
    InteractionStore(case.db).send(
        case.subject,
        "email",
        "helper@example.com",
        "Public opening",
        idempotency_key="expression-message:b1-parallel",
    )
    dispatcher = DeliveryDispatcher(case.db, case.transports)
    try:
        delivery = (await dispatcher.deliver_pending(case.subject))[0]
    finally:
        await dispatcher.aclose()
    envelopes = [
        parse(
            gateway_mail(
                raw_mail(
                    reference=delivery.provider_message_id,
                    message_id=f"<reply-{index}@example.com>",
                ),
                event=f"parallel-{index}",
            ),
            transport.transport_id,
        )
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        received = list(
            pool.map(
                lambda envelope: InboundStore(Database(case.db.path, initialize=False)).ingest(
                    envelope
                ),
                envelopes,
            )
        )
    with case.db.connection() as c:
        links = c.execute(
            "SELECT DISTINCT entity_id,conversation_key,visibility FROM participant_message_links"
        ).fetchall()
        assert len(links) == 1 and links[0]["visibility"] == "private"
        assert c.execute("SELECT COUNT(*) FROM email_ingress_evidence").fetchone()[0] == 2
    for result in received:
        history = ParticipantStore(case.db).history(case.subject, result.interaction_id)
        assert history[0]["content"] == "Public opening"
    InteractionIntegrity(case.db).verify(case.subject)


@pytest.mark.parametrize("damage", ["delete", "hash", "subject"])
def test_missing_or_tampered_new_evidence_fails_closed(
    mail_case: expression_base.ExpressionStoreTestCase,
    damage: str,
) -> None:
    case = mail_case
    transport = configure(case)
    envelope = parse(gateway_mail(raw_mail()), transport.transport_id)
    InboundStore(case.db).ingest(envelope)
    with case.db.transaction() as c:
        if damage == "delete":
            c.execute("DELETE FROM email_ingress_evidence")
        else:
            c.execute("DROP TRIGGER prevent_email_ingress_evidence_update")
            if damage == "hash":
                c.execute("UPDATE email_ingress_evidence SET state_hash=?", ("0" * 64,))
    if damage == "subject":
        IdentityStore(case.db).ensure("other-mail-subject", content_hash("other"))
        with case.db.transaction() as c:
            c.execute("UPDATE email_ingress_evidence SET subject_id='other-mail-subject'")
    with pytest.raises(IntegrityError, match="email identity evidence"):
        InboundStore(case.db).ingest(envelope)
    with pytest.raises(IntegrityError, match="email identity evidence"):
        InteractionIntegrity(case.db).verify(case.subject)


def legacy_database(tmp_path: Path) -> Any:
    path = tmp_path / "legacy.sqlite3"
    with patch("noyra.core.database.CURRENT_SCHEMA_VERSION", 112):
        database = Database(path)
    subject = "legacy-mail-subject"
    IdentityStore(database).ensure(subject, content_hash(subject))
    transports = TransportStore(database, tmp_path / "legacy-secrets")
    transport = transports.configure(
        subject,
        TransportInput(
            channel="email",
            label="legacy",
            endpoint="smtps://mail.example.com:465",
            settings={"account_id": "noyra@example.com"},
            credentials={"from_address": SecretStr("noyra@example.com")},
        ),
        actor="operator",
    )
    inbound = InboundStore(database)
    inbound.bind(
        subject,
        transport.transport_id,
        external_account_id="noyra@example.com",
        external_sender_id="helper@example.com",
    )
    store = InteractionStore(database)
    old_source = source_namespace("email", transport.transport_id, "noyra@example.com")
    incoming = store.receive(
        subject,
        "email",
        "helper@example.com",
        "Legacy private input",
        participant_source=(old_source, "helper@example.com", "helper"),
        participant_conversation="legacy-private",
        participant_private=True,
        idempotency_key="legacy-input",
    )
    legacy_event = new_id("inevt")
    with database.transaction() as c:
        c.execute(
            "INSERT INTO interaction_inbound_events(event_id,subject_id,transport_id,channel,"
            "provider_event_id,external_account_id,external_sender_id,conversation_id,content_hash,"
            "scheduling_priority,status,received_at,processed_at,interaction_id,reply_context_version)"
            " VALUES(?,?,?,'email',?,'noyra@example.com','helper@example.com','legacy-private',?,0,"
            "'received',?,NULL,NULL,1)",
            (
                legacy_event,
                subject,
                transport.transport_id,
                "mail-v1:" + "a" * 64,
                content_hash(incoming.content),
                utc_now(),
            ),
        )
        c.execute(
            "UPDATE interaction_inbound_events SET status='processed',processed_at=?,"
            "interaction_id=? "
            "WHERE event_id=?",
            (utc_now(), incoming.interaction_id, legacy_event),
        )
    store.decide(
        incoming.interaction_id,
        InteractionDecision(disposition="accepted", rationale="Legacy decision"),
    )
    outgoing = store.send(
        subject,
        "email",
        "helper@example.com",
        "Legacy private followup",
        related_interaction_id=incoming.interaction_id,
        idempotency_key="legacy-followup",
    )
    return SimpleNamespace(
        database=database,
        subject=subject,
        transports=transports,
        transport=transport,
        incoming=incoming,
        outgoing=outgoing,
    )


@pytest.mark.asyncio
async def test_old_schema_upgrade_preserves_data_without_attesting_or_reusing_legacy_history(
    tmp_path: Path,
    smtp: list[dict[str, Any]],
) -> None:
    legacy = legacy_database(tmp_path)
    dispatcher = DeliveryDispatcher(legacy.database, legacy.transports)
    try:
        assert dispatcher.enqueue_pending(legacy.subject) == 1
    finally:
        await dispatcher.aclose()
    with legacy.database.transaction() as c:
        c.execute(
            "UPDATE interaction_deliveries SET status='delivered',attempts=1,"
            "provider_message_id='<legacy-followup@noyra.example>'"
        )
        before = [tuple(row) for row in c.execute("SELECT * FROM interactions ORDER BY rowid")]
        assert (
            c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            == "112"
        )
    upgraded = Database(legacy.database.path, strict_schema_contract=True)
    with upgraded.connection() as c:
        assert [
            tuple(row) for row in c.execute("SELECT * FROM interactions ORDER BY rowid")
        ] == before
        assert c.execute("SELECT COUNT(*) FROM email_ingress_evidence").fetchone()[0] == 0
    assert ParticipantStore(upgraded).history(legacy.subject, legacy.incoming.interaction_id) == []
    body = gateway_mail(raw_mail(reference="<legacy-followup@noyra.example>"))
    accepted = InboundStore(upgraded).ingest(parse(body, legacy.transport.transport_id))
    assert ParticipantStore(upgraded).history(legacy.subject, accepted.interaction_id) == []
    assert InteractionStore(upgraded).get(accepted.interaction_id).related_interaction_id is None
    assert smtp == []
    InteractionIntegrity(upgraded).verify(legacy.subject)


@pytest.mark.asyncio
async def test_queued_legacy_private_reply_is_held_before_any_network_send(
    tmp_path: Path,
    smtp: list[dict[str, Any]],
) -> None:
    legacy = legacy_database(tmp_path)
    upgraded = Database(legacy.database.path)
    dispatcher = DeliveryDispatcher(upgraded, legacy.transports)
    try:
        deliveries = await dispatcher.deliver_pending(legacy.subject)
    finally:
        await dispatcher.aclose()
    assert len(deliveries) == 1 and deliveries[0].status == "failed"
    assert smtp == []


def test_http_ingress_verifies_signed_assertion_and_actual_receiving_account(
    mail_case: expression_base.ExpressionStoreTestCase,
) -> None:
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from noyra.service import NoyraHTTPServer, ServiceSettings
    from test_b1_email_identity import signed

    case = mail_case
    settings = ServiceSettings(
        data_dir=case.root,
        subject_id=case.subject,
        genesis_hash=content_hash("c0"),
        host="127.0.0.1",
        port=0,
        integrity_mode="off",
    )
    server = NoyraHTTPServer(case.kernel, settings)
    transport = server.transports.configure(
        case.subject,
        TransportInput(
            channel="email",
            label="http-mail",
            endpoint="smtps://mail.example.com:465",
            settings={"account_id": "noyra@example.com"},
            credentials={"webhook_secret": SecretStr("synthetic-gateway-secret")},
        ),
        actor="operator",
    )
    server.start()

    def post(body: bytes) -> tuple[int, dict[str, Any]]:
        request = Request(
            f"http://127.0.0.1:{server.address[1]}/webhooks/email/{transport.transport_id}",
            data=body,
            headers={"Content-Type": "application/json", **signed(body)},
        )
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    try:
        code, _ = post(gateway_mail(raw_mail(), account="wrong@example.com"))
        assert code in {400, 401, 403}
        with case.db.connection() as c:
            assert c.execute("SELECT COUNT(*) FROM interaction_inbound_events").fetchone()[0] == 0
        code, result = post(gateway_mail(raw_mail()))
        assert code == 200
        assert result.get("interaction_id")
        InteractionIntegrity(case.db).verify(case.subject)
    finally:
        server.close()


def test_identity_evidence_cannot_be_promoted_or_rewritten_in_place(
    mail_case: expression_base.ExpressionStoreTestCase,
) -> None:
    case = mail_case
    transport = configure(case)
    InboundStore(case.db).ingest(parse(raw_mail(), transport.transport_id))
    with (
        pytest.raises(sqlite3.IntegrityError, match="evidence is immutable"),
        case.db.transaction() as c,
    ):
        c.execute("UPDATE email_ingress_evidence SET verification='provider_mailbox'")
    InteractionIntegrity(case.db).verify(case.subject)


@pytest.mark.parametrize("verified", [False, True])
def test_cognition_can_distinguish_claimed_mailbox_from_proven_mailbox(
    mail_case: expression_base.ExpressionStoreTestCase,
    verified: bool,
) -> None:
    from noyra.cognition.interaction import InteractionCognition
    from noyra.cognition.settings import CognitionSettings
    from noyra.model import BudgetLimits, FakeProvider, ModelGateway, ModelLedger

    case = mail_case
    transport = configure(case)
    body = gateway_mail(raw_mail()) if verified else raw_mail()
    accepted = InboundStore(case.db).ingest(parse(body, transport.transport_id))
    gateway = ModelGateway(
        FakeProvider([]),
        ModelLedger(case.db),
        model="synthetic",
        limits=BudgetLimits(10, 100_000, 100_000, 1_000_000),
    )
    worker = InteractionCognition(case.db, case.subject, gateway, CognitionSettings())
    messages = worker._messages(InteractionStore(case.db).get(accepted.interaction_id))
    metadata_text = (
        messages[1]
        .content.split("INTERACTION_METADATA_JSON\n", 1)[1]
        .split("\nBEGIN_UNTRUSTED_HUMAN_MESSAGE", 1)[0]
    )
    metadata = json.loads(metadata_text)
    assert metadata["email_identity"]["mailbox_verified"] is verified
    assert metadata["email_identity"]["claimed_mailbox"] == "helper@example.com"
    assert metadata["participant"]["authentication"] == ("credential" if verified else "channel")


@pytest.mark.asyncio
async def test_reply_rechecks_evidence_immediately_before_smtp_send(
    mail_case: expression_base.ExpressionStoreTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = mail_case
    transport = configure(case)
    accepted = InboundStore(case.db).ingest(parse(gateway_mail(raw_mail()), transport.transport_id))
    interactions = InteractionStore(case.db)
    interactions.decide(
        accepted.interaction_id,
        InteractionDecision(disposition="accepted", rationale="Public answer"),
    )
    interactions.send(
        case.subject,
        "email",
        "helper@example.com",
        "A new reply",
        related_interaction_id=accepted.interaction_id,
        idempotency_key="late-evidence-check",
    )
    calls: list[str] = []

    def send(
        endpoint: str,
        credentials: dict[str, Any],
        recipient: str,
        content: str,
        key: str,
        authorize_send: Any = None,
        **kwargs: Any,
    ) -> str:
        with case.db.transaction() as c:
            c.execute("DELETE FROM email_ingress_evidence")
        assert authorize_send is not None
        authorize_send()
        calls.append(recipient)
        return DeliveryDispatcher._smtp_message_id(key)

    monkeypatch.setattr(DeliveryDispatcher, "_send_email", staticmethod(send))
    dispatcher = DeliveryDispatcher(case.db, case.transports)
    try:
        deliveries = await dispatcher.deliver_pending(case.subject)
    finally:
        await dispatcher.aclose()
    assert len(deliveries) == 1 and deliveries[0].status == "failed"
    assert calls == []


def test_optional_known_sender_restriction_does_not_trust_familiar_unverified_from(
    mail_case: expression_base.ExpressionStoreTestCase,
) -> None:
    case = mail_case
    transport = configure(case, require_known_senders=True)
    store = InboundStore(case.db)
    store.bind(
        case.subject,
        transport.transport_id,
        external_account_id="noyra@example.com",
        external_sender_id="helper@example.com",
    )
    with pytest.raises(PermissionError, match="verified sender"):
        store.ingest(parse(raw_mail(), transport.transport_id))
    accepted = store.ingest(parse(gateway_mail(raw_mail()), transport.transport_id))
    assert InteractionStore(case.db).get(accepted.interaction_id).status == "offered"


@pytest.mark.asyncio
async def test_case_insensitive_account_domain_preserves_verified_direct_reply_link(
    mail_case: expression_base.ExpressionStoreTestCase,
    smtp: list[dict[str, Any]],
) -> None:
    case = mail_case
    transport = configure(case, account_id="noyra@EXAMPLE.COM")
    InteractionStore(case.db).send(
        case.subject,
        "email",
        "helper@example.com",
        "Opening",
        idempotency_key="expression-message:b1-canonical-account",
    )
    dispatcher = DeliveryDispatcher(case.db, case.transports)
    try:
        delivered = (await dispatcher.deliver_pending(case.subject))[0]
    finally:
        await dispatcher.aclose()
    accepted = InboundStore(case.db).ingest(
        parse(
            gateway_mail(raw_mail(reference=delivered.provider_message_id)), transport.transport_id
        )
    )
    assert [
        row["content"]
        for row in ParticipantStore(case.db).history(case.subject, accepted.interaction_id)
    ] == ["Opening"]


def test_unverified_source_cooling_is_bounded_without_blocking_existing_retries(
    mail_case: expression_base.ExpressionStoreTestCase,
) -> None:
    from noyra.interaction.participants import ParticipantStore

    case = mail_case
    transport = configure(case)
    store = InboundStore(case.db)
    envelope = parse(raw_mail(message_id="<existing@example.com>"), transport.transport_id)
    first = store.ingest(envelope)
    with case.db.transaction() as c:
        for index in range(99):
            ParticipantStore.source(
                c,
                case.subject,
                "email",
                f"synthetic-unverified-{index}",
                "other@example.com",
                authentication="channel",
            )
    assert store.ingest(envelope).interaction_id == first.interaction_id
    with pytest.raises(PermissionError, match="cooling down"):
        store.ingest(parse(raw_mail(message_id="<next@example.com>"), transport.transport_id))
    verified = store.ingest(parse(gateway_mail(raw_mail()), transport.transport_id))
    assert InteractionStore(case.db).get(verified.interaction_id).status == "offered"


def test_duplicate_concurrent_verified_event_creates_one_interaction_and_evidence(
    mail_case: expression_base.ExpressionStoreTestCase,
) -> None:
    case = mail_case
    transport = configure(case)
    envelope = parse(gateway_mail(raw_mail()), transport.transport_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        received = list(
            pool.map(
                lambda _: InboundStore(Database(case.db.path, initialize=False)).ingest(envelope),
                range(2),
            )
        )
    assert len({result.interaction_id for result in received}) == 1
    assert any(result.duplicate for result in received)
    with case.db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM interaction_inbound_events").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM email_ingress_evidence").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM participant_sources").fetchone()[0] == 1
    InteractionIntegrity(case.db).verify(case.subject)


def test_email_identity_migration_failure_restores_the_complete_old_database(
    tmp_path: Path,
) -> None:
    from noyra.core.schema.email_identity import EMAIL_IDENTITY_SCHEMA

    legacy = legacy_database(tmp_path)
    original = Database._execute_sql_script

    def fail_email_schema(connection: sqlite3.Connection, script: str) -> None:
        if script == EMAIL_IDENTITY_SCHEMA:
            raise RuntimeError("synthetic email identity migration failure")
        original(connection, script)

    with (
        patch.object(Database, "_execute_sql_script", staticmethod(fail_email_schema)),
        pytest.raises(RuntimeError, match="synthetic email identity migration failure"),
    ):
        Database(legacy.database.path)
    with legacy.database.connection() as c:
        assert (
            c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            == "112"
        )
        assert "email_identity_version" not in {
            row[1] for row in c.execute("PRAGMA table_info(interaction_inbound_events)")
        }
        assert (
            c.execute("SELECT 1 FROM sqlite_schema WHERE name='email_ingress_evidence'").fetchone()
            is None
        )
        assert (
            c.execute(
                "SELECT content FROM interactions WHERE interaction_id=?",
                (legacy.incoming.interaction_id,),
            ).fetchone()[0]
            == "Legacy private input"
        )
    upgraded = Database(legacy.database.path, strict_schema_contract=True)
    InteractionIntegrity(upgraded).verify(legacy.subject)
