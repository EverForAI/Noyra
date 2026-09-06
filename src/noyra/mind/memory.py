from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Set
from datetime import UTC, datetime
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_finite_float,
    strict_int,
    strict_json_loads,
    utc_now,
)

from .errors import MindStateConflictError
from .retrieval import BoundedRetrievalConfig, HybridRetrievalWeights, MemoryEmbeddingIndex
from .types import MemoryConsolidationRecord, MemoryRecall, MemoryRecord, MemoryType
from .validation import validate_event_ids

MEMORY_STATUSES = frozenset({"active", "superseded", "archived"})
CONSOLIDATION_STATUSES = frozenset({"committed", "no_change"})
MEMORY_INTEGRATION_OPERATIONS = frozenset({"merge", "supersede", "contradict"})
MEMORY_INTEGRATION_STATUSES = frozenset({"active", "reverted"})
TOKEN_PATTERN = re.compile(r"[\w]+", re.UNICODE)
RETRIEVAL_MEMORY_TYPES = (
    "episodic",
    "autobiographical",
    "semantic",
    "procedural",
    "emotional",
    "relationship",
    "prediction",
    "reflection",
)


class MemoryStore:
    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], str] = utc_now,
        embedding_index: MemoryEmbeddingIndex | None = None,
        retrieval_weights: HybridRetrievalWeights | None = None,
        retrieval_config: BoundedRetrievalConfig | None = None,
    ):
        self.database = database
        self.clock = clock
        self.embedding_index = embedding_index
        self.retrieval_weights = retrieval_weights or HybridRetrievalWeights()
        self.retrieval_config = retrieval_config or BoundedRetrievalConfig()

    def create(
        self,
        subject_id: str,
        memory_type: MemoryType,
        content: str,
        *,
        salience: float,
        confidence: float,
        source_event_ids: tuple[str, ...],
        privacy_level: str = "private",
        reason: str = "memory formation",
    ) -> MemoryRecord:
        self._validate_fields(content, salience, confidence, privacy_level, reason)
        with self.database.transaction() as connection:
            return self._create_connection(
                connection,
                subject_id,
                memory_type,
                content,
                salience=salience,
                confidence=confidence,
                source_event_ids=source_event_ids,
                privacy_level=privacy_level,
                reason=reason,
            )

    def _create_connection(
        self,
        connection: Any,
        subject_id: str,
        memory_type: MemoryType,
        content: str,
        *,
        salience: float,
        confidence: float,
        source_event_ids: tuple[str, ...],
        privacy_level: str = "private",
        reason: str = "memory formation",
    ) -> MemoryRecord:
        self._validate_fields(content, salience, confidence, privacy_level, reason)
        sources = validate_event_ids(connection, subject_id, source_event_ids)
        memory_id = new_id("mem")
        now = self.clock()
        digest = content_hash(content)
        state_hash = self._state_hash(content, salience, confidence, "active")
        connection.execute(
            """INSERT INTO memories(
                memory_id, subject_id, memory_type, content, content_hash, state_hash,
                salience, confidence, privacy_level, status, current_revision,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)""",
            (
                memory_id,
                subject_id,
                memory_type,
                content,
                digest,
                state_hash,
                salience,
                confidence,
                privacy_level,
                now,
                now,
            ),
        )
        self._insert_revision(
            connection,
            memory_id,
            1,
            content,
            digest,
            salience,
            confidence,
            "active",
            reason,
            sources,
            now,
        )
        return self._load_connection(connection, memory_id)

    def revise(
        self,
        memory_id: str,
        *,
        content: str,
        salience: float,
        confidence: float,
        status: str,
        reason: str,
        source_event_ids: tuple[str, ...],
        expected_revision: int | None = None,
    ) -> MemoryRecord:
        self._validate_fields(content, salience, confidence, "private", reason)
        if status not in MEMORY_STATUSES:
            raise ValueError(f"invalid memory status: {status}")
        with self.database.transaction() as connection:
            return self._revise_connection(
                connection,
                memory_id,
                content=content,
                salience=salience,
                confidence=confidence,
                status=status,
                reason=reason,
                source_event_ids=source_event_ids,
                expected_revision=expected_revision,
            )

    def _revise_connection(
        self,
        connection: Any,
        memory_id: str,
        *,
        content: str,
        salience: float,
        confidence: float,
        status: str,
        reason: str,
        source_event_ids: tuple[str, ...],
        expected_revision: int | None = None,
    ) -> MemoryRecord:
        self._validate_fields(content, salience, confidence, "private", reason)
        if status not in MEMORY_STATUSES:
            raise ValueError(f"invalid memory status: {status}")
        row = self._get_row(connection, memory_id)
        current_revision = int(row["current_revision"])
        if expected_revision is not None and expected_revision != current_revision:
            raise MindStateConflictError("memory revision changed before update")
        sources = validate_event_ids(connection, row["subject_id"], source_event_ids)
        revision = current_revision + 1
        now = self.clock()
        digest = content_hash(content)
        state_hash = self._state_hash(content, salience, confidence, status)
        self._insert_revision(
            connection,
            memory_id,
            revision,
            content,
            digest,
            salience,
            confidence,
            status,
            reason,
            sources,
            now,
        )
        connection.execute(
            """UPDATE memories SET
                content = ?, content_hash = ?, state_hash = ?, salience = ?, confidence = ?,
                status = ?, current_revision = ?, updated_at = ?
                WHERE memory_id = ?""",
            (
                content,
                digest,
                state_hash,
                salience,
                confidence,
                status,
                revision,
                now,
                memory_id,
            ),
        )
        return self._load_connection(connection, memory_id)

    def get(self, memory_id: str) -> MemoryRecord:
        with self.database.connection() as connection:
            return self._load_connection(connection, memory_id)

    def search(
        self,
        subject_id: str,
        *,
        query: str = "",
        memory_type: str | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        bounded_limit = max(1, min(limit, 200))
        with self.database.connection() as connection:
            if memory_type is None:
                rows = connection.execute(
                    "SELECT * FROM memories WHERE subject_id = ? AND status = 'active' "
                    "ORDER BY salience DESC, confidence DESC, updated_at DESC LIMIT 1000",
                    (subject_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM memories WHERE subject_id = ? AND memory_type = ? "
                    "AND status = 'active' "
                    "ORDER BY salience DESC, confidence DESC, updated_at DESC LIMIT 1000",
                    (subject_id, memory_type),
                ).fetchall()
        records = [self._from_row(row) for row in rows]
        normalized_query = query.strip().casefold()
        if len(normalized_query) > 1_000:
            raise ValueError("memory search query is too long")
        if normalized_query:
            records = [
                record for record in records if normalized_query in record.content.casefold()
            ]
        return records[:bounded_limit]

    def recall(
        self,
        subject_id: str,
        query: str,
        *,
        context_type: str,
        context_id: str,
        memory_types: tuple[str, ...] = (),
        limit: int = 8,
        record_access: bool = True,
    ) -> list[MemoryRecall]:
        normalized_query = query.strip()
        if not normalized_query or len(normalized_query) > 4_000:
            raise ValueError("memory recall query must be nonblank and bounded")
        if not context_type.strip() or not context_id.strip():
            raise ValueError("memory recall context is required")
        bounded = max(1, min(limit, 32))
        now = self._parse_time(self.clock())
        query_tokens = self._tokens(normalized_query)
        fts_scores = self._fts_scores(subject_id, query_tokens)
        candidate_ids = self._candidate_memory_ids(subject_id, fts_scores)
        semantic_scores: dict[str, float] = {}
        if self.embedding_index is not None:
            try:
                semantic_scores = self.embedding_index.similarities(
                    subject_id,
                    normalized_query,
                    candidate_ids,
                )
            except Exception:
                semantic_scores = {}
        entity_scores = self._entity_scores(subject_id, query_tokens, candidate_ids)
        causal_scores = self._causal_scores(subject_id, query_tokens, candidate_ids)
        candidate_ids = self._rank_candidate_ids(
            candidate_ids,
            fts_scores,
            semantic_scores,
            entity_scores,
            causal_scores,
        )
        placeholders = ",".join("?" for _ in candidate_ids)
        allowed_types = set(memory_types)
        with self.database.connection() as connection:
            # Keep the IN-list as point lookups instead of scanning every memory for the subject.
            query = (
                "SELECT m.*, COALESCE(SUM(a.access_count), 0) AS total_access_count, "
                "MAX(a.last_accessed_at) AS last_accessed_at "
                "FROM memories m INDEXED BY sqlite_autoindex_memories_1 "
                "LEFT JOIN memory_accesses a ON a.memory_id = m.memory_id "
                "WHERE m.subject_id = ? AND m.status = 'active' "
                f"AND m.memory_id IN ({placeholders})"
            )
            params: list[Any] = [subject_id, *candidate_ids]
            if allowed_types:
                type_placeholders = ",".join("?" for _ in allowed_types)
                query += f" AND m.memory_type IN ({type_placeholders})"
                params.extend(sorted(allowed_types))
            query += " GROUP BY m.memory_id"
            rows = connection.execute(query, params).fetchall()
        weights = self.retrieval_weights
        recalls: list[MemoryRecall] = []
        for row in rows:
            memory = self._from_row(row)
            memory_tokens = self._tokens(memory.content)
            lexical = max(
                self._lexical_score(query_tokens, memory_tokens, normalized_query, memory.content),
                fts_scores.get(memory.memory_id, 0.0),
            )
            age_days = max(
                0.0, (now - self._parse_time(memory.updated_at)).total_seconds() / 86_400
            )
            recency = 1 / (1 + age_days / 30)
            access_count = int(row["total_access_count"])
            access_score = min(1.0, access_count / 12)
            semantic = semantic_scores.get(memory.memory_id, 0.0)
            entity = entity_scores.get(memory.memory_id, 0.0)
            causal = causal_scores.get(memory.memory_id, 0.0)
            temporal = recency
            relevance = min(
                1.0,
                lexical * weights.lexical
                + semantic * weights.semantic
                + memory.salience * weights.salience
                + memory.confidence * weights.confidence
                + recency * weights.recency
                + access_score * weights.access
                + entity * weights.entity
                + temporal * weights.temporal
                + causal * weights.causal,
            )
            if lexical <= 0 and semantic <= 0 and relevance < 0.22:
                continue
            recalls.append(
                MemoryRecall(
                    memory,
                    relevance,
                    lexical,
                    memory.salience,
                    memory.confidence,
                    recency,
                    access_score,
                    semantic,
                    entity,
                    temporal,
                    causal,
                )
            )
        recalls.sort(
            key=lambda item: (
                -item.relevance,
                -item.memory.salience,
                -item.memory.confidence,
                item.memory.memory_id,
            )
        )
        selected = recalls[:bounded]
        if record_access and selected:
            self._record_accesses(
                subject_id,
                selected,
                context_type=context_type,
                context_id=context_id,
                query_hash=content_hash(normalized_query.casefold()),
            )
        return selected

    def _candidate_memory_ids(
        self,
        subject_id: str,
        fts_scores: dict[str, float],
    ) -> list[str]:
        config = self.retrieval_config
        ranked = [
            memory_id
            for memory_id, _ in sorted(
                fts_scores.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ][: config.candidate_limit]
        remaining = min(
            config.fallback_candidate_limit,
            config.candidate_limit - len(ranked),
        )
        if remaining <= 0:
            return ranked
        buckets: list[list[str]] = []
        with self.database.connection() as connection:
            for memory_type in RETRIEVAL_MEMORY_TYPES:
                rows = connection.execute(
                    "SELECT memory_id FROM memories INDEXED BY idx_memories_subject_type "
                    "WHERE subject_id = ? AND memory_type = ? AND status = 'active' "
                    "ORDER BY rowid DESC LIMIT ?",
                    (subject_id, memory_type, remaining),
                ).fetchall()
                buckets.append([str(row["memory_id"]) for row in rows])
        fallback_ids: list[str] = []
        for offset in range(remaining):
            for bucket in buckets:
                if offset < len(bucket):
                    fallback_ids.append(bucket[offset])
                    if len(fallback_ids) >= remaining:
                        break
            if len(fallback_ids) >= remaining:
                break
        return list(dict.fromkeys([*ranked, *fallback_ids]))[: config.candidate_limit]

    def _rank_candidate_ids(
        self, candidate_ids: list[str], *score_maps: dict[str, float]
    ) -> list[str]:
        scores = {
            memory_id: max((mapping.get(memory_id, 0.0) for mapping in score_maps), default=0.0)
            for memory_id in candidate_ids
        }
        return sorted(candidate_ids, key=lambda memory_id: (-scores[memory_id], memory_id))[
            : self.retrieval_config.candidate_limit
        ]

    def _fts_scores(self, subject_id: str, query_tokens: Set[str]) -> dict[str, float]:
        if not query_tokens:
            return {}
        expression = " OR ".join(
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in sorted(query_tokens)
        )
        limit = min(
            self.retrieval_config.fts_candidate_limit,
            self.retrieval_config.candidate_limit,
        )
        if limit <= 0:
            return {}
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    "SELECT memory_id, bm25(memory_fts) AS rank FROM memory_fts "
                    "WHERE memory_fts MATCH ? AND subject_id = ? ORDER BY rank LIMIT ?",
                    (expression, subject_id, limit),
                ).fetchall()
        except Exception:
            return {}
        total = max(1, len(rows))
        return {
            str(row["memory_id"]): max(0.0, 1.0 - index / total) for index, row in enumerate(rows)
        }

    def _entity_scores(
        self, subject_id: str, query_tokens: Set[str], memory_ids: list[str]
    ) -> dict[str, float]:
        if not query_tokens or not memory_ids:
            return {}
        with self.database.connection() as connection:
            placeholders = ",".join("?" for _ in memory_ids)
            rows = connection.execute(
                "SELECT e.entity_id, e.display_name, e.canonical_key, e.aliases_json, "
                "e.confidence, l.source_id AS memory_id FROM entities e "
                "JOIN entity_evidence_links l ON l.entity_id = e.entity_id "
                f"WHERE e.subject_id = ? AND l.subject_id = ? AND l.source_type = 'memory' "
                f"AND l.source_id IN ({placeholders})",
                (subject_id, subject_id, *memory_ids),
            ).fetchall()
        scores: dict[str, float] = {}
        for row in rows:
            aliases = json.loads(row["aliases_json"])
            text = " ".join(
                [str(row["display_name"]), str(row["canonical_key"])]
                + ([str(value) for value in aliases] if isinstance(aliases, list) else [])
            )
            overlap = query_tokens & self._tokens(text)
            if overlap:
                score = min(1.0, len(overlap) / len(query_tokens)) * float(row["confidence"])
                memory_id = str(row["memory_id"])
                scores[memory_id] = max(scores.get(memory_id, 0.0), score)
        return scores

    def _causal_scores(
        self, subject_id: str, query_tokens: Set[str], memory_ids: list[str]
    ) -> dict[str, float]:
        if not query_tokens or not memory_ids:
            return {}
        with self.database.connection() as connection:
            placeholders = ",".join("?" for _ in memory_ids)
            revisions = connection.execute(
                "SELECT r.memory_id, r.source_event_ids_json FROM memory_revisions r "
                "JOIN memories m ON m.memory_id = r.memory_id "
                "WHERE m.subject_id = ? AND m.status = 'active' "
                f"AND r.revision_number = m.current_revision AND r.memory_id IN ({placeholders})",
                (subject_id, *memory_ids),
            ).fetchall()
            source_ids: set[str] = set()
            for revision in revisions:
                sources = json.loads(revision["source_event_ids_json"])
                if isinstance(sources, list):
                    source_ids.update(str(item) for item in sources[:32])
            event_map: dict[str, Any] = {}
            for chunk in self._chunks(sorted(source_ids), 400):
                event_placeholders = ",".join("?" for _ in chunk)
                events = connection.execute(
                    "SELECT event_id, payload_json, causal_parent_ids_json FROM events "
                    f"WHERE subject_id = ? AND event_id IN ({event_placeholders})",
                    (subject_id, *chunk),
                ).fetchall()
                event_map.update({str(row["event_id"]): row for row in events})
            parent_ids: set[str] = set()
            for event in event_map.values():
                parents = json.loads(event["causal_parent_ids_json"])
                if isinstance(parents, list):
                    parent_ids.update(str(item) for item in parents[:32])
            for chunk in self._chunks(sorted(parent_ids - event_map.keys()), 400):
                event_placeholders = ",".join("?" for _ in chunk)
                events = connection.execute(
                    "SELECT event_id, payload_json, causal_parent_ids_json FROM events "
                    f"WHERE subject_id = ? AND event_id IN ({event_placeholders})",
                    (subject_id, *chunk),
                ).fetchall()
                event_map.update({str(row["event_id"]): row for row in events})
        scores: dict[str, float] = {}
        for revision in revisions:
            source_ids = json.loads(revision["source_event_ids_json"])
            if not isinstance(source_ids, list):
                continue
            best = 0.0
            for event_id in source_ids:
                event = event_map.get(str(event_id))
                if event is None:
                    continue
                payload_tokens = self._tokens(str(event["payload_json"]))
                if query_tokens & payload_tokens:
                    best = max(best, 1.0)
                parents = json.loads(event["causal_parent_ids_json"])
                if isinstance(parents, list):
                    for parent_id in parents:
                        parent = event_map.get(str(parent_id))
                        if parent is not None and query_tokens & self._tokens(
                            str(parent["payload_json"])
                        ):
                            best = max(best, 0.7)
            if best:
                scores[str(revision["memory_id"])] = best
        return scores

    @staticmethod
    def _chunks(values: list[str], size: int) -> list[list[str]]:
        return [values[index : index + size] for index in range(0, len(values), size)]

    def access_history(self, memory_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            self._get_row(connection, memory_id)
            rows = connection.execute(
                "SELECT * FROM memory_accesses WHERE memory_id = ? "
                "ORDER BY last_accessed_at DESC, access_id DESC",
                (memory_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def consolidation_history(self, subject_id: str) -> list[MemoryConsolidationRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_consolidation_runs WHERE subject_id = ? "
                "ORDER BY created_at DESC, consolidation_id DESC",
                (subject_id,),
            ).fetchall()
        return [self._consolidation_from_row(row) for row in rows]

    def verify_lifecycle_integrity(self, subject_id: str) -> dict[str, int]:
        with self.database.read_transaction() as connection:
            access_rows = connection.execute(
                "SELECT * FROM memory_accesses WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for row in access_rows:
                context = f"memory access {row['access_id']}"
                relevance = self._persisted_float(
                    row["relevance"], f"{context} relevance", minimum=0.0, maximum=1.0
                )
                access_count = self._persisted_integer(
                    row["access_count"], f"{context} access_count", minimum=1
                )
                expected = self._access_hash(
                    row["context_type"],
                    row["context_id"],
                    row["query_hash"],
                    relevance,
                    access_count,
                    row["first_accessed_at"],
                    row["last_accessed_at"],
                )
                if expected != row["state_hash"]:
                    raise IntegrityError(f"memory access hash mismatch: {row['access_id']}")
            run_rows = connection.execute(
                "SELECT * FROM memory_consolidation_runs WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for row in run_rows:
                record = self._consolidation_from_row(row)
                members = connection.execute(
                    "SELECT * FROM memory_consolidation_members WHERE consolidation_id = ?",
                    (record.consolidation_id,),
                ).fetchall()
                for member in members:
                    expected = content_hash(
                        {
                            "source_memory_id": member["source_memory_id"],
                            "result_memory_id": member["result_memory_id"],
                            "disposition": member["disposition"],
                            "reason": member["reason"],
                            "created_at": member["created_at"],
                        }
                    )
                    if expected != member["state_hash"]:
                        raise IntegrityError(
                            f"memory consolidation member mismatch: {member['member_id']}"
                        )
            integration_count, integration_revision_count = self._verify_memory_integrations(
                connection, subject_id
            )
        return {
            "memory_accesses": len(access_rows),
            "memory_consolidations": len(run_rows),
            "memory_integrations": integration_count,
            "memory_integration_revisions": integration_revision_count,
        }

    def source_event_ids(self, memory_id: str) -> tuple[str, ...]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT source_event_ids_json FROM memory_revisions WHERE memory_id = ? "
                "ORDER BY revision_number DESC LIMIT 1",
                (memory_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"memory not found: {memory_id}")
        value = json.loads(row["source_event_ids_json"])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise IntegrityError(f"memory sources are invalid: {memory_id}")
        return tuple(value)

    def revisions(self, memory_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            self._get_row(connection, memory_id)
            rows = connection.execute(
                "SELECT * FROM memory_revisions WHERE memory_id = ? ORDER BY revision_number",
                (memory_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _record_accesses(
        self,
        subject_id: str,
        recalls: Iterable[MemoryRecall],
        *,
        context_type: str,
        context_id: str,
        query_hash: str,
    ) -> None:
        now = self.clock()
        with self.database.transaction() as connection:
            for recall in recalls:
                row = connection.execute(
                    """SELECT * FROM memory_accesses WHERE subject_id = ? AND memory_id = ?
                       AND context_type = ? AND context_id = ? AND query_hash = ?""",
                    (
                        subject_id,
                        recall.memory.memory_id,
                        context_type,
                        context_id,
                        query_hash,
                    ),
                ).fetchone()
                access_count = 1 if row is None else int(row["access_count"]) + 1
                first = now if row is None else str(row["first_accessed_at"])
                state_hash = self._access_hash(
                    context_type,
                    context_id,
                    query_hash,
                    recall.relevance,
                    access_count,
                    first,
                    now,
                )
                if row is None:
                    connection.execute(
                        """INSERT INTO memory_accesses(
                            access_id, subject_id, memory_id, context_type, context_id,
                            query_hash, relevance, access_count, state_hash,
                            first_accessed_at, last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                        (
                            new_id("macc"),
                            subject_id,
                            recall.memory.memory_id,
                            context_type,
                            context_id,
                            query_hash,
                            recall.relevance,
                            state_hash,
                            first,
                            now,
                        ),
                    )
                else:
                    connection.execute(
                        """UPDATE memory_accesses SET relevance = ?, access_count = ?,
                            state_hash = ?, last_accessed_at = ? WHERE access_id = ?""",
                        (recall.relevance, access_count, state_hash, now, row["access_id"]),
                    )

    @staticmethod
    def _validate_fields(
        content: str, salience: float, confidence: float, privacy_level: str, reason: str
    ) -> None:
        if not content.strip() or not privacy_level.strip() or not reason.strip():
            raise ValueError("memory content, privacy level and reason are required")
        if len(content) > 200_000 or len(reason) > 10_000 or len(privacy_level) > 64:
            raise ValueError("memory fields exceed their storage limits")
        if not 0 <= salience <= 1 or not 0 <= confidence <= 1:
            raise ValueError("memory salience and confidence must be between zero and one")

    @staticmethod
    def _tokens(value: str) -> frozenset[str]:
        return frozenset(
            token.casefold() for token in TOKEN_PATTERN.findall(value) if len(token) > 1
        )

    @staticmethod
    def _lexical_score(
        query_tokens: frozenset[str],
        memory_tokens: frozenset[str],
        query: str,
        content: str,
    ) -> float:
        if query.casefold() in content.casefold():
            return 1.0
        if not query_tokens or not memory_tokens:
            return 0.0
        intersection = len(query_tokens & memory_tokens)
        return min(1.0, intersection / max(1, len(query_tokens)))

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("memory time requires a timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _access_hash(
        context_type: str,
        context_id: str,
        query_hash: str,
        relevance: float,
        access_count: int,
        first_accessed_at: str,
        last_accessed_at: str,
    ) -> str:
        return content_hash(
            {
                "context_type": context_type,
                "context_id": context_id,
                "query_hash": query_hash,
                "relevance": float(relevance),
                "access_count": access_count,
                "first_accessed_at": first_accessed_at,
                "last_accessed_at": last_accessed_at,
            }
        )

    @staticmethod
    def _consolidation_from_row(row: Any) -> MemoryConsolidationRecord:
        context = f"memory consolidation {row['consolidation_id']}"
        status = MemoryStore._persisted_consolidation_status(row["status"], context)
        reviewed_count = MemoryStore._persisted_integer(
            row["reviewed_count"], f"{context} reviewed_count", minimum=0
        )
        archived = MemoryStore._persisted_json_strings(
            row["archived_memory_ids_json"], f"{context} archived memory ids"
        )
        strengthened = MemoryStore._persisted_json_strings(
            row["strengthened_memory_ids_json"], f"{context} strengthened memory ids"
        )
        summaries = MemoryStore._persisted_json_strings(
            row["summary_memory_ids_json"], f"{context} summary memory ids"
        )
        expected = content_hash(
            {
                "status": status,
                "reviewed_count": reviewed_count,
                "archived_memory_ids": list(archived),
                "strengthened_memory_ids": list(strengthened),
                "summary_memory_ids": list(summaries),
                "summary": row["summary"],
                "created_at": row["created_at"],
            }
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"memory consolidation hash mismatch: {row['consolidation_id']}")
        return MemoryConsolidationRecord(
            row["consolidation_id"],
            row["subject_id"],
            status,
            reviewed_count,
            archived,
            strengthened,
            summaries,
            row["summary"],
            row["created_at"],
        )

    @classmethod
    def _verify_memory_integrations(cls, connection: Any, subject_id: str) -> tuple[int, int]:
        rows = connection.execute(
            "SELECT * FROM memory_integrations WHERE subject_id = ? "
            "ORDER BY created_at, integration_id",
            (subject_id,),
        ).fetchall()
        revision_count = 0
        for row in rows:
            integration_id = cls._persisted_text(row["integration_id"], "memory integration id")
            context = f"memory integration {integration_id}"
            persisted_subject = cls._persisted_text(row["subject_id"], f"{context} subject")
            if persisted_subject != subject_id:
                raise IntegrityError(f"{context} ownership is invalid")
            operation = row["operation"]
            status = row["status"]
            if (
                not isinstance(operation, str)
                or operation not in MEMORY_INTEGRATION_OPERATIONS
                or not isinstance(status, str)
                or status not in MEMORY_INTEGRATION_STATUSES
            ):
                raise IntegrityError(f"{context} state is invalid")
            sources = cls._persisted_json_strings(
                row["source_memory_ids_json"], f"{context} source memory ids"
            )
            evidence = cls._persisted_json_strings(
                row["evidence_event_ids_json"], f"{context} evidence event ids"
            )
            if len(sources) < 2 or not evidence:
                raise IntegrityError(f"{context} references are invalid")
            output_memory_id = cls._persisted_text(
                row["output_memory_id"], f"{context} output memory id"
            )
            if output_memory_id in sources:
                raise IntegrityError(f"{context} output aliases a source memory")
            reason = cls._persisted_text(row["reason"], f"{context} reason")
            confidence = cls._persisted_float(
                row["confidence"], f"{context} confidence", minimum=0.0, maximum=1.0
            )
            created_at = cls._persisted_timestamp(row["created_at"], f"{context} created_at")
            reverted_at_raw = row["reverted_at"]
            reverted_at = (
                None
                if reverted_at_raw is None
                else cls._persisted_timestamp(reverted_at_raw, f"{context} reverted_at")
            )
            if (status == "active") != (reverted_at is None):
                raise IntegrityError(f"{context} lifecycle is invalid")
            state_hash = row["state_hash"]
            if not cls._valid_hash(state_hash):
                raise IntegrityError(f"{context} state hash is invalid")
            expected_state = content_hash(
                {
                    "operation": operation,
                    "source_memory_ids": list(sources),
                    "output_memory_id": output_memory_id,
                    "evidence_event_ids": list(evidence),
                    "reason": reason,
                    "confidence": confidence,
                    "status": status,
                }
            )
            if state_hash != expected_state:
                raise IntegrityError(f"{context} state hash mismatch")

            cls._verify_owned_ids(
                connection,
                "memories",
                "memory_id",
                subject_id,
                sources,
                f"{context} source memory",
            )
            cls._verify_owned_ids(
                connection,
                "memories",
                "memory_id",
                subject_id,
                (output_memory_id,),
                f"{context} output memory",
            )
            cls._verify_owned_ids(
                connection,
                "events",
                "event_id",
                subject_id,
                evidence,
                f"{context} evidence event",
            )

            revisions = connection.execute(
                "SELECT * FROM memory_integration_revisions WHERE integration_id = ? "
                "ORDER BY created_at, rowid",
                (integration_id,),
            ).fetchall()
            expected_actions = ("commit",) if status == "active" else ("commit", "revert")
            if len(revisions) != len(expected_actions):
                raise IntegrityError(f"{context} revision history is invalid")
            for index, (revision, expected_action) in enumerate(
                zip(revisions, expected_actions, strict=True)
            ):
                revision_id = cls._persisted_text(revision["revision_id"], f"{context} revision id")
                revision_context = f"memory integration revision {revision_id}"
                if (
                    cls._persisted_text(
                        revision["integration_id"], f"{revision_context} integration id"
                    )
                    != integration_id
                ):
                    raise IntegrityError(f"{revision_context} ownership is invalid")
                action = revision["action"]
                if not isinstance(action, str) or action != expected_action:
                    raise IntegrityError(f"{context} revision sequence is invalid")
                revision_reason = cls._persisted_text(
                    revision["reason"], f"{revision_context} reason"
                )
                revision_created_at = cls._persisted_timestamp(
                    revision["created_at"], f"{revision_context} created_at"
                )
                revision_hash = revision["state_hash"]
                if not cls._valid_hash(revision_hash) or revision_hash != content_hash(
                    {
                        "integration_id": integration_id,
                        "action": action,
                        "reason": revision_reason,
                        "created_at": revision_created_at,
                    }
                ):
                    raise IntegrityError(f"{revision_context} hash mismatch")
                if index == 0 and (revision_reason != reason or revision_created_at != created_at):
                    raise IntegrityError(f"{context} commit revision is invalid")
                if action == "revert" and revision_created_at != reverted_at:
                    raise IntegrityError(f"{context} revert revision is invalid")
            revision_count += len(revisions)
        return len(rows), revision_count

    @staticmethod
    def _persisted_float(
        value: Any,
        context: str,
        *,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            parsed = strict_finite_float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"{context} is invalid") from error
        if not minimum <= parsed <= maximum:
            raise IntegrityError(f"{context} is invalid")
        return parsed

    @staticmethod
    def _persisted_integer(value: Any, context: str, *, minimum: int) -> int:
        try:
            parsed = strict_int(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"{context} is invalid") from error
        if parsed < minimum:
            raise IntegrityError(f"{context} is invalid")
        return parsed

    @staticmethod
    def _persisted_json_strings(value: Any, context: str) -> tuple[str, ...]:
        if not isinstance(value, str):
            raise IntegrityError(f"{context} JSON is invalid")
        try:
            parsed = strict_json_loads(value)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"{context} JSON is invalid") from error
        if (
            not isinstance(parsed, list)
            or not all(isinstance(item, str) for item in parsed)
            or len(set(parsed)) != len(parsed)
        ):
            raise IntegrityError(f"{context} list is invalid")
        return tuple(parsed)

    @staticmethod
    def _persisted_text(value: Any, context: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise IntegrityError(f"{context} is invalid")
        return value

    @classmethod
    def _persisted_timestamp(cls, value: Any, context: str) -> str:
        parsed = cls._persisted_text(value, context)
        try:
            cls._parse_time(parsed)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"{context} is invalid") from error
        return parsed

    @staticmethod
    def _valid_hash(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @classmethod
    def _verify_owned_ids(
        cls,
        connection: Any,
        table: str,
        key: str,
        subject_id: str,
        values: tuple[str, ...],
        context: str,
    ) -> None:
        found: set[str] = set()
        for chunk in cls._chunks(list(values), 500):
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"SELECT {key}, subject_id FROM {table} WHERE {key} IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for row in rows:
                if row["subject_id"] != subject_id:
                    raise IntegrityError(f"{context} crosses a subject boundary")
                found.add(str(row[key]))
        if found != set(values):
            raise IntegrityError(f"{context} is missing or invalid")

    @staticmethod
    def _persisted_consolidation_status(value: Any, context: str) -> str:
        if not isinstance(value, str) or value not in CONSOLIDATION_STATUSES:
            raise IntegrityError(f"{context} status is invalid")
        return value

    @staticmethod
    def _insert_revision(
        connection: Any,
        memory_id: str,
        revision: int,
        content: str,
        digest: str,
        salience: float,
        confidence: float,
        status: str,
        reason: str,
        sources: tuple[str, ...],
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO memory_revisions(
                revision_id, memory_id, revision_number, content, content_hash, state_hash,
                salience, confidence, status, reason, source_event_ids_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("mrev"),
                memory_id,
                revision,
                content,
                digest,
                MemoryStore._state_hash(content, salience, confidence, status),
                salience,
                confidence,
                status,
                reason,
                canonical_json(list(sources)),
                created_at,
            ),
        )

    @staticmethod
    def _get_row(connection: Any, memory_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"memory not found: {memory_id}")
        return row

    @classmethod
    def _load_connection(cls, connection: Any, memory_id: str) -> MemoryRecord:
        return cls._from_row(cls._get_row(connection, memory_id))

    @staticmethod
    def _from_row(row: Any) -> MemoryRecord:
        memory_id = row["memory_id"]
        context = f"memory {memory_id}"
        if content_hash(row["content"]) != row["content_hash"]:
            raise IntegrityError(f"memory content hash mismatch: {memory_id}")
        salience = MemoryStore._persisted_float(
            row["salience"], f"{context} salience", minimum=0.0, maximum=1.0
        )
        confidence = MemoryStore._persisted_float(
            row["confidence"], f"{context} confidence", minimum=0.0, maximum=1.0
        )
        revision = MemoryStore._persisted_integer(
            row["current_revision"], f"{context} current_revision", minimum=1
        )
        status = row["status"]
        if not isinstance(status, str) or status not in MEMORY_STATUSES:
            raise IntegrityError(f"{context} status is invalid")
        expected_state = MemoryStore._state_hash(
            row["content"],
            salience,
            confidence,
            status,
        )
        if expected_state != row["state_hash"]:
            raise IntegrityError(f"memory state hash mismatch: {memory_id}")
        return MemoryRecord(
            memory_id=memory_id,
            subject_id=row["subject_id"],
            memory_type=row["memory_type"],
            content=row["content"],
            content_hash=row["content_hash"],
            salience=salience,
            confidence=confidence,
            privacy_level=row["privacy_level"],
            status=status,
            current_revision=revision,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _state_hash(content: str, salience: float, confidence: float, status: str) -> str:
        return content_hash(
            {
                "content": content,
                "salience": float(salience),
                "confidence": float(confidence),
                "status": status,
            }
        )
