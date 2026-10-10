from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from noyra.core import Database, IdentityStore, IntegrityRegistry
from noyra.core.errors import IntegrityError, InvalidTransitionError
from noyra.core.evidence_capacity import evidence_counts
from noyra.core.locking import ProcessLock
from noyra.core.retention import RetentionManager
from noyra.core.types import content_hash
from noyra.model import CognitiveResourceGroupInput, CognitiveResourceStore, ModelMessage
from noyra.model.errors import ProviderCallError
from noyra.model.fake import FakeProvider
from noyra.model.resources import RoutedModelGateway
from noyra.model.types import ModelUsage, ProviderResponse


class Answer(BaseModel):
    answer: str


def setup_route(tmp_path: Path) -> tuple[Database, str, RoutedModelGateway, FakeProvider, str, str]:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-route-capacity"
    IdentityStore(db).ensure(subject, "a" * 64)
    resources = CognitiveResourceStore(db, tmp_path / "secrets")
    group = resources.configure(
        subject,
        CognitiveResourceGroupInput(
            pool="economy",
            label="fixture",
            model="model",
            base_url="https://example.test/v1",
            api_keys=(SecretStr("fixture-key"),),
            max_attempts=1,
        ),
        actor="operator",
    )
    provider = FakeProvider([ProviderResponse('{"answer":"ok"}', ModelUsage(2, 1))])
    gateway = RoutedModelGateway(db, subject, resources, provider_factory=lambda _: provider)
    key = resources.keys(group.group_id, subject_id=subject)[0]
    return db, subject, gateway, provider, group.group_id, key.key_id


def decision(gateway: RoutedModelGateway) -> str:
    return gateway._record_decision(
        "world_cognition:capacity",
        "economy",
        selected_route="economy_model",
        group_id=None,
        key_id=None,
        reason_code="purpose_classified_locally",
    ).decision_id


async def complete(gateway: RoutedModelGateway, subject: str, key: str) -> Any:
    return await gateway.complete_structured(
        subject,
        "world_cognition:capacity",
        [ModelMessage(role="user", content="JSON")],
        Answer,
        idempotency_key=key,
    )


def test_route_admission_reserves_attempt_terminals_and_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "1")
    db, subject, gateway, _, group, key = setup_route(tmp_path)
    route = decision(gateway)
    with pytest.raises(InvalidTransitionError, match="evidence_capacity_reached"):
        decision(gateway)
    for number in range(1, 33):
        gateway._record_attempt(route, group, key, number, "selected", "resource_selected", 0)
        gateway._record_attempt(route, group, key, number, "failed", "known_failure", 1)
    with pytest.raises(InvalidTransitionError, match="evidence_capacity_reached"):
        gateway._record_attempt(route, group, key, 33, "selected", "resource_selected", 0)
    with pytest.raises(IntegrityError, match="transition invalid"):
        gateway._record_attempt(route, group, key, 32, "failed", "duplicate", 1)
    gateway._finish_decision(route, "failed", "known_failure")
    gateway._finish_decision(route, "failed", "known_failure")
    counts = RetentionManager(db).evidence_capacity(subject)["counts"]
    assert counts["cognitive_route_decisions"] == counts["cognitive_route_outcomes"] == 1
    assert counts["cognitive_route_attempts"] == 64
    assert gateway.resources.verify_routing_integrity(subject)["cognitive_route_attempts"] == 64


def test_concurrent_route_admission_cannot_overrun_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "3")
    db, subject, gateway, _, _, _ = setup_route(tmp_path)

    def admit(_: int) -> bool:
        try:
            decision(gateway)
            return True
        except InvalidTransitionError:
            return False

    with ThreadPoolExecutor(max_workers=6) as workers:
        assert sum(workers.map(admit, range(12))) == 3
    assert (
        RetentionManager(db).evidence_capacity(subject)["counts"]["cognitive_route_decisions"] == 3
    )


@pytest.mark.asyncio
async def test_full_capacity_still_replays_cache_without_new_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "1")
    db, subject, gateway, provider, _, _ = setup_route(tmp_path)
    assert (await complete(gateway, subject, "once")).output.answer == "ok"
    before = RetentionManager(db).evidence_capacity(subject)["counts"]
    for _ in range(5):
        assert (await complete(gateway, subject, "once")).cached
    assert RetentionManager(db).evidence_capacity(subject)["counts"] == before
    with pytest.raises(InvalidTransitionError, match="evidence_capacity_reached"):
        await complete(gateway, subject, "another")
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_attempt_capacity_denial_settles_owned_call_and_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.model import CognitiveResourceGroupUpdate

    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "1")
    db, subject, gateway, _, group, _ = setup_route(tmp_path)
    gateway.resources.update(
        group,
        CognitiveResourceGroupUpdate(
            max_attempts=2,
            input_usd_per_million=Decimal("1"),
            output_usd_per_million=Decimal("1"),
        ),
        subject_id=subject,
        actor="operator",
        reason="fixture",
    )
    provider = FakeProvider([ProviderResponse("invalid-json", ModelUsage(2, 1))])
    gateway.provider_factory = lambda _: provider
    with pytest.raises(InvalidTransitionError, match="evidence_capacity_reached"):
        await complete(gateway, subject, "retry-full")
    assert len(provider.requests) == 1
    with db.connection() as c:
        assert c.execute("SELECT status FROM model_calls").fetchone()[0] == "failed"
        assert c.execute("SELECT status FROM model_attempts").fetchone()[0] == "succeeded"
        assert c.execute("SELECT cost_microusd FROM model_attempts").fetchone()[0] == 3
        assert c.execute("SELECT outcome FROM cognitive_route_outcomes").fetchone()[0] == "failed"
    assert gateway.resources.verify_routing_integrity(subject)["cognitive_route_attempts"] == 2


@pytest.mark.asyncio
async def test_first_attempt_capacity_denial_sends_nothing_and_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.model import BudgetLimits, ModelLedger

    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "2")
    db, subject, gateway, provider, _, _ = setup_route(tmp_path)
    ledger = ModelLedger(db)
    call, _ = ledger.prepare_call(subject, "fixture", "model", "test", "a" * 64, "previous")
    for number in range(2):
        attempt = ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(100, 10000, 10000, 10000),
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=1,
            continuation=number > 0,
        )
        ledger.start_attempt(attempt.attempt_id)
        ledger.finish_attempt(attempt.attempt_id, "failed", usage=ModelUsage(0, 0), cost_microusd=0)
    ledger.finish_call(call.call_id, "failed")
    with pytest.raises(InvalidTransitionError, match="evidence_capacity_reached"):
        await complete(gateway, subject, "initial-full")
    assert not provider.requests
    with db.connection() as c:
        assert {row[0] for row in c.execute("SELECT status FROM model_calls")} == {"failed"}
        assert c.execute("SELECT outcome FROM cognitive_route_outcomes").fetchone()[0] == "failed"
    assert gateway.resources.verify_routing_integrity(subject)["cognitive_route_attempts"] == 2


@pytest.mark.asyncio
async def test_unknown_and_not_due_do_not_grow_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "1")
    db, subject, gateway, _, _, _ = setup_route(tmp_path)
    provider = FakeProvider([ProviderCallError("unknown", retryable=False, outcome_unknown=True)])
    gateway.provider_factory = lambda _: provider
    with pytest.raises(ProviderCallError):
        await complete(gateway, subject, "unknown")
    before = RetentionManager(db).evidence_capacity(subject)["counts"]
    for _ in range(5):
        with pytest.raises(ProviderCallError, match="quarantined"):
            await complete(gateway, subject, "unknown")
        with pytest.raises(ProviderCallError, match="pool_waiting"):
            await complete(gateway, subject, "not-due")
    assert len(provider.requests) == 1
    assert RetentionManager(db).evidence_capacity(subject)["counts"] == before
    assert RetentionManager(db).evidence_capacity(subject)["blocked"] is True
    with db.connection() as c:
        attempt = c.execute("SELECT * FROM model_attempts").fetchone()
        assert attempt["status"] == "unknown"
        assert attempt["input_tokens"] is None and attempt["cost_microusd"] is None
        assert attempt["reserved_input_tokens"] > 0
        assert (
            gateway.ledger.budget_status(subject, gateway.limits).input_tokens
            >= (attempt["reserved_input_tokens"])
        )


def test_wait_history_bounds_still_allow_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "3")
    db, subject, gateway, _, _, _ = setup_route(tmp_path)
    for number in range(100):
        gateway._record_unavailable("economy", "world_cognition:wait", str(number), force=True)
    gateway._resolve_wait("economy", "world_cognition:wait")
    for number in range(100):
        gateway._record_unavailable("economy", f"world_cognition:{number}", "missing", force=True)
    counts = RetentionManager(db).evidence_capacity(subject)["counts"]
    assert counts["waiting_cognitive_tasks"] == 1
    assert counts["waiting_cognitive_task_revisions"] == 4
    assert (
        gateway.resources.verify_routing_integrity(subject)["waiting_cognitive_task_revisions"] == 4
    )
    with db.connection() as c:
        assert c.execute("SELECT status FROM waiting_cognitive_tasks").fetchone()[0] == "resolved"


def test_failure_recovery_cycles_preserve_valid_bounded_wait_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "6")
    db, subject, gateway, _, _, _ = setup_route(tmp_path)
    for _ in range(10):
        gateway._record_unavailable("economy", "world_cognition:wait", "missing", force=True)
        gateway._resolve_wait("economy", "world_cognition:wait")
    counts = RetentionManager(db).evidence_capacity(subject)["counts"]
    assert counts["waiting_cognitive_tasks"] == 1
    assert counts["waiting_cognitive_task_revisions"] == 6
    assert (
        gateway.resources.verify_routing_integrity(subject)["waiting_cognitive_task_revisions"] == 6
    )


def test_route_counter_backfill_isolation_rollback_and_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import noyra.core.database as schema

    with monkeypatch.context() as old:
        old.setattr(schema, "CURRENT_SCHEMA_VERSION", 91)
        db, subject, gateway, _, _, _ = setup_route(tmp_path)
        decision(gateway)
        gateway._record_unavailable("economy", "world_cognition:old", "missing")
    db = Database(db.path)
    other = "Noyra-route-other"
    IdentityStore(db).ensure(other, "b" * 64)
    with db.connection() as c:
        assert evidence_counts(c, subject)["cognitive_route_decisions"] == 1
        assert evidence_counts(c, subject)["waiting_cognitive_task_revisions"] == 1
        assert evidence_counts(c, other)["cognitive_route_decisions"] == 0
    with pytest.raises(RuntimeError, match="rollback"), db.transaction() as c:
        c.execute(
            "INSERT INTO cognitive_route_decisions SELECT 'rolled-back',subject_id,purpose,"
            "task_kind,selected_route,pool,group_id,key_id,importance,risk,ambiguity,"
            "reason_code,state_hash,created_at FROM cognitive_route_decisions"
        )
        assert evidence_counts(c, subject)["cognitive_route_decisions"] == 2
        raise RuntimeError("rollback")
    with db.connection() as c:
        assert evidence_counts(c, subject)["cognitive_route_decisions"] == 1
    with db.transaction() as c:
        c.execute(
            "UPDATE route_evidence_row_counts SET row_count=0 "
            "WHERE subject_id=? AND table_name='waiting_cognitive_task_revisions'",
            (subject,),
        )
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
        c.execute("DELETE FROM route_evidence_row_counts WHERE subject_id=?", (subject,))
    with pytest.raises(IntegrityError, match="counter contract"):
        decision(gateway)


@pytest.mark.asyncio
async def test_route_lock_files_are_bounded_across_many_ids_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, subject, gateway, _, _, _ = setup_route(tmp_path)

    async def no_request(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(gateway, "_complete_structured_locked", no_request)
    for number in range(1000):
        await complete(gateway, subject, str(number))
    folder = db.path.with_name(f".{db.path.name}.model-route-locks-v2")
    assert 200 <= len(list(folder.glob("*.lock"))) <= 256
    assert not db.path.with_name(f".{db.path.name}.model-route-locks").exists()
    successor = RoutedModelGateway(Database(db.path), subject, gateway.resources)
    monkeypatch.setattr(successor, "_complete_structured_locked", no_request)
    await complete(successor, subject, "0")
    assert RoutedModelGateway._logical_route_locks == {}


@pytest.mark.parametrize("mode", ["same", "collision", "legacy"])
def test_route_lock_fences_real_process_and_allows_successor(tmp_path: Path, mode: str) -> None:
    path = tmp_path / "noyra.sqlite3"
    digest = content_hash(
        {
            "subject_id": "subject",
            "purpose": "world_cognition:capacity",
            "idempotency_key": "once",
        }
    )
    if mode == "collision":
        digest = next(
            candidate
            for number in range(10000)
            if int(
                (
                    candidate := content_hash(
                        {
                            "subject_id": "subject",
                            "purpose": "world_cognition:capacity",
                            "idempotency_key": str(number),
                        }
                    )
                )[:8],
                16,
            )
            % 256
            == int(digest[:8], 16) % 256
        )
    lock_path = (
        path.with_name(f".{path.name}.model-route-locks") / f"{digest}.lock"
        if mode == "legacy"
        else path.with_name(f".{path.name}.model-route-locks-v2")
        / f"{int(digest[:8], 16) % 256:02x}.lock"
    )
    probe = """
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[2])
from pydantic import BaseModel
from noyra.model.resources import RoutedModelGateway
from noyra.model.errors import ProviderCallError
async def no_request(*args, **kwargs):
    return None
gateway = object.__new__(RoutedModelGateway)
gateway.database = SimpleNamespace(path=Path(sys.argv[1]))
gateway._complete_structured_locked = no_request
try:
    asyncio.run(gateway.complete_structured(
        'subject', 'world_cognition:capacity', [], BaseModel, idempotency_key='once'))
except ProviderCallError as exc:
    assert exc.code == 'routed_model_call_in_progress' and exc.outcome_unknown
    raise SystemExit(23)
assert RoutedModelGateway._logical_route_locks == {}
"""
    command = [
        sys.executable,
        "-c",
        probe,
        str(path),
        str(Path(__file__).resolve().parents[1] / "src"),
    ]
    owner = ProcessLock(lock_path)
    owner.acquire()
    try:
        contender = subprocess.run(command, capture_output=True, text=True, timeout=20)
        assert contender.returncode == 23, contender.stderr
    finally:
        owner.release()
    successor = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert successor.returncode == 0, successor.stderr
    assert lock_path.exists()


@pytest.mark.asyncio
async def test_unlock_error_does_not_leak_other_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, subject, gateway, _, _, _ = setup_route(tmp_path)
    digest = content_hash(
        {
            "subject_id": subject,
            "purpose": "world_cognition:capacity",
            "idempotency_key": "once",
        }
    )
    legacy = db.path.with_name(f".{db.path.name}.model-route-locks") / f"{digest}.lock"
    legacy.parent.mkdir()
    legacy.touch()
    original = ProcessLock.release

    def fail_after_release(lock: ProcessLock) -> None:
        original(lock)
        if lock.path == legacy:
            raise OSError("legacy unlock failed")

    monkeypatch.setattr(ProcessLock, "release", fail_after_release)
    with pytest.raises(OSError, match="legacy unlock failed"):
        await complete(gateway, subject, "once")
    assert RoutedModelGateway._logical_route_locks == {}
    monkeypatch.setattr(ProcessLock, "release", original)
    assert (await complete(gateway, subject, "once")).cached
