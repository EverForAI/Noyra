from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

import pytest

from noyra.migration.discovery import MigrationDiscovery, MigrationNeed
from noyra.migration.runtime import MigrationJudgment, MigrationRuntime
from test_migration_discovery import StaticObservationProvider, _observation, _setup
from test_migration_service import _add_attested_target, _json_post, migration_http  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recommend,trust,expected", [(True, True, 1), (False, True, 0), (True, False, 0)]
)
async def test_cognitive_review_persists_only_trusted_needed_manual_proposals(
    tmp_path: Any,
    recommend: bool,
    trust: bool,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.core.admission import RuntimeAdmissionGate
    from noyra.migration.manager import MigrationManager
    from noyra.migration.policy import MigrationStore
    from noyra.migration.proposals import MigrationProposalStore

    database, _, _, private = _setup(tmp_path)
    store = MigrationStore(database)
    policy = store.read_policy("Noyra-0001")
    store.update_policy(
        "Noyra-0001", policy.revision, {"enabled": True, "min_free_bytes": 1024}, "operator"
    )
    manager = MigrationManager(database, store)
    calls: list[Any] = []

    class Gateway:
        async def complete_structured(self, *args: Any, **kwargs: Any) -> Any:
            calls.append(args)
            return SimpleNamespace(
                call_id="cognitive-review",
                output=MigrationJudgment(
                    recommend=recommend,
                    highly_trusted=trust,
                    reason="Storage is nearly exhausted",
                    trust_reason="Enrolled host plus signed independent evidence",
                    benefit_score=0.95,
                    risk_score=0.05,
                ),
            )

    owner = SimpleNamespace(
        kernel=SimpleNamespace(
            database=database, subject_id="Noyra-0001", admission=RuntimeAdmissionGate("Noyra-0001")
        ),
        migration_store=store,
        migration_manager=manager,
        migration_proposals=MigrationProposalStore(database),
    )
    runtime = MigrationRuntime(
        owner,
        MigrationDiscovery(
            database, StaticObservationProvider({"target-1": _observation(private)})
        ),
        gateway=Gateway,
        to_thread=asyncio.to_thread,
    )
    monkeypatch.setattr(
        runtime,
        "source_need",
        lambda: MigrationNeed.assess(source_health=1.0, storage_pressure=0.95),
    )
    await runtime.tick()
    proposals = manager.list_proposals("Noyra-0001")
    assert len(proposals) == expected
    assert len(calls) == 1
    assert manager.list_tasks("Noyra-0001") == []
    if expected:
        assert proposals[0]["status"] == "awaiting_approval"
        assert "cognitive-review" in str(proposals[0])
    await runtime.tick()
    assert len(calls) == 1


def test_browser_execution_queues_approved_task_without_fabricated_proofs(
    migration_http: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, url = migration_http
    policy = server.migration_store.read_policy(server.kernel.subject_id)
    policy = server.migration_store.update_policy(
        server.kernel.subject_id, policy.revision, {"enabled": True}, "operator"
    )
    _add_attested_target(server)
    proposal = server.migration_manager.create_proposal(
        subject_id=server.kernel.subject_id,
        target_id="migration-target-1",
        policy_revision=policy.revision,
        reason="needed migration",
        reason_code="maintenance",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    task = server.migration_manager.approve(
        proposal.proposal_id, actor="operator", idempotency_key="browser-test"
    )
    called: list[str] = []
    monkeypatch.setattr(server.migration_runtime, "execute", lambda task_id: called.append(task_id))
    status, response = _json_post(f"{url}/api/v1/admin/migration/tasks/{task.task_id}/cutover", {})
    assert status == 202
    assert response["status"] == "queued"
    server._migration_worker.join(timeout=5)
    assert called == [task.task_id]


def test_target_observation_requires_explicit_cost_and_verifies_signature(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_migration_binding_proofs import _agent

    agent, _private = _agent(tmp_path)
    monkeypatch.delenv("NOYRA_MIGRATION_COST_MICROUSD_MONTH", raising=False)
    with pytest.raises(ValueError, match="cost"):
        agent.observe()
    monkeypatch.setenv("NOYRA_MIGRATION_COST_MICROUSD_MONTH", "123456")
    from noyra.migration.discovery import ResourceObservation

    observation = ResourceObservation(**agent.observe())
    assert agent.public_key is not None
    observation.verify(
        public_key=agent.public_key,
        expected_target_id=agent.target_id,
        expected_generation=agent.generation,
    )
    assert observation.cost_microusd_month == 123456
    assert asdict(observation)["capacity"]["free_bytes"] > 0
