from __future__ import annotations

import json
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from noyra.model.errors import ConfigurationError
from noyra.world import canonical_public_url


class WorldSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1, max_length=256)
    url: str = Field(min_length=1, max_length=2_048)
    source_type: Literal["news", "rss", "web", "api"]
    trust_score: float = Field(default=0.5, ge=0, le=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("world source name cannot be blank")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return canonical_public_url(value)


class CognitionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    sources: tuple[WorldSourceConfig, ...] = Field(default=(), max_length=64)
    source_refresh_seconds: float = Field(default=900, ge=0, le=604_800)
    web_rate_limit_per_hour: int = Field(default=24, ge=1, le=10_000)
    # Public HTTPS reading is safe only through SourceRegistry + SafeWebReader
    # (canonical URL validation, DNS/IP pinning, response limits and audit
    # records).  Keep the policy explicit so an operator can opt back into a
    # host allow-list for high-assurance deployments without changing code.
    web_read_public_by_default: bool = True
    max_observation_chars: int = Field(default=50_000, ge=1_024, le=200_000)
    max_output_tokens: int = Field(default=2_500, ge=256, le=32_000)
    temperature: float = Field(default=0.2, ge=0, le=1)
    max_model_calls_per_observation: int = Field(default=3, ge=1, le=10)
    minimum_genesis_cycles: int = Field(default=7, ge=1, le=100)
    genesis_sleep_seconds: float = Field(default=3_600, ge=0, le=604_800)
    interaction_cooldown_seconds: float = Field(default=300, ge=0, le=86_400)
    max_interaction_model_calls_per_day: int = Field(default=3, ge=1, le=20)
    max_sleep_model_calls_per_run: int = Field(default=3, ge=1, le=10)
    max_sleep_context_chars: int = Field(default=60_000, ge=10_000, le=200_000)
    goal_governance_interval_seconds: float = Field(default=3_600, ge=60, le=604_800)
    max_goal_governance_model_calls_per_day: int = Field(default=4, ge=1, le=24)
    max_goal_governance_context_chars: int = Field(default=40_000, ge=8_000, le=100_000)
    max_active_goals: int = Field(default=3, ge=1, le=8)
    action_deliberation_interval_seconds: float = Field(default=3_600, ge=60, le=604_800)
    max_action_deliberation_model_calls_per_day: int = Field(default=4, ge=1, le=24)
    max_action_deliberation_context_chars: int = Field(default=24_000, ge=4_000, le=100_000)
    max_web_actions_per_goal_per_day: int = Field(default=3, ge=1, le=24)
    research_interval_seconds: float = Field(default=7_200, ge=60, le=604_800)
    max_research_model_calls_per_day: int = Field(default=6, ge=1, le=48)
    max_research_context_chars: int = Field(default=36_000, ge=8_000, le=150_000)
    max_search_rounds_per_run: int = Field(default=2, ge=1, le=4)
    max_search_results_per_round: int = Field(default=8, ge=1, le=20)
    max_discovered_sources_per_run: int = Field(default=4, ge=1, le=12)
    max_browser_searches_per_hour: int = Field(default=12, ge=1, le=100)
    outcome_evaluation_interval_seconds: float = Field(default=60, ge=0, le=86_400)
    max_goal_progress_delta_per_outcome: float = Field(default=0.05, ge=0.001, le=0.1)
    max_epistemic_review_model_calls_per_day: int = Field(default=4, ge=1, le=24)
    max_belief_confidence_delta: float = Field(default=0.15, ge=0.01, le=0.3)
    memory_consolidation_interval_seconds: float = Field(default=86_400, ge=60, le=2_592_000)
    memory_stale_after_days: float = Field(default=30, ge=1, le=3_650)
    memory_archive_after_days: float = Field(default=180, ge=7, le=7_300)
    minimum_active_memories: int = Field(default=24, ge=1, le=10_000)
    memory_integration_interval_seconds: float = Field(default=86_400, ge=3_600, le=2_592_000)
    max_memory_integration_calls_per_day: int = Field(default=2, ge=1, le=12)
    max_memory_integration_context_chars: int = Field(default=32_000, ge=8_000, le=150_000)
    social_review_interval_seconds: float = Field(default=21_600, ge=300, le=2_592_000)
    max_social_model_calls_per_day: int = Field(default=4, ge=1, le=24)
    max_social_context_chars: int = Field(default=32_000, ge=8_000, le=150_000)
    self_model_review_interval_seconds: float = Field(default=86_400, ge=3_600, le=2_592_000)
    max_self_model_calls_per_day: int = Field(default=2, ge=1, le=12)
    max_self_model_context_chars: int = Field(default=48_000, ge=12_000, le=200_000)
    thought_interval_seconds: float = Field(default=1_800, ge=300, le=604_800)
    max_thought_model_calls_per_day: int = Field(default=8, ge=1, le=96)
    max_thought_context_chars: int = Field(default=32_000, ge=8_000, le=150_000)
    max_thought_no_change_streak: int = Field(default=3, ge=1, le=12)
    thought_cooldown_seconds: float = Field(default=21_600, ge=300, le=2_592_000)
    max_thought_goals_per_day: int = Field(default=1, ge=0, le=8)
    metacognitive_sleep_threshold: float = Field(default=0.82, ge=0.5, le=1)
    metacognitive_sleep_seconds: float = Field(default=3_600, ge=0, le=604_800)
    metacognitive_fixation_penalty: float = Field(default=0.2, ge=0.05, le=0.5)
    metacognitive_pending_timeout_seconds: float = Field(default=1_800, ge=60, le=604_800)
    motivation_review_interval_seconds: float = Field(default=172_800, ge=3_600, le=5_184_000)
    max_motivation_model_calls_per_day: int = Field(default=1, ge=1, le=8)
    max_motivation_context_chars: int = Field(default=56_000, ge=12_000, le=200_000)
    max_value_weight_delta: float = Field(default=0.15, ge=0.01, le=0.3)
    minimum_mission_value_count: int = Field(default=2, ge=2, le=8)
    minimum_mission_sleep_count: int = Field(default=2, ge=1, le=30)
    minimum_mission_adoption_sleep_count: int = Field(default=5, ge=2, le=100)
    project_formation_interval_seconds: float = Field(default=86_400, ge=3_600, le=2_592_000)
    project_review_interval_seconds: float = Field(default=7_200, ge=300, le=604_800)
    max_project_model_calls_per_day: int = Field(default=4, ge=1, le=24)
    max_project_context_chars: int = Field(default=48_000, ge=12_000, le=200_000)
    max_active_projects: int = Field(default=2, ge=1, le=8)
    max_project_duration_hours: float = Field(default=168, ge=1, le=720)
    max_project_phases: int = Field(default=8, ge=2, le=16)
    max_project_cycles: int = Field(default=24, ge=2, le=96)
    max_project_model_calls: int = Field(default=16, ge=2, le=48)
    max_project_searches: int = Field(default=12, ge=0, le=48)
    max_project_external_actions: int = Field(default=8, ge=0, le=24)
    max_project_storage_bytes: int = Field(default=2_000_000, ge=0, le=20_000_000)
    max_project_no_progress_reviews: int = Field(default=3, ge=1, le=12)

    @model_validator(mode="after")
    def validate_enabled_sources(self) -> CognitionSettings:
        if self.enabled and not self.sources:
            raise ValueError("enabled cognition requires at least one configured world source")
        urls = [source.url for source in self.sources]
        if len(set(urls)) != len(urls):
            raise ValueError("world source URLs must be unique")
        return self

    @classmethod
    def from_env(cls) -> CognitionSettings:
        enabled = cls._parse_bool(os.getenv("NOYRA_COGNITION_ENABLED", "false"))
        raw_sources = os.getenv("NOYRA_WORLD_SOURCES_JSON", "[]")
        try:
            sources = json.loads(raw_sources)
        except (TypeError, ValueError) as error:
            raise ConfigurationError("NOYRA_WORLD_SOURCES_JSON must be valid JSON") from error
        if not isinstance(sources, list):
            raise ConfigurationError("NOYRA_WORLD_SOURCES_JSON must contain a JSON array")
        try:
            return cls.model_validate(
                {
                    "enabled": enabled,
                    "sources": sources,
                    "source_refresh_seconds": os.getenv("NOYRA_SOURCE_REFRESH_SECONDS", "900"),
                    "web_rate_limit_per_hour": os.getenv("NOYRA_WORLD_READS_PER_HOUR", "24"),
                    "web_read_public_by_default": cls._parse_bool(
                        os.getenv("NOYRA_WEB_READ_PUBLIC_BY_DEFAULT", "true")
                    ),
                    "max_observation_chars": os.getenv(
                        "NOYRA_COGNITION_MAX_OBSERVATION_CHARS", "50000"
                    ),
                    "max_output_tokens": os.getenv("NOYRA_COGNITION_MAX_OUTPUT_TOKENS", "2500"),
                    "temperature": os.getenv("NOYRA_COGNITION_TEMPERATURE", "0.2"),
                    "max_model_calls_per_observation": os.getenv(
                        "NOYRA_COGNITION_MAX_MODEL_CALLS_PER_OBSERVATION", "3"
                    ),
                    "minimum_genesis_cycles": os.getenv("NOYRA_MINIMUM_GENESIS_CYCLES", "7"),
                    "genesis_sleep_seconds": os.getenv("NOYRA_GENESIS_SLEEP_SECONDS", "3600"),
                    "interaction_cooldown_seconds": os.getenv(
                        "NOYRA_INTERACTION_COOLDOWN_SECONDS", "300"
                    ),
                    "max_interaction_model_calls_per_day": os.getenv(
                        "NOYRA_MAX_INTERACTION_MODEL_CALLS_PER_DAY", "3"
                    ),
                    "max_sleep_model_calls_per_run": os.getenv(
                        "NOYRA_MAX_SLEEP_MODEL_CALLS_PER_RUN", "3"
                    ),
                    "max_sleep_context_chars": os.getenv("NOYRA_MAX_SLEEP_CONTEXT_CHARS", "60000"),
                    "goal_governance_interval_seconds": os.getenv(
                        "NOYRA_GOAL_GOVERNANCE_INTERVAL_SECONDS", "3600"
                    ),
                    "max_goal_governance_model_calls_per_day": os.getenv(
                        "NOYRA_MAX_GOAL_GOVERNANCE_MODEL_CALLS_PER_DAY", "4"
                    ),
                    "max_goal_governance_context_chars": os.getenv(
                        "NOYRA_MAX_GOAL_GOVERNANCE_CONTEXT_CHARS", "40000"
                    ),
                    "max_active_goals": os.getenv("NOYRA_MAX_ACTIVE_GOALS", "3"),
                    "action_deliberation_interval_seconds": os.getenv(
                        "NOYRA_ACTION_DELIBERATION_INTERVAL_SECONDS", "3600"
                    ),
                    "max_action_deliberation_model_calls_per_day": os.getenv(
                        "NOYRA_MAX_ACTION_DELIBERATION_MODEL_CALLS_PER_DAY", "4"
                    ),
                    "max_action_deliberation_context_chars": os.getenv(
                        "NOYRA_MAX_ACTION_DELIBERATION_CONTEXT_CHARS", "24000"
                    ),
                    "max_web_actions_per_goal_per_day": os.getenv(
                        "NOYRA_MAX_WEB_ACTIONS_PER_GOAL_PER_DAY", "3"
                    ),
                    "research_interval_seconds": os.getenv(
                        "NOYRA_RESEARCH_INTERVAL_SECONDS", "7200"
                    ),
                    "max_research_model_calls_per_day": os.getenv(
                        "NOYRA_MAX_RESEARCH_MODEL_CALLS_PER_DAY", "6"
                    ),
                    "max_research_context_chars": os.getenv(
                        "NOYRA_MAX_RESEARCH_CONTEXT_CHARS", "36000"
                    ),
                    "max_search_rounds_per_run": os.getenv("NOYRA_MAX_SEARCH_ROUNDS_PER_RUN", "2"),
                    "max_search_results_per_round": os.getenv(
                        "NOYRA_MAX_SEARCH_RESULTS_PER_ROUND", "8"
                    ),
                    "max_discovered_sources_per_run": os.getenv(
                        "NOYRA_MAX_DISCOVERED_SOURCES_PER_RUN", "4"
                    ),
                    "max_browser_searches_per_hour": os.getenv(
                        "NOYRA_MAX_BROWSER_SEARCHES_PER_HOUR", "12"
                    ),
                    "outcome_evaluation_interval_seconds": os.getenv(
                        "NOYRA_OUTCOME_EVALUATION_INTERVAL_SECONDS", "60"
                    ),
                    "max_goal_progress_delta_per_outcome": os.getenv(
                        "NOYRA_MAX_GOAL_PROGRESS_DELTA_PER_OUTCOME", "0.05"
                    ),
                    "max_epistemic_review_model_calls_per_day": os.getenv(
                        "NOYRA_MAX_EPISTEMIC_REVIEW_MODEL_CALLS_PER_DAY", "4"
                    ),
                    "max_belief_confidence_delta": os.getenv(
                        "NOYRA_MAX_BELIEF_CONFIDENCE_DELTA", "0.15"
                    ),
                    "memory_consolidation_interval_seconds": os.getenv(
                        "NOYRA_MEMORY_CONSOLIDATION_INTERVAL_SECONDS", "86400"
                    ),
                    "memory_stale_after_days": os.getenv("NOYRA_MEMORY_STALE_AFTER_DAYS", "30"),
                    "memory_archive_after_days": os.getenv(
                        "NOYRA_MEMORY_ARCHIVE_AFTER_DAYS", "180"
                    ),
                    "minimum_active_memories": os.getenv("NOYRA_MINIMUM_ACTIVE_MEMORIES", "24"),
                    "memory_integration_interval_seconds": os.getenv(
                        "NOYRA_MEMORY_INTEGRATION_INTERVAL_SECONDS", "86400"
                    ),
                    "max_memory_integration_calls_per_day": os.getenv(
                        "NOYRA_MAX_MEMORY_INTEGRATION_CALLS_PER_DAY", "2"
                    ),
                    "max_memory_integration_context_chars": os.getenv(
                        "NOYRA_MAX_MEMORY_INTEGRATION_CONTEXT_CHARS", "32000"
                    ),
                    "social_review_interval_seconds": os.getenv(
                        "NOYRA_SOCIAL_REVIEW_INTERVAL_SECONDS", "21600"
                    ),
                    "max_social_model_calls_per_day": os.getenv(
                        "NOYRA_MAX_SOCIAL_MODEL_CALLS_PER_DAY", "4"
                    ),
                    "max_social_context_chars": os.getenv(
                        "NOYRA_MAX_SOCIAL_CONTEXT_CHARS", "32000"
                    ),
                    "self_model_review_interval_seconds": os.getenv(
                        "NOYRA_SELF_MODEL_REVIEW_INTERVAL_SECONDS", "86400"
                    ),
                    "max_self_model_calls_per_day": os.getenv(
                        "NOYRA_MAX_SELF_MODEL_CALLS_PER_DAY", "2"
                    ),
                    "max_self_model_context_chars": os.getenv(
                        "NOYRA_MAX_SELF_MODEL_CONTEXT_CHARS", "48000"
                    ),
                    "thought_interval_seconds": os.getenv("NOYRA_THOUGHT_INTERVAL_SECONDS", "1800"),
                    "max_thought_model_calls_per_day": os.getenv(
                        "NOYRA_MAX_THOUGHT_MODEL_CALLS_PER_DAY", "8"
                    ),
                    "max_thought_context_chars": os.getenv(
                        "NOYRA_MAX_THOUGHT_CONTEXT_CHARS", "32000"
                    ),
                    "max_thought_no_change_streak": os.getenv(
                        "NOYRA_MAX_THOUGHT_NO_CHANGE_STREAK", "3"
                    ),
                    "thought_cooldown_seconds": os.getenv(
                        "NOYRA_THOUGHT_COOLDOWN_SECONDS", "21600"
                    ),
                    "max_thought_goals_per_day": os.getenv("NOYRA_MAX_THOUGHT_GOALS_PER_DAY", "1"),
                    "metacognitive_sleep_threshold": os.getenv(
                        "NOYRA_METACOGNITIVE_SLEEP_THRESHOLD", "0.82"
                    ),
                    "metacognitive_sleep_seconds": os.getenv(
                        "NOYRA_METACOGNITIVE_SLEEP_SECONDS", "3600"
                    ),
                    "metacognitive_fixation_penalty": os.getenv(
                        "NOYRA_METACOGNITIVE_FIXATION_PENALTY", "0.2"
                    ),
                    "metacognitive_pending_timeout_seconds": os.getenv(
                        "NOYRA_METACOGNITIVE_PENDING_TIMEOUT_SECONDS", "1800"
                    ),
                    "motivation_review_interval_seconds": os.getenv(
                        "NOYRA_MOTIVATION_REVIEW_INTERVAL_SECONDS", "172800"
                    ),
                    "max_motivation_model_calls_per_day": os.getenv(
                        "NOYRA_MAX_MOTIVATION_MODEL_CALLS_PER_DAY", "1"
                    ),
                    "max_motivation_context_chars": os.getenv(
                        "NOYRA_MAX_MOTIVATION_CONTEXT_CHARS", "56000"
                    ),
                    "max_value_weight_delta": os.getenv("NOYRA_MAX_VALUE_WEIGHT_DELTA", "0.15"),
                    "minimum_mission_value_count": os.getenv(
                        "NOYRA_MINIMUM_MISSION_VALUE_COUNT", "2"
                    ),
                    "minimum_mission_sleep_count": os.getenv(
                        "NOYRA_MINIMUM_MISSION_SLEEP_COUNT", "2"
                    ),
                    "minimum_mission_adoption_sleep_count": os.getenv(
                        "NOYRA_MINIMUM_MISSION_ADOPTION_SLEEP_COUNT", "5"
                    ),
                    "project_formation_interval_seconds": os.getenv(
                        "NOYRA_PROJECT_FORMATION_INTERVAL_SECONDS", "86400"
                    ),
                    "project_review_interval_seconds": os.getenv(
                        "NOYRA_PROJECT_REVIEW_INTERVAL_SECONDS", "7200"
                    ),
                    "max_project_model_calls_per_day": os.getenv(
                        "NOYRA_MAX_PROJECT_MODEL_CALLS_PER_DAY", "4"
                    ),
                    "max_project_context_chars": os.getenv(
                        "NOYRA_MAX_PROJECT_CONTEXT_CHARS", "48000"
                    ),
                    "max_active_projects": os.getenv("NOYRA_MAX_ACTIVE_PROJECTS", "2"),
                    "max_project_duration_hours": os.getenv(
                        "NOYRA_MAX_PROJECT_DURATION_HOURS", "168"
                    ),
                    "max_project_phases": os.getenv("NOYRA_MAX_PROJECT_PHASES", "8"),
                    "max_project_cycles": os.getenv("NOYRA_MAX_PROJECT_CYCLES", "24"),
                    "max_project_model_calls": os.getenv("NOYRA_MAX_PROJECT_MODEL_CALLS", "16"),
                    "max_project_searches": os.getenv("NOYRA_MAX_PROJECT_SEARCHES", "12"),
                    "max_project_external_actions": os.getenv(
                        "NOYRA_MAX_PROJECT_EXTERNAL_ACTIONS", "8"
                    ),
                    "max_project_storage_bytes": os.getenv(
                        "NOYRA_MAX_PROJECT_STORAGE_BYTES", "2000000"
                    ),
                    "max_project_no_progress_reviews": os.getenv(
                        "NOYRA_MAX_PROJECT_NO_PROGRESS_REVIEWS", "3"
                    ),
                }
            )
        except ValidationError as error:
            raise ConfigurationError("invalid cognition settings") from error

    @staticmethod
    def _parse_bool(value: str) -> bool:
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ConfigurationError("NOYRA_COGNITION_ENABLED must be true or false")
