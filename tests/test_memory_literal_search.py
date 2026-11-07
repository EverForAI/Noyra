from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from noyra.core import Database, EventStore, IdentityStore
from noyra.core.types import content_hash
from noyra.mind import MemoryStore
from noyra.mind.types import MemoryRecord, MemoryType


@dataclass
class SearchFixture:
    database: Database
    store: MemoryStore
    event_id: str

    def create(
        self,
        text: str,
        *,
        salience: float = 0.5,
        confidence: float = 0.7,
        kind: MemoryType = "semantic",
        subject: str = "one",
    ) -> MemoryRecord:
        event = (
            self.event_id
            if subject == "one"
            else EventStore(self.database)
            .append(subject, "memory-evidence", "fixture", {"synthetic": True})
            .event_id
        )
        return self.store.create(
            subject, kind, text, salience=salience, confidence=confidence, source_event_ids=(event,)
        )


@pytest.fixture
def memories(tmp_path: Path) -> Iterator[SearchFixture]:
    db = Database(tmp_path / "noyra.sqlite3")
    for subject in ("one", "two"):
        IdentityStore(db).ensure(subject, content_hash(subject))
    event = EventStore(db).append("one", "memory-evidence", "fixture", {"synthetic": True})
    yield SearchFixture(db, MemoryStore(db), event.event_id)


def fill(memories: SearchFixture, count: int, *, text: str = "unrelated") -> None:
    # Exercise the real writer/revision/FTS triggers in one bounded fixture transaction.
    with memories.database.transaction() as connection:
        for index in range(count):
            memories.store._create_connection(
                connection,
                "one",
                "semantic",
                f"{text} {index}",
                salience=0.9,
                confidence=0.9,
                source_event_ids=(memories.event_id,),
            )


@pytest.mark.parametrize("memory_type", [None, "semantic"])
def test_unique_literal_match_after_first_thousand_candidates_is_returned(
    memories: SearchFixture, memory_type: str | None
) -> None:
    fill(memories, 1000)
    wanted = memories.create("Only this memory contains 海边灯塔-Straße", salience=0.01)
    result = memories.store.search(
        "one", query="海边灯塔-STRASSE", memory_type=memory_type, limit=1
    )
    assert [item.memory_id for item in result] == [wanted.memory_id]


def test_returned_records_are_bounded_before_materializing_them_in_python(
    memories: SearchFixture,
) -> None:
    fill(memories, 240, text="matching")
    parse = MemoryStore._from_row
    with patch.object(MemoryStore, "_from_row", wraps=parse) as decoded:
        result = memories.store.search("one", query="matching", limit=3)
    assert len(result) == 3
    assert decoded.call_count == 3


def test_oversized_query_is_rejected_before_opening_a_database_connection(
    memories: SearchFixture,
) -> None:
    with (
        patch.object(
            memories.database, "connection", side_effect=AssertionError("invalid query opened DB")
        ),
        pytest.raises(ValueError, match="too long"),
    ):
        memories.store.search("one", query="ß" * 501)


@pytest.mark.parametrize(
    ("content", "query", "matches"),
    [
        ("Straße am Meer", "STRASSE", True),
        ("ΟΣ και θάλασσα", "ος", True),
        ("İstanbul", "i\u0307stanbul", True),
        ("A ﬃ ligature", "FFI", True),
        ("海边灯塔记录", "海边灯塔", True),
        ("A Mixed-Case phrase", "mixed-CASE", True),
        ("cafe\u0301", "café", False),
        ("Istanbul", "\u0131stanbul", False),
    ],
)
def test_unicode_matching_preserves_python_casefold_without_extra_normalization(
    memories: SearchFixture, content: str, query: str, matches: bool
) -> None:
    memory = memories.create(content)
    result = memories.store.search("one", query=query)
    assert [item.memory_id for item in result] == ([memory.memory_id] if matches else [])
    assert matches == (query.strip().casefold() in content.casefold())


@pytest.mark.parametrize("query", ["%", "_", "\\", "' OR 1=1 --", "[ab]", "\x00尾"])
def test_sql_wildcards_quotes_and_nul_are_literal(memories: SearchFixture, query: str) -> None:
    memories.create("A higher-ranked unrelated memory", salience=0.99)
    memory = memories.create("prefix " + query + " suffix", salience=0.01)
    assert [item.memory_id for item in memories.store.search("one", query=query)] == [
        memory.memory_id
    ]


def test_subject_type_and_lifecycle_filters_apply_before_limit(memories: SearchFixture) -> None:
    memories.create("needle private to another subject", subject="two", salience=1)
    memories.create("needle episodic", kind="episodic", salience=0.99)
    archived = memories.create("needle archived", salience=0.98)
    superseded = memories.create("needle superseded", salience=0.97)
    for memory, status in ((archived, "archived"), (superseded, "superseded")):
        memories.store.revise(
            memory.memory_id,
            content=memory.content,
            salience=memory.salience,
            confidence=memory.confidence,
            status=status,
            reason="Synthetic lifecycle fixture",
            source_event_ids=(memories.event_id,),
            expected_revision=memory.current_revision,
        )
    wanted = memories.create("needle active semantic", salience=0.01)
    result = memories.store.search("one", query="needle", memory_type="semantic", limit=1)
    assert [item.memory_id for item in result] == [wanted.memory_id]
    assert memories.store.search("one", query="needle", memory_type="not-a-real-type") == []
    assert len(memories.store.search("two", query="needle")) == 1


def test_matching_results_keep_existing_salience_confidence_and_recency_order(
    memories: SearchFixture,
) -> None:
    values = []
    for text, salience, confidence, second in (
        ("needle low-salience", 0.4, 1.0, 5),
        ("needle low-confidence", 0.8, 0.4, 6),
        ("needle older", 0.8, 0.8, 1),
        ("needle newest", 0.8, 0.8, 9),
    ):

        def fixed_clock(value: int = second) -> str:
            return f"2026-11-04T09:00:{value:02d}+00:00"

        memories.store.clock = fixed_clock
        values.append(memories.create(text, salience=salience, confidence=confidence))
    result = memories.store.search("one", query="needle", limit=3)
    assert [item.memory_id for item in result] == [
        values[3].memory_id,
        values[2].memory_id,
        values[1].memory_id,
    ]
    assert memories.store.search("one", query="absent") == []


@pytest.mark.parametrize("query", ["", "  \t\n  "])
def test_empty_query_decodes_only_requested_highest_ranked_records(
    memories: SearchFixture, query: str
) -> None:
    fill(memories, 250)
    parse = MemoryStore._from_row
    with patch.object(MemoryStore, "_from_row", wraps=parse) as decoded:
        assert len(memories.store.search("one", query=query, limit=2)) == 2
    assert decoded.call_count == 2
    assert len(memories.store.search("one", query=query, limit=10000)) == 200
    assert len(memories.store.search("one", query=query, limit=0)) == 1


def test_casefold_query_length_boundary_and_surrounding_whitespace(memories: SearchFixture) -> None:
    memory = memories.create("prefix " + "ß" * 500 + " suffix")
    assert [
        item.memory_id for item in memories.store.search("one", query="  " + "SS" * 500 + "  ")
    ] == [memory.memory_id]
    with pytest.raises(ValueError, match="too long"):
        memories.store.search("one", query="ß" * 501)


def test_returned_matching_record_still_uses_integrity_validation(memories: SearchFixture) -> None:
    from noyra.core.errors import IntegrityError

    memory = memories.create("needle content")
    with memories.database.transaction() as connection:
        connection.execute(
            "UPDATE memories SET content_hash=? WHERE memory_id=?", ("0" * 64, memory.memory_id)
        )
    with pytest.raises(IntegrityError, match="content hash mismatch"):
        memories.store.search("one", query="needle")


def test_search_works_on_read_only_reopened_database_without_writing_schema(
    memories: SearchFixture,
) -> None:
    memory = memories.create("needle Straße")
    with memories.database.connection() as connection:
        before = Database.schema_ddl_fingerprint(connection)
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM memory_accesses),(SELECT COUNT(*) FROM memory_revisions)"
        ).fetchone()
    readonly = Database(memories.database.path, initialize=False, read_only=True)
    assert [item.memory_id for item in MemoryStore(readonly).search("one", query="STRASSE")] == [
        memory.memory_id
    ]
    with memories.database.connection() as connection:
        assert Database.schema_ddl_fingerprint(connection) == before
        after = connection.execute(
            "SELECT (SELECT COUNT(*) FROM memory_accesses),(SELECT COUNT(*) FROM memory_revisions)"
        ).fetchone()
        assert tuple(after) == tuple(counts)
