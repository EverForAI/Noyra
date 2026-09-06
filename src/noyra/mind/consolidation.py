from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

from .memory import MemoryStore
from .types import MemoryConsolidationRecord, MemoryRecord


@dataclass(frozen=True)
class _ConsolidationCandidate:
    memory: MemoryRecord
    source_event_ids: tuple[str, ...]
    access_count: int
    last_accessed_at: str | None


class MemoryConsolidator:
    """Deterministically decays, strengthens, and summarizes long-term memory state."""

    def __init__(
        self,
        database: Database,
        subject_id: str,
        *,
        clock: Callable[[], str] = utc_now,
        interval_seconds: float = 86_400,
        stale_after_days: float = 30,
        archive_after_days: float = 180,
        minimum_active_memories: int = 24,
    ):
        self.database = database
        self.subject_id = subject_id
        self.clock = clock
        self.interval_seconds = interval_seconds
        self.stale_after_days = stale_after_days
        self.archive_after_days = archive_after_days
        self.minimum_active_memories = minimum_active_memories
        self.memories = MemoryStore(database, clock=clock)
        self.events = EventStore(database)

    def run_due(self) -> str | None:
        if not self._is_due():
            return None
        candidates = self._candidates()
        period = self.clock()[:10]
        key = f"memory-consolidation:{period}"
        with self.database.connection() as connection:
            existing = connection.execute(
                "SELECT status FROM memory_consolidation_runs WHERE subject_id = ? "
                "AND idempotency_key = ?",
                (self.subject_id, key),
            ).fetchone()
        if existing is not None:
            return f"memory_consolidation_{existing['status']}"
        return self._commit(key, candidates)

    def latest(self) -> MemoryConsolidationRecord | None:
        history = self.memories.consolidation_history(self.subject_id)
        return history[0] if history else None

    def _is_due(self) -> bool:
        latest = self.latest()
        if latest is None:
            return True
        elapsed = self._parse_time(self.clock()) - self._parse_time(latest.created_at)
        return elapsed.total_seconds() >= self.interval_seconds

    def _candidates(self) -> list[_ConsolidationCandidate]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT m.*, COALESCE(SUM(a.access_count), 0) AS total_access_count,
                          MAX(a.last_accessed_at) AS last_accessed_at,
                          r.source_event_ids_json
                   FROM memories m LEFT JOIN memory_accesses a ON a.memory_id = m.memory_id
                   LEFT JOIN memory_revisions r
                     ON r.memory_id = m.memory_id AND r.revision_number = m.current_revision
                   WHERE m.subject_id = ? AND m.status = 'active'
                   GROUP BY m.memory_id, r.source_event_ids_json
                   ORDER BY m.updated_at, m.memory_id""",
                (self.subject_id,),
            ).fetchall()
            candidates = []
            for row in rows:
                if row["source_event_ids_json"] is None:
                    raise IntegrityError(f"memory revision is missing: {row['memory_id']}")
                sources = json.loads(row["source_event_ids_json"])
                if not isinstance(sources, list) or not all(
                    isinstance(item, str) for item in sources
                ):
                    raise IntegrityError(f"memory sources are invalid: {row['memory_id']}")
                candidates.append(
                    _ConsolidationCandidate(
                        MemoryStore._from_row(row),
                        tuple(sources),
                        int(row["total_access_count"]),
                        row["last_accessed_at"],
                    )
                )
        return candidates

    def _commit(self, key: str, candidates: list[_ConsolidationCandidate]) -> str:
        now = self.clock()
        current = self._parse_time(now)
        by_hash: dict[tuple[str, str], list[_ConsolidationCandidate]] = defaultdict(list)
        for candidate in candidates:
            normalized = " ".join(candidate.memory.content.casefold().split())
            by_hash[(candidate.memory.memory_type, content_hash(normalized))].append(candidate)
        duplicate_losers: dict[str, str] = {}
        for group in by_hash.values():
            if len(group) < 2:
                continue
            group.sort(
                key=lambda item: (
                    -item.memory.confidence,
                    -item.memory.salience,
                    -item.access_count,
                    item.memory.created_at,
                    item.memory.memory_id,
                )
            )
            keeper = group[0].memory.memory_id
            for duplicate in group[1:]:
                duplicate_losers[duplicate.memory.memory_id] = keeper

        archived: list[str] = []
        strengthened: list[str] = []
        summary_ids: list[str] = []
        active_count = len(candidates)
        reviewed_count = len(candidates)
        with self.database.transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM memory_consolidation_runs WHERE subject_id = ? "
                    "AND idempotency_key = ?",
                    (self.subject_id, key),
                ).fetchone()
                is not None
            ):
                return "memory_consolidation_replayed"
            event = self.events._append_connection(
                connection,
                self.subject_id,
                "memory_consolidation",
                "memory_supervisor",
                {"idempotency_key": key, "reviewed_count": reviewed_count},
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=now,
                event_id=None,
            )
            dispositions: list[tuple[str, str | None, str, str]] = []
            for candidate in candidates:
                memory = candidate.memory
                age_days = max(
                    0.0,
                    (current - self._parse_time(memory.updated_at)).total_seconds() / 86_400,
                )
                duplicate_keeper = duplicate_losers.get(memory.memory_id)
                if duplicate_keeper is not None:
                    self.memories._revise_connection(
                        connection,
                        memory.memory_id,
                        content=memory.content,
                        salience=memory.salience,
                        confidence=memory.confidence,
                        status="archived",
                        reason=f"duplicate consolidated into {duplicate_keeper}",
                        source_event_ids=(*candidate.source_event_ids, event.event_id),
                        expected_revision=memory.current_revision,
                    )
                    archived.append(memory.memory_id)
                    active_count -= 1
                    dispositions.append(
                        (
                            memory.memory_id,
                            duplicate_keeper,
                            "archived_duplicate",
                            "exact duplicate",
                        )
                    )
                    continue
                if candidate.access_count >= 3 and memory.salience < 0.95:
                    revised = self.memories._revise_connection(
                        connection,
                        memory.memory_id,
                        content=memory.content,
                        salience=min(
                            0.95, memory.salience + min(0.12, candidate.access_count * 0.02)
                        ),
                        confidence=memory.confidence,
                        status="active",
                        reason="repeated contextual recall strengthened accessibility",
                        source_event_ids=(*candidate.source_event_ids, event.event_id),
                        expected_revision=memory.current_revision,
                    )
                    strengthened.append(revised.memory_id)
                    dispositions.append(
                        (memory.memory_id, memory.memory_id, "strengthened", "repeated recall")
                    )
                    continue
                forgettable = memory.memory_type in {"episodic", "reflection", "prediction"}
                stale = age_days >= self.archive_after_days and candidate.access_count == 0
                weak = memory.salience < 0.3 and memory.confidence < 0.55
                if forgettable and stale and weak and active_count > self.minimum_active_memories:
                    cluster = tuple(
                        sorted(
                            item.memory.memory_id
                            for item in candidates
                            if item.memory.memory_type == memory.memory_type
                            and item.access_count == 0
                            and item.memory.salience < 0.3
                            and item.memory.confidence < 0.55
                            and max(
                                0.0,
                                (current - self._parse_time(item.memory.updated_at)).total_seconds()
                                / 86_400,
                            )
                            >= self.stale_after_days
                        )
                    )[:8]
                    summary_memory_id: str | None = None
                    if len(cluster) >= 2:
                        summary_content = "Consolidated memory cluster: " + ", ".join(cluster)
                        existing_summary = connection.execute(
                            "SELECT memory_id FROM memories WHERE subject_id = ? "
                            "AND content_hash = ? LIMIT 1",
                            (self.subject_id, content_hash(summary_content)),
                        ).fetchone()
                        if existing_summary is None:
                            summary_memory = self.memories._create_connection(
                                connection,
                                self.subject_id,
                                "reflection",
                                summary_content,
                                salience=0.45,
                                confidence=0.45,
                                source_event_ids=(event.event_id,),
                                reason="deterministic consolidation of low-access memories",
                            )
                            summary_memory_id = summary_memory.memory_id
                            summary_ids.append(summary_memory.memory_id)
                        else:
                            summary_memory_id = str(existing_summary["memory_id"])
                    self.memories._revise_connection(
                        connection,
                        memory.memory_id,
                        content=memory.content,
                        salience=memory.salience,
                        confidence=memory.confidence,
                        status="archived",
                        reason="inactive low-salience memory moved out of active recall",
                        source_event_ids=(*candidate.source_event_ids, event.event_id),
                        expected_revision=memory.current_revision,
                    )
                    archived.append(memory.memory_id)
                    active_count -= 1
                    dispositions.append(
                        (
                            memory.memory_id,
                            summary_memory_id,
                            "summarized",
                            "archived without deleting history",
                        )
                    )
                else:
                    dispositions.append(
                        (memory.memory_id, memory.memory_id, "retained", "retained")
                    )
            status = "committed" if archived or strengthened or summary_ids else "no_change"
            summary = (
                f"Reviewed {reviewed_count} memories; archived {len(archived)}, "
                f"strengthened {len(strengthened)}, created {len(summary_ids)} summaries."
            )
            consolidation_id = new_id("mcon")
            state_hash = self._state_hash(
                status,
                reviewed_count,
                tuple(archived),
                tuple(strengthened),
                tuple(summary_ids),
                summary,
                now,
            )
            connection.execute(
                """INSERT INTO memory_consolidation_runs(
                    consolidation_id, subject_id, idempotency_key, status, reviewed_count,
                    archived_memory_ids_json, strengthened_memory_ids_json,
                    summary_memory_ids_json, summary, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    consolidation_id,
                    self.subject_id,
                    key,
                    status,
                    reviewed_count,
                    canonical_json(archived),
                    canonical_json(strengthened),
                    canonical_json(summary_ids),
                    summary,
                    state_hash,
                    now,
                ),
            )
            for source_id, result_id, disposition, reason in dispositions:
                member_hash = content_hash(
                    {
                        "source_memory_id": source_id,
                        "result_memory_id": result_id,
                        "disposition": disposition,
                        "reason": reason,
                        "created_at": now,
                    }
                )
                connection.execute(
                    """INSERT INTO memory_consolidation_members(
                        member_id, consolidation_id, source_memory_id, result_memory_id,
                        disposition, reason, state_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_id("mconmember"),
                        consolidation_id,
                        source_id,
                        result_id,
                        disposition,
                        reason,
                        member_hash,
                        now,
                    ),
                )
        return f"memory_consolidation_{status}"

    @staticmethod
    def _state_hash(
        status: str,
        reviewed_count: int,
        archived: tuple[str, ...],
        strengthened: tuple[str, ...],
        summaries: tuple[str, ...],
        summary: str,
        created_at: str,
    ) -> str:
        return content_hash(
            {
                "status": status,
                "reviewed_count": reviewed_count,
                "archived_memory_ids": list(archived),
                "strengthened_memory_ids": list(strengthened),
                "summary_memory_ids": list(summaries),
                "summary": summary,
                "created_at": created_at,
            }
        )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("memory consolidation time requires a timezone")
        return parsed.astimezone(UTC)
