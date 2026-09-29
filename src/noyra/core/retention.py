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
    runtime_days: int = 90

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
            runtime_days=_positive(env.get("NOYRA_RETENTION_RUNTIME_DAYS", "90"), "runtime days"),
        )


# Only rebuildable runtime projections belong here. Immutable actions,
# moderation, wallet and audit evidence are intentionally excluded.
DERIVED_RUNTIME_TABLES: dict[str, tuple[str, str]] = {
    # Append-only route evidence is deliberately excluded.  These tables have
    # database triggers which reject DELETE; retention must never schedule
    # them as rebuildable projections.
}

RETENTION_TABLES = (
    "provider_health_buckets",
    "search_provider_uses",
    "provider_health_attempts",
    *DERIVED_RUNTIME_TABLES,
)
_LEGACY_RETENTION_TABLES = {"cognitive_route_attempts", "cognitive_route_outcomes"}


def _valid_iso(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_cursor(cursor: Any, *, allow_legacy: bool = True) -> bool:
    if not isinstance(cursor, dict):
        return False
    expected = set(RETENTION_TABLES)
    if allow_legacy:
        expected |= _LEGACY_RETENTION_TABLES
    if set(cursor) - expected:
        return False
    for table, entry in cursor.items():
        if not isinstance(entry, dict):
            return False
        if set(entry) != {"cursor", "cutoff", "deleted", "protected"}:
            return False
        if not _valid_iso(entry["cutoff"]):
            return False
        if isinstance(entry["deleted"], bool) or not isinstance(entry["deleted"], int):
            return False
        if entry["deleted"] < 0:
            return False
        if entry["protected"] is not None and (
            isinstance(entry["protected"], bool)
            or not isinstance(entry["protected"], int)
            or entry["protected"] < 0
        ):
            return False
        value = entry["cursor"]
        if value is not None and not isinstance(value, (str, dict)):
            return False
    return True


def validate_retention_run_row(row: Mapping[str, Any]) -> None:
    """Validate a persisted retention row and its canonical provenance hash."""
    def field(name: str, default: Any = None) -> Any:
        try:
            return row[name]
        except (KeyError, IndexError):
            return default

    if not isinstance(field("run_id"), str) or not field("run_id"):
        raise ValueError("retention run id is invalid")
    if not isinstance(field("subject_id"), str) or not field("subject_id"):
        raise ValueError("retention subject id is invalid")
    if not _valid_iso(field("started_at")):
        raise ValueError("retention start time is invalid")
    if field("completed_at") is not None and not _valid_iso(field("completed_at")):
        raise ValueError("retention completion time is invalid")
    try:
        deleted = strict_json_loads(str(field("deleted_by_table_json")))
        cursor = strict_json_loads(str(field("next_cursor"))) if field("next_cursor") else None
    except (TypeError, ValueError) as error:
        raise ValueError("retention provenance JSON is invalid") from error
    if not isinstance(deleted, dict) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in deleted.values()
    ):
        raise ValueError("retention deletion counts are invalid")
    if cursor is None or not _valid_cursor(cursor):
        raise ValueError("retention cursor is invalid")
    protected = field("protected_rows")
    if isinstance(protected, bool) or not isinstance(protected, int) or protected < 0:
        raise ValueError("retention protected count is invalid")
    failure = field("failed_reason")
    stage = field("failure_stage")
    if field("completed_at") is None:
        if not isinstance(failure, str) or not failure:
            raise ValueError("failed retention run has no failure reason")
        if not isinstance(stage, str) or not stage:
            raise ValueError("failed retention run has no failure stage")
        payload = {
            "run_id": field("run_id"),
            "subject_id": field("subject_id"),
            "failed_reason": failure,
            "failure_stage": stage,
            "cursor": cursor,
        }
    else:
        if failure is not None or stage is not None:
            raise ValueError("successful retention run contains failure metadata")
        payload = {
            "run_id": field("run_id"),
            "subject_id": field("subject_id"),
            "deleted": deleted,
            "protected": protected,
            "cursor": cursor,
        }
    if field("state_hash") != content_hash(payload):
        raise ValueError("retention run state hash mismatch")


class RetentionManager:
    def __init__(self, database: Database, settings: RetentionSettings | None = None):
        self.database = database
        self.settings = settings or RetentionSettings()
        with self.database.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_runs'"
            ).fetchone()
        if exists is None:
            raise RuntimeError("retention schema is unavailable; run the database migrations first")

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
        runtime_cutoff = (moment - timedelta(days=self.settings.runtime_days)).isoformat()
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
            for table in RETENTION_TABLES
        }
        # Continue a prior bounded batch when its cutoff is still applicable.
        # A malformed/legacy cursor is ignored and safely starts a fresh pass.
        with self.database.connection() as connection:
            previous = connection.execute(
                "SELECT next_cursor FROM retention_runs WHERE subject_id=? "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
        if previous is not None and previous["next_cursor"]:
            try:
                previous_cursor = strict_json_loads(previous["next_cursor"])
            except (TypeError, ValueError):
                previous_cursor = None
            if _valid_cursor(previous_cursor):
                for table in RETENTION_TABLES:
                    entry = previous_cursor.get(table)
                    if isinstance(entry, dict):
                        cursor[table]["cursor"] = entry.get("cursor")
        pruned_run_history = 0
        failure_stage: str | None = None
        try:
            with self.database.transaction() as connection:
                remaining = size
                for table, table_cutoff in (
                    ("provider_health_buckets", cutoff),
                    ("search_provider_uses", search_use_cutoff),
                    ("provider_health_attempts", cutoff),
                    *[(table, runtime_cutoff) for table in DERIVED_RUNTIME_TABLES],
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
                        failure_stage, retry_at, failure_count, protected_rows_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        "protected row predicates are not configured for this registry",
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
                            failure_stage, retry_at, failure_count, protected_rows_reason
                        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                            "protected row predicates are not configured for this registry",
                        ),
                    )
            except Exception:
                # A second failure (for example, a full disk) is returned to
                # the caller; it cannot safely be made durable.
                pass
        return {
            "run_id": run_id,
            "deleted_by_table": deleted,
            "protected_rows": None,
            "protected_rows_reason": (
                "protected row predicates are not configured for this registry"
            ),
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
            keyset = ""
            params: list[Any] = [subject_id, cutoff]
            if isinstance(cursor, dict) and {
                "bucket_start",
                "provider_kind",
                "provider_id",
            } <= set(cursor):
                keyset = (
                    " AND (bucket_start > ? OR "
                    "(bucket_start = ? AND provider_kind > ?) OR "
                    "(bucket_start = ? AND provider_kind = ? AND provider_id > ?))"
                )
                params.extend(
                    [
                        cursor["bucket_start"],
                        cursor["bucket_start"],
                        cursor["provider_kind"],
                        cursor["bucket_start"],
                        cursor["provider_kind"],
                        cursor["provider_id"],
                    ]
                )
            params.append(limit)
            rows = connection.execute(
                f"""SELECT subject_id, provider_kind, provider_id, bucket_start
                   FROM provider_health_buckets
                   WHERE subject_id=? AND bucket_start < ?{keyset}
                   ORDER BY bucket_start, provider_kind, provider_id LIMIT ?""",
                tuple(params),
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
            keyset = ""
            params: list[Any] = [subject_id, cutoff]
            if isinstance(cursor, dict) and {"created_at", "use_id"} <= set(cursor):
                keyset = " AND (created_at > ? OR (created_at = ? AND use_id > ?))"
                params.extend([cursor["created_at"], cursor["created_at"], cursor["use_id"]])
            params.append(limit)
            rows = connection.execute(
                f"""SELECT use_id, created_at FROM search_provider_uses
                   WHERE subject_id=? AND created_at < ?{keyset}
                   ORDER BY created_at, use_id LIMIT ?""",
                tuple(params),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "DELETE FROM search_provider_uses WHERE use_id=? AND subject_id=?",
                    (row["use_id"], subject_id),
                )
            return len(rows), (
                {"created_at": rows[-1]["created_at"], "use_id": str(rows[-1]["use_id"])}
                if rows
                else None
            )
        if table in DERIVED_RUNTIME_TABLES:
            time_column, id_column = DERIVED_RUNTIME_TABLES[table]
            keyset = ""
            params = [subject_id, cutoff]
            if isinstance(cursor, dict) and {time_column, id_column} <= set(cursor):
                keyset = f" AND ({time_column} > ? OR ({time_column} = ? AND {id_column} > ?))"
                params.extend([cursor[time_column], cursor[time_column], cursor[id_column]])
            params.append(limit)
            rows = connection.execute(
                f"SELECT {id_column}, {time_column} FROM {table} WHERE subject_id=? "
                f"AND {time_column} < ?{keyset} ORDER BY {time_column}, {id_column} LIMIT ?",
                tuple(params),
            ).fetchall()
            for row in rows:
                connection.execute(
                    f"DELETE FROM {table} WHERE {id_column}=? AND subject_id=?",
                    (row[id_column], subject_id),
                )
            return len(rows), (
                {time_column: rows[-1][time_column], id_column: str(rows[-1][id_column])}
                if rows
                else None
            )
        keyset = ""
        params = [subject_id, cutoff]
        if isinstance(cursor, dict) and {"occurred_at", "attempt_key"} <= set(cursor):
            keyset = " AND (occurred_at > ? OR (occurred_at = ? AND attempt_key > ?))"
            params.extend([cursor["occurred_at"], cursor["occurred_at"], cursor["attempt_key"]])
        params.append(limit)
        rows = connection.execute(
            f"""SELECT attempt_key, occurred_at FROM provider_health_attempts
               WHERE subject_id=? AND occurred_at < ?{keyset}
               ORDER BY occurred_at, attempt_key LIMIT ?""",
            tuple(params),
        ).fetchall()
        for row in rows:
            connection.execute(
                "DELETE FROM provider_health_attempts WHERE attempt_key=?", (row["attempt_key"],)
            )
        return len(rows), (
            {"occurred_at": rows[-1]["occurred_at"], "attempt_key": str(rows[-1]["attempt_key"])}
            if rows
            else None
        )

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
            "protected_rows": None,
            "protected_rows_reason": row["protected_rows_reason"],
            "failed_reason": row["failed_reason"],
            "next_cursor": strict_json_loads(row["next_cursor"]) if row["next_cursor"] else None,
            "failure_stage": row["failure_stage"],
            "retry_at": row["retry_at"],
            "failure_count": row["failure_count"],
        }
