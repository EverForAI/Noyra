from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager, closing
from typing import Any
from unittest.mock import patch

import pytest

from noyra.cognition import CognitionSettings, SemanticMemoryIntegrator
from noyra.cognition.memory_conflicts import ERROR_CODE
from noyra.core import EventStore, IdentityStore, SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash
from noyra.mind import MemoryStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)
from noyra.model.errors import ModelCallConflictError
from test_semantic_memory_recovery import kernel as kernel
from test_semantic_memory_recovery import setup


class SyntheticProcessExit(BaseException):
    pass


async def prepared_round(kernel: SubjectKernel) -> tuple[SemanticMemoryIntegrator, FakeProvider]:
    worker, provider, _ = setup(kernel)
    with (
        patch.object(worker.gateway.ledger, "authorize_attempt", side_effect=SyntheticProcessExit),
        pytest.raises(SyntheticProcessExit),
    ):
        await worker.run_due()
    assert not provider.requests
    with kernel.database.read_transaction() as connection:
        assert connection.execute("SELECT status FROM model_calls").fetchone()[0] == "prepared"
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
    return worker, provider


@pytest.mark.asyncio
async def test_prepared_model_conflict_closes_without_dispatch(
    kernel: SubjectKernel,
) -> None:
    _worker, provider = await prepared_round(kernel)
    replacement_provider = FakeProvider([])
    replacement = ModelGateway(
        replacement_provider,
        ModelLedger(kernel.database),
        model="changed-model",
        limits=BudgetLimits(20, 100_000, 100_000, 100_000),
    )
    restarted = SemanticMemoryIntegrator(
        kernel.database, kernel.subject_id, replacement, CognitionSettings()
    )
    assert await restarted.run_due() == "memory_integration_context_changed"
    with kernel.database.read_transaction() as c:
        assert c.execute("SELECT status FROM model_calls").fetchone()[0] == "failed"
        assert c.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
        state = restarted.rounds.state(restarted.rounds.read(c))
        assert state["phase"] == "closed" and state["outcome"] == "context_changed"
    assert not provider.requests and not replacement_provider.requests
    assert restarted.rounds.verify_integrity() == 2


def changed_gateway(
    kernel: SubjectKernel, *, provider: FakeProvider | None = None, **kwargs: Any
) -> ModelGateway:
    return ModelGateway(
        provider or FakeProvider([]),
        ModelLedger(kernel.database),
        model=kwargs.pop("model", "changed-model"),
        limits=BudgetLimits(20, 100_000, 100_000, 100_000),
        **kwargs,
    )


def snapshot(kernel: SubjectKernel) -> dict[str, list[dict[str, Any]]]:
    with kernel.database.read_transaction() as c:
        return {
            name: [dict(row) for row in c.execute("SELECT * FROM " + name + " ORDER BY rowid")]
            for name in (
                "model_calls",
                "model_attempts",
                "workflow_checkpoints",
                "memory_integrations",
                "audit_records",
            )
        }


@pytest.mark.asyncio
async def test_unchanged_prepared_request_dispatches_once_with_its_original_binding(
    kernel: SubjectKernel,
) -> None:
    worker, provider = await prepared_round(kernel)
    assert await worker.run_due() == "memory_integration_committed"
    assert len(provider.requests) == 1 and worker.rounds.verify_integrity() == 2
    with kernel.database.read_transaction() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='semantic_memory_unsent_retired'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["provider", "reasoning", "pool"])
async def test_request_conflict_retires_only_the_exact_never_attempted_bound_call(
    kernel: SubjectKernel, change: str
) -> None:
    worker, provider = await prepared_round(kernel)
    replacement = changed_gateway(kernel, model="fixture")
    if change == "provider":
        replacement.provider.name = "different-synthetic-provider"
    elif change == "reasoning":
        replacement.reasoning_capability_verified = True
        replacement.reasoning_effort = "high"
    else:
        replacement.resource_pool = "economy"
    worker.gateway = replacement
    assert await worker.run_due() == "memory_integration_context_changed"
    assert not provider.requests
    with kernel.database.read_transaction() as c:
        call = c.execute("SELECT * FROM model_calls").fetchone()
        assert call["status"] == "failed" and call["error_code"] == ERROR_CODE
        assert c.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
    assert worker.rounds.verify_integrity() == 2


@pytest.mark.asyncio
async def test_changed_source_closes_zero_attempt_request_without_replacing_frozen_context(
    kernel: SubjectKernel,
) -> None:
    worker, provider = await prepared_round(kernel)
    with kernel.database.read_transaction() as c:
        state = worker.rounds.state(worker.rounds.read(c))
    memory = MemoryStore(kernel.database).get(state["sources"][0]["memory_id"])
    MemoryStore(kernel.database).revise(
        memory.memory_id,
        content="Independent later revision",
        salience=memory.salience,
        confidence=memory.confidence,
        status=memory.status,
        reason="New evidence",
        source_event_ids=(
            EventStore(kernel.database)
            .append(kernel.subject_id, "memory_revision_evidence", "fixture", {})
            .event_id,
        ),
        expected_revision=memory.current_revision,
    )
    assert await worker.run_due() == "memory_integration_context_changed"
    assert not provider.requests
    with kernel.database.read_transaction() as c:
        after = worker.rounds.state(worker.rounds.read(c))
        assert worker.rounds.prepared_hash(after) == worker.rounds.prepared_hash(state)
    assert worker.rounds.verify_integrity() == 2


@pytest.mark.asyncio
async def test_unknown_and_active_calls_never_lose_their_attempt_reservations(
    kernel: SubjectKernel,
) -> None:
    worker, provider = await prepared_round(kernel)
    ledger = ModelLedger(kernel.database)
    with kernel.database.read_transaction() as c:
        call = c.execute("SELECT call_id FROM model_calls").fetchone()[0]
    attempt = ledger.authorize_attempt(
        call,
        BudgetLimits(20, 100_000, 100_000, 100_000),
        reserved_input_tokens=100,
        reserved_output_tokens=200,
        reserved_cost_microusd=30,
    )
    ledger.start_attempt(attempt.attempt_id)
    before = snapshot(kernel)
    worker.gateway = changed_gateway(kernel)
    assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    assert snapshot(kernel) == before
    ledger.recover_interrupted(kernel.subject_id)
    before = snapshot(kernel)
    assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    assert snapshot(kernel) == before and not provider.requests


@pytest.mark.asyncio
async def test_unattributed_conflict_without_a_prepared_call_never_closes_or_quarantines_a_round(
    kernel: SubjectKernel,
) -> None:
    worker, provider, _ = setup(kernel)
    with patch.object(
        worker.gateway,
        "complete_structured",
        side_effect=ModelCallConflictError("synthetic unattributed conflict"),
    ):
        assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    with kernel.database.read_transaction() as c:
        assert worker.rounds.state(worker.rounds.read(c))["phase"] == "prepared"
        assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert (
            c.execute(
                "SELECT COUNT(*) FROM audit_records WHERE action='model_request_context_conflict'"
            ).fetchone()[0]
            == 0
        )
    assert not provider.requests
    assert await worker.run_due() == "memory_integration_committed"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_injected_conflict_without_actual_request_drift_preserves_prepared_work(
    kernel: SubjectKernel,
) -> None:
    worker, provider = await prepared_round(kernel)
    before = snapshot(kernel)
    with patch.object(
        worker.gateway,
        "complete_structured",
        side_effect=ModelCallConflictError("unexpected conflict"),
    ):
        assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    assert snapshot(kernel) == before and not provider.requests
    assert await worker.run_due() == "memory_integration_committed"


@pytest.mark.asyncio
async def test_operator_retry_and_routed_prepared_work_stay_behind_explicit_reconciliation(
    kernel: SubjectKernel,
) -> None:
    worker, provider = await prepared_round(kernel)
    worker.gateway.requires_explicit_prepared_retry = True
    before = snapshot(kernel)
    assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    assert snapshot(kernel) == before and not provider.requests


@pytest.mark.asyncio
async def test_racing_attempt_admission_is_not_retired_by_the_conflict_handler(
    kernel: SubjectKernel,
) -> None:
    worker, provider = await prepared_round(kernel)
    with kernel.database.read_transaction() as c:
        identifier = c.execute("SELECT call_id FROM model_calls").fetchone()[0]
    replacement = changed_gateway(kernel)
    original = replacement.complete_structured

    async def conflicting(*args: Any, **kwargs: Any) -> Any:
        try:
            return await original(*args, **kwargs)
        except ModelCallConflictError:
            ModelLedger(kernel.database).authorize_attempt(
                identifier,
                BudgetLimits(20, 100_000, 100_000, 100_000),
                reserved_input_tokens=100,
                reserved_output_tokens=100,
                reserved_cost_microusd=30,
            )
            raise

    worker.gateway = replacement
    with patch.object(replacement, "complete_structured", side_effect=conflicting):
        assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    with kernel.database.read_transaction() as c:
        assert c.execute("SELECT status FROM model_calls").fetchone()[0] == "executing"
        assert c.execute("SELECT reserved_cost_microusd FROM model_attempts").fetchone()[0] == 30
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='semantic_memory_unsent_retired'"
            ).fetchone()[0]
            == 0
        )
    assert not provider.requests


@pytest.mark.asyncio
async def test_success_arriving_during_conflict_is_preserved_for_local_recovery(
    kernel: SubjectKernel,
) -> None:
    worker, provider = await prepared_round(kernel)
    replacement = changed_gateway(kernel)
    complete = worker.gateway.complete_structured
    other = replacement.complete_structured

    async def conflicting(*args: Any, **kwargs: Any) -> Any:
        try:
            return await other(*args, **kwargs)
        except ModelCallConflictError:
            await complete(*args, **kwargs)
            raise

    worker.gateway = replacement
    with patch.object(replacement, "complete_structured", side_effect=conflicting):
        assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    saved = snapshot(kernel)
    assert saved["model_calls"][0]["status"] == "succeeded"
    assert await worker.run_due() == "memory_integration_committed"
    after = snapshot(kernel)
    assert (
        after["model_calls"] == saved["model_calls"]
        and after["model_attempts"] == saved["model_attempts"]
    )
    assert len(provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["receipt", "checkpoint"])
async def test_unsent_retirement_and_round_close_roll_back_together_then_retry(
    kernel: SubjectKernel, point: str
) -> None:
    worker, provider = await prepared_round(kernel)
    worker.gateway = changed_gateway(kernel)
    before = snapshot(kernel)
    target: AbstractContextManager[Any]
    if point == "checkpoint":
        target = patch.object(
            worker.rounds, "close", side_effect=OSError("synthetic close failure")
        )
    else:
        original = EventStore._append_connection

        def fail(self: EventStore, *args: Any, **kwargs: Any) -> Any:
            if len(args) > 2 and args[2] == "semantic_memory_unsent_retired":
                raise OSError("synthetic close failure")
            return original(self, *args, **kwargs)

        target = patch.object(EventStore, "_append_connection", fail)
    with target, pytest.raises(OSError, match="synthetic close failure"):
        await worker.run_due()
    assert snapshot(kernel) == before and not provider.requests
    assert await worker.run_due() == "memory_integration_context_changed"
    assert worker.rounds.verify_integrity() == 2


@pytest.mark.asyncio
async def test_retiring_this_round_does_not_mutate_other_subject_or_purpose_calls(
    kernel: SubjectKernel,
) -> None:
    worker, provider = await prepared_round(kernel)
    ledger = ModelLedger(kernel.database)
    IdentityStore(kernel.database).ensure("other-subject", content_hash("other"))
    for subject, purpose in (
        (kernel.subject_id, "unrelated-purpose"),
        ("other-subject", "semantic_memory_integration:round:0"),
    ):
        ledger.prepare_call(
            subject, "fixture-provider", "fixture", purpose, "f" * 64, "unrelated-key"
        )
    before = [
        call
        for call in snapshot(kernel)["model_calls"]
        if call["idempotency_key"] == "unrelated-key"
    ]
    worker.gateway = changed_gateway(kernel)
    assert await worker.run_due() == "memory_integration_context_changed"
    assert [
        call
        for call in snapshot(kernel)["model_calls"]
        if call["idempotency_key"] == "unrelated-key"
    ] == before
    assert not provider.requests


@pytest.mark.asyncio
async def test_later_round_uses_a_new_identity_without_retrying_the_retired_call(
    kernel: SubjectKernel,
) -> None:
    worker, provider = await prepared_round(kernel)
    replacement_provider = FakeProvider(
        [
            ProviderResponse(
                content='{"disposition":"wait","reason":"No merge needed"}',
                usage=ModelUsage(10, 10),
            )
        ]
    )
    worker.gateway = changed_gateway(kernel, provider=replacement_provider)
    assert await worker.run_due() == "memory_integration_context_changed"
    original = snapshot(kernel)["model_calls"][0]
    assert await worker.run_due() == "memory_integration_waited"
    calls = snapshot(kernel)["model_calls"]
    assert calls[0] == original and len(calls) == 2
    assert calls[0]["purpose"].endswith(":0") and calls[1]["purpose"].endswith(":1")
    assert not provider.requests and len(replacement_provider.requests) == 1
    assert worker.rounds.verify_integrity() == 4


@pytest.mark.asyncio
async def test_pause_racing_with_configuration_conflict_preserves_the_original_prepared_call(
    kernel: SubjectKernel,
) -> None:
    worker, provider = await prepared_round(kernel)
    replacement = changed_gateway(kernel)
    original = replacement.complete_structured

    async def conflicting(*args: Any, **kwargs: Any) -> Any:
        try:
            return await original(*args, **kwargs)
        except ModelCallConflictError:
            kernel.pause("Pause during local recovery")
            raise

    worker.gateway = replacement
    before = snapshot(kernel)
    with patch.object(replacement, "complete_structured", side_effect=conflicting):
        assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    assert snapshot(kernel) == before and not provider.requests


@pytest.mark.asyncio
async def test_corrupt_original_binding_is_not_disguised_as_obsolete_work(
    kernel: SubjectKernel,
) -> None:
    worker, _ = await prepared_round(kernel)
    with kernel.database.transaction() as c:
        c.execute("UPDATE model_calls SET request_hash=?", ("0" * 64,))
    before = snapshot(kernel)
    worker.gateway = changed_gateway(kernel)
    with pytest.raises(IntegrityError, match="context binding"):
        await worker.run_due()
    assert snapshot(kernel) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["payload", "missing", "hash"])
async def test_integrity_rejects_missing_or_changed_unsent_retirement_evidence(
    kernel: SubjectKernel, damage: str
) -> None:
    worker, provider = await prepared_round(kernel)
    worker.gateway = changed_gateway(kernel)
    assert await worker.run_due() == "memory_integration_context_changed"
    # Model a damaged file through a standalone SQLite connection, outside
    # the application's FK/append-only boundary. This is fixture corruption,
    # never an exposed repair API or a change to normal database enforcement.
    with closing(sqlite3.connect(kernel.database.path)) as c:
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("BEGIN IMMEDIATE")
        triggers = c.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='events'"
        ).fetchall()
        for row in triggers:
            c.execute('DROP TRIGGER "' + row[0] + '"')
        if damage == "missing":
            c.execute("DELETE FROM events WHERE event_type='semantic_memory_unsent_retired'")
        elif damage == "hash":
            c.execute(
                "UPDATE events SET payload_hash=? "
                "WHERE event_type='semantic_memory_unsent_retired'",
                ("0" * 64,),
            )
        else:
            row = c.execute(
                "SELECT event_id,payload_json FROM events "
                "WHERE event_type='semantic_memory_unsent_retired'"
            ).fetchone()
            import json

            value = json.loads(row[1])
            value["prepared_hash"] = "0" * 64
            c.execute(
                "UPDATE events SET payload_json=?,payload_hash=? WHERE event_id=?",
                (canonical_json(value), content_hash(value), row[0]),
            )
        c.commit()
    with pytest.raises(IntegrityError, match="retirement receipt"):
        worker.rounds.verify_integrity()
    assert not provider.requests


@pytest.mark.asyncio
async def test_old_model_worker_cannot_authorize_the_retired_prepared_call(
    kernel: SubjectKernel,
) -> None:
    from noyra.model.errors import ModelCallStateError

    worker, provider = await prepared_round(kernel)
    with kernel.database.read_transaction() as c:
        call = c.execute("SELECT call_id FROM model_calls").fetchone()[0]
    worker.gateway = changed_gateway(kernel)
    assert await worker.run_due() == "memory_integration_context_changed"
    before = snapshot(kernel)
    with pytest.raises(ModelCallStateError):
        ModelLedger(kernel.database).authorize_attempt(
            call,
            BudgetLimits(20, 100_000, 100_000, 100_000),
            reserved_input_tokens=10,
            reserved_output_tokens=20,
            reserved_cost_microusd=5,
        )
    assert snapshot(kernel) == before and not provider.requests


@pytest.mark.asyncio
async def test_old_operator_quarantine_cannot_be_automatically_retired(
    kernel: SubjectKernel,
) -> None:
    worker, provider = await prepared_round(kernel)
    with kernel.database.read_transaction() as c:
        state = worker.rounds.state(worker.rounds.read(c))
    ModelLedger(kernel.database).quarantine_prepared_context(
        kernel.subject_id, worker.rounds.purpose(state)
    )
    before = snapshot(kernel)
    worker.gateway = changed_gateway(kernel)
    assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    assert snapshot(kernel) == before and not provider.requests


@pytest.mark.asyncio
async def test_unsent_retirement_survives_encrypted_event_archiving_and_runtime_restart(
    kernel: SubjectKernel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64
    from datetime import datetime, timedelta

    from noyra.core.event_archive import EventPayloadArchive

    worker, provider = await prepared_round(kernel)
    worker.gateway = changed_gateway(kernel)
    assert await worker.run_due() == "memory_integration_context_changed"
    before = snapshot(kernel)
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )

    class Future(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Future:
            value = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(value.timestamp(), tz)

    archive = EventPayloadArchive(
        kernel.database, kernel.database.path.parent / "subject/cold", subject_id=kernel.subject_id
    )
    with patch("noyra.core.event_archive.datetime", Future):
        assert archive.archive_cold(kernel.subject_id, older_than_days=90) > 0
    kernel.close()
    restarted = SubjectKernel(
        kernel.database.path, kernel.subject_id, content_hash("memory-recovery")
    )
    try:
        restarted.boot()
        restored = SemanticMemoryIntegrator(
            restarted.database, restarted.subject_id, worker.gateway, CognitionSettings()
        )
        assert restored.rounds.verify_integrity() == 2
        EventStore(restarted.database).verify_chain(kernel.subject_id)
        with restarted.database.read_transaction() as c:
            assert restored.rounds.state(restored.rounds.read(c))["phase"] == "closed"
        after = snapshot(restarted)
        assert (
            after["model_calls"] == before["model_calls"]
            and after["model_attempts"] == before["model_attempts"]
        )
        assert not provider.requests
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_unsent_receipt_with_offset_clock_is_consistent_with_the_call_completion(
    kernel: SubjectKernel,
) -> None:
    from datetime import datetime, timedelta, timezone

    worker, provider = await prepared_round(kernel)
    worker.gateway = changed_gateway(kernel)
    worker.clock = lambda: datetime.now(timezone(timedelta(hours=8))).isoformat()
    assert await worker.run_due() == "memory_integration_context_changed"
    assert worker.rounds.verify_integrity() == 2 and not provider.requests


@pytest.mark.asyncio
async def test_actual_idempotency_collision_with_another_purpose_preserves_both_lanes(
    kernel: SubjectKernel,
) -> None:
    worker, provider, _ = setup(kernel)
    with patch.object(
        worker.gateway,
        "complete_structured",
        side_effect=ModelCallConflictError("prepare checkpoint without a call"),
    ):
        assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    with kernel.database.read_transaction() as c:
        state = worker.rounds.state(worker.rounds.read(c))
    ledger = ModelLedger(kernel.database)
    ledger.prepare_call(
        kernel.subject_id,
        worker.gateway.provider.name,
        worker.gateway.model,
        "other-business-purpose",
        content_hash("other-business-request"),
        worker.rounds.logical_key(state),
    )
    before = snapshot(kernel)
    assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    assert snapshot(kernel) == before
    assert not provider.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_context", [False, True])
async def test_custom_gateway_cannot_attest_a_direct_gateway_request_identity(
    kernel: SubjectKernel, changed_context: bool
) -> None:
    class CustomGateway(ModelGateway):
        pass

    worker, provider = await prepared_round(kernel)
    if changed_context:
        with kernel.database.read_transaction() as c:
            state = worker.rounds.state(worker.rounds.read(c))
        memory = MemoryStore(kernel.database).get(state["sources"][0]["memory_id"])
        MemoryStore(kernel.database).revise(
            memory.memory_id,
            content="Changed source context",
            salience=memory.salience,
            confidence=memory.confidence,
            status=memory.status,
            reason="Synthetic context update",
            source_event_ids=(
                EventStore(kernel.database)
                .append(kernel.subject_id, "memory_revision_evidence", "fixture", {})
                .event_id,
            ),
            expected_revision=memory.current_revision,
        )
    worker.gateway = CustomGateway(
        FakeProvider([]),
        ModelLedger(kernel.database),
        model="changed-model",
        limits=BudgetLimits(20, 100_000, 100_000, 100_000),
    )
    before = snapshot(kernel)
    assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    assert snapshot(kernel) == before
    assert not provider.requests
