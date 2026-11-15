"""Independent checks of empty pool telemetry and the legacy exhaustion signal."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from noyra.autonomy import AutonomyLoop
from noyra.core import SubjectKernel
from noyra.core.types import content_hash
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelMessage,
    ModelUsage,
    ProviderResponse,
)
from noyra.model.errors import BudgetExhaustedError
from noyra.model.resources import RoutedModelGateway
from noyra.sleep import FatigueInputs, FatigueTracker, SleepIntegrity, SleepReflectionPlan


class Output(BaseModel):
    ok: bool


@dataclass
class Case:
    kernel: SubjectKernel
    now: datetime

    def clock(self) -> str:
        return self.now.isoformat(timespec="milliseconds")

    def tracker(self) -> FatigueTracker:
        return FatigueTracker(self.kernel.database, clock=self.clock)


@pytest.fixture
def case(tmp_path: Path) -> Iterator[Case]:
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", "M02-synthetic", content_hash("M02"))
    kernel.boot()
    kernel.orient()
    kernel.activate()
    owner = Case(kernel, datetime(2026, 11, 15, 22, tzinfo=UTC))
    try:
        with patch("noyra.model.ledger.utc_now", side_effect=owner.clock):
            yield owner
    finally:
        owner.kernel.close()


def inputs(resource_pressure: float) -> FatigueInputs:
    return FatigueInputs(
        resource_pressure=resource_pressure,
        cognitive_load=0.0,
        frustration=0.0,
        goal_conflict=0.0,
        staleness=0.0,
    )


def accounting(kernel: SubjectKernel) -> dict[str, list[tuple[Any, ...]]]:
    with kernel.database.read_transaction() as connection:
        return {
            table: [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in ("model_calls", "model_attempts")
        }


@pytest.mark.asyncio
async def test_fresh_empty_pool_records_do_not_put_the_runtime_to_sleep(case: Case) -> None:
    tracker = case.tracker()
    assert tracker.pool_pressures(case.kernel.subject_id) == {}
    state = tracker.ensure(case.kernel.subject_id)
    assert state.resource_pressure == state.fatigue == 0
    assert tracker.should_sleep(case.kernel.subject_id) == (False, ())
    hook = AsyncMock(return_value="synthetic_local_work")
    loop = AutonomyLoop(case.kernel, active_hook=hook, clock=lambda: case.now)
    for _ in range(3):
        tick = await loop.tick()
        assert (tick.lifecycle, tick.action) == ("active", "synthetic_local_work")
    assert hook.await_count == 3
    assert loop.sleep.current() is None
    assert tracker.pool_pressures(case.kernel.subject_id) == {}
    assert accounting(case.kernel) == {"model_calls": [], "model_attempts": []}


@pytest.mark.asyncio
async def test_configured_unsampled_pools_can_reach_the_active_sampling_hook(case: Case) -> None:
    from test_audit_20261022_remediation import resources_for

    database, subject = case.kernel.database, case.kernel.subject_id
    resources, _ = resources_for(database, subject, database.path.parent, "economy")
    resources_for(database, subject, database.path.parent, "deep")
    provider = FakeProvider([])
    gateway = RoutedModelGateway(database, subject, resources, provider_factory=lambda _: provider)
    tracker = case.tracker()
    assert tracker.pool_pressures(subject) == {}

    async def sample() -> str:
        statuses = gateway.pool_status()
        assert all(value["status"] == "healthy" for value in statuses.values())
        tracker.set_pool_pressures(
            subject, {pool: value["pressure"] for pool, value in statuses.items()}
        )
        return "synthetic_resources_sampled"

    loop = AutonomyLoop(case.kernel, active_hook=sample, clock=lambda: case.now)
    tick = await loop.tick()
    assert (tick.lifecycle, tick.action) == ("active", "synthetic_resources_sampled")
    assert tracker.pool_pressures(subject) == {"economy": 0.0, "deep": 0.0}
    assert tracker.should_sleep(subject) == (False, ())
    assert provider.requests == []


@pytest.mark.parametrize(
    ("pressures", "aggregate", "exhausted"),
    [
        ({}, 0.0, False),
        ({}, 1.0, True),
        ({"economy": 1.0, "deep": 0.0}, 1.0, False),
        ({"economy": 0.0, "deep": 1.0}, 1.0, False),
        ({"economy": 1.0, "deep": 1.0}, 1.0, True),
        ({"economy": 1.0, "deep": 1.0}, 0.0, False),
        ({"economy": 0.0, "deep": 0.0, "embedding": 1.0}, 1.0, False),
    ],
)
def test_empty_records_are_only_a_fallback_for_an_existing_hard_signal(
    case: Case,
    pressures: dict[str, float],
    aggregate: float,
    exhausted: bool,
) -> None:
    tracker, subject = case.tracker(), case.kernel.subject_id
    tracker.set_pool_pressures(subject, pressures)
    state = tracker.assess(subject, inputs(aggregate), reason="Synthetic resource assessment")
    assert tracker.should_sleep(subject) == (
        (True, ("fatigue_threshold", "hard_resource_budget_exhausted"))
        if exhausted
        else (False, ())
    )
    assert state.fatigue == (100.0 if exhausted else 45.0 * aggregate)
    assert tracker.pool_pressures(subject) == pressures
    SleepIntegrity(case.kernel.database).verify(subject)


@pytest.mark.asyncio
async def test_real_legacy_budget_exhaustion_can_sleep_restart_and_wake_without_erasing_usage(
    case: Case,
) -> None:
    database, subject = case.kernel.database, case.kernel.subject_id
    provider = FakeProvider([ProviderResponse('{"ok":true}', ModelUsage(3, 2))])
    limits = BudgetLimits(1, 100_000, 100_000, 1_000_000)
    ledger = ModelLedger(database)
    gateway = ModelGateway(provider, ledger, model="synthetic-budget", limits=limits)
    messages = (ModelMessage(role="user", content="Return synthetic JSON."),)
    await gateway.complete_structured(
        subject, "synthetic-capacity", messages, Output, idempotency_key="first"
    )
    with pytest.raises(BudgetExhaustedError):
        await gateway.complete_structured(
            subject, "synthetic-capacity", messages, Output, idempotency_key="denied"
        )
    tracker = case.tracker()
    assert tracker.pool_pressures(subject) == {}
    assert ledger.budget_status(subject, limits).pressure == 1.0
    tracker.assess(subject, inputs(1.0), reason="Actual synthetic model budget denial")
    facts = accounting(case.kernel)
    hook = AsyncMock(return_value="synthetic_capacity_resampled")
    loop = AutonomyLoop(
        case.kernel,
        active_hook=hook,
        reflection_hook=AsyncMock(
            return_value=SleepReflectionPlan(summary="Synthetic local budget reflection")
        ),
        clock=lambda: case.now,
    )
    tick = await loop.tick()
    assert (tick.lifecycle, tick.action) == ("winding_down", "sleep_requested")
    run = loop.sleep.current()
    assert run is not None and run.trigger_type == "budget"
    assert run.wake_after == "2026-11-16T00:00:00.000+00:00"
    hook.assert_not_awaited()
    for expected in (
        "reflection_started",
        "reflection_committed",
        "deep_sleep_entered",
        "deep_sleep_wait",
    ):
        assert (await loop.tick()).action == expected
    assert accounting(case.kernel) == facts
    path = database.path
    case.kernel.close()
    reopened = SubjectKernel(path, subject, content_hash("M02"))
    case.kernel = reopened
    assert reopened.boot().state == "deep_sleep"
    resumed = AutonomyLoop(reopened, active_hook=hook, clock=lambda: case.now)
    assert (await resumed.tick()).action == "deep_sleep_wait"
    case.now = datetime(2026, 11, 16, tzinfo=UTC)
    assert (await resumed.tick()).action == "wake_started"
    assert (await resumed.tick()).action == "wake_completed"
    assert resumed.fatigue.get(subject).resource_pressure == 0.0
    assert resumed.fatigue.pool_pressures(subject) == {}
    tick = await resumed.tick()
    assert (tick.lifecycle, tick.action) == ("active", "synthetic_capacity_resampled")
    hook.assert_awaited_once()
    assert ModelLedger(reopened.database).budget_status(subject, limits).pressure == 0.0
    assert accounting(reopened) == facts
    assert len(provider.requests) == 1
    SleepIntegrity(reopened.database).verify(subject)


def test_another_subjects_healthy_samples_do_not_erase_the_legacy_hard_signal(case: Case) -> None:
    from noyra.core import IdentityStore

    other = "M02-other-synthetic"
    IdentityStore(case.kernel.database).ensure(other, content_hash(other))
    tracker = case.tracker()
    tracker.set_pool_pressures(other, {"economy": 0.0, "deep": 0.0})
    tracker.assess(case.kernel.subject_id, inputs(1.0), reason="Synthetic subject-scoped denial")
    assert tracker.pool_pressures(case.kernel.subject_id) == {}
    assert "hard_resource_budget_exhausted" in tracker.should_sleep(case.kernel.subject_id)[1]
    assert tracker.pool_pressures(other) == {"economy": 0.0, "deep": 0.0}
