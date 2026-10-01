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
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

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


class MigrationProposalBuilder:
    def build(
        self,
        *,
        subject_id: str,
        candidate: Any,
        need: Mapping[str, Any] | None,
        trust: TrustDecision,
        policy: MigrationPolicy,
    ) -> MigrationProposal | None:
        if not policy.enabled or policy.approval_mode == "disabled" or need is None:
            return None
        if not trust.accepted:
            return None
        target_id = getattr(candidate, "target_id", "")
        if policy.allowed_target_ids and target_id not in policy.allowed_target_ids:
            return None
        benefit = float(need.get("benefit_score", 0.0))
        if not 0.0 <= benefit <= 1.0 or benefit <= 0.5:
            return None
        reason_code = str(need.get("reason_code", "resource_rebalance"))
        reason = str(need.get("reason", "migration opportunity identified"))
        if not reason_code or len(reason_code) > 64 or not reason.strip() or len(reason) > 2048:
            return None
        evidence = need.get("evidence", {})
        if not isinstance(evidence, Mapping) or len(canonical_json(evidence).encode()) > 16_384:
            return None
        risk = min(1.0, max(0.0, 0.2 + float(need.get("storage_pressure", 0.0)) * 0.2))
        created = utc_now()
        expiry = (datetime.now(UTC) + timedelta(seconds=policy.proposal_expiry_seconds)).isoformat(timespec="milliseconds")
        return MigrationProposal(
            proposal_id=new_id("migrationproposal"), subject_id=subject_id, target_id=target_id,
            policy_revision=policy.revision, status="awaiting_approval" if policy.approval_mode == "manual" else "approved",
            reason_code=reason_code, reason=reason.strip(), evidence=dict(evidence), benefit_score=benefit,
            risk_score=risk, created_at=created, expires_at=expiry,
        )


class MigrationProposalStore:
    def __init__(self, database: Database):
        self.database = database

    def record_rejection(
        self, *, subject_id: str, target_id: str, reason_code: str, reason: str,
        policy_revision: int, cooldown_seconds: int, actor: str,
    ) -> str:
        if cooldown_seconds < 0 or cooldown_seconds > 31_536_000:
            raise ValueError("cooldown is outside safety bounds")
        if not reason_code.strip() or not reason.strip():
            raise ValueError("rejection reason is required")
        now = datetime.now(UTC)
        created_at = now.isoformat(timespec="milliseconds")
        cooldown_until = (now + timedelta(seconds=cooldown_seconds)).isoformat(timespec="milliseconds")
        rejection_id = new_id("migrationrejection")
        proposal_id = new_id("migrationproposal")
        with self.database.transaction() as connection:
            if connection.execute("SELECT 1 FROM subject_identity WHERE subject_id=?", (subject_id,)).fetchone() is None:
                raise NotFoundError(f"subject not found: {subject_id}")
            if connection.execute("SELECT 1 FROM migration_targets WHERE target_id=? AND subject_id=?", (target_id, subject_id)).fetchone() is None:
                raise NotFoundError(f"migration target not found: {target_id}")
            connection.execute(
                """INSERT INTO migration_proposals(
                   proposal_id,subject_id,target_id,policy_revision,status,reason_code,reason,
                   evidence_json,benefit_score,risk_score,expires_at,created_at,decided_at,
                   decision_reason,state_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    proposal_id, subject_id, target_id, policy_revision, "rejected", reason_code.strip(),
                    reason.strip(), "{}", 0.0, 1.0, cooldown_until, created_at, created_at,
                    reason.strip(), content_hash({"proposal_id": proposal_id, "subject_id": subject_id, "target_id": target_id, "status": "rejected", "reason_code": reason_code.strip(), "reason": reason.strip(), "policy_revision": policy_revision, "created_at": created_at}),
                ),
            )
            connection.execute(
                """INSERT INTO migration_rejections(rejection_id,subject_id,proposal_id,target_id,reason_code,reason,cooldown_until,policy_revision,actor,created_at,state_hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (rejection_id, subject_id, proposal_id, target_id, reason_code.strip(), reason.strip(), cooldown_until, policy_revision, actor.strip(), created_at,
                 content_hash({"rejection_id": rejection_id, "subject_id": subject_id, "target_id": target_id, "reason_code": reason_code.strip(), "reason": reason.strip(), "cooldown_until": cooldown_until, "policy_revision": policy_revision, "actor": actor.strip(), "created_at": created_at})),
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
