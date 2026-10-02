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


@dataclass(frozen=True)
class RetentionTableSpec:
    """One explicit lifecycle contract for a persistent, long-lived table.

    ``retention_action`` is deliberately explicit.  A table is never placed
    in the DELETE plan merely because it has a timestamp column; append-only
    evidence therefore remains protected even when a future migration adds a
    new growing table.
    """

    table: str
    retention_class: str
    retention_action: str
    time_column: str | None
    id_columns: tuple[str, ...]
    cutoff_setting: str | None
    cutoff_kind: str = "time"

    def __post_init__(self) -> None:
        if self.retention_class not in {
            "core_evidence",
            "required_audit",
            "rebuildable_aggregate",
            "temporary_queue",
        }:
            raise ValueError(f"unknown retention class for {self.table}")
        if self.retention_action not in {"preserve", "delete"}:
            raise ValueError(f"unknown retention action for {self.table}")
        if self.retention_action == "delete" and (
            not self.time_column or not self.id_columns or not self.cutoff_setting
        ):
            raise ValueError(f"delete policy for {self.table} is incomplete")
        if self.cutoff_kind not in {"time", "count"}:
            raise ValueError(f"unknown cutoff kind for {self.table}")


# This is the single inventory used by projection, estimation and cleanup.
# Evidence tables are registered even though they are never deleted.  Keeping
# them here makes an omitted table a visible diagnostic instead of an implicit
# retention policy.
RETENTION_REGISTRY: tuple[RetentionTableSpec, ...] = (
    RetentionTableSpec(
        "cognitive_route_decisions",
        "core_evidence",
        "preserve",
        "created_at",
        ("decision_id",),
        None,
    ),
    RetentionTableSpec(
        "cognitive_route_attempts", "core_evidence", "preserve", "created_at", ("attempt_id",), None
    ),
    RetentionTableSpec(
        "cognitive_route_outcomes", "core_evidence", "preserve", "created_at", ("outcome_id",), None
    ),
    RetentionTableSpec(
        "model_calls", "required_audit", "preserve", "created_at", ("call_id",), None
    ),
    RetentionTableSpec(
        "model_attempts", "required_audit", "preserve", "started_at", ("attempt_id",), None
    ),
    RetentionTableSpec(
        "research_search_runs", "core_evidence", "preserve", "created_at", ("research_id",), None
    ),
    RetentionTableSpec(
        "action_deliberation_runs",
        "core_evidence",
        "preserve",
        "created_at",
        ("deliberation_id",),
        None,
    ),
    RetentionTableSpec(
        "behavior_logs", "core_evidence", "preserve", "occurred_at", ("log_id",), None
    ),
    RetentionTableSpec(
        "provider_health_buckets",
        "rebuildable_aggregate",
        "delete",
        "bucket_start",
        ("bucket_start", "provider_kind", "provider_id"),
        "health_days",
    ),
    RetentionTableSpec(
        "provider_health_attempts",
        "rebuildable_aggregate",
        "delete",
        "occurred_at",
        ("attempt_key",),
        "health_days",
    ),
    RetentionTableSpec(
        "search_provider_uses",
        "temporary_queue",
        "delete",
        "created_at",
        ("use_id",),
        "search_use_hours",
    ),
    RetentionTableSpec(
        "retention_runs",
        "temporary_queue",
        "delete",
        "started_at",
        ("run_id",),
        "run_history",
        cutoff_kind="count",
    ),
)

_RETENTION_BY_TABLE = {item.table: item for item in RETENTION_REGISTRY}
_DELETE_SPECS = tuple(item for item in RETENTION_REGISTRY if item.retention_action == "delete")
RETENTION_TABLES = tuple(item.table for item in _DELETE_SPECS)
RETENTION_REGISTRY_VERSION = content_hash(
    [
        {
            "table": item.table,
            "class": item.retention_class,
            "action": item.retention_action,
            "time": item.time_column,
            "ids": item.id_columns,
            "cutoff": item.cutoff_setting,
            "cutoff_kind": item.cutoff_kind,
        }
        for item in RETENTION_REGISTRY
    ]
)
_CURSOR_META_KEY = "_meta"

# Backwards-compatible view used by callers that need a time/id pair.  The
# actual policy remains the dataclass registry above.
DERIVED_RUNTIME_TABLES: dict[str, tuple[str, str]] = {
    item.table: (item.time_column or "", item.id_columns[0])
    for item in _DELETE_SPECS
    if item.table not in {"provider_health_buckets", "search_provider_uses", "retention_runs"}
}
_LEGACY_RETENTION_TABLES = {"cognitive_route_attempts", "cognitive_route_outcomes"}
_CURSOR_SORT_KEY_VERSION = "v1"


def retention_registry_diagnostics(connection: Any | None = None) -> dict[str, tuple[str, ...]]:
    """Return deterministic diagnostics for operators and integrity checks."""
    diagnostics = {
        "delete_tables": tuple(item.table for item in _DELETE_SPECS),
        "preserve_tables": tuple(
            item.table for item in RETENTION_REGISTRY if item.retention_action == "preserve"
        ),
        "unclassified": (),
    }
    if connection is None:
        return diagnostics
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        ).fetchall()
        if str(row[0]) not in {"sqlite_sequence"}
    }
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='retention_contract_inventory'"
    ).fetchone()
    if row is None:
        diagnostics["unclassified"] = tuple(sorted(tables))
        return diagnostics
    try:
        baseline = strict_json_loads(str(row[0]))
    except (TypeError, ValueError):
        diagnostics["unclassified"] = tuple(sorted(tables))
        return diagnostics
    if not isinstance(baseline, list) or not all(
        isinstance(item, str) and item for item in baseline
    ):
        diagnostics["unclassified"] = tuple(sorted(tables))
        return diagnostics
    diagnostics["unclassified"] = tuple(sorted(tables - set(baseline)))
    diagnostics["missing"] = tuple(sorted(set(baseline) - tables))
    return diagnostics


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
    expected.add(_CURSOR_META_KEY)
    if set(cursor) - expected:
        return False
    metadata = cursor.get(_CURSOR_META_KEY)
    if metadata is not None and (
        not isinstance(metadata, dict)
        or set(metadata)
        != {
            "registry_version",
            "data_epoch",
            "cutoff_policy",
            "cutoffs",
            "sort_key_version",
        }
        or not all(
            isinstance(metadata.get(key), str) and metadata[key]
            for key in ("registry_version", "data_epoch", "cutoff_policy", "sort_key_version")
        )
    ):
        return False
    if metadata is not None:
        cutoffs = metadata.get("cutoffs")
        if not isinstance(cutoffs, dict) or set(cutoffs) != set(RETENTION_TABLES):
            return False
        if not all(_valid_iso(value) for value in cutoffs.values()):
            return False
    for _table, entry in cursor.items():
        if _table == _CURSOR_META_KEY:
            continue
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
        with self.database.connection() as connection:
            estimate: dict[str, int] = {}
            for spec in _DELETE_SPECS:
                if not self._table_exists(connection, spec.table):
                    estimate[spec.table] = 0
                    continue
                if spec.cutoff_kind == "count":
                    count = int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {spec.table} WHERE subject_id=?",
                            (subject_id,),
                        ).fetchone()[0]
                    )
                    estimate[spec.table] = max(0, count - self.settings.run_history)
                    continue
                cutoff = self._cutoff_for(spec, moment)
                estimate[spec.table] = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {spec.table} WHERE subject_id=? "
                        f"AND {spec.time_column} < ?",
                        (subject_id, cutoff),
                    ).fetchone()[0]
                )
        return estimate

    def _cutoff_for(self, spec: RetentionTableSpec, moment: datetime) -> str:
        if spec.cutoff_setting == "health_days":
            cutoff = moment - timedelta(days=self.settings.health_days)
        elif spec.cutoff_setting == "search_use_hours":
            cutoff = moment - timedelta(hours=self.settings.search_use_hours)
        elif spec.cutoff_setting == "runtime_days":
            cutoff = moment - timedelta(days=self.settings.runtime_days)
        elif spec.cutoff_setting == "run_history":
            cutoff = moment
        else:
            raise ValueError(f"unknown retention cutoff setting for {spec.table}")
        return cutoff.isoformat()

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
        run_id = new_id("retention")
        started = utc_now()
        deleted: dict[str, int] = {spec.table: 0 for spec in _DELETE_SPECS}
        protected = 0
        failed_reason: str | None = None
        cutoffs = {spec.table: self._cutoff_for(spec, moment) for spec in _DELETE_SPECS}
        cursor: dict[str, dict[str, Any]] = {
            spec.table: {
                "cursor": None,
                "cutoff": cutoffs[spec.table],
                "deleted": 0,
                "protected": None,
            }
            for spec in _DELETE_SPECS
        }
        with self.database.connection() as connection:
            schema_row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        cursor[_CURSOR_META_KEY] = {
            "registry_version": RETENTION_REGISTRY_VERSION,
            "data_epoch": str(schema_row[0]) if schema_row is not None else "unknown",
            "cutoff_policy": content_hash(
                {
                    "health_days": self.settings.health_days,
                    "search_use_hours": self.settings.search_use_hours,
                    "run_history": self.settings.run_history,
                    "runtime_days": self.settings.runtime_days,
                }
            ),
            "cutoffs": cutoffs,
            "sort_key_version": _CURSOR_SORT_KEY_VERSION,
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
            if _valid_cursor(previous_cursor) and self._cursor_compatible(previous_cursor, cursor):
                for table in RETENTION_TABLES:
                    entry = previous_cursor.get(table)
                    if isinstance(entry, dict):
                        cursor[table]["cursor"] = entry.get("cursor")
        pruned_run_history = 0
        failure_stage: str | None = None
        try:
            with self.database.transaction() as connection:
                remaining = size
                for spec in _DELETE_SPECS:
                    table = spec.table
                    if spec.cutoff_kind == "count":
                        continue
                    if not remaining or not self._table_exists(connection, table):
                        continue
                    failure_stage = "delete"
                    count, last_cursor = self._delete_table(
                        connection,
                        table,
                        subject_id,
                        cursor[table]["cutoff"],
                        remaining,
                        cursor[table]["cursor"],
                    )
                    deleted[table] = count
                    cursor[table]["deleted"] += count
                    cursor[table]["cursor"] = last_cursor
                    remaining -= count
                # Retention run summaries are bounded operational metadata,
                # separate from the capped batch of user-facing aggregates.
                stale_runs = connection.execute(
                    "SELECT run_id FROM (SELECT run_id FROM retention_runs "
                    "WHERE subject_id=? ORDER BY started_at DESC, rowid DESC "
                    "LIMIT -1 OFFSET ?) ORDER BY run_id",
                    (subject_id, max(self.settings.run_history - 1, 0)),
                ).fetchall()
                for stale in stale_runs:
                    connection.execute(
                        "DELETE FROM retention_runs WHERE run_id=? AND subject_id=?",
                        (stale["run_id"], subject_id),
                    )
                pruned_run_history = len(stale_runs)
                deleted["retention_runs"] = pruned_run_history
                cursor["retention_runs"]["deleted"] = pruned_run_history
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
            "protected_rows": protected,
            "protected_rows_reason": (
                "protected row predicates are not configured for this registry"
            ),
            "pruned_run_history": pruned_run_history,
            "failed_reason": failed_reason,
            "next_cursor": cursor,
        }

    @staticmethod
    def _cursor_compatible(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
        previous_meta = previous.get(_CURSOR_META_KEY)
        current_meta = current.get(_CURSOR_META_KEY)
        if previous_meta is None or current_meta is None:
            return previous_meta is None and current_meta is not None
        return bool(previous_meta == current_meta)

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
            use_params: list[Any] = [subject_id, cutoff]
            if isinstance(cursor, dict) and {"created_at", "use_id"} <= set(cursor):
                keyset = " AND (created_at > ? OR (created_at = ? AND use_id > ?))"
                use_params.extend([cursor["created_at"], cursor["created_at"], cursor["use_id"]])
            use_params.append(limit)
            rows = connection.execute(
                f"""SELECT use_id, created_at FROM search_provider_uses
                   WHERE subject_id=? AND created_at < ?{keyset}
                   ORDER BY created_at, use_id LIMIT ?""",
                tuple(use_params),
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
