from __future__ import annotations

import json
import random
import sqlite3
import tempfile
import tracemalloc
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, TypedDict, cast
from unittest.mock import patch

import noyra.mind.retrieval as retrieval_module
from noyra.core import Database, EventStore, IdentityStore
from noyra.core.types import canonical_json, content_hash, strict_json_loads
from noyra.mind import (
    BoundedRetrievalConfig,
    HybridRetrievalWeights,
    MemoryEmbeddingIndex,
    MemoryStore,
)


class TopicFixture(TypedDict):
    content_term: str
    query_term: str


class RetrievalFixture(TypedDict):
    version: int
    seed: int
    topics: list[TopicFixture]
    memories_per_topic: int
    candidate_limits: list[int]
    recall_at: int
    minimum_recall_curve: list[float]
    approved_candidate_limit: int
    approved_recall_floor: float
    large_profile_embeddings: int
    large_profile_candidate_limit: int
    large_profile_max_selects: int
    large_profile_max_json_decodes: int
    large_profile_max_peak_bytes: int
    large_profile_recall_candidate_limit: int
    large_profile_recall_selects: int


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "synthetic" / "p1_09_retrieval_quality.json"


def _load_fixture() -> RetrievalFixture:
    raw: object = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AssertionError("P1-09 retrieval fixture must be an object")
    return cast(RetrievalFixture, raw)


class TrackingDatabase(Database):
    def __init__(self, path: Path | str):
        self.capture_statements = False
        self.statements: list[str] = []
        super().__init__(path)

    def _connect(self) -> sqlite3.Connection:
        connection = super()._connect()
        if self.capture_statements:
            connection.set_trace_callback(self.statements.append)
        return connection


class TopicEmbedding:
    name = "p1-09-topic-embedding"

    def __init__(self, topics: Sequence[TopicFixture]):
        self.topics = tuple(topics)
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(tuple(texts))
        vectors: list[list[float]] = []
        for text in texts:
            normalized = text.casefold()
            vector = [0.0] * len(self.topics)
            for index, topic in enumerate(self.topics):
                if topic["content_term"] in normalized or topic["query_term"] in normalized:
                    vector[index] = 1.0
                    break
            vectors.append(vector)
        return vectors


class ConstantEmbedding:
    name = "p1-09-large-embedding"

    def __init__(self, dimensions: int = 64):
        self.vector = [1.0, *([0.0] * (dimensions - 1))]
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(tuple(texts))
        return [list(self.vector) for _ in texts]


class RecordingMemoryEmbeddingIndex(MemoryEmbeddingIndex):
    def __init__(self, database: Database, provider: TopicEmbedding):
        super().__init__(database, provider)
        self.candidate_counts: list[int] = []

    def similarities(
        self,
        subject_id: str,
        query: str,
        candidate_memory_ids: Iterable[str],
    ) -> dict[str, float]:
        candidates = tuple(candidate_memory_ids)
        self.candidate_counts.append(len(candidates))
        return super().similarities(subject_id, query, candidates)


def _quality_curve(
    database: Database,
    subject_id: str,
    index: MemoryEmbeddingIndex,
    fixture: RetrievalFixture,
) -> list[float]:
    weights = HybridRetrievalWeights(
        lexical=0.2,
        semantic=0.8,
        salience=0.0,
        confidence=0.0,
        recency=0.0,
        access=0.0,
        entity=0.0,
        temporal=0.0,
        causal=0.0,
    )
    curve: list[float] = []
    recall_at = fixture["recall_at"]
    for candidate_limit in fixture["candidate_limits"]:
        store = MemoryStore(
            database,
            embedding_index=index,
            retrieval_weights=weights,
            retrieval_config=BoundedRetrievalConfig(
                candidate_limit=candidate_limit,
                fts_candidate_limit=candidate_limit,
                fallback_candidate_limit=0,
            ),
        )
        relevant = 0
        for topic in fixture["topics"]:
            recalls = store.recall(
                subject_id,
                f"shared context {topic['query_term']} analysis",
                context_type="p1-09-quality",
                context_id=f"candidates-{candidate_limit}-{topic['query_term']}",
                limit=recall_at,
                record_access=False,
            )
            relevant += sum(
                topic["content_term"] in recall.memory.content.casefold() for recall in recalls
            )
        denominator = len(fixture["topics"]) * recall_at
        curve.append(relevant / denominator)
    return curve


def test_bounded_retrieval_has_deterministic_recall_curve_and_approved_floor() -> None:
    fixture = _load_fixture()
    assert fixture["version"] == 1
    topic_indexes = [
        index
        for index in range(len(fixture["topics"]))
        for _ in range(fixture["memories_per_topic"])
    ]
    first_order = list(topic_indexes)
    second_order = list(topic_indexes)
    random.Random(fixture["seed"]).shuffle(first_order)
    random.Random(fixture["seed"]).shuffle(second_order)
    assert first_order == second_order

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "quality.sqlite3")
        subject_id = "Noyra-p1-09-quality"
        IdentityStore(database).ensure(subject_id, content_hash({"seed": fixture["seed"]}))
        event = EventStore(database).append(
            subject_id,
            "retrieval-benchmark",
            "test",
            {"fixture_version": fixture["version"], "seed": fixture["seed"]},
        )
        store = MemoryStore(database)
        for ordinal, topic_index in enumerate(first_order):
            topic = fixture["topics"][topic_index]
            store.create(
                subject_id,
                "semantic",
                f"shared context {topic['content_term']} evidence item {ordinal}",
                salience=0.5,
                confidence=0.8,
                source_event_ids=(event.event_id,),
            )
        provider = TopicEmbedding(fixture["topics"])
        index = RecordingMemoryEmbeddingIndex(database, provider)
        assert index.rebuild_missing(subject_id, limit=2_000) == len(first_order)

        curve = _quality_curve(database, subject_id, index, fixture)
        expected_candidate_counts = [
            candidate_limit
            for candidate_limit in fixture["candidate_limits"]
            for _ in fixture["topics"]
        ]
        assert index.candidate_counts == expected_candidate_counts
        assert curve == _quality_curve(database, subject_id, index, fixture)
        assert index.candidate_counts == expected_candidate_counts * 2
        assert curve == sorted(curve)
        assert all(
            actual >= minimum
            for actual, minimum in zip(
                curve,
                fixture["minimum_recall_curve"],
                strict=True,
            )
        )
        approved_index = fixture["candidate_limits"].index(fixture["approved_candidate_limit"])
        assert curve[approved_index] >= fixture["approved_recall_floor"]


def test_large_embedding_table_scores_one_hard_bounded_candidate_query() -> None:
    fixture = _load_fixture()
    with tempfile.TemporaryDirectory() as directory:
        database = TrackingDatabase(Path(directory) / "large.sqlite3")
        subject_id = "Noyra-p1-09-large"
        IdentityStore(database).ensure(subject_id, content_hash({"seed": "p1-09-large"}))
        row_count = fixture["large_profile_embeddings"]
        memory_ids = [f"mem-p1-09-{index:06d}" for index in range(row_count)]
        timestamp = "2026-08-17T00:00:00.000+00:00"
        memory_rows: list[tuple[object, ...]] = []
        for row_index, memory_id in enumerate(memory_ids):
            content = f"bounded retrieval corpus item {row_index}"
            memory_rows.append(
                (
                    memory_id,
                    subject_id,
                    "semantic",
                    content,
                    content_hash(content),
                    content_hash(
                        {
                            "content": content,
                            "salience": 0.5,
                            "confidence": 0.8,
                            "status": "active",
                        }
                    ),
                    0.5,
                    0.8,
                    "private",
                    "active",
                    1,
                    timestamp,
                    timestamp,
                )
            )
        provider = ConstantEmbedding()
        vector_json = canonical_json(provider.vector)
        vector_hash = content_hash(provider.vector)
        embedding_rows = [
            (
                memory_id,
                subject_id,
                provider.name,
                len(provider.vector),
                vector_json,
                vector_hash,
                timestamp,
            )
            for memory_id in memory_ids
        ]
        with database.transaction() as connection:
            connection.executemany(
                "INSERT INTO memories(memory_id, subject_id, memory_type, content, content_hash, "
                "state_hash, salience, confidence, privacy_level, status, current_revision, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                memory_rows,
            )
            connection.executemany(
                "INSERT INTO memory_embeddings(memory_id, subject_id, provider, dimensions, "
                "vector_json, vector_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                embedding_rows,
            )
        del memory_rows, embedding_rows

        decode_count = 0
        original_decoder = strict_json_loads

        def counting_decoder(value: str) -> Any:
            nonlocal decode_count
            decode_count += 1
            return original_decoder(value)

        database.statements.clear()
        database.capture_statements = True
        embedding_index = MemoryEmbeddingIndex(database, provider)
        tracemalloc.start()
        try:
            with patch.object(
                retrieval_module,
                "strict_json_loads",
                side_effect=counting_decoder,
            ):
                scores = embedding_index.similarities(subject_id, "bounded query", memory_ids)
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            database.capture_statements = False

        candidate_limit = fixture["large_profile_candidate_limit"]
        assert candidate_limit == retrieval_module.MAX_SEMANTIC_CANDIDATES
        assert len(scores) == candidate_limit
        assert decode_count == fixture["large_profile_max_json_decodes"]
        assert provider.calls == [("bounded query",)]
        semantic_selects = [
            statement
            for statement in database.statements
            if statement.lstrip().upper().startswith("SELECT")
            and "FROM MEMORY_EMBEDDINGS" in statement.upper()
        ]
        assert len(semantic_selects) == fixture["large_profile_max_selects"]
        assert "MEMORY_ID IN (" in semantic_selects[0].upper()
        assert "INDEXED BY SQLITE_AUTOINDEX_MEMORY_EMBEDDINGS_1" in semantic_selects[0].upper()
        with database.connection() as connection:
            query_plan = [
                str(row["detail"])
                for row in connection.execute(
                    f"EXPLAIN QUERY PLAN {semantic_selects[0]}"
                ).fetchall()
            ]
        assert any(
            "sqlite_autoindex_memory_embeddings_1 (memory_id=?)" in detail for detail in query_plan
        )
        assert peak_bytes <= fixture["large_profile_max_peak_bytes"]

        database.statements.clear()
        database.capture_statements = True
        recall_candidate_limit = fixture["large_profile_recall_candidate_limit"]
        store = MemoryStore(
            database,
            embedding_index=embedding_index,
            retrieval_config=BoundedRetrievalConfig(
                candidate_limit=recall_candidate_limit,
                fts_candidate_limit=recall_candidate_limit,
                fallback_candidate_limit=0,
            ),
        )
        try:
            recalls = store.recall(
                subject_id,
                "bounded retrieval",
                context_type="p1-09-large",
                context_id="end-to-end",
                limit=8,
                record_access=False,
            )
        finally:
            database.capture_statements = False
        assert len(recalls) == 8
        assert provider.calls == [("bounded query",), ("bounded retrieval",)]
        recall_selects = [
            statement
            for statement in database.statements
            if statement.lstrip().upper().startswith("SELECT")
        ]
        assert len(recall_selects) == fixture["large_profile_recall_selects"]
        final_select = next(
            statement for statement in recall_selects if "FROM MEMORIES M" in statement.upper()
        )
        assert "INDEXED BY SQLITE_AUTOINDEX_MEMORIES_1" in final_select.upper()
        with database.connection() as connection:
            final_plan = [
                str(row["detail"])
                for row in connection.execute(f"EXPLAIN QUERY PLAN {final_select}").fetchall()
            ]
        assert any("sqlite_autoindex_memories_1 (memory_id=?)" in detail for detail in final_plan)
