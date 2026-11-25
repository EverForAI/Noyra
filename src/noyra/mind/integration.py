from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.source_provenance import MAX_SOURCE_EVENT_IDS
from noyra.core.types import canonical_json, content_hash, new_id, strict_json_loads, utc_now

from .errors import CausalValidationError
from .memory import MemoryStore
from .types import MemoryRecord
from .validation import validate_event_ids


@dataclass(frozen=True)
class MemoryIntegrationRecord:
    integration_id: str
    subject_id: str
    operation: str
    source_memory_ids: tuple[str, ...]
    output_memory_id: str | None
    evidence_event_ids: tuple[str, ...]
    reason: str
    confidence: float
    status: str
    created_at: str
    reverted_at: str | None


class MemoryIntegrationError(ValueError):
    pass


class MemoryIntegrationSupervisor:
    """Commits merge/supersession/conflict proposals without deleting originals."""

    def __init__(self, database: Database, subject_id: str):
        self.database = database
        self.subject_id = subject_id
        self.memories = MemoryStore(database)

    def integrate(
        self,
        operation: str,
        source_memory_ids: tuple[str, ...],
        *,
        synthesis: str,
        confidence: float,
        evidence_event_ids: tuple[str, ...],
        reason: str,
    ) -> MemoryIntegrationRecord:
        with self.database.transaction() as connection:
            identifier = self._integrate_connection(
                connection,
                operation,
                source_memory_ids,
                synthesis=synthesis,
                confidence=confidence,
                evidence_event_ids=evidence_event_ids,
                reason=reason,
            )
        return self.get(identifier)

    def _integrate_connection(
        self,
        connection: Any,
        operation: str,
        source_memory_ids: tuple[str, ...],
        *,
        synthesis: str,
        confidence: float,
        evidence_event_ids: tuple[str, ...],
        reason: str,
    ) -> str:
        """Run the same supervisor validation and writes in a caller-owned transaction."""
        if operation not in {"merge", "supersede", "contradict"}:
            raise MemoryIntegrationError("invalid memory integration operation")
        if len(source_memory_ids) < 2 or len(set(source_memory_ids)) != len(source_memory_ids):
            raise MemoryIntegrationError("integration requires distinct source memories")
        if not synthesis.strip() or not reason.strip() or not 0 <= confidence <= 1:
            raise MemoryIntegrationError("invalid integration synthesis")
        if not evidence_event_ids or len(set(evidence_event_ids)) != len(evidence_event_ids):
            raise MemoryIntegrationError("integration evidence is required")
        records = [
            self.memories._load_connection(connection, memory_id) for memory_id in source_memory_ids
        ]
        if any(record.subject_id != self.subject_id for record in records):
            raise MemoryIntegrationError("memory integration crosses subject boundary")
        if confidence > max(record.confidence for record in records):
            raise MemoryIntegrationError("integration confidence exceeds source evidence")
        source_events = {
            record.memory_id: self._current_source_events(connection, record) for record in records
        }
        combined = dict.fromkeys(evidence_event_ids)
        for sources in source_events.values():
            combined.update(dict.fromkeys(sources))
        if len(combined) > MAX_SOURCE_EVENT_IDS:
            raise MemoryIntegrationError("integration exceeds the memory source limit")
        try:
            output_sources = validate_event_ids(connection, self.subject_id, combined)
        except CausalValidationError as error:
            raise MemoryIntegrationError("integration source events are unavailable") from error
        output = self.memories._create_connection(
            connection,
            self.subject_id,
            "reflection" if operation == "contradict" else "semantic",
            synthesis.strip(),
            salience=max(record.salience for record in records),
            confidence=confidence,
            source_event_ids=output_sources,
            reason=f"memory integration: {reason.strip()}",
        )
        if operation in {"merge", "supersede"}:
            for record in records:
                self.memories._revise_connection(
                    connection,
                    record.memory_id,
                    content=record.content,
                    salience=record.salience,
                    confidence=record.confidence,
                    status="superseded",
                    reason=f"superseded by integration {output.memory_id}",
                    source_event_ids=source_events[record.memory_id],
                    expected_revision=record.current_revision,
                )
        now = utc_now()
        integration_id = new_id("memint")
        state_hash = self._hash(
            operation,
            source_memory_ids,
            output.memory_id,
            evidence_event_ids,
            reason.strip(),
            confidence,
            "active",
        )
        connection.execute(
            """INSERT INTO memory_integrations(
                integration_id, subject_id, operation, source_memory_ids_json,
                output_memory_id, evidence_event_ids_json, reason, confidence,
                status, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                integration_id,
                self.subject_id,
                operation,
                canonical_json(list(source_memory_ids)),
                output.memory_id,
                canonical_json(list(evidence_event_ids)),
                reason.strip(),
                confidence,
                state_hash,
                now,
            ),
        )
        self._revision(connection, integration_id, "commit", reason.strip(), now)
        return integration_id

    def revert(self, integration_id: str, *, reason: str) -> MemoryIntegrationRecord:
        now = utc_now()
        with self.database.transaction() as connection:
            record = self._get_connection(connection, integration_id)
            if record.status != "active" or not reason.strip():
                raise MemoryIntegrationError("integration cannot be reverted")
            output = (
                self.memories._load_connection(connection, record.output_memory_id)
                if record.output_memory_id is not None
                else None
            )
            for memory_id in record.source_memory_ids:
                memory = self.memories._load_connection(connection, memory_id)
                if memory.status == "superseded":
                    self.memories._revise_connection(
                        connection,
                        memory_id,
                        content=memory.content,
                        salience=memory.salience,
                        confidence=memory.confidence,
                        status="active",
                        reason=f"integration reverted: {reason.strip()}",
                        source_event_ids=self._current_source_events(connection, memory),
                        expected_revision=memory.current_revision,
                    )
            if output is not None and output.status != "archived":
                self.memories._revise_connection(
                    connection,
                    output.memory_id,
                    content=output.content,
                    salience=output.salience,
                    confidence=output.confidence,
                    status="archived",
                    reason=f"integration output reverted: {reason.strip()}",
                    source_event_ids=self._current_source_events(connection, output),
                    expected_revision=output.current_revision,
                )
            state_hash = self._hash(
                record.operation,
                record.source_memory_ids,
                record.output_memory_id,
                record.evidence_event_ids,
                record.reason,
                record.confidence,
                "reverted",
            )
            connection.execute(
                "UPDATE memory_integrations SET status = 'reverted', state_hash = ?, "
                "reverted_at = ? WHERE integration_id = ?",
                (state_hash, now, integration_id),
            )
            self._revision(connection, integration_id, "revert", reason.strip(), now)
        return self.get(integration_id)

    def get(self, integration_id: str) -> MemoryIntegrationRecord:
        with self.database.connection() as connection:
            return self._get_connection(connection, integration_id)

    def _get_connection(self, connection: Any, integration_id: str) -> MemoryIntegrationRecord:
        row = connection.execute(
            "SELECT * FROM memory_integrations WHERE subject_id = ? AND integration_id = ?",
            (self.subject_id, integration_id),
        ).fetchone()
        if row is None:
            raise MemoryIntegrationError("memory integration is unavailable")
        record = self._record(row)
        expected = self._hash(
            record.operation,
            record.source_memory_ids,
            record.output_memory_id,
            record.evidence_event_ids,
            record.reason,
            record.confidence,
            record.status,
        )
        if expected != row["state_hash"]:
            raise IntegrityError("memory integration hash mismatch")
        return record

    def _current_source_events(self, connection: Any, memory: MemoryRecord) -> tuple[str, ...]:
        if memory.subject_id != self.subject_id:
            raise MemoryIntegrationError("memory integration crosses subject boundary")
        row = connection.execute(
            "SELECT source_event_ids_json FROM memory_revisions "
            "WHERE memory_id=? AND revision_number=?",
            (memory.memory_id, memory.current_revision),
        ).fetchone()
        if row is None:
            raise MemoryIntegrationError("memory source revision is unavailable")
        try:
            sources = strict_json_loads(row["source_event_ids_json"])
        except (ValueError, TypeError, RecursionError) as error:
            raise MemoryIntegrationError("memory source references are invalid") from error
        if (
            not isinstance(sources, list)
            or not sources
            or len(sources) > MAX_SOURCE_EVENT_IDS
            or not all(isinstance(source, str) and source for source in sources)
            or len(set(sources)) != len(sources)
        ):
            raise MemoryIntegrationError("memory source references are invalid")
        return tuple(sources)

    @staticmethod
    def _record(row: Any) -> MemoryIntegrationRecord:
        return MemoryIntegrationRecord(
            row["integration_id"],
            row["subject_id"],
            row["operation"],
            tuple(json.loads(row["source_memory_ids_json"])),
            row["output_memory_id"],
            tuple(json.loads(row["evidence_event_ids_json"])),
            row["reason"],
            float(row["confidence"]),
            row["status"],
            row["created_at"],
            row["reverted_at"],
        )

    @staticmethod
    def _hash(
        operation: str,
        sources: tuple[str, ...],
        output: str | None,
        evidence: tuple[str, ...],
        reason: str,
        confidence: float,
        status: str,
    ) -> str:
        return content_hash(
            {
                "operation": operation,
                "source_memory_ids": list(sources),
                "output_memory_id": output,
                "evidence_event_ids": list(evidence),
                "reason": reason,
                "confidence": confidence,
                "status": status,
            }
        )

    @staticmethod
    def _revision(connection: Any, integration_id: str, action: str, reason: str, now: str) -> None:
        payload = {
            "integration_id": integration_id,
            "action": action,
            "reason": reason,
            "created_at": now,
        }
        connection.execute(
            """INSERT INTO memory_integration_revisions(
                revision_id, integration_id, action, reason, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (new_id("memintrev"), integration_id, action, reason, content_hash(payload), now),
        )
