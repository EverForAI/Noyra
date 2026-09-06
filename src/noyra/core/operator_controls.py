"""Fail-closed operator lifecycle controls and privacy-safe health projection.

The HTTP layer deliberately delegates lifecycle mutations here.  This keeps
authorization-facing routes small and gives pause/resume/reset/reconcile one
bounded, auditable state machine.  The projection contains counts, statuses,
and bounded metadata only; it never returns payloads, targets, credentials, or
operator free-form explanations.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .actions import ActionLedger
from .archive import ArchiveKeyring
from .database import CURRENT_SCHEMA_VERSION, Database
from .errors import (
    ArchiveKeyUnavailableError,
    IntegrityError,
    InvalidTransitionError,
    NotFoundError,
)
from .storage import StorageUsageScanner
from .types import canonical_json, content_hash, new_id, utc_now


class OperatorControlConflict(RuntimeError):
    """A requested operator mutation cannot be performed safely right now."""

    def __init__(self, code: str, *, detail: str | None = None):
        super().__init__(code if detail is None else f"{code}: {detail}")
        self.code = code


class OperatorControlNotFound(LookupError):
    """A subject-scoped operator resource does not exist."""


@dataclass(frozen=True)
class OperatorMutation:
    operation: str
    lifecycle: dict[str, Any]
    idempotent: bool

    def public(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "lifecycle": self.lifecycle,
            "idempotent": self.idempotent,
        }


class OperatorControlService:
    """Coordinate safe operator controls for one subject runtime."""

    def __init__(
        self,
        kernel: Any,
        *,
        integrity: Any | None = None,
        at_rest: Any | None = None,
        storage: Any | None = None,
        export_jobs: Any | None = None,
        cloud_archive_status: dict[str, Any] | None = None,
    ):
        self.kernel = kernel
        self.database: Database = kernel.database
        self.integrity = integrity
        self.at_rest = at_rest
        self.storage = storage
        self.export_jobs = export_jobs
        self.cloud_archive_status = cloud_archive_status or {
            "configured": False,
            "ready": False,
            "profile": None,
        }
        self._lock = threading.RLock()

    @property
    def subject_id(self) -> str:
        return str(self.kernel.subject_id)

    def pause(self, *, actor: str, reason: str) -> OperatorMutation:
        actor, reason = self._validate_actor_reason(actor, reason)
        with self._lock:
            current = self.kernel.lifecycle.current()
            if current.state == "paused":
                self._audit("operator_pause", actor, reason, {"idempotent": True})
                return OperatorMutation("pause", current.__dict__, True)
            if current.state != "active":
                raise OperatorControlConflict(
                    "lifecycle_pause_unavailable",
                    detail=f"pause requires active lifecycle, got {current.state}",
                )
            self.kernel.admission.invalidate()
            try:
                state = self.kernel.pause(self._public_reason("pause"), actor=actor)
            except (InvalidTransitionError, IntegrityError, RuntimeError) as error:
                # ``SubjectKernel.pause`` fences before attempting the
                # durable transition.  If the transition itself failed while
                # the lifecycle stayed active, restore admission with the
                # newer epoch so pre-pause operations remain invalidated.
                with suppress(Exception):
                    if self.kernel.lifecycle.current().state == "active":
                        self.kernel.admission.open()
                raise OperatorControlConflict(
                    "lifecycle_pause_failed", detail=type(error).__name__
                ) from error
            self._audit("operator_pause", actor, reason, {"idempotent": False})
            return OperatorMutation("pause", state.__dict__, False)

    def resume(self, *, actor: str, reason: str) -> OperatorMutation:
        actor, reason = self._validate_actor_reason(actor, reason)
        with self._lock:
            self._require_resume_health()
            current = self.kernel.lifecycle.current()
            if current.state == "active":
                self._audit("operator_resume", actor, reason, {"idempotent": True})
                return OperatorMutation("resume", current.__dict__, True)
            if current.state != "paused":
                raise OperatorControlConflict(
                    "lifecycle_resume_unavailable",
                    detail=f"resume requires paused lifecycle, got {current.state}",
                )
            try:
                state = self.kernel.resume(self._public_reason("resume"), actor=actor)
            except (InvalidTransitionError, IntegrityError, RuntimeError) as error:
                raise OperatorControlConflict(
                    "lifecycle_resume_failed", detail=type(error).__name__
                ) from error
            self.kernel.admission.open(epoch=state.version)
            self._audit("operator_resume", actor, reason, {"idempotent": False})
            return OperatorMutation("resume", state.__dict__, False)

    def reset(self, *, actor: str, reason: str) -> OperatorMutation:
        """Reset transient runtime control state without deleting subject data.

        Reset is intentionally narrower than a data wipe.  It is only allowed
        while paused and when no prepared/unknown/in-flight work or open sleep
        run exists.  Every lifecycle edge is committed in one transaction; a
        failure rolls the runtime back to its prior paused state.
        """

        actor, reason = self._validate_actor_reason(actor, reason)
        with self._lock:
            self._require_resume_health()
            current = self.kernel.lifecycle.current()
            if current.state != "paused":
                raise OperatorControlConflict(
                    "reset_requires_paused",
                    detail=f"reset requires paused lifecycle, got {current.state}",
                )
            if self.kernel.admission.active_operations:
                raise OperatorControlConflict(
                    "reset_blocked_by_active_operations",
                    detail="runtime operations are still draining after pause",
                )
            self.kernel.admission.invalidate()
            try:
                with self.database.transaction() as connection:
                    work = self._work_counts_connection(connection)
                    if work["blocking"]:
                        raise OperatorControlConflict(
                            "reset_blocked_by_recoverable_work",
                            detail="prepared, unknown, executing, or in-flight work remains",
                        )
                    open_sleep = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM sleep_runs WHERE subject_id = ? "
                            "AND status NOT IN ('complete', 'failed')",
                            (self.subject_id,),
                        ).fetchone()[0]
                    )
                    if open_sleep:
                        raise OperatorControlConflict(
                            "reset_blocked_by_sleep", detail="an open sleep run remains"
                        )
                    active_exports = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM export_jobs WHERE subject_id = ? "
                            "AND status IN ('queued', 'running')",
                            (self.subject_id,),
                        ).fetchone()[0]
                    )
                    if active_exports:
                        raise OperatorControlConflict(
                            "reset_blocked_by_export", detail="an export is still active"
                        )
                    # Keep identity, memories, events, and durable work history.
                    lifecycle = self.kernel.lifecycle
                    for state in ("stopped", "resetting", "booting", "orienting", "active"):
                        lifecycle._transition_connection(
                            connection,
                            state,
                            self._public_reason("reset"),
                            actor=actor,
                        )
                    now = utc_now()
                    connection.execute(
                        "INSERT OR IGNORE INTO autonomy_loop_state(subject_id, updated_at) "
                        "VALUES (?, ?)",
                        (self.subject_id, now),
                    )
                    connection.execute(
                        "UPDATE autonomy_loop_state SET consecutive_failures = 0, "
                        "circuit_status = 'closed', next_retry_at = NULL, "
                        "last_tick_started_at = NULL, last_error_type = NULL, updated_at = ? "
                        "WHERE subject_id = ?",
                        (now, self.subject_id),
                    )
                    self._audit_connection(
                        connection,
                        "operator_reset",
                        actor,
                        reason,
                        {"idempotent": False, "preserved_subject_data": True},
                    )
                state = self.kernel.lifecycle.current()
            except OperatorControlConflict:
                raise
            except (InvalidTransitionError, IntegrityError, sqlite3.Error, RuntimeError) as error:
                raise OperatorControlConflict(
                    "reset_failed", detail=type(error).__name__
                ) from error
            self.kernel.admission.open(epoch=state.version)
            return OperatorMutation("reset", state.__dict__, False)

    def reconcile_action(
        self,
        action_id: str,
        *,
        actor: str,
        reason: str,
        outcome: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        actor, reason = self._validate_actor_reason(actor, reason)
        if outcome not in {"succeeded", "failed", "cancelled"}:
            raise OperatorControlConflict("invalid_reconciliation_outcome")
        if not isinstance(result, dict):
            raise OperatorControlConflict("invalid_reconciliation_result")
        with self._lock:
            row = self._subject_row("actions", "action_id", action_id)
            if row is None:
                raise OperatorControlNotFound("action not found")
            status = str(row["status"])
            if status in {"succeeded", "failed", "cancelled"}:
                if status == outcome:
                    return {"action_id": action_id, "status": status, "idempotent": True}
                raise OperatorControlConflict("action_already_terminal")
            ledger = ActionLedger(self.database)
            try:
                if status == "prepared":
                    if outcome != "cancelled":
                        raise OperatorControlConflict("prepared_action_must_be_cancelled")
                    record = ledger.cancel(
                        action_id,
                        "Operator cancelled prepared work without starting execution.",
                    )
                elif status == "unknown":
                    record = ledger.reconcile_unknown(
                        action_id,
                        outcome,
                        result,
                        public_explanation="Operator reconciled an unknown action outcome.",
                        side_effect_summary="operator reconciliation; automatic replay disabled",
                    )
                else:
                    raise OperatorControlConflict("action_not_reconcilable")
            except OperatorControlConflict:
                raise
            except (InvalidTransitionError, NotFoundError, ValueError, IntegrityError) as error:
                raise OperatorControlConflict(
                    "action_reconciliation_conflict", detail=type(error).__name__
                ) from error
            self._audit(
                "operator_action_reconcile",
                actor,
                reason,
                {"action_id": action_id, "outcome": record.status},
            )
            return {"action_id": action_id, "status": record.status, "idempotent": False}

    def reconcile_model_call(
        self,
        call_id: str,
        *,
        actor: str,
        reason: str,
        outcome: str,
        response: dict[str, Any] | None,
    ) -> dict[str, Any]:
        actor, reason = self._validate_actor_reason(actor, reason)
        if outcome not in {"succeeded", "failed", "retry"}:
            raise OperatorControlConflict("invalid_reconciliation_outcome")
        if response is not None and not isinstance(response, dict):
            raise OperatorControlConflict("invalid_reconciliation_result")
        if outcome == "retry" and response is not None:
            raise OperatorControlConflict("invalid_reconciliation_result")
        with self._lock:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT status FROM model_calls WHERE call_id = ? AND subject_id = ?",
                    (call_id, self.subject_id),
                ).fetchone()
            if row is None:
                raise OperatorControlNotFound("model call not found")
            status = str(row["status"])
            if status in {"succeeded", "failed"}:
                if status == outcome:
                    return {"call_id": call_id, "status": status, "idempotent": True}
                raise OperatorControlConflict("model_call_already_terminal")
            from noyra.model.ledger import ModelLedger

            ledger = ModelLedger(self.database)
            try:
                if status == "prepared":
                    if outcome == "retry":
                        with self.database.connection() as connection:
                            authorized = connection.execute(
                                "SELECT 1 FROM audit_records WHERE subject_id = ? "
                                "AND action = 'model_unknown_retry_authorized' "
                                "AND json_valid(payload_json) "
                                "AND json_extract(payload_json, '$.call_id') = ? LIMIT 1",
                                (self.subject_id, call_id),
                            ).fetchone()
                        if authorized is not None:
                            return {
                                "call_id": call_id,
                                "status": "prepared",
                                "idempotent": True,
                            }
                        raise OperatorControlConflict("prepared_model_call_must_fail_closed")
                    if outcome != "failed":
                        raise OperatorControlConflict("prepared_model_call_must_fail_closed")
                    record = ledger.reconcile_prepared_failed(
                        call_id,
                        actor=actor,
                        reason=reason,
                    )
                elif status == "unknown":
                    if outcome == "retry":
                        record = ledger.prepare_unknown_retry(
                            call_id,
                            actor=actor,
                            reason=reason,
                        )
                    else:
                        record = ledger.reconcile_unknown(
                            call_id,
                            outcome=outcome,
                            actor=actor,
                            reason=reason,
                            response=response if outcome == "succeeded" else None,
                        )
                else:
                    raise OperatorControlConflict("model_call_not_reconcilable")
            except OperatorControlConflict:
                raise
            except (
                InvalidTransitionError,
                NotFoundError,
                ValueError,
                IntegrityError,
                RuntimeError,
            ) as error:
                raise OperatorControlConflict(
                    "model_call_reconciliation_conflict", detail=type(error).__name__
                ) from error
            self._audit(
                "operator_model_call_reconcile",
                actor,
                reason,
                {"call_id": call_id, "outcome": record.status},
            )
            return {"call_id": call_id, "status": record.status, "idempotent": False}

    def recoverable_work(self, *, limit: int = 100) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 1_000))
        with self.database.connection() as connection:
            actions = connection.execute(
                "SELECT action_id, status FROM actions WHERE subject_id = ? "
                "AND status IN ('prepared', 'executing', 'unknown') "
                "ORDER BY prepared_at, action_id LIMIT ?",
                (self.subject_id, bounded),
            ).fetchall()
            calls = connection.execute(
                "SELECT call_id, status FROM model_calls WHERE subject_id = ? "
                "AND status IN ('prepared', 'executing', 'unknown') "
                "ORDER BY created_at, call_id LIMIT ?",
                (self.subject_id, bounded),
            ).fetchall()
            deliveries = connection.execute(
                "SELECT delivery_id, status FROM interaction_deliveries WHERE subject_id = ? "
                "AND status IN ('queued', 'sending', 'unknown') "
                "ORDER BY created_at, delivery_id LIMIT ?",
                (self.subject_id, bounded),
            ).fetchall()
            totals = self._work_counts_connection(connection)
        return {
            "actions": [dict(row) for row in actions],
            "model_calls": [dict(row) for row in calls],
            "deliveries": [dict(row) for row in deliveries],
            "counts": {
                "actions": totals["actions"],
                "model_calls": totals["model_calls"],
                "model_attempts": totals["model_attempts"],
                "deliveries": totals["deliveries"],
            },
            "truncated": any(
                totals[name] > len(rows)
                for name, rows in (
                    ("actions", actions),
                    ("model_calls", calls),
                    ("deliveries", deliveries),
                )
            ),
        }

    def health(self) -> dict[str, Any]:
        """Return only bounded status/count metadata suitable for diagnostics."""
        components: dict[str, dict[str, Any]] = {
            "integrity": self._integrity_health(),
            "archive_key": self._archive_key_health(),
            "migration": self._migration_health(),
            "wal": self._wal_health(),
            "storage": self._storage_health(),
            "export": self._export_health(),
            "at_rest": self._at_rest_health(),
            "work": self._work_health(),
        }
        degraded = any(item.get("state") == "degraded" for item in components.values())
        return {"status": "degraded" if degraded else "ok", **components}

    def _require_resume_health(self) -> None:
        if self.at_rest is not None and getattr(self.at_rest, "required", False):
            try:
                self.at_rest.require_ready()
            except Exception as error:
                raise OperatorControlConflict(
                    "at_rest_boundary_unavailable", detail=type(error).__name__
                ) from error
        if self.integrity is not None:
            try:
                summary = self.integrity.summary()
            except Exception as error:
                raise OperatorControlConflict(
                    "integrity_health_unavailable", detail=type(error).__name__
                ) from error
            blocking_policy = summary.get("policy_mode") == "pause"
            if blocking_policy and (int(summary.get("p0", 0)) or int(summary.get("p1", 0))):
                raise OperatorControlConflict("integrity_findings_block_resume")

    def _work_health(self) -> dict[str, Any]:
        try:
            with self.database.connection() as connection:
                counts = self._work_counts_connection(connection)
            state = "attention" if counts["blocking"] else "ok"
            return {
                "state": state,
                "actions": counts["actions"],
                "model_calls": counts["model_calls"],
                "model_attempts": counts["model_attempts"],
                "deliveries": counts["deliveries"],
            }
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return {"state": "degraded", "reason": "unavailable"}

    def _integrity_health(self) -> dict[str, Any]:
        if self.integrity is None:
            return {"state": "not_configured", "status": "not_configured"}
        try:
            summary = self.integrity.summary()
            status = str(summary.get("status", "unknown"))
            state = "ok" if status == "ok" else "degraded"
            return {
                "state": state,
                "status": status,
                "policy_mode": str(summary.get("policy_mode", "unknown")),
                "p0": int(summary.get("p0", 0)),
                "p1": int(summary.get("p1", 0)),
                "last_completed_at": summary.get("last_completed_at"),
                "next_due_at": summary.get("next_due_at"),
            }
        except Exception:
            return {"state": "degraded", "status": "unavailable"}

    def _archive_key_health(self) -> dict[str, Any]:
        try:
            with self.database.connection() as connection:
                references = int(
                    connection.execute(
                        "SELECT (SELECT COUNT(*) FROM event_payload_segments "
                        "WHERE subject_id = ?) + (SELECT COUNT(*) FROM "
                        "observation_content_segments WHERE subject_id = ?)",
                        (self.subject_id, self.subject_id),
                    ).fetchone()[0]
                )
            configured = ArchiveKeyring.configured()
            if not configured and references == 0:
                return {"state": "not_configured", "configured": False, "references": 0}
            if not configured:
                return {
                    "state": "degraded",
                    "configured": False,
                    "references": references,
                    "reason": "key_unavailable",
                }
            ring = ArchiveKeyring.from_env()
            ring.validate_revision(self.database, self.subject_id)
            return {
                "state": "ok",
                "configured": True,
                "references": references,
                "generation": ring.generation,
                "retained_keys": len(ring.metadata()),
            }
        except ArchiveKeyUnavailableError:
            return {"state": "degraded", "configured": True, "reason": "key_unavailable"}
        except Exception:
            return {"state": "degraded", "configured": True, "reason": "invalid_key_metadata"}

    def _migration_health(self) -> dict[str, Any]:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
            version = int(row[0]) if row is not None else 0
            backups = sum(
                1
                for path in self.database.path.parent.glob(
                    f"{self.database.path.name}.pre-migration-v*.bak"
                )
                if path.is_file()
            )
            if version != CURRENT_SCHEMA_VERSION:
                return {
                    "state": "degraded",
                    "schema_version": version,
                    "expected_schema_version": CURRENT_SCHEMA_VERSION,
                    "rollback_backups": backups,
                    "reason": "schema_version_mismatch",
                }
            return {
                "state": "ok",
                "schema_version": version,
                "expected_schema_version": CURRENT_SCHEMA_VERSION,
                "rollback_backups": backups,
            }
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return {"state": "degraded", "reason": "migration_health_unavailable"}

    def _wal_health(self) -> dict[str, Any]:
        try:
            with self.database.connection() as connection:
                journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                synchronous = str(connection.execute("PRAGMA synchronous").fetchone()[0]).lower()
                quick = str(connection.execute("PRAGMA quick_check(1)").fetchone()[0]).lower()
            wal_path = self.database.path.with_name(self.database.path.name + "-wal")
            shm_path = self.database.path.with_name(self.database.path.name + "-shm")
            wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0
            shm_bytes = shm_path.stat().st_size if shm_path.is_file() else 0
            state = "ok" if journal == "wal" and quick == "ok" else "degraded"
            return {
                "state": state,
                "journal_mode": journal,
                "synchronous": synchronous,
                "quick_check": quick,
                "wal_bytes": max(0, int(wal_bytes)),
                "shm_bytes": max(0, int(shm_bytes)),
            }
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return {"state": "degraded", "reason": "wal_health_unavailable"}

    def _storage_health(self) -> dict[str, Any]:
        try:
            scanner = (
                self.storage.scanner
                if self.storage is not None and hasattr(self.storage, "scanner")
                else StorageUsageScanner(self.database.path.parent, self.database)
            )
            usage = scanner.scan()
            warnings: tuple[str, ...] = ()
            if self.storage is not None and hasattr(self.storage, "quota"):
                warnings = tuple(usage.warnings(self.storage.quota))
            minimum_free = (
                int(self.storage.minimum_free_bytes)
                if self.storage is not None and hasattr(self.storage, "minimum_free_bytes")
                else 0
            )
            critical = usage.free_bytes < minimum_free
            return {
                "state": "degraded" if warnings or critical else "ok",
                "warnings": list(warnings),
                "free_bytes": usage.free_bytes,
                "subject_bytes": usage.subject_bytes,
                "effective_subject_bytes": usage.effective_subject_bytes,
                "database_bytes": usage.database_bytes,
                "wal_bytes": usage.wal_bytes,
                "exports_bytes": usage.exports_bytes,
                "minimum_free_bytes": minimum_free,
            }
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return {"state": "degraded", "reason": "storage_health_unavailable"}

    def _export_health(self) -> dict[str, Any]:
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM export_jobs WHERE subject_id = ? "
                    "GROUP BY status",
                    (self.subject_id,),
                ).fetchall()
            counts = {str(row["status"]): int(row["count"]) for row in rows}
            active = counts.get("queued", 0) + counts.get("running", 0)
            return {"state": "ok", "counts": counts, "active": active}
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return {"state": "degraded", "reason": "export_health_unavailable"}

    def _at_rest_health(self) -> dict[str, Any]:
        if self.at_rest is None:
            return {"state": "not_configured", "enforced": False}
        try:
            payload = self.at_rest.health()
            if not isinstance(payload, dict):
                return {"state": "degraded", "enforced": True, "reason": "invalid_projection"}
            enforced = bool(payload.get("enforced"))
            ready = bool(payload.get("ready"))
            return {
                "state": "ok" if not enforced or ready else "degraded",
                "enforced": enforced,
                "ready": ready,
                "mode": payload.get("mode"),
            }
        except Exception:
            return {"state": "degraded", "enforced": True, "reason": "unavailable"}

    def _work_counts_connection(self, connection: Any) -> dict[str, Any]:
        def count(table: str, statuses: tuple[str, ...]) -> int:
            marks = ",".join("?" for _ in statuses)
            return int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE subject_id = ? AND status IN ({marks})",
                    (self.subject_id, *statuses),
                ).fetchone()[0]
            )

        actions = count("actions", ("prepared", "executing", "unknown"))
        calls = count("model_calls", ("prepared", "executing", "unknown"))
        attempts = count("model_attempts", ("authorized", "executing", "unknown"))
        deliveries = count("interaction_deliveries", ("queued", "sending", "unknown"))
        return {
            "actions": actions,
            "model_calls": calls,
            "model_attempts": attempts,
            "deliveries": deliveries,
            "blocking": bool(actions or calls or attempts or deliveries),
        }

    def _subject_row(self, table: str, key: str, value: str) -> Any | None:
        with self.database.connection() as connection:
            return connection.execute(
                f"SELECT * FROM {table} WHERE {key} = ? AND subject_id = ?",
                (value, self.subject_id),
            ).fetchone()

    @staticmethod
    def _validate_actor_reason(actor: str, reason: str) -> tuple[str, str]:
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 256:
            raise OperatorControlConflict("operator_actor_required")
        if actor.strip().casefold() == "subject":
            raise OperatorControlConflict("operator_authorization_required")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2_000:
            raise OperatorControlConflict("operator_reason_required")
        return actor.strip(), reason.strip()

    @staticmethod
    def _public_reason(operation: str) -> str:
        return f"operator {operation} through the management API"

    def _audit(self, action: str, actor: str, reason: str, payload: dict[str, Any]) -> None:
        with self.database.transaction() as connection:
            self._audit_connection(connection, action, actor, reason, payload)

    def _audit_connection(
        self,
        connection: Any,
        action: str,
        actor: str,
        reason: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO audit_records(audit_id, subject_id, action, actor, "
            "payload_json, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                new_id("audit"),
                self.subject_id,
                action,
                actor,
                canonical_json(
                    {
                        **payload,
                        "reason": reason,
                        "reason_hash": content_hash(reason),
                    }
                ),
                utc_now(),
            ),
        )


__all__ = [
    "OperatorControlConflict",
    "OperatorControlNotFound",
    "OperatorControlService",
    "OperatorMutation",
]
