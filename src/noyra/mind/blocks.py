from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, ClassVar

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import content_hash, new_id, utc_now

from .memory import MemoryStore


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


@dataclass(frozen=True, slots=True)
class MemoryBlockSourceReference:
    memory_id: str
    revision_number: int
    revision_hash: str


@dataclass(frozen=True, slots=True)
class MemoryBlockRevisionLineage:
    revision_id: str
    block_id: str
    version: int
    lineage_status: str
    sources: tuple[MemoryBlockSourceReference, ...]


class MemoryBlockStore:
    """Small stateful context blocks with revision-level memory provenance."""

    _TYPES: ClassVar[frozenset[str]] = frozenset(
        {"working_context", "self_summary", "knowledge_focus", "relationship_context"}
    )
    _MAX_SOURCES: ClassVar[int] = 64

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
        source_memory_ids: tuple[str, ...] | None = None,
    ) -> MemoryBlockRecord:
        self._validate(block_type, label, content, reason, actor)
        sources = self._validate_source_ids(source_memory_ids)
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
            self._revision(
                connection,
                block_id,
                1,
                content.strip(),
                "active",
                reason,
                actor,
                now,
                sources,
            )
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
        source_memory_ids: tuple[str, ...] | None = None,
    ) -> MemoryBlockRecord:
        if status not in {"active", "archived"}:
            raise ValueError("memory block status is invalid")
        if not content.strip() or len(content) > 100_000 or not reason.strip() or not actor.strip():
            raise ValueError("memory block revision is invalid")
        sources = self._validate_source_ids(source_memory_ids)
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
                connection,
                block_id,
                version,
                content.strip(),
                status,
                reason,
                actor,
                now,
                sources,
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

    def lineage_history(self, block_id: str) -> list[MemoryBlockRevisionLineage]:
        """Return verified source snapshots for every revision, including legacy unknowns."""
        with self.database.read_transaction() as connection:
            block = connection.execute(
                "SELECT block_id FROM memory_blocks WHERE block_id = ?", (block_id,)
            ).fetchone()
            if block is None:
                raise NotFoundError(f"memory block not found: {block_id}")
            revisions = connection.execute(
                "SELECT * FROM memory_block_revisions WHERE block_id = ? ORDER BY version",
                (block_id,),
            ).fetchall()
            result: list[MemoryBlockRevisionLineage] = []
            for revision in revisions:
                version = self._persisted_version(
                    revision["version"], f"memory block {block_id} revision version"
                )
                lineage_status, sources = self._verified_revision_lineage(connection, revision)
                result.append(
                    MemoryBlockRevisionLineage(
                        str(revision["revision_id"]),
                        block_id,
                        version,
                        lineage_status,
                        sources,
                    )
                )
            return result

    def verify_integrity(self, subject_id: str) -> dict[str, int]:
        with self.database.read_transaction() as connection:
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
                    self._verified_revision_lineage(connection, revision)
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

    @classmethod
    def _validate_source_ids(cls, source_memory_ids: tuple[str, ...] | None) -> tuple[str, ...]:
        """Require an explicit parent list; empty means declared no memory parents only."""
        if source_memory_ids is None:
            raise ValueError("memory block lineage must be explicitly declared")
        if not isinstance(source_memory_ids, tuple) or len(source_memory_ids) > cls._MAX_SOURCES:
            raise ValueError("memory block source list is invalid")
        if any(
            not isinstance(memory_id, str)
            or not memory_id
            or memory_id != memory_id.strip()
            for memory_id in source_memory_ids
        ):
            raise ValueError("memory block source list is invalid")
        if len(set(source_memory_ids)) != len(source_memory_ids):
            raise ValueError("memory block source list contains duplicates")
        return tuple(sorted(source_memory_ids))

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
        source_memory_ids: tuple[str, ...],
    ) -> None:
        block = connection.execute(
            "SELECT subject_id FROM memory_blocks WHERE block_id = ?", (block_id,)
        ).fetchone()
        if block is None:
            raise IntegrityError("memory block disappeared during revision")
        source_refs = MemoryBlockStore._capture_sources(
            connection, str(block["subject_id"]), source_memory_ids
        )
        lineage_status = "memory" if source_refs else "declared_no_memory_sources"
        lineage_hash = MemoryBlockStore._lineage_hash(lineage_status, source_refs)
        revision_id = new_id("mblockrev")
        connection.execute(
            "INSERT INTO memory_block_revisions(revision_id, block_id, version, content, "
            "content_hash, status, reason, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_id,
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
        connection.execute(
            "INSERT INTO memory_block_revision_lineage(revision_id, block_id, lineage_status, "
            "lineage_hash) VALUES (?, ?, ?, ?)",
            (revision_id, block_id, lineage_status, lineage_hash),
        )
        for source in source_refs:
            connection.execute(
                "INSERT INTO memory_block_revision_sources(revision_id, block_id, "
                "source_memory_id, source_revision_number, source_revision_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    revision_id,
                    block_id,
                    source.memory_id,
                    source.revision_number,
                    source.revision_hash,
                ),
            )

    @classmethod
    def _capture_sources(
        cls, connection: Any, subject_id: str, source_memory_ids: tuple[str, ...]
    ) -> tuple[MemoryBlockSourceReference, ...]:
        refs: list[MemoryBlockSourceReference] = []
        for memory_id in source_memory_ids:
            memory = connection.execute(
                "SELECT * FROM memories WHERE memory_id = ? AND subject_id = ?",
                (memory_id, subject_id),
            ).fetchone()
            if memory is None:
                raise ValueError("memory block source memory is unavailable")
            memory_record = MemoryStore._from_row(memory)
            revision = connection.execute(
                "SELECT * FROM memory_revisions WHERE memory_id = ? AND revision_number = ?",
                (memory_id, memory_record.current_revision),
            ).fetchone()
            if revision is None:
                raise IntegrityError("memory block source revision is unavailable")
            if cls._source_revision_number(revision) != memory_record.current_revision:
                raise IntegrityError("memory block source revision number mismatch")
            if (
                revision["content"] != memory_record.content
                or revision["content_hash"] != memory["content_hash"]
                or float(revision["salience"]) != memory_record.salience
                or float(revision["confidence"]) != memory_record.confidence
                or revision["status"] != memory_record.status
            ):
                raise IntegrityError("current memory state does not match its source revision")
            refs.append(
                MemoryBlockSourceReference(
                    memory_id,
                    memory_record.current_revision,
                    cls._source_revision_hash(revision),
                )
            )
        return tuple(refs)

    @classmethod
    def _verified_revision_lineage(
        cls, connection: Any, revision: Any
    ) -> tuple[str, tuple[MemoryBlockSourceReference, ...]]:
        revision_id = str(revision["revision_id"])
        lineage = connection.execute(
            "SELECT * FROM memory_block_revision_lineage WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if lineage is None:
            raise IntegrityError("memory block revision lineage record is missing")
        if lineage["block_id"] != revision["block_id"]:
            raise IntegrityError("memory block lineage identity mismatch")
        lineage_status = lineage["lineage_status"]
        if lineage_status not in {"unknown", "declared_no_memory_sources", "memory"}:
            raise IntegrityError("memory block lineage status is invalid")
        if lineage_status == "unknown":
            if lineage["lineage_hash"] != "" or connection.execute(
                "SELECT 1 FROM memory_block_revision_sources WHERE revision_id=? LIMIT 1",
                (revision_id,),
            ).fetchone():
                raise IntegrityError("unknown memory block lineage has source evidence")
            return "unknown", ()
        source_rows = connection.execute(
            "SELECT s.revision_id,s.block_id,s.source_memory_id,s.source_revision_number,"
            "s.source_revision_hash,mr.memory_id,mr.revision_number,mr.content,"
            "mr.content_hash,mr.state_hash,mr.salience,mr.confidence,mr.status,"
            "mr.reason,mr.source_event_ids_json,mr.created_at,"
            "m.subject_id AS source_subject,b.subject_id AS block_subject "
            "FROM memory_block_revision_sources s "
            "JOIN memory_revisions mr ON mr.memory_id=s.source_memory_id "
            "AND mr.revision_number=s.source_revision_number "
            "JOIN memories m ON m.memory_id=s.source_memory_id "
            "JOIN memory_blocks b ON b.block_id=s.block_id "
            "WHERE s.revision_id=? ORDER BY s.source_memory_id",
            (revision_id,),
        ).fetchall()
        if lineage_status == "declared_no_memory_sources" and source_rows:
            raise IntegrityError("declared-no-memory-sources block revision has memory sources")
        if lineage_status == "memory" and not source_rows:
            raise IntegrityError("memory-derived block revision has no source memories")
        expected_lineage_hash = cls._lineage_hash(lineage_status, ())
        if (
            lineage_status == "declared_no_memory_sources"
            and lineage["lineage_hash"] != expected_lineage_hash
        ):
            raise IntegrityError("memory block declared-no-memory-sources lineage hash mismatch")

        sources: list[MemoryBlockSourceReference] = []
        for row in source_rows:
            if (
                row["source_subject"] != row["block_subject"]
                or row["block_id"] != revision["block_id"]
            ):
                raise IntegrityError("memory block source subject or identity mismatch")
            source_revision_number = cls._source_revision_number(row)
            source_hash = cls._source_revision_hash(row)
            if source_hash != row["source_revision_hash"]:
                raise IntegrityError("memory block source revision hash mismatch")
            sources.append(
                MemoryBlockSourceReference(
                    str(row["source_memory_id"]), source_revision_number, source_hash
                )
            )
        if lineage_status == "memory" and lineage["lineage_hash"] != cls._lineage_hash(
            lineage_status, tuple(sources)
        ):
            raise IntegrityError("memory block lineage hash mismatch")
        return str(lineage_status), tuple(sources)

    @classmethod
    def _source_revision_number(cls, revision: Any) -> int:
        return MemoryStore._persisted_integer(
            revision["revision_number"], "memory block source revision number", minimum=1
        )

    @classmethod
    def _source_revision_hash(cls, revision: Any) -> str:
        content = revision["content"]
        content_digest = revision["content_hash"]
        if not isinstance(content, str) or not isinstance(content_digest, str):
            raise IntegrityError("memory block source revision content is invalid")
        if content_hash(content) != content_digest:
            raise IntegrityError("memory block source revision content hash mismatch")
        salience = MemoryStore._persisted_float(
            revision["salience"], "memory block source revision salience", minimum=0.0, maximum=1.0
        )
        confidence = MemoryStore._persisted_float(
            revision["confidence"],
            "memory block source revision confidence",
            minimum=0.0,
            maximum=1.0,
        )
        status = revision["status"]
        if not isinstance(status, str) or status not in {"active", "superseded", "archived"}:
            raise IntegrityError("memory block source revision status is invalid")
        if MemoryStore._state_hash(content, salience, confidence, status) != revision["state_hash"]:
            raise IntegrityError("memory block source revision state hash mismatch")
        event_sources = revision["source_event_ids_json"]
        if not isinstance(event_sources, str):
            raise IntegrityError("memory block source event lineage is invalid")
        return content_hash(
            {
                "memory_id": revision["memory_id"],
                "revision_number": cls._source_revision_number(revision),
                "content_hash": content_digest,
                "state_hash": revision["state_hash"],
                "salience": salience,
                "confidence": confidence,
                "status": status,
                "reason": revision["reason"],
                "source_event_ids_json": event_sources,
                "created_at": revision["created_at"],
            }
        )

    @staticmethod
    def _lineage_hash(
        lineage_status: str, sources: tuple[MemoryBlockSourceReference, ...]
    ) -> str:
        return content_hash(
            {
                "lineage_status": lineage_status,
                "sources": [
                    {
                        "memory_id": source.memory_id,
                        "revision_number": source.revision_number,
                        "revision_hash": source.revision_hash,
                    }
                    for source in sources
                ],
            }
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
