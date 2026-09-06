from __future__ import annotations

import builtins
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.events import EventStore
from noyra.core.types import content_hash, new_id, utc_now

from .errors import InteractionStateConflictError
from .types import InteractionDecision, InteractionKind, InteractionRecord


class InteractionStore:
    """Stores messages as invitations and never treats incoming text as an instruction."""

    def __init__(self, database: Database):
        self.database = database
        self.events = EventStore(database)

    def receive(
        self,
        subject_id: str,
        channel: str,
        counterparty: str,
        content: str,
        *,
        kind: InteractionKind = "human_message",
        related_interaction_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> InteractionRecord:
        self._validate_message(channel, counterparty, content)
        if kind == "subject_message":
            raise ValueError("incoming interactions cannot be subject messages")
        resolved_key = idempotency_key or content_hash(
            {
                "direction": "incoming",
                "channel": channel,
                "counterparty": counterparty,
                "content": content,
                "related": related_interaction_id,
            }
        )
        with self.database.transaction() as connection:
            return self._create_connection(
                connection,
                subject_id,
                "incoming",
                kind,
                channel,
                counterparty,
                content,
                related_interaction_id,
                resolved_key,
                "offered",
                None,
            )

    def send(
        self,
        subject_id: str,
        channel: str,
        counterparty: str,
        content: str,
        *,
        kind: InteractionKind = "subject_message",
        related_interaction_id: str | None = None,
        idempotency_key: str | None = None,
        actor: str = "subject",
    ) -> InteractionRecord:
        if actor != "subject":
            raise PermissionError("only the subject can initiate an interaction")
        self._validate_message(channel, counterparty, content)
        if kind == "human_message":
            raise ValueError("outgoing subject messages cannot be human messages")
        resolved_key = idempotency_key or content_hash(
            {
                "direction": "outgoing",
                "channel": channel,
                "counterparty": counterparty,
                "content": content,
                "related": related_interaction_id,
            }
        )
        with self.database.transaction() as connection:
            return self._create_connection(
                connection,
                subject_id,
                "outgoing",
                kind,
                channel,
                counterparty,
                content,
                related_interaction_id,
                resolved_key,
                "sent",
                None,
            )

    def decide(
        self,
        interaction_id: str,
        decision: InteractionDecision,
        *,
        actor: str = "subject",
    ) -> InteractionRecord:
        if actor != "subject":
            raise PermissionError("only the subject can decide whether to engage")
        with self.database.transaction() as connection:
            row = self._get_row(connection, interaction_id)
            if row["direction"] != "incoming":
                raise InteractionStateConflictError("only incoming invitations need a decision")
            if row["status"] not in {"offered", "deferred"}:
                raise InteractionStateConflictError("interaction is already finalized")
            now = utc_now()
            state_hash = self._state_hash(row, decision.disposition, decision.rationale)
            connection.execute(
                """UPDATE interactions SET status = ?, rationale = ?, state_hash = ?,
                    decided_at = ? WHERE interaction_id = ?""",
                (decision.disposition, decision.rationale, state_hash, now, interaction_id),
            )
            decision_hash = content_hash(
                {
                    "from_status": row["status"],
                    "to_status": decision.disposition,
                    "rationale": decision.rationale,
                    "actor": actor,
                }
            )
            connection.execute(
                """INSERT INTO interaction_decisions(
                    decision_id, interaction_id, from_status, to_status, rationale,
                    actor, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("idec"),
                    interaction_id,
                    row["status"],
                    decision.disposition,
                    decision.rationale,
                    actor,
                    decision_hash,
                    now,
                ),
            )
            self.events._append_connection(
                connection,
                row["subject_id"],
                "interaction_decision",
                actor,
                {"interaction_id": interaction_id, "disposition": decision.disposition},
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=now,
                event_id=None,
            )
            return self._load_connection(connection, interaction_id)

    def get(self, interaction_id: str) -> InteractionRecord:
        with self.database.connection() as connection:
            return self._load_connection(connection, interaction_id)

    def list(self, subject_id: str, *, limit: int = 100) -> list[InteractionRecord]:
        bounded = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM interactions WHERE subject_id = ? "
                "ORDER BY created_at DESC, interaction_id DESC LIMIT ?",
                (subject_id, bounded),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_channel(
        self,
        subject_id: str,
        channel: str,
        *,
        limit: int = 100,
    ) -> builtins.list[InteractionRecord]:
        if not channel.strip() or len(channel) > 128:
            raise ValueError("interaction channel is invalid")
        bounded = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM interactions WHERE subject_id = ? AND channel = ? "
                "ORDER BY created_at DESC, interaction_id DESC LIMIT ?",
                (subject_id, channel, bounded),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def _create_connection(
        self,
        connection: Any,
        subject_id: str,
        direction: str,
        kind: str,
        channel: str,
        counterparty: str,
        content: str,
        related_interaction_id: str | None,
        idempotency_key: str,
        status: str,
        rationale: str | None,
    ) -> InteractionRecord:
        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise ValueError("interaction idempotency key is invalid")
        existing = connection.execute(
            "SELECT * FROM interactions WHERE subject_id = ? AND idempotency_key = ?",
            (subject_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            expected = (direction, kind, channel, counterparty, content, related_interaction_id)
            actual = (
                existing["direction"],
                existing["kind"],
                existing["channel"],
                existing["counterparty"],
                existing["content"],
                existing["related_interaction_id"],
            )
            if expected != actual:
                raise InteractionStateConflictError("interaction key identifies different content")
            return self._from_row(existing)
        now = utc_now()
        interaction_id = new_id("int")
        state_hash = self._state_hash_values(
            content, direction, kind, channel, counterparty, status, rationale
        )
        connection.execute(
            """INSERT INTO interactions(
                interaction_id, subject_id, direction, kind, channel, counterparty,
                content, content_hash, related_interaction_id, idempotency_key, status,
                rationale, state_hash, created_at, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (
                interaction_id,
                subject_id,
                direction,
                kind,
                channel,
                counterparty,
                content,
                content_hash(content),
                related_interaction_id,
                idempotency_key,
                status,
                rationale,
                state_hash,
                now,
            ),
        )
        self.events._append_connection(
            connection,
            subject_id,
            "interaction_received" if direction == "incoming" else "interaction_sent",
            "human" if direction == "incoming" else "subject",
            {"interaction_id": interaction_id, "channel": channel, "kind": kind},
            privacy_level="private",
            causal_parent_ids=(),
            occurred_at=now,
            event_id=None,
        )
        return self._load_connection(connection, interaction_id)

    @staticmethod
    def _validate_message(channel: str, counterparty: str, content: str) -> None:
        if not channel.strip() or not counterparty.strip() or not content.strip():
            raise ValueError("interaction channel, counterparty and content are required")
        if len(channel) > 128 or len(counterparty) > 512 or len(content) > 100_000:
            raise ValueError("interaction content exceeds its storage limit")

    @staticmethod
    def _state_hash(row: Any, status: str, rationale: str) -> str:
        return InteractionStore._state_hash_values(
            row["content"],
            row["direction"],
            row["kind"],
            row["channel"],
            row["counterparty"],
            status,
            rationale,
        )

    @staticmethod
    def _state_hash_values(
        content: str,
        direction: str,
        kind: str,
        channel: str,
        counterparty: str,
        status: str,
        rationale: str | None,
    ) -> str:
        return content_hash(
            {
                "content_hash": content_hash(content),
                "direction": direction,
                "kind": kind,
                "channel": channel,
                "counterparty": counterparty,
                "status": status,
                "rationale": rationale,
            }
        )

    @staticmethod
    def _get_row(connection: Any, interaction_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"interaction not found: {interaction_id}")
        return row

    @classmethod
    def _load_connection(cls, connection: Any, interaction_id: str) -> InteractionRecord:
        return cls._from_row(cls._get_row(connection, interaction_id))

    @classmethod
    def _from_row(cls, row: Any) -> InteractionRecord:
        if content_hash(row["content"]) != row["content_hash"]:
            raise IntegrityError(f"interaction content hash mismatch: {row['interaction_id']}")
        expected = cls._state_hash_values(
            row["content"],
            row["direction"],
            row["kind"],
            row["channel"],
            row["counterparty"],
            row["status"],
            row["rationale"],
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"interaction state hash mismatch: {row['interaction_id']}")
        return InteractionRecord(
            row["interaction_id"],
            row["subject_id"],
            row["direction"],
            row["kind"],
            row["channel"],
            row["counterparty"],
            row["content"],
            row["related_interaction_id"],
            row["idempotency_key"],
            row["status"],
            row["rationale"],
            row["created_at"],
            row["decided_at"],
        )
