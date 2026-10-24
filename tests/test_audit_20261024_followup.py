from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from urllib.request import HTTPCookieProcessor

import pytest

from noyra.cognition import InteractionCognition
from noyra.cognition.social import RelationshipSocialCognition
from noyra.core import SubjectKernel
from noyra.interaction import InboundStore, TransportStore
from noyra.interaction.accounts import AccountError, AccountStore
from noyra.interaction.inbound import InboundAccepted
from noyra.interaction.participants import Participant
from noyra.interaction.transport import TransportRecord
from noyra.interaction.types import InteractionRecord
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


def unknown_fixture(
    owner: SubjectKernel, tmp_path: Path, routed: bool
) -> tuple[
    InteractionCognition,
    FakeProvider,
    InteractionRecord,
    InteractionRecord,
    Callable[[Participant, str], InteractionRecord],
    Participant,
    Participant,
]:
    from noyra.cognition import CognitionSettings, InteractionCognition
    from noyra.interaction import InteractionStore
    from noyra.model import BudgetLimits, FakeProvider, ModelGateway, ModelLedger, ModelPricing
    from noyra.model.errors import ProviderCallError
    from noyra.model.resources import RoutedModelGateway
    from test_audit_20261022_remediation import resources_for
    from test_audit_20261024_remediation import cognition

    _prototype, queued = cognition(owner, 5)
    # Reuse only a valid synthetic response, not a precomputed result.
    provider = FakeProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            *list(queued._outcomes),
        ]
    )
    gateway: ModelGateway
    if routed:
        resources, _ = resources_for(owner.database, owner.subject_id, tmp_path)
        resources_for(owner.database, owner.subject_id, tmp_path, label="Independent route")
        gateway = RoutedModelGateway(
            owner.database, owner.subject_id, resources, provider_factory=lambda _: provider
        )
    else:
        gateway = ModelGateway(
            provider,
            ModelLedger(owner.database),
            model="test-model",
            limits=BudgetLimits(20, 500000, 100000, 5000000),
            pricing=ModelPricing(),
        )
    accounts = AccountStore(owner.database, owner.subject_id)
    accounts.register("alice", "correct horse battery", "Alice")
    accounts.register("bob", "correct horse battery", "Bob")
    alice = accounts.authenticate(accounts.login("alice", "correct horse battery")[0])
    bob = accounts.authenticate(accounts.login("bob", "correct horse battery")[0])
    assert alice and bob
    interactions = InteractionStore(owner.database)

    def receive(who: Participant, key: str) -> InteractionRecord:
        return interactions.receive(
            owner.subject_id,
            "participant",
            who.entity_id,
            "Independent invitation " + key,
            idempotency_key=key,
            participant=who,
            participant_private=True,
            participant_conversation="participant:" + who.entity_id,
        )

    first = receive(alice, "first")
    second = receive(bob, "second")
    engine = InteractionCognition(
        owner.database,
        owner.subject_id,
        gateway,
        CognitionSettings(interaction_cooldown_seconds=0, max_interaction_model_calls_per_day=20),
    )
    return engine, provider, first, second, receive, alice, bob


@pytest.mark.asyncio
@pytest.mark.parametrize("routed", [False, True])
async def test_unknown_round_yields_to_independent_messages_without_releasing_evidence(
    kernel: SubjectKernel, tmp_path: Path, routed: bool
) -> None:
    from noyra.cognition import InteractionCognition
    from noyra.interaction import InteractionStore

    engine, provider, first, second, receive, alice, _ = unknown_fixture(kernel, tmp_path, routed)
    assert await engine.run_due() == "interaction_model_failed"
    with kernel.database.connection() as c:
        call = dict(c.execute("SELECT * FROM model_calls").fetchone())
        attempt = dict(c.execute("SELECT * FROM model_attempts").fetchone())
    # Recreate the worker and cross the scheduling day without changing evidence.
    restarted = InteractionCognition(
        kernel.database,
        kernel.subject_id,
        engine.gateway,
        engine.settings,
        clock=lambda: "2099-01-01T00:00:00.000+00:00",
    )
    assert due_id(restarted) == second.interaction_id
    assert await restarted.run_due() == "interaction_silent"
    same_contact = receive(alice, "later-same-contact")
    assert await restarted.run_due() == "interaction_silent"
    assert InteractionStore(kernel.database).get(same_contact.interaction_id).status == "silent"
    assert await restarted.run_due() is None
    assert len(provider.requests) == 3
    with kernel.database.connection() as c:
        assert (
            dict(
                c.execute(
                    "SELECT * FROM model_calls WHERE call_id=?", (call["call_id"],)
                ).fetchone()
            )
            == call
        )
        assert (
            dict(
                c.execute(
                    "SELECT * FROM model_attempts WHERE attempt_id=?", (attempt["attempt_id"],)
                ).fetchone()
            )
            == attempt
        )
    assert InteractionStore(kernel.database).get(first.interaction_id).status == "offered"


@pytest.mark.asyncio
@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("resolution", ["retry", "succeeded", "failed"])
async def test_explicit_unknown_resolution_resumes_only_original_business_round(
    kernel: SubjectKernel, tmp_path: Path, routed: bool, resolution: str
) -> None:

    engine, provider, first, second, _, _, _ = unknown_fixture(kernel, tmp_path, routed)
    assert await engine.run_due() == "interaction_model_failed"
    with kernel.database.connection() as c:
        call_id = c.execute("SELECT call_id FROM model_calls").fetchone()[0]
    if resolution == "retry":
        engine.gateway.ledger.prepare_unknown_retry(
            call_id, actor="operator", reason="Synthetic explicit retry"
        )
    else:
        evidence = None
        if resolution == "succeeded":
            outcome = provider._outcomes[0]
            assert isinstance(outcome, ProviderResponse)
            evidence = {"content": outcome.content}
        engine.gateway.ledger.reconcile_unknown(
            call_id,
            outcome=resolution,
            actor="operator",
            reason="Synthetic provider evidence",
            response=evidence,
        )
    assert due_id(engine) == first.interaction_id
    with resource_cooldown_elapsed():
        assert await engine.run_due() == "interaction_silent"
    assert len(provider.requests) == (1 if resolution == "succeeded" else 2)
    with kernel.database.connection() as c:
        rows = c.execute(
            "SELECT call_id,idempotency_key FROM model_calls WHERE purpose=?",
            ("interaction_cognition:" + first.interaction_id + ":0",),
        ).fetchall()
        if resolution == "retry":
            assert len(rows) == 1 and rows[0]["call_id"] == call_id
    assert due_id(engine) == second.interaction_id


@pytest.mark.asyncio
async def test_consumed_failed_unknown_retry_stays_isolated_until_closed(
    kernel: SubjectKernel, tmp_path: Path
) -> None:
    from noyra.model.errors import ProviderCallError

    engine, provider, first, second, _, _, _ = unknown_fixture(kernel, tmp_path, True)
    assert await engine.run_due() == "interaction_model_failed"
    with kernel.database.connection() as c:
        call_id = c.execute("SELECT call_id FROM model_calls").fetchone()[0]
    engine.gateway.ledger.prepare_unknown_retry(
        call_id, actor="operator", reason="One explicit retry"
    )
    provider._outcomes.appendleft(
        ProviderCallError("synthetic_known_failure", retryable=False, outcome_unknown=False)
    )
    with resource_cooldown_elapsed():
        assert await engine.run_due() == "interaction_model_failed"
    assert due_id(engine) == second.interaction_id
    engine.gateway.ledger.close_failed_unknown_retry(
        call_id,
        actor="operator",
        reason="Synthetic manual reconciliation",
        resolution_evidence="synthetic-provider-record",
        accept_duplicate_risk=True,
    )
    assert due_id(engine) == first.interaction_id


def due_id(engine: InteractionCognition) -> str | None:
    record = engine._due_interaction()
    return record.interaction_id if record is not None else None


@contextmanager
def resource_cooldown_elapsed() -> Iterator[None]:
    future = datetime.now(UTC) + timedelta(days=1)

    class Future(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Future:
            return cls.fromtimestamp(future.timestamp(), tz)

    # Advance natural route cooldowns without changing any ledger/unknown row.
    with (
        patch("noyra.core.provider_health.datetime", Future),
        patch("noyra.model.resources.utc_now", lambda: future.isoformat(timespec="milliseconds")),
    ):
        yield


@pytest.mark.asyncio
async def test_unapproved_prepared_and_executing_rounds_do_not_block_queue(
    kernel: SubjectKernel, tmp_path: Path
) -> None:
    from noyra.core.types import content_hash

    engine, provider, first, second, _, _, _ = unknown_fixture(kernel, tmp_path, True)
    purpose = "interaction_cognition:" + first.interaction_id + ":0"
    call, _ = engine.gateway.ledger.prepare_call(
        kernel.subject_id,
        "fake",
        "test-model",
        purpose,
        content_hash("synthetic prepared request"),
        "synthetic-prepared",
        resource_pool="economy",
    )
    assert due_id(engine) == second.interaction_id
    assert len(provider.requests) == 0
    # A separate executing call must also stay fenced while preserving reservation.
    from noyra.model import BudgetLimits

    attempt = engine.gateway.ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(20, 500000, 100000, 5000000),
        reserved_input_tokens=100,
        reserved_output_tokens=100,
        reserved_cost_microusd=0,
    )
    engine.gateway.ledger.start_attempt(attempt.attempt_id)
    assert due_id(engine) == second.interaction_id
    with kernel.database.connection() as c:
        assert (
            c.execute(
                "SELECT status FROM model_attempts WHERE attempt_id=?", (attempt.attempt_id,)
            ).fetchone()[0]
            == "executing"
        )


@pytest.mark.asyncio
async def test_cancelled_unknown_retry_is_not_a_permanent_scheduler_block(
    kernel: SubjectKernel, tmp_path: Path
) -> None:
    engine, provider, first, _, _, _, _ = unknown_fixture(kernel, tmp_path, True)
    assert await engine.run_due() == "interaction_model_failed"
    with kernel.database.connection() as c:
        call_id = c.execute("SELECT call_id FROM model_calls").fetchone()[0]
    engine.gateway.ledger.prepare_unknown_retry(
        call_id, actor="operator", reason="One intentional retry"
    )
    engine.gateway.ledger.reconcile_prepared_failed(
        call_id, actor="operator", reason="Cancel unused retry"
    )
    assert due_id(engine) == first.interaction_id
    with resource_cooldown_elapsed():
        assert await engine.run_due() == "interaction_silent"
    assert len(provider.requests) == 2
