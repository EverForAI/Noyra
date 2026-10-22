from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from noyra.cognitive_wait_maintenance import inspect_waits, preview_connection, repair_waits
from noyra.core import SubjectKernel
from noyra.core.cognitive_waits import (
    cancel_terminal_waits,
    terminal_wait_reason,
    validate_wait,
    wait_counts,
)
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash, utc_now
from noyra.model import (
    BudgetLimits,
    CognitiveResourceStore,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelMessage,
    RoutedModelGateway,
)
from noyra.model.errors import ProviderCallError
from noyra.sleep import SleepEngine, SleepIntegrity, SleepReflectionPlan
from noyra.world import FetchedDocument, ObservationStore, SourceRegistry


class _Output(BaseModel):
    ok: bool


class Environment:
    def __init__(self, root: Path) -> None:
        self.subject = "Noyra-terminal-waits"
        self.path = root / "noyra.sqlite3"
        self.kernel = SubjectKernel(self.path, self.subject, content_hash({"seed": "waits"}))
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.db = self.kernel.database
        self.resources = CognitiveResourceStore(self.db, root / "secrets")
        self.gateway = RoutedModelGateway(self.db, self.subject, self.resources)
        self.sleep = SleepEngine(self.db, self.subject)
        self.store = ObservationStore(self.db)
        self.source = SourceRegistry(self.db).register(
            self.subject, "Local fixture", "https://example.com/news", "news", status="active"
        )
        self.counter = 0

    def observation(self) -> str:
        self.counter += 1
        text = f"Fixture {self.counter}"
        document = FetchedDocument(
            self.source.url,
            "Fixture",
            text,
            content_hash(text),
            "text/html",
            (),
            None,
            None,
            utc_now(),
        )
        return self.store.record(self.subject, self.source.source_id, document)[0].observation_id

    def wait(self, purpose: str, pool: str = "economy") -> None:
        self.gateway._record_unavailable(pool, purpose, "all_pool_resources_unavailable")

    def legacy_wait(self, *, pools: tuple[str, ...] = ("economy",)) -> str:
        purpose = f"world_cognition:{self.observation()}"
        for pool in pools:
            self.wait(purpose, pool)
        # Reproduce the old release's missing terminal hook, retaining valid
        # parent and wait histories. Never disable integrity checks in production.
        with patch("noyra.world.store.cancel_terminal_waits", return_value=0):
            self.store.mark(purpose.split(":", 1)[1], "rejected", subject_id=self.subject)
        return purpose

    def preview(self, **kwargs: Any) -> dict[str, Any]:
        with preview_connection(self.path) as connection:
            return inspect_waits(connection, self.path, self.subject, **kwargs)

    def rows(self, table: str) -> list[tuple[Any, ...]]:
        assert table in {
            "waiting_cognitive_tasks",
            "waiting_cognitive_task_revisions",
            "model_calls",
            "model_attempts",
            "events",
            "observations",
            "observation_status_transitions",
        }
        with self.db.connection() as connection:
            return [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]

    def check_wait_integrity(self) -> None:
        self.resources.verify_routing_integrity(self.subject)
        with self.db.connection() as connection:
            for row in connection.execute("SELECT * FROM waiting_cognitive_tasks"):
                validate_wait(connection, row)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Environment]:
    value = Environment(tmp_path)
    try:
        yield value
    finally:
        value.kernel.close()


@pytest.mark.parametrize("status", ["analyzed", "rejected"])
def test_observation_terminal_hook_is_atomic_and_does_not_reopen(
    env: Environment, status: str
) -> None:
    observation = env.observation()
    purpose = f"world_cognition:{observation}"
    env.wait(purpose)
    env.wait("world_cognition:missing")
    env.store.mark(observation, status, subject_id=env.subject)
    env.wait(purpose)
    env.gateway._record_unavailable("economy", purpose, "new_failure", force=True)
    with env.db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM waiting_cognitive_tasks WHERE purpose=?", (purpose,)
        ).fetchone()
        assert row["status"] == "cancelled"
        assert row["reason_code"] == f"parent_observation_{status}"
        assert row["retry_count"] == 1
        assert wait_counts(connection, env.subject, "economy") == {
            "pending_wait_count": 1,
            "recorded_wait_count": 1,
            "terminal_parent_wait_count": 0,
            "missing_parent_wait_count": 1,
            "cancelled_wait_count": 1,
        }
    env.check_wait_integrity()


def test_sleep_fallback_commit_closes_wait_before_deep_sleep(env: Environment) -> None:
    run = env.sleep.start("subject_choice", "local fixture")
    env.sleep.begin_reflection(run.sleep_id)
    purpose = f"sleep_reflection:{run.sleep_id}"
    env.wait(purpose, "deep")
    env.sleep.commit_reflection(run.sleep_id, SleepReflectionPlan(summary="bounded fallback"))
    env.wait(purpose, "deep")
    with env.db.connection() as connection:
        assert (
            terminal_wait_reason(connection, env.subject, purpose) == "sleep_reflection_committed"
        )
        assert (
            connection.execute("SELECT status FROM waiting_cognitive_tasks").fetchone()[0]
            == "cancelled"
        )
    SleepIntegrity(env.db).verify(env.subject)
    env.check_wait_integrity()


@pytest.mark.parametrize("failed", [False, True])
def test_sleep_terminal_hook_covers_legacy_and_failed_runs(env: Environment, failed: bool) -> None:
    run = env.sleep.start("subject_choice", "local fixture")
    env.sleep.begin_reflection(run.sleep_id)
    env.wait(f"sleep_reflection:{run.sleep_id}", "deep")
    if failed:
        with env.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM sleep_runs WHERE sleep_id=?", (run.sleep_id,)
            ).fetchone()
            env.sleep._transition_run(connection, row, "failed", "fixture failure")
    else:
        with patch("noyra.sleep.engine.cancel_terminal_waits", return_value=0):
            env.sleep.commit_reflection(
                run.sleep_id, SleepReflectionPlan(summary="legacy fallback")
            )
        env.sleep.enter_deep_sleep(run.sleep_id)
        env.sleep.wake_early(run.sleep_id, "fixture explicit wake")
        env.sleep.complete_wake(run.sleep_id)
    with env.db.connection() as connection:
        row = connection.execute("SELECT * FROM waiting_cognitive_tasks").fetchone()
        assert row["status"] == "cancelled"
        assert row["reason_code"] == ("parent_sleep_failed" if failed else "parent_sleep_complete")
    env.check_wait_integrity()


def test_terminal_parent_never_creates_first_wait(env: Environment) -> None:
    observation = env.observation()
    env.store.mark(observation, "rejected", subject_id=env.subject)
    env.wait(f"world_cognition:{observation}")
    assert env.rows("waiting_cognitive_tasks") == []


def test_missing_parent_and_foreign_subject_are_not_cancelled(env: Environment) -> None:
    purpose = f"world_cognition:{env.observation()}"
    env.wait(purpose)
    env.wait("sleep_reflection:missing", "deep")
    with env.db.transaction() as connection:
        assert cancel_terminal_waits(connection, "another-subject", purpose, now=utc_now()) == 0
        assert cancel_terminal_waits(connection, env.subject, purpose, now=utc_now()) == 0
        assert (
            cancel_terminal_waits(
                connection, env.subject, "sleep_reflection:missing", now=utc_now()
            )
            == 0
        )
    assert {row[4] for row in env.rows("waiting_cognitive_tasks")} == {"waiting"}
    assert env.preview()["eligible"] == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_unknown_attempt_and_reserved_budget_are_not_reconciled(
    env: Environment, legacy: bool
) -> None:
    purpose = f"world_cognition:{env.observation()}"
    ledger = ModelLedger(env.db)
    limits = BudgetLimits(
        daily_attempts=10,
        daily_input_tokens=10000,
        daily_output_tokens=10000,
        daily_cost_microusd=1000000,
    )
    provider = FakeProvider(
        [ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True)]
    )
    gateway = ModelGateway(provider, ledger, model="fixture-model", limits=limits)
    with pytest.raises(ProviderCallError):
        asyncio.run(
            gateway.complete_structured(
                env.subject,
                purpose,
                [ModelMessage(role="user", content="fixture")],
                _Output,
                idempotency_key="unknown-fixture",
                max_output_tokens=64,
            )
        )
    env.wait(purpose)
    before = {table: env.rows(table) for table in ("model_calls", "model_attempts")}
    budget = ledger.budget_status(env.subject, limits)
    if legacy:
        with patch("noyra.world.store.cancel_terminal_waits", return_value=0):
            env.store.mark(purpose.split(":", 1)[1], "rejected", subject_id=env.subject)
        plan = env.preview()
        repair_waits(
            env.path, env.subject, expected_plan=plan["plan_hash"], reason="unknown fixture"
        )
    else:
        env.store.mark(purpose.split(":", 1)[1], "rejected", subject_id=env.subject)
    assert {table: env.rows(table) for table in before} == before
    assert ledger.budget_status(env.subject, limits) == budget
    assert env.rows("model_calls")[0][7] == "unknown"
    env.check_wait_integrity()


def test_preview_is_readonly_and_wal_aware(env: Environment) -> None:
    env.legacy_wait()
    before = env.rows("waiting_cognitive_tasks")
    with preview_connection(env.path) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM waiting_cognitive_tasks")
    plan = env.preview()
    assert plan["mode"] == "preview" and plan["eligible"] == 1
    assert env.rows("waiting_cognitive_tasks") == before
    assert env.rows("model_calls") == []


def test_repair_is_audited_scoped_and_idempotent(env: Environment) -> None:
    env.legacy_wait()
    env.wait(f"world_cognition:{env.observation()}")
    env.wait("world_cognition:missing")
    plan = env.preview()
    result = repair_waits(
        env.path, env.subject, expected_plan=plan["plan_hash"], reason="reviewed fixture"
    )
    assert result["cancelled"] == 1 and result["event_id"]
    assert {row[4] for row in env.rows("waiting_cognitive_tasks")} == {"waiting", "cancelled"}
    before = env.rows("waiting_cognitive_task_revisions")
    plan = env.preview()
    second = repair_waits(
        env.path, env.subject, expected_plan=plan["plan_hash"], reason="repeat fixture"
    )
    assert second["cancelled"] == 0 and second["event_id"] is None
    assert env.rows("waiting_cognitive_task_revisions") == before
    env.check_wait_integrity()


def test_batch_does_not_cancel_same_purpose_outside_preview(env: Environment) -> None:
    env.legacy_wait(pools=("economy", "deep"))
    plan = env.preview(limit=1)
    assert plan["eligible"] == 1 and plan["next_cursor"]
    result = repair_waits(
        env.path, env.subject, expected_plan=plan["plan_hash"], reason="batch fixture", limit=1
    )
    assert result["cancelled"] == 1
    assert sum(row[4] == "waiting" for row in env.rows("waiting_cognitive_tasks")) == 1
    remaining = env.preview(limit=1, after_task_id=plan["next_cursor"])
    assert remaining["eligible"] == 1
    repair_waits(
        env.path,
        env.subject,
        expected_plan=remaining["plan_hash"],
        reason="next fixture",
        limit=1,
        after_task_id=plan["next_cursor"],
    )
    env.check_wait_integrity()


def test_stale_preview_refuses_every_write(env: Environment) -> None:
    env.legacy_wait()
    plan = env.preview()
    env.wait("world_cognition:new-after-preview")
    before = env.rows("waiting_cognitive_tasks")
    with pytest.raises(ValueError, match="preview_stale"):
        repair_waits(env.path, env.subject, expected_plan=plan["plan_hash"], reason="stale fixture")
    assert env.rows("waiting_cognitive_tasks") == before


def test_corrupt_wait_refuses_parent_terminal_transaction(env: Environment) -> None:
    observation = env.observation()
    env.wait(f"world_cognition:{observation}")
    with env.db.transaction() as connection:
        connection.execute("UPDATE waiting_cognitive_tasks SET state_hash='bad'")
    before = env.rows("observation_status_transitions")
    with pytest.raises(IntegrityError):
        env.store.mark(observation, "rejected", subject_id=env.subject)
    assert env.store.get(observation, subject_id=env.subject).status == "new"
    assert env.rows("observation_status_transitions") == before


def test_corrupt_history_refuses_preview(env: Environment) -> None:
    env.legacy_wait()
    with env.db.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM waiting_cognitive_task_revisions LIMIT 1"
        ).fetchone()
        connection.execute(
            "INSERT INTO waiting_cognitive_task_revisions VALUES (?,?,?,?,?,?,?,?)",
            (
                "bad-revision",
                row["task_id"],
                "waiting",
                row["reason_code"],
                2,
                row["next_retry_at"],
                "bad",
                utc_now(),
            ),
        )
    with pytest.raises(IntegrityError):
        env.preview()


def test_concurrent_late_failure_does_not_resurrect_wait(env: Environment) -> None:
    observation = env.observation()
    purpose = f"world_cognition:{observation}"
    env.wait(purpose)
    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(env.wait, purpose)
        env.store.mark(observation, "rejected", subject_id=env.subject)
        future.result(timeout=5)
    env.wait(purpose)
    assert {row[4] for row in env.rows("waiting_cognitive_tasks")} == {"cancelled"}
    env.check_wait_integrity()


def test_apply_lock_wait_is_bounded(env: Environment) -> None:
    env.legacy_wait()
    plan = env.preview()
    blocker = sqlite3.connect(env.path, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            repair_waits(
                env.path, env.subject, expected_plan=plan["plan_hash"], reason="locked fixture"
            )
        assert time.monotonic() - started < 2
    finally:
        blocker.close()


def test_cli_defaults_to_preview_and_never_creates_database(
    env: Environment, tmp_path: Path
) -> None:
    env.legacy_wait()
    command = [
        sys.executable,
        "-m",
        "noyra.cognitive_wait_maintenance",
        "--subject-id",
        env.subject,
    ]
    result = subprocess.run(
        [*command, "--database", str(env.path)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "preview"
    missing = tmp_path / "missing.sqlite3"
    refused = subprocess.run(
        [*command, "--database", str(missing)], capture_output=True, text=True, timeout=20
    )
    assert refused.returncode == 2 and not missing.exists()


def test_bare_parent_status_edit_is_not_terminal_evidence_for_repair(env: Environment) -> None:
    observation = env.observation()
    env.wait(f"world_cognition:{observation}")
    with env.db.transaction() as connection:
        connection.execute(
            "UPDATE observations SET status='rejected' WHERE observation_id=?", (observation,)
        )
    before = env.rows("waiting_cognitive_tasks")
    with pytest.raises(IntegrityError):
        env.preview()
    assert env.rows("waiting_cognitive_tasks") == before


def test_apply_requires_the_preview_hash_and_operator_reason(env: Environment) -> None:
    env.legacy_wait()
    for expected, reason in [("", "fixture"), ("a" * 64, "")]:
        with pytest.raises(ValueError):
            repair_waits(env.path, env.subject, expected_plan=expected, reason=reason)
    assert {row[4] for row in env.rows("waiting_cognitive_tasks")} == {"waiting"}


@pytest.mark.parametrize("stage", ["committed", "complete", "failed"])
def test_legacy_sleep_repair_validates_parent_and_preserves_sleep_state(
    env: Environment, stage: str
) -> None:
    run = env.sleep.start("subject_choice", "legacy fixture")
    env.sleep.begin_reflection(run.sleep_id)
    env.wait(f"sleep_reflection:{run.sleep_id}", "deep")
    with patch("noyra.sleep.engine.cancel_terminal_waits", return_value=0):
        if stage == "failed":
            with env.db.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM sleep_runs WHERE sleep_id=?", (run.sleep_id,)
                ).fetchone()
                env.sleep._transition_run(connection, row, "failed", "legacy failure")
        else:
            env.sleep.commit_reflection(
                run.sleep_id, SleepReflectionPlan(summary="legacy fallback")
            )
            if stage == "complete":
                env.sleep.enter_deep_sleep(run.sleep_id)
                env.sleep.wake_early(run.sleep_id, "fixture explicit wake")
    before = env.sleep.get(run.sleep_id)
    plan = env.preview()
    assert plan["eligible"] == 1
    repair_waits(
        env.path, env.subject, expected_plan=plan["plan_hash"], reason="legacy sleep review"
    )
    assert env.sleep.get(run.sleep_id) == before
    assert {row[4] for row in env.rows("waiting_cognitive_tasks")} == {"cancelled"}
    env.check_wait_integrity()


def test_maintenance_audit_failure_rolls_back_the_entire_batch(env: Environment) -> None:
    env.legacy_wait()
    env.legacy_wait()
    plan = env.preview()
    before = {
        table: env.rows(table)
        for table in (
            "waiting_cognitive_tasks",
            "waiting_cognitive_task_revisions",
            "events",
        )
    }
    with (
        patch(
            "noyra.cognitive_wait_maintenance.EventStore._append_connection",
            side_effect=IntegrityError("fixture audit refusal"),
        ),
        pytest.raises(IntegrityError),
    ):
        repair_waits(env.path, env.subject, expected_plan=plan["plan_hash"], reason="audit fixture")
    assert {table: env.rows(table) for table in before} == before
    env.check_wait_integrity()


def test_preview_sees_committed_wal_not_an_immutable_main_file(env: Environment) -> None:
    reader = sqlite3.connect(env.path, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM waiting_cognitive_tasks").fetchone()
        env.legacy_wait()
        assert env.path.with_name(env.path.name + "-wal").stat().st_size > 0
        assert env.preview()["eligible"] == 1
    finally:
        reader.close()
