from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from noyra.cognition import EpistemicReviewProposal
from test_epistemic_terminal_selection import case as case
from test_epistemic_terminal_selection import no_change, setup_review, snapshot


async def paid_before_commit(review: Any) -> None:
    with (
        patch.object(review, "_commit", side_effect=RuntimeError("after paid success")),
        pytest.raises(RuntimeError, match="after paid success"),
    ):
        await review.run_due()


@pytest.mark.asyncio
async def test_paid_success_recovers_locally_even_with_daily_allowance_exhausted(case: Any) -> None:
    case.observation("original")
    review, provider = setup_review(case, [no_change()])
    review.settings = review.settings.model_copy(
        update={"max_epistemic_review_model_calls_per_day": 1}
    )
    await paid_before_commit(review)
    before = snapshot(case, "model_calls"), snapshot(case, "model_attempts")
    assert await review.run_due() == "epistemic_review_no_change"
    assert (snapshot(case, "model_calls"), snapshot(case, "model_attempts")) == before
    assert len(provider.requests) == 1 and review.verify_integrity() == 1


@pytest.mark.asyncio
async def test_bound_success_survives_new_observations_and_changed_day(case: Any) -> None:
    original = case.observation("original")
    review, provider = setup_review(case, [no_change()])
    await paid_before_commit(review)
    before = snapshot(case, "model_calls"), snapshot(case, "model_attempts")
    case.clock_value = "2026-08-13T13:00:00.000+00:00"
    case.observation("new evidence")
    assert await review.run_due() == "epistemic_review_no_change"
    assert (
        snapshot(case, "epistemic_review_runs")[0]["trigger_observation_id"]
        == original.observation_id
    )
    assert (snapshot(case, "model_calls"), snapshot(case, "model_attempts")) == before
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_legacy_success_is_adopted_only_when_current_request_hash_proves_original_input(
    case: Any,
) -> None:
    case.observation("old-compatible")
    review, provider = setup_review(case, [no_change()])
    context = review._context()
    assert context is not None
    await review.gateway.complete_structured(
        case.subject_id,
        f"epistemic_review:{context.trigger_observation_id}",
        review._messages(context),
        EpistemicReviewProposal,
        idempotency_key=f"epistemic-review:{context.trigger_observation_id}",
        max_output_tokens=min(4000, review.settings.max_output_tokens),
        temperature=review.settings.temperature,
    )
    review.settings = review.settings.model_copy(
        update={"max_epistemic_review_model_calls_per_day": 1}
    )
    assert await review.run_due() == "epistemic_review_no_change"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_new_receipt_failure_rolls_back_before_provider_dispatch(case: Any) -> None:
    from noyra.core.model_admission import track_model_preparation

    case.observation("original")
    review, provider = setup_review(case, [no_change()])

    def fail(connection: Any, call: Any) -> None:
        raise RuntimeError("synthetic binding failure")

    with track_model_preparation(fail), pytest.raises(RuntimeError):
        await review.run_due()
    assert not provider.requests and not snapshot(case, "model_calls")
    assert await review.run_due() == "epistemic_review_no_change"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["during_model", "before_commit"])
async def test_changed_target_revision_is_rejected_without_overwriting_new_state(
    case: Any, timing: str
) -> None:
    from noyra.mind import BeliefStore
    from test_epistemic_terminal_selection import no_change

    evidence = case.observation("original")
    store = BeliefStore(case.kernel.database)
    belief = store.create(
        case.subject_id,
        "Original bounded belief",
        confidence=0.5,
        scope="fixture",
        supporting_event_ids=(evidence.event_id,),
    )
    proposal = no_change()
    proposal["belief_revisions"] = [
        {
            "belief_id": belief.belief_id,
            "disposition": "strengthen",
            "proposition": belief.proposition,
            "confidence": 0.6,
            "supporting_observation_ids": [evidence.observation_id],
            "counter_observation_ids": [],
            "reason": "Original evidence supports modest update",
        }
    ]
    review, provider = setup_review(case, [proposal])

    def revise() -> None:
        with case.kernel.database.transaction() as connection:
            store._revise_connection(
                connection,
                belief.belief_id,
                proposition="A newer belief version",
                confidence=0.7,
                status="active",
                supporting_event_ids=(evidence.event_id,),
                counter_event_ids=(),
                reason="Independent newer evidence",
                expected_revision=belief.current_revision,
            )

    complete = provider.complete
    commit = review._commit

    async def during(request: Any) -> Any:
        result = await complete(request)
        revise()
        return result

    def before(*args: Any, **kwargs: Any) -> Any:
        revise()
        return commit(*args, **kwargs)

    with (
        patch.object(provider, "complete", side_effect=during)
        if timing == "during_model"
        else patch.object(review, "_commit", side_effect=before)
    ):
        assert await review.run_due() == "epistemic_review_rejected"
    current = store.get(belief.belief_id)
    assert current.proposition == "A newer belief version" and current.confidence == 0.7
    assert current.current_revision == belief.current_revision + 1
    assert len(provider.requests) == 1 and review.verify_integrity() == 1


@pytest.mark.asyncio
async def test_paused_subject_cannot_recover_paid_success(case: Any) -> None:
    case.observation("original")
    review, provider = setup_review(case, [no_change()])
    await paid_before_commit(review)
    before = snapshot(case, "model_calls"), snapshot(case, "model_attempts")
    case.kernel.lifecycle.transition("paused", "Synthetic pause", actor="operator")
    assert await review.run_due() == "epistemic_review_model_failed"
    assert (snapshot(case, "model_calls"), snapshot(case, "model_attempts")) == before
    assert not snapshot(case, "epistemic_review_runs") and len(provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["binding", "response"])
async def test_corrupt_paid_evidence_never_starts_replacement_request(
    case: Any, tamper: str
) -> None:
    from noyra.core.errors import IntegrityError

    case.observation("original")
    review, provider = setup_review(case, [no_change()])
    await paid_before_commit(review)
    with case.kernel.database.transaction() as connection:
        if tamper == "binding":
            connection.execute("DROP TRIGGER prevent_event_immutable_update")
            connection.execute(
                "UPDATE events SET payload_hash=? WHERE event_type='epistemic_context_bound'",
                ("0" * 64,),
            )
        else:
            connection.execute("UPDATE model_calls SET response_hash=?", ("0" * 64,))
    with pytest.raises(IntegrityError):
        await review.run_due()
    assert len(provider.requests) == 1 and not snapshot(case, "epistemic_review_runs")


@pytest.mark.asyncio
async def test_reconciled_content_only_success_does_not_invent_billing_fields(case: Any) -> None:
    import json

    from noyra.model.errors import ProviderCallError

    case.observation("uncertain")
    review, provider = setup_review(
        case, [ProviderCallError("synthetic_unknown", retryable=False, outcome_unknown=True)]
    )
    assert await review.run_due() == "epistemic_review_model_failed"
    attempts = snapshot(case, "model_attempts")
    call = review.gateway.ledger.unknown_calls(case.subject_id)[0]
    review.gateway.ledger.reconcile_unknown(
        call.call_id,
        outcome="succeeded",
        actor="operator",
        reason="Synthetic provider evidence",
        response={"content": json.dumps(no_change())},
    )
    assert await review.run_due() == "epistemic_review_no_change"
    assert snapshot(case, "model_attempts") == attempts and len(provider.requests) == 1


@pytest.mark.asyncio
async def test_concurrent_review_worker_yields_without_second_provider_call(case: Any) -> None:
    from noyra.cognition import EpistemicReview

    case.observation("original")
    review, provider = setup_review(case, [no_change()])
    other = EpistemicReview(
        case.kernel.database,
        case.subject_id,
        review.gateway,
        review.settings,
        clock=lambda: case.clock_value,
    )
    complete = provider.complete

    async def concurrent(request: Any) -> Any:
        assert await other.run_due() == "epistemic_review_waiting_reconciliation"
        return await complete(request)

    with patch.object(provider, "complete", side_effect=concurrent):
        assert await review.run_due() == "epistemic_review_no_change"
    assert len(provider.requests) == 1 and review.verify_integrity() == 1


@pytest.mark.asyncio
async def test_bound_paid_result_survives_real_encrypted_archive_and_kernel_restart(
    case: Any, monkeypatch: Any
) -> None:
    import base64
    from datetime import datetime, timedelta

    from noyra.cognition import EpistemicReview
    from noyra.core import EventStore, SubjectKernel
    from noyra.core.event_archive import EventPayloadArchive
    from noyra.core.types import content_hash

    case.observation("original")
    review, provider = setup_review(case, [no_change()])
    await paid_before_commit(review)
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )

    class Future(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            value = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(value.timestamp(), tz)

    archive = EventPayloadArchive(
        case.kernel.database,
        case.kernel.database.path.parent / "subject/cold",
        subject_id=case.subject_id,
    )
    with patch("noyra.core.event_archive.datetime", Future):
        assert archive.archive_cold(case.subject_id, older_than_days=90) > 0
    case.kernel.close()
    case.kernel = SubjectKernel(
        case.kernel.database.path, case.subject_id, content_hash({"seed": "epistemic-test"})
    )
    case.kernel.boot()
    if case.kernel.lifecycle.current().state == "booting":
        case.kernel.orient()
        case.kernel.activate()
    restarted = EpistemicReview(
        case.kernel.database,
        case.subject_id,
        review.gateway,
        review.settings,
        clock=lambda: case.clock_value,
    )
    assert await restarted.run_due() == "epistemic_review_no_change"
    assert len(provider.requests) == 1 and restarted.verify_integrity() == 1
    EventStore(case.kernel.database).verify_chain(case.subject_id)


@pytest.mark.asyncio
async def test_paid_recovery_does_not_need_original_gateway_configuration(case: Any) -> None:
    from noyra.model import BudgetLimits, FakeProvider, ModelGateway, ModelLedger

    case.observation("original")
    review, provider = setup_review(case, [no_change()])
    await paid_before_commit(review)
    empty = FakeProvider([])
    review.gateway = ModelGateway(
        empty,
        ModelLedger(case.kernel.database),
        model="replacement",
        limits=BudgetLimits(1, 1, 1, 1),
    )
    assert await review.run_due() == "epistemic_review_no_change"
    assert len(provider.requests) == 1 and not empty.requests


@pytest.mark.asyncio
async def test_registry_verifies_context_binding_without_constructing_gateway(case: Any) -> None:
    from noyra.core.integrity import IntegrityRegistry

    case.observation("original")
    review, _ = setup_review(case, [no_change()])
    assert await review.run_due() == "epistemic_review_no_change"

    def registry() -> Any:
        return IntegrityRegistry().run(
            case.kernel.database,
            case.subject_id,
            case.kernel.database.path.parent,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("cognition.metacognition",),
        )

    report = registry()
    assert not report.p0 and not report.p1
    with case.kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_event_immutable_update")
        connection.execute(
            "UPDATE events SET payload_hash=? WHERE event_type='epistemic_context_bound'",
            ("0" * 64,),
        )
    report = registry()
    assert report.p0 or report.p1
