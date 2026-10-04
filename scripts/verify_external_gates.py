#!/usr/bin/env python3
"""Validate the signed, same-commit external release gate contract."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature

EXPECTED_GATE_IDS = (
    "ubuntu_systemd",
    "encrypted_volume",
    "backup_restore",
    "migration_fence",
    "signer_kms",
    "reorg_nonce",
    "https_proxy",
    "soak",
)
MAX_AGE = timedelta(hours=72)
MAX_ARTIFACT_BYTES = 256 * 1024
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "api_keys",
        "authorization",
        "bearer_token",
        "client_secret",
        "credential",
        "credentials",
        "mnemonic",
        "password",
        "private_key",
        "secret",
        "secret_key",
        "seed",
        "token",
    }
)


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
    except (KeyError, ValueError, TypeError, ImportError, InvalidSignature):
        return False


def _secret_field_errors(record: dict[str, Any]) -> tuple[str, ...]:
    """Reject secret-shaped fields before an evidence record is persisted."""
    errors: list[str] = []
    stack: list[tuple[str, Any]] = [("", record)]
    visited = 0
    while stack:
        path, value = stack.pop()
        visited += 1
        if visited > 4096:
            errors.append("secret_scan_limit")
            break
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    continue
                normalized = key.strip().lower().replace("-", "_")
                child_path = f"{path}.{key}" if path else key
                if normalized in SENSITIVE_FIELD_NAMES:
                    errors.append(f"secret_field:{child_path}")
                elif isinstance(child, (dict, list)):
                    stack.append((child_path, child))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, list)):
                    stack.append((f"{path}[{index}]", child))
    return tuple(dict.fromkeys(errors))


def validate_external_gates(
    record: dict[str, Any],
    *,
    expected_sha: str,
    now: datetime | None = None,
    public_key: str,
) -> tuple[str, ...]:
    """Return stable field-level errors; an empty tuple means valid."""
    errors: list[str] = []
    errors.extend(_secret_field_errors(record))
    if (
        record.get("format") != "noyra-external-gates/v1"
        or type(record.get("schema_version")) is not int
        or record.get("schema_version") != 1
    ):
        errors.append("schema")
    commit = record.get("commit_sha")
    if (
        not isinstance(expected_sha, str)
        or not COMMIT_SHA_RE.fullmatch(expected_sha)
        or not isinstance(commit, str)
        or not COMMIT_SHA_RE.fullmatch(commit)
        or commit != expected_sha
    ):
        errors.append("commit_sha")
    if record.get("status") != "passed":
        errors.append("status")
    reviewed = _time(record.get("reviewed_at"))
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if reviewed is None or reviewed > current or current - reviewed > MAX_AGE:
        errors.append("reviewed_at")
    reviewer = record.get("reviewer")
    reviewer_id = reviewer.get("id") if isinstance(reviewer, dict) else None
    reviewer_role = reviewer.get("role") if isinstance(reviewer, dict) else None
    if (
        not isinstance(reviewer_id, str)
        or not reviewer_id.strip()
        or not isinstance(reviewer_role, str)
        or not reviewer_role.strip()
    ):
        errors.append("reviewer")
    gates = record.get("gates")
    if not isinstance(gates, list):
        errors.append("gate_ids")
        gates = []
    ids: list[str] = [
        gate["id"] for gate in gates if isinstance(gate, dict) and isinstance(gate.get("id"), str)
    ]
    if tuple(sorted(ids)) != tuple(sorted(EXPECTED_GATE_IDS)):
        errors.append("gate_ids")
    for gate in gates:
        if not isinstance(gate, dict):
            errors.append("gate_entry")
            continue
        gate_id = gate.get("id")
        gate_label = (
            gate_id if isinstance(gate_id, str) and gate_id in EXPECTED_GATE_IDS else "unknown"
        )
        if gate.get("status") != "passed":
            errors.append(f"gate:{gate_label}")
        executed_by = gate.get("executed_by")
        reviewed_by = gate.get("reviewed_by")
        if (
            not isinstance(executed_by, str)
            or not executed_by.strip()
            or not isinstance(reviewed_by, str)
            or not reviewed_by.strip()
            or reviewed_by != reviewer_id
            or executed_by == reviewer_id
        ):
            errors.append(f"gate_reviewer:{gate_label}")
        evidence_refs = gate.get("evidence_refs")
        if (
            not isinstance(evidence_refs, list)
            or not evidence_refs
            or any(not isinstance(value, str) or not value.strip() for value in evidence_refs)
        ):
            errors.append(f"evidence_refs:{gate_label}")
        started_at = _time(gate.get("started_at"))
        finished_at = _time(gate.get("finished_at"))
        if (
            started_at is None
            or finished_at is None
            or finished_at <= started_at
            or reviewed is None
            or finished_at > reviewed
        ):
            errors.append(f"gate_window:{gate_label}")
    if not public_key.strip() or not _verify_signature(record, public_key):
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
