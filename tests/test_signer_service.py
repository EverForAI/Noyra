from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

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
            from eth_utils import keccak

            tx_hash = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
            self.sent_raw_transactions.append(tx_hash)
            if self.raise_after_send is not None:
                raise self.raise_after_send
            return tx_hash
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(str(params[0]).lower())
        if method == "eth_getTransactionByHash":
            return None
        if method == "eth_getBlockByNumber":
            return None
        if method == "eth_blockNumber":
            return "0x1"
        raise AssertionError(f"unexpected RPC method: {method}")


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
    assert len(engine.rpc.sent_raw_transactions) == 1


def test_reusing_request_id_for_different_transfer_is_rejected(engine: SignerEngine) -> None:
    engine.transfer("request-1", native_transfer())
    with pytest.raises(SignerError, match="request identity"):
        engine.transfer("request-1", native_transfer(nonce=1))


def test_ambiguous_broadcast_is_persisted_and_never_retried(engine: SignerEngine) -> None:
    engine.rpc.raise_after_send = TimeoutError()
    with pytest.raises(SignerError, match="broadcast unknown"):
        engine.transfer("request-2", native_transfer())
    engine.rpc.raise_after_send = None
    with pytest.raises(SignerError, match="broadcast unknown"):
        engine.transfer("request-2", native_transfer())
    assert len(engine.rpc.sent_raw_transactions) == 1


def test_pending_request_is_fail_closed_after_restart(tmp_path: Path, account: Any) -> None:
    state = SignerState.open(tmp_path / "signer.sqlite3")
    state.connection.execute(
        "INSERT INTO signer_requests VALUES (?,?,?,?,?,?,?,?,?)",
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


def test_identity_signature_uses_configured_account(engine: SignerEngine, account: Any) -> None:
    challenge = "noyra signer challenge"
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
        {"signer_id": "staging-signer-01", "challenge": "challenge"},
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
    assert "ProtectSystem=strict" in text


def _transfer_hash_for_test() -> str:
    return _sha256_json(valid_transfer_payload())


def _sha256_json(value: object) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
