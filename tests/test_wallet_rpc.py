from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from noyra.core import Database, IdentityStore
from noyra.core.http import PublicDNSHTTPTransport
from noyra.core.types import content_hash
from noyra.wallet import (
    WalletAddressInput,
    WalletAssetInput,
    WalletNetworkInput,
    WalletRPCBalanceAcquirer,
    WalletRPCError,
    WalletStore,
)
from noyra.wallet.rpc import WALLET_RPC_MAX_RESPONSE_BYTES


def _store(tmp_path: Path) -> tuple[Database, str, WalletStore]:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-wallet-rpc"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    return database, subject_id, WalletStore(database)


def _graph(
    store: WalletStore,
    subject_id: str,
    *,
    rpc_url: str | None = "https://rpc.example",
) -> tuple[str, str, str]:
    network = store.register_network(
        subject_id,
        WalletNetworkInput(
            label="Ethereum Mainnet",
            chain_id=1,
            native_symbol="ETH",
            rpc_url=rpc_url,
        ),
        actor="operator",
    )
    asset = store.register_asset(
        subject_id,
        WalletAssetInput(
            network_id=network.network_id,
            asset_type="native",
            name="Ether",
            symbol="ETH",
            decimals=18,
        ),
        actor="operator",
    )
    address = store.register_address(
        subject_id,
        WalletAddressInput(
            network_id=network.network_id,
            label="Treasury",
            address="0xA111111111111111111111111111111111111111",
            purpose="treasury",
        ),
        actor="operator",
    )
    return network.network_id, asset.asset_id, address.address_id


def _response(payload: object, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"content-type": "application/json"},
        json=payload,
    )


def test_native_balance_request_is_pinned_to_registered_origin_and_snapshot(tmp_path: Path) -> None:
    _database, subject_id, store = _store(tmp_path)
    network_id, asset_id, address_id = _graph(store, subject_id)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if json.loads(request.content)["method"] == "eth_chainId":
            return _response(
                {
                    "jsonrpc": "2.0",
                    "id": "noyra-wallet-balance-v1",
                    "result": "0x1",
                }
            )
        return _response(
            {
                "jsonrpc": "2.0",
                "id": "noyra-wallet-balance-v1",
                "result": "0xffff",
            }
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        WalletRPCBalanceAcquirer(store, client=client) as acquirer,
    ):
        snapshot = acquirer.acquire_balance(
            subject_id,
            asset_id=asset_id,
            address_id=address_id,
            actor="operator",
        )

    assert network_id
    assert snapshot.balance == "65535"
    assert snapshot.source == "evm_rpc"
    assert len(requests) == 2
    assert all(str(request.url) == "https://rpc.example" for request in requests)
    assert all(request.method == "POST" for request in requests)
    assert all(request.headers["accept-encoding"] == "identity" for request in requests)
    assert [json.loads(request.content) for request in requests] == [
        {
            "jsonrpc": "2.0",
            "id": "noyra-wallet-balance-v1",
            "method": "eth_chainId",
            "params": [],
        },
        {
            "jsonrpc": "2.0",
            "id": "noyra-wallet-balance-v1",
            "method": "eth_getBalance",
            "params": ["0xa111111111111111111111111111111111111111", "latest"],
        },
    ]
    assert store.latest_balances(subject_id) == [snapshot]


def test_token_balance_uses_only_the_fixed_erc20_balanceof_call(tmp_path: Path) -> None:
    _database, subject_id, store = _store(tmp_path)
    _network_id, _native_asset_id, address_id = _graph(store, subject_id)
    address = store.get_address(address_id, subject_id=subject_id)
    token = store.register_asset(
        subject_id,
        WalletAssetInput(
            network_id=address.network_id,
            asset_type="token",
            contract_address="0xB222222222222222222222222222222222222222",
            name="US Dollar Coin",
            symbol="USDC",
            decimals=6,
        ),
        actor="operator",
    )
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["method"] == "eth_chainId":
            return _response(
                {
                    "jsonrpc": "2.0",
                    "id": "noyra-wallet-balance-v1",
                    "result": "0x1",
                }
            )
        return _response(
            {
                "jsonrpc": "2.0",
                "id": "noyra-wallet-balance-v1",
                "result": "0x" + "0" * 58 + "123456",
            }
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshot = WalletRPCBalanceAcquirer(store, client=client).acquire_balance(
            subject_id,
            asset_id=token.asset_id,
            address_id=address_id,
            actor="operator",
        )

    assert snapshot.balance == str(int("123456", 16))
    assert requests == [
        {
            "jsonrpc": "2.0",
            "id": "noyra-wallet-balance-v1",
            "method": "eth_chainId",
            "params": [],
        },
        {
            "jsonrpc": "2.0",
            "id": "noyra-wallet-balance-v1",
            "method": "eth_call",
            "params": [
                {
                    "to": "0xb222222222222222222222222222222222222222",
                    "data": (
                        "0x70a08231000000000000000000000000a111111111111111111111111111111111111111"
                    ),
                },
                "latest",
            ],
        },
    ]


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_response({"jsonrpc": "2.0", "id": "wrong", "result": "0x1"}), "invalid_response"),
        (
            _response({"jsonrpc": "2.0", "id": "noyra-wallet-balance-v1", "error": {}}),
            "remote_error",
        ),
        (
            _response({"jsonrpc": "2.0", "id": "noyra-wallet-balance-v1", "result": "0x00"}),
            "invalid_quantity",
        ),
        (httpx.Response(302, headers={"location": "https://redirect.example"}), "http_302"),
        (
            httpx.Response(200, headers={"content-type": "text/plain"}, content=b"{}"),
            "content_type",
        ),
        (
            httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-length": str(WALLET_RPC_MAX_RESPONSE_BYTES + 1),
                },
                content=b"{}",
            ),
            "response_too_large",
        ),
    ],
)
def test_rpc_failures_are_bounded_and_sanitized(
    tmp_path: Path,
    response: httpx.Response,
    code: str,
) -> None:
    _database, subject_id, store = _store(tmp_path)
    _network_id, asset_id, address_id = _graph(store, subject_id)

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["method"] == "eth_chainId":
            return _response(
                {
                    "jsonrpc": "2.0",
                    "id": "noyra-wallet-balance-v1",
                    "result": "0x1",
                }
            )
        return response

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(WalletRPCError, match=f"wallet_rpc_{code}"),
    ):
        WalletRPCBalanceAcquirer(store, client=client).acquire_balance(
            subject_id,
            asset_id=asset_id,
            address_id=address_id,
            actor="operator",
        )
    assert store.list_balance_snapshots(subject_id) == []


def test_rpc_rejects_a_registered_network_when_provider_chain_id_differs(tmp_path: Path) -> None:
    _database, subject_id, store = _store(tmp_path)
    _network_id, asset_id, address_id = _graph(store, subject_id)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(str(json.loads(request.content)["method"]))
        return _response(
            {
                "jsonrpc": "2.0",
                "id": "noyra-wallet-balance-v1",
                "result": "0x2",
            }
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(WalletRPCError, match="wallet_rpc_chain_id_mismatch"),
    ):
        WalletRPCBalanceAcquirer(store, client=client).acquire_balance(
            subject_id,
            asset_id=asset_id,
            address_id=address_id,
            actor="operator",
        )
    assert methods == ["eth_chainId"]
    assert store.list_balance_snapshots(subject_id) == []


def test_rpc_configuration_and_operator_checks_fail_before_connecting(tmp_path: Path) -> None:
    _database, subject_id, store = _store(tmp_path)
    _network_id, asset_id, address_id = _graph(store, subject_id, rpc_url=None)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        return _response({"jsonrpc": "2.0", "id": "noyra-wallet-balance-v1", "result": "0x1"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        acquirer = WalletRPCBalanceAcquirer(store, client=client)
        with pytest.raises(WalletRPCError, match="wallet_rpc_not_configured"):
            acquirer.acquire_balance(
                subject_id,
                asset_id=asset_id,
                address_id=address_id,
                actor="operator",
            )
        with pytest.raises(PermissionError):
            acquirer.acquire_balance(
                subject_id,
                asset_id=asset_id,
                address_id=address_id,
                actor="subject",
            )
    assert calls == 0


def test_rpc_url_rejects_private_origins_and_canonicalizes_public_origin() -> None:
    proposal = WalletNetworkInput(
        label="Public RPC",
        chain_id=1,
        native_symbol="ETH",
        rpc_url="https://RPC.Example/",
    )
    assert proposal.rpc_url == "https://rpc.example"
    for rpc_url in ("https://localhost", "https://127.0.0.1", "https://[::1]"):
        with pytest.raises(ValueError):
            WalletNetworkInput(
                label="Unsafe RPC",
                chain_id=1,
                native_symbol="ETH",
                rpc_url=rpc_url,
            )


def test_default_rpc_client_uses_the_public_dns_pinned_transport(tmp_path: Path) -> None:
    _database, _subject_id, store = _store(tmp_path)
    acquirer = WalletRPCBalanceAcquirer(store)
    try:
        assert isinstance(acquirer._client._transport, PublicDNSHTTPTransport)
    finally:
        acquirer.close()


def test_rpc_acquirer_enforces_its_concurrency_budget(tmp_path: Path) -> None:
    _database, subject_id, store = _store(tmp_path)
    _network_id, asset_id, address_id = _graph(store, subject_id)
    with httpx.Client(transport=httpx.MockTransport(lambda _: _response({}))) as client:
        acquirer = WalletRPCBalanceAcquirer(
            store,
            client=client,
            timeout_seconds=0.01,
            max_concurrent_requests=1,
        )
        assert acquirer._slots.acquire(blocking=False)
        try:
            with pytest.raises(WalletRPCError, match="wallet_rpc_capacity_exhausted"):
                acquirer.acquire_balance(
                    subject_id,
                    asset_id=asset_id,
                    address_id=address_id,
                    actor="operator",
                )
        finally:
            acquirer._slots.release()
