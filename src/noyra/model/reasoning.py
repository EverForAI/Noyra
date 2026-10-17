"""Conservative capability profiles for optional reasoning controls."""

from __future__ import annotations

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
        return value  # type: ignore[return-value]


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
