from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.migration.policy import MigrationStore
from noyra.migration.recovery import RecoveryCoordinator, RecoveryRequest
from noyra.migration.targets import TargetRegistry


def _setup(tmp_path: Any) -> Any:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-0001"
    IdentityStore(database).ensure(subject_id, "d" * 64)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO runtime_state(subject_id,state,reason,version,changed_at) "
            "VALUES (?,?,?,?,?)",
            (subject_id, "active", "test", 1, "2026-01-01T00:00:00+00:00"),
        )
    store = MigrationStore(database)
    policy = store.read_policy(subject_id).with_updates(
        enabled=True,
        approval_mode="emergency_recovery",
        emergency_recovery_enabled=True,
        allowed_target_ids=("standby-1",),
    )
    store.update_policy(
        subject_id,
        1,
        {
            "enabled": policy.enabled,
            "approval_mode": policy.approval_mode,
            "emergency_recovery_enabled": policy.emergency_recovery_enabled,
            "allowed_target_ids": policy.allowed_target_ids,
        },
        "operator",
    )
    private = Ed25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes_raw()
    public = base64.urlsafe_b64encode(public_bytes).decode("ascii")
    registry = TargetRegistry(database, store)
    target = registry.register(
        subject_id,
        target_id="standby-1",
        public_key=public,
        endpoint="https://standby.example/migration",
        capabilities={"encrypted_restore": True},
        region="test",
        provider="test",
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    challenge = registry.issue_challenge(target.target_id, source_epoch="runtime-1")
    signature = base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode("ascii")
    registry.attest(target.target_id, challenge, signature, actor="operator")
    backup_path = tmp_path / "backup.bin"
    backup_path.write_bytes(b"verified recovery backup")
    backup_hash = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    RecoveryCoordinator(database).register_backup(
        subject_id=subject_id,
        backup_id="backup-1",
        backup_path=str(backup_path),
        content_hash_value=backup_hash,
        byte_size=backup_path.stat().st_size,
        schema_version=CURRENT_SCHEMA_VERSION,
        genesis_hash="d" * 64,
        key_id="backup-key-1",
        keyring_generation=1,
    )
    return database, store.read_policy(subject_id), private, registry


def _request(private: Ed25519PrivateKey) -> RecoveryRequest:
    request = RecoveryRequest(
        task_id="recovery-task-1",
        standby_target_id="standby-1",
        verified_backup_id="backup-1",
        source_failure_evidence="source unreachable after health timeout",
        manifest_digest="a" * 64,
        restore_report_digest="b" * 64,
        health_report_digest="c" * 64,
        target_signature="placeholder",
    )
    signature = base64.urlsafe_b64encode(
        private.sign(RecoveryCoordinator.signing_bytes(request))
    ).decode("ascii")
    return replace(request, target_signature=signature)


def test_emergency_recovery_requires_verified_target_proof_and_acquires_epoch(
    tmp_path: Any,
) -> None:
    database, policy, private, _ = _setup(tmp_path)
    result = RecoveryCoordinator(database).restore_standby(_request(private), policy)

    assert result["status"] == "recovery_ready"
    assert result["epoch_id"]
    with database.connection() as connection:
        task = connection.execute(
            "SELECT status,target_epoch_id,manifest_digest,artifact_id "
            "FROM migration_tasks WHERE task_id=?",
            ("recovery-task-1",),
        ).fetchone()
        epoch = connection.execute(
            "SELECT status,target_id FROM migration_epochs WHERE epoch_id=?",
            (result["epoch_id"],),
        ).fetchone()
    assert task["status"] == "validating"
    assert task["target_epoch_id"] == result["epoch_id"]
    assert task["manifest_digest"] == "a" * 64
    assert task["artifact_id"] == "backup-1"
    assert epoch["status"] == "active"
    assert epoch["target_id"] == "standby-1"


def test_emergency_recovery_rejects_invalid_signature_without_creating_task(tmp_path: Any) -> None:
    database, policy, _, _ = _setup(tmp_path)
    request = _request(Ed25519PrivateKey.generate())
    with pytest.raises(ValueError, match="signature"):
        RecoveryCoordinator(database).restore_standby(request, policy)
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM migration_tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM migration_epochs").fetchone()[0] == 0


def test_emergency_recovery_rejects_secret_failure_evidence(tmp_path: Any) -> None:
    database, policy, private, _ = _setup(tmp_path)
    request = replace(_request(private), source_failure_evidence="token=secret")
    with pytest.raises(ValueError, match="secret"):
        RecoveryCoordinator(database).restore_standby(request, policy)


def test_emergency_recovery_is_idempotent_for_same_task_and_proof(tmp_path: Any) -> None:
    database, policy, private, _ = _setup(tmp_path)
    coordinator = RecoveryCoordinator(database)
    request = _request(private)
    first = coordinator.restore_standby(request, policy)
    second = coordinator.restore_standby(request, policy)

    assert second == first
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM migration_tasks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM migration_epochs").fetchone()[0] == 1


def test_emergency_recovery_rejects_active_epoch_before_new_task(tmp_path: Any) -> None:
    database, policy, private, _ = _setup(tmp_path)
    coordinator = RecoveryCoordinator(database)
    request = _request(private)
    coordinator.restore_standby(request, policy)
    changed = replace(request, task_id="recovery-task-2")
    changed = replace(
        changed,
        target_signature=base64.urlsafe_b64encode(
            private.sign(RecoveryCoordinator.signing_bytes(changed))
        ).decode("ascii"),
    )
    with pytest.raises(ValueError, match="active migration epoch"):
        coordinator.restore_standby(changed, policy)
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM migration_tasks").fetchone()[0] == 1


def test_recovery_proof_rejects_changed_digest(tmp_path: Any) -> None:
    database, policy, private, _ = _setup(tmp_path)
    request = _request(private)
    changed = replace(request, health_report_digest="d" * 64)
    with pytest.raises(ValueError, match="signature"):
        RecoveryCoordinator(database).restore_standby(changed, policy)
