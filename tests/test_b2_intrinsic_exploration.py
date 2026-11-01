from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from noyra.cognition import CognitionSettings, IntrinsicThought, MetacognitiveControl
from noyra.cognition.attention import ATTENTION_BATCH_SIZE, attention_batch
from noyra.core import Database, EventStore, SubjectKernel
from noyra.core.actions import ActionLedger
from noyra.core.types import content_hash
from noyra.model import (
    BudgetLimits,
    ModelGateway,
    ModelLedger,
    ModelPricing,
    ModelUsage,
    ProviderResponse,
    RetryPolicy,
)
from noyra.model.types import CompletionRequest


class Reflector:
    name = "b2-fixture"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> ProviderResponse:
        self.requests.append(request)
        context = json.loads(request.messages[1].content)["context"]
        proposal = {
            "disposition": "reflect",
            "summary": "Considered this internal question.",
            "insight": f"A distinct perspective on {context['agenda']['topic']}.",
            "next_question": None,
            "source_event_ids": [context["events"][0]["event_id"]],
            "source_memory_ids": [],
            "source_belief_ids": [],
            "source_goal_ids": [],
            "source_relationship_ids": [],
            "goal_candidate": None,
        }
        return ProviderResponse(content=json.dumps(proposal), usage=ModelUsage(100, 80))


@dataclass
class Harness:
    kernel: SubjectKernel
    now: datetime
    provider: Reflector
    thought: IntrinsicThought

    @property
    def database(self) -> Database:
        return self.kernel.database

    @property
    def subject(self) -> str:
        return self.kernel.subject_id

    def agenda(self, identity: str, topic: str, *, urgency: float = 0.5) -> None:
        urgency = float(urgency)
        now = self.now.isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO thought_agenda_items(agenda_id,subject_id,source_type,"
                "source_id,topic,urgency,novelty,emotional_weight,recurrence_count,"
                "consecutive_no_change,status,cooldown_until,state_hash,created_at,updated_at) "
                "VALUES (?,?,'question',?,?,?,0.5,0.5,1,0,'open',NULL,?,?,?)",
                (
                    identity,
                    self.subject,
                    identity,
                    topic,
                    urgency,
                    self.thought._agenda_hash(
                        "question", identity, topic, urgency, 0.5, 0.5, 1, 0, "open", None
                    ),
                    now,
                    now,
                ),
            )
            self.thought._insert_agenda_revision(
                connection,
                identity,
                old_status=None,
                new_status="open",
                old_recurrence=0,
                new_recurrence=1,
                old_no_change=0,
                new_no_change=0,
                cooldown_until=None,
                reason="fixture question",
                created_at=now,
            )

    def batch(self) -> list[Any]:
        return attention_batch(
            self.database,
            self.subject,
            self.now.isoformat(),
            self.thought.settings.thought_interval_seconds,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    kernel = SubjectKernel(tmp_path / "subject.sqlite3", "b2-fixture", content_hash("b2"))
    kernel.boot()
    kernel.orient()
    kernel.activate()
    now = datetime.now(UTC)
    EventStore(kernel.database).append(
        kernel.subject_id, "experience", "self", {"theme": "inquiry"}
    )
    provider = Reflector()
    gateway = ModelGateway(
        provider,
        ModelLedger(kernel.database),
        model="fixture",
        limits=BudgetLimits(100, 500_000, 100_000, 5_000_000),
        pricing=ModelPricing(),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    settings = CognitionSettings(enabled=True, max_thought_model_calls_per_day=96)
    thought = IntrinsicThought(
        kernel.database, kernel.subject_id, gateway, settings, clock=lambda: holder.now.isoformat()
    )
    holder = Harness(kernel, now, provider, thought)
    try:
        yield holder
    finally:
        kernel.close()


@pytest.mark.asyncio
async def test_backlog_receives_persistent_turns_before_repeated_high_score(
    harness: Harness,
) -> None:
    for index in range(34):
        harness.agenda(
            f"agenda-{index:02d}", f"Question {index}", urgency=1.0 if index == 0 else 0.1
        )
    visited: set[str] = set()
    for _ in range(35):
        assert await harness.thought.run_due() == "intrinsic_thought_reflect"
        latest = harness.thought.latest()
        assert latest is not None
        assert latest.agenda_id not in visited
        visited.add(latest.agenda_id)
        harness.now += timedelta(hours=2)
    assert len(visited) == 35  # includes the legitimate no-continuable-goal question
    assert harness.thought.verify_integrity()["thought_episodes"] == 35


@pytest.mark.asyncio
async def test_equivalent_question_is_one_opportunity_without_erasing_history(
    harness: Harness,
) -> None:
    harness.agenda("agenda-first", "Same open question")
    harness.agenda("agenda-duplicate", "Same open question")
    harness.agenda("agenda-other", "Independent question")
    assert len(harness.batch()) == 2
    assert await harness.thought.run_due(agenda_id="agenda-first") == "intrinsic_thought_reflect"
    assert [
        row["agenda_id"] for row in harness.batch() if str(row["agenda_id"]).startswith("agenda-")
    ] == ["agenda-other"]
    with harness.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM thought_agenda_items").fetchone()[0] >= 3
    harness.now += timedelta(hours=1)
    assert any(row["topic"] == "Same open question" for row in harness.batch())


@pytest.mark.asyncio
async def test_selection_survives_worker_restart_and_matches_metacognition(
    harness: Harness,
) -> None:
    harness.agenda("agenda-hot", "Hot issue", urgency=1)
    harness.agenda("agenda-old", "Waiting issue", urgency=0.1)
    await harness.thought.run_due(agenda_id="agenda-hot")
    harness.now += timedelta(hours=1)
    restarted = IntrinsicThought(
        harness.database,
        harness.subject,
        harness.thought.gateway,
        harness.thought.settings,
        clock=lambda: harness.now.isoformat(),
    )
    selected = restarted._select_agenda()
    assert selected is not None and selected.agenda_id != "agenda-hot"
    control = MetacognitiveControl(
        harness.database,
        harness.subject,
        harness.thought.settings,
        clock=lambda: harness.now.isoformat(),
        agenda_inspector=restarted.inspect_agenda,
    )
    choices, _ = control._candidates()
    assert (
        next(item for item in choices if item.strategy == "think").target_id == selected.agenda_id
    )


def test_unknown_round_quarantines_equivalent_questions_without_retry(harness: Harness) -> None:
    harness.agenda("agenda-unknown", "Shared uncertainty")
    harness.agenda("agenda-sibling", "Shared uncertainty")
    ledger = ModelLedger(harness.database)
    call, _ = ledger.prepare_call(
        harness.subject,
        "fixture",
        "fixture",
        "intrinsic_thought:agenda-unknown:0",
        content_hash("request"),
        "intrinsic-thought:agenda-unknown:0",
    )
    # Use the existing interruption recovery contract rather than manufacturing a thought.
    with harness.database.transaction() as connection:
        connection.execute(
            "UPDATE model_calls SET status='unknown' WHERE call_id=?", (call.call_id,)
        )
    assert harness.batch() == []
    assert harness.provider.requests == []


def test_candidate_batch_is_bounded_and_read_only(harness: Harness) -> None:
    for index in range(ATTENTION_BATCH_SIZE + 10):
        harness.agenda(f"agenda-{index}", f"Question {index}")
    with harness.database.connection() as connection:
        before = connection.execute("SELECT COUNT(*) FROM thought_agenda_revisions").fetchone()[0]
    assert len(harness.batch()) == ATTENTION_BATCH_SIZE
    assert len(harness.batch()) == ATTENTION_BATCH_SIZE
    with harness.database.connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM thought_agenda_revisions").fetchone()[0]
            == before
        )
    assert harness.provider.requests == []
    ActionLedger(harness.database).verify_integrity(harness.subject)
