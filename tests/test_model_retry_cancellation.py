from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.admission import OperationInvalidated, RuntimeAdmissionGate, bind_lease
from noyra.core.types import content_hash
from noyra.model import (
    BudgetLimits,
    CognitiveResourceGroupInput,
    CognitiveResourceStore,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelMessage,
    ModelPricing,
    ModelUsage,
    ProviderResponse,
    RetryPolicy,
    RoutedModelGateway,
)
from noyra.model.errors import ModelCallStateError, ProviderCallError


class Output(BaseModel):
    ok: bool


@dataclass
class ModelFixture:
    database: Database
    ledger: ModelLedger
    subject: str
    limits: BudgetLimits
    pricing: ModelPricing

    def gateway(
        self, provider: FakeProvider, sleep: Callable[[float], Awaitable[None]]
    ) -> ModelGateway:
        return ModelGateway(
            provider,
            self.ledger,
            model="f03-model",
            limits=self.limits,
            pricing=self.pricing,
            retry_policy=RetryPolicy(max_attempts=3),
            sleep=sleep,
        )

    def calls(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM model_calls")]

    def attempts(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM model_attempts ORDER BY attempt_number"
                )
            ]


@pytest.fixture
def model(tmp_path: Path) -> ModelFixture:
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-f03-cancel"
    IdentityStore(database).ensure(subject, content_hash({"fixture": subject}))
    return ModelFixture(
        database,
        ModelLedger(database),
        subject,
        BudgetLimits(20, 100_000, 100_000, 10_000_000),
        ModelPricing(input_microusd_per_million=1_000_000, output_microusd_per_million=2_000_000),
    )


MESSAGES = (ModelMessage(role="user", content="Synthetic retry cancellation fixture"),)


def _outcome(kind: str) -> ProviderCallError | ProviderResponse:
    if kind == "known_failure":
        return ProviderCallError("provider_http_429", retryable=True, outcome_unknown=False)
    if kind == "unknown_usage":
        return ProviderCallError(
            "synthetic_known_failure_unknown_usage",
            retryable=True,
            outcome_unknown=False,
            usage_unknown=True,
        )
    if kind == "estimated_usage":
        return ProviderResponse("not valid JSON", None, provider_request_id="fixture-estimated")
    return ProviderResponse("not valid JSON", ModelUsage(7, 3), provider_request_id="fixture-paid")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["known_failure", "invalid_output", "unknown_usage", "estimated_usage"]
)
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_cancel_during_retry_delay_finishes_call_and_keeps_attempt(
    model: ModelFixture,
    kind: str,
    repeat_cancel: bool,
) -> None:
    waiting = asyncio.Event()

    async def backoff(_delay: float) -> None:
        waiting.set()
        await asyncio.Future[None]()

    provider = FakeProvider([_outcome(kind), ProviderResponse('{"ok":true}', ModelUsage(2, 1))])
    gateway = model.gateway(provider, backoff)
    task = asyncio.create_task(
        gateway.complete_structured(
            model.subject,
            "f03",
            MESSAGES,
            Output,
            idempotency_key="cancel-during-backoff",
            max_output_tokens=20,
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        attempts = model.attempts()
        budget = model.ledger.budget_status(model.subject, model.limits)
        assert model.calls()[0]["status"] == "executing"
        assert len(attempts) == budget.attempts == 1
        if kind == "known_failure":
            assert attempts[0]["status"] == "failed"
            assert budget.input_tokens == budget.output_tokens == budget.cost_microusd == 0
        elif kind == "unknown_usage":
            assert attempts[0]["cost_microusd"] is None
            assert budget.cost_microusd == attempts[0]["reserved_cost_microusd"] > 0
        elif kind == "estimated_usage":
            assert attempts[0]["usage_source"] == "estimated"
            assert budget.cost_microusd == attempts[0]["reserved_cost_microusd"] > 0
        else:
            assert attempts[0]["status"] == "succeeded"
            assert attempts[0]["provider_request_id"] == "fixture-paid"
            assert budget.input_tokens == 7 and budget.output_tokens == 3
            assert budget.cost_microusd == 13
        assert task.cancel()
        if repeat_cancel:
            assert task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        call = model.calls()[0]
        assert call["status"] == "failed"
        assert call["error_code"] == "model_retry_cancelled"
        assert call["completed_at"] is not None
        assert call["response_json"] is None and call["response_hash"] is None
        assert model.attempts() == attempts
        assert model.ledger.budget_status(model.subject, model.limits) == budget
        assert len(provider.requests) == 1
        with pytest.raises(ModelCallStateError):
            await gateway.complete_structured(
                model.subject,
                "f03",
                MESSAGES,
                Output,
                idempotency_key="cancel-during-backoff",
                max_output_tokens=20,
            )
        assert len(provider.requests) == 1
        assert model.ledger.recover_interrupted(model.subject) == []
        assert model.calls()[0] == call
        assert model.attempts() == attempts
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_cancelling_later_backoff_preserves_all_earlier_paid_and_reserved_usage(
    model: ModelFixture,
) -> None:
    waiting = asyncio.Event()
    delays: list[float] = []

    async def backoff(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 2:
            waiting.set()
            await asyncio.Future[None]()

    provider = FakeProvider(
        [
            _outcome("invalid_output"),
            _outcome("unknown_usage"),
            ProviderResponse('{"ok":true}', ModelUsage(2, 1)),
        ]
    )
    gateway = model.gateway(provider, backoff)
    task = asyncio.create_task(
        gateway.complete_structured(
            model.subject,
            "f03",
            MESSAGES,
            Output,
            idempotency_key="later-retry",
            max_output_tokens=20,
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        attempts = model.attempts()
        budget = model.ledger.budget_status(model.subject, model.limits)
        assert [entry["status"] for entry in attempts] == ["succeeded", "failed"]
        assert budget.cost_microusd == 13 + attempts[1]["reserved_cost_microusd"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert model.calls()[0]["status"] == "failed"
        assert model.attempts() == attempts
        assert model.ledger.budget_status(model.subject, model.limits) == budget
        assert len(provider.requests) == 2
        assert model.ledger.recover_interrupted(model.subject) == []
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_request_in_flight_cancellation_remains_unknown_and_retains_reservation(
    model: ModelFixture,
) -> None:
    entered = asyncio.Event()

    class Provider(FakeProvider):
        async def complete(self, request: Any) -> ProviderResponse:
            self.requests.append(request)
            entered.set()
            await asyncio.Future[None]()
            raise AssertionError("in-flight provider fixture should have been cancelled")

    async def no_backoff(_delay: float) -> None:
        raise AssertionError("an in-flight cancellation must not enter retry backoff")

    provider = Provider([])
    gateway = model.gateway(provider, no_backoff)
    task = asyncio.create_task(
        gateway.complete_structured(
            model.subject,
            "f03",
            MESSAGES,
            Output,
            idempotency_key="in-flight",
            max_output_tokens=20,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        before = model.ledger.budget_status(model.subject, model.limits)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        call = model.calls()[0]
        attempt = model.attempts()[0]
        assert call["status"] == attempt["status"] == "unknown"
        assert call["error_code"] == attempt["error_code"] == "model_call_cancelled"
        assert attempt["cost_microusd"] is None
        assert model.ledger.budget_status(model.subject, model.limits) == before
        assert len(provider.requests) == 1
        with pytest.raises(ModelCallStateError):
            await gateway.complete_structured(
                model.subject,
                "f03",
                MESSAGES,
                Output,
                idempotency_key="in-flight",
                max_output_tokens=20,
            )
        assert len(provider.requests) == 1
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["known_failure", "invalid_output"])
async def test_normal_retry_still_succeeds_and_caches_without_resending(
    model: ModelFixture,
    kind: str,
) -> None:
    delays = []

    async def backoff(delay: float) -> None:
        delays.append(delay)

    provider = FakeProvider([_outcome(kind), ProviderResponse('{"ok":true}', ModelUsage(2, 1))])
    gateway = model.gateway(provider, backoff)
    result = await gateway.complete_structured(
        model.subject,
        "f03",
        MESSAGES,
        Output,
        idempotency_key="normal",
        max_output_tokens=20,
    )
    assert result.output.ok and result.attempts == 2 and not result.cached
    assert len(delays) == 1 and len(provider.requests) == 2
    assert result.cost_microusd == (13 if kind == "invalid_output" else 0) + 4
    repeated = await gateway.complete_structured(
        model.subject,
        "f03",
        MESSAGES,
        Output,
        idempotency_key="normal",
        max_output_tokens=20,
    )
    assert repeated.cached and repeated.output == result.output
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_cancellation_accounting_error_is_visible_and_not_claimed_as_settled(
    model: ModelFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    waiting = asyncio.Event()

    async def backoff(_delay: float) -> None:
        waiting.set()
        await asyncio.Future[None]()

    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("synthetic write failure during cancellation settlement")

    provider = FakeProvider([_outcome("invalid_output")])
    gateway = model.gateway(provider, backoff)
    task = asyncio.create_task(
        gateway.complete_structured(
            model.subject,
            "f03",
            MESSAGES,
            Output,
            idempotency_key="accounting-failure",
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        attempts = model.attempts()
        monkeypatch.setattr(model.ledger, "finish_call", unavailable)
        task.cancel()
        with pytest.raises(sqlite3.OperationalError, match="synthetic write failure"):
            await task
        assert model.calls()[0]["status"] == "executing"
        assert model.attempts() == attempts
        assert len(provider.requests) == 1
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["known_failure", "invalid_output"])
@pytest.mark.parametrize("fence", ["pause", "quarantine", "drain", "migration"])
async def test_backoff_cancellation_settles_after_lease_invalidation_without_sending(
    model: ModelFixture,
    kind: str,
    fence: str,
) -> None:
    waiting = asyncio.Event()

    async def backoff(_delay: float) -> None:
        waiting.set()
        await asyncio.Future[None]()

    gate = RuntimeAdmissionGate(model.subject)
    lease = gate.begin("f03-backoff")
    provider = FakeProvider([_outcome(kind), ProviderResponse('{"ok":true}', ModelUsage(1, 1))])
    gateway = model.gateway(provider, backoff)
    with bind_lease(lease):
        task = asyncio.create_task(
            gateway.complete_structured(
                model.subject,
                "f03",
                MESSAGES,
                Output,
                idempotency_key="cancel-fenced",
            )
        )
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        attempts = model.attempts()
        budget = model.ledger.budget_status(model.subject, model.limits)
        if fence == "quarantine":
            gate.quarantine()
        elif fence == "drain":
            gate.begin_drain()
        elif fence == "migration":
            with gate.migration_control_scope(), pytest.raises(OperationInvalidated):
                gate.fence_for_migration(timeout=0)
        else:
            gate.invalidate()
        with pytest.raises(OperationInvalidated):
            lease.assert_current()
        task.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert model.calls()[0]["status"] == "failed"
        assert model.calls()[0]["error_code"] == "model_retry_cancelled"
        assert model.attempts() == attempts
        assert model.ledger.budget_status(model.subject, model.limits) == budget
        assert len(provider.requests) == 1
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        gate.finish(lease)
    assert gate.active_operations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["known_failure", "invalid_output"])
async def test_outer_timeout_during_backoff_persists_terminal_call(
    model: ModelFixture,
    kind: str,
) -> None:
    waiting = asyncio.Event()

    async def backoff(_delay: float) -> None:
        waiting.set()
        await asyncio.Future[None]()

    provider = FakeProvider([_outcome(kind)])
    gateway = model.gateway(provider, backoff)
    task = asyncio.create_task(
        gateway.complete_structured(
            model.subject,
            "f03",
            MESSAGES,
            Output,
            idempotency_key="outer-timeout",
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, timeout=0)
        assert model.calls()[0]["status"] == "failed"
        assert model.calls()[0]["error_code"] == "model_retry_cancelled"
        assert len(provider.requests) == 1
        assert model.ledger.recover_interrupted(model.subject) == []
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


def _router(
    model: ModelFixture,
    tmp_path: Path,
    primary: FakeProvider,
    backup: FakeProvider,
    backoff: Callable[[float], Awaitable[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[RoutedModelGateway, CognitiveResourceStore, list[str]]:
    store = CognitiveResourceStore(model.database, tmp_path / "secrets")
    for name, priority in (("primary", 1), ("backup", 2)):
        store.configure(
            model.subject,
            CognitiveResourceGroupInput(
                pool="deep",
                label=name,
                model=name,
                base_url="https://fixture.example/v1",
                api_keys=(SecretStr("synthetic-key-one"), SecretStr("synthetic-key-two")),
                priority=priority,
                max_attempts=3,
                input_usd_per_million=Decimal("1"),
                output_usd_per_million=Decimal("2"),
            ),
            actor="operator",
        )
    constructed: list[str] = []

    def provider_factory(settings: Any) -> FakeProvider:
        constructed.append(settings.model)
        return primary if settings.model == "primary" else backup

    router = RoutedModelGateway(
        model.database, model.subject, store, provider_factory=provider_factory
    )
    build_gateway = router._gateway

    def gateway_with_controlled_delay(*args: Any, **kwargs: Any) -> ModelGateway:
        gateway = build_gateway(*args, **kwargs)
        gateway._sleep = backoff
        return gateway

    monkeypatch.setattr(router, "_gateway", gateway_with_controlled_delay)
    return router, store, constructed


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["known_failure", "invalid_output"])
async def test_routed_cancellation_does_not_rotate_key_or_group_and_releases_logical_lock(
    model: ModelFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    waiting = asyncio.Event()

    async def backoff(_delay: float) -> None:
        waiting.set()
        await asyncio.Future[None]()

    primary = FakeProvider([_outcome(kind), ProviderResponse('{"ok":true}', ModelUsage(1, 1))])
    backup = FakeProvider([ProviderResponse('{"ok":true}', ModelUsage(1, 1))])
    router, store, constructed = _router(model, tmp_path, primary, backup, backoff, monkeypatch)
    purpose, logical_key = "f03:routed", "cancelled-logical-round"
    task = asyncio.create_task(
        router.complete_structured(
            model.subject,
            purpose,
            MESSAGES,
            Output,
            idempotency_key=logical_key,
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        attempts = model.attempts()
        budget = model.ledger.budget_status(model.subject, model.limits)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert model.calls()[0]["status"] == "failed"
        assert model.calls()[0]["error_code"] == "model_retry_cancelled"
        assert model.attempts() == attempts
        assert model.ledger.budget_status(model.subject, model.limits) == budget
        assert len(primary.requests) == 1 and backup.requests == []
        assert constructed == ["primary"]
        assert router._logical_route_state(purpose, "deep", logical_key) is None
        assert router.ledger.recover_interrupted(model.subject) == []
        store.verify_routing_integrity(model.subject)
        # A distinct explicit request proves the cancellation released both logical
        # route locks; it must not report routed_model_call_in_progress.
        result = await router.complete_structured(
            model.subject,
            purpose,
            MESSAGES,
            Output,
            idempotency_key="different-explicit-round",
        )
        assert result.output.ok and len(primary.requests) == 2 and backup.requests == []
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["known_failure", "invalid_output"])
async def test_cancelled_operator_retry_does_not_discard_old_unknown_or_grant_more_sends(
    model: ModelFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    waiting = asyncio.Event()

    async def backoff(_delay: float) -> None:
        waiting.set()
        await asyncio.Future[None]()

    primary = FakeProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            _outcome(kind),
            ProviderResponse('{"ok":true}', ModelUsage(1, 1)),
        ]
    )
    backup = FakeProvider([ProviderResponse('{"ok":true}', ModelUsage(1, 1))])
    router, store, _constructed = _router(model, tmp_path, primary, backup, backoff, monkeypatch)
    purpose, logical_key = "f03:operator-retry", "operator-authorized-round"
    with pytest.raises(ProviderCallError):
        await router.complete_structured(
            model.subject,
            purpose,
            MESSAGES,
            Output,
            idempotency_key=logical_key,
        )
    call_id = model.calls()[0]["call_id"]
    original_attempt = model.attempts()[0]
    assert original_attempt["status"] == "unknown"
    router.ledger.prepare_unknown_retry(
        call_id, actor="operator", reason="synthetic reviewed retry"
    )
    with model.database.connection() as connection:
        audit_before = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM audit_records WHERE action LIKE 'model_unknown_retry_%'"
            )
        ]
    task = asyncio.create_task(
        router.complete_structured(
            model.subject,
            purpose,
            MESSAGES,
            Output,
            idempotency_key=logical_key,
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        attempts = model.attempts()
        assert len(attempts) == 2 and attempts[0] == original_attempt
        budget = model.ledger.budget_status(model.subject, model.limits)
        assert budget.cost_microusd >= original_attempt["reserved_cost_microusd"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert model.calls()[0]["status"] == "failed"
        assert model.calls()[0]["error_code"] == "model_retry_cancelled"
        assert model.attempts() == attempts
        assert model.ledger.budget_status(model.subject, model.limits) == budget
        assert len(primary.requests) == 2 and backup.requests == []
        with model.database.connection() as connection:
            assert [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM audit_records WHERE action LIKE 'model_unknown_retry_%'"
                )
            ] == audit_before
        state = router._logical_route_state(purpose, "deep", logical_key)
        assert state is not None and state.disposition == "quarantine"
        # The old operator grant is not unlimited permission after cancellation.
        with pytest.raises(ProviderCallError) as blocked:
            await router.complete_structured(
                model.subject,
                purpose,
                MESSAGES,
                Output,
                idempotency_key=logical_key,
            )
        assert blocked.value.outcome_unknown and not blocked.value.retryable
        assert len(primary.requests) == 2 and backup.requests == []
        assert router.ledger.recover_interrupted(model.subject) == []
        store.verify_routing_integrity(model.subject)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_cancelling_one_call_does_not_settle_or_cancel_another(model: ModelFixture) -> None:
    waiting = asyncio.Event()

    async def blocked_backoff(_delay: float) -> None:
        waiting.set()
        await asyncio.Future[None]()

    async def unused_backoff(_delay: float) -> None:
        raise AssertionError("successful independent call must not back off")

    cancelled_provider = FakeProvider([_outcome("invalid_output")])
    successful_provider = FakeProvider([ProviderResponse('{"ok":true}', ModelUsage(2, 1))])
    cancelled_gateway = model.gateway(cancelled_provider, blocked_backoff)
    successful_gateway = model.gateway(successful_provider, unused_backoff)
    task = asyncio.create_task(
        cancelled_gateway.complete_structured(
            model.subject,
            "f03",
            MESSAGES,
            Output,
            idempotency_key="concurrent-cancelled",
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        successful = await successful_gateway.complete_structured(
            model.subject,
            "f03",
            MESSAGES,
            Output,
            idempotency_key="concurrent-successful",
        )
        completed = model.ledger.get_call(successful.call_id)
        attempts = model.attempts()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        by_key = {row["idempotency_key"]: row for row in model.calls()}
        assert by_key["concurrent-cancelled"]["status"] == "failed"
        assert by_key["concurrent-successful"]["status"] == "succeeded"
        assert model.ledger.get_call(successful.call_id) == completed
        assert model.attempts() == attempts
        assert len(cancelled_provider.requests) == len(successful_provider.requests) == 1
        assert model.ledger.recover_interrupted(model.subject) == []
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
