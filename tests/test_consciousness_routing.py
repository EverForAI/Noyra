from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from noyra.cognition.consciousness import ConsciousnessFrameStore
from noyra.core import IdentityStore
from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.model import (
    CognitiveResourceGroupInput,
    CognitiveResourceStore,
    ModelMessage,
    RoutedModelGateway,
)
from noyra.model.errors import ProviderCallError
from noyra.model.fake import FakeProvider
from noyra.model.types import BudgetLimits, ModelUsage, ProviderResponse


class Insight(BaseModel):
    summary: str
    confidence: float


def make_database(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-m24-test"
    IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
    return database, subject_id


def test_consciousness_restart_chain_and_duplicate_idle(tmp_path: Path) -> None:
    database, subject_id = make_database(tmp_path)
    first = ConsciousnessFrameStore(database, subject_id)
    assert first.ensure_initial().sequence_number == 1
    assert first.observe_result("world_sources_waiting").sequence_number == 2
    assert first.observe_result("world_sources_waiting").sequence_number == 2
    restarted = ConsciousnessFrameStore(database, subject_id)
    assert restarted.ensure_initial().sequence_number == 2
    assert restarted.verify_integrity()["consciousness_frames"] == 2

    with database.transaction() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE consciousness_frames SET reason_code = 'tampered' WHERE sequence_number = 1"
        )


def test_consciousness_integrity_rejects_blob_sequence_and_json(tmp_path: Path) -> None:
    database, subject_id = make_database(tmp_path)
    store = ConsciousnessFrameStore(database, subject_id)
    frame = store.ensure_initial()
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_consciousness_frame_update")
    with database.connection() as connection:
        original = dict(
            connection.execute(
                "SELECT * FROM consciousness_frames WHERE frame_id = ?", (frame.frame_id,)
            ).fetchone()
        )
    columns = (
        "sequence_number",
        "internal_changes_json",
        "world_changes_json",
        "unresolved_tensions_json",
        "candidate_workflows_json",
        "wake_condition_json",
    )
    for column in columns:
        with pytest.raises(IntegrityError):
            with database.transaction() as connection:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute(
                    f'UPDATE consciousness_frames SET "{column}" = ? WHERE frame_id = ?',
                    (sqlite3.Binary(str(original[column]).encode("utf-8")), frame.frame_id),
                )
            store.verify_integrity()
        with database.transaction() as connection:
            connection.execute(
                f'UPDATE consciousness_frames SET "{column}" = ? WHERE frame_id = ?',
                (original[column], frame.frame_id),
            )


def test_cognitive_resource_secrets_stay_outside_database(tmp_path: Path) -> None:
    database, subject_id = make_database(tmp_path)
    secret_dir = tmp_path / "secrets"
    store = CognitiveResourceStore(database, secret_dir)
    record = store.configure(
        subject_id,
        CognitiveResourceGroupInput(
            pool="economy",
            label="cheap",
            base_url="https://models.example/v1",
            model="cheap-model",
            api_keys=(SecretStr("m24-secret-key"),),
        ),
        actor="operator",
    )
    assert (
        store.api_key(
            store.keys(record.group_id, subject_id=subject_id)[0].key_id,
            subject_id=subject_id,
        )
        == "m24-secret-key"
    )
    non_secret_files = (
        path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and path.suffix != ".key"
    )
    assert b"m24-secret-key" not in b"".join(non_secret_files)


def test_pool_budget_isolation(tmp_path: Path) -> None:
    database, subject_id = make_database(tmp_path)
    from noyra.model.ledger import ModelLedger

    ledger = ModelLedger(database)
    call, _ = ledger.prepare_call(
        subject_id,
        "provider",
        "model",
        "economy:test",
        "hash",
        "key",
        resource_pool="economy",
    )
    ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(1, 100, 100, 100),
        reserved_input_tokens=10,
        reserved_output_tokens=10,
        reserved_cost_microusd=10,
    )
    assert (
        ledger.budget_status(
            subject_id, BudgetLimits(10, 1000, 1000, 1000), resource_pool="deep"
        ).attempts
        == 0
    )
    assert (
        ledger.budget_status(
            subject_id, BudgetLimits(10, 1000, 1000, 1000), resource_pool="economy"
        ).attempts
        == 1
    )


@pytest.mark.asyncio
async def test_provider_failover_stays_inside_selected_pool(tmp_path: Path) -> None:
    database, subject_id = make_database(tmp_path)
    store = CognitiveResourceStore(database, tmp_path / "model-secrets")
    economy_groups = [
        store.configure(
            subject_id,
            CognitiveResourceGroupInput(
                pool="economy",
                label=label,
                base_url=f"https://{label}.example/v1",
                model=label,
                api_keys=(SecretStr(f"{label}-secret"),),
                max_attempts=1,
            ),
            actor="operator",
        )
        for label in ("first", "second")
    ]
    store.configure(
        subject_id,
        CognitiveResourceGroupInput(
            pool="deep",
            label="deep",
            base_url="https://deep.example/v1",
            model="deep",
            api_keys=(SecretStr("deep-secret"),),
        ),
        actor="operator",
    )
    providers = {
        "first": FakeProvider(
            [ProviderCallError("first_failed", retryable=False, outcome_unknown=False)]
        ),
        "second": FakeProvider(
            [
                ProviderResponse(
                    '{"summary":"ok","confidence":0.8}',
                    ModelUsage(4, 2),
                )
            ]
        ),
        "deep": FakeProvider([]),
    }

    def factory(settings: Any) -> FakeProvider:
        return providers[str(settings.model)]

    gateway = RoutedModelGateway(
        database,
        subject_id,
        store,
        provider_factory=factory,
        cooldown_seconds=60,
    )
    result = await gateway.complete_structured(
        subject_id,
        "world_cognition:one",
        [ModelMessage(role="user", content="Return JSON")],
        Insight,
        idempotency_key="failover",
    )
    assert result.output.summary == "ok"
    assert not providers["deep"].requests
    assert len(providers["first"].requests) == 1
    assert len(providers["second"].requests) == 1
    assert {record.group_id for record in economy_groups} == {
        store.get(economy_groups[0].group_id, subject_id=subject_id).group_id,
        store.get(economy_groups[1].group_id, subject_id=subject_id).group_id,
    }


@pytest.mark.asyncio
async def test_explicit_model_probe_targets_one_resource_and_is_budgeted(tmp_path: Path) -> None:
    database, subject_id = make_database(tmp_path)
    store = CognitiveResourceStore(database, tmp_path / "model-secrets")
    group = store.configure(
        subject_id,
        CognitiveResourceGroupInput(
            pool="economy",
            label="probe",
            base_url="https://probe.example/v1",
            model="probe-model",
            api_keys=(SecretStr("probe-secret"),),
            max_attempts=1,
        ),
        actor="operator",
    )
    provider = FakeProvider([ProviderResponse('{"ok":true}', ModelUsage(7, 2))])
    gateway = RoutedModelGateway(
        database,
        subject_id,
        store,
        provider_factory=lambda _settings: provider,
    )

    result = await gateway.test_resource(group.group_id, subject_id=subject_id)

    assert result["status"] == "succeeded"
    assert result["ok"] is True
    assert result["group_id"] == group.group_id
    assert len(provider.requests) == 1
    with database.connection() as connection:
        call = connection.execute(
            "SELECT purpose, resource_pool, resource_group_id, status "
            "FROM model_calls WHERE subject_id = ? ORDER BY created_at DESC LIMIT 1",
            (subject_id,),
        ).fetchone()
    assert call["purpose"] == f"operator_model_test:{group.group_id}"
    assert call["resource_pool"] == "economy"
    assert call["resource_group_id"] == group.group_id
    assert call["status"] == "succeeded"


@pytest.mark.asyncio
async def test_one_group_rotates_keys_and_deduplicates_waiting_task(tmp_path: Path) -> None:
    database, subject_id = make_database(tmp_path)
    store = CognitiveResourceStore(database, tmp_path / "rotating-secrets")
    group = store.configure(
        subject_id,
        CognitiveResourceGroupInput(
            pool="economy",
            label="rotating",
            base_url="https://rotating.example/v1",
            model="rotating",
            api_keys=(SecretStr("first-key"), SecretStr("second-key")),
            max_attempts=1,
        ),
        actor="operator",
    )
    provider = FakeProvider(
        [
            ProviderCallError("key_failed", retryable=False, outcome_unknown=False),
            ProviderResponse('{"summary":"rotated","confidence":0.7}', ModelUsage(3, 2)),
        ]
    )
    gateway = RoutedModelGateway(
        database,
        subject_id,
        store,
        provider_factory=lambda _: provider,
        cooldown_seconds=60,
    )
    result = await gateway.complete_structured(
        subject_id,
        "world_cognition:rotation",
        [ModelMessage(role="user", content="Return JSON")],
        Insight,
        idempotency_key="rotate",
    )
    assert result.output.summary == "rotated"
    keys = store.keys(group.group_id, subject_id=subject_id)
    assert sorted(record.selection_count for record in keys) == [1, 1]

    unavailable = RoutedModelGateway(database, subject_id, store, max_group_failovers=0)
    for _ in range(2):
        with pytest.raises(ProviderCallError):
            await unavailable.complete_structured(
                subject_id,
                "world_cognition:waiting",
                [ModelMessage(role="user", content="Return JSON")],
                Insight,
                idempotency_key="waiting",
            )
    with database.connection() as connection:
        waiting = connection.execute(
            "SELECT retry_count FROM waiting_cognitive_tasks WHERE purpose = ?",
            ("world_cognition:waiting",),
        ).fetchone()
    assert waiting["retry_count"] == 1


def test_cooldown_keys_are_not_reported_available(tmp_path: Path) -> None:
    database, subject_id = make_database(tmp_path)
    store = CognitiveResourceStore(database, tmp_path / "cooldown-secrets")
    group = store.configure(
        subject_id,
        CognitiveResourceGroupInput(
            pool="deep",
            label="cooldown",
            base_url="https://cooldown.example/v1",
            model="deep",
            api_keys=(SecretStr("cooldown-key"),),
        ),
        actor="operator",
    )
    store.record_failure(
        store.keys(group.group_id, subject_id=subject_id)[0].key_id,
        "test",
        cooldown_seconds=3600,
        subject_id=subject_id,
    )
    assert store.get(group.group_id, subject_id=subject_id).available_key_count == 0
