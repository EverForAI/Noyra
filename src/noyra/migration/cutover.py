"""Explicit preparation, commit, and rollback controls for a migration task."""

# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass

from noyra.core.database import Database
from noyra.core.types import utc_now


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

    def prepare(self, task_id: str) -> CutoverPlan:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT task_id,source_epoch,target_id,status FROM migration_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError("migration task not found")
            if row["status"] not in {"approved", "preparing"}:
                raise ValueError("migration task is not ready for cutover")
            connection.execute(
                "UPDATE migration_tasks SET status='preparing',updated_at=? WHERE task_id=?",
                (utc_now(), task_id),
            )
            return CutoverPlan(task_id, row["source_epoch"], row["target_id"], "preparing", utc_now())

    def commit(self, task_id: str) -> dict[str, str]:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM migration_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError("migration task not found")
            if row["status"] not in {"preparing", "validating", "cutover"}:
                raise ValueError("migration task is not in cutover state")
            connection.execute(
                "UPDATE migration_tasks SET status='committed',updated_at=? WHERE task_id=?",
                (utc_now(), task_id),
            )
        return {"task_id": task_id, "status": "committed"}

    def rollback(self, task_id: str, reason: str) -> dict[str, str]:
        if not reason.strip():
            raise ValueError("rollback reason is required")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM migration_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError("migration task not found")
            if row["status"] in {"committed", "rolled_back"}:
                raise ValueError("migration task cannot be rolled back")
            connection.execute(
                "UPDATE migration_tasks SET status='rolled_back',error_code=?,updated_at=? WHERE task_id=?",
                (reason.strip()[:256], utc_now(), task_id),
            )
        return {"task_id": task_id, "status": "rolled_back"}
