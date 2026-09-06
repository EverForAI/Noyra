from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.payload_codec import decompress_text
from noyra.core.types import content_hash, new_id, utc_now

from ._integrity import durable_boundary, durable_float, durable_int
from .settings import CognitionSettings

CognitiveStrategy = Literal[
    "think",
    "research",
    "action",
    "epistemic_review",
    "goal_review",
    "social_review",
    "sleep",
    "wait",
]


@dataclass(frozen=True)
class MetacognitiveDecisionRecord:
    decision_id: str
    subject_id: str
    strategy: str
    target_type: str
    target_id: str | None
    reason_code: str
    score: float
    uncertainty: float
    fixation_risk: float
    resource_pressure: float
    created_at: str


@dataclass(frozen=True)
class CognitiveStrategyProfileRecord:
    profile_id: str
    subject_id: str
    strategy: str
    attempts: int
    productive: int
    stagnant: int
    failed: int
    confidence: float
    last_outcome: str
    current_revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class _PendingOutcome:
    decision_id: str
    strategy: str
    source_type: str
    source_id: str
    outcome: str
    token_cost: int
    rationale_code: str
    created_at: str


@dataclass(frozen=True)
class _Candidate:
    strategy: CognitiveStrategy
    target_type: str
    target_id: str | None
    reason_code: str
    base_score: float
    uncertainty: float
    fixation_risk: float
    eligible: bool = True


class MetacognitiveControl:
    """Select the next bounded cognitive strategy and learn from durable outcomes."""

    def __init__(
        self,
        database: Database,
        subject_id: str,
        settings: CognitionSettings,
        *,
        clock: Callable[[], str] = utc_now,
    ):
        self.database = database
        self.subject_id = subject_id
        self.settings = settings
        self.clock = clock
        self.events = EventStore(database)

    def run_due(self) -> MetacognitiveDecisionRecord:
        pending = self._pending_decision()
        if pending is not None and not self._quarantine_if_stale(pending):
            return pending
        candidates, resource_pressure = self._candidates()
        selected = max(
            (candidate for candidate in candidates if candidate.eligible),
            key=lambda candidate: (
                self._score(candidate, resource_pressure),
                candidate.strategy,
                candidate.target_id or "",
            ),
        )
        score = self._score(selected, resource_pressure)
        return self._commit_decision(selected, score, resource_pressure)

    def _quarantine_if_stale(self, decision: MetacognitiveDecisionRecord) -> bool:
        age = (
            self._parse_time(self.clock()) - self._parse_time(decision.created_at)
        ).total_seconds()
        if age < self.settings.metacognitive_pending_timeout_seconds:
            return False
        self._commit_outcome(
            _PendingOutcome(
                decision_id=decision.decision_id,
                strategy=decision.strategy,
                source_type="metacognitive_decision",
                source_id=decision.decision_id,
                outcome="unknown",
                token_cost=0,
                rationale_code="workflow_timeout_quarantined",
                created_at=self.clock(),
            )
        )
        return True

    def record_result(self, result: str) -> str:
        pending = self._pending_decision()
        if pending is None:
            return result
        source_type, source_id, outcome, rationale, token_cost = self._classify(pending, result)
        self._commit_outcome(
            _PendingOutcome(
                pending.decision_id,
                pending.strategy,
                source_type,
                source_id,
                outcome,
                token_cost,
                rationale,
                self.clock(),
            )
        )
        return result

    def latest(self) -> MetacognitiveDecisionRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM metacognitive_decisions WHERE subject_id = ? "
                "ORDER BY created_at DESC, decision_id DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
        return None if row is None else self._decision_from_row(row)

    def profiles(self) -> list[CognitiveStrategyProfileRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM cognitive_strategy_profiles WHERE subject_id = ? "
                "ORDER BY confidence DESC, strategy",
                (self.subject_id,),
            ).fetchall()
        return [self._profile_from_row(row) for row in rows]

    def verify_integrity(self) -> dict[str, int]:
        with self.database.read_transaction() as connection:
            decisions = connection.execute(
                "SELECT * FROM metacognitive_decisions WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchall()
            outcomes = connection.execute(
                "SELECT * FROM metacognitive_outcomes WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchall()
            profiles = connection.execute(
                "SELECT * FROM cognitive_strategy_profiles WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchall()
            from .epistemic import EpistemicReview

            epistemic_review_count = EpistemicReview._verify_connection(connection, self.subject_id)
            for row in decisions:
                self._decision_from_row(row)
            for row in outcomes:
                outcome_id = row["outcome_id"]
                with durable_boundary("metacognitive outcome", outcome_id):
                    expected = self._outcome_hash(
                        row["decision_id"],
                        row["strategy"],
                        row["source_type"],
                        row["source_id"],
                        row["outcome"],
                        durable_int(row["token_cost"], "metacognitive outcome", outcome_id),
                        row["rationale_code"],
                        row["created_at"],
                    )
                if expected != row["state_hash"]:
                    raise IntegrityError(f"metacognitive outcome mismatch: {row['outcome_id']}")
                decision = connection.execute(
                    "SELECT subject_id, strategy FROM metacognitive_decisions "
                    "WHERE decision_id = ?",
                    (row["decision_id"],),
                ).fetchone()
                if (
                    decision is None
                    or decision["subject_id"] != self.subject_id
                    or decision["strategy"] != row["strategy"]
                ):
                    raise IntegrityError(
                        f"metacognitive outcome ownership mismatch: {row['outcome_id']}"
                    )
            for row in profiles:
                profile = self._profile_from_row(row)
                revisions = connection.execute(
                    "SELECT * FROM cognitive_strategy_profile_revisions "
                    "WHERE profile_id = ? ORDER BY revision_number",
                    (profile.profile_id,),
                ).fetchall()
                if len(revisions) != profile.current_revision:
                    raise IntegrityError(
                        f"cognitive strategy revision mismatch: {profile.profile_id}"
                    )
                for revision in revisions:
                    revision_id = revision["revision_id"]
                    with durable_boundary("cognitive strategy revision", revision_id):
                        durable_int(
                            revision["revision_number"],
                            "cognitive strategy revision",
                            revision_id,
                        )
                        expected = self._profile_revision_hash(
                            durable_int(
                                revision["attempts"],
                                "cognitive strategy revision",
                                revision_id,
                            ),
                            durable_int(
                                revision["productive"],
                                "cognitive strategy revision",
                                revision_id,
                            ),
                            durable_int(
                                revision["stagnant"],
                                "cognitive strategy revision",
                                revision_id,
                            ),
                            durable_int(
                                revision["failed"],
                                "cognitive strategy revision",
                                revision_id,
                            ),
                            durable_float(
                                revision["confidence"],
                                "cognitive strategy revision",
                                revision_id,
                            ),
                            revision["last_outcome"],
                            revision["source_type"],
                            revision["source_id"],
                            revision["reason"],
                        )
                    if expected != revision["state_hash"]:
                        raise IntegrityError(
                            f"cognitive strategy revision hash mismatch: {profile.profile_id}"
                        )
            return {
                "metacognitive_decisions": len(decisions),
                "metacognitive_outcomes": len(outcomes),
                "cognitive_strategy_profiles": len(profiles),
                "epistemic_review_runs": epistemic_review_count,
            }

    def _candidates(self) -> tuple[list[_Candidate], float]:
        now = self._parse_time(self.clock())
        with self.database.connection() as connection:
            fatigue = connection.execute(
                "SELECT fatigue, resource_pressure, cognitive_load, frustration, "
                "goal_conflict, staleness FROM fatigue_states WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchone()
            pending_interaction = connection.execute(
                "SELECT interaction_id FROM interactions WHERE subject_id = ? "
                "AND direction = 'incoming' AND status IN ('offered', 'deferred') "
                "ORDER BY created_at LIMIT 1",
                (self.subject_id,),
            ).fetchone()
            pending_outcome = connection.execute(
                """SELECT 1 FROM (
                    SELECT d.action_id AS source_id FROM action_deliberation_runs d
                    LEFT JOIN outcome_evaluations o
                      ON o.subject_id = d.subject_id AND o.source_type = 'action'
                     AND o.source_id = d.action_id
                    WHERE d.subject_id = ? AND d.action_id IS NOT NULL AND o.evaluation_id IS NULL
                    UNION ALL
                    SELECT r.research_id FROM research_search_runs r
                    LEFT JOIN outcome_evaluations o
                      ON o.subject_id = r.subject_id AND o.source_type = 'research'
                     AND o.source_id = r.research_id
                    WHERE r.subject_id = ? AND o.evaluation_id IS NULL
                ) LIMIT 1""",
                (self.subject_id, self.subject_id),
            ).fetchone()
            authorized_source = connection.execute(
                """SELECT s.source_id FROM world_sources s
                   JOIN capability_grants g ON g.subject_id = s.subject_id
                   WHERE s.subject_id = ? AND s.status = 'active'
                     AND g.capability_type = 'web_read' AND g.status = 'active'
                     AND g.requires_approval = 0
                    LIMIT 1""",
                (self.subject_id,),
            ).fetchone()
            active_goal = connection.execute(
                "SELECT goal_id, priority, commitment, progress, emotional_pressure "
                "FROM goals WHERE subject_id = ? AND origin != 'human_proposal' "
                "AND status = 'active' ORDER BY priority DESC, commitment DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
            review_goal = connection.execute(
                "SELECT goal_id, priority, commitment, emotional_pressure FROM goals "
                "WHERE subject_id = ? AND origin != 'human_proposal' "
                "AND status IN ('candidate', 'reconsidering', 'paused') "
                "ORDER BY priority DESC, commitment DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
            agenda = connection.execute(
                "SELECT agenda_id, urgency, novelty, emotional_weight, recurrence_count, "
                "consecutive_no_change, status, cooldown_until FROM thought_agenda_items "
                "WHERE subject_id = ? AND status IN ('open', 'cooling') "
                "ORDER BY updated_at DESC LIMIT 100",
                (self.subject_id,),
            ).fetchall()
            trigger_observation = connection.execute(
                """SELECT o.observation_id FROM observations o
                   LEFT JOIN epistemic_review_runs r
                     ON r.subject_id = o.subject_id AND r.trigger_observation_id = o.observation_id
                    AND r.status IN ('committed', 'no_change')
                   WHERE o.subject_id = ? AND o.status = 'analyzed' AND r.review_id IS NULL
                   ORDER BY o.fetched_at, o.observation_id LIMIT 1""",
                (self.subject_id,),
            ).fetchone()
            relationship = connection.execute(
                "SELECT relationship_id, trust, affinity, conflict, familiarity "
                "FROM relationships WHERE subject_id = ? AND entity_type = 'human' "
                "ORDER BY MAX(affinity, conflict) DESC, familiarity DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
            recent = connection.execute(
                "SELECT strategy FROM metacognitive_decisions WHERE subject_id = ? "
                "ORDER BY created_at DESC, decision_id DESC LIMIT 4",
                (self.subject_id,),
            ).fetchall()
            last_times = {
                "think": self._latest_time(connection, "thought_episodes", "created_at"),
                "research": self._latest_time(connection, "research_search_runs", "created_at"),
                "action": self._latest_time(connection, "action_deliberation_runs", "created_at"),
                "epistemic_review": self._latest_time(
                    connection, "epistemic_review_runs", "created_at"
                ),
                "goal_review": self._latest_time(connection, "goal_governance_runs", "created_at"),
                "social_review": self._latest_time(
                    connection, "relationship_social_runs", "created_at"
                ),
            }
        fatigue_score = 0.0 if fatigue is None else float(fatigue["fatigue"]) / 100
        resource_pressure = 0.0 if fatigue is None else float(fatigue["resource_pressure"])
        repeat_streak = 0
        latest_strategy = None if not recent else str(recent[0]["strategy"])
        for row in recent:
            if str(row["strategy"]) != latest_strategy:
                break
            repeat_streak += 1
        candidates: list[_Candidate] = []
        available_agenda = [
            row
            for row in agenda
            if row["status"] == "open"
            or (
                row["cooldown_until"] is not None and self._parse_time(row["cooldown_until"]) <= now
            )
        ]
        if available_agenda:
            selected_agenda = max(
                available_agenda,
                key=lambda row: (
                    float(row["urgency"]) * 0.4
                    + float(row["novelty"]) * 0.25
                    + float(row["emotional_weight"]) * 0.25
                    - min(0.45, int(row["consecutive_no_change"]) * 0.15),
                    row["agenda_id"],
                ),
            )
            candidates.append(
                _Candidate(
                    "think",
                    "agenda",
                    str(selected_agenda["agenda_id"]),
                    "internal_question_has_attention_value",
                    min(
                        1.0,
                        float(selected_agenda["urgency"]) * 0.4
                        + float(selected_agenda["novelty"]) * 0.25
                        + float(selected_agenda["emotional_weight"]) * 0.25
                        + 0.1,
                    ),
                    max(0.0, 1 - float(selected_agenda["novelty"])),
                    min(
                        1.0,
                        int(selected_agenda["consecutive_no_change"])
                        / max(1, self.settings.max_thought_no_change_streak),
                    ),
                    self._interval_due(last_times["think"], self.settings.thought_interval_seconds),
                )
            )
        if active_goal is not None:
            uncertainty = self._goal_uncertainty()
            progress_gap = 1 - float(active_goal["progress"])
            candidates.append(
                _Candidate(
                    "research",
                    "goal",
                    str(active_goal["goal_id"]),
                    "active_goal_needs_new_evidence",
                    min(
                        1.0,
                        0.35
                        + float(active_goal["priority"]) * 0.25
                        + progress_gap * 0.2
                        + uncertainty * 0.2,
                    ),
                    uncertainty,
                    self._recent_failure_risk("research"),
                    authorized_source is not None
                    and self._interval_due(
                        last_times["research"], self.settings.research_interval_seconds
                    ),
                )
            )
            candidates.append(
                _Candidate(
                    "action",
                    "goal",
                    str(active_goal["goal_id"]),
                    "active_goal_has_authorized_evidence_action",
                    min(
                        1.0,
                        0.3
                        + float(active_goal["priority"]) * 0.25
                        + float(active_goal["commitment"]) * 0.2
                        + progress_gap * 0.15,
                    ),
                    uncertainty,
                    self._recent_failure_risk("action"),
                    authorized_source is not None
                    and self._interval_due(
                        last_times["action"], self.settings.action_deliberation_interval_seconds
                    ),
                )
            )
        if trigger_observation is not None:
            candidates.append(
                _Candidate(
                    "epistemic_review",
                    "observation",
                    str(trigger_observation["observation_id"]),
                    "analyzed_evidence_awaits_belief_review",
                    0.78,
                    0.7,
                    self._recent_failure_risk("epistemic_review"),
                )
            )
        if review_goal is not None:
            candidates.append(
                _Candidate(
                    "goal_review",
                    "goal",
                    str(review_goal["goal_id"]),
                    "goal_state_requires_deliberate_review",
                    min(
                        1.0,
                        0.5
                        + float(review_goal["priority"]) * 0.2
                        + float(review_goal["commitment"]) * 0.15
                        + abs(float(review_goal["emotional_pressure"])) * 0.15,
                    ),
                    0.55,
                    self._recent_failure_risk("goal_review"),
                    self._interval_due(
                        last_times["goal_review"],
                        self.settings.goal_governance_interval_seconds,
                    ),
                )
            )
        if relationship is not None:
            relational_pressure = max(
                abs(float(relationship["affinity"])), float(relationship["conflict"])
            )
            candidates.append(
                _Candidate(
                    "social_review",
                    "relationship",
                    str(relationship["relationship_id"]),
                    "relationship_merits_autonomous_review",
                    min(
                        1.0,
                        0.25
                        + relational_pressure * 0.45
                        + float(relationship["familiarity"]) * 0.15,
                    ),
                    max(0.0, 1 - float(relationship["familiarity"])),
                    max(
                        float(relationship["conflict"]), self._recent_failure_risk("social_review")
                    ),
                    self._interval_due(
                        last_times["social_review"], self.settings.social_review_interval_seconds
                    ),
                )
            )
        sleep_pressure = max(
            fatigue_score,
            0.0 if fatigue is None else float(fatigue["frustration"]),
            0.0 if fatigue is None else float(fatigue["goal_conflict"]),
            0.0 if fatigue is None else float(fatigue["staleness"]),
        )
        if sleep_pressure >= self.settings.metacognitive_sleep_threshold:
            candidates.append(
                _Candidate(
                    "sleep",
                    "subject",
                    self.subject_id,
                    "cognitive_pressure_requires_integration",
                    min(1.0, 0.45 + sleep_pressure * 0.55),
                    0.2,
                    0.0,
                )
            )
            return [candidates[-1]], resource_pressure
        if pending_interaction is not None:
            candidates.append(
                _Candidate(
                    "wait",
                    "none",
                    None,
                    "human_invitation_is_handled_by_equal_interaction",
                    0.35,
                    0.1,
                    0.0,
                )
            )
        if pending_outcome is not None:
            candidates.append(
                _Candidate(
                    "wait",
                    "none",
                    None,
                    "durable_outcome_must_be_evaluated_first",
                    0.99,
                    0.1,
                    0.0,
                )
            )
        candidates.append(
            _Candidate(
                "wait",
                "none",
                None,
                "no_cognitive_strategy_is_currently_due",
                0.2 + resource_pressure * 0.5,
                0.1,
                0.0,
            )
        )
        if latest_strategy is not None and repeat_streak > 1:
            candidates = [
                _Candidate(
                    item.strategy,
                    item.target_type,
                    item.target_id,
                    item.reason_code,
                    item.base_score,
                    item.uncertainty,
                    max(
                        item.fixation_risk,
                        min(
                            1.0, (repeat_streak - 1) * self.settings.metacognitive_fixation_penalty
                        ),
                    )
                    if item.strategy == latest_strategy
                    else item.fixation_risk,
                    item.eligible,
                )
                for item in candidates
            ]
        return candidates, resource_pressure

    def _score(self, candidate: _Candidate, resource_pressure: float) -> float:
        profile = self._profile(candidate.strategy)
        learned = 0.5 if profile is None else profile.confidence
        cost = {
            "think": 0.65,
            "research": 0.85,
            "action": 0.9,
            "epistemic_review": 0.7,
            "goal_review": 0.55,
            "social_review": 0.6,
            "sleep": 0.1,
            "wait": 0.0,
        }[candidate.strategy]
        value = (
            candidate.base_score * 0.62
            + learned * 0.23
            - candidate.fixation_risk * 0.3
            - resource_pressure * cost * 0.25
        )
        return round(max(0.0, min(1.0, value)), 6)

    def _classify(
        self, decision: MetacognitiveDecisionRecord, result: str
    ) -> tuple[str, str, str, str, int]:
        with self.database.connection() as connection:
            if decision.strategy == "think":
                row = connection.execute(
                    "SELECT thought_id, disposition, changed_state, created_at, model_call_id "
                    "FROM thought_episodes WHERE subject_id = ? AND created_at >= ? "
                    "ORDER BY created_at DESC, thought_id DESC LIMIT 1",
                    (self.subject_id, decision.created_at),
                ).fetchone()
                if row is not None:
                    changed = bool(row["changed_state"])
                    terminal = row["disposition"] in {"resolve", "abandon"}
                    outcome = "productive" if changed or terminal else "stagnant"
                    rationale = (
                        "thought_changed_state" if changed or terminal else "thought_repeated"
                    )
                    return (
                        "thought_episode",
                        str(row["thought_id"]),
                        outcome,
                        rationale,
                        self._call_tokens(connection, row["model_call_id"]),
                    )
            elif decision.strategy == "research":
                row = connection.execute(
                    "SELECT research_id, status, planner_call_id FROM research_search_runs "
                    "WHERE subject_id = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                    (self.subject_id, decision.created_at),
                ).fetchone()
                if row is not None:
                    outcome = (
                        "productive"
                        if row["status"] == "accepted"
                        else "stagnant"
                        if row["status"] in {"no_results", "waited"}
                        else "failed"
                    )
                    return (
                        "research_run",
                        str(row["research_id"]),
                        outcome,
                        f"research_{row['status']}",
                        self._call_tokens(connection, row["planner_call_id"]),
                    )
            elif decision.strategy == "action":
                row = connection.execute(
                    "SELECT deliberation_id, status, model_call_id FROM action_deliberation_runs "
                    "WHERE subject_id = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                    (self.subject_id, decision.created_at),
                ).fetchone()
                if row is not None:
                    outcome = (
                        "productive"
                        if row["status"] == "succeeded"
                        else "stagnant"
                        if row["status"] in {"unchanged", "waited", "outcome_unavailable"}
                        else "failed"
                    )
                    return (
                        "action_deliberation",
                        str(row["deliberation_id"]),
                        outcome,
                        f"action_{row['status']}",
                        self._call_tokens(connection, row["model_call_id"]),
                    )
            elif decision.strategy == "epistemic_review":
                row = connection.execute(
                    "SELECT review_id, status, model_call_id FROM epistemic_review_runs "
                    "WHERE subject_id = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                    (self.subject_id, decision.created_at),
                ).fetchone()
                if row is not None:
                    outcome = (
                        "productive"
                        if row["status"] == "committed"
                        else "stagnant"
                        if row["status"] == "no_change"
                        else "failed"
                    )
                    return (
                        "epistemic_review",
                        str(row["review_id"]),
                        outcome,
                        f"epistemic_{row['status']}",
                        self._call_tokens(connection, row["model_call_id"]),
                    )
            elif decision.strategy == "goal_review":
                row = connection.execute(
                    "SELECT governance_id, model_call_id FROM goal_governance_runs "
                    "WHERE subject_id = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                    (self.subject_id, decision.created_at),
                ).fetchone()
                if row is not None:
                    return (
                        "goal_governance",
                        str(row["governance_id"]),
                        "productive",
                        "goal_focus_reconsidered",
                        self._call_tokens(connection, row["model_call_id"]),
                    )
            elif decision.strategy == "social_review":
                row = connection.execute(
                    "SELECT social_id, disposition, model_call_id FROM relationship_social_runs "
                    "WHERE subject_id = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                    (self.subject_id, decision.created_at),
                ).fetchone()
                if row is not None:
                    outcome = (
                        "productive"
                        if row["disposition"] in {"contact", "request_help"}
                        else "stagnant"
                    )
                    return (
                        "relationship_social",
                        str(row["social_id"]),
                        outcome,
                        f"social_{row['disposition']}",
                        self._call_tokens(connection, row["model_call_id"]),
                    )
        if decision.strategy == "sleep":
            outcome = "productive" if result == "metacognitive_sleep_requested" else "failed"
        elif decision.strategy == "wait":
            outcome = "stagnant"
        elif any(token in result for token in ("failed", "rejected", "budget_exhausted")):
            outcome = "failed"
        elif result.endswith("_waiting") or result.endswith("_waited"):
            outcome = "stagnant"
        else:
            outcome = "unknown"
        return (
            "cycle_result",
            content_hash({"decision": decision.decision_id, "result": result}),
            outcome,
            result,
            0,
        )

    def _commit_decision(
        self, candidate: _Candidate, score: float, resource_pressure: float
    ) -> MetacognitiveDecisionRecord:
        now = self.clock()
        decision_id = new_id("meta")
        state_hash = self._decision_hash(
            candidate.strategy,
            candidate.target_type,
            candidate.target_id,
            candidate.reason_code,
            score,
            candidate.uncertainty,
            candidate.fixation_risk,
            resource_pressure,
            now,
        )
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO metacognitive_decisions(
                    decision_id, subject_id, strategy, target_type, target_id, reason_code,
                    score, uncertainty, fixation_risk, resource_pressure, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    self.subject_id,
                    candidate.strategy,
                    candidate.target_type,
                    candidate.target_id,
                    candidate.reason_code,
                    score,
                    candidate.uncertainty,
                    candidate.fixation_risk,
                    resource_pressure,
                    state_hash,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM metacognitive_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            raise IntegrityError("metacognitive decision is missing")
        return self._decision_from_row(row)

    def _commit_outcome(self, pending: _PendingOutcome) -> None:
        with self.database.transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM metacognitive_outcomes WHERE decision_id = ?",
                    (pending.decision_id,),
                ).fetchone()
                is not None
            ):
                return
            profile_row = connection.execute(
                "SELECT * FROM cognitive_strategy_profiles WHERE subject_id = ? AND strategy = ?",
                (self.subject_id, pending.strategy),
            ).fetchone()
            attempts = 1 if profile_row is None else int(profile_row["attempts"]) + 1
            productive = (0 if profile_row is None else int(profile_row["productive"])) + int(
                pending.outcome == "productive"
            )
            stagnant = (0 if profile_row is None else int(profile_row["stagnant"])) + int(
                pending.outcome == "stagnant"
            )
            failed = (0 if profile_row is None else int(profile_row["failed"])) + int(
                pending.outcome == "failed"
            )
            confidence = self._confidence(productive, stagnant, failed)
            profile_id = new_id("cstrategy") if profile_row is None else profile_row["profile_id"]
            revision = 1 if profile_row is None else int(profile_row["current_revision"]) + 1
            profile_hash = self._profile_hash(
                pending.strategy,
                attempts,
                productive,
                stagnant,
                failed,
                confidence,
                pending.outcome,
            )
            if profile_row is None:
                connection.execute(
                    """INSERT INTO cognitive_strategy_profiles(
                        profile_id, subject_id, strategy, attempts, productive, stagnant,
                        failed, confidence, last_outcome, state_hash, current_revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        profile_id,
                        self.subject_id,
                        pending.strategy,
                        attempts,
                        productive,
                        stagnant,
                        failed,
                        confidence,
                        pending.outcome,
                        profile_hash,
                        pending.created_at,
                        pending.created_at,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE cognitive_strategy_profiles
                       SET attempts = ?, productive = ?, stagnant = ?, failed = ?,
                           confidence = ?, last_outcome = ?, state_hash = ?,
                           current_revision = ?, updated_at = ?
                       WHERE profile_id = ?""",
                    (
                        attempts,
                        productive,
                        stagnant,
                        failed,
                        confidence,
                        pending.outcome,
                        profile_hash,
                        revision,
                        pending.created_at,
                        profile_id,
                    ),
                )
            revision_hash = self._profile_revision_hash(
                attempts,
                productive,
                stagnant,
                failed,
                confidence,
                pending.outcome,
                pending.source_type,
                pending.source_id,
                pending.rationale_code,
            )
            connection.execute(
                """INSERT INTO cognitive_strategy_profile_revisions(
                    revision_id, profile_id, revision_number, attempts, productive, stagnant,
                    failed, confidence, last_outcome, source_type, source_id, reason,
                    state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("cstrategyrev"),
                    profile_id,
                    revision,
                    attempts,
                    productive,
                    stagnant,
                    failed,
                    confidence,
                    pending.outcome,
                    pending.source_type,
                    pending.source_id,
                    pending.rationale_code,
                    revision_hash,
                    pending.created_at,
                ),
            )
            outcome_hash = self._outcome_hash(
                pending.decision_id,
                pending.strategy,
                pending.source_type,
                pending.source_id,
                pending.outcome,
                pending.token_cost,
                pending.rationale_code,
                pending.created_at,
            )
            outcome_id = new_id("metaout")
            connection.execute(
                """INSERT INTO metacognitive_outcomes(
                    outcome_id, subject_id, decision_id, strategy, source_type, source_id,
                    outcome, token_cost, rationale_code, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    outcome_id,
                    self.subject_id,
                    pending.decision_id,
                    pending.strategy,
                    pending.source_type,
                    pending.source_id,
                    pending.outcome,
                    pending.token_cost,
                    pending.rationale_code,
                    outcome_hash,
                    pending.created_at,
                ),
            )
            self.events._append_connection(
                connection,
                self.subject_id,
                "metacognitive_outcome",
                "metacognitive_control",
                {
                    "outcome_id": outcome_id,
                    "decision_id": pending.decision_id,
                    "strategy": pending.strategy,
                    "outcome": pending.outcome,
                    "rationale_code": pending.rationale_code,
                },
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=pending.created_at,
                event_id=None,
            )

    def _pending_decision(self) -> MetacognitiveDecisionRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT d.* FROM metacognitive_decisions d
                   LEFT JOIN metacognitive_outcomes o ON o.decision_id = d.decision_id
                   WHERE d.subject_id = ? AND o.outcome_id IS NULL
                   ORDER BY d.created_at, d.decision_id LIMIT 1""",
                (self.subject_id,),
            ).fetchone()
        return None if row is None else self._decision_from_row(row)

    def _profile(self, strategy: str) -> CognitiveStrategyProfileRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM cognitive_strategy_profiles WHERE subject_id = ? AND strategy = ?",
                (self.subject_id, strategy),
            ).fetchone()
        return None if row is None else self._profile_from_row(row)

    def _recent_failure_risk(self, strategy: str) -> float:
        profile = self._profile(strategy)
        if profile is None or profile.attempts == 0:
            return 0.0
        return min(1.0, (profile.failed + profile.stagnant * 0.5) / profile.attempts)

    def _goal_uncertainty(self) -> float:
        import json

        with self.database.connection() as connection:
            belief = connection.execute(
                "SELECT AVG(confidence) FROM beliefs WHERE subject_id = ? "
                "AND status != 'retracted'",
                (self.subject_id,),
            ).fetchone()[0]
            self_model = connection.execute(
                "SELECT uncertainties_json FROM self_models WHERE subject_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
        belief_uncertainty = 0.5 if belief is None else 1 - float(belief)
        if self_model is None:
            model_uncertainty = 0.5
        else:
            uncertainties = json.loads(self_model["uncertainties_json"])
            if not isinstance(uncertainties, list):
                raise IntegrityError("self-model uncertainty list is invalid")
            model_uncertainty = min(1.0, len(uncertainties) / 6)
        return min(1.0, belief_uncertainty * 0.6 + model_uncertainty * 0.4)

    def _interval_due(self, latest: str | None, interval: float) -> bool:
        if latest is None:
            return True
        return (
            self._parse_time(self.clock()) - self._parse_time(latest)
        ).total_seconds() >= interval

    def _latest_time(self, connection: Any, table: str, column: str) -> str | None:
        row = connection.execute(
            f'SELECT MAX("{column}") FROM "{table}" WHERE subject_id = ?',
            (self.subject_id,),
        ).fetchone()
        return None if row is None else row[0]

    @staticmethod
    def _call_tokens(connection: Any, call_id: str) -> int:
        row = connection.execute(
            "SELECT response_json FROM model_calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        if row is None or row["response_json"] is None:
            return 0
        import json

        response = json.loads(decompress_text(row["response_json"]) or "null")
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))

    @staticmethod
    def _confidence(productive: int, stagnant: int, failed: int) -> float:
        value = (productive + 1) / (productive + stagnant * 0.5 + failed + 2)
        return round(max(0.05, min(0.95, value)), 6)

    @classmethod
    def _decision_from_row(cls, row: Any) -> MetacognitiveDecisionRecord:
        decision_id = row["decision_id"]
        with durable_boundary("metacognitive decision", decision_id):
            score = durable_float(row["score"], "metacognitive decision", decision_id)
            uncertainty = durable_float(row["uncertainty"], "metacognitive decision", decision_id)
            fixation_risk = durable_float(
                row["fixation_risk"], "metacognitive decision", decision_id
            )
            resource_pressure = durable_float(
                row["resource_pressure"], "metacognitive decision", decision_id
            )
            expected = cls._decision_hash(
                row["strategy"],
                row["target_type"],
                row["target_id"],
                row["reason_code"],
                score,
                uncertainty,
                fixation_risk,
                resource_pressure,
                row["created_at"],
            )
            if expected != row["state_hash"]:
                raise IntegrityError(f"metacognitive decision mismatch: {decision_id}")
            return MetacognitiveDecisionRecord(
                decision_id,
                row["subject_id"],
                row["strategy"],
                row["target_type"],
                row["target_id"],
                row["reason_code"],
                score,
                uncertainty,
                fixation_risk,
                resource_pressure,
                row["created_at"],
            )

    @classmethod
    def _profile_from_row(cls, row: Any) -> CognitiveStrategyProfileRecord:
        profile_id = row["profile_id"]
        with durable_boundary("cognitive strategy profile", profile_id):
            attempts = durable_int(row["attempts"], "cognitive strategy profile", profile_id)
            productive = durable_int(row["productive"], "cognitive strategy profile", profile_id)
            stagnant = durable_int(row["stagnant"], "cognitive strategy profile", profile_id)
            failed = durable_int(row["failed"], "cognitive strategy profile", profile_id)
            confidence = durable_float(row["confidence"], "cognitive strategy profile", profile_id)
            current_revision = durable_int(
                row["current_revision"], "cognitive strategy profile", profile_id
            )
            expected = cls._profile_hash(
                row["strategy"],
                attempts,
                productive,
                stagnant,
                failed,
                confidence,
                row["last_outcome"],
            )
            if expected != row["state_hash"]:
                raise IntegrityError(f"cognitive strategy profile mismatch: {profile_id}")
            return CognitiveStrategyProfileRecord(
                profile_id,
                row["subject_id"],
                row["strategy"],
                attempts,
                productive,
                stagnant,
                failed,
                confidence,
                row["last_outcome"],
                current_revision,
                row["created_at"],
                row["updated_at"],
            )

    @staticmethod
    def _decision_hash(
        strategy: str,
        target_type: str,
        target_id: str | None,
        reason_code: str,
        score: float,
        uncertainty: float,
        fixation_risk: float,
        resource_pressure: float,
        created_at: str,
    ) -> str:
        return content_hash(
            {
                "strategy": strategy,
                "target_type": target_type,
                "target_id": target_id,
                "reason_code": reason_code,
                "score": score,
                "uncertainty": uncertainty,
                "fixation_risk": fixation_risk,
                "resource_pressure": resource_pressure,
                "created_at": created_at,
            }
        )

    @staticmethod
    def _profile_hash(
        strategy: str,
        attempts: int,
        productive: int,
        stagnant: int,
        failed: int,
        confidence: float,
        last_outcome: str,
    ) -> str:
        return content_hash(
            {
                "strategy": strategy,
                "attempts": attempts,
                "productive": productive,
                "stagnant": stagnant,
                "failed": failed,
                "confidence": confidence,
                "last_outcome": last_outcome,
            }
        )

    @staticmethod
    def _profile_revision_hash(
        attempts: int,
        productive: int,
        stagnant: int,
        failed: int,
        confidence: float,
        last_outcome: str,
        source_type: str,
        source_id: str,
        reason: str,
    ) -> str:
        return content_hash(
            {
                "attempts": attempts,
                "productive": productive,
                "stagnant": stagnant,
                "failed": failed,
                "confidence": confidence,
                "last_outcome": last_outcome,
                "source_type": source_type,
                "source_id": source_id,
                "reason": reason,
            }
        )

    @staticmethod
    def _outcome_hash(
        decision_id: str,
        strategy: str,
        source_type: str,
        source_id: str,
        outcome: str,
        token_cost: int,
        rationale_code: str,
        created_at: str,
    ) -> str:
        return content_hash(
            {
                "decision_id": decision_id,
                "strategy": strategy,
                "source_type": source_type,
                "source_id": source_id,
                "outcome": outcome,
                "token_cost": token_cost,
                "rationale_code": rationale_code,
                "created_at": created_at,
            }
        )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("metacognitive time requires a timezone")
        return parsed.astimezone(UTC)


def metacognitive_wake_after(now: str, seconds: float) -> str:
    parsed = MetacognitiveControl._parse_time(now)
    return (parsed + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
