from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from noyra.capability import CapabilityGrant, CapabilityStore
from noyra.cognition import (
    AutonomousProjectManager,
    AutonomousProjectPhaseRecord,
    AutonomousProjectRecord,
    CognitionSettings,
    ProjectExecutionError,
    ProjectExecutionLedger,
    ProjectExecutionRecord,
    ProjectPhaseExecutor,
    WorldSourceConfig,
)
from noyra.cognition.execution import ProjectExecutionValidationError, _run_prototype_tests
from noyra.core import Database, EventStore, IdentityStore, SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash
from noyra.mind import GoalCandidate, GoalStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)
from noyra.world import ObservationStore, PredictionProposal, PredictionStore, SourceRegistry
from noyra.world.types import FetchedDocument


class ProjectExecutionIntegrityTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "noyra.sqlite3"
        self.subject_id = "Noyra-p1-12"
        self.kernel = SubjectKernel(
            self.database_path,
            self.subject_id,
            content_hash({"seed": "m42-p1-12"}),
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.clock_value = "2026-08-17T01:00:00.000+00:00"
        self.event = EventStore(self.kernel.database).append(
            self.subject_id,
            "world_evidence",
            "world",
            {"observation": "bounded execution evidence"},
            occurred_at=self.clock_value,
        )
        candidate = GoalStore(self.kernel.database).create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Validate one bounded project output",
                description="Require executable acceptance before phase completion.",
                origin="self",
                priority=0.8,
                commitment=0.8,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(self.event.event_id,),
            reason="P1-12 fixture goal",
        )
        self.goal = GoalStore(self.kernel.database).activate(
            candidate.goal_id,
            rationale="The integrity fixture is bounded and inspectable.",
            causal_source_ids=(self.event.event_id,),
        )

    def tearDown(self) -> None:
        self.kernel.close()
        self.temporary.cleanup()

    def gateway(self, payloads: list[Mapping[str, object]]) -> ModelGateway:
        provider = FakeProvider(
            [
                ProviderResponse(content=json.dumps(payload), usage=ModelUsage(300, 150))
                for payload in payloads
            ]
        )
        return ModelGateway(
            provider,
            ModelLedger(self.kernel.database),
            model="p1-12-fixture",
            limits=BudgetLimits(40, 500_000, 200_000, 5_000_000),
            resource_pool="economy",
        )

    def settings(self) -> CognitionSettings:
        return CognitionSettings(
            enabled=True,
            sources=(
                WorldSourceConfig(
                    name="P1-12 source",
                    url="https://example.com/p1-12",
                    source_type="web",
                ),
            ),
            project_formation_interval_seconds=3_600,
            project_review_interval_seconds=300,
            max_project_model_calls_per_day=12,
            max_project_context_chars=64_000,
        )

    def formation(self, output_type: str, project_type: str) -> dict[str, object]:
        return {
            "disposition": "form",
            "summary": "A bounded executable acceptance fixture is warranted.",
            "goal_id": self.goal.goal_id,
            "project_type": project_type,
            "title": f"Validated {output_type}",
            "purpose": "Exercise one durable output validator.",
            "deliverable": "A canonical artifact joined to durable evidence.",
            "acceptance_criteria": ["The output passes its executable validator."],
            "size_class": "small",
            "estimated_duration_hours": 24.0,
            "budget": {
                "max_cycles": 6,
                "max_model_calls": 6,
                "max_searches": 4,
                "max_external_actions": 2,
                "max_storage_bytes": 100_000,
            },
            "phases": [
                {
                    "phase_key": "produce",
                    "title": "Produce output",
                    "objective": "Produce one inspectable result with durable provenance.",
                    "output_type": output_type,
                    "acceptance_criteria": ["A non-placeholder durable result exists."],
                    "dependency_keys": [],
                },
                {
                    "phase_key": "review",
                    "title": "Review output",
                    "objective": "Review the validated result and uncertainty.",
                    "output_type": "research_note",
                    "acceptance_criteria": ["The review cites durable evidence."],
                    "dependency_keys": ["produce"],
                },
            ],
            "source_event_ids": [self.event.event_id],
            "source_value_ids": [],
            "source_mission_id": None,
        }

    async def active_project(
        self, output_type: str, project_type: str
    ) -> tuple[AutonomousProjectRecord, AutonomousProjectPhaseRecord]:
        if project_type == "software_prototype":
            CapabilityStore(self.kernel.database).grant(
                self.subject_id,
                CapabilityGrant(
                    capability_type="filesystem_write",
                    scope={"root": str(self.root / "workspace" / self.subject_id)},
                    issuer="fixture-operator",
                    rate_limit_per_hour=10,
                    side_effect=True,
                ),
                actor="fixture-operator",
            )
        manager = AutonomousProjectManager(
            self.kernel.database,
            self.subject_id,
            self.gateway([self.formation(output_type, project_type)]),
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await manager.run_due(), "autonomous_project_planned")
        project = manager.projects()[0]
        phase = manager.phases(project.project_id)[0]
        self.clock_value = "2026-08-17T02:00:00.000+00:00"
        manager.gateway = self.gateway(
            [
                {
                    "project_id": project.project_id,
                    "phase_id": phase.phase_id,
                    "disposition": "activate",
                    "summary": "The bounded phase is ready.",
                    "reason": "Its resources and evidence remain available.",
                    "evidence_event_ids": [self.event.event_id],
                    "revised_budget": None,
                    "help_title": None,
                    "help_description": None,
                    "help_public_summary": None,
                }
            ]
        )
        self.assertEqual(await manager.run_due(), "autonomous_project_activate")
        return manager.get(project.project_id), manager.phases(project.project_id)[0]

    def record_observation(self) -> str:
        source = SourceRegistry(self.kernel.database).register(
            self.subject_id,
            "P1-12 execution source",
            "https://example.com/p1-12-execution",
            "web",
            trust_score=0.8,
            status="active",
            reason="P1-12 analyzed evidence",
        )
        content = "The bounded observation was independently recorded."
        observation, _ = ObservationStore(self.kernel.database).record(
            self.subject_id,
            source.source_id,
            FetchedDocument(
                url=source.url,
                title="P1-12 evidence",
                content=content,
                content_hash=content_hash(content),
                media_type="text/plain",
                injection_signals=(),
                etag=None,
                last_modified=None,
                fetched_at="2026-08-17T02:10:00.000+00:00",
            ),
        )
        ObservationStore(self.kernel.database).mark(
            observation.observation_id, "analyzed", subject_id=self.subject_id
        )
        return observation.observation_id

    async def prediction_execution(
        self,
    ) -> tuple[ProjectExecutionLedger, ProjectExecutionRecord]:
        project, phase = await self.active_project("prediction_record", "prediction")
        observation_id = self.record_observation()
        predictions = PredictionStore(self.kernel.database, clock=lambda: self.clock_value)
        for index in range(3):
            calibration = predictions.create(
                self.subject_id,
                PredictionProposal(
                    statement=f"Calibration outcome {index}",
                    probability=0.6,
                    target_at=f"2026-09-0{index + 1}T00:00:00.000+00:00",
                    resolution_criteria="The calibration observation remains analyzed.",
                ),
                evidence_observation_ids=(observation_id,),
                idempotency_key=f"p1-12-calibration-{index}",
            )
            predictions.resolve(
                calibration.prediction_id,
                outcome=True,
                evidence_observation_ids=(observation_id,),
                rationale="fixture calibration outcome",
            )
        executor = ProjectPhaseExecutor(self.kernel.database, self.subject_id, Mock())
        execution = await executor.run_phase(project, phase)
        self.assertEqual(execution.status, "executing")
        prediction_id = str(execution.acceptance["evidence"]["prediction_id"])
        target_at = str(execution.acceptance["evidence"]["target_at"])
        executor.predictions._clock = lambda: (
            datetime.fromisoformat(target_at) + timedelta(seconds=1)
        ).isoformat(timespec="milliseconds")
        executor.predictions.resolve(
            prediction_id,
            outcome=True,
            evidence_observation_ids=(observation_id,),
            rationale="fixture measured target outcome",
        )
        execution = await executor.run_phase(project, phase)
        self.assertEqual(execution.status, "succeeded")
        return executor.ledger, execution

    def reanchor_execution(self, ledger: ProjectExecutionLedger, execution_id: str) -> None:
        with self.kernel.database.transaction() as connection:
            connection.execute("DROP TRIGGER IF EXISTS prevent_project_execution_revision_update")
            row = connection.execute(
                "SELECT * FROM autonomous_project_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            record = ledger._from_row(row)
            result_hash = ledger._result_hash(
                record,
                status=record.status,
                research_id=record.research_id,
                action_id=record.action_id,
                model_call_id=record.model_call_id,
                artifact_path=record.artifact_path,
                artifact_hash=record.artifact_hash,
                acceptance=record.acceptance,
                error_code=record.error_code,
                updated_at=record.updated_at,
                completed_at=record.completed_at,
            )
            revision = connection.execute(
                "SELECT * FROM autonomous_project_execution_revisions WHERE execution_id = ? "
                "ORDER BY created_at DESC, revision_id DESC LIMIT 1",
                (execution_id,),
            ).fetchone()
            revision_state = content_hash(
                {
                    "execution_id": execution_id,
                    "status": revision["status"],
                    "result_hash": result_hash,
                    "reason": revision["reason"],
                    "created_at": revision["created_at"],
                }
            )
            connection.execute(
                "UPDATE autonomous_project_executions SET result_hash = ? WHERE execution_id = ?",
                (result_hash, execution_id),
            )
            connection.execute(
                "UPDATE autonomous_project_execution_revisions SET result_hash = ?, state_hash = ? "
                "WHERE revision_id = ?",
                (result_hash, revision_state, revision["revision_id"]),
            )

    async def test_prediction_result_restarts_with_joined_digests(self) -> None:
        ledger, execution = await self.prediction_execution()
        self.assertEqual(ledger.verified(execution.execution_id).status, "succeeded")
        artifact = Path(execution.artifact_path or "")
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), execution.artifact_hash)
        self.assertEqual(execution.acceptance["validator"]["status"], "passed")

        restarted = ProjectExecutionLedger(Database(self.database_path), self.subject_id)
        self.assertEqual(restarted.verify_integrity(), 1)
        self.assertEqual(
            restarted.verified(execution.execution_id).result_hash, execution.result_hash
        )

    async def test_execution_prepare_rejects_forged_subject_and_phase_bindings(self) -> None:
        project, phase = await self.active_project("self_experiment", "self_development")
        ledger = ProjectExecutionLedger(self.kernel.database, self.subject_id)
        with self.assertRaises(ProjectExecutionError):
            ledger.get_or_prepare(
                replace(project, subject_id="Noyra-p1-12-foreign"),
                phase,
                workflow="self_experiment",
            )
        with self.assertRaises(ProjectExecutionError):
            ledger.get_or_prepare(
                project,
                replace(phase, project_id="project-foreign"),
                workflow="self_experiment",
            )

    async def test_tamper_matrix_rejects_artifact_evidence_source_and_revision_damage(self) -> None:
        ledger, execution = await self.prediction_execution()
        artifact = Path(execution.artifact_path or "")
        original_payload = artifact.read_bytes()
        artifact.write_bytes(b'{"forged":true}')
        with self.assertRaises(IntegrityError):
            ledger.verify_integrity()
        artifact.write_bytes(original_payload)

        prediction_id = execution.acceptance["evidence"]["prediction_id"]
        foreign = "Noyra-p1-12-foreign"
        IdentityStore(self.kernel.database).ensure(foreign, content_hash({"subject": foreign}))
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE predictions SET subject_id = ? WHERE prediction_id = ?",
                (foreign, prediction_id),
            )
        with self.assertRaises(IntegrityError):
            ledger.verify_integrity()

        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE predictions SET subject_id = ? WHERE prediction_id = ?",
                (self.subject_id, prediction_id),
            )
            connection.execute("DROP TRIGGER prevent_project_execution_revision_delete")
            connection.execute(
                "DELETE FROM autonomous_project_execution_revisions WHERE execution_id = ? "
                "AND status = 'executing'",
                (execution.execution_id,),
            )
        with self.assertRaises(IntegrityError):
            ledger.verify_integrity()

    async def test_forged_evidence_fails_even_when_result_and_revision_hashes_are_reanchored(
        self,
    ) -> None:
        ledger, execution = await self.prediction_execution()
        artifact = Path(execution.artifact_path or "")
        acceptance = dict(execution.acceptance)
        evidence = dict(acceptance["evidence"])
        evidence["probability"] = 0.99
        acceptance["evidence"] = evidence
        acceptance["evidence_hash"] = content_hash(evidence)
        payload = canonical_json(evidence).encode("utf-8")
        artifact.write_bytes(payload)
        artifact_hash = hashlib.sha256(payload).hexdigest()
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE autonomous_project_executions SET acceptance_json = ?, artifact_hash = ? "
                "WHERE execution_id = ?",
                (canonical_json(acceptance), artifact_hash, execution.execution_id),
            )
        self.reanchor_execution(ledger, execution.execution_id)
        with self.assertRaises(IntegrityError):
            ledger.verify_integrity()

    async def test_prediction_resolution_evidence_requires_owned_analyzed_observations(
        self,
    ) -> None:
        ledger, execution = await self.prediction_execution()
        prediction_id = str(execution.acceptance["evidence"]["prediction_id"])
        prediction = PredictionStore(self.kernel.database).get(prediction_id)
        foreign = "Noyra-p1-12-resolution-foreign"
        IdentityStore(self.kernel.database).ensure(foreign, content_hash({"subject": foreign}))
        source = SourceRegistry(self.kernel.database).register(
            foreign,
            "Foreign resolution source",
            "https://example.com/p1-12-foreign-resolution",
            "web",
            trust_score=0.8,
            status="active",
            reason="foreign prediction evidence fixture",
        )
        content = "A foreign subject recorded an unrelated measured outcome."
        observation, _ = ObservationStore(self.kernel.database).record(
            foreign,
            source.source_id,
            FetchedDocument(
                url=source.url,
                title="Foreign resolution evidence",
                content=content,
                content_hash=content_hash(content),
                media_type="text/plain",
                injection_signals=(),
                etag=None,
                last_modified=None,
                fetched_at="2026-08-17T04:00:00.000+00:00",
            ),
        )
        ObservationStore(self.kernel.database).mark(
            observation.observation_id, "analyzed", subject_id=foreign
        )
        with self.kernel.database.transaction() as connection:
            review = connection.execute(
                "SELECT * FROM prediction_reviews WHERE prediction_id = ? "
                "AND resulting_status = 'resolved'",
                (prediction_id,),
            ).fetchone()
            connection.execute("DROP TRIGGER prevent_prediction_review_update")
            connection.execute(
                "UPDATE prediction_reviews SET evidence_observation_ids_json = ?, state_hash = ? "
                "WHERE review_id = ?",
                (
                    canonical_json([observation.observation_id]),
                    PredictionStore._review_hash(
                        bool(prediction.outcome),
                        (observation.observation_id,),
                        review["rationale"],
                        "resolved",
                        prediction.brier_score,
                    ),
                    review["review_id"],
                ),
            )
        forged = dict(execution.acceptance["evidence"])
        forged["resolution_evidence_observation_ids"] = [observation.observation_id]
        with self.kernel.database.connection() as connection:
            phase = connection.execute(
                "SELECT * FROM autonomous_project_phases WHERE phase_id = ?",
                (execution.phase_id,),
            ).fetchone()
            with self.assertRaises(ProjectExecutionValidationError):
                ledger._validate_prediction_connection(connection, execution, forged, phase)

    async def test_prediction_validator_rejects_unhashable_calibration_ids(self) -> None:
        ledger, execution = await self.prediction_execution()
        malformed = dict(execution.acceptance["evidence"])
        malformed["calibration_prediction_ids"] = [[], [], []]
        with self.kernel.database.connection() as connection:
            phase = connection.execute(
                "SELECT * FROM autonomous_project_phases WHERE phase_id = ?",
                (execution.phase_id,),
            ).fetchone()
            with self.assertRaises(ProjectExecutionValidationError):
                ledger._validate_prediction_connection(connection, execution, malformed, phase)

    async def test_missing_and_workspace_escaped_artifacts_are_rejected(self) -> None:
        ledger, execution = await self.prediction_execution()
        artifact = Path(execution.artifact_path or "")
        payload = artifact.read_bytes()
        artifact.unlink()
        with self.assertRaises(IntegrityError):
            ledger.verify_integrity()

        artifact.write_bytes(payload)
        outside = self.root / "outside-evidence.json"
        outside.write_bytes(payload)
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE autonomous_project_executions SET artifact_path = ? WHERE execution_id = ?",
                (str(outside), execution.execution_id),
            )
        self.reanchor_execution(ledger, execution.execution_id)
        with self.assertRaises(IntegrityError):
            ledger.verify_integrity()

    async def test_placeholder_software_and_baseline_only_experiment_cannot_succeed(self) -> None:
        project, phase = await self.active_project("software_prototype", "software_prototype")
        gateway = self.gateway(
            [
                {
                    "summary": "TODO placeholder",
                    "files": [{"path": "index.html", "content": "<h1>TODO</h1>"}],
                    "validation": ["The model claims this is complete."],
                }
            ]
        )
        software = await ProjectPhaseExecutor(
            self.kernel.database, self.subject_id, Mock(gateway=gateway)
        ).run_phase(project, phase)
        self.assertEqual(software.status, "blocked")
        self.assertEqual(software.error_code, "output_validation_failed")

    async def test_software_requires_and_records_bounded_declarative_tests(self) -> None:
        project, phase = await self.active_project("software_prototype", "software_prototype")
        gateway = self.gateway(
            [
                {
                    "summary": "A complete bounded prototype with executable assertions.",
                    "files": [
                        {"path": "index.html", "content": "<h1>Measured prototype</h1>"},
                        {
                            "path": "prototype-tests.json",
                            "content": json.dumps(
                                {
                                    "version": 1,
                                    "tests": [
                                        {
                                            "name": "title-present",
                                            "path": "index.html",
                                            "contains": "Measured prototype",
                                        }
                                    ],
                                },
                                separators=(",", ":"),
                            ),
                        },
                    ],
                    "validation": ["The declarative test manifest passed."],
                }
            ]
        )
        software = await ProjectPhaseExecutor(
            self.kernel.database, self.subject_id, Mock(gateway=gateway)
        ).run_phase(project, phase)
        self.assertEqual(software.status, "succeeded")
        validator = software.acceptance["validator"]
        self.assertIn("prototype_build", validator["checks"])
        self.assertIn("prototype_tests", validator["checks"])

    def test_vacuous_model_authored_negative_prototype_assertion_is_rejected(self) -> None:
        with self.assertRaises(ProjectExecutionValidationError):
            _run_prototype_tests(
                {
                    "index.html": b"<h1>Measured output</h1>",
                    "prototype-tests.json": canonical_json(
                        {
                            "version": 1,
                            "tests": [
                                {
                                    "name": "vacuous",
                                    "path": "index.html",
                                    "not_contains": "a string that is absent",
                                }
                            ],
                        }
                    ).encode("utf-8"),
                }
            )

    async def test_collaboration_is_artifact_bound_while_self_experiment_awaits_follow_up(
        self,
    ) -> None:
        project, phase = await self.active_project("collaboration_request", "collaboration")
        collaboration = await ProjectPhaseExecutor(
            self.kernel.database, self.subject_id, Mock()
        ).run_phase(project, phase)
        self.assertEqual(collaboration.status, "succeeded")
        self.assertTrue(Path(collaboration.artifact_path or "").is_file())

    async def test_baseline_only_self_experiment_awaits_independent_follow_up(self) -> None:
        project, phase = await self.active_project("self_experiment", "self_development")
        experiment = await ProjectPhaseExecutor(
            self.kernel.database, self.subject_id, Mock()
        ).run_phase(project, phase)
        self.assertEqual(experiment.status, "executing")
        self.assertEqual(experiment.error_code, "self_experiment_follow_up_pending")

    async def test_self_experiment_succeeds_only_after_measured_follow_up(self) -> None:
        project, phase = await self.active_project("self_experiment", "self_development")
        executor = ProjectPhaseExecutor(self.kernel.database, self.subject_id, Mock())
        baseline = await executor.run_phase(project, phase)
        self.assertEqual(baseline.status, "executing")
        follow_up = await executor.run_phase(project, phase)
        self.assertEqual(follow_up.status, "succeeded")
        evidence = follow_up.acceptance["evidence"]
        self.assertIn("follow_up_event_id", evidence)
        self.assertEqual(
            evidence["delta"],
            {
                key: evidence["follow_up"][key] - evidence["baseline"][key]
                for key in evidence["baseline"]
            },
        )
        self.assertEqual(executor.ledger.verify_integrity(), 1)

    async def test_self_experiment_events_are_bound_to_the_execution_phase(self) -> None:
        project, phase = await self.active_project("self_experiment", "self_development")
        executor = ProjectPhaseExecutor(self.kernel.database, self.subject_id, Mock())
        await executor.run_phase(project, phase)
        execution = await executor.run_phase(project, phase)
        baseline_id = str(execution.acceptance["evidence"]["baseline_event_id"])
        with self.kernel.database.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM events WHERE event_id = ?", (baseline_id,)
            ).fetchone()
            payload = json.loads(row["payload_json"])
            payload["project_id"] = "project-foreign"
            connection.execute("DROP TRIGGER prevent_event_immutable_update")
            connection.execute(
                "UPDATE events SET payload_json = ?, payload_hash = ? WHERE event_id = ?",
                (canonical_json(payload), content_hash(payload), baseline_id),
            )
        with self.kernel.database.connection() as connection:
            phase_row = connection.execute(
                "SELECT * FROM autonomous_project_phases WHERE phase_id = ?",
                (execution.phase_id,),
            ).fetchone()
            with self.assertRaises(ProjectExecutionValidationError):
                executor.ledger._validate_self_experiment_connection(
                    connection,
                    execution,
                    execution.acceptance["evidence"],
                    phase_row,
                )

    async def test_prediction_without_calibration_stays_open(self) -> None:
        project, phase = await self.active_project("prediction_record", "prediction")
        self.record_observation()
        executor = ProjectPhaseExecutor(self.kernel.database, self.subject_id, Mock())
        execution = await executor.run_phase(project, phase)
        self.assertEqual(execution.status, "executing")
        self.assertEqual(execution.error_code, "prediction_calibration_unavailable")
        self.assertEqual(execution.acceptance["evidence"]["status"], "pending_calibration")


if __name__ == "__main__":
    unittest.main()
