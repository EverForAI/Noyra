from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from pydantic import SecretStr

from noyra.cognition import CognitionSettings, WorldSourceConfig
from noyra.cognition.cycle import CognitionCycle
from noyra.core import SubjectKernel
from noyra.core.admission import OperationInvalidated, bind_lease
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.locking import ProcessLock
from noyra.core.types import content_hash
from noyra.model import (
    BudgetLimits,
    EmbeddingBudgetLimits,
    EmbeddingCircuitPolicy,
    EmbeddingGateway,
    EmbeddingLedger,
    EmbeddingPricing,
    EmbeddingSettings,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    OpenAIEmbeddingProvider,
)
from noyra.model import embedding_gateway as gateway_module
from noyra.model.errors import EmbeddingProviderError, ModelGatewayError
from noyra.service import NoyraService, ServiceSettings


class _Provider:
    name = "shutdown-test"

    def __init__(self, release: threading.Event | None = None) -> None:
        self.release = release
        self.entered = threading.Event()
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        self.entered.set()
        if self.release is not None and not self.release.wait(5):
            raise RuntimeError("test watchdog expired")
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    owner = SubjectKernel(tmp_path / "noyra.sqlite3", "Noyra-r05", content_hash("r05"))
    owner.boot()
    owner.orient()
    owner.activate()
    try:
        yield owner
    finally:
        owner.close()


def _gateway(
    owner: SubjectKernel,
    provider: Any,
    *,
    accounting: Any = None,
    executor: ThreadPoolExecutor | None = None,
) -> EmbeddingGateway:
    return EmbeddingGateway(
        provider,
        EmbeddingLedger(owner.database),
        subject_id=owner.subject_id,
        resource_id="r05-embedding",
        model="fixture",
        limits=EmbeddingBudgetLimits(100, 100000, 100000),
        pricing=EmbeddingPricing(1),
        circuit_policy=EmbeddingCircuitPolicy(10, 60),
        timeout_seconds=0.02,
        accounting=accounting,
        executor=executor,
    )


def _cycle(owner: SubjectKernel, gateway: EmbeddingGateway | None) -> CognitionCycle:
    model = ModelGateway(
        FakeProvider([]),
        ModelLedger(owner.database),
        model="fixture",
        limits=BudgetLimits(100, 100000, 100000, 100000),
    )
    with patch("noyra.cognition.cycle.EmbeddingSettings.from_env", return_value=None):
        cycle = CognitionCycle(
            owner,
            model,
            CognitionSettings(
                enabled=True,
                sources=(
                    WorldSourceConfig(
                        name="fixture", url="https://example.test/", source_type="web"
                    ),
                ),
            ),
        )
    cycle.embedding_gateway = gateway
    return cycle


def _usage(owner: SubjectKernel) -> tuple[str, str | None]:
    with owner.database.connection() as connection:
        row = connection.execute(
            "SELECT status,error_code FROM embedding_usage_entries ORDER BY created_at LIMIT 1"
        ).fetchone()
    assert row is not None
    return str(row["status"]), row["error_code"]


def _start_timeout(owner: SubjectKernel, gateway: EmbeddingGateway) -> None:
    with (
        owner.admission.operation("embedding-test") as lease,
        bind_lease(lease),
        pytest.raises(EmbeddingProviderError, match="embedding_hard_timeout"),
    ):
        gateway.embed(("isolated test input",))
    assert _usage(owner) == ("unknown", "embedding_hard_timeout")


def test_close_rejects_new_calls_before_budget_or_provider_work(kernel: SubjectKernel) -> None:
    provider = _Provider()
    gateway = _gateway(kernel, provider)
    assert gateway.close() is True
    try:
        with pytest.raises(EmbeddingProviderError, match="embedding_gateway_closed"):
            gateway.embed(("must not be authorized",))
        with kernel.database.connection() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM embedding_usage_entries").fetchone()[0]
                == 0
            )
        assert provider.calls == 0
    finally:
        gateway.close(wait=True, timeout=1)


@pytest.mark.asyncio
async def test_cycle_close_allows_outer_timeout_and_retry(kernel: SubjectKernel) -> None:
    release = threading.Event()
    gateway = _gateway(kernel, _Provider(release))
    cycle = _cycle(kernel, gateway)
    _start_timeout(kernel, gateway)
    timer = threading.Timer(0.4, release.set)
    timer.daemon = True
    timer.start()
    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_later(0.01, heartbeat.set)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(cycle.aclose(), timeout=0.05)
        assert heartbeat.is_set()
        assert not cycle._closed
        assert not release.is_set()
        assert kernel.process_lock.held
    finally:
        release.set()
        timer.cancel()
        assert gateway.close(wait=True, timeout=1)
        await cycle.aclose()
    assert cycle._closed
    assert _usage(kernel) == ("unknown", "embedding_hard_timeout")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["authorization", "response"])
async def test_real_provider_late_callbacks_cannot_write_or_replace_unknown(
    kernel: SubjectKernel, stage: str
) -> None:
    release = threading.Event()
    entered = threading.Event()
    callback_finished = threading.Event()
    requests: list[str] = []
    late_errors: list[BaseException] = []

    def authorize() -> None:
        if stage == "authorization":
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test watchdog expired")

    def record_dimensions(dimensions: int) -> None:
        assert dimensions == 2
        if stage == "response":
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test watchdog expired")
            try:
                with kernel.database.transaction() as connection:
                    connection.execute(
                        "UPDATE subject_identity SET personal_name='late mutation' "
                        "WHERE subject_id=?",
                        (kernel.subject_id,),
                    )
            except OperationInvalidated as error:
                late_errors.append(error)
                raise
            finally:
                callback_finished.set()

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1.0, 0.0]}], "usage": {"prompt_tokens": 1}},
        )

    provider = OpenAIEmbeddingProvider(
        EmbeddingSettings(
            base_url="https://example.test/v1/",
            model="fixture",
            api_key=SecretStr("synthetic-only"),
            timeout_seconds=1.0,
        ),
        call_authorizer=authorize,
        observed_dimension_recorder=record_dimensions,
        transport=httpx.MockTransport(response),
    )
    gateway = _gateway(kernel, provider)
    cycle = _cycle(kernel, gateway)
    try:
        _start_timeout(kernel, gateway)
        assert entered.wait(2)
        kernel.admission.begin_drain()
        with pytest.raises(ModelGatewayError, match="embedding_shutdown_incomplete"):
            await cycle.aclose(embedding_timeout=0.02)
        assert not cycle._closed
        assert kernel.process_lock.held
        release.set()
        assert await gateway.aclose(timeout=2)
        await cycle.aclose()
        assert _usage(kernel) == ("unknown", "embedding_hard_timeout")
        assert requests == (
            [] if stage == "authorization" else ["https://example.test/v1/embeddings"]
        )
        if stage == "response":
            assert callback_finished.is_set()
            assert len(late_errors) == 1
        with kernel.database.connection() as connection:
            assert (
                connection.execute("SELECT personal_name FROM subject_identity").fetchone()[0]
                is None
            )
    finally:
        release.set()
        assert gateway.close(wait=True, timeout=2)
        await cycle.aclose()


@pytest.mark.asyncio
async def test_full_call_is_tracked_after_provider_worker_has_finished(
    kernel: SubjectKernel,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    outcomes: list[Any] = []

    def accounting(event: Any) -> None:
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test watchdog expired")

    gateway = _gateway(kernel, _Provider(), accounting=accounting)

    def invoke() -> None:
        try:
            outcomes.append(gateway.embed(("settlement in progress",)))
        except BaseException as error:
            outcomes.append(error)

    caller = threading.Thread(target=invoke, daemon=True)
    caller.start()
    try:
        assert entered.wait(2)
        assert not gateway._workers and not gateway._futures
        assert gateway.close() is False
        assert await gateway.aclose(timeout=0.02) is False
        assert _usage(kernel)[0] == "succeeded"
    finally:
        release.set()
        caller.join(2)
        assert not caller.is_alive()
        assert gateway.close(wait=True, timeout=1)
    assert outcomes == [[[1.0, 0.0]]]


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_borrowed_executor_is_not_shutdown_and_pending_work_is_not_abandoned(
    kernel: SubjectKernel, queued: bool
) -> None:
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    if queued:
        executor.submit(release.wait, 5)
    provider = _Provider(None if queued else release)
    gateway = _gateway(kernel, provider, executor=executor)
    try:
        _start_timeout(kernel, gateway)
        assert gateway.close(wait=True, timeout=0.01) is False
        assert await gateway.aclose(timeout=0.02) is False
        kernel.admission.begin_drain()
        release.set()
        assert await gateway.aclose(timeout=2)
        assert executor.submit(lambda: 42).result(1) == 42
        if queued:
            assert provider.calls == 0
        assert _usage(kernel) == ("unknown", "embedding_hard_timeout")
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)
        assert gateway.close(wait=True, timeout=1)


@pytest.mark.parametrize("kind", ["thread_start", "executor_submit", "bad_input"])
def test_failed_dispatch_or_input_does_not_leak_active_calls(
    kernel: SubjectKernel, kind: str
) -> None:
    executor = ThreadPoolExecutor(max_workers=1) if kind == "executor_submit" else None
    gateway = _gateway(kernel, _Provider(), executor=executor)
    try:
        if kind == "executor_submit":
            assert executor is not None
            executor.shutdown(wait=True)
            with pytest.raises(EmbeddingProviderError):
                gateway.embed(("test",))
        elif kind == "thread_start":
            with (
                patch("threading.Thread.start", side_effect=RuntimeError("fixture failure")),
                pytest.raises(EmbeddingProviderError),
            ):
                gateway.embed(("test",))
        else:
            with pytest.raises(ValueError):
                gateway.embed(())
        assert gateway.close(wait=True, timeout=0) is True
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
        gateway.close(wait=True, timeout=1)


@pytest.mark.parametrize("timeout", [-1.0, 301.0, float("nan"), float("inf"), -float("inf"), True])
def test_invalid_shutdown_timeout_is_rejected_without_closing(timeout: float) -> None:
    gateway = EmbeddingGateway(
        _Provider(),
        Mock(spec=EmbeddingLedger),
        subject_id="Noyra-r05",
        resource_id="test",
        model="test",
        limits=EmbeddingBudgetLimits(10, 100, 100),
        pricing=EmbeddingPricing(1),
        circuit_policy=EmbeddingCircuitPolicy(3, 60),
        timeout_seconds=1,
    )
    with pytest.raises(ValueError, match="timeout"):
        gateway.close(wait=True, timeout=timeout)
    assert gateway._closed is False
    assert gateway.close(wait=True, timeout=0) is True


@pytest.mark.asyncio
async def test_default_sync_wait_is_finite_and_async_wait_keeps_loop_live(
    kernel: SubjectKernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module, "DEFAULT_EMBEDDING_CLOSE_TIMEOUT_SECONDS", 0.02)
    release = threading.Event()
    gateway = _gateway(kernel, _Provider(release))
    _start_timeout(kernel, gateway)
    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_later(0.005, heartbeat.set)
    try:
        assert gateway.close(wait=True) is False
        assert await gateway.aclose() is False
        assert heartbeat.is_set()
        assert not release.is_set()
    finally:
        release.set()
        assert gateway.close(wait=True, timeout=2)


@pytest.mark.asyncio
async def test_concurrent_cycle_close_is_idempotent(
    kernel: SubjectKernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle = _cycle(kernel, None)
    original = cycle.autonomous_research.aclose
    spy = AsyncMock(side_effect=original)
    monkeypatch.setattr(cycle.autonomous_research, "aclose", spy)
    await asyncio.gather(cycle.aclose(), cycle.aclose())
    await cycle.aclose()
    assert spy.await_count == 1
    assert cycle._closed


@pytest.mark.asyncio
async def test_service_async_and_sync_close_retain_real_ownership_until_worker_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module, "DEFAULT_EMBEDDING_CLOSE_TIMEOUT_SECONDS", 0.03)
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="Noyra-r05-service",
        genesis_hash=content_hash("service-r05"),
        host="127.0.0.1",
        port=0,
    )
    service = NoyraService(settings)
    service.boot()
    release = threading.Event()
    gateway = _gateway(service.kernel, _Provider(release))
    cycle = _cycle(service.kernel, gateway)
    service.cognition = cycle
    _start_timeout(service.kernel, gateway)
    contender = ProcessLock(service.kernel.process_lock.path)

    async def stop_loop(stop_event: asyncio.Event) -> None:
        return

    monkeypatch.setattr(service, "boot", lambda: None)
    monkeypatch.setattr(service.loop, "run_forever", stop_loop)
    try:
        with pytest.raises(ModelGatewayError, match="embedding_shutdown_incomplete"):
            await asyncio.wait_for(service.run(), timeout=5)
        assert service.kernel.process_lock.held
        with pytest.raises(RuntimeOwnershipError):
            contender.acquire()
        with pytest.raises(ModelGatewayError, match="embedding_shutdown_incomplete"):
            service.close()
        assert service.kernel.process_lock.held
        assert _usage(service.kernel) == ("unknown", "embedding_hard_timeout")
        release.set()
        assert await gateway.aclose(timeout=2)
        await cycle.aclose()
        service.close()
        assert not service.kernel.process_lock.held
        assert contender.acquire()
        contender.release()
    finally:
        release.set()
        assert gateway.close(wait=True, timeout=2)
        await cycle.aclose()
        contender.release()
        service.close()


@pytest.mark.asyncio
async def test_accepted_call_is_tracked_before_a_worker_exists(
    kernel: SubjectKernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    provider = _Provider()
    gateway = _gateway(kernel, provider)
    gateway.timeout_seconds = 1.0
    original = gateway.ledger.authorize
    results: list[Any] = []

    def authorize(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test watchdog expired")
        return original(*args, **kwargs)

    monkeypatch.setattr(gateway.ledger, "authorize", authorize)

    def invoke() -> None:
        try:
            results.append(gateway.embed(("already admitted",)))
        except BaseException as error:
            results.append(error)

    caller = threading.Thread(target=invoke, daemon=True)
    caller.start()
    try:
        assert entered.wait(2)
        assert not gateway._workers and not gateway._futures
        assert gateway.close() is False
        assert await gateway.aclose(timeout=0.02) is False
        with pytest.raises(EmbeddingProviderError, match="embedding_gateway_closed"):
            gateway.embed(("new call must be refused",))
        with kernel.database.connection() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM embedding_usage_entries").fetchone()[0]
                == 0
            )
    finally:
        release.set()
        caller.join(2)
        assert not caller.is_alive()
        assert gateway.close(wait=True, timeout=2)
    assert results == [[[1.0, 0.0]]]
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_concurrent_incomplete_closes_do_not_skip_the_retry_guard(
    kernel: SubjectKernel,
) -> None:
    release = threading.Event()
    gateway = _gateway(kernel, _Provider(release))
    cycle = _cycle(kernel, gateway)
    _start_timeout(kernel, gateway)
    cleanup = AsyncMock()
    with patch.object(cycle.autonomous_research, "aclose", cleanup):
        try:
            results = await asyncio.gather(
                cycle.aclose(embedding_timeout=0.02),
                cycle.aclose(embedding_timeout=0.02),
                return_exceptions=True,
            )
            assert all(isinstance(error, ModelGatewayError) for error in results)
            assert all(str(error) == "embedding_shutdown_incomplete" for error in results)
            assert cleanup.await_count == 0
            assert not cycle._closed
            assert not cycle._close_lock.locked()
        finally:
            release.set()
            assert gateway.close(wait=True, timeout=2)
            await cycle.aclose()
        assert cleanup.await_count == 1
        assert cycle._closed


def test_sync_close_still_supports_a_partially_initialized_service(kernel: SubjectKernel) -> None:
    service = object.__new__(NoyraService)
    service.kernel = kernel
    service.http = Mock()
    service.close()
    assert not kernel.process_lock.held
