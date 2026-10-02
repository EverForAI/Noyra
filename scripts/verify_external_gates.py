#!/usr/bin/env python3
"""Validate the signed, same-commit external release gate contract."""

from __future__ import annotations

import argparse
import base64
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

EXPECTED_GATE_IDS = (
    "testnet_transfer",
    "signer_faults",
    "signer_isolation",
    "soak",
    "backup_restore",
    "operator_approval",
)
MAX_AGE = timedelta(hours=72)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _canonical_payload(record: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in record.items() if key != "signature"}
    payload = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (payload + "\n").encode("utf-8")


def _verify_signature(record: dict[str, Any], public_key: str) -> bool:
    signature = record.get("signature")
    if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519":
        return False
    try:
        value = base64.b64decode(str(signature["value"]), validate=True)
        key_bytes = base64.b64decode(public_key, validate=True)
        if len(key_bytes) != 32 or len(value) != 64:
            return False
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(key_bytes).verify(value, _canonical_payload(record))
        return True
    except (KeyError, ValueError, TypeError, ImportError):
        return False


def validate_external_gates(
    record: dict[str, Any],
    *,
    expected_sha: str,
    now: datetime | None = None,
    public_key: str | None = None,
) -> tuple[str, ...]:
    """Return stable field-level errors; an empty tuple means valid."""
    errors: list[str] = []
    if record.get("format") != "noyra-external-gates/v1" or record.get("schema_version") != 1:
        errors.append("schema")
    commit = record.get("commit_sha")
    if not isinstance(commit, str) or commit != expected_sha:
        errors.append("commit_sha")
    if record.get("status") != "passed":
        errors.append("status")
    reviewed = _time(record.get("reviewed_at"))
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if reviewed is None or reviewed > current or current - reviewed > MAX_AGE:
        errors.append("reviewed_at")
    reviewer = record.get("reviewer")
    if not isinstance(reviewer, dict) or not reviewer.get("id") or not reviewer.get("role"):
        errors.append("reviewer")
    gates = record.get("gates")
    if not isinstance(gates, list):
        errors.append("gate_ids")
        gates = []
    ids = [gate.get("id") for gate in gates if isinstance(gate, dict)]
    if tuple(sorted(ids)) != tuple(sorted(EXPECTED_GATE_IDS)):
        errors.append("gate_ids")
    for gate in gates:
        if not isinstance(gate, dict):
            errors.append("gate_entry")
            continue
        if gate.get("status") != "passed":
            errors.append(f"gate:{gate.get('id', 'unknown')}")
        if not gate.get("executed_by") or not gate.get("reviewed_by"):
            errors.append(f"gate_reviewer:{gate.get('id', 'unknown')}")
        if not isinstance(gate.get("evidence_refs"), list) or not gate["evidence_refs"]:
            errors.append(f"evidence_refs:{gate.get('id', 'unknown')}")
        if _time(gate.get("started_at")) is None or _time(gate.get("finished_at")) is None:
            errors.append(f"gate_window:{gate.get('id', 'unknown')}")
    if public_key is not None and not _verify_signature(record, public_key):
        errors.append("signature")
    return tuple(dict.fromkeys(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--max-age-hours", type=int, default=72)
    args = parser.parse_args()
    if args.max_age_hours != 72:
        raise SystemExit("external gate freshness is fixed at 72 hours")
    try:
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
    errors = validate_external_gates(
        record,
        expected_sha=args.expected_sha,
        public_key=os.environ.get("EXTERNAL_GATES_PUBLIC_KEY") or None,
    )
    print(json.dumps({"status": "pass" if not errors else "fail", "errors": list(errors)}))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
