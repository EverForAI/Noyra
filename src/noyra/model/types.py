from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message content cannot be blank")
        return value


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model: str = Field(min_length=1, max_length=256)
    messages: tuple[ModelMessage, ...] = Field(min_length=1)
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    temperature: float = Field(ge=0, le=2)
    schema_name: str = Field(min_length=1, max_length=128)
    output_schema: dict[str, Any]


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token usage cannot be negative")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    usage: ModelUsage | None
    finish_reason: str | None = None
    provider_request_id: str | None = None


@dataclass(frozen=True)
class BudgetLimits:
    daily_attempts: int
    daily_input_tokens: int
    daily_output_tokens: int
    daily_cost_microusd: int

    def __post_init__(self) -> None:
        if (
            min(
                self.daily_attempts,
                self.daily_input_tokens,
                self.daily_output_tokens,
                self.daily_cost_microusd,
            )
            < 0
        ):
            raise ValueError("budget limits cannot be negative")


@dataclass(frozen=True)
class ModelPricing:
    input_microusd_per_million: int = 0
    output_microusd_per_million: int = 0

    def __post_init__(self) -> None:
        if self.input_microusd_per_million < 0 or self.output_microusd_per_million < 0:
            raise ValueError("model pricing cannot be negative")

    def cost_microusd(self, usage: ModelUsage) -> int:
        numerator = (
            usage.input_tokens * self.input_microusd_per_million
            + usage.output_tokens * self.output_microusd_per_million
        )
        return (numerator + 999_999) // 1_000_000


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    retry_invalid_output: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError("base retry delay cannot exceed maximum delay")


@dataclass(frozen=True)
class CallRecord:
    call_id: str
    subject_id: str
    provider: str
    model: str
    purpose: str
    request_hash: str
    idempotency_key: str
    status: str
    response: dict[str, Any] | None
    response_hash: str | None
    usage_estimated: bool
    error_code: str | None
    created_at: str
    completed_at: str | None
    resource_pool: str = "deep"
    resource_group_id: str | None = None


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    call_id: str
    subject_id: str
    budget_day: str
    attempt_number: int
    status: str
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_cost_microusd: int
    input_tokens: int | None
    output_tokens: int | None
    cost_microusd: int | None
    provider_request_id: str | None
    error_code: str | None
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class BudgetStatus:
    budget_day: str
    attempts: int
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    limits: BudgetLimits

    @property
    def pressure(self) -> float:
        ratios = [
            self.attempts / self.limits.daily_attempts if self.limits.daily_attempts else 1.0,
            self.input_tokens / self.limits.daily_input_tokens
            if self.limits.daily_input_tokens
            else 1.0,
            self.output_tokens / self.limits.daily_output_tokens
            if self.limits.daily_output_tokens
            else 1.0,
            self.cost_microusd / self.limits.daily_cost_microusd
            if self.limits.daily_cost_microusd
            else 1.0,
        ]
        return min(1.0, max(ratios))


OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True)
class GatewayResult(Generic[OutputT]):
    output: OutputT
    raw_content: str
    usage: ModelUsage
    usage_estimated: bool
    cost_microusd: int
    call_id: str
    attempts: int
    cached: bool
