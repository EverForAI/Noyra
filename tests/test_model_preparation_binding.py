from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from noyra.cognition.types import SemanticMemoryIntegrationProposal
from noyra.core import EventStore, SubjectKernel
from noyra.core.model_admission import track_model_preparation
from noyra.core.types import content_hash
from noyra.mind import MemoryIntegrationSupervisor, MemoryStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelMessage,
    ModelUsage,
    ProviderResponse,
)


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    value = SubjectKernel(tmp_path / "binding.sqlite3", "one", content_hash("binding"))
    value.boot()
    value.orient()
    value.activate()
    try:
        yield value
    finally:
        value.close()


def gateway(kernel: SubjectKernel) -> tuple[ModelGateway, FakeProvider]:
    provider = FakeProvider(
        [
            ProviderResponse(
                content=json.dumps({"disposition": "wait", "reason": "No integration"}),
                usage=ModelUsage(1, 1),
            )
        ]
    )
    return ModelGateway(
        provider,
        ModelLedger(kernel.database),
        model="fixture",
        limits=BudgetLimits(20, 100000, 100000, 100000),
    ), provider


@pytest.mark.asyncio
async def test_preparation_observers_compose_in_one_transaction_and_restore_after_exit(
    kernel: SubjectKernel,
) -> None:
    model, provider = gateway(kernel)
    observed = []

    def outer(connection: Any, call: Any) -> None:
        assert connection.in_transaction
        observed.append(("outer", call.call_id, call.status))

    def inner(connection: Any, call: Any) -> None:
        assert connection.execute(
            "SELECT call_id FROM model_calls WHERE call_id=?", (call.call_id,)
        ).fetchone()
        observed.append(("inner", call.call_id, call.status))

    with track_model_preparation(outer), track_model_preparation(inner):
        result = await model.complete_structured(
            kernel.subject_id,
            "binding-test",
            [ModelMessage(role="user", content="Synthetic fixture")],
            SemanticMemoryIntegrationProposal,
            idempotency_key="one",
        )
    assert [o[0] for o in observed] == ["outer", "inner"]
    assert {o[1] for o in observed} == {result.call_id}
    assert all(o[2] == "prepared" for o in observed)
    cached = await model.complete_structured(
        kernel.subject_id,
        "binding-test",
        [ModelMessage(role="user", content="Synthetic fixture")],
        SemanticMemoryIntegrationProposal,
        idempotency_key="one",
    )
    assert cached.cached and len(provider.requests) == 1 and len(observed) == 2


@pytest.mark.asyncio
async def test_binding_exception_rolls_back_preparation_and_no_attempt_is_admitted(
    kernel: SubjectKernel,
) -> None:
    model, provider = gateway(kernel)

    def broken(connection: Any, call: Any) -> None:
        assert call.status == "prepared"
        raise RuntimeError("cannot bind")

    with track_model_preparation(broken), pytest.raises(RuntimeError, match="cannot bind"):
        await model.complete_structured(
            kernel.subject_id,
            "binding-test",
            [ModelMessage(role="user", content="Synthetic fixture")],
            SemanticMemoryIntegrationProposal,
            idempotency_key="one",
        )
    assert not provider.requests
    with kernel.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
    await model.complete_structured(
        kernel.subject_id,
        "binding-test",
        [ModelMessage(role="user", content="Synthetic fixture")],
        SemanticMemoryIntegrationProposal,
        idempotency_key="one",
    )
    assert len(provider.requests) == 1


def test_supervisor_transaction_local_commit_can_rollback_with_business_receipt(
    kernel: SubjectKernel,
) -> None:
    event = EventStore(kernel.database).append(
        kernel.subject_id, "experience", "fixture", {"topic": "state"}
    )
    memories = MemoryStore(kernel.database)
    originals = [
        memories.create(
            kernel.subject_id,
            "semantic",
            text,
            salience=0.8,
            confidence=0.8,
            source_event_ids=(event.event_id,),
        )
        for text in ["State", "History"]
    ]
    supervisor = MemoryIntegrationSupervisor(kernel.database, kernel.subject_id)
    with pytest.raises(RuntimeError, match="receipt failed"), kernel.database.transaction() as c:
        supervisor._integrate_connection(
            c,
            "merge",
            tuple(m.memory_id for m in originals),
            synthesis="State and history",
            confidence=0.8,
            evidence_event_ids=(event.event_id,),
            reason="Combine evidence",
        )
        raise RuntimeError("receipt failed")
    assert [memories.get(m.memory_id) for m in originals] == originals
    assert memories.verify_lifecycle_integrity(kernel.subject_id)["memory_integrations"] == 0
    with kernel.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
