from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from noyra.autonomy import DurableWorkflowStore
from noyra.cognition import (
    AutonomousProjectManager,
    CognitionSettings,
    ProjectExecutionLedger,
    ProjectExecutionValidationError,
    ProjectPhaseExecutor,
    WorldSourceConfig,
)
from noyra.core import EventStore, IdentityStore, SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.integrity import IntegrityRegistry
from noyra.core.types import canonical_json, content_hash, utc_now
from noyra.interaction import PublicProjection
from noyra.mind import GoalCandidate, GoalStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)
from noyra.sleep import SleepEngine
from noyra.world import ObservationStore, PredictionProposal, PredictionStore, SourceRegistry
from noyra.world.types import FetchedDocument


class AutonomousProjectsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.subject_id = "Noyra-project-test"
        self.kernel = SubjectKernel(
            Path(self.temp_dir.name) / "noyra.sqlite3",
            self.subject_id,
            content_hash({"seed": "autonomous-project-test"}),
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.clock_value = "2026-08-14T12:00:00.000+00:00"
        self.event = EventStore(self.kernel.database).append(
            self.subject_id,
            "world_evidence",
            "world",
            {"theme": "continuity", "observation": "a stable public pattern"},
            occurred_at=self.clock_value,
        )
        candidate = GoalStore(self.kernel.database).create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Track evidence about continuity",
                description="Compare a bounded set of public observations over time.",
                origin="self",
                priority=0.8,
                commitment=0.75,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(self.event.event_id,),
            reason="project fixture goal",
        )
        self.goal = GoalStore(self.kernel.database).activate(
            candidate.goal_id,
            rationale="the bounded investigation remains worthwhile",
            causal_source_ids=(self.event.event_id,),
        )

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def test_phase_parser_rejects_blob_durable_json(self) -> None:
        with self.assertRaises(IntegrityError):
            AutonomousProjectManager._phase_from_row(
                {
                    "phase_id": "phase-corrupt",
                    "acceptance_criteria_json": b"[]",
                }
            )

    def settings(self, **updates: object) -> CognitionSettings:
        base = CognitionSettings(
            enabled=True,
            sources=(
                WorldSourceConfig(
                    name="Project fixture source",
                    url="https://example.com/projects",
                    source_type="web",
                ),
            ),
            project_formation_interval_seconds=3_600,
            project_review_interval_seconds=300,
            max_project_model_calls_per_day=8,
            max_project_context_chars=48_000,
            max_project_no_progress_reviews=2,
        )
        return base.model_copy(update=updates)

    def gateway(self, payloads: list[Mapping[str, object]]) -> tuple[ModelGateway, FakeProvider]:
        provider = FakeProvider(
            [
                ProviderResponse(content=json.dumps(payload), usage=ModelUsage(500, 250))
                for payload in payloads
            ]
        )
        gateway = ModelGateway(
            provider,
            ModelLedger(self.kernel.database),
            model="project-fixture",
            limits=BudgetLimits(40, 500_000, 200_000, 5_000_000),
            resource_pool="economy",
        )
        return gateway, provider

    def formation(self, *, goal_id: str | None = None) -> dict[str, object]:
        return {
            "disposition": "form",
            "summary": "A short evidence-tracking project fits the present goal.",
            "goal_id": goal_id or self.goal.goal_id,
            "project_type": "prediction",
            "title": "Continuity signal forecast",
            "purpose": "Test one bounded expectation against public evidence.",
            "deliverable": "A prediction record with sources and a final calibration note.",
            "acceptance_criteria": [
                "One explicit probability is recorded before evidence collection.",
                "The final note cites durable observations and reports the result.",
            ],
            "size_class": "small",
            "estimated_duration_hours": 48.0,
            "budget": {
                "max_cycles": 6,
                "max_model_calls": 6,
                "max_searches": 4,
                "max_external_actions": 2,
                "max_storage_bytes": 100_000,
            },
            "phases": [
                {
                    "phase_key": "forecast",
                    "title": "Record forecast",
                    "objective": "State the event, horizon, probability and resolution rule.",
                    "output_type": "prediction_record",
                    "acceptance_criteria": ["A falsifiable probability statement exists."],
                    "dependency_keys": [],
                },
                {
                    "phase_key": "review",
                    "title": "Review evidence",
                    "objective": "Compare the forecast with observed evidence.",
                    "output_type": "research_note",
                    "acceptance_criteria": ["The note cites evidence and records uncertainty."],
                    "dependency_keys": ["forecast"],
                },
            ],
            "source_event_ids": [self.event.event_id],
            "source_value_ids": [],
            "source_mission_id": None,
        }

    async def form_project(self) -> tuple[AutonomousProjectManager, FakeProvider]:
        gateway, provider = self.gateway([self.formation()])
        manager = AutonomousProjectManager(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await manager.run_due(), "autonomous_project_planned")
        return manager, provider

    def record_analyzed_observation(self) -> None:
        source = SourceRegistry(self.kernel.database).register(
            self.subject_id,
            "Project execution evidence",
            "https://example.com/project-execution-evidence",
            "web",
            trust_score=0.8,
            status="active",
            reason="validated project execution fixture",
        )
        content = "A stable public observation supports the bounded forecast."
        observation, _ = ObservationStore(self.kernel.database).record(
            self.subject_id,
            source.source_id,
            FetchedDocument(
                url=source.url,
                title="Project evidence",
                content=content,
                content_hash=content_hash(content),
                media_type="text/plain",
                injection_signals=(),
                etag=None,
                last_modified=None,
                fetched_at="2026-08-14T15:30:00.000+00:00",
            ),
        )
        ObservationStore(self.kernel.database).mark(
            observation.observation_id, "analyzed", subject_id=self.subject_id
        )

    async def request_help(self, manager: AutonomousProjectManager) -> None:
        project = manager.projects()[0]
        phase = manager.phases(project.project_id)[0]
        self.clock_value = "2026-08-14T13:00:00.000+00:00"
        manager.gateway = self.gateway(
            [
                {
                    "project_id": project.project_id,
                    "phase_id": phase.phase_id,
                    "disposition": "request_help",
                    "summary": "One bounded external clarification is needed.",
                    "reason": "The current phase needs a concrete missing input.",
                    "evidence_event_ids": [self.event.event_id],
                    "revised_budget": None,
                    "help_title": "Clarify the resolution evidence",
                    "help_description": "Provide one public source that resolves the forecast.",
                    "help_public_summary": "A public resolution source is needed.",
                }
            ]
        )[0]
        self.assertEqual(await manager.run_due(), "autonomous_project_request_help")

    def assert_project_registry_p0(self) -> None:
        report = IntegrityRegistry().run(
            self.kernel.database,
            self.subject_id,
            Path(self.temp_dir.name),
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("cognition.autonomous_projects",),
        )
        self.assertEqual(report.status, "corrupt")
        self.assertEqual(report.p0, ("cognition.autonomous_projects:integrity_error",))
        self.assertEqual(report.p1, ())

    async def test_forms_bounded_project_and_exposes_safe_projection(self) -> None:
        manager, provider = await self.form_project()
        project = manager.projects()[0]
        phases = manager.phases(project.project_id)
        self.assertEqual(project.status, "planned")
        self.assertEqual(project.current_phase_id, phases[0].phase_id)
        self.assertEqual([phase.status for phase in phases], ["active", "pending"])
        self.assertIn(
            "BEGIN_PRIVATE_PROJECT_FORMATION_CONTEXT",
            provider.requests[0].messages[1].content,
        )
        public = PublicProjection(self.kernel.database).projects_view(self.subject_id)
        self.assertEqual(public[0]["project_id"], project.project_id)
        self.assertEqual(public[0]["current_phase_title"], "Record forecast")
        self.assertNotIn("source_event_ids", public[0])
        state = PublicProjection(self.kernel.database).private_state(self.subject_id)
        self.assertEqual(state["project_summary"]["project_count"], 1)
        self.assertEqual(manager.verify_integrity()["autonomous_projects"], 1)

    async def test_review_activates_and_completes_ordered_phases(self) -> None:
        manager, _ = await self.form_project()
        project = manager.projects()[0]
        phase = manager.phases(project.project_id)[0]
        self.clock_value = "2026-08-14T14:01:00.000+00:00"
        activation = {
            "project_id": project.project_id,
            "phase_id": phase.phase_id,
            "disposition": "activate",
            "summary": "The first phase is ready.",
            "reason": "The goal and resource envelope remain available.",
            "evidence_event_ids": [self.event.event_id],
            "revised_budget": None,
            "help_title": None,
            "help_description": None,
            "help_public_summary": None,
        }
        manager.gateway = self.gateway([activation])[0]
        self.assertEqual(await manager.run_due(), "autonomous_project_activate")
        self.record_analyzed_observation()
        with self.kernel.database.connection() as connection:
            observation_id = str(
                connection.execute(
                    "SELECT observation_id FROM observations WHERE subject_id = ? "
                    "AND status = 'analyzed' ORDER BY fetched_at DESC LIMIT 1",
                    (self.subject_id,),
                ).fetchone()["observation_id"]
            )
        calibration = PredictionStore(self.kernel.database)
        for index in range(3):
            calibration_prediction = calibration.create(
                self.subject_id,
                PredictionProposal(
                    statement=f"Calibration outcome {index}",
                    probability=0.6,
                    target_at="2099-01-01T00:00:00.000+00:00",
                    resolution_criteria="The calibration observation remains analyzed.",
                ),
                evidence_observation_ids=(observation_id,),
                rationale="fixture calibration outcome",
                idempotency_key=f"autonomous-project-calibration-{index}",
            )
            calibration.resolve(
                calibration_prediction.prediction_id,
                outcome=True,
                evidence_observation_ids=(observation_id,),
                rationale="fixture calibration outcome",
            )
        activated = manager.get(project.project_id)
        active_phase = manager.phases(project.project_id)[0]
        executor = ProjectPhaseExecutor(
            self.kernel.database,
            self.subject_id,
            Mock(),
        )
        execution = await executor.run_phase(activated, active_phase)
        self.assertEqual(execution.status, "executing")
        prediction_id = str(execution.acceptance["evidence"]["prediction_id"])
        target_at = str(execution.acceptance["evidence"]["target_at"])
        executor.predictions._clock = lambda: (
            (datetime.fromisoformat(target_at) + timedelta(seconds=1))
            .astimezone(UTC)
            .isoformat(timespec="milliseconds")
        )
        executor.predictions.resolve(
            prediction_id,
            outcome=True,
            evidence_observation_ids=(observation_id,),
            rationale="fixture measured target outcome",
        )
        execution = await executor.run_phase(activated, active_phase)
        self.assertEqual(execution.status, "succeeded")
        self.clock_value = "2026-08-14T16:02:00.000+00:00"
        complete = {
            **activation,
            "disposition": "complete_phase",
            "summary": "The forecast record now exists as durable evidence.",
            "reason": "The supplied event documents the accepted phase result.",
        }
        manager.gateway = self.gateway([complete])[0]
        self.assertEqual(await manager.run_due(), "autonomous_project_complete_phase")
        updated = manager.get(project.project_id)
        phases = manager.phases(project.project_id)
        self.assertEqual(updated.status, "active")
        self.assertEqual(updated.current_phase_id, phases[1].phase_id)
        self.assertEqual([item.status for item in phases], ["completed", "active"])

    async def test_rejects_forged_goal_and_oversized_budget(self) -> None:
        invalid = self.formation(goal_id="goal_forged")
        budget = invalid["budget"]
        assert isinstance(budget, dict)
        budget["max_model_calls"] = 48
        gateway, _ = self.gateway([invalid])
        manager = AutonomousProjectManager(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(max_project_model_calls=8),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await manager.run_due(), "autonomous_project_rejected")
        self.assertEqual(manager.projects(), [])

    async def test_active_duration_deadline_abandons_project_without_new_model_call(self) -> None:
        manager, provider = await self.form_project()
        project = manager.projects()[0]
        manager._apply_local_transition(
            project,
            "active",
            "fixture activation",
            reason_code="fixture",
        )
        session = manager.begin_execution(project.project_id, "phase_execution")
        self.clock_value = "2026-08-16T11:59:59.000+00:00"
        manager.finish_execution(project.project_id, session)
        self.assertFalse(manager._expire_overdue_projects())
        self.assertEqual(manager.get(project.project_id).status, "active")

        session = manager.begin_execution(project.project_id, "project_review")
        self.clock_value = "2026-08-16T12:00:01.000+00:00"
        manager.finish_execution(project.project_id, session)
        self.assertTrue(manager._expire_overdue_projects())
        self.assertEqual(manager.get(project.project_id).status, "abandoned")
        self.assertEqual(len(provider.requests), 1)
        with self.kernel.database.connection() as connection:
            event = connection.execute(
                "SELECT payload_json FROM events WHERE event_type = 'autonomous_project_bounded' "
                "ORDER BY occurred_at DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(event)
        assert event is not None
        self.assertIn("active_execution_deadline_reached", str(event["payload_json"]))

    async def test_execution_clock_recovery_discards_offline_pause_and_sleep_time(self) -> None:
        manager, _ = await self.form_project()
        project = manager.projects()[0]
        manager._apply_local_transition(
            project,
            "active",
            "fixture activation",
            reason_code="fixture",
        )
        manager.begin_execution(project.project_id, "phase_execution")
        self.clock_value = "2026-08-20T12:00:00.000+00:00"
        restarted = AutonomousProjectManager(
            self.kernel.database,
            self.subject_id,
            manager.gateway,
            manager.settings,
            clock=lambda: self.clock_value,
        )
        self.assertEqual(restarted.active_execution_seconds(project.project_id), 0.0)

        current = restarted.get(project.project_id)
        restarted._apply_local_transition(
            current,
            "paused",
            "fixture pause",
            reason_code="fixture_pause",
        )
        sleep = SleepEngine(self.kernel.database, self.subject_id).start(
            "subject_choice", "fixture sleep"
        )
        restarted.reflect_for_sleep(sleep.sleep_id)
        self.clock_value = "2026-09-20T12:00:00.000+00:00"
        self.assertFalse(restarted._expire_overdue_projects())
        self.assertEqual(restarted.active_execution_seconds(project.project_id), 0.0)

        restarted._apply_local_transition(
            restarted.get(project.project_id),
            "active",
            "fixture resume",
            reason_code="fixture_resume",
        )
        session = restarted.begin_execution(project.project_id, "project_review")
        self.clock_value = "2026-09-20T12:01:00.000+00:00"
        restarted.finish_execution(project.project_id, session)
        self.assertEqual(restarted.active_execution_seconds(project.project_id), 60.0)
        self.assertEqual(
            restarted.verify_integrity()["autonomous_project_execution_clock_events"], 4
        )

    async def test_planned_blocked_and_paused_wall_time_do_not_consume_deadline(self) -> None:
        manager, _ = await self.form_project()
        project = manager.projects()[0]
        self.clock_value = "2026-09-14T12:00:00.000+00:00"
        self.assertFalse(manager._expire_overdue_projects())
        self.assertEqual(manager.active_execution_seconds(project.project_id), 0.0)

        manager._apply_local_transition(
            project,
            "blocked",
            "fixture dependency block",
            reason_code="fixture_block",
        )
        self.clock_value = "2026-10-14T12:00:00.000+00:00"
        self.assertFalse(manager._expire_overdue_projects())
        self.assertEqual(manager.active_execution_seconds(project.project_id), 0.0)

        manager._apply_local_transition(
            manager.get(project.project_id),
            "paused",
            "fixture pause",
            reason_code="fixture_pause",
        )
        self.clock_value = "2026-11-14T12:00:00.000+00:00"
        self.assertFalse(manager._expire_overdue_projects())
        self.assertEqual(manager.active_execution_seconds(project.project_id), 0.0)

    async def test_active_project_waiting_for_review_does_not_start_execution_clock(self) -> None:
        manager, _ = await self.form_project()
        project = manager.projects()[0]
        manager._apply_local_transition(
            project,
            "active",
            "fixture activation",
            reason_code="fixture",
        )
        self.assertIsNone(await manager.run_due())
        self.assertEqual(manager.active_execution_seconds(project.project_id), 0.0)
        with self.kernel.database.connection() as connection:
            events = connection.execute(
                "SELECT COUNT(*) FROM autonomous_project_execution_clock_events "
                "WHERE project_id = ?",
                (project.project_id,),
            ).fetchone()[0]
        self.assertEqual(events, 0)

    async def test_project_duration_rejects_sub_hour_active_budget(self) -> None:
        formation = self.formation()
        formation["estimated_duration_hours"] = 0.5
        gateway, _ = self.gateway([formation])
        manager = AutonomousProjectManager(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await manager.run_due(), "autonomous_project_rejected")
        self.assertEqual(manager.projects(), [])

    async def test_local_no_progress_guard_pauses_without_another_model_call(self) -> None:
        manager, _ = await self.form_project()
        project = manager.projects()[0]
        phase = manager.phases(project.project_id)[0]
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "DROP TRIGGER IF EXISTS prevent_autonomous_project_phase_revision_update"
            )
            connection.execute("DROP TRIGGER restrict_autonomous_project_phase_update")
            connection.execute(
                "UPDATE autonomous_project_phases SET no_progress_count = 2, state_hash = ? "
                "WHERE phase_id = ?",
                (
                    manager._phase_hash(
                        phase.project_id,
                        phase.phase_key,
                        phase.position,
                        phase.title,
                        phase.objective,
                        phase.output_type,
                        phase.acceptance_criteria,
                        phase.dependency_keys,
                        phase.status,
                        phase.attempt_count,
                        2,
                        phase.current_revision,
                    ),
                    phase.phase_id,
                ),
            )
            connection.execute(
                "UPDATE autonomous_project_phase_revisions SET no_progress_count = 2, "
                "state_hash = ? WHERE phase_id = ? AND revision_number = ?",
                (
                    manager._phase_revision_hash(
                        phase.status,
                        phase.attempt_count,
                        2,
                        "project formation",
                        (self.event.event_id,),
                    ),
                    phase.phase_id,
                    phase.current_revision,
                ),
            )
        # Planned projects first activate; the guard applies once work is active.
        manager._apply_local_transition(
            project,
            "active",
            "fixture activation",
            reason_code="fixture",
        )
        manager.gateway, provider = self.gateway([])
        self.assertEqual(await manager.run_due(), "autonomous_project_paused")
        self.assertEqual(provider.requests, [])

    async def test_sleep_records_project_review_and_histories_are_append_only(self) -> None:
        manager, _ = await self.form_project()
        project = manager.projects()[0]
        sleep = SleepEngine(self.kernel.database, self.subject_id)
        run = sleep.start("subject_choice", "review project state")
        self.assertEqual(manager.reflect_for_sleep(run.sleep_id), 1)
        self.assertEqual(manager.reflect_for_sleep(run.sleep_id), 0)
        with self.kernel.database.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM autonomous_project_sleep_reflections"
                ).fetchone()[0],
                1,
            )
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute(
                "DELETE FROM autonomous_project_revisions WHERE project_id = ?",
                (project.project_id,),
            )
        with self.kernel.database.connection() as connection:
            connection.execute("DROP TRIGGER prevent_autonomous_project_revision_update")
            connection.execute(
                "UPDATE autonomous_project_revisions SET reason = 'tampered' WHERE project_id = ?",
                (project.project_id,),
            )
            connection.commit()
        with self.assertRaises(IntegrityError):
            manager.verify_integrity()

    async def test_integrity_covers_project_resource_assistance_and_sleep_domains(self) -> None:
        manager, _ = await self.form_project()
        initial = manager.verify_integrity()
        self.assertEqual(initial["autonomous_project_resource_uses"], 1)
        self.assertEqual(initial["autonomous_project_assistance_requests"], 0)
        self.assertEqual(initial["autonomous_project_sleep_reflections"], 0)

        await self.request_help(manager)
        sleep = SleepEngine(self.kernel.database, self.subject_id).start(
            "subject_choice", "review blocked project"
        )
        self.assertEqual(manager.reflect_for_sleep(sleep.sleep_id), 1)

        verified = manager.verify_integrity()
        self.assertEqual(verified["autonomous_project_resource_uses"], 3)
        self.assertEqual(verified["autonomous_project_assistance_requests"], 1)
        self.assertEqual(verified["autonomous_project_sleep_reflections"], 1)

    async def test_fractional_project_resource_quantity_is_registry_p0(self) -> None:
        manager, _ = await self.form_project()
        project = manager.projects()[0]
        with self.kernel.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM autonomous_project_resource_uses WHERE project_id = ?",
                (project.project_id,),
            ).fetchone()
            assert row is not None
            payload = {
                "subject_id": row["subject_id"],
                "project_id": row["project_id"],
                "resource_type": row["resource_type"],
                "quantity": 1.5,
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "created_at": row["created_at"],
            }
            connection.execute("DROP TRIGGER prevent_autonomous_project_resource_use_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE autonomous_project_resource_uses SET quantity = 1.5, state_hash = ? "
                "WHERE usage_id = ?",
                (content_hash(payload), row["usage_id"]),
            )

        self.assert_project_registry_p0()

    async def test_orphan_project_storage_resource_is_registry_p0(self) -> None:
        manager, _ = await self.form_project()
        project = manager.projects()[0]
        created_at = "2026-08-14T13:00:00.000+00:00"
        payload = {
            "subject_id": self.subject_id,
            "project_id": project.project_id,
            "resource_type": "storage",
            "quantity": 1,
            "source_type": "artifact",
            "source_id": "execution-missing-artifact-source",
            "created_at": created_at,
        }
        with self.kernel.database.transaction() as connection:
            connection.execute(
                """INSERT INTO autonomous_project_resource_uses(
                    usage_id, subject_id, project_id, resource_type, quantity,
                    source_type, source_id, state_hash, created_at
                ) VALUES (?, ?, ?, 'storage', 1, 'artifact', ?, ?, ?)""",
                (
                    "projectuse_orphan_storage",
                    self.subject_id,
                    project.project_id,
                    payload["source_id"],
                    content_hash(payload),
                    created_at,
                ),
            )

        self.assert_project_registry_p0()

    async def test_foreign_project_assistance_owner_is_registry_p0(self) -> None:
        manager, _ = await self.form_project()
        project = manager.projects()[0]
        phase = manager.phases(project.project_id)[0]
        foreign_subject = "Noyra-project-assistance-foreign"
        IdentityStore(self.kernel.database).ensure(
            foreign_subject, content_hash({"subject": foreign_subject})
        )
        created_at = "2026-08-14T13:00:00.000+00:00"
        payload = {
            "subject_id": foreign_subject,
            "project_id": project.project_id,
            "phase_id": phase.phase_id,
            "request_kind": "technical_support",
            "title": "Foreign assistance binding",
            "description": "This row binds another subject to the local project.",
            "public_summary": "Invalid cross-subject project assistance.",
            "status": "resolved",
            "created_at": created_at,
            "updated_at": created_at,
        }
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "DROP TRIGGER "
                "validate_autonomous_project_assistance_requests_project_id_subject_insert"
            )
            connection.execute(
                "DROP TRIGGER "
                "validate_autonomous_project_assistance_requests_phase_id_subject_insert"
            )
            connection.execute(
                """INSERT INTO autonomous_project_assistance_requests(
                    request_id, subject_id, project_id, phase_id, request_kind, title,
                    description, public_summary, status, state_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "projecthelp_foreign_owner",
                    foreign_subject,
                    project.project_id,
                    phase.phase_id,
                    payload["request_kind"],
                    payload["title"],
                    payload["description"],
                    payload["public_summary"],
                    payload["status"],
                    content_hash(payload),
                    created_at,
                    created_at,
                ),
            )

        self.assert_project_registry_p0()

    async def test_noncanonical_project_sleep_risks_are_registry_p0(self) -> None:
        manager, _ = await self.form_project()
        sleep = SleepEngine(self.kernel.database, self.subject_id).start(
            "subject_choice", "verify project reflection"
        )
        self.assertEqual(manager.reflect_for_sleep(sleep.sleep_id), 1)
        with self.kernel.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_autonomous_project_sleep_reflection_update")
            connection.execute(
                "UPDATE autonomous_project_sleep_reflections "
                "SET unresolved_risks_json = '[ ]' WHERE sleep_id = ?",
                (sleep.sleep_id,),
            )

        self.assert_project_registry_p0()

    async def test_project_identity_and_phase_definition_are_immutable(self) -> None:
        manager, _ = await self.form_project()
        project = manager.projects()[0]
        phase = manager.phases(project.project_id)[0]
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute(
                "UPDATE autonomous_projects SET title = 'forged' WHERE project_id = ?",
                (project.project_id,),
            )
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute(
                "UPDATE autonomous_project_phases SET objective = 'forged' WHERE phase_id = ?",
                (phase.phase_id,),
            )

    async def test_project_execution_is_idempotent_append_only_and_acceptance_bound(self) -> None:
        manager, _ = await self.form_project()
        project = manager.projects()[0]
        phase = manager.phases(project.project_id)[0]
        ledger = ProjectExecutionLedger(self.kernel.database, self.subject_id)
        first = ledger.get_or_prepare(project, phase, workflow="autonomous_research")
        repeated = ledger.get_or_prepare(project, phase, workflow="autonomous_research")
        self.assertEqual(first.execution_id, repeated.execution_id)
        executing = ledger.transition(
            first.execution_id,
            "executing",
            reason="fixture started",
        )
        self.assertEqual(executing.status, "executing")
        with self.assertRaises(ProjectExecutionValidationError):
            ledger.transition(
                first.execution_id,
                "succeeded",
                evidence={"acceptance_record": "fixture", "result_count": 1},
                reason="fixture acceptance evidence",
            )
        completed = ledger.transition(
            first.execution_id,
            "failed",
            evidence={"acceptance_record": "fixture", "result_count": 0},
            error_code="fixture_not_validated",
            reason="fixture acceptance rejected",
        )
        self.assertEqual(completed.status, "failed")
        self.assertEqual(ledger.verify_integrity(), 1)
        with self.kernel.database.connection() as connection:
            row = connection.execute(
                "SELECT project_id, phase_id, status, acceptance_json "
                "FROM autonomous_project_executions WHERE execution_id = ?",
                (first.execution_id,),
            ).fetchone()
        self.assertEqual(row["project_id"], project.project_id)
        self.assertEqual(row["phase_id"], phase.phase_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(json.loads(row["acceptance_json"])["evidence"]["result_count"], 0)

    async def test_execution_integrity_parses_durable_acceptance_json(self) -> None:
        manager, _ = await self.form_project()
        project = manager.projects()[0]
        phase = manager.phases(project.project_id)[0]
        ledger = ProjectExecutionLedger(self.kernel.database, self.subject_id)
        execution = ledger.get_or_prepare(project, phase, workflow="autonomous_research")
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE autonomous_project_executions SET acceptance_json = '{' "
                "WHERE execution_id = ?",
                (execution.execution_id,),
            )
        with self.assertRaises(IntegrityError):
            ledger.verify_integrity()

    async def test_execution_integrity_covers_strict_workflow_checkpoints(self) -> None:
        workflows = DurableWorkflowStore(self.kernel.database)
        workflows.checkpoint(
            self.subject_id,
            "workflow-integrity",
            "project-phase",
            "running",
            {"step": 1},
            reason="workflow started",
        )
        workflows.checkpoint(
            self.subject_id,
            "workflow-integrity",
            "project-phase",
            "waiting",
            {"step": 2},
            reason="waiting for evidence",
        )
        workflows.resume(
            self.subject_id,
            "workflow-integrity",
            {"step": 3},
            reason="evidence arrived",
        )
        workflows.checkpoint(
            self.subject_id,
            "workflow-integrity",
            "project-phase",
            "failed",
            {"step": 4},
            reason="bounded attempt failed",
        )
        workflows.resume(
            self.subject_id,
            "workflow-integrity",
            {"step": 5},
            reason="explicit retry",
        )
        ledger = ProjectExecutionLedger(self.kernel.database, self.subject_id)
        self.assertEqual(ledger.verify_integrity(), 0)

        with self.kernel.database.transaction() as connection:
            row = connection.execute(
                "SELECT state_json FROM workflow_checkpoints WHERE workflow_id = ? "
                "AND checkpoint_version = 5",
                ("workflow-integrity",),
            ).fetchone()
            connection.execute(
                "UPDATE workflow_checkpoints SET state_json = ? WHERE workflow_id = ? "
                "AND checkpoint_version = 5",
                (row["state_json"].encode("utf-8"), "workflow-integrity"),
            )
        with self.assertRaises(IntegrityError):
            ledger.verify_integrity()

    async def test_execution_integrity_rejects_workflow_history_and_subject_damage(
        self,
    ) -> None:
        workflows = DurableWorkflowStore(self.kernel.database)
        workflows.checkpoint(
            self.subject_id,
            "workflow-history",
            "project-phase",
            "running",
            {"step": 1},
            reason="workflow started",
        )
        workflows.checkpoint(
            self.subject_id,
            "workflow-history",
            "project-phase",
            "completed",
            {"step": 2},
            reason="workflow completed",
        )
        forged_state = {"step": 3}
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO workflow_checkpoints(workflow_id, subject_id, workflow_type, "
                "checkpoint_version, status, state_json, state_hash, reason, created_at) "
                "VALUES (?, ?, ?, 3, 'running', ?, ?, ?, ?)",
                (
                    "workflow-history",
                    self.subject_id,
                    "project-phase",
                    canonical_json(forged_state),
                    content_hash(forged_state),
                    "forged terminal retry",
                    utc_now(),
                ),
            )
        ledger = ProjectExecutionLedger(self.kernel.database, self.subject_id)
        with self.assertRaises(IntegrityError):
            ledger.verify_integrity()

        with self.kernel.database.transaction() as connection:
            connection.execute(
                "DELETE FROM workflow_checkpoints WHERE workflow_id = ? AND checkpoint_version = 3",
                ("workflow-history",),
            )
        workflows.checkpoint(
            self.subject_id,
            "workflow-owner",
            "project-phase",
            "running",
            {"step": 1},
            reason="owned workflow",
        )
        other_subject = "Noyra-project-other-subject"
        IdentityStore(self.kernel.database).ensure(
            other_subject, content_hash({"seed": "project-other-subject"})
        )
        other_state = {"step": 2}
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO workflow_checkpoints(workflow_id, subject_id, workflow_type, "
                "checkpoint_version, status, state_json, state_hash, reason, created_at) "
                "VALUES (?, ?, ?, 2, 'running', ?, ?, ?, ?)",
                (
                    "workflow-owner",
                    other_subject,
                    "project-phase",
                    canonical_json(other_state),
                    content_hash(other_state),
                    "cross-subject checkpoint",
                    utc_now(),
                ),
            )
        with self.assertRaises(IntegrityError):
            ledger.verify_integrity()


if __name__ == "__main__":
    unittest.main()
