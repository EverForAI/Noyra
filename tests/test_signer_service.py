from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from noyra.core.types import canonical_json
from noyra.signer_service import (
    SignerEngine,
    SignerError,
    SignerHTTPServer,
    SignerState,
    parse_transfer,
)
from noyra.wallet.execution import WalletUnsignedTransfer

TOKEN = "signer-test-token"
ADDRESS = "0x19e7e376e7c213b7e7e7e46cc70a5dd086daff2a"
CHAIN_ID = 11155111


def identity_challenge() -> str:
    return canonical_json(
        {
            "domain": "noyra-migration-wallet/v1",
            "task_id": "task-test",
            "target_id": "target-test",
            "target_identity": "identity-test",
            "manifest_digest": "a" * 64,
            "signer_id": "staging-signer-01",
            "nonce": "b" * 64,
        }
    )


def valid_transfer_payload(*, nonce: int = 0) -> dict[str, Any]:
    return {
        "chain_id": CHAIN_ID,
        "nonce": nonce,
        "gas_limit": 21_000,
        "max_fee_per_gas": "2000000000",
        "source_address": ADDRESS,
        "to_address": "0x2222222222222222222222222222222222222222",
        "value": "1000",
        "data": "0x",
        "asset_type": "native",
        "contract_address": None,
    }


def native_transfer(*, nonce: int = 0) -> WalletUnsignedTransfer:
    return parse_transfer(valid_transfer_payload(nonce=nonce))


class FakeRPC:
    def __init__(self) -> None:
        self.sent_raw_transactions: list[str] = []
        self.raise_after_send: BaseException | None = None
        self.receipts: dict[str, dict[str, Any]] = {}
        self.transactions: dict[str, dict[str, Any]] = {}
        self.blocks: dict[str, dict[str, Any]] = {}
        self.latest_block = 1

    def call(self, method: str, params: list[object]) -> object:
        if method == "eth_chainId":
            return "0xaa36a7"
        if method == "eth_getTransactionCount":
            return "0x0"
        if method == "eth_getBalance":
            return hex(10**18)
        if method == "eth_gasPrice":
            return "0x3b9aca00"
        if method == "eth_estimateGas":
            return "0x5208"
        if method == "eth_sendRawTransaction":
            raw = str(params[0])
            from eth_hash.auto import keccak

            tx_hash = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
            self.sent_raw_transactions.append(tx_hash)
            if self.raise_after_send is not None:
                raise self.raise_after_send
            return tx_hash
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(str(params[0]).lower())
        if method == "eth_getTransactionByHash":
            return self.transactions.get(str(params[0]).lower())
        if method == "eth_getBlockByNumber":
            return self.blocks.get(str(params[0]).lower())
        if method == "eth_blockNumber":
            return hex(self.latest_block)
        raise AssertionError(f"unexpected RPC method: {method}")


def fake_rpc(engine: SignerEngine) -> FakeRPC:
    return cast(FakeRPC, engine.rpc)


@pytest.fixture
def account() -> Any:
    return Account.from_key("0x" + "11" * 32)


@pytest.fixture
def engine(tmp_path: Path, account: Any) -> SignerEngine:
    state = SignerState.open(tmp_path / "signer.sqlite3")
    return SignerEngine(
        account=account,
        signer_id="staging-signer-01",
        chain_id=CHAIN_ID,
        rpc=FakeRPC(),
        state=state,
    )


def test_transfer_parser_rejects_extra_fields() -> None:
    payload = valid_transfer_payload()
    payload["unexpected"] = True
    with pytest.raises(SignerError, match="invalid transfer"):
        parse_transfer(payload)


def test_rpc_allow_list_is_enforced() -> None:
    from noyra.signer_service import ALLOWED_RPC_METHODS

    assert "eth_sendRawTransaction" in ALLOWED_RPC_METHODS
    assert "debug_traceTransaction" not in ALLOWED_RPC_METHODS


def test_duplicate_request_id_returns_original_result_without_second_broadcast(
    engine: SignerEngine,
) -> None:
    first = engine.transfer("request-1", native_transfer())
    second = engine.transfer("request-1", native_transfer())
    assert second == first
    assert len(fake_rpc(engine).sent_raw_transactions) == 1


def test_reusing_request_id_for_different_transfer_is_rejected(engine: SignerEngine) -> None:
    engine.transfer("request-1", native_transfer())
    with pytest.raises(SignerError, match="request identity"):
        engine.transfer("request-1", native_transfer(nonce=1))


def test_ambiguous_broadcast_is_persisted_and_never_retried(engine: SignerEngine) -> None:
    fake_rpc(engine).raise_after_send = TimeoutError()
    with pytest.raises(SignerError, match="broadcast unknown"):
        engine.transfer("request-2", native_transfer())
    fake_rpc(engine).raise_after_send = None
    with pytest.raises(SignerError, match="broadcast unknown"):
        engine.transfer("request-2", native_transfer())
    assert len(fake_rpc(engine).sent_raw_transactions) == 1


def test_pending_request_is_fail_closed_after_restart(tmp_path: Path, account: Any) -> None:
    state = SignerState.open(tmp_path / "signer.sqlite3")
    state.connection.execute(
        "INSERT INTO signer_requests("
        "request_id,transfer_hash,status,tx_hash,chain_id,nonce,gas_limit,max_fee_per_gas,"
        "accepted_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "request-pending",
            _transfer_hash_for_test(),
            "pending",
            "0x" + "aa" * 32,
            CHAIN_ID,
            0,
            21_000,
            "2000000000",
            None,
        ),
    )
    state.connection.commit()
    engine = SignerEngine(
        account=account,
        signer_id="staging-signer-01",
        chain_id=CHAIN_ID,
        rpc=FakeRPC(),
        state=state,
    )
    with pytest.raises(SignerError, match="broadcast unknown"):
        engine.transfer("request-pending", native_transfer())


def test_legacy_request_is_marked_unbound_during_state_migration(
    tmp_path: Path, account: Any
) -> None:
    path = tmp_path / "legacy-signer.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE signer_requests ("
        "request_id TEXT PRIMARY KEY, transfer_hash TEXT NOT NULL, status TEXT NOT NULL,"
        "tx_hash TEXT, chain_id INTEGER NOT NULL, nonce INTEGER NOT NULL,"
        "gas_limit INTEGER NOT NULL, max_fee_per_gas TEXT NOT NULL, accepted_at TEXT)"
    )
    connection.execute(
        "INSERT INTO signer_requests VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "legacy-receipt",
            "a" * 64,
            "broadcast",
            "0x" + "aa" * 32,
            CHAIN_ID,
            0,
            21_000,
            "2000000000",
            "2026-10-18T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()

    state = SignerState.open(path)
    row = state.connection.execute(
        "SELECT envelope_status FROM signer_requests WHERE request_id='legacy-receipt'"
    ).fetchone()
    assert row[0] == "legacy_unbound"
    engine = SignerEngine(
        account=account,
        signer_id="staging-signer-01",
        chain_id=CHAIN_ID,
        rpc=FakeRPC(),
        state=state,
    )
    with pytest.raises(SignerError, match="legacy_unbound"):
        engine.receipt("0x" + "aa" * 32, CHAIN_ID)


def test_identity_signature_uses_configured_account(engine: SignerEngine, account: Any) -> None:
    challenge = identity_challenge()
    signature = engine.prove_identity("staging-signer-01", challenge)
    recovered = Account.recover_message(encode_defunct(text=challenge), signature=signature)
    assert recovered.casefold() == account.address.casefold()


def test_engine_state_survives_restart(tmp_path: Path, account: Any) -> None:
    path = tmp_path / "signer.sqlite3"
    rpc = FakeRPC()
    first = SignerEngine(
        account=account,
        signer_id="staging-signer-01",
        chain_id=CHAIN_ID,
        rpc=rpc,
        state=SignerState.open(path),
    )
    result = first.transfer("request-1", native_transfer())
    second = SignerEngine(
        account=account,
        signer_id="staging-signer-01",
        chain_id=CHAIN_ID,
        rpc=rpc,
        state=SignerState.open(path),
    )
    assert second.transfer("request-1", native_transfer()) == result
    assert len(rpc.sent_raw_transactions) == 1


def post_json(
    server: SignerHTTPServer, path: str, payload: dict[str, Any], token: str | None
) -> Any:
    import httpx

    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.post(server.url + path, json=payload, headers=headers, timeout=2.0)


@pytest.fixture
def http_server(tmp_path: Path, account: Any) -> Any:
    state = SignerState.open(tmp_path / "signer.sqlite3")
    engine = SignerEngine(
        account=account,
        signer_id="staging-signer-01",
        chain_id=CHAIN_ID,
        rpc=FakeRPC(),
        state=state,
    )
    server = SignerHTTPServer(engine, bearer_token=TOKEN, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        state.close()


def test_http_requires_bearer_and_returns_nonce(http_server: SignerHTTPServer) -> None:
    payload = {"address": ADDRESS, "chain_id": CHAIN_ID}
    assert post_json(http_server, "/v1/nonce", payload, None).status_code == 401
    response = post_json(http_server, "/v1/nonce", payload, TOKEN)
    assert response.status_code == 200
    assert response.json() == {"nonce": 0}


def test_http_identity_route_returns_signature(http_server: SignerHTTPServer) -> None:
    response = post_json(
        http_server,
        "/migration-identity",
        {"signer_id": "staging-signer-01", "challenge": identity_challenge()},
        TOKEN,
    )
    assert response.status_code == 200
    assert set(response.json()) == {"signature"}


def test_http_rejects_wrong_signer_id(http_server: SignerHTTPServer) -> None:
    response = post_json(
        http_server,
        "/migration-identity",
        {"signer_id": "other", "challenge": "challenge"},
        TOKEN,
    )
    assert response.status_code == 400


def test_http_receipt_not_found_is_404(http_server: SignerHTTPServer) -> None:
    response = post_json(
        http_server,
        "/v1/receipt",
        {"tx_hash": "0x" + "aa" * 32, "chain_id": CHAIN_ID},
        TOKEN,
    )
    assert response.status_code == 404


def test_http_nonce_conflict_uses_machine_readable_error_code(
    http_server: SignerHTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def conflict(_: object, __: object) -> object:
        raise SignerError("nonce conflict", code="nonce_conflict")

    monkeypatch.setattr(http_server.engine, "nonce", conflict)
    response = post_json(
        http_server,
        "/v1/nonce",
        {"address": ADDRESS, "chain_id": CHAIN_ID},
        TOKEN,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "nonce_conflict"


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError, ConnectionResetError])
def test_http_internal_errors_return_diagnostic_500_without_exception_text(
    http_server: SignerHTTPServer, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    def fail(_: object, __: object) -> object:
        raise error_type("secret-provider-response")

    monkeypatch.setattr(http_server.engine, "nonce", fail)
    response = post_json(
        http_server,
        "/v1/nonce",
        {"address": ADDRESS, "chain_id": CHAIN_ID},
        TOKEN,
    )
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "signer_internal_error"
    assert isinstance(body["request_id"], str) and len(body["request_id"]) == 16
    assert "secret-provider-response" not in response.text


def test_receipt_requires_matching_broadcast_envelope_and_clamps_stale_rpc_height(
    engine: SignerEngine,
) -> None:
    transfer = native_transfer()
    broadcast = engine.transfer("receipt-bound-request", transfer)
    rpc = fake_rpc(engine)
    block_hash = "0x" + "ab" * 32
    rpc.receipts[broadcast.tx_hash] = {
        "transactionHash": broadcast.tx_hash,
        "status": "0x1",
        "blockNumber": "0x5",
        "blockHash": block_hash,
    }
    rpc.transactions[broadcast.tx_hash] = {
        "hash": broadcast.tx_hash,
        "from": engine.address,
        "chainId": hex(CHAIN_ID),
        "nonce": hex(transfer.nonce),
        "to": transfer.to_address,
        "value": hex(int(transfer.value)),
        "input": transfer.data,
    }
    rpc.blocks["0x5"] = {"number": "0x5", "hash": block_hash}
    rpc.latest_block = 4

    receipt = engine.receipt(broadcast.tx_hash, CHAIN_ID)
    assert receipt is not None
    assert receipt.confirmations == 0

    rpc.transactions[broadcast.tx_hash]["to"] = "0x3333333333333333333333333333333333333333"
    with pytest.raises(SignerError, match="does not match the broadcast request"):
        engine.receipt(broadcast.tx_hash, CHAIN_ID)


def test_receipt_does_not_query_unregistered_transaction(engine: SignerEngine) -> None:
    assert engine.receipt("0x" + "cc" * 32, CHAIN_ID) is None
    assert fake_rpc(engine).transactions == {}


def test_http_server_rejects_non_loopback_bind(account: Any, tmp_path: Path) -> None:
    engine = SignerEngine(
        account=account,
        signer_id="staging-signer-01",
        chain_id=CHAIN_ID,
        rpc=FakeRPC(),
        state=SignerState.open(tmp_path / "signer.sqlite3"),
    )
    with pytest.raises(ValueError, match="loopback"):
        SignerHTTPServer(engine, bearer_token=TOKEN, host="0.0.0.0")


def test_systemd_unit_is_loopback_only_and_credential_backed() -> None:
    text = (Path(__file__).parents[1] / "deploy/systemd/noyra-signer.service").read_text()
    assert "User=noyra-signer" in text
    assert "127.0.0.1" in text and "8780" in text
    assert text.count("LoadCredential=") == 3
    assert "PermissionsStartOnly=true" in text
    assert (
        "ExecStartPre=/usr/bin/install -o noyra-signer -g noyra-signer -m 0600 "
        "%d/wallet-password /var/lib/noyra-signer/.wallet-password"
    ) in text
    assert "--wallet-password-credential /var/lib/noyra-signer/.wallet-password" in text
    assert "ExecStopPost=/usr/bin/rm -f /var/lib/noyra-signer/.wallet-password" in text
    assert "ProtectSystem=strict" in text


def _transfer_hash_for_test() -> str:
    return _sha256_json(valid_transfer_payload())


def _sha256_json(value: object) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_transfer_accepts_canonical_lowercase_recipient_address(engine: SignerEngine) -> None:
    payload = valid_transfer_payload()
    payload["to_address"] = "0xb5ac3ebb4fd1be6561d0f56dcb2c4a114c98d5af"
    transfer = parse_transfer(payload)
    result = engine.transfer("mixed-case-recipient-regression", transfer)
    assert result.chain_id == CHAIN_ID
    assert len(fake_rpc(engine).sent_raw_transactions) == 1


@pytest.mark.parametrize("existing_column", [False, True])
@pytest.mark.parametrize("damage", [None, "json", "hash", "nonce", "gas", "fee", "missing"])
def test_migration_validates_historical_envelopes(
    tmp_path: Path, account: Any, existing_column: bool, damage: str | None
) -> None:
    from noyra.signer_service import _transfer_hash

    path = tmp_path / "old-envelope.sqlite3"
    transfer = native_transfer()
    payload: str | None = json.dumps(valid_transfer_payload())
    if damage == "json":
        payload = "{invalid"
    elif damage == "missing":
        payload = None
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE signer_requests (request_id TEXT PRIMARY KEY, transfer_hash TEXT, "
            "status TEXT, tx_hash TEXT, chain_id INTEGER, nonce INTEGER, gas_limit INTEGER, "
            "max_fee_per_gas TEXT, accepted_at TEXT, transfer_json TEXT"
            + (", envelope_status TEXT" if existing_column else "")
            + ")"
        )
        values = [
            "history",
            "0" * 64 if damage == "hash" else _transfer_hash(transfer),
            "unknown",
            "0x" + "aa" * 32,
            CHAIN_ID,
            9 if damage == "nonce" else 0,
            999 if damage == "gas" else 21_000,
            "1" if damage == "fee" else "2000000000",
            None,
            payload,
        ]
        if existing_column:
            values.append("legacy_unbound")
        connection.execute(
            "INSERT INTO signer_requests VALUES (" + ",".join("?" for _ in values) + ")", values
        )
    for _ in range(2):
        state = SignerState.open(path)
        try:
            row = state.connection.execute("SELECT * FROM signer_requests").fetchone()
            assert row["envelope_status"] == ("bound" if damage is None else "legacy_unbound")
            assert row["status"] == "unknown"
            rpc = FakeRPC()
            engine = SignerEngine(account=account, signer_id="test", rpc=rpc, state=state)
            if damage is None:
                assert engine.receipt("0x" + "aa" * 32, CHAIN_ID) is None
            else:
                with pytest.raises(SignerError, match="legacy_unbound"):
                    engine.receipt("0x" + "aa" * 32, CHAIN_ID)
            assert rpc.sent_raw_transactions == []
        finally:
            state.close()


@pytest.mark.parametrize("kind", ["oversized", "compressed", "wrong-id", "valid"])
def test_rpc_stream_limits_and_response_binding(kind: str) -> None:
    import gzip

    import httpx

    from noyra.signer_service import HTTPRPC, MAX_RPC_RESPONSE_BYTES

    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "wrong" if kind == "wrong-id" else "noyra-signer-v1",
            "result": "0x1",
            "padding": "x"
            * (MAX_RPC_RESPONSE_BYTES + 1 if kind in {"oversized", "compressed"} else 1),
        }
    ).encode()

    class Body(httpx.SyncByteStream):
        closed = False
        consumed = 0

        def __iter__(self) -> Any:
            for start in range(0, len(payload), 4096):
                self.consumed += 4096
                yield payload[start : start + 4096]

        def close(self) -> None:
            self.closed = True

    stream = Body()

    def response(request: httpx.Request) -> httpx.Response:
        if kind == "compressed":
            return httpx.Response(
                200, content=gzip.compress(payload), headers={"Content-Encoding": "gzip"}
            )
        return httpx.Response(200, stream=stream)

    rpc = HTTPRPC("https://rpc.example", CHAIN_ID)
    rpc.client.close()
    rpc.client = httpx.Client(transport=httpx.MockTransport(response))
    try:
        if kind == "valid":
            assert rpc.call("eth_chainId", []) == "0x1"
        else:
            with pytest.raises(SignerError):
                rpc.call("eth_chainId", [])
        if kind != "compressed":
            assert stream.closed
            assert stream.consumed <= MAX_RPC_RESPONSE_BYTES + 4096
    finally:
        rpc.close()


def test_rpc_absolute_deadline_closes_response_and_preserves_unknown(engine: SignerEngine) -> None:
    import time

    import httpx

    from noyra.signer_service import HTTPRPC

    release = threading.Event()
    sends: list[str] = []

    class StalledBody(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            yield b"{"
            release.wait(2)
            yield b"}"

        def close(self) -> None:
            release.set()

    def response(request: httpx.Request) -> httpx.Response:
        sends.append(str(request.url))
        return httpx.Response(200, stream=StalledBody())

    rpc = HTTPRPC("https://rpc.example", CHAIN_ID, timeout=0.15)
    rpc.client.close()
    rpc.client = httpx.Client(transport=httpx.MockTransport(response))
    original = engine.rpc

    class SendRPC:
        def call(self, method: str, params: list[object]) -> object:
            return (
                rpc.call(method, params)
                if method == "eth_sendRawTransaction"
                else original.call(method, params)
            )

    engine.rpc = SendRPC()
    started = time.perf_counter()
    try:
        with pytest.raises(SignerError, match="broadcast unknown"):
            engine.transfer("timeout", native_transfer())
        assert time.perf_counter() - started < 1.0
        assert release.is_set()
        with pytest.raises(SignerError, match="broadcast unknown"):
            engine.transfer("timeout", native_transfer())
        assert len(sends) == 1
        assert (
            engine.state.connection.execute(
                "SELECT status FROM signer_requests WHERE request_id='timeout'"
            ).fetchone()[0]
            == "unknown"
        )
    finally:
        release.set()
        rpc.close()


def test_rpc_rejects_private_dns_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    from noyra.signer_service import HTTPRPC

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))
        ],
    )
    rpc = HTTPRPC("https://rpc.example", CHAIN_ID, timeout=0.5)
    try:
        with pytest.raises(SignerError, match="rpc unavailable"):
            rpc.call("eth_chainId", [])
    finally:
        rpc.close()


@pytest.mark.parametrize("send_request", [False, True])
def test_http_slow_clients_are_bounded_and_release_capacity(
    http_server: SignerHTTPServer, monkeypatch: pytest.MonkeyPatch, send_request: bool
) -> None:
    import socket

    from noyra.core.request_io import DeadlineRequestReader

    monkeypatch.setattr("noyra.signer_service.SIGNER_REQUEST_SECONDS", 0.4)
    entered = threading.Barrier(3)
    release_read = threading.Event()
    released_slots = threading.Event()
    original_read = DeadlineRequestReader.readinto

    def hold_first_read(reader: Any, buffer: Any) -> int:
        if not getattr(reader, "_test_first_read_held", False):
            reader._test_first_read_held = True
            entered.wait(timeout=5)
            assert release_read.wait(5)
        return original_read(reader, buffer)

    class CompletionSemaphore(threading.BoundedSemaphore):
        def release(self, n: int = 1) -> None:
            super().release(n)
            if self._value == 2:
                released_slots.set()

    # Keep both real workers occupied during the overload probe; scheduler
    # delays must not let their short deadlines race the capacity assertion.
    monkeypatch.setattr(DeadlineRequestReader, "readinto", hold_first_read)
    http_server._worker_slots = CompletionSemaphore(2)
    clients = [
        socket.create_connection(("127.0.0.1", http_server.server_port), timeout=3)
        for _ in range(2)
    ]
    try:
        for client in clients:
            client.sendall(b"POST /v1/nonce HTTP/1.1\r\nHost: localhost\r\n")
        entered.wait(timeout=5)
        with socket.create_connection(
            ("127.0.0.1", http_server.server_port), timeout=3
        ) as overloaded:
            try:
                if send_request:
                    overloaded.sendall(b"GET / HTTP/1.0\r\n\r\n")
                response = overloaded.recv(512)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                # Rejection precedes HTTP parsing. Closing with unread peer
                # bytes may reset TCP instead of delivering the best-effort 503.
                assert send_request
            else:
                assert b"503" in response
        monkeypatch.setattr(DeadlineRequestReader, "readinto", original_read)
        release_read.set()
        for client in clients:
            # Either EOF or reset closes the expired partial request.
            with suppress(ConnectionResetError, ConnectionAbortedError):
                assert client.recv(512) == b""
        assert released_slots.wait(3)
        assert (
            post_json(
                http_server, "/v1/nonce", {"address": ADDRESS, "chain_id": CHAIN_ID}, TOKEN
            ).status_code
            == 200
        )
    finally:
        release_read.set()
        entered.abort()
        for client in clients:
            client.close()


def test_http_incomplete_authenticated_body_never_dispatches(
    http_server: SignerHTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    monkeypatch.setattr("noyra.signer_service.SIGNER_REQUEST_SECONDS", 0.15)
    with socket.create_connection(("127.0.0.1", http_server.server_port), timeout=1) as client:
        client.sendall(
            (
                "POST /v1/transfer HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
                + TOKEN
                + "\r\nContent-Type: application/json\r\nContent-Length: 20\r\n\r\n{"
            ).encode()
        )
        assert client.recv(512) == b""
    assert (
        http_server.engine.state.connection.execute(
            "SELECT COUNT(*) FROM signer_requests"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "error", [TimeoutError("body deadline"), ConnectionResetError("peer reset")]
)
def test_http_body_read_failure_closes_without_reply_or_dispatch(
    http_server: SignerHTTPServer, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    from email.message import Message
    from unittest.mock import Mock

    # Inject the read error independently of the clock: on Windows a socket
    # timeout can arrive just before the absolute deadline has expired.
    handler = object.__new__(http_server._handler())
    handler.headers = Message()
    for key, value in (
        ("Authorization", f"Bearer {TOKEN}"),
        ("Content-Length", "20"),
        ("Content-Type", "application/json"),
    ):
        handler.headers[key] = value
    handler.path = "/v1/transfer"
    handler.rfile = Mock(read=Mock(side_effect=error))
    handler.close_connection = False
    dispatch = Mock()
    send = Mock()
    monkeypatch.setattr(handler, "_dispatch", dispatch)
    monkeypatch.setattr(handler, "_send", send)

    cast(Any, handler).do_POST()

    assert handler.close_connection
    dispatch.assert_not_called()
    send.assert_not_called()
    assert (
        http_server.engine.state.connection.execute(
            "SELECT COUNT(*) FROM signer_requests"
        ).fetchone()[0]
        == 0
    )


def test_rpc_worker_cannot_send_after_its_caller_timed_out(engine: SignerEngine) -> None:
    import httpx

    from noyra.signer_service import HTTPRPC

    release = threading.Event()
    finished = threading.Event()
    sent: list[httpx.Request] = []

    def delayed_send(request: httpx.Request) -> httpx.Response:
        try:
            assert release.wait(2)
            # Mirror the production HTTP transport's final pre-header hook.
            request.extensions["trace"]("http11.send_request_headers.started", {})
            sent.append(request)
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": "noyra-signer-v1", "result": "0x0"}
            )
        finally:
            finished.set()

    rpc = HTTPRPC("https://rpc.example", CHAIN_ID, timeout=0.15)
    rpc.client.close()
    rpc.client = httpx.Client(transport=httpx.MockTransport(delayed_send))
    original = engine.rpc

    class SendRPC:
        def call(self, method: str, params: list[object]) -> object:
            return (
                rpc.call(method, params)
                if method == "eth_sendRawTransaction"
                else original.call(method, params)
            )

    engine.rpc = SendRPC()
    try:
        with pytest.raises(SignerError, match="broadcast unknown"):
            engine.transfer("late-send", native_transfer())
        release.set()
        assert finished.wait(1)
        assert not sent
        with pytest.raises(SignerError, match="broadcast unknown"):
            engine.transfer("late-send", native_transfer())
        assert not sent
    finally:
        release.set()
        rpc.close()


@pytest.mark.parametrize(
    "challenge",
    [
        "Unrelated login request",
        "{}",
        "[]",
        "null",
        identity_challenge().replace("noyra-migration-wallet/v1", "unrelated-auth/v1"),
        identity_challenge().replace('"nonce":', '"unknown":'),
        identity_challenge().replace('"staging-signer-01"', '"wrong-signer"'),
        identity_challenge().replace('"manifest_digest":"' + "a" * 64, '"manifest_digest":"short'),
        identity_challenge() + " ",
    ],
)
def test_identity_rejects_other_domains_and_malformed_context(
    engine: SignerEngine,
    challenge: str,
) -> None:
    with pytest.raises(SignerError, match="identity rejected"):
        engine.prove_identity("staging-signer-01", challenge)


def test_http_identity_cannot_be_used_as_generic_personal_sign(
    http_server: SignerHTTPServer,
) -> None:
    response = post_json(
        http_server,
        "/migration-identity",
        {
            "signer_id": "staging-signer-01",
            "challenge": "Unrelated login request",
        },
        TOKEN,
    )
    assert response.status_code == 400
