from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from support.ci_shard import shard_index

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run-ci-tests.py"


def load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("noyra_ci_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("damage", ["none", "omitted", "duplicated", "different", "missing"])
def test_ci_inventory_cannot_accept_missing_or_duplicate_tests(tmp_path: Path, damage: str) -> None:
    first = {"index": 0, "count": 2, "all": ["a", "b"], "selected": ["a"]}
    second = {"index": 1, "count": 2, "all": ["a", "b"], "selected": ["b"]}
    if damage == "omitted":
        second["selected"] = []
    elif damage == "duplicated":
        second["selected"] = ["a"]
    elif damage == "different":
        second["all"] = ["a", "c"]
        second["selected"] = ["c"]
    (tmp_path / "shard-0.json").write_text(json.dumps(first), encoding="utf-8")
    if damage != "missing":
        (tmp_path / "shard-1.json").write_text(json.dumps(second), encoding="utf-8")
    runner = load_runner()
    if damage == "none":
        assert runner.verify_manifests(tmp_path, 2) == 2
    else:
        with pytest.raises((ValueError, OSError)):
            runner.verify_manifests(tmp_path, 2)


@pytest.mark.parametrize("fail", [False, True])
def test_ci_runner_runs_every_test_and_propagates_worker_failure(
    tmp_path: Path, fail: bool
) -> None:
    # Root the synthetic suite here so node IDs are identical on both platforms.
    names: list[str] = []
    for index in range(2):
        names.extend(
            f"test_case_{number}"
            for number in range(50)
            if shard_index(f"test_sample.py::test_case_{number}", 2) == index
        )
        names = names[: (index + 1) * 2]
    assert len(names) == len(set(names)) == 4
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "from noyra.core.types import content_hash\n\n"
        + "\n\n".join(
            f"def {name}():\n    assert len(content_hash({name!r})) == 64\n"
            + ("    assert False, 'intentional CI worker failure'\n" if fail and i == 0 else "")
            for i, name in enumerate(names)
        ),
        encoding="utf-8",
    )
    environment = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
    # Clear ancestor pytest workers' coverage startup hooks in this nested test.
    environment.pop("COVERAGE_PROCESS_START", None)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(RUNNER),
            "--workers",
            "2",
            "--output-dir",
            str(tmp_path / "evidence"),
            "--",
            str(sample),
            "--rootdir",
            str(tmp_path),
            "-q",
            "--tb=short",
        ],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == int(fail), result.stdout + result.stderr
    evidence = next((tmp_path / "evidence").glob("noyra-ci-*"))
    assert load_runner().verify_manifests(evidence, 2) == 4
    assert (evidence / ".coverage").is_file()
    assert "CI inventory verified: 4 tests assigned exactly once" in result.stdout
    if fail:
        assert "intentional CI worker failure" in result.stdout


def test_ci_job_keeps_matrix_and_runs_all_checks() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "os: [ubuntu-24.04, windows-2022]" in workflow
    assert 'python-version: ["3.11", "3.12"]' in workflow
    assert "python scripts/run-ci-tests.py --workers 2" in workflow
    for command in (
        "audit-m41.py --scope targeted --profile smoke",
        "ruff check .",
        "ruff format --check .",
        "mypy src tests",
        "pip_audit",
    ):
        assert command in workflow
