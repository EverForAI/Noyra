from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import content_hash
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.wallet import (
    WALLET_BALANCE_OBSERVATION_MAX_GROUPS,
    WalletAddressInput,
    WalletAssetInput,
    WalletBalanceSnapshotInput,
    WalletNetworkInput,
    WalletStore,
)

EVALUATED_AT = "2026-09-02T00:00:00.000+00:00"


@pytest.fixture
def breakdown_store(tmp_path: Path) -> tuple[Database, str, WalletStore]:
    database = Database(tmp_path / "wallet-breakdown.sqlite3")
    subject_id = "Noyra-wallet-breakdown"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    return database, subject_id, WalletStore(database)


def _network(store: WalletStore, subject_id: str, label: str, chain_id: int) -> Any:
    return store.register_network(
        subject_id,
        WalletNetworkInput(
            label=label,
            chain_id=chain_id,
            native_symbol="ETH",
            rpc_url="https://rpc.example",
        ),
        actor="operator",
    )


def _pair(store: WalletStore, subject_id: str, network_id: str, index: int) -> tuple[Any, Any]:
    asset = store.register_asset(
        subject_id,
        WalletAssetInput(
            network_id=network_id,
            asset_type="native",
            name=f"Ether {index}",
            symbol="ETH",
            decimals=18,
        ),
        actor="operator",
    )
    address = store.register_address(
        subject_id,
        WalletAddressInput(
            network_id=network_id,
            label=f"Address {index}",
            address=f"0x{index:040x}",
            purpose="observation",
        ),
        actor="operator",
    )
    return asset, address


def _record(
    store: WalletStore,
    subject_id: str,
    asset_id: str,
    address_id: str,
    balance: str,
    source: str,
    observed_at: str,
) -> None:
    store.record_balance_snapshot(
        subject_id,
        WalletBalanceSnapshotInput(
            asset_id=asset_id,
            address_id=address_id,
            balance=balance,
            source=source,
            observed_at=observed_at,
        ),
        actor="operator",
    )


def test_breakdown_empty_and_never_pairs_are_bounded(
    breakdown_store: tuple[Database, str, WalletStore],
) -> None:
    _database, subject_id, store = breakdown_store
    empty = store.observation_health_breakdown(subject_id, now=EVALUATED_AT)
    assert empty["status"] == "ok"
    assert empty["groups"] == []
    assert empty["total_groups"] == 0
    assert empty["has_more"] is False

    network = _network(store, subject_id, "Never", 7001)
    _pair(store, subject_id, network.network_id, 1)
    network_view = store.observation_health_by_network(subject_id, now=EVALUATED_AT)
    assert network_view["total_groups"] == 1
    assert network_view["groups"][0]["value"] == network.network_id
    assert network_view["groups"][0]["active_pairs"] == 1
    assert network_view["groups"][0]["never_pairs"] == 1
    assert network_view["groups"][0]["snapshot_count"] == 0

    source_view = store.observation_health_by_source(subject_id, now=EVALUATED_AT)
    assert source_view["groups"][0]["value"] is None
    assert source_view["groups"][0]["never_pairs"] == 1


def test_breakdown_groups_by_latest_source_and_counts_all_history(
    breakdown_store: tuple[Database, str, WalletStore],
) -> None:
    _database, subject_id, store = breakdown_store
    first_network = _network(store, subject_id, "First", 7002)
    first_asset, first_address = _pair(store, subject_id, first_network.network_id, 2)
    second_network = _network(store, subject_id, "Second", 7003)
    second_asset, second_address = _pair(store, subject_id, second_network.network_id, 3)

    _record(
        store,
        subject_id,
        first_asset.asset_id,
        first_address.address_id,
        "10",
        "rpc_a",
        "2026-09-01T00:00:00.000+00:00",
    )
    _record(
        store,
        subject_id,
        first_asset.asset_id,
        first_address.address_id,
        "11",
        "rpc_b",
        "2026-09-01T12:00:00.000+00:00",
    )
    _record(
        store,
        subject_id,
        second_asset.asset_id,
        second_address.address_id,
        "4",
        "rpc_a",
        "2026-09-01T18:00:00.000+00:00",
    )

    source_view = store.observation_health_by_source(subject_id, now=EVALUATED_AT)
    assert [group["value"] for group in source_view["groups"]] == ["rpc_a", "rpc_b"]
    rpc_a, rpc_b = source_view["groups"]
    assert rpc_a["active_pairs"] == rpc_a["observed_pairs"] == 1
    assert rpc_a["snapshot_count"] == 1
    assert rpc_a["latest_observed_at"] == "2026-09-01T18:00:00.000+00:00"
    assert rpc_b["active_pairs"] == rpc_b["observed_pairs"] == 1
    assert rpc_b["snapshot_count"] == 2
    assert rpc_b["latest_observed_at"] == "2026-09-01T12:00:00.000+00:00"
    assert source_view["status"] == "ok"

    network_view = store.observation_health_by_network(subject_id, now=EVALUATED_AT)
    assert {group["value"] for group in network_view["groups"]} == {
        first_network.network_id,
        second_network.network_id,
    }
    assert sum(group["snapshot_count"] for group in network_view["groups"]) == 3


def test_breakdown_filters_and_pagination_are_subject_scoped(
    breakdown_store: tuple[Database, str, WalletStore],
) -> None:
    database, subject_id, store = breakdown_store
    network_a = _network(store, subject_id, "A", 7004)
    asset_a, address_a = _pair(store, subject_id, network_a.network_id, 4)
    network_b = _network(store, subject_id, "B", 7005)
    asset_b, address_b = _pair(store, subject_id, network_b.network_id, 5)
    _record(
        store,
        subject_id,
        asset_a.asset_id,
        address_a.address_id,
        "1",
        "rpc_a",
        "2026-09-01T00:00:00.000+00:00",
    )
    _record(
        store,
        subject_id,
        asset_b.asset_id,
        address_b.address_id,
        "1",
        "rpc_b",
        "2026-09-01T00:00:00.000+00:00",
    )

    page = store.observation_health_breakdown(subject_id, limit=1, now=EVALUATED_AT)
    assert len(page["groups"]) == 1
    assert page["total_groups"] == 2
    assert page["has_more"] is True

    filtered = store.observation_health_breakdown(
        subject_id,
        group_by="network",
        network_id=network_a.network_id,
        source="rpc_a",
        now=EVALUATED_AT,
    )
    assert filtered["total_groups"] == 1
    assert filtered["groups"][0]["value"] == network_a.network_id
    assert filtered["groups"][0]["observed_pairs"] == 1

    source_filtered = store.observation_health_by_source(
        subject_id, source="rpc_b", now=EVALUATED_AT
    )
    assert [group["value"] for group in source_filtered["groups"]] == ["rpc_b"]

    other_subject = "Noyra-wallet-breakdown-other"
    IdentityStore(database).ensure(other_subject, content_hash({"subject": other_subject}))
    other_network = _network(store, other_subject, "Other", 7006)
    with pytest.raises(NotFoundError):
        store.observation_health_breakdown(subject_id, network_id=other_network.network_id)

    for kwargs in (
        {"group_by": "asset"},
        {"source": "RPC"},
        {"source": "rpc-a"},
        {"limit": 0},
        {"limit": WALLET_BALANCE_OBSERVATION_MAX_GROUPS + 1},
        {"network_id": ""},
    ):
        with pytest.raises((ValueError, TypeError)):
            store.observation_health_breakdown(subject_id, **kwargs)


def test_breakdown_active_pair_cap_fails_closed(
    breakdown_store: tuple[Database, str, WalletStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, subject_id, store = breakdown_store
    network = _network(store, subject_id, "Cap", 7008)
    _pair(store, subject_id, network.network_id, 8)
    second_network = _network(store, subject_id, "Cap Two", 7009)
    _pair(store, subject_id, second_network.network_id, 9)
    monkeypatch.setattr("noyra.wallet.store.WALLET_BALANCE_OBSERVATION_MAX_ACTIVE_PAIRS", 1)
    with pytest.raises(IntegrityError, match="active pair limit"):
        store.observation_health_breakdown(subject_id, now=EVALUATED_AT)


def test_breakdown_rejects_malformed_projection_and_keeps_source_order() -> None:
    from noyra.service import _wallet_observation_health_breakdown

    base_group = {
        "value": "rpc_a",
        "active_pairs": 1,
        "observed_pairs": 1,
        "fresh_pairs": 1,
        "near_expiry_pairs": 0,
        "stale_pairs": 0,
        "never_pairs": 0,
        "future_pairs": 0,
        "anomalous_pairs": 0,
        "snapshot_count": 1,
        "latest_observed_at": "2026-09-02T00:00:00.000+00:00",
        "max_age_seconds": 0,
        "status": "ok",
    }
    payload = {
        "group_by": "source",
        "evaluated_at": EVALUATED_AT,
        "stale_after_seconds": 86400,
        "status": "ok",
        "groups": [base_group],
        "total_groups": 1,
        "has_more": False,
    }
    assert _wallet_observation_health_breakdown(payload)["groups"] == [base_group]
    invalid = {**payload, "groups": [{**base_group, "value": None}, base_group], "total_groups": 2}
    with pytest.raises(ValueError, match="null group must be last"):
        _wallet_observation_health_breakdown(invalid)
    invalid_source = {**payload, "groups": [{**base_group, "value": "RPC"}]}
    with pytest.raises(ValueError, match="source value"):
        _wallet_observation_health_breakdown(invalid_source)
    network_payload = {
        **payload,
        "group_by": "network",
        "groups": [{**base_group, "value": "Network A"}],
    }
    assert (
        _wallet_observation_health_breakdown(network_payload)["groups"][0]["value"] == "Network A"
    )


@pytest.fixture
def breakdown_http(tmp_path: Path) -> Iterator[tuple[NoyraHTTPServer, str, str]]:
    token = "wallet-breakdown-http-token-with-sufficient-entropy"
    settings = ServiceSettings(
        data_dir=tmp_path / "data",
        subject_id="Noyra-wallet-breakdown-http",
        genesis_hash=content_hash({"seed": "wallet-breakdown-http"}),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr(token),
        active_interval_seconds=1,
        sleep_interval_seconds=1,
        error_backoff_seconds=1,
    )
    kernel = SubjectKernel(
        settings.data_dir / "noyra.sqlite3", settings.subject_id, settings.genesis_hash
    )
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    server.start()
    _host, port = server.address
    try:
        yield server, f"http://127.0.0.1:{port}", token
    finally:
        server.close()
        kernel.close()


def _http_get(base_url: str, path: str, token: str | None = None) -> tuple[int, Any]:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    request = Request(base_url + path, headers=headers)
    with urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def test_breakdown_http_route_error_matrix_and_redaction(
    breakdown_http: tuple[NoyraHTTPServer, str, str],
) -> None:
    server, base_url, token = breakdown_http
    with pytest.raises(HTTPError) as unauthorized:
        _http_get(base_url, "/api/v1/config/wallet-observation-health")
    assert unauthorized.value.code == 401

    network = _network(server.wallets, server.kernel.subject_id, "HTTP", 7007)
    asset, address = _pair(server.wallets, server.kernel.subject_id, network.network_id, 7)
    _record(
        server.wallets,
        server.kernel.subject_id,
        asset.asset_id,
        address.address_id,
        "123",
        "rpc_http",
        "2026-09-01T00:00:00.000+00:00",
    )

    status, payload = _http_get(
        base_url, "/api/v1/config/wallet-observation-health?group_by=source&limit=1", token
    )
    assert status == 200
    assert payload["group_by"] == "source"
    assert payload["groups"][0]["value"] == "rpc_http"
    serialized = json.dumps(payload, sort_keys=True)
    assert "123" not in serialized
    assert network.network_id not in serialized
    assert asset.asset_id not in serialized
    assert address.address_id not in serialized

    for path, code in (
        ("/api/v1/config/wallet-observation-health?group_by=asset", 400),
        ("/api/v1/config/wallet-observation-health?source=RPC", 400),
        ("/api/v1/config/wallet-observation-health?source=", 400),
        ("/api/v1/config/wallet-observation-health?group_by=", 400),
        ("/api/v1/config/wallet-observation-health?network_id=missing", 404),
    ):
        with pytest.raises(HTTPError) as error:
            _http_get(base_url, path, token)
        assert error.value.code == code
