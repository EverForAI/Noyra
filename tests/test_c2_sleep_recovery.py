from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import EventStore, SubjectKernel
from noyra.core.errors import IntegrityError, InvalidTransitionError, NotFoundError
from noyra.core.types import content_hash
from noyra.sleep import SleepEngine, SleepIntegrity, SleepReflectionPlan
from noyra.sleep.errors import SleepStateConflictError


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    with SubjectKernel(tmp_path / "subject.sqlite3", "c2-sleep", content_hash("c2")) as instance:
        instance.boot()
        instance.orient()
        instance.activate()
        yield instance


def at_phase(kernel: SubjectKernel, phase: str) -> tuple[SleepEngine, str]:
    engine = SleepEngine(kernel.database, kernel.subject_id)
    run = engine.start("subject_choice", "Synthetic sleep")
    if phase != "winding_down":
        engine.begin_reflection(run.sleep_id)
    if phase in {"deep_sleep", "waking"}:
        engine.commit_reflection(run.sleep_id, SleepReflectionPlan(summary="Synthetic reflection"))
        engine.enter_deep_sleep(run.sleep_id)
    if phase == "waking":
        engine.wake(run.sleep_id, "Synthetic wake")
    return engine, run.sleep_id


@pytest.mark.parametrize("phase", ["winding_down", "reflective_sleep", "deep_sleep", "waking"])
def test_failed_sleep_exits_atomically_without_claiming_restoration(
    kernel: SubjectKernel, phase: str
) -> None:
    engine, sleep_id = at_phase(kernel, phase)
    before = engine.fatigue.ensure(kernel.subject_id)
    run = engine.get(sleep_id)
    failed = engine.fail(sleep_id, "operator_abort", expected_version=run.version)
    assert failed.status == "failed" and failed.completed_at is not None
    assert kernel.lifecycle.current().state == "paused"
    assert engine.fatigue.ensure(kernel.subject_id) == before
    assert failed.reflection_event_id == run.reflection_event_id
    assert failed.checkpoint_id == run.checkpoint_id
    assert engine.current() is None
    SleepIntegrity(kernel.database).verify(kernel.subject_id)


def snapshot(kernel: SubjectKernel) -> dict[str, list[dict[str, Any]]]:
    with kernel.database.read_transaction() as c:
        return {
            name: [dict(row) for row in c.execute("SELECT * FROM " + name + " ORDER BY rowid")]
            for name in (
                "sleep_runs",
                "sleep_transitions",
                "fatigue_states",
                "fatigue_transitions",
                "events",
                "runtime_state",
                "model_calls",
                "model_attempts",
                "waiting_cognitive_tasks",
                "waiting_cognitive_task_revisions",
            )
        }


@pytest.mark.parametrize("failure_point", ["event", "lifecycle", "waits"])
def test_failure_exit_rolls_back_as_one_unit_then_retries(
    kernel: SubjectKernel, failure_point: str
) -> None:
    engine, identifier = at_phase(kernel, "reflective_sleep")
    run = engine.get(identifier)
    before = snapshot(kernel)
    if failure_point == "lifecycle":
        target = patch.object(
            engine.lifecycle, "_transition_connection", side_effect=OSError("fault")
        )
    elif failure_point == "waits":
        target = patch("noyra.sleep.engine.cancel_terminal_waits", side_effect=OSError("fault"))
    else:
        original = engine.events._append_connection

        def fail(*args: Any, **kwargs: Any) -> Any:
            if len(args) > 2 and args[2] == "sleep_failed":
                raise OSError("fault")
            return original(*args, **kwargs)

        target = patch.object(engine.events, "_append_connection", side_effect=fail)
    with target, pytest.raises(OSError, match="fault"):
        engine.fail(identifier, "operator_abort", expected_version=run.version)
    assert snapshot(kernel) == before
    assert (
        engine.fail(identifier, "operator_abort", expected_version=run.version).status == "failed"
    )
    SleepIntegrity(kernel.database).verify(kernel.subject_id)


def test_failure_is_idempotent_and_stale_versions_cannot_close_new_work(
    kernel: SubjectKernel,
) -> None:
    engine, identifier = at_phase(kernel, "reflective_sleep")
    run = engine.get(identifier)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: engine.fail(identifier, "operator_abort", expected_version=run.version),
                range(2),
            )
        )
    assert results[0] == results[1]
    before = snapshot(kernel)
    with pytest.raises(SleepStateConflictError):
        engine.fail(identifier, "operator_abort", expected_version=run.version - 1)
    assert snapshot(kernel) == before
    kernel.resume("Synthetic recovery")
    later = engine.start("subject_choice", "New sleep")
    assert engine.fail(identifier, "operator_abort", expected_version=run.version) == results[0]
    assert engine.current() == later
    assert kernel.lifecycle.current().state == "winding_down"


def test_foreign_run_and_late_reflection_cannot_cross_failure_boundary(
    kernel: SubjectKernel,
) -> None:
    engine, identifier = at_phase(kernel, "reflective_sleep")
    run = engine.get(identifier)
    before = snapshot(kernel)
    foreign = SleepEngine(kernel.database, "another-subject")
    with pytest.raises(NotFoundError):
        foreign.fail(identifier, "operator_abort", expected_version=run.version)
    assert snapshot(kernel) == before
    engine.fail(identifier, "operator_abort", expected_version=run.version)
    before = snapshot(kernel)
    with pytest.raises(SleepStateConflictError):
        engine.commit_reflection(identifier, SleepReflectionPlan(summary="Late response"))
    assert snapshot(kernel) == before


def test_unknown_attempt_and_cancelled_wait_retain_their_accounting(kernel: SubjectKernel) -> None:
    from noyra.model import BudgetLimits, CognitiveResourceStore, ModelLedger, RoutedModelGateway

    engine, identifier = at_phase(kernel, "reflective_sleep")
    purpose = "sleep_reflection:" + identifier
    ledger = ModelLedger(kernel.database)
    call, _ = ledger.prepare_call(
        kernel.subject_id, "fixture", "fixture", purpose, "a" * 64, "c2-key"
    )
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(10, 1000, 1000, 1000),
        reserved_input_tokens=10,
        reserved_output_tokens=20,
        reserved_cost_microusd=30,
    )
    ledger.start_attempt(attempt.attempt_id)
    ledger.recover_interrupted(kernel.subject_id)
    router = RoutedModelGateway(
        kernel.database,
        kernel.subject_id,
        CognitiveResourceStore(kernel.database, kernel.database.path.parent / "secrets"),
    )
    router._record_unavailable("deep", purpose, "model_outcome_unknown_quarantined")
    before = snapshot(kernel)
    engine.fail(identifier, "operator_abort", expected_version=engine.get(identifier).version)
    after = snapshot(kernel)
    assert after["model_calls"] == before["model_calls"]
    assert after["model_attempts"] == before["model_attempts"]
    assert after["model_calls"][0]["status"] == "unknown"
    assert after["model_attempts"][0]["reserved_cost_microusd"] == 30
    assert after["waiting_cognitive_tasks"][0]["status"] == "cancelled"
    assert after["waiting_cognitive_tasks"][0]["reason_code"] == "parent_sleep_failed"
    SleepIntegrity(kernel.database).verify(kernel.subject_id)


def test_corruption_cannot_be_disguised_as_a_clean_abort(kernel: SubjectKernel) -> None:
    engine, identifier = at_phase(kernel, "reflective_sleep")
    version = engine.get(identifier).version
    # Standalone damaged-file fixture, outside normal append-only enforcement.
    with closing(sqlite3.connect(kernel.database.path)) as c:
        for (name,) in c.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='sleep_transitions'"
        ).fetchall():
            c.execute('DROP TRIGGER "' + name + '"')
        c.execute(
            "UPDATE sleep_transitions SET state_hash=? WHERE sleep_id=?", ("0" * 64, identifier)
        )
        c.commit()
    before = snapshot(kernel)
    with pytest.raises(IntegrityError):
        engine.fail(identifier, "operator_abort", expected_version=version)
    assert snapshot(kernel) == before


def test_generic_pause_cannot_bypass_sleep_terminal_protocol(kernel: SubjectKernel) -> None:
    engine, identifier = at_phase(kernel, "reflective_sleep")
    with pytest.raises(InvalidTransitionError, match="sleep failure protocol"):
        kernel.lifecycle.transition("paused", "Bypass attempt")
    assert engine.get(identifier).status == "reflective_sleep"


def test_failed_run_survives_real_restart_and_clock_normalization(kernel: SubjectKernel) -> None:
    engine, identifier = at_phase(kernel, "reflective_sleep")
    version = engine.get(identifier).version
    engine.clock = lambda: "2026-11-06T16:00:00.123456+08:00"
    result = engine.fail(identifier, "operator_abort", expected_version=version)
    assert result.completed_at == "2026-11-06T08:00:00.123+00:00"
    path, subject = kernel.database.path, kernel.subject_id
    kernel.close()
    with SubjectKernel(path, subject, content_hash("c2")) as reopened:
        assert reopened.boot().state == "paused"
        SleepIntegrity(reopened.database).verify(subject)
        EventStore(reopened.database).verify_chain(subject)
        assert SleepEngine(reopened.database, subject).get(identifier) == result
