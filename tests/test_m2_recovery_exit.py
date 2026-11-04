from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import test_autonomous_projects as helpers
from noyra.cognition.recovery import AutonomyRecovery
from noyra.core.recovery_budget import read_recovery, workflow_key, write_recovery
from noyra.world import SourceRegistry
from noyra.world.poll import SourcePollStore
from test_a0_recovery_budget import arm, reserve
from test_a0_recovery_budget import case as case


async def prepared(case: helpers.AutonomousProjectsTestCase) -> AutonomyRecovery:
    manager, _ = await case.form_project()
    arm(case)
    with case.kernel.database.transaction() as c:
        state = read_recovery(c, case.subject_id)
        assert state is not None
        state["project_ids"] = [manager.projects()[0].project_id]
        write_recovery(c, case.subject_id, state, case.clock_value)
    return AutonomyRecovery(case.kernel.database, case.subject_id, clock=lambda: case.clock_value)


def count(case: helpers.AutonomousProjectsTestCase) -> int:
    with case.kernel.database.connection() as c:
        return int(
            c.execute(
                "SELECT COUNT(*) FROM workflow_checkpoints WHERE workflow_id=?",
                (workflow_key(case.subject_id),),
            ).fetchone()[0]
        )


@pytest.mark.asyncio
async def test_health_heartbeat_is_bounded_and_exit_has_timed_conditions(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    state = recovery.refresh()
    assert state is not None and state["active"]
    origin = datetime.fromisoformat(case.clock_value)
    baseline = count(case)
    case.clock_value = (origin + timedelta(minutes=5)).isoformat()
    recovery.refresh()
    assert count(case) == baseline
    for minute in range(10, 66, 5):
        case.clock_value = (origin + timedelta(minutes=minute)).isoformat()
        state = recovery.refresh()
    assert state is not None and not state["active"]
    assert state["reason"] == "normal_operation_restored"
    assert all(c["satisfied"] and c["unmet_since"] is None for c in state["exit_conditions"])
    assert count(case) <= baseline + 6


@pytest.mark.asyncio
async def test_more_than_65_healthy_sources_are_not_an_artificial_exit_blocker(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    sources = SourceRegistry(case.kernel.database)
    for number in range(66):
        source = sources.register(
            case.subject_id,
            str(number),
            f"https://example.com/feed/{number}",
            "rss",
            status="active",
        )
        with case.kernel.database.transaction() as c:
            SourcePollStore.complete(
                c, case.subject_id, source.source_id, case.clock_value, outcome="unchanged"
            )
    state = recovery.refresh()
    assert state is not None
    check = {c["code"]: c["satisfied"] for c in state["exit_conditions"]}
    assert check["source_polling"]
    assert state["stable_since"] == case.clock_value


@pytest.mark.asyncio
async def test_rollback_resets_continuous_health_records_fact_and_keeps_reservations(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = await prepared(case)
    reserve(case, 1)
    state = recovery.refresh()
    assert state is not None and state["stable_since"]
    origin = datetime.fromisoformat(case.clock_value)
    case.clock_value = (origin - timedelta(seconds=1)).isoformat()
    state = recovery.refresh()
    assert state is not None and state["stable_since"] is None
    assert state["clock_status"] == "wall_clock_behind_history"
    assert state["observed_at"] == case.clock_value
    assert state["reservations"][0]["attempt_id"] == "attempt-1"
    baseline = count(case)
    case.clock_value = (origin - timedelta(hours=1)).isoformat()
    recovery.refresh()
    assert count(case) == baseline
    case.clock_value = (origin + timedelta(minutes=5)).isoformat()
    restarted = AutonomyRecovery(
        case.kernel.database, case.subject_id, clock=lambda: case.clock_value
    )
    state = restarted.refresh()
    assert state is not None and state["stable_since"] == case.clock_value and state["active"]
    assert state["clock_status"] == "normal"


@pytest.mark.asyncio
async def test_budget_admission_uses_all_reservation_high_water_not_only_last_health_check(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.model.errors import BudgetExhaustedError

    await prepared(case)
    reserve(case, 1)
    with case.kernel.database.transaction() as c:
        state = read_recovery(c, case.subject_id)
        assert state is not None
        state["checked_at"] = "2026-08-13T12:00:00+00:00"
        state.pop("clock_high_water_at", None)
        write_recovery(c, case.subject_id, state, case.clock_value)
    case.clock_value = "2026-08-14T11:59:59+00:00"
    with pytest.raises(BudgetExhaustedError, match="recovery_clock_wait"):
        reserve(case, 2)


@pytest.mark.asyncio
async def test_new_replay_incident_does_not_reset_the_rolling_day_allowance(
    case: helpers.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("noyra.cognition.execution.utc_now", lambda: case.clock_value)
    from unittest.mock import Mock

    from noyra.cognition import ProjectPhaseExecutor
    from test_a0_project_recovery import active

    manager = await active(case)
    project = manager.projects()[0]
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    await executor.run_phase(project, manager.phases(project.project_id)[0])
    arm(case)
    reserve(case, 1)
    with case.kernel.database.transaction() as c:
        state = read_recovery(c, case.subject_id)
        assert state is not None
        state.update(active=False, reason="normal_operation_restored")
        write_recovery(c, case.subject_id, state, case.clock_value)
    # Reproduce the historical terminal-replay detector's real clock evidence.
    for _ in range(3):
        session = manager.begin_execution(project.project_id, "phase_execution")
        manager.finish_execution(project.project_id, session)
    recovery = AutonomyRecovery(
        case.kernel.database, case.subject_id, clock=lambda: case.clock_value
    )
    state = recovery.refresh()
    assert state is not None and state["active"]
    assert [r["attempt_id"] for r in state["reservations"]] == ["attempt-1"]


@pytest.mark.asyncio
async def test_recovery_exit_uses_the_same_real_project_dependency_wait_as_review(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.core.types import content_hash
    from test_a0_project_recovery import active

    manager = await active(case)
    project = manager.projects()[0]
    arm(case)
    with case.kernel.database.transaction() as c:
        state = read_recovery(c, case.subject_id)
        assert state is not None
        state["project_ids"] = [project.project_id]
        write_recovery(c, case.subject_id, state, case.clock_value)
        planner = c.execute(
            "SELECT call_id FROM model_calls WHERE subject_id=? LIMIT 1", (case.subject_id,)
        ).fetchone()[0]
        c.execute(
            "INSERT INTO research_search_runs(research_id,subject_id,planner_call_id,goal_id,"
            "idempotency_key,status,initial_method,final_method,query_hash,result_count,"
            "accepted_source_ids_json,rounds_json,plan_json,plan_hash,state_hash,"
            "created_at,completed_at) "
            "VALUES ('m2-dependency',?,?,?,'m2-dependency','no_results','browser','browser',?,0,"
            "'[]','[]','{}',?,?,?,?)",
            (
                case.subject_id,
                planner,
                project.goal_id,
                content_hash("query"),
                content_hash({}),
                content_hash({}),
                case.clock_value,
                case.clock_value,
            ),
        )
    assert manager.pending_dependencies(project)
    recovery = AutonomyRecovery(
        case.kernel.database, case.subject_id, clock=lambda: case.clock_value
    )
    state = recovery.refresh()
    assert state is not None
    assert not next(c for c in state["exit_conditions"] if c["code"] == "project_coordination")[
        "satisfied"
    ]
    manager.review_control.record(project, "waiting_dependencies")
    case.clock_value = (datetime.fromisoformat(case.clock_value) + timedelta(minutes=5)).isoformat()
    state = recovery.refresh()
    assert state is not None
    assert next(c for c in state["exit_conditions"] if c["code"] == "project_coordination")[
        "satisfied"
    ]
