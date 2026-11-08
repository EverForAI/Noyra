from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

import pytest

from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core.admission import OperationInvalidated, bind_lease
from noyra.core.errors import IntegrityError
from noyra.core.fair_scheduling import WORKFLOWS, FairScheduler, ScheduleState, WorkflowOutcome
from noyra.core.scheduling_clock import CooldownClock
from noyra.core.types import content_hash
from noyra.model import BudgetLimits, ModelLedger
from noyra.model.errors import BudgetExhaustedError


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "scheduler.sqlite3", strict_schema_contract=True)
    IdentityStore(db).ensure("one", content_hash("fixture-one"))
    IdentityStore(db).ensure("two", content_hash("fixture-two"))
    return db


@pytest.mark.asyncio
async def test_continuous_first_workflow_cannot_starve_other_service_slots(
    database: Database,
) -> None:
    mono = [0.0]
    now = [datetime(2026, 8, 15, tzinfo=UTC)]

    def make() -> FairScheduler:
        return FairScheduler(
            database, "one", clock=lambda: now[0].isoformat(), monotonic_clock=lambda: mono[0]
        )

    scheduler = make()
    visited = []

    async def dispatch(name: str) -> WorkflowOutcome:
        visited.append(name)
        return WorkflowOutcome("fixture_committed", did_execute=True, made_progress=True)

    for index in range(len(WORKFLOWS) * 3):
        if index % 5 == 0:
            scheduler = make()  # restart cannot restore first-slot priority
        assert await scheduler.run(dispatch, active=lambda: True) == "fixture_committed"
        mono[0] += 30
        now[0] += timedelta(seconds=30)
    assert visited == list(WORKFLOWS) * 3
    with database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM autonomy_schedule_state").fetchone()[0] == 1
        assert ScheduleState.read_connection(c, "two")["turn"] == 0


@pytest.mark.asyncio
async def test_wait_replay_and_budget_results_yield_without_claiming_activity(
    database: Database,
) -> None:
    scheduler = FairScheduler(database, "one")
    visited = []

    async def dispatch(name: str) -> str | None:
        visited.append(name)
        if name == "self_modification":
            return "memory_consolidation_replayed"
        if name == "motivation":
            return "motivation_development_budget_exhausted"
        if name == "world_acquisition":
            return "observation_unchanged"
        return None

    assert await scheduler.run(dispatch, active=lambda: True) == "observation_unchanged"
    assert visited == list(WORKFLOWS)
    state = scheduler.state.read()
    assert not any(r["did_execute"] or r["made_progress"] for r in state["workflows"].values())
    assert state["workflows"]["motivation"]["next_due_at"] is not None


@pytest.mark.asyncio
async def test_world_service_and_two_projects_remain_bounded_for_72_virtual_hours(
    database: Database,
) -> None:
    mono = [0.0]
    now = [datetime(2026, 8, 15, tzinfo=UTC)]
    scheduler = FairScheduler(
        database, "one", clock=lambda: now[0].isoformat(), monotonic_clock=lambda: mono[0]
    )
    seen: dict[str, list[float]] = {n: [] for n in WORKFLOWS}
    projects = []

    async def dispatch(name: str) -> WorkflowOutcome:
        seen[name].append(mono[0])
        target = None
        if name.startswith("project_"):
            target = scheduler.next_target(name, ["project-b", "project-a"])
            projects.append((name, target))
        if name == "world_acquisition":
            return WorkflowOutcome("world_fetch_failed", target_id="permanent-failure")
        return WorkflowOutcome(
            "fixture_committed", did_execute=True, made_progress=True, target_id=target
        )

    for tick in range(72 * 12):
        if tick % 29 == 0:
            scheduler = FairScheduler(
                database, "one", clock=lambda: now[0].isoformat(), monotonic_clock=lambda: mono[0]
            )
        await scheduler.run(dispatch, active=lambda: True)
        now[0] += timedelta(minutes=5)
        mono[0] += 300
    assert all(len(times) > 40 for times in seen.values())
    for times in seen.values():
        assert max(b - a for a, b in pairwise(times)) <= len(WORKFLOWS) * 300
    for name in ("project_execution", "project_review"):
        targets = [target for n, target in projects if n == name]
        assert set(targets) == {"project-a", "project-b"}
        assert all(a != b for a, b in pairwise(targets))


@pytest.mark.asyncio
async def test_state_exception_moves_cursor_and_has_persistent_backoff(database: Database) -> None:
    scheduler = FairScheduler(database, "one")

    async def failed(name: str) -> None:
        raise ValueError("private exception must not be serialized")

    with pytest.raises(ValueError):
        await scheduler.run(failed, active=lambda: True)
    state = scheduler.state.read()
    assert state["cursor"] == 1
    assert state["workflows"][WORKFLOWS[0]]["reason"] == "workflow_exception_valueerror"
    assert "private exception" not in str(state)


@pytest.mark.asyncio
async def test_runtime_pause_and_invalid_lease_never_dispatch_remaining_work(
    tmp_path: Path,
) -> None:
    kernel = SubjectKernel(tmp_path / "lease.sqlite3", "one", content_hash("lease"))
    kernel.boot()
    kernel.orient()
    kernel.activate()
    scheduler = FairScheduler(kernel.database, "one")
    lease = kernel.admission.begin("fixture")
    visited = []

    async def dispatch(name: str) -> WorkflowOutcome:
        visited.append(name)
        kernel.admission.invalidate(closed=True)
        return WorkflowOutcome("fixture_committed", made_progress=True)

    try:
        with bind_lease(lease), pytest.raises(OperationInvalidated):
            await scheduler.run(dispatch, active=lambda: True)
        assert visited == [WORKFLOWS[0]]
        assert scheduler.state.read()["cursor"] == 1
    finally:
        kernel.admission.finish(lease)
        kernel.close()


def test_cooldown_uses_monotonic_elapsed_and_restart_utc_remaining() -> None:
    mono = [100.0]
    cooldown = CooldownClock(lambda: mono[0], capacity=2)
    assert not cooldown.ready("one", "stable", 60)
    mono[0] += 20
    assert not cooldown.ready("one", "stable", 100000)  # rollback cannot extend anchored wait
    mono[0] += 41
    assert cooldown.ready("one", "stable", 100000)
    restarted = CooldownClock(lambda: mono[0])
    assert not restarted.ready("one", "stable", 20)
    mono[0] += 21
    assert restarted.ready("one", "stable", -1)


def test_schema111_upgrade_keeps_historical_rows_and_scheduler_scope(tmp_path: Path) -> None:
    with patch("noyra.core.database.CURRENT_SCHEMA_VERSION", 111):
        old = Database(tmp_path / "old.sqlite3", strict_schema_contract=True)
        IdentityStore(old).ensure("one", content_hash("old"))
    with old.connection() as c:
        before = tuple(c.execute("SELECT * FROM subject_identity").fetchone())
    upgraded = Database(old.path, strict_schema_contract=True)
    upgraded.assert_schema_contract()
    with upgraded.connection() as c:
        assert tuple(c.execute("SELECT * FROM subject_identity").fetchone()) == before
        assert c.execute("SELECT COUNT(*) FROM autonomy_schedule_state").fetchone()[0] == 0
    state = ScheduleState(upgraded, "one")
    state.begin("2026-08-15T00:00:00+00:00")
    with upgraded.transaction() as c:
        c.execute("UPDATE autonomy_schedule_state SET state_hash=?", ("0" * 64,))
    with pytest.raises(IntegrityError):
        state.read()


def test_wall_clock_rollback_cannot_reopen_a_previous_model_budget_day(database: Database) -> None:
    ledger = ModelLedger(database)
    call, _ = ledger.prepare_call(
        "one", "fake", "fixture", "fixture", content_hash("request"), "one"
    )
    ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(100, 1000, 1000, 1000),
        reserved_input_tokens=10,
        reserved_output_tokens=10,
        reserved_cost_microusd=0,
        budget_day="2026-08-16",
    )
    call, _ = ledger.prepare_call("one", "fake", "fixture", "fixture", content_hash("two"), "two")
    with pytest.raises(BudgetExhaustedError, match="model_budget_clock_wait"):
        ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(100, 1000, 1000, 1000),
            reserved_input_tokens=10,
            reserved_output_tokens=10,
            reserved_cost_microusd=0,
            budget_day="2026-08-15",
        )
    assert ledger.attempts(call.call_id) == []


@pytest.mark.asyncio
async def test_model_attempt_without_progress_ends_turn_before_another_dispatch(
    database: Database,
) -> None:
    visited = []
    scheduler = FairScheduler(database, "one")

    async def dispatch(name: str) -> str:
        visited.append(name)
        ledger = ModelLedger(database)
        call, _ = ledger.prepare_call(
            "one", "fake", "fixture", "fixture", content_hash("fixture"), "paid"
        )
        ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(100, 1000, 1000, 1000),
            reserved_input_tokens=10,
            reserved_output_tokens=10,
            reserved_cost_microusd=0,
        )
        return "fixture_model_failed"

    assert await scheduler.run(dispatch, active=lambda: True) == "fixture_model_failed"
    assert visited == [WORKFLOWS[0]]
    row = scheduler.state.read()["workflows"][WORKFLOWS[0]]
    assert row["did_execute"] and not row["made_progress"]


@pytest.mark.asyncio
async def test_inactive_runtime_does_not_even_advance_scheduler_cursor(database: Database) -> None:
    from unittest.mock import AsyncMock

    scheduler = FairScheduler(database, "one")
    dispatch = AsyncMock()
    assert await scheduler.run(dispatch, active=lambda: False) == "cognition_inactive"
    dispatch.assert_not_awaited()
    assert scheduler.state.read()["turn"] == 0


@pytest.mark.asyncio
async def test_checkpoint_after_dispatch_does_not_preempt_inflight_work(database: Database) -> None:
    from unittest.mock import AsyncMock

    mono = [0.0]
    scheduler = FairScheduler(database, "one", monotonic_clock=lambda: mono[0])
    started = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(name: str) -> WorkflowOutcome:
        started.set()
        await release.wait()
        return WorkflowOutcome("fixture_committed", did_execute=True, made_progress=True)

    task = asyncio.create_task(scheduler.run(dispatch, active=lambda: True))
    await started.wait()
    mono[0] = 1000
    await asyncio.sleep(0)
    assert not task.done()
    duplicate = AsyncMock()
    assert await scheduler.run(duplicate, active=lambda: True) == "scheduler_in_progress"
    duplicate.assert_not_awaited()
    release.set()
    assert await task == "fixture_committed"


@pytest.mark.asyncio
async def test_export_preserves_owned_scheduler_state_without_other_subject(
    database: Database,
) -> None:
    import json
    from io import BytesIO
    from zipfile import ZipFile

    from noyra.core.runtime_export import RuntimeLogExporter

    async def idle(name: str) -> None:
        return None

    for subject in ("one", "two"):
        await FairScheduler(database, subject).run(idle, active=lambda: True)
    artifact = RuntimeLogExporter(database).export("one", actor="fixture-operator")
    with ZipFile(BytesIO(artifact.content)) as archive:
        rows = [
            json.loads(row)
            for row in archive.read("tables/autonomy_schedule_state.jsonl").splitlines()
        ]
    assert len(rows) == 1 and rows[0]["subject_id"] == "one"


@pytest.mark.asyncio
async def test_control_semantics_tampering_is_rejected_even_with_rehashed_json(
    database: Database,
) -> None:
    from noyra.core.types import canonical_json

    scheduler = FairScheduler(database, "one")

    async def idle(name: str) -> None:
        return None

    await scheduler.run(idle, active=lambda: True)
    state = scheduler.state.read()
    state["workflows"][WORKFLOWS[0]]["considerations"] = True
    with database.transaction() as c:
        c.execute(
            "UPDATE autonomy_schedule_state SET state_json=?,state_hash=? WHERE subject_id='one'",
            (canonical_json(state), content_hash(state)),
        )
    with pytest.raises(IntegrityError):
        scheduler.state.read()


@pytest.mark.asyncio
async def test_turn_budget_yields_between_callbacks_without_hiding_next_slot(
    database: Database,
) -> None:
    mono = [0.0]
    scheduler = FairScheduler(database, "one", monotonic_clock=lambda: mono[0])
    seen = []

    async def idle(name: str) -> None:
        seen.append(name)
        mono[0] += 5

    await scheduler.run(idle, active=lambda: True)
    await scheduler.run(idle, active=lambda: True)
    assert seen == list(WORKFLOWS[:2])
