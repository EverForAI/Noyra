from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from noyra.world import FetchedDocument

CapabilityType = Literal[
    "web_read", "filesystem_read", "filesystem_write", "publish", "message", "wallet"
]


class CapabilityGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    capability_type: CapabilityType
    scope: dict[str, Any]
    issuer: str = Field(min_length=1, max_length=256)
    rate_limit_per_hour: int = Field(ge=0, le=100_000)
    side_effect: bool
    # Kept only to read/hash legacy rows; true grants are rejected by the store.
    requires_approval: bool = Field(
        default=False,
        description="Legacy compatibility flag; per-use approval is unsupported.",
    )
    expires_at: str | None = Field(default=None, max_length=64)

    @field_validator("issuer")
    @classmethod
    def nonblank_issuer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("capability issuer cannot be blank")
        return value


@dataclass(frozen=True)
class CapabilityGrantRecord:
    grant_id: str
    subject_id: str
    capability_type: str
    scope: dict[str, Any]
    issuer: str
    rate_limit_per_hour: int
    side_effect: bool
    requires_approval: bool
    status: str
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    revoke_reason: str | None


@dataclass(frozen=True)
class ToolResult:
    action_id: str
    status: str
    content: str | None
    bytes_written: int


@dataclass(frozen=True)
class WebToolResult:
    action_id: str
    status: str
    document: FetchedDocument | None
