from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.types import content_hash
from noyra.core.usage_projection import UsageProjection, usage_window
from noyra.model.ledger import ModelLedger
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
        subject, "fake", "test", "test", content_hash("req"), "request-key",
        resource_pool="economy",
    )
    limits = BudgetLimits(100, 100_000, 100_000, 100_000)
    first = ledger.authorize_attempt(
        call.call_id, limits, reserved_input_tokens=100, reserved_output_tokens=100,
        reserved_cost_microusd=10,
    )
    ledger.start_attempt(first.attempt_id)
    ledger.finish_attempt(
        first.attempt_id, "failed", usage=ModelUsage(2, 3), cost_microusd=1,
        error_code="timeout",
    )
    second = ledger.authorize_attempt(
        call.call_id, limits, reserved_input_tokens=100, reserved_output_tokens=100,
        reserved_cost_microusd=12, continuation=True,
    )
    ledger.start_attempt(second.attempt_id)
    ledger.finish_attempt(
        second.attempt_id, "unknown", usage=None, cost_microusd=None, error_code="unknown",
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    old = (now - timedelta(days=8)).isoformat()
    with database.transaction() as connection:
        connection.execute("UPDATE model_calls SET created_at=?", (old,))
        connection.execute(
            "UPDATE model_attempts SET started_at=? WHERE attempt_id=?", (old, first.attempt_id),
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
