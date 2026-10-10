from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.admission import OperationInvalidated, RuntimeAdmissionGate, bind_lease
from noyra.core.types import content_hash
from noyra.model import (
    CognitiveResourceGroupInput,
    CognitiveResourceStore,
    EmbeddingGateway,
    EmbeddingLedger,
    EmbeddingResourceInput,
    EmbeddingResourceStore,
    ModelMessage,
    OpenAICompatibleProvider,
    OpenAICompatibleSettings,
    OpenAIEmbeddingProvider,
    RoutedModelGateway,
)
from noyra.model.errors import EmbeddingProviderError
from noyra.research.provider import SearchProviderStore
from noyra.research.routing import set_search_provider_enabled
from noyra.research.search import SearchExecutor
from noyra.research.types import SearchProviderInput


class Output(BaseModel):
    ok: bool


def fence(gate: RuntimeAdmissionGate, kind: str) -> None:
    if kind == "quarantine":
        gate.quarantine()
    elif kind == "drain":
        gate.begin_drain()
    elif kind == "migration":
        with gate.migration_control_scope(), pytest.raises(OperationInvalidated):
            gate.fence_for_migration(timeout=0)
    else:
        gate.invalidate()


def database(tmp_path: Path) -> tuple[Database, str, RuntimeAdmissionGate]:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "dispatch-test"
    IdentityStore(db).ensure(subject, content_hash(subject))
    gate = RuntimeAdmissionGate(subject)
    gate.open()
    return db, subject, gate


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pause", "quarantine", "drain", "migration"])
async def test_model_fence_during_connection_cancels_only_unsent_attempt(
    tmp_path: Path, kind: str
) -> None:
    db, subject, gate = database(tmp_path)
    store = CognitiveResourceStore(db, tmp_path / "secrets")
    group = store.configure(
        subject,
        CognitiveResourceGroupInput(
            pool="economy",
            label="test",
            model="test",
            base_url="https://example.test/v1",
            api_keys=(SecretStr("synthetic"),),
        ),
        actor="operator",
    )
    sends = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            fence(gate, kind)
            await request.extensions["trace"]("http11.send_request_headers.started", {})
            sends.append(True)
            raise AssertionError("fenced request sent")

    async with httpx.AsyncClient(transport=Transport()) as client:
        router = RoutedModelGateway(
            db,
            subject,
            store,
            provider_factory=lambda settings: OpenAICompatibleProvider(settings, client=client),
        )
        key = store.keys(group.group_id, subject_id=subject)[0]
        with (
            gate.operation("test") as lease,
            bind_lease(lease),
            pytest.raises(OperationInvalidated),
        ):
            await router._gateway(group, key).complete_structured(
                subject,
                "test",
                [ModelMessage(role="user", content="hi")],
                Output,
                idempotency_key="fence",
            )
    assert sends == []
    with db.connection() as connection:
        row = connection.execute("SELECT * FROM model_attempts").fetchone()
        assert row["status"] == "cancelled"
        assert connection.execute("SELECT status FROM model_calls").fetchone()[0] == "failed"


@pytest.mark.parametrize("kind", ["revoke", "pause", "quarantine", "drain", "migration"])
@pytest.mark.parametrize("executor", [False, True])
def test_embedding_final_authorization_inherits_lease_through_both_workers(
    tmp_path: Path, kind: str, executor: bool
) -> None:
    db, subject, gate = database(tmp_path)
    store = EmbeddingResourceStore(db, tmp_path / "secrets")
    record = store.configure(
        subject,
        EmbeddingResourceInput(
            label="test",
            model="test",
            base_url="https://example.test/v1",
            api_key=SecretStr("synthetic"),
        ),
        actor="operator",
    )
    settings = store.active_settings(subject)
    assert settings is not None
    sends = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if kind == "revoke":
                store.revoke(record.config_id, subject_id=subject, actor="operator", reason="test")
            else:
                fence(gate, kind)
            request.extensions["trace"]("http11.send_request_headers.started", {})
            sends.append(True)
            raise AssertionError("fenced embedding sent")

    provider = OpenAIEmbeddingProvider(
        settings,
        transport=Transport(),
        call_authorizer=store.call_authorizer(subject, settings),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        gateway = EmbeddingGateway(
            provider,
            EmbeddingLedger(db),
            subject_id=subject,
            resource_id=record.config_id,
            model=settings.model,
            limits=settings.budget_limits(),
            pricing=settings.pricing(),
            circuit_policy=settings.circuit_policy(),
            timeout_seconds=10,
            executor=pool if executor else None,
        )
        expected = EmbeddingProviderError if kind == "revoke" else OperationInvalidated
        with gate.operation("test") as lease, bind_lease(lease), pytest.raises(expected):
            gateway.embed(("synthetic",))
    assert sends == []
    with db.connection() as connection:
        row = connection.execute("SELECT * FROM embedding_usage_entries").fetchone()
        assert row["status"] == "failed" and row["cost_microusd"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["revoke", "disabled", "pause", "quarantine", "drain", "migration"]
)
@pytest.mark.parametrize("provider_type", ["brave", "bing", "tavily", "serper"])
async def test_search_final_authorization_stops_before_headers(
    tmp_path: Path, kind: str, provider_type: Literal["brave", "bing", "tavily", "serper"]
) -> None:
    db, subject, gate = database(tmp_path)
    store = SearchProviderStore(db, tmp_path / "secrets")
    record = store.configure(
        subject,
        SearchProviderInput(provider_type=provider_type, label="test", api_key="synthetic"),
        actor="operator",
    )
    sends = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if kind == "revoke":
                store.revoke(record.config_id, subject_id=subject, actor="operator", reason="test")
            elif kind == "disabled":
                set_search_provider_enabled(db, record.config_id, subject, False)
            else:
                fence(gate, kind)
            await request.extensions["trace"]("http11.send_request_headers.started", {})
            sends.append(True)
            raise AssertionError("fenced search sent")

    async with httpx.AsyncClient(transport=Transport()) as client:
        search = SearchExecutor(db, store, client=client)
        with gate.operation("test") as lease, bind_lease(lease):
            kwargs: dict[str, Any] = dict(
                goal_id=None,
                strategy_id=None,
                expected_outcome="test",
                idempotency_key="fence",
            )
            if kind in {"revoke", "disabled"}:
                result = await search.search(subject, record, "synthetic", **kwargs)
                assert result.status == "failed"
            else:
                with pytest.raises(OperationInvalidated):
                    await search.search(subject, record, "synthetic", **kwargs)
    assert sends == []
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM actions").fetchone()[0] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "unknown"])
async def test_search_sent_outcome_settles_before_stale_result_is_fenced(
    tmp_path: Path, outcome: str
) -> None:
    db, subject, gate = database(tmp_path)
    store = SearchProviderStore(db, tmp_path / "secrets")
    record = store.configure(
        subject,
        SearchProviderInput(provider_type="brave", label="test", api_key="synthetic"),
        actor="operator",
    )

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await request.extensions["trace"]("http11.send_request_headers.started", {})
            gate.invalidate()
            if outcome == "unknown":
                raise httpx.ReadTimeout("synthetic sent request")
            return httpx.Response(200, json={"web": {"results": []}}, request=request)

    async with httpx.AsyncClient(transport=Transport()) as client:
        search = SearchExecutor(db, store, client=client)
        with (
            gate.operation("test") as lease,
            bind_lease(lease),
            pytest.raises(OperationInvalidated),
        ):
            kwargs: dict[str, Any] = dict(goal_id=None, strategy_id=None)
            await search.search(
                subject,
                record,
                "synthetic",
                **kwargs,
                expected_outcome="test",
                idempotency_key="sent",
            )
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM actions").fetchone()[0] == (
            "succeeded" if outcome == "success" else "unknown"
        )


def test_embedding_sent_success_keeps_usage_but_fences_recall_result(tmp_path: Path) -> None:
    db, subject, gate = database(tmp_path)
    store = EmbeddingResourceStore(db, tmp_path / "secrets")
    record = store.configure(
        subject,
        EmbeddingResourceInput(
            label="test",
            model="test",
            base_url="https://example.test/v1",
            api_key=SecretStr("synthetic"),
        ),
        actor="operator",
    )
    settings = store.active_settings(subject)
    assert settings is not None

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            request.extensions["trace"]("http11.send_request_headers.started", {})
            gate.invalidate()
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1.0]}], "usage": {"prompt_tokens": 1}},
                request=request,
            )

    gateway = EmbeddingGateway(
        OpenAIEmbeddingProvider(
            settings,
            transport=Transport(),
            call_authorizer=store.call_authorizer(subject, settings),
        ),
        EmbeddingLedger(db),
        subject_id=subject,
        resource_id=record.config_id,
        model=settings.model,
        limits=settings.budget_limits(),
        pricing=settings.pricing(),
        circuit_policy=settings.circuit_policy(),
        timeout_seconds=10,
    )
    with gate.operation("test") as lease, bind_lease(lease), pytest.raises(OperationInvalidated):
        gateway.embed(("synthetic",))
    with db.connection() as connection:
        row = connection.execute("SELECT * FROM embedding_usage_entries").fetchone()
        assert row["status"] == "succeeded" and row["input_tokens"] == 1


@pytest.mark.asyncio
async def test_model_trace_retains_its_own_call_authorizer_during_overlap() -> None:
    from noyra.model.types import CompletionRequest

    entered = asyncio.Event()
    release = asyncio.Event()
    checked: list[str] = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            entered.set()
            await release.wait()
            await request.extensions["trace"]("http11.send_request_headers.started", {})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"ok":true}'}}]},
                request=request,
            )

    async with httpx.AsyncClient(transport=Transport()) as client:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                base_url="https://example.test/v1",
                model="test",
                api_key=SecretStr("synthetic"),
            ),
            client=client,
        )
        request = CompletionRequest(
            model="test",
            messages=(ModelMessage(role="user", content="hi"),),
            max_output_tokens=10,
            temperature=0.2,
            schema_name="Output",
            output_schema={},
        )
        provider.before_send = lambda: checked.append("first")
        task = asyncio.create_task(provider.complete(request))
        await entered.wait()
        provider.before_send = lambda: checked.append("second")
        release.set()
        await task
        assert checked == ["first", "first"]
