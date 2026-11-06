from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash
from test_d1_workflow_queries import seed
from test_d1_workflow_queries import workflows as workflows


def test_large_workflow_returns_explicit_unverified_maintenance_metadata(
    workflows: DurableWorkflowStore,
) -> None:
    seed(workflows, 50_001)
    result = workflows.clock_catalog("one", "long")
    assert result["status"] == "maintenance_required"
    assert result["history_verified"] is False
    assert result["expected_fingerprint"] is None
    assert result["capacity"]["records"] == 50_001
    assert result["unexplained_versions"] is None
    assert result["maintenance"]["execution_available"] is False


@pytest.mark.parametrize("damage", ["state_hash", "version_gap", "wrong_type"])
def test_small_catalog_still_validates_complete_history_and_never_hides_corruption(
    workflows: DurableWorkflowStore,
    damage: str,
) -> None:
    seed(workflows, 4)
    with workflows.database.transaction() as c:
        if damage == "state_hash":
            c.execute(
                "UPDATE workflow_checkpoints SET state_hash=? WHERE checkpoint_version=2",
                ("0" * 64,),
            )
        elif damage == "version_gap":
            c.execute("DELETE FROM workflow_checkpoints WHERE checkpoint_version=2")
        else:
            c.execute(
                "UPDATE workflow_checkpoints SET workflow_type='other' WHERE checkpoint_version=2"
            )
    with pytest.raises(IntegrityError):
        workflows.clock_catalog("one", "long")


def test_oversized_catalog_does_not_pretend_to_have_checked_hashes(
    workflows: DurableWorkflowStore,
) -> None:
    seed(workflows, 50_001)
    with workflows.database.transaction() as c:
        c.execute(
            "UPDATE workflow_checkpoints SET state_hash=? WHERE checkpoint_version=50_001",
            ("0" * 64,),
        )
    with patch.object(
        DurableWorkflowStore, "_from_row", side_effect=AssertionError("materialized history")
    ):
        result = workflows.clock_catalog("one", "long")
    assert result["history_verified"] is False and result["structure_verified"] is False
    assert result["unexplained_versions"] is None and result["high_water_at"] is None
    with pytest.raises(ValueError, match="exceeds recovery bound"):
        workflows.clock_history("one", "long")
    with (
        workflows.database.transaction() as c,
        pytest.raises(ValueError, match="exceeds recovery bound"),
    ):
        workflows.acknowledge_clock_history(
            c,
            "one",
            "long",
            "0" * 64,
            actor="operator",
            reason="Synthetic review",
            evidence_sha256=content_hash("review"),
        )


def test_ownership_is_checked_before_capacity_or_state_fields(
    workflows: DurableWorkflowStore,
) -> None:
    seed(workflows, 2)
    for subject in ("two", "no-subject"):
        with workflows.database.read_transaction() as c:
            trace: list[str] = []
            connect = workflows.database._connect

            def traced(
                open_connection: Callable[[], Any] = connect, statements: list[str] = trace
            ) -> Any:
                connection = open_connection()
                connection.set_trace_callback(statements.append)
                return connection

            with (
                patch.object(workflows.database, "_connect", side_effect=traced),
                pytest.raises(PermissionError),
            ):
                workflows.clock_catalog(subject, "long")
            assert not any("SUM(" in sql.upper() or "SELECT *" in sql.upper() for sql in trace)
    with workflows.database.transaction() as c:
        for row in c.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name='workflow_checkpoints'"
        ).fetchall():
            c.execute('DROP TRIGGER "' + row[0] + '"')
        c.execute("UPDATE workflow_checkpoints SET subject_id='two' WHERE checkpoint_version=2")
    for subject in ("one", "two"):
        with pytest.raises(PermissionError):
            workflows.clock_catalog(subject, "long")


def test_clock_catalog_distinguishes_unexplained_time_from_verified_history(
    workflows: DurableWorkflowStore,
) -> None:
    seed(workflows, 3)
    with workflows.database.transaction() as c:
        c.execute(
            "UPDATE workflow_checkpoints SET created_at='2026-11-05T00:00:00+00:00' "
            "WHERE checkpoint_version=2"
        )
    catalog = workflows.clock_catalog("one", "long")
    assert catalog["status"] == "clock_evidence_required"
    assert not catalog["history_verified"] and catalog["structure_verified"]
    assert catalog["unexplained_versions"] == [2]
    assert isinstance(catalog["expected_fingerprint"], str)
    with workflows.database.transaction() as c:
        workflows.acknowledge_clock_history(
            c,
            "one",
            "long",
            catalog["expected_fingerprint"],
            actor="operator",
            reason="Independent synthetic clock evidence",
            evidence_sha256=content_hash("review"),
        )
    final = workflows.clock_catalog("one", "long")
    assert final["status"] == "verified" and final["history_verified"]
    assert final["expected_fingerprint"] == catalog["expected_fingerprint"]
    assert final["unexplained_versions"] == []


def test_byte_capacity_uses_utf8_and_returns_no_private_state(
    workflows: DurableWorkflowStore,
) -> None:
    state = {"private": "雪" * 180_000}
    encoded = canonical_json(state)
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
                    "PRIVATE_REASON",
                    "2026-11-06T00:00:00+00:00",
                )
                for version in range(1, 33)
            ],
        )
    result = workflows.clock_catalog("one", "large-bytes")
    assert result["status"] == "maintenance_required"
    assert result["capacity"]["state_bytes"] == len(encoded.encode()) * 32
    assert result["maintenance"]["exceeded"] == ["state_byte_limit"]
    assert "PRIVATE_REASON" not in str(result) and "雪" not in str(result)


@pytest.mark.parametrize(("subject", "workflow"), [("one", ""), ("one", "x" * 257), ("", "long")])
def test_invalid_scope_does_not_open_a_read(
    workflows: DurableWorkflowStore, subject: str, workflow: str
) -> None:
    with (
        patch.object(
            workflows.database, "read_transaction", side_effect=AssertionError("opened DB")
        ),
        pytest.raises(ValueError),
    ):
        workflows.clock_catalog(subject, workflow)


def test_ordinary_catalog_reuses_its_same_snapshot_capacity_measurement(
    workflows: DurableWorkflowStore,
) -> None:
    seed(workflows, 3)
    trace: list[str] = []
    connect = workflows.database._connect

    def traced() -> Any:
        connection = connect()
        connection.set_trace_callback(trace.append)
        return connection

    with patch.object(workflows.database, "_connect", side_effect=traced):
        result = workflows.clock_catalog("one", "long")
    assert result["history_verified"]
    assert sum("SUM(length(CAST(state_json" in sql for sql in trace) == 1


def test_maintenance_observation_does_not_accept_model_or_runtime_work(
    workflows: DurableWorkflowStore,
) -> None:
    seed(workflows, 50_001)
    with workflows.database.read_transaction() as c:
        before = {
            name: [dict(row) for row in c.execute("SELECT * FROM " + name)]
            for name in (
                "model_calls",
                "model_attempts",
                "events",
                "audit_records",
                "runtime_state",
            )
        }
    workflows.clock_catalog("one", "long")
    with workflows.database.read_transaction() as c:
        after = {name: [dict(row) for row in c.execute("SELECT * FROM " + name)] for name in before}
    assert before == after
