"""Authenticated controls' narrow bridge to abnormal sleep termination."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from noyra.sleep import SleepEngine
from noyra.sleep.errors import SleepStateConflictError
from noyra.sleep.failure import fail_connection, verify_failure

from .errors import IntegrityError, NotFoundError
from .operator_controls import OperatorControlConflict, OperatorControlNotFound

if TYPE_CHECKING:
    from .operator_controls import OperatorControlService


def directory(controls: OperatorControlService) -> dict[str, Any]:
    engine = SleepEngine(controls.database, controls.subject_id)
    with controls.database.read_transaction() as c:
        state = c.execute(
            "SELECT state FROM runtime_state WHERE subject_id=?", (controls.subject_id,)
        ).fetchone()
        row = c.execute(
            "SELECT * FROM sleep_runs WHERE subject_id=? ORDER BY rowid DESC LIMIT 1",
            (controls.subject_id,),
        ).fetchone()
        run = None if row is None else engine._from_row(row)
        if row is not None:
            verify_failure(controls.database, c, row)
    return {
        "lifecycle": None if state is None else state["state"],
        "run": None
        if run is None
        else {
            "sleep_id": run.sleep_id,
            "status": run.status,
            "expected_version": run.version,
            "started_at": run.started_at,
            "updated_at": run.updated_at,
            "completed_at": run.completed_at,
            "reflection_committed": run.reflection_event_id is not None,
        },
    }


def abort(
    controls: OperatorControlService,
    sleep_id: str,
    expected_version: int,
    *,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    actor, reason = controls._validate_actor_reason(actor, reason)
    if not isinstance(sleep_id, str) or not sleep_id.strip() or len(sleep_id) > 200:
        raise ValueError("invalid sleep id")
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError("invalid sleep version")
    with controls._lock, controls.kernel.admission.lifecycle_control_scope():
        controls.kernel.admission.require_owned(controls.kernel.process_lock)
        if controls.kernel.admission.migration_fenced:
            raise OperatorControlConflict("sleep_recovery_migration_fenced")
        if controls.kernel.admission.active_operations:
            raise OperatorControlConflict("sleep_recovery_operations_not_drained")
        controls._require_resume_health()
        engine = SleepEngine(
            controls.database,
            controls.subject_id,
            mutation_guard=controls.kernel.lifecycle.mutation_guard,
        )
        try:
            with controls.database.transaction() as c:
                if engine.lifecycle.mutation_guard is not None:
                    engine.lifecycle.mutation_guard(c)
                row = engine._get_row(c, sleep_id)
                idempotent = row["status"] == "failed"
                fail_connection(engine, c, row, "operator_abort", actor, expected_version)
                if not idempotent:
                    controls._audit_connection(
                        c,
                        "operator_sleep_abort",
                        actor,
                        reason,
                        {"sleep_id": sleep_id, "expected_version": expected_version},
                    )
        except NotFoundError as error:
            raise OperatorControlNotFound("sleep run not found") from error
        except SleepStateConflictError as error:
            raise OperatorControlConflict("sleep_recovery_snapshot_conflict") from error
        except (IntegrityError, sqlite3.Error) as error:
            raise OperatorControlConflict("sleep_recovery_evidence_unavailable") from error
        if not idempotent:
            controls.kernel.admission.invalidate()
        return {
            "sleep_id": sleep_id,
            "status": "failed",
            "idempotent": idempotent,
            "lifecycle": controls.kernel.lifecycle.current().state,
        }
