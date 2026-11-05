"""Detect an old project replay loop and retire its temporary guard automatically."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from noyra.core.database import Database
from noyra.core.recovery_budget import (
    parse_time,
    read_recovery,
    recovery_high_water,
    write_recovery,
)
from noyra.core.types import content_hash, utc_now

from .project_control import ProjectReviewControl, project_dependencies_pending


@dataclass(frozen=True)
class _HealthSample:
    evidence_hash: str
    evaluated_at: str
    stable_since: str | None


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
        self._health_sample: _HealthSample | None = None

    def refresh(self) -> dict[str, Any] | None:
        # Only successfully completed evaluations can extend continuity. Clear
        # the cache before reading the clock or SQL so any failure cannot leave
        # an earlier healthy window eligible for a later exit.
        previous_sample = self._health_sample
        self._health_sample = None
        now = self.clock()
        if self._last_check is not None:
            elapsed = (parse_time(now) - parse_time(self._last_check)).total_seconds()
            if 0 <= elapsed < 300:
                self._health_sample = previous_sample
                return None
        next_sample = None
        with self.database.transaction() as connection:
            state = read_recovery(connection, self.subject_id)
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
                    "reservations": [] if state is None else state["reservations"],
                    "reason": "project_replay_recovery",
                    "stable_since": None,
                    "last_healthy_at": None,
                }
                write_recovery(connection, self.subject_id, state, now)
            else:
                instant = parse_time(now)
                sample = (
                    previous_sample
                    if previous_sample is not None
                    and previous_sample.evidence_hash == self._health_evidence_hash(state)
                    else None
                )
                watermark = recovery_high_water(state)
                if sample is not None:
                    watermark = max(watermark, parse_time(sample.evaluated_at))
                if instant < watermark:
                    if (
                        state.get("clock_status") != "wall_clock_behind_history"
                        or state.get("stable_since") is not None
                    ):
                        state.update(
                            clock_status="wall_clock_behind_history",
                            observed_at=now,
                            clock_high_water_at=watermark.isoformat(),
                            stable_since=None,
                            last_healthy_at=None,
                            last_health_evaluated_at=now,
                        )
                        state["exit_conditions"] = self._conditions(
                            state, {"wall_clock": False, "continuous_health": False}, now
                        )
                        write_recovery(connection, self.subject_id, state, now)
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
                    "help_public_summary_requires_review",
                    "subject_wait",
                    "review_backoff",
                }
                for project_id in state["project_ids"]:
                    row = connection.execute(
                        "SELECT p.*,ph.status AS phase_status,e.status AS result_status "
                        "FROM autonomous_projects p LEFT JOIN autonomous_project_phases ph "
                        "ON ph.phase_id=p.current_phase_id AND ph.project_id=p.project_id "
                        "AND ph.subject_id=p.subject_id "
                        "LEFT JOIN autonomous_project_executions e "
                        "ON e.execution_key=p.project_id||':'||ph.phase_id||':'||ph.attempt_count "
                        "AND e.subject_id=p.subject_id WHERE p.subject_id=? AND p.project_id=?",
                        (self.subject_id, project_id),
                    ).fetchone()
                    if row is None:
                        unresolved = True
                        continue
                    if row["status"] != "active":
                        continue
                    dependencies = project_dependencies_pending(connection, self.subject_id, row)
                    needs_coordination = (
                        dependencies
                        or row["phase_status"] != "active"
                        or row["result_status"]
                        in {"executing", "succeeded", "failed", "blocked", "unknown"}
                    )
                    if not needs_coordination:
                        continue
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
                    control = (
                        None
                        if checkpoint is None
                        else ProjectReviewControl.decode(
                            checkpoint, self.subject_id, project_id, int(row["current_revision"])
                        )
                    )
                    acknowledged = control is not None and control["reason"] in acknowledged_waits
                    if dependencies:
                        acknowledged = (
                            control is not None and control["reason"] == "waiting_dependencies"
                        )
                    unresolved = unresolved or not acknowledged
                # Aggregate all sources; an arbitrary sample size is never an exit condition.
                cutoff = (
                    instant - timedelta(seconds=max(1800, 2 * self.source_refresh_seconds))
                ).isoformat()
                stale = connection.execute(
                    "SELECT COUNT(*) FROM world_sources s LEFT JOIN world_poll_state p "
                    "ON p.source_id=s.source_id AND p.subject_id=s.subject_id "
                    "WHERE s.subject_id=? AND s.status='active' AND (p.last_attempt_at IS NULL "
                    "OR julianday(p.last_attempt_at) IS NULL "
                    "OR julianday(p.last_attempt_at)<julianday(?) "
                    "OR julianday(p.last_attempt_at)>julianday(?))",
                    (self.subject_id, cutoff, now),
                ).fetchone()[0]
                backlog = connection.execute(
                    "SELECT 1 FROM observations WHERE subject_id=? AND status='new' LIMIT 9",
                    (self.subject_id,),
                ).fetchall()
                failures = connection.execute(
                    "SELECT 1 FROM model_calls WHERE subject_id=? "
                    "AND status IN ('failed','unknown') "
                    "AND julianday(created_at)>=julianday(?) "
                    "AND (error_code IS NULL OR error_code NOT IN "
                    "('budget_exhausted','autonomy_recovery_budget_exhausted','recovery_clock_wait',"
                    "'model_budget_clock_wait','cost_only_pricing_unknown')) LIMIT 4",
                    (self.subject_id, (instant - timedelta(minutes=30)).isoformat()),
                ).fetchall()
                checks = {
                    "project_coordination": not unresolved,
                    "source_polling": not stale,
                    "observation_backlog": len(backlog) <= 8,
                    "recent_model_failures": len(failures) < 4,
                    "wall_clock": True,
                }
                healthy = all(checks.values())
                # A persisted heartbeat may be older than the most recent
                # successful sample. Use process-local sample evidence, bound
                # to the health checkpoint, rather than its write timestamp.
                # A new instance or changed health evidence starts a new window.
                stable = sample.stable_since if healthy and sample is not None else None
                if sample is None or instant - parse_time(sample.evaluated_at) > timedelta(
                    minutes=10
                ):
                    stable = None
                if healthy and stable is None:
                    stable = now
                checks["continuous_health"] = stable is not None and instant - parse_time(
                    stable
                ) >= timedelta(hours=1)
                active = not checks["continuous_health"]
                conditions = self._conditions(state, checks, now)
                prior_checks = {c["code"]: c["satisfied"] for c in state.get("exit_conditions", [])}
                last_evaluated = state.get("last_health_evaluated_at")
                heartbeat = last_evaluated is None or instant - parse_time(
                    last_evaluated
                ) >= timedelta(minutes=10)
                if (
                    prior_checks != checks
                    or state.get("stable_since") != stable
                    or state["active"] != active
                    or state.get("clock_status") == "wall_clock_behind_history"
                    or heartbeat
                ):
                    state.update(
                        stable_since=stable,
                        active=active,
                        reason="project_replay_recovery" if active else "normal_operation_restored",
                        checked_at=now,
                        observed_at=now,
                        clock_high_water_at=now,
                        clock_status="normal",
                        last_healthy_at=now if healthy else None,
                        last_health_evaluated_at=now,
                        exit_conditions=conditions,
                    )
                    write_recovery(connection, self.subject_id, state, now)
                next_sample = _HealthSample(self._health_evidence_hash(state), now, stable)
        # Publish in-memory evidence only after the transaction committed. Budget
        # reservations can append checkpoints between samples without counting
        # as health observations or discarding this process's real observations.
        self._health_sample = next_sample
        self._last_check = now
        return state

    @staticmethod
    def _health_evidence_hash(state: dict[str, Any]) -> str:
        # Budget admission changes checked_at, high-water and reservations, but
        # cannot manufacture continuity. Any change to the actual health/incident
        # evidence invalidates the cached sample; old or other-instance writes
        # are never silently overwritten with our unpersisted stable window.
        return content_hash(
            {
                field: state.get(field)
                for field in (
                    "incident",
                    "started_at",
                    "project_ids",
                    "active",
                    "stable_since",
                    "last_healthy_at",
                    "last_health_evaluated_at",
                    "clock_status",
                    "exit_conditions",
                )
            }
        )

    @staticmethod
    def _conditions(
        state: dict[str, Any], checks: dict[str, bool], now: str
    ) -> list[dict[str, Any]]:
        old = {c["code"]: c for c in state.get("exit_conditions", [])}
        # During a rollback, keep other conditions explicitly stale rather than
        # claiming a new successful evaluation of them.
        values = {key: value["satisfied"] for key, value in old.items()}
        values.update(checks)
        return [
            {
                "code": code,
                "satisfied": value,
                "observed_at": now if code in checks else old[code]["observed_at"],
                "unmet_since": None if value else old.get(code, {}).get("unmet_since") or now,
            }
            for code, value in sorted(values.items())
        ]
