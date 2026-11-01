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
        self.disposition = "reflect"

    async def complete(self, request: CompletionRequest) -> ProviderResponse:
        self.requests.append(request)
        context = json.loads(request.messages[1].content)["context"]
        proposal = {
            "disposition": self.disposition,
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

    def unknown_call(self, agenda_id: str) -> None:
        ledger = ModelLedger(self.database)
        call, _ = ledger.prepare_call(
            self.subject,
            "fixture",
            "fixture",
            f"intrinsic_thought:{agenda_id}:0",
            content_hash("request"),
            f"intrinsic-thought:{agenda_id}:0",
        )
        attempt = ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(100, 500_000, 100_000, 5_000_000),
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=0,
        )
        ledger.start_attempt(attempt.attempt_id)
        ledger.finish_attempt(
            attempt.attempt_id,
            "unknown",
            usage=None,
            cost_microusd=None,
            error_code="synthetic_unknown",
        )
        ledger.finish_call(call.call_id, "unknown", error_code="synthetic_unknown")

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
    harness.unknown_call("agenda-unknown")
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


def test_later_discovery_is_not_quarantined_by_an_older_equivalent_unknown(
    harness: Harness,
) -> None:
    harness.agenda("agenda-old-unknown", "Recurring question")
    harness.unknown_call("agenda-old-unknown")
    harness.now += timedelta(hours=1)
    harness.agenda("agenda-new-evidence", "Recurring question")
    assert [row["agenda_id"] for row in harness.batch()] == ["agenda-new-evidence"]
    assert harness.provider.requests == []


def test_clock_rollback_does_not_admit_future_unserved_questions(harness: Harness) -> None:
    harness.agenda("agenda-future", "A question from the future")
    assert len(harness.batch()) == 1
    harness.now -= timedelta(days=1)
    assert harness.batch() == []
    assert harness.provider.requests == []


def test_unrunnable_prepared_rounds_cannot_fill_the_entire_candidate_batch(
    harness: Harness,
) -> None:
    ledger = ModelLedger(harness.database)
    for index in range(ATTENTION_BATCH_SIZE):
        agenda_id = f"agenda-prepared-{index}"
        harness.agenda(agenda_id, f"Prepared question {index}")
        ledger.prepare_call(
            harness.subject,
            "fixture",
            "fixture",
            f"intrinsic_thought:{agenda_id}:0",
            content_hash(["prepared", index]),
            f"intrinsic-thought:{agenda_id}:0",
        )
    harness.agenda("agenda-eligible", "Available question")
    assert [row["agenda_id"] for row in harness.batch()] == ["agenda-eligible"]
    assert harness.provider.requests == []


@pytest.mark.asyncio
async def test_resolved_equivalent_question_is_not_reopened_by_its_duplicate(
    harness: Harness,
) -> None:
    harness.agenda("agenda-completed", "One shared issue")
    harness.agenda("agenda-sibling", "One shared issue")
    harness.provider.disposition = "resolve"
    assert (
        await harness.thought.run_due(agenda_id="agenda-completed") == "intrinsic_thought_resolve"
    )
    assert not any(row["topic"] == "One shared issue" for row in harness.batch())
    with harness.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM thought_agenda_items WHERE topic=?", ("One shared issue",)
            ).fetchone()[0]
            == 2
        )
