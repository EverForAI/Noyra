from __future__ import annotations

from typing import Any

import pytest

from noyra.core.errors import IntegrityError
from test_memory_literal_search import SearchFixture
from test_memory_literal_search import memories as memories


@pytest.mark.parametrize("memory_type", [None, "semantic"])
def test_complete_memory_order_is_indexed_without_temporary_sort(
    memories: SearchFixture, memory_type: str | None
) -> None:
    query = "SELECT * FROM memories WHERE subject_id=? AND status='active'"
    parameters: list[Any] = ["one"]
    if memory_type is not None:
        query += " AND memory_type=?"
        parameters.append(memory_type)
    query += " ORDER BY salience DESC,confidence DESC,updated_at DESC LIMIT ?"
    parameters.append(20)
    with memories.database.read_transaction() as c:
        plan = " ".join(
            row["detail"] for row in c.execute("EXPLAIN QUERY PLAN " + query, parameters)
        )
    assert "USE TEMP B-TREE" not in plan
    assert (
        "idx_memories_subject_rank" if memory_type is None else "idx_memories_subject_type_rank"
    ) in plan


def test_index_keeps_full_ranking_subject_filters_and_bad_hash_detection(
    memories: SearchFixture,
) -> None:
    store = memories.store
    with memories.database.transaction() as c:
        for index in range(600):
            store._create_connection(
                c,
                "one",
                "semantic",
                f"Synthetic low priority {index}",
                salience=0.1,
                confidence=0.2,
                source_event_ids=(memories.event_id,),
            )
    top = memories.create("Inserted after the low priority records", salience=0.99)
    higher_other = memories.create("Different subject", subject="two", salience=1.0)
    assert higher_other.memory_id != top.memory_id
    assert [row.memory_id for row in store.search("one", limit=1)] == [top.memory_id]
    assert [row.memory_id for row in store.search("one", memory_type="semantic", limit=1)] == [
        top.memory_id
    ]
    with memories.database.transaction() as c:
        c.execute("UPDATE memories SET state_hash=? WHERE memory_id=?", ("0" * 64, top.memory_id))
    with pytest.raises(IntegrityError):
        store.search("one", limit=1)
