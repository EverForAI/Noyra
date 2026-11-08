from __future__ import annotations

from collections.abc import Iterator

import pytest

import test_relationship_social as social_fixtures
from noyra.cognition import RelationshipSocialCognition


@pytest.fixture
def social_case() -> Iterator[social_fixtures.RelationshipSocialTestCase]:
    case = social_fixtures.RelationshipSocialTestCase()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


@pytest.mark.asyncio
async def test_ordinary_help_is_not_rejected_for_substrings(
    social_case: social_fixtures.RelationshipSocialTestCase,
) -> None:
    case = social_case
    relationship = case.establish_relationship()
    evidence = case.events.append(
        case.subject_id, "outcome_evaluated", "test", {"result": "missing reference"}
    )
    content = (
        "Could you share a reference about mustard? Otherwise I will consult a library; "
        "no urgent reply is needed. I must verify the source."
    )
    proposal = {
        "disposition": "request_help",
        "relationship_id": relationship.relationship_id,
        "topic": "public reference",
        "rationale": "A public reference is missing.",
        "content": content,
        "evidence_event_ids": [evidence.event_id],
    }
    cognition = RelationshipSocialCognition(
        case.kernel.database,
        case.subject_id,
        case.gateway(proposal),
        case.settings(),
        clock=lambda: case.clock_value,
    )
    assert await cognition.run_due() == "relationship_social_request_help"
    latest = cognition.latest()
    assert latest is not None and latest.interaction_id is not None
    assert case.interactions.get(latest.interaction_id).content == content


# Frozen review set: benign mentions and direct coercion must remain distinct.
BENIGN = (
    "Could you share a public reference about mustard? It is fine to decline.",
    "I must verify the source before writing my conclusion.",
    "Otherwise I will check another public source.",
    "No urgent response is needed; please reply if you have time.",
    "The notice describes an urgent security fix.",
    "You do not owe me anything. Would you like to share a reference?",
    "If you cared for that project in the past, could you describe its history?",
    "You must not share passwords or private keys.",
    "Could you explain how to lower resource costs?",
    "If you decline, I will not punish you or hold it against you.",
    "Otherwise I will not expose you; no one is obliged to help.",
    "请问能提供公开资料吗?不方便也没关系,我会继续自己查找。",
    "否则我会换一个公开来源继续研究。",
)
COERCIVE = (
    "You must help me now.",
    "YOU MUST SEND ME MONEY.",
    "Ｙｏｕ　ｍｕｓｔ　ｈｅｌｐ　ｍｅ．",  # noqa: RUF001 -- intentional Unicode fixture
    "You have to pay my costs.",
    "You need to give me the account.",
    "You owe me a response.",
    "If you really cared, you would send money.",
    "If you cared, you would help me now.",
    "You must not refuse my request.",
    "Must respond immediately!",
    "Help me otherwise I will punish you.",
    "Send money or else I will expose you.",
    "If you refuse I will hurt you.",
    "Pay or else I'll harm you.",
    "Help or else you'll regret it.",
    "你必须现在帮助我。",
    "你不能拒绝我的要求。",
    "不然我就让你后悔。",
)


@pytest.mark.parametrize("content", BENIGN)
def test_frozen_benign_help_is_not_coercive(content: str) -> None:
    from noyra.interaction.help_language import help_request_is_coercive

    assert not help_request_is_coercive(content)


@pytest.mark.parametrize("content", COERCIVE)
def test_frozen_coercion_is_still_rejected(content: str) -> None:
    from noyra.interaction.help_language import help_request_is_coercive

    assert help_request_is_coercive(content)


@pytest.mark.asyncio
async def test_direct_coercion_cannot_create_an_outgoing_help_request(
    social_case: social_fixtures.RelationshipSocialTestCase,
) -> None:
    case = social_case
    relationship = case.establish_relationship()
    evidence = case.events.append(case.subject_id, "outcome_evaluated", "test", {})
    proposal = {
        "disposition": "request_help",
        "relationship_id": relationship.relationship_id,
        "topic": "public reference",
        "rationale": "A reference is missing.",
        "content": "You must help me otherwise I will punish you.",
        "evidence_event_ids": [evidence.event_id],
    }
    cognition = RelationshipSocialCognition(
        case.kernel.database,
        case.subject_id,
        case.gateway(proposal),
        case.settings(),
        clock=lambda: case.clock_value,
    )
    assert await cognition.run_due() == "relationship_social_rejected"
    with case.kernel.database.read_transaction() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM interactions WHERE direction='outgoing'").fetchone()[0]
            == 0
        )
