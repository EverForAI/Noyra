from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import (
    canonical_json,
    content_hash,
    strict_int,
    strict_json_loads,
    utc_now,
)


@dataclass(frozen=True)
class WorkflowCheckpoint:
    workflow_id: str
    subject_id: str
    workflow_type: str
    checkpoint_version: int
    status: str
    state: dict[str, Any]
    reason: str
    created_at: str


class DurableWorkflowStore:
    """Minimal durable checkpoint and interrupt contract for local workflows."""

    _ALLOWED: ClassVar[dict[str | None, set[str]]] = {
        None: {"running"},
        "running": {"running", "waiting", "interrupted", "completed", "failed"},
        "waiting": {"running", "interrupted", "failed"},
        "interrupted": {"running", "failed"},
        "completed": set(),
        "failed": {"running"},
    }

    def __init__(self, database: Database):
        self.database = database

    def checkpoint(
        self,
        subject_id: str,
        workflow_id: str,
        workflow_type: str,
        status: str,
        state: dict[str, Any],
        *,
        reason: str,
    ) -> WorkflowCheckpoint:
        if not all(value.strip() for value in (subject_id, workflow_id, workflow_type, reason)):
            raise ValueError("workflow checkpoint metadata cannot be blank")
        if status not in {"running", "interrupted", "waiting", "completed", "failed"}:
            raise ValueError("workflow checkpoint status is invalid")
        encoded = canonical_json(state)
        if len(encoded) > 1_000_000:
            raise ValueError("workflow checkpoint exceeds one megabyte")
        now = utc_now()
        with self.database.transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM workflow_checkpoints WHERE workflow_id = ? "
                "ORDER BY checkpoint_version DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
            previous_record = None if previous is None else self._from_row(previous)
            previous_status = None if previous_record is None else previous_record.status
            if previous_record is not None and previous_record.subject_id != subject_id:
                raise PermissionError("workflow belongs to another subject")
            if previous_record is not None and previous_record.workflow_type != workflow_type:
                raise ValueError("workflow type cannot change across checkpoints")
            if status not in self._ALLOWED[previous_status]:
                raise ValueError(f"invalid workflow transition: {previous_status} -> {status}")
            version = 1 if previous_record is None else previous_record.checkpoint_version + 1
            connection.execute(
                "INSERT INTO workflow_checkpoints(workflow_id, subject_id, workflow_type, "
                "checkpoint_version, status, state_json, state_hash, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workflow_id,
                    subject_id,
                    workflow_type,
                    version,
                    status,
                    encoded,
                    content_hash(state),
                    reason,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM workflow_checkpoints WHERE workflow_id = ? "
                "AND checkpoint_version = ?",
                (workflow_id, version),
            ).fetchone()
        return self._from_row(row)

    def latest(self, workflow_id: str) -> WorkflowCheckpoint:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_checkpoints WHERE workflow_id = ? "
                "ORDER BY checkpoint_version DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"workflow not found: {workflow_id}")
        return self._from_row(row)

    def resume(
        self, subject_id: str, workflow_id: str, state: dict[str, Any], *, reason: str
    ) -> WorkflowCheckpoint:
        current = self.latest(workflow_id)
        return self.checkpoint(
            subject_id,
            workflow_id,
            current.workflow_type,
            "running",
            state,
            reason=reason,
        )

    def verify_integrity(self, subject_id: str) -> int:
        with self.database.read_transaction() as connection:
            return self._verify_integrity_connection(connection, subject_id)

    @classmethod
    def _verify_integrity_connection(cls, connection: Any, subject_id: str) -> int:
        if not isinstance(subject_id, str) or not subject_id.strip():
            raise IntegrityError("workflow checkpoint subject is invalid")
        crossing = connection.execute(
            "SELECT own.workflow_id FROM workflow_checkpoints own "
            "JOIN workflow_checkpoints other ON other.workflow_id = own.workflow_id "
            "AND other.subject_id != own.subject_id "
            "WHERE own.subject_id = ? LIMIT 1",
            (subject_id,),
        ).fetchone()
        if crossing is not None:
            raise IntegrityError(
                f"workflow checkpoint crosses subject boundary: {crossing['workflow_id']}"
            )
        rows = connection.execute(
            "SELECT * FROM workflow_checkpoints WHERE subject_id = ? "
            "ORDER BY workflow_id, checkpoint_version",
            (subject_id,),
        ).fetchall()
        previous: dict[str, WorkflowCheckpoint] = {}
        for row in rows:
            record = cls._from_row(row)
            if record.subject_id != subject_id:
                raise IntegrityError(
                    f"workflow checkpoint ownership mismatch: {record.workflow_id}"
                )
            prior = previous.get(record.workflow_id)
            expected_version = 1 if prior is None else prior.checkpoint_version + 1
            expected_previous_status = None if prior is None else prior.status
            if (
                record.checkpoint_version != expected_version
                or record.status not in cls._ALLOWED[expected_previous_status]
                or (
                    prior is not None
                    and (
                        record.workflow_type != prior.workflow_type
                        or cls._parse_time(record.created_at) < cls._parse_time(prior.created_at)
                    )
                )
            ):
                raise IntegrityError(
                    f"workflow checkpoint history is invalid: {record.workflow_id}"
                )
            previous[record.workflow_id] = record
        return len(rows)

    @classmethod
    def _from_row(cls, row: Any) -> WorkflowCheckpoint:
        workflow_id = cls._persisted_text(row["workflow_id"], "workflow id")
        context = f"workflow checkpoint {workflow_id}"
        subject_id = cls._persisted_text(row["subject_id"], f"{context} subject")
        workflow_type = cls._persisted_text(row["workflow_type"], f"{context} workflow type")
        reason = cls._persisted_text(row["reason"], f"{context} reason")
        created_at = cls._persisted_text(row["created_at"], f"{context} created_at")
        cls._parse_time(created_at)
        status = row["status"]
        if not isinstance(status, str) or status not in cls._ALLOWED:
            raise IntegrityError(f"{context} status is invalid")
        try:
            checkpoint_version = strict_int(row["checkpoint_version"])
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"{context} version is invalid") from error
        if checkpoint_version < 1:
            raise IntegrityError(f"{context} version is invalid")
        raw_state = row["state_json"]
        if not isinstance(raw_state, str):
            raise IntegrityError(f"{context} state JSON is invalid")
        try:
            state = strict_json_loads(raw_state)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"{context} state JSON is invalid") from error
        state_hash = row["state_hash"]
        if (
            not isinstance(state, dict)
            or not cls._valid_hash(state_hash)
            or content_hash(state) != state_hash
        ):
            raise IntegrityError(f"workflow checkpoint hash mismatch: {workflow_id}")
        return WorkflowCheckpoint(
            workflow_id=workflow_id,
            subject_id=subject_id,
            workflow_type=workflow_type,
            checkpoint_version=checkpoint_version,
            status=status,
            state=state,
            reason=reason,
            created_at=created_at,
        )

    @staticmethod
    def _persisted_text(value: Any, context: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise IntegrityError(f"{context} is invalid")
        return value

    @staticmethod
    def _valid_hash(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise IntegrityError("workflow checkpoint timestamp is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise IntegrityError("workflow checkpoint timestamp is invalid")
        return parsed
