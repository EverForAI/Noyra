from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

InteractionDirection = Literal["incoming", "outgoing"]
InteractionKind = Literal["human_message", "subject_message", "help_request"]
InteractionStatus = Literal[
    "offered", "accepted", "rejected", "deferred", "silent", "sent", "expired"
]
DecisionDisposition = Literal["accepted", "rejected", "deferred", "silent"]
PublicPostKind = Literal["post", "help_request"]
PublicPostStatus = Literal["draft", "pending_review", "published", "rejected", "archived"]


class InteractionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    disposition: DecisionDisposition
    rationale: str = Field(min_length=1, max_length=10_000)

    @field_validator("rationale")
    @classmethod
    def nonblank_rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("interaction rationale cannot be blank")
        return value


@dataclass(frozen=True)
class InteractionRecord:
    interaction_id: str
    subject_id: str
    direction: str
    kind: str
    channel: str
    counterparty: str
    content: str
    related_interaction_id: str | None
    idempotency_key: str
    status: str
    rationale: str | None
    created_at: str
    decided_at: str | None


@dataclass(frozen=True)
class PublicDiaryEntry:
    entry_id: str
    subject_id: str
    source_sleep_id: str
    title: str
    body: str
    created_at: str


class PublicPostInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: PublicPostKind
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=20_000)
    author_label: str = Field(default="访客", min_length=1, max_length=128)

    @field_validator("title", "content", "author_label")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("public post text cannot be blank")
        return value.strip()


@dataclass(frozen=True)
class PublicPostRecord:
    post_id: str
    subject_id: str
    kind: str
    title: str
    content: str
    author_label: str
    author_provenance: str
    status: str
    created_at: str
    updated_at: str
    published_at: str | None
