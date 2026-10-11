from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel, SecretStr

from noyra.core import Database, IdentityStore
from noyra.core import database as database_module
from noyra.core.types import content_hash
from noyra.core.usage_projection import UsageProjection, usage_window
from noyra.model import (
    FakeProvider,
    ModelGateway,
    ModelMessage,
    ModelPricing,
    ProviderResponse,
    RetryPolicy,
)
from noyra.model.ledger import ModelLedger
from noyra.model.resources import (
    CognitiveResourceGroupInput,
    CognitiveResourceGroupUpdate,
    CognitiveResourceStore,
)
from noyra.model.types import BudgetLimits, ModelUsage


@pytest.mark.parametrize("window", ["today", "3", "7", "30", "90", "all"])
def test_six_usage_windows(window: str) -> None:
    now = datetime(2026, 10, 10, 1, 30, tzinfo=UTC)
    start, end = usage_window(window, 480, now)
    assert end == now
    if window == "today":
        assert start == datetime(2026, 10, 9, 16, tzinfo=UTC)
    elif window == "all":
        assert start is None
    else:
        assert start == now - timedelta(days=int(window))


@pytest.mark.parametrize("window,offset", [("1", 0), ("7", 841), ("all", -841)])
def test_invalid_window_timezone(window: str, offset: int) -> None:
    with pytest.raises(ValueError):
        usage_window(window, offset, datetime.now(UTC))


def test_cross_window_retry_uses_attempt_time_and_is_not_duplicate(tmp_path: Path) -> None:
    database = Database(tmp_path / "db")
    subject = "usage-test"
    IdentityStore(database).ensure(subject, content_hash("usage"))
    ledger = ModelLedger(database)
    call, _ = ledger.prepare_call(
        subject,
        "fake",
        "test",
        "test",
        content_hash("req"),
        "request-key",
        resource_pool="economy",
    )
    limits = BudgetLimits(100, 100_000, 100_000, 100_000)
    first = ledger.authorize_attempt(
        call.call_id,
        limits,
        reserved_input_tokens=100,
        reserved_output_tokens=100,
        reserved_cost_microusd=10,
    )
    ledger.start_attempt(first.attempt_id)
    ledger.finish_attempt(
        first.attempt_id,
        "failed",
        usage=ModelUsage(2, 3),
        cost_microusd=1,
        error_code="timeout",
    )
    second = ledger.authorize_attempt(
        call.call_id,
        limits,
        reserved_input_tokens=100,
        reserved_output_tokens=100,
        reserved_cost_microusd=12,
        continuation=True,
    )
    ledger.start_attempt(second.attempt_id)
    ledger.finish_attempt(
        second.attempt_id,
        "unknown",
        usage=None,
        cost_microusd=None,
        error_code="unknown",
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    old = (now - timedelta(days=8)).isoformat()
    with database.transaction() as connection:
        connection.execute("UPDATE model_calls SET created_at=?", (old,))
        connection.execute(
            "UPDATE model_attempts SET started_at=? WHERE attempt_id=?",
            (old, first.attempt_id),
        )
    projection = UsageProjection(database)
    week = projection.summary(subject, window="7", now=now)
    assert week["calls"] == "0"
    assert week["attempts"] == "1"
    assert week["retry_attempts"] == "1"
    assert week["unknown_attempts"] == "1"
    assert week["known_cost_microusd"] == "0"
    assert week["estimated_cost_microusd"] == "12"
    all_time = projection.summary(subject, window="all", now=now)
    assert all_time["calls"] == "1"
    assert all_time["attempts"] == "2"
    assert all_time["known_cost_microusd"] == "1"
    assert all_time["cost_microusd"] == "13"
    assert projection.summary(subject, pool="deep", now=now)["attempts"] == "0"
    assert projection.summary("another-subject", now=now)["attempts"] == "0"


def test_empty_usage_reports_retained_coverage_and_utc_budget(tmp_path: Path) -> None:
    projection = UsageProjection(Database(tmp_path / "db"))
    result = projection.summary("absent", window="all")
    assert result["coverage"] == "all_retained_records"
    assert result["earliest_model_record_at"] is None
    assert result["attempt_clock"] == "started_at"
    assert result["budget_timezone"] == "UTC"


class UsageOutput(BaseModel):
    value: str


@pytest.mark.asyncio
@pytest.mark.parametrize("estimated_first", [True, False])
async def test_mixed_retry_usage_is_independent_of_final_call(
    tmp_path: Path, estimated_first: bool
) -> None:
    database = Database(tmp_path / "db")
    IdentityStore(database).ensure("usage", content_hash("usage"))
    provider = FakeProvider(
        [
            ProviderResponse(
                content="invalid", usage=None if estimated_first else ModelUsage(2, 3)
            ),
            ProviderResponse(
                content='{"value":"ok"}', usage=ModelUsage(2, 3) if estimated_first else None
            ),
        ]
    )
    ledger = ModelLedger(database)
    gateway = ModelGateway(
        provider,
        ledger,
        model="test",
        limits=BudgetLimits(100, 100_000, 100_000, 100_000),
        pricing=ModelPricing(1_000_000, 1_000_000),
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0),
    )
    result = await gateway.complete_structured(
        "usage",
        "test",
        [ModelMessage(role="user", content="hi")],
        UsageOutput,
        idempotency_key="mixed",
        max_output_tokens=10,
    )
    projection = UsageProjection(database).summary("usage", window="all")
    assert projection["known_cost_microusd"] == "5"
    assert projection["estimated_cost_attempts"] == "1"
    assert int(projection["cost_microusd"]) == result.cost_microusd
    assert int(projection["estimated_cost_microusd"]) + 5 == result.cost_microusd


def test_price_evidence_survives_free_price_and_priority_edits(tmp_path: Path) -> None:
    database = Database(tmp_path / "db")
    IdentityStore(database).ensure("usage", content_hash("usage"))
    store = CognitiveResourceStore(database, tmp_path / "secrets")
    group = store.configure(
        "usage",
        CognitiveResourceGroupInput(
            pool="economy",
            label="free",
            base_url="https://model.example/v1",
            model="test",
            api_keys=(SecretStr("synthetic"),),
            pricing_status="free",
        ),
        actor="operator",
    )
    ledger = ModelLedger(database)
    call, _ = ledger.prepare_call(
        "usage",
        "fake",
        "test",
        "test",
        content_hash("req"),
        "free",
        resource_group_id=group.group_id,
        resource_pool="economy",
    )
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(100, 10000, 10000, 10000),
        reserved_input_tokens=10,
        reserved_output_tokens=10,
        reserved_cost_microusd=0,
        pricing=ModelPricing(),
    )
    ledger.start_attempt(attempt.attempt_id)
    ledger.finish_attempt(attempt.attempt_id, "succeeded", usage=ModelUsage(2, 3), cost_microusd=0)
    for changes in [
        CognitiveResourceGroupUpdate(priority=7),
        CognitiveResourceGroupUpdate(input_usd_per_million=Decimal("1")),
    ]:
        store.update(group.group_id, changes, subject_id="usage", actor="operator", reason="edit")
        summary = UsageProjection(database).summary("usage", window="all")
        assert summary["unknown_price_attempts"] == "0"
        assert summary["cost_microusd"] == "0"
    with database.connection() as connection:
        evidence = json.loads(
            connection.execute("SELECT pricing_snapshot_json FROM model_attempts").fetchone()[0]
        )
    assert evidence["status"] == "free" and evidence["revision"] == 1
    with (
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
        database.transaction() as connection,
    ):
        connection.execute("UPDATE model_attempts SET pricing_snapshot_json='{}'")
    with (
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
        database.transaction() as connection,
    ):
        connection.execute("UPDATE model_attempts SET usage_source='estimated'")


def test_schema90_usage_migration_keeps_cost_and_does_not_guess_source(tmp_path: Path) -> None:
    path = tmp_path / "db"
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 90):
        old = Database(path)
    IdentityStore(old).ensure("usage", content_hash("usage"))
    with old.transaction() as connection:
        connection.execute(
            "INSERT INTO model_calls(call_id,subject_id,provider,model,purpose,"
            "request_hash,idempotency_key,status,created_at) VALUES"
            "('c','usage','fake','test','test','hash','old','failed','2026-10-10T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO model_attempts(attempt_id,call_id,subject_id,budget_day,attempt_number,"
            "status,reserved_input_tokens,reserved_output_tokens,reserved_cost_microusd,"
            "input_tokens,output_tokens,cost_microusd,started_at,completed_at) VALUES"
            "('a','c','usage','2026-10-10',1,'failed',5,5,10,2,3,7,"
            "'2026-10-10T00:00:00+00:00','2026-10-10T00:00:01+00:00')"
        )
    database = Database(path)
    summary = UsageProjection(database).summary(
        "usage", window="all", now=datetime(2026, 10, 11, tzinfo=UTC)
    )
    assert summary["cost_microusd"] == summary["untraceable_cost_microusd"] == "7"
    assert summary["known_cost_microusd"] == summary["estimated_cost_microusd"] == "0"
    assert summary["untraceable_usage_attempts"] == summary["unknown_price_attempts"] == "1"
    Database(path)


def test_cancelled_reservation_is_not_reported_as_spent(tmp_path: Path) -> None:
    database = Database(tmp_path / "db")
    IdentityStore(database).ensure("usage", content_hash("usage"))
    ledger = ModelLedger(database)
    call, _ = ledger.prepare_call("usage", "fake", "test", "test", content_hash("req"), "cancel")
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(10, 1000, 1000, 1000),
        reserved_input_tokens=10,
        reserved_output_tokens=10,
        reserved_cost_microusd=20,
    )
    ledger.start_attempt(attempt.attempt_id)
    ledger.cancel_unsent_attempt(attempt.attempt_id, error_code="denied")
    summary = UsageProjection(database).summary("usage", window="all")
    assert summary["attempts"] == "1"
    assert summary["cost_microusd"] == summary["estimated_cost_microusd"] == "0"
