from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.core.admission import RuntimeAdmissionGate
from noyra.migration.cutover import CutoverCoordinator
from noyra.migration.fencing import EpochLease
from noyra.migration.manager import MigrationManager
from noyra.migration.policy import MigrationStore
from noyra.migration.targets import TargetRegistry


def _enabled_store(db: Database) -> tuple[MigrationStore, int]:
    store = MigrationStore(db)
    policy = store.read_policy("Noyra-0001")
    updated = store.update_policy("Noyra-0001", policy.revision, {"enabled": True}, "operator")
    return store, updated.revision


def test_cutover_rejects_missing_target_proof_without_changing_source_authority(tmp_path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("Noyra-0001", "2" * 64)
    private = Ed25519PrivateKey.generate()
    registry = TargetRegistry(db, MigrationStore(db))
    registry.register(
        "Noyra-0001",
        target_id="target-1",
        public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(),
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    challenge = registry.issue_challenge("target-1", source_epoch="epoch-1")
    registry.attest(
        "target-1",
        challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode(),
        actor="operator",
    )
    store, revision = _enabled_store(db)
    manager = MigrationManager(db, store)
    proposal = manager.create_proposal(
        subject_id="Noyra-0001",
        target_id="target-1",
        policy_revision=revision,
        reason_code="maintenance",
        reason="planned",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="cutover-1")
    coordinator = CutoverCoordinator(db)
    with pytest.raises(ValueError, match="verified target restore and health proof"):
        coordinator.prepare(task.task_id)
    with pytest.raises(ValueError, match="verified target restore and health proof"):
        coordinator.commit(task.task_id)
    unchanged = manager.get_task(task.task_id)
    assert unchanged.status == "approved"
    assert unchanged.source_epoch == task.source_epoch
    assert unchanged.target_epoch_id is None
    with db.connection() as connection:
        epochs = connection.execute("SELECT COUNT(*) FROM migration_epochs").fetchone()[0]
    assert epochs == 0


def test_target_epoch_transition_preserves_source_provenance_and_task_hash(tmp_path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("Noyra-0001", "4" * 64)
    private = Ed25519PrivateKey.generate()
    registry = TargetRegistry(db, MigrationStore(db))
    registry.register(
        "Noyra-0001",
        target_id="target-1",
        public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(),
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    challenge = registry.issue_challenge("target-1", source_epoch="epoch-1")
    registry.attest(
        "target-1",
        challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode(),
        actor="operator",
    )
    store, revision = _enabled_store(db)
    manager = MigrationManager(db, store)
    proposal = manager.create_proposal(
        subject_id="Noyra-0001",
        target_id="target-1",
        policy_revision=revision,
        reason_code="maintenance",
        reason="planned",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="cutover-hash")
    target_lease = EpochLease._acquire_unchecked(
        db,
        task.subject_id,
        task.target_id,
        expected_source_epoch=task.source_epoch,
        actor="test",
    )
    changed = manager.transition_task(
        task.task_id,
        "preparing",
        actor="test",
        target_epoch_id=target_lease.epoch_id,
    )
    assert changed.source_epoch == task.source_epoch
    assert changed.target_epoch_id == target_lease.epoch_id
    assert manager.get_task(task.task_id) == changed
    target_lease.revoke("test cleanup", "test")


def test_cutover_fences_epoch_and_source_admission(tmp_path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("Noyra-0001", "3" * 64)
    private = Ed25519PrivateKey.generate()
    TargetRegistry(db, MigrationStore(db)).register(
        "Noyra-0001",
        target_id="target-1",
        public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(),
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    registry = TargetRegistry(db, MigrationStore(db))
    challenge = registry.issue_challenge("target-1", source_epoch="epoch-1")
    registry.attest(
        "target-1",
        challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode(),
        actor="operator",
    )
    store, revision = _enabled_store(db)
    manager = MigrationManager(db, store)
    proposal = manager.create_proposal(
        subject_id="Noyra-0001",
        target_id="target-1",
        policy_revision=revision,
        reason_code="maintenance",
        reason="planned",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="cutover-2")
    gate = RuntimeAdmissionGate("Noyra-0001")
    gate.open(epoch=1)
    coordinator = CutoverCoordinator(db, admission=gate)
    with pytest.raises(ValueError, match="verified target restore and health proof"):
        coordinator.prepare(task.task_id)
    assert gate.begin("source-still-authoritative")


def test_rollback_does_not_mark_task_rolled_back_before_epoch_revocation(tmp_path) -> None:
    db = Database(tmp_path / "rollback.sqlite3")
    IdentityStore(db).ensure("Noyra-0001", "8" * 64)
    private = Ed25519PrivateKey.generate()
    registry = TargetRegistry(db, MigrationStore(db))
    registry.register(
        "Noyra-0001",
        target_id="target-1",
        public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(),
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    challenge = registry.issue_challenge("target-1", source_epoch="epoch-1")
    registry.attest(
        "target-1",
        challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode(),
        actor="operator",
    )
    store, revision = _enabled_store(db)
    manager = MigrationManager(db, store)
    proposal = manager.create_proposal(
        subject_id="Noyra-0001",
        target_id="target-1",
        policy_revision=revision,
        reason_code="maintenance",
        reason="planned",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="rollback-1")
    lease = EpochLease._acquire_unchecked(
        db,
        task.subject_id,
        task.target_id,
        expected_source_epoch=task.source_epoch,
        actor="test",
    )
    manager.transition_task(
        task.task_id,
        "preparing",
        actor="test",
        target_epoch_id=lease.epoch_id,
    )
    with db.transaction() as connection:
        connection.execute(
            "UPDATE migration_epochs SET state_hash=? WHERE epoch_id=?",
            ("0" * 64, lease.epoch_id),
        )

    coordinator = CutoverCoordinator(db)
    with pytest.raises(ValueError, match="integrity"):
        coordinator.rollback(task.task_id, "test rollback")

    assert manager.get_task(task.task_id).status == "rolling_back"
    with db.connection() as connection:
        epoch = connection.execute(
            "SELECT status FROM migration_epochs WHERE epoch_id=?", (lease.epoch_id,)
        ).fetchone()
    assert epoch["status"] == "active"
