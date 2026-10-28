from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core import database as database_module
from noyra.interaction import InteractionStore
from noyra.interaction.participants import ParticipantStore
from noyra.mind import RelationshipStore
from test_audit_20261024_followup import social_fixture
from test_audit_20261024_remediation import kernel as base_kernel


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    yield from cast(Any, base_kernel).__wrapped__(tmp_path)


def test_recipient_queries_use_indexes_independent_of_unrelated_history(
    kernel: SubjectKernel,
) -> None:
    social, _, _, _, _, _, first, who, _ = social_fixture(kernel)
    messages = InteractionStore(kernel.database)
    legacy = messages.receive(kernel.subject_id, "local", "legacy", "Legacy invitation")
    with kernel.database.connection() as c:
        legacy_event = c.execute(
            "SELECT event_id FROM events WHERE json_extract(payload_json,'$.interaction_id')=?",
            (legacy.interaction_id,),
        ).fetchone()[0]
    relationships = [
        social.relationships.find(kernel.subject_id, who.relationship_type, who.entity_id),
        RelationshipStore(kernel.database).ensure(
            kernel.subject_id, "human", "legacy", "Legacy", source_event_ids=(legacy_event,)
        ),
    ]

    def measure() -> tuple[list[int], list[list[str]]]:
        counts: list[int] = []
        plans: list[list[str]] = []
        for relationship, expected in zip(
            relationships, [first.interaction_id, legacy.interaction_id], strict=True
        ):
            instructions = 0
            statements: list[str] = []

            def progress() -> int:
                nonlocal instructions
                instructions += 1
                return 0

            with kernel.database.connection() as c:
                c.set_progress_handler(progress, 1)
                c.set_trace_callback(statements.append)
                rows = social._recipient_interactions(c, relationship)
                c.set_progress_handler(None, 0)
                c.set_trace_callback(None)
                assert rows[0]["interaction_id"] == expected
                sql = next(s for s in statements if s.startswith("SELECT i.interaction_id"))
                plans.append([str(r[3]) for r in c.execute("EXPLAIN QUERY PLAN " + sql)])
            counts.append(instructions)
        return counts, plans

    before, _ = measure()
    # Real message writer in one transaction: valid hashes and event evidence,
    # without paying for 10,000 unrelated transaction open/close operations.
    with kernel.database.transaction() as c:
        for i in range(10_000):
            messages._create_connection(
                c,
                kernel.subject_id,
                "incoming",
                "human_message",
                "local",
                "unrelated",
                "Unrelated message",
                None,
                f"noise:{i}",
                "offered",
                None,
            )
    after, plans = measure()
    assert all(a <= b + 100 for a, b in zip(after, before, strict=True))
    assert any("idx_participant_messages_timeline" in p for p in plans[0])
    assert any("idx_interactions_correspondent_timeline" in p for p in plans[1])
    assert not any("TEMP B-TREE" in p for plan in plans for p in plan)
    assert social._context() is not None


@pytest.mark.asyncio
async def test_context_budget_interrupts_expensive_query_and_releases_reader(
    kernel: SubjectKernel,
) -> None:
    social, provider, *_ = social_fixture(kernel)

    def expensive(c: sqlite3.Connection) -> None:
        c.execute(
            "WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL "
            "SELECT n+1 FROM numbers WHERE n<10000000) SELECT sum(n) FROM numbers"
        ).fetchone()
        raise AssertionError("SQL budget did not interrupt the query")

    with patch.object(social, "_context_connection", side_effect=expensive):
        assert await social.run_due() is None
    assert provider.requests == []
    assert social._context() is not None
    with kernel.database.transaction() as c:
        c.execute("INSERT INTO schema_meta VALUES('budget_released','yes')")


def test_v104_migration_preserves_original_message_time_and_attribution(tmp_path: Path) -> None:
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 104):
        db = Database(tmp_path / "old.db", strict_schema_contract=True)
    IdentityStore(db).ensure("subject", "a" * 64)
    message = InteractionStore(db).receive("subject", "test", "room", "Original invitation")
    with db.transaction() as c:
        who = ParticipantStore.source(c, "subject", "test", "tenant", "sender")
        c.execute(
            "INSERT INTO participant_message_links VALUES(?,?,?,?,?,?,?)",
            (
                message.interaction_id,
                "subject",
                who.entity_id,
                who.source_id,
                "room",
                "channel",
                "2099-01-01T00:00:00Z",
            ),
        )
    db = Database(db.path, strict_schema_contract=True)
    with db.connection() as c:
        row = c.execute("SELECT * FROM participant_message_links").fetchone()
        assert row["interaction_created_at"] == message.created_at
        assert row["created_at"] == "2099-01-01T00:00:00Z"
        assert row["entity_id"] == who.entity_id
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    with db.transaction() as c, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        c.execute("UPDATE participant_message_links SET interaction_created_at='tampered'")
    other = InteractionStore(db).receive("subject", "test", "room", "Another invitation")
    with db.transaction() as c, pytest.raises(sqlite3.IntegrityError, match="time mismatch"):
        c.execute(
            "INSERT INTO participant_message_links VALUES(?,?,?,?,?,?,?,?)",
            (
                other.interaction_id,
                "subject",
                who.entity_id,
                who.source_id,
                "room",
                "channel",
                "2099-01-01T00:00:00Z",
                "2099-01-01T00:00:00Z",
            ),
        )
    Database(db.path, strict_schema_contract=True)


def test_v104_migration_failure_is_atomic_and_can_retry(tmp_path: Path) -> None:
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 104):
        db = Database(tmp_path / "old.db", strict_schema_contract=True)
    original = Database._execute_sql_script

    def fail(c: sqlite3.Connection, script: str) -> None:
        if "idx_participant_messages_timeline" in script:
            raise RuntimeError("Injected migration failure")
        original(c, script)

    with (
        patch.object(Database, "_execute_sql_script", side_effect=fail),
        pytest.raises(RuntimeError, match="Injected migration failure"),
    ):
        Database(db.path, strict_schema_contract=True)
    with db.connection() as c:
        assert (
            c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            == "104"
        )
        assert "interaction_created_at" not in [
            r[1] for r in c.execute("PRAGMA table_info(participant_message_links)")
        ]
        assert c.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?",
            ("prevent_participant_message_links_update",),
        ).fetchone()
    Database(db.path, strict_schema_contract=True)


@pytest.mark.parametrize("blocked", [False, True])
def test_full_history_pages_keep_advancing_under_instruction_budget(
    kernel: SubjectKernel,
    blocked: bool,
) -> None:
    social, _, _, _, _, _, first, who, _ = social_fixture(kernel)
    with kernel.database.connection() as c:
        event = c.execute(
            "SELECT event_id FROM events WHERE json_extract(payload_json,'$.interaction_id')=?",
            (first.interaction_id,),
        ).fetchone()[0]
    for n in range(64):
        relation = social.relationships.ensure(
            kernel.subject_id,
            "human",
            f"unavailable:{n}",
            f"Unavailable {n}",
            source_event_ids=(event,),
        )
        social.relationships.revise(
            relation.relationship_id,
            trust=0,
            affinity=0,
            conflict=0,
            familiarity=1,
            boundaries={"no_contact": True} if blocked else {},
            reason="Local history fixture",
            source_event_ids=(event,),
        )
    with kernel.database.transaction() as c:
        for n in range(64):
            for i in range(48):
                social.interactions._create_connection(
                    c,
                    kernel.subject_id,
                    "incoming" if blocked else "outgoing",
                    "human_message" if blocked else "subject_message",
                    "unsupported",
                    f"unavailable:{n}",
                    "An old invitation",
                    None,
                    f"unavailable:{n}:{i}",
                    "offered" if blocked else "sent",
                    None,
                )
    # Exercise the VM cap deterministically; the independent wall-clock cap
    # still applies in production, regardless of machine load in parallel CI.
    with (
        patch("noyra.cognition.social.time.monotonic", return_value=0),
        patch.object(
            social, "_recipient_interactions", wraps=social._recipient_interactions
        ) as read,
    ):
        assert social._context() is None
        assert read.call_count == (0 if blocked else 64)
        with kernel.database.connection() as c:
            assert c.execute(
                "SELECT value FROM schema_meta WHERE key=?",
                (f"relationship_social_cursor:{kernel.subject_id}",),
            ).fetchone()
        context = social._context()
    assert context
    assert {r.entity_key for r in context.relationships.values()} == {who.entity_id}
    assert next(iter(context.routes.values())).interaction_id == first.interaction_id
