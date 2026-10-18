import importlib.util
import io
import json
import sqlite3
from http.client import HTTPMessage
from pathlib import Path
from types import ModuleType
from typing import Literal
from unittest.mock import patch
from urllib.error import HTTPError

import pytest


def _helper() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "noyra-soak-collector.py"
    spec = importlib.util.spec_from_file_location("noyra_soak_collector", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_is_bounded_and_contains_only_operational_summaries(tmp_path: Path) -> None:
    helper = _helper()
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    (data / "nested").mkdir(parents=True)
    backups.mkdir()
    (data / "nested" / "payload.bin").write_bytes(b"payload")
    (backups / "one.backup").write_bytes(b"backup")
    database = sqlite3.connect(data / "noyra.sqlite3")
    database.execute(
        "CREATE TABLE retention_runs "
        "(started_at TEXT, completed_at TEXT, failed_reason TEXT, "
        "failure_stage TEXT, retry_at TEXT)"
    )
    database.execute(
        "INSERT INTO retention_runs VALUES (?, ?, ?, ?, ?)",
        ("2026-10-18T00:00:00Z", None, "OperationalError", "transaction", None),
    )
    database.commit()
    database.close()

    service = {
        "ActiveState": "active",
        "SubState": "running",
        "MainPID": 42,
        "NRestarts": 0,
        "Result": "success",
    }
    with (
        patch.object(helper, "_systemd", return_value=service),
        patch.object(helper, "_memory", return_value={"rss_bytes": 10, "vm_size_bytes": 20}),
        patch.object(
            helper,
            "_health",
            side_effect=(
                {"status": 200, "body": {"status": "ok", "service": "noyra"}},
                {"status": 200, "body": {"status": "ok", "service": "noyra"}},
            ),
        ),
        patch.object(helper, "_free_bytes", return_value=123),
    ):
        snapshot = helper.collect_snapshot(
            data_dir=data,
            backup_dir=backups,
            now="2026-10-18T01:02:03.000+00:00",
        )

    assert snapshot["format"] == "noyra-soak-snapshot/v1"
    assert snapshot["collected_at"] == "2026-10-18T01:02:03.000+00:00"
    assert snapshot["storage"]["data"]["files"] == 1
    assert snapshot["storage"]["data"]["bytes"] == (data / "noyra.sqlite3").stat().st_size
    assert snapshot["storage"]["backups"]["files"] == 1
    assert snapshot["storage"]["backups"]["bytes"] == len(b"backup")
    assert (
        snapshot["storage"]["database"]["database_bytes"] == (data / "noyra.sqlite3").stat().st_size
    )
    assert snapshot["retention"]["failed_reason"] == "OperationalError"
    assert snapshot["providers"] == {"available": False, "error": "OperationalError"}
    serialized = json.dumps(snapshot)
    assert "secret" not in serialized
    assert "sqlite" not in serialized


def test_provider_snapshot_contains_aggregates_without_provider_identifiers(tmp_path: Path) -> None:
    helper = _helper()
    database = tmp_path / "noyra.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE provider_health_state "
        "(subject_id TEXT, provider_kind TEXT, provider_id TEXT, state TEXT, updated_at TEXT)"
    )
    connection.executemany(
        "INSERT INTO provider_health_state VALUES (?, ?, ?, ?, ?)",
        [
            ("subject-secret", "model", "provider-secret", "healthy", "2026-10-18T00:00:00Z"),
            ("subject-secret", "model", "another-secret", "cooldown", "2026-10-18T00:00:00Z"),
        ],
    )
    connection.commit()
    connection.close()

    result = helper._provider_health(database)

    assert result == {"available": True, "states": {"model": {"cooldown": 1, "healthy": 1}}}
    assert "secret" not in json.dumps(result)


def test_model_activity_snapshot_is_bounded_and_uses_a_time_cursor(tmp_path: Path) -> None:
    helper = _helper()
    database = tmp_path / "noyra.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE model_calls (call_id TEXT, created_at TEXT, completed_at TEXT, "
        "status TEXT, error_code TEXT)"
    )
    connection.execute(
        "CREATE TABLE model_attempts (attempt_id TEXT, started_at TEXT, completed_at TEXT, "
        "status TEXT, error_code TEXT)"
    )
    connection.execute(
        "INSERT INTO model_calls VALUES (?, ?, ?, ?, ?)",
        (
            "call-private-id",
            "2026-10-18T01:00:00+00:00",
            "2026-10-18T01:00:00.250+00:00",
            "failed",
            "provider_timeout",
        ),
    )
    connection.execute(
        "INSERT INTO model_attempts VALUES (?, ?, ?, ?, ?)",
        (
            "attempt-private-id",
            "2026-10-18T01:00:00+00:00",
            "2026-10-18T01:00:00.500+00:00",
            "unknown",
            "response_lost",
        ),
    )
    connection.commit()
    connection.close()

    result = helper._model_activity(
        database,
        since="2026-10-18T00:59:00+00:00",
        until="2026-10-18T01:01:00+00:00",
    )

    assert result["available"] is True
    assert result["calls"]["statuses"] == {"failed": 1}
    assert result["attempts"]["statuses"] == {"unknown": 1}
    assert result["calls"]["duration"]["p50_ms"] == 250
    assert result["attempts"]["duration"]["p95_ms"] == 500
    assert result["calls"]["cursor"] == {"started_at": "2026-10-18T01:00:00+00:00"}
    assert "call-private-id" not in json.dumps(result)
    assert "attempt-private-id" not in json.dumps(result)


def test_model_activity_keyset_cursor_drains_rows_beyond_limit(tmp_path: Path) -> None:
    helper = _helper()
    database = tmp_path / "noyra.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE model_calls (call_id TEXT, created_at TEXT, completed_at TEXT, "
        "status TEXT, error_code TEXT)"
    )
    connection.executemany(
        "INSERT INTO model_calls VALUES (?, ?, ?, ?, ?)",
        [
            (f"call-{index:04d}", "2026-10-18T01:00:00+00:00", None, "ok", None)
            for index in range(helper.MAX_ACTIVITY_ROWS + 5)
        ],
    )
    connection.commit()
    connection.close()

    first_cursor: dict[str, dict[str, str]] = {}
    first = helper._model_activity(
        database,
        since="2026-10-18T00:00:00+00:00",
        until="2026-10-18T02:00:00+00:00",
        cursor_sink=first_cursor,
    )
    assert first["calls"]["rows"] == helper.MAX_ACTIVITY_ROWS
    assert first_cursor == {"calls": {"timestamp": "2026-10-18T01:00:00+00:00", "id": "call-1999"}}

    second_cursor: dict[str, dict[str, str]] = {}
    second = helper._model_activity(
        database,
        since="2026-10-18T00:00:00+00:00",
        until="2026-10-18T02:00:00+00:00",
        cursors=first_cursor,
        cursor_sink=second_cursor,
    )
    assert second["calls"]["rows"] == 5
    assert second_cursor["calls"]["id"] == "call-2004"


def test_degraded_snapshot_contains_bounded_fault_window(tmp_path: Path) -> None:
    helper = _helper()
    output = tmp_path / "soak.jsonl"
    with (
        patch.object(
            helper,
            "_systemd",
            return_value={"ActiveState": "active", "SubState": "running", "MainPID": 0},
        ),
        patch.object(helper, "_health", return_value={"status": 503, "body": {}}),
        patch.object(helper, "_provider_health", return_value={"available": True, "states": {}}),
        patch.object(helper, "_memory", return_value={"rss_bytes": None, "vm_size_bytes": None}),
        patch.object(helper, "_free_bytes", return_value=123),
    ):
        snapshot = helper.collect_snapshot(
            data_dir=tmp_path,
            backup_dir=tmp_path,
            now="2026-10-18T01:02:03+00:00",
            fault_state={},
            output=output,
        )

    assert snapshot["fault_window"]["phase"] == "degraded"
    assert snapshot["fault_window"]["complete"] is False
    assert snapshot["fault_window"]["samples"] == []


def test_health_sanitizes_untrusted_json_fields() -> None:
    helper = _helper()

    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> Literal[False]:
            return False

        def read(self, _limit: int) -> bytes:
            return json.dumps(
                {
                    "status": "ok",
                    "service": "noyra",
                    "token": "do-not-record-this",
                    "details": {"credential": "do-not-record-this"},
                }
            ).encode()

    with patch.object(helper, "urlopen", return_value=Response()):
        result = helper._health("http://health")

    assert result == {"status": 200, "body": {"status": "ok", "service": "noyra"}}
    assert "do-not-record-this" not in json.dumps(result)


def test_health_failure_is_recorded_without_raising() -> None:
    helper = _helper()
    with patch.object(helper, "urlopen", side_effect=OSError("network secret")):
        result = helper._health("http://health")
    assert result == {"status": None, "error": "OSError"}


def test_http_health_failure_preserves_status_without_body_secrets() -> None:
    helper = _helper()
    error = HTTPError(
        "http://health",
        503,
        "unavailable",
        HTTPMessage(),
        None,
    )
    body = io.BytesIO(
        json.dumps({"status": "degraded", "service": "noyra", "token": "secret"}).encode()
    )
    error.fp = body
    with patch.object(helper, "urlopen", side_effect=error):
        result = helper._health("http://health")
    assert result == {"status": 503, "body": {"status": "degraded", "service": "noyra"}}
    assert "secret" not in json.dumps(result)
    assert body.closed


def test_systemd_failure_is_recorded_without_command_output() -> None:
    helper = _helper()
    with patch.object(helper.subprocess, "run", side_effect=OSError("unit secret")):
        result = helper._systemd()
    assert result == {"error": "OSError"}
    assert "secret" not in json.dumps(result)


def test_append_snapshot_keeps_latest_records_under_limit(tmp_path: Path) -> None:
    helper = _helper()
    output = tmp_path / "soak" / "snapshots.jsonl"
    for index in range(80):
        helper.append_snapshot(output, {"index": index, "payload": "x" * 40}, max_bytes=1_024)
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert records[-1] == {"index": 79, "payload": "x" * 40}
    assert records[0]["index"] > 0
    assert output.stat().st_size <= 1_024


def test_append_snapshot_rejects_oversized_record_and_invalid_limits(tmp_path: Path) -> None:
    helper = _helper()
    output = tmp_path / "snapshots.jsonl"
    with pytest.raises(ValueError, match="snapshot_exceeds_max_bytes"):
        helper.append_snapshot(output, {"value": "x" * 2_000}, max_bytes=1_024)
    with pytest.raises(ValueError, match="invalid soak output"):
        helper.append_snapshot(Path("relative.jsonl"), {"ok": True})
    with pytest.raises(ValueError, match="invalid soak output"):
        helper.append_snapshot(tmp_path / "too-large.jsonl", {"ok": True}, max_bytes=100_000_001)
