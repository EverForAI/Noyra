#!/usr/bin/env python3
"""Validate an independently signed external-gate record before artifact upload."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any

from scripts.verify_external_gates import validate_external_gates

MAX_BUNDLE_BASE64_CHARS = 65_000


def decode_external_gates_bundle(
    encoded: str, *, expected_sha: str, public_key: str
) -> dict[str, Any]:
    """Decode and verify a signed gate bundle without logging its contents."""
    if not encoded or len(encoded) > MAX_BUNDLE_BASE64_CHARS:
        raise ValueError("bundle_size")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("bundle_base64") from None
    try:
        record = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("bundle_json") from None
    if not isinstance(record, dict):
        raise ValueError("bundle_schema")
    errors = validate_external_gates(
        record,
        expected_sha=expected_sha,
        public_key=public_key,
    )
    if errors:
        raise ValueError(",".join(errors))
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    encoded = os.environ.get("EXTERNAL_GATES_BUNDLE_BASE64", "")
    public_key = os.environ.get("EXTERNAL_GATES_PUBLIC_KEY", "").strip()
    if not public_key:
        raise SystemExit("external gate verification key is not configured")
    try:
        record = decode_external_gates_bundle(
            encoded,
            expected_sha=args.expected_sha,
            public_key=public_key,
        )
    except ValueError as error:
        raise SystemExit(f"external gate evidence rejected: {error}") from None

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
