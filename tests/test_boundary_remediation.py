from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError

import httpx
import pytest

from noyra.capability import CapabilityGrant, CapabilityStore, ToolRunner
from noyra.core import Database, IdentityStore
from noyra.core.admission import (
    OperationInvalidated,
    RuntimeAdmissionGate,
    bind_lease,
)
from noyra.core.at_rest import AtRestConfig, AtRestGuard
from noyra.core.types import content_hash
from noyra.core.xml import parse_untrusted_xml
from noyra.interaction.inbound import WeChatInboundAdapter
from noyra.migration.executor import MigrationExecutionError
from noyra.migration.http_executor import UrllibHTTPTransport
from noyra.migration.transfer import EncryptedTransferSession, TransferSession
from noyra.research.browser import BrowserSearchExecutor
from noyra.world import SafeWebReader, SourceRegistry
from test_wallet_rpc import _graph, _store


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "audit.sqlite3")
    IdentityStore(db).ensure("audit", content_hash("audit"))
    return db


@pytest.mark.parametrize("when", ["before", "after"])
def test_file_publication_is_fenced_but_terminal_accounting_settles(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    import noyra.capability.tools as module

    target = tmp_path / "note"
    target.write_text("old")
    CapabilityStore(database).grant(
        "audit",
        CapabilityGrant(
            capability_type="filesystem_write",
            scope={"root": str(tmp_path)},
            issuer="owner",
            rate_limit_per_hour=10,
            side_effect=True,
        ),
        actor="operator",
    )
    gate = RuntimeAdmissionGate("audit")
    from noyra.capability.filesystem import write_atomic as original

    def race(*args: Any, **kwargs: Any) -> None:
        if when == "before":
            gate.invalidate()
        original(*args, **kwargs)
        if when == "after":
            gate.invalidate()

    monkeypatch.setattr(module, "write_atomic", race)
    with gate.operation("test") as lease, bind_lease(lease):
        result = ToolRunner(database).write_text("audit", target, "new")
    assert result.status == ("failed" if when == "before" else "succeeded")
    assert target.read_text() == ("old" if when == "before" else "new")
    assert not list(tmp_path.glob(".note.*"))
    with database.connection() as connection:
        assert connection.execute("SELECT status FROM actions").fetchone()[0] == result.status


@pytest.mark.parametrize("change", ["pause", "revoke"])
def test_web_last_dispatch_rechecks_and_records_failure(
    database: Database,
    change: str,
) -> None:
    store = CapabilityStore(database)
    grant = store.grant(
        "audit",
        CapabilityGrant(
            capability_type="web_read",
            scope={"hosts": ["example.com"]},
            issuer="owner",
            rate_limit_per_hour=10,
            side_effect=False,
        ),
        actor="operator",
    )
    source = SourceRegistry(database).register(
        "audit",
        "Example",
        "https://example.com/",
        "news",
        status="active",
    )
    gate = RuntimeAdmissionGate("audit")
    sent: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if change == "pause":
            gate.invalidate()
        else:
            store.revoke(grant.grant_id, subject_id="audit", actor="operator", reason="withdrawn")
        await request.extensions["trace"]("http11.send_request_headers.started", {})
        sent.append(True)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="result")

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reader = SafeWebReader(client=client, resolver=resolver, verify_peer_address=False)
            with gate.operation("test") as lease, bind_lease(lease):
                if change == "pause":
                    # Accounting finishes the action, but stale work may not
                    # commit poll/scheduling state after epoch invalidation.
                    with pytest.raises(OperationInvalidated):
                        await ToolRunner(database).fetch_document("audit", source, reader)
                else:
                    result = await ToolRunner(database).fetch_document("audit", source, reader)
                    assert result.status == "failed" and result.document is None

    asyncio.run(run())
    assert not sent
    with database.connection() as connection:
        assert connection.execute("SELECT status FROM actions").fetchone()[0] == "failed"
        # Pause must finish accounting without mutating the now-stale epoch.
        # An ordinary revoked grant may still record its failed poll cooldown.
        polls = connection.execute("SELECT outcome FROM world_poll_state").fetchall()
        assert [row[0] for row in polls] == ([] if change == "pause" else ["failed"])


@pytest.mark.parametrize("change", ["pause", "revoke"])
def test_balance_does_not_send_second_rpc_after_revocation(tmp_path: Path, change: str) -> None:
    from noyra.wallet import WalletRPCBalanceAcquirer, WalletRPCError

    _db, subject, store = _store(tmp_path)
    _network, asset, address = _graph(store, subject)
    gate = RuntimeAdmissionGate(subject)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        methods.append(method)
        if change == "pause":
            gate.invalidate()
        else:
            store.revoke_asset(asset, subject_id=subject, actor="operator", reason="withdrawn")
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": "noyra-wallet-balance-v1", "result": "0x1"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        acquirer = WalletRPCBalanceAcquirer(store, client=client)
        with (
            gate.operation("test") as lease,
            bind_lease(lease),
            pytest.raises((OperationInvalidated, WalletRPCError)),
        ):
            acquirer.read_balance(subject, asset_id=asset, address_id=address, actor="operator")
    assert methods == ["eth_chainId"]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-be", "utf-32"])
def test_xml_rejects_comment_disguised_dtd_in_all_encodings(encoding: str) -> None:
    raw = (
        '<!-- <fake> --><!DOCTYPE xml [<!ENTITY audit "expanded">]>'
        "<xml><Content>&audit;</Content></xml>"
    ).encode(encoding)
    with pytest.raises((ValueError, ParseError)):
        parse_untrusted_xml(raw)


def test_wechat_and_rss_reject_report_payloads_but_keep_cdata() -> None:
    raw = (
        b'<!-- <fake> --><!DOCTYPE xml [<!ENTITY audit "expanded">]>'
        b"<xml><Content>&audit;</Content></xml>"
    )
    with pytest.raises(ValueError):
        WeChatInboundAdapter._parse_xml(raw)
    xml = (
        '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE rss [<!ENTITY a "x">]><rss>&a;</rss>'
    ).encode("utf-16")
    with pytest.raises(ValueError):
        BrowserSearchExecutor._parse(httpx.Response(200, content=xml), 2)
    assert (
        WeChatInboundAdapter._parse_xml(b"<xml><Content><![CDATA[<hi>]]></Content></xml>").findtext(
            "Content"
        )
        == "<hi>"
    )


@pytest.mark.parametrize(
    "raw,kwargs",
    [
        (b"<x><x/><x/></x>", {"max_nodes": 2}),
        (b"<x><x><x/></x></x>", {"max_depth": 1}),
        (b"<x>abcd</x>", {"max_text_chars": 3}),
    ],
)
def test_xml_budgets_apply_during_construction(raw: bytes, kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="limit"):
        parse_untrusted_xml(raw, **kwargs)


@pytest.mark.parametrize("size", [0, 1, 4096, 4097, 8192, 8193])
def test_legacy_resume_handles_final_short_or_full_chunk(tmp_path: Path, size: int) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"x" * size)
    session = TransferSession(chunk_bytes=4096)
    receipt = session.send(
        source, tmp_path / "target", stop_after_chunks=max(1, (size + 4095) // 4096)
    )
    if not receipt.complete:
        receipt = session.resume(receipt)
    session.verify(receipt)
    assert Path(receipt.destination).read_bytes() == source.read_bytes()


@pytest.mark.parametrize("damage", ["cipher", "hash", "size", "header", "trailing"])
@pytest.mark.parametrize("existing", [False, True])
def test_encrypted_receive_is_failure_atomic(tmp_path: Path, damage: str, existing: bool) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"x" * 4097)
    target = tmp_path / "target"
    if existing:
        target.write_bytes(b"preserved")
    encrypted = tmp_path / "encrypted"
    session = EncryptedTransferSession(b"k" * 32, chunk_bytes=4096)
    receipt = session.send(source, encrypted, manifest_digest="a" * 64)
    if damage == "cipher":
        content = bytearray(encrypted.read_bytes())
        content[-1] ^= 1
        encrypted.write_bytes(content)
    elif damage == "header":
        encrypted.write_bytes(b"bad")
    elif damage == "trailing":
        with encrypted.open("ab") as stream:
            stream.write(b"bad")
    elif damage == "hash":
        receipt = replace(receipt, artifact_hash="0" * 64)
    else:
        receipt = replace(receipt, byte_size=1)
    with pytest.raises(ValueError):
        session.receive(receipt, target, manifest_digest="a" * 64)
    assert target.read_bytes() == b"preserved" if existing else not target.exists()
    assert not list(tmp_path.glob(".noyra-receive-*"))


def test_migration_request_has_total_deadline_and_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import noyra.migration.http_executor as module

    done = threading.Event()

    class Drip(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "7")
            self.end_headers()
            try:
                for byte in b'{"x":1}':
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    if done.wait(0.3):
                        break
            except OSError:
                pass

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        module,
        "_PinnedHTTPSConnection",
        lambda *a, **kw: http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=kw["timeout"]
        ),
    )
    transport = UrllibHTTPTransport(timeout_seconds=1, allowed_private_networks=("127.0.0.0/8",))
    started = time.monotonic()
    try:
        with pytest.raises(MigrationExecutionError):
            transport.request("https://127.0.0.1/", {}, "t" * 32)
        assert time.monotonic() - started < 1.8
    finally:
        done.set()
        transport.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_migration_cancelled_dns_cannot_send_late(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = UrllibHTTPTransport(timeout_seconds=1)
    entered = threading.Event()
    release = threading.Event()
    late = threading.Event()

    def resolve(url: str) -> tuple[str, tuple[str, ...]]:
        entered.set()
        release.wait(5)
        return url, ("93.184.216.34",)

    monkeypatch.setattr(transport, "_validate_connection_endpoint", resolve)
    import noyra.migration.http_executor as module

    monkeypatch.setattr(module, "_PinnedHTTPSConnection", lambda *a, **kw: late.set())
    try:
        with pytest.raises(MigrationExecutionError):
            transport.request("https://example.com/", {}, "t" * 32)
        assert entered.is_set()
    finally:
        transport.close()
        release.set()
    assert not late.wait(0.1)


def test_http_permission_requests_are_singleflight_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import noyra.core.at_rest as module

    root = tmp_path / "data"
    root.mkdir()
    guard = AtRestGuard(AtRestConfig(data_root=root, mode="required"))
    guard._prepared = True
    key = type("Key", (), {"metadata": lambda self: {}})()
    monkeypatch.setattr(guard, "_require_volume", lambda: None)
    monkeypatch.setattr(guard, "_require_keyring", lambda: key)
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def audit(path: Path, **kwargs: Any) -> str | None:
        calls.append(1)
        entered.set()
        release.wait(5)
        return None

    monkeypatch.setattr(module, "_private_permission_error", audit)
    try:
        assert not guard.request_health()["ready"]
        assert entered.wait(2)
        for _ in range(30):
            assert not guard.request_health()["ready"]
        assert len(calls) == 1
        release.set()
        deadline = time.monotonic() + 3
        while not guard.request_health()["ready"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert guard.request_health()["ready"]
        with guard._permission_lock:
            snapshot = guard._permission_snapshot
            assert snapshot is not None
            guard._permission_snapshot = (snapshot[0], time.monotonic() - 6, None)
        assert not guard.request_health()["ready"]
    finally:
        release.set()
        guard.close()
    assert not guard.request_health()["ready"]


def test_migration_agent_bounds_workers_and_unauthenticated_header_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_migration_agent_cli import _module

    module = _module()
    monkeypatch.setattr(module, "MAX_AGENT_WORKERS", 2)
    monkeypatch.setattr(module, "AGENT_REQUEST_SECONDS", 0.3)
    accepted: list[float | None] = []
    entered = threading.Event()

    original_setup = module.Handler.setup

    def setup(self: Any) -> None:
        original_setup(self)
        accepted.append(self.connection.gettimeout())
        if len(accepted) == 2:
            entered.set()

    monkeypatch.setattr(module.Handler, "setup", setup)
    server = module.MigrationHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sockets: list[socket.socket] = []
    try:
        for _ in range(2):
            connection = socket.create_connection(server.server_address, timeout=2)
            connection.sendall(b"POST /v1/health HTTP/1.1\r\nX-Test: ")
            sockets.append(connection)
        assert entered.wait(2)
        assert accepted == [0.3, 0.3]
        extra = socket.create_connection(server.server_address, timeout=2)
        sockets.append(extra)
        assert extra.recv(1) == b""
        assert len(accepted) == 2
        for connection in sockets[:2]:
            assert connection.recv(1) == b""
        # Slots must be released after header timeout, not leaked forever.
        deadline = time.monotonic() + 2
        while not server._worker_slots.acquire(blocking=False):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        server._worker_slots.release()
    finally:
        for connection in sockets:
            connection.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_file_publish_serializes_pause_until_after_replace(
    tmp_path: Path,
) -> None:
    from noyra.capability.filesystem import write_atomic

    gate = RuntimeAdmissionGate("audit")
    target = tmp_path / "target"
    target.write_text("old")
    ready = threading.Event()
    pausing = threading.Event()
    paused = threading.Event()

    def pause() -> None:
        assert ready.wait(2)
        pausing.set()
        gate.invalidate()
        paused.set()

    thread = threading.Thread(target=pause)
    thread.start()

    def before_publish() -> None:
        ready.set()
        assert pausing.wait(2)
        assert not paused.is_set()
        assert target.read_text() == "old"

    try:
        with gate.operation("test") as lease, bind_lease(lease):
            write_atomic(target, tmp_path, b"new", before_publish=before_publish)
        assert paused.wait(2)
        assert target.read_text() == "new"
    finally:
        ready.set()
        thread.join(timeout=3)


def test_http_permission_snapshot_is_not_invalidated_by_normal_wal_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = AtRestGuard(AtRestConfig(data_root=tmp_path, mode="required"))
    before = guard._permission_root_signature()
    (tmp_path / "noyra.sqlite3-wal").write_bytes(b"test")
    assert guard._permission_root_signature() == before


@pytest.mark.parametrize("protocol", ["HTTP/1.0", "HTTP/1.1"])
@pytest.mark.parametrize("tls", [False, True])
def test_migration_transport_close_interrupts_body_and_releases_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    tls: bool,
) -> None:
    import noyra.migration.http_executor as module

    entered = threading.Event()
    release = threading.Event()
    worker_done = threading.Event()
    errors: list[Exception] = []

    class Stalled(BaseHTTPRequestHandler):
        protocol_version = protocol

        def do_POST(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.flush()
            entered.set()
            release.wait(10)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Stalled)
    client_options: dict[str, Any] = {}
    connection_class: type[http.client.HTTPConnection] | type[http.client.HTTPSConnection] = (
        http.client.HTTPConnection
    )
    if tls:
        import ssl
        from datetime import UTC, datetime, timedelta

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        cert_path, key_path = tmp_path / "cert.pem", tmp_path / "test-key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        client_context = ssl.create_default_context(cafile=str(cert_path))
        client_context.check_hostname = False  # local fixture only; certificate remains verified
        client_options["context"] = client_context
        connection_class = http.client.HTTPSConnection
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    monkeypatch.setattr(
        module,
        "_PinnedHTTPSConnection",
        lambda *a, **kw: connection_class(
            "127.0.0.1", server.server_port, timeout=kw["timeout"], **client_options
        ),
    )
    transport = UrllibHTTPTransport(timeout_seconds=10, allowed_private_networks=("127.0.0.0/8",))
    original = transport._request_bounded

    def bounded(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return original(*args, **kwargs)
        finally:
            worker_done.set()

    monkeypatch.setattr(transport, "_request_bounded", bounded)

    def request() -> None:
        try:
            transport.request("https://127.0.0.1/", {}, "t" * 32)
        except Exception as error:
            errors.append(error)

    caller = threading.Thread(target=request, daemon=True)
    caller.start()
    try:
        assert entered.wait(2)
        transport.close()
        assert worker_done.wait(2)
        caller.join(2)
        assert not caller.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], MigrationExecutionError)
        with pytest.raises(MigrationExecutionError, match="target_transport_closed"):
            transport.request("https://127.0.0.1/", {}, "t" * 32)
    finally:
        release.set()
        transport.close()
        server.shutdown()
        server.server_close()
        serving.join()
        caller.join(2)


def test_migration_transport_real_http_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import noyra.migration.http_executor as module

    bodies: list[bytes] = []

    class Success(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers["Authorization"].startswith("Noyra-HMAC ")
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Success)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        module,
        "_PinnedHTTPSConnection",
        lambda *a, **kw: http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=kw["timeout"]
        ),
    )
    transport = UrllibHTTPTransport(timeout_seconds=2, allowed_private_networks=("127.0.0.0/8",))
    try:
        assert transport.request("https://127.0.0.1/", {"task": "test"}, "t" * 32) == {"ok": True}
        assert bodies == [b'{"task":"test"}']
    finally:
        transport.close()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("loss", ["volume", "keyring", "root", "scan"])
def test_http_permission_snapshot_does_not_override_live_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loss: str,
) -> None:
    from noyra.core.at_rest import AtRestError

    guard = AtRestGuard(AtRestConfig(data_root=tmp_path, mode="required"))
    guard._prepared = True
    key = type("Key", (), {"metadata": lambda self: {}})()
    monkeypatch.setattr(guard, "_require_volume", lambda: None)
    monkeypatch.setattr(guard, "_require_keyring", lambda: key)
    monkeypatch.setattr(guard, "_monitor_permissions", lambda: None)
    signature = guard._permission_root_signature()
    guard._permission_snapshot = (signature, time.monotonic(), None)
    try:
        assert guard.request_health()["ready"]

        def unavailable() -> None:
            raise AtRestError("live boundary lost")

        if loss in {"volume", "keyring"}:
            monkeypatch.setattr(guard, "_require_" + loss, unavailable)
        elif loss == "root":
            monkeypatch.setattr(guard, "_permission_root_signature", lambda: (*signature, 1))
        else:
            guard._permission_snapshot = (signature, time.monotonic(), "unsafe nested permissions")
        assert not guard.request_health()["ready"]
        with pytest.raises(AtRestError):
            guard.require_request_ready()
    finally:
        guard.close()


def test_xml_element_depth_and_attribute_text_budgets() -> None:
    assert parse_untrusted_xml(b"<x/>", max_depth=1).tag == "x"
    with pytest.raises(ValueError, match="structure"):
        parse_untrusted_xml(b"<x><y/></x>", max_depth=1)
    assert parse_untrusted_xml(b'<x a="123"/>', max_text_chars=3).attrib["a"] == "123"
    with pytest.raises(ValueError, match="text"):
        parse_untrusted_xml(b'<x a="123">4</x>', max_text_chars=3)


def test_strict_json_nesting_failure_is_value_error() -> None:
    from noyra.core.types import strict_json_loads

    with pytest.raises(ValueError, match="nesting"):
        strict_json_loads("[" * 10_000 + "0" + "]" * 10_000)


def test_request_reader_separates_header_and_body_budgets() -> None:
    import io
    from unittest.mock import Mock

    from noyra.core.request_io import DeadlineRequestReader

    header = b"POST / HTTP/1.0\r\nContent-Length: 100\r\n\r\n"
    body = b"x" * 100
    reader = DeadlineRequestReader(
        io.BytesIO(header + body),
        Mock(),
        5,
        max_bytes=len(header) + len(body),
        max_header_bytes=len(header),
    )
    assert io.BufferedReader(reader).read() == header + body
    reader = DeadlineRequestReader(
        io.BytesIO(header + body),
        Mock(),
        5,
        max_bytes=len(header) + len(body),
        max_header_bytes=len(header) - 1,
    )
    with pytest.raises(OSError, match="header byte"):
        io.BufferedReader(reader).read()


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 8192])
@pytest.mark.parametrize("request_line", [False, True])
def test_request_head_budget_has_typed_phase_and_bounded_reads(
    chunk_size: int,
    request_line: bool,
) -> None:
    import io
    from unittest.mock import Mock

    from noyra.core.request_io import DeadlineRequestReader, RequestHeadLimitError

    budget = 64
    raw = (
        b"GET /" + b"x" * 1000 + b" HTTP/1.1\r\n\r\n"
        if request_line
        else b"GET / HTTP/1.1\r\nX-Private: " + b"x" * 1000 + b"\r\n\r\n"
    )

    class ChunkedInput(io.BytesIO):
        def read1(self, size: int | None = -1) -> bytes:
            maximum = chunk_size if size is None or size < 0 else min(size, chunk_size)
            return super().read1(maximum)

    source = ChunkedInput(raw)
    reader = DeadlineRequestReader(source, Mock(), 5, max_bytes=2000, max_header_bytes=budget)
    with io.BufferedReader(reader) as buffered:
        with pytest.raises(RequestHeadLimitError) as caught:
            buffered.read()
        assert caught.value.request_line is request_line
        assert str(caught.value) == "request header byte limit exceeded"
        assert source.tell() == budget + 1


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 8192])
@pytest.mark.parametrize("line_ending", [b"\r\n", b"\n", b"\n\r\n"])
def test_exact_head_budget_does_not_consume_body_allowance(
    chunk_size: int,
    line_ending: bytes,
) -> None:
    import io
    from unittest.mock import Mock

    from noyra.core.request_io import DeadlineRequestReader

    first = b"POST / HTTP/1.0" + (b"\n" if line_ending == b"\n\r\n" else line_ending)
    header = first + b"Content-Length: 100" + line_ending
    if line_ending != b"\n\r\n":
        header += line_ending
    body = b"b" * 100

    class ChunkedInput(io.BytesIO):
        def read1(self, size: int | None = -1) -> bytes:
            maximum = chunk_size if size is None or size < 0 else min(size, chunk_size)
            return super().read1(maximum)

    reader = DeadlineRequestReader(
        ChunkedInput(header + body),
        Mock(),
        5,
        max_bytes=len(header) + len(body),
        max_header_bytes=len(header),
    )
    assert io.BufferedReader(reader).read() == header + body


def test_total_request_budget_is_not_disabled_after_head() -> None:
    import io
    from unittest.mock import Mock

    from noyra.core.request_io import DeadlineRequestReader, RequestHeadLimitError

    head = b"POST / HTTP/1.0\r\nContent-Length: 100\r\n\r\n"
    source = io.BytesIO(head + b"b" * 100)
    reader = DeadlineRequestReader(
        source, Mock(), 5, max_bytes=len(head) + 10, max_header_bytes=len(head)
    )
    with pytest.raises(OSError, match="request byte limit") as caught:
        io.BufferedReader(reader).read()
    assert not isinstance(caught.value, RequestHeadLimitError)
