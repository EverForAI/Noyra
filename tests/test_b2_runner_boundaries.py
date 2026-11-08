from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from test_migration_runner import _module, _request

ROOT = Path(__file__).parents[1]


def runner_fixture(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    module = _module()
    module.DATA_ROOT = tmp_path
    for name in ("requests", "source", "artifacts", "status", "fences", "restored"):
        (tmp_path / name).mkdir(mode=0o700)
    (tmp_path / "source" / "epoch").write_text("runtime-1", encoding="ascii")
    request = _request()
    database = tmp_path / "artifacts" / "artifact-1.bin"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE runtime_state(subject_id TEXT NOT NULL)")
        connection.execute("INSERT INTO runtime_state VALUES ('Noyra-0001')")
    raw = database.read_bytes()
    request["artifact_sha256"] = hashlib.sha256(raw).hexdigest()
    request["artifact_byte_size"] = len(raw)
    return module, request


def write_request(tmp_path: Path, request: dict[str, Any]) -> None:
    (tmp_path / "requests" / "task-1.json").write_text(json.dumps(request), encoding="utf-8")


def test_root_runner_contains_no_application_import_even_for_encrypted_inputs() -> None:
    source = (ROOT / "scripts" / "noyra-migration-runner.py").read_text(encoding="utf-8")
    imports: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    assert all(name.split(".")[0] in sys.stdlib_module_names for name in imports)


def test_encrypted_root_request_fails_before_fencing_or_restoring(tmp_path: Path) -> None:
    module, request = runner_fixture(tmp_path)
    request["artifact_format"] = "noyra-encrypted-backup"
    write_request(tmp_path, request)
    with pytest.raises(module.RunnerError, match="requires_unprivileged_agent"):
        module.run_request("task-1", "restore")
    assert list((tmp_path / "fences").iterdir()) == []
    assert list((tmp_path / "restored").iterdir()) == []
    assert (tmp_path / "requests" / "task-1.json").is_file()


def test_restore_can_resume_after_database_publication_without_overwriting_it(
    tmp_path: Path,
) -> None:
    module, request = runner_fixture(tmp_path)
    write_request(tmp_path, request)
    original = module._write_json

    def fail_receipt(path: Path, value: dict[str, Any]) -> None:
        if path.parent == tmp_path / "status":
            raise OSError("synthetic interrupted receipt publication")
        original(path, value)

    with (
        patch.object(module, "_write_json", side_effect=fail_receipt),
        pytest.raises(OSError, match="interrupted receipt"),
    ):
        module.run_request("task-1", "restore")
    database = tmp_path / "restored" / "target-1" / "noyra.sqlite3"
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    assert (tmp_path / "requests" / "task-1.json").exists()
    result = module.run_request("task-1", "restore")
    assert result["status"] == "completed"
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before
    assert module.run_request("task-1", "status") == result


def test_restore_retry_refuses_existing_unrelated_database(tmp_path: Path) -> None:
    module, request = runner_fixture(tmp_path)
    write_request(tmp_path, request)
    root = tmp_path / "restored" / "target-1"
    root.mkdir()
    destination = root / "noyra.sqlite3"
    destination.write_bytes(b"not the approved database")
    with pytest.raises(module.RunnerError, match="restore_target_not_empty"):
        module.run_request("task-1", "restore")
    assert destination.read_bytes() == b"not the approved database"


def test_runner_unit_and_installer_have_matching_minimal_write_and_helper_contracts() -> None:
    unit = (ROOT / "deploy" / "systemd" / "noyra-migration-runner.service").read_text(
        encoding="utf-8"
    )
    paths = (
        next(line for line in unit.splitlines() if line.startswith("ReadWritePaths="))
        .split("=", 1)[1]
        .split()
    )
    assert set(paths) == {
        f"/var/lib/noyra/migration/{name}"
        for name in ("source", "fences", "requests", "status", "restored")
    }
    assert "User=root" in unit and "ProtectSystem=strict" in unit
    wrapper = (ROOT / "scripts" / "noyra-migration-runner.sh").read_text(encoding="utf-8")
    assert 'exec /usr/bin/python3 -I "$runner_python"' in wrapper
    assert 'runner_python="$SCRIPT_DIR/noyra-migration-runner.py"' in wrapper
    assert "NOYRA_PYTHON:-" not in wrapper and "NOYRA_MIGRATION_RUNNER_PY:-" not in wrapper
    installer = (ROOT / "scripts" / "install-ubuntu.sh").read_text(encoding="utf-8")
    assert '"$SOURCE_DIR/scripts/noyra-migration-runner.py" "$MIGRATION_RUNNER_PY"' in installer
    assert 'install -d -o root -g noyra -m 0750 "$DATA_DIR/migration/restored"' in installer
    for operation in ("snapshot", "restore"):
        block = installer.split("noyra_migration_components_" + operation, 1)[1].split("\n", 4)
        assert any('"$MIGRATION_RUNNER_PY"' in line for line in block)
    agent = (ROOT / "deploy" / "systemd" / "noyra-migration-agent.service").read_text(
        encoding="utf-8"
    )
    assert "User=noyra" in agent
    assert "/opt/noyra/current/.venv/bin/python" in agent
    assert "EnvironmentFile=-/etc/noyra/migration/agent.env" in agent
    assert "EnvironmentFile=/etc/noyra/noyra.env" not in agent


def test_real_encrypted_backup_restores_through_application_agent_without_root_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.core import SubjectKernel
    from noyra.core.at_rest import BackupKeyring, EncryptedBackupManager, VolumeEncryptionStatus
    from noyra.migration.agent import MigrationAgent

    source = tmp_path / "source"
    source.mkdir()
    kernel = SubjectKernel(source / "noyra.sqlite3", "Noyra-0001", "f" * 64)
    kernel.close()
    keyring = tmp_path / "keys" / "backup.json"
    BackupKeyring.initialize(keyring)
    archive = tmp_path / "synthetic.backup"
    EncryptedBackupManager(source, keyring).create(archive)
    root = tmp_path / "agent"
    restore = tmp_path / "restored"
    monkeypatch.setattr(
        "noyra.core.at_rest.VolumeEncryptionProbe.probe",
        lambda self, root, *, backend, attestation_path: VolumeEncryptionStatus(
            True, "synthetic", "test-only volume evidence", str(root)
        ),
    )
    agent = MigrationAgent(
        target_id="target-1",
        key_fingerprint="a" * 64,
        data_root=root,
        restore_root=restore,
        backup_manager=EncryptedBackupManager(root, keyring),
    )
    raw = archive.read_bytes()
    receipt = agent.receive(
        {
            "artifact_id": "encrypted-fixture",
            "subject_id": "Noyra-0001",
            "byte_size": len(raw),
            "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "artifact_format": "noyra-encrypted-backup",
        },
        artifact=raw,
    )
    report = agent.restore(receipt, task_id="task-backup-1")
    assert report.status == "restored" and report.restore_path is not None
    assert Path(report.restore_path).is_file()
    assert agent.validate(report)["status"] == "healthy"
    before = Path(report.restore_path).read_bytes()
    with pytest.raises(ValueError, match="encrypted backup restore failed"):
        agent.restore(receipt, task_id="task-backup-1")
    assert Path(report.restore_path).read_bytes() == before
