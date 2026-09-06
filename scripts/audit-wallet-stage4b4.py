from __future__ import annotations

import argparse
import datetime as dt
import importlib.metadata
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = ROOT / ".runtime" / "wallet-stage4b4"
EVIDENCE_ROOT = ROOT / "artifacts" / "release" / "stage4b4"
RESOURCE_LIMITS = {
    "smoke": {"rss": 512 * 1024**2, "wal": 512 * 1024**2, "database": 2 * 1024**3},
    "pressure": {"rss": 768 * 1024**2, "wal": 1024**3, "database": 4 * 1024**3},
    "soak": {"rss": 1024**3, "wal": 2 * 1024**3, "database": 8 * 1024**3},
}
TARGETED_TESTS = (
    "tests/test_wallet_release_gates.py",
    "tests/test_wallet_schema60.py",
    "tests/test_wallet_gate_runner.py",
    "tests/test_wallet.py::test_wallet_integrity_registry_detects_audit_and_state_tampering",
    "tests/test_wallet.py::test_wallet_execution_http_is_fail_closed_without_signer",
    "tests/test_wallet.py::test_wallet_diagnostics_exposes_bounded_state_without_wallet_secrets",
    "tests/test_wallet.py::test_wallet_acquisition_operator_api_is_bounded_and_redacts_leases",
    "tests/test_wallet_execution.py::test_broadcast_response_loss_is_unknown_and_requires_explicit_retry",
    "tests/test_wallet_execution.py::test_chain_failure_can_be_refunded_once",
    "tests/test_wallet_execution.py::test_signer_exception_is_classified_without_persisting_secret",
    "tests/test_wallet_execution.py::test_execution_integrity_consumes_history_without_fetchall",
    "tests/test_wallet_reward_workflow.py::test_unknown_broadcast_recovers_after_restart_and_settles_once",
    "tests/test_wallet_reward_workflow.py::test_confirmed_receipt_reorganization_opens_incident_before_completion",
    "tests/test_wallet_acquisition.py::test_lease_renewal_expiry_and_interrupted_recovery_require_explicit_retry",
    "tests/test_wallet_acquisition.py::test_claim_limits_concurrency_and_reserves_actual_request_budget",
    "tests/test_wallet_acquisition.py::test_runtime_export_and_store_integrity_include_acquisition_rows_without_cross_subject_leak",
    "tests/test_wallet_rpc.py::test_rpc_failures_are_bounded_and_sanitized",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _short_runtime_root() -> Path:
    """Keep pytest-created paths below Windows MAX_PATH while remaining isolated."""
    if os.name == "nt":
        drive = Path(tempfile.gettempdir()).anchor
        return Path(drive) / "ng"
    return TEMP_ROOT


@contextmanager
def _runtime_directory(record: dict[str, object]) -> Iterator[Path]:
    runtime_root = _short_runtime_root()
    runtime_root.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="", dir=runtime_root)
    record["runtime_directory"] = temporary.name
    try:
        yield Path(temporary.name)
    finally:
        already_failed = sys.exc_info()[0] is not None
        try:
            temporary.cleanup()
        except OSError as error:
            record["cleanup_error"] = f"{type(error).__name__}: {error}"
            if not already_failed:
                raise


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _source_state() -> tuple[str, bool]:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, text=True
        ).strip()
    )
    return commit, dirty


def _assert_source(commit: str) -> None:
    current, dirty = _source_state()
    if dirty or current != commit:
        raise RuntimeError("release gates require the same clean commit throughout the run")


def _versions() -> dict[str, str]:
    versions = {
        "python": sys.version,
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
    }
    for name in ("pytest", "ruff", "mypy", "pip", "httpx", "pydantic", "cryptography"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "missing"
    return versions


def _process_rss_bytes(pid: int) -> int | None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("page_fault_count", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "peak_working_set_size",
                    "working_set_size",
                    "quota_peak_paged_pool_usage",
                    "quota_paged_pool_usage",
                    "quota_peak_non_paged_pool_usage",
                    "quota_non_paged_pool_usage",
                    "pagefile_usage",
                    "peak_pagefile_usage",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return int(counters.peak_working_set_size)
            return None
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError):
            return None
    return None


def _database_bytes(root: Path) -> tuple[int, int]:
    database_bytes = 0
    wal_bytes = 0

    def traversal_error(error: OSError) -> None:
        # Test cleanup can remove directories while a sample is in progress.
        if not isinstance(error, FileNotFoundError):
            raise error

    for directory, _subdirectories, filenames in os.walk(root, onerror=traversal_error):
        for filename in filenames:
            if not filename.endswith((".sqlite3", ".sqlite", ".db", "-wal")):
                continue
            path = Path(directory) / filename
            try:
                if path.is_symlink():
                    continue
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if filename.endswith("-wal"):
                wal_bytes += size
            else:
                database_bytes += size
    return database_bytes, wal_bytes


def _process_tree_rss_bytes(pid: int) -> int | None:
    if os.name != "nt":
        return _process_rss_bytes(pid)

    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    children: dict[int, list[int]] = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while available:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            available = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(snapshot)

    # Windows venv python.exe is a redirector; pytest runs in its child process.
    pending = [pid]
    visited: set[int] = set()
    total = 0
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        rss = _process_rss_bytes(current)
        if rss is None:
            return None
        total += rss
        pending.extend(children.get(current, ()))
    return total


def run(
    command: list[str],
    environment: dict[str, str],
    *,
    log_path: Path,
    measurement_root: Path,
    commit: str,
) -> dict[str, object]:
    result: dict[str, object] = {
        "command": command,
        "commit_sha": commit,
        "started_at": _utc_now(),
        "finished_at": None,
        "exit_code": None,
        "status": "running",
        "peak_rss_bytes": 0,
        "peak_database_bytes": 0,
        "peak_wal_bytes": 0,
        "rss_samples": 0,
        "error": None,
    }
    print(f"+ {' '.join(command)}", flush=True)
    _write_json(log_path.with_suffix(".json"), result)
    process = None
    try:
        with log_path.open("w", encoding="utf-8") as output:
            output.write(json.dumps(result, sort_keys=True) + "\n")
            output.flush()
            process = subprocess.Popen(
                command, cwd=ROOT, env=environment, stdout=output, stderr=subprocess.STDOUT
            )
            while True:
                rss = _process_tree_rss_bytes(process.pid)
                if rss is not None:
                    result["rss_samples"] = int(result["rss_samples"]) + 1
                    result["peak_rss_bytes"] = max(int(result["peak_rss_bytes"]), rss)
                database_bytes, wal_bytes = _database_bytes(measurement_root)
                result["peak_database_bytes"] = max(
                    int(result["peak_database_bytes"]), database_bytes
                )
                result["peak_wal_bytes"] = max(int(result["peak_wal_bytes"]), wal_bytes)
                if process.poll() is not None:
                    break
                time.sleep(0.1)
            result["exit_code"] = process.returncode
            result["status"] = "passed" if process.returncode == 0 else "failed"
    except BaseException as error:
        result["status"] = "failed"
        result["error"] = f"{type(error).__name__}: {error}"
        if process is not None:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=10,
                    )
                process.kill()
            result["exit_code"] = process.wait()
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        result["finished_at"] = _utc_now()
        _write_json(log_path.with_suffix(".json"), result)
        with log_path.open("a", encoding="utf-8") as output:
            output.write("\n" + json.dumps(result, sort_keys=True) + "\n")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 4-B-4 clean-commit release gates")
    parser.add_argument("--scope", choices=("targeted", "full"), default="targeted")
    parser.add_argument("--profile", choices=("smoke", "pressure", "soak"), default="smoke")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    commit, dirty = _source_state()
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    evidence = EVIDENCE_ROOT / commit / run_id
    evidence.mkdir(parents=True, exist_ok=False)
    record = {
        "commit_sha": commit,
        "working_tree_dirty": dirty,
        "profile": args.profile,
        "scope": args.scope,
        "started_at": _utc_now(),
        "finished_at": None,
        "status": "running",
        "error": None,
        "commands": [],
        "versions": _versions(),
    }
    _write_json(evidence / "run.json", record)
    print(f"Evidence: {evidence}", flush=True)
    try:
        if dirty:
            raise RuntimeError("release gates refuse a dirty working tree")
        _assert_source(commit)
        with _runtime_directory(record) as run_root:
            temporary = run_root / "t"
            temporary.mkdir()
            environment = dict(os.environ)
            environment.update(
                {
                    "NOYRA_STAGE4B4_PROFILE": args.profile,
                    "TMP": str(temporary),
                    "TEMP": str(temporary),
                    "TMPDIR": str(temporary),
                    "PYTEST_DEBUG_TEMPROOT": str(temporary),
                    "PYTHONPYCACHEPREFIX": str(run_root / "pycache"),
                    "RUFF_CACHE_DIR": str(run_root / "ruff-cache"),
                    "MYPY_CACHE_DIR": str(run_root / "mypy-cache"),
                    "PIP_CACHE_DIR": str(run_root / "pip-cache"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                }
            )
            python = sys.executable

            def execute(command: list[str], name: str) -> dict[str, object]:
                _assert_source(commit)
                result = run(
                    command,
                    environment,
                    log_path=evidence / name,
                    measurement_root=temporary,
                    commit=commit,
                )
                record["commands"].append({"log": name, **result})
                _write_json(evidence / "run.json", record)
                return result

            name = "targeted.txt" if args.profile == "smoke" else "pressure.txt"
            targeted = execute(
                [python, "-m", "pytest", *TARGETED_TESTS, "-q", "-p", "no:cacheprovider"], name
            )
            violations = [
                metric
                for metric, key in (
                    ("peak_rss_bytes", "rss"),
                    ("peak_wal_bytes", "wal"),
                    ("peak_database_bytes", "database"),
                )
                if int(targeted[metric]) > RESOURCE_LIMITS[args.profile][key]
            ]
            if not targeted["rss_samples"] or not targeted["peak_database_bytes"]:
                violations.append("resource_samples_unavailable")
            metrics = {
                "profile": args.profile,
                "history_size": {"smoke": 16, "pressure": 64, "soak": 128}[args.profile],
                "limits": RESOURCE_LIMITS[args.profile],
                "violations": violations,
                "measurement_scope": (
                    "pytest PID (including Windows launcher descendants) "
                    "and isolated temporary directory"
                ),
                **{
                    key: targeted[key]
                    for key in (
                        "peak_rss_bytes",
                        "peak_wal_bytes",
                        "peak_database_bytes",
                        "rss_samples",
                    )
                },
            }
            _write_json(evidence / "pressure-metrics.json", metrics)
            if targeted["status"] != "passed" or violations:
                raise RuntimeError("targeted/pressure gate failed; see command log and metrics")
            _assert_source(commit)
            if args.scope == "full":
                with (evidence / "full.txt").open("w", encoding="utf-8") as full_log:
                    full_log.write(json.dumps(record["versions"], sort_keys=True) + "\n")
                    full_log.flush()
                    for index, command in enumerate(
                        (
                            [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                            [
                                python,
                                "-m",
                                "ruff",
                                "check",
                                "src",
                                "tests",
                                "scripts/audit-wallet-stage4b4.py",
                                "scripts/authorize-wallet-schema60.py",
                            ],
                            [
                                python,
                                "-m",
                                "ruff",
                                "format",
                                "--check",
                                "src",
                                "tests",
                                "scripts/audit-wallet-stage4b4.py",
                                "scripts/authorize-wallet-schema60.py",
                            ],
                            [python, "-m", "mypy"],
                            [
                                python,
                                "-m",
                                "compileall",
                                "-q",
                                "src",
                                "tests",
                                "scripts/audit-wallet-stage4b4.py",
                                "scripts/authorize-wallet-schema60.py",
                            ],
                            [python, "-m", "pip", "check"],
                            ["git", "diff", "--check"],
                        )
                    ):
                        name = f"full-{index:02d}.txt"
                        result = execute(command, name)
                        with (evidence / name).open(encoding="utf-8", errors="replace") as source:
                            shutil.copyfileobj(source, full_log)
                        full_log.flush()
                        if result["status"] != "passed":
                            raise RuntimeError(f"full gate failed: {name}")
                        _assert_source(commit)
        _assert_source(commit)
        record["status"] = "passed"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        record["finished_at"] = _utc_now()
        _write_json(evidence / "run.json", record)


if __name__ == "__main__":
    main()
