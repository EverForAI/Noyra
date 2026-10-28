"""Durable acquisition heartbeat, independent of new observation creation."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash

from .feed import FEED_FINGERPRINT_VERSION

MAX_SEEN_VERSIONS = 4_096


class SourcePollStore:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _read(
        connection: sqlite3.Connection, subject_id: str, source_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM world_poll_state WHERE subject_id=? AND source_id=?",
            (subject_id, source_id),
        ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        state_hash = payload.pop("state_hash")
        if content_hash(payload) != state_hash:
            raise IntegrityError("world poll state hash mismatch")
        return payload

    def last_checked_at(self, subject_id: str, source_id: str) -> str | None:
        with self.database.connection() as connection:
            state = self._read(connection, subject_id, source_id)
            if state is not None:
                return str(state["last_attempt_at"])
            # Only a genuinely absent row uses legacy observations. A missing
            # table is an incompatible schema and must fail closed.
            row = connection.execute(
                "SELECT MAX(fetched_at) FROM observations WHERE subject_id=? AND source_id=?",
                (subject_id, source_id),
            ).fetchone()
        return None if row is None else row[0]

    @classmethod
    def complete(
        cls,
        connection: sqlite3.Connection,
        subject_id: str,
        source_id: str,
        checked_at: str,
        *,
        outcome: str,
        new_versions: int = 0,
    ) -> None:
        previous = cls._read(connection, subject_id, source_id)
        checked = datetime.fromisoformat(checked_at)
        if checked.tzinfo is None or checked.utcoffset() is None:
            raise IntegrityError("world poll timestamp must be timezone aware")
        if previous is not None and checked < datetime.fromisoformat(previous["last_attempt_at"]):
            raise IntegrityError("world poll completion is out of order")
        legacy = (
            connection.execute(
                "SELECT 1 FROM observations WHERE subject_id=? AND source_id=? LIMIT 1",
                (subject_id, source_id),
            ).fetchone()
            is not None
        )
        payload = {
            "source_id": source_id,
            "subject_id": subject_id,
            "fingerprint_version": FEED_FINGERPRINT_VERSION,
            "last_attempt_at": checked_at,
            "last_success_at": (
                checked_at
                if outcome != "failed"
                else previous["last_success_at"]
                if previous
                else None
            ),
            "last_change_at": (
                checked_at
                if outcome == "changed"
                else previous["last_change_at"]
                if previous
                else None
            ),
            "outcome": outcome,
            "checks": int(previous["checks"]) + 1 if previous else 1,
            "unchanged_checks": (int(previous["unchanged_checks"]) if previous else 0)
            + int(outcome == "unchanged"),
            "new_item_versions": (int(previous["new_item_versions"]) if previous else 0)
            + new_versions,
            "baseline": previous["baseline"]
            if previous
            else "legacy_unavailable"
            if legacy
            else "fresh",
        }
        columns = (*tuple(payload), "state_hash")
        updates = ",".join(f"{name}=excluded.{name}" for name in columns if name != "source_id")
        connection.execute(
            f"INSERT INTO world_poll_state({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)}) "
            f"ON CONFLICT(source_id) DO UPDATE SET {updates}",
            (*payload.values(), content_hash(payload)),
        )

    def failed(self, subject_id: str, source_id: str, checked_at: str) -> None:
        with self.database.transaction() as connection:
            self.complete(connection, subject_id, source_id, checked_at, outcome="failed")

    @staticmethod
    def prune(connection: sqlite3.Connection, source_id: str) -> None:
        # Evict by first appearance, never by feed ordering or last sighting.
        # This is a bounded cache: an item returning after eviction may be
        # observed again. No historical observation is rewritten or deleted.
        connection.execute(
            "DELETE FROM world_feed_seen WHERE source_id=? AND rowid NOT IN ("
            "SELECT rowid FROM world_feed_seen WHERE source_id=? "
            "ORDER BY first_seen_at DESC,rowid DESC LIMIT ?)",
            (source_id, source_id, MAX_SEEN_VERSIONS),
        )
