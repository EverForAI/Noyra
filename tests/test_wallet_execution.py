import json
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from sqlite3 import IntegrityError as SQLiteIntegrityError
from typing import Any

import httpx
import pytest

from noyra.core import Database, IdentityStore
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError, InvalidTransitionError
from noyra.core.types import content_hash, utc_now
from noyra.wallet import (
    BountyInput,
    EVMTransferAdapter,
    HTTPSWalletSigner,
    MockSigner,
    PaymentPolicyInput,
    SubmissionInput,
    WalletAddressInput,
    WalletAssetInput,
    WalletEconomyStore,
    WalletNetworkInput,
    WalletPaymentExecutionEngine,
    WalletRewardWorkflow,
    WalletStore,
    WalletTransferIntent,
)


@pytest.mark.parametrize(
    ("error_code", "status", "expected"),
    [
        ("signer_rejected", "failed", "signer_rejection"),
        ("broadcast_unknown", "unknown", "broadcast_unknown"),
        ("receipt_lookup_unknown", "unknown", "receipt_chain_unknown"),
        (None, "failed", "recovery_mismatch"),
        (None, "unknown", "payment_unknown"),
    ],
)
def test_reward_execution_incident_taxonomy_is_bounded(
    error_code: str | None, status: str, expected: str
) -> None:
    assert WalletRewardWorkflow._execution_incident_kind(error_code, status) == expected


def fixture(tmp_path: Path) -> tuple[Database, str, Any, Any, Any, Any]:
    db = Database(tmp_path / "db.sqlite3")
    subject = "Noyra-execution-test"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    wallets = WalletStore(db)
    network = wallets.register_network(
        subject,
        WalletNetworkInput(
            label="Ethereum", chain_id=1, native_symbol="ETH", rpc_url="https://rpc.example"
        ),
        actor="operator",
    )
    asset = wallets.register_asset(
        subject,
        WalletAssetInput(
            network_id=network.network_id,
            asset_type="native",
            name="Ether",
            symbol="ETH",
            decimals=18,
        ),
        actor="operator",
    )
    source = wallets.register_address(
        subject,
        WalletAddressInput(
            network_id=network.network_id,
            label="Spend",
            address="0xA111111111111111111111111111111111111111",
            purpose="spending",
        ),
        actor="operator",
    )
    goal = "goal_execution"
    now = utc_now()
    with db.transaction() as c:
        c.execute(
            "INSERT INTO goals("
            "goal_id,subject_id,title,description,origin,status,priority,commitment,"
            "progress,emotional_pressure,state_hash,current_revision,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (goal, subject, "Goal", "Goal", "self", "active", 1, 1, 0, 0, "x", 1, now, now),
        )
    economy = WalletEconomyStore(db)
    economy.update_policy(
        subject,
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
    t = datetime.now(UTC)
    bounty = economy.create_bounty(
        subject,
        BountyInput(
            title="Help",
            description="Proof",
            acceptance_criteria=["proof"],
            network_id=network.network_id,
            asset_id=asset.asset_id,
            reward_amount="10",
            opens_at=(t - timedelta(minutes=1)).isoformat(),
            expires_at=(t + timedelta(days=1)).isoformat(),
            max_submissions=2,
            reward_slots=1,
            goal_id=goal,
            idempotency_key="bounty-exec",
        ),
        actor="operator",
    )
    economy.publish_bounty(bounty.bounty_id, subject, actor="operator")
    submission = economy.submit(
        bounty.bounty_id,
        subject,
        SubmissionInput(
            counterparty="human",
            content="done",
            recipient_address="0xB111111111111111111111111111111111111111",
            idempotency_key="submission-exec",
            consent_version=1,
        ),
    )
    economy.decide_submission(
        submission.submission_id, subject, accepted=True, reason="verified", actor="operator"
    )
    return db, subject, network, asset, source, economy.list_orders(subject)[0]


def test_https_signer_uses_only_fixed_transfer_and_receipt_routes() -> None:
    tx_hash = "0x" + "a" * 64
    calls: list[tuple[str, object]] = []
    responses = [
        {"tx_hash": tx_hash, "chain_id": 1, "nonce": 4, "accepted_at": utc_now()},
        {"tx_hash": tx_hash, "chain_id": 1, "status": 1, "block_number": 7},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=responses.pop(0), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    signer = HTTPSWalletSigner("https://signer.example", signer_id="testnet-signer", client=client)
    transfer = EVMTransferAdapter().build(
        WalletTransferIntent(
            order_id="order",
            subject_id="subject",
            network_id="network",
            asset_id="asset",
            asset_type="native",
            source_address="0xA111111111111111111111111111111111111111",
            recipient_address="0xB111111111111111111111111111111111111111",
            amount="1",
            chain_id=1,
            nonce=4,
            gas_limit=21_000,
            max_fee_per_gas="1",
        )
    )
    result = signer.sign_and_broadcast(transfer, request_id="order:attempt:1")
    receipt = signer.get_receipt(tx_hash, chain_id=1)
    assert result.tx_hash == tx_hash
    assert receipt is not None and receipt.status == 1
    assert [call[0] for call in calls] == ["/v1/transfer", "/v1/receipt"]
    assert isinstance(calls[0][1], dict)
    assert set(calls[0][1]) == {"request_id", "transfer"}
    assert "private_key" not in json.dumps(calls)


def test_https_signer_rejects_insecure_or_credentialed_endpoint() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    for endpoint in (
        "http://signer.example",
        "https://user:secret@signer.example",
        "https://127.0.0.1",
        "https://signer.example?method=eth_sendRawTransaction",
    ):
        with pytest.raises(ValueError):
            HTTPSWalletSigner(endpoint, signer_id="signer", client=client)


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(400, "rejected"), (429, "rejected"), (500, "unknown")],
)
def test_https_signer_classifies_transfer_http_status(status_code: int, expected: str) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code, request=request))
    )
    signer = HTTPSWalletSigner("https://signer.example", signer_id="signer", client=client)
    transfer = EVMTransferAdapter().build(
        WalletTransferIntent(
            order_id="order",
            subject_id="subject",
            network_id="network",
            asset_id="asset",
            asset_type="native",
            source_address="0xA111111111111111111111111111111111111111",
            recipient_address="0xB111111111111111111111111111111111111111",
            amount="1",
            chain_id=1,
            nonce=0,
            gas_limit=21_000,
            max_fee_per_gas="1",
        )
    )
    from noyra.wallet.execution import WalletBroadcastUnknownError, WalletSignerError

    error = WalletSignerError if expected == "rejected" else WalletBroadcastUnknownError
    with pytest.raises(error):
        signer.sign_and_broadcast(transfer, request_id="order:attempt:1")


def test_https_signer_receipt_404_and_malformed_response_are_bounded() -> None:
    tx_hash = "0x" + "a" * 64
    responses = [
        httpx.Response(404),
        httpx.Response(200, json={"chain_id": 1, "status": 1}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        response = responses.pop(0)
        response.request = request
        return response

    signer = HTTPSWalletSigner(
        "https://signer.example",
        signer_id="signer",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert signer.get_receipt(tx_hash, chain_id=1) is None
    from noyra.wallet.execution import WalletExecutionError

    with pytest.raises(WalletExecutionError):
        signer.get_receipt(tx_hash, chain_id=1)


def test_schema_55_migrates_execution_tables_and_reopens_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "migration.sqlite3"
    database = Database(path)
    with database.transaction() as connection:
        connection.execute("UPDATE schema_meta SET value='55' WHERE key='schema_version'")
    migrated = Database(path)
    with migrated.connection() as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()["value"]
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM wallet_payment_executions "
            "WHERE subject_id=? ORDER BY created_at DESC,execution_id DESC LIMIT 100",
            ("subject",),
        ).fetchall()
    assert version == str(CURRENT_SCHEMA_VERSION)
    assert {
        "wallet_payment_executions",
        "wallet_payment_execution_attempts",
    } <= tables
    plan_text = " ".join(str(row["detail"]) for row in plan)
    assert "idx_wallet_payment_executions_history" in plan_text
    assert "USE TEMP B-TREE" not in plan_text
    with migrated.transaction() as connection:
        connection.execute("DROP TRIGGER validate_wallet_execution_transition")
        connection.execute(
            "CREATE TRIGGER validate_wallet_execution_transition "
            "BEFORE UPDATE ON wallet_payment_executions "
            "WHEN NOT (NEW.status=OLD.status AND NEW.state_hash=OLD.state_hash) "
            "BEGIN SELECT RAISE(ABORT,'weak transition'); END"
        )
    reopened = Database(path)
    with reopened.connection() as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='validate_wallet_execution_transition'"
        ).fetchone()["sql"]
    assert "NEW.error_code IS OLD.error_code" in trigger_sql


def test_signer_exception_is_classified_without_persisting_secret(tmp_path: Path) -> None:
    class ExplodingSigner(MockSigner):
        def sign_and_broadcast(self, transfer: Any, *, request_id: str) -> Any:
            del transfer, request_id
            raise RuntimeError("private-key-material-must-not-leak")

    db, subject, network, _asset, source, order = fixture(tmp_path)
    signer = ExplodingSigner(chain_id=network.chain_id, source_address=source.address)
    record = WalletPaymentExecutionEngine(db, signer).execute_order(
        order.order_id, subject, actor="operator"
    )
    assert record.status == "unknown"
    assert record.error_code == "signer_transport_unknown"
    with db.connection() as connection:
        values = " ".join(
            str(value)
            for row in connection.execute(
                "SELECT payload_json FROM audit_records WHERE subject_id=?", (subject,)
            ).fetchall()
            for value in row
        )
    assert "private-key-material-must-not-leak" not in values


def test_native_execution_confirm_and_settle(tmp_path: Path) -> None:
    db, subject, network, _asset, source, order = fixture(tmp_path)
    signer = MockSigner(chain_id=network.chain_id, source_address=source.address)
    engine = WalletPaymentExecutionEngine(db, signer)
    record = engine.execute_order(order.order_id, subject, actor="operator")
    assert record.status == "broadcast"
    assert signer.requests[0].to_address == "0xb111111111111111111111111111111111111111"
    assert signer.requests[0].data == "0x"
    assert record.tx_hash is not None
    signer.set_receipt(record.tx_hash, chain_id=1, status=1, block_number=7)
    assert engine.poll_receipt(record.execution_id, subject, actor="operator").status == "confirmed"
    assert [(b.account, b.net) for b in WalletEconomyStore(db).ledger_balances(subject)] == [
        ("available", "10"),
        ("paid", "-10"),
        ("reserved", "0"),
    ]


def test_broadcast_response_loss_is_unknown_and_requires_explicit_retry(tmp_path: Path) -> None:
    db, subject, network, _asset, source, order = fixture(tmp_path)
    signer = MockSigner(
        chain_id=network.chain_id, source_address=source.address, lose_first_response=True
    )
    engine = WalletPaymentExecutionEngine(db, signer)
    unknown = engine.execute_order(order.order_id, subject, actor="operator")
    assert unknown.status == "unknown"
    assert len(engine.list_attempts(unknown.execution_id, subject)) == 1
    with pytest.raises(InvalidTransitionError):
        engine.execute_order(order.order_id, subject, actor="operator")
    retried = engine.retry_unknown(
        order.order_id, subject, actor="operator", reason="receipt lookup unavailable"
    )
    assert retried.status == "broadcast"
    assert retried.attempt_count == 2


def test_unknown_retry_rejects_silent_signer_identity_change(tmp_path: Path) -> None:
    db, subject, network, _asset, source, order = fixture(tmp_path)
    unknown = WalletPaymentExecutionEngine(
        db,
        MockSigner(
            signer_id="signer-a",
            chain_id=network.chain_id,
            source_address=source.address,
            lose_first_response=True,
        ),
    ).execute_order(order.order_id, subject, actor="operator")
    assert unknown.status == "unknown"
    changed = WalletPaymentExecutionEngine(
        db,
        MockSigner(signer_id="signer-b", chain_id=network.chain_id, source_address=source.address),
    )
    with pytest.raises(ValueError, match="signer identity changed"):
        changed.retry_unknown(order.order_id, subject, actor="operator", reason="retry")


def test_chain_failure_can_be_refunded_once(tmp_path: Path) -> None:
    db, subject, network, _asset, source, order = fixture(tmp_path)
    signer = MockSigner(chain_id=network.chain_id, source_address=source.address)
    engine = WalletPaymentExecutionEngine(db, signer)
    broadcast = engine.execute_order(order.order_id, subject, actor="operator")
    assert broadcast.tx_hash is not None
    signer.set_receipt(broadcast.tx_hash, chain_id=1, status=0, block_number=8)
    failed = engine.poll_receipt(broadcast.execution_id, subject, actor="operator")
    assert failed.status == "failed"
    refunded = engine.refund(order.order_id, subject, actor="operator", reason="receipt failed")
    assert refunded.status == "refunded"
    with pytest.raises(InvalidTransitionError):
        engine.refund(order.order_id, subject, actor="operator", reason="again")


def test_transfer_adapter_rejects_arbitrary_transaction_shape() -> None:
    with pytest.raises(ValueError):
        WalletTransferIntent(
            order_id="o",
            subject_id="s",
            network_id="n",
            asset_id="a",
            asset_type="native",
            source_address="0xA111111111111111111111111111111111111111",
            recipient_address="0xB111111111111111111111111111111111111111",
            amount="1",
            chain_id=1,
            nonce=0,
            gas_limit=21_000,
            max_fee_per_gas="1",
            contract_address="0xC111111111111111111111111111111111111111",
        )
    assert isinstance(EVMTransferAdapter(), EVMTransferAdapter)


class _BadResponseSigner(MockSigner):
    def sign_and_broadcast(self, transfer: Any, *, request_id: str) -> Any:
        result = super().sign_and_broadcast(transfer, request_id=request_id)
        return type(result)(result.tx_hash, result.chain_id + 1, result.nonce, result.accepted_at)

    def get_receipt(self, tx_hash: str, *, chain_id: int) -> Any:
        return type(
            "BadReceipt",
            (),
            {"tx_hash": object(), "chain_id": chain_id, "status": 1, "block_number": 1},
        )()


def test_chain_mismatch_is_unknown_and_invalid_receipt_is_fail_closed(tmp_path: Path) -> None:
    db, subject, network, _asset, source, order = fixture(tmp_path)
    signer = _BadResponseSigner(chain_id=network.chain_id, source_address=source.address)
    engine = WalletPaymentExecutionEngine(db, signer)
    unknown = engine.execute_order(order.order_id, subject, actor="operator")
    assert unknown.status == "unknown"
    assert unknown.error_code == "signer_response_invalid"


def test_failed_receipt_records_receipt_status_and_refund_is_balanced(tmp_path: Path) -> None:
    db, subject, network, _asset, source, order = fixture(tmp_path)
    signer = MockSigner(chain_id=network.chain_id, source_address=source.address)
    engine = WalletPaymentExecutionEngine(db, signer)
    broadcast = engine.execute_order(order.order_id, subject, actor="operator")
    assert broadcast.tx_hash is not None
    signer.set_receipt(broadcast.tx_hash, chain_id=network.chain_id, status=0, block_number=8)
    failed = engine.poll_receipt(broadcast.execution_id, subject, actor="operator")
    assert failed.receipt_status == 0
    assert failed.receipt_block_number == 8
    assert (
        engine.refund(order.order_id, subject, actor="operator", reason="operator review").status
        == "refunded"
    )
    assert engine.verify_integrity(subject) == {
        "wallet_payment_executions": 1,
        "wallet_payment_execution_attempts": 1,
    }


def test_token_transfer_calldata_is_fixed_and_canonical() -> None:
    intent = WalletTransferIntent(
        order_id="o",
        subject_id="s",
        network_id="n",
        asset_id="a",
        asset_type="token",
        contract_address="0xC111111111111111111111111111111111111111",
        source_address="0xA111111111111111111111111111111111111111",
        recipient_address="0xB111111111111111111111111111111111111111",
        amount="10",
        chain_id=1,
        nonce=4,
        gas_limit=100_000,
        max_fee_per_gas="2",
    )
    envelope = EVMTransferAdapter().build(intent)
    assert envelope.to_address == intent.contract_address
    assert envelope.value == "0"
    assert (
        envelope.data
        == "0xa9059cbb" + "0" * 24 + "b111111111111111111111111111111111111111" + "0" * 63 + "a"
    )


def test_execution_identity_and_attempt_cross_subject_triggers(tmp_path: Path) -> None:
    db, subject, _network, _asset, _source, order = fixture(tmp_path)
    signer = MockSigner()
    engine = WalletPaymentExecutionEngine(db, signer)
    record = engine.execute_order(order.order_id, subject, actor="operator")
    with pytest.raises(SQLiteIntegrityError), db.transaction() as c:
        c.execute(
            "UPDATE wallet_payment_executions SET order_id=? WHERE execution_id=?",
            ("other-order", record.execution_id),
        )
    with pytest.raises(SQLiteIntegrityError), db.transaction() as c:
        c.execute(
            "INSERT INTO wallet_payment_execution_attempts("
            "attempt_id,execution_id,subject_id,attempt_number,request_id,status,"
            "started_at,state_hash,created_audit_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "foreign-attempt",
                record.execution_id,
                "Other-subject",
                1,
                "foreign-request",
                "signing",
                utc_now(),
                "0" * 64,
                record.request_id,
            ),
        )


def test_execution_same_status_updates_cannot_bypass_state_hash(tmp_path: Path) -> None:
    db, subject, _network, _asset, _source, order = fixture(tmp_path)
    engine = WalletPaymentExecutionEngine(db, MockSigner())
    record = engine.execute_order(order.order_id, subject, actor="operator")
    with pytest.raises(SQLiteIntegrityError), db.transaction() as c:
        c.execute(
            "UPDATE wallet_payment_executions SET error_code='tampered' WHERE execution_id=?",
            (record.execution_id,),
        )
    with pytest.raises(SQLiteIntegrityError), db.transaction() as c:
        c.execute(
            "UPDATE wallet_payment_execution_attempts SET error_code='tampered' "
            "WHERE execution_id=?",
            (record.execution_id,),
        )


def test_execution_integrity_consumes_history_without_fetchall(tmp_path: Path) -> None:
    db, subject, _network, _asset, _source, order = fixture(tmp_path)
    WalletPaymentExecutionEngine(db, MockSigner()).execute_order(
        order.order_id, subject, actor="operator"
    )

    class NoFetchallCursor:
        def __init__(self, cursor: Any) -> None:
            self._cursor = cursor

        def __iter__(self) -> Iterator[Any]:
            return iter(self._cursor)

        def fetchone(self) -> Any:
            return self._cursor.fetchone()

        def fetchall(self) -> list[Any]:
            raise AssertionError("wallet execution integrity must stream history")

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

    counts = WalletPaymentExecutionEngine.verify_database_integrity(
        NoFetchallDatabase(db.path), subject
    )
    assert counts == {
        "wallet_payment_executions": 1,
        "wallet_payment_execution_attempts": 1,
    }


def test_execution_integrity_detects_tampering_and_runtime_export_owns_tables(
    tmp_path: Path,
) -> None:
    db, subject, network, _asset, source, order = fixture(tmp_path)
    signer = MockSigner(chain_id=network.chain_id, source_address=source.address)
    engine = WalletPaymentExecutionEngine(db, signer)
    record = engine.execute_order(order.order_id, subject, actor="operator")
    assert engine.verify_integrity(subject)["wallet_payment_executions"] == 1
    from noyra.core.runtime_export import RuntimeLogExporter

    artifact = RuntimeLogExporter(db).export(subject, actor="operator")
    with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        tables = {item["name"] for item in manifest["tables"]}
        assert "wallet_payment_executions" in tables
        assert "wallet_payment_execution_attempts" in tables
        payload = archive.read(
            next(
                item["file"]
                for item in manifest["tables"]
                if item["name"] == "wallet_payment_executions"
            )
        )
        assert record.execution_id.encode() in payload
        assert b"private_key" not in payload
    with db.transaction() as c:
        c.execute(
            "UPDATE wallet_payment_executions SET status='unknown',error_code='tampered' "
            "WHERE execution_id=?",
            (record.execution_id,),
        )
    with pytest.raises(IntegrityError):
        engine.verify_integrity(subject)
