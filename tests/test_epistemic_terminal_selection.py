from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

import test_epistemic_review as helpers
from noyra.cognition import EpistemicReview, MetacognitiveControl
from noyra.core.database import Database
from noyra.core.events import EventStore
from noyra.mind import BeliefStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)
from noyra.model.errors import ProviderCallError


@pytest.fixture
def case(monkeypatch: pytest.MonkeyPatch) -> Iterator[helpers.EpistemicReviewTestCase]:
    value = helpers.EpistemicReviewTestCase(methodName="runTest")
    value.setUp()
    monkeypatch.setattr("noyra.model.ledger.utc_now", lambda: value.clock_value)
    try:
        yield value
    finally:
        value.tearDown()


def setup_review(
    case: helpers.EpistemicReviewTestCase,
    outcomes: list[Mapping[str, object] | ProviderCallError],
) -> tuple[EpistemicReview, FakeProvider]:
    provider = FakeProvider(
        [
            outcome
            if isinstance(outcome, ProviderCallError)
            else ProviderResponse(
                content=json.dumps(outcome), usage=ModelUsage(100, 50), finish_reason="stop"
            )
            for outcome in outcomes
        ]
    )
    gateway = ModelGateway(
        provider,
        ModelLedger(case.kernel.database),
        model="epistemic-model",
        limits=BudgetLimits(20, 100_000, 100_000, 1_000_000),
    )
    return EpistemicReview(
        case.kernel.database,
        case.subject_id,
        gateway,
        case.settings(),
        clock=lambda: case.clock_value,
    ), provider


def no_change() -> dict[str, object]:
    return {
        "summary": "No evidence warrants a revision.",
        "belief_revisions": [],
        "prediction_resolutions": [],
    }


def rejected_proposal(observation_id: str) -> dict[str, object]:
    return {
        "summary": "This proposal references an unavailable belief.",
        "belief_revisions": [
            {
                "belief_id": "unavailable-belief",
                "disposition": "strengthen",
                "proposition": "Unsupported belief",
                "confidence": 0.6,
                "supporting_observation_ids": [observation_id],
                "counter_observation_ids": [],
                "reason": "Synthetic invalid proposal",
            }
        ],
        "prediction_resolutions": [],
    }


def control(case: helpers.EpistemicReviewTestCase) -> MetacognitiveControl:
    return MetacognitiveControl(
        case.kernel.database, case.subject_id, case.settings(), clock=lambda: case.clock_value
    )


def snapshot(case: helpers.EpistemicReviewTestCase, table: str) -> list[dict[str, Any]]:
    assert table in {
        "epistemic_review_runs",
        "model_calls",
        "model_attempts",
        "observations",
        "beliefs",
        "predictions",
    }
    with case.kernel.database.read_transaction() as c:
        return [dict(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]


@pytest.mark.asyncio
@pytest.mark.parametrize("reopen", [False, True], ids=["same-process", "old-store-reopen"])
async def test_rejected_round_releases_both_scheduler_and_executor_for_new_evidence(
    case: helpers.EpistemicReviewTestCase,
    reopen: bool,
) -> None:
    first = case.observation("first")
    review, provider = setup_review(case, [rejected_proposal(first.observation_id), no_change()])
    scheduler = control(case)
    decision = scheduler.run_due()
    assert decision.strategy == "epistemic_review" and decision.target_id == first.observation_id
    result = await review.run_due()
    assert result == "epistemic_review_rejected"
    assert scheduler.record_result(result) == result
    before_review = snapshot(case, "epistemic_review_runs")
    before_calls = snapshot(case, "model_calls")
    before_attempts = snapshot(case, "model_attempts")
    before_observation = snapshot(case, "observations")
    assert before_review[0]["status"] == "rejected"
    assert not snapshot(case, "beliefs") and not snapshot(case, "predictions")
    case.clock_value = "2026-08-12T13:00:00.000+00:00"
    second = case.observation("second")
    if reopen:
        fresh = Database(case.kernel.database.path, initialize=False)
        review = EpistemicReview(
            fresh, case.subject_id, review.gateway, case.settings(), clock=lambda: case.clock_value
        )
        scheduler = MetacognitiveControl(
            fresh, case.subject_id, case.settings(), clock=lambda: case.clock_value
        )
    decision = scheduler.run_due()
    assert decision.strategy == "epistemic_review" and decision.target_id == second.observation_id
    result = await review.run_due()
    assert result == "epistemic_review_no_change"
    assert scheduler.record_result(result) == result
    assert len(provider.requests) == 2
    assert snapshot(case, "epistemic_review_runs")[0] == before_review[0]
    assert snapshot(case, "model_calls")[0] == before_calls[0]
    assert snapshot(case, "model_attempts")[0] == before_attempts[0]
    assert snapshot(case, "observations")[0] == before_observation[0]
    assert [row["trigger_observation_id"] for row in snapshot(case, "epistemic_review_runs")] == [
        first.observation_id,
        second.observation_id,
    ]
    assert (
        snapshot(case, "model_calls")[1]["purpose"] == f"epistemic_review:{second.observation_id}"
    )
    assert review.verify_integrity() == 2
    assert scheduler.verify_integrity()["epistemic_review_runs"] == 2
    EventStore(case.kernel.database).verify_chain(case.subject_id)


@pytest.mark.asyncio
async def test_rejected_round_without_new_analyzed_evidence_has_no_more_work_or_calls(
    case: helpers.EpistemicReviewTestCase,
) -> None:
    first = case.observation("first")
    review, provider = setup_review(case, [rejected_proposal(first.observation_id)])
    assert await review.run_due() == "epistemic_review_rejected"
    case.observation("pending", analyzed=False)
    before = snapshot(case, "epistemic_review_runs")
    for _ in range(3):
        assert await review.run_due() is None
    candidates, _ = control(case)._candidates()
    assert not any(candidate.strategy == "epistemic_review" for candidate in candidates)
    assert len(provider.requests) == 1
    assert snapshot(case, "epistemic_review_runs") == before


@pytest.mark.asyncio
async def test_rejection_does_not_remove_old_evidence_from_a_later_valid_review(
    case: helpers.EpistemicReviewTestCase,
) -> None:
    first = case.observation("original evidence")
    belief = BeliefStore(case.kernel.database).create(
        case.subject_id,
        "A bounded belief.",
        confidence=0.5,
        scope="test",
        supporting_event_ids=(first.event_id,),
    )
    valid: dict[str, object] = {
        "summary": "The analyzed evidence supports a bounded revision.",
        "belief_revisions": [
            {
                "belief_id": belief.belief_id,
                "disposition": "strengthen",
                "proposition": belief.proposition,
                "confidence": 0.6,
                "supporting_observation_ids": [first.observation_id],
                "counter_observation_ids": [],
                "reason": "Use genuine analyzed evidence, including the old trigger.",
            }
        ],
        "prediction_resolutions": [],
    }
    review, provider = setup_review(case, [rejected_proposal(first.observation_id), valid])
    assert await review.run_due() == "epistemic_review_rejected"
    assert BeliefStore(case.kernel.database).get(belief.belief_id).current_revision == 1
    original = snapshot(case, "observations")[0]
    case.clock_value = "2026-08-12T13:00:00.000+00:00"
    second = case.observation("new evidence")
    context = review._context()
    assert context is not None and context.trigger_observation_id == second.observation_id
    assert first.observation_id in context.eligible_observations
    assert await review.run_due() == "epistemic_review_committed"
    updated = BeliefStore(case.kernel.database).get(belief.belief_id)
    assert updated.current_revision == 2 and updated.confidence == 0.6
    assert snapshot(case, "observations")[0] == original
    assert len(provider.requests) == 2
    assert review.verify_integrity() == 2
    assert len(BeliefStore(case.kernel.database).revisions(belief.belief_id)) == 2


@pytest.mark.asyncio
async def test_unknown_round_keeps_identity_and_reservations_across_restart_and_day_change(
    case: helpers.EpistemicReviewTestCase,
) -> None:
    first = case.observation("first")
    review, provider = setup_review(
        case,
        [
            rejected_proposal(first.observation_id),
            ProviderCallError(
                "provider_outcome_unknown",
                retryable=False,
                outcome_unknown=True,
                usage_unknown=True,
            ),
        ],
    )
    assert await review.run_due() == "epistemic_review_rejected"
    case.clock_value = "2026-08-12T13:00:00.000+00:00"
    second = case.observation("uncertain")
    assert await review.run_due() == "epistemic_review_model_failed"
    calls = snapshot(case, "model_calls")
    attempts = snapshot(case, "model_attempts")
    reviews = snapshot(case, "epistemic_review_runs")
    assert calls[-1]["status"] == "unknown"
    assert attempts[-1]["status"] == "unknown" and attempts[-1]["reserved_output_tokens"] > 0
    assert len(reviews) == 1 and reviews[0]["status"] == "rejected"
    fresh = Database(case.kernel.database.path, initialize=False)
    restarted = EpistemicReview(
        fresh, case.subject_id, review.gateway, case.settings(), clock=lambda: case.clock_value
    )
    for now in ["2026-08-12T13:01:00.000+00:00", "2026-08-13T13:00:00.000+00:00"]:
        case.clock_value = now
        context = restarted._context()
        assert context is not None and context.trigger_observation_id == second.observation_id
        assert await restarted.run_due() == "epistemic_review_model_failed"
    assert len(provider.requests) == 2
    assert snapshot(case, "model_calls") == calls
    assert snapshot(case, "model_attempts") == attempts
    assert snapshot(case, "epistemic_review_runs") == reviews
    assert restarted.verify_integrity() == 1
    candidates, _ = control(case)._candidates()
    assert (
        next(c for c in candidates if c.strategy == "epistemic_review").target_id
        == second.observation_id
    )


@pytest.mark.asyncio
async def test_advancing_past_rejection_does_not_bypass_daily_review_quota(
    case: helpers.EpistemicReviewTestCase,
) -> None:
    first = case.observation("first")
    review, provider = setup_review(case, [rejected_proposal(first.observation_id), no_change()])
    review.settings = review.settings.model_copy(
        update={"max_epistemic_review_model_calls_per_day": 1}
    )
    assert await review.run_due() == "epistemic_review_rejected"
    calls = snapshot(case, "model_calls")
    case.clock_value = "2026-08-12T13:00:00.000+00:00"
    second = case.observation("next")
    assert await review.run_due() is None
    assert len(provider.requests) == 1 and snapshot(case, "model_calls") == calls
    case.clock_value = "2026-08-13T12:00:00.000+00:00"
    assert await review.run_due() == "epistemic_review_no_change"
    assert len(provider.requests) == 2
    assert (
        snapshot(case, "epistemic_review_runs")[-1]["trigger_observation_id"]
        == second.observation_id
    )


@pytest.mark.asyncio
async def test_subject_scope_and_equal_time_order_are_preserved(
    case: helpers.EpistemicReviewTestCase,
) -> None:
    from noyra.core.identity import IdentityStore
    from noyra.core.types import content_hash
    from noyra.world import FetchedDocument, ObservationStore, SourceRegistry

    first = case.observation("one")
    second = case.observation("two")
    case.observation("not analyzed", analyzed=False)
    foreign_subject = "other-subject"
    IdentityStore(case.kernel.database).ensure(foreign_subject, content_hash("other"))
    source = SourceRegistry(case.kernel.database).register(
        foreign_subject,
        "Older foreign evidence",
        "https://example.com/foreign",
        "news",
        status="active",
    )
    store = ObservationStore(case.kernel.database)
    foreign, _ = store.record(
        foreign_subject,
        source.source_id,
        FetchedDocument(
            url=source.url,
            title="foreign",
            content="foreign content",
            content_hash=content_hash("foreign content"),
            media_type="text/plain",
            injection_signals=(),
            etag=None,
            last_modified=None,
            fetched_at="2026-08-11T12:00:00.000+00:00",
        ),
    )
    store.mark(foreign.observation_id, "analyzed", subject_id=foreign_subject)
    before = store.get(foreign.observation_id, subject_id=foreign_subject)
    review, provider = setup_review(case, [no_change(), no_change()])
    expected = sorted([first.observation_id, second.observation_id])
    for identifier in expected:
        candidates, _ = control(case)._candidates()
        assert (
            next(c for c in candidates if c.strategy == "epistemic_review").target_id == identifier
        )
        assert await review.run_due() == "epistemic_review_no_change"
    assert await review.run_due() is None
    assert len(provider.requests) == 2
    assert [
        r["trigger_observation_id"] for r in snapshot(case, "epistemic_review_runs")
    ] == expected
    assert store.get(foreign.observation_id, subject_id=foreign_subject) == before
    assert review.verify_integrity() == 2
