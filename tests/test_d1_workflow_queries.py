from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from noyra.autonomy.workflow import CHECKPOINT_OWNERSHIP_SQL, DurableWorkflowStore
from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash


@pytest.fixture
def workflows(tmp_path: Path) -> Iterator[DurableWorkflowStore]:
    db = Database(tmp_path / "workflow.sqlite3")
    for subject in ("one", "two"):
        IdentityStore(db).ensure(subject, content_hash(subject))
    yield DurableWorkflowStore(db)


def seed(
    store: DurableWorkflowStore, count: int, *, workflow: str = "long", subject: str = "one"
) -> None:
    with store.database.transaction() as c:
        c.executemany(
            "INSERT INTO workflow_checkpoints VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    workflow,
                    subject,
                    "fixture",
                    index,
                    "running",
                    canonical_json({"n": index}),
                    content_hash({"n": index}),
                    "Synthetic checkpoint",
                    "2026-11-06T00:00:00+00:00",
                )
                for index in range(1, count + 1)
            ],
        )


def measured_integrity(store: DurableWorkflowStore, subject: str) -> tuple[int, int, list[str]]:
    ticks = 0
    statements: list[str] = []

    def progress() -> int:
        nonlocal ticks
        ticks += 1
        return 0

    with store.database.read_transaction() as c:
        c.set_progress_handler(progress, 100)
        c.set_trace_callback(statements.append)
        try:
            total = store._verify_integrity_connection(c, subject, database=store.database)
        finally:
            c.set_progress_handler(None, 0)
            c.set_trace_callback(None)
    return total, ticks * 100, statements


def test_workflow_integrity_work_does_not_square_with_history(
    workflows: DurableWorkflowStore,
) -> None:
    seed(workflows, 1024)
    total, steps, _ = measured_integrity(workflows, "one")
    assert total == 1024
    # VM work, independent of CPU speed and Windows filesystem timing. Full
    # state/clock verification must stay; only repeated ownership joins go.
    assert steps < 1024 * 300


@pytest.mark.parametrize("damage", ["state_hash", "late_owner", "early_owner"])
def test_full_history_still_rejects_late_and_early_corruption(
    workflows: DurableWorkflowStore, damage: str
) -> None:
    seed(workflows, 256)
    with workflows.database.transaction() as c:
        if damage == "state_hash":
            c.execute(
                "UPDATE workflow_checkpoints SET state_hash=? WHERE checkpoint_version=256",
                ("0" * 64,),
            )
        else:
            # A damaged-file fixture bypasses the normal ownership trigger.
            # Domain verification must still catch a rehashed foreign row.
            for row in c.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' "
                "AND tbl_name='workflow_checkpoints'"
            ).fetchall():
                c.execute('DROP TRIGGER "' + row[0] + '"')
            c.execute(
                "UPDATE workflow_checkpoints SET subject_id='two' WHERE checkpoint_version=?",
                (256 if damage == "late_owner" else 1,),
            )
    with pytest.raises(IntegrityError):
        workflows.verify_integrity("one")
    if damage != "state_hash":
        with pytest.raises(IntegrityError):
            workflows.verify_integrity("two")


def test_ownership_probe_work_grows_linearly_without_omitting_any_workflow(
    workflows: DurableWorkflowStore,
) -> None:
    with workflows.database.read_transaction() as c:
        plan = " ".join(
            row["detail"]
            for row in c.execute("EXPLAIN QUERY PLAN " + CHECKPOINT_OWNERSHIP_SQL, ("one", "one"))
        )
        assert "idx_workflow_checkpoints_subject_history" in plan
        assert "USE TEMP B-TREE" not in plan
    seed(workflows, 128, workflow="small")
    _, small_steps, _ = measured_integrity(workflows, "one")
    seed(workflows, 896, workflow="large")
    seed(workflows, 128, workflow="foreign", subject="two")
    total, large_steps, statements = measured_integrity(workflows, "one")
    assert total == 1024
    assert large_steps < max(100, small_steps) * 12  # Eight times the owned history.
    select = next(sql for sql in statements if "SELECT own.workflow_id" in sql)
    assert "DISTINCT workflow_id" in select
    assert "LIMIT 1" in select  # Limits conflict reports, not the owned candidate set.
    assert workflows.verify_integrity("two") == 128
    assert workflows.verify_integrity("unrelated-subject") == 0


def test_empty_subject_and_middle_type_corruption_still_use_complete_history(
    workflows: DurableWorkflowStore,
) -> None:
    assert workflows.verify_integrity("one") == 0
    seed(workflows, 256)
    # The final valid row cannot hide an invalid middle row.
    with workflows.database.transaction() as c:
        c.execute(
            "UPDATE workflow_checkpoints SET workflow_type='changed' WHERE checkpoint_version=128"
        )
    with pytest.raises(IntegrityError):
        workflows.verify_integrity("one")
