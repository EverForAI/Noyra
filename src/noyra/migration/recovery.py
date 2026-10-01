"""Emergency recovery boundary for pre-registered standby targets."""

from __future__ import annotations

from dataclasses import dataclass

from .policy import MigrationPolicy


@dataclass(frozen=True)
class RecoveryRequest:
    task_id: str
    standby_target_id: str
    verified_backup_id: str
    source_failure_evidence: str


class RecoveryCoordinator:
    def restore_standby(self, request: RecoveryRequest, policy: MigrationPolicy) -> dict[str, str]:
        if not policy.enabled or not policy.emergency_recovery_enabled:
            raise ValueError("emergency recovery is disabled")
        if policy.approval_mode != "emergency_recovery":
            raise ValueError("emergency recovery requires its dedicated approval mode")
        if request.standby_target_id not in policy.allowed_target_ids:
            raise ValueError("standby target is not allowlisted")
        if not request.verified_backup_id or not request.source_failure_evidence:
            raise ValueError("verified backup and source failure evidence are required")
        return {
            "status": "recovery_queued",
            "task_id": request.task_id,
            "target_id": request.standby_target_id,
            "backup_id": request.verified_backup_id,
        }
