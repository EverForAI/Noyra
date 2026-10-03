from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
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
    artifact = b"sqlite-fixture"
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
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "artifact_byte_size": len(artifact),
        "artifact_format": "sqlite",
    }


def test_runner_accepts_bound_receipts_and_consumes_request(tmp_path: Path) -> None:
    module = _module()
    module.DATA_ROOT = tmp_path
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    request = _request()
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    database = artifact_dir / "task-artifact.bin"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE runtime_state(subject_id TEXT NOT NULL)")
        connection.execute("INSERT INTO runtime_state(subject_id) VALUES (?)", ("Noyra-0001",))
    artifact = database.read_bytes()
    request["artifact_sha256"] = hashlib.sha256(artifact).hexdigest()
    request["artifact_byte_size"] = len(artifact)
    (artifact_dir / "artifact-1.bin").write_bytes(artifact)
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "epoch").write_text("runtime-1", encoding="ascii")
    (request_dir / "task-1.json").write_text(json.dumps(request), encoding="utf-8")

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
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "artifact-1.bin").write_bytes(b"x")
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "epoch").write_text("runtime-1", encoding="ascii")
    request["health_report"] = {**request["health_report"], "checks": {"database": False}}
    (request_dir / "task-1.json").write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(module.RunnerError, match="health_checks_failed"):
        module.run_request("task-1", "restore")

    request = _request()
    request["artifact_sha256"] = hashlib.sha256(b"x").hexdigest()
    request["artifact_byte_size"] = 1
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
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "artifact-1.bin").write_bytes(b"x")
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "epoch").write_text("runtime-1", encoding="ascii")
    request["fence_proof"] = {**request["fence_proof"], "source_epoch": "runtime-2"}
    (request_dir / "task-1.json").write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(module.RunnerError, match="fence_proof_binding_invalid"):
        module.run_request("task-1", "fence")


def test_runner_fence_creates_runtime_marker_and_unfence_requires_same_epoch(
    tmp_path: Path,
) -> None:
    module = _module()
    module.DATA_ROOT = tmp_path
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    request = _request()
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "epoch").write_text("runtime-1", encoding="ascii")
    (request_dir / "task-1.json").write_text(json.dumps(request), encoding="utf-8")

    result = module.run_request("task-1", "fence")
    marker = tmp_path / "fences" / "task-1.json"
    assert result["status"] == "completed"
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "active"

    (request_dir / "task-1.json").write_text(json.dumps(request), encoding="utf-8")
    result = module.run_request("task-1", "unfence")
    assert result["status"] == "completed"
    assert not marker.exists()


def test_runner_unfence_rejects_changed_source_epoch(tmp_path: Path) -> None:
    module = _module()
    module.DATA_ROOT = tmp_path
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    request = _request()
    (tmp_path / "artifacts").mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "epoch").write_text("runtime-1", encoding="ascii")
    (request_dir / "task-1.json").write_text(json.dumps(request), encoding="utf-8")
    module.run_request("task-1", "fence")
    (source / "epoch").write_text("runtime-2", encoding="ascii")
    (request_dir / "task-1.json").write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(module.RunnerError, match="source_epoch_mismatch"):
        module.run_request("task-1", "unfence")
    assert (tmp_path / "fences" / "task-1.json").exists()


def test_runner_never_completes_from_receipts_without_real_artifact(tmp_path: Path) -> None:
    module = _module()
    module.DATA_ROOT = tmp_path
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    request = _request()
    (request_dir / "task-1.json").write_text(json.dumps(request), encoding="utf-8")

    with pytest.raises(module.RunnerError, match=r"(source|artifact)_directory_invalid"):
        module.run_request("task-1", "restore")
    assert not (tmp_path / "status" / "task-1.json").exists()
