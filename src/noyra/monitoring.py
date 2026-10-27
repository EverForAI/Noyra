"""Bounded private operational summaries served by the owning runtime.

The host collector reads these summaries over loopback so its read-only mount
never needs SQLite WAL/SHM creation rights. No model inputs or credentials are
included in the operational payload.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from noyra.core.at_rest import _write_private_file, validate_private_file, validate_private_root
from noyra.core.credentials import read_secret_file

_SAFE_LABEL = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
MAX_ACTIVITY_ROWS = 2_000
FAULT_WINDOW_SECONDS = 900
MONITOR_PATH = "/api/v1/admin/monitoring/soak"


def load_monitor_token(data_dir: Path) -> SecretStr:
    # cache is machine-local and excluded from durable subject backups.
    cache_path = data_dir / "cache"
    cache = validate_private_root(cache_path, create=not cache_path.exists())
    root = validate_private_root(cache / "monitoring", create=True)
    path = validate_private_file(root, "read-token")
    if not path.exists():
        with suppress(FileExistsError):
            _write_private_file(path, secrets.token_hex(32).encode("ascii"))
    value = read_secret_file(path, label="local monitoring token", max_bytes=64)
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("local monitoring token is invalid")
    return SecretStr(value)


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=200")
    deadline = time.monotonic() + 0.5
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1_000)
    return connection


def _unavailable(error: BaseException) -> dict[str, Any]:
    result: dict[str, Any] = {"available": False, "error": type(error).__name__}
    name = _safe_label(getattr(error, "sqlite_errorname", None))
    if name is not None:
        result["sqlite_errorname"] = name
    return result


def _safe_label(value: object) -> str | None:
    if isinstance(value, str) and _SAFE_LABEL.fullmatch(value):
        return value
    return None


def _safe_timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _retention(database: Path, subject_id: str) -> dict[str, object]:
    if not database.is_file() or database.is_symlink():
        return {"available": False}
    try:
        connection = _connect(database)
        with closing(connection):
            row = connection.execute(
                "SELECT started_at, completed_at, failed_reason, failure_stage, retry_at "
                "FROM retention_runs WHERE subject_id = ? ORDER BY started_at DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
    except (sqlite3.Error, OSError, ValueError) as error:
        return _unavailable(error)
    return {
        "available": True,
        "started_at": _safe_timestamp(row[0]) if row else None,
        "completed_at": _safe_timestamp(row[1]) if row else None,
        "failed_reason": _safe_label(row[2]) if row else None,
        "failure_stage": _safe_label(row[3]) if row else None,
        "retry_at": _safe_timestamp(row[4]) if row else None,
    }


def _provider_health(
    database: Path, subject_id: str, *, since: str | None = None
) -> dict[str, object]:
    if not database.is_file() or database.is_symlink():
        return {"available": False}
    try:
        connection = _connect(database)
        with closing(connection):
            rows = connection.execute(
                "SELECT provider_kind, state, COUNT(*) "
                "FROM provider_health_state WHERE subject_id = ? "
                "GROUP BY provider_kind, state ORDER BY provider_kind, state LIMIT 128",
                (subject_id,),
            ).fetchall()
            recent_rows = (
                connection.execute(
                    "SELECT provider_kind, state, COUNT(*) "
                    "FROM provider_health_state WHERE subject_id = ? AND updated_at >= ? "
                    "GROUP BY provider_kind, state ORDER BY provider_kind, state LIMIT 128",
                    (subject_id, since),
                ).fetchall()
                if since is not None
                else []
            )
    except (sqlite3.Error, OSError, ValueError) as error:
        return _unavailable(error)
    states: dict[str, dict[str, int]] = {}
    for kind, state, count in rows:
        safe_kind = _safe_label(kind)
        safe_state = _safe_label(state)
        if safe_kind is not None and safe_state is not None:
            states.setdefault(safe_kind, {})[safe_state] = int(count)
    result: dict[str, object] = {"available": True, "states": states}
    if since is not None:
        recent: dict[str, dict[str, int]] = {}
        for kind, state, count in recent_rows:
            safe_kind = _safe_label(kind)
            safe_state = _safe_label(state)
            if safe_kind is not None and safe_state is not None:
                recent.setdefault(safe_kind, {})[safe_state] = int(count)
        result["recent_states"] = recent
        result["since"] = _safe_timestamp(since)
    return result


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _duration_summary(values: list[float]) -> dict[str, int | None]:
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None}
    values.sort()
    return {
        "count": len(values),
        "p50_ms": int(values[(len(values) - 1) // 2]),
        "p95_ms": int(values[min(len(values) - 1, max(0, int(len(values) * 0.95) - 1))]),
    }


def _model_activity(
    database: Path,
    subject_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
    cursors: dict[str, object] | None = None,
    cursor_sink: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    """Return bounded call/attempt aggregates without model payloads."""

    if not database.is_file() or database.is_symlink():
        return {"available": False}
    cutoff = since
    if cutoff is None:
        cutoff = (datetime.now(UTC) - timedelta(seconds=FAULT_WINDOW_SECONDS)).isoformat()
    end = until or datetime.now(UTC).isoformat()
    try:
        connection = _connect(database)
        with closing(connection):
            calls: list[tuple[object, ...]] = []
            attempts: list[tuple[object, ...]] = []
            in_progress_calls = 0
            in_progress_attempts = 0
            call_cursor = cursors.get("calls") if isinstance(cursors, dict) else None
            attempt_cursor = cursors.get("attempts") if isinstance(cursors, dict) else None

            def cursor_values(value: object) -> tuple[str, str]:
                if isinstance(value, dict):
                    timestamp = value.get("timestamp")
                    item_id = value.get("id")
                    if isinstance(timestamp, str) and isinstance(item_id, str):
                        return timestamp, item_id
                if isinstance(since, str):
                    return since, ""
                return cutoff, ""

            call_timestamp, call_id = cursor_values(call_cursor)
            attempt_timestamp, attempt_id = cursor_values(attempt_cursor)
            if _table_exists(connection, "model_calls"):
                calls = connection.execute(
                    "SELECT call_id, created_at, completed_at, status, error_code "
                    "FROM model_calls WHERE subject_id = ? AND completed_at IS NOT NULL AND "
                    "(completed_at > ? OR (completed_at = ? AND call_id > ?)) "
                    "AND completed_at <= ? ORDER BY completed_at, call_id LIMIT ?",
                    (subject_id, call_timestamp, call_timestamp, call_id, end, MAX_ACTIVITY_ROWS),
                ).fetchall()
                in_progress_calls = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM model_calls "
                        "WHERE subject_id = ? AND completed_at IS NULL",
                        (subject_id,),
                    ).fetchone()[0]
                )
            if _table_exists(connection, "model_attempts"):
                attempts = connection.execute(
                    "SELECT attempt_id, started_at, completed_at, status, error_code "
                    "FROM model_attempts WHERE subject_id = ? AND completed_at IS NOT NULL AND "
                    "(completed_at > ? OR (completed_at = ? AND attempt_id > ?)) "
                    "AND completed_at <= ? ORDER BY completed_at, attempt_id LIMIT ?",
                    (
                        subject_id,
                        attempt_timestamp,
                        attempt_timestamp,
                        attempt_id,
                        end,
                        MAX_ACTIVITY_ROWS,
                    ),
                ).fetchall()
                in_progress_attempts = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM model_attempts "
                        "WHERE subject_id = ? AND completed_at IS NULL",
                        (subject_id,),
                    ).fetchone()[0]
                )
    except (sqlite3.Error, OSError, ValueError) as error:
        return _unavailable(error)

    def summarize(rows: list[tuple[object, ...]]) -> dict[str, object]:
        status_counts: dict[str, int] = {}
        error_counts: dict[str, int] = {}
        durations: list[float] = []
        latest: dict[str, str] | None = None
        for row in rows:
            started_raw = row[1] if isinstance(row[1], str) else None
            completed_raw = row[2] if isinstance(row[2], str) else None
            started = _safe_timestamp(started_raw)
            status = _safe_label(row[3])
            error = _safe_label(row[4])
            if status is not None:
                status_counts[status] = status_counts.get(status, 0) + 1
            error_key = error or "none"
            error_counts[error_key] = error_counts.get(error_key, 0) + 1
            if started_raw is not None and completed_raw is not None:
                try:
                    elapsed = (
                        datetime.fromisoformat(completed_raw.replace("Z", "+00:00"))
                        - datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
                    ).total_seconds() * 1000
                    if 0 <= elapsed <= 7 * 24 * 60 * 60 * 1000:
                        durations.append(elapsed)
                except ValueError:
                    pass
            if started is not None:
                latest = {"started_at": started}
        return {
            "rows": len(rows),
            "statuses": status_counts,
            "errors": error_counts,
            "duration": _duration_summary(durations),
            "cursor": latest,
        }

    if cursor_sink is not None:
        if calls:
            timestamp = calls[-1][2]
            item_id = calls[-1][0]
            if isinstance(timestamp, str) and isinstance(item_id, str):
                cursor_sink["calls"] = {"timestamp": timestamp, "id": item_id}
        if attempts:
            timestamp = attempts[-1][2]
            item_id = attempts[-1][0]
            if isinstance(timestamp, str) and isinstance(item_id, str):
                cursor_sink["attempts"] = {"timestamp": timestamp, "id": item_id}

    return {
        "available": True,
        "window": {"since": _safe_timestamp(cutoff), "until": _safe_timestamp(end)},
        "limited": len(calls) >= MAX_ACTIVITY_ROWS or len(attempts) >= MAX_ACTIVITY_ROWS,
        "calls": summarize(calls),
        "attempts": summarize(attempts),
        "in_progress": {"calls": in_progress_calls, "attempts": in_progress_attempts},
    }


def operational_summary(
    database: Path,
    subject_id: str,
    *,
    since: str | None = None,
    cursors: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Return aggregates plus private cursor state, never source/provider text."""
    until = datetime.now(UTC).isoformat(timespec="milliseconds")
    sink: dict[str, dict[str, str]] = {}
    activity = _model_activity(
        database, subject_id, since=since, until=until, cursors=cursors, cursor_sink=sink
    )
    return {
        "format": "noyra-monitoring/v1",
        "collected_at": until,
        "model_activity": activity,
        "providers": _provider_health(database, subject_id, since=since),
        "retention": _retention(database, subject_id),
        "activity_cursors": sink,
    }
