from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeAlias

import httpx
import pytest

from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.types import canonical_json, content_hash
from noyra.wallet import (
    WalletAcquisitionRunError,
    WalletAddressInput,
    WalletAssetInput,
    WalletBalanceAcquisitionLedger,
    WalletBalanceAcquisitionRunner,
    WalletNetworkInput,
    WalletRPCBalanceAcquirer,
    WalletRPCError,
    WalletStore,
)


@dataclass
class FakeClock:
    value: datetime = datetime(2026, 8, 31, tzinfo=UTC)

    def __call__(self) -> str:
        return self.value.isoformat(timespec="milliseconds")

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


AcquisitionFixture: TypeAlias = tuple[
    Database, str, WalletStore, WalletBalanceAcquisitionLedger, FakeClock
]


@pytest.fixture
def acquisition_fixture(
    tmp_path: Path,
) -> AcquisitionFixture:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-wallet-acquisition"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    clock = FakeClock()
    store = WalletStore(database)
    ledger = WalletBalanceAcquisitionLedger(
        database,
        clock=clock,
        max_concurrent=2,
        min_interval_seconds=0,
        window_seconds=60,
        request_limit_per_window=30,
        lease_seconds=10,
        backoff_seconds=2,
    )
    return database, subject_id, store, ledger, clock


def _graph(
    store: WalletStore,
    subject_id: str,
    *,
    suffix: str = "",
    chain_id: int = 1,
) -> tuple[Any, Any, Any]:
    network = store.register_network(
        subject_id,
        WalletNetworkInput(
            label=f"Ethereum Mainnet{suffix}",
            chain_id=chain_id,
            native_symbol="ETH",
            rpc_url="https://rpc.example",
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
            label=f"Treasury{suffix}",
            address="0x" + ("a" if not suffix else "b") * 40,
            purpose="treasury",
        ),
        actor="operator",
    )
    return network, asset, address


def _address(
    store: WalletStore,
    subject_id: str,
    network_id: str,
    label: str,
    nibble: str,
) -> Any:
    return store.register_address(
        subject_id,
        WalletAddressInput(
            network_id=network_id,
            label=label,
            address="0x" + nibble * 40,
            purpose="observation",
        ),
        actor="operator",
    )


def _rpc_response(payload: object, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"content-type": "application/json"},
        json=payload,
    )


def test_enqueue_is_idempotent_and_fences_one_active_target(
    acquisition_fixture: AcquisitionFixture,
) -> None:
    _database, subject_id, store, ledger, _clock = acquisition_fixture
    _network, asset, address = _graph(store, subject_id)

    first = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=address.address_id,
        actor="operator",
        idempotency_key="balance-1",
    )
    replay = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=address.address_id,
        actor="operator",
        idempotency_key="balance-1",
    )
    assert replay == first
    with pytest.raises(ValueError, match="active run"):
        ledger.enqueue(
            subject_id,
            asset_id=asset.asset_id,
            address_id=address.address_id,
            actor="operator",
            idempotency_key="balance-2",
        )
    with pytest.raises(PermissionError):
        ledger.enqueue(
            subject_id,
            asset_id=asset.asset_id,
            address_id=address.address_id,
            actor="subject",
            idempotency_key="balance-3",
        )
    assert ledger.verify_integrity(subject_id) == {
        "wallet_balance_acquisition_runs": 1,
        "wallet_balance_acquisition_attempts": 0,
    }


def test_reads_revalidate_run_and_attempt_hashes(
    acquisition_fixture: AcquisitionFixture,
) -> None:
    database, subject_id, store, ledger, _clock = acquisition_fixture
    _network, asset, address = _graph(store, subject_id)
    run = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=address.address_id,
        actor="operator",
        idempotency_key="read-integrity",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE wallet_balance_acquisition_runs SET state_hash = ? WHERE run_id = ?",
            ("f" * 64, run.run_id),
        )
    with pytest.raises(IntegrityError, match="hash mismatch"):
        ledger.get(run.run_id, subject_id=subject_id)
    with pytest.raises(IntegrityError, match="hash mismatch"):
        ledger.list(subject_id)

    # Restore the run and create an executing attempt, then ensure the attempt
    # listing cannot expose a tampered immutable evidence row.
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM wallet_balance_acquisition_runs WHERE run_id = ?", (run.run_id,)
        ).fetchone()
        assert row is not None
        state_hash = ledger._run_hash(
            run_id=row["run_id"],
            subject_id=row["subject_id"],
            network_id=row["network_id"],
            asset_id=row["asset_id"],
            address_id=row["address_id"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            next_attempt_at=row["next_attempt_at"],
            last_error_code=row["last_error_code"],
            snapshot_id=row["snapshot_id"],
            claim_token=row["claim_token"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            updated_at=row["updated_at"],
        )
        connection.execute(
            "UPDATE wallet_balance_acquisition_runs SET state_hash = ? WHERE run_id = ?",
            (state_hash, run.run_id),
        )
    ledger.claim_due(subject_id, worker_owner="read-integrity-worker")
    with database.transaction() as connection:
        connection.execute(
            "DROP TRIGGER prevent_wallet_balance_acquisition_attempt_identity_update"
        )
        connection.execute("DROP TRIGGER validate_wallet_balance_acquisition_attempt_transition")
        connection.execute(
            "UPDATE wallet_balance_acquisition_attempts SET state_hash = ? WHERE run_id = ?",
            ("e" * 64, run.run_id),
        )
    with pytest.raises(IntegrityError, match="attempt hash mismatch"):
        ledger.attempts(run.run_id, subject_id=subject_id)


def test_claim_due_cancels_queued_target_revoked_after_enqueue(
    acquisition_fixture: AcquisitionFixture,
) -> None:
    _database, subject_id, store, ledger, _clock = acquisition_fixture
    _network, asset, address = _graph(store, subject_id)
    run = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=address.address_id,
        actor="operator",
        idempotency_key="revoked-before-claim",
    )
    store.revoke_asset(asset.asset_id, reason="retired", actor="operator", subject_id=subject_id)
    store.revoke_address(
        address.address_id, reason="retired", actor="operator", subject_id=subject_id
    )
    assert ledger.claim_due(subject_id, worker_owner="worker-a") == []
    cancelled = ledger.get(run.run_id, subject_id=subject_id)
    assert cancelled.status == "cancelled"
    assert cancelled.last_error_code == "wallet_rpc_target_revoked"
    assert ledger.attempts(run.run_id, subject_id=subject_id) == []
    assert ledger.verify_integrity(subject_id)["wallet_balance_acquisition_runs"] == 1


def test_claim_limits_concurrency_and_reserves_actual_request_budget(
    acquisition_fixture: AcquisitionFixture,
) -> None:
    database, subject_id, store, _ledger, clock = acquisition_fixture
    network, asset, first_address = _graph(store, subject_id)
    second_address = _address(store, subject_id, network.network_id, "Second", "c")
    ledger = WalletBalanceAcquisitionLedger(
        database,
        clock=clock,
        max_concurrent=2,
        min_interval_seconds=0,
        window_seconds=60,
        request_limit_per_window=4,
        lease_seconds=10,
    )
    first = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=first_address.address_id,
        actor="operator",
        idempotency_key="budget-1",
    )
    second = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=second_address.address_id,
        actor="operator",
        idempotency_key="budget-2",
    )

    claims = ledger.claim_due(subject_id, worker_owner="worker-a", limit=2)
    assert {claim.run_id for claim in claims} == {first.run_id, second.run_id}
    assert ledger.claim_due(subject_id, worker_owner="worker-b", limit=1) == []
    budget = ledger.budget_status(subject_id, network.network_id)
    assert (budget.requests_used, budget.requests_reserved) == (0, 4)

    first_token = next(claim.claim_token for claim in claims if claim.run_id == first.run_id)
    assert first_token is not None
    failed = ledger.fail(
        first.run_id,
        subject_id=subject_id,
        claim_token=first_token,
        worker_owner="worker-a",
        error=WalletRPCError("wallet_rpc_timeout", request_count=1),
        actor="operator",
    )
    assert failed.status == "retry_wait"
    budget = ledger.budget_status(subject_id, network.network_id)
    assert (budget.requests_used, budget.requests_reserved) == (1, 2)

    second_token = next(claim.claim_token for claim in claims if claim.run_id == second.run_id)
    assert second_token is not None
    ledger.fail(
        second.run_id,
        subject_id=subject_id,
        claim_token=second_token,
        worker_owner="worker-a",
        error_code="wallet_rpc_remote_error",
        request_count=0,
        actor="operator",
    )
    clock.advance(1)
    assert ledger.claim_due(subject_id, worker_owner="worker-c", limit=1) == []
    clock.advance(1)
    retry_claims = ledger.claim_due(subject_id, worker_owner="worker-c", limit=1)
    assert len(retry_claims) == 1
    assert retry_claims[0].run_id == first.run_id


def test_lease_renewal_expiry_and_interrupted_recovery_require_explicit_retry(
    acquisition_fixture: AcquisitionFixture,
) -> None:
    _database, subject_id, store, ledger, clock = acquisition_fixture
    _network, asset, address = _graph(store, subject_id)
    run = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=address.address_id,
        actor="operator",
        idempotency_key="lease-1",
    )
    claim = ledger.claim_due(subject_id, worker_owner="worker-a")[0]
    assert claim.claim_token is not None
    clock.advance(1)
    renewed = ledger.renew(
        run.run_id,
        subject_id=subject_id,
        claim_token=claim.claim_token,
        worker_owner="worker-a",
    )
    assert renewed is not None
    assert renewed.lease_expires_at != claim.lease_expires_at

    clock.advance(11)
    assert ledger.recover_expired(subject_id) == 1
    unknown = ledger.get(run.run_id, subject_id=subject_id)
    assert unknown.status == "unknown"
    assert unknown.last_error_code == "wallet_rpc_lease_expired"
    assert ledger.attempts(run.run_id, subject_id=subject_id)[0].status == "unknown"

    queued = ledger.retry_unknown(
        run.run_id,
        subject_id=subject_id,
        actor="operator",
        reason="lease outcome reviewed",
    )
    assert queued.status == "queued"
    claim = ledger.claim_due(subject_id, worker_owner="worker-b")[0]
    assert ledger.recover_interrupted(subject_id) == 1
    interrupted = ledger.get(run.run_id, subject_id=subject_id)
    assert interrupted.status == "unknown"
    assert interrupted.last_error_code == "wallet_rpc_interrupted"
    assert len(ledger.attempts(run.run_id, subject_id=subject_id)) == 2
    assert claim.claim_token is not None
    assert ledger.verify_integrity(subject_id)["wallet_balance_acquisition_attempts"] == 2


def test_cancel_queued_and_retry_wait_but_not_running(
    acquisition_fixture: AcquisitionFixture,
) -> None:
    _database, subject_id, store, ledger, _clock = acquisition_fixture
    network, asset, address = _graph(store, subject_id)
    queued = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=address.address_id,
        actor="operator",
        idempotency_key="cancel-queued",
    )
    cancelled = ledger.cancel(
        queued.run_id,
        subject_id=subject_id,
        actor="operator",
        reason="operator stopped it",
    )
    assert cancelled.status == "cancelled"
    assert ledger.attempts(queued.run_id, subject_id=subject_id) == []

    address_two = _address(store, subject_id, network.network_id, "Second", "c")
    retry = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=address_two.address_id,
        actor="operator",
        idempotency_key="cancel-retry",
    )
    claim = ledger.claim_due(subject_id, worker_owner="worker-a")[0]
    failed = ledger.fail(
        retry.run_id,
        subject_id=subject_id,
        claim_token=claim.claim_token or "missing",
        worker_owner="worker-a",
        error_code="wallet_rpc_timeout",
        request_count=2,
        actor="operator",
    )
    assert failed.status == "retry_wait"
    cancelled_retry = ledger.cancel(
        retry.run_id,
        subject_id=subject_id,
        actor="operator",
        reason="retry no longer needed",
    )
    assert cancelled_retry.status == "cancelled"
    with pytest.raises(WalletAcquisitionRunError, match="cancel_not_allowed"):
        ledger.cancel(
            retry.run_id,
            subject_id=subject_id,
            actor="operator",
            reason="duplicate cancellation",
        )
    assert ledger.verify_integrity(subject_id)["wallet_balance_acquisition_runs"] == 2


def test_success_is_atomic_and_records_snapshot_attempt_run_and_audit(
    acquisition_fixture: AcquisitionFixture,
) -> None:
    database, subject_id, store, ledger, _clock = acquisition_fixture
    _network, asset, address = _graph(store, subject_id)
    run = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=address.address_id,
        actor="operator",
        idempotency_key="success-1",
    )
    claim = ledger.claim_due(subject_id, worker_owner="worker-a")[0]
    completed, snapshot = ledger.complete_success(
        run.run_id,
        subject_id=subject_id,
        claim_token=claim.claim_token or "missing",
        worker_owner="worker-a",
        balance="42",
        actor="operator",
    )
    assert completed.status == "succeeded"
    assert completed.snapshot_id == snapshot.snapshot_id
    assert store.get_balance_snapshot(snapshot.snapshot_id, subject_id=subject_id) == snapshot
    assert ledger.attempts(run.run_id, subject_id=subject_id)[0].snapshot_id == snapshot.snapshot_id
    with database.connection() as connection:
        actions = [
            row["action"]
            for row in connection.execute(
                "SELECT action FROM audit_records WHERE subject_id = ? "
                "AND action LIKE 'wallet_balance_acquisition_%' ORDER BY occurred_at, audit_id",
                (subject_id,),
            ).fetchall()
        ]
    assert set(actions) == {
        "wallet_balance_acquisition_queued",
        "wallet_balance_acquisition_succeeded",
    }
    assert len(actions) == 2
    assert ledger.verify_integrity(subject_id) == {
        "wallet_balance_acquisition_runs": 1,
        "wallet_balance_acquisition_attempts": 1,
    }


def test_completion_rolls_back_snapshot_when_run_cas_fails(
    acquisition_fixture: AcquisitionFixture,
) -> None:
    database, subject_id, store, ledger, _clock = acquisition_fixture
    _network, asset, address = _graph(store, subject_id)
    run = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=address.address_id,
        actor="operator",
        idempotency_key="atomic-cas",
    )
    claim = ledger.claim_due(subject_id, worker_owner="worker-a")[0]
    original = ledger.store._record_balance_snapshot_connection

    def insert_then_fail(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("forced post-insert failure")

    ledger.store._record_balance_snapshot_connection = insert_then_fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="post-insert"):
        ledger.complete_success(
            run.run_id,
            subject_id=subject_id,
            claim_token=claim.claim_token or "missing",
            worker_owner="worker-a",
            balance="7",
            actor="operator",
        )
    assert store.list_balance_snapshots(subject_id) == []
    with database.connection() as connection:
        row = connection.execute(
            "SELECT status FROM wallet_balance_acquisition_attempts WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert row is not None and row["status"] == "executing"


def test_runner_success_failure_and_unknown_paths_are_durable(
    acquisition_fixture: AcquisitionFixture,
) -> None:
    _database, subject_id, store, ledger, _clock = acquisition_fixture
    _network, asset, address = _graph(store, subject_id)
    requests: list[str] = []

    def success_handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        requests.append(method)
        result = "0x1" if method == "eth_chainId" else "0x2a"
        return _rpc_response({"jsonrpc": "2.0", "id": "noyra-wallet-balance-v1", "result": result})

    with httpx.Client(transport=httpx.MockTransport(success_handler)) as client:
        acquirer = WalletRPCBalanceAcquirer(store, client=client)
        runner = WalletBalanceAcquisitionRunner(
            ledger,
            acquirer=acquirer,
            worker_owner="runner-a",
        )
        ledger.enqueue(
            subject_id,
            asset_id=asset.asset_id,
            address_id=address.address_id,
            actor="operator",
            idempotency_key="runner-success",
        )
        result = runner.run_once(subject_id, actor="operator")
        assert result is not None and result.status == "succeeded"
        assert requests == ["eth_chainId", "eth_getBalance"]

    address_two = _address(store, subject_id, asset.network_id, "Second", "c")

    def remote_error_handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        if method == "eth_chainId":
            return _rpc_response(
                {"jsonrpc": "2.0", "id": "noyra-wallet-balance-v1", "result": "0x1"}
            )
        return _rpc_response({"jsonrpc": "2.0", "id": "noyra-wallet-balance-v1", "error": {}})

    with httpx.Client(transport=httpx.MockTransport(remote_error_handler)) as client:
        acquirer = WalletRPCBalanceAcquirer(store, client=client)
        runner = WalletBalanceAcquisitionRunner(ledger, acquirer=acquirer, worker_owner="runner-b")
        failed_run = ledger.enqueue(
            subject_id,
            asset_id=asset.asset_id,
            address_id=address_two.address_id,
            actor="operator",
            idempotency_key="runner-failure",
        )
        result = runner.run_once(subject_id, actor="operator")
        assert result is not None and result.status == "failed"
        assert result.last_error_code == "wallet_rpc_remote_error"
        assert ledger.attempts(failed_run.run_id, subject_id=subject_id)[0].request_count == 2

    address_three = _address(store, subject_id, asset.network_id, "Third", "d")

    def unexpected_handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unexpected provider failure")

    with httpx.Client(transport=httpx.MockTransport(unexpected_handler)) as client:
        acquirer = WalletRPCBalanceAcquirer(store, client=client)
        runner = WalletBalanceAcquisitionRunner(ledger, acquirer=acquirer, worker_owner="runner-c")
        unknown_run = ledger.enqueue(
            subject_id,
            asset_id=asset.asset_id,
            address_id=address_three.address_id,
            actor="operator",
            idempotency_key="runner-unknown",
        )
        result = runner.run_once(subject_id, actor="operator")
        assert result is not None and result.status == "unknown"
        assert result.last_error_code == "wallet_rpc_unexpected_failure"
        assert ledger.attempts(unknown_run.run_id, subject_id=subject_id)[0].status == "unknown"


def test_integrity_detects_state_audit_and_extra_evidence_tampering(
    acquisition_fixture: AcquisitionFixture,
) -> None:
    database, subject_id, store, ledger, _clock = acquisition_fixture
    _network, asset, address = _graph(store, subject_id)
    run = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=address.address_id,
        actor="operator",
        idempotency_key="tamper-1",
    )
    claim = ledger.claim_due(subject_id, worker_owner="worker-a")[0]
    ledger.fail(
        run.run_id,
        subject_id=subject_id,
        claim_token=claim.claim_token or "missing",
        worker_owner="worker-a",
        error_code="wallet_rpc_http_500",
        request_count=2,
        actor="operator",
    )
    assert ledger.verify_integrity(subject_id)["wallet_balance_acquisition_runs"] == 1

    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_audit_record_update")
        connection.execute(
            "UPDATE audit_records SET payload_json = ? "
            "WHERE action = 'wallet_balance_acquisition_failed'",
            (
                json.dumps(
                    {
                        "run_id": run.run_id,
                        "attempt_number": 1,
                        "error_code": "wallet_rpc_http_500",
                        "retryable": True,
                        "next_attempt_at": "not-a-time",
                    },
                    separators=(",", ":"),
                ),
            ),
        )
    with pytest.raises(IntegrityError, match="audit"):
        ledger.verify_integrity(subject_id)

    current = ledger.get(run.run_id, subject_id=subject_id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE audit_records SET payload_json = ? "
            "WHERE action = 'wallet_balance_acquisition_failed'",
            (
                canonical_json(
                    {
                        "run_id": run.run_id,
                        "attempt_number": 1,
                        "error_code": "wallet_rpc_http_500",
                        "retryable": True,
                        "next_attempt_at": current.next_attempt_at,
                    }
                ),
            ),
        )
    assert ledger.verify_integrity(subject_id)["wallet_balance_acquisition_runs"] == 1

    with database.transaction() as connection:
        connection.execute(
            "UPDATE wallet_balance_acquisition_runs SET state_hash = ? WHERE run_id = ?",
            ("f" * 64, run.run_id),
        )
    with pytest.raises(IntegrityError, match="hash mismatch"):
        ledger.verify_integrity(subject_id)


def test_runtime_export_and_store_integrity_include_acquisition_rows_without_cross_subject_leak(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_a = "Noyra-wallet-export-a"
    subject_b = "Noyra-wallet-export-b"
    for subject in (subject_a, subject_b):
        IdentityStore(database).ensure(subject, content_hash({"subject": subject}))
    clocks = {subject_a: FakeClock(), subject_b: FakeClock()}
    stores = {subject: WalletStore(database) for subject in (subject_a, subject_b)}
    ledgers = {
        subject: WalletBalanceAcquisitionLedger(
            database, clock=clocks[subject], min_interval_seconds=0
        )
        for subject in (subject_a, subject_b)
    }
    runs = []
    for subject in (subject_a, subject_b):
        _network, asset, address = _graph(
            stores[subject],
            subject,
            suffix=f"-{subject[-1]}",
            chain_id=1 if subject == subject_a else 2,
        )
        runs.append(
            ledgers[subject].enqueue(
                subject,
                asset_id=asset.asset_id,
                address_id=address.address_id,
                actor="operator",
                idempotency_key=f"export-{subject}",
            )
        )

    artifact = RuntimeLogExporter(database).export(subject_a, actor="test")
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        table_names = {entry["name"] for entry in manifest["tables"]}
        run_entry = next(
            entry
            for entry in manifest["tables"]
            if entry["name"] == "wallet_balance_acquisition_runs"
        )
        exported = archive.read(run_entry["file"]).decode()
    assert "wallet_balance_acquisition_runs" in table_names
    assert runs[0].run_id in exported
    assert runs[1].run_id not in exported
    assert stores[subject_a].verify_integrity(subject_a)["wallet_balance_acquisition_runs"] == 1


def test_schema_reinitialization_repairs_an_early_v54_acquisition_trigger(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "repair.sqlite3")
    subject_id = "Noyra-wallet-trigger-repair"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    store = WalletStore(database)
    _network, asset, address = _graph(store, subject_id)
    ledger = WalletBalanceAcquisitionLedger(database, min_interval_seconds=0)
    run = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=address.address_id,
        actor="operator",
        idempotency_key="repair-trigger",
    )
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_wallet_balance_acquisition_run_delete")
        connection.execute(
            "CREATE TRIGGER prevent_wallet_balance_acquisition_run_delete "
            "BEFORE DELETE ON wallet_balance_acquisition_runs BEGIN SELECT NULL; END"
        )
    Database(database.path)
    with (
        pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"),
        database.transaction() as connection,
    ):
        connection.execute(
            "DELETE FROM wallet_balance_acquisition_runs WHERE run_id = ?", (run.run_id,)
        )


def test_status_summary_is_bounded_and_attempt_listing_is_subject_scoped(
    acquisition_fixture: AcquisitionFixture,
) -> None:
    _database, subject_id, store, ledger, _clock = acquisition_fixture
    _network, asset, address = _graph(store, subject_id)
    run = ledger.enqueue(
        subject_id,
        asset_id=asset.asset_id,
        address_id=address.address_id,
        actor="operator",
        idempotency_key="summary-1",
    )
    summary = ledger.status_summary(subject_id)
    assert summary["counts"]["queued"] == 1
    assert summary["active"] == 1
    assert summary["attempts"] == 0
    assert "claim_token" not in summary
    assert ledger.attempts(run.run_id, subject_id=subject_id) == []
    with pytest.raises(NotFoundError):
        ledger.attempts(run.run_id, subject_id="Noyra-wallet-other")
