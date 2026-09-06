from __future__ import annotations

from typing import Any

from noyra.core.admission import accounting_scope
from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.payload_codec import compress_text, decompress_text
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_bool,
    strict_int,
    strict_json_loads,
    utc_now,
)

from .errors import BudgetExhaustedError, ModelCallConflictError, ModelCallStateError
from .types import (
    AttemptRecord,
    BudgetLimits,
    BudgetStatus,
    CallRecord,
    ModelUsage,
)

CALL_STATES = frozenset({"prepared", "executing", "succeeded", "failed", "unknown"})
ATTEMPT_STATES = frozenset(
    {"authorized", "executing", "succeeded", "failed", "unknown", "cancelled"}
)


class ModelLedger:
    """Persistent model-call accounting and hard budget authorization."""

    def __init__(self, database: Database):
        self.database = database

    def prepare_call(
        self,
        subject_id: str,
        provider: str,
        model: str,
        purpose: str,
        request_hash: str,
        idempotency_key: str,
        *,
        resource_pool: str = "deep",
        resource_group_id: str | None = None,
        request: dict[str, Any] | None = None,
        enforce_training_policy: bool = False,
    ) -> tuple[CallRecord, bool]:
        values = (provider, model, purpose, request_hash, idempotency_key)
        if any(not value.strip() for value in values):
            raise ValueError("model call metadata cannot be blank")
        if resource_pool not in {"economy", "deep"}:
            raise ValueError("model resource pool is invalid")
        with self.database.transaction() as connection:
            request_json = canonical_json(request) if request is not None else None
            capture_policy_version: int | None = None
            if enforce_training_policy:
                policy = connection.execute(
                    "SELECT include_model_io, policy_version FROM training_policies "
                    "WHERE subject_id = ?",
                    (subject_id,),
                ).fetchone()
                if policy is None or not bool(policy["include_model_io"]):
                    request_json = None
                if policy is not None:
                    capture_policy_version = int(policy["policy_version"])
            existing = connection.execute(
                "SELECT * FROM model_calls WHERE subject_id = ? AND idempotency_key = ?",
                (subject_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["provider"] != provider
                    or existing["model"] != model
                    or existing["purpose"] != purpose
                    or existing["request_hash"] != request_hash
                    or existing["resource_pool"] != resource_pool
                    or existing["resource_group_id"] != resource_group_id
                ):
                    raise ModelCallConflictError(
                        "idempotency key already identifies a different model call"
                    )
                return self._call_from_row(existing), False
            call_id = new_id("mcall")
            connection.execute(
                """INSERT INTO model_calls(
                    call_id, subject_id, provider, model, purpose, request_hash,
                    idempotency_key, status, response_json, response_hash,
                    usage_estimated, error_code, created_at, completed_at, resource_pool,
                    resource_group_id, request_json, capture_policy_version
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, NULL, 0, NULL, ?, NULL, ?, ?, ?, ?
                )""",
                (
                    call_id,
                    subject_id,
                    provider,
                    model,
                    purpose,
                    request_hash,
                    idempotency_key,
                    utc_now(),
                    resource_pool,
                    resource_group_id,
                    request_json,
                    capture_policy_version,
                ),
            )
            return self._load_call_connection(connection, call_id), True

    def authorize_attempt(
        self,
        call_id: str,
        limits: BudgetLimits,
        *,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        reserved_cost_microusd: int,
        budget_day: str | None = None,
        resource_group_id: str | None = None,
        pool_limits: BudgetLimits | None = None,
        continuation: bool = False,
    ) -> AttemptRecord:
        if min(reserved_input_tokens, reserved_output_tokens, reserved_cost_microusd) < 0:
            raise ValueError("model attempt reservations cannot be negative")
        budget_day = budget_day or utc_now()[:10]
        with self.database.transaction() as connection:
            call = self._get_call_row(connection, call_id)
            if call["status"] == "executing" and continuation:
                previous = connection.execute(
                    "SELECT status FROM model_attempts WHERE call_id = ? "
                    "ORDER BY attempt_number DESC LIMIT 1",
                    (call_id,),
                ).fetchone()
                if previous is None or previous["status"] in {"authorized", "executing"}:
                    raise ModelCallStateError("cannot continue a model call with an active attempt")
            elif call["status"] != "prepared":
                raise ModelCallStateError(
                    f"cannot authorize attempt for call in state {call['status']}"
                )
            group_id = call["resource_group_id"]
            if resource_group_id is not None and resource_group_id != group_id:
                raise ModelCallStateError("model attempt resource group does not match its call")
            status = self._budget_status_connection(
                connection,
                call["subject_id"],
                limits,
                budget_day,
                resource_pool=str(call["resource_pool"]),
                resource_group_id=group_id,
            )
            projected = (
                status.attempts + 1,
                status.input_tokens + reserved_input_tokens,
                status.output_tokens + reserved_output_tokens,
                status.cost_microusd + reserved_cost_microusd,
            )
            maximum = (
                limits.daily_attempts,
                limits.daily_input_tokens,
                limits.daily_output_tokens,
                limits.daily_cost_microusd,
            )
            dimensions = ("attempts", "input_tokens", "output_tokens", "cost")
            for name, value, limit in zip(dimensions, projected, maximum, strict=True):
                if value > limit:
                    raise BudgetExhaustedError(f"daily model {name} budget exhausted")

            # The per-pool aggregate reservation must be checked while this
            # transaction still holds SQLite's BEGIN IMMEDIATE writer lock.  A
            # separate gateway pre-check would allow concurrent groups to both
            # observe the same remaining pool capacity and overrun the limit.
            if pool_limits is not None:
                pool_status = self._budget_status_connection(
                    connection,
                    call["subject_id"],
                    pool_limits,
                    budget_day,
                    resource_pool=str(call["resource_pool"]),
                )
                pool_projected = (
                    pool_status.attempts + 1,
                    pool_status.input_tokens + reserved_input_tokens,
                    pool_status.output_tokens + reserved_output_tokens,
                    pool_status.cost_microusd + reserved_cost_microusd,
                )
                pool_maximum = (
                    pool_limits.daily_attempts,
                    pool_limits.daily_input_tokens,
                    pool_limits.daily_output_tokens,
                    pool_limits.daily_cost_microusd,
                )
                if any(
                    value > limit for value, limit in zip(pool_projected, pool_maximum, strict=True)
                ):
                    raise BudgetExhaustedError("daily model pool budget exhausted")

            attempt_number = (
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM model_attempts WHERE call_id = ?", (call_id,)
                    ).fetchone()[0]
                )
                + 1
            )
            attempt_id = new_id("matt")
            connection.execute(
                """INSERT INTO model_attempts(
                    attempt_id, call_id, subject_id, budget_day, attempt_number, status,
                    reserved_input_tokens, reserved_output_tokens, reserved_cost_microusd,
                    input_tokens, output_tokens, cost_microusd, provider_request_id,
                    error_code, started_at, completed_at
                ) VALUES (
                    ?, ?, ?, ?, ?, 'authorized', ?, ?, ?,
                    NULL, NULL, NULL, NULL, NULL, NULL, NULL
                )""",
                (
                    attempt_id,
                    call_id,
                    call["subject_id"],
                    budget_day,
                    attempt_number,
                    reserved_input_tokens,
                    reserved_output_tokens,
                    reserved_cost_microusd,
                ),
            )
            connection.execute(
                "UPDATE model_calls SET status = 'executing', error_code = NULL WHERE call_id = ?",
                (call_id,),
            )
            return self._load_attempt_connection(connection, attempt_id)

    def start_attempt(self, attempt_id: str) -> AttemptRecord:
        with self.database.transaction() as connection:
            row = self._get_attempt_row(connection, attempt_id)
            if row["status"] != "authorized":
                raise ModelCallStateError(f"cannot start model attempt in state {row['status']}")
            connection.execute(
                "UPDATE model_attempts SET status = 'executing', started_at = ? "
                "WHERE attempt_id = ?",
                (utc_now(), attempt_id),
            )
            return self._load_attempt_connection(connection, attempt_id)

    def finish_attempt(
        self,
        attempt_id: str,
        status: str,
        *,
        usage: ModelUsage | None,
        cost_microusd: int | None,
        provider_request_id: str | None = None,
        error_code: str | None = None,
    ) -> AttemptRecord:
        if status not in {"succeeded", "failed", "unknown"}:
            raise ValueError(f"invalid model attempt completion status: {status}")
        if cost_microusd is not None and cost_microusd < 0:
            raise ValueError("model attempt cost cannot be negative")
        if (usage is None) != (cost_microusd is None):
            raise ValueError("usage and cost must both be known or both be unknown")
        with accounting_scope(), self.database.transaction() as connection:
            row = self._get_attempt_row(connection, attempt_id)
            if row["status"] != "executing":
                raise ModelCallStateError(f"cannot finish model attempt in state {row['status']}")
            connection.execute(
                """UPDATE model_attempts SET
                    status = ?, input_tokens = ?, output_tokens = ?, cost_microusd = ?,
                    provider_request_id = ?, error_code = ?, completed_at = ?
                    WHERE attempt_id = ?""",
                (
                    status,
                    usage.input_tokens if usage else None,
                    usage.output_tokens if usage else None,
                    cost_microusd,
                    provider_request_id,
                    error_code,
                    utc_now(),
                    attempt_id,
                ),
            )
            return self._load_attempt_connection(connection, attempt_id)

    def finish_call(
        self,
        call_id: str,
        status: str,
        *,
        response: dict[str, Any] | None = None,
        usage_estimated: bool = False,
        error_code: str | None = None,
    ) -> CallRecord:
        if status not in {"succeeded", "failed", "unknown"}:
            raise ValueError(f"invalid model call completion status: {status}")
        if status == "succeeded" and response is None:
            raise ValueError("successful model call requires a response")
        with accounting_scope(), self.database.transaction() as connection:
            row = self._get_call_row(connection, call_id)
            allowed = {"executing"}
            if status == "failed":
                allowed.add("prepared")
            if row["status"] not in allowed:
                raise ModelCallStateError(f"cannot finish model call in state {row['status']}")
            response_json = canonical_json(response) if response is not None else None
            response_hash = content_hash(response) if response is not None else None
            connection.execute(
                """UPDATE model_calls SET
                    status = ?, response_json = ?, response_hash = ?, usage_estimated = ?,
                    error_code = ?, completed_at = ? WHERE call_id = ?""",
                (
                    status,
                    response_json,
                    response_hash,
                    int(usage_estimated),
                    error_code,
                    utc_now(),
                    call_id,
                ),
            )
            return self._load_call_connection(connection, call_id)

    def get_call(self, call_id: str) -> CallRecord:
        with self.database.connection() as connection:
            row = self._get_call_row(connection, call_id)
        return self._call_from_row(row)

    def attempts(self, call_id: str) -> list[AttemptRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM model_attempts WHERE call_id = ? ORDER BY attempt_number",
                (call_id,),
            ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    def budget_status(
        self,
        subject_id: str,
        limits: BudgetLimits,
        *,
        budget_day: str | None = None,
        resource_pool: str | None = None,
        resource_group_id: str | None = None,
    ) -> BudgetStatus:
        budget_day = budget_day or utc_now()[:10]
        with self.database.connection() as connection:
            return self._budget_status_connection(
                connection,
                subject_id,
                limits,
                budget_day,
                resource_pool=resource_pool,
                resource_group_id=resource_group_id,
            )

    def recover_interrupted(self, subject_id: str) -> list[CallRecord]:
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE model_attempts SET
                    status = 'cancelled', error_code = 'interrupted_before_send', completed_at = ?
                    WHERE subject_id = ? AND status = 'authorized'""",
                (now, subject_id),
            )
            connection.execute(
                """UPDATE model_attempts SET
                    status = 'unknown', error_code = 'interrupted_during_request', completed_at = ?
                    WHERE subject_id = ? AND status = 'executing'""",
                (now, subject_id),
            )
            rows = connection.execute(
                "SELECT * FROM model_calls WHERE subject_id = ? "
                "AND status IN ('prepared', 'executing')",
                (subject_id,),
            ).fetchall()
            recovered: list[CallRecord] = []
            for row in rows:
                ambiguous_attempts = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM model_attempts WHERE call_id = ? "
                        "AND status IN ('succeeded', 'unknown')",
                        (row["call_id"],),
                    ).fetchone()[0]
                )
                status = "unknown" if ambiguous_attempts else "failed"
                error_code = (
                    "interrupted_model_call"
                    if status == "unknown"
                    else "interrupted_before_attempt"
                )
                connection.execute(
                    "UPDATE model_calls SET status = ?, error_code = ?, completed_at = ? "
                    "WHERE call_id = ?",
                    (status, error_code, now, row["call_id"]),
                )
                recovered.append(self._load_call_connection(connection, row["call_id"]))
            return recovered

    def unknown_calls(self, subject_id: str, *, limit: int = 100) -> list[CallRecord]:
        bounded = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM model_calls WHERE subject_id = ? AND status = 'unknown' "
                "ORDER BY created_at, call_id LIMIT ?",
                (subject_id, bounded),
            ).fetchall()
        return [self._call_from_row(row) for row in rows]

    def compress_cold_payloads(
        self, subject_id: str, *, older_than_days: int = 90, limit: int = 500
    ) -> int:
        """Compress old terminal request/response JSON without changing hashes."""
        from datetime import UTC, datetime, timedelta

        if older_than_days < 1:
            raise ValueError("model payload retention must be at least one day")
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(
            timespec="milliseconds"
        )
        bounded = max(1, min(limit, 2_000))
        changed = 0
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT call_id, request_json, response_json FROM model_calls "
                "WHERE subject_id = ? AND status IN ('succeeded', 'failed', 'unknown') "
                "AND created_at < ? AND (request_json IS NOT NULL OR response_json IS NOT NULL) "
                "ORDER BY created_at, call_id LIMIT ?",
                (subject_id, cutoff, bounded),
            ).fetchall()
            for row in rows:
                request_json = (
                    compress_text(str(row["request_json"])) if row["request_json"] else None
                )
                response_json = (
                    compress_text(str(row["response_json"])) if row["response_json"] else None
                )
                if request_json == row["request_json"] and response_json == row["response_json"]:
                    continue
                connection.execute(
                    "UPDATE model_calls SET request_json = ?, response_json = ? WHERE call_id = ?",
                    (request_json, response_json, row["call_id"]),
                )
                changed += 1
        return changed

    def prepare_unknown_retry(self, call_id: str, *, actor: str, reason: str) -> CallRecord:
        """Explicitly authorize one retry of an outcome-unknown model call.

        Unknown calls never retry automatically.  The caller must record a
        concrete actor and rationale so duplicate-provider risk is visible.
        """
        if not actor.strip() or actor.strip().casefold() == "subject" or not reason.strip():
            raise ValueError("unknown retry requires actor and reason")
        now = utc_now()
        with self.database.transaction() as connection:
            row = self._get_call_row(connection, call_id)
            if row["status"] != "unknown":
                raise ModelCallStateError("only unknown model calls can be retried")
            connection.execute(
                "UPDATE model_calls SET status = 'prepared', error_code = NULL, "
                "completed_at = NULL WHERE call_id = ?",
                (call_id,),
            )
            connection.execute(
                "INSERT INTO audit_records(audit_id, subject_id, action, actor, payload_json, "
                "occurred_at) VALUES (?, ?, 'model_unknown_retry_authorized', ?, ?, ?)",
                (
                    new_id("audit"),
                    row["subject_id"],
                    actor,
                    canonical_json({"call_id": call_id, "reason": reason}),
                    now,
                ),
            )
            return self._load_call_connection(connection, call_id)

    def reconcile_unknown(
        self,
        call_id: str,
        *,
        outcome: str,
        actor: str,
        reason: str,
        response: dict[str, Any] | None = None,
    ) -> CallRecord:
        if outcome not in {"succeeded", "failed"}:
            raise ValueError("unknown reconciliation outcome must be succeeded or failed")
        if not actor.strip() or not reason.strip():
            raise ValueError("unknown reconciliation requires actor and reason")
        if outcome == "succeeded" and response is None:
            raise ValueError("successful reconciliation requires provider response evidence")
        now = utc_now()
        with self.database.transaction() as connection:
            row = self._get_call_row(connection, call_id)
            if row["status"] != "unknown":
                raise ModelCallStateError("only unknown model calls can be reconciled")
            response_json = canonical_json(response) if response is not None else None
            response_hash = content_hash(response) if response is not None else None
            connection.execute(
                "UPDATE model_calls SET status = ?, response_json = ?, response_hash = ?, "
                "error_code = ?, completed_at = ? WHERE call_id = ?",
                (
                    outcome,
                    response_json,
                    response_hash,
                    None if outcome == "succeeded" else "operator_reconciled_failed",
                    now,
                    call_id,
                ),
            )
            connection.execute(
                "INSERT INTO audit_records(audit_id, subject_id, action, actor, payload_json, "
                "occurred_at) VALUES (?, ?, 'model_unknown_reconciled', ?, ?, ?)",
                (
                    new_id("audit"),
                    row["subject_id"],
                    actor,
                    canonical_json({"call_id": call_id, "outcome": outcome, "reason": reason}),
                    now,
                ),
            )
            return self._load_call_connection(connection, call_id)

    def reconcile_prepared_failed(
        self,
        call_id: str,
        *,
        actor: str,
        reason: str,
    ) -> CallRecord:
        """Fail a prepared call and consume any one-shot unknown retry grant.

        An operator may cancel a retry after authorizing it but before the
        provider request starts.  The terminal call transition and the
        append-only cancellation evidence must be one transaction; otherwise a
        crash between them would leave a stale authorization that permanently
        quarantines the logical request.
        """
        if not actor.strip() or actor.strip().casefold() == "subject" or not reason.strip():
            raise ValueError("prepared call reconciliation requires actor and reason")
        now = utc_now()
        with self.database.transaction() as connection:
            row = self._get_call_row(connection, call_id)
            if row["status"] != "prepared":
                raise ModelCallStateError("only prepared model calls can be failed")
            authorized = connection.execute(
                "SELECT 1 FROM audit_records WHERE subject_id = ? "
                "AND action = 'model_unknown_retry_authorized' AND json_valid(payload_json) "
                "AND json_extract(payload_json, '$.call_id') = ? LIMIT 1",
                (row["subject_id"], call_id),
            ).fetchone()
            connection.execute(
                "UPDATE model_calls SET status = 'failed', error_code = ?, completed_at = ? "
                "WHERE call_id = ?",
                ("operator_cancelled", now, call_id),
            )
            if authorized is not None:
                connection.execute(
                    "INSERT INTO audit_records(audit_id, subject_id, action, actor, "
                    "payload_json, occurred_at) VALUES (?, ?, "
                    "'model_unknown_retry_cancelled', ?, ?, ?)",
                    (
                        new_id("audit"),
                        row["subject_id"],
                        actor,
                        canonical_json({"call_id": call_id, "reason": reason}),
                        now,
                    ),
                )
            return self._load_call_connection(connection, call_id)

    @staticmethod
    def _budget_status_connection(
        connection: Any,
        subject_id: str,
        limits: BudgetLimits,
        budget_day: str,
        *,
        resource_pool: str | None = None,
        resource_group_id: str | None = None,
    ) -> BudgetStatus:
        if resource_pool is not None and resource_pool not in {"economy", "deep"}:
            raise ValueError("model resource pool is invalid")
        pool_clause = "" if resource_pool is None else " AND c.resource_pool = ?"
        params: tuple[Any, ...] = (subject_id, budget_day)
        if resource_pool is not None:
            params += (resource_pool,)
        group_clause = "" if resource_group_id is None else " AND c.resource_group_id = ?"
        if resource_group_id is not None:
            params += (resource_group_id,)
        query = """SELECT
                COUNT(*) AS attempts,
                COALESCE(SUM(COALESCE(input_tokens, reserved_input_tokens)), 0) AS input_tokens,
                COALESCE(SUM(COALESCE(output_tokens, reserved_output_tokens)), 0) AS output_tokens,
                COALESCE(SUM(COALESCE(cost_microusd, reserved_cost_microusd)), 0) AS cost_microusd
               FROM model_attempts a
               JOIN model_calls c ON c.call_id = a.call_id
                WHERE a.subject_id = ? AND a.budget_day = ? AND a.status != 'cancelled'"""
        query += pool_clause
        query += group_clause
        row = connection.execute(query, params).fetchone()
        return BudgetStatus(
            budget_day=budget_day,
            attempts=int(row["attempts"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cost_microusd=int(row["cost_microusd"]),
            limits=limits,
        )

    @staticmethod
    def _get_call_row(connection: Any, call_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM model_calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"model call not found: {call_id}")
        return row

    @staticmethod
    def _get_attempt_row(connection: Any, attempt_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM model_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"model attempt not found: {attempt_id}")
        return row

    @classmethod
    def _load_call_connection(cls, connection: Any, call_id: str) -> CallRecord:
        return cls._call_from_row(cls._get_call_row(connection, call_id))

    @classmethod
    def _load_attempt_connection(cls, connection: Any, attempt_id: str) -> AttemptRecord:
        return cls._attempt_from_row(cls._get_attempt_row(connection, attempt_id))

    @staticmethod
    def _call_from_row(row: Any) -> CallRecord:
        status = row["status"]
        if status not in CALL_STATES:
            raise IntegrityError(f"invalid persisted model call status: {status}")
        call_id = row["call_id"]
        metadata = (
            row["provider"],
            row["model"],
            row["purpose"],
            row["request_hash"],
            row["idempotency_key"],
        )
        if any(not isinstance(value, str) or not value.strip() for value in metadata):
            raise IntegrityError(f"model call metadata is invalid: {call_id}")
        resource_pool = row["resource_pool"]
        if resource_pool not in {"economy", "deep"}:
            raise IntegrityError(f"model call resource pool is invalid: {call_id}")
        capture_policy_version = row["capture_policy_version"]
        if capture_policy_version is not None:
            try:
                parsed_policy_version = strict_int(capture_policy_version)
            except (TypeError, ValueError, OverflowError) as error:
                raise IntegrityError(
                    f"model call capture policy version is invalid: {call_id}"
                ) from error
            if parsed_policy_version < 1:
                raise IntegrityError(f"model call capture policy version is invalid: {call_id}")
        raw_response = row["response_json"]
        if raw_response is not None and not isinstance(raw_response, str):
            raise IntegrityError(f"model response payload is invalid: {call_id}")
        try:
            response_json = decompress_text(raw_response)
            response = strict_json_loads(response_json) if response_json else None
        except IntegrityError:
            raise
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"model response payload is invalid: {call_id}") from error
        if (response is None) != (row["response_hash"] is None):
            raise IntegrityError(f"model response metadata mismatch: {call_id}")
        if response is not None and (
            not isinstance(response, dict) or content_hash(response) != row["response_hash"]
        ):
            raise IntegrityError(f"model response integrity failure: {call_id}")
        if status == "succeeded" and response is None:
            raise IntegrityError(f"successful model call has no response: {call_id}")
        completed_at = row["completed_at"]
        if (status in {"prepared", "executing"}) != (completed_at is None):
            raise IntegrityError(f"model call completion state is invalid: {call_id}")
        try:
            usage_estimated = strict_bool(row["usage_estimated"])
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"model call usage flag is invalid: {call_id}") from error
        return CallRecord(
            call_id=call_id,
            subject_id=row["subject_id"],
            provider=row["provider"],
            model=row["model"],
            purpose=row["purpose"],
            request_hash=row["request_hash"],
            idempotency_key=row["idempotency_key"],
            status=status,
            response=response,
            response_hash=row["response_hash"],
            usage_estimated=usage_estimated,
            error_code=row["error_code"],
            created_at=row["created_at"],
            completed_at=completed_at,
            resource_pool=resource_pool,
            resource_group_id=row["resource_group_id"],
        )

    @staticmethod
    def _attempt_from_row(row: Any) -> AttemptRecord:
        status = row["status"]
        if status not in ATTEMPT_STATES:
            raise IntegrityError(f"invalid persisted model attempt status: {status}")
        attempt_id = row["attempt_id"]

        def counter(column: str, *, minimum: int) -> int:
            try:
                parsed = strict_int(row[column])
            except (TypeError, ValueError, OverflowError) as error:
                raise IntegrityError(f"model attempt {column} is invalid: {attempt_id}") from error
            if parsed < minimum:
                raise IntegrityError(f"model attempt {column} is invalid: {attempt_id}")
            return parsed

        def optional_counter(column: str) -> int | None:
            return None if row[column] is None else counter(column, minimum=0)

        input_tokens = optional_counter("input_tokens")
        output_tokens = optional_counter("output_tokens")
        cost_microusd = optional_counter("cost_microusd")
        usage_known = (
            input_tokens is not None,
            output_tokens is not None,
            cost_microusd is not None,
        )
        if len(set(usage_known)) != 1:
            raise IntegrityError(f"model attempt usage state is invalid: {attempt_id}")
        started_at = row["started_at"]
        completed_at = row["completed_at"]
        if status == "authorized" and (
            started_at is not None or completed_at is not None or any(usage_known)
        ):
            raise IntegrityError(f"model attempt lifecycle is invalid: {attempt_id}")
        if status == "executing" and (
            started_at is None or completed_at is not None or any(usage_known)
        ):
            raise IntegrityError(f"model attempt lifecycle is invalid: {attempt_id}")
        if status in {"succeeded", "failed", "unknown"} and (
            started_at is None or completed_at is None
        ):
            raise IntegrityError(f"model attempt lifecycle is invalid: {attempt_id}")
        if status == "cancelled" and completed_at is None:
            raise IntegrityError(f"model attempt lifecycle is invalid: {attempt_id}")
        if status == "succeeded" and not all(usage_known):
            raise IntegrityError(f"model attempt usage state is invalid: {attempt_id}")

        return AttemptRecord(
            attempt_id=attempt_id,
            call_id=row["call_id"],
            subject_id=row["subject_id"],
            budget_day=row["budget_day"],
            attempt_number=counter("attempt_number", minimum=1),
            status=status,
            reserved_input_tokens=counter("reserved_input_tokens", minimum=0),
            reserved_output_tokens=counter("reserved_output_tokens", minimum=0),
            reserved_cost_microusd=counter("reserved_cost_microusd", minimum=0),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=cost_microusd,
            provider_request_id=row["provider_request_id"],
            error_code=row["error_code"],
            started_at=started_at,
            completed_at=completed_at,
        )
