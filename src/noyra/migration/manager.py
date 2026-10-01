"""Durable migration proposal and task state machine."""

# SQL transition statements are kept close to their state changes.
# ruff: noqa: E501

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from noyra.core.admission import assert_current_lease
from noyra.core.database import Database
from noyra.core.errors import NotFoundError
from noyra.core.identity import validate_subject_id
from noyra.core.redaction import redact_secret_text, redact_secrets
from noyra.core.types import content_hash, new_id, utc_now

from .policy import MigrationPolicy, MigrationStore
from .targets import TargetRegistry

_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"preflight", "awaiting_approval", "cancelled", "failed"}),
    "preflight": frozenset({"awaiting_approval", "approved", "cancelled", "failed"}),
    "awaiting_approval": frozenset({"approved", "rejected", "expired", "cancelled"}),
    "approved": frozenset({"preparing", "rolling_back", "cancelled", "failed"}),
    "preparing": frozenset(
        {
            "transferring",
            "restoring",
            "validating",
            "cutover",
            "committed",
            "rolling_back",
            "failed",
        }
    ),
    "transferring": frozenset({"restoring", "validating", "rolling_back", "failed"}),
    "restoring": frozenset({"validating", "rolling_back", "failed"}),
    "validating": frozenset({"cutover", "committed", "rolling_back", "failed"}),
    "cutover": frozenset({"committed", "rolling_back", "failed"}),
    "committed": frozenset(),
    "rolling_back": frozenset({"rolled_back", "failed"}),
    "rolled_back": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset({"preflight", "rolling_back", "cancelled"}),
}
_PROPOSAL_ACTIVE = frozenset({"planned", "awaiting_approval", "approved"})


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
    source_epoch: str
    status: str
    policy_revision: int
    expires_at: str
    manifest_digest: str | None = None
    artifact_id: str | None = None
    error_code: str | None = None
    target_epoch_id: str | None = None
    created_at: str = ""
    updated_at: str = ""


class MigrationManager:
    def __init__(self, database: Database, policy_store: MigrationStore):
        self.database = database
        self.policy_store = policy_store

    def create_proposal(
        self,
        *,
        subject_id: str,
        target_id: str,
        policy_revision: int,
        reason_code: str,
        reason: str,
        expires_at: str,
        evidence: dict[str, Any] | None = None,
        benefit_score: float = 0.5,
        risk_score: float = 0.5,
    ) -> MigrationProposalRecord:
        assert_current_lease()
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2048:
            raise ValueError("migration proposal reason is invalid")
        if not isinstance(reason_code, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", reason_code.strip()
        ):
            raise ValueError("migration proposal reason code is invalid")
        if type(policy_revision) is not int or policy_revision < 1:
            raise ValueError("migration policy revision is invalid")
        self._parse_timestamp(expires_at)
        if type(benefit_score) is not float or not 0.0 <= benefit_score <= 1.0:
            raise ValueError("migration benefit score is invalid")
        if type(risk_score) is not float or not 0.0 <= risk_score <= 1.0:
            raise ValueError("migration risk score is invalid")
        evidence = {} if evidence is None else dict(evidence)
        evidence = redact_secrets(evidence)
        safe_reason = redact_secret_text(reason.strip())
        if len(evidence) > 64 or len(self._json(evidence).encode()) > 16_384:
            raise ValueError("migration proposal evidence is too large")
        with self.database.transaction() as connection:
            MigrationStore._ensure_policy(connection, subject_id)
            policy_row = connection.execute(
                "SELECT * FROM migration_policies WHERE subject_id=?", (subject_id,)
            ).fetchone()
            policy = MigrationPolicy.from_record(policy_row)
            if not policy.enabled or policy.approval_mode == "disabled":
                raise ValueError("migration is disabled")
            if policy.revision != policy_revision:
                raise ValueError("migration policy revision is stale")
            self._ensure_target(connection, subject_id, target_id)
            proposal_id = new_id("migrationproposal")
            created_at = utc_now()
            values = {
                "proposal_id": proposal_id,
                "subject_id": subject_id,
                "target_id": target_id,
                "policy_revision": policy_revision,
                "status": "awaiting_approval",
                "reason_code": reason_code.strip(),
                "reason": safe_reason,
                "evidence_json": self._json(evidence),
                "benefit_score": benefit_score,
                "risk_score": risk_score,
                "expires_at": expires_at,
                "created_at": created_at,
            }
            connection.execute(
                "INSERT INTO migration_proposals(proposal_id,subject_id,target_id,policy_revision,status,reason_code,reason,evidence_json,benefit_score,risk_score,expires_at,created_at,state_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*values.values(), self._proposal_hash(values)),
            )
            MigrationStore._append_audit(
                connection,
                subject_id,
                "migration_proposal_created",
                "planner",
                {
                    "proposal_id": proposal_id,
                    "target_id": target_id,
                    "policy_revision": policy_revision,
                },
            )
            return MigrationProposalRecord(
                proposal_id,
                subject_id,
                target_id,
                policy_revision,
                "awaiting_approval",
                reason_code.strip(),
                safe_reason,
                expires_at,
            )

    def approve(self, proposal_id: str, *, actor: str, idempotency_key: str) -> MigrationTask:
        assert_current_lease()
        self._validate_actor(actor)
        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise ValueError("idempotency key is invalid")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM migration_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"migration proposal not found: {proposal_id}")
            self._assert_proposal_integrity(row)
            existing = connection.execute(
                "SELECT * FROM migration_tasks WHERE subject_id=? AND idempotency_key=?",
                (row["subject_id"], idempotency_key),
            ).fetchone()
            if existing is not None:
                self._assert_task_integrity(existing)
                if existing["proposal_id"] != proposal_id:
                    raise ValueError("idempotency key belongs to another migration proposal")
                return self._task(existing)
            if row["status"] not in _PROPOSAL_ACTIVE:
                raise ValueError("migration proposal is not awaiting approval")
            if self._parse_timestamp(row["expires_at"]) <= datetime.now(UTC):
                self._set_proposal_status(connection, row, "expired", actor, "proposal expired")
                raise ValueError("migration proposal is expired")
            self._ensure_target(connection, row["subject_id"], row["target_id"])
            current_revision = self._policy_revision(connection, row["subject_id"])
            if current_revision is not None and current_revision != int(row["policy_revision"]):
                raise ValueError("migration policy revision is stale")
            now = utc_now()
            epoch_row = connection.execute(
                "SELECT version FROM runtime_state WHERE subject_id=?", (row["subject_id"],)
            ).fetchone()
            source_epoch = (
                f"runtime-{int(epoch_row['version'])}"
                if epoch_row is not None
                else "source-epoch-1"
            )
            task_id = new_id("migrationtask")
            task_values = {
                "task_id": task_id,
                "proposal_id": proposal_id,
                "subject_id": row["subject_id"],
                "target_id": row["target_id"],
                "idempotency_key": idempotency_key,
                "source_epoch": source_epoch,
                "policy_revision": int(row["policy_revision"]),
                "status": "approved",
                "manifest_digest": None,
                "artifact_id": None,
                "error_code": None,
                "target_epoch_id": None,
                "expires_at": row["expires_at"],
                "created_at": now,
                "updated_at": now,
            }
            connection.execute(
                "INSERT INTO migration_tasks(task_id,proposal_id,subject_id,target_id,idempotency_key,source_epoch,policy_revision,status,manifest_digest,artifact_id,error_code,target_epoch_id,expires_at,created_at,updated_at,state_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*task_values.values(), self._task_hash(task_values)),
            )
            self._set_proposal_status(connection, row, "approved", actor, actor.strip())
            MigrationStore._append_audit(
                connection,
                row["subject_id"],
                "migration_task_created",
                actor.strip(),
                {"task_id": task_id, "proposal_id": proposal_id, "source_epoch": source_epoch},
            )
            return self._task(
                connection.execute(
                    "SELECT * FROM migration_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            )

    def reject(self, proposal_id: str, *, actor: str, reason: str) -> MigrationProposalRecord:
        assert_current_lease()
        self._validate_actor(actor)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise ValueError("rejection reason is required")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM migration_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"migration proposal not found: {proposal_id}")
            self._assert_proposal_integrity(row)
            if row["status"] == "rejected":
                # Replaying a rejection is safe and must not append a second
                # cooldown or audit event. A different decision requires a new
                # proposal after the existing cooldown expires.
                return self._proposal(row)
            if row["status"] not in _PROPOSAL_ACTIVE:
                raise ValueError("migration proposal is not awaiting approval")
            policy_row = connection.execute(
                "SELECT rejection_cooldown_seconds FROM migration_policies WHERE subject_id=?",
                (row["subject_id"],),
            ).fetchone()
            cooldown = int(policy_row["rejection_cooldown_seconds"]) if policy_row else 604800
            now = datetime.now(UTC)
            cooldown_text = datetime.fromtimestamp(now.timestamp() + cooldown, UTC).isoformat(
                timespec="milliseconds"
            )
            self._set_proposal_status(connection, row, "rejected", actor, reason.strip())
            rejection_id = new_id("migrationrejection")
            connection.execute(
                "INSERT INTO migration_rejections(rejection_id,subject_id,proposal_id,target_id,reason_code,reason,cooldown_until,policy_revision,actor,created_at,state_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rejection_id,
                    row["subject_id"],
                    proposal_id,
                    row["target_id"],
                    row["reason_code"],
                    reason.strip(),
                    cooldown_text,
                    int(row["policy_revision"]),
                    actor.strip(),
                    now.isoformat(timespec="milliseconds"),
                    content_hash(
                        {
                            "rejection_id": rejection_id,
                            "proposal_id": proposal_id,
                            "subject_id": row["subject_id"],
                            "target_id": row["target_id"],
                            "reason_code": row["reason_code"],
                            "reason": reason.strip(),
                            "cooldown_until": cooldown_text,
                            "policy_revision": int(row["policy_revision"]),
                            "actor": actor.strip(),
                        }
                    ),
                ),
            )
            MigrationStore._append_audit(
                connection,
                row["subject_id"],
                "migration_proposal_rejected",
                actor.strip(),
                {
                    "proposal_id": proposal_id,
                    "rejection_id": rejection_id,
                    "cooldown_until": cooldown_text,
                },
            )
            return self._proposal(
                connection.execute(
                    "SELECT * FROM migration_proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
            )

    def transition_task(
        self,
        task_id: str,
        status: str,
        *,
        actor: str,
        expected_status: str | None = None,
        manifest_digest: str | None = None,
        artifact_id: str | None = None,
        error_code: str | None = None,
        target_epoch_id: str | None = None,
    ) -> MigrationTask:
        assert_current_lease()
        self._validate_actor(actor)
        if status not in _TASK_TRANSITIONS:
            raise ValueError("migration task status is invalid")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM migration_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"migration task not found: {task_id}")
            self._assert_task_integrity(row)
            if expected_status is not None and row["status"] != expected_status:
                raise ValueError("migration task status changed")
            if row["status"] == status:
                return self._task(row)
            if status not in _TASK_TRANSITIONS[row["status"]]:
                raise ValueError("migration task transition is invalid")
            current_revision = self._policy_revision(connection, row["subject_id"])
            if current_revision is not None and current_revision != int(row["policy_revision"]):
                raise ValueError("migration policy revision is stale")
            now = utc_now()
            values = dict(row)
            if target_epoch_id is not None:
                epoch = connection.execute(
                    "SELECT subject_id,target_id,status FROM migration_epochs WHERE epoch_id=?",
                    (target_epoch_id,),
                ).fetchone()
                if epoch is None:
                    raise ValueError("migration target epoch not found")
                if (
                    epoch["subject_id"] != row["subject_id"]
                    or epoch["target_id"] != row["target_id"]
                ):
                    raise ValueError("migration target epoch does not match task")
                if epoch["status"] != "active":
                    raise ValueError("migration target epoch is not active")
            values.update(
                {
                    "status": status,
                    "manifest_digest": manifest_digest
                    if manifest_digest is not None
                    else row["manifest_digest"],
                    "artifact_id": artifact_id if artifact_id is not None else row["artifact_id"],
                    "error_code": error_code if error_code is not None else row["error_code"],
                    "target_epoch_id": (
                        target_epoch_id if target_epoch_id is not None else row["target_epoch_id"]
                    ),
                    "updated_at": now,
                }
            )
            result = connection.execute(
                "UPDATE migration_tasks SET status=?,manifest_digest=?,artifact_id=?,error_code=?,target_epoch_id=?,updated_at=?,state_hash=? WHERE task_id=? AND status=?",
                (
                    values["status"],
                    values["manifest_digest"],
                    values["artifact_id"],
                    values["error_code"],
                    values["target_epoch_id"],
                    values["updated_at"],
                    self._task_hash(values),
                    task_id,
                    row["status"],
                ),
            )
            if result.rowcount != 1:
                raise ValueError("migration task status changed")
            audit_action = (
                "migration_task_cancelled"
                if status == "cancelled"
                else "migration_task_transitioned"
            )
            audit_payload = {"task_id": task_id, "from": row["status"], "to": status}
            if status == "cancelled":
                audit_payload["reason"] = values["error_code"]
            MigrationStore._append_audit(
                connection,
                row["subject_id"],
                audit_action,
                actor.strip(),
                audit_payload,
            )
            return self._task(
                connection.execute(
                    "SELECT * FROM migration_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            )

    def get_proposal(self, subject_id: str, proposal_id: str) -> dict[str, Any]:
        validate_subject_id(subject_id)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM migration_proposals WHERE proposal_id=? AND subject_id=?",
                (proposal_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"migration proposal not found: {proposal_id}")
        self._assert_proposal_integrity(row)
        return self._proposal_projection(row)

    def list_proposals(self, subject_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        validate_subject_id(subject_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("migration proposal limit is invalid")
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM migration_proposals WHERE subject_id=? "
                "ORDER BY created_at DESC,proposal_id DESC LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        return [self._proposal_projection(row) for row in rows]

    def get_task(self, task_id: str, *, subject_id: str | None = None) -> MigrationTask:
        with self.database.connection() as connection:
            if subject_id is None:
                row = connection.execute(
                    "SELECT * FROM migration_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            else:
                validate_subject_id(subject_id)
                row = connection.execute(
                    "SELECT * FROM migration_tasks WHERE task_id=? AND subject_id=?",
                    (task_id, subject_id),
                ).fetchone()
        if row is None:
            raise NotFoundError(f"migration task not found: {task_id}")
        self._assert_task_integrity(row)
        return self._task(row)

    def list_tasks(self, subject_id: str, *, limit: int = 50) -> list[MigrationTask]:
        validate_subject_id(subject_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("migration task limit is invalid")
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM migration_tasks WHERE subject_id=? "
                "ORDER BY updated_at DESC,task_id DESC LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        for row in rows:
            self._assert_task_integrity(row)
        return [self._task(row) for row in rows]

    def cancel(self, task_id: str, *, actor: str, reason: str) -> MigrationTask:
        """Cancel a task before cutover, retaining an auditable reason."""
        self._validate_actor(actor)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise ValueError("migration cancellation reason is required")
        safe_reason = redact_secret_text(reason.strip())[:256]
        task = self.get_task(task_id)
        if task.status == "cancelled":
            return task
        if task.status in {"committed", "rolled_back", "failed"}:
            raise ValueError("migration task cannot be cancelled")
        if "cancelled" not in _TASK_TRANSITIONS.get(task.status, frozenset()):
            raise ValueError("migration task cannot be cancelled after transfer begins")
        return self.transition_task(
            task_id,
            "cancelled",
            actor=actor,
            error_code=safe_reason,
        )

    def _set_proposal_status(
        self, connection: Any, row: Any, status: str, actor: str, reason: str
    ) -> None:
        now = utc_now()
        values = dict(row)
        values.update({"status": status, "decided_at": now, "decision_reason": reason[:512]})
        result = connection.execute(
            "UPDATE migration_proposals SET status=?,decided_at=?,decision_reason=?,state_hash=? WHERE proposal_id=? AND status=?",
            (
                status,
                now,
                reason[:512],
                self._proposal_hash(values),
                row["proposal_id"],
                row["status"],
            ),
        )
        if result.rowcount != 1:
            raise ValueError("migration proposal status changed")

    @staticmethod
    def _ensure_target(connection: Any, subject_id: str, target_id: str) -> None:
        subject = connection.execute(
            "SELECT 1 FROM subject_identity WHERE subject_id=?", (subject_id,)
        ).fetchone()
        target = connection.execute(
            "SELECT * FROM migration_targets WHERE target_id=?",
            (target_id,),
        ).fetchone()
        if subject is None:
            raise NotFoundError(f"subject not found: {subject_id}")
        if target is None or target["subject_id"] != subject_id:
            raise NotFoundError(f"migration target not found: {target_id}")
        TargetRegistry._assert_row_integrity(target)
        if target["status"] != "active" or not target["attested_at"]:
            raise ValueError("migration target has not completed trust attestation")

    @staticmethod
    def _policy_revision(connection: Any, subject_id: str) -> int | None:
        row = connection.execute(
            "SELECT revision FROM migration_policies WHERE subject_id=?", (subject_id,)
        ).fetchone()
        return None if row is None else int(row["revision"])

    @staticmethod
    def _validate_actor(actor: str) -> None:
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
            raise ValueError("migration actor is invalid")

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("migration timestamp is invalid") from error
        if parsed.tzinfo is None:
            raise ValueError("migration timestamp must include timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _json(value: Any) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _proposal_hash(values: Any) -> str:
        return content_hash(
            {
                "proposal_id": values["proposal_id"],
                "subject_id": values["subject_id"],
                "target_id": values["target_id"],
                "policy_revision": int(values["policy_revision"]),
                "status": values["status"],
                "reason_code": values["reason_code"],
                "reason": values["reason"],
                "evidence_json": values["evidence_json"],
                "benefit_score": float(values["benefit_score"]),
                "risk_score": float(values["risk_score"]),
                "expires_at": values["expires_at"],
                "created_at": values["created_at"],
                "decided_at": values.get("decided_at"),
                "decision_reason": values.get("decision_reason"),
            }
        )

    @staticmethod
    def _task_hash(values: Any) -> str:
        return content_hash(
            {
                "task_id": values["task_id"],
                "proposal_id": values["proposal_id"],
                "subject_id": values["subject_id"],
                "target_id": values["target_id"],
                "idempotency_key": values["idempotency_key"],
                "source_epoch": values["source_epoch"],
                "policy_revision": int(values["policy_revision"]),
                "status": values["status"],
                "manifest_digest": values.get("manifest_digest"),
                "artifact_id": values.get("artifact_id"),
                "error_code": values.get("error_code"),
                "target_epoch_id": values.get("target_epoch_id"),
                "expires_at": values["expires_at"],
                "created_at": values["created_at"],
                "updated_at": values["updated_at"],
            }
        )

    @staticmethod
    def _proposal(row: Any) -> MigrationProposalRecord:
        return MigrationProposalRecord(
            row["proposal_id"],
            row["subject_id"],
            row["target_id"],
            int(row["policy_revision"]),
            row["status"],
            row["reason_code"],
            row["reason"],
            row["expires_at"],
        )

    @classmethod
    def _proposal_projection(cls, row: Any) -> dict[str, Any]:
        import json

        cls._assert_proposal_integrity(row)
        values = dict(row)
        try:
            evidence = json.loads(values["evidence_json"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("migration proposal evidence is invalid") from error
        if not isinstance(evidence, dict):
            raise ValueError("migration proposal evidence is invalid")
        return {
            key: values[key]
            for key in (
                "proposal_id",
                "subject_id",
                "target_id",
                "policy_revision",
                "status",
                "reason_code",
                "expires_at",
                "created_at",
                "decided_at",
            )
        } | {
            "reason": redact_secret_text(str(values["reason"])),
            "evidence": redact_secrets(evidence),
            "benefit_score": float(values["benefit_score"]),
            "risk_score": float(values["risk_score"]),
            "decision_reason": (
                None
                if values.get("decision_reason") is None
                else redact_secret_text(str(values["decision_reason"]))
            ),
        }

    @classmethod
    def _assert_proposal_integrity(cls, row: Any) -> None:
        values = dict(row)
        if values.get("state_hash") != cls._proposal_hash(values):
            raise ValueError("migration proposal integrity check failed")

    @classmethod
    def _assert_task_integrity(cls, row: Any) -> None:
        values = dict(row)
        if values.get("state_hash") == cls._task_hash(values):
            return
        if values.get("target_epoch_id") is None and values.get(
            "state_hash"
        ) == cls._legacy_task_hash(values):
            return
        else:
            raise ValueError("migration task integrity check failed")

    @staticmethod
    def _legacy_task_hash(values: Any) -> str:
        return content_hash(
            {
                "task_id": values["task_id"],
                "proposal_id": values["proposal_id"],
                "subject_id": values["subject_id"],
                "target_id": values["target_id"],
                "idempotency_key": values["idempotency_key"],
                "source_epoch": values["source_epoch"],
                "policy_revision": int(values["policy_revision"]),
                "status": values["status"],
                "manifest_digest": values.get("manifest_digest"),
                "artifact_id": values.get("artifact_id"),
                "error_code": values.get("error_code"),
                "expires_at": values["expires_at"],
                "created_at": values["created_at"],
                "updated_at": values["updated_at"],
            }
        )

    @staticmethod
    def _task(row: Any) -> MigrationTask:
        return MigrationTask(
            row["task_id"],
            row["proposal_id"],
            row["subject_id"],
            row["target_id"],
            row["idempotency_key"],
            row["source_epoch"],
            row["status"],
            int(row["policy_revision"]),
            row["expires_at"],
            row["manifest_digest"],
            row["artifact_id"],
            row["error_code"],
            row["target_epoch_id"],
            row["created_at"],
            row["updated_at"],
        )
