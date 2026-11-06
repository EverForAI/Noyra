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

from noyra.sleep import SleepEngine

from .actions import ActionLedger
from .admission import OperationInvalidated
from .archive import ArchiveKeyring
from .database import CURRENT_SCHEMA_VERSION, Database
from .delivery_state import UNRESOLVED_DELIVERY_SQL, delivery_status_counts
from .errors import (
    ArchiveKeyUnavailableError,
    IntegrityError,
    InvalidTransitionError,
    NotFoundError,
    RuntimeOwnershipError,
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

    def sleep_recovery(self) -> dict[str, Any]:
        from .sleep_recovery import directory

        return directory(self)

    def abort_sleep(
        self, sleep_id: str, expected_version: int, *, actor: str, reason: str
    ) -> dict[str, Any]:
        from .sleep_recovery import abort

        return abort(self, sleep_id, expected_version, actor=actor, reason=reason)

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

    def wake(self, *, actor: str, reason: str) -> OperatorMutation:
        """Wake a scheduled deep-sleep run early through the sleep protocol."""
        actor, reason = self._validate_actor_reason(actor, reason)
        with self._lock:
            self._require_resume_health()
            current = self.kernel.lifecycle.current()
            if current.state != "deep_sleep":
                raise OperatorControlConflict(
                    "lifecycle_wake_unavailable",
                    detail=f"wake requires deep_sleep lifecycle, got {current.state}",
                )
            try:
                with self.database.read_transaction() as connection:
                    work = self._work_counts_connection(connection)
            except (IntegrityError, sqlite3.Error) as error:
                raise OperatorControlConflict(
                    "wake_work_state_unavailable", detail=type(error).__name__
                ) from error
            if work["blocking"]:
                raise OperatorControlConflict(
                    "wake_blocked_by_recoverable_work",
                    detail="prepared, unknown, executing, or in-flight work remains",
                )
            sleep = SleepEngine(self.database, self.subject_id)
            run = sleep.current()
            if run is None or run.status != "deep_sleep":
                raise OperatorControlConflict("lifecycle_wake_unavailable")
            public_reason = self._public_reason("wake")
            try:
                sleep.wake_early(run.sleep_id, public_reason, actor=actor)
            except (
                InvalidTransitionError,
                IntegrityError,
                NotFoundError,
                RuntimeError,
                ValueError,
            ) as error:
                raise OperatorControlConflict(
                    "lifecycle_wake_failed", detail=type(error).__name__
                ) from error
            state = self.kernel.lifecycle.current()
            self.kernel.admission.open(epoch=state.version)
            self._audit("operator_wake", actor, reason, {"idempotent": False})
            return OperatorMutation("wake", state.__dict__, False)

    def reset(self, *, actor: str, reason: str) -> OperatorMutation:
        """Reset transient runtime control state without deleting subject data.

        Reset is intentionally narrower than a data wipe.  It is only allowed
        while paused and when no prepared/unknown/in-flight work or open sleep
        run exists.  Every lifecycle edge is committed in one transaction; a
        failure rolls the runtime back to its prior paused state.
        """

        actor, reason = self._validate_actor_reason(actor, reason)
        try:
            # Match the HTTP control lock order. Keep process ownership until
            # both the durable reset and reopening admission have completed.
            with (
                self.kernel.admission.lifecycle_control_scope(),
                self._lock,
                self.kernel.process_lock.ownership_scope(),
            ):
                return self._reset_owned(actor=actor, reason=reason)
        except (OperationInvalidated, RuntimeOwnershipError) as error:
            raise OperatorControlConflict(
                "reset_runtime_unavailable", detail=type(error).__name__
            ) from error

    def _reset_owned(self, *, actor: str, reason: str) -> OperatorMutation:
        if self.kernel.admission.migration_fenced:
            raise OperatorControlConflict("reset_migration_fenced")
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
                lifecycle = self.kernel.lifecycle
                if lifecycle.mutation_guard is not None:
                    lifecycle.mutation_guard(connection)
                durable = connection.execute(
                    "SELECT * FROM runtime_state WHERE subject_id=?", (self.subject_id,)
                ).fetchone()
                if (
                    durable is None
                    or durable["state"] != "paused"
                    or durable["version"] != current.version
                ):
                    raise OperatorControlConflict("reset_lifecycle_changed")
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
            raise OperatorControlConflict("reset_failed", detail=type(error).__name__) from error
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
        resolution_evidence: str | None = None,
        accept_duplicate_risk: bool = False,
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
                    retry_grant_closed = False
                    if status == "failed" and outcome == "failed":
                        from noyra.model.errors import (
                            ModelCallStateError,
                            UnknownRetryResolutionEvidenceRequired,
                        )
                        from noyra.model.ledger import ModelLedger

                        try:
                            retry_grant_closed = ModelLedger(
                                self.database
                            ).close_failed_unknown_retry(
                                call_id,
                                actor=actor,
                                reason=reason,
                                resolution_evidence=resolution_evidence,
                                accept_duplicate_risk=accept_duplicate_risk,
                            )
                        except UnknownRetryResolutionEvidenceRequired as error:
                            raise OperatorControlConflict(
                                "model_unknown_retry_resolution_evidence_required"
                            ) from error
                        except (ModelCallStateError, ValueError) as error:
                            raise OperatorControlConflict(
                                "model_call_reconciliation_conflict",
                                detail=type(error).__name__,
                            ) from error
                        if retry_grant_closed:
                            self._audit(
                                "operator_model_call_reconcile",
                                actor,
                                reason,
                                {"call_id": call_id, "outcome": "failed"},
                            )
                    return {
                        "call_id": call_id,
                        "status": status,
                        "idempotent": not retry_grant_closed,
                        "retry_grant_closed": retry_grant_closed,
                        "duplicate_risk_accepted": retry_grant_closed,
                    }
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

    def memory_integration_recovery(self) -> dict[str, Any]:
        from dataclasses import asdict

        from noyra.cognition.execution_recovery import pending_effects
        from noyra.cognition.memory_rounds import MemoryIntegrationRounds
        from noyra.model import ModelLedger

        rounds = MemoryIntegrationRounds(self.database, self.subject_id)
        with self.database.read_transaction() as c:
            # Validate existing acknowledgements before omitting them from the
            # pending page; a corrupt old receipt must not disappear via SQL.
            rounds.legacy_blocked(c)
            rows = rounds.legacy_records(c, limit=101)
            lifecycle = c.execute(
                "SELECT state FROM runtime_state WHERE subject_id=?", (self.subject_id,)
            ).fetchone()
            blocked = pending_effects(c, self.subject_id)
            ready = (
                lifecycle is not None
                and lifecycle[0] == "paused"
                and self.kernel.admission.active_operations == 0
                and self.kernel.process_lock.held
                and not self.kernel.admission.quarantined
                and not self.kernel.admission.migration_fenced
                and not any(blocked.values())
            )
            items = []
            for row in rows[:100]:
                call = ModelLedger._call_from_row(row)
                discarded = rounds.legacy_receipt(c, call)
                items.append(
                    {
                        "call_id": call.call_id,
                        "purpose": call.purpose,
                        "status": call.status,
                        "expected_fingerprint": content_hash(asdict(call)),
                        "discarded": discarded,
                        "can_discard": ready and call.status == "succeeded" and not discarded,
                    }
                )
        return {"records": items, "truncated": len(rows) > 100, "external_blockers": blocked}

    def discard_legacy_memory_result(
        self,
        call_id: str,
        *,
        expected_fingerprint: str,
        evidence_sha256: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        from noyra.cognition.execution_recovery import pending_effects
        from noyra.cognition.memory_rounds import MemoryIntegrationRounds

        actor, reason = self._validate_actor_reason(actor, reason)
        if (
            not isinstance(call_id, str)
            or not 1 <= len(call_id) <= 256
            or not MemoryIntegrationRounds.digest(expected_fingerprint)
            or not MemoryIntegrationRounds.digest(evidence_sha256)
        ):
            raise OperatorControlConflict("invalid_memory_recovery_request")
        with self._lock, self.kernel.admission.lifecycle_control_scope():
            self.kernel.admission.require_owned(self.kernel.process_lock)
            self._require_resume_health()
            if self.kernel.admission.migration_fenced or self.kernel.admission.active_operations:
                raise OperatorControlConflict("memory_recovery_not_drained_or_migration_fenced")
            try:
                with self.database.transaction() as c:
                    lifecycle = c.execute(
                        "SELECT state FROM runtime_state WHERE subject_id=?", (self.subject_id,)
                    ).fetchone()
                    if lifecycle is None or lifecycle[0] != "paused":
                        raise OperatorControlConflict("memory_recovery_requires_paused")
                    if any(pending_effects(c, self.subject_id).values()):
                        raise OperatorControlConflict("memory_recovery_external_work_unresolved")
                    result = MemoryIntegrationRounds(self.database, self.subject_id).discard_legacy(
                        c,
                        call_id,
                        expected_fingerprint,
                        actor=actor,
                        reason=reason,
                        evidence_sha256=evidence_sha256,
                    )
                    if not result["idempotent"]:
                        self._audit_connection(
                            c,
                            "semantic_memory_legacy_discarded",
                            actor,
                            reason,
                            {"call_id": call_id, "evidence_sha256": evidence_sha256},
                        )
                    return result
            except ValueError as error:
                raise OperatorControlConflict(
                    "memory_recovery_snapshot_or_status_conflict"
                ) from error
            except (IntegrityError, sqlite3.Error) as error:
                raise OperatorControlConflict("memory_recovery_evidence_unavailable") from error

    def workflow_exports(self, *, limit: int = 50) -> dict[str, Any]:
        from .workflow_export_catalog import WorkflowExportCatalog

        return WorkflowExportCatalog(self.database, self.subject_id).page(limit=limit)

    def export_workflow(
        self, workflow_id: str, expected_version: int, request_key: str, *, actor: str, reason: str
    ) -> dict[str, Any]:
        from .workflow_export_control import export_workflow

        return export_workflow(
            self, workflow_id, expected_version, request_key, actor=actor, reason=reason
        )

    def verify_workflow_export(self, publication_id: str) -> dict[str, Any]:
        from .workflow_archive_control import open_workflow_export

        with open_workflow_export(self, publication_id) as evidence:
            return {"publication_id": publication_id, **evidence.summary()}

    def workflow_clock_history(self, workflow_id: str) -> dict[str, Any]:
        from noyra.autonomy.workflow import DurableWorkflowStore

        if not isinstance(workflow_id, str) or not 1 <= len(workflow_id) <= 256:
            raise OperatorControlConflict("invalid_workflow_clock_request")
        result = DurableWorkflowStore(self.database).clock_catalog(self.subject_id, workflow_id)
        unexplained = result["unexplained_versions"]
        result["unexplained_count"] = None if unexplained is None else len(unexplained)
        result["truncated"] = unexplained is not None and len(unexplained) > 100
        result["unexplained_versions"] = None if unexplained is None else unexplained[:100]
        return result

    def acknowledge_workflow_clock(
        self,
        workflow_id: str,
        *,
        expected_fingerprint: str,
        evidence_sha256: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        from noyra.autonomy.workflow import DurableWorkflowStore

        from .admission import bind_lease

        actor, reason = self._validate_actor_reason(actor, reason)
        if (
            not isinstance(workflow_id, str)
            or not 1 <= len(workflow_id) <= 256
            or not isinstance(expected_fingerprint, str)
            or len(expected_fingerprint) != 64
            or set(expected_fingerprint) - set("0123456789abcdef")
            or not isinstance(evidence_sha256, str)
            or len(evidence_sha256) != 64
            or set(evidence_sha256) - set("0123456789abcdef")
        ):
            raise OperatorControlConflict("invalid_workflow_clock_request")
        with (
            self._lock,
            self.kernel.admission.operation(
                "workflow-clock-recovery", allow_quarantine=True
            ) as lease,
        ):
            self.kernel.admission.require_owned(self.kernel.process_lock)
            with bind_lease(lease), self.kernel.admission.commit_scope(lease):
                if self.kernel.admission.active_operations != 1:
                    raise OperatorControlConflict("workflow_clock_operations_not_drained")
                if self.at_rest is not None and getattr(self.at_rest, "required", False):
                    try:
                        self.at_rest.require_ready()
                    except Exception as error:
                        raise OperatorControlConflict("at_rest_boundary_unavailable") from error
                try:
                    with self.database.transaction() as connection:
                        lifecycle = connection.execute(
                            "SELECT state FROM runtime_state WHERE subject_id=?", (self.subject_id,)
                        ).fetchone()
                        if lifecycle is None or lifecycle[0] != "paused":
                            raise OperatorControlConflict("workflow_clock_requires_paused")
                        result = DurableWorkflowStore(self.database).acknowledge_clock_history(
                            connection,
                            self.subject_id,
                            workflow_id,
                            expected_fingerprint,
                            actor=actor,
                            reason=reason,
                            evidence_sha256=evidence_sha256,
                        )
                        if not result["idempotent"]:
                            self._audit_connection(
                                connection,
                                "workflow_clock_acknowledged",
                                actor,
                                reason,
                                {
                                    "workflow_id": workflow_id,
                                    "evidence_sha256": evidence_sha256,
                                    "acknowledged_versions": result["acknowledged_versions"],
                                },
                            )
                        return result
                except (ValueError, PermissionError) as error:
                    raise OperatorControlConflict(
                        "workflow_clock_snapshot_or_evidence_conflict"
                    ) from error
                except IntegrityError as error:
                    raise OperatorControlConflict(
                        "workflow_clock_non_time_evidence_invalid"
                    ) from error

    def project_execution_recovery(self, *, limit: int = 50) -> dict[str, Any]:
        from noyra.cognition.execution_recovery import ProjectExecutionRecovery

        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid recovery limit")
        with self.database.read_transaction() as connection:
            result = ProjectExecutionRecovery(self.database, self.subject_id).catalog(
                connection, limit=limit
            )
            lifecycle = connection.execute(
                "SELECT state FROM runtime_state WHERE subject_id=?", (self.subject_id,)
            ).fetchone()
            paused = lifecycle is not None and lifecycle[0] == "paused"
            result["runtime_paused"] = paused
            result["migration_fenced"] = self.kernel.admission.migration_fenced
            result["runtime_owned"] = self.kernel.process_lock.held
            result["operations_drained"] = self.kernel.admission.active_operations == 0
            for item in result["records"]:
                item["can_release"] = bool(
                    item.pop("eligible")
                    and paused
                    and result["operations_drained"]
                    and result["runtime_owned"]
                    and not result["migration_fenced"]
                    and not self.kernel.admission.quarantined
                    and not self.kernel.admission.closed
                )
        return result

    def release_project_execution_review(
        self, execution_id: str, *, expected_fingerprint: str, actor: str, reason: str
    ) -> dict[str, Any]:
        from noyra.cognition.execution_recovery import ProjectExecutionRecovery

        actor, reason = self._validate_actor_reason(actor, reason)
        if (
            not isinstance(execution_id, str)
            or not 1 <= len(execution_id) <= 256
            or not isinstance(expected_fingerprint, str)
            or len(expected_fingerprint) != 64
            or set(expected_fingerprint) - set("0123456789abcdef")
        ):
            raise OperatorControlConflict("invalid_project_recovery_request")
        with self._lock, self.kernel.admission.lifecycle_control_scope():
            self.kernel.admission.require_owned(self.kernel.process_lock)
            if self.kernel.admission.migration_fenced:
                raise OperatorControlConflict("project_recovery_migration_fenced")
            self._require_resume_health()
            if self.kernel.admission.active_operations:
                raise OperatorControlConflict("project_recovery_operations_not_drained")
            try:
                with self.database.transaction() as connection:
                    result = ProjectExecutionRecovery(self.database, self.subject_id).release(
                        connection, execution_id, expected_fingerprint, actor=actor, reason=reason
                    )
                    if not result["idempotent"]:
                        self._audit_connection(
                            connection,
                            "project_execution_review_released",
                            actor,
                            reason,
                            {"execution_id": execution_id, "disposition": "review_only"},
                        )
                    return result
            except NotFoundError as error:
                raise OperatorControlNotFound("project execution not found") from error
            except ValueError as error:
                raise OperatorControlConflict(str(error)) from error
            except (IntegrityError, sqlite3.Error) as error:
                raise OperatorControlConflict("project_recovery_evidence_unavailable") from error

    def recoverable_work(self, *, limit: int = 100) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 1_000))
        with self.database.read_transaction() as connection:
            # Validate effective delivery outcomes before filtering the list.
            # Both totals and rows must describe this same WAL snapshot.
            totals = self._work_counts_connection(connection)
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
                "SELECT d.delivery_id, d.status FROM interaction_deliveries d "
                "WHERE d.subject_id=? AND "
                + UNRESOLVED_DELIVERY_SQL
                + " ORDER BY d.created_at,d.delivery_id LIMIT ?",
                (self.subject_id, bounded),
            ).fetchall()
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
            if blocking_policy and summary.get("evidence", {}).get("recheck_pending"):
                raise OperatorControlConflict("integrity_health_unavailable")

    def _work_health(self) -> dict[str, Any]:
        try:
            with self.database.read_transaction() as connection:
                counts = self._work_counts_connection(connection)
            state = "attention" if counts["blocking"] else "ok"
            return {
                "state": state,
                "actions": counts["actions"],
                "model_calls": counts["model_calls"],
                "model_attempts": counts["model_attempts"],
                "deliveries": counts["deliveries"],
            }
        except (OSError, sqlite3.Error, IntegrityError, TypeError, ValueError):
            return {"state": "degraded", "reason": "unavailable"}

    def _integrity_health(self) -> dict[str, Any]:
        if self.integrity is None:
            return {"state": "not_configured", "status": "not_configured"}
        try:
            summary = self.integrity.summary()
            status = str(summary.get("status", "unknown"))
            state = (
                "disabled"
                if summary.get("policy_mode") == "off"
                else "ok"
                if status == "ok"
                else "degraded"
            )
            return {
                "state": state,
                "status": status,
                "policy_mode": str(summary.get("policy_mode", "unknown")),
                "p0": int(summary.get("p0", 0)),
                "p1": int(summary.get("p1", 0)),
                "last_completed_at": summary.get("last_completed_at"),
                "next_due_at": summary.get("next_due_at"),
                "evidence": summary.get("evidence"),
                "coverage": summary.get("coverage"),
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
                for table in ("workflow_export_publications", "workflow_increment_publications"):
                    if connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone():
                        references += int(connection.execute(
                            "SELECT COUNT(*) FROM " + table + " WHERE subject_id=?",
                            (self.subject_id,),
                        ).fetchone()[0])
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
        delivery_counts = delivery_status_counts(connection, self.subject_id)
        deliveries = sum(
            delivery_counts.get(status, 0) for status in ("queued", "sending", "unknown")
        )
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
