#!/usr/bin/env python3
"""Run the complete pytest selection in bounded processes and combine coverage.

Every worker collects the same suite. Acceptance requires identical inventories
and exactly one assignment per collected node ID. No dependency/plugin is added;
the partition hook is loaded explicitly only in these worker processes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]


def verify_manifests(folder: Path, count: int) -> int:
    universe: list[str] | None = None
    assigned: list[str] = []
    for index in range(count):
        payload = json.loads((folder / f"shard-{index}.json").read_text(encoding="utf-8"))
        if payload["index"] != index or payload["count"] != count:
            raise ValueError("CI shard identity mismatch")
        all_ids, selected = payload["all"], payload["selected"]
        if not all_ids or all_ids != sorted(set(all_ids)):
            raise ValueError("CI collection is empty or has duplicate node IDs")
        if not selected or selected != sorted(set(selected)):
            raise ValueError("CI shard is empty or has duplicate node IDs")
        if universe is None:
            universe = all_ids
        elif all_ids != universe:
            raise ValueError("CI workers collected different inventories")
        assigned.extend(selected)
    if sorted(assigned) != universe:
        raise ValueError("CI partition omitted or duplicated collected tests")
    return len(assigned)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    pytest_args = args.pytest_args
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    parent = args.output_dir or Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="noyra-ci-", dir=parent)).resolve()
    print(f"CI test evidence: {folder}", flush=True)
    processes: list[subprocess.Popen[bytes]] = []
    logs: list[TextIO] = []
    codes: list[int] = []
    try:
        for index in range(args.workers):
            scratch = folder / f"tmp-{index}"
            scratch.mkdir()
            environment = dict(os.environ)
            environment.update(
                PYTHONPATH=os.pathsep.join(
                    (str(ROOT / "tests"), str(ROOT / "src"), environment.get("PYTHONPATH", ""))
                ),
                TEMP=str(scratch),
                TMP=str(scratch),
                TMPDIR=str(scratch),
                # Warm bytecode avoids reparsing the full application in
                # spawned deadline-bounded readers; keep it inside this shard.
                PYTHONDONTWRITEBYTECODE="",
                PYTHONPYCACHEPREFIX=str(scratch / "pycache"),
                PYTHONUTF8="1",
                PYTHONIOENCODING="utf-8",
                COVERAGE_FILE=str(folder / f".coverage.{index}"),
            )
            log = (folder / f"shard-{index}.log").open("w", encoding="utf-8")
            logs.append(log)
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-p",
                        "support.ci_shard",
                        f"--ci-shard-index={index}",
                        f"--ci-shard-count={args.workers}",
                        f"--ci-shard-manifest={folder / f'shard-{index}.json'}",
                        "--cov=noyra",
                        "--cov-report=",
                        "-p",
                        "no:cacheprovider",
                        *pytest_args,
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            )
        next_report = time.monotonic() + 60
        while any(process.poll() is None for process in processes):
            if time.monotonic() >= next_report:
                print("CI shard status:", [p.poll() for p in processes], flush=True)
                next_report = time.monotonic() + 60
            time.sleep(1)
        codes = [process.wait() for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()
    for index, code in enumerate(codes):
        print(f"\n=== CI shard {index}: exit {code} ===", flush=True)
        print((folder / f"shard-{index}.log").read_text(encoding="utf-8", errors="replace"))
    try:
        total = verify_manifests(folder, args.workers)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"CI inventory verification failed: {error}", file=sys.stderr)
        return 1
    print(f"CI inventory verified: {total} tests assigned exactly once", flush=True)
    # Require every coverage file explicitly: combine otherwise only warns and
    # skips unreadable files, which must never look like full-suite coverage.
    from coverage import CoverageData

    for index in range(args.workers):
        data = CoverageData(basename=str(folder / f".coverage.{index}"))
        data.read()
        if not data.measured_files():
            print(f"Missing coverage data from CI shard {index}", file=sys.stderr)
            return 1
    environment = dict(os.environ, COVERAGE_FILE=str(folder / ".coverage"))
    merged = subprocess.run(
        [sys.executable, "-m", "coverage", "combine", "--keep", str(folder)],
        cwd=ROOT,
        env=environment,
        check=False,
    )
    if merged.returncode:
        return 1
    report = subprocess.run(
        [sys.executable, "-m", "coverage", "report", "--show-missing"],
        cwd=ROOT,
        env=environment,
        check=False,
    )
    return int(any(codes) or report.returncode != 0)


if __name__ == "__main__":
    raise SystemExit(main())
