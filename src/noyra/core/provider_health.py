"""Small, credential-free provider health aggregates and circuit breaker state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from .database import Database
from .errors import IntegrityError
from .types import content_hash, new_id, utc_now


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _bucket(value: str) -> str:
    stamp = _parse_time(value)
    assert stamp is not None
    return stamp.replace(minute=0, second=0, microsecond=0).isoformat()


def _error_category(error_code: str | None) -> str | None:
    if not error_code:
        return None
    normalized = error_code.casefold()
    if "timeout" in normalized:
        return "timeout"
    if "429" in normalized or "rate" in normalized or "throttle" in normalized:
        return "rate_limited"
    if "401" in normalized or "403" in normalized or "auth" in normalized:
        return "auth"
    if "schema" in normalized or "json" in normalized or "format" in normalized:
        return "schema"
    if any(code in normalized for code in ("500", "502", "503", "504", "5xx")):
        return "server_error"
    return "unknown"


def _percentile(samples: list[int], fraction: float) -> int:
    if not samples:
        return 0
    return samples[min(len(samples) - 1, int(len(samples) * fraction))]


class ProviderHealthStore:
    """Persist provider outcomes as bounded hourly aggregates.

    Only hourly counters and current breaker state are persisted. Individual
    attempts, identifiers, error codes, URLs, and request/response data are
    never stored. The caller supplies an attempt id for call-site consistency;
    it is deliberately not retained or used as a per-call deduplication key.
    """

    def __init__(self, database: Database, *, failure_threshold: int = 3):
        if failure_threshold < 1:
            raise ValueError("failure threshold must be positive")
        self.database = database
        self.failure_threshold = failure_threshold
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with self.database.connection() as connection:
            missing = [
                table
                for table in ("provider_health_buckets", "provider_health_state")
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                is None
            ]
        if missing:
            raise IntegrityError(
                "provider health schema is unavailable; run the database migrations first"
            )

    @staticmethod
    def _bucket_hash(
        subject_id: str,
        provider_kind: str,
        provider_id: str,
        bucket_start: str,
        attempt_count: int,
        success_count: int,
        failure_count: int,
        latency_total_ms: int,
        last_success_at: str | None,
        last_failure_at: str | None,
        error_counts: dict[str, int] | None = None,
        latency_samples: list[int] | tuple[int, ...] | None = None,
    ) -> str:
        return content_hash(
            {
                "subject_id": subject_id,
                "provider_kind": provider_kind,
                "provider_id": provider_id,
                "bucket_start": bucket_start,
                "attempt_count": attempt_count,
                "success_count": success_count,
                "failure_count": failure_count,
                "latency_total_ms": latency_total_ms,
                "last_success_at": last_success_at,
                "last_failure_at": last_failure_at,
                "error_counts": error_counts or {},
                "latency_samples": list(latency_samples or ()),
            }
        )

    @staticmethod
    def _legacy_bucket_hash(row: Any) -> str:
        return content_hash(
            {
                "subject": row["subject_id"],
                "kind": row["provider_kind"],
                "provider": row["provider_id"],
                "bucket": row["bucket_start"],
                "attempts": int(row["attempt_count"]),
                "successes": int(row["success_count"]),
                "failures": int(row["failure_count"]),
                "latency": int(row["latency_total_ms"]),
            }
        )

    @staticmethod
    def _state_hash(
        subject_id: str,
        provider_kind: str,
        provider_id: str,
        state: str,
        cooldown_until: str | None,
        probe_token: str | None,
        probe_started_at: str | None,
        consecutive_failures: int,
        last_success_at: str | None,
        last_failure_at: str | None,
        updated_at: str,
    ) -> str:
        return content_hash(
            {
                "subject_id": subject_id,
                "provider_kind": provider_kind,
                "provider_id": provider_id,
                "state": state,
                "cooldown_until": cooldown_until,
                "probe_token": probe_token,
                "probe_started_at": probe_started_at,
                "consecutive_failures": consecutive_failures,
                "last_success_at": last_success_at,
                "last_failure_at": last_failure_at,
                "updated_at": updated_at,
            }
        )

    @staticmethod
    def _legacy_state_hash(row: Any) -> str:
        return content_hash(
            {
                "subject": row["subject_id"],
                "kind": row["provider_kind"],
                "provider": row["provider_id"],
                "state": row["state"],
                "cooldown": row["cooldown_until"],
                "consecutive": int(row["consecutive_failures"]),
                "updated": row["updated_at"],
            }
        )

    @classmethod
    def _verify_bucket(cls, row: Any) -> None:
        attempts = int(row["attempt_count"])
        successes = int(row["success_count"])
        failures = int(row["failure_count"])
        latency = int(row["latency_total_ms"])
        try:
            error_counts = json.loads(row["error_counts_json"] or "{}")
            latency_samples = json.loads(row["latency_samples_json"] or "[]")
        except (TypeError, ValueError) as error:
            raise IntegrityError("provider health detail counters are invalid") from error
        if (
            attempts < 0
            or successes < 0
            or failures < 0
            or successes + failures != attempts
            or latency < 0
            or row["state_hash"]
            not in {
                cls._bucket_hash(
                    row["subject_id"],
                    row["provider_kind"],
                    row["provider_id"],
                    row["bucket_start"],
                    attempts,
                    successes,
                    failures,
                    latency,
                    row["last_success_at"],
                    row["last_failure_at"],
                    error_counts,
                    latency_samples,
                ),
                cls._legacy_bucket_hash(row),
            }
        ):
            raise IntegrityError("provider health bucket integrity check failed")

    @classmethod
    def _verify_state(cls, row: Any) -> None:
        consecutive = int(row["consecutive_failures"])
        if (
            row["state"] not in {"healthy", "degraded", "cooldown", "half_open"}
            or consecutive < 0
            or not isinstance(row["updated_at"], str)
            or row["state_hash"]
            not in {
                cls._state_hash(
                    row["subject_id"],
                    row["provider_kind"],
                    row["provider_id"],
                    row["state"],
                    row["cooldown_until"],
                    row["probe_token"],
                    row["probe_started_at"],
                    consecutive,
                    row["last_success_at"],
                    row["last_failure_at"],
                    row["updated_at"],
                ),
                cls._legacy_state_hash(row),
            }
        ):
            raise IntegrityError("provider health state integrity check failed")
        for key in ("cooldown_until", "probe_started_at", "last_success_at", "last_failure_at"):
            if row[key] is not None and _parse_time(row[key]) is None:
                raise IntegrityError("provider health timestamp is invalid")

    def record_attempt(
        self,
        subject_id: str,
        provider_kind: str,
        provider_id: str,
        attempt_id: str,
        success: bool,
        latency_ms: int,
        error_code: str | None,
        *,
        cooldown_seconds: int = 60,
    ) -> bool:
        if not all(
            isinstance(value, str) and value
            for value in (subject_id, provider_kind, provider_id, attempt_id)
        ):
            raise ValueError("provider health identity is invalid")
        if (
            type(success) is not bool
            or isinstance(latency_ms, bool)
            or not isinstance(latency_ms, int)
            or latency_ms < 0
            or isinstance(cooldown_seconds, bool)
            or not isinstance(cooldown_seconds, int)
            or cooldown_seconds < 0
        ):
            raise ValueError("latency and cooldown must be non-negative")
        now = utc_now()
        bucket = _bucket(now)
        category = _error_category(error_code)
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM provider_health_buckets
                WHERE subject_id=? AND provider_kind=? AND provider_id=? AND bucket_start=?
                """,
                (subject_id, provider_kind, provider_id, bucket),
            ).fetchone()
            if existing is not None:
                self._verify_bucket(existing)
            success_count = int(existing["success_count"]) if existing else 0
            failure_count = int(existing["failure_count"]) if existing else 0
            attempt_count = int(existing["attempt_count"]) if existing else 0
            latency_total = int(existing["latency_total_ms"]) if existing else 0
            error_counts = json.loads(existing["error_counts_json"] or "{}") if existing else {}
            latency_samples = (
                json.loads(existing["latency_samples_json"] or "[]") if existing else []
            )
            if category is not None:
                error_counts[category] = int(error_counts.get(category, 0)) + 1
            latency_samples = [*latency_samples, latency_ms][-128:]
            success_count += int(success)
            failure_count += int(not success)
            attempt_count += 1
            latency_total += latency_ms
            last_success = now if success else (existing["last_success_at"] if existing else None)
            last_failure = (
                now if not success else (existing["last_failure_at"] if existing else None)
            )
            bucket_hash = self._bucket_hash(
                subject_id,
                provider_kind,
                provider_id,
                bucket,
                attempt_count,
                success_count,
                failure_count,
                latency_total,
                last_success,
                last_failure,
                error_counts,
                latency_samples,
            )
            connection.execute(
                """
                INSERT INTO provider_health_buckets
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_id, provider_kind, provider_id, bucket_start)
                DO UPDATE SET attempt_count=excluded.attempt_count,
                    success_count=excluded.success_count,
                    failure_count=excluded.failure_count,
                    latency_total_ms=excluded.latency_total_ms,
                    last_success_at=excluded.last_success_at,
                    last_failure_at=excluded.last_failure_at,
                    error_counts_json=excluded.error_counts_json,
                    latency_samples_json=excluded.latency_samples_json,
                    state_hash=excluded.state_hash
                """,
                (
                    subject_id,
                    provider_kind,
                    provider_id,
                    bucket,
                    attempt_count,
                    success_count,
                    failure_count,
                    latency_total,
                    last_success,
                    last_failure,
                    bucket_hash,
                    json.dumps(error_counts, sort_keys=True, separators=(",", ":")),
                    json.dumps(latency_samples, separators=(",", ":")),
                ),
            )
            state = connection.execute(
                """
                SELECT * FROM provider_health_state
                WHERE subject_id=? AND provider_kind=? AND provider_id=?
                """,
                (subject_id, provider_kind, provider_id),
            ).fetchone()
            if state is not None:
                self._verify_state(state)
            consecutive = 0 if success else (int(state["consecutive_failures"]) + 1 if state else 1)
            cooldown_until = None
            state_name = "healthy" if success else "degraded"
            if not success and consecutive >= self.failure_threshold and cooldown_seconds:
                cooldown_until = (
                    datetime.now(UTC) + timedelta(seconds=cooldown_seconds)
                ).isoformat()
                state_name = "cooldown"
            if success:
                cooldown_until = None
            # Once an in-flight request has completed, release the half-open
            # claim on either outcome. A failure gets a fresh cooldown above.
            probe_token = None
            probe_started_at = None
            state_last_success = now if success else (state["last_success_at"] if state else None)
            state_last_failure = (
                now if not success else (state["last_failure_at"] if state else None)
            )
            state_hash = self._state_hash(
                subject_id,
                provider_kind,
                provider_id,
                state_name,
                cooldown_until,
                probe_token,
                probe_started_at,
                consecutive,
                state_last_success,
                state_last_failure,
                now,
            )
            connection.execute(
                """
                INSERT INTO provider_health_state
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_id, provider_kind, provider_id)
                DO UPDATE SET state=excluded.state,
                    cooldown_until=excluded.cooldown_until,
                    probe_token=excluded.probe_token,
                    probe_started_at=excluded.probe_started_at,
                    consecutive_failures=excluded.consecutive_failures,
                    last_success_at=excluded.last_success_at,
                    last_failure_at=excluded.last_failure_at,
                    updated_at=excluded.updated_at,
                    state_hash=excluded.state_hash
                """,
                (
                    subject_id,
                    provider_kind,
                    provider_id,
                    state_name,
                    cooldown_until,
                    probe_token,
                    probe_started_at,
                    consecutive,
                    state_last_success,
                    state_last_failure,
                    now,
                    state_hash,
                ),
            )
        return True

    def list_projection(self, subject_id: str, provider_kind: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            states = connection.execute(
                """
                SELECT * FROM provider_health_state
                WHERE subject_id=? AND provider_kind=? ORDER BY provider_id
                """,
                (subject_id, provider_kind),
            ).fetchall()
            buckets = connection.execute(
                "SELECT * FROM provider_health_buckets WHERE subject_id=? AND provider_kind=?",
                (subject_id, provider_kind),
            ).fetchall()
        aggregates: dict[str, dict[str, Any]] = {}
        for bucket in buckets:
            self._verify_bucket(bucket)
            aggregate = aggregates.setdefault(
                str(bucket["provider_id"]),
                {
                    "attempts": 0,
                    "successes": 0,
                    "failures": 0,
                    "latency": 0,
                    "last_success": None,
                    "errors": {},
                    "samples": [],
                },
            )
            aggregate["attempts"] += int(bucket["attempt_count"])
            aggregate["successes"] += int(bucket["success_count"])
            aggregate["failures"] += int(bucket["failure_count"])
            aggregate["latency"] += int(bucket["latency_total_ms"])
            for category, count in json.loads(bucket["error_counts_json"] or "{}").items():
                aggregate["errors"][category] = aggregate["errors"].get(category, 0) + int(count)
            aggregate["samples"].extend(json.loads(bucket["latency_samples_json"] or "[]"))
            if bucket["last_success_at"] and (
                aggregate["last_success"] is None
                or bucket["last_success_at"] > aggregate["last_success"]
            ):
                aggregate["last_success"] = bucket["last_success_at"]
        result = []
        now = datetime.now(UTC)
        for row in states:
            self._verify_state(row)
            aggregate = aggregates.get(
                str(row["provider_id"]),
                {
                    "attempts": 0,
                    "successes": 0,
                    "failures": 0,
                    "latency": 0,
                    "last_success": None,
                    "errors": {},
                    "samples": [],
                },
            )
            attempts = int(aggregate["attempts"])
            failures = int(aggregate["failures"])
            samples = sorted(int(value) for value in aggregate.get("samples", []))

            cooldown = _parse_time(row["cooldown_until"])
            state = row["state"]
            if state == "cooldown" and cooldown and cooldown <= now and not row["probe_token"]:
                state = "half_open"
            result.append(
                {
                    "provider_id": row["provider_id"],
                    "provider_kind": row["provider_kind"],
                    "state": state,
                    "attempt_count": attempts,
                    "success_count": int(aggregate["successes"]),
                    "failure_count": failures,
                    "failure_rate": (failures / attempts) if attempts else 0.0,
                    "average_latency_ms": (int(aggregate["latency"]) / attempts) if attempts else 0,
                    "p50_latency_ms": _percentile(samples, 0.50),
                    "p95_latency_ms": _percentile(samples, 0.95),
                    "error_counts": dict(sorted(aggregate.get("errors", {}).items())),
                    "last_success_at": aggregate["last_success"] or row["last_success_at"],
                    "cooldown_until": row["cooldown_until"],
                }
            )
        return result

    def route_available(self, subject_id: str, provider_kind: str, provider_id: str) -> bool:
        """Return whether normal traffic may use a provider, claiming one recovery probe."""
        now = datetime.now(UTC)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM provider_health_state "
                "WHERE subject_id=? AND provider_kind=? AND provider_id=?",
                (subject_id, provider_kind, provider_id),
            ).fetchone()
            if row is None:
                return True
            self._verify_state(row)
            if row["state"] in {"healthy", "degraded"}:
                return True
            if row["probe_token"]:
                started = _parse_time(row["probe_started_at"])
                if started and started + timedelta(minutes=2) > now:
                    return False
            cooldown = _parse_time(row["cooldown_until"])
            if cooldown is None or cooldown > now:
                return False
            token = new_id("probe")
            timestamp = now.isoformat()
            state_hash = self._state_hash(
                subject_id,
                provider_kind,
                provider_id,
                "half_open",
                row["cooldown_until"],
                token,
                timestamp,
                int(row["consecutive_failures"]),
                row["last_success_at"],
                row["last_failure_at"],
                timestamp,
            )
            updated = connection.execute(
                """
                UPDATE provider_health_state
                SET state='half_open', probe_token=?, probe_started_at=?, updated_at=?, state_hash=?
                WHERE subject_id=? AND provider_kind=? AND provider_id=?
                    AND (probe_token IS NULL OR probe_started_at <= ?)
                """,
                (
                    token,
                    timestamp,
                    timestamp,
                    state_hash,
                    subject_id,
                    provider_kind,
                    provider_id,
                    (now - timedelta(minutes=2)).isoformat(),
                ),
            ).rowcount
            return bool(updated)
