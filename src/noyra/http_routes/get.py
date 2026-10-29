"""Get HTTP domain; preserves existing request behavior."""

# Keep route selection separate from completion: a selected branch can fall
# through, and elif priority must not change when a helper returns False.
# ruff: noqa: SIM102

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from . import (
    assets,
    health,
    read_admin,
    read_cognition,
    read_connections,
    read_exports,
    read_migration,
    read_public,
    read_wallet,
)

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def dispatch(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not owner.allow_request(
        owner.client_ip(str(self.client_address[0]), self.headers.get("X-Forwarded-For"))
    ):
        self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"})
        return
    if self.path.startswith("/api/v1/"):
        normalized_path = self.path.replace("/api/v1/", "/api/", 1)
        if normalized_path.startswith("/api/webhooks/"):
            self.path = normalized_path.replace("/api/webhooks/", "/webhooks/", 1)
    if self.path.startswith("/webhooks/wechat/"):
        self._wechat_webhook_challenge()
        return
    parsed = urlsplit(self.path)
    from noyra.interaction.participant_http import dispatch as participant_dispatch

    if participant_dispatch(self, owner, "GET"):
        return
    if parsed.path.startswith("/public/"):
        self._public_article(parsed)
        return
    wallet_list_path = parsed.path.replace("/api/v1/", "/api/", 1)
    if wallet_list_path.startswith("/api/admin/wallet-lists/"):
        if read_wallet.admin_wallet_lists_item(self, owner, parsed, wallet_list_path):
            return
    if parsed.path in {"/admin", "/admin.html"}:
        if assets.admin_admin_html(self, owner):
            return
    if parsed.path == "/admin.js":
        if assets.admin_js(self, owner):
            return
    if parsed.path == "/admin.css":
        if assets.admin_css(self, owner):
            return
    if parsed.path == "/admin/session":
        if assets.admin_session(self, owner):
            return
    if parsed.path == "/":
        if assets.index(self, owner):
            return
    if parsed.path == "/participant.js":
        if assets.participant_js(self, owner):
            return
    if parsed.path == "/app.js":
        if assets.app_js(self, owner):
            return
    if parsed.path == "/styles.css":
        if assets.styles_css(self, owner):
            return
    if parsed.path == "/assets/public-hero.webp":
        if assets.assets_public_hero_webp(self, owner):
            return
    if parsed.path == "/assets/public-hero-mobile.webp":
        if assets.assets_public_hero_mobile_webp(self, owner):
            return
    if parsed.path == "/assets/public-social.png":
        if assets.assets_public_social_png(self, owner):
            return
    if parsed.path == "/robots.txt":
        if assets.robots_txt(self, owner):
            return
    if parsed.path == "/sitemap.xml":
        if assets.sitemap_xml(self, owner, parsed):
            return
    if parsed.path == "/favicon.ico":
        if assets.favicon_ico(self, owner):
            return
    if parsed.path == "/health/live":
        if health.health_live(self, owner):
            return
    if parsed.path in {"/health/ready", "/api/admin/readiness", "/api/v1/admin/readiness"}:
        if health.health_ready_admin_readiness(self, owner, parsed):
            return
    if parsed.path == "/health":
        if health.health_cached(self, owner):
            return
    if parsed.path.startswith("/api/v1/"):
        self.path = self.path.replace("/api/v1/", "/api/", 1)
        parsed = urlsplit(self.path)
    if parsed.path in {
        "/api/config/wallet-balance-history",
        "/api/v1/config/wallet-balance-history",
        "/api/config/wallet-observation-health",
        "/api/v1/config/wallet-observation-health",
    }:
        query = parse_qs(parsed.query, keep_blank_values=True)
    else:
        query = parse_qs(parsed.query)
    limit = self._limit(query)
    if parsed.path == "/api/admin/migration/policy":
        if read_migration.admin_migration_policy(self, owner):
            return
    if parsed.path == "/api/admin/migration/targets":
        if read_migration.admin_migration_targets(self, owner, limit):
            return
    if parsed.path == "/api/admin/migration/candidates":
        if read_migration.admin_migration_candidates(self, owner):
            return
    if parsed.path == "/api/admin/migration/proposals":
        if read_migration.admin_migration_proposals(self, owner, limit):
            return
    if parsed.path.startswith("/api/admin/migration/proposals/"):
        if read_migration.admin_migration_proposals_item(self, owner, parsed):
            return
    if parsed.path == "/api/admin/migration/tasks":
        if read_migration.admin_migration_tasks(self, owner, limit):
            return
    if parsed.path.startswith("/api/admin/migration/tasks/"):
        if read_migration.admin_migration_tasks_item(self, owner, parsed):
            return
    if (
        parsed.path != "/health"
        and parsed.path.startswith("/api/")
        and (not self._at_rest_available())
    ):
        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "at_rest_boundary_unavailable"})
        return
    if parsed.path == "/health":
        if health.health_status(self, owner):
            return
    elif parsed.path.startswith("/api/public/"):
        if read_public.public_item(self, owner, parsed):
            return
    elif parsed.path == "/api/admin/monitoring/soak":
        if read_admin.monitoring_soak(self, owner, parsed):
            return
    elif parsed.path == "/api/admin/public-publications":
        if read_admin.admin_public_publications(self, owner, parsed):
            return
    elif parsed.path == "/api/state":
        if read_public.state(self, owner):
            return
    elif parsed.path == "/api/state/details":
        if read_public.state_details(self, owner):
            return
    elif parsed.path == "/api/diary":
        if read_public.diary(self, owner, limit):
            return
    elif parsed.path == "/api/behavior":
        if read_admin.behavior(self, owner, limit):
            return
    elif parsed.path == "/api/goals":
        if read_admin.goals(self, owner, limit):
            return
    elif parsed.path == "/api/projects":
        if read_public.projects(self, owner, limit):
            return
    elif parsed.path == "/api/outcomes":
        if read_admin.outcomes(self, owner, limit):
            return
    elif parsed.path in {"/api/runtime-logs", "/api/admin/activity-logs"}:
        if read_admin.runtime_logs_admin_activity_logs(self, owner, parsed, query, limit):
            return
    elif parsed.path == "/api/admin/lifecycle-summary":
        if read_admin.admin_lifecycle_summary(self, owner):
            return
    elif parsed.path == "/api/admin/runtime-summary":
        if read_admin.admin_runtime_summary(self, owner):
            return
    elif parsed.path == "/api/diagnostics":
        if read_admin.diagnostics(self, owner):
            return
    elif parsed.path == "/api/admin/world-acquisition":
        if read_cognition.admin_world_acquisition(self, owner):
            return
    elif parsed.path == "/api/admin/usage":
        if read_cognition.admin_usage(self, owner, query):
            return
    elif parsed.path in {
        "/api/admin/upgrade/check",
        "/api/admin/upgrade/status",
        "/api/admin/upgrade/installed",
    }:
        if read_admin.admin_upgrade_check_admin_upgrade_status_admin_upgrade_installed(
            self, owner, parsed
        ):
            return
    elif parsed.path == "/api/admin/health":
        if health.admin_health(self, owner):
            return
    elif parsed.path == "/api/admin/recoverable-work":
        if read_admin.admin_recoverable_work(self, owner, limit):
            return
    elif parsed.path == "/api/admin/lifecycle":
        if read_admin.admin_lifecycle(self, owner):
            return
    elif parsed.path == "/api/interactions":
        if read_admin.interactions(self, owner, limit):
            return
    elif parsed.path == "/api/public-posts":
        if read_public.public_posts(self, owner, limit):
            return
    elif parsed.path == "/api/admin/public-posts":
        if read_admin.admin_public_posts(self, owner, query, limit):
            return
    elif parsed.path == "/api/admin/public-post-controls":
        if read_admin.admin_public_post_controls(self, owner):
            return
    elif parsed.path == "/api/common-knowledge/public-key":
        if read_connections.common_knowledge_public_key(self, owner):
            return
    elif parsed.path == "/api/common-knowledge/discovery":
        if read_connections.common_knowledge_discovery(self, owner):
            return
    elif parsed.path == "/api/common-knowledge/feed":
        if read_connections.common_knowledge_feed(self, owner, query, limit):
            return
    elif parsed.path == "/api/common-knowledge/public":
        if read_connections.common_knowledge_public(self, owner, limit):
            return
    elif parsed.path == "/api/admin/conversations":
        if read_admin.admin_conversations(self, owner, query, limit):
            return
    elif parsed.path.startswith("/api/admin/conversations/") and parsed.path.endswith("/messages"):
        if read_admin.admin_conversations_messages(self, owner, parsed, query, limit):
            return
    elif parsed.path == "/api/mailbox":
        if read_admin.mailbox(self, owner, limit):
            return
    elif parsed.path == "/api/bounties":
        if read_wallet.bounties(self, owner, limit):
            return
    elif parsed.path.startswith("/api/bounties/") and parsed.path.endswith("/submissions"):
        if read_wallet.bounties_submissions(self, owner):
            return
    elif parsed.path.startswith("/api/bounties/"):
        if read_wallet.bounties_item(self, owner, parsed):
            return
    elif parsed.path == "/api/admin/wallet-bounties":
        if read_wallet.admin_wallet_bounties(self, owner, limit):
            return
    elif parsed.path == "/api/admin/wallet-submissions":
        if read_wallet.admin_wallet_submissions(self, owner, limit):
            return
    elif parsed.path == "/api/admin/wallet-orders":
        if read_wallet.admin_wallet_orders(self, owner, limit):
            return
    elif parsed.path == "/api/admin/wallet-audits":
        if read_wallet.admin_wallet_audits(self, owner, parsed):
            return
    elif parsed.path == "/api/admin/wallet-rewards":
        if read_wallet.admin_wallet_rewards(self, owner, limit):
            return
    elif parsed.path == "/api/admin/wallet-reward-incidents":
        if read_wallet.admin_wallet_reward_incidents(self, owner, limit):
            return
    elif parsed.path == "/api/admin/wallet-executions":
        if read_wallet.admin_wallet_executions(self, owner, limit):
            return
    elif parsed.path.startswith("/api/admin/wallet-executions/"):
        if read_wallet.admin_wallet_executions_item(self, owner, parsed, limit):
            return
    elif parsed.path == "/api/admin/wallet-policy":
        if read_wallet.admin_wallet_policy(self, owner):
            return
    elif parsed.path in {"/api/v1/admin/wallet-automation", "/api/admin/wallet-automation"}:
        if read_wallet.admin_wallet_automation(self, owner):
            return
    elif parsed.path in {"/api/v1/admin/provider-health", "/api/admin/provider-health"}:
        if health.admin_provider_health(self, owner, query):
            return
    elif parsed.path in {"/api/v1/admin/retention", "/api/admin/retention"}:
        if read_admin.admin_retention(self, owner):
            return
    elif parsed.path == "/api/admin/wallet-ledger":
        if read_wallet.admin_wallet_ledger(self, owner):
            return
    elif parsed.path == "/api/config/search-providers":
        if read_connections.config_search_providers(self, owner):
            return
    elif parsed.path == "/api/config/search-routing":
        if read_connections.config_search_routing(self, owner):
            return
    elif parsed.path == "/api/config/wallet-networks":
        if read_wallet.config_wallet_networks(self, owner, limit):
            return
    elif parsed.path == "/api/config/wallet-assets":
        if read_wallet.config_wallet_assets(self, owner, query, limit):
            return
    elif parsed.path == "/api/config/wallet-addresses":
        if read_wallet.config_wallet_addresses(self, owner, query, limit):
            return
    elif parsed.path == "/api/config/wallet-balances":
        if read_wallet.config_wallet_balances(self, owner, query, limit):
            return
    elif parsed.path == "/api/config/wallet-observation-health":
        if read_wallet.config_wallet_observation_health(self, owner, query):
            return
    elif parsed.path == "/api/config/wallet-balance-history":
        if read_wallet.config_wallet_balance_history(self, owner, query):
            return
    elif parsed.path == "/api/admin/wallet-acquisitions":
        if read_wallet.admin_wallet_acquisitions(self, owner, query, limit):
            return
    elif parsed.path == "/api/admin/wallet-acquisition-budget":
        if read_wallet.admin_wallet_acquisition_budget(self, owner, query):
            return
    elif parsed.path.startswith("/api/admin/wallet-acquisitions/"):
        if read_wallet.admin_wallet_acquisitions_item(self, owner, parsed):
            return
    elif parsed.path.startswith("/api/config/identity-challenges/"):
        if read_connections.config_identity_challenges_item(self, owner, parsed):
            return
    elif parsed.path == "/api/config/inbound-bindings":
        if read_connections.config_inbound_bindings(self, owner, limit):
            return
    elif parsed.path == "/api/config/model-capabilities":
        if read_cognition.config_model_capabilities(self, owner, query):
            return
    elif parsed.path.startswith("/api/config/model-resources/"):
        if read_cognition.config_model_resources_item(self, owner, parsed):
            return
    elif parsed.path == "/api/config/model-resources":
        if read_cognition.config_model_resources(self, owner, query, limit):
            return
    elif parsed.path == "/api/config/embedding-resources":
        if read_cognition.config_embedding_resources(self, owner):
            return
    elif parsed.path == "/api/config/training-policy":
        if read_exports.config_training_policy(self, owner):
            return
    elif parsed.path == "/api/config/common-knowledge":
        if read_connections.config_common_knowledge(self, owner, limit):
            return
    elif parsed.path == "/api/config/transports":
        if read_connections.config_transports(self, owner):
            return
    elif parsed.path == "/api/config/capabilities":
        if read_connections.config_capabilities(self, owner):
            return
    elif parsed.path == "/api/deliveries":
        if read_connections.deliveries(self, owner, limit):
            return
    elif parsed.path.startswith("/api/deliveries/") and parsed.path.endswith("/reconciliations"):
        if read_connections.deliveries_reconciliations(self, owner, parsed):
            return
    elif parsed.path == "/api/self-modification":
        if read_admin.self_modification(self, owner, limit):
            return
    elif parsed.path == "/api/admin/export-jobs":
        if read_exports.admin_export_jobs(self, owner, limit):
            return
    elif parsed.path.startswith("/api/admin/export-jobs/"):
        if read_exports.admin_export_jobs_item(self, owner, parsed):
            return
    elif parsed.path == "/api/admin/runtime-export":
        if read_exports.admin_runtime_export(self, owner, parsed):
            return
    elif parsed.path == "/api/admin/training-export":
        if read_exports.admin_training_export(self, owner, parsed):
            return
    else:
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
