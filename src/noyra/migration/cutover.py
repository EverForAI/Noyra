"""Explicit preparation, commit, and rollback controls for a migration task."""

# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass

from noyra.core.database import Database
from noyra.core.types import utc_now

from .manager import MigrationManager
from .policy import MigrationStore


@dataclass(frozen=True)
class CutoverPlan:
    task_id: str
    source_epoch: str
    target_id: str
    status: str
    prepared_at: str


class CutoverCoordinator:
    def __init__(self, database: Database):
        self.database = database
        self.manager = MigrationManager(database, MigrationStore(database))

    def prepare(self, task_id: str, *, actor: str = "operator") -> CutoverPlan:
        task = self.manager.get_task(task_id)
        if task.status == "approved":
            task = self.manager.transition_task(task_id, "preparing", actor=actor)
        elif task.status != "preparing":
            raise ValueError("migration task is not ready for cutover")
        return CutoverPlan(task.task_id, self._source_epoch(task_id), task.target_id, task.status, utc_now())

    def commit(self, task_id: str, *, actor: str = "operator") -> dict[str, str]:
        task = self.manager.get_task(task_id)
        if task.status == "committed":
            return {"task_id": task_id, "status": "committed"}
        if task.status not in {"preparing", "validating", "cutover"}:
            raise ValueError("migration task is not in cutover state")
        self.manager.transition_task(task_id, "committed", actor=actor)
        return {"task_id": task_id, "status": "committed"}

    def rollback(self, task_id: str, reason: str, *, actor: str = "operator") -> dict[str, str]:
        if not reason.strip():
            raise ValueError("rollback reason is required")
        task = self.manager.get_task(task_id)
        if task.status in {"committed", "rolled_back"}:
            raise ValueError("migration task cannot be rolled back")
        if task.status != "rolling_back":
            self.manager.transition_task(
                task_id, "rolling_back", actor=actor, error_code=reason.strip()[:256]
            )
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
