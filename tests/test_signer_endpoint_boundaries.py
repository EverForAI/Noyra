"""Verify public HTTPS signer and loopback HTTP origin roles without public traffic."""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from eth_account import Account

from noyra.signer_service import SignerEngine, SignerHTTPServer, SignerState
from noyra.wallet.config import configured_wallet_signer_from_env
from noyra.wallet.execution import HTTPSWalletSigner, WalletExecutionError
from test_signer_service import ADDRESS, CHAIN_ID, TOKEN, FakeRPC, native_transfer


@pytest.fixture(autouse=True)
def isolated_wallet_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for suffix in (
        "MODE",
        "SIGNER_ENDPOINT",
        "SIGNER_ID",
        "SIGNER_ADDRESS",
        "SIGNER_BEARER_TOKEN",
        "SIGNER_BEARER_TOKEN_FILE",
        "SIGNER_BEARER_TOKEN_CREDENTIAL",
        "SIGNER_TIMEOUT_SECONDS",
        "KEYSTORE_PATH",
        "LOCAL_KEYSTORE_PATH",
        "PASSWORD_FILE",
        "LOCAL_PASSWORD_FILE",
        "RPC_URLS_JSON",
        "LOCAL_RPC_URLS_JSON",
    ):
        monkeypatch.delenv("NOYRA_WALLET_" + suffix, raising=False)


@dataclass
class Frontend:
    origin: SignerHTTPServer
    url: str
    trust: ssl.SSLContext
    rpc: FakeRPC
    paths: list[str] = field(default_factory=list)
    redirect: bool = False

    def client(self, *, trust_certificate: bool = True) -> httpx.Client:
        # This explicit test client connects the public-shaped hostname to a
        # local TLS fixture. Production keeps PublicDNSHTTPTransport, checked
        # separately below; no production resolver or validation is relaxed.
        return httpx.Client(
            transport=httpx.HTTPTransport(verify=self.trust if trust_certificate else True),
            trust_env=False,
            follow_redirects=True,
            timeout=5,
        )


@pytest.fixture
def frontend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Frontend]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "signer.example")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("signer.example")]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    certificate, private_key = tmp_path / "synthetic-ca.pem", tmp_path / "synthetic-tls.key"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, private_key)
    trust = ssl.create_default_context(cafile=str(certificate))
    rpc = FakeRPC()
    state = SignerState.open(tmp_path / "synthetic-signer.sqlite3")
    engine = SignerEngine(
        account=Account.from_key("0x" + "11" * 32),
        signer_id="staging-signer-01",
        chain_id=CHAIN_ID,
        rpc=rpc,
        state=state,
    )
    origin = SignerHTTPServer(engine, bearer_token=TOKEN, port=0)
    fixture = Frontend(origin, "", trust, rpc)

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            fixture.paths.append(self.path)
            size = int(self.headers.get("Content-Length", "0"))
            assert 0 < size <= 16384
            # Consume the request before closing TLS after an early redirect.
            # Otherwise unread body bytes can reset the connection on Windows.
            body = self.rfile.read(size)
            assert len(body) == size
            if fixture.redirect:
                self.send_response(302)
                self.send_header("Location", fixture.origin.url + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            connection = http.client.HTTPConnection("127.0.0.1", origin.server_port, timeout=5)
            try:
                headers = {"Content-Type": "application/json"}
                if self.headers.get("Authorization"):
                    headers["Authorization"] = str(self.headers["Authorization"])
                connection.request("POST", self.path, body=body, headers=headers)
                response = connection.getresponse()
                body = response.read(65536)
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                connection.close()

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    proxy.socket = server_context.wrap_socket(proxy.socket, server_side=True)
    fixture.url = f"https://signer.example:{proxy.server_port}"
    original = socket.getaddrinfo

    def test_dns(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        return original(
            "127.0.0.1" if host in {"signer.example", b"signer.example"} else host,
            port,
            *args,
            **kwargs,
        )

    monkeypatch.setattr(socket, "getaddrinfo", test_dns)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True) for server in (origin, proxy)
    ]
    for thread in threads:
        thread.start()
    try:
        yield fixture
    finally:
        for server in (proxy, origin):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
        state.close()


def proof_context() -> dict[str, str]:
    return {
        "task_id": "synthetic-task",
        "target_id": "synthetic-target",
        "manifest_digest": "a" * 64,
        "target_identity": "synthetic-identity",
    }


def test_https_frontend_can_use_the_loopback_origin_protocol_without_reconfiguring_either(
    frontend: Frontend,
) -> None:
    with (
        frontend.client() as client,
        HTTPSWalletSigner(
            frontend.url,
            signer_id="staging-signer-01",
            wallet_address=ADDRESS,
            bearer_token=TOKEN,
            client=client,
        ) as signer,
    ):
        assert len(signer.prove_migration_identity(proof_context())) == 64
        assert signer.get_pending_nonce(ADDRESS, chain_id=CHAIN_ID) == 0
        assert signer.get_fee_quote(native_transfer()) == (25200, "2000000000")
        first = signer.sign_and_broadcast(native_transfer(), request_id="synthetic-once")
        assert signer.sign_and_broadcast(native_transfer(), request_id="synthetic-once") == first
        assert signer.get_receipt(first.tx_hash, chain_id=CHAIN_ID) is None
    assert frontend.origin.server_address[0] == "127.0.0.1"
    assert frontend.origin.url.startswith("http://127.0.0.1:")
    assert len(frontend.rpc.sent_raw_transactions) == 1
    assert frontend.paths == [
        "/migration-identity",
        "/v1/nonce",
        "/v1/fee-quote",
        "/v1/transfer",
        "/v1/transfer",
        "/v1/receipt",
    ]


@pytest.mark.parametrize("token", [None, "synthetic-wrong-token"])
def test_tls_proxy_keeps_origin_bearer_authentication(
    frontend: Frontend, token: str | None
) -> None:
    with (
        frontend.client() as client,
        HTTPSWalletSigner(
            frontend.url,
            signer_id="staging-signer-01",
            wallet_address=ADDRESS,
            bearer_token=token,
            client=client,
        ) as signer,
        pytest.raises(ValueError, match="possession proof invalid"),
    ):
        signer.prove_migration_identity(proof_context())
    assert frontend.rpc.sent_raw_transactions == []
    assert (
        frontend.origin.engine.state.connection.execute(
            "SELECT COUNT(*) FROM signer_requests"
        ).fetchone()[0]
        == 0
    )


def test_frontend_requires_certificate_trust(frontend: Frontend) -> None:
    with (
        frontend.client(trust_certificate=False) as client,
        HTTPSWalletSigner(
            frontend.url,
            signer_id="staging-signer-01",
            wallet_address=ADDRESS,
            bearer_token=TOKEN,
            client=client,
        ) as signer,
        pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"),
    ):
        signer.prove_migration_identity(proof_context())
    assert frontend.paths == []


def test_redirect_is_not_followed_even_with_a_redirect_enabled_injected_client(
    frontend: Frontend,
) -> None:
    frontend.redirect = True
    statuses: list[int] = []
    with (
        frontend.client() as client,
        HTTPSWalletSigner(
            frontend.url,
            signer_id="staging-signer-01",
            wallet_address=ADDRESS,
            bearer_token=TOKEN,
            client=client,
        ) as signer,
        pytest.raises(ValueError, match="possession proof invalid"),
    ):
        client.event_hooks["response"].append(
            lambda response: statuses.append(response.status_code)
        )
        signer.prove_migration_identity(proof_context())
    assert statuses == [302]
    assert frontend.paths == ["/migration-identity"]
    assert frontend.rpc.sent_raw_transactions == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8780",
        "https://127.0.0.1:8780",
        "https://[::1]:8780",
        "https://10.0.0.1",
        "http://signer.example",
        "https://user:synthetic-secret@signer.example",
        "https://signer.example?token=synthetic",
        "https://signer.example#fragment",
    ],
)
def test_external_mode_rejects_origin_or_unsafe_urls_without_connecting(endpoint: str) -> None:
    handler = Mock(side_effect=AssertionError("no request is allowed"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(ValueError):
        HTTPSWalletSigner(endpoint, signer_id="synthetic", client=client)
    handler.assert_not_called()


def test_production_transport_still_rejects_a_hostname_resolving_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ],
    )
    connect = Mock(side_effect=AssertionError("must reject before TCP connect"))
    monkeypatch.setattr(socket, "create_connection", connect)
    with (
        HTTPSWalletSigner(
            "https://signer.example", signer_id="synthetic", wallet_address=ADDRESS
        ) as signer,
        pytest.raises(WalletExecutionError, match="nonce lookup failed") as error,
    ):
        signer.get_pending_nonce(ADDRESS, chain_id=CHAIN_ID)
    assert isinstance(error.value.__cause__, httpx.ConnectError)
    connect.assert_not_called()


def test_external_environment_rejects_local_origin_without_disclosing_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOYRA_WALLET_MODE", "external")
    endpoint = "http://127.0.0.1:8780/private-synthetic-value"
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ENDPOINT", endpoint)
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ID", "staging-signer-01")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_BEARER_TOKEN", "synthetic-env-token")
    with pytest.raises(
        ValueError, match="external wallet signer configuration is invalid"
    ) as error:
        configured_wallet_signer_from_env()
    assert endpoint not in str(error.value) and "synthetic-env-token" not in str(error.value)


def test_environment_selects_https_frontend_and_never_uses_the_origin_url(
    frontend: Frontend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import noyra.wallet.config as config

    monkeypatch.setenv("NOYRA_WALLET_MODE", "external")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ENDPOINT", frontend.url)
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ID", "staging-signer-01")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ADDRESS", ADDRESS)
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_BEARER_TOKEN", TOKEN)
    with frontend.client() as client:

        def factory(endpoint: str, **kwargs: Any) -> HTTPSWalletSigner:
            assert endpoint == frontend.url
            return HTTPSWalletSigner(endpoint, client=client, **kwargs)

        monkeypatch.setattr(config, "HTTPSWalletSigner", factory)
        signer = config.configured_wallet_signer_from_env()
        assert isinstance(signer, HTTPSWalletSigner)
        assert signer.endpoint == frontend.url and signer.endpoint != frontend.origin.url
        assert len(signer.prove_migration_identity(proof_context())) == 64
    assert frontend.paths == ["/migration-identity"]
    assert frontend.rpc.sent_raw_transactions == []
