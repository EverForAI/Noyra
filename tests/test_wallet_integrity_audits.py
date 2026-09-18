from __future__ import annotations

import json
from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core import integrity as integrity_module
from noyra.core.integrity import IntegrityRegistry, IntegrityReport
from noyra.core.types import canonical_json, content_hash, utc_now
from noyra.wallet import (
    MockSigner,
    WalletBalanceAcquisitionLedger,
    WalletNetworkInput,
    WalletPaymentExecutionEngine,
    WalletStore,
)
from test_m42_p1_02_integrity_runtime import _active_kernel, _controller
from test_wallet_execution import fixture
from test_wallet_reward_workflow import _accept, _open, _submission
from test_wallet_reward_workflow import _fixture as reward_fixture


def _audit(database: Database, subject: str, root: Path) -> IntegrityReport:
    return IntegrityRegistry().run(
        database,
        subject,
        root,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.actions", "wallet.state"),
    )


def test_wallet_setup_acquisition_and_payment_do_not_trigger_core_quarantine(
    tmp_path: Path,
) -> None:
    database, subject, network, asset, source, order = fixture(tmp_path)
    assert _audit(database, subject, tmp_path).status == "ok"
    acquisition = WalletBalanceAcquisitionLedger(database)
    run = acquisition.enqueue(
        subject,
        asset_id=asset.asset_id,
        address_id=source.address_id,
        actor="operator",
        idempotency_key="integrity-balance",
    )
    assert _audit(database, subject, tmp_path).status == "ok"
    acquisition.cancel(run.run_id, subject_id=subject, actor="operator", reason="test complete")
    signer = MockSigner(chain_id=network.chain_id, source_address=source.address)
    engine = WalletPaymentExecutionEngine(database, signer)
    sent = engine.execute_order(order.order_id, subject, actor="operator")
    assert sent.status == "broadcast"
    assert _audit(database, subject, tmp_path).status == "ok"
    assert sent.tx_hash is not None
    signer.set_receipt(sent.tx_hash, chain_id=1, status=1, block_number=7)
    assert engine.poll_receipt(sent.execution_id, subject, actor="operator").status == "confirmed"
    assert _audit(database, subject, tmp_path).status == "ok"


def test_reward_workflow_audits_remain_healthy_through_settlement(tmp_path: Path) -> None:
    setup = reward_fixture(tmp_path)
    opened = _open(setup)
    submitted = setup.workflow.submit_public(
        opened.workflow_id, setup.subject_id, _submission(network_id=setup.network_id)
    )
    assert submitted.submission is not None
    _accept(setup, submitted.submission.submission_id)
    assert _audit(setup.database, setup.subject_id, tmp_path).status == "ok"
    sent = setup.workflow.execute_ready(setup.subject_id, actor="operator")[0]
    assert sent.execution_id is not None
    engine = setup.workflow.execution
    assert engine is not None
    execution = engine.get_execution(sent.execution_id, setup.subject_id)
    assert execution.tx_hash is not None
    setup.signer.set_receipt(execution.tx_hash, chain_id=1, status=1, block_number=7)
    setup.workflow.execute_ready(setup.subject_id, actor="operator")
    closed = setup.workflow.execute_ready(setup.subject_id, actor="operator")
    assert closed[0].workflow.status == "closed"
    assert _audit(setup.database, setup.subject_id, tmp_path).status == "ok"


@pytest.mark.parametrize(
    "damage",
    ["unknown_action", "unknown_wallet_action", "orphan", "cross_subject", "actor", "json"],
)
def test_wallet_audit_routing_rejects_invalid_records(tmp_path: Path, damage: str) -> None:
    database = Database(tmp_path / "db.sqlite3")
    subject = "Noyra-wallet-audit"
    IdentityStore(database).ensure(subject, content_hash(subject))
    store = WalletStore(database)
    network = store.register_network(
        subject,
        WalletNetworkInput(label="Sepolia", chain_id=11155111, native_symbol="ETH"),
        actor="operator",
    )
    assert _audit(database, subject, tmp_path).status == "ok"
    other = "Noyra-wallet-audit-other"
    if damage == "cross_subject":
        IdentityStore(database).ensure(other, content_hash(other))
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM audit_records WHERE audit_id="
            "(SELECT created_audit_id FROM wallet_networks WHERE network_id=?)",
            (network.network_id,),
        ).fetchone()
        assert row is not None
        if damage == "orphan":
            connection.execute(
                "INSERT INTO audit_records VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "audit_unbound",
                    subject,
                    row["action"],
                    row["actor"],
                    row["payload_json"],
                    utc_now(),
                ),
            )
        else:
            connection.execute("DROP TRIGGER prevent_audit_record_update")
            if damage in {"unknown_action", "unknown_wallet_action"}:
                action = (
                    "wallet_unrecognized_action" if damage == "unknown_wallet_action" else "bogus"
                )
                connection.execute("UPDATE audit_records SET action=?", (action,))
            elif damage == "cross_subject":
                connection.execute("UPDATE audit_records SET subject_id=?", (other,))
            elif damage == "actor":
                connection.execute("UPDATE audit_records SET actor='subject'")
            else:
                connection.execute("UPDATE audit_records SET payload_json='{}'")
    report = _audit(database, other if damage == "cross_subject" else subject, tmp_path)
    assert "core.actions:integrity_error" in report.p0


def test_wallet_state_corruption_is_still_detected_after_audit_routing(tmp_path: Path) -> None:
    database, subject, _network, _asset, _source, _order = fixture(tmp_path)
    assert _audit(database, subject, tmp_path).status == "ok"
    with database.transaction() as connection:
        connection.execute("UPDATE wallet_payment_policies SET state_hash='tampered'")
    assert "wallet.state:integrity_error" in _audit(database, subject, tmp_path).p0


@pytest.mark.parametrize("damage", ["actor", "future_version", "mode_type", "missing_order"])
def test_wallet_policy_and_payment_audits_still_validate_evidence(
    tmp_path: Path, damage: str
) -> None:
    database, subject, _network, _asset, _source, _order = fixture(tmp_path)
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_audit_record_update")
        row = connection.execute(
            "SELECT * FROM audit_records WHERE action='wallet_payment_policy_updated'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        if damage == "actor":
            connection.execute(
                "UPDATE audit_records SET actor='subject' WHERE audit_id=?", (row["audit_id"],)
            )
        elif damage == "missing_order":
            connection.execute(
                "INSERT INTO audit_records VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "audit_orphan_payment",
                    subject,
                    "wallet_payment_confirmed",
                    "operator",
                    canonical_json({"order_id": "order_missing"}),
                    utc_now(),
                ),
            )
        else:
            payload["policy_version" if damage == "future_version" else "mode"] = (
                999 if damage == "future_version" else []
            )
            connection.execute(
                "UPDATE audit_records SET payload_json=? WHERE audit_id=?",
                (canonical_json(payload), row["audit_id"]),
            )
    assert "core.actions:integrity_error" in _audit(database, subject, tmp_path).p0


def test_corrected_check_clears_persisted_quarantine_without_deleting_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = "Noyra-wallet-audit-recovery"
    kernel = _active_kernel(tmp_path, subject)
    registry = IntegrityRegistry(
        [check for check in IntegrityRegistry().checks if check.check_id == "core.actions"]
    )
    payload = canonical_json(
        {
            "expected_fingerprint": "e" * 64,
            "reason": "Reviewed offline",
            "target_schema": 60,
            "historical_authenticity_proven": False,
        }
    )
    try:
        with kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_records VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "audit_migration",
                    subject,
                    "wallet_legacy_state_authorized",
                    "operator",
                    payload,
                    utc_now(),
                ),
            )
        controller = _controller(kernel, tmp_path, registry=registry)
        with monkeypatch.context() as old:
            old.setattr(
                integrity_module,
                "_CORE_AUDIT_ACTIONS",
                integrity_module._CORE_AUDIT_ACTIONS - {"wallet_legacy_state_authorized"},
            )
            failure = controller.run_periodic_if_due(force=True)
        assert failure is not None and failure.p0 == ("core.actions:integrity_error",)
        restarted = _controller(kernel, tmp_path, registry=registry)
        assert restarted.summary()["p0"] == 1
        clean = restarted.run_periodic_if_due(force=True)
        assert clean is not None and clean.status == "ok"
        assert restarted.summary()["p0"] == 0
        with kernel.database.read_transaction() as connection:
            assert (
                connection.execute(
                    "SELECT payload_json FROM audit_records WHERE audit_id='audit_migration'"
                ).fetchone()[0]
                == payload
            )
        assert (restarted._directory / f"{failure.run_id}.json").is_file()
    finally:
        kernel.close()
