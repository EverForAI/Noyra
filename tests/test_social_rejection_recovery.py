from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.cognition import CognitionSettings, RelationshipSocialCognition
from noyra.core import SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash, utc_now
from noyra.model import FakeProvider
from noyra.model.errors import ProviderCallError
from test_audit_20261024_followup import social_fixture


@pytest.fixture
def owner(tmp_path: Path) -> Iterator[SubjectKernel]:
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", "social-recovery", content_hash("seed"))
    kernel.boot()
    kernel.orient()
    kernel.activate()
    try:
        yield kernel
    finally:
        kernel.close()


def scenario(owner: SubjectKernel) -> tuple[RelationshipSocialCognition, FakeProvider, list[str]]:
    social, provider, *_, response = social_fixture(owner)
    social.settings = CognitionSettings(social_review_interval_seconds=300)
    current = [datetime.fromisoformat(utc_now()).replace(minute=0, second=0).isoformat()]
    social.clock = lambda: current[0]
    invalid = response("request_help", "You must answer this request")
    provider._outcomes.clear()
    provider._outcomes.extend([invalid, response()])
    return social, provider, current


@pytest.mark.asyncio
async def test_determined_rejection_waits_without_revalidating_or_repaying(
    owner: SubjectKernel,
) -> None:
    social, provider, _ = scenario(owner)
    with patch.object(social, "_validate", wraps=social._validate) as validate:
        assert await social.run_due() == "relationship_social_rejected"
        first_validations = validate.call_count
        for _ in range(3):
            assert await social.run_due() == "relationship_social_waiting"
        assert validate.call_count == first_validations
    assert len(provider.requests) == 1
    with owner.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='relationship_social_rejected'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM relationship_social_runs").fetchone()[0] == 0
        )


@pytest.mark.asyncio
async def test_new_evidence_after_cooldown_starts_fresh_proposal_in_same_hour(
    owner: SubjectKernel,
) -> None:
    social, provider, current = scenario(owner)
    assert await social.run_due() == "relationship_social_rejected"
    social.events.append(owner.subject_id, "outcome_evaluated", "test", {"new": True})
    assert await social.run_due() == "relationship_social_waiting"
    current[0] = (datetime.fromisoformat(current[0]) + timedelta(seconds=300)).isoformat()
    assert await social.run_due() == "relationship_social_contact"
    assert len(provider.requests) == 2
    with owner.database.connection() as connection:
        calls = connection.execute("SELECT purpose,idempotency_key FROM model_calls").fetchall()
    assert len(calls) == 2 and calls[0][0] == calls[1][0] and calls[0][1] != calls[1][1]


@pytest.mark.asyncio
async def test_hour_rollover_does_not_bypass_unknown_social_call(owner: SubjectKernel) -> None:
    social, provider, current = scenario(owner)
    provider._outcomes[0] = ProviderCallError(
        "provider_outcome_unknown", retryable=False, outcome_unknown=True
    )
    assert await social.run_due() == "relationship_social_model_failed"
    current[0] = (datetime.fromisoformat(current[0]) + timedelta(hours=1)).isoformat()
    assert await social.run_due() == "relationship_social_model_failed"
    assert len(provider.requests) == 1
    with owner.database.connection() as connection:
        assert connection.execute("SELECT status FROM model_calls").fetchone()[0] == "unknown"


@pytest.mark.asyncio
async def test_success_recovery_does_not_need_another_daily_call(owner: SubjectKernel) -> None:
    social, provider, _ = scenario(owner)
    provider._outcomes.popleft()
    social.settings = CognitionSettings(
        social_review_interval_seconds=300, max_social_model_calls_per_day=1
    )
    with (
        patch.object(social, "_commit", side_effect=RuntimeError("local crash")),
        pytest.raises(RuntimeError),
    ):
        await social.run_due()
    assert await social.run_due() == "relationship_social_contact"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_unchanged_evidence_waits_but_next_hour_can_reconsider(owner: SubjectKernel) -> None:
    social, provider, current = scenario(owner)
    assert await social.run_due() == "relationship_social_rejected"
    current[0] = (datetime.fromisoformat(current[0]) + timedelta(minutes=10)).isoformat()
    assert await social.run_due() == "relationship_social_waiting"
    current[0] = (datetime.fromisoformat(current[0]) + timedelta(hours=1)).isoformat()
    assert await social.run_due() == "relationship_social_contact"
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_changed_evidence_does_not_bypass_daily_limit(owner: SubjectKernel) -> None:
    social, provider, current = scenario(owner)
    social.settings = CognitionSettings(
        social_review_interval_seconds=300, max_social_model_calls_per_day=1
    )
    assert await social.run_due() == "relationship_social_rejected"
    social.events.append(owner.subject_id, "outcome_evaluated", "test", {"new": True})
    current[0] = (datetime.fromisoformat(current[0]) + timedelta(minutes=5)).isoformat()
    assert await social.run_due() is None
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_rejection_survives_real_kernel_restart(owner: SubjectKernel) -> None:
    social, provider, current = scenario(owner)
    assert await social.run_due() == "relationship_social_rejected"
    owner.close()
    restarted = SubjectKernel(owner.database.path, owner.subject_id, content_hash("seed"))
    try:
        restarted.boot()
        if restarted.lifecycle.current().state == "booting":
            restarted.orient()
            restarted.activate()
        worker = RelationshipSocialCognition(
            restarted.database,
            owner.subject_id,
            social.gateway,
            social.settings,
            clock=lambda: current[0],
        )
        assert await worker.run_due() == "relationship_social_waiting"
        assert len(provider.requests) == 1
        worker.events.verify_chain(owner.subject_id)
    finally:
        restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["receipt", "response"])
async def test_corrupt_evidence_never_authorizes_replacement(
    owner: SubjectKernel, tamper: str
) -> None:
    social, provider, _ = scenario(owner)
    assert await social.run_due() == "relationship_social_rejected"
    with owner.database.transaction() as connection:
        if tamper == "receipt":
            connection.execute("DROP TRIGGER prevent_event_immutable_update")
            connection.execute(
                "UPDATE events SET payload_hash=? WHERE event_type='relationship_social_rejected'",
                ("0" * 64,),
            )
        else:
            connection.execute("UPDATE model_calls SET response_hash=?", ("0" * 64,))
    with pytest.raises(IntegrityError):
        await social.run_due()
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_same_route_authorized_retry_keeps_original_key_after_hour(
    owner: SubjectKernel,
) -> None:
    social, provider, current = scenario(owner)
    provider._outcomes[0] = ProviderCallError(
        "provider_outcome_unknown", retryable=False, outcome_unknown=True
    )
    assert await social.run_due() == "relationship_social_model_failed"
    with owner.database.connection() as connection:
        original = dict(connection.execute("SELECT * FROM model_calls").fetchone())
    social.gateway.ledger.prepare_unknown_retry(
        original["call_id"], actor="operator", reason="Explicit synthetic retry"
    )
    current[0] = (datetime.fromisoformat(current[0]) + timedelta(hours=1)).isoformat()
    assert await social.run_due() == "relationship_social_contact"
    assert len(provider.requests) == 2
    with owner.database.connection() as connection:
        rows = connection.execute(
            "SELECT call_id,purpose,idempotency_key FROM model_calls"
        ).fetchall()
        assert len(rows) == 1
        assert tuple(rows[0]) == tuple(
            original[key] for key in ("call_id", "purpose", "idempotency_key")
        )


@pytest.mark.asyncio
async def test_paid_success_after_hour_recovers_instead_of_repaying(owner: SubjectKernel) -> None:
    social, provider, current = scenario(owner)
    provider._outcomes.popleft()
    with (
        patch.object(social, "_commit", side_effect=RuntimeError("local crash")),
        pytest.raises(RuntimeError),
    ):
        await social.run_due()
    current[0] = (datetime.fromisoformat(current[0]) + timedelta(hours=1)).isoformat()
    assert await social.run_due() == "relationship_social_contact"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_concurrent_social_worker_yields_without_duplicate_request(
    owner: SubjectKernel,
) -> None:
    social, provider, _ = scenario(owner)
    provider._outcomes.popleft()
    other = RelationshipSocialCognition(
        owner.database, owner.subject_id, social.gateway, social.settings, clock=social.clock
    )
    complete = provider.complete

    async def concurrent(request: Any) -> Any:
        assert await other.run_due() == "relationship_social_waiting"
        return await complete(request)

    with patch.object(provider, "complete", side_effect=concurrent):
        assert await social.run_due() == "relationship_social_contact"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_unknown_appearing_at_model_preparation_rolls_back_new_call(
    owner: SubjectKernel,
) -> None:
    social, provider, _ = scenario(owner)
    provider._outcomes.popleft()
    prepare = social.gateway.ledger.prepare_call
    blocker_id = ""

    def concurrent(*args: Any, **kwargs: Any) -> Any:
        nonlocal blocker_id
        # A separate writer does not inherit this worker's context-local guard.
        with patch("noyra.model.ledger.notify_model_preparation"):
            blocker, _ = prepare(
                owner.subject_id,
                "fake",
                "test-model",
                "relationship_social:old-round",
                content_hash("old request"),
                "independent-unknown",
            )
        blocker_id = blocker.call_id
        attempt = social.gateway.ledger.authorize_attempt(
            blocker_id,
            social.gateway.limits,
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=1,
        )
        social.gateway.ledger.start_attempt(attempt.attempt_id)
        social.gateway.ledger.finish_attempt(
            attempt.attempt_id,
            "unknown",
            usage=None,
            cost_microusd=None,
            error_code="synthetic_unknown",
        )
        social.gateway.ledger.finish_call(blocker_id, "unknown", error_code="synthetic_unknown")
        return prepare(*args, **kwargs)

    with patch.object(social.gateway.ledger, "prepare_call", side_effect=concurrent):
        assert await social.run_due() == "relationship_social_model_failed"
    assert not provider.requests and blocker_id
    with owner.database.connection() as connection:
        rows = connection.execute("SELECT call_id,status FROM model_calls").fetchall()
        assert len(rows) == 1 and tuple(rows[0]) == (blocker_id, "unknown")


@pytest.mark.asyncio
async def test_paused_subject_cannot_recover_or_send_paid_success(owner: SubjectKernel) -> None:
    social, provider, _ = scenario(owner)
    provider._outcomes.popleft()
    with (
        patch.object(social, "_commit", side_effect=RuntimeError("local crash")),
        pytest.raises(RuntimeError),
    ):
        await social.run_due()
    owner.lifecycle.transition("paused", "synthetic pause", actor="operator")
    assert await social.run_due() == "relationship_social_model_failed"
    assert len(provider.requests) == 1 and social.latest() is None


@pytest.mark.asyncio
async def test_preupgrade_paid_rejection_gets_one_receipt_then_can_advance(
    owner: SubjectKernel,
) -> None:
    from dataclasses import asdict

    from noyra.cognition.types import RelationshipSocialProposal

    social, provider, current = scenario(owner)
    context = social._context()
    assert context is not None
    route_hash = content_hash(
        {
            "relationships": sorted(context.relationships),
            "routes": {key: asdict(route) for key, route in context.routes.items()},
        }
    )
    purpose = f"relationship_social:{current[0][:13]}:route:{route_hash}"
    await social.gateway.complete_structured(
        owner.subject_id,
        purpose,
        social._messages(context),
        RelationshipSocialProposal,
        idempotency_key=f"relationship-social:{current[0][:13]}:{route_hash}",
        max_output_tokens=2000,
        temperature=social.settings.temperature,
    )
    assert await social.run_due() == "relationship_social_rejected"
    assert await social.run_due() == "relationship_social_waiting"
    social.events.append(owner.subject_id, "outcome_evaluated", "test", {"new": True})
    current[0] = (datetime.fromisoformat(current[0]) + timedelta(minutes=5)).isoformat()
    assert await social.run_due() == "relationship_social_contact"
    assert len(provider.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_routed_rounds_preserve_rejection_and_unknown_fences(
    owner: SubjectKernel, unknown: bool
) -> None:
    from noyra.model import RoutedModelGateway
    from test_audit_20261022_remediation import resources_for

    social, provider, current = scenario(owner)
    resources, _ = resources_for(
        owner.database, owner.subject_id, owner.database.path.parent, pool="deep"
    )
    social.gateway = RoutedModelGateway(
        owner.database, owner.subject_id, resources, provider_factory=lambda _: provider
    )
    if unknown:
        provider._outcomes[0] = ProviderCallError(
            "provider_outcome_unknown", retryable=False, outcome_unknown=True
        )
        assert await social.run_due() == "relationship_social_model_failed"
        with owner.database.connection() as connection:
            before = [dict(row) for row in connection.execute("SELECT * FROM model_attempts")]
        current[0] = (datetime.fromisoformat(current[0]) + timedelta(hours=1)).isoformat()
        # A replacement router also cannot redispatch the old unknown result.
        social.gateway = RoutedModelGateway(
            owner.database, owner.subject_id, resources, provider_factory=lambda _: provider
        )
        assert await social.run_due() == "relationship_social_model_failed"
        assert len(provider.requests) == 1
        with owner.database.connection() as connection:
            assert before == [
                dict(row) for row in connection.execute("SELECT * FROM model_attempts")
            ]
        call = social.gateway.ledger.unknown_calls(owner.subject_id)[0]
        social.gateway.ledger.prepare_unknown_retry(
            call.call_id, actor="operator", reason="Explicit routed retry"
        )
        assert await social.run_due() == "relationship_social_contact"
        assert len(provider.requests) == 2
        with owner.database.connection() as connection:
            rows = connection.execute("SELECT call_id FROM model_calls").fetchall()
            assert len(rows) == 1 and rows[0][0] == call.call_id
    else:
        assert await social.run_due() == "relationship_social_rejected"
        assert await social.run_due() == "relationship_social_waiting"
        social.events.append(owner.subject_id, "outcome_evaluated", "test", {"new": True})
        current[0] = (datetime.fromisoformat(current[0]) + timedelta(minutes=5)).isoformat()
        assert await social.run_due() == "relationship_social_contact"
        assert len(provider.requests) == 2
        with owner.database.connection() as connection:
            rows = connection.execute("SELECT resource_group_id FROM model_calls").fetchall()
            assert len(rows) == 2 and all(row[0] for row in rows)


@pytest.mark.asyncio
async def test_consumed_unknown_retry_waits_until_explicitly_closed(owner: SubjectKernel) -> None:
    social, provider, current = scenario(owner)
    provider._outcomes[0] = ProviderCallError(
        "provider_outcome_unknown", retryable=False, outcome_unknown=True
    )
    assert await social.run_due() == "relationship_social_model_failed"
    with owner.database.connection() as connection:
        call_id = connection.execute("SELECT call_id FROM model_calls").fetchone()[0]
    social.gateway.ledger.prepare_unknown_retry(call_id, actor="operator", reason="Explicit retry")
    good = provider._outcomes[0]
    provider._outcomes[0] = ProviderCallError(
        "synthetic_failure", retryable=False, outcome_unknown=False
    )
    current[0] = (datetime.fromisoformat(current[0]) + timedelta(hours=1)).isoformat()
    assert await social.run_due() == "relationship_social_model_failed"
    assert await social.run_due() == "relationship_social_model_failed"
    assert len(provider.requests) == 2
    social.gateway.ledger.close_failed_unknown_retry(
        call_id,
        actor="operator",
        reason="Close retry",
        resolution_evidence="synthetic-provider-record",
        accept_duplicate_risk=True,
    )
    provider._outcomes.append(good)
    assert await social.run_due() == "relationship_social_contact"
    assert len(provider.requests) == 3


@pytest.mark.asyncio
async def test_rejection_receipt_is_read_after_real_encrypted_archive(
    owner: SubjectKernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    from noyra.core.event_archive import EventPayloadArchive

    social, provider, _ = scenario(owner)
    assert await social.run_due() == "relationship_social_rejected"
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )

    class Future(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Future:
            value = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(value.timestamp(), tz)

    archive = EventPayloadArchive(
        owner.database, owner.database.path.parent / "subject/cold", subject_id=owner.subject_id
    )
    with patch("noyra.core.event_archive.datetime", Future):
        assert archive.archive_cold(owner.subject_id, older_than_days=90) > 0
    with owner.database.connection() as connection:
        row = connection.execute(
            "SELECT payload_json,payload_archive_key FROM events "
            "WHERE event_type='relationship_social_rejected'"
        ).fetchone()
        assert row is not None and row[0] == "{}" and row[1]
    assert await social.run_due() == "relationship_social_waiting"
    assert len(provider.requests) == 1
    social.events.verify_chain(owner.subject_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("original_routed", [False, True])
async def test_gateway_kind_change_cannot_move_an_authorized_retry(
    owner: SubjectKernel, original_routed: bool
) -> None:
    from noyra.model import BudgetLimits, ModelGateway, ModelLedger, RoutedModelGateway
    from test_audit_20261022_remediation import resources_for

    social, provider, _ = scenario(owner)
    resources, _ = resources_for(
        owner.database, owner.subject_id, owner.database.path.parent, pool="deep"
    )
    direct = social.gateway
    routed = RoutedModelGateway(
        owner.database, owner.subject_id, resources, provider_factory=lambda _: provider
    )
    social.gateway = routed if original_routed else direct
    provider._outcomes[0] = ProviderCallError(
        "provider_outcome_unknown", retryable=False, outcome_unknown=True
    )
    assert await social.run_due() == "relationship_social_model_failed"
    call = social.gateway.ledger.unknown_calls(owner.subject_id)[0]
    social.gateway.ledger.prepare_unknown_retry(
        call.call_id, actor="operator", reason="Original only"
    )
    social.gateway = (
        ModelGateway(
            provider,
            ModelLedger(owner.database),
            model="replacement-model",
            limits=BudgetLimits(20, 500000, 100000, 5000000),
        )
        if original_routed
        else routed
    )
    assert await social.run_due() in {
        "relationship_social_context_changed",
        "relationship_social_model_failed",
    }
    assert len(provider.requests) == 1
    with owner.database.connection() as connection:
        rows = connection.execute("SELECT call_id,status FROM model_calls").fetchall()
        assert len(rows) == 1 and tuple(rows[0]) == (call.call_id, "prepared")
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_explicitly_reconciled_success_recovers_original_paid_result(
    owner: SubjectKernel,
) -> None:
    from noyra.model import ProviderResponse

    social, provider, current = scenario(owner)
    provider._outcomes[0] = ProviderCallError(
        "provider_outcome_unknown", retryable=False, outcome_unknown=True
    )
    assert await social.run_due() == "relationship_social_model_failed"
    call = social.gateway.ledger.unknown_calls(owner.subject_id)[0]
    outcome = provider._outcomes[0]
    assert isinstance(outcome, ProviderResponse)
    social.gateway.ledger.reconcile_unknown(
        call.call_id,
        outcome="succeeded",
        actor="operator",
        reason="Synthetic provider record",
        response={"content": outcome.content},
    )
    with owner.database.connection() as connection:
        attempts = [dict(row) for row in connection.execute("SELECT * FROM model_attempts")]
    current[0] = (datetime.fromisoformat(current[0]) + timedelta(hours=1)).isoformat()
    assert await social.run_due() == "relationship_social_contact"
    assert len(provider.requests) == 1
    with owner.database.connection() as connection:
        assert attempts == [dict(row) for row in connection.execute("SELECT * FROM model_attempts")]
    latest = social.latest()
    assert latest is not None and latest.model_call_id == call.call_id


@pytest.mark.asyncio
async def test_rehashed_receipt_must_still_match_successful_call(owner: SubjectKernel) -> None:
    import json

    from noyra.core.types import canonical_json

    social, provider, _ = scenario(owner)
    assert await social.run_due() == "relationship_social_rejected"
    with owner.database.transaction() as connection:
        row = connection.execute(
            "SELECT event_id,payload_json FROM events "
            "WHERE event_type='relationship_social_rejected'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["response_hash"] = "0" * 64
        connection.execute("DROP TRIGGER prevent_event_immutable_update")
        connection.execute(
            "UPDATE events SET payload_json=?,payload_hash=? WHERE event_id=?",
            (canonical_json(payload), content_hash(payload), row["event_id"]),
        )
    with pytest.raises(IntegrityError, match="receipt is invalid"):
        await social.run_due()
    assert len(provider.requests) == 1
