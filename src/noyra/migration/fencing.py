"""Single-active epoch fencing for migration cutover."""

# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass

from noyra.core.database import Database
from noyra.core.types import content_hash, new_id, utc_now


@dataclass(frozen=True)
class EpochLease:
    database: Database
    subject_id: str
    target_id: str
    epoch_id: str
    epoch_number: int

    @classmethod
    def acquire(cls, database: Database, subject_id: str, target_id: str, *, expected_source_epoch: str | None) -> EpochLease:
        with database.transaction() as c:
            active = c.execute("SELECT * FROM migration_epochs WHERE subject_id=? AND status='active'", (subject_id,)).fetchone()
            if active is not None:
                raise ValueError("subject already has an active migration epoch")
            latest = c.execute("SELECT COALESCE(MAX(epoch_number),0) FROM migration_epochs WHERE subject_id=?", (subject_id,)).fetchone()[0]
            epoch_id = new_id("migrationepoch")
            number = int(latest) + 1
            now = utc_now()
            c.execute("INSERT INTO migration_epochs(epoch_id,subject_id,target_id,epoch_number,status,acquired_at,state_hash) VALUES (?,?,?,?,?,?,?)", (epoch_id, subject_id, target_id, number, "active", now, content_hash({"epoch_id": epoch_id, "subject_id": subject_id, "target_id": target_id, "epoch_number": number, "status": "active", "acquired_at": now})))
            return cls(database, subject_id, target_id, epoch_id, number)

    def assert_current(self) -> None:
        with self.database.connection() as c:
            row = c.execute("SELECT status,target_id FROM migration_epochs WHERE epoch_id=? AND subject_id=?", (self.epoch_id, self.subject_id)).fetchone()
        if row is None or row["status"] != "active" or row["target_id"] != self.target_id:
            raise ValueError("migration epoch is stale")

    def revoke(self, reason: str, actor: str) -> None:
        if not reason.strip() or not actor.strip():
            raise ValueError("epoch revoke metadata is required")
        with self.database.transaction() as c:
            updated = c.execute("UPDATE migration_epochs SET status='revoked', revoked_at=? WHERE epoch_id=? AND status='active'", (utc_now(), self.epoch_id))
            if updated.rowcount != 1:
                raise ValueError("migration epoch is already inactive")
