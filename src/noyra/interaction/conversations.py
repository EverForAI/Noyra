"""Logical windows over the original invitation and reply records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import content_hash, new_id, utc_now

if TYPE_CHECKING:
    from noyra.core.database import Database

WEB_PRINCIPAL = "operator:web-user"


class ConversationConflict(ValueError):
    pass


def _row(connection: Any, subject_id: str, conversation_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM private_conversations WHERE subject_id=? AND conversation_id=?",
        (subject_id, conversation_id),
    ).fetchone()
    if row is None:
        raise NotFoundError("conversation not found")
    state = dict(row)
    digest = state.pop("state_hash")
    if content_hash(state) != digest:
        raise IntegrityError("conversation state mismatch")
    return state


def _insert(
    connection: Any, subject_id: str, principal_id: str, title: str, conversation_id: str
) -> dict[str, Any]:
    now = utc_now()
    state = dict(
        conversation_id=conversation_id,
        subject_id=subject_id,
        principal_id=principal_id,
        title=title,
        pinned=0,
        status="active",
        revision=1,
        created_at=now,
        updated_at=now,
    )
    connection.execute(
        "INSERT INTO private_conversations VALUES(?,?,?,?,?,?,?,?,?,?)",
        (*state.values(), content_hash(state)),
    )
    return state


def default_window(connection: Any, subject_id: str, principal_id: str = WEB_PRINCIPAL) -> str:
    window_id = "conversation_" + content_hash([subject_id, principal_id, "legacy-default"])[:32]
    if not connection.execute(
        "SELECT 1 FROM private_conversations WHERE conversation_id=?", (window_id,)
    ).fetchone():
        _insert(connection, subject_id, principal_id, "历史对话", window_id)
    return window_id


def migrate_web_history(connection: Any) -> None:
    verified = any(
        row[1] == "author_verified"
        for row in connection.execute("PRAGMA table_info(private_conversation_messages)")
    )
    for subject in connection.execute(
        "SELECT DISTINCT subject_id FROM interactions "
        "WHERE channel='web' AND counterparty='web-user'"
    ).fetchall():
        window_id = default_window(connection, subject[0])
        connection.execute(
            "INSERT OR IGNORE INTO private_conversation_messages "
            + (
                "SELECT interaction_id, ?, subject_id, 0 FROM interactions "
                if verified
                else "SELECT interaction_id, ?, subject_id FROM interactions "
            )
            + "WHERE subject_id=? AND channel='web' AND counterparty='web-user'",
            (window_id, subject[0]),
        )


def link_message(
    connection: Any,
    subject_id: str,
    interaction_id: str,
    *,
    conversation_id: str | None,
    related_id: str | None,
    channel: str,
    counterparty: str,
) -> None:
    author_verified = channel == "web"
    if conversation_id is None and related_id:
        source = connection.execute(
            "SELECT conversation_id,author_verified FROM private_conversation_messages "
            "WHERE interaction_id=? AND subject_id=?",
            (related_id, subject_id),
        ).fetchone()
        conversation_id = source[0] if source else None
        author_verified = bool(source[1]) if source else False
    if conversation_id is None and channel == "web" and counterparty == "web-user":
        conversation_id = default_window(connection, subject_id)
    if conversation_id is None:
        return
    state = _row(connection, subject_id, conversation_id)
    if state["status"] != "active" and not (state["status"] == "archived" and related_id):
        raise ConversationConflict("conversation is unavailable")
    if channel == "web" and state["principal_id"] != WEB_PRINCIPAL:
        raise PermissionError("conversation principal mismatch")
    existing = connection.execute(
        "SELECT conversation_id FROM private_conversation_messages WHERE interaction_id=?",
        (interaction_id,),
    ).fetchone()
    if existing and existing[0] != conversation_id:
        raise ConversationConflict("message belongs to another conversation")
    connection.execute(
        "INSERT OR IGNORE INTO private_conversation_messages VALUES(?,?,?,?)",
        (interaction_id, conversation_id, subject_id, int(author_verified)),
    )


class ConversationStore:
    def __init__(self, database: Database):
        self.database = database

    def authorized_history(
        self, subject_id: str, interaction_id: str, *, limit: int = 16
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("invalid private history limit")
        with self.database.read_transaction() as connection:
            current = connection.execute(
                "SELECT c.principal_id, m.author_verified FROM private_conversation_messages m "
                "JOIN private_conversations c ON c.conversation_id=m.conversation_id "
                "WHERE m.interaction_id=? AND m.subject_id=? AND c.subject_id=? "
                "AND c.status IN ('active','archived')",
                (interaction_id, subject_id, subject_id),
            ).fetchone()
            if current is None or not current["author_verified"]:
                return []
            rows = connection.execute(
                "SELECT i.*, m.conversation_id FROM interactions i "
                "JOIN private_conversation_messages m ON m.interaction_id=i.interaction_id "
                "JOIN private_conversations c ON c.conversation_id=m.conversation_id "
                "WHERE i.subject_id=? AND m.subject_id=? AND c.subject_id=? "
                "AND c.principal_id=? AND c.status IN ('active','archived') "
                "AND m.author_verified=1 AND i.interaction_id!=? "
                "ORDER BY i.created_at DESC,i.interaction_id DESC LIMIT ?",
                (
                    subject_id,
                    subject_id,
                    subject_id,
                    current["principal_id"],
                    interaction_id,
                    limit,
                ),
            ).fetchall()
            from .store import InteractionStore

            return [
                {
                    **InteractionStore._from_row(row).__dict__,
                    "conversation_id": row["conversation_id"],
                }
                for row in reversed(rows)
            ]

    def create(self, subject_id: str, title: str) -> dict[str, Any]:
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
            raise ValueError("invalid conversation title")
        with self.database.transaction() as connection:
            return _insert(
                connection, subject_id, WEB_PRINCIPAL, title.strip(), new_id("conversation")
            )

    def list(
        self, subject_id: str, *, archived: bool = False, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("invalid conversation page")
        with self.database.read_transaction() as connection:
            rows = connection.execute(
                "SELECT conversation_id FROM private_conversations WHERE subject_id=? AND status=? "
                "ORDER BY pinned DESC, updated_at DESC, conversation_id DESC LIMIT ? OFFSET ?",
                (subject_id, "archived" if archived else "active", limit + 1, offset),
            ).fetchall()
            items = [_row(connection, subject_id, row[0]) for row in rows[:limit]]
        return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}

    def update(
        self,
        subject_id: str,
        conversation_id: str,
        *,
        expected_revision: int,
        title: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction() as connection:
            state = _row(connection, subject_id, conversation_id)
            if type(expected_revision) is not int or state["revision"] != expected_revision:
                raise ConversationConflict("conversation revision changed")
            if state["status"] in {"deleting", "deleted"}:
                raise ConversationConflict("conversation unavailable")
            if title is not None:
                if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
                    raise ValueError("invalid title")
                state["title"] = title.strip()
            for name, value in (("pinned", pinned), ("archived", archived)):
                if value is not None and type(value) is not bool:
                    raise ValueError(f"invalid {name}")
            if pinned is not None:
                state["pinned"] = int(pinned)
            if archived is not None:
                state["status"] = "archived" if archived else "active"
            state["revision"] += 1
            state["updated_at"] = utc_now()
            connection.execute(
                "UPDATE private_conversations SET title=?, pinned=?, status=?, revision=?, "
                "updated_at=?, state_hash=? WHERE conversation_id=? AND subject_id=?",
                (
                    state["title"],
                    state["pinned"],
                    state["status"],
                    state["revision"],
                    state["updated_at"],
                    content_hash(state),
                    conversation_id,
                    subject_id,
                ),
            )
        return state

    def messages(
        self,
        subject_id: str,
        conversation_id: str,
        *,
        before: str | None = None,
        query: str = "",
        status: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100 or len(query) > 200:
            raise ValueError("invalid message query")
        with self.database.read_transaction() as connection:
            state = _row(connection, subject_id, conversation_id)
            if state["status"] in {"deleting", "deleted"}:
                raise ConversationConflict("conversation unavailable")
            filters = ""
            parameters: list[Any] = [subject_id, conversation_id]
            if before:
                anchor = connection.execute(
                    "SELECT i.created_at FROM interactions i JOIN private_conversation_messages m "
                    "ON m.interaction_id=i.interaction_id WHERE i.interaction_id=? "
                    "AND m.conversation_id=? AND m.subject_id=?",
                    (before, conversation_id, subject_id),
                ).fetchone()
                if anchor is None:
                    raise ValueError("invalid message cursor")
                filters += " AND (i.created_at,i.interaction_id) < (?,?)"
                parameters.extend([anchor[0], before])
            if query:
                filters += " AND instr(lower(i.content),lower(?)) > 0"
                parameters.append(query)
            if status:
                if status not in {"offered", "accepted", "rejected", "deferred", "silent", "sent"}:
                    raise ValueError("invalid message status")
                filters += " AND i.status=?"
                parameters.append(status)
            rows = connection.execute(
                "SELECT i.*, m.author_verified FROM interactions i "
                "JOIN private_conversation_messages m "
                "ON m.interaction_id=i.interaction_id WHERE m.subject_id=? AND m.conversation_id=? "
                + filters
                + " ORDER BY i.created_at DESC,i.interaction_id DESC LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
            from .store import InteractionStore

            items = [
                {
                    **InteractionStore._from_row(row).__dict__,
                    "author_verified": bool(row["author_verified"]),
                }
                for row in reversed(rows[:limit])
            ]
            for item in items:
                call = connection.execute(
                    "SELECT status FROM model_calls WHERE subject_id=? AND "
                    "substr(purpose,1,?)=? ORDER BY created_at DESC,call_id DESC LIMIT 1",
                    (
                        subject_id,
                        len(f"interaction_cognition:{item['interaction_id']}:"),
                        f"interaction_cognition:{item['interaction_id']}:",
                    ),
                ).fetchone()
                item["model_call_status"] = call[0] if call else None
        return {
            "conversation": state,
            "items": items,
            "next_before": rows[limit - 1]["interaction_id"] if len(rows) > limit else None,
        }
