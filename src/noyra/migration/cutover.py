"""Explicit preparation, commit, and rollback controls for a migration task."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from noyra.core.admission import RuntimeAdmissionGate
from noyra.core.database import Database
from noyra.core.types import canonical_json, content_hash, utc_now

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
    def __init__(self, database: Database, *, admission: RuntimeAdmissionGate | None = None):
        self.database = database
        self.manager = MigrationManager(database, MigrationStore(database))
        self.admission = admission

    def prepare(
        self,
        task_id: str,
        *,
        actor: str = "operator",
        proof: dict[str, object] | None = None,
    ) -> CutoverPlan:
        task = self.manager.get_task(task_id)
        if proof is None:
            raise ValueError("verified target restore and health proof is required")
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
        epoch = EpochLease.acquire(
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
                    "restore_report_digest": verified["restore_report_digest"],
                    "health_report_digest": verified["health_report_digest"],
                    "epoch_id": epoch.epoch_id,
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
        if task.status not in {"validating", "cutover"}:
            raise ValueError("verified target restore and health proof is required before commit")
        epoch = self._epoch_for_task(task_id)
        if epoch is None:
            raise ValueError("migration target epoch is missing")
        epoch.assert_current()
        admission = self.admission
        lease = admission.begin("migration-cutover") if admission is not None else None
        try:
            if task.status == "validating":
                task = self.manager.transition_task(task_id, "cutover", actor=actor)
            with self.database.transaction() as connection:
                epoch.assert_current_in_transaction(connection)
                task = self.manager.transition_task_in_transaction(
                    connection,
                    task_id,
                    "committed",
                    actor=actor,
                    expected_status="cutover",
                )
                epoch.complete_in_transaction(connection, actor)
            return {"task_id": task_id, "status": task.status}
        finally:
            if lease is not None and admission is not None:
                admission.finish(lease)

    def rollback(self, task_id: str, reason: str, *, actor: str = "operator") -> dict[str, str]:
        if not reason.strip():
            raise ValueError("rollback reason is required")
        task = self.manager.get_task(task_id)
        if task.status == "rolled_back":
            return {"task_id": task_id, "status": "rolled_back"}
        if task.status == "committed":
            raise ValueError("migration task cannot be rolled back")
        epoch = self._epoch_for_task(task_id)
        if task.status == "rolling_back" and epoch is None:
            self.manager.transition_task(
                task_id, "rolled_back", actor=actor, error_code=reason.strip()[:256]
            )
            return {"task_id": task_id, "status": "rolled_back"}
        if task.status != "rolling_back":
            self.manager.transition_task(
                task_id, "rolling_back", actor=actor, error_code=reason.strip()[:256]
            )
        if epoch is not None:
            epoch.revoke(reason.strip()[:256], actor)
        self.manager.transition_task(
            task_id, "rolled_back", actor=actor, error_code=reason.strip()[:256]
        )
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
        if set(proof) != required:
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
