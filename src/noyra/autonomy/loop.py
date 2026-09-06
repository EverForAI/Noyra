from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from noyra.core.events import EventStore
from noyra.core.runtime import SubjectKernel
from noyra.core.types import utc_now
from noyra.sleep import FatigueTracker, SleepEngine, SleepReflectionPlan, SleepRunRecord
from noyra.sleep.errors import SleepStateConflictError

from .types import LoopConfig, TickResult

ActiveHook = Callable[[], Awaitable[str | None]]
ReflectionHook = Callable[[SleepRunRecord], Awaitable[SleepReflectionPlan]]
PreTickHook = Callable[[], Awaitable[TickResult | None]]
NextWakeupHook = Callable[[], float | None]
WallClock = Callable[[], datetime]


def _utc_datetime() -> datetime:
    return datetime.now(UTC)


class AutonomyLoop:
    """A bounded heartbeat that never invents work or treats messages as commands."""

    def __init__(
        self,
        kernel: SubjectKernel,
        *,
        config: LoopConfig | None = None,
        active_hook: ActiveHook | None = None,
        reflection_hook: ReflectionHook | None = None,
        pre_tick_hook: PreTickHook | None = None,
        next_wakeup_hook: NextWakeupHook | None = None,
        clock: WallClock = _utc_datetime,
    ):
        self.kernel = kernel
        self.config = config or LoopConfig()
        self.active_hook = active_hook
        self.reflection_hook = reflection_hook
        self.pre_tick_hook = pre_tick_hook
        self.next_wakeup_hook = next_wakeup_hook
        self.events = EventStore(kernel.database)
        self.fatigue = FatigueTracker(kernel.database)
        self.sleep = SleepEngine(kernel.database, kernel.subject_id)
        self._tick_lock = asyncio.Lock()
        self._clock = clock
        self._sleep_conflict_started_at: datetime | None = None

    async def tick(self) -> TickResult:
        async with self._tick_lock:
            if self.pre_tick_hook is not None:
                try:
                    guarded = await self.pre_tick_hook()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    event = self.events.append(
                        self.kernel.subject_id,
                        "integrity_watchdog_error",
                        "runtime",
                        {"error_type": type(error).__name__},
                        privacy_level="private",
                    )
                    return TickResult(
                        self.kernel.lifecycle.current().state,
                        "integrity_check_deferred",
                        event.event_id,
                        self.config.error_backoff_seconds,
                    )
                if guarded is not None:
                    return guarded
            circuit_wait = self._begin_tick()
            if circuit_wait is not None:
                return circuit_wait
            try:
                result = await self._tick_once()
            except Exception as error:
                self._record_failure(type(error).__name__)
                raise
            self._record_success()
            return result

    async def _tick_once(self) -> TickResult:
        lifecycle = self.kernel.lifecycle.current().state
        if lifecycle in {"deep_sleep", "winding_down", "reflective_sleep", "waking"}:
            return await self._advance_sleep(lifecycle)
        if lifecycle != "active":
            return TickResult(
                lifecycle,
                "inactive_wait",
                None,
                self.config.sleep_interval_seconds,
            )
        self.fatigue.ensure(self.kernel.subject_id)
        should_sleep, reasons = self.fatigue.should_sleep(self.kernel.subject_id)
        if should_sleep:
            if "hard_resource_budget_exhausted" in reasons:
                trigger = "budget"
            elif "fatigue_threshold" in reasons:
                trigger = "fatigue"
            else:
                trigger = "subject_choice"
            try:
                self.sleep.start(
                    trigger,
                    ",".join(reasons),
                    wake_after=self._wake_after(trigger),
                )
            except SleepStateConflictError as error:
                return await self._handle_sleep_conflict(
                    error,
                    trigger=trigger,
                    reason=",".join(reasons),
                    wake_after=self._wake_after(trigger),
                )
            self._clear_sleep_conflict()
            return TickResult(
                self.kernel.lifecycle.current().state,
                "sleep_requested",
                None,
                self.config.sleep_interval_seconds,
            )
        self._clear_sleep_conflict()
        hook_result = await self.active_hook() if self.active_hook is not None else None
        event = self.events.append(
            self.kernel.subject_id,
            "autonomy_tick",
            "runtime",
            {"hook_result": hook_result, "occurred_at": utc_now()},
            privacy_level="private",
        )
        return TickResult(
            self.kernel.lifecycle.current().state,
            hook_result or "heartbeat",
            event.event_id,
            self.config.active_interval_seconds,
        )

    async def _handle_sleep_conflict(
        self,
        error: SleepStateConflictError,
        *,
        trigger: str,
        reason: str,
        wake_after: str | None,
    ) -> TickResult:
        """Keep the runtime active while recoverable work gets a chance to settle."""
        now = self._clock()
        if self._sleep_conflict_started_at is None:
            persisted_started_at = self._load_sleep_conflict_started_at()
            self._sleep_conflict_started_at = persisted_started_at or now
            if persisted_started_at is None:
                event = self.events.append(
                    self.kernel.subject_id,
                    "autonomy_sleep_blocked",
                    "runtime",
                    {
                        "error": str(error),
                        "deadline_seconds": self.config.sleep_conflict_deadline_seconds,
                    },
                    privacy_level="private",
                    occurred_at=now.isoformat(timespec="milliseconds"),
                )
            else:
                event = None
        else:
            event = None

        if self.active_hook is not None:
            await self.active_hook()
        try:
            self.sleep.start(trigger, reason, wake_after=wake_after)
        except SleepStateConflictError:
            pass
        else:
            self._clear_sleep_conflict()
            return TickResult(
                self.kernel.lifecycle.current().state,
                "sleep_requested",
                event.event_id if event is not None else None,
                self.config.sleep_interval_seconds,
            )
        elapsed = max(0.0, (now - self._sleep_conflict_started_at).total_seconds())
        if elapsed >= self.config.sleep_conflict_deadline_seconds:
            paused = self.kernel.pause(
                "sleep blocked by recoverable action beyond the configured deadline"
            )
            return TickResult(
                paused.state,
                "sleep_blocked_paused",
                event.event_id if event is not None else None,
                self.config.sleep_interval_seconds,
            )
        return TickResult(
            self.kernel.lifecycle.current().state,
            "sleep_blocked",
            event.event_id if event is not None else None,
            self.config.active_interval_seconds,
        )

    def _load_sleep_conflict_started_at(self) -> datetime | None:
        with self.kernel.database.connection() as connection:
            row = connection.execute(
                "SELECT event_type, occurred_at FROM events "
                "WHERE subject_id = ? AND event_type IN "
                "('autonomy_sleep_blocked', 'autonomy_sleep_conflict_cleared') "
                "ORDER BY rowid DESC LIMIT 1",
                (self.kernel.subject_id,),
            ).fetchone()
        if row is None or row["event_type"] != "autonomy_sleep_blocked":
            return None
        try:
            return datetime.fromisoformat(str(row["occurred_at"])).astimezone(UTC)
        except ValueError:
            return None

    def _clear_sleep_conflict(self) -> None:
        had_persisted_conflict = self._load_sleep_conflict_started_at() is not None
        if self._sleep_conflict_started_at is not None or had_persisted_conflict:
            now = self._clock()
            self.events.append(
                self.kernel.subject_id,
                "autonomy_sleep_conflict_cleared",
                "runtime",
                {"reason": "sleep conflict no longer blocks the runtime"},
                privacy_level="private",
                occurred_at=now.isoformat(timespec="milliseconds"),
            )
        self._sleep_conflict_started_at = None

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                result = await self.tick()
                interval = result.next_interval_seconds
            except Exception as error:
                self.events.append(
                    self.kernel.subject_id,
                    "autonomy_error",
                    "runtime",
                    {"error_type": type(error).__name__},
                    privacy_level="private",
                )
                failures = int(str(self.health()["consecutive_failures"]))
                interval = min(
                    self.config.max_error_backoff_seconds,
                    self.config.error_backoff_seconds * math.pow(2, max(0, failures - 1)),
                )
            if self.next_wakeup_hook is not None:
                next_wakeup = self.next_wakeup_hook()
                if next_wakeup is not None:
                    interval = min(interval, max(0.1, next_wakeup))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    def health(self) -> dict[str, object]:
        self._ensure_loop_state()
        with self.kernel.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM autonomy_loop_state WHERE subject_id = ?",
                (self.kernel.subject_id,),
            ).fetchone()
        return dict(row)

    def _ensure_loop_state(self) -> None:
        now = utc_now()
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO autonomy_loop_state(subject_id, updated_at) VALUES (?, ?)",
                (self.kernel.subject_id, now),
            )

    def _begin_tick(self) -> TickResult | None:
        self._ensure_loop_state()
        now = utc_now()
        with self.kernel.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM autonomy_loop_state WHERE subject_id = ?",
                (self.kernel.subject_id,),
            ).fetchone()
            if (
                row["circuit_status"] == "open"
                and row["next_retry_at"] is not None
                and row["next_retry_at"] > now
            ):
                return TickResult(
                    self.kernel.lifecycle.current().state,
                    "circuit_open",
                    None,
                    self.config.max_error_backoff_seconds,
                )
            circuit_status = "half_open" if row["circuit_status"] == "open" else "closed"
            connection.execute(
                "UPDATE autonomy_loop_state SET circuit_status = ?, "
                "last_tick_started_at = ?, updated_at = ? WHERE subject_id = ?",
                (circuit_status, now, now, self.kernel.subject_id),
            )
        return None

    def _record_success(self) -> None:
        now = utc_now()
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE autonomy_loop_state SET consecutive_failures = 0, "
                "circuit_status = 'closed', next_retry_at = NULL, "
                "last_successful_tick_at = ?, last_error_type = NULL, updated_at = ? "
                "WHERE subject_id = ?",
                (now, now, self.kernel.subject_id),
            )

    def _record_failure(self, error_type: str) -> None:
        now = datetime.now(UTC)
        with self.kernel.database.transaction() as connection:
            row = connection.execute(
                "SELECT consecutive_failures FROM autonomy_loop_state WHERE subject_id = ?",
                (self.kernel.subject_id,),
            ).fetchone()
            failures = int(row["consecutive_failures"]) + 1
            circuit_status = (
                "open" if failures >= self.config.max_consecutive_failures else "closed"
            )
            next_retry_at = (
                (now + timedelta(seconds=self.config.circuit_cooldown_seconds)).isoformat(
                    timespec="milliseconds"
                )
                if circuit_status == "open"
                else None
            )
            connection.execute(
                "UPDATE autonomy_loop_state SET consecutive_failures = ?, "
                "circuit_status = ?, next_retry_at = ?, last_error_type = ?, updated_at = ? "
                "WHERE subject_id = ?",
                (
                    failures,
                    circuit_status,
                    next_retry_at,
                    error_type,
                    now.isoformat(timespec="milliseconds"),
                    self.kernel.subject_id,
                ),
            )
            if circuit_status == "open":
                self.events._append_connection(
                    connection,
                    self.kernel.subject_id,
                    "autonomy_circuit_opened",
                    "runtime",
                    {"consecutive_failures": failures, "error_type": error_type},
                    privacy_level="private",
                    causal_parent_ids=(),
                    occurred_at=now.isoformat(timespec="milliseconds"),
                    event_id=None,
                )

    async def _advance_sleep(self, lifecycle: str) -> TickResult:
        run = self.sleep.current()
        if run is None or self.reflection_hook is None:
            return TickResult(
                lifecycle,
                "sleep_state_wait",
                None,
                self.config.sleep_interval_seconds,
            )
        if run.status == "winding_down":
            self.sleep.begin_reflection(run.sleep_id)
            action = "reflection_started"
        elif run.status == "reflective_sleep" and run.reflection_event_id is None:
            plan = await self.reflection_hook(run)
            self.sleep.commit_reflection(run.sleep_id, plan)
            action = "reflection_committed"
        elif run.status == "reflective_sleep":
            self.sleep.enter_deep_sleep(run.sleep_id)
            action = "deep_sleep_entered"
        elif run.status == "deep_sleep":
            if run.wake_after and datetime.now(UTC) < datetime.fromisoformat(run.wake_after):
                action = "deep_sleep_wait"
            else:
                self.sleep.wake(run.sleep_id, "scheduled autonomous wake")
                action = "wake_started"
        elif run.status == "waking":
            self.sleep.complete_wake(run.sleep_id)
            action = "wake_completed"
        else:
            action = "sleep_state_wait"
        return TickResult(
            self.kernel.lifecycle.current().state,
            action,
            None,
            self.config.sleep_interval_seconds,
        )

    def _wake_after(self, trigger: str) -> str | None:
        if self.config.deep_sleep_seconds == 0:
            return None
        now = datetime.now(UTC)
        if trigger == "budget":
            wake_at = (now + timedelta(days=1)).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        else:
            wake_at = now + timedelta(seconds=self.config.deep_sleep_seconds)
        return wake_at.isoformat(timespec="milliseconds")
