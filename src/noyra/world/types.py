from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SourceType = Literal["news", "rss", "web", "api"]
SourceStatus = Literal["candidate", "active", "blocked"]


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    subject_id: str
    name: str
    url: str
    source_type: str
    trust_score: float
    status: str
    state_hash: str
    current_revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class FetchedDocument:
    url: str
    title: str | None
    content: str
    content_hash: str
    media_type: str
    injection_signals: tuple[str, ...]
    etag: str | None
    last_modified: str | None
    fetched_at: str


@dataclass(frozen=True)
class ObservationRecord:
    observation_id: str
    subject_id: str
    source_id: str
    event_id: str
    canonical_url: str
    title: str | None
    content: str
    content_hash: str
    media_type: str
    injection_signals: tuple[str, ...]
    fetched_at: str
    status: str


class ClaimProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposition: str = Field(min_length=1, max_length=20_000)
    confidence: float = Field(ge=0, le=1)

    @field_validator("proposition")
    @classmethod
    def reject_blank_claim(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("claim proposition cannot be blank")
        return value


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    subject_id: str
    proposition: str
    proposition_hash: str
    confidence: float
    status: str
    state_hash: str
    current_revision: int
    created_at: str
    updated_at: str


class PredictionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    statement: str = Field(min_length=1, max_length=20_000)
    probability: float = Field(ge=0, le=1)
    target_at: str = Field(min_length=1, max_length=64)
    resolution_criteria: str = Field(min_length=1, max_length=10_000)

    @field_validator("statement", "target_at", "resolution_criteria")
    @classmethod
    def reject_blank_prediction(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prediction fields cannot be blank")
        return value


@dataclass(frozen=True)
class PredictionRecord:
    prediction_id: str
    subject_id: str
    statement: str
    probability: float
    target_at: str
    resolution_criteria: str
    status: str
    outcome: bool | None
    brier_score: float | None
    state_hash: str
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class GenesisRunRecord:
    run_id: str
    subject_id: str
    status: str
    minimum_cycles: int
    completed_cycles: int
    sleep_reference: str | None
    state_hash: str
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class GenesisCycleRecord:
    cycle_id: str
    run_id: str
    cycle_number: int
    observation_ids: tuple[str, ...]
    appraisal_ids: tuple[str, ...]
    prediction_ids: tuple[str, ...]
    goal_ids: tuple[str, ...]
    summary: str
    state_hash: str
    created_at: str
