"""Durable, revision-bound project review waits using the existing workflow contract."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import content_hash

if TYPE_CHECKING:
    from .projects import AutonomousProjectRecord

REVIEW_WAIT_REASONS = frozenset(
    {
        "review_due",
        "subject_wait",
        "review_committed",
        "waiting_daily_quota",
        "waiting_budget",
        "review_backoff",
        "review_retry_limit",
        "awaiting_reconciliation",
        "waiting_dependencies",
        "help_pending_route",
        "help_waiting_reply",
        "help_delivery_pending",
        "help_delivery_unknown",
        "help_public_summary_requires_review",
    }
)
MAX_REVIEW_RETRIES = 3


class ProjectReviewControl:
    """Control records do not change phase state, attempts, progress or historical clocks."""

    def __init__(self, database: Database, subject_id: str):
        self.database = database
        self.subject_id = subject_id
        self.workflows = DurableWorkflowStore(database)

    def key(self, project: AutonomousProjectRecord) -> str:
        if project.subject_id != self.subject_id:
            raise PermissionError("project review control belongs to another subject")
        return "project-review-control:" + content_hash(
            {
                "subject": self.subject_id,
                "project": project.project_id,
                "revision": project.current_revision,
            }
        )

    def read(self, project: AutonomousProjectRecord) -> dict[str, Any]:
        key = self.key(project)
        try:
            record = self.workflows.latest(key)
        except NotFoundError:
            return {
                "version": 1,
                "project_id": project.project_id,
                "revision": project.current_revision,
                "reason": "review_due",
                "retry_index": 0,
                "request_index": 0,
                "next_due_at": None,
            }
        return self.decode_record(
            record, self.subject_id, project.project_id, project.current_revision
        )

    @staticmethod
    def decode(row: Any, subject_id: str, project_id: str, revision: int) -> dict[str, Any]:
        return ProjectReviewControl.decode_record(
            DurableWorkflowStore._from_row(row), subject_id, project_id, revision
        )

    @staticmethod
    def decode_record(
        record: Any, subject_id: str, project_id: str, revision: int
    ) -> dict[str, Any]:
        state = record.state
        if (
            record.subject_id != subject_id
            or record.workflow_type != "project_review_control"
            or record.status != "running"
            or type(state.get("version")) is not int
            or state.get("version") != 1
            or state.get("project_id") != project_id
            or type(state.get("revision")) is not int
            or state.get("revision") != revision
            or state.get("reason") not in REVIEW_WAIT_REASONS
            or type(state.get("retry_index")) is not int
            or not 0 <= state["retry_index"] <= MAX_REVIEW_RETRIES
            or type(state.get("request_index")) is not int
            or state["request_index"] < 0
        ):
            raise IntegrityError("project review control is invalid")
        due = state.get("next_due_at")
        if due is not None:
            try:
                parsed = datetime.fromisoformat(due)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError("naive timestamp")
            except (TypeError, ValueError) as error:
                raise IntegrityError("project review due time is invalid") from error
        return dict(state)

    def record(
        self,
        project: AutonomousProjectRecord,
        reason: str,
        *,
        next_due_at: str | None = None,
        retry_index: int | None = None,
        request_index: int | None = None,
    ) -> None:
        if reason not in REVIEW_WAIT_REASONS:
            raise ValueError("unknown project review wait reason")
        previous = self.read(project)
        state = {
            **previous,
            "reason": reason,
            "next_due_at": next_due_at,
            "retry_index": previous["retry_index"] if retry_index is None else retry_index,
            "request_index": previous["request_index"] if request_index is None else request_index,
        }
        if state == previous:
            return
        self.workflows.checkpoint(
            self.subject_id,
            self.key(project),
            "project_review_control",
            "running",
            state,
            reason=reason,
        )


def project_dependencies_pending(connection: Any, subject_id: str, project: Any) -> bool:
    return (
        connection.execute(
            "SELECT 1 WHERE EXISTS(SELECT 1 FROM research_search_runs r "
            "WHERE r.subject_id=? AND r.status IN ('accepted','no_results','failed') "
            "AND (r.project_id=? OR "
            "(r.project_id IS NULL AND r.goal_id=? AND r.created_at>=?)) AND NOT EXISTS("
            "SELECT 1 FROM outcome_evaluations e WHERE e.subject_id=r.subject_id "
            "AND e.source_type='research' AND e.source_id=r.research_id)) OR EXISTS("
            "SELECT 1 FROM action_deliberation_runs a WHERE a.subject_id=? "
            "AND (a.project_id=? OR "
            "(a.project_id IS NULL AND a.goal_id=? AND a.created_at>=?)) "
            "AND a.action_id IS NOT NULL AND NOT EXISTS("
            "SELECT 1 FROM outcome_evaluations e WHERE e.subject_id=a.subject_id "
            "AND e.source_type='action' AND e.source_id=a.deliberation_id))",
            (
                subject_id,
                project["project_id"],
                project["goal_id"],
                project["created_at"],
                subject_id,
                project["project_id"],
                project["goal_id"],
                project["created_at"],
            ),
        ).fetchone()
        is not None
    )
