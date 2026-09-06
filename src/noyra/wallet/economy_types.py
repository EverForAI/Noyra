from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .types import SQLITE_INT64_MAX, canonical_evm_address, canonical_timestamp

_EVM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_AMOUNT = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")

BountyStatus = Literal["draft", "published", "closed", "cancelled", "expired"]
SubmissionStatus = Literal["submitted", "accepted", "rejected", "withdrawn", "expired"]
PaymentMode = Literal["disabled", "conditional_confirmation", "automatic"]
PaymentOrderStatus = Literal[
    "pending_policy",
    "awaiting_confirmation",
    "reserved",
    "rejected",
    "cancelled",
    "expired",
    "signing",
    "broadcast",
    "unknown",
    "confirmed",
    "failed",
    "refunded",
]


def _identifier(value: str, label: str) -> str:
    value = value.strip()
    if not _EVM_ID.fullmatch(value):
        raise ValueError(f"wallet {label} is invalid")
    return value


def canonical_amount(value: str) -> str:
    if not isinstance(value, str) or _AMOUNT.fullmatch(value) is None:
        raise ValueError("wallet amount must be a canonical integer string")
    parsed = int(value)
    if parsed > SQLITE_INT64_MAX:
        raise ValueError("wallet amount exceeds SQLite integer bound")
    return value


class BountyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=20_000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=32)
    network_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    reward_amount: str = Field(min_length=1, max_length=19)
    opens_at: str
    expires_at: str
    max_submissions: int = Field(ge=1, le=10_000)
    reward_slots: int = Field(ge=1, le=10_000)
    project_id: str | None = Field(default=None, max_length=128)
    goal_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("title", "description")
    @classmethod
    def text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("wallet bounty text cannot be blank")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def criteria(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 2_000 for item in cleaned) or len(set(cleaned)) != len(
            cleaned
        ):
            raise ValueError("wallet bounty acceptance criteria are invalid")
        return cleaned

    @field_validator("network_id", "asset_id", "idempotency_key")
    @classmethod
    def ids(cls, value: str) -> str:
        return _identifier(value, "identifier")

    @field_validator("project_id", "goal_id")
    @classmethod
    def optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, "provenance identifier")

    @field_validator("reward_amount")
    @classmethod
    def amount(cls, value: str) -> str:
        normalized = canonical_amount(value)
        if normalized == "0":
            raise ValueError("wallet bounty reward must be positive")
        return normalized

    @field_validator("opens_at", "expires_at")
    @classmethod
    def times(cls, value: str) -> str:
        return canonical_timestamp(value)

    @model_validator(mode="after")
    def valid_window(self) -> BountyInput:
        if self.expires_at <= self.opens_at:
            raise ValueError("wallet bounty expiry must be after opening")
        if self.project_id is None and self.goal_id is None:
            raise ValueError("wallet bounty requires project or goal provenance")
        if self.reward_slots > self.max_submissions:
            raise ValueError("wallet bounty reward slots exceed submission limit")
        return self


class SubmissionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    counterparty: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=20_000)
    evidence: list[str] = Field(default_factory=list, max_length=32)
    recipient_address: str = Field(min_length=42, max_length=42)
    idempotency_key: str = Field(min_length=1, max_length=128)
    consent_version: int = Field(ge=1, le=1000)

    @field_validator("counterparty", "content")
    @classmethod
    def text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("wallet submission text cannot be blank")
        return value

    @field_validator("evidence")
    @classmethod
    def evidence_values(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 2_000 for item in cleaned):
            raise ValueError("wallet submission evidence is invalid")
        return cleaned

    @field_validator("recipient_address")
    @classmethod
    def address(cls, value: str) -> str:
        return canonical_evm_address(value)

    @field_validator("idempotency_key")
    @classmethod
    def key(cls, value: str) -> str:
        return _identifier(value, "submission idempotency key")


class PaymentPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    mode: PaymentMode = "disabled"
    allowed_network_ids: list[str] = Field(default_factory=list, max_length=256)
    allowed_asset_ids: list[str] = Field(default_factory=list, max_length=256)
    per_order_limit: str = "0"
    daily_limit: str = "0"
    monthly_limit: str = "0"
    daily_order_limit: int = Field(default=0, ge=0, le=1_000_000)
    monthly_order_limit: int = Field(default=0, ge=0, le=1_000_000)
    min_balance: str = "0"
    max_observation_age_seconds: int = Field(default=0, ge=0, le=2_592_000)
    automatic_max_amount: str = "0"
    anomaly_block: bool = True
    emergency_paused: bool = False

    @field_validator("allowed_network_ids", "allowed_asset_ids")
    @classmethod
    def allowlist(cls, value: list[str]) -> list[str]:
        cleaned = [_identifier(item, "allow-list identifier") for item in value]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("wallet policy allow-list contains duplicates")
        return cleaned

    @field_validator(
        "per_order_limit", "daily_limit", "monthly_limit", "min_balance", "automatic_max_amount"
    )
    @classmethod
    def amounts(cls, value: str) -> str:
        return canonical_amount(value)


@dataclass(frozen=True)
class BountyRecord:
    bounty_id: str
    subject_id: str
    project_id: str | None
    goal_id: str | None
    idempotency_key: str
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    network_id: str
    asset_id: str
    reward_amount: str
    opens_at: str
    expires_at: str
    max_submissions: int
    reward_slots: int
    status: str
    state_hash: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SubmissionRecord:
    submission_id: str
    bounty_id: str
    subject_id: str
    counterparty: str
    content: str
    evidence: tuple[str, ...]
    recipient_address: str
    idempotency_key: str
    consent_version: int
    status: str
    decision_reason: str | None
    created_at: str
    decided_at: str | None


@dataclass(frozen=True)
class PaymentPolicyRecord:
    subject_id: str
    mode: str
    allowed_network_ids: tuple[str, ...]
    allowed_asset_ids: tuple[str, ...]
    per_order_limit: str
    daily_limit: str
    monthly_limit: str
    daily_order_limit: int
    monthly_order_limit: int
    min_balance: str
    max_observation_age_seconds: int
    automatic_max_amount: str
    anomaly_block: bool
    emergency_paused: bool
    policy_version: int
    updated_at: str


@dataclass(frozen=True)
class PaymentOrderRecord:
    order_id: str
    subject_id: str
    bounty_id: str
    submission_id: str
    network_id: str
    asset_id: str
    recipient_address: str
    amount: str
    payment_mode: str
    policy_version: int
    idempotency_key: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class LedgerEntryRecord:
    entry_id: str
    journal_id: str
    subject_id: str
    order_id: str
    account: str
    direction: str
    amount: str
    created_at: str


@dataclass(frozen=True)
class LedgerJournalRecord:
    journal_id: str
    subject_id: str
    order_id: str
    network_id: str
    asset_id: str
    journal_type: str
    amount: str
    created_at: str
    entries: tuple[LedgerEntryRecord, ...]


@dataclass(frozen=True)
class LedgerBalance:
    account: str
    debit: str
    credit: str
    net: str
    # Unfiltered ledger projections remain unambiguous when more than one
    # network or asset is configured.  Existing callers may ignore these
    # optional dimensions.
    network_id: str | None = None
    asset_id: str | None = None
