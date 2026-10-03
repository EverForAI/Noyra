from __future__ import annotations

import base64
import hashlib
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.core.events import EventStore
from noyra.core.lifecycle import LifecycleManager
from noyra.core.types import content_hash
from noyra.migration.activation import (
    TargetActivationError,
    TargetRuntimeActivator,
    run_target_activation_requests,
)
from noyra.migration.fencing import EpochLease
from noyra.migration.manager import MigrationManager
from noyra.migration.policy import MigrationStore
from noyra.migration.targets import TargetRegistry

SUBJECT_ID = "Noyra-0001"
TARGET_ID = "target-1"
MANIFEST_DIGEST = "a" * 64
ARTIFACT_ID = "artifact-1"


class _Systemd:
    def __init__(self, ready: Any | None = None, *, start_error: bool = False) -> None:
        self.active = True
        self.calls: list[str] = []
        self.ready = ready or (lambda subject, target: True)
        self.start_error = start_error

    def stop(self) -> None:
        self.calls.append("stop")
        self.active = False

    def start(self) -> None:
        self.calls.append("start")
        if self.start_error:
            self.start_error = False
            self.active = False
            raise TargetActivationError("service_start_failed")
        self.active = True

    def daemon_reload(self) -> None:
        self.calls.append("reload")

    def is_active(self) -> bool:
        return self.active

    def wait_ready(self, subject_id: str, target_id: str | None, timeout: float) -> bool:
        self.calls.append(f"ready:{subject_id}:{target_id}")
        return bool(self.ready(subject_id, target_id))


def _prepared_target_database(path: Path) -> tuple[str, str]:
    database = Database(path)
    IdentityStore(database).ensure(SUBJECT_ID, content_hash({"subject": SUBJECT_ID}))
    LifecycleManager(database, EventStore(database), SUBJECT_ID).ensure_initial()
    store = MigrationStore(database)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    targets = TargetRegistry(database, store)
    targets.register(
        SUBJECT_ID,
        target_id=TARGET_ID,
        public_key=base64.urlsafe_b64encode(public).decode(),
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    challenge = targets.issue_challenge(TARGET_ID, source_epoch="runtime-0")
    targets.attest(
        TARGET_ID,
        challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode(),
        actor="operator",
    )
    policy = store.read_policy(SUBJECT_ID)
    policy = store.update_policy(SUBJECT_ID, policy.revision, {"enabled": True}, "operator")
    manager = MigrationManager(database, store)
    proposal = manager.create_proposal(
        subject_id=SUBJECT_ID,
        target_id=TARGET_ID,
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="approved migration fixture",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(
        proposal.proposal_id, actor="operator", idempotency_key="activation-test"
    )
    epoch = EpochLease._acquire_unchecked(
        database,
        SUBJECT_ID,
        TARGET_ID,
        expected_source_epoch=task.source_epoch,
        actor="test",
    )
    manager.transition_task(
        task.task_id,
        "preparing",
        actor="test",
        target_epoch_id=epoch.epoch_id,
    )
    manager.transition_task(
        task.task_id,
        "transferring",
        actor="test",
        manifest_digest=MANIFEST_DIGEST,
        artifact_id=ARTIFACT_ID,
    )
    manager.transition_task(task.task_id, "restoring", actor="test")
    manager.transition_task(task.task_id, "validating", actor="test")
    return task.task_id, content_hash(
        {
            "task_id": task.task_id,
            "source_epoch": task.source_epoch,
            "epoch_id": epoch.epoch_id,
            "epoch_number": epoch.epoch_number,
            "status": "active",
        }
    )


def _request(task_id: str, source_fence_digest: str) -> dict[str, str]:
    return {
        "task_id": task_id,
        "subject_id": SUBJECT_ID,
        "target_id": TARGET_ID,
        "source_epoch": "runtime-0",
        "manifest_digest": MANIFEST_DIGEST,
        "artifact_id": ARTIFACT_ID,
        "health_report_digest": "b" * 64,
        "source_fence_digest": source_fence_digest,
    }


def _runtime_fixture(tmp_path: Path, *, ready: Any | None = None) -> tuple[Any, ...]:
    data_root = tmp_path / "var-lib-noyra"
    task_id, fence_digest = _prepared_target_database(tmp_path / "target.sqlite3")
    restored = data_root / "migration-agent" / "restored" / task_id / "noyra.sqlite3"
    restored.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tmp_path / "target.sqlite3", restored)

    active_database = data_root / "noyra.sqlite3"
    active_database.parent.mkdir(parents=True, exist_ok=True)
    old_database = Database(active_database)
    IdentityStore(old_database).ensure(SUBJECT_ID, content_hash({"subject": SUBJECT_ID}))
    LifecycleManager(old_database, EventStore(old_database), SUBJECT_ID).ensure_initial()
    previous_digest = hashlib.sha256(active_database.read_bytes()).hexdigest()

    systemd = _Systemd(ready)
    activator = TargetRuntimeActivator(
        data_root,
        tmp_path / "etc-noyra",
        systemd=systemd,
        ready_timeout_seconds=0.01,
    )
    return task_id, fence_digest, active_database, previous_digest, systemd, activator


def test_target_activation_switches_to_the_restored_subject_database(tmp_path: Path) -> None:
    task_id, fence_digest, active_database, _, systemd, activator = _runtime_fixture(tmp_path)

    receipt = activator.activate(_request(task_id, fence_digest))

    assert receipt["status"] == "active"
    assert receipt["service_unit"] == "noyra.service"
    assert receipt["source_fence_digest"] == fence_digest
    assert (
        receipt["active_database_sha256"]
        == hashlib.sha256(active_database.read_bytes()).hexdigest()
    )
    assert systemd.calls[-2:] == ["start", f"ready:{SUBJECT_ID}:{TARGET_ID}"]
    with sqlite3.connect(active_database) as connection:
        task = connection.execute(
            "SELECT status FROM migration_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    assert task == ("committed",)


def test_controller_recovery_state_is_outside_the_agent_writable_root(tmp_path: Path) -> None:
    data_root = tmp_path / "var-lib-noyra"
    activator = TargetRuntimeActivator(data_root, tmp_path / "etc-noyra", systemd=_Systemd())

    assert activator.rollback_root.is_relative_to(data_root / "migration-agent") is False
    assert activator.activation_root.is_relative_to(data_root / "migration-agent") is False


def test_target_activation_reverts_the_previous_runtime_after_readiness_failure(
    tmp_path: Path,
) -> None:
    task_id, fence_digest, active_database, old_digest, systemd, activator = _runtime_fixture(
        tmp_path,
        ready=lambda subject, target: target is None,
    )

    with pytest.raises(TargetActivationError, match="target_runtime_readiness_failed"):
        activator.activate(_request(task_id, fence_digest))

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.active is True
    assert systemd.calls[-2:] == ["start", f"ready:{SUBJECT_ID}:None"]


def test_activation_rejects_wrong_subject_before_touching_current_runtime(tmp_path: Path) -> None:
    task_id, fence_digest, active_database, old_digest, systemd, activator = _runtime_fixture(
        tmp_path
    )
    request = _request(task_id, fence_digest) | {"subject_id": "Noyra-9999"}

    with pytest.raises(TargetActivationError, match="restored_database_identity_invalid"):
        activator.activate(request)

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.calls == []


def test_activation_rejects_mismatched_source_fence_before_switch(tmp_path: Path) -> None:
    task_id, _, active_database, old_digest, systemd, activator = _runtime_fixture(tmp_path)

    with pytest.raises(TargetActivationError, match="restored_migration_epoch_invalid"):
        activator.activate(_request(task_id, "f" * 64))

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.calls == []


def test_controller_revalidates_the_root_owned_staged_copy(tmp_path: Path, monkeypatch) -> None:
    task_id, fence_digest, active_database, old_digest, systemd, activator = _runtime_fixture(
        tmp_path
    )
    wrong_path = tmp_path / "wrong.sqlite3"
    wrong_database = Database(wrong_path)
    wrong_subject = "Noyra-9999"
    IdentityStore(wrong_database).ensure(wrong_subject, content_hash({"subject": wrong_subject}))
    LifecycleManager(wrong_database, EventStore(wrong_database), wrong_subject).ensure_initial()

    def substitute_copy(source: Path, destination: Path) -> None:
        shutil.copyfile(wrong_path, destination)

    monkeypatch.setattr("noyra.migration.activation._copy_private", substitute_copy)

    with pytest.raises(TargetActivationError, match="restored_database_identity_invalid"):
        activator.activate(_request(task_id, fence_digest))

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.calls == []


def test_activation_reverts_previous_runtime_after_service_start_failure(tmp_path: Path) -> None:
    task_id, fence_digest, active_database, old_digest, _, activator = _runtime_fixture(tmp_path)
    systemd = _Systemd(start_error=True)
    activator.systemd = systemd

    with pytest.raises(TargetActivationError, match="service_start_failed"):
        activator.activate(_request(task_id, fence_digest))

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.active is True
    assert systemd.calls[-3:] == ["reload", "start", f"ready:{SUBJECT_ID}:None"]


def test_duplicate_activation_returns_same_receipt_without_restarting(tmp_path: Path) -> None:
    task_id, fence_digest, _, _, systemd, activator = _runtime_fixture(tmp_path)

    first = activator.activate(_request(task_id, fence_digest))
    calls = list(systemd.calls)
    second = activator.activate(_request(task_id, fence_digest))

    assert second == first
    assert systemd.calls == calls


def test_deactivation_is_idempotent_and_restores_previous_database(tmp_path: Path) -> None:
    task_id, fence_digest, active_database, old_digest, systemd, activator = _runtime_fixture(
        tmp_path
    )
    activator.activate(_request(task_id, fence_digest))
    deactivate_request = {
        "task_id": task_id,
        "target_id": TARGET_ID,
        "source_epoch": "runtime-0",
        "manifest_digest": MANIFEST_DIGEST,
    }

    first = activator.deactivate(deactivate_request)
    calls = list(systemd.calls)
    second = activator.deactivate(deactivate_request)

    assert first == {"status": "deactivated", "task_id": task_id}
    assert second == first
    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.calls == calls


def test_deactivation_readiness_failure_leaves_durable_recovery_state(tmp_path: Path) -> None:
    task_id, fence_digest, _, _, _, activator = _runtime_fixture(tmp_path)
    activator.activate(_request(task_id, fence_digest))
    activator.systemd = _Systemd(ready=lambda subject, target: target == TARGET_ID)

    with pytest.raises(TargetActivationError, match="rollback_runtime_readiness_failed"):
        activator.deactivate(
            {
                "task_id": task_id,
                "target_id": TARGET_ID,
                "source_epoch": "runtime-0",
                "manifest_digest": MANIFEST_DIGEST,
            }
        )

    marker = activator.activation_root / f"{task_id}.json"
    import json

    assert json.loads(marker.read_text())["status"] == "deactivating"


def test_root_activation_runner_rejects_unsigned_request_and_persists_failure(
    tmp_path: Path,
) -> None:
    from noyra.migration.activation import _atomic_json

    root = tmp_path / "target-activation"
    requests = root / "requests"
    requests.mkdir(parents=True)
    statuses = root / "status"
    statuses.mkdir()
    identity_file = tmp_path / "identity.json"
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    identity_file.write_text(
        '{"target_id":"target-1","public_key":"' + base64.urlsafe_b64encode(public).decode() + '"}'
    )
    task_id = "migrationtask_unsigned"
    _atomic_json(
        requests / f"{task_id}.activate.json",
        {
            "payload": _request(task_id, "c" * 64),
            "signature": "invalid",
            "request_digest": content_hash(_request(task_id, "c" * 64)),
        },
        0o600,
    )

    assert (
        run_target_activation_requests(
            root=root,
            identity_file=identity_file,
            data_root=tmp_path / "var-lib-noyra",
            config_root=tmp_path / "etc-noyra",
            systemd=_Systemd(),
        )
        == 1
    )
    status = statuses / f"{task_id}.activate.json"
    assert status.is_file()
    import json

    assert json.loads(status.read_text())["error_code"] == "activation_request_signature_invalid"
    assert status.is_file(), "root runner result must remain available for audit"


def test_activation_systemd_units_use_fixed_entrypoint_and_recovery_precedes_service() -> None:
    from pathlib import Path

    deploy = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
    runner = (deploy / "noyra-target-activation.service").read_text(encoding="utf-8")
    path = (deploy / "noyra-target-activation.path").read_text(encoding="utf-8")
    recovery = (deploy / "noyra-target-activation-recover.service").read_text(encoding="utf-8")
    service = (deploy / "noyra.service").read_text(encoding="utf-8")
    installer = (deploy.parents[1] / "scripts" / "install-ubuntu.sh").read_text(encoding="utf-8")

    assert "User=root" in runner
    assert "noyra-target-activation-runner.py" in runner
    assert "ExecStart=" in runner and "systemctl" not in runner
    assert "PathChanged=/var/lib/noyra/migration/target-activation/requests" in path
    assert "noyra-target-activation-recover.service" in service
    assert "noyra-target-activation-runner.py" in recovery
    assert "--recover" in recovery
    assert "noyra-target-activation.path" in installer
    assert "enable --now noyra-target-activation.path" in installer
    assert (
        'install -d -o root -g root -m 0700 "$DATA_DIR/migration/target-activation/state"'
        in installer
    )
