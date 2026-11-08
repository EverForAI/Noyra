from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.admission import OperationInvalidated, RuntimeAdmissionGate, bind_lease
from noyra.core.types import content_hash
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelPricing,
    ModelUsage,
    ProviderResponse,
)
from noyra.model.types import GatewayResult
from test_external_dispatch_fencing import fence
from test_model_retry_cancellation import MESSAGES, ModelFixture, Output, _outcome


@pytest.fixture
def model(tmp_path: Path) -> ModelFixture:
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "model-invalidation"
    IdentityStore(database).ensure(subject, content_hash(subject))
    return ModelFixture(
        database,
        ModelLedger(database),
        subject,
        BudgetLimits(20, 100_000, 100_000, 10_000_000),
        ModelPricing(input_microusd_per_million=1_000_000, output_microusd_per_million=2_000_000),
    )


async def complete(model: ModelFixture, gateway: ModelGateway) -> GatewayResult[Output]:
    return await gateway.complete_structured(
        model.subject,
        "world_cognition:invalidation",
        MESSAGES,
        Output,
        idempotency_key="invalidation",
        max_output_tokens=20,
    )


async def no_backoff(_delay: float) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pause", "quarantine", "drain", "migration"])
async def test_epoch_invalid_after_prepare_finishes_unsent_without_masking_error(
    model: ModelFixture,
    kind: str,
) -> None:
    gate = RuntimeAdmissionGate(model.subject)
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    prepare = model.ledger.prepare_call

    def invalidated(*args: Any, **kwargs: Any) -> Any:
        result = prepare(*args, **kwargs)
        fence(gate, kind)
        return result

    with (
        gate.operation("prepare") as lease,
        bind_lease(lease),
        patch.object(model.ledger, "prepare_call", side_effect=invalidated),
        pytest.raises(OperationInvalidated),
    ):
        await complete(model, gateway)
    assert model.calls()[0]["status"] == "failed"
    assert model.calls()[0]["error_code"] == "runtime_epoch_invalidated"
    assert model.attempts() == [] and provider.requests == []
    assert model.ledger.budget_status(model.subject, model.limits).cost_microusd == 0


@pytest.mark.asyncio
async def test_epoch_invalid_at_authorization_entry_finishes_without_attempt(
    model: ModelFixture,
) -> None:
    gate = RuntimeAdmissionGate(model.subject)
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    authorize = model.ledger.authorize_attempt

    def invalidated(*args: Any, **kwargs: Any) -> Any:
        gate.invalidate()
        return authorize(*args, **kwargs)

    with (
        gate.operation("authorize") as lease,
        bind_lease(lease),
        patch.object(model.ledger, "authorize_attempt", side_effect=invalidated),
        pytest.raises(OperationInvalidated),
    ):
        await complete(model, gateway)
    assert model.calls()[0]["status"] == "failed"
    assert model.calls()[0]["error_code"] == "runtime_epoch_invalidated"
    assert model.attempts() == [] and provider.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["known_failure", "invalid_output", "unknown_usage", "estimated_usage"]
)
async def test_epoch_invalid_between_settled_attempts_preserves_all_accounting(
    model: ModelFixture, kind: str
) -> None:
    gate = RuntimeAdmissionGate(model.subject)
    saved: list[dict[str, Any]] = []

    async def backoff(_delay: float) -> None:
        saved.extend(model.attempts())
        gate.invalidate()

    provider = FakeProvider([_outcome(kind)])
    gateway = model.gateway(provider, backoff)
    with gate.operation("retry") as lease, bind_lease(lease), pytest.raises(OperationInvalidated):
        await complete(model, gateway)
    assert model.calls()[0]["status"] == "failed"
    assert model.calls()[0]["error_code"] == "runtime_epoch_invalidated"
    assert len(saved) == 1 and model.attempts() == saved
    budget = model.ledger.budget_status(model.subject, model.limits)
    if kind == "known_failure":
        assert budget.cost_microusd == 0
    elif kind == "invalid_output":
        assert budget.cost_microusd == 13
    else:
        assert budget.cost_microusd == saved[0]["reserved_cost_microusd"] > 0
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_invalid_lease_before_prepare_creates_no_call(model: ModelFixture) -> None:
    gate = RuntimeAdmissionGate(model.subject)
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    with gate.operation("early") as lease, bind_lease(lease):
        gate.invalidate()
        with pytest.raises(OperationInvalidated):
            await complete(model, gateway)
    assert not model.calls() and not model.attempts() and not provider.requests


@pytest.mark.asyncio
async def test_authorization_transaction_rollback_leaves_no_attempt(model: ModelFixture) -> None:
    gate = RuntimeAdmissionGate(model.subject)
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    expired = OperationInvalidated("synthetic authorization rollback")

    def abort(connection: Any, attempt_id: str) -> Any:
        assert (
            connection.execute(
                "SELECT status FROM model_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()[0]
            == "authorized"
        )
        gate.invalidate()
        raise expired

    with (
        gate.operation("rollback") as lease,
        bind_lease(lease),
        patch.object(model.ledger, "_load_attempt_connection", side_effect=abort),
        pytest.raises(OperationInvalidated) as raised,
    ):
        await complete(model, gateway)
    assert raised.value is expired
    assert model.calls()[0]["status"] == "failed"
    assert not model.attempts() and not provider.requests
    assert model.ledger.budget_status(model.subject, model.limits).attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["authorized", "admitted", "started"])
async def test_epoch_invalid_after_authorization_cancels_only_unsent_attempt(
    model: ModelFixture, phase: str
) -> None:
    from contextlib import ExitStack

    from noyra.core.model_admission import track_model_admission

    gate = RuntimeAdmissionGate(model.subject)
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    with ExitStack() as stack:
        if phase == "admitted":

            def invalidate() -> None:
                gate.invalidate()

            stack.enter_context(track_model_admission(invalidate))
        else:
            method = "authorize_attempt" if phase == "authorized" else "start_attempt"
            original = getattr(model.ledger, method)

            def invalidated(*args: Any, **kwargs: Any) -> Any:
                result = original(*args, **kwargs)
                gate.invalidate()
                return result

            stack.enter_context(patch.object(model.ledger, method, side_effect=invalidated))
        with (
            gate.operation("unsent") as lease,
            bind_lease(lease),
            pytest.raises(OperationInvalidated),
        ):
            await complete(model, gateway)
    assert model.calls()[0]["status"] == "failed"
    assert [row["status"] for row in model.attempts()] == ["cancelled"]
    assert not provider.requests
    budget = model.ledger.budget_status(model.subject, model.limits)
    assert budget.cost_microusd == budget.input_tokens == budget.output_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["known_failure", "invalid_output", "unknown_usage", "estimated_usage"]
)
async def test_epoch_invalid_immediately_after_settlement_never_turns_call_unknown(
    model: ModelFixture, kind: str
) -> None:
    gate = RuntimeAdmissionGate(model.subject)
    provider = FakeProvider([_outcome(kind)])
    gateway = model.gateway(provider, no_backoff)
    finish = model.ledger.finish_attempt
    saved: list[dict[str, Any]] = []

    def invalidated(*args: Any, **kwargs: Any) -> Any:
        result = finish(*args, **kwargs)
        saved.extend(model.attempts())
        gate.invalidate()
        return result

    with (
        gate.operation("settled") as lease,
        bind_lease(lease),
        patch.object(model.ledger, "finish_attempt", side_effect=invalidated),
        pytest.raises(OperationInvalidated),
    ):
        await complete(model, gateway)
    assert model.calls()[0]["status"] == "failed"
    assert saved == model.attempts() and len(saved) == 1
    assert len(provider.requests) == 1
    budget = model.ledger.budget_status(model.subject, model.limits)
    expected = (
        0
        if kind == "known_failure"
        else 13
        if kind == "invalid_output"
        else saved[0]["reserved_cost_microusd"]
    )
    assert budget.cost_microusd == expected


@pytest.mark.asyncio
async def test_retry_authorization_failure_keeps_previous_paid_attempt(model: ModelFixture) -> None:
    gate = RuntimeAdmissionGate(model.subject)
    provider = FakeProvider([_outcome("invalid_output")])
    gateway = model.gateway(provider, no_backoff)
    authorize = model.ledger.authorize_attempt
    saved: list[dict[str, Any]] = []
    count = 0

    def invalidated(*args: Any, **kwargs: Any) -> Any:
        nonlocal count
        count += 1
        if count == 2:
            saved.extend(model.attempts())
            gate.invalidate()
        return authorize(*args, **kwargs)

    with (
        gate.operation("retry-authorization") as lease,
        bind_lease(lease),
        patch.object(model.ledger, "authorize_attempt", side_effect=invalidated),
        pytest.raises(OperationInvalidated),
    ):
        await complete(model, gateway)
    assert model.calls()[0]["status"] == "failed"
    assert count == 2 and model.attempts() == saved
    assert model.ledger.budget_status(model.subject, model.limits).cost_microusd == 13
    assert len(provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["authorized", "executing", "unknown", "settled", "succeeded"])
async def test_stale_prepared_worker_cannot_rewrite_competing_worker_state(
    model: ModelFixture, state: str
) -> None:
    gate = RuntimeAdmissionGate(model.subject)
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    prepare = model.ledger.prepare_call
    saved_calls: list[dict[str, Any]] = []
    saved_attempts: list[dict[str, Any]] = []

    def advanced(*args: Any, **kwargs: Any) -> Any:
        result = prepare(*args, **kwargs)
        call = result[0]
        attempt = model.ledger.authorize_attempt(
            call.call_id,
            model.limits,
            reserved_input_tokens=10,
            reserved_output_tokens=5,
            reserved_cost_microusd=20,
        )
        if state != "authorized":
            model.ledger.start_attempt(attempt.attempt_id)
        if state in {"unknown", "settled", "succeeded"}:
            unknown = state == "unknown"
            model.ledger.finish_attempt(
                attempt.attempt_id,
                "unknown" if unknown else "failed" if state == "settled" else "succeeded",
                usage=None if unknown else ModelUsage(10, 5),
                cost_microusd=None if unknown else 20,
            )
            if state == "unknown":
                model.ledger.finish_call(call.call_id, "unknown", error_code="other_worker_unknown")
            elif state == "succeeded":
                model.ledger.finish_call(
                    call.call_id, "succeeded", response={"content": '{"ok":true}'}
                )
        saved_calls.extend(model.calls())
        saved_attempts.extend(model.attempts())
        gate.invalidate()
        return result

    with (
        gate.operation("stale-worker") as lease,
        bind_lease(lease),
        patch.object(model.ledger, "prepare_call", side_effect=advanced),
        pytest.raises(OperationInvalidated),
    ):
        await complete(model, gateway)
    assert model.calls() == saved_calls and model.attempts() == saved_attempts
    assert not provider.requests
    assert model.ledger.budget_status(model.subject, model.limits).cost_microusd == 20


@pytest.mark.asyncio
async def test_stale_retry_cannot_claim_another_workers_settled_attempt(
    model: ModelFixture,
) -> None:
    gate = RuntimeAdmissionGate(model.subject)
    saved_calls: list[dict[str, Any]] = []
    saved_attempts: list[dict[str, Any]] = []

    async def competing_retry(_delay: float) -> None:
        original = model.calls()[0]
        attempt = model.ledger.authorize_attempt(
            original["call_id"],
            model.limits,
            continuation=True,
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=1,
        )
        model.ledger.start_attempt(attempt.attempt_id)
        model.ledger.finish_attempt(
            attempt.attempt_id, "failed", usage=ModelUsage(0, 0), cost_microusd=0
        )
        saved_calls.extend(model.calls())
        saved_attempts.extend(model.attempts())
        gate.invalidate()

    provider = FakeProvider([_outcome("known_failure")])
    gateway = model.gateway(provider, competing_retry)
    with (
        gate.operation("retry-race") as lease,
        bind_lease(lease),
        pytest.raises(OperationInvalidated),
    ):
        await complete(model, gateway)
    assert model.calls() == saved_calls and model.attempts() == saved_attempts
    assert len(saved_attempts) == 2 and len(provider.requests) == 1


@pytest.mark.asyncio
async def test_possibly_sent_invalidated_request_stays_unknown_with_reservation(
    model: ModelFixture,
) -> None:
    gate = RuntimeAdmissionGate(model.subject)
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    expired = OperationInvalidated("synthetic invalidation after dispatch")
    before: list[Any] = []

    async def in_flight(request: Any) -> ProviderResponse:
        provider.requests.append(request)
        before.append(model.ledger.budget_status(model.subject, model.limits))
        gate.invalidate()
        raise expired

    with (
        gate.operation("sent") as lease,
        bind_lease(lease),
        patch.object(provider, "complete", side_effect=in_flight),
        pytest.raises(OperationInvalidated) as raised,
    ):
        await complete(model, gateway)
    assert raised.value is expired
    assert model.calls()[0]["status"] == model.attempts()[0]["status"] == "unknown"
    assert model.ledger.budget_status(model.subject, model.limits) == before[0]
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_original_invalidation_instance_is_preserved(model: ModelFixture) -> None:
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    expired = OperationInvalidated("original before attempt")
    with (
        patch("noyra.model.gateway.assert_current_lease", side_effect=expired),
        pytest.raises(OperationInvalidated) as raised,
    ):
        await complete(model, gateway)
    assert raised.value is expired
    assert model.calls()[0]["status"] == "failed" and not model.attempts()


@pytest.mark.asyncio
async def test_accounting_failure_is_visible_and_keeps_original_invalidation_context(
    model: ModelFixture,
) -> None:
    import sqlite3

    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    expired = OperationInvalidated("original before attempt")
    unavailable = sqlite3.OperationalError("synthetic terminal accounting failure")
    with (
        patch("noyra.model.gateway.assert_current_lease", side_effect=expired),
        patch.object(model.ledger, "fail_before_attempt", side_effect=unavailable),
        pytest.raises(sqlite3.OperationalError) as raised,
    ):
        await complete(model, gateway)
    assert raised.value is unavailable and unavailable.__context__ is expired
    assert model.calls()[0]["status"] == "prepared"
    assert not model.attempts() and not provider.requests


@pytest.mark.asyncio
async def test_failed_unsent_call_survives_reopen_without_implicit_retry(
    model: ModelFixture,
) -> None:
    from noyra.model.errors import ModelCallStateError

    gate = RuntimeAdmissionGate(model.subject)
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    prepare = model.ledger.prepare_call

    def invalidated(*args: Any, **kwargs: Any) -> Any:
        result = prepare(*args, **kwargs)
        gate.invalidate()
        return result

    with (
        gate.operation("before-restart") as lease,
        bind_lease(lease),
        patch.object(model.ledger, "prepare_call", side_effect=invalidated),
        pytest.raises(OperationInvalidated),
    ):
        await complete(model, gateway)
    saved = model.calls()
    reopened = Database(model.database.path)
    ledger = ModelLedger(reopened)
    assert ledger.recover_interrupted(model.subject) == []
    gateway = ModelGateway(
        provider, ledger, model="f03-model", limits=model.limits, pricing=model.pricing
    )
    with pytest.raises(ModelCallStateError):
        await complete(model, gateway)
    assert model.calls() == saved and not model.attempts() and not provider.requests


@pytest.mark.asyncio
async def test_invalidated_explicit_retry_preserves_original_unknown_fees_and_route_fence(
    model: ModelFixture,
) -> None:
    from decimal import Decimal

    from noyra.model import RoutedModelGateway
    from noyra.model.errors import ProviderCallError
    from test_audit_20261022_remediation import resources_for

    resources, _ = resources_for(
        model.database,
        model.subject,
        model.database.path.parent,
        pool="economy",
        input_usd_per_million=Decimal("1"),
        output_usd_per_million=Decimal("2"),
    )
    provider = FakeProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            ProviderResponse('{"ok":true}', ModelUsage(1, 1)),
        ]
    )
    router = RoutedModelGateway(
        model.database, model.subject, resources, provider_factory=lambda _: provider
    )
    with pytest.raises(ProviderCallError):
        await complete(model, router)
    call = router.ledger.unknown_calls(model.subject)[0]
    router.ledger.prepare_unknown_retry(
        call.call_id, actor="operator", reason="Original route only"
    )
    attempts = model.attempts()
    budget = model.ledger.budget_status(model.subject, model.limits)
    assert budget.cost_microusd > 0 and attempts[0]["status"] == "unknown"
    gate = RuntimeAdmissionGate(model.subject)
    prepare = ModelLedger.prepare_call

    def invalidated(ledger: ModelLedger, *args: Any, **kwargs: Any) -> Any:
        result = prepare(ledger, *args, **kwargs)
        if result[0].call_id == call.call_id:
            gate.invalidate()
        return result

    with (
        gate.operation("explicit-retry") as lease,
        bind_lease(lease),
        patch.object(ModelLedger, "prepare_call", new=invalidated),
        pytest.raises(OperationInvalidated),
    ):
        await complete(model, router)
    assert model.calls()[0]["status"] == "failed"
    assert model.attempts() == attempts
    assert model.ledger.budget_status(model.subject, model.limits) == budget
    with pytest.raises(ProviderCallError, match="quarantined"):
        await router.complete_structured(
            model.subject,
            call.purpose,
            MESSAGES,
            Output,
            idempotency_key="must-not-bypass-one-shot-retry",
            max_output_tokens=20,
        )
    assert len(provider.requests) == 1
