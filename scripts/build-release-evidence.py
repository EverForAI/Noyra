#!/usr/bin/env python3
"""Write the non-secret release contract manifest consumed by the release job."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    from noyra.core.database import CURRENT_SCHEMA_VERSION
    from noyra.core.integrity import IntegrityRegistry

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = ROOT
    commit = os.environ.get("EXPECTED_SHA", "").strip()
    if not commit:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    payload = {
        "format": "noyra-release-evidence/v1",
        "commit_sha": commit,
        "release_tag": os.environ.get("RELEASE_TAG", "").strip() or None,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "integrity_registry_version": IntegrityRegistry.version,
        "feature_markers": {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "integrity_registry_version": IntegrityRegistry.version,
        },
        "wallet_gate_evidence": "artifacts/release/stage4b4/<commit_sha>/",
        "external_gates_required": True,
        "external_gates_file": "external-gates.json",
        "signature_artifacts": ["SHA256SUMS", "SHA256SUMS.sig", "SHA256SUMS.pem"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
