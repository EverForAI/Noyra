"""Conservative capability profiles for optional reasoning controls."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

ReasoningEffort = Literal["low", "medium", "high", "xhigh"]
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = ("low", "medium", "high", "xhigh")


@dataclass(frozen=True)
class ReasoningProfile:
    supported: bool
    efforts: tuple[ReasoningEffort, ...]
    parameter: str | None
    version: int = 1

    def normalize(self, value: str | None) -> ReasoningEffort | None:
        if value is None or not self.supported:
            return None
        if value not in self.efforts:
            raise ValueError("reasoning effort is not supported by this model")
        return value


def reasoning_profile_payload(
    model: str,
    provider_type: str = "openai_compatible",
    *,
    selected: ReasoningEffort | None = None,
) -> dict[str, object]:
    """Return the server-owned capability contract used by the management UI."""

    profile = reasoning_profile(model, provider_type)
    revision_input = "|".join(
        (
            str(profile.version),
            provider_type,
            model.strip().casefold(),
            "1" if profile.supported else "0",
            ",".join(profile.efforts),
            profile.parameter or "",
        )
    )
    return {
        "supported": profile.supported,
        "efforts": list(profile.efforts),
        "parameter": profile.parameter,
        "version": profile.version,
        "revision": hashlib.sha256(revision_input.encode("utf-8")).hexdigest()[:16],
        "selected": selected if profile.supported else None,
    }


def reasoning_profile(model: str, provider_type: str = "openai_compatible") -> ReasoningProfile:
    normalized = model.strip().casefold()
    supported = provider_type == "openai_compatible" and normalized.startswith(
        ("o1", "o3", "o4", "gpt-5", "codex")
    )
    return ReasoningProfile(
        supported,
        REASONING_EFFORTS if supported else (),
        "reasoning_effort" if supported else None,
    )


def normalize_reasoning_effort(
    model: str, value: str | None, provider_type: str = "openai_compatible"
) -> ReasoningEffort | None:
    return reasoning_profile(model, provider_type).normalize(value)
