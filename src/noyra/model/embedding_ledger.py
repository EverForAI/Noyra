from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from noyra.core.admission import accounting_scope
from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash, new_id, strict_bool, strict_int, utc_now

from .embedding import EmbeddingBudgetLimits, EmbeddingCircuitPolicy
from .errors import EmbeddingBudgetExhaustedError, EmbeddingCircuitOpenError

USAGE_STATES = frozenset({"authorized", "executing", "succeeded", "failed", "unknown", "cancelled"})
CIRCUIT_STATES = frozenset({"closed", "open", "half_open"})


@dataclass(frozen=True)
class EmbeddingUsageRecord:
    usage_id: str
    subject_id: str
    resource_id: str
    provider: str
    model: str
    purpose: str
    request_hash: str
    idempotency_key: str
    budget_day: str
    status: str
    text_count: int
    reserved_tokens: int
    input_tokens: int | None
    reserved_cost_microusd: int
    cost_microusd: int | None
    usage_estimated: bool
    provider_request_id: str | None
    error_code: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class EmbeddingBudgetStatus:
    budget_day: str
    calls: int
    input_tokens: int
    cost_microusd: int
    limits: EmbeddingBudgetLimits

    @property
    def pressure(self) -> float:
        ratios = (
            self.calls / self.limits.daily_calls if self.limits.daily_calls else 1.0,
            self.input_tokens / self.limits.daily_tokens if self.limits.daily_tokens else 1.0,
            self.cost_microusd / self.limits.daily_cost_microusd
            if self.limits.daily_cost_microusd
            else 1.0,
        )
        return min(1.0, max(ratios))


@dataclass(frozen=True)
class EmbeddingCircuitRecord:
    subject_id: str
    resource_id: str
    status: str
    consecutive_failures: int
    next_probe_at: str | None
    probe_usage_id: str | None
    last_error_code: str | None
    state_hash: str
    version: int
    updated_at: str


class EmbeddingLedger:
    """Atomic embedding budgets, usage reconciliation, and provider circuit state."""

    def __init__(self, database: Database, *, clock: Callable[[], str] = utc_now):
        self.database = database
        self.clock = clock

    def authorize(
        self,
        subject_id: str,
        resource_id: str,
        provider: str,
        model: str,
        purpose: str,
        request_hash: str,
        idempotency_key: str,
        limits: EmbeddingBudgetLimits,
        circuit_policy: EmbeddingCircuitPolicy,
        *,
        text_count: int,
        reserved_tokens: int,
        reserved_cost_microusd: int,
    ) -> tuple[EmbeddingUsageRecord, bool]:
        values = (
            subject_id,
            resource_id,
            provider,
            model,
            purpose,
            request_hash,
            idempotency_key,
        )
        if any(not value.strip() for value in values):
            raise ValueError("embedding usage metadata cannot be blank")
        if not 1 <= text_count <= 128:
            raise ValueError("embedding text count is invalid")
        if min(reserved_tokens, reserved_cost_microusd) < 0:
            raise ValueError("embedding reservations cannot be negative")
        now = self.clock()
        self._parse_time(now, "embedding authorization time")
        budget_day = now[:10]
        usage_id = new_id("embuse")
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM embedding_usage_entries "
                "WHERE subject_id = ? AND idempotency_key = ?",
                (subject_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                record = self._usage_from_row(existing)
                if (
                    record.resource_id != resource_id
                    or record.provider != provider
                    or record.model != model
                    or record.purpose != purpose
                    or record.request_hash != request_hash
                    or record.text_count != text_count
                    or record.reserved_tokens != reserved_tokens
                    or record.reserved_cost_microusd != reserved_cost_microusd
                ):
                    raise ValueError("embedding idempotency key identifies another request")
                return record, False

            circuit = self._ensure_circuit(connection, subject_id, resource_id, now)
            half_open = False
            if circuit.status == "open":
                if circuit.next_probe_at is None:
                    raise IntegrityError("open embedding circuit has no probe deadline")
                probe_at = self._parse_time(circuit.next_probe_at, "embedding probe deadline")
                if probe_at > self._parse_time(now, "embedding authorization time"):
                    raise EmbeddingCircuitOpenError("embedding circuit is open")
                half_open = True
            elif circuit.status == "half_open":
                raise EmbeddingCircuitOpenError("embedding circuit probe is already in progress")

            status = self._budget_status_connection(
                connection,
                subject_id,
                resource_id,
                limits,
                budget_day,
            )
            projected = (
                status.calls + 1,
                status.input_tokens + reserved_tokens,
                status.cost_microusd + reserved_cost_microusd,
            )
            maximum = (
                limits.daily_calls,
                limits.daily_tokens,
                limits.daily_cost_microusd,
            )
            for dimension, value, limit in zip(
                ("calls", "tokens", "cost"), projected, maximum, strict=True
            ):
                if value > limit:
                    raise EmbeddingBudgetExhaustedError(
                        f"daily embedding {dimension} budget exhausted"
                    )

            connection.execute(
                "INSERT INTO embedding_usage_entries("
                "usage_id, subject_id, resource_id, provider, model, purpose, request_hash, "
                "idempotency_key, budget_day, status, text_count, reserved_tokens, "
                "input_tokens, reserved_cost_microusd, cost_microusd, usage_estimated, "
                "provider_request_id, error_code, created_at, started_at, completed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'authorized', ?, ?, NULL, ?, NULL, "
                "1, NULL, NULL, ?, NULL, NULL)",
                (
                    usage_id,
                    subject_id,
                    resource_id,
                    provider,
                    model,
                    purpose,
                    request_hash,
                    idempotency_key,
                    budget_day,
                    text_count,
                    reserved_tokens,
                    reserved_cost_microusd,
                    now,
                ),
            )
            if half_open:
                self._set_circuit(
                    connection,
                    circuit,
                    status="half_open",
                    consecutive_failures=circuit.consecutive_failures,
                    next_probe_at=None,
                    probe_usage_id=usage_id,
                    error_code=circuit.last_error_code,
                    now=now,
                )
            return self._load_usage(connection, usage_id), True

    def start(self, usage_id: str) -> EmbeddingUsageRecord:
        now = self.clock()
        with accounting_scope(), self.database.transaction() as connection:
            row = self._usage_row(connection, usage_id)
            if row["status"] != "authorized":
                raise ValueError("embedding usage is not authorized")
            connection.execute(
                "UPDATE embedding_usage_entries SET status = 'executing', started_at = ? "
                "WHERE usage_id = ?",
                (now, usage_id),
            )
            return self._load_usage(connection, usage_id)

    def get(self, usage_id: str) -> EmbeddingUsageRecord:
        """Read one usage entry with the same strict parser used by integrity checks."""
        with self.database.connection() as connection:
            return self._load_usage(connection, usage_id)

    def succeed(
        self,
        usage_id: str,
        *,
        input_tokens: int,
        cost_microusd: int,
        usage_estimated: bool,
        provider_request_id: str | None,
    ) -> EmbeddingUsageRecord:
        if min(input_tokens, cost_microusd) < 0:
            raise ValueError("embedding usage cannot be negative")
        now = self.clock()
        with accounting_scope(), self.database.transaction() as connection:
            row = self._usage_row(connection, usage_id)
            if row["status"] != "executing":
                raise ValueError("embedding usage is not executing")
            if input_tokens > int(row["reserved_tokens"]):
                raise ValueError("embedding provider usage exceeds the hard token reservation")
            if cost_microusd > int(row["reserved_cost_microusd"]):
                raise ValueError("embedding provider cost exceeds the hard reservation")
            connection.execute(
                "UPDATE embedding_usage_entries SET status = 'succeeded', input_tokens = ?, "
                "cost_microusd = ?, usage_estimated = ?, provider_request_id = ?, "
                "completed_at = ? WHERE usage_id = ?",
                (
                    input_tokens,
                    cost_microusd,
                    int(usage_estimated),
                    provider_request_id,
                    now,
                    usage_id,
                ),
            )
            circuit = self._ensure_circuit(
                connection, str(row["subject_id"]), str(row["resource_id"]), now
            )
            if circuit.status != "closed" or circuit.consecutive_failures:
                self._set_circuit(
                    connection,
                    circuit,
                    status="closed",
                    consecutive_failures=0,
                    next_probe_at=None,
                    probe_usage_id=None,
                    error_code=None,
                    now=now,
                )
            return self._load_usage(connection, usage_id)

    def fail(
        self,
        usage_id: str,
        *,
        error_code: str,
        usage_unknown: bool,
        circuit_policy: EmbeddingCircuitPolicy,
    ) -> EmbeddingUsageRecord:
        if not error_code.strip():
            raise ValueError("embedding failure code is required")
        now = self.clock()
        with accounting_scope(), self.database.transaction() as connection:
            row = self._usage_row(connection, usage_id)
            if row["status"] != "executing":
                raise ValueError("embedding usage is not executing")
            connection.execute(
                "UPDATE embedding_usage_entries SET status = ?, input_tokens = ?, "
                "cost_microusd = ?, usage_estimated = ?, error_code = ?, completed_at = ? "
                "WHERE usage_id = ?",
                (
                    "unknown" if usage_unknown else "failed",
                    None if usage_unknown else 0,
                    None if usage_unknown else 0,
                    int(usage_unknown),
                    error_code,
                    now,
                    usage_id,
                ),
            )
            circuit = self._ensure_circuit(
                connection, str(row["subject_id"]), str(row["resource_id"]), now
            )
            failures = circuit.consecutive_failures + 1
            should_open = circuit.status == "half_open" or (
                failures >= circuit_policy.failure_threshold
            )
            if should_open:
                failures = max(failures, circuit_policy.failure_threshold)
                next_probe_at = (
                    self._parse_time(now, "embedding failure time")
                    + timedelta(seconds=circuit_policy.cooldown_seconds)
                ).isoformat(timespec="milliseconds")
                status = "open"
            else:
                next_probe_at = None
                status = "closed"
            self._set_circuit(
                connection,
                circuit,
                status=status,
                consecutive_failures=failures,
                next_probe_at=next_probe_at,
                probe_usage_id=None,
                error_code=error_code,
                now=now,
            )
            return self._load_usage(connection, usage_id)

    def budget_status(
        self,
        subject_id: str,
        resource_id: str,
        limits: EmbeddingBudgetLimits,
        *,
        budget_day: str | None = None,
    ) -> EmbeddingBudgetStatus:
        budget_day = budget_day or self.clock()[:10]
        with self.database.connection() as connection:
            return self._budget_status_connection(
                connection, subject_id, resource_id, limits, budget_day
            )

    def circuit(self, subject_id: str, resource_id: str) -> EmbeddingCircuitRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM embedding_circuit_states WHERE subject_id = ? AND resource_id = ?",
                (subject_id, resource_id),
            ).fetchone()
        if row is None:
            raise KeyError("embedding circuit state does not exist")
        return self._circuit_from_row(row)

    def recover_interrupted(
        self,
        subject_id: str,
        *,
        circuit_policy: EmbeddingCircuitPolicy | None = None,
    ) -> int:
        policy = circuit_policy or EmbeddingCircuitPolicy()
        now = self.clock()
        with self.database.transaction() as connection:
            authorized = connection.execute(
                "UPDATE embedding_usage_entries SET status = 'cancelled', "
                "error_code = 'interrupted_before_send', completed_at = ? "
                "WHERE subject_id = ? AND status = 'authorized'",
                (now, subject_id),
            ).rowcount
            abandoned_probes = connection.execute(
                "SELECT s.* FROM embedding_circuit_states s "
                "JOIN embedding_usage_entries u ON u.usage_id = s.probe_usage_id "
                "WHERE s.subject_id = ? AND s.status = 'half_open' "
                "AND u.status = 'cancelled'",
                (subject_id,),
            ).fetchall()
            for row in abandoned_probes:
                circuit = self._circuit_from_row(row)
                self._set_circuit(
                    connection,
                    circuit,
                    status="open",
                    consecutive_failures=max(
                        circuit.consecutive_failures, policy.failure_threshold
                    ),
                    next_probe_at=now,
                    probe_usage_id=None,
                    error_code="interrupted_before_send",
                    now=now,
                )
            executing = connection.execute(
                "SELECT usage_id FROM embedding_usage_entries "
                "WHERE subject_id = ? AND status = 'executing'",
                (subject_id,),
            ).fetchall()
            for row in executing:
                usage = self._usage_row(connection, str(row["usage_id"]))
                connection.execute(
                    "UPDATE embedding_usage_entries SET status = 'unknown', usage_estimated = 1, "
                    "error_code = 'interrupted_during_embedding', completed_at = ? "
                    "WHERE usage_id = ?",
                    (now, usage["usage_id"]),
                )
                circuit = self._ensure_circuit(
                    connection,
                    str(usage["subject_id"]),
                    str(usage["resource_id"]),
                    now,
                )
                failures = max(circuit.consecutive_failures + 1, policy.failure_threshold)
                next_probe_at = (
                    self._parse_time(now, "embedding recovery time")
                    + timedelta(seconds=policy.cooldown_seconds)
                ).isoformat(timespec="milliseconds")
                self._set_circuit(
                    connection,
                    circuit,
                    status="open",
                    consecutive_failures=failures,
                    next_probe_at=next_probe_at,
                    probe_usage_id=None,
                    error_code="interrupted_during_embedding",
                    now=now,
                )
            return int(authorized) + len(executing)

    def verify_integrity(self, subject_id: str, *, connection: Any | None = None) -> dict[str, int]:
        if connection is None:
            with self.database.connection() as owned_connection:
                return self._verify_integrity_connection(owned_connection, subject_id)
        return self._verify_integrity_connection(connection, subject_id)

    def _verify_integrity_connection(self, connection: Any, subject_id: str) -> dict[str, int]:
        usage_rows = connection.execute(
            "SELECT u.*, r.subject_id AS configured_resource_subject_id "
            "FROM embedding_usage_entries u "
            "LEFT JOIN embedding_resources r ON r.config_id = u.resource_id "
            "WHERE u.subject_id = ? OR r.subject_id = ? ORDER BY u.created_at, u.usage_id",
            (subject_id, subject_id),
        ).fetchall()
        state_rows = connection.execute(
            "SELECT s.*, r.subject_id AS configured_resource_subject_id "
            "FROM embedding_circuit_states s "
            "LEFT JOIN embedding_resources r ON r.config_id = s.resource_id "
            "LEFT JOIN embedding_usage_entries u ON u.usage_id = s.probe_usage_id "
            "WHERE s.subject_id = ? OR r.subject_id = ? OR u.subject_id = ? "
            "ORDER BY s.resource_id",
            (subject_id, subject_id, subject_id),
        ).fetchall()
        transition_rows = connection.execute(
            "SELECT t.*, r.subject_id AS configured_resource_subject_id "
            "FROM embedding_circuit_transitions t "
            "LEFT JOIN embedding_resources r ON r.config_id = t.resource_id "
            "LEFT JOIN embedding_usage_entries u ON u.usage_id = t.probe_usage_id "
            "WHERE t.subject_id = ? OR r.subject_id = ? OR u.subject_id = ? "
            "ORDER BY t.resource_id, t.version",
            (subject_id, subject_id, subject_id),
        ).fetchall()

        usage_ids: set[str] = set()
        usage_records: dict[str, EmbeddingUsageRecord] = {}
        for row in usage_rows:
            record = self._usage_from_row(row)
            if record.subject_id != subject_id or row["configured_resource_subject_id"] not in {
                None,
                subject_id,
            }:
                raise IntegrityError(f"embedding usage ownership mismatch: {record.usage_id}")
            if record.usage_id in usage_ids:
                raise IntegrityError("duplicate embedding usage identifier")
            usage_ids.add(record.usage_id)
            usage_records[record.usage_id] = record
            self._verify_usage_lifecycle(record)

        transitions_by_resource: dict[str, list[Any]] = {}
        for row in transition_rows:
            if row["subject_id"] != subject_id or row["configured_resource_subject_id"] not in {
                None,
                subject_id,
            }:
                raise IntegrityError(
                    f"embedding circuit transition ownership mismatch: {row['transition_id']}"
                )
            transitions_by_resource.setdefault(str(row["resource_id"]), []).append(row)
        for row in state_rows:
            state = self._circuit_from_row(row)
            if state.subject_id != subject_id or row["configured_resource_subject_id"] not in {
                None,
                subject_id,
            }:
                raise IntegrityError(f"embedding circuit ownership mismatch: {state.resource_id}")
            transitions = transitions_by_resource.pop(state.resource_id, [])
            if not transitions or len(transitions) != state.version:
                raise IntegrityError(
                    f"embedding circuit history is incomplete: {state.resource_id}"
                )
            previous_status = "closed"
            for expected_version, transition in enumerate(transitions, start=1):
                self._verify_circuit_transition(
                    transition,
                    subject_id,
                    state.resource_id,
                    expected_version,
                    previous_status,
                    usage_records,
                )
                previous_status = str(transition["new_status"])
            latest = transitions[-1]
            fields = (
                ("new_status", state.status),
                ("consecutive_failures", state.consecutive_failures),
                ("next_probe_at", state.next_probe_at),
                ("probe_usage_id", state.probe_usage_id),
                ("error_code", state.last_error_code),
                ("state_hash", state.state_hash),
                ("version", state.version),
                ("created_at", state.updated_at),
            )
            if any(latest[column] != value for column, value in fields):
                raise IntegrityError(f"embedding circuit history mismatch: {state.resource_id}")
        if transitions_by_resource:
            raise IntegrityError("embedding circuit history has no current state")
        return {
            "embedding_usage_entries": len(usage_rows),
            "embedding_circuit_states": len(state_rows),
            "embedding_circuit_transitions": len(transition_rows),
        }

    @staticmethod
    def _budget_status_connection(
        connection: Any,
        subject_id: str,
        resource_id: str,
        limits: EmbeddingBudgetLimits,
        budget_day: str,
    ) -> EmbeddingBudgetStatus:
        row = connection.execute(
            "SELECT COUNT(*) AS calls, "
            "COALESCE(SUM(COALESCE(input_tokens, reserved_tokens)), 0) AS input_tokens, "
            "COALESCE(SUM(COALESCE(cost_microusd, reserved_cost_microusd)), 0) "
            "AS cost_microusd FROM embedding_usage_entries "
            "WHERE subject_id = ? AND resource_id = ? AND budget_day = ? "
            "AND status != 'cancelled'",
            (subject_id, resource_id, budget_day),
        ).fetchone()
        return EmbeddingBudgetStatus(
            budget_day,
            int(row["calls"]),
            int(row["input_tokens"]),
            int(row["cost_microusd"]),
            limits,
        )

    def _ensure_circuit(
        self, connection: Any, subject_id: str, resource_id: str, now: str
    ) -> EmbeddingCircuitRecord:
        row = connection.execute(
            "SELECT * FROM embedding_circuit_states WHERE subject_id = ? AND resource_id = ?",
            (subject_id, resource_id),
        ).fetchone()
        if row is not None:
            return self._circuit_from_row(row)
        version = 1
        state_hash = self._circuit_hash(
            subject_id,
            resource_id,
            "closed",
            0,
            None,
            None,
            None,
            version,
            now,
        )
        connection.execute(
            "INSERT INTO embedding_circuit_states("
            "subject_id, resource_id, status, consecutive_failures, next_probe_at, "
            "probe_usage_id, last_error_code, state_hash, version, updated_at"
            ") VALUES (?, ?, 'closed', 0, NULL, NULL, NULL, ?, ?, ?)",
            (subject_id, resource_id, state_hash, version, now),
        )
        connection.execute(
            "INSERT INTO embedding_circuit_transitions("
            "transition_id, subject_id, resource_id, old_status, new_status, "
            "consecutive_failures, next_probe_at, probe_usage_id, error_code, state_hash, "
            "version, created_at) VALUES (?, ?, ?, 'closed', 'closed', 0, NULL, NULL, "
            "NULL, ?, ?, ?)",
            (new_id("embcirc"), subject_id, resource_id, state_hash, version, now),
        )
        return self._circuit_from_row(
            connection.execute(
                "SELECT * FROM embedding_circuit_states WHERE subject_id = ? AND resource_id = ?",
                (subject_id, resource_id),
            ).fetchone()
        )

    def _set_circuit(
        self,
        connection: Any,
        current: EmbeddingCircuitRecord,
        *,
        status: str,
        consecutive_failures: int,
        next_probe_at: str | None,
        probe_usage_id: str | None,
        error_code: str | None,
        now: str,
    ) -> EmbeddingCircuitRecord:
        if status not in CIRCUIT_STATES or consecutive_failures < 0:
            raise ValueError("embedding circuit state is invalid")
        version = current.version + 1
        state_hash = self._circuit_hash(
            current.subject_id,
            current.resource_id,
            status,
            consecutive_failures,
            next_probe_at,
            probe_usage_id,
            error_code,
            version,
            now,
        )
        connection.execute(
            "UPDATE embedding_circuit_states SET status = ?, consecutive_failures = ?, "
            "next_probe_at = ?, probe_usage_id = ?, last_error_code = ?, state_hash = ?, "
            "version = ?, updated_at = ? WHERE subject_id = ? AND resource_id = ?",
            (
                status,
                consecutive_failures,
                next_probe_at,
                probe_usage_id,
                error_code,
                state_hash,
                version,
                now,
                current.subject_id,
                current.resource_id,
            ),
        )
        connection.execute(
            "INSERT INTO embedding_circuit_transitions("
            "transition_id, subject_id, resource_id, old_status, new_status, "
            "consecutive_failures, next_probe_at, probe_usage_id, error_code, state_hash, "
            "version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("embcirc"),
                current.subject_id,
                current.resource_id,
                current.status,
                status,
                consecutive_failures,
                next_probe_at,
                probe_usage_id,
                error_code,
                state_hash,
                version,
                now,
            ),
        )
        return self._circuit_from_row(
            connection.execute(
                "SELECT * FROM embedding_circuit_states WHERE subject_id = ? AND resource_id = ?",
                (current.subject_id, current.resource_id),
            ).fetchone()
        )

    @staticmethod
    def _circuit_hash(
        subject_id: str,
        resource_id: str,
        status: str,
        consecutive_failures: int,
        next_probe_at: str | None,
        probe_usage_id: str | None,
        error_code: str | None,
        version: int,
        updated_at: str,
    ) -> str:
        return content_hash(
            {
                "subject_id": subject_id,
                "resource_id": resource_id,
                "status": status,
                "consecutive_failures": consecutive_failures,
                "next_probe_at": next_probe_at,
                "probe_usage_id": probe_usage_id,
                "last_error_code": error_code,
                "version": version,
                "updated_at": updated_at,
            }
        )

    @classmethod
    def _usage_from_row(cls, row: Any) -> EmbeddingUsageRecord:
        status = row["status"]
        if status not in USAGE_STATES:
            raise IntegrityError("embedding usage status is invalid")
        required = (
            "usage_id",
            "subject_id",
            "resource_id",
            "provider",
            "model",
            "purpose",
            "request_hash",
            "idempotency_key",
            "budget_day",
            "created_at",
        )
        if any(not isinstance(row[field], str) or not row[field].strip() for field in required):
            raise IntegrityError("embedding usage text state is invalid")
        try:
            text_count = strict_int(row["text_count"])
            reserved_tokens = strict_int(row["reserved_tokens"])
            reserved_cost = strict_int(row["reserved_cost_microusd"])
            input_tokens = None if row["input_tokens"] is None else strict_int(row["input_tokens"])
            cost = None if row["cost_microusd"] is None else strict_int(row["cost_microusd"])
            estimated = strict_bool(row["usage_estimated"])
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError("embedding usage numeric state is invalid") from error
        if (
            not 1 <= text_count <= 128
            or min(reserved_tokens, reserved_cost) < 0
            or (input_tokens is not None and input_tokens < 0)
            or (cost is not None and cost < 0)
        ):
            raise IntegrityError("embedding usage numeric state is invalid")
        for field in ("provider_request_id", "error_code", "started_at", "completed_at"):
            value = row[field]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise IntegrityError("embedding usage optional text state is invalid")
        return EmbeddingUsageRecord(
            row["usage_id"],
            row["subject_id"],
            row["resource_id"],
            row["provider"],
            row["model"],
            row["purpose"],
            row["request_hash"],
            row["idempotency_key"],
            row["budget_day"],
            status,
            text_count,
            reserved_tokens,
            input_tokens,
            reserved_cost,
            cost,
            estimated,
            row["provider_request_id"],
            row["error_code"],
            row["created_at"],
            row["started_at"],
            row["completed_at"],
        )

    @classmethod
    def _circuit_from_row(cls, row: Any) -> EmbeddingCircuitRecord:
        status = row["status"]
        if status not in CIRCUIT_STATES:
            raise IntegrityError("embedding circuit status is invalid")
        required = ("subject_id", "resource_id", "state_hash", "updated_at")
        if any(not isinstance(row[field], str) or not row[field].strip() for field in required):
            raise IntegrityError("embedding circuit text state is invalid")
        try:
            failures = strict_int(row["consecutive_failures"])
            version = strict_int(row["version"])
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError("embedding circuit numeric state is invalid") from error
        if failures < 0 or version < 1:
            raise IntegrityError("embedding circuit numeric state is invalid")
        for field in ("next_probe_at", "probe_usage_id", "last_error_code"):
            value = row[field]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise IntegrityError("embedding circuit optional text is invalid")
        cls._parse_time(str(row["updated_at"]), "embedding circuit update time")
        if row["next_probe_at"] is not None:
            cls._parse_time(str(row["next_probe_at"]), "embedding circuit probe deadline")
        if status == "open" and row["next_probe_at"] is None:
            raise IntegrityError("open embedding circuit has no probe deadline")
        if status == "half_open" and row["probe_usage_id"] is None:
            raise IntegrityError("half-open embedding circuit has no probe usage")
        if status == "closed" and (
            row["next_probe_at"] is not None or row["probe_usage_id"] is not None
        ):
            raise IntegrityError("closed embedding circuit retains probe state")
        expected_hash = cls._circuit_hash(
            str(row["subject_id"]),
            str(row["resource_id"]),
            str(status),
            failures,
            row["next_probe_at"],
            row["probe_usage_id"],
            row["last_error_code"],
            version,
            str(row["updated_at"]),
        )
        if expected_hash != row["state_hash"]:
            raise IntegrityError("embedding circuit state hash mismatch")
        return EmbeddingCircuitRecord(
            row["subject_id"],
            row["resource_id"],
            status,
            failures,
            row["next_probe_at"],
            row["probe_usage_id"],
            row["last_error_code"],
            row["state_hash"],
            version,
            row["updated_at"],
        )

    @classmethod
    def _verify_usage_lifecycle(cls, record: EmbeddingUsageRecord) -> None:
        created_at = cls._parse_time(record.created_at, "embedding usage creation time")
        if record.budget_day != record.created_at[:10]:
            raise IntegrityError(f"embedding usage budget day is invalid: {record.usage_id}")
        if len(record.request_hash) != 64 or any(
            character not in "0123456789abcdef" for character in record.request_hash
        ):
            raise IntegrityError(f"embedding request hash is invalid: {record.usage_id}")
        started_at = None
        if record.started_at is not None:
            started_at = cls._parse_time(record.started_at, "embedding usage start time")
            if started_at < created_at:
                raise IntegrityError(f"embedding usage time order is invalid: {record.usage_id}")
        if record.completed_at is not None:
            completed_at = cls._parse_time(record.completed_at, "embedding usage completion time")
            if completed_at < (started_at or created_at):
                raise IntegrityError(f"embedding usage time order is invalid: {record.usage_id}")
        if record.status == "authorized":
            valid = (
                record.started_at is None
                and record.completed_at is None
                and record.input_tokens is None
                and record.cost_microusd is None
                and record.usage_estimated
                and record.provider_request_id is None
                and record.error_code is None
            )
        elif record.status == "executing":
            valid = (
                record.started_at is not None
                and record.completed_at is None
                and record.input_tokens is None
                and record.cost_microusd is None
                and record.usage_estimated
                and record.provider_request_id is None
                and record.error_code is None
            )
        elif record.status == "succeeded":
            valid = (
                record.started_at is not None
                and record.completed_at is not None
                and record.input_tokens is not None
                and record.cost_microusd is not None
                and record.input_tokens <= record.reserved_tokens
                and record.cost_microusd <= record.reserved_cost_microusd
                and record.error_code is None
            )
        elif record.status == "failed":
            valid = (
                record.started_at is not None
                and record.completed_at is not None
                and record.input_tokens == 0
                and record.cost_microusd == 0
                and not record.usage_estimated
                and record.provider_request_id is None
                and record.error_code is not None
            )
        elif record.status == "unknown":
            valid = (
                record.started_at is not None
                and record.completed_at is not None
                and record.input_tokens is None
                and record.cost_microusd is None
                and record.usage_estimated
                and record.provider_request_id is None
                and record.error_code is not None
            )
        else:
            valid = (
                record.started_at is None
                and record.completed_at is not None
                and record.input_tokens is None
                and record.cost_microusd is None
                and record.usage_estimated
                and record.provider_request_id is None
                and record.error_code == "interrupted_before_send"
            )
        if not valid:
            raise IntegrityError(f"embedding usage lifecycle is invalid: {record.usage_id}")

    @classmethod
    def _verify_circuit_transition(
        cls,
        row: Any,
        subject_id: str,
        resource_id: str,
        expected_version: int,
        previous_status: str,
        usage_records: dict[str, EmbeddingUsageRecord],
    ) -> None:
        try:
            version = strict_int(row["version"])
            failures = strict_int(row["consecutive_failures"])
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError("embedding circuit transition numeric state is invalid") from error
        if (
            not isinstance(row["transition_id"], str)
            or not row["transition_id"].strip()
            or row["subject_id"] != subject_id
            or row["resource_id"] != resource_id
            or version != expected_version
            or row["old_status"] != previous_status
            or row["new_status"] not in CIRCUIT_STATES
            or failures < 0
        ):
            raise IntegrityError(f"embedding circuit transition is invalid: {resource_id}")
        cls._parse_time(str(row["created_at"]), "embedding circuit transition time")
        probe_usage_id = row["probe_usage_id"]
        if probe_usage_id is not None:
            usage = usage_records.get(str(probe_usage_id))
            if usage is None or usage.resource_id != resource_id:
                raise IntegrityError(f"embedding circuit probe usage is missing: {resource_id}")
        new_status = str(row["new_status"])
        if new_status == "open" and row["next_probe_at"] is None:
            raise IntegrityError(f"open embedding circuit transition is invalid: {resource_id}")
        if row["next_probe_at"] is not None:
            cls._parse_time(
                str(row["next_probe_at"]), "embedding circuit transition probe deadline"
            )
        if new_status == "half_open" and probe_usage_id is None:
            raise IntegrityError(
                f"half-open embedding circuit transition is invalid: {resource_id}"
            )
        if new_status == "closed" and (
            row["next_probe_at"] is not None or probe_usage_id is not None
        ):
            raise IntegrityError(f"closed embedding circuit transition is invalid: {resource_id}")
        expected_hash = cls._circuit_hash(
            subject_id,
            resource_id,
            str(row["new_status"]),
            failures,
            row["next_probe_at"],
            probe_usage_id,
            row["error_code"],
            version,
            str(row["created_at"]),
        )
        if expected_hash != row["state_hash"]:
            raise IntegrityError(f"embedding circuit transition hash mismatch: {resource_id}")

    @classmethod
    def _load_usage(cls, connection: Any, usage_id: str) -> EmbeddingUsageRecord:
        return cls._usage_from_row(cls._usage_row(connection, usage_id))

    @staticmethod
    def _usage_row(connection: Any, usage_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM embedding_usage_entries WHERE usage_id = ?", (usage_id,)
        ).fetchone()
        if row is None:
            raise KeyError("embedding usage entry does not exist")
        return row

    @staticmethod
    def _parse_time(value: str, context: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"{context} is invalid") from error
        if parsed.tzinfo is None:
            raise IntegrityError(f"{context} is invalid")
        return parsed
