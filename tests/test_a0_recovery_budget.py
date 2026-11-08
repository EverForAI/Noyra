from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest

import test_autonomous_projects as helpers
from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.cognition.recovery import AutonomyRecovery
from noyra.core.recovery_budget import (
    RECOVERY_DAILY_INPUT_TOKENS,
    RECOVERY_HOURLY_ATTEMPTS,
    admit_recovery_attempt,
    read_recovery,
    write_recovery,
)
from noyra.core.types import content_hash
from noyra.model import BudgetLimits, ModelLedger
from noyra.model.errors import BudgetExhaustedError


@pytest.fixture
def case(monkeypatch: pytest.MonkeyPatch) -> Iterator[helpers.AutonomousProjectsTestCase]:
    value = helpers.AutonomousProjectsTestCase(methodName="runTest")
    value.setUp()
    monkeypatch.setattr("noyra.model.ledger.utc_now", lambda: value.clock_value)
    try:
        yield value
    finally:
        value.tearDown()


def arm(case: helpers.AutonomousProjectsTestCase) -> None:
    now = case.clock_value
    state: dict[str, Any] = {
        "version": 1,
        "active": True,
        "incident": content_hash("fixture"),
        "project_ids": ["fixture-project"],
        "started_at": now,
        "checked_at": now,
        "reservations": [],
        "reason": "project_replay_recovery",
        "stable_since": None,
    }
    with case.kernel.database.transaction() as c:
        write_recovery(c, case.subject_id, state, now)


def reserve(case: helpers.AutonomousProjectsTestCase, index: int, tokens: int = 10) -> None:
    with case.kernel.database.transaction() as c:
        admit_recovery_attempt(
            c, case.subject_id, f"call-{index}", f"attempt-{index}", tokens, 10, case.clock_value
        )


def test_recovery_reservation_survives_restart_and_hour_rollover(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    arm(case)
    for index in range(RECOVERY_HOURLY_ATTEMPTS):
        reserve(case, index)
    with pytest.raises(BudgetExhaustedError):
        reserve(case, 7)
    with case.kernel.database.connection() as c:
        state = read_recovery(c, case.subject_id)
    assert state is not None and len(state["reservations"]) == RECOVERY_HOURLY_ATTEMPTS
    case.clock_value = (
        datetime.fromisoformat(case.clock_value) + timedelta(hours=1, seconds=1)
    ).isoformat()
    reserve(case, 7)
    DurableWorkflowStore(case.kernel.database).verify_integrity(case.subject_id)


def test_token_admission_is_atomic_with_attempt_ledger(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    arm(case)
    ledger = ModelLedger(case.kernel.database)
    call, _ = ledger.prepare_call(
        case.subject_id,
        "fake",
        "fixture",
        "world_cognition:fixture",
        content_hash("request"),
        "fixture-key",
    )
    with pytest.raises(BudgetExhaustedError):
        ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(100, 10**7, 10**7, 10**7),
            reserved_input_tokens=RECOVERY_DAILY_INPUT_TOKENS + 1,
            reserved_output_tokens=1,
            reserved_cost_microusd=0,
        )
    assert not ledger.attempts(call.call_id)
    with case.kernel.database.connection() as c:
        state = read_recovery(c, case.subject_id)
    assert state is not None and state["reservations"] == []


def test_recovery_clock_rollback_does_not_unlock_budget(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    arm(case)
    reserve(case, 1)
    case.clock_value = "2026-08-13T12:00:00.000+00:00"
    with pytest.raises(BudgetExhaustedError, match="recovery_clock_wait"):
        reserve(case, 2)


@pytest.mark.asyncio
async def test_recovery_guard_automatically_retires_without_operator_action(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager, _ = await case.form_project()
    project = manager.projects()[0]
    arm(case)
    with case.kernel.database.transaction() as c:
        state = read_recovery(c, case.subject_id)
        assert state is not None
        state["project_ids"] = [project.project_id]
        write_recovery(c, case.subject_id, state, case.clock_value)
    recovery = AutonomyRecovery(
        case.kernel.database, case.subject_id, clock=lambda: case.clock_value
    )
    state = recovery.refresh()
    assert state is not None and state["active"]
    origin = datetime.fromisoformat(case.clock_value)
    for minute in range(5, 66, 5):
        case.clock_value = (origin + timedelta(minutes=minute)).isoformat()
        state = recovery.refresh()
    assert state is not None and not state["active"]
    assert state["reason"] == "normal_operation_restored"
    DurableWorkflowStore(case.kernel.database).verify_integrity(case.subject_id)


def test_new_install_has_no_recovery_limit_without_incident(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    recovery = AutonomyRecovery(
        case.kernel.database, case.subject_id, clock=lambda: case.clock_value
    )
    assert recovery.refresh() is None
    reserve(case, 1, RECOVERY_DAILY_INPUT_TOKENS + 1)
    with case.kernel.database.connection() as c:
        assert read_recovery(c, case.subject_id) is None


@pytest.mark.asyncio
async def test_real_legacy_replay_is_detected_and_restart_preserves_reservations(
    case: helpers.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from noyra.cognition import ProjectPhaseExecutor
    from test_a0_project_recovery import active

    monkeypatch.setattr("noyra.cognition.execution.utc_now", lambda: case.clock_value)
    manager = await active(case)
    project = manager.projects()[0]
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    result = await executor.run_phase(project, manager.phases(project.project_id)[0])
    assert result.status == "succeeded"
    origin = datetime.fromisoformat(case.clock_value)
    for index in range(3):
        case.clock_value = (origin + timedelta(minutes=index + 1)).isoformat()
        session = manager.begin_execution(project.project_id, "phase_execution")
        manager.finish_execution(project.project_id, session)
    recovery = AutonomyRecovery(
        case.kernel.database, case.subject_id, clock=lambda: case.clock_value
    )
    state = recovery.refresh()
    assert state is not None and state["active"]
    reserve(case, 1)
    restarted = AutonomyRecovery(
        case.kernel.database, case.subject_id, clock=lambda: case.clock_value
    )
    state = restarted.refresh()
    assert state is not None and len(state["reservations"]) == 1
    assert state["stable_since"] is None
    manager.verify_integrity()


@pytest.mark.asyncio
async def test_offline_gap_is_not_evidence_of_continuous_health(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager, _ = await case.form_project()
    arm(case)
    with case.kernel.database.transaction() as c:
        state = read_recovery(c, case.subject_id)
        assert state is not None
        state["project_ids"] = [manager.projects()[0].project_id]
        write_recovery(c, case.subject_id, state, case.clock_value)
    first = AutonomyRecovery(case.kernel.database, case.subject_id, clock=lambda: case.clock_value)
    first.refresh()
    case.clock_value = "2026-08-17T12:00:00.000+00:00"
    restarted = AutonomyRecovery(
        case.kernel.database, case.subject_id, clock=lambda: case.clock_value
    )
    state = restarted.refresh()
    assert state is not None and state["active"]
    assert state["stable_since"] == case.clock_value


def test_rolling_day_budget_does_not_reset_at_midnight(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.core.recovery_budget import RECOVERY_DAILY_ATTEMPTS

    arm(case)
    origin = datetime.fromisoformat(case.clock_value)
    for index in range(RECOVERY_DAILY_ATTEMPTS):
        case.clock_value = (origin + timedelta(hours=index // RECOVERY_HOURLY_ATTEMPTS)).isoformat()
        reserve(case, index)
    case.clock_value = "2026-08-15T00:01:00.000+00:00"
    with pytest.raises(BudgetExhaustedError):
        reserve(case, 100)
    case.clock_value = "2026-08-15T12:00:01.000+00:00"
    reserve(case, 100)


def test_concurrent_ledgers_share_one_atomic_recovery_allowance(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    arm(case)

    def admitted(index: int) -> bool:
        ledger = ModelLedger(case.kernel.database)
        call, _ = ledger.prepare_call(
            case.subject_id,
            "fake",
            "fixture",
            f"fixture:{index}",
            content_hash(index),
            f"request-{index}",
        )
        try:
            ledger.authorize_attempt(
                call.call_id,
                BudgetLimits(100, 10**7, 10**7, 10**7),
                reserved_input_tokens=100,
                reserved_output_tokens=100,
                reserved_cost_microusd=0,
            )
            return True
        except BudgetExhaustedError:
            return False

    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(admitted, range(12)))
    assert sum(results) == RECOVERY_HOURLY_ATTEMPTS
    with case.kernel.database.connection() as c:
        state = read_recovery(c, case.subject_id)
        assert state is not None and len(state["reservations"]) == RECOVERY_HOURLY_ATTEMPTS
        assert (
            c.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0]
            == RECOVERY_HOURLY_ATTEMPTS
        )
    DurableWorkflowStore(case.kernel.database).verify_integrity(case.subject_id)


@pytest.mark.asyncio
async def test_runtime_summary_reports_recovery_without_private_state(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    import json

    from noyra.core.runtime_observation import RuntimeObserver
    from test_a0_project_recovery import active

    manager = await active(case)
    project = manager.projects()[0]
    manager.review_control.record(
        project, "waiting_budget", next_due_at="2026-08-15T00:00:00+00:00"
    )
    arm(case)
    result = RuntimeObserver(
        case.kernel.database.path, case.kernel.database.path.parent, case.subject_id
    ).snapshot(cognition_enabled=True, interval_seconds=30)
    assert result["database_observation"] == "ok"
    assert result["autonomy_observation"] == "ok"
    assert result["autonomy_recovery"]["active"] is True
    assert result["project_coordination"]["records"][0]["reason"] == "waiting_budget"
    assert "reservations" not in result["autonomy_recovery"]
    assert "request_json" not in json.dumps(result)
