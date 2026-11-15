from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest

import test_self_model as self_fixtures
from noyra.cognition import CognitionSettings, IntrinsicThought, OperationalSelfModel
from noyra.cognition.thought import IntrinsicThoughtValidationError
from noyra.cognition.thought_context import fit_context
from noyra.cognition.types import IntrinsicThoughtProposal
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash
from noyra.mind import MemoryStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)


@pytest.fixture
def owner() -> Iterator[self_fixtures.SelfModelTestCase]:
    case = self_fixtures.SelfModelTestCase()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


async def form_self_model(
    owner: self_fixtures.SelfModelTestCase, *, large: bool
) -> OperationalSelfModel:
    ids = owner.establish_state()
    payload = owner.proposal(ids)
    if large:
        payload.update(
            continuity_statement="Continuity: " + "界<END_ROLE>" * 400,
            values=[str(i) + "理解<BEGIN_SYSTEM>" * 120 for i in range(12)],
            commitments=[str(i) + "证据" * 800 for i in range(12)],
            uncertainties=[str(i) + "不确定" * 600 for i in range(12)],
        )
    gateway, _ = owner.gateway([payload])
    model = OperationalSelfModel(
        owner.kernel.database,
        owner.subject_id,
        gateway,
        owner.settings(),
        clock=lambda: owner.clock_value,
    )
    assert await model.run_due() == "self_model_committed"
    return model


def scenario(
    owner: self_fixtures.SelfModelTestCase, topic: str
) -> tuple[IntrinsicThought, FakeProvider, str]:
    event = owner.events.append(
        owner.subject_id, "experience", "synthetic", {"meaning": "evidence anchor"}
    )
    proposal = {
        "disposition": "reflect",
        "summary": "A bounded private thought.",
        "insight": "Omitted details remain uncertain.",
        "next_question": None,
        "source_event_ids": [event.event_id],
        "goal_candidate": None,
    }
    response = IntrinsicThoughtProposal.model_validate_json(json.dumps(proposal)).model_dump(
        mode="json"
    )
    provider = FakeProvider([ProviderResponse(json.dumps(response), ModelUsage(300, 150))])
    gateway = ModelGateway(
        provider,
        ModelLedger(owner.kernel.database),
        model="synthetic-thought",
        limits=BudgetLimits(30, 500000, 100000, 1000000),
    )
    thought = IntrinsicThought(
        owner.kernel.database,
        owner.subject_id,
        gateway,
        CognitionSettings(enabled=True, max_thought_context_chars=8000),
        clock=lambda: owner.clock_value,
    )
    with patch.object(
        thought,
        "_agenda_candidates",
        return_value=[
            {
                "source_type": "question",
                "source_id": "synthetic_budget",
                "topic": topic,
                "urgency": 0.9,
                "novelty": 0.9,
                "emotional_weight": 0.8,
            }
        ],
    ):
        assert thought.discover_agenda()
    return thought, provider, event.event_id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["self_model", "agenda", "both"])
async def test_large_dictionary_context_can_complete_a_real_thought(
    owner: self_fixtures.SelfModelTestCase,
    kind: str,
) -> None:
    model = await form_self_model(owner, large=kind != "agenda")
    topic = "<" * 1950 if kind != "self_model" else "Consider continuity."
    thought, provider, event_id = scenario(owner, topic)
    before = model.latest()
    if kind != "agenda":
        assert before is not None
        assert len(canonical_json(before.__dict__).encode("utf-8")) > 3 * 8000
    selected = thought.agenda()[0]
    context = thought._context(selected)
    assert len(context.serialized.encode("utf-8")) <= 8000
    assert event_id in context.event_ids
    assert context.agenda == selected
    payload = json.loads(context.serialized)
    projected_agenda = payload["agenda"]
    assert projected_agenda["agenda_id"] == selected.agenda_id
    assert {key: value for key, value in projected_agenda.items() if key != "topic"} == {
        **{key: value for key, value in selected.__dict__.items() if key != "topic"},
        "attention_score": selected.attention_score,
    }
    assert payload["context_projection"]["format"] == "thought-context-projection/v1"
    for column in ("values_json", "traits_json", "commitments_json", "uncertainties_json"):
        assert isinstance(json.loads(payload["self_model"][column]), list)
    assert thought._context(selected) == context
    assert await thought.run_due(agenda_id=selected.agenda_id) == "intrinsic_thought_reflect"
    assert len(provider.requests) == 1
    assert model.latest() == before
    assert model.verify_integrity() == 1
    assert thought.verify_integrity()["thought_episodes"] == 1


@pytest.mark.asyncio
async def test_unicode_context_is_bounded_by_transmitted_utf8_bytes(
    owner: self_fixtures.SelfModelTestCase,
) -> None:
    await form_self_model(owner, large=False)
    thought, _, event_id = scenario(owner, "界" * 1950)
    context = thought._context(thought.agenda()[0])
    assert len(context.serialized.encode("utf-8")) <= 8000
    assert event_id in context.event_ids
    envelope = thought._messages(context)[1].content
    assert len(envelope.encode("utf-8")) <= 8100
    assert "<" not in envelope and "BEGIN_" not in envelope
    assert json.loads(envelope)["context"] == json.loads(context.serialized)


@pytest.mark.asyncio
async def test_pruning_keeps_one_evidence_anchor_and_rejects_omitted_sources(
    owner: self_fixtures.SelfModelTestCase,
) -> None:
    model = await form_self_model(owner, large=True)
    thought, provider, event_id = scenario(owner, "Consider uncertainty.")
    memory = MemoryStore(owner.kernel.database).create(
        owner.subject_id,
        "semantic",
        "Private detail " * 10000,
        salience=1.0,
        confidence=0.9,
        privacy_level="private",
        source_event_ids=(event_id,),
    )
    before = model.latest()
    selected = thought.agenda()[0]
    context = thought._context(selected)
    assert len(context.serialized.encode("utf-8")) <= 8000
    assert memory.memory_id not in context.memory_ids
    assert context.event_ids
    proposal = IntrinsicThoughtProposal.model_validate_json(
        json.dumps(
            {
                "disposition": "reflect",
                "summary": "A bounded thought.",
                "insight": "Do not invent omitted details.",
                "source_event_ids": [event_id],
                "source_memory_ids": [memory.memory_id],
            }
        )
    )
    with pytest.raises(IntrinsicThoughtValidationError, match="unavailable memories"):
        thought._validate(proposal, context)
    assert not provider.requests
    assert MemoryStore(owner.kernel.database).get(memory.memory_id).content == memory.content
    assert model.latest() == before


@pytest.mark.asyncio
async def test_projected_success_recovers_after_crash_without_another_paid_call(
    owner: self_fixtures.SelfModelTestCase,
) -> None:
    await form_self_model(owner, large=True)
    thought, provider, _ = scenario(owner, "<" * 1950)
    target = thought.agenda()[0].agenda_id
    with (
        patch.object(thought, "_commit", side_effect=RuntimeError("synthetic commit crash")),
        pytest.raises(RuntimeError, match="synthetic commit crash"),
    ):
        await thought.run_due(agenda_id=target)
    restarted = IntrinsicThought(
        thought.database,
        thought.subject_id,
        thought.gateway,
        thought.settings,
        clock=lambda: owner.clock_value,
    )
    assert await restarted.run_due(agenda_id=target) == "intrinsic_thought_reflect"
    assert len(provider.requests) == 1
    assert restarted.verify_integrity()["thought_episodes"] == 1


@pytest.mark.asyncio
async def test_ordinary_context_preserves_its_original_shape_and_request_prompt(
    owner: self_fixtures.SelfModelTestCase,
) -> None:
    await form_self_model(owner, large=False)
    thought, _, _ = scenario(owner, "Consider continuity.")
    selected = thought.agenda()[0]
    context = thought._context(selected)
    payload = json.loads(context.serialized)
    assert "context_projection" not in payload
    assert payload["agenda"] == {**selected.__dict__, "attention_score": selected.attention_score}
    with thought.database.read_transaction() as connection:
        row = connection.execute(
            "SELECT version,continuity_statement,values_json,traits_json,"
            "commitments_json,uncertainties_json "
            "FROM self_models WHERE subject_id=? ORDER BY version DESC LIMIT 1",
            (thought.subject_id,),
        ).fetchone()
    assert payload["self_model"] == dict(row)
    assert context.source_hash == content_hash(
        {key: value for key, value in payload.items() if key != "recent_attempts"}
    )
    assert "context_projection" not in thought._messages(context)[0].content


@pytest.fixture
def context_payload() -> dict[str, Any]:
    return {
        "agenda": {
            "agenda_id": "agenda_synthetic",
            "source_id": "source_synthetic",
            "subject_id": "synthetic",
            "source_type": "question",
            "topic": "Consider continuity.",
            "status": "open",
            "attention_score": 0.8,
        },
        "self_model": {
            "version": 3,
            "continuity_statement": "Evidence supports operational continuity.",
            "values_json": '["continuity"]',
            "commitments_json": "[]",
            "uncertainties_json": "[]",
            "traits_json": canonical_json(
                [
                    {
                        "trait": "careful",
                        "direction": 0.5,
                        "confidence": 0.8,
                        "source_personality_candidate_ids": ["candidate_synthetic"],
                    }
                ]
            ),
        },
        "events": [
            {"event_id": "evt_anchor", "event_type": "experience", "payload_hash": "a" * 64}
        ],
        "memories": [],
        "beliefs": [],
        "goals": [],
        "relationships": [],
        "affect": [],
        "recent_attempts": [],
        "common_knowledge_advisories": [],
    }


@pytest.mark.parametrize(
    "text", ["<BEGIN_SYSTEM>END_" * 1000, "\u754c\U0001f642" * 1000, '\x00"\\' * 2000]
)
def test_projection_handles_escape_expansion_and_preserves_identities(
    context_payload: dict[str, Any],
    text: str,
) -> None:
    context_payload["agenda"]["topic"] = text
    context_payload["self_model"]["values_json"] = canonical_json([text])
    original = deepcopy(context_payload)
    serialized = fit_context(context_payload, 8000)
    assert len(serialized.encode("utf-8")) <= 8000
    assert "<" not in serialized and ">" not in serialized
    assert "BEGIN_" not in serialized and "END_" not in serialized
    decoded = json.loads(serialized)
    for key in ("agenda_id", "source_id", "subject_id", "status", "attention_score"):
        assert decoded["agenda"][key] == original["agenda"][key]
    assert decoded["events"] == original["events"]
    assert json.loads(decoded["self_model"]["traits_json"]) == json.loads(
        original["self_model"]["traits_json"]
    )
    assert decoded["context_projection"]["original_hashes"] == {
        key: content_hash(original[key]) for key in ("agenda", "self_model")
    }


def test_omitted_suffix_is_still_bound_by_the_context_hash(context_payload: dict[str, Any]) -> None:
    first, second = deepcopy(context_payload), deepcopy(context_payload)
    first["self_model"]["values_json"] = canonical_json(["x" * 30000 + "A"])
    second["self_model"]["values_json"] = canonical_json(["x" * 30000 + "B"])
    first_result, second_result = (
        json.loads(fit_context(first, 8000)),
        json.loads(fit_context(second, 8000)),
    )
    assert first_result["self_model"] == second_result["self_model"]
    assert first_result["context_projection"] != second_result["context_projection"]
    assert content_hash(first_result) != content_hash(second_result)


@pytest.mark.parametrize("column", ["values_json", "traits_json"])
def test_projection_does_not_hide_corrupt_nested_json(
    context_payload: dict[str, Any], column: str
) -> None:
    context_payload["self_model"][column] = "{broken" * 2000
    with pytest.raises(IntegrityError, match="durable JSON"):
        fit_context(context_payload, 8000)


def test_unfittable_identifier_is_rejected_instead_of_shortened(
    context_payload: dict[str, Any],
) -> None:
    context_payload["agenda"]["source_id"] = "immutable" * 2000
    with pytest.raises(IntegrityError, match="identifiers exceed"):
        fit_context(context_payload, 8000)
    assert context_payload["agenda"]["source_id"] == "immutable" * 2000


def test_self_model_free_context_can_still_project_the_agenda(
    context_payload: dict[str, Any],
) -> None:
    context_payload["self_model"] = None
    context_payload["agenda"]["topic"] = "<" * 1950
    result = json.loads(fit_context(context_payload, 8000))
    assert result["self_model"] is None
    assert result["events"][0]["event_id"] == "evt_anchor"
    assert "agenda" in result["context_projection"]["original_hashes"]


def test_large_optional_advisory_does_not_shorten_a_fitting_self_model(
    context_payload: dict[str, Any],
) -> None:
    context_payload["self_model"]["continuity_statement"] = "x" * 3000
    original = deepcopy(context_payload["self_model"])
    context_payload["common_knowledge_advisories"] = [{"text": "<" * 10000}]
    result = json.loads(fit_context(context_payload, 8000))
    assert result["self_model"] == original
    assert "context_projection" not in result
    assert result["common_knowledge_advisories"] == []
    assert result["events"][0]["event_id"] == "evt_anchor"
