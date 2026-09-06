from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, strict_int, strict_json_loads, utc_now

MAX_SEMANTIC_CANDIDATES = 768
MAX_EMBEDDING_DIMENSIONS = 16_384
MAX_VECTOR_JSON_BYTES = 1_048_576


class EmbeddingProvider(Protocol):
    @property
    def name(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class HybridRetrievalWeights:
    lexical: float = 0.35
    semantic: float = 0.25
    salience: float = 0.12
    confidence: float = 0.08
    recency: float = 0.08
    access: float = 0.03
    entity: float = 0.04
    temporal: float = 0.02
    causal: float = 0.03

    def __post_init__(self) -> None:
        values = tuple(self.__dict__.values())
        if any(value < 0 for value in values) or not math.isclose(sum(values), 1.0):
            raise ValueError("hybrid retrieval weights must be nonnegative and sum to one")


@dataclass(frozen=True)
class BoundedRetrievalConfig:
    candidate_limit: int = MAX_SEMANTIC_CANDIDATES
    fts_candidate_limit: int = 512
    fallback_candidate_limit: int = 256

    def __post_init__(self) -> None:
        values = (
            self.candidate_limit,
            self.fts_candidate_limit,
            self.fallback_candidate_limit,
        )
        if any(type(value) is not int for value in values):
            raise ValueError("retrieval candidate limits must be integers")
        if not 1 <= self.candidate_limit <= MAX_SEMANTIC_CANDIDATES:
            raise ValueError("retrieval candidate limit is out of bounds")
        if not 0 <= self.fts_candidate_limit <= MAX_SEMANTIC_CANDIDATES:
            raise ValueError("FTS candidate limit is out of bounds")
        if not 0 <= self.fallback_candidate_limit <= MAX_SEMANTIC_CANDIDATES:
            raise ValueError("fallback candidate limit is out of bounds")
        if self.fts_candidate_limit == 0 and self.fallback_candidate_limit == 0:
            raise ValueError("retrieval requires at least one candidate source")


class MemoryEmbeddingIndex:
    """Rebuildable SQLite vector index; it never owns memory content."""

    def __init__(
        self,
        database: Database,
        provider: EmbeddingProvider,
        *,
        clock: Callable[[], str] = utc_now,
    ):
        self.database = database
        self.provider = provider
        self.clock = clock

    def index_memory(self, subject_id: str, memory_id: str, content: str) -> None:
        vectors = self.provider.embed((content,))
        if len(vectors) != 1:
            raise ValueError("embedding provider returned an invalid batch")
        vector = self._validate_vector(vectors[0])
        payload = canonical_json(vector)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT subject_id, content_hash FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            if row is None or row["subject_id"] != subject_id:
                raise ValueError("memory does not belong to subject")
            connection.execute(
                """INSERT INTO memory_embeddings(
                   memory_id, subject_id, provider, dimensions, vector_json, vector_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET provider = excluded.provider,
                   dimensions = excluded.dimensions, vector_json = excluded.vector_json,
                   vector_hash = excluded.vector_hash, updated_at = excluded.updated_at""",
                (
                    memory_id,
                    subject_id,
                    self.provider.name,
                    len(vector),
                    payload,
                    content_hash(vector),
                    self.clock(),
                ),
            )

    def missing(self, subject_id: str, *, limit: int = 128) -> list[tuple[str, str]]:
        bounded = max(1, min(limit, 2_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT m.memory_id, m.content FROM memories m
                   LEFT JOIN memory_embeddings e ON e.memory_id = m.memory_id
                   WHERE m.subject_id = ? AND m.status = 'active'
                     AND (e.memory_id IS NULL OR e.provider != ?)
                   ORDER BY m.updated_at DESC LIMIT ?""",
                (subject_id, self.provider.name, bounded),
            ).fetchall()
        return [(str(row["memory_id"]), str(row["content"])) for row in rows]

    def rebuild_missing(
        self,
        subject_id: str,
        *,
        limit: int = 128,
        checkpoint: Callable[[], None] | None = None,
    ) -> int:
        if checkpoint is not None:
            checkpoint()
        records = self.missing(subject_id, limit=limit)
        if not records:
            return 0
        vectors = self.provider.embed(tuple(content for _, content in records))
        if checkpoint is not None:
            checkpoint()
        if len(vectors) != len(records):
            raise ValueError("embedding provider returned an invalid batch")
        now = self.clock()
        with self.database.transaction() as connection:
            for (memory_id, _), raw_vector in zip(records, vectors, strict=True):
                if checkpoint is not None:
                    checkpoint()
                vector = self._validate_vector(raw_vector)
                connection.execute(
                    """INSERT INTO memory_embeddings(
                       memory_id, subject_id, provider, dimensions, vector_json,
                       vector_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id) DO UPDATE SET provider = excluded.provider,
                       dimensions = excluded.dimensions, vector_json = excluded.vector_json,
                       vector_hash = excluded.vector_hash, updated_at = excluded.updated_at""",
                    (
                        memory_id,
                        subject_id,
                        self.provider.name,
                        len(vector),
                        canonical_json(vector),
                        content_hash(vector),
                        now,
                    ),
                )
        return len(records)

    def similarities(
        self,
        subject_id: str,
        query: str,
        candidate_memory_ids: Iterable[str],
    ) -> dict[str, float]:
        candidate_ids = self._bounded_candidate_ids(candidate_memory_ids)
        if not candidate_ids:
            return {}
        vectors = self.provider.embed((query,))
        if len(vectors) != 1:
            return {}
        query_vector = self._validate_vector(vectors[0])
        scores: dict[str, float] = {}
        placeholders = ",".join("?" for _ in candidate_ids)
        with self.database.connection() as connection:
            # SQLite otherwise favors the subject/provider index and scans the full corpus.
            rows = connection.execute(
                "SELECT memory_id, dimensions, vector_json, vector_hash "
                "FROM memory_embeddings INDEXED BY sqlite_autoindex_memory_embeddings_1 "
                "WHERE subject_id = ? AND provider = ? "
                f"AND memory_id IN ({placeholders}) "
                "AND dimensions BETWEEN 1 AND ? "
                "AND length(CAST(vector_json AS BLOB)) <= ? "
                "AND length(vector_hash) = 64",
                (
                    subject_id,
                    self.provider.name,
                    *candidate_ids,
                    MAX_EMBEDDING_DIMENSIONS,
                    MAX_VECTOR_JSON_BYTES,
                ),
            )
            for row in rows:
                try:
                    dimensions = strict_int(row["dimensions"])
                    vector = self._validate_vector(strict_json_loads(row["vector_json"]))
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    len(vector) != dimensions
                    or dimensions != len(query_vector)
                    or content_hash(vector) != row["vector_hash"]
                ):
                    continue
                scores[str(row["memory_id"])] = max(0.0, self.cosine(query_vector, vector))
        return scores

    @staticmethod
    def _bounded_candidate_ids(values: Iterable[str]) -> tuple[str, ...]:
        ordered: list[str] = []
        seen: set[str] = set()
        for index, value in enumerate(values):
            if index >= MAX_SEMANTIC_CANDIDATES:
                break
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError("semantic candidate memory id is invalid")
            if value not in seen:
                seen.add(value)
                ordered.append(value)
        return tuple(ordered)

    def verify_integrity(self, subject_id: str) -> int:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_embeddings WHERE subject_id = ?", (subject_id,)
            ).fetchall()
        for row in rows:
            context = f"memory embedding {row['memory_id']}"
            raw_vector = row["vector_json"]
            if not isinstance(raw_vector, str):
                raise IntegrityError(f"{context} vector JSON is invalid")
            try:
                decoded = strict_json_loads(raw_vector)
            except (TypeError, ValueError, UnicodeError) as error:
                raise IntegrityError(f"{context} vector JSON is invalid") from error
            try:
                vector = self._validate_vector(decoded)
            except (TypeError, ValueError, OverflowError) as error:
                raise IntegrityError(f"{context} vector is invalid") from error
            dimensions = self._persisted_dimensions(row["dimensions"], context)
            if len(vector) != dimensions or content_hash(vector) != row["vector_hash"]:
                raise IntegrityError(f"memory embedding mismatch: {row['memory_id']}")
        return len(rows)

    @staticmethod
    def _persisted_dimensions(value: Any, context: str) -> int:
        try:
            dimensions = strict_int(value)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"{context} dimensions are invalid") from error
        if dimensions <= 0:
            raise IntegrityError(f"{context} dimensions are invalid")
        return dimensions

    @staticmethod
    def cosine(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)

    @staticmethod
    def _validate_vector(value: Any) -> list[float]:
        if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_EMBEDDING_DIMENSIONS:
            raise ValueError("embedding vector has invalid dimensions")
        vector = [float(item) for item in value]
        if any(not math.isfinite(item) for item in vector):
            raise ValueError("embedding vector contains non-finite values")
        return vector
