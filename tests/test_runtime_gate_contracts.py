from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core.admission import OperationInvalidated, RuntimeAdmissionGate
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.integrity import (
    IntegrityCheckOutcome,
    IntegrityCheckSpec,
    IntegrityRegistry,
    IntegrityRuntimeController,
)
from noyra.service import NoyraService
from noyra.sleep import SleepEngine, SleepReflectionPlan
from test_authentication_input_contract import call
from test_gate0_runtime_boundaries import _settings


def business_state(connection: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        name: list(connection.execute("SELECT * FROM " + name + " ORDER BY rowid"))
        for name in (
            "subject_identity",
            "runtime_state",
            "state_snapshots",
            "events",
            "actions",
            "model_calls",
            "model_attempts",
            "audit_records",
        )
    }


def test_unowned_service_diagnostic_reads_committed_wal_without_business_writes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "runtime", "gate-contract-wal")
    owner = NoyraService(settings)
    observer: NoyraService | None = None
    try:
        owner.boot()
        database = settings.data_dir / "noyra.sqlite3"
        with closing(sqlite3.connect(database)) as anchor:
            assert anchor.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert anchor.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
            anchor.execute("PRAGMA wal_autocheckpoint=0")
            checkpoint = owner.kernel.checkpoint(
                {"state": "committed WAL only"}, reason="Synthetic diagnostic boundary"
            )
            paused = owner.kernel.pause("Synthetic WAL-only pause")
            wal = database.with_name(database.name + "-wal")
            before_bytes = (database.read_bytes(), wal.read_bytes())
            before = business_state(anchor)
            with patch(
                "noyra.core.database.sqlite3.connect",
                side_effect=AssertionError("unowned startup cannot open SQLite"),
            ):
                observer = NoyraService(settings)
                with pytest.raises(RuntimeOwnershipError):
                    observer.boot()
            assert not observer.kernel.process_lock.held and owner.kernel.process_lock.held
            assert observer.kernel.database.read_only
            assert not observer.kernel.database.immutable_snapshot
            health = observer.kernel.health()
            assert health["lifecycle"] == paused.__dict__
            assert observer.kernel.identity is not None
            assert observer.kernel.identity.last_checkpoint == checkpoint.snapshot_id
            assert (
                observer.kernel.snapshot_store.latest(settings.subject_id).snapshot_id
                == checkpoint.snapshot_id
            )
            # A write must still fail even when the additional query_only
            # safeguard is explicitly disabled: mode=ro is authoritative.
            with observer.kernel.database.connection() as reader:
                reader.execute("PRAGMA query_only=OFF")
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    reader.execute("UPDATE runtime_state SET version=version+1")
            assert business_state(anchor) == before
            assert (database.read_bytes(), wal.read_bytes()) == before_bytes
            observer.close()
            assert owner.kernel.process_lock.held
    finally:
        if observer is not None:
            observer.close()
        owner.close()


@dataclass
class RecheckCase:
    service: NoyraService
    now: list[datetime]
    deep_outcome: list[IntegrityCheckOutcome]
    checks_run: list[str]

    def next_tick(self) -> Any:
        self.now[0] += timedelta(seconds=2)
        return asyncio.run(self.service._integrity_pre_tick())


@pytest.fixture
def recheck_case(tmp_path: Path) -> Iterator[RecheckCase]:
    # A tiny deterministic registry isolates the evidence protocol; this is
    # not a substitute for the real registry's neighboring regression suite.
    service = NoyraService(_settings(tmp_path, "gate-contract-recheck", integrity_mode="off"))
    service.boot()
    now = [datetime.now(UTC)]
    outcome = [IntegrityCheckOutcome()]
    checks_run: list[str] = []

    class PreviousRegistry(IntegrityRegistry):
        version = "gate-contract/previous"

    class CurrentRegistry(IntegrityRegistry):
        version = "gate-contract/current"

    old = IntegrityRuntimeController(
        service.kernel,
        tmp_path,
        policy_mode="pause",
        clock=lambda: now[0],
        registry=PreviousRegistry(
            (
                IntegrityCheckSpec(
                    "gate.old",
                    1,
                    "gate",
                    frozenset({"startup_light"}),
                    lambda _context: IntegrityCheckOutcome(),
                    "light",
                ),
            )
        ),
    )
    old.run_startup()
    assert old.summary()["status"] == "ok"
    service.kernel.pause("Synthetic manual pause")

    def light(_context: Any) -> IntegrityCheckOutcome:
        checks_run.append("gate.new_light")
        return IntegrityCheckOutcome()

    def deep(_context: Any) -> IntegrityCheckOutcome:
        checks_run.append("gate.new_deep")
        return outcome[0]

    controller = IntegrityRuntimeController(
        service.kernel,
        tmp_path,
        policy_mode="pause",
        interval_seconds=1,
        retry_seconds=1,
        clock=lambda: now[0],
        registry=CurrentRegistry(
            (
                IntegrityCheckSpec(
                    "gate.new_light", 1, "gate", frozenset({"startup_light"}), light, "light"
                ),
                IntegrityCheckSpec(
                    "gate.new_deep", 1, "gate", frozenset({"periodic_deep"}), deep, "deep"
                ),
            )
        ),
    )
    service.integrity = controller
    service.http.integrity = controller
    assert service.http.operator_controls is not None
    service.http.operator_controls.integrity = controller
    try:
        yield RecheckCase(service, now, outcome, checks_run)
    finally:
        service.close()


def test_clean_recheck_restores_only_paused_control_access(recheck_case: RecheckCase) -> None:
    case = recheck_case
    service = case.service
    assert service.integrity.summary()["evidence"]["recheck_pending"]
    first = case.next_tick()
    assert first is not None and first.action == "integrity_evidence_recheck_pending"
    assert case.checks_run == ["gate.new_light"]
    assert not service.kernel.admission.accepting and service.kernel.admission.quarantined
    assert service.kernel.lifecycle.current().state == "paused"
    with (
        pytest.raises(OperationInvalidated),
        service.kernel.admission.lifecycle_control_scope(),
    ):
        pytest.fail("pending evidence must block lifecycle controls")
    assert case.next_tick() is None
    assert case.checks_run == ["gate.new_light", "gate.new_deep"]
    assert service.integrity.summary()["coverage"]["status"] == "complete"
    assert not service.integrity.summary()["evidence"]["recheck_pending"]
    assert service.integrity.summary()["status"] == "ok"
    assert service.kernel.lifecycle.current().state == "paused"
    assert not service.kernel.admission.accepting
    # This was the remaining bug: clean evidence did not restore the operator
    # control lane, although ordinary cognition must remain manually paused.
    with service.kernel.admission.lifecycle_control_scope():
        pass
    with pytest.raises(OperationInvalidated):
        service.kernel.admission.begin("ordinary-work")
    service.http.start()
    status, result, _ = call(
        service.http,
        "/api/v1/admin/lifecycle/resume",
        headers={"Authorization": "Bearer gate0-admin-token-" + "x" * 32},
        payload={"reason": "Synthetic operator resume after verified recovery"},
    )
    assert status == 200 and result["lifecycle"]["state"] == "active"
    assert service.kernel.admission.accepting


@pytest.mark.parametrize(
    "outcome",
    [
        IntegrityCheckOutcome("incomplete", "p1", "synthetic_incomplete"),
        IntegrityCheckOutcome("corrupt", "p1", "synthetic_corrupt"),
        IntegrityCheckOutcome("corrupt", "p0", "synthetic_corrupt_critical"),
    ],
)
def test_incomplete_or_corrupt_recheck_blocks_controls_until_a_real_clean_result(
    recheck_case: RecheckCase, outcome: IntegrityCheckOutcome
) -> None:
    case = recheck_case
    assert case.next_tick().action == "integrity_evidence_recheck_pending"
    case.deep_outcome[0] = outcome
    result = case.next_tick()
    assert result is not None
    assert result.action in {"integrity_evidence_recheck_pending", "integrity_safe_pause"}
    assert case.service.kernel.lifecycle.current().state == "paused"
    assert case.service.kernel.admission.quarantined
    assert not case.service.kernel.admission.accepting
    with (
        pytest.raises(OperationInvalidated),
        case.service.kernel.admission.lifecycle_control_scope(),
    ):
        pytest.fail("pending or corrupt evidence must keep controls quarantined")
    case.deep_outcome[0] = IntegrityCheckOutcome()
    assert case.next_tick() is None
    assert case.service.integrity.summary()["status"] == "ok"
    assert not case.service.integrity.summary()["evidence"]["recheck_pending"]
    assert not case.service.kernel.admission.quarantined
    assert not case.service.kernel.admission.accepting
    assert case.service.kernel.lifecycle.current().state == "paused"


def test_verified_recovery_does_not_revive_pre_quarantine_work(recheck_case: RecheckCase) -> None:
    case = recheck_case
    # A lease from before quarantine must remain invalid even after every new
    # check passes; clearing quarantine cannot roll the epoch backwards.
    old = case.service.kernel.admission.begin("synthetic-old", allow_quarantine=True)
    case.service.kernel.admission.finish(old)
    case.next_tick()
    invalidated_epoch = case.service.kernel.admission.epoch
    assert not old.valid
    case.next_tick()
    assert not case.service.kernel.admission.quarantined
    assert case.service.kernel.admission.epoch == invalidated_epoch
    assert not old.valid
    assert not case.service.kernel.admission.accepting


@pytest.mark.parametrize("fence", ["draining", "migration"])
def test_verified_recovery_does_not_clear_shutdown_or_migration_fencing(
    recheck_case: RecheckCase, fence: str
) -> None:
    case = recheck_case
    if fence == "draining":
        case.next_tick()
        case.service.kernel.admission.begin_drain()
    else:
        # Authorize the synthetic migration before integrity quarantine;
        # migration control admission itself must not bypass quarantine.
        with case.service.kernel.admission.migration_control_scope():
            case.next_tick()
            case.service.kernel.admission.fence_for_migration(timeout=0)
    case.next_tick()
    assert case.service.integrity.summary()["status"] == "ok"
    assert case.service.kernel.admission.quarantined
    assert not case.service.kernel.admission.accepting
    assert case.service.kernel.lifecycle.current().state == "paused"
    assert case.service.kernel.admission.closed == (fence == "draining")
    assert case.service.kernel.admission.migration_fenced == (fence == "migration")


def test_verified_recovery_restores_sleep_progress_without_ordinary_work(
    recheck_case: RecheckCase,
) -> None:
    case = recheck_case
    case.service.kernel.resume("Synthetic sleep fixture")
    engine = SleepEngine(case.service.kernel.database, case.service.kernel.subject_id)
    run = engine.start("subject_choice", "Synthetic reflected sleep")
    engine.begin_reflection(run.sleep_id)
    engine.commit_reflection(run.sleep_id, SleepReflectionPlan(summary="Synthetic local plan"))
    engine.enter_deep_sleep(run.sleep_id)
    case.next_tick()
    assert case.service.kernel.admission.quarantined
    assert case.service.kernel.lifecycle.current().state == "deep_sleep"
    assert case.next_tick() is None
    assert not case.service.kernel.admission.quarantined
    assert not case.service.kernel.admission.accepting
    assert case.service.kernel.lifecycle.current().state == "deep_sleep"
    assert asyncio.run(case.service.loop.tick()).action == "wake_started"
    assert asyncio.run(case.service.loop.tick()).action == "wake_completed"
    assert case.service.kernel.lifecycle.current().state == "active"
    assert case.service.kernel.admission.accepting


@pytest.mark.parametrize("when", ["during_worker", "after_check"])
def test_clean_report_cannot_clear_a_newer_quarantine_during_recovery(
    recheck_case: RecheckCase, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    case = recheck_case
    case.next_tick()
    checked = case.service._http_quarantine_active

    def quarantine_after_checked_clean() -> bool:
        blocked = checked()
        if not blocked:
            case.service.kernel.admission.quarantine()
        return blocked

    run_checks = case.service.integrity.run_periodic_if_due

    def quarantine_after_worker_report() -> Any:
        report = run_checks()
        case.service.kernel.admission.quarantine()
        return report

    with monkeypatch.context() as context:
        if when == "during_worker":
            context.setattr(
                case.service.integrity, "run_periodic_if_due", quarantine_after_worker_report
            )
        else:
            context.setattr(case.service, "_http_quarantine_active", quarantine_after_checked_clean)
        assert case.next_tick() is None
    # A clean report from the older epoch cannot authorize removing a later
    # quarantine. A later independent clean tick may recover normally.
    assert case.service.integrity.summary()["status"] == "ok"
    assert case.service.kernel.admission.quarantined
    assert not case.service.kernel.admission.accepting
    assert case.service.kernel.lifecycle.current().state == "paused"
    assert case.next_tick() is None
    assert not case.service.kernel.admission.quarantined
    assert not case.service.kernel.admission.accepting


def test_guarded_quarantine_clear_keeps_epoch_and_closed_admission() -> None:
    gate = RuntimeAdmissionGate("gate-contract")
    previous = gate.begin("previous-work")
    gate.finish(previous)
    expected = gate.quarantine()
    gate.clear_quarantine(expected_epoch=expected - 1)
    assert gate.quarantined and not gate.accepting
    gate.clear_quarantine(expected_epoch=expected)
    assert not gate.quarantined and not gate.accepting
    assert gate.epoch == expected and not previous.valid
    gate.begin_drain()
    with pytest.raises(OperationInvalidated, match="draining"):
        gate.clear_quarantine(expected_epoch=gate.epoch)
    assert gate.closed and not gate.accepting
