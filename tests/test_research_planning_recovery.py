from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from pydantic import SecretStr

import test_autonomous_projects as projects_base
import test_research as research_base
from noyra.cognition import AutonomousResearch
from noyra.core import Database, EventStore
from noyra.mind import GoalCandidate, GoalStore
from noyra.model import (
    BudgetLimits,
    CognitiveResourceGroupInput,
    FakeProvider,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
    RoutedModelGateway,
)
from noyra.model.errors import ProviderCallError
from noyra.model.request_identity import is_business_round
from noyra.research import ResearchPlanProposal, SearchProviderInput


@pytest_asyncio.fixture
async def case() -> AsyncIterator[research_base.ResearchTestCase]:
    value = research_base.ResearchTestCase(methodName="runTest")
    value.setUp()
    try:
        yield value
    finally:
        await value.asyncTearDown()


def add_long_goals(
    case: research_base.ResearchTestCase, count: int = 8, *, escaped: bool = False
) -> None:
    for number in range(count):
        candidate = GoalStore(case.kernel.database).create_candidate(
            case.subject_id,
            GoalCandidate(
                title=(str(number) + ("\x01" * 255 if escaped else "长目标" * 100))[:256],
                description=("\x01" * 700 if escaped else "背景" * 350),
                origin="self",
                priority=0.9,
                commitment=0.8,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(case.evidence.event_id,),
            reason="Research budget fixture",
        )
        GoalStore(case.kernel.database).activate(
            candidate.goal_id,
            rationale="A bounded research interest",
            causal_source_ids=(case.evidence.event_id,),
        )


@pytest.mark.asyncio
async def test_minimum_context_budget_accepts_eight_legal_long_goals(
    case: research_base.ResearchTestCase,
) -> None:
    add_long_goals(case)
    worker = case.research(research_base.SearchFixtureProvider([]))
    worker.settings = worker.settings.model_copy(update={"max_research_context_chars": 8_000})
    context = worker._context(worker._active_goals(), [])
    payload = json.loads(context.serialized)
    assert len(context.serialized) <= 8_000
    assert payload["goals"]
    assert set(context.goals) == {item["goal_id"] for item in payload["goals"]}
    assert context.event_ids


@pytest.mark.asyncio
async def test_unknown_planner_is_not_reissued_on_the_next_budget_day(
    case: research_base.ResearchTestCase,
) -> None:
    provider = research_base.SearchFixtureProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True)
            for _ in range(2)
        ]
    )
    current = datetime.now(UTC)
    first = case.research(provider)
    first.clock = lambda: current.isoformat()
    assert await first.run_due() == "research_model_failed"
    current += timedelta(days=1)
    reopened = case.research(provider)
    reopened.clock = lambda: current.isoformat()
    assert await reopened.run_due() == "research_waiting_reconciliation"
    assert len(provider.requests) == 1


def wait_plan(case: research_base.ResearchTestCase) -> str:
    return json.dumps(
        {
            "summary": "Wait for more useful public evidence.",
            "disposition": "wait",
            "method": "wait",
            "reason": "Avoid redundant searching.",
            "evidence_event_ids": [case.evidence.event_id],
        }
    )


def planning_calls(case: research_base.ResearchTestCase) -> list[dict[str, Any]]:
    with case.kernel.database.connection() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM model_calls WHERE purpose LIKE 'research_plan:%' ORDER BY rowid"
            )
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("escaped", [False, True])
@pytest.mark.parametrize("provider_count", [0, 1, 20])
async def test_context_candidates_and_provider_maps_match_bounded_json(
    case: research_base.ResearchTestCase,
    escaped: bool,
    provider_count: int,
) -> None:
    add_long_goals(case, escaped=escaped)
    for number in range(provider_count):
        case.provider_store.configure(
            case.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label=(str(number) + "长标签" * 60)[:128],
                api_key="synthetic-key",
            ),
            actor="operator",
        )
    worker = case.research(research_base.SearchFixtureProvider([]))
    worker.settings = worker.settings.model_copy(update={"max_research_context_chars": 8_000})
    goals = worker._active_goals()
    context = worker._context(goals, case.provider_store.active(case.subject_id))
    payload = json.loads(context.serialized)
    assert len(context.serialized) <= 8_000
    shown_goals = [item["goal_id"] for item in payload["goals"]]
    assert shown_goals and shown_goals == [goal.goal_id for goal in goals[: len(shown_goals)]]
    assert set(context.goals) == set(shown_goals)
    assert set(context.providers) == {
        item["config_id"] for item in payload["configured_search_resources"]
    }
    assert context.event_ids == {item["event_id"] for item in payload["recent_events"]}
    assert len(payload["configured_search_resources"]) <= 16
    # An omitted provider remains invalid; the runtime does not silently
    # authorize hidden IDs simply because they exist in the database.
    omitted = set(p.config_id for p in case.provider_store.active(case.subject_id)) - set(
        context.providers
    )
    if omitted:
        invalid = ResearchPlanProposal.model_validate_json(
            case.plan(method="api", config_id=next(iter(omitted)))
        )
        invalid = invalid.model_copy(
            update={"goal_id": shown_goals[0], "evidence_event_ids": tuple(context.event_ids)[:1]}
        )
        with pytest.raises(ValueError, match="provider is unavailable"):
            worker._validate_plan(invalid, context)


@pytest.mark.asyncio
async def test_trimmed_goal_is_not_an_authorized_planner_candidate(
    case: research_base.ResearchTestCase,
) -> None:
    add_long_goals(case, escaped=True)
    worker = case.research(research_base.SearchFixtureProvider([]))
    worker.settings = worker.settings.model_copy(update={"max_research_context_chars": 8_000})
    goals = worker._active_goals()
    # Real legal event/source metadata can also occupy the optional envelope.
    from noyra.world import SourceRegistry

    SourceRegistry(case.kernel.database).register(
        case.subject_id,
        "Long optional source",
        "https://example.com/" + "x" * 2_000,
        "web",
        reason="Budget fixture",
    )
    context = worker._context(goals, [])
    shown = set(context.goals)
    assert shown and len(shown) < len(goals)
    omitted = next(goal.goal_id for goal in goals if goal.goal_id not in shown)
    plan = ResearchPlanProposal.model_validate_json(case.plan(method="browser")).model_copy(
        update={"goal_id": omitted, "evidence_event_ids": tuple(context.event_ids)[:1]}
    )
    with pytest.raises(ValueError, match="goal is unavailable"):
        worker._validate_plan(plan, context)


@pytest.mark.asyncio
async def test_small_context_reaches_the_model_and_commits_a_valid_wait(
    case: research_base.ResearchTestCase,
) -> None:
    add_long_goals(case)
    provider = research_base.SearchFixtureProvider(
        [ProviderResponse(wait_plan(case), ModelUsage(50, 15))]
    )
    worker = case.research(provider)
    worker.settings = worker.settings.model_copy(update={"max_research_context_chars": 8_000})
    # Use an evidence ID actually visible in the reduced context.
    context = worker._context(worker._active_goals(), [])
    output = json.loads(wait_plan(case))
    output["evidence_event_ids"] = list(context.event_ids)[:1]
    provider = research_base.SearchFixtureProvider(
        [ProviderResponse(json.dumps(output), ModelUsage(50, 15))]
    )
    worker.gateway = case.gateway(provider)
    assert await worker.run_due() == "research_waited"
    assert len(provider.requests) == 1
    assert worker.verify_integrity() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["prepared", "executing", "unknown"])
async def test_legacy_dated_calls_survive_day_context_resource_and_database_changes(
    case: research_base.ResearchTestCase,
    status: str,
) -> None:
    database = case.kernel.database
    provider = research_base.SearchFixtureProvider([])
    worker = case.research(provider)
    ledger = ModelLedger(database)
    call, _ = ledger.prepare_call(
        case.subject_id,
        "retired-provider",
        "retired-model",
        "research_plan:0",
        "legacy-context-hash",
        "research-plan:0:2026-10-31",
    )
    if status != "prepared":
        attempt = ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(10, 100000, 100000, 1000000),
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=0,
        )
        if status == "unknown":
            ledger.start_attempt(attempt.attempt_id)
            ledger.finish_attempt(attempt.attempt_id, "unknown", usage=None, cost_microusd=None)
            ledger.finish_call(call.call_id, "unknown", error_code="provider_outcome_unknown")
    EventStore(database).append(case.subject_id, "new_fact", "world", {"changed": True})
    reopened = AutonomousResearch(
        Database(database.path, initialize=False),
        case.subject_id,
        case.gateway(provider),
        worker.settings,
        secret_dir=case.root / "secrets" / "search",
        search_executor=case.executor,
        browser_search_executor=case.browser_executor,
        reader=case.reader,
        clock=lambda: (datetime.now(UTC) + timedelta(days=2)).isoformat(),
    )
    result = await reopened.run_due()
    assert result == (
        "research_planner_context_wait"
        if status == "prepared"
        else "research_waiting_reconciliation"
    )
    assert provider.requests == []
    assert len(planning_calls(case)) == 1
    assert ledger.get_call(call.call_id).status == status


@pytest.mark.asyncio
async def test_explicit_failed_reconciliation_allows_bounded_retry_after_cooldown(
    case: research_base.ResearchTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr("noyra.model.ledger.utc_now", lambda: now.isoformat())
    provider = research_base.SearchFixtureProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            ProviderResponse(wait_plan(case), ModelUsage(50, 15)),
        ]
    )
    worker = case.research(provider)
    worker.clock = lambda: now.isoformat()
    assert await worker.run_due() == "research_model_failed"
    original = planning_calls(case)[0]
    ModelLedger(case.kernel.database).reconcile_unknown(
        original["call_id"],
        outcome="failed",
        actor="operator",
        reason="Synthetic provider evidence confirmed no result",
    )
    assert await worker.run_due() == "research_planner_wait"
    now += timedelta(seconds=61)
    assert await worker.run_due() == "research_waited"
    assert len(provider.requests) == 2
    calls = planning_calls(case)
    assert len(calls) == 2 and calls[0]["status"] == "failed" and calls[1]["status"] == "succeeded"
    assert calls[0]["purpose"] == calls[1]["purpose"]
    assert calls[0]["idempotency_key"] != calls[1]["idempotency_key"]


@pytest.mark.asyncio
async def test_known_failures_have_cooldown_daily_cap_and_clock_rollback_wait(
    case: research_base.ResearchTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr("noyra.model.ledger.utc_now", lambda: now.isoformat())
    provider = research_base.SearchFixtureProvider(
        [
            ProviderCallError("provider_http_429", retryable=False, outcome_unknown=False)
            for _ in range(3)
        ]
    )
    worker = case.research(provider)
    worker.clock = lambda: now.isoformat()
    assert await worker.run_due() == "research_model_failed"
    assert await worker.run_due() == "research_planner_wait"
    now += timedelta(seconds=61)
    assert await worker.run_due() == "research_model_failed"
    now += timedelta(hours=2)
    assert await worker.run_due() == "research_planner_wait"
    assert len(provider.requests) == 2
    now -= timedelta(hours=4)
    assert await worker.run_due() == "research_planner_wait"
    now += timedelta(days=1)
    assert await worker.run_due() == "research_model_failed"
    assert len(provider.requests) == 3
    assert len({row["idempotency_key"] for row in planning_calls(case)}) == 3


@pytest.mark.asyncio
async def test_successful_uncommitted_legacy_plan_recovers_before_current_day_quota(
    case: research_base.ResearchTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = case.research(research_base.SearchFixtureProvider([]))
    provider = research_base.SearchFixtureProvider(
        [ProviderResponse(wait_plan(case), ModelUsage(50, 15))]
    )
    gateway = case.gateway(provider)
    context = worker._context(worker._active_goals(), [])
    result = await gateway.complete_structured(
        case.subject_id,
        "research_plan:0",
        worker._planning_messages(context),
        ResearchPlanProposal,
        idempotency_key="research-plan:0:2026-10-31",
    )
    worker.gateway = case.gateway(research_base.SearchFixtureProvider([]))
    worker.clock = lambda: (datetime.now(UTC) + timedelta(days=2)).isoformat()
    monkeypatch.setattr(
        worker, "_calls_today", lambda _: worker.settings.max_research_model_calls_per_day
    )
    assert await worker.run_due() == "research_waited"
    latest = worker.latest()
    assert latest is not None and latest.research_id
    assert len(planning_calls(case)) == 1
    assert planning_calls(case)[0]["call_id"] == result.call_id
    assert worker.verify_integrity() == 1


@pytest.mark.parametrize("purpose", ["research_plan:0", "research_plan:15:project-id:phase-id"])
def test_research_planner_uses_existing_business_round_fence(purpose: str) -> None:
    assert is_business_round(purpose)
    assert not is_business_round(purpose + ":extra")
    assert not is_business_round("research_plan:not-a-round")


@pytest.mark.asyncio
async def test_matching_prepared_legacy_request_resumes_without_new_identity_or_call_quota(
    case: research_base.ResearchTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = research_base.SearchFixtureProvider(
        [ProviderResponse(wait_plan(case), ModelUsage(50, 15))]
    )
    worker = case.research(provider)
    original_prepare = worker.gateway.ledger.prepare_call

    class InterruptedBeforeAttempt(Exception):
        pass

    def prepare_then_stop(*args: Any, **kwargs: Any) -> Any:
        original_prepare(*args, **kwargs)
        raise InterruptedBeforeAttempt

    with (
        patch.object(worker.gateway.ledger, "prepare_call", side_effect=prepare_then_stop),
        pytest.raises(InterruptedBeforeAttempt),
    ):
        await worker.run_due()
    assert len(planning_calls(case)) == 1 and planning_calls(case)[0]["status"] == "prepared"
    assert provider.requests == []
    worker.settings = worker.settings.model_copy(update={"max_research_model_calls_per_day": 1})
    assert await worker.run_due() == "research_waited"
    assert len(provider.requests) == 1 and len(planning_calls(case)) == 1


@pytest.mark.asyncio
async def test_rejected_planner_is_recorded_once_then_retries_within_budget(
    case: research_base.ResearchTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr("noyra.model.ledger.utc_now", lambda: now.isoformat())
    invalid = json.loads(case.plan(method="browser"))
    invalid["goal_id"] = "goal_not_supplied"
    provider = research_base.SearchFixtureProvider(
        [
            ProviderResponse(json.dumps(invalid), ModelUsage(50, 15)),
            ProviderResponse(wait_plan(case), ModelUsage(50, 15)),
        ]
    )
    worker = case.research(provider)
    worker.clock = lambda: now.isoformat()
    assert await worker.run_due() == "research_rejected"
    for _ in range(3):
        assert await worker.run_due() == "research_planner_wait"
    with case.kernel.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='research_rejected'"
            ).fetchone()[0]
            == 1
        )
    now += timedelta(seconds=61)
    assert await worker.run_due() == "research_waited"
    assert len(provider.requests) == 2
    EventStore(case.kernel.database).verify_chain(case.subject_id)


@pytest.mark.asyncio
async def test_routed_prepared_and_failed_one_shot_retry_keep_existing_fences(
    case: research_base.ResearchTestCase,
) -> None:
    from test_audit_20261022_remediation import resources_for

    store, _ = resources_for(case.kernel.database, case.subject_id, case.root)
    provider = research_base.SearchFixtureProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            ProviderCallError("known-retry-failure", retryable=False, outcome_unknown=False),
        ]
    )
    router = RoutedModelGateway(
        case.kernel.database, case.subject_id, store, provider_factory=lambda _: provider
    )
    worker = case.research(provider)
    worker.gateway = router
    assert await worker.run_due() == "research_model_failed"
    call = planning_calls(case)[0]
    router.ledger.prepare_unknown_retry(
        call["call_id"], actor="operator", reason="Authorize one synthetic retry"
    )
    # Dispatch the exact original request through the established router. This
    # simulates an authorized retry failing before the research lane returns.
    context = worker._context(worker._active_goals(), [])
    logical_key, prepared = worker._planner_key("research_plan:0")
    assert prepared and logical_key == "research-plan:0"
    assert logical_key is not None
    with pytest.raises(ProviderCallError, match="known-retry-failure"):
        await router.complete_structured(
            case.subject_id,
            "research_plan:0",
            worker._planning_messages(context),
            ResearchPlanProposal,
            idempotency_key=logical_key,
            max_output_tokens=min(2000, worker.settings.max_output_tokens),
            temperature=worker.settings.temperature,
        )
    assert planning_calls(case)[0]["status"] == "failed"
    assert await worker.run_due() == "research_waiting_reconciliation"
    assert len(provider.requests) == 2 and len(planning_calls(case)) == 1


@pytest.mark.asyncio
async def test_routed_concurrent_research_planners_have_one_owner(
    case: research_base.ResearchTestCase,
) -> None:
    from test_audit_20261022_remediation import resources_for

    store, _ = resources_for(case.kernel.database, case.subject_id, case.root)
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingProvider(FakeProvider):
        async def complete(self, request: Any) -> ProviderResponse:
            self.requests.append(request)
            entered.set()
            await release.wait()
            return ProviderResponse(wait_plan(case), ModelUsage(50, 15))

    provider = BlockingProvider([])
    router = RoutedModelGateway(
        case.kernel.database, case.subject_id, store, provider_factory=lambda _: provider
    )
    worker = case.research(provider)
    worker.gateway = router
    task = asyncio.create_task(worker.run_due())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        second = case.research(provider)
        second.gateway = router
        assert await second.run_due() == "research_waiting_reconciliation"
        assert len(provider.requests) == 1
    finally:
        release.set()
        assert await task == "research_waited"
    assert len(planning_calls(case)) == 1


@pytest.fixture
def project_case() -> Iterator[projects_base.AutonomousProjectsTestCase]:
    value = projects_base.AutonomousProjectsTestCase(methodName="runTest")
    value.setUp()
    try:
        yield value
    finally:
        value.tearDown()


@pytest.mark.asyncio
async def test_project_unresolved_round_survives_other_lane_commit_and_keeps_bound_goal(
    project_case: projects_base.AutonomousProjectsTestCase,
) -> None:
    owner = project_case
    manager, _ = await owner.form_project()
    project = manager.projects()[0]
    phase = manager.phases(project.project_id)[0]
    gateway, _ = owner.gateway([])
    provider = FakeProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            ProviderResponse(
                json.dumps(
                    {
                        "summary": "Wait independently",
                        "disposition": "wait",
                        "method": "wait",
                        "reason": "Wait for evidence",
                        "evidence_event_ids": [owner.event.event_id],
                    }
                ),
                ModelUsage(50, 15),
            ),
        ]
    )
    gateway.provider = provider
    worker = AutonomousResearch(
        owner.kernel.database,
        owner.subject_id,
        gateway,
        owner.settings(),
        secret_dir=owner.kernel.database.path.parent / "secrets" / "search",
        clock=lambda: datetime.now(UTC).isoformat(),
    )
    try:
        worker.bind_project_phase(project.project_id, phase.phase_id)
        assert [goal.goal_id for goal in worker._active_goals()] == [project.goal_id]
        assert await worker.run_due() == "research_model_failed"
        worker.clear_project_binding()
        assert await worker.run_due() == "research_waited"
        assert worker._committed_count() == 1
        worker.bind_project_phase(project.project_id, phase.phase_id)
        assert await worker.run_due() == "research_waiting_reconciliation"
        assert len(provider.requests) == 2
        with owner.kernel.database.connection() as connection:
            uncertain = connection.execute(
                "SELECT purpose FROM model_calls WHERE status='unknown'"
            ).fetchone()
            assert uncertain[0] == f"research_plan:0:{project.project_id}:{phase.phase_id}"
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_routed_unknown_cannot_switch_to_a_new_resource_on_a_new_day(
    case: research_base.ResearchTestCase,
) -> None:
    from test_audit_20261022_remediation import resources_for

    store, _ = resources_for(case.kernel.database, case.subject_id, case.root)
    provider = research_base.SearchFixtureProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
        ]
    )
    router = RoutedModelGateway(
        case.kernel.database, case.subject_id, store, provider_factory=lambda _: provider
    )
    worker = case.research(provider)
    worker.gateway = router
    assert await worker.run_due() == "research_model_failed"
    store.configure(
        case.subject_id,
        CognitiveResourceGroupInput(
            pool="economy",
            label="New resource",
            model="new-model",
            base_url="https://new-model.example/v1",
            api_keys=(SecretStr("synthetic-new-key"),),
            priority=1,
            max_attempts=1,
        ),
        actor="operator",
    )
    EventStore(case.kernel.database).append(
        case.subject_id, "new_world_fact", "world", {"later": True}
    )
    worker.clock = lambda: (datetime.now(UTC) + timedelta(days=2)).isoformat()
    for _ in range(3):
        assert await worker.run_due() == "research_waiting_reconciliation"
    assert len(provider.requests) == 1 and len(planning_calls(case)) == 1
    assert planning_calls(case)[0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_provider_evidence_can_reconcile_unknown_success_without_a_new_request(
    case: research_base.ResearchTestCase,
) -> None:
    provider = research_base.SearchFixtureProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
        ]
    )
    worker = case.research(provider)
    assert await worker.run_due() == "research_model_failed"
    call = planning_calls(case)[0]
    ModelLedger(case.kernel.database).reconcile_unknown(
        call["call_id"],
        outcome="succeeded",
        actor="operator",
        reason="Synthetic provider response was retrieved",
        response={"content": wait_plan(case), "usage": {"input_tokens": 50, "output_tokens": 15}},
    )
    assert await worker.run_due() == "research_waited"
    assert len(provider.requests) == 1 and len(planning_calls(case)) == 1
    assert worker.verify_integrity() == 1
