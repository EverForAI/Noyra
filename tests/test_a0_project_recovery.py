from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import test_autonomous_projects as helpers
from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.cognition import AutonomousProjectManager, ProjectPhaseExecutor
from noyra.cognition.cycle import CognitionCycle
from noyra.model import BudgetLimits, ModelGateway
from noyra.model.errors import BudgetExhaustedError, ProviderCallError


@pytest.fixture
def case(monkeypatch: pytest.MonkeyPatch) -> Iterator[helpers.AutonomousProjectsTestCase]:
    value = helpers.AutonomousProjectsTestCase(methodName="runTest")
    value.setUp()
    monkeypatch.setattr("noyra.model.ledger.utc_now", lambda: value.clock_value)
    try:
        yield value
    finally:
        value.tearDown()


def review(
    case: helpers.AutonomousProjectsTestCase,
    manager: AutonomousProjectManager,
    disposition: str = "activate",
) -> dict[str, Any]:
    project = manager.projects()[0]
    return {
        "project_id": project.project_id,
        "phase_id": project.current_phase_id or manager.phases(project.project_id)[0].phase_id,
        "disposition": disposition,
        "summary": "A bounded review based on durable evidence.",
        "reason": "The available evidence supports this decision.",
        "evidence_event_ids": [case.event.event_id],
        "revised_budget": None,
        "help_title": None,
        "help_description": None,
        "help_public_summary": None,
    }


async def active(
    case: helpers.AutonomousProjectsTestCase, *, max_model_calls: int = 6
) -> AutonomousProjectManager:
    formation: dict[str, Any] = case.formation()
    formation["project_type"] = "collaboration"
    formation["budget"]["max_model_calls"] = max_model_calls
    formation["phases"][0]["output_type"] = "collaboration_request"
    gateway, _ = case.gateway([formation])
    manager = AutonomousProjectManager(
        case.kernel.database,
        case.subject_id,
        gateway,
        case.settings(),
        clock=lambda: case.clock_value,
    )
    assert await manager.run_due() == "autonomous_project_planned"
    manager.gateway = case.gateway([review(case, manager)])[0]
    assert await manager.run_due() == "autonomous_project_activate"
    return manager


def count(case: helpers.AutonomousProjectsTestCase, table: str) -> int:
    with case.kernel.database.connection() as c:
        return int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.mark.asyncio
async def test_terminal_replay_yields_and_does_not_start_clock(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    project = manager.projects()[0]
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    cycle = object.__new__(CognitionCycle)
    cycle.autonomous_projects = manager
    cycle.project_executor = executor
    cycle.clock = lambda: case.clock_value
    assert await cycle._run_project_phase() == "autonomous_project_phase_succeeded"
    clocks = count(case, "autonomous_project_execution_clock_events")
    for _ in range(30):
        assert await cycle._run_project_phase() is None
    assert count(case, "autonomous_project_execution_clock_events") == clocks
    assert count(case, "autonomous_project_executions") == 1
    assert manager.get(project.project_id).progress == 0


@pytest.mark.asyncio
async def test_new_terminal_result_is_reviewed_without_waiting_period(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    project = manager.projects()[0]
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    assert (
        await executor.run_phase(project, manager.phases(project.project_id)[0])
    ).status == "succeeded"
    manager.gateway = case.gateway([review(case, manager, "complete_phase")])[0]
    assert await manager.run_due() == "autonomous_project_complete_phase"
    assert manager.get(project.project_id).progress == 0.5
    assert manager.phases(project.project_id)[0].status == "completed"
    manager.verify_integrity()


@pytest.mark.asyncio
async def test_unrelated_observation_does_not_hold_project_review(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    case.record_analyzed_observation()
    with case.kernel.database.transaction() as c:
        c.execute("UPDATE observations SET status='new' WHERE subject_id=?", (case.subject_id,))
    assert not manager.pending_dependencies(manager.projects()[0])


@pytest.mark.asyncio
async def test_daily_quota_is_durable_wait_without_attempt_or_no_progress(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    case.clock_value = "2026-08-14T13:00:00.000+00:00"
    manager.settings = manager.settings.model_copy(update={"max_project_model_calls_per_day": 1})
    project = manager.projects()[0]
    before = manager.phases(project.project_id)[0]
    clocks = count(case, "autonomous_project_execution_clock_events")
    assert await manager.run_due() is None
    state = manager.review_control.read(project)
    assert state["reason"] == "waiting_daily_quota"
    assert state["next_due_at"].startswith("2026-08-15T00:00:00")
    for _ in range(8):
        assert await manager.run_due() is None
    after = manager.phases(project.project_id)[0]
    assert (before.attempt_count, before.no_progress_count) == (
        after.attempt_count,
        after.no_progress_count,
    )
    assert count(case, "autonomous_project_execution_clock_events") == clocks
    DurableWorkflowStore(case.kernel.database).verify_integrity(case.subject_id)


@pytest.mark.asyncio
async def test_definite_review_failure_backs_off_across_restart(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    case.clock_value = "2026-08-14T13:00:00.000+00:00"
    failing = AsyncMock(
        side_effect=ProviderCallError(
            "provider_connect_failed", retryable=True, outcome_unknown=False
        )
    )
    manager.gateway = Mock(spec=ModelGateway, complete_structured=failing)
    project = manager.projects()[0]
    assert await manager.run_due() is None
    state = manager.review_control.read(project)
    assert state["reason"] == "review_backoff"
    restarted = AutonomousProjectManager(
        case.kernel.database,
        case.subject_id,
        manager.gateway,
        manager.settings,
        clock=lambda: case.clock_value,
    )
    for _ in range(5):
        assert await restarted.run_due() is None
    assert failing.await_count == 1
    case.clock_value = (
        datetime.fromisoformat(state["next_due_at"]) + timedelta(seconds=1)
    ).isoformat()
    restarted.gateway = case.gateway([review(case, manager, "continue")])[0]
    assert await restarted.run_due() == "autonomous_project_continue"
    DurableWorkflowStore(case.kernel.database).verify_integrity(case.subject_id)


@pytest.mark.asyncio
async def test_unknown_review_does_not_get_new_paid_retry(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    case.clock_value = "2026-08-14T13:00:00.000+00:00"
    gateway, provider = case.gateway([])
    provider._outcomes.append(
        ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True)
    )
    manager.gateway = gateway
    project = manager.projects()[0]
    assert await manager.run_due() is None
    case.clock_value = "2026-08-16T13:00:00.000+00:00"
    for _ in range(5):
        assert await manager.run_due() is None
    assert len(provider.requests) == 1
    assert manager.review_control.read(project)["reason"] == "awaiting_reconciliation"


@pytest.mark.asyncio
async def test_budget_wait_yields_without_repeated_failure(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    case.clock_value = "2026-08-14T13:00:00.000+00:00"
    failing = AsyncMock(side_effect=BudgetExhaustedError("limited"))
    manager.gateway = Mock(spec=ModelGateway, complete_structured=failing)
    project = manager.projects()[0]
    assert await manager.run_due() is None
    for _ in range(8):
        assert await manager.run_due() is None
    assert failing.await_count == 1
    assert manager.review_control.read(project)["reason"] == "waiting_budget"


@pytest.mark.asyncio
async def test_help_has_one_public_inbox_item_and_no_fake_delivery(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    case.clock_value = "2026-08-14T13:00:00.000+00:00"
    proposal = review(case, manager, "request_help")
    proposal.update(
        help_title="Need evidence",
        help_description="Private working context.",
        help_public_summary="Please share one public source.",
    )
    manager.gateway = case.gateway([proposal])[0]
    assert await manager.run_due() == "autonomous_project_request_help"
    project = manager.projects()[0]
    for _ in range(12):
        assert await manager.run_due() is None
    assert count(case, "autonomous_project_assistance_requests") == 1
    assert count(case, "interactions") == 1
    with case.kernel.database.connection() as c:
        row = c.execute("SELECT content FROM interactions").fetchone()
    assert row[0] == "Please share one public source."
    assert manager.review_control.read(project)["reason"] == "help_pending_route"
    manager.verify_integrity()


@pytest.mark.asyncio
async def test_real_budget_denial_does_not_start_execution_clock(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    case.clock_value = "2026-08-14T13:00:00.000+00:00"
    gateway, provider = case.gateway([review(case, manager, "continue")])
    from noyra.model import BudgetLimits

    gateway.limits = BudgetLimits(0, 0, 0, 0)
    manager.gateway = gateway
    clocks = count(case, "autonomous_project_execution_clock_events")
    assert await manager.run_due() is None
    assert count(case, "autonomous_project_execution_clock_events") == clocks
    assert not provider.requests


@pytest.mark.asyncio
async def test_unknown_phase_yields_to_other_work_without_new_attempt(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    project = manager.projects()[0]
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    phase = manager.phases(project.project_id)[0]
    execution = executor.ledger.get_or_prepare(project, phase, workflow="collaboration_request")
    executor.ledger.transition(execution.execution_id, "executing", reason="fixture start")
    executor.ledger.transition(execution.execution_id, "unknown", reason="fixture lost receipt")
    clocks = count(case, "autonomous_project_execution_clock_events")
    for _ in range(5):
        assert await manager.run_due() is None
    assert manager.review_control.read(project)["reason"] == "awaiting_reconciliation"
    assert count(case, "autonomous_project_executions") == 1
    assert count(case, "autonomous_project_execution_clock_events") == clocks


@pytest.mark.asyncio
async def test_review_retry_limit_yields_and_preserves_phase_attempt(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    project = manager.projects()[0]
    phase = manager.phases(project.project_id)[0]
    manager.review_control.record(project, "review_retry_limit", retry_index=3)
    case.clock_value = "2026-08-19T13:00:00.000+00:00"
    calls = count(case, "model_calls")
    for _ in range(5):
        assert await manager.run_due() is None
    assert count(case, "model_calls") == calls
    assert manager.phases(project.project_id)[0].attempt_count == phase.attempt_count


def make_cycle(
    case: helpers.AutonomousProjectsTestCase,
    manager: AutonomousProjectManager,
    executor: ProjectPhaseExecutor,
) -> CognitionCycle:
    cycle = object.__new__(CognitionCycle)
    cycle.bootstrap = Mock()  # type: ignore[method-assign]
    cycle.kernel = case.kernel
    cycle.subject_id = case.subject_id
    cycle.settings = manager.settings
    cycle.embedding_index = None
    cycle.gateway = manager.gateway
    cycle._runnable_observation = Mock(return_value=None)  # type: ignore[method-assign]
    cycle.recovery = Mock()
    from noyra.core.fair_scheduling import FairScheduler
    from noyra.core.scheduling_clock import CooldownClock

    cycle.scheduler = FairScheduler(
        case.kernel.database,
        case.subject_id,
        clock=lambda: case.clock_value,
        monotonic_clock=lambda: datetime.fromisoformat(case.clock_value).timestamp(),
    )
    cycle._poll_clock = CooldownClock(lambda: datetime.fromisoformat(case.clock_value).timestamp())
    cycle.intrinsic_thought = Mock()
    cycle.genesis = Mock()
    cycle.genesis.start.return_value = SimpleNamespace(status="complete")
    cycle._handle_genesis_ready_for_sleep = Mock(return_value=None)  # type: ignore[method-assign]
    cycle.self_modification = Mock()
    cycle.self_modification.run_due.return_value = None
    cycle.outcome_evaluator = Mock()
    cycle.outcome_evaluator.run_due.return_value = None
    cycle.memory_consolidator = Mock()
    cycle.memory_consolidator.run_due.return_value = None
    for name in (
        "memory_integrator",
        "motivation_development",
        "self_model",
        "interaction_cognition",
    ):
        setattr(cycle, name, Mock(run_due=AsyncMock(return_value=None)))
    cycle.autonomous_expression = Mock(
        run_due=AsyncMock(return_value=None), _calls_today=Mock(return_value=0)
    )
    cycle.autonomous_assistance = Mock(run_due=AsyncMock(return_value=None))
    cycle.autonomous_projects = manager
    cycle.project_executor = executor
    cycle.clock = lambda: case.clock_value
    cycle._run_metacognitive_strategy = AsyncMock(return_value="metacognitive_wait")  # type: ignore[method-assign]
    cycle._pending_observation = Mock(return_value=None)  # type: ignore[method-assign]
    cycle._select_source = Mock(return_value=None)  # type: ignore[method-assign]
    return cycle


@pytest.mark.asyncio
async def test_full_cycle_reaches_world_after_completed_phase_replay(
    case: helpers.AutonomousProjectsTestCase,
) -> None:

    manager = await active(case)
    project = manager.projects()[0]
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    await executor.run_phase(project, manager.phases(project.project_id)[0])
    manager.settings = manager.settings.model_copy(update={"max_project_model_calls_per_day": 1})
    cycle = make_cycle(case, manager, executor)
    for _ in range(20):
        assert await cycle._run_once_impl() == "world_sources_waiting"
    assert cast(Mock, cycle._select_source).call_count == 20
    assert count(case, "autonomous_project_executions") == 1


@pytest.mark.asyncio
async def test_real_budget_refusal_can_resume_without_spending_failure_retries(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.model import BudgetLimits

    manager = await active(case)
    project = manager.projects()[0]
    case.clock_value = "2026-08-14T13:00:00.000+00:00"
    gateway, provider = case.gateway([review(case, manager, "continue")])
    gateway.limits = BudgetLimits(0, 0, 0, 0)
    manager.gateway = gateway
    assert await manager.run_due() is None
    state = manager.review_control.read(project)
    assert state["retry_index"] == 0
    case.clock_value = (
        datetime.fromisoformat(state["next_due_at"]) + timedelta(seconds=1)
    ).isoformat()
    gateway.limits = BudgetLimits(40, 500_000, 200_000, 5_000_000)
    assert await manager.run_due() == "autonomous_project_continue"
    assert len(provider.requests) == 1
    assert manager.phases(project.project_id)[0].no_progress_count == 0


@pytest.mark.asyncio
async def test_subject_wait_does_not_immediately_execute_a_new_phase_attempt(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    case.clock_value = "2026-08-14T13:00:00.000+00:00"
    manager.gateway = case.gateway([review(case, manager, "wait")])[0]
    assert await manager.run_due() == "autonomous_project_wait"
    cycle = object.__new__(CognitionCycle)
    cycle.autonomous_projects = manager
    cycle.project_executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    cycle.clock = lambda: case.clock_value
    before = count(case, "autonomous_project_execution_clock_events")
    for _ in range(8):
        assert await cycle._run_project_phase() is None
    assert count(case, "autonomous_project_execution_clock_events") == before
    assert count(case, "autonomous_project_executions") == 0


@pytest.mark.asyncio
async def test_72_hour_virtual_clock_keeps_review_budget_wait_from_starving_world(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    project = manager.projects()[0]
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    await executor.run_phase(project, manager.phases(project.project_id)[0])
    manager.gateway = Mock(
        spec=ModelGateway,
        complete_structured=AsyncMock(side_effect=BudgetExhaustedError("limited")),
    )
    cycle = make_cycle(case, manager, executor)
    initial_clocks = count(case, "autonomous_project_execution_clock_events")
    initial_calls = count(case, "model_calls")
    origin = datetime.fromisoformat(case.clock_value)
    for quarter_hour in range(72 * 4 + 1):
        case.clock_value = (origin + timedelta(minutes=15 * quarter_hour)).isoformat()
        assert await cycle._run_once_impl() == "world_sources_waiting"
    assert cast(Mock, cycle._select_source).call_count == 72 * 4 + 1
    assert count(case, "autonomous_project_execution_clock_events") == initial_clocks
    assert count(case, "model_calls") == initial_calls
    assert manager.phases(project.project_id)[0].attempt_count == 1
    manager.verify_integrity()


@pytest.mark.asyncio
async def test_completed_project_replay_allows_real_unchanged_feed_heartbeats(
    case: helpers.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from noyra.capability import CapabilityGrant, CapabilityStore, ToolRunner
    from noyra.core.admission import bind_lease
    from noyra.world import ObservationStore, SafeWebReader, SourceRegistry

    monkeypatch.setattr("noyra.world.source.utc_now", lambda: case.clock_value)
    manager = await active(case)
    project = manager.projects()[0]
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    await executor.run_phase(project, manager.phases(project.project_id)[0])
    # Daily review quota waits must still allow actual acquisition work.
    manager.settings = manager.settings.model_copy(update={"max_project_model_calls_per_day": 1})
    cycle = make_cycle(case, manager, executor)
    source = SourceRegistry(case.kernel.database).register(
        case.subject_id,
        "Feed fixture",
        "https://example.com/feed.xml",
        "rss",
        trust_score=0.5,
        status="active",
        reason="public fixture",
    )
    cycle.capabilities = CapabilityStore(case.kernel.database)
    cycle.capabilities.grant(
        case.subject_id,
        CapabilityGrant(
            capability_type="web_read",
            scope={"public_https": True},
            issuer="fixture",
            rate_limit_per_hour=24,
            side_effect=False,
        ),
        actor="operator",
    )
    cycle.sources = SourceRegistry(case.kernel.database)
    cycle.observations = ObservationStore(case.kernel.database)
    cycle.tools = ToolRunner(case.kernel.database)
    # Replace only the selection stub; use the actual selector and reader below.
    cycle._select_source = lambda: CognitionCycle._select_source(cycle)  # type: ignore[method-assign]
    cycle._assess_failure = Mock()  # type: ignore[method-assign]
    feed = (
        '<rss version="2.0"><channel><title>Updates</title><item><guid>one</guid>'
        "<title>Stable entry</title><link>https://example.com/item</link>"
        "<description>Stable evidence.</description></item></channel></rss>"
    )

    async def resolve(host: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/rss+xml"}, text=feed
            )
        )
    ) as client:
        cycle.reader = SafeWebReader(client=client, resolver=resolve, verify_peer_address=False)
        baseline = await cycle.reader.fetch(source)
        observation, _ = cycle.observations.ingest(
            case.subject_id, source.source_id, baseline, max_content_chars=50_000
        )
        assert observation is not None
        cycle.observations.mark(observation.observation_id, "analyzed", subject_id=case.subject_id)
        original_calls = count(case, "model_calls")
        origin = datetime.fromisoformat(case.clock_value)
        for minutes in (16, 32):
            case.clock_value = (origin + timedelta(minutes=minutes)).isoformat()
            lease = case.kernel.admission.begin("cognition")
            try:
                with bind_lease(lease):
                    assert await cycle._run_once_impl() == "observation_unchanged"
            finally:
                case.kernel.admission.finish(lease)
    with case.kernel.database.connection() as c:
        state = c.execute(
            "SELECT checks,unchanged_checks,last_attempt_at FROM world_poll_state "
            "WHERE source_id=?",
            (source.source_id,),
        ).fetchone()
    assert (state[0], state[1]) == (3, 2)
    assert state[2] == case.clock_value
    assert count(case, "observations") == 1
    assert count(case, "model_calls") == original_calls


@pytest.mark.asyncio
async def test_gateway_state_race_cannot_turn_an_unknown_call_into_a_new_retry(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.core.types import content_hash
    from noyra.model import ModelLedger
    from noyra.model.errors import ModelCallStateError

    manager = await active(case)
    project = manager.projects()[0]
    case.clock_value = "2026-08-14T13:00:00.000+00:00"

    async def race(*args: Any, **kwargs: Any) -> None:
        ledger = ModelLedger(case.kernel.database)
        call, _ = ledger.prepare_call(
            case.subject_id,
            "fake",
            "fixture",
            str(args[1]),
            content_hash("fixture-race"),
            str(kwargs["idempotency_key"]),
        )
        attempt = ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(40, 500_000, 200_000, 5_000_000),
            reserved_input_tokens=100,
            reserved_output_tokens=100,
            reserved_cost_microusd=0,
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
        raise ModelCallStateError("another route settled ambiguously")

    dispatch = AsyncMock(side_effect=race)
    manager.gateway = Mock(spec=ModelGateway, complete_structured=dispatch)
    assert await manager.run_due() is None
    state = manager.review_control.read(project)
    assert state["reason"] == "awaiting_reconciliation"
    assert (state["retry_index"], state["request_index"]) == (0, 0)
    case.clock_value = "2026-08-15T13:00:00.000+00:00"
    for _ in range(4):
        assert await manager.run_due() is None
    assert dispatch.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "created_at", "expected"),
    [
        ("rejected", "2026-08-14T13:00:00+00:00", False),
        ("no_results", "2026-08-13T13:00:00+00:00", False),
        ("no_results", "2026-08-14T13:00:00+00:00", True),
    ],
)
async def test_legacy_research_dependency_matches_evaluator_scope(
    case: helpers.AutonomousProjectsTestCase,
    status: str,
    created_at: str,
    expected: bool,
) -> None:
    from noyra.core.types import content_hash

    manager = await active(case)
    project = manager.projects()[0]
    with case.kernel.database.transaction() as c:
        planner = c.execute(
            "SELECT call_id FROM model_calls WHERE subject_id=? LIMIT 1", (case.subject_id,)
        ).fetchone()[0]
        c.execute(
            "INSERT INTO research_search_runs(research_id,subject_id,planner_call_id,goal_id,"
            "idempotency_key,status,initial_method,final_method,query_hash,result_count,"
            "accepted_source_ids_json,rounds_json,plan_json,plan_hash,state_hash,"
            "created_at,completed_at) "
            "VALUES ('fixture-dependency',?,?,?,'fixture-dependency',?,'browser','browser',?,0,"
            "'[]','[]','{}',?,?,?,?)",
            (
                case.subject_id,
                planner,
                project.goal_id,
                status,
                content_hash("fixture"),
                content_hash({}),
                content_hash({}),
                created_at,
                created_at,
            ),
        )
    assert manager.pending_dependencies(project) is expected


@pytest.mark.asyncio
async def test_completed_action_evaluation_releases_project_dependency(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.core import ActionLedger
    from noyra.core.types import content_hash
    from noyra.learning import OutcomeEvaluator
    from noyra.world import SourceRegistry

    manager = await active(case)
    project = manager.projects()[0]
    source = SourceRegistry(case.kernel.database).register(
        case.subject_id,
        "Fixture source",
        "https://example.com/evidence",
        "web",
        trust_score=0.5,
        status="active",
        reason="fixture",
    )
    ledger = ActionLedger(case.kernel.database)
    action = ledger.prepare(
        case.subject_id,
        "web_read",
        "web_read",
        source.url,
        {},
        goal_id=project.goal_id,
        project_id=project.project_id,
        phase_id=project.current_phase_id,
        strategy_id="fixture-strategy",
    )
    ledger.start(action.action_id)
    ledger.finish(action.action_id, "failed", {"error": "fixture_failure"})
    with case.kernel.database.transaction() as c:
        model_call = c.execute(
            "SELECT call_id FROM model_calls WHERE subject_id=? LIMIT 1", (case.subject_id,)
        ).fetchone()[0]
        c.execute(
            "INSERT INTO action_deliberation_runs(deliberation_id,subject_id,model_call_id,"
            "goal_id,source_id,action_id,idempotency_key,status,strategy_title,expected_observation,"
            "summary,reason,evidence_event_ids_json,proposal_json,proposal_hash,result_hash,"
            "state_hash,created_at,completed_at,project_id,phase_id) "
            "VALUES ('fixture-deliberation',?,?,?,?,?,'fixture-deliberation','failed',"
            "'fixture-strategy','fixture evidence','fixture summary','fixture reason',"
            "'[]','{}',?,?,?,?,?,?,?)",
            (
                case.subject_id,
                model_call,
                project.goal_id,
                source.source_id,
                action.action_id,
                content_hash({}),
                content_hash({}),
                content_hash({}),
                case.clock_value,
                case.clock_value,
                project.project_id,
                project.current_phase_id,
            ),
        )
    assert manager.pending_dependencies(project)
    evaluator = OutcomeEvaluator(
        case.kernel.database, case.subject_id, clock=lambda: case.clock_value, interval_seconds=0
    )
    assert evaluator.run_due() is not None
    assert evaluator.latest().source_id == "fixture-deliberation"  # type: ignore[union-attr]
    assert not manager.pending_dependencies(project)


@pytest.mark.asyncio
async def test_phase_dispatch_rechecks_existing_project_budget_before_clock_or_side_effect(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case, max_model_calls=2)
    project = manager.projects()[0]
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    cycle = make_cycle(case, manager, executor)
    before = count(case, "autonomous_project_execution_clock_events")
    assert await cycle._run_project_phase() == "autonomous_project_paused"
    assert manager.get(project.project_id).status == "paused"
    assert count(case, "autonomous_project_executions") == 0
    assert count(case, "autonomous_project_execution_clock_events") == before
    assert count(case, "interactions") == 0


@pytest.mark.asyncio
async def test_sensitive_new_help_summary_is_rejected_before_public_request(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await active(case)
    case.clock_value = "2026-08-14T13:00:00.000+00:00"
    proposal = review(case, manager, "request_help")
    proposal.update(
        help_title="Need evidence",
        help_description="Private working context.",
        help_public_summary="token: synthetic-private-credential",
    )
    manager.gateway = case.gateway([proposal])[0]
    phase = manager.phases(manager.projects()[0].project_id)[0]
    assert await manager.run_due() is None
    project = manager.projects()[0]
    assert manager.review_control.read(project)["reason"] == "review_backoff"
    assert count(case, "autonomous_project_assistance_requests") == 0
    assert count(case, "interactions") == 0
    assert manager.phases(project.project_id)[0].attempt_count == phase.attempt_count
    manager.verify_integrity()


@pytest.mark.asyncio
async def test_legacy_sensitive_help_wait_is_durable_and_yields_without_publication(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.cognition.types import AutonomousProjectReviewProposal
    from noyra.core.fair_scheduling import FairScheduler

    manager = await active(case)
    project = manager.projects()[0]
    proposal = review(case, manager, "request_help")
    proposal.update(
        help_title="Legacy public summary",
        help_description="Private working context.",
        help_public_summary="token: synthetic-private-credential",
    )
    # Seed the existing pre-upgrade request contract without rewriting its hash.
    with case.kernel.database.transaction() as connection:
        manager._create_assistance_request_connection(
            connection,
            project,
            manager.phases(project.project_id)[0],
            AutonomousProjectReviewProposal.model_validate(
                {**proposal, "evidence_event_ids": tuple(proposal["evidence_event_ids"])}
            ),
            case.clock_value,
        )
    before_calls = count(case, "model_calls")
    served: list[str] = []

    async def dispatch(name: str) -> str | None:
        if name == "project_review":
            return await manager.run_due()
        served.append(name)
        return None

    scheduler = FairScheduler(case.kernel.database, case.subject_id, clock=lambda: case.clock_value)
    assert await scheduler.run(dispatch, active=lambda: True) == "world_sources_waiting"
    state = manager.review_control.read(project)
    assert state["reason"] == "help_public_summary_requires_review"
    assert "world_acquisition" in served and "interaction" in served
    checkpoints = count(case, "workflow_checkpoints")
    # A restarted reader accepts the new reason; repeated waits do not grow history.
    from noyra.cognition.project_control import ProjectReviewControl

    assert ProjectReviewControl(case.kernel.database, case.subject_id).read(project) == state
    for _ in range(3):
        assert manager._help_wait(project)
    assert count(case, "workflow_checkpoints") == checkpoints
    assert count(case, "interactions") == 0
    assert count(case, "model_calls") == before_calls
    with case.kernel.database.connection() as connection:
        row = connection.execute(
            "SELECT public_summary FROM autonomous_project_assistance_requests"
        ).fetchone()
    assert row[0] == proposal["help_public_summary"]


def test_project_review_reason_literals_are_registered() -> None:
    import ast
    import inspect
    from pathlib import Path

    from noyra.cognition.project_control import REVIEW_WAIT_REASONS

    tree = ast.parse(inspect.getsource(AutonomousProjectManager))
    reasons = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if (
            (
                (
                    node.func.attr == "record"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "review_control"
                )
                or node.func.attr == "_review_defer"
            )
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
        ):
            reasons.add(node.args[1].value)
    assert reasons and reasons <= REVIEW_WAIT_REASONS
    admin = (Path(__file__).parents[1] / "src/noyra/web/admin.js").read_text(encoding="utf-8")
    assert all(reason + ":" in admin for reason in REVIEW_WAIT_REASONS)


@pytest.mark.asyncio
async def test_help_wait_observes_verified_delivery_reconciliation(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.interaction import DeliveryDispatcher, TransportInput, TransportStore

    manager = await active(case)
    await case.request_help(manager)
    transports = TransportStore(case.kernel.database, Path(case.temp_dir.name) / "secrets")
    transports.configure(
        case.subject_id,
        TransportInput(
            channel="webhook",
            label="Help destination",
            endpoint="https://example.com/help",
            settings={"recipient": "synthetic-founder"},
        ),
        actor="operator",
    )
    dispatcher = DeliveryDispatcher(case.kernel.database, transports)
    dispatcher.enqueue_pending(case.subject_id)
    with case.kernel.database.transaction() as connection:
        identifier = str(
            connection.execute("SELECT delivery_id FROM interaction_deliveries").fetchone()[0]
        )
        connection.execute(
            "UPDATE interaction_deliveries SET status='unknown',attempts=1 WHERE delivery_id=?",
            (identifier,),
        )
    project = manager.projects()[0]
    assert manager._help_wait(project)
    assert manager.review_control.read(project)["reason"] == "help_delivery_unknown"
    dispatcher.reconcile_unknown(
        identifier, "delivered", actor="operator", reason="Verified receipt"
    )
    calls = count(case, "model_calls")
    assert manager._help_wait(project)
    assert manager.review_control.read(project)["reason"] == "help_waiting_reply"
    assert count(case, "model_calls") == calls
    with case.kernel.database.connection() as connection:
        assert (
            connection.execute("SELECT status FROM interaction_deliveries").fetchone()[0]
            == "unknown"
        )
    manager.verify_integrity()
