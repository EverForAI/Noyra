"""Explicit private input provenance independent of provider payload capture."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .types import content_hash

_SOURCES: ContextVar[tuple[str, ...]] = ContextVar("noyra_private_interaction_sources", default=())

_INVITATION: ContextVar[tuple[str, str] | None] = ContextVar(
    "noyra_active_invitation", default=None
)
# The application key and message attribution are immutable. The invitation
# key was derived in the same transaction in schema 104, covering old rows too.
WITHDRAWN_PARTICIPANT_INVITATION = """EXISTS (
 SELECT 1 FROM participant_message_links p JOIN participant_applications a
 ON a.subject_id=p.subject_id AND a.entity_id=p.entity_id
 WHERE p.interaction_id=i.interaction_id AND p.subject_id=i.subject_id
 AND a.status='withdrawn' AND substr(i.idempotency_key,-9)=':interest'
 AND a.idempotency_key=substr(i.idempotency_key,1,length(i.idempotency_key)-9)
)"""


class InvitationWithdrawnError(PermissionError):
    """An active invitation was withdrawn; accounting must still settle."""


def participant_invitation_withdrawn(connection: Any, subject_id: str, interaction_id: str) -> bool:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='participant_applications'"
    ).fetchone():
        return False
    return (
        connection.execute(
            "SELECT 1 FROM interactions i WHERE i.subject_id=? AND i.interaction_id=? AND "
            + WITHDRAWN_PARTICIPANT_INVITATION,
            (subject_id, interaction_id),
        ).fetchone()
        is not None
    )


@contextmanager
def invitation_input_scope(subject_id: str, interaction_id: str) -> Iterator[None]:
    token = _INVITATION.set((subject_id, interaction_id))
    try:
        yield
    finally:
        _INVITATION.reset(token)


def assert_event_sources_available(connection: Any, subject_id: str, event_id: str) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND "
            "name='private_conversation_messages'"
        ).fetchone()
        is None
    ):
        return
    withdrawn = connection.execute(
        "SELECT 1 FROM events e JOIN private_conversation_messages m "
        "ON json_extract(e.payload_json,'$.interaction_id')=m.interaction_id "
        "JOIN private_conversations c ON c.conversation_id=m.conversation_id "
        "WHERE e.subject_id=? AND e.event_id=? AND c.status IN ('deleting','deleted') LIMIT 1",
        (subject_id, event_id),
    ).fetchone()
    if withdrawn is not None:
        raise PermissionError("private event source has been withdrawn")


def assert_private_inputs_available(connection: Any) -> None:
    active = _INVITATION.get()
    if active is not None and participant_invitation_withdrawn(connection, *active):
        raise InvitationWithdrawnError("participant invitation has been withdrawn")
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
