from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from collections.abc import Iterator, Mapping, Set
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class InjectedFault(RuntimeError):
    """A deterministic test-only failure at a named operation boundary."""


@dataclass(frozen=True)
class FaultRule:
    point: str
    occurrence: int = 1
    reason: str = "injected failure"

    def __post_init__(self) -> None:
        if not self.point:
            raise ValueError("fault point is required")
        if self.occurrence < 1:
            raise ValueError("fault occurrence must be positive")


class FaultInjector:
    def __init__(self, rules: tuple[FaultRule, ...] = ()):
        keys = [(rule.point, rule.occurrence) for rule in rules]
        if len(keys) != len(set(keys)):
            raise ValueError("fault rules must have unique point/occurrence pairs")
        self._rules = {(rule.point, rule.occurrence): rule for rule in rules}
        self._calls: Counter[str] = Counter()
        self._trace: list[str] = []

    @property
    def trace(self) -> tuple[str, ...]:
        return tuple(self._trace)

    def checkpoint(self, point: str) -> None:
        if not point:
            raise ValueError("fault point is required")
        self._calls[point] += 1
        occurrence = self._calls[point]
        self._trace.append(f"{point}:{occurrence}")
        rule = self._rules.get((point, occurrence))
        if rule is not None:
            raise InjectedFault(f"{point}@{occurrence}: {rule.reason}")


@dataclass(frozen=True)
class StateTransition:
    previous: str
    current: str
    evidence: str


class StateMachineProbe:
    """Records transitions and rejects edges outside an explicit contract."""

    def __init__(self, initial: str, allowed: Mapping[str, Set[str]]):
        if initial not in allowed:
            raise ValueError("initial state is absent from the transition contract")
        self._state = initial
        self._allowed = {state: frozenset(targets) for state, targets in allowed.items()}
        self._transitions: list[StateTransition] = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def transitions(self) -> tuple[StateTransition, ...]:
        return tuple(self._transitions)

    def move(self, target: str, *, evidence: str) -> None:
        if target not in self._allowed.get(self._state, frozenset()):
            raise AssertionError(f"forbidden state transition: {self._state} -> {target}")
        if not evidence:
            raise ValueError("state transition evidence is required")
        self._transitions.append(StateTransition(self._state, target, evidence))
        self._state = target


class DeterministicGate:
    """Coordinates concurrent tests without timing sleeps."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()

    def pause(self, *, timeout: float = 5.0) -> None:
        self.reached.set()
        if not self.release.wait(timeout):
            raise TimeoutError("deterministic test gate was not released")


@contextmanager
def hold_sqlite_write_lock(database_path: Path) -> Iterator[None]:
    """Hold a real SQLite write transaction for lock-contention tests."""
    connection = sqlite3.connect(database_path, timeout=1, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    finally:
        connection.rollback()
        connection.close()


def blocking_integrity_process_worker(sender: Any, *_args: object) -> None:
    """Emit one completed finding, then block until the parent terminates the process."""
    from noyra.core.integrity import IntegrityCheckResult, IntegrityFinding

    send = sender.send
    send(("snapshot", {"schema_version": 1, "event_count": 0}))
    send(
        (
            "check_started",
            {
                "id": "core.sqlite_quick_check",
                "version": 1,
                "domain": "core",
                "tier": "light",
            },
        )
    )
    completed = IntegrityCheckResult(
        id="core.sqlite_quick_check",
        version=1,
        domain="core",
        tier="light",
        status="corrupt",
        severity="p0",
        reason_code="integrity_error",
        duration_ms=1,
        rows_examined=1,
        bytes_examined=0,
        details={},
    )
    finding = IntegrityFinding("core.sqlite_quick_check", 1, "p0", "integrity_error")
    send(("check_result", (completed, (finding,))))
    send(
        (
            "check_started",
            {
                "id": "model.ledger",
                "version": 1,
                "domain": "model",
                "tier": "deep",
            },
        )
    )
    threading.Event().wait()


def blocking_periodic_integrity_process_worker(
    sender: Any,
    database_path: str,
    subject_id: str,
    data_root: str,
    profile: str,
    policy_mode: str,
    deadline_seconds: float,
    limits: object,
    check_ids: tuple[str, ...] | None,
    selection: Mapping[str, object],
) -> None:
    if profile == "startup_light":
        from noyra.core.integrity import _run_isolated_registry_worker

        _run_isolated_registry_worker(
            sender,
            database_path,
            subject_id,
            data_root,
            profile,  # type: ignore[arg-type]
            policy_mode,  # type: ignore[arg-type]
            deadline_seconds,
            limits,  # type: ignore[arg-type]
            check_ids,
            selection,
        )
        return
    blocking_integrity_process_worker(sender)
