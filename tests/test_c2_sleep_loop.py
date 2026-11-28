from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta, tzinfo
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from noyra.autonomy import AutonomyLoop, LoopConfig
from noyra.cognition import CognitionSettings, SleepReflectionCognition
from noyra.core import EventStore, SubjectKernel
from noyra.core.admission import OperationInvalidated
from noyra.core.errors import IntegrityError, RuntimeOwnershipError
from noyra.core.private_sources import private_input_scope
from noyra.core.scheduling_clock import CooldownClock
from noyra.core.types import content_hash
from noyra.interaction import InteractionStore
from noyra.interaction.conversations import ConversationStore
from noyra.mind import BeliefStore, MemoryStore
from noyra.model import (
    BudgetLimits,
    CognitiveResourceGroupInput,
    CognitiveResourceStore,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelMessage,
    ModelPricing,
    ModelUsage,
    ProviderResponse,
    RoutedModelGateway,
)
from noyra.model.request_identity import business_request_key
from noyra.model.types import CompletionRequest
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "withdrawal",
    ["before_context", "before_recovery", "during_provider", "before_commit", "derived_only"],
)
async def test_private_reflection_withdrawal_continues_to_wake(
    kernel: SubjectKernel, withdrawal: str
) -> None:
    subject = kernel.subject_id
    db = kernel.database
    events = EventStore(db)
    window = ConversationStore(db).create(subject, "Private test window")
    private_message = InteractionStore(db).receive(
        subject,
        "web",
        "web-user",
        "private-raw-test-marker",
        conversation_id=window["conversation_id"],
    )
    with db.read_transaction() as connection:
        private_event = connection.execute(
            "SELECT event_id FROM events WHERE event_type='interaction_received' "
            "AND json_extract(payload_json,'$.interaction_id')=?",
            (private_message.interaction_id,),
        ).fetchone()[0]
    if withdrawal == "derived_only":
        previous, previous_id = at_phase(kernel, "deep_sleep")
        previous.wake(previous_id, "Synthetic wake")
        previous.complete_wake(previous_id)
    public_event = events.append(subject, "experience", "test", {}).event_id
    memories = MemoryStore(db)
    prior_memory = memories.create(
        subject,
        "reflection",
        "private-memory-test-marker",
        salience=0.8,
        confidence=0.6,
        source_event_ids=(public_event, private_event),
    )
    beliefs = BeliefStore(db)
    prior_belief = beliefs.create(
        subject,
        "private-belief-test-marker",
        confidence=0.5,
        scope="local test",
        supporting_event_ids=(public_event,),
    )
    beliefs.revise(
        prior_belief.belief_id,
        proposition=prior_belief.proposition,
        confidence=0.4,
        status="qualified",
        supporting_event_ids=(public_event,),
        counter_event_ids=(private_event,),
        reason="review",
    )
    engine, sleep_id = at_phase(kernel, "reflective_sleep")
    output = {
        "summary": "new-model-summary-test-marker",
        "memories": [
            {
                "content": "new-model-memory-test-marker",
                "salience": 0.7,
                "confidence": 0.6,
                "source_event_ids": [public_event],
            }
        ],
        "public_diary_candidate": "new-model-diary-test-marker",
    }

    class WithdrawingProvider(FakeProvider):
        async def complete(self, request: CompletionRequest) -> ProviderResponse:
            if withdrawal == "during_provider":
                # Simulate another operation changing the durable authorization.
                with private_input_scope(()):
                    mark_withdrawn()
            return await super().complete(request)

    provider = WithdrawingProvider(
        [ProviderResponse(content=json.dumps(output), usage=ModelUsage(50, 50))]
    )
    ledger = ModelLedger(db)
    gateway = ModelGateway(
        provider,
        ledger,
        model="mock",
        limits=BudgetLimits(20, 500_000, 100_000, 5_000_000),
        pricing=ModelPricing(),
    )
    settings = CognitionSettings(enabled=False, max_sleep_context_chars=60_000)
    cognition = SleepReflectionCognition(db, subject, gateway, settings)

    def mark_withdrawn() -> None:
        # Represent the persisted withdrawal boundary without enabling erasure.
        with db.transaction() as connection:
            connection.execute(
                "UPDATE private_conversations SET status='deleting' WHERE conversation_id=?",
                (window["conversation_id"],),
            )

    if withdrawal == "before_context":
        mark_withdrawn()
    elif withdrawal == "before_recovery":
        proposal = await cognition.propose(engine.get(sleep_id))
        assert proposal.summary == output["summary"]
        mark_withdrawn()
        cognition = SleepReflectionCognition(db, subject, gateway, settings)

    async def reflect(run: SleepRunRecord) -> SleepReflectionPlan:
        plan = await cognition.propose(run)
        if withdrawal in {"before_commit", "derived_only"}:
            assert plan.summary == output["summary"]
            mark_withdrawn()
        return plan

    loop = AutonomyLoop(
        kernel, config=LoopConfig(max_consecutive_failures=1), reflection_hook=reflect
    )
    for expected in (
        "reflection_committed",
        "deep_sleep_entered",
        "wake_started",
        "wake_completed",
    ):
        assert (await loop.tick()).action == expected
    assert kernel.lifecycle.current().state == "active"
    assert loop.sleep.get(sleep_id).status == "complete"
    assert len(provider.requests) == 1
    context = provider.requests[0].messages[-1].content
    raw_fragments = (private_message.interaction_id, private_event, "private-raw-test-marker")
    derived_fragments = (
        prior_memory.memory_id,
        prior_belief.belief_id,
        "private-memory-test-marker",
        "private-belief-test-marker",
    )
    with db.read_transaction() as connection:
        row = connection.execute(
            "SELECT plan_json FROM sleep_reflections WHERE sleep_id=?", (sleep_id,)
        ).fetchone()
        plan = SleepReflectionPlan.model_validate_json(row[0])
        source_rows = connection.execute(
            "SELECT s.interaction_id FROM private_model_sources s JOIN model_calls c "
            "ON c.call_id=s.call_id WHERE c.purpose=?",
            (f"sleep_reflection:{sleep_id}",),
        ).fetchall()
        state = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='sleep_stage_failed'"
        ).fetchone()[0]
        assert not state
        if withdrawal == "during_provider":
            assert (
                connection.execute(
                    "SELECT error_code FROM model_calls WHERE purpose=?",
                    (f"sleep_reflection:{sleep_id}",),
                ).fetchone()[0]
                == "private_content_erased"
            )
    if withdrawal == "before_context":
        assert all(fragment not in context for fragment in (*raw_fragments, *derived_fragments))
        assert not source_rows
        assert plan.summary == output["summary"]
        assert len(memories.search(subject)) == 2
    else:
        assert all(fragment in context for fragment in derived_fragments)
        if withdrawal == "derived_only":
            assert all(fragment not in context for fragment in raw_fragments)
        else:
            assert all(fragment in context for fragment in raw_fragments)
        assert [row[0] for row in source_rows] == [private_message.interaction_id]
        assert plan == SleepReflectionPlan.after_source_withdrawal()
        assert len(memories.search(subject)) == 1
    SleepIntegrity(db).verify(subject)
