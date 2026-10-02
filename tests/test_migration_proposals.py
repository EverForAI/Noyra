from __future__ import annotations

import base64
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.migration.policy import MigrationPolicy, MigrationStore
from noyra.migration.proposals import MigrationProposalBuilder, MigrationProposalStore
from noyra.migration.targets import TargetRegistry
from noyra.migration.trust import TrustDecision


def _policy(**changes: Any) -> Any:
    policy = MigrationPolicy.default("Noyra-0001").with_updates(enabled=True)
    return policy.with_updates(**changes) if changes else policy


def _candidate(**changes: Any) -> Any:
    values = {
        "target_id": "target-1",
        "status": "active",
        "region": "us-east",
        "encrypted_volume": True,
        "free_bytes": 10_000_000_000,
        "release_sha": "a" * 40,
        "trust_level": 5,
    }
    values.update(changes)
    return type("Candidate", (), values)()


def test_resources_alone_do_not_create_proposal() -> None:
    proposal = MigrationProposalBuilder().build(
        subject_id="Noyra-0001",
        candidate=_candidate(),
        need=None,
        trust=TrustDecision(True),
        policy=_policy(),
    )
    assert proposal is None


def test_untrusted_target_is_rejected_before_proposal() -> None:
    proposal = MigrationProposalBuilder().build(
        subject_id="Noyra-0001",
        candidate=_candidate(),
        need={"source_health": 0.1, "storage_pressure": 0.9, "benefit_score": 0.9},
        trust=TrustDecision(False, ("target_not_active",)),
        policy=_policy(),
    )
    assert proposal is None


def test_proposal_contains_reason_evidence_and_expiry() -> None:
    proposal = MigrationProposalBuilder().build(
        subject_id="Noyra-0001",
        candidate=_candidate(),
        need={
            "source_health": 0.2,
            "storage_pressure": 0.8,
            "benefit_score": 0.9,
            "reason_code": "storage_pressure",
            "reason": "source storage is under pressure",
            "evidence": {"storage": "hash-storage"},
        },
        trust=TrustDecision(True),
        policy=_policy(),
    )
    assert proposal is not None
    assert proposal.reason_code == "storage_pressure"
    assert proposal.evidence["storage"] == "hash-storage"
    assert proposal.expires_at > proposal.created_at


def test_rejection_cooldown_is_keyed_to_target_and_reason(tmp_path: Any) -> None:
    from noyra.core import Database, IdentityStore

    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-0001", "e" * 64)
    private = Ed25519PrivateKey.generate()
    TargetRegistry(database, MigrationStore(database)).register(
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
    store = MigrationProposalStore(database)
    proposal_id = store.record_rejection(
        subject_id="Noyra-0001",
        target_id="target-1",
        reason_code="storage_pressure",
        reason="declined",
        policy_revision=1,
        cooldown_seconds=3600,
        actor="operator",
    )
    assert proposal_id
    assert store.next_eligible_at("Noyra-0001", "target-1", "storage_pressure") is not None
    assert store.next_eligible_at("Noyra-0001", "target-2", "storage_pressure") is None
