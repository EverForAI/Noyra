from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from noyra.autonomy import AutonomyLoop, LoopConfig
from noyra.core import SubjectKernel
from noyra.core.types import content_hash
from noyra.sleep import FatigueInputs, FatigueTracker


def _kernel(tmp_path: Path, subject_id: str) -> SubjectKernel:
    kernel = SubjectKernel(
        tmp_path / f"{subject_id}.sqlite3",
        subject_id,
        content_hash({"subject": subject_id}),
    )
    kernel.boot()
    kernel.orient()
    kernel.activate()
    return kernel


def _prepare_action(kernel: SubjectKernel) -> str:
    action = kernel.action_ledger.prepare(
        kernel.subject_id,
        "observe",
        "test-tool",
        "target",
        {"test": True},
        idempotency_key="m42-sleep-deadlock",
    )
    return action.action_id


def _exhaust_fatigue(kernel: SubjectKernel) -> None:
    FatigueTracker(kernel.database).assess(
        kernel.subject_id,
        FatigueInputs(
            resource_pressure=1.0,
            cognitive_load=1.0,
            frustration=1.0,
            goal_conflict=1.0,
            staleness=1.0,
        ),
        reason="M42 prepared-action sleep regression",
    )


def test_prepared_action_blocks_sleep_honestly_and_can_settle(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, "Noyra-m42-sleep-settle")
    action_id = _prepare_action(kernel)
    _exhaust_fatigue(kernel)
    hook_calls = 0

    async def settle_action() -> str:
        nonlocal hook_calls
        hook_calls += 1
        if hook_calls == 1:
            kernel.action_ledger.cancel(action_id, "deferred before fatigue sleep")
        return "action_settled"

    loop = AutonomyLoop(
        kernel,
        config=LoopConfig(active_interval_seconds=1, sleep_interval_seconds=2),
        active_hook=settle_action,
    )
    try:
        first = asyncio.run(loop.tick())
        assert first.action == "sleep_requested"
        assert first.lifecycle == "winding_down"
        assert hook_calls == 1
        with kernel.database.connection() as connection:
            action = connection.execute(
                "SELECT status FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        assert action is not None
        assert action["status"] == "cancelled"

        second = asyncio.run(loop.tick())
        assert second.action == "sleep_state_wait"
        assert second.lifecycle == "winding_down"
    finally:
        kernel.close()


def test_sleep_conflict_deadline_pauses_instead_of_reporting_sleep(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, "Noyra-m42-sleep-deadline")
    _prepare_action(kernel)
    _exhaust_fatigue(kernel)
    clock_value = [datetime(2026, 8, 15, tzinfo=UTC)]
    loop = AutonomyLoop(
        kernel,
        config=LoopConfig(
            active_interval_seconds=1,
            sleep_interval_seconds=2,
            sleep_conflict_deadline_seconds=5,
        ),
        clock=lambda: clock_value[0],
    )
    try:
        first = asyncio.run(loop.tick())
        assert first.action == "sleep_blocked"
        assert first.lifecycle == "active"

        clock_value[0] += timedelta(seconds=5)
        second = asyncio.run(loop.tick())
        assert second.action == "sleep_blocked_paused"
        assert second.lifecycle == "paused"
        assert kernel.lifecycle.current().state == "paused"
    finally:
        kernel.close()


def test_restart_does_not_lie_about_an_unsettled_prepared_action(tmp_path: Path) -> None:
    database_path = tmp_path / "Noyra-m42-sleep-restart.sqlite3"
    subject_id = "Noyra-m42-sleep-restart"
    kernel = SubjectKernel(database_path, subject_id, content_hash({"subject": subject_id}))
    kernel.boot()
    kernel.orient()
    kernel.activate()
    _prepare_action(kernel)
    _exhaust_fatigue(kernel)
    clock_value = datetime(2026, 8, 15, tzinfo=UTC)
    first = asyncio.run(
        AutonomyLoop(
            kernel,
            config=LoopConfig(sleep_conflict_deadline_seconds=5),
            clock=lambda: clock_value,
        ).tick()
    )
    assert first.action == "sleep_blocked"
    kernel.close()

    restarted_early = SubjectKernel(
        database_path, subject_id, content_hash({"subject": subject_id})
    )
    try:
        restarted_early.boot()
        restarted_early.orient()
        restarted_early.activate()
        before_deadline = clock_value + timedelta(seconds=2)
        second = asyncio.run(
            AutonomyLoop(
                restarted_early,
                config=LoopConfig(sleep_conflict_deadline_seconds=5),
                clock=lambda: before_deadline,
            ).tick()
        )
        assert second.action == "sleep_blocked"
        assert second.lifecycle == "active"
    finally:
        restarted_early.close()

    restarted_at_deadline = SubjectKernel(
        database_path, subject_id, content_hash({"subject": subject_id})
    )
    try:
        restarted_at_deadline.boot()
        restarted_at_deadline.orient()
        restarted_at_deadline.activate()
        at_deadline = clock_value + timedelta(seconds=5)
        third = asyncio.run(
            AutonomyLoop(
                restarted_at_deadline,
                config=LoopConfig(sleep_conflict_deadline_seconds=5),
                clock=lambda: at_deadline,
            ).tick()
        )
        assert third.action == "sleep_blocked_paused"
        assert third.lifecycle == "paused"
        with restarted_at_deadline.database.connection() as connection:
            blocked_events = connection.execute(
                "SELECT COUNT(*) FROM events WHERE subject_id = ? "
                "AND event_type = 'autonomy_sleep_blocked'",
                (subject_id,),
            ).fetchone()[0]
        assert blocked_events == 1
    finally:
        restarted_at_deadline.close()
