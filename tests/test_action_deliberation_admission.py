from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio

import test_action_deliberation as helpers
from noyra.core.actions import ActionLedger
from noyra.model import FakeProvider, ModelUsage, ProviderResponse


@pytest_asyncio.fixture
async def case() -> AsyncIterator[helpers.ActionDeliberationTestCase]:
    value = helpers.ActionDeliberationTestCase(methodName="runTest")
    value.setUp()
    try:
        yield value
    finally:
        await value.asyncTearDown()


def blocker(case: helpers.ActionDeliberationTestCase, status: str = "prepared") -> Any:
    ledger = ActionLedger(case.kernel.database)
    action = ledger.prepare(
        case.subject_id,
        "local_inspection",
        "fixture",
        "local",
        {"fixture": status},
        side_effect=False,
        idempotency_key="blocker",
    )
    if status in {"executing", "unknown"}:
        action = ledger.start(action.action_id)
    if status == "unknown":
        action = ledger.finish(action.action_id, "unknown", {"error": "synthetic interruption"})
    return action


def raw(case: helpers.ActionDeliberationTestCase, table: str) -> list[dict[str, Any]]:
    assert table in {
        "actions",
        "model_calls",
        "model_attempts",
        "action_deliberation_runs",
        "events",
    }
    with case.kernel.database.read_transaction() as c:
        return [dict(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["prepared", "executing", "unknown"])
async def test_preexisting_open_action_prevents_any_paid_deliberation(
    case: helpers.ActionDeliberationTestCase, status: str
) -> None:
    action = blocker(case, status)
    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    worker = case.deliberation(provider)
    original = raw(case, "actions")
    for _ in range(3):
        assert await worker.run_due() == "action_deliberation_waiting_reconciliation"
    assert not provider.requests
    assert not raw(case, "model_calls") and not raw(case, "model_attempts")
    assert raw(case, "actions") == original and not raw(case, "action_deliberation_runs")
    assert original[0]["action_id"] == action.action_id


@pytest.mark.asyncio
async def test_action_appearing_after_paid_success_defers_original_result_until_resolved(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    provider = FakeProvider(
        [ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50)) for _ in range(2)]
    )
    worker = case.deliberation(provider)
    complete = provider.complete

    async def appear(request: Any) -> Any:
        response = await complete(request)
        blocker(case)
        return response

    with patch.object(provider, "complete", side_effect=appear):
        assert await worker.run_due() == "action_deliberation_waiting_reconciliation"
    calls, attempts = raw(case, "model_calls"), raw(case, "model_attempts")
    for _ in range(3):
        assert await worker.run_due() == "action_deliberation_waiting_reconciliation"
    assert len(provider.requests) == 1
    assert raw(case, "model_calls") == calls and raw(case, "model_attempts") == attempts
    assert not any(e["event_type"] == "action_deliberation_rejected" for e in raw(case, "events"))
    ActionLedger(case.kernel.database).cancel(
        raw(case, "actions")[0]["action_id"], "Verified unsent"
    )
    restarted = case.deliberation(provider)
    assert await restarted.run_due() == "action_deliberation_observed"
    assert len(provider.requests) == 1 and len(raw(case, "action_deliberation_runs")) == 1


@pytest.mark.asyncio
async def test_model_preparation_rechecks_action_slot_before_reserving_usage(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    worker = case.deliberation(provider)
    context = worker._context

    def race(*args: Any, **kwargs: Any) -> Any:
        value = context(*args, **kwargs)
        blocker(case)
        return value

    with patch.object(worker, "_context", side_effect=race):
        assert await worker.run_due() == "action_deliberation_waiting_reconciliation"
    assert (
        not provider.requests and not raw(case, "model_calls") and not raw(case, "model_attempts")
    )
    assert len(raw(case, "actions")) == 1 and not raw(case, "action_deliberation_runs")


@pytest.mark.asyncio
async def test_action_prepare_rechecks_slot_atomically_and_preserves_paid_response(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    worker = case.deliberation(provider)
    execute = worker._execute

    async def race(*args: Any, **kwargs: Any) -> str:
        blocker(case)
        return await execute(*args, **kwargs)

    with patch.object(worker, "_execute", side_effect=race):
        assert await worker.run_due() == "action_deliberation_waiting_reconciliation"
    assert len(provider.requests) == 1 and len(raw(case, "actions")) == 1
    assert not raw(case, "action_deliberation_runs")
    assert not any(e["event_type"] == "action_deliberation_rejected" for e in raw(case, "events"))
    ActionLedger(case.kernel.database).cancel(raw(case, "actions")[0]["action_id"], "Unsent")
    assert await worker.run_due() == "action_deliberation_observed"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_other_subject_open_action_does_not_block_subject_deliberation(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    from noyra.core.identity import IdentityStore
    from noyra.core.types import content_hash

    IdentityStore(case.kernel.database).ensure("other", content_hash("other"))
    other = ActionLedger(case.kernel.database).prepare(
        "other", "inspection", "fixture", "local", {}, idempotency_key="other"
    )
    original = raw(case, "actions")[0]
    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    assert await case.deliberation(provider).run_due() == "action_deliberation_observed"
    assert len(provider.requests) == 1 and raw(case, "actions")[0] == original
    assert original["action_id"] == other.action_id


@pytest.mark.asyncio
async def test_paid_wait_survives_event_window_churn_and_quota_without_new_model_call(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    from datetime import UTC, datetime, timedelta

    from noyra.core import EventStore

    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    worker = case.deliberation(provider, max_calls=1)
    complete = provider.complete

    async def appear(request: Any) -> Any:
        result = await complete(request)
        blocker(case)
        return result

    with patch.object(provider, "complete", side_effect=appear):
        assert await worker.run_due() == "action_deliberation_waiting_reconciliation"
    calls, attempts = raw(case, "model_calls"), raw(case, "model_attempts")
    now = datetime.now(UTC)
    for number in range(55):
        EventStore(case.kernel.database).append(
            case.subject_id,
            "new_observation",
            "fixture",
            {"n": number},
            occurred_at=(now + timedelta(seconds=number + 1)).isoformat(),
        )
    assert (
        case.evidence.event_id
        not in worker._context(worker._active_goals(), worker._authorized_sources()).event_ids
    )
    assert await worker.run_due() == "action_deliberation_waiting_reconciliation"
    ActionLedger(case.kernel.database).cancel(raw(case, "actions")[0]["action_id"], "Unsent")
    worker = case.deliberation(provider, max_calls=1)
    assert await worker.run_due() == "action_deliberation_observed"
    assert len(provider.requests) == 1
    assert raw(case, "model_calls") == calls and raw(case, "model_attempts") == attempts
    assert worker.verify_integrity() == 1


@pytest.mark.asyncio
async def test_unknown_model_round_never_receives_a_new_identity_after_day_or_restart(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    from noyra.model.errors import ProviderCallError

    provider = FakeProvider(
        [ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True)]
    )
    worker = case.deliberation(provider)
    assert await worker.run_due() == "action_deliberation_model_failed"
    calls, attempts = raw(case, "model_calls"), raw(case, "model_attempts")
    for now in ("2026-11-05T12:00:00+00:00", "2026-11-06T12:00:00+00:00"):
        worker = case.deliberation(provider)

        def current_time(value: str = now) -> str:
            return value

        worker.clock = current_time
        assert await worker.run_due() == "action_deliberation_waiting_reconciliation"
    assert len(provider.requests) == 1 and not raw(case, "actions")
    assert raw(case, "model_calls") == calls and raw(case, "model_attempts") == attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["executing", "unknown"])
async def test_own_interrupted_action_is_not_restarted_or_recorded_as_failed(
    case: helpers.ActionDeliberationTestCase, status: str
) -> None:
    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    worker = case.deliberation(provider)
    with (
        patch.object(worker, "_execute", side_effect=RuntimeError("before action")),
        pytest.raises(RuntimeError),
    ):
        await worker.run_due()
    identifier = raw(case, "model_calls")[0]["call_id"]
    ledger = ActionLedger(case.kernel.database)
    own = ledger.prepare(
        case.subject_id,
        "web_read",
        "safe_web_reader",
        case.source.url,
        {"source_id": case.source.source_id, "url": case.source.url},
        idempotency_key="action-deliberation:" + identifier,
    )
    own = ledger.start(own.action_id)
    if status == "unknown":
        ledger.finish(own.action_id, "unknown", {"error": "interrupted"})
    actions = raw(case, "actions")
    assert await worker.run_due() == "action_deliberation_waiting_reconciliation"
    assert raw(case, "actions") == actions and not raw(case, "action_deliberation_runs")
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_concurrent_deliberation_workers_share_one_paid_response_and_action(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()
    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    complete = provider.complete

    async def slow(request: Any) -> Any:
        entered.set()
        await release.wait()
        return await complete(request)

    first, second = case.deliberation(provider), case.deliberation(provider)
    with patch.object(provider, "complete", side_effect=slow):
        task = asyncio.create_task(first.run_due())
        await asyncio.wait_for(entered.wait(), 5)
        assert await second.run_due() == "action_deliberation_waiting_reconciliation"
        release.set()
        assert await task == "action_deliberation_observed"
    assert (
        len(provider.requests) == 1
        and len(raw(case, "actions")) == 1
        and len(raw(case, "action_deliberation_runs")) == 1
    )


@pytest.mark.asyncio
async def test_corrupted_binding_cannot_trigger_replacement_paid_response(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    from noyra.core.errors import IntegrityError

    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    worker = case.deliberation(provider)
    with (
        patch.object(worker, "_execute", side_effect=RuntimeError("before action")),
        pytest.raises(RuntimeError),
    ):
        await worker.run_due()
    with case.kernel.database.transaction() as c:
        c.execute("DROP TRIGGER prevent_event_immutable_update")
        c.execute(
            "UPDATE events SET payload_hash=? WHERE event_type='action_deliberation_context_bound'",
            ("0" * 64,),
        )
    with pytest.raises(IntegrityError):
        await worker.run_due()
    with pytest.raises(IntegrityError):
        worker.verify_integrity()
    assert len(provider.requests) == 1 and not raw(case, "actions")


@pytest.mark.asyncio
async def test_waiting_for_action_reconciliation_is_not_a_new_metacognitive_failure(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    from noyra.cognition import MetacognitiveControl
    from noyra.cognition.metacognition import _Candidate

    provider = FakeProvider([])
    worker = case.deliberation(provider)
    blocker(case)
    control = MetacognitiveControl(case.kernel.database, case.subject_id, worker.settings)
    control._commit_decision(
        _Candidate("action", "goal", case.goal.goal_id, "fixture", 0.5, 0.5, 0.0), 0.5, 0.0
    )
    result = await worker.run_due()
    control.record_result(result or "missing")
    with case.kernel.database.read_transaction() as c:
        row = c.execute(
            "SELECT outcome,token_cost,rationale_code FROM metacognitive_outcomes "
            "WHERE subject_id=?",
            (case.subject_id,),
        ).fetchone()
    assert dict(row) == {
        "outcome": "stagnant",
        "token_cost": 0,
        "rationale_code": "action_deliberation_waiting_reconciliation",
    }


@pytest.mark.asyncio
async def test_paid_proposal_original_goal_revision_cannot_be_replaced_by_current_state(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    from noyra.mind import GoalStore

    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    worker = case.deliberation(provider, max_calls=1)
    with (
        patch.object(worker, "_execute", side_effect=RuntimeError("before action")),
        pytest.raises(RuntimeError),
    ):
        await worker.run_due()
    calls, attempts = raw(case, "model_calls"), raw(case, "model_attempts")
    goal = GoalStore(case.kernel.database).get(case.goal.goal_id)
    GoalStore(case.kernel.database).revise(
        goal.goal_id,
        status=goal.status,
        priority=goal.priority,
        commitment=goal.commitment,
        progress=0.25,
        emotional_pressure=goal.emotional_pressure,
        reason="New autonomous progress",
        causal_source_ids=(case.evidence.event_id,),
        expected_revision=goal.current_revision,
    )
    assert (
        await worker.run_due() is None
    )  # Paid old result is refused; today's quota prevents a replacement.
    assert len(provider.requests) == 1 and not raw(case, "actions")
    assert raw(case, "model_calls") == calls and raw(case, "model_attempts") == attempts
    assert any(e["event_type"] == "action_deliberation_rejected" for e in raw(case, "events"))


@pytest.mark.asyncio
async def test_bound_paid_proposal_survives_real_encrypted_archive_and_restart(
    case: helpers.ActionDeliberationTestCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64
    from datetime import datetime, timedelta

    from noyra.core import EventStore, SubjectKernel
    from noyra.core.event_archive import EventPayloadArchive
    from noyra.core.types import content_hash

    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    worker = case.deliberation(provider)
    with (
        patch.object(worker, "_execute", side_effect=RuntimeError("before action")),
        pytest.raises(RuntimeError),
    ):
        await worker.run_due()
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )

    class Future(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Future:
            value = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(value.timestamp(), tz)

    archive = EventPayloadArchive(
        case.kernel.database,
        case.kernel.database.path.parent / "subject/cold",
        subject_id=case.subject_id,
    )
    with patch("noyra.core.event_archive.datetime", Future):
        assert archive.archive_cold(case.subject_id, older_than_days=90) > 0
    case.kernel.close()
    case.kernel = SubjectKernel(
        case.kernel.database.path,
        case.subject_id,
        content_hash({"seed": "action-deliberation-test"}),
    )
    case.kernel.boot()
    if case.kernel.lifecycle.current().state == "booting":
        case.kernel.orient()
        case.kernel.activate()
    worker = case.deliberation(provider)
    assert await worker.run_due() == "action_deliberation_observed"
    assert worker.verify_integrity() == 1 and len(provider.requests) == 1
    EventStore(case.kernel.database).verify_chain(case.subject_id)


@pytest.mark.asyncio
async def test_cancellation_keeps_model_unknown_and_releases_worker_lock(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    import asyncio

    entered = asyncio.Event()
    provider = FakeProvider([])

    async def waiting(request: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    worker = case.deliberation(provider)
    with patch.object(provider, "complete", side_effect=waiting):
        task = asyncio.create_task(worker.run_due())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    calls, attempts = raw(case, "model_calls"), raw(case, "model_attempts")
    assert calls[0]["status"] == "unknown" and attempts[0]["status"] == "unknown"
    assert (
        await case.deliberation(provider).run_due() == "action_deliberation_waiting_reconciliation"
    )
    assert raw(case, "model_calls") == calls and raw(case, "model_attempts") == attempts


@pytest.mark.asyncio
async def test_routed_paid_wait_recovers_without_second_physical_call(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    from noyra.model import RoutedModelGateway
    from test_audit_20261022_remediation import resources_for

    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    resources, _ = resources_for(
        case.kernel.database, case.subject_id, case.kernel.database.path.parent, pool="deep"
    )
    worker = case.deliberation(provider, max_calls=1)
    worker.gateway = RoutedModelGateway(
        case.kernel.database, case.subject_id, resources, provider_factory=lambda _: provider
    )
    complete = provider.complete

    async def appear(request: Any) -> Any:
        result = await complete(request)
        blocker(case)
        return result

    with patch.object(provider, "complete", side_effect=appear):
        assert await worker.run_due() == "action_deliberation_waiting_reconciliation"
    calls, attempts = raw(case, "model_calls"), raw(case, "model_attempts")
    assert calls[0]["resource_group_id"]
    ActionLedger(case.kernel.database).cancel(raw(case, "actions")[0]["action_id"], "Unsent")
    # A new direct gateway is sufficient for local result recovery.
    restarted = case.deliberation(FakeProvider([]), max_calls=1)
    assert await restarted.run_due() == "action_deliberation_observed"
    assert len(provider.requests) == 1
    assert raw(case, "model_calls") == calls and raw(case, "model_attempts") == attempts


@pytest.mark.asyncio
async def test_legacy_success_without_bound_context_retains_existing_validated_recovery(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    from noyra.cognition import ActionDeliberationProposal

    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    worker = case.deliberation(provider, max_calls=1)
    context = worker._context(worker._active_goals(), worker._authorized_sources())
    await worker.gateway.complete_structured(
        case.subject_id,
        "action_deliberation:0",
        worker._messages(context),
        ActionDeliberationProposal,
        idempotency_key="action-deliberation:0:legacy",
    )
    assert await worker.run_due() == "action_deliberation_observed"
    assert len(provider.requests) == 1 and worker.verify_integrity() == 1


@pytest.mark.asyncio
async def test_corrupted_successful_response_is_not_replaced_with_another_paid_call(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    from noyra.core.errors import IntegrityError

    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    worker = case.deliberation(provider)
    with (
        patch.object(worker, "_execute", side_effect=RuntimeError("before action")),
        pytest.raises(RuntimeError),
    ):
        await worker.run_due()
    with case.kernel.database.transaction() as connection:
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='model_calls'"
        ).fetchall()
        for trigger in triggers:
            connection.execute('DROP TRIGGER "' + trigger[0] + '"')
        connection.execute("UPDATE model_calls SET response_hash=?", ("0" * 64,))
    with pytest.raises(IntegrityError):
        await worker.run_due()
    assert len(provider.requests) == 1 and not raw(case, "actions")


@pytest.mark.asyncio
async def test_real_registry_validates_receipts_without_constructing_gateway_or_reader(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    from noyra.core.integrity import IntegrityRegistry

    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    worker = case.deliberation(provider)
    assert await worker.run_due() == "action_deliberation_observed"

    def registry() -> Any:
        return IntegrityRegistry().run(
            case.kernel.database,
            case.subject_id,
            case.kernel.database.path.parent,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("cognition.action_deliberation",),
        )

    report = registry()
    assert not report.p0 and not report.p1
    with case.kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_event_immutable_update")
        connection.execute(
            "UPDATE events SET payload_hash=? WHERE event_type='action_deliberation_context_bound'",
            ("0" * 64,),
        )
    report = registry()
    assert report.p0 or report.p1


@pytest.mark.asyncio
async def test_successful_cache_cannot_dispatch_while_subject_is_paused(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    provider = FakeProvider([ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50))])
    worker = case.deliberation(provider)
    with (
        patch.object(worker, "_execute", side_effect=RuntimeError("before action")),
        pytest.raises(RuntimeError),
    ):
        await worker.run_due()
    calls, attempts = raw(case, "model_calls"), raw(case, "model_attempts")
    case.kernel.pause("maintenance")
    assert await worker.run_due() == "action_deliberation_model_failed"
    assert not raw(case, "actions")
    case.kernel.resume()
    assert await worker.run_due() == "action_deliberation_observed"
    assert (
        len(provider.requests) == 1
        and raw(case, "model_calls") == calls
        and raw(case, "model_attempts") == attempts
    )


@pytest.mark.asyncio
async def test_existing_own_prepared_action_prioritizes_its_paid_proposal_over_other_cached_success(
    case: helpers.ActionDeliberationTestCase,
) -> None:
    import json

    from noyra.cognition import ActionDeliberationProposal
    from noyra.core.types import content_hash

    provider = FakeProvider(
        [ProviderResponse(content=case.proposal(), usage=ModelUsage(100, 50)) for _ in range(2)]
    )
    worker = case.deliberation(provider)
    with (
        patch.object(worker, "_execute", side_effect=RuntimeError("before action")),
        pytest.raises(RuntimeError),
    ):
        await worker.run_due()
    own_call = raw(case, "model_calls")[0]["call_id"]
    context = worker._context(worker._active_goals(), worker._authorized_sources())
    # Pre-upgrade stores may already have another successful candidate in this round.
    await worker.gateway.complete_structured(
        case.subject_id,
        "action_deliberation:0",
        worker._messages(context),
        ActionDeliberationProposal,
        idempotency_key="legacy-second-success",
    )
    proposal = json.loads(case.proposal())
    strategy = content_hash(
        {
            "goal_id": case.goal.goal_id,
            "source_id": case.source.source_id,
            "strategy_title": proposal["strategy_title"],
        }
    )[:32]
    own = ActionLedger(case.kernel.database).prepare(
        case.subject_id,
        "web_read",
        "safe_web_reader",
        case.source.url,
        {"source_id": case.source.source_id, "url": case.source.url},
        goal_id=case.goal.goal_id,
        strategy_id=strategy,
        expected_outcome=proposal["expected_observation"],
        idempotency_key="action-deliberation:" + own_call,
    )
    assert await worker.run_due() == "action_deliberation_observed"
    assert len(provider.requests) == 2 and len(raw(case, "actions")) == 1
    assert raw(case, "actions")[0]["action_id"] == own.action_id
    assert raw(case, "action_deliberation_runs")[0]["model_call_id"] == own_call
    assert worker.verify_integrity() == 1
