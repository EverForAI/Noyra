#!/usr/bin/env python3
"""Create an explicitly pending acceptance template for the final commit."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from noyra.core.external_gates import EXPECTED_GATE_IDS  # noqa: E402


def pending_record(sha: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("exact 40-character commit SHA required")
    return {
        "format": "noyra-external-gates/v1",
        "schema_version": 1,
        "commit_sha": sha,
        "status": "pending",
        "reviewed_at": None,
        "reviewer": {"id": "", "role": "release-reviewer"},
        "gates": [
            {
                "id": gate,
                "status": "pending",
                "executed_by": "",
                "reviewed_by": "",
                "started_at": None,
                "finished_at": None,
                "evidence_refs": [],
                "failure_reason": "real environment verification not yet performed",
            }
            for gate in EXPECTED_GATE_IDS
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sha = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"{args.commit}^{{commit}}"], cwd=ROOT, text=True
    ).strip()
    record = pending_record(sha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Pending only; no gate executed or passed. Commit: {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
