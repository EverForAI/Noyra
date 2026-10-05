#!/usr/bin/env python3
# ruff: noqa: E402
"""Validate the signed, same-commit external release gate contract."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from noyra.core.external_gates import (
    EXPECTED_GATE_IDS as EXPECTED_GATE_IDS,
)
from noyra.core.external_gates import (
    MAX_ARTIFACT_BYTES,
)
from noyra.core.external_gates import (
    _canonical_payload as _canonical_payload,
)
from noyra.core.external_gates import (
    validate_external_gates as validate_external_gates,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--max-age-hours", type=int, default=72)
    args = parser.parse_args()
    if args.max_age_hours != 72:
        raise SystemExit("external gate freshness is fixed at 72 hours")
    try:
        metadata = args.path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            print(json.dumps({"status": "fail", "errors": ["artifact_not_file"]}))
            return 1
        if metadata.st_size > MAX_ARTIFACT_BYTES:
            print(json.dumps({"status": "fail", "errors": ["artifact_too_large"]}))
            return 1
        record = json.loads(args.path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(json.dumps({"status": "fail", "errors": ["missing_artifact"]}))
        return 1
    except IsADirectoryError:
        print(json.dumps({"status": "fail", "errors": ["artifact_not_file"]}))
        return 1
    except json.JSONDecodeError:
        print(json.dumps({"status": "fail", "errors": ["invalid_json"]}))
        return 1
    except OSError as error:
        print(json.dumps({"status": "fail", "errors": [type(error).__name__]}))
        return 1
    if not isinstance(record, dict):
        print(json.dumps({"status": "fail", "errors": ["schema"]}))
        return 1
    public_key = os.environ.get("EXTERNAL_GATES_PUBLIC_KEY", "").strip()
    if not public_key:
        print(json.dumps({"status": "fail", "errors": ["public_key_missing"]}))
        return 1
    errors = validate_external_gates(
        record,
        expected_sha=args.expected_sha,
        public_key=public_key,
    )
    print(json.dumps({"status": "pass" if not errors else "fail", "errors": list(errors)}))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
