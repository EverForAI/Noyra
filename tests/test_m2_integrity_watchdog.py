from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from noyra.autonomy.loop import AutonomyLoop
from noyra.autonomy.types import LoopConfig, TickResult
from noyra.core.runtime import SubjectKernel
from noyra.core.types import content_hash


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    value = SubjectKernel(tmp_path / "watchdog.sqlite3", "one", content_hash("watchdog"))
    value.boot()
    value.orient()
    value.activate()
    try:
        yield value
    finally:
        value.close()


@pytest.mark.asyncio
async def test_watchdog_fault_has_its_own_backoff_and_never_calls_cognition(
    kernel: SubjectKernel,
) -> None:
    pre = AsyncMock(side_effect=RuntimeError("private provider or filesystem detail"))
    active = AsyncMock(return_value="should not run")
    loop = AutonomyLoop(kernel, pre_tick_hook=pre, active_hook=active)
    assert (await loop.tick()).action == "integrity_check_deferred"
    assert (await loop.tick()).action == "integrity_check_backoff"
    assert pre.await_count == 1
    active.assert_not_called()
    health = loop.health()
    assert health["consecutive_failures"] == 0
    watchdog = health["integrity_watchdog"]
    assert isinstance(watchdog, dict) and watchdog["consecutive_failures"] == 1

    from noyra.core.runtime_observation import RuntimeObserver

    observed = RuntimeObserver(
        kernel.database.path, kernel.database.path.parent, kernel.subject_id
    ).snapshot(cognition_enabled=True, interval_seconds=30)
    assert observed["autonomy_observation"] == "ok"
    assert observed["integrity_watchdog"]["status"] == "backoff"
    assert observed["integrity_watchdog"]["persisted"]


@pytest.mark.asyncio
async def test_watchdog_escalates_once_survives_restart_and_recovers_without_fake_tick_success(
    kernel: SubjectKernel,
) -> None:
    from datetime import timedelta

    from noyra.autonomy.workflow import DurableWorkflowStore

    wall = [datetime(2026, 11, 4, tzinfo=UTC)]
    mono = [0.0]
    pre = AsyncMock(
        side_effect=[
            RuntimeError("secret"),
            ValueError("secret"),
            OSError("secret"),
            TickResult("paused", "integrity_quarantine", None, 60),
        ]
    )
    active = AsyncMock(return_value="never")
    config = LoopConfig(
        error_backoff_seconds=10, max_error_backoff_seconds=20, max_consecutive_failures=2
    )

    def make() -> AutonomyLoop:
        loop = AutonomyLoop(
            kernel, pre_tick_hook=pre, active_hook=active, config=config, clock=lambda: wall[0]
        )
        loop.integrity_watchdog.monotonic = lambda: mono[0]
        return loop

    loop = make()
    for seconds in [0, 10, 20]:
        mono[0] += seconds
        wall[0] += timedelta(seconds=seconds)
        assert (await loop.tick()).action == "integrity_check_deferred"
    health = loop.integrity_watchdog.health()
    assert health["consecutive_failures"] == 3 and health["escalated"]
    restarted = make()
    assert (await restarted.tick()).action == "integrity_check_backoff"
    assert pre.await_count == 3
    # Forward wall-clock jumps cannot shorten an anchored in-process cooldown.
    wall[0] += timedelta(days=1)
    assert (await restarted.tick()).action == "integrity_check_backoff"
    mono[0] += 20
    assert (await restarted.tick()).action == "integrity_quarantine"
    active.assert_not_called()
    saved = restarted.integrity_watchdog.health()
    assert saved["consecutive_failures"] == 0 and saved["total_failures"] == 3
    assert restarted.health()["last_successful_tick_at"] is None
    with kernel.database.connection() as c:
        events = c.execute(
            "SELECT event_type,payload_json FROM events "
            "WHERE event_type LIKE 'integrity_watchdog_%'"
        ).fetchall()
    assert [r[0] for r in events] == [
        "integrity_watchdog_error",
        "integrity_watchdog_escalated",
        "integrity_watchdog_recovered",
    ]
    assert "secret" not in str([r[1] for r in events])
    DurableWorkflowStore(kernel.database).verify_integrity(kernel.subject_id)


@pytest.mark.asyncio
async def test_watchdog_storage_failure_uses_memory_backoff_and_remains_fail_closed(
    kernel: SubjectKernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    pre = AsyncMock(side_effect=RuntimeError("failure"))
    loop = AutonomyLoop(kernel, pre_tick_hook=pre, active_hook=AsyncMock())
    monkeypatch.setattr(
        loop.integrity_watchdog, "_persist", lambda *args: (_ for _ in ()).throw(OSError("disk"))
    )
    assert (await loop.tick()).action == "integrity_check_deferred"
    assert loop.integrity_watchdog.health()["persisted"] is False
    assert loop.integrity_watchdog.health()["consecutive_failures"] == 1
    assert (await loop.tick()).action == "integrity_check_backoff"
    assert pre.await_count == 1


@pytest.mark.asyncio
async def test_watchdog_cancel_is_not_an_error_and_due_hint_cannot_bypass_backoff(
    kernel: SubjectKernel,
) -> None:
    import asyncio

    cancelled = AutonomyLoop(kernel, pre_tick_hook=AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await cancelled.tick()
    assert cancelled.integrity_watchdog.health()["total_failures"] == 0
    stop = asyncio.Event()

    async def error() -> None:
        stop.set()
        raise OSError("fault")

    # A synchronous due hint must not reduce retry delay to a busy loop.
    from unittest.mock import Mock

    hint = Mock(return_value=0.1)
    loop = AutonomyLoop(kernel, pre_tick_hook=error, next_wakeup_hook=hint)
    await loop.run_forever(stop)
    hint.assert_not_called()


@pytest.mark.asyncio
async def test_bad_watchdog_evidence_never_runs_the_checker_or_cognition(
    kernel: SubjectKernel,
) -> None:
    from noyra.core.integrity_watchdog import workflow_key

    pre = AsyncMock(side_effect=RuntimeError("first failure"))
    first = AutonomyLoop(kernel, pre_tick_hook=pre)
    await first.tick()
    with kernel.database.transaction() as c:
        triggers = c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='workflow_checkpoints'"
        ).fetchall()
        for row in triggers:
            c.execute('DROP TRIGGER "' + row[0] + '"')
        c.execute(
            "UPDATE workflow_checkpoints SET state_hash=? WHERE workflow_id=?",
            ("0" * 64, workflow_key(kernel.subject_id)),
        )
    active = AsyncMock()
    restarted = AutonomyLoop(kernel, pre_tick_hook=pre, active_hook=active)
    assert (await restarted.tick()).action == "integrity_check_backoff"
    assert restarted.integrity_watchdog.health()["status"] == "evidence_unavailable"
    assert pre.await_count == 1
    active.assert_not_called()


@pytest.mark.asyncio
async def test_service_watchdog_projection_uses_actual_memory_without_http_side_disk_io(
    kernel: SubjectKernel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = AutonomyLoop(kernel, pre_tick_hook=AsyncMock(side_effect=RuntimeError("fault")))
    assert loop.integrity_watchdog.snapshot() == {"status": "not_checked"}
    await loop.tick()
    monkeypatch.setattr(
        loop.integrity_watchdog,
        "_load",
        lambda: (_ for _ in ()).throw(AssertionError("HTTP must not read disk")),
    )
    assert loop.integrity_watchdog.snapshot()["consecutive_failures"] == 1
    assert loop.integrity_watchdog.snapshot()["persisted"]
