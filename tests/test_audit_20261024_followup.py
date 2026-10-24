from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from urllib.request import HTTPCookieProcessor

import pytest

from noyra.cognition.social import RelationshipSocialCognition
from noyra.core import SubjectKernel
from noyra.interaction import InboundStore, TransportStore
from noyra.interaction.accounts import AccountError, AccountStore
from noyra.interaction.inbound import InboundAccepted
from noyra.interaction.participants import Participant
from noyra.interaction.transport import TransportRecord
from noyra.model import CompletionRequest, FakeProvider, ProviderResponse
from noyra.service import NoyraHTTPServer
from test_audit_20261024_remediation import kernel as base_kernel
from test_participant_http import Client
from test_participant_http import http as participant_http_fixture


@pytest.fixture
def participant_http(tmp_path: Path) -> Iterator[tuple[NoyraHTTPServer, str]]:
    yield from cast(Any, participant_http_fixture).__wrapped__(tmp_path)


@pytest.mark.parametrize("revocation", ["recover", "password", "logout"])
@pytest.mark.parametrize("revoke_key", [False, True])
def test_credential_operation_cannot_cross_session_revocation(
    participant_http: tuple[NoyraHTTPServer, str],
    revocation: str,
    revoke_key: bool,
) -> None:
    owner, base = participant_http
    store = AccountStore(owner.kernel.database, owner.kernel.subject_id)
    registration = store.register("alice", "correct horse battery", "Alice")
    client = Client(base)
    client.login("alice")
    arrived, release = threading.Event(), threading.Event()
    throttle = AccountStore.throttle

    def controlled(self: AccountStore, client_id: str, operation: str, limit: int = 12) -> None:
        throttle(self, client_id, operation, limit)
        if operation == "writes":
            arrived.set()
            assert release.wait(15)

    with (
        patch.object(AccountStore, "throttle", controlled),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        pending = pool.submit(
            client.request, "/api/participants/credential", {"revoke": revoke_key}
        )
        try:
            assert arrived.wait(15)
            if revocation == "recover":
                store.reset("alice", registration["recovery_code"], "new correct horse battery")
            elif revocation == "password":
                token, csrf, _ = store.login("alice", "correct horse battery")
                who = store.authenticate(token, csrf=csrf)
                assert who
                store.change_password(who, "correct horse battery", "new correct horse battery")
            else:
                old_token = next(
                    cookie.value
                    for handler in cast(Any, client.opener).handlers
                    if isinstance(handler, HTTPCookieProcessor)
                    for cookie in handler.cookiejar
                    if cookie.name == "noyra_participant"
                )
                assert isinstance(old_token, str)
                store.logout(old_token)
            # Simulate a fresh, legitimately authorized credential minted after
            # recovery. A stale revoke must not remove it either.
            password = (
                "correct horse battery" if revocation == "logout" else "new correct horse battery"
            )
            token, csrf, _ = store.login("alice", password)
            who = store.authenticate(token, csrf=csrf)
            assert who
            fresh = store.api_key(who, session_token=token, csrf=csrf)
            assert fresh
        finally:
            release.set()
        status, body = pending.result(timeout=15)
    assert status == 401 and body == {"error": "invalid_credentials"}
    assert store.authenticate(fresh, api=True)
    with store.database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM participant_api_credentials").fetchone()[0] == 1


def test_credential_transaction_checks_expiry_csrf_account_and_disabled_entity(
    participant_http: tuple[NoyraHTTPServer, str],
) -> None:
    owner, _ = participant_http
    store = AccountStore(owner.kernel.database, owner.kernel.subject_id)
    store.register("alice", "correct horse battery", "Alice")
    store.register("bob", "correct horse battery", "Bob")
    token, csrf, _ = store.login("alice", "correct horse battery")
    other, other_csrf, _ = store.login("bob", "correct horse battery")
    who = store.authenticate(token, csrf=csrf)
    assert who
    for candidate, code in [(token, "wrong"), (other, other_csrf)]:
        with pytest.raises(AccountError, match="invalid_credentials"):
            store.api_key(who, session_token=candidate, csrf=code)
    key = store.api_key(who, session_token=token, csrf=csrf)
    assert key and store.authenticate(key, api=True)
    with store.database.transaction() as c:
        c.execute(
            "UPDATE participant_sessions SET expires_at=0 WHERE subject_id=?", (store.subject,)
        )
    with pytest.raises(AccountError, match="invalid_credentials"):
        store.api_key(who, session_token=token, csrf=csrf)
    token, csrf, _ = store.login("alice", "correct horse battery")
    with store.database.transaction() as c:
        c.execute(
            "UPDATE participant_entities SET status='disabled' WHERE entity_id=?", (who.entity_id,)
        )
    with pytest.raises(AccountError, match="invalid_credentials"):
        store.api_key(who, session_token=token, csrf=csrf)


def test_credential_committed_before_reset_is_revoked(
    participant_http: tuple[NoyraHTTPServer, str],
) -> None:
    owner, _ = participant_http
    store = AccountStore(owner.kernel.database, owner.kernel.subject_id)
    registration = store.register("alice", "correct horse battery", "Alice")
    token, csrf, _ = store.login("alice", "correct horse battery")
    who = store.authenticate(token, csrf=csrf)
    assert who
    key = store.api_key(who, session_token=token, csrf=csrf)
    assert key and store.authenticate(key, api=True)
    store.reset("alice", registration["recovery_code"], "new correct horse battery")
    assert store.authenticate(key, api=True) is None


# Reuse the valid synthetic subject fixture; all model responses remain local.
@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    yield from cast(Any, base_kernel).__wrapped__(tmp_path)


def social_fixture(
    owner: SubjectKernel,
) -> tuple[
    RelationshipSocialCognition,
    FakeProvider,
    Callable[..., InboundAccepted],
    TransportStore,
    InboundStore,
    TransportRecord,
    InboundAccepted,
    Participant,
    Callable[..., ProviderResponse],
]:
    import json

    from pydantic import SecretStr

    from noyra.cognition import CognitionSettings
    from noyra.cognition.social import RelationshipSocialCognition
    from noyra.interaction import InboundEnvelope, InboundStore, TransportInput, TransportStore
    from noyra.interaction.participants import ParticipantStore
    from noyra.mind import RelationshipStore
    from noyra.model import (
        BudgetLimits,
        FakeProvider,
        ModelGateway,
        ModelLedger,
        ModelPricing,
        ModelUsage,
        ProviderResponse,
    )

    transports = TransportStore(owner.database, owner.database.path.parent / "secrets")
    bot = transports.configure(
        owner.subject_id,
        TransportInput(
            channel="webhook",
            label="Original bot",
            endpoint="https://example.test/first",
            credentials={"webhook_secret": SecretStr("synthetic")},
        ),
        actor="operator",
    )
    inbound = InboundStore(owner.database)
    inbound.bind(
        owner.subject_id,
        bot.transport_id,
        external_account_id="account",
        external_sender_id="alice",
    )

    def receive(room: str, event: str, transport: TransportRecord = bot) -> InboundAccepted:
        result = inbound.ingest(
            InboundEnvelope(
                channel="webhook",
                transport_id=transport.transport_id,
                provider_event_id=event,
                external_account_id="account",
                external_sender_id="alice",
                sender_label="Alice",
                conversation_id=room,
                content="Invitation in " + room,
            )
        )

        assert isinstance(result, InboundAccepted)
        return result

    first = receive("original-room", "first")
    who = ParticipantStore(owner.database).for_message(owner.subject_id, first.interaction_id)
    assert who
    with owner.database.connection() as c:
        event = c.execute(
            "SELECT event_id FROM events WHERE json_extract(payload_json,'$.interaction_id')=?",
            (first.interaction_id,),
        ).fetchone()[0]
    relationship = RelationshipStore(owner.database).ensure(
        owner.subject_id,
        who.relationship_type,
        who.entity_id,
        who.display_name,
        source_event_ids=(event,),
    )

    def response(
        disposition: str = "contact", content: str | None = "Draft for original room"
    ) -> ProviderResponse:
        return ProviderResponse(
            json.dumps(
                {
                    "disposition": disposition,
                    "relationship_id": relationship.relationship_id,
                    "topic": "A voluntary followup",
                    "rationale": "I choose this disposition",
                    "content": content,
                    "evidence_event_ids": [event],
                }
            ),
            ModelUsage(100, 100),
        )

    provider = FakeProvider([response(), response(content="New decision for new room")])
    gateway = ModelGateway(
        provider,
        ModelLedger(owner.database),
        model="test-model",
        limits=BudgetLimits(20, 500000, 100000, 5000000),
        pricing=ModelPricing(),
    )
    social = RelationshipSocialCognition(
        owner.database, owner.subject_id, gateway, CognitionSettings()
    )
    return social, provider, receive, transports, inbound, bot, first, who, response


@pytest.mark.asyncio
@pytest.mark.parametrize("new_bot", [False, True])
async def test_social_route_stays_bound_to_model_conversation(
    kernel: SubjectKernel, new_bot: bool
) -> None:
    from pydantic import SecretStr

    from noyra.interaction import DeliveryDispatcher, TransportInput
    from noyra.interaction.integrity import InteractionIntegrity
    from noyra.interaction.participants import ParticipantStore, source_namespace

    social, provider, receive, transports, inbound, bot, first, who, _ = social_fixture(kernel)
    alternate = bot
    if new_bot:
        alternate = transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="webhook",
                label="Other bot",
                endpoint="https://example.test/second",
                credentials={"webhook_secret": SecretStr("other")},
            ),
            actor="operator",
        )
        inbound.bind(
            kernel.subject_id,
            alternate.transport_id,
            external_account_id="account",
            external_sender_id="alice",
        )
        with kernel.database.transaction() as c:
            ParticipantStore.source(
                c,
                kernel.subject_id,
                "webhook",
                source_namespace("webhook", alternate.transport_id, "account"),
                "alice",
                entity_id=who.entity_id,
            )
    complete = provider.complete

    async def concurrent_incoming(request: CompletionRequest) -> ProviderResponse:
        assert (
            "original-room" in request.messages[1].content
            and "new-room" not in request.messages[1].content
        )
        receive("new-room", "second", alternate)
        return await complete(request)

    with patch.object(provider, "complete", side_effect=concurrent_incoming):
        assert await social.run_due() == "relationship_social_contact"
    assert DeliveryDispatcher(kernel.database, transports).enqueue_pending(kernel.subject_id) == 1
    with kernel.database.connection() as c:
        row = c.execute(
            "SELECT i.counterparty,i.related_interaction_id,d.transport_id FROM interactions i "
            "JOIN interaction_deliveries d ON d.interaction_id=i.interaction_id"
        ).fetchone()
        assert tuple(row) == ("original-room", first.interaction_id, bot.transport_id)
    InteractionIntegrity(kernel.database).verify(kernel.subject_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("withdraw", ["binding", "transport", "entity"])
async def test_social_route_revocation_rejects_without_rerouting(
    kernel: SubjectKernel, withdraw: str
) -> None:
    social, provider, _receive, transports, inbound, bot, _first, who, _ = social_fixture(kernel)
    complete = provider.complete

    async def revoked_during_model(request: CompletionRequest) -> ProviderResponse:
        if withdraw == "binding":
            binding = inbound.list_bindings(kernel.subject_id)[0]["binding_id"]
            inbound.change_binding(
                binding,
                kernel.subject_id,
                status="disabled",
                actor="operator",
                reason="Synthetic revocation",
            )
        elif withdraw == "transport":
            transports.disable(
                bot.transport_id,
                subject_id=kernel.subject_id,
                reason="Synthetic revocation",
                actor="operator",
            )
        else:
            with kernel.database.transaction() as c:
                c.execute(
                    "UPDATE participant_entities SET status='disabled' WHERE entity_id=?",
                    (who.entity_id,),
                )
        return await complete(request)

    with patch.object(provider, "complete", side_effect=revoked_during_model):
        assert await social.run_due() == "relationship_social_rejected"
    with kernel.database.connection() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM interactions WHERE direction='outgoing'").fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_social_recovery_never_reuses_old_room_proposal_for_new_route(
    kernel: SubjectKernel,
) -> None:
    from noyra.interaction import InteractionStore

    social, provider, receive, _, _, _, _, _, _ = social_fixture(kernel)
    with (
        patch.object(social, "_commit", side_effect=RuntimeError("synthetic crash after response")),
        pytest.raises(RuntimeError),
    ):
        await social.run_due()
    newer = receive("new-room", "after-crash")
    assert await social.run_due() == "relationship_social_contact"
    assert len(provider.requests) == 2
    out = next(
        r
        for r in InteractionStore(kernel.database).list(kernel.subject_id)
        if r.direction == "outgoing"
    )
    assert out.related_interaction_id == newer.interaction_id
    assert out.content == "New decision for new room"


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["wait", "respect_distance", "contact"])
async def test_legacy_entity_prefix_is_not_an_internal_identity(
    kernel: SubjectKernel, disposition: str
) -> None:
    import json

    from noyra.cognition import CognitionSettings
    from noyra.cognition.social import RelationshipSocialCognition
    from noyra.interaction import InteractionStore
    from noyra.interaction.integrity import InteractionIntegrity
    from noyra.mind import RelationshipStore
    from noyra.model import (
        BudgetLimits,
        FakeProvider,
        ModelGateway,
        ModelLedger,
        ModelUsage,
        ProviderResponse,
    )

    incoming = InteractionStore(kernel.database).receive(
        kernel.subject_id, "webhook", "entity_legacy_peer", "Legacy invitation"
    )
    with kernel.database.connection() as c:
        event = c.execute(
            "SELECT event_id FROM events WHERE json_extract(payload_json,'$.interaction_id')=?",
            (incoming.interaction_id,),
        ).fetchone()[0]
    relationship = RelationshipStore(kernel.database).ensure(
        kernel.subject_id, "human", "entity_legacy_peer", "Legacy peer", source_event_ids=(event,)
    )
    provider = FakeProvider(
        [
            ProviderResponse(
                json.dumps(
                    {
                        "disposition": disposition,
                        "relationship_id": relationship.relationship_id,
                        "topic": "Legacy",
                        "rationale": "Voluntary decision",
                        "content": "Legacy contact" if disposition == "contact" else None,
                        "evidence_event_ids": [event],
                    }
                ),
                ModelUsage(100, 100),
            )
        ]
    )
    gateway = ModelGateway(
        provider,
        ModelLedger(kernel.database),
        model="test-model",
        limits=BudgetLimits(20, 500000, 100000, 5000000),
    )
    social = RelationshipSocialCognition(
        kernel.database, kernel.subject_id, gateway, CognitionSettings()
    )
    assert await social.run_due() == "relationship_social_" + disposition
    assert await social.run_due() is None
    InteractionIntegrity(kernel.database).verify(kernel.subject_id)


@pytest.mark.asyncio
async def test_social_unknown_cannot_be_bypassed_by_new_conversation(kernel: SubjectKernel) -> None:
    from noyra.model.errors import ProviderCallError

    social, provider, receive, _, _, _, _, _, _ = social_fixture(kernel)
    provider._outcomes.clear()
    provider._outcomes.append(
        ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True)
    )
    assert await social.run_due() == "relationship_social_model_failed"
    receive("new-room", "after-unknown")
    assert await social.run_due() == "relationship_social_model_failed"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_social_recovery_same_route_reuses_successful_response(kernel: SubjectKernel) -> None:
    social, provider, _, _, _, _, _, _, _ = social_fixture(kernel)
    with (
        patch.object(social, "_commit", side_effect=RuntimeError("synthetic crash")),
        pytest.raises(RuntimeError),
    ):
        await social.run_due()
    assert await social.run_due() == "relationship_social_contact"
    assert len(provider.requests) == 1
