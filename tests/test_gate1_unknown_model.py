from __future__ import annotations

import asyncio
import errno
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from noyra.core import IdentityStore
from noyra.core.database import Database
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.locking import ProcessLock
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import content_hash
from noyra.model import CognitiveResourceGroupInput, CognitiveResourceStore, ModelMessage
from noyra.model.errors import ProviderCallError
from noyra.model.fake import FakeProvider
from noyra.model.ledger import ModelLedger
from noyra.model.resources import RoutedModelGateway
from noyra.model.types import ModelUsage, ProviderResponse


class Insight(BaseModel):
    summary: str
    confidence: float


def _setup(tmp_path: Path) -> tuple[Database, str, CognitiveResourceStore, Any, str]:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-gate1-unknown"
    IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
    resources = CognitiveResourceStore(database, tmp_path / "secrets")
    group = resources.configure(
        subject_id,
        CognitiveResourceGroupInput(
            pool="economy",
            label="unknown-test",
            base_url="https://models.example/v1",
            model="unknown-test",
            api_keys=(SecretStr("first-key"), SecretStr("second-key")),
            max_attempts=1,
        ),
        actor="operator",
    )
    provider = FakeProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            ProviderResponse('{"summary":"retry-ok","confidence":0.9}', ModelUsage(4, 2)),
        ]
    )
    return database, subject_id, resources, provider, group.group_id


def _gateway(
    database: Database,
    subject_id: str,
    resources: CognitiveResourceStore,
    provider: Any,
) -> RoutedModelGateway:
    return RoutedModelGateway(
        database,
        subject_id,
        resources,
        provider_factory=lambda _settings: provider,
        cooldown_seconds=60,
    )


async def _complete(gateway: RoutedModelGateway, subject_id: str, key: str) -> Any:
    return await gateway.complete_structured(
        subject_id,
        "world_cognition:unknown",
        [ModelMessage(role="user", content="Return JSON")],
        Insight,
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_concurrent_logical_request_has_single_provider_owner(tmp_path: Path) -> None:
    database, subject_id, resources, _provider, _group_id = _setup(tmp_path)

    class BlockingProvider:
        name = "blocking"

        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.requests: list[Any] = []

        async def complete(self, request: Any) -> ProviderResponse:
            self.requests.append(request)
            self.entered.set()
            await self.release.wait()
            return ProviderResponse(
                '{"summary":"single-owner","confidence":0.95}',
                ModelUsage(4, 2),
            )

    provider = BlockingProvider()
    first_gateway = _gateway(database, subject_id, resources, provider)
    second_gateway = _gateway(database, subject_id, resources, provider)
    first = asyncio.create_task(_complete(first_gateway, subject_id, "logical-concurrent"))
    await provider.entered.wait()
    with pytest.raises(ProviderCallError) as collision:
        await _complete(second_gateway, subject_id, "logical-concurrent")
    assert collision.value.code == "routed_model_call_in_progress"
    assert collision.value.outcome_unknown is True
    assert len(provider.requests) == 1
    provider.release.set()
    result = await first
    assert result.output.summary == "single-owner"
    assert len(provider.requests) == 1
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM model_calls WHERE subject_id = ? AND purpose = ?",
                (subject_id, "world_cognition:unknown"),
            ).fetchone()[0]
            == 1
        )
    assert RoutedModelGateway._logical_route_locks == {}


@pytest.mark.asyncio
async def test_route_lock_contention_fails_closed_before_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, subject_id, resources, provider, _group_id = _setup(tmp_path)
    failure = RuntimeOwnershipError("injected route lock contention")

    def fail_acquire(_lock: ProcessLock) -> bool:
        raise failure

    monkeypatch.setattr(ProcessLock, "acquire", fail_acquire)

    with pytest.raises(ProviderCallError) as collision:
        await _complete(
            _gateway(database, subject_id, resources, provider),
            subject_id,
            "logical-cross-process",
        )

    assert collision.value.code == "routed_model_call_in_progress"
    assert collision.value.outcome_unknown is True
    assert provider.requests == []


@pytest.mark.asyncio
async def test_route_lock_io_error_is_not_misreported_as_in_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, subject_id, resources, provider, _group_id = _setup(tmp_path)
    failure = OSError(errno.EIO, "injected model route lock I/O error")

    def fail_acquire(_lock: ProcessLock) -> bool:
        raise failure

    monkeypatch.setattr(ProcessLock, "acquire", fail_acquire)

    with pytest.raises(OSError) as caught:
        await _complete(
            _gateway(database, subject_id, resources, provider),
            subject_id,
            "logical-lock-io-error",
        )

    assert caught.value is failure
    assert provider.requests == []
    assert RoutedModelGateway._logical_route_locks == {}


@pytest.mark.asyncio
async def test_unknown_logical_request_is_quarantined_across_ticks_and_restart(
    tmp_path: Path,
) -> None:
    database, subject_id, resources, provider, group_id = _setup(tmp_path)
    first = _gateway(database, subject_id, resources, provider)
    with pytest.raises(ProviderCallError) as caught:
        await _complete(first, subject_id, "logical-unknown")
    assert caught.value.outcome_unknown is True
    assert len(provider.requests) == 1

    with database.connection() as connection:
        physical = connection.execute(
            "SELECT call_id, idempotency_key, status FROM model_calls "
            "WHERE subject_id = ? AND purpose = ?",
            (subject_id, "world_cognition:unknown"),
        ).fetchall()
        attempts = connection.execute(
            "SELECT COUNT(*) FROM model_attempts WHERE call_id = ?", (physical[0]["call_id"],)
        ).fetchone()[0]
        route_unknown = connection.execute(
            "SELECT COUNT(*) FROM cognitive_route_attempts WHERE outcome = 'unknown'"
        ).fetchone()[0]
        waiting = connection.execute(
            "SELECT status, reason_code FROM waiting_cognitive_tasks "
            "WHERE subject_id = ? AND purpose = ?",
            (subject_id, "world_cognition:unknown"),
        ).fetchone()
    assert len(physical) == 1
    assert physical[0]["status"] == "unknown"
    assert attempts == 1
    assert route_unknown == 1
    assert waiting["status"] == "waiting"
    assert waiting["reason_code"] == "model_outcome_unknown_quarantined"

    # A repeated tick must not wait for the resource backoff and then rotate
    # to the second key.
    with pytest.raises(ProviderCallError) as repeated:
        await _complete(first, subject_id, "logical-unknown")
    assert repeated.value.code == "routed_model_call_quarantined"

    # Even after the ordinary resource backoff expires, the logical unknown
    # fence wins over the waiting scheduler.
    with database.transaction() as connection:
        connection.execute(
            "UPDATE waiting_cognitive_tasks SET next_retry_at = ? "
            "WHERE subject_id = ? AND purpose = ?",
            ("2000-01-01T00:00:00.000+00:00", subject_id, "world_cognition:unknown"),
        )
    with pytest.raises(ProviderCallError) as expired:
        await _complete(first, subject_id, "logical-unknown")
    assert expired.value.code == "routed_model_call_quarantined"

    # Constructing a fresh gateway is the restart simulation.  The durable
    # model_calls row remains the fence, so no provider request is made.
    restarted = _gateway(database, subject_id, resources, provider)
    with pytest.raises(ProviderCallError):
        await _complete(restarted, subject_id, "logical-unknown")
    assert len(provider.requests) == 1
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM model_calls WHERE subject_id = ? AND purpose = ?",
                (subject_id, "world_cognition:unknown"),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM model_attempts WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()[0]
            == 1
        )
    assert sorted(
        key.selection_count for key in resources.keys(group_id, subject_id=subject_id)
    ) == [0, 1]


@pytest.mark.asyncio
async def test_malformed_retry_audit_json_cannot_break_logical_fence(tmp_path: Path) -> None:
    database, subject_id, resources, provider, _group_id = _setup(tmp_path)
    gateway = _gateway(database, subject_id, resources, provider)
    with pytest.raises(ProviderCallError):
        await _complete(gateway, subject_id, "logical-malformed-audit")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO audit_records(audit_id, subject_id, action, actor, payload_json, "
            "occurred_at) VALUES ('audit-malformed-gate1', ?, "
            "'model_unknown_retry_authorized', 'operator', '{', "
            "'2026-08-19T00:00:00.000+00:00')",
            (subject_id,),
        )

    with pytest.raises(ProviderCallError) as fenced:
        await _complete(gateway, subject_id, "logical-malformed-audit")
    assert fenced.value.code == "routed_model_call_quarantined"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_route_key_delimiter_cannot_alias_another_logical_request(tmp_path: Path) -> None:
    database, subject_id, resources, _provider, group_id = _setup(tmp_path)
    provider = FakeProvider(
        [
            ProviderResponse('{"summary":"request-b","confidence":0.8}', ModelUsage(4, 2)),
            ProviderResponse('{"summary":"request-a","confidence":0.9}', ModelUsage(4, 2)),
        ]
    )
    gateway = _gateway(database, subject_id, resources, provider)
    crafted = f"foo:pool:economy:group:{group_id}:key:"
    result_b = await _complete(gateway, subject_id, crafted)
    result_a = await _complete(gateway, subject_id, "foo")
    assert result_b.output.summary == "request-b"
    assert result_a.output.summary == "request-a"
    assert result_a.cached is False
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_malformed_legacy_route_is_quarantined_not_silently_rerouted(
    tmp_path: Path,
) -> None:
    database, subject_id, resources, provider, group_id = _setup(tmp_path)
    gateway = _gateway(database, subject_id, resources, provider)
    with pytest.raises(ProviderCallError):
        await _complete(gateway, subject_id, "legacy-malformed-route")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE model_calls SET idempotency_key = ? WHERE subject_id = ?",
            (
                "legacy-malformed-route:pool:economy:group:"
                f"{group_id}:key:tampered:selection:1:pool:economy:group:evil:key:x:selection:1",
                subject_id,
            ),
        )

    with pytest.raises(ProviderCallError) as quarantined:
        await _complete(gateway, subject_id, "legacy-malformed-route")
    assert quarantined.value.code == "routed_model_call_quarantined"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_unknown_retry_requires_authorization_and_reuses_original_route(
    tmp_path: Path,
) -> None:
    database, subject_id, resources, provider, group_id = _setup(tmp_path)
    gateway = _gateway(database, subject_id, resources, provider)
    with pytest.raises(ProviderCallError):
        await _complete(gateway, subject_id, "logical-explicit-retry")

    ledger = ModelLedger(database)
    call = ledger.unknown_calls(subject_id)[0]
    original_key = call.idempotency_key
    original_group = call.resource_group_id
    ledger.prepare_unknown_retry(
        call.call_id,
        actor="operator",
        reason="provider confirmed the request was not accepted",
    )

    result = await _complete(
        _gateway(database, subject_id, resources, provider),
        subject_id,
        "logical-explicit-retry",
    )
    assert result.output.summary == "retry-ok"
    assert len(provider.requests) == 2

    with database.connection() as connection:
        rows = connection.execute(
            "SELECT call_id, idempotency_key, resource_group_id, status "
            "FROM model_calls WHERE subject_id = ? AND purpose = ?",
            (subject_id, "world_cognition:unknown"),
        ).fetchall()
        attempts = connection.execute(
            "SELECT attempt_number, status FROM model_attempts WHERE call_id = ? "
            "ORDER BY attempt_number",
            (call.call_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["call_id"] == call.call_id
    assert rows[0]["idempotency_key"] == original_key
    assert rows[0]["resource_group_id"] == original_group == group_id
    assert rows[0]["status"] == "succeeded"
    assert [(row["attempt_number"], row["status"]) for row in attempts] == [
        (1, "unknown"),
        (2, "succeeded"),
    ]
    # The retry is bound to the first key; selection_count is not incremented
    # and the second key is never selected as a failover.
    assert sorted(
        key.selection_count for key in resources.keys(group_id, subject_id=subject_id)
    ) == [0, 1]


@pytest.mark.asyncio
async def test_one_retry_authorization_cannot_be_reused_after_known_failure(
    tmp_path: Path,
) -> None:
    database, subject_id, resources, _provider, _group_id = _setup(tmp_path)
    provider = FakeProvider(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            ProviderCallError("known_retry_failure", retryable=False, outcome_unknown=False),
            ProviderResponse('{"summary":"must-not-run","confidence":0.9}', ModelUsage(4, 2)),
        ]
    )
    gateway = _gateway(database, subject_id, resources, provider)
    with pytest.raises(ProviderCallError):
        await _complete(gateway, subject_id, "one-retry-only")
    call = ModelLedger(database).unknown_calls(subject_id)[0]
    ModelLedger(database).prepare_unknown_retry(
        call.call_id,
        actor="operator",
        reason="provider approved exactly one replay",
    )
    with pytest.raises(ProviderCallError) as retried:
        await _complete(
            _gateway(database, subject_id, resources, provider),
            subject_id,
            "one-retry-only",
        )
    assert retried.value.code == "known_retry_failure"
    assert len(provider.requests) == 2

    with pytest.raises(ProviderCallError) as repeated:
        await _complete(
            _gateway(database, subject_id, resources, provider),
            subject_id,
            "one-retry-only",
        )
    assert repeated.value.code == "routed_model_call_quarantined"
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_operator_cancelling_prepared_retry_releases_route_fence(tmp_path: Path) -> None:
    database, subject_id, resources, provider, _group_id = _setup(tmp_path)
    gateway = _gateway(database, subject_id, resources, provider)
    with pytest.raises(ProviderCallError):
        await _complete(gateway, subject_id, "prepared-retry-cancel")
    call = ModelLedger(database).unknown_calls(subject_id)[0]
    ModelLedger(database).prepare_unknown_retry(
        call.call_id,
        actor="operator",
        reason="authorize review before execution",
    )
    controls = OperatorControlService(SimpleNamespace(database=database, subject_id=subject_id))
    result = controls.reconcile_model_call(
        call.call_id,
        actor="operator",
        reason="provider confirmed retry should be cancelled",
        outcome="failed",
        response=None,
    )
    assert result["status"] == "failed"
    with database.transaction() as connection:
        connection.execute(
            "UPDATE waiting_cognitive_tasks SET next_retry_at = ? "
            "WHERE subject_id = ? AND purpose = ?",
            ("2000-01-01T00:00:00.000+00:00", subject_id, "world_cognition:unknown"),
        )
    rerouted = await _complete(
        _gateway(database, subject_id, resources, provider),
        subject_id,
        "prepared-retry-cancel",
    )
    assert rerouted.output.summary == "retry-ok"
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_operator_reconcile_failed_releases_unknown_fence_for_normal_reroute(
    tmp_path: Path,
) -> None:
    database, subject_id, resources, provider, _group_id = _setup(tmp_path)
    gateway = _gateway(database, subject_id, resources, provider)
    with pytest.raises(ProviderCallError):
        await _complete(gateway, subject_id, "logical-reconcile-failed")

    call = ModelLedger(database).unknown_calls(subject_id)[0]
    ModelLedger(database).reconcile_unknown(
        call.call_id,
        outcome="failed",
        actor="operator",
        reason="provider confirmed the request was not accepted",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE waiting_cognitive_tasks SET next_retry_at = ? "
            "WHERE subject_id = ? AND purpose = ?",
            (
                "2000-01-01T00:00:00.000+00:00",
                subject_id,
                "world_cognition:unknown",
            ),
        )

    rerouted = await _complete(
        _gateway(database, subject_id, resources, provider),
        subject_id,
        "logical-reconcile-failed",
    )
    assert rerouted.output.summary == "retry-ok"
    assert len(provider.requests) == 2
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT idempotency_key, status FROM model_calls "
            "WHERE subject_id = ? AND purpose = ? ORDER BY created_at, call_id",
            (subject_id, "world_cognition:unknown"),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["status"] == "failed"
    assert rows[1]["status"] == "succeeded"
    assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]


@pytest.mark.asyncio
async def test_operator_reconcile_succeeded_is_cached_without_provider_recall(
    tmp_path: Path,
) -> None:
    database, subject_id, resources, provider, _group_id = _setup(tmp_path)
    gateway = _gateway(database, subject_id, resources, provider)
    with pytest.raises(ProviderCallError):
        await _complete(gateway, subject_id, "logical-reconcile-success")

    call = ModelLedger(database).unknown_calls(subject_id)[0]
    ModelLedger(database).reconcile_unknown(
        call.call_id,
        outcome="succeeded",
        actor="operator",
        reason="provider supplied a durable response record",
        response={
            "content": '{"summary":"operator-confirmed","confidence":0.8}',
            "usage": {"input_tokens": 4, "output_tokens": 2},
            "cost_microusd": 0,
            "attempts": 1,
        },
    )
    result = await _complete(
        _gateway(database, subject_id, resources, provider),
        subject_id,
        "logical-reconcile-success",
    )
    assert result.output.summary == "operator-confirmed"
    assert result.cached is True
    assert len(provider.requests) == 1
