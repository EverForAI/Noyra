"""Bounded, conservative cleanup for derived runtime aggregates."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .database import Database
from .types import canonical_json, content_hash, new_id, strict_json_loads, utc_now


def _positive(value: Any, name: str, *, maximum: int = 36_500) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed < 1 or parsed > maximum:
        raise ValueError(f"{name} is outside the supported range")
    return parsed


@dataclass(frozen=True)
class RetentionSettings:
    health_days: int = 30
    search_use_hours: int = 2
    batch_size: int = 500
    interval_seconds: int = 86_400
    run_history: int = 100

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RetentionSettings:
        env = os.environ if environ is None else environ
        search_use_hours = _positive(
            env.get("NOYRA_RETENTION_SEARCH_USE_HOURS", "2"),
            "search use hours",
            maximum=8_760,
        )
        if search_use_hours < 2:
            raise ValueError("search use hours must preserve the two-hour rate-limit window")
        return cls(
            health_days=_positive(env.get("NOYRA_RETENTION_HEALTH_DAYS", "30"), "health days"),
            search_use_hours=search_use_hours,
            batch_size=_positive(
                env.get("NOYRA_RETENTION_BATCH_SIZE", "500"), "batch size", maximum=500
            ),
            interval_seconds=_positive(
                env.get("NOYRA_RETENTION_INTERVAL_SECONDS", "86400"),
                "interval seconds",
                maximum=31_536_000,
            ),
            run_history=_positive(
                env.get("NOYRA_RETENTION_RUN_HISTORY", "100"), "run history", maximum=10_000
            ),
        )


class RetentionManager:
    def __init__(self, database: Database, settings: RetentionSettings | None = None):
        self.database = database
        self.settings = settings or RetentionSettings()
        with self.database.transaction() as connection:
            self.database._execute_sql_script(
                connection,
                """
                CREATE TABLE IF NOT EXISTS retention_runs (
                    run_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    deleted_by_table_json TEXT NOT NULL,
                    protected_rows INTEGER NOT NULL DEFAULT 0,
                    failed_reason TEXT,
                    next_cursor TEXT,
                    state_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_retention_runs_subject_time
                    ON retention_runs(subject_id, started_at DESC);
                """,
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(retention_runs)").fetchall()
            }
            for name, definition in (
                ("failure_stage", "TEXT"),
                ("retry_at", "TEXT"),
                ("failure_count", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE retention_runs ADD COLUMN {name} {definition}")

    @staticmethod
    def _table_exists(connection: Any, table: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is not None
        )

    def estimate(self, subject_id: str, *, now: datetime | None = None) -> dict[str, int]:
        moment = now or datetime.now(UTC)
        health_cutoff = (moment - timedelta(days=self.settings.health_days)).isoformat()
        search_use_cutoff = (moment - timedelta(hours=self.settings.search_use_hours)).isoformat()
        with self.database.connection() as connection:
            buckets = 0
            if self._table_exists(connection, "provider_health_buckets"):
                buckets = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM provider_health_buckets
                        WHERE subject_id=? AND bucket_start < ?
                        """,
                        (subject_id, health_cutoff),
                    ).fetchone()[0]
                )
            search_uses = 0
            if self._table_exists(connection, "search_provider_uses"):
                search_uses = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM search_provider_uses
                        WHERE subject_id=? AND created_at < ?
                        """,
                        (subject_id, search_use_cutoff),
                    ).fetchone()[0]
                )
            run_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM retention_runs WHERE subject_id=?", (subject_id,)
                ).fetchone()[0]
            )
        return {
            "provider_health_buckets": buckets,
            "search_provider_uses": search_uses,
            "retention_runs": max(0, run_count - self.settings.run_history),
        }

    def run_batch(
        self,
        subject_id: str,
        now: datetime | None = None,
        *,
        batch_size: int | None = None,
    ) -> dict[str, Any]:
        size = (
            self.settings.batch_size
            if batch_size is None
            else _positive(batch_size, "batch size", maximum=500)
        )
        moment = now or datetime.now(UTC)
        cutoff = (moment - timedelta(days=self.settings.health_days)).isoformat()
        search_use_cutoff = (moment - timedelta(hours=self.settings.search_use_hours)).isoformat()
        run_id = new_id("retention")
        started = utc_now()
        deleted: dict[str, int] = {
            "provider_health_buckets": 0,
            "search_provider_uses": 0,
            "provider_health_attempts": 0,
        }
        protected = 0
        failed_reason: str | None = None
        cursor: dict[str, dict[str, Any]] = {
            table: {"cursor": None, "cutoff": cutoff, "deleted": 0, "protected": None}
            for table in (
                "provider_health_buckets",
                "search_provider_uses",
                "provider_health_attempts",
            )
        }
        pruned_run_history = 0
        failure_stage: str | None = None
        try:
            with self.database.transaction() as connection:
                remaining = size
                for table, table_cutoff in (
                    ("provider_health_buckets", cutoff),
                    ("search_provider_uses", search_use_cutoff),
                    ("provider_health_attempts", cutoff),
                ):
                    if not remaining or not self._table_exists(connection, table):
                        continue
                    failure_stage = "delete"
                    count, last_cursor = self._delete_table(
                        connection,
                        table,
                        subject_id,
                        table_cutoff,
                        remaining,
                        cursor[table]["cursor"],
                    )
                    deleted[table] = count
                    cursor[table]["deleted"] += count
                    cursor[table]["cursor"] = last_cursor
                    remaining -= count
                payload = {
                    "run_id": run_id,
                    "subject_id": subject_id,
                    "deleted": deleted,
                    "protected": protected,
                    "cursor": cursor,
                }
                connection.execute(
                    """INSERT INTO retention_runs(
                        run_id, subject_id, started_at, completed_at, deleted_by_table_json,
                        protected_rows, failed_reason, next_cursor, state_hash,
                        failure_stage, retry_at, failure_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        subject_id,
                        started,
                        utc_now(),
                        canonical_json(deleted),
                        protected,
                        failed_reason,
                        canonical_json(cursor),
                        content_hash(payload),
                        None,
                        None,
                        0,
                    ),
                )
                # Retention run summaries are bounded operational metadata,
                # separate from the capped batch of user-facing aggregates.
                stale_runs = connection.execute(
                    "SELECT run_id FROM (SELECT run_id FROM retention_runs "
                    "WHERE subject_id=? ORDER BY started_at DESC, rowid DESC "
                    "LIMIT -1 OFFSET ?) ORDER BY run_id",
                    (subject_id, self.settings.run_history),
                ).fetchall()
                for stale in stale_runs:
                    connection.execute(
                        "DELETE FROM retention_runs WHERE run_id=? AND subject_id=?",
                        (stale["run_id"], subject_id),
                    )
                pruned_run_history = len(stale_runs)
        except Exception as error:
            failed_reason = type(error).__name__
            retry_at = (moment + timedelta(seconds=self.settings.interval_seconds)).isoformat()
            try:
                with self.database.transaction() as connection:
                    previous = connection.execute(
                        "SELECT failure_count FROM retention_runs WHERE subject_id=? "
                        "ORDER BY started_at DESC LIMIT 1",
                        (subject_id,),
                    ).fetchone()
                    failure_count = int(previous["failure_count"] or 0) + 1 if previous else 1
                    payload = {
                        "run_id": run_id,
                        "subject_id": subject_id,
                        "failed_reason": failed_reason,
                        "failure_stage": failure_stage or "transaction",
                        "cursor": cursor,
                    }
                    connection.execute(
                        """INSERT INTO retention_runs(
                            run_id, subject_id, started_at, completed_at, deleted_by_table_json,
                            protected_rows, failed_reason, next_cursor, state_hash,
                            failure_stage, retry_at, failure_count
                        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            run_id,
                            subject_id,
                            started,
                            canonical_json(deleted),
                            protected,
                            failed_reason,
                            canonical_json(cursor),
                            content_hash(payload),
                            failure_stage or "transaction",
                            retry_at,
                            failure_count,
                        ),
                    )
            except Exception:
                # A second failure (for example, a full disk) is returned to
                # the caller; it cannot safely be made durable.
                pass
        return {
            "run_id": run_id,
            "deleted_by_table": deleted,
            "protected_rows": protected,
            "pruned_run_history": pruned_run_history,
            "failed_reason": failed_reason,
            "next_cursor": cursor,
        }

    def _delete_table(
        self,
        connection: Any,
        table: str,
        subject_id: str,
        cutoff: str,
        limit: int,
        cursor: Any,
    ) -> tuple[int, Any]:
        """Delete one bounded table and return its real ordering cursor."""
        if table == "provider_health_buckets":
            rows = connection.execute(
                """SELECT subject_id, provider_kind, provider_id, bucket_start
                   FROM provider_health_buckets
                   WHERE subject_id=? AND bucket_start < ?
                   ORDER BY bucket_start, provider_kind, provider_id LIMIT ?""",
                (subject_id, cutoff, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """DELETE FROM provider_health_buckets
                       WHERE subject_id=? AND provider_kind=? AND provider_id=?
                         AND bucket_start=?""",
                    tuple(row),
                )
            last = None
            if rows:
                row = rows[-1]
                last = {
                    "bucket_start": row["bucket_start"],
                    "provider_kind": row["provider_kind"],
                    "provider_id": row["provider_id"],
                }
            return len(rows), last
        if table == "search_provider_uses":
            rows = connection.execute(
                """SELECT use_id FROM search_provider_uses
                   WHERE subject_id=? AND created_at < ?
                   ORDER BY created_at, use_id LIMIT ?""",
                (subject_id, cutoff, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "DELETE FROM search_provider_uses WHERE use_id=? AND subject_id=?",
                    (row["use_id"], subject_id),
                )
            return len(rows), (str(rows[-1]["use_id"]) if rows else None)
        rows = connection.execute(
            """SELECT attempt_key FROM provider_health_attempts
               WHERE subject_id=? AND occurred_at < ?
               ORDER BY occurred_at, attempt_key LIMIT ?""",
            (subject_id, cutoff, limit),
        ).fetchall()
        for row in rows:
            connection.execute(
                "DELETE FROM provider_health_attempts WHERE attempt_key=?", (row["attempt_key"],)
            )
        return len(rows), (str(rows[-1]["attempt_key"]) if rows else None)

    def latest(self, subject_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM retention_runs WHERE subject_id=? ORDER BY started_at DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "deleted_by_table": strict_json_loads(row["deleted_by_table_json"]),
            "protected_rows": row["protected_rows"],
            "failed_reason": row["failed_reason"],
            "next_cursor": strict_json_loads(row["next_cursor"]) if row["next_cursor"] else None,
            "failure_stage": row["failure_stage"],
            "retry_at": row["retry_at"],
            "failure_count": row["failure_count"],
        }
