from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core.runtime_observation import (
    ATTEMPT_LIMIT,
    CALL_LIMIT,
    ObservationBusy,
    RuntimeObserver,
    autonomy_progress_observation,
    loop_observation,
    resource_observation,
)
from noyra.core.types import content_hash
from noyra.model.ledger import ModelLedger
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.sleep import FatigueInputs, FatigueTracker, SleepEngine, SleepReflectionPlan

NOW = datetime(2026, 10, 22, tzinfo=UTC)


@pytest.mark.parametrize(
    ("lifecycle", "circuit", "age", "expected"),
    [
        ("active", "closed", 5, "recent_success"),
        ("active", "closed", 181, "tick_not_recent"),
        ("active", "closed", -5, "insufficient_evidence"),
        ("active", "closed", None, "insufficient_evidence"),
        ("active", "open", None, "cooling_down"),
        ("active", "half_open", 5, "recovering"),
        ("paused", "open", 900, "paused"),
        ("deep_sleep", "closed", 900, "resting"),
        ("waking", "closed", 900, "resting"),
        ("stopped", "closed", 5, "stopped"),
        ("booting", "closed", 5, "insufficient_evidence"),
    ],
)
def test_loop_explanations_never_infer_failure_from_sleep_or_absent_evidence(
    lifecycle: str, circuit: str, age: int | None, expected: str
) -> None:
    row = {
        "circuit_status": circuit,
        "consecutive_failures": 2,
        "last_successful_tick_at": None
        if age is None
        else (NOW - timedelta(seconds=age)).isoformat(),
        "next_retry_at": (NOW + timedelta(seconds=30)).isoformat(),
    }
    result = loop_observation(row, lifecycle, now=NOW, stale_after=180)
    assert result["reason_code"] == expected
    assert result["next_retry_at"] == row["next_retry_at"]
    assert (
        result["last_success_age_seconds"] is None
        if age is None or age < 0
        else result["last_success_age_seconds"] == age
    )


@pytest.mark.parametrize(
    ("lifecycle", "loop_reason", "sleep", "capacity", "quarantined", "block", "expected"),
    [
        (
            "active",
            "recent_success",
            {"available": False},
            {"available": True, "blocked": False},
            False,
            None,
            "running",
        ),
        (
            "deep_sleep",
            "resting",
            {"available": True, "status": "deep_sleep", "trigger_type": "fatigue"},
            {"available": True, "blocked": False},
            False,
            None,
            "planned_sleep",
        ),
        (
            "deep_sleep",
            "resting",
            {
                "available": True,
                "status": "deep_sleep",
                "trigger_type": "budget",
                "wake_after": "2026-10-22T01:00:00+00:00",
            },
            {"available": True, "blocked": False},
            False,
            None,
            "budget_wait",
        ),
        (
            "paused",
            "paused",
            {"available": False},
            {"available": True, "blocked": False},
            False,
            None,
            "manual_pause",
        ),
        (
            "paused",
            "paused",
            {"available": False},
            {"available": True, "blocked": False},
            True,
            None,
            "safety_quarantine",
        ),
        (
            "active",
            "recent_success",
            {"available": False},
            {
                "available": True,
                "blocked": True,
                "blocks_embedding": True,
                "blocks_cognition": False,
                "blocking_tables": ["embedding_usage_entries"],
            },
            False,
            None,
            "running",
        ),
        (
            "active",
            "recent_success",
            {"available": False},
            {"available": True, "blocked": True, "blocking_tables": ["model_calls"]},
            False,
            None,
            "evidence_capacity",
        ),
        (
            "active",
            "recent_success",
            {"available": False},
            {"available": True, "blocked": False},
            False,
            {"reason_code": "storage_pressure", "observed_at": NOW.isoformat()},
            "storage_pressure",
        ),
        (
            "active",
            "cooling_down",
            {"available": False},
            {"available": True, "blocked": False},
            False,
            None,
            "loop_cooldown",
        ),
        (
            "active",
            "tick_not_recent",
            {"available": False},
            {"available": True, "blocked": False},
            False,
            None,
            "tick_stalled",
        ),
        (
            "active",
            "recent_success",
            {"available": False},
            {"available": True, "blocked": False},
            False,
            None,
            "cognition_disabled",
        ),
        (
            "active",
            "recent_success",
            {"available": False},
            {"available": False},
            False,
            None,
            "evidence_capacity_unavailable",
        ),
    ],
)
def test_autonomy_progress_distinguishes_planned_waits_from_blockers(
    lifecycle: str,
    loop_reason: str,
    sleep: dict[str, Any],
    capacity: dict[str, Any],
    quarantined: bool,
    block: dict[str, Any] | None,
    expected: str,
) -> None:
    observation = {
        "database_observation": "ok",
        "lifecycle": lifecycle,
        "cognition": {
            "enabled": loop_reason != "recent_success" or expected != "cognition_disabled"
        },
        "loop": {
            "reason_code": loop_reason,
            "last_successful_tick_at": NOW.isoformat(),
            "last_success_age_seconds": 5,
            "last_tick_started_age_seconds": 500,
            "consecutive_failures": 0,
            "next_retry_at": (NOW + timedelta(minutes=2)).isoformat(),
        },
        "sleep": sleep,
        "evidence_capacity": capacity,
        "resources": {"data_free_bytes": 10_000_000_000},
    }
    result = autonomy_progress_observation(
        observation,
        now=NOW,
        minimum_free_bytes=500_000_000,
        safety_quarantined=quarantined,
        block_state=block,
    )
    assert result["reason_code"] == expected
    assert result["observed_at"] == NOW.isoformat()
    if capacity.get("blocks_embedding"):
        assert result["evidence_capacity"]["blocks_embedding"] is True
        assert "evidence_capacity" not in result["blockers"]
    if expected in {"planned_sleep", "budget_wait"}:
        assert result["status"] in {"planned_sleep", "waiting"}
        assert "tick_stalled" not in result["blockers"]


@pytest.mark.parametrize(
    "observed_at",
    [
        NOW - timedelta(seconds=181),
        NOW + timedelta(seconds=1),
    ],
)
def test_expired_or_future_storage_block_signal_is_not_carried_forward(
    observed_at: datetime,
) -> None:
    observation = {
        "database_observation": "ok",
        "lifecycle": "active",
        "cognition": {"enabled": True},
        "loop": {
            "reason_code": "recent_success",
            "last_successful_tick_at": NOW.isoformat(),
            "last_success_age_seconds": 5,
        },
        "sleep": {"available": False},
        "evidence_capacity": {"available": True, "blocked": False},
        "resources": {"data_free_bytes": 10_000_000_000},
    }

    result = autonomy_progress_observation(
        observation,
        now=NOW,
        minimum_free_bytes=500_000_000,
        block_state={
            "reason_code": "storage_pressure",
            "observed_at": observed_at.isoformat(),
        },
        stale_after=180,
    )

    assert result["reason_code"] == "running"
    assert "storage_pressure" not in result["blockers"]


def test_autonomy_progress_keeps_sleep_admission_closed_and_reports_migration_fence() -> None:
    observation = {
        "database_observation": "ok",
        "lifecycle": "deep_sleep",
        "cognition": {"enabled": True},
        "loop": {"reason_code": "resting"},
        "sleep": {"available": True, "status": "deep_sleep", "trigger_type": "fatigue"},
        "evidence_capacity": {"available": True, "blocked": False},
        "resources": {"data_free_bytes": 10_000_000_000},
    }
    sleeping = autonomy_progress_observation(
        observation,
        now=NOW,
        minimum_free_bytes=500_000_000,
        admission_accepting=False,
    )
    assert sleeping["reason_code"] == "planned_sleep"
    assert sleeping["blockers"] == []

    migrating = autonomy_progress_observation(
        {**observation, "lifecycle": "active"},
        now=NOW,
        minimum_free_bytes=500_000_000,
        migration_fenced=True,
        admission_accepting=False,
    )
    assert migrating["reason_code"] == "migration_fenced"
    assert migrating["blockers"] == ["migration_fenced"]


@pytest.mark.parametrize(
    "sleep",
    [
        {"available": False},
        {"available": True, "status": "waking", "trigger_type": "budget"},
    ],
)
def test_sleep_without_matching_evidence_is_not_reported_as_planned(
    sleep: dict[str, Any],
) -> None:
    observation = {
        "database_observation": "ok",
        "lifecycle": "deep_sleep",
        "cognition": {"enabled": True},
        "loop": {"reason_code": "resting"},
        "sleep": sleep,
        "evidence_capacity": {"available": True, "blocked": False},
        "resources": {"data_free_bytes": 10_000_000_000},
    }

    result = autonomy_progress_observation(
        observation,
        now=NOW,
        minimum_free_bytes=500_000_000,
    )

    assert result["reason_code"] == "sleep_evidence_unavailable"
    assert result["status"] == "unknown"
    assert result["next_action_at"] is None


def test_recent_success_from_a_previous_process_is_not_current_process_progress() -> None:
    observation = {
        "database_observation": "ok",
        "lifecycle": "active",
        "cognition": {"enabled": True},
        "loop": {
            "reason_code": "recent_success",
            "last_successful_tick_at": (NOW - timedelta(seconds=10)).isoformat(),
            "last_tick_started_at": (NOW - timedelta(seconds=11)).isoformat(),
            "last_tick_started_age_seconds": 11,
        },
        "sleep": {"available": False},
        "evidence_capacity": {"available": True, "blocked": False},
        "resources": {"data_free_bytes": 10_000_000_000},
    }
    process_start = NOW - timedelta(seconds=5)
    result = autonomy_progress_observation(
        observation,
        now=NOW,
        minimum_free_bytes=500_000_000,
        process_started_at=process_start,
    )
    assert result["reason_code"] == "starting"

    observation["loop"]["last_tick_started_at"] = (NOW - timedelta(seconds=2)).isoformat()
    observation["loop"]["last_tick_started_age_seconds"] = 2
    in_progress = autonomy_progress_observation(
        observation,
        now=NOW,
        minimum_free_bytes=500_000_000,
        process_started_at=process_start,
    )
    assert in_progress["reason_code"] == "tick_unconfirmed"


def _database(path: Path, *, calls: int = 1, attempts: int = 1) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE actions(subject_id TEXT,status TEXT);
            CREATE TABLE wallet_payment_executions(subject_id TEXT,status TEXT);
            CREATE TABLE interaction_deliveries(
              delivery_id TEXT PRIMARY KEY,subject_id TEXT,status TEXT);
            CREATE TABLE interaction_delivery_reconciliations(
              reconciliation_id TEXT PRIMARY KEY,delivery_id TEXT,subject_id TEXT,sequence INTEGER,
              source TEXT,outcome TEXT,provider_message_id TEXT,provider_status TEXT,
              evidence_json TEXT,evidence_hash TEXT,actor TEXT,reason TEXT,state_hash TEXT,
              created_at TEXT);
            CREATE TABLE runtime_state(subject_id TEXT PRIMARY KEY, state TEXT);
            CREATE TABLE autonomy_loop_state(subject_id TEXT PRIMARY KEY, circuit_status TEXT,
              consecutive_failures INTEGER,last_successful_tick_at TEXT,next_retry_at TEXT);
            CREATE TABLE model_calls(call_id TEXT PRIMARY KEY,subject_id TEXT,status TEXT,
              usage_estimated INTEGER,created_at TEXT,completed_at TEXT,response_json TEXT,
              resource_group_id TEXT,resource_pool TEXT DEFAULT 'economy',
              model TEXT DEFAULT 'fixture-model',purpose TEXT DEFAULT 'fixture',error_code TEXT);
            CREATE TABLE waiting_cognitive_tasks(
              task_id TEXT PRIMARY KEY,subject_id TEXT,purpose TEXT,status TEXT);
            CREATE TABLE sleep_runs(
              sleep_id TEXT PRIMARY KEY,subject_id TEXT,status TEXT,reflection_event_id TEXT);
            CREATE TABLE observations(
              observation_id TEXT PRIMARY KEY,subject_id TEXT,status TEXT);
            CREATE INDEX calls_time ON model_calls(subject_id,created_at,call_id);
            CREATE TABLE model_attempts(call_id TEXT,subject_id TEXT,attempt_number INTEGER,
              status TEXT,input_tokens INTEGER,output_tokens INTEGER,cost_microusd INTEGER,
              private_payload TEXT, UNIQUE(call_id,attempt_number));
        """)
        connection.execute("INSERT INTO runtime_state VALUES ('subject','active')")
        connection.execute(
            "INSERT INTO autonomy_loop_state VALUES ('subject','closed',0,?,NULL)",
            (datetime.now(UTC).isoformat(),),
        )
        for i in range(calls):
            connection.execute(
                "INSERT INTO model_calls(call_id,subject_id,status,usage_estimated,created_at,"
                "completed_at,response_json) VALUES (?,?,?,?,?,?,?)",
                (
                    f"call-{i:04}",
                    "subject",
                    "succeeded",
                    0,
                    (NOW + timedelta(seconds=i)).isoformat(),
                    (NOW + timedelta(seconds=i + 10)).isoformat(),
                    "DO_NOT_EXPOSE_RESPONSE",
                ),
            )
        for i in range(attempts):
            connection.execute(
                "INSERT INTO model_attempts VALUES (?,?,?,?,?,?,?,?)",
                (
                    "call-0000",
                    "subject",
                    i + 1,
                    "succeeded",
                    10,
                    20,
                    9_007_199_254_740_993 if i == 0 else None,
                    "DO_NOT_EXPOSE_CREDENTIAL",
                ),
            )
        connection.execute(
            "INSERT INTO model_calls(call_id,subject_id,status,usage_estimated,created_at,"
            "completed_at,response_json) VALUES (?,?,?,?,?,?,?)",
            ("other", "another-subject", "unknown", 0, NOW.isoformat(), None, "OTHER_SUBJECT"),
        )


def test_samples_are_exact_subject_scoped_and_never_include_payloads(tmp_path: Path) -> None:
    database = tmp_path / "noyra.sqlite3"
    _database(database, calls=2, attempts=2)
    observer = RuntimeObserver(database, tmp_path, "subject")
    result = observer.snapshot(cognition_enabled=True, interval_seconds=30)
    sample = result["model_sample"]
    assert result["database_observation"] == "ok"
    assert sample["calls"] == 2 and sample["attempts"] == 2
    assert sample["unknown_calls"] == 0
    assert sample["known_cost_microusd"] == "9007199254740993"
    assert sample["missing_cost_attempts"] == 1
    assert sample["call_elapsed_ms_p50"] == sample["call_elapsed_ms_p95"] == 10_000
    assert sample["scope"] == "latest_calls_not_daily_total"
    serialized = json.dumps(result)
    for private in ("DO_NOT_EXPOSE", "OTHER_SUBJECT", "call-0000", "another-subject"):
        assert private not in serialized
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 3


def test_runtime_observation_reads_evidence_capacity_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "1")
    subject = "runtime-observation-capacity"
    database = Database(tmp_path / "capacity.sqlite3")
    IdentityStore(database).ensure(subject, content_hash(subject))
    ModelLedger(database).prepare_call(
        subject, "provider", "model", "fixture", content_hash("request"), "one"
    )
    observation = RuntimeObserver(database.path, tmp_path, subject).snapshot(
        cognition_enabled=True, interval_seconds=30
    )

    capacity = observation["evidence_capacity"]
    assert observation["database_observation"] == "ok"
    assert capacity["available"] is True
    assert capacity["blocked"] is True
    assert "model_calls" in capacity["blocking_tables"]
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM model_calls WHERE subject_id=?", (subject,)
            ).fetchone()[0]
            == 1
        )


def test_row_caps_and_truncation_are_explicit(tmp_path: Path) -> None:
    database = tmp_path / "noyra.sqlite3"
    _database(database, calls=CALL_LIMIT + 1, attempts=ATTEMPT_LIMIT + 1)
    # Put attempts on the latest call so they are included in the call sample.
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE model_attempts SET call_id=?", (f"call-{CALL_LIMIT:04}",))
    result = RuntimeObserver(database, tmp_path, "subject").snapshot(
        cognition_enabled=True, interval_seconds=30
    )
    sample = result["model_sample"]
    assert sample["calls"] == CALL_LIMIT and sample["calls_truncated"]
    assert sample["attempts"] == ATTEMPT_LIMIT and sample["attempts_truncated"]


def test_cache_coalesces_and_does_not_expose_mutable_shared_results(tmp_path: Path) -> None:
    database = tmp_path / "noyra.sqlite3"
    _database(database)
    observer = RuntimeObserver(database, tmp_path, "subject")
    with patch.object(observer, "_collect", wraps=observer._collect) as collect:
        first = observer.snapshot(cognition_enabled=True, interval_seconds=30)
        first["model_sample"]["calls"] = -1
        second = observer.snapshot(cognition_enabled=True, interval_seconds=30)
    assert collect.call_count == 1
    assert second["model_sample"]["calls"] == 1
    with patch("noyra.core.runtime_observation.CACHE_SECONDS", 0):
        third = observer.snapshot(cognition_enabled=False, interval_seconds=30)
    assert third["cognition"]["enabled"] is False


def test_busy_observation_fails_with_bounded_wait(tmp_path: Path) -> None:
    observer = RuntimeObserver(tmp_path / "missing.sqlite3", tmp_path, "subject")
    observer._lock.acquire()
    try:
        begin = time.monotonic()
        with pytest.raises(ObservationBusy):
            observer.snapshot(cognition_enabled=True, interval_seconds=30)
        assert time.monotonic() - begin < 1
    finally:
        observer._lock.release()


def test_missing_locked_and_query_limited_databases_are_unknown_not_zero(tmp_path: Path) -> None:
    database = tmp_path / "noyra.sqlite3"
    observer = RuntimeObserver(database, tmp_path, "subject")
    missing = observer.snapshot(cognition_enabled=True, interval_seconds=30)
    assert missing["database_observation"] == "unavailable"
    assert missing["model_sample"] is None and not database.exists()
    _database(database, calls=CALL_LIMIT + 1)
    with patch("noyra.core.runtime_observation.CACHE_SECONDS", 0):
        with sqlite3.connect(database) as connection:
            connection.execute("BEGIN EXCLUSIVE")
            begin = time.monotonic()
            locked = observer.snapshot(cognition_enabled=True, interval_seconds=30)
            assert time.monotonic() - begin < 1
            assert locked["model_sample"] is None
        with patch("noyra.core.runtime_observation.QUERY_SECONDS", -1):
            limited = observer.snapshot(cognition_enabled=True, interval_seconds=30)
            assert limited["database_observation"] == "unavailable"
            assert limited["model_sample"] is None


def test_unavailable_resource_values_are_not_reported_as_healthy_zero(tmp_path: Path) -> None:
    with patch("noyra.core.runtime_observation.shutil.disk_usage", side_effect=OSError):
        result = resource_observation(tmp_path, tmp_path / "missing.sqlite3")
    assert result["data_free_bytes"] is None
    assert result["database_bytes"] is None


def test_summary_authentication_and_no_expensive_or_external_probes(tmp_path: Path) -> None:
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", "observation-http", content_hash("observe"))
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id=kernel.subject_id,
        genesis_hash=content_hash("observe"),
        host="127.0.0.1",
        port=0,
        operator_token=SecretStr("observation-operator-token-long-enough"),
        read_token=SecretStr("observation-read-token-with-sufficient-length"),
    )
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    try:
        server.start()
        url = f"http://127.0.0.1:{server.address[1]}/api/v1/admin/runtime-summary"
        with pytest.raises(HTTPError) as denied:
            urlopen(url, timeout=5)
        assert denied.value.code == 401
        denied.value.close()
        read_headers = {"Authorization": "Bearer observation-read-token-with-sufficient-length"}
        with pytest.raises(HTTPError) as read_denied:
            urlopen(Request(url, headers=read_headers), timeout=5)
        assert read_denied.value.code == 401
        read_denied.value.close()
        headers = {"Authorization": "Bearer observation-operator-token-long-enough"}
        server.autonomy_block_state = {
            "reason_code": "storage_pressure",
            "observed_at": datetime.now(UTC).isoformat(),
            "free_bytes": 1,
            "over_quota": [],
        }
        with (
            patch.object(server, "diagnostics", side_effect=AssertionError("heavy diagnostics")),
            patch.object(server, "_current_storage", side_effect=AssertionError("storage scan")),
            urlopen(Request(url, headers=headers), timeout=5) as response,
        ):
            body: dict[str, Any] = json.load(response)
        assert body["format"] == "noyra-runtime-observation/v1"
        assert body["database_observation"] == "ok"
        assert body["cognition"]["enabled"] is False
        assert body["autonomy"]["status"] == "blocked"
        assert body["autonomy"]["reason_code"] == "storage_pressure"
        assert body["autonomy"]["blockers"] == ["storage_pressure"]
        assert "observation-operator-token-long-enough" not in json.dumps(body)
        with (
            patch.object(server, "cognition_is_enabled", return_value=True),
            patch("noyra.core.runtime_observation.CACHE_SECONDS", 0),
            patch.object(server, "diagnostics", side_effect=AssertionError("heavy diagnostics")),
            patch.object(server, "_current_storage", side_effect=AssertionError("storage scan")),
            urlopen(Request(url, headers=headers), timeout=5) as enabled_response,
        ):
            assert json.load(enabled_response)["cognition"]["enabled"] is True
        with urlopen(
            Request(url.replace("/api/v1/", "/api/"), headers=headers), timeout=5
        ) as alias:
            assert json.load(alias)["format"] == body["format"]
        server.runtime_observer._lock.acquire()
        try:
            with pytest.raises(HTTPError) as busy:
                urlopen(Request(url, headers=headers), timeout=5)
            assert busy.value.code == 503
            busy.value.close()
        finally:
            server.runtime_observer._lock.release()
    finally:
        server.close()
        kernel.close()


def test_admin_runtime_summary_matches_isolated_persisted_lifecycle_read_only(
    tmp_path: Path,
) -> None:
    subject = "observation-lifecycle-http"
    genesis = content_hash(subject)
    kernel = SubjectKernel(tmp_path / "lifecycle.sqlite3", subject, genesis)
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id=subject,
        genesis_hash=genesis,
        host="127.0.0.1",
        port=0,
        operator_token=SecretStr("lifecycle-observation-operator-token-long-enough"),
    )
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    url = f"http://127.0.0.1:{server.address[1]}/api/v1/admin/runtime-summary"
    headers = {"Authorization": "Bearer lifecycle-observation-operator-token-long-enough"}

    def persisted_state() -> dict[str, list[dict[str, Any]]]:
        with kernel.database.read_transaction() as connection:
            return {
                table: [
                    dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
                ]
                for table in (
                    "runtime_state",
                    "autonomy_loop_state",
                    "sleep_runs",
                    "sleep_transitions",
                    "events",
                    "model_calls",
                    "model_attempts",
                    "audit_records",
                )
            }

    def observe() -> dict[str, Any]:
        with (
            patch.object(server, "cognition_is_enabled", return_value=True),
            patch("noyra.core.runtime_observation.CACHE_SECONDS", 0),
            urlopen(Request(url, headers=headers), timeout=5) as response,
        ):
            assert response.status == 200
            return json.load(response)

    try:
        server.start()
        now = datetime.now(UTC)
        with kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO autonomy_loop_state(subject_id,circuit_status,consecutive_failures,"
                "last_tick_started_at,last_successful_tick_at,updated_at) "
                "VALUES (?, 'closed', 0, ?, ?, ?)",
                (subject, now.isoformat(), now.isoformat(), now.isoformat()),
            )
        before = persisted_state()
        running = observe()
        assert running["lifecycle"] == "active"
        assert running["autonomy"]["reason_code"] == "running"
        assert persisted_state() == before

        sleep = SleepEngine(kernel.database, subject)
        run = sleep.start("subject_choice", "Synthetic observer validation")
        sleep.begin_reflection(run.sleep_id)
        sleep.commit_reflection(run.sleep_id, SleepReflectionPlan(summary="Synthetic reflection"))
        sleep.enter_deep_sleep(run.sleep_id)
        before = persisted_state()
        planned = observe()
        assert planned["lifecycle"] == "deep_sleep"
        assert planned["sleep"]["available"] is True
        assert planned["sleep"]["status"] == planned["lifecycle"]
        assert planned["autonomy"]["reason_code"] == "planned_sleep"
        assert planned["autonomy"]["blockers"] == []
        assert persisted_state() == before

        base = server.runtime_observer.snapshot(cognition_enabled=True, interval_seconds=30)
        before = persisted_state()
        with patch.object(
            server.runtime_observer,
            "snapshot",
            return_value={**base, "sleep": {"available": False}},
        ):
            missing_evidence = observe()
        assert missing_evidence["autonomy"]["reason_code"] == "sleep_evidence_unavailable"
        assert missing_evidence["autonomy"]["status"] == "unknown"
        assert persisted_state() == before

        sleep.wake(run.sleep_id, "Synthetic observer wake", force=True)
        before = persisted_state()
        waking = observe()
        assert waking["lifecycle"] == "waking"
        assert waking["sleep"]["status"] == "waking"
        assert waking["autonomy"]["reason_code"] == "waking"
        assert persisted_state() == before
        sleep.complete_wake(run.sleep_id)
        assert kernel.lifecycle.current().state == "active"

        fatigue = FatigueTracker(kernel.database)
        fatigue.assess(
            subject,
            FatigueInputs(
                resource_pressure=1.0,
                cognitive_load=0.0,
                frustration=0.0,
                goal_conflict=0.0,
                staleness=0.0,
            ),
            reason="Synthetic exhausted-budget validation",
        )
        planned_wake = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        budget_run = sleep.start("budget", "Synthetic budget wait", wake_after=planned_wake)
        sleep.begin_reflection(budget_run.sleep_id)
        sleep.commit_reflection(
            budget_run.sleep_id, SleepReflectionPlan(summary="Synthetic budget reflection")
        )
        sleep.enter_deep_sleep(budget_run.sleep_id)
        before = persisted_state()
        budget_wait = observe()
        assert budget_wait["lifecycle"] == "deep_sleep"
        assert budget_wait["sleep"]["trigger_type"] == "budget"
        assert budget_wait["autonomy"]["reason_code"] == "budget_wait"
        assert budget_wait["autonomy"]["status"] == "waiting"
        recorded_wake = sleep.get(budget_run.sleep_id).wake_after
        assert recorded_wake is not None
        assert datetime.fromisoformat(
            budget_wait["autonomy"]["next_action_at"]
        ) == datetime.fromisoformat(recorded_wake)
        assert persisted_state() == before
        sleep.wake(budget_run.sleep_id, "Synthetic budget wake", force=True)
        sleep.complete_wake(budget_run.sleep_id)
    finally:
        server.close()
        kernel.close()


def test_admin_runtime_summary_reports_capacity_and_storage_blockers_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = "observation-resource-http"
    genesis = content_hash(subject)
    kernel = SubjectKernel(tmp_path / "resources.sqlite3", subject, genesis)
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id=subject,
        genesis_hash=genesis,
        host="127.0.0.1",
        port=0,
        operator_token=SecretStr("resource-observation-operator-token-long-enough"),
    )
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    url = f"http://127.0.0.1:{server.address[1]}/api/v1/admin/runtime-summary"
    headers = {"Authorization": "Bearer resource-observation-operator-token-long-enough"}

    def persisted_state() -> dict[str, list[dict[str, Any]]]:
        with kernel.database.read_transaction() as connection:
            return {
                table: [
                    dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
                ]
                for table in (
                    "runtime_state",
                    "autonomy_loop_state",
                    "sleep_runs",
                    "sleep_transitions",
                    "events",
                    "model_calls",
                    "model_attempts",
                    "audit_records",
                )
            }

    def observe() -> dict[str, Any]:
        with (
            patch.object(server, "cognition_is_enabled", return_value=True),
            patch("noyra.core.runtime_observation.CACHE_SECONDS", 0),
            urlopen(Request(url, headers=headers), timeout=5) as response,
        ):
            assert response.status == 200
            return json.load(response)

    try:
        server.start()
        monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "5")
        ledger = ModelLedger(kernel.database)

        def admit_call(number: int) -> None:
            ledger.prepare_call(
                subject, "provider", "model", "fixture", content_hash("request"), str(number)
            )

        for number in range(3):
            admit_call(number)
        assert observe()["autonomy"]["evidence_capacity"]["core_near_limit"] == []

        admit_call(3)
        before = persisted_state()
        warning = observe()
        assert warning["autonomy"]["evidence_capacity"]["core_warning_threshold_percent"] == 80
        assert warning["autonomy"]["evidence_capacity"]["core_near_limit"] == [
            {"table": "model_calls", "count": 4, "limit": 5, "remaining": 1}
        ]
        assert warning["autonomy"]["evidence_capacity"]["blocks_cognition"] is False
        assert "evidence_capacity" not in warning["autonomy"]["blockers"]
        assert persisted_state() == before

        # The warning did not change admission. The next row can reach the limit.
        admit_call(4)
        before = persisted_state()
        capacity = observe()
        assert capacity["autonomy"]["reason_code"] == "evidence_capacity"
        assert "evidence_capacity" in capacity["autonomy"]["blockers"]
        assert capacity["autonomy"]["evidence_capacity"]["core_near_limit"] == []
        assert persisted_state() == before

        with patch(
            "noyra.core.runtime_observation.shutil.disk_usage",
            return_value=SimpleNamespace(total=1_000_000_000, used=999_000_000, free=1_000_000),
        ):
            storage = observe()
        assert storage["autonomy"]["reason_code"] == "storage_pressure"
        assert "storage_pressure" in storage["autonomy"]["blockers"]
        assert "evidence_capacity" in storage["autonomy"]["blockers"]
        assert persisted_state() == before

        with patch(
            "noyra.core.runtime_observation.evidence_capacity_status",
            side_effect=sqlite3.OperationalError("counter read failed"),
        ):
            missing_capacity = observe()["autonomy"]
        assert missing_capacity["reason_code"] == "evidence_capacity_unavailable"
        assert missing_capacity["evidence_capacity"]["core_near_limit"] is None
        assert persisted_state() == before
    finally:
        server.close()
        kernel.close()


def test_attention_does_not_hide_old_unknown_payments_or_resolved_deliveries(
    tmp_path: Path,
) -> None:
    database = tmp_path / "noyra.sqlite3"
    _database(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO wallet_payment_executions VALUES ('subject','unknown')", [()] * 51
        )
        connection.execute(
            "INSERT INTO wallet_payment_executions VALUES ('another-subject','unknown')"
        )
        connection.execute("INSERT INTO interaction_deliveries VALUES ('old','subject','unknown')")
        connection.execute(
            "INSERT INTO interaction_deliveries VALUES ('pending','subject','unknown')"
        )
        receipt = {
            "reconciliation_id": "synthetic-receipt",
            "delivery_id": "old",
            "subject_id": "subject",
            "sequence": 1,
            "source": "operator",
            "outcome": "delivered",
            "provider_message_id": None,
            "provider_status": None,
            "evidence_hash": content_hash({}),
            "actor": "operator",
            "reason": "Synthetic receipt",
            "created_at": NOW.isoformat(),
        }
        fields = [*receipt, "evidence_json", "state_hash"]
        connection.execute(
            "INSERT INTO interaction_delivery_reconciliations(" + ",".join(fields) + ") "
            "VALUES (" + ",".join("?" for _ in fields) + ")",
            (*receipt.values(), "{}", content_hash(receipt)),
        )
    observation = RuntimeObserver(database, tmp_path, "subject").snapshot(
        cognition_enabled=True, interval_seconds=30
    )
    assert observation["attention"]["unknown_payments"] == {"count": 50, "more": True}
    attention = observation["attention"]["unresolved_deliveries"]
    assert attention["count"] == 1 and attention["more"] is False
    assert attention["sampled"] == 2 and attention["truncated"] is False


def test_resource_samples_distinguish_operator_tests_and_do_not_expose_responses(
    tmp_path: Path,
) -> None:
    database = tmp_path / "noyra.sqlite3"
    _database(database, calls=4, attempts=1)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE model_calls SET resource_group_id='old',model='old-model' "
            "WHERE subject_id='subject'"
        )
        connection.execute("UPDATE model_calls SET status='unknown' WHERE call_id='call-0000'")
        connection.execute(
            "UPDATE model_calls SET resource_group_id='new',model='new-model',"
            "purpose='operator_model_test:new' WHERE call_id='call-0003'"
        )
    result = RuntimeObserver(database, tmp_path, "subject").snapshot(
        cognition_enabled=True, interval_seconds=30
    )
    sample = result["model_sample"]
    groups = {row["resource_id"]: row for row in sample["by_resource"]}
    assert groups["old"]["unknown"] == 1 and groups["old"]["calls"] == 3
    assert groups["new"]["succeeded"] == 1 and groups["new"]["operator_tests"] == 1
    assert sample["operator_test_calls"] == 1
    assert "DO_NOT_EXPOSE" not in json.dumps(result)


def test_wait_sample_separates_terminal_history_and_keeps_missing_parents_visible(
    tmp_path: Path,
) -> None:
    database = tmp_path / "noyra.sqlite3"
    _database(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO observations VALUES (?,?,?)",
            [
                ("new", "subject", "new"),
                ("rejected", "subject", "rejected"),
                ("foreign", "another-subject", "rejected"),
            ],
        )
        connection.execute("INSERT INTO sleep_runs VALUES ('done','subject','complete',NULL)")
        connection.executemany(
            "INSERT INTO waiting_cognitive_tasks VALUES (?,?,?,?)",
            [
                ("live", "subject", "world_cognition:new", "waiting"),
                ("terminal", "subject", "world_cognition:rejected", "waiting"),
                ("sleep", "subject", "sleep_reflection:done", "waiting"),
                ("cancelled", "subject", "world_cognition:rejected", "cancelled"),
                ("missing", "subject", "world_cognition:missing", "waiting"),
                ("foreign", "subject", "world_cognition:foreign", "waiting"),
            ],
        )
    result = RuntimeObserver(database, tmp_path, "subject").snapshot(
        cognition_enabled=True, interval_seconds=30
    )
    assert result["waiting_sample"] == {
        "scope": "latest_wait_records_not_total",
        "record_limit": CALL_LIMIT,
        "records": 6,
        "truncated": False,
        "active": 3,
        "terminal_parent": 2,
        "cancelled": 1,
        "missing_parent": 2,
    }
