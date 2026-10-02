from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


def _module() -> Any:
    path = Path(__file__).parents[1] / "scripts" / "noyra-migration-runner.py"
    spec = importlib.util.spec_from_file_location("noyra_migration_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request() -> dict[str, Any]:
    manifest = "a" * 64
    restore = {
        "target_id": "target-1",
        "artifact_id": "artifact-1",
        "manifest_digest": manifest,
        "status": "restored",
    }
    health = {
        **restore,
        "status": "healthy",
        "host_identity": "host-1",
        "checks": {"database": True, "runtime": True},
    }
    return {
        "request_id": "task-1",
        "task_id": "task-1",
        "subject_id": "Noyra-0001",
        "target_id": "target-1",
        "source_epoch": "runtime-1",
        "manifest_digest": manifest,
        "artifact_id": "artifact-1",
        "restore_report": restore,
        "health_report": health,
        "fence_proof": {
            "task_id": "task-1",
            "subject_id": "Noyra-0001",
            "target_id": "target-1",
            "source_epoch": "runtime-1",
            "target_epoch_id": "epoch-1",
            "epoch_number": 1,
            "status": "active",
        },
    }


def test_runner_accepts_bound_receipts_and_consumes_request(tmp_path: Path) -> None:
    module = _module()
    module.DATA_ROOT = tmp_path
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    (request_dir / "task-1.json").write_text(json.dumps(_request()), encoding="utf-8")

    result = module.run_request("task-1", "restore")

    assert result["status"] == "completed"
    assert result["target_epoch_id"] == "epoch-1"
    assert not (request_dir / "task-1.json").exists()
    status = json.loads((tmp_path / "status" / "task-1.json").read_text(encoding="utf-8"))
    assert status["health_report_digest"] == result["health_report_digest"]
    assert module.run_request("task-1", "status") == status


def test_runner_rejects_unhealthy_or_secret_bearing_request(tmp_path: Path) -> None:
    module = _module()
    module.DATA_ROOT = tmp_path
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    request = _request()
    request["health_report"] = {**request["health_report"], "checks": {"database": False}}
    (request_dir / "task-1.json").write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(module.RunnerError, match="health_checks_failed"):
        module.run_request("task-1", "restore")

    request = _request()
    request["session_token"] = "should-never-be-in-a-request"
    (request_dir / "task-1.json").write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(module.RunnerError, match="secret"):
        module.run_request("task-1", "restore")


def test_runner_fence_operation_requires_task_bound_epoch(tmp_path: Path) -> None:
    module = _module()
    module.DATA_ROOT = tmp_path
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    request = _request()
    request["fence_proof"] = {**request["fence_proof"], "source_epoch": "runtime-2"}
    (request_dir / "task-1.json").write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(module.RunnerError, match="fence_proof_binding_invalid"):
        module.run_request("task-1", "fence")
