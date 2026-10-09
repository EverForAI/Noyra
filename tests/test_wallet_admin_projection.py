from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, new_id
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.wallet import (
    WalletAddressInput,
    WalletAssetInput,
    WalletBalanceAcquisitionLedger,
    WalletBalanceSnapshotInput,
    WalletNetworkInput,
    WalletStore,
)
from noyra.wallet.admin_projection import WalletAdminProjection


def test_pages_exclude_concurrent_insert_and_bind_filters(tmp_path: Path) -> None:
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", "wallet-pages", content_hash("pages"))
    try:
        store = WalletStore(kernel.database)
        original = [
            store.register_network(
                kernel.subject_id,
                WalletNetworkInput(
                    label=f"Network {i}",
                    chain_id=i + 1,
                    native_symbol="ETH",
                    rpc_url="https://rpc.example",
                ),
                actor="operator",
            )
            for i in range(7)
        ]
        projection = WalletAdminProjection(kernel.database)
        page = projection.page(kernel.subject_id, "networks", limit=2, status="active")
        items = page["items"][:]
        store.register_network(
            kernel.subject_id,
            WalletNetworkInput(
                label="Late",
                chain_id=99,
                native_symbol="ETH",
            ),
            actor="operator",
        )
        assert page["total"] == 7
        assert "private-path" not in json.dumps(page)
        with pytest.raises(ValueError, match="scope"):
            projection.page(kernel.subject_id, "networks", cursor=page["next_cursor"])
        while page["has_more"]:
            page = projection.page(
                kernel.subject_id, "networks", limit=2, status="active", cursor=page["next_cursor"]
            )
            items.extend(page["items"])
        assert {row["network_id"] for row in items} == {row.network_id for row in original}
        assert len(items) == 7
        assert page["next_cursor"] is None
        store.revoke_network(
            original[0].network_id, reason="retire", actor="operator", subject_id=kernel.subject_id
        )
        assert projection.page(kernel.subject_id, "networks", status="revoked")["total"] == 1
        for kwargs in (
            {"limit": True},
            {"limit": 0},
            {"status": "bogus"},
            {"network_id": "not-applicable"},
            {"cursor": "%%%"},
        ):
            with pytest.raises(ValueError):
                projection.page(kernel.subject_id, "networks", **kwargs)
    finally:
        kernel.close()


def test_wallet_audits_are_scoped_and_projected(tmp_path: Path) -> None:
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", "wallet-audit-pages", content_hash("pages"))
    try:
        with kernel.database.transaction() as connection:
            for subject, action in (
                (kernel.subject_id, "wallet_payment_failed"),
                (kernel.subject_id, "not_wallet"),
            ):
                connection.execute(
                    "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
                    (
                        new_id("audit"),
                        subject,
                        action,
                        "operator",
                        canonical_json({"amount": "900719925474099312345", "private": "hidden"}),
                        "2026-10-10T00:00:00Z",
                    ),
                )
        page = WalletAdminProjection(kernel.database).page(kernel.subject_id, "audits")
        assert page["total"] == 1
        assert page["items"][0]["details"]["amount"] == "900719925474099312345"
        assert "hidden" not in json.dumps(page)
        with kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
                (
                    new_id("audit"),
                    kernel.subject_id,
                    "wallet_payment_failed",
                    "operator",
                    "[]",
                    "2026-10-10T00:00:01Z",
                ),
            )
        with pytest.raises(IntegrityError):
            WalletAdminProjection(kernel.database).page(kernel.subject_id, "audits")
    finally:
        kernel.close()


def test_registry_and_acquisition_pages_verify_records_and_exclude_leases(tmp_path: Path) -> None:
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", "wallet-record-pages", content_hash("pages"))
    try:
        store = WalletStore(kernel.database)
        network = store.register_network(
            kernel.subject_id,
            WalletNetworkInput(
                label="Test network",
                chain_id=1,
                native_symbol="ETH",
            ),
            actor="operator",
        )
        asset = store.register_asset(
            kernel.subject_id,
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
            kernel.subject_id,
            WalletAddressInput(
                network_id=network.network_id,
                label="Observe",
                address="0x" + "a" * 40,
                purpose="observation",
            ),
            actor="operator",
        )
        ledger = WalletBalanceAcquisitionLedger(kernel.database)
        run = ledger.enqueue(
            kernel.subject_id,
            asset_id=asset.asset_id,
            address_id=address.address_id,
            actor="operator",
        )
        projection = WalletAdminProjection(kernel.database)
        for kind in ("assets", "addresses", "acquisitions"):
            page = projection.page(kernel.subject_id, kind, network_id=network.network_id)
            assert page["total"] == 1
        for index in range(3):
            store.record_balance_snapshot(
                kernel.subject_id,
                WalletBalanceSnapshotInput(
                    asset_id=asset.asset_id,
                    address_id=address.address_id,
                    balance=str(index),
                    observed_at=f"2026-10-09T00:00:0{index}Z",
                ),
                actor="operator",
            )
        other_address = store.register_address(
            kernel.subject_id,
            WalletAddressInput(
                network_id=network.network_id,
                label="Other",
                address="0x" + "b" * 40,
            ),
            actor="operator",
        )
        store.record_balance_snapshot(
            kernel.subject_id,
            WalletBalanceSnapshotInput(
                asset_id=asset.asset_id,
                address_id=other_address.address_id,
                balance="0",
                observed_at="2026-10-09T00:00:03Z",
            ),
            actor="operator",
        )
        first = projection.page(kernel.subject_id, "balances", limit=1)
        assert first["total"] == 2
        assert first["items"][0]["balance"] == "0"
        store.record_balance_snapshot(
            kernel.subject_id,
            WalletBalanceSnapshotInput(
                asset_id=asset.asset_id,
                address_id=address.address_id,
                balance="99",
                observed_at="2026-10-09T00:00:04Z",
            ),
            actor="operator",
        )
        next_page = projection.page(
            kernel.subject_id, "balances", limit=1, cursor=first["next_cursor"]
        )
        assert next_page["items"][0]["balance"] == "2"
        assert not next_page["has_more"]
        assert projection.page(kernel.subject_id, "balances")["items"][0]["balance"] == "99"
        page = projection.page(kernel.subject_id, "acquisitions", status="queued")
        assert page["items"][0]["run_id"] == run.run_id
        assert "claim_token" not in page["items"][0]
        assert "lease_owner" not in page["items"][0]
        with kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE wallet_balance_acquisition_runs SET state_hash=? WHERE run_id=?",
                ("0" * 64, run.run_id),
            )
        with pytest.raises(IntegrityError):
            projection.page(kernel.subject_id, "acquisitions")
    finally:
        kernel.close()


def test_wallet_page_http_auth_validation_and_read_only(tmp_path: Path) -> None:
    token = "synthetic-wallet-pages-operator-token"
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="wallet-list-http",
        genesis_hash=content_hash("pages"),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr(token),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    server = NoyraHTTPServer(kernel, settings)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.address[1]}"
        with pytest.raises(HTTPError) as denied:
            urlopen(base + "/api/v1/admin/wallet-lists/networks", timeout=10)
        assert denied.value.code == 401
        with kernel.database.connection() as connection:
            before = connection.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0]
        for kind in WalletAdminProjection.SOURCES:
            with urlopen(
                Request(
                    base + f"/api/admin/wallet-lists/{kind}",
                    headers={"Authorization": f"Bearer {token}"},
                ),
                timeout=10,
            ) as reply:
                assert reply.status == 200
                assert json.loads(reply.read())["items"] == []
        for query in ("limit=0", "limit=1&limit=2", "status=", "bad=1", "cursor=", "limit=-1"):
            with pytest.raises(HTTPError) as invalid:
                urlopen(
                    Request(
                        base + "/api/v1/admin/wallet-lists/networks?" + query,
                        headers={"Authorization": f"Bearer {token}"},
                    ),
                    timeout=10,
                )
            assert invalid.value.code == 400
        with kernel.database.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0] == before
    finally:
        server.close()
        kernel.close()
