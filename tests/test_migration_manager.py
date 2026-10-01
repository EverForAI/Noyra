from __future__ import annotations

# ruff: noqa: E501
import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.migration.manager import MigrationManager
from noyra.migration.policy import MigrationStore
from noyra.migration.targets import TargetRegistry


def _manager(tmp_path):
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("Noyra-0001", "f" * 64)
    private = Ed25519PrivateKey.generate()
    TargetRegistry(db, MigrationStore(db)).register("Noyra-0001", target_id="target-1", public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(), endpoint="https://target.example", capabilities={}, region=None, provider=None, release_sha="a" * 40, os_arch="linux-amd64", encrypted_volume=True, actor="operator")
    registry = TargetRegistry(db, MigrationStore(db))
    challenge = registry.issue_challenge("target-1", source_epoch="epoch-1")
    registry.attest(
        "target-1", challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode(),
        actor="operator",
    )
    return db, MigrationManager(db, MigrationStore(db))


def test_manual_task_requires_approval_and_is_idempotent(tmp_path) -> None:
    _, manager = _manager(tmp_path)
    proposal = manager.create_proposal(
        subject_id="Noyra-0001", target_id="target-1", policy_revision=1,
        reason_code="storage_pressure", reason="low space", expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="k1")
    assert task.status == "approved"
    assert manager.approve(proposal.proposal_id, actor="operator", idempotency_key="k1").task_id == task.task_id


def test_expired_proposal_cannot_be_approved(tmp_path) -> None:
    _, manager = _manager(tmp_path)
    proposal = manager.create_proposal(
        subject_id="Noyra-0001", target_id="target-1", policy_revision=1,
        reason_code="storage_pressure", reason="low space", expires_at="2000-01-01T00:00:00+00:00",
    )
    with pytest.raises(ValueError, match="expired"):
        manager.approve(proposal.proposal_id, actor="operator", idempotency_key="k1")


def test_cancel_task_requires_reason_and_is_audited(tmp_path) -> None:
    db, manager = _manager(tmp_path)
    proposal = manager.create_proposal(
        subject_id="Noyra-0001", target_id="target-1", policy_revision=1,
        reason_code="maintenance", reason="planned", expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="cancel-1")
    with pytest.raises(ValueError, match="reason"):
        manager.cancel(task.task_id, actor="operator", reason="")
    cancelled = manager.cancel(task.task_id, actor="operator", reason="operator stopped")
    assert cancelled.status == "cancelled"
    with db.connection() as connection:
        action = connection.execute(
            "SELECT action FROM migration_audit_events WHERE action='migration_task_cancelled'"
        ).fetchone()
    assert action is not None


def test_cancel_reason_is_redacted_before_audit_persistence(tmp_path) -> None:
    db, manager = _manager(tmp_path)
    proposal = manager.create_proposal(
        subject_id="Noyra-0001", target_id="target-1", policy_revision=1,
        reason_code="maintenance", reason="planned", expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="cancel-secret")
    manager.cancel(
        task.task_id,
        actor="operator",
        reason="operator stopped; bearer token=super-secret-value",
    )
    with db.connection() as connection:
        task_row = connection.execute(
            "SELECT error_code FROM migration_tasks WHERE task_id=?", (task.task_id,)
        ).fetchone()
        audit_row = connection.execute(
            "SELECT payload_json FROM migration_audit_events WHERE action='migration_task_cancelled'"
        ).fetchone()
    assert "super-secret-value" not in task_row["error_code"]
    assert "super-secret-value" not in audit_row["payload_json"]
