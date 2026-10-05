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
    assert status["blocked"]
    assert not status["lifecycle"]["model_calls"]["count_is_lower_bound"]
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


@pytest.mark.asyncio
async def test_capacity_allows_real_queued_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from pydantic import SecretStr

    from noyra.interaction import InteractionStore
    from noyra.interaction.transport import DeliveryDispatcher, TransportInput

    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "1")
    service = NoyraService(
        ServiceSettings(
            data_dir=tmp_path,
            subject_id="Noyra-cap-delivery",
            genesis_hash="a" * 64,
            port=0,
            integrity_mode="off",
        )
    )
    service.boot()
    subject = service.kernel.subject_id
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "delivered-once"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dispatcher = DeliveryDispatcher(
            service.kernel.database, service.http.transports, client=client
        )
        monkeypatch.setattr(service.http, "deliveries", dispatcher)
        try:
            service.http.transports.configure(
                subject,
                TransportInput(
                    channel="qq",
                    label="fixture",
                    endpoint="https://api.sgroup.qq.com",
                    settings={"target_type": "group"},
                    credentials={"access_token": SecretStr("test")},
                ),
                actor="operator",
            )
            InteractionStore(service.kernel.database).send(subject, "qq", "recipient", "accepted")
            assert dispatcher.enqueue_pending(subject) == 1
            ModelLedger(service.kernel.database).prepare_call(
                subject, "p", "m", "test", "a" * 64, "full"
            )
            assert await service._active_tick() == "evidence_capacity_reached"
            assert await service._active_tick() == "evidence_capacity_reached"
            assert len(requests) == 1
            with service.kernel.database.connection() as c:
                assert (
                    c.execute("SELECT status FROM interaction_deliveries").fetchone()[0]
                    == "delivered"
                )
        finally:
            service.http.close()
            service.kernel.close()


def test_embedding_cap_preserves_replay_recovery_and_circuit_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.model import EmbeddingBudgetLimits, EmbeddingCircuitPolicy, EmbeddingLedger
    from noyra.model.embedding_ledger import EmbeddingUsageRecord

    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "2")
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-embedding-cap"
    IdentityStore(db).ensure(subject, "a" * 64)
    ledger = EmbeddingLedger(db)
    policy = EmbeddingCircuitPolicy(1, 1)

    def authorize(key: str, resource: str = "resource") -> tuple[EmbeddingUsageRecord, bool]:
        return ledger.authorize(
            subject,
            resource,
            "provider",
            "model",
            "test",
            "a" * 64,
            key,
            EmbeddingBudgetLimits(100, 10000, 10000),
            policy,
            text_count=1,
            reserved_tokens=10,
            reserved_cost_microusd=10,
        )

    first, _ = authorize("one")
    ledger.start(first.usage_id)
    ledger.fail(
        first.usage_id, error_code="unavailable", usage_unknown=False, circuit_policy=policy
    )
    second, _ = authorize("two", "another-resource")
    ledger.start(second.usage_id)
    with pytest.raises(InvalidTransitionError, match="evidence_capacity_reached"):
        authorize("three", "third-resource")
    assert authorize("one")[1] is False
    restarted = EmbeddingLedger(Database(db.path))
    assert restarted.recover_interrupted(subject) == 1
    assert restarted.recover_interrupted(subject) == 0
    counts = RetentionManager(db).evidence_capacity(subject)["counts"]
    assert counts["embedding_usage_entries"] == 2
    assert counts["embedding_circuit_transitions"] == 4
    assert restarted.verify_integrity(subject)["embedding_circuit_transitions"] == 4
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM embedding_circuit_states").fetchone()[0] == 2


def test_counter_migration_backfill_rollback_and_tamper_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import noyra.core.database as schema
    from noyra.core import IntegrityRegistry
    from noyra.core.errors import IntegrityError

    subject = "Noyra-counter-upgrade"
    with monkeypatch.context() as old:
        old.setattr(schema, "CURRENT_SCHEMA_VERSION", 79)
        db = Database(tmp_path / "noyra.sqlite3")
        IdentityStore(db).ensure(subject, "a" * 64)
        for i in range(3):
            ModelLedger(db).prepare_call(subject, "p", "m", "test", "a" * 64, str(i))
    db = Database(db.path)
    assert RetentionManager(db).evidence_capacity(subject)["counts"]["model_calls"] == 3
    with pytest.raises(RuntimeError, match="rollback"), db.transaction() as c:
        c.execute(
            "INSERT INTO model_calls(call_id,subject_id,provider,model,purpose,request_hash,"
            "idempotency_key,status,created_at) VALUES(?,?,'p','m','test',?,?,'prepared',?)",
            ("rolled-back", subject, "a" * 64, "rolled-back", "2026-10-05T00:00:00+00:00"),
        )
        assert (
            c.execute(
                "SELECT row_count FROM evidence_row_counts WHERE table_name='model_calls'"
            ).fetchone()[0]
            == 4
        )
        raise RuntimeError("rollback")
    assert (
        RetentionManager(Database(db.path)).evidence_capacity(subject)["counts"]["model_calls"] == 3
    )
    with db.transaction() as c:
        c.execute("UPDATE evidence_row_counts SET row_count=0 WHERE table_name='model_calls'")
    db = Database(db.path)
    report = IntegrityRegistry().run(
        db,
        subject,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.evidence_counts",),
    )
    assert report.status == "corrupt"
    with db.transaction() as c:
        c.execute("DELETE FROM evidence_row_counts WHERE table_name='model_calls'")
    with pytest.raises(IntegrityError, match="counter contract"):
        ModelLedger(db).prepare_call(subject, "p", "m", "test", "a" * 64, "missing-counter")


def test_capacity_query_cost_does_not_grow_with_history(tmp_path: Path) -> None:
    from noyra.core.evidence_capacity import evidence_capacity_status, require_evidence_capacity

    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-counter-cost"
    IdentityStore(db).ensure(subject, "a" * 64)

    def query_steps() -> int:
        steps = 0

        def progress() -> int:
            nonlocal steps
            steps += 1
            return 0

        with db.transaction() as c:
            c.set_progress_handler(progress, 1)
            require_evidence_capacity(c, subject, "model_calls")
            evidence_capacity_status(c, subject)
            c.set_progress_handler(None, 0)
        return steps

    empty_steps = query_steps()
    with db.transaction() as c:
        # Real historical rows and triggers; one transaction keeps this fixture fast.
        c.executemany(
            "INSERT INTO model_calls(call_id,subject_id,provider,model,purpose,request_hash,"
            "idempotency_key,status,created_at) VALUES(?,?,'p','m','test',?,?,'prepared',?)",
            (
                (str(i), subject, "a" * 64, str(i), "2026-10-05T00:00:00+00:00")
                for i in range(10000)
            ),
        )
    assert RetentionManager(db).evidence_capacity(subject)["counts"]["model_calls"] == 10000
    assert query_steps() <= empty_steps + 20
