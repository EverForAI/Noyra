from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, ClassVar

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import content_hash, new_id, utc_now


@dataclass(frozen=True)
class MemoryBlockRecord:
    block_id: str
    subject_id: str
    block_type: str
    label: str
    content: str
    privacy_level: str
    status: str
    version: int
    created_at: str
    updated_at: str


class MemoryBlockStore:
    """Small stateful context blocks with complete revision history."""

    _TYPES: ClassVar[frozenset[str]] = frozenset(
        {"working_context", "self_summary", "knowledge_focus", "relationship_context"}
    )

    def __init__(self, database: Database):
        self.database = database

    def create(
        self,
        subject_id: str,
        block_type: str,
        label: str,
        content: str,
        *,
        privacy_level: str = "private",
        reason: str,
        actor: str = "subject",
    ) -> MemoryBlockRecord:
        self._validate(block_type, label, content, reason, actor)
        block_id = new_id("mblock")
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO memory_blocks(block_id, subject_id, block_type, label, content, "
                "content_hash, privacy_level, status, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)",
                (
                    block_id,
                    subject_id,
                    block_type,
                    label.strip(),
                    content.strip(),
                    content_hash(content.strip()),
                    privacy_level,
                    now,
                    now,
                ),
            )
            self._revision(connection, block_id, 1, content.strip(), "active", reason, actor, now)
        return self.get(block_id)

    def revise(
        self,
        block_id: str,
        content: str,
        *,
        reason: str,
        actor: str = "subject",
        expected_version: int | None = None,
        status: str = "active",
    ) -> MemoryBlockRecord:
        if status not in {"active", "archived"}:
            raise ValueError("memory block status is invalid")
        if not content.strip() or len(content) > 100_000 or not reason.strip() or not actor.strip():
            raise ValueError("memory block revision is invalid")
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memory_blocks WHERE block_id = ?", (block_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"memory block not found: {block_id}")
            current_version = self._persisted_version(
                row["version"], f"memory block {block_id} version"
            )
            if expected_version is not None and current_version != expected_version:
                raise ValueError("memory block version changed before revision")
            version = current_version + 1
            connection.execute(
                "UPDATE memory_blocks SET content = ?, content_hash = ?, status = ?, "
                "version = ?, updated_at = ? WHERE block_id = ?",
                (content.strip(), content_hash(content.strip()), status, version, now, block_id),
            )
            self._revision(
                connection, block_id, version, content.strip(), status, reason, actor, now
            )
        return self.get(block_id)

    def get(self, block_id: str) -> MemoryBlockRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_blocks WHERE block_id = ?", (block_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"memory block not found: {block_id}")
        return self._from_row(row)

    def active(self, subject_id: str) -> list[MemoryBlockRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_blocks WHERE subject_id = ? AND status = 'active' "
                "ORDER BY block_type, label",
                (subject_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def verify_integrity(self, subject_id: str) -> dict[str, int]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_blocks WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            revision_count = 0
            for row in rows:
                block = self._from_row(row)
                revisions = connection.execute(
                    "SELECT * FROM memory_block_revisions WHERE block_id = ? ORDER BY version",
                    (row["block_id"],),
                ).fetchall()
                if len(revisions) != block.version:
                    raise IntegrityError("memory block revision coverage mismatch")
                for expected_version, revision in enumerate(revisions, start=1):
                    revision_version = self._persisted_version(
                        revision["version"], "memory block revision version"
                    )
                    if revision_version != expected_version:
                        raise IntegrityError("memory block revision sequence mismatch")
                    if content_hash(revision["content"]) != revision["content_hash"]:
                        raise IntegrityError("memory block revision hash mismatch")
                revision_count += len(revisions)
        return {"memory_blocks": len(rows), "memory_block_revisions": revision_count}

    @classmethod
    def _validate(cls, block_type: str, label: str, content: str, reason: str, actor: str) -> None:
        if block_type not in cls._TYPES:
            raise ValueError("memory block type is invalid")
        if (
            not label.strip()
            or len(label) > 128
            or not content.strip()
            or len(content) > 100_000
            or not reason.strip()
            or not actor.strip()
        ):
            raise ValueError("memory block content is invalid")

    @staticmethod
    def _revision(
        connection: Any,
        block_id: str,
        version: int,
        content: str,
        status: str,
        reason: str,
        actor: str,
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO memory_block_revisions(revision_id, block_id, version, content, "
            "content_hash, status, reason, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("mblockrev"),
                block_id,
                version,
                content,
                content_hash(content),
                status,
                reason,
                actor,
                now,
            ),
        )

    @staticmethod
    def _from_row(row: Any) -> MemoryBlockRecord:
        if content_hash(row["content"]) != row["content_hash"]:
            raise IntegrityError(f"memory block hash mismatch: {row['block_id']}")
        version = MemoryBlockStore._persisted_version(
            row["version"], f"memory block {row['block_id']} version"
        )
        return MemoryBlockRecord(
            str(row["block_id"]),
            str(row["subject_id"]),
            str(row["block_type"]),
            str(row["label"]),
            str(row["content"]),
            str(row["privacy_level"]),
            str(row["status"]),
            version,
            str(row["created_at"]),
            str(row["updated_at"]),
        )

    @staticmethod
    def _persisted_version(value: Any, context: str) -> int:
        if isinstance(value, bool):
            raise IntegrityError(f"{context} is invalid")
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"{context} is invalid") from error
        if not math.isfinite(numeric) or not numeric.is_integer() or numeric < 1:
            raise IntegrityError(f"{context} is invalid")
        return int(numeric)
