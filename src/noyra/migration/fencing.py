"""Single-active epoch fencing for migration cutover."""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from noyra.core.database import Database
from noyra.core.types import content_hash, new_id, utc_now

from .policy import MigrationStore


@dataclass(frozen=True)
class EpochLease:
    database: Database
    subject_id: str
    target_id: str
    epoch_id: str
    epoch_number: int

    @classmethod
    def acquire(
        cls,
        database: Database,
        subject_id: str,
        target_id: str,
        *,
        expected_source_epoch: str | None,
        target_validation_proof: Mapping[str, object] | None = None,
        actor: str = "system",
    ) -> EpochLease:
        """Acquire a lease only after a task-bound target proof is supplied.

        The target-agent restore/health verifier is introduced in the next
        migration module. Until it exists, this boundary intentionally rejects
        every unproven acquisition instead of allowing a caller to publish a
        target epoch optimistically.
        """
        proof = cls._validate_target_validation_proof(target_validation_proof)
        if proof["subject_id"] != subject_id or proof["target_id"] != target_id:
            raise ValueError("verified target proof does not match subject or target")
        with database.connection() as connection:
            task = connection.execute(
                "SELECT subject_id,target_id,status,manifest_digest FROM migration_tasks "
                "WHERE task_id=?",
                (proof["task_id"],),
            ).fetchone()
        if task is None or task["subject_id"] != subject_id or task["target_id"] != target_id:
            raise ValueError("verified target proof task binding is invalid")
        if task["status"] not in {
            "preparing",
            "transferring",
            "restoring",
            "validating",
            "cutover",
        }:
            raise ValueError("verified target proof task is not in cutover state")
        if not task["manifest_digest"] or task["manifest_digest"] != proof["manifest_digest"]:
            raise ValueError("verified target proof manifest binding is invalid")
        return cls._acquire_unchecked(
            database,
            subject_id,
            target_id,
            expected_source_epoch=expected_source_epoch,
            actor=actor,
        )

    @classmethod
    def _acquire_unchecked(
        cls,
        database: Database,
        subject_id: str,
        target_id: str,
        *,
        expected_source_epoch: str | None,
        actor: str = "system",
    ) -> EpochLease:
        """Internal fixture hook until target proof verification is integrated."""
        if not actor.strip():
            raise ValueError("epoch acquisition actor is required")
        with database.transaction() as c:
            active = c.execute(
                "SELECT * FROM migration_epochs WHERE subject_id=? AND status='active'",
                (subject_id,),
            ).fetchone()
            if active is not None:
                raise ValueError("subject already has an active migration epoch")
            latest = c.execute(
                "SELECT COALESCE(MAX(epoch_number),0) FROM migration_epochs WHERE subject_id=?",
                (subject_id,),
            ).fetchone()[0]
            if expected_source_epoch is not None and not expected_source_epoch.strip():
                raise ValueError("expected source epoch is invalid")
            if expected_source_epoch and expected_source_epoch.startswith("runtime-"):
                runtime = c.execute(
                    "SELECT version FROM runtime_state WHERE subject_id=?", (subject_id,)
                ).fetchone()
                if runtime is None or expected_source_epoch != f"runtime-{int(runtime['version'])}":
                    raise ValueError("source runtime epoch is stale")
            epoch_id = new_id("migrationepoch")
            number = int(latest) + 1
            now = utc_now()
            c.execute(
                "INSERT INTO migration_epochs(epoch_id,subject_id,target_id,epoch_number,status,acquired_at,state_hash) VALUES (?,?,?,?,?,?,?)",
                (
                    epoch_id,
                    subject_id,
                    target_id,
                    number,
                    "active",
                    now,
                    content_hash(
                        {
                            "epoch_id": epoch_id,
                            "subject_id": subject_id,
                            "target_id": target_id,
                            "epoch_number": number,
                            "status": "active",
                            "acquired_at": now,
                            "revoked_at": None,
                        }
                    ),
                ),
            )
            MigrationStore._append_audit(
                c,
                subject_id,
                "migration_epoch_acquired",
                actor.strip(),
                {"epoch_id": epoch_id, "target_id": target_id, "epoch_number": number},
            )
            return cls(database, subject_id, target_id, epoch_id, number)

    @staticmethod
    def _validate_target_validation_proof(
        proof: Mapping[str, object] | None,
    ) -> dict[str, str]:
        if not isinstance(proof, Mapping):
            raise ValueError("verified target restore and health proof is required")
        required = (
            "task_id",
            "subject_id",
            "target_id",
            "manifest_digest",
            "restore_report_digest",
            "health_report_digest",
            "target_signature",
        )
        if any(
            not isinstance(proof.get(key), str) or not str(proof[key]).strip() for key in required
        ):
            raise ValueError("verified target restore and health proof is invalid")
        return {key: str(proof[key]).strip() for key in required}

    def assert_current(self) -> None:
        with self.database.connection() as c:
            self.assert_current_in_transaction(c)

    def assert_current_in_transaction(self, connection: Any) -> None:
        row = connection.execute(
            "SELECT epoch_id,subject_id,status,target_id,epoch_number,acquired_at,revoked_at,state_hash "
            "FROM migration_epochs WHERE epoch_id=? AND subject_id=?",
            (self.epoch_id, self.subject_id),
        ).fetchone()
        if row is None or row["status"] != "active" or row["target_id"] != self.target_id:
            raise ValueError("migration epoch is stale")
        if row["state_hash"] != self._state_hash(row):
            raise ValueError("migration epoch integrity check failed")

    def revoke(self, reason: str, actor: str) -> None:
        if not reason.strip() or not actor.strip():
            raise ValueError("epoch revoke metadata is required")
        self.assert_current()
        with self.database.transaction() as c:
            revoked_at = utc_now()
            row = c.execute(
                "SELECT * FROM migration_epochs WHERE epoch_id=? AND status='active'",
                (self.epoch_id,),
            ).fetchone()
            if row is None:
                raise ValueError("migration epoch is already inactive")
            updated = c.execute(
                "UPDATE migration_epochs SET status='revoked', revoked_at=?, state_hash=? WHERE epoch_id=? AND status='active'",
                (
                    revoked_at,
                    self._state_hash({**dict(row), "status": "revoked", "revoked_at": revoked_at}),
                    self.epoch_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("migration epoch is already inactive")
            MigrationStore._append_audit(
                c,
                self.subject_id,
                "migration_epoch_revoked",
                actor.strip(),
                {"epoch_id": self.epoch_id, "reason": reason.strip()},
            )

    def complete(self, actor: str) -> None:
        if not actor.strip():
            raise ValueError("epoch completion actor is required")
        with self.database.transaction() as c:
            self.complete_in_transaction(c, actor)

    def complete_in_transaction(self, connection: Any, actor: str) -> None:
        """Complete the epoch without committing the caller's transaction."""
        if not actor.strip():
            raise ValueError("epoch completion actor is required")
        self.assert_current_in_transaction(connection)
        completed_at = utc_now()
        row = connection.execute(
            "SELECT * FROM migration_epochs WHERE epoch_id=? AND status='active'",
            (self.epoch_id,),
        ).fetchone()
        if row is None:
            raise ValueError("migration epoch is already inactive")
        updated = connection.execute(
            "UPDATE migration_epochs SET status='completed', revoked_at=?, state_hash=? WHERE epoch_id=? AND status='active'",
            (
                completed_at,
                self._state_hash({**dict(row), "status": "completed", "revoked_at": completed_at}),
                self.epoch_id,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("migration epoch is already inactive")
        MigrationStore._append_audit(
            connection,
            self.subject_id,
            "migration_epoch_completed",
            actor.strip(),
            {"epoch_id": self.epoch_id},
        )

    @staticmethod
    def _state_hash(row: object) -> str:
        values = row if isinstance(row, Mapping) else dict(cast(Any, row))
        return content_hash(
            {
                "epoch_id": values["epoch_id"],
                "subject_id": values["subject_id"],
                "target_id": values["target_id"],
                "epoch_number": int(values["epoch_number"]),
                "status": values["status"],
                "acquired_at": values["acquired_at"],
                "revoked_at": values.get("revoked_at"),
            }
        )
