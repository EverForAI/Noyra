"""Execution boundary for migration side effects.

The database coordinator owns durable task/epoch transitions.  This module
defines the separate boundary that must perform the real source fence, target
restore/health checks, and target activation before the coordinator can mark a
task committed.  A missing executor is an intentional fail-closed state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .manager import MigrationTask


class MigrationExecutionError(RuntimeError):
    """Raised when a real migration side effect did not complete."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class MigrationExecutionReceipt:
    """Non-secret evidence returned by a completed execution boundary."""

    task_id: str
    subject_id: str
    target_id: str
    source_epoch: str
    manifest_digest: str
    artifact_id: str
    restore_report_digest: str
    health_report_digest: str
    source_fence_digest: str
    target_activation_digest: str

    def validate(self, task: MigrationTask, proof: Mapping[str, object]) -> None:
        expected = {
            "task_id": task.task_id,
            "subject_id": task.subject_id,
            "target_id": task.target_id,
            "source_epoch": task.source_epoch,
            "manifest_digest": proof.get("manifest_digest"),
            "artifact_id": proof.get("artifact_id"),
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise MigrationExecutionError(f"execution_receipt_{name}_mismatch")
        for name in (
            "restore_report_digest",
            "health_report_digest",
            "source_fence_digest",
            "target_activation_digest",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64:
                raise MigrationExecutionError(f"execution_receipt_{name}_invalid")


class MigrationExecutor(Protocol):
    """Perform irreversible migration work outside the database coordinator."""

    def execute(
        self,
        task: MigrationTask,
        *,
        proof: Mapping[str, object],
        source_epoch: str,
    ) -> MigrationExecutionReceipt:
        """Fence the source, restore/validate the target, and activate it."""

    def rollback(
        self,
        task: MigrationTask,
        *,
        receipt: MigrationExecutionReceipt,
        reason: str,
    ) -> None:
        """Undo completed external steps when durable commit cannot finish."""


def execution_receipt_from(value: Any) -> MigrationExecutionReceipt:
    """Convert an executor result without accepting arbitrary mappings."""

    if isinstance(value, MigrationExecutionReceipt):
        return value
    raise MigrationExecutionError("execution_receipt_invalid")


__all__ = [
    "MigrationExecutionError",
    "MigrationExecutionReceipt",
    "MigrationExecutor",
    "execution_receipt_from",
]
