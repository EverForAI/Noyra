from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from noyra.cognition.social import RelationshipSocialCognition
from noyra.core import SubjectKernel
from noyra.core.events import EventStore
from noyra.interaction import InteractionStore
from noyra.interaction.accounts import AccountStore
from noyra.mind import GoalCandidate, GoalStore, RelationshipStore
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


@pytest.mark.asyncio
@pytest.mark.parametrize("unroutable", [12, 80])
async def test_social_candidates_progress_past_invalid_contacts_and_global_feed(
    kernel: SubjectKernel, unroutable: int
) -> None:
    social, provider, _, _, inbound, _, first, who, _ = social_fixture(kernel)
    with kernel.database.connection() as connection:
        event = connection.execute(
            "SELECT event_id FROM events WHERE json_extract(payload_json,'$.interaction_id')=?",
            (first.interaction_id,),
        ).fetchone()[0]
    for index in range(unroutable):
        relationship = social.relationships.ensure(
            kernel.subject_id,
            "human",
            f"unroutable-{index}",
            f"Contact {index}",
            source_event_ids=(event,),
        )
        social.relationships.revise(
            relationship.relationship_id,
            trust=0,
            affinity=0,
            conflict=0,
            familiarity=1,
            boundaries={},
            reason="Known contact without an active route",
            source_event_ids=(event,),
        )
    # Unrelated recent traffic must not remove Alice's older established route.
    for index in range(50):
        InteractionStore(kernel.database).receive(
            kernel.subject_id, "web", "unrelated", f"Noise {index}"
        )
    with patch.object(social.relationships, "list", side_effect=AssertionError("unbounded read")):
        if unroutable > 64:
            assert await social.run_due() is None
            assert provider.requests == []
            assert len(social._candidate_relationships()[0]) <= 64
            # Cursor is durable; a fresh worker resumes after the invalid page.
            social = RelationshipSocialCognition(
                kernel.database,
                kernel.subject_id,
                social.gateway,
                social.settings,
                clock=social.clock,
            )
        context = social._context()
        assert context
        assert {r.entity_key for r in context.relationships.values()} == {who.entity_id}
        assert next(iter(context.routes.values())).interaction_id == first.interaction_id
        assert await social.run_due() == "relationship_social_contact"
    assert len(provider.requests) == 1
    with kernel.database.connection() as connection:
        outgoing = connection.execute(
            "SELECT counterparty,related_interaction_id FROM interactions "
            "WHERE direction='outgoing'"
        ).fetchone()
    assert tuple(outgoing) == ("original-room", first.interaction_id)
    if unroutable > 64:
        binding = inbound.list_bindings(kernel.subject_id)[0]["binding_id"]
        inbound.change_binding(
            binding, kernel.subject_id, status="disabled", actor="operator", reason="Withdraw route"
        )
        assert social._context() is None
        with kernel.database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT 1 FROM schema_meta WHERE key=?",
                    (f"relationship_social_cursor:{kernel.subject_id}",),
                ).fetchone()
                is None
            )  # Tail exhausted; restart from the priority head.
        assert social._context() is None
        inbound.change_binding(
            binding, kernel.subject_id, status="active", actor="operator", reason="Restore route"
        )
        # A high-priority restored recipient is visible even with a saved tail.
        original = social.relationships.find(
            kernel.subject_id, who.relationship_type, who.entity_id
        )
        social.relationships.revise(
            original.relationship_id,
            trust=0,
            affinity=1,
            conflict=0,
            familiarity=1,
            boundaries={},
            reason="Restore priority",
            source_event_ids=(event,),
        )
        assert social._context() is not None


def test_legacy_contact_history_survives_unrelated_recent_messages(kernel: SubjectKernel) -> None:
    social, _, _, _, _, _, first, who, _ = social_fixture(kernel)
    original = social.relationships.find(kernel.subject_id, who.relationship_type, who.entity_id)
    with kernel.database.connection() as connection:
        event = connection.execute(
            "SELECT event_id FROM events WHERE json_extract(payload_json,'$.interaction_id')=?",
            (first.interaction_id,),
        ).fetchone()[0]
    social.relationships.revise(
        original.relationship_id,
        trust=0,
        affinity=0,
        conflict=0,
        familiarity=0,
        boundaries={"no_contact": True},
        reason="Respect Alice's distance",
        source_event_ids=(event,),
    )
    incoming = InteractionStore(kernel.database).receive(
        kernel.subject_id, "web", "legacy-founder", "A known legacy correspondent"
    )
    relationship = RelationshipStore(kernel.database).ensure(
        kernel.subject_id,
        "human",
        "legacy-founder",
        "Founder",
        source_event_ids=(event,),
    )
    for index in range(50):
        InteractionStore(kernel.database).receive(
            kernel.subject_id, "web", "elsewhere", f"Unrelated {index}"
        )
    context = social._context()
    assert context
    assert set(context.relationships) == {relationship.relationship_id}
    assert context.routes[relationship.relationship_id].interaction_id == incoming.interaction_id
