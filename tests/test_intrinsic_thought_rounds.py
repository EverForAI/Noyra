from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from pydantic import SecretStr

import test_intrinsic_thought as base
import test_intrinsic_thought_recurrence as r
from noyra.cognition import IntrinsicThought
from noyra.cognition.thought_rounds import ThoughtRounds
from noyra.core import Database, IdentityStore, IntegrityRegistry
from noyra.core.database import CURRENT_SCHEMA_VERSION, MIGRATIONS
from noyra.core.errors import IntegrityError, SchemaContractError
from noyra.core.integrity import IntegrityAuditLimits
from noyra.core.retention import RETENTION_REGISTRY
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.types import canonical_json, content_hash
from noyra.model import CognitiveResourceGroupInput, CognitiveResourceStore, FakeProvider
from noyra.model.errors import ProviderCallError
from noyra.model.ledger import ModelLedger
from noyra.model.request_identity import is_business_round
from noyra.model.resources import RoutedModelGateway
from noyra.model.types import BudgetLimits, ModelUsage, ProviderResponse, RetryPolicy


@pytest.fixture
def fixture() -> Iterator[base.IntrinsicThoughtTestCase]:
    case = base.IntrinsicThoughtTestCase()
    case.setUp()
    try:
        with patch("noyra.model.ledger.utc_now", side_effect=lambda: case.clock_value):
            yield case
    finally:
        case.tearDown()


@pytest.mark.asyncio
async def test_known_failure_advances_only_after_durable_cooldown(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()])
    provider = thought.gateway.provider
    assert isinstance(provider, FakeProvider)
    failure = FakeProvider(
        [ProviderCallError("provider_http_401", retryable=False, outcome_unknown=False)]
    )
    thought.gateway.provider = failure
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    agenda = r.absence(thought)
    assert thought._episode_count(agenda.agenda_id) == 0
    assert await thought.run_due() is None
    r.advance(fixture, 65)
    thought.gateway.provider = provider
    assert await thought.run_due() == "intrinsic_thought_reflect"
    assert len(failure.requests) == len(provider.requests) == 1
    assert thought._episode_count(agenda.agenda_id) == 1
    with thought.database.connection() as connection:
        keys = [
            row[0] for row in connection.execute("SELECT purpose FROM model_calls ORDER BY rowid")
        ]
    assert keys == [
        f"intrinsic_thought:{agenda.agenda_id}:0",
        f"intrinsic_thought:{agenda.agenda_id}:1",
    ]


@pytest.mark.asyncio
async def test_success_without_episode_recovers_at_daily_cap_without_new_call(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()], max_thought_model_calls_per_day=1)
    with (
        patch.object(thought, "_commit", side_effect=RuntimeError("synthetic crash")),
        pytest.raises(RuntimeError, match="synthetic crash"),
    ):
        await thought.run_due()
    assert thought._calls_today() == 1
    assert thought.latest() is None
    assert await thought.run_due() == "intrinsic_thought_reflect"
    assert isinstance(thought.gateway.provider, FakeProvider)
    assert len(thought.gateway.provider.requests) == 1
    assert thought.verify_integrity()["thought_episodes"] == 1


def closures(thought: IntrinsicThought) -> list[dict[str, Any]]:
    with thought.database.connection() as c:
        return [
            dict(row)
            for row in c.execute(
                "SELECT * FROM thought_round_closures WHERE subject_id=? ORDER BY round_number",
                (thought.subject_id,),
            )
        ]


def snapshot_calls(thought: IntrinsicThought) -> dict[str, list[tuple[Any, ...]]]:
    with thought.database.connection() as c:
        return {
            table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("model_calls", "model_attempts")
        }


def seed_call(
    thought: IntrinsicThought, status: str, *, number: int = 0, route: str = "direct"
) -> str:
    thought.discover_agenda()
    agenda = r.absence(thought)
    purpose, key = thought.rounds.identity(agenda.agenda_id, number)
    if route == "v2":
        key = RoutedModelGateway._route_prefix(key, "deep") + "synthetic-group:key:synthetic-key"
    elif route == "v1":
        key += ":pool:deep:group:synthetic-group:key:synthetic-key"
    ledger = ModelLedger(thought.database)
    call, _ = ledger.prepare_call(
        thought.subject_id, "fake", "synthetic", purpose, content_hash("synthetic request"), key
    )
    if status == "failed":
        ledger.finish_call(call.call_id, "failed", error_code="provider_http_401")
    elif status != "prepared":
        attempt = ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(30, 100000, 100000, 1000000),
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=0,
        )
        ledger.start_attempt(attempt.attempt_id)
        if status == "unknown":
            ledger.finish_attempt(
                attempt.attempt_id,
                "unknown",
                usage=None,
                cost_microusd=None,
                error_code="synthetic_unknown",
            )
            ledger.finish_call(call.call_id, "unknown", error_code="synthetic_unknown")
    return call.call_id


@pytest.mark.asyncio
async def test_failure_receipt_survives_restart_and_does_not_reset_daily_accounting(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture)
    seed_call(thought, "failed")
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    original = closures(thought)
    facts = snapshot_calls(thought)
    assert len(original) == 1 and thought._calls_today() == 1
    restarted = r.thought_for(fixture)
    assert await restarted.run_due() is None
    assert closures(restarted) == original
    assert snapshot_calls(restarted) == facts
    assert restarted._calls_today() == 1
    assert restarted.verify_integrity()["thought_round_closures"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["invalid_json", "invalid_evidence", "duplicate_result"])
async def test_known_rejections_do_not_permanently_block_the_agenda(
    fixture: base.IntrinsicThoughtTestCase,
    failure_kind: str,
) -> None:
    invalid = r.proposal()
    if failure_kind == "invalid_evidence":
        invalid["source_goal_ids"] = ["unavailable-goal"]
    thought = r.thought_for(fixture, [invalid, r.proposal(insight="A distinct and valid framing.")])
    thought.gateway.retry_policy = RetryPolicy(max_attempts=1)
    if failure_kind == "invalid_json":
        provider = thought.gateway.provider
        assert isinstance(provider, FakeProvider)
        provider._outcomes.appendleft(ProviderResponse("not JSON", ModelUsage(3, 2)))
    if failure_kind == "duplicate_result":
        first = thought.gateway.provider
        assert isinstance(first, FakeProvider)
        first._outcomes.appendleft(first._outcomes[0])
        assert await thought.run_due() == "intrinsic_thought_reflect"
        r.advance(fixture)
    result = await thought.run_due()
    assert result == (
        "intrinsic_thought_model_failed"
        if failure_kind == "invalid_json"
        else "intrinsic_thought_rejected"
    )
    before = closures(thought)
    assert len(before) == 1
    assert await thought.run_due() is None
    # Remove the extra invalid-evidence response in the invalid-JSON fixture;
    # the real paid failure must never be relabeled as a successful episode.
    if failure_kind == "invalid_json":
        assert isinstance(provider, FakeProvider)
        provider._outcomes.popleft()
    r.advance(fixture, 65)
    assert await thought.run_due() == "intrinsic_thought_reflect"
    assert closures(thought) == before
    assert thought.verify_integrity()["thought_episodes"] == (
        2 if failure_kind == "duplicate_result" else 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["direct", "v1", "v2"])
@pytest.mark.parametrize("status", ["prepared", "executing", "unknown"])
async def test_unresolved_physical_calls_never_advance_or_reissue(
    fixture: base.IntrinsicThoughtTestCase,
    status: str,
    route: str,
) -> None:
    thought = r.thought_for(fixture)
    seed_call(thought, status, route=route)
    before = snapshot_calls(thought)
    for _ in range(2):
        restarted = r.thought_for(fixture)
        assert await restarted.run_due() is None
        r.advance(fixture, 24 * 60)
    assert closures(thought) == []
    assert snapshot_calls(thought) == before
    assert thought.rounds.current(r.absence(thought).agenda_id)[0] == 0


@pytest.mark.asyncio
async def test_unclosed_operator_retry_authorization_remains_quarantined(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture)
    call_id = seed_call(thought, "failed")
    with thought.database.transaction() as c:
        c.execute(
            "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
            (
                "synthetic-authorization",
                thought.subject_id,
                "model_unknown_retry_authorized",
                "synthetic-operator",
                canonical_json({"call_id": call_id}),
                fixture.clock_value,
            ),
        )
    assert await thought.run_due() is None
    assert closures(thought) == []
    r.advance(fixture, 24 * 60)
    assert await thought.run_due() is None
    assert closures(thought) == []


@pytest.mark.asyncio
async def test_failed_call_with_unsettled_attempt_is_not_treated_as_retryable(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture)
    call_id = seed_call(thought, "executing")
    ModelLedger(thought.database).finish_call(
        call_id, "failed", error_code="synthetic_inconsistent"
    )
    assert await thought.run_due() is None
    assert closures(thought) == []


@pytest.mark.asyncio
async def test_retry_budget_and_new_day_do_not_bypass_persisted_cooldown(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    fixture.clock_value = "2026-08-13T23:55:00.000+00:00"
    thought = r.thought_for(fixture, [r.proposal()], max_thought_model_calls_per_day=1)
    seed_call(thought, "failed")
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    r.advance(fixture, 10)
    assert thought._calls_today() == 0
    assert await thought.run_due() is None
    r.advance(fixture, 55)
    assert await thought.run_due() == "intrinsic_thought_reflect"
    assert thought._calls_today() == 1


@pytest.mark.asyncio
async def test_failure_uses_no_extra_request_after_today_cap_even_when_cooldown_ends(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()], max_thought_model_calls_per_day=1)
    seed_call(thought, "failed")
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    r.advance(fixture, 65)
    assert await thought.run_due() is None
    assert isinstance(thought.gateway.provider, FakeProvider)
    assert thought.gateway.provider.requests == []
    assert len(closures(thought)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["unknown", "cooldown"])
async def test_fenced_high_score_agenda_does_not_starve_other_eligible_work(
    fixture: base.IntrinsicThoughtTestCase,
    fence: str,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()])
    seed_call(thought, "unknown" if fence == "unknown" else "failed")
    if fence == "cooldown":
        assert await thought.run_due() == "intrinsic_thought_model_failed"
    candidates = thought._agenda_candidates()
    candidates.append(
        {
            "source_type": "question",
            "source_id": "second-question",
            "topic": "A different optional question",
            "urgency": 0.1,
            "novelty": 0.1,
            "emotional_weight": 0.01,
        }
    )
    with patch.object(thought, "_agenda_candidates", return_value=candidates):
        thought._refresh_agenda()
    selected = thought._select_agenda()
    assert selected is not None and selected.source_id == "second-question"
    assert await thought.run_due() == "intrinsic_thought_reflect"
    assert thought._episode_count(r.absence(thought).agenda_id) == 0


@pytest.mark.asyncio
async def test_concurrent_workers_and_cancellation_do_not_duplicate_requests(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()])
    entered, release = asyncio.Event(), asyncio.Event()
    original = thought.gateway.provider
    assert isinstance(original, FakeProvider)
    complete = original.complete

    async def blocking(request: Any) -> ProviderResponse:
        entered.set()
        await release.wait()
        return await complete(request)

    other = r.thought_for(fixture)
    with patch.object(original, "complete", side_effect=blocking):
        task = asyncio.create_task(thought.run_due())
        await asyncio.wait_for(entered.wait(), 5)
        try:
            assert await other.run_due() == "intrinsic_thought_in_progress"
            assert await thought.run_due() == "intrinsic_thought_in_progress"
        finally:
            release.set()
            assert await task == "intrinsic_thought_reflect"
    assert len(original.requests) == 1
    assert thought.verify_integrity()["thought_episodes"] == 1


@pytest.mark.asyncio
async def test_cancellation_marks_unknown_and_releases_only_worker_lock(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture)
    entered = asyncio.Event()

    async def blocking(request: Any) -> ProviderResponse:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with patch.object(thought.gateway.provider, "complete", side_effect=blocking) as calls:
        task = asyncio.create_task(thought.run_due())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        r.advance(fixture, 24 * 60)
        assert await thought.run_due() is None
        assert calls.call_count == 1
    assert not thought._processing_lock.held
    assert closures(thought) == []
    assert snapshot_calls(thought)["model_calls"][0][7] == "unknown"


@pytest.mark.asyncio
async def test_failure_closure_write_failure_is_recovered_without_a_second_provider_call(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture)
    provider = FakeProvider(
        [ProviderCallError("provider_http_401", retryable=False, outcome_unknown=False)]
    )
    thought.gateway.provider = provider
    with (
        patch.object(thought.rounds, "close", side_effect=RuntimeError("synthetic closure crash")),
        pytest.raises(RuntimeError, match="synthetic closure crash"),
    ):
        await thought.run_due()
    assert closures(thought) == []
    assert not thought._processing_lock.held
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    assert len(provider.requests) == 1
    assert len(closures(thought)) == 1


@pytest.mark.asyncio
async def test_corrupted_success_response_is_not_reissued_or_reclassified(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()])
    with (
        patch.object(thought, "_commit", side_effect=RuntimeError("synthetic commit crash")),
        pytest.raises(RuntimeError, match="synthetic commit crash"),
    ):
        await thought.run_due()
    with thought.database.transaction() as c:
        c.execute("UPDATE model_calls SET response_hash=?", ("0" * 64,))
    with pytest.raises(IntegrityError):
        await thought.run_due()
    assert closures(thought) == []
    assert isinstance(thought.gateway.provider, FakeProvider)
    assert len(thought.gateway.provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["hash", "binding", "evidence", "cooldown"])
async def test_closure_corruption_fails_closed_in_runtime_and_integrity(
    fixture: base.IntrinsicThoughtTestCase,
    damage: str,
) -> None:
    thought = r.thought_for(fixture)
    seed_call(thought, "failed")
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    payload = closures(thought)[0]
    if damage == "binding":
        IdentityStore(thought.database).ensure("foreign-round-subject", content_hash("foreign"))
    with thought.database.transaction() as c:
        c.execute("DROP TRIGGER prevent_thought_round_closure_update")
        if damage == "hash":
            c.execute("UPDATE thought_round_closures SET state_hash=?", ("0" * 64,))
        else:
            payload.pop("state_hash")
            if damage == "binding":
                payload["subject_id"] = "foreign-round-subject"
            elif damage == "evidence":
                payload["call_evidence_json"] = '[{"call_id":"forged"}]'
            else:
                payload["retry_after"] = payload["closed_at"]
            c.execute(
                "UPDATE thought_round_closures SET "
                + ",".join(f"{key}=?" for key in payload)
                + ",state_hash=?",
                (*payload.values(), content_hash(payload)),
            )
    r.advance(fixture, 65)
    with pytest.raises(IntegrityError):
        await thought.run_due()
    with pytest.raises(IntegrityError):
        thought.verify_integrity()


@pytest.mark.asyncio
async def test_closures_are_append_only_and_do_not_capture_prompt_text(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture)
    seed_call(thought, "failed")
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    payload = closures(thought)[0]
    assert set(payload) == {
        "agenda_id",
        "subject_id",
        "round_number",
        "reason",
        "call_evidence_json",
        "closed_at",
        "retry_after",
        "state_hash",
    }
    evidence = json.loads(payload["call_evidence_json"])
    assert set(evidence[0]) == {"call_id", "status", "request_hash", "response_hash"}
    for sql in (
        "UPDATE thought_round_closures SET reason='proposal_rejected'",
        "DELETE FROM thought_round_closures",
    ):
        with (
            pytest.raises(sqlite3.IntegrityError, match="append-only"),
            thought.database.transaction() as c,
        ):
            c.execute(sql)
    spec = next(item for item in RETENTION_REGISTRY if item.table == "thought_round_closures")
    assert spec.retention_action == "preserve"
    report = IntegrityRegistry().run(
        thought.database,
        thought.subject_id,
        thought.database.path.parent,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=15,
        check_ids=("cognition.thought",),
    )
    assert report.status == "ok" and report.checks[0].version == 2
    assert report.checks[0].details["thought_round_closures"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["known_failure", "unknown"])
async def test_real_router_does_not_escape_ambiguity_by_changing_resource_or_round(
    fixture: base.IntrinsicThoughtTestCase,
    outcome: str,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()])
    successful = thought.gateway.provider
    assert isinstance(successful, FakeProvider)
    response = successful._outcomes[0]
    provider = FakeProvider(
        [
            ProviderCallError(
                "provider_http_401" if outcome == "known_failure" else "provider_outcome_unknown",
                retryable=False,
                outcome_unknown=outcome == "unknown",
            ),
            response,
        ]
    )
    resources = CognitiveResourceStore(
        thought.database, thought.database.path.parent / "synthetic-secrets"
    )

    def configure(label: str) -> None:
        resources.configure(
            thought.subject_id,
            CognitiveResourceGroupInput(
                pool="deep",
                label=label,
                base_url="https://models.example/v1",
                model="synthetic-thought",
                api_keys=(SecretStr(f"synthetic-key-{label}"),),
                max_attempts=1,
            ),
            actor="operator",
        )

    configure("first")
    thought.gateway = RoutedModelGateway(
        thought.database,
        thought.subject_id,
        resources,
        provider_factory=lambda _: provider,
        max_group_failovers=1,
    )
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    original = snapshot_calls(thought)
    with thought.database.connection() as c:
        waits = c.execute("SELECT status FROM waiting_cognitive_tasks").fetchall()
    assert len(waits) == 1
    assert waits[0][0] == ("waiting" if outcome == "unknown" else "cancelled")
    resources.verify_integrity(thought.subject_id)
    configure("second")
    r.advance(fixture, 24 * 60)
    if outcome == "unknown":
        assert await thought.run_due() is None
        assert snapshot_calls(thought) == original
        assert closures(thought) == []
        assert len(provider.requests) == 1
    else:
        assert len(closures(thought)) == 1
        assert await thought.run_due() == "intrinsic_thought_reflect"
        assert len(provider.requests) == 2
        assert thought.verify_integrity()["thought_episodes"] == 1


@pytest.mark.asyncio
async def test_real_router_success_is_recovered_without_redispatch_at_daily_cap(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()], max_thought_model_calls_per_day=1)
    provider = thought.gateway.provider
    resources = CognitiveResourceStore(
        thought.database, thought.database.path.parent / "synthetic-secrets"
    )
    resources.configure(
        thought.subject_id,
        CognitiveResourceGroupInput(
            pool="deep",
            label="recovery",
            base_url="https://models.example/v1",
            model="synthetic-thought",
            api_keys=(SecretStr("synthetic-recovery-key"),),
            max_attempts=1,
        ),
        actor="operator",
    )
    thought.gateway = RoutedModelGateway(
        thought.database, thought.subject_id, resources, provider_factory=lambda _: provider
    )
    with (
        patch.object(thought, "_commit", side_effect=RuntimeError("synthetic crash")),
        pytest.raises(RuntimeError, match="synthetic crash"),
    ):
        await thought.run_due()
    before = snapshot_calls(thought)
    assert await thought.run_due() == "intrinsic_thought_reflect"
    assert snapshot_calls(thought) == before
    assert isinstance(provider, FakeProvider) and len(provider.requests) == 1
    thought.verify_integrity()


@pytest.mark.asyncio
@pytest.mark.parametrize("migration_failure", [False, True])
async def test_v110_upgrade_does_not_rewrite_historical_model_outcomes(
    fixture: base.IntrinsicThoughtTestCase,
    tmp_path: Path,
    migration_failure: bool,
) -> None:
    with patch("noyra.core.database.CURRENT_SCHEMA_VERSION", 110):
        old = Database(tmp_path / "v110.sqlite3", strict_schema_contract=True)
        IdentityStore(old).ensure(fixture.subject_id, content_hash("legacy-failure"))
        gateway, provider = fixture.gateway([])
        thought = IntrinsicThought(
            old, fixture.subject_id, gateway, fixture.settings(), clock=lambda: fixture.clock_value
        )
        seed_call(thought, "failed")
    before = snapshot_calls(thought)
    if migration_failure:
        with (
            patch.dict(
                MIGRATIONS,
                {111: MIGRATIONS[111] + "SELECT * FROM synthetic_f05_migration_failure;"},
            ),
            pytest.raises(sqlite3.OperationalError, match="synthetic_f05_migration_failure"),
        ):
            Database(old.path, strict_schema_contract=True)
        with old.connection() as c:
            assert (
                c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
                == "110"
            )
            assert (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='thought_round_closures'"
                ).fetchone()
                is None
            )
        assert snapshot_calls(thought) == before
    upgraded = Database(old.path, strict_schema_contract=True)
    thought = IntrinsicThought(
        upgraded, fixture.subject_id, gateway, fixture.settings(), clock=lambda: fixture.clock_value
    )
    assert snapshot_calls(thought) == before
    assert closures(thought) == []
    upgraded.assert_schema_contract()
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    assert snapshot_calls(thought) == before
    assert len(closures(thought)) == 1 and provider.requests == []
    thought.verify_integrity()
    with upgraded.connection() as c:
        assert c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[
            0
        ] == str(CURRENT_SCHEMA_VERSION)
    with (
        patch("noyra.core.database.CURRENT_SCHEMA_VERSION", 110),
        pytest.raises(RuntimeError, match="newer than runtime"),
    ):
        Database(upgraded.path)


@pytest.mark.asyncio
async def test_closure_exports_and_runtime_remain_subject_scoped(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture)
    seed_call(thought, "failed")
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    foreign = "foreign-thought-round"
    IdentityStore(thought.database).ensure(foreign, content_hash(foreign))
    other = IntrinsicThought(
        thought.database,
        foreign,
        thought.gateway,
        thought.settings,
        clock=lambda: fixture.clock_value,
    )
    other.discover_agenda()
    selected = next(item for item in other.agenda() if item.source_id == other._absence_source_id())
    purpose, key = ThoughtRounds.identity(selected.agenda_id, 0)
    ledger = ModelLedger(thought.database)
    call, _ = ledger.prepare_call(
        foreign, "fake", "synthetic", purpose, content_hash("foreign"), key
    )
    ledger.finish_call(call.call_id, "failed", error_code="synthetic_failure")
    other._close_round(selected.agenda_id, 0, "model_failed")
    for subject in (thought.subject_id, foreign):
        artifact = RuntimeLogExporter(thought.database).export(subject, actor="synthetic-test")
        with ZipFile(BytesIO(artifact.content)) as z:
            records = [
                json.loads(line)
                for line in z.read("tables/thought_round_closures.jsonl").splitlines()
            ]
        assert len(records) == 1 and records[0]["subject_id"] == subject
    thought.verify_integrity()
    other.verify_integrity()


@pytest.mark.asyncio
async def test_cached_success_is_not_starved_by_fresh_higher_score_work_at_budget_cap(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()], max_thought_model_calls_per_day=1)
    with (
        patch.object(thought, "_commit", side_effect=RuntimeError("synthetic crash")),
        pytest.raises(RuntimeError, match="synthetic crash"),
    ):
        await thought.run_due()
    first = r.absence(thought)
    candidates = thought._agenda_candidates()
    candidates.append(
        {
            "source_type": "question",
            "source_id": "higher-scoring-fresh-question",
            "topic": "A different optional question",
            "urgency": 1.0,
            "novelty": 1.0,
            "emotional_weight": 1.0,
        }
    )
    with patch.object(thought, "_agenda_candidates", return_value=candidates):
        thought._refresh_agenda()
    selected = thought._select_agenda()
    assert selected is not None and selected.agenda_id == first.agenda_id
    assert await thought.run_due() == "intrinsic_thought_reflect"
    assert thought._episode_count(first.agenda_id) == 1
    assert isinstance(thought.gateway.provider, FakeProvider)
    assert len(thought.gateway.provider.requests) == 1


@pytest.mark.asyncio
async def test_legacy_successful_but_structurally_invalid_proposal_is_closed_not_replayed(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture)
    call_id = seed_call(thought, "executing")
    ledger = ModelLedger(thought.database)
    attempt = ledger.attempts(call_id)[0]
    ledger.finish_attempt(attempt.attempt_id, "succeeded", usage=ModelUsage(3, 2), cost_microusd=0)
    ledger.finish_call(call_id, "succeeded", response={"content": "{}"})
    before = snapshot_calls(thought)
    assert await thought.run_due() == "intrinsic_thought_rejected"
    assert snapshot_calls(thought) == before
    assert closures(thought)[0]["reason"] == "proposal_rejected"
    assert thought.verify_integrity()["thought_episodes"] == 0


@pytest.mark.asyncio
async def test_closure_proof_does_not_allow_cross_subject_insertion(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture)
    seed_call(thought, "failed")
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    payload = closures(thought)[0]
    foreign = "foreign-thought-closure"
    IdentityStore(thought.database).ensure(foreign, content_hash(foreign))
    payload["subject_id"] = foreign
    payload["round_number"] = 1
    payload.pop("state_hash")
    with (
        pytest.raises(sqlite3.IntegrityError, match="subject mismatch"),
        thought.database.transaction() as c,
    ):
        c.execute(
            "INSERT INTO thought_round_closures VALUES(?,?,?,?,?,?,?,?)",
            (*payload.values(), content_hash(payload)),
        )


@pytest.mark.asyncio
async def test_round_integrity_is_query_only_and_obeys_existing_row_budget(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture)
    for number in range(2):
        seed_call(thought, "failed", number=number)
        assert await thought.run_due() == "intrinsic_thought_model_failed"
        r.advance(fixture, 65)
    before = snapshot_calls(thought)
    receipts = closures(thought)
    with thought.database.connection() as c:
        c.execute("PRAGMA query_only=ON")
        assert ThoughtRounds(thought.database, thought.subject_id).verify(c) == 2
    report = IntegrityRegistry().run(
        thought.database,
        thought.subject_id,
        thought.database.path.parent,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=15,
        limits=IntegrityAuditLimits(max_rows_per_check=1),
        check_ids=("cognition.thought",),
    )
    assert report.status == "degraded" and report.p0 == ()
    assert report.p1 == ("cognition.thought:row_limit",)
    assert snapshot_calls(thought) == before and closures(thought) == receipts


def test_missing_round_storage_is_not_silently_recreated(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    database = fixture.kernel.database
    with database.transaction() as c:
        c.execute("DROP TABLE thought_round_closures")
    with pytest.raises(SchemaContractError):
        Database(database.path, strict_schema_contract=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
async def test_explicit_reconciliation_preserves_unknown_usage_but_unblocks_known_outcome(
    fixture: base.IntrinsicThoughtTestCase,
    outcome: str,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()])
    provider = thought.gateway.provider
    assert isinstance(provider, FakeProvider)
    success = provider._outcomes[0]
    assert isinstance(success, ProviderResponse)
    call_id = seed_call(thought, "unknown")
    ledger = ModelLedger(thought.database)
    attempts_before = ledger.attempts(call_id)
    ledger.reconcile_unknown(
        call_id,
        outcome=outcome,
        actor="operator",
        reason="synthetic provider evidence reviewed",
        response={"content": success.content} if outcome == "succeeded" else None,
    )
    assert await thought.run_due() == (
        "intrinsic_thought_reflect" if outcome == "succeeded" else "intrinsic_thought_model_failed"
    )
    assert ledger.attempts(call_id) == attempts_before
    assert provider.requests == []
    thought.verify_integrity()


@pytest.mark.asyncio
@pytest.mark.parametrize("change_context", [False, True])
async def test_explicit_retry_reuses_the_original_call_and_checks_request_context(
    fixture: base.IntrinsicThoughtTestCase,
    change_context: bool,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()])
    original = thought.gateway.provider
    assert isinstance(original, FakeProvider)
    provider = FakeProvider(
        [
            ProviderCallError("synthetic_unknown", retryable=False, outcome_unknown=True),
            original._outcomes[0],
        ]
    )
    thought.gateway.provider = provider
    assert await thought.run_due() == "intrinsic_thought_model_failed"
    with thought.database.connection() as c:
        call_id = c.execute("SELECT call_id FROM model_calls").fetchone()[0]
    ledger = ModelLedger(thought.database)
    ledger.prepare_unknown_retry(
        call_id, actor="operator", reason="synthetic one-shot retry approved"
    )
    if change_context:
        fixture.events.append(
            thought.subject_id, "experience", "synthetic-test", {"changed_input": True}
        )
    result = await thought.run_due()
    assert result == (
        "intrinsic_thought_model_failed" if change_context else "intrinsic_thought_reflect"
    )
    with thought.database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
    if change_context:
        assert len(provider.requests) == 1
        assert ledger.get_call(call_id).status == "prepared"
        assert await thought.run_due() is None
        assert closures(thought) == []
    else:
        assert len(provider.requests) == 2
        assert ledger.get_call(call_id).status == "succeeded"
        assert ledger.attempts(call_id)[0].status == "unknown"
        assert thought.verify_integrity()["thought_episodes"] == 1


@pytest.mark.asyncio
async def test_success_and_another_authorized_prepared_route_require_manual_review(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()])
    original = thought.gateway.provider
    assert isinstance(original, FakeProvider)
    response = original._outcomes[0]
    assert isinstance(response, ProviderResponse)
    ledger = ModelLedger(thought.database)
    success_id = seed_call(thought, "executing")
    attempt = ledger.attempts(success_id)[0]
    ledger.finish_attempt(attempt.attempt_id, "succeeded", usage=ModelUsage(3, 2), cost_microusd=0)
    ledger.finish_call(success_id, "succeeded", response={"content": response.content})
    other_id = seed_call(thought, "unknown", route="v2")
    ledger.prepare_unknown_retry(
        other_id, actor="operator", reason="synthetic duplicate-route grant"
    )
    before = snapshot_calls(thought)
    assert await thought.run_due() is None
    assert snapshot_calls(thought) == before
    assert original.requests == [] and closures(thought) == []


@pytest.mark.asyncio
async def test_recovered_success_closes_only_its_own_old_resource_wait(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture, [r.proposal()])
    resources = CognitiveResourceStore(
        thought.database, thought.database.path.parent / "synthetic-secrets"
    )
    routed = RoutedModelGateway(thought.database, thought.subject_id, resources)
    thought.discover_agenda()
    agenda = r.absence(thought)
    purpose, _ = thought.rounds.identity(agenda.agenda_id, 0)
    routed._record_unavailable("deep", purpose, "all_pool_resources_unavailable")
    routed._record_unavailable(
        "deep", "intrinsic_thought:another-agenda:0", "all_pool_resources_unavailable"
    )
    with (
        patch.object(thought, "_commit", side_effect=RuntimeError("synthetic crash")),
        pytest.raises(RuntimeError, match="synthetic crash"),
    ):
        await thought.run_due()
    assert await thought.run_due() == "intrinsic_thought_reflect"
    with thought.database.connection() as c:
        waits = {
            row[0]: row[1]
            for row in c.execute("SELECT purpose,status FROM waiting_cognitive_tasks")
        }
    assert waits[purpose] == "cancelled"
    assert waits["intrinsic_thought:another-agenda:0"] == "waiting"
    resources.verify_integrity(thought.subject_id)
    thought.verify_integrity()


@pytest.mark.asyncio
async def test_invalid_wait_evidence_rolls_back_failure_receipt_without_retrying_provider(
    fixture: base.IntrinsicThoughtTestCase,
) -> None:
    thought = r.thought_for(fixture)
    seed_call(thought, "failed")
    agenda = r.absence(thought)
    purpose, _ = thought.rounds.identity(agenda.agenda_id, 0)
    resources = CognitiveResourceStore(
        thought.database, thought.database.path.parent / "synthetic-secrets"
    )
    routed = RoutedModelGateway(thought.database, thought.subject_id, resources)
    routed._record_unavailable("deep", purpose, "all_pool_resources_unavailable")
    with thought.database.transaction() as c:
        c.execute("UPDATE waiting_cognitive_tasks SET state_hash=?", ("0" * 64,))
    with pytest.raises(IntegrityError, match="waiting cognitive task hash mismatch"):
        await thought.run_due()
    assert closures(thought) == []
    assert thought.rounds.current(agenda.agenda_id)[0] == 0
    assert isinstance(thought.gateway.provider, FakeProvider)
    assert thought.gateway.provider.requests == []


def test_intrinsic_thought_is_a_fenced_business_round() -> None:
    assert is_business_round("intrinsic_thought:agenda:0") is True
    assert is_business_round("intrinsic_thought:agenda-id") is False
