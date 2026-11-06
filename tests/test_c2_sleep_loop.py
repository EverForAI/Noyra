from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from noyra.autonomy import AutonomyLoop, LoopConfig
from noyra.core import SubjectKernel
from noyra.core.admission import OperationInvalidated
from noyra.core.errors import IntegrityError, RuntimeOwnershipError
from noyra.core.types import content_hash
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
