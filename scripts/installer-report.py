#!/usr/bin/env python3
"""Persist a bounded, secret-free native installer outcome using only stdlib."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def write_report(directory: Path, report_id: str, payload: dict[str, Any]) -> Path:
    if not directory.is_absolute() or not SEGMENT.fullmatch(report_id):
        raise ValueError("invalid_report_location")
    # Never follow a service-writable directory or replace an existing report.
    for parent in (*reversed(directory.parents), directory.parent):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or parent.is_symlink():
            raise ValueError("unsafe_report_directory")
        if os.name == "posix" and (
            info.st_uid != getattr(os, "geteuid", lambda: -1)() or info.st_mode & 0o022
        ):
            raise ValueError("unsafe_report_owner")
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
        raise ValueError("unsafe_report_directory")
    if os.name == "posix" and (
        info.st_uid != getattr(os, "geteuid", lambda: -1)()
        or info.st_nlink < 1
        or info.st_mode & 0o077
    ):
        raise ValueError("unsafe_report_owner")
    target = directory / (report_id + ".json")
    if target.exists() or target.is_symlink():
        raise ValueError("report_already_exists")
    data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(data) > 16_384:
        raise ValueError("report_too_large")
    descriptor, temporary = tempfile.mkstemp(prefix=".install-report-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # The report directory is root-only; no untrusted writer can race this.
        os.replace(temporary, target)
        if os.name == "posix":
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--release", default="")
    parser.add_argument("--source-sha", default="")
    parser.add_argument("--previous-release", default="")
    parser.add_argument("--backup", default="")
    parser.add_argument("--service-state", choices=("running", "stopped", "unknown"), required=True)
    flags = (
        "backup-verified",
        "migration-started",
        "migration-completed",
        "readiness-verified",
        "service-observed",
        "previously-running",
        "start-requested",
        "rollback",
        "database-recovery-required",
    )
    for flag in flags:
        parser.add_argument("--" + flag, choices=("true", "false"), required=True)
    args = parser.parse_args()
    if os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0:
        parser.error("root_on_linux_required")
    for value in (args.phase, args.release, args.previous_release):
        if value and not SEGMENT.fullmatch(value):
            parser.error("invalid_report_label")
    if args.source_sha and not re.fullmatch(r"[0-9a-f]{40,64}", args.source_sha):
        parser.error("invalid_source_sha")
    datetime.fromisoformat(args.started_at.replace("Z", "+00:00"))
    booleans = {
        flag.replace("-", "_"): getattr(args, flag.replace("-", "_")) == "true" for flag in flags
    }
    payload = {
        "format": "noyra-install-report/v1",
        "started_at": args.started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "exit_code": args.exit_code,
        "last_phase": args.phase,
        "requested_release": args.release or None,
        "source_sha": args.source_sha or None,
        "previous_release": args.previous_release or None,
        "cold_backup": args.backup or None,
        "service_state": args.service_state,
        "outcome": "failed"
        if args.exit_code
        else ("readiness_verified" if booleans["readiness_verified"] else "installed_not_verified"),
        "database_restore_automatic": False,
        "external_eight_gates_performed": False,
        "rollback_note": (
            "Code rollback requires compatible data; "
            "use a verified cold backup when schema differs."
        ),
        **booleans,
    }
    current = args.directory.parent / "current"
    if current.is_symlink():
        selected = Path(os.readlink(current)).name
        payload["selected_release"] = selected if SEGMENT.fullmatch(selected) else None
    else:
        payload["selected_release"] = None
    path = write_report(args.directory, args.report_id, payload)
    print("Installation report: " + str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
