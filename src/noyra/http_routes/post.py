"""Post HTTP domain; preserves existing request behavior."""

# Keep route selection separate from completion: a selected branch can fall
# through, and elif priority must not change when a helper returns False.
# ruff: noqa: SIM102

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from noyra.core.errors import NotFoundError
from noyra.interaction.conversations import ConversationConflict
from noyra.interaction.errors import InteractionStateConflictError

from . import (
    post_admin,
    post_cognition,
    post_connections,
    post_exports,
    post_migration,
    post_public,
    post_wallet,
)

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def dispatch(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    from noyra.interaction.participant_http import dispatch as participant_dispatch

    if participant_dispatch(self, owner, "POST"):
        return
    if self.path == "/api/admin/migration/policy":
        if post_migration.admin_migration_policy(self, owner):
            return
    if self.path == "/api/admin/migration/targets":
        if post_migration.admin_migration_targets(self, owner):
            return
    if self.path.startswith("/api/admin/migration/targets/") and self.path.endswith("/challenge"):
        if post_migration.admin_migration_targets_challenge(self, owner):
            return
    if self.path.startswith("/api/admin/migration/targets/") and self.path.endswith("/attest"):
        if post_migration.admin_migration_targets_attest(self, owner):
            return
    if self.path.startswith("/api/admin/migration/targets/") and self.path.endswith("/revoke"):
        if post_migration.admin_migration_targets_revoke(self, owner):
            return
    if self.path.startswith("/api/admin/migration/proposals/") and self.path.endswith("/approve"):
        if post_migration.admin_migration_proposals_approve(self, owner):
            return
    if self.path.startswith("/api/admin/migration/proposals/") and self.path.endswith("/reject"):
        if post_migration.admin_migration_proposals_reject(self, owner):
            return
    if self.path.startswith("/api/admin/migration/tasks/") and self.path.endswith(
        "/wallet-approval"
    ):
        if post_wallet.admin_migration_tasks_wallet_approval(self, owner):
            return
    if self.path.startswith("/api/admin/migration/tasks/") and self.path.endswith("/cancel"):
        if post_migration.admin_migration_tasks_cancel(self, owner):
            return
    if self.path.startswith("/api/admin/migration/tasks/") and self.path.endswith("/cutover"):
        if post_migration.admin_migration_tasks_cutover(self, owner):
            return
    if self.path.startswith("/api/admin/migration/tasks/") and self.path.endswith("/rollback"):
        if post_migration.admin_migration_tasks_rollback(self, owner):
            return
    if self.path == "/api/admin/migration/recovery":
        if post_migration.admin_migration_recovery(self, owner):
            return
    if self.path == "/api/admin/upgrade":
        if post_admin.admin_upgrade(self, owner):
            return
    if self.path in {
        "/api/admin/lifecycle/pause",
        "/api/admin/lifecycle/resume",
        "/api/admin/lifecycle/reset",
        "/api/admin/lifecycle/wake",
    }:
        if post_admin.admin_lifecycle_control(self, owner):
            return
    if self.path.startswith("/api/admin/actions/") and self.path.endswith("/reconcile"):
        if post_admin.admin_actions_reconcile(self, owner):
            return
    if self.path.startswith("/api/admin/model-calls/") and self.path.endswith("/reconcile"):
        if post_cognition.admin_model_calls_reconcile(self, owner):
            return
    if self.path == "/api/admin/export-jobs":
        if post_exports.admin_export_jobs(self, owner):
            return
    if self.path.startswith("/api/admin/export-jobs/") and self.path.endswith("/cancel"):
        if post_exports.admin_export_jobs_cancel(self, owner):
            return
    if self.path == "/api/admin/wallet-bounties":
        if post_wallet.admin_wallet_bounties(self, owner):
            return
    if self.path == "/api/admin/wallet-rewards":
        if post_wallet.admin_wallet_rewards(self, owner):
            return
    if self.path == "/api/admin/wallet-rewards/execute-ready":
        if post_wallet.admin_wallet_rewards_execute_ready(self, owner):
            return
    if self.path.startswith("/api/admin/wallet-reward-incidents/") and self.path.endswith(
        "/resolve"
    ):
        if post_wallet.admin_wallet_reward_incidents_resolve(self, owner):
            return
    if self.path.startswith("/api/admin/wallet-reward-submissions/") and self.path.endswith(
        "/decide"
    ):
        if post_wallet.admin_wallet_reward_submissions_decide(self, owner):
            return
    if self.path.startswith("/api/admin/wallet-rewards/"):
        if post_wallet.admin_wallet_rewards_item(self, owner):
            return
    if self.path.startswith("/api/wallet-rewards/") and self.path.endswith("/submissions"):
        if post_wallet.wallet_rewards_submissions(self, owner):
            return
    if self.path.startswith("/api/admin/wallet-bounties/"):
        if post_wallet.admin_wallet_bounties_item(self, owner):
            return
    if self.path.startswith("/api/bounties/") and self.path.endswith("/submissions"):
        if post_wallet.bounties_submissions(self, owner):
            return
    if self.path.startswith("/api/admin/wallet-submissions/"):
        if post_wallet.admin_wallet_submissions_item(self, owner):
            return
    if self.path == "/api/admin/wallet-executions/recover":
        if post_wallet.admin_wallet_executions_recover(self, owner):
            return
    if self.path.startswith("/api/admin/wallet-executions/"):
        if post_wallet.admin_wallet_executions_item(self, owner):
            return
    if self.path.startswith("/api/admin/wallet-orders/"):
        if post_wallet.admin_wallet_orders_item(self, owner):
            return
    if self.path == "/api/admin/wallet-policy":
        if post_wallet.admin_wallet_policy(self, owner):
            return
    if self.path in {"/api/v1/admin/wallet-automation", "/api/admin/wallet-automation"}:
        if post_wallet.admin_wallet_automation(self, owner):
            return
    if self.path in {
        "/api/v1/admin/wallet-automation/pause",
        "/api/admin/wallet-automation/pause",
        "/api/v1/admin/wallet-automation/resume",
        "/api/admin/wallet-automation/resume",
    }:
        if post_wallet.admin_wallet_automation_pause_admin_wallet_automation_resume(self, owner):
            return
    if self.path in {"/api/v1/admin/retention/run", "/api/admin/retention/run"}:
        if post_admin.admin_retention_run(self, owner):
            return
    if self.path == "/api/admin/retention/settings":
        if post_admin.admin_retention_settings(self, owner):
            return
    if self.path == "/api/config/wallet-networks":
        if post_wallet.config_wallet_networks(self, owner):
            return
    if self.path == "/api/config/wallet-assets":
        if post_wallet.config_wallet_assets(self, owner):
            return
    if self.path == "/api/config/wallet-addresses":
        if post_wallet.config_wallet_addresses(self, owner):
            return
    if self.path == "/api/admin/wallet-acquisitions":
        if post_wallet.admin_wallet_acquisitions(self, owner):
            return
    if self.path == "/api/admin/wallet-acquisitions/run":
        if post_wallet.admin_wallet_acquisitions_run(self, owner):
            return
    if self.path.startswith("/api/admin/wallet-acquisitions/") and self.path.endswith("/retry"):
        if post_wallet.admin_wallet_acquisitions_retry(self, owner):
            return
    if self.path.startswith("/api/admin/wallet-acquisitions/") and self.path.endswith("/cancel"):
        if post_wallet.admin_wallet_acquisitions_cancel(self, owner):
            return
    if self.path.startswith("/api/config/wallet-networks/") and self.path.endswith("/revoke"):
        if post_wallet.config_wallet_networks_revoke(self, owner):
            return
    if self.path.startswith("/api/config/wallet-assets/") and self.path.endswith("/revoke"):
        if post_wallet.config_wallet_assets_revoke(self, owner):
            return
    if self.path.startswith("/api/config/wallet-addresses/") and self.path.endswith("/revoke"):
        if post_wallet.config_wallet_addresses_revoke(self, owner):
            return
    if self.path == "/api/config/search-providers":
        if post_connections.config_search_providers(self, owner):
            return
    if self.path == "/api/config/search-providers/test":
        if post_connections.config_search_providers_test(self, owner):
            return
    if self.path == "/api/config/search-routing":
        if post_connections.config_search_routing(self, owner):
            return
    if self.path.startswith("/api/config/search-providers/") and self.path.endswith("/routing"):
        if post_connections.config_search_providers_routing(self, owner):
            return
    if self.path == "/api/config/identity-challenges" or self.path.startswith(
        "/api/config/identity-challenges/"
    ):
        if post_connections.config_identity_challenges_item(self, owner):
            return
    if self.path == "/api/config/inbound-bindings":
        if post_connections.config_inbound_bindings(self, owner):
            return
    if self.path.startswith("/api/config/inbound-bindings/"):
        if post_connections.config_inbound_bindings_item(self, owner):
            return
    if self.path.startswith("/api/config/search-providers/") and self.path.endswith("/revoke"):
        if post_connections.config_search_providers_revoke(self, owner):
            return
    if self.path.startswith("/api/config/search-providers/") and (
        self.path.endswith("/enable") or self.path.endswith("/disable")
    ):
        if post_connections.config_search_providers_enable_disable(self, owner):
            return
    if self.path == "/api/config/model-resources":
        if post_cognition.config_model_resources(self, owner):
            return
    if self.path == "/api/config/model-resources/test":
        if post_cognition.config_model_resources_test(self, owner):
            return
    if self.path == "/api/config/model-resources/models":
        if post_cognition.config_model_resources_models(self, owner):
            return
    if self.path == "/api/config/embedding-resources":
        if post_cognition.config_embedding_resources(self, owner):
            return
    if self.path.startswith("/api/config/model-resources/") and self.path.endswith("/test"):
        if post_cognition.config_model_resources_test_variant_2(self, owner):
            return
    if self.path.startswith("/api/config/model-resources/"):
        if post_cognition.config_model_resources_item(self, owner):
            return
    if self.path.startswith("/api/config/model-resources/") and self.path.endswith("/keys"):
        if post_cognition.config_model_resources_keys(self, owner):
            return
    if self.path.startswith("/api/config/model-resources/") and self.path.endswith("/update"):
        if post_cognition.config_model_resources_update(self, owner):
            return
    if self.path.startswith("/api/config/model-resources/"):
        if post_cognition.config_model_resources_item_variant_2(self, owner):
            return
    if self.path.startswith("/api/config/embedding-resources/"):
        if post_cognition.config_embedding_resources_item(self, owner):
            return
    if self.path == "/api/config/training-policy":
        if post_exports.config_training_policy(self, owner):
            return
    if self.path == "/api/config/transports":
        if post_connections.config_transports(self, owner):
            return
    if self.path == "/api/config/public-post-controls":
        if post_admin.config_public_post_controls(self, owner):
            return
    if self.path.startswith("/api/config/transports/"):
        if post_connections.config_transports_item(self, owner):
            return
    if self.path.startswith("/api/deliveries/"):
        if post_connections.deliveries_item(self, owner):
            return
    if self.path == "/api/config/capabilities":
        if post_connections.config_capabilities(self, owner):
            return
    if self.path.startswith("/api/config/capabilities/") and self.path.endswith("/revoke"):
        if post_connections.config_capabilities_revoke(self, owner):
            return
    if self.path == "/api/config/common-knowledge/trust":
        if post_connections.config_common_knowledge_trust(self, owner):
            return
    if self.path == "/api/config/common-knowledge/import":
        if post_connections.config_common_knowledge_import(self, owner):
            return
    if self.path == "/api/config/common-knowledge/peers":
        if post_connections.config_common_knowledge_peers(self, owner):
            return
    if self.path.startswith("/api/config/common-knowledge/peers/") and self.path.endswith("/sync"):
        if post_connections.config_common_knowledge_peers_sync(self, owner):
            return
    if self.path.startswith("/api/config/common-knowledge/") and self.path.endswith("/revoke"):
        if post_connections.config_common_knowledge_revoke(self, owner):
            return
    if self.path == "/api/public-posts/captcha":
        if post_public.public_posts_captcha(self, owner):
            return
    if self.path == "/api/admin/public-publications" or (
        self.path.startswith("/api/admin/public-publications/") and self.path.endswith("/withdraw")
    ):
        if post_admin.admin_public_publications_withdraw(self, owner):
            return
    if self.path == "/api/public-posts":
        if post_public.public_posts(self, owner):
            return
    if self.path.startswith("/api/admin/public-posts/") and self.path.rsplit("/", 1)[-1] in {
        "publish",
        "reject",
        "archive",
    }:
        if post_public.admin_public_posts_item(self, owner):
            return
    if self.path == "/api/admin/conversations" or self.path.startswith("/api/admin/conversations/"):
        if post_admin.admin_conversations_item(self, owner):
            return
    if self.path != "/api/interactions":
        self._discard_small_request_body()
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        return
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    try:
        channel = payload.get("channel", "web")
        counterparty = payload.get("counterparty", "web-user")
        content = payload.get("content")
        idempotency_key = payload.get("idempotency_key")
        conversation_id = payload.get("conversation_id")
        if conversation_id is not None and (not isinstance(conversation_id, str)):
            raise ValueError("invalid conversation id")
        if not isinstance(channel, str) or not isinstance(counterparty, str):
            raise TypeError("interaction metadata must be text")
        if channel != "web" or counterparty != "web-user":
            raise ValueError("web mailbox identity cannot be overridden")
        if not isinstance(content, str):
            raise TypeError("interaction content must be text")
        if idempotency_key is not None and (not isinstance(idempotency_key, str)):
            raise TypeError("idempotency key must be text")
        interaction_record = owner.interactions.receive(
            owner.kernel.subject_id,
            channel,
            counterparty,
            content,
            idempotency_key=idempotency_key,
            conversation_id=conversation_id,
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "conversation_not_found"})
        return
    except (ConversationConflict, InteractionStateConflictError):
        self._json(HTTPStatus.CONFLICT, {"error": "interaction_conflict"})
        return
    except (ValueError, TypeError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_interaction"})
        return
    self._json(
        HTTPStatus.CREATED,
        {"interaction_id": interaction_record.interaction_id, "status": interaction_record.status},
    )
