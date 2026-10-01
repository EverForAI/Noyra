"""Emergency recovery boundary for pre-registered standby targets."""

from __future__ import annotations

import re
from dataclasses import dataclass

from noyra.core.database import Database
from noyra.core.types import content_hash

from .policy import MigrationPolicy


@dataclass(frozen=True)
class RecoveryRequest:
    task_id: str
    standby_target_id: str
    verified_backup_id: str
    source_failure_evidence: str


class RecoveryCoordinator:
    def __init__(self, database: Database | None = None):
        self.database = database

    def restore_standby(self, request: RecoveryRequest, policy: MigrationPolicy) -> dict[str, str]:
        if not policy.enabled or not policy.emergency_recovery_enabled:
            raise ValueError("emergency recovery is disabled")
        if policy.approval_mode != "emergency_recovery":
            raise ValueError("emergency recovery requires its dedicated approval mode")
        if request.standby_target_id not in policy.allowed_target_ids:
            raise ValueError("standby target is not allowlisted")
        if not request.verified_backup_id or not request.source_failure_evidence:
            raise ValueError("verified backup and source failure evidence are required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}", request.verified_backup_id):
            raise ValueError("verified backup id is invalid")
        if len(request.source_failure_evidence) > 4096 or any(
            marker in request.source_failure_evidence.casefold()
            for marker in ("api_key", "private_key", "password", "bearer", "token")
        ):
            raise ValueError("source failure evidence contains secret material")
        if self.database is not None:
            with self.database.connection() as connection:
                target = connection.execute(
                    "SELECT status,attested_at FROM migration_targets "
                    "WHERE target_id=? AND subject_id=?",
                    (request.standby_target_id, policy.subject_id),
                ).fetchone()
            if target is None or target["status"] != "active" or not target["attested_at"]:
                raise ValueError("standby target is not attested")
            with self.database.transaction() as connection:
                from .policy import MigrationStore

                MigrationStore._append_audit(
                    connection,
                    policy.subject_id,
                    "migration_emergency_recovery_queued",
                    "operator",
                    {
                        "task_id": request.task_id,
                        "target_id": request.standby_target_id,
                        "backup_id": request.verified_backup_id,
                        "evidence_hash": content_hash(request.source_failure_evidence),
                    },
                )
        return {
            "status": "recovery_queued",
            "task_id": request.task_id,
            "target_id": request.standby_target_id,
            "backup_id": request.verified_backup_id,
        }
