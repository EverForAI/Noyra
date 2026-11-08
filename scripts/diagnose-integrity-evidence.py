#!/usr/bin/env python3
"""Read integrity evidence without repairs or runtime state changes.

Optional core rechecks require an explicitly sealed SQLite copy.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from noyra.core.database import Database
from noyra.core.identity import validate_subject_id
from noyra.core.integrity import IntegrityAuditLimits, IntegrityRegistry
from noyra.core.integrity_evidence import read_evidence
from noyra.core.storage import StorageLayout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="subject integrity evidence directory")
    parser.add_argument(
        "--snapshot", type=Path, help="explicitly sealed journal-free SQLite copy to recheck"
    )
    parser.add_argument("--subject", help="subject to check in the supplied snapshot")
    args = parser.parse_args()
    if bool(args.snapshot) != bool(args.subject):
        parser.error("--snapshot and --subject must be supplied together")
    output: dict[str, object] = {}
    for name in ("watchdog-state.json", "latest-report.json"):
        result = read_evidence(args.directory / name)
        item: dict[str, Any] = {
            "status": result.status,
            "source": result.source,
            "reason_code": result.reason_code,
        }
        if isinstance(result.payload, dict):
            summary = result.payload.get("last_summary") or result.payload.get("summary")
            summary = summary if isinstance(summary, dict) else {}
            item.update(
                {
                    "run_id": result.payload.get("last_run_id") or result.payload.get("run_id"),
                    "status_value": result.payload.get("last_status")
                    or result.payload.get("status"),
                    "registry_version": result.payload.get("registry_version"),
                    "last_completed_at": result.payload.get("last_completed_at"),
                    "summary": {
                        key: value
                        for key, value in summary.items()
                        if key in {"checks", "ok", "p0", "p1"} and type(value) is int
                    },
                }
            )
        output[name] = item
    evidence_ok = all(
        isinstance(item, dict) and item.get("status") == "ok" for item in output.values()
    )
    exit_code = 0 if evidence_ok else 2
    if args.snapshot is not None:
        try:
            validate_subject_id(args.subject)
            database = Database(
                args.snapshot, initialize=False, read_only=True, immutable_snapshot=True
            )
            root = database.path.parent
            layout = StorageLayout(
                root,
                root / "subject",
                root / "training_raw",
                root / "workspace",
                root / "cache",
                root / "exports",
            )
            report = IntegrityRegistry().run(
                database,
                args.subject,
                layout,
                profile="startup_light",
                policy_mode="off",
                deadline_seconds=5,
                limits=IntegrityAuditLimits(
                    max_rows_per_check=10_000,
                    max_bytes_per_check=2_000_000,
                    max_value_bytes=1_000_000,
                ),
                check_ids=(
                    "core.schema_contract",
                    "core.sqlite_quick_check",
                    "core.foreign_keys",
                    "core.identity_continuity",
                    "core.event_chain_tail",
                ),
            )
            output["snapshot_recheck"] = {
                "status": report.status,
                "scope": "core_startup_only",
                "runtime_state_changed": False,
                "checks": [
                    {"id": check.id, "status": check.status, "reason_code": check.reason_code}
                    for check in report.checks
                ],
            }
            if report.status != "ok":
                exit_code = 1 if report.status == "corrupt" else 2
        except Exception as error:
            output["snapshot_recheck"] = {
                "status": "evidence_unavailable",
                "error_type": type(error).__name__,
            }
            exit_code = 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
