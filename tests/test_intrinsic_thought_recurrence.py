from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import patch

import pytest

import test_intrinsic_thought as fixtures
from noyra.cognition import IntrinsicThought
from noyra.cognition.thought import ThoughtAgendaRecord
from noyra.core import Database, EventStore, IdentityStore, IntegrityRegistry
from noyra.core.database import CURRENT_SCHEMA_VERSION, MIGRATIONS
from noyra.core.errors import IntegrityError, SchemaContractError
from noyra.core.schema.contracts import _SCHEMA_DDL_FINGERPRINTS, _SCHEMA_STRUCTURE_FINGERPRINTS
from noyra.core.types import content_hash
from noyra.mind import GoalCandidate, GoalStore
from noyra.model import FakeProvider
from noyra.model.errors import ProviderCallError


@pytest.fixture
def fixture() -> Iterator[fixtures.IntrinsicThoughtTestCase]:
    case = fixtures.IntrinsicThoughtTestCase()
    case.setUp()
    try:
        with patch("noyra.model.ledger.utc_now", side_effect=lambda: case.clock_value):
            yield case
    finally:
        case.tearDown()


def advance(fixture: fixtures.IntrinsicThoughtTestCase, minutes: int = 5) -> None:
    fixture.clock_value = (
        datetime.fromisoformat(fixture.clock_value) + timedelta(minutes=minutes)
    ).isoformat(timespec="milliseconds")


def thought_for(
    fixture: fixtures.IntrinsicThoughtTestCase,
    payloads: list[Mapping[str, object]] | None = None,
    **settings: object,
) -> IntrinsicThought:
    if payloads is not None:
        event = fixture.events.append(
            fixture.subject_id, "experience", "synthetic-test", {"condition": "no goals"}
        )
        payloads = [dict(payload, source_event_ids=[event.event_id]) for payload in payloads]
    gateway, _ = fixture.gateway([] if payloads is None else payloads)
    return IntrinsicThought(
        fixture.kernel.database,
        fixture.subject_id,
        gateway,
        fixture.settings(**settings),
        clock=lambda: fixture.clock_value,
    )


def absence(thought: IntrinsicThought) -> ThoughtAgendaRecord:
    return next(item for item in thought.agenda() if item.source_id == thought._absence_source_id())


def create_goal(fixture: fixtures.IntrinsicThoughtTestCase) -> tuple[str, str]:
    event = fixture.events.append(
        fixture.subject_id, "experience", "synthetic-test", {"condition": "goal appears"}
    )
    goal = GoalStore(fixture.kernel.database).create_candidate(
        fixture.subject_id,
        GoalCandidate(
            title="Investigate evidence",
            description="Optional intrinsic investigation.",
            origin="self",
            priority=0.3,
            commitment=0.2,
            motive_emotion="curiosity",
        ),
        causal_source_ids=(event.event_id,),
        reason="synthetic recurrence fixture",
    )
    return goal.goal_id, event.event_id


def finish_goal(fixture: fixtures.IntrinsicThoughtTestCase, goal: tuple[str, str]) -> None:
    store = GoalStore(fixture.kernel.database)
    current = store.get(goal[0])
    store.revise(
        current.goal_id,
        status="abandoned",
        priority=current.priority,
        commitment=current.commitment,
        progress=current.progress,
        emotional_pressure=current.emotional_pressure,
        reason="synthetic investigation ended",
        causal_source_ids=(goal[1],),
        expected_revision=current.current_revision,
    )


def goal_cycle(fixture: fixtures.IntrinsicThoughtTestCase, thought: IntrinsicThought) -> None:
    goal = create_goal(fixture)
    advance(fixture)
    assert thought.discover_agenda()
    assert absence(thought).status == "resolved"
    finish_goal(fixture, goal)
    advance(fixture)
    assert thought.discover_agenda()


def proposal(
    disposition: str = "reflect", *, insight: str = "Waiting remains optional."
) -> dict[str, Any]:
    return {
        "disposition": disposition,
        "summary": "Considered the current absence of goals.",
        "insight": insight,
        "next_question": None,
        "source_event_ids": [],
        "source_memory_ids": [],
        "source_belief_ids": [],
        "source_goal_ids": [],
        "source_relationship_ids": [],
        "goal_candidate": None,
    }


def test_terminal_parent_goal_retires_its_current_tension_agenda(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(fixture)
    goal = create_goal(fixture)
    advance(fixture)
    assert thought.discover_agenda()
    original = next(item for item in thought.agenda() if item.source_id == goal[0])
    assert original.status == "open"

    finish_goal(fixture, goal)
    assert thought.inspect_agenda(original.agenda_id).status == "source_terminal"

    advance(fixture)
    assert thought.discover_agenda()
    retired = next(item for item in thought.agenda() if item.agenda_id == original.agenda_id)
    assert retired.status == "abandoned"
    assert retired.cooldown_until is None
    with fixture.kernel.database.connection() as connection:
        revision = connection.execute(
            "SELECT * FROM thought_agenda_revisions WHERE agenda_id=? ORDER BY rowid DESC LIMIT 1",
            (original.agenda_id,),
        ).fetchone()
    assert revision is not None
    assert revision["old_status"] == "open"
    assert revision["new_status"] == "abandoned"
    assert revision["reason"] == "parent goal abandoned; agenda retired"
    thought.verify_integrity()


@pytest.mark.asyncio
async def test_terminal_parent_goal_is_not_executed_or_replaced(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(fixture, [proposal()])
    goal = create_goal(fixture)
    advance(fixture)
    assert thought.discover_agenda()
    original = next(item for item in thought.agenda() if item.source_id == goal[0])
    finish_goal(fixture, goal)

    assert await thought.run_due() is None
    with fixture.kernel.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM model_calls WHERE subject_id=?",
                (fixture.subject_id,),
            ).fetchone()[0]
            == 0
        )
    assert thought.inspect_agenda(original.agenda_id).status == "source_terminal"


def test_absence_recurrence_reuses_the_same_agenda(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(fixture)
    thought.discover_agenda()
    first = absence(thought)
    goal_cycle(fixture, thought)
    returned = absence(thought)
    assert returned.agenda_id == first.agenda_id
    assert returned.status == "open"
    assert returned.recurrence_count == first.recurrence_count + 1
    with patch.object(thought, "agenda", return_value=[returned]):
        assert thought._select_agenda() == returned
    assert thought.verify_integrity()["thought_episodes"] == 0


@pytest.mark.asyncio
async def test_returning_condition_preserves_cooldown_and_no_change(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(fixture, [proposal()], max_thought_no_change_streak=1)
    assert await thought.run_due() == "intrinsic_thought_reflect"
    original = absence(thought)
    assert original.status == "cooling"
    goal_cycle(fixture, thought)
    returned = absence(thought)
    assert returned.status == "cooling"
    assert returned.cooldown_until == original.cooldown_until
    assert returned.consecutive_no_change == original.consecutive_no_change == 1
    assert thought._calls_today() == 1
    assert thought._episode_count(returned.agenda_id) == 1
    with patch.object(thought, "agenda", return_value=[returned]):
        assert await thought.run_due() is None
    thought.verify_integrity()


def revisions(thought: IntrinsicThought) -> list[dict[str, Any]]:
    with thought.database.connection() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT r.* FROM thought_agenda_revisions r JOIN thought_agenda_items a "
                "ON a.agenda_id=r.agenda_id WHERE a.subject_id=? AND a.source_id=? "
                "ORDER BY r.rowid",
                (thought.subject_id, thought._absence_source_id()),
            )
        ]


def test_multiple_cycles_restart_and_stable_scans_do_not_duplicate_occurrences(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(fixture)
    thought.discover_agenda()
    original = absence(thought)
    for cycle in range(3):
        goal_cycle(fixture, thought)
        returned = absence(thought)
        assert returned.agenda_id == original.agenda_id
        assert returned.status == "open"
        assert returned.recurrence_count == cycle + 2
        original_revisions = revisions(thought)
        # An unrelated event/global state change is not a new condition cycle.
        fixture.events.append(fixture.subject_id, "experience", "synthetic-test", {"cycle": cycle})
        advance(fixture)
        thought.discover_agenda()
        restarted = thought_for(fixture)
        assert restarted.discover_agenda()
        assert absence(restarted) == returned
        assert revisions(restarted) == original_revisions
        thought = restarted
    assert len(revisions(thought)) == 7
    report = IntegrityRegistry().run(
        thought.database,
        thought.subject_id,
        thought.database.path.parent,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=15,
        check_ids=("cognition.thought",),
    )
    assert report.status == "ok"
    assert report.checks[0].details["thought_agenda_items"] == 4
    with thought.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM thought_agenda_items WHERE subject_id=? AND source_id=?",
                (thought.subject_id, thought._absence_source_id()),
            ).fetchone()[0]
            == 1
        )
    thought.verify_integrity()


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["resolve", "abandon"])
async def test_autonomous_terminal_decisions_are_not_reopened(
    fixture: fixtures.IntrinsicThoughtTestCase,
    disposition: str,
) -> None:
    thought = thought_for(fixture, [proposal(disposition)])
    assert await thought.run_due() == f"intrinsic_thought_{disposition}"
    original = absence(thought)
    for _ in range(2):
        advance(fixture)
        thought.discover_agenda()
    goal = create_goal(fixture)
    advance(fixture)
    thought.discover_agenda()
    finish_goal(fixture, goal)
    advance(fixture)
    thought.discover_agenda()
    restarted = thought_for(fixture)
    restarted.discover_agenda()
    assert absence(restarted) == original
    assert len(revisions(thought)) == 2
    assert thought._calls_today() == 1
    thought.verify_integrity()


@pytest.mark.asyncio
async def test_expired_cooldown_opens_without_resetting_streak_or_history(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(fixture, [proposal()], max_thought_no_change_streak=1)
    assert await thought.run_due() == "intrinsic_thought_reflect"
    original = absence(thought)
    goal = create_goal(fixture)
    advance(fixture)
    thought.discover_agenda()
    assert absence(thought).cooldown_until == original.cooldown_until
    finish_goal(fixture, goal)
    advance(fixture, 120)
    thought.discover_agenda()
    returned = absence(thought)
    assert returned.status == "open"
    assert returned.cooldown_until is None
    assert returned.consecutive_no_change == original.consecutive_no_change == 1
    assert thought._episode_count(returned.agenda_id) == 1
    assert thought._calls_today() == 1
    thought.verify_integrity()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["daily_budget", "minimum_interval"])
async def test_condition_cycle_does_not_bypass_existing_call_limits(
    fixture: fixtures.IntrinsicThoughtTestCase,
    gate: str,
) -> None:
    settings = (
        {"max_thought_model_calls_per_day": 1}
        if gate == "daily_budget"
        else {"thought_interval_seconds": 3600}
    )
    thought = thought_for(fixture, [proposal()], **settings)
    assert await thought.run_due() == "intrinsic_thought_reflect"
    original = absence(thought)
    goal_cycle(fixture, thought)
    returned = absence(thought)
    assert returned.status == "open"
    assert returned.agenda_id == original.agenda_id
    assert returned.consecutive_no_change == original.consecutive_no_change == 1
    assert thought._calls_today() == 1
    restarted = thought_for(fixture, None, **settings)
    restarted.discover_agenda()
    assert restarted._calls_today() == 1
    if gate == "minimum_interval":
        assert not restarted._is_due(returned)
    with patch.object(restarted, "agenda", return_value=[returned]):
        assert await restarted.run_due() is None
    assert isinstance(thought.gateway.provider, FakeProvider)
    assert len(thought.gateway.provider.requests) == 1
    assert isinstance(restarted.gateway.provider, FakeProvider)
    assert restarted.gateway.provider.requests == []
    restarted.verify_integrity()


@pytest.mark.asyncio
async def test_second_occurrence_uses_the_next_existing_episode_key(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(
        fixture, [proposal(), proposal(insight="The changed circumstances allow a new framing.")]
    )
    assert await thought.run_due() == "intrinsic_thought_reflect"
    original = absence(thought)
    goal_cycle(fixture, thought)
    returned = absence(thought)
    assert thought._episode_count(returned.agenda_id) == 1
    with patch.object(thought, "agenda", return_value=[returned]):
        assert await thought.run_due() == "intrinsic_thought_reflect"
    assert thought._episode_count(original.agenda_id) == 2
    assert thought._calls_today() == 2
    with thought.database.connection() as connection:
        keys = [
            row[0]
            for row in connection.execute(
                "SELECT idempotency_key FROM thought_episodes WHERE agenda_id=? ORDER BY rowid",
                (original.agenda_id,),
            )
        ]
    assert keys == [
        f"intrinsic-thought:{original.agenda_id}:0",
        f"intrinsic-thought:{original.agenda_id}:1",
    ]
    thought.verify_integrity()


@pytest.mark.asyncio
async def test_unknown_model_outcome_is_not_reissued_after_condition_recurrence(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(fixture)
    provider = FakeProvider(
        [ProviderCallError("synthetic_unknown", retryable=False, outcome_unknown=True)]
    )
    thought.gateway.provider = provider
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    first = absence(thought)
    assert thought._episode_count(first.agenda_id) == 0
    goal_cycle(fixture, thought)
    returned = absence(thought)
    assert returned.status == "open"
    with patch.object(thought, "agenda", return_value=[returned]):
        assert await thought.run_due() is None
    assert len(provider.requests) == 1
    assert thought._calls_today() == 1
    with thought.database.connection() as connection:
        assert connection.execute("SELECT status FROM model_calls").fetchone()[0] == "unknown"


@pytest.mark.asyncio
async def test_legacy_automatic_resolution_recovers_only_proven_cooldown(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(fixture, [proposal()], max_thought_no_change_streak=1)
    assert await thought.run_due() == "intrinsic_thought_reflect"
    original = absence(thought)
    goal = create_goal(fixture)
    advance(fixture)
    # Reproduce the prior implementation's persisted resolution, which cleared
    # cooldown_until. Do not rewrite or backfill the immutable earlier revision.
    with thought.database.transaction() as connection:
        state_hash = thought._agenda_hash(
            original.source_type,
            original.source_id,
            original.topic,
            original.urgency,
            original.novelty,
            original.emotional_weight,
            original.recurrence_count,
            original.consecutive_no_change,
            "resolved",
            None,
        )
        connection.execute(
            "UPDATE thought_agenda_items SET status='resolved',cooldown_until=NULL,"
            "state_hash=?,updated_at=? WHERE agenda_id=?",
            (state_hash, fixture.clock_value, original.agenda_id),
        )
        thought._insert_agenda_revision(
            connection,
            original.agenda_id,
            old_status="cooling",
            new_status="resolved",
            old_recurrence=original.recurrence_count,
            new_recurrence=original.recurrence_count,
            old_no_change=original.consecutive_no_change,
            new_no_change=original.consecutive_no_change,
            cooldown_until=None,
            reason="internal absence condition no longer present",
            created_at=fixture.clock_value,
        )
    old_history = revisions(thought)
    finish_goal(fixture, goal)
    advance(fixture)
    restarted = thought_for(fixture)
    restarted.discover_agenda()
    returned = absence(restarted)
    assert returned.agenda_id == original.agenda_id
    assert returned.status == "cooling"
    assert returned.cooldown_until == original.cooldown_until
    assert returned.consecutive_no_change == original.consecutive_no_change
    assert revisions(restarted)[:-1] == old_history
    restarted.verify_integrity()


def test_candidate_read_and_agenda_write_share_the_same_transaction(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(fixture)
    original = thought._agenda_candidates
    observed = []

    def capture(*, connection: Any = None) -> list[dict[str, Any]]:
        observed.append(connection is not None and connection.in_transaction)
        return original(connection=connection)

    with patch.object(thought, "_agenda_candidates", side_effect=capture):
        assert thought.discover_agenda()
    assert observed == [True]
    thought.verify_integrity()


def test_two_discoverers_cannot_double_reopen_one_occurrence(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(fixture)
    thought.discover_agenda()
    original = absence(thought)
    goal = create_goal(fixture)
    advance(fixture)
    thought.discover_agenda()
    finish_goal(fixture, goal)
    advance(fixture)
    discoverers = [thought_for(fixture), thought_for(fixture)]
    barrier = Barrier(2)

    def discover(item: IntrinsicThought) -> bool:
        barrier.wait(timeout=15)
        return item.discover_agenda()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(discover, item) for item in discoverers]
        assert [future.result(timeout=15) for future in futures] == [True, True]
    returned = absence(thought)
    assert returned.agenda_id == original.agenda_id
    assert returned.recurrence_count == 2
    assert len(revisions(thought)) == 3
    thought.verify_integrity()


def test_failed_reopen_rolls_back_state_and_revision_atomically(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(fixture)
    thought.discover_agenda()
    goal = create_goal(fixture)
    advance(fixture)
    thought.discover_agenda()
    finish_goal(fixture, goal)
    advance(fixture)
    before = absence(thought)
    history = revisions(thought)
    with (
        patch.object(
            thought, "_insert_agenda_revision", side_effect=RuntimeError("synthetic failure")
        ),
        pytest.raises(RuntimeError, match="synthetic failure"),
    ):
        thought.discover_agenda()
    assert absence(thought) == before
    assert revisions(thought) == history
    assert thought.discover_agenda()
    assert absence(thought).recurrence_count == 2
    thought.verify_integrity()


@pytest.mark.parametrize(
    "damage", ["latest_hash", "previous_hash", "missing_predecessor", "valid_hash_broken_chain"]
)
def test_invalid_resolution_evidence_is_not_used_to_reopen(
    fixture: fixtures.IntrinsicThoughtTestCase,
    damage: str,
) -> None:
    thought = thought_for(fixture)
    thought.discover_agenda()
    goal = create_goal(fixture)
    advance(fixture)
    thought.discover_agenda()
    history = revisions(thought)
    with thought.database.transaction() as connection:
        if damage == "missing_predecessor":
            connection.execute("DROP TRIGGER prevent_thought_agenda_revision_delete")
            connection.execute(
                "DELETE FROM thought_agenda_revisions WHERE revision_id=?",
                (history[0]["revision_id"],),
            )
        else:
            connection.execute("DROP TRIGGER prevent_thought_agenda_revision_update")
            target = history[-1] if damage == "latest_hash" else history[-2]
            if damage == "valid_hash_broken_chain":
                target["new_recurrence_count"] += 1
                state_hash = thought._agenda_revision_hash(
                    target["old_status"],
                    target["new_status"],
                    target["old_recurrence_count"],
                    target["new_recurrence_count"],
                    target["old_consecutive_no_change"],
                    target["new_consecutive_no_change"],
                    target["cooldown_until"],
                    target["reason"],
                )
                connection.execute(
                    "UPDATE thought_agenda_revisions SET new_recurrence_count=?,state_hash=? "
                    "WHERE revision_id=?",
                    (target["new_recurrence_count"], state_hash, target["revision_id"]),
                )
            else:
                connection.execute(
                    "UPDATE thought_agenda_revisions SET state_hash=? WHERE revision_id=?",
                    ("0" * 64, target["revision_id"]),
                )
    finish_goal(fixture, goal)
    advance(fixture)
    before = absence(thought)
    with pytest.raises(IntegrityError):
        thought.discover_agenda()
    assert absence(thought) == before


def test_subjects_do_not_share_absence_lifecycle(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    thought = thought_for(fixture)
    thought.discover_agenda()
    other_id = "other-intrinsic-subject"
    IdentityStore(thought.database).ensure(other_id, content_hash(other_id))
    other = IntrinsicThought(
        thought.database,
        other_id,
        thought.gateway,
        thought.settings,
        clock=lambda: fixture.clock_value,
    )
    other.discover_agenda()
    original = absence(other)
    history = revisions(other)
    goal_cycle(fixture, thought)
    other.discover_agenda()
    assert absence(other) == original
    assert revisions(other) == history
    assert other._absence_source_id() != thought._absence_source_id()
    thought.verify_integrity()
    other.verify_integrity()


def business_rows(database: Database) -> dict[str, list[tuple[Any, ...]]]:
    with database.connection() as connection:
        return {
            table: [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in (
                "thought_agenda_items",
                "thought_agenda_revisions",
                "events",
                "goals",
                "goal_revisions",
            )
        }


@pytest.mark.parametrize("inject_failure", [False, True])
def test_v109_index_upgrade_preserves_history_and_is_recoverable(
    fixture: fixtures.IntrinsicThoughtTestCase,
    tmp_path: Path,
    inject_failure: bool,
) -> None:
    gateway, provider = fixture.gateway([])
    with patch("noyra.core.database.CURRENT_SCHEMA_VERSION", 109):
        old = Database(tmp_path / "v109.sqlite3", strict_schema_contract=True)
        IdentityStore(old).ensure(fixture.subject_id, content_hash("legacy-thought-seed"))
        old_thought = IntrinsicThought(
            old,
            fixture.subject_id,
            gateway,
            fixture.settings(),
            clock=lambda: fixture.clock_value,
        )
        old_thought.discover_agenda()
        original = absence(old_thought)
        event = EventStore(old).append(
            fixture.subject_id, "experience", "synthetic-test", {"goal": True}
        )
        goals = GoalStore(old)
        goal = goals.create_candidate(
            fixture.subject_id,
            GoalCandidate(
                title="A legacy investigation",
                description="Optional investigation.",
                origin="self",
                priority=0.3,
                commitment=0.2,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(event.event_id,),
            reason="synthetic legacy goal",
        )
        advance(fixture)
        old_thought.discover_agenda()
        assert absence(old_thought).status == "resolved"
        goals.revise(
            goal.goal_id,
            status="abandoned",
            priority=goal.priority,
            commitment=goal.commitment,
            progress=goal.progress,
            emotional_pressure=goal.emotional_pressure,
            causal_source_ids=(event.event_id,),
            reason="legacy investigation ended",
        )
    before = business_rows(old)
    with old.connection() as connection:
        assert Database.schema_ddl_fingerprint(connection) == _SCHEMA_DDL_FINGERPRINTS[109]
        assert (
            Database.schema_structure_fingerprint(connection) == _SCHEMA_STRUCTURE_FINGERPRINTS[109]
        )
    if inject_failure:
        with (
            patch.dict(
                MIGRATIONS, {110: MIGRATIONS[110] + "SELECT * FROM noyra_f04_simulated_failure;"}
            ),
            pytest.raises(sqlite3.OperationalError, match="noyra_f04_simulated_failure"),
        ):
            Database(old.path, strict_schema_contract=True)
        with old.connection() as connection:
            assert (
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0]
                == "109"
            )
            assert Database.schema_ddl_fingerprint(connection) == _SCHEMA_DDL_FINGERPRINTS[109]
        assert business_rows(old) == before
    upgraded = Database(old.path, strict_schema_contract=True)
    assert business_rows(upgraded) == before
    with upgraded.connection() as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(CURRENT_SCHEMA_VERSION)
        plan = [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM thought_agenda_revisions "
                "WHERE agenda_id=? ORDER BY rowid DESC LIMIT 2",
                (original.agenda_id,),
            )
        ]
    assert any("SEARCH" in step and "idx_thought_agenda_revision_agenda" in step for step in plan)
    assert not any("TEMP B-TREE" in step for step in plan)
    advance(fixture)
    restarted = IntrinsicThought(
        upgraded,
        fixture.subject_id,
        gateway,
        fixture.settings(),
        clock=lambda: fixture.clock_value,
    )
    restarted.discover_agenda()
    returned = absence(restarted)
    assert returned.agenda_id == original.agenda_id
    assert returned.status == "open" and returned.recurrence_count == 2
    assert provider.requests == []
    restarted.verify_integrity()
    upgraded.assert_schema_contract()


def test_missing_recurrence_index_is_not_silently_recreated(
    fixture: fixtures.IntrinsicThoughtTestCase,
) -> None:
    database = fixture.kernel.database
    with database.transaction() as connection:
        connection.execute("DROP INDEX idx_thought_agenda_revision_agenda")
    with pytest.raises(SchemaContractError):
        Database(database.path)
