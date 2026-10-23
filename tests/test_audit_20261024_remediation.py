from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from noyra.cognition import CognitionSettings, InteractionCognition
from noyra.cognition.settings import WorldSourceConfig
from noyra.core import SubjectKernel
from noyra.core.types import content_hash
from noyra.interaction import InteractionStore
from noyra.interaction.accounts import AccountStore
from noyra.mind.memory import MAX_RECALL_QUERY_CHARS, MemoryStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelPricing,
    ModelUsage,
    ProviderResponse,
)


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
