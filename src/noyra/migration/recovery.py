"""Emergency recovery with a pre-registered, proof-bearing standby target."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from noyra.core.database import Database
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

from .fencing import EpochLease
from .manager import MigrationManager
from .policy import MigrationPolicy, MigrationStore
from .targets import TargetRegistry

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{80,100}={0,2}\Z")


@dataclass(frozen=True)
class RecoveryRequest:
    task_id: str
    standby_target_id: str
    verified_backup_id: str
    source_failure_evidence: str
    manifest_digest: str = ""
    restore_report_digest: str = ""
    health_report_digest: str = ""
    target_signature: str = ""


class RecoveryCoordinator:
    """Create a recovery task only after backup, restore, and health proofs agree."""

    def __init__(self, database: Database | None = None):
        self.database = database

    @staticmethod
    def proof_payload(request: RecoveryRequest) -> dict[str, str]:
        return {
            "kind": "noyra-recovery-proof-v1",
            "task_id": request.task_id,
            "target_id": request.standby_target_id,
            "backup_id": request.verified_backup_id,
            "manifest_digest": request.manifest_digest,
            "restore_report_digest": request.restore_report_digest,
            "health_report_digest": request.health_report_digest,
            "source_failure_evidence_hash": content_hash(request.source_failure_evidence),
        }

    @classmethod
    def signing_bytes(cls, request: RecoveryRequest) -> bytes:
        return canonical_json(cls.proof_payload(request)).encode()

    def restore_standby(self, request: RecoveryRequest, policy: MigrationPolicy) -> dict[str, str]:
        if not policy.enabled or not policy.emergency_recovery_enabled:
            raise ValueError("emergency recovery is disabled")
        if policy.approval_mode != "emergency_recovery":
            raise ValueError("emergency recovery requires its dedicated approval mode")
        if request.standby_target_id not in policy.allowed_target_ids:
            raise ValueError("standby target is not allowlisted")
        self._validate_request(request)
        if self.database is None:
            return {
                "status": "recovery_verified",
                "task_id": request.task_id,
                "target_id": request.standby_target_id,
                "backup_id": request.verified_backup_id,
            }

        with self.database.transaction() as connection:
            target = connection.execute(
                "SELECT * FROM migration_targets WHERE target_id=? AND subject_id=?",
                (request.standby_target_id, policy.subject_id),
            ).fetchone()
            if target is None or target["status"] != "active" or not target["attested_at"]:
                raise ValueError("standby target is not attested")
            TargetRegistry._assert_row_integrity(target)
            self._verify_target_signature(target["public_key"], request)
            existing = connection.execute(
                "SELECT t.*, p.evidence_json FROM migration_tasks t "
                "JOIN migration_proposals p ON p.proposal_id=t.proposal_id "
                "WHERE t.task_id=?",
                (request.task_id,),
            ).fetchone()
            if existing is not None:
                MigrationManager._assert_task_integrity(existing)
                if not self._matches_existing_request(existing, request, policy.subject_id):
                    raise ValueError("recovery task identity is already used")
                if existing["target_epoch_id"]:
                    return self._task_result(existing)
                if existing["status"] != "validating":
                    return self._task_result(existing)
            active = connection.execute(
                "SELECT epoch_id FROM migration_epochs WHERE subject_id=? AND status='active'",
                (policy.subject_id,),
            ).fetchone()
            if active is not None:
                raise ValueError("subject already has an active migration epoch")
            if existing is None:
                now = utc_now()
                proposal_id = new_id("migrationproposal")
                evidence = {
                    "source_failure_evidence_hash": content_hash(request.source_failure_evidence),
                    "backup_id": request.verified_backup_id,
                    "manifest_digest": request.manifest_digest,
                    "restore_report_digest": request.restore_report_digest,
                    "health_report_digest": request.health_report_digest,
                }
                expires_at = "9999-12-31T23:59:59+00:00"
                proposal_values = {
                    "proposal_id": proposal_id,
                    "subject_id": policy.subject_id,
                    "target_id": request.standby_target_id,
                    "policy_revision": policy.revision,
                    "status": "approved",
                    "reason_code": "emergency_recovery",
                    "reason": "verified standby recovery",
                    "evidence_json": json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                    "benefit_score": 1.0,
                    "risk_score": 0.5,
                    "expires_at": expires_at,
                    "created_at": now,
                    "decided_at": now,
                    "decision_reason": "verified emergency recovery proof",
                }
                connection.execute(
                    "INSERT INTO migration_proposals("
                    "proposal_id,subject_id,target_id,policy_revision,status,reason_code,reason,"
                    "evidence_json,benefit_score,risk_score,expires_at,created_at,decided_at,"
                    "decision_reason,state_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*proposal_values.values(), MigrationManager._proposal_hash(proposal_values)),
                )
                source_epoch = f"recovery-{policy.revision}-{now.replace(':', '').replace('-', '')}"
                task_values = {
                    "task_id": request.task_id,
                    "proposal_id": proposal_id,
                    "subject_id": policy.subject_id,
                    "target_id": request.standby_target_id,
                    "idempotency_key": f"recovery:{request.task_id}",
                    "source_epoch": source_epoch,
                    "policy_revision": policy.revision,
                    "status": "validating",
                    "manifest_digest": request.manifest_digest,
                    "artifact_id": request.verified_backup_id,
                    "error_code": None,
                    "target_epoch_id": None,
                    "expires_at": expires_at,
                    "created_at": now,
                    "updated_at": now,
                }
                connection.execute(
                    "INSERT INTO migration_tasks("
                    "task_id,proposal_id,subject_id,target_id,idempotency_key,source_epoch,"
                    "policy_revision,status,manifest_digest,artifact_id,error_code,target_epoch_id,"
                    "expires_at,created_at,updated_at,state_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*task_values.values(), MigrationManager._task_hash(task_values)),
                )
                MigrationStore._append_audit(
                    connection,
                    policy.subject_id,
                    "migration_emergency_recovery_verified",
                    "operator",
                    {
                        "task_id": request.task_id,
                        "target_id": request.standby_target_id,
                        **evidence,
                    },
                )

        proof = {
            "task_id": request.task_id,
            "subject_id": policy.subject_id,
            "target_id": request.standby_target_id,
            "manifest_digest": request.manifest_digest,
            "restore_report_digest": request.restore_report_digest,
            "health_report_digest": request.health_report_digest,
            "target_signature": request.target_signature,
        }
        try:
            epoch = EpochLease.acquire(
                self.database,
                policy.subject_id,
                request.standby_target_id,
                expected_source_epoch=None,
                target_validation_proof=proof,
                actor="operator",
            )
        except Exception:
            self._mark_task_failed(request.task_id, "recovery_epoch_acquisition_failed")
            raise
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM migration_tasks WHERE task_id=?", (request.task_id,)
            ).fetchone()
            if row is None:
                raise ValueError("recovery task disappeared")
            values = dict(row)
            values["target_epoch_id"] = epoch.epoch_id
            values["updated_at"] = utc_now()
            result = connection.execute(
                "UPDATE migration_tasks SET target_epoch_id=?,updated_at=?,state_hash=? "
                "WHERE task_id=? AND status='validating'",
                (
                    epoch.epoch_id,
                    values["updated_at"],
                    MigrationManager._task_hash(values),
                    request.task_id,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("recovery task state changed")
            MigrationStore._append_audit(
                connection,
                policy.subject_id,
                "migration_emergency_recovery_epoch_acquired",
                "operator",
                {"task_id": request.task_id, "epoch_id": epoch.epoch_id},
            )
        return {
            "status": "recovery_ready",
            "task_id": request.task_id,
            "target_id": request.standby_target_id,
            "backup_id": request.verified_backup_id,
            "epoch_id": epoch.epoch_id,
        }

    @staticmethod
    def _validate_request(request: RecoveryRequest) -> None:
        if not _ID.fullmatch(request.task_id) or not _ID.fullmatch(request.standby_target_id):
            raise ValueError("recovery task or target id is invalid")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}\Z", request.verified_backup_id):
            raise ValueError("verified backup id is invalid")
        if not request.source_failure_evidence or len(request.source_failure_evidence) > 4096:
            raise ValueError("source failure evidence is required")
        if any(
            marker in request.source_failure_evidence.casefold()
            for marker in ("api_key", "private_key", "password", "bearer", "token")
        ):
            raise ValueError("source failure evidence contains secret material")
        for name in ("manifest_digest", "restore_report_digest", "health_report_digest"):
            if not _DIGEST.fullmatch(getattr(request, name)):
                raise ValueError(f"{name} is invalid")
        if not _SIGNATURE.fullmatch(request.target_signature):
            raise ValueError("target recovery signature is invalid")

    @classmethod
    def _verify_target_signature(cls, public_key: str, request: RecoveryRequest) -> None:
        try:
            key_bytes = base64.urlsafe_b64decode(public_key + "=" * (-len(public_key) % 4))
            signature = base64.urlsafe_b64decode(
                request.target_signature + "=" * (-len(request.target_signature) % 4)
            )
            if len(signature) != 64:
                raise ValueError("target recovery signature has invalid length")
            Ed25519PublicKey.from_public_bytes(key_bytes).verify(
                signature, cls.signing_bytes(request)
            )
        except (ValueError, TypeError, InvalidSignature, binascii.Error) as error:
            raise ValueError("target recovery signature verification failed") from error

    @staticmethod
    def _matches_existing_request(row: Any, request: RecoveryRequest, subject_id: str) -> bool:
        if (
            row["subject_id"] != subject_id
            or row["target_id"] != request.standby_target_id
            or row["manifest_digest"] != request.manifest_digest
            or row["artifact_id"] != request.verified_backup_id
        ):
            return False
        try:
            evidence = json.loads(row["evidence_json"])
        except (TypeError, ValueError):
            return False
        return evidence == {
            "source_failure_evidence_hash": content_hash(request.source_failure_evidence),
            "backup_id": request.verified_backup_id,
            "manifest_digest": request.manifest_digest,
            "restore_report_digest": request.restore_report_digest,
            "health_report_digest": request.health_report_digest,
        }

    def _mark_task_failed(self, task_id: str, error_code: str) -> None:
        if self.database is None:
            return
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM migration_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None or row["status"] != "validating":
                return
            values = dict(row)
            values["status"] = "failed"
            values["error_code"] = error_code
            values["updated_at"] = utc_now()
            connection.execute(
                "UPDATE migration_tasks SET status='failed',error_code=?,updated_at=?,state_hash=? "
                "WHERE task_id=? AND status='validating'",
                (
                    error_code,
                    values["updated_at"],
                    MigrationManager._task_hash(values),
                    task_id,
                ),
            )
            MigrationStore._append_audit(
                connection,
                row["subject_id"],
                "migration_emergency_recovery_failed",
                "operator",
                {"task_id": task_id, "error_code": error_code},
            )

    @staticmethod
    def _task_result(row: Any) -> dict[str, str]:
        if row["target_epoch_id"]:
            status = "recovery_ready"
        elif row["status"] == "failed":
            status = "recovery_failed"
        else:
            status = "recovery_verified"
        result = {
            "status": status,
            "task_id": row["task_id"],
            "target_id": row["target_id"],
            "backup_id": row["artifact_id"],
        }
        if row["target_epoch_id"]:
            result["epoch_id"] = row["target_epoch_id"]
        if row["error_code"]:
            result["error_code"] = row["error_code"]
        return result
