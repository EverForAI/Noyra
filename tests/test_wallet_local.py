from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import httpx
import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import keccak  # type: ignore[attr-defined]

from noyra.core.types import content_hash
from noyra.wallet.execution import (
    WalletBroadcastUnknownError,
    WalletExecutionError,
    WalletSignerError,
    WalletUnsignedTransfer,
)
from noyra.wallet.local import LocalWalletSigner

RECIPIENT = "0x" + "22" * 20
CONTRACT = "0x" + "33" * 20
BLOCK_HASH = "0x" + "aa" * 32


def account() -> LocalAccount:
    return cast(LocalAccount, Account.from_key(bytes.fromhex("11" * 32)))


def transfer(*, token: bool = False) -> WalletUnsignedTransfer:
    return WalletUnsignedTransfer(
        chain_id=1,
        nonce=0,
        gas_limit=100_000 if token else 21_000,
        max_fee_per_gas="2000000000",
        source_address=account().address.lower(),
        to_address=CONTRACT if token else RECIPIENT,
        value="0" if token else "1000",
        data="0xa9059cbb" + RECIPIENT[2:].rjust(64, "0") + hex(1000)[2:].rjust(64, "0")
        if token
        else "0x",
        asset_type="token" if token else "native",
        contract_address=CONTRACT if token else None,
    )


class Chain:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.overrides: dict[str, Any] = {}
        self.lose_response = False
        self.receipt: dict[str, Any] | None = None
        self.transaction: dict[str, Any] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        defaults = {
            "eth_chainId": "0x1",
            "eth_getTransactionCount": "0x0",
            "eth_getBalance": hex(10**20),
            "eth_gasPrice": hex(10**9),
            "eth_estimateGas": "0x5208",
            "eth_call": "0x" + hex(10**20)[2:].rjust(64, "0"),
            "eth_getTransactionReceipt": self.receipt,
            "eth_getTransactionByHash": self.transaction,
            "eth_getBlockByNumber": {"number": "0x64", "hash": BLOCK_HASH},
            "eth_blockNumber": "0x66",
        }
        if method == "eth_sendRawTransaction":
            self.sent.append(params[0])
            if self.lose_response:
                raise httpx.ReadTimeout("private provider response", request=request)
            result: object = "0x" + keccak(bytes.fromhex(params[0][2:])).hex()
        else:
            assert method in defaults, method
            result = defaults[method]
        result = self.overrides.get(method, result)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@contextmanager
def signer(chain: Chain) -> Iterator[LocalWalletSigner]:
    with httpx.Client(transport=httpx.MockTransport(chain.handle)) as client:
        instance = LocalWalletSigner(account(), {1: "https://rpc.example"}, client=client)
        try:
            yield instance
        finally:
            instance.close()


@pytest.mark.parametrize("token", [False, True])
def test_local_signs_real_fixed_transfer_and_deterministic_restart_retry(token: bool) -> None:
    chain = Chain()
    envelope = transfer(token=token)
    with signer(chain) as wallet:
        result = wallet.sign_and_broadcast(envelope, request_id="payment:1")
        assert Account.recover_transaction(chain.sent[0]).lower() == envelope.source_address
        assert result.tx_hash == "0x" + keccak(bytes.fromhex(chain.sent[0][2:])).hex()
        assert result.chain_id == 1 and result.nonce == 0
    with signer(chain) as wallet:
        retried = wallet.sign_and_broadcast(envelope, request_id="payment:2")
    assert result.tx_hash == retried.tx_hash
    assert chain.sent[0] == chain.sent[1]


def test_lost_broadcast_preserves_transaction_hash() -> None:
    chain = Chain()
    chain.lose_response = True
    with signer(chain) as wallet, pytest.raises(WalletBroadcastUnknownError) as error:
        wallet.sign_and_broadcast(transfer(), request_id="payment")
    assert error.value.tx_hash == "0x" + keccak(bytes.fromhex(chain.sent[0][2:])).hex()
    assert "private provider" not in str(error.value)


@pytest.mark.parametrize(
    "method,result",
    [
        ("eth_chainId", "0x2"),
        ("eth_getBalance", "0x0"),
        ("eth_estimateGas", "0xffffff"),
        ("eth_gasPrice", hex(10**18)),
    ],
)
def test_local_preflight_rejects_invalid_chain_balance_or_fees(method: str, result: str) -> None:
    chain = Chain()
    chain.overrides[method] = result
    with signer(chain) as wallet, pytest.raises(WalletSignerError):
        wallet.sign_and_broadcast(transfer(), request_id="payment")
    assert chain.sent == []


def test_advanced_nonce_is_unknown_so_prior_payment_is_not_released() -> None:
    chain = Chain()
    chain.overrides["eth_getTransactionCount"] = "0x1"
    with signer(chain) as wallet, pytest.raises(WalletBroadcastUnknownError) as error:
        wallet.sign_and_broadcast(transfer(), request_id="payment")
    assert error.value.tx_hash is not None
    assert chain.sent == []


def test_wrong_source_and_noncanonical_token_input_never_broadcast() -> None:
    chain = Chain()
    invalid = transfer().model_copy(update={"source_address": RECIPIENT})
    with signer(chain) as wallet, pytest.raises(WalletSignerError):
        wallet.sign_and_broadcast(invalid, request_id="payment")
    malformed = transfer(token=True).model_copy(update={"data": "0xa9059cbb" + "f" * 128})
    with signer(chain) as wallet, pytest.raises(WalletSignerError):
        wallet.sign_and_broadcast(malformed, request_id="payment")
    assert chain.sent == []


def test_nonce_quote_and_fee_quote_are_chain_derived() -> None:
    chain = Chain()
    with signer(chain) as wallet:
        assert wallet.get_pending_nonce(account().address, chain_id=1) == 0
        gas, fee = wallet.get_fee_quote(transfer())
        assert gas >= 21_000
        assert int(fee) >= 10**9


def mined(chain: Chain, envelope: WalletUnsignedTransfer, tx_hash: str) -> None:
    chain.transaction = {
        "hash": tx_hash,
        "chainId": "0x1",
        "nonce": "0x0",
        "from": envelope.source_address,
        "to": envelope.to_address,
        "value": hex(int(envelope.value)),
        "gas": hex(envelope.gas_limit),
        "gasPrice": hex(int(envelope.max_fee_per_gas)),
        "input": envelope.data,
        "blockHash": BLOCK_HASH,
        "blockNumber": "0x64",
        "type": "0x0",
    }
    chain.receipt = {
        "transactionHash": tx_hash,
        "blockHash": BLOCK_HASH,
        "blockNumber": "0x64",
        "status": "0x1",
        "from": envelope.source_address,
        "to": envelope.to_address,
        "logs": [],
    }
    if envelope.asset_type == "token":
        chain.receipt["logs"] = [
            {
                "address": CONTRACT,
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + envelope.source_address[2:].rjust(64, "0"),
                    "0x" + RECIPIENT[2:].rjust(64, "0"),
                ],
                "data": "0x" + hex(1000)[2:].rjust(64, "0"),
                "removed": False,
                "transactionHash": tx_hash,
                "blockHash": BLOCK_HASH,
                "blockNumber": "0x64",
            }
        ]


@pytest.mark.parametrize("token", [False, True])
def test_receipt_verifies_canonical_block_and_transfer_effect(token: bool) -> None:
    chain = Chain()
    envelope = transfer(token=token)
    with signer(chain) as wallet:
        result = wallet.sign_and_broadcast(envelope, request_id="payment")
    mined(chain, envelope, result.tx_hash)
    with signer(chain) as wallet:
        receipt = wallet.get_receipt(result.tx_hash, chain_id=1)
        assert receipt is not None
        assert receipt.status == 1 and receipt.confirmations == 3
        assert receipt.effect_hash == "0x" + content_hash(
            {
                "tx_hash": result.tx_hash,
                "chain_id": 1,
                "asset_type": envelope.asset_type,
                "contract_address": envelope.contract_address,
                "recipient_address": RECIPIENT,
                "amount": "1000",
            }
        )
        chain.overrides["eth_getBlockByNumber"] = {"number": "0x64", "hash": "0x" + "bb" * 32}
        with pytest.raises(WalletExecutionError):
            wallet.get_receipt(result.tx_hash, chain_id=1)


@pytest.mark.parametrize("corruption", ["no_log", "wrong_amount", "wrong_sender", "wrong_tx"])
def test_successful_token_receipt_without_matching_effect_is_rejected(corruption: str) -> None:
    chain = Chain()
    with signer(chain) as wallet:
        result = wallet.sign_and_broadcast(transfer(token=True), request_id="payment")
        mined(chain, transfer(token=True), result.tx_hash)
        assert chain.receipt is not None and chain.transaction is not None
        if corruption == "no_log":
            chain.receipt["logs"] = []
        elif corruption == "wrong_amount":
            chain.receipt["logs"][0]["data"] = "0x" + "0" * 64
        elif corruption == "wrong_sender":
            chain.receipt["logs"][0]["topics"][1] = "0x" + "0" * 64
        else:
            chain.transaction["value"] = "0x1"
        with pytest.raises(WalletExecutionError):
            wallet.get_receipt(result.tx_hash, chain_id=1)


@pytest.mark.parametrize(
    "url", ["http://rpc.example", "https://127.0.0.1", "https://a:b@rpc.example"]
)
def test_local_rpc_rejects_unsafe_endpoint(url: str) -> None:
    with pytest.raises(ValueError):
        LocalWalletSigner(account(), {1: url})
