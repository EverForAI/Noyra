"""Explicit preparation, commit, and rollback controls for a migration task."""

from __future__ import annotations

from dataclasses import dataclass

from noyra.core.admission import RuntimeAdmissionGate
from noyra.core.database import Database

from .fencing import EpochLease
from .manager import MigrationManager
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

    def prepare(self, task_id: str, *, actor: str = "operator") -> CutoverPlan:
        self.manager.get_task(task_id)
        raise ValueError("verified target restore and health proof is required")

    def commit(self, task_id: str, *, actor: str = "operator") -> dict[str, str]:
        self.manager.get_task(task_id)
        raise ValueError("verified target restore and health proof is required")

    def rollback(self, task_id: str, reason: str, *, actor: str = "operator") -> dict[str, str]:
        if not reason.strip():
            raise ValueError("rollback reason is required")
        task = self.manager.get_task(task_id)
        if task.status in {"committed", "rolled_back"}:
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
