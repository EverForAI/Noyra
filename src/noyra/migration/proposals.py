"""Deterministic proposal construction and rejection cooldowns."""

# Proposal SQL and hash envelopes stay adjacent for auditability.
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from noyra.core.database import Database
from noyra.core.redaction import redact_secret_text, redact_secrets
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

from .discovery import DiscoveryResult, MigrationNeed, NeedAssessment
from .policy import MigrationPolicy
from .trust import TrustDecision


@dataclass(frozen=True)
class MigrationProposal:
    proposal_id: str
    subject_id: str
    target_id: str
    policy_revision: int
    status: str
    reason_code: str
    reason: str
    evidence: dict[str, Any]
    benefit_score: float
    risk_score: float
    created_at: str
    expires_at: str

    @property
    def hard_gates(self) -> dict[str, bool]:
        value = self.evidence.get("hard_gates", {})
        return dict(value) if isinstance(value, Mapping) else {}

    @property
    def estimated_downtime_seconds(self) -> int:
        value = self.evidence.get("estimated_downtime_seconds", 0)
        return int(value) if type(value) is int else 0

    @property
    def key_plan(self) -> str:
        return str(self.evidence.get("key_plan", "external_signer_rebind"))

    @property
    def rollback_plan(self) -> str:
        return str(
            self.evidence.get("rollback_plan", "revoke target epoch and retain source authority")
        )


class MigrationProposalBuilder:
    def __init__(self, rejection_store: MigrationProposalStore | None = None) -> None:
        self.rejection_store = rejection_store

    def build(
        self,
        *,
        subject_id: str,
        candidate: Any,
        need: Mapping[str, Any] | NeedAssessment | None,
        trust: TrustDecision,
        policy: MigrationPolicy,
        estimated_downtime_seconds: int | None = None,
    ) -> MigrationProposal | None:
        if not policy.enabled or policy.approval_mode == "disabled" or need is None:
            return None
        if not trust.accepted:
            return None
        need = self._need(need)
        if (
            not isinstance(candidate, DiscoveryResult)
            or candidate.trusted is not True
            or candidate.resources_verified is not True
            or candidate.eligible is not True
            or candidate.reasons
            or candidate.observation is None
            or not candidate.observation.is_fresh()
        ):
            return None
        target_id = candidate.target_id
        if policy.allowed_target_ids and target_id not in policy.allowed_target_ids:
            return None
        benefit = float(need.benefit_score)
        if not 0.0 <= benefit <= 1.0 or not need.actionable:
            return None
        if need.payment_in_flight or need.maintenance_conflict:
            return None
        if candidate.status != "active":
            return None
        if candidate.encrypted_volume is not True or candidate.trust_level < policy.trust_level:
            return None
        if candidate.free_bytes < policy.min_free_bytes:
            return None
        if policy.allowed_regions and candidate.region not in policy.allowed_regions:
            return None
        candidate_cost = candidate.cost_microusd_month
        if candidate_cost > policy.max_cost_microusd:
            return None
        downtime = estimated_downtime_seconds
        if downtime is None:
            downtime = int(
                need.evidence.get(
                    "estimated_downtime_seconds", min(policy.max_downtime_seconds, 3600)
                )
            )
        if type(downtime) is not int or not 0 <= downtime <= policy.max_downtime_seconds:
            return None
        reason_code = str(need.reason_code)
        reason = redact_secret_text(str(need.reason))
        if not reason_code or len(reason_code) > 64 or not reason.strip() or len(reason) > 2048:
            return None
        if (
            self.rejection_store is not None
            and self.rejection_store.next_eligible_at(subject_id, target_id, reason_code)
            is not None
        ):
            return None
        risk = self._risk(candidate, need, policy, downtime)
        if risk >= benefit:
            return None
        hard_gates = {
            "migration_enabled": True,
            "target_trusted": bool(trust.accepted),
            "target_active": candidate.status == "active",
            "target_resources_verified": True,
            "target_volume_encrypted": candidate.encrypted_volume,
            "target_capacity": candidate.free_bytes >= policy.min_free_bytes,
            "source_need": need.actionable,
            "no_payment_in_flight": not need.payment_in_flight,
            "no_maintenance_conflict": not need.maintenance_conflict,
            "within_downtime_limit": True,
            "cooldown_clear": True,
        }
        evidence = redact_secrets(dict(need.evidence))
        evidence.update(
            {
                "hard_gates": hard_gates,
                "evidence_hashes": {
                    "source_need": content_hash(need.evidence),
                    "target_resources": candidate.observation.evidence_hash,
                },
                "estimated_downtime_seconds": downtime,
                "key_plan": policy.wallet_mode,
                "migration_execution_ready": True,
                "target": {
                    "target_id": target_id,
                    "endpoint": candidate.endpoint,
                    "region": candidate.region,
                    "release_sha": candidate.release_sha,
                    "free_bytes": candidate.free_bytes,
                    "cost_microusd_month": candidate_cost,
                    "latency_ms": candidate.latency_ms,
                    "trust_level": candidate.trust_level,
                    "observed_at": candidate.observation.observed_at,
                },
                "data_plan": (
                    "recipient-encrypted bundle; target wallet/credential binding and "
                    "volume proof required before activation"
                ),
                "rollback_plan": "revoke target epoch and retain source authority until commit is verified",
            }
        )
        if len(canonical_json(evidence).encode()) > 16_384:
            raise ValueError("migration proposal evidence is too large")
        created = utc_now()
        expiry = (datetime.now(UTC) + timedelta(seconds=policy.proposal_expiry_seconds)).isoformat(
            timespec="milliseconds"
        )
        return MigrationProposal(
            proposal_id=new_id("migrationproposal"),
            subject_id=subject_id,
            target_id=target_id,
            policy_revision=policy.revision,
            status="awaiting_approval" if policy.approval_mode == "manual" else "approved",
            reason_code=reason_code,
            reason=reason.strip(),
            evidence=evidence,
            benefit_score=benefit,
            risk_score=risk,
            created_at=created,
            expires_at=expiry,
        )

    @staticmethod
    def _need(need: Mapping[str, Any] | NeedAssessment) -> NeedAssessment:
        if isinstance(need, NeedAssessment):
            return need
        if not isinstance(need, Mapping):
            return MigrationNeed.assess(source_health=1.0)
        assessment = MigrationNeed.assess(
            source_health=float(need.get("source_health", 1.0)),
            workload=float(need.get("workload", 0.0)),
            storage_pressure=float(need.get("storage_pressure", 0.0)),
            provider_health=float(need.get("provider_health", 1.0)),
            evidence=need.get("evidence", {}),
            payment_in_flight=need.get("payment_in_flight", False),
            maintenance_conflict=need.get("maintenance_conflict", False),
            reason_code=need.get("reason_code"),
            reason=need.get("reason"),
        )
        if "benefit_score" in need:
            benefit = float(need["benefit_score"])
            if not 0.0 <= benefit <= 1.0:
                raise ValueError("migration benefit score is invalid")
            from dataclasses import replace

            assessment = replace(assessment, benefit_score=benefit)
        return assessment

    @staticmethod
    def _risk(
        candidate: Any, need: NeedAssessment, policy: MigrationPolicy, downtime: int
    ) -> float:
        latency = min(1.0, int(getattr(candidate, "latency_ms", 0) or 0) / 2000)
        cost = min(
            1.0,
            int(getattr(candidate, "cost_microusd_month", 0) or 0)
            / max(policy.max_cost_microusd, 1),
        )
        outage = min(1.0, downtime / max(policy.max_downtime_seconds, 1))
        return min(
            1.0, 0.25 * latency + 0.2 * cost + 0.35 * outage + 0.2 * (1.0 - need.source_health)
        )


class MigrationProposalStore:
    def __init__(self, database: Database):
        self.database = database

    def record_rejection(
        self,
        *,
        subject_id: str,
        target_id: str,
        reason_code: str,
        reason: str,
        policy_revision: int,
        cooldown_seconds: int,
        actor: str,
    ) -> str:
        from .manager import MigrationManager
        from .policy import MigrationStore

        return MigrationManager(self.database, MigrationStore(self.database)).record_rejection(
            subject_id=subject_id,
            target_id=target_id,
            reason_code=reason_code,
            reason=reason,
            policy_revision=policy_revision,
            cooldown_seconds=cooldown_seconds,
            actor=actor,
        )

    def next_eligible_at(self, subject_id: str, target_id: str, reason_code: str) -> str | None:
        from .manager import MigrationManager

        with self.database.connection() as connection:
            return MigrationManager.cooldown_until(connection, subject_id, target_id)
