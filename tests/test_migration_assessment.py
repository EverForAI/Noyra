from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.migration.discovery import MigrationNeed
from noyra.migration.policy import MigrationPolicy, MigrationStore
from noyra.migration.proposals import MigrationProposalBuilder, MigrationProposalStore
from noyra.migration.targets import TargetRegistry
from noyra.migration.trust import TrustDecision


def _context(tmp_path: Any) -> Any:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-0001", "e" * 64)
    policy = MigrationPolicy.default("Noyra-0001").with_updates(
        enabled=True,
        allowed_target_ids=("target-1",),
        min_free_bytes=1024,
        max_downtime_seconds=7200,
    )
    store = MigrationProposalStore(database)
    return database, policy, store


def _candidate(**changes: Any) -> Any:
    return SimpleNamespace(
        target_id="target-1",
        status="active",
        region="test",
        encrypted_volume=True,
        free_bytes=8_000_000_000,
        cost_microusd_month=0,
        trust_level=5,
        release_sha="a" * 40,
        observation_evidence_hash="f" * 64,
        **changes,
    )


def _need(**changes: Any) -> Any:
    values: dict[str, Any] = {
        "source_health": 0.2,
        "workload": 0.6,
        "storage_pressure": 0.9,
        "provider_health": 0.3,
        "payment_in_flight": False,
        "maintenance_conflict": False,
        "evidence": {"storage": "a" * 64, "health": "b" * 64},
    }
    values.update(changes)
    return MigrationNeed.assess(**values)


def test_resources_alone_or_need_alone_never_create_a_proposal(tmp_path: Any) -> None:
    _, policy, store = _context(tmp_path)
    builder = MigrationProposalBuilder(rejection_store=store)

    assert (
        builder.build(
            subject_id="Noyra-0001",
            candidate=_candidate(),
            need=None,
            trust=TrustDecision(True),
            policy=policy,
        )
        is None
    )
    assert (
        builder.build(
            subject_id="Noyra-0001",
            candidate=None,
            need=_need(),
            trust=TrustDecision(False, ("target_missing",)),
            policy=policy,
        )
        is None
    )


def test_payment_or_maintenance_conflicts_block_proposal(tmp_path: Any) -> None:
    _, policy, store = _context(tmp_path)
    builder = MigrationProposalBuilder(rejection_store=store)

    for need in (_need(payment_in_flight=True), _need(maintenance_conflict=True)):
        assert (
            builder.build(
                subject_id="Noyra-0001",
                candidate=_candidate(),
                need=need,
                trust=TrustDecision(True),
                policy=policy,
            )
            is None
        )


def test_proposal_records_hard_gates_scores_key_and_rollback_plans(tmp_path: Any) -> None:
    _, policy, store = _context(tmp_path)

    proposal = MigrationProposalBuilder(rejection_store=store).build(
        subject_id="Noyra-0001",
        candidate=_candidate(),
        need=_need(),
        trust=TrustDecision(True),
        policy=policy,
        estimated_downtime_seconds=1800,
    )

    assert proposal is not None
    assert proposal.reason_code == "storage_pressure"
    assert proposal.evidence["hard_gates"]["target_trusted"] is True
    assert proposal.evidence["evidence_hashes"]["target_resources"] == "f" * 64
    assert proposal.evidence["estimated_downtime_seconds"] == 1800
    assert proposal.evidence["key_plan"] == "external_signer_rebind"
    assert proposal.evidence["rollback_plan"]
    assert proposal.benefit_score > proposal.risk_score


def test_rejection_cooldown_cannot_be_bypassed_with_new_proposal_text(tmp_path: Any) -> None:
    database, policy, store = _context(tmp_path)
    private = Ed25519PrivateKey.generate()
    TargetRegistry(database, MigrationStore(database)).register(
        "Noyra-0001",
        target_id="target-1",
        public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(),
        endpoint="https://target.example/migration",
        capabilities={},
        region="test",
        provider="test",
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    store.record_rejection(
        subject_id="Noyra-0001",
        target_id="target-1",
        reason_code="storage_pressure",
        reason="denied",
        policy_revision=policy.revision,
        cooldown_seconds=3600,
        actor="operator",
    )
    proposal = MigrationProposalBuilder(rejection_store=store).build(
        subject_id="Noyra-0001",
        candidate=_candidate(),
        need=_need(),
        trust=TrustDecision(True),
        policy=policy,
    )

    assert proposal is None
