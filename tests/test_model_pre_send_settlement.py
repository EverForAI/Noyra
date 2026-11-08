from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.model_admission import track_model_admission
from noyra.core.types import content_hash
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelLedger,
    ModelPricing,
    ModelUsage,
    OpenAICompatibleProvider,
    OpenAICompatibleSettings,
    ProviderResponse,
)
from noyra.model.errors import ModelAuthorizationError, ProviderCallError
from test_model_invalidation_before_attempt import complete, no_backoff
from test_model_retry_cancellation import ModelFixture, _outcome


@pytest.fixture
def model(tmp_path: Path) -> ModelFixture:
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "model-pre-send"
    IdentityStore(database).ensure(subject, content_hash(subject))
    return ModelFixture(
        database,
        ModelLedger(database),
        subject,
        BudgetLimits(20, 100_000, 100_000, 10_000_000),
        ModelPricing(input_microusd_per_million=1_000_000, output_microusd_per_million=2_000_000),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["admission", "start", "authorization"])
async def test_unexpected_unsent_failure_closes_call_and_attempt_atomically(
    model: ModelFixture, phase: str
) -> None:
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    failure = sqlite3.OperationalError("synthetic failure before sending")
    target = {
        "admission": "noyra.model.gateway.notify_model_admission",
        "start": "noyra.model.ledger.ModelLedger.start_attempt",
        "authorization": "noyra.model.ledger.ModelLedger.check_request_authorization",
    }[phase]
    with patch(target, side_effect=failure), pytest.raises(sqlite3.OperationalError) as raised:
        await complete(model, gateway)
    assert raised.value is failure
    assert model.calls()[0]["status"] == "failed"
    assert model.attempts()[0]["status"] == "cancelled"
    assert not provider.requests
    budget = model.ledger.budget_status(model.subject, model.limits)
    assert budget.input_tokens == budget.output_tokens == budget.cost_microusd == 0


@pytest.mark.asyncio
async def test_authorization_transaction_error_finishes_only_rolled_back_call(
    model: ModelFixture,
) -> None:
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    failure = sqlite3.OperationalError("synthetic attempt transaction rollback")
    with (
        patch.object(model.ledger, "_load_attempt_connection", side_effect=failure),
        pytest.raises(sqlite3.OperationalError) as raised,
    ):
        await complete(model, gateway)
    assert raised.value is failure
    assert model.calls()[0]["status"] == "failed"
    assert not model.attempts() and not provider.requests


@pytest.mark.asyncio
async def test_real_admission_observer_exception_keeps_original_error(model: ModelFixture) -> None:
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    failure = RuntimeError("synthetic observer failure")

    def observer() -> None:
        raise failure

    with track_model_admission(observer), pytest.raises(RuntimeError) as raised:
        await complete(model, gateway)
    assert raised.value is failure
    assert model.calls()[0]["status"] == "failed"
    assert model.attempts()[0]["status"] == "cancelled" and not provider.requests


@pytest.mark.asyncio
async def test_http_final_header_guard_error_is_unsent_and_not_provider_unknown(
    model: ModelFixture,
) -> None:
    failure = sqlite3.OperationalError("synthetic final header authorization failure")
    sent: list[bool] = []
    pending = [False]

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            pending[0] = True
            await request.extensions["trace"]("http11.send_request_headers.started", {})
            sent.append(True)
            raise AssertionError("unsent request reached transport")

    check = model.ledger.check_request_authorization

    def authorize(*args: Any, **kwargs: Any) -> None:
        if pending[0]:
            raise failure
        check(*args, **kwargs)

    async with httpx.AsyncClient(transport=Transport()) as client:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                base_url="https://fixture.example/v1",
                model="f03-model",
                api_key=SecretStr("synthetic"),
            ),
            client=client,
        )
        gateway = model.gateway(FakeProvider([]), no_backoff)
        gateway.provider = provider
        with (
            patch.object(model.ledger, "check_request_authorization", side_effect=authorize),
            pytest.raises(sqlite3.OperationalError) as raised,
        ):
            await complete(model, gateway)
    assert raised.value is failure and not sent
    assert model.calls()[0]["status"] == "failed"
    assert model.attempts()[0]["status"] == "cancelled"
    assert model.ledger.budget_status(model.subject, model.limits).cost_microusd == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [IntegrityError("synthetic integrity failure"), asyncio.CancelledError()]
)
async def test_pre_send_integrity_and_cancellation_stop_without_hiding_original(
    model: ModelFixture, failure: BaseException
) -> None:
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    with (
        patch("noyra.model.gateway.notify_model_admission", side_effect=failure),
        pytest.raises(type(failure)) as raised,
    ):
        await complete(model, gateway)
    assert raised.value is failure
    assert model.calls()[0]["status"] == "failed"
    assert model.attempts()[0]["status"] == "cancelled" and not provider.requests


@pytest.mark.asyncio
async def test_start_transaction_failure_rolls_back_then_closes_unsent(model: ModelFixture) -> None:
    failure = sqlite3.OperationalError("synthetic start transaction rollback")
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    read = model.ledger._load_attempt_connection
    saw_start = []

    def abort(connection: Any, attempt_id: str) -> Any:
        state = connection.execute(
            "SELECT status FROM model_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()[0]
        if state == "executing":
            saw_start.append(True)
            raise failure
        return read(connection, attempt_id)

    with (
        patch.object(model.ledger, "_load_attempt_connection", side_effect=abort),
        pytest.raises(sqlite3.OperationalError) as raised,
    ):
        await complete(model, gateway)
    assert raised.value is failure and saw_start
    assert model.calls()[0]["status"] == "failed"
    assert model.attempts()[0]["status"] == "cancelled"
    assert model.attempts()[0]["started_at"] is None
    assert not provider.requests


@pytest.mark.asyncio
async def test_unsent_call_and_attempt_updates_roll_back_as_one_transaction(
    model: ModelFixture,
) -> None:
    with model.database.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_fail_call BEFORE UPDATE OF status ON model_calls "
            "WHEN NEW.status='failed' BEGIN SELECT RAISE(ABORT,'synthetic settlement failure'); END"
        )
    failure = RuntimeError("synthetic observer error")
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    with (
        patch("noyra.model.gateway.notify_model_admission", side_effect=failure),
        pytest.raises(sqlite3.IntegrityError, match="synthetic settlement failure") as raised,
    ):
        await complete(model, gateway)
    assert raised.value.__context__ is failure
    assert model.calls()[0]["status"] == "executing"
    assert model.attempts()[0]["status"] == "authorized"
    assert model.attempts()[0]["completed_at"] is None
    assert model.ledger.budget_status(model.subject, model.limits).cost_microusd > 0
    assert not provider.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["known_failure", "invalid_output", "unknown_usage", "estimated_usage"]
)
async def test_second_attempt_failure_preserves_all_previous_usage(
    model: ModelFixture, kind: str
) -> None:
    provider = FakeProvider([_outcome(kind)])
    gateway = model.gateway(provider, no_backoff)
    failure = RuntimeError("synthetic second admission failure")
    calls = 0
    saved: list[dict[str, Any]] = []

    def observer() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            saved.extend(model.attempts()[:1])
            raise failure

    with track_model_admission(observer), pytest.raises(RuntimeError) as raised:
        await complete(model, gateway)
    assert raised.value is failure
    assert model.calls()[0]["status"] == "failed"
    attempts = model.attempts()
    assert attempts[:1] == saved and attempts[1]["status"] == "cancelled"
    assert len(attempts) == 2 and len(provider.requests) == 1
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
@pytest.mark.parametrize("phase", ["second_gateway_guard", "provider_entry", "headers"])
@pytest.mark.parametrize("kind", ["database", "http_wrapped", "cancel", "authorization"])
async def test_final_guards_preserve_original_even_when_http_adapter_wraps_it(
    model: ModelFixture, phase: str, kind: str
) -> None:
    failure: BaseException
    if kind == "http_wrapped":
        failure = httpx.ConnectError("synthetic authorization dependency failed")
    elif kind == "cancel":
        failure = asyncio.CancelledError()
    elif kind == "authorization":
        failure = ModelAuthorizationError("synthetic key revocation")
    else:
        failure = sqlite3.OperationalError("synthetic authorization database failure")
    entered = []
    sends = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await request.extensions["trace"]("http11.send_request_headers.started", {})
            sends.append(True)
            raise AssertionError("guard error did not block headers")

    check = model.ledger.check_request_authorization
    guard_checks = 0
    fail_at = {"second_gateway_guard": 2, "provider_entry": 3, "headers": 4}[phase]

    def authorization(*args: Any, **kwargs: Any) -> None:
        nonlocal guard_checks
        guard_checks += 1
        if guard_checks == fail_at:
            raise failure
        check(*args, **kwargs)

    async with httpx.AsyncClient(transport=Transport()) as client:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                base_url="https://fixture.example/v1",
                model="f03-model",
                api_key=SecretStr("synthetic"),
            ),
            client=client,
        )
        original = provider.complete

        async def entry(request: Any) -> ProviderResponse:
            entered.append(True)
            return await original(request)

        gateway = model.gateway(FakeProvider([]), no_backoff)
        gateway.provider = provider
        with (
            patch.object(provider, "complete", side_effect=entry),
            patch.object(model.ledger, "check_request_authorization", side_effect=authorization),
            pytest.raises(type(failure)) as raised,
        ):
            await complete(model, gateway)
    assert raised.value is failure and not sends
    assert bool(entered) == (phase != "second_gateway_guard")
    assert model.calls()[0]["status"] == "failed"
    assert model.attempts()[0]["status"] == "cancelled"
    assert model.ledger.budget_status(model.subject, model.limits).cost_microusd == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["database", "integrity", "authorization", "timeout", "provider_unknown", "cancel"]
)
async def test_errors_after_possible_dispatch_keep_unknown_reservation(
    model: ModelFixture, kind: str
) -> None:
    if kind == "database":
        failure: BaseException = sqlite3.OperationalError("synthetic provider failure after send")
    elif kind == "integrity":
        failure = IntegrityError("synthetic provider integrity failure after send")
    elif kind == "authorization":
        failure = ModelAuthorizationError("not a guard refusal")
    elif kind == "timeout":
        failure = TimeoutError("synthetic provider timeout")
    elif kind == "provider_unknown":
        failure = ProviderCallError("synthetic_unknown", retryable=False, outcome_unknown=True)
    else:
        failure = asyncio.CancelledError()
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    budgets = []

    async def sent(request: Any) -> ProviderResponse:
        provider.requests.append(request)
        budgets.append(model.ledger.budget_status(model.subject, model.limits))
        raise failure

    with (
        patch.object(provider, "complete", side_effect=sent),
        pytest.raises(asyncio.CancelledError if kind == "cancel" else ProviderCallError),
    ):
        await complete(model, gateway)
    assert model.calls()[0]["status"] == model.attempts()[0]["status"] == "unknown"
    assert model.ledger.budget_status(model.subject, model.limits) == budgets[0]
    assert len(provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", "succeeded", "failed", "new_attempt"])
async def test_unsent_cleanup_never_overwrites_an_advanced_call(
    model: ModelFixture, state: str
) -> None:
    failure = RuntimeError("synthetic observer error after competing state transition")
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    saved_calls = []
    saved_attempts = []

    def observer() -> None:
        call_id = model.calls()[0]["call_id"]
        attempt_id = model.attempts()[0]["attempt_id"]
        model.ledger.start_attempt(attempt_id)
        unknown = state == "unknown"
        model.ledger.finish_attempt(
            attempt_id,
            "unknown"
            if unknown
            else "failed"
            if state in {"failed", "new_attempt"}
            else "succeeded",
            usage=None if unknown else ModelUsage(1, 1),
            cost_microusd=None if unknown else 3,
        )
        if state == "new_attempt":
            model.ledger.authorize_attempt(
                call_id,
                model.limits,
                continuation=True,
                reserved_input_tokens=1,
                reserved_output_tokens=1,
                reserved_cost_microusd=1,
            )
        else:
            model.ledger.finish_call(
                call_id,
                state,
                response={"content": '{"ok":true}'} if state == "succeeded" else None,
            )
        saved_calls.extend(model.calls())
        saved_attempts.extend(model.attempts())
        raise failure

    with track_model_admission(observer), pytest.raises(RuntimeError) as raised:
        await complete(model, gateway)
    assert raised.value is failure
    assert model.calls() == saved_calls and model.attempts() == saved_attempts
    assert not provider.requests


@pytest.mark.asyncio
async def test_unknown_commit_outcome_in_authorization_is_not_guessed_unsent(
    model: ModelFixture,
) -> None:
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    authorize = model.ledger.authorize_attempt
    failure = sqlite3.OperationalError("synthetic failure after authorization committed")
    saved_calls = []
    saved_attempts = []

    def incomplete_return(*args: Any, **kwargs: Any) -> Any:
        authorize(*args, **kwargs)
        saved_calls.extend(model.calls())
        saved_attempts.extend(model.attempts())
        raise failure

    with (
        patch.object(model.ledger, "authorize_attempt", side_effect=incomplete_return),
        pytest.raises(sqlite3.OperationalError) as raised,
    ):
        await complete(model, gateway)
    assert raised.value is failure
    assert model.calls() == saved_calls and model.attempts() == saved_attempts
    assert model.attempts()[0]["status"] == "authorized"
    assert model.ledger.budget_status(model.subject, model.limits).cost_microusd > 0
    assert not provider.requests


@pytest.mark.asyncio
async def test_routed_local_failure_does_not_rotate_keys_or_resources(
    model: ModelFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_model_retry_cancellation import MESSAGES, Output, _router

    provider = FakeProvider([])
    backup = FakeProvider([])
    router, _, constructed = _router(model, tmp_path, provider, backup, no_backoff, monkeypatch)
    failure = sqlite3.OperationalError("synthetic local accounting observer error")

    def observer() -> None:
        raise failure

    with track_model_admission(observer), pytest.raises(sqlite3.OperationalError) as raised:
        await router.complete_structured(
            model.subject,
            "f03:routed",
            MESSAGES,
            Output,
            idempotency_key="pre-send-local-failure",
            max_output_tokens=20,
        )
    assert raised.value is failure
    assert constructed == ["primary"]
    assert not provider.requests and not backup.requests
    assert len(model.calls()) == 1 and model.calls()[0]["status"] == "failed"
    assert model.attempts()[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_unsent_authorized_unknown_retry_keeps_old_fees_and_original_fence(
    model: ModelFixture,
) -> None:
    from decimal import Decimal

    from noyra.model import RoutedModelGateway
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
        call.call_id, actor="operator", reason="Explicit retry only"
    )
    before = model.attempts()
    budget = model.ledger.budget_status(model.subject, model.limits)
    failure = RuntimeError("synthetic admission error on authorized retry")

    def observer() -> None:
        raise failure

    with track_model_admission(observer), pytest.raises(RuntimeError) as raised:
        await complete(model, router)
    assert raised.value is failure
    assert model.calls()[0]["status"] == "failed"
    assert model.attempts()[:1] == before
    assert model.attempts()[1]["status"] == "cancelled"
    assert model.ledger.budget_status(model.subject, model.limits) == budget
    with pytest.raises(ProviderCallError, match="quarantined"):
        await complete(model, router)
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_atomic_unsent_failure_survives_database_reopen_without_recovery_rewrite(
    model: ModelFixture,
) -> None:
    from noyra.model.errors import ModelCallStateError

    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    failure = RuntimeError("synthetic observer failure")
    with (
        patch("noyra.model.gateway.notify_model_admission", side_effect=failure),
        pytest.raises(RuntimeError),
    ):
        await complete(model, gateway)
    calls, attempts = model.calls(), model.attempts()
    reopened = ModelLedger(Database(model.database.path))
    assert reopened.recover_interrupted(model.subject) == []
    assert reopened.get_call(calls[0]["call_id"]).status == "failed"
    assert reopened.attempts(calls[0]["call_id"])[0].status == "cancelled"
    gateway.ledger = reopened
    with pytest.raises(ModelCallStateError):
        await complete(model, gateway)
    assert calls == model.calls() and attempts == model.attempts() and not provider.requests


@pytest.mark.asyncio
async def test_corrupt_attempt_does_not_authorize_cancellation_or_release(
    model: ModelFixture,
) -> None:
    provider = FakeProvider([])
    gateway = model.gateway(provider, no_backoff)
    failure = RuntimeError("original observer failure")
    saved_calls = []
    saved_attempts = []

    def observer() -> None:
        with model.database.transaction() as connection:
            connection.execute("UPDATE model_attempts SET reserved_cost_microusd='invalid'")
        saved_calls.extend(model.calls())
        saved_attempts.extend(model.attempts())
        raise failure

    with track_model_admission(observer), pytest.raises(IntegrityError) as raised:
        await complete(model, gateway)
    chain: list[BaseException] = []
    current: BaseException | None = raised.value
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__context__
    assert failure in chain
    assert model.calls() == saved_calls and model.attempts() == saved_attempts
    assert not provider.requests
