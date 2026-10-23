"""Stable contact entities, distinct from delivery addresses and conversations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import content_hash, new_id, utc_now

ENTITY_TYPES = frozenset({"human", "agent", "noyra", "unknown"})


@dataclass(frozen=True)
class Participant:
    entity_id: str
    entity_type: str
    display_name: str
    source_id: str
    channel: str
    authentication: str

    @property
    def relationship_type(self) -> str:
        return (
            "human"
            if self.entity_type == "human"
            else "agent"
            if self.entity_type in {"agent", "noyra"}
            else "entity"
        )


class ParticipantStore:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def source(
        c: Any,
        subject: str,
        channel: str,
        namespace: str,
        sender: str,
        *,
        label: str = "",
        entity_type: str = "unknown",
        authentication: str = "channel",
        entity_id: str | None = None,
    ) -> Participant:
        if entity_type not in ENTITY_TYPES or not all(
            isinstance(v, str) and 0 < len(v) <= 512 for v in (channel, namespace, sender)
        ):
            raise ValueError("invalid participant source")
        row = c.execute(
            "SELECT e.*,s.source_id,s.channel,s.authentication FROM participant_sources s "
            "JOIN participant_entities e ON e.entity_id=s.entity_id AND e.subject_id=s.subject_id "
            "WHERE s.subject_id=? AND s.channel=? AND s.namespace=? AND s.sender_id=?",
            (subject, channel, namespace, sender),
        ).fetchone()
        if row:
            if row["status"] != "active":
                raise PermissionError("participant disabled")
            if entity_id and row["entity_id"] != entity_id:
                raise ValueError("source already belongs to another participant")
            return Participant(
                *(
                    str(row[k])
                    for k in (
                        "entity_id",
                        "entity_type",
                        "display_name",
                        "source_id",
                        "channel",
                        "authentication",
                    )
                )
            )
        now = utc_now()
        if entity_id:
            entity = c.execute(
                "SELECT * FROM participant_entities WHERE subject_id=? AND entity_id=? AND "
                "status='active'",
                (subject, entity_id),
            ).fetchone()
            if entity is None:
                raise NotFoundError("participant not found")
            entity_type, label = entity["entity_type"], entity["display_name"]
        else:
            if (
                c.execute(
                    "SELECT COUNT(*) FROM participant_entities WHERE subject_id=?", (subject,)
                ).fetchone()[0]
                >= 10000
            ):
                raise ValueError("participant capacity reached")
            entity_id = new_id("entity")
            label = (label.strip() or sender)[:128]
            c.execute(
                "INSERT INTO participant_entities VALUES(?,?,?,?,?,?)",
                (entity_id, subject, entity_type, label, "active", now),
            )
        source_id = new_id("psource")
        c.execute(
            "INSERT INTO participant_sources VALUES(?,?,?,?,?,?,?,?)",
            (source_id, subject, entity_id, channel, namespace, sender, authentication, now),
        )
        return Participant(entity_id, entity_type, label, source_id, channel, authentication)

    @staticmethod
    def link(
        c: Any,
        subject: str,
        interaction: str,
        who: Participant,
        conversation: str,
        *,
        private: bool = False,
    ) -> None:
        row = c.execute(
            "SELECT subject_id FROM interactions WHERE interaction_id=?", (interaction,)
        ).fetchone()
        source = c.execute(
            "SELECT subject_id,entity_id FROM participant_sources WHERE source_id=?",
            (who.source_id,),
        ).fetchone()
        if (
            row is None
            or row[0] != subject
            or source is None
            or tuple(source) != (subject, who.entity_id)
        ):
            raise IntegrityError("participant message ownership mismatch")
        existing = c.execute(
            "SELECT * FROM participant_message_links WHERE interaction_id=?", (interaction,)
        ).fetchone()
        values = (
            subject,
            who.entity_id,
            who.source_id,
            conversation,
            "private" if private else "channel",
        )
        if existing:
            if (
                tuple(
                    existing[k]
                    for k in (
                        "subject_id",
                        "entity_id",
                        "source_id",
                        "conversation_key",
                        "visibility",
                    )
                )
                != values
            ):
                raise IntegrityError("participant message attribution conflict")
            return
        c.execute(
            "INSERT INTO participant_message_links VALUES(?,?,?,?,?,?,?)",
            (interaction, *values, utc_now()),
        )

    @staticmethod
    def for_message_connection(c: Any, subject: str, interaction: str) -> Participant | None:
        row = c.execute(
            "SELECT "
            "e.entity_id,e.entity_type,e.display_name,s.source_id,s.channel,s.authentication "
            "FROM participant_message_links m JOIN participant_entities e ON "
            "e.entity_id=m.entity_id AND e.subject_id=m.subject_id "
            "JOIN participant_sources s ON s.source_id=m.source_id AND "
            "s.subject_id=m.subject_id AND s.entity_id=m.entity_id "
            "WHERE m.subject_id=? AND m.interaction_id=?",
            (subject, interaction),
        ).fetchone()
        return Participant(*(str(v) for v in row)) if row else None

    def for_message(self, subject: str, interaction: str) -> Participant | None:
        with self.database.connection() as c:
            return self.for_message_connection(c, subject, interaction)

    def history(self, subject: str, interaction: str, limit: int = 16) -> list[dict[str, Any]]:
        with self.database.connection() as c:
            link = c.execute(
                "SELECT * FROM participant_message_links WHERE subject_id=? AND interaction_id=?",
                (subject, interaction),
            ).fetchone()
            if link is None or link["visibility"] != "private":
                return []
            rows = c.execute(
                "SELECT "
                "i.interaction_id,i.direction,i.content,i.created_at,m.conversation_key AS "
                "conversation_id "
                "FROM participant_message_links m JOIN interactions i ON "
                "i.interaction_id=m.interaction_id AND i.subject_id=m.subject_id "
                "WHERE m.subject_id=? AND m.entity_id=? AND m.conversation_key=? AND "
                "m.visibility='private' "
                "AND i.rowid < (SELECT rowid FROM interactions WHERE interaction_id=?) "
                "ORDER BY i.rowid DESC LIMIT ?",
                (subject, link["entity_id"], link["conversation_key"], interaction, min(limit, 16)),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    @staticmethod
    def legacy_key(channel: str, namespace: str, sender: str) -> str:
        return "source:" + content_hash([channel, namespace, sender])
