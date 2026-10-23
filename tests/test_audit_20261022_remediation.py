from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from noyra.cognition.reflection import ReflectionCognitionPending, SleepReflectionCognition
from noyra.cognition.settings import CognitionSettings
from noyra.core import Database, EventStore, IdentityStore, SubjectKernel
from noyra.core.admission import bind_lease
from noyra.core.database import _SCHEMA_DDL_FINGERPRINTS, CURRENT_SCHEMA_VERSION
from noyra.core.integrity import IntegrityRegistry
from noyra.core.operator_controls import OperatorControlConflict, OperatorControlService
from noyra.core.types import canonical_json, content_hash
from noyra.mind import MemoryEmbeddingIndex, MemoryStore
from noyra.model import (
    BudgetLimits,
    CognitiveResourceGroupInput,
    CognitiveResourceStore,
    ModelGateway,
    ModelMessage,
)
from noyra.model.config import OpenAICompatibleSettings
from noyra.model.embedding import EmbeddingSettings, OpenAIEmbeddingProvider
from noyra.model.errors import ModelLifecycleBlockedError, ProviderCallError
from noyra.model.fake import FakeProvider
from noyra.model.openai_compatible import OpenAICompatibleProvider
from noyra.model.request_identity import business_request_key
from noyra.model.resources import RoutedModelGateway
from noyra.model.types import ModelUsage, ProviderResponse, RetryPolicy
from noyra.sleep import SleepEngine, SleepReflectionPlan


class Output(BaseModel):
    ok: bool


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    owner = SubjectKernel(tmp_path / "noyra.sqlite3", "audit-remediation", content_hash("seed"))
    owner.boot()
    owner.orient()
    owner.activate()
    try:
        yield owner
    finally:
        owner.close()


def resources_for(
    database: Database, subject_id: str, root: Path, pool: str = "economy", **overrides: Any
) -> tuple[CognitiveResourceStore, Any]:
    store = CognitiveResourceStore(database, root / "secrets")
    payload = dict(
        pool=pool,
        label="fixture",
        base_url="https://models.example/v1",
        model="test-model",
        api_keys=(SecretStr("synthetic-key"),),
        max_attempts=1,
    )
    payload.update(overrides)
    group = store.configure(
        subject_id, CognitiveResourceGroupInput.model_validate(payload), actor="operator"
    )
    return store, group


async def complete(gateway: RoutedModelGateway, subject: str, purpose: str, key: str) -> Any:
    return await gateway.complete_structured(
        subject,
        purpose,
        (ModelMessage(role="user", content="Return JSON"),),
        Output,
        idempotency_key=key,
    )


def response() -> ProviderResponse:
    return ProviderResponse('{"ok":true}', ModelUsage(3, 3))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "purpose", ["world_cognition:one", "sleep_reflection:one", "interaction_cognition:one:0"]
)
async def test_business_round_unknown_cannot_be_bypassed_by_old_counter_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
) -> None:
    database = Database(tmp_path / "db")
    IdentityStore(database).ensure("audit-subject", content_hash("audit-subject"))
    pool = RoutedModelGateway.pool_for_purpose(purpose)
    store, _ = resources_for(database, "audit-subject", tmp_path, pool)
    provider = FakeProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            response(),
        ]
    )
    gateway = RoutedModelGateway(
        database, "audit-subject", store, provider_factory=lambda _: provider
    )
    with pytest.raises(ProviderCallError):
        await complete(gateway, "audit-subject", purpose, "pre-upgrade:counter:1")
    first_identity = business_request_key(database, "audit-subject", purpose)
    assert first_identity == "pre-upgrade:counter:1"
    monkeypatch.setattr("noyra.model.resources.utc_now", lambda: "2099-01-01T00:00:00+00:00")
    reopened = RoutedModelGateway(
        Database(database.path), "audit-subject", store, provider_factory=lambda _: provider
    )
    with pytest.raises(ProviderCallError, match="quarantined"):
        await complete(reopened, "audit-subject", purpose, "pre-upgrade:next-day:counter:900")
    assert business_request_key(database, "audit-subject", purpose) == first_identity
    assert len(provider.requests) == 1
    # An explicit one-shot authorization is pinned to the original physical route.
    call = gateway.ledger.unknown_calls("audit-subject")[0]
    gateway.ledger.prepare_unknown_retry(
        call.call_id, actor="operator", reason="synthetic approval"
    )
    result = await complete(reopened, "audit-subject", purpose, "yet-another-key")
    assert result.output.ok
    assert len(provider.requests) == 2
    assert gateway.ledger.get_call(call.call_id).status == "succeeded"


@pytest.mark.asyncio
async def test_real_reflection_keeps_unknown_identity(
    kernel: SubjectKernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = resources_for(kernel.database, kernel.subject_id, tmp_path, "deep")
    provider = FakeProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            response(),
        ]
    )
    gateway = RoutedModelGateway(
        kernel.database, kernel.subject_id, store, provider_factory=lambda _: provider
    )
    sleep = SleepEngine(kernel.database, kernel.subject_id)
    run = sleep.start("subject_choice", "synthetic fixture")
    run = sleep.begin_reflection(run.sleep_id)
    cognition = SleepReflectionCognition(
        kernel.database,
        kernel.subject_id,
        gateway,
        CognitionSettings(max_sleep_model_calls_per_run=3),
    )
    with pytest.raises(ReflectionCognitionPending):
        await cognition.propose(run)
    monkeypatch.setattr("noyra.model.resources.utc_now", lambda: "2099-01-01T00:00:00+00:00")
    with pytest.raises(ReflectionCognitionPending):
        await cognition.propose(run)
    assert len(provider.requests) == 1
    assert len(gateway.ledger.unknown_calls(kernel.subject_id)) == 1


@pytest.mark.asyncio
async def test_known_failure_advances_identity_but_other_rounds_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "db")
    IdentityStore(database).ensure("audit-subject", content_hash("audit-subject"))
    store, _ = resources_for(database, "audit-subject", tmp_path)
    provider = FakeProvider(
        [ProviderCallError("known-rejection", retryable=False, outcome_unknown=False), response()]
    )
    gateway = RoutedModelGateway(
        database, "audit-subject", store, provider_factory=lambda _: provider
    )
    purpose = "world_cognition:known"
    first = business_request_key(database, "audit-subject", purpose)
    with pytest.raises(ProviderCallError):
        await complete(gateway, "audit-subject", purpose, first)
    second = business_request_key(database, "audit-subject", purpose)
    assert first != second
    monkeypatch.setattr("noyra.model.resources.utc_now", lambda: "2099-01-01T00:00:00+00:00")
    assert (await complete(gateway, "audit-subject", purpose, second)).output.ok
    assert len(provider.requests) == 2
    assert (
        business_request_key(database, "audit-subject", "interaction_cognition:thread:0") != second
    )


@pytest.mark.asyncio
async def test_changed_key_concurrent_business_request_has_one_owner(tmp_path: Path) -> None:
    database = Database(tmp_path / "db")
    IdentityStore(database).ensure("audit-subject", content_hash("audit-subject"))
    store, _ = resources_for(database, "audit-subject", tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking:
        name = "mock"
        count = 0

        async def complete(self, request: Any) -> ProviderResponse:
            self.count += 1
            entered.set()
            await release.wait()
            return response()

    provider = Blocking()
    gateway = RoutedModelGateway(
        database, "audit-subject", store, provider_factory=lambda _: provider
    )
    task = asyncio.create_task(
        complete(gateway, "audit-subject", "world_cognition:same", "counter:1")
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ProviderCallError, match="in_progress"):
            await complete(gateway, "audit-subject", "world_cognition:same", "counter:2")
    finally:
        release.set()
        await task
    assert provider.count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "purpose",
    [
        "world_cognition:retry-close",
        "sleep_reflection:retry-close",
        "interaction_cognition:retry-close:0",
    ],
)
async def test_operator_can_close_failed_one_shot_unknown_retry(
    kernel: SubjectKernel, tmp_path: Path, purpose: str
) -> None:
    database = kernel.database
    subject = kernel.subject_id
    pool = RoutedModelGateway.pool_for_purpose(purpose)
    store, _ = resources_for(database, subject, tmp_path, pool)
    provider = FakeProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            ProviderCallError("known-retry-failure", retryable=False, outcome_unknown=False),
            response(),
        ]
    )
    gateway = RoutedModelGateway(database, subject, store, provider_factory=lambda _: provider)
    controls = OperatorControlService(kernel)
    if purpose.startswith("sleep_reflection:"):
        sleep = SleepEngine(database, subject)
        run = sleep.start("subject_choice", "synthetic retry closure fixture")
        sleep.begin_reflection(run.sleep_id)

    first_key = business_request_key(database, subject, purpose)
    with pytest.raises(ProviderCallError):
        await complete(gateway, subject, purpose, first_key)
    call = gateway.ledger.unknown_calls(subject)[0]
    controls.reconcile_model_call(
        call.call_id,
        actor="operator",
        reason="reviewed provider outcome before one retry",
        outcome="retry",
        response=None,
    )
    with pytest.raises(ProviderCallError, match="known-retry-failure"):
        await complete(gateway, subject, purpose, first_key)

    with pytest.raises(
        OperatorControlConflict,
        match="model_unknown_retry_resolution_evidence_required",
    ):
        controls.reconcile_model_call(
            call.call_id,
            actor="operator",
            reason="insufficient evidence; keep quarantined",
            outcome="failed",
            response=None,
        )
    evidence = "provider record PR-0001 verified both request IDs were not executed"
    closed = controls.reconcile_model_call(
        call.call_id,
        actor="operator",
        reason="provider records reviewed; explicitly accept residual duplicate risk",
        outcome="failed",
        response=None,
        resolution_evidence=evidence,
        accept_duplicate_risk=True,
    )
    assert closed["retry_grant_closed"] is True
    assert closed["idempotent"] is False
    assert closed["duplicate_risk_accepted"] is True
    repeated = controls.reconcile_model_call(
        call.call_id,
        actor="operator",
        reason="duplicate close request",
        outcome="failed",
        response=None,
    )
    assert repeated["retry_grant_closed"] is False
    assert repeated["idempotent"] is True

    with database.transaction() as connection:
        connection.execute(
            "UPDATE waiting_cognitive_tasks SET next_retry_at=? WHERE subject_id=? AND purpose=?",
            ("2000-01-01T00:00:00.000+00:00", subject, purpose),
        )
    resources_for(
        database,
        subject,
        tmp_path,
        pool,
        label="healthy-fallback",
        priority=200,
    )
    next_key = business_request_key(database, subject, purpose)
    assert next_key != first_key
    result = await complete(gateway, subject, purpose, next_key)
    assert result.output.ok
    assert len(provider.requests) == 3
    with database.connection() as connection:
        closed_rows = connection.execute(
            "SELECT COUNT(*) FROM audit_records WHERE subject_id=? "
            "AND action='model_unknown_retry_closed' "
            "AND json_valid(payload_json) AND json_extract(payload_json,'$.call_id')=?",
            (subject, call.call_id),
        ).fetchone()[0]
    assert closed_rows == 1
    with database.connection() as connection:
        audit_payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM audit_records WHERE subject_id=? "
                "AND action='model_unknown_retry_closed' "
                "AND json_valid(payload_json) AND json_extract(payload_json,'$.call_id')=?",
                (subject, call.call_id),
            ).fetchone()[0]
        )
    assert audit_payload["resolution_evidence_hash"] == content_hash({"evidence": evidence})
    assert audit_payload["duplicate_risk_accepted"] is True
    assert evidence not in json.dumps(audit_payload)
    report = IntegrityRegistry().run(
        database,
        subject,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.actions",),
    )
    assert report.status == "ok", report.to_dict()


class Embeddings:
    name = "synthetic-embedding"

    def __init__(self, hook: Callable[[], None] | None = None):
        self.hook = hook
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.hook is not None:
            hook, self.hook = self.hook, None
            hook()
        return [[1.0, 0.0] if text == "apples" else [0.0, 1.0] for text in texts]


def memory_fixture(database: Database, subject: str) -> tuple[MemoryStore, Any, str]:
    event = EventStore(database).append(subject, "experience", "test", {"synthetic": True})
    store = MemoryStore(database)
    memory = store.create(
        subject,
        "semantic",
        "apples",
        salience=0.5,
        confidence=0.9,
        source_event_ids=(event.event_id,),
    )
    return store, memory, event.event_id


def revise(
    store: MemoryStore, memory_id: str, event_id: str, content: str, status: str = "active"
) -> None:
    store.revise(
        memory_id,
        content=content,
        salience=0.5,
        confidence=0.9,
        status=status,
        reason="synthetic correction",
        source_event_ids=(event_id,),
    )


def test_revised_memory_vector_is_excluded_then_rebuilt(kernel: SubjectKernel) -> None:
    store, memory, event = memory_fixture(kernel.database, kernel.subject_id)
    index = MemoryEmbeddingIndex(kernel.database, Embeddings())
    assert index.rebuild_missing(kernel.subject_id) == 1
    revise(store, memory.memory_id, event, "oranges")
    assert index.similarities(kernel.subject_id, "apples", [memory.memory_id]) == {}
    assert index.missing(kernel.subject_id) == [(memory.memory_id, "oranges")]
    assert index.rebuild_missing(kernel.subject_id) == 1
    assert (
        index.similarities(kernel.subject_id, "oranges", [memory.memory_id])[memory.memory_id] == 1
    )
    assert index.verify_integrity(kernel.subject_id) == 1


def test_embedding_space_identity_tracks_endpoint_dimensions_but_not_key(
    kernel: SubjectKernel,
) -> None:
    def provider(
        endpoint: str, dimensions: int, vector: list[float], key: str
    ) -> OpenAIEmbeddingProvider:
        settings = EmbeddingSettings(
            base_url=endpoint,
            model="same-alias",
            api_key=SecretStr(key),
            dimensions=dimensions,
        )

        def handle(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": index, "embedding": vector}
                        for index, _ in enumerate(payload["input"])
                    ]
                },
            )

        return OpenAIEmbeddingProvider(settings, transport=httpx.MockTransport(handle))

    store, memory, _ = memory_fixture(kernel.database, kernel.subject_id)
    del store
    original = provider("https://first.example/v1", 2, [1.0, 0.0], "key-one")
    rotated_key = provider("https://first.example/v1/", 2, [1.0, 0.0], "key-two")
    changed_endpoint = provider("https://second.example/v1", 2, [0.0, 1.0], "key-one")
    changed_dimensions = provider("https://first.example/v1", 3, [0.0, 1.0, 0.0], "key-one")
    assert original.name == rotated_key.name
    assert original.name != changed_endpoint.name
    assert original.name != changed_dimensions.name

    old_index = MemoryEmbeddingIndex(kernel.database, original)
    old_index.index_memory(kernel.subject_id, memory.memory_id, memory.content)
    for replacement in (changed_endpoint, changed_dimensions):
        new_index = MemoryEmbeddingIndex(kernel.database, replacement)
        assert new_index.missing(kernel.subject_id) == [(memory.memory_id, memory.content)]
        assert new_index.rebuild_missing(kernel.subject_id) == 1
        assert (
            new_index.similarities(kernel.subject_id, memory.content, [memory.memory_id])[
                memory.memory_id
            ]
            == 1.0
        )


def test_embedding_default_dimension_change_invalidates_provider_identity(
    kernel: SubjectKernel,
) -> None:
    def provider(vector: list[float]) -> OpenAIEmbeddingProvider:
        settings = EmbeddingSettings(
            base_url="https://mutable.example/v1",
            model="mutable-alias",
            api_key=SecretStr("synthetic"),
            dimensions=None,
        )

        def handle(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": index, "embedding": vector}
                        for index, _ in enumerate(payload["input"])
                    ]
                },
            )

        return OpenAIEmbeddingProvider(settings, transport=httpx.MockTransport(handle))

    _, memory, _ = memory_fixture(kernel.database, kernel.subject_id)
    first = MemoryEmbeddingIndex(kernel.database, provider([1.0, 0.0]))
    first.index_memory(kernel.subject_id, memory.memory_id, memory.content)
    second_provider = provider([1.0, 0.0, 0.0])
    second = MemoryEmbeddingIndex(kernel.database, second_provider)
    assert first.provider.name != second_provider.name
    assert second.similarities(kernel.subject_id, memory.content, [memory.memory_id]) == {}
    assert second.missing(kernel.subject_id) == [(memory.memory_id, memory.content)]


def test_embedding_configuration_drift_is_rejected_by_authorizer_and_integrity(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "db")
    subject = "audit-embedding-drift"
    IdentityStore(database).ensure(subject, content_hash(subject))
    from noyra.core.integrity import IntegrityRegistry

    store = __import__("noyra.model", fromlist=["EmbeddingResourceStore"]).EmbeddingResourceStore(
        database, tmp_path / "secrets"
    )
    record = store.configure(
        subject,
        __import__("noyra.model", fromlist=["EmbeddingResourceInput"]).EmbeddingResourceInput(
            label="managed",
            base_url="https://embedding.example/v1",
            model="embed-v1",
            api_key=SecretStr("synthetic-key"),
            dimensions=2,
            daily_call_limit=9,
        ),
        actor="operator",
    )
    settings = store.active_settings(subject)
    assert settings is not None
    authorizer = store.call_authorizer(subject, settings)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE embedding_resources SET dimensions=3, daily_call_limit=0 WHERE config_id=?",
            (record.config_id,),
        )
    with pytest.raises(Exception, match="embedding provider no longer matches"):
        authorizer()
    report = IntegrityRegistry().run(
        database,
        subject,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=15,
        check_ids=("model.embedding_resources",),
    )
    assert report.status == "corrupt"


@pytest.mark.parametrize("status", ["active", "superseded"])
def test_embedding_cas_rejects_content_changed_during_provider_call(
    kernel: SubjectKernel, status: str
) -> None:
    store, memory, event = memory_fixture(kernel.database, kernel.subject_id)
    provider = Embeddings(lambda: revise(store, memory.memory_id, event, "oranges", status))
    index = MemoryEmbeddingIndex(kernel.database, provider)
    assert index.rebuild_missing(kernel.subject_id) == 0
    with kernel.database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM memory_embedding_inputs").fetchone()[0] == 0


def test_index_memory_refuses_wrong_input_before_spending(kernel: SubjectKernel) -> None:
    _, memory, _ = memory_fixture(kernel.database, kernel.subject_id)
    provider = Embeddings()
    index = MemoryEmbeddingIndex(kernel.database, provider)
    index.index_memory(kernel.subject_id, memory.memory_id, "not-current-content")
    assert provider.calls == 0
    assert index.missing(kernel.subject_id) == [(memory.memory_id, "apples")]


def test_schema99_upgrade_marks_unbound_vectors_stale_and_indexes_wait_history(
    kernel: SubjectKernel,
) -> None:
    _, memory, _ = memory_fixture(kernel.database, kernel.subject_id)
    with kernel.database.transaction() as c:
        c.execute("DROP TABLE memory_embedding_inputs")
        c.execute("DROP INDEX idx_waiting_cognitive_task_revisions_task")
        c.execute("DROP INDEX idx_model_calls_subject_purpose_pool")
        c.execute("UPDATE schema_meta SET value='99' WHERE key='schema_version'")
        c.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_ddl_contract'",
            ("99:" + _SCHEMA_DDL_FINGERPRINTS[99],),
        )
        c.execute(
            "INSERT INTO memory_embeddings VALUES(?,?,?,?,?,?,?)",
            (
                memory.memory_id,
                kernel.subject_id,
                Embeddings.name,
                2,
                canonical_json([1.0, 0.0]),
                content_hash([1.0, 0.0]),
                "2026-10-22T00:00:00+00:00",
            ),
        )
    upgraded = Database(kernel.database.path, strict_schema_contract=True)
    index = MemoryEmbeddingIndex(upgraded, Embeddings())
    assert index.similarities(kernel.subject_id, "apples", [memory.memory_id]) == {}
    assert index.missing(kernel.subject_id) == [(memory.memory_id, "apples")]
    assert index.rebuild_missing(kernel.subject_id) == 1
    with upgraded.connection() as c:
        assert (
            int(c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0])
            == CURRENT_SCHEMA_VERSION
        )
        plan = [
            dict(row)
            for row in c.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM waiting_cognitive_task_revisions "
                "WHERE task_id=? ORDER BY rowid",
                ("fixture",),
            )
        ]
        assert any("idx_waiting_cognitive_task_revisions_task" in row["detail"] for row in plan)
        model_plan = c.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM model_calls "
            "WHERE subject_id=? AND purpose=? AND status IN ('succeeded','failed')",
            (kernel.subject_id, "world_cognition:fixture"),
        ).fetchall()
        assert any("idx_model_calls_subject_purpose_pool" in row["detail"] for row in model_plan)
    Database(upgraded.path, strict_schema_contract=True)


def test_schema100_upgrade_adds_indexed_audit_lookup_with_malformed_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "schema100.sqlite3"
    database = Database(path, strict_schema_contract=True)
    subject = "audit-index-subject"
    IdentityStore(database).ensure(subject, content_hash(subject))
    with database.transaction() as connection:
        connection.execute("DROP INDEX idx_audit_subject_action_call")
        connection.execute(
            "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
            (
                "audit-malformed",
                subject,
                "unrelated",
                "test",
                "{malformed",
                "2026-10-22T00:00:00+00:00",
            ),
        )
        connection.execute("UPDATE schema_meta SET value='100' WHERE key='schema_version'")
        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_ddl_contract'",
            ("100:" + _SCHEMA_DDL_FINGERPRINTS[100],),
        )

    upgraded = Database(path, strict_schema_contract=True)
    store, _ = resources_for(upgraded, subject, tmp_path, "economy")
    gateway = RoutedModelGateway(
        upgraded, subject, store, provider_factory=lambda _: pytest.fail("no provider call")
    )
    purpose = "world_cognition:index-scale"
    call, _ = gateway.ledger.prepare_call(
        subject,
        "synthetic-provider",
        "synthetic-model",
        purpose,
        content_hash("synthetic request"),
        "synthetic-old-key",
        resource_pool="economy",
    )
    gateway.ledger.finish_call(call.call_id, "failed", error_code="synthetic-known-failure")
    with upgraded.transaction() as connection:
        connection.execute(
            "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
            (
                "audit-authorized-call",
                subject,
                "model_unknown_retry_authorized",
                "synthetic-operator",
                json.dumps({"call_id": call.call_id, "reason": "synthetic fixture"}),
                "2026-10-22T00:00:01+00:00",
            ),
        )

    steps = [0]
    connect = upgraded._connect

    def tracked_connect() -> Any:
        connection = connect()
        connection.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 100) or 0, 100)
        return connection

    monkeypatch.setattr(upgraded, "_connect", tracked_connect)

    def measure_route_fence() -> int:
        steps[0] = 0
        state = gateway._logical_route_state(
            purpose, "economy", "business-round-v1:next-generation"
        )
        assert state is not None and state.disposition == "quarantine"
        return steps[0]

    initial_steps = measure_route_fence()
    with upgraded.transaction() as connection:
        index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_audit_subject_action_call'"
        ).fetchone()
        assert index is not None and "WHERE json_valid(payload_json)" in index["sql"]
        for number in range(10_000):
            connection.execute(
                "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
                (
                    f"audit-unrelated-{number}",
                    subject,
                    "unrelated",
                    "test",
                    json.dumps({"sequence": number}),
                    f"2026-10-22T00:{number // 60:02d}:{number % 60:02d}+00:00",
                ),
            )
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT action,payload_json FROM audit_records "
            "INDEXED BY idx_audit_subject_action_call WHERE subject_id=? "
            "AND action IN ('model_unknown_retry_authorized','model_unknown_retry_cancelled') "
            "AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.call_id') IN (?) "
            "ORDER BY occurred_at,audit_id",
            (subject, "missing-call"),
        ).fetchall()
        assert any("idx_audit_subject_action_call" in row["detail"] for row in plan)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_records WHERE audit_id='audit-malformed'"
            ).fetchone()[0]
            == 1
        )
    scaled_steps = measure_route_fence()
    assert scaled_steps <= initial_steps + 2_000
    Database(path, strict_schema_contract=True)


@pytest.mark.asyncio
async def test_draft_and_saved_reasoning_probes_share_budget(
    kernel: SubjectKernel, tmp_path: Path
) -> None:
    limits: list[int] = []

    def handle(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        limit = body.get("max_completion_tokens", body.get("max_tokens"))
        limits.append(limit)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": '{"ok":true}' if limit >= 100 else ""},
                        "finish_reason": "stop" if limit >= 100 else "length",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 100},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert (
            await OpenAICompatibleProvider.probe_unstored(
                base_url="https://models.example/v1",
                model="gpt-5",
                api_key=SecretStr("synthetic"),
                reasoning_effort="high",
                client=client,
            )
        )["ok"]
        store, group = resources_for(
            kernel.database, kernel.subject_id, tmp_path, model="gpt-5", reasoning_effort="high"
        )
        gateway = RoutedModelGateway(
            kernel.database,
            kernel.subject_id,
            store,
            provider_factory=lambda settings: OpenAICompatibleProvider(settings, client=client),
        )
        assert (await gateway.test_resource(group.group_id, subject_id=kernel.subject_id))["ok"]
    assert limits == [4096, 4096]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["deep_sleep", "paused"])
async def test_lifecycle_change_before_first_send_cancels_without_unknown(
    kernel: SubjectKernel,
    tmp_path: Path,
    target: str,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    sent: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        sent.append(kernel.lifecycle.current().state)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    class Deferred(OpenAICompatibleProvider):
        async def complete(self, request: Any) -> ProviderResponse:
            entered.set()
            await release.wait()
            return await super().complete(request)

    store, group = resources_for(kernel.database, kernel.subject_id, tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = RoutedModelGateway(
            kernel.database,
            kernel.subject_id,
            store,
            provider_factory=lambda settings: Deferred(settings, client=client),
        )
        lease = kernel.admission.begin("operator_model_test")
        try:
            with bind_lease(lease):
                task = asyncio.create_task(
                    gateway.test_resource(group.group_id, subject_id=kernel.subject_id)
                )
            await asyncio.wait_for(entered.wait(), 5)
            if target == "deep_sleep":
                sleep = SleepEngine(kernel.database, kernel.subject_id)
                run = sleep.start("subject_choice", "fixture")
                sleep.begin_reflection(run.sleep_id)
                sleep.commit_reflection(run.sleep_id, SleepReflectionPlan(summary="local fixture"))
                sleep.enter_deep_sleep(run.sleep_id)
            else:
                kernel.lifecycle.transition("paused", "fixture")
            release.set()
            with pytest.raises(ModelLifecycleBlockedError):
                await task
            with kernel.database.connection() as c:
                assert c.execute("SELECT status FROM model_calls").fetchone()[0] == "failed"
                assert c.execute("SELECT status FROM model_attempts").fetchone()[0] == "cancelled"
            assert sent == []
        finally:
            release.set()
            kernel.admission.finish(lease)


@pytest.mark.asyncio
async def test_dispatched_result_can_settle_after_pause(
    kernel: SubjectKernel, tmp_path: Path
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def handle(req: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 3},
            },
        )

    store, group = resources_for(kernel.database, kernel.subject_id, tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = RoutedModelGateway(
            kernel.database,
            kernel.subject_id,
            store,
            provider_factory=lambda settings: OpenAICompatibleProvider(settings, client=client),
        )
        task = asyncio.create_task(
            gateway.test_resource(group.group_id, subject_id=kernel.subject_id)
        )
        await asyncio.wait_for(entered.wait(), 5)
        kernel.lifecycle.transition("paused", "fixture")
        release.set()
        assert (await task)["ok"]
        assert not gateway.ledger.unknown_calls(kernel.subject_id)


@pytest.mark.asyncio
async def test_model_timeout_defaults_are_100_total_and_20_connect() -> None:
    settings = OpenAICompatibleSettings(
        base_url="https://models.example/v1", model="fixture", api_key=SecretStr("synthetic")
    )
    assert settings.timeout_seconds == 100
    assert settings.connect_timeout_seconds == 20
    provider = OpenAICompatibleProvider(settings)
    try:
        assert provider._client.timeout.connect == 20
        assert provider._client.timeout.read == 100
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_legacy_business_retry_requires_explicit_grant(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy.db")
    subject = "legacy-subject"
    IdentityStore(database).ensure(subject, content_hash(subject))
    store = CognitiveResourceStore(database, tmp_path / "secrets")
    provider = FakeProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            response(),
        ]
    )
    from noyra.model.ledger import ModelLedger

    legacy = ModelGateway(
        provider,
        ModelLedger(database),
        model="legacy-model",
        resource_pool="economy",
        limits=BudgetLimits(20, 100000, 100000, 10000000),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    gateway = RoutedModelGateway(database, subject, store, legacy_gateway=legacy)
    purpose = "world_cognition:legacy"
    with pytest.raises(ProviderCallError):
        await complete(gateway, subject, purpose, "legacy-counter:1")
    with pytest.raises(ProviderCallError, match="quarantined"):
        await complete(gateway, subject, purpose, "legacy-counter:2")
    call = legacy.ledger.unknown_calls(subject)[0]
    legacy.ledger.prepare_unknown_retry(call.call_id, actor="operator", reason="synthetic grant")
    assert (await complete(gateway, subject, purpose, "different-key")).output.ok
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_pause_during_retry_delay_prevents_second_physical_request(
    kernel: SubjectKernel,
) -> None:
    from noyra.model.ledger import ModelLedger

    provider = FakeProvider(
        [
            ProviderCallError("known-temporary-error", retryable=True, outcome_unknown=False),
            response(),
        ]
    )

    async def pause_in_backoff(delay: float) -> None:
        kernel.lifecycle.transition("paused", "synthetic pause during retry")

    gateway = ModelGateway(
        provider,
        ModelLedger(kernel.database),
        model="fixture",
        limits=BudgetLimits(20, 100000, 100000, 10000000),
        retry_policy=RetryPolicy(max_attempts=2),
        sleep=pause_in_backoff,
    )
    with pytest.raises(ModelLifecycleBlockedError):
        await gateway.complete_structured(
            kernel.subject_id,
            "operator_model_test:retry",
            (ModelMessage(role="user", content="Return JSON"),),
            Output,
            idempotency_key="retry",
        )
    assert len(provider.requests) == 1
    with kernel.database.connection() as c:
        statuses = [row[0] for row in c.execute("SELECT status FROM model_attempts ORDER BY rowid")]
        call = c.execute("SELECT status, error_code FROM model_calls").fetchone()
    # The second attempt was never admitted, so no reservation needs cancelling.
    assert statuses == ["failed"]
    assert tuple(call) == ("failed", "model_lifecycle_blocked")


def test_schema100_export_scopes_embedding_bindings_to_their_memory_owner(tmp_path: Path) -> None:
    from io import BytesIO
    from zipfile import ZipFile

    from noyra.core.runtime_export import RuntimeLogExporter

    database = Database(tmp_path / "export.sqlite3")
    identifiers = {}
    for subject in ("export-owner", "export-other"):
        IdentityStore(database).ensure(subject, content_hash(subject))
        _, memory, _ = memory_fixture(database, subject)
        MemoryEmbeddingIndex(database, Embeddings()).index_memory(
            subject, memory.memory_id, memory.content
        )
        identifiers[subject] = memory.memory_id
    artifact = RuntimeLogExporter(database).export("export-owner", actor="synthetic-operator")
    with ZipFile(BytesIO(artifact.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        entry = next(
            item for item in manifest["tables"] if item["name"] == "memory_embedding_inputs"
        )
        rows = [json.loads(line) for line in archive.read(entry["file"]).splitlines()]
    assert len(rows) == 1
    assert rows[0]["memory_id"] == identifiers["export-owner"]
    assert rows[0]["content_hash"] == content_hash("apples")


@pytest.mark.asyncio
async def test_settled_business_history_does_not_trip_the_unresolved_route_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "bounded-history.sqlite3")
    subject = "settled-business"
    IdentityStore(database).ensure(subject, content_hash(subject))
    store, _ = resources_for(database, subject, tmp_path)
    provider = FakeProvider(
        [
            ProviderCallError("known-rejection", retryable=False, outcome_unknown=False),
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
        ]
    )
    gateway = RoutedModelGateway(database, subject, store, provider_factory=lambda _: provider)
    purpose = "world_cognition:many-settled"
    with pytest.raises(ProviderCallError):
        await complete(gateway, subject, purpose, business_request_key(database, subject, purpose))
    # Seed settled history in one transaction. No additional external work occurred.
    with database.transaction() as connection:
        first = dict(connection.execute("SELECT * FROM model_calls").fetchone())
        columns = tuple(first)
        placeholders = ",".join("?" for _ in columns)
        for number in range(300):
            row = dict(first)
            row["call_id"] = f"synthetic-settled-{number}"
            row["idempotency_key"] = f"settled-key-{number}"
            connection.execute(
                f"INSERT INTO model_calls ({','.join(columns)}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )
    next_key = business_request_key(database, subject, purpose)
    monkeypatch.setattr("noyra.model.resources.utc_now", lambda: "2099-01-01T00:00:00+00:00")
    with pytest.raises(ProviderCallError):
        await complete(gateway, subject, purpose, next_key)
    assert len(gateway.ledger.unknown_calls(subject)) == 1
    # The unknown row comes after all 301 settled rows. A new key must still find it.
    with pytest.raises(ProviderCallError, match="quarantined"):
        await complete(gateway, subject, purpose, "new-counter-cannot-hide-unknown")
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_routed_lifecycle_denial_keeps_real_reason_and_does_not_penalize_provider(
    kernel: SubjectKernel,
    tmp_path: Path,
) -> None:
    sent = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    class PauseBeforeSend(OpenAICompatibleProvider):
        async def complete(self, request: Any) -> ProviderResponse:
            kernel.lifecycle.transition("paused", "synthetic routed dispatch race")
            return await super().complete(request)

    store, _ = resources_for(kernel.database, kernel.subject_id, tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = RoutedModelGateway(
            kernel.database,
            kernel.subject_id,
            store,
            provider_factory=lambda settings: PauseBeforeSend(settings, client=client),
        )
        with pytest.raises(ModelLifecycleBlockedError):
            await complete(gateway, kernel.subject_id, "world_cognition:lifecycle-race", "one")
    assert sent == []
    with kernel.database.connection() as connection:
        call = connection.execute("SELECT status, error_code FROM model_calls").fetchone()
        outcome = connection.execute(
            "SELECT outcome, reason_code FROM cognitive_route_outcomes"
        ).fetchone()
        health_buckets = connection.execute(
            "SELECT COUNT(*) FROM provider_health_buckets"
        ).fetchone()[0]
    assert tuple(call) == ("failed", "model_lifecycle_blocked")
    assert tuple(outcome) == ("deferred", "model_lifecycle_blocked")
    assert health_buckets == 0


@pytest.mark.asyncio
async def test_business_generation_cannot_reuse_consumed_unknown_retry_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "consumed-grant.sqlite3")
    subject = "consumed-business-grant"
    IdentityStore(database).ensure(subject, content_hash(subject))
    store, _ = resources_for(database, subject, tmp_path)
    provider = FakeProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            ProviderCallError("known_retry_failure", retryable=False, outcome_unknown=False),
            response(),
        ]
    )
    gateway = RoutedModelGateway(database, subject, store, provider_factory=lambda _: provider)
    purpose = "world_cognition:one-grant"
    with pytest.raises(ProviderCallError):
        await complete(gateway, subject, purpose, "before-upgrade-counter")
    call = gateway.ledger.unknown_calls(subject)[0]
    gateway.ledger.prepare_unknown_retry(
        call.call_id, actor="operator", reason="one synthetic replay"
    )
    with pytest.raises(ProviderCallError):
        await complete(gateway, subject, purpose, business_request_key(database, subject, purpose))
    assert gateway.ledger.get_call(call.call_id).status == "failed"
    monkeypatch.setattr("noyra.model.resources.utc_now", lambda: "2099-01-01T00:00:00+00:00")
    with pytest.raises(ProviderCallError, match="quarantined"):
        await complete(gateway, subject, purpose, business_request_key(database, subject, purpose))
    assert len(provider.requests) == 2
