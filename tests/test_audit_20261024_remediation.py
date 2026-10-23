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
