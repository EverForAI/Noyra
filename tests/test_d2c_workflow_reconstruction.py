from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, strict_json_loads
from noyra.core.workflow_reconstruction import _reconstruct
from test_d2b_workflow_bundle import Objects, export, nothing, seed
from test_d2b_workflow_bundle import database as database
from test_d2b_workflow_bundle import template as template


def test_large_isolated_reconstruction_has_complete_original_rows_and_bounded_pages(
    database: Database,
) -> None:
    seed(database, 50_001)
    objects = Objects()
    source, descriptor = export(database, objects)
    with _reconstruct(objects, descriptor, source, nothing) as view:
        assert view.summary()["records"] == 50_001
        assert view.summary()["restore_or_dispatch_authorized"] is False
        first = view.page(limit=2)
        assert [r["row"]["checkpoint_version"] for r in first["records"]] == [1, 2]
        assert first["truncated"] and first["next_after_version"] == 2
        assert view.latest()["row"]["checkpoint_version"] == 50_001
        assert view.page(after_version=50_001)["records"] == []
        with database.read_transaction() as connection:
            last = dict(
                connection.execute(
                    "SELECT * FROM workflow_checkpoints WHERE checkpoint_version=50001"
                ).fetchone()
            )
        assert view.latest()["row"] == last
        assert "schema_meta" not in {
            r[0]
            for r in view._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            view._connection.execute("DELETE FROM reconstructed_records")
    with pytest.raises(ValueError, match="closed"):
        view.latest()


def test_reconstruction_preserves_original_rollback_receipts(database: Database) -> None:
    workflow = DurableWorkflowStore(database)
    for hour in [12, 10, 11, 13]:
        when = f"2026-11-06T{hour}:00:00+00:00"
        with patch("noyra.autonomy.workflow.utc_now", return_value=when):
            workflow.checkpoint("export-subject", "workflow", "fixture", "running", {}, reason="x")
    objects = Objects()
    source, descriptor = export(database, objects)
    assert source["receipt_count"] == 2
    with _reconstruct(objects, descriptor, source, nothing) as view:
        records = view.page()["records"]
        assert records[0]["receipt"] is None
        assert records[1]["receipt"]["payload"]["mode"] == "rollback"
        assert view.summary()["receipt_count"] == 2
        assert all(r["high_water"] == records[0]["high_water"] for r in records[:3])
        with database.read_transaction() as connection:
            for record in records:
                if record["receipt"] is not None:
                    original = connection.execute(
                        "SELECT * FROM events WHERE event_id=?", (record["receipt"]["event_id"],)
                    ).fetchone()
                    assert record["receipt"]["payload_hash"] == original["payload_hash"]
                    assert record["receipt"]["occurred_at"] == original["occurred_at"]


@pytest.mark.parametrize("damage", ["missing", "state", "aggregate", "ordering", "scope"])
def test_unverified_or_partial_reconstruction_never_exposes_view(
    database: Database, damage: str
) -> None:
    seed(database, 3)
    objects = Objects()
    source, descriptor = export(database, objects)
    manifest = strict_json_loads(objects.values["manifest.enc"])
    segment = manifest["segments"][0]
    body = strict_json_loads(objects.values[segment["name"]])
    if damage == "missing":
        del objects.values[segment["name"]]
    elif damage == "aggregate":
        source["history_hash"] = "0" * 64
        manifest["source"] = source
    else:
        if damage == "ordering":
            body["records"].reverse()
        elif damage == "scope":
            body["records"][0]["row"]["subject_id"] = "another-subject"
        else:
            row = body["records"][0]["row"]
            row["state_json"] = canonical_json({"tampered": True})
            row["state_hash"] = content_hash({"tampered": True})
        changed = objects.put(segment["name"], canonical_json(body).encode())
        manifest["segments"][0] = {**segment, **changed}
    descriptor = objects.put("manifest.enc", canonical_json(manifest).encode())
    with (
        pytest.raises((IntegrityError, KeyError)),
        _reconstruct(objects, descriptor, source, nothing),
    ):
        pytest.fail("tentative records escaped before complete aggregate validation")


def test_second_pass_detects_changes_in_reconstructed_sqlite_rows(database: Database) -> None:
    seed(database, 3)
    objects = Objects()
    source, descriptor = export(database, objects)
    connect = sqlite3.connect

    class CorruptingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if sql == "INSERT INTO reconstructed_records VALUES(?,?)" and parameters[0] == 2:
                record = strict_json_loads(parameters[1])
                record["row"]["reason"] = "changed in reconstruction"
                parameters = (2, canonical_json(record))
            return super().execute(sql, parameters)

    def corrupting_connect(path: str) -> sqlite3.Connection:
        return connect(path, factory=CorruptingConnection)

    with (
        patch("noyra.core.workflow_reconstruction.sqlite3.connect", corrupting_connect),
        pytest.raises(IntegrityError),
        _reconstruct(objects, descriptor, source, nothing),
    ):
        pytest.fail("reconstructed database was not independently read back")


def test_reconstruction_budget_cancellation_and_page_validation(database: Database) -> None:
    seed(database, 3)
    objects = Objects()
    source, descriptor = export(database, objects)
    with (
        patch("noyra.core.workflow_reconstruction.MAX_RECONSTRUCTION_BYTES", 1),
        pytest.raises(IntegrityError, match="budget"),
        _reconstruct(objects, descriptor, source, nothing),
    ):
        pytest.fail("memory budget was ignored")
    with _reconstruct(objects, descriptor, source, nothing) as view:
        for after, limit in [(True, 1), (0, False), (-1, 1), (4, 1), (0, 101)]:
            with pytest.raises(ValueError):
                view.page(after_version=after, limit=limit)
        with patch("noyra.core.workflow_reconstruction.MAX_PAGE_BYTES", 600):
            page = view.page()
            assert len(page["records"]) < 3 and page["truncated"]

    def cancelled() -> None:
        raise TimeoutError("synthetic cancelled")

    with pytest.raises(TimeoutError), _reconstruct(objects, descriptor, source, cancelled):
        pytest.fail("cancelled view escaped")


def test_reconstruction_creates_no_disk_database(database: Database) -> None:
    seed(database, 1)
    objects = Objects()
    source, descriptor = export(database, objects)
    before = set(database.path.parent.iterdir())
    with _reconstruct(objects, descriptor, source, nothing) as view:
        assert view._connection.execute("PRAGMA database_list").fetchone()[2] == ""
        assert view.summary()["storage"] == "ephemeral_memory_sqlite"
    assert set(database.path.parent.iterdir()) == before
    assert Path(database.path).is_file()
