#!/usr/bin/env python3
"""Collect one bounded, secret-free Noyra soak snapshot."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_SAFE_LABEL = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_SYSTEMD_FIELDS = {"ActiveState", "SubState", "MainPID", "NRestarts", "Result"}
MAX_OUTPUT_BYTES = 100_000_000


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


def _provider_health(database: Path) -> dict[str, object]:
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
    except (sqlite3.Error, OSError, ValueError) as error:
        return {"available": False, "error": type(error).__name__}
    states: dict[str, dict[str, int]] = {}
    for kind, state, count in rows:
        safe_kind = _safe_label(kind)
        safe_state = _safe_label(state)
        if safe_kind is not None and safe_state is not None:
            states.setdefault(safe_kind, {})[safe_state] = int(count)
    return {"available": True, "states": states}


def collect_snapshot(
    *,
    data_dir: Path,
    backup_dir: Path,
    health_base: str = "http://127.0.0.1:8765",
    now: str | None = None,
) -> dict[str, object]:
    service = _systemd()
    return {
        "format": "noyra-soak-snapshot/v1",
        "collected_at": now or datetime.now(UTC).isoformat(timespec="milliseconds"),
        "service": service,
        "memory": _memory(service.get("MainPID")),
        "health": {
            "live": _health(f"{health_base}/health/live"),
            "ready": _health(f"{health_base}/health/ready"),
        },
        "storage": {
            "data": {**_tree_size(data_dir), "free_bytes": _free_bytes(data_dir)},
            "backups": {
                **_tree_size(backup_dir),
                "free_bytes": _free_bytes(backup_dir),
            },
            "database": _database_size(data_dir / "noyra.sqlite3"),
        },
        "providers": _provider_health(data_dir / "noyra.sqlite3"),
        "retention": _retention(data_dir / "noyra.sqlite3"),
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
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o640)
    with os.fdopen(descriptor, "ab") as stream:
        if not os.path.isfile(path) or path.is_symlink():
            raise ValueError("unsafe soak output")
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    if path.stat().st_size <= max_bytes:
        return
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
    with path.open("wb") as stream:
        stream.write(b"\n".join(reversed(kept)) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/noyra"))
    parser.add_argument("--backup-dir", type=Path, default=Path("/var/backups/noyra"))
    parser.add_argument("--health-base", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, default=Path("/var/log/noyra/soak.jsonl"))
    parser.add_argument("--max-bytes", type=int, default=10_000_000)
    args = parser.parse_args()
    snapshot = collect_snapshot(
        data_dir=args.data_dir,
        backup_dir=args.backup_dir,
        health_base=args.health_base.rstrip("/"),
    )
    append_snapshot(args.output, snapshot, max_bytes=args.max_bytes)
    print(json.dumps(snapshot, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
