"""Read existing action/audit evidence without initializing or repairing a database."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from noyra.core import integrity as checks
from noyra.core.errors import IntegrityError


def diagnose(database_path: Path) -> tuple[dict[str, Any], int]:
    database = checks._ReadOnlyDatabase(database_path)
    if not database.path.is_file():
        return {"check": "core.actions", "status": "unavailable", "reason": "missing_database"}, 2
    try:
        with database.read_transaction() as connection:
            subjects = connection.execute("SELECT subject_id FROM runtime_state").fetchall()
            if len(subjects) != 1:
                return {
                    "check": "core.actions",
                    "status": "unavailable",
                    "reason": "subject_count_unexpected",
                }, 2
            context = SimpleNamespace(
                database=checks._SnapshotDatabase(database, cast(Any, connection)),
                connection=connection,
                subject_id=subjects[0]["subject_id"],
            )
            counts = checks._check_actions(cast(Any, context))
        return {"check": "core.actions", "status": "ok", "counts": counts}, 0
    except IntegrityError as error:
        # Stack metadata identifies the failed rule without revealing exception
        # messages, SQL row payloads, private text, credentials or secrets.
        sites: list[dict[str, Any]] = []
        trace = error.__traceback__
        while trace is not None:
            frame = trace.tb_frame
            name = Path(frame.f_code.co_filename).name
            if name in {"integrity.py", "actions.py"}:
                site: dict[str, Any] = {
                    "file": name,
                    "line": trace.tb_lineno,
                    "function": frame.f_code.co_name,
                }
                action = frame.f_locals.get("action")
                if isinstance(action, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", action):
                    site["audit_action"] = action
                sites.append(site)
            trace = trace.tb_next
        return {
            "check": "core.actions",
            "status": "corrupt",
            "error_type": type(error).__name__,
            "sites": sites,
        }, 1
    except Exception as error:
        return {
            "check": "core.actions",
            "status": "unavailable",
            "error_type": type(error).__name__,
        }, 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--require-stopped", metavar="SYSTEMD_UNIT")
    args = parser.parse_args()
    if args.require_stopped:
        try:
            result = subprocess.run(
                [
                    "systemctl",
                    "show",
                    args.require_stopped,
                    "-p",
                    "ActiveState",
                    "-p",
                    "MainPID",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            stopped = (
                state.get("ActiveState") in {"inactive", "failed"} and state.get("MainPID") == "0"
            )
        except (OSError, subprocess.SubprocessError):
            stopped = False
        if not stopped:
            print(json.dumps({"status": "unavailable", "reason": "service_not_confirmed_stopped"}))
            return 2
    report, exit_code = diagnose(args.database)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
