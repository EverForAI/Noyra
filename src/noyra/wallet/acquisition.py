from __future__ import annotations

import builtins
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import TracebackType
from typing import Any, Self

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.identity import validate_subject_id
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_int,
    strict_json_loads,
    utc_now,
)

from .rpc import WalletRPCBalanceAcquirer, WalletRPCError
from .store import WalletStore
from .types import (
    WalletBalanceSnapshotInput,
    WalletBalanceSnapshotRecord,
    canonical_balance,
    validate_timestamp,
)

ACQUISITION_RUN_STATES = frozenset(
    {"queued", "running", "retry_wait", "succeeded", "failed", "unknown", "cancelled"}
)
ACQUISITION_ATTEMPT_STATES = frozenset({"executing", "succeeded", "failed", "unknown"})
WALLET_ACQUISITION_MAX_ATTEMPTS = 5
WALLET_ACQUISITION_MAX_BATCH = 32
WALLET_ACQUISITION_DEFAULT_MAX_CONCURRENT = 2
WALLET_ACQUISITION_DEFAULT_MIN_INTERVAL_SECONDS = 5.0
WALLET_ACQUISITION_DEFAULT_WINDOW_SECONDS = 60.0
WALLET_ACQUISITION_DEFAULT_REQUEST_LIMIT_PER_WINDOW = 30
WALLET_ACQUISITION_DEFAULT_LEASE_SECONDS = 60
WALLET_ACQUISITION_DEFAULT_BACKOFF_SECONDS = 2.0
WALLET_ACQUISITION_MAX_BACKOFF_SECONDS = 300.0
_ERROR_CODE = re.compile(r"wallet_rpc_[a-z0-9_]{1,85}\Z")
_ACQUISITION_AUDIT_ACTIONS = frozenset(
    {
        "wallet_balance_acquisition_queued",
        "wallet_balance_acquisition_failed",
        "wallet_balance_acquisition_unknown",
        "wallet_balance_acquisition_retried",
        "wallet_balance_acquisition_succeeded",
        "wallet_balance_acquisition_cancelled",
    }
)
_ACQUISITION_CANCELLATION_ERROR_CODES = frozenset(
    {"wallet_rpc_operator_cancelled", "wallet_rpc_target_revoked"}
)


@dataclass(frozen=True)
class WalletBalanceAcquisitionRunRecord:
    run_id: str
    subject_id: str
    network_id: str
    asset_id: str
    address_id: str
    idempotency_key: str
    status: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: str | None
    last_error_code: str | None
    snapshot_id: str | None
    claim_token: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    updated_at: str


@dataclass(frozen=True)
class WalletBalanceAcquisitionAttemptRecord:
    attempt_id: str
    run_id: str
    subject_id: str
    network_id: str
    asset_id: str
    address_id: str
    attempt_number: int
    status: str
    reserved_request_count: int
    request_count: int | None
    error_code: str | None
    snapshot_id: str | None
    claim_token: str
    lease_owner: str
    started_at: str
    completed_at: str | None


@dataclass(frozen=True)
class WalletAcquisitionBudgetStatus:
    subject_id: str
    network_id: str
    window_seconds: float
    request_limit: int
    requests_used: int
    requests_reserved: int
    next_allowed_at: str | None


class WalletAcquisitionRunError(RuntimeError):
    """Sanitized durable acquisition-run error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class WalletAcquisitionConflictError(ValueError):
    """A durable queue conflict that callers may map to HTTP 409."""

    code = "wallet_acquisition_conflict"


class WalletBalanceAcquisitionLedger:
    """Durable queue, leases, bounded retries, and read-only balance evidence.

    A run is deliberately limited to the two fixed RPC requests performed by
    ``WalletRPCBalanceAcquirer``: ``eth_chainId`` and one balance read.  This
    ledger never stores URLs, request payloads, response bodies, credentials,
    private keys, signatures, transactions, or payment amounts.
    """

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], str] = utc_now,
        max_concurrent: int = WALLET_ACQUISITION_DEFAULT_MAX_CONCURRENT,
        min_interval_seconds: float = WALLET_ACQUISITION_DEFAULT_MIN_INTERVAL_SECONDS,
        window_seconds: float = WALLET_ACQUISITION_DEFAULT_WINDOW_SECONDS,
        request_limit_per_window: int = WALLET_ACQUISITION_DEFAULT_REQUEST_LIMIT_PER_WINDOW,
        lease_seconds: int = WALLET_ACQUISITION_DEFAULT_LEASE_SECONDS,
        backoff_seconds: float = WALLET_ACQUISITION_DEFAULT_BACKOFF_SECONDS,
    ):
        if not all(
            callable(getattr(database, method, None))
            for method in ("transaction", "connection", "read_transaction")
        ):
            raise TypeError("wallet acquisition database is invalid")
        if isinstance(max_concurrent, bool) or not 1 <= max_concurrent <= 8:
            raise ValueError("wallet acquisition concurrency limit is invalid")
        if isinstance(min_interval_seconds, bool) or not 0 <= min_interval_seconds <= 3_600:
            raise ValueError("wallet acquisition minimum interval is invalid")
        if isinstance(window_seconds, bool) or not 1 <= window_seconds <= 86_400:
            raise ValueError("wallet acquisition window is invalid")
        if isinstance(request_limit_per_window, bool) or not (
            2 <= request_limit_per_window <= 1_000
        ):
            raise ValueError("wallet acquisition request budget is invalid")
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 86_400:
            raise ValueError("wallet acquisition lease is invalid")
        if isinstance(backoff_seconds, bool) or not (
            0 < backoff_seconds <= WALLET_ACQUISITION_MAX_BACKOFF_SECONDS
        ):
            raise ValueError("wallet acquisition backoff is invalid")
        self.database = database
        self.store = WalletStore(database)
        self.clock = clock
        self.max_concurrent = max_concurrent
        self.min_interval_seconds = float(min_interval_seconds)
        self.window_seconds = float(window_seconds)
        self.request_limit_per_window = request_limit_per_window
        self.lease_seconds = lease_seconds
        self.backoff_seconds = float(backoff_seconds)
        # Durable claims are authoritative across processes.  This semaphore
        # is only an additional per-process guard against needless work.
        self._slots = threading.BoundedSemaphore(max_concurrent)

    @staticmethod
    def _parse_time(value: object, field: str = "time") -> datetime:
        if not isinstance(value, str):
            raise WalletAcquisitionRunError(f"wallet_acquisition_invalid_{field}")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise WalletAcquisitionRunError(f"wallet_acquisition_invalid_{field}") from error
        if parsed.tzinfo is None:
            raise WalletAcquisitionRunError(f"wallet_acquisition_invalid_{field}")
        return parsed.astimezone(UTC)

    def _now(self) -> str:
        value = self.clock()
        return self._parse_time(value, "clock").isoformat(timespec="milliseconds")

    @classmethod
    def _canonical_time(cls, value: object, field: str) -> str:
        return cls._parse_time(value, field).isoformat(timespec="milliseconds")

    def _lease_expiry(self, now: str) -> str:
        return (self._parse_time(now) + timedelta(seconds=self.lease_seconds)).isoformat(
            timespec="milliseconds"
        )

    def _retry_at(self, now: str, attempt_number: int) -> str:
        delay = min(
            WALLET_ACQUISITION_MAX_BACKOFF_SECONDS,
            self.backoff_seconds * (2 ** max(0, attempt_number - 1)),
        )
        return (self._parse_time(now) + timedelta(seconds=delay)).isoformat(timespec="milliseconds")

    @staticmethod
    def _validate_idempotency_key(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("wallet acquisition idempotency key is invalid")
        candidate = value.strip()
        if not 1 <= len(candidate) <= 128 or re.fullmatch(r"[A-Za-z0-9._:-]+", candidate) is None:
            raise ValueError("wallet acquisition idempotency key is invalid")
        return candidate

    @staticmethod
    def _validate_claim_token(value: object) -> str:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128:
            raise ValueError("wallet acquisition claim token is invalid")
        return value.strip()

    @staticmethod
    def _validate_worker_owner(value: object) -> str:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128:
            raise ValueError("wallet acquisition worker owner is invalid")
        return value.strip()

    @staticmethod
    def _validate_reason(value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("wallet acquisition reason must be text")
        reason = value.strip()
        if not reason or len(reason) > 2_000:
            raise ValueError("wallet acquisition reason is invalid")
        return reason

    @staticmethod
    def _error_code(error: BaseException) -> str:
        code = getattr(error, "code", None)
        if not isinstance(code, str) or _ERROR_CODE.fullmatch(code) is None:
            return "wallet_rpc_transport_failed"
        return code

    @staticmethod
    def _validate_error_code(value: object) -> str:
        if not isinstance(value, str) or _ERROR_CODE.fullmatch(value) is None:
            raise ValueError("wallet acquisition error code is invalid")
        return value

    @staticmethod
    def _is_retryable(code: str) -> bool:
        if code in {
            "wallet_rpc_timeout",
            "wallet_rpc_connect_failed",
            "wallet_rpc_transport_failed",
            "wallet_rpc_capacity_exhausted",
        }:
            return True
        match = re.fullmatch(r"wallet_rpc_http_(5[0-9]{2})", code)
        return match is not None and int(match.group(1)) in {500, 502, 503, 504}

    @staticmethod
    def _run_hash(
        *,
        run_id: str,
        subject_id: str,
        network_id: str,
        asset_id: str,
        address_id: str,
        idempotency_key: str,
        status: str,
        attempt_count: int,
        max_attempts: int,
        next_attempt_at: str | None,
        last_error_code: str | None,
        snapshot_id: str | None,
        claim_token: str | None,
        lease_owner: str | None,
        lease_expires_at: str | None,
        created_at: str,
        started_at: str | None,
        completed_at: str | None,
        updated_at: str,
    ) -> str:
        return content_hash(
            {
                "run_id": run_id,
                "subject_id": subject_id,
                "network_id": network_id,
                "asset_id": asset_id,
                "address_id": address_id,
                "idempotency_key": idempotency_key,
                "status": status,
                "attempt_count": attempt_count,
                "max_attempts": max_attempts,
                "next_attempt_at": next_attempt_at,
                "last_error_code": last_error_code,
                "snapshot_id": snapshot_id,
                "claim_token": claim_token,
                "lease_owner": lease_owner,
                "lease_expires_at": lease_expires_at,
                "created_at": created_at,
                "started_at": started_at,
                "completed_at": completed_at,
                "updated_at": updated_at,
            }
        )

    @staticmethod
    def _attempt_hash(
        *,
        attempt_id: str,
        run_id: str,
        subject_id: str,
        network_id: str,
        asset_id: str,
        address_id: str,
        attempt_number: int,
        status: str,
        reserved_request_count: int,
        request_count: int | None,
        error_code: str | None,
        snapshot_id: str | None,
        claim_token: str,
        lease_owner: str,
        started_at: str,
        completed_at: str | None,
    ) -> str:
        return content_hash(
            {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "subject_id": subject_id,
                "network_id": network_id,
                "asset_id": asset_id,
                "address_id": address_id,
                "attempt_number": attempt_number,
                "status": status,
                "reserved_request_count": reserved_request_count,
                "request_count": request_count,
                "error_code": error_code,
                "snapshot_id": snapshot_id,
                "claim_token": claim_token,
                "lease_owner": lease_owner,
                "started_at": started_at,
                "completed_at": completed_at,
            }
        )

    @classmethod
    def _assert_run_hash(cls, row: Any) -> None:
        """Reject a stale or manually altered run before rewriting its state."""
        try:
            expected = cls._run_hash(
                run_id=str(row["run_id"]),
                subject_id=str(row["subject_id"]),
                network_id=str(row["network_id"]),
                asset_id=str(row["asset_id"]),
                address_id=str(row["address_id"]),
                idempotency_key=str(row["idempotency_key"]),
                status=str(row["status"]),
                attempt_count=int(row["attempt_count"]),
                max_attempts=int(row["max_attempts"]),
                next_attempt_at=row["next_attempt_at"],
                last_error_code=row["last_error_code"],
                snapshot_id=row["snapshot_id"],
                claim_token=row["claim_token"],
                lease_owner=row["lease_owner"],
                lease_expires_at=row["lease_expires_at"],
                created_at=str(row["created_at"]),
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                updated_at=str(row["updated_at"]),
            )
        except (TypeError, ValueError, KeyError) as error:
            raise IntegrityError(
                f"wallet acquisition run hash inputs are invalid: {row['run_id']}"
            ) from error
        if row["state_hash"] != expected:
            raise IntegrityError(f"wallet acquisition run hash mismatch: {row['run_id']}")

    @classmethod
    def _assert_attempt_hash(cls, row: Any) -> None:
        """Reject a tampered attempt before applying a terminal transition."""
        try:
            expected = cls._attempt_hash(
                attempt_id=str(row["attempt_id"]),
                run_id=str(row["run_id"]),
                subject_id=str(row["subject_id"]),
                network_id=str(row["network_id"]),
                asset_id=str(row["asset_id"]),
                address_id=str(row["address_id"]),
                attempt_number=int(row["attempt_number"]),
                status=str(row["status"]),
                reserved_request_count=int(row["reserved_request_count"]),
                request_count=(None if row["request_count"] is None else int(row["request_count"])),
                error_code=row["error_code"],
                snapshot_id=row["snapshot_id"],
                claim_token=str(row["claim_token"]),
                lease_owner=str(row["lease_owner"]),
                started_at=str(row["started_at"]),
                completed_at=row["completed_at"],
            )
        except (TypeError, ValueError, KeyError) as error:
            raise IntegrityError(
                f"wallet acquisition attempt hash inputs are invalid: {row['attempt_id']}"
            ) from error
        if row["state_hash"] != expected:
            raise IntegrityError(f"wallet acquisition attempt hash mismatch: {row['attempt_id']}")

    @classmethod
    def _run_from_row(cls, row: Any) -> WalletBalanceAcquisitionRunRecord:
        return WalletBalanceAcquisitionRunRecord(
            str(row["run_id"]),
            str(row["subject_id"]),
            str(row["network_id"]),
            str(row["asset_id"]),
            str(row["address_id"]),
            str(row["idempotency_key"]),
            str(row["status"]),
            int(row["attempt_count"]),
            int(row["max_attempts"]),
            row["next_attempt_at"],
            row["last_error_code"],
            row["snapshot_id"],
            row["claim_token"],
            row["lease_owner"],
            row["lease_expires_at"],
            str(row["created_at"]),
            row["started_at"],
            row["completed_at"],
            str(row["updated_at"]),
        )

    @classmethod
    def _attempt_from_row(cls, row: Any) -> WalletBalanceAcquisitionAttemptRecord:
        return WalletBalanceAcquisitionAttemptRecord(
            str(row["attempt_id"]),
            str(row["run_id"]),
            str(row["subject_id"]),
            str(row["network_id"]),
            str(row["asset_id"]),
            str(row["address_id"]),
            int(row["attempt_number"]),
            str(row["status"]),
            int(row["reserved_request_count"]),
            None if row["request_count"] is None else int(row["request_count"]),
            row["error_code"],
            row["snapshot_id"],
            str(row["claim_token"]),
            str(row["lease_owner"]),
            str(row["started_at"]),
            row["completed_at"],
        )

    def enqueue(
        self,
        subject_id: str,
        *,
        asset_id: str,
        address_id: str,
        actor: str,
        idempotency_key: str | None = None,
        max_attempts: int = WALLET_ACQUISITION_MAX_ATTEMPTS,
        not_before: str | None = None,
    ) -> WalletBalanceAcquisitionRunRecord:
        WalletStore._require_operator(actor, "queue a wallet balance acquisition")
        validate_subject_id(subject_id)
        if isinstance(max_attempts, bool) or not (
            1 <= max_attempts <= WALLET_ACQUISITION_MAX_ATTEMPTS
        ):
            raise ValueError("wallet acquisition max attempts is invalid")
        asset = self.store.get_asset(asset_id, subject_id=subject_id)
        address = self.store.get_address(address_id, subject_id=subject_id)
        if (
            asset.status != "active"
            or address.status != "active"
            or asset.network_id != address.network_id
        ):
            raise ValueError("wallet acquisition references are not active on one network")
        network = self.store.get_network(asset.network_id, subject_id=subject_id)
        if network.status != "active":
            raise ValueError("wallet network is not active")
        key = self._validate_idempotency_key(
            idempotency_key
            if idempotency_key is not None
            else content_hash(
                {"asset_id": asset_id, "address_id": address_id, "nonce": new_id("queue")}
            )
        )
        now = self._now()
        due_at = now if not_before is None else self._canonical_time(not_before, "not_before")
        run_id = new_id("walletacq")
        audit_id = new_id("audit")
        state_hash = self._run_hash(
            run_id=run_id,
            subject_id=subject_id,
            network_id=asset.network_id,
            asset_id=asset_id,
            address_id=address_id,
            idempotency_key=key,
            status="queued",
            attempt_count=0,
            max_attempts=max_attempts,
            next_attempt_at=due_at,
            last_error_code=None,
            snapshot_id=None,
            claim_token=None,
            lease_owner=None,
            lease_expires_at=None,
            created_at=now,
            started_at=None,
            completed_at=None,
            updated_at=now,
        )
        audit_payload = canonical_json(
            {
                "run_id": run_id,
                "network_id": asset.network_id,
                "asset_id": asset_id,
                "address_id": address_id,
                "idempotency_key": key,
            }
        )
        try:
            with self.database.transaction() as connection:
                current = connection.execute(
                    """SELECT * FROM wallet_balance_acquisition_runs
                       WHERE subject_id = ? AND idempotency_key = ?""",
                    (subject_id, key),
                ).fetchone()
                if current is not None:
                    self._assert_run_hash(current)
                    existing = self._run_from_row(current)
                    if (
                        existing.asset_id != asset_id
                        or existing.address_id != address_id
                        or existing.network_id != asset.network_id
                        or existing.max_attempts != max_attempts
                    ):
                        raise WalletAcquisitionConflictError(
                            "wallet acquisition idempotency key identifies another target"
                        )
                    return existing
                connection.execute(
                    """INSERT INTO audit_records(
                        audit_id, subject_id, action, actor, payload_json, occurred_at
                    ) VALUES (?, ?, 'wallet_balance_acquisition_queued', ?, ?, ?)""",
                    (audit_id, subject_id, actor.strip(), audit_payload, now),
                )
                connection.execute(
                    """INSERT INTO wallet_balance_acquisition_runs(
                        run_id, subject_id, network_id, asset_id, address_id, idempotency_key,
                        status, attempt_count, max_attempts, next_attempt_at, last_error_code,
                        snapshot_id, claim_token, lease_owner, lease_expires_at, created_audit_id,
                        state_hash, created_at, started_at, completed_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, NULL, NULL, NULL, NULL, NULL,
                              ?, ?, ?, NULL, NULL, ?)""",
                    (
                        run_id,
                        subject_id,
                        asset.network_id,
                        asset_id,
                        address_id,
                        key,
                        max_attempts,
                        due_at,
                        audit_id,
                        state_hash,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as error:
            message = str(error)
            if (
                "UNIQUE constraint failed" not in message
                or "wallet_balance_acquisition_runs.subject_id" not in message
            ):
                raise
            raise WalletAcquisitionConflictError(
                "wallet acquisition target already has an active run"
            ) from error
        return self.get(run_id, subject_id=subject_id)

    def get(self, run_id: str, *, subject_id: str) -> WalletBalanceAcquisitionRunRecord:
        validate_subject_id(subject_id)
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 128:
            raise ValueError("wallet acquisition run identifier is invalid")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM wallet_balance_acquisition_runs WHERE run_id = ? AND subject_id = ?",
                (run_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"wallet acquisition run not found: {run_id}")
        self._assert_run_hash(row)
        return self._run_from_row(row)

    def list(
        self,
        subject_id: str,
        *,
        limit: int = 100,
        statuses: tuple[str, ...] | None = None,
    ) -> builtins.list[WalletBalanceAcquisitionRunRecord]:
        validate_subject_id(subject_id)
        if isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ValueError("wallet acquisition list limit is invalid")
        clauses = ["subject_id = ?"]
        parameters: builtins.list[object] = [subject_id]
        if statuses is not None:
            if not statuses or any(status not in ACQUISITION_RUN_STATES for status in statuses):
                raise ValueError("wallet acquisition status filter is invalid")
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            parameters.extend(statuses)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM wallet_balance_acquisition_runs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (*parameters, limit),
            ).fetchall()
        records: builtins.list[WalletBalanceAcquisitionRunRecord] = []
        for row in rows:
            self._assert_run_hash(row)
            records.append(self._run_from_row(row))
        return records

    def status_summary(self, subject_id: str) -> dict[str, Any]:
        """Return bounded queue counters for operator health projections.

        This projection intentionally omits run identifiers, payloads, claim
        tokens, and worker credentials.  Aggregate SQL keeps diagnostics
        constant-size even when acquisition history is large.
        """

        validate_subject_id(subject_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count "
                "FROM wallet_balance_acquisition_runs WHERE subject_id = ? "
                "GROUP BY status ORDER BY status",
                (subject_id,),
            ).fetchall()
            attempts = connection.execute(
                "SELECT COUNT(*) AS count FROM wallet_balance_acquisition_attempts "
                "WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            next_attempt = connection.execute(
                "SELECT MIN(next_attempt_at) AS next_attempt_at "
                "FROM wallet_balance_acquisition_runs WHERE subject_id = ? "
                "AND status IN ('queued', 'retry_wait')",
                (subject_id,),
            ).fetchone()
            now = self._now()
            expired = connection.execute(
                "SELECT COUNT(*) AS count FROM wallet_balance_acquisition_runs "
                "WHERE subject_id = ? AND status = 'running' "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?)",
                (subject_id, now),
            ).fetchone()
        counts = {state: 0 for state in sorted(ACQUISITION_RUN_STATES)}
        for row in rows:
            status = str(row["status"])
            if status not in counts:
                raise IntegrityError(f"wallet acquisition status is invalid: {status}")
            try:
                counts[status] = int(row["count"])
            except (TypeError, ValueError) as error:
                raise IntegrityError("wallet acquisition status count is invalid") from error
        total = sum(counts.values())
        active = counts["queued"] + counts["running"] + counts["retry_wait"]
        return {
            "subject_id": subject_id,
            "counts": counts,
            "total": total,
            "active": active,
            "attention": counts["unknown"] + counts["failed"],
            "attempts": int(attempts["count"]),
            "expired_running": int(expired["count"]),
            "next_attempt_at": None if next_attempt is None else next_attempt["next_attempt_at"],
        }

    def attempts(
        self, run_id: str, *, subject_id: str
    ) -> builtins.list[WalletBalanceAcquisitionAttemptRecord]:
        validate_subject_id(subject_id)
        with self.database.connection() as connection:
            run = connection.execute(
                "SELECT * FROM wallet_balance_acquisition_runs WHERE run_id = ? AND subject_id = ?",
                (run_id, subject_id),
            ).fetchone()
            if run is None:
                raise NotFoundError(f"wallet acquisition run not found: {run_id}")
            rows = connection.execute(
                """SELECT a.* FROM wallet_balance_acquisition_attempts a
                   JOIN wallet_balance_acquisition_runs r ON r.run_id = a.run_id
                   WHERE a.run_id = ? AND a.subject_id = ? AND r.subject_id = ?
                   ORDER BY a.attempt_number""",
                (run_id, subject_id, subject_id),
            ).fetchall()
        self._assert_run_hash(run)
        records: builtins.list[WalletBalanceAcquisitionAttemptRecord] = []
        for row in rows:
            self._assert_attempt_hash(row)
            records.append(self._attempt_from_row(row))
        return records

    def _load_run_connection(self, connection: Any, run_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM wallet_balance_acquisition_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"wallet acquisition run not found: {run_id}")
        self._assert_run_hash(row)
        return row

    @staticmethod
    def _load_attempt_connection(connection: Any, run_id: str, token: str) -> Any:
        row = connection.execute(
            """SELECT * FROM wallet_balance_acquisition_attempts
               WHERE run_id = ? AND claim_token = ?""",
            (run_id, token),
        ).fetchone()
        if row is None:
            raise WalletAcquisitionRunError("wallet_acquisition_attempt_missing")
        return row

    def budget_status(
        self, subject_id: str, network_id: str, *, now: str | None = None
    ) -> WalletAcquisitionBudgetStatus:
        validate_subject_id(subject_id)
        if not isinstance(network_id, str) or not network_id.strip() or len(network_id) > 128:
            raise ValueError("wallet acquisition network identifier is invalid")
        current = self._now() if now is None else self._canonical_time(now, "budget_time")
        current_time = self._parse_time(current, "budget_time")
        cutoff = (current_time - timedelta(seconds=self.window_seconds)).isoformat(
            timespec="milliseconds"
        )
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT status, reserved_request_count, request_count, started_at
                   FROM wallet_balance_acquisition_attempts
                   WHERE subject_id = ? AND network_id = ? AND started_at > ?
                   ORDER BY started_at, attempt_id""",
                (subject_id, network_id, cutoff),
            ).fetchall()
        used = sum(int(row["request_count"]) for row in rows if row["request_count"] is not None)
        reserved = sum(
            int(row["reserved_request_count"]) for row in rows if row["request_count"] is None
        )
        next_allowed: datetime | None = None
        if rows and self.min_interval_seconds > 0:
            next_allowed = self._parse_time(rows[-1]["started_at"], "attempt_time") + timedelta(
                seconds=self.min_interval_seconds
            )
        outstanding = used + reserved
        if outstanding + 2 > self.request_limit_per_window:
            for row in rows:
                cost = (
                    int(row["request_count"])
                    if row["request_count"] is not None
                    else int(row["reserved_request_count"])
                )
                outstanding -= cost
                if outstanding + 2 <= self.request_limit_per_window:
                    budget_due = self._parse_time(row["started_at"], "attempt_time") + timedelta(
                        seconds=self.window_seconds
                    )
                    next_allowed = (
                        budget_due if next_allowed is None else max(next_allowed, budget_due)
                    )
                    break
        return WalletAcquisitionBudgetStatus(
            subject_id,
            network_id,
            self.window_seconds,
            self.request_limit_per_window,
            used,
            reserved,
            None if next_allowed is None else next_allowed.isoformat(timespec="milliseconds"),
        )

    def _network_due_allowed(
        self, connection: Any, subject_id: str, network_id: str, now: str
    ) -> bool:
        current = self._parse_time(now, "claim_time")
        if self.min_interval_seconds <= 0:
            return True
        row = connection.execute(
            """SELECT started_at FROM wallet_balance_acquisition_attempts
               WHERE subject_id = ? AND network_id = ?
               ORDER BY started_at DESC, attempt_id DESC LIMIT 1""",
            (subject_id, network_id),
        ).fetchone()
        if row is None:
            return True
        try:
            latest = self._parse_time(row["started_at"], "attempt_time")
        except WalletAcquisitionRunError as error:
            raise IntegrityError("wallet acquisition attempt timestamp is invalid") from error
        return current >= latest + timedelta(seconds=self.min_interval_seconds)

    def _request_budget_allowed(
        self, connection: Any, subject_id: str, network_id: str, now: str
    ) -> bool:
        cutoff = (
            self._parse_time(now, "claim_time") - timedelta(seconds=self.window_seconds)
        ).isoformat(timespec="milliseconds")
        row = connection.execute(
            """SELECT COALESCE(SUM(
                       CASE WHEN request_count IS NULL
                            THEN reserved_request_count ELSE request_count END
                   ), 0) AS requests
                   FROM wallet_balance_acquisition_attempts
                   WHERE subject_id = ? AND network_id = ? AND started_at > ?""",
            (subject_id, network_id, cutoff),
        ).fetchone()
        return int(row["requests"]) + 2 <= self.request_limit_per_window

    def _target_is_active_connection(self, connection: Any, row: Any) -> bool:
        """Revalidate the public target immediately before leasing work."""

        target = connection.execute(
            """SELECT n.status AS network_status, a.status AS asset_status,
                      d.status AS address_status, a.network_id AS asset_network_id,
                      d.network_id AS address_network_id
               FROM wallet_networks n
               JOIN wallet_assets a ON a.asset_id = ?
               JOIN wallet_addresses d ON d.address_id = ?
               WHERE n.network_id = ? AND n.subject_id = ?
                 AND a.subject_id = ? AND d.subject_id = ?""",
            (
                row["asset_id"],
                row["address_id"],
                row["network_id"],
                row["subject_id"],
                row["subject_id"],
                row["subject_id"],
            ),
        ).fetchone()
        return bool(
            target is not None
            and target["network_status"] == "active"
            and target["asset_status"] == "active"
            and target["address_status"] == "active"
            and target["asset_network_id"] == row["network_id"]
            and target["address_network_id"] == row["network_id"]
        )

    def recover_expired(
        self,
        subject_id: str,
        *,
        limit: int = WALLET_ACQUISITION_MAX_BATCH,
        now: str | None = None,
    ) -> int:
        validate_subject_id(subject_id)
        if isinstance(limit, bool) or not 1 <= limit <= WALLET_ACQUISITION_MAX_BATCH:
            raise ValueError("wallet acquisition recovery limit is invalid")
        current = self._now() if now is None else self._canonical_time(now, "recovery_time")
        recovered = 0
        while recovered < limit:
            with self.database.transaction() as connection:
                row = connection.execute(
                    """SELECT * FROM wallet_balance_acquisition_runs
                       WHERE subject_id = ? AND status = 'running'
                         AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                       ORDER BY lease_expires_at, run_id LIMIT 1""",
                    (subject_id, current),
                ).fetchone()
                if row is None:
                    break
                self._assert_run_hash(row)
                self._recover_running_connection(
                    connection,
                    row,
                    current,
                    error_code="wallet_rpc_lease_expired",
                    actor="system",
                )
                recovered += 1
        return recovered

    def recover_interrupted(
        self, subject_id: str, *, limit: int = WALLET_ACQUISITION_MAX_BATCH
    ) -> int:
        """Quarantine all in-flight runs during process restart.

        An interrupted external read is not replayed automatically.  It is
        recorded as ``unknown`` and requires an explicit operator requeue.
        """

        validate_subject_id(subject_id)
        if isinstance(limit, bool) or not 1 <= limit <= WALLET_ACQUISITION_MAX_BATCH:
            raise ValueError("wallet acquisition recovery limit is invalid")
        now = self._now()
        recovered = 0
        while recovered < limit:
            with self.database.transaction() as connection:
                row = connection.execute(
                    """SELECT * FROM wallet_balance_acquisition_runs
                       WHERE subject_id = ? AND status = 'running'
                       ORDER BY started_at, run_id LIMIT 1""",
                    (subject_id,),
                ).fetchone()
                if row is None:
                    break
                self._assert_run_hash(row)
                self._recover_running_connection(
                    connection,
                    row,
                    now,
                    error_code="wallet_rpc_interrupted",
                    actor="system",
                )
                recovered += 1
        return recovered

    def _recover_running_connection(
        self,
        connection: Any,
        row: Any,
        now: str,
        *,
        error_code: str,
        actor: str,
    ) -> None:
        token = row["claim_token"]
        owner = row["lease_owner"]
        if not isinstance(token, str) or not isinstance(owner, str):
            raise IntegrityError(f"wallet acquisition lease is invalid: {row['run_id']}")
        self._assert_run_hash(row)
        attempt = connection.execute(
            """SELECT * FROM wallet_balance_acquisition_attempts
               WHERE run_id = ? AND attempt_number = ? AND status = 'executing'""",
            (row["run_id"], row["attempt_count"]),
        ).fetchone()
        if attempt is None:
            raise IntegrityError(f"wallet acquisition attempt is missing: {row['run_id']}")
        self._assert_attempt_hash(attempt)
        error_code = self._validate_error_code(error_code)
        attempt_hash = self._attempt_hash(
            attempt_id=str(attempt["attempt_id"]),
            run_id=str(row["run_id"]),
            subject_id=str(row["subject_id"]),
            network_id=str(row["network_id"]),
            asset_id=str(row["asset_id"]),
            address_id=str(row["address_id"]),
            attempt_number=int(row["attempt_count"]),
            status="unknown",
            reserved_request_count=2,
            request_count=None,
            error_code=error_code,
            snapshot_id=None,
            claim_token=str(token),
            lease_owner=str(owner),
            started_at=str(attempt["started_at"]),
            completed_at=now,
        )
        changed_attempt = connection.execute(
            """UPDATE wallet_balance_acquisition_attempts
               SET status = 'unknown', request_count = NULL, error_code = ?,
                   state_hash = ?, completed_at = ?
               WHERE attempt_id = ? AND run_id = ? AND status = 'executing'
                 AND claim_token = ? AND lease_owner = ?""",
            (
                error_code,
                attempt_hash,
                now,
                attempt["attempt_id"],
                row["run_id"],
                token,
                owner,
            ),
        )
        if changed_attempt.rowcount != 1:
            raise WalletAcquisitionRunError("wallet_acquisition_attempt_lost")
        state_hash = self._run_hash(
            run_id=str(row["run_id"]),
            subject_id=str(row["subject_id"]),
            network_id=str(row["network_id"]),
            asset_id=str(row["asset_id"]),
            address_id=str(row["address_id"]),
            idempotency_key=str(row["idempotency_key"]),
            status="unknown",
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=None,
            last_error_code=error_code,
            snapshot_id=None,
            claim_token=None,
            lease_owner=None,
            lease_expires_at=None,
            created_at=str(row["created_at"]),
            started_at=row["started_at"],
            completed_at=now,
            updated_at=now,
        )
        changed = connection.execute(
            """UPDATE wallet_balance_acquisition_runs
               SET status = 'unknown', last_error_code = ?, next_attempt_at = NULL,
                   claim_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                   completed_at = ?, updated_at = ?, state_hash = ?
               WHERE run_id = ? AND subject_id = ? AND status = 'running'
                 AND claim_token = ? AND lease_owner = ? AND state_hash = ?""",
            (
                error_code,
                now,
                now,
                state_hash,
                row["run_id"],
                row["subject_id"],
                token,
                owner,
                row["state_hash"],
            ),
        )
        if changed.rowcount != 1:
            raise WalletAcquisitionRunError("wallet_acquisition_lease_lost")
        connection.execute(
            """INSERT INTO audit_records(
                audit_id, subject_id, action, actor, payload_json, occurred_at
            ) VALUES (?, ?, 'wallet_balance_acquisition_unknown', ?, ?, ?)""",
            (
                new_id("audit"),
                row["subject_id"],
                actor,
                canonical_json(
                    {
                        "run_id": row["run_id"],
                        "attempt_number": int(row["attempt_count"]),
                        "error_code": error_code,
                    }
                ),
                now,
            ),
        )

    def claim_due(
        self,
        subject_id: str,
        *,
        worker_owner: str,
        limit: int = 1,
        now: str | None = None,
    ) -> builtins.list[WalletBalanceAcquisitionRunRecord]:
        validate_subject_id(subject_id)
        owner = self._validate_worker_owner(worker_owner)
        if isinstance(limit, bool) or not 1 <= limit <= WALLET_ACQUISITION_MAX_BATCH:
            raise ValueError("wallet acquisition claim limit is invalid")
        current = self._now() if now is None else self._canonical_time(now, "claim_time")
        self.recover_expired(subject_id, limit=limit, now=current)
        claimed: builtins.list[WalletBalanceAcquisitionRunRecord] = []
        for _ in range(limit):
            token = new_id("walletacqclaim")
            lease = self._lease_expiry(current)
            with self.database.transaction() as connection:
                active = connection.execute(
                    "SELECT COUNT(*) AS count FROM wallet_balance_acquisition_runs "
                    "WHERE subject_id = ? AND status = 'running'",
                    (subject_id,),
                ).fetchone()
                if int(active["count"]) >= self.max_concurrent:
                    break
                candidates = connection.execute(
                    """SELECT * FROM wallet_balance_acquisition_runs
                       WHERE subject_id = ?
                         AND status IN ('queued', 'retry_wait')
                         AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?
                       ORDER BY next_attempt_at, run_id LIMIT ?""",
                    (subject_id, current, WALLET_ACQUISITION_MAX_BATCH),
                ).fetchall()
                selected = None
                for candidate in candidates:
                    if not self._target_is_active_connection(connection, candidate):
                        # Revocation can race with a queued run.  Do not lease a
                        # target that the RPC boundary would reject; cancel it
                        # durably with operator-visible evidence instead.
                        self._cancel_revoked_candidate_connection(connection, candidate, current)
                        continue
                    if not self._network_due_allowed(
                        connection, subject_id, str(candidate["network_id"]), current
                    ):
                        continue
                    if not self._request_budget_allowed(
                        connection, subject_id, str(candidate["network_id"]), current
                    ):
                        continue
                    selected = candidate
                    break
                if selected is None:
                    break
                self._assert_run_hash(selected)
                attempt_number = int(selected["attempt_count"]) + 1
                if attempt_number > int(selected["max_attempts"]):
                    raise IntegrityError(
                        f"wallet acquisition retry count exceeded: {selected['run_id']}"
                    )
                started_at = current
                changed = connection.execute(
                    """UPDATE wallet_balance_acquisition_runs
                       SET status = 'running', attempt_count = ?, next_attempt_at = NULL,
                           last_error_code = NULL, claim_token = ?, lease_owner = ?,
                           lease_expires_at = ?, started_at = COALESCE(started_at, ?),
                           completed_at = NULL, updated_at = ?, state_hash = ?
                       WHERE run_id = ? AND subject_id = ?
                         AND status IN ('queued', 'retry_wait')
                         AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?
                         AND state_hash = ?""",
                    (
                        attempt_number,
                        token,
                        owner,
                        lease,
                        started_at,
                        current,
                        self._run_hash(
                            run_id=str(selected["run_id"]),
                            subject_id=subject_id,
                            network_id=str(selected["network_id"]),
                            asset_id=str(selected["asset_id"]),
                            address_id=str(selected["address_id"]),
                            idempotency_key=str(selected["idempotency_key"]),
                            status="running",
                            attempt_count=attempt_number,
                            max_attempts=int(selected["max_attempts"]),
                            next_attempt_at=None,
                            last_error_code=None,
                            snapshot_id=None,
                            claim_token=token,
                            lease_owner=owner,
                            lease_expires_at=lease,
                            created_at=str(selected["created_at"]),
                            started_at=selected["started_at"] or started_at,
                            completed_at=None,
                            updated_at=current,
                        ),
                        selected["run_id"],
                        subject_id,
                        current,
                        selected["state_hash"],
                    ),
                )
                if changed.rowcount != 1:
                    continue
                attempt_id = new_id("walletacqatt")
                attempt_hash = self._attempt_hash(
                    attempt_id=attempt_id,
                    run_id=str(selected["run_id"]),
                    subject_id=subject_id,
                    network_id=str(selected["network_id"]),
                    asset_id=str(selected["asset_id"]),
                    address_id=str(selected["address_id"]),
                    attempt_number=attempt_number,
                    status="executing",
                    reserved_request_count=2,
                    request_count=None,
                    error_code=None,
                    snapshot_id=None,
                    claim_token=token,
                    lease_owner=owner,
                    started_at=started_at,
                    completed_at=None,
                )
                connection.execute(
                    """INSERT INTO wallet_balance_acquisition_attempts(
                       attempt_id, run_id, subject_id, network_id, asset_id, address_id,
                       attempt_number, status, reserved_request_count, request_count,
                       error_code, snapshot_id, claim_token, lease_owner, state_hash,
                       started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'executing', 2, NULL, NULL, NULL,
                              ?, ?, ?, ?, NULL)""",
                    (
                        attempt_id,
                        selected["run_id"],
                        subject_id,
                        selected["network_id"],
                        selected["asset_id"],
                        selected["address_id"],
                        attempt_number,
                        token,
                        owner,
                        attempt_hash,
                        started_at,
                    ),
                )
                claimed.append(
                    self._run_from_row(
                        connection.execute(
                            "SELECT * FROM wallet_balance_acquisition_runs WHERE run_id = ?",
                            (selected["run_id"],),
                        ).fetchone()
                    )
                )
        return claimed

    def _cancel_revoked_candidate_connection(self, connection: Any, row: Any, now: str) -> None:
        self._assert_run_hash(row)
        error_code = "wallet_rpc_target_revoked"
        state_hash = self._run_hash(
            run_id=str(row["run_id"]),
            subject_id=str(row["subject_id"]),
            network_id=str(row["network_id"]),
            asset_id=str(row["asset_id"]),
            address_id=str(row["address_id"]),
            idempotency_key=str(row["idempotency_key"]),
            status="cancelled",
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=None,
            last_error_code=error_code,
            snapshot_id=None,
            claim_token=None,
            lease_owner=None,
            lease_expires_at=None,
            created_at=str(row["created_at"]),
            started_at=row["started_at"],
            completed_at=now,
            updated_at=now,
        )
        changed = connection.execute(
            """UPDATE wallet_balance_acquisition_runs
               SET status = 'cancelled', next_attempt_at = NULL,
                   last_error_code = ?, claim_token = NULL, lease_owner = NULL,
                   lease_expires_at = NULL, completed_at = ?, updated_at = ?, state_hash = ?
               WHERE run_id = ? AND subject_id = ?
                 AND status IN ('queued', 'retry_wait') AND state_hash = ?""",
            (
                error_code,
                now,
                now,
                state_hash,
                row["run_id"],
                row["subject_id"],
                row["state_hash"],
            ),
        )
        if changed.rowcount != 1:
            raise WalletAcquisitionRunError("wallet_acquisition_target_state_conflict")
        connection.execute(
            """INSERT INTO audit_records(
                audit_id, subject_id, action, actor, payload_json, occurred_at
            ) VALUES (?, ?, 'wallet_balance_acquisition_cancelled', 'system', ?, ?)""",
            (
                new_id("audit"),
                row["subject_id"],
                canonical_json(
                    {
                        "run_id": row["run_id"],
                        "attempt_number": int(row["attempt_count"]),
                        "error_code": error_code,
                        "reason": "registered wallet target is no longer active",
                    }
                ),
                now,
            ),
        )

    def _claim_row(
        self,
        connection: Any,
        run_id: str,
        subject_id: str,
        claim_token: str,
        worker_owner: str,
        now: str,
    ) -> tuple[Any, Any]:
        row = self._load_run_connection(connection, run_id)
        if (
            row["subject_id"] != subject_id
            or row["status"] != "running"
            or row["claim_token"] != claim_token
            or row["lease_owner"] != worker_owner
            or row["lease_expires_at"] is None
            or self._parse_time(row["lease_expires_at"], "lease_expiry")
            <= self._parse_time(now, "claim_time")
        ):
            raise WalletAcquisitionRunError("wallet_acquisition_lease_lost")
        attempt = connection.execute(
            """SELECT * FROM wallet_balance_acquisition_attempts
               WHERE run_id = ? AND claim_token = ? AND status = 'executing'""",
            (run_id, claim_token),
        ).fetchone()
        if attempt is None:
            raise WalletAcquisitionRunError("wallet_acquisition_attempt_missing")
        self._assert_attempt_hash(attempt)
        return row, attempt

    def renew(
        self,
        run_id: str,
        *,
        subject_id: str,
        claim_token: str,
        worker_owner: str,
    ) -> WalletBalanceAcquisitionRunRecord | None:
        validate_subject_id(subject_id)
        token = self._validate_claim_token(claim_token)
        owner = self._validate_worker_owner(worker_owner)
        now = self._now()
        expiry = self._lease_expiry(now)
        with self.database.transaction() as connection:
            row = self._load_run_connection(connection, run_id)
            if (
                row["subject_id"] != subject_id
                or row["status"] != "running"
                or row["claim_token"] != token
                or row["lease_owner"] != owner
                or row["lease_expires_at"] is None
                or self._parse_time(row["lease_expires_at"], "lease_expiry")
                <= self._parse_time(now, "claim_time")
            ):
                return None
            state_hash = self._run_hash(
                run_id=str(row["run_id"]),
                subject_id=str(row["subject_id"]),
                network_id=str(row["network_id"]),
                asset_id=str(row["asset_id"]),
                address_id=str(row["address_id"]),
                idempotency_key=str(row["idempotency_key"]),
                status="running",
                attempt_count=int(row["attempt_count"]),
                max_attempts=int(row["max_attempts"]),
                next_attempt_at=None,
                last_error_code=None,
                snapshot_id=None,
                claim_token=token,
                lease_owner=owner,
                lease_expires_at=expiry,
                created_at=str(row["created_at"]),
                started_at=row["started_at"],
                completed_at=None,
                updated_at=now,
            )
            changed = connection.execute(
                """UPDATE wallet_balance_acquisition_runs
                   SET lease_expires_at = ?, updated_at = ?, state_hash = ?
                   WHERE run_id = ? AND subject_id = ? AND status = 'running'
                     AND claim_token = ? AND lease_owner = ? AND lease_expires_at > ?
                     AND state_hash = ?""",
                (expiry, now, state_hash, run_id, subject_id, token, owner, now, row["state_hash"]),
            )
            if changed.rowcount != 1:
                return None
            return self._run_from_row(self._load_run_connection(connection, run_id))

    def complete_success(
        self,
        run_id: str,
        *,
        subject_id: str,
        claim_token: str,
        worker_owner: str,
        balance: str,
        actor: str = "operator",
    ) -> tuple[WalletBalanceAcquisitionRunRecord, WalletBalanceSnapshotRecord]:
        WalletStore._require_operator(actor, "record a wallet balance acquisition")
        validate_subject_id(subject_id)
        if not isinstance(balance, str):
            raise TypeError("wallet acquisition balance must be text")
        resolved_balance = canonical_balance(balance)
        token = self._validate_claim_token(claim_token)
        owner = self._validate_worker_owner(worker_owner)
        now = self._now()
        snapshot_id = new_id("walletbal")
        with self.database.transaction() as connection:
            row, attempt = self._claim_row(connection, run_id, subject_id, token, owner, now)
            # Rebuild the input from the durable target; caller-supplied target
            # identifiers never enter this completion path.
            proposal = WalletBalanceSnapshotInput(
                asset_id=str(row["asset_id"]),
                address_id=str(row["address_id"]),
                balance=resolved_balance,
                source="evm_rpc",
            )
            snapshot = self.store._record_balance_snapshot_connection(
                connection,
                subject_id,
                proposal,
                actor=actor,
                snapshot_id=snapshot_id,
                created_at=now,
            )
            attempt_hash = self._attempt_hash(
                attempt_id=str(attempt["attempt_id"]),
                run_id=run_id,
                subject_id=subject_id,
                network_id=str(row["network_id"]),
                asset_id=str(row["asset_id"]),
                address_id=str(row["address_id"]),
                attempt_number=int(row["attempt_count"]),
                status="succeeded",
                reserved_request_count=2,
                request_count=2,
                error_code=None,
                snapshot_id=snapshot_id,
                claim_token=token,
                lease_owner=owner,
                started_at=str(attempt["started_at"]),
                completed_at=now,
            )
            changed_attempt = connection.execute(
                """UPDATE wallet_balance_acquisition_attempts
                   SET status = 'succeeded', request_count = 2, error_code = NULL,
                       snapshot_id = ?, state_hash = ?, completed_at = ?
                   WHERE attempt_id = ? AND run_id = ? AND status = 'executing'
                     AND claim_token = ? AND lease_owner = ?""",
                (
                    snapshot_id,
                    attempt_hash,
                    now,
                    attempt["attempt_id"],
                    run_id,
                    token,
                    owner,
                ),
            )
            if changed_attempt.rowcount != 1:
                raise WalletAcquisitionRunError("wallet_acquisition_attempt_lost")
            state_hash = self._run_hash(
                run_id=run_id,
                subject_id=subject_id,
                network_id=str(row["network_id"]),
                asset_id=str(row["asset_id"]),
                address_id=str(row["address_id"]),
                idempotency_key=str(row["idempotency_key"]),
                status="succeeded",
                attempt_count=int(row["attempt_count"]),
                max_attempts=int(row["max_attempts"]),
                next_attempt_at=None,
                last_error_code=None,
                snapshot_id=snapshot_id,
                claim_token=None,
                lease_owner=None,
                lease_expires_at=None,
                created_at=str(row["created_at"]),
                started_at=row["started_at"],
                completed_at=now,
                updated_at=now,
            )
            changed = connection.execute(
                """UPDATE wallet_balance_acquisition_runs
                   SET status = 'succeeded', next_attempt_at = NULL, last_error_code = NULL,
                       snapshot_id = ?, claim_token = NULL, lease_owner = NULL,
                       lease_expires_at = NULL, completed_at = ?, updated_at = ?, state_hash = ?
                   WHERE run_id = ? AND subject_id = ? AND status = 'running'
                     AND claim_token = ? AND lease_owner = ?
                     AND lease_expires_at > ? AND state_hash = ?""",
                (
                    snapshot_id,
                    now,
                    now,
                    state_hash,
                    run_id,
                    subject_id,
                    token,
                    owner,
                    now,
                    row["state_hash"],
                ),
            )
            if changed.rowcount != 1:
                raise WalletAcquisitionRunError("wallet_acquisition_lease_lost")
            connection.execute(
                """INSERT INTO audit_records(
                    audit_id, subject_id, action, actor, payload_json, occurred_at
                ) VALUES (?, ?, 'wallet_balance_acquisition_succeeded', ?, ?, ?)""",
                (
                    new_id("audit"),
                    subject_id,
                    actor.strip(),
                    canonical_json(
                        {
                            "run_id": run_id,
                            "attempt_number": int(row["attempt_count"]),
                            "snapshot_id": snapshot_id,
                        }
                    ),
                    now,
                ),
            )
            return self._run_from_row(self._load_run_connection(connection, run_id)), snapshot

    def fail(
        self,
        run_id: str,
        *,
        subject_id: str,
        claim_token: str,
        worker_owner: str,
        error: BaseException | None = None,
        error_code: str | None = None,
        request_count: int | None = None,
        retryable: bool | None = None,
        actor: str = "operator",
    ) -> WalletBalanceAcquisitionRunRecord:
        WalletStore._require_operator(actor, "record a wallet acquisition failure")
        validate_subject_id(subject_id)
        token = self._validate_claim_token(claim_token)
        owner = self._validate_worker_owner(worker_owner)
        if error_code is None:
            error_code = self._error_code(error or WalletRPCError("wallet_rpc_transport_failed"))
        error_code = self._validate_error_code(error_code)
        should_retry = self._is_retryable(error_code) if retryable is None else retryable
        if not isinstance(should_retry, bool):
            raise ValueError("wallet acquisition retry flag is invalid")
        if request_count is None:
            request_count = getattr(error, "request_count", None)
        if request_count is None:
            request_count = 2
        if (
            isinstance(request_count, bool)
            or not isinstance(request_count, int)
            or not 0 <= request_count <= 2
        ):
            raise ValueError("wallet acquisition request count is invalid")
        now = self._now()
        with self.database.transaction() as connection:
            row, attempt = self._claim_row(connection, run_id, subject_id, token, owner, now)
            attempt_number = int(row["attempt_count"])
            final_retry = should_retry and attempt_number < int(row["max_attempts"])
            final_status = "retry_wait" if final_retry else "failed"
            next_at = self._retry_at(now, attempt_number) if final_retry else None
            completed_at = None if final_retry else now
            attempt_hash = self._attempt_hash(
                attempt_id=str(attempt["attempt_id"]),
                run_id=run_id,
                subject_id=subject_id,
                network_id=str(row["network_id"]),
                asset_id=str(row["asset_id"]),
                address_id=str(row["address_id"]),
                attempt_number=attempt_number,
                status="failed",
                reserved_request_count=2,
                request_count=request_count,
                error_code=error_code,
                snapshot_id=None,
                claim_token=token,
                lease_owner=owner,
                started_at=str(attempt["started_at"]),
                completed_at=now,
            )
            changed_attempt = connection.execute(
                """UPDATE wallet_balance_acquisition_attempts
                   SET status = 'failed', request_count = ?, error_code = ?,
                       snapshot_id = NULL, state_hash = ?, completed_at = ?
                   WHERE attempt_id = ? AND run_id = ? AND status = 'executing'
                     AND claim_token = ? AND lease_owner = ?""",
                (
                    request_count,
                    error_code,
                    attempt_hash,
                    now,
                    attempt["attempt_id"],
                    run_id,
                    token,
                    owner,
                ),
            )
            if changed_attempt.rowcount != 1:
                raise WalletAcquisitionRunError("wallet_acquisition_attempt_lost")
            state_hash = self._run_hash(
                run_id=run_id,
                subject_id=subject_id,
                network_id=str(row["network_id"]),
                asset_id=str(row["asset_id"]),
                address_id=str(row["address_id"]),
                idempotency_key=str(row["idempotency_key"]),
                status=final_status,
                attempt_count=attempt_number,
                max_attempts=int(row["max_attempts"]),
                next_attempt_at=next_at,
                last_error_code=error_code,
                snapshot_id=None,
                claim_token=None,
                lease_owner=None,
                lease_expires_at=None,
                created_at=str(row["created_at"]),
                started_at=row["started_at"],
                completed_at=completed_at,
                updated_at=now,
            )
            changed = connection.execute(
                """UPDATE wallet_balance_acquisition_runs
                   SET status = ?, next_attempt_at = ?, last_error_code = ?, snapshot_id = NULL,
                       claim_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                       completed_at = ?, updated_at = ?, state_hash = ?
                   WHERE run_id = ? AND subject_id = ? AND status = 'running'
                     AND claim_token = ? AND lease_owner = ?
                     AND lease_expires_at > ? AND state_hash = ?""",
                (
                    final_status,
                    next_at,
                    error_code,
                    completed_at,
                    now,
                    state_hash,
                    run_id,
                    subject_id,
                    token,
                    owner,
                    now,
                    row["state_hash"],
                ),
            )
            if changed.rowcount != 1:
                raise WalletAcquisitionRunError("wallet_acquisition_lease_lost")
            connection.execute(
                """INSERT INTO audit_records(
                    audit_id, subject_id, action, actor, payload_json, occurred_at
                ) VALUES (?, ?, 'wallet_balance_acquisition_failed', ?, ?, ?)""",
                (
                    new_id("audit"),
                    subject_id,
                    actor.strip(),
                    canonical_json(
                        {
                            "run_id": run_id,
                            "attempt_number": attempt_number,
                            "error_code": error_code,
                            "retryable": final_retry,
                            "next_attempt_at": next_at,
                        }
                    ),
                    now,
                ),
            )
            return self._run_from_row(self._load_run_connection(connection, run_id))

    def mark_unknown(
        self,
        run_id: str,
        *,
        subject_id: str,
        claim_token: str,
        worker_owner: str,
        error_code: str = "wallet_rpc_outcome_unknown",
        actor: str = "system",
    ) -> WalletBalanceAcquisitionRunRecord:
        """Quarantine an in-flight read whose request count or result is ambiguous."""

        WalletStore._require_operator(actor, "quarantine a wallet acquisition")
        validate_subject_id(subject_id)
        token = self._validate_claim_token(claim_token)
        owner = self._validate_worker_owner(worker_owner)
        resolved_error = self._validate_error_code(error_code)
        now = self._now()
        with self.database.transaction() as connection:
            row, attempt = self._claim_row(connection, run_id, subject_id, token, owner, now)
            attempt_number = int(row["attempt_count"])
            attempt_hash = self._attempt_hash(
                attempt_id=str(attempt["attempt_id"]),
                run_id=run_id,
                subject_id=subject_id,
                network_id=str(row["network_id"]),
                asset_id=str(row["asset_id"]),
                address_id=str(row["address_id"]),
                attempt_number=attempt_number,
                status="unknown",
                reserved_request_count=2,
                request_count=None,
                error_code=resolved_error,
                snapshot_id=None,
                claim_token=token,
                lease_owner=owner,
                started_at=str(attempt["started_at"]),
                completed_at=now,
            )
            changed_attempt = connection.execute(
                """UPDATE wallet_balance_acquisition_attempts
                   SET status = 'unknown', request_count = NULL, error_code = ?,
                       snapshot_id = NULL, state_hash = ?, completed_at = ?
                   WHERE attempt_id = ? AND run_id = ? AND status = 'executing'
                     AND claim_token = ? AND lease_owner = ?""",
                (
                    resolved_error,
                    attempt_hash,
                    now,
                    attempt["attempt_id"],
                    run_id,
                    token,
                    owner,
                ),
            )
            if changed_attempt.rowcount != 1:
                raise WalletAcquisitionRunError("wallet_acquisition_attempt_lost")
            state_hash = self._run_hash(
                run_id=run_id,
                subject_id=subject_id,
                network_id=str(row["network_id"]),
                asset_id=str(row["asset_id"]),
                address_id=str(row["address_id"]),
                idempotency_key=str(row["idempotency_key"]),
                status="unknown",
                attempt_count=attempt_number,
                max_attempts=int(row["max_attempts"]),
                next_attempt_at=None,
                last_error_code=resolved_error,
                snapshot_id=None,
                claim_token=None,
                lease_owner=None,
                lease_expires_at=None,
                created_at=str(row["created_at"]),
                started_at=row["started_at"],
                completed_at=now,
                updated_at=now,
            )
            changed = connection.execute(
                """UPDATE wallet_balance_acquisition_runs
                   SET status = 'unknown', next_attempt_at = NULL,
                       last_error_code = ?, snapshot_id = NULL, claim_token = NULL,
                       lease_owner = NULL, lease_expires_at = NULL,
                       completed_at = ?, updated_at = ?, state_hash = ?
                   WHERE run_id = ? AND subject_id = ? AND status = 'running'
                     AND claim_token = ? AND lease_owner = ?
                     AND lease_expires_at > ? AND state_hash = ?""",
                (
                    resolved_error,
                    now,
                    now,
                    state_hash,
                    run_id,
                    subject_id,
                    token,
                    owner,
                    now,
                    row["state_hash"],
                ),
            )
            if changed.rowcount != 1:
                raise WalletAcquisitionRunError("wallet_acquisition_lease_lost")
            connection.execute(
                """INSERT INTO audit_records(
                    audit_id, subject_id, action, actor, payload_json, occurred_at
                ) VALUES (?, ?, 'wallet_balance_acquisition_unknown', ?, ?, ?)""",
                (
                    new_id("audit"),
                    subject_id,
                    actor.strip(),
                    canonical_json(
                        {
                            "run_id": run_id,
                            "attempt_number": attempt_number,
                            "error_code": resolved_error,
                        }
                    ),
                    now,
                ),
            )
            return self._run_from_row(self._load_run_connection(connection, run_id))

    def retry_unknown(
        self,
        run_id: str,
        *,
        subject_id: str,
        actor: str,
        reason: str,
        not_before: str | None = None,
    ) -> WalletBalanceAcquisitionRunRecord:
        """Explicitly requeue one outcome-unknown read with operator evidence."""

        WalletStore._require_operator(actor, "retry an unknown wallet acquisition")
        validate_subject_id(subject_id)
        resolved_reason = self._validate_reason(reason)
        now = self._now()
        due_at = now if not_before is None else self._canonical_time(not_before, "not_before")
        try:
            with self.database.transaction() as connection:
                row = self._load_run_connection(connection, run_id)
                if row["subject_id"] != subject_id:
                    raise NotFoundError(f"wallet acquisition run not found: {run_id}")
                if row["status"] != "unknown":
                    raise WalletAcquisitionRunError("wallet_acquisition_unknown_retry_not_allowed")
                if int(row["attempt_count"]) >= int(row["max_attempts"]):
                    raise WalletAcquisitionRunError("wallet_acquisition_attempt_limit_reached")
                previous_error = self._validate_error_code(row["last_error_code"])
                state_hash = self._run_hash(
                    run_id=run_id,
                    subject_id=subject_id,
                    network_id=str(row["network_id"]),
                    asset_id=str(row["asset_id"]),
                    address_id=str(row["address_id"]),
                    idempotency_key=str(row["idempotency_key"]),
                    status="queued",
                    attempt_count=int(row["attempt_count"]),
                    max_attempts=int(row["max_attempts"]),
                    next_attempt_at=due_at,
                    last_error_code=None,
                    snapshot_id=None,
                    claim_token=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    created_at=str(row["created_at"]),
                    started_at=row["started_at"],
                    completed_at=None,
                    updated_at=now,
                )
                changed = connection.execute(
                    """UPDATE wallet_balance_acquisition_runs
                       SET status = 'queued', next_attempt_at = ?, last_error_code = NULL,
                           snapshot_id = NULL, claim_token = NULL, lease_owner = NULL,
                           lease_expires_at = NULL, completed_at = NULL,
                           updated_at = ?, state_hash = ?
                       WHERE run_id = ? AND subject_id = ? AND status = 'unknown'
                         AND state_hash = ?""",
                    (due_at, now, state_hash, run_id, subject_id, row["state_hash"]),
                )
                if changed.rowcount != 1:
                    raise WalletAcquisitionRunError("wallet_acquisition_unknown_retry_conflict")
                connection.execute(
                    """INSERT INTO audit_records(
                        audit_id, subject_id, action, actor, payload_json, occurred_at
                    ) VALUES (?, ?, 'wallet_balance_acquisition_retried', ?, ?, ?)""",
                    (
                        new_id("audit"),
                        subject_id,
                        actor.strip(),
                        canonical_json(
                            {
                                "run_id": run_id,
                                "attempt_number": int(row["attempt_count"]),
                                "previous_error_code": previous_error,
                                "reason": resolved_reason,
                                "next_attempt_at": due_at,
                            }
                        ),
                        now,
                    ),
                )
                return self._run_from_row(self._load_run_connection(connection, run_id))
        except sqlite3.IntegrityError as error:
            message = str(error)
            if (
                "UNIQUE constraint failed" not in message
                or "wallet_balance_acquisition_runs.subject_id" not in message
            ):
                raise
            raise WalletAcquisitionConflictError(
                "wallet acquisition target already has an active run"
            ) from error

    def cancel(
        self,
        run_id: str,
        *,
        subject_id: str,
        actor: str,
        reason: str,
    ) -> WalletBalanceAcquisitionRunRecord:
        """Cancel work that has not started or is waiting for a known retry."""

        WalletStore._require_operator(actor, "cancel a wallet acquisition")
        validate_subject_id(subject_id)
        resolved_reason = self._validate_reason(reason)
        error_code = "wallet_rpc_operator_cancelled"
        now = self._now()
        with self.database.transaction() as connection:
            row = self._load_run_connection(connection, run_id)
            if row["subject_id"] != subject_id:
                raise NotFoundError(f"wallet acquisition run not found: {run_id}")
            if row["status"] not in {"queued", "retry_wait"}:
                raise WalletAcquisitionRunError("wallet_acquisition_cancel_not_allowed")
            state_hash = self._run_hash(
                run_id=run_id,
                subject_id=subject_id,
                network_id=str(row["network_id"]),
                asset_id=str(row["asset_id"]),
                address_id=str(row["address_id"]),
                idempotency_key=str(row["idempotency_key"]),
                status="cancelled",
                attempt_count=int(row["attempt_count"]),
                max_attempts=int(row["max_attempts"]),
                next_attempt_at=None,
                last_error_code=error_code,
                snapshot_id=None,
                claim_token=None,
                lease_owner=None,
                lease_expires_at=None,
                created_at=str(row["created_at"]),
                started_at=row["started_at"],
                completed_at=now,
                updated_at=now,
            )
            changed = connection.execute(
                """UPDATE wallet_balance_acquisition_runs
                   SET status = 'cancelled', next_attempt_at = NULL,
                       last_error_code = ?, snapshot_id = NULL,
                       claim_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                       completed_at = ?, updated_at = ?, state_hash = ?
                   WHERE run_id = ? AND subject_id = ?
                     AND status IN ('queued', 'retry_wait') AND state_hash = ?""",
                (
                    error_code,
                    now,
                    now,
                    state_hash,
                    run_id,
                    subject_id,
                    row["state_hash"],
                ),
            )
            if changed.rowcount != 1:
                raise WalletAcquisitionRunError("wallet_acquisition_cancel_conflict")
            connection.execute(
                """INSERT INTO audit_records(
                    audit_id, subject_id, action, actor, payload_json, occurred_at
                ) VALUES (?, ?, 'wallet_balance_acquisition_cancelled', ?, ?, ?)""",
                (
                    new_id("audit"),
                    subject_id,
                    actor.strip(),
                    canonical_json(
                        {
                            "run_id": run_id,
                            "attempt_number": int(row["attempt_count"]),
                            "error_code": error_code,
                            "reason": resolved_reason,
                        }
                    ),
                    now,
                ),
            )
            return self._run_from_row(self._load_run_connection(connection, run_id))

    def verify_integrity(self, subject_id: str) -> dict[str, int]:
        """Verify durable acquisition state, attempts, references, and audits."""

        validate_subject_id(subject_id)
        with self.database.read_transaction() as connection:
            return self._verify_integrity_connection(connection, subject_id)

    @classmethod
    def _integrity_time(cls, value: object, context: str) -> str:
        if not validate_timestamp(value):
            raise IntegrityError(f"wallet acquisition {context} is invalid")
        try:
            canonical = cls._canonical_time(value, context)
        except WalletAcquisitionRunError as error:
            raise IntegrityError(f"wallet acquisition {context} is invalid") from error
        if canonical != value:
            raise IntegrityError(f"wallet acquisition {context} is non-canonical")
        return canonical

    @staticmethod
    def _integrity_identifier(value: object, context: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise IntegrityError(f"wallet acquisition {context} is invalid")
        return value

    @classmethod
    def _verified_run_values(cls, row: Any, subject_id: str) -> dict[str, Any]:
        run_id = cls._integrity_identifier(row["run_id"], "run identifier")
        try:
            validate_subject_id(row["subject_id"])
            idempotency_key = cls._validate_idempotency_key(row["idempotency_key"])
            attempt_count = strict_int(row["attempt_count"])
            max_attempts = strict_int(row["max_attempts"])
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"wallet acquisition run state is invalid: {run_id}") from error
        network_id = cls._integrity_identifier(row["network_id"], "network identifier")
        asset_id = cls._integrity_identifier(row["asset_id"], "asset identifier")
        address_id = cls._integrity_identifier(row["address_id"], "address identifier")
        status = row["status"]
        if (
            row["subject_id"] != subject_id
            or row["network_subject_id"] != subject_id
            or row["asset_subject_id"] != subject_id
            or row["address_subject_id"] != subject_id
            or row["asset_network_id"] != network_id
            or row["address_network_id"] != network_id
            or status not in ACQUISITION_RUN_STATES
            or not 0 <= attempt_count <= max_attempts <= WALLET_ACQUISITION_MAX_ATTEMPTS
        ):
            raise IntegrityError(f"wallet acquisition run ownership or state is invalid: {run_id}")
        created_at = cls._integrity_time(row["created_at"], "run creation time")
        updated_at = cls._integrity_time(row["updated_at"], "run update time")
        started_at = (
            None
            if row["started_at"] is None
            else cls._integrity_time(row["started_at"], "run start time")
        )
        completed_at = (
            None
            if row["completed_at"] is None
            else cls._integrity_time(row["completed_at"], "run completion time")
        )
        next_attempt_at = (
            None
            if row["next_attempt_at"] is None
            else cls._integrity_time(row["next_attempt_at"], "next attempt time")
        )
        lease_expires_at = (
            None
            if row["lease_expires_at"] is None
            else cls._integrity_time(row["lease_expires_at"], "lease expiry time")
        )
        last_error_code = row["last_error_code"]
        if last_error_code is not None:
            try:
                last_error_code = cls._validate_error_code(last_error_code)
            except ValueError as error:
                raise IntegrityError(
                    f"wallet acquisition run error is invalid: {run_id}"
                ) from error
        if status == "cancelled" and last_error_code not in _ACQUISITION_CANCELLATION_ERROR_CODES:
            raise IntegrityError(f"wallet acquisition cancellation code is invalid: {run_id}")
        snapshot_id = row["snapshot_id"]
        if snapshot_id is not None:
            snapshot_id = cls._integrity_identifier(snapshot_id, "snapshot identifier")
        claim_token = row["claim_token"]
        lease_owner = row["lease_owner"]
        if claim_token is not None:
            claim_token = cls._integrity_identifier(claim_token, "claim token")
        if lease_owner is not None:
            lease_owner = cls._integrity_identifier(lease_owner, "lease owner")
        no_claim = claim_token is None and lease_owner is None and lease_expires_at is None
        no_result = snapshot_id is None
        shape_valid = False
        if status == "queued":
            shape_valid = (
                attempt_count < max_attempts
                and next_attempt_at is not None
                and last_error_code is None
                and no_result
                and no_claim
                and completed_at is None
            )
        elif status == "running":
            shape_valid = (
                attempt_count >= 1
                and next_attempt_at is None
                and last_error_code is None
                and no_result
                and claim_token is not None
                and lease_owner is not None
                and lease_expires_at is not None
                and started_at is not None
                and completed_at is None
            )
        elif status == "retry_wait":
            shape_valid = (
                1 <= attempt_count < max_attempts
                and next_attempt_at is not None
                and last_error_code is not None
                and no_result
                and no_claim
                and started_at is not None
                and completed_at is None
            )
        elif status == "succeeded":
            shape_valid = (
                attempt_count >= 1
                and next_attempt_at is None
                and last_error_code is None
                and snapshot_id is not None
                and no_claim
                and started_at is not None
                and completed_at is not None
            )
        elif status in {"failed", "unknown"}:
            shape_valid = (
                attempt_count >= 1
                and next_attempt_at is None
                and last_error_code is not None
                and no_result
                and no_claim
                and started_at is not None
                and completed_at is not None
            )
        elif status == "cancelled":
            shape_valid = (
                next_attempt_at is None
                and last_error_code in _ACQUISITION_CANCELLATION_ERROR_CODES
                and no_result
                and no_claim
                and completed_at is not None
                and ((attempt_count == 0) == (started_at is None))
            )
        if (
            not shape_valid
            or datetime.fromisoformat(updated_at) < datetime.fromisoformat(created_at)
            or (
                started_at is not None
                and datetime.fromisoformat(started_at) < datetime.fromisoformat(created_at)
            )
            or (
                completed_at is not None
                and datetime.fromisoformat(completed_at)
                < datetime.fromisoformat(started_at or created_at)
            )
        ):
            raise IntegrityError(f"wallet acquisition run lifecycle is invalid: {run_id}")
        expected_hash = cls._run_hash(
            run_id=run_id,
            subject_id=subject_id,
            network_id=network_id,
            asset_id=asset_id,
            address_id=address_id,
            idempotency_key=idempotency_key,
            status=status,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            next_attempt_at=next_attempt_at,
            last_error_code=last_error_code,
            snapshot_id=snapshot_id,
            claim_token=claim_token,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
            created_at=created_at,
            started_at=started_at,
            completed_at=completed_at,
            updated_at=updated_at,
        )
        if row["state_hash"] != expected_hash:
            raise IntegrityError(f"wallet acquisition run hash mismatch: {run_id}")
        return {
            "run_id": run_id,
            "network_id": network_id,
            "asset_id": asset_id,
            "address_id": address_id,
            "idempotency_key": idempotency_key,
            "status": status,
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "next_attempt_at": next_attempt_at,
            "last_error_code": last_error_code,
            "snapshot_id": snapshot_id,
            "claim_token": claim_token,
            "lease_owner": lease_owner,
            "lease_expires_at": lease_expires_at,
            "created_at": created_at,
            "started_at": started_at,
            "completed_at": completed_at,
            "updated_at": updated_at,
            "created_audit_id": row["created_audit_id"],
        }

    @classmethod
    def _verified_attempt_values(
        cls,
        row: Any,
        run: dict[str, Any],
        expected_number: int,
        subject_id: str,
    ) -> dict[str, Any]:
        attempt_id = cls._integrity_identifier(row["attempt_id"], "attempt identifier")
        try:
            attempt_number = strict_int(row["attempt_number"])
            reserved = strict_int(row["reserved_request_count"])
            request_count = (
                None if row["request_count"] is None else strict_int(row["request_count"])
            )
        except (TypeError, ValueError) as error:
            raise IntegrityError(
                f"wallet acquisition attempt state is invalid: {attempt_id}"
            ) from error
        status = row["status"]
        claim_token = cls._integrity_identifier(row["claim_token"], "attempt claim token")
        lease_owner = cls._integrity_identifier(row["lease_owner"], "attempt lease owner")
        if (
            row["run_id"] != run["run_id"]
            or row["subject_id"] != subject_id
            or row["network_id"] != run["network_id"]
            or row["asset_id"] != run["asset_id"]
            or row["address_id"] != run["address_id"]
            or attempt_number != expected_number
            or reserved != 2
            or status not in ACQUISITION_ATTEMPT_STATES
        ):
            raise IntegrityError(
                f"wallet acquisition attempt ownership or state is invalid: {attempt_id}"
            )
        started_at = cls._integrity_time(row["started_at"], "attempt start time")
        completed_at = (
            None
            if row["completed_at"] is None
            else cls._integrity_time(row["completed_at"], "attempt completion time")
        )
        error_code = row["error_code"]
        if error_code is not None:
            try:
                error_code = cls._validate_error_code(error_code)
            except ValueError as error:
                raise IntegrityError(
                    f"wallet acquisition attempt error is invalid: {attempt_id}"
                ) from error
        snapshot_id = row["snapshot_id"]
        if snapshot_id is not None:
            snapshot_id = cls._integrity_identifier(snapshot_id, "attempt snapshot identifier")
        shape_valid = (
            (
                status == "executing"
                and request_count is None
                and error_code is None
                and snapshot_id is None
                and completed_at is None
            )
            or (
                status == "succeeded"
                and request_count == 2
                and error_code is None
                and snapshot_id is not None
                and completed_at is not None
            )
            or (
                status == "failed"
                and request_count is not None
                and 0 <= request_count <= 2
                and error_code is not None
                and snapshot_id is None
                and completed_at is not None
            )
            or (
                status == "unknown"
                and request_count is None
                and error_code is not None
                and snapshot_id is None
                and completed_at is not None
            )
        )
        if (
            not shape_valid
            or datetime.fromisoformat(started_at) < datetime.fromisoformat(run["created_at"])
            or (
                completed_at is not None
                and datetime.fromisoformat(completed_at) < datetime.fromisoformat(started_at)
            )
        ):
            raise IntegrityError(f"wallet acquisition attempt lifecycle is invalid: {attempt_id}")
        if status == "succeeded":
            if (
                row["snapshot_subject_id"] != subject_id
                or row["snapshot_network_id"] != run["network_id"]
                or row["snapshot_asset_id"] != run["asset_id"]
                or row["snapshot_address_id"] != run["address_id"]
                or row["snapshot_source"] != "evm_rpc"
            ):
                raise IntegrityError(
                    f"wallet acquisition attempt snapshot is invalid: {attempt_id}"
                )
        elif any(
            row[field] is not None
            for field in (
                "snapshot_subject_id",
                "snapshot_network_id",
                "snapshot_asset_id",
                "snapshot_address_id",
                "snapshot_source",
            )
        ):
            raise IntegrityError(
                f"wallet acquisition attempt has unexpected snapshot: {attempt_id}"
            )
        expected_hash = cls._attempt_hash(
            attempt_id=attempt_id,
            run_id=run["run_id"],
            subject_id=subject_id,
            network_id=run["network_id"],
            asset_id=run["asset_id"],
            address_id=run["address_id"],
            attempt_number=attempt_number,
            status=status,
            reserved_request_count=reserved,
            request_count=request_count,
            error_code=error_code,
            snapshot_id=snapshot_id,
            claim_token=claim_token,
            lease_owner=lease_owner,
            started_at=started_at,
            completed_at=completed_at,
        )
        if row["state_hash"] != expected_hash:
            raise IntegrityError(f"wallet acquisition attempt hash mismatch: {attempt_id}")
        return {
            "attempt_id": attempt_id,
            "attempt_number": attempt_number,
            "status": status,
            "request_count": request_count,
            "error_code": error_code,
            "snapshot_id": snapshot_id,
            "claim_token": claim_token,
            "lease_owner": lease_owner,
            "started_at": started_at,
            "completed_at": completed_at,
        }

    @classmethod
    def _verify_run_attempts(
        cls, connection: Any, run: dict[str, Any], subject_id: str
    ) -> builtins.list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT attempt.*,
                      snapshot.subject_id AS snapshot_subject_id,
                      snapshot.network_id AS snapshot_network_id,
                      snapshot.asset_id AS snapshot_asset_id,
                      snapshot.address_id AS snapshot_address_id,
                      snapshot.source AS snapshot_source
               FROM wallet_balance_acquisition_attempts attempt
               LEFT JOIN wallet_balance_snapshots snapshot
                 ON snapshot.snapshot_id = attempt.snapshot_id
               WHERE attempt.run_id = ?
               ORDER BY attempt.attempt_number, attempt.attempt_id""",
            (run["run_id"],),
        ).fetchall()
        if len(rows) != run["attempt_count"] or len(rows) > WALLET_ACQUISITION_MAX_ATTEMPTS:
            raise IntegrityError(
                f"wallet acquisition attempt history is incomplete: {run['run_id']}"
            )
        attempts = [
            cls._verified_attempt_values(row, run, number, subject_id)
            for number, row in enumerate(rows, start=1)
        ]
        if not attempts:
            if run["status"] not in {"queued", "cancelled"} or run["started_at"] is not None:
                raise IntegrityError(
                    f"wallet acquisition empty history is invalid: {run['run_id']}"
                )
            return attempts
        if attempts[0]["started_at"] != run["started_at"]:
            raise IntegrityError(f"wallet acquisition start evidence is invalid: {run['run_id']}")
        for previous, current in pairwise(attempts):
            if previous["status"] not in {"failed", "unknown"}:
                raise IntegrityError(
                    f"wallet acquisition retry history is invalid: {run['run_id']}"
                )
            if previous["completed_at"] is None or datetime.fromisoformat(
                current["started_at"]
            ) < datetime.fromisoformat(previous["completed_at"]):
                raise IntegrityError(
                    f"wallet acquisition attempt ordering is invalid: {run['run_id']}"
                )
        last = attempts[-1]
        expected_last = {
            "queued": "unknown",
            "running": "executing",
            "retry_wait": "failed",
            "succeeded": "succeeded",
            "failed": "failed",
            "unknown": "unknown",
        }.get(run["status"])
        if expected_last is not None and last["status"] != expected_last:
            raise IntegrityError(
                f"wallet acquisition terminal evidence is invalid: {run['run_id']}"
            )
        if run["status"] == "cancelled" and last["status"] not in {"failed", "unknown"}:
            raise IntegrityError(
                f"wallet acquisition cancellation evidence is invalid: {run['run_id']}"
            )
        if run["status"] == "running" and (
            last["claim_token"] != run["claim_token"] or last["lease_owner"] != run["lease_owner"]
        ):
            raise IntegrityError(f"wallet acquisition active claim is invalid: {run['run_id']}")
        elif run["status"] in {"retry_wait", "failed", "unknown"} and (
            last["error_code"] != run["last_error_code"]
        ):
            raise IntegrityError(f"wallet acquisition error evidence is invalid: {run['run_id']}")
        elif run["status"] == "succeeded" and (
            last["snapshot_id"] != run["snapshot_id"] or last["completed_at"] != run["completed_at"]
        ):
            raise IntegrityError(f"wallet acquisition success evidence is invalid: {run['run_id']}")
        if run["status"] in {"failed", "unknown"} and (last["completed_at"] != run["completed_at"]):
            raise IntegrityError(
                f"wallet acquisition completion evidence is invalid: {run['run_id']}"
            )
        return attempts

    @classmethod
    def _verify_run_audits(
        cls,
        connection: Any,
        run: dict[str, Any],
        attempts: builtins.list[dict[str, Any]],
        subject_id: str,
    ) -> None:
        placeholders = ",".join("?" for _ in _ACQUISITION_AUDIT_ACTIONS)
        rows = connection.execute(
            "SELECT * FROM audit_records WHERE subject_id = ? AND action IN ("
            + placeholders
            + ") AND json_valid(payload_json) "
            "AND json_extract(payload_json, '$.run_id') = ? "
            "ORDER BY occurred_at, audit_id",
            (subject_id, *sorted(_ACQUISITION_AUDIT_ACTIONS), run["run_id"]),
        ).fetchall()
        events: builtins.list[tuple[Any, dict[str, Any], str]] = []
        for row in rows:
            audit_id = cls._integrity_identifier(row["audit_id"], "audit identifier")
            actor = row["actor"]
            if not isinstance(actor, str) or not actor.strip() or actor.strip() == "subject":
                raise IntegrityError(f"wallet acquisition audit actor is invalid: {audit_id}")
            occurred_at = cls._integrity_time(row["occurred_at"], "audit occurrence time")
            try:
                payload = strict_json_loads(row["payload_json"])
            except (TypeError, ValueError) as error:
                raise IntegrityError(
                    f"wallet acquisition audit payload is invalid: {audit_id}"
                ) from error
            if (
                not isinstance(payload, dict)
                or canonical_json(payload) != row["payload_json"]
                or payload.get("run_id") != run["run_id"]
            ):
                raise IntegrityError(f"wallet acquisition audit payload is invalid: {audit_id}")
            events.append((row, payload, occurred_at))

        consumed: set[str] = set()

        def unique(
            action: str, attempt_number: int | None = None
        ) -> tuple[Any, dict[str, Any], str]:
            matching = []
            for event in events:
                row, payload, _occurred_at = event
                if row["action"] != action:
                    continue
                if attempt_number is not None and payload.get("attempt_number") != attempt_number:
                    continue
                matching.append(event)
            if len(matching) != 1:
                raise IntegrityError(
                    f"wallet acquisition audit history is invalid: {run['run_id']}"
                )
            consumed.add(str(matching[0][0]["audit_id"]))
            return matching[0]

        queued_row, queued_payload, queued_at = unique("wallet_balance_acquisition_queued")
        if (
            queued_row["audit_id"] != run["created_audit_id"]
            or queued_at != run["created_at"]
            or queued_payload
            != {
                "run_id": run["run_id"],
                "network_id": run["network_id"],
                "asset_id": run["asset_id"],
                "address_id": run["address_id"],
                "idempotency_key": run["idempotency_key"],
            }
        ):
            raise IntegrityError(f"wallet acquisition creation audit is invalid: {run['run_id']}")

        for attempt in attempts:
            number = int(attempt["attempt_number"])
            is_last = number == len(attempts)
            if attempt["status"] == "failed":
                _row, payload, occurred_at = unique("wallet_balance_acquisition_failed", number)
                retryable = (not is_last) or (run["status"] in {"retry_wait", "cancelled"})
                expected_keys = {
                    "run_id",
                    "attempt_number",
                    "error_code",
                    "retryable",
                    "next_attempt_at",
                }
                next_at = payload.get("next_attempt_at")
                if retryable:
                    next_at = cls._integrity_time(next_at, "retry audit time")
                    if (
                        run["status"] == "retry_wait"
                        and is_last
                        and next_at != run["next_attempt_at"]
                    ):
                        raise IntegrityError(
                            f"wallet acquisition retry audit is invalid: {run['run_id']}"
                        )
                if (
                    set(payload) != expected_keys
                    or payload["run_id"] != run["run_id"]
                    or payload["attempt_number"] != number
                    or payload["error_code"] != attempt["error_code"]
                    or payload["retryable"] is not retryable
                    or (not retryable and next_at is not None)
                    or occurred_at != attempt["completed_at"]
                ):
                    raise IntegrityError(
                        f"wallet acquisition failure audit is invalid: {run['run_id']}"
                    )
            elif attempt["status"] == "unknown":
                _row, payload, occurred_at = unique("wallet_balance_acquisition_unknown", number)
                if (
                    payload
                    != {
                        "run_id": run["run_id"],
                        "attempt_number": number,
                        "error_code": attempt["error_code"],
                    }
                    or occurred_at != attempt["completed_at"]
                ):
                    raise IntegrityError(
                        f"wallet acquisition unknown audit is invalid: {run['run_id']}"
                    )
            elif attempt["status"] == "succeeded":
                _row, payload, occurred_at = unique("wallet_balance_acquisition_succeeded", number)
                if (
                    payload
                    != {
                        "run_id": run["run_id"],
                        "attempt_number": number,
                        "snapshot_id": attempt["snapshot_id"],
                    }
                    or occurred_at != attempt["completed_at"]
                ):
                    raise IntegrityError(
                        f"wallet acquisition success audit is invalid: {run['run_id']}"
                    )

            retried_unknown = attempt["status"] == "unknown" and (
                not is_last or run["status"] in {"queued", "cancelled"}
            )
            if retried_unknown:
                _row, payload, occurred_at = unique("wallet_balance_acquisition_retried", number)
                next_at = cls._integrity_time(
                    payload.get("next_attempt_at"), "unknown retry audit time"
                )
                reason = payload.get("reason")
                if (
                    set(payload)
                    != {
                        "run_id",
                        "attempt_number",
                        "previous_error_code",
                        "reason",
                        "next_attempt_at",
                    }
                    or payload["previous_error_code"] != attempt["error_code"]
                    or not isinstance(reason, str)
                    or not reason.strip()
                    or len(reason) > 2_000
                    or datetime.fromisoformat(occurred_at)
                    < datetime.fromisoformat(str(attempt["completed_at"]))
                    or (
                        not is_last
                        and datetime.fromisoformat(occurred_at)
                        > datetime.fromisoformat(attempts[number]["started_at"])
                    )
                    or (is_last and run["status"] == "queued" and next_at != run["next_attempt_at"])
                ):
                    raise IntegrityError(
                        f"wallet acquisition unknown retry audit is invalid: {run['run_id']}"
                    )

        if run["status"] == "cancelled":
            _row, payload, occurred_at = unique("wallet_balance_acquisition_cancelled")
            reason = payload.get("reason")
            if (
                set(payload) != {"run_id", "attempt_number", "error_code", "reason"}
                or payload["attempt_number"] != run["attempt_count"]
                or payload["error_code"] not in _ACQUISITION_CANCELLATION_ERROR_CODES
                or not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 2_000
                or occurred_at != run["completed_at"]
            ):
                raise IntegrityError(
                    f"wallet acquisition cancellation audit is invalid: {run['run_id']}"
                )
        if len(consumed) != len(events):
            raise IntegrityError(
                f"wallet acquisition has unexpected audit evidence: {run['run_id']}"
            )

    @classmethod
    def _verify_integrity_connection(cls, connection: Any, subject_id: str) -> dict[str, int]:
        orphan_attempt = connection.execute(
            """SELECT attempt.attempt_id
               FROM wallet_balance_acquisition_attempts attempt
               LEFT JOIN wallet_balance_acquisition_runs run
                 ON run.run_id = attempt.run_id
               WHERE (attempt.subject_id = ? OR run.subject_id = ?)
                 AND (
                     run.run_id IS NULL
                     OR attempt.subject_id != run.subject_id
                     OR attempt.network_id != run.network_id
                     OR attempt.asset_id != run.asset_id
                     OR attempt.address_id != run.address_id
                 )
               LIMIT 1""",
            (subject_id, subject_id),
        ).fetchone()
        if orphan_attempt is not None:
            raise IntegrityError(
                "wallet acquisition attempt ownership is invalid: "
                + str(orphan_attempt["attempt_id"])
            )

        placeholders = ",".join("?" for _ in _ACQUISITION_AUDIT_ACTIONS)
        orphan_audit = connection.execute(
            "SELECT audit.audit_id FROM audit_records audit "
            "LEFT JOIN wallet_balance_acquisition_runs run ON run.run_id = CASE "
            "WHEN typeof(audit.payload_json) = 'text' AND json_valid(audit.payload_json) "
            "THEN json_extract(audit.payload_json, '$.run_id') END "
            "WHERE audit.subject_id = ? AND audit.action IN ("
            + placeholders
            + ") AND (run.run_id IS NULL OR run.subject_id != ?) LIMIT 1",
            (subject_id, *sorted(_ACQUISITION_AUDIT_ACTIONS), subject_id),
        ).fetchone()
        if orphan_audit is not None:
            raise IntegrityError(
                "wallet acquisition audit ownership is invalid: " + str(orphan_audit["audit_id"])
            )

        cursor = connection.execute(
            """SELECT run.*,
                      network.subject_id AS network_subject_id,
                      asset.subject_id AS asset_subject_id,
                      asset.network_id AS asset_network_id,
                      address.subject_id AS address_subject_id,
                      address.network_id AS address_network_id,
                      snapshot.subject_id AS run_snapshot_subject_id,
                      snapshot.network_id AS run_snapshot_network_id,
                      snapshot.asset_id AS run_snapshot_asset_id,
                      snapshot.address_id AS run_snapshot_address_id,
                      snapshot.source AS run_snapshot_source
               FROM wallet_balance_acquisition_runs run
               LEFT JOIN wallet_networks network ON network.network_id = run.network_id
               LEFT JOIN wallet_assets asset ON asset.asset_id = run.asset_id
               LEFT JOIN wallet_addresses address ON address.address_id = run.address_id
               LEFT JOIN wallet_balance_snapshots snapshot
                 ON snapshot.snapshot_id = run.snapshot_id
               WHERE run.subject_id = ? OR network.subject_id = ?
                  OR asset.subject_id = ? OR address.subject_id = ?
                  OR snapshot.subject_id = ?
               ORDER BY run.created_at, run.run_id""",
            (subject_id, subject_id, subject_id, subject_id, subject_id),
        )
        run_count = 0
        attempt_count = 0
        for row in cursor:
            run = cls._verified_run_values(row, subject_id)
            if run["status"] == "succeeded":
                if (
                    row["run_snapshot_subject_id"] != subject_id
                    or row["run_snapshot_network_id"] != run["network_id"]
                    or row["run_snapshot_asset_id"] != run["asset_id"]
                    or row["run_snapshot_address_id"] != run["address_id"]
                    or row["run_snapshot_source"] != "evm_rpc"
                ):
                    raise IntegrityError(
                        f"wallet acquisition run snapshot is invalid: {run['run_id']}"
                    )
            elif any(
                row[field] is not None
                for field in (
                    "run_snapshot_subject_id",
                    "run_snapshot_network_id",
                    "run_snapshot_asset_id",
                    "run_snapshot_address_id",
                    "run_snapshot_source",
                )
            ):
                raise IntegrityError(
                    f"wallet acquisition run has unexpected snapshot: {run['run_id']}"
                )
            attempts = cls._verify_run_attempts(connection, run, subject_id)
            cls._verify_run_audits(connection, run, attempts, subject_id)
            run_count += 1
            attempt_count += len(attempts)
        return {
            "wallet_balance_acquisition_runs": run_count,
            "wallet_balance_acquisition_attempts": attempt_count,
        }


class WalletBalanceAcquisitionRunner:
    """Run durable claims through the fixed read-only wallet RPC boundary."""

    def __init__(
        self,
        ledger: WalletBalanceAcquisitionLedger,
        *,
        acquirer: WalletRPCBalanceAcquirer | None = None,
        worker_owner: str | None = None,
    ):
        if not isinstance(ledger, WalletBalanceAcquisitionLedger):
            raise TypeError("wallet acquisition ledger is invalid")
        if acquirer is not None and not isinstance(acquirer, WalletRPCBalanceAcquirer):
            raise TypeError("wallet balance acquirer is invalid")
        resolved_acquirer = acquirer or WalletRPCBalanceAcquirer(ledger.store)
        if resolved_acquirer.store.database.path != ledger.database.path:
            if acquirer is None:
                resolved_acquirer.close()
            raise ValueError("wallet acquisition database does not match RPC store")
        self.ledger = ledger
        self.acquirer = resolved_acquirer
        self.worker_owner = ledger._validate_worker_owner(worker_owner or new_id("wallet-worker"))
        self._owns_acquirer = acquirer is None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        if self._owns_acquirer:
            self.acquirer.close()

    def _after_claim_loss(self, subject_id: str, run_id: str) -> WalletBalanceAcquisitionRunRecord:
        self.ledger.recover_expired(subject_id, limit=1)
        return self.ledger.get(run_id, subject_id=subject_id)

    def run_once(
        self, subject_id: str, *, actor: str = "system"
    ) -> WalletBalanceAcquisitionRunRecord | None:
        WalletStore._require_operator(actor, "run a wallet balance acquisition")
        validate_subject_id(subject_id)
        if not self.ledger._slots.acquire(blocking=False):
            return None
        try:
            claims = self.ledger.claim_due(
                subject_id,
                worker_owner=self.worker_owner,
                limit=1,
            )
            if not claims:
                return None
            claim = claims[0]
            token = claim.claim_token
            if token is None:
                raise IntegrityError(f"wallet acquisition claimed without token: {claim.run_id}")
            try:
                balance, _asset, _address = self.acquirer.read_balance(
                    subject_id,
                    asset_id=claim.asset_id,
                    address_id=claim.address_id,
                    actor=actor,
                )
            except WalletRPCError as error:
                try:
                    return self.ledger.fail(
                        claim.run_id,
                        subject_id=subject_id,
                        claim_token=token,
                        worker_owner=self.worker_owner,
                        error=error,
                        actor=actor,
                    )
                except WalletAcquisitionRunError as state_error:
                    if state_error.code != "wallet_acquisition_lease_lost":
                        raise
                    return self._after_claim_loss(subject_id, claim.run_id)
            except (NotFoundError, ValueError) as error:
                try:
                    return self.ledger.fail(
                        claim.run_id,
                        subject_id=subject_id,
                        claim_token=token,
                        worker_owner=self.worker_owner,
                        error=error,
                        error_code="wallet_rpc_configuration_invalid",
                        request_count=0,
                        retryable=False,
                        actor=actor,
                    )
                except WalletAcquisitionRunError as state_error:
                    if state_error.code != "wallet_acquisition_lease_lost":
                        raise
                    return self._after_claim_loss(subject_id, claim.run_id)
            except Exception:
                try:
                    return self.ledger.mark_unknown(
                        claim.run_id,
                        subject_id=subject_id,
                        claim_token=token,
                        worker_owner=self.worker_owner,
                        error_code="wallet_rpc_unexpected_failure",
                        actor=actor,
                    )
                except WalletAcquisitionRunError as state_error:
                    if state_error.code != "wallet_acquisition_lease_lost":
                        raise
                    return self._after_claim_loss(subject_id, claim.run_id)

            renewed = self.ledger.renew(
                claim.run_id,
                subject_id=subject_id,
                claim_token=token,
                worker_owner=self.worker_owner,
            )
            if renewed is None:
                return self._after_claim_loss(subject_id, claim.run_id)
            try:
                completed, _snapshot = self.ledger.complete_success(
                    claim.run_id,
                    subject_id=subject_id,
                    claim_token=token,
                    worker_owner=self.worker_owner,
                    balance=balance,
                    actor=actor,
                )
            except WalletAcquisitionRunError as state_error:
                if state_error.code != "wallet_acquisition_lease_lost":
                    raise
                return self._after_claim_loss(subject_id, claim.run_id)
            return completed
        finally:
            self.ledger._slots.release()

    def run_due(
        self,
        subject_id: str,
        *,
        actor: str = "system",
        limit: int = WALLET_ACQUISITION_MAX_BATCH,
    ) -> builtins.list[WalletBalanceAcquisitionRunRecord]:
        WalletStore._require_operator(actor, "run wallet balance acquisitions")
        validate_subject_id(subject_id)
        if isinstance(limit, bool) or not 1 <= limit <= WALLET_ACQUISITION_MAX_BATCH:
            raise ValueError("wallet acquisition runner limit is invalid")
        completed: builtins.list[WalletBalanceAcquisitionRunRecord] = []
        for _ in range(limit):
            result = self.run_once(subject_id, actor=actor)
            if result is None:
                break
            completed.append(result)
        return completed
