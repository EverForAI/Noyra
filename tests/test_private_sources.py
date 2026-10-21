from __future__ import annotations

from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.private_sources import private_input_scope
from noyra.core.types import content_hash
from noyra.interaction import InteractionStore
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.integrity import InteractionIntegrity
from noyra.model.ledger import ModelLedger


def test_schema85_upgrade_preserves_messages_and_installs_source_contract(tmp_path: Path) -> None:
    db = Database(tmp_path / "migration.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    message = InteractionStore(db).receive("owner", "web", "web-user", "original")
    with db.transaction() as connection:
        connection.execute("DROP TABLE private_model_sources")
        connection.execute("ALTER TABLE cognitive_resource_groups DROP COLUMN reasoning_effort")
        connection.execute("UPDATE schema_meta SET value='85' WHERE key='schema_version'")
    migrated = Database(db.path)
    assert InteractionStore(migrated).get(message.interaction_id).content == "original"
    with migrated.connection() as connection:
        assert (
            int(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0]
            )
            == CURRENT_SCHEMA_VERSION
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM private_model_sources").fetchone()[0] == 0


def test_authorized_history_spans_windows_and_excludes_unverified_and_other_owner(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "history.sqlite3")
    for owner in ("owner", "other"):
        IdentityStore(db).ensure(owner, content_hash(owner))
    windows = ConversationStore(db)
    messages = InteractionStore(db)
    first = windows.create("owner", "first")
    second = windows.create("owner", "second")
    old = messages.receive(
        "owner", "web", "web-user", "remember a fact", conversation_id=first["conversation_id"]
    )
    messages.send(
        "owner", "web", "web-user", "a true reply", related_interaction_id=old.interaction_id
    )
    unknown = messages.receive("owner", "web", "web-user", "unconfirmed legacy text")
    with db.transaction() as connection:
        connection.execute(
            "UPDATE private_conversation_messages SET author_verified=0 WHERE interaction_id=?",
            (unknown.interaction_id,),
        )
    messages.receive("other", "web", "web-user", "another owner's text")
    current = messages.receive(
        "owner", "web", "web-user", "new window", conversation_id=second["conversation_id"]
    )
    history = windows.authorized_history("owner", current.interaction_id)
    assert [row["content"] for row in history] == ["remember a fact", "a true reply"]
    assert windows.authorized_history("other", current.interaction_id) == []
    assert windows.authorized_history("owner", unknown.interaction_id) == []


def test_model_sources_survive_disabled_payload_capture_and_rollback_cross_subject(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "source.sqlite3")
    for owner in ("owner", "other"):
        IdentityStore(db).ensure(owner, content_hash(owner))
    message = InteractionStore(db).receive("owner", "web", "web-user", "private payload")
    ledger = ModelLedger(db)
    with private_input_scope((message.interaction_id,)):
        call, created = ledger.prepare_call(
            "owner", "mock", "mock", "interaction", content_hash("input"), "source-test"
        )
    assert created
    with db.connection() as connection:
        assert (
            connection.execute(
                "SELECT request_json FROM model_calls WHERE call_id=?", (call.call_id,)
            ).fetchone()[0]
            is None
        )
        source = connection.execute(
            "SELECT * FROM private_model_sources WHERE call_id=?", (call.call_id,)
        ).fetchone()
        assert source["interaction_id"] == message.interaction_id
        assert "private payload" not in str(dict(source))
    assert InteractionIntegrity(db).verify("owner")["interactions"] == 1
    with private_input_scope((message.interaction_id,)), pytest.raises(PermissionError):
        ledger.prepare_call(
            "other", "mock", "mock", "interaction", content_hash("input"), "wrong-owner"
        )
    with db.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM model_calls WHERE subject_id='other'"
            ).fetchone()[0]
            == 0
        )
    with db.transaction() as connection:
        connection.execute(
            "UPDATE interactions SET content_hash=? WHERE interaction_id=?",
            (content_hash("tampered"), message.interaction_id),
        )
    with pytest.raises(IntegrityError):
        InteractionIntegrity(db).verify("owner")
