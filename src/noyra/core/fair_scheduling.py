"""Serial, bounded workflow service; capabilities and inference stay with their owners."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from .admission import assert_current_lease
from .database import Database
from .deadline import monotonic_time
from .errors import IntegrityError
from .scheduling_clock import CooldownClock, aware_time
from .types import canonical_json, content_hash, strict_json_loads, utc_now

WORKFLOWS = (
    "self_modification",
    "outcome_evaluation",
    "memory_consolidation",
    "memory_integration",
    "motivation",
    "self_model",
    "project_execution",
    "project_review",
    "metacognition",
    "interaction",
    "world_acquisition",
    "world_cognition",
    "embedding_maintenance",
)
MAX_TURN_SECONDS = 3.0
_SAFE_REASON = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


@dataclass(frozen=True)
class WorkflowOutcome:
    reason: str
    did_execute: bool = False
    made_progress: bool = False
    next_due_at: str | None = None
    target_id: str | None = None

    @classmethod
    def legacy(cls, reason: str | None) -> WorkflowOutcome:
        # Adapters retain their exact old string contract. A wait/failure string
        # alone is never proof that a provider ran or useful state changed.
        text = reason or "not_due_or_no_work"
        progress = text.endswith(
            (
                "_committed",
                "_applied",
                "_planned",
                "_activate",
                "_complete_phase",
                "_phase_succeeded",
            )
        )
        progress = (
            progress
            or text
            in {
                "cognition_committed",
                "world_observation_recorded",
                "interaction_accepted",
                "interaction_rejected",
                "interaction_silent",
                "interaction_deferred",
            }
            or text.startswith("outcome_")
        )
        if any(word in text for word in ("replayed", "wait", "failed", "exhausted", "unknown")):
            progress = False
        return cls(text, made_progress=progress)


class ScheduleState:
    """One bounded mutable row per subject, not an append-only heartbeat stream."""

    def __init__(self, database: Database, subject_id: str):
        self.database, self.subject_id = database, subject_id

    @staticmethod
    def empty(subject_id: str) -> dict[str, Any]:
        return {
            "version": 1,
            "subject_id": subject_id,
            "cursor": 0,
            "turn": 0,
            "clock_high_water_at": None,
            "clock_status": "unobserved",
            "workflows": {},
        }

    @classmethod
    def read_connection(cls, connection: Any, subject_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM autonomy_schedule_state WHERE subject_id=?", (subject_id,)
        ).fetchone()
        if row is None:
            return cls.empty(subject_id)
        try:
            state = strict_json_loads(row["state_json"])
        except (ValueError, TypeError) as error:
            raise IntegrityError("scheduler control JSON is invalid") from error
        if not isinstance(state, dict) or row["state_hash"] != content_hash(state):
            raise IntegrityError("scheduler control hash mismatch")
        if (
            row["subject_id"] != subject_id
            or state.get("subject_id") != subject_id
            or type(row["revision"]) is not int
            or row["revision"] < 1
            or type(state.get("version")) is not int
            or state["version"] != 1
            or type(state.get("cursor")) is not int
            or not 0 <= state["cursor"] < len(WORKFLOWS)
            or type(state.get("turn")) is not int
            or state["turn"] < 0
            or state.get("clock_status")
            not in {"unobserved", "normal", "wall_clock_behind_history"}
            or not isinstance(state.get("workflows"), dict)
            or not set(state["workflows"]).issubset(WORKFLOWS)
        ):
            raise IntegrityError("scheduler control shape is invalid")
        aware_time(row["updated_at"])
        if state["clock_status"] == "normal" and state["clock_high_water_at"] != row["updated_at"]:
            raise IntegrityError("scheduler high-water time is inconsistent")
        if state.get("clock_high_water_at") is not None:
            aware_time(state["clock_high_water_at"])
        for item in state["workflows"].values():
            if (
                not isinstance(item, dict)
                or type(item.get("considerations")) is not int
                or item["considerations"] < 1
                or not isinstance(item.get("reason"), str)
                or not _SAFE_REASON.fullmatch(item["reason"])
                or any(type(item.get(k)) is not bool for k in ("did_execute", "made_progress"))
                or (
                    item.get("target_id") is not None
                    and (not isinstance(item["target_id"], str) or len(item["target_id"]) > 256)
                )
            ):
                raise IntegrityError("workflow service observation is invalid")
            for field in (
                "last_considered_at",
                "last_finished_at",
                "last_progress_at",
                "next_due_at",
            ):
                if item.get(field) is not None:
                    aware_time(item[field])
        return state

    def read(self) -> dict[str, Any]:
        with self.database.connection() as c:
            return self.read_connection(c, self.subject_id)

    def _write(self, connection: Any, state: dict[str, Any], now: str) -> None:
        high = state.get("clock_high_water_at")
        state["clock_status"] = (
            "wall_clock_behind_history"
            if high is not None and aware_time(now) < aware_time(high)
            else "normal"
        )
        if high is None or aware_time(now) > aware_time(high):
            state["clock_high_water_at"] = now
        connection.execute(
            "INSERT INTO autonomy_schedule_state(subject_id,revision,state_json,state_hash,"
            "updated_at) "
            "VALUES (?,1,?,?,?) ON CONFLICT(subject_id) DO UPDATE SET "
            "revision=autonomy_schedule_state.revision+1,state_json=excluded.state_json,"
            "state_hash=excluded.state_hash,updated_at=excluded.updated_at",
            (self.subject_id, canonical_json(state), content_hash(state), now),
        )

    def begin(self, now: str) -> tuple[dict[str, Any], int]:
        with self.database.transaction() as c:
            state = self.read_connection(c, self.subject_id)
            start = int(state["cursor"])
            state["cursor"] = (start + 1) % len(WORKFLOWS)
            state["turn"] += 1
            self._write(c, state, now)
        return state, start

    def finish(self, observations: dict[str, Any], cursor: int, now: str) -> None:
        with self.database.transaction() as c:
            state = self.read_connection(c, self.subject_id)
            state["workflows"].update(observations)
            state["cursor"] = cursor
            self._write(c, state, now)


class FairScheduler:
    def __init__(
        self,
        database: Database,
        subject_id: str,
        *,
        clock: Callable[[], str] = utc_now,
        monotonic_clock: Callable[[], float] = monotonic_time,
    ):
        self.database, self.subject_id, self.clock = database, subject_id, clock
        self.monotonic_clock = monotonic_clock
        self.cooldowns = CooldownClock(monotonic_clock)
        self.state = ScheduleState(database, subject_id)
        self._lock = asyncio.Lock()
        self._current: dict[str, Any] = {}

    def next_target(self, name: str, targets: list[str]) -> str | None:
        if not targets:
            return None
        last = self._current.get("workflows", {}).get(name, {}).get("target_id")
        ordered = sorted(set(targets))
        if last in ordered:
            return ordered[(ordered.index(last) + 1) % len(ordered)]
        return ordered[0]

    def _activity(self) -> int:
        with self.database.connection() as c:
            return int(
                c.execute(
                    "SELECT COALESCE(SUM(row_count),0) FROM evidence_row_counts "
                    "WHERE subject_id=? AND table_name IN "
                    "('actions','model_attempts','embedding_usage_entries')",
                    (self.subject_id,),
                ).fetchone()[0]
            )

    async def run(
        self,
        dispatch: Callable[[str], Awaitable[str | WorkflowOutcome | None]],
        *,
        active: Callable[[], bool],
    ) -> str:
        if self._lock.locked():
            return "scheduler_in_progress"
        async with self._lock:
            if not active():
                return "cognition_inactive"
            state, start = self.state.begin(self.clock())
            self._current = state
            records: dict[str, Any] = {}
            cursor = (start + 1) % len(WORKFLOWS)
            began = self.monotonic_clock()
            idle = "world_sources_waiting"
            try:
                for offset in range(len(WORKFLOWS)):
                    assert_current_lease()
                    if not active():
                        return "cognition_inactive"
                    if offset and self.monotonic_clock() - began >= MAX_TURN_SECONDS:
                        cursor = (start + offset) % len(WORKFLOWS)
                        break
                    index = (start + offset) % len(WORKFLOWS)
                    name, now = WORKFLOWS[index], self.clock()
                    previous = state["workflows"].get(name, {})
                    due = previous.get("next_due_at")
                    if due is not None:
                        token = str(previous.get("last_finished_at")) + due
                        remaining = (aware_time(due) - aware_time(now)).total_seconds()
                        if not self.cooldowns.ready(name, token, remaining):
                            continue
                    before = self._activity()
                    considered = now
                    try:
                        raw = await dispatch(name)
                    except Exception as error:
                        assert_current_lease()
                        now = self.clock()
                        delay_at = (aware_time(now) + timedelta(seconds=60)).isoformat()
                        reason = "workflow_exception_" + type(error).__name__.lower()
                        if not _SAFE_REASON.fullmatch(reason):
                            reason = "workflow_exception"
                        records[name] = {
                            "considerations": int(previous.get("considerations", 0)) + 1,
                            "last_considered_at": considered,
                            "last_finished_at": now,
                            "last_progress_at": previous.get("last_progress_at"),
                            "reason": reason[:128],
                            "did_execute": self._activity() > before,
                            "made_progress": False,
                            "next_due_at": delay_at,
                            "target_id": previous.get("target_id"),
                        }
                        cursor = (index + 1) % len(WORKFLOWS)
                        raise
                    assert_current_lease()
                    outcome = (
                        raw if isinstance(raw, WorkflowOutcome) else WorkflowOutcome.legacy(raw)
                    )
                    outcome = replace(
                        outcome, did_execute=outcome.did_execute or self._activity() > before
                    )
                    now = self.clock()
                    reason = (
                        outcome.reason
                        if _SAFE_REASON.fullmatch(outcome.reason)
                        else "workflow_result"
                    )
                    next_due = outcome.next_due_at
                    if next_due is None and not outcome.made_progress:
                        # A failed/empty dispatch yields instead of repeatedly monopolizing a turn.
                        delay = (
                            60
                            if any(x in reason for x in ("failed", "exhausted", "unknown"))
                            else 0
                        )
                        if delay:
                            next_due = (aware_time(now) + timedelta(seconds=delay)).isoformat()
                    record = {
                        "considerations": int(previous.get("considerations", 0)) + 1,
                        "last_considered_at": considered,
                        "last_finished_at": now,
                        "last_progress_at": now
                        if outcome.made_progress
                        else previous.get("last_progress_at"),
                        "reason": reason,
                        "did_execute": outcome.did_execute,
                        "made_progress": outcome.made_progress,
                        "next_due_at": next_due,
                        "target_id": outcome.target_id
                        if outcome.target_id is not None
                        else previous.get("target_id"),
                    }
                    records[name] = record
                    if next_due is not None:
                        self.cooldowns.defer(
                            name,
                            now + next_due,
                            (aware_time(next_due) - aware_time(now)).total_seconds(),
                        )
                    if name.startswith("world_") and raw is not None:
                        idle = outcome.reason
                    if outcome.did_execute or outcome.made_progress or not active():
                        cursor = (index + 1) % len(WORKFLOWS)
                        return outcome.reason
                return idle
            finally:
                # Expired runtime leases must not make subject commits. The next
                # owner recovers the cursor reserved before dispatch instead.
                assert_current_lease()
                self.state.finish(records, cursor, self.clock())
