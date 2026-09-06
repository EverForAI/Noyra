from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SearchProviderType = Literal["brave", "bing", "tavily", "serper"]
SearchMethod = Literal["api", "model", "browser", "wait"]


class SearchProviderInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_type: SearchProviderType
    label: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1, max_length=4096)
    rate_limit_per_hour: int = Field(default=20, ge=1, le=10_000)
    extras: dict[str, str] = Field(default_factory=dict)

    @field_validator("label", "api_key")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("search provider fields cannot be blank")
        return value

    @field_validator("extras")
    @classmethod
    def validate_extras(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 16 or any(
            not key.strip() or not item.strip() or len(key) > 64 or len(item) > 2048
            for key, item in value.items()
        ):
            raise ValueError("search provider extras are invalid")
        return value


@dataclass(frozen=True)
class SearchProviderRecord:
    config_id: str
    subject_id: str
    provider_type: str
    label: str
    key_fingerprint: str
    extras: dict[str, str]
    rate_limit_per_hour: int
    status: str
    created_at: str
    revoked_at: str | None
    revoke_reason: str | None


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    rank: int


@dataclass(frozen=True)
class SearchExecution:
    action_id: str
    provider_config_id: str
    provider_type: str
    query: str
    results: tuple[SearchResult, ...]


class ResearchPlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    summary: str = Field(min_length=1, max_length=20_000)
    disposition: Literal["search", "wait"]
    goal_id: str | None = Field(default=None, max_length=256)
    query: str | None = Field(default=None, max_length=512)
    method: SearchMethod
    provider_config_id: str | None = Field(default=None, max_length=256)
    expected_information: str | None = Field(default=None, max_length=10_000)
    reason: str = Field(min_length=1, max_length=10_000)
    evidence_event_ids: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("summary", "reason")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("research plan fields cannot be blank")
        return value

    @field_validator("evidence_event_ids")
    @classmethod
    def validate_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("research evidence must be distinct and nonblank")
        return value

    @model_validator(mode="after")
    def validate_plan(self) -> ResearchPlanProposal:
        if self.disposition == "wait":
            if self.method != "wait" or any(
                value is not None
                for value in (
                    self.goal_id,
                    self.query,
                    self.provider_config_id,
                    self.expected_information,
                )
            ):
                raise ValueError("wait research plan cannot contain a search")
            return self
        if self.method == "wait":
            raise ValueError("search research plan requires a search method")
        if any(
            value is None or not value.strip()
            for value in (self.goal_id, self.query, self.expected_information)
        ):
            raise ValueError("search research plan is incomplete")
        if self.method == "api" and self.provider_config_id is None:
            raise ValueError("API research requires a configured provider")
        if self.method != "api" and self.provider_config_id is not None:
            raise ValueError("non-API research cannot select an API provider")
        return self


class ModelSearchProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    results: tuple[dict[str, Any], ...] = Field(default=(), max_length=10)


class ResearchAssessmentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sufficient: bool
    next_method: SearchMethod
    reason: str = Field(min_length=1, max_length=10_000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("research assessment reason cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_assessment(self) -> ResearchAssessmentProposal:
        if self.sufficient and self.next_method != "wait":
            raise ValueError("sufficient results require no next search method")
        if not self.sufficient and self.next_method == "wait":
            raise ValueError("insufficient results require another search method")
        return self
