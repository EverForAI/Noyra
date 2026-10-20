#!/usr/bin/env python3
"""Collect one bounded, secret-free Noyra soak snapshot."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_SAFE_LABEL = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_SYSTEMD_FIELDS = {"ActiveState", "SubState", "MainPID", "NRestarts", "Result"}
MAX_OUTPUT_BYTES = 100_000_000
MAX_ACTIVITY_ROWS = 2_000
FAULT_WINDOW_SECONDS = 900
ACTIVITY_CURSOR_VERSION = 2


def _safe_label(value: object) -> str | None:
    if isinstance(value, str) and _SAFE_LABEL.fullmatch(value):
        return value
    return None


def _safe_health_body(body: object) -> dict[str, object]:
    if not isinstance(body, dict):
        return {}
    result: dict[str, object] = {}
    for key in ("status", "service", "version"):
        value = _safe_label(body.get(key))
        if value is not None:
            result[key] = value
    return result


def _health(url: str) -> dict[str, object]:
    try:
        with urlopen(Request(url), timeout=3) as response:
            body = response.read(16_384)
            return {"status": response.status, "body": _safe_health_body(json.loads(body))}
    except HTTPError as error:
        try:
            if error.fp is None:
                safe_body = {}
            else:
                with closing(error.fp):
                    body = error.fp.read(16_384)
                safe_body = _safe_health_body(json.loads(body))
        except (OSError, TypeError, ValueError):
            safe_body = {}
        return {"status": error.code, "body": safe_body}
    except (OSError, URLError, ValueError, TimeoutError) as error:
        return {"status": None, "error": type(error).__name__}


def _systemd() -> dict[str, object]:
    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                "noyra",
                "--no-pager",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "MainPID",
                "-p",
                "NRestarts",
                "-p",
                "Result",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": type(error).__name__}
    values: dict[str, object] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in _SYSTEMD_FIELDS:
            continue
        if key in {"MainPID", "NRestarts"}:
            try:
                values[key] = int(value)
            except ValueError:
                values[key] = None
        else:
            values[key] = value
    values["returncode"] = result.returncode
    return values


def _memory(main_pid: object) -> dict[str, int | None]:
    if not isinstance(main_pid, int) or main_pid <= 0:
        return {"rss_bytes": None, "vm_size_bytes": None}
    try:
        values: dict[str, int | None] = {"rss_bytes": None, "vm_size_bytes": None}
        for line in (Path("/proc") / str(main_pid) / "status").read_text().splitlines():
            key, separator, raw = line.partition(":")
            if key not in {"VmRSS", "VmSize"} or not separator:
                continue
            values["rss_bytes" if key == "VmRSS" else "vm_size_bytes"] = (
                int(raw.strip().split()[0]) * 1024
            )
        return values
    except (OSError, ValueError, IndexError):
        return {"rss_bytes": None, "vm_size_bytes": None}


def _tree_size(path: Path) -> dict[str, int | None]:
    try:
        total = 0
        count = 0
        if path.is_symlink():
            return {"bytes": None, "files": None}
        if path.is_file():
            return {"bytes": path.stat().st_size, "files": 1}
        if not path.is_dir():
            return {"bytes": 0, "files": 0}
        with os.scandir(path) as entries:
            for index, entry in enumerate(entries):
                if index >= 10_000:
                    return {"bytes": None, "files": None}
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                total += entry.stat(follow_symlinks=False).st_size
                count += 1
        return {"bytes": total, "files": count}
    except (OSError, ValueError):
        return {"bytes": None, "files": None}


def _free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
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


def _retention(database: Path) -> dict[str, object]:
    if not database.is_file() or database.is_symlink():
        return {"available": False}
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        with closing(connection):
            row = connection.execute(
                "SELECT started_at, completed_at, failed_reason, failure_stage, retry_at "
                "FROM retention_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
    except (sqlite3.Error, OSError, ValueError) as error:
        return {"available": False, "error": type(error).__name__}
    return {
        "available": True,
        "started_at": _safe_timestamp(row[0]) if row else None,
        "completed_at": _safe_timestamp(row[1]) if row else None,
        "failed_reason": _safe_label(row[2]) if row else None,
        "failure_stage": _safe_label(row[3]) if row else None,
        "retry_at": _safe_timestamp(row[4]) if row else None,
    }


def _provider_health(database: Path, *, since: str | None = None) -> dict[str, object]:
    if not database.is_file() or database.is_symlink():
        return {"available": False}
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        with closing(connection):
            connection.set_progress_handler(lambda: 1, 50_000)
            rows = connection.execute(
                "SELECT provider_kind, state, COUNT(*) "
                "FROM provider_health_state GROUP BY provider_kind, state"
            ).fetchall()
            recent_rows = (
                connection.execute(
                    "SELECT provider_kind, state, COUNT(*) "
                    "FROM provider_health_state WHERE updated_at >= ? "
                    "GROUP BY provider_kind, state",
                    (since,),
                ).fetchall()
                if since is not None
                else []
            )
    except (sqlite3.Error, OSError, ValueError) as error:
        return {"available": False, "error": type(error).__name__}
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
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        with closing(connection):
            connection.set_progress_handler(lambda: 1, 50_000)
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
                    "FROM model_calls WHERE completed_at IS NOT NULL AND "
                    "(completed_at > ? OR (completed_at = ? AND call_id > ?)) "
                    "AND completed_at <= ? ORDER BY completed_at, call_id LIMIT ?",
                    (call_timestamp, call_timestamp, call_id, end, MAX_ACTIVITY_ROWS),
                ).fetchall()
                in_progress_calls = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM model_calls WHERE completed_at IS NULL"
                    ).fetchone()[0]
                )
            if _table_exists(connection, "model_attempts"):
                attempts = connection.execute(
                    "SELECT attempt_id, started_at, completed_at, status, error_code "
                    "FROM model_attempts WHERE completed_at IS NOT NULL AND "
                    "(completed_at > ? OR (completed_at = ? AND attempt_id > ?)) "
                    "AND completed_at <= ? ORDER BY completed_at, attempt_id LIMIT ?",
                    (attempt_timestamp, attempt_timestamp, attempt_id, end, MAX_ACTIVITY_ROWS),
                ).fetchall()
                in_progress_attempts = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM model_attempts WHERE completed_at IS NULL"
                    ).fetchone()[0]
                )
    except (sqlite3.Error, OSError, ValueError) as error:
        return {"available": False, "error": type(error).__name__}

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


def _read_state(path: Path) -> tuple[dict[str, object], str | None]:
    try:
        if path.is_symlink():
            return {}, "symlink"
        if path.stat().st_size > 16_384:
            return {}, "oversized"
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}, "invalid_shape"
        return value, None
    except FileNotFoundError:
        return {}, None
    except OSError:
        return {}, "unreadable"
    except (UnicodeError, ValueError):
        return {}, "invalid_json"


def _write_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".soak-state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _fault_window(
    output: Path,
    *,
    collected_at: str,
    degraded: bool,
    state: dict[str, object],
) -> dict[str, object] | None:
    active_since = state.get("fault_since")
    if degraded and not isinstance(active_since, str):
        active_since = collected_at
    if not isinstance(active_since, str):
        return None
    try:
        current = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        started = datetime.fromisoformat(active_since.replace("Z", "+00:00"))
    except ValueError:
        return None
    if current - started > timedelta(seconds=FAULT_WINDOW_SECONDS) and not degraded:
        return None
    samples: list[dict[str, object]] = []
    try:
        with output.open("rb") as stream:
            stream.seek(max(0, output.stat().st_size - 1_000_000))
            tail = stream.read(1_000_000)
        for line in tail.decode("utf-8", errors="replace").splitlines()[-200:]:
            value = json.loads(line)
            if not isinstance(value, dict):
                continue
            timestamp = value.get("collected_at")
            if not isinstance(timestamp, str):
                continue
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if started - timedelta(seconds=FAULT_WINDOW_SECONDS) <= parsed <= current:
                samples.append(
                    {
                        "collected_at": _safe_timestamp(timestamp),
                        "health": value.get("health"),
                        "service": value.get("service"),
                    }
                )
    except (OSError, ValueError, TypeError):
        pass
    return {
        "started_at": _safe_timestamp(started.isoformat()),
        "since": _safe_timestamp((started - timedelta(seconds=FAULT_WINDOW_SECONDS)).isoformat()),
        "until": _safe_timestamp((started + timedelta(seconds=FAULT_WINDOW_SECONDS)).isoformat()),
        "phase": "degraded" if degraded else "recovery",
        "complete": not degraded and current >= started + timedelta(seconds=FAULT_WINDOW_SECONDS),
        "samples": samples[-100:],
    }


def collect_snapshot(
    *,
    data_dir: Path,
    backup_dir: Path,
    health_base: str = "http://127.0.0.1:8765",
    now: str | None = None,
    since: str | None = None,
    activity_cursors: dict[str, object] | None = None,
    activity_cursor_sink: dict[str, dict[str, str]] | None = None,
    fault_state: dict[str, object] | None = None,
    output: Path | None = None,
) -> dict[str, object]:
    collected_at = now or datetime.now(UTC).isoformat(timespec="milliseconds")
    service = _systemd()
    live = _health(f"{health_base}/health/live")
    ready = _health(f"{health_base}/health/ready")
    activity = _model_activity(
        data_dir / "noyra.sqlite3",
        since=since,
        until=collected_at,
        cursors=activity_cursors,
        cursor_sink=activity_cursor_sink,
    )
    degraded = (
        live.get("status") != 200
        or ready.get("status") != 200
        or service.get("ActiveState") != "active"
        or service.get("SubState") != "running"
    )
    window = _fault_window(
        output or Path("/var/log/noyra/soak.jsonl"),
        collected_at=collected_at,
        degraded=degraded,
        state=fault_state or {},
    )
    return {
        "format": "noyra-soak-snapshot/v1",
        "collected_at": collected_at,
        "service": service,
        "memory": _memory(service.get("MainPID")),
        "health": {
            "live": live,
            "ready": ready,
        },
        "storage": {
            "data": {**_tree_size(data_dir), "free_bytes": _free_bytes(data_dir)},
            "backups": {
                **_tree_size(backup_dir),
                "free_bytes": _free_bytes(backup_dir),
            },
            "database": _database_size(data_dir / "noyra.sqlite3"),
        },
        "providers": _provider_health(data_dir / "noyra.sqlite3", since=since),
        "model_activity": activity,
        "retention": _retention(data_dir / "noyra.sqlite3"),
        "fault_window": window,
    }


def _database_size(path: Path) -> dict[str, int | None]:
    main = _tree_size(path)
    if main["bytes"] is None:
        return {
            "database_bytes": None,
            "wal_bytes": None,
            "shm_bytes": None,
            "physical_bytes": None,
        }
    wal = _tree_size(path.with_name(path.name + "-wal"))["bytes"]
    shm = _tree_size(path.with_name(path.name + "-shm"))["bytes"]
    if wal is None or shm is None:
        return {
            "database_bytes": int(main["bytes"]),
            "wal_bytes": wal,
            "shm_bytes": shm,
            "physical_bytes": None,
        }
    wal_bytes = int(wal)
    shm_bytes = int(shm)
    return {
        "database_bytes": int(main["bytes"]),
        "wal_bytes": wal_bytes,
        "shm_bytes": shm_bytes,
        "physical_bytes": int(main["bytes"]) + wal_bytes + shm_bytes,
    }


def append_snapshot(
    path: Path, snapshot: dict[str, object], *, max_bytes: int = 10_000_000
) -> None:
    if not path.is_absolute() or not 1_024 <= max_bytes <= MAX_OUTPUT_BYTES:
        raise ValueError("invalid soak output")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    line = (json.dumps(snapshot, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8")
    if len(line) > max_bytes:
        raise ValueError("soak_snapshot_exceeds_max_bytes")
    with _soak_output_lock(path):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o640)
        with os.fdopen(descriptor, "ab") as stream:
            opened = os.fstat(stream.fileno())
            current = path.lstat()
            if (
                not stat.S_ISREG(current.st_mode)
                or path.is_symlink()
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise ValueError("unsafe soak output")
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
        if path.stat().st_size <= max_bytes:
            return
        rotated_from = path.lstat()
        total_size = path.stat().st_size
        start = max(0, total_size - max_bytes - 1)
        with path.open("rb") as stream:
            stream.seek(start)
            tail = stream.read(max_bytes + 1)
        lines = tail.splitlines()
        if start:
            lines = lines[1:]
        kept: list[bytes] = []
        size = 0
        target_size = max_bytes * 4 // 5
        for item in reversed(lines):
            required = len(item) + 1
            if kept and size + required > target_size:
                break
            kept.append(item)
            size += required
        current = path.lstat()
        if (rotated_from.st_dev, rotated_from.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("unsafe soak output replacement")
        _atomic_replace_snapshot(path, b"\n".join(reversed(kept)) + b"\n")


@contextmanager
def _soak_output_lock(path: Path):
    """Serialize append and rotation across overlapping timer processes."""

    lock_path = path.with_name(path.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o640)
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        yield
    finally:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        else:
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


def _atomic_replace_snapshot(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError("unsafe soak output")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.chmod(temporary, 0o640)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/noyra"))
    parser.add_argument("--backup-dir", type=Path, default=Path("/var/backups/noyra"))
    parser.add_argument("--health-base", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, default=Path("/var/log/noyra/soak.jsonl"))
    parser.add_argument("--state", type=Path, default=Path("/var/log/noyra/soak-state.json"))
    parser.add_argument("--max-bytes", type=int, default=10_000_000)
    args = parser.parse_args(sys.argv[1:])
    state, state_reset_reason = _read_state(args.state)
    cursor = state.get("cursor")
    activity_cursors = state.get("activity_cursors")
    if activity_cursors is not None and not isinstance(activity_cursors, dict):
        state_reset_reason = state_reset_reason or "invalid_activity_cursors"
        activity_cursors = None
    if isinstance(activity_cursors, dict) and state.get("activity_cursor_version") != (
        ACTIVITY_CURSOR_VERSION
    ):
        state_reset_reason = state_reset_reason or "activity_cursor_version_changed"
        activity_cursors = None
    if cursor is not None and not isinstance(cursor, str):
        state_reset_reason = state_reset_reason or "invalid_snapshot_cursor"
        cursor = None
    next_activity_cursors: dict[str, dict[str, str]] = {}
    snapshot = collect_snapshot(
        data_dir=args.data_dir,
        backup_dir=args.backup_dir,
        health_base=args.health_base.rstrip("/"),
        since=cursor if isinstance(cursor, str) else None,
        activity_cursors=activity_cursors,
        activity_cursor_sink=next_activity_cursors,
        fault_state=state,
        output=args.output,
    )
    if state_reset_reason is not None:
        snapshot["state_reset_reason"] = state_reset_reason
    append_snapshot(args.output, snapshot, max_bytes=args.max_bytes)
    collected_at = snapshot["collected_at"]
    if isinstance(collected_at, str):
        persisted_activity_cursors = dict(activity_cursors or {})
        persisted_activity_cursors.update(next_activity_cursors)
        next_state: dict[str, object] = {
            "cursor": collected_at,
            "activity_cursors": persisted_activity_cursors,
            "activity_cursor_version": ACTIVITY_CURSOR_VERSION,
        }
        fault_window = snapshot.get("fault_window")
        if isinstance(fault_window, dict) and not fault_window.get("complete"):
            next_state["fault_since"] = fault_window.get("started_at")
        _write_state(args.state, next_state)
    print(json.dumps(snapshot, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
