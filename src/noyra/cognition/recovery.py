"""Detect an old project replay loop and retire its temporary guard automatically."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from noyra.core.database import Database
from noyra.core.recovery_budget import parse_time, read_recovery, write_recovery
from noyra.core.types import content_hash, utc_now

from .project_control import ProjectReviewControl


class AutonomyRecovery:
    def __init__(
        self,
        database: Database,
        subject_id: str,
        *,
        clock: Callable[[], str] = utc_now,
        source_refresh_seconds: float = 900,
    ):
        self.database = database
        self.subject_id = subject_id
        self.clock = clock
        self.source_refresh_seconds = source_refresh_seconds
        self._last_check: str | None = None

    def refresh(self) -> dict[str, Any] | None:
        now = self.clock()
        if self._last_check is not None:
            elapsed = (parse_time(now) - parse_time(self._last_check)).total_seconds()
            if 0 <= elapsed < 300:
                return None
        with self.database.transaction() as connection:
            state = read_recovery(connection, self.subject_id)
            if state is not None and parse_time(now) < parse_time(state["checked_at"]):
                self._last_check = now
                return state
            if state is None or not state["active"]:
                incidents = connection.execute(
                    "SELECT e.execution_id,e.result_hash,p.project_id FROM autonomous_projects p "
                    "JOIN autonomous_project_phases ph ON ph.phase_id=p.current_phase_id "
                    "JOIN autonomous_project_executions e ON e.phase_id=ph.phase_id "
                    "AND e.project_id=p.project_id AND e.subject_id=p.subject_id "
                    "AND e.execution_key=p.project_id||':'||ph.phase_id||':'||ph.attempt_count "
                    "WHERE p.subject_id=? AND p.status='active' "
                    "AND e.status IN ('succeeded','failed','blocked','unknown') "
                    "AND (SELECT COUNT(*) FROM (SELECT 1 FROM "
                    "autonomous_project_execution_clock_events clk "
                    "WHERE clk.project_id=p.project_id "
                    "AND clk.execution_kind='phase_execution' AND clk.action='start' "
                    "AND clk.occurred_at>=e.completed_at LIMIT 3))>=3 "
                    "ORDER BY p.project_id,e.execution_id LIMIT 8",
                    (self.subject_id,),
                ).fetchall()
                signature = content_hash([dict(r) for r in incidents])
                if not incidents or (state is not None and state["incident"] == signature):
                    self._last_check = now
                    return state
                state = {
                    "version": 1,
                    "active": True,
                    "incident": signature,
                    "project_ids": [str(r["project_id"]) for r in incidents],
                    "started_at": now,
                    "checked_at": now,
                    "reservations": [],
                    "reason": "project_replay_recovery",
                    "stable_since": None,
                    "last_healthy_at": None,
                }
                write_recovery(connection, self.subject_id, state, now)
            else:
                instant = parse_time(now)
                if instant < parse_time(state["checked_at"]):
                    self._last_check = now
                    return state
                unresolved = False
                acknowledged_waits = {
                    "waiting_daily_quota",
                    "waiting_budget",
                    "awaiting_reconciliation",
                    "help_pending_route",
                    "help_waiting_reply",
                    "help_delivery_pending",
                    "help_delivery_unknown",
                    "review_retry_limit",
                }
                for project_id in state["project_ids"]:
                    row = connection.execute(
                        "SELECT p.status,p.current_revision,p.current_phase_id,"
                        "e.status AS result_status "
                        "FROM autonomous_projects p LEFT JOIN autonomous_project_phases ph "
                        "ON ph.phase_id=p.current_phase_id "
                        "LEFT JOIN autonomous_project_executions e "
                        "ON e.execution_key=p.project_id||':'||ph.phase_id||':'||ph.attempt_count "
                        "AND e.subject_id=p.subject_id WHERE p.subject_id=? AND p.project_id=?",
                        (self.subject_id, project_id),
                    ).fetchone()
                    if row is None:
                        unresolved = True
                    if (
                        row is not None
                        and row["status"] == "active"
                        and row["result_status"]
                        in {"executing", "succeeded", "failed", "blocked", "unknown"}
                    ):
                        control_key = "project-review-control:" + content_hash(
                            {
                                "subject": self.subject_id,
                                "project": project_id,
                                "revision": row["current_revision"],
                            }
                        )
                        checkpoint = connection.execute(
                            "SELECT * FROM workflow_checkpoints WHERE workflow_id=? "
                            "ORDER BY checkpoint_version DESC LIMIT 1",
                            (control_key,),
                        ).fetchone()
                        acknowledged = False
                        if checkpoint is not None:
                            control = ProjectReviewControl.decode(
                                checkpoint,
                                self.subject_id,
                                project_id,
                                int(row["current_revision"]),
                            )
                            acknowledged = control["reason"] in acknowledged_waits
                        unresolved = unresolved or not acknowledged
                polls = connection.execute(
                    "SELECT s.source_id,p.last_attempt_at FROM world_sources s "
                    "LEFT JOIN world_poll_state p ON p.source_id=s.source_id "
                    "AND p.subject_id=s.subject_id "
                    "WHERE s.subject_id=? AND s.status='active' LIMIT 65",
                    (self.subject_id,),
                ).fetchall()
                timely = len(polls) < 65 and all(
                    r["last_attempt_at"] is not None
                    and instant - timedelta(seconds=max(1800, 2 * self.source_refresh_seconds))
                    <= parse_time(r["last_attempt_at"])
                    <= instant
                    for r in polls
                )
                backlog = connection.execute(
                    "SELECT 1 FROM observations WHERE subject_id=? AND status='new' LIMIT 9",
                    (self.subject_id,),
                ).fetchall()
                failures = connection.execute(
                    "SELECT 1 FROM model_calls WHERE subject_id=? "
                    "AND status IN ('failed','unknown') "
                    "AND created_at>=? AND (error_code IS NULL OR error_code!='budget_exhausted') "
                    "LIMIT 4",
                    (self.subject_id, (instant - timedelta(minutes=30)).isoformat()),
                ).fetchall()
                healthy = not unresolved and timely and len(backlog) <= 8 and len(failures) < 4
                stable = state.get("stable_since") if healthy else None
                last_healthy = state.get("last_healthy_at")
                if last_healthy is None or instant - parse_time(last_healthy) > timedelta(
                    minutes=10
                ):
                    stable = None
                if healthy and stable is None:
                    stable = now
                reason = "project_replay_recovery"
                active = True
                if stable is not None and instant - parse_time(stable) >= timedelta(hours=1):
                    active, reason = False, "normal_operation_restored"
                if healthy or state.get("stable_since") != stable or state["active"] != active:
                    state.update(
                        stable_since=stable,
                        active=active,
                        reason=reason,
                        checked_at=now,
                        last_healthy_at=now if healthy else None,
                    )
                    write_recovery(connection, self.subject_id, state, now)
        self._last_check = now
        return state
