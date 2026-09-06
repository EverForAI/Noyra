from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FatigueInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    resource_pressure: float = Field(ge=0, le=1)
    cognitive_load: float = Field(ge=0, le=1)
    frustration: float = Field(ge=0, le=1)
    goal_conflict: float = Field(ge=0, le=1)
    staleness: float = Field(ge=0, le=1)


class SleepMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    content: str = Field(min_length=1, max_length=200_000)
    memory_type: Literal[
        "episodic",
        "autobiographical",
        "semantic",
        "procedural",
        "emotional",
        "relationship",
        "prediction",
        "reflection",
    ] = "reflection"
    salience: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    source_event_ids: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("content")
    @classmethod
    def nonblank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sleep memory content cannot be blank")
        return value


class SleepGoalRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    goal_id: str = Field(min_length=1, max_length=256)
    status: Literal["candidate", "active", "paused", "reconsidering", "achieved", "abandoned"]
    priority: float = Field(ge=0, le=1)
    commitment: float = Field(ge=0, le=1)
    progress: float = Field(ge=0, le=1)
    emotional_pressure: float = Field(ge=-1, le=1)
    reason: str = Field(min_length=1, max_length=10_000)

    @field_validator("goal_id", "reason")
    @classmethod
    def nonblank_goal_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sleep goal fields cannot be blank")
        return value


class SleepBeliefRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    belief_id: str = Field(min_length=1, max_length=256)
    proposition: str = Field(min_length=1, max_length=20_000)
    confidence: float = Field(ge=0, le=1)
    status: Literal["active", "qualified", "retracted"]
    supporting_event_ids: tuple[str, ...] = Field(default=(), max_length=64)
    counter_event_ids: tuple[str, ...] = Field(default=(), max_length=64)
    reason: str = Field(min_length=1, max_length=10_000)

    @field_validator("belief_id", "proposition", "reason")
    @classmethod
    def nonblank_belief_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sleep belief fields cannot be blank")
        return value


class SleepRetryBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1, max_length=256)
    target: str = Field(min_length=1, max_length=4_000)
    goal_id: str | None = Field(default=None, max_length=256)
    strategy_id: str | None = Field(default=None, max_length=256)
    reason: str = Field(min_length=1, max_length=10_000)
    action_ids: tuple[str, ...] = Field(min_length=2, max_length=64)

    @field_validator("tool", "target", "reason")
    @classmethod
    def nonblank_retry_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sleep retry fields cannot be blank")
        return value

    @field_validator("action_ids")
    @classmethod
    def require_distinct_actions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value) or len(set(value)) < 2:
            raise ValueError("sleep retry blocks require at least two distinct actions")
        return value


class PersonalityCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trait: str = Field(min_length=1, max_length=128)
    direction: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: tuple[str, ...] = Field(min_length=3, max_length=64)

    @field_validator("trait")
    @classmethod
    def nonblank_trait(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("personality trait cannot be blank")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def require_distinct_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value) or len(set(value)) < 3:
            raise ValueError("personality candidates require three distinct evidence sources")
        return value


class SleepReflectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=20_000)
    facts: tuple[str, ...] = Field(default=(), max_length=128)
    contradictions: tuple[str, ...] = Field(default=(), max_length=128)
    prediction_errors: tuple[str, ...] = Field(default=(), max_length=128)
    unresolved_questions: tuple[str, ...] = Field(default=(), max_length=128)
    public_diary_candidate: str | None = Field(default=None, max_length=20_000)
    memories: tuple[SleepMemory, ...] = Field(default=(), max_length=32)
    goal_revisions: tuple[SleepGoalRevision, ...] = Field(default=(), max_length=32)
    belief_revisions: tuple[SleepBeliefRevision, ...] = Field(default=(), max_length=32)
    retry_blocks: tuple[SleepRetryBlock, ...] = Field(default=(), max_length=32)
    personality_candidates: tuple[PersonalityCandidateInput, ...] = Field(default=(), max_length=32)

    @field_validator("summary")
    @classmethod
    def nonblank_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sleep reflection summary cannot be blank")
        return value

    @field_validator("public_diary_candidate")
    @classmethod
    def nonblank_public_candidate(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("sleep public diary candidate cannot be blank")
        return value

    @field_validator("facts", "contradictions", "prediction_errors", "unresolved_questions")
    @classmethod
    def validate_reflection_lists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 20_000 for item in value):
            raise ValueError("sleep reflection list entries must be nonblank and bounded")
        if len(set(value)) != len(value):
            raise ValueError("sleep reflection list entries must be unique")
        return value


@dataclass(frozen=True)
class FatigueState:
    subject_id: str
    fatigue: float
    mode: str
    resource_pressure: float
    cognitive_load: float
    frustration: float
    goal_conflict: float
    staleness: float
    version: int
    updated_at: str


@dataclass(frozen=True)
class SleepRunRecord:
    sleep_id: str
    subject_id: str
    status: str
    trigger_type: str
    trigger_reason: str
    emergency: bool
    pre_sleep_fatigue: float
    wake_after: str | None
    reflection_event_id: str | None
    checkpoint_id: str | None
    version: int
    started_at: str
    updated_at: str
    completed_at: str | None
