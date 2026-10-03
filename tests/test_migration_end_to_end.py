from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.types import canonical_json, content_hash
from noyra.migration.cutover import CutoverCoordinator
from noyra.migration.executor import MigrationExecutionReceipt
from noyra.migration.fencing import EpochLease
from noyra.migration.manager import MigrationManager, MigrationTask
from noyra.migration.policy import MigrationStore
from noyra.migration.recovery import RecoveryCoordinator, RecoveryRequest
from noyra.migration.targets import TargetRegistry
from noyra.migration.transfer import EncryptedTransferSession, TransferSession
from noyra.migration.wallet import WalletMigration


class _ProofExecutor:
    def execute(
        self,
        task: MigrationTask,
        *,
        proof: Mapping[str, object],
        source_epoch: str,
    ) -> MigrationExecutionReceipt:
        restore = proof["restore_report"]
        health = proof["health_report"]
        assert isinstance(restore, dict)
        assert isinstance(health, dict)
        return MigrationExecutionReceipt(
            task.task_id,
            task.subject_id,
            task.target_id,
            source_epoch,
            str(proof["manifest_digest"]),
            str(proof["artifact_id"]),
            content_hash(restore),
            content_hash(health),
            "f" * 64,
            "a" * 64,
        )

    def rollback(
        self,
        task: MigrationTask,
        *,
        receipt: MigrationExecutionReceipt,
        reason: str,
    ) -> None:
        del task, receipt, reason


def _target_context(tmp_path: Any, *, emergency: bool = False) -> Any:
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
    patch = {"enabled": True, "allowed_target_ids": ("standby-1",)}
    if emergency:
        patch.update(approval_mode="emergency_recovery", emergency_recovery_enabled=True)
    policy = store.read_policy(subject_id)
    store.update_policy(subject_id, policy.revision, patch, "operator")
    private = Ed25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes_raw()
    registry = TargetRegistry(database, store)
    registry.register(
        subject_id,
        target_id="standby-1",
        public_key=base64.urlsafe_b64encode(public_bytes).decode("ascii"),
        endpoint="https://standby.example/migration",
        capabilities={"encrypted_restore": True},
        region="test",
        provider="test",
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    challenge = registry.issue_challenge("standby-1", source_epoch="runtime-1")
    registry.attest(
        "standby-1",
        challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode("ascii"),
        actor="operator",
    )
    backup_path = tmp_path / "backup.bin"
    backup_path.write_bytes(b"verified recovery backup")
    RecoveryCoordinator(database).register_backup(
        subject_id=subject_id,
        backup_id="backup-1",
        backup_path=str(backup_path),
        content_hash_value=hashlib.sha256(backup_path.read_bytes()).hexdigest(),
        byte_size=backup_path.stat().st_size,
        schema_version=CURRENT_SCHEMA_VERSION,
        genesis_hash="d" * 64,
        key_id="backup-key-1",
        keyring_generation=1,
    )
    return database, store, registry, private


def _recovery_request(private: Ed25519PrivateKey, task_id: str = "recovery-task-1") -> Any:
    request = RecoveryRequest(
        task_id=task_id,
        standby_target_id="standby-1",
        verified_backup_id="backup-1",
        source_failure_evidence="source health check timed out",
        manifest_digest="a" * 64,
        restore_report_digest="b" * 64,
        health_report_digest="c" * 64,
    )
    signature = base64.urlsafe_b64encode(
        private.sign(RecoveryCoordinator.signing_bytes(request))
    ).decode("ascii")
    return replace(request, target_signature=signature)


def test_recovery_duplicate_request_and_double_active_fencing(tmp_path: Any) -> None:
    database, store, _, private = _target_context(tmp_path, emergency=True)
    policy = store.read_policy("Noyra-0001")
    coordinator = RecoveryCoordinator(database)
    request = _recovery_request(private)

    first = coordinator.restore_standby(request, policy)
    assert coordinator.restore_standby(request, policy) == first
    changed = _recovery_request(private, task_id="recovery-task-2")
    with pytest.raises(ValueError, match="active migration epoch"):
        coordinator.restore_standby(changed, policy)
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM migration_epochs WHERE status='active'"
            ).fetchone()[0]
            == 1
        )


def test_revoked_target_and_changed_health_proof_fail_closed(tmp_path: Any) -> None:
    database, store, registry, private = _target_context(tmp_path, emergency=True)
    policy = store.read_policy("Noyra-0001")
    registry.revoke("standby-1", reason="target health failed", actor="operator")
    with pytest.raises(ValueError, match="attested"):
        RecoveryCoordinator(database).restore_standby(_recovery_request(private), policy)

    changed_root = tmp_path / "changed"
    changed_root.mkdir()
    database, store, _, private = _target_context(changed_root, emergency=True)
    policy = store.read_policy("Noyra-0001")
    request = _recovery_request(private)
    changed = replace(request, health_report_digest="d" * 64)
    with pytest.raises(ValueError, match="signature"):
        RecoveryCoordinator(database).restore_standby(changed, policy)


def test_interrupted_and_corrupted_encrypted_transfer_are_rejected(tmp_path: Any) -> None:
    source = tmp_path / "source.bin"
    encrypted = tmp_path / "source.enc"
    restored = tmp_path / "restored.bin"
    source.write_bytes(b"payload" * 20_000)
    plain = TransferSession(chunk_bytes=4096)
    receipt = plain.send(source, tmp_path / "partial.bin", stop_after_chunks=2)
    assert not receipt.complete
    resumed = plain.resume(receipt)
    plain.verify(resumed)

    session = EncryptedTransferSession(b"k" * 32, chunk_bytes=4096)
    encrypted_receipt = session.send(source, encrypted, manifest_digest="a" * 64)
    encrypted_bytes = bytearray(encrypted.read_bytes())
    encrypted_bytes[-1] ^= 1
    encrypted.write_bytes(encrypted_bytes)
    with pytest.raises(ValueError, match="decrypt"):
        session.receive(encrypted_receipt, restored, manifest_digest="a" * 64)
    assert not restored.exists()


def test_stale_approval_changed_policy_cutover_failure_and_rollback(tmp_path: Any) -> None:
    database, store, _, _ = _target_context(tmp_path)
    manager = MigrationManager(database, store)
    policy = store.read_policy("Noyra-0001")
    proposal = manager.create_proposal(
        subject_id="Noyra-0001",
        target_id="standby-1",
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="planned maintenance",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="e2e-task")
    store.update_policy("Noyra-0001", policy.revision, {"trust_level": 4}, "operator")
    with pytest.raises(ValueError, match="policy"):
        manager.transition_task(task.task_id, "preparing", actor="operator")

    rollback_root = tmp_path / "rollback"
    rollback_root.mkdir()
    rollback_db, rollback_store, _, _ = _target_context(rollback_root)
    rollback_manager = MigrationManager(rollback_db, rollback_store)
    rollback_policy = rollback_store.read_policy("Noyra-0001")
    rollback_proposal = rollback_manager.create_proposal(
        subject_id="Noyra-0001",
        target_id="standby-1",
        policy_revision=rollback_policy.revision,
        reason_code="maintenance",
        reason="planned maintenance",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    rollback_task = rollback_manager.approve(
        rollback_proposal.proposal_id, actor="operator", idempotency_key="rollback-e2e"
    )
    cutover = CutoverCoordinator(rollback_db, executor=_ProofExecutor())
    with pytest.raises(ValueError, match="verified target"):
        cutover.commit(rollback_task.task_id)
    result = cutover.rollback(rollback_task.task_id, "cutover health failed")
    assert result == {"task_id": rollback_task.task_id, "status": "rolled_back"}


def test_source_restart_remains_fenced_and_local_wallet_needs_approval(tmp_path: Any) -> None:
    database, _, _, _ = _target_context(tmp_path)
    lease = EpochLease._acquire_unchecked(
        database, "Noyra-0001", "standby-1", expected_source_epoch=None, actor="operator"
    )
    database_path = tmp_path / "noyra.sqlite3"
    lease.assert_current()
    kernel = SubjectKernel(database_path, "Noyra-0001", "d" * 64)
    with pytest.raises(RuntimeOwnershipError, match="fenced"):
        kernel.boot()
    kernel.close()
    lease.revoke("test cleanup", "operator")

    plan = WalletMigration.plan(
        mode="local_wallet_transfer",
        source_address="0xabc",
        target_address="0xabc",
        signer_id=None,
        task_id="wallet-e2e",
        local_transfer_enabled=True,
    )
    with pytest.raises(ValueError, match="second approval"):
        WalletMigration.apply_local_transfer(plan, approval=None)


def test_cutover_accepts_task_bound_signed_restore_and_health_proof(tmp_path: Any) -> None:
    database, store, registry, private = _target_context(tmp_path)
    policy = store.read_policy("Noyra-0001")
    manager = MigrationManager(database, store)
    proposal = manager.create_proposal(
        subject_id="Noyra-0001",
        target_id="standby-1",
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="planned maintenance",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="cutover-e2e")
    challenge = registry.issue_challenge("standby-1", source_epoch=task.source_epoch)
    registry.attest(
        "standby-1",
        challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode("ascii"),
        actor="operator",
    )
    restore = {
        "target_id": "standby-1",
        "artifact_id": "artifact-1",
        "manifest_digest": "b" * 64,
        "status": "restored",
    }
    health = {
        **restore,
        "status": "healthy",
        "host_identity": "host-1",
        "checks": {"database": True, "runtime": True},
    }
    signing_payload = {
        "task_id": task.task_id,
        "subject_id": task.subject_id,
        "target_id": task.target_id,
        "source_epoch": task.source_epoch,
        "manifest_digest": "b" * 64,
        "artifact_id": "artifact-1",
        "restore_report_digest": content_hash(restore),
        "health_report_digest": content_hash(health),
    }
    proof: dict[str, object] = {
        "manifest_digest": "b" * 64,
        "artifact_id": "artifact-1",
        "restore_report": restore,
        "health_report": health,
        "target_signature": base64.urlsafe_b64encode(
            private.sign(canonical_json(signing_payload).encode())
        ).decode("ascii"),
    }
    cutover = CutoverCoordinator(database, executor=_ProofExecutor())
    prepared = cutover.prepare(task.task_id, proof=proof)
    assert prepared.status == "validating"
    assert cutover.commit(task.task_id) == {"task_id": task.task_id, "status": "committed"}


def test_cutover_commit_rolls_back_task_when_epoch_completion_fails(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, store, registry, private = _target_context(tmp_path)
    policy = store.read_policy("Noyra-0001")
    manager = MigrationManager(database, store)
    proposal = manager.create_proposal(
        subject_id="Noyra-0001",
        target_id="standby-1",
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="planned maintenance",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="cutover-atomic")
    challenge = registry.issue_challenge("standby-1", source_epoch=task.source_epoch)
    registry.attest(
        "standby-1",
        challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode("ascii"),
        actor="operator",
    )
    restore = {
        "target_id": "standby-1",
        "artifact_id": "artifact-atomic",
        "manifest_digest": "c" * 64,
        "status": "restored",
    }
    health = {
        **restore,
        "status": "healthy",
        "host_identity": "host-atomic",
        "checks": {"database": True, "runtime": True},
    }
    signing_payload = {
        "task_id": task.task_id,
        "subject_id": task.subject_id,
        "target_id": task.target_id,
        "source_epoch": task.source_epoch,
        "manifest_digest": restore["manifest_digest"],
        "artifact_id": restore["artifact_id"],
        "restore_report_digest": content_hash(restore),
        "health_report_digest": content_hash(health),
    }
    proof: dict[str, object] = {
        "manifest_digest": restore["manifest_digest"],
        "artifact_id": restore["artifact_id"],
        "restore_report": restore,
        "health_report": health,
        "target_signature": base64.urlsafe_b64encode(
            private.sign(canonical_json(signing_payload).encode())
        ).decode("ascii"),
    }
    cutover = CutoverCoordinator(database, executor=_ProofExecutor())
    cutover.prepare(task.task_id, proof=proof)

    def fail_completion(self: EpochLease, connection: Any, actor: str) -> None:
        del self, actor
        connection.execute(
            "UPDATE migration_epochs SET status='completed' "
            "WHERE epoch_id=(SELECT target_epoch_id FROM migration_tasks WHERE task_id=?)",
            (task.task_id,),
        )
        raise RuntimeError("injected epoch completion failure")

    monkeypatch.setattr(EpochLease, "complete_in_transaction", fail_completion)
    with pytest.raises(RuntimeError, match="injected epoch completion failure"):
        cutover.commit(task.task_id)

    # The durable transaction rolls back completely, leaving the task in its
    # retryable validation state while the executor compensates the target.
    assert manager.get_task(task.task_id).status == "validating"
    with database.connection() as connection:
        epoch = connection.execute(
            "SELECT status FROM migration_epochs "
            "WHERE epoch_id=(SELECT target_epoch_id FROM migration_tasks WHERE task_id=?)",
            (task.task_id,),
        ).fetchone()
    assert epoch["status"] == "active"
