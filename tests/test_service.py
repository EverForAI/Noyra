from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pydantic import SecretStr, ValidationError

from noyra.capability import CapabilityGrant
from noyra.core import EventStore, OperationInvalidated, SubjectKernel
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.interaction import InteractionStore, PublicProjection
from noyra.mind import GoalCandidate, GoalStore
from noyra.model import (
    EmbeddingResourceStore,
    FakeProvider,
    ModelUsage,
    ProviderResponse,
    RoutedModelGateway,
)
from noyra.model.errors import ProviderCallError
from noyra.service import NoyraHTTPServer, NoyraService, ServiceSettings
from noyra.sleep import SleepEngine, SleepReflectionPlan


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.settings = ServiceSettings(
            data_dir=self.data_dir,
            subject_id="Noyra-service-test",
            genesis_hash=content_hash({"seed": "service-test"}),
            host="127.0.0.1",
            port=0,
            admin_token=SecretStr("test-admin-token-with-sufficient-entropy"),
            active_interval_seconds=1,
            sleep_interval_seconds=1,
            error_backoff_seconds=1,
        )
        self.kernel = SubjectKernel(
            self.data_dir / "noyra.sqlite3",
            self.settings.subject_id,
            self.settings.genesis_hash,
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.http = NoyraHTTPServer(self.kernel, self.settings)
        self.http.start()
        _, port = self.http.address
        self.base_url = f"http://127.0.0.1:{port}"

    def tearDown(self) -> None:
        self.http.close()
        self.kernel.close()
        self.temp_dir.cleanup()

    def get_json(self, path: str) -> object:
        with urlopen(f"{self.base_url}{path}", timeout=5) as response:
            return json.loads(response.read())

    def authorized_json(
        self, path: str, *, payload: Mapping[str, object] | None = None
    ) -> tuple[int, object]:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method="POST" if payload is not None else "GET",
            headers={
                "Authorization": "Bearer test-admin-token-with-sufficient-entropy",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
        )
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def wait_for_export_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 10
        job: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status, current = self.authorized_json(f"/api/admin/export-jobs/{job_id}")
            self.assertEqual(status, 200)
            assert isinstance(current, dict)
            job = current
            if job["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        self.assertEqual(job.get("status"), "completed", job)
        return job

    def test_dashboard_and_read_only_public_endpoints(self) -> None:
        with urlopen(f"{self.base_url}/", timeout=5) as response:
            html = response.read().decode()
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("Noyra", html)
        self.assertIn('href="/admin"', html)
        with urlopen(f"{self.base_url}/favicon.ico", timeout=5) as response:
            self.assertEqual(response.status, 204)
        state = self.get_json("/api/state")
        assert isinstance(state, dict)
        self.assertEqual(state["subject_id"], self.settings.subject_id)
        self.assertEqual(state["schema"], "PublicSubjectStateV1")
        self.assertNotIn("cognitive_resources", state)
        versioned_state = self.get_json("/api/v1/state")
        assert isinstance(versioned_state, dict)
        self.assertEqual(versioned_state["subject_id"], self.settings.subject_id)
        gateway_calls: list[str] = []

        def pool_status(_: object) -> dict[str, object]:
            gateway_calls.append("called")
            return {"economy": {}}

        self.http.cognition_gateway = cast(
            Any, type("GatewayStatus", (), {"pool_status": pool_status})()
        )
        state = self.get_json("/api/state")
        assert isinstance(state, dict)
        self.assertNotIn("cognitive_resources", state)
        self.assertEqual(gateway_calls, [])
        versioned_state = self.get_json("/api/v1/state")
        assert isinstance(versioned_state, dict)
        self.assertEqual(versioned_state, state)
        self.assertEqual(gateway_calls, [])
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/api/state/details", timeout=5)
        self.assertEqual(error.exception.code, 401)
        details_status, details = self.authorized_json("/api/state/details")
        self.assertEqual(details_status, 200)
        assert isinstance(details, dict)
        self.assertIn("cognitive_resources", details)
        self.assertEqual(gateway_calls, ["called"])
        health = self.get_json("/health")
        assert isinstance(health, dict)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(self.get_json("/api/interactions"), [])
        self.assertEqual(self.get_json("/api/behavior"), [])
        for private_view in ("/api/goals", "/api/projects", "/api/outcomes"):
            with self.assertRaises(HTTPError) as error:
                urlopen(f"{self.base_url}{private_view}", timeout=5)
            self.assertEqual(error.exception.code, 401)
        self.assertEqual(self.authorized_json("/api/outcomes")[1], [])
        self.assertEqual(self.authorized_json("/api/goals")[1], [])
        self.assertEqual(self.authorized_json("/api/projects")[1], [])
        for protected in (
            "/api/mailbox",
            "/api/diagnostics",
            "/api/config/search-providers",
            "/api/config/model-resources",
        ):
            with self.assertRaises(HTTPError) as error:
                urlopen(f"{self.base_url}{protected}", timeout=5)
            self.assertEqual(error.exception.code, 401)
        self.assertEqual(self.authorized_json("/api/mailbox")[1], [])
        diagnostics_status, diagnostics = self.authorized_json("/api/diagnostics")
        self.assertEqual(diagnostics_status, 200)
        assert isinstance(diagnostics, dict)
        self.assertIn("unknown", diagnostics)
        self.assertIn("cognition", diagnostics)
        assert isinstance(diagnostics["cognition"], dict)
        self.assertFalse(diagnostics["cognition"]["enabled"])
        knowledge_status, knowledge = self.authorized_json("/api/config/common-knowledge")
        self.assertEqual(knowledge_status, 200)
        assert isinstance(knowledge, dict)
        self.assertIn("review_queue", knowledge)
        for asset in ("/app.js", "/styles.css"):
            with urlopen(f"{self.base_url}{asset}", timeout=5) as response:
                self.assertGreater(len(response.read()), 100)
        self.assertEqual(self.get_json("/api/diary?limit=invalid"), [])
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/missing", timeout=5)
        self.assertEqual(error.exception.code, 404)

    def test_public_state_allowlist_and_unknown_lifecycle_fail_closed(self) -> None:
        forbidden_fields = {
            "reason",
            "changed_at",
            "version",
            "last_checkpoint",
            "fatigue",
            "goal",
            "goals",
            "goal_summary",
            "focus",
            "title",
            "description",
            "project",
            "projects",
            "project_summary",
            "deliverable",
            "progress",
            "learning_summary",
            "cognitive_resources",
            "model",
            "model_usage",
            "motivation_summary",
            "mission",
            "horizon",
            "commitment",
            "confidence",
            "memory",
            "memories",
            "relationship_summary",
            "relationships",
            "thought_summary",
            "thought",
            "self_model_summary",
            "self_model",
            "consciousness",
            "workflow",
            "attention_type",
            "resource_pool",
            "strategy",
            "strategy_kind",
            "reason_code",
            "budget_reset",
        }

        def nested_keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value) | {key for child in value.values() for key in nested_keys(child)}
            if isinstance(value, list):
                return {key for child in value for key in nested_keys(child)}
            return set()

        state = self.get_json("/api/state")
        assert isinstance(state, dict)
        self.assertEqual(set(state), PublicProjection.PUBLIC_STATE_FIELDS)
        self.assertEqual(set(state["lifecycle"]), PublicProjection.PUBLIC_LIFECYCLE_FIELDS)
        self.assertFalse(nested_keys(state) & forbidden_fields)
        versioned = self.get_json("/api/v1/state")
        assert isinstance(versioned, dict)
        self.assertEqual(versioned, state)

        with self.kernel.database.transaction() as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE runtime_state SET state = 'future_unknown' WHERE subject_id = ?",
                (self.settings.subject_id,),
            )
        unknown = self.get_json("/api/state")
        assert isinstance(unknown, dict)
        self.assertEqual(unknown["lifecycle"], {"state": "future_unknown"})
        self.assertFalse(unknown["online"])

    def test_capability_api_rejects_unsupported_per_use_approval(self) -> None:
        payload = {
            "capability_type": "filesystem_read",
            "scope": {"root": str(self.data_dir.resolve())},
            "issuer": "workspace-owner",
            "rate_limit_per_hour": 10,
            "side_effect": False,
            "requires_approval": True,
        }
        with self.assertRaises(HTTPError) as error:
            self.authorized_json("/api/config/capabilities", payload=payload)
        self.assertEqual(error.exception.code, 400)

    def test_legacy_capability_is_listed_as_blocked_for_operator_recovery(self) -> None:
        proposal = CapabilityGrant(
            capability_type="filesystem_read",
            scope={"root": str(self.data_dir.resolve())},
            issuer="workspace-owner",
            rate_limit_per_hour=10,
            side_effect=False,
        )
        record = self.http.capabilities.grant(
            self.settings.subject_id,
            proposal,
            actor="operator",
        )
        legacy = proposal.model_copy(update={"requires_approval": True})
        with self.kernel.database.transaction() as connection:
            row = connection.execute(
                "SELECT created_at FROM capability_grants WHERE grant_id = ?",
                (record.grant_id,),
            ).fetchone()
            assert row is not None
            state_hash = self.http.capabilities._state_hash_values(
                self.settings.subject_id,
                legacy,
                "active",
                row["created_at"],
                None,
                None,
            )
            connection.execute(
                "UPDATE capability_grants SET requires_approval = 1, state_hash = ? "
                "WHERE grant_id = ?",
                (state_hash, record.grant_id),
            )
        status, rows = self.authorized_json("/api/config/capabilities")
        self.assertEqual(status, 200)
        assert isinstance(rows, list)
        listed = next(item for item in rows if item["grant_id"] == record.grant_id)
        self.assertEqual(listed["status"], "active")
        self.assertEqual(listed["effective_status"], "blocked_legacy_approval")

    def test_capability_revoke_rejects_invalid_bodies_before_store_access(self) -> None:
        admin_token = self.settings.admin_token
        assert admin_token is not None
        headers = {
            "Authorization": "Bearer " + admin_token.get_secret_value(),
            "Content-Type": "application/json",
        }
        with patch.object(
            self.http.capabilities,
            "revoke",
            side_effect=AssertionError("invalid request reached the capability store"),
        ) as revoke:
            for body, expected_error in (
                ([], "invalid_json"),
                ({"reason": "   "}, "invalid_capability"),
            ):
                request = Request(
                    f"{self.base_url}/api/config/capabilities/grant-unused/revoke",
                    data=json.dumps(body).encode(),
                    method="POST",
                    headers=headers,
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=5)
                self.assertEqual(raised.exception.code, 400)
                self.assertEqual(
                    json.loads(raised.exception.read()),
                    {"error": expected_error},
                )
        revoke.assert_not_called()

    def test_service_settings_reject_weak_credentials_and_invalid_hash(self) -> None:
        with self.assertRaises(ValidationError):
            ServiceSettings(
                data_dir=self.data_dir,
                subject_id="Noyra-service-test",
                genesis_hash="not-a-valid-hash".ljust(64, "x"),
            )
        with self.assertRaises(ValidationError):
            ServiceSettings(
                data_dir=self.data_dir,
                subject_id="Noyra-service-test",
                genesis_hash=content_hash({"seed": "service-test"}),
                admin_token=SecretStr("too-short"),
            )
        for changes in (
            {"host": " "},
            {"subject_id": "        "},
            {"developer_log_export_enabled": "invalid"},
            {"integrity_mode": "invalid"},
            {
                "integrity_max_bytes_per_check": 1_000_000,
                "integrity_max_value_bytes": 1_000_001,
            },
        ):
            with self.assertRaises(ValidationError):
                ServiceSettings.model_validate(
                    {
                        "data_dir": self.data_dir,
                        "subject_id": "Noyra-service-test",
                        "genesis_hash": content_hash({"seed": "service-test"}),
                        **changes,
                    }
                )
        enabled = ServiceSettings.model_validate(
            {
                "data_dir": self.data_dir,
                "subject_id": "Noyra-service-test",
                "genesis_hash": content_hash({"seed": "service-test"}),
                "developer_log_export_enabled": "true",
            }
        )
        self.assertTrue(enabled.developer_log_export_enabled)
        disabled = ServiceSettings.model_validate(
            {
                "data_dir": self.data_dir,
                "subject_id": "Noyra-service-test",
                "genesis_hash": content_hash({"seed": "service-test"}),
                "developer_log_export_enabled": "false",
            }
        )
        self.assertFalse(disabled.developer_log_export_enabled)
        with self.assertRaises(ValueError):
            self.http._run_export(
                self.settings.subject_id, "invalid", self.data_dir / "invalid.zip"
            )
        for _ in range(self.settings.request_rate_limit_per_minute):
            self.assertTrue(self.http.allow_request("coverage-rate-client"))
        self.assertFalse(self.http.allow_request("coverage-rate-client"))
        for index in range(10_001):
            self.http.allow_request(f"coverage-unique-client-{index}")
        with self.assertRaises(ValidationError):
            ServiceSettings(
                data_dir=self.data_dir,
                subject_id="Noyra-service-test",
                genesis_hash=content_hash({"seed": "service-test"}),
                host="0.0.0.0",
            )

        with (
            patch.dict(os.environ, {"NOYRA_DEVELOPER_LOG_EXPORT_ENABLED": "invalid"}),
            self.assertRaises(ValueError),
        ):
            ServiceSettings.from_env()

    def test_service_settings_load_from_environment(self) -> None:
        environment = {
            "NOYRA_DATA_DIR": str(self.data_dir / "environment"),
            "NOYRA_SUBJECT_ID": "Noyra-environment-test",
            "NOYRA_GENESIS_HASH": content_hash({"seed": "environment-test"}),
            "NOYRA_HOST": "127.0.0.1",
            "NOYRA_PORT": "0",
            "NOYRA_ADMIN_TOKEN": "environment-admin-token-with-sufficient-entropy",
            "NOYRA_ACTIVE_INTERVAL_SECONDS": "1",
            "NOYRA_SLEEP_INTERVAL_SECONDS": "2",
            "NOYRA_DEEP_SLEEP_SECONDS": "3",
            "NOYRA_INTEGRITY_MODE": "pause",
            "NOYRA_INTEGRITY_INTERVAL_SECONDS": "11",
            "NOYRA_INTEGRITY_RETRY_SECONDS": "5",
            "NOYRA_INTEGRITY_STARTUP_DEADLINE_SECONDS": "2",
            "NOYRA_INTEGRITY_PERIODIC_DEADLINE_SECONDS": "3",
            "NOYRA_INTEGRITY_MAX_ROWS_PER_CHECK": "1234",
            "NOYRA_INTEGRITY_MAX_BYTES_PER_CHECK": "2000000",
            "NOYRA_INTEGRITY_MAX_VALUE_BYTES": "1000000",
            "NOYRA_INTEGRITY_MAX_FILES_PER_CHECK": "200",
            "NOYRA_ERROR_BACKOFF_SECONDS": "4",
            "NOYRA_MAX_REQUEST_BYTES": "4096",
            "NOYRA_REQUEST_TIMEOUT_SECONDS": "7",
            "NOYRA_DEVELOPER_LOG_EXPORT_ENABLED": "false",
            "NOYRA_SUBJECT_STORAGE_QUOTA_BYTES": "20000000",
            "NOYRA_TRAINING_STORAGE_QUOTA_BYTES": "30000000",
            "NOYRA_WORKSPACE_STORAGE_QUOTA_BYTES": "40000000",
            "NOYRA_MINIMUM_FREE_STORAGE_BYTES": "10000000",
            "NOYRA_TRAINING_RECORD_ENABLED": "true",
            "NOYRA_TRAINING_EXPORT_ENABLED": "false",
            "NOYRA_TRAINING_INCLUDE_PRIVATE_PSYCHOLOGY": "true",
            "NOYRA_TRAINING_INCLUDE_CONVERSATIONS": "false",
            "NOYRA_TRAINING_INCLUDE_MODEL_IO": "true",
            "NOYRA_TRAINING_INCLUDE_EXTERNAL_ACTIONS": "false",
            "NOYRA_TRAINING_INCLUDE_WORKSPACE": "true",
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = ServiceSettings.from_env()
        self.assertEqual(settings.port, 0)
        self.assertEqual(settings.deep_sleep_seconds, 3)
        self.assertEqual(settings.integrity_mode, "pause")
        self.assertEqual(settings.integrity_interval_seconds, 11)
        self.assertEqual(settings.integrity_retry_seconds, 5)
        self.assertEqual(settings.integrity_startup_deadline_seconds, 2)
        self.assertEqual(settings.integrity_periodic_deadline_seconds, 3)
        self.assertEqual(settings.integrity_max_rows_per_check, 1_234)
        self.assertEqual(settings.integrity_max_bytes_per_check, 2_000_000)
        self.assertEqual(settings.integrity_max_value_bytes, 1_000_000)
        self.assertEqual(settings.integrity_max_files_per_check, 200)
        self.assertEqual(settings.max_request_bytes, 4096)
        self.assertEqual(settings.request_timeout_seconds, 7)
        self.assertFalse(settings.developer_log_export_enabled)
        self.assertEqual(settings.subject_storage_quota_bytes, 20_000_000)
        self.assertTrue(settings.training_record_enabled)
        self.assertFalse(settings.training_export_enabled)
        self.assertTrue(settings.training_include_private_psychology)
        self.assertFalse(settings.training_include_conversations)
        self.assertTrue(settings.training_include_model_io)
        self.assertFalse(settings.training_include_external_actions)
        self.assertTrue(settings.training_include_workspace)

    def test_optional_environment_flag_rejects_invalid_value(self) -> None:
        with (
            patch.dict(os.environ, {"NOYRA_TRAINING_RECORD_ENABLED": "invalid"}),
            self.assertRaises(ValueError),
        ):
            ServiceSettings.from_env()

    def test_service_from_env_builds_remote_cognition_without_calling_provider(self) -> None:
        environment = {
            "NOYRA_DATA_DIR": str(self.data_dir / "cognition-environment"),
            "NOYRA_SUBJECT_ID": "Noyra-cognition-environment",
            "NOYRA_GENESIS_HASH": content_hash({"seed": "cognition-environment"}),
            "NOYRA_HOST": "127.0.0.1",
            "NOYRA_PORT": "0",
            "NOYRA_COGNITION_ENABLED": "true",
            "NOYRA_WORLD_SOURCES_JSON": json.dumps(
                [
                    {
                        "name": "Configured source",
                        "url": "https://example.com/world",
                        "source_type": "news",
                        "trust_score": 0.5,
                    }
                ]
            ),
            "NOYRA_MODEL_PROVIDER": "openai_compatible",
            "NOYRA_MODEL_BASE_URL": "https://api.example.com/v1",
            "NOYRA_MODEL_NAME": "test-model",
            "NOYRA_MODEL_API_KEY": "test-api-key",
            "NOYRA_DAILY_MODEL_CALLS": "10",
            "NOYRA_DAILY_INPUT_TOKENS": "100000",
            "NOYRA_DAILY_OUTPUT_TOKENS": "20000",
            "NOYRA_DAILY_COST_LIMIT": "5",
            "NOYRA_MODEL_INPUT_USD_PER_MILLION": "0",
            "NOYRA_MODEL_OUTPUT_USD_PER_MILLION": "0",
        }
        with patch.dict(os.environ, environment, clear=False):
            service = NoyraService.from_env()
        try:
            self.assertIsNotNone(service.cognition)
            assert service.cognition is not None
            self.assertEqual(service.cognition.gateway.model, "test-model")
            self.assertEqual(
                service.cognition.settings.sources[0].url,
                "https://example.com/world",
            )
        finally:
            if service.cognition is not None:
                asyncio.run(service.cognition.aclose())
            service.http.close()
            service.kernel.close()

    def test_service_from_env_does_not_fallback_after_managed_embedding_integrity_failure(
        self,
    ) -> None:
        environment = {
            "NOYRA_DATA_DIR": str(self.data_dir / "managed-embedding-integrity"),
            "NOYRA_SUBJECT_ID": "Noyra-managed-embedding-integrity",
            "NOYRA_GENESIS_HASH": content_hash({"seed": "managed-embedding-integrity"}),
            "NOYRA_HOST": "127.0.0.1",
            "NOYRA_PORT": "0",
            "NOYRA_COGNITION_ENABLED": "true",
            "NOYRA_WORLD_SOURCES_JSON": json.dumps(
                [
                    {
                        "name": "Integrity fixture source",
                        "url": "https://example.com/world",
                        "source_type": "news",
                        "trust_score": 0.5,
                    }
                ]
            ),
            "NOYRA_MODEL_PROVIDER": "openai_compatible",
            "NOYRA_MODEL_BASE_URL": "https://api.example.com/v1",
            "NOYRA_MODEL_NAME": "test-model",
            "NOYRA_MODEL_API_KEY": "test-api-key",
            "NOYRA_DAILY_MODEL_CALLS": "10",
            "NOYRA_DAILY_INPUT_TOKENS": "100000",
            "NOYRA_DAILY_OUTPUT_TOKENS": "20000",
            "NOYRA_DAILY_COST_LIMIT": "5",
            "NOYRA_MODEL_INPUT_USD_PER_MILLION": "0",
            "NOYRA_MODEL_OUTPUT_USD_PER_MILLION": "0",
            "NOYRA_EMBEDDING_BASE_URL": "https://embedding.example/v1",
            "NOYRA_EMBEDDING_MODEL": "embed-v1",
            "NOYRA_EMBEDDING_API_KEY": "fallback-key-must-not-be-used",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(
                EmbeddingResourceStore,
                "active_settings",
                side_effect=IntegrityError("managed embedding secret fingerprint mismatch"),
            ),
            self.assertRaises(IntegrityError),
        ):
            NoyraService.from_env()

    def test_service_reads_live_model_io_consent_policy(self) -> None:
        settings = ServiceSettings(
            data_dir=self.data_dir / "live-policy",
            subject_id="Noyra-live-policy-test",
            genesis_hash=content_hash({"seed": "live-policy-test"}),
            host="127.0.0.1",
            port=0,
        )
        service = NoyraService(settings)
        try:
            self.assertFalse(service._capture_model_io_enabled())
            service.http.training.update_policy(
                settings.subject_id,
                include_model_io=True,
            )
            self.assertTrue(service._capture_model_io_enabled())
            service.http.training.update_policy(
                settings.subject_id,
                include_model_io=False,
            )
            self.assertFalse(service._capture_model_io_enabled())
        finally:
            service.http.close()
            service.kernel.close()

    def test_http_request_validation_rejects_unsupported_or_malformed_bodies(self) -> None:
        token = "Bearer test-admin-token-with-sufficient-entropy"

        def request(body: bytes, content_type: str = "application/json") -> int:
            connection = http.client.HTTPConnection(self.http.address[0], self.http.address[1], 5)
            connection.request(
                "POST",
                "/api/interactions",
                body=body,
                headers={"Authorization": token, "Content-Type": content_type},
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            return response.status

        self.assertEqual(request(b"{}", "text/plain"), 415)
        self.assertEqual(request(b"{"), 400)
        self.assertEqual(request(b"[]"), 400)
        self.assertEqual(request(json.dumps({"content": 7}).encode()), 400)
        self.assertEqual(
            request(json.dumps({"content": "hello", "idempotency_key": 7}).encode()), 400
        )

        connection = http.client.HTTPConnection(self.http.address[0], self.http.address[1], 5)
        connection.putrequest("POST", "/api/interactions")
        connection.putheader("Authorization", token)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(self.settings.max_request_bytes + 1))
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 413)
        connection.close()

        with self.assertRaises(HTTPError) as error:
            request_object = Request(
                f"{self.base_url}/missing",
                data=b"{}",
                method="POST",
                headers={"Authorization": token, "Content-Type": "application/json"},
            )
            urlopen(request_object, timeout=5)
        self.assertEqual(error.exception.code, 404)

    def test_incoming_http_message_requires_auth_and_remains_an_invitation(self) -> None:
        payload = json.dumps(
            {
                "channel": "web",
                "counterparty": "web-user",
                "content": "Please perform this task.",
                "idempotency_key": "web-message-1",
            }
        ).encode()
        unauthorized = Request(
            f"{self.base_url}/api/interactions",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(unauthorized, timeout=5)
        self.assertEqual(error.exception.code, 401)
        authorized = Request(
            f"{self.base_url}/api/interactions",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer test-admin-token-with-sufficient-entropy",
            },
        )
        with urlopen(authorized, timeout=5) as response:
            result = json.loads(response.read())
        self.assertEqual(result["status"], "offered")
        with self.kernel.database.connection() as connection:
            interaction = connection.execute(
                "SELECT status FROM interactions WHERE interaction_id = ?",
                (result["interaction_id"],),
            ).fetchone()
            actions = connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
            goals = connection.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
        self.assertEqual(interaction["status"], "offered")
        self.assertEqual(actions, 0)
        self.assertEqual(goals, 0)
        self.assertEqual(self.get_json("/api/interactions"), [])
        InteractionStore(self.kernel.database).send(
            self.kernel.subject_id,
            "local",
            "another-person",
            "A newer non-web message must not consume the mailbox limit.",
            idempotency_key="mailbox-filtering-test",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/api/mailbox", timeout=5)
        self.assertEqual(error.exception.code, 401)
        mailbox_request = Request(
            f"{self.base_url}/api/mailbox?limit=1",
            headers={"Authorization": "Bearer test-admin-token-with-sufficient-entropy"},
        )
        with urlopen(mailbox_request, timeout=5) as response:
            mailbox = json.loads(response.read())
        self.assertEqual(mailbox[0]["content"], "Please perform this task.")
        self.assertNotIn("rationale", mailbox[0])
        self.assertNotIn("idempotency_key", mailbox[0])

        forged_transport = json.dumps(
            {
                "channel": "public:web",
                "counterparty": "web-user",
                "content": "Publish this private invitation.",
            }
        ).encode()
        forged_request = Request(
            f"{self.base_url}/api/interactions",
            data=forged_transport,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer test-admin-token-with-sufficient-entropy",
            },
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(forged_request, timeout=5)
        self.assertEqual(error.exception.code, 400)

    def test_goals_endpoint_exposes_goal_state_without_private_pressure(self) -> None:
        evidence = EventStore(self.kernel.database).append(
            self.kernel.subject_id,
            "goal-evidence",
            "test",
            {"observation": "bounded"},
        )
        goal = GoalStore(self.kernel.database).create_candidate(
            self.kernel.subject_id,
            GoalCandidate(
                title="Compare public evidence",
                description="Keep a bounded autonomous research direction.",
                origin="self",
                priority=0.6,
                commitment=0.5,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(evidence.event_id,),
            reason="test autonomous goal",
        )
        goals_status, goals = self.authorized_json("/api/goals")
        self.assertEqual(goals_status, 200)
        assert isinstance(goals, list) and goals and isinstance(goals[0], dict)
        public_goal = goals[0]
        self.assertEqual(public_goal["goal_id"], goal.goal_id)
        self.assertEqual(public_goal["status"], "candidate")
        self.assertNotIn("emotional_pressure", public_goal)
        self.assertNotIn("causal_source_ids", public_goal)
        state = self.get_json("/api/state")
        assert isinstance(state, dict)
        self.assertNotIn("goal_summary", state)
        self.assertNotIn("learning_summary", state)
        self.assertNotIn("Compare public evidence", json.dumps(state))
        details_status, details = self.authorized_json("/api/state/details")
        self.assertEqual(details_status, 200)
        assert isinstance(details, dict) and isinstance(details["goal_summary"], dict)
        self.assertEqual(details["goal_summary"]["candidate_count"], 1)
        self.assertIsNone(details["goal_summary"]["focus"])
        assert isinstance(details["learning_summary"], dict)
        self.assertEqual(details["learning_summary"]["evaluation_count"], 0)

    def test_search_provider_configuration_requires_auth_and_never_returns_secret(self) -> None:
        secret = "search-api-secret-that-must-not-leak"
        payload = {
            "provider_type": "brave",
            "label": "primary-search",
            "api_key": secret,
            "rate_limit_per_hour": 12,
            "extras": {},
        }
        unauthorized = Request(
            f"{self.base_url}/api/config/search-providers",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(unauthorized, timeout=5)
        self.assertEqual(error.exception.code, 401)

        status, created = self.authorized_json("/api/config/search-providers", payload=payload)
        self.assertEqual(status, 201)
        assert isinstance(created, dict)
        self.assertNotIn(secret, json.dumps(created))

        status, records = self.authorized_json("/api/config/search-providers")
        self.assertEqual(status, 200)
        assert isinstance(records, list) and records and isinstance(records[0], dict)
        self.assertEqual(records[0]["label"], "primary-search")
        serialized = json.dumps(records)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("key_reference", serialized)

        config_id = str(created["config_id"])
        status, revoked = self.authorized_json(
            f"/api/config/search-providers/{config_id}/revoke",
            payload={"reason": "operator withdrew the resource"},
        )
        self.assertEqual(status, 200)
        assert isinstance(revoked, dict)
        self.assertEqual(revoked["status"], "revoked")

        missing = Request(
            f"{self.base_url}/api/config/search-providers/searchcfg_missing/revoke",
            data=json.dumps({"reason": "missing"}).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer test-admin-token-with-sufficient-entropy",
                "Content-Type": "application/json",
            },
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(missing, timeout=5)
        self.assertEqual(error.exception.code, 404)

        database_bytes = (self.data_dir / "noyra.sqlite3").read_bytes()
        self.assertNotIn(secret.encode(), database_bytes)

    def test_model_resource_configuration_requires_auth_and_hides_keys(self) -> None:
        secret = "model-resource-secret-that-must-not-leak"
        payload = {
            "pool": "economy",
            "label": "economy-test",
            "base_url": "https://models.example/v1",
            "model": "economy-model",
            "api_keys": [secret, "second-model-resource-secret"],
        }
        unauthorized = Request(
            f"{self.base_url}/api/config/model-resources",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(unauthorized, timeout=5)
        self.assertEqual(error.exception.code, 401)

        status, created = self.authorized_json("/api/config/model-resources", payload=payload)
        self.assertEqual(status, 201)
        self.assertNotIn(secret, json.dumps(created))
        status, records = self.authorized_json("/api/config/model-resources?view=summary")
        self.assertEqual(status, 200)
        assert isinstance(records, list) and records and isinstance(records[0], dict)
        self.assertEqual(records[0]["key_count"], 2)
        group_id = str(records[0]["group_id"])
        status, keys_added = self.authorized_json(
            f"/api/config/model-resources/{group_id}/keys",
            payload={"api_keys": ["third-model-resource-secret"]},
        )
        self.assertEqual(status, 200)
        assert isinstance(keys_added, dict)
        self.assertEqual(keys_added["key_count"], 3)
        status, key_metadata = self.authorized_json(
            f"/api/config/model-resources/{group_id}/keys?view=metadata"
        )
        self.assertEqual(status, 200)
        assert isinstance(key_metadata, list)
        self.assertEqual(len(key_metadata), 3)
        self.assertNotIn("api_key", json.dumps(key_metadata))
        self.assertNotIn("key_reference", json.dumps(key_metadata))
        status, key_rows = self.authorized_json("/api/config/model-resources")
        self.assertEqual(status, 200)
        assert isinstance(key_rows, list) and key_rows
        # Key identifiers are intentionally not exposed by the group list;
        # exercise the lifecycle through the store-backed route below using a
        # direct lookup that never reads the secret value.
        key_records = self.http.cognitive_resources.keys(
            group_id, subject_id=self.settings.subject_id
        )
        revoked_key_id = key_records[0].key_id
        status, revoked = self.authorized_json(
            f"/api/config/model-resources/{group_id}/keys/{revoked_key_id}/revoke",
            payload={"reason": "retire compromised credential"},
        )
        self.assertEqual(status, 200)
        assert isinstance(revoked, dict)
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(
            self.http.cognitive_resources.keys(group_id, subject_id=self.settings.subject_id)[
                0
            ].status,
            "revoked",
        )
        status, changed = self.authorized_json(
            f"/api/config/model-resources/{group_id}/disable?reason=maintenance",
            payload={"reason": "maintenance"},
        )
        self.assertEqual(status, 200)
        assert isinstance(changed, dict)
        self.assertEqual(changed["status"], "disabled")
        serialized = json.dumps(records)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("key_reference", serialized)
        self.assertNotIn(secret.encode(), (self.data_dir / "noyra.sqlite3").read_bytes())

        duplicate_request = Request(
            f"{self.base_url}/api/config/model-resources",
            data=json.dumps({**payload, "label": "economy-test"}).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer test-admin-token-with-sufficient-entropy",
                "Content-Type": "application/json",
            },
        )
        with self.assertRaises(HTTPError) as duplicate_error:
            urlopen(duplicate_request, timeout=5)
        self.assertEqual(duplicate_error.exception.code, 409)
        assert duplicate_error.exception.fp is not None
        self.assertEqual(
            json.loads(duplicate_error.exception.read())["error"],
            "model_resource_label_exists",
        )

    def test_model_resource_budget_update_is_audited_and_keeps_provider_identity(self) -> None:
        status, created = self.authorized_json(
            "/api/config/model-resources",
            payload={
                "pool": "economy",
                "label": "budget-update",
                "base_url": "https://budget.example/v1",
                "model": "budget-model",
                "api_keys": ["budget-update-secret"],
                "daily_attempts": 10,
                "daily_input_tokens": 1000,
                "daily_output_tokens": 500,
                "daily_cost_limit_usd": 1,
            },
        )
        self.assertEqual(status, 201)
        assert isinstance(created, dict)
        group_id = str(created["group_id"])
        status, updated = self.authorized_json(
            f"/api/v1/config/model-resources/{group_id}/update?source=stage3",
            payload={
                "daily_attempts": "25",
                "daily_input_tokens": "9223372036854775807",
                "daily_output_tokens": "1200",
                "daily_cost_limit_usd": "2.500000",
                "priority": "10",
                "weight": "200",
                "reason": "raise budget for staged cognition",
            },
        )
        self.assertEqual(status, 200)
        assert isinstance(updated, dict)
        self.assertEqual(updated["daily_attempts"], 25)
        self.assertEqual(updated["daily_input_tokens"], 9_223_372_036_854_775_807)
        self.assertEqual(updated["daily_output_tokens"], 1200)
        self.assertEqual(updated["daily_cost_microusd"], 2_500_000)
        self.assertEqual(updated["priority"], 10)
        self.assertEqual(updated["weight"], 200)
        self.assertEqual(updated["model"], "budget-model")
        self.assertEqual(
            updated["exact_budget"]["daily_input_tokens"],
            "9223372036854775807",
        )
        status, listed_resources = self.authorized_json("/api/config/model-resources")
        self.assertEqual(status, 200)
        assert isinstance(listed_resources, list)
        listed = next(item for item in listed_resources if item["group_id"] == group_id)
        self.assertEqual(listed["exact_budget"], updated["exact_budget"])
        with self.kernel.database.connection() as connection:
            revisions = connection.execute(
                "SELECT status, reason FROM cognitive_resource_group_revisions "
                "WHERE group_id = ? ORDER BY created_at",
                (group_id,),
            ).fetchall()
        self.assertEqual(
            [(row["status"], row["reason"]) for row in revisions],
            [
                ("active", "operator configured cognitive resource"),
                ("active", "raise budget for staged cognition"),
            ],
        )
        with self.assertRaises(HTTPError) as empty_error:
            self.authorized_json(
                f"/api/config/model-resources/{group_id}/update?source=noop",
                payload={"reason": "no changes"},
            )
        self.assertEqual(empty_error.exception.code, 400)

    def test_model_resource_integrity_failures_return_retryable_503(self) -> None:
        status, created = self.authorized_json(
            "/api/config/model-resources",
            payload={
                "pool": "economy",
                "label": "integrity-unavailable",
                "base_url": "https://integrity.example/v1",
                "model": "integrity-model",
                "api_keys": ["integrity-secret"],
            },
        )
        self.assertEqual(status, 201)
        assert isinstance(created, dict)
        group_id = str(created["group_id"])
        key_id = self.http.cognitive_resources.keys(group_id, subject_id=self.settings.subject_id)[
            0
        ].key_id
        admin_token = self.settings.admin_token
        assert admin_token is not None

        def assert_integrity_unavailable(
            path: str, *, payload: Mapping[str, object] | None = None
        ) -> None:
            request = Request(
                f"{self.base_url}{path}",
                data=json.dumps(payload).encode() if payload is not None else None,
                method="POST" if payload is not None else "GET",
                headers={
                    "Authorization": "Bearer " + admin_token.get_secret_value(),
                    **({"Content-Type": "application/json"} if payload is not None else {}),
                },
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=5)
            error = raised.exception
            self.assertEqual(error.code, 503)
            self.assertEqual(error.headers.get("Retry-After"), "60")
            assert error.fp is not None
            self.assertEqual(
                json.loads(error.read()),
                {"error": "model_resource_integrity_unavailable"},
            )

        with patch.object(
            self.http.cognitive_resources,
            "keys",
            side_effect=IntegrityError("model resource key integrity unavailable"),
        ):
            assert_integrity_unavailable(f"/api/config/model-resources/{group_id}/keys")

        with patch.object(
            self.http.cognitive_resources,
            "update",
            side_effect=IntegrityError("model resource update integrity unavailable"),
        ):
            assert_integrity_unavailable(
                f"/api/config/model-resources/{group_id}/update",
                payload={"priority": 5, "reason": "integrity regression test"},
            )

        with patch.object(
            self.http.cognitive_resources,
            "revoke_key",
            side_effect=IntegrityError("model resource key revoke integrity unavailable"),
        ):
            assert_integrity_unavailable(
                f"/api/config/model-resources/{group_id}/keys/{key_id}/revoke",
                payload={"reason": "integrity regression test"},
            )

    def test_model_resource_routes_fail_closed_for_malformed_paths_and_bodies(self) -> None:
        """Malformed resource URLs must answer promptly instead of hanging."""

        token = "Bearer " + "test-admin-token-with-sufficient-" + "entropy"

        # Path shape must not disclose whether a resource exists before the
        # caller has authenticated.  Every malformed URL in the protected
        # namespace therefore has the same unauthorized response.
        for path in (
            "/api/config/model-resources/missing/unknown",
            "/api/config/model-resources/missing/keys/extra",
            "/api/config/model-resources//keys",
        ):
            with self.assertRaises(HTTPError) as error:
                urlopen(f"{self.base_url}{path}", timeout=2)
            self.assertEqual(error.exception.code, 401, path)

        for path in (
            "/api/config/model-resources/missing/unknown",
            "/api/config/model-resources/missing/keys/extra",
            "/api/config/model-resources//keys",
        ):
            request = Request(
                f"{self.base_url}{path}",
                headers={"Authorization": token},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 404, path)

        # The resource mutation handlers must reject a JSON scalar before
        # trying to access mapping methods such as ``get``.
        for path in (
            "/api/config/model-resources/missing/update",
            "/api/config/model-resources/missing/keys",
            "/api/config/model-resources/missing/keys/key-missing/revoke",
        ):
            request = Request(
                f"{self.base_url}{path}",
                data=b"[]",
                method="POST",
                headers={
                    "Authorization": token,
                    "Content-Type": "application/json",
                },
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 400, path)

        # Empty reasons are not meaningful audit records and must be rejected
        # consistently across status and update operations.
        for operation in ("disable", "enable", "revoke"):
            request = Request(
                f"{self.base_url}/api/config/model-resources/missing/{operation}",
                data=json.dumps({"reason": "   "}).encode(),
                method="POST",
                headers={
                    "Authorization": token,
                    "Content-Type": "application/json",
                },
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 400, operation)

        # Mutation payloads are closed schemas.  Validate the schema before
        # looking up the resource so an unknown group cannot turn an extra
        # field into a misleading 404 (or an unhandled exception).
        for path, payload in (
            (
                "/api/config/model-resources/missing/keys",
                {"api_keys": ["key"], "unexpected": 1},
            ),
            (
                "/api/config/model-resources/missing/disable",
                {"reason": "maintenance", "unexpected": 1},
            ),
            (
                "/api/config/model-resources/missing/test",
                {"reason": "probe", "unexpected": 1},
            ),
        ):
            request = Request(
                f"{self.base_url}{path}",
                data=json.dumps(payload).encode(),
                method="POST",
                headers={
                    "Authorization": token,
                    "Content-Type": "application/json",
                },
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 400, path)

    def test_diagnostics_exposes_pending_interaction_reason_without_content(self) -> None:
        invitation = InteractionStore(self.kernel.database).receive(
            self.kernel.subject_id,
            "web",
            "web-user",
            "private message must not be copied into diagnostics",
            idempotency_key="diagnostics-pending-message",
        )
        status, diagnostics = self.authorized_json("/api/diagnostics")
        self.assertEqual(status, 200)
        assert isinstance(diagnostics, dict)
        pending = diagnostics["cognition"]["pending_interactions"]
        self.assertEqual(pending[0]["interaction_id"], invitation.interaction_id)
        self.assertEqual(pending[0]["status"], "offered")
        self.assertNotIn("content", pending[0])

    def test_model_resource_test_endpoint_is_explicit_and_targets_only_requested_resource(
        self,
    ) -> None:
        first_status, first = self.authorized_json(
            "/api/config/model-resources",
            payload={
                "pool": "economy",
                "label": "probe-first",
                "base_url": "https://first.example/v1",
                "model": "probe-first-model",
                "api_keys": ["probe-first-secret"],
                "max_attempts": 1,
            },
        )
        second_status, second = self.authorized_json(
            "/api/config/model-resources",
            payload={
                "pool": "economy",
                "label": "probe-second",
                "base_url": "https://second.example/v1",
                "model": "probe-second-model",
                "api_keys": ["probe-second-secret"],
                "max_attempts": 1,
            },
        )
        self.assertEqual(first_status, 201)
        self.assertEqual(second_status, 201)
        assert isinstance(first, dict) and isinstance(second, dict)
        first_group_id = str(first["group_id"])
        second_group_id = str(second["group_id"])

        first_provider = FakeProvider([ProviderResponse('{"ok":true}', ModelUsage(2, 1))])
        second_provider = FakeProvider(
            [
                ProviderCallError(
                    "provider_http_401",
                    retryable=False,
                    outcome_unknown=False,
                )
            ]
        )
        providers = {
            "probe-first-model": first_provider,
            "probe-second-model": second_provider,
        }
        gateway = RoutedModelGateway(
            self.kernel.database,
            self.kernel.subject_id,
            self.http.cognitive_resources,
            provider_factory=lambda settings: providers[settings.model],
        )

        self.http.cognition_gateway = gateway
        status, result = self.authorized_json(
            f"/api/config/model-resources/{first_group_id}/test",
            payload={"reason": "operator endpoint regression test"},
        )
        self.assertEqual(status, 200)
        assert isinstance(result, dict)
        self.assertEqual(result["group_id"], first_group_id)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(first_provider.requests), 1)
        self.assertEqual(len(second_provider.requests), 0)

        with self.assertRaises(HTTPError) as missing_error:
            self.authorized_json(
                "/api/config/model-resources/cgroup_missing/test",
                payload={"reason": "missing resource"},
            )
        self.assertEqual(missing_error.exception.code, 404)

        with self.assertRaises(HTTPError) as provider_error:
            self.authorized_json(
                f"/api/config/model-resources/{second_group_id}/test",
                payload={"reason": "invalid provider regression test"},
            )
        self.assertEqual(provider_error.exception.code, 502)
        assert provider_error.exception.fp is not None
        self.assertEqual(json.loads(provider_error.exception.read())["error"], "provider_http_401")

        with self.kernel.database.connection() as connection:
            call = connection.execute(
                "SELECT purpose, resource_group_id, status, error_code "
                "FROM model_calls WHERE subject_id = ? "
                "AND purpose LIKE 'operator_model_test:%' "
                "ORDER BY created_at DESC LIMIT 1",
                (self.kernel.subject_id,),
            ).fetchone()
        self.assertEqual(call["resource_group_id"], second_group_id)
        self.assertEqual(call["status"], "failed")
        self.assertEqual(call["error_code"], "provider_http_401")

    def test_model_resource_test_endpoint_reports_missing_cognition_gateway(self) -> None:
        status, created = self.authorized_json(
            "/api/config/model-resources",
            payload={
                "pool": "economy",
                "label": "probe-no-gateway",
                "base_url": "https://nogateway.example/v1",
                "model": "probe-no-gateway-model",
                "api_keys": ["probe-no-gateway-secret"],
            },
        )
        self.assertEqual(status, 201)
        assert isinstance(created, dict)
        with self.assertRaises(HTTPError) as error:
            self.authorized_json(
                f"/api/config/model-resources/{created['group_id']}/test",
                payload={"reason": "cognition disabled"},
            )
        self.assertEqual(error.exception.code, 503)

    def test_model_resource_test_endpoint_reports_runtime_unavailable(self) -> None:
        status, created = self.authorized_json(
            "/api/config/model-resources",
            payload={
                "pool": "economy",
                "label": "probe-runtime-unavailable",
                "base_url": "https://runtime-unavailable.example/v1",
                "model": "probe-runtime-unavailable-model",
                "api_keys": ["probe-runtime-unavailable-secret"],
            },
        )
        self.assertEqual(status, 201)
        assert isinstance(created, dict)
        self.http.cognition_gateway = Mock()
        original_begin = self.kernel.admission.begin

        def reject_only_model_test(kind: str, *, allow_quarantine: bool = False) -> Any:
            if kind == "operator_model_test":
                raise OperationInvalidated("runtime is draining")
            return original_begin(kind, allow_quarantine=allow_quarantine)

        with (
            patch.object(
                self.kernel.admission,
                "begin",
                side_effect=reject_only_model_test,
            ),
            self.assertRaises(HTTPError) as error,
        ):
            self.authorized_json(
                f"/api/config/model-resources/{created['group_id']}/test",
                payload={"reason": "runtime unavailable regression test"},
            )
        self.assertEqual(error.exception.code, 409)
        assert error.exception.fp is not None
        self.assertEqual(json.loads(error.exception.read()), {"error": "runtime_unavailable"})

    def test_bearer_tokens_reject_non_ascii_values(self) -> None:
        with self.assertRaises(ValueError, msg="ASCII-only tokens prevent HTTP header failures"):
            ServiceSettings(
                data_dir=self.data_dir,
                subject_id="Noyra-service-token-test",
                genesis_hash=content_hash({"seed": "token"}),
                admin_token=SecretStr("令牌" * 16),
            )

    def test_admin_session_authenticates_private_surface_and_requires_csrf(self) -> None:
        with urlopen(f"{self.base_url}/admin", timeout=5) as response:
            admin_html = response.read().decode()
            self.assertIn("管理台", admin_html)
        login = Request(
            f"{self.base_url}/admin/session",
            data=json.dumps({"token": "test-admin-token-with-sufficient-entropy"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(login, timeout=5) as response:
            self.assertEqual(response.status, 200)
            session_payload = json.loads(response.read())
            set_cookie = response.headers["Set-Cookie"]
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("SameSite=Strict", set_cookie)
            self.assertIn("Path=/", set_cookie)
            cookie = set_cookie.split(";", 1)[0]
        self.assertTrue(session_payload["csrf_token"])
        no_cookie_status = Request(f"{self.base_url}/admin/session")
        with urlopen(no_cookie_status, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"authenticated": False})
        session_status = Request(
            f"{self.base_url}/admin/session",
            headers={"Cookie": cookie},
        )
        with urlopen(session_status, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                json.loads(response.read())["csrf_token"], session_payload["csrf_token"]
            )
        private_get = Request(
            f"{self.base_url}/api/diagnostics",
            headers={"Cookie": cookie},
        )
        with urlopen(private_get, timeout=5) as response:
            self.assertEqual(response.status, 200)
        post_without_csrf = Request(
            f"{self.base_url}/api/interactions",
            data=json.dumps(
                {
                    "channel": "web",
                    "counterparty": "web-user",
                    "content": "csrf boundary",
                    "idempotency_key": "admin-session-csrf-missing",
                }
            ).encode(),
            method="POST",
            headers={"Cookie": cookie, "Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as csrf_error:
            urlopen(post_without_csrf, timeout=5)
        self.assertEqual(csrf_error.exception.code, 401)
        post_with_csrf = Request(
            f"{self.base_url}/api/interactions",
            data=json.dumps(
                {
                    "channel": "web",
                    "counterparty": "web-user",
                    "content": "csrf boundary",
                    "idempotency_key": "admin-session-csrf-valid",
                }
            ).encode(),
            method="POST",
            headers={
                "Cookie": cookie,
                "Content-Type": "application/json",
                "X-CSRF-Token": session_payload["csrf_token"],
            },
        )
        with urlopen(post_with_csrf, timeout=5) as response:
            self.assertEqual(response.status, 201)

    def test_embedding_resource_configuration_is_independent(self) -> None:
        secret = "embedding-api-key-that-must-not-export"
        status, created = self.authorized_json(
            "/api/config/embedding-resources",
            payload={
                "label": "semantic-provider",
                "base_url": "https://embedding.example/v1",
                "model": "embed-v1",
                "api_key": secret,
                "dimensions": 768,
            },
        )
        self.assertEqual(status, 201)
        assert isinstance(created, dict)
        config_id = str(created["config_id"])
        self.assertNotIn(secret, json.dumps(created))
        status, records = self.authorized_json("/api/config/embedding-resources")
        self.assertEqual(status, 200)
        assert isinstance(records, list) and records
        self.assertEqual(records[0]["model"], "embed-v1")
        self.assertNotIn(secret, json.dumps(records))
        status, changed = self.authorized_json(
            f"/api/config/embedding-resources/{config_id}/disable",
            payload={"reason": "maintenance"},
        )
        self.assertEqual(status, 200)
        assert isinstance(changed, dict)
        self.assertEqual(changed["status"], "disabled")
        self.assertNotIn(secret.encode(), (self.data_dir / "noyra.sqlite3").read_bytes())

    def test_runtime_logs_and_complete_export_require_auth_and_redact_secrets(self) -> None:
        model_secret = "model-api-key-that-must-never-export"
        search_secret = "search-api-key-that-must-never-export"
        admin_secret = "test-admin-token-with-sufficient-entropy"
        EventStore(self.kernel.database).append(
            self.kernel.subject_id,
            "diagnostic-event",
            "test",
            {
                "message": f"Authorization: Bearer {model_secret}",
                "api_key": search_secret,
                "admin_token": admin_secret,
            },
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/api/runtime-logs", timeout=5)
        self.assertEqual(error.exception.code, 401)
        status, logs = self.authorized_json("/api/runtime-logs")
        self.assertEqual(status, 200)
        assert isinstance(logs, dict) and logs["items"]
        self.assertIn("next_cursor", logs)
        overflow_cursor = PublicProjection._encode_runtime_log_cursor(
            "2026-08-17T00:00:00.000+00:00",
            "event",
            "overflow-anchor",
            {source[0]: 1 << 63 for source in PublicProjection.RUNTIME_LOG_SOURCES},
        )
        for path, expected in (
            ("/api/runtime-logs?offset=1", "offset_pagination_unsupported"),
            ("/api/runtime-logs?cursor=invalid", "invalid_cursor"),
            (f"/api/runtime-logs?cursor={overflow_cursor}", "invalid_cursor"),
        ):
            request = Request(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {admin_secret}"},
            )
            with self.assertRaises(HTTPError) as bad_request:
                urlopen(request, timeout=5)
            self.assertEqual(bad_request.exception.code, 400)
            self.assertEqual(json.loads(bad_request.exception.read()), {"error": expected})

        request = Request(
            f"{self.base_url}/api/admin/runtime-export",
            headers={"Authorization": f"Bearer {admin_secret}"},
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
            created = json.loads(response.read())
        self.assertIsInstance(created, dict)
        job_id = str(created["job_id"])
        job = self.wait_for_export_job(job_id)
        with urlopen(
            Request(
                f"{self.base_url}/api/admin/export-jobs/{job_id}/download",
                headers={"Authorization": f"Bearer {admin_secret}"},
            ),
            timeout=5,
        ) as response:
            self.assertEqual(response.headers["Content-Type"], "application/zip")
            self.assertEqual(response.headers["X-Archive-SHA256"], job["sha256"])
            archive_bytes = response.read()
        self.assertNotIn(model_secret.encode(), archive_bytes)
        self.assertNotIn(search_secret.encode(), archive_bytes)
        self.assertNotIn(admin_secret.encode(), archive_bytes)
        with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
            names = archive.namelist()
            self.assertIn("manifest.json", names)
            self.assertIn("tables/events.jsonl", names)
            self.assertIn("tables/model_calls.jsonl", names)
            self.assertIn("tables/self_models.jsonl", names)
            self.assertIn("tables/thought_agenda_items.jsonl", names)
            self.assertIn("tables/thought_episodes.jsonl", names)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["schema_version"], CURRENT_SCHEMA_VERSION)
            self.assertIn("tables/autonomous_projects.jsonl", names)
            self.assertIn("tables/autonomous_project_phases.jsonl", names)
            self.assertIn("tables/value_profiles.jsonl", names)
            self.assertIn("tables/mission_candidates.jsonl", names)
            self.assertIn("tables/motivation_reviews.jsonl", names)
            self.assertIn("tables/metacognitive_decisions.jsonl", names)
            self.assertIn("tables/metacognitive_outcomes.jsonl", names)
            self.assertTrue(manifest["tables"])
            for entry in manifest["tables"]:
                self.assertEqual(
                    entry["sha256"],
                    hashlib.sha256(archive.read(entry["file"])).hexdigest(),
                )
            exported = b"\n".join(archive.read(name) for name in names)
        self.assertNotIn(model_secret.encode(), exported)
        self.assertNotIn(search_secret.encode(), exported)
        self.assertNotIn(admin_secret.encode(), exported)
        self.assertIn(b"[REDACTED]", exported)
        with self.kernel.database.connection() as connection:
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM audit_records WHERE action = 'runtime_exported'"
            ).fetchone()[0]
        self.assertEqual(audit_count, 1)

    def test_training_policy_and_export_are_authenticated(self) -> None:
        EventStore(self.kernel.database).append(
            self.kernel.subject_id,
            "training-public-event",
            "test",
            {"value": 1},
            privacy_level="public",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/api/config/training-policy", timeout=5)
        self.assertEqual(error.exception.code, 401)
        status, policy = self.authorized_json("/api/config/training-policy")
        self.assertEqual(status, 200)
        assert isinstance(policy, dict)
        self.assertTrue(policy["record_enabled"])
        missing_version = Request(
            f"{self.base_url}/api/config/training-policy",
            data=json.dumps({"include_conversations": True}).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer test-admin-token-with-sufficient-entropy",
                "Content-Type": "application/json",
            },
        )
        with self.assertRaises(HTTPError) as missing_error:
            urlopen(missing_version, timeout=5)
        self.assertEqual(missing_error.exception.code, 400)
        status, updated = self.authorized_json(
            "/api/config/training-policy",
            payload={
                "expected_version": policy["policy_version"],
                "include_conversations": True,
            },
        )
        self.assertEqual(status, 200)
        assert isinstance(updated, dict)
        self.assertTrue(updated["include_conversations"])
        stale_version = Request(
            f"{self.base_url}/api/config/training-policy",
            data=json.dumps(
                {
                    "expected_version": policy["policy_version"],
                    "include_workspace": True,
                }
            ).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer test-admin-token-with-sufficient-entropy",
                "Content-Type": "application/json",
            },
        )
        with self.assertRaises(HTTPError) as stale_error:
            urlopen(stale_version, timeout=5)
        self.assertEqual(stale_error.exception.code, 409)
        conflict = json.loads(stale_error.exception.read())
        self.assertEqual(conflict["error"], "training_policy_version_conflict")
        self.assertFalse(conflict["current"]["include_workspace"])

        request = Request(
            f"{self.base_url}/api/admin/training-export",
            headers={"Authorization": "Bearer test-admin-token-with-sufficient-entropy"},
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
            created = json.loads(response.read())
        self.assertIsInstance(created, dict)
        job_id = str(created["job_id"])
        job = self.wait_for_export_job(job_id)
        with urlopen(
            Request(
                f"{self.base_url}/api/admin/export-jobs/{job_id}/download",
                headers={"Authorization": "Bearer test-admin-token-with-sufficient-entropy"},
            ),
            timeout=5,
        ) as response:
            self.assertEqual(response.headers["Content-Type"], "application/zip")
            self.assertEqual(response.headers["X-Archive-SHA256"], job["sha256"])
            content = response.read()
        with zipfile.ZipFile(BytesIO(content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            events = archive.read("events.jsonl")
        self.assertGreaterEqual(manifest["row_count"], 1)
        self.assertIn(b"training-public-event", events)

    def test_legacy_export_route_enqueues_without_blocking_request_worker(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocked_worker(
            _subject: str, _kind: str, target: Path, _control: Any
        ) -> tuple[str, str]:
            started.set()
            if not release.wait(5):
                raise RuntimeError("test worker was not released")
            target.write_bytes(b"compatibility-artifact")
            return "compatibility.zip", "compatibility-digest"

        self.http.export_jobs.worker = blocked_worker
        try:
            started_at = time.monotonic()
            status, created = self.authorized_json("/api/admin/runtime-export")
            elapsed = time.monotonic() - started_at
            self.assertEqual(status, 202)
            assert isinstance(created, dict)
            self.assertLess(elapsed, 2.0)
            self.assertTrue(started.wait(2))
            release.set()
            job = self.wait_for_export_job(str(created["job_id"]))
            self.assertEqual(job["status"], "completed")
        finally:
            release.set()

    def test_export_jobs_are_authenticated_bounded_and_downloadable(self) -> None:
        with self.assertRaises(HTTPError) as error:
            urlopen(
                Request(
                    f"{self.base_url}/api/admin/export-jobs",
                    data=json.dumps({"kind": "runtime"}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                ),
                timeout=5,
            )
        self.assertEqual(error.exception.code, 401)

        status, created = self.authorized_json(
            "/api/admin/export-jobs", payload={"kind": "runtime"}
        )
        self.assertEqual(status, 202)
        assert isinstance(created, dict)
        job_id = str(created["job_id"])
        self.assertIn(created["status"], {"queued", "running", "completed"})

        deadline = time.monotonic() + 10
        job: dict[str, Any] = created
        while time.monotonic() < deadline:
            status, current = self.authorized_json(f"/api/admin/export-jobs/{job_id}")
            self.assertEqual(status, 200)
            assert isinstance(current, dict)
            job = current
            if job["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        self.assertEqual(job["status"], "completed")
        status, records = self.authorized_json("/api/admin/export-jobs")
        self.assertEqual(status, 200)
        assert isinstance(records, list)
        self.assertTrue(any(record["job_id"] == job_id for record in records))

        with urlopen(
            Request(
                f"{self.base_url}/api/admin/export-jobs/{job_id}/download",
                headers={"Authorization": "Bearer test-admin-token-with-sufficient-entropy"},
            ),
            timeout=5,
        ) as response:
            content = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "application/zip")
            self.assertEqual(response.headers["X-Archive-SHA256"], job["sha256"])
        self.assertGreater(len(content), 100)

        with self.assertRaises(HTTPError) as error:
            self.authorized_json("/api/admin/export-jobs", payload={"kind": "invalid"})
        self.assertEqual(error.exception.code, 400)

    def test_self_modification_status_is_read_only_and_authenticated(self) -> None:
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/api/self-modification", timeout=5)
        self.assertEqual(error.exception.code, 401)
        status, records = self.authorized_json("/api/self-modification")
        self.assertEqual(status, 200)
        self.assertEqual(records, [])

    def test_service_boots_runtime_to_active(self) -> None:
        other = ServiceSettings(
            data_dir=Path(self.temp_dir.name) / "other",
            subject_id="Noyra-service-other",
            genesis_hash=content_hash({"seed": "service-other"}),
            host="127.0.0.1",
            port=0,
        )
        service = NoyraService(other)
        service.boot()
        self.assertEqual(service.kernel.lifecycle.current().state, "active")
        service.http.start()
        service.http.start()
        service.http.close()
        service.http.close()
        service.kernel.close()

    def test_boot_syncs_explicit_training_policy_and_creates_checkpoint(self) -> None:
        settings = ServiceSettings(
            data_dir=Path(self.temp_dir.name) / "policy-service",
            subject_id="Noyra-policy-service",
            genesis_hash=content_hash({"seed": "policy-service"}),
            host="127.0.0.1",
            port=0,
            training_export_enabled=False,
            training_include_private_psychology=True,
            training_include_model_io=True,
        )
        service = NoyraService(settings)
        try:
            service.boot()
            policy = service.http.training.policy(settings.subject_id)
            self.assertFalse(policy.export_enabled)
            self.assertTrue(policy.include_private_psychology)
            self.assertTrue(policy.include_model_io)
            snapshot = service.kernel.snapshot_store.latest(settings.subject_id)
            self.assertEqual(snapshot.state_version, 1)
            self.assertIsNone(asyncio.run(service._active_tick()))
        finally:
            service.http.server.server_close()
            service.kernel.close()

    def test_cloud_archive_configuration_failure_aborts_startup(self) -> None:
        environment = {
            "NOYRA_DATA_DIR": str(self.data_dir / "cloud-fallback"),
            "NOYRA_SUBJECT_ID": "Noyra-cloud-fallback",
            "NOYRA_GENESIS_HASH": content_hash({"seed": "cloud-fallback"}),
            "NOYRA_PORT": "0",
            "NOYRA_COGNITION_ENABLED": "false",
            "NOYRA_ARCHIVE_S3_BUCKET": "configured-but-unavailable",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch("noyra.service.S3ArchiveProvider.from_env", side_effect=RuntimeError("offline")),
            self.assertRaisesRegex(RuntimeError, "cloud install profile is unavailable"),
        ):
            NoyraService.from_env()

    def test_cloud_archive_configuration_reports_ready_profile(self) -> None:
        environment = {
            "NOYRA_DATA_DIR": str(self.data_dir / "cloud-profile"),
            "NOYRA_SUBJECT_ID": "Noyra-cloud-profile",
            "NOYRA_GENESIS_HASH": content_hash({"seed": "cloud-profile"}),
            "NOYRA_PORT": "0",
            "NOYRA_COGNITION_ENABLED": "false",
            "NOYRA_ARCHIVE_S3_BUCKET": "configured-cloud",
            "NOYRA_INSTALL_PROFILE": "cloud",
        }
        provider = Mock()
        with (
            patch.dict(os.environ, environment, clear=False),
            patch("noyra.service.S3ArchiveProvider.from_env", return_value=provider),
        ):
            service = NoyraService.from_env()
        try:
            self.assertIs(service.cloud_archives.provider, provider)
            self.assertEqual(
                service.http.cloud_archive_status,
                {"configured": True, "ready": True, "profile": "cloud"},
            )
        finally:
            service.http.server.server_close()
            service.kernel.close()

    def test_sleep_reflection_delegates_to_enabled_cognition(self) -> None:
        settings = ServiceSettings(
            data_dir=Path(self.temp_dir.name) / "reflection-service",
            subject_id="Noyra-reflection-service",
            genesis_hash=content_hash({"seed": "reflection-service"}),
            host="127.0.0.1",
            port=0,
        )
        service = NoyraService(settings)

        class CognitionStub:
            def __init__(self) -> None:
                self.calls = 0

            async def reflect_sleep(self, _: object) -> SleepReflectionPlan:
                self.calls += 1
                return SleepReflectionPlan(summary="A delegated bounded reflection.")

        try:
            service.boot()
            sleep = SleepEngine(service.kernel.database, settings.subject_id)
            run = sleep.start("subject_choice", "test delegation")
            run = sleep.begin_reflection(run.sleep_id)
            cognition = CognitionStub()
            service.cognition = cast(Any, cognition)
            plan = asyncio.run(service._sleep_reflection(run))
            self.assertEqual(plan.summary, "A delegated bounded reflection.")
            self.assertEqual(cognition.calls, 1)
        finally:
            service.http.close()
            service.kernel.close()

    def test_service_run_accepts_a_graceful_shutdown_request(self) -> None:
        async def exercise() -> None:
            settings = ServiceSettings(
                data_dir=Path(self.temp_dir.name) / "run-service",
                subject_id="Noyra-run-service",
                genesis_hash=content_hash({"seed": "run-service"}),
                host="127.0.0.1",
                port=0,
                active_interval_seconds=1,
                sleep_interval_seconds=1,
                error_backoff_seconds=1,
            )
            service = NoyraService(settings)
            task = asyncio.create_task(service.run())
            deadline = time.monotonic() + 30
            while service.http.thread is None and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertIsNotNone(service.http.thread)
            service.request_shutdown()
            await asyncio.wait_for(task, timeout=5)
            self.assertIsNone(service.http.thread)
            self.assertFalse(service.kernel.process_lock.held)

        asyncio.run(exercise())

    def test_service_loop_advances_a_restart_safe_sleep_cycle(self) -> None:
        async def exercise() -> None:
            from noyra.sleep import FatigueInputs, FatigueTracker

            settings = ServiceSettings(
                data_dir=Path(self.temp_dir.name) / "sleep-service",
                subject_id="Noyra-sleep-service",
                genesis_hash=content_hash({"seed": "sleep-service"}),
                host="127.0.0.1",
                port=0,
                active_interval_seconds=1,
                sleep_interval_seconds=1,
                deep_sleep_seconds=0,
                error_backoff_seconds=1,
            )
            service = NoyraService(settings)
            try:
                service.boot()
                FatigueTracker(service.kernel.database).assess(
                    settings.subject_id,
                    FatigueInputs(
                        resource_pressure=0.0,
                        cognitive_load=1.0,
                        frustration=1.0,
                        goal_conflict=1.0,
                        staleness=1.0,
                    ),
                    reason="test sleep pressure",
                )
                self.assertEqual((await service.loop.tick()).action, "sleep_requested")
                self.assertEqual((await service.loop.tick()).action, "reflection_started")
                self.assertEqual((await service.loop.tick()).action, "reflection_committed")
                self.assertEqual((await service.loop.tick()).action, "deep_sleep_entered")
                self.assertEqual((await service.loop.tick()).action, "wake_started")
                self.assertEqual((await service.loop.tick()).action, "wake_completed")
                self.assertEqual(service.kernel.lifecycle.current().state, "active")
            finally:
                service.http.server.server_close()
                service.kernel.close()

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
