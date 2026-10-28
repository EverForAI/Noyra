from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from noyra.cognition import CognitionSettings
from noyra.cognition.social import RelationshipSocialCognition
from noyra.core import SubjectKernel
from noyra.interaction import InteractionStore
from noyra.model import (
    BudgetLimits,
    CompletionRequest,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)
from test_audit_6a742e5_integrity import assert_clean_and_active
from test_audit_6a742e5_integrity import kernel as base_kernel
from test_audit_20261024_remediation import make_collaboration


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    yield from cast(Any, base_kernel).__wrapped__(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before", "dispatch", "response", "commit"])
async def test_social_invitation_withdrawal_fences_all_boundaries_and_preserves_new_contact(
    kernel: SubjectKernel,
    timing: str,
) -> None:
    collaboration, who, bounty = make_collaboration(kernel)
    application = collaboration.write(
        "/api/participants/apply",
        {"bounty_id": bounty.bounty_id, "content": "I can help", "idempotency_key": "interest"},
        who,
        "127.0.0.1",
    )
    with kernel.database.connection() as c:
        source = c.execute(
            "SELECT interaction_id FROM interactions WHERE direction='incoming'"
        ).fetchone()[0]
        event = c.execute(
            "SELECT event_id FROM events WHERE json_extract(payload_json,'$.interaction_id')=?",
            (source,),
        ).fetchone()[0]
    provider = FakeProvider([])
    ledger = ModelLedger(kernel.database)
    gateway = ModelGateway(
        provider, ledger, model="test-model", limits=BudgetLimits(10, 100000, 100000, 1000000)
    )
    social = RelationshipSocialCognition(
        kernel.database, kernel.subject_id, gateway, CognitionSettings()
    )
    relation = social.relationships.ensure(
        kernel.subject_id,
        who.relationship_type,
        who.entity_id,
        who.display_name,
        source_event_ids=(event,),
    )

    def withdraw() -> None:
        collaboration.write(
            "/api/participants/withdraw",
            {"kind": "application", "id": application["application_id"]},
            who,
            "127.0.0.1",
        )

    response = ProviderResponse(
        json.dumps(
            {
                "disposition": "contact",
                "relationship_id": relation.relationship_id,
                "topic": "Followup",
                "rationale": "Local synthetic decision",
                "content": "Hello",
                "evidence_event_ids": [event],
            }
        ),
        ModelUsage(100, 100),
    )
    actual_requests: list[CompletionRequest] = []

    async def complete(request: CompletionRequest) -> ProviderResponse:
        actual_requests.append(request)
        if timing == "response" and len(actual_requests) == 1:
            withdraw()
        return response

    authorize = ledger.check_request_authorization
    commit = social._commit
    withdrawn = False

    def dispatch(*args: Any, **kwargs: Any) -> None:
        nonlocal withdrawn
        if timing == "dispatch" and not withdrawn:
            withdrawn = True
            withdraw()
        authorize(*args, **kwargs)

    def commit_after_withdrawal(*args: Any, **kwargs: Any) -> Any:
        nonlocal withdrawn
        if timing == "commit" and not withdrawn:
            withdrawn = True
            withdraw()
        return commit(*args, **kwargs)

    if timing == "before":
        withdraw()
    with (
        patch.object(provider, "complete", side_effect=complete),
        patch.object(ledger, "check_request_authorization", side_effect=dispatch),
        patch.object(social, "_commit", side_effect=commit_after_withdrawal),
    ):
        result = await social.run_due()
        assert result is None if timing == "before" else result == "relationship_social_rejected"
        with kernel.database.connection() as c:
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM interactions WHERE direction='outgoing'"
                ).fetchone()[0]
                == 0
            )
            calls = c.execute("SELECT status FROM model_calls").fetchall()
            assert [row[0] for row in calls] == (
                [] if timing == "before" else ["failed"] if timing == "dispatch" else ["succeeded"]
            )
        assert len(actual_requests) == (0 if timing in {"before", "dispatch"} else 1)
        assert_clean_and_active(kernel)
        # Withdrawal is scoped to one invitation, not a permanent account ban.
        new = InteractionStore(kernel.database).receive(
            kernel.subject_id,
            "participant",
            who.entity_id,
            "An independent new invitation",
            participant=who,
            participant_private=True,
            participant_conversation="participant:" + who.entity_id,
        )
        assert await social.run_due() == "relationship_social_contact"
    with kernel.database.connection() as c:
        outgoing = c.execute(
            "SELECT related_interaction_id FROM interactions WHERE direction='outgoing'"
        ).fetchall()
        assert [row[0] for row in outgoing] == [new.interaction_id]
    assert_clean_and_active(kernel)
