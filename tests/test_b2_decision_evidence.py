from __future__ import annotations

import base64
import json
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import test_goal_governance as governance_base
import test_intrinsic_thought as thought_base
import test_motivation_development as motivation_base
from noyra.cognition import IntrinsicThought, MotivationDevelopment
from noyra.cognition.decision_evidence import (
    diagnose_motivation_review,
    replay_motivation_validation,
)
from noyra.core import Database, EventStore
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.types import content_hash
from noyra.model import BudgetLimits, FakeProvider, ModelLedger, ModelUsage, ProviderResponse


@pytest.fixture
def fixture() -> Iterator[motivation_base.MotivationDevelopmentTestCase]:
    case = motivation_base.MotivationDevelopmentTestCase()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def rejected_development(
    case: motivation_base.MotivationDevelopmentTestCase,
) -> tuple[MotivationDevelopment, Any]:
    ids = case.establish_state()
    proposal = case.proposal(ids)
    values = proposal["values"]
    assert isinstance(values, list)
    values[0]["weight"] = 0.99
    gateway, provider = case.gateway([proposal])
    development = MotivationDevelopment(
        case.kernel.database,
        case.subject_id,
        gateway,
        case.settings().model_copy(update={"max_motivation_model_calls_per_day": 1}),
        clock=lambda: case.clock_value,
    )
    return development, provider


@pytest.mark.asyncio
async def test_rejection_records_original_policy_and_can_be_reproduced_read_only(
    fixture: motivation_base.MotivationDevelopmentTestCase,
) -> None:
    development, provider = rejected_development(fixture)
    assert await development.run_due() == "motivation_development_rejected"
    review = development.latest_review()
    assert review is not None
    readonly = Database(fixture.kernel.database.path, initialize=False, read_only=True)
    with readonly.connection() as connection:
        before = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    result = diagnose_motivation_review(readonly, fixture.subject_id, review.review_id)
    assert result["reason_codes"] == ["value_initial_strength"]
    assert result["offline_validation"] == "value_initial_strength"
    assert result["matches_recorded_reason"] is True
    assert result["validator_versions"] == ["motivation/v1"]
    assert development.values() == [] and development.missions() == []
    fixture.events.append(fixture.subject_id, "new_fact", "world", {"later": True})
    fixture.clock_value = (
        datetime.fromisoformat(fixture.clock_value) + timedelta(hours=2)
    ).isoformat()
    assert await development.run_due() is None
    assert len(provider.requests) == 1
    # Changing current settings does not change the original private receipt.
    development.settings = development.settings.model_copy(
        update={"minimum_mission_value_count": 8}
    )
    assert diagnose_motivation_review(readonly, fixture.subject_id, review.review_id) == result
    with readonly.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before + 1
    EventStore(fixture.kernel.database).verify_chain(fixture.subject_id)
    assert development.verify_integrity()["motivation_reviews"] == 1


@pytest.mark.asyncio
async def test_missing_historical_context_is_reported_as_insufficient_not_invented(
    fixture: motivation_base.MotivationDevelopmentTestCase,
) -> None:
    development, provider = rejected_development(fixture)
    with patch("noyra.cognition.motivation.append_rejection"):
        assert await development.run_due() == "motivation_development_rejected"
    review = development.latest_review()
    assert review is not None
    result = diagnose_motivation_review(
        fixture.kernel.database, fixture.subject_id, review.review_id
    )
    assert result["diagnosis"] == "historical_evidence_insufficient"
    assert result["reason_codes"] == [] and result["offline_validation"] is None
    assert result["response_available"] is True
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_rejection_receipt_and_review_are_atomic_and_recovery_uses_original_call(
    fixture: motivation_base.MotivationDevelopmentTestCase,
) -> None:
    development, provider = rejected_development(fixture)
    with (
        patch("noyra.cognition.motivation.append_rejection", side_effect=RuntimeError("disk")),
        pytest.raises(RuntimeError, match="disk"),
    ):
        await development.run_due()
    assert development.latest_review() is None
    assert len(provider.requests) == 1
    # The daily cap is now reached, but recording the existing rejected fact
    # must still recover without another model call.
    assert await development.run_due() == "motivation_development_rejected"
    assert len(provider.requests) == 1
    review = development.latest_review()
    assert review is not None
    assert (
        diagnose_motivation_review(fixture.kernel.database, fixture.subject_id, review.review_id)[
            "matches_recorded_reason"
        ]
        is True
    )


@pytest.mark.asyncio
async def test_private_diagnostic_cli_never_creates_or_writes_a_database(
    fixture: motivation_base.MotivationDevelopmentTestCase,
) -> None:
    development, _ = rejected_development(fixture)
    await development.run_due()
    review = development.latest_review()
    assert review is not None
    script = Path(__file__).resolve().parents[1] / "scripts" / "diagnose-motivation-review.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            str(fixture.kernel.database.path),
            "--subject",
            fixture.subject_id,
            "--review",
            review.review_id,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["diagnosis"] == "recorded_rejection"
    assert "Careful understanding" not in result.stdout
    with pytest.raises(ValueError, match="unavailable"):
        diagnose_motivation_review(fixture.kernel.database, "another-subject", review.review_id)


@pytest.mark.asyncio
async def test_intrinsic_rejection_saves_specific_reason_and_no_partial_goal() -> None:
    case = thought_base.IntrinsicThoughtTestCase()
    case.setUp()
    try:
        ids = case.establish_goal_and_affect()
        invalid = case.proposal(ids)
        invalid["source_event_ids"] = ["unavailable-event"]
        gateway, provider = case.gateway([invalid])
        thought = IntrinsicThought(
            case.kernel.database,
            case.subject_id,
            gateway,
            case.settings(),
            clock=lambda: case.clock_value,
        )
        assert await thought.run_due() == "intrinsic_thought_rejected"
        with case.kernel.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE subject_id=? AND "
                "event_type='intrinsic_decision_rejected'",
                (case.subject_id,),
            ).fetchall()
        assert len(rows) == 1
        receipt = EventStore(case.kernel.database).payload_from_row(rows[0])
        assert receipt["reason_code"] == "thought_evidence_unavailable"
        assert receipt["validation_context_hash"] == content_hash(receipt["validation_context"])
        assert thought.latest() is None
        with case.kernel.database.connection() as connection:
            agenda_id = connection.execute(
                "SELECT agenda_id FROM thought_round_closures"
            ).fetchone()[0]
        assert await thought.run_due(agenda_id=agenda_id) == "intrinsic_thought_target_not_due"
        assert len(provider.requests) == 1
        assert thought.verify_integrity()["thought_round_closures"] == 1
    finally:
        case.tearDown()


@pytest.mark.asyncio
async def test_governance_rejection_has_stable_specific_evidence_and_keeps_goals_unchanged() -> (
    None
):
    case = governance_base.GoalGovernanceTestCase()
    case.setUp()
    try:
        provider = FakeProvider(
            [ProviderResponse(case.proposal(goal_id="missing-goal"), ModelUsage(10, 10))]
        )
        governance = case.governance(provider, max_calls=1)
        assert await governance.run_due() == "goal_governance_rejected"
        with case.kernel.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE subject_id=? "
                "AND event_type='intrinsic_decision_rejected'",
                (case.subject_id,),
            ).fetchone()
        receipt = EventStore(case.kernel.database).payload_from_row(row)
        assert receipt["reason_code"] == "goal_unavailable_or_duplicate"
        assert receipt["context_role"] == "validation_time"
        assert receipt["validation_context_hash"] == content_hash(receipt["validation_context"])
        assert governance.latest() is None
        assert await governance.run_due() is None
        assert len(provider.requests) == 1
        with case.kernel.database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE subject_id=? "
                    "AND event_type='intrinsic_decision_rejected'",
                    (case.subject_id,),
                ).fetchone()[0]
                == 1
            )
        EventStore(case.kernel.database).verify_chain(case.subject_id)
    finally:
        await case.asyncTearDown()


@pytest.mark.asyncio
async def test_offline_replay_refuses_a_different_validator_policy(
    fixture: motivation_base.MotivationDevelopmentTestCase,
) -> None:
    development, _ = rejected_development(fixture)
    await development.run_due()
    with fixture.kernel.database.connection() as connection:
        row = connection.execute(
            "SELECT * FROM events WHERE subject_id=? AND event_type='intrinsic_decision_rejected'",
            (fixture.subject_id,),
        ).fetchone()
    receipt = EventStore(fixture.kernel.database).payload_from_row(row)
    call = development.gateway.ledger.get_call(receipt["model_call_id"])
    assert call.response is not None
    proposal = json.loads(call.response["content"])
    receipt["validation_context"]["policy"]["max_new_value_weight"] = 0.99
    assert replay_motivation_validation(receipt, proposal) == "validator_version_unavailable"


@pytest.mark.asyncio
async def test_affect_removed_during_thought_call_cannot_create_a_stale_goal() -> None:
    case = thought_base.IntrinsicThoughtTestCase()
    case.setUp()
    try:
        ids = case.establish_goal_and_affect()
        proposal = case.proposal(
            ids,
            goal_candidate={
                "title": "Explore this question",
                "description": "A real curiosity should support this candidate.",
                "priority": 0.6,
                "commitment": 0.5,
                "motive_emotion": "curiosity",
                "motive_target_type": "world",
                "motive_target_id": None,
            },
        )
        gateway, provider = case.gateway([proposal])
        original = provider.complete

        async def affect_changes(request: Any) -> Any:
            result = await original(request)
            with case.kernel.database.transaction() as connection:
                connection.execute(
                    "DELETE FROM affect_components WHERE subject_id=?", (case.subject_id,)
                )
            return result

        with patch.object(provider, "complete", side_effect=affect_changes):
            thought = IntrinsicThought(
                case.kernel.database,
                case.subject_id,
                gateway,
                case.settings(),
                clock=lambda: case.clock_value,
            )
            assert await thought.run_due() == "intrinsic_thought_rejected"
        assert thought.latest() is None
        with case.kernel.database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM goals WHERE subject_id=?", (case.subject_id,)
                ).fetchone()[0]
                == 1
            )
            row = connection.execute(
                "SELECT * FROM events WHERE subject_id=? "
                "AND event_type='intrinsic_decision_rejected'",
                (case.subject_id,),
            ).fetchone()
        receipt = EventStore(case.kernel.database).payload_from_row(row)
        assert receipt["reason_code"] == "thought_affect_missing"
        assert receipt["validation_context"]["live_affect_support"] is None
    finally:
        case.tearDown()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unknown", "executing", "succeeded"])
async def test_changed_context_does_not_reissue_unsettled_motivation_work(
    fixture: motivation_base.MotivationDevelopmentTestCase,
    status: str,
) -> None:
    development, provider = rejected_development(fixture)
    context = development._context()
    assert context is not None
    purpose = f"motivation_development:0:{context.source_state_hash[:16]}"
    ledger = ModelLedger(fixture.kernel.database)
    call, _ = ledger.prepare_call(
        fixture.subject_id,
        "fake",
        "fixture",
        purpose,
        content_hash("request"),
        f"motivation-development:0:{context.source_state_hash[:16]}",
    )
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(20, 200_000, 100_000, 1_000_000),
        reserved_input_tokens=1,
        reserved_output_tokens=1,
        reserved_cost_microusd=0,
    )
    ledger.start_attempt(attempt.attempt_id)
    if status != "executing":
        ledger.finish_attempt(
            attempt.attempt_id,
            status,
            usage=ModelUsage(1, 1) if status == "succeeded" else None,
            cost_microusd=0 if status == "succeeded" else None,
            error_code=None if status == "succeeded" else "unknown",
        )
        ledger.finish_call(
            call.call_id,
            status,
            response={"content": "{}"} if status == "succeeded" else None,
            error_code=None if status == "succeeded" else "unknown",
        )
    fixture.events.append(fixture.subject_id, "new_evidence", "world", {"later": True})
    fixture.clock_value = (
        datetime.fromisoformat(fixture.clock_value) + timedelta(days=1)
    ).isoformat()
    expected = "waiting_previous_context" if status == "succeeded" else "waiting_reconciliation"
    assert await development.run_due() == f"motivation_development_{expected}"
    assert provider.requests == []
    assert development.latest_review() is None


@pytest.mark.asyncio
async def test_archived_rejection_diagnostic_uses_local_read_only_archive(
    fixture: motivation_base.MotivationDevelopmentTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID", "b2-synthetic-archive-key")
    monkeypatch.delenv("NOYRA_ARCHIVE_KEYRING_PATH", raising=False)
    development, _ = rejected_development(fixture)
    await development.run_due()
    review = development.latest_review()
    assert review is not None
    expected = diagnose_motivation_review(
        fixture.kernel.database, fixture.subject_id, review.review_id
    )
    archive = EventPayloadArchive(
        fixture.kernel.database,
        fixture.kernel.database.path.parent / "subject" / "cold",
        subject_id=fixture.subject_id,
    )
    assert archive.archive_cold(fixture.subject_id, older_than_days=30) > 0
    readonly = Database(fixture.kernel.database.path, initialize=False, read_only=True)
    before = {
        str(path.relative_to(readonly.path.parent)) for path in readonly.path.parent.rglob("*")
    }
    assert diagnose_motivation_review(readonly, fixture.subject_id, review.review_id) == expected
    after = {
        str(path.relative_to(readonly.path.parent)) for path in readonly.path.parent.rglob("*")
    }
    assert before == after
