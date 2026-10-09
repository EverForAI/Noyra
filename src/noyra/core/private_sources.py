"""Explicit private input provenance independent of provider payload capture."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .types import content_hash

_SOURCES: ContextVar[tuple[str, ...]] = ContextVar("noyra_private_interaction_sources", default=())


def assert_private_inputs_available(connection: Any) -> None:
    for interaction_id in _SOURCES.get():
        state = connection.execute(
            "SELECT c.status FROM private_conversation_messages m JOIN private_conversations c "
            "ON c.conversation_id=m.conversation_id WHERE m.interaction_id=?",
            (interaction_id,),
        ).fetchone()
        if state is not None and state[0] in {"deleting", "deleted"}:
            raise PermissionError("private input has been withdrawn")


MIGRATION_V86 = """
CREATE TABLE IF NOT EXISTS private_model_sources (
 call_id TEXT NOT NULL REFERENCES model_calls(call_id),
 interaction_id TEXT NOT NULL REFERENCES interactions(interaction_id),
 conversation_id TEXT NOT NULL REFERENCES private_conversations(conversation_id),
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 source_hash TEXT NOT NULL,
 PRIMARY KEY(call_id,interaction_id)
);
CREATE INDEX IF NOT EXISTS idx_private_model_sources_window
 ON private_model_sources(subject_id,conversation_id,call_id);
CREATE TRIGGER IF NOT EXISTS prevent_private_model_source_update
 BEFORE UPDATE ON private_model_sources BEGIN
 SELECT RAISE(ABORT,'private model sources are immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_private_model_source_delete
 BEFORE DELETE ON private_model_sources BEGIN
 SELECT RAISE(ABORT,'private model sources require explicit erasure'); END;
"""


@contextmanager
def private_input_scope(interaction_ids: tuple[str, ...]) -> Iterator[None]:
    if len(interaction_ids) > 100 or len(set(interaction_ids)) != len(interaction_ids):
        raise ValueError("private sources must be distinct and bounded")
    token = _SOURCES.set(interaction_ids)
    try:
        yield
    finally:
        _SOURCES.reset(token)


def record_private_sources(connection: Any, subject_id: str, call_id: str) -> None:
    for interaction_id in _SOURCES.get():
        source = connection.execute(
            "SELECT i.content_hash, m.conversation_id, c.status "
            "FROM interactions i JOIN private_conversation_messages m "
            "ON m.interaction_id=i.interaction_id JOIN private_conversations c "
            "ON c.conversation_id=m.conversation_id "
            "WHERE i.interaction_id=? AND i.subject_id=? AND m.subject_id=? "
            "AND c.subject_id=?",
            (interaction_id, subject_id, subject_id, subject_id),
        ).fetchone()
        if source is None:
            # External invitations are not windows yet and retain their original provenance.
            if not connection.execute(
                "SELECT 1 FROM interactions WHERE interaction_id=? AND subject_id=?",
                (interaction_id, subject_id),
            ).fetchone():
                raise PermissionError("private source subject mismatch")
            continue
        if source["status"] not in {"active", "archived"}:
            raise PermissionError("private input is unavailable")
        state = {
            "call_id": call_id,
            "interaction_id": interaction_id,
            "conversation_id": source["conversation_id"],
            "subject_id": subject_id,
            "content_hash": source["content_hash"],
        }
        connection.execute(
            "INSERT OR IGNORE INTO private_model_sources VALUES(?,?,?,?,?)",
            (call_id, interaction_id, source["conversation_id"], subject_id, content_hash(state)),
        )
