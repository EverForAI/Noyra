from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.runtime_observation import (
    ATTEMPT_LIMIT,
    CALL_LIMIT,
    ObservationBusy,
    RuntimeObserver,
    loop_observation,
    resource_observation,
)
from noyra.core.types import content_hash
from noyra.service import NoyraHTTPServer, ServiceSettings

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


def _database(path: Path, *, calls: int = 1, attempts: int = 1) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE actions(subject_id TEXT,status TEXT);
            CREATE TABLE wallet_payment_executions(subject_id TEXT,status TEXT);
            CREATE TABLE interaction_deliveries(
              delivery_id TEXT PRIMARY KEY,subject_id TEXT,status TEXT);
            CREATE TABLE interaction_delivery_reconciliations(delivery_id TEXT,outcome TEXT);
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
        with (
            patch.object(server, "diagnostics", side_effect=AssertionError("heavy diagnostics")),
            patch.object(server, "_current_storage", side_effect=AssertionError("storage scan")),
            urlopen(Request(url, headers=headers), timeout=5) as response,
        ):
            body: dict[str, Any] = json.load(response)
        assert body["format"] == "noyra-runtime-observation/v1"
        assert body["database_observation"] == "ok"
        assert body["cognition"]["enabled"] is False
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
        connection.execute(
            "INSERT INTO interaction_delivery_reconciliations VALUES ('old','delivered')"
        )
    observation = RuntimeObserver(database, tmp_path, "subject").snapshot(
        cognition_enabled=True, interval_seconds=30
    )
    assert observation["attention"]["unknown_payments"] == {"count": 50, "more": True}
    assert observation["attention"]["unresolved_deliveries"] == {"count": 1, "more": False}


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
