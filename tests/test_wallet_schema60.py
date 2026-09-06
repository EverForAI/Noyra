from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core import database as database_module
from noyra.core.errors import IntegrityError
from noyra.core.locking import ProcessLock
from noyra.core.types import content_hash
from noyra.core.wallet_schema import (
    WalletLegacyApproval,
    legacy_entry_hash,
    legacy_journal_hash,
    wallet_upgrade_fingerprint,
)
from noyra.wallet import WalletEconomyStore
from test_wallet_release_gates import _bounty, _fixture, _goal, _submission


def _approval(database: Database) -> WalletLegacyApproval:
    with database.read_transaction() as connection:
        fingerprint = wallet_upgrade_fingerprint(connection)
    return WalletLegacyApproval(fingerprint, "release-operator", "Independently verified offline")


def _legacy_policy(database: Database, subject_id: str) -> None:
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM wallet_payment_policies WHERE subject_id=?", (subject_id,)
        ).fetchone()
        assert row is not None
        legacy_hash = content_hash(
            {
                "subject_id": subject_id,
                "version": row["policy_version"],
                "mode": row["mode"],
                "updated_at": row["updated_at"],
            }
        )
        connection.execute(
            "UPDATE wallet_payment_policies SET state_hash=? WHERE subject_id=?",
            (legacy_hash, subject_id),
        )


def _reservation(tmp_path: Path) -> tuple[Database, str, WalletEconomyStore]:
    database, subject_id, _wallets, economy, network, asset, _source = _fixture(tmp_path)
    goal_id = _goal(database, subject_id)
    bounty = _bounty(economy, subject_id, network.network_id, asset.asset_id, goal_id)
    economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    submission = economy.submit(bounty.bounty_id, subject_id, _submission(89))
    economy.decide_submission(
        submission.submission_id, subject_id, accepted=True, reason="verified", actor="operator"
    )
    return database, subject_id, economy


@pytest.mark.parametrize("timestamp", ["2099-01-01T00:00:00Z", "not-a-time"])
def test_bootstrap_tampered_time_is_rejected_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timestamp: str
) -> None:
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 59)
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-bootstrap", "genesis")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE wallet_payment_policies SET state_hash='bootstrap',updated_at=?", (timestamp,)
        )
    before = _approval(database).expected_fingerprint
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 60)
    with pytest.raises((RuntimeError, ValueError)):
        Database(database.path)
    assert _approval(database).expected_fingerprint == before


def test_custom_legacy_policy_requires_bound_explicit_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 59)
    database, subject_id, _wallets, economy, *_rest = _fixture(tmp_path)
    _legacy_policy(database, subject_id)
    policy = economy.get_policy(subject_id)
    approval = _approval(database)
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 60)
    with pytest.raises(RuntimeError, match="cannot be upgraded safely"):
        Database(database.path)
    database.initialize(wallet_legacy_approval=approval)
    assert economy.get_policy(subject_id) == policy
    economy.verify_integrity(subject_id)
    Database(database.path)
    with database.connection() as connection:
        audits = list(
            connection.execute(
                "SELECT actor,payload_json FROM audit_records "
                "WHERE action='wallet_legacy_state_authorized'"
            )
        )
    assert len(audits) == 1
    assert audits[0]["actor"] == approval.actor
    assert json.loads(audits[0]["payload_json"])["historical_authenticity_proven"] is False


def test_schema60_temporal_upgrade_accepts_bound_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid schema-60 database can finish the chronology migration."""
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 60)
    database, subject_id, _economy = _reservation(tmp_path)
    # The fixture is created at the current schema but the migration marker is
    # moved back to the supported handoff point.  Recompute the pre-61 order
    # hash so the migration can distinguish a valid legacy row from tampering.
    with database.transaction() as connection:
        connection.execute("UPDATE schema_meta SET value='60' WHERE key='schema_version'")
        connection.execute("DROP TRIGGER IF EXISTS prevent_wallet_order_identity_update")
        connection.execute("DROP TRIGGER IF EXISTS validate_wallet_order_transition")
        row = connection.execute(
            "SELECT * FROM wallet_payment_orders WHERE subject_id=?", (subject_id,)
        ).fetchone()
        if row is not None:
            legacy = content_hash(
                {
                    "order_id": row["order_id"],
                    "subject_id": row["subject_id"],
                    "bounty_id": row["bounty_id"],
                    "submission_id": row["submission_id"],
                    "network_id": row["network_id"],
                    "asset_id": row["asset_id"],
                    "recipient_address": row["recipient_address"],
                    "amount": row["amount"],
                    "payment_mode": row["payment_mode"],
                    "policy_version": row["policy_version"],
                    "idempotency_key": row["idempotency_key"],
                    "status": row["status"],
                }
            )
            connection.execute(
                "UPDATE wallet_payment_orders SET state_hash=? WHERE order_id=?",
                (legacy, row["order_id"]),
            )
    approval = _approval(database)
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 62)
    with pytest.raises(RuntimeError, match="legacy timestamps require explicit approval"):
        Database(database.path)
    database.initialize(wallet_legacy_approval=approval)
    assert database_module.CURRENT_SCHEMA_VERSION == 62
    WalletEconomyStore(database).verify_integrity(subject_id)


def test_schema60_temporal_upgrade_rejects_stale_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 60)
    database, _subject_id, _economy = _reservation(tmp_path)
    with database.transaction() as connection:
        connection.execute("UPDATE schema_meta SET value='60' WHERE key='schema_version'")
        connection.execute("DROP TRIGGER IF EXISTS prevent_wallet_order_identity_update")
        connection.execute("DROP TRIGGER IF EXISTS validate_wallet_order_transition")
        connection.execute(
            "UPDATE wallet_payment_orders SET state_hash='0' || substr(state_hash,2)"
        )
    approval = _approval(database)
    with database.transaction() as connection:
        connection.execute("UPDATE wallet_payment_orders SET amount='8'")
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 62)
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        database.initialize(wallet_legacy_approval=approval)


@pytest.mark.parametrize("field", ["daily_limit", "updated_at"])
def test_stale_approval_does_not_authorize_changed_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 59)
    database, subject_id, *_rest = _fixture(tmp_path)
    _legacy_policy(database, subject_id)
    approval = _approval(database)
    with database.transaction() as connection:
        connection.execute(
            f"UPDATE wallet_payment_policies SET {field}=?",
            ("999" if field == "daily_limit" else "2099-01-01T00:00:00Z",),
        )
    before = _approval(database).expected_fingerprint
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 60)
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        database.initialize(wallet_legacy_approval=approval)
    assert _approval(database).expected_fingerprint == before


@pytest.mark.parametrize("timestamp", ["2099-01-01T00:00:00Z", "not-a-time"])
@pytest.mark.parametrize(
    "table,trigger",
    [
        ("wallet_ledger_journals", "prevent_wallet_journal_update"),
        ("wallet_ledger_entries", "prevent_wallet_entry_update"),
    ],
)
def test_ledger_timestamp_tampering_is_detected(
    tmp_path: Path, table: str, trigger: str, timestamp: str
) -> None:
    database, subject_id, economy = _reservation(tmp_path)
    with database.transaction() as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(f"UPDATE {table} SET created_at=?", (timestamp,))
    with pytest.raises(IntegrityError):
        economy.verify_integrity(subject_id)


def test_matching_journal_and_entry_time_tampering_is_detected(tmp_path: Path) -> None:
    database, subject_id, economy = _reservation(tmp_path)
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_wallet_journal_update")
        connection.execute("DROP TRIGGER prevent_wallet_entry_update")
        for table in ("wallet_ledger_journals", "wallet_ledger_entries"):
            connection.execute(f"UPDATE {table} SET created_at='2099-01-01T00:00:00Z'")
    with pytest.raises(IntegrityError, match="journal hash mismatch"):
        economy.verify_integrity(subject_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("daily_limit", "-1"),
        ("allowed_network_ids_json", '{"network":true}'),
        ("allowed_asset_ids_json", '["duplicate","duplicate"]'),
        ("max_observation_age_seconds", 2_592_001),
    ],
)
def test_approval_cannot_authorize_malformed_legacy_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str | int
) -> None:
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 59)
    database, subject_id, *_rest = _fixture(tmp_path)
    _legacy_policy(database, subject_id)
    with database.transaction() as connection:
        connection.execute(f"UPDATE wallet_payment_policies SET {field}=?", (value,))
    approval = _approval(database)
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 60)
    with pytest.raises(IntegrityError, match="policy is malformed"):
        database.initialize(wallet_legacy_approval=approval)
    assert _approval(database) == approval


@pytest.mark.parametrize(
    "table,trigger",
    [
        ("wallet_payment_policies", None),
        ("wallet_ledger_journals", "prevent_wallet_journal_update"),
        ("wallet_ledger_entries", "prevent_wallet_entry_update"),
    ],
)
def test_approval_cannot_bypass_corrupt_legacy_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str, trigger: str | None
) -> None:
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 59)
    database, _subject_id, _economy = _reservation(tmp_path)
    with database.transaction() as connection:
        if trigger is not None:
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(f"UPDATE {table} SET state_hash='corrupt'")
    approval = _approval(database)
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 60)
    with pytest.raises(RuntimeError, match="legacy hash mismatch"):
        database.initialize(wallet_legacy_approval=approval)
    assert _approval(database) == approval


def test_legacy_ledger_requires_approval_and_backfill_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 59)
    database, subject_id, economy = _reservation(tmp_path)
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_wallet_journal_update")
        connection.execute("DROP TRIGGER prevent_wallet_entry_update")
        for table, primary_key, hasher in (
            ("wallet_ledger_journals", "journal_id", legacy_journal_hash),
            ("wallet_ledger_entries", "entry_id", legacy_entry_hash),
        ):
            for row in connection.execute(f"SELECT * FROM {table}"):
                connection.execute(
                    f"UPDATE {table} SET state_hash=? WHERE {primary_key}=?",
                    (hasher(row), row[primary_key]),
                )
    approval = _approval(database)
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 60)
    with pytest.raises(RuntimeError, match="timestamps require explicit approval"):
        Database(database.path)
    assert _approval(database) == approval
    database.initialize(wallet_legacy_approval=approval)
    assert economy.verify_integrity(subject_id)["wallet_ledger_entries"] == 2
    with database.transaction() as connection:
        for table in ("wallet_ledger_journals", "wallet_ledger_entries"):
            with pytest.raises(Exception, match="append-only"):
                connection.execute(f"UPDATE {table} SET created_at='2099-01-01T00:00:00Z'")


def test_failure_after_hash_backfill_restores_marker_hashes_and_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 59)
    database, subject_id, *_rest = _fixture(tmp_path)
    _legacy_policy(database, subject_id)
    with database.transaction() as connection:
        connection.execute("DROP INDEX idx_wallet_ledger_entries_subject")
        connection.execute("DROP INDEX idx_wallet_ledger_entries_journal_entry")
    approval = _approval(database)
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 60)
    monkeypatch.setitem(
        database_module.MIGRATIONS,
        60,
        database_module.MIGRATIONS[60] + "SELECT missing_column FROM schema_meta;",
    )
    with pytest.raises(Exception, match="missing_column"):
        database.initialize(wallet_legacy_approval=approval)
    assert _approval(database) == approval
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'idx_wallet_ledger_entries_%'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM audit_records WHERE action='wallet_legacy_state_authorized'"
            ).fetchone()[0]
            == 0
        )


def test_offline_cli_inspection_authorization_and_runtime_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 59)
    database, subject_id, *_rest = _fixture(tmp_path)
    _legacy_policy(database, subject_id)
    script = Path(__file__).resolve().parents[1] / "scripts" / "authorize-wallet-schema60.py"
    command = [sys.executable, str(script), str(database.path)]
    inspection = subprocess.run(command, capture_output=True, text=True, check=True)
    report = json.loads(inspection.stdout)
    assert report["approved"] is False
    lock = ProcessLock(f"{database.path}.lock")
    lock.acquire()
    try:
        blocked = subprocess.run(command, capture_output=True, text=True)
        assert blocked.returncode != 0
    finally:
        lock.release()
    authorized = subprocess.run(
        [
            *command,
            "--expected-fingerprint",
            report["fingerprint"],
            "--actor",
            "operator",
            "--reason",
            "Reviewed offline",
        ],
        capture_output=True,
        text=True,
    )
    assert authorized.returncode == 0, authorized.stderr
    WalletEconomyStore(database).verify_integrity(subject_id)
