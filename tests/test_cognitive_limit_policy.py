from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from itertools import product
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import SecretStr

from noyra.core import IdentityStore
from noyra.core import database as database_module
from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.resource_policy import LIMIT_DIMENSIONS
from noyra.core.types import content_hash
from noyra.model.errors import BudgetExhaustedError, ModelCallStateError
from noyra.model.ledger import ModelLedger
from noyra.model.resources import (
    CognitiveResourceGroupInput,
    CognitiveResourceGroupRecord,
    CognitiveResourceGroupUpdate,
    CognitiveResourceStore,
    RoutedModelGateway,
)
from noyra.model.types import BudgetLimits


def resources(tmp_path: Path) -> tuple[Database, CognitiveResourceStore, str]:
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "limit-policy"
    IdentityStore(database).ensure(subject, content_hash("limits"))
    return database, CognitiveResourceStore(database, tmp_path / "secrets"), subject


def configure(
    store: CognitiveResourceStore, subject: str, **values: object
) -> CognitiveResourceGroupRecord:
    return store.configure(
        subject,
        CognitiveResourceGroupInput.model_validate(
            {
                "pool": "economy",
                "label": "resource",
                "base_url": "https://model.example/v1",
                "model": "test",
                "api_keys": (SecretStr("synthetic-secret"),),
                **values,
            }
        ),
        actor="operator",
    )


def prepare(ledger: ModelLedger, subject: str, group_id: str, key: str) -> str:
    call, _ = ledger.prepare_call(
        subject_id=subject,
        provider="test",
        model="test",
        purpose="test",
        request_hash=content_hash({"text": "synthetic"}),
        request={"text": "synthetic"},
        idempotency_key=key,
        resource_pool="economy",
        resource_group_id=group_id,
    )
    return call.call_id


def test_zero_and_disabled_dimensions_stale_limits_and_revocation(tmp_path: Path) -> None:
    database, store, subject = resources(tmp_path)
    group = configure(store, subject, daily_attempts=0)
    ledger = ModelLedger(database)
    call = prepare(ledger, subject, group.group_id, "first-request")
    stale = RoutedModelGateway._limits_for_group(group)
    with pytest.raises(BudgetExhaustedError):
        ledger.authorize_attempt(
            call,
            stale,
            reserved_input_tokens=10,
            reserved_output_tokens=10,
            reserved_cost_microusd=0,
        )
    enabled = dict.fromkeys(LIMIT_DIMENSIONS, True)
    enabled["attempts"] = False
    group = store.update(
        group.group_id,
        CognitiveResourceGroupUpdate(limit_enabled=enabled),
        subject_id=subject,
        reason="allow attempts",
        actor="operator",
    )
    ledger.authorize_attempt(
        call, stale, reserved_input_tokens=10, reserved_output_tokens=10, reserved_cost_microusd=0
    )
    assert ledger.budget_status(subject, stale, resource_pool="economy").attempts == 1
    store.verify_integrity(subject)
    store.revoke(group.group_id, subject_id=subject, actor="operator", reason="delete")
    next_call = prepare(ledger, subject, group.group_id, "second-request")
    with pytest.raises(ModelCallStateError):
        ledger.authorize_attempt(
            next_call,
            stale,
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=0,
        )
    key = store.keys(group.group_id, subject_id=subject)[0]
    store.record_success(key.key_id, subject_id=subject)
    assert store.keys(group.group_id, subject_id=subject)[0].status == "revoked"
    assert not list((tmp_path / "secrets").glob("*.key"))


def test_two_concurrent_calls_cannot_overrun_one_attempt(tmp_path: Path) -> None:
    database, store, subject = resources(tmp_path)
    group = configure(store, subject, daily_attempts=1)
    ledger = ModelLedger(database)
    calls = [prepare(ledger, subject, group.group_id, f"concurrent-{i}") for i in range(2)]
    barrier = Barrier(2)

    def reserve(call: str) -> bool:
        barrier.wait(timeout=10)
        try:
            ledger.authorize_attempt(
                call,
                BudgetLimits(999, 999, 999, 999),
                reserved_input_tokens=1,
                reserved_output_tokens=1,
                reserved_cost_microusd=0,
            )
        except BudgetExhaustedError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, calls)) == [False, True]


def test_policy_tampering_and_non_boolean_input_are_rejected(tmp_path: Path) -> None:
    database, store, subject = resources(tmp_path)
    group = configure(store, subject)
    with database.transaction() as connection:
        connection.execute("UPDATE cognitive_resource_limit_policies SET attempts_enabled=0")
    with pytest.raises(IntegrityError):
        store.get(group.group_id, subject_id=subject)
    with pytest.raises(ValueError):
        CognitiveResourceGroupInput.validate_limit_switches(dict.fromkeys(LIMIT_DIMENSIONS, 1))


@pytest.mark.parametrize("switches", list(product((False, True), repeat=4)))
def test_all_sixteen_switch_combinations_keep_zero_limits(
    tmp_path: Path, switches: tuple[bool, ...]
) -> None:
    database, store, subject = resources(tmp_path)
    enabled = dict(zip(LIMIT_DIMENSIONS, switches, strict=True))
    group = configure(
        store,
        subject,
        daily_attempts=0,
        daily_input_tokens=0,
        daily_output_tokens=0,
        daily_cost_limit_usd="0",
        limit_enabled=enabled,
    )
    ledger = ModelLedger(database)
    call = prepare(ledger, subject, group.group_id, "switch-combination")
    limits = RoutedModelGateway._limits_for_group(group)
    if any(switches):
        with pytest.raises(BudgetExhaustedError):
            ledger.authorize_attempt(
                call,
                limits,
                reserved_input_tokens=1,
                reserved_output_tokens=1,
                reserved_cost_microusd=1,
            )
    else:
        ledger.authorize_attempt(
            call,
            limits,
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=1,
        )
        assert limits.enabled == switches


def test_revision_conflict_cannot_override_limits(tmp_path: Path) -> None:
    _, store, subject = resources(tmp_path)
    group = configure(store, subject)
    store.update(
        group.group_id,
        CognitiveResourceGroupUpdate(
            daily_attempts=2, expected_limit_revision=group.limit_revision
        ),
        actor="operator",
        reason="first",
        subject_id=subject,
    )
    with pytest.raises(ValueError, match="conflict"):
        store.update(
            group.group_id,
            CognitiveResourceGroupUpdate(
                daily_attempts=500, expected_limit_revision=group.limit_revision
            ),
            actor="operator",
            reason="late",
            subject_id=subject,
        )
    assert store.get(group.group_id, subject_id=subject).daily_attempts == 2


def test_schema80_upgrade_preserves_old_limits_hashes_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, store, subject = resources(tmp_path)
    group = configure(store, subject, daily_attempts=0, daily_input_tokens=1234)
    with database.transaction() as connection:
        old_hash = connection.execute(
            "SELECT state_hash FROM cognitive_resource_groups"
        ).fetchone()[0]
    old_path = tmp_path / "schema80.sqlite3"
    with monkeypatch.context() as context:
        context.setattr(database_module, "CURRENT_SCHEMA_VERSION", 80)
        old_database = Database(old_path)
    with old_database.transaction() as connection:
        connection.execute("ATTACH DATABASE ? AS seed", (str(database.path),))
        for table in (
            "subject_identity", "cognitive_resource_groups", "cognitive_resource_group_revisions",
            "cognitive_resource_keys", "cognitive_resource_key_events",
        ):
            connection.execute(f"INSERT INTO {table} SELECT * FROM seed.{table}")
    upgraded = Database(old_path)
    current = CognitiveResourceStore(upgraded, tmp_path / "secrets").get(
        group.group_id, subject_id=subject
    )
    assert current.daily_attempts == 0
    assert current.daily_input_tokens == 1234
    assert all(current.limit_enabled.values())
    with upgraded.connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute("SELECT state_hash FROM cognitive_resource_groups").fetchone()[0]
            == old_hash
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM cognitive_resource_limit_revisions")


def test_same_name_recreation_keeps_old_group_and_usage(tmp_path: Path) -> None:
    database, store, subject = resources(tmp_path)
    old = configure(store, subject)
    ledger = ModelLedger(database)
    call_id = prepare(ledger, subject, old.group_id, "old-group-request")
    ledger.authorize_attempt(
        call_id, RoutedModelGateway._limits_for_group(old), reserved_input_tokens=1,
        reserved_output_tokens=1, reserved_cost_microusd=0,
    )
    store.revoke(old.group_id, subject_id=subject, actor="operator", reason="replace")
    new = configure(store, subject)
    assert old.group_id != new.group_id
    old_summary = store.budget_summary(store.get(old.group_id, subject_id=subject))
    assert old_summary["today_usage"]["attempts"] == "1"
    assert store.budget_summary(new)["today_usage"]["attempts"] == "0"
    store.verify_integrity(subject)


def test_stale_resource_revision_cannot_authorize_or_weaken_explicit_pool_cap(
    tmp_path: Path,
) -> None:
    database, store, subject = resources(tmp_path)
    group = configure(store, subject)
    ledger = ModelLedger(database)
    call_id = prepare(ledger, subject, group.group_id, "changed-price")
    store.update(
        group.group_id, CognitiveResourceGroupUpdate(input_usd_per_million=Decimal("1")),
        subject_id=subject, actor="operator", reason="new price",
    )
    with pytest.raises(ModelCallStateError, match="changed"):
        ledger.authorize_attempt(
            call_id, RoutedModelGateway._limits_for_group(group),
            reserved_input_tokens=1, reserved_output_tokens=1, reserved_cost_microusd=0,
            expected_resource_revision=group.limit_revision,
        )
    with pytest.raises(BudgetExhaustedError, match="pool"):
        ledger.authorize_attempt(
            call_id, RoutedModelGateway._limits_for_group(group),
            reserved_input_tokens=1, reserved_output_tokens=1, reserved_cost_microusd=0,
            pool_limits=BudgetLimits(0, 100, 100, 100),
        )
    assert store.budget_summary(group)["today_usage"]["attempts"] == "0"


def test_pricing_unknown_and_confirmed_free_are_distinct(tmp_path: Path) -> None:
    _, store, subject = resources(tmp_path)
    group = configure(store, subject)
    assert group.pricing_status == "unknown"
    group = store.update(
        group.group_id, CognitiveResourceGroupUpdate(pricing_status="free"),
        subject_id=subject, actor="operator", reason="supplier free tier",
    )
    assert group.pricing_status == "free"
    with pytest.raises(ValueError, match="zero prices"):
        store.update(
            group.group_id,
            CognitiveResourceGroupUpdate(pricing_status="free", input_usd_per_million=Decimal("1")),
            subject_id=subject, actor="operator", reason="invalid",
        )


def test_management_summary_counts_pool_outside_resource_filters(tmp_path: Path) -> None:
    _, store, subject = resources(tmp_path)
    first = configure(store, subject)
    second = configure(store, subject, label="second")
    configure(store, subject, label="another pool", pool="deep")
    assert store.budget_summary(first)["pool_summary"] == {
        "active_resources": 2, "available_resources": 2
    }
    key = store.keys(second.group_id, subject_id=subject)[0]
    store.revoke_key(key.key_id, subject_id=subject, actor="operator", reason="retire")
    assert store.budget_summary(first)["pool_summary"] == {
        "active_resources": 2, "available_resources": 1
    }
    store.disable(second.group_id, subject_id=subject, actor="operator", reason="stop")
    assert store.budget_summary(first)["pool_summary"] == {
        "active_resources": 1, "available_resources": 1
    }
