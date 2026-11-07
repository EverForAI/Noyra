from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.types import canonical_json, content_hash
from test_d1_workflow_queries import seed
from test_d1_workflow_queries import workflows as workflows
from test_m42_p2_17_runtime_log_keyset import insert_records, make_projection


def test_workflow_capacity_metadata_describes_the_complete_verified_history(
    workflows: DurableWorkflowStore,
) -> None:
    seed(workflows, 40)
    result = workflows.clock_history("one", "long")
    with workflows.database.read_transaction() as c:
        expected_bytes = c.execute(
            "SELECT SUM(length(CAST(state_json AS BLOB))) FROM workflow_checkpoints "
            "WHERE workflow_id='long'"
        ).fetchone()[0]
        assert c.total_changes == 0
    assert result["checkpoint_count"] == 40
    assert result["capacity"] == {
        "scope": "complete_workflow_history",
        "records": 40,
        "state_bytes": expected_bytes,
        "record_limit": 50_000,
        "state_byte_limit": 16 * 1024 * 1024,
        "truncated": False,
    }


def test_runtime_page_metadata_matches_anchored_keyset_without_total_count(tmp_path: Path) -> None:
    kernel, projection = make_projection(tmp_path)
    try:
        insert_records(kernel, 0, 18, prefix="capacity")
        page = projection.runtime_logs(kernel.subject_id, limit=7)
        assert page["page"] == {
            "scope": "anchored_keyset_page",
            "returned": 7,
            "record_limit": 7,
            "has_more": True,
        }
        original = list(page["items"])
        cursor = page["next_cursor"]
        assert cursor is not None
        insert_records(kernel, 18, 3, prefix="newer")
        trace: list[str] = []
        connect = kernel.database._connect

        def traced() -> Any:
            connection = connect()
            connection.set_trace_callback(trace.append)
            return connection

        with patch.object(kernel.database, "_connect", side_effect=traced):
            while cursor:
                page = projection.runtime_logs(kernel.subject_id, limit=7, cursor=cursor)
                assert page["page"]["returned"] == len(page["items"])
                assert page["page"]["has_more"] == (page["next_cursor"] is not None)
                original.extend(page["items"])
                cursor = page["next_cursor"]
        assert page["page"]["has_more"] is False
        assert not any("COUNT(" in sql.upper() or "OFFSET" in sql.upper() for sql in trace)
        assert len({(row["category"], row["record_id"]) for row in original}) == len(original)
        assert all("newer" not in row["summary"] for row in original)
        empty = projection.runtime_logs("unknown-subject", limit=50)
        assert empty["page"] == {
            "scope": "anchored_keyset_page",
            "returned": 0,
            "record_limit": 50,
            "has_more": False,
        }
    finally:
        kernel.close()


def test_record_limit_refuses_oversized_history_before_materialization(
    workflows: DurableWorkflowStore,
) -> None:
    seed(workflows, 50_001)
    with (
        patch.object(
            DurableWorkflowStore, "_from_row", side_effect=AssertionError("oversized read")
        ),
        pytest.raises(ValueError, match="exceeds recovery bound"),
    ):
        workflows.clock_history("one", "long")
    with workflows.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0] == 50_001


def test_byte_limit_counts_multibyte_payloads_without_truncating_them(
    workflows: DurableWorkflowStore,
) -> None:
    state = {"text": "雪" * 180_000}
    encoded = canonical_json(state)
    assert len(encoded) * 32 < 16 * 1024 * 1024
    assert len(encoded.encode()) * 32 > 16 * 1024 * 1024
    with workflows.database.transaction() as c:
        c.executemany(
            "INSERT INTO workflow_checkpoints VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    "large-bytes",
                    "one",
                    "fixture",
                    version,
                    "running",
                    encoded,
                    content_hash(state),
                    "Synthetic multi-byte history",
                    "2026-11-06T00:00:00+00:00",
                )
                for version in range(1, 33)
            ],
        )
    with (
        patch.object(
            DurableWorkflowStore, "_from_row", side_effect=AssertionError("oversized read")
        ),
        pytest.raises(ValueError, match="exceeds recovery bound"),
    ):
        workflows.clock_history("one", "large-bytes")
