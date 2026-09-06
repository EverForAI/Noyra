from __future__ import annotations

import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.types import content_hash
from noyra.model import BudgetLimits, ModelLedger
from noyra.model.errors import BudgetExhaustedError, ModelCallStateError


def test_cross_group_attempts_reserve_one_aggregate_pool_slot() -> None:
    """Group checks and the pool check must share one writer transaction."""

    with tempfile.TemporaryDirectory() as temp_dir:
        database = Database(Path(temp_dir) / "noyra.sqlite3")
        subject_id = "aggregate-budget-subject"
        IdentityStore(database).ensure(subject_id, content_hash({"seed": "aggregate-budget"}))
        ledger = ModelLedger(database)
        group_limits = BudgetLimits(
            daily_attempts=10,
            daily_input_tokens=10_000,
            daily_output_tokens=10_000,
            daily_cost_microusd=1_000_000,
        )
        pool_limits = BudgetLimits(
            daily_attempts=1,
            daily_input_tokens=10_000,
            daily_output_tokens=10_000,
            daily_cost_microusd=1_000_000,
        )
        calls = [
            ledger.prepare_call(
                subject_id,
                "fake",
                "model",
                f"group-{group}",
                f"request-{group}",
                f"idempotency-{group}",
                resource_pool="deep",
                resource_group_id=f"group-{group}",
            )[0]
            for group in ("a", "b")
        ]
        ready = threading.Barrier(2)

        def authorize(call_index: int) -> str:
            ready.wait()
            call = calls[call_index]
            try:
                ledger.authorize_attempt(
                    call.call_id,
                    group_limits,
                    reserved_input_tokens=100,
                    reserved_output_tokens=100,
                    reserved_cost_microusd=100,
                    resource_group_id=call.resource_group_id,
                    pool_limits=pool_limits,
                )
            except BudgetExhaustedError as error:
                assert str(error) == "daily model pool budget exhausted"
                return "blocked"
            return "authorized"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(authorize, (0, 1)))

        assert sorted(outcomes) == ["authorized", "blocked"]
        assert (
            ledger.budget_status(
                subject_id,
                pool_limits,
                resource_pool="deep",
            ).attempts
            == 1
        )
        assert (
            sum(
                ledger.budget_status(
                    subject_id,
                    group_limits,
                    resource_pool="deep",
                    resource_group_id=f"group-{group}",
                ).attempts
                for group in ("a", "b")
            )
            == 1
        )


def test_same_physical_call_has_one_first_attempt_owner() -> None:
    """Concurrent idempotent callers cannot both invoke the provider."""

    with tempfile.TemporaryDirectory() as temp_dir:
        database = Database(Path(temp_dir) / "noyra.sqlite3")
        subject_id = "physical-call-owner-subject"
        IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
        ledger = ModelLedger(database)
        limits = BudgetLimits(10, 10_000, 10_000, 1_000_000)
        call = ledger.prepare_call(
            subject_id,
            "fake",
            "model",
            "idempotent owner",
            "request-hash",
            "one-physical-call",
        )[0]
        ready = threading.Barrier(2)

        def authorize(_: int) -> str:
            ready.wait()
            try:
                ledger.authorize_attempt(
                    call.call_id,
                    limits,
                    reserved_input_tokens=1,
                    reserved_output_tokens=1,
                    reserved_cost_microusd=1,
                )
            except ModelCallStateError:
                return "blocked"
            return "authorized"

        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(authorize, (0, 1))) == ["authorized", "blocked"]
        attempts = ledger.attempts(call.call_id)
        assert len(attempts) == 1
        ledger.start_attempt(attempts[0].attempt_id)
        ledger.finish_attempt(
            attempts[0].attempt_id,
            "failed",
            usage=None,
            cost_microusd=None,
            error_code="known_failure",
        )
        continued = ledger.authorize_attempt(
            call.call_id,
            limits,
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=1,
            continuation=True,
        )
        assert continued.attempt_number == 2


def test_attempt_cannot_charge_a_different_group_projection() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        database = Database(Path(temp_dir) / "noyra.sqlite3")
        subject_id = "group-binding-subject"
        IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
        ledger = ModelLedger(database)
        call = ledger.prepare_call(
            subject_id,
            "fake",
            "model",
            "group binding",
            "request-hash",
            "group-bound-call",
            resource_pool="deep",
            resource_group_id="durable-group",
        )[0]
        with pytest.raises(ModelCallStateError, match="does not match"):
            ledger.authorize_attempt(
                call.call_id,
                BudgetLimits(10, 10_000, 10_000, 1_000_000),
                reserved_input_tokens=1,
                reserved_output_tokens=1,
                reserved_cost_microusd=1,
                resource_group_id="different-group",
            )
        assert ledger.attempts(call.call_id) == []
