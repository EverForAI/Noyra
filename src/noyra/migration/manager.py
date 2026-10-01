"""Durable migration task state machine."""

# The SQL transition statements are kept close to their state changes.
# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import NotFoundError
from noyra.core.types import content_hash, new_id, utc_now

from .policy import MigrationStore


@dataclass(frozen=True)
class MigrationProposalRecord:
    proposal_id: str
    subject_id: str
    target_id: str
    policy_revision: int
    status: str
    reason_code: str
    reason: str
    expires_at: str


@dataclass(frozen=True)
class MigrationTask:
    task_id: str
    proposal_id: str
    subject_id: str
    target_id: str
    idempotency_key: str
    status: str
    policy_revision: int
    expires_at: str


class MigrationManager:
    def __init__(self, database: Database, policy_store: MigrationStore):
        self.database = database
        self.policy_store = policy_store

    def create_proposal(self, *, subject_id: str, target_id: str, policy_revision: int, reason_code: str, reason: str, expires_at: str) -> MigrationProposalRecord:
        if not reason.strip() or len(reason) > 2048 or not reason_code.strip():
            raise ValueError("migration proposal reason is invalid")
        with self.database.transaction() as c:
            proposal_id = new_id("migrationproposal")
            c.execute("INSERT INTO migration_proposals(proposal_id,subject_id,target_id,policy_revision,status,reason_code,reason,evidence_json,benefit_score,risk_score,expires_at,created_at,state_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (proposal_id, subject_id, target_id, policy_revision, "awaiting_approval", reason_code.strip(), reason.strip(), "{}", 0.5, 0.5, expires_at, utc_now(), content_hash({"proposal_id": proposal_id, "subject_id": subject_id, "target_id": target_id, "policy_revision": policy_revision, "reason_code": reason_code.strip(), "reason": reason.strip(), "expires_at": expires_at})))
            return MigrationProposalRecord(proposal_id, subject_id, target_id, policy_revision, "awaiting_approval", reason_code.strip(), reason.strip(), expires_at)

    def approve(self, proposal_id: str, *, actor: str, idempotency_key: str) -> MigrationTask:
        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise ValueError("idempotency key is invalid")
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM migration_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"migration proposal not found: {proposal_id}")
            if datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) <= datetime.now(UTC):
                raise ValueError("migration proposal is expired")
            existing = c.execute("SELECT * FROM migration_tasks WHERE subject_id=? AND idempotency_key=?", (row["subject_id"], idempotency_key)).fetchone()
            if existing is not None:
                return self._task(existing)
            task_id = new_id("migrationtask")
            now = utc_now()
            c.execute("INSERT INTO migration_tasks(task_id,proposal_id,subject_id,target_id,idempotency_key,source_epoch,policy_revision,status,expires_at,created_at,updated_at,state_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (task_id, proposal_id, row["subject_id"], row["target_id"], idempotency_key, "source-epoch-1", row["policy_revision"], "approved", row["expires_at"], now, now, content_hash({"task_id": task_id, "proposal_id": proposal_id, "subject_id": row["subject_id"], "target_id": row["target_id"], "idempotency_key": idempotency_key, "status": "approved", "policy_revision": row["policy_revision"], "created_at": now})))
            c.execute("UPDATE migration_proposals SET status='approved', decided_at=?, decision_reason=? WHERE proposal_id=?", (now, actor.strip(), proposal_id))
            return self._task(c.execute("SELECT * FROM migration_tasks WHERE task_id=?", (task_id,)).fetchone())

    @staticmethod
    def _task(row: Any) -> MigrationTask:
        return MigrationTask(row["task_id"], row["proposal_id"], row["subject_id"], row["target_id"], row["idempotency_key"], row["status"], int(row["policy_revision"]), row["expires_at"])
