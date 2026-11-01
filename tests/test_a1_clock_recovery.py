from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

import test_a0_project_recovery as projects
import test_autonomous_projects as project_helpers
from noyra.cognition import ProjectPhaseExecutor
from noyra.cognition.cycle import CognitionCycle
from noyra.core import Database, IdentityStore
from noyra.core.fair_scheduling import ScheduleState
from noyra.core.types import content_hash
from noyra.world import ObservationStore, SourceRegistry
from noyra.world.poll import SourcePollStore
from test_world_feed_poll import document, item, rss

case = projects.case


@pytest.mark.asyncio
async def test_poll_rollback_yields_without_fetch_or_rewriting_heartbeat(
    case: project_helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await projects.active(case)
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    cycle = projects.make_cycle(case, manager, executor)
    registry = SourceRegistry(case.kernel.database)
    source = registry.register(
        case.subject_id, "Clock feed", "https://example.com/clock", "rss", status="active"
    )
    from noyra.capability import CapabilityGrant, CapabilityStore

    cycle.sources = registry
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
    now = datetime.fromisoformat(case.clock_value)
    future = (now + timedelta(hours=1)).isoformat()
    store = ObservationStore(case.kernel.database)
    store.ingest(case.subject_id, source.source_id, document(source, rss(item()), future))
    cycle._select_source = lambda: CognitionCycle._select_source(cycle)  # type: ignore[method-assign]
    assert cycle._select_source() is None
    assert cycle._source_wait_reason == "world_poll_clock_wait"
    SourcePollStore(case.kernel.database).failed(
        case.subject_id, source.source_id, case.clock_value
    )
    with case.kernel.database.connection() as c:
        state = SourcePollStore._read(c, case.subject_id, source.source_id)
    assert state is not None and state["last_attempt_at"] == future and state["checks"] == 1
    case.clock_value = (now + timedelta(hours=1, minutes=16)).isoformat()
    assert cycle._select_source() == source


def test_scheduler_reports_clock_regression_and_never_rewrites_old_service_times(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "clock.sqlite3")
    IdentityStore(db).ensure("one", content_hash("clock"))
    state = ScheduleState(db, "one")
    state.begin("2026-08-15T12:00:00+00:00")
    state.begin("2026-08-15T10:00:00+00:00")
    saved = state.read()
    assert saved["clock_status"] == "wall_clock_behind_history"
    assert saved["clock_high_water_at"] == "2026-08-15T12:00:00+00:00"
    state.begin("2026-08-15T13:00:00+00:00")
    assert state.read()["clock_status"] == "normal"


@pytest.mark.asyncio
async def test_restart_during_wall_clock_rollback_does_not_revise_agenda_history(
    case: project_helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.cognition.thought import IntrinsicThought

    gateway, _ = case.gateway([])
    thought = IntrinsicThought(
        case.kernel.database,
        case.subject_id,
        gateway,
        case.settings(),
        clock=lambda: case.clock_value,
    )
    assert thought.discover_agenda()
    with case.kernel.database.connection() as c:
        before = [tuple(r) for r in c.execute("SELECT * FROM thought_agenda_revisions")]
    case.clock_value = "2026-08-13T12:00:00+00:00"
    restarted = IntrinsicThought(
        case.kernel.database,
        case.subject_id,
        gateway,
        case.settings(),
        clock=lambda: case.clock_value,
    )
    assert not restarted.discover_agenda()
    with case.kernel.database.connection() as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM thought_agenda_revisions")] == before
    thought.verify_integrity()


@pytest.mark.asyncio
async def test_agenda_discovery_reads_existing_candidates_in_batches_without_rewriting_history(
    case: project_helpers.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.cognition.thought import IntrinsicThought

    gateway, _ = case.gateway([])
    thought = IntrinsicThought(
        case.kernel.database,
        case.subject_id,
        gateway,
        case.settings(),
        clock=lambda: case.clock_value,
    )
    candidates = [
        {
            "source_type": "question",
            "source_id": content_hash(n),
            "topic": f"Question {n}",
            "urgency": 0.4,
            "novelty": 0.4,
            "emotional_weight": 0.2,
        }
        for n in range(80)
    ]
    monkeypatch.setattr(thought, "_agenda_candidates", lambda **kwargs: candidates)
    thought._refresh_agenda()
    queries: list[str] = []
    original = case.kernel.database._connect

    def connect() -> sqlite3.Connection:
        c = original()
        c.set_trace_callback(queries.append)
        return c

    monkeypatch.setattr(case.kernel.database, "_connect", connect)
    thought._refresh_agenda()
    probes = [q for q in queries if "JOIN thought_agenda_items a" in q]
    assert len(probes) == 1
    assert not any(
        q.lstrip()
        .upper()
        .startswith(("UPDATE THOUGHT_AGENDA_ITEMS", "INSERT INTO THOUGHT_AGENDA_REVISIONS"))
        for q in queries
    )
    with case.kernel.database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM thought_agenda_items").fetchone()[0] == 80
        assert c.execute("SELECT COUNT(*) FROM thought_agenda_revisions").fetchone()[0] == 80


@pytest.mark.asyncio
async def test_permanent_failed_source_cools_and_yields_to_other_public_sources(
    case: project_helpers.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from typing import cast

    import httpx

    from noyra.capability import CapabilityGrant, CapabilityStore, ToolRunner
    from noyra.core.admission import bind_lease
    from noyra.world import SafeWebReader
    from noyra.world.types import GenesisRunRecord

    manager = await projects.active(case)
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    cycle = projects.make_cycle(case, manager, executor)
    cycle.sources = SourceRegistry(case.kernel.database)
    bad = cycle.sources.register(
        case.subject_id, "Bad feed", "https://example.com/bad", "rss", status="active"
    )
    good = cycle.sources.register(
        case.subject_id, "Good feed", "https://example.org/good", "rss", status="active"
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
    cycle.tools = ToolRunner(case.kernel.database)
    cycle.observations = ObservationStore(case.kernel.database)
    cycle._assess_failure = Mock()  # type: ignore[method-assign]
    cycle._select_source = lambda: CognitionCycle._select_source(cycle)  # type: ignore[method-assign]
    monkeypatch.setattr("noyra.world.source.utc_now", lambda: case.clock_value)

    async def resolve(host: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    def response(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(503)
            if request.url.host == "example.com"
            else httpx.Response(
                200, headers={"content-type": "application/rss+xml"}, text=rss(item())
            )
        )

    with case.kernel.database.transaction() as c:
        SourcePollStore.complete(
            c, case.subject_id, good.source_id, case.clock_value, outcome="unchanged"
        )
    case.clock_value = (
        datetime.fromisoformat(case.clock_value) + timedelta(minutes=16)
    ).isoformat()
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        cycle.reader = SafeWebReader(client=client, resolver=resolve, verify_peer_address=False)
        assert cycle._select_source() == bad
        lease = case.kernel.admission.begin("cognition")
        try:
            with bind_lease(lease):
                assert (
                    await cycle._run_world(
                        cast(GenesisRunRecord, SimpleNamespace(status="complete")),
                        mode="acquisition",
                    )
                    == "world_fetch_failed"
                )
        finally:
            case.kernel.admission.finish(lease)
        assert cycle._select_source() == good
        lease = case.kernel.admission.begin("cognition")
        try:
            with bind_lease(lease):
                assert (
                    await cycle._run_world(
                        cast(GenesisRunRecord, SimpleNamespace(status="complete")),
                        mode="acquisition",
                    )
                    == "world_observation_recorded"
                )
        finally:
            case.kernel.admission.finish(lease)
    with case.kernel.database.connection() as c:
        failed = SourcePollStore._read(c, case.subject_id, bad.source_id)
        assert failed is not None and failed["checks"] == 1 and failed["outcome"] == "failed"
        assert failed["last_success_at"] is None
    assert cycle._select_source() is None


@pytest.mark.asyncio
async def test_embedding_maintenance_has_its_own_bounded_service_opportunity(
    case: project_helpers.AutonomousProjectsTestCase,
) -> None:
    manager = await projects.active(case)
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    cycle = projects.make_cycle(case, manager, executor)
    index = Mock()
    index.rebuild_missing.return_value = 2
    cycle.embedding_index = index
    outcome = await cycle._dispatch_workflow("embedding_maintenance")
    from noyra.core.fair_scheduling import WorkflowOutcome

    assert isinstance(outcome, WorkflowOutcome) and outcome.did_execute and outcome.made_progress
    index.rebuild_missing.assert_called_once()
    assert index.rebuild_missing.call_args.kwargs["limit"] == 32


@pytest.mark.asyncio
async def test_unknown_world_call_does_not_block_a_different_runnable_observation(
    case: project_helpers.AutonomousProjectsTestCase,
) -> None:

    from noyra.core.types import content_hash
    from noyra.model import BudgetLimits, ModelLedger

    manager = await projects.active(case)
    cycle = projects.make_cycle(
        case, manager, ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    )
    cycle.observations = ObservationStore(case.kernel.database)
    source = SourceRegistry(case.kernel.database).register(
        case.subject_id, "Fixture", "https://example.com/world", "rss", status="active"
    )
    first, _ = cycle.observations.ingest(
        case.subject_id, source.source_id, document(source, rss(item("first")), case.clock_value)
    )
    at = (datetime.fromisoformat(case.clock_value) + timedelta(seconds=1)).isoformat()
    second, _ = cycle.observations.ingest(
        case.subject_id, source.source_id, document(source, rss(item("first"), item("second")), at)
    )
    assert first is not None and second is not None
    ledger = ModelLedger(case.kernel.database)
    call, _ = ledger.prepare_call(
        case.subject_id,
        "fake",
        "fixture",
        f"world_cognition:{first.observation_id}",
        content_hash("first-request"),
        "unknown-first",
    )
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(100, 100000, 100000, 100000),
        reserved_input_tokens=10,
        reserved_output_tokens=10,
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
    assert CognitionCycle._runnable_observation(cycle).observation_id == second.observation_id  # type: ignore[union-attr]
    assert ledger.get_call(call.call_id).status == "unknown"
    assert len(ledger.attempts(call.call_id)) == 1
