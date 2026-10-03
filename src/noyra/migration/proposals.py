"""Deterministic proposal construction and rejection cooldowns."""

# Proposal SQL and hash envelopes stay adjacent for auditability.
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import NotFoundError
from noyra.core.redaction import redact_secret_text, redact_secrets
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

from .discovery import MigrationNeed, NeedAssessment
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
        target_id = getattr(candidate, "target_id", "")
        if not target_id:
            return None
        if policy.allowed_target_ids and target_id not in policy.allowed_target_ids:
            return None
        if getattr(candidate, "observation_verified", True) is False:
            return None
        benefit = float(need.benefit_score)
        if not 0.0 <= benefit <= 1.0 or not need.actionable:
            return None
        if need.payment_in_flight or need.maintenance_conflict:
            return None
        if getattr(candidate, "status", "active") != "active":
            return None
        if not bool(getattr(candidate, "encrypted_volume", True)):
            return None
        if int(getattr(candidate, "free_bytes", 0)) < policy.min_free_bytes:
            return None
        candidate_cost = int(
            getattr(candidate, "cost_microusd_month", getattr(candidate, "cost", 0)) or 0
        )
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
            "target_active": getattr(candidate, "status", "active") == "active",
            "target_volume_encrypted": bool(getattr(candidate, "encrypted_volume", True)),
            "target_capacity": int(getattr(candidate, "free_bytes", 0)) >= policy.min_free_bytes,
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
                    "target_resources": str(
                        getattr(candidate, "observation_evidence_hash", "")
                        or content_hash(
                            {
                                "target_id": target_id,
                                "free_bytes": int(getattr(candidate, "free_bytes", 0)),
                                "region": getattr(candidate, "region", None),
                            }
                        )
                    ),
                },
                "estimated_downtime_seconds": downtime,
                "key_plan": policy.wallet_mode,
                "migration_execution_ready": False,
                "data_plan": (
                    "blocked: recipient-encrypted bundle and target wallet/credential "
                    "binding are not verified"
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
            payment_in_flight=bool(need.get("payment_in_flight", False)),
            maintenance_conflict=bool(need.get("maintenance_conflict", False)),
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
        if cooldown_seconds < 0 or cooldown_seconds > 31_536_000:
            raise ValueError("cooldown is outside safety bounds")
        if not reason_code.strip() or not reason.strip():
            raise ValueError("rejection reason is required")
        now = datetime.now(UTC)
        created_at = now.isoformat(timespec="milliseconds")
        cooldown_until = (now + timedelta(seconds=cooldown_seconds)).isoformat(
            timespec="milliseconds"
        )
        rejection_id = new_id("migrationrejection")
        proposal_id = new_id("migrationproposal")
        with self.database.transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM subject_identity WHERE subject_id=?", (subject_id,)
                ).fetchone()
                is None
            ):
                raise NotFoundError(f"subject not found: {subject_id}")
            if (
                connection.execute(
                    "SELECT 1 FROM migration_targets WHERE target_id=? AND subject_id=?",
                    (target_id, subject_id),
                ).fetchone()
                is None
            ):
                raise NotFoundError(f"migration target not found: {target_id}")
            connection.execute(
                """INSERT INTO migration_proposals(
                   proposal_id,subject_id,target_id,policy_revision,status,reason_code,reason,
                   evidence_json,benefit_score,risk_score,expires_at,created_at,decided_at,
                   decision_reason,state_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    proposal_id,
                    subject_id,
                    target_id,
                    policy_revision,
                    "rejected",
                    reason_code.strip(),
                    reason.strip(),
                    "{}",
                    0.0,
                    1.0,
                    cooldown_until,
                    created_at,
                    created_at,
                    reason.strip(),
                    content_hash(
                        {
                            "proposal_id": proposal_id,
                            "subject_id": subject_id,
                            "target_id": target_id,
                            "status": "rejected",
                            "reason_code": reason_code.strip(),
                            "reason": reason.strip(),
                            "policy_revision": policy_revision,
                            "created_at": created_at,
                        }
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO migration_rejections(rejection_id,subject_id,proposal_id,target_id,reason_code,reason,cooldown_until,policy_revision,actor,created_at,state_hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rejection_id,
                    subject_id,
                    proposal_id,
                    target_id,
                    reason_code.strip(),
                    reason.strip(),
                    cooldown_until,
                    policy_revision,
                    actor.strip(),
                    created_at,
                    content_hash(
                        {
                            "rejection_id": rejection_id,
                            "subject_id": subject_id,
                            "target_id": target_id,
                            "reason_code": reason_code.strip(),
                            "reason": reason.strip(),
                            "cooldown_until": cooldown_until,
                            "policy_revision": policy_revision,
                            "actor": actor.strip(),
                            "created_at": created_at,
                        }
                    ),
                ),
            )
        return rejection_id

    def next_eligible_at(self, subject_id: str, target_id: str, reason_code: str) -> str | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT cooldown_until FROM migration_rejections WHERE subject_id=? AND target_id=? AND reason_code=? ORDER BY cooldown_until DESC LIMIT 1",
                (subject_id, target_id, reason_code),
            ).fetchone()
        if row is None:
            return None
        value = str(row["cooldown_until"])
        return value if value > utc_now() else None
