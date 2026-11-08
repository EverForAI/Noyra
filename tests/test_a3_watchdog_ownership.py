from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from noyra.autonomy.loop import AutonomyLoop
from noyra.autonomy.types import LoopConfig
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.events import EventStore
from noyra.core.runtime import SubjectKernel, _migration_epoch_guard
from noyra.core.types import content_hash
from test_a3_reset_ownership import acquire_epoch
from test_m2_integrity_watchdog import kernel as kernel


def snapshot(kernel: SubjectKernel) -> dict[str, list[dict[str, Any]]]:
    with kernel.database.read_transaction() as connection:
        return {
            table: [
                dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in ("runtime_state", "events", "workflow_checkpoints", "autonomy_loop_state")
        }


@pytest.mark.asyncio
async def test_checker_ownership_failure_keeps_only_unpersisted_process_diagnostics(
    kernel: SubjectKernel,
) -> None:
    async def checker() -> None:
        kernel._require_ownership()

    active = AsyncMock()
    loop = AutonomyLoop(kernel, pre_tick_hook=checker, active_hook=active)
    kernel.process_lock.release()
    before = snapshot(kernel)
    assert (await loop.tick()).action == "integrity_check_deferred"
    assert snapshot(kernel) == before
    assert not loop.integrity_watchdog.snapshot()["persisted"]
    active.assert_not_called()


@pytest.mark.asyncio
async def test_watchdog_recovery_cannot_write_after_process_ownership_is_released(
    kernel: SubjectKernel,
) -> None:
    loop = AutonomyLoop(
        kernel, pre_tick_hook=AsyncMock(side_effect=RuntimeError("synthetic fault"))
    )
    await loop.tick()
    kernel.process_lock.release()
    before = snapshot(kernel)
    with pytest.raises(RuntimeOwnershipError):
        loop.integrity_watchdog.succeeded()
    assert snapshot(kernel) == before


@pytest.mark.asyncio
async def test_watchdog_diagnostics_cannot_mutate_a_migration_fenced_source(
    kernel: SubjectKernel,
) -> None:
    loop = AutonomyLoop(
        kernel, pre_tick_hook=AsyncMock(side_effect=RuntimeError("synthetic fault"))
    )
    acquire_epoch(kernel)
    before = snapshot(kernel)
    assert (await loop.tick()).action == "integrity_check_deferred"
    assert snapshot(kernel) == before
    assert not loop.integrity_watchdog.snapshot()["persisted"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restriction", ["completed_epoch", "memory_fence", "draining", "file_fence"]
)
async def test_every_control_write_refuses_non_owned_or_closing_runtime(
    kernel: SubjectKernel, tmp_path: Path, restriction: str
) -> None:
    loop = AutonomyLoop(kernel, pre_tick_hook=AsyncMock(side_effect=RuntimeError("checker fault")))
    if restriction == "completed_epoch":
        acquire_epoch(kernel).complete("test")
    elif restriction == "memory_fence":
        with kernel.admission.migration_control_scope():
            kernel.admission.fence_for_migration(timeout=0)
    elif restriction == "draining":
        kernel.admission.begin_drain()
    else:
        fence = tmp_path / "migration" / "fences"
        fence.mkdir(parents=True)
        (fence / "task.json").write_text(
            json.dumps({"subject_id": kernel.subject_id, "status": "active"}), encoding="utf8"
        )
        kernel.lifecycle.mutation_guard = lambda c: _migration_epoch_guard(
            c, kernel.subject_id, fence.parent
        )
    before = snapshot(kernel)
    assert (await loop.tick()).action == "integrity_check_deferred"
    assert snapshot(kernel) == before
    assert loop.integrity_watchdog.snapshot()["persisted"] is False
    assert kernel.admission.active_operations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["paused", "quarantined"])
async def test_current_owner_can_record_diagnostics_without_reopening_cognition(
    kernel: SubjectKernel, restriction: str
) -> None:
    if restriction == "paused":
        kernel.pause("Maintenance", actor="operator")
    else:
        kernel.admission.quarantine()
    loop = AutonomyLoop(
        kernel,
        pre_tick_hook=AsyncMock(side_effect=RuntimeError("checker fault")),
        active_hook=AsyncMock(),
    )
    before = kernel.lifecycle.current()
    assert (await loop.tick()).action == "integrity_check_deferred"
    assert loop.integrity_watchdog.snapshot()["persisted"]
    assert kernel.lifecycle.current() == before
    assert not kernel.admission.accepting
    assert kernel.admission.active_operations == 0
    assert isinstance(loop.active_hook, AsyncMock)
    loop.active_hook.assert_not_called()
    loop.integrity_watchdog.succeeded()
    assert not kernel.admission.accepting
    assert loop.integrity_watchdog.snapshot()["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_watchdog_rechecks_process_lock_after_admission_before_any_write(
    kernel: SubjectKernel,
) -> None:
    loop = AutonomyLoop(kernel, pre_tick_hook=AsyncMock(side_effect=RuntimeError("checker fault")))
    ownership_scope = kernel.process_lock.ownership_scope

    @contextmanager
    def released_before_scope() -> Iterator[None]:
        kernel.process_lock.release()
        with ownership_scope():
            yield

    before = snapshot(kernel)
    with patch.object(kernel.process_lock, "ownership_scope", released_before_scope):
        assert (await loop.tick()).action == "integrity_check_deferred"
    assert snapshot(kernel) == before
    assert not loop.integrity_watchdog.snapshot()["persisted"]
    assert kernel.admission.active_operations == 0


@pytest.mark.asyncio
async def test_watchdog_calls_migration_guard_inside_its_checkpoint_transaction(
    kernel: SubjectKernel,
) -> None:
    checked = []

    def reject(connection: Any) -> None:
        assert connection.in_transaction
        checked.append(True)
        raise RuntimeOwnershipError("synthetic fence in writer transaction")

    kernel.lifecycle.mutation_guard = reject
    loop = AutonomyLoop(kernel, pre_tick_hook=AsyncMock(side_effect=RuntimeError("checker fault")))
    before = snapshot(kernel)
    assert (await loop.tick()).action == "integrity_check_deferred"
    assert checked == [True]
    assert snapshot(kernel) == before
    assert not loop.integrity_watchdog.snapshot()["persisted"]


@pytest.mark.asyncio
async def test_checkpoint_and_error_event_rollback_together_on_persistence_failure(
    kernel: SubjectKernel,
) -> None:
    loop = AutonomyLoop(kernel, pre_tick_hook=AsyncMock(side_effect=RuntimeError("checker fault")))
    before = snapshot(kernel)
    with patch.object(
        EventStore, "_append_connection", side_effect=OSError("synthetic event write failure")
    ):
        assert (await loop.tick()).action == "integrity_check_deferred"
    assert snapshot(kernel) == before
    assert kernel.admission.active_operations == 0
    assert not loop.integrity_watchdog.snapshot()["persisted"]
    loop.integrity_watchdog.succeeded()
    assert loop.integrity_watchdog.snapshot()["persisted"]
    assert loop.integrity_watchdog.snapshot()["consecutive_failures"] == 0
    assert loop.integrity_watchdog.snapshot()["total_failures"] == 1


@pytest.mark.asyncio
async def test_ownership_pinning_delays_release_until_diagnostic_commit(
    kernel: SubjectKernel,
) -> None:
    requested, released = threading.Event(), threading.Event()
    guard = kernel.lifecycle.mutation_guard
    assert guard is not None
    worker = None

    def release() -> None:
        requested.set()
        kernel.process_lock.release()
        released.set()

    def check(connection: Any) -> None:
        nonlocal worker
        guard(connection)
        worker = threading.Thread(target=release)
        worker.start()
        assert requested.wait(5)
        assert not released.wait(0.05)

    kernel.lifecycle.mutation_guard = check
    loop = AutonomyLoop(kernel, pre_tick_hook=AsyncMock(side_effect=RuntimeError("checker fault")))
    assert (await loop.tick()).action == "integrity_check_deferred"
    assert worker is not None
    worker.join(timeout=5)
    assert released.is_set() and not kernel.process_lock.held
    before = snapshot(kernel)
    loop.integrity_watchdog.failed("NextFailure")
    assert snapshot(kernel) == before
    assert not loop.integrity_watchdog.snapshot()["persisted"]
    assert kernel.admission.active_operations == 0


@pytest.mark.asyncio
async def test_stale_watchdog_cannot_overwrite_legitimate_replacement_owner(
    kernel: SubjectKernel,
) -> None:
    loop = AutonomyLoop(kernel, pre_tick_hook=AsyncMock(side_effect=RuntimeError("old checker")))
    await loop.tick()
    kernel.process_lock.release()
    replacement = SubjectKernel(kernel.database.path, kernel.subject_id, content_hash("watchdog"))
    replacement.boot()
    replacement.orient()
    replacement.activate()
    try:
        new_loop = AutonomyLoop(
            replacement, pre_tick_hook=AsyncMock(side_effect=ValueError("new checker"))
        )
        new_loop.integrity_watchdog.failed("NewCheckerError")
        before = snapshot(replacement)
        loop.integrity_watchdog.failed("OldCheckerError")
        assert snapshot(replacement) == before
        assert not loop.integrity_watchdog.snapshot()["persisted"]
        assert new_loop.integrity_watchdog.snapshot()["persisted"]
        assert new_loop.integrity_watchdog.snapshot()["last_error_type"] == "NewCheckerError"
    finally:
        replacement.close()


@pytest.mark.asyncio
async def test_unowned_error_backoff_and_escalation_remain_bounded_and_unpersisted(
    kernel: SubjectKernel,
) -> None:
    loop = AutonomyLoop(
        kernel,
        config=LoopConfig(max_consecutive_failures=2),
        pre_tick_hook=AsyncMock(side_effect=RuntimeError("checker")),
    )
    kernel.process_lock.release()
    before = snapshot(kernel)
    for _ in range(3):
        loop.integrity_watchdog.failed("RuntimeOwnershipError")
    health = loop.integrity_watchdog.snapshot()
    assert health["escalated"] and health["total_failures"] == 3
    assert not health["persisted"]
    assert 0 < health["retry_seconds"] <= loop.config.max_error_backoff_seconds
    assert (await loop.tick()).action == "integrity_check_backoff"
    assert isinstance(loop.pre_tick_hook, AsyncMock)
    loop.pre_tick_hook.assert_not_called()
    assert snapshot(kernel) == before


@pytest.mark.asyncio
async def test_same_target_migration_owner_can_persist_checker_health(
    kernel: SubjectKernel,
) -> None:
    acquire_epoch(kernel)
    kernel._migration_target_id = "a3-target"
    loop = AutonomyLoop(kernel, pre_tick_hook=AsyncMock(side_effect=RuntimeError("checker fault")))
    assert (await loop.tick()).action == "integrity_check_deferred"
    assert loop.integrity_watchdog.snapshot()["persisted"]


@pytest.mark.asyncio
async def test_shutdown_waits_for_short_diagnostic_commit_without_deadlock(
    kernel: SubjectKernel,
) -> None:
    requested, closed = threading.Event(), threading.Event()
    guard = kernel.lifecycle.mutation_guard
    assert guard is not None
    worker = None

    def close() -> None:
        requested.set()
        kernel.close()
        closed.set()

    def check(connection: Any) -> None:
        nonlocal worker
        guard(connection)
        worker = threading.Thread(target=close)
        worker.start()
        assert requested.wait(5)
        assert not closed.wait(0.05)

    kernel.lifecycle.mutation_guard = check
    loop = AutonomyLoop(kernel, pre_tick_hook=AsyncMock(side_effect=RuntimeError("checker fault")))
    assert (await loop.tick()).action == "integrity_check_deferred"
    assert worker is not None
    worker.join(timeout=5)
    assert closed.is_set()
    before = snapshot(kernel)
    loop.integrity_watchdog.failed("AfterShutdown")
    assert snapshot(kernel) == before
    assert kernel.admission.active_operations == 0


@pytest.mark.asyncio
async def test_real_owner_reacquisition_can_settle_only_its_diagnostic_health(
    kernel: SubjectKernel,
) -> None:
    loop = AutonomyLoop(kernel, pre_tick_hook=AsyncMock(side_effect=RuntimeError("checker fault")))
    await loop.tick()
    kernel.process_lock.release()
    before = snapshot(kernel)
    loop.integrity_watchdog.failed("RuntimeOwnershipError")
    assert snapshot(kernel) == before
    assert not loop.integrity_watchdog.snapshot()["persisted"]
    assert kernel.process_lock.acquire()
    loop.integrity_watchdog.succeeded()
    health = loop.integrity_watchdog.snapshot()
    assert health["persisted"] and health["consecutive_failures"] == 0
    assert health["total_failures"] == 2
    assert kernel.admission.active_operations == 0
    assert kernel.lifecycle.current().state == "active"
    with kernel.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='integrity_watchdog_recovered'"
            ).fetchone()[0]
            == 1
        )
