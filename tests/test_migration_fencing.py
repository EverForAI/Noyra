from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core.admission import OperationInvalidated, RuntimeAdmissionGate
from noyra.core.errors import RuntimeOwnershipError
from noyra.migration.fencing import EpochLease
from noyra.migration.policy import MigrationStore
from noyra.migration.targets import TargetRegistry


def test_only_one_active_epoch_and_old_epoch_is_stale(tmp_path: Any) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("Noyra-0001", "1" * 64)
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
    TargetRegistry(db, MigrationStore(db)).register(
        "Noyra-0001",
        target_id="target-2",
        public_key=base64.urlsafe_b64encode(
            Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        ).decode(),
        endpoint="https://target2.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    first = EpochLease._acquire_unchecked(db, "Noyra-0001", "target-1", expected_source_epoch=None)
    first.assert_current()
    with pytest.raises(ValueError, match="active"):
        EpochLease._acquire_unchecked(
            db, "Noyra-0001", "target-2", expected_source_epoch=first.epoch_id
        )
    first.revoke("cutover", "operator")
    with pytest.raises(ValueError, match="stale"):
        first.assert_current()


def test_active_target_epoch_fences_source_admission_across_restart(tmp_path: Any) -> None:
    database_path = tmp_path / "noyra.sqlite3"
    subject_id = "Noyra-0001"
    kernel = SubjectKernel(database_path, subject_id, "5" * 64)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    private = Ed25519PrivateKey.generate()
    TargetRegistry(kernel.database, MigrationStore(kernel.database)).register(
        subject_id,
        target_id="target-1",
        public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(),
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="test",
    )
    EpochLease._acquire_unchecked(
        kernel.database,
        subject_id,
        "target-1",
        expected_source_epoch=None,
        actor="test",
    )

    with pytest.raises(RuntimeOwnershipError, match="ownership"):
        kernel.admission.begin("source-write")
    kernel.close()

    restarted = SubjectKernel(database_path, subject_id, "5" * 64)
    with pytest.raises(RuntimeOwnershipError, match="fenced by a migration epoch"):
        restarted.boot()
    assert not restarted.process_lock.held


def test_active_target_epoch_rejects_direct_checkpoint_mutation(tmp_path: Any) -> None:
    kernel = SubjectKernel(tmp_path / "checkpoint.sqlite3", "Noyra-0001", "6" * 64)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    private = Ed25519PrivateKey.generate()
    TargetRegistry(kernel.database, MigrationStore(kernel.database)).register(
        kernel.subject_id,
        target_id="target-1",
        public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(),
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="test",
    )
    EpochLease._acquire_unchecked(
        kernel.database,
        kernel.subject_id,
        "target-1",
        expected_source_epoch=None,
        actor="test",
    )

    with pytest.raises(RuntimeOwnershipError, match="ownership"):
        kernel.checkpoint({"memory": "must stay unchanged"}, reason="stale source")

    with kernel.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM state_snapshots WHERE subject_id=?", (kernel.subject_id,)
            ).fetchone()[0]
            == 0
        )
    kernel.close()


def test_completed_epoch_does_not_restore_source_authority_after_restart(tmp_path: Any) -> None:
    database_path = tmp_path / "completed-epoch.sqlite3"
    subject_id = "Noyra-0001"
    kernel = SubjectKernel(database_path, subject_id, "7" * 64)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    private = Ed25519PrivateKey.generate()
    TargetRegistry(kernel.database, MigrationStore(kernel.database)).register(
        subject_id,
        target_id="target-1",
        public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(),
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="test",
    )
    lease = EpochLease._acquire_unchecked(
        kernel.database,
        subject_id,
        "target-1",
        expected_source_epoch=None,
        actor="test",
    )
    lease.complete("test")
    kernel.close()

    restarted = SubjectKernel(database_path, subject_id, "7" * 64)
    with pytest.raises(RuntimeOwnershipError, match="fenced by a migration epoch"):
        restarted.boot()
    assert not restarted.process_lock.held


def test_epoch_acquisition_rejects_missing_target_validation_proof(tmp_path: Any) -> None:
    db = Database(tmp_path / "proof.sqlite3")
    IdentityStore(db).ensure("Noyra-0001", "9" * 64)
    with pytest.raises(ValueError, match="verified target restore and health proof"):
        EpochLease.acquire(
            db,
            "Noyra-0001",
            "target-1",
            expected_source_epoch=None,
        )


def test_filesystem_source_fence_closes_admission_and_survives_restart(tmp_path: Any) -> None:
    database_path = tmp_path / "file-fence.sqlite3"
    fence_root = tmp_path / "migration"
    subject_id = "Noyra-0001"
    kernel = SubjectKernel(
        database_path,
        subject_id,
        "8" * 64,
        migration_fence_root=fence_root,
    )
    kernel.boot()
    kernel.orient()
    state = kernel.activate()
    source = fence_root / "source"
    fences = fence_root / "fences"
    source.mkdir(parents=True, exist_ok=True)
    fences.mkdir(parents=True, exist_ok=True)
    (source / "epoch").write_text(f"runtime-{state.version}", encoding="ascii")
    lease = kernel.admission.begin("fenced-operation")
    (fences / "task-1.json").write_text(
        json.dumps(
            {
                "task_id": "task-1",
                "subject_id": subject_id,
                "source_epoch": f"runtime-{state.version}",
                "status": "active",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeOwnershipError, match="ownership"):
        kernel.admission.begin("new-operation")
    with pytest.raises(Exception, match="ownership"):
        lease.assert_current()
    with pytest.raises(RuntimeOwnershipError, match="migration fence"):
        kernel.checkpoint({"fenced": True}, reason="must be rejected")
    kernel.admission.finish(lease)
    kernel.close()

    restarted = SubjectKernel(
        database_path,
        subject_id,
        "8" * 64,
        migration_fence_root=fence_root,
    )
    with pytest.raises(RuntimeOwnershipError, match="migration fence"):
        restarted.boot()
    assert not restarted.process_lock.held


def test_matching_migration_target_can_boot_and_mutate_its_fenced_runtime(tmp_path: Any) -> None:
    database_path = tmp_path / "target-owned.sqlite3"
    subject_id = "Noyra-0001"
    kernel = SubjectKernel(database_path, subject_id, "7" * 64)
    kernel.boot()
    kernel.orient()
    state = kernel.activate()
    private = Ed25519PrivateKey.generate()
    TargetRegistry(kernel.database, MigrationStore(kernel.database)).register(
        subject_id,
        target_id="target-1",
        public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(),
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="test",
    )
    EpochLease._acquire_unchecked(
        kernel.database,
        subject_id,
        "target-1",
        expected_source_epoch=f"runtime-{state.version}",
        actor="test",
    )
    kernel.close()

    source = SubjectKernel(database_path, subject_id, "7" * 64)
    with pytest.raises(RuntimeOwnershipError, match="migration epoch"):
        source.boot()

    target = SubjectKernel(
        database_path,
        subject_id,
        "7" * 64,
        migration_target_id="target-1",
    )
    target.boot()
    target.orient()
    target.activate()
    target_lease = target.admission.begin("target-owned-operation")
    target_lease.assert_current()
    target.admission.finish(target_lease)
    target.checkpoint({"active": "on target"}, reason="target ownership")
    target.close()


def test_migration_control_fences_existing_and_new_normal_operations() -> None:
    gate = RuntimeAdmissionGate(
        "Noyra-0001",
        ownership_check=lambda: True,
        control_ownership_check=lambda: True,
    )
    gate.open(epoch=1)
    lease = gate.begin("ordinary-write")

    def finish_later() -> None:
        time.sleep(0.02)
        gate.finish(lease)

    worker = threading.Thread(target=finish_later)
    worker.start()

    with gate.migration_control_scope():
        fence_epoch = gate.fence_for_migration()
        assert gate.migration_fenced is True
        assert gate.active_operations == 0
        with pytest.raises(RuntimeOwnershipError):
            gate.begin("new-ordinary-write")
    worker.join()

    assert lease.valid is False
    with pytest.raises(OperationInvalidated):
        lease.assert_current()
    with gate.migration_control_scope():
        gate.clear_migration_fence(fence_epoch)
    restored_lease = gate.begin("after-rollback")
    assert restored_lease.valid is True
    gate.finish(restored_lease)


def test_migration_control_requires_process_ownership() -> None:
    gate = RuntimeAdmissionGate(
        "Noyra-0001",
        ownership_check=lambda: False,
        control_ownership_check=lambda: False,
    )
    gate.open(epoch=1)
    with pytest.raises(RuntimeOwnershipError, match="ownership"), gate.migration_control_scope():
        pass


def test_migration_fence_cannot_be_cleared_while_durable_epoch_is_active() -> None:
    durable_active = True
    gate = RuntimeAdmissionGate(
        "Noyra-0001",
        ownership_check=lambda: not durable_active,
        control_ownership_check=lambda: True,
        migration_clear_check=lambda: not durable_active,
    )
    gate.open(epoch=1)
    with gate.migration_control_scope():
        fence_epoch = gate.fence_for_migration()
        with pytest.raises(RuntimeOwnershipError, match="epoch is still active"):
            gate.open(epoch=2)
        with pytest.raises(RuntimeOwnershipError, match="epoch is still active"):
            gate.clear_migration_fence(fence_epoch)
        durable_active = False
        gate.clear_migration_fence(fence_epoch)
    assert gate.migration_fenced is False
