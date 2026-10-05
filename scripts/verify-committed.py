#!/usr/bin/env python3
"""Verify an exact commit in a temporary checkout without local experiments."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def export_commit(repository: Path, revision: str, destination: Path) -> str:
    sha = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=repository, text=True
    ).strip()
    # Independent metadata preserves the original SHA for release tests while
    # avoiding shared worktree/index state and dirty editable package sources.
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-local",
            "--no-checkout",
            str(repository),
            str(destination),
        ],
        check=True,
    )
    subprocess.run(["git", "checkout", "--quiet", "--detach", sha], cwd=destination, check=True)
    return sha


def verification_commands(scope: str, tests: list[str]) -> list[list[str]]:
    commands = [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "-m", "mypy", "src", "tests"],
        [sys.executable, "-m", "pip", "check"],
    ]
    if scope != "static":
        commands.append([sys.executable, "-m", "pytest", "-q", *tests])
    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--scope", choices=("static", "all"), default="all")
    parser.add_argument("--tests", nargs="*", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="noyra-committed-check-") as directory:
        checkout = Path(directory)
        sha = export_commit(ROOT, args.commit, checkout)
        for test in args.tests:
            relative = PurePosixPath(test)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not test.startswith("tests/")
                or not (checkout / test).is_file()
            ):
                parser.error("--tests must name test files included in the exact commit")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(checkout / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUTF8"] = "1"
        environment.pop("PYTEST_ADDOPTS", None)
        environment.pop("MYPYPATH", None)
        print(f"Verifying exact committed source: {sha}", flush=True)
        for command in verification_commands(args.scope, args.tests):
            print("Running " + " ".join(command[1:]), flush=True)
            completed = subprocess.run(command, cwd=checkout, env=environment, check=False)
            results.append({"command": command[1:], "exit_code": completed.returncode})
            if completed.returncode:
                break
    passed = all(result["exit_code"] == 0 for result in results)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "commit_sha": sha,
                    "scope": args.scope,
                    "tests": args.tests or "all",
                    "status": "passed" if passed else "failed",
                    "commands": results,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
