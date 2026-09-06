from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class LoopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    active_interval_seconds: float = Field(default=30, ge=1, le=86_400)
    sleep_interval_seconds: float = Field(default=60, ge=1, le=86_400)
    deep_sleep_seconds: float = Field(default=21_600, ge=0, le=604_800)
    error_backoff_seconds: float = Field(default=30, ge=1, le=86_400)
    max_error_backoff_seconds: float = Field(default=1_800, ge=1, le=86_400)
    max_consecutive_failures: int = Field(default=5, ge=1, le=100)
    circuit_cooldown_seconds: float = Field(default=3_600, ge=1, le=604_800)
    sleep_conflict_deadline_seconds: float = Field(default=300, ge=1, le=604_800)


@dataclass(frozen=True)
class TickResult:
    lifecycle: str
    action: str
    event_id: str | None
    next_interval_seconds: float
