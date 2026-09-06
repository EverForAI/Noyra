from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from noyra.core import Database, IdentityStore, TrainingStore
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.model import (
    BudgetLimits,
    CognitiveResourceGroupInput,
    CognitiveResourceGroupUpdate,
    CognitiveResourceStore,
    CompletionRequest,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelMessage,
    ModelPricing,
    ModelRuntimeSettings,
    ModelUsage,
    OpenAICompatibleProvider,
    OpenAICompatibleSettings,
    ProviderResponse,
    RetryPolicy,
    RoutedModelGateway,
)
from noyra.model.errors import (
    BudgetExhaustedError,
    ConfigurationError,
    ModelCallConflictError,
    ProviderCallError,
    StructuredOutputError,
)
from noyra.model.resources import MAX_USD, SQLITE_INT64_MAX


class Insight(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


def insight_response(
    *,
    summary: str = "A durable observation",
    input_tokens: int = 20,
    output_tokens: int = 10,
) -> ProviderResponse:
    return ProviderResponse(
        content=json.dumps({"summary": summary, "confidence": 0.8}),
        usage=ModelUsage(input_tokens, output_tokens),
        finish_reason="stop",
        provider_request_id="provider-request-1",
    )


class ModelTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "noyra.sqlite3"
        self.database = Database(self.db_path)
        self.subject_id = "Noyra-model-test"
        IdentityStore(self.database).ensure(self.subject_id, content_hash({"seed": "model-test"}))
        self.ledger = ModelLedger(self.database)
        self.limits = BudgetLimits(
            daily_attempts=10,
            daily_input_tokens=10_000,
            daily_output_tokens=10_000,
            daily_cost_microusd=1_000_000,
        )
        self.messages = [ModelMessage(role="user", content="Observe the world.")]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def gateway(
        self,
        provider: FakeProvider,
        *,
        limits: BudgetLimits | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Any = None,
        capture_model_io_getter: Any = None,
    ) -> ModelGateway:
        kwargs: dict[str, Any] = {}
        if sleep is not None:
            kwargs["sleep"] = sleep
        if capture_model_io_getter is not None:
            kwargs["capture_model_io_getter"] = capture_model_io_getter
        return ModelGateway(
            provider,
            self.ledger,
            model="test-model",
            limits=limits or self.limits,
            pricing=ModelPricing(
                input_microusd_per_million=5_000_000,
                output_microusd_per_million=10_000_000,
            ),
            retry_policy=retry_policy or RetryPolicy(base_delay_seconds=0),
            random_source=lambda: 0,
            **kwargs,
        )

    def routing_fixture(self, *, include_live_state: bool = False) -> dict[str, Any]:
        store = CognitiveResourceStore(self.database, Path(self.temp_dir.name) / "routing-secrets")
        group = store.configure(
            self.subject_id,
            CognitiveResourceGroupInput(
                pool="economy",
                label="routing-integrity",
                base_url="https://routing.example/v1",
                model="routing-model",
                api_keys=(SecretStr("routing-integrity-key"),),
            ),
            actor="operator",
        )
        key = store.keys(group.group_id, subject_id=self.subject_id)[0]
        gateway = RoutedModelGateway(self.database, self.subject_id, store)
        decision = gateway._record_decision(
            "world_cognition:routing-integrity",
            "economy",
            selected_route="economy_model",
            group_id=None,
            key_id=None,
            reason_code="purpose_classified_locally",
        )
        gateway._record_attempt(
            decision.decision_id,
            group.group_id,
            key.key_id,
            1,
            "selected",
            "resource_selected",
            0,
        )
        gateway._record_attempt(
            decision.decision_id,
            group.group_id,
            key.key_id,
            1,
            "succeeded",
            "provider_call_succeeded",
            5,
        )
        gateway._finish_decision(
            decision.decision_id,
            "succeeded",
            "resource_pool_succeeded",
            latency_ms=5,
        )
        gateway._record_unavailable(
            "economy", "world_cognition:resolved-wait", "pool_not_configured"
        )
        gateway._resolve_wait("economy", "world_cognition:resolved-wait")
        live_decision = None
        if include_live_state:
            live_decision = gateway._record_decision(
                "world_cognition:live-routing",
                "economy",
                selected_route="economy_model",
                group_id=None,
                key_id=None,
                reason_code="purpose_classified_locally",
            )
            gateway._record_attempt(
                live_decision.decision_id,
                group.group_id,
                key.key_id,
                1,
                "selected",
                "resource_selected",
                0,
            )
            gateway._record_unavailable(
                "economy", "world_cognition:live-wait", "pool_not_configured"
            )
        return {
            "store": store,
            "gateway": gateway,
            "group": group,
            "key": key,
            "decision": decision,
            "live_decision": live_decision,
        }

    def test_schema_migrates_from_version_one(self) -> None:
        with self.database.transaction() as connection:
            connection.execute("DROP TABLE model_attempts")
            connection.execute("DROP TABLE model_calls")
            connection.execute("UPDATE schema_meta SET value = '1' WHERE key = 'schema_version'")
        migrated = Database(self.db_path)
        with migrated.connection() as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)
        self.assertTrue({"model_calls", "model_attempts"}.issubset(tables))

    def test_ledger_idempotency_conflicts_are_rejected(self) -> None:
        first, created = self.ledger.prepare_call(
            self.subject_id, "fake", "model", "reflection", "hash-one", "same-key"
        )
        duplicate, duplicate_created = self.ledger.prepare_call(
            self.subject_id, "fake", "model", "reflection", "hash-one", "same-key"
        )
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.call_id, duplicate.call_id)
        with self.assertRaises(ModelCallConflictError):
            self.ledger.prepare_call(
                self.subject_id,
                "fake",
                "model",
                "reflection",
                "different-hash",
                "same-key",
            )

    def test_model_call_rejects_invalid_usage_estimated_storage(self) -> None:
        call, _ = self.ledger.prepare_call(
            self.subject_id,
            "fake",
            "model",
            "integrity",
            "integrity-request-hash",
            "integrity-usage-estimated",
        )
        for value in (2, 0.5, sqlite3.Binary(b"1")):
            with self.subTest(value=value):
                with self.database.transaction() as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        "UPDATE model_calls SET usage_estimated = ? WHERE call_id = ?",
                        (value, call.call_id),
                    )
                with self.assertRaises(IntegrityError):
                    self.ledger.get_call(call.call_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE model_calls SET usage_estimated = 0 WHERE call_id = ?",
                        (call.call_id,),
                    )

    def test_model_call_rejects_invalid_durable_metadata(self) -> None:
        call, _ = self.ledger.prepare_call(
            self.subject_id,
            "fake",
            "model",
            "integrity",
            "integrity-metadata-request-hash",
            "integrity-metadata",
        )
        with self.database.connection() as connection:
            original = dict(
                connection.execute(
                    "SELECT * FROM model_calls WHERE call_id = ?", (call.call_id,)
                ).fetchone()
            )
        cases = (
            ("request_hash", sqlite3.Binary(b"integrity-metadata-request-hash")),
            ("resource_pool", "invalid"),
            ("capture_policy_version", 0.5),
            ("capture_policy_version", sqlite3.Binary(b"1")),
            ("capture_policy_version", 0),
        )
        for column, value in cases:
            with self.subTest(column=column, value=value):
                with self.database.transaction() as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f'UPDATE model_calls SET "{column}" = ? WHERE call_id = ?',
                        (value, call.call_id),
                    )
                with self.assertRaises(IntegrityError):
                    self.ledger.get_call(call.call_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE model_calls SET "{column}" = ? WHERE call_id = ?',
                        (original[column], call.call_id),
                    )

    def test_model_attempt_rejects_invalid_durable_counters(self) -> None:
        call, _ = self.ledger.prepare_call(
            self.subject_id,
            "fake",
            "model",
            "integrity",
            "integrity-attempt-request-hash",
            "integrity-attempt-counters",
        )
        attempt = self.ledger.authorize_attempt(
            call.call_id,
            self.limits,
            reserved_input_tokens=10,
            reserved_output_tokens=20,
            reserved_cost_microusd=30,
        )
        with self.database.connection() as connection:
            original = dict(
                connection.execute(
                    "SELECT * FROM model_attempts WHERE attempt_id = ?", (attempt.attempt_id,)
                ).fetchone()
            )
        columns = (
            "attempt_number",
            "reserved_input_tokens",
            "reserved_output_tokens",
            "reserved_cost_microusd",
            "input_tokens",
            "output_tokens",
            "cost_microusd",
        )
        for column in columns:
            invalid_values = (0.5, sqlite3.Binary(b"1"), -1)
            for value in invalid_values:
                with self.subTest(column=column, value=value):
                    with self.database.transaction() as connection:
                        connection.execute("PRAGMA ignore_check_constraints = ON")
                        connection.execute(
                            f'UPDATE model_attempts SET "{column}" = ? WHERE attempt_id = ?',
                            (value, attempt.attempt_id),
                        )
                    with self.assertRaises(IntegrityError):
                        self.ledger.attempts(call.call_id)
                    with self.database.transaction() as connection:
                        connection.execute(
                            f'UPDATE model_attempts SET "{column}" = ? WHERE attempt_id = ?',
                            (original[column], attempt.attempt_id),
                        )

    def test_model_ledger_rejects_invalid_single_row_lifecycle_state(self) -> None:
        call, _ = self.ledger.prepare_call(
            self.subject_id,
            "fake",
            "model",
            "integrity",
            "integrity-lifecycle-request-hash",
            "integrity-lifecycle",
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE model_calls SET completed_at = ? WHERE call_id = ?",
                ("2026-08-17T00:00:00.000+00:00", call.call_id),
            )
        with self.assertRaises(IntegrityError):
            self.ledger.get_call(call.call_id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE model_calls SET completed_at = NULL WHERE call_id = ?", (call.call_id,)
            )

        attempt = self.ledger.authorize_attempt(
            call.call_id,
            self.limits,
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=1,
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE model_attempts SET status = 'succeeded' WHERE attempt_id = ?",
                (attempt.attempt_id,),
            )
        with self.assertRaises(IntegrityError):
            self.ledger.attempts(call.call_id)

    async def test_structured_success_is_persisted_and_cached(self) -> None:
        provider = FakeProvider([insight_response()])
        gateway = self.gateway(provider)
        first = await gateway.complete_structured(
            self.subject_id,
            "reflection",
            self.messages,
            Insight,
            idempotency_key="reflection-1",
        )
        cached = await gateway.complete_structured(
            self.subject_id,
            "reflection",
            self.messages,
            Insight,
            idempotency_key="reflection-1",
        )
        self.assertEqual(first.output.summary, "A durable observation")
        self.assertFalse(first.cached)
        self.assertTrue(cached.cached)
        self.assertEqual(first.call_id, cached.call_id)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(self.ledger.get_call(first.call_id).status, "succeeded")

    async def test_model_io_capture_reads_live_consent_and_fails_closed(self) -> None:
        state = {"enabled": False}
        provider = FakeProvider(
            [
                insight_response(summary="without consent"),
                insight_response(summary="with consent"),
                insight_response(summary="after withdrawal"),
            ]
        )
        gateway = self.gateway(provider, capture_model_io_getter=lambda: state["enabled"])

        first = await gateway.complete_structured(
            self.subject_id,
            "reflection",
            self.messages,
            Insight,
            idempotency_key="consent-off",
        )
        state["enabled"] = True
        second = await gateway.complete_structured(
            self.subject_id,
            "reflection",
            self.messages,
            Insight,
            idempotency_key="consent-on",
        )
        state["enabled"] = False
        third = await gateway.complete_structured(
            self.subject_id,
            "reflection",
            self.messages,
            Insight,
            idempotency_key="consent-withdrawn",
        )

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT call_id, idempotency_key, request_json FROM model_calls "
                "WHERE subject_id = ? ORDER BY created_at, call_id",
                (self.subject_id,),
            ).fetchall()
        self.assertEqual(
            [first.call_id, second.call_id, third.call_id], [row["call_id"] for row in rows]
        )
        self.assertIsNone(rows[0]["request_json"])
        self.assertIsNotNone(rows[1]["request_json"])
        self.assertIsNone(rows[2]["request_json"])

    async def test_cold_model_payload_compression_preserves_cached_response(self) -> None:
        provider = FakeProvider([insight_response(summary="x" * 2_000)])
        gateway = self.gateway(provider)
        result = await gateway.complete_structured(
            self.subject_id,
            "reflection",
            self.messages,
            Insight,
            idempotency_key="compress-response",
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE model_calls SET created_at = ? WHERE call_id = ?",
                ("2020-01-01T00:00:00+00:00", result.call_id),
            )
        changed = self.ledger.compress_cold_payloads(
            self.subject_id,
            older_than_days=1,
        )
        self.assertEqual(changed, 1)
        restored = self.ledger.get_call(result.call_id)
        assert restored.response is not None
        self.assertIn("x" * 2_000, restored.response["content"])
        with self.database.connection() as connection:
            stored = connection.execute(
                "SELECT response_json FROM model_calls WHERE call_id = ?",
                (result.call_id,),
            ).fetchone()[0]
        self.assertTrue(str(stored).startswith("noyra-zlib-b64:"))
        self.assertEqual(
            self.ledger.compress_cold_payloads(self.subject_id, older_than_days=1),
            0,
        )

    def test_ledger_rechecks_training_policy_inside_prepare_transaction(self) -> None:
        store = TrainingStore(self.database)
        first, _ = self.ledger.prepare_call(
            self.subject_id,
            "fake",
            "test-model",
            "policy-test",
            "policy-request-1",
            "policy-call-1",
            request={"messages": [{"content": "private"}]},
            enforce_training_policy=True,
        )
        store.update_policy(self.subject_id, include_model_io=True)
        second, _ = self.ledger.prepare_call(
            self.subject_id,
            "fake",
            "test-model",
            "policy-test",
            "policy-request-2",
            "policy-call-2",
            request={"messages": [{"content": "consented"}]},
            enforce_training_policy=True,
        )
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT call_id, request_json, capture_policy_version FROM model_calls "
                "WHERE call_id IN (?, ?)",
                (first.call_id, second.call_id),
            ).fetchall()
        by_id = {row["call_id"]: row for row in rows}
        self.assertIsNone(by_id[first.call_id]["request_json"])
        self.assertEqual(by_id[first.call_id]["capture_policy_version"], 1)
        self.assertIsNotNone(by_id[second.call_id]["request_json"])
        self.assertEqual(by_id[second.call_id]["capture_policy_version"], 2)

    async def test_known_retryable_failure_retries_with_accounting(self) -> None:
        delays: list[float] = []

        async def record_delay(delay: float) -> None:
            delays.append(delay)

        provider = FakeProvider(
            [
                ProviderCallError("provider_http_429", retryable=True, outcome_unknown=False),
                insight_response(),
            ]
        )
        gateway = self.gateway(provider, sleep=record_delay)
        result = await gateway.complete_structured(
            self.subject_id,
            "prediction",
            self.messages,
            Insight,
            idempotency_key="prediction-retry",
        )
        attempts = self.ledger.attempts(result.call_id)
        self.assertEqual(result.attempts, 2)
        self.assertEqual([attempt.status for attempt in attempts], ["failed", "succeeded"])
        self.assertEqual(len(delays), 1)
        self.assertEqual(self.ledger.budget_status(self.subject_id, self.limits).attempts, 2)

    async def test_retry_with_unknown_usage_keeps_the_failed_attempt_reservation(self) -> None:
        provider = FakeProvider(
            [
                ProviderCallError(
                    "provider_http_503",
                    retryable=True,
                    outcome_unknown=False,
                    usage_unknown=True,
                ),
                insight_response(input_tokens=4, output_tokens=2),
            ]
        )
        result = await self.gateway(provider).complete_structured(
            self.subject_id,
            "prediction",
            self.messages,
            Insight,
            idempotency_key="unknown-usage-retry",
            max_output_tokens=20,
        )
        budget = self.ledger.budget_status(self.subject_id, self.limits)
        self.assertEqual(result.attempts, 2)
        self.assertGreater(budget.input_tokens, result.usage.input_tokens)
        self.assertGreater(budget.output_tokens, result.usage.output_tokens)

    async def test_ambiguous_failure_never_retries_and_holds_reservation(self) -> None:
        provider = FakeProvider(
            [
                ProviderCallError(
                    "provider_outcome_unknown", retryable=False, outcome_unknown=True
                ),
                insight_response(summary="must not execute"),
            ]
        )
        gateway = self.gateway(provider)
        with self.assertRaises(ProviderCallError):
            await gateway.complete_structured(
                self.subject_id,
                "observation",
                self.messages,
                Insight,
                idempotency_key="ambiguous",
            )
        self.assertEqual(len(provider.requests), 1)
        budget = self.ledger.budget_status(self.subject_id, self.limits)
        self.assertEqual(budget.attempts, 1)
        self.assertGreater(budget.output_tokens, 0)
        with self.database.connection() as connection:
            state = connection.execute(
                "SELECT status FROM model_calls WHERE idempotency_key = 'ambiguous'"
            ).fetchone()[0]
        self.assertEqual(state, "unknown")

    async def test_unknown_call_requires_explicit_retry_authorization(self) -> None:
        first_provider = FakeProvider(
            [ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True)]
        )
        gateway = self.gateway(first_provider)
        with self.assertRaises(ProviderCallError):
            await gateway.complete_structured(
                self.subject_id,
                "observation",
                self.messages,
                Insight,
                idempotency_key="explicit-unknown-retry",
            )
        call = self.ledger.unknown_calls(self.subject_id)[0]
        self.ledger.prepare_unknown_retry(
            call.call_id,
            actor="operator",
            reason="provider confirmed the first request was not accepted",
        )
        retry_provider = FakeProvider([insight_response(summary="reconciled retry")])
        result = await self.gateway(retry_provider).complete_structured(
            self.subject_id,
            "observation",
            self.messages,
            Insight,
            idempotency_key="explicit-unknown-retry",
        )
        self.assertEqual(result.output.summary, "reconciled retry")
        self.assertEqual(len(self.ledger.attempts(call.call_id)), 2)

    async def test_hard_budget_blocks_before_provider_request(self) -> None:
        provider = FakeProvider([insight_response()])
        blocked_limits = BudgetLimits(0, 10_000, 10_000, 1_000_000)
        gateway = self.gateway(provider, limits=blocked_limits)
        with self.assertRaises(BudgetExhaustedError):
            await gateway.complete_structured(
                self.subject_id,
                "reflection",
                self.messages,
                Insight,
                idempotency_key="blocked",
            )
        self.assertEqual(provider.requests, [])
        self.assertEqual(
            self.ledger.budget_status(self.subject_id, blocked_limits).pressure,
            1.0,
        )

    async def test_invalid_structured_output_is_bounded(self) -> None:
        invalid = ProviderResponse("not-json", ModelUsage(3, 2))
        provider = FakeProvider([invalid, invalid])
        gateway = self.gateway(
            provider,
            retry_policy=RetryPolicy(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
                retry_invalid_output=True,
            ),
        )
        with self.assertRaises(StructuredOutputError):
            await gateway.complete_structured(
                self.subject_id,
                "reflection",
                self.messages,
                Insight,
                idempotency_key="invalid-output",
            )
        self.assertEqual(len(provider.requests), 2)

    async def test_missing_usage_is_conservatively_estimated(self) -> None:
        response = ProviderResponse(
            content=json.dumps({"summary": "estimated", "confidence": 0.5}),
            usage=None,
        )
        result = await self.gateway(FakeProvider([response])).complete_structured(
            self.subject_id,
            "reflection",
            self.messages,
            Insight,
            idempotency_key="estimated-usage",
            max_output_tokens=50,
        )
        self.assertTrue(result.usage_estimated)
        self.assertEqual(result.usage.output_tokens, 50)

    def test_restart_recovery_distinguishes_unsent_and_ambiguous_attempts(self) -> None:
        unsent_call, _ = self.ledger.prepare_call(
            self.subject_id, "fake", "model", "one", "hash-one", "unsent"
        )
        self.ledger.authorize_attempt(
            unsent_call.call_id,
            self.limits,
            reserved_input_tokens=10,
            reserved_output_tokens=20,
            reserved_cost_microusd=30,
        )
        active_call, _ = self.ledger.prepare_call(
            self.subject_id, "fake", "model", "two", "hash-two", "active"
        )
        active_attempt = self.ledger.authorize_attempt(
            active_call.call_id,
            self.limits,
            reserved_input_tokens=10,
            reserved_output_tokens=20,
            reserved_cost_microusd=30,
        )
        self.ledger.start_attempt(active_attempt.attempt_id)

        recovered = self.ledger.recover_interrupted(self.subject_id)
        states = {record.idempotency_key: record.status for record in recovered}
        self.assertEqual(states, {"unsent": "failed", "active": "unknown"})
        self.assertEqual(
            self.ledger.attempts(unsent_call.call_id)[0].status,
            "cancelled",
        )
        self.assertEqual(
            self.ledger.attempts(active_call.call_id)[0].status,
            "unknown",
        )

    def test_routing_integrity_accepts_completed_and_live_states(self) -> None:
        fixture = self.routing_fixture(include_live_state=True)

        report = fixture["store"].verify_routing_integrity(self.subject_id)

        self.assertEqual(
            report,
            {
                "cognitive_route_decisions": 2,
                "cognitive_route_attempts": 3,
                "cognitive_route_outcomes": 1,
                "waiting_cognitive_tasks": 2,
                "waiting_cognitive_task_revisions": 3,
            },
        )

    def test_revoked_key_ignores_late_provider_outcomes(self) -> None:
        store = CognitiveResourceStore(
            self.database, Path(self.temp_dir.name) / "revocation-secrets"
        )
        group = store.configure(
            self.subject_id,
            CognitiveResourceGroupInput(
                pool="economy",
                label="revocation-terminal",
                base_url="https://routing.example/v1",
                model="routing-model",
                api_keys=(SecretStr("revocation-terminal-key"),),
            ),
            actor="operator",
        )
        key = store.keys(group.group_id, subject_id=self.subject_id)[0]

        revoked = store.revoke_key(
            key.key_id,
            reason="operator rotated the credential",
            actor="operator",
            subject_id=self.subject_id,
        )
        self.assertEqual(revoked.status, "revoked")

        # These callbacks can arrive after a provider request was in flight.
        # They must not resurrect the key or append a misleading live-state
        # event after the terminal revocation event.
        store.record_success(key.key_id, subject_id=self.subject_id)
        store.record_failure(
            key.key_id,
            "late_provider_failure",
            cooldown_seconds=300,
            subject_id=self.subject_id,
        )

        current = store.keys(group.group_id, subject_id=self.subject_id)[0]
        self.assertEqual(current.status, "revoked")
        self.assertEqual(current.consecutive_failures, 0)
        with self.database.connection() as connection:
            events = [
                row["event_type"]
                for row in connection.execute(
                    "SELECT event_type FROM cognitive_resource_key_events "
                    "WHERE key_id = ? ORDER BY rowid",
                    (key.key_id,),
                ).fetchall()
            ]
        self.assertEqual(events, ["configured", "revoked"])

    def test_cognitive_resource_views_are_page_bounded_and_ordered(self) -> None:
        store = CognitiveResourceStore(
            self.database, Path(self.temp_dir.name) / "paged-resource-secrets"
        )
        group = store.configure(
            self.subject_id,
            CognitiveResourceGroupInput(
                pool="economy",
                label="paged-resource",
                base_url="https://paged.example/v1",
                model="paged-model",
                api_keys=(SecretStr("key-a"), SecretStr("key-b"), SecretStr("key-c")),
            ),
            actor="operator",
        )

        first_page = store.keys(group.group_id, subject_id=self.subject_id, limit=2)
        second_page = store.keys(group.group_id, subject_id=self.subject_id, limit=2, offset=2)
        self.assertEqual(len(first_page), 2)
        self.assertEqual(len(second_page), 1)
        self.assertEqual(
            [item.key_id for item in first_page + second_page],
            [item.key_id for item in store.keys(group.group_id, subject_id=self.subject_id)],
        )
        self.assertEqual(len(store.list(self.subject_id, limit=1)), 1)

    def test_cognitive_resource_add_keys_rejects_unbounded_batches(self) -> None:
        store = CognitiveResourceStore(
            self.database, Path(self.temp_dir.name) / "bounded-resource-secrets"
        )
        group = store.configure(
            self.subject_id,
            CognitiveResourceGroupInput(
                pool="economy",
                label="bounded-resource",
                base_url="https://bounded.example/v1",
                model="bounded-model",
                api_keys=(SecretStr("initial-key"),),
            ),
            actor="operator",
        )
        with self.assertRaises(ValueError):
            store.add_keys(
                group.group_id,
                tuple(SecretStr(f"key-{index}") for index in range(65)),
                actor="operator",
                subject_id=self.subject_id,
            )

    def test_cognitive_resource_budget_boundary_round_trip(self) -> None:
        secret_dir = Path(self.temp_dir.name) / "boundary-secrets"
        store = CognitiveResourceStore(self.database, secret_dir)
        group = store.configure(
            self.subject_id,
            CognitiveResourceGroupInput(
                pool="economy",
                label="boundary-round-trip",
                base_url="https://boundary.example/v1",
                model="boundary-model",
                api_keys=(SecretStr("boundary-round-trip-key"),),
                priority=0,
                weight=1,
                daily_attempts=0,
                daily_input_tokens=0,
                daily_output_tokens=0,
                daily_cost_limit_usd=Decimal("0"),
                input_usd_per_million=Decimal("0"),
                output_usd_per_million=Decimal("0"),
                max_attempts=1,
            ),
            actor="operator",
        )
        maximum = store.update(
            group.group_id,
            CognitiveResourceGroupUpdate(
                priority=1_000,
                weight=1_000,
                daily_attempts=SQLITE_INT64_MAX,
                daily_input_tokens=SQLITE_INT64_MAX,
                daily_output_tokens=SQLITE_INT64_MAX,
                daily_cost_limit_usd=MAX_USD,
                input_usd_per_million=MAX_USD,
                output_usd_per_million=MAX_USD,
                max_attempts=10,
            ),
            reason="exercise budget boundaries",
            actor="operator",
            subject_id=self.subject_id,
        )

        self.assertEqual(maximum.priority, 1_000)
        self.assertEqual(maximum.weight, 1_000)
        self.assertEqual(maximum.daily_attempts, SQLITE_INT64_MAX)
        self.assertEqual(maximum.daily_input_tokens, SQLITE_INT64_MAX)
        self.assertEqual(maximum.daily_output_tokens, SQLITE_INT64_MAX)
        self.assertEqual(maximum.daily_cost_microusd, SQLITE_INT64_MAX)
        self.assertEqual(maximum.input_microusd_per_million, SQLITE_INT64_MAX)
        self.assertEqual(maximum.output_microusd_per_million, SQLITE_INT64_MAX)
        self.assertEqual(maximum.max_attempts, 10)
        self.assertEqual(store.get(group.group_id, subject_id=self.subject_id), maximum)

    def test_cognitive_resource_public_budget_validation_is_strict(self) -> None:
        input_base: dict[str, Any] = {
            "pool": "economy",
            "label": "strict-budget",
            "base_url": "https://strict-budget.example/v1",
            "model": "strict-budget-model",
            "api_keys": (SecretStr("strict-budget-key"),),
        }
        integer_fields = (
            "priority",
            "weight",
            "daily_attempts",
            "daily_input_tokens",
            "daily_output_tokens",
            "max_attempts",
        )
        invalid_integer_values: tuple[Any, ...] = (
            SQLITE_INT64_MAX + 1,
            True,
            1.0,
            "01",
            " 1",
            "+1",
            -1,
        )
        for model_type in (CognitiveResourceGroupInput, CognitiveResourceGroupUpdate):
            for field in integer_fields:
                for value in invalid_integer_values:
                    with self.subTest(model=model_type.__name__, field=field, value=repr(value)):
                        kwargs = (
                            {**input_base, field: value}
                            if model_type is CognitiveResourceGroupInput
                            else {field: value}
                        )
                        with self.assertRaises(ValidationError):
                            model_type(**kwargs)

        usd_fields = (
            "daily_cost_limit_usd",
            "input_usd_per_million",
            "output_usd_per_million",
        )
        invalid_usd_values: tuple[Any, ...] = (
            float("inf"),
            float("nan"),
            MAX_USD + Decimal("0.000001"),
            Decimal("0.0000001"),
        )
        for model_type in (CognitiveResourceGroupInput, CognitiveResourceGroupUpdate):
            for field in usd_fields:
                for value in invalid_usd_values:
                    with self.subTest(model=model_type.__name__, field=field, value=repr(value)):
                        kwargs = (
                            {**input_base, field: value}
                            if model_type is CognitiveResourceGroupInput
                            else {field: value}
                        )
                        with self.assertRaises(ValidationError):
                            model_type(**kwargs)

    def test_configure_rejects_model_copy_bypass_before_secret_write(self) -> None:
        secret_dir = Path(self.temp_dir.name) / "copy-bypass-secrets"
        store = CognitiveResourceStore(self.database, secret_dir)
        valid = CognitiveResourceGroupInput(
            pool="economy",
            label="copy-bypass",
            base_url="https://copy-bypass.example/v1",
            model="copy-bypass-model",
            api_keys=(SecretStr("copy-bypass-key"),),
        )
        invalid = valid.model_copy(update={"daily_attempts": "1"})

        with self.assertRaises(IntegrityError):
            store.configure(self.subject_id, invalid, actor="operator")

        with self.database.connection() as connection:
            group_count = connection.execute(
                "SELECT COUNT(*) AS count FROM cognitive_resource_groups "
                "WHERE subject_id = ? AND label = ?",
                (self.subject_id, "copy-bypass"),
            ).fetchone()["count"]
            intent_count = connection.execute(
                "SELECT COUNT(*) AS count FROM secret_file_intents "
                "WHERE subject_id = ? AND resource_type = 'cognitive'",
                (self.subject_id,),
            ).fetchone()["count"]
        self.assertEqual(group_count, 0)
        self.assertEqual(intent_count, 0)
        self.assertTrue(secret_dir.exists())
        self.assertEqual(list(secret_dir.iterdir()), [])

    def test_update_model_construct_bypass_is_atomic(self) -> None:
        fixture = self.routing_fixture()
        store: CognitiveResourceStore = fixture["store"]
        group_id = fixture["group"].group_id
        with self.database.connection() as connection:
            before = dict(
                connection.execute(
                    "SELECT * FROM cognitive_resource_groups WHERE group_id = ?",
                    (group_id,),
                ).fetchone()
            )
            revision_count_before = connection.execute(
                "SELECT COUNT(*) AS count FROM cognitive_resource_group_revisions "
                "WHERE group_id = ?",
                (group_id,),
            ).fetchone()["count"]

        invalid = CognitiveResourceGroupUpdate.model_construct(daily_attempts="1")
        with self.assertRaises(IntegrityError):
            store.update(
                group_id,
                invalid,
                reason="should not commit",
                actor="operator",
                subject_id=self.subject_id,
            )

        with self.database.connection() as connection:
            after = dict(
                connection.execute(
                    "SELECT * FROM cognitive_resource_groups WHERE group_id = ?",
                    (group_id,),
                ).fetchone()
            )
            revision_count_after = connection.execute(
                "SELECT COUNT(*) AS count FROM cognitive_resource_group_revisions "
                "WHERE group_id = ?",
                (group_id,),
            ).fetchone()["count"]
        self.assertEqual(after, before)
        self.assertEqual(revision_count_after, revision_count_before)

    def test_persistent_numeric_corruption_rejected_by_get_and_integrity(self) -> None:
        fixture = self.routing_fixture()
        store: CognitiveResourceStore = fixture["store"]
        group_id = fixture["group"].group_id
        with self.database.connection() as connection:
            original = connection.execute(
                "SELECT daily_attempts FROM cognitive_resource_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()["daily_attempts"]

        for corrupted in ("not-a-number", 1.5, sqlite3.Binary(b"1"), -1):
            with self.subTest(corrupted=repr(corrupted)):
                with self.database.transaction() as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        "UPDATE cognitive_resource_groups SET daily_attempts = ? "
                        "WHERE group_id = ?",
                        (corrupted, group_id),
                    )
                try:
                    with self.assertRaises(IntegrityError):
                        store.get(group_id, subject_id=self.subject_id)
                    with self.assertRaises(IntegrityError):
                        store.verify_routing_integrity(self.subject_id)
                finally:
                    with self.database.transaction() as connection:
                        connection.execute(
                            "UPDATE cognitive_resource_groups SET daily_attempts = ? "
                            "WHERE group_id = ?",
                            (original, group_id),
                        )

    def test_model_resource_url_validation_is_strict_for_input_and_persistence(self) -> None:
        input_base: dict[str, Any] = {
            "pool": "economy",
            "label": "strict-url",
            "base_url": "https://models.example/v1",
            "model": "strict-url-model",
            "api_keys": (SecretStr("strict-url-key"),),
        }
        invalid_urls = (
            "http://models.example/v1",
            "https://localhost/v1",
            "https://localhost./v1",
            "https://127.0.0.1/v1",
            "https://10.0.0.1/v1",
            "https://[::1]/v1",
            "https://user:password@models.example/v1",
            "https://models.example/v1?token=secret",
            "https://models.example/v1#fragment",
            "https://models.example:invalid/v1",
        )
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                CognitiveResourceGroupInput(**{**input_base, "base_url": url})

        normalized = CognitiveResourceGroupInput(
            **{**input_base, "base_url": "https://models.example/v1/"}
        )
        self.assertEqual(normalized.base_url, "https://models.example/v1")

        fixture = self.routing_fixture()
        store: CognitiveResourceStore = fixture["store"]
        group_id = fixture["group"].group_id
        with self.database.connection() as connection:
            row = dict(
                connection.execute(
                    "SELECT * FROM cognitive_resource_groups WHERE group_id = ?",
                    (group_id,),
                ).fetchone()
            )
        values = list(store._row_values(row))
        for url in (*invalid_urls, "https://models.example/v1/"):
            with self.subTest(persisted_url=url):
                corrupted = list(values)
                corrupted[4] = url
                with self.assertRaises(IntegrityError):
                    store._validate_group_values(tuple(corrupted), row["status"])

        valid = list(values)
        valid[4] = "https://public-model.example/v1"
        store._validate_group_values(tuple(valid), row["status"])

    def test_row_values_preserve_raw_sqlite_numeric_types(self) -> None:
        fixture = self.routing_fixture()
        store: CognitiveResourceStore = fixture["store"]
        group_id = fixture["group"].group_id
        with self.database.connection() as connection:
            row = dict(
                connection.execute(
                    "SELECT * FROM cognitive_resource_groups WHERE group_id = ?",
                    (group_id,),
                ).fetchone()
            )
        row["daily_attempts"] = "100"
        values = store._row_values(row)
        self.assertEqual(values[8], "100")
        with self.assertRaises(IntegrityError):
            store._validate_group_values(values, row["status"])

    def test_routing_integrity_rejects_storage_numbers_and_hashes(self) -> None:
        fixture = self.routing_fixture()
        store: CognitiveResourceStore = fixture["store"]
        decision_id = fixture["decision"].decision_id
        with self.database.transaction() as connection:
            for trigger in (
                "prevent_cognitive_route_decision_update",
                "prevent_cognitive_route_attempt_update",
                "prevent_cognitive_route_outcome_update",
                "prevent_waiting_cognitive_task_revision_update",
            ):
                connection.execute(f'DROP TRIGGER "{trigger}"')
        with self.database.connection() as connection:
            decision = dict(
                connection.execute(
                    "SELECT * FROM cognitive_route_decisions WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
            )
            attempt = dict(
                connection.execute(
                    "SELECT * FROM cognitive_route_attempts WHERE decision_id = ? "
                    "ORDER BY rowid LIMIT 1",
                    (decision_id,),
                ).fetchone()
            )
            outcome = dict(
                connection.execute(
                    "SELECT * FROM cognitive_route_outcomes WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
            )
            task = dict(
                connection.execute(
                    "SELECT * FROM waiting_cognitive_tasks WHERE purpose = ?",
                    ("world_cognition:resolved-wait",),
                ).fetchone()
            )
            revision = dict(
                connection.execute(
                    "SELECT * FROM waiting_cognitive_task_revisions WHERE task_id = ? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (task["task_id"],),
                ).fetchone()
            )

        cases = (
            (
                "cognitive_route_decisions",
                "decision_id",
                decision["decision_id"],
                "importance",
                sqlite3.Binary(b"0.5"),
                decision["importance"],
            ),
            (
                "cognitive_route_decisions",
                "decision_id",
                decision["decision_id"],
                "reason_code",
                sqlite3.Binary(b'{"invalid":'),
                decision["reason_code"],
            ),
            (
                "cognitive_route_attempts",
                "attempt_id",
                attempt["attempt_id"],
                "latency_ms",
                0.5,
                attempt["latency_ms"],
            ),
            (
                "cognitive_route_outcomes",
                "outcome_id",
                outcome["outcome_id"],
                "result_changed_state",
                2,
                outcome["result_changed_state"],
            ),
            (
                "waiting_cognitive_tasks",
                "task_id",
                task["task_id"],
                "retry_count",
                sqlite3.Binary(b"1"),
                task["retry_count"],
            ),
            (
                "waiting_cognitive_task_revisions",
                "revision_id",
                revision["revision_id"],
                "next_retry_at",
                sqlite3.Binary(str(revision["next_retry_at"]).encode("utf-8")),
                revision["next_retry_at"],
            ),
            (
                "cognitive_route_decisions",
                "decision_id",
                decision["decision_id"],
                "state_hash",
                "0" * 64,
                decision["state_hash"],
            ),
        )
        for table, key_column, key, column, value, original in cases:
            with self.subTest(table=table, column=column):
                with self.database.transaction() as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f'UPDATE "{table}" SET "{column}" = ? WHERE "{key_column}" = ?',
                        (value, key),
                    )
                with self.assertRaises(IntegrityError):
                    store.verify_routing_integrity(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE "{table}" SET "{column}" = ? WHERE "{key_column}" = ?',
                        (original, key),
                    )

    def test_routing_integrity_rejects_ownership_sequence_and_wait_chain(self) -> None:
        fixture = self.routing_fixture()
        store: CognitiveResourceStore = fixture["store"]
        decision_id = fixture["decision"].decision_id
        other_subject = "Noyra-model-routing-foreign"
        IdentityStore(self.database).ensure(
            other_subject, content_hash({"seed": "model-routing-foreign"})
        )
        foreign_group = store.configure(
            other_subject,
            CognitiveResourceGroupInput(
                pool="economy",
                label="foreign-routing",
                base_url="https://foreign-routing.example/v1",
                model="foreign-routing-model",
                api_keys=(SecretStr("foreign-routing-key"),),
            ),
            actor="operator",
        )
        foreign_key = store.keys(foreign_group.group_id, subject_id=other_subject)[0]
        foreign_gateway = RoutedModelGateway(self.database, other_subject, store)
        foreign_decision = foreign_gateway._record_decision(
            "world_cognition:foreign-routing",
            "economy",
            selected_route="economy_model",
            group_id=None,
            key_id=None,
            reason_code="purpose_classified_locally",
        )
        with self.database.transaction() as connection:
            for trigger in (
                "prevent_cognitive_route_decision_update",
                "prevent_cognitive_route_attempt_update",
                "prevent_cognitive_route_outcome_update",
                "prevent_waiting_cognitive_task_revision_update",
            ):
                connection.execute(f'DROP TRIGGER "{trigger}"')
        with self.database.connection() as connection:
            decision = dict(
                connection.execute(
                    "SELECT * FROM cognitive_route_decisions WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
            )
            attempts = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM cognitive_route_attempts WHERE decision_id = ? ORDER BY rowid",
                    (decision_id,),
                ).fetchall()
            ]
            outcome = dict(
                connection.execute(
                    "SELECT * FROM cognitive_route_outcomes WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
            )
            task = dict(
                connection.execute(
                    "SELECT * FROM waiting_cognitive_tasks WHERE purpose = ?",
                    ("world_cognition:resolved-wait",),
                ).fetchone()
            )
            revisions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM waiting_cognitive_task_revisions WHERE task_id = ? "
                    "ORDER BY rowid",
                    (task["task_id"],),
                ).fetchall()
            ]

        def rehash(row: dict[str, Any], fields: tuple[str, ...], **changes: Any) -> str:
            return content_hash(
                {field: changes[field] if field in changes else row[field] for field in fields}
            )

        decision_fields = (
            "decision_id",
            "subject_id",
            "purpose",
            "task_kind",
            "selected_route",
            "pool",
            "group_id",
            "key_id",
            "importance",
            "risk",
            "ambiguity",
            "reason_code",
            "created_at",
        )
        attempt_fields = (
            "subject_id",
            "decision_id",
            "group_id",
            "key_id",
            "attempt_number",
            "outcome",
            "reason_code",
            "latency_ms",
            "created_at",
        )
        outcome_fields = (
            "subject_id",
            "decision_id",
            "outcome",
            "result_changed_state",
            "input_tokens",
            "output_tokens",
            "cost_microusd",
            "latency_ms",
            "reason_code",
            "created_at",
        )
        task_fields = (
            "task_id",
            "subject_id",
            "pool",
            "purpose",
            "status",
            "reason_code",
            "retry_count",
            "next_retry_at",
            "first_waited_at",
            "updated_at",
        )
        revision_fields = (
            "task_id",
            "status",
            "reason_code",
            "retry_count",
            "next_retry_at",
            "created_at",
        )

        decision_changes = {
            "group_id": foreign_group.group_id,
            "key_id": foreign_key.key_id,
        }
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE cognitive_route_decisions SET group_id = ?, key_id = ?, state_hash = ? "
                "WHERE decision_id = ?",
                (
                    foreign_group.group_id,
                    foreign_key.key_id,
                    rehash(decision, decision_fields, **decision_changes),
                    decision_id,
                ),
            )
        with self.assertRaises(IntegrityError):
            store.verify_routing_integrity(self.subject_id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE cognitive_route_decisions SET group_id = ?, key_id = ?, state_hash = ? "
                "WHERE decision_id = ?",
                (decision["group_id"], decision["key_id"], decision["state_hash"], decision_id),
            )

        target_attempt = attempts[0]
        for changes in (
            {"subject_id": other_subject},
            {"decision_id": foreign_decision.decision_id},
            {"group_id": foreign_group.group_id, "key_id": foreign_key.key_id},
        ):
            with self.subTest(changes=changes):
                assignments = ", ".join(f'"{field}" = ?' for field in changes)
                values = list(changes.values())
                with self.database.transaction() as connection:
                    connection.execute(
                        f"UPDATE cognitive_route_attempts SET {assignments}, state_hash = ? "
                        "WHERE attempt_id = ?",
                        (
                            *values,
                            rehash(target_attempt, attempt_fields, **changes),
                            target_attempt["attempt_id"],
                        ),
                    )
                with self.assertRaises(IntegrityError):
                    store.verify_routing_integrity(self.subject_id)
                with self.database.transaction() as connection:
                    restore = ", ".join(f'"{field}" = ?' for field in changes)
                    connection.execute(
                        f"UPDATE cognitive_route_attempts SET {restore}, state_hash = ? "
                        "WHERE attempt_id = ?",
                        (
                            *(target_attempt[field] for field in changes),
                            target_attempt["state_hash"],
                            target_attempt["attempt_id"],
                        ),
                    )

        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE cognitive_route_outcomes SET decision_id = ?, state_hash = ? "
                "WHERE outcome_id = ?",
                (
                    foreign_decision.decision_id,
                    rehash(
                        outcome,
                        outcome_fields,
                        decision_id=foreign_decision.decision_id,
                    ),
                    outcome["outcome_id"],
                ),
            )
        with self.assertRaises(IntegrityError):
            store.verify_routing_integrity(self.subject_id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE cognitive_route_outcomes SET decision_id = ?, state_hash = ? "
                "WHERE outcome_id = ?",
                (outcome["decision_id"], outcome["state_hash"], outcome["outcome_id"]),
            )

        with self.database.transaction() as connection:
            for attempt in attempts:
                connection.execute(
                    "UPDATE cognitive_route_attempts SET attempt_number = 2, state_hash = ? "
                    "WHERE attempt_id = ?",
                    (
                        rehash(attempt, attempt_fields, attempt_number=2),
                        attempt["attempt_id"],
                    ),
                )
        with self.assertRaises(IntegrityError):
            store.verify_routing_integrity(self.subject_id)
        with self.database.transaction() as connection:
            for attempt in attempts:
                connection.execute(
                    "UPDATE cognitive_route_attempts SET attempt_number = ?, state_hash = ? "
                    "WHERE attempt_id = ?",
                    (
                        attempt["attempt_number"],
                        attempt["state_hash"],
                        attempt["attempt_id"],
                    ),
                )

        latest_revision = revisions[-1]
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE waiting_cognitive_task_revisions SET retry_count = 2, state_hash = ? "
                "WHERE revision_id = ?",
                (
                    rehash(latest_revision, revision_fields, retry_count=2),
                    latest_revision["revision_id"],
                ),
            )
        with self.assertRaises(IntegrityError):
            store.verify_routing_integrity(self.subject_id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE waiting_cognitive_task_revisions SET retry_count = ?, state_hash = ? "
                "WHERE revision_id = ?",
                (
                    latest_revision["retry_count"],
                    latest_revision["state_hash"],
                    latest_revision["revision_id"],
                ),
            )

        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE waiting_cognitive_tasks SET status = 'cancelled', state_hash = ? "
                "WHERE task_id = ?",
                (rehash(task, task_fields, status="cancelled"), task["task_id"]),
            )
        with self.assertRaises(IntegrityError):
            store.verify_routing_integrity(self.subject_id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE waiting_cognitive_tasks SET status = ?, state_hash = ? WHERE task_id = ?",
                (task["status"], task["state_hash"], task["task_id"]),
            )


class ProviderTestCase(unittest.IsolatedAsyncioTestCase):
    def settings(self, base_url: str = "https://models.example/v1") -> OpenAICompatibleSettings:
        return OpenAICompatibleSettings(
            base_url=base_url,
            model="remote-model",
            api_key=SecretStr("super-secret-key"),
        )

    def request(self) -> CompletionRequest:
        return CompletionRequest(
            model="remote-model",
            messages=(ModelMessage(role="user", content="Return one insight."),),
            max_output_tokens=100,
            temperature=0.2,
            schema_name="Insight",
            output_schema=Insight.model_json_schema(),
        )

    async def test_provider_sends_schema_and_parses_usage_without_leaking_key(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"x-request-id": "header-request"},
                json={
                    "id": "response-id",
                    "choices": [
                        {
                            "message": {"content": '{"summary":"remote","confidence":0.7}'},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleProvider(self.settings(), client=client)
        response = await provider.complete(self.request())
        await client.aclose()

        self.assertEqual(captured["authorization"], "Bearer super-secret-key")
        self.assertEqual(captured["body"]["response_format"]["type"], "json_schema")
        self.assertEqual(response.usage, ModelUsage(11, 7))
        self.assertEqual(response.provider_request_id, "response-id")
        self.assertNotIn("super-secret-key", repr(provider.settings))

    async def test_provider_classifies_http_and_transport_failures(self) -> None:
        async def rate_limited(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="sensitive provider body")

        limited_client = httpx.AsyncClient(transport=httpx.MockTransport(rate_limited))
        limited = OpenAICompatibleProvider(self.settings(), client=limited_client)
        with self.assertRaises(ProviderCallError) as caught:
            await limited.complete(self.request())
        await limited_client.aclose()
        self.assertTrue(caught.exception.retryable)
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertNotIn("sensitive", str(caught.exception))

        async def connect_failure(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network details", request=request)

        failed_client = httpx.AsyncClient(transport=httpx.MockTransport(connect_failure))
        failed = OpenAICompatibleProvider(self.settings(), client=failed_client)
        with self.assertRaises(ProviderCallError) as connect_caught:
            await failed.complete(self.request())
        await failed_client.aclose()
        self.assertTrue(connect_caught.exception.retryable)
        self.assertFalse(connect_caught.exception.outcome_unknown)

    async def test_provider_rejects_oversized_response_without_buffering_it_all(self) -> None:
        async def oversized(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 2_048)

        client = httpx.AsyncClient(transport=httpx.MockTransport(oversized))
        settings = self.settings().model_copy(update={"max_response_bytes": 1_024})
        provider = OpenAICompatibleProvider(settings, client=client)
        with self.assertRaises(ProviderCallError) as caught:
            await provider.complete(self.request())
        await client.aclose()
        self.assertEqual(caught.exception.code, "provider_response_too_large")
        self.assertTrue(caught.exception.usage_unknown)

    def test_remote_plaintext_url_and_missing_environment_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.settings("http://models.example/v1")
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            self.assertRaises(ConfigurationError),
        ):
            OpenAICompatibleSettings.from_env()

    def test_runtime_budget_settings_convert_usd_without_float_rounding(self) -> None:
        environment = {
            "NOYRA_DAILY_MODEL_CALLS": "12",
            "NOYRA_DAILY_INPUT_TOKENS": "1000",
            "NOYRA_DAILY_OUTPUT_TOKENS": "500",
            "NOYRA_DAILY_COST_LIMIT": "1.234567",
            "NOYRA_MODEL_INPUT_USD_PER_MILLION": "2.50",
            "NOYRA_MODEL_OUTPUT_USD_PER_MILLION": "10.00",
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            settings = ModelRuntimeSettings.from_env()
        self.assertEqual(settings.budget_limits().daily_cost_microusd, 1_234_567)
        self.assertEqual(settings.pricing().input_microusd_per_million, 2_500_000)
        self.assertEqual(settings.retry_policy().max_attempts, 3)

    async def test_provider_rejects_model_mismatch_before_http(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleProvider(self.settings(), client=client)
        request = self.request().model_copy(update={"model": "unapproved-model"})
        with self.assertRaises(ProviderCallError) as caught:
            await provider.complete(request)
        await client.aclose()
        self.assertEqual(caught.exception.code, "provider_model_mismatch")
        self.assertEqual(requests, [])


@pytest.mark.asyncio
async def test_model_secret_is_not_persisted(tmp_path: Path) -> None:
    database_path = tmp_path / "secret-check.sqlite3"
    database = Database(database_path)
    subject_id = "Noyra-secret-test"
    IdentityStore(database).ensure(subject_id, content_hash({"seed": "secret"}))
    ledger = ModelLedger(database)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer never-persist-this-key"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"summary":"private","confidence":0.5}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        OpenAICompatibleSettings(
            base_url="https://models.example/v1",
            model="model",
            api_key=SecretStr("never-persist-this-key"),
        ),
        client=client,
    )
    gateway = ModelGateway(
        provider,
        ledger,
        model="model",
        limits=BudgetLimits(10, 10_000, 10_000, 1_000_000),
    )
    await gateway.complete_structured(
        subject_id,
        "secret test",
        [ModelMessage(role="user", content="Return JSON.")],
        Insight,
        idempotency_key="secret-test",
    )
    await client.aclose()
    persisted = b"".join(path.read_bytes() for path in tmp_path.iterdir() if path.is_file())
    assert b"never-persist-this-key" not in persisted


def test_sqlite_rejects_unknown_model_subject(tmp_path: Path) -> None:
    database = Database(tmp_path / "foreign-key.sqlite3")
    with pytest.raises(sqlite3.IntegrityError):
        ModelLedger(database).prepare_call(
            "missing-subject", "fake", "model", "test", "hash", "key"
        )
