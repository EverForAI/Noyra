from __future__ import annotations

import asyncio
import base64
import json
import multiprocessing
import os
import sqlite3
import threading
import time
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from pydantic import SecretStr

from noyra.autonomy import AutonomyLoop, LoopConfig, TickResult
from noyra.capability import CapabilityGrant, CapabilityStore
from noyra.cognition.consciousness import ConsciousnessFrameStore
from noyra.core import ActionLedger, Database, EventStore, IdentityStore, SubjectKernel
from noyra.core import integrity as integrity_module
from noyra.core.errors import IntegrityError
from noyra.core.integrity import (
    IntegrityAuditLimits,
    IntegrityCheckOutcome,
    IntegrityCheckSpec,
    IntegrityRegistry,
    IntegrityRuntimeController,
)
from noyra.core.types import canonical_json, content_hash
from noyra.interaction import InteractionStore, TransportInput, TransportStore
from noyra.knowledge import CommonKnowledgeProposal, CommonKnowledgeStore
from noyra.mind import MemoryBlockStore, RelationshipStore
from noyra.model import (
    BudgetLimits,
    CognitiveResourceGroupInput,
    CognitiveResourceStore,
    EmbeddingResourceInput,
    EmbeddingResourceStore,
    ModelLedger,
)
from noyra.research import SearchProviderInput, SearchProviderStore
from noyra.service import NoyraService, ServiceSettings
from noyra.world import SourceRegistry
from support.faults import (
    blocking_integrity_process_worker,
    blocking_periodic_integrity_process_worker,
)

_REGISTRY_VERSION = "noyra-integrity-registry/v2"
_REPORT_VERSION = "noyra-integrity-report/v1"
_CHECK_IDS = (
    "core.sqlite_quick_check",
    "core.foreign_keys",
    "core.identity_continuity",
    "core.event_chain_tail",
    "core.event_payloads",
    "core.event_chain",
    "core.snapshot_archives",
    "core.archive_dead_letter",
    "core.event_causal_order",
    "core.storage_boundary",
    "core.actions",
    "mind.state",
    "mind.memory_blocks",
    "mind.entities",
    "mind.memory_lifecycle",
    "mind.memory_embeddings",
    "sleep.state",
    "interaction.state",
    "interaction.transport",
    "capability.state",
    "wallet.state",
    "world.state",
    "learning.outcomes",
    "cognition.consciousness",
    "cognition.action_deliberation",
    "cognition.project_executions",
    "cognition.goal_governance",
    "cognition.metacognition",
    "cognition.motivation",
    "cognition.autonomous_projects",
    "cognition.research",
    "cognition.self_model",
    "cognition.self_modification",
    "cognition.thought",
    "model.ledger",
    "model.resources",
    "model.embedding_resources",
    "knowledge.common",
)


def _active_kernel(tmp_path: Path, subject_id: str) -> SubjectKernel:
    kernel = SubjectKernel(
        tmp_path / f"{subject_id}.sqlite3",
        subject_id,
        content_hash({"subject": subject_id}),
    )
    kernel.boot()
    kernel.orient()
    kernel.activate()
    kernel.checkpoint({"ready": True}, reason="P1-02 integrity fixture")
    return kernel


def _controller(
    kernel: SubjectKernel,
    tmp_path: Path,
    *,
    policy_mode: str = "alert",
    interval_seconds: float = 60,
    startup_deadline_seconds: float = 30,
    periodic_deadline_seconds: float = 30,
    limits: IntegrityAuditLimits | None = None,
    registry: IntegrityRegistry | None = None,
    clock: Any = None,
    report_retention: int | None = None,
) -> IntegrityRuntimeController:
    kwargs: dict[str, Any] = {
        "policy_mode": policy_mode,
        "interval_seconds": interval_seconds,
        "startup_deadline_seconds": startup_deadline_seconds,
        "periodic_deadline_seconds": periodic_deadline_seconds,
        "limits": limits or IntegrityAuditLimits(),
    }
    if registry is not None:
        kwargs["registry"] = registry
    if clock is not None:
        kwargs["clock"] = clock
    if report_retention is not None:
        kwargs["report_retention"] = report_retention
    return IntegrityRuntimeController(kernel, tmp_path, **kwargs)


def _assert_report_schema(report: Any, *, subject_id: str, profile: str) -> None:
    payload = report.to_dict() if hasattr(report, "to_dict") else report
    assert payload["format_version"] == _REPORT_VERSION
    assert payload["registry_version"] == _REGISTRY_VERSION
    assert payload["subject_id"] == subject_id
    assert payload["profile"] == profile
    assert payload["policy_mode"] in {"off", "alert", "pause"}
    assert payload["status"] in {"ok", "degraded", "corrupt", "incomplete"}
    assert isinstance(payload["run_id"], str) and payload["run_id"]
    assert payload["started_at"] <= payload["completed_at"]
    assert isinstance(payload["snapshot"], dict)
    assert isinstance(payload["selection"], dict)
    assert isinstance(payload["checks"], list)
    assert isinstance(payload["findings"], list)
    assert isinstance(payload["summary"], dict)
    assert isinstance(payload["action"], dict)
    assert isinstance(payload["deferred_coverage"], list)
    for check in payload["checks"]:
        assert set(check) >= {
            "id",
            "version",
            "domain",
            "tier",
            "status",
            "severity",
            "reason_code",
            "duration_ms",
            "rows_examined",
            "bytes_examined",
            "details",
        }


def _manual_spec(
    check_id: str,
    runner: Any,
    *,
    tier: Literal["light", "deep"] = "deep",
) -> IntegrityCheckSpec:
    return IntegrityCheckSpec(
        check_id=check_id,
        version=1,
        domain="test",
        profiles=frozenset({"manual"}),
        runner=runner,
        tier=tier,
    )


def test_registry_v2_inventory_is_complete_and_stable(tmp_path: Path) -> None:
    kernel = _active_kernel(tmp_path, "Noyra-p102-registry")
    try:
        registry = IntegrityRegistry.default(
            kernel.database,
            kernel.subject_id,
            tmp_path,
        )

        assert registry.version == _REGISTRY_VERSION
        assert tuple(spec.check_id for spec in registry.checks) == _CHECK_IDS
        assert len({spec.check_id for spec in registry.checks}) == len(_CHECK_IDS)
        assert all(spec.version >= 1 for spec in registry.checks)
        assert all(spec.domain and spec.profiles for spec in registry.checks)
        assert {
            "mind.memory_lifecycle",
            "mind.memory_embeddings",
            "cognition.consciousness",
            "cognition.action_deliberation",
            "cognition.project_executions",
            "cognition.goal_governance",
            "cognition.metacognition",
            "cognition.motivation",
            "cognition.autonomous_projects",
            "cognition.research",
            "cognition.self_model",
            "cognition.self_modification",
            "cognition.thought",
            "model.resources",
            "wallet.state",
        } <= {spec.check_id for spec in registry.checks}
    finally:
        kernel.close()


def test_manual_registry_reads_all_previously_missing_durable_domains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject_id = "Noyra-p102-durable-table-coverage"
    kernel = _active_kernel(tmp_path, subject_id)
    expected = {
        "audit_records",
        "autonomous_project_assistance_requests",
        "autonomous_project_resource_uses",
        "autonomous_project_sleep_reflections",
        "cognitive_resource_group_revisions",
        "cognitive_resource_key_events",
        "embedding_circuit_states",
        "embedding_circuit_transitions",
        "embedding_usage_entries",
        "fatigue_transitions",
        "observation_content_segments",
        "training_exports",
        "training_policies",
        "training_records",
    }
    reads: set[str] = set()
    original_connect = kernel.database._connect

    def tracked_connect() -> sqlite3.Connection:
        connection = original_connect()

        def authorizer(
            action: int,
            first: str | None,
            _second: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_READ and first:
                reads.add(first)
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        return connection

    monkeypatch.setattr(kernel.database, "_connect", tracked_connect)
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=15,
        )

        assert report.status == "ok"
        assert report.p0 == ()
        assert report.p1 == ()
        assert expected <= reads
    finally:
        kernel.close()


def test_epistemic_registry_rejects_foreign_review_referencing_subject_evidence(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-epistemic-reverse-owner"
    foreign_subject = "Noyra-p102-epistemic-foreign"
    kernel = _active_kernel(tmp_path, subject_id)
    IdentityStore(kernel.database).ensure(
        foreign_subject, content_hash({"subject": foreign_subject})
    )
    source = SourceRegistry(kernel.database).register(
        subject_id,
        "P1-02 epistemic ownership source",
        "https://example.com/p102-epistemic-ownership",
        "news",
        trust_score=0.8,
        status="active",
    )
    event = EventStore(kernel.database).append(
        subject_id,
        "world_observation",
        "test",
        {"fixture": "epistemic reverse ownership"},
    )
    created_at = datetime.now(UTC).isoformat(timespec="milliseconds")
    observation_id = "obs-p102-epistemic-reverse-owner"
    observation_content = "Analyzed P1-02 ownership evidence."
    proposal = {
        "summary": "No epistemic state change is justified.",
        "belief_revisions": [],
        "prediction_resolutions": [],
    }
    call, _created = ModelLedger(kernel.database).prepare_call(
        subject_id,
        "fixture-provider",
        "fixture-model",
        f"epistemic_review:{observation_id}",
        content_hash({"fixture": "epistemic reverse ownership"}),
        "p102-epistemic-reverse-owner",
    )
    summary = str(proposal["summary"])
    state = {
        "model_call_id": call.call_id,
        "status": "no_change",
        "trigger_observation_id": observation_id,
        "belief_ids": [],
        "prediction_ids": [],
        "summary": summary,
    }
    with kernel.database.transaction() as connection:
        connection.execute(
            "DROP TRIGGER validate_epistemic_review_runs_model_call_id_subject_insert"
        )
        connection.execute(
            "DROP TRIGGER validate_epistemic_review_runs_trigger_observation_id_subject_insert"
        )
        connection.execute(
            "UPDATE model_calls SET status = 'succeeded', completed_at = ? WHERE call_id = ?",
            (created_at, call.call_id),
        )
        connection.execute(
            """INSERT INTO observations(
                observation_id, subject_id, source_id, event_id, canonical_url, title,
                content, content_hash, media_type, injection_signals_json, record_hash,
                http_etag, http_last_modified, fetched_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'text/plain', '[]', ?, NULL, NULL, ?, 'analyzed')""",
            (
                observation_id,
                subject_id,
                source.source_id,
                event.event_id,
                source.url,
                "P1-02 epistemic ownership",
                observation_content,
                content_hash(observation_content),
                content_hash({"fixture": "epistemic observation"}),
                created_at,
            ),
        )
        connection.execute(
            """INSERT INTO epistemic_review_runs(
                review_id, subject_id, model_call_id, idempotency_key, status,
                trigger_observation_id, proposal_json, proposal_hash,
                applied_belief_ids_json, resolved_prediction_ids_json, summary,
                state_hash, created_at, completed_at
            ) VALUES (?, ?, ?, ?, 'no_change', ?, ?, ?, '[]', '[]', ?, ?, ?, ?)""",
            (
                "review-p102-epistemic-reverse-owner",
                foreign_subject,
                call.call_id,
                f"epistemic-review:{observation_id}",
                observation_id,
                canonical_json(proposal),
                content_hash(proposal),
                summary,
                content_hash(state),
                created_at,
                created_at,
            ),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("cognition.metacognition",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("cognition.metacognition:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_research_registry_rejects_foreign_provider_use_referencing_subject_action(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-search-use-reverse-owner"
    foreign_subject = "Noyra-p102-search-use-foreign"
    kernel = _active_kernel(tmp_path, subject_id)
    IdentityStore(kernel.database).ensure(
        foreign_subject, content_hash({"subject": foreign_subject})
    )
    provider_store = SearchProviderStore(kernel.database, tmp_path / "secrets" / "search")
    config = provider_store.configure(
        foreign_subject,
        SearchProviderInput(
            provider_type="brave",
            label="P1-02 foreign search resource",
            api_key="p102-foreign-search-secret",
            rate_limit_per_hour=10,
        ),
        actor="operator",
    )
    query_hash = content_hash("P1-02 reverse ownership query")
    resource = {"config_id": config.config_id, "query_hash": query_hash, "limit": 8}
    action = ActionLedger(kernel.database).prepare(
        subject_id,
        "search",
        "search_api:brave",
        "[private-search-query]",
        resource,
        resource_cost=resource,
        idempotency_key="p102-search-use-reverse-owner",
    )
    with kernel.database.transaction() as connection:
        connection.execute(
            """INSERT INTO search_provider_uses(
                use_id, config_id, subject_id, action_id, query_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "searchuse-p102-reverse-owner",
                config.config_id,
                foreign_subject,
                action.action_id,
                query_hash,
                datetime.now(UTC).isoformat(timespec="milliseconds"),
            ),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("cognition.research",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("cognition.research:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_startup_light_produces_a_clean_versioned_report(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-startup-clean"
    kernel = _active_kernel(tmp_path, subject_id)
    try:
        controller = _controller(kernel, tmp_path, policy_mode="alert")

        report = controller.run_startup()

        assert report is not None
        _assert_report_schema(report, subject_id=subject_id, profile="startup_light")
        assert report.status == "ok"
        assert report.p0 == ()
        assert report.p1 == ()
        assert report.summary["checks"] > 0
        assert report.summary["ok"] == report.summary["checks"]
        assert kernel.lifecycle.current().state == "active"
        summary = controller.summary()
        assert summary["registry_version"] == _REGISTRY_VERSION
        assert summary["status"] == "ok"
        assert summary["p0"] == 0
        assert summary["p1"] == 0
        latest_path = controller._latest_path
        latest_payload = json.loads(latest_path.read_text(encoding="utf-8"))
        assert latest_payload["run_id"] == report.run_id
        assert latest_path.with_suffix(".json.sha256").read_text(encoding="ascii") == content_hash(
            latest_payload
        )
    finally:
        kernel.close()


def test_periodic_execution_is_due_driven_and_runs_one_persisted_shard(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-periodic"
    kernel = _active_kernel(tmp_path, subject_id)
    now = [datetime(2026, 8, 16, 12, 0, tzinfo=UTC)]
    controller = _controller(
        kernel,
        tmp_path,
        interval_seconds=60,
        clock=lambda: now[0],
    )
    try:
        controller.run_startup()
        assert controller.run_periodic_if_due() is None

        now[0] += timedelta(seconds=60)
        first = controller.run_periodic_if_due()

        assert first is not None
        assert first.profile == "periodic_deep"
        assert len(first.checks) == 1
        assert first.selection["check_ids"] == [first.checks[0].id]
        assert first.selection["shard_count"] == len(
            controller.registry.profile_checks("periodic_deep")
        )
        assert controller.run_periodic_if_due() is None
        first_cursor = controller.summary()["next_cursor"]

        restarted_scheduler = _controller(
            kernel,
            tmp_path,
            interval_seconds=60,
            clock=lambda: now[0],
        )
        assert restarted_scheduler.summary()["next_cursor"] == first_cursor
        second = restarted_scheduler.run_periodic_if_due(force=True)
        assert second is not None
        assert len(second.checks) == 1
        assert second.checks[0].id != first.checks[0].id
    finally:
        kernel.close()


def test_incomplete_shard_retries_are_bounded_and_do_not_starve_later_checks(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-bounded-shard-retry"
    kernel = _active_kernel(tmp_path, subject_id)
    calls: list[str] = []

    def incomplete(_context: Any) -> IntegrityCheckOutcome:
        calls.append("incomplete")
        return IntegrityCheckOutcome("incomplete", "p1", "fixture_incomplete")

    def corrupt(_context: Any) -> IntegrityCheckOutcome:
        calls.append("corrupt")
        return IntegrityCheckOutcome("corrupt", "p0", "fixture_corruption")

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.incomplete",
                1,
                "test",
                frozenset({"periodic_deep", "manual"}),
                incomplete,
            ),
            IntegrityCheckSpec(
                "test.corrupt",
                1,
                "test",
                frozenset({"periodic_deep", "manual"}),
                corrupt,
            ),
        )
    )
    controller = _controller(kernel, tmp_path, registry=registry)
    try:
        first = controller.run_periodic_if_due(force=True)
        second = controller.run_periodic_if_due(force=True)
        third = controller.run_periodic_if_due(force=True)

        assert first is not None and first.checks[0].id == "test.incomplete"
        assert second is not None and second.checks[0].id == "test.incomplete"
        assert third is not None and third.checks[0].id == "test.corrupt"
        assert calls == ["incomplete", "incomplete", "corrupt"]
        assert controller.summary()["p0"] == 1
        assert controller.summary()["p1"] == 1
    finally:
        kernel.close()


def test_startup_incomplete_coverage_uses_retry_schedule_even_with_p0(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-startup-p0-incomplete"
    kernel = _active_kernel(tmp_path, subject_id)
    now = datetime(2026, 8, 17, tzinfo=UTC)
    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.p0",
                1,
                "test",
                frozenset({"startup_light"}),
                lambda _context: IntegrityCheckOutcome("corrupt", "p0", "fixture_corruption"),
                "light",
            ),
            IntegrityCheckSpec(
                "test.incomplete",
                1,
                "test",
                frozenset({"startup_light"}),
                lambda _context: IntegrityCheckOutcome("incomplete", "p1", "fixture_incomplete"),
                "light",
            ),
        )
    )
    controller = IntegrityRuntimeController(
        kernel,
        tmp_path,
        policy_mode="alert",
        interval_seconds=60,
        retry_seconds=5,
        registry=registry,
        clock=lambda: now,
    )
    try:
        report = controller.run_startup()

        assert report is not None and report.status == "corrupt"
        assert any(result.status == "incomplete" for result in report.checks)
        assert controller.seconds_until_due() == 5
    finally:
        kernel.close()


def test_report_history_is_pruned_to_a_bounded_retention_window(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-report-retention"
    kernel = _active_kernel(tmp_path, subject_id)
    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.retention",
                1,
                "test",
                frozenset({"startup_light", "periodic_deep"}),
                lambda _context: IntegrityCheckOutcome(),
                "light",
            ),
        )
    )
    controller = _controller(
        kernel,
        tmp_path,
        registry=registry,
        report_retention=3,
    )
    try:
        controller.run_startup()
        for _ in range(6):
            controller.run_periodic_if_due(force=True)

        reports = sorted(controller._directory.glob("integrity-run_*.json"))
        digests = sorted(controller._directory.glob("integrity-run_*.json.sha256"))
        assert len(reports) == 3
        assert len(digests) == 3
        assert controller._latest_path.exists()
        assert controller._state_path.exists()
    finally:
        kernel.close()


def test_materializing_cursor_fetches_one_row_before_enforcing_the_budget() -> None:
    class CursorStub:
        arraysize = 128

        def __init__(self) -> None:
            self.rows = iter(((b"a" * 16,), (b"b" * 16,), (b"c" * 16,)))
            self.fetchone_calls = 0
            self.fetchmany_calls = 0

        def fetchone(self) -> Any | None:
            self.fetchone_calls += 1
            return next(self.rows, None)

        def fetchmany(self, _size: int) -> list[Any]:
            self.fetchmany_calls += 1
            raise AssertionError("budgeted materialization must not prefetch a batch")

    raw = CursorStub()
    budget = integrity_module._IntegrityBudget(
        IntegrityAuditLimits(
            max_rows_per_check=2,
            max_bytes_per_check=64,
            max_value_bytes=16,
        ),
        lambda: None,
    )
    cursor = integrity_module._BudgetedCursor(raw, budget)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="row_limit"):
        cursor.fetchall()

    assert raw.fetchone_calls == 3
    assert raw.fetchmany_calls == 0


def test_row_budget_is_a_p1_incomplete_finding_not_corruption(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-row-limit"
    kernel = _active_kernel(tmp_path, subject_id)
    for index in range(6):
        kernel.event_store.append(
            subject_id,
            "row_limit_fixture",
            "p102-test",
            {"index": index},
        )

    def scan_events(context: Any) -> IntegrityCheckOutcome:
        rows = context.connection.execute(
            "SELECT event_id, payload_json FROM events WHERE subject_id = ? ORDER BY rowid",
            (context.subject_id,),
        ).fetchall()
        return IntegrityCheckOutcome(details={"rows": len(rows)})

    registry = IntegrityRegistry((_manual_spec("test.row_limit", scan_events),))
    try:
        report = registry.run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            limits=IntegrityAuditLimits(max_rows_per_check=3),
        )

        _assert_report_schema(report, subject_id=subject_id, profile="manual")
        assert report.status == "degraded"
        assert report.p0 == ()
        assert report.p1 == ("test.row_limit:row_limit",)
        assert report.checks[0].status == "degraded"
        assert report.checks[0].severity == "p1"
        assert report.checks[0].reason_code == "row_limit"
        assert report.checks[0].rows_examined == 4
        assert kernel.lifecycle.current().state == "active"
    finally:
        kernel.close()


def test_cooperative_deadline_is_a_p1_and_does_not_run_the_checker(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-timeout"
    kernel = _active_kernel(tmp_path, subject_id)
    runner_calls = 0

    def should_not_run(_context: Any) -> IntegrityCheckOutcome:
        nonlocal runner_calls
        runner_calls += 1
        return IntegrityCheckOutcome()

    monotonic_values = iter((0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0))
    registry = IntegrityRegistry((_manual_spec("test.timeout", should_not_run),))
    try:
        report = registry.run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=0.5,
            monotonic=lambda: next(monotonic_values, 1.0),
        )

        assert runner_calls == 0
        assert report.status == "incomplete"
        assert report.p0 == ()
        assert report.p1 == ("registry.snapshot:timeout",)
        assert report.checks[0].reason_code == "timeout"
        assert report.checks[0].severity == "p1"
        assert kernel.lifecycle.current().state == "active"
    finally:
        kernel.close()


def test_checker_overrun_is_reclassified_after_the_runner_returns(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-checker-overrun"
    kernel = _active_kernel(tmp_path, subject_id)
    now = 0.0

    def slow_checker(_context: Any) -> dict[str, bool]:
        nonlocal now
        now = 0.1
        return {"finished": True}

    try:
        report = IntegrityRegistry((_manual_spec("test.slow", slow_checker),)).run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=0.05,
            monotonic=lambda: now,
        )

        assert report.status == "incomplete"
        assert report.p1 == ("test.slow:timeout",)
    finally:
        kernel.close()


def test_default_registry_hard_timeout_terminates_the_audit_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject_id = "Noyra-p102-hard-timeout"
    kernel = _active_kernel(tmp_path, subject_id)
    monkeypatch.setattr(
        integrity_module,
        "_run_isolated_registry_worker",
        blocking_integrity_process_worker,
    )
    started = time.monotonic()
    try:
        report = IntegrityRegistry().run_isolated(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            # Windows spawn plus coverage instrumentation can take more than two seconds
            # before the fixture worker emits its first completed result.
            deadline_seconds=5,
            check_ids=("core.sqlite_quick_check", "model.ledger"),
        )

        assert time.monotonic() - started < 8
        assert report.status == "corrupt"
        assert report.p0 == ("core.sqlite_quick_check:integrity_error",)
        assert report.p1 == ("model.ledger:hard_timeout",)
        assert [result.id for result in report.checks] == [
            "core.sqlite_quick_check",
            "model.ledger",
        ]
        assert not any(
            child.name == "noyra-integrity-audit" for child in multiprocessing.active_children()
        )
    finally:
        kernel.close()


def test_isolated_registry_shutdown_terminates_child_without_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject_id = "Noyra-p102-isolated-shutdown"
    kernel = _active_kernel(tmp_path, subject_id)
    shutdown = threading.Event()

    def checkpoint() -> None:
        if shutdown.is_set():
            raise integrity_module.IntegrityAuditShutdown("test shutdown")

    controller = IntegrityRuntimeController(
        kernel,
        tmp_path,
        policy_mode="alert",
        interval_seconds=1,
        checkpoint=checkpoint,
    )
    monkeypatch.setattr(
        integrity_module,
        "_run_isolated_registry_worker",
        blocking_integrity_process_worker,
    )
    errors: list[BaseException] = []

    def run_periodic() -> None:
        try:
            controller.run_periodic_if_due(force=True)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run_periodic)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not any(
            child.name == "noyra-integrity-audit" for child in multiprocessing.active_children()
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        shutdown.set()
        worker.join(timeout=5)

        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], integrity_module.IntegrityAuditShutdown)
        assert controller.latest_report is None
        assert not any(
            child.name == "noyra-integrity-audit" for child in multiprocessing.active_children()
        )
    finally:
        shutdown.set()
        worker.join(timeout=5)
        kernel.close()


@pytest.mark.parametrize("shutdown_mode", ("graceful", "cancel"))
def test_service_shutdown_terminates_periodic_integrity_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shutdown_mode: str,
) -> None:
    subject_id = f"Noyra-p102-process-shutdown-{shutdown_mode}"
    service = NoyraService(
        ServiceSettings(
            data_dir=tmp_path / shutdown_mode,
            subject_id=subject_id,
            genesis_hash=content_hash({"subject": subject_id}),
            host="127.0.0.1",
            port=0,
            active_interval_seconds=1,
            sleep_interval_seconds=1,
            integrity_interval_seconds=1,
            integrity_periodic_deadline_seconds=30,
        )
    )
    monkeypatch.setattr(
        integrity_module,
        "_run_isolated_registry_worker",
        blocking_periodic_integrity_process_worker,
    )

    async def exercise() -> None:
        task = asyncio.create_task(service.run())
        # Windows spawn, coverage instrumentation, and the growing startup
        # integrity inventory can legitimately take more than ten seconds
        # before the periodic worker is observable.  The assertion below is
        # about shutdown ownership, not startup latency.
        deadline = time.monotonic() + 30
        try:
            while True:
                periodic_running = service.integrity.latest_report is not None and any(
                    child.name == "noyra-integrity-audit"
                    for child in multiprocessing.active_children()
                )
                if periodic_running:
                    break
                if task.done():
                    await task
                    raise AssertionError("service stopped before periodic integrity started")
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            report_path = service.integrity._latest_path
            modified_at = report_path.stat().st_mtime_ns
            if shutdown_mode == "graceful":
                service.request_shutdown()
                await asyncio.wait_for(task, timeout=5)
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)

            assert not service.kernel.process_lock.held
            assert report_path.stat().st_mtime_ns == modified_at
            assert not any(
                child.name == "noyra-integrity-audit" for child in multiprocessing.active_children()
            )
        finally:
            service.request_shutdown()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(exercise())


def test_registry_checks_share_one_sqlite_snapshot(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-single-snapshot"
    kernel = _active_kernel(tmp_path, subject_id)
    observed_counts: list[int] = []

    def count_then_write(context: Any) -> IntegrityCheckOutcome:
        count = int(
            context.connection.execute(
                "SELECT COUNT(*) FROM events WHERE subject_id = ?",
                (context.subject_id,),
            ).fetchone()[0]
        )
        observed_counts.append(count)
        EventStore(kernel.database).append(
            context.subject_id,
            "snapshot_concurrent_write",
            "p102-test",
            {},
            event_id="evt-p102-concurrent",
        )
        return IntegrityCheckOutcome(details={"count": count})

    def count_again(context: Any) -> IntegrityCheckOutcome:
        count = int(
            context.connection.execute(
                "SELECT COUNT(*) FROM events WHERE subject_id = ?",
                (context.subject_id,),
            ).fetchone()[0]
        )
        observed_counts.append(count)
        return IntegrityCheckOutcome(details={"count": count})

    registry = IntegrityRegistry(
        (
            _manual_spec("test.snapshot.before", count_then_write),
            _manual_spec("test.snapshot.after", count_again),
        )
    )
    try:
        report = registry.run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
        )

        assert report.status == "ok"
        assert observed_counts[0] == observed_counts[1]
        with kernel.database.connection() as connection:
            live_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE subject_id = ?", (subject_id,)
                ).fetchone()[0]
            )
        assert live_count == observed_counts[0] + 1
        assert report.snapshot["event_count"] == observed_counts[0]
    finally:
        kernel.close()


def test_safe_pause_state_persists_across_restart(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-safe-pause"
    genesis_hash = content_hash({"subject": subject_id})
    kernel = _active_kernel(tmp_path, subject_id)
    database_path = kernel.database.path
    calls = 0

    def finding_then_clean(_context: Any) -> IntegrityCheckOutcome:
        nonlocal calls
        calls += 1
        if calls == 1:
            return IntegrityCheckOutcome(
                "corrupt",
                "p0",
                "forced_corruption",
                {"private_evidence": "must not enter the public lifecycle reason"},
            )
        return IntegrityCheckOutcome()

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                check_id="test.safe_pause",
                version=1,
                domain="test",
                profiles=frozenset({"startup_light"}),
                runner=finding_then_clean,
                tier="light",
            ),
        )
    )
    first_controller = _controller(
        kernel,
        tmp_path,
        policy_mode="pause",
        registry=registry,
    )
    try:
        first = first_controller.run_startup()

        assert first is not None
        assert first.status == "corrupt"
        assert first.p0 == ("test.safe_pause:forced_corruption",)
        assert first.action["result"] == "paused"
        assert kernel.lifecycle.current().state == "paused"
        assert first_controller.summary()["pause_pending"] is False
        with kernel.database.connection() as connection:
            transition = connection.execute(
                "SELECT source, payload_json FROM events WHERE subject_id = ? "
                "AND event_type = 'lifecycle_transition' ORDER BY rowid DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
        assert transition is not None
        assert transition["source"] == "resilience_watchdog"
        public_payload = json.loads(transition["payload_json"])
        assert public_payload["reason"] == "integrity policy requested safe pause"
        assert "test.safe_pause" not in transition["payload_json"]
        assert "private_evidence" not in transition["payload_json"]
    finally:
        kernel.close()

    reopened = SubjectKernel(database_path, subject_id, genesis_hash)
    assert reopened.boot().state == "paused"
    second_controller = IntegrityRuntimeController(
        reopened,
        tmp_path,
        policy_mode="pause",
        interval_seconds=60,
        startup_deadline_seconds=30,
        periodic_deadline_seconds=30,
        limits=IntegrityAuditLimits(),
        registry=registry,
    )
    try:
        assert second_controller.latest_report is not None
        assert second_controller.latest_report.status == "corrupt"
        assert second_controller.summary()["pause_pending"] is False

        second = second_controller.run_startup()

        assert second is not None
        assert reopened.lifecycle.current().state == "paused"
        assert second.status == "ok"
        assert second.action["result"] == "clean"
        assert second_controller.summary()["pause_pending"] is False
        with reopened.database.connection() as connection:
            paused_transitions = int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE subject_id = ? "
                    "AND event_type = 'lifecycle_transition' "
                    "AND json_extract(payload_json, '$.to') = 'paused'",
                    (subject_id,),
                ).fetchone()[0]
            )
        assert paused_transitions == 1
    finally:
        reopened.close()


def test_pause_policy_also_blocks_on_p1_resource_findings(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-p1-pause"
    kernel = _active_kernel(tmp_path, subject_id)
    for index in range(5):
        kernel.event_store.append(
            subject_id,
            "p1_pause_fixture",
            "p102-test",
            {"index": index},
        )

    def exceed_row_limit(context: Any) -> IntegrityCheckOutcome:
        context.connection.execute(
            "SELECT event_id FROM events WHERE subject_id = ? ORDER BY rowid",
            (context.subject_id,),
        ).fetchall()
        return IntegrityCheckOutcome()

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                check_id="test.p1_pause",
                version=1,
                domain="test",
                profiles=frozenset({"startup_light"}),
                runner=exceed_row_limit,
                tier="light",
            ),
        )
    )
    controller = _controller(
        kernel,
        tmp_path,
        policy_mode="pause",
        limits=IntegrityAuditLimits(max_rows_per_check=3),
        registry=registry,
    )
    try:
        report = controller.run_startup()

        assert report is not None
        assert report.status == "degraded"
        assert report.p0 == ()
        assert report.p1 == ("test.p1_pause:row_limit",)
        assert report.action["result"] == "paused"
        assert kernel.lifecycle.current().state == "paused"
    finally:
        kernel.close()


def test_unresolved_finding_reapplies_pause_after_operator_resume(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-resume-repause"
    kernel = _active_kernel(tmp_path, subject_id)
    checks = 0

    def corrupt(_context: Any) -> IntegrityCheckOutcome:
        nonlocal checks
        checks += 1
        return IntegrityCheckOutcome("corrupt", "p0", "resume_fixture")

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.resume_repause",
                1,
                "test",
                frozenset({"startup_light"}),
                corrupt,
                "light",
            ),
        )
    )
    controller = _controller(kernel, tmp_path, policy_mode="pause", registry=registry)
    try:
        first = controller.run_startup()
        assert first is not None and first.action["result"] == "paused"
        assert controller.summary()["pause_pending"] is False

        kernel.resume("P1-02 unresolved-finding fixture")
        reapplied = controller.run_periodic_if_due()

        assert reapplied is not None
        assert reapplied.action["result"] == "paused"
        assert kernel.lifecycle.current().state == "paused"
        assert checks == 1
    finally:
        kernel.close()


def test_pause_resume_race_remains_quarantined(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-pause-race"
    kernel = _active_kernel(tmp_path, subject_id)
    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.pause_race",
                1,
                "test",
                frozenset({"startup_light"}),
                lambda _context: IntegrityCheckOutcome("corrupt", "p0", "race_fixture"),
                "light",
            ),
        )
    )
    controller = _controller(kernel, tmp_path, policy_mode="pause", registry=registry)
    original_safe_pause = kernel.safe_pause

    def pause_then_resume(reason: str) -> Any:
        paused = original_safe_pause(reason)
        kernel.resume("P1-02 pause race fixture")
        return paused

    kernel.safe_pause = pause_then_resume  # type: ignore[assignment]
    try:
        report = controller.run_startup()

        assert report is not None
        assert report.action["result"] == "pause_lost"
        assert report.action["lifecycle_after"] == "active"
        assert kernel.lifecycle.current().state == "active"
        assert controller.summary()["p0"] == 1
        assert controller.summary()["pause_pending"] is True
    finally:
        kernel.close()


def test_service_boot_runs_startup_integrity_before_runtime_exposure(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-wiring",
        subject_id="Noyra-p102-service-wiring",
        genesis_hash=content_hash({"subject": "Noyra-p102-service-wiring"}),
        host="127.0.0.1",
        port=0,
        integrity_interval_seconds=60,
    )
    service = NoyraService(settings)
    try:
        assert service.integrity.latest_report is None
        assert service.loop.pre_tick_hook is not None
        assert service.loop.next_wakeup_hook is not None

        service.boot()

        report = service.integrity.latest_report
        assert report is not None
        assert report.profile == "startup_light"
        assert report.status == "ok"
        assert service.kernel.lifecycle.current().state == "active"
    finally:
        service.http.close()
        service.kernel.close()


def test_existing_database_missing_common_key_is_not_repaired_during_construction(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "common-key-construction"
    subject_id = "Noyra-p102-common-key-construction"
    settings = ServiceSettings(
        data_dir=data_dir,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
    )
    seed = NoyraService(settings)
    key_path = data_dir / "secrets" / "common-knowledge" / "common-knowledge-ed25519.key"
    try:
        seed.boot()
        assert key_path.is_file()
    finally:
        seed.http.close()
        seed.kernel.close()
    key_path.unlink()

    reopened = NoyraService(settings)
    try:
        assert not key_path.exists()
        report = IntegrityRegistry().run(
            reopened.kernel.database,
            subject_id,
            data_dir,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("knowledge.common",),
        )

        assert report.status == "degraded"
        assert report.p1 == ("knowledge.common:resource_unavailable",)
        assert not key_path.exists()
    finally:
        reopened.http.close()
        reopened.kernel.close()


def test_existing_database_missing_subject_state_is_not_recreated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "missing-subject-state"
    data_dir.mkdir(parents=True)
    Database(data_dir / "noyra.sqlite3")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"m" * 32).decode("ascii"),
    )
    monkeypatch.delenv("NOYRA_ARCHIVE_KEYRING_PATH", raising=False)
    subject_id = "Noyra-p102-missing-subject-state"
    service = NoyraService(
        ServiceSettings(
            data_dir=data_dir,
            subject_id=subject_id,
            genesis_hash=content_hash({"subject": subject_id}),
            host="127.0.0.1",
            port=0,
            integrity_mode="pause",
        )
    )
    try:
        with service.kernel.database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT 1 FROM subject_identity WHERE subject_id = ?", (subject_id,)
                ).fetchone()
                is None
            )
            assert (
                connection.execute(
                    "SELECT 1 FROM runtime_state WHERE subject_id = ?", (subject_id,)
                ).fetchone()
                is None
            )

        with pytest.raises(IntegrityError):
            service.boot()

        assert service.integrity.latest_report is not None
        assert service.integrity.latest_report.action["result"] == "pause_failed"
        with service.kernel.database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT 1 FROM subject_identity WHERE subject_id = ?", (subject_id,)
                ).fetchone()
                is None
            )
            assert (
                connection.execute(
                    "SELECT 1 FROM runtime_state WHERE subject_id = ?", (subject_id,)
                ).fetchone()
                is None
            )
    finally:
        service.http.close()
        service.kernel.close()


def test_existing_database_missing_storage_key_reaches_integrity_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "missing-subject-storage-key"
    subject_id = "Noyra-p102-missing-storage-key"
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
    )
    monkeypatch.delenv("NOYRA_ARCHIVE_KEYRING_PATH", raising=False)
    settings = ServiceSettings(
        data_dir=data_dir,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
        integrity_mode="pause",
    )
    seed = NoyraService(settings)
    try:
        seed.boot()
        with seed.kernel.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_subject_storage_key_delete")
            connection.execute(
                "DELETE FROM subject_storage_keys WHERE subject_id = ?",
                (subject_id,),
            )
    finally:
        seed.close()

    reopened = NoyraService(settings)
    try:
        reopened.boot()

        assert reopened.integrity.latest_report is not None
        assert reopened.integrity.latest_report.action["result"] == "paused"
        assert reopened.kernel.lifecycle.current().state == "paused"
        with reopened.kernel.database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT 1 FROM subject_storage_keys WHERE subject_id = ?",
                    (subject_id,),
                ).fetchone()
                is None
            )
    finally:
        reopened.close()


def test_existing_database_does_not_mask_unsafe_archive_root(tmp_path: Path) -> None:
    data_dir = tmp_path / "unsafe-archive-root"
    subject_id = "Noyra-p102-unsafe-archive-root"
    settings = ServiceSettings(
        data_dir=data_dir,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
        integrity_mode="pause",
    )
    seed = NoyraService(settings)
    seed.close()

    outside = tmp_path / "outside-archive-root"
    outside.mkdir()
    archive_root = data_dir / "subject" / "cold"
    try:
        archive_root.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable on this host: {error}")

    with pytest.raises(IntegrityError, match=r"symbolic link|reparse point"):
        NoyraService(settings)


def test_runtime_corruption_persists_pause_failure_without_breaking_summary(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "runtime-corruption-service"
    subject_id = "Noyra-p102-runtime-corruption-service"
    settings = ServiceSettings(
        data_dir=data_dir,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
        integrity_mode="pause",
    )
    seed = NoyraService(settings)
    try:
        seed.boot()
        with seed.kernel.database.transaction() as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE runtime_state SET version = 'bad' WHERE subject_id = ?",
                (subject_id,),
            )
    finally:
        seed.http.close()
        seed.kernel.close()

    reopened = NoyraService(settings)
    try:
        with pytest.raises(IntegrityError):
            reopened.boot()

        report = reopened.integrity.latest_report
        assert report is not None and report.status == "corrupt"
        assert report.action["result"] == "pause_failed"
        assert report.action["error_type"] == "LifecycleStateUnavailable"
        assert reopened.integrity.summary()["p0"] >= 1
        assert reopened.integrity.summary()["pause_pending"] is True
        assert reopened.integrity._latest_path.is_file()
        assert reopened.integrity._latest_path.with_suffix(".json.sha256").is_file()
    finally:
        reopened.http.close()
        reopened.kernel.close()

    persisted = NoyraService(settings)
    try:
        assert persisted.integrity.latest_report is not None
        assert persisted.integrity.latest_report.action["result"] == "pause_failed"
        assert persisted.integrity.summary()["p0"] >= 1
        assert persisted.integrity.summary()["pause_pending"] is True
    finally:
        persisted.http.close()
        persisted.kernel.close()


def test_service_boot_pause_policy_skips_cognition_bootstrap(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-pause",
        subject_id="Noyra-p102-service-pause",
        genesis_hash=content_hash({"subject": "Noyra-p102-service-pause"}),
        host="127.0.0.1",
        port=0,
        integrity_mode="pause",
    )
    service = NoyraService(settings)
    bootstrap_calls: list[str] = []

    class CognitionStub:
        def bootstrap(self) -> None:
            bootstrap_calls.append("bootstrap")

    def corrupt(_context: Any) -> IntegrityCheckOutcome:
        return IntegrityCheckOutcome("corrupt", "p0", "service_boot_fixture")

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.service_boot",
                1,
                "test",
                frozenset({"startup_light"}),
                corrupt,
                "light",
            ),
        )
    )
    service.integrity = IntegrityRuntimeController(
        service.kernel,
        settings.data_dir,
        policy_mode="pause",
        registry=registry,
    )
    service.http.integrity = service.integrity
    service.loop.pre_tick_hook = service._integrity_pre_tick
    service.loop.next_wakeup_hook = service.integrity.seconds_until_due
    service.cognition = CognitionStub()  # type: ignore[assignment]
    try:
        service.boot()

        assert service.kernel.lifecycle.current().state == "stopped"
        assert bootstrap_calls == []
        assert service.integrity.latest_report is not None
        assert service.integrity.latest_report.action["result"] == "pause_deferred"
        assert service._boot_recovery_pending is True
    finally:
        service.http.close()
        service.kernel.close()


def test_startup_quarantine_precedes_secret_repair_resource_sync_and_recovery(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "pre-gate-side-effects"
    subject_id = "Noyra-p102-pre-gate-side-effects"
    settings = ServiceSettings(
        data_dir=data_dir,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
        integrity_mode="pause",
    )
    seed = NoyraService(settings)
    secret_paths: list[Path] = []
    try:
        seed.boot()
        stores: tuple[tuple[Any, str], ...] = (
            (seed.http.transports, "transport"),
            (seed.http.search_providers, "search"),
            (seed.http.cognitive_resources, "cognitive"),
        )
        for index, (store, resource_type) in enumerate(stores):
            reference = f"p102-pre-gate-{index}.key"
            secret_path = store.secret_dir / reference
            secret_path.write_text("fixture-secret", encoding="utf-8")
            store.secret_cleanup.enqueue(
                subject_id,
                resource_type,
                f"fixture-{index}",
                reference,
                OSError("fixture cleanup pending"),
            )
            secret_paths.append(secret_path)
        with seed.kernel.database.connection() as connection:
            recovery_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE subject_id = ? "
                    "AND event_type = 'lifecycle_recovery'",
                    (subject_id,),
                ).fetchone()[0]
            )
    finally:
        seed.http.close()
        seed.kernel.close()

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.pre_gate",
                1,
                "test",
                frozenset({"startup_light"}),
                lambda _context: IntegrityCheckOutcome("corrupt", "p0", "pre_gate_fixture"),
                "light",
            ),
        )
    )
    service = NoyraService(settings)
    service.integrity = IntegrityRuntimeController(
        service.kernel,
        data_dir,
        policy_mode="pause",
        registry=registry,
    )
    service.http.integrity = service.integrity
    service.loop.pre_tick_hook = service._integrity_pre_tick
    service.loop.next_wakeup_hook = service.integrity.seconds_until_due
    proposal = CognitiveResourceGroupInput(
        pool="deep",
        label="P1-02 pre-gate fixture",
        base_url="https://example.invalid/v1",
        model="fixture-model",
        api_keys=(SecretStr("fixture-key"),),
    )
    service._pending_cognitive_resource_proposals = (proposal,)
    sync_calls: list[tuple[CognitiveResourceGroupInput, ...]] = []
    service._sync_cognitive_resources = sync_calls.append  # type: ignore[assignment]
    try:
        service.boot()

        report = service.integrity.latest_report
        assert report is not None and report.status == "corrupt"
        assert report.action["result"] == "paused"
        assert service._boot_recovery_pending is True
        assert sync_calls == []
        assert all(path.is_file() for path in secret_paths)
        assert service.http.transports.secret_cleanup.pending(subject_id) == 3
        with service.kernel.database.connection() as connection:
            assert (
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM events WHERE subject_id = ? "
                        "AND event_type = 'lifecycle_recovery'",
                        (subject_id,),
                    ).fetchone()[0]
                )
                == recovery_count
            )
    finally:
        service.http.close()
        service.kernel.close()


def test_alert_mode_health_is_degraded_without_exposing_private_findings(
    tmp_path: Path,
) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-alert-health",
        subject_id="Noyra-p102-alert-health",
        genesis_hash=content_hash({"subject": "Noyra-p102-alert-health"}),
        host="127.0.0.1",
        port=0,
        integrity_mode="alert",
    )
    service = NoyraService(settings)

    def corrupt(_context: Any) -> IntegrityCheckOutcome:
        return IntegrityCheckOutcome(
            "corrupt", "p0", "private_health_fixture", {"secret_detail": "not public"}
        )

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.health",
                1,
                "test",
                frozenset({"startup_light"}),
                corrupt,
                "light",
            ),
        )
    )
    service.integrity = IntegrityRuntimeController(
        service.kernel,
        settings.data_dir,
        policy_mode="alert",
        registry=registry,
    )
    service.http.integrity = service.integrity
    service.loop.pre_tick_hook = service._integrity_pre_tick
    service.loop.next_wakeup_hook = service.integrity.seconds_until_due
    try:
        service.boot()
        service.http.start()
        _, port = service.http.address
        with pytest.raises(HTTPError) as error:
            urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
        assert error.value.code == 503
        health = json.loads(error.value.read())
        assert health["status"] == "degraded"
        assert health["integrity"]["p0"] == 1
        assert health["integrity"]["p1"] == 0
        assert "test.health" not in json.dumps(health)
        assert "secret_detail" not in json.dumps(health)
        assert service.kernel.lifecycle.current().state == "active"
    finally:
        service.http.close()
        service.kernel.close()


def test_event_chain_coverage_damage_uses_safe_pause_fallback(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-pause-fallback"
    kernel = _active_kernel(tmp_path, subject_id)
    with kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_event_chain_delete")
        connection.execute(
            "DELETE FROM event_chain_roots WHERE subject_id = ? AND sequence_number = "
            "(SELECT MAX(sequence_number) FROM event_chain_roots WHERE subject_id = ?)",
            (subject_id, subject_id),
        )
    try:
        report = _controller(kernel, tmp_path, policy_mode="pause").run_startup()

        assert report is not None
        assert report.status == "corrupt"
        assert report.action["result"] == "paused"
        assert report.action["event_recorded"] is False
        assert kernel.lifecycle.current().state == "paused"
        with kernel.database.connection() as connection:
            fallback = connection.execute(
                "SELECT actor, payload_json FROM audit_records WHERE subject_id = ? "
                "AND action = 'integrity_safe_pause_fallback' ORDER BY rowid DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
        assert fallback is not None
        assert fallback["actor"] == "resilience_watchdog"
        assert json.loads(fallback["payload_json"])["to"] == "paused"
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("persisted_state", "expected_state", "expected_action"),
    (
        ("active", "paused", "paused"),
        ("stopped", "stopped", "pause_deferred"),
        ("deep_sleep", "deep_sleep", "pause_deferred"),
    ),
)
def test_startup_integrity_precedes_restart_recovery_writes(
    tmp_path: Path,
    persisted_state: str,
    expected_state: str,
    expected_action: str,
) -> None:
    data_dir = tmp_path / persisted_state
    subject_id = f"Noyra-p102-pre-recovery-{persisted_state}"
    settings = ServiceSettings(
        data_dir=data_dir,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
        integrity_mode="pause",
    )
    seed = NoyraService(settings)
    try:
        seed.boot()
        if persisted_state == "stopped":
            seed.kernel.lifecycle.transition("stopped", "P1-02 pre-recovery fixture")
        elif persisted_state == "deep_sleep":
            seed.kernel.lifecycle.transition("winding_down", "P1-02 pre-recovery fixture")
            seed.kernel.lifecycle.transition("reflective_sleep", "P1-02 pre-recovery fixture")
            seed.kernel.lifecycle.transition("deep_sleep", "P1-02 pre-recovery fixture")
        with seed.kernel.database.transaction() as connection:
            recovery_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE subject_id = ? "
                    "AND event_type = 'lifecycle_recovery'",
                    (subject_id,),
                ).fetchone()[0]
            )
            connection.execute("DROP TRIGGER prevent_event_chain_delete")
            connection.execute(
                "DELETE FROM event_chain_roots WHERE subject_id = ? AND sequence_number = "
                "(SELECT MAX(sequence_number) FROM event_chain_roots WHERE subject_id = ?)",
                (subject_id, subject_id),
            )
    finally:
        seed.http.close()
        seed.kernel.close()

    service = NoyraService(settings)
    try:
        service.boot()

        report = service.integrity.latest_report
        assert report is not None and report.status == "corrupt"
        assert report.action["result"] == expected_action
        assert service.kernel.lifecycle.current().state == expected_state
        assert service._boot_recovery_pending is True
        assert service.integrity._latest_path.exists()
        assert service.integrity._latest_path.with_suffix(".json.sha256").exists()
        with service.kernel.database.connection() as connection:
            assert (
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM events WHERE subject_id = ? "
                        "AND event_type = 'lifecycle_recovery'",
                        (subject_id,),
                    ).fetchone()[0]
                )
                == recovery_count
            )
    finally:
        service.http.close()
        service.kernel.close()


def test_large_generated_result_is_stopped_by_the_central_row_budget(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-large-bound"
    kernel = _active_kernel(tmp_path, subject_id)

    def scan_large_result(context: Any) -> IntegrityCheckOutcome:
        context.connection.execute(
            "WITH RECURSIVE sequence(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM sequence WHERE value < 100000"
            ") SELECT value FROM sequence"
        ).fetchall()
        return IntegrityCheckOutcome()

    registry = IntegrityRegistry((_manual_spec("test.large_bound", scan_large_result),))
    try:
        report = registry.run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            limits=IntegrityAuditLimits(max_rows_per_check=1_000),
        )

        assert report.status == "degraded"
        assert report.checks[0].reason_code == "row_limit"
        assert report.checks[0].rows_examined == 1_001
    finally:
        kernel.close()


def test_large_streaming_scan_is_bounded_by_deadline_not_cumulative_rows(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-large-stream"
    kernel = _active_kernel(tmp_path, subject_id)

    def stream_large_result(context: Any) -> IntegrityCheckOutcome:
        rows = 0
        for _row in context.connection.execute(
            "WITH RECURSIVE sequence(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM sequence WHERE value < 100000"
            ") SELECT value FROM sequence"
        ):
            rows += 1
        return IntegrityCheckOutcome(details={"rows": rows})

    try:
        report = IntegrityRegistry((_manual_spec("test.large_stream", stream_large_result),)).run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            limits=IntegrityAuditLimits(max_rows_per_check=1_000),
        )

        assert report.status == "ok"
        assert report.checks[0].rows_examined == 100_000
        assert report.checks[0].details["rows"] == 100_000
    finally:
        kernel.close()


def test_previously_omitted_cognition_domain_reports_named_p0_corruption(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-consciousness-tamper"
    kernel = _active_kernel(tmp_path, subject_id)
    ConsciousnessFrameStore(kernel.database, subject_id).ensure_initial()
    with kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_consciousness_frame_update")
        connection.execute(
            "UPDATE consciousness_frames SET sequence_number = 2 WHERE subject_id = ?",
            (subject_id,),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("cognition.consciousness",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("cognition.consciousness:integrity_error",)
        assert report.checks[0].id == "cognition.consciousness"
        assert report.checks[0].severity == "p0"
    finally:
        kernel.close()


def test_autonomy_pre_tick_guard_pauses_before_active_work_and_bounds_wakeup(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-autonomy-guard"
    kernel = _active_kernel(tmp_path, subject_id)
    active_calls = 0
    wakeup_calls = 0
    stop_event: asyncio.Event | None = None

    def corrupt(_context: Any) -> IntegrityCheckOutcome:
        return IntegrityCheckOutcome("corrupt", "p0", "forced_corruption")

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                check_id="test.autonomy_guard",
                version=1,
                domain="test",
                profiles=frozenset({"periodic_deep"}),
                runner=corrupt,
            ),
        )
    )
    controller = _controller(
        kernel,
        tmp_path,
        policy_mode="pause",
        registry=registry,
    )

    async def active_hook() -> str:
        nonlocal active_calls
        active_calls += 1
        return "active_work_ran"

    async def pre_tick_hook() -> TickResult | None:
        report = controller.run_periodic_if_due(force=True)
        assert report is not None
        assert stop_event is not None
        stop_event.set()
        if report.action["result"] == "paused":
            return TickResult("paused", "integrity_safe_pause", None, 10)
        return None

    def next_wakeup_hook() -> float:
        nonlocal wakeup_calls
        wakeup_calls += 1
        return controller.seconds_until_due()

    loop = AutonomyLoop(
        kernel,
        config=LoopConfig(active_interval_seconds=300, sleep_interval_seconds=10),
        active_hook=active_hook,
        pre_tick_hook=pre_tick_hook,
        next_wakeup_hook=next_wakeup_hook,
    )

    async def exercise() -> None:
        nonlocal stop_event
        stop_event = asyncio.Event()
        await loop.run_forever(stop_event)

    try:
        asyncio.run(exercise())

        assert active_calls == 0
        assert wakeup_calls == 1
        assert kernel.lifecycle.current().state == "paused"
        assert loop.health()["consecutive_failures"] == 0
    finally:
        kernel.close()


def test_alert_finding_survives_clean_shards_until_the_same_check_passes(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-active-alert"
    kernel = _active_kernel(tmp_path, subject_id)
    finding_calls = 0

    def finding_then_clean(_context: Any) -> IntegrityCheckOutcome:
        nonlocal finding_calls
        finding_calls += 1
        if finding_calls == 1:
            return IntegrityCheckOutcome("corrupt", "p0", "persistent_fixture")
        return IntegrityCheckOutcome()

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.persistent",
                1,
                "test",
                frozenset({"periodic_deep"}),
                finding_then_clean,
            ),
            IntegrityCheckSpec(
                "test.unrelated_clean",
                1,
                "test",
                frozenset({"periodic_deep"}),
                lambda _context: IntegrityCheckOutcome(),
            ),
        )
    )
    controller = _controller(kernel, tmp_path, registry=registry)
    try:
        first = controller.run_periodic_if_due(force=True)
        second = controller.run_periodic_if_due(force=True)
        summary_after_second = controller.summary()
        third = controller.run_periodic_if_due(force=True)

        assert first is not None and first.status == "corrupt"
        assert second is not None and second.checks[0].id == "test.unrelated_clean"
        assert second.checks[0].status == "ok"
        assert second.status == "corrupt"
        assert second.p0 == ("test.persistent:persistent_fixture",)
        assert summary_after_second["p0"] == 1
        assert third is not None and third.status == "ok"
        assert third.p0 == ()
        assert finding_calls == 2
    finally:
        kernel.close()


def test_deferred_pause_survives_clean_shards_and_applies_after_wake(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-deferred-pause"
    kernel = _active_kernel(tmp_path, subject_id)
    runner_calls = 0

    def corrupt(_context: Any) -> IntegrityCheckOutcome:
        nonlocal runner_calls
        runner_calls += 1
        return IntegrityCheckOutcome("corrupt", "p0", "sleep_fixture")

    def clean(_context: Any) -> IntegrityCheckOutcome:
        nonlocal runner_calls
        runner_calls += 1
        return IntegrityCheckOutcome()

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.sleep_corrupt", 1, "test", frozenset({"periodic_deep"}), corrupt
            ),
            IntegrityCheckSpec("test.sleep_clean", 1, "test", frozenset({"periodic_deep"}), clean),
        )
    )
    controller = _controller(kernel, tmp_path, policy_mode="pause", registry=registry)
    try:
        kernel.lifecycle.transition("winding_down", "P1-02 deferred pause fixture")
        first = controller.run_periodic_if_due(force=True)
        second = controller.run_periodic_if_due(force=True)

        assert first is not None and first.action["result"] == "pause_deferred"
        assert second is not None and second.action["result"] == "pause_deferred"
        assert second.status == "corrupt"
        assert controller.summary()["pause_pending"] is True
        kernel.lifecycle.transition("reflective_sleep", "P1-02 deferred pause fixture")
        kernel.lifecycle.transition("deep_sleep", "P1-02 deferred pause fixture")
        kernel.lifecycle.transition("waking", "P1-02 deferred pause fixture")
        kernel.lifecycle.transition("active", "P1-02 deferred pause fixture")

        applied = controller.run_periodic_if_due(force=True)

        assert applied is not None
        assert applied.action["result"] == "paused"
        assert kernel.lifecycle.current().state == "paused"
        assert runner_calls == 2
    finally:
        kernel.close()


def test_registry_upgrade_preserves_quarantine_until_full_new_registry_passes(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-registry-upgrade"
    genesis_hash = content_hash({"subject": subject_id})
    database_path = tmp_path / f"{subject_id}.sqlite3"

    class OldRegistry(IntegrityRegistry):
        version = "noyra-integrity-registry/test-v1"

    class NewRegistry(IntegrityRegistry):
        version = "noyra-integrity-registry/test-v2"

    old_registry = OldRegistry(
        (
            IntegrityCheckSpec(
                "test.old_removed_check",
                1,
                "test",
                frozenset({"periodic_deep", "manual"}),
                lambda _context: IntegrityCheckOutcome("corrupt", "p0", "old_fixture"),
            ),
        )
    )
    first_kernel = _active_kernel(tmp_path, subject_id)
    try:
        first_kernel.lifecycle.transition("winding_down", "registry upgrade fixture")
        old_controller = _controller(
            first_kernel,
            tmp_path,
            policy_mode="pause",
            registry=old_registry,
        )
        old_report = old_controller.run_periodic_if_due(force=True)
        assert old_report is not None and old_report.action["result"] == "pause_deferred"
        assert old_controller.summary()["p0"] == 1
    finally:
        first_kernel.close()

    new_calls = 0

    def new_clean(_context: Any) -> IntegrityCheckOutcome:
        nonlocal new_calls
        new_calls += 1
        return IntegrityCheckOutcome()

    new_registry = NewRegistry(
        (
            IntegrityCheckSpec(
                "test.new_check",
                1,
                "test",
                frozenset({"startup_light", "periodic_deep", "manual"}),
                new_clean,
                "light",
            ),
        )
    )
    reopened = SubjectKernel(database_path, subject_id, genesis_hash)
    reopened.acquire_ownership()
    controller = _controller(
        reopened,
        tmp_path,
        policy_mode="pause",
        registry=new_registry,
    )
    try:
        assert controller.summary()["p0"] == 1
        assert controller.summary()["pause_pending"] is True

        report = controller.run_startup()

        assert report is not None
        assert report.profile == "manual"
        assert report.status == "ok"
        assert report.action["result"] == "clean"
        assert controller.summary()["p0"] == 0
        assert controller.summary()["pause_pending"] is False
        assert new_calls == 1
    finally:
        reopened.close()


def test_integrity_state_does_not_cross_subjects_in_one_data_root(tmp_path: Path) -> None:
    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.subject_bound",
                1,
                "test",
                frozenset({"startup_light"}),
                lambda _context: IntegrityCheckOutcome("corrupt", "p0", "subject_fixture"),
                "light",
            ),
        )
    )
    first_kernel = _active_kernel(tmp_path, "Noyra-p102-subject-a")
    first_run_id = ""
    first_directory: Path | None = None
    try:
        first_controller = _controller(first_kernel, tmp_path, registry=registry)
        first_directory = first_controller._directory
        first = first_controller.run_startup()
        assert first is not None and first.status == "corrupt"
        first_run_id = first.run_id
    finally:
        first_kernel.close()

    second_kernel = _active_kernel(tmp_path, "Noyra-p102-subject-b")
    try:
        second = _controller(second_kernel, tmp_path, registry=registry)
        assert second.latest_report is None
        assert second.summary()["status"] == "ok"
        assert second.summary()["p0"] == 0
        second_report = second.run_startup()
        assert second_report is not None and second_report.status == "corrupt"
        assert second._directory != first_directory
    finally:
        second_kernel.close()

    reopened_kernel = _active_kernel(tmp_path, "Noyra-p102-subject-a")
    try:
        reopened = _controller(reopened_kernel, tmp_path, registry=registry)
        assert reopened.latest_report is not None
        assert reopened.latest_report.run_id == first_run_id
        assert reopened.summary()["status"] == "corrupt"
        assert reopened.summary()["p0"] == 1
    finally:
        reopened_kernel.close()


def test_snapshot_marker_obeys_the_registry_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject_id = "Noyra-p102-snapshot-deadline"
    kernel = _active_kernel(tmp_path, subject_id)
    monotonic_calls = 0

    def slow_marker(connection: Any, marker_subject_id: str) -> dict[str, Any]:
        connection.execute(
            "WITH RECURSIVE sequence(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM sequence WHERE value < 100000000"
            ") SELECT SUM(value) FROM sequence"
        ).fetchone()
        return {"subject_id": marker_subject_id}

    def monotonic() -> float:
        nonlocal monotonic_calls
        monotonic_calls += 1
        return 0.0 if monotonic_calls < 4 else 2.0

    monkeypatch.setattr(IntegrityRegistry, "_snapshot_marker", staticmethod(slow_marker))
    try:
        report = IntegrityRegistry((_manual_spec("test.never_runs", lambda _context: {}),)).run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=1,
            monotonic=monotonic,
        )

        assert report.status == "incomplete"
        assert report.p1 == ("registry.snapshot:timeout",)
        assert report.checks[0].reason_code == "timeout"
    finally:
        kernel.close()


@pytest.mark.parametrize("error_type", [ValueError, TypeError])
def test_checker_programming_errors_are_p1_not_false_corruption(
    tmp_path: Path, error_type: type[Exception]
) -> None:
    subject_id = f"Noyra-p102-checker-{error_type.__name__.lower()}"
    kernel = _active_kernel(tmp_path, subject_id)

    def broken_checker(_context: Any) -> IntegrityCheckOutcome:
        raise error_type("checker fixture")

    try:
        report = IntegrityRegistry((_manual_spec("test.broken", broken_checker),)).run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
        )

        assert report.status == "incomplete"
        assert report.p0 == ()
        assert report.p1 == ("test.broken:checker_runtime",)
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("check_id", "profile"),
    (("core.event_chain_tail", "startup_light"), ("core.event_chain", "manual")),
)
@pytest.mark.parametrize("bad_sequence", ("bad", 1.5))
def test_malformed_persisted_event_sequence_is_p0(
    tmp_path: Path,
    check_id: str,
    profile: Literal["startup_light", "manual"],
    bad_sequence: Any,
) -> None:
    subject_id = (
        f"Noyra-p102-event-sequence-{check_id.rsplit('.', 1)[-1]}-{content_hash(bad_sequence)[:8]}"
    )
    kernel = _active_kernel(tmp_path, subject_id)
    with kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_event_chain_update")
        connection.execute(
            "UPDATE event_chain_roots SET sequence_number = ? WHERE event_id = "
            "(SELECT event_id FROM event_chain_roots WHERE subject_id = ? "
            "ORDER BY sequence_number DESC LIMIT 1)",
            (bad_sequence, subject_id),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile=profile,
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=(check_id,),
        )

        assert report.status == "corrupt"
        assert report.p0 == (f"{check_id}:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_malformed_schema_version_is_a_registry_snapshot_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-schema-version-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    with kernel.database.transaction() as connection:
        connection.execute("UPDATE schema_meta SET value = 'bad' WHERE key = 'schema_version'")
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("core.sqlite_quick_check",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("registry.snapshot:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_physical_sqlite_corruption_is_registry_snapshot_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-physical-sqlite-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    kernel.close()
    with kernel.database.path.open("r+b") as stream:
        stream.seek(0)
        stream.write(b"not-a-sqlite-db!")
        stream.flush()
        os.fsync(stream.fileno())

    report = IntegrityRegistry().run(
        kernel.database,
        subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=5,
        check_ids=("core.sqlite_quick_check",),
    )

    assert report.status == "corrupt"
    assert report.p0 == ("registry.snapshot:integrity_error",)
    assert report.p1 == ()


@pytest.mark.parametrize(
    "damage",
    ("runtime_version", "identity_version", "snapshot_version"),
)
def test_malformed_identity_continuity_numbers_are_p0(tmp_path: Path, damage: str) -> None:
    subject_id = f"Noyra-p102-identity-number-{damage}"
    kernel = _active_kernel(tmp_path, subject_id)
    with kernel.database.transaction() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        if damage == "runtime_version":
            connection.execute(
                "UPDATE runtime_state SET version = 'bad' WHERE subject_id = ?",
                (subject_id,),
            )
        elif damage == "identity_version":
            connection.execute(
                "UPDATE subject_identity SET state_version = 'bad' WHERE subject_id = ?",
                (subject_id,),
            )
        else:
            connection.execute(
                "UPDATE state_snapshots SET state_version = 'bad' WHERE snapshot_id = "
                "(SELECT last_checkpoint FROM subject_identity WHERE subject_id = ?)",
                (subject_id,),
            )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("core.identity_continuity",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("core.identity_continuity:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_malformed_snapshot_archive_blob_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-snapshot-blob-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    with kernel.database.transaction() as connection:
        connection.execute(
            "INSERT INTO snapshot_archives(archive_id, subject_id, first_version, last_version, "
            "snapshot_count, compressed_payload, payload_hash, compressed_hash, created_at) "
            "VALUES (?, ?, 1, 1, 1, ?, ?, ?, ?)",
            (
                "snapshot-archive-invalid-blob",
                subject_id,
                "not-a-blob",
                content_hash({"entries": []}),
                content_hash({"compressed_hex": ""}),
                datetime.now(UTC).isoformat(timespec="milliseconds"),
            ),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("core.snapshot_archives",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("core.snapshot_archives:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


@pytest.mark.parametrize("bad_request", (42, '{"x":NaN}'))
def test_malformed_model_call_json_is_p0(tmp_path: Path, bad_request: Any) -> None:
    subject_id = "Noyra-p102-model-json-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    request = {"messages": [{"role": "user", "content": "bounded fixture"}]}
    call, _created = ModelLedger(kernel.database).prepare_call(
        subject_id,
        "fixture-provider",
        "fixture-model",
        "integrity-test",
        content_hash(request),
        "p102-model-json",
        request=request,
    )
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE model_calls SET request_json = ? WHERE call_id = ?",
            (bad_request, call.call_id),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("model.ledger",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("model.ledger:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("corruption_sql", "parameters"),
    (
        (
            "UPDATE model_calls SET status = 'prepared' WHERE call_id = ?",
            ("call_id",),
        ),
        (
            "UPDATE model_attempts SET attempt_number = 100 WHERE attempt_id = ?",
            ("attempt_id",),
        ),
    ),
)
def test_model_ledger_cross_row_state_corruption_is_p0(
    tmp_path: Path,
    corruption_sql: str,
    parameters: tuple[str, ...],
) -> None:
    subject_id = "Noyra-p102-model-state-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    ledger = ModelLedger(kernel.database)
    call, _created = ledger.prepare_call(
        subject_id,
        "fixture-provider",
        "fixture-model",
        "integrity-test",
        "fixture-request-hash",
        "p102-model-state",
    )
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(10, 10_000, 10_000, 1_000_000),
        reserved_input_tokens=10,
        reserved_output_tokens=10,
        reserved_cost_microusd=10,
    )
    values = {
        "call_id": call.call_id,
        "attempt_id": attempt.attempt_id,
    }
    with kernel.database.transaction() as connection:
        connection.execute(corruption_sql, tuple(values[name] for name in parameters))
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("model.ledger",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("model.ledger:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_malformed_common_knowledge_version_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-common-version-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    store = CommonKnowledgeStore(
        kernel.database,
        subject_id,
        tmp_path / "secrets" / "common-knowledge",
    )
    package = store.publish(
        CommonKnowledgeProposal(
            scope="protocol",
            title="P1-02 durable version fixture",
            summary="A signed package used to verify corruption classification.",
            payload={
                "procedure": ["validate durable fields"],
                "compatibility": ["integrity-v1"],
                "validation": ["classified as p0"],
                "tags": ["integrity"],
            },
        )
    )
    with kernel.database.transaction() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE common_knowledge_packages SET version = 'bad' WHERE package_id = ?",
            (package.package_id,),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("knowledge.common",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("knowledge.common:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_nonfinite_common_knowledge_json_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-common-json-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    store = CommonKnowledgeStore(
        kernel.database,
        subject_id,
        tmp_path / "secrets" / "common-knowledge",
    )
    package = store.publish(
        CommonKnowledgeProposal(
            scope="protocol",
            title="P1-02 durable JSON fixture",
            summary="A signed package used to verify non-finite JSON rejection.",
            payload={
                "procedure": ["reject non-finite values"],
                "compatibility": ["integrity-v1"],
                "validation": ["classified as p0"],
                "tags": ["integrity"],
            },
        )
    )
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE common_knowledge_packages SET payload_json = '{\"x\":NaN}' "
            "WHERE package_id = ?",
            (package.package_id,),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("knowledge.common",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("knowledge.common:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_common_knowledge_payload_json_blob_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-common-json-blob"
    kernel = _active_kernel(tmp_path, subject_id)
    store = CommonKnowledgeStore(
        kernel.database,
        subject_id,
        tmp_path / "secrets" / "common-knowledge",
    )
    package = store.publish(
        CommonKnowledgeProposal(
            scope="protocol",
            title="P1-02 durable JSON BLOB fixture",
            summary="A signed package used to reject SQLite BLOB JSON.",
            payload={
                "procedure": ["reject BLOB storage classes"],
                "compatibility": ["integrity-v1"],
                "validation": ["classified as p0"],
                "tags": ["integrity"],
            },
        )
    )
    with kernel.database.transaction() as connection:
        row = connection.execute(
            "SELECT payload_json FROM common_knowledge_packages WHERE package_id = ?",
            (package.package_id,),
        ).fetchone()
        connection.execute(
            "UPDATE common_knowledge_packages SET payload_json = ? WHERE package_id = ?",
            (row["payload_json"].encode("utf-8"), package.package_id),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("knowledge.common",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("knowledge.common:integrity_error",)
    finally:
        kernel.close()


def test_nonfinite_snapshot_state_json_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-snapshot-json-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE state_snapshots SET state_json = '{\"x\":NaN}' WHERE snapshot_id = "
            "(SELECT last_checkpoint FROM subject_identity WHERE subject_id = ?)",
            (subject_id,),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("core.identity_continuity",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("core.identity_continuity:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_snapshot_state_json_blob_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-snapshot-json-blob"
    kernel = _active_kernel(tmp_path, subject_id)
    with kernel.database.transaction() as connection:
        row = connection.execute(
            "SELECT snapshot_id, state_json FROM state_snapshots WHERE snapshot_id = "
            "(SELECT last_checkpoint FROM subject_identity WHERE subject_id = ?)",
            (subject_id,),
        ).fetchone()
        connection.execute(
            "UPDATE state_snapshots SET state_json = ? WHERE snapshot_id = ?",
            (row["state_json"].encode("utf-8"), row["snapshot_id"]),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("core.identity_continuity",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("core.identity_continuity:integrity_error",)
    finally:
        kernel.close()


def test_event_payload_json_blob_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-event-json-blob"
    kernel = _active_kernel(tmp_path, subject_id)
    with kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_event_immutable_update")
        row = connection.execute(
            "SELECT event_id, payload_json FROM events WHERE subject_id = ? ORDER BY rowid LIMIT 1",
            (subject_id,),
        ).fetchone()
        connection.execute(
            "UPDATE events SET payload_json = ? WHERE event_id = ?",
            (row["payload_json"].encode("utf-8"), row["event_id"]),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("core.event_payloads",),
        )

        assert report.status == "corrupt"
        assert report.checks[0].reason_code == "event_corruption"
        assert len(report.p0) == 1
        assert report.p0[0].startswith("core.event_payloads:event_hash:")
    finally:
        kernel.close()


def test_malformed_action_json_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-action-json-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    action = kernel.action_ledger.prepare(
        subject_id,
        "integrity-fixture",
        "fixture-tool",
        "fixture-target",
        {"bounded": True},
        resource_cost={"tokens": 1},
    )
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE actions SET resource_cost_json = '{' WHERE action_id = ?",
            (action.action_id,),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("core.actions",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("core.actions:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("column", "value"),
    (("retry_count", 0.5), ("side_effect", 0.5)),
)
def test_fractional_action_integer_state_is_p0(tmp_path: Path, column: str, value: float) -> None:
    subject_id = f"Noyra-p102-action-fractional-{column}"
    kernel = _active_kernel(tmp_path, subject_id)
    action = kernel.action_ledger.prepare(
        subject_id,
        "integrity-fixture",
        "fixture-tool",
        "fixture-target",
        {"bounded": True},
    )
    with kernel.database.transaction() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE actions SET {column} = ? WHERE action_id = ?",
            (value, action.action_id),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("core.actions",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("core.actions:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_core_provenance_registry_accepts_healthy_state(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-core-provenance-health"
    kernel = _active_kernel(tmp_path, subject_id)
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("core.actions",),
        )

        assert report.status == "ok"
        assert report.p0 == ()
        assert report.p1 == ()
        assert report.checks[0].details["training_policies"] == 1
        assert report.checks[0].details["training_records"] >= 1
    finally:
        kernel.close()


@pytest.mark.parametrize(
    "domain",
    ("audit_records", "training_policies", "training_records", "training_exports"),
)
def test_core_provenance_durable_domain_damage_is_p0(tmp_path: Path, domain: str) -> None:
    subject_id = f"Noyra-p102-core-provenance-{domain.replace('_', '-')}"
    kernel = _active_kernel(tmp_path, subject_id)
    with kernel.database.transaction() as connection:
        if domain == "audit_records":
            connection.execute(
                "INSERT INTO audit_records(audit_id, subject_id, action, actor, payload_json, "
                "occurred_at) VALUES (?, ?, 'runtime_exported', 'test', '{', ?)",
                ("audit_p102_corrupt", subject_id, "2026-08-17T00:00:00.000+00:00"),
            )
        elif domain == "training_policies":
            connection.execute(
                "UPDATE training_policies SET policy_version = 2 WHERE subject_id = ?",
                (subject_id,),
            )
        elif domain == "training_records":
            record = connection.execute(
                "SELECT record_id FROM training_records WHERE subject_id = ? "
                "ORDER BY created_at, record_id LIMIT 1",
                (subject_id,),
            ).fetchone()
            assert record is not None
            connection.execute(
                "UPDATE training_records SET updated_at = ? WHERE record_id = ?",
                ("2000-01-01T00:00:00.000+00:00", record["record_id"]),
            )
        else:
            connection.execute(
                """INSERT INTO training_exports(
                    export_id, subject_id, format, manifest_hash, row_count,
                    byte_size, consent_version, created_at
                ) VALUES (?, ?, 'jsonl-zip', ?, 0, 0, 1, ?)""",
                (
                    "training-export-p102-orphan",
                    subject_id,
                    content_hash({"manifest": "orphan"}),
                    "2026-08-17T00:00:00.000+00:00",
                ),
            )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("core.actions",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("core.actions:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


@pytest.mark.parametrize("damage", ("proposal", "model_response"))
def test_malformed_social_or_model_json_is_p0(tmp_path: Path, damage: str) -> None:
    subject_id = f"Noyra-p102-social-json-{damage.replace('_', '-')}"
    kernel = _active_kernel(tmp_path, subject_id)
    source_event = EventStore(kernel.database).append(
        subject_id,
        "integrity_fixture",
        "test",
        {"purpose": "social relationship"},
    )
    relationship = RelationshipStore(kernel.database).ensure(
        subject_id,
        "human",
        "founder",
        "Founder",
        source_event_ids=(source_event.event_id,),
    )
    call, _created = ModelLedger(kernel.database).prepare_call(
        subject_id,
        "fixture-provider",
        "fixture-model",
        "integrity-social",
        content_hash({"fixture": True}),
        "p102-social-json",
        request={"fixture": True},
    )
    proposal = {
        "disposition": "wait",
        "relationship_id": relationship.relationship_id,
        "topic": "wait safely",
        "rationale": "No interaction is needed for this fixture.",
        "content": None,
        "evidence_event_ids": [source_event.event_id],
    }
    created_at = datetime.now(UTC).isoformat(timespec="milliseconds")
    proposal_hash = content_hash(proposal)
    state_hash = content_hash(
        {
            "relationship_id": relationship.relationship_id,
            "model_call_id": call.call_id,
            "interaction_id": None,
            "disposition": "wait",
            "channel": "none",
            "counterparty": "founder",
            "topic": "wait safely",
            "rationale": "No interaction is needed for this fixture.",
            "proposal_hash": proposal_hash,
            "evidence_event_ids": [source_event.event_id],
            "created_at": created_at,
        }
    )
    with kernel.database.transaction() as connection:
        if damage == "model_response":
            connection.execute(
                "UPDATE model_calls SET status = 'succeeded', response_json = '{', "
                "response_hash = ?, completed_at = ? WHERE call_id = ?",
                (content_hash({}), created_at, call.call_id),
            )
        connection.execute(
            """INSERT INTO relationship_social_runs(
                social_id, subject_id, relationship_id, model_call_id, interaction_id,
                idempotency_key, disposition, channel, counterparty, topic, rationale,
                proposal_json, proposal_hash, evidence_event_ids_json, state_hash, created_at
            ) VALUES (?, ?, ?, ?, NULL, ?, 'wait', 'none', 'founder', 'wait safely',
                'No interaction is needed for this fixture.', ?, ?, ?, ?, ?)""",
            (
                "social-p102-invalid-json",
                subject_id,
                relationship.relationship_id,
                call.call_id,
                "p102-social-invalid-json",
                "{" if damage == "proposal" else json.dumps(proposal),
                proposal_hash,
                json.dumps([source_event.event_id]),
                state_hash,
                created_at,
            ),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("interaction.state",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("interaction.state:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("timeout_seconds", "bad"),
        ("timeout_seconds", "NaN"),
        ("timeout_seconds", "Inf"),
        ("dimensions", 128.5),
    ),
)
def test_malformed_embedding_resource_numbers_are_p0(
    tmp_path: Path, column: str, value: Any
) -> None:
    subject_id = f"Noyra-p102-embedding-number-{column}-{content_hash(value)[:8]}"
    kernel = _active_kernel(tmp_path, subject_id)
    store = EmbeddingResourceStore(kernel.database, tmp_path / "secrets" / "embedding")
    record = store.configure(
        subject_id,
        EmbeddingResourceInput(
            label="P1-02 embedding fixture",
            base_url="https://embeddings.example/v1",
            model="fixture-embedding",
            api_key=SecretStr("embedding-secret"),
            dimensions=128,
            timeout_seconds=30,
        ),
        actor="operator",
    )
    with kernel.database.transaction() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE embedding_resources SET {column} = ? WHERE config_id = ?",
            (value, record.config_id),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("model.embedding_resources",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("model.embedding_resources:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_fractional_model_resource_priority_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-model-resource-fractional"
    kernel = _active_kernel(tmp_path, subject_id)
    store = CognitiveResourceStore(kernel.database, tmp_path / "secrets" / "models")
    group = store.configure(
        subject_id,
        CognitiveResourceGroupInput(
            pool="economy",
            label="P1-02 numeric fixture",
            base_url="https://models.example/v1",
            model="fixture-model",
            api_keys=(SecretStr("model-secret"),),
        ),
        actor="operator",
    )
    with kernel.database.transaction() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE cognitive_resource_groups SET priority = 100.5 WHERE group_id = ?",
            (group.group_id,),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("model.resources",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("model.resources:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_model_resource_check_covers_cognitive_routing_history(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-routing-history-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    with kernel.database.transaction() as connection:
        connection.execute(
            "INSERT INTO cognitive_route_decisions(decision_id, subject_id, purpose, task_kind, "
            "selected_route, pool, group_id, key_id, importance, risk, ambiguity, reason_code, "
            "state_hash, created_at) VALUES (?, ?, ?, ?, 'wait', 'deep', NULL, NULL, "
            "0.5, 0.5, 0.5, 'fixture_wait', 'bad-hash', ?)",
            (
                "route-p102-corrupt",
                subject_id,
                "world_cognition:fixture",
                "world_cognition",
                now,
            ),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("model.resources",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("model.resources:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_model_resource_routing_history_streams_past_materialization_limit(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-routing-history-stream"
    kernel = _active_kernel(tmp_path, subject_id)
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    decisions: list[tuple[Any, ...]] = []
    outcomes: list[tuple[Any, ...]] = []
    for index in range(32):
        decision_id = f"route-p102-stream-{index}"
        decision_payload = {
            "decision_id": decision_id,
            "subject_id": subject_id,
            "purpose": f"world_cognition:stream-{index}",
            "task_kind": "world_cognition",
            "selected_route": "deep_model",
            "pool": "deep",
            "group_id": None,
            "key_id": None,
            "importance": 0.5,
            "risk": 0.8,
            "ambiguity": 0.4,
            "reason_code": "purpose_classified_locally",
            "created_at": now,
        }
        decisions.append((*decision_payload.values(), content_hash(decision_payload)))
        outcome_payload = {
            "subject_id": subject_id,
            "decision_id": decision_id,
            "outcome": "deferred",
            "result_changed_state": False,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_microusd": 0,
            "latency_ms": 0,
            "reason_code": "pool_not_configured",
            "created_at": now,
        }
        outcomes.append(
            (
                f"route-outcome-p102-stream-{index}",
                *outcome_payload.values(),
                content_hash(outcome_payload),
            )
        )
    with kernel.database.transaction() as connection:
        connection.executemany(
            "INSERT INTO cognitive_route_decisions(decision_id, subject_id, purpose, task_kind, "
            "selected_route, pool, group_id, key_id, importance, risk, ambiguity, reason_code, "
            "created_at, state_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            decisions,
        )
        connection.executemany(
            "INSERT INTO cognitive_route_outcomes(outcome_id, subject_id, decision_id, outcome, "
            "result_changed_state, input_tokens, output_tokens, cost_microusd, latency_ms, "
            "reason_code, created_at, state_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            outcomes,
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            limits=IntegrityAuditLimits(max_rows_per_check=16),
            check_ids=("model.resources",),
        )

        assert report.status == "ok"
        assert report.p0 == ()
        assert report.p1 == ()
        assert report.checks[0].details["cognitive_route_decisions"] == 32
        assert report.checks[0].details["cognitive_route_outcomes"] == 32
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("table", "trigger", "id_column"),
    (
        (
            "cognitive_resource_group_revisions",
            "prevent_cognitive_resource_group_revision_update",
            "revision_id",
        ),
        (
            "cognitive_resource_key_events",
            "prevent_cognitive_resource_key_event_update",
            "event_id",
        ),
    ),
)
def test_model_resource_history_tampering_is_p0(
    tmp_path: Path, table: str, trigger: str, id_column: str
) -> None:
    subject_id = f"Noyra-p102-model-history-{table}"
    kernel = _active_kernel(tmp_path, subject_id)
    store = CognitiveResourceStore(kernel.database, tmp_path / "secrets" / "models")
    store.configure(
        subject_id,
        CognitiveResourceGroupInput(
            pool="economy",
            label="P1-02 history fixture",
            base_url="https://models.example/v1",
            model="fixture-model",
            api_keys=(SecretStr("model-history-secret"),),
        ),
        actor="operator",
    )
    with kernel.database.transaction() as connection:
        connection.execute(f'DROP TRIGGER "{trigger}"')
        row_id = connection.execute(f'SELECT "{id_column}" FROM "{table}"').fetchone()[0]
        connection.execute(
            f'UPDATE "{table}" SET state_hash = ? WHERE "{id_column}" = ?',
            ("0" * 64, row_id),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("model.resources",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("model.resources:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_model_resource_key_event_reverse_subject_binding_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-model-event-owner"
    other_subject = "Noyra-p102-model-event-foreign"
    kernel = _active_kernel(tmp_path, subject_id)
    IdentityStore(kernel.database).ensure(other_subject, content_hash({"subject": other_subject}))
    store = CognitiveResourceStore(kernel.database, tmp_path / "secrets" / "models")
    foreign_group = store.configure(
        other_subject,
        CognitiveResourceGroupInput(
            pool="economy",
            label="P1-02 foreign event fixture",
            base_url="https://foreign-models.example/v1",
            model="foreign-fixture-model",
            api_keys=(SecretStr("foreign-model-history-secret"),),
        ),
        actor="operator",
    )
    foreign_key = store.keys(foreign_group.group_id, subject_id=other_subject)[0]
    with kernel.database.transaction() as connection:
        connection.execute('DROP TRIGGER "prevent_cognitive_resource_key_event_update"')
        event = dict(
            connection.execute(
                "SELECT * FROM cognitive_resource_key_events WHERE key_id = ?",
                (foreign_key.key_id,),
            ).fetchone()
        )
        payload = {
            "key_id": event["key_id"],
            "subject_id": subject_id,
            "event_type": event["event_type"],
            "reason_code": event["reason_code"],
            "created_at": event["created_at"],
        }
        connection.execute(
            "UPDATE cognitive_resource_key_events SET subject_id = ?, state_hash = ? "
            "WHERE event_id = ?",
            (subject_id, content_hash(payload), event["event_id"]),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("model.resources",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("model.resources:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_orphan_fatigue_transition_is_p0_without_current_state(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-orphan-fatigue-history"
    kernel = _active_kernel(tmp_path, subject_id)
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    state_hash = content_hash(
        {
            "fatigue": 0.0,
            "mode": "active",
            "resource_pressure": 0.0,
            "cognitive_load": 0.0,
            "frustration": 0.0,
            "goal_conflict": 0.0,
            "staleness": 0.0,
        }
    )
    with kernel.database.transaction() as connection:
        connection.execute("DELETE FROM fatigue_states WHERE subject_id = ?", (subject_id,))
        connection.execute(
            "INSERT INTO fatigue_transitions(transition_id, subject_id, old_fatigue, "
            "new_fatigue, mode, resource_pressure, cognitive_load, frustration, goal_conflict, "
            "staleness, reason, state_hash, created_at) "
            "VALUES (?, ?, 0, 0, 'active', 0, 0, 0, 0, 0, ?, ?, ?)",
            ("fat-p102-orphan", subject_id, "orphan history fixture", state_hash, now),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("sleep.state",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("sleep.state:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_orphan_observation_content_segment_is_p0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject_id = "Noyra-p102-orphan-observation-segment"
    kernel = _active_kernel(tmp_path, subject_id)
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"o" * 32).decode("ascii"),
    )
    segment_id = "observation-segment-p102-orphan"
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    with kernel.database.transaction() as connection:
        connection.execute(
            "INSERT INTO observation_content_segments(segment_id, subject_id, object_key, "
            "first_fetched_at, last_fetched_at, observation_count, compressed_hash, "
            "archive_format, encryption_key_id, encryption_key_fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, NULL, NULL, ?)",
            (
                segment_id,
                subject_id,
                f"observations/{segment_id}.json.zlib.enc",
                now,
                now,
                "0" * 64,
                "noyra-observation-content-segment-v1",
                now,
            ),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("world.state",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("world.state:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


@pytest.mark.parametrize("attempts", (0.5, b"0"))
def test_malformed_transport_delivery_attempts_are_p0(tmp_path: Path, attempts: Any) -> None:
    subject_id = f"Noyra-p102-delivery-attempts-{content_hash(repr(attempts))[:8]}"
    kernel = _active_kernel(tmp_path, subject_id)
    transport = TransportStore(kernel.database, tmp_path / "secrets" / "transports").configure(
        subject_id,
        TransportInput(
            channel="webhook",
            label="P1-02 delivery fixture",
            endpoint="https://example.com/hook",
            credentials={"token": SecretStr("transport-secret")},
        ),
        actor="operator",
    )
    interaction = InteractionStore(kernel.database).send(
        subject_id,
        "webhook",
        "example.com",
        "Bounded delivery integrity fixture.",
    )
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    with kernel.database.transaction() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "INSERT INTO interaction_deliveries(delivery_id, interaction_id, subject_id, "
            "transport_id, idempotency_key, status, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
            (
                "delivery-p102-invalid-attempts",
                interaction.interaction_id,
                subject_id,
                transport.transport_id,
                "p102-delivery-invalid-attempts",
                attempts,
                now,
                now,
            ),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("interaction.transport",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("interaction.transport:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_malformed_memory_block_version_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-memory-block-version-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    block = MemoryBlockStore(kernel.database).create(
        subject_id,
        "working_context",
        "Integrity fixture",
        "A bounded memory block.",
        reason="P1-02 classification fixture",
    )
    with kernel.database.transaction() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE memory_blocks SET version = 'bad' WHERE block_id = ?",
            (block.block_id,),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("mind.memory_blocks",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("mind.memory_blocks:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_malformed_world_source_trust_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-world-source-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    source = SourceRegistry(kernel.database).register(
        subject_id,
        "Integrity source",
        "https://example.com/integrity",
        "news",
        trust_score=0.7,
        status="active",
        reason="P1-02 classification fixture",
    )
    with kernel.database.transaction() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE world_sources SET trust_score = 'bad' WHERE source_id = ?",
            (source.source_id,),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("world.state",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("world.state:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_malformed_capability_scope_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-capability-scope-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    grant = CapabilityStore(kernel.database).grant(
        subject_id,
        CapabilityGrant(
            capability_type="web_read",
            scope={"hosts": ["example.com"]},
            issuer="workspace-owner",
            rate_limit_per_hour=10,
            side_effect=False,
        ),
        actor="operator",
    )
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE capability_grants SET scope_json = '{' WHERE grant_id = ?",
            (grant.grant_id,),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("capability.state",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("capability.state:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_capability_scope_json_blob_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-capability-scope-blob"
    kernel = _active_kernel(tmp_path, subject_id)
    grant = CapabilityStore(kernel.database).grant(
        subject_id,
        CapabilityGrant(
            capability_type="web_read",
            scope={"hosts": ["example.com"]},
            issuer="workspace-owner",
            rate_limit_per_hour=10,
            side_effect=False,
        ),
        actor="operator",
    )
    with kernel.database.transaction() as connection:
        row = connection.execute(
            "SELECT scope_json FROM capability_grants WHERE grant_id = ?",
            (grant.grant_id,),
        ).fetchone()
        connection.execute(
            "UPDATE capability_grants SET scope_json = ? WHERE grant_id = ?",
            (row["scope_json"].encode("utf-8"), grant.grant_id),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("capability.state",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("capability.state:integrity_error",)
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("rate_limit_per_hour", 10.5),
        ("side_effect", 2),
        ("side_effect", 0.5),
        ("requires_approval", 0.5),
    ),
)
def test_malformed_capability_numbers_are_p0(tmp_path: Path, column: str, value: Any) -> None:
    subject_id = f"Noyra-p102-capability-number-{column}-{content_hash(value)[:8]}"
    kernel = _active_kernel(tmp_path, subject_id)
    grant = CapabilityStore(kernel.database).grant(
        subject_id,
        CapabilityGrant(
            capability_type="web_read",
            scope={"hosts": ["example.com"]},
            issuer="workspace-owner",
            rate_limit_per_hour=10,
            side_effect=True,
        ),
        actor="operator",
    )
    with kernel.database.transaction() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE capability_grants SET {column} = ? WHERE grant_id = ?",
            (value, grant.grant_id),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("capability.state",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("capability.state:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_malformed_consciousness_sequence_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-consciousness-sequence-corrupt"
    kernel = _active_kernel(tmp_path, subject_id)
    frame = ConsciousnessFrameStore(kernel.database, subject_id).ensure_initial()
    with kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_consciousness_frame_update")
        connection.execute(
            "UPDATE consciousness_frames SET sequence_number = 'bad' WHERE frame_id = ?",
            (frame.frame_id,),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("cognition.consciousness",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("cognition.consciousness:integrity_error",)
        assert report.p1 == ()
    finally:
        kernel.close()


def test_model_resource_programming_type_error_is_not_false_p0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject_id = "Noyra-p102-model-programming-error"
    kernel = _active_kernel(tmp_path, subject_id)

    def broken_verifier(_store: Any, _subject_id: str) -> dict[str, int]:
        raise TypeError("model verifier programming fixture")

    monkeypatch.setattr(CognitiveResourceStore, "verify_integrity", broken_verifier)
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("model.resources",),
        )

        assert report.status == "incomplete"
        assert report.p0 == ()
        assert report.p1 == ("model.resources:checker_runtime",)
    finally:
        kernel.close()


def test_malformed_snapshot_compression_with_matching_hash_is_p0(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-invalid-zlib"
    kernel = _active_kernel(tmp_path, subject_id)
    compressed = b"not-a-zlib-stream"
    with kernel.database.transaction() as connection:
        connection.execute(
            "INSERT INTO snapshot_archives(archive_id, subject_id, first_version, last_version, "
            "snapshot_count, compressed_payload, payload_hash, compressed_hash, created_at) "
            "VALUES (?, ?, 1, 1, 1, ?, ?, ?, ?)",
            (
                "snapshot-archive-invalid-zlib",
                subject_id,
                compressed,
                content_hash({"entries": []}),
                content_hash({"compressed_hex": compressed.hex()}),
                datetime.now(UTC).isoformat(timespec="milliseconds"),
            ),
        )
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            check_ids=("core.snapshot_archives",),
        )

        assert report.status == "corrupt"
        assert report.p0 == ("core.snapshot_archives:integrity_error",)
    finally:
        kernel.close()


def test_snapshot_decompression_uses_the_remaining_external_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject_id = "Noyra-p102-snapshot-remaining-budget"
    kernel = _active_kernel(tmp_path, subject_id)
    payload: dict[str, list[dict[str, Any]]] = {
        "entries": [{"base_state": {}, "state_hash": content_hash({})}]
    }
    decoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    compressed = zlib.compress(decoded)
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    with kernel.database.transaction() as connection:
        for index in range(2):
            connection.execute(
                "INSERT INTO snapshot_archives(archive_id, subject_id, first_version, "
                "last_version, "
                "snapshot_count, compressed_payload, payload_hash, compressed_hash, created_at) "
                "VALUES (?, ?, 1, 1, 1, ?, ?, ?, ?)",
                (
                    f"snapshot-archive-budget-{index}",
                    subject_id,
                    compressed,
                    content_hash(payload),
                    content_hash({"compressed_hex": compressed.hex()}),
                    now,
                ),
            )

    real_factory = zlib.decompressobj
    decompress_limits: list[int] = []
    flush_limits: list[int] = []

    class TrackingDecompressor:
        def __init__(self) -> None:
            self.inner: Any = real_factory()

        @property
        def unconsumed_tail(self) -> bytes:
            return bytes(self.inner.unconsumed_tail)

        @property
        def eof(self) -> bool:
            return bool(self.inner.eof)

        @property
        def unused_data(self) -> bytes:
            return bytes(self.inner.unused_data)

        def decompress(self, data: bytes, max_length: int) -> bytes:
            decompress_limits.append(max_length)
            return bytes(self.inner.decompress(data, max_length))

        def flush(self, length: int) -> bytes:
            flush_limits.append(length)
            return bytes(self.inner.flush(length))

    monkeypatch.setattr(zlib, "decompressobj", TrackingDecompressor)
    byte_limit = len(decoded) * 2 + 100_000
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=5,
            limits=IntegrityAuditLimits(
                max_bytes_per_check=byte_limit,
                max_value_bytes=byte_limit,
            ),
            check_ids=("core.snapshot_archives",),
        )

        assert report.status == "ok"
        assert decompress_limits == [byte_limit + 1, byte_limit - len(decoded) + 1]
        assert flush_limits == [
            byte_limit - len(decoded) + 1,
            byte_limit - (2 * len(decoded)) + 1,
        ]
    finally:
        kernel.close()


def test_invalid_or_oversized_watchdog_state_falls_back_to_the_hashed_report(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p102-state-fallback"
    kernel = _active_kernel(tmp_path, subject_id)
    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.state_fallback",
                1,
                "test",
                frozenset({"startup_light"}),
                lambda _context: IntegrityCheckOutcome("corrupt", "p0", "state_fixture"),
                "light",
            ),
        )
    )
    controller = _controller(kernel, tmp_path, registry=registry)
    try:
        report = controller.run_startup()
        assert report is not None and report.status == "corrupt"
        state_path = controller._state_path
        digest_path = state_path.with_suffix(".json.sha256")
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["format_version"] = "invalid-watchdog-format"
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        digest_path.write_text(content_hash(payload), encoding="ascii")

        wrong_format = _controller(kernel, tmp_path, registry=registry)

        assert wrong_format.latest_report is not None
        assert wrong_format.summary()["status"] == "corrupt"
        assert wrong_format.summary()["p0"] == 1

        state_path.write_bytes(b" " * 4_000_001)
        digest_path.write_text("0" * 64, encoding="ascii")
        oversized = _controller(kernel, tmp_path, registry=registry)

        assert oversized.latest_report is not None
        assert oversized.summary()["status"] == "corrupt"
        assert oversized.summary()["p0"] == 1
    finally:
        kernel.close()


def test_large_storage_files_are_metadata_only_for_the_byte_budget(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-storage-metadata"
    kernel = _active_kernel(tmp_path, subject_id)
    large_path = tmp_path / "subject" / "large-sparse.bin"
    large_path.parent.mkdir(parents=True, exist_ok=True)
    with large_path.open("wb") as stream:
        stream.seek(128_000_000 - 1)
        stream.write(b"\0")
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            limits=IntegrityAuditLimits(
                max_rows_per_check=1_000,
                max_bytes_per_check=4_096,
                max_value_bytes=4_096,
                max_files_per_check=1_000,
            ),
            check_ids=("core.storage_boundary",),
        )

        assert report.status == "ok"
        assert report.checks[0].bytes_examined == 0
        assert int(report.checks[0].details["subject_bytes"]) >= 128_000_000
    finally:
        kernel.close()


def test_storage_scan_errors_are_p1_instead_of_partial_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject_id = "Noyra-p102-storage-error"
    kernel = _active_kernel(tmp_path, subject_id)
    blocked = tmp_path / "subject" / "blocked"
    blocked.mkdir(parents=True)
    original_scandir = os.scandir

    def guarded_scandir(path: Any) -> Any:
        if Path(path).resolve() == blocked.resolve():
            raise PermissionError("storage fixture")
        return original_scandir(path)

    monkeypatch.setattr("noyra.core.integrity.os.scandir", guarded_scandir)
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("core.storage_boundary",),
        )

        assert report.status == "degraded"
        assert report.p1 == ("core.storage_boundary:resource_unavailable",)
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("damage", "expected_status", "expected_reason"),
    (
        ("missing", "degraded", "resource_unavailable"),
        ("malformed", "corrupt", "integrity_error"),
        ("oversized", "degraded", "value_byte_limit"),
    ),
)
def test_transport_secret_verification_is_bounded_and_classified(
    tmp_path: Path, damage: str, expected_status: str, expected_reason: str
) -> None:
    subject_id = f"Noyra-p102-transport-{damage}"
    kernel = _active_kernel(tmp_path, subject_id)
    secret_dir = tmp_path / "secrets" / "transports"
    store = TransportStore(kernel.database, secret_dir)
    record = store.configure(
        subject_id,
        TransportInput(
            channel="webhook",
            label="P1-02 fixture",
            endpoint="https://example.com/hook",
            credentials={"token": SecretStr("transport-secret")},
        ),
        actor="operator",
    )
    secret_path = secret_dir / f"{record.transport_id}.json"
    if damage == "missing":
        secret_path.unlink()
    elif damage == "malformed":
        secret_path.write_bytes(b"{")
    else:
        secret_path.write_bytes(b"x" * 4_097)
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            limits=IntegrityAuditLimits(max_bytes_per_check=16_384, max_value_bytes=4_096),
            check_ids=("interaction.transport",),
        )

        assert report.status == expected_status
        assert report.checks[0].reason_code == expected_reason
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("damage", "expected_status", "expected_reason"),
    (
        ("missing", "degraded", "resource_unavailable"),
        ("tampered", "corrupt", "integrity_error"),
        ("oversized", "degraded", "value_byte_limit"),
    ),
)
def test_model_resource_keys_are_actually_verified(
    tmp_path: Path, damage: str, expected_status: str, expected_reason: str
) -> None:
    subject_id = f"Noyra-p102-model-secret-{damage}"
    kernel = _active_kernel(tmp_path, subject_id)
    secret_dir = tmp_path / "secrets" / "models"
    store = CognitiveResourceStore(kernel.database, secret_dir)
    group = store.configure(
        subject_id,
        CognitiveResourceGroupInput(
            pool="economy",
            label="P1-02 fixture",
            base_url="https://models.example/v1",
            model="fixture-model",
            api_keys=(SecretStr("model-secret"),),
        ),
        actor="operator",
    )
    key_id = store.keys(group.group_id, subject_id=subject_id)[0].key_id
    secret_path = secret_dir / f"{key_id}.key"
    if damage == "missing":
        secret_path.unlink()
    elif damage == "tampered":
        secret_path.write_text("wrong-secret", encoding="utf-8")
    else:
        secret_path.write_bytes(b"x" * 4_097)
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            limits=IntegrityAuditLimits(max_bytes_per_check=16_384, max_value_bytes=4_096),
            check_ids=("model.resources",),
        )

        assert report.status == expected_status
        assert report.checks[0].reason_code == expected_reason
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("damage", "expected_status", "expected_reason"),
    (
        ("missing", "degraded", "resource_unavailable"),
        ("oversized", "corrupt", "integrity_error"),
    ),
)
def test_common_knowledge_private_key_reads_are_bounded(
    tmp_path: Path, damage: str, expected_status: str, expected_reason: str
) -> None:
    subject_id = f"Noyra-p102-common-key-{damage}"
    kernel = _active_kernel(tmp_path, subject_id)
    key_dir = tmp_path / "secrets" / "common-knowledge"
    store = CommonKnowledgeStore(kernel.database, subject_id, key_dir)
    store.publish(
        CommonKnowledgeProposal(
            scope="protocol",
            title="P1-02 bounded verification",
            summary="A bounded integrity verification fixture.",
            payload={
                "procedure": ["inspect bounded input"],
                "compatibility": ["integrity-v1"],
                "validation": ["classified result"],
                "tags": ["integrity"],
            },
        )
    )
    key_path = key_dir / "common-knowledge-ed25519.key"
    if damage == "missing":
        key_path.unlink()
    else:
        key_path.write_bytes(b"x" * 33)
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("knowledge.common",),
        )

        assert report.status == expected_status
        assert report.checks[0].reason_code == expected_reason
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("policy_mode", "expected_calls"),
    (("pause", []), ("alert", ["sync", "bootstrap"])),
)
def test_sleep_state_startup_findings_suppress_only_pause_mode_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy_mode: Literal["alert", "pause"],
    expected_calls: list[str],
) -> None:
    data_dir = tmp_path / policy_mode
    subject_id = f"Noyra-p102-sleep-boot-{policy_mode}"
    settings = ServiceSettings(
        data_dir=data_dir,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
        integrity_mode=policy_mode,
    )
    seed = NoyraService(settings)
    try:
        seed.boot()
        seed.kernel.lifecycle.transition("winding_down", "P1-02 startup sleep fixture")
        seed.kernel.lifecycle.transition("reflective_sleep", "P1-02 startup sleep fixture")
        seed.kernel.lifecycle.transition("deep_sleep", "P1-02 startup sleep fixture")
    finally:
        seed.http.close()
        seed.kernel.close()

    service = NoyraService(settings)
    calls: list[str] = []

    class CognitionStub:
        def bootstrap(self) -> None:
            calls.append("bootstrap")

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.sleep_startup",
                1,
                "test",
                frozenset({"startup_light"}),
                lambda _context: IntegrityCheckOutcome("corrupt", "p0", "sleep_boot_fixture"),
                "light",
            ),
        )
    )
    service.integrity = IntegrityRuntimeController(
        service.kernel,
        data_dir,
        policy_mode=policy_mode,
        registry=registry,
    )
    service.http.integrity = service.integrity
    service.loop.pre_tick_hook = service._integrity_pre_tick
    service.loop.next_wakeup_hook = service.integrity.seconds_until_due
    service.cognition = CognitionStub()  # type: ignore[assignment]
    monkeypatch.setattr(service, "_sync_training_policy", lambda: calls.append("sync"))
    try:
        service.boot()

        assert service.kernel.lifecycle.current().state == "deep_sleep"
        assert calls == expected_calls
        assert service.integrity.latest_report is not None
        expected_action = "pause_deferred" if policy_mode == "pause" else "alert_recorded"
        assert service.integrity.latest_report.action["result"] == expected_action
    finally:
        service.http.close()
        service.kernel.close()


@pytest.mark.parametrize(
    "sleep_state",
    ("winding_down", "reflective_sleep", "deep_sleep", "waking"),
)
def test_pause_pending_quarantines_all_sleep_progression(tmp_path: Path, sleep_state: str) -> None:
    data_dir = tmp_path / sleep_state
    subject_id = f"Noyra-p102-sleep-quarantine-{sleep_state}"
    settings = ServiceSettings(
        data_dir=data_dir,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
        integrity_mode="pause",
    )
    seed = NoyraService(settings)
    try:
        seed.boot()
        seed.kernel.lifecycle.transition("winding_down", "P1-02 sleep quarantine fixture")
        if sleep_state != "winding_down":
            seed.kernel.lifecycle.transition("reflective_sleep", "P1-02 sleep quarantine fixture")
        if sleep_state in {"deep_sleep", "waking"}:
            seed.kernel.lifecycle.transition("deep_sleep", "P1-02 sleep quarantine fixture")
        if sleep_state == "waking":
            seed.kernel.lifecycle.transition("waking", "P1-02 sleep quarantine fixture")
    finally:
        seed.http.close()
        seed.kernel.close()

    service = NoyraService(settings)
    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.sleep_quarantine",
                1,
                "test",
                frozenset({"startup_light", "periodic_deep"}),
                lambda _context: IntegrityCheckOutcome("corrupt", "p0", "sleep_quarantine_fixture"),
                "light",
            ),
        )
    )
    service.integrity = IntegrityRuntimeController(
        service.kernel,
        data_dir,
        policy_mode="pause",
        registry=registry,
    )
    service.http.integrity = service.integrity
    service.loop.pre_tick_hook = service._integrity_pre_tick
    service.loop.next_wakeup_hook = service.integrity.seconds_until_due
    advance_calls = 0

    async def unexpected_advance(_lifecycle: str) -> TickResult:
        nonlocal advance_calls
        advance_calls += 1
        raise AssertionError("sleep progression must be quarantined")

    service.loop._advance_sleep = unexpected_advance  # type: ignore[assignment]
    try:
        service.boot()
        result = asyncio.run(service.loop.tick())

        assert result.action == "integrity_startup_quarantine"
        assert advance_calls == 0
        assert service.kernel.lifecycle.current().state == sleep_state
    finally:
        service.http.close()
        service.kernel.close()


def _blocked_service_fixture(
    tmp_path: Path, subject_id: str
) -> tuple[NoyraService, threading.Event, threading.Event]:
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
        active_interval_seconds=1,
        sleep_interval_seconds=1,
        integrity_interval_seconds=1,
    )
    service = NoyraService(settings)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def blocking_runner(_context: Any) -> IntegrityCheckOutcome:
        nonlocal calls
        calls += 1
        if calls == 1:
            return IntegrityCheckOutcome()
        entered.set()
        release.wait()
        return IntegrityCheckOutcome()

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.blocked_worker",
                1,
                "test",
                frozenset({"startup_light", "periodic_deep"}),
                blocking_runner,
                "light",
            ),
        )
    )
    service.integrity = IntegrityRuntimeController(
        service.kernel,
        tmp_path,
        policy_mode="alert",
        interval_seconds=0.01,
        registry=registry,
    )
    service.http.integrity = service.integrity
    service.loop.pre_tick_hook = service._integrity_pre_tick
    service.loop.next_wakeup_hook = service.integrity.seconds_until_due
    return service, entered, release


async def _wait_for_worker_entry(
    entered: threading.Event,
    service_task: asyncio.Task[Any],
    *,
    timeout: float = 20.0,
) -> None:
    """Wait without occupying the executor needed by the worker under test."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not entered.is_set():
        if service_task.done():
            # Surface a real startup failure instead of misreporting it as an
            # event timeout.
            await service_task
            raise AssertionError("service stopped before integrity worker entry")
        if loop.time() >= deadline:
            raise AssertionError("integrity worker did not enter before timeout")
        await asyncio.sleep(0.05)


def test_service_cancellation_joins_integrity_worker_before_releasing_ownership(
    tmp_path: Path,
) -> None:
    service, entered, release = _blocked_service_fixture(tmp_path, "Noyra-p102-cancel-worker")

    async def exercise() -> None:
        task = asyncio.create_task(service.run())
        try:
            await _wait_for_worker_entry(entered, task)
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done()
            assert service.kernel.process_lock.held
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not service.kernel.process_lock.held
            report_path = service.integrity._latest_path
            modified_at = report_path.stat().st_mtime_ns
            await asyncio.sleep(0.05)
            assert report_path.stat().st_mtime_ns == modified_at
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(exercise())


def test_graceful_shutdown_waits_for_integrity_worker_before_cleanup(tmp_path: Path) -> None:
    service, entered, release = _blocked_service_fixture(tmp_path, "Noyra-p102-graceful-worker")
    active_calls = 0

    async def active_hook() -> str:
        nonlocal active_calls
        active_calls += 1
        return "unexpected_active_work"

    service.loop.active_hook = active_hook

    async def exercise() -> None:
        task = asyncio.create_task(service.run())
        try:
            await _wait_for_worker_entry(entered, task)
            service.request_shutdown()
            await asyncio.sleep(0.05)
            assert not task.done()
            assert service.kernel.process_lock.held
            release.set()
            await asyncio.wait_for(task, timeout=5)
            assert not service.kernel.process_lock.held
            assert active_calls == 0
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(exercise())


def test_pause_mode_shutdown_cancellation_is_not_an_integrity_finding(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-shutdown-not-finding"
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
        integrity_mode="pause",
        active_interval_seconds=1,
        sleep_interval_seconds=1,
        integrity_interval_seconds=1,
    )
    service = NoyraService(settings)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def cooperative_runner(context: Any) -> IntegrityCheckOutcome:
        nonlocal calls
        calls += 1
        if calls == 1:
            return IntegrityCheckOutcome()
        entered.set()
        while not release.wait(0.01):
            context.checkpoint()
        return IntegrityCheckOutcome()

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.shutdown_cancel",
                1,
                "test",
                frozenset({"startup_light", "periodic_deep"}),
                cooperative_runner,
                "light",
            ),
        )
    )
    service.integrity = IntegrityRuntimeController(
        service.kernel,
        tmp_path,
        policy_mode="pause",
        interval_seconds=0.01,
        registry=registry,
        checkpoint=service._integrity_checkpoint,
    )
    service.http.integrity = service.integrity
    service.loop.pre_tick_hook = service._integrity_pre_tick
    service.loop.next_wakeup_hook = service.integrity.seconds_until_due

    async def exercise() -> None:
        task = asyncio.create_task(service.run())
        try:
            await _wait_for_worker_entry(entered, task)
            service.request_shutdown()
            await asyncio.wait_for(task, timeout=5)

            assert service.kernel.lifecycle.current().state == "active"
            assert service.integrity.latest_report is not None
            assert service.integrity.latest_report.status == "ok"
            assert service.integrity.latest_report.findings == ()
            assert service.integrity.summary()["p0"] == 0
            assert service.integrity.summary()["p1"] == 0
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(exercise())


def test_sql_progress_shutdown_is_not_an_integrity_finding(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-sql-shutdown-not-finding"
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
        integrity_mode="pause",
        active_interval_seconds=1,
        sleep_interval_seconds=1,
        integrity_interval_seconds=1,
    )
    service = NoyraService(settings)
    entered = threading.Event()
    calls = 0

    def sql_runner(context: Any) -> IntegrityCheckOutcome:
        nonlocal calls
        calls += 1
        if calls == 1:
            return IntegrityCheckOutcome()
        entered.set()
        context.connection.execute(
            "WITH RECURSIVE sequence(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM sequence WHERE value < 100000000"
            ") SELECT SUM(value) FROM sequence"
        ).fetchone()
        return IntegrityCheckOutcome()

    registry = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.sql_shutdown",
                1,
                "test",
                frozenset({"startup_light", "periodic_deep"}),
                sql_runner,
                "light",
            ),
        )
    )
    service.integrity = IntegrityRuntimeController(
        service.kernel,
        tmp_path,
        policy_mode="pause",
        interval_seconds=0.01,
        registry=registry,
        checkpoint=service._integrity_checkpoint,
    )
    service.http.integrity = service.integrity
    service.loop.pre_tick_hook = service._integrity_pre_tick
    service.loop.next_wakeup_hook = service.integrity.seconds_until_due

    async def exercise() -> None:
        task = asyncio.create_task(service.run())
        try:
            await _wait_for_worker_entry(entered, task)
            service.request_shutdown()
            await asyncio.wait_for(task, timeout=5)

            assert service.kernel.lifecycle.current().state == "active"
            assert service.integrity.latest_report is not None
            assert service.integrity.latest_report.status == "ok"
            assert service.integrity.latest_report.findings == ()
            assert service.integrity.summary()["p0"] == 0
            assert service.integrity.summary()["p1"] == 0
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(exercise())


def test_active_tick_stops_before_work_after_shutdown_request(tmp_path: Path) -> None:
    subject_id = "Noyra-p102-active-shutdown-guard"
    service = NoyraService(
        ServiceSettings(
            data_dir=tmp_path,
            subject_id=subject_id,
            genesis_hash=content_hash({"subject": subject_id}),
            host="127.0.0.1",
            port=0,
        )
    )
    storage_calls = 0

    def maintain() -> Any:
        nonlocal storage_calls
        storage_calls += 1
        raise AssertionError("storage work must not start after shutdown")

    cast(Any, service.storage_lifecycle).maintain = maintain
    service.request_shutdown()
    try:
        assert asyncio.run(service._active_tick()) == "shutdown_pending"
        assert storage_calls == 0
    finally:
        service.http.close()
        service.kernel.close()
