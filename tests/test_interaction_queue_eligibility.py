from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from noyra.cognition import InteractionCognition
from noyra.core.types import content_hash
from noyra.interaction import InboundEnvelope, InboundStore, TransportInput, TransportStore
from noyra.interaction.integrity import InteractionIntegrity
from noyra.interaction.rejections import EVENT_TYPE
from noyra.model import BudgetLimits
from noyra.model.errors import ProviderCallError
from test_interaction_rejection_backoff import Case, output
from test_interaction_rejection_backoff import case as case


def run(case: Case, engine: InteractionCognition) -> str | None:
    # Keep decision timestamps on the same controlled clock as eligibility.
    with patch("noyra.interaction.store.utc_now", return_value=case.clock()):
        return asyncio.run(engine.run_due())


def restart(case: Case, engine: InteractionCognition) -> InteractionCognition:
    return InteractionCognition(
        case.kernel.database,
        case.kernel.subject_id,
        engine.gateway,
        engine.settings,
        clock=case.clock,
    )


def due(engine: InteractionCognition) -> str | None:
    selected = engine._due_interaction()
    return selected.interaction_id if selected is not None else None


def state(case: Case) -> dict[str, list[tuple[Any, ...]]]:
    with case.kernel.database.read_transaction() as connection:
        return {
            table: [
                tuple(row)
                for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")
            ]
            for table in (
                "interactions",
                "interaction_decisions",
                "model_calls",
                "model_attempts",
                "events",
            )
        }


@pytest.mark.parametrize("kind", ["target", "schema"])
@pytest.mark.parametrize("restarted", [False, True])
def test_multiple_backoff_heads_yield_one_decision_without_rewriting_skipped_evidence(
    case: Case, kind: str, restarted: bool
) -> None:
    blocked = [case.receive(f"blocked-{index}") for index in range(6)]
    ready = [case.receive(f"ready-{index}") for index in range(2)]
    engine, provider = case.engine([output(kind)] * 6 + [output("valid")] * 2)
    for _ in blocked:
        assert run(case, engine) in {"interaction_proposal_rejected", "interaction_model_failed"}
    if restarted:
        engine = restart(case, engine)
    before = state(case)
    for _ in range(3):
        selected = engine._due_interaction()
        assert selected is not None and selected.interaction_id == ready[0]
    assert state(case) == before  # Selection itself neither claims nor reschedules rows.
    assert run(case, engine) == "interaction_silent"
    after = state(case)
    for table in ("model_calls", "model_attempts", "events"):
        assert after[table][: len(before[table])] == before[table], table
    assert len(after["interaction_decisions"]) == 1
    assert all(engine.interactions.get(key).status == "offered" for key in blocked + ready[1:])
    assert engine.interactions.get(ready[0]).status == "silent"
    assert len(provider.requests) == 7
    selected = engine._due_interaction()
    assert selected is not None and selected.interaction_id == ready[1]
    assert (
        InteractionIntegrity(case.kernel.database).verify(case.kernel.subject_id)[
            "interaction_proposal_rejections"
        ]
        == 6
    )


@pytest.mark.parametrize("kind", ["target", "schema"])
def test_skipping_priority_head_preserves_priority_time_id_order_and_expiry(
    case: Case, tmp_path: Path, kind: str
) -> None:
    low = case.receive("oldest-lower-priority")
    transport = TransportStore(case.kernel.database, tmp_path / "transport-secrets").configure(
        case.kernel.subject_id,
        TransportInput(
            channel="webhook",
            label="Synthetic queue priority",
            endpoint="https://example.invalid/inbound",
            credentials={"webhook_secret": SecretStr("synthetic-queue-secret")},
        ),
        actor="operator",
    )
    inbound = InboundStore(case.kernel.database)
    inbound.bind(
        case.kernel.subject_id,
        transport.transport_id,
        external_account_id="synthetic-account",
        external_sender_id="synthetic-creator",
        role="creator",
    )

    def receive(key: str) -> str:
        with patch("noyra.interaction.store.utc_now", return_value=case.clock()):
            item = inbound.ingest(
                InboundEnvelope(
                    channel="webhook",
                    transport_id=transport.transport_id,
                    provider_event_id=key,
                    external_account_id="synthetic-account",
                    external_sender_id="synthetic-creator",
                    conversation_id="synthetic-conversation",
                    content="Synthetic priority invitation " + key,
                    received_at=case.clock(),
                )
            )
        assert item.scheduling_priority == 100
        assert item.interaction_id is not None
        return item.interaction_id

    head = receive("head")
    case.now += timedelta(milliseconds=1)
    tied = sorted([receive("tie-a"), receive("tie-b")])
    engine, provider = case.engine([output(kind)] + [output("valid")] * 2)
    assert due(engine) == head
    assert run(case, engine) in {"interaction_proposal_rejected", "interaction_model_failed"}
    engine = restart(case, engine)
    assert due(engine) == tied[0]
    assert run(case, engine) == "interaction_silent"
    assert engine.interactions.get(low).status == "offered"
    assert engine.interactions.get(tied[1]).status == "offered"
    # Backoff is a temporary eligibility fence, never a priority demotion.
    case.now += timedelta(seconds=60)
    assert due(engine) == head
    assert run(case, engine) == "interaction_silent"
    assert due(engine) == tied[1]
    assert len(provider.requests) == 3


@pytest.mark.parametrize("gate", ["decision-cooldown", "daily-cap"])
def test_scanning_past_backoff_does_not_bypass_global_limits(case: Case, gate: str) -> None:
    case.receive("rejected-head")
    case.receive("decided")
    tail = case.receive("waiting-tail")
    engine, provider = case.engine([output("target"), output("valid"), output("valid")])
    assert run(case, engine) == "interaction_proposal_rejected"
    assert run(case, engine) == "interaction_silent"
    update = (
        {"interaction_cooldown_seconds": 120}
        if gate == "decision-cooldown"
        else {"max_interaction_model_calls_per_day": 2}
    )
    engine.settings = engine.settings.model_copy(update=update)
    before = state(case)
    for _ in range(3):
        assert run(case, engine) is None
    engine = restart(case, engine)
    assert run(case, engine) is None
    assert state(case) == before
    assert engine.interactions.get(tail).status == "offered"
    assert len(provider.requests) == 2


@pytest.mark.parametrize("fence", ["unknown", "executing", "prepared"])
def test_model_fence_and_rejection_backoff_both_yield_without_mutating_reserved_calls(
    case: Case, fence: str
) -> None:
    first = case.receive("fenced-head")
    second = case.receive("rejected-middle")
    tail = case.receive("ready-tail")
    outcomes: list[str | ProviderCallError] = [output("target"), output("valid")]
    if fence == "unknown":
        outcomes.insert(
            0, ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True)
        )
    engine, provider = case.engine(outcomes, routed=fence != "unknown")
    if fence == "unknown":
        assert run(case, engine) == "interaction_model_failed"
    else:
        call, _ = engine.gateway.ledger.prepare_call(
            case.kernel.subject_id,
            "fake",
            "test-model",
            f"interaction_cognition:{first}:0",
            content_hash("synthetic fenced request"),
            "synthetic-queue-fence",
            resource_pool="economy",
        )
        if fence == "executing":
            attempt = engine.gateway.ledger.authorize_attempt(
                call.call_id,
                BudgetLimits(20, 500000, 100000, 5000000),
                reserved_input_tokens=100,
                reserved_output_tokens=100,
                reserved_cost_microusd=0,
            )
            engine.gateway.ledger.start_attempt(attempt.attempt_id)
    assert run(case, engine) == "interaction_proposal_rejected"
    with case.kernel.database.read_transaction() as connection:
        calls = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM model_calls WHERE purpose=?", (f"interaction_cognition:{first}:0",)
            )
        ]
        attempts = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM model_attempts WHERE call_id IN "
                "(SELECT call_id FROM model_calls WHERE purpose=?)",
                (f"interaction_cognition:{first}:0",),
            )
        ]
    engine = restart(case, engine)
    assert due(engine) == tail
    assert run(case, engine) == "interaction_silent"
    assert (
        engine.interactions.get(first).status == engine.interactions.get(second).status == "offered"
    )
    with case.kernel.database.read_transaction() as connection:
        assert calls == [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM model_calls WHERE purpose=?", (f"interaction_cognition:{first}:0",)
            )
        ]
        assert attempts == [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM model_attempts WHERE call_id IN "
                "(SELECT call_id FROM model_calls WHERE purpose=?)",
                (f"interaction_cognition:{first}:0",),
            )
        ]
    assert len(provider.requests) == (3 if fence == "unknown" else 2)


@pytest.mark.parametrize("routed", [False, True])
def test_known_provider_rejection_yields_to_an_independent_invitation(
    case: Case, routed: bool
) -> None:
    head = case.receive("provider-rejected-head")
    tail = case.receive("independent-tail")
    engine, provider = case.engine(
        [
            ProviderCallError("provider_http_400", retryable=False, outcome_unknown=False),
            output("valid"),
        ],
        routed=routed,
    )
    assert run(case, engine) == "interaction_model_failed"
    if routed:
        from test_audit_20261022_remediation import resources_for

        resources_for(
            case.kernel.database,
            case.kernel.subject_id,
            case.kernel.database.path.parent,
            label="Independent healthy queue route",
        )
    engine = restart(case, engine)
    assert due(engine) == tail
    assert run(case, engine) == "interaction_silent"
    assert engine.interactions.get(head).status == "offered"
    assert engine.interactions.get(tail).status == "silent"
    assert len(provider.requests) == 2


@pytest.mark.parametrize("interval", [0, 120])
@pytest.mark.parametrize("code", ["provider_http_400", "provider_http_429", "provider_auth_failed"])
def test_known_provider_backoff_uses_settlement_time_across_restart_and_expires_exactly(
    case: Case, interval: int, code: str
) -> None:
    head = case.receive("provider-rejected")
    engine, provider = case.engine(
        [ProviderCallError(code, retryable=False, outcome_unknown=False), output("valid")]
    )
    engine.settings = engine.settings.model_copy(update={"interaction_cooldown_seconds": interval})
    assert run(case, engine) == "interaction_model_failed"
    before = state(case)
    with case.kernel.database.read_transaction() as connection:
        call = dict(connection.execute("SELECT * FROM model_calls").fetchone())
        assert call["status"] == "failed" and call["error_code"] == code
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type=?", (EVENT_TYPE,)
            ).fetchone()[0]
            == 0
        )  # Provider rejection is not a rejected model proposal.
    retry_at = datetime.fromisoformat(call["completed_at"]) + timedelta(seconds=max(60, interval))
    case.now = retry_at - timedelta(milliseconds=1)
    for _ in range(3):
        assert run(case, engine) is None
    engine = restart(case, engine)
    assert run(case, engine) is None
    case.now = case.now.astimezone(timezone(timedelta(hours=-7)))
    assert run(case, engine) is None
    assert state(case) == before
    assert len(provider.requests) == 1
    case.now = retry_at
    assert due(engine) == head
    assert run(case, engine) == "interaction_silent"
    assert len(provider.requests) == 2
    assert len(state(case)["interaction_decisions"]) == 1


def test_known_provider_backoff_survives_utc_midnight_without_reset(case: Case) -> None:
    case.now = datetime(2026, 11, 15, 23, 59, 30, tzinfo=UTC)
    stamp = case.clock()
    head = case.receive("midnight-provider-rejection")
    engine, provider = case.engine(
        [
            ProviderCallError("provider_http_400", retryable=False, outcome_unknown=False),
            output("valid"),
        ]
    )
    with patch("noyra.model.ledger.utc_now", return_value=stamp):
        assert run(case, engine) == "interaction_model_failed"
    before = state(case)
    case.now = datetime(2026, 11, 16, 0, 0, 0, tzinfo=UTC)
    engine = restart(case, engine)
    assert run(case, engine) is None
    assert state(case) == before
    case.now += timedelta(seconds=30)
    assert due(engine) == head
    assert run(case, engine) == "interaction_silent"
    assert len(provider.requests) == 2


def test_explicit_unknown_retry_closure_is_not_delayed_by_provider_backoff(case: Case) -> None:
    head = case.receive("unknown-head")
    tail = case.receive("ready-tail")
    engine, provider = case.engine(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            ProviderCallError("provider_http_400", retryable=False, outcome_unknown=False),
            output("valid"),
        ]
    )
    assert run(case, engine) == "interaction_model_failed"
    with case.kernel.database.read_transaction() as connection:
        call_id = connection.execute("SELECT call_id FROM model_calls").fetchone()[0]
    engine.gateway.ledger.prepare_unknown_retry(
        call_id, actor="operator", reason="Synthetic explicit retry"
    )
    assert due(engine) == head
    assert run(case, engine) == "interaction_model_failed"
    assert due(engine) == tail
    assert engine.gateway.ledger.close_failed_unknown_retry(
        call_id,
        actor="operator",
        reason="Synthetic closure",
        resolution_evidence="synthetic-provider-record",
        accept_duplicate_risk=True,
    )
    assert due(restart(case, engine)) == head
    assert run(case, engine) == "interaction_silent"
    assert len(provider.requests) == 3
