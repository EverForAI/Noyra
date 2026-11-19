"""Distinguish recovery of settled evidence from permission for fresh thought work."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

import test_intrinsic_thought as base
import test_intrinsic_thought_recurrence as r
import test_intrinsic_thought_rounds as rounds
from noyra.cognition import IntrinsicThought
from noyra.model import FakeProvider, ModelLedger, ModelUsage, ProviderResponse
from noyra.model.errors import ProviderCallError


@pytest.fixture
def owner() -> Iterator[base.IntrinsicThoughtTestCase]:
    case = base.IntrinsicThoughtTestCase()
    case.setUp()
    try:
        with patch("noyra.model.ledger.utc_now", side_effect=lambda: case.clock_value):
            yield case
    finally:
        case.tearDown()


def restart(thought: IntrinsicThought, owner: base.IntrinsicThoughtTestCase) -> IntrinsicThought:
    return IntrinsicThought(
        thought.database,
        thought.subject_id,
        thought.gateway,
        thought.settings,
        clock=lambda: owner.clock_value,
    )


def snapshot(thought: IntrinsicThought) -> dict[str, list[tuple[Any, ...]]]:
    with thought.database.read_transaction() as connection:
        return {
            table: [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in (
                "model_calls",
                "model_attempts",
                "thought_agenda_items",
                "thought_agenda_revisions",
                "thought_episodes",
                "thought_round_closures",
                "events",
                "waiting_cognitive_tasks",
            )
        }


async def assert_waiting_is_read_only(
    thought: IntrinsicThought,
    owner: base.IntrinsicThoughtTestCase,
    target: str,
    explicit_result: str,
) -> None:
    before = snapshot(thought)
    for _ in range(3):
        thought = restart(thought, owner)
        assert await thought.run_due(agenda_id=target) == explicit_result
        assert await thought.run_due() is None
        assert snapshot(thought) == before


@pytest.mark.asyncio
async def test_committed_success_does_not_bypass_the_interval_on_repeated_ticks(
    owner: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(owner, [r.proposal(), r.proposal(insight="A later framing.")])
    assert await thought.run_due() == "intrinsic_thought_reflect"
    target = r.absence(thought).agenda_id
    assert thought.inspect_agenda(target).status == "not_due"
    await assert_waiting_is_read_only(thought, owner, target, "intrinsic_thought_target_not_due")
    assert isinstance(thought.gateway.provider, FakeProvider)
    assert len(thought.gateway.provider.requests) == 1
    assert thought.verify_integrity()["thought_episodes"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "succeeded", "prepared", "executing", "unknown"])
async def test_hard_cooldown_precedes_every_existing_call_state(
    owner: base.IntrinsicThoughtTestCase,
    status: str,
) -> None:
    thought = r.thought_for(owner, [r.proposal()], max_thought_no_change_streak=1)
    provider = thought.gateway.provider
    assert isinstance(provider, FakeProvider)
    response = provider._outcomes[0]
    assert isinstance(response, ProviderResponse)
    assert await thought.run_due() == "intrinsic_thought_reflect"
    agenda = r.absence(thought)
    assert agenda.status == "cooling"
    number, _ = thought.rounds.current(agenda.agenda_id)
    call_id = rounds.seed_call(
        thought, "executing" if status == "succeeded" else status, number=number
    )
    if status == "succeeded":
        ledger = ModelLedger(thought.database)
        attempt = ledger.attempts(call_id)[0]
        ledger.finish_attempt(
            attempt.attempt_id, "succeeded", usage=ModelUsage(2, 3), cost_microusd=7
        )
        ledger.finish_call(call_id, "succeeded", response={"content": response.content})
    assert thought.inspect_agenda(agenda.agenda_id, allow_new_calls=True).status == "cooling"
    await assert_waiting_is_read_only(
        thought, owner, agenda.agenda_id, "intrinsic_thought_target_waiting"
    )
    assert len(provider.requests) == 1
    assert thought._episode_count(agenda.agenda_id) == 1


@pytest.mark.asyncio
async def test_preexisting_settled_success_during_interval_recovers_once_without_dispatch(
    owner: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(
        owner,
        [r.proposal(), r.proposal(insight="A distinct already-settled result.")],
        max_thought_model_calls_per_day=1,
    )
    provider = thought.gateway.provider
    assert isinstance(provider, FakeProvider)
    assert await thought.run_due() == "intrinsic_thought_reflect"
    agenda = r.absence(thought)
    assert thought.inspect_agenda(agenda.agenda_id).status == "budget_exhausted"
    # Represent durable evidence left by an older worker/operator reconciliation.
    # Seeding it does not assert that the current scheduler can dispatch here.
    response = provider._outcomes[0]
    assert isinstance(response, ProviderResponse)
    number, _ = thought.rounds.current(agenda.agenda_id)
    call_id = rounds.seed_call(thought, "executing", number=number)
    ledger = ModelLedger(thought.database)
    ledger.finish_attempt(
        ledger.attempts(call_id)[0].attempt_id, "succeeded", usage=ModelUsage(2, 3), cost_microusd=7
    )
    ledger.finish_call(call_id, "succeeded", response={"content": response.content})
    facts = rounds.snapshot_calls(thought)
    assert thought.inspect_agenda(agenda.agenda_id, allow_new_calls=False).eligible
    recovered = restart(thought, owner)
    assert await recovered.run_due(agenda_id=agenda.agenda_id) == "intrinsic_thought_reflect"
    assert recovered._episode_count(agenda.agenda_id) == 2
    assert rounds.snapshot_calls(recovered) == facts
    assert len(provider.requests) == 1
    await assert_waiting_is_read_only(
        recovered, owner, agenda.agenda_id, "intrinsic_thought_target_waiting"
    )
    assert recovered.verify_integrity()["thought_episodes"] == 2


@pytest.mark.asyncio
async def test_known_failure_after_crash_is_closed_once_without_an_episode_or_new_call(
    owner: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(owner, [r.proposal()], max_thought_model_calls_per_day=1)
    provider = FakeProvider(
        [
            ProviderCallError("synthetic_known_failure", retryable=False, outcome_unknown=False),
        ]
    )
    thought.gateway.provider = provider
    with (
        patch.object(thought, "_close_round", side_effect=RuntimeError("synthetic closure crash")),
        pytest.raises(RuntimeError, match="synthetic closure crash"),
    ):
        await thought.run_due()
    target = r.absence(thought).agenda_id
    facts = rounds.snapshot_calls(thought)
    assert thought.inspect_agenda(target, allow_new_calls=False).eligible
    recovered = restart(thought, owner)
    assert await recovered.run_due(agenda_id=target) == "intrinsic_thought_model_failed"
    assert len(rounds.closures(recovered)) == 1
    assert recovered._episode_count(target) == 0
    assert rounds.snapshot_calls(recovered) == facts
    assert len(provider.requests) == 1
    await assert_waiting_is_read_only(recovered, owner, target, "intrinsic_thought_target_waiting")
    assert recovered.verify_integrity()["thought_round_closures"] == 1


@pytest.mark.asyncio
async def test_authorized_prepared_retry_still_obeys_the_daily_budget(
    owner: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(owner, [r.proposal()], max_thought_model_calls_per_day=1)
    original = thought.gateway.provider
    assert isinstance(original, FakeProvider)
    response = original._outcomes[0]
    provider = FakeProvider(
        [
            ProviderCallError("synthetic_unknown", retryable=False, outcome_unknown=True),
            response,
        ]
    )
    thought.gateway.provider = provider
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    target = r.absence(thought).agenda_id
    with thought.database.read_transaction() as connection:
        call_id = connection.execute("SELECT call_id FROM model_calls").fetchone()[0]
    ModelLedger(thought.database).prepare_unknown_retry(
        call_id,
        actor="operator",
        reason="synthetic explicit retry authorization",
    )
    assert thought.inspect_agenda(target).status == "budget_exhausted"
    await assert_waiting_is_read_only(thought, owner, target, "intrinsic_thought_target_waiting")
    assert len(provider.requests) == 1
    assert ModelLedger(thought.database).attempts(call_id)[0].status == "unknown"


@pytest.mark.asyncio
async def test_prepared_next_round_cannot_bypass_a_failed_round_retry_after(
    owner: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(owner, [r.proposal()])
    rounds.seed_call(thought, "failed")
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    target = r.absence(thought).agenda_id
    number, _ = thought.rounds.current(target)
    call_id = rounds.seed_call(thought, "unknown", number=number)
    ModelLedger(thought.database).prepare_unknown_retry(
        call_id,
        actor="operator",
        reason="synthetic explicit retry authorization",
    )
    assert thought.inspect_agenda(target, allow_new_calls=True).status == "not_due"
    await assert_waiting_is_read_only(thought, owner, target, "intrinsic_thought_target_not_due")
    assert isinstance(thought.gateway.provider, FakeProvider)
    assert thought.gateway.provider.requests == []
