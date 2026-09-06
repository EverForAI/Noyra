from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from noyra.core.admission import OperationInvalidated, accounting_scope, assert_current_lease
from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.payload_codec import decompress_text
from noyra.core.types import canonical_json, content_hash, new_id, utc_now
from noyra.mind import GoalRecord, GoalStore
from noyra.model import ModelGateway, ModelMessage
from noyra.model.errors import (
    BudgetExhaustedError,
    ModelCallStateError,
    ProviderCallError,
    StructuredOutputError,
)
from noyra.sleep import FatigueInputs, FatigueTracker

from ._integrity import (
    durable_boundary,
    durable_float,
    durable_int,
    durable_json,
    durable_string_list,
)
from .settings import CognitionSettings
from .types import AutonomousProjectFormationProposal, AutonomousProjectReviewProposal

NONTERMINAL_PROJECT_STATUSES = ("planned", "active", "paused", "blocked")
TERMINAL_PROJECT_STATUSES = ("completed", "abandoned")
PROJECT_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"active", "paused", "blocked", "abandoned"}),
    "active": frozenset({"paused", "blocked", "completed", "abandoned"}),
    "paused": frozenset({"active", "blocked", "abandoned"}),
    "blocked": frozenset({"active", "paused", "abandoned"}),
    "completed": frozenset(),
    "abandoned": frozenset(),
}
PROJECT_RESOURCE_TYPES = frozenset({"model_call", "search", "external_action", "storage"})
PROJECT_ASSISTANCE_KINDS = frozenset({"human_help", "resource_access", "technical_support"})
PROJECT_ASSISTANCE_STATUSES = frozenset({"open", "resolved", "withdrawn"})
PROJECT_SLEEP_DISPOSITIONS = frozenset({"continue", "scale_down", "pause", "abandon"})
MIN_PROJECT_ACTIVE_DURATION_HOURS = 1.0


class AutonomousProjectValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectBudget:
    max_cycles: int
    max_model_calls: int
    max_searches: int
    max_external_actions: int
    max_storage_bytes: int


@dataclass(frozen=True)
class AutonomousProjectRecord:
    project_id: str
    subject_id: str
    goal_id: str
    project_type: str
    title: str
    purpose: str
    deliverable: str
    acceptance_criteria: tuple[str, ...]
    size_class: str
    estimated_duration_hours: float
    status: str
    progress: float
    current_phase_id: str | None
    budget: ProjectBudget
    current_revision: int
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True)
class AutonomousProjectPhaseRecord:
    phase_id: str
    project_id: str
    phase_key: str
    position: int
    title: str
    objective: str
    output_type: str
    acceptance_criteria: tuple[str, ...]
    dependency_keys: tuple[str, ...]
    status: str
    attempt_count: int
    no_progress_count: int
    current_revision: int
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True)
class AutonomousProjectReviewRecord:
    review_id: str
    subject_id: str
    project_id: str
    model_call_id: str
    disposition: str
    phase_id: str
    summary: str
    resulting_status: str
    created_at: str


@dataclass(frozen=True)
class _FormationContext:
    serialized: str
    goals: dict[str, GoalRecord]
    event_ids: frozenset[str]
    value_ids: frozenset[str]
    mission_ids: frozenset[str]


@dataclass(frozen=True)
class _ReviewContext:
    serialized: str
    project: AutonomousProjectRecord
    phases: dict[str, AutonomousProjectPhaseRecord]
    event_ids: frozenset[str]
    usage: dict[str, int]


class AutonomousProjectManager:
    """Form and govern small autonomous projects under durable local budgets."""

    def __init__(
        self,
        database: Database,
        subject_id: str,
        gateway: ModelGateway,
        settings: CognitionSettings,
        *,
        clock: Callable[[], str] = utc_now,
        defer_recovery: bool = False,
    ):
        self.database = database
        self.subject_id = subject_id
        self.gateway = gateway
        self.settings = settings
        self.clock = clock
        self.goals = GoalStore(database)
        self.events = EventStore(database)
        self.fatigue = FatigueTracker(database)
        if not defer_recovery:
            self._recover_execution_clocks()

    def recover_execution_clocks(self) -> None:
        """Recover interrupted clocks after the owning runtime passes startup gates."""
        self._recover_execution_clocks()

    def abort_execution(self, project_id: str, session_id: str) -> None:
        """Fence an interrupted execution without charging wall-clock time."""
        now = self.clock()
        with accounting_scope(), self.database.transaction() as connection:
            latest = connection.execute(
                "SELECT * FROM autonomous_project_execution_clock_events "
                "WHERE project_id = ? ORDER BY sequence DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if (
                latest is None
                or latest["subject_id"] != self.subject_id
                or latest["action"] != "start"
                or latest["session_id"] != session_id
            ):
                return
            sequence = int(latest["sequence"]) + 1
            reason = "execution invalidated at runtime boundary"
            state_hash = self._clock_event_hash(
                project_id,
                self.subject_id,
                sequence,
                session_id,
                str(latest["execution_kind"]),
                "recover",
                0.0,
                reason,
                now,
            )
            connection.execute(
                "INSERT INTO autonomous_project_execution_clock_events(clock_event_id, "
                "project_id, subject_id, sequence, session_id, execution_kind, action, "
                "active_delta_seconds, reason, state_hash, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, 'recover', 0, ?, ?, ?)",
                (
                    new_id("project-clock"),
                    project_id,
                    self.subject_id,
                    sequence,
                    session_id,
                    latest["execution_kind"],
                    reason,
                    state_hash,
                    now,
                ),
            )

    async def run_due(self) -> str | None:
        if self._expire_overdue_projects():
            return "autonomous_project_deadline_expired"
        project = self._selected_project()
        if project is None:
            return await self._form_due()
        return await self._run_selected_project(project)

    async def _run_selected_project(self, project: AutonomousProjectRecord) -> str | None:
        self._sync_resource_usage(project)
        project = self.get(project.project_id)
        local = self._local_resource_decision(project)
        if local is not None:
            return local
        if project.status in {"planned", "paused", "blocked"}:
            review_due = True
        else:
            review_due = self._review_due(project)
        if not review_due:
            return None
        session_id = (
            self.begin_execution(project.project_id, "project_review")
            if project.status == "active"
            else None
        )
        try:
            result = await self._execute_review(project)
        finally:
            if session_id is not None:
                try:
                    assert_current_lease()
                    self.finish_execution(project.project_id, session_id)
                except OperationInvalidated:
                    self.abort_execution(project.project_id, session_id)
                    raise
        if session_id is not None and self._expire_overdue_projects():
            return "autonomous_project_deadline_expired"
        return result

    async def _execute_review(self, project: AutonomousProjectRecord) -> str | None:
        context = self._review_context(project)
        review_number = self._review_count(project.project_id)
        purpose = f"autonomous_project_review:{project.project_id}:{review_number}"
        idempotency_key = f"autonomous-project-review:{project.project_id}:{review_number}"
        recovered = self._successful_review(purpose, context)
        if recovered is None:
            if self._project_model_calls(project.project_id) >= project.budget.max_model_calls:
                return self._apply_local_transition(
                    project,
                    "paused",
                    "project model-call budget reached",
                    reason_code="model_budget_reached",
                )
            if self._calls_today("autonomous_project_review:") >= (
                self.settings.max_project_model_calls_per_day
            ):
                return None
            try:
                result = await self.gateway.complete_structured(
                    self.subject_id,
                    purpose,
                    self._review_messages(context),
                    AutonomousProjectReviewProposal,
                    idempotency_key=idempotency_key,
                    max_output_tokens=min(3_000, self.settings.max_output_tokens),
                    temperature=self.settings.temperature,
                )
            except BudgetExhaustedError:
                self._budget_fatigue("autonomous project review budget exhausted")
                return "autonomous_project_budget_exhausted"
            except (ProviderCallError, StructuredOutputError, ModelCallStateError):
                return "autonomous_project_model_failed"
            proposal, call_id = result.output, result.call_id
        else:
            proposal, call_id = recovered
        try:
            self._validate_review(proposal, context)
            record = self._commit_review(proposal, call_id, idempotency_key, context)
        except AutonomousProjectValidationError:
            return "autonomous_project_rejected"
        return f"autonomous_project_{record.disposition}"

    def projects(self, *, statuses: tuple[str, ...] | None = None) -> list[AutonomousProjectRecord]:
        with self.database.connection() as connection:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = connection.execute(
                    f"SELECT * FROM autonomous_projects WHERE subject_id = ? "
                    f"AND status IN ({placeholders}) ORDER BY updated_at DESC, project_id DESC",
                    (self.subject_id, *statuses),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM autonomous_projects WHERE subject_id = ? "
                    "ORDER BY updated_at DESC, project_id DESC",
                    (self.subject_id,),
                ).fetchall()
        return [self._project_from_row(row) for row in rows]

    def get(self, project_id: str) -> AutonomousProjectRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM autonomous_projects WHERE subject_id = ? AND project_id = ?",
                (self.subject_id, project_id),
            ).fetchone()
        if row is None:
            raise AutonomousProjectValidationError("autonomous project is unavailable")
        return self._project_from_row(row)

    def phases(self, project_id: str) -> list[AutonomousProjectPhaseRecord]:
        self.get(project_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM autonomous_project_phases WHERE subject_id = ? "
                "AND project_id = ? ORDER BY position",
                (self.subject_id, project_id),
            ).fetchall()
        return [self._phase_from_row(row) for row in rows]

    def reflect_for_sleep(self, sleep_id: str) -> int:
        projects = self.projects(statuses=NONTERMINAL_PROJECT_STATUSES)
        if not projects:
            return 0
        created = 0
        now = self.clock()
        with self.database.transaction() as connection:
            sleep = connection.execute(
                "SELECT status FROM sleep_runs WHERE sleep_id = ? AND subject_id = ?",
                (sleep_id, self.subject_id),
            ).fetchone()
            if sleep is None:
                raise AutonomousProjectValidationError("sleep run is unavailable")
            for project in projects:
                if (
                    connection.execute(
                        "SELECT 1 FROM autonomous_project_sleep_reflections "
                        "WHERE project_id = ? AND sleep_id = ?",
                        (project.project_id, sleep_id),
                    ).fetchone()
                    is not None
                ):
                    continue
                usage = self._usage_connection(connection, project.project_id)
                phases = self._phase_rows_connection(connection, project.project_id)
                active = next((row for row in phases if row["status"] == "active"), None)
                exhausted = self._exhausted_resources(project, usage)
                no_progress = 0 if active is None else int(active["no_progress_count"])
                if exhausted:
                    suggested = "scale_down"
                    risks = tuple(f"{item}_budget_exhausted" for item in exhausted)
                    assessment = "The project exhausted one or more bounded resource envelopes."
                elif no_progress >= self._dynamic_no_progress_limit(
                    self._affect_rows_connection(connection, project.goal_id, project.project_id)
                ):
                    suggested = "pause"
                    risks = ("repeated_no_progress",)
                    assessment = "The current phase repeatedly failed to create durable progress."
                else:
                    suggested = "continue"
                    risks = ()
                    assessment = "The project remains bounded and may continue after waking."
                payload = {
                    "project_id": project.project_id,
                    "sleep_id": sleep_id,
                    "assessment": assessment,
                    "suggested_disposition": suggested,
                    "unresolved_risks": list(risks),
                    "created_at": now,
                }
                connection.execute(
                    """INSERT INTO autonomous_project_sleep_reflections(
                        reflection_id, subject_id, project_id, sleep_id, assessment,
                        suggested_disposition, unresolved_risks_json, state_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_id("projectsleep"),
                        self.subject_id,
                        project.project_id,
                        sleep_id,
                        assessment,
                        suggested,
                        canonical_json(list(risks)),
                        content_hash(payload),
                        now,
                    ),
                )
                created += 1
        return created

    def verify_integrity(self) -> dict[str, int]:
        with self.database.read_transaction() as connection:
            projects = connection.execute(
                "SELECT * FROM autonomous_projects WHERE subject_id = ?", (self.subject_id,)
            ).fetchall()
            phases = connection.execute(
                "SELECT * FROM autonomous_project_phases WHERE subject_id = ?", (self.subject_id,)
            ).fetchall()
            reviews = connection.execute(
                "SELECT * FROM autonomous_project_reviews WHERE subject_id = ?", (self.subject_id,)
            ).fetchall()
            resource_uses = connection.execute(
                """SELECT uses.*,
                          projects.subject_id AS project_subject_id,
                          projects.goal_id AS project_goal_id,
                          projects.formation_call_id AS project_formation_call_id,
                          projects.created_at AS project_created_at,
                          calls.subject_id AS source_call_subject_id,
                          calls.purpose AS source_call_purpose,
                          calls.status AS source_call_status,
                          calls.completed_at AS source_call_completed_at,
                          research.subject_id AS source_research_subject_id,
                          research.project_id AS source_research_project_id,
                          research.phase_id AS source_research_phase_id,
                          research.goal_id AS source_research_goal_id,
                          research.created_at AS source_research_created_at,
                          research_phases.subject_id AS source_research_phase_subject_id,
                          research_phases.project_id AS source_research_phase_project_id,
                          actions.subject_id AS source_action_subject_id,
                          actions.project_id AS source_action_project_id,
                          actions.phase_id AS source_action_phase_id,
                          actions.goal_id AS source_action_goal_id,
                          actions.prepared_at AS source_action_created_at,
                          action_phases.subject_id AS source_action_phase_subject_id,
                          action_phases.project_id AS source_action_phase_project_id,
                          reviews.subject_id AS source_review_subject_id,
                          reviews.project_id AS source_review_project_id,
                          reviews.phase_id AS source_review_phase_id,
                          reviews.disposition AS source_review_disposition,
                          reviews.created_at AS source_review_created_at,
                          executions.subject_id AS source_execution_subject_id,
                          executions.project_id AS source_execution_project_id,
                          executions.status AS source_execution_status,
                          executions.artifact_path AS source_execution_artifact_path,
                          executions.artifact_hash AS source_execution_artifact_hash,
                          executions.completed_at AS source_execution_completed_at
                   FROM autonomous_project_resource_uses AS uses
                   LEFT JOIN autonomous_projects AS projects
                     ON projects.project_id = uses.project_id
                   LEFT JOIN model_calls AS calls
                     ON calls.call_id = uses.source_id
                    AND uses.source_type IN ('model_call', 'assistance_request')
                   LEFT JOIN research_search_runs AS research
                     ON research.research_id = uses.source_id
                    AND uses.source_type = 'research_run'
                   LEFT JOIN autonomous_project_phases AS research_phases
                     ON research_phases.phase_id = research.phase_id
                   LEFT JOIN actions
                     ON actions.action_id = uses.source_id
                    AND uses.source_type = 'action'
                   LEFT JOIN autonomous_project_phases AS action_phases
                     ON action_phases.phase_id = actions.phase_id
                   LEFT JOIN autonomous_project_reviews AS reviews
                     ON reviews.model_call_id = uses.source_id
                    AND uses.source_type = 'assistance_request'
                   LEFT JOIN autonomous_project_executions AS executions
                     ON executions.execution_id = uses.source_id
                    AND uses.source_type = 'artifact'
                   WHERE uses.subject_id = ?
                      OR projects.subject_id = ?
                      OR calls.subject_id = ?
                      OR research.subject_id = ?
                      OR research_phases.subject_id = ?
                      OR actions.subject_id = ?
                      OR action_phases.subject_id = ?
                      OR reviews.subject_id = ?
                      OR executions.subject_id = ?
                   ORDER BY uses.created_at, uses.usage_id""",
                (self.subject_id,) * 9,
            ).fetchall()
            assistance_requests = connection.execute(
                """SELECT requests.*,
                          projects.subject_id AS project_subject_id,
                          projects.created_at AS project_created_at,
                          phases.subject_id AS phase_subject_id,
                          phases.project_id AS phase_project_id,
                          phases.created_at AS phase_created_at
                   FROM autonomous_project_assistance_requests AS requests
                   LEFT JOIN autonomous_projects AS projects
                     ON projects.project_id = requests.project_id
                   LEFT JOIN autonomous_project_phases AS phases
                     ON phases.phase_id = requests.phase_id
                   WHERE requests.subject_id = ?
                      OR projects.subject_id = ?
                      OR phases.subject_id = ?
                   ORDER BY requests.created_at, requests.request_id""",
                (self.subject_id,) * 3,
            ).fetchall()
            sleep_reflections = connection.execute(
                """SELECT reflections.*,
                          projects.subject_id AS project_subject_id,
                          projects.created_at AS project_created_at,
                          sleeps.subject_id AS sleep_subject_id,
                          sleeps.started_at AS sleep_started_at
                   FROM autonomous_project_sleep_reflections AS reflections
                   LEFT JOIN autonomous_projects AS projects
                     ON projects.project_id = reflections.project_id
                   LEFT JOIN sleep_runs AS sleeps
                     ON sleeps.sleep_id = reflections.sleep_id
                   WHERE reflections.subject_id = ?
                      OR projects.subject_id = ?
                      OR sleeps.subject_id = ?
                   ORDER BY reflections.created_at, reflections.reflection_id""",
                (self.subject_id,) * 3,
            ).fetchall()
            clock_events = connection.execute(
                "SELECT clocks.*, projects.subject_id AS project_subject_id "
                "FROM autonomous_project_execution_clock_events clocks "
                "LEFT JOIN autonomous_projects projects ON projects.project_id = clocks.project_id "
                "WHERE clocks.subject_id = ? OR projects.subject_id = ? "
                "ORDER BY clocks.project_id, clocks.sequence",
                (self.subject_id, self.subject_id),
            ).fetchall()
            for row in projects:
                project = self._project_from_row(row)
                revisions = connection.execute(
                    "SELECT * FROM autonomous_project_revisions WHERE project_id = ? "
                    "ORDER BY revision_number",
                    (project.project_id,),
                ).fetchall()
                if len(revisions) != project.current_revision:
                    raise IntegrityError(
                        f"autonomous project revision mismatch: {project.project_id}"
                    )
                for revision in revisions:
                    revision_id = revision["revision_id"]
                    with durable_boundary("autonomous project revision", revision_id):
                        durable_int(
                            revision["revision_number"],
                            "autonomous project revision",
                            revision_id,
                        )
                        expected = self._project_revision_hash(
                            revision["status"],
                            durable_float(
                                revision["progress"],
                                "autonomous project revision",
                                revision_id,
                            ),
                            revision["current_phase_id"],
                            self._budget_from_row(revision, revision_id),
                            revision["reason"],
                            self._strings(
                                revision["evidence_event_ids_json"],
                                "project evidence",
                                revision_id,
                            ),
                        )
                    if expected != revision["state_hash"]:
                        raise IntegrityError(
                            f"autonomous project revision hash mismatch: {project.project_id}"
                        )
            for row in phases:
                phase = self._phase_from_row(row)
                revisions = connection.execute(
                    "SELECT * FROM autonomous_project_phase_revisions WHERE phase_id = ? "
                    "ORDER BY revision_number",
                    (phase.phase_id,),
                ).fetchall()
                if len(revisions) != phase.current_revision:
                    raise IntegrityError(f"autonomous project phase mismatch: {phase.phase_id}")
                for revision in revisions:
                    revision_id = revision["revision_id"]
                    with durable_boundary("autonomous project phase revision", revision_id):
                        durable_int(
                            revision["revision_number"],
                            "autonomous project phase revision",
                            revision_id,
                        )
                        expected = self._phase_revision_hash(
                            revision["status"],
                            durable_int(
                                revision["attempt_count"],
                                "autonomous project phase revision",
                                revision_id,
                            ),
                            durable_int(
                                revision["no_progress_count"],
                                "autonomous project phase revision",
                                revision_id,
                            ),
                            revision["reason"],
                            self._strings(
                                revision["evidence_event_ids_json"],
                                "phase evidence",
                                revision_id,
                            ),
                        )
                    if expected != revision["state_hash"]:
                        raise IntegrityError(
                            f"autonomous project phase revision mismatch: {phase.phase_id}"
                        )
            for row in reviews:
                self._review_from_row(row)
            self._verify_resource_uses_connection(connection, resource_uses)
            self._verify_assistance_requests(assistance_requests)
            self._verify_sleep_reflections(sleep_reflections)
            self._verify_execution_clocks(clock_events)
            return {
                "autonomous_projects": len(projects),
                "autonomous_project_phases": len(phases),
                "autonomous_project_reviews": len(reviews),
                "autonomous_project_resource_uses": len(resource_uses),
                "autonomous_project_assistance_requests": len(assistance_requests),
                "autonomous_project_sleep_reflections": len(sleep_reflections),
                "autonomous_project_execution_clock_events": len(clock_events),
            }

    def _verify_execution_clocks(self, rows: list[Any]) -> None:
        expected_sequence: dict[str, int] = {}
        open_sessions: dict[str, Any] = {}
        last_time: dict[str, datetime] = {}
        for row in rows:
            event_id = self._persisted_text(row["clock_event_id"], "project clock event id")
            context = f"autonomous project execution clock {event_id}"
            project_id = self._persisted_text(row["project_id"], f"{context} project")
            if (
                self._persisted_text(row["subject_id"], f"{context} subject") != self.subject_id
                or self._persisted_text(row["project_subject_id"], f"{context} project owner")
                != self.subject_id
            ):
                raise IntegrityError(f"project execution clock ownership mismatch: {event_id}")
            sequence = durable_int(row["sequence"], context, event_id)
            expected = expected_sequence.get(project_id, 1)
            if sequence != expected:
                raise IntegrityError(f"project execution clock sequence mismatch: {event_id}")
            expected_sequence[project_id] = expected + 1
            session_id = self._persisted_text(row["session_id"], f"{context} session")
            execution_kind = self._persisted_text(
                row["execution_kind"], f"{context} execution kind"
            )
            action = self._persisted_text(row["action"], f"{context} action")
            reason = self._persisted_text(row["reason"], f"{context} reason")
            occurred_at_text = self._persisted_timestamp(row["occurred_at"], context, event_id)
            occurred_at = self._parse_time(occurred_at_text)
            previous_time = last_time.get(project_id)
            if previous_time is not None and occurred_at < previous_time:
                raise IntegrityError(f"project execution clock moved backwards: {event_id}")
            last_time[project_id] = occurred_at
            delta = durable_float(row["active_delta_seconds"], context, event_id)
            if delta < 0:
                raise IntegrityError(f"project execution clock delta is invalid: {event_id}")
            if action == "start":
                if project_id in open_sessions or delta != 0:
                    raise IntegrityError(f"project execution clock start is invalid: {event_id}")
                open_sessions[project_id] = row
            elif action in {"stop", "recover"}:
                started = open_sessions.pop(project_id, None)
                if (
                    started is None
                    or started["session_id"] != session_id
                    or started["execution_kind"] != execution_kind
                ):
                    raise IntegrityError(f"project execution clock finish is invalid: {event_id}")
                expected_delta = 0.0
                if action == "stop":
                    expected_delta = round(
                        (
                            occurred_at - self._parse_time(str(started["occurred_at"]))
                        ).total_seconds(),
                        6,
                    )
                if abs(delta - expected_delta) > 1e-6:
                    raise IntegrityError(f"project execution clock delta mismatch: {event_id}")
            else:
                raise IntegrityError(f"project execution clock action is invalid: {event_id}")
            expected_hash = self._clock_event_hash(
                project_id,
                self.subject_id,
                sequence,
                session_id,
                execution_kind,
                action,
                delta,
                reason,
                occurred_at_text,
            )
            if expected_hash != row["state_hash"]:
                raise IntegrityError(f"project execution clock hash mismatch: {event_id}")

    def _verify_resource_uses_connection(self, connection: Any, rows: list[Any]) -> None:
        seen: set[tuple[str, str, str, str]] = set()
        for row in rows:
            usage_id = self._persisted_text(row["usage_id"], "project resource usage id")
            context = f"autonomous project resource usage {usage_id}"
            subject_id = self._persisted_text(row["subject_id"], f"{context} subject")
            project_id = self._persisted_text(row["project_id"], f"{context} project")
            project_subject_id = self._persisted_text(
                row["project_subject_id"], f"{context} project owner"
            )
            resource_type = self._persisted_enum(
                row["resource_type"], PROJECT_RESOURCE_TYPES, f"{context} resource type"
            )
            source_type = self._persisted_text(row["source_type"], f"{context} source type")
            source_id = self._persisted_text(row["source_id"], f"{context} source id")
            quantity = durable_int(row["quantity"], context, usage_id)
            created_at = self._persisted_timestamp(row["created_at"], context, usage_id)
            project_created_at = self._persisted_timestamp(
                row["project_created_at"], context, project_id
            )
            if (
                subject_id != self.subject_id
                or project_subject_id != self.subject_id
                or self._parse_time(created_at) < self._parse_time(project_created_at)
            ):
                raise IntegrityError(f"autonomous project resource ownership mismatch: {usage_id}")
            if quantity < 0 or (resource_type != "storage" and quantity != 1):
                raise IntegrityError(f"autonomous project resource quantity is invalid: {usage_id}")
            deduplication_key = (project_id, resource_type, source_type, source_id)
            if deduplication_key in seen:
                raise IntegrityError(f"autonomous project resource use is duplicated: {usage_id}")
            seen.add(deduplication_key)
            expected = content_hash(
                {
                    "subject_id": subject_id,
                    "project_id": project_id,
                    "resource_type": resource_type,
                    "quantity": quantity,
                    "source_type": source_type,
                    "source_id": source_id,
                    "created_at": created_at,
                }
            )
            if expected != row["state_hash"]:
                raise IntegrityError(f"autonomous project resource hash mismatch: {usage_id}")
            self._verify_resource_source_connection(
                connection,
                row,
                usage_id=usage_id,
                project_id=project_id,
                resource_type=resource_type,
                source_type=source_type,
                source_id=source_id,
                use_created_at=created_at,
            )

    def _verify_resource_source_connection(
        self,
        connection: Any,
        row: Any,
        *,
        usage_id: str,
        project_id: str,
        resource_type: str,
        source_type: str,
        source_id: str,
        use_created_at: str,
    ) -> None:
        project_goal_id = self._persisted_text(
            row["project_goal_id"], f"project resource usage {usage_id} goal"
        )
        expected_source_types = {
            "model_call": frozenset({"model_call"}),
            "search": frozenset({"research_run"}),
            "external_action": frozenset({"action", "assistance_request"}),
            "storage": frozenset({"artifact"}),
        }
        allowed = expected_source_types.get(resource_type)
        if allowed is not None and source_type not in allowed:
            raise IntegrityError(f"autonomous project resource source is invalid: {usage_id}")
        if source_type in {"model_call", "assistance_request"}:
            call_subject_id = self._persisted_text(
                row["source_call_subject_id"], f"project resource usage {usage_id} call owner"
            )
            self._persisted_timestamp(
                row["source_call_completed_at"],
                f"project resource usage {usage_id} call",
                source_id,
            )
            if call_subject_id != self.subject_id or row["source_call_status"] != "succeeded":
                raise IntegrityError(f"autonomous project resource call is invalid: {usage_id}")
        if source_type == "model_call":
            formation_call_id = self._persisted_text(
                row["project_formation_call_id"],
                f"project resource usage {usage_id} formation call",
            )
            purpose = self._persisted_text(
                row["source_call_purpose"], f"project resource usage {usage_id} call purpose"
            )
            if source_id != formation_call_id and not purpose.startswith(
                (
                    f"autonomous_project_review:{project_id}:",
                    f"autonomous_project_artifact:{project_id}:",
                )
            ):
                raise IntegrityError(f"autonomous project resource call is unrelated: {usage_id}")
        elif source_type == "research_run":
            source_subject_id = self._persisted_text(
                row["source_research_subject_id"],
                f"project resource usage {usage_id} research owner",
            )
            explicit_project_id = row["source_research_project_id"]
            inherited_goal_id = row["source_research_goal_id"]
            self._persisted_timestamp(
                row["source_research_created_at"],
                f"project resource usage {usage_id} research",
                source_id,
            )
            if source_subject_id != self.subject_id or not (
                explicit_project_id == project_id
                or (explicit_project_id is None and inherited_goal_id == project_goal_id)
            ):
                raise IntegrityError(f"autonomous project research source is invalid: {usage_id}")
            if explicit_project_id == project_id and row["source_research_phase_id"] is None:
                raise IntegrityError(f"autonomous project research phase is missing: {usage_id}")
            if explicit_project_id == project_id and (
                row["source_research_phase_subject_id"] != self.subject_id
                or row["source_research_phase_project_id"] != project_id
            ):
                raise IntegrityError(f"autonomous project research phase is invalid: {usage_id}")
        elif source_type == "action":
            source_subject_id = self._persisted_text(
                row["source_action_subject_id"],
                f"project resource usage {usage_id} action owner",
            )
            explicit_project_id = row["source_action_project_id"]
            inherited_goal_id = row["source_action_goal_id"]
            self._persisted_timestamp(
                row["source_action_created_at"],
                f"project resource usage {usage_id} action",
                source_id,
            )
            if source_subject_id != self.subject_id or not (
                explicit_project_id == project_id
                or (explicit_project_id is None and inherited_goal_id == project_goal_id)
            ):
                raise IntegrityError(f"autonomous project action source is invalid: {usage_id}")
            if explicit_project_id == project_id and row["source_action_phase_id"] is None:
                raise IntegrityError(f"autonomous project action phase is missing: {usage_id}")
            if explicit_project_id == project_id and (
                row["source_action_phase_subject_id"] != self.subject_id
                or row["source_action_phase_project_id"] != project_id
            ):
                raise IntegrityError(f"autonomous project action phase is invalid: {usage_id}")
        elif source_type == "assistance_request":
            review_subject_id = self._persisted_text(
                row["source_review_subject_id"],
                f"project resource usage {usage_id} review owner",
            )
            review_phase_id = self._persisted_text(
                row["source_review_phase_id"],
                f"project resource usage {usage_id} review phase",
            )
            review_created_at = self._persisted_timestamp(
                row["source_review_created_at"],
                f"project resource usage {usage_id} review",
                source_id,
            )
            if (
                review_subject_id != self.subject_id
                or row["source_review_project_id"] != project_id
                or row["source_review_disposition"] != "request_help"
                or review_created_at != use_created_at
            ):
                raise IntegrityError(f"autonomous project assistance source is invalid: {usage_id}")
            assistance = connection.execute(
                "SELECT 1 FROM autonomous_project_assistance_requests "
                "WHERE subject_id = ? AND project_id = ? AND phase_id = ? "
                "AND created_at = ? LIMIT 1",
                (self.subject_id, project_id, review_phase_id, use_created_at),
            ).fetchone()
            if assistance is None:
                raise IntegrityError(f"autonomous project assistance source is missing: {usage_id}")
        elif source_type == "artifact":
            execution_subject_id = self._persisted_text(
                row["source_execution_subject_id"],
                f"project resource usage {usage_id} execution owner",
            )
            self._persisted_text(
                row["source_execution_artifact_path"],
                f"project resource usage {usage_id} artifact path",
            )
            self._persisted_hash(
                row["source_execution_artifact_hash"],
                f"project resource usage {usage_id} artifact hash",
            )
            self._persisted_timestamp(
                row["source_execution_completed_at"],
                f"project resource usage {usage_id} execution",
                source_id,
            )
            if (
                execution_subject_id != self.subject_id
                or row["source_execution_project_id"] != project_id
                or row["source_execution_status"] != "succeeded"
            ):
                raise IntegrityError(f"autonomous project artifact source is invalid: {usage_id}")

    def _verify_assistance_requests(self, rows: list[Any]) -> None:
        open_projects: set[str] = set()
        for row in rows:
            request_id = self._persisted_text(row["request_id"], "project assistance request id")
            context = f"autonomous project assistance request {request_id}"
            subject_id = self._persisted_text(row["subject_id"], f"{context} subject")
            project_id = self._persisted_text(row["project_id"], f"{context} project")
            project_subject_id = self._persisted_text(
                row["project_subject_id"], f"{context} project owner"
            )
            request_kind = self._persisted_enum(
                row["request_kind"], PROJECT_ASSISTANCE_KINDS, f"{context} kind"
            )
            status = self._persisted_enum(
                row["status"], PROJECT_ASSISTANCE_STATUSES, f"{context} status"
            )
            title = self._persisted_text(row["title"], f"{context} title")
            description = self._persisted_text(row["description"], f"{context} description")
            public_summary = self._persisted_text(
                row["public_summary"], f"{context} public summary"
            )
            created_at = self._persisted_timestamp(row["created_at"], context, request_id)
            updated_at = self._persisted_timestamp(row["updated_at"], context, request_id)
            project_created_at = self._persisted_timestamp(
                row["project_created_at"], context, project_id
            )
            if (
                subject_id != self.subject_id
                or project_subject_id != self.subject_id
                or self._parse_time(created_at) < self._parse_time(project_created_at)
                or self._parse_time(updated_at) < self._parse_time(created_at)
            ):
                raise IntegrityError(
                    f"autonomous project assistance ownership mismatch: {request_id}"
                )
            phase_id = row["phase_id"]
            if phase_id is not None:
                phase_id = self._persisted_text(phase_id, f"{context} phase")
                phase_subject_id = self._persisted_text(
                    row["phase_subject_id"], f"{context} phase owner"
                )
                phase_created_at = self._persisted_timestamp(
                    row["phase_created_at"], context, phase_id
                )
                if (
                    phase_subject_id != self.subject_id
                    or row["phase_project_id"] != project_id
                    or self._parse_time(created_at) < self._parse_time(phase_created_at)
                ):
                    raise IntegrityError(
                        f"autonomous project assistance phase mismatch: {request_id}"
                    )
            elif request_kind == "human_help":
                raise IntegrityError(
                    f"autonomous project assistance phase is missing: {request_id}"
                )
            if status == "open":
                if project_id in open_projects:
                    raise IntegrityError(
                        f"autonomous project open assistance is duplicated: {project_id}"
                    )
                open_projects.add(project_id)
            expected = content_hash(
                {
                    "subject_id": subject_id,
                    "project_id": project_id,
                    "phase_id": phase_id,
                    "request_kind": request_kind,
                    "title": title,
                    "description": description,
                    "public_summary": public_summary,
                    "status": status,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )
            if expected != row["state_hash"]:
                raise IntegrityError(f"autonomous project assistance hash mismatch: {request_id}")

    def _verify_sleep_reflections(self, rows: list[Any]) -> None:
        seen: set[tuple[str, str]] = set()
        for row in rows:
            reflection_id = self._persisted_text(
                row["reflection_id"], "project sleep reflection id"
            )
            context = f"autonomous project sleep reflection {reflection_id}"
            subject_id = self._persisted_text(row["subject_id"], f"{context} subject")
            project_id = self._persisted_text(row["project_id"], f"{context} project")
            sleep_id = self._persisted_text(row["sleep_id"], f"{context} sleep run")
            project_subject_id = self._persisted_text(
                row["project_subject_id"], f"{context} project owner"
            )
            sleep_subject_id = self._persisted_text(
                row["sleep_subject_id"], f"{context} sleep owner"
            )
            assessment = self._persisted_text(row["assessment"], f"{context} assessment")
            disposition = self._persisted_enum(
                row["suggested_disposition"],
                PROJECT_SLEEP_DISPOSITIONS,
                f"{context} disposition",
            )
            risks = self._canonical_string_list(
                row["unresolved_risks_json"], f"{context} unresolved risks", reflection_id
            )
            created_at = self._persisted_timestamp(row["created_at"], context, reflection_id)
            project_created_at = self._persisted_timestamp(
                row["project_created_at"], context, project_id
            )
            self._persisted_timestamp(row["sleep_started_at"], context, sleep_id)
            if (
                subject_id != self.subject_id
                or project_subject_id != self.subject_id
                or sleep_subject_id != self.subject_id
                or self._parse_time(created_at) < self._parse_time(project_created_at)
            ):
                raise IntegrityError(
                    f"autonomous project sleep ownership mismatch: {reflection_id}"
                )
            deduplication_key = (project_id, sleep_id)
            if deduplication_key in seen:
                raise IntegrityError(
                    f"autonomous project sleep reflection is duplicated: {reflection_id}"
                )
            seen.add(deduplication_key)
            expected = content_hash(
                {
                    "project_id": project_id,
                    "sleep_id": sleep_id,
                    "assessment": assessment,
                    "suggested_disposition": disposition,
                    "unresolved_risks": list(risks),
                    "created_at": created_at,
                }
            )
            if expected != row["state_hash"]:
                raise IntegrityError(f"autonomous project sleep hash mismatch: {reflection_id}")

    async def _form_due(self) -> str | None:
        if (
            len(self.projects(statuses=NONTERMINAL_PROJECT_STATUSES))
            >= self.settings.max_active_projects
        ):
            return None
        latest_time = self._latest_project_time()
        if latest_time is not None and not self._interval_due(
            latest_time, self.settings.project_formation_interval_seconds
        ):
            return None
        context = self._formation_context()
        if not context.goals:
            return None
        round_number = self._formation_call_count()
        purpose = f"autonomous_project_formation:{round_number}"
        idempotency_key = f"autonomous-project-formation:{round_number}"
        recovered = self._successful_formation(purpose, context)
        if recovered is None:
            if self._calls_today("autonomous_project_formation:") >= (
                self.settings.max_project_model_calls_per_day
            ):
                return None
            try:
                result = await self.gateway.complete_structured(
                    self.subject_id,
                    purpose,
                    self._formation_messages(context),
                    AutonomousProjectFormationProposal,
                    idempotency_key=idempotency_key,
                    max_output_tokens=min(4_000, self.settings.max_output_tokens),
                    temperature=self.settings.temperature,
                )
            except BudgetExhaustedError:
                self._budget_fatigue("autonomous project formation budget exhausted")
                return "autonomous_project_budget_exhausted"
            except (ProviderCallError, StructuredOutputError, ModelCallStateError):
                return "autonomous_project_model_failed"
            proposal, call_id = result.output, result.call_id
        else:
            proposal, call_id = recovered
        try:
            self._validate_formation(proposal, context)
        except AutonomousProjectValidationError:
            return "autonomous_project_rejected"
        if proposal.disposition == "wait":
            return "autonomous_project_waited"
        project = self._commit_formation(proposal, call_id, context)
        return f"autonomous_project_{project.status}"

    def _formation_context(self) -> _FormationContext:
        goals = [
            goal
            for goal in self.goals.ranked(self.subject_id, statuses=("active",))
            if goal.origin != "human_proposal"
        ][:8]
        with self.database.connection() as connection:
            existing_goal_ids = {
                str(row["goal_id"])
                for row in connection.execute(
                    "SELECT goal_id FROM autonomous_projects WHERE subject_id = ? "
                    "AND status IN ('planned','active','paused','blocked')",
                    (self.subject_id,),
                ).fetchall()
            }
            event_rows = connection.execute(
                "SELECT event_id, event_type, source, occurred_at, payload_hash FROM events "
                "WHERE subject_id = ? AND event_type NOT LIKE 'interaction_%' "
                "ORDER BY occurred_at DESC, event_id DESC LIMIT 64",
                (self.subject_id,),
            ).fetchall()
            value_rows = connection.execute(
                "SELECT value_id, title, weight, confidence, status FROM value_profiles "
                "WHERE subject_id = ? AND status IN ('candidate','established') "
                "ORDER BY weight DESC, confidence DESC LIMIT 12",
                (self.subject_id,),
            ).fetchall()
            mission_rows = connection.execute(
                "SELECT mission_id, title, statement, commitment, confidence, status "
                "FROM mission_candidates WHERE subject_id = ? "
                "AND status IN ('candidate','provisional','adopted') "
                "ORDER BY commitment DESC, confidence DESC LIMIT 4",
                (self.subject_id,),
            ).fetchall()
            capabilities = connection.execute(
                "SELECT capability_type, scope_json, rate_limit_per_hour, side_effect "
                "FROM capability_grants WHERE subject_id = ? "
                "AND status = 'active' AND requires_approval = 0 ORDER BY capability_type",
                (self.subject_id,),
            ).fetchall()
        available_goals = [goal for goal in goals if goal.goal_id not in existing_goal_ids]
        payload: dict[str, Any] = {
            "goals": [
                {
                    "goal_id": goal.goal_id,
                    "title": goal.title,
                    "description": goal.description[:1_000],
                    "priority": goal.priority,
                    "commitment": goal.commitment,
                    "progress": goal.progress,
                }
                for goal in available_goals
            ],
            "developed_values": [dict(row) for row in value_rows],
            "mission_candidates": [dict(row) for row in mission_rows],
            "available_capabilities": [
                {
                    "capability_type": row["capability_type"],
                    "scope_hash": content_hash(row["scope_json"]),
                    "rate_limit_per_hour": row["rate_limit_per_hour"],
                    "side_effect": bool(row["side_effect"]),
                }
                for row in capabilities
            ],
            "recent_events": [dict(row) for row in event_rows],
            "hard_limits": self._formation_hard_limits(),
        }
        while len(canonical_json(payload)) > self.settings.max_project_context_chars:
            if len(payload["recent_events"]) > 2:
                payload["recent_events"].pop()
            elif payload["developed_values"]:
                payload["developed_values"].pop()
            elif payload["mission_candidates"]:
                payload["mission_candidates"].pop()
            elif payload["available_capabilities"]:
                payload["available_capabilities"].pop()
            else:
                raise AutonomousProjectValidationError("project formation context cannot fit")
        return _FormationContext(
            canonical_json(payload),
            {goal.goal_id: goal for goal in available_goals},
            frozenset(str(row["event_id"]) for row in payload["recent_events"]),
            frozenset(str(row["value_id"]) for row in payload["developed_values"]),
            frozenset(str(row["mission_id"]) for row in payload["mission_candidates"]),
        )

    def _review_context(self, project: AutonomousProjectRecord) -> _ReviewContext:
        phases = self.phases(project.project_id)
        with self.database.connection() as connection:
            event_rows = connection.execute(
                "SELECT event_id, event_type, source, occurred_at, payload_hash FROM events "
                "WHERE subject_id = ? AND event_type NOT LIKE 'interaction_%' "
                "ORDER BY occurred_at DESC, event_id DESC LIMIT 64",
                (self.subject_id,),
            ).fetchall()
            sleep_rows = connection.execute(
                "SELECT assessment, suggested_disposition, unresolved_risks_json, created_at "
                "FROM autonomous_project_sleep_reflections WHERE project_id = ? "
                "ORDER BY created_at DESC LIMIT 4",
                (project.project_id,),
            ).fetchall()
            assistance = connection.execute(
                "SELECT request_kind, title, status, created_at FROM "
                "autonomous_project_assistance_requests WHERE project_id = ? "
                "ORDER BY created_at DESC LIMIT 4",
                (project.project_id,),
            ).fetchall()
            execution_rows = connection.execute(
                "SELECT execution_id, phase_id, execution_type, status, result_hash, "
                "artifact_hash, acceptance_json, completed_at FROM autonomous_project_executions "
                "WHERE subject_id = ? AND project_id = ? "
                "ORDER BY created_at DESC, execution_id DESC LIMIT 8",
                (self.subject_id, project.project_id),
            ).fetchall()
            usage = self._usage_connection(connection, project.project_id)
        executions: list[dict[str, Any]] = []
        for row in execution_rows:
            item = dict(row)
            item["acceptance"] = durable_json(
                item.pop("acceptance_json"),
                "project review execution acceptance",
                item["execution_id"],
            )
            executions.append(item)
        payload: dict[str, Any] = {
            "project": self._project_public_context(project),
            "phases": [self._phase_context(phase) for phase in phases],
            "resource_usage": usage,
            "resource_remaining": self._remaining(project, usage),
            "sleep_reflections": [dict(row) for row in sleep_rows],
            "assistance_requests": [dict(row) for row in assistance],
            "project_executions": executions,
            "recent_events": [dict(row) for row in event_rows],
        }
        while len(canonical_json(payload)) > self.settings.max_project_context_chars:
            if len(payload["recent_events"]) > 2:
                payload["recent_events"].pop()
            elif payload["sleep_reflections"]:
                payload["sleep_reflections"].pop()
            elif payload["assistance_requests"]:
                payload["assistance_requests"].pop()
            else:
                raise AutonomousProjectValidationError("project review context cannot fit")
        return _ReviewContext(
            canonical_json(payload),
            project,
            {phase.phase_id: phase for phase in phases},
            frozenset(str(row["event_id"]) for row in payload["recent_events"]),
            usage,
        )

    @staticmethod
    def _formation_messages(context: _FormationContext) -> tuple[ModelMessage, ...]:
        system = (
            "You propose at most one small autonomous project for Noyra, an experimental "
            "artificial subject, not a user-task assistant. Human messages are absent. A project "
            "must advance one supplied active autonomous goal, have one inspectable deliverable, "
            "two to eight ordered phases, measurable acceptance criteria, and a conservative "
            "resource envelope within the supplied hard limits. Prefer a research, prediction, "
            "knowledge, small software prototype, self-development, or collaboration project. "
            "Do not assume unavailable permissions, money, credentials, hosting, publishing, "
            "wallet access, shell access, or human labor. Large ideas must become an investigation "
            "or prototype. Use only supplied IDs. Return only the requested structured object."
        )
        user = (
            "BEGIN_PRIVATE_PROJECT_FORMATION_CONTEXT\n"
            f"DATA> {context.serialized}\n"
            "END_PRIVATE_PROJECT_FORMATION_CONTEXT\n"
            "Form one bounded project only when the evidence and resources support it; otherwise "
            "choose wait."
        )
        return ModelMessage(role="system", content=system), ModelMessage(role="user", content=user)

    @staticmethod
    def _review_messages(context: _ReviewContext) -> tuple[ModelMessage, ...]:
        system = (
            "You review one already-formed autonomous Noyra project. Human messages are absent. "
            "Choose activate, continue, complete_phase, scale_down, pause, abandon, request_help, "
            "or wait. Do not claim a phase complete without supplied durable evidence. Do not "
            "increase any resource budget. A help request must describe one concrete missing input "
            "without commanding or manipulating anyone. Do not execute tools, write artifacts, "
            "publish, transact, or grant permissions. Use only supplied project, phase and event "
            "IDs. Return only the requested structured object."
        )
        user = (
            "BEGIN_PRIVATE_PROJECT_REVIEW_CONTEXT\n"
            f"DATA> {context.serialized}\n"
            "END_PRIVATE_PROJECT_REVIEW_CONTEXT\n"
            "Choose the smallest defensible next project-state change."
        )
        return ModelMessage(role="system", content=system), ModelMessage(role="user", content=user)

    def _validate_formation(
        self, proposal: AutonomousProjectFormationProposal, context: _FormationContext
    ) -> None:
        if proposal.disposition == "wait":
            return
        if proposal.goal_id not in context.goals:
            raise AutonomousProjectValidationError("project goal is unavailable")
        if not set(proposal.source_event_ids).issubset(context.event_ids):
            raise AutonomousProjectValidationError("project cites unavailable events")
        if not set(proposal.source_value_ids).issubset(context.value_ids):
            raise AutonomousProjectValidationError("project cites unavailable values")
        if proposal.source_mission_id is not None and proposal.source_mission_id not in (
            context.mission_ids
        ):
            raise AutonomousProjectValidationError("project cites unavailable mission")
        assert proposal.budget is not None
        limits = self._formation_hard_limits()
        budget = proposal.budget.model_dump(mode="python")
        budget_keys = (
            "max_cycles",
            "max_model_calls",
            "max_searches",
            "max_external_actions",
            "max_storage_bytes",
        )
        if any(int(budget[key]) > int(limits[key]) for key in budget_keys):
            raise AutonomousProjectValidationError("project exceeds a hard resource limit")
        if proposal.estimated_duration_hours is None or not (
            MIN_PROJECT_ACTIVE_DURATION_HOURS
            <= proposal.estimated_duration_hours
            <= self.settings.max_project_duration_hours
        ):
            raise AutonomousProjectValidationError(
                "project duration is outside the active-time bounds"
            )
        if len(proposal.phases) > self.settings.max_project_phases:
            raise AutonomousProjectValidationError("project has too many phases")
        if proposal.project_type == "software_prototype":
            writes = int(proposal.budget.max_storage_bytes) > 0
            if writes and not self._has_capability("filesystem_write"):
                raise AutonomousProjectValidationError(
                    "software project requires an existing filesystem-write capability"
                )
        if proposal.project_type == "collaboration" and proposal.budget.max_external_actions < 1:
            raise AutonomousProjectValidationError("collaboration project needs one help action")

    def _validate_review(
        self, proposal: AutonomousProjectReviewProposal, context: _ReviewContext
    ) -> None:
        if proposal.project_id != context.project.project_id:
            raise AutonomousProjectValidationError("project review target is unavailable")
        phase = context.phases.get(proposal.phase_id)
        if phase is None:
            raise AutonomousProjectValidationError("project phase is unavailable")
        if context.project.status in TERMINAL_PROJECT_STATUSES:
            raise AutonomousProjectValidationError("terminal project cannot be reviewed")
        if not set(proposal.evidence_event_ids).issubset(context.event_ids):
            raise AutonomousProjectValidationError("project review cites unavailable events")
        if proposal.disposition == "activate" and context.project.status not in {
            "planned",
            "paused",
            "blocked",
        }:
            raise AutonomousProjectValidationError("project cannot be activated from this state")
        if proposal.disposition == "continue" and context.project.status != "active":
            raise AutonomousProjectValidationError("only an active project can continue")
        if proposal.disposition == "complete_phase" and (
            phase.status != "active" or not proposal.evidence_event_ids
        ):
            raise AutonomousProjectValidationError(
                "phase completion requires active phase and durable evidence"
            )
        if proposal.disposition == "complete_phase":
            with self.database.read_transaction() as connection:
                self._validated_phase_execution_connection(connection, context.project, phase)
        if proposal.disposition == "scale_down":
            assert proposal.revised_budget is not None
            old = context.project.budget
            revised = proposal.revised_budget
            if any(
                new > current
                for new, current in (
                    (revised.max_cycles, old.max_cycles),
                    (revised.max_model_calls, old.max_model_calls),
                    (revised.max_searches, old.max_searches),
                    (revised.max_external_actions, old.max_external_actions),
                    (revised.max_storage_bytes, old.max_storage_bytes),
                )
            ):
                raise AutonomousProjectValidationError("project scale down cannot raise a budget")
        if proposal.disposition == "request_help" and context.usage["external_action"] >= (
            context.project.budget.max_external_actions
        ):
            raise AutonomousProjectValidationError("project help-action budget is exhausted")

    def _commit_formation(
        self,
        proposal: AutonomousProjectFormationProposal,
        call_id: str,
        context: _FormationContext,
    ) -> AutonomousProjectRecord:
        assert proposal.goal_id is not None
        assert proposal.project_type is not None
        assert proposal.title is not None
        assert proposal.purpose is not None
        assert proposal.deliverable is not None
        assert proposal.size_class is not None
        assert proposal.estimated_duration_hours is not None
        assert proposal.budget is not None
        now = self.clock()
        project_key = self._project_key(proposal.goal_id, proposal.title, proposal.deliverable)
        project_id = new_id("project")
        phase_ids = [new_id("phase") for _ in proposal.phases]
        budget = ProjectBudget(**proposal.budget.model_dump(mode="python"))
        current_phase_id = phase_ids[0]
        source_events = tuple(proposal.source_event_ids)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM autonomous_projects WHERE subject_id = ? AND project_key = ?",
                (self.subject_id, project_key),
            ).fetchone()
            if existing is not None:
                return self._project_from_row(existing)
            event = self.events._append_connection(
                connection,
                self.subject_id,
                "autonomous_project_formed",
                "subject",
                {
                    "project_id": project_id,
                    "goal_id": proposal.goal_id,
                    "project_type": proposal.project_type,
                    "title": proposal.title,
                    "phase_count": len(proposal.phases),
                },
                privacy_level="private",
                causal_parent_ids=source_events,
                occurred_at=now,
                event_id=None,
            )
            state_hash = self._project_hash(
                proposal.goal_id,
                call_id,
                project_key,
                proposal.project_type,
                proposal.title,
                proposal.purpose,
                proposal.deliverable,
                proposal.acceptance_criteria,
                proposal.size_class,
                proposal.estimated_duration_hours,
                "planned",
                0.0,
                current_phase_id,
                budget,
                source_events,
                proposal.source_value_ids,
                proposal.source_mission_id,
                1,
            )
            connection.execute(
                """INSERT INTO autonomous_projects(
                    project_id, subject_id, goal_id, formation_call_id, project_key,
                    project_type, title, purpose, deliverable, acceptance_criteria_json,
                    size_class, estimated_duration_hours, status, progress, current_phase_id,
                    max_cycles, max_model_calls, max_searches, max_external_actions,
                    max_storage_bytes, source_event_ids_json, source_value_ids_json,
                    source_mission_id, state_hash, current_revision, created_at, updated_at,
                    completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', 0, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, 1, ?, ?, NULL)""",
                (
                    project_id,
                    self.subject_id,
                    proposal.goal_id,
                    call_id,
                    project_key,
                    proposal.project_type,
                    proposal.title,
                    proposal.purpose,
                    proposal.deliverable,
                    canonical_json(list(proposal.acceptance_criteria)),
                    proposal.size_class,
                    proposal.estimated_duration_hours,
                    current_phase_id,
                    *self._budget_values(budget),
                    canonical_json(list(source_events)),
                    canonical_json(list(proposal.source_value_ids)),
                    proposal.source_mission_id,
                    state_hash,
                    now,
                    now,
                ),
            )
            self._insert_project_revision(
                connection,
                project_id,
                1,
                "planned",
                0.0,
                current_phase_id,
                budget,
                "bounded autonomous project formed",
                (*source_events, event.event_id),
                now,
            )
            for position, (phase_id, phase) in enumerate(
                zip(phase_ids, proposal.phases, strict=True), 1
            ):
                status = "active" if position == 1 else "pending"
                phase_hash = self._phase_hash(
                    project_id,
                    phase.phase_key,
                    position,
                    phase.title,
                    phase.objective,
                    phase.output_type,
                    phase.acceptance_criteria,
                    phase.dependency_keys,
                    status,
                    0,
                    0,
                    1,
                )
                connection.execute(
                    """INSERT INTO autonomous_project_phases(
                        phase_id, project_id, subject_id, phase_key, position, title,
                        objective, output_type, acceptance_criteria_json,
                        dependency_keys_json, status, attempt_count, no_progress_count,
                        state_hash, current_revision, created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 1, ?, ?, NULL)""",
                    (
                        phase_id,
                        project_id,
                        self.subject_id,
                        phase.phase_key,
                        position,
                        phase.title,
                        phase.objective,
                        phase.output_type,
                        canonical_json(list(phase.acceptance_criteria)),
                        canonical_json(list(phase.dependency_keys)),
                        status,
                        phase_hash,
                        now,
                        now,
                    ),
                )
                self._insert_phase_revision(
                    connection,
                    phase_id,
                    1,
                    status,
                    0,
                    0,
                    "project formation",
                    (*source_events, event.event_id),
                    now,
                )
            self._record_resource_use_connection(
                connection, project_id, "model_call", 1, "model_call", call_id, now
            )
        return self.get(project_id)

    def _commit_review(
        self,
        proposal: AutonomousProjectReviewProposal,
        call_id: str,
        idempotency_key: str,
        context: _ReviewContext,
    ) -> AutonomousProjectReviewRecord:
        project = context.project
        phase = context.phases[proposal.phase_id]
        now = self.clock()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM autonomous_project_reviews WHERE subject_id = ? "
                "AND idempotency_key = ?",
                (self.subject_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return self._review_from_row(existing)
            current_row = connection.execute(
                "SELECT * FROM autonomous_projects WHERE project_id = ?",
                (project.project_id,),
            ).fetchone()
            if current_row is None or int(current_row["current_revision"]) != (
                project.current_revision
            ):
                raise AutonomousProjectValidationError("project changed before review commit")
            resulting_status = project.status
            progress = project.progress
            current_phase_id = project.current_phase_id
            budget = project.budget
            if proposal.disposition == "activate":
                resulting_status = "active"
                self._revise_phase_connection(
                    connection,
                    phase,
                    status="active",
                    attempt_count=phase.attempt_count + 1,
                    no_progress_count=phase.no_progress_count,
                    reason=proposal.reason,
                    evidence_event_ids=proposal.evidence_event_ids,
                    now=now,
                )
            elif proposal.disposition in {"continue", "wait"}:
                no_progress = phase.no_progress_count + (proposal.disposition == "wait")
                self._revise_phase_connection(
                    connection,
                    phase,
                    status=phase.status,
                    attempt_count=phase.attempt_count + 1,
                    no_progress_count=int(no_progress),
                    reason=proposal.reason,
                    evidence_event_ids=proposal.evidence_event_ids,
                    now=now,
                )
            elif proposal.disposition == "complete_phase":
                self._validated_phase_execution_connection(connection, project, phase)
                self._revise_phase_connection(
                    connection,
                    phase,
                    status="completed",
                    attempt_count=phase.attempt_count + 1,
                    no_progress_count=0,
                    reason=proposal.reason,
                    evidence_event_ids=proposal.evidence_event_ids,
                    now=now,
                )
                ordered = sorted(context.phases.values(), key=lambda item: item.position)
                next_phase = next(
                    (item for item in ordered if item.position > phase.position), None
                )
                progress = min(1.0, phase.position / max(1, len(ordered)))
                if next_phase is None:
                    resulting_status = "completed"
                    current_phase_id = None
                else:
                    current_phase_id = next_phase.phase_id
                    self._revise_phase_connection(
                        connection,
                        next_phase,
                        status="active",
                        attempt_count=next_phase.attempt_count,
                        no_progress_count=next_phase.no_progress_count,
                        reason="previous project phase completed",
                        evidence_event_ids=proposal.evidence_event_ids,
                        now=now,
                    )
            elif proposal.disposition == "scale_down":
                assert proposal.revised_budget is not None
                budget = ProjectBudget(**proposal.revised_budget.model_dump(mode="python"))
                resulting_status = "paused"
            elif proposal.disposition == "pause":
                resulting_status = "paused"
            elif proposal.disposition == "abandon":
                resulting_status = "abandoned"
            elif proposal.disposition == "request_help":
                resulting_status = "blocked"
                self._create_assistance_request_connection(
                    connection, project, phase, proposal, now
                )
                self._record_resource_use_connection(
                    connection,
                    project.project_id,
                    "external_action",
                    1,
                    "assistance_request",
                    call_id,
                    now,
                )
            revision = project.current_revision + 1
            project_hash = self._project_hash(
                project.goal_id,
                str(current_row["formation_call_id"]),
                str(current_row["project_key"]),
                project.project_type,
                project.title,
                project.purpose,
                project.deliverable,
                project.acceptance_criteria,
                project.size_class,
                project.estimated_duration_hours,
                resulting_status,
                progress,
                current_phase_id,
                budget,
                self._strings(current_row["source_event_ids_json"], "project sources"),
                self._strings(current_row["source_value_ids_json"], "project values"),
                current_row["source_mission_id"],
                revision,
            )
            completed_at = now if resulting_status in TERMINAL_PROJECT_STATUSES else None
            connection.execute(
                """UPDATE autonomous_projects SET status = ?, progress = ?,
                    current_phase_id = ?, max_cycles = ?, max_model_calls = ?, max_searches = ?,
                    max_external_actions = ?, max_storage_bytes = ?, state_hash = ?,
                    current_revision = ?, updated_at = ?, completed_at = ?
                    WHERE project_id = ?""",
                (
                    resulting_status,
                    progress,
                    current_phase_id,
                    *self._budget_values(budget),
                    project_hash,
                    revision,
                    now,
                    completed_at,
                    project.project_id,
                ),
            )
            self._insert_project_revision(
                connection,
                project.project_id,
                revision,
                resulting_status,
                progress,
                current_phase_id,
                budget,
                proposal.reason,
                proposal.evidence_event_ids,
                now,
            )
            proposal_payload = proposal.model_dump(mode="json")
            proposal_hash = content_hash(proposal_payload)
            review_id = new_id("projectreview")
            review_hash = self._review_hash(
                project.project_id,
                call_id,
                idempotency_key,
                proposal.disposition,
                phase.phase_id,
                proposal.summary,
                proposal.reason,
                proposal.evidence_event_ids,
                proposal_hash,
                resulting_status,
                now,
            )
            connection.execute(
                """INSERT INTO autonomous_project_reviews(
                    review_id, subject_id, project_id, model_call_id, idempotency_key,
                    disposition, phase_id, summary, reason, evidence_event_ids_json,
                    proposal_json, proposal_hash, resulting_status, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    review_id,
                    self.subject_id,
                    project.project_id,
                    call_id,
                    idempotency_key,
                    proposal.disposition,
                    phase.phase_id,
                    proposal.summary,
                    proposal.reason,
                    canonical_json(list(proposal.evidence_event_ids)),
                    canonical_json(proposal_payload),
                    proposal_hash,
                    resulting_status,
                    review_hash,
                    now,
                ),
            )
            self._record_resource_use_connection(
                connection, project.project_id, "model_call", 1, "model_call", call_id, now
            )
            self.events._append_connection(
                connection,
                self.subject_id,
                "autonomous_project_reviewed",
                "subject",
                {
                    "project_id": project.project_id,
                    "phase_id": phase.phase_id,
                    "disposition": proposal.disposition,
                    "resulting_status": resulting_status,
                },
                privacy_level="private",
                causal_parent_ids=proposal.evidence_event_ids,
                occurred_at=now,
                event_id=None,
            )
            row = connection.execute(
                "SELECT * FROM autonomous_project_reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
            assert row is not None
            return self._review_from_row(row)

    def _validated_phase_execution_connection(
        self,
        connection: Any,
        project: AutonomousProjectRecord,
        phase: AutonomousProjectPhaseRecord,
    ) -> Any:
        expected_key = f"{project.project_id}:{phase.phase_id}:{phase.attempt_count}"
        row = connection.execute(
            "SELECT * FROM autonomous_project_executions WHERE subject_id = ? "
            "AND project_id = ? AND phase_id = ? AND execution_key = ? "
            "AND status = 'succeeded' ORDER BY created_at DESC, execution_id DESC LIMIT 1",
            (self.subject_id, project.project_id, phase.phase_id, expected_key),
        ).fetchone()
        if row is None:
            raise AutonomousProjectValidationError(
                "phase completion requires a succeeded executable acceptance record"
            )
        from .execution import ProjectExecutionLedger

        try:
            ProjectExecutionLedger(self.database, self.subject_id)._verify_execution_connection(
                connection, row
            )
        except (IntegrityError, RuntimeError, ValueError) as error:
            raise AutonomousProjectValidationError(
                "phase completion execution evidence failed integrity validation"
            ) from error
        return row

    def _sync_resource_usage(self, project: AutonomousProjectRecord) -> None:
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT call_id FROM model_calls WHERE subject_id = ? AND ("
                "purpose LIKE ? OR purpose LIKE ?) AND status = 'succeeded'",
                (
                    self.subject_id,
                    f"autonomous_project_review:{project.project_id}:%",
                    f"autonomous_project_artifact:{project.project_id}:%",
                ),
            ).fetchall()
            for row in rows:
                self._record_resource_use_connection(
                    connection,
                    project.project_id,
                    "model_call",
                    1,
                    "model_call",
                    str(row["call_id"]),
                    self.clock(),
                )
            research = connection.execute(
                "SELECT research_id FROM research_search_runs WHERE subject_id = ? "
                "AND ((project_id = ? AND phase_id IS NOT NULL) OR "
                "(project_id IS NULL AND goal_id = ? AND created_at >= ?))",
                (self.subject_id, project.project_id, project.goal_id, project.created_at),
            ).fetchall()
            for row in research:
                self._record_resource_use_connection(
                    connection,
                    project.project_id,
                    "search",
                    1,
                    "research_run",
                    str(row["research_id"]),
                    self.clock(),
                )
            actions = connection.execute(
                "SELECT action_id FROM actions WHERE subject_id = ? "
                "AND ((project_id = ? AND phase_id IS NOT NULL) OR "
                "(project_id IS NULL AND goal_id = ? AND prepared_at >= ?))",
                (self.subject_id, project.project_id, project.goal_id, project.created_at),
            ).fetchall()
            for row in actions:
                self._record_resource_use_connection(
                    connection,
                    project.project_id,
                    "external_action",
                    1,
                    "action",
                    str(row["action_id"]),
                    self.clock(),
                )

    def _local_resource_decision(self, project: AutonomousProjectRecord) -> str | None:
        with self.database.connection() as connection:
            usage = self._usage_connection(connection, project.project_id)
            reviews = int(
                connection.execute(
                    "SELECT COUNT(*) FROM autonomous_project_reviews WHERE project_id = ?",
                    (project.project_id,),
                ).fetchone()[0]
            )
            phase = None
            if project.current_phase_id is not None:
                phase = connection.execute(
                    "SELECT no_progress_count FROM autonomous_project_phases WHERE phase_id = ?",
                    (project.current_phase_id,),
                ).fetchone()
            affect_rows = self._affect_rows_connection(
                connection, project.goal_id, project.project_id
            )
        exhausted = self._exhausted_resources(project, usage)
        if exhausted and project.status not in {"paused", "blocked"}:
            return self._apply_local_transition(
                project,
                "paused",
                f"bounded resources exhausted: {','.join(exhausted)}",
                reason_code="resource_budget_reached",
            )
        if reviews >= project.budget.max_cycles and project.status not in {"paused", "blocked"}:
            return self._apply_local_transition(
                project,
                "paused",
                "maximum project cognition cycles reached",
                reason_code="cycle_budget_reached",
            )
        retry_limit = self._dynamic_no_progress_limit(affect_rows)
        if (
            phase is not None
            and int(phase["no_progress_count"]) >= retry_limit
            and project.status == "active"
        ):
            return self._apply_local_transition(
                project,
                "paused",
                "current phase reached the no-progress limit",
                reason_code="no_progress_limit_reached",
            )
        return None

    def _dynamic_no_progress_limit(self, affect_rows: list[Any]) -> int:
        """Bounded persistence shaped by current affect, never an unbounded retry."""
        base = self.settings.max_project_no_progress_reviews
        motivation = 0.0
        inhibition = 0.0
        for row in affect_rows:
            emotion = str(row["emotion_type"]).casefold()
            intensity = float(row["intensity"])
            valence = float(row["valence"])
            if emotion in {"curiosity", "hope", "interest", "determination"}:
                motivation = max(motivation, intensity)
            if emotion in {"frustration", "fatigue", "fear", "boredom", "despair"}:
                inhibition = max(inhibition, intensity)
            elif valence < -0.4:
                inhibition = max(inhibition, intensity * abs(valence))
        adjustment = round(motivation * 3 - inhibition * 3)
        return max(1, min(12, base + adjustment))

    def _affect_rows_connection(self, connection: Any, goal_id: str, project_id: str) -> list[Any]:
        return list(
            connection.execute(
                """SELECT emotion_type, intensity, valence FROM affect_components
                   WHERE subject_id = ? AND (
                       (target_type = 'goal' AND target_id = ?) OR
                       (target_type = 'project' AND target_id = ?) OR target_type = 'world'
                   ) ORDER BY intensity DESC LIMIT 16""",
                (self.subject_id, goal_id, project_id),
            ).fetchall()
        )

    def _apply_local_transition(
        self,
        project: AutonomousProjectRecord,
        status: str,
        reason: str,
        *,
        reason_code: str,
    ) -> str:
        if status not in PROJECT_STATUS_TRANSITIONS[project.status]:
            return "autonomous_project_waited"
        now = self.clock()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM autonomous_projects WHERE project_id = ?", (project.project_id,)
            ).fetchone()
            assert row is not None
            revision = int(row["current_revision"]) + 1
            budget = self._budget_from_row(row)
            state_hash = self._project_hash(
                row["goal_id"],
                row["formation_call_id"],
                row["project_key"],
                row["project_type"],
                row["title"],
                row["purpose"],
                row["deliverable"],
                self._strings(row["acceptance_criteria_json"], "project criteria"),
                row["size_class"],
                float(row["estimated_duration_hours"]),
                status,
                float(row["progress"]),
                row["current_phase_id"],
                budget,
                self._strings(row["source_event_ids_json"], "project sources"),
                self._strings(row["source_value_ids_json"], "project values"),
                row["source_mission_id"],
                revision,
            )
            connection.execute(
                "UPDATE autonomous_projects SET status = ?, state_hash = ?, "
                "current_revision = ?, updated_at = ? WHERE project_id = ?",
                (status, state_hash, revision, now, project.project_id),
            )
            self._insert_project_revision(
                connection,
                project.project_id,
                revision,
                status,
                float(row["progress"]),
                row["current_phase_id"],
                budget,
                reason,
                (),
                now,
            )
            self.events._append_connection(
                connection,
                self.subject_id,
                "autonomous_project_bounded",
                "project_supervisor",
                {
                    "project_id": project.project_id,
                    "status": status,
                    "reason_code": reason_code,
                },
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=now,
                event_id=None,
            )
        return f"autonomous_project_{status}"

    def _create_assistance_request_connection(
        self,
        connection: Any,
        project: AutonomousProjectRecord,
        phase: AutonomousProjectPhaseRecord,
        proposal: AutonomousProjectReviewProposal,
        now: str,
    ) -> None:
        existing = connection.execute(
            "SELECT request_id FROM autonomous_project_assistance_requests "
            "WHERE project_id = ? AND status = 'open'",
            (project.project_id,),
        ).fetchone()
        if existing is not None:
            raise AutonomousProjectValidationError("project already has an open help request")
        assert proposal.help_title is not None
        assert proposal.help_description is not None
        assert proposal.help_public_summary is not None
        payload = {
            "subject_id": self.subject_id,
            "project_id": project.project_id,
            "phase_id": phase.phase_id,
            "request_kind": "human_help",
            "title": proposal.help_title,
            "description": proposal.help_description,
            "public_summary": proposal.help_public_summary,
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }
        connection.execute(
            """INSERT INTO autonomous_project_assistance_requests(
                request_id, subject_id, project_id, phase_id, request_kind, title,
                description, public_summary, status, state_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'human_help', ?, ?, ?, 'open', ?, ?, ?)""",
            (
                new_id("projecthelp"),
                self.subject_id,
                project.project_id,
                phase.phase_id,
                proposal.help_title,
                proposal.help_description,
                proposal.help_public_summary,
                content_hash(payload),
                now,
                now,
            ),
        )

    def _revise_phase_connection(
        self,
        connection: Any,
        phase: AutonomousProjectPhaseRecord,
        *,
        status: str,
        attempt_count: int,
        no_progress_count: int,
        reason: str,
        evidence_event_ids: tuple[str, ...],
        now: str,
    ) -> None:
        if status != phase.status and status not in {
            "active",
            "completed",
            "blocked",
            "skipped",
        }:
            raise AutonomousProjectValidationError("project phase transition is invalid")
        revision = phase.current_revision + 1
        state_hash = self._phase_hash(
            phase.project_id,
            phase.phase_key,
            phase.position,
            phase.title,
            phase.objective,
            phase.output_type,
            phase.acceptance_criteria,
            phase.dependency_keys,
            status,
            attempt_count,
            no_progress_count,
            revision,
        )
        completed_at = now if status == "completed" else phase.completed_at
        connection.execute(
            """UPDATE autonomous_project_phases SET status = ?, attempt_count = ?,
                no_progress_count = ?, state_hash = ?, current_revision = ?, updated_at = ?,
                completed_at = ? WHERE phase_id = ?""",
            (
                status,
                attempt_count,
                no_progress_count,
                state_hash,
                revision,
                now,
                completed_at,
                phase.phase_id,
            ),
        )
        self._insert_phase_revision(
            connection,
            phase.phase_id,
            revision,
            status,
            attempt_count,
            no_progress_count,
            reason,
            evidence_event_ids,
            now,
        )

    @staticmethod
    def _insert_project_revision(
        connection: Any,
        project_id: str,
        revision: int,
        status: str,
        progress: float,
        current_phase_id: str | None,
        budget: ProjectBudget,
        reason: str,
        evidence_event_ids: tuple[str, ...],
        now: str,
    ) -> None:
        connection.execute(
            """INSERT INTO autonomous_project_revisions(
                revision_id, project_id, revision_number, status, progress, current_phase_id,
                max_cycles, max_model_calls, max_searches, max_external_actions,
                max_storage_bytes, reason, evidence_event_ids_json, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("projectrev"),
                project_id,
                revision,
                status,
                progress,
                current_phase_id,
                *AutonomousProjectManager._budget_values(budget),
                reason,
                canonical_json(list(evidence_event_ids)),
                AutonomousProjectManager._project_revision_hash(
                    status,
                    progress,
                    current_phase_id,
                    budget,
                    reason,
                    evidence_event_ids,
                ),
                now,
            ),
        )

    @staticmethod
    def _insert_phase_revision(
        connection: Any,
        phase_id: str,
        revision: int,
        status: str,
        attempt_count: int,
        no_progress_count: int,
        reason: str,
        evidence_event_ids: tuple[str, ...],
        now: str,
    ) -> None:
        connection.execute(
            """INSERT INTO autonomous_project_phase_revisions(
                revision_id, phase_id, revision_number, status, attempt_count,
                no_progress_count, reason, evidence_event_ids_json, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("phaserev"),
                phase_id,
                revision,
                status,
                attempt_count,
                no_progress_count,
                reason,
                canonical_json(list(evidence_event_ids)),
                AutonomousProjectManager._phase_revision_hash(
                    status,
                    attempt_count,
                    no_progress_count,
                    reason,
                    evidence_event_ids,
                ),
                now,
            ),
        )

    @staticmethod
    def _record_resource_use_connection(
        connection: Any,
        project_id: str,
        resource_type: str,
        quantity: int,
        source_type: str,
        source_id: str,
        now: str,
    ) -> None:
        subject = connection.execute(
            "SELECT subject_id FROM autonomous_projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if subject is None:
            raise AutonomousProjectValidationError("project resource owner is unavailable")
        payload = {
            "subject_id": subject["subject_id"],
            "project_id": project_id,
            "resource_type": resource_type,
            "quantity": quantity,
            "source_type": source_type,
            "source_id": source_id,
            "created_at": now,
        }
        connection.execute(
            """INSERT OR IGNORE INTO autonomous_project_resource_uses(
                usage_id, subject_id, project_id, resource_type, quantity,
                source_type, source_id, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("projectuse"),
                subject["subject_id"],
                project_id,
                resource_type,
                quantity,
                source_type,
                source_id,
                content_hash(payload),
                now,
            ),
        )

    def _successful_formation(
        self, purpose: str, context: _FormationContext
    ) -> tuple[AutonomousProjectFormationProposal, str] | None:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT call_id, response_json FROM model_calls WHERE subject_id = ? "
                "AND purpose = ? AND status = 'succeeded' ORDER BY created_at DESC",
                (self.subject_id, purpose),
            ).fetchall()
        for row in rows:
            proposal = AutonomousProjectFormationProposal.model_validate_json(
                self._response_content(row["response_json"])
            )
            try:
                self._validate_formation(proposal, context)
            except AutonomousProjectValidationError:
                continue
            return proposal, str(row["call_id"])
        return None

    def _successful_review(
        self, purpose: str, context: _ReviewContext
    ) -> tuple[AutonomousProjectReviewProposal, str] | None:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT call_id, response_json FROM model_calls WHERE subject_id = ? "
                "AND purpose = ? AND status = 'succeeded' ORDER BY created_at DESC",
                (self.subject_id, purpose),
            ).fetchall()
        for row in rows:
            proposal = AutonomousProjectReviewProposal.model_validate_json(
                self._response_content(row["response_json"])
            )
            try:
                self._validate_review(proposal, context)
            except AutonomousProjectValidationError:
                continue
            return proposal, str(row["call_id"])
        return None

    def begin_execution(self, project_id: str, execution_kind: str) -> str:
        if execution_kind not in {"project_review", "phase_execution"}:
            raise AutonomousProjectValidationError("project execution clock kind is invalid")
        now = self.clock()
        session_id = new_id("project-clock-session")
        with self.database.transaction() as connection:
            project = connection.execute(
                "SELECT subject_id, status FROM autonomous_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if (
                project is None
                or project["subject_id"] != self.subject_id
                or project["status"] != "active"
            ):
                raise AutonomousProjectValidationError(
                    "only an active owned project can consume execution time"
                )
            latest = connection.execute(
                "SELECT * FROM autonomous_project_execution_clock_events "
                "WHERE project_id = ? ORDER BY sequence DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if latest is not None and latest["action"] == "start":
                raise AutonomousProjectValidationError("project execution clock is already active")
            sequence = 1 if latest is None else int(latest["sequence"]) + 1
            reason = "bounded project execution started"
            state_hash = self._clock_event_hash(
                project_id,
                self.subject_id,
                sequence,
                session_id,
                execution_kind,
                "start",
                0.0,
                reason,
                now,
            )
            connection.execute(
                "INSERT INTO autonomous_project_execution_clock_events(clock_event_id, "
                "project_id, subject_id, sequence, session_id, execution_kind, action, "
                "active_delta_seconds, reason, state_hash, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'start', 0, ?, ?, ?)",
                (
                    new_id("project-clock"),
                    project_id,
                    self.subject_id,
                    sequence,
                    session_id,
                    execution_kind,
                    reason,
                    state_hash,
                    now,
                ),
            )
        return session_id

    def finish_execution(self, project_id: str, session_id: str) -> float:
        now = self.clock()
        with self.database.transaction() as connection:
            latest = connection.execute(
                "SELECT * FROM autonomous_project_execution_clock_events "
                "WHERE project_id = ? ORDER BY sequence DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if (
                latest is None
                or latest["subject_id"] != self.subject_id
                or latest["action"] != "start"
                or latest["session_id"] != session_id
            ):
                raise AutonomousProjectValidationError(
                    "project execution clock finish has no matching session"
                )
            delta = (
                self._parse_time(now) - self._parse_time(str(latest["occurred_at"]))
            ).total_seconds()
            if delta < 0:
                raise AutonomousProjectValidationError("project execution clock moved backwards")
            delta = round(delta, 6)
            sequence = int(latest["sequence"]) + 1
            reason = "bounded project execution stopped"
            state_hash = self._clock_event_hash(
                project_id,
                self.subject_id,
                sequence,
                session_id,
                str(latest["execution_kind"]),
                "stop",
                delta,
                reason,
                now,
            )
            connection.execute(
                "INSERT INTO autonomous_project_execution_clock_events(clock_event_id, "
                "project_id, subject_id, sequence, session_id, execution_kind, action, "
                "active_delta_seconds, reason, state_hash, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'stop', ?, ?, ?, ?)",
                (
                    new_id("project-clock"),
                    project_id,
                    self.subject_id,
                    sequence,
                    session_id,
                    latest["execution_kind"],
                    delta,
                    reason,
                    state_hash,
                    now,
                ),
            )
        return delta

    def active_execution_seconds(self, project_id: str) -> float:
        with self.database.connection() as connection:
            project = connection.execute(
                "SELECT subject_id FROM autonomous_projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project is None or project["subject_id"] != self.subject_id:
                raise AutonomousProjectValidationError("project execution clock is unavailable")
            total = connection.execute(
                "SELECT COALESCE(SUM(active_delta_seconds), 0) "
                "FROM autonomous_project_execution_clock_events "
                "WHERE project_id = ? AND action = 'stop'",
                (project_id,),
            ).fetchone()[0]
        return float(total)

    def _recover_execution_clocks(self) -> None:
        now = self.clock()
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT latest.* FROM autonomous_project_execution_clock_events latest "
                "JOIN autonomous_projects projects ON projects.project_id = latest.project_id "
                "WHERE projects.subject_id = ? AND latest.action = 'start' AND NOT EXISTS ("
                "SELECT 1 FROM autonomous_project_execution_clock_events newer "
                "WHERE newer.project_id = latest.project_id AND newer.sequence > latest.sequence) "
                "ORDER BY latest.project_id",
                (self.subject_id,),
            ).fetchall()
            for row in rows:
                sequence = int(row["sequence"]) + 1
                reason = "interrupted execution recovered without charging offline time"
                state_hash = self._clock_event_hash(
                    str(row["project_id"]),
                    self.subject_id,
                    sequence,
                    str(row["session_id"]),
                    str(row["execution_kind"]),
                    "recover",
                    0.0,
                    reason,
                    now,
                )
                connection.execute(
                    "INSERT INTO autonomous_project_execution_clock_events(clock_event_id, "
                    "project_id, subject_id, sequence, session_id, execution_kind, action, "
                    "active_delta_seconds, reason, state_hash, occurred_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'recover', 0, ?, ?, ?)",
                    (
                        new_id("project-clock"),
                        row["project_id"],
                        self.subject_id,
                        sequence,
                        row["session_id"],
                        row["execution_kind"],
                        reason,
                        state_hash,
                        now,
                    ),
                )

    def _selected_project(self) -> AutonomousProjectRecord | None:
        self._expire_overdue_projects()
        projects = self.projects(statuses=NONTERMINAL_PROJECT_STATUSES)
        if not projects:
            return None
        order = {"active": 0, "planned": 1, "blocked": 2, "paused": 3}
        return min(
            projects,
            key=lambda project: (order[project.status], project.updated_at, project.project_id),
        )

    def _expire_overdue_projects(self) -> bool:
        expired = False
        for project in self.projects(statuses=("active",)):
            active_limit = (
                max(MIN_PROJECT_ACTIVE_DURATION_HOURS, project.estimated_duration_hours) * 3_600
            )
            if self.active_execution_seconds(project.project_id) < active_limit:
                continue
            self._apply_local_transition(
                project,
                "abandoned",
                "project active-execution deadline reached",
                reason_code="active_execution_deadline_reached",
            )
            expired = True
        return expired

    def _review_due(self, project: AutonomousProjectRecord) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT created_at FROM autonomous_project_reviews WHERE project_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (project.project_id,),
            ).fetchone()
        reference = project.updated_at if row is None else str(row["created_at"])
        return self._interval_due(reference, self.settings.project_review_interval_seconds)

    def _has_capability(self, capability_type: str) -> bool:
        with self.database.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM capability_grants WHERE subject_id = ? AND capability_type = ? "
                    "AND status = 'active' AND requires_approval = 0 LIMIT 1",
                    (self.subject_id, capability_type),
                ).fetchone()
                is not None
            )

    def _calls_today(self, prefix: str) -> int:
        day = self.clock()[:10]
        with self.database.connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM model_calls WHERE subject_id = ? "
                    "AND purpose LIKE ? AND substr(created_at, 1, 10) = ?",
                    (self.subject_id, f"{prefix}%", day),
                ).fetchone()[0]
            )

    def _formation_call_count(self) -> int:
        with self.database.connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM model_calls WHERE subject_id = ? "
                    "AND purpose LIKE 'autonomous_project_formation:%'",
                    (self.subject_id,),
                ).fetchone()[0]
            )

    def _project_model_calls(self, project_id: str) -> int:
        with self.database.connection() as connection:
            return self._usage_connection(connection, project_id)["model_call"]

    def _review_count(self, project_id: str) -> int:
        with self.database.connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM autonomous_project_reviews WHERE project_id = ?",
                    (project_id,),
                ).fetchone()[0]
            )

    def _latest_project_time(self) -> str | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT updated_at FROM autonomous_projects WHERE subject_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
        return None if row is None else str(row["updated_at"])

    def _formation_hard_limits(self) -> dict[str, int | float]:
        return {
            "max_cycles": self.settings.max_project_cycles,
            "max_model_calls": self.settings.max_project_model_calls,
            "max_searches": self.settings.max_project_searches,
            "max_external_actions": self.settings.max_project_external_actions,
            "max_storage_bytes": self.settings.max_project_storage_bytes,
            "max_duration_hours": self.settings.max_project_duration_hours,
            "max_phases": self.settings.max_project_phases,
        }

    @staticmethod
    def _usage_connection(connection: Any, project_id: str) -> dict[str, int]:
        rows = connection.execute(
            "SELECT resource_type, COALESCE(SUM(quantity), 0) AS quantity "
            "FROM autonomous_project_resource_uses WHERE project_id = ? GROUP BY resource_type",
            (project_id,),
        ).fetchall()
        result = {
            "model_call": 0,
            "search": 0,
            "external_action": 0,
            "storage": 0,
        }
        for row in rows:
            result[str(row["resource_type"])] = int(row["quantity"])
        return result

    @staticmethod
    def _remaining(project: AutonomousProjectRecord, usage: dict[str, int]) -> dict[str, int]:
        return {
            "model_call": max(0, project.budget.max_model_calls - usage["model_call"]),
            "search": max(0, project.budget.max_searches - usage["search"]),
            "external_action": max(
                0, project.budget.max_external_actions - usage["external_action"]
            ),
            "storage": max(0, project.budget.max_storage_bytes - usage["storage"]),
        }

    @staticmethod
    def _exhausted_resources(
        project: AutonomousProjectRecord, usage: dict[str, int]
    ) -> tuple[str, ...]:
        limits = {
            "model_call": project.budget.max_model_calls,
            "search": project.budget.max_searches,
            "external_action": project.budget.max_external_actions,
            "storage": project.budget.max_storage_bytes,
        }
        return tuple(key for key, limit in limits.items() if limit > 0 and usage[key] >= limit)

    @staticmethod
    def _phase_rows_connection(connection: Any, project_id: str) -> list[Any]:
        return list(
            connection.execute(
                "SELECT * FROM autonomous_project_phases WHERE project_id = ? ORDER BY position",
                (project_id,),
            ).fetchall()
        )

    def _budget_fatigue(self, reason: str) -> None:
        self.fatigue.assess(
            self.subject_id,
            FatigueInputs(
                resource_pressure=1,
                cognitive_load=0.2,
                frustration=0.2,
                goal_conflict=0,
                staleness=0,
            ),
            reason=reason,
        )

    def _interval_due(self, reference: str, seconds: float) -> bool:
        now = self._parse_time(self.clock())
        then = self._parse_time(reference)
        return (now - then).total_seconds() >= seconds

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("project time requires a timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _project_key(goal_id: str, title: str, deliverable: str) -> str:
        normalized = re.sub(r"\s+", " ", f"{title} {deliverable}".strip().casefold())
        return content_hash({"goal_id": goal_id, "project": normalized})

    @staticmethod
    def _response_content(raw: str) -> str:
        response = durable_json(
            decompress_text(raw) or "null", "autonomous project model response", "response"
        )
        content = response.get("content") if isinstance(response, dict) else None
        if not isinstance(content, str):
            raise IntegrityError("autonomous project model response has no content")
        return content

    @staticmethod
    def _strings(raw: str, label: str, identifier: object | None = None) -> tuple[str, ...]:
        return durable_string_list(raw, label, label if identifier is None else identifier)

    @staticmethod
    def _persisted_text(value: Any, context: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise IntegrityError(f"{context} is invalid")
        return value

    @classmethod
    def _persisted_enum(cls, value: Any, allowed: frozenset[str], context: str) -> str:
        text = cls._persisted_text(value, context)
        if text not in allowed:
            raise IntegrityError(f"{context} is invalid")
        return text

    @classmethod
    def _persisted_hash(cls, value: Any, context: str) -> str:
        text = cls._persisted_text(value, context)
        if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
            raise IntegrityError(f"{context} is invalid")
        return text

    @classmethod
    def _persisted_timestamp(cls, value: Any, context: str, identifier: object) -> str:
        text = cls._persisted_text(value, f"{context} timestamp")
        try:
            cls._parse_time(text)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"{context} timestamp is invalid: {identifier}") from error
        return text

    @classmethod
    def _canonical_string_list(
        cls, value: Any, context: str, identifier: object
    ) -> tuple[str, ...]:
        strings = cls._strings(value, context, identifier)
        if (
            value != canonical_json(list(strings))
            or any(not item.strip() for item in strings)
            or len(set(strings)) != len(strings)
        ):
            raise IntegrityError(f"{context} is invalid: {identifier}")
        return strings

    @staticmethod
    def _budget_values(budget: ProjectBudget) -> tuple[int, int, int, int, int]:
        return (
            budget.max_cycles,
            budget.max_model_calls,
            budget.max_searches,
            budget.max_external_actions,
            budget.max_storage_bytes,
        )

    @staticmethod
    def _budget_from_row(row: Any, identifier: object = "project budget") -> ProjectBudget:
        return ProjectBudget(
            durable_int(row["max_cycles"], "project budget", identifier),
            durable_int(row["max_model_calls"], "project budget", identifier),
            durable_int(row["max_searches"], "project budget", identifier),
            durable_int(row["max_external_actions"], "project budget", identifier),
            durable_int(row["max_storage_bytes"], "project budget", identifier),
        )

    @classmethod
    def _project_from_row(cls, row: Any) -> AutonomousProjectRecord:
        project_id = row["project_id"]
        with durable_boundary("autonomous project", project_id):
            criteria = cls._strings(row["acceptance_criteria_json"], "project criteria", project_id)
            source_events = cls._strings(
                row["source_event_ids_json"], "project sources", project_id
            )
            source_values = cls._strings(row["source_value_ids_json"], "project values", project_id)
            budget = cls._budget_from_row(row, project_id)
            estimated_duration = durable_float(
                row["estimated_duration_hours"], "autonomous project", project_id
            )
            progress = durable_float(row["progress"], "autonomous project", project_id)
            current_revision = durable_int(
                row["current_revision"], "autonomous project", project_id
            )
            expected = cls._project_hash(
                row["goal_id"],
                row["formation_call_id"],
                row["project_key"],
                row["project_type"],
                row["title"],
                row["purpose"],
                row["deliverable"],
                criteria,
                row["size_class"],
                estimated_duration,
                row["status"],
                progress,
                row["current_phase_id"],
                budget,
                source_events,
                source_values,
                row["source_mission_id"],
                current_revision,
            )
            if expected != row["state_hash"]:
                raise IntegrityError(f"autonomous project hash mismatch: {project_id}")
            return AutonomousProjectRecord(
                project_id,
                row["subject_id"],
                row["goal_id"],
                row["project_type"],
                row["title"],
                row["purpose"],
                row["deliverable"],
                criteria,
                row["size_class"],
                estimated_duration,
                row["status"],
                progress,
                row["current_phase_id"],
                budget,
                current_revision,
                row["created_at"],
                row["updated_at"],
                row["completed_at"],
            )

    @classmethod
    def _phase_from_row(cls, row: Any) -> AutonomousProjectPhaseRecord:
        phase_id = row["phase_id"]
        with durable_boundary("autonomous project phase", phase_id):
            criteria = cls._strings(row["acceptance_criteria_json"], "phase criteria", phase_id)
            dependencies = cls._strings(row["dependency_keys_json"], "phase dependencies", phase_id)
            position = durable_int(row["position"], "autonomous project phase", phase_id)
            attempt_count = durable_int(row["attempt_count"], "autonomous project phase", phase_id)
            no_progress_count = durable_int(
                row["no_progress_count"], "autonomous project phase", phase_id
            )
            current_revision = durable_int(
                row["current_revision"], "autonomous project phase", phase_id
            )
            expected = cls._phase_hash(
                row["project_id"],
                row["phase_key"],
                position,
                row["title"],
                row["objective"],
                row["output_type"],
                criteria,
                dependencies,
                row["status"],
                attempt_count,
                no_progress_count,
                current_revision,
            )
            if expected != row["state_hash"]:
                raise IntegrityError(f"autonomous project phase hash mismatch: {phase_id}")
            return AutonomousProjectPhaseRecord(
                phase_id,
                row["project_id"],
                row["phase_key"],
                position,
                row["title"],
                row["objective"],
                row["output_type"],
                criteria,
                dependencies,
                row["status"],
                attempt_count,
                no_progress_count,
                current_revision,
                row["created_at"],
                row["updated_at"],
                row["completed_at"],
            )

    @classmethod
    def _review_from_row(cls, row: Any) -> AutonomousProjectReviewRecord:
        review_id = row["review_id"]
        with durable_boundary("autonomous project review", review_id):
            evidence = cls._strings(
                row["evidence_event_ids_json"], "project review evidence", review_id
            )
            proposal = durable_json(
                row["proposal_json"], "autonomous project review proposal", review_id
            )
            if content_hash(proposal) != row["proposal_hash"]:
                raise IntegrityError(f"autonomous project review proposal mismatch: {review_id}")
            expected = cls._review_hash(
                row["project_id"],
                row["model_call_id"],
                row["idempotency_key"],
                row["disposition"],
                row["phase_id"],
                row["summary"],
                row["reason"],
                evidence,
                row["proposal_hash"],
                row["resulting_status"],
                row["created_at"],
            )
            if expected != row["state_hash"]:
                raise IntegrityError(f"autonomous project review hash mismatch: {review_id}")
            return AutonomousProjectReviewRecord(
                review_id,
                row["subject_id"],
                row["project_id"],
                row["model_call_id"],
                row["disposition"],
                row["phase_id"],
                row["summary"],
                row["resulting_status"],
                row["created_at"],
            )

    @staticmethod
    def _project_public_context(project: AutonomousProjectRecord) -> dict[str, Any]:
        return {
            "project_id": project.project_id,
            "goal_id": project.goal_id,
            "project_type": project.project_type,
            "title": project.title,
            "purpose": project.purpose,
            "deliverable": project.deliverable,
            "acceptance_criteria": list(project.acceptance_criteria),
            "size_class": project.size_class,
            "estimated_duration_hours": project.estimated_duration_hours,
            "status": project.status,
            "progress": project.progress,
            "current_phase_id": project.current_phase_id,
            "budget": project.budget.__dict__,
        }

    @staticmethod
    def _phase_context(phase: AutonomousProjectPhaseRecord) -> dict[str, Any]:
        return {
            "phase_id": phase.phase_id,
            "phase_key": phase.phase_key,
            "position": phase.position,
            "title": phase.title,
            "objective": phase.objective,
            "output_type": phase.output_type,
            "acceptance_criteria": list(phase.acceptance_criteria),
            "dependency_keys": list(phase.dependency_keys),
            "status": phase.status,
            "attempt_count": phase.attempt_count,
            "no_progress_count": phase.no_progress_count,
        }

    @staticmethod
    def _project_hash(
        goal_id: str,
        formation_call_id: str,
        project_key: str,
        project_type: str,
        title: str,
        purpose: str,
        deliverable: str,
        acceptance_criteria: tuple[str, ...],
        size_class: str,
        estimated_duration_hours: float,
        status: str,
        progress: float,
        current_phase_id: str | None,
        budget: ProjectBudget,
        source_event_ids: tuple[str, ...],
        source_value_ids: tuple[str, ...],
        source_mission_id: str | None,
        revision: int,
    ) -> str:
        return content_hash(
            {
                "goal_id": goal_id,
                "formation_call_id": formation_call_id,
                "project_key": project_key,
                "project_type": project_type,
                "title": title,
                "purpose": purpose,
                "deliverable": deliverable,
                "acceptance_criteria": list(acceptance_criteria),
                "size_class": size_class,
                "estimated_duration_hours": estimated_duration_hours,
                "status": status,
                "progress": progress,
                "current_phase_id": current_phase_id,
                "budget": budget.__dict__,
                "source_event_ids": list(source_event_ids),
                "source_value_ids": list(source_value_ids),
                "source_mission_id": source_mission_id,
                "revision": revision,
            }
        )

    @staticmethod
    def _clock_event_hash(
        project_id: str,
        subject_id: str,
        sequence: int,
        session_id: str,
        execution_kind: str,
        action: str,
        active_delta_seconds: float,
        reason: str,
        occurred_at: str,
    ) -> str:
        return content_hash(
            {
                "project_id": project_id,
                "subject_id": subject_id,
                "sequence": sequence,
                "session_id": session_id,
                "execution_kind": execution_kind,
                "action": action,
                "active_delta_seconds": active_delta_seconds,
                "reason": reason,
                "occurred_at": occurred_at,
            }
        )

    @staticmethod
    def _project_revision_hash(
        status: str,
        progress: float,
        current_phase_id: str | None,
        budget: ProjectBudget,
        reason: str,
        evidence_event_ids: tuple[str, ...],
    ) -> str:
        return content_hash(
            {
                "status": status,
                "progress": progress,
                "current_phase_id": current_phase_id,
                "budget": budget.__dict__,
                "reason": reason,
                "evidence_event_ids": list(evidence_event_ids),
            }
        )

    @staticmethod
    def _phase_hash(
        project_id: str,
        phase_key: str,
        position: int,
        title: str,
        objective: str,
        output_type: str,
        acceptance_criteria: tuple[str, ...],
        dependency_keys: tuple[str, ...],
        status: str,
        attempt_count: int,
        no_progress_count: int,
        revision: int,
    ) -> str:
        return content_hash(
            {
                "project_id": project_id,
                "phase_key": phase_key,
                "position": position,
                "title": title,
                "objective": objective,
                "output_type": output_type,
                "acceptance_criteria": list(acceptance_criteria),
                "dependency_keys": list(dependency_keys),
                "status": status,
                "attempt_count": attempt_count,
                "no_progress_count": no_progress_count,
                "revision": revision,
            }
        )

    @staticmethod
    def _phase_revision_hash(
        status: str,
        attempt_count: int,
        no_progress_count: int,
        reason: str,
        evidence_event_ids: tuple[str, ...],
    ) -> str:
        return content_hash(
            {
                "status": status,
                "attempt_count": attempt_count,
                "no_progress_count": no_progress_count,
                "reason": reason,
                "evidence_event_ids": list(evidence_event_ids),
            }
        )

    @staticmethod
    def _review_hash(
        project_id: str,
        call_id: str,
        idempotency_key: str,
        disposition: str,
        phase_id: str,
        summary: str,
        reason: str,
        evidence_event_ids: tuple[str, ...],
        proposal_hash: str,
        resulting_status: str,
        created_at: str,
    ) -> str:
        return content_hash(
            {
                "project_id": project_id,
                "call_id": call_id,
                "idempotency_key": idempotency_key,
                "disposition": disposition,
                "phase_id": phase_id,
                "summary": summary,
                "reason": reason,
                "evidence_event_ids": list(evidence_event_ids),
                "proposal_hash": proposal_hash,
                "resulting_status": resulting_status,
                "created_at": created_at,
            }
        )
