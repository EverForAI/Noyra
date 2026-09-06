from __future__ import annotations

import json
import sqlite3
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr, ValidationError

import noyra.core.database as database_module
from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core.database import CURRENT_SCHEMA_VERSION, wallet_submission_state_hash
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.integrity import IntegrityRegistry
from noyra.core.runtime_export import RuntimeExportArtifact, RuntimeLogExporter
from noyra.core.types import content_hash, utc_now
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.wallet import (
    SQLITE_INT64_MAX,
    BountyInput,
    MockSigner,
    PaymentPolicyInput,
    SubmissionInput,
    WalletAddressInput,
    WalletAddressRecord,
    WalletAssetInput,
    WalletAssetRecord,
    WalletBalanceSnapshotInput,
    WalletEconomyStore,
    WalletNetworkInput,
    WalletNetworkRecord,
    WalletStore,
    canonical_balance,
    canonical_evm_address,
)

WalletFixture = tuple[Database, str, WalletStore]


@pytest.fixture
def wallet_store(tmp_path: Path) -> WalletFixture:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-wallet-test"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    return database, subject_id, WalletStore(database)


def _network_input(
    *,
    label: str = "Ethereum Mainnet",
    chain_id: int = 1,
    native_symbol: str = "ETH",
) -> WalletNetworkInput:
    return WalletNetworkInput(
        label=label,
        chain_id=chain_id,
        native_symbol=native_symbol,
        rpc_url="https://rpc.example",
    )


def _register_graph(
    store: WalletStore,
    subject_id: str,
) -> tuple[WalletNetworkRecord, WalletAssetRecord, WalletAddressRecord]:
    network = store.register_network(subject_id, _network_input(), actor="operator")
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
    return network, asset, address


def _read_export(
    artifact: RuntimeExportArtifact,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert isinstance(manifest, dict)
        rows: dict[str, list[dict[str, Any]]] = {}
        for entry in manifest["tables"]:
            assert isinstance(entry, dict)
            name = entry["name"]
            filename = entry["file"]
            assert isinstance(name, str)
            assert isinstance(filename, str)
            rows[name] = [
                parsed
                for line in archive.read(filename).splitlines()
                if isinstance(parsed := json.loads(line), dict)
            ]
    return manifest, rows


def _http_json(
    base_url: str,
    path: str,
    *,
    payload: Mapping[str, object] | None = None,
    token: str | None = None,
) -> tuple[int, object]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{base_url}{path}",
        data=None if payload is None else json.dumps(dict(payload)).encode(),
        method="POST" if payload is not None else "GET",
        headers=headers,
    )
    with urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def test_schema_56_installs_wallet_tables_triggers_and_lifecycle_constraint(
    wallet_store: WalletFixture,
) -> None:
    database, _subject_id, _store = wallet_store
    expected_tables = {
        "wallet_networks",
        "wallet_network_revisions",
        "wallet_assets",
        "wallet_asset_revisions",
        "wallet_addresses",
        "wallet_address_revisions",
        "wallet_balance_snapshots",
    }
    expected_triggers = {
        "validate_wallet_network_creation_audit",
        "validate_wallet_network_transition",
        "validate_wallet_asset_creation_audit",
        "validate_wallet_asset_transition",
        "validate_wallet_address_creation_audit",
        "validate_wallet_address_transition",
        "validate_wallet_balance_snapshot_insert",
    }

    with database.connection() as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert version is not None
        assert version["value"] == str(CURRENT_SCHEMA_VERSION)
        names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        trigger_names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        index_names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        network_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'wallet_networks'"
        ).fetchone()
        assert network_sql_row is not None
        network_sql = str(network_sql_row["sql"])

    assert expected_tables <= names
    assert expected_triggers <= trigger_names
    assert "idx_wallet_balance_snapshots_subject_history" in index_names
    assert "revoke_reason IS NOT NULL" in network_sql


def test_wallet_migration_is_idempotent_after_an_interrupted_schema_54_attempt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "migration.sqlite3"
    database = Database(path)
    with database.transaction() as connection:
        connection.execute("UPDATE schema_meta SET value = '52' WHERE key = 'schema_version'")

    migrated = Database(path)
    with migrated.connection() as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        trigger = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'trigger' AND name = 'validate_wallet_network_transition'"
        ).fetchone()
        assert version is not None and version["value"] == str(CURRENT_SCHEMA_VERSION)
    assert trigger is not None


def test_schema_58_expands_reward_incident_kinds_and_reinstalls_guards(tmp_path: Path) -> None:
    path = tmp_path / "reward-incident-migration.sqlite3"
    database = Database(path)
    with database.transaction() as connection:
        connection.execute("UPDATE schema_meta SET value = '57' WHERE key = 'schema_version'")

    migrated = Database(path)
    with migrated.connection() as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='wallet_reward_incidents'"
        ).fetchone()["sql"]
        delete_trigger = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND name='prevent_wallet_reward_incident_delete'"
        ).fetchone()

    assert version == str(CURRENT_SCHEMA_VERSION)
    assert "signer_rejection" in table_sql
    assert "broadcast_unknown" in table_sql
    assert "receipt_chain_unknown" in table_sql
    assert delete_trigger is not None


def test_schema_59_adds_submission_consent_and_rehashes_legacy_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "submission-consent-migration.sqlite3"
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 58)
    legacy = Database(path)
    subject_id = "Noyra-wallet-consent-migration"
    IdentityStore(legacy).ensure(subject_id, content_hash({"subject": subject_id}))
    wallets = WalletStore(legacy)
    network, asset, _address = _register_graph(wallets, subject_id)
    now = utc_now()
    with legacy.transaction() as connection:
        connection.execute(
            "INSERT INTO goals(goal_id,subject_id,title,description,origin,status,priority,"
            "commitment,progress,emotional_pressure,state_hash,current_revision,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "goal_consent_migration",
                subject_id,
                "Consent migration",
                "Consent migration",
                "self",
                "active",
                1,
                1,
                0,
                0,
                "legacy-goal-hash",
                1,
                now,
                now,
            ),
        )
    economy = WalletEconomyStore(legacy)
    current = datetime.now(UTC)
    bounty = economy.create_bounty(
        subject_id,
        BountyInput(
            title="Legacy bounty",
            description="Legacy submission consent migration",
            acceptance_criteria=["proof"],
            network_id=network.network_id,
            asset_id=asset.asset_id,
            reward_amount="1",
            opens_at=(current - timedelta(minutes=1)).isoformat(),
            expires_at=(current + timedelta(days=1)).isoformat(),
            max_submissions=1,
            reward_slots=1,
            goal_id="goal_consent_migration",
            idempotency_key="legacy-consent-bounty",
        ),
        actor="operator",
    )
    economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    submission_id = "submission_legacy_consent"
    evidence_json = "[]"
    legacy_hash = content_hash(
        {
            "submission_id": submission_id,
            "bounty_id": bounty.bounty_id,
            "subject_id": subject_id,
            "counterparty": "legacy-human",
            "content": "legacy proof",
            "evidence_json": evidence_json,
            "recipient_address": "0xb111111111111111111111111111111111111111",
            "idempotency_key": "legacy-consent-submission",
            "status": "submitted",
            "decision_reason": None,
        }
    )
    with legacy.transaction() as connection:
        audit_id = economy._audit(
            connection,
            subject_id,
            "wallet_bounty_submission_created",
            "visitor",
            {"submission_id": submission_id},
        )
        connection.execute(
            "INSERT INTO wallet_bounty_submissions(submission_id,bounty_id,subject_id,"
            "counterparty,content,evidence_json,recipient_address,idempotency_key,status,"
            "decision_reason,created_at,decided_at,state_hash,created_audit_id) "
            "VALUES(?,?,?,?,?,?,?,?,'submitted',NULL,?,NULL,?,?)",
            (
                submission_id,
                bounty.bounty_id,
                subject_id,
                "legacy-human",
                "legacy proof",
                evidence_json,
                "0xb111111111111111111111111111111111111111",
                "legacy-consent-submission",
                now,
                legacy_hash,
                audit_id,
            ),
        )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(wallet_bounty_submissions)")
        }
    assert "consent_version" not in columns

    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 59)
    migrated = Database(path)
    with migrated.connection() as connection:
        row = connection.execute(
            "SELECT * FROM wallet_bounty_submissions WHERE submission_id=?",
            (submission_id,),
        ).fetchone()
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='prevent_wallet_submission_identity_update'"
        ).fetchone()[0]
    assert version == "59"
    assert row["consent_version"] == 1
    assert row["state_hash"] == wallet_submission_state_hash(
        submission_id=submission_id,
        bounty_id=bounty.bounty_id,
        subject_id=subject_id,
        counterparty="legacy-human",
        content="legacy proof",
        evidence_json=evidence_json,
        recipient_address="0xb111111111111111111111111111111111111111",
        idempotency_key="legacy-consent-submission",
        consent_version=1,
        status="submitted",
        decision_reason=None,
    )
    assert row["state_hash"] != legacy_hash
    assert "consent_version" in trigger_sql
    with pytest.raises(sqlite3.IntegrityError), migrated.transaction() as connection:
        connection.execute(
            "UPDATE wallet_bounty_submissions SET consent_version=2 WHERE submission_id=?",
            (submission_id,),
        )


def test_schema_60_refuses_unprovable_legacy_policy_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "policy-hash-migration.sqlite3"
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 59)
    legacy = Database(path)
    subject_id = "Noyra-policy-hash-migration"
    IdentityStore(legacy).ensure(subject_id, content_hash({"subject": subject_id}))
    with legacy.connection() as connection:
        row = connection.execute(
            "SELECT * FROM wallet_payment_policies WHERE subject_id=?", (subject_id,)
        ).fetchone()
    assert row is not None
    legacy_hash = content_hash(
        {
            "subject_id": subject_id,
            "version": int(row["policy_version"]),
            "mode": str(row["mode"]),
            "updated_at": str(row["updated_at"]),
        }
    )
    with legacy.transaction() as connection:
        connection.execute(
            "UPDATE wallet_payment_policies SET daily_limit=?,state_hash=? WHERE subject_id=?",
            ("999", legacy_hash, subject_id),
        )

    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 60)
    with pytest.raises(RuntimeError, match="cannot be upgraded safely"):
        Database(path)
    with sqlite3.connect(path) as connection:
        marker = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        policy = connection.execute(
            "SELECT daily_limit,state_hash FROM wallet_payment_policies WHERE subject_id=?",
            (subject_id,),
        ).fetchone()
    assert marker == ("59",)
    assert policy == ("999", legacy_hash)


def test_schema_60_upgrades_default_bootstrap_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "policy-bootstrap-migration.sqlite3"
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 59)
    legacy = Database(path)
    subject_id = "Noyra-policy-bootstrap-migration"
    IdentityStore(legacy).ensure(subject_id, content_hash({"subject": subject_id}))
    with legacy.transaction() as connection:
        connection.execute(
            "UPDATE wallet_payment_policies SET state_hash='bootstrap' WHERE subject_id=?",
            (subject_id,),
        )
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 60)
    Database(path)
    with sqlite3.connect(path) as connection:
        marker = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        policy = connection.execute(
            "SELECT state_hash FROM wallet_payment_policies WHERE subject_id=?", (subject_id,)
        ).fetchone()
    assert marker == ("60",)
    assert policy is not None and policy[0] != "bootstrap"


def test_input_boundaries_canonicalize_public_values_and_reject_unsafe_values() -> None:
    assert canonical_evm_address(" 0xA111111111111111111111111111111111111111 ") == (
        "0xa111111111111111111111111111111111111111"
    )
    assert canonical_balance("9" * 256) == "9" * 256
    for value in ("-1", "01", "", "1.0"):
        with pytest.raises(ValueError):
            canonical_balance(value)
    with pytest.raises(ValueError):
        canonical_evm_address("0x1111")
    with pytest.raises(ValidationError):
        WalletNetworkInput(
            label="insecure",
            chain_id=1,
            native_symbol="ETH",
            rpc_url="http://rpc.example",
        )
    with pytest.raises(ValidationError):
        WalletNetworkInput(
            label="credential-bearing",
            chain_id=1,
            native_symbol="ETH",
            rpc_url="https://token@rpc.example",
        )
    assert _network_input(chain_id=SQLITE_INT64_MAX).chain_id == SQLITE_INT64_MAX
    with pytest.raises(ValidationError):
        _network_input(chain_id=SQLITE_INT64_MAX + 1)
    with pytest.raises(ValidationError):
        WalletAssetInput(
            network_id="walletnet_test",
            asset_type="token",
            name="Token",
            symbol="TKN",
            decimals=18,
        )
    with pytest.raises(ValidationError):
        WalletAssetInput(
            network_id="walletnet_test",
            asset_type="native",
            contract_address="0xA111111111111111111111111111111111111111",
            name="Ether",
            symbol="ETH",
            decimals=18,
        )


def test_wallet_registration_balance_history_and_operator_boundary(
    wallet_store: WalletFixture,
) -> None:
    _database, subject_id, store = wallet_store
    with pytest.raises(PermissionError):
        store.register_network(subject_id, _network_input(), actor=" subject ")

    network, native_asset, address = _register_graph(store, subject_id)
    token = store.register_asset(
        subject_id,
        WalletAssetInput(
            network_id=network.network_id,
            asset_type="token",
            contract_address="0xB222222222222222222222222222222222222222",
            name="US Dollar Coin",
            symbol="usdc",
            decimals=6,
        ),
        actor="operator",
    )
    first = store.record_balance_snapshot(
        subject_id,
        WalletBalanceSnapshotInput(
            asset_id=native_asset.asset_id,
            address_id=address.address_id,
            balance="9" * 256,
            source="operator_observation",
            observed_at="2026-08-30T08:00:00+08:00",
        ),
        actor="operator",
    )
    second = store.record_balance_snapshot(
        subject_id,
        WalletBalanceSnapshotInput(
            asset_id=native_asset.asset_id,
            address_id=address.address_id,
            balance="42",
            source="operator_observation",
            observed_at="2026-08-30T00:01:00Z",
        ),
        actor="operator",
    )

    assert address.address == "0xa111111111111111111111111111111111111111"
    assert token.contract_address == "0xb222222222222222222222222222222222222222"
    assert token.symbol == "USDC"
    assert first.observed_at == "2026-08-30T00:00:00.000+00:00"
    assert second.observed_at == "2026-08-30T00:01:00.000+00:00"
    assert [record.snapshot_id for record in store.list_balance_snapshots(subject_id)] == [
        second.snapshot_id,
        first.snapshot_id,
    ]
    assert [record.balance for record in store.latest_balances(subject_id)] == ["42"]
    assert {record.asset_id for record in store.list_assets(subject_id)} == {
        native_asset.asset_id,
        token.asset_id,
    }
    assert store.verify_integrity(subject_id) == {
        "wallet_networks": 1,
        "wallet_network_revisions": 1,
        "wallet_assets": 2,
        "wallet_asset_revisions": 2,
        "wallet_addresses": 1,
        "wallet_address_revisions": 1,
        "wallet_balance_snapshots": 2,
    }


def test_wallet_balance_history_page_binds_filters_and_high_water_cursor(
    wallet_store: WalletFixture,
) -> None:
    database, subject_id, store = wallet_store
    _network, asset, address = _register_graph(store, subject_id)
    for index in range(5):
        store.record_balance_snapshot(
            subject_id,
            WalletBalanceSnapshotInput(
                asset_id=asset.asset_id,
                address_id=address.address_id,
                balance=str(index),
                observed_at=f"2026-08-30T00:0{index}:00Z",
            ),
            actor="operator",
        )

    baseline = store.list_balance_snapshots(subject_id)
    first = store.balance_history_page(subject_id, limit=2)
    assert len(first.items) == 2
    assert first.has_more is True
    assert first.next_cursor is not None

    # Newer and backdated writes are both outside the first page's high-water
    # mark and must not alter a traversal already in progress.
    for observed_at, balance in (
        ("2026-08-31T00:00:00Z", "100"),
        ("2026-08-30T00:00:30Z", "101"),
    ):
        store.record_balance_snapshot(
            subject_id,
            WalletBalanceSnapshotInput(
                asset_id=asset.asset_id,
                address_id=address.address_id,
                balance=balance,
                observed_at=observed_at,
            ),
            actor="operator",
        )

    replay = store.balance_history_page(
        subject_id,
        limit=2,
        cursor=first.next_cursor,
    )
    assert replay == store.balance_history_page(
        subject_id,
        limit=2,
        cursor=first.next_cursor,
    )
    walked = list(first.items)
    page = replay
    while page.next_cursor is not None:
        walked.extend(page.items)
        page = store.balance_history_page(subject_id, limit=2, cursor=page.next_cursor)
    walked.extend(page.items)
    assert [record.snapshot_id for record in walked] == [record.snapshot_id for record in baseline]

    with pytest.raises(ValueError, match="does not match"):
        store.balance_history_page(subject_id, cursor=first.next_cursor, asset_id="other-asset")
    other_subject = "Noyra-wallet-history-other"
    IdentityStore(database).ensure(other_subject, content_hash({"subject": other_subject}))
    with pytest.raises(ValueError, match="does not match"):
        store.balance_history_page(other_subject, cursor=first.next_cursor)

    for invalid in ("not-a-cursor", "=", "A" * 8_193):
        with pytest.raises(ValueError, match="cursor"):
            store.balance_history_page(subject_id, cursor=invalid)
    for invalid_limit in (0, 1_001, True):
        with pytest.raises(ValueError, match="page limit"):
            store.balance_history_page(subject_id, limit=invalid_limit)


def test_wallet_balance_history_http_route_is_bounded_and_operator_only(
    wallet_http: tuple[NoyraHTTPServer, str, str],
) -> None:
    server, base_url, token = wallet_http
    with pytest.raises(HTTPError) as unauthorized:
        _http_json(base_url, "/api/v1/config/wallet-balance-history")
    assert unauthorized.value.code == 401

    _network, asset, address = _register_graph(server.wallets, server.kernel.subject_id)
    for index in range(3):
        server.wallets.record_balance_snapshot(
            server.kernel.subject_id,
            WalletBalanceSnapshotInput(
                asset_id=asset.asset_id,
                address_id=address.address_id,
                balance=str(index),
                observed_at=f"2026-08-30T00:0{index}:00Z",
            ),
            actor="operator",
        )

    status, payload = _http_json(
        base_url,
        "/api/v1/config/wallet-balance-history?limit=2",
        token=token,
    )
    assert status == 200
    assert isinstance(payload, dict)
    assert payload["has_more"] is True
    assert len(payload["items"]) == 2
    cursor = payload["next_cursor"]
    assert isinstance(cursor, str) and cursor

    status, next_payload = _http_json(
        base_url,
        f"/api/v1/config/wallet-balance-history?limit=2&cursor={cursor}",
        token=token,
    )
    assert status == 200
    assert isinstance(next_payload, dict)
    assert next_payload["has_more"] is False
    assert len(next_payload["items"]) == 1

    for query in (
        "?limit=0",
        "?limit=1001",
        "?limit=",
        "?limit=2&limit=",
        "?limit=2&offset=1",
        "?unknown=",
        "?asset_id=",
        "?cursor=",
        "?limit=2&cursor=not-a-cursor",
        f"?limit=2&cursor={cursor}&asset_id=other-asset",
    ):
        with pytest.raises(HTTPError) as invalid:
            _http_json(base_url, f"/api/v1/config/wallet-balance-history{query}", token=token)
        assert invalid.value.code == 400


def test_wallet_rejects_duplicate_or_cross_subject_registrations(
    wallet_store: WalletFixture,
) -> None:
    database, subject_id, store = wallet_store
    network, native_asset, address = _register_graph(store, subject_id)
    other_subject_id = "Noyra-wallet-other"
    IdentityStore(database).ensure(other_subject_id, content_hash({"subject": other_subject_id}))

    with pytest.raises(ValueError, match="already registered"):
        store.register_network(subject_id, _network_input(label="Another label"), actor="operator")
    with pytest.raises(ValueError, match="must match"):
        store.register_asset(
            subject_id,
            WalletAssetInput(
                network_id=network.network_id,
                asset_type="native",
                name="Not Ether",
                symbol="NOPE",
                decimals=18,
            ),
            actor="operator",
        )
    with pytest.raises(ValueError, match="already registered"):
        store.register_address(
            subject_id,
            WalletAddressInput(
                network_id=network.network_id,
                label="Another address label",
                address=address.address,
            ),
            actor="operator",
        )
    with pytest.raises(NotFoundError):
        store.get_network(network.network_id, subject_id=other_subject_id)
    with pytest.raises(NotFoundError):
        store.get_asset(native_asset.asset_id, subject_id=other_subject_id)
    with pytest.raises(NotFoundError):
        store.register_asset(
            other_subject_id,
            WalletAssetInput(
                network_id=network.network_id,
                asset_type="native",
                name="Ether",
                symbol="ETH",
                decimals=18,
            ),
            actor="operator",
        )


def test_wallet_revocation_audits_revisions_and_history_are_append_only(
    wallet_store: WalletFixture,
) -> None:
    database, subject_id, store = wallet_store
    network, asset, address = _register_graph(store, subject_id)
    snapshot = store.record_balance_snapshot(
        subject_id,
        WalletBalanceSnapshotInput(
            asset_id=asset.asset_id,
            address_id=address.address_id,
            balance="10",
        ),
        actor="operator",
    )

    with pytest.raises(ValueError, match="active asset or address"):
        store.revoke_network(
            network.network_id,
            reason="retire network",
            actor="operator",
            subject_id=subject_id,
        )
    assert (
        store.revoke_asset(
            asset.asset_id,
            reason="retire asset",
            actor="operator",
            subject_id=subject_id,
        ).status
        == "revoked"
    )
    assert (
        store.revoke_address(
            address.address_id,
            reason="retire address",
            actor="operator",
            subject_id=subject_id,
        ).status
        == "revoked"
    )
    revoked_network = store.revoke_network(
        network.network_id,
        reason="retire network",
        actor="operator",
        subject_id=subject_id,
    )
    assert revoked_network.status == "revoked"
    assert (
        store.revoke_network(
            network.network_id,
            reason="ignored on idempotent replay",
            actor="operator",
            subject_id=subject_id,
        ).revoked_at
        == revoked_network.revoked_at
    )

    with database.connection() as connection:
        actions = {
            row["action"]
            for row in connection.execute(
                "SELECT action FROM audit_records WHERE subject_id = ? AND action LIKE 'wallet_%'",
                (subject_id,),
            ).fetchall()
        }
        revision_counts = {
            table: connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in (
                "wallet_network_revisions",
                "wallet_asset_revisions",
                "wallet_address_revisions",
            )
        }
    assert actions == {
        "wallet_network_registered",
        "wallet_asset_registered",
        "wallet_address_registered",
        "wallet_network_revoked",
        "wallet_asset_revoked",
        "wallet_address_revoked",
    }
    assert revision_counts == {
        "wallet_network_revisions": 2,
        "wallet_asset_revisions": 2,
        "wallet_address_revisions": 2,
    }
    with database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE wallet_network_revisions SET reason = 'altered' WHERE network_id = ?",
                (network.network_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM wallet_balance_snapshots WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            )
    assert store.list_networks(subject_id, include_revoked=False) == []
    assert store.verify_integrity(subject_id)["wallet_balance_snapshots"] == 1


def test_wallet_integrity_registry_detects_audit_and_state_tampering(
    tmp_path: Path,
    wallet_store: WalletFixture,
) -> None:
    database, subject_id, store = wallet_store
    network, _asset, _address = _register_graph(store, subject_id)
    registry = IntegrityRegistry()
    healthy = registry.run(
        database,
        subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("wallet.state",),
    )
    assert healthy.status == "ok"
    assert [(check.id, check.status) for check in healthy.checks] == [("wallet.state", "ok")]

    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_audit_record_update")
        connection.execute(
            "UPDATE audit_records SET actor = 'subject' "
            "WHERE audit_id = (SELECT created_audit_id FROM wallet_networks WHERE network_id = ?)",
            (network.network_id,),
        )
    with pytest.raises(IntegrityError, match="lifecycle audit is invalid"):
        store.verify_integrity(subject_id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE audit_records SET actor = 'operator' "
            "WHERE audit_id = (SELECT created_audit_id FROM wallet_networks WHERE network_id = ?)",
            (network.network_id,),
        )
        connection.execute("DROP TRIGGER validate_wallet_network_transition")
        connection.execute(
            "UPDATE wallet_networks SET state_hash = ? WHERE network_id = ?",
            ("0" * 64, network.network_id),
        )
    damaged = registry.run(
        database,
        subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("wallet.state",),
    )
    assert damaged.status == "corrupt"
    assert [(check.id, check.status) for check in damaged.checks] == [("wallet.state", "corrupt")]


def test_wallet_integrity_consumes_history_cursors_without_fetchall(
    wallet_store: WalletFixture,
) -> None:
    database, subject_id, store = wallet_store
    _register_graph(store, subject_id)

    class NoFetchallCursor:
        def __init__(self, cursor: Any) -> None:
            self._cursor = cursor

        def __iter__(self) -> Iterator[Any]:
            return iter(self._cursor)

        def fetchone(self) -> Any:
            return self._cursor.fetchone()

        def fetchall(self) -> list[Any]:
            raise AssertionError("wallet integrity must consume cursors incrementally")

    class NoFetchallConnection:
        def __init__(self, connection: Any) -> None:
            self._connection = connection

        def execute(self, *args: Any, **kwargs: Any) -> NoFetchallCursor:
            return NoFetchallCursor(self._connection.execute(*args, **kwargs))

    class NoFetchallDatabase(Database):
        @contextmanager
        def read_transaction(self) -> Iterator[Any]:
            with super().read_transaction() as connection:
                yield NoFetchallConnection(connection)

    assert (
        WalletStore(NoFetchallDatabase(database.path, initialize=False)).verify_integrity(
            subject_id
        )["wallet_balance_snapshots"]
        == 0
    )


def test_wallet_status_summary_is_constant_size_and_redacts_public_state(
    wallet_store: WalletFixture,
) -> None:
    _database, subject_id, store = wallet_store
    network, asset, address = _register_graph(store, subject_id)
    snapshot = store.record_balance_snapshot(
        subject_id,
        WalletBalanceSnapshotInput(
            asset_id=asset.asset_id,
            address_id=address.address_id,
            balance="12345678901234567890",
            source="operator_observation",
        ),
        actor="operator",
    )

    summary = store.status_summary(subject_id)
    balance_history = summary["balance_history"]
    assert isinstance(balance_history, dict)
    observation_health = balance_history["observation_health"]
    assert isinstance(observation_health, dict)
    assert observation_health["status"] == "ok"
    assert observation_health["observed_pairs"] == 1
    assert observation_health["never_pairs"] == 0
    assert observation_health["anomalous_pairs"] == 0
    assert observation_health["max_age_seconds"] >= 0
    summary_without_health = {
        **summary,
        "balance_history": {
            key: value for key, value in balance_history.items() if key != "observation_health"
        },
    }
    assert summary_without_health == {
        "subject_id": subject_id,
        "resources": {
            "networks": {"active": 1, "revoked": 0, "total": 1},
            "assets": {"active": 1, "revoked": 0, "total": 1},
            "addresses": {"active": 1, "revoked": 0, "total": 1},
        },
        "balance_history": {
            "snapshots": 1,
            "latest_observed_at": snapshot.observed_at,
            "latest_created_at": snapshot.created_at,
        },
    }
    encoded = json.dumps(summary, sort_keys=True)
    for secret in (
        network.rpc_url,
        network.network_id,
        asset.asset_id,
        address.address_id,
        snapshot.balance,
    ):
        if secret is not None:
            assert secret not in encoded


def test_wallet_runtime_export_preserves_subject_ownership(wallet_store: WalletFixture) -> None:
    database, subject_a, store = wallet_store
    network_a, asset_a, address_a = _register_graph(store, subject_a)
    snapshot_a = store.record_balance_snapshot(
        subject_a,
        WalletBalanceSnapshotInput(
            asset_id=asset_a.asset_id,
            address_id=address_a.address_id,
            balance="7",
        ),
        actor="operator",
    )
    subject_b = "Noyra-wallet-export-other"
    IdentityStore(database).ensure(subject_b, content_hash({"subject": subject_b}))
    network_b, asset_b, address_b = _register_graph(store, subject_b)

    manifest, rows = _read_export(RuntimeLogExporter(database).export(subject_a, actor="test"))
    assert manifest["schema_version"] == CURRENT_SCHEMA_VERSION
    assert {row["network_id"] for row in rows["wallet_networks"]} == {network_a.network_id}
    assert {row["asset_id"] for row in rows["wallet_assets"]} == {asset_a.asset_id}
    assert {row["address_id"] for row in rows["wallet_addresses"]} == {address_a.address_id}
    assert {row["snapshot_id"] for row in rows["wallet_balance_snapshots"]} == {
        snapshot_a.snapshot_id
    }
    assert {row["network_id"] for row in rows["wallet_network_revisions"]} == {network_a.network_id}
    assert {row["asset_id"] for row in rows["wallet_asset_revisions"]} == {asset_a.asset_id}
    assert {row["address_id"] for row in rows["wallet_address_revisions"]} == {address_a.address_id}
    exported = json.dumps(rows, sort_keys=True)
    for identifier in (network_b.network_id, asset_b.asset_id, address_b.address_id):
        assert identifier not in exported


@pytest.fixture
def wallet_http(tmp_path: Path) -> Iterator[tuple[NoyraHTTPServer, str, str]]:
    token = "wallet-http-admin-token-with-sufficient-entropy"
    settings = ServiceSettings(
        data_dir=tmp_path / "data",
        subject_id="Noyra-wallet-http",
        genesis_hash=content_hash({"seed": "wallet-http"}),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr(token),
        active_interval_seconds=1,
        sleep_interval_seconds=1,
        error_backoff_seconds=1,
    )
    kernel = SubjectKernel(
        settings.data_dir / "noyra.sqlite3",
        settings.subject_id,
        settings.genesis_hash,
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


@pytest.mark.parametrize(
    ("method_name", "path", "payload"),
    [
        (
            "create",
            "/api/v1/admin/wallet-rewards",
            {
                "assistance_request_id": "request_http_boundary",
                "acceptance_criteria": ["proof"],
                "network_id": "walletnet_http_boundary",
                "asset_id": "walletasset_http_boundary",
                "reward_amount": "1",
                "opens_at": "2026-09-04T00:00:00+00:00",
                "expires_at": "2026-09-05T00:00:00+00:00",
                "max_submissions": 1,
                "reward_slots": 1,
                "idempotency_key": "reward-http-boundary",
            },
        ),
        (
            "submit_public",
            "/api/v1/wallet-rewards/reward_http_boundary/submissions",
            {
                "counterparty": "human",
                "content": "proof",
                "evidence": [],
                "recipient_address": "0xb111111111111111111111111111111111111111",
                "network_id": "walletnet_http_boundary",
                "idempotency_key": "submission-http-boundary",
                "consent_version": 1,
            },
        ),
        ("execute_ready", "/api/v1/admin/wallet-rewards/execute-ready", {"limit": 1}),
    ],
)
def test_wallet_reward_http_integrity_failures_are_service_unavailable(
    wallet_http: tuple[NoyraHTTPServer, str, str],
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    path: str,
    payload: Mapping[str, Any],
) -> None:
    server, base_url, token = wallet_http

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise IntegrityError("reward state unavailable")

    monkeypatch.setattr(server.wallet_rewards, method_name, unavailable)
    with pytest.raises(HTTPError) as response:
        _http_json(base_url, path, payload=payload, token=token)
    assert response.value.code == 503
    assert response.value.headers["Retry-After"] == "60"
    assert json.loads(response.value.read()) == {"error": "wallet_reward_integrity_unavailable"}


def test_wallet_execution_http_is_fail_closed_without_signer(
    wallet_http: tuple[NoyraHTTPServer, str, str],
) -> None:
    _server, base_url, token = wallet_http
    with pytest.raises(HTTPError) as unavailable:
        _http_json(base_url, "/api/v1/admin/wallet-executions", token=token)
    assert unavailable.value.code == 503


def test_wallet_execution_http_happy_path_with_isolated_signer(tmp_path: Path) -> None:
    token = "wallet-execution-http-admin-token-with-sufficient-entropy"
    settings = ServiceSettings(
        data_dir=tmp_path / "data",
        subject_id="Noyra-wallet-execution-http",
        genesis_hash=content_hash({"seed": "wallet-execution-http"}),
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
    source_address = "0xA111111111111111111111111111111111111111"
    signer = MockSigner(chain_id=1, source_address=source_address)
    server = NoyraHTTPServer(kernel, settings, wallet_signer=signer)
    server.start()
    _host, port = server.address
    base_url = f"http://127.0.0.1:{port}"
    try:
        network = server.wallets.register_network(
            settings.subject_id, _network_input(), actor="operator"
        )
        asset = server.wallets.register_asset(
            settings.subject_id,
            WalletAssetInput(
                network_id=network.network_id,
                asset_type="native",
                name="Ether",
                symbol="ETH",
                decimals=18,
            ),
            actor="operator",
        )
        server.wallets.register_address(
            settings.subject_id,
            WalletAddressInput(
                network_id=network.network_id,
                label="Spend",
                address=source_address,
                purpose="spending",
            ),
            actor="operator",
        )
        now = utc_now()
        with kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO goals("
                "goal_id,subject_id,title,description,origin,status,priority,commitment,"
                "progress,emotional_pressure,state_hash,current_revision,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "goal_wallet_execution_http",
                    settings.subject_id,
                    "Goal",
                    "Goal",
                    "self",
                    "active",
                    1,
                    1,
                    0,
                    0,
                    "x",
                    1,
                    now,
                    now,
                ),
            )
        server.wallet_economy.update_policy(
            settings.subject_id,
            PaymentPolicyInput(
                mode="automatic",
                allowed_network_ids=[network.network_id],
                allowed_asset_ids=[asset.asset_id],
                per_order_limit="20",
                automatic_max_amount="20",
            ),
            expected_version=1,
            actor="operator",
        )
        clock = datetime.now(UTC)
        bounty = server.wallet_economy.create_bounty(
            settings.subject_id,
            BountyInput(
                title="Help",
                description="Proof",
                acceptance_criteria=["proof"],
                network_id=network.network_id,
                asset_id=asset.asset_id,
                reward_amount="10",
                opens_at=(clock - timedelta(minutes=1)).isoformat(),
                expires_at=(clock + timedelta(days=1)).isoformat(),
                max_submissions=2,
                reward_slots=1,
                goal_id="goal_wallet_execution_http",
                idempotency_key="bounty-http-execution",
            ),
            actor="operator",
        )
        server.wallet_economy.publish_bounty(
            bounty.bounty_id, settings.subject_id, actor="operator"
        )
        submission = server.wallet_economy.submit(
            bounty.bounty_id,
            settings.subject_id,
            SubmissionInput(
                counterparty="human",
                content="done",
                recipient_address="0xB111111111111111111111111111111111111111",
                idempotency_key="submission-http-execution",
                consent_version=1,
            ),
        )
        server.wallet_economy.decide_submission(
            submission.submission_id,
            settings.subject_id,
            accepted=True,
            reason="verified",
            actor="operator",
        )
        order = server.wallet_economy.list_orders(settings.subject_id)[0]

        status, executed = _http_json(
            base_url,
            f"/api/v1/admin/wallet-orders/{order.order_id}/execute",
            payload={},
            token=token,
        )
        assert status == 200 and isinstance(executed, dict)
        assert executed["status"] == "broadcast"
        execution_id = str(executed["execution_id"])

        for path in (
            "/api/v1/admin/wallet-executions?limit=100",
            f"/api/v1/admin/wallet-executions/{execution_id}",
            f"/api/v1/admin/wallet-executions/{execution_id}/attempts",
        ):
            status, response = _http_json(base_url, path, token=token)
            assert status == 200 and response

        tx_hash = str(executed["tx_hash"])
        signer.set_receipt(tx_hash, chain_id=1, status=1, block_number=7)
        status, confirmed = _http_json(
            base_url,
            f"/api/v1/admin/wallet-executions/{execution_id}/receipt",
            payload={},
            token=token,
        )
        assert status == 200 and isinstance(confirmed, dict)
        assert confirmed["status"] == "confirmed"
        assert server.wallet_economy.list_orders(settings.subject_id)[0].status == "confirmed"
    finally:
        server.close()
        kernel.close()


def test_wallet_http_routes_are_operator_only_and_keep_balance_writes_internal(
    wallet_http: tuple[NoyraHTTPServer, str, str],
) -> None:
    server, base_url, token = wallet_http
    with pytest.raises(HTTPError) as unauthorized:
        _http_json(base_url, "/api/config/wallet-networks")
    assert unauthorized.value.code == 401

    status, created_network = _http_json(
        base_url,
        "/api/v1/config/wallet-networks",
        payload={
            "label": "Ethereum Mainnet",
            "chain_id": 1,
            "native_symbol": "ETH",
            "rpc_url": "https://rpc.example",
        },
        token=token,
    )
    assert status == 201
    assert isinstance(created_network, dict)
    network_id = str(created_network["network_id"])
    status, created_asset = _http_json(
        base_url,
        "/api/config/wallet-assets",
        payload={
            "network_id": network_id,
            "asset_type": "native",
            "name": "Ether",
            "symbol": "ETH",
            "decimals": 18,
        },
        token=token,
    )
    assert status == 201
    assert isinstance(created_asset, dict)
    asset_id = str(created_asset["asset_id"])
    status, created_address = _http_json(
        base_url,
        "/api/config/wallet-addresses",
        payload={
            "network_id": network_id,
            "label": "Treasury",
            "address": "0xA111111111111111111111111111111111111111",
            "purpose": "treasury",
        },
        token=token,
    )
    assert status == 201
    assert isinstance(created_address, dict)
    address_id = str(created_address["address_id"])

    server.wallets.record_balance_snapshot(
        server.kernel.subject_id,
        WalletBalanceSnapshotInput(
            asset_id=asset_id,
            address_id=address_id,
            balance="123456789012345678901234567890",
        ),
        actor="operator",
    )
    status, balances = _http_json(
        base_url,
        f"/api/v1/config/wallet-balances?network_id={network_id}",
        token=token,
    )
    assert status == 200
    assert isinstance(balances, list)
    assert len(balances) == 1
    balance = balances[0]
    assert isinstance(balance, dict)
    assert balance["subject_id"] == server.kernel.subject_id
    assert balance["network_id"] == network_id
    assert balance["asset_id"] == asset_id
    assert balance["address_id"] == address_id
    assert balance["balance"] == "123456789012345678901234567890"
    assert balance["source"] == "operator_observation"
    with pytest.raises(HTTPError) as write_attempt:
        _http_json(
            base_url,
            "/api/config/wallet-balances",
            payload={"balance": "1"},
            token=token,
        )
    assert write_attempt.value.code == 404

    with pytest.raises(HTTPError) as blocked_network_revoke:
        _http_json(
            base_url,
            f"/api/config/wallet-networks/{network_id}/revoke",
            payload={"reason": "network retirement"},
            token=token,
        )
    assert blocked_network_revoke.value.code == 400
    for path, resource_id in (
        ("wallet-assets", asset_id),
        ("wallet-addresses", address_id),
        ("wallet-networks", network_id),
    ):
        status, revoked = _http_json(
            base_url,
            f"/api/config/{path}/{resource_id}/revoke",
            payload={"reason": "operator retirement"},
            token=token,
        )
        assert status == 200
        assert isinstance(revoked, dict)
        assert revoked["status"] == "revoked"


def test_wallet_diagnostics_exposes_bounded_state_without_wallet_secrets(
    wallet_http: tuple[NoyraHTTPServer, str, str],
) -> None:
    server, base_url, token = wallet_http
    network, asset, address = _register_graph(server.wallets, server.kernel.subject_id)
    server.wallets.record_balance_snapshot(
        server.kernel.subject_id,
        WalletBalanceSnapshotInput(
            asset_id=asset.asset_id,
            address_id=address.address_id,
            balance="987654321",
        ),
        actor="operator",
    )

    status, diagnostics = _http_json(base_url, "/api/v1/diagnostics", token=token)
    assert status == 200
    assert isinstance(diagnostics, dict)
    wallet = diagnostics["wallet"]
    assert isinstance(wallet, dict)
    assert wallet["resources"]["networks"]["total"] == 1
    assert wallet["resources"]["assets"]["total"] == 1
    assert wallet["resources"]["addresses"]["total"] == 1
    assert wallet["balance_history"]["snapshots"] == 1
    assert network.network_id not in json.dumps(wallet, sort_keys=True)
    assert asset.asset_id not in json.dumps(wallet, sort_keys=True)
    assert address.address_id not in json.dumps(wallet, sort_keys=True)
    assert "claim_token" not in json.dumps(wallet, sort_keys=True)
    assert "lease_owner" not in json.dumps(wallet, sort_keys=True)


def test_wallet_diagnostics_reports_integrity_unavailability(
    wallet_http: tuple[NoyraHTTPServer, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, base_url, token = wallet_http

    def unavailable(_subject_id: str) -> dict[str, Any]:
        raise IntegrityError("wallet state unavailable")

    monkeypatch.setattr(server.wallets, "status_summary", unavailable)
    status, diagnostics = _http_json(base_url, "/api/v1/diagnostics", token=token)
    assert status == 200
    assert isinstance(diagnostics, dict)
    assert diagnostics["wallet"] == {"status": "degraded", "reason": "unavailable"}


def test_wallet_acquisition_operator_api_is_bounded_and_redacts_leases(
    wallet_http: tuple[NoyraHTTPServer, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, base_url, token = wallet_http
    with pytest.raises(HTTPError) as unauthorized:
        _http_json(base_url, "/api/v1/admin/wallet-acquisitions")
    assert unauthorized.value.code == 401

    status, network = _http_json(
        base_url,
        "/api/v1/config/wallet-networks",
        payload={
            "label": "Ethereum Mainnet",
            "chain_id": 1,
            "native_symbol": "ETH",
            "rpc_url": "https://rpc.example",
        },
        token=token,
    )
    assert status == 201
    assert isinstance(network, dict)
    network_id = str(network["network_id"])
    status, asset = _http_json(
        base_url,
        "/api/v1/config/wallet-assets",
        payload={
            "network_id": network_id,
            "asset_type": "native",
            "name": "Ether",
            "symbol": "ETH",
            "decimals": 18,
        },
        token=token,
    )
    assert status == 201
    assert isinstance(asset, dict)
    asset_id = str(asset["asset_id"])
    status, address = _http_json(
        base_url,
        "/api/v1/config/wallet-addresses",
        payload={
            "network_id": network_id,
            "label": "Treasury",
            "address": "0xA111111111111111111111111111111111111111",
            "purpose": "treasury",
        },
        token=token,
    )
    assert status == 201
    assert isinstance(address, dict)
    address_id = str(address["address_id"])

    status, queued = _http_json(
        base_url,
        "/api/v1/admin/wallet-acquisitions",
        payload={
            "asset_id": asset_id,
            "address_id": address_id,
            "idempotency_key": "operator-api-queued",
        },
        token=token,
    )
    assert status == 202
    assert isinstance(queued, dict)
    run_id = str(queued["run_id"])
    assert queued["status"] == "queued"
    assert "claim_token" not in queued
    assert "lease_owner" not in queued

    status, listed = _http_json(
        base_url,
        "/api/v1/admin/wallet-acquisitions?status=queued",
        token=token,
    )
    assert status == 200
    assert isinstance(listed, list)
    assert [row["run_id"] for row in listed if isinstance(row, dict)] == [run_id]
    status, detail = _http_json(
        base_url,
        f"/api/v1/admin/wallet-acquisitions/{run_id}",
        token=token,
    )
    assert status == 200
    assert isinstance(detail, dict)
    assert detail["run_id"] == run_id
    assert "claim_token" not in detail
    assert "lease_owner" not in detail
    with pytest.raises(HTTPError) as active_conflict:
        _http_json(
            base_url,
            "/api/v1/admin/wallet-acquisitions",
            payload={
                "asset_id": asset_id,
                "address_id": address_id,
                "idempotency_key": "operator-api-conflict",
            },
            token=token,
        )
    assert active_conflict.value.code == 409
    status, attempts = _http_json(
        base_url,
        f"/api/v1/admin/wallet-acquisitions/{run_id}/attempts",
        token=token,
    )
    assert status == 200
    assert attempts == []

    status, budget = _http_json(
        base_url,
        f"/api/v1/admin/wallet-acquisition-budget?network_id={network_id}",
        token=token,
    )
    assert status == 200
    assert isinstance(budget, dict)
    assert budget["network_id"] == network_id
    assert budget["requests_used"] == 0
    with pytest.raises(HTTPError) as missing_budget_network:
        _http_json(
            base_url,
            "/api/v1/admin/wallet-acquisition-budget?network_id=missing",
            token=token,
        )
    assert missing_budget_network.value.code == 404

    calls: list[tuple[str, str, int]] = []

    def fake_run_due(subject_id: str, *, actor: str, limit: int) -> list[object]:
        calls.append((subject_id, actor, limit))
        return []

    monkeypatch.setattr(server.wallet_acquisition_runner, "run_due", fake_run_due)
    status, run_result = _http_json(
        base_url,
        "/api/v1/admin/wallet-acquisitions/run",
        payload={"limit": 2},
        token=token,
    )
    assert status == 200
    assert run_result == {"processed": 0, "runs": []}
    assert calls == [(server.kernel.subject_id, "web-admin", 2)]

    status, cancelled = _http_json(
        base_url,
        f"/api/v1/admin/wallet-acquisitions/{run_id}/cancel",
        payload={"reason": "operator stopped observation"},
        token=token,
    )
    assert status == 200
    assert isinstance(cancelled, dict)
    assert cancelled["status"] == "cancelled"

    second = server.wallet_acquisitions.enqueue(
        server.kernel.subject_id,
        asset_id=asset_id,
        address_id=address_id,
        actor="operator",
        idempotency_key="operator-api-unknown",
    )
    claim = server.wallet_acquisitions.claim_due(
        server.kernel.subject_id,
        worker_owner="operator-api-worker",
    )[0]
    assert claim.claim_token is not None
    server.wallet_acquisitions.mark_unknown(
        second.run_id,
        subject_id=server.kernel.subject_id,
        claim_token=claim.claim_token,
        worker_owner="operator-api-worker",
        actor="operator",
    )
    status, unknown_detail = _http_json(
        base_url,
        f"/api/v1/admin/wallet-acquisitions/{second.run_id}",
        token=token,
    )
    assert status == 200
    assert isinstance(unknown_detail, dict)
    assert unknown_detail["status"] == "unknown"
    assert "claim_token" not in unknown_detail
    assert "lease_owner" not in unknown_detail
    status, unknown_attempts = _http_json(
        base_url,
        f"/api/v1/admin/wallet-acquisitions/{second.run_id}/attempts",
        token=token,
    )
    assert status == 200
    assert isinstance(unknown_attempts, list) and unknown_attempts
    assert all(
        "claim_token" not in attempt and "lease_owner" not in attempt
        for attempt in unknown_attempts
    )
    status, retried = _http_json(
        base_url,
        f"/api/v1/admin/wallet-acquisitions/{second.run_id}/retry",
        payload={"reason": "operator reviewed ambiguous read"},
        token=token,
    )
    assert status == 200
    assert isinstance(retried, dict)
    assert retried["status"] == "queued"
