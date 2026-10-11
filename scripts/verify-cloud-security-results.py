from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

REQUIRED_CASES = {
    (
        "tests.test_runtime_logging",
        "test_actual_s3_signing_endpoint_logs_and_history_are_safe[200]",
    ),
    (
        "tests.test_runtime_logging",
        "test_actual_s3_signing_endpoint_logs_and_history_are_safe[403]",
    ),
    (
        "tests.test_cloud_dependency_compatibility",
        "test_locked_cloud_sdk_reads_and_checks_archive_without_network",
    ),
}


def verify(path: Path) -> None:
    """Require real SDK cases to pass; summary counts alone cannot prove this."""
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases or any(
        child.tag in {"skipped", "failure", "error"} for case in cases for child in case
    ):
        raise ValueError("cloud security cases are empty, skipped or failed")
    observed = [(case.get("classname"), case.get("name")) for case in cases]
    if any(observed.count(required) != 1 for required in REQUIRED_CASES):
        raise ValueError("required cloud SDK cases are missing or duplicated")
    for suite in root.iter("testsuite"):
        if any(int(suite.get(field, "0")) for field in ("skipped", "failures", "errors")):
            raise ValueError("cloud security suite is incomplete")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify(args.path)
    except (OSError, ValueError, ET.ParseError):
        print("cloud_security=failed reason=incomplete_results")
        return 1
    print("cloud_security=passed required_sdk_cases=3 skipped=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
