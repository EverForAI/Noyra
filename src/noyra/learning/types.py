from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyProfileRecord:
    profile_id: str
    subject_id: str
    goal_id: str
    strategy_id: str
    strategy_kind: str
    method: str
    attempts: int
    successes: int
    failures: int
    inconclusive: int
    confidence: float
    last_outcome: str
    current_revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class OutcomeEvaluationRecord:
    evaluation_id: str
    subject_id: str
    goal_id: str
    strategy_id: str
    strategy_kind: str
    source_type: str
    source_id: str
    source_status: str
    outcome: str
    evidence_event_id: str
    observation_id: str | None
    result_count: int
    accepted_source_count: int
    progress_before: float
    progress_after: float
    confidence_before: float
    confidence_after: float
    rationale_code: str
    public_summary: str
    created_at: str
