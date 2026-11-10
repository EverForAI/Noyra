"""Require the native root rotation case, rather than a skipped matrix placeholder."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

REQUIRED = (
    "tests.test_backup_key_cli",
    "test_root_explicit_api_retains_group_read_and_rejects_retried_altered_expectation",
)


def verify(path: Path) -> None:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if len(cases) != 1 or (cases[0].get("classname"), cases[0].get("name")) != REQUIRED:
        raise ValueError("required native root case is missing or duplicated")
    if any(child.tag in {"skipped", "failure", "error"} for child in cases[0]):
        raise ValueError("native root case did not pass")
    suites = list(root.iter("testsuite"))
    if not suites or sum(int(suite.get("tests", "0")) for suite in suites) != 1:
        raise ValueError("invalid native root suite count")
    if any(
        int(suite.get(field, "0"))
        for suite in suites
        for field in ("skipped", "failures", "errors")
    ):
        raise ValueError("native root suite is incomplete")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify(args.path)
    except (OSError, ValueError, ET.ParseError):
        print("root_permission=failed reason=incomplete_results")
        return 1
    print("root_permission=passed required_cases=1 skipped=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
