from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from noyra.core.admission import RuntimeAdmissionGate, bind_lease
from noyra.core.http import SyncHTTPTimeoutHook
from noyra.core.types import utc_now
from noyra.wallet import (
    HTTPSWalletSigner,
    MockSigner,
    PaymentPolicyInput,
    WalletPaymentExecutionEngine,
)
from noyra.wallet.dispatch import WalletRequestFence, wallet_policy_scope
from noyra.wallet.execution import WalletBroadcastUnknownError
from test_wallet_execution import fixture


def change_policy(engine: WalletPaymentExecutionEngine, subject: str, kind: str) -> None:
    policy = engine.economy.get_policy(subject)
    proposal = PaymentPolicyInput(mode="automatic", per_order_limit="20", daily_limit="100")
    updates: dict[str, dict[str, Any]] = {
        "disabled": {"mode": "disabled"},
        "paused": {"emergency_paused": True},
        "automation": {"automation_enabled": False},
        "limit": {"per_order_limit": "1"},
        "version": {"daily_limit": "200"},
    }
    update = updates[kind]
    engine.economy.update_policy(
        subject,
        proposal.model_copy(update=update),
        expected_version=policy.policy_version,
        actor="operator",
    )


@pytest.mark.parametrize("kind", ["disabled", "paused", "automation", "limit", "version"])
@pytest.mark.parametrize("retry", [False, True])
def test_policy_change_after_prepare_blocks_new_signature_and_preserves_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, retry: bool
) -> None:
    db, subject, _network, _asset, _source, order = fixture(tmp_path)
    signer = MockSigner(lose_first_response=retry)
    engine = WalletPaymentExecutionEngine(db, signer)
    previous = engine.execute_order(order.order_id, subject, actor="operator") if retry else None
    original = engine._prepare

    def prepared(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        change_policy(engine, subject, kind)
        return result

    monkeypatch.setattr(engine, "_prepare", prepared)
    result = (
        engine.retry_unknown(order.order_id, subject, actor="operator", reason="test")
        if retry
        else engine.execute_order(order.order_id, subject, actor="operator")
    )
    assert len(signer.requests) == int(retry)
    assert result.error_code == "dispatch_authorization_changed"
    assert result.status == ("unknown" if retry else "failed")
    if previous:
        assert result.tx_hash == previous.tx_hash and result.nonce == previous.nonce
    engine.economy.verify_integrity(subject)


@pytest.mark.parametrize("when", ["before_headers", "after_body"])
def test_https_late_worker_cannot_start_payment_after_timeout(tmp_path: Path, when: str) -> None:
    db, subject, _network, _asset, _source, order = fixture(tmp_path)
    gate = RuntimeAdmissionGate(subject)
    engine = WalletPaymentExecutionEngine(db, MockSigner())
    execution, transfer, request_id = engine._prepare(
        order.order_id,
        subject,
        actor="operator",
        gas_limit=None,
        max_fee_per_gas=None,
        nonce=None,
        retry_unknown=False,
    )
    policy_version = engine.economy.get_policy(subject).policy_version
    release = threading.Event()
    done = threading.Event()
    sends: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            trace = request.extensions["trace"]
            if when == "before_headers":
                assert release.wait(10)
            trace("http11.send_request_headers.started", {})
            trace("http11.send_request_headers.complete", {})
            trace("http11.send_request_body.started", {})
            sends.append("payment")
            trace("http11.send_request_body.complete", {})
            if when == "after_body":
                assert release.wait(10)
            return httpx.Response(
                200,
                json={
                    "tx_hash": "0x" + "a" * 64,
                    "chain_id": transfer.chain_id,
                    "nonce": transfer.nonce,
                    "accepted_at": utc_now(),
                },
            )
        finally:
            done.set()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        engine.signer = HTTPSWalletSigner(
            "https://signer.example", signer_id=engine.signer_id, client=client, timeout_seconds=0.1
        )
        with gate.operation("test") as lease, bind_lease(lease):
            try:
                with pytest.raises(WalletBroadcastUnknownError):
                    engine._sign_and_broadcast(
                        transfer,
                        request_id=request_id,
                        execution=execution,
                        policy_version=policy_version,
                    )
                # The worker has released its write boundary after sending, or
                # has not acquired it yet. Invalidating must not wait on it.
                gate.invalidate()
            finally:
                release.set()
            assert done.wait(10)
    assert sends == (["payment"] if when == "after_body" else [])


def test_https_policy_change_during_connection_is_checked_in_worker(tmp_path: Path) -> None:
    db, subject, _network, _asset, _source, order = fixture(tmp_path)
    engine = WalletPaymentExecutionEngine(db, MockSigner())
    execution, transfer, request_id = engine._prepare(
        order.order_id,
        subject,
        actor="operator",
        gas_limit=None,
        max_fee_per_gas=None,
        nonce=None,
        retry_unknown=False,
    )
    policy_version = engine.economy.get_policy(subject).policy_version
    sends = []

    def handler(request: httpx.Request) -> httpx.Response:
        change_policy(engine, subject, "paused")
        request.extensions["trace"]("http11.send_request_headers.started", {})
        sends.append(request)
        raise AssertionError("withdrawn payment was sent")

    from noyra.wallet.dispatch import WalletDispatchDenied

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        engine.signer = HTTPSWalletSigner(
            "https://signer.example", signer_id=engine.signer_id, client=client, timeout_seconds=10
        )
        with pytest.raises(WalletDispatchDenied):
            engine._sign_and_broadcast(
                transfer, request_id=request_id, execution=execution, policy_version=policy_version
            )
    assert sends == []


def test_policy_update_waits_for_request_body_but_not_response(tmp_path: Path) -> None:
    db, subject, _network, _asset, _source, order = fixture(tmp_path)
    engine = WalletPaymentExecutionEngine(db, MockSigner())
    execution, transfer, request_id = engine._prepare(
        order.order_id,
        subject,
        actor="operator",
        gas_limit=None,
        max_fee_per_gas=None,
        nonce=None,
        retry_unknown=False,
    )
    version = engine.economy.get_policy(subject).policy_version
    writing = threading.Event()
    release = threading.Event()
    updated = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        trace = request.extensions["trace"]
        trace("http11.send_request_headers.started", {})
        writing.set()
        assert release.wait(10)
        trace("http11.send_request_body.started", {})
        trace("http11.send_request_body.complete", {})
        assert updated.wait(10)
        return httpx.Response(
            200,
            json={
                "tx_hash": "0x" + "a" * 64,
                "chain_id": transfer.chain_id,
                "nonce": transfer.nonce,
                "accepted_at": utc_now(),
            },
        )

    def pause() -> None:
        change_policy(engine, subject, "paused")
        updated.set()

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        ThreadPoolExecutor() as pool,
    ):
        engine.signer = HTTPSWalletSigner(
            "https://signer.example", signer_id=engine.signer_id, client=client, timeout_seconds=10
        )
        sent = pool.submit(
            engine._sign_and_broadcast,
            transfer,
            request_id=request_id,
            execution=execution,
            policy_version=version,
        )
        try:
            assert writing.wait(10)
            paused = pool.submit(pause)
            assert not updated.wait(0.1)
        finally:
            release.set()
        paused.result(10)
        assert sent.result(10).tx_hash == "0x" + "a" * 64
    # Both paths released the stable lock, including after the response wait.
    with wallet_policy_scope(db):
        pass


def test_cancelled_request_cannot_write_body_after_headers() -> None:
    import time

    from noyra.wallet.dispatch import bind_wallet_dispatch

    hook = SyncHTTPTimeoutHook()
    held = []

    @contextmanager
    def scope() -> Iterator[None]:
        held.append(True)
        try:
            yield
        finally:
            held.pop()

    with bind_wallet_dispatch(scope):
        fence = WalletRequestFence(time.monotonic() + 10, hook, payment=True)
    try:
        fence.trace("http11.send_request_headers.started", {})
        assert held == [True]
        hook.cancel()
        with pytest.raises(TimeoutError):
            fence.trace("http11.send_request_body.started", {})
    finally:
        fence.close()
    assert held == []
