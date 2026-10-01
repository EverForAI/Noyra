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
