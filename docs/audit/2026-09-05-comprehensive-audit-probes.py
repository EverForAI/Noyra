"""Read-only audit probes: temporary fixtures and mock signers, never live funds.

Assertions intentionally describe confirmed defects at audit commit 925f1cf.
These are evidence probes, not passing acceptance tests for the desired behavior.
"""

# Fixture imports follow explicit test-path setup; full fixture unpacking aids comparison.
# ruff: noqa: E402, F811, RUF059

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from noyra.capability import CapabilityGrant, CapabilityStore, ToolRunner
from noyra.core import SubjectKernel
from noyra.core.admission import OperationInvalidated, RuntimeAdmissionGate, bind_lease
from noyra.core.locking import ProcessLock
from noyra.core.types import utc_now
from noyra.wallet import (
    BountyInput,
    MockSigner,
    PaymentPolicyInput,
    SubmissionInput,
    WalletAddressInput,
    WalletBalanceSnapshotInput,
    WalletEconomyStore,
    WalletPaymentExecutionEngine,
    WalletStore,
)
from noyra.wallet.execution import WalletSignerError
from test_wallet import _http_json, wallet_http  # noqa: F401
from test_wallet_economy import _goal
from test_wallet_execution import fixture
from test_wallet_reward_workflow import _accept, _fixture, _open, _submission


def policy(economy, sid, network_id, asset_id, **changes):
    values = dict(
        mode="automatic",
        allowed_network_ids=[network_id],
        allowed_asset_ids=[asset_id],
        per_order_limit="20",
        automatic_max_amount="20",
    )
    values.update(changes)
    economy.update_policy(
        sid,
        PaymentPolicyInput(**values),
        expected_version=economy.get_policy(sid).policy_version,
        actor="operator",
    )


def order(economy, sid, network_id, asset_id, key, amount="10"):
    now = datetime.now(UTC)
    bounty = economy.create_bounty(
        sid,
        BountyInput(
            title="Audit proof",
            description="Synthetic audit fixture",
            acceptance_criteria=["proof"],
            network_id=network_id,
            asset_id=asset_id,
            reward_amount=amount,
            opens_at=(now - timedelta(minutes=1)).isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
            max_submissions=1,
            reward_slots=1,
            goal_id="goal_execution",
            idempotency_key=key,
        ),
        actor="operator",
    )
    economy.publish_bounty(bounty.bounty_id, sid, actor="operator")
    submission = economy.submit(
        bounty.bounty_id,
        sid,
        SubmissionInput(
            counterparty="audit",
            content="proof",
            recipient_address="0xB111111111111111111111111111111111111111",
            idempotency_key=key,
            consent_version=1,
        ),
    )
    economy.decide_submission(
        submission.submission_id, sid, accepted=True, reason="audit", actor="operator"
    )
    return economy.order_for_submission(submission.submission_id, sid)


def test_a01_reserved_order_executes_after_policy_disabled(tmp_path):
    db, sid, network, asset, source, reserved = fixture(tmp_path)
    economy = WalletEconomyStore(db)
    policy(economy, sid, network.network_id, asset.asset_id, mode="disabled")
    signer = MockSigner()
    result = WalletPaymentExecutionEngine(db, signer).execute_order(
        reserved.order_id, sid, actor="operator"
    )
    assert result.status == "broadcast" and len(signer.requests) == 1


def test_a02_unknown_retry_bypasses_emergency_pause(tmp_path):
    db, sid, network, asset, source, reserved = fixture(tmp_path)
    signer = MockSigner(lose_first_response=True)
    engine = WalletPaymentExecutionEngine(db, signer)
    assert engine.execute_order(reserved.order_id, sid, actor="operator").status == "unknown"
    policy(engine.economy, sid, network.network_id, asset.asset_id, emergency_paused=True)
    result = engine.retry_unknown(reserved.order_id, sid, actor="operator", reason="audit")
    assert result.status == "broadcast" and len(signer.requests) == 2


def test_a03_receipt_without_block_is_settled(tmp_path):
    db, sid, network, asset, source, reserved = fixture(tmp_path)
    signer = MockSigner()
    engine = WalletPaymentExecutionEngine(db, signer)
    result = engine.execute_order(reserved.order_id, sid, actor="operator")
    signer.set_receipt(result.tx_hash, chain_id=1, status=1, block_number=None)
    assert engine.poll_receipt(result.execution_id, sid, actor="operator").status == "confirmed"
    assert engine.verify_confirmed_receipt(result.execution_id, sid).status == "confirmed"


def test_a04_watch_only_balance_authorizes_spending(tmp_path):
    db, sid, network, asset, source, reserved = fixture(tmp_path)
    wallets, economy = WalletStore(db), WalletEconomyStore(db)
    watch = wallets.register_address(
        sid,
        WalletAddressInput(
            network_id=network.network_id,
            label="Not controlled",
            address="0xC111111111111111111111111111111111111111",
            purpose="observation",
        ),
        actor="operator",
    )
    for address, balance in ((source, "0"), (watch, "100")):
        wallets.record_balance_snapshot(
            sid,
            WalletBalanceSnapshotInput(
                asset_id=asset.asset_id,
                address_id=address.address_id,
                balance=balance,
                source="audit",
                observed_at=utc_now(),
            ),
            actor="operator",
        )
    policy(economy, sid, network.network_id, asset.asset_id, min_balance="1")
    created = order(economy, sid, network.network_id, asset.asset_id, "watch-funds")
    assert created.status == "reserved"


def test_a05_outstanding_reservations_are_not_subtracted(tmp_path):
    db, sid, network, asset, source, reserved = fixture(tmp_path)
    wallets, economy = WalletStore(db), WalletEconomyStore(db)
    wallets.record_balance_snapshot(
        sid,
        WalletBalanceSnapshotInput(
            asset_id=asset.asset_id,
            address_id=source.address_id,
            balance="11",
            source="audit",
            observed_at=utc_now(),
        ),
        actor="operator",
    )
    policy(economy, sid, network.network_id, asset.asset_id, min_balance="1")
    created = order(economy, sid, network.network_id, asset.asset_id, "double-reserve")
    assert created.status == "reserved"
    assert (
        sum(int(row.amount) for row in economy.list_orders(sid) if row.status == "reserved") == 20
    )


def test_a06_policy_sum_overflows_with_valid_amounts(tmp_path):
    db, sid, network, asset, source, reserved = fixture(tmp_path)
    economy = WalletEconomyStore(db)
    policy(
        economy,
        sid,
        network.network_id,
        asset.asset_id,
        per_order_limit="5000000000000000000",
        automatic_max_amount="5000000000000000000",
    )
    order(economy, sid, network.network_id, asset.asset_id, "large-one", "5000000000000000000")
    order(economy, sid, network.network_id, asset.asset_id, "large-two", "5000000000000000000")
    with pytest.raises(sqlite3.OperationalError, match="integer overflow"):
        order(economy, sid, network.network_id, asset.asset_id, "overflow")


def test_a07_order_timestamp_tamper_passes_integrity(tmp_path):
    db, sid, network, asset, source, reserved = fixture(tmp_path)
    with db.transaction() as c:
        c.execute("DROP TRIGGER prevent_wallet_order_identity_update")
        c.execute("UPDATE wallet_payment_orders SET created_at='2000-01-01T00:00:00+00:00'")
    assert WalletEconomyStore(db).verify_integrity(sid)["wallet_payment_orders"] == 1


def test_a08_execution_timestamp_tamper_passes_integrity(tmp_path):
    db, sid, network, asset, source, reserved = fixture(tmp_path)
    engine = WalletPaymentExecutionEngine(db, MockSigner())
    engine.execute_order(reserved.order_id, sid, actor="operator")
    with db.transaction() as c:
        c.execute("DROP TRIGGER prevent_wallet_execution_identity_update")
        c.execute("DROP TRIGGER validate_wallet_execution_transition")
        c.execute("UPDATE wallet_payment_executions SET created_at='invalid',updated_at='invalid'")
    assert engine.verify_integrity(sid)["wallet_payment_executions"] == 1


def test_a09_nonactionable_head_starves_reserved_workflow_submission(tmp_path):
    f = _fixture(tmp_path, reward_slots=2)
    opened = _open(f)
    policy(f.economy, f.subject_id, f.network_id, f.asset_id, mode="conditional_confirmation")
    first = f.workflow.submit_public(
        opened.workflow_id, f.subject_id, _submission("first", network_id=f.network_id)
    )
    _accept(f, first.submission.submission_id, key="first-decision")
    policy(f.economy, f.subject_id, f.network_id, f.asset_id)
    second = f.workflow.submit_public(
        opened.workflow_id, f.subject_id, _submission("second", network_id=f.network_id)
    )
    assert (
        _accept(f, second.submission.submission_id, key="second-decision").order_status
        == "reserved"
    )
    for _ in range(3):
        assert f.workflow.execute_ready(f.subject_id, actor="operator", limit=1) == []
    assert f.signer.requests == []


def test_a10_pause_after_prepare_does_not_fence_external_signing(tmp_path, monkeypatch):
    db, sid, network, asset, source, reserved = fixture(tmp_path)
    signer = MockSigner()
    engine = WalletPaymentExecutionEngine(db, signer)
    gate = RuntimeAdmissionGate(sid)
    original = engine._prepare

    def pause_after_prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        gate.invalidate()
        return result

    monkeypatch.setattr(engine, "_prepare", pause_after_prepare)
    with (
        gate.operation("audit") as lease,
        bind_lease(lease),
        pytest.raises(OperationInvalidated),
    ):
        engine.execute_order(reserved.order_id, sid, actor="operator")
    assert len(signer.requests) == 1
    assert engine._order_execution(reserved.order_id, sid).status == "signing"


def test_a11_prebroadcast_failure_consumes_nonce(tmp_path):
    db, sid, network, asset, source, reserved = fixture(tmp_path)

    class RejectFirstSigner(MockSigner):
        def sign_and_broadcast(self, transfer, *, request_id):
            if transfer.nonce == 0:
                raise WalletSignerError("rejected before signing")
            return super().sign_and_broadcast(transfer, request_id=request_id)

    signer = RejectFirstSigner()
    engine = WalletPaymentExecutionEngine(db, signer)
    assert engine.execute_order(reserved.order_id, sid, actor="operator").status == "failed"
    second = order(engine.economy, sid, network.network_id, asset.asset_id, "nonce-gap")
    result = engine.execute_order(second.order_id, sid, actor="operator")
    assert result.nonce == 1 and signer.requests[0].nonce == 1


def test_a12_kernel_initializes_after_releasing_ownership_probe(tmp_path, monkeypatch):
    path = tmp_path / "race.sqlite3"
    competitor = ProcessLock(f"{path.resolve()}.lock")
    original = ProcessLock.release
    intercepted = False

    def acquire_competitor_after_release(lock):
        nonlocal intercepted
        original(lock)
        if lock is not competitor and not intercepted:
            intercepted = True
            competitor.acquire()

    monkeypatch.setattr(ProcessLock, "release", acquire_competitor_after_release)
    kernel = None
    try:
        kernel = SubjectKernel(path, "Noyra-audit", "audit-genesis")
        assert competitor.held and not kernel.process_lock.held
        assert kernel.identity is not None and kernel.identity.subject_id == "Noyra-audit"
        assert path.is_file()
    finally:
        original(competitor)
        if kernel is not None:
            kernel.close()


def filesystem_fixture(tmp_path, capability_type):
    root = tmp_path / "authorized"
    root.mkdir()
    kernel = SubjectKernel(tmp_path / "fs.sqlite3", "Noyra-audit-fs", "audit-genesis")
    kernel.boot()
    kernel.orient()
    kernel.activate()
    CapabilityStore(kernel.database).grant(
        kernel.subject_id,
        CapabilityGrant(
            capability_type=capability_type,
            scope={"root": str(root)},
            issuer="audit",
            rate_limit_per_hour=10,
            side_effect=capability_type == "filesystem_write",
        ),
        actor="operator",
    )
    return kernel, root, ToolRunner(kernel.database, max_file_bytes=1024)


def test_a13_authorized_write_follows_replaced_directory(tmp_path, monkeypatch):
    kernel, root, runner = filesystem_fixture(tmp_path, "filesystem_write")
    folder = root / "subdir"
    folder.mkdir()
    outside = tmp_path / "outside-grant"
    outside.mkdir()
    moved = root / "original-subdir"
    original = runner.actions.start
    swapped = False

    def swap_after_authorization(*args, **kwargs):
        nonlocal swapped
        result = original(*args, **kwargs)
        folder.rename(moved)
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(folder), str(outside)],
                check=True,
                capture_output=True,
            )
        else:
            folder.symlink_to(outside, target_is_directory=True)
        swapped = True
        return result

    monkeypatch.setattr(runner.actions, "start", swap_after_authorization)
    try:
        result = runner.write_text(kernel.subject_id, folder / "proof.txt", "audit marker")
        assert result.status == "succeeded"
        assert (outside / "proof.txt").read_text(encoding="utf-8") == "audit marker"
    finally:
        if swapped:
            if os.name == "nt":
                folder.rmdir()
            else:
                folder.unlink()
        kernel.close()


def test_a14_file_growth_bypasses_read_byte_limit(tmp_path, monkeypatch):
    kernel, root, runner = filesystem_fixture(tmp_path, "filesystem_read")
    target = root / "growing.txt"
    target.write_text("small", encoding="utf-8")
    original = Path.read_text

    def grow_before_read(path, *args, **kwargs):
        if path == target:
            path.write_text("x" * 2048, encoding="utf-8")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", grow_before_read)
    try:
        result = runner.read_text(kernel.subject_id, target)
        assert result.status == "succeeded" and len(result.content) > runner.max_file_bytes
    finally:
        kernel.close()


def test_a15_disabled_policy_still_allows_pending_confirmation(tmp_path):
    db, sid, network, asset, source, reserved = fixture(tmp_path)
    economy = WalletEconomyStore(db)
    policy(economy, sid, network.network_id, asset.asset_id, mode="conditional_confirmation")
    pending = order(economy, sid, network.network_id, asset.asset_id, "confirm-disabled")
    assert pending.status == "awaiting_confirmation"
    policy(economy, sid, network.network_id, asset.asset_id, mode="disabled")
    assert economy.confirm_order(pending.order_id, sid, actor="operator").status == "reserved"


def test_a16_delayed_confirmations_bypass_current_day_budget(tmp_path):
    db, sid, network, asset, source, reserved = fixture(tmp_path)
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    current = today.isoformat()
    economy = WalletEconomyStore(db, clock=lambda: current)
    policy(
        economy,
        sid,
        network.network_id,
        asset.asset_id,
        mode="conditional_confirmation",
        daily_limit="10",
        daily_order_limit=1,
    )
    first = order(economy, sid, network.network_id, asset.asset_id, "delayed-one")
    second = order(economy, sid, network.network_id, asset.asset_id, "delayed-two")
    current = (today + timedelta(days=1)).isoformat()
    assert economy.confirm_order(first.order_id, sid, actor="operator").status == "reserved"
    assert economy.confirm_order(second.order_id, sid, actor="operator").status == "reserved"


def test_a17_public_get_mutates_wallet_during_quarantine(wallet_http):
    server, base_url, _token = wallet_http
    from test_wallet import _register_graph

    network, asset, _address = _register_graph(
        server.wallet_economy.wallets, server.kernel.subject_id
    )
    now = datetime.now(UTC)
    bounty = server.wallet_economy.create_bounty(
        server.kernel.subject_id,
        BountyInput(
            title="Expiry audit",
            description="Synthetic",
            acceptance_criteria=["proof"],
            network_id=network.network_id,
            asset_id=asset.asset_id,
            reward_amount="1",
            opens_at=(now - timedelta(minutes=1)).isoformat(),
            expires_at=(now + timedelta(hours=1)).isoformat(),
            max_submissions=1,
            reward_slots=1,
            idempotency_key="public-expiry",
            goal_id=_goal(server.kernel.database, server.kernel.subject_id),
        ),
        actor="operator",
    )
    server.wallet_economy.publish_bounty(
        bounty.bounty_id, server.kernel.subject_id, actor="operator"
    )
    server.wallet_economy.clock = lambda: (now + timedelta(hours=2)).isoformat()
    server.kernel.admission.quarantine()
    code, payload = _http_json(base_url, "/api/v1/bounties")
    assert code == 200 and payload == []
    with server.kernel.database.connection() as c:
        row = c.execute(
            "SELECT status FROM wallet_bounties WHERE bounty_id=?", (bounty.bounty_id,)
        ).fetchone()
    assert row["status"] == "expired"


def test_a18_anomaly_block_does_not_block_anomalous_balance(tmp_path):
    db, sid, network, asset, source, reserved = fixture(tmp_path)
    wallets, economy = WalletStore(db), WalletEconomyStore(db)
    now = datetime.now(UTC)
    for offset, balance in ((-1, "100"), (0, "1000")):
        wallets.record_balance_snapshot(
            sid,
            WalletBalanceSnapshotInput(
                asset_id=asset.asset_id,
                address_id=source.address_id,
                balance=balance,
                source="audit",
                observed_at=(now + timedelta(minutes=offset)).isoformat(),
            ),
            actor="operator",
        )
    assert wallets.observation_health(sid)["anomalous_pairs"] == 1
    policy(
        economy,
        sid,
        network.network_id,
        asset.asset_id,
        anomaly_block=True,
        min_balance="1",
        max_observation_age_seconds=60,
    )
    created = order(economy, sid, network.network_id, asset.asset_id, "anomaly-ignored")
    assert created.status == "reserved"
