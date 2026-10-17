from __future__ import annotations

import json
from typing import Any

import pytest

from noyra.model.openai_compatible import OpenAICompatibleProvider
from noyra.model.reasoning import normalize_reasoning_effort, reasoning_profile
from noyra.model.types import CompletionRequest, ModelMessage


def test_known_reasoning_models_expose_bounded_efforts() -> None:
    profile = reasoning_profile("gpt-5-mini")
    assert profile.supported is True
    assert profile.efforts == ("low", "medium", "high", "xhigh")
    assert profile.parameter == "reasoning_effort"
    assert normalize_reasoning_effort("gpt-5-mini", "high") == "high"


def test_unknown_models_fail_closed_without_reasoning() -> None:
    profile = reasoning_profile("deepseek-chat")
    assert profile.supported is False
    assert normalize_reasoning_effort("deepseek-chat", "high") is None


def test_known_model_rejects_unknown_effort() -> None:
    with pytest.raises(ValueError, match="not supported"):
        normalize_reasoning_effort("o3-mini", "extreme")


def test_provider_payload_only_contains_optional_field_when_selected() -> None:
    base: dict[str, Any] = dict(
        model="gpt-5-mini",
        messages=(ModelMessage(role="user", content="hello"),),
        max_output_tokens=8,
        temperature=0,
        schema_name="Output",
        output_schema={"type": "object"},
    )
    without = OpenAICompatibleProvider._request_payload(CompletionRequest(**base))
    with_effort = OpenAICompatibleProvider._request_payload(
        CompletionRequest(**base, reasoning_effort="high")
    )
    assert "reasoning_effort" not in without
    assert with_effort["reasoning_effort"] == "high"
    json.dumps(with_effort)
