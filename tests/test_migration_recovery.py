from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import tracemalloc
from dataclasses import replace
from pathlib import Path
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


def test_backup_identity_agrees_for_handle_and_path_with_distinct_file_times(
    tmp_path: Path,
) -> None:
    from noyra.migration.recovery import _PinnedBackup

    path = tmp_path / "different-times.bin"
    path.write_bytes(b"synthetic verified backup")
    # Creation/change time and mtime must differ, even on a fast filesystem.
    os.utime(path, ns=(1_000_000_000, 2_000_000_000))
    descriptor = RecoveryCoordinator._open_backup(path)
    try:
        assert _PinnedBackup.file_identity(os.fstat(descriptor)) == _PinnedBackup.file_identity(
            path.lstat()
        )
    finally:
        os.close(descriptor)
    with RecoveryCoordinator._pin_verified_file(
        path, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()
    ) as backup:
        backup.assert_unchanged()


def test_backup_hash_is_streamed_without_holding_database_writer(
    tmp_path: Path, monkeypatch: Any
) -> None:
    database, policy, private, _ = _setup(tmp_path)
    backup = tmp_path / "large.bin"
    chunk = b"x" * (1024 * 1024)
    digest = hashlib.sha256()
    with backup.open("wb") as stream:
        for _ in range(8):
            stream.write(chunk)
            digest.update(chunk)
    del chunk
    original = RecoveryCoordinator._stream_digest
    hashes = 0

    def check_writer(stream: Any, size: int) -> str:
        nonlocal hashes
        hashes += 1
        # A second connection must acquire the SQLite writer while hashing.
        with sqlite3.connect(database.path, timeout=0) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()
        return original(stream, size)

    monkeypatch.setattr(RecoveryCoordinator, "_stream_digest", staticmethod(check_writer))
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("whole-file read is forbidden"))
    tracemalloc.start()
    try:
        coordinator = RecoveryCoordinator(database)
        coordinator.register_backup(
            subject_id=policy.subject_id,
            backup_id="large-backup",
            backup_path=str(backup),
            content_hash_value=digest.hexdigest(),
            byte_size=backup.stat().st_size,
            schema_version=CURRENT_SCHEMA_VERSION,
            genesis_hash="d" * 64,
            key_id="backup-key-1",
            keyring_generation=1,
        )
        request = replace(_request(private), verified_backup_id="large-backup")
        request = replace(
            request,
            target_signature=base64.urlsafe_b64encode(
                private.sign(RecoveryCoordinator.signing_bytes(request))
            ).decode(),
        )
        assert coordinator.restore_standby(request, policy)["status"] == "recovery_ready"
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert hashes == 2
    assert peak < 5 * 1024 * 1024


@pytest.mark.parametrize("change", ["truncate", "same_size", "replace"])
def test_recovery_rejects_backup_changed_while_hashing(
    tmp_path: Path, monkeypatch: Any, change: str
) -> None:
    database, policy, private, _ = _setup(tmp_path)
    path = tmp_path / "backup.bin"
    original = RecoveryCoordinator._stream_digest

    def mutate(stream: Any, size: int) -> str:
        result = original(stream, size)
        if change == "truncate":
            path.write_bytes(b"short")
        elif change == "same_size":
            before = path.stat()
            path.write_bytes(b"z" * size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        else:
            replacement = tmp_path / "replacement.bin"
            replacement.write_bytes(b"z" * size)
            try:
                replacement.replace(path)
            except PermissionError:
                # Windows may prevent replacement of an open file; in-place
                # mutation still tests that digest alone cannot authorize it.
                path.write_bytes(b"z" * size)
        return result

    monkeypatch.setattr(RecoveryCoordinator, "_stream_digest", staticmethod(mutate))
    with pytest.raises(ValueError, match=r"identity changed|file is unavailable"):
        RecoveryCoordinator(database).restore_standby(_request(private), policy)
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM migration_tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM migration_epochs").fetchone()[0] == 0


def test_backup_registry_snapshot_is_rechecked_in_publication_transaction(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from noyra.core.types import content_hash

    database, policy, private, _ = _setup(tmp_path)
    original = RecoveryCoordinator._stream_digest

    def change_registry(stream: Any, size: int) -> str:
        result = original(stream, size)
        with database.transaction() as connection:
            row = dict(
                connection.execute(
                    "SELECT * FROM migration_backup_registry WHERE backup_id='backup-1'"
                ).fetchone()
            )
            row.pop("state_hash")
            row["keyring_generation"] = 2
            connection.execute(
                "UPDATE migration_backup_registry SET keyring_generation=2,state_hash=? "
                "WHERE backup_id='backup-1'",
                (content_hash(row),),
            )
        return result

    monkeypatch.setattr(RecoveryCoordinator, "_stream_digest", staticmethod(change_registry))
    with pytest.raises(ValueError, match="registry changed"):
        RecoveryCoordinator(database).restore_standby(_request(private), policy)
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM migration_tasks").fetchone()[0] == 0


def test_recovery_rechecks_file_before_task_publication_commit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    database, policy, private, _ = _setup(tmp_path)
    original = MigrationStore._append_audit

    def modify_after_insert(
        connection: Any, subject: str, action: str, actor: str, payload: Any
    ) -> None:
        original(connection, subject, action, actor, payload)
        if action == "migration_emergency_recovery_verified":
            (tmp_path / "backup.bin").write_bytes(b"changed after publication")

    monkeypatch.setattr(MigrationStore, "_append_audit", staticmethod(modify_after_insert))
    with pytest.raises(ValueError, match=r"identity changed|file is unavailable"):
        RecoveryCoordinator(database).restore_standby(_request(private), policy)
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM migration_tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM migration_proposals").fetchone()[0] == 0


def test_registration_rechecks_file_before_commit(tmp_path: Path, monkeypatch: Any) -> None:
    database, policy, _, _ = _setup(tmp_path)
    path = tmp_path / "registration.bin"
    path.write_bytes(b"registered")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    original = MigrationStore._append_audit

    def modify(connection: Any, subject: str, action: str, actor: str, payload: Any) -> None:
        original(connection, subject, action, actor, payload)
        if action == "migration_backup_registered":
            path.write_bytes(b"changed")

    monkeypatch.setattr(MigrationStore, "_append_audit", staticmethod(modify))
    with pytest.raises(ValueError, match=r"identity changed|file is unavailable"):
        RecoveryCoordinator(database).register_backup(
            subject_id=policy.subject_id,
            backup_id="registration-2",
            backup_path=str(path),
            content_hash_value=digest,
            byte_size=10,
            schema_version=CURRENT_SCHEMA_VERSION,
            genesis_hash="d" * 64,
            key_id="backup-key-1",
            keyring_generation=1,
        )
    with database.connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM migration_backup_registry").fetchone()[0] == 1
        )
