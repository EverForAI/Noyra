from __future__ import annotations

import ipaddress
import json
import smtplib
import socket
import sqlite3
import tempfile
import zipfile
from collections.abc import Mapping
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from typing import Any, ClassVar, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import httpx
import pytest
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.errors import InvalidTransitionError, NotFoundError
from noyra.core.integrity import IntegrityRegistry
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.types import content_hash
from noyra.interaction import (
    DeliveryDispatcher,
    DeliveryRecord,
    InteractionStore,
    ProviderStatusEvidence,
    TransportInput,
    TransportRecord,
    TransportStore,
)
from noyra.interaction.transport import DeliveryReconciliationOutcome
from noyra.service import NoyraHTTPServer, ServiceSettings


class _FakePeerSocket:
    def __init__(self, address: str):
        self.address = address

    def getpeername(self) -> tuple[str, int]:
        return self.address, 25


class _FakeSMTP:
    instances: ClassVar[list[_FakeSMTP]] = []
    peer_override: ClassVar[str | None] = None
    send_error: ClassVar[BaseException | None] = None
    quit_error: ClassVar[BaseException | None] = None

    def __init__(self, *args: object, **kwargs: object):
        del args, kwargs
        self._host = ""
        self.sock: _FakePeerSocket | None = None
        self.connected_to: tuple[str, int] | None = None
        self.started_tls = False
        self.message_ids: list[str] = []
        self.closed = False
        type(self).instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.peer_override = None
        cls.send_error = None
        cls.quit_error = None

    def connect(self, host: str, port: int) -> tuple[int, bytes]:
        self.connected_to = (host, port)
        self.sock = _FakePeerSocket(self.peer_override or host)
        return 220, b"ready"

    def starttls(self, *, context: object) -> tuple[int, bytes]:
        del context
        self.started_tls = True
        return 220, b"tls"

    def login(self, username: str, password: str) -> tuple[int, bytes]:
        del username, password
        return 235, b"ok"

    def send_message(self, message: EmailMessage) -> dict[str, tuple[int, bytes]]:
        self.message_ids.append(str(message["Message-ID"]))
        if self.send_error is not None:
            raise self.send_error
        return {}

    def quit(self) -> tuple[int, bytes]:
        if self.quit_error is not None:
            raise self.quit_error
        self.closed = True
        return 221, b"bye"

    def close(self) -> None:
        self.closed = True


def _dns_answer(address: str) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
    parsed = ipaddress.ip_address(address)
    if parsed.version == 6:
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 0, 0, 0))]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]


async def _create_unknown_delivery(
    kernel: SubjectKernel,
    transports: TransportStore,
    *,
    settings: Mapping[str, str | int | bool] | None = None,
) -> DeliveryRecord:
    transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="webhook",
            label="unknown-provider",
            endpoint="https://delivery.example/send",
            settings={} if settings is None else dict(settings),
        ),
        actor="operator",
    )
    InteractionStore(kernel.database).send(
        kernel.subject_id,
        "webhook",
        "provider-target",
        "A bounded delivery reconciliation fixture.",
        idempotency_key="p2-05-unknown-delivery",
    )

    async def ambiguous(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response lost", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(ambiguous))
    dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
    try:
        result = await dispatcher.deliver_pending(kernel.subject_id)
    finally:
        await client.aclose()
    assert len(result) == 1
    assert result[0].status == "unknown"
    return result[0]


@pytest.mark.parametrize(
    ("endpoint", "address", "uses_starttls"),
    [
        ("smtp://mail.example:587", "8.8.8.8", True),
        ("smtps://[2606:4700:4700::1111]:465", "2606:4700:4700::1111", False),
    ],
)
def test_smtp_resolves_public_ipv4_ipv6_and_pins_tls_hostname(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    address: str,
    uses_starttls: bool,
) -> None:
    _FakeSMTP.reset()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _dns_answer(address))
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)

    message_id = DeliveryDispatcher._send_email(
        endpoint,
        {"from_address": "sender@example.com"},
        "recipient@example.com",
        "Pinned SMTP delivery.",
        "smtp-pinning",
    )

    assert len(_FakeSMTP.instances) == 1
    client = _FakeSMTP.instances[0]
    assert client.connected_to == (address, 587 if uses_starttls else 465)
    expected_hostname = "mail.example" if uses_starttls else "2606:4700:4700::1111"
    assert client._host == expected_hostname
    assert client.started_tls is uses_starttls
    assert client.message_ids == [message_id]


def test_smtp_rejects_dns_change_to_private_and_peer_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSMTP.reset()
    answers = iter((_dns_answer("8.8.8.8"), _dns_answer("127.0.0.1")))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    DeliveryDispatcher._send_email(
        "smtp://mail.example:587",
        {"from_address": "sender@example.com"},
        "recipient@example.com",
        "First public resolution.",
        "smtp-public-first",
    )
    with pytest.raises(OSError, match="non-public"):
        DeliveryDispatcher._send_email(
            "smtp://mail.example:587",
            {"from_address": "sender@example.com"},
            "recipient@example.com",
            "Rebound resolution.",
            "smtp-private-second",
        )
    assert len(_FakeSMTP.instances) == 1

    _FakeSMTP.peer_override = "127.0.0.1"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _dns_answer("8.8.8.8"))
    with pytest.raises(OSError, match="changed after DNS pinning"):
        DeliveryDispatcher._send_email(
            "smtp://mail.example:587",
            {"from_address": "sender@example.com"},
            "recipient@example.com",
            "Peer mismatch.",
            "smtp-peer-mismatch",
        )


def test_successful_smtp_send_ignores_quit_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSMTP.reset()
    _FakeSMTP.quit_error = smtplib.SMTPServerDisconnected("QUIT response lost")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _dns_answer("8.8.8.8"))
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    message_id = DeliveryDispatcher._send_email(
        "smtp://mail.example:587",
        {"from_address": "sender@example.com"},
        "recipient@example.com",
        "The DATA transaction completed before QUIT disconnected.",
        "smtp-quit-disconnect",
    )

    assert _FakeSMTP.instances[0].message_ids == [message_id]
    assert _FakeSMTP.instances[0].closed is True


@pytest.mark.asyncio
async def test_post_send_smtp_timeout_is_unknown_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSMTP.reset()
    _FakeSMTP.send_error = TimeoutError("reply lost after DATA")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _dns_answer("8.8.8.8"))
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-p2-04-smtp-timeout",
            content_hash({"seed": "p2-04-smtp-timeout"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="email",
                label="smtp-timeout",
                endpoint="smtp://mail.example:587",
                credentials={"from_address": SecretStr("sender@example.com")},
            ),
            actor="operator",
        )
        InteractionStore(kernel.database).send(
            kernel.subject_id,
            "email",
            "recipient@example.com",
            "Post-send timeout fixture.",
            idempotency_key="smtp-post-send-timeout",
        )
        dispatcher = DeliveryDispatcher(kernel.database, transports)
        first = await dispatcher.deliver_pending(kernel.subject_id)
        second = await dispatcher.deliver_pending(kernel.subject_id)

        assert first[0].status == "unknown"
        assert first[0].provider_message_id == DeliveryDispatcher._smtp_message_id(
            first[0].idempotency_key
        )
        assert first[0].next_retry_at is None
        assert second == []
        assert len(_FakeSMTP.instances) == 1
        assert len(_FakeSMTP.instances[0].message_ids) == 1


@pytest.mark.asyncio
async def test_http_write_timeout_is_unknown_and_never_retried() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-p2-04-http-write-timeout",
            content_hash({"seed": "p2-04-http-write-timeout"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="webhook",
                label="write-timeout",
                endpoint="https://delivery.example/send",
            ),
            actor="operator",
        )
        InteractionStore(kernel.database).send(
            kernel.subject_id,
            "webhook",
            "provider-target",
            "The request body may have reached the provider.",
        )

        async def ambiguous(request: httpx.Request) -> httpx.Response:
            raise httpx.WriteTimeout("request write outcome unknown", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(ambiguous))
        dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
        try:
            first = await dispatcher.deliver_pending(kernel.subject_id)
            second = await dispatcher.deliver_pending(kernel.subject_id)
        finally:
            await client.aclose()

        assert first[0].status == "unknown"
        assert first[0].next_retry_at is None
        assert second == []


@pytest.mark.asyncio
async def test_restart_recovers_sending_as_unknown_without_resend() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-p2-04-restart",
            content_hash({"seed": "p2-04-restart"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="webhook",
                label="restart",
                endpoint="https://delivery.example/send",
            ),
            actor="operator",
        )
        InteractionStore(kernel.database).send(
            kernel.subject_id,
            "webhook",
            "provider-target",
            "Interrupted delivery fixture.",
        )
        dispatcher = DeliveryDispatcher(kernel.database, transports)
        dispatcher.enqueue_pending(kernel.subject_id)
        with kernel.database.transaction() as connection:
            delivery_id = str(
                connection.execute("SELECT delivery_id FROM interaction_deliveries").fetchone()[0]
            )
            connection.execute(
                "UPDATE interaction_deliveries SET status = 'sending', attempts = 1 "
                "WHERE delivery_id = ?",
                (delivery_id,),
            )

        restarted = DeliveryDispatcher(kernel.database, transports)
        assert await restarted.deliver_pending(kernel.subject_id) == []
        recovered = restarted.get_delivery(delivery_id)
        assert recovered.status == "unknown"
        assert recovered.attempts == 1
        assert recovered.next_retry_at is None


@pytest.mark.parametrize("outcome", ["delivered", "failed", "unknown", "unsupported", "error"])
@pytest.mark.asyncio
async def test_provider_status_matrix_is_durable_across_restart(outcome: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            f"Noyra-p2-05-{outcome}",
            content_hash({"seed": f"p2-05-{outcome}"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        unknown = await _create_unknown_delivery(kernel, transports)

        async def lookup(
            delivery: DeliveryRecord,
            transport: TransportRecord,
            secret: Mapping[str, Any],
            settings: Mapping[str, Any],
        ) -> ProviderStatusEvidence:
            del delivery, transport, secret, settings
            return ProviderStatusEvidence(
                cast(DeliveryReconciliationOutcome, outcome),
                f"provider_{outcome}",
                "provider-message-42",
                {"lookup": outcome},
            )

        dispatcher = DeliveryDispatcher(kernel.database, transports, status_lookup=lookup)
        resolved = await dispatcher.lookup_unknown(
            unknown.delivery_id,
            actor="operator",
            reason="provider status matrix",
        )
        expected = outcome if outcome in {"delivered", "failed"} else "unknown"
        assert resolved.status == expected
        history = dispatcher.reconciliation_history(unknown.delivery_id)
        assert len(history) == 1
        assert history[0].outcome == outcome
        assert history[0].evidence == {"lookup": outcome}

        restarted = DeliveryDispatcher(kernel.database, transports)
        assert restarted.get_delivery(unknown.delivery_id).status == expected
        assert len(restarted.reconciliation_history(unknown.delivery_id)) == 1
        assert await restarted.deliver_pending(kernel.subject_id) == []


@pytest.mark.asyncio
async def test_generic_provider_status_endpoint_reconciles_with_hashed_evidence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-p2-05-status-endpoint",
            content_hash({"seed": "p2-05-status-endpoint"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        unknown = await _create_unknown_delivery(
            kernel,
            transports,
            settings={"status_endpoint": "https://status.example/messages"},
        )
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"status": "delivered", "message_id": "remote-42"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dispatcher = DeliveryDispatcher(
            kernel.database,
            transports,
            status_client=client,
        )
        try:
            resolved = await dispatcher.lookup_unknown(
                unknown.delivery_id,
                actor="operator",
                reason="query configured provider status endpoint",
            )
        finally:
            await client.aclose()
        assert resolved.status == "delivered"
        assert resolved.provider_message_id == "remote-42"
        assert requests[0].url.params["idempotency_key"] == unknown.idempotency_key
        history = dispatcher.reconciliation_history(unknown.delivery_id)
        assert history[0].evidence["http_status"] == 200
        assert len(str(history[0].evidence["response_hash"])) == 64


@pytest.mark.asyncio
async def test_generic_provider_not_found_is_not_treated_as_definitive_failure() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-p2-05-status-not-found",
            content_hash({"seed": "p2-05-status-not-found"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        unknown = await _create_unknown_delivery(
            kernel,
            transports,
            settings={"status_endpoint": "https://status.example/messages"},
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "not_found"}, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dispatcher = DeliveryDispatcher(kernel.database, transports, status_client=client)
        try:
            resolved = await dispatcher.lookup_unknown(
                unknown.delivery_id,
                actor="operator",
                reason="provider has no durable status record",
            )
        finally:
            await client.aclose()

        assert resolved.status == "unknown"
        assert dispatcher.reconciliation_history(unknown.delivery_id)[0].outcome == "unknown"


@pytest.mark.asyncio
async def test_manual_reconciliation_is_authorized_append_only_and_replay_safe() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-p2-05-manual",
            content_hash({"seed": "p2-05-manual"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        unknown = await _create_unknown_delivery(kernel, transports)
        dispatcher = DeliveryDispatcher(kernel.database, transports)

        with pytest.raises(PermissionError):
            dispatcher.reconcile_unknown(
                unknown.delivery_id,
                "delivered",
                actor=" Subject ",
                reason="forged subject reconciliation",
            )
        with pytest.raises(NotFoundError):
            dispatcher.reconcile_unknown(
                unknown.delivery_id,
                "delivered",
                subject_id="Noyra-other-subject",
                actor="operator",
                reason="cross-subject reconciliation attempt",
            )
        resolved = dispatcher.reconcile_unknown(
            unknown.delivery_id,
            "delivered",
            actor="operator",
            reason="operator confirmed provider receipt",
            provider_message_id="manual-provider-42",
            evidence={"ticket": "ops-42"},
        )
        assert resolved.status == "delivered"
        replayed = dispatcher.reconcile_unknown(
            unknown.delivery_id,
            "delivered",
            actor="operator",
            reason="idempotent replay",
            provider_message_id="manual-provider-42",
        )
        assert replayed.status == "delivered"
        assert len(dispatcher.reconciliation_history(unknown.delivery_id)) == 1
        with pytest.raises(InvalidTransitionError):
            dispatcher.reconcile_unknown(
                unknown.delivery_id,
                "failed",
                actor="operator",
                reason="conflicting replay",
            )
        with kernel.database.connection() as connection:
            raw_status = connection.execute(
                "SELECT status FROM interaction_deliveries WHERE delivery_id = ?",
                (unknown.delivery_id,),
            ).fetchone()[0]
            reconciliation_id = connection.execute(
                "SELECT reconciliation_id FROM interaction_delivery_reconciliations "
                "WHERE delivery_id = ?",
                (unknown.delivery_id,),
            ).fetchone()[0]
        assert raw_status == "unknown"
        with (
            pytest.raises(sqlite3.IntegrityError, match="append-only"),
            kernel.database.transaction() as connection,
        ):
            connection.execute(
                "UPDATE interaction_delivery_reconciliations SET reason = 'tampered' "
                "WHERE reconciliation_id = ?",
                (reconciliation_id,),
            )
        with (
            pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"),
            kernel.database.transaction() as connection,
        ):
            connection.execute(
                "DELETE FROM interaction_delivery_reconciliations WHERE reconciliation_id = ?",
                (reconciliation_id,),
            )


@pytest.mark.asyncio
async def test_reconciliation_is_exported_and_integrity_registry_detects_hash_tampering() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-p2-05-integrity",
            content_hash({"seed": "p2-05-integrity"}),
        )
        transports = TransportStore(kernel.database, root / "secrets" / "transports")
        unknown = await _create_unknown_delivery(kernel, transports)
        dispatcher = DeliveryDispatcher(kernel.database, transports)
        dispatcher.reconcile_unknown(
            unknown.delivery_id,
            "delivered",
            actor="operator",
            reason="integrity and export fixture",
            evidence={"ticket": "integrity-42"},
        )

        report = IntegrityRegistry().run(
            kernel.database,
            kernel.subject_id,
            root,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("interaction.transport",),
        )
        assert report.status == "ok"
        artifact = RuntimeLogExporter(kernel.database).export(kernel.subject_id, actor="test")
        with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
            rows = [
                json.loads(line)
                for line in archive.read(
                    "tables/interaction_delivery_reconciliations.jsonl"
                ).splitlines()
            ]
        assert len(rows) == 1
        assert rows[0]["delivery_id"] == unknown.delivery_id

        with kernel.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_delivery_reconciliation_update")
            connection.execute(
                "UPDATE interaction_delivery_reconciliations SET state_hash = ? "
                "WHERE delivery_id = ?",
                ("0" * 64, unknown.delivery_id),
            )
        damaged = IntegrityRegistry().run(
            kernel.database,
            kernel.subject_id,
            root,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("interaction.transport",),
        )
        assert damaged.status == "corrupt"
        assert damaged.p0 == ("interaction.transport:integrity_error",)


def test_delivery_reconciliation_api_requires_operator_and_exposes_history() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings = ServiceSettings(
            data_dir=root,
            subject_id="Noyra-p2-05-api",
            genesis_hash=content_hash({"seed": "p2-05-api"}),
            host="127.0.0.1",
            port=0,
            admin_token=SecretStr("p2-05-admin-token-with-sufficient-entropy"),
        )
        kernel = SubjectKernel(root / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
        kernel.boot()
        kernel.orient()
        kernel.activate()
        server = NoyraHTTPServer(kernel, settings)
        transport = server.transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="webhook",
                label="api-reconcile",
                endpoint="https://delivery.example/send",
            ),
            actor="operator",
        )
        interaction = InteractionStore(kernel.database).send(
            kernel.subject_id,
            "webhook",
            "provider-target",
            "API reconciliation fixture.",
        )
        server.deliveries.enqueue_pending(kernel.subject_id)
        with kernel.database.transaction() as connection:
            delivery_id = str(
                connection.execute(
                    "SELECT delivery_id FROM interaction_deliveries "
                    "WHERE interaction_id = ? AND transport_id = ?",
                    (interaction.interaction_id, transport.transport_id),
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE interaction_deliveries SET status = 'unknown', attempts = 1 "
                "WHERE delivery_id = ?",
                (delivery_id,),
            )

        async def api_lookup(
            delivery: DeliveryRecord,
            transport_record: TransportRecord,
            secret: Mapping[str, Any],
            transport_settings: Mapping[str, Any],
        ) -> ProviderStatusEvidence:
            del delivery, transport_record, secret, transport_settings
            return ProviderStatusEvidence(
                "unknown",
                "provider_pending",
                details={"lookup": "api"},
            )

        server.deliveries = DeliveryDispatcher(
            kernel.database,
            server.transports,
            status_lookup=api_lookup,
        )
        server.start()
        base_url = f"http://127.0.0.1:{server.address[1]}"
        payload = json.dumps(
            {"status": "delivered", "reason": "operator confirmed external receipt"}
        ).encode()
        unauthorized = Request(
            f"{base_url}/api/deliveries/{delivery_id}/reconcile",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with pytest.raises(HTTPError) as denied:
                urlopen(unauthorized, timeout=5)
            assert denied.value.code == 401

            authorized_headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer p2-05-admin-token-with-sufficient-entropy",
            }
            lookup_request = Request(
                f"{base_url}/api/deliveries/{delivery_id}/lookup",
                data=json.dumps({"reason": "query provider from operator API"}).encode(),
                method="POST",
                headers=authorized_headers,
            )
            with urlopen(lookup_request, timeout=5) as response:
                lookup_record = json.loads(response.read())
            assert lookup_record["status"] == "unknown"

            request = Request(
                f"{base_url}/api/deliveries/{delivery_id}/reconcile",
                data=payload,
                method="POST",
                headers=authorized_headers,
            )
            with urlopen(request, timeout=5) as response:
                record = json.loads(response.read())
            assert record["status"] == "delivered"

            history_request = Request(
                f"{base_url}/api/deliveries/{delivery_id}/reconciliations",
                headers={"Authorization": "Bearer p2-05-admin-token-with-sufficient-entropy"},
            )
            with urlopen(history_request, timeout=5) as response:
                history = json.loads(response.read())
            assert len(history) == 2
            assert [row["source"] for row in history] == ["provider", "operator"]

            with urlopen(request, timeout=5) as response:
                replay = json.loads(response.read())
            assert replay["status"] == "delivered"

            conflict_payload = json.dumps(
                {"status": "failed", "reason": "conflicting operator replay"}
            ).encode()
            conflict = Request(
                f"{base_url}/api/deliveries/{delivery_id}/reconcile",
                data=conflict_payload,
                method="POST",
                headers=authorized_headers,
            )
            with pytest.raises(HTTPError) as conflict_error:
                urlopen(conflict, timeout=5)
            assert conflict_error.value.code == 409
        finally:
            server.close()
            kernel.close()
