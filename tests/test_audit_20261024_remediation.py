from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.cognition import CognitionSettings, InteractionCognition
from noyra.cognition.settings import WorldSourceConfig
from noyra.core import SubjectKernel
from noyra.core.types import content_hash
from noyra.interaction import InteractionStore
from noyra.interaction.accounts import AccountStore
from noyra.interaction.collaboration import ParticipantCollaboration
from noyra.interaction.participants import Participant
from noyra.mind.memory import MAX_RECALL_QUERY_CHARS, MemoryStore
from noyra.model import (
    BudgetLimits,
    CompletionRequest,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelPricing,
    ModelUsage,
    ProviderResponse,
)
from noyra.wallet import BountyRecord


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    instance = SubjectKernel(
        tmp_path / "data" / "noyra.sqlite3", "audit-20261024", content_hash("audit")
    )
    instance.boot()
    instance.orient()
    instance.activate()
    try:
        yield instance
    finally:
        instance.close()


def cognition(kernel: SubjectKernel, count: int = 2) -> tuple[InteractionCognition, FakeProvider]:
    proposal = json.dumps(
        {
            "disposition": "silent",
            "rationale": "I choose to remain silent.",
            "response": None,
            "appraisal": {
                "novelty": 0.4,
                "goal_congruence": 0.0,
                "controllability": 0.5,
                "certainty": 0.8,
                "agency": "external correspondent",
                "narrative": "An invitation.",
            },
            "affect_impulses": [],
        }
    )
    provider = FakeProvider(
        [ProviderResponse(content=proposal, usage=ModelUsage(100, 100)) for _ in range(count)]
    )
    gateway = ModelGateway(
        provider,
        ModelLedger(kernel.database),
        model="test-model",
        limits=BudgetLimits(20, 500000, 100000, 5000000),
        pricing=ModelPricing(),
    )
    return InteractionCognition(
        kernel.database,
        kernel.subject_id,
        gateway,
        CognitionSettings(
            enabled=True,
            sources=(
                WorldSourceConfig(
                    name="Synthetic",
                    url="https://example.test/world",
                    source_type="web",
                    trust_score=0.5,
                ),
            ),
            interaction_cooldown_seconds=0,
            max_interaction_model_calls_per_day=20,
        ),
    ), provider


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [4000, 4001, 10000])
@pytest.mark.parametrize("attributed", [False, True])
async def test_long_invitation_and_followup_both_progress(
    kernel: SubjectKernel, size: int, attributed: bool
) -> None:
    accounts = AccountStore(kernel.database, kernel.subject_id)
    accounts.register("alice", "correct horse battery", "Alice")
    who = accounts.authenticate(accounts.login("alice", "correct horse battery")[0])
    assert who
    interactions = InteractionStore(kernel.database)
    body = " " * 4100 + "长" * size
    first = interactions.receive(
        kernel.subject_id,
        "participant" if attributed else "web",
        who.entity_id,
        body,
        idempotency_key="long",
        participant=who if attributed else None,
        participant_private=attributed,
        participant_conversation="participant:" + who.entity_id,
    )
    second = interactions.receive(
        kernel.subject_id,
        "participant" if attributed else "web",
        who.entity_id,
        "A normal followup",
        idempotency_key="followup",
        participant=who if attributed else None,
        participant_private=attributed,
        participant_conversation="participant:" + who.entity_id,
    )
    engine, provider = cognition(kernel)
    with patch.object(engine.memories, "recall", wraps=engine.memories.recall) as recall:
        assert await engine.run_due() == "interaction_silent"
        assert await engine.run_due() == "interaction_silent"
        if attributed:
            recall.assert_not_called()
        else:
            assert all(
                0 < len(call.args[1]) <= MAX_RECALL_QUERY_CHARS for call in recall.call_args_list
            )
    assert len(provider.requests) == 2
    assert body in provider.requests[0].messages[1].content
    assert interactions.get(first.interaction_id).content == body
    assert interactions.get(first.interaction_id).status == "silent"
    assert interactions.get(second.interaction_id).status == "silent"


def test_excluded_memories_do_not_record_access(kernel: SubjectKernel) -> None:
    accounts = AccountStore(kernel.database, kernel.subject_id)
    accounts.register("alice", "correct horse battery", "Alice")
    who = accounts.authenticate(accounts.login("alice", "correct horse battery")[0])
    assert who
    record = InteractionStore(kernel.database).receive(
        kernel.subject_id,
        "participant",
        who.entity_id,
        "memory topic",
        participant=who,
        participant_private=True,
        participant_conversation="participant:" + who.entity_id,
    )
    with kernel.database.connection() as c:
        event = c.execute(
            "SELECT event_id FROM events WHERE subject_id=? LIMIT 1", (kernel.subject_id,)
        ).fetchone()[0]
    memory = MemoryStore(kernel.database).create(
        kernel.subject_id,
        "episodic",
        "memory topic other contact secret",
        salience=1.0,
        confidence=1.0,
        source_event_ids=(event,),
    )
    engine, _ = cognition(kernel)
    with patch.object(engine.memories, "recall", side_effect=AssertionError("unscoped recall")):
        messages = engine._messages(record)
    assert "other contact secret" not in messages[1].content
    with kernel.database.connection() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM memory_accesses WHERE memory_id=?", (memory.memory_id,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("blocked_channel", ["participant", "unconfigured"])
def test_ineligible_messages_cannot_fill_delivery_batch(
    kernel: SubjectKernel, tmp_path: Path, blocked_channel: str
) -> None:
    from pydantic import SecretStr

    from noyra.interaction import DeliveryDispatcher, TransportInput, TransportStore

    store = InteractionStore(kernel.database)
    for index in range(12):
        store.send(
            kernel.subject_id,
            blocked_channel,
            "local",
            "Internal or unavailable",
            idempotency_key="blocked-" + str(index),
        )
    transports = TransportStore(kernel.database, tmp_path / "secrets")
    target = transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="webhook",
            label="Synthetic",
            endpoint="https://example.test/delivery",
            credentials={"webhook_secret": SecretStr("synthetic")},
        ),
        actor="operator",
    )
    message = store.send(
        kernel.subject_id, "webhook", "remote", "Deliver this", idempotency_key="external"
    )
    dispatcher = DeliveryDispatcher(kernel.database, transports)
    assert dispatcher.enqueue_pending(kernel.subject_id, limit=10) == 1
    assert dispatcher.enqueue_pending(kernel.subject_id, limit=10) == 0
    with kernel.database.connection() as c:
        rows = c.execute(
            "SELECT interaction_id,transport_id FROM interaction_deliveries WHERE subject_id=?",
            (kernel.subject_id,),
        ).fetchall()
        assert [tuple(row) for row in rows] == [(message.interaction_id, target.transport_id)]


def test_disabled_source_is_skipped_without_switching_bots(
    kernel: SubjectKernel, tmp_path: Path
) -> None:
    from pydantic import SecretStr

    from noyra.interaction import (
        DeliveryDispatcher,
        InboundEnvelope,
        InboundStore,
        TransportInput,
        TransportStore,
    )

    transports = TransportStore(kernel.database, tmp_path / "secrets")
    original = transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="webhook",
            label="Original",
            endpoint="https://example.test/original",
            credentials={"webhook_secret": SecretStr("original")},
        ),
        actor="operator",
    )
    other = transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="webhook",
            label="Other",
            endpoint="https://example.test/other",
            credentials={"webhook_secret": SecretStr("other")},
        ),
        actor="operator",
    )
    inbound = InboundStore(kernel.database)
    inbound.bind(
        kernel.subject_id,
        original.transport_id,
        external_account_id="account",
        external_sender_id="sender",
    )
    store = InteractionStore(kernel.database)
    blocked = []
    for index in range(11):
        incoming = inbound.ingest(
            InboundEnvelope(
                channel="webhook",
                transport_id=original.transport_id,
                provider_event_id="incoming-" + str(index),
                external_account_id="account",
                external_sender_id="sender",
                conversation_id="remote",
                content="Invitation",
            )
        )
        blocked.append(
            store.send(
                kernel.subject_id,
                "webhook",
                "remote",
                "Reply on original only",
                related_interaction_id=incoming.interaction_id,
                idempotency_key="reply-" + str(index),
            ).interaction_id
        )
    transports.disable(
        original.transport_id,
        subject_id=kernel.subject_id,
        actor="operator",
        reason="synthetic test",
    )
    eligible = store.send(
        kernel.subject_id,
        "webhook",
        "other-remote",
        "Unrelated message",
        idempotency_key="eligible",
    )
    dispatcher = DeliveryDispatcher(kernel.database, transports)
    assert dispatcher.enqueue_pending(kernel.subject_id, limit=10) == 1
    with kernel.database.connection() as c:
        rows = c.execute(
            "SELECT interaction_id,transport_id FROM interaction_deliveries"
        ).fetchall()
        assert [tuple(row) for row in rows] == [(eligible.interaction_id, other.transport_id)]
    assert all(store.get(identifier).status == "sent" for identifier in blocked)


def make_collaboration(
    kernel: SubjectKernel,
) -> tuple[ParticipantCollaboration, Participant, BountyRecord]:
    from types import SimpleNamespace

    from noyra.wallet import WalletAssetInput, WalletEconomyStore, WalletNetworkInput, WalletStore
    from noyra.wallet.workflow import WalletRewardWorkflow
    from test_wallet_economy import _bounty

    db, subject = kernel.database, kernel.subject_id
    wallets = WalletStore(db)
    network = wallets.register_network(
        subject,
        WalletNetworkInput(
            label="Synthetic", chain_id=1, native_symbol="ETH", rpc_url="https://rpc.example"
        ),
        actor="operator",
    )
    asset = wallets.register_asset(
        subject,
        WalletAssetInput(
            network_id=network.network_id,
            asset_type="native",
            name="Ether",
            symbol="ETH",
            decimals=18,
        ),
        actor="operator",
    )
    economy = WalletEconomyStore(db)
    from noyra.core import EventStore
    from noyra.mind import GoalCandidate, GoalStore

    event = EventStore(db).append(subject, "experience", "test", {"kind": "synthetic fixture"})
    goals = GoalStore(db)
    goal = goals.create_candidate(
        subject,
        GoalCandidate(
            title="Synthetic",
            description="Synthetic",
            origin="self",
            priority=0.5,
            commitment=0.5,
            motive_emotion="interest",
        ),
        causal_source_ids=(event.event_id,),
        reason="synthetic fixture",
    )
    goal = goals.activate(
        goal.goal_id, rationale="synthetic fixture", causal_source_ids=(event.event_id,)
    )
    bounty = _bounty(economy, subject, network.network_id, asset.asset_id, goal.goal_id)
    economy.publish_bounty(bounty.bounty_id, subject, actor="operator")
    accounts = AccountStore(db, subject)
    accounts.register("alice", "correct horse battery", "Alice")
    who = accounts.authenticate(accounts.login("alice", "correct horse battery")[0])
    assert who
    owner = SimpleNamespace(
        kernel=kernel, wallet_economy=economy, wallet_rewards=WalletRewardWorkflow(db)
    )
    return ParticipantCollaboration(owner), who, bounty


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_withdrawn_interest_is_not_scheduled_and_followup_still_works(
    kernel: SubjectKernel,
    legacy: bool,
) -> None:
    collaboration, who, bounty = make_collaboration(kernel)
    result = collaboration.write(
        "/api/participants/apply",
        {"bounty_id": bounty.bounty_id, "content": "I can help", "idempotency_key": "interest"},
        who,
        "127.0.0.1",
    )
    if legacy:
        with kernel.database.transaction() as c:
            c.execute(
                "UPDATE participant_applications SET status='withdrawn' WHERE application_id=?",
                (result["application_id"],),
            )
    else:
        collaboration.write(
            "/api/participants/withdraw",
            {"kind": "application", "id": result["application_id"]},
            who,
            "127.0.0.1",
        )
    assert (
        collaboration.read("/api/participants/messages", {}, who)["items"][0]["status"]
        == "withdrawn"
    )
    from noyra.interaction.integrity import InteractionIntegrity

    InteractionIntegrity(kernel.database).verify(kernel.subject_id)
    engine, provider = cognition(kernel)
    assert await engine.run_due() is None
    assert provider.requests == []
    followup = collaboration.write(
        "/api/participants/messages",
        {"content": "A later invitation", "idempotency_key": "later"},
        who,
        "127.0.0.1",
    )
    assert await engine.run_due() == "interaction_silent"
    assert InteractionStore(kernel.database).get(followup["interaction_id"]).status == "silent"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_withdrawal_during_call_preserves_accounting_without_committing_decision(
    kernel: SubjectKernel,
) -> None:
    collaboration, who, bounty = make_collaboration(kernel)
    result = collaboration.write(
        "/api/participants/apply",
        {"bounty_id": bounty.bounty_id, "content": "I can help", "idempotency_key": "interest"},
        who,
        "127.0.0.1",
    )
    engine, provider = cognition(kernel)
    complete = provider.complete

    async def withdraw_then_complete(request: CompletionRequest) -> ProviderResponse:
        collaboration.write(
            "/api/participants/withdraw",
            {"kind": "application", "id": result["application_id"]},
            who,
            "127.0.0.1",
        )
        return await complete(request)

    with patch.object(provider, "complete", side_effect=withdraw_then_complete):
        assert await engine.run_due() == "interaction_private_input_withdrawn"
    with kernel.database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM interaction_decisions").fetchone()[0] == 0
        assert c.execute("SELECT status FROM model_calls").fetchone()[0] == "succeeded"
        assert (
            c.execute("SELECT COUNT(*) FROM interactions WHERE direction='outgoing'").fetchone()[0]
            == 0
        )
    assert await engine.run_due() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["authorize_attempt", "start_attempt", "check_request_authorization"]
)
async def test_withdrawal_at_model_send_guard_cancels_unsent_attempt(
    kernel: SubjectKernel, phase: str
) -> None:
    collaboration, who, bounty = make_collaboration(kernel)
    result = collaboration.write(
        "/api/participants/apply",
        {"bounty_id": bounty.bounty_id, "content": "I can help", "idempotency_key": "interest"},
        who,
        "127.0.0.1",
    )
    engine, provider = cognition(kernel)
    check = getattr(engine.gateway.ledger, phase)

    def withdraw_before_guard(*args: Any, **kwargs: Any) -> Any:
        collaboration.write(
            "/api/participants/withdraw",
            {"kind": "application", "id": result["application_id"]},
            who,
            "127.0.0.1",
        )
        return check(*args, **kwargs)

    with patch.object(engine.gateway.ledger, phase, side_effect=withdraw_before_guard):
        outcome = await engine.run_due()
        assert outcome == (
            "interaction_model_failed"
            if phase == "check_request_authorization"
            else "interaction_private_input_withdrawn"
        )
    assert provider.requests == []
    with kernel.database.connection() as c:
        attempts = c.execute("SELECT status FROM model_attempts").fetchall()
        assert [row[0] for row in attempts] == (
            [] if phase == "authorize_attempt" else ["cancelled"]
        )
        assert c.execute("SELECT status FROM model_calls").fetchone()[0] == "failed"
    assert await engine.run_due() is None


@pytest.mark.parametrize("kind", ["apply", "submit"])
@pytest.mark.parametrize("transition", ["closed", "expired"])
def test_receipt_replay_after_bounty_closes(
    kernel: SubjectKernel, kind: str, transition: str
) -> None:
    from datetime import datetime, timedelta

    collaboration, who, bounty = make_collaboration(kernel)
    payload: dict[str, Any] = {
        "bounty_id": bounty.bounty_id,
        "content": "Evidence",
        "idempotency_key": "receipt",
    }
    if kind == "submit":
        payload.update(
            evidence=["https://example.test/evidence"],
            recipient_address="0x" + "1" * 40,
            consent_version=1,
        )
    first = collaboration.write("/api/participants/" + kind, payload, who, "127.0.0.1")
    economy = collaboration.owner.wallet_economy
    if transition == "closed":
        economy.close_bounty(bounty.bounty_id, kernel.subject_id, actor="operator")
    else:
        economy.clock = lambda: (
            datetime.fromisoformat(bounty.expires_at) + timedelta(days=1)
        ).isoformat(timespec="milliseconds")
    with kernel.database.connection() as c:
        before = c.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0]
    assert collaboration.write("/api/participants/" + kind, payload, who, "127.0.0.1") == first
    if kind == "submit":
        from noyra.wallet.economy_types import SubmissionInput

        replay = economy.submit_to_bounty(
            bounty.bounty_id,
            kernel.subject_id,
            SubmissionInput(
                counterparty=who.entity_id,
                content=payload["content"],
                evidence=payload["evidence"],
                recipient_address=payload["recipient_address"],
                consent_version=1,
                idempotency_key="participant:" + content_hash([who.entity_id, kind, "receipt"]),
            ),
        )
        assert replay.submission_id == first["submission_id"]
    with pytest.raises(ValueError):
        collaboration.write(
            "/api/participants/" + kind, {**payload, "content": "Changed"}, who, "127.0.0.1"
        )
    with kernel.database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0] == before
        assert c.execute("SELECT COUNT(*) FROM wallet_payment_orders").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kind,capacity", [("messages", "_MESSAGE_CAP"), ("apply", "_APPLICATION_CAP")]
)
def test_receipt_replay_at_capacity_and_concurrent_duplicate(
    kernel: SubjectKernel, kind: str, capacity: str
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    collaboration, who, bounty = make_collaboration(kernel)
    payload: dict[str, Any] = {"content": "One invitation", "idempotency_key": "receipt"}
    if kind == "apply":
        payload["bounty_id"] = bounty.bounty_id
    with patch("noyra.interaction.collaboration." + capacity, 1):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(
                workers.map(
                    lambda _: collaboration.write(
                        "/api/participants/" + kind, payload, who, "127.0.0.1"
                    ),
                    range(2),
                )
            )
        assert results[0] == results[1]
        assert (
            collaboration.write("/api/participants/" + kind, payload, who, "127.0.0.1")
            == results[0]
        )
        with pytest.raises(ValueError, match="capacity_reached"):
            collaboration.write(
                "/api/participants/" + kind, {**payload, "idempotency_key": "new"}, who, "127.0.0.1"
            )


def test_submission_key_cannot_be_reused_for_another_bounty(kernel: SubjectKernel) -> None:
    from test_wallet_economy import _bounty

    collaboration, who, bounty = make_collaboration(kernel)
    economy = collaboration.owner.wallet_economy
    assert bounty.goal_id is not None
    another = _bounty(
        economy,
        kernel.subject_id,
        bounty.network_id,
        bounty.asset_id,
        bounty.goal_id,
        key="another",
    )
    economy.publish_bounty(another.bounty_id, kernel.subject_id, actor="operator")
    payload: dict[str, Any] = {
        "bounty_id": bounty.bounty_id,
        "content": "Evidence",
        "idempotency_key": "receipt",
        "recipient_address": "0x" + "1" * 40,
    }
    collaboration.write("/api/participants/submit", payload, who, "127.0.0.1")
    with pytest.raises(ValueError, match="idempotency_conflict"):
        collaboration.write(
            "/api/participants/submit",
            {**payload, "bounty_id": another.bounty_id},
            who,
            "127.0.0.1",
        )


def test_message_receipt_across_browser_and_program_keeps_original_source(
    kernel: SubjectKernel,
) -> None:
    collaboration, who, _ = make_collaboration(kernel)
    payload = {"content": "Same invitation", "idempotency_key": "receipt"}
    first = collaboration.write("/api/participants/messages", payload, who, "127.0.0.1")
    accounts = AccountStore(kernel.database, kernel.subject_id)
    key = accounts.api_key(who)
    assert key
    api_who = accounts.authenticate(key, api=True)
    assert api_who and api_who.source_id != who.source_id
    assert collaboration.write("/api/participants/messages", payload, api_who, "127.0.0.1") == first
    with kernel.database.connection() as c:
        assert (
            c.execute(
                "SELECT source_id FROM participant_message_links WHERE interaction_id=?",
                (first["interaction_id"],),
            ).fetchone()[0]
            == who.source_id
        )


def test_notifications_survive_incoming_traffic_and_support_owned_deep_reads(
    kernel: SubjectKernel,
) -> None:
    collaboration, who, _ = make_collaboration(kernel)
    first = collaboration.write(
        "/api/participants/messages",
        {"content": "Hello", "idempotency_key": "first"},
        who,
        "127.0.0.1",
    )
    store = InteractionStore(kernel.database)
    reply = store.send(
        kernel.subject_id,
        "participant",
        who.entity_id,
        "Reply",
        related_interaction_id=first["interaction_id"],
        idempotency_key="reply",
    )
    for index in range(50):
        collaboration.write(
            "/api/participants/messages",
            {"content": "Incoming", "idempotency_key": "incoming-" + str(index)},
            who,
            "127.0.0.1",
        )
    assert (
        collaboration.read("/api/participants/notifications", {}, who)["items"][0]["related_id"]
        == reply.interaction_id
    )
    for index in range(55):
        store.send(
            kernel.subject_id,
            "participant",
            who.entity_id,
            "Later reply",
            related_interaction_id=first["interaction_id"],
            idempotency_key="outgoing-" + str(index),
        )
    page = collaboration.read("/api/participants/notifications", {}, who)
    assert len(page["items"]) == 50 and page["next_cursor"]
    rest = collaboration.read(
        "/api/participants/notifications", {"before": [page["next_cursor"]]}, who
    )
    assert len(rest["items"]) == 6 and rest["next_cursor"] is None
    assert len({row["notification_id"] for row in page["items"] + rest["items"]}) == 56
    notice = rest["items"][-1]
    payload = {k: notice[k] for k in ("notification_id", "kind", "related_id")}
    assert collaboration.write(
        "/api/participants/notification-read", payload, who, "127.0.0.1"
    ) == {"ok": True}
    reread = collaboration.read(
        "/api/participants/notifications", {"before": [page["next_cursor"]]}, who
    )
    assert reread["items"][-1]["read"] is True
    # A previous occurrence of the same state must not mark a later occurrence read.
    with kernel.database.transaction() as c:
        c.execute(
            "UPDATE participant_notification_reads SET read_at='2000-01-01T00:00:00+00:00' "
            "WHERE notification_id=?",
            (notice["notification_id"],),
        )
    assert (
        collaboration.read(
            "/api/participants/notifications", {"before": [page["next_cursor"]]}, who
        )["items"][-1]["read"]
        is False
    )
    collaboration.write("/api/participants/notification-read", payload, who, "127.0.0.1")
    assert (
        collaboration.read(
            "/api/participants/notifications", {"before": [page["next_cursor"]]}, who
        )["items"][-1]["read"]
        is True
    )
    accounts = AccountStore(kernel.database, kernel.subject_id)
    accounts.register("bob", "correct horse battery", "Bob")
    bob = accounts.authenticate(accounts.login("bob", "correct horse battery")[0])
    assert bob
    assert collaboration.read("/api/participants/notifications", {}, bob)["items"] == []
    with pytest.raises(LookupError):
        collaboration.write("/api/participants/notification-read", payload, bob, "127.0.0.1")


def test_old_submission_new_decision_is_not_hidden_by_new_pending_work(
    kernel: SubjectKernel,
) -> None:
    from noyra.wallet import BountyInput

    collaboration, who, original = make_collaboration(kernel)
    economy = collaboration.owner.wallet_economy
    bounty = economy.create_bounty(
        kernel.subject_id,
        BountyInput(
            title="Many entries",
            description="Synthetic",
            acceptance_criteria=["evidence"],
            network_id=original.network_id,
            asset_id=original.asset_id,
            reward_amount="10",
            opens_at=original.opens_at,
            expires_at=original.expires_at,
            max_submissions=100,
            reward_slots=1,
            goal_id=original.goal_id,
            idempotency_key="large-bounty",
        ),
        actor="operator",
    )
    economy.publish_bounty(bounty.bounty_id, kernel.subject_id, actor="operator")
    entries = [
        collaboration.write(
            "/api/participants/submit",
            {
                "bounty_id": bounty.bounty_id,
                "content": "Evidence",
                "recipient_address": "0x" + "1" * 40,
                "idempotency_key": "submission-" + str(index),
            },
            who,
            "127.0.0.1",
        )
        for index in range(55)
    ]
    economy.decide_submission(
        entries[0]["submission_id"],
        kernel.subject_id,
        accepted=True,
        reason="Synthetic verification",
        actor="operator",
    )
    notices = collaboration.read("/api/participants/notifications", {}, who)["items"]
    assert len(notices) == 1 and notices[0]["related_id"] == entries[0]["submission_id"]
    assert notices[0]["status"].startswith("accepted:")


def test_large_opportunities_are_complete_and_bounded_over_real_http(kernel: SubjectKernel) -> None:
    from pydantic import SecretStr

    from noyra.service import NoyraHTTPServer, ServiceSettings
    from noyra.wallet import BountyInput
    from test_participant_http import Client

    collaboration, _, original = make_collaboration(kernel)
    economy = collaboration.owner.wallet_economy
    expected = {original.bounty_id}
    for index in range(52):
        bounty = economy.create_bounty(
            kernel.subject_id,
            BountyInput(
                title="Synthetic " + str(index),
                description="详" * 20000,
                acceptance_criteria=["Complete criterion"],
                network_id=original.network_id,
                asset_id=original.asset_id,
                reward_amount="10",
                opens_at=original.opens_at,
                expires_at=original.expires_at,
                max_submissions=3,
                reward_slots=1,
                goal_id=original.goal_id,
                idempotency_key="page-" + str(index),
            ),
            actor="operator",
        )
        economy.publish_bounty(bounty.bounty_id, kernel.subject_id, actor="operator")
        expected.add(bounty.bounty_id)
    server = NoyraHTTPServer(
        kernel,
        ServiceSettings(
            data_dir=kernel.database.path.parent,
            subject_id=kernel.subject_id,
            genesis_hash=content_hash("audit"),
            host="127.0.0.1",
            port=0,
            admin_token=SecretStr("synthetic-admin-token-with-entropy"),
        ),
    )
    server.start()
    try:
        client = Client("http://127.0.0.1:" + str(server.address[1]))
        cursor = None
        seen = []
        for _ in range(5):
            status, page = client.request(
                "/api/participants/opportunities" + ("?before=" + cursor if cursor else "")
            )
            assert status == 200
            assert len(json.dumps(page, ensure_ascii=False).encode()) < 2000000
            for item in page["items"]:
                seen.append(item["bounty_id"])
                assert "goal_id" not in item and "created_at" not in item
                if item["bounty_id"] != original.bounty_id:
                    assert item["description"] == "详" * 20000
                    assert item["acceptance_criteria"] == ["Complete criterion"]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert cursor is None and len(seen) == len(expected) and set(seen) == expected
        assert client.request("/api/participants/opportunities?unknown=1")[0] == 400
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        "authorize_attempt",
        "start_attempt",
        "check_request_authorization",
        "provider_complete",
        "provider_failed",
        "provider_unknown",
    ],
)
async def test_routed_withdrawal_settles_route_without_fallback_or_state_commit(
    kernel: SubjectKernel, tmp_path: Path, phase: str
) -> None:
    from noyra.model.resources import RoutedModelGateway
    from test_audit_20261022_remediation import resources_for

    collaboration, who, bounty = make_collaboration(kernel)
    result = collaboration.write(
        "/api/participants/apply",
        {"bounty_id": bounty.bounty_id, "content": "I can help", "idempotency_key": "interest"},
        who,
        "127.0.0.1",
    )
    engine, provider = cognition(kernel)
    resources, _ = resources_for(kernel.database, kernel.subject_id, tmp_path)
    routed = RoutedModelGateway(
        kernel.database, kernel.subject_id, resources, provider_factory=lambda _: provider
    )
    engine.gateway = routed

    def withdraw() -> None:
        collaboration.write(
            "/api/participants/withdraw",
            {"kind": "application", "id": result["application_id"]},
            who,
            "127.0.0.1",
        )

    target: Any
    if phase.startswith("provider_"):
        complete = provider.complete

        async def after_send(request: CompletionRequest) -> ProviderResponse:
            withdraw()
            from noyra.core.private_sources import InvitationWithdrawnError

            with pytest.raises(InvitationWithdrawnError), kernel.database.transaction():
                pass
            if phase != "provider_complete":
                from noyra.model.errors import ProviderCallError

                provider.requests.append(request)
                raise ProviderCallError(
                    "synthetic_provider_failure",
                    retryable=False,
                    outcome_unknown=phase == "provider_unknown",
                )
            return await complete(request)

        target, name, hook = provider, "complete", after_send
    else:
        check = getattr(routed.ledger, phase)

        def before_send(*args: Any, **kwargs: Any) -> Any:
            withdraw()
            return check(*args, **kwargs)

        target, name, hook = routed.ledger, phase, before_send
    with patch.object(target, name, side_effect=hook):
        await engine.run_due()
    with kernel.database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM interaction_decisions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM cognitive_route_outcomes").fetchone()[0] == 1
        assert c.execute("SELECT status FROM model_calls").fetchone()[0] == (
            "succeeded"
            if phase == "provider_complete"
            else "unknown"
            if phase == "provider_unknown"
            else "failed"
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM model_attempts WHERE status IN ('authorized','executing')"
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM waiting_cognitive_tasks WHERE status='waiting'"
            ).fetchone()[0]
            == 0
        )
    assert len(provider.requests) == (1 if phase.startswith("provider_") else 0)


def test_submission_receipt_requires_existing_authenticated_ownership(
    kernel: SubjectKernel,
) -> None:
    from noyra.wallet.economy_types import SubmissionInput

    collaboration, who, bounty = make_collaboration(kernel)
    payload = {
        "bounty_id": bounty.bounty_id,
        "content": "Legacy contribution",
        "recipient_address": "0x" + "1" * 40,
        "idempotency_key": "receipt",
    }
    collaboration.owner.wallet_economy.submit_to_bounty(
        bounty.bounty_id,
        kernel.subject_id,
        SubmissionInput(
            counterparty=who.entity_id,
            consent_version=1,
            content=payload["content"],
            recipient_address=payload["recipient_address"],
            idempotency_key="participant:" + content_hash([who.entity_id, "submit", "receipt"]),
        ),
    )
    with pytest.raises(PermissionError, match="submission_owner_mismatch"):
        collaboration.write("/api/participants/submit", payload, who, "127.0.0.1")
    assert collaboration.read("/api/participants/submissions", {}, who)["items"] == []
    with kernel.database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM participant_submissions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM wallet_payment_orders").fetchone()[0] == 0
