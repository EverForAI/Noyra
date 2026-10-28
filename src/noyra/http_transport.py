"""HTTP framing, sessions, admission, webhooks and response IO."""

from __future__ import annotations

import hashlib
import hmac
import html
import io
import json
import logging
import time
from collections.abc import Mapping
from contextlib import suppress
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qs, quote, unquote, urlsplit

from pydantic import SecretStr

from noyra.core.admission import OperationInvalidated, bind_lease
from noyra.core.at_rest import AtRestError
from noyra.core.deadline import operation_deadline
from noyra.core.errors import IntegrityError, NotFoundError, RuntimeOwnershipError
from noyra.core.operator_controls import OperatorControlConflict
from noyra.core.request_io import DeadlineRequestReader
from noyra.http_access_logging import http_log_fields
from noyra.http_routes import (
    cognition,
    exports,
    get,
    interaction,
    knowledge,
    operator,
    post,
    public_posts,
    search,
    wallet_automation,
    wallet_payments,
    wallet_resources,
    wallet_rewards,
)
from noyra.http_routes.auth import _AdminSession as _AdminSession
from noyra.http_routes.auth import _ascii_token_equal as _ascii_token_equal
from noyra.http_routes.auth import (
    _verify_trusted_proxy_principal as _verify_trusted_proxy_principal,
)
from noyra.interaction import (
    ADAPTERS,
    REPLAY_WINDOW_SECONDS,
    InboundAuthenticationError,
    InboundChallenge,
    InboundIdentityObserved,
    InboundIgnored,
    WeChatInboundAdapter,
)
from noyra.interaction.public_archive import ARCHIVE_CONTRACT, PublicQueryBudgetExceeded
from noyra.interaction.public_contract import (
    PUBLIC_CACHE_CONTROL,
    PUBLIC_CONTRACT_ID,
    PUBLIC_MAX_RESPONSE_BYTES,
)
from noyra.interaction.public_reading import PUBLIC_VIEWS, public_item_path
from noyra.model.reasoning import ReasoningEffort

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

LOGGER = logging.getLogger("noyra.service")


class PublicArticleRenderer(Protocol):
    def __call__(
        self,
        item: dict[str, Any],
        site_url: str,
        *,
        english: bool = False,
        theme: str = "system",
        return_view: str | None = None,
    ) -> bytes: ...


def handler_type(
    owner: NoyraHTTPServer,
    *,
    web_root: Path,
    public_article_html: PublicArticleRenderer,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "NoyraHTTP/0.1.0"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(owner.settings.request_timeout_seconds)
            self.rfile = io.BufferedReader(
                DeadlineRequestReader(
                    self.rfile, self.connection, owner.settings.request_timeout_seconds
                )
            )

        def handle_one_request(self) -> None:
            self._log_request_parsed = False
            with operation_deadline(owner.settings.request_timeout_seconds):
                super().handle_one_request()

        def parse_request(self) -> bool:
            self._log_request_parsed = super().parse_request()
            return self._log_request_parsed

        def do_GET(self) -> None:
            return get.dispatch(self, owner)

        def do_POST(self) -> None:
            if not owner.allow_request(
                owner.client_ip(str(self.client_address[0]), self.headers.get("X-Forwarded-For"))
            ):
                self._discard_small_request_body()
                self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"})
                return
            # Route admission and dispatch must never include a query
            # string. Keep original target for webhook query signatures.
            self._request_target = self.path
            request_path = urlsplit(self.path).path
            if request_path == "/admin/session":
                self._create_admin_session()
                return
            if request_path == "/admin/session/logout":
                self._logout_admin_session()
                return
            if request_path.startswith("/api/v1/"):
                normalized_path = request_path.replace("/api/v1/", "/api/", 1)
                if normalized_path.startswith("/api/webhooks/"):
                    self.path = normalized_path.replace("/api/webhooks/", "/webhooks/", 1)
                else:
                    self.path = normalized_path
            else:
                self.path = request_path
            if self.path.startswith("/webhooks/"):
                self._receive_inbound_webhook()
                return
            if not self._at_rest_available():
                self._discard_small_request_body()
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "at_rest_boundary_unavailable"},
                )
                return
            if not owner.allow_mutation(self.path):
                self.close_connection = True
                self._discard_small_request_body()
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "integrity_quarantine"},
                )
                return
            # Lifecycle controls intentionally invalidate/reopen the gate
            # as part of their own transaction.  They must not run under
            # a lease that they invalidate themselves.
            if self.path in {
                "/api/admin/lifecycle/pause",
                "/api/admin/lifecycle/resume",
                "/api/admin/lifecycle/reset",
                "/api/admin/lifecycle/wake",
            }:
                try:
                    with owner.admission.lifecycle_control_scope():
                        if not owner.allow_mutation(self.path):
                            raise OperationInvalidated(
                                "lifecycle control crossed integrity quarantine"
                            )
                        self._dispatch_post()
                except OperationInvalidated:
                    self.close_connection = True
                    self._discard_small_request_body()
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "integrity_quarantine"},
                    )
                return
            # Migration cutover and rollback are privileged controls that
            # must remain usable while the durable migration epoch fences
            # ordinary runtime admission.  The coordinator still requires
            # the process ownership check and performs its own drain;
            # this scope does not grant a general HTTP bypass.
            migration_control = owner._is_migration_control(self.path)
            if migration_control:
                # CutoverCoordinator owns the control scope so the
                # authenticated HTTP route cannot accidentally nest a
                # second scope around its durable epoch transition.
                try:
                    self._dispatch_post()
                except (OperationInvalidated, RuntimeOwnershipError):
                    self.close_connection = True
                    self._discard_small_request_body()
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "migration_control_unavailable"},
                    )
                return
            # Only explicit recovery endpoints may bypass a paused or
            # quarantined admission.  Deriving this from the current
            # gate state would let a race after ``allow_mutation`` turn
            # an ordinary POST into an unfenced write.
            allow_recovery = owner._is_recovery_mutation(self.path)
            try:
                lease = owner.admission.begin(
                    "http_mutation",
                    allow_quarantine=allow_recovery,
                )
            except (OperationInvalidated, RuntimeOwnershipError):
                self.close_connection = True
                self._discard_small_request_body()
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "runtime_admission_unavailable"},
                )
                return
            try:
                with bind_lease(lease):
                    self._dispatch_post()
            except OperationInvalidated:
                self.close_connection = True
                with suppress(Exception):
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "runtime_epoch_invalidated"},
                    )
            finally:
                owner.admission.finish(lease)

        def do_PUT(self) -> None:
            """Route authenticated JSON updates through the same admission boundary."""
            self.command = "PUT"
            self.do_POST()

        def _dispatch_post(self) -> None:
            return post.dispatch(self, owner)

        def _configure_conversation(self) -> None:
            return interaction.configure_conversation(self, owner)

        def _lifecycle_control(self) -> None:
            return operator.lifecycle_control(self, owner)

        def _run_retention_batch(self) -> None:
            return operator.run_retention_batch(self, owner)

        def _configure_retention(self) -> None:
            return operator.configure_retention(self, owner)

        def _reconcile_action(self) -> None:
            return operator.reconcile_action(self, owner)

        def _reconcile_model_call(self) -> None:
            return operator.reconcile_model_call(self, owner)

        def _create_wallet_bounty(self) -> None:
            return wallet_rewards.create_wallet_bounty(self, owner)

        def _create_wallet_reward(self) -> None:
            return wallet_rewards.create_wallet_reward(self, owner)

        def _wallet_reward_operation(self) -> None:
            return wallet_rewards.wallet_reward_operation(self, owner)

        def _decide_wallet_reward_submission(self) -> None:
            return wallet_rewards.decide_wallet_reward_submission(self, owner)

        def _resolve_wallet_reward_incident(self) -> None:
            return wallet_rewards.resolve_wallet_reward_incident(self, owner)

        def _submit_wallet_reward(self) -> None:
            return wallet_rewards.submit_wallet_reward(self, owner)

        def _execute_wallet_rewards(self) -> None:
            return wallet_rewards.execute_wallet_rewards(self, owner)

        def _wallet_bounty_operation(self) -> None:
            return wallet_rewards.wallet_bounty_operation(self, owner)

        def _submit_wallet_bounty(self) -> None:
            return wallet_rewards.submit_wallet_bounty(self, owner)

        def _wallet_submission_operation(self) -> None:
            return wallet_rewards.wallet_submission_operation(self, owner)

        def _wallet_order_operation(self) -> None:
            return wallet_payments.wallet_order_operation(self, owner)

        def _wallet_execution_operation(self) -> None:
            return wallet_payments.wallet_execution_operation(self, owner)

        def _recover_wallet_executions(self) -> None:
            return wallet_payments.recover_wallet_executions(self, owner)

        def _update_wallet_policy(self) -> None:
            return wallet_payments.update_wallet_policy(self, owner)

        def _wallet_automation_projection(self) -> dict[str, Any]:
            return wallet_automation.wallet_automation_projection(self, owner)

        def _update_wallet_automation(self, payload: object | None = None) -> None:
            return wallet_automation.update_wallet_automation(self, owner, payload)

        def _set_wallet_automation_pause(self, paused: bool) -> None:
            return wallet_automation.set_wallet_automation_pause(self, owner, paused)

        def _configure_wallet_network(self) -> None:
            return wallet_resources.configure_wallet_network(self, owner)

        def _configure_wallet_asset(self) -> None:
            return wallet_resources.configure_wallet_asset(self, owner)

        def _configure_wallet_address(self) -> None:
            return wallet_resources.configure_wallet_address(self, owner)

        def _revoke_wallet_resource(self, resource_type: str) -> None:
            return wallet_resources.revoke_wallet_resource(self, owner, resource_type)

        @staticmethod
        def _wallet_observation_health_query(
            query: dict[str, list[str]],
        ) -> tuple[str, int, str | None, str | None]:
            return wallet_resources.wallet_observation_health_query(owner, query)

        @staticmethod
        def _wallet_balance_history_query(
            query: dict[str, list[str]],
        ) -> tuple[int, str | None, str | None, str | None, str | None]:
            return wallet_resources.wallet_balance_history_query(owner, query)

        @staticmethod
        def _wallet_acquisition_id(path: str, operation: str) -> str | None:
            return wallet_resources.wallet_acquisition_id(owner, path, operation)

        @staticmethod
        def _wallet_acquisition_statuses(
            query: dict[str, list[str]],
        ) -> tuple[str, ...] | None:
            return wallet_resources.wallet_acquisition_statuses(owner, query)

        def _wallet_payload(self) -> dict[str, Any] | None:
            return wallet_payments.wallet_payload(self, owner)

        def _enqueue_wallet_acquisition(self) -> None:
            return wallet_resources.enqueue_wallet_acquisition(self, owner)

        def _run_wallet_acquisitions(self) -> None:
            return wallet_resources.run_wallet_acquisitions(self, owner)

        def _retry_wallet_acquisition(self) -> None:
            return wallet_resources.retry_wallet_acquisition(self, owner)

        def _cancel_wallet_acquisition(self) -> None:
            return wallet_resources.cancel_wallet_acquisition(self, owner)

        def _configure_search_provider(self) -> None:
            return search.configure_search_provider(self, owner)

        def _configure_search_routing(self) -> None:
            return search.configure_search_routing(self, owner)

        def _set_search_provider_routing(self) -> None:
            return search.set_search_provider_routing(self, owner)

        def _set_search_provider_enabled(self) -> None:
            return search.handle_set_search_provider_enabled(self, owner)

        def _test_unstored_search_provider(self) -> None:
            return search.test_unstored_search_provider(self, owner)

        def _configure_transport(self) -> None:
            return interaction.configure_transport(self, owner)

        def _public_publication(self) -> None:
            return public_posts.public_publication(self, owner)

        def _configure_public_post_controls(self) -> None:
            return public_posts.configure_public_post_controls(self, owner)

        def _issue_public_post_captcha(self) -> None:
            return public_posts.issue_public_post_captcha(self, owner)

        def _change_transport_status(self) -> None:
            return interaction.change_transport_status(self, owner)

        def _delivery_operation(self) -> None:
            return interaction.delivery_operation(self, owner)

        def _grant_capability(self) -> None:
            return operator.grant_capability(self, owner)

        def _revoke_capability(self) -> None:
            return operator.revoke_capability(self, owner)

        def _trust_common_knowledge_key(self) -> None:
            return knowledge.trust_common_knowledge_key(self, owner)

        def _import_common_knowledge(self) -> None:
            return knowledge.import_common_knowledge(self, owner)

        def _register_common_knowledge_peer(self) -> None:
            return knowledge.register_common_knowledge_peer(self, owner)

        def _sync_common_knowledge_peer(self) -> None:
            return knowledge.sync_common_knowledge_peer(self, owner)

        def _revoke_common_knowledge(self) -> None:
            return knowledge.revoke_common_knowledge(self, owner)

        def _configure_model_resource(self) -> None:
            return cognition.configure_model_resource(self, owner)

        def _draft_model_credentials(
            self, payload: object, *, require_model: bool
        ) -> tuple[str, str, SecretStr, ReasoningEffort | None]:
            return cognition.draft_model_credentials(
                self, owner, payload, require_model=require_model
            )

        def _test_unstored_model_resource(self) -> None:
            return cognition.test_unstored_model_resource(self, owner)

        def _discover_unstored_models(self) -> None:
            return cognition.discover_unstored_models(self, owner)

        def _configure_embedding_resource(self) -> None:
            return cognition.configure_embedding_resource(self, owner)

        def _test_model_resource(self) -> None:
            return cognition.test_model_resource(self, owner)

        def _change_embedding_resource_status(self) -> None:
            return cognition.change_embedding_resource_status(self, owner)

        def _change_model_resource_status(self) -> None:
            return cognition.change_model_resource_status(self, owner)

        def _update_model_resource(self) -> None:
            return cognition.update_model_resource(self, owner)

        def _add_model_resource_keys(self) -> None:
            return cognition.add_model_resource_keys(self, owner)

        def _revoke_model_resource_key(self) -> None:
            return cognition.revoke_model_resource_key(self, owner)

        def _revoke_search_provider(self) -> None:
            return search.revoke_search_provider(self, owner)

        def _start_upgrade(self) -> None:
            return operator.start_upgrade(self, owner)

        def _health_projection(self, payload: dict[str, Any]) -> dict[str, Any]:
            # Apply the access boundary on every read, including cache hits.
            if self._authorized("operator"):
                return payload
            return {"status": payload["status"], "service": "noyra"}

        def _authorized(self, required_role: str = "operator") -> bool:
            allowed: dict[str, tuple[SecretStr | None, ...]] = {
                "read": (
                    owner.settings.read_token,
                    owner.settings.operator_token,
                    owner.settings.export_token,
                    owner.settings.break_glass_token,
                    owner.settings.admin_token,
                ),
                "operator": (
                    owner.settings.operator_token,
                    owner.settings.break_glass_token,
                    owner.settings.admin_token,
                ),
                "export": (
                    owner.settings.export_token,
                    owner.settings.break_glass_token,
                    owner.settings.admin_token,
                ),
                "break_glass": (owner.settings.break_glass_token,),
            }
            if required_role not in allowed:
                return False
            supplied = self.headers.get("Authorization", "")
            bearer_authorized = any(
                secret is not None
                and _ascii_token_equal(
                    supplied,
                    f"Bearer {secret.get_secret_value()}",
                )
                for secret in allowed[required_role]
            )
            if bearer_authorized:
                return True
            _session_id, session = self._session_from_cookie()
            if session is None or required_role not in {"read", "operator"}:
                return False
            if required_role == "operator" and session.role not in {
                "operator",
                "admin",
                "break_glass",
            }:
                return False
            if self.command in {"POST", "PUT"} and self.path not in {
                "/admin/session",
                "/admin/session/logout",
            }:
                return _ascii_token_equal(self.headers.get("X-CSRF-Token", ""), session.csrf_token)
            return True

        def _actor(self) -> str:
            supplied = self.headers.get("Authorization", "")
            for role, secret in (
                ("admin", owner.settings.admin_token),
                ("operator", owner.settings.operator_token),
                ("break_glass", owner.settings.break_glass_token),
            ):
                if secret is not None and _ascii_token_equal(
                    supplied, f"Bearer {secret.get_secret_value()}"
                ):
                    return f"web-{role}"
            _session_id, session = self._session_from_cookie()
            return session.actor if session is not None else "web-operator"

        def _approval_identity(self) -> tuple[str, str | None, str | None]:
            """Return an audit label and a non-secret review binding."""
            supplied = self.headers.get("Authorization", "")
            for role, secret in (
                ("admin", owner.settings.admin_token),
                ("operator", owner.settings.operator_token),
                ("break_glass", owner.settings.break_glass_token),
            ):
                if secret is not None and _ascii_token_equal(
                    supplied, f"Bearer {secret.get_secret_value()}"
                ):
                    binding = hashlib.sha256(
                        f"noyra-admin-principal:{owner.kernel.subject_id}:{role}".encode()
                    ).hexdigest()
                    actor = f"web-{role}"
                    if owner.settings.migration_require_independent_review:
                        return actor, None, self._trusted_reviewer_principal()
                    return actor, binding, None
            session_id, session = self._session_from_cookie()
            if session is None:
                return self._actor(), None, self._trusted_reviewer_principal()
            if owner.settings.migration_require_independent_review:
                return session.actor, None, self._trusted_reviewer_principal()
            return session.actor, owner._admin_session_hash(session_id), None

        def _trusted_reviewer_principal(self) -> str | None:
            principal = self.headers.get("X-Noyra-Authenticated-Principal", "").strip()
            return _verify_trusted_proxy_principal(
                principal,
                self.headers.get("X-Noyra-Principal-Timestamp", ""),
                self.headers.get("X-Noyra-Principal-Signature", ""),
                self.client_address[0],
                trusted_proxy_cidrs=owner.settings.trusted_proxy_cidrs,
                hmac_key=owner.settings.migration_proxy_principal_hmac_key,
                method=self.command,
                path=self.path,
            )

        def _session_from_cookie(self) -> tuple[str, _AdminSession | None]:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                return "", None
            session_id = cookie.get("noyra_admin_session")
            value = "" if session_id is None else session_id.value
            return value, owner.admin_session(value)

        def _create_admin_session(self) -> None:
            if self.headers.get_content_type() != "application/json":
                self._discard_small_request_body()
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                return
            client_ip = owner.client_ip(
                str(self.client_address[0]), self.headers.get("X-Forwarded-For")
            )
            if not owner.allow_admin_login(client_ip):
                self._discard_small_request_body()
                owner.audit_admin_event(
                    "admin_login_rate_limited",
                    "web-anonymous",
                    {"path": "/admin/session"},
                )
                self._json(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {"error": "admin_login_rate_limited"},
                    retry_after=60,
                )
                return
            payload = self._request_json()
            if payload is None:
                return
            token = payload.get("token")
            role = None
            for candidate, secret in (
                ("admin", owner.settings.admin_token),
                ("operator", owner.settings.operator_token),
                ("break_glass", owner.settings.break_glass_token),
            ):
                if (
                    isinstance(token, str)
                    and secret is not None
                    and _ascii_token_equal(token, secret.get_secret_value())
                ):
                    role = candidate
                    break
            if role is None:
                owner.audit_admin_event(
                    "admin_login_failed", "web-anonymous", {"path": "/admin/session"}
                )
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            if role == "break_glass":
                owner.clear_admin_login_failures(client_ip)
                owner.audit_admin_event("admin_break_glass_rejected_session", "web-break_glass", {})
                self._json(HTTPStatus.FORBIDDEN, {"error": "break_glass_session_forbidden"})
                return
            owner.clear_admin_login_failures(client_ip)
            session_id, session = owner.create_admin_session(role=role, actor=f"web-{role}")
            owner.audit_admin_event("admin_login_succeeded", session.actor, {"role": role})
            body = json.dumps(
                {"authenticated": True, "role": session.role, "csrf_token": session.csrf_token},
                separators=(",", ":"),
            ).encode()
            cookie = (
                f"noyra_admin_session={session_id}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={owner.settings.admin_session_ttl_seconds}"
                + ("; Secure" if owner.settings.admin_session_cookie_secure else "")
            )
            self.send_response(HTTPStatus.OK)
            self.send_header("Set-Cookie", cookie)
            self._headers("application/json; charset=utf-8", len(body))
            self.end_headers()
            self.wfile.write(body)

        def _admin_session_status(self) -> None:
            _session_id, session = self._session_from_cookie()
            if session is None:
                self._json(HTTPStatus.OK, {"authenticated": False})
                return
            self._json(
                HTTPStatus.OK,
                {
                    "authenticated": True,
                    "role": session.role,
                    "csrf_token": session.csrf_token,
                },
            )

        def _logout_admin_session(self) -> None:
            session_id, session = self._session_from_cookie()
            if session is not None and not _ascii_token_equal(
                self.headers.get("X-CSRF-Token", ""), session.csrf_token
            ):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "csrf_required"})
                return
            owner.revoke_admin_session(session_id)
            if session is not None:
                owner.audit_admin_event("admin_logout", session.actor, {"role": session.role})
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header(
                "Set-Cookie",
                "noyra_admin_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
            )
            self._headers("application/json; charset=utf-8", 0)
            self.end_headers()

        @staticmethod
        def _operator_error_status(error: OperatorControlConflict) -> HTTPStatus:
            if error.code.startswith(("invalid_", "operator_")):
                return HTTPStatus.BAD_REQUEST
            if error.code == "at_rest_boundary_unavailable":
                return HTTPStatus.SERVICE_UNAVAILABLE
            return HTTPStatus.CONFLICT

        @staticmethod
        def _at_rest_available() -> bool:
            if owner.at_rest is None or not owner.at_rest.required:
                return True
            try:
                check = getattr(owner.at_rest, "require_request_ready", owner.at_rest.require_ready)
                check()
            except AtRestError:
                return False
            return True

        def _update_training_policy(self) -> None:
            return exports.update_training_policy(self, owner)

        def _create_export_job(self) -> None:
            return exports.create_export_job(self, owner)

        def _enqueue_export_job(self, export_kind: str) -> None:
            return exports.enqueue_export_job(self, owner, export_kind)

        def _cancel_export_job(self) -> None:
            return exports.cancel_export_job(self, owner)

        def _request_json(self) -> dict[str, Any] | None:
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdigit():
                self._json(HTTPStatus.LENGTH_REQUIRED, {"error": "length_required"})
                return None
            length = int(raw_length)
            if length > owner.settings.max_request_bytes:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too_large"})
                return None
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, ValueError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return None
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return None
            return payload

        def _receive_inbound_webhook(self) -> None:
            """Decode one configured native channel callback.

            Provider callbacks are authenticated by their transport secret,
            bounded before parsing, and accepted only after an explicit
            account binding.  The provider payload is never forwarded as a
            command or copied into an interaction beyond its text envelope.
            """
            if not self._at_rest_available() or not owner.allow_mutation(self.path):
                self.close_connection = True
                self._discard_small_request_body()
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "integrity_quarantine"},
                )
                return
            parts = self.path.split("/", 3)
            if len(parts) != 4 or parts[1] != "webhooks":
                self._discard_small_request_body()
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            channel = parts[2]
            transport_id = parts[3]
            if channel not in ADAPTERS or not transport_id or len(transport_id) > 128:
                self._discard_small_request_body()
                self._json(HTTPStatus.NOT_FOUND, {"error": "inbound_route_not_found"})
                return
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdigit():
                self._json(HTTPStatus.LENGTH_REQUIRED, {"error": "length_required"})
                return
            length = int(raw_length)
            if length > min(owner.settings.max_request_bytes, 1_048_576):
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too_large"})
                return
            body = self.rfile.read(length)
            try:
                transport = owner.transports.get(transport_id, subject_id=owner.kernel.subject_id)
                if transport.channel != channel or transport.status != "active":
                    raise PermissionError("inbound transport is unavailable")
                secret_payload = owner.transports.secret(
                    transport_id, subject_id=owner.kernel.subject_id
                )
                credentials = secret_payload.get("credentials")
                if not isinstance(credentials, dict):
                    raise PermissionError("inbound transport secret is invalid")
                transport_settings = owner.transports.settings(
                    transport_id, subject_id=owner.kernel.subject_id
                )
                if channel == "qq":
                    # Official QQ HTTP callbacks derive Ed25519 from
                    # AppSecret.  Legacy HMAC is opt-in and separate.
                    secret_value = str(credentials.get("app_secret") or "")
                    signing_key = str(credentials.get("webhook_secret") or "") or None
                elif channel == "feishu":
                    secret_value = str(
                        credentials.get("verification_token")
                        or credentials.get("webhook_secret")
                        or ""
                    )
                    signing_key = None
                else:
                    secret_value = next(
                        (
                            str(credentials[key])
                            for key in (
                                "webhook_secret",
                                "verification_token",
                                "secret",
                                "signing_secret",
                                "token",
                            )
                            if credentials.get(key)
                        ),
                        "",
                    )
                    signing_key = None
                callback_headers = {str(key): str(value) for key, value in self.headers.items()}
                if channel == "wechat":
                    # WeChat signs callbacks with query parameters rather
                    # than HTTP headers. Normalize them for the adapter.
                    request_target = getattr(self, "_request_target", self.path)
                    callback_query = parse_qs(urlsplit(request_target).query)
                    for query_name, header_name in (
                        ("signature", "X-Wechat-Signature"),
                        ("msg_signature", "X-Wechat-MsgSignature"),
                        ("timestamp", "X-Wechat-Timestamp"),
                        ("nonce", "X-Wechat-Nonce"),
                        ("encrypt_type", "X-Wechat-EncryptType"),
                    ):
                        values = callback_query.get(query_name, [])
                        if len(values) == 1:
                            callback_headers[header_name] = values[0]
                adapter = ADAPTERS[channel]()
                if channel == "wechat":
                    envelope = adapter.parse(
                        body,
                        callback_headers,
                        transport_id=transport_id,
                        secret=secret_value,
                        encryption_key=(
                            str(credentials.get("encoding_aes_key"))
                            if credentials.get("encoding_aes_key")
                            else None
                        ),
                        signing_key=signing_key,
                        account_id=(
                            str(credentials.get("app_id")) if credentials.get("app_id") else None
                        ),
                    )
                elif channel == "feishu":
                    envelope = adapter.parse(
                        body,
                        callback_headers,
                        transport_id=transport_id,
                        secret=secret_value,
                        encryption_key=(
                            str(credentials.get("encrypt_key"))
                            if credentials.get("encrypt_key")
                            else None
                        ),
                        signing_key=signing_key,
                    )
                else:
                    envelope = adapter.parse(
                        body,
                        callback_headers,
                        transport_id=transport_id,
                        secret=secret_value,
                        signing_key=signing_key,
                    )
                if isinstance(envelope, InboundChallenge):
                    self._json(
                        HTTPStatus.OK,
                        envelope.response or {"challenge": envelope.challenge},
                    )
                    return
                if isinstance(envelope, InboundIgnored):
                    self._inbound_ack(
                        channel,
                        {"status": "ignored", "reason": envelope.reason},
                    )
                    return
                configured_account = transport_settings.get("external_account_id")
                if not configured_account:
                    configured_account = transport_settings.get("account_id")
                if not configured_account and channel in {"qq", "feishu"}:
                    configured_account = credentials.get("app_id")
                if (
                    isinstance(configured_account, (str, int))
                    and str(configured_account).strip()
                    and channel in {"qq", "feishu"}
                ):
                    envelope = envelope.model_copy(
                        update={"external_account_id": str(configured_account).strip()}
                    )
                accepted = owner.inbound.ingest(envelope)
                if isinstance(accepted, InboundIdentityObserved):
                    self._inbound_ack(channel, {"status": "identity_observed"})
                    return
                try:
                    owner.wallet_rewards.submit_inbound(
                        accepted,
                        external_counterparty=envelope.external_sender_id,
                    )
                except ValueError as error:
                    if "not a reward submission envelope" not in str(error):
                        raise
            except (PermissionError, InboundAuthenticationError):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "inbound_unauthorized"})
                return
            except IntegrityError:
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "inbound_integrity_unavailable"},
                    retry_after=60,
                )
                return
            except (ValueError, NotFoundError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_inbound_event"})
                return
            try:
                owner.audit_admin_event(
                    "inbound_message_received",
                    f"transport:{channel}",
                    {
                        "event_id": accepted.event_id,
                        "interaction_id": accepted.interaction_id,
                        "channel": channel,
                        "duplicate": accepted.duplicate,
                        "scheduling_priority": accepted.scheduling_priority,
                    },
                )
            except Exception:
                # Durable ingestion is already complete.  A provider must
                # receive a success response or it will redeliver forever.
                LOGGER.exception("inbound acceptance audit failed")
            self._inbound_ack(
                channel,
                {
                    "event_id": accepted.event_id,
                    "interaction_id": accepted.interaction_id,
                    "duplicate": accepted.duplicate,
                    "status": "accepted",
                },
            )

        def _wechat_webhook_challenge(self) -> None:
            if not self._at_rest_available() or not owner.allow_mutation(self.path):
                self.close_connection = True
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "integrity_quarantine"},
                    retry_after=60,
                )
                return
            parsed = urlsplit(self.path)
            transport_id = parsed.path.removeprefix("/webhooks/wechat/").strip("/")
            query = parse_qs(parsed.query)
            if not transport_id or any(
                len(query.get(key, [])) != 1 for key in ("timestamp", "nonce", "echostr")
            ):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wechat_challenge"})
                return
            try:
                transport = owner.transports.get(transport_id, subject_id=owner.kernel.subject_id)
                secret_payload = owner.transports.secret(
                    transport_id, subject_id=owner.kernel.subject_id
                )
                credentials = secret_payload.get("credentials")
                secret = (
                    ""
                    if not isinstance(credentials, dict)
                    else next(
                        (
                            str(credentials[key])
                            for key in ("webhook_secret", "secret", "token")
                            if credentials.get(key)
                        ),
                        "",
                    )
                )
                if transport.channel != "wechat" or transport.status != "active" or not secret:
                    raise ValueError("wechat transport is unavailable")
                encrypt_type = query.get("encrypt_type", [""])[0].casefold()
                encrypted_mode = encrypt_type == "aes" or "msg_signature" in query
                signature_name = "msg_signature" if encrypted_mode else "signature"
                if len(query.get(signature_name, [])) != 1:
                    raise ValueError("wechat challenge signature is missing")
                signature = query[signature_name][0]
                timestamp = query["timestamp"][0]
                nonce = query["nonce"][0]
                try:
                    timestamp_value = int(timestamp)
                except ValueError as error:
                    raise ValueError("wechat timestamp is invalid") from error
                if abs(time.time() - timestamp_value) > REPLAY_WINDOW_SECONDS:
                    raise ValueError("wechat signature is stale")
                echostr = query["echostr"][0]
                signature_message = (
                    (secret, timestamp, nonce, echostr)
                    if encrypted_mode
                    else (
                        secret,
                        timestamp,
                        nonce,
                    )
                )
                expected = hashlib.sha1("".join(sorted(signature_message)).encode()).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    raise ValueError("wechat signature is invalid")
                if not echostr:
                    raise ValueError("wechat challenge is missing")
                if encrypted_mode:
                    encoding_key = (
                        credentials.get("encoding_aes_key")
                        if isinstance(credentials, dict)
                        else None
                    )
                    if not isinstance(encoding_key, str) or not encoding_key:
                        raise ValueError("wechat encrypted challenge is not configured")
                    account_id = (
                        credentials.get("app_id") if isinstance(credentials, dict) else None
                    )
                    if not isinstance(account_id, str) or not account_id.strip():
                        raise ValueError("wechat encrypted challenge account is not configured")
                    body = WeChatInboundAdapter._decrypt_xml(
                        echostr,
                        encoding_key,
                        expected_account_id=account_id,
                    )
                else:
                    body = echostr.encode()
            except IntegrityError:
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "inbound_integrity_unavailable"},
                    retry_after=60,
                )
                return
            except (ValueError, NotFoundError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wechat_challenge"})
                return
            self.send_response(HTTPStatus.OK)
            self._headers("text/plain; charset=utf-8", len(body))
            self.end_headers()
            self.wfile.write(body)

        def _configure_inbound_binding(self) -> None:
            return interaction.configure_inbound_binding(self, owner)

        def _identity_challenge(self) -> None:
            return interaction.identity_challenge(self, owner)

        def _change_inbound_binding(self) -> None:
            return interaction.change_inbound_binding(self, owner)

        def _discard_small_request_body(self) -> None:
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdigit():
                return
            length = int(raw_length)
            if 0 < length <= owner.settings.max_request_bytes:
                self.rfile.read(length)

        def _asset(self, filename: str, content_type: str) -> None:
            path = web_root / filename
            try:
                body = path.read_bytes()
            except OSError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "asset_not_found"})
                return
            cache_control = "no-store"
            if filename == "index.html":
                hero_path = web_root / "assets" / "public-hero.webp"
                try:
                    hero_body = hero_path.read_bytes()
                except OSError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "asset_not_found"})
                    return
                hero_version = hashlib.sha256(hero_body).hexdigest()[:16]
                mobile_hero_path = hero_path.with_name("public-hero-mobile.webp")
                try:
                    mobile_hero_body = mobile_hero_path.read_bytes()
                except OSError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "asset_not_found"})
                    return
                mobile_hero_version = hashlib.sha256(mobile_hero_body).hexdigest()[:16]
                body = body.replace(b"__PUBLIC_HERO_VERSION__", hero_version.encode("ascii"))
                body = body.replace(
                    b"__PUBLIC_HERO_MOBILE_VERSION__", mobile_hero_version.encode("ascii")
                )
                site_url = owner.settings.public_site_url
                metadata = [
                    '<meta property="og:type" content="website">',
                    '<meta property="og:locale" content="zh_CN">',
                    '<meta property="og:title" content="Noyra \u00b7 \u516c\u5f00\u6863\u6848">',
                    '<meta property="og:description" '
                    'content="Noyra '
                    "\u7684\u516c\u5f00\u6863\u6848\uff1a\u8bb0\u5f55\u4e00\u4e2a"
                    "\u6301\u7eed\u8fd0\u884c\u7684"
                    "\u4eba\u5de5\u4e3b\u4f53"
                    '\u5982\u4f55\u89c2\u5bdf\u3001\u5224\u65ad\u4e0e\u53cd\u601d">',
                ]
                if site_url:
                    escaped_url = html.escape(site_url + "/", quote=True)
                    image_url = html.escape(f"{site_url}/assets/public-social.png", quote=True)
                    metadata.extend(
                        (
                            f'<link rel="canonical" href="{escaped_url}">',
                            f'<meta property="og:url" content="{escaped_url}">',
                            f'<meta property="og:image" content="{image_url}">',
                            '<meta property="og:image:width" content="1200">',
                            '<meta property="og:image:height" content="630">',
                            '<meta property="og:image:alt" content="Noyra 公开档案的治愈科技视觉">',
                            f'<meta name="twitter:image" content="{image_url}">',
                        )
                    )
                body = body.replace(
                    b"<!-- NOYRA_SEO_METADATA -->", "\n  ".join(metadata).encode("utf-8")
                )
                cache_control = "no-cache"
            elif filename in {"app.js", "participant.js", "styles.css"}:
                cache_control = "public, max-age=300, must-revalidate"
            elif filename in {
                "assets/public-hero.webp",
                "assets/public-hero-mobile.webp",
            }:
                versions = parse_qs(urlsplit(self.path).query).get("v", [])
                expected_version = hashlib.sha256(body).hexdigest()[:16]
                cache_control = (
                    "public, max-age=31536000, immutable"
                    if len(versions) == 1 and hmac.compare_digest(versions[0], expected_version)
                    else "no-cache"
                )
            elif filename == "assets/public-social.png":
                cache_control = "public, max-age=31536000, immutable"
            self.send_response(HTTPStatus.OK)
            self._headers(content_type, len(body), cache_control=cache_control)
            self.end_headers()
            self.wfile.write(body)

        def _public_article(self, parsed: Any) -> None:
            parts = parsed.path.split("/")
            if len(parts) != 4 or parts[2] not in PUBLIC_VIEWS or not parts[3]:
                self._text(
                    HTTPStatus.NOT_FOUND,
                    "text/plain; charset=utf-8",
                    b"Not found\n",
                    cache_control="no-store",
                )
                return
            kind = parts[2]
            try:
                identifier = unquote(parts[3], errors="strict")
                params = parse_qs(parsed.query, keep_blank_values=True)
                if (
                    set(params) - {"lang", "theme", "return"}
                    or any(len(values) != 1 for values in params.values())
                    or params.get("lang", ["zh"]) not in [["zh"], ["en"]]
                    or params.get("theme", ["system"]) not in [["system"], ["light"], ["dark"]]
                    or params.get("return", [PUBLIC_VIEWS[kind]])[0]
                    not in {*PUBLIC_VIEWS.values(), "overview", "timeline"}
                ):
                    raise ValueError("invalid reading language")
                page = owner.public_archive.page(
                    owner.kernel.subject_id, kind=kind, limit=1, identifier=identifier
                )
            except (ValueError, UnicodeError):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_public_query"},
                    public_contract=ARCHIVE_CONTRACT,
                )
                return
            except PublicQueryBudgetExceeded:
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "public_query_budget_exceeded"},
                    public_contract=ARCHIVE_CONTRACT,
                )
                return
            except IntegrityError:
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "public_content_unavailable"},
                    public_contract=ARCHIVE_CONTRACT,
                )
                return
            if not page["items"]:
                self._text(
                    HTTPStatus.NOT_FOUND,
                    "text/plain; charset=utf-8",
                    b"Not found\n",
                    cache_control="no-store",
                )
                return
            language = params.get("lang", ["zh"])[0]
            try:
                body = public_article_html(
                    page["items"][0],
                    owner.settings.public_site_url or "",
                    english=language == "en",
                    theme=params.get("theme", ["system"])[0],
                    return_view=params.get("return", [None])[0],
                )
            except (KeyError, TypeError, ValueError):
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "public_content_unavailable"},
                    public_contract=ARCHIVE_CONTRACT,
                )
                return
            if len(body) > PUBLIC_MAX_RESPONSE_BYTES:
                self._json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": "public_response_too_large"},
                    public_contract=ARCHIVE_CONTRACT,
                )
                return
            self.send_response(HTTPStatus.OK)
            self._headers("text/html; charset=utf-8", len(body), cache_control="no-store")
            self.end_headers()
            self.wfile.write(body)

        def _robots_txt(self) -> None:
            lines = [
                "User-agent: *",
                "Disallow: /admin",
                "Disallow: /admin.html",
                "Disallow: /api/",
                "Disallow: /health/",
            ]
            if owner.settings.public_site_url:
                lines.append(f"Sitemap: {owner.settings.public_site_url}/sitemap.xml")
            body = ("\n".join(lines) + "\n").encode("utf-8")
            self._text(HTTPStatus.OK, "text/plain; charset=utf-8", body)

        def _sitemap_xml(self, parsed: Any) -> None:
            site_url = owner.settings.public_site_url
            if not site_url:
                self._text(
                    HTTPStatus.NOT_FOUND,
                    "text/plain; charset=utf-8",
                    b"Configure NOYRA_PUBLIC_SITE_URL to publish a sitemap.\n",
                    cache_control="no-store",
                )
                return
            try:
                params = parse_qs(parsed.query, keep_blank_values=True)
                if set(params) - {"part"} or (params and len(params["part"]) != 1):
                    raise ValueError("invalid sitemap query")
                if params:
                    if params["part"] == ["home"]:
                        items = []
                    else:
                        items = owner.public_archive.sitemap_partition(
                            owner.kernel.subject_id, params["part"][0]
                        )
                    entries = "".join(
                        "<url><loc>"
                        f"{html.escape(site_url + public_item_path(item['kind'], item['id']))}"
                        "</loc>"
                        f"<lastmod>{html.escape(item['updated_at'])}</lastmod></url>\n"
                        for item in items
                    )
                    if params["part"] == ["home"]:
                        entries = f"<url><loc>{html.escape(site_url + '/')}</loc></url>\n"
                    root_tag = "urlset"
                else:
                    partitions = owner.public_archive.sitemap_partitions(owner.kernel.subject_id)
                    entries = "".join(
                        "<sitemap><loc>"
                        f"{html.escape(site_url + '/sitemap.xml?part=' + quote(part, safe=''))}"
                        "</loc></sitemap>\n"
                        for part in ["home", *partitions]
                    )
                    root_tag = "sitemapindex"
            except (ValueError, UnicodeError):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_public_query"},
                    public_contract=ARCHIVE_CONTRACT,
                )
                return
            except PublicQueryBudgetExceeded:
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "public_query_budget_exceeded"},
                    public_contract=ARCHIVE_CONTRACT,
                )
                return
            except IntegrityError:
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "public_content_unavailable"},
                    public_contract=ARCHIVE_CONTRACT,
                )
                return
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<{root_tag} xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                f"{entries}"
                f"</{root_tag}>\n"
            ).encode()
            if len(body) > PUBLIC_MAX_RESPONSE_BYTES:
                self._json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": "public_response_too_large"},
                    public_contract=ARCHIVE_CONTRACT,
                )
                return
            self._text(
                HTTPStatus.OK,
                "application/xml; charset=utf-8",
                body,
                cache_control="no-store",
            )

        def _text(
            self,
            status: HTTPStatus,
            content_type: str,
            body: bytes,
            *,
            cache_control: str = "public, max-age=300",
        ) -> None:
            self.send_response(status)
            self._headers(content_type, len(body), cache_control=cache_control)
            self.end_headers()
            self.wfile.write(body)

        def _send_file(
            self, path: Path, *, filename: str, digest: str, export_job_id: str | None = None
        ) -> None:
            length = path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("X-Archive-SHA256", digest)
            self._headers("application/zip", length)
            self.end_headers()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    if export_job_id is not None:
                        try:
                            owner.export_jobs.download_path(
                                export_job_id, subject_id=owner.kernel.subject_id
                            )
                        except (RuntimeError, FileNotFoundError):
                            self.close_connection = True
                            return
                    self.wfile.write(chunk)

        def _inbound_ack(self, channel: str, payload: Mapping[str, Any]) -> None:
            """Return the platform-native acknowledgement after durable handling."""
            if channel == "qq":
                self._json(HTTPStatus.OK, {"op": 12, "d": 0})
                return
            if channel == "wechat":
                body = b"success"
                self.send_response(HTTPStatus.OK)
                self._headers("text/plain; charset=utf-8", len(body))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json(HTTPStatus.OK, payload)

        def _json(
            self,
            status: HTTPStatus,
            payload: Any,
            *,
            retry_after: int | None = None,
            public_contract: bool | str = False,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            if public_contract and len(body) > PUBLIC_MAX_RESPONSE_BYTES:
                status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                body = json.dumps(
                    {
                        "error": "public_response_too_large",
                        "contract": public_contract
                        if isinstance(public_contract, str)
                        else PUBLIC_CONTRACT_ID,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            try:
                self.send_response(status)
                if status == HTTPStatus.TOO_MANY_REQUESTS:
                    self.send_header("Retry-After", str(60 if retry_after is None else retry_after))
                elif status == HTTPStatus.SERVICE_UNAVAILABLE:
                    self.send_header("Retry-After", str(5 if retry_after is None else retry_after))
                self._headers(
                    "application/json; charset=utf-8",
                    len(body),
                    cache_control=PUBLIC_CACHE_CONTROL if public_contract else "no-store",
                    public_contract=public_contract,
                )
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Clients routinely cancel health polling or large admin
                # responses.  The request is already gone; avoid a noisy
                # traceback while allowing the worker to be reused.
                self.close_connection = True
                return

        def _headers(
            self,
            content_type: str,
            length: int,
            *,
            cache_control: str = "no-store",
            public_contract: bool | str = False,
        ) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Permitted-Cross-Domain-Policies", "none")
            if public_contract:
                self.send_header(
                    "X-Noyra-Public-Contract",
                    public_contract if isinstance(public_contract, str) else PUBLIC_CONTRACT_ID,
                )
            if owner.settings.public_site_url or owner.settings.admin_session_cookie_secure:
                self.send_header(
                    "Strict-Transport-Security",
                    "max-age=31536000; includeSubDomains",
                )
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; media-src 'self' data:; "
                "base-uri 'none'; object-src 'none'; "
                "frame-ancestors 'none'; "
                "script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'none'",
            )

        @staticmethod
        def _limit(query: dict[str, list[str]]) -> int:
            try:
                return max(1, min(int(query.get("limit", ["100"])[0]), 1_000))
            except ValueError:
                return 100

        @staticmethod
        def _cursor(query: dict[str, list[str]]) -> str | None:
            values = query.get("cursor")
            if not values:
                return None
            value = values[0]
            if not value or len(value) > 8_192:
                raise ValueError("runtime log cursor is invalid")
            return value

        def _log_http_event(self, event: str, code: object = "-") -> None:
            method, route, status = http_log_fields(
                getattr(self, "command", None),
                getattr(self, "path", "") if getattr(self, "_log_request_parsed", False) else "",
                code,
            )
            LOGGER.info("http event=%s method=%s route=%s status=%s", event, method, route, status)

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            # Never delegate to the stdlib request-line formatter. Neither
            # the original target nor response/header/body data enter logs.
            self._log_http_event("request", code)

        def log_error(self, format: str, *args: Any) -> None:
            if format == "code %d, message %s" and args:
                self._log_http_event("response_error", args[0])
            elif format == "Request timed out: %r":
                self._log_http_event("timeout")
            else:
                self._log_http_event("protocol_error")

        def log_message(self, format: str, *args: Any) -> None:
            # Unknown/future stdlib diagnostics may contain request data or
            # exception text. Keep a fixed event, never interpolate them.
            self._log_http_event("diagnostic")

    return Handler
