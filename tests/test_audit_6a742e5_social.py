from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from noyra.core import SubjectKernel
from noyra.core.events import EventStore
from noyra.interaction import InteractionStore
from noyra.interaction.accounts import AccountStore
from noyra.mind import GoalCandidate, GoalStore
from noyra.model import CompletionRequest, ModelUsage, ProviderResponse
from test_audit_20261024_followup import social_fixture
from test_audit_20261024_remediation import kernel as base_kernel


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    yield from cast(Any, base_kernel).__wrapped__(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["self", "environment", "mixed"])
async def test_recipient_generation_never_discloses_private_derived_goals(
    kernel: SubjectKernel, origin: str
) -> None:
    social, provider, _, _, _, _, first, _, _ = social_fixture(kernel)
    accounts = AccountStore(kernel.database, kernel.subject_id)
    accounts.register("bob", "correct horse battery", "Bob")
    bob = accounts.authenticate(accounts.login("bob", "correct horse battery")[0])
    assert bob
    marker = "PRIVATE_BOB_GOAL_7bc132"
    incoming = InteractionStore(kernel.database).receive(
        kernel.subject_id,
        "participant",
        bob.entity_id,
        marker,
        participant=bob,
        participant_private=True,
        participant_conversation="participant:" + bob.entity_id,
    )
    with kernel.database.connection() as connection:
        event = connection.execute(
            "SELECT event_id FROM events WHERE json_extract(payload_json,'$.interaction_id')=?",
            (incoming.interaction_id,),
        ).fetchone()[0]
    goals = GoalStore(kernel.database)
    goal = goals.create_candidate(
        kernel.subject_id,
        GoalCandidate.model_validate(
            {
                "title": "Remember " + marker,
                "description": "Private matter learned from Bob",
                "origin": origin,
                "priority": 0.5,
                "commitment": 0.5,
                "motive_emotion": "interest",
            }
        ),
        causal_source_ids=(event,),
        reason="Private internally generated goal",
    )
    goals.activate(goal.goal_id, rationale="Personal choice", causal_source_ids=(event,))
    metadata_secret = "PRIVATE_EVENT_SOURCE_3bc27"
    evidence = EventStore(kernel.database).append(
        kernel.subject_id,
        "goal_governance_committed",
        metadata_secret,
        {"private": metadata_secret},
    )
    context = social._context()
    assert context
    relation = next(iter(context.relationships))
    actual_requests: list[str] = []

    async def echo_actual_input(request: CompletionRequest) -> ProviderResponse:
        actual_requests.append(request.messages[1].content)
        secrets = [text for text in (marker, metadata_secret) if text in actual_requests[-1]]
        return ProviderResponse(
            json.dumps(
                {
                    "disposition": "contact",
                    "relationship_id": relation,
                    "topic": "Followup",
                    "rationale": "Synthetic prompt echo",
                    "content": " ".join(secrets) or "Hello",
                    "evidence_event_ids": [evidence.event_id],
                }
            ),
            ModelUsage(100, 100),
        )

    with patch.object(provider, "complete", side_effect=echo_actual_input):
        assert await social.run_due() == "relationship_social_contact"
    assert len(actual_requests) == 1
    assert marker not in actual_requests[0] and metadata_secret not in actual_requests[0]
    with kernel.database.connection() as connection:
        outgoing = connection.execute(
            "SELECT * FROM interactions WHERE direction='outgoing'"
        ).fetchone()
    assert outgoing["counterparty"] == "original-room"
    assert outgoing["related_interaction_id"] == first.interaction_id
    assert marker not in outgoing["content"] and metadata_secret not in outgoing["content"]
    assert goals.get(goal.goal_id).title == "Remember " + marker
    assert goals.get(goal.goal_id).status == "active"
    assert json.loads(context.serialized)["active_goals"] == []
    assert all(
        set(row) == {"event_id", "event_type", "occurred_at"}
        for row in json.loads(context.serialized)["recent_events"]
    )
