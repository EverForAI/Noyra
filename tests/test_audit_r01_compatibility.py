from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.cognition.interaction import InteractionCognition
from noyra.cognition.settings import CognitionSettings
from noyra.cognition.social import RelationshipSocialCognition
from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.types import canonical_json, content_hash, utc_now
from noyra.interaction import InteractionStore
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.erasure import ConversationErasure
from noyra.interaction.integrity import InteractionIntegrity
from noyra.interaction.participants import Participant, ParticipantStore
from noyra.mind import MindEngine, RelationshipStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)
from support.historical import load_historical_fixtures, materialize_historical_database
from support.relationship_provenance import Evidence, inspect, open_readonly, snapshot

SUBJECT = "Noyra-r01-validation"
OTHER = "Noyra-r01-foreign"
FIXTURES = load_historical_fixtures(Path(__file__).parent / "fixtures/historical/manifest.json")


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "fixture.sqlite3")
    for subject in (SUBJECT, OTHER):
        IdentityStore(database).ensure(subject, content_hash(subject))
    return database


def person(
    db: Database,
    name: str,
    *,
    kind: str = "human",
    subject: str = SUBJECT,
    channel: str = "fixture",
) -> Participant:
    with db.transaction() as c:
        return ParticipantStore.source(
            c, subject, channel, "fixture-namespace", name, label="same nickname", entity_type=kind
        )


def message(db: Database, who: Participant, *, subject: str = SUBJECT) -> tuple[Any, str]:
    record = InteractionStore(db).receive(
        subject, who.channel, who.entity_id, "PRIVATE_SENTINEL synthetic message", participant=who
    )
    with db.connection() as c:
        event = c.execute(
            "SELECT event_id FROM events WHERE subject_id=? AND "
            "json_extract(payload_json,'$.interaction_id')=?",
            (subject, record.interaction_id),
        ).fetchone()[0]
    return record, str(event)


def relation(db: Database, who: Participant, sources: tuple[str, ...]) -> Any:
    return RelationshipStore(db).ensure(
        SUBJECT, who.relationship_type, who.entity_id, who.display_name, source_event_ids=sources
    )


def read_evidence(db: Database) -> tuple[Evidence, ...]:
    with open_readonly(db.path) as c:
        return inspect(c, SUBJECT)


def legacy_rows(db: Database) -> str:
    with open_readonly(db.path) as c:
        return snapshot(c)


def copy_database(db: Database, destination: Path) -> Database:
    with db.connection() as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
    return Database(destination, initialize=False)


def _probe_apply(
    db: Database,
    expected: str,
    observations: tuple[Evidence, ...],
    *,
    hook: Callable[[str], None] = lambda phase: None,
) -> None:
    """Fixture-only transaction prototype. NOT a migration installed by Noyra."""
    with closing(sqlite3.connect(db.path)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("BEGIN IMMEDIATE")
        try:
            if snapshot(c) != expected or inspect(c, SUBJECT) != observations:
                raise ValueError("stale survey; do not freeze an unreviewed value")
            c.execute(
                "CREATE TABLE IF NOT EXISTS r01_probe_bindings (relationship_id TEXT "
                "PRIMARY KEY REFERENCES relationships(relationship_id),subject_id "
                "TEXT NOT NULL REFERENCES subject_identity(subject_id),entity_type "
                "TEXT NOT NULL,entity_key TEXT NOT NULL,category TEXT NOT NULL "
                "CHECK(category IN "
                "('correlated_not_proven','unattested','review_required')),"
                "proven_at_creation INTEGER NOT NULL CHECK(proven_at_creation=0))"
            )
            c.execute(
                "CREATE TRIGGER IF NOT EXISTS r01_probe_identity_immutable BEFORE "
                "UPDATE OF relationship_id,subject_id,entity_type,entity_key ON "
                "relationships WHEN EXISTS(SELECT 1 FROM r01_probe_bindings b WHERE "
                "b.relationship_id=OLD.relationship_id) AND "
                "(NEW.relationship_id<>OLD.relationship_id OR "
                "NEW.subject_id<>OLD.subject_id OR NEW.entity_type<>OLD.entity_type "
                "OR NEW.entity_key<>OLD.entity_key) BEGIN SELECT RAISE(ABORT,'probe "
                "identity is immutable'); END"
            )
            for action in ("UPDATE", "DELETE"):
                c.execute(
                    f"CREATE TRIGGER IF NOT EXISTS r01_probe_no_{action.lower()} "
                    f"BEFORE {action} ON r01_probe_bindings BEGIN SELECT "
                    "RAISE(ABORT,'probe evidence is immutable'); END"
                )
            hook("prepared")
            for evidence in observations:
                values = (
                    evidence.relationship_id,
                    evidence.subject_id,
                    evidence.entity_type,
                    evidence.entity_key,
                    evidence.category,
                    0,
                )
                existing = c.execute(
                    "SELECT * FROM r01_probe_bindings WHERE relationship_id=?",
                    (evidence.relationship_id,),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != values:
                        raise ValueError("existing baseline differs; do not overwrite")
                else:
                    c.execute("INSERT INTO r01_probe_bindings VALUES(?,?,?,?,?,?)", values)
                hook("row_written")
            c.commit()
        except BaseException:
            c.rollback()
            raise


def test_real_cognition_creation_only_supports_correlation_not_original_identity_proof(
    db: Database,
) -> None:
    alice = person(db, "alice")
    record, _ = message(db, alice)
    provider = FakeProvider([])
    gateway = ModelGateway(
        provider, ModelLedger(db), model="fixture", limits=BudgetLimits(10, 100000, 100000, 100000)
    )
    cognition = InteractionCognition(db, SUBJECT, gateway, CognitionSettings())
    cognition._revise_relationship(record, "accepted", 1)
    (observed,) = read_evidence(db)
    assert observed.category == "correlated_not_proven"
    assert not observed.identity_at_creation_proven
    assert provider.requests == []
    with db.connection() as c:
        rows = c.execute("SELECT * FROM relationship_revisions").fetchall()
        assert len(rows) == 2
        assert "entity_key" not in set(rows[0].keys())
        assert "entity_type" not in set(rows[0].keys())
    assert EventStore(db).verify_chain(SUBJECT)["event_count"] == 1
    assert MindEngine(db).verify_integrity(SUBJECT)["relationships"] == 1


def test_indistinguishable_histories_defeat_automatic_backfill_even_with_valid_chain(
    db: Database, tmp_path: Path
) -> None:
    alice, bob = person(db, "alice"), person(db, "bob")
    _, event = message(db, alice)
    first = copy_database(db, tmp_path / "first.sqlite3")
    second = copy_database(db, tmp_path / "second.sqlite3")
    formation_at = utc_now()
    for database, original in ((first, alice), (second, bob)):
        with (
            patch("noyra.mind.relationship.new_id", side_effect=("rel-fixed", "rrev-fixed")),
            patch("noyra.mind.relationship.utc_now", return_value=formation_at),
        ):
            relation(database, original, (event,))
    # Legacy-only rows remain ambiguous, but schema 107 witnesses distinguish
    # the two histories. Explicitly bypass the new guard to inject corruption.
    with second.transaction() as c:
        c.execute("DROP TRIGGER prevent_relationship_identity_update")
        c.execute(
            "UPDATE relationships SET entity_key=? WHERE relationship_id='rel-fixed'",
            (alice.entity_id,),
        )
    assert legacy_rows(first) == legacy_rows(second)
    assert read_evidence(first) == read_evidence(second)
    assert read_evidence(first)[0].category == "correlated_not_proven"
    for database in (first, second):
        assert EventStore(database).verify_chain(SUBJECT)["event_count"] == 1
        if database is first:
            assert MindEngine(database).verify_integrity(SUBJECT)["relationships"] == 1
        else:
            with pytest.raises(IntegrityError):
                MindEngine(database).verify_integrity(SUBJECT)
        assert not read_evidence(database)[0].identity_at_creation_proven


@pytest.mark.parametrize("field", ["entity_key", "entity_type", "subject_id"])
def test_identity_changes_are_review_signals_without_inventing_repair(
    db: Database, field: str
) -> None:
    alice, bob = person(db, "alice"), person(db, "bob")
    _, event = message(db, alice)
    created = relation(db, alice, (event,))
    value = {"entity_key": bob.entity_id, "entity_type": "agent", "subject_id": OTHER}[field]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), db.transaction() as c:
        c.execute(
            f"UPDATE relationships SET {field}=? WHERE relationship_id=?",
            (value, created.relationship_id),
        )
    with db.transaction() as c:
        c.execute("DROP TRIGGER prevent_relationship_identity_update")
        c.execute(
            f"UPDATE relationships SET {field}=? WHERE relationship_id=?",
            (value, created.relationship_id),
        )
    with open_readonly(db.path) as c:
        (evidence,) = inspect(c, OTHER if field == "subject_id" else SUBJECT)
    assert evidence.category == "review_required"
    assert not evidence.identity_at_creation_proven
    with pytest.raises(IntegrityError):
        RelationshipStore(db).get(created.relationship_id)


@pytest.mark.parametrize("kind", ["human", "agent", "noyra", "unknown"])
def test_same_nickname_and_agent_mapping_do_not_merge_subjects(db: Database, kind: str) -> None:
    first, second = person(db, "one", kind=kind), person(db, "two", kind=kind)
    for who in (first, second):
        _, event = message(db, who)
        relation(db, who, (event,))
    observations = read_evidence(db)
    assert len(observations) == 2
    assert {r.entity_key for r in observations} == {first.entity_id, second.entity_id}
    assert all(r.category == "correlated_not_proven" for r in observations)
    assert all(r.entity_type == first.relationship_type for r in observations)


def test_generic_payload_claim_is_not_treated_as_identity_evidence(db: Database) -> None:
    who = person(db, "alice")
    event = EventStore(db).append(
        SUBJECT,
        "world_observation",
        "untrusted",
        {"entity_id": who.entity_id, "message": "PRIVATE_SENTINEL do not export"},
    )
    relation(db, who, (event.event_id,))
    (observation,) = read_evidence(db)
    assert observation.category == "unattested"
    assert "source_has_no_identity_contract" in observation.reasons
    assert "PRIVATE_SENTINEL" not in json.dumps(asdict(observation))


def test_mixed_sources_do_not_assign_one_senders_weights_to_another(db: Database) -> None:
    alice, bob = person(db, "alice"), person(db, "bob")
    events = tuple(message(db, who)[1] for who in (alice, bob))
    relation(db, alice, events)
    (observed,) = read_evidence(db)
    assert observed.category == "review_required"
    assert "multiple_source_entities" in observed.reasons


def test_legacy_same_address_on_different_channels_remains_unattested(db: Database) -> None:
    events = []
    for channel in ("matrix", "telegram"):
        record = InteractionStore(db).receive(SUBJECT, channel, "same-address", "synthetic")
        with db.connection() as c:
            events.append(
                c.execute(
                    "SELECT event_id FROM events WHERE "
                    "json_extract(payload_json,'$.interaction_id')=?",
                    (record.interaction_id,),
                ).fetchone()[0]
            )
    RelationshipStore(db).ensure(
        SUBJECT, "human", "same-address", "same nickname", source_event_ids=tuple(events)
    )
    (observed,) = read_evidence(db)
    assert observed.category == "unattested"
    assert "legacy_address_without_identity_namespace" in observed.reasons


def test_later_attribution_does_not_retroactively_prove_formation(db: Database) -> None:
    who = person(db, "alice")
    record = InteractionStore(db).receive(SUBJECT, "fixture", who.entity_id, "synthetic")
    with db.connection() as c:
        event = c.execute(
            "SELECT event_id FROM events WHERE json_extract(payload_json,'$.interaction_id')=?",
            (record.interaction_id,),
        ).fetchone()[0]
    relation(db, who, (event,))
    with (
        patch("noyra.interaction.participants.utc_now", return_value="2099-01-01T00:00:00+00:00"),
        db.transaction() as c,
    ):
        ParticipantStore.link(c, SUBJECT, record.interaction_id, who, "fixture")
    (observed,) = read_evidence(db)
    assert observed.category == "unattested"
    assert "attribution_added_after_formation" in observed.reasons


def test_private_withdrawal_stays_unavailable_and_no_body_is_recovered(db: Database) -> None:
    who = person(db, "alice", channel="web")
    window = ConversationStore(db).create(SUBJECT, "synthetic private window")
    record = InteractionStore(db).receive(
        SUBJECT,
        "web",
        "web-user",
        "PRIVATE_SENTINEL",
        participant=who,
        conversation_id=window["conversation_id"],
    )
    with db.connection() as c:
        event = c.execute(
            "SELECT event_id FROM events WHERE json_extract(payload_json,'$.interaction_id')=?",
            (record.interaction_id,),
        ).fetchone()[0]
    relation(db, who, (event,))
    assessment = ConversationErasure(db).assess(SUBJECT, window["conversation_id"])
    assert assessment["status"] == "review_required"
    assert assessment["deletion_available"] is False
    with db.transaction() as c:
        state = dict(
            c.execute(
                "SELECT * FROM private_conversations WHERE conversation_id=?",
                (window["conversation_id"],),
            ).fetchone()
        )
        state.pop("state_hash")
        state["status"] = "deleting"
        c.execute(
            "UPDATE private_conversations SET status=?,state_hash=? WHERE conversation_id=?",
            ("deleting", content_hash(state), window["conversation_id"]),
        )
    (observed,) = read_evidence(db)
    assert observed.category == "unattested"
    assert "private_source_withdrawn" in observed.reasons
    assert "PRIVATE_SENTINEL" not in json.dumps(asdict(observed))


def test_disabled_participant_is_not_reassigned_to_a_new_entity(db: Database) -> None:
    who = person(db, "alice")
    _, event = message(db, who)
    relation(db, who, (event,))
    with db.transaction() as c:
        c.execute(
            "UPDATE participant_entities SET status='disabled' WHERE entity_id=?", (who.entity_id,)
        )
    (observed,) = read_evidence(db)
    assert observed.entity_key == who.entity_id
    assert observed.category == "unattested"
    assert "participant_disabled" in observed.reasons


def test_readonly_survey_does_not_create_or_write_database(db: Database, tmp_path: Path) -> None:
    absent = tmp_path / "absent.sqlite3"
    with pytest.raises(ValueError, match="existing"), open_readonly(absent):
        pytest.fail("missing database must not open")
    assert not absent.exists()
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    with open_readonly(db.path) as c:
        assert inspect(c, SUBJECT) == ()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("CREATE TABLE should_not_exist(value)")
    assert hashlib.sha256(db.path.read_bytes()).hexdigest() == before


def test_survey_refuses_truncation_instead_of_freezing_a_partial_page(db: Database) -> None:
    for key in ("alice", "bob"):
        who = person(db, key)
        _, event = message(db, who)
        relation(db, who, (event,))
    with open_readonly(db.path) as c, pytest.raises(ValueError, match="budget exceeded"):
        inspect(c, SUBJECT, limit=1)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.name)
def test_real_historical_schemas_with_seeded_legacy_relations_keep_state_through_current_upgrade(
    tmp_path: Path, fixture: Any
) -> None:
    path = materialize_historical_database(fixture, tmp_path)
    with closing(sqlite3.connect(path)) as c:
        c.row_factory = sqlite3.Row
        assert c.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 0
        # Deliberately synthetic legacy row; never present this as production history.
        digest = RelationshipStore._state_hash(0, 0, 0, 0, {})
        now = "2026-08-01T00:00:00+00:00"
        c.execute(
            "INSERT INTO relationships VALUES(?,?,?,?,?,0,0,0,0,?,?,?,1,?,?)",
            (
                "rel-legacy",
                fixture.subject_id,
                "human",
                "legacy-address",
                "old nickname",
                "{}",
                content_hash({}),
                digest,
                now,
                now,
            ),
        )
        c.execute(
            "INSERT INTO relationship_revisions VALUES(?,?,1,0,0,0,0,?,?,?,?,?,?)",
            (
                "rrev-legacy",
                "rel-legacy",
                "{}",
                content_hash({}),
                digest,
                "fixture",
                canonical_json([fixture.anchor_event_id]),
                now,
            ),
        )
        c.commit()
        before = snapshot(c)
    with open_readonly(path) as c:
        (old,) = inspect(c, fixture.subject_id)
        assert old.category in {"unattested", "review_required"}
    database = Database(path)
    with open_readonly(path) as c:
        assert snapshot(c) == before
        (new,) = inspect(c, fixture.subject_id)
        assert not new.identity_at_creation_proven
        assert new.category in {"unattested", "review_required"}
        if fixture.schema_version <= 31:
            assert "event_chain_invalid" in new.reasons
    assert RelationshipStore(database).get("rel-legacy").entity_key == "legacy-address"
    destination = os.environ.get("NOYRA_R01_VALIDATION_EVIDENCE_DIR")
    if destination:
        target = Path(destination)
        target.mkdir(parents=True, exist_ok=True)
        (target / (fixture.name + "-compatibility.json")).write_text(
            json.dumps(
                {
                    "fixture": fixture.name,
                    "old_schema": fixture.schema_version,
                    "real_fixture_relationship_count": 0,
                    "synthetic_seeded_relationships": 1,
                    "old_category": old.category,
                    "old_reasons": old.reasons,
                    "new_category": new.category,
                    "new_reasons": new.reasons,
                    "legacy_rows_preserved": True,
                    "identity_at_creation_proven": False,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


@pytest.mark.parametrize("phase", ["prepared", "row_written"])
def test_additive_prototype_rolls_back_and_retries_without_rewriting_history(
    db: Database, tmp_path: Path, phase: str
) -> None:
    who = person(db, "alice")
    _, event = message(db, who)
    relation(db, who, (event,))
    replica = copy_database(db, tmp_path / "probe.sqlite3")
    before = legacy_rows(replica)
    observed = read_evidence(replica)

    def fail(at: str) -> None:
        if at == phase:
            raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected"):
        _probe_apply(replica, before, observed, hook=fail)
    with replica.connection() as c:
        assert (
            c.execute("SELECT name FROM sqlite_master WHERE name='r01_probe_bindings'").fetchone()
            is None
        )
    assert legacy_rows(replica) == before
    _probe_apply(replica, before, observed)
    _probe_apply(replica, before, observed)
    with replica.connection() as c:
        assert c.execute(
            "SELECT COUNT(*),SUM(proven_at_creation) FROM r01_probe_bindings"
        ).fetchone()[:] == (1, 0)
    assert legacy_rows(replica) == before
    assert legacy_rows(db) == before


@pytest.mark.parametrize(
    "field,value",
    [("subject_id", OTHER), ("entity_key", "another-entity"), ("entity_type", "agent")],
)
def test_prototype_rejects_identity_mutation_but_preserves_ordinary_revision(
    db: Database, tmp_path: Path, field: str, value: str
) -> None:
    who = person(db, "alice")
    _, event = message(db, who)
    created = relation(db, who, (event,))
    replica = copy_database(db, tmp_path / "probe.sqlite3")
    _probe_apply(replica, legacy_rows(replica), read_evidence(replica))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), replica.transaction() as c:
        c.execute(
            f"UPDATE relationships SET {field}=? WHERE relationship_id=?",
            (value, created.relationship_id),
        )
    updated = RelationshipStore(replica).revise(
        created.relationship_id,
        trust=0.2,
        affinity=0.1,
        conflict=0,
        familiarity=0.2,
        boundaries={"no_contact": True},
        reason="ordinary revision",
        source_event_ids=(event,),
    )
    assert updated.boundaries["no_contact"] is True
    assert updated.entity_key == who.entity_id
    assert MindEngine(replica).verify_integrity(SUBJECT)["relationships"] == 1


@pytest.mark.parametrize("changed", ["source_status", "identity"])
def test_changed_source_or_relation_between_survey_and_cutover_is_rejected(
    db: Database, changed: str
) -> None:
    who = person(db, "alice")
    _, event = message(db, who)
    relation(db, who, (event,))
    before, observed = legacy_rows(db), read_evidence(db)
    with db.transaction() as c:
        if changed == "source_status":
            c.execute(
                "UPDATE participant_entities SET status='disabled' WHERE entity_id=?",
                (who.entity_id,),
            )
        else:
            c.execute("DROP TRIGGER prevent_relationship_identity_update")
            c.execute("UPDATE relationships SET entity_key='changed-before-cutover'")
    after = legacy_rows(db)
    with pytest.raises(ValueError, match="stale survey"):
        _probe_apply(db, before, observed)
    assert legacy_rows(db) == after
    with db.connection() as c:
        assert (
            c.execute("SELECT 1 FROM sqlite_master WHERE name='r01_probe_bindings'").fetchone()
            is None
        )


def test_prototype_survives_process_crash_with_transaction_rolled_back(
    db: Database, tmp_path: Path
) -> None:
    who = person(db, "alice")
    _, event = message(db, who)
    relation(db, who, (event,))
    replica = copy_database(db, tmp_path / "crash.sqlite3")
    program = """import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from noyra.core import Database
from test_audit_r01_compatibility import _probe_apply,legacy_rows,read_evidence
replica=Database(Path(sys.argv[2]),initialize=False)
def crash(phase):
    if phase=='row_written': os._exit(73)
_probe_apply(replica,legacy_rows(replica),read_evidence(replica),hook=crash)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(Path(__file__).parent), str(replica.path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 73, result.stdout + result.stderr
    assert legacy_rows(replica) == legacy_rows(db)
    _probe_apply(replica, legacy_rows(replica), read_evidence(replica))
    with replica.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM r01_probe_bindings").fetchone()[0] == 1


def test_prototype_backup_roundtrip_and_export_fail_closed_boundary(
    db: Database, tmp_path: Path
) -> None:
    who = person(db, "alice")
    _, event = message(db, who)
    created = relation(db, who, (event,))
    before = legacy_rows(db)
    artifact = RuntimeLogExporter(db).export(SUBJECT, actor="fixture")
    with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
        assert any("relationships" in name for name in archive.namelist())
        assert any("relationship_revisions" in name for name in archive.namelist())
    replica = copy_database(db, tmp_path / "probe.sqlite3")
    _probe_apply(replica, before, read_evidence(replica))
    restored = copy_database(replica, tmp_path / "restored.sqlite3")
    assert legacy_rows(restored) == before
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), restored.transaction() as c:
        c.execute(
            "UPDATE relationships SET entity_key='wrong' WHERE relationship_id=?",
            (created.relationship_id,),
        )
    # Product exporter must NOT silently drop a newly introduced identity table.
    with pytest.raises(RuntimeError, match="ownership graph does not cover"):
        RuntimeLogExporter(replica).export(SUBJECT, actor="fixture")


def test_fixture_category_distribution_is_explicitly_not_production_statistics(
    db: Database, tmp_path: Path
) -> None:
    good, bad, plain = person(db, "good"), person(db, "bad"), person(db, "plain")
    _, event = message(db, good)
    relation(db, good, (event,))
    relation(db, bad, (event,))
    generic = EventStore(db).append(SUBJECT, "observation", "fixture", {})
    relation(db, plain, (generic.event_id,))
    data = read_evidence(db)
    counts = {
        label: sum(row.category == label for row in data)
        for label in ("correlated_not_proven", "unattested", "review_required")
    }
    assert counts == {"correlated_not_proven": 1, "unattested": 1, "review_required": 1}
    assert all(not row.identity_at_creation_proven for row in data)
    target = os.environ.get("NOYRA_R01_VALIDATION_EVIDENCE_DIR")
    if target:
        output = Path(target)
        output.mkdir(parents=True, exist_ok=True)
        (output / "synthetic-distribution.json").write_text(
            json.dumps(
                {
                    "scope": "synthetic fixtures only; production not accessed",
                    "counts": counts,
                    "original_identity_proven": 0,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_later_social_snapshot_corroborates_only_its_own_time_not_formation(
    db: Database,
) -> None:
    alice, bob = person(db, "alice"), person(db, "bob")
    _, event = message(db, alice)
    message(db, bob)
    created = relation(db, alice, (event,))
    provider = FakeProvider(
        [
            ProviderResponse(
                json.dumps(
                    {
                        "disposition": "wait",
                        "relationship_id": created.relationship_id,
                        "topic": "fixture",
                        "rationale": "synthetic voluntary wait",
                        "content": None,
                        "evidence_event_ids": [event],
                    }
                ),
                ModelUsage(10, 10),
            )
        ]
    )
    gateway = ModelGateway(
        provider, ModelLedger(db), model="fixture", limits=BudgetLimits(10, 100000, 100000, 100000)
    )
    social = RelationshipSocialCognition(db, SUBJECT, gateway, CognitionSettings())
    assert await social.run_due() == "relationship_social_wait"
    with db.connection() as c:
        run = c.execute("SELECT * FROM relationship_social_runs").fetchone()
        InteractionIntegrity._verify_social_run(c, SUBJECT, run)
    (observed,) = read_evidence(db)
    assert observed.category == "correlated_not_proven"
    assert observed.matching_social_snapshots == 1
    assert not observed.identity_at_creation_proven
    with db.transaction() as c:
        c.execute("DROP TRIGGER prevent_relationship_identity_update")
        c.execute(
            "UPDATE relationships SET entity_key=? WHERE relationship_id=?",
            (bob.entity_id, created.relationship_id),
        )
    (observed,) = read_evidence(db)
    assert observed.category == "review_required"
    assert "social_snapshot_disagrees" in observed.reasons
    # Schema 107 also detects corruption against the creation-time binding.
    with db.connection() as c, pytest.raises(IntegrityError, match="identity binding mismatch"):
        InteractionIntegrity._verify_social_run(
            c, SUBJECT, c.execute("SELECT * FROM relationship_social_runs").fetchone()
        )
    assert len(provider.requests) == 1


def test_archived_event_payload_is_not_rehydrated_for_identity_guessing(db: Database) -> None:
    who = person(db, "alice")
    _, event = message(db, who)
    relation(db, who, (event,))
    with db.transaction() as c:
        c.execute(
            "UPDATE events SET payload_json='{}',payload_archive_key='fixture-not-read',"
            "payload_archived_at=? WHERE event_id=?",
            (utc_now(), event),
        )
    with patch.object(
        EventStore,
        "payload_from_row",
        side_effect=AssertionError("no archive/private payload reads"),
    ):
        (observed,) = read_evidence(db)
    assert observed.category == "unattested"
    assert "archived_payload_not_read" in observed.reasons


def test_original_source_is_not_replaced_by_a_more_recent_matching_revision(db: Database) -> None:
    alice, bob = person(db, "alice"), person(db, "bob")
    events = [message(db, p)[1] for p in (alice, bob)]
    created = relation(db, alice, (events[0],))
    RelationshipStore(db).revise(
        created.relationship_id,
        trust=0.8,
        affinity=0.8,
        conflict=0,
        familiarity=0.9,
        boundaries={},
        reason="later source",
        source_event_ids=(events[1],),
    )
    with db.transaction() as c:
        c.execute("DROP TRIGGER prevent_relationship_identity_update")
        c.execute(
            "UPDATE relationships SET entity_key=? WHERE relationship_id=?",
            (bob.entity_id, created.relationship_id),
        )
    (observed,) = read_evidence(db)
    assert observed.category == "review_required"
    assert "multiple_source_entities" in observed.reasons


def test_old_binary_requires_forward_schema_gate_before_a_real_rollout(
    db: Database, tmp_path: Path
) -> None:
    from noyra.core.database import CURRENT_SCHEMA_VERSION

    replica = copy_database(db, tmp_path / "next-schema.sqlite3")
    with replica.transaction() as c:
        c.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(CURRENT_SCHEMA_VERSION + 1),),
        )
    with pytest.raises(RuntimeError, match="newer than runtime"):
        Database(replica.path)


def test_prototype_baseline_cannot_be_edited_deleted_or_promoted_to_birth_proof(
    db: Database,
) -> None:
    who = person(db, "alice")
    _, event = message(db, who)
    relation(db, who, (event,))
    before = legacy_rows(db)
    _probe_apply(db, before, read_evidence(db))
    for sql in (
        "UPDATE r01_probe_bindings SET category='correlated_not_proven'",
        "UPDATE r01_probe_bindings SET proven_at_creation=1",
        "DELETE FROM r01_probe_bindings",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"), db.transaction() as c:
            c.execute(sql)
    assert legacy_rows(db) == before
