from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from noyra.mind import AffectImpulse, AppraisalInput, GoalCandidate
from noyra.world import ClaimProposal, PredictionProposal


class CognitionGoalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=10_000)
    origin: Literal["self", "environment", "mixed"]
    priority: float = Field(ge=0, le=1)
    commitment: float = Field(ge=0, le=1)
    motive_emotion: str = Field(min_length=1, max_length=64)
    motive_target_type: str = Field(default="world", min_length=1, max_length=64)
    motive_target_id: str | None = Field(default=None, max_length=256)
    minimum_motive_intensity: float = Field(default=0.35, ge=0, le=1)

    @field_validator("title", "description", "motive_emotion", "motive_target_type")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("cognition goal text cannot be blank")
        return value

    def to_goal_candidate(self) -> GoalCandidate:
        return GoalCandidate.model_validate(self.model_dump(mode="python"))


class CognitionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    summary: str = Field(min_length=1, max_length=20_000)
    appraisal: AppraisalInput
    affect_impulses: tuple[AffectImpulse, ...] = Field(default=(), max_length=8)
    claims: tuple[ClaimProposal, ...] = Field(default=(), max_length=4)
    predictions: tuple[PredictionProposal, ...] = Field(min_length=1, max_length=2)
    goals: tuple[CognitionGoalCandidate, ...] = Field(default=(), max_length=2)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("cognition summary cannot be blank")
        return value


class InteractionCognitionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    disposition: Literal["accepted", "rejected", "deferred", "silent"]
    rationale: str = Field(min_length=1, max_length=10_000)
    response: str | None = Field(default=None, max_length=20_000)
    appraisal: AppraisalInput
    affect_impulses: tuple[AffectImpulse, ...] = Field(default=(), max_length=8)

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("interaction rationale cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_response(self) -> InteractionCognitionProposal:
        if self.response is not None and not self.response.strip():
            raise ValueError("interaction response cannot be blank")
        if self.disposition == "accepted" and self.response is None:
            raise ValueError("accepted interaction requires a response")
        if self.disposition == "silent" and self.response is not None:
            raise ValueError("silent interaction cannot include a response")
        return self


class RelationshipSocialProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    disposition: Literal["contact", "request_help", "wait", "respect_distance"]
    relationship_id: str = Field(min_length=1, max_length=256)
    topic: str = Field(min_length=1, max_length=512)
    rationale: str = Field(min_length=1, max_length=10_000)
    content: str | None = Field(default=None, max_length=20_000)
    evidence_event_ids: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("relationship_id", "topic", "rationale")
    @classmethod
    def validate_social_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("relationship social fields cannot be blank")
        return value

    @field_validator("evidence_event_ids")
    @classmethod
    def validate_social_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("relationship social evidence must be distinct and nonblank")
        return value

    @model_validator(mode="after")
    def validate_social_content(self) -> RelationshipSocialProposal:
        sends = self.disposition in {"contact", "request_help"}
        if sends and (self.content is None or not self.content.strip()):
            raise ValueError("a social contact requires content")
        if not sends and self.content is not None:
            raise ValueError("a non-contact social decision cannot contain content")
        return self


class SelfModelTraitProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trait: str = Field(min_length=1, max_length=128)
    direction: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    source_personality_candidate_ids: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("trait")
    @classmethod
    def validate_trait(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("self-model trait cannot be blank")
        return value


class SelfModelProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    continuity_statement: str = Field(min_length=1, max_length=5_000)
    identity_narrative: str = Field(min_length=1, max_length=20_000)
    values: tuple[str, ...] = Field(min_length=1, max_length=12)
    traits: tuple[SelfModelTraitProposal, ...] = Field(default=(), max_length=12)
    commitments: tuple[str, ...] = Field(default=(), max_length=12)
    uncertainties: tuple[str, ...] = Field(default=(), max_length=12)
    source_event_ids: tuple[str, ...] = Field(min_length=1, max_length=24)
    source_memory_ids: tuple[str, ...] = Field(default=(), max_length=16)
    source_belief_ids: tuple[str, ...] = Field(default=(), max_length=16)
    source_goal_ids: tuple[str, ...] = Field(default=(), max_length=12)
    source_relationship_ids: tuple[str, ...] = Field(default=(), max_length=12)

    @field_validator("continuity_statement", "identity_narrative")
    @classmethod
    def validate_self_model_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("self-model narrative cannot be blank")
        return value

    @field_validator("values", "commitments", "uncertainties")
    @classmethod
    def validate_self_model_lists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 2_000 for item in value):
            raise ValueError("self-model list entries must be nonblank and bounded")
        if len(set(value)) != len(value):
            raise ValueError("self-model list entries must be distinct")
        return value

    @field_validator(
        "source_event_ids",
        "source_memory_ids",
        "source_belief_ids",
        "source_goal_ids",
        "source_relationship_ids",
    )
    @classmethod
    def validate_self_model_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("self-model source IDs must be distinct and nonblank")
        return value


class IntrinsicThoughtGoalProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=10_000)
    priority: float = Field(ge=0, le=1)
    commitment: float = Field(ge=0, le=1)
    motive_emotion: str = Field(min_length=1, max_length=64)
    motive_target_type: str = Field(min_length=1, max_length=64)
    motive_target_id: str | None = Field(default=None, max_length=256)

    @field_validator("title", "description", "motive_emotion", "motive_target_type")
    @classmethod
    def validate_thought_goal_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("thought goal fields cannot be blank")
        return value


class IntrinsicThoughtProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    disposition: Literal["reflect", "reframe", "defer", "resolve", "abandon"]
    summary: str = Field(min_length=1, max_length=10_000)
    insight: str = Field(min_length=1, max_length=20_000)
    next_question: str | None = Field(default=None, max_length=2_000)
    source_event_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    source_memory_ids: tuple[str, ...] = Field(default=(), max_length=12)
    source_belief_ids: tuple[str, ...] = Field(default=(), max_length=12)
    source_goal_ids: tuple[str, ...] = Field(default=(), max_length=8)
    source_relationship_ids: tuple[str, ...] = Field(default=(), max_length=8)
    goal_candidate: IntrinsicThoughtGoalProposal | None = None

    @field_validator("summary", "insight")
    @classmethod
    def validate_thought_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("thought text cannot be blank")
        return value

    @field_validator("next_question")
    @classmethod
    def validate_next_question(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("thought next question cannot be blank")
        return value

    @field_validator(
        "source_event_ids",
        "source_memory_ids",
        "source_belief_ids",
        "source_goal_ids",
        "source_relationship_ids",
    )
    @classmethod
    def validate_thought_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("thought source IDs must be distinct and nonblank")
        return value

    @model_validator(mode="after")
    def validate_thought_shape(self) -> IntrinsicThoughtProposal:
        if self.disposition in {"resolve", "abandon"} and self.next_question is not None:
            raise ValueError("terminal thought cannot keep a next question")
        if self.goal_candidate is not None and self.disposition not in {"reflect", "reframe"}:
            raise ValueError("only reflective thought can propose a goal")
        return self


class ValueDevelopmentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    value_id: str | None = Field(default=None, max_length=256)
    title: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=4_000)
    disposition: Literal["form", "strengthen", "weaken", "contest", "retire"]
    weight: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    source_event_ids: tuple[str, ...] = Field(min_length=2, max_length=24)
    source_memory_ids: tuple[str, ...] = Field(default=(), max_length=16)
    source_belief_ids: tuple[str, ...] = Field(default=(), max_length=16)
    source_goal_ids: tuple[str, ...] = Field(default=(), max_length=12)
    source_relationship_ids: tuple[str, ...] = Field(default=(), max_length=12)

    @field_validator("title", "description")
    @classmethod
    def validate_value_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value development text cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_value_shape(self) -> ValueDevelopmentProposal:
        if self.disposition == "form" and self.value_id is not None:
            raise ValueError("new value cannot cite an existing value ID")
        if self.disposition != "form" and self.value_id is None:
            raise ValueError("value revision requires an existing value ID")
        return self


class MissionDevelopmentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mission_id: str | None = Field(default=None, max_length=256)
    title: str = Field(default="", max_length=256)
    statement: str = Field(default="", max_length=10_000)
    disposition: Literal["form", "revise", "adopt", "contest", "retire", "none"]
    horizon: Literal["open", "long_term", "life_direction"]
    commitment: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    source_value_ids: tuple[str, ...] = Field(default=(), max_length=12)
    source_event_ids: tuple[str, ...] = Field(default=(), max_length=24)
    source_memory_ids: tuple[str, ...] = Field(default=(), max_length=16)
    source_belief_ids: tuple[str, ...] = Field(default=(), max_length=16)
    source_goal_ids: tuple[str, ...] = Field(default=(), max_length=12)

    @field_validator("title", "statement")
    @classmethod
    def validate_mission_text(cls, value: str) -> str:
        return value

    @model_validator(mode="after")
    def validate_mission_shape(self) -> MissionDevelopmentProposal:
        if self.disposition == "none":
            if self.title or self.statement:
                raise ValueError("no mission change cannot contain mission text")
            if self.mission_id is not None or any(
                (
                    self.source_value_ids,
                    self.source_event_ids,
                    self.source_memory_ids,
                    self.source_belief_ids,
                    self.source_goal_ids,
                )
            ):
                raise ValueError("no mission change cannot cite state")
            return self
        if not self.title.strip() or not self.statement.strip():
            raise ValueError("mission development text cannot be blank")
        if self.disposition == "form" and self.mission_id is not None:
            raise ValueError("new mission cannot cite an existing mission ID")
        if self.disposition not in {"form", "none"} and self.mission_id is None:
            raise ValueError("mission revision requires an existing mission ID")
        if not self.source_value_ids or len(self.source_event_ids) < 3:
            raise ValueError("mission development requires values and three events")
        return self


class MotivationDevelopmentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    summary: str = Field(min_length=1, max_length=20_000)
    values: tuple[ValueDevelopmentProposal, ...] = Field(default=(), max_length=8)
    mission: MissionDevelopmentProposal

    @field_validator("summary")
    @classmethod
    def validate_motivation_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("motivation summary cannot be blank")
        return value


class AutonomousProjectBudgetProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_cycles: int = Field(ge=1, le=96)
    max_model_calls: int = Field(ge=1, le=48)
    max_searches: int = Field(ge=0, le=48)
    max_external_actions: int = Field(ge=0, le=24)
    max_storage_bytes: int = Field(ge=0, le=20_000_000)


class AutonomousProjectPhaseProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    phase_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=4_000)
    output_type: Literal[
        "research_note",
        "prediction_record",
        "knowledge_collection",
        "software_prototype",
        "self_experiment",
        "collaboration_request",
    ]
    acceptance_criteria: tuple[str, ...] = Field(min_length=1, max_length=8)
    dependency_keys: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("title", "objective")
    @classmethod
    def validate_project_phase_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("autonomous project phase text cannot be blank")
        return value

    @field_validator("acceptance_criteria", "dependency_keys")
    @classmethod
    def validate_project_phase_lists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 1_000 for item in value):
            raise ValueError("autonomous project phase entries must be nonblank and bounded")
        if len(set(value)) != len(value):
            raise ValueError("autonomous project phase entries must be distinct")
        return value


class AutonomousProjectFormationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    disposition: Literal["form", "wait"]
    summary: str = Field(min_length=1, max_length=20_000)
    goal_id: str | None = Field(default=None, max_length=256)
    project_type: (
        Literal[
            "research",
            "prediction",
            "knowledge",
            "software_prototype",
            "self_development",
            "collaboration",
        ]
        | None
    ) = None
    title: str | None = Field(default=None, max_length=256)
    purpose: str | None = Field(default=None, max_length=10_000)
    deliverable: str | None = Field(default=None, max_length=4_000)
    acceptance_criteria: tuple[str, ...] = Field(default=(), max_length=8)
    size_class: Literal["micro", "small"] | None = None
    estimated_duration_hours: float | None = Field(default=None, gt=0, le=168)
    budget: AutonomousProjectBudgetProposal | None = None
    phases: tuple[AutonomousProjectPhaseProposal, ...] = Field(default=(), max_length=8)
    source_event_ids: tuple[str, ...] = Field(default=(), max_length=16)
    source_value_ids: tuple[str, ...] = Field(default=(), max_length=8)
    source_mission_id: str | None = Field(default=None, max_length=256)

    @field_validator("summary")
    @classmethod
    def validate_project_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("autonomous project summary cannot be blank")
        return value

    @field_validator("acceptance_criteria", "source_event_ids", "source_value_ids")
    @classmethod
    def validate_project_lists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 1_000 for item in value):
            raise ValueError("autonomous project entries must be nonblank and bounded")
        if len(set(value)) != len(value):
            raise ValueError("autonomous project entries must be distinct")
        return value

    @model_validator(mode="after")
    def validate_project_shape(self) -> AutonomousProjectFormationProposal:
        fields = (
            self.goal_id,
            self.project_type,
            self.title,
            self.purpose,
            self.deliverable,
            self.size_class,
            self.estimated_duration_hours,
            self.budget,
        )
        if self.disposition == "wait":
            if any(item is not None for item in fields) or any(
                (
                    self.acceptance_criteria,
                    self.phases,
                    self.source_event_ids,
                    self.source_value_ids,
                    self.source_mission_id,
                )
            ):
                raise ValueError("waiting project formation cannot include a project")
            return self
        if any(item is None for item in fields):
            raise ValueError("project formation requires a complete bounded project")
        if any(not str(item).strip() for item in (self.title, self.purpose, self.deliverable)):
            raise ValueError("project formation text cannot be blank")
        if not self.acceptance_criteria or len(self.phases) < 2 or not self.source_event_ids:
            raise ValueError("project formation needs criteria, phases and causal evidence")
        phase_keys = tuple(item.phase_key for item in self.phases)
        if len(set(phase_keys)) != len(phase_keys):
            raise ValueError("project phase keys must be distinct")
        available: set[str] = set()
        for phase in self.phases:
            if phase.phase_key in phase.dependency_keys:
                raise ValueError("project phase cannot depend on itself")
            if not set(phase.dependency_keys).issubset(available):
                raise ValueError("project phase dependencies must reference earlier phases")
            available.add(phase.phase_key)
        return self


class AutonomousProjectReviewProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    project_id: str = Field(min_length=1, max_length=256)
    phase_id: str = Field(min_length=1, max_length=256)
    disposition: Literal[
        "activate",
        "continue",
        "complete_phase",
        "scale_down",
        "pause",
        "abandon",
        "request_help",
        "wait",
    ]
    summary: str = Field(min_length=1, max_length=20_000)
    reason: str = Field(min_length=1, max_length=10_000)
    evidence_event_ids: tuple[str, ...] = Field(default=(), max_length=16)
    revised_budget: AutonomousProjectBudgetProposal | None = None
    help_title: str | None = Field(default=None, max_length=256)
    help_description: str | None = Field(default=None, max_length=10_000)
    help_public_summary: str | None = Field(default=None, max_length=2_000)

    @field_validator("project_id", "phase_id", "summary", "reason")
    @classmethod
    def validate_project_review_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("autonomous project review text cannot be blank")
        return value

    @field_validator("evidence_event_ids")
    @classmethod
    def validate_project_review_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("autonomous project review evidence must be distinct and nonblank")
        return value

    @model_validator(mode="after")
    def validate_project_review_shape(self) -> AutonomousProjectReviewProposal:
        help_fields = (self.help_title, self.help_description, self.help_public_summary)
        if self.disposition == "request_help":
            if any(value is None or not value.strip() for value in help_fields):
                raise ValueError("project help request requires complete bounded content")
        elif any(value is not None for value in help_fields):
            raise ValueError("non-help project review cannot include help content")
        if self.disposition == "scale_down" and self.revised_budget is None:
            raise ValueError("project scale down requires a revised budget")
        if self.disposition != "scale_down" and self.revised_budget is not None:
            raise ValueError("only project scale down may revise its budget")
        return self


class GoalGovernanceDecisionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    goal_id: str = Field(min_length=1, max_length=256)
    disposition: Literal["activate", "maintain", "pause", "reconsider", "abandon"]
    priority: float = Field(ge=0, le=1)
    commitment: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=10_000)
    evidence_event_ids: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("goal_id", "reason")
    @classmethod
    def validate_governance_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("goal governance fields cannot be blank")
        return value

    @field_validator("evidence_event_ids")
    @classmethod
    def validate_governance_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("goal governance evidence must be distinct and nonblank")
        return value


class GoalGovernanceProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    summary: str = Field(min_length=1, max_length=20_000)
    focus_goal_id: str | None = Field(default=None, max_length=256)
    intention_title: str | None = Field(default=None, max_length=256)
    intention_description: str | None = Field(default=None, max_length=10_000)
    decisions: tuple[GoalGovernanceDecisionProposal, ...] = Field(min_length=1, max_length=12)

    @field_validator("summary")
    @classmethod
    def validate_governance_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("goal governance summary cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_focus_intention(self) -> GoalGovernanceProposal:
        intention_values = (self.intention_title, self.intention_description)
        if self.focus_goal_id is None:
            if any(value is not None for value in intention_values):
                raise ValueError("an intention requires a focus goal")
            return self
        if any(value is None or not value.strip() for value in intention_values):
            raise ValueError("a focus goal requires a complete intention")
        return self


class ActionDeliberationProposal(BaseModel):
    """A bounded choice to investigate one authorized source or deliberately wait."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    summary: str = Field(min_length=1, max_length=20_000)
    disposition: Literal["investigate", "wait"]
    goal_id: str | None = Field(default=None, max_length=256)
    source_id: str | None = Field(default=None, max_length=256)
    strategy_title: str | None = Field(default=None, max_length=256)
    expected_observation: str | None = Field(default=None, max_length=10_000)
    reason: str = Field(min_length=1, max_length=10_000)
    evidence_event_ids: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator(
        "summary",
        "reason",
    )
    @classmethod
    def validate_deliberation_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("action deliberation fields cannot be blank")
        return value

    @field_validator("evidence_event_ids")
    @classmethod
    def validate_deliberation_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("action deliberation evidence must be distinct and nonblank")
        return value

    @model_validator(mode="after")
    def validate_deliberation_choice(self) -> ActionDeliberationProposal:
        action_fields = (
            self.goal_id,
            self.source_id,
            self.strategy_title,
            self.expected_observation,
        )
        if self.disposition == "wait":
            if any(value is not None for value in action_fields):
                raise ValueError("wait deliberation cannot include an action")
            return self
        if any(value is None or not value.strip() for value in action_fields):
            raise ValueError("investigation deliberation requires a complete action")
        return self


class SemanticMemoryIntegrationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    disposition: Literal["integrate", "wait"]
    operation: Literal["merge", "supersede", "contradict"] | None = None
    source_memory_ids: tuple[str, ...] = Field(default=(), max_length=4)
    synthesis: str | None = Field(default=None, max_length=20_000)
    confidence: float = Field(default=0, ge=0, le=1)
    evidence_event_ids: tuple[str, ...] = Field(default=(), max_length=16)
    reason: str = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_shape(self) -> SemanticMemoryIntegrationProposal:
        if self.disposition == "wait":
            if self.operation is not None or self.source_memory_ids or self.synthesis is not None:
                raise ValueError("wait cannot contain a memory integration")
            return self
        if self.operation is None or self.synthesis is None or not self.synthesis.strip():
            raise ValueError("integration proposal is incomplete")
        if len(self.source_memory_ids) < 2 or len(set(self.source_memory_ids)) != len(
            self.source_memory_ids
        ):
            raise ValueError("integration requires distinct source memories")
        if not self.evidence_event_ids or len(set(self.evidence_event_ids)) != len(
            self.evidence_event_ids
        ):
            raise ValueError("integration requires distinct evidence events")
        return self


@dataclass(frozen=True)
class ValidatedCognition:
    summary: str
    appraisal: AppraisalInput
    affect_impulses: tuple[AffectImpulse, ...]
    claims: tuple[ClaimProposal, ...]
    predictions: tuple[PredictionProposal, ...]
    goals: tuple[GoalCandidate, ...]


class CognitionValidationError(ValueError):
    pass
