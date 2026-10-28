from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from noyra.core import Database, EventStore, IdentityStore, SubjectKernel
from noyra.core import database as database_module
from noyra.core.errors import IntegrityError, SchemaContractError
from noyra.core.integrity import IntegrityRegistry, IntegrityRuntimeController
from noyra.core.types import content_hash, utc_now
from noyra.mind import GoalCandidate, GoalStore, MindEngine


def form_goal(db: Database, subject: str, origin: str) -> tuple[GoalStore, str, str]:
    event = EventStore(db).append(subject, "world_observation", "local-test", {"test": "goal"})
    goals = GoalStore(db)
    goal = goals.create_candidate(
        subject,
        GoalCandidate.model_validate(
            {
                "title": "Local goal",
                "description": "A deliberate local goal",
                "origin": origin,
                "priority": 0.6,
                "commitment": 0.5,
                "motive_emotion": "interest",
            }
        ),
        causal_source_ids=(event.event_id,),
        reason="Local test evidence",
    )
    return goals, goal.goal_id, event.event_id


@pytest.mark.parametrize(
    "origin", ["self", "environment", "human_proposal", "maintenance", "mixed"]
)
def test_new_goal_provenance_survives_accept_activate_and_revise(
    tmp_path: Path, origin: str
) -> None:
    db = Database(tmp_path / "db", strict_schema_contract=True)
    IdentityStore(db).ensure("subject", content_hash("seed"))
    goals, goal_id, event = form_goal(db, "subject", origin)
    assert goals.get(goal_id).origin_evidence == "created"
    with db.connection() as c:
        witness = c.execute("SELECT origin_evidence_hash FROM goals").fetchone()[0]
    if origin == "human_proposal":
        goals.accept_human_proposal(goal_id, rationale="I accept", causal_source_ids=(event,))
    goals.activate(goal_id, rationale="I choose this", causal_source_ids=(event,))
    revised = goals.revise(
        goal_id,
        status="active",
        title="Revised goal",
        priority=0.8,
        commitment=0.7,
        progress=0.1,
        emotional_pressure=0.2,
        reason="Updated evidence",
        causal_source_ids=(event,),
    )
    assert revised.origin == origin and revised.origin_evidence == "created"
    with db.connection() as c:
        assert c.execute("SELECT origin_evidence_hash FROM goals").fetchone()[0] == witness
    assert MindEngine(db).verify_integrity("subject")["goals"] == 1
    Database(db.path, strict_schema_contract=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin", "self"),
        ("goal_id", "another"),
        ("subject_id", "another"),
        ("created_at", "2099-01-01T00:00:00Z"),
        ("origin_evidence", "legacy_baseline"),
        ("origin_evidence_hash", "0" * 64),
    ],
)
def test_goal_provenance_identity_is_immutable(tmp_path: Path, field: str, value: str) -> None:
    db = Database(tmp_path / "db")
    IdentityStore(db).ensure("subject", content_hash("seed"))
    goals, goal_id, _ = form_goal(db, "subject", "human_proposal")
    with db.transaction() as c, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        c.execute(f"UPDATE goals SET {field}=? WHERE goal_id=?", (value, goal_id))
    assert goals.get(goal_id).origin == "human_proposal"


def test_legal_origin_corruption_is_detected_by_mind_state_and_pauses_subject(
    tmp_path: Path,
) -> None:
    owner = SubjectKernel(tmp_path / "db", "goal-origin", content_hash("seed"))
    owner.boot()
    owner.orient()
    owner.activate()
    try:
        goals, goal_id, _ = form_goal(owner.database, owner.subject_id, "human_proposal")
        with owner.database.transaction() as c:
            guard = c.execute(
                "SELECT sql FROM sqlite_master WHERE name='prevent_goal_origin_update'"
            ).fetchone()[0]
            c.execute("DROP TRIGGER prevent_goal_origin_update")
            c.execute("UPDATE goals SET origin='self' WHERE goal_id=?", (goal_id,))
            c.execute(guard)
        with pytest.raises(IntegrityError, match="goal origin evidence mismatch"):
            goals.get(goal_id)
        # Schema is intact again; this is detection of row corruption, not DDL drift.
        report = IntegrityRegistry().run(
            owner.database,
            owner.subject_id,
            tmp_path,
            profile="manual",
            policy_mode="pause",
            deadline_seconds=15,
            check_ids=("mind.state",),
        )
        assert report.status == "corrupt", report.to_dict()
        applied = IntegrityRuntimeController(owner, tmp_path, policy_mode="pause")._apply_policy(
            report
        )
        assert applied.action["result"] == "paused"
        assert owner.lifecycle.current().state == "paused"
    finally:
        owner.close()


def legacy_database(path: Path) -> tuple[Database, str, str]:
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 105):
        db = Database(path, strict_schema_contract=True)
    IdentityStore(db).ensure("subject", content_hash("seed"))
    event = EventStore(db).append("subject", "world_observation", "local-test", {"legacy": True})
    now = utc_now()
    state = GoalStore._state_hash("Old goal", "Old description", "proposed", 0.6, 0.5, 0, 0)
    with db.transaction() as c:
        c.execute(
            "INSERT INTO goals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "old-goal",
                "subject",
                "Old goal",
                "Old description",
                "human_proposal",
                "proposed",
                0.6,
                0.5,
                0,
                0,
                state,
                1,
                now,
                now,
            ),
        )
        GoalStore._insert_revision(
            c,
            "old-goal",
            1,
            "Old goal",
            "Old description",
            "proposed",
            0.6,
            0.5,
            0,
            0,
            "Old reason",
            (event.event_id,),
            now,
        )
    return db, "old-goal", event.event_id


@pytest.mark.parametrize("unprovable_old_edit", [False, True])
def test_v105_migration_pins_honest_legacy_baseline_without_rewriting_history(
    tmp_path: Path,
    unprovable_old_edit: bool,
) -> None:
    db, goal_id, event = legacy_database(tmp_path / "old.db")
    with db.transaction() as c:
        revisions_before = [tuple(r) for r in c.execute("SELECT * FROM goal_revisions")]
        if unprovable_old_edit:
            c.execute("UPDATE goals SET origin='self'")
        hash_before = c.execute("SELECT state_hash FROM goals").fetchone()[0]
    db = Database(db.path, strict_schema_contract=True)
    goals = GoalStore(db)
    baseline = goals.get(goal_id)
    assert baseline.origin_evidence == "legacy_baseline"
    assert baseline.origin == ("self" if unprovable_old_edit else "human_proposal")
    with db.connection() as c:
        assert c.execute("SELECT state_hash FROM goals").fetchone()[0] == hash_before
        assert [tuple(r) for r in c.execute("SELECT * FROM goal_revisions")] == revisions_before
    if not unprovable_old_edit:
        goals.accept_human_proposal(
            goal_id, rationale="An independent choice", causal_source_ids=(event,)
        )
        goals.activate(goal_id, rationale="I choose this", causal_source_ids=(event,))
    else:
        # Migration cannot repair an unprovable past edit or infer acceptance.
        assert goals.get(goal_id).status == "proposed"
    assert goals.get(goal_id).origin_evidence == "legacy_baseline"
    assert MindEngine(db).verify_integrity("subject")["goals"] == 1
    from io import BytesIO
    from zipfile import ZipFile

    from noyra.core.runtime_export import RuntimeLogExporter

    with ZipFile(
        BytesIO(RuntimeLogExporter(db).export("subject", actor="test").content)
    ) as archive:
        exported = json.loads(archive.read("tables/goals.jsonl"))
        assert exported["origin_evidence"] == "legacy_baseline"
    # New rows cannot copy a legacy witness to claim historical provenance.
    with db.transaction() as c, pytest.raises(sqlite3.IntegrityError, match="migration-only"):
        c.execute(
            "INSERT INTO goals SELECT 'forged',subject_id,title,description,origin,status,"
            "priority,commitment,progress,emotional_pressure,state_hash,current_revision,"
            "created_at,updated_at,origin_evidence,origin_evidence_hash FROM goals LIMIT 1"
        )
    Database(db.path, strict_schema_contract=True)


def test_provenance_migration_failure_rolls_back_and_retry_keeps_history(tmp_path: Path) -> None:
    db, goal_id, _ = legacy_database(tmp_path / "old.db")
    original = Database._execute_sql_script

    def fail(c: sqlite3.Connection, script: str) -> None:
        if "prevent_goal_origin_update" in script:
            raise RuntimeError("Injected provenance migration failure")
        original(c, script)

    with (
        patch.object(Database, "_execute_sql_script", side_effect=fail),
        pytest.raises(RuntimeError, match="Injected provenance"),
    ):
        Database(db.path, strict_schema_contract=True)
    with db.connection() as c:
        assert (
            c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            == "105"
        )
        assert "origin_evidence" not in [r[1] for r in c.execute("PRAGMA table_info(goals)")]
    assert (
        GoalStore(Database(db.path, strict_schema_contract=True)).get(goal_id).origin_evidence
        == "legacy_baseline"
    )


def test_strict_schema_reopen_rejects_provenance_guard_removal(tmp_path: Path) -> None:
    db = Database(tmp_path / "db")
    with db.transaction() as c:
        c.execute("DROP TRIGGER prevent_goal_origin_update")
    with pytest.raises(SchemaContractError):
        Database(db.path, strict_schema_contract=True)
