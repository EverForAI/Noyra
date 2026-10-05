"""Execution boundary for migration side effects.

The database coordinator owns durable task/epoch transitions.  This module
defines the separate boundary that must perform the real source fence, target
restore/health checks, and target activation before the coordinator can mark a
task committed.  A missing executor is an intentional fail-closed state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from noyra.core.types import content_hash

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
    recipient_key_fingerprint: str | None = None
    target_volume_proof_digest: str | None = None
    credential_binding_digest: str | None = None
    signer_binding_digest: str | None = None
    wallet_mode: str | None = None
    wallet_proof_digest: str | None = None

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
        optional_digests = (
            "recipient_key_fingerprint",
            "target_volume_proof_digest",
            "credential_binding_digest",
            "signer_binding_digest",
            "wallet_proof_digest",
        )
        for name in optional_digests:
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            ):
                raise MigrationExecutionError(f"execution_receipt_{name}_invalid")
        if self.wallet_mode is not None and self.wallet_mode not in {
            "external_signer_rebind",
            "local_wallet_transfer",
            "disabled",
        }:
            raise MigrationExecutionError("execution_receipt_wallet_mode_invalid")
        if not isinstance(self.recipient_key_fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.recipient_key_fingerprint
        ):
            raise MigrationExecutionError("execution_receipt_recipient_key_fingerprint_required")
        if proof.get("recipient_key_fingerprint") != self.recipient_key_fingerprint:
            raise MigrationExecutionError("execution_receipt_recipient_key_fingerprint_mismatch")
        required_bindings = {
            "target_volume_proof_digest": proof.get("target_volume_proof"),
            "credential_binding_digest": proof.get("credential_binding"),
        }
        for name, binding in required_bindings.items():
            if not isinstance(binding, Mapping):
                raise MigrationExecutionError(f"execution_receipt_{name}_required")
        if self.wallet_mode is None:
            raise MigrationExecutionError("execution_receipt_wallet_mode_required")
        volume = required_bindings["target_volume_proof_digest"]
        credential = required_bindings["credential_binding_digest"]
        assert isinstance(volume, Mapping)
        assert isinstance(credential, Mapping)
        expected_volume = content_hash(
            {
                "task_id": task.task_id,
                "manifest_digest": self.manifest_digest,
                "proof": dict(volume),
            }
        )
        expected_credential = content_hash(
            {
                "task_id": task.task_id,
                "manifest_digest": self.manifest_digest,
                "binding": dict(credential),
            }
        )
        if self.target_volume_proof_digest != expected_volume:
            raise MigrationExecutionError("execution_receipt_target_volume_proof_mismatch")
        if self.credential_binding_digest != expected_credential:
            raise MigrationExecutionError("execution_receipt_credential_binding_mismatch")
        wallet = proof.get("wallet_binding")
        if not isinstance(wallet, Mapping):
            raise MigrationExecutionError("execution_receipt_wallet_binding_required")
        if wallet.get("mode") != self.wallet_mode:
            raise MigrationExecutionError("execution_receipt_wallet_mode_mismatch")
        expected_wallet = (
            content_hash(
                {
                    "task_id": task.task_id,
                    "manifest_digest": self.manifest_digest,
                    "binding": dict(wallet),
                }
            )
            if self.wallet_mode != "disabled"
            else None
        )
        if self.wallet_proof_digest != expected_wallet:
            raise MigrationExecutionError("execution_receipt_wallet_proof_mismatch")
        expected_signer = expected_wallet if self.wallet_mode == "external_signer_rebind" else None
        if self.signer_binding_digest != expected_signer:
            raise MigrationExecutionError("execution_receipt_signer_binding_mismatch")


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
        receipt: MigrationExecutionReceipt | None = None,
        reason: str,
    ) -> Mapping[str, Any]:
        """Persistently cancel target activation, even when its reply was lost.

        Return verified task/epoch/manifest-bound deactivation evidence. Never
        restore source admission here; only the durable coordinator may do so.
        """


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
