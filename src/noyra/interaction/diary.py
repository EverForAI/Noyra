from __future__ import annotations

from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import content_hash, new_id, utc_now

from .errors import InteractionStateConflictError
from .types import PublicDiaryEntry


class PublicDiaryStore:
    """Subject-controlled immutable publication of sleep diary candidates."""

    def __init__(self, database: Database):
        self.database = database

    def publish(
        self,
        subject_id: str,
        source_sleep_id: str,
        title: str,
        body: str,
        *,
        idempotency_key: str | None = None,
        actor: str = "subject",
    ) -> PublicDiaryEntry:
        if actor != "subject":
            raise PermissionError("only the subject can publish a public diary entry")
        if not title.strip() or not body.strip() or not source_sleep_id.strip():
            raise ValueError("public diary title, body and sleep source are required")
        if len(title) > 256 or len(body) > 100_000:
            raise ValueError("public diary content exceeds its storage limit")
        key = idempotency_key or content_hash(
            {"source_sleep_id": source_sleep_id, "title": title, "body": body}
        )
        with self.database.transaction() as connection:
            sleep = connection.execute(
                """SELECT status FROM sleep_runs WHERE sleep_id = ? AND subject_id = ?""",
                (source_sleep_id, subject_id),
            ).fetchone()
            if sleep is None:
                raise NotFoundError("diary sleep source is missing or belongs to another subject")
            if sleep["status"] != "complete":
                raise InteractionStateConflictError("a diary can be published only after wake")
            reflection = connection.execute(
                "SELECT public_diary_candidate FROM sleep_reflections WHERE sleep_id = ?",
                (source_sleep_id,),
            ).fetchone()
            if reflection is None:
                raise InteractionStateConflictError("diary source has no reflection")
            candidate = reflection["public_diary_candidate"]
            if candidate is not None and candidate != body:
                raise InteractionStateConflictError(
                    "published diary body must match the subject's selected candidate"
                )
            existing = connection.execute(
                "SELECT * FROM public_diary_entries WHERE subject_id = ? AND idempotency_key = ?",
                (subject_id, key),
            ).fetchone()
            if existing is not None:
                if existing["body"] != body or existing["title"] != title:
                    raise InteractionStateConflictError("diary idempotency key conflicts")
                return self._from_row(existing)
            now = utc_now()
            entry_id = new_id("diary")
            state_hash = self._state_hash(source_sleep_id, title, body)
            connection.execute(
                """INSERT INTO public_diary_entries(
                    entry_id, subject_id, source_sleep_id, title, body, body_hash,
                    idempotency_key, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry_id,
                    subject_id,
                    source_sleep_id,
                    title,
                    body,
                    content_hash(body),
                    key,
                    state_hash,
                    now,
                ),
            )
            return self._load_connection(connection, entry_id)

    def list(self, subject_id: str, *, limit: int = 100) -> list[PublicDiaryEntry]:
        bounded = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM public_diary_entries WHERE subject_id = ? "
                "ORDER BY created_at DESC, entry_id DESC LIMIT ?",
                (subject_id, bounded),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _state_hash(source_sleep_id: str, title: str, body: str) -> str:
        return content_hash(
            {"source_sleep_id": source_sleep_id, "title": title, "body_hash": content_hash(body)}
        )

    @classmethod
    def _from_row(cls, row: Any) -> PublicDiaryEntry:
        if content_hash(row["body"]) != row["body_hash"]:
            raise IntegrityError(f"public diary body hash mismatch: {row['entry_id']}")
        if cls._state_hash(row["source_sleep_id"], row["title"], row["body"]) != row["state_hash"]:
            raise IntegrityError(f"public diary state hash mismatch: {row['entry_id']}")
        return PublicDiaryEntry(
            row["entry_id"],
            row["subject_id"],
            row["source_sleep_id"],
            row["title"],
            row["body"],
            row["created_at"],
        )

    @classmethod
    def _load_connection(cls, connection: Any, entry_id: str) -> PublicDiaryEntry:
        row = connection.execute(
            "SELECT * FROM public_diary_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"public diary entry not found: {entry_id}")
        return cls._from_row(row)
