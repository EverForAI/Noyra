import asyncio
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.actions import ActionLedger
from noyra.core.at_rest import BackupKeyring, EncryptedBackupManager
from noyra.core.errors import InvalidTransitionError
from noyra.core.retention import RetentionManager
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.types import content_hash
from noyra.model.ledger import ModelLedger
from noyra.model.types import BudgetLimits, ModelUsage
from noyra.service import NoyraService, ServiceSettings


def test_evidence_limit_preserves_idempotency_and_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "1")
    subject = "Noyra-evidence-capacity"
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    ledger = ModelLedger(db)
    args = (subject, "provider", "model", "test", content_hash({"prompt": "test"}), "once")
    call, _ = ledger.prepare_call(*args)
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(100, 10000, 10000, 10000),
        reserved_input_tokens=1,
        reserved_output_tokens=1,
        reserved_cost_microusd=1,
    )
    ledger.start_attempt(attempt.attempt_id)
    ledger.finish_attempt(attempt.attempt_id, "failed", usage=ModelUsage(0, 0), cost_microusd=0)
    ledger.finish_call(call.call_id, "failed")
    replay, created = ledger.prepare_call(*args)
    assert not created
    assert replay.call_id == call.call_id
    with pytest.raises(InvalidTransitionError, match="evidence_capacity_reached"):
        ledger.prepare_call(*args[:-1], "different")
    status = RetentionManager(db).evidence_capacity(subject)
    assert status["blocked"] is True
    assert status["counts"]["model_calls"] == 1
    assert status["counts"]["model_attempts"] == 1
    assert ledger.get_call(call.call_id).status == "failed"


def test_action_limit_still_allows_completion_and_duplicate_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "1")
    subject = "Noyra-action-capacity"
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    ledger = ActionLedger(db)
    action = ledger.prepare(subject, "test", "test", "target", {}, idempotency_key="once")
    with pytest.raises(InvalidTransitionError, match="evidence_capacity_reached"):
        ledger.prepare(subject, "test", "test", "another", {}, idempotency_key="twice")
    ledger.start(action.action_id)
    ledger.finish(action.action_id, "succeeded", {})
    assert (
        ledger.prepare(subject, "test", "test", "target", {}, idempotency_key="once").action_id
        == action.action_id
    )
    assert ledger.verify_integrity(subject)["behavior_logs"] == 1


def test_concurrent_admission_cannot_overrun_finite_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "4")
    subject = "Noyra-cap-race"
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure(subject, "a" * 64)
    ledger = ModelLedger(database)

    def prepare(number: int) -> bool:
        try:
            ledger.prepare_call(subject, "provider", "model", "test", "a" * 64, str(number))
            return True
        except InvalidTransitionError:
            return False

    with ThreadPoolExecutor(max_workers=8) as workers:
        assert sum(workers.map(prepare, range(12))) == 4
    assert RetentionManager(database).evidence_capacity(subject)["counts"]["model_calls"] == 4


def test_bounded_history_survives_cleanup_export_and_authenticated_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "3")
    subject = "Noyra-cap-recovery"
    data = tmp_path / "data"
    database = Database(data / "noyra.sqlite3")
    IdentityStore(database).ensure(subject, "a" * 64)
    ledger = ModelLedger(database)
    for number in range(3):
        call, _ = ledger.prepare_call(subject, "provider", "model", "test", "a" * 64, str(number))
        ledger.finish_call(call.call_id, "failed")
    retention = RetentionManager(database)
    for _ in range(3):
        assert retention.run_batch(subject)["failed_reason"] is None
    status = retention.evidence_capacity(subject)
    assert status["blocked"] and status["lifecycle"]["model_calls"]["count_is_lower_bound"]
    with zipfile.ZipFile(
        io.BytesIO(RuntimeLogExporter(database).export(subject, actor="operator").content)
    ) as archive:
        calls = [json.loads(line) for line in archive.read("tables/model_calls.jsonl").splitlines()]
    assert len(calls) == 3
    keyring = tmp_path / "offline" / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(data, keyring)
    backup = tmp_path / "history.noyra-backup"
    manager.create(backup)
    restored = manager.restore(backup, tmp_path / "restored")
    recovered = Database(restored / "noyra.sqlite3")
    replay, created = ModelLedger(recovered).prepare_call(
        subject, "provider", "model", "test", "a" * 64, "0"
    )
    assert not created and replay.status == "failed"
    with recovered.connection() as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_capacity_exhaustion_preserves_wallet_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any, cast

    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "1")
    subject = "Noyra-cap-maintenance"
    service = NoyraService(
        ServiceSettings(
            data_dir=tmp_path,
            subject_id=subject,
            genesis_hash="a" * 64,
            port=0,
            integrity_mode="off",
        )
    )
    service.boot()
    completed: list[str] = []

    def maintenance(*args: Any, **kwargs: Any) -> list[object]:
        completed.append("existing-wallet-work")
        return []

    def new_work(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("new reward workflows must remain stopped")

    monkeypatch.setattr(service.http, "wallet_execution", cast(Any, object()))
    monkeypatch.setattr(service.http.wallet_rewards, "execute_ready", maintenance)
    monkeypatch.setattr(service, "_advance_autonomous_reward_workflows", new_work)
    try:
        ModelLedger(service.kernel.database).prepare_call(
            subject, "provider", "model", "test", "a" * 64, "at-limit"
        )
        assert asyncio.run(service._active_tick()) == "evidence_capacity_reached"
        assert completed == ["existing-wallet-work"]
    finally:
        service.http.close()
        service.kernel.close()
