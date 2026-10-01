from __future__ import annotations

# ruff: noqa: E501
import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.migration.cutover import CutoverCoordinator
from noyra.migration.manager import MigrationManager
from noyra.migration.policy import MigrationStore
from noyra.migration.targets import TargetRegistry


def test_cutover_can_prepare_commit_and_reject_post_commit_rollback(tmp_path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("Noyra-0001", "2" * 64)
    private = Ed25519PrivateKey.generate()
    TargetRegistry(db, MigrationStore(db)).register("Noyra-0001", target_id="target-1", public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(), endpoint="https://target.example", capabilities={}, region=None, provider=None, release_sha="a" * 40, os_arch="linux-amd64", encrypted_volume=True, actor="operator")
    manager = MigrationManager(db, MigrationStore(db))
    proposal = manager.create_proposal(subject_id="Noyra-0001", target_id="target-1", policy_revision=1, reason_code="maintenance", reason="planned", expires_at="2099-01-01T00:00:00+00:00")
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="cutover-1")
    coordinator = CutoverCoordinator(db)
    assert coordinator.prepare(task.task_id).status == "preparing"
    assert coordinator.commit(task.task_id)["status"] == "committed"
