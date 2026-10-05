"""Explicit preparation, commit, and rollback controls for a migration task."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from noyra.core.admission import RuntimeAdmissionGate
from noyra.core.database import Database
from noyra.core.types import canonical_json, content_hash, utc_now

from .executor import (
    MigrationExecutionError,
    MigrationExecutionReceipt,
    MigrationExecutor,
    execution_receipt_from,
)
from .fencing import EpochLease
from .manager import MigrationManager, MigrationTask
from .policy import MigrationStore


@dataclass(frozen=True)
class CutoverPlan:
    task_id: str
    source_epoch: str
    target_id: str
    target_epoch_id: str | None
    status: str
    prepared_at: str


class CutoverCoordinator:
    def __init__(
        self,
        database: Database,
        *,
        admission: RuntimeAdmissionGate | None = None,
        executor: MigrationExecutor | None = None,
        production: bool | None = None,
    ):
        self.database = database
        self.manager = MigrationManager(database, MigrationStore(database, production=production))
        self.admission = admission
        self.executor = executor

    def run(
        self, task_id: str, *, binding: dict[str, object], actor: str = "operator"
    ) -> dict[str, str]:
        """Execute an approved task using one fenced snapshot and real target proofs."""
        from .http_executor import HTTPMigrationExecutor
        from .policy import MigrationPolicy
        from .runtime import payment_in_flight

        if not isinstance(self.executor, HTTPMigrationExecutor):
            raise ValueError("managed migration executor unavailable")
        task = self.manager.get_task(task_id)
        if task.status == "committed":
            return {"task_id": task_id, "status": "committed"}
        policy = self.manager.policy_store.read_policy(task.subject_id)
        self.manager.policy_store.assert_automation_allowed(policy)
        if not policy.enabled or policy.revision != task.policy_revision:
            raise ValueError("migration policy is disabled or stale")
        if task.status != "approved":
            raise ValueError("migration task requires rollback or reconciliation")
        if self.manager._parse_timestamp(task.expires_at) <= datetime.now(UTC):
            raise ValueError("migration task is expired")
        if policy.allowed_target_ids and task.target_id not in policy.allowed_target_ids:
            raise ValueError("migration target is not allowed")
        wallet = binding.get("wallet_binding")
        if not isinstance(wallet, dict) or wallet.get("mode") != policy.wallet_mode:
            raise ValueError("wallet migration policy binding mismatch")
        minute = datetime.now(UTC).hour * 60 + datetime.now(UTC).minute
        if (
            minute - policy.maintenance_window_start_minute
        ) % 1440 >= policy.maintenance_window_duration_minutes:
            raise ValueError("migration maintenance window is closed")
        admission = self.admission
        with admission.migration_control_scope() if admission is not None else nullcontext():
            fence_epoch = admission.fence_for_migration() if admission is not None else None
            try:
                with self.database.transaction() as connection:
                    current_policy = MigrationPolicy.from_record(
                        connection.execute(
                            "SELECT * FROM migration_policies WHERE subject_id=?",
                            (task.subject_id,),
                        ).fetchone()
                    )
                    if current_policy != policy:
                        raise ValueError("migration policy changed before fencing")
                    if payment_in_flight(connection, task.subject_id):
                        raise ValueError("migration blocked by unresolved wallet payments")
                    self.manager._ensure_target(connection, task.subject_id, task.target_id)
                    if self.manager.cooldown_until(connection, task.subject_id, task.target_id):
                        raise ValueError("migration rejection cooldown is active")
                    epoch = EpochLease._acquire_in_transaction(
                        self.database,
                        connection,
                        task.subject_id,
                        task.target_id,
                        expected_source_epoch=task.source_epoch,
                        actor=actor,
                    )
                    task = self.manager.transition_task_in_transaction(
                        connection,
                        task_id,
                        "preparing",
                        expected_status="approved",
                        actor=actor,
                        target_epoch_id=epoch.epoch_id,
                        artifact_id=f"migration-{task.task_id}",
                    )
                    MigrationStore._append_audit(
                        connection,
                        task.subject_id,
                        "migration_execution_started",
                        actor,
                        {"task_id": task_id, "epoch_id": epoch.epoch_id},
                    )
            except Exception:
                # No remote side effect is possible before this reservation commits.
                if admission is not None and fence_epoch is not None:
                    admission.clear_migration_fence(fence_epoch)
                raise
            proof = {**binding, "artifact_id": task.artifact_id}

            def artifact_ready(manifest: Mapping[str, Any]) -> None:
                self.manager.transition_task(
                    task_id,
                    "transferring",
                    actor=actor,
                    expected_status="preparing",
                    manifest_digest=content_hash(manifest),
                    artifact_id=str(manifest["artifact_id"]),
                )

            def target_verified(verified: dict[str, object]) -> None:
                self.prepare(task_id, actor=actor, proof=verified)

            receipt = self.executor.execute(
                task,
                proof=proof,
                source_epoch=task.source_epoch,
                artifact_ready=artifact_ready,
                target_verified=target_verified,
            )
            current = self.manager.get_task(task_id)
            return self._commit_receipt(current, epoch, receipt, proof, actor)

    def prepare(
        self,
        task_id: str,
        *,
        actor: str = "operator",
        proof: dict[str, object] | None = None,
    ) -> CutoverPlan:
        task = self.manager.get_task(task_id)
        self.manager.policy_store.assert_automation_allowed(
            self.manager.policy_store.read_policy(task.subject_id)
        )
        if proof is None:
            raise ValueError("verified target restore and health proof is required")
        if self.executor is None:
            raise ValueError("migration executor unavailable")
        verified = self._verify_target_proof(task, proof)
        if task.status == "approved":
            task = self.manager.transition_task(
                task_id,
                "preparing",
                actor=actor,
                manifest_digest=verified["manifest_digest"],
                artifact_id=verified["artifact_id"],
            )
        elif task.status not in {"preparing", "transferring", "restoring", "validating"}:
            raise ValueError("migration task is not ready for target validation")
        elif task.manifest_digest != verified["manifest_digest"]:
            raise ValueError("verified target proof manifest binding is invalid")
        epoch = self._epoch_for_task(task_id) or EpochLease.acquire(
            self.database,
            task.subject_id,
            task.target_id,
            expected_source_epoch=task.source_epoch,
            target_validation_proof=verified,
            actor=actor,
        )
        if task.status != "validating":
            task = self.manager.transition_task(
                task_id, "validating", actor=actor, target_epoch_id=epoch.epoch_id
            )
        else:
            task = self.manager.get_task(task_id)
        with self.database.transaction() as connection:
            MigrationStore._append_audit(
                connection,
                task.subject_id,
                "migration_target_proof_verified",
                actor,
                {
                    "task_id": task_id,
                    "target_id": task.target_id,
                    "manifest_digest": verified["manifest_digest"],
                    "artifact_id": verified["artifact_id"],
                    "restore_report": proof["restore_report"],
                    "health_report": proof["health_report"],
                    "target_signature": verified["target_signature"],
                    "restore_report_digest": verified["restore_report_digest"],
                    "health_report_digest": verified["health_report_digest"],
                    "epoch_id": epoch.epoch_id,
                    **({"manifest": proof["manifest"]} if "manifest" in proof else {}),
                    **(
                        {"credential_binding": proof["credential_binding"]}
                        if "credential_binding" in proof
                        else {}
                    ),
                    **(
                        {"wallet_binding": proof["wallet_binding"]}
                        if "wallet_binding" in proof
                        else {}
                    ),
                    **(
                        {"recipient_key_fingerprint": proof["recipient_key_fingerprint"]}
                        if "recipient_key_fingerprint" in proof
                        else {}
                    ),
                    **(
                        {"target_volume_proof": proof["target_volume_proof"]}
                        if "target_volume_proof" in proof
                        else {}
                    ),
                },
            )
        return CutoverPlan(
            task_id,
            task.source_epoch,
            task.target_id,
            epoch.epoch_id,
            task.status,
            utc_now(),
        )

    def commit(self, task_id: str, *, actor: str = "operator") -> dict[str, str]:
        task = self.manager.get_task(task_id)
        if task.status == "committed":
            return {"task_id": task_id, "status": "committed"}
        self.manager.policy_store.assert_automation_allowed(
            self.manager.policy_store.read_policy(task.subject_id)
        )
        if task.status not in {"validating", "cutover"}:
            raise ValueError("verified target restore and health proof is required before commit")
        if self.executor is None:
            raise ValueError("migration executor unavailable")
        epoch = self._epoch_for_task(task_id)
        if epoch is None:
            raise ValueError("migration target epoch is missing")
        admission = self.admission
        control = admission.migration_control_scope() if admission is not None else nullcontext()
        receipt: MigrationExecutionReceipt | None = None
        with control:
            try:
                epoch.assert_current()
                if admission is not None:
                    # The durable epoch is already active at this point.  Fence
                    # the in-memory gate as well, invalidate existing normal
                    # leases, and wait for them to drain before any side effect.
                    admission.fence_for_migration()
                proof = self._proof_for_task(task_id)
                with self.database.transaction() as connection:
                    MigrationStore._append_audit(
                        connection,
                        task.subject_id,
                        "migration_execution_started",
                        actor,
                        {
                            "task_id": task.task_id,
                            "target_id": task.target_id,
                            "source_epoch": task.source_epoch,
                            "manifest_digest": task.manifest_digest,
                        },
                    )
                receipt = execution_receipt_from(
                    self.executor.execute(
                        task,
                        proof=proof,
                        source_epoch=task.source_epoch,
                    )
                )
                return self._commit_receipt(task, epoch, receipt, proof, actor)
            except Exception:
                # A lost reply is indistinguishable from successful activation.
                # Leave the durable epoch and admission fence in place until an
                # explicit rollback obtains persistent target cancellation.
                raise

    def _commit_receipt(
        self,
        task: MigrationTask,
        epoch: EpochLease,
        receipt: MigrationExecutionReceipt,
        proof: dict[str, object],
        actor: str,
    ) -> dict[str, str]:
        task_id = task.task_id
        receipt.validate(task, proof)
        with self.database.transaction() as connection:
            if task.status == "validating":
                task = self.manager.transition_task_in_transaction(
                    connection,
                    task_id,
                    "cutover",
                    actor=actor,
                    expected_status="validating",
                )
            epoch.assert_current_in_transaction(connection)
            task = self.manager.transition_task_in_transaction(
                connection,
                task_id,
                "committed",
                actor=actor,
                expected_status="cutover",
            )
            MigrationStore._append_audit(
                connection,
                task.subject_id,
                "migration_execution_completed",
                actor,
                {
                    "task_id": receipt.task_id,
                    "target_id": receipt.target_id,
                    "source_epoch": receipt.source_epoch,
                    "manifest_digest": receipt.manifest_digest,
                    "artifact_id": receipt.artifact_id,
                    "restore_report_digest": receipt.restore_report_digest,
                    "health_report_digest": receipt.health_report_digest,
                    "source_fence_digest": receipt.source_fence_digest,
                    "target_activation_digest": receipt.target_activation_digest,
                    "recipient_key_fingerprint": receipt.recipient_key_fingerprint,
                    "target_volume_proof_digest": receipt.target_volume_proof_digest,
                    "credential_binding_digest": receipt.credential_binding_digest,
                    "signer_binding_digest": receipt.signer_binding_digest,
                    "wallet_mode": receipt.wallet_mode,
                    "wallet_proof_digest": receipt.wallet_proof_digest,
                },
            )
            epoch.complete_in_transaction(connection, actor)
        return {"task_id": task_id, "status": task.status}

    def _proof_for_task(self, task_id: str) -> dict[str, object]:
        """Load the proof digests recorded when the task entered validation."""
        task = self.manager.get_task(task_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM migration_audit_events "
                "WHERE subject_id=? AND action='migration_target_proof_verified' "
                "ORDER BY rowid DESC LIMIT 32",
                (task.subject_id,),
            ).fetchall()
        import json

        for row in rows:
            try:
                values = json.loads(row["payload_json"])
            except (TypeError, ValueError, KeyError):
                continue
            if isinstance(values, dict) and values.get("task_id") == task_id:
                return values
        raise ValueError("migration execution proof is missing")

    def rollback(self, task_id: str, reason: str, *, actor: str = "operator") -> dict[str, str]:
        if not reason.strip():
            raise ValueError("rollback reason is required")
        admission = self.admission
        control = admission.migration_control_scope() if admission is not None else nullcontext()
        with control:
            task = self.manager.get_task(task_id)
            if task.status == "rolled_back":
                # An old task must never clear a newer migration's fence.
                return {"task_id": task_id, "status": "rolled_back"}
            if task.status == "committed":
                raise ValueError("migration task cannot be rolled back")
            epoch = self._epoch_for_task(task_id)
            if task.status != "rolling_back":
                self.manager.transition_task(
                    task_id, "rolling_back", actor=actor, error_code=reason.strip()[:256]
                )
            if epoch is not None:
                epoch.assert_current()
            if task.manifest_digest is not None:
                if self.executor is None:
                    raise MigrationExecutionError("target_deactivation_unavailable")
                cancellation = self.executor.rollback(task, reason=reason)
                expected = {
                    "task_id": task.task_id,
                    "target_id": task.target_id,
                    "source_epoch": task.source_epoch,
                    "manifest_digest": task.manifest_digest,
                    "status": "deactivated",
                    "activation_revoked": True,
                }
                if not isinstance(cancellation, dict) or any(
                    cancellation.get(key) != value for key, value in expected.items()
                ):
                    raise MigrationExecutionError("target_deactivation_proof_invalid")
                with self.database.transaction() as connection:
                    MigrationStore._append_audit(
                        connection,
                        task.subject_id,
                        "migration_target_deactivated",
                        actor,
                        dict(cancellation),
                    )
            # Epoch revocation and terminal task status are one durable change.
            # An audit/transition failure leaves the source fenced on restart.
            with self.database.transaction() as connection:
                if epoch is not None:
                    epoch.revoke_in_transaction(connection, reason.strip()[:256], actor)
                self.manager.transition_task_in_transaction(
                    connection,
                    task_id,
                    "rolled_back",
                    actor=actor,
                    error_code=reason.strip()[:256],
                    expected_status="rolling_back",
                )
            if epoch is not None and admission is not None and admission.migration_fenced:
                fence_epoch = admission.migration_fence_epoch
                if fence_epoch is not None:
                    admission.clear_migration_fence(fence_epoch)
            return {"task_id": task_id, "status": "rolled_back"}

    def _source_epoch(self, task_id: str) -> str:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT source_epoch FROM migration_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise ValueError("migration task not found")
        return str(row["source_epoch"])

    def _verify_target_proof(self, task: MigrationTask, proof: dict[str, object]) -> dict[str, str]:
        required = {
            "manifest_digest",
            "artifact_id",
            "restore_report",
            "health_report",
            "target_signature",
        }
        optional = {
            "manifest",
            "credential_binding",
            "wallet_binding",
            "recipient_key_fingerprint",
            "target_volume_proof",
        }
        if not required.issubset(proof) or set(proof) - required - optional:
            raise ValueError("verified target restore and health proof is invalid")
        manifest = proof["manifest_digest"]
        artifact = proof["artifact_id"]
        signature = proof["target_signature"]
        if (
            not isinstance(manifest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest)
            or not isinstance(artifact, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", artifact)
            or not isinstance(signature, str)
        ):
            raise ValueError("verified target restore and health proof is invalid")
        restore = proof["restore_report"]
        health = proof["health_report"]
        if not isinstance(restore, dict) or not isinstance(health, dict):
            raise ValueError("verified target restore and health proof is invalid")
        manifest_proof = proof.get("manifest")
        if manifest_proof is not None:
            if not isinstance(manifest_proof, dict) or content_hash(manifest_proof) != manifest:
                raise ValueError("verified migration artifact manifest is invalid")
            if manifest_proof.get("artifact_id") != artifact:
                raise ValueError("verified migration artifact manifest binding is invalid")
        for key in ("credential_binding", "wallet_binding"):
            value = proof.get(key)
            if value is not None and not isinstance(value, dict):
                raise ValueError("verified migration binding request is invalid")
            if isinstance(value, dict) and any(
                isinstance(name, str)
                and any(
                    token in name.casefold()
                    for token in ("secret", "token", "password", "private", "api_key")
                )
                for name in value
            ):
                raise ValueError("verified migration binding request contains a secret")
        for report, status in ((restore, "restored"), (health, "healthy")):
            if (
                report.get("target_id") != task.target_id
                or report.get("artifact_id") != artifact
                or report.get("manifest_digest") != manifest
                or report.get("status") != status
            ):
                raise ValueError("verified target restore and health proof is invalid")
        checks = health.get("checks")
        if (
            not isinstance(checks, dict)
            or not checks
            or not all(value is True for value in checks.values())
        ):
            raise ValueError("target health proof is not healthy")
        with self.database.connection() as connection:
            target = connection.execute(
                "SELECT * FROM migration_targets WHERE target_id=? AND subject_id=?",
                (task.target_id, task.subject_id),
            ).fetchone()
        if target is None or target["status"] != "active":
            raise ValueError("migration target is not active")
        if target["attestation_epoch"] != task.source_epoch:
            raise ValueError("migration target attestation is stale")
        restore_digest = content_hash(restore)
        health_digest = content_hash(health)
        signing_payload = {
            "task_id": task.task_id,
            "subject_id": task.subject_id,
            "target_id": task.target_id,
            "source_epoch": task.source_epoch,
            "manifest_digest": manifest,
            "artifact_id": artifact,
            "restore_report_digest": restore_digest,
            "health_report_digest": health_digest,
        }
        try:
            public_key = base64.urlsafe_b64decode(
                str(target["public_key"]) + "=" * (-len(str(target["public_key"])) % 4)
            )
            raw_signature = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                raw_signature, canonical_json(signing_payload).encode()
            )
        except (ValueError, TypeError, InvalidSignature, binascii.Error) as error:
            raise ValueError("verified target proof signature is invalid") from error
        return {
            "task_id": task.task_id,
            "subject_id": task.subject_id,
            "target_id": task.target_id,
            "manifest_digest": manifest,
            "artifact_id": artifact,
            "restore_report_digest": restore_digest,
            "health_report_digest": health_digest,
            "target_signature": signature,
        }

    def _epoch_for_task(self, task_id: str) -> EpochLease | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT e.epoch_id,e.subject_id,e.target_id,e.epoch_number FROM migration_epochs e "
                "JOIN migration_tasks t ON t.target_epoch_id=e.epoch_id "
                "WHERE t.task_id=? AND e.status='active'",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return EpochLease(
            self.database,
            row["subject_id"],
            row["target_id"],
            row["epoch_id"],
            int(row["epoch_number"]),
        )
