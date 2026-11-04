from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.cognition import CognitionSettings, SemanticMemoryIntegrator
from noyra.core import EventStore, SubjectKernel
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


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    value = SubjectKernel(tmp_path / "memory.sqlite3", "one", content_hash("memory-recovery"))
    value.boot()
    value.orient()
    value.activate()
    try:
        yield value
    finally:
        value.close()


def setup(kernel: SubjectKernel) -> tuple[SemanticMemoryIntegrator, FakeProvider, dict[str, Any]]:
    event = EventStore(kernel.database).append(
        kernel.subject_id, "experience", "fixture", {"topic": "continuity"}
    )
    memories = MemoryStore(kernel.database)
    sources = [
        memories.create(
            kernel.subject_id,
            "semantic",
            text,
            salience=0.8,
            confidence=0.8,
            source_event_ids=(event.event_id,),
        )
        for text in ["State persists.", "History persists."]
    ]
    proposal = {
        "disposition": "integrate",
        "operation": "merge",
        "source_memory_ids": [m.memory_id for m in sources],
        "synthesis": "Continuity needs state and history.",
        "confidence": 0.8,
        "evidence_event_ids": [event.event_id],
        "reason": "Complementary evidence.",
    }
    provider = FakeProvider(
        [
            ProviderResponse(content=json.dumps(proposal), usage=ModelUsage(100, 50))
            for _ in range(2)
        ]
    )
    gateway = ModelGateway(
        provider,
        ModelLedger(kernel.database),
        model="fixture",
        limits=BudgetLimits(20, 100000, 100000, 100000),
    )
    return (
        SemanticMemoryIntegrator(kernel.database, kernel.subject_id, gateway, CognitionSettings()),
        provider,
        proposal,
    )


@pytest.mark.asyncio
async def test_success_before_business_commit_is_recovered_without_a_second_provider_request(
    kernel: SubjectKernel,
) -> None:
    worker, provider, _ = setup(kernel)
    complete = worker.gateway.complete_structured

    async def crash_after_success(*args: Any, **kwargs: Any) -> Any:
        await complete(*args, **kwargs)
        raise RuntimeError("synthetic crash after successful paid response")

    with (
        patch.object(worker.gateway, "complete_structured", side_effect=crash_after_success),
        pytest.raises(RuntimeError, match="synthetic crash"),
    ):
        await worker.run_due()
    with kernel.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM memory_integrations").fetchone()[0] == 0
        assert c.execute("SELECT status FROM model_calls").fetchone()[0] == "succeeded"
    restarted = SemanticMemoryIntegrator(
        kernel.database, kernel.subject_id, worker.gateway, CognitionSettings()
    )
    assert await restarted.run_due() == "memory_integration_committed"
    assert len(provider.requests) == 1
    with kernel.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM memory_integrations").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_model_context_binding_failure_rolls_back_before_any_provider_work(
    kernel: SubjectKernel,
) -> None:
    from noyra.core.model_admission import track_model_preparation

    worker, provider, _ = setup(kernel)

    def fail(connection: Any, call: Any) -> None:
        assert connection.in_transaction and call.status == "prepared"
        raise RuntimeError("binding storage failed")

    with track_model_preparation(fail), pytest.raises(RuntimeError, match="binding storage failed"):
        await worker.run_due()
    assert not provider.requests
    with kernel.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
    # Exiting the tracker must restore ordinary model execution.
    assert await worker.run_due() == "memory_integration_committed"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_transaction_local_supervisor_rolls_back_output_and_source_revisions(
    kernel: SubjectKernel,
) -> None:
    worker, _, proposal = setup(kernel)
    with (
        pytest.raises(RuntimeError, match="business receipt failed"),
        kernel.database.transaction() as c,
    ):
        worker.supervisor._integrate_connection(
            c,
            "merge",
            tuple(proposal["source_memory_ids"]),
            synthesis=proposal["synthesis"],
            confidence=0.8,
            evidence_event_ids=tuple(proposal["evidence_event_ids"]),
            reason=proposal["reason"],
        )
        raise RuntimeError("business receipt failed")
    with kernel.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM memory_integrations").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
        assert all(row[0] == "active" for row in c.execute("SELECT status FROM memories"))


async def interrupted(worker: SemanticMemoryIntegrator) -> None:
    complete = worker.gateway.complete_structured

    async def crash(*args: Any, **kwargs: Any) -> Any:
        await complete(*args, **kwargs)
        raise RuntimeError("after success")

    with (
        patch.object(worker.gateway, "complete_structured", side_effect=crash),
        pytest.raises(RuntimeError, match="after success"),
    ):
        await worker.run_due()


def raw(kernel: SubjectKernel, table: str) -> list[dict[str, Any]]:
    assert table in {
        "model_calls",
        "model_attempts",
        "memories",
        "memory_integrations",
        "workflow_checkpoints",
    }
    with kernel.database.read_transaction() as c:
        return [dict(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]


@pytest.mark.asyncio
async def test_recovery_ignores_new_context_route_configuration_and_daily_quota(
    kernel: SubjectKernel,
) -> None:
    worker, provider, proposal = setup(kernel)
    await interrupted(worker)
    calls, attempts = raw(kernel, "model_calls"), raw(kernel, "model_attempts")
    events = EventStore(kernel.database)
    event = events.append(kernel.subject_id, "new_evidence", "fixture", {"topic": "new"})
    newer = MemoryStore(kernel.database).create(
        kernel.subject_id,
        "semantic",
        "New unrelated high-priority memory",
        salience=1.0,
        confidence=1.0,
        source_event_ids=(event.event_id,),
    )
    # Cached recovery must not call any provider/gateway even if routing config changed.
    replacement = ModelGateway(
        FakeProvider([]),
        ModelLedger(kernel.database),
        model="changed-model",
        limits=BudgetLimits(0, 0, 0, 0),
    )
    restarted = SemanticMemoryIntegrator(
        kernel.database,
        kernel.subject_id,
        replacement,
        CognitionSettings(max_memory_integration_calls_per_day=1),
    )
    assert await restarted.run_due() == "memory_integration_committed"
    assert len(provider.requests) == 1
    assert raw(kernel, "model_calls") == calls and raw(kernel, "model_attempts") == attempts
    assert MemoryStore(kernel.database).get(newer.memory_id) == newer
    assert len(raw(kernel, "memory_integrations")) == 1
    assert all(
        MemoryStore(kernel.database).get(identifier).status == "superseded"
        for identifier in proposal["source_memory_ids"]
    )


@pytest.mark.asyncio
async def test_changed_source_rejects_old_success_locally_without_rewriting_paid_call(
    kernel: SubjectKernel,
) -> None:
    worker, provider, proposal = setup(kernel)
    await interrupted(worker)
    calls, attempts = raw(kernel, "model_calls"), raw(kernel, "model_attempts")
    store = MemoryStore(kernel.database)
    original = store.get(proposal["source_memory_ids"][0])
    store.revise(
        original.memory_id,
        content="Changed meaning",
        salience=original.salience,
        confidence=original.confidence,
        status=original.status,
        reason="New independent evidence",
        source_event_ids=tuple(proposal["evidence_event_ids"]),
        expected_revision=original.current_revision,
    )
    assert await worker.run_due() == "memory_integration_context_changed"
    assert len(provider.requests) == 1 and not raw(kernel, "memory_integrations")
    assert raw(kernel, "model_calls") == calls and raw(kernel, "model_attempts") == attempts
    with kernel.database.read_transaction() as c:
        state = worker.rounds.state(worker.rounds.read(c))
        assert state["phase"] == "closed" and state["outcome"] == "context_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["after_output", "before_receipt", "after_receipt"])
async def test_every_local_commit_fault_rolls_back_and_replays_only_paid_result(
    kernel: SubjectKernel, point: str
) -> None:
    worker, provider, _ = setup(kernel)
    integrate = worker.supervisor._integrate_connection
    close = worker.rounds.close

    def faulty_integrate(*args: Any, **kwargs: Any) -> Any:
        integrate(*args, **kwargs)
        raise RuntimeError("local commit crash")

    def faulty_close(*args: Any, **kwargs: Any) -> Any:
        if point == "after_receipt":
            close(*args, **kwargs)
        raise RuntimeError("local commit crash")

    fault = (
        patch.object(worker.supervisor, "_integrate_connection", side_effect=faulty_integrate)
        if point == "after_output"
        else patch.object(worker.rounds, "close", side_effect=faulty_close)
    )
    with fault, pytest.raises(RuntimeError, match="local commit crash"):
        await worker.run_due()
    assert not raw(kernel, "memory_integrations") and len(raw(kernel, "memories")) == 2
    assert all(m["status"] == "active" for m in raw(kernel, "memories"))
    calls, attempts = raw(kernel, "model_calls"), raw(kernel, "model_attempts")
    assert await worker.run_due() == "memory_integration_committed"
    assert await worker.run_due() is None
    assert len(provider.requests) == 1 and len(raw(kernel, "memory_integrations")) == 1
    assert raw(kernel, "model_calls") == calls and raw(kernel, "model_attempts") == attempts


@pytest.mark.asyncio
async def test_concurrent_worker_does_not_dispatch_or_apply_a_second_copy(
    kernel: SubjectKernel,
) -> None:
    import asyncio

    worker, provider, _ = setup(kernel)
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.complete

    async def slow(request: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(request)

    other = SemanticMemoryIntegrator(
        kernel.database, kernel.subject_id, worker.gateway, CognitionSettings()
    )
    with patch.object(provider, "complete", side_effect=slow):
        first = asyncio.create_task(worker.run_due())
        await entered.wait()
        assert await other.run_due() == "memory_integration_in_progress"
        release.set()
        assert await first == "memory_integration_committed"
    assert len(provider.requests) == 1 and len(raw(kernel, "memory_integrations")) == 1


@pytest.mark.asyncio
async def test_routed_result_binds_actual_physical_call_and_recovers_without_route(
    kernel: SubjectKernel,
) -> None:
    from noyra.model import RoutedModelGateway
    from test_audit_20261022_remediation import resources_for

    worker, provider, _ = setup(kernel)
    resources, _ = resources_for(
        kernel.database, kernel.subject_id, kernel.database.path.parent, pool="deep"
    )
    worker.gateway = RoutedModelGateway(
        kernel.database, kernel.subject_id, resources, provider_factory=lambda _: provider
    )
    await interrupted(worker)
    calls = raw(kernel, "model_calls")
    assert len(calls) == 1 and calls[0]["resource_group_id"]
    assert calls[0]["idempotency_key"] != worker.rounds.logical_key({"number": 0})
    replacement = ModelGateway(
        FakeProvider([]),
        ModelLedger(kernel.database),
        model="no-route",
        limits=BudgetLimits(0, 0, 0, 0),
    )
    restarted = SemanticMemoryIntegrator(
        kernel.database, kernel.subject_id, replacement, CognitionSettings()
    )
    assert await restarted.run_due() == "memory_integration_committed"
    assert len(provider.requests) == 1 and raw(kernel, "model_calls") == calls
    assert restarted.rounds.verify_integrity() == 2


@pytest.mark.asyncio
async def test_unknown_and_cancelled_model_attempt_never_advance_or_release_reservations(
    kernel: SubjectKernel,
) -> None:
    from noyra.model.errors import ProviderCallError

    worker, provider, _ = setup(kernel)
    provider._outcomes.clear()
    provider._outcomes.append(
        ProviderCallError(
            "provider_outcome_unknown", retryable=False, outcome_unknown=True, usage_unknown=True
        )
    )
    assert await worker.run_due() == "memory_integration_model_failed"
    calls, attempts = raw(kernel, "model_calls"), raw(kernel, "model_attempts")
    assert calls[0]["status"] == "unknown" and attempts[0]["reserved_output_tokens"] > 0
    for _ in range(3):
        assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    assert len(provider.requests) == 1 and not raw(kernel, "memory_integrations")
    assert raw(kernel, "model_calls") == calls and raw(kernel, "model_attempts") == attempts
    assert worker.rounds.verify_integrity() == 1


@pytest.mark.asyncio
async def test_cancel_during_provider_call_keeps_unknown_across_restart(
    kernel: SubjectKernel,
) -> None:
    import asyncio

    worker, provider, _ = setup(kernel)
    entered = asyncio.Event()

    async def wait(request: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    with patch.object(provider, "complete", side_effect=wait):
        task = asyncio.create_task(worker.run_due())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    calls, attempts = raw(kernel, "model_calls"), raw(kernel, "model_attempts")
    assert calls[0]["status"] == "unknown"
    restarted = SemanticMemoryIntegrator(
        kernel.database, kernel.subject_id, worker.gateway, CognitionSettings()
    )
    assert await restarted.run_due() == "memory_integration_waiting_reconciliation"
    assert raw(kernel, "model_calls") == calls and raw(kernel, "model_attempts") == attempts


@pytest.mark.asyncio
async def test_success_recovery_while_paused_never_applies_or_discards_result(
    kernel: SubjectKernel,
) -> None:
    worker, provider, _ = setup(kernel)
    await interrupted(worker)
    before = raw(kernel, "workflow_checkpoints")
    kernel.pause("maintenance")
    assert await worker.run_due() == "memory_integration_model_failed"
    assert raw(kernel, "workflow_checkpoints") == before and not raw(kernel, "memory_integrations")
    kernel.resume()
    assert await worker.run_due() == "memory_integration_committed"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_bound_success_tampering_is_rejected_without_new_provider_request(
    kernel: SubjectKernel,
) -> None:
    from noyra.core.errors import IntegrityError

    worker, provider, _ = setup(kernel)
    await interrupted(worker)
    with kernel.database.transaction() as c:
        c.execute("DROP TRIGGER prevent_event_immutable_update")
        c.execute(
            "UPDATE events SET payload_hash=? WHERE event_type='semantic_memory_call_bound'",
            ("0" * 64,),
        )
    with pytest.raises(IntegrityError):
        await worker.run_due()
    assert len(provider.requests) == 1 and not raw(kernel, "memory_integrations")


@pytest.mark.asyncio
async def test_legacy_success_is_not_implicitly_replayed_and_explicit_discard_preserves_cost(
    kernel: SubjectKernel,
) -> None:
    from noyra.cognition.types import SemanticMemoryIntegrationProposal
    from noyra.core.operator_controls import OperatorControlConflict, OperatorControlService

    worker, provider, _ = setup(kernel)
    # Real old-format call with no input-round binding, as on pre-upgrade stores.
    await worker.gateway.complete_structured(
        kernel.subject_id,
        "semantic_memory_integration:0",
        worker._messages('{"legacy":"original inputs unavailable"}'),
        SemanticMemoryIntegrationProposal,
        idempotency_key="semantic_memory_integration-0",
    )
    calls, attempts = raw(kernel, "model_calls"), raw(kernel, "model_attempts")
    assert await worker.run_due() == "memory_integration_legacy_recovery_required"
    assert len(provider.requests) == 1 and not raw(kernel, "memory_integrations")
    controls = OperatorControlService(kernel)
    item = controls.memory_integration_recovery()["records"][0]
    args = {
        "expected_fingerprint": item["expected_fingerprint"],
        "evidence_sha256": content_hash("independent review"),
        "actor": "operator",
        "reason": "Reviewed and discard this unbound local result",
    }
    with pytest.raises(OperatorControlConflict, match="requires_paused"):
        controls.discard_legacy_memory_result(item["call_id"], **args)
    controls.pause(actor="operator", reason="Check old result")
    assert not controls.discard_legacy_memory_result(item["call_id"], **args)["idempotent"]
    assert controls.discard_legacy_memory_result(item["call_id"], **args)["idempotent"]
    assert raw(kernel, "model_calls") == calls and raw(kernel, "model_attempts") == attempts
    assert kernel.lifecycle.current().state == "paused"
    controls.resume(actor="operator", reason="Resume independently")
    assert await worker.run_due() == "memory_integration_committed"
    assert (
        len(provider.requests) == 2
    )  # One explicitly discarded legacy call and one new normal round.


@pytest.mark.asyncio
async def test_success_recovery_works_after_encrypted_archive_and_real_runtime_restart(
    kernel: SubjectKernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64
    from datetime import datetime, timedelta

    from noyra.core.event_archive import EventPayloadArchive

    worker, provider, _ = setup(kernel)
    await interrupted(worker)
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )

    class Future(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Future:
            future = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(future.timestamp(), tz)

    archive = EventPayloadArchive(
        kernel.database, kernel.database.path.parent / "subject/cold", subject_id=kernel.subject_id
    )
    with patch("noyra.core.event_archive.datetime", Future):
        assert archive.archive_cold(kernel.subject_id, older_than_days=90) > 0
    kernel.close()
    restarted_kernel = SubjectKernel(
        kernel.database.path, kernel.subject_id, content_hash("memory-recovery")
    )
    try:
        restarted_kernel.boot()
        if restarted_kernel.lifecycle.current().state == "booting":
            restarted_kernel.orient()
            restarted_kernel.activate()
        worker = SemanticMemoryIntegrator(
            restarted_kernel.database, kernel.subject_id, worker.gateway, CognitionSettings()
        )
        assert await worker.run_due() == "memory_integration_committed"
        assert worker.rounds.verify_integrity() == 2
        EventStore(restarted_kernel.database).verify_chain(kernel.subject_id)
        assert len(provider.requests) == 1
    finally:
        restarted_kernel.close()


@pytest.mark.asyncio
async def test_pause_after_paid_response_keeps_result_for_separately_authorized_resume(
    kernel: SubjectKernel,
) -> None:
    from noyra.core.admission import OperationInvalidated, bind_lease

    worker, provider, _ = setup(kernel)
    complete = worker.gateway.complete_structured

    async def pause_after(*args: Any, **kwargs: Any) -> Any:
        result = await complete(*args, **kwargs)
        from contextvars import Context

        Context().run(kernel.pause, "synthetic pause after response")
        return result

    lease = kernel.admission.begin("memory-integration")
    try:
        with (
            bind_lease(lease),
            patch.object(worker.gateway, "complete_structured", side_effect=pause_after),
            pytest.raises(OperationInvalidated),
        ):
            await worker.run_due()
    finally:
        kernel.admission.finish(lease)
    assert not raw(kernel, "memory_integrations")
    calls, attempts = raw(kernel, "model_calls"), raw(kernel, "model_attempts")
    kernel.resume()
    assert await worker.run_due() == "memory_integration_committed"
    assert raw(kernel, "model_calls") == calls and raw(kernel, "model_attempts") == attempts
    assert len(provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["wait", "rejected"])
async def test_successful_non_commit_decision_is_closed_once_after_crash(
    kernel: SubjectKernel, disposition: str
) -> None:
    worker, provider, proposal = setup(kernel)
    outcome: dict[str, Any]
    if disposition == "wait":
        outcome = {"disposition": "wait", "reason": "More evidence required"}
    else:
        outcome = {**proposal, "source_memory_ids": ["foreign-or-missing", "another-missing"]}
    provider._outcomes.clear()
    provider._outcomes.append(
        ProviderResponse(content=json.dumps(outcome), usage=ModelUsage(100, 50))
    )
    await interrupted(worker)
    calls, attempts = raw(kernel, "model_calls"), raw(kernel, "model_attempts")
    assert await worker.run_due() == (
        "memory_integration_waited" if disposition == "wait" else "memory_integration_rejected"
    )
    assert worker.rounds.verify_integrity() == 2
    assert not raw(kernel, "memory_integrations")
    assert raw(kernel, "model_calls") == calls and raw(kernel, "model_attempts") == attempts
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_routed_one_shot_retry_failure_cannot_silently_open_new_round(
    kernel: SubjectKernel,
) -> None:
    from noyra.cognition.types import SemanticMemoryIntegrationProposal
    from noyra.model import RoutedModelGateway
    from noyra.model.errors import ProviderCallError
    from test_audit_20261022_remediation import resources_for

    worker, provider, _ = setup(kernel)
    resources, _ = resources_for(
        kernel.database, kernel.subject_id, kernel.database.path.parent, pool="deep"
    )
    worker.gateway = RoutedModelGateway(
        kernel.database, kernel.subject_id, resources, provider_factory=lambda _: provider
    )
    provider._outcomes.clear()
    provider._outcomes.extend(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            ProviderCallError("provider_auth_failed", retryable=False, outcome_unknown=False),
        ]
    )
    assert await worker.run_due() == "memory_integration_model_failed"
    call = raw(kernel, "model_calls")[0]
    worker.gateway.ledger.prepare_unknown_retry(
        call["call_id"], actor="operator", reason="One explicit retry"
    )
    with kernel.database.read_transaction() as c:
        state = worker.rounds.state(worker.rounds.read(c))
        payload = worker.rounds.reconstruct(c, state)
    with pytest.raises(ProviderCallError):
        from noyra.core.model_admission import track_model_preparation

        with track_model_preparation(worker.rounds.bind):
            await worker.gateway.complete_structured(
                kernel.subject_id,
                worker.rounds.purpose(state),
                worker._messages(canonical_json(payload)),
                SemanticMemoryIntegrationProposal,
                idempotency_key=worker.rounds.logical_key(state),
                max_output_tokens=state["options"]["max_output_tokens"],
                temperature=state["options"]["temperature"],
            )
    assert await worker.run_due() == "memory_integration_waiting_reconciliation"
    assert len(provider.requests) == 2 and len(raw(kernel, "model_calls")) == 1


@pytest.mark.asyncio
async def test_cached_response_corruption_cannot_be_replaced_by_new_paid_request(
    kernel: SubjectKernel,
) -> None:
    from noyra.core.errors import IntegrityError

    worker, provider, _ = setup(kernel)
    await interrupted(worker)
    with kernel.database.transaction() as c:
        triggers = c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='model_calls'"
        ).fetchall()
        for trigger in triggers:
            c.execute('DROP TRIGGER "' + trigger[0] + '"')
        c.execute("UPDATE model_calls SET response_hash=?", ("0" * 64,))
    with pytest.raises(IntegrityError):
        await worker.run_due()
    assert len(provider.requests) == 1 and not raw(kernel, "memory_integrations")


@pytest.mark.asyncio
async def test_legitimate_later_output_revision_does_not_invalidate_historical_round(
    kernel: SubjectKernel,
) -> None:
    worker, _, proposal = setup(kernel)
    assert await worker.run_due() == "memory_integration_committed"
    result = raw(kernel, "memory_integrations")[0]
    memories = MemoryStore(kernel.database)
    output = memories.get(result["output_memory_id"])
    memories.revise(
        output.memory_id,
        content="Later independently revised synthesis",
        salience=output.salience,
        confidence=output.confidence,
        status=output.status,
        reason="Further evidence",
        source_event_ids=tuple(proposal["evidence_event_ids"]),
        expected_revision=output.current_revision,
    )
    assert worker.rounds.verify_integrity() == 2
    assert memories.verify_lifecycle_integrity(kernel.subject_id)["memory_integrations"] == 1


@pytest.mark.asyncio
async def test_context_budget_shrinking_preserves_only_the_ids_actually_sent(
    kernel: SubjectKernel,
) -> None:
    worker, provider, proposal = setup(kernel)
    event_id = proposal["evidence_event_ids"][0]
    store = MemoryStore(kernel.database)
    for number in range(6):
        store.create(
            kernel.subject_id,
            "semantic",
            ("Evidence " + str(number) + " ") * 200,
            salience=0.8,
            confidence=0.8,
            source_event_ids=(event_id,),
        )
    worker.settings = worker.settings.model_copy(
        update={"max_memory_integration_context_chars": 5000}
    )
    await interrupted(worker)
    with kernel.database.read_transaction() as c:
        state = worker.rounds.state(worker.rounds.read(c))
        payload = worker.rounds.reconstruct(c, state)
    assert len(canonical_json(payload)) <= 5000
    assert len(payload["memories"]) == len(state["sources"]) < 8
    assert await worker.run_due() in {"memory_integration_committed", "memory_integration_rejected"}
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_integrity_rejects_closed_round_which_claims_another_result(
    kernel: SubjectKernel,
) -> None:
    from noyra.core.errors import IntegrityError
    from noyra.core.types import strict_json_loads

    worker, _, _ = setup(kernel)
    assert await worker.run_due() == "memory_integration_committed"
    assert worker.rounds.verify_integrity() == 2
    with kernel.database.transaction() as c:
        triggers = c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='workflow_checkpoints'"
        ).fetchall()
        for trigger in triggers:
            c.execute('DROP TRIGGER "' + trigger[0] + '"')
        row = c.execute(
            "SELECT * FROM workflow_checkpoints WHERE workflow_id=? "
            "ORDER BY checkpoint_version DESC LIMIT 1",
            (worker.rounds.key,),
        ).fetchone()
        state = strict_json_loads(row["state_json"])
        state["integration_id"] = "different-integration"
        c.execute(
            "UPDATE workflow_checkpoints SET state_json=?,state_hash=? "
            "WHERE workflow_id=? AND checkpoint_version=?",
            (
                canonical_json(state),
                content_hash(state),
                worker.rounds.key,
                row["checkpoint_version"],
            ),
        )
    with pytest.raises(IntegrityError):
        worker.rounds.verify_integrity()


@pytest.mark.asyncio
async def test_other_subject_cannot_apply_or_discard_bound_success(kernel: SubjectKernel) -> None:
    from noyra.cognition.memory_rounds import MemoryIntegrationRounds
    from noyra.core.identity import IdentityStore

    worker, provider, _ = setup(kernel)
    await interrupted(worker)
    IdentityStore(kernel.database).ensure("other", content_hash("other"))
    other = MemoryIntegrationRounds(kernel.database, "other")
    with kernel.database.read_transaction() as c:
        assert other.read(c) is None
        assert not other.legacy_records(c)
    assert await worker.run_due() == "memory_integration_committed"
    assert len(provider.requests) == 1
    with kernel.database.read_transaction() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM memory_integrations WHERE subject_id='other'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_explicit_one_shot_retry_success_is_recoverable_without_refunding_old_unknown_attempt(
    kernel: SubjectKernel,
) -> None:
    from noyra.model import RoutedModelGateway
    from noyra.model.errors import ProviderCallError
    from test_audit_20261022_remediation import resources_for

    worker, provider, proposal = setup(kernel)
    resources, _ = resources_for(
        kernel.database, kernel.subject_id, kernel.database.path.parent, pool="deep"
    )
    worker.gateway = RoutedModelGateway(
        kernel.database, kernel.subject_id, resources, provider_factory=lambda _: provider
    )
    provider._outcomes.clear()
    provider._outcomes.extend(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            ProviderResponse(content=json.dumps(proposal), usage=ModelUsage(100, 50)),
        ]
    )
    assert await worker.run_due() == "memory_integration_model_failed"
    call = raw(kernel, "model_calls")[0]
    old_attempt = raw(kernel, "model_attempts")[0]
    assert old_attempt["status"] == "unknown"
    worker.gateway.ledger.prepare_unknown_retry(
        call["call_id"], actor="operator", reason="Explicitly accept one duplicate-risk retry"
    )
    assert await worker.run_due() == "memory_integration_committed"
    assert len(provider.requests) == 2 and len(raw(kernel, "model_calls")) == 1
    assert raw(kernel, "model_attempts")[0] == old_attempt
    assert len(raw(kernel, "memory_integrations")) == 1
    assert worker.rounds.verify_integrity() == 2


@pytest.mark.asyncio
async def test_runtime_integrity_registry_checks_new_round_receipts(kernel: SubjectKernel) -> None:
    from noyra.core.integrity import IntegrityRegistry

    worker, _, _ = setup(kernel)
    assert await worker.run_due() == "memory_integration_committed"
    report = IntegrityRegistry().run(
        kernel.database,
        kernel.subject_id,
        kernel.database.path.parent,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("mind.memory_lifecycle",),
    )
    assert not report.p0 and not report.p1
    with kernel.database.transaction() as c:
        c.execute("DROP TRIGGER prevent_event_immutable_update")
        c.execute(
            "UPDATE events SET payload_hash=? WHERE event_type='semantic_memory_call_bound'",
            ("0" * 64,),
        )
    report = IntegrityRegistry().run(
        kernel.database,
        kernel.subject_id,
        kernel.database.path.parent,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("mind.memory_lifecycle",),
    )
    assert report.p0 or report.p1


@pytest.mark.asyncio
async def test_old_failed_one_shot_authorization_remains_a_legacy_reconciliation_object(
    kernel: SubjectKernel,
) -> None:
    from noyra.cognition.types import SemanticMemoryIntegrationProposal
    from noyra.core.operator_controls import OperatorControlService
    from noyra.model.errors import ProviderCallError

    worker, provider, _ = setup(kernel)
    provider._outcomes.clear()
    provider._outcomes.extend(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            ProviderCallError("provider_auth_failed", retryable=False, outcome_unknown=False),
        ]
    )

    async def old_request() -> None:
        await worker.gateway.complete_structured(
            kernel.subject_id,
            "semantic_memory_integration:0",
            worker._messages("legacy-context"),
            SemanticMemoryIntegrationProposal,
            idempotency_key="old-memory-request",
        )

    with pytest.raises(ProviderCallError):
        await old_request()
    identifier = raw(kernel, "model_calls")[0]["call_id"]
    worker.gateway.ledger.prepare_unknown_retry(
        identifier, actor="operator", reason="Allow exactly one old-format retry"
    )
    with pytest.raises(ProviderCallError):
        await old_request()
    calls, attempts = raw(kernel, "model_calls"), raw(kernel, "model_attempts")
    assert calls[0]["status"] == "failed"
    assert await worker.run_due() == "memory_integration_legacy_recovery_required"
    items = OperatorControlService(kernel).memory_integration_recovery()["records"]
    assert len(items) == 1 and items[0]["call_id"] == identifier and not items[0]["can_discard"]
    assert raw(kernel, "model_calls") == calls and raw(kernel, "model_attempts") == attempts
    assert len(provider.requests) == 2
