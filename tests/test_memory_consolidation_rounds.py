from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import Database, EventStore, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.source_provenance import MAX_SOURCE_EVENT_IDS
from noyra.core.types import content_hash
from noyra.mind import MemoryConsolidator, MemoryStore
from noyra.mind.types import MemoryRecord


@dataclass
class Case:
    database: Database
    now: datetime
    subject: str = "M03-synthetic"

    def clock(self) -> str:
        return self.now.isoformat(timespec="milliseconds")

    def worker(self, *, interval: float = 60) -> MemoryConsolidator:
        return MemoryConsolidator(
            self.database, self.subject, clock=self.clock, interval_seconds=interval
        )

    def memory(self, text: str, *, salience: float = 0.5) -> MemoryRecord:
        event = EventStore(self.database).append(
            self.subject,
            "experience",
            "synthetic",
            {"text_hash": content_hash(text)},
            occurred_at=self.clock(),
        )
        return MemoryStore(self.database, clock=self.clock).create(
            self.subject,
            "semantic",
            text,
            salience=salience,
            confidence=0.8,
            source_event_ids=(event.event_id,),
        )


@pytest.fixture
def case(tmp_path: Path) -> Case:
    db = Database(tmp_path / "noyra.sqlite3")
    value = Case(db, datetime(2026, 11, 15, 12, tzinfo=UTC))
    IdentityStore(db).ensure(value.subject, content_hash("M03"))
    return value


@pytest.mark.parametrize("changed", [False, True])
def test_same_day_second_due_round_really_reviews_current_memory(case: Case, changed: bool) -> None:
    memory = case.memory("A useful synthetic memory.")
    store = MemoryStore(case.database, clock=case.clock)
    if changed:
        for index in range(3):
            store.recall(
                case.subject,
                "useful synthetic memory",
                context_type="synthetic",
                context_id=str(index),
            )
    worker = case.worker()
    expected = "memory_consolidation_committed" if changed else "memory_consolidation_no_change"
    assert worker.run_due() == expected
    first = worker.latest()
    assert first is not None
    first_salience = store.get(memory.memory_id).salience
    if not changed:
        case.memory("A newly available synthetic memory.")
    case.now += timedelta(seconds=60)
    assert case.worker().run_due() == expected
    history = store.consolidation_history(case.subject)
    assert len(history) == 2
    assert history[0].consolidation_id != first.consolidation_id
    assert history[0].reviewed_count == (1 if changed else 2)
    if changed:
        assert store.get(memory.memory_id).salience > first_salience
    store.verify_lifecycle_integrity(case.subject)


def snapshot(case: Case) -> dict[str, list[tuple[Any, ...]]]:
    with case.database.read_transaction() as connection:
        return {
            table: [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in (
                "memories",
                "memory_revisions",
                "memory_consolidation_runs",
                "memory_consolidation_members",
                "events",
                "event_chain_roots",
                "training_records",
            )
        }


def used_memory(case: Case) -> MemoryRecord:
    value = case.memory("A repeatedly useful synthetic memory.")
    store = MemoryStore(case.database, clock=case.clock)
    for index in range(3):
        store.recall(
            case.subject, "repeatedly useful", context_type="synthetic", context_id=str(index)
        )
    return value


def test_consolidation_preserves_full_source_list(case: Case) -> None:
    events = EventStore(case.database)
    sources = tuple(
        events.append(case.subject, "experience", "synthetic", {"sequence": index}).event_id
        for index in range(MAX_SOURCE_EVENT_IDS)
    )
    store = MemoryStore(case.database, clock=case.clock)
    value = store.create(
        case.subject,
        "semantic",
        "A repeatedly useful memory.",
        salience=0.5,
        confidence=0.8,
        source_event_ids=sources,
    )
    for index in range(3):
        store.recall(
            case.subject, "repeatedly useful", context_type="synthetic", context_id=str(index)
        )
    assert case.worker().run_due() == "memory_consolidation_committed"
    assert store.source_event_ids(value.memory_id) == sources
    assert store.consolidation_history(case.subject)[0].strengthened_memory_ids == (
        value.memory_id,
    )
    store.verify_lifecycle_integrity(case.subject)


def test_interval_boundary_restart_and_clock_rollback_do_not_repeat_writes(case: Case) -> None:
    case.memory("An interval anchor.")
    assert case.worker().run_due() == "memory_consolidation_no_change"
    before = snapshot(case)
    case.now += timedelta(seconds=59, milliseconds=999)
    for _ in range(3):
        assert case.worker().run_due() is None
    assert snapshot(case) == before
    case.now -= timedelta(days=1)
    assert case.worker().run_due() is None
    assert snapshot(case) == before
    case.now += timedelta(days=1, milliseconds=1)
    assert case.worker().run_due() == "memory_consolidation_no_change"
    assert len(MemoryStore(case.database).consolidation_history(case.subject)) == 2


def test_crossing_midnight_does_not_shorten_a_long_interval(case: Case) -> None:
    case.now = datetime(2026, 11, 15, 23, 59, tzinfo=UTC)
    assert case.worker(interval=86400).run_due() == "memory_consolidation_no_change"
    before = snapshot(case)
    case.now += timedelta(minutes=2)
    assert case.worker(interval=86400).run_due() is None
    assert snapshot(case) == before
    case.now += timedelta(days=1) - timedelta(minutes=2)
    assert case.worker(interval=86400).run_due() == "memory_consolidation_no_change"
    assert len(MemoryStore(case.database).consolidation_history(case.subject)) == 2


def test_timezone_representation_does_not_change_the_successor_identity(case: Case) -> None:
    assert case.worker().run_due() == "memory_consolidation_no_change"
    before = snapshot(case)
    case.now = case.now.astimezone(timezone(timedelta(hours=-12)))
    assert case.worker().run_due() is None
    assert snapshot(case) == before
    case.now += timedelta(seconds=60)
    assert case.worker().run_due() == "memory_consolidation_no_change"
    last = case.worker().latest()
    assert last is not None
    case.now = case.now.astimezone(UTC)
    assert case.worker().run_due() is None
    assert case.worker().latest() == last
    case.now += timedelta(seconds=60)
    assert case.worker().run_due() == "memory_consolidation_no_change"
    assert len(MemoryStore(case.database).consolidation_history(case.subject)) == 3


@pytest.mark.parametrize("has_previous", [False, True])
def test_parallel_workers_apply_one_successor_and_one_strengthening(
    case: Case, has_previous: bool
) -> None:
    if has_previous:
        assert case.worker().run_due() == "memory_consolidation_no_change"
        case.now += timedelta(seconds=60)
    memory = used_memory(case)
    barrier = Barrier(2)
    workers = [case.worker(), case.worker()]

    def run(worker: MemoryConsolidator) -> str | None:
        original = worker._candidates

        def candidates() -> Any:
            selected = original()
            barrier.wait(timeout=10)
            return selected

        with patch.object(worker, "_candidates", side_effect=candidates):
            return worker.run_due()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, worker) for worker in workers]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(str(result) for result in results) == [
        "memory_consolidation_committed",
        "memory_consolidation_replayed",
    ]
    store = MemoryStore(case.database)
    assert len(store.consolidation_history(case.subject)) == (2 if has_previous else 1)
    assert len(store.revisions(memory.memory_id)) == 2
    assert store.get(memory.memory_id).salience == pytest.approx(0.56)
    store.verify_lifecycle_integrity(case.subject)
    EventStore(case.database).verify_chain(case.subject)


def test_failure_after_a_memory_revision_rolls_back_the_entire_round(case: Case) -> None:
    used_memory(case)
    worker = case.worker()
    before = snapshot(case)
    original = worker.memories._revise_connection

    def fail(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise OSError("synthetic revision crash")

    with (
        patch.object(worker.memories, "_revise_connection", side_effect=fail),
        pytest.raises(OSError, match="synthetic revision crash"),
    ):
        worker.run_due()
    assert snapshot(case) == before
    assert case.worker().run_due() == "memory_consolidation_committed"
    assert len(MemoryStore(case.database).consolidation_history(case.subject)) == 1
    MemoryStore(case.database).verify_lifecycle_integrity(case.subject)
    EventStore(case.database).verify_chain(case.subject)


def test_crash_after_commit_is_not_another_round_until_due(case: Case) -> None:
    memory = used_memory(case)
    worker = case.worker()
    original = worker._commit

    def fail(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("synthetic post-commit crash")

    with (
        patch.object(worker, "_commit", side_effect=fail),
        pytest.raises(RuntimeError, match="synthetic post-commit crash"),
    ):
        worker.run_due()
    before = snapshot(case)
    for _ in range(3):
        assert case.worker().run_due() is None
    assert snapshot(case) == before
    assert len(MemoryStore(case.database).revisions(memory.memory_id)) == 2
    case.now += timedelta(seconds=60)
    assert case.worker().run_due() == "memory_consolidation_committed"
    assert len(MemoryStore(case.database).revisions(memory.memory_id)) == 3


def test_legacy_daily_receipt_remains_unchanged_and_can_have_a_same_day_successor(
    case: Case,
) -> None:
    legacy_key = "memory-consolidation:2026-11-15"
    with case.database.transaction() as connection:
        connection.execute(
            "INSERT INTO memory_consolidation_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "mcon_legacy",
                case.subject,
                legacy_key,
                "no_change",
                0,
                "[]",
                "[]",
                "[]",
                "Synthetic legacy no-op",
                MemoryConsolidator._state_hash(
                    "no_change", 0, (), (), (), "Synthetic legacy no-op", case.clock()
                ),
                case.clock(),
            ),
        )
    with case.database.read_transaction() as connection:
        original = tuple(connection.execute("SELECT * FROM memory_consolidation_runs").fetchone())
    case.memory("A memory recorded after the legacy run.")
    assert case.worker().run_due() is None
    case.now += timedelta(seconds=60)
    assert case.worker().run_due() == "memory_consolidation_no_change"
    with case.database.read_transaction() as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM memory_consolidation_runs WHERE consolidation_id='mcon_legacy'"
                ).fetchone()
            )
            == original
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM memory_consolidation_runs").fetchone()[0] == 2
        )
    latest = case.worker().latest()
    assert latest is not None and latest.reviewed_count == 1
    MemoryStore(case.database).verify_lifecycle_integrity(case.subject)


def test_subjects_have_independent_initial_rounds(case: Case) -> None:
    assert case.worker().run_due() == "memory_consolidation_no_change"
    other = "M03-other-synthetic"
    IdentityStore(case.database).ensure(other, content_hash(other))
    worker = MemoryConsolidator(case.database, other, clock=case.clock, interval_seconds=60)
    assert worker.run_due() == "memory_consolidation_no_change"
    assert case.worker().run_due() is None
    assert worker.run_due() is None
    assert len(MemoryStore(case.database).consolidation_history(case.subject)) == 1
    assert len(MemoryStore(case.database).consolidation_history(other)) == 1


def test_latest_uses_commit_order_when_zero_interval_timestamps_tie(case: Case) -> None:
    worker = case.worker(interval=0)
    seen = []
    for _ in range(4):
        assert worker.run_due() == "memory_consolidation_no_change"
        latest = worker.latest()
        assert latest is not None
        seen.append(latest.consolidation_id)
    assert len(set(seen)) == 4
    with case.database.read_transaction() as connection:
        assert seen == [
            row[0]
            for row in connection.execute(
                "SELECT consolidation_id FROM memory_consolidation_runs ORDER BY rowid"
            )
        ]
    MemoryStore(case.database).verify_lifecycle_integrity(case.subject)


def test_due_state_is_rechecked_after_candidate_selection(case: Case) -> None:
    worker = case.worker()
    assert worker.run_due() == "memory_consolidation_no_change"
    case.now += timedelta(seconds=60)
    before = snapshot(case)
    original = worker._candidates

    def candidates() -> Any:
        values = original()
        case.now -= timedelta(seconds=120)
        return values

    with patch.object(worker, "_candidates", side_effect=candidates):
        assert worker.run_due() is None
    assert snapshot(case) == before


@pytest.mark.parametrize("older", [False, True])
def test_corrupt_history_is_not_ignored_to_start_a_new_round(case: Case, older: bool) -> None:
    assert case.worker().run_due() == "memory_consolidation_no_change"
    first = case.worker().latest()
    assert first is not None
    if older:
        case.now += timedelta(seconds=60)
        assert case.worker().run_due() == "memory_consolidation_no_change"
    case.now += timedelta(seconds=60)
    with case.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_memory_consolidation_update")
        connection.execute(
            "UPDATE memory_consolidation_runs SET state_hash=? WHERE consolidation_id=?",
            ("0" * 64, first.consolidation_id),
        )
    with pytest.raises(IntegrityError, match="consolidation hash mismatch"):
        case.worker().run_due()
    with case.database.read_transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_consolidation_runs").fetchone()[
            0
        ] == (2 if older else 1)
