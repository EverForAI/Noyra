from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .economy_types import canonical_amount
from .types import canonical_evm_address, canonical_timestamp

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

RewardWorkflowStatus = Literal[
    "awaiting_publication",
    "open",
    "closed",
    "cancelled",
    "manual_intervention",
]
RewardVerificationStatus = Literal[
    "pending_verification", "accepted", "rejected", "manual_intervention"
]


def _identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if _IDENTIFIER.fullmatch(cleaned) is None:
        raise ValueError(f"reward {label} is invalid")
    return cleaned


class RewardWorkflowInput(BaseModel):
    """Bounded supervisor input for one autonomous assistance bounty."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    assistance_request_id: str = Field(min_length=1, max_length=128)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=32)
    network_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    reward_amount: str = Field(min_length=1, max_length=19)
    opens_at: str
    expires_at: str
    max_submissions: int = Field(ge=1, le=10_000)
    reward_slots: int = Field(ge=1, le=10_000)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("assistance_request_id", "network_id", "asset_id", "idempotency_key")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return _identifier(value, "workflow identifier")

    @field_validator("acceptance_criteria")
    @classmethod
    def criteria(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 2_000 for item in cleaned) or len(set(cleaned)) != len(
            cleaned
        ):
            raise ValueError("reward acceptance criteria are invalid")
        return cleaned

    @field_validator("reward_amount")
    @classmethod
    def amount(cls, value: str) -> str:
        value = canonical_amount(value)
        if value == "0":
            raise ValueError("reward amount must be positive")
        return value

    @field_validator("opens_at", "expires_at")
    @classmethod
    def timestamps(cls, value: str) -> str:
        return canonical_timestamp(value)

    @model_validator(mode="after")
    def window(self) -> RewardWorkflowInput:
        if self.expires_at <= self.opens_at:
            raise ValueError("reward workflow expiry must be after opening")
        if self.reward_slots > self.max_submissions:
            raise ValueError("reward workflow slots exceed submission limit")
        return self


class RewardPublicSubmissionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    counterparty: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=20_000)
    evidence: list[str] = Field(default_factory=list, max_length=32)
    recipient_address: str = Field(min_length=42, max_length=42)
    network_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    consent_version: int = Field(ge=1, le=1_000)

    @field_validator("counterparty", "content")
    @classmethod
    def text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reward submission text cannot be blank")
        return cleaned

    @field_validator("evidence")
    @classmethod
    def evidence_values(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 2_000 for item in cleaned):
            raise ValueError("reward submission evidence is invalid")
        return cleaned

    @field_validator("recipient_address")
    @classmethod
    def address(cls, value: str) -> str:
        return canonical_evm_address(value)

    @field_validator("network_id", "idempotency_key")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return _identifier(value, "submission identifier")


class RewardInboundSubmissionPayload(BaseModel):
    """Exact JSON envelope a bound human may send over a native channel."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["noyra_reward_submission"]
    workflow_id: str = Field(min_length=1, max_length=128)
    evidence: list[str] = Field(default_factory=list, max_length=32)
    recipient_address: str = Field(min_length=42, max_length=42)
    network_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    consent_version: int = Field(ge=1, le=1_000)

    @field_validator("workflow_id", "network_id", "idempotency_key")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return _identifier(value, "inbound identifier")

    @field_validator("recipient_address")
    @classmethod
    def address(cls, value: str) -> str:
        return canonical_evm_address(value)

    @field_validator("evidence")
    @classmethod
    def evidence_values(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 2_000 for item in cleaned):
            raise ValueError("reward inbound evidence is invalid")
        return cleaned


class RewardEvidenceDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    accepted: bool
    criteria_results: list[bool] = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=2_000)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("reason")
    @classmethod
    def reason_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reward decision reason cannot be blank")
        return cleaned

    @field_validator("idempotency_key")
    @classmethod
    def identifier(cls, value: str) -> str:
        return _identifier(value, "decision identifier")


class RewardIncidentResolutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    resolution: str = Field(min_length=1, max_length=2_000)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("resolution")
    @classmethod
    def resolution_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reward incident resolution cannot be blank")
        return cleaned

    @field_validator("idempotency_key")
    @classmethod
    def identifier(cls, value: str) -> str:
        return _identifier(value, "incident resolution identifier")


@dataclass(frozen=True)
class RewardWorkflowRecord:
    workflow_id: str
    subject_id: str
    assistance_request_id: str
    project_id: str
    phase_id: str
    goal_id: str
    post_id: str
    bounty_id: str
    idempotency_key: str
    status: str
    manual_reason: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RewardSubmissionLinkRecord:
    submission_id: str
    workflow_id: str
    subject_id: str
    source_type: str
    inbound_event_id: str | None
    interaction_id: str | None
    claimed_network_id: str
    source_content_hash: str
    verification_status: str
    criteria_results: tuple[bool, ...] | None
    verification_reason: str | None
    verified_by: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RewardIncidentRecord:
    incident_id: str
    subject_id: str
    workflow_id: str
    submission_id: str | None
    execution_id: str | None
    kind: str
    idempotency_key: str
    status: str
    reason: str
    resolution: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RewardAdvanceResult:
    workflow: RewardWorkflowRecord
    submission: RewardSubmissionLinkRecord | None
    order_status: str | None
    execution_id: str | None
    execution_status: str | None
    incident: RewardIncidentRecord | None
