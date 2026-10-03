from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.core.types import canonical_json, content_hash
from noyra.migration.cutover import CutoverCoordinator
from noyra.migration.executor import MigrationExecutionReceipt
from noyra.migration.manager import MigrationManager, MigrationTask
from noyra.migration.policy import MigrationStore
from noyra.migration.targets import TargetRegistry


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(
        self,
        task: MigrationTask,
        *,
        proof: Mapping[str, object],
        source_epoch: str,
    ) -> MigrationExecutionReceipt:
        self.calls.append(task.task_id)
        assert source_epoch == task.source_epoch
        assert proof["manifest_digest"] == "b" * 64
        volume = proof["target_volume_proof"]
        credential = proof["credential_binding"]
        return MigrationExecutionReceipt(
            task.task_id,
            task.subject_id,
            task.target_id,
            task.source_epoch,
            "b" * 64,
            "artifact-1",
            content_hash(proof["restore_report"]),
            content_hash(proof["health_report"]),
            "f" * 64,
            "a" * 64,
            "c" * 64,
            content_hash(
                {"task_id": task.task_id, "manifest_digest": "b" * 64, "proof": volume}
            ),
            content_hash(
                {"task_id": task.task_id, "manifest_digest": "b" * 64, "binding": credential}
            ),
            None,
            "disabled",
            None,
        )

    def rollback(
        self,
        task: MigrationTask,
        *,
        receipt: MigrationExecutionReceipt,
        reason: str,
    ) -> None:
        del task, receipt, reason


def _task(tmp_path: Any) -> tuple[Database, MigrationTask, dict[str, object]]:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-0001"
    IdentityStore(database).ensure(subject_id, "1" * 64)
    store = MigrationStore(database)
    policy = store.update_policy(
        subject_id,
        store.read_policy(subject_id).revision,
        {"enabled": True},
        "operator",
    )
    private = Ed25519PrivateKey.generate()
    registry = TargetRegistry(database, store)
    registry.register(
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
        actor="operator",
    )
    challenge = registry.issue_challenge("target-1", source_epoch="source-epoch-1")
    registry.attest(
        "target-1",
        challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode(),
        actor="operator",
    )
    manager = MigrationManager(database, store)
    proposal = manager.create_proposal(
        subject_id=subject_id,
        target_id="target-1",
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="planned",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="executor-test")
    restore = {
        "target_id": "target-1",
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
    payload = {
        "task_id": task.task_id,
        "subject_id": subject_id,
        "target_id": "target-1",
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
            private.sign(canonical_json(payload).encode())
        ).decode(),
        "recipient_key_fingerprint": "c" * 64,
        "target_volume_proof": {
            "status": "verified",
            "encrypted": True,
            "target_id": "target-1",
            "target_identity": "host-1",
            "manifest_digest": "b" * 64,
            "proof_digest": "d" * 64,
        },
        "credential_binding": {
            "status": "verified",
            "target_id": "target-1",
            "target_identity": "host-1",
            "manifest_digest": "b" * 64,
            "availability_proof": "e" * 64,
        },
        "wallet_binding": {
            "status": "verified",
            "mode": "disabled",
            "target_id": "target-1",
            "target_identity": "host-1",
            "manifest_digest": "b" * 64,
        },
    }
    return database, task, proof


def test_cutover_without_executor_does_not_allocate_epoch_or_commit(tmp_path: Any) -> None:
    database, task, proof = _task(tmp_path)
    coordinator = CutoverCoordinator(database)

    with pytest.raises(ValueError, match="migration executor unavailable"):
        coordinator.prepare(task.task_id, proof=proof)

    assert (
        MigrationManager(database, MigrationStore(database)).get_task(task.task_id).status
        == "approved"
    )
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM migration_epochs").fetchone()[0] == 0


def test_cutover_executes_before_committing_and_returns_bound_receipt(tmp_path: Any) -> None:
    database, task, proof = _task(tmp_path)
    executor = _FakeExecutor()
    coordinator = CutoverCoordinator(database, executor=executor)

    prepared = coordinator.prepare(task.task_id, proof=proof)
    assert prepared.status == "validating"
    result = coordinator.commit(task.task_id)

    assert result["task_id"] == task.task_id
    assert result["status"] == "committed"
    assert executor.calls == [task.task_id]
