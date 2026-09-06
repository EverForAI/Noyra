from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MemoryType = Literal[
    "episodic",
    "autobiographical",
    "semantic",
    "procedural",
    "emotional",
    "relationship",
    "prediction",
    "reflection",
]
GoalOrigin = Literal["self", "environment", "human_proposal", "maintenance", "mixed"]
GoalStatus = Literal[
    "proposed",
    "candidate",
    "active",
    "paused",
    "reconsidering",
    "achieved",
    "abandoned",
]


class AppraisalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    novelty: float = Field(ge=0, le=1)
    goal_congruence: float = Field(ge=-1, le=1)
    controllability: float = Field(ge=0, le=1)
    certainty: float = Field(ge=0, le=1)
    agency: str = Field(min_length=1, max_length=128)
    narrative: str = Field(min_length=1, max_length=20_000)

    @field_validator("agency", "narrative")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("appraisal text cannot be blank")
        return value


class AffectImpulse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    emotion_type: str = Field(min_length=1, max_length=64)
    target_type: str = Field(default="world", min_length=1, max_length=64)
    target_id: str | None = Field(default=None, max_length=256)
    impulse: float = Field(ge=-1, le=1)
    valence: float = Field(ge=-1, le=1)
    arousal: float = Field(ge=0, le=1)
    dominance: float = Field(ge=-1, le=1)
    decay_rate: float = Field(default=0.1, ge=0, le=1)
    goal_effect: float = Field(default=0, ge=-1, le=1)

    @field_validator("emotion_type", "target_type")
    @classmethod
    def normalize_labels(cls, value: str) -> str:
        normalized = " ".join(value.strip().lower().split())
        if not normalized:
            raise ValueError("affect labels cannot be blank")
        return normalized


class GoalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=10_000)
    origin: GoalOrigin
    priority: float = Field(ge=0, le=1)
    commitment: float = Field(ge=0, le=1)
    motive_emotion: str = Field(min_length=1, max_length=64)
    motive_target_type: str = Field(default="world", min_length=1, max_length=64)
    motive_target_id: str | None = Field(default=None, max_length=256)
    minimum_motive_intensity: float = Field(default=0.35, ge=0, le=1)

    @field_validator("title", "description", "motive_emotion", "motive_target_type")
    @classmethod
    def reject_blank_candidate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("goal candidate text cannot be blank")
        return value


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    subject_id: str
    memory_type: str
    content: str
    content_hash: str
    salience: float
    confidence: float
    privacy_level: str
    status: str
    current_revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MemoryRecall:
    memory: MemoryRecord
    relevance: float
    lexical_score: float
    salience_score: float
    confidence_score: float
    recency_score: float
    access_score: float
    semantic_score: float = 0.0
    entity_score: float = 0.0
    temporal_score: float = 0.0
    causal_score: float = 0.0


@dataclass(frozen=True)
class MemoryConsolidationRecord:
    consolidation_id: str
    subject_id: str
    status: str
    reviewed_count: int
    archived_memory_ids: tuple[str, ...]
    strengthened_memory_ids: tuple[str, ...]
    summary_memory_ids: tuple[str, ...]
    summary: str
    created_at: str


@dataclass(frozen=True)
class BeliefRecord:
    belief_id: str
    subject_id: str
    proposition: str
    confidence: float
    scope: str
    status: str
    current_revision: int
    created_at: str
    reviewed_at: str


@dataclass(frozen=True)
class AppraisalRecord:
    appraisal_id: str
    subject_id: str
    event_id: str
    novelty: float
    goal_congruence: float
    controllability: float
    certainty: float
    agency: str
    narrative: str
    narrative_hash: str
    created_at: str


@dataclass(frozen=True)
class AffectRecord:
    subject_id: str
    emotion_type: str
    target_key: str
    target_type: str
    target_id: str | None
    intensity: float
    valence: float
    arousal: float
    dominance: float
    decay_rate: float
    goal_effect: float
    version: int
    updated_at: str


@dataclass(frozen=True)
class AffectTransition:
    transition_id: str
    appraisal_id: str
    emotion_type: str
    target_key: str
    old_intensity: float
    impulse: float
    new_intensity: float
    goal_effect: float
    created_at: str


@dataclass(frozen=True)
class MoodRecord:
    subject_id: str
    valence: float
    arousal: float
    stability: float
    version: int
    updated_at: str


@dataclass(frozen=True)
class GoalRecord:
    goal_id: str
    subject_id: str
    title: str
    description: str
    origin: str
    status: str
    priority: float
    commitment: float
    progress: float
    emotional_pressure: float
    current_revision: int
    created_at: str
    updated_at: str

    @property
    def selection_score(self) -> float:
        score = (
            self.priority * 0.45
            + self.commitment * 0.35
            + self.emotional_pressure * 0.15
            + (1 - self.progress) * 0.05
        )
        return max(0.0, min(1.0, score))


@dataclass(frozen=True)
class RelationshipRecord:
    relationship_id: str
    subject_id: str
    entity_type: str
    entity_key: str
    display_name: str
    trust: float
    affinity: float
    conflict: float
    familiarity: float
    boundaries: dict[str, Any]
    current_revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CausalLinkRecord:
    link_id: str
    subject_id: str
    source_type: str
    source_id: str
    relation: str
    target_type: str
    target_id: str
    strength: float
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class PsychologicalSnapshot:
    snapshot_id: str
    subject_id: str
    appraisal_id: str | None
    version: int
    state: dict[str, Any]
    state_hash: str
    created_at: str


@dataclass(frozen=True)
class ExperienceResult:
    appraisal: AppraisalRecord
    transitions: tuple[AffectTransition, ...]
    goals: tuple[GoalRecord, ...]
    mood: MoodRecord
    snapshot: PsychologicalSnapshot
