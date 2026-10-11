from __future__ import annotations

import asyncio
import json
import smtplib
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.admission import OperationInvalidated, RuntimeAdmissionGate, bind_lease
from noyra.core.types import content_hash
from noyra.interaction import DeliveryDispatcher, InteractionStore, TransportInput, TransportStore
from noyra.interaction.smtp_deadline import SMTPOperation


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connect", "tls", "auth_challenge", "data"])
@pytest.mark.parametrize("withdraw", ["lease", "disable", "credential"])
async def test_smtp_revocation_at_each_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, withdraw: str
) -> None:
    db = Database(tmp_path / "test.sqlite3")
    subject = "smtp-dispatch"
    IdentityStore(db).ensure(subject, content_hash(subject))
    gate = RuntimeAdmissionGate(subject)
    gate.open()
    store = TransportStore(db, tmp_path / "secrets")
    record = store.configure(
        subject,
        TransportInput(
            channel="email",
            label="synthetic",
            endpoint="smtp://mail.example:587",
            credentials={
                "username": SecretStr("synthetic"),
                "password": SecretStr("synthetic"),
                "from_address": SecretStr("sender@example.com"),
            },
        ),
        actor="operator",
    )
    InteractionStore(db).send(subject, "email", "recipient@example.com", "hello")
    writes: list[str] = []

    def invalidate(at: str) -> None:
        if at != phase:
            return
        if withdraw == "lease":
            gate.invalidate()
        elif withdraw == "disable":
            store.disable(record.transport_id, subject_id=subject, actor="operator", reason="test")
        else:
            path = tmp_path / "secrets" / f"{record.transport_id}.json"
            data = json.loads(path.read_text())
            data["credentials"]["password"] = "rotated-synthetic"
            path.write_text(json.dumps(data))

    class Peer:
        def getpeername(self) -> tuple[str, int]:
            return "8.8.8.8", 587

    class SMTP:
        def __init__(self, **kwargs: Any) -> None:
            self.sock: Any = None

        def connect(self, *args: Any) -> tuple[int, bytes]:
            self.sock = Peer()
            invalidate("connect")
            return 220, b"ready"

        def starttls(self, **kwargs: Any) -> None:
            invalidate("tls")

        def send(self, data: str) -> None:
            writes.append(data)

        def login(self, *args: Any) -> None:
            self.send("AUTH")
            invalidate("auth_challenge")
            self.send("AUTH_RESPONSE")

        def send_message(self, *args: Any) -> None:
            self.send("DATA")
            invalidate("data")
            self.send("BODY")

        def close(self) -> None:
            pass

    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    monkeypatch.setattr("noyra.interaction.transport.public_addresses", lambda *a, **k: ["8.8.8.8"])
    dispatcher = DeliveryDispatcher(db, store)
    with gate.operation("test") as lease, bind_lease(lease):
        if withdraw == "lease":
            with pytest.raises(OperationInvalidated):
                await dispatcher.deliver_pending(subject)
        else:
            result = await dispatcher.deliver_pending(subject)
            assert result[0].status == ("unknown" if phase == "data" else "failed")
            if phase != "data":
                assert result[0].attempts >= 3
            assert await dispatcher.deliver_pending(subject) == []
    assert "BODY" not in writes
    if phase in {"connect", "tls"}:
        assert writes == []
    if phase == "auth_challenge":
        assert writes == ["AUTH"]
    await dispatcher.aclose()


def test_real_smtp_greeting_drip_stops_at_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    stopped = threading.Event()

    def drip() -> None:
        try:
            with listener.accept()[0] as peer:
                for byte in b"220 a very slow greeting\r\n":
                    if stopped.wait(0.04):
                        break
                    peer.sendall(bytes([byte]))
        except OSError:
            pass

    worker = threading.Thread(target=drip, daemon=True)
    worker.start()
    monkeypatch.setattr(
        "noyra.interaction.transport.public_addresses", lambda *a, **k: ["127.0.0.1"]
    )
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, smtplib.SMTPServerDisconnected)):
            DeliveryDispatcher._send_email(
                f"smtp://mail.example:{port}",
                {"from_address": "s@example.com"},
                "r@example.com",
                "synthetic",
                "drip",
                operation=SMTPOperation(0.2),
            )
        assert time.monotonic() - started < 1.5
    finally:
        stopped.set()
        listener.close()
        worker.join(2)
    assert not worker.is_alive()


def test_deadline_before_connect_installs_socket_cannot_enter_greeting_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[int] = []
    operation = SMTPOperation(10)

    class SMTP:
        sock = None

        def __init__(self, **kwargs: Any) -> None:
            pass

        def connect(self, *args: Any) -> tuple[int, bytes]:
            operation.abort()
            return self.getreply()

        def getreply(self) -> tuple[int, bytes]:
            reads.append(1)
            return 220, b"ready"

        def close(self) -> None:
            pass

    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    monkeypatch.setattr("noyra.interaction.transport.public_addresses", lambda *a, **k: ["8.8.8.8"])
    with pytest.raises(TimeoutError):
        DeliveryDispatcher._send_email(
            "smtp://mail.example:587",
            {"from_address": "s@example.com"},
            "r@example.com",
            "synthetic",
            "late-connect",
            operation=operation,
        )
    assert not reads


@pytest.mark.asyncio
async def test_smtp_cancel_does_not_wait_for_dns_or_allow_late_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    sends: list[str] = []

    def dns(*args: Any, **kwargs: Any) -> list[str]:
        entered.set()
        release.wait(5)
        return ["8.8.8.8"]

    monkeypatch.setattr("noyra.interaction.transport.public_addresses", dns)
    operation = SMTPOperation(10)

    def send() -> str:
        try:
            return DeliveryDispatcher._send_email(
                "smtp://mail.example:587",
                {"from_address": "s@example.com"},
                "r@example.com",
                "synthetic",
                "cancel",
                lambda: sends.append("auth"),
                operation=operation,
            )
        finally:
            exited.set()

    task = asyncio.create_task(operation.run(send))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        assert not release.is_set()
    finally:
        release.set()
    while not exited.is_set():
        await asyncio.sleep(0.01)
    assert sends == []


@pytest.mark.asyncio
async def test_timed_out_smtp_workers_remain_bounded() -> None:
    release = threading.Event()
    started: list[int] = []

    def blocked() -> str:
        started.append(1)
        release.wait(5)
        return "late"

    try:
        results = await asyncio.gather(
            *(SMTPOperation(0.1).run(blocked) for _ in range(12)), return_exceptions=True
        )
        assert all(isinstance(result, TimeoutError) for result in results)
        assert len(started) <= 4
    finally:
        release.set()
    assert await SMTPOperation(2).run(lambda: "next") == "next"
