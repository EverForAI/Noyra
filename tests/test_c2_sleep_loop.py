from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from noyra.autonomy import AutonomyLoop, LoopConfig
from noyra.cognition import CognitionSettings, SleepReflectionCognition
from noyra.core import SubjectKernel
from noyra.core.admission import OperationInvalidated
from noyra.core.errors import IntegrityError, RuntimeOwnershipError
from noyra.core.scheduling_clock import CooldownClock
from noyra.core.types import content_hash
from noyra.model import (
    BudgetLimits,
    CognitiveResourceGroupInput,
    CognitiveResourceStore,
    FakeProvider,
    ModelLedger,
    ModelMessage,
    ModelUsage,
    ProviderResponse,
    RoutedModelGateway,
)
from noyra.model.request_identity import business_request_key
from noyra.sleep import SleepIntegrity, SleepReflectionPlan, SleepRunRecord
from test_c2_sleep_recovery import at_phase
from test_c2_sleep_recovery import kernel as kernel


@pytest.mark.asyncio
async def test_no_reflection_hook_does_not_stop_an_already_reflected_sleep(
    kernel: SubjectKernel,
) -> None:
    _, sleep_id = at_phase(kernel, "deep_sleep")
    loop = AutonomyLoop(kernel)
    assert (await loop.tick()).action == "wake_started"
    assert (await loop.tick()).action == "wake_completed"
    assert loop.sleep.get(sleep_id).status == "complete"


@pytest.mark.asyncio
async def test_persistent_sleep_failure_pauses_instead_of_cycling_forever(
    kernel: SubjectKernel,
) -> None:
    _, sleep_id = at_phase(kernel, "reflective_sleep")
    hook = AsyncMock(side_effect=RuntimeError("Synthetic persistent failure"))
    loop = AutonomyLoop(kernel, config=LoopConfig(max_consecutive_failures=2), reflection_hook=hook)
    with pytest.raises(RuntimeError):
        await loop.tick()
    with pytest.raises(RuntimeError):
        await loop.tick()
    assert kernel.lifecycle.current().state == "paused"
    assert loop.sleep.get(sleep_id).status == "failed"
    assert hook.await_count == 2


@pytest.mark.asyncio
async def test_missing_hook_is_bounded_without_inventing_a_reflection(
    kernel: SubjectKernel,
) -> None:
    _, identifier = at_phase(kernel, "winding_down")
    loop = AutonomyLoop(kernel, config=LoopConfig(max_consecutive_failures=2))
    assert (await loop.tick()).action == "sleep_state_wait"
    result = await loop.tick()
    assert (result.action, result.lifecycle) == ("sleep_failed_paused", "paused")
    assert loop.sleep.get(identifier).reflection_event_id is None
    assert loop.sleep.get(identifier).status == "failed"


@pytest.mark.asyncio
async def test_sleep_clock_controls_wake_and_utc_budget_day(kernel: SubjectKernel) -> None:
    now = [datetime.fromisoformat("2026-11-06T23:59:00+08:00")]
    hook = AsyncMock(return_value=SleepReflectionPlan(summary="Synthetic bounded reflection"))
    loop = AutonomyLoop(kernel, reflection_hook=hook, clock=lambda: now[0])
    assert loop._wake_after("budget") == "2026-11-07T00:00:00.000+00:00"
    run = loop.sleep.start(
        "subject_choice", "Clock boundary", wake_after="2026-11-06T16:00:00+00:00"
    )
    assert run.started_at == "2026-11-06T15:59:00.000+00:00"
    for expected in ["reflection_started", "reflection_committed", "deep_sleep_entered"]:
        assert (await loop.tick()).action == expected
    assert (await loop.tick()).action == "deep_sleep_wait"
    now[0] -= timedelta(hours=1)
    assert (await loop.tick()).action == "deep_sleep_wait"
    now[0] = datetime(2026, 11, 6, 16, tzinfo=UTC)
    assert (await loop.tick()).action == "wake_started"
    assert (await loop.tick()).action == "wake_completed"
    assert hook.await_count == 1


@pytest.mark.asyncio
async def test_sleep_advances_after_restart_with_ordinary_admission_closed(
    kernel: SubjectKernel,
) -> None:
    _, identifier = at_phase(kernel, "deep_sleep")
    path, subject = kernel.database.path, kernel.subject_id
    kernel.close()
    with SubjectKernel(path, subject, content_hash("c2")) as reopened:
        assert reopened.boot().state == "deep_sleep"
        assert not reopened.admission.accepting
        with pytest.raises(OperationInvalidated):
            reopened.admission.begin("ordinary-work")
        loop = AutonomyLoop(reopened)
        assert (await loop.tick()).action == "wake_started"
        assert (await loop.tick()).action == "wake_completed"
        assert loop.sleep.get(identifier).status == "complete"
        assert reopened.admission.accepting
        lease = reopened.admission.begin("ordinary-work")
        reopened.admission.finish(lease)
        SleepIntegrity(reopened.database).verify(subject)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["prepared", "executing", "unknown"])
async def test_fenced_legacy_reflection_call_allows_sleep_to_finish(
    kernel: SubjectKernel,
    status: str,
) -> None:
    _, sleep_id = at_phase(kernel, "reflective_sleep")
    database, subject = kernel.database, kernel.subject_id
    purpose = f"sleep_reflection:{sleep_id}"
    provider = FakeProvider([])
    ledger = ModelLedger(database)
    call, _ = ledger.prepare_call(
        subject,
        provider.name,
        "legacy-model",
        purpose,
        content_hash("legacy context"),
        business_request_key(database, subject, purpose),
    )
    if status != "prepared":
        attempt = ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(10, 1_000, 1_000, 1_000),
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=1,
        )
        ledger.start_attempt(attempt.attempt_id)
        if status == "unknown":
            ledger.finish_attempt(attempt.attempt_id, "unknown", usage=None, cost_microusd=None)
            ledger.finish_call(call.call_id, "unknown")
    call = ledger.get_call(call.call_id)
    assert call.status == status
    attempts = ledger.attempts(call.call_id)
    resources = CognitiveResourceStore(database, database.path.parent / "model-secrets")
    gateway = RoutedModelGateway(database, subject, resources, provider_factory=lambda _: provider)
    cognition = SleepReflectionCognition(
        database,
        subject,
        gateway,
        CognitionSettings(enabled=False),
    )
    loop = AutonomyLoop(kernel, reflection_hook=cognition.propose)
    for action in ("reflection_committed", "deep_sleep_entered", "wake_started", "wake_completed"):
        assert (await loop.tick()).action == action
    assert loop.sleep.get(sleep_id).status == "complete"
    assert not provider.requests
    assert ledger.get_call(call.call_id) == call
    assert ledger.attempts(call.call_id) == attempts
    with database.read_transaction() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM model_calls WHERE purpose=?", (purpose,)
            ).fetchone()[0]
            == 1
        )
    SleepIntegrity(database).verify(subject)


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers_before_deadline", [False, True])
async def test_deep_pool_outage_does_not_pause_sleep_after_restart(
    kernel: SubjectKernel, monkeypatch: pytest.MonkeyPatch, recovers_before_deadline: bool
) -> None:
    now = datetime.now(UTC)
    started = now
    provider = FakeProvider(
        [ProviderResponse('{"summary":"Model reflection"}', ModelUsage(10, 10))]
    )
    monkeypatch.setattr("noyra.model.resources.utc_now", lambda: now.isoformat())

    # The router schedules the initial wait from its own datetime clock.
    class ModelClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> ModelClock:
            return cls.fromtimestamp(now.timestamp(), tz)

    monkeypatch.setattr("noyra.model.resources.datetime", ModelClock)

    def make_loop(owner: SubjectKernel) -> tuple[AutonomyLoop, RoutedModelGateway]:
        database, subject = owner.database, owner.subject_id
        resources = CognitiveResourceStore(database, database.path.parent / "model-secrets")
        gateway = RoutedModelGateway(
            database, subject, resources, provider_factory=lambda _: provider
        )
        cognition = SleepReflectionCognition(
            database,
            subject,
            gateway,
            CognitionSettings(enabled=False),
            clock=lambda: now.isoformat(),
        )
        monkeypatch.setattr(
            cognition,
            "_model_wait_clock",
            CooldownClock(lambda: (now - started).total_seconds(), capacity=1),
        )
        return AutonomyLoop(
            owner,
            reflection_hook=cognition.propose,
            clock=lambda: now,
            config=LoopConfig(max_consecutive_failures=1),
        ), gateway

    def configure_deep_pool(gateway: RoutedModelGateway) -> None:
        gateway.resources.configure(
            gateway.subject_id,
            CognitiveResourceGroupInput(
                pool="deep",
                label="fixture",
                base_url="https://models.example/v1",
                model="fixture-model",
                api_keys=(SecretStr("fixture-key"),),
                max_attempts=1,
            ),
            actor="operator",
        )

    loop, _ = make_loop(kernel)
    wake_at = started + timedelta(minutes=11)
    run = loop.sleep.start("subject_choice", "Fixture", wake_after=wake_at.isoformat())
    assert (await loop.tick()).action == "reflection_started"
    assert (await loop.tick()).action == "reflection_model_wait"
    assert len(provider.requests) == 0
    path, subject = kernel.database.path, kernel.subject_id
    kernel.close()

    now = started + timedelta(minutes=6)
    with SubjectKernel(path, subject, content_hash("c2")) as reopened:
        assert reopened.boot().state == "reflective_sleep"
        loop, gateway = make_loop(reopened)
        if recovers_before_deadline:
            configure_deep_pool(gateway)
        else:
            assert (await loop.tick()).action == "reflection_model_wait"
            now = started + timedelta(minutes=10)

        assert (await loop.tick()).action == "reflection_committed"
        assert len(provider.requests) == int(recovers_before_deadline)
        with reopened.database.read_transaction() as connection:
            plan = SleepReflectionPlan.model_validate_json(
                connection.execute(
                    "SELECT plan_json FROM sleep_reflections WHERE sleep_id=?", (run.sleep_id,)
                ).fetchone()[0]
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM events WHERE event_type='sleep_stage_failed'"
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM waiting_cognitive_tasks WHERE status='waiting'"
                ).fetchone()[0]
                == 0
            )
        if recovers_before_deadline:
            assert plan.summary == "Model reflection"
        else:
            assert "deep model pool unavailable after waiting" in plan.summary
            assert not any(
                (
                    plan.memories,
                    plan.belief_revisions,
                    plan.goal_revisions,
                    plan.retry_blocks,
                    plan.personality_candidates,
                    plan.public_diary_candidate,
                )
            )

        assert (await loop.tick()).action == "deep_sleep_entered"
        assert (await loop.tick()).action == "deep_sleep_wait"
        now = wake_at
        assert (await loop.tick()).action == "wake_started"
        assert (await loop.tick()).action == "wake_completed"
        assert reopened.admission.accepting
        assert loop.sleep.get(run.sleep_id).status == "complete"
        if not recovers_before_deadline:
            configure_deep_pool(gateway)
            result = await gateway.complete_structured(
                subject,
                "fixture:post-wake",
                (ModelMessage(role="user", content="Fixture"),),
                SleepReflectionPlan,
                idempotency_key="fixture-post-wake",
            )
            assert result.output.summary == "Model reflection"
            assert len(provider.requests) == 1
        SleepIntegrity(reopened.database).verify(subject)


@pytest.mark.asyncio
async def test_stage_failure_count_survives_restart_without_another_model_call(
    kernel: SubjectKernel,
) -> None:
    _, identifier = at_phase(kernel, "reflective_sleep")
    hook = AsyncMock(side_effect=RuntimeError("fault"))
    loop = AutonomyLoop(kernel, config=LoopConfig(max_consecutive_failures=2), reflection_hook=hook)
    with pytest.raises(RuntimeError):
        await loop.tick()
    path, subject = kernel.database.path, kernel.subject_id
    kernel.close()
    with SubjectKernel(path, subject, content_hash("c2")) as reopened:
        reopened.boot()
        restarted = AutonomyLoop(
            reopened, config=LoopConfig(max_consecutive_failures=2), reflection_hook=hook
        )
        with pytest.raises(RuntimeError):
            await restarted.tick()
        assert restarted.sleep.get(identifier).status == "failed"
        assert reopened.lifecycle.current().state == "paused"
        assert hook.await_count == 2


@pytest.mark.asyncio
async def test_abort_failure_after_threshold_is_recovered_before_circuit_cooldown(
    kernel: SubjectKernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, identifier = at_phase(kernel, "reflective_sleep")
    loop = AutonomyLoop(
        kernel,
        config=LoopConfig(max_consecutive_failures=1),
        reflection_hook=AsyncMock(side_effect=RuntimeError("fault")),
    )
    original = loop.sleep.fail
    monkeypatch.setattr(
        loop.sleep, "fail", lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(OSError):
        await loop.tick()
    assert loop.sleep.get(identifier).status == "reflective_sleep"
    monkeypatch.setattr(loop.sleep, "fail", original)
    await loop.tick()
    assert loop.sleep.get(identifier).status == "failed"
    assert kernel.lifecycle.current().state == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["quarantine", "migration", "ownership"])
async def test_sleep_work_does_not_bypass_runtime_fences(
    kernel: SubjectKernel, boundary: str
) -> None:
    _, identifier = at_phase(kernel, "deep_sleep")
    if boundary == "quarantine":
        kernel.admission.quarantine()
    elif boundary == "migration":
        with kernel.admission.migration_control_scope():
            kernel.admission.fence_for_migration(timeout=0)
    else:
        kernel.process_lock.release()
    loop = AutonomyLoop(kernel)
    with pytest.raises((OperationInvalidated, RuntimeOwnershipError)):
        await loop.tick()
    assert loop.sleep.get(identifier).status == "deep_sleep"


@pytest.mark.asyncio
async def test_cancellation_releases_sleep_lease_without_counting_as_stage_failure(
    kernel: SubjectKernel,
) -> None:
    _, identifier = at_phase(kernel, "reflective_sleep")
    entered, release = asyncio.Event(), asyncio.Event()

    async def reflection(_: SleepRunRecord) -> SleepReflectionPlan:
        entered.set()
        await release.wait()
        return SleepReflectionPlan(summary="Synthetic response")

    loop = AutonomyLoop(kernel, reflection_hook=reflection)
    task = asyncio.create_task(loop.tick())
    await entered.wait()
    assert kernel.admission.active_operations == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kernel.admission.active_operations == 0
    assert loop.sleep.get(identifier).status == "reflective_sleep"
    with kernel.database.read_transaction() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='sleep_stage_failed'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_integrity_error_never_becomes_a_benign_failed_sleep(kernel: SubjectKernel) -> None:
    _, identifier = at_phase(kernel, "reflective_sleep")
    loop = AutonomyLoop(
        kernel,
        config=LoopConfig(max_consecutive_failures=1),
        reflection_hook=AsyncMock(side_effect=IntegrityError("Synthetic integrity failure")),
    )
    with pytest.raises(IntegrityError):
        await loop.tick()
    assert loop.sleep.get(identifier).status == "reflective_sleep"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "method"),
    [
        ("winding_down", "begin_reflection"),
        ("reflective_sleep", "commit_reflection"),
        ("deep_sleep", "wake"),
        ("waking", "complete_wake"),
    ],
)
async def test_each_persistent_stage_write_failure_has_a_bounded_exit(
    kernel: SubjectKernel, monkeypatch: pytest.MonkeyPatch, phase: str, method: str
) -> None:
    _, identifier = at_phase(kernel, phase)
    loop = AutonomyLoop(
        kernel,
        config=LoopConfig(max_consecutive_failures=2),
        reflection_hook=AsyncMock(return_value=SleepReflectionPlan(summary="Synthetic plan")),
    )

    def failed_write(*args: object, **kwargs: object) -> None:
        raise OSError("Synthetic stage write failure")

    monkeypatch.setattr(loop.sleep, method, failed_write)
    for _ in range(2):
        with pytest.raises(OSError, match="stage write failure"):
            await loop.tick()
    assert loop.sleep.get(identifier).status == "failed"
    assert kernel.lifecycle.current().state == "paused"
    assert kernel.admission.active_operations == 0
