"""Fail-closed health and retry control for the integrity checker itself."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from .checkpoint_clock import append_checkpoint
from .database import Database
from .deadline import monotonic_time
from .errors import IntegrityError
from .events import EventStore
from .scheduling_clock import aware_time
from .types import content_hash


def workflow_key(subject_id: str) -> str:
    return "integrity-watchdog:" + content_hash(subject_id)


def read_watchdog(connection: Any, subject_id: str) -> dict[str, Any] | None:
    from noyra.autonomy.workflow import DurableWorkflowStore

    row = connection.execute(
        "SELECT * FROM workflow_checkpoints WHERE workflow_id=? "
        "ORDER BY checkpoint_version DESC LIMIT 1",
        (workflow_key(subject_id),),
    ).fetchone()
    if row is None:
        return None
    record = DurableWorkflowStore._from_row(row)
    state = record.state
    if (
        record.subject_id != subject_id
        or record.workflow_type != "integrity_watchdog"
        or record.status != "running"
        or set(state)
        != {
            "version",
            "consecutive_failures",
            "total_failures",
            "escalated",
            "retry_seconds",
            "last_attempt_at",
            "last_failure_at",
            "last_success_at",
            "last_error_type",
            "next_retry_at",
        }
        or state.get("version") != 1
        or type(state.get("version")) is not int
        or any(
            type(state.get(k)) is not int or not 0 <= state[k] <= 10**9
            for k in ("consecutive_failures", "total_failures")
        )
        or state["total_failures"] < state["consecutive_failures"]
        or type(state.get("escalated")) is not bool
        or not isinstance(state.get("retry_seconds"), (int, float))
        or isinstance(state["retry_seconds"], bool)
        or not 0 <= state["retry_seconds"] <= 86400
    ):
        raise IntegrityError("integrity watchdog control is invalid")
    for key in ("last_attempt_at", "last_failure_at", "last_success_at", "next_retry_at"):
        if state.get(key) is not None:
            aware_time(state[key])
    error = state.get("last_error_type")
    if error is not None and (
        not isinstance(error, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error) is None
    ):
        raise IntegrityError("integrity watchdog error type is invalid")
    if state["consecutive_failures"] and (state.get("next_retry_at") is None or error is None):
        raise IntegrityError("integrity watchdog retry evidence is missing")
    if (
        state["last_attempt_at"] is None
        or state["last_failure_at"] is None
        or (
            not state["consecutive_failures"]
            and (
                state["next_retry_at"] is not None
                or state["last_error_type"] is not None
                or state["escalated"]
                or state["retry_seconds"] != 0
            )
        )
    ):
        raise IntegrityError("integrity watchdog transition evidence is invalid")
    return dict(state)


class IntegrityWatchdog:
    def __init__(
        self,
        database: Database,
        subject_id: str,
        *,
        base_seconds: float,
        maximum_seconds: float,
        threshold: int,
        clock: Callable[[], str],
        monotonic: Callable[[], float] = monotonic_time,
    ):
        self.database, self.subject_id = database, subject_id
        self.base, self.maximum, self.threshold = base_seconds, maximum_seconds, threshold
        self.clock, self.monotonic = clock, monotonic
        self._deadline: float | None = None
        self._token: tuple[Any, ...] | None = None
        self._fallback: dict[str, Any] | None = None
        self._evidence_unavailable = False
        self._observed = False
        self._last_persisted: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any] | None:
        with self.database.read_transaction() as c:
            state = read_watchdog(c, self.subject_id)
        self._observed = True
        self._last_persisted = state
        return state

    def remaining(self) -> float:
        if (
            self._fallback is not None
            and self._deadline is not None
            and self.monotonic() < self._deadline
        ):
            return self._deadline - self.monotonic()
        try:
            state = self._load()
        except Exception:
            # Bad/unavailable watchdog evidence cannot become permission to run.
            self._evidence_unavailable = True
            self._fallback_failure("WatchdogEvidenceUnavailable")
            return max(0.1, (self._deadline or self.monotonic()) - self.monotonic())
        if self._fallback is not None:
            state = self._fallback
        if state is None or not state["consecutive_failures"]:
            return 0
        token = (state["consecutive_failures"], state["next_retry_at"])
        if self._token != token:
            delay = min(
                float(state["retry_seconds"]),
                max(
                    0.0,
                    (aware_time(state["next_retry_at"]) - aware_time(self.clock())).total_seconds(),
                ),
            )
            self._deadline = self.monotonic() + delay
            self._token = token
        return max(0.0, (self._deadline or self.monotonic()) - self.monotonic())

    def failed(self, error_type: str) -> tuple[dict[str, Any], str | None]:
        try:
            previous = self._load()
            base = previous
            if self._fallback is not None and (
                base is None or self._fallback["total_failures"] > base["total_failures"]
            ):
                base = self._fallback
            state = self._failure_state(base, error_type)
            event_type = (
                "integrity_watchdog_error"
                if previous is None or not previous["consecutive_failures"]
                else "integrity_watchdog_escalated"
                if state["escalated"] and not previous["escalated"]
                else None
            )
            event = self._persist(state, event_type)
            self._fallback = None
            self._evidence_unavailable = False
            self._last_persisted = dict(state)
        except Exception:
            state = self._fallback_failure(error_type, previous if "previous" in locals() else None)
            event = None
        self._token = (state["consecutive_failures"], state["next_retry_at"])
        self._deadline = self.monotonic() + float(state["retry_seconds"])
        return state, event

    def _failure_state(self, previous: dict[str, Any] | None, error_type: str) -> dict[str, Any]:
        prior = previous or {}
        failures = min(10**9, int(prior.get("consecutive_failures", 0)) + 1)
        delay = min(self.maximum, self.base * 2 ** min(failures - 1, 20))
        now = self.clock()
        safe_error = (
            error_type
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_type)
            else "CheckerError"
        )
        return {
            "version": 1,
            "consecutive_failures": failures,
            "total_failures": min(10**9, int(prior.get("total_failures", 0)) + 1),
            "escalated": failures >= self.threshold,
            "retry_seconds": delay,
            "last_attempt_at": now,
            "last_failure_at": now,
            "last_success_at": prior.get("last_success_at"),
            "last_error_type": safe_error,
            "next_retry_at": (aware_time(now) + timedelta(seconds=delay)).isoformat(),
        }

    def _fallback_failure(
        self, error_type: str, previous: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        prior = self._fallback
        if previous is not None and (
            prior is None or previous["total_failures"] > prior["total_failures"]
        ):
            prior = previous
        self._fallback = self._failure_state(prior, error_type)
        self._token = (self._fallback["consecutive_failures"], self._fallback["next_retry_at"])
        self._deadline = self.monotonic() + float(self._fallback["retry_seconds"])
        return self._fallback

    def succeeded(self) -> None:
        previous = self._load()
        if previous is None and self._fallback is None:
            return
        if previous is not None and not previous["consecutive_failures"] and self._fallback is None:
            return
        state = dict(previous or self._fallback or {})
        if self._fallback is not None:
            state["total_failures"] = max(
                state.get("total_failures", 0), self._fallback["total_failures"]
            )
        state.update(
            consecutive_failures=0,
            escalated=False,
            retry_seconds=0,
            next_retry_at=None,
            last_error_type=None,
            last_success_at=self.clock(),
            last_attempt_at=self.clock(),
        )
        self._persist(state, "integrity_watchdog_recovered")
        self._last_persisted = dict(state)
        self._fallback = None
        self._evidence_unavailable = False
        self._deadline = None
        self._token = None

    def _persist(self, state: dict[str, Any], event_type: str | None) -> str | None:
        with self.database.transaction() as c:
            read_watchdog(c, self.subject_id)
            prior = c.execute(
                "SELECT * FROM workflow_checkpoints WHERE workflow_id=? "
                "ORDER BY checkpoint_version DESC LIMIT 1",
                (workflow_key(self.subject_id),),
            ).fetchone()
            append_checkpoint(
                c,
                self.database,
                subject_id=self.subject_id,
                workflow_id=workflow_key(self.subject_id),
                workflow_type="integrity_watchdog",
                version=1 if prior is None else int(prior["checkpoint_version"]) + 1,
                status="running",
                state=state,
                reason="integrity_checker_health",
                observed_at=self.clock(),
                previous=prior,
            )
            if event_type is None:
                return None
            event = EventStore(self.database)._append_connection(
                c,
                self.subject_id,
                event_type,
                "runtime",
                {
                    "consecutive_failures": state["consecutive_failures"],
                    "error_type": state["last_error_type"],
                    "escalated": state["escalated"],
                    "retry_seconds": state["retry_seconds"],
                },
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=self.clock(),
                event_id=None,
            )
            return event.event_id

    def snapshot(self) -> dict[str, Any]:
        """Current main-service health without a new HTTP-side database read."""
        if self._fallback is not None:
            return self.health()
        if not self._observed:
            return {"status": "not_checked"}
        state = self._last_persisted
        return {
            "status": "ok"
            if state is None or not state["consecutive_failures"]
            else "escalated"
            if state["escalated"]
            else "backoff",
            "persisted": True,
            **(state or {"consecutive_failures": 0, "total_failures": 0}),
        }

    def health(self) -> dict[str, Any]:
        state: dict[str, Any] | None
        if self._fallback is not None:
            state = self._fallback
            return {
                **state,
                "status": "evidence_unavailable"
                if self._evidence_unavailable
                else "escalated"
                if state["escalated"]
                else "backoff",
                "persisted": False,
            }
        try:
            state = self._load()
        except Exception:
            state = self._fallback
            return {
                "status": "evidence_unavailable",
                "consecutive_failures": None if state is None else state["consecutive_failures"],
                "persisted": False,
            }
        if self._fallback is not None:
            state = self._fallback
        return {
            "status": "ok"
            if state is None or not state["consecutive_failures"]
            else "escalated"
            if state["escalated"]
            else "backoff",
            "persisted": self._fallback is None,
            **(state or {"consecutive_failures": 0, "total_failures": 0}),
        }
