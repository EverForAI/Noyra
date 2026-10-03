from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore, SubjectKernel
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
