from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def gate() -> Any:
    script = Path(__file__).resolve().parents[1] / "scripts" / "audit-wallet-stage4b4.py"
    spec = importlib.util.spec_from_file_location("wallet_gate_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configure(gate: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "EVIDENCE_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(gate, "_short_runtime_root", lambda: tmp_path / "runtime")
    monkeypatch.setattr(gate, "_source_state", lambda: ("a" * 40, False))
    monkeypatch.setattr(
        gate, "parse_args", lambda: SimpleNamespace(scope="full", profile="pressure")
    )


def _run_record(tmp_path: Path) -> tuple[Path, Any]:
    paths = list((tmp_path / "evidence").glob("*/*/run.json"))
    assert len(paths) == 1
    return paths[0].parent, json.loads(paths[0].read_text(encoding="utf-8"))


def test_dirty_tree_is_rejected_before_tests_and_recorded(
    gate: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(gate, tmp_path, monkeypatch)
    monkeypatch.setattr(gate, "_source_state", lambda: ("a" * 40, True))

    def unexpected_run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("dirty working tree must not run any tests")

    monkeypatch.setattr(gate, "run", unexpected_run)
    with pytest.raises(RuntimeError, match="dirty working tree"):
        gate.main()
    _directory, record = _run_record(tmp_path)
    assert record["working_tree_dirty"] is True
    assert record["status"] == "failed"
    assert record["finished_at"]
    assert record["commands"] == []


@pytest.mark.parametrize("failed", [False, True])
def test_cleanup_failure_does_not_replace_original_gate_failure(
    gate: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    _configure(gate, tmp_path, monkeypatch)

    def failed_cleanup() -> None:
        raise OSError("cleanup blocked")

    monkeypatch.setattr(
        gate.tempfile,
        "TemporaryDirectory",
        lambda **kwargs: SimpleNamespace(name=str(tmp_path), cleanup=failed_cleanup),
    )
    record: dict[str, object] = {}
    with (
        pytest.raises(RuntimeError if failed else OSError, match=r"test failed|cleanup blocked"),
        gate._runtime_directory(record),
    ):
        if failed:
            raise RuntimeError("test failed")
    assert record["cleanup_error"] == "OSError: cleanup blocked"
    assert record["runtime_directory"] == str(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows path length contract")
def test_runtime_path_budget_allows_deep_prototype_files_and_cleanup(gate: Any) -> None:
    record: dict[str, object] = {}
    with gate._runtime_directory(record) as run_root:
        temp_root = run_root / "t"
        temp_root.mkdir()
        with tempfile.TemporaryDirectory(dir=temp_root) as directory:
            path = (
                Path(directory)
                / "workspace"
                / ("subject_" + "a" * 32)
                / ("project_" + "b" * 32)
                / "phases"
                / ("phase_" + "c" * 32)
                / "prototype-generations"
                / ("pexec_" + "d" * 32)
                / "prototype-tests.json"
            )
            assert len(str(path)) < 256
            path.parent.mkdir(parents=True)
            path.write_text("{}")
        assert not Path(directory).exists()
    assert not run_root.exists()


@pytest.mark.parametrize("failure", ["targeted", "full", "resource", "sampling", "source"])
def test_failures_keep_diagnostic_evidence(
    gate: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _configure(gate, tmp_path, monkeypatch)
    calls: list[Path] = []

    def fake_run(
        command: list[str],
        environment: dict[str, str],
        *,
        log_path: Path,
        measurement_root: Path,
        commit: str,
    ) -> dict[str, Any]:
        assert environment["TMP"] == environment["TEMP"] == str(measurement_root)
        assert environment["PYTEST_DEBUG_TEMPROOT"] == str(measurement_root)
        calls.append(log_path)
        failed = (failure == "targeted" and len(calls) == 1) or (
            failure == "full" and len(calls) == 3
        )
        result = {
            "command": command,
            "commit_sha": commit,
            "exit_code": 7 if failed else 0,
            "status": "failed" if failed else "passed",
            "started_at": "start",
            "finished_at": "end",
            "peak_rss_bytes": 1,
            "rss_samples": 1,
            "peak_database_bytes": 2,
            "peak_wal_bytes": 3,
        }
        if failure == "resource":
            result["peak_wal_bytes"] = 10 * 1024**3
        if failure == "sampling":
            result["rss_samples"] = 0
        if failure == "source":
            monkeypatch.setattr(gate, "_source_state", lambda: ("b" * 40, False))
        log_path.write_text("complete command output\n", encoding="utf-8")
        gate._write_json(log_path.with_suffix(".json"), result)
        return result

    monkeypatch.setattr(gate, "run", fake_run)
    with pytest.raises(RuntimeError):
        gate.main()
    directory, record = _run_record(tmp_path)
    assert record["status"] == "failed"
    assert record["finished_at"] and record["error"]
    assert record["versions"]["python"]
    assert (directory / "pressure.txt").is_file()
    metrics = json.loads((directory / "pressure-metrics.json").read_text())
    if failure in {"resource", "sampling"}:
        assert metrics["violations"]
    if failure == "full":
        assert len(record["commands"]) == 3
        assert record["commands"][-1]["exit_code"] == 7
        assert (directory / "full.txt").read_text().count("complete command output") == 2


def test_process_failure_and_large_output_are_persisted(gate: Any, tmp_path: Path) -> None:
    log_path = tmp_path / "failed.txt"
    command = [
        sys.executable,
        "-c",
        "import sys; print('start'); print('x'*2000000); print('end'); sys.exit(9)",
    ]
    result = gate.run(
        command, dict(os.environ), log_path=log_path, measurement_root=tmp_path, commit="commit"
    )
    assert result["exit_code"] == 9 and result["status"] == "failed"
    output = log_path.read_text(encoding="utf-8")
    assert "start\n" in output and "end\n" in output
    assert output.count("x") >= 2000000
    metadata = json.loads(log_path.with_suffix(".json").read_text())
    assert metadata["exit_code"] == 9 and metadata["finished_at"]


def test_launch_failure_still_records_command_and_error(gate: Any, tmp_path: Path) -> None:
    log_path = tmp_path / "launch.txt"
    result = gate.run(
        [str(tmp_path / "nonexistent-executable")],
        dict(os.environ),
        log_path=log_path,
        measurement_root=tmp_path,
        commit="commit",
    )
    assert result["status"] == "failed" and result["exit_code"] is None
    assert result["error"]
    assert log_path.is_file() and log_path.with_suffix(".json").is_file()


def test_rss_includes_actual_python_worker_behind_venv_launcher(gate: Any, tmp_path: Path) -> None:
    result = gate.run(
        [sys.executable, "-c", "import time; memory=bytearray(64*1024**2); time.sleep(0.6)"],
        dict(os.environ),
        log_path=tmp_path / "memory.txt",
        measurement_root=tmp_path,
        commit="commit",
    )
    assert result["status"] == "passed"
    assert result["rss_samples"] > 0
    assert result["peak_rss_bytes"] >= 64 * 1024**2


def test_disk_samples_only_include_this_runs_root(gate: Any, tmp_path: Path) -> None:
    root = tmp_path / "current"
    root.mkdir()
    (tmp_path / "noyra.sqlite3").write_bytes(b"unrelated" * 1000)
    (root / "noyra.sqlite3").write_bytes(b"a" * 17)
    (root / "renamed.db").write_bytes(b"a" * 19)
    (root / "noyra.sqlite3-wal").write_bytes(b"a" * 11)
    assert gate._database_bytes(root) == (36, 11)


def test_linux_rss_samples_requested_pid(gate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(gate, "sys", SimpleNamespace(platform="linux"))
    paths: list[str] = []

    def read_status(path: Path, *args: Any, **kwargs: Any) -> str:
        paths.append(str(path))
        return "Name: pytest\nVmHWM: 2048 kB\n"

    monkeypatch.setattr(Path, "read_text", read_status)
    assert gate._process_rss_bytes(4321) == 2048 * 1024
    assert paths == [str(Path("/proc/4321/status"))]


def test_disk_traversal_permission_failure_is_not_silently_ignored(
    gate: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def inaccessible_walk(root: Path, *, onerror: Any) -> list[Any]:
        onerror(PermissionError("cannot measure database directory"))
        return []

    monkeypatch.setattr(gate.os, "walk", inaccessible_walk)
    with pytest.raises(PermissionError, match="cannot measure"):
        gate._database_bytes(tmp_path)


def test_sampling_failure_terminates_process_and_preserves_evidence(
    gate: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_sample(root: Path) -> tuple[int, int]:
        raise PermissionError("sampling blocked")

    monkeypatch.setattr(gate, "_database_bytes", failed_sample)
    log_path = tmp_path / "sampling.txt"
    result = gate.run(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        dict(os.environ),
        log_path=log_path,
        measurement_root=tmp_path,
        commit="commit",
    )
    assert result["status"] == "failed" and result["exit_code"] is not None
    assert "sampling blocked" in result["error"]
    assert json.loads(log_path.with_suffix(".json").read_text()) == result
    assert "sampling blocked" in log_path.read_text()


def test_git_check_detects_untracked_staged_and_unstaged_changes(
    gate: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "ROOT", tmp_path)

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("original")
    git("add", "tracked.txt")
    git(
        "-c",
        "user.name=Gate Test",
        "-c",
        "user.email=gate@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "fixture",
    )
    commit, dirty = gate._source_state()
    assert not dirty
    tracked.write_text("modified")
    with pytest.raises(RuntimeError, match="same clean commit"):
        gate._assert_source(commit)
    git("add", "tracked.txt")
    assert gate._source_state()[1]
    tracked.write_text("original")
    git("add", "tracked.txt")
    assert not gate._source_state()[1]
    (tmp_path / "untracked.txt").write_text("new")
    assert gate._source_state()[1]
