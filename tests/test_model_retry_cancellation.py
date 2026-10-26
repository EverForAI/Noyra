from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from noyra.core import Database, IdentityStore
from noyra.core.types import content_hash
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelMessage,
    ModelPricing,
    ModelUsage,
    ProviderResponse,
    RetryPolicy,
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
