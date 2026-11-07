#!/usr/bin/env python3
"""Explain one local motivation review without modifying data or contacting a provider."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from noyra.cognition.decision_evidence import diagnose_motivation_review
from noyra.core.database import Database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--review", required=True)
    args = parser.parse_args()
    database = Database(args.database, initialize=False, read_only=True)
    result = diagnose_motivation_review(database, args.subject, args.review)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
