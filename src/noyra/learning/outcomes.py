from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.types import (
    content_hash,
    new_id,
    strict_finite_float,
    strict_int,
    strict_json_loads,
    utc_now,
)
from noyra.mind import GoalRecord, GoalStore

from ._integrity import durable_boundary
from .types import OutcomeEvaluationRecord, StrategyProfileRecord


@dataclass(frozen=True)
class PendingOutcome:
    source_type: str
    source_id: str
    source_status: str
    goal: GoalRecord
    strategy_id: str
    strategy_kind: str
    method: str
    observation_id: str | None
    result_count: int
    accepted_source_count: int
    evidence_event_id: str | None
    created_at: str


class OutcomeEvaluator:
    """Deterministically turns durable tool outcomes into bounded strategy and goal updates."""

    def __init__(
        self,
        database: Database,
        subject_id: str,
        *,
        clock: Callable[[], str] = utc_now,
        interval_seconds: float = 60,
        max_progress_delta: float = 0.05,
    ):
        self.database = database
        self.subject_id = subject_id
        self.clock = clock
        self.interval_seconds = interval_seconds
        self.max_progress_delta = max_progress_delta
        self.events = EventStore(database)
        self.goals = GoalStore(database)

    def run_due(self) -> str | None:
        pending = self._pending()
        if pending is None:
            return None
        if not self._is_due(pending.created_at):
            return None
        return self._evaluate(pending)

    def latest(self) -> OutcomeEvaluationRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM outcome_evaluations WHERE subject_id = ? "
                "ORDER BY created_at DESC, evaluation_id DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
        return self._evaluation_from_row(row) if row is not None else None

    def profiles(self, goal_id: str | None = None) -> list[StrategyProfileRecord]:
        with self.database.connection() as connection:
            if goal_id is None:
                rows = connection.execute(
                    "SELECT * FROM strategy_profiles WHERE subject_id = ? "
                    "ORDER BY confidence DESC, updated_at DESC",
                    (self.subject_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM strategy_profiles WHERE subject_id = ? AND goal_id = ? "
                    "ORDER BY confidence DESC, updated_at DESC",
                    (self.subject_id, goal_id),
                ).fetchall()
        return [self._profile_from_row(row) for row in rows]

    def verify_integrity(self) -> dict[str, int]:
        with self.database.connection() as connection:
            profiles = connection.execute(
                "SELECT * FROM strategy_profiles WHERE subject_id = ?", (self.subject_id,)
            ).fetchall()
            evaluations = connection.execute(
                "SELECT * FROM outcome_evaluations WHERE subject_id = ?", (self.subject_id,)
            ).fetchall()
            revisions = connection.execute(
                """SELECT r.*, p.subject_id FROM strategy_profile_revisions r
                   JOIN strategy_profiles p ON p.profile_id = r.profile_id
                   WHERE p.subject_id = ?""",
                (self.subject_id,),
            ).fetchall()
            for row in profiles:
                self._profile_from_row(row)
            for row in evaluations:
                record = self._evaluation_from_row(row)
                event = connection.execute(
                    "SELECT subject_id, event_type FROM events WHERE event_id = ?",
                    (record.evidence_event_id,),
                ).fetchone()
                if (
                    event is None
                    or event["subject_id"] != self.subject_id
                    or event["event_type"] != "outcome_evaluated"
                ):
                    raise IntegrityError("outcome evaluation evidence event is invalid")
            for row in revisions:
                revision_id = row["revision_id"]
                with durable_boundary("strategy profile revision", revision_id):
                    strict_int(row["revision_number"])
                    expected = self._revision_hash(
                        strict_int(row["attempts"]),
                        strict_int(row["successes"]),
                        strict_int(row["failures"]),
                        strict_int(row["inconclusive"]),
                        strict_finite_float(row["confidence"]),
                        row["last_outcome"],
                        row["evidence_type"],
                        row["evidence_id"],
                        row["reason"],
                    )
                if expected != row["state_hash"]:
                    raise IntegrityError("strategy profile revision hash mismatch")
        return {
            "strategy_profiles": len(profiles),
            "strategy_profile_revisions": len(revisions),
            "outcome_evaluations": len(evaluations),
        }

    def _pending(self) -> PendingOutcome | None:
        while True:
            action = self._pending_action()
            research = self._pending_research()
            candidates = [item for item in (action, research) if item is not None]
            if not candidates:
                return None
            pending = min(candidates, key=lambda item: (item.created_at, item.source_id))
            current = self.goals.get(pending.goal.goal_id)
            if current.origin != "human_proposal" and current.status not in {
                "achieved",
                "abandoned",
            }:
                return PendingOutcome(
                    pending.source_type,
                    pending.source_id,
                    pending.source_status,
                    current,
                    pending.strategy_id,
                    pending.strategy_kind,
                    pending.method,
                    pending.observation_id,
                    pending.result_count,
                    pending.accepted_source_count,
                    pending.evidence_event_id,
                    pending.created_at,
                )
            self._record_ignored(pending, "goal_not_eligible_for_autonomous_learning")

    def _pending_action(self) -> PendingOutcome | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT d.deliberation_id, d.goal_id, d.action_id, d.observation_id,
                          d.status AS source_status, d.strategy_title, d.completed_at,
                          a.strategy_id, a.tool, o.status AS observation_status
                   FROM action_deliberation_runs d
                   JOIN actions a ON a.action_id = d.action_id
                   LEFT JOIN observations o ON o.observation_id = d.observation_id
                   LEFT JOIN outcome_evaluations e
                     ON e.subject_id = d.subject_id AND e.source_type = 'action'
                    AND e.source_id = d.deliberation_id
                   WHERE d.subject_id = ? AND e.evaluation_id IS NULL
                     AND (
                         d.status IN ('unchanged', 'failed', 'outcome_unavailable')
                         OR (d.status = 'succeeded' AND o.status IN ('analyzed', 'rejected'))
                     )
                   ORDER BY d.completed_at, d.deliberation_id LIMIT 1""",
                (self.subject_id,),
            ).fetchone()
        if row is None:
            return None
        goal = self.goals.get(row["goal_id"])
        source_status = (
            "observation_rejected"
            if row["source_status"] == "succeeded" and row["observation_status"] == "rejected"
            else row["source_status"]
        )
        return PendingOutcome(
            "action",
            row["deliberation_id"],
            source_status,
            goal,
            row["strategy_id"],
            "action",
            row["tool"],
            row["observation_id"],
            1 if row["observation_id"] is not None else 0,
            0,
            self._observation_event(row["observation_id"]),
            row["completed_at"],
        )

    def _pending_research(self) -> PendingOutcome | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT r.* FROM research_search_runs r
                   LEFT JOIN outcome_evaluations e
                     ON e.subject_id = r.subject_id AND e.source_type = 'research'
                    AND e.source_id = r.research_id
                   WHERE r.subject_id = ? AND e.evaluation_id IS NULL
                     AND r.goal_id IS NOT NULL AND r.status IN ('accepted', 'no_results', 'failed')
                   ORDER BY r.completed_at, r.research_id LIMIT 1""",
                (self.subject_id,),
            ).fetchone()
        if row is None:
            return None
        goal = self.goals.get(row["goal_id"])
        strategy_id = content_hash(
            {
                "goal_id": row["goal_id"],
                "query_hash": row["query_hash"],
                "method": row["final_method"],
            }
        )[:32]
        accepted = self._string_list(row["accepted_source_ids_json"])
        return PendingOutcome(
            "research",
            row["research_id"],
            row["status"],
            goal,
            strategy_id,
            "research",
            row["final_method"],
            None,
            int(row["result_count"]),
            len(accepted),
            None,
            row["completed_at"],
        )

    def _evaluate(self, pending: PendingOutcome) -> str:
        pending = PendingOutcome(
            pending.source_type,
            pending.source_id,
            pending.source_status,
            self.goals.get(pending.goal.goal_id),
            pending.strategy_id,
            pending.strategy_kind,
            pending.method,
            pending.observation_id,
            pending.result_count,
            pending.accepted_source_count,
            pending.evidence_event_id,
            pending.created_at,
        )
        outcome, rationale, progress_delta, success_delta, failure_delta, inconclusive_delta = (
            self._classify(pending)
        )
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM outcome_evaluations WHERE subject_id = ? AND source_type = ? "
                "AND source_id = ?",
                (self.subject_id, pending.source_type, pending.source_id),
            ).fetchone()
            if existing is not None:
                return f"outcome_{existing['outcome']}"
            profile_row = connection.execute(
                "SELECT * FROM strategy_profiles WHERE subject_id = ? AND goal_id = ? "
                "AND strategy_id = ?",
                (self.subject_id, pending.goal.goal_id, pending.strategy_id),
            ).fetchone()
            confidence_before = 0.5 if profile_row is None else float(profile_row["confidence"])
            attempts_before = 0 if profile_row is None else int(profile_row["attempts"])
            successes_before = 0 if profile_row is None else int(profile_row["successes"])
            failures_before = 0 if profile_row is None else int(profile_row["failures"])
            inconclusive_before = 0 if profile_row is None else int(profile_row["inconclusive"])
            attempts = attempts_before + 1
            successes = successes_before + success_delta
            failures = failures_before + failure_delta
            inconclusive = inconclusive_before + inconclusive_delta
            confidence_after = self._confidence(successes, failures, inconclusive)
            progress_before = pending.goal.progress
            progress_after = min(0.99, progress_before + progress_delta)
            event = self.events._append_connection(
                connection,
                self.subject_id,
                "outcome_evaluated",
                "outcome_evaluator",
                {
                    "source_type": pending.source_type,
                    "source_id": pending.source_id,
                    "goal_id": pending.goal.goal_id,
                    "strategy_id": pending.strategy_id,
                    "outcome": outcome,
                    "rationale_code": rationale,
                    "result_count": pending.result_count,
                    "accepted_source_count": pending.accepted_source_count,
                    "progress_before": progress_before,
                    "progress_after": progress_after,
                    "confidence_before": confidence_before,
                    "confidence_after": confidence_after,
                },
                privacy_level="private",
                causal_parent_ids=(pending.evidence_event_id,)
                if pending.evidence_event_id is not None
                else (),
                occurred_at=self.clock(),
                event_id=None,
            )
            if progress_after > progress_before:
                self.goals._revise_connection(
                    connection,
                    pending.goal.goal_id,
                    status=pending.goal.status,
                    priority=pending.goal.priority,
                    commitment=pending.goal.commitment,
                    progress=progress_after,
                    emotional_pressure=pending.goal.emotional_pressure,
                    reason="verified external outcome advanced the goal",
                    causal_source_ids=tuple(
                        item
                        for item in (pending.evidence_event_id, event.event_id)
                        if item is not None
                    ),
                    expected_revision=pending.goal.current_revision,
                )
            profile_id = (
                new_id("strategy") if profile_row is None else str(profile_row["profile_id"])
            )
            revision = 1 if profile_row is None else int(profile_row["current_revision"]) + 1
            state_hash = self._profile_hash(
                pending.goal.goal_id,
                pending.strategy_id,
                pending.strategy_kind,
                pending.method,
                attempts,
                successes,
                failures,
                inconclusive,
                confidence_after,
                outcome,
            )
            now = self.clock()
            if profile_row is None:
                connection.execute(
                    """INSERT INTO strategy_profiles(
                        profile_id, subject_id, goal_id, strategy_id, strategy_kind, method,
                        attempts, successes, failures, inconclusive, confidence, last_outcome,
                        state_hash, current_revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        profile_id,
                        self.subject_id,
                        pending.goal.goal_id,
                        pending.strategy_id,
                        pending.strategy_kind,
                        pending.method,
                        attempts,
                        successes,
                        failures,
                        inconclusive,
                        confidence_after,
                        outcome,
                        state_hash,
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE strategy_profiles SET attempts = ?, successes = ?, failures = ?,
                        inconclusive = ?, confidence = ?, last_outcome = ?, state_hash = ?,
                        current_revision = ?, updated_at = ? WHERE profile_id = ?""",
                    (
                        attempts,
                        successes,
                        failures,
                        inconclusive,
                        confidence_after,
                        outcome,
                        state_hash,
                        revision,
                        now,
                        profile_id,
                    ),
                )
            revision_hash = self._revision_hash(
                attempts,
                successes,
                failures,
                inconclusive,
                confidence_after,
                outcome,
                pending.source_type,
                pending.source_id,
                rationale,
            )
            connection.execute(
                """INSERT INTO strategy_profile_revisions(
                    revision_id, profile_id, revision_number, attempts, successes, failures,
                    inconclusive, confidence, last_outcome, evidence_type, evidence_id, reason,
                    state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("strategyrev"),
                    profile_id,
                    revision,
                    attempts,
                    successes,
                    failures,
                    inconclusive,
                    confidence_after,
                    outcome,
                    pending.source_type,
                    pending.source_id,
                    rationale,
                    revision_hash,
                    now,
                ),
            )
            evaluation_id = new_id("outcome")
            public_summary = self._public_summary(pending.strategy_kind, outcome)
            evaluation_hash = self._evaluation_hash(
                pending,
                outcome,
                event.event_id,
                progress_before,
                progress_after,
                confidence_before,
                confidence_after,
                rationale,
                public_summary,
                now,
            )
            connection.execute(
                """INSERT INTO outcome_evaluations(
                    evaluation_id, subject_id, goal_id, strategy_id, strategy_kind,
                    source_type, source_id, source_status, outcome, evidence_event_id,
                    observation_id, result_count, accepted_source_count, progress_before,
                    progress_after, confidence_before, confidence_after, rationale_code,
                    public_summary, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evaluation_id,
                    self.subject_id,
                    pending.goal.goal_id,
                    pending.strategy_id,
                    pending.strategy_kind,
                    pending.source_type,
                    pending.source_id,
                    pending.source_status,
                    outcome,
                    event.event_id,
                    pending.observation_id,
                    pending.result_count,
                    pending.accepted_source_count,
                    progress_before,
                    progress_after,
                    confidence_before,
                    confidence_after,
                    rationale,
                    public_summary,
                    evaluation_hash,
                    now,
                ),
            )
        return f"outcome_{outcome}"

    def _record_ignored(self, pending: PendingOutcome, rationale: str) -> None:
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM outcome_evaluations WHERE subject_id = ? AND source_type = ? "
                "AND source_id = ?",
                (self.subject_id, pending.source_type, pending.source_id),
            ).fetchone()
            if existing is not None:
                return
            now = self.clock()
            event = self.events._append_connection(
                connection,
                self.subject_id,
                "outcome_evaluated",
                "outcome_evaluator",
                {
                    "source_type": pending.source_type,
                    "source_id": pending.source_id,
                    "goal_id": pending.goal.goal_id,
                    "strategy_id": pending.strategy_id,
                    "outcome": "unknown",
                    "rationale_code": rationale,
                },
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=now,
                event_id=None,
            )
            summary = "Outcome ignored because its goal is outside autonomous learning."
            evaluation_hash = self._evaluation_hash(
                pending,
                "unknown",
                event.event_id,
                pending.goal.progress,
                pending.goal.progress,
                0.5,
                0.5,
                rationale,
                summary,
                now,
            )
            connection.execute(
                """INSERT INTO outcome_evaluations(
                    evaluation_id, subject_id, goal_id, strategy_id, strategy_kind,
                    source_type, source_id, source_status, outcome, evidence_event_id,
                    observation_id, result_count, accepted_source_count, progress_before,
                    progress_after, confidence_before, confidence_after, rationale_code,
                    public_summary, state_hash, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?, ?, ?, ?, ?, 0.5, 0.5, ?, ?, ?, ?
                )""",
                (
                    new_id("outcome"),
                    self.subject_id,
                    pending.goal.goal_id,
                    pending.strategy_id,
                    pending.strategy_kind,
                    pending.source_type,
                    pending.source_id,
                    pending.source_status,
                    event.event_id,
                    pending.observation_id,
                    pending.result_count,
                    pending.accepted_source_count,
                    pending.goal.progress,
                    pending.goal.progress,
                    rationale,
                    summary,
                    evaluation_hash,
                    now,
                ),
            )

    def _classify(self, pending: PendingOutcome) -> tuple[str, str, float, int, int, int]:
        if pending.source_type == "action":
            if pending.source_status == "succeeded" and pending.observation_id is not None:
                return (
                    "progress",
                    "new_observation_recorded",
                    self.max_progress_delta,
                    1,
                    0,
                    0,
                )
            if pending.source_status == "unchanged":
                return "no_change", "observation_not_novel", 0, 0, 0, 1
            if pending.source_status == "failed":
                return "failure", "action_failed", 0, 0, 1, 0
            if pending.source_status == "observation_rejected":
                return "failure", "observation_rejected", 0, 0, 1, 0
            return "unknown", "action_outcome_unavailable", 0, 0, 0, 1
        if pending.source_status == "accepted" and pending.accepted_source_count > 0:
            return "informative", "candidate_sources_discovered", 0, 1, 0, 0
        if pending.source_status == "no_results":
            return "no_change", "research_returned_no_sources", 0, 0, 0, 1
        return "failure", "research_failed", 0, 0, 1, 0

    def _observation_event(self, observation_id: str | None) -> str | None:
        if observation_id is None:
            return None
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT event_id FROM observations WHERE observation_id = ? AND subject_id = ?",
                (observation_id, self.subject_id),
            ).fetchone()
        return None if row is None else str(row["event_id"])

    def _is_due(self, created_at: str) -> bool:
        elapsed = self._parse_time(self.clock()) - self._parse_time(created_at)
        return elapsed.total_seconds() >= self.interval_seconds

    @staticmethod
    def _confidence(successes: int, failures: int, inconclusive: int) -> float:
        value = (successes + 1) / (successes + failures + inconclusive * 0.5 + 2)
        return round(max(0.05, min(0.95, value)), 6)

    @staticmethod
    def _public_summary(strategy_kind: str, outcome: str) -> str:
        labels = {
            "progress": "Verified new evidence advanced the goal slightly.",
            "informative": "The strategy found candidate information for later verification.",
            "no_change": "The strategy produced no verified change.",
            "failure": "The strategy failed without advancing the goal.",
            "unknown": "The strategy outcome remains unknown.",
        }
        return f"{strategy_kind.capitalize()} evaluation: {labels[outcome]}"

    @staticmethod
    def _profile_hash(
        goal_id: str,
        strategy_id: str,
        strategy_kind: str,
        method: str,
        attempts: int,
        successes: int,
        failures: int,
        inconclusive: int,
        confidence: float,
        outcome: str,
    ) -> str:
        return content_hash(
            {
                "goal_id": goal_id,
                "strategy_id": strategy_id,
                "strategy_kind": strategy_kind,
                "method": method,
                "attempts": attempts,
                "successes": successes,
                "failures": failures,
                "inconclusive": inconclusive,
                "confidence": confidence,
                "last_outcome": outcome,
            }
        )

    @staticmethod
    def _revision_hash(
        attempts: int,
        successes: int,
        failures: int,
        inconclusive: int,
        confidence: float,
        outcome: str,
        evidence_type: str,
        evidence_id: str,
        reason: str,
    ) -> str:
        return content_hash(
            {
                "attempts": attempts,
                "successes": successes,
                "failures": failures,
                "inconclusive": inconclusive,
                "confidence": confidence,
                "last_outcome": outcome,
                "evidence_type": evidence_type,
                "evidence_id": evidence_id,
                "reason": reason,
            }
        )

    @classmethod
    def _evaluation_hash(
        cls,
        pending: PendingOutcome,
        outcome: str,
        evidence_event_id: str,
        progress_before: float,
        progress_after: float,
        confidence_before: float,
        confidence_after: float,
        rationale: str,
        public_summary: str,
        created_at: str,
    ) -> str:
        return content_hash(
            {
                "goal_id": pending.goal.goal_id,
                "strategy_id": pending.strategy_id,
                "strategy_kind": pending.strategy_kind,
                "source_type": pending.source_type,
                "source_id": pending.source_id,
                "source_status": pending.source_status,
                "outcome": outcome,
                "evidence_event_id": evidence_event_id,
                "observation_id": pending.observation_id,
                "result_count": pending.result_count,
                "accepted_source_count": pending.accepted_source_count,
                "progress_before": progress_before,
                "progress_after": progress_after,
                "confidence_before": confidence_before,
                "confidence_after": confidence_after,
                "rationale_code": rationale,
                "public_summary": public_summary,
                "created_at": created_at,
            }
        )

    @classmethod
    def _profile_from_row(cls, row: Any) -> StrategyProfileRecord:
        profile_id = row["profile_id"]
        with durable_boundary("strategy profile", profile_id):
            attempts = strict_int(row["attempts"])
            successes = strict_int(row["successes"])
            failures = strict_int(row["failures"])
            inconclusive = strict_int(row["inconclusive"])
            confidence = strict_finite_float(row["confidence"])
            current_revision = strict_int(row["current_revision"])
            expected = cls._profile_hash(
                row["goal_id"],
                row["strategy_id"],
                row["strategy_kind"],
                row["method"],
                attempts,
                successes,
                failures,
                inconclusive,
                confidence,
                row["last_outcome"],
            )
            if expected != row["state_hash"]:
                raise IntegrityError("strategy profile state hash mismatch")
            return StrategyProfileRecord(
                profile_id,
                row["subject_id"],
                row["goal_id"],
                row["strategy_id"],
                row["strategy_kind"],
                row["method"],
                attempts,
                successes,
                failures,
                inconclusive,
                confidence,
                row["last_outcome"],
                current_revision,
                row["created_at"],
                row["updated_at"],
            )

    @classmethod
    def _evaluation_from_row(cls, row: Any) -> OutcomeEvaluationRecord:
        evaluation_id = row["evaluation_id"]
        with durable_boundary("outcome evaluation", evaluation_id):
            result_count = strict_int(row["result_count"])
            accepted_source_count = strict_int(row["accepted_source_count"])
            progress_before = strict_finite_float(row["progress_before"])
            progress_after = strict_finite_float(row["progress_after"])
            confidence_before = strict_finite_float(row["confidence_before"])
            confidence_after = strict_finite_float(row["confidence_after"])
            pending = PendingOutcome(
                row["source_type"],
                row["source_id"],
                row["source_status"],
                GoalRecord(
                    row["goal_id"],
                    row["subject_id"],
                    "",
                    "",
                    "self",
                    "active",
                    0,
                    0,
                    progress_before,
                    0,
                    1,
                    row["created_at"],
                    row["created_at"],
                ),
                row["strategy_id"],
                row["strategy_kind"],
                "",
                row["observation_id"],
                result_count,
                accepted_source_count,
                row["evidence_event_id"],
                row["created_at"],
            )
            expected = cls._evaluation_hash(
                pending,
                row["outcome"],
                row["evidence_event_id"],
                progress_before,
                progress_after,
                confidence_before,
                confidence_after,
                row["rationale_code"],
                row["public_summary"],
                row["created_at"],
            )
            if expected != row["state_hash"]:
                raise IntegrityError("outcome evaluation state hash mismatch")
            return OutcomeEvaluationRecord(
                evaluation_id,
                row["subject_id"],
                row["goal_id"],
                row["strategy_id"],
                row["strategy_kind"],
                row["source_type"],
                row["source_id"],
                row["source_status"],
                row["outcome"],
                row["evidence_event_id"],
                row["observation_id"],
                result_count,
                accepted_source_count,
                progress_before,
                progress_after,
                confidence_before,
                confidence_after,
                row["rationale_code"],
                row["public_summary"],
                row["created_at"],
            )

    @staticmethod
    def _string_list(raw: str) -> list[str]:
        with durable_boundary("outcome source list", "source list"):
            if not isinstance(raw, str):
                raise IntegrityError("outcome source list JSON is invalid")
            value = strict_json_loads(raw)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise IntegrityError("outcome source list is invalid")
            return value

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("outcome evaluation time requires a timezone")
        return parsed.astimezone(UTC)
