from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.migration.manager import MigrationManager
from noyra.migration.policy import MigrationStore
from noyra.migration.targets import TargetRegistry

# SQL assertions are intentionally kept compact in this focused state-machine test.
# ruff: noqa: E501


def _manager(tmp_path):
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("Noyra-0001", "f" * 64)
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
    store = MigrationStore(db)
    policy = store.read_policy("Noyra-0001")
    store.update_policy("Noyra-0001", policy.revision, {"enabled": True}, "operator")
    return db, MigrationManager(db, store)


def _proposal(manager: MigrationManager):
    return manager.create_proposal(
        subject_id="Noyra-0001",
        target_id="target-1",
        policy_revision=manager.policy_store.read_policy("Noyra-0001").revision,
        reason_code="maintenance",
        reason="planned maintenance",
        expires_at="2099-01-01T00:00:00+00:00",
    )


def test_rejection_is_atomic_and_audited(tmp_path) -> None:
    db, manager = _manager(tmp_path)
    proposal = _proposal(manager)
    rejection = manager.reject(proposal.proposal_id, actor="operator", reason="risk too high")
    assert rejection.proposal_id == proposal.proposal_id
    with db.connection() as connection:
        row = connection.execute(
            "SELECT status, decision_reason, state_hash FROM migration_proposals WHERE proposal_id=?",
            (proposal.proposal_id,),
        ).fetchone()
        audit = connection.execute(
            "SELECT action FROM migration_audit_events WHERE subject_id=? ORDER BY occurred_at",
            ("Noyra-0001",),
        ).fetchall()
    assert row["status"] == "rejected"
    assert row["decision_reason"] == "risk too high"
    assert len(row["state_hash"]) == 64
    assert [item["action"] for item in audit][-2:] == [
        "migration_proposal_created",
        "migration_proposal_rejected",
    ]


def test_task_transitions_reject_skips_and_record_audit(tmp_path) -> None:
    db, manager = _manager(tmp_path)
    proposal = _proposal(manager)
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="task-1")
    with pytest.raises(ValueError, match="transition"):
        manager.transition_task(task.task_id, "committed", actor="operator")
    updated = manager.transition_task(task.task_id, "preparing", actor="operator")
    assert updated.status == "preparing"
    with db.connection() as connection:
        actions = connection.execute(
            "SELECT action FROM migration_audit_events WHERE subject_id=? ORDER BY occurred_at",
            ("Noyra-0001",),
        ).fetchall()
    assert actions[-1]["action"] == "migration_task_transitioned"


def test_task_transition_rejects_stale_policy_revision(tmp_path) -> None:
    db, manager = _manager(tmp_path)
    proposal = _proposal(manager)
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="task-1")
    store = MigrationStore(db)
    store.read_policy("Noyra-0001")
    store.update_policy(
        "Noyra-0001",
        manager.policy_store.read_policy("Noyra-0001").revision,
        {"rejection_cooldown_seconds": 3600},
        "operator",
    )
    with pytest.raises(ValueError, match="policy"):
        manager.transition_task(task.task_id, "preparing", actor="operator")
