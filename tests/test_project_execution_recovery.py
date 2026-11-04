from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

import test_autonomous_projects as helpers
from noyra.cognition import AutonomousProjectManager, ProjectExecutionLedger
from noyra.cognition.execution_recovery import ProjectExecutionRecovery
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import content_hash
from test_a0_project_recovery import case as case
from test_a0_project_recovery import count, review


async def interrupted(
    case: helpers.AutonomousProjectsTestCase,
    status: str = "unknown",
    output_type: str = "research_note",
) -> tuple[AutonomousProjectManager, str]:
    formation: dict[str, Any] = case.formation()
    formation["project_type"] = {
        "knowledge_collection": "knowledge",
        "software_prototype": "software_prototype",
        "prediction_record": "prediction",
    }.get(output_type, "research")
    formation["phases"][0]["output_type"] = output_type
    if output_type == "software_prototype":
        from noyra.capability import CapabilityGrant, CapabilityStore

        CapabilityStore(case.kernel.database).grant(
            case.subject_id,
            CapabilityGrant(
                capability_type="filesystem_write",
                scope={
                    "root": str(case.kernel.database.path.parent / "workspace" / case.subject_id)
                },
                issuer="fixture-operator",
                rate_limit_per_hour=10,
                side_effect=True,
            ),
            actor="fixture-operator",
        )
    gateway, _ = case.gateway([formation])
    manager = AutonomousProjectManager(
        case.kernel.database,
        case.subject_id,
        gateway,
        case.settings(),
        clock=lambda: case.clock_value,
    )
    assert await manager.run_due() == "autonomous_project_planned"
    manager.gateway = case.gateway([review(case, manager)])[0]
    assert await manager.run_due() == "autonomous_project_activate"
    project = manager.projects()[0]
    ledger = ProjectExecutionLedger(case.kernel.database, case.subject_id)
    execution = ledger.get_or_prepare(
        project, manager.phases(project.project_id)[0], workflow="autonomous_research"
    )
    if status == "prepared":
        return manager, execution.execution_id
    ledger.transition(execution.execution_id, "executing", reason="Synthetic interruption")
    if status == "unknown":
        ledger.transition(
            execution.execution_id, "unknown", reason="Synthetic uncertain local result"
        )
    return manager, execution.execution_id


def issue(case: helpers.AutonomousProjectsTestCase, identifier: str) -> dict[str, Any]:
    controls = OperatorControlService(case.kernel)
    controls.pause(actor="operator", reason="Inspect interrupted local attempt")
    recovery = ProjectExecutionRecovery(case.kernel.database, case.subject_id)
    with case.kernel.database.transaction() as c:
        row, project, phase = recovery.load(c, identifier)
        return recovery.release(
            c,
            identifier,
            recovery.fingerprint(row, project, phase),
            actor="operator",
            reason="External work verified; discard incomplete local result and allow Noyra review",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["executing", "unknown"])
async def test_released_attempt_reaches_subject_review_without_rewriting_execution(
    case: helpers.AutonomousProjectsTestCase,
    status: str,
) -> None:
    manager, identifier = await interrupted(case, status)
    ledger = ProjectExecutionLedger(case.kernel.database, case.subject_id)
    before = ledger.get(identifier)
    assert await manager.run_due() is None
    issue(case, identifier)
    OperatorControlService(case.kernel).resume(actor="operator", reason="Resume independent review")
    case.clock_value = "2026-08-14T13:00:00.000+00:00"
    manager.gateway, provider = case.gateway([review(case, manager, "continue")])
    assert await manager.run_due() == "autonomous_project_continue"
    assert len(provider.requests) == 1
    assert "External work verified; discard incomplete local result" not in str(provider.requests)
    assert ledger.get(identifier) == before
    phase = manager.phases(manager.projects()[0].project_id)[0]
    assert phase.attempt_count == 2 and phase.status == "active"
    assert manager.projects()[0].progress == 0
    manager.verify_integrity()
    ledger.verify_integrity()


@pytest.mark.asyncio
async def test_release_is_never_phase_acceptance_evidence(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager, identifier = await interrupted(case)
    issue(case, identifier)
    OperatorControlService(case.kernel).resume(actor="operator", reason="Review")
    case.clock_value = "2026-08-14T13:00:00.000+00:00"
    manager.gateway, provider = case.gateway([review(case, manager, "complete_phase")])
    assert await manager.run_due() is None
    assert len(provider.requests) == 1
    assert manager.review_control.read(manager.projects()[0])["reason"] == "review_backoff"
    assert manager.phases(manager.projects()[0].project_id)[0].status == "active"
    assert manager.projects()[0].progress == 0


@pytest.mark.asyncio
async def test_release_is_bound_idempotent_and_waits_for_real_ambiguity(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager, identifier = await interrupted(case)
    recovery = ProjectExecutionRecovery(case.kernel.database, case.subject_id)
    controls = OperatorControlService(case.kernel)
    controls.pause(actor="operator", reason="Maintenance")
    with case.kernel.database.read_transaction() as c:
        row, project, phase = recovery.load(c, identifier)
        fingerprint = recovery.fingerprint(row, project, phase)
    call, _ = manager.gateway.ledger.prepare_call(
        case.subject_id,
        "fake",
        "fixture",
        "world_cognition:pending",
        content_hash("pending"),
        "pending",
    )
    with (
        case.kernel.database.transaction() as c,
        pytest.raises(ValueError, match="external_work_unresolved"),
    ):
        recovery.release(c, identifier, fingerprint, actor="operator", reason="No guessing")
    assert not recovery.released_for_review(identifier)
    manager.gateway.ledger.finish_call(call.call_id, "failed", error_code="synthetic_unsent")
    with case.kernel.database.transaction() as c:
        first = recovery.release(
            c, identifier, fingerprint, actor="operator", reason="Verified unsent"
        )
        second = recovery.release(
            c, identifier, fingerprint, actor="operator", reason="Retry receipt"
        )
    assert first["idempotent"] is False and second["idempotent"] is True
    assert recovery.released_for_review(identifier)
    calls = count(case, "model_calls")
    with (
        case.kernel.database.transaction() as c,
        pytest.raises(ValueError, match="snapshot_conflict"),
    ):
        recovery.release(c, identifier, "0" * 64, actor="operator", reason="Stale browser")
    assert count(case, "model_calls") == calls


@pytest.mark.asyncio
async def test_operator_release_requires_pause_drain_fingerprint_and_atomic_audit(
    case: helpers.AutonomousProjectsTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.core.operator_controls import OperatorControlConflict

    manager, identifier = await interrupted(case)
    controls = OperatorControlService(case.kernel)
    item = controls.project_execution_recovery()["records"][0]
    assert not item["can_release"]
    with pytest.raises(OperatorControlConflict, match="requires_paused"):
        controls.release_project_execution_review(
            identifier,
            expected_fingerprint=item["expected_fingerprint"],
            actor="operator",
            reason="Not paused",
        )
    lease = case.kernel.admission.begin("synthetic-inflight-model")
    controls.pause(actor="operator", reason="Drain before recovery")
    with pytest.raises(OperatorControlConflict, match="not_drained"):
        controls.release_project_execution_review(
            identifier,
            expected_fingerprint=item["expected_fingerprint"],
            actor="operator",
            reason="Worker still active",
        )
    case.kernel.admission.finish(lease)
    before = ProjectExecutionLedger(case.kernel.database, case.subject_id).get(identifier)
    original_audit = controls._audit_connection

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(controls, "_audit_connection", broken)
    with pytest.raises(RuntimeError, match="audit failure"):
        controls.release_project_execution_review(
            identifier,
            expected_fingerprint=item["expected_fingerprint"],
            actor="operator",
            reason="Atomic",
        )
    assert not ProjectExecutionRecovery(case.kernel.database, case.subject_id).released_for_review(
        identifier
    )
    monkeypatch.setattr(controls, "_audit_connection", original_audit)
    result = controls.release_project_execution_review(
        identifier,
        expected_fingerprint=item["expected_fingerprint"],
        actor="operator",
        reason="Verified incomplete local output",
    )
    assert result["review_released"]
    assert ProjectExecutionLedger(case.kernel.database, case.subject_id).get(identifier) == before
    assert case.kernel.lifecycle.current().state == "paused"
    assert manager.projects()[0].progress == 0
    assert not controls.project_execution_recovery()["records"][0]["can_release"]


@pytest.mark.asyncio
async def test_bad_receipt_never_opens_review_and_does_not_rewrite_execution(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.cognition.execution_recovery import release_event_id
    from noyra.core.errors import IntegrityError

    _, identifier = await interrupted(case)
    issue(case, identifier)
    with case.kernel.database.transaction() as c:
        c.execute("DROP TRIGGER prevent_event_immutable_update")
        c.execute(
            "UPDATE events SET payload_hash=? WHERE event_id=?",
            ("0" * 64, release_event_id(case.subject_id, identifier)),
        )
    with pytest.raises(IntegrityError):
        ProjectExecutionRecovery(case.kernel.database, case.subject_id).released_for_review(
            identifier
        )
    with pytest.raises(IntegrityError):
        ProjectExecutionLedger(case.kernel.database, case.subject_id).verify_integrity()
    assert count(case, "autonomous_project_executions") == 1


@pytest.mark.asyncio
async def test_release_does_not_make_interrupted_phase_dispatchable(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from unittest.mock import AsyncMock, patch

    from noyra.cognition import ProjectPhaseExecutor
    from test_a0_project_recovery import make_cycle

    manager, identifier = await interrupted(case, "executing")
    issue(case, identifier)
    OperatorControlService(case.kernel).resume(actor="operator", reason="Review only")
    executor = ProjectPhaseExecutor(case.kernel.database, case.subject_id, Mock())
    dispatch = AsyncMock(side_effect=AssertionError("recovery must not dispatch"))
    with patch.object(executor, "run_phase", dispatch):
        cycle = make_cycle(case, manager, executor)
        assert await cycle._run_project_phase() is None
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_is_subject_bound_and_stale_phase_is_not_released(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.core.errors import NotFoundError
    from noyra.core.operator_controls import OperatorControlConflict

    manager, identifier = await interrupted(case)
    controls = OperatorControlService(case.kernel)
    item = controls.project_execution_recovery()["records"][0]
    controls.pause(actor="operator", reason="Inspect")
    with case.kernel.database.read_transaction() as c, pytest.raises(NotFoundError):
        ProjectExecutionRecovery(case.kernel.database, "other-subject").load(c, identifier)
    # The snapshot includes phase and project revisions, not just execution ID.
    phase = manager.phases(manager.projects()[0].project_id)[0]
    with case.kernel.database.transaction() as c:
        manager._revise_phase_connection(
            c,
            phase,
            status=phase.status,
            attempt_count=phase.attempt_count + 1,
            no_progress_count=phase.no_progress_count,
            reason="New valid local revision",
            evidence_event_ids=(),
            now=case.clock_value,
        )
    with pytest.raises(OperatorControlConflict, match="snapshot_conflict"):
        controls.release_project_execution_review(
            identifier,
            expected_fingerprint=item["expected_fingerprint"],
            actor="operator",
            reason="Old browser",
        )


@pytest.mark.asyncio
async def test_new_ambiguous_work_after_release_still_blocks_review_commit(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    manager, identifier = await interrupted(case)
    issue(case, identifier)
    OperatorControlService(case.kernel).resume(actor="operator", reason="Review")
    case.clock_value = "2026-08-14T13:00:00.000+00:00"
    context = manager._review_context(manager.projects()[0])
    from noyra.cognition.projects import AutonomousProjectValidationError
    from noyra.cognition.types import AutonomousProjectReviewProposal
    from noyra.core.types import canonical_json

    proposal = AutonomousProjectReviewProposal.model_validate_json(
        canonical_json(review(case, manager, "continue"))
    )
    manager.gateway.ledger.prepare_call(
        case.subject_id, "fake", "fixture", "world_cognition:late", content_hash("late"), "late"
    )
    with pytest.raises(AutonomousProjectValidationError, match="no longer permits"):
        manager._commit_review(proposal, "never-dispatched", "blocked-commit", context)
    assert manager.phases(manager.projects()[0].project_id)[0].attempt_count == 1


@pytest.mark.asyncio
async def test_release_receipt_survives_cold_archive_and_restart(
    case: helpers.AutonomousProjectsTestCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64
    from datetime import datetime, timedelta
    from unittest.mock import patch

    from noyra.cognition.execution_recovery import release_event_id
    from noyra.core.database import Database
    from noyra.core.event_archive import EventPayloadArchive
    from noyra.core.events import EventStore

    _, identifier = await interrupted(case)
    issue(case, identifier)
    db = case.kernel.database
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"x" * 32).decode())

    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> FutureDateTime:
            future = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(future.timestamp(), tz)

    archive = EventPayloadArchive(db, db.path.parent / "subject/cold", subject_id=case.subject_id)
    with patch("noyra.core.event_archive.datetime", FutureDateTime):
        assert archive.archive_cold(case.subject_id, older_than_days=90, limit=100) > 0
    with db.connection() as connection:
        event = connection.execute(
            "SELECT payload_json,payload_archive_key FROM events WHERE event_id=?",
            (release_event_id(case.subject_id, identifier),),
        ).fetchone()
        assert event[0] == "{}" and event[1] is not None
    fresh = Database(db.path, initialize=False)
    assert ProjectExecutionRecovery(fresh, case.subject_id).released_for_review(identifier)
    ProjectExecutionLedger(fresh, case.subject_id).verify_integrity()
    EventStore(fresh).verify_chain(case.subject_id)
    # Reopen the real runtime, preserving its durable manual pause.
    from noyra.core.runtime import SubjectKernel

    case.kernel.close()
    case.kernel = SubjectKernel(
        db.path, case.subject_id, content_hash({"seed": "autonomous-project-test"})
    )
    assert case.kernel.boot().state == "paused"
    OperatorControlService(case.kernel).resume(actor="operator", reason="Independent review")
    manager = AutonomousProjectManager(
        case.kernel.database,
        case.subject_id,
        case.gateway([])[0],
        case.settings(),
        clock=lambda: case.clock_value,
    )
    manager.gateway, provider = case.gateway([review(case, manager, "continue")])
    assert await manager.run_due() == "autonomous_project_continue"
    assert len(provider.requests) == 1
    assert ProjectExecutionLedger(case.kernel.database, case.subject_id).get(identifier).status == (
        "unknown"
    )


@pytest.mark.asyncio
async def test_prepared_work_is_not_discarded_and_operator_cannot_impersonate_subject(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.core.operator_controls import OperatorControlConflict

    _, identifier = await interrupted(case, "prepared")
    controls = OperatorControlService(case.kernel)
    controls.pause(actor="operator", reason="Check ordinary prepared work")
    recovery = ProjectExecutionRecovery(case.kernel.database, case.subject_id)
    with case.kernel.database.read_transaction() as c:
        row, project, phase = recovery.load(c, identifier)
        fingerprint = recovery.fingerprint(row, project, phase)
    with pytest.raises(OperatorControlConflict, match="unsupported_or_stale"):
        controls.release_project_execution_review(
            identifier, expected_fingerprint=fingerprint, actor="operator", reason="No discard"
        )
    with pytest.raises(OperatorControlConflict, match="operator_authorization_required"):
        controls.release_project_execution_review(
            identifier, expected_fingerprint=fingerprint, actor="subject", reason="No spoof"
        )
    assert ProjectExecutionLedger(case.kernel.database, case.subject_id).get(identifier).status == (
        "prepared"
    )
    assert not controls.project_execution_recovery()["records"]


@pytest.mark.asyncio
async def test_unreviewed_executing_attempt_does_not_retire_recovery_guard(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from datetime import datetime, timedelta

    from noyra.cognition.recovery import AutonomyRecovery
    from noyra.core.recovery_budget import read_recovery, write_recovery
    from test_a0_recovery_budget import arm

    manager, _ = await interrupted(case, "executing")
    arm(case)
    with case.kernel.database.transaction() as c:
        state = read_recovery(c, case.subject_id)
        assert state is not None
        state["project_ids"] = [manager.projects()[0].project_id]
        write_recovery(c, case.subject_id, state, case.clock_value)
    recovery = AutonomyRecovery(
        case.kernel.database, case.subject_id, clock=lambda: case.clock_value
    )
    origin = datetime.fromisoformat(case.clock_value)
    for minute in range(0, 66, 5):
        case.clock_value = (origin + timedelta(minutes=minute)).isoformat()
        state = recovery.refresh()
        assert state is not None and state["active"] and state["stable_since"] is None


@pytest.mark.asyncio
async def test_phase_revision_change_rejects_review_result_after_release(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.cognition.projects import AutonomousProjectValidationError
    from noyra.cognition.types import AutonomousProjectReviewProposal
    from noyra.core.types import canonical_json

    manager, identifier = await interrupted(case)
    issue(case, identifier)
    context = manager._review_context(manager.projects()[0])
    proposal = AutonomousProjectReviewProposal.model_validate_json(
        canonical_json(review(case, manager, "continue"))
    )
    phase = manager.phases(manager.projects()[0].project_id)[0]
    with case.kernel.database.transaction() as c:
        manager._revise_phase_connection(
            c,
            phase,
            status=phase.status,
            attempt_count=phase.attempt_count,
            no_progress_count=phase.no_progress_count,
            reason="New phase evidence",
            evidence_event_ids=(),
            now=case.clock_value,
        )
    with pytest.raises(AutonomousProjectValidationError, match="no longer permits"):
        manager._commit_review(proposal, "never-dispatched", "stale-phase", context)
    assert manager.phases(manager.projects()[0].project_id)[0].attempt_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output_type", ["knowledge_collection", "software_prototype", "prediction_record"]
)
async def test_only_local_output_types_have_review_release(
    case: helpers.AutonomousProjectsTestCase, output_type: str
) -> None:
    from noyra.core.operator_controls import OperatorControlConflict

    manager, identifier = await interrupted(case, output_type=output_type)
    controls = OperatorControlService(case.kernel)
    controls.pause(actor="operator", reason="Inspect output type")
    item = controls.project_execution_recovery()["records"][0]
    if output_type == "prediction_record":
        assert not item["can_release"]
        with pytest.raises(OperatorControlConflict, match="unsupported_or_stale"):
            controls.release_project_execution_review(
                identifier,
                expected_fingerprint=item["expected_fingerprint"],
                actor="operator",
                reason="Predictions retain their dedicated recovery",
            )
        return
    assert item["can_release"]
    controls.release_project_execution_review(
        identifier,
        expected_fingerprint=item["expected_fingerprint"],
        actor="operator",
        reason="Verified local interruption",
    )
    controls.resume(actor="operator", reason="Review")
    manager.gateway, _ = case.gateway([review(case, manager, "continue")])
    assert await manager.run_due() == "autonomous_project_continue"
    ProjectExecutionLedger(case.kernel.database, case.subject_id).verify_integrity()


@pytest.mark.asyncio
async def test_released_executing_snapshot_cannot_accept_a_late_worker_update(
    case: helpers.AutonomousProjectsTestCase,
) -> None:
    from noyra.cognition.execution import ProjectExecutionError

    _, identifier = await interrupted(case, "executing")
    issue(case, identifier)
    ledger = ProjectExecutionLedger(case.kernel.database, case.subject_id)
    before = ledger.get(identifier)
    with pytest.raises(ProjectExecutionError, match="review-released"):
        ledger.transition(identifier, "unknown", reason="Late callback")
    assert ledger.get(identifier) == before
    ledger.verify_integrity()
