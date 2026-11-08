from __future__ import annotations

from typing import Any

import pytest

from noyra.cognition import EpistemicReviewProposal
from noyra.model.errors import ProviderCallError
from test_epistemic_terminal_selection import case as case
from test_epistemic_terminal_selection import no_change, setup_review, snapshot


async def legacy_call(case: Any, review: Any) -> Any:
    context = review._context()
    assert context is not None
    return await review.gateway.complete_structured(
        case.subject_id,
        f"epistemic_review:{context.trigger_observation_id}",
        review._messages(context),
        EpistemicReviewProposal,
        idempotency_key=f"epistemic-review:{context.trigger_observation_id}",
        max_output_tokens=min(4000, review.settings.max_output_tokens),
        temperature=review.settings.temperature,
    )


@pytest.mark.asyncio
async def test_determined_model_failure_retires_trigger_without_faking_business_proposal(
    case: Any,
) -> None:
    first = case.observation("first")
    review, provider = setup_review(
        case,
        [
            ProviderCallError("synthetic_known_failure", retryable=False, outcome_unknown=False),
            no_change(),
        ],
    )
    assert await review.run_due() == "epistemic_review_model_failed"
    before = snapshot(case, "model_calls")[0]
    case.clock_value = "2026-08-13T13:00:00.000+00:00"
    second = case.observation("second")
    current = review._context()
    assert current is not None and current.trigger_observation_id == second.observation_id
    assert await review.run_due() == "epistemic_review_no_change"
    assert snapshot(case, "model_calls")[0] == before
    runs = snapshot(case, "epistemic_review_runs")
    assert len(runs) == 1 and runs[0]["trigger_observation_id"] == second.observation_id
    assert all(row["status"] == "analyzed" for row in snapshot(case, "observations"))
    assert first.observation_id != second.observation_id and len(provider.requests) == 2


@pytest.mark.asyncio
async def test_old_failed_call_is_closed_locally_before_any_new_provider_request(case: Any) -> None:
    case.observation("old failure")
    review, provider = setup_review(
        case,
        [
            ProviderCallError("synthetic_known_failure", retryable=False, outcome_unknown=False),
            no_change(),
        ],
    )
    with pytest.raises(ProviderCallError):
        await legacy_call(case, review)
    before = snapshot(case, "model_calls")[0]
    case.clock_value = "2026-08-13T13:00:00.000+00:00"
    second = case.observation("second")
    assert await review.run_due() == "epistemic_review_model_failed"
    assert len(provider.requests) == 1
    current = review._context()
    assert current is not None and current.trigger_observation_id == second.observation_id
    assert await review.run_due() == "epistemic_review_no_change"
    assert snapshot(case, "model_calls")[0] == before


@pytest.mark.asyncio
async def test_unprovable_old_success_is_held_without_repayment_and_next_trigger_can_run(
    case: Any,
) -> None:
    case.observation("old success")
    review, provider = setup_review(case, [no_change(), no_change()])
    await legacy_call(case, review)
    before = snapshot(case, "model_calls")[0]
    case.clock_value = "2026-08-13T13:00:00.000+00:00"
    second = case.observation("new evidence")
    assert await review.run_due() == "epistemic_review_legacy_result_held"
    assert len(provider.requests) == 1 and not snapshot(case, "epistemic_review_runs")
    assert await review.run_due() == "epistemic_review_no_change"
    assert (
        snapshot(case, "epistemic_review_runs")[0]["trigger_observation_id"]
        == second.observation_id
    )
    assert snapshot(case, "model_calls")[0] == before
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_failure_exit_is_shared_with_metacognitive_candidate_selection(case: Any) -> None:
    from test_epistemic_terminal_selection import control

    first = case.observation("first")
    review, _ = setup_review(
        case, [ProviderCallError("synthetic_failure", retryable=False, outcome_unknown=False)]
    )
    assert await review.run_due() == "epistemic_review_model_failed"
    case.clock_value = "2026-08-13T13:00:00.000+00:00"
    second = case.observation("second")
    candidates, _ = control(case)._candidates()
    selected = next(item for item in candidates if item.strategy == "epistemic_review")
    assert (
        selected.target_id == second.observation_id and selected.target_id != first.observation_id
    )
    assert review.verify_integrity() == 0


@pytest.mark.asyncio
async def test_budget_wait_does_not_retire_trigger_and_uses_bounded_new_identity(case: Any) -> None:
    from noyra.model import BudgetLimits

    review, provider = setup_review(case, [no_change()])
    original = case.observation("budget wait")
    review.gateway.limits = BudgetLimits(0, 100000, 100000, 100000)
    assert await review.run_due() == "epistemic_review_budget_exhausted"
    calls = snapshot(case, "model_calls")
    for _ in range(3):
        assert await review.run_due() == "epistemic_review_budget_exhausted"
    assert snapshot(case, "model_calls") == calls and not provider.requests
    assert review._context() is not None
    review.gateway.limits = BudgetLimits(10, 100000, 100000, 100000)
    case.clock_value = "2026-08-12T12:02:00.000+00:00"
    assert await review.run_due() == "epistemic_review_no_change"
    rows = snapshot(case, "model_calls")
    assert len(rows) == 2 and rows[0] == calls[0]
    assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]
    assert rows[0]["purpose"] == rows[1]["purpose"] == f"epistemic_review:{original.observation_id}"
    assert len(provider.requests) == 1 and review.verify_integrity() == 1


@pytest.mark.asyncio
async def test_exit_cannot_bypass_daily_model_call_allowance(case: Any) -> None:
    case.observation("first")
    review, provider = setup_review(
        case,
        [
            ProviderCallError("synthetic_failure", retryable=False, outcome_unknown=False),
            no_change(),
        ],
    )
    review.settings = review.settings.model_copy(
        update={"max_epistemic_review_model_calls_per_day": 1}
    )
    assert await review.run_due() == "epistemic_review_model_failed"
    case.clock_value = "2026-08-12T13:00:00.000+00:00"
    second = case.observation("second")
    assert await review.run_due() is None
    assert len(provider.requests) == 1
    case.clock_value = "2026-08-13T13:00:00.000+00:00"
    assert await review.run_due() == "epistemic_review_no_change"
    assert (
        snapshot(case, "epistemic_review_runs")[0]["trigger_observation_id"]
        == second.observation_id
    )


@pytest.mark.asyncio
async def test_unknown_and_consumed_explicit_retry_remain_fenced_until_closed(case: Any) -> None:
    review, provider = setup_review(
        case,
        [
            ProviderCallError("synthetic_unknown", retryable=False, outcome_unknown=True),
            ProviderCallError("synthetic_known_failure", retryable=False, outcome_unknown=False),
            no_change(),
        ],
    )
    original = case.observation("unknown")
    assert await review.run_due() == "epistemic_review_model_failed"
    before = snapshot(case, "model_attempts")
    call = review.gateway.ledger.unknown_calls(case.subject_id)[0]
    for _ in range(2):
        assert await review.run_due() == "epistemic_review_model_failed"
    assert len(provider.requests) == 1 and snapshot(case, "model_attempts") == before
    review.gateway.ledger.prepare_unknown_retry(
        call.call_id, actor="operator", reason="Synthetic explicit retry"
    )
    assert await review.run_due() in {
        "epistemic_review_model_failed",
        "epistemic_review_waiting_reconciliation",
    }
    assert len(provider.requests) == 2
    assert snapshot(case, "model_attempts")[0] == before[0]
    for _ in range(2):
        assert await review.run_due() == "epistemic_review_model_failed"
    review.gateway.ledger.close_failed_unknown_retry(
        call.call_id,
        actor="operator",
        reason="Synthetic close",
        resolution_evidence="synthetic-provider-proof",
        accept_duplicate_risk=True,
    )
    case.clock_value = "2026-08-13T13:00:00.000+00:00"
    second = case.observation("new after closure")
    assert await review.run_due() == "epistemic_review_model_failed"
    assert await review.run_due() == "epistemic_review_no_change"
    assert (
        snapshot(case, "epistemic_review_runs")[0]["trigger_observation_id"]
        == second.observation_id
    )
    assert snapshot(case, "model_attempts")[0] == before[0]
    assert original.observation_id != second.observation_id


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["hash", "rehashed_calls", "event_type"])
async def test_corrupted_exit_cannot_silently_remove_observation_from_scheduler(
    case: Any, tamper: str
) -> None:
    import json

    from noyra.core.errors import IntegrityError
    from noyra.core.types import canonical_json, content_hash

    case.observation("failed")
    review, provider = setup_review(
        case, [ProviderCallError("synthetic_failure", retryable=False, outcome_unknown=False)]
    )
    assert await review.run_due() == "epistemic_review_model_failed"
    with case.kernel.database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM events WHERE event_type='epistemic_round_closed'"
        ).fetchone()
        connection.execute("DROP TRIGGER prevent_event_immutable_update")
        if tamper == "hash":
            connection.execute(
                "UPDATE events SET payload_hash=? WHERE event_id=?", ("0" * 64, row["event_id"])
            )
        elif tamper == "event_type":
            connection.execute(
                "UPDATE events SET event_type='different_type' WHERE event_id=?", (row["event_id"],)
            )
        else:
            payload = json.loads(row["payload_json"])
            payload["calls"][0]["request_hash"] = "0" * 64
            connection.execute(
                "UPDATE events SET payload_json=?,payload_hash=? WHERE event_id=?",
                (canonical_json(payload), content_hash(payload), row["event_id"]),
            )
    with pytest.raises(IntegrityError):
        await review.run_due()
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_failed_exit_write_rolls_back_and_old_failure_is_closed_on_next_tick(
    case: Any,
) -> None:
    from unittest.mock import patch

    from noyra.cognition.epistemic_rounds import EpistemicRoundExit

    case.observation("failed")
    review, provider = setup_review(
        case, [ProviderCallError("synthetic_failure", retryable=False, outcome_unknown=False)]
    )
    with (
        patch.object(
            EpistemicRoundExit, "close", side_effect=RuntimeError("synthetic receipt failure")
        ),
        pytest.raises(RuntimeError),
    ):
        await review.run_due()
    before = snapshot(case, "model_calls")
    assert await review.run_due() == "epistemic_review_model_failed"
    assert review._context() is None
    assert snapshot(case, "model_calls") == before and len(provider.requests) == 1


@pytest.mark.asyncio
async def test_failed_exit_archive_and_restart_preserve_candidate_progress(
    case: Any, monkeypatch: Any
) -> None:
    import base64
    from datetime import datetime, timedelta
    from unittest.mock import patch

    from noyra.cognition import EpistemicReview
    from noyra.core import SubjectKernel
    from noyra.core.event_archive import EventPayloadArchive
    from noyra.core.types import content_hash

    case.observation("failed")
    review, provider = setup_review(
        case,
        [
            ProviderCallError("synthetic_failure", retryable=False, outcome_unknown=False),
            no_change(),
        ],
    )
    assert await review.run_due() == "epistemic_review_model_failed"
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
    case.clock_value = "2026-08-13T13:00:00.000+00:00"
    second = case.observation("second")
    restarted = EpistemicReview(
        case.kernel.database,
        case.subject_id,
        review.gateway,
        review.settings,
        clock=lambda: case.clock_value,
    )
    assert await restarted.run_due() == "epistemic_review_no_change"
    assert (
        snapshot(case, "epistemic_review_runs")[0]["trigger_observation_id"]
        == second.observation_id
    )
    assert len(provider.requests) == 2 and restarted.verify_integrity() == 1


@pytest.mark.asyncio
async def test_shared_registry_rejects_exit_receipt_tampering(case: Any) -> None:
    from noyra.core.integrity import IntegrityRegistry

    case.observation("failure")
    review, _ = setup_review(
        case, [ProviderCallError("synthetic_failure", retryable=False, outcome_unknown=False)]
    )
    assert await review.run_due() == "epistemic_review_model_failed"

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

    good = registry()
    assert not good.p0 and not good.p1
    with case.kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_event_immutable_update")
        connection.execute(
            "UPDATE events SET payload_hash=? WHERE event_type='epistemic_round_closed'",
            ("0" * 64,),
        )
    bad = registry()
    assert bad.p0 or bad.p1


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_routed_epistemic_failure_exits_but_unknown_never_rotates_or_closes(
    case: Any, unknown: bool
) -> None:
    from noyra.model import RoutedModelGateway
    from test_audit_20261022_remediation import resources_for

    original = case.observation("routed")
    review, provider = setup_review(
        case,
        [
            ProviderCallError(
                "provider_outcome_unknown" if unknown else "provider_auth_failed",
                retryable=False,
                outcome_unknown=unknown,
            ),
            no_change(),
        ],
    )
    resources, _ = resources_for(
        case.kernel.database, case.subject_id, case.kernel.database.path.parent, pool="deep"
    )
    review.gateway = RoutedModelGateway(
        case.kernel.database, case.subject_id, resources, provider_factory=lambda _: provider
    )
    assert await review.run_due() == "epistemic_review_model_failed"
    before = snapshot(case, "model_calls"), snapshot(case, "model_attempts")
    if unknown:
        for _ in range(2):
            assert await review.run_due() == "epistemic_review_model_failed"
        assert before == (snapshot(case, "model_calls"), snapshot(case, "model_attempts"))
        call = review.gateway.ledger.unknown_calls(case.subject_id)[0]
        review.gateway.ledger.prepare_unknown_retry(
            call.call_id, actor="operator", reason="Exact routed retry"
        )
        assert await review.run_due() == "epistemic_review_no_change"
        assert snapshot(case, "model_calls")[0]["call_id"] == call.call_id
        assert snapshot(case, "model_attempts")[0] == before[1][0]
        assert len(provider.requests) == 2
    else:
        assert review._context() is None
        assert len(provider.requests) == 1
        with case.kernel.database.read_transaction() as connection:
            receipt = connection.execute(
                "SELECT event_id FROM events WHERE event_type='epistemic_round_closed'"
            ).fetchone()
            assert receipt and receipt[0].endswith(original.observation_id)


@pytest.mark.asyncio
async def test_exit_observation_remains_eligible_evidence_in_next_valid_review(case: Any) -> None:
    from noyra.mind import BeliefStore

    first = case.observation("old evidence")
    belief = BeliefStore(case.kernel.database).create(
        case.subject_id,
        "Bounded proposition",
        confidence=0.5,
        scope="fixture",
        supporting_event_ids=(first.event_id,),
    )
    proposal = no_change()
    proposal["belief_revisions"] = [
        {
            "belief_id": belief.belief_id,
            "disposition": "strengthen",
            "proposition": belief.proposition,
            "confidence": 0.6,
            "supporting_observation_ids": [first.observation_id],
            "counter_observation_ids": [],
            "reason": "Still valid analyzed evidence despite an earlier model failure",
        }
    ]
    review, provider = setup_review(
        case,
        [ProviderCallError("synthetic_failure", retryable=False, outcome_unknown=False), proposal],
    )
    assert await review.run_due() == "epistemic_review_model_failed"
    case.clock_value = "2026-08-13T13:00:00.000+00:00"
    second = case.observation("new trigger")
    assert await review.run_due() == "epistemic_review_committed"
    assert BeliefStore(case.kernel.database).get(belief.belief_id).confidence == 0.6
    assert (
        snapshot(case, "epistemic_review_runs")[0]["trigger_observation_id"]
        == second.observation_id
    )
    assert len(provider.requests) == 2 and review.verify_integrity() == 1
