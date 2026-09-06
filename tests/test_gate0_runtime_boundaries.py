from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from noyra.core import OperationInvalidated, RuntimeAdmissionGate, SubjectKernel, bind_lease
from noyra.core.at_rest import AtRestError
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.integrity import (
    IntegrityCheckOutcome,
    IntegrityCheckSpec,
    IntegrityRegistry,
    IntegrityRuntimeController,
)
from noyra.core.operator_controls import OperatorControlConflict, OperatorControlService
from noyra.core.types import content_hash
from noyra.interaction import (
    DeliveryDispatcher,
    InteractionStore,
    TransportInput,
    TransportStore,
)
from noyra.model import (
    BudgetLimits,
    CompletionRequest,
    ModelGateway,
    ModelLedger,
    ModelMessage,
    ModelUsage,
    ProviderResponse,
)
from noyra.service import NoyraService, ServiceSettings


class _Gate0Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str


class _BlockingProvider:
    name = "gate0-blocking-provider"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: CompletionRequest) -> ProviderResponse:
        del request
        self.entered.set()
        await self.release.wait()
        return ProviderResponse(
            content=json.dumps({"value": "stale"}),
            usage=ModelUsage(1, 1),
            finish_reason="stop",
            provider_request_id="gate0-provider-request",
        )


class _BlockingInvalidOutputProvider(_BlockingProvider):
    async def complete(self, request: CompletionRequest) -> ProviderResponse:
        del request
        self.entered.set()
        await self.release.wait()
        return ProviderResponse(
            content="not-json",
            usage=ModelUsage(1, 1),
            finish_reason="stop",
            provider_request_id="gate0-invalid-provider-request",
        )


def _settings(root: Path, subject_id: str, **overrides: object) -> ServiceSettings:
    return ServiceSettings(
        data_dir=root,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr("gate0-admin-token-" + "x" * 32),
        **cast(dict[str, Any], overrides),
    )


def _wait_for_http_thread(service: NoyraService, timeout: float = 30.0) -> None:
    """Wait for the service HTTP thread without assuming a fast local start."""

    deadline = time.monotonic() + timeout
    while service.http.thread is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if service.http.thread is None:
        raise AssertionError(f"HTTP server did not start within {timeout:.1f}s")


def _join_started(thread: threading.Thread, timeout: float = 10.0) -> None:
    """Join a thread only after it has been started (including failed setup paths)."""

    if thread.ident is not None:
        thread.join(timeout)


def test_second_service_cannot_touch_database_before_ownership(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "runtime", "Noyra-gate0-owner")
    first = NoyraService(settings)
    try:
        database = settings.data_dir / "noyra.sqlite3"
        before = database.read_bytes()
        sidecars = {
            suffix: database.with_name(database.name + suffix) for suffix in ("-wal", "-shm")
        }
        sidecars_before = {
            suffix: path.read_bytes() if path.exists() else None
            for suffix, path in sidecars.items()
        }
        with patch(
            "noyra.core.database.sqlite3.connect",
            side_effect=AssertionError("unowned preflight must not open SQLite"),
        ):
            second = NoyraService(settings)
            with pytest.raises(RuntimeOwnershipError):
                second.boot()
        health = second.kernel.health()
        assert health["subject_id"] == settings.subject_id
        assert database.read_bytes() == before
        for suffix, path in sidecars.items():
            assert (path.read_bytes() if path.exists() else None) == sidecars_before[suffix]
        second.close()
    finally:
        first.close()


def test_training_withdrawal_survives_restart(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path / "consent",
        "Noyra-gate0-consent",
        training_record_enabled=True,
        training_export_enabled=True,
    )
    first = NoyraService(settings)
    first.boot()
    first.http.training.update_policy(
        settings.subject_id,
        actor="gate0-test",
        reason="withdraw consent",
        record_enabled=False,
        export_enabled=False,
    )
    first.close()

    second = NoyraService(settings)
    try:
        second.boot()
        policy = second.http.training.policy(settings.subject_id)
        assert policy.record_enabled is False
        assert policy.export_enabled is False
    finally:
        second.close()


def test_integrity_quarantine_rejects_http_mutation(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "quarantine", "Noyra-gate0-quarantine")
    service = NoyraService(settings)
    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "gate0.corrupt",
                1,
                "gate0",
                frozenset({"startup_light"}),
                lambda _context: IntegrityCheckOutcome("corrupt", "p0", "fixture"),
                "light",
            ),
        )
    )
    service.integrity = IntegrityRuntimeController(
        service.kernel,
        settings.data_dir,
        policy_mode="pause",
        registry=registry,
    )
    service.http.integrity = service.integrity
    before = 0
    try:
        service.boot()
        service.http.start()
        with service.kernel.database.connection() as connection:
            before = int(
                connection.execute(
                    "SELECT COUNT(*) FROM interactions WHERE subject_id = ?",
                    (settings.subject_id,),
                ).fetchone()[0]
            )
        request = Request(
            f"http://127.0.0.1:{service.http.address[1]}/api/interactions",
            data=json.dumps({"content": "must be quarantined"}).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer gate0-admin-token-" + "x" * 32,
                "Content-Type": "application/json",
            },
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=5)
        assert error.value.code == 503
        with service.kernel.database.connection() as connection:
            after = int(
                connection.execute(
                    "SELECT COUNT(*) FROM interactions WHERE subject_id = ?",
                    (settings.subject_id,),
                ).fetchone()[0]
            )
        assert after == before
    finally:
        service.close()


def test_quarantine_race_cannot_fence_ordinary_http_mutation(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "quarantine-race", "Noyra-gate0-quarantine-race")
    service = NoyraService(settings)
    service.boot()
    service.http.start()
    original_allow_mutation = service.http.allow_mutation

    def invalidate_after_route_check(path: str) -> bool:
        allowed = bool(original_allow_mutation(path))
        service.kernel.admission.quarantine()
        return allowed

    service.http.allow_mutation = invalidate_after_route_check
    try:
        request = Request(
            f"http://127.0.0.1:{service.http.address[1]}/api/interactions",
            data=json.dumps({"content": "must not cross quarantine race"}).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer gate0-admin-token-" + "x" * 32,
                "Content-Type": "application/json",
            },
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=5)
        assert error.value.code == 503
        with service.kernel.database.connection() as connection:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM interactions WHERE subject_id = ?",
                    (settings.subject_id,),
                ).fetchone()[0]
            )
        assert count == 0
    finally:
        service.close()


def test_runtime_epoch_invalidates_existing_operation() -> None:
    gate = RuntimeAdmissionGate("Noyra-gate0-epoch")
    with gate.operation("test") as lease:
        lease.assert_current()
        gate.invalidate()
        with pytest.raises(OperationInvalidated):
            lease.assert_current()


def test_failed_pause_reopens_active_admission_with_new_epoch(tmp_path: Path) -> None:
    subject_id = "Noyra-gate0-pause-failure"
    kernel = SubjectKernel(
        tmp_path / "pause-failure.sqlite3",
        subject_id,
        content_hash({"subject": subject_id}),
    )
    try:
        kernel.boot()
        kernel.orient()
        kernel.activate()
        controls = OperatorControlService(kernel)

        def fail_transition(*_args: object, **_kwargs: object) -> Any:
            raise RuntimeError("injected pause failure")

        original_transition = kernel.lifecycle.transition
        kernel.lifecycle.transition = fail_transition  # type: ignore[method-assign]
        try:
            with pytest.raises(OperatorControlConflict) as error:
                controls.pause(actor="gate0-test", reason="inject pause failure")
            assert error.value.code == "lifecycle_pause_failed"
        finally:
            kernel.lifecycle.transition = original_transition  # type: ignore[method-assign]

        assert kernel.lifecycle.current().state == "active"
        assert kernel.admission.accepting
        lease = kernel.admission.begin("post-failure-operation")
        kernel.admission.finish(lease)
    finally:
        kernel.close()


def test_boot_readiness_failure_releases_startup_lock(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "boot-readiness", "Noyra-gate0-boot-lock")
    first = NoyraService(settings)

    class FailedAtRest:
        @staticmethod
        def require_ready() -> dict[str, object]:
            raise AtRestError("injected readiness failure")

    first.at_rest = FailedAtRest()  # type: ignore[assignment]
    try:
        with pytest.raises(AtRestError):
            first.boot()
        second = NoyraService(settings)
        try:
            assert second._unowned_preflight is False
        finally:
            second.close()
    finally:
        first.close()


@pytest.mark.asyncio
async def test_provider_result_cannot_cross_pause_epoch(tmp_path: Path) -> None:
    subject_id = "Noyra-gate0-provider-epoch"
    kernel = SubjectKernel(
        tmp_path / "provider.sqlite3",
        subject_id,
        content_hash({"subject": subject_id}),
    )
    try:
        kernel.boot()
        kernel.orient()
        kernel.activate()
        provider = _BlockingProvider()
        gateway = ModelGateway(
            provider,
            ModelLedger(kernel.database),
            model="gate0-model",
            limits=BudgetLimits(
                daily_attempts=4,
                daily_input_tokens=1_000,
                daily_output_tokens=1_000,
                daily_cost_microusd=1_000_000,
            ),
        )
        lease = kernel.admission.begin("provider-test")
        try:
            with bind_lease(lease):
                task = asyncio.create_task(
                    gateway.complete_structured(
                        subject_id,
                        "gate0_epoch",
                        (ModelMessage(role="user", content="test"),),
                        _Gate0Output,
                        idempotency_key="gate0-provider-epoch",
                        max_output_tokens=10,
                    )
                )
            await asyncio.wait_for(provider.entered.wait(), timeout=5)
            kernel.pause("gate0 pause")
            provider.release.set()
            with pytest.raises(OperationInvalidated):
                await task
        finally:
            kernel.admission.finish(lease)
        with kernel.database.connection() as connection:
            row = connection.execute(
                "SELECT status FROM model_calls WHERE subject_id = ? AND purpose = ?",
                (subject_id, "gate0_epoch"),
            ).fetchone()
        assert row is not None and row["status"] == "succeeded"
    finally:
        kernel.close()


@pytest.mark.asyncio
async def test_stale_invalid_provider_result_is_accounted_then_fenced(tmp_path: Path) -> None:
    subject_id = "Noyra-gate0-provider-invalid-epoch"
    kernel = SubjectKernel(
        tmp_path / "provider-invalid.sqlite3",
        subject_id,
        content_hash({"subject": subject_id}),
    )
    try:
        kernel.boot()
        kernel.orient()
        kernel.activate()
        provider = _BlockingInvalidOutputProvider()
        gateway = ModelGateway(
            provider,
            ModelLedger(kernel.database),
            model="gate0-invalid-model",
            limits=BudgetLimits(
                daily_attempts=4,
                daily_input_tokens=1_000,
                daily_output_tokens=1_000,
                daily_cost_microusd=1_000_000,
            ),
        )
        lease = kernel.admission.begin("provider-invalid-test")
        try:
            with bind_lease(lease):
                task = asyncio.create_task(
                    gateway.complete_structured(
                        subject_id,
                        "gate0_invalid_epoch",
                        (ModelMessage(role="user", content="test"),),
                        _Gate0Output,
                        idempotency_key="gate0-provider-invalid-epoch",
                        max_output_tokens=10,
                    )
                )
            await asyncio.wait_for(provider.entered.wait(), timeout=5)
            kernel.pause("gate0 invalid output pause")
            provider.release.set()
            with pytest.raises(OperationInvalidated):
                await task
        finally:
            kernel.admission.finish(lease)
        with kernel.database.connection() as connection:
            call = connection.execute(
                "SELECT call_id, status, error_code FROM model_calls "
                "WHERE subject_id = ? AND purpose = ?",
                (subject_id, "gate0_invalid_epoch"),
            ).fetchone()
            attempt = connection.execute(
                "SELECT status, input_tokens, output_tokens FROM model_attempts WHERE call_id = ?",
                (call["call_id"],),
            ).fetchone()
        assert call is not None and call["status"] == "failed"
        assert call["error_code"] == "structured_output_invalid"
        assert attempt is not None
        assert (attempt["status"], attempt["input_tokens"], attempt["output_tokens"]) == (
            "succeeded",
            1,
            1,
        )
    finally:
        kernel.close()


def test_http_close_waits_for_inflight_handler(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "http-drain", "Noyra-gate0-http-drain")
    service = NoyraService(settings)
    service.boot()
    service.http.start()
    entered = threading.Event()
    release = threading.Event()
    original_receive = service.http.interactions.receive

    def blocked_receive(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        release.wait(5)
        return original_receive(*args, **kwargs)

    service.http.interactions.receive = blocked_receive
    result: list[object] = []

    def send_request() -> None:
        request = Request(
            f"http://127.0.0.1:{service.http.address[1]}/api/interactions",
            data=json.dumps({"content": "blocked"}).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer gate0-admin-token-" + "x" * 32,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                result.append(response.status)
        except Exception as error:  # pragma: no cover - diagnostic on failure
            result.append(type(error).__name__)

    request_thread = threading.Thread(target=send_request)
    request_thread.start()
    assert entered.wait(5)
    close_done = threading.Event()

    def close_http() -> None:
        service.http.close()
        close_done.set()

    close_thread = threading.Thread(target=close_http)
    close_thread.start()
    time.sleep(0.2)
    assert not close_done.is_set()
    release.set()
    request_thread.join(10)
    close_thread.join(10)
    assert close_done.is_set()
    assert result == [201]
    service.kernel.close()


def test_service_shutdown_does_not_block_service_loop_on_async_handler(
    tmp_path: Path,
) -> None:
    """A handler waiting on the service loop must be drainable during shutdown."""

    settings = _settings(
        tmp_path / "http-async-drain",
        "Noyra-gate0-async-drain",
        request_timeout_seconds=2,
    )
    service = NoyraService(settings)
    entered = threading.Event()
    release = threading.Event()
    run_done = threading.Event()
    request_done = threading.Event()
    result: list[object] = []

    async def blocked_lookup(*_args: object, **_kwargs: object) -> Any:
        entered.set()
        await asyncio.to_thread(release.wait)
        return SimpleNamespace(delivery_id="gate0-async", status="unknown")

    service.http.deliveries.lookup_unknown = blocked_lookup

    def run_service() -> None:
        try:
            asyncio.run(service.run())
        except BaseException as error:  # pragma: no cover - diagnostic on failure
            result.append(error)
        finally:
            run_done.set()

    def send_lookup() -> None:
        request = Request(
            f"http://127.0.0.1:{service.http.address[1]}/api/deliveries/gate0-async/lookup",
            data=json.dumps({"reason": "gate0 shutdown"}).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer gate0-admin-token-" + "x" * 32,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                result.append(response.status)
        except Exception as error:  # pragma: no cover - diagnostic on failure
            result.append(type(error).__name__)
        finally:
            request_done.set()

    run_thread = threading.Thread(target=run_service)
    request_thread = threading.Thread(target=send_lookup)
    try:
        run_thread.start()
        _wait_for_http_thread(service)
        request_thread.start()
        assert entered.wait(5)

        service.request_shutdown()
        time.sleep(0.2)
        assert not run_done.is_set()

        release.set()
        request_thread.join(10)
        run_thread.join(10)
        assert request_done.is_set()
        assert run_done.is_set()
        assert result == [200]
    finally:
        release.set()
        service.request_shutdown()
        _join_started(request_thread)
        _join_started(run_thread)
        service.close()


def test_service_run_waits_for_admission_leases_before_releasing_lock(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path / "lease-drain",
        "Noyra-gate0-lease-drain",
        active_interval_seconds=60,
        sleep_interval_seconds=60,
        integrity_interval_seconds=60,
    )
    service = NoyraService(settings)
    lease = None

    async def exercise() -> None:
        nonlocal lease
        task = asyncio.create_task(service.run())
        try:
            deadline = time.monotonic() + 30
            while (
                service.http.thread is None
                or service.kernel.lifecycle.current().state != "active"
                or not service.kernel.admission.accepting
            ) and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert service.http.thread is not None
            lease = service.kernel.admission.begin("gate0-shutdown-drain")
            service.request_shutdown()
            await asyncio.sleep(0.1)
            assert not task.done()
            assert service.kernel.process_lock.held
            assert service.kernel.admission.active_operations == 1
            service.kernel.admission.finish(lease)
            lease = None
            await asyncio.wait_for(task, timeout=5)
            assert not service.kernel.process_lock.held
        finally:
            if lease is not None:
                service.kernel.admission.finish(lease)
                lease = None
            if not task.done():
                service.request_shutdown()
                await asyncio.wait_for(task, timeout=5)

    asyncio.run(exercise())


def test_service_close_waits_for_admission_leases_before_releasing_lock(
    tmp_path: Path,
) -> None:
    service = NoyraService(_settings(tmp_path / "close-drain", "Noyra-gate0-close-drain"))
    service.boot()
    lease = service.kernel.admission.begin("gate0-close-drain")
    close_done = threading.Event()

    def close_service() -> None:
        service.close()
        close_done.set()

    close_thread = threading.Thread(target=close_service)
    try:
        close_thread.start()
        time.sleep(0.1)
        assert not close_done.is_set()
        assert service.kernel.process_lock.held
        service.kernel.admission.finish(lease)
        close_thread.join(5)
        assert close_done.is_set()
        assert not service.kernel.process_lock.held
    finally:
        service.kernel.admission.finish(lease)
        if close_thread.is_alive():
            close_thread.join(5)
        service.close()


def test_clean_integrity_report_does_not_reopen_paused_admission(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path / "paused-clean",
        "Noyra-gate0-paused-clean",
        integrity_mode="pause",
    )
    service = NoyraService(settings)
    service.boot()
    try:
        service.kernel.pause("gate0 paused-clean fixture")
        assert service.kernel.lifecycle.current().state == "paused"
        assert not service.kernel.admission.accepting

        registry = IntegrityRegistry(
            (
                IntegrityCheckSpec(
                    "gate0.clean_periodic",
                    1,
                    "gate0",
                    frozenset({"periodic_deep"}),
                    lambda _context: IntegrityCheckOutcome(),
                    "light",
                ),
            )
        )
        service.integrity = IntegrityRuntimeController(
            service.kernel,
            settings.data_dir,
            policy_mode="pause",
            interval_seconds=0.01,
            registry=registry,
        )
        service.http.integrity = service.integrity
        service.loop.pre_tick_hook = service._integrity_pre_tick
        service.loop.next_wakeup_hook = service.integrity.seconds_until_due

        assert asyncio.run(service._integrity_pre_tick()) is None
        assert not service.kernel.admission.accepting
    finally:
        service.close()


def test_async_handler_timeout_cancels_and_drains_main_loop_task(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path / "http-async-timeout",
        "Noyra-gate0-async-timeout",
        request_timeout_seconds=1,
    )
    service = NoyraService(settings)
    entered = threading.Event()
    cancelled = threading.Event()
    release = threading.Event()
    run_done = threading.Event()
    request_done = threading.Event()

    async def stubborn_lookup(*_args: object, **_kwargs: object) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await asyncio.to_thread(release.wait)
            raise

    service.http.deliveries.lookup_unknown = stubborn_lookup

    def run_service() -> None:
        try:
            asyncio.run(service.run())
        finally:
            run_done.set()

    def send_lookup() -> None:
        request = Request(
            f"http://127.0.0.1:{service.http.address[1]}/api/deliveries/gate0-timeout/lookup",
            data=json.dumps({"reason": "gate0 timeout"}).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer gate0-admin-token-" + "x" * 32,
                "Content-Type": "application/json",
            },
        )
        try:
            urlopen(request, timeout=10).close()
        except Exception:
            pass
        finally:
            request_done.set()

    run_thread = threading.Thread(target=run_service)
    request_thread = threading.Thread(target=send_lookup)
    try:
        run_thread.start()
        _wait_for_http_thread(service)
        request_thread.start()
        assert entered.wait(5)
        assert cancelled.wait(5)
        service.request_shutdown()
        time.sleep(0.2)
        assert not run_done.is_set()

        release.set()
        request_thread.join(10)
        run_thread.join(10)
        assert request_done.is_set()
        assert run_done.is_set()
    finally:
        release.set()
        service.request_shutdown()
        _join_started(request_thread)
        _join_started(run_thread)
        service.close()


@pytest.mark.asyncio
async def test_delivery_cancellation_stops_queue_progression(tmp_path: Path) -> None:
    subject_id = "Noyra-gate0-delivery"
    kernel = SubjectKernel(
        tmp_path / "delivery.sqlite3",
        subject_id,
        content_hash({"subject": subject_id}),
    )
    try:
        kernel.boot()
        transports = TransportStore(kernel.database, tmp_path / "secrets")
        transports.configure(
            subject_id,
            TransportInput(
                channel="webhook",
                label="gate0-webhook",
                endpoint="https://delivery.example/send",
                settings={},
            ),
            actor="gate0-test",
        )
        interactions = InteractionStore(kernel.database)
        interactions.send(
            subject_id,
            "webhook",
            "target",
            "first",
            idempotency_key="gate0-first",
        )
        interactions.send(
            subject_id,
            "webhook",
            "target",
            "second",
            idempotency_key="gate0-second",
        )
        dispatcher = DeliveryDispatcher(kernel.database, transports)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked(*_args: object, **_kwargs: object) -> str | None:
            entered.set()
            await release.wait()
            return None

        dispatcher._send = blocked  # type: ignore[method-assign]
        task = asyncio.create_task(dispatcher.deliver_pending(subject_id, limit=2))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with kernel.database.connection() as connection:
            rows = connection.execute(
                "SELECT i.content, d.status FROM interaction_deliveries d "
                "JOIN interactions i ON i.interaction_id = d.interaction_id "
                "WHERE d.subject_id = ? ORDER BY i.content",
                (subject_id,),
            ).fetchall()
        assert [(str(row["content"]), str(row["status"])) for row in rows] == [
            ("first", "unknown"),
            ("second", "queued"),
        ]
    finally:
        kernel.close()
