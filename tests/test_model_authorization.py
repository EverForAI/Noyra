from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.types import content_hash
from noyra.model import (
    CognitiveResourceGroupInput,
    CognitiveResourceGroupUpdate,
    CognitiveResourceStore,
    ModelMessage,
    ModelUsage,
    OpenAICompatibleProvider,
    ProviderResponse,
    RoutedModelGateway,
)
from noyra.model.errors import ModelAuthorizationError, ProviderCallError


class Output(BaseModel):
    ok: bool


def setup(tmp_path: Path) -> tuple[Database, CognitiveResourceStore, str, Any, Any]:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "authorization-test"
    IdentityStore(db).ensure(subject, content_hash(subject))
    store = CognitiveResourceStore(db, tmp_path / "secrets")
    group = store.configure(
        subject,
        CognitiveResourceGroupInput(
            pool="economy",
            label="test",
            model="test",
            base_url="https://models.example/v1",
            api_keys=(SecretStr("synthetic"),),
            max_attempts=2,
            input_usd_per_million=Decimal("1"),
            output_usd_per_million=Decimal("1"),
        ),
        actor="operator",
    )
    return db, store, subject, group, store.keys(group.group_id, subject_id=subject)[0]


def mutation(
    store: CognitiveResourceStore, subject: str, group: Any, key: Any, action: str
) -> None:
    if action == "key":
        store.revoke_key(key.key_id, subject_id=subject, actor="operator", reason="test")
    elif action == "group":
        store.disable(group.group_id, subject_id=subject, actor="operator", reason="test")
    else:
        store.update(
            group.group_id,
            CognitiveResourceGroupUpdate(daily_attempts=3),
            subject_id=subject,
            actor="operator",
            reason="test",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["key", "group", "revision"])
async def test_retry_authorization_denial_is_terminal_and_keeps_spent_cost(
    tmp_path: Path, action: str
) -> None:
    db, store, subject, group, key = setup(tmp_path)

    class Provider:
        name = "synthetic"
        calls = 0

        async def complete(self, request: Any) -> ProviderResponse:
            self.calls += 1
            mutation(store, subject, group, key, action)
            return ProviderResponse("invalid json", ModelUsage(3, 2))

    provider = Provider()
    router = RoutedModelGateway(db, subject, store, provider_factory=lambda _: provider)
    gateway = router._gateway(group, key)
    with pytest.raises(ModelAuthorizationError):
        await gateway.complete_structured(
            subject,
            "test",
            [ModelMessage(role="user", content="hi")],
            Output,
            idempotency_key="retry",
        )
    assert provider.calls == 1
    with db.connection() as connection:
        call = connection.execute("SELECT * FROM model_calls").fetchone()
        attempt = connection.execute("SELECT * FROM model_attempts").fetchone()
    assert call["status"] == "failed" and call["error_code"] == "model_authorization_denied"
    assert attempt["status"] == "succeeded" and attempt["cost_microusd"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["key", "group", "revision"])
@pytest.mark.parametrize("stage", ["authorize", "start", "provider"])
async def test_denial_before_send_cancels_reservation_without_network(
    tmp_path: Path, action: str, stage: str
) -> None:
    db, store, subject, group, key = setup(tmp_path)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = RoutedModelGateway(
            db,
            subject,
            store,
            provider_factory=lambda settings: OpenAICompatibleProvider(settings, client=client),
        )
        gateway = router._gateway(group, key)
        name = "authorize_attempt" if stage == "authorize" else "start_attempt"
        original = getattr(gateway.ledger, name)

        def changed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            mutation(store, subject, group, key, action)
            return result

        if stage == "provider":
            provider = gateway.provider
            assert isinstance(provider, OpenAICompatibleProvider)
            original_complete = provider.complete

            async def changed_complete(request: Any) -> ProviderResponse:
                mutation(store, subject, group, key, action)
                return await original_complete(request)

            target: Any = provider
            replacement: Callable[..., Any] = changed_complete
            name = "complete"
        else:
            target = gateway.ledger
            replacement = changed
        with patch.object(target, name, replacement), pytest.raises(ModelAuthorizationError):
            await gateway.complete_structured(
                subject,
                "test",
                [ModelMessage(role="user", content="hi")],
                Output,
                idempotency_key="before-send",
            )
    assert calls == 0
    with db.connection() as connection:
        call = connection.execute("SELECT status FROM model_calls").fetchone()[0]
        attempt = connection.execute("SELECT status FROM model_attempts").fetchone()[0]
    assert call == "failed" and attempt == "cancelled"


@pytest.mark.asyncio
async def test_routed_group_denial_is_not_a_fake_concurrent_call(tmp_path: Path) -> None:
    db, store, subject, group, _key = setup(tmp_path)

    class Provider:
        name = "synthetic"

        async def complete(self, request: Any) -> ProviderResponse:
            store.disable(group.group_id, subject_id=subject, actor="operator", reason="stop")
            raise ProviderCallError("known", retryable=True, outcome_unknown=False)

    router = RoutedModelGateway(db, subject, store, provider_factory=lambda _: Provider())
    with pytest.raises(ModelAuthorizationError):
        await router.complete_structured(
            subject,
            "world_cognition:test",
            [ModelMessage(role="user", content="hi")],
            Output,
            idempotency_key="routed",
        )
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM model_calls").fetchone()[0] == "failed"


@pytest.mark.asyncio
async def test_connection_wait_cannot_send_after_revocation(tmp_path: Path) -> None:
    db, store, subject, group, key = setup(tmp_path)

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            store.revoke_key(key.key_id, subject_id=subject, actor="operator", reason="waiting")
            await request.extensions["trace"]("http11.send_request_headers.started", {})
            raise AssertionError("revoked request reached network send")

    async with httpx.AsyncClient(transport=Transport()) as client:
        router = RoutedModelGateway(
            db,
            subject,
            store,
            provider_factory=lambda settings: OpenAICompatibleProvider(settings, client=client),
        )
        with pytest.raises(ModelAuthorizationError):
            await router._gateway(group, key).complete_structured(
                subject,
                "test",
                [ModelMessage(role="user", content="hi")],
                Output,
                idempotency_key="connection-wait",
            )
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM model_attempts").fetchone()[0] == "cancelled"


@pytest.mark.asyncio
async def test_backoff_revocation_blocks_even_when_secret_cleanup_fails(tmp_path: Path) -> None:
    db, store, subject, group, key = setup(tmp_path)
    provider_calls = 0

    class Provider:
        name = "synthetic"

        async def complete(self, request: Any) -> ProviderResponse:
            nonlocal provider_calls
            provider_calls += 1
            raise ProviderCallError("known", retryable=True, outcome_unknown=False)

    gateway = RoutedModelGateway(
        db, subject, store, provider_factory=lambda _: Provider()
    )._gateway(group, key)

    async def revoke_during_wait(delay: float) -> None:
        with patch.object(Path, "unlink", side_effect=OSError("synthetic busy file")):
            store.revoke_key(key.key_id, subject_id=subject, actor="operator", reason="backoff")

    gateway._sleep = revoke_during_wait
    with pytest.raises(ModelAuthorizationError):
        await gateway.complete_structured(
            subject,
            "test",
            [ModelMessage(role="user", content="hi")],
            Output,
            idempotency_key="cleanup-failure",
        )
    assert provider_calls == 1
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM model_calls").fetchone()[0] == "failed"
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 1
