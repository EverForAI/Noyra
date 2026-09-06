from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = ROOT / ".runtime" / "m41"
TARGETED_TESTS = (
    "tests/test_m41_verification_foundation.py",
    "tests/test_m42_p1_01_sleep_deadlock.py",
    "tests/test_m42_p1_02_integrity_runtime.py",
    "tests/test_m42_p1_03_archive_integrity.py",
    "tests/test_m42_p1_04_runtime_export.py",
    "tests/test_m42_p1_05_training_consent.py",
    "tests/test_m42_p1_06_export_ownership.py",
    "tests/test_m42_p1_07_training_bounds.py",
    "tests/test_service.py::ServiceTestCase::test_dashboard_and_read_only_public_endpoints",
    "tests/test_service.py::ServiceTestCase::test_public_state_allowlist_and_unknown_lifecycle_fail_closed",
    "tests/test_service.py::ServiceTestCase::test_goals_endpoint_exposes_goal_state_without_private_pressure",
    "tests/test_service.py::ServiceTestCase::test_capability_api_rejects_unsupported_per_use_approval",
    "tests/test_service.py::ServiceTestCase::test_legacy_capability_is_listed_as_blocked_for_operator_recovery",
    "tests/test_capability.py",
    "tests/test_storage.py",
    "tests/test_m42_p2_01_training_workspace.py",
    "tests/test_m42_p2_02_behavior_reconciliation.py",
    "tests/test_m42_p2_03_http_bounds.py",
    "tests/test_m42_p2_16_export_snapshots.py",
    "tests/test_service.py::ServiceTestCase::test_legacy_export_route_enqueues_without_blocking_request_worker",
)


def run(command: list[str], environment: dict[str, str]) -> None:
    print(f"+ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, env=environment, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the cross-platform M41 verification gate.")
    parser.add_argument(
        "--scope",
        choices=("targeted", "full"),
        default="targeted",
        help="targeted runs M41 contracts; full also runs repository tests and static gates",
    )
    parser.add_argument(
        "--profile",
        choices=("smoke", "large", "soak"),
        default="smoke",
        help="synthetic history size used by the M41 test",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="audit-", dir=TEMP_ROOT) as directory:
        run_root = Path(directory)
        environment = dict(os.environ)
        environment.update(
            {
                "NOYRA_M41_PROFILE": args.profile,
                "TEMP": str(run_root / "tmp"),
                "TMP": str(run_root / "tmp"),
                "TMPDIR": str(run_root / "tmp"),
                "PYTHONPYCACHEPREFIX": str(run_root / "pycache"),
                "RUFF_CACHE_DIR": str(run_root / "ruff-cache"),
                "MYPY_CACHE_DIR": str(run_root / "mypy-cache"),
                "PIP_CACHE_DIR": str(run_root / "pip-cache"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        (run_root / "tmp").mkdir()

        python = sys.executable
        run(
            [python, "-m", "pytest", *TARGETED_TESTS, "-q", "-p", "no:cacheprovider"],
            environment,
        )
        if args.scope == "full":
            run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider"], environment)
            run([python, "-m", "ruff", "check", "."], environment)
            run([python, "-m", "ruff", "format", "--check", "."], environment)
            run([python, "-m", "mypy", "src", "tests"], environment)
            run([python, "-m", "compileall", "-q", "src", "tests"], environment)
            run([python, "-m", "pip", "check"], environment)
            run(
                [
                    python,
                    "-m",
                    "pip_audit",
                    "--no-deps",
                    "--disable-pip",
                    "-r",
                    "requirements.lock",
                ],
                environment,
            )


if __name__ == "__main__":
    main()
