from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from noyra.cognition.epistemic import EpistemicReviewProposal
from noyra.cognition.execution import SoftwarePrototypeProposal
from noyra.cognition.research import AutonomousResearch, ModelSearchResults
from noyra.cognition.types import (
    ActionDeliberationProposal,
    AutonomousProjectFormationProposal,
    AutonomousProjectReviewProposal,
    CognitionProposal,
    GoalGovernanceProposal,
    InteractionCognitionProposal,
    IntrinsicThoughtProposal,
    MotivationDevelopmentProposal,
    RelationshipSocialProposal,
    SelfModelProposal,
    SemanticMemoryIntegrationProposal,
)
from noyra.migration.runtime import MigrationJudgment
from noyra.model.config import OpenAICompatibleSettings
from noyra.model.openai_compatible import OpenAICompatibleProvider
from noyra.model.types import CompletionRequest, ModelMessage
from noyra.model.wire import strict_output_schema
from noyra.research.types import ResearchAssessmentProposal, ResearchPlanProposal
from noyra.sleep.types import SleepReflectionPlan


def assert_strict_schema(value: Any) -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            assert set(value["required"]) == set(value["properties"])
            assert value["additionalProperties"] is False
        assert "default" not in value
        for child in value.values():
            assert_strict_schema(child)
    elif isinstance(value, list):
        for child in value:
            assert_strict_schema(child)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        CognitionProposal,
        ActionDeliberationProposal,
        EpistemicReviewProposal,
        SoftwarePrototypeProposal,
        GoalGovernanceProposal,
        InteractionCognitionProposal,
        SemanticMemoryIntegrationProposal,
        MotivationDevelopmentProposal,
        AutonomousProjectReviewProposal,
        AutonomousProjectFormationProposal,
        SleepReflectionPlan,
        ResearchPlanProposal,
        ModelSearchResults,
        ResearchAssessmentProposal,
        SelfModelProposal,
        RelationshipSocialProposal,
        IntrinsicThoughtProposal,
        MigrationJudgment,
    ],
)
async def test_production_output_schema_is_accepted_by_strict_peer(output: type[BaseModel]) -> None:
    original = output.model_json_schema()
    before = copy.deepcopy(original)
    captured: list[dict[str, Any]] = []

    async def peer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert_strict_schema(body["response_format"]["json_schema"]["schema"])
        captured.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(peer)) as client:
        probe = await OpenAICompatibleProvider.probe_unstored(
            base_url="https://models.example/v1",
            model="gpt-4.1-mini",
            api_key=SecretStr("test-fixture"),
            client=client,
        )
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                base_url="https://models.example/v1",
                model="gpt-4.1-mini",
                api_key=SecretStr("fixture"),
            ),
            client=client,
        )
        await provider.complete(
            CompletionRequest(
                model="gpt-4.1-mini",
                messages=(ModelMessage(role="user", content="test"),),
                max_output_tokens=1000,
                temperature=0.2,
                schema_name=output.__name__,
                output_schema=original,
            )
        )
    assert probe["ok"]
    assert len(captured) == 2
    assert original == before == output.model_json_schema()
    if output is InteractionCognitionProposal:
        response = captured[-1]["response_format"]["json_schema"]["schema"]["properties"][
            "response"
        ]
        assert {part.get("type") for part in response["anyOf"]} == {"string", "null"}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["o1", "o3-mini", "o4-mini", "gpt-5-mini", "gpt-5.1"])
async def test_reasoning_probe_and_completion_use_same_parameter_contract(model: str) -> None:
    bodies: list[dict[str, Any]] = []

    async def peer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "max_tokens" not in body
        assert "temperature" not in body
        assert body["max_completion_tokens"] > 8
        assert body["reasoning_effort"] == "high"
        bodies.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(peer)) as client:
        await OpenAICompatibleProvider.probe_unstored(
            base_url="https://models.example/v1",
            model=model,
            api_key=SecretStr("fixture"),
            reasoning_effort="high",
            client=client,
        )
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                base_url="https://models.example/v1",
                model=model,
                api_key=SecretStr("fixture"),
            ),
            client=client,
        )
        await provider.complete(
            CompletionRequest(
                model=model,
                messages=(ModelMessage(role="user", content="test"),),
                max_output_tokens=1024,
                temperature=0.2,
                schema_name="Test",
                output_schema={"type": "object"},
                reasoning_effort="high",
            )
        )
    assert len(bodies) == 2
    assert bodies[-1]["max_completion_tokens"] == 1024


@pytest.mark.parametrize("model", ["gpt-4.1", "deepseek-chat", "provider-alias"])
def test_legacy_and_verified_alias_parameter_encoding_is_preserved(model: str) -> None:
    payload = OpenAICompatibleProvider._request_payload(
        CompletionRequest(
            model=model,
            messages=(ModelMessage(role="user", content="test"),),
            max_output_tokens=512,
            temperature=0.2,
            schema_name="Test",
            output_schema={"type": "object"},
            reasoning_effort="high" if model == "provider-alias" else None,
        )
    )
    assert payload["max_tokens"] == 512
    assert payload["temperature"] == 0.2
    assert "max_completion_tokens" not in payload


def test_wire_conversion_preserves_recursive_references_and_explicit_dictionary_contract() -> None:
    class Node(BaseModel):
        label: str | None = None
        children: list[Node] = []

    schema = strict_output_schema(Node.model_json_schema())
    assert_strict_schema(schema)
    assert schema["$ref"] == "#/$defs/Node"
    mapping = {"type": "object", "additionalProperties": {"type": "string"}}
    assert strict_output_schema(mapping)["additionalProperties"] == {"type": "string"}


def test_model_search_wire_shape_keeps_legacy_response_readable() -> None:
    legacy = ModelSearchResults.model_validate_json(
        '{"results":[{"url":"https://example.org/source","legacy_score":0.5}]}'
    )
    assert legacy.results[0]["legacy_score"] == 0.5
    result = AutonomousResearch._validated_results(legacy.results)
    assert len(result) == 1
    assert result[0].url == "https://example.org/source"
    assert result[0].title == result[0].url
    assert result[0].snippet == ""
