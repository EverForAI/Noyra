from __future__ import annotations

import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from noyra.cognition.cycle import CognitionCycle
from noyra.core import Database, EventStore, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.integrity import IntegrityRegistry
from noyra.core.types import canonical_json, content_hash
from noyra.mind import MemoryEmbeddingIndex, MemoryStore
from noyra.model import (
    EmbeddingBudgetLimits,
    EmbeddingCircuitPolicy,
    EmbeddingGateway,
    EmbeddingLedger,
    EmbeddingPricing,
    EmbeddingProviderResponse,
    EmbeddingResourceInput,
    EmbeddingResourceStore,
    EmbeddingSettings,
    EmbeddingUsage,
    OpenAIEmbeddingProvider,
)
from noyra.model.errors import (
    EmbeddingBudgetExhaustedError,
    EmbeddingCircuitOpenError,
    EmbeddingProviderError,
)
from noyra.sleep import FatigueTracker


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 17, tzinfo=UTC)

    def __call__(self) -> str:
        return self.value.isoformat(timespec="milliseconds")

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class ScriptedEmbeddingProvider:
    name = "scripted-embedding"

    def __init__(self, outcomes: Sequence[int | Exception]):
        self._outcomes = list(outcomes)
        self._lock = Lock()
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(vector) for vector in self.embed_with_usage(texts).vectors]

    def embed_with_usage(self, texts: Sequence[str]) -> EmbeddingProviderResponse:
        with self._lock:
            self.calls += 1
            outcome = self._outcomes.pop(0) if self._outcomes else 1
            request_id = f"embedding-request-{self.calls}"
        if isinstance(outcome, Exception):
            raise outcome
        return EmbeddingProviderResponse(
            tuple((1.0, 0.0) for _ in texts),
            EmbeddingUsage(outcome),
            request_id,
        )


class BlockingEmbeddingProvider:
    name = "blocking-embedding"

    def __init__(self, release: Event):
        self.release = release
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(vector) for vector in self.embed_with_usage(texts).vectors]

    def embed_with_usage(self, texts: Sequence[str]) -> EmbeddingProviderResponse:
        self.calls += 1
        self.release.wait(timeout=1)
        return EmbeddingProviderResponse(
            tuple((1.0, 0.0) for _ in texts),
            EmbeddingUsage(1),
        )


@pytest.fixture
def embedding_database(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-p110-embedding"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    return database, subject_id


def gateway_for(
    database: Database,
    subject_id: str,
    provider: Any,
    *,
    clock: MutableClock | None = None,
    limits: EmbeddingBudgetLimits | None = None,
    pricing: EmbeddingPricing | None = None,
    circuit_policy: EmbeddingCircuitPolicy | None = None,
    timeout_seconds: float = 1,
    accounting: Any = None,
    resource_id: str = "embedding-resource-test",
) -> EmbeddingGateway:
    clock = clock or MutableClock()
    return EmbeddingGateway(
        provider,
        EmbeddingLedger(database, clock=clock),
        subject_id=subject_id,
        resource_id=resource_id,
        model="embedding-test-v1",
        limits=limits or EmbeddingBudgetLimits(100, 1_000_000, 1_000_000),
        pricing=pricing or EmbeddingPricing(1_000_000),
        circuit_policy=circuit_policy or EmbeddingCircuitPolicy(3, 60),
        timeout_seconds=timeout_seconds,
        accounting=accounting,
    )


@pytest.mark.parametrize("dimension", ("calls", "tokens", "cost"))
def test_embedding_budgets_block_before_provider_call(
    embedding_database: tuple[Database, str], dimension: str
) -> None:
    database, subject_id = embedding_database
    provider = ScriptedEmbeddingProvider((1,))
    estimated = EmbeddingGateway.estimate_input_tokens(("bounded embedding request",))
    pricing = EmbeddingPricing(1_000_000)
    limits = {
        "calls": EmbeddingBudgetLimits(0, 1_000_000, 1_000_000),
        "tokens": EmbeddingBudgetLimits(10, estimated - 1, 1_000_000),
        "cost": EmbeddingBudgetLimits(10, 1_000_000, pricing.cost_microusd(estimated) - 1),
    }[dimension]
    gateway = gateway_for(
        database,
        subject_id,
        provider,
        limits=limits,
        pricing=pricing,
    )
    try:
        with pytest.raises(EmbeddingBudgetExhaustedError):
            gateway.embed(("bounded embedding request",))
        assert provider.calls == 0
        with database.connection() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM embedding_usage_entries").fetchone()[0]
                == 0
            )
            assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    finally:
        gateway.close()


def test_embedding_budget_authorization_is_atomic_under_concurrency(
    embedding_database: tuple[Database, str],
) -> None:
    database, subject_id = embedding_database
    provider = ScriptedEmbeddingProvider(tuple(1 for _ in range(8)))
    gateway = gateway_for(
        database,
        subject_id,
        provider,
        limits=EmbeddingBudgetLimits(2, 1_000_000, 1_000_000),
        pricing=EmbeddingPricing(0),
    )

    def invoke(index: int) -> str:
        try:
            gateway.embed((f"concurrent embedding {index}",))
        except EmbeddingBudgetExhaustedError:
            return "blocked"
        return "succeeded"

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(invoke, range(8)))
        assert outcomes.count("succeeded") == 2
        assert outcomes.count("blocked") == 6
        assert provider.calls == 2
        status = gateway.ledger.budget_status(subject_id, gateway.resource_id, gateway.limits)
        assert status.calls == 2
        assert gateway.ledger.verify_integrity(subject_id)["embedding_usage_entries"] == 2
    finally:
        gateway.close()


def test_embedding_usage_reconciles_provider_tokens_and_cost_durably(
    embedding_database: tuple[Database, str],
) -> None:
    database, subject_id = embedding_database

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-request-id": "provider-usage-1"},
            json={
                "data": [{"index": 0, "embedding": [1.0, 0.0]}],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            },
        )

    settings = EmbeddingSettings(
        base_url="https://embedding.example/v1",
        model="embedding-v1",
        api_key=SecretStr("not-persisted"),
    )
    provider = OpenAIEmbeddingProvider(settings, transport=httpx.MockTransport(handler))
    gateway = gateway_for(
        database,
        subject_id,
        provider,
        pricing=EmbeddingPricing(2_000_000),
    )
    try:
        assert gateway.embed(("usage reconciliation",)) == [[1.0, 0.0]]
        status = gateway.ledger.budget_status(subject_id, gateway.resource_id, gateway.limits)
        assert status.calls == 1
        assert status.input_tokens == 3
        assert status.cost_microusd == 6
        with database.connection() as connection:
            row = connection.execute("SELECT * FROM embedding_usage_entries").fetchone()
        assert row["status"] == "succeeded"
        assert row["usage_estimated"] == 0
        assert row["provider_request_id"] == "provider-usage-1"
        assert gateway.ledger.verify_integrity(subject_id) == {
            "embedding_usage_entries": 1,
            "embedding_circuit_states": 1,
            "embedding_circuit_transitions": 1,
        }
    finally:
        gateway.close()


def test_network_timeout_opens_circuit_and_holds_unknown_usage_reservation(
    embedding_database: tuple[Database, str],
) -> None:
    database, subject_id = embedding_database
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise httpx.ReadTimeout("injected timeout", request=request)

    settings = EmbeddingSettings(
        base_url="https://embedding.example/v1",
        model="embedding-v1",
        api_key=SecretStr("network-secret"),
    )
    provider = OpenAIEmbeddingProvider(settings, transport=httpx.MockTransport(handler))
    gateway = gateway_for(
        database,
        subject_id,
        provider,
        circuit_policy=EmbeddingCircuitPolicy(1, 60),
    )
    reserved_tokens = gateway.estimate_input_tokens(("network timeout",))
    try:
        with pytest.raises(EmbeddingProviderError, match="embedding_timeout"):
            gateway.embed(("network timeout",))
        with pytest.raises(EmbeddingCircuitOpenError):
            gateway.embed(("local fallback",))
        assert requests == 1
        status = gateway.ledger.budget_status(subject_id, gateway.resource_id, gateway.limits)
        assert status.calls == 1
        assert status.input_tokens == reserved_tokens
        assert gateway.ledger.circuit(subject_id, gateway.resource_id).status == "open"
        with database.connection() as connection:
            row = connection.execute("SELECT * FROM embedding_usage_entries").fetchone()
        assert row["status"] == "unknown"
        assert row["input_tokens"] is None
        assert row["reserved_tokens"] == reserved_tokens
    finally:
        gateway.close()


def test_embedding_circuit_allows_one_probe_and_recovers(
    embedding_database: tuple[Database, str],
) -> None:
    database, subject_id = embedding_database
    clock = MutableClock()
    failure = EmbeddingProviderError("provider_unavailable", usage_unknown=True)
    provider = ScriptedEmbeddingProvider((failure, failure, 2))
    gateway = gateway_for(
        database,
        subject_id,
        provider,
        clock=clock,
        circuit_policy=EmbeddingCircuitPolicy(2, 10),
    )
    try:
        for _ in range(2):
            with pytest.raises(EmbeddingProviderError, match="provider_unavailable"):
                gateway.embed(("provider recovery",))
        with pytest.raises(EmbeddingCircuitOpenError):
            gateway.embed(("provider recovery",))
        assert provider.calls == 2

        clock.advance(11)
        assert gateway.embed(("provider recovery",)) == [[1.0, 0.0]]
        circuit = gateway.ledger.circuit(subject_id, gateway.resource_id)
        assert circuit.status == "closed"
        assert circuit.consecutive_failures == 0
        with database.connection() as connection:
            states = [
                row["status"]
                for row in connection.execute(
                    "SELECT status FROM embedding_usage_entries ORDER BY created_at, usage_id"
                ).fetchall()
            ]
        assert states == ["unknown", "unknown", "succeeded"]
        assert gateway.ledger.verify_integrity(subject_id)["embedding_circuit_transitions"] == 5
    finally:
        gateway.close()


def test_restart_reconciles_unsent_and_ambiguous_embedding_usage(
    embedding_database: tuple[Database, str],
) -> None:
    database, subject_id = embedding_database
    clock = MutableClock()
    ledger = EmbeddingLedger(database, clock=clock)
    limits = EmbeddingBudgetLimits(10, 10_000, 10_000)
    policy = EmbeddingCircuitPolicy(2, 30)
    metadata = (
        subject_id,
        "embedding-resource-test",
        "restart-provider",
        "restart-model",
        "memory_embedding",
    )
    unsent, _ = ledger.authorize(
        *metadata,
        content_hash({"request": "unsent"}),
        "restart-unsent",
        limits,
        policy,
        text_count=1,
        reserved_tokens=20,
        reserved_cost_microusd=10,
    )
    executing, _ = ledger.authorize(
        *metadata,
        content_hash({"request": "executing"}),
        "restart-executing",
        limits,
        policy,
        text_count=1,
        reserved_tokens=30,
        reserved_cost_microusd=15,
    )
    ledger.start(executing.usage_id)

    restarted = EmbeddingLedger(database, clock=clock)
    assert restarted.recover_interrupted(subject_id, circuit_policy=policy) == 2
    status = restarted.budget_status(subject_id, metadata[1], limits)
    assert status.calls == 1
    assert status.input_tokens == 30
    assert restarted.circuit(subject_id, metadata[1]).status == "open"
    with database.connection() as connection:
        rows = {
            row["usage_id"]: row["status"]
            for row in connection.execute(
                "SELECT usage_id, status FROM embedding_usage_entries"
            ).fetchall()
        }
    assert rows == {unsent.usage_id: "cancelled", executing.usage_id: "unknown"}
    assert restarted.verify_integrity(subject_id)["embedding_usage_entries"] == 2


def test_restart_releases_authorized_half_open_probe(
    embedding_database: tuple[Database, str],
) -> None:
    database, subject_id = embedding_database
    clock = MutableClock()
    ledger = EmbeddingLedger(database, clock=clock)
    limits = EmbeddingBudgetLimits(10, 10_000, 10_000)
    policy = EmbeddingCircuitPolicy(1, 30)
    metadata = (
        subject_id,
        "embedding-resource-half-open-restart",
        "restart-provider",
        "restart-model",
        "memory_embedding",
    )
    failed, _ = ledger.authorize(
        *metadata,
        content_hash({"request": "open-circuit"}),
        "restart-open-circuit",
        limits,
        policy,
        text_count=1,
        reserved_tokens=20,
        reserved_cost_microusd=10,
    )
    ledger.start(failed.usage_id)
    ledger.fail(
        failed.usage_id,
        error_code="provider_unavailable",
        usage_unknown=True,
        circuit_policy=policy,
    )
    clock.advance(31)
    abandoned_probe, _ = ledger.authorize(
        *metadata,
        content_hash({"request": "abandoned-probe"}),
        "restart-abandoned-probe",
        limits,
        policy,
        text_count=1,
        reserved_tokens=20,
        reserved_cost_microusd=10,
    )
    assert ledger.circuit(subject_id, metadata[1]).status == "half_open"

    restarted = EmbeddingLedger(database, clock=clock)
    assert restarted.recover_interrupted(subject_id, circuit_policy=policy) == 1
    with database.connection() as connection:
        probe_status = connection.execute(
            "SELECT status FROM embedding_usage_entries WHERE usage_id = ?",
            (abandoned_probe.usage_id,),
        ).fetchone()["status"]
    assert probe_status == "cancelled"
    recovered = restarted.circuit(subject_id, metadata[1])
    assert recovered.status == "open"
    assert recovered.next_probe_at == clock()
    assert recovered.probe_usage_id is None

    next_probe, _ = restarted.authorize(
        *metadata,
        content_hash({"request": "next-probe"}),
        "restart-next-probe",
        limits,
        policy,
        text_count=1,
        reserved_tokens=20,
        reserved_cost_microusd=10,
    )
    assert next_probe.status == "authorized"
    assert restarted.circuit(subject_id, metadata[1]).status == "half_open"
    assert restarted.verify_integrity(subject_id)["embedding_usage_entries"] == 3


def test_hard_deadline_returns_without_waiting_for_stalled_provider(
    embedding_database: tuple[Database, str],
) -> None:
    database, subject_id = embedding_database
    release = Event()
    provider = BlockingEmbeddingProvider(release)
    gateway = gateway_for(
        database,
        subject_id,
        provider,
        timeout_seconds=0.05,
        circuit_policy=EmbeddingCircuitPolicy(1, 60),
    )
    started = time.monotonic()
    try:
        with pytest.raises(EmbeddingProviderError, match="embedding_hard_timeout"):
            gateway.embed(("stalled provider",))
        assert time.monotonic() - started < 0.5
        assert gateway.ledger.circuit(subject_id, gateway.resource_id).status == "open"
    finally:
        release.set()
        gateway.close()


def test_recall_falls_back_locally_and_failure_is_counted_as_fatigue(
    embedding_database: tuple[Database, str],
) -> None:
    database, subject_id = embedding_database
    events = EventStore(database)
    event = events.append(subject_id, "observation", "test", {"value": "local recall"})
    plain_store = MemoryStore(database)
    memory = plain_store.create(
        subject_id,
        "semantic",
        "Local fallback preserves this relevant memory.",
        salience=0.7,
        confidence=0.8,
        source_event_ids=(event.event_id,),
    )
    tracker = FatigueTracker(database)
    tracker.ensure(subject_id)
    tracker.set_pool_pressures(subject_id, {"economy": 0.0, "deep": 0.0})
    cycle = object.__new__(CognitionCycle)
    cycle.subject_id = subject_id
    cycle.fatigue = tracker
    provider = ScriptedEmbeddingProvider(
        (EmbeddingProviderError("provider_unavailable", usage_unknown=True),)
    )
    gateway = gateway_for(
        database,
        subject_id,
        provider,
        circuit_policy=EmbeddingCircuitPolicy(1, 60),
        accounting=cycle._record_embedding_accounting,
    )
    vector = [1.0, 0.0]
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO memory_embeddings(memory_id, subject_id, provider, dimensions, "
            "vector_json, vector_hash, updated_at) VALUES (?, ?, ?, 2, ?, ?, ?)",
            (
                memory.memory_id,
                subject_id,
                gateway.name,
                canonical_json(vector),
                content_hash(vector),
                "2026-08-17T00:00:00.000+00:00",
            ),
        )
    store = MemoryStore(database, embedding_index=MemoryEmbeddingIndex(database, gateway))
    try:
        recalls = store.recall(
            subject_id,
            "local fallback relevant memory",
            context_type="test",
            context_id="fallback",
        )
        assert recalls[0].memory.memory_id == memory.memory_id
        assert recalls[0].lexical_score > 0
        assert tracker.pool_pressures(subject_id)["embedding"] == 1.0
        with database.connection() as connection:
            transition = connection.execute(
                "SELECT reason FROM fatigue_transitions WHERE subject_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
        assert transition["reason"] == "embedding resource outcome: provider_unavailable"
    finally:
        gateway.close()


def test_embedding_usage_tamper_is_a_registry_p0(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-p110-integrity"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    gateway = gateway_for(database, subject_id, ScriptedEmbeddingProvider((1,)))
    try:
        gateway.embed(("integrity fixture",))
        with database.transaction() as connection:
            connection.execute(
                "UPDATE embedding_usage_entries SET input_tokens = reserved_tokens + 1"
            )
        with pytest.raises(IntegrityError):
            gateway.ledger.verify_integrity(subject_id)
        report = IntegrityRegistry().run(
            database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("model.embedding_resources",),
        )
        assert report.status == "corrupt"
        assert report.p0 == ("model.embedding_resources:integrity_error",)
    finally:
        gateway.close()


def test_foreign_embedding_usage_bound_to_subject_resource_is_a_registry_p0(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    owner_id = "Noyra-p110-resource-owner"
    foreign_id = "Noyra-p110-foreign-usage"
    identities = IdentityStore(database)
    identities.ensure(owner_id, content_hash({"subject": owner_id}))
    identities.ensure(foreign_id, content_hash({"subject": foreign_id}))
    store = EmbeddingResourceStore(database, tmp_path / "secrets" / "embedding")
    resource = store.configure(
        owner_id,
        EmbeddingResourceInput(
            label="owner-only embedding",
            base_url="https://embedding.example/v1",
            model="embedding-v1",
            api_key=SecretStr("owner-secret"),
        ),
        actor="operator",
    )
    gateway = gateway_for(
        database,
        foreign_id,
        ScriptedEmbeddingProvider((1,)),
        resource_id=resource.config_id,
    )
    try:
        gateway.embed(("foreign ownership fixture",))
        report = IntegrityRegistry().run(
            database,
            owner_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("model.embedding_resources",),
        )
        assert report.status == "corrupt"
        assert report.p0 == ("model.embedding_resources:integrity_error",)
    finally:
        gateway.close()
