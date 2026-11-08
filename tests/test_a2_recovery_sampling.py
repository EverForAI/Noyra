from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from math import ceil
from typing import Any
from unittest.mock import patch

import pytest

import test_autonomous_projects as helpers
from noyra.cognition.recovery import AutonomyRecovery
from noyra.core.errors import IntegrityError
from noyra.core.recovery_budget import read_recovery, workflow_key, write_recovery
from noyra.core.types import content_hash, utc_now
from noyra.model import BudgetLimits, ModelLedger
from noyra.world import SourceRegistry
from noyra.world.poll import SourcePollStore
from test_a0_recovery_budget import case as case
from test_a0_recovery_budget import reserve
from test_m2_recovery_exit import prepared


def state(case: helpers.AutonomousProjectsTestCase) -> dict[str, Any]:
    with case.kernel.database.read_transaction() as connection:
        record = read_recovery(connection, case.subject_id)
    assert record is not None
    return record


def checkpoint_count(case: helpers.AutonomousProjectsTestCase) -> int:
    with case.kernel.database.connection() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM workflow_checkpoints WHERE workflow_id=?",
                (workflow_key(case.subject_id),),
            ).fetchone()[0]
        )


@pytest.mark.asyncio
async def test_301_second_samples_exit_without_persisting_every_sample(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    origin = datetime.fromisoformat(case.clock_value)
    first = recovery.refresh()
    assert first is not None and first["active"]
    baseline = checkpoint_count(case)
    for index in range(1, 13):
        case.clock_value = (origin + timedelta(seconds=301 * index)).isoformat()
        recovery.refresh()
    result = state(case)
    assert not result["active"]
    assert result["stable_since"] == first["stable_since"]
    assert all(condition["satisfied"] for condition in result["exit_conditions"])
    assert checkpoint_count(case) - baseline <= 6


def at(case: helpers.AutonomousProjectsTestCase, origin: datetime, seconds: float) -> None:
    case.clock_value = (origin + timedelta(seconds=seconds)).isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [300, 330, 599, 600])
async def test_successive_real_samples_within_gap_bound_eventually_exit(
    case: helpers.AutonomousProjectsTestCase, interval: int
) -> None:
    recovery = await prepared(case)
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    baseline = checkpoint_count(case)
    steps = ceil(3600 / interval)
    for index in range(1, steps + 1):
        at(case, origin, interval * index)
        recovery.refresh()
        assert state(case)["active"] is (interval * index < 3600)
    result = state(case)
    assert result["stable_since"] == origin.isoformat(timespec="milliseconds")
    assert result["reason"] == "normal_operation_restored"
    assert checkpoint_count(case) - baseline <= ceil(steps * interval / 600) + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [601, 900])
async def test_actual_long_sample_gaps_cannot_claim_continuous_health(
    case: helpers.AutonomousProjectsTestCase, interval: int
) -> None:
    recovery = await prepared(case)
    reserve(case, 1)
    reservations = state(case)["reservations"]
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    for index in range(1, 9):
        at(case, origin, interval * index)
        recovery.refresh()
        current = state(case)
        assert current["active"] and current["stable_since"] == case.clock_value
        assert current["reservations"] == reservations
        assert not next(
            c["satisfied"] for c in current["exit_conditions"] if c["code"] == "continuous_health"
        )


@pytest.mark.asyncio
async def test_throttled_ticks_are_not_health_samples_or_checkpoint_writes(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    baseline = checkpoint_count(case)
    for seconds in range(30, 3601, 30):
        at(case, origin, seconds)
        result = recovery.refresh()
        if seconds % 300:
            assert result is None
    assert not state(case)["active"]
    assert checkpoint_count(case) - baseline <= 6


@pytest.mark.asyncio
async def test_restart_within_gap_bound_requires_a_new_health_window_and_keeps_costs(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    reserve(case, 1)
    reservations = state(case)["reservations"]
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    for seconds in (301, 602, 903, 1204, 1505, 1806):
        at(case, origin, seconds)
        recovery.refresh()
    at(case, origin, 1860)
    restarted = AutonomyRecovery(
        case.kernel.database, case.subject_id, clock=lambda: case.clock_value
    )
    assert restarted.refresh() is not None
    assert state(case)["stable_since"] == case.clock_value
    for seconds in range(2161, 5473, 301):
        at(case, origin, seconds)
        restarted.refresh()
    result = state(case)
    assert not result["active"]
    assert result["stable_since"] == (origin + timedelta(seconds=1860)).isoformat()
    assert result["reservations"] == reservations


@pytest.mark.asyncio
async def test_budget_checkpoints_are_not_samples_and_do_not_discard_live_continuity(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    at(case, origin, 301)
    recovery.refresh()
    durable_health_time = state(case)["last_health_evaluated_at"]
    at(case, origin, 450)
    reserve(case, 1)
    reservation = state(case)["reservations"]
    assert state(case)["last_health_evaluated_at"] == durable_health_time
    assert state(case)["checked_at"] == case.clock_value
    for index in range(2, 13):
        at(case, origin, 301 * index)
        recovery.refresh()
    result = state(case)
    assert not result["active"]
    assert result["stable_since"] == origin.isoformat(timespec="milliseconds")
    assert result["reservations"] == reservation


@pytest.mark.asyncio
async def test_budget_activity_during_a_real_health_gap_cannot_fake_continuity(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    for seconds in (300, 600):
        at(case, origin, seconds)
        reserve(case, seconds)
    at(case, origin, 601)
    recovery.refresh()
    assert state(case)["active"]
    assert state(case)["stable_since"] == case.clock_value
    assert len(state(case)["reservations"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["read", "write", "commit", "clock"])
async def test_failed_sample_discards_continuity_without_committing_partial_health(
    case: helpers.AutonomousProjectsTestCase, kind: str
) -> None:
    recovery = await prepared(case)
    reserve(case, 1)
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    at(case, origin, 301)
    recovery.refresh()
    before = state(case)
    count = checkpoint_count(case)
    at(case, origin, 602)
    transaction = case.kernel.database.transaction

    @contextmanager
    def failing_commit() -> Iterator[sqlite3.Connection]:
        with transaction() as connection:
            yield connection
            raise sqlite3.OperationalError("synthetic commit failure")

    target: AbstractContextManager[Any]
    if kind == "commit":
        target = patch.object(case.kernel.database, "transaction", failing_commit)
    elif kind == "clock":
        target = patch.object(
            recovery, "clock", side_effect=RuntimeError("synthetic clock failure")
        )
    else:
        target = patch(
            "noyra.cognition.recovery." + ("read_recovery" if kind == "read" else "write_recovery"),
            side_effect=RuntimeError("synthetic health sample failure"),
        )
    with target, pytest.raises((RuntimeError, sqlite3.OperationalError)):
        recovery.refresh()
    assert state(case) == before
    assert checkpoint_count(case) == count
    at(case, origin, 603)
    recovery.refresh()
    result = state(case)
    assert result["active"] and result["stable_since"] == case.clock_value
    assert result["reservations"] == before["reservations"]


@pytest.mark.asyncio
async def test_malformed_clock_cannot_leave_a_pre_failure_continuity_cache(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    at(case, origin, 301)
    recovery.refresh()
    case.clock_value = "invalid"
    with pytest.raises(IntegrityError):
        recovery.refresh()
    at(case, origin, 602)
    recovery.refresh()
    assert state(case)["active"] and state(case)["stable_since"] == case.clock_value


@pytest.mark.asyncio
async def test_rollback_behind_unpersisted_sample_is_not_hidden_by_write_throttling(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    reserve(case, 1)
    reservations = state(case)["reservations"]
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    at(case, origin, 301)
    recovery.refresh()
    assert state(case)["last_health_evaluated_at"] != case.clock_value
    at(case, origin, 300)
    recovery.refresh()
    result = state(case)
    assert result["active"] and result["stable_since"] is None
    assert result["clock_status"] == "wall_clock_behind_history"
    assert datetime.fromisoformat(result["clock_high_water_at"]) >= origin + timedelta(seconds=301)
    assert result["reservations"] == reservations
    at(case, origin, 600)
    recovery.refresh()
    assert state(case)["clock_status"] == "normal"
    assert state(case)["stable_since"] == case.clock_value


@pytest.mark.asyncio
async def test_forward_jump_resets_sampled_health_without_lowering_the_budget_watermark(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    at(case, origin, 301)
    recovery.refresh()
    at(case, origin, 86400)
    recovery.refresh()
    result = state(case)
    assert result["active"] and result["stable_since"] == case.clock_value
    assert datetime.fromisoformat(result["clock_high_water_at"]) == origin + timedelta(days=1)


@pytest.mark.asyncio
async def test_real_unhealthy_source_resets_window_then_recovers_without_refunding(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    reserve(case, 1)
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    at(case, origin, 301)
    recovery.refresh()
    source = SourceRegistry(case.kernel.database).register(
        case.subject_id, "New source", "https://example.com/health", "rss", status="active"
    )
    at(case, origin, 602)
    recovery.refresh()
    unhealthy = state(case)
    assert unhealthy["active"] and unhealthy["stable_since"] is None
    assert not next(
        c["satisfied"] for c in unhealthy["exit_conditions"] if c["code"] == "source_polling"
    )
    at(case, origin, 903)
    with case.kernel.database.transaction() as connection:
        SourcePollStore.complete(
            connection, case.subject_id, source.source_id, case.clock_value, outcome="unchanged"
        )
    recovery.refresh()
    result = state(case)
    assert result["active"] and result["stable_since"] == case.clock_value
    assert result["reservations"] == unhealthy["reservations"]


@pytest.mark.asyncio
async def test_changed_incident_cannot_inherit_unpersisted_old_health(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    at(case, origin, 301)
    recovery.refresh()
    at(case, origin, 450)
    with case.kernel.database.transaction() as connection:
        current = read_recovery(connection, case.subject_id)
        assert current is not None
        current["incident"] = content_hash("new incident")
        write_recovery(connection, case.subject_id, current, case.clock_value)
    at(case, origin, 602)
    recovery.refresh()
    result = state(case)
    assert result["active"] and result["stable_since"] == case.clock_value
    assert result["incident"] == content_hash("new incident")


@pytest.mark.asyncio
async def test_other_instance_health_write_invalidates_old_process_sample(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    at(case, origin, 301)
    restarted = AutonomyRecovery(
        case.kernel.database, case.subject_id, clock=lambda: case.clock_value
    )
    restarted.refresh()
    assert state(case)["stable_since"] == case.clock_value
    at(case, origin, 602)
    recovery.refresh()
    assert state(case)["stable_since"] == case.clock_value
    assert state(case)["active"]


@pytest.mark.asyncio
async def test_unknown_model_call_and_its_reserved_cost_survive_health_exit(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    case.clock_value = utc_now()
    ledger = ModelLedger(case.kernel.database)
    with patch("noyra.model.ledger.utc_now", lambda: case.clock_value):
        call, _ = ledger.prepare_call(
            case.subject_id,
            "fixture",
            "fixture",
            "world_cognition:a2",
            content_hash("a2"),
            "a2-unknown",
        )
        attempt = ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(100, 1000000, 1000000, 1000000),
            reserved_input_tokens=100,
            reserved_output_tokens=50,
            reserved_cost_microusd=300,
        )
        ledger.start_attempt(attempt.attempt_id)
        ledger.finish_attempt(
            attempt.attempt_id,
            "unknown",
            usage=None,
            cost_microusd=None,
            error_code="provider_outcome_unknown",
        )
        ledger.finish_call(call.call_id, "unknown", error_code="provider_outcome_unknown")
    before = (
        asdict(ledger.get_call(call.call_id)),
        [asdict(a) for a in ledger.attempts(call.call_id)],
    )
    reservations = state(case)["reservations"]
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    for index in range(1, 13):
        at(case, origin, 301 * index)
        recovery.refresh()
    assert not state(case)["active"]
    assert state(case)["reservations"] == reservations
    assert (
        asdict(ledger.get_call(call.call_id)),
        [asdict(a) for a in ledger.attempts(call.call_id)],
    ) == before


@pytest.mark.asyncio
async def test_failed_exit_commit_cannot_disable_recovery_or_reuse_the_old_window(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    reserve(case, 1)
    origin = datetime.fromisoformat(case.clock_value)
    recovery.refresh()
    for index in range(1, 12):
        at(case, origin, 301 * index)
        recovery.refresh()
    before = state(case)
    assert before["active"]
    transaction = case.kernel.database.transaction

    @contextmanager
    def fail_exit_commit() -> Iterator[sqlite3.Connection]:
        with transaction() as connection:
            yield connection
            raise sqlite3.OperationalError("synthetic exit commit failure")

    at(case, origin, 3612)
    with (
        patch.object(case.kernel.database, "transaction", fail_exit_commit),
        pytest.raises(sqlite3.OperationalError, match="synthetic exit commit failure"),
    ):
        recovery.refresh()
    assert state(case) == before
    at(case, origin, 3613)
    recovery.refresh()
    result = state(case)
    assert result["active"] and result["stable_since"] == case.clock_value
    assert result["reservations"] == before["reservations"]
