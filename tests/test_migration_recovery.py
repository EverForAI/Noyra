from __future__ import annotations

import pytest

from noyra.migration.policy import MigrationPolicy
from noyra.migration.recovery import RecoveryCoordinator, RecoveryRequest


def test_recovery_requires_dedicated_mode_and_standby_allowlist() -> None:
    policy = MigrationPolicy.default("Noyra-0001").with_updates(
        enabled=True, approval_mode="emergency_recovery", emergency_recovery_enabled=True,
        allowed_target_ids=["standby-1"],
    )
    result = RecoveryCoordinator().restore_standby(
        RecoveryRequest("task-1", "standby-1", "backup-1", "source-down"), policy
    )
    assert result["status"] == "recovery_queued"
    with pytest.raises(ValueError, match="allowlisted"):
        RecoveryCoordinator().restore_standby(
            RecoveryRequest("task-1", "other", "backup-1", "source-down"), policy
        )
