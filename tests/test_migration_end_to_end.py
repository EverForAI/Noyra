from __future__ import annotations

import base64
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core.errors import RuntimeOwnershipError
from noyra.migration.cutover import CutoverCoordinator
from noyra.migration.fencing import EpochLease
from noyra.migration.manager import MigrationManager
from noyra.migration.policy import MigrationStore
from noyra.migration.recovery import RecoveryCoordinator, RecoveryRequest
from noyra.migration.targets import TargetRegistry
from noyra.migration.transfer import EncryptedTransferSession, TransferSession
from noyra.migration.wallet import WalletMigration


def _target_context(tmp_path, *, emergency: bool = False):
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-0001"
    IdentityStore(database).ensure(subject_id, "d" * 64)
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
    challenge = registry.issue_challenge("standby-1", source_epoch="source-1")
    registry.attest(
        "standby-1",
        challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode("ascii"),
        actor="operator",
    )
    return database, store, registry, private


def _recovery_request(private: Ed25519PrivateKey, task_id: str = "recovery-task-1"):
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


def test_recovery_duplicate_request_and_double_active_fencing(tmp_path) -> None:
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


def test_revoked_target_and_changed_health_proof_fail_closed(tmp_path) -> None:
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


def test_interrupted_and_corrupted_encrypted_transfer_are_rejected(tmp_path) -> None:
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


def test_stale_approval_changed_policy_cutover_failure_and_rollback(tmp_path) -> None:
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
    cutover = CutoverCoordinator(rollback_db)
    with pytest.raises(ValueError, match="verified target"):
        cutover.commit(rollback_task.task_id)
    result = cutover.rollback(rollback_task.task_id, "cutover health failed")
    assert result == {"task_id": rollback_task.task_id, "status": "rolled_back"}


def test_source_restart_remains_fenced_and_local_wallet_needs_approval(tmp_path) -> None:
    database, _, _, _ = _target_context(tmp_path)
    lease = EpochLease._acquire_unchecked(
        database, "Noyra-0001", "standby-1", expected_source_epoch=None, actor="operator"
    )
    database_path = tmp_path / "noyra.sqlite3"
    lease.assert_current()
    with pytest.raises(RuntimeOwnershipError, match="fenced"):
        SubjectKernel(database_path, "Noyra-0001", "d" * 64)
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
