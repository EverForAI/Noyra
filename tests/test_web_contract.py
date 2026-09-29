from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
APP = (ROOT / "src" / "noyra" / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "src" / "noyra" / "web" / "index.html").read_text(encoding="utf-8")
ADMIN_HTML = (ROOT / "src" / "noyra" / "web" / "admin.html").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "src" / "noyra" / "web" / "admin.js").read_text(encoding="utf-8")
ADMIN_CSS = (ROOT / "src" / "noyra" / "web" / "admin.css").read_text(encoding="utf-8")
CADDY = (ROOT / "deploy" / "caddy" / "noyra.Caddyfile.example").read_text(encoding="utf-8")
NGINX = (ROOT / "deploy" / "nginx" / "noyra.conf.example").read_text(encoding="utf-8")
TOKEN_ROTATION = (ROOT / "scripts" / "rotate-operator-token.sh").read_text(encoding="utf-8")


def test_frontend_request_lifecycle_is_generation_and_abort_safe() -> None:
    assert "viewGeneration" in APP
    assert "AbortController" in APP
    assert "currentViewGeneration" in APP
    assert "signal" in APP
    assert "setInterval(async () =>" in APP
    assert "archive.blob()" not in APP


def test_export_frontend_has_bounded_streaming_and_backoff() -> None:
    assert "MAX_EXPORT_DOWNLOAD_BYTES" in APP
    assert "getReader()" in APP
    assert "Retry-After" in APP
    assert "delayMs = Math.min(delayMs * 2, 5000)" in APP
    assert "/api/admin/export-jobs/${encodeURIComponent(jobId)}/cancel" in APP


def test_public_surface_keeps_operator_controls_private() -> None:
    for marker in (
        'id="operator-controls"',
        'id="knowledge-trust-form"',
        'id="knowledge-import-form"',
        'id="knowledge-peer-form"',
    ):
        assert marker not in HTML
    assert "confirmPrivilegedAction" in APP
    for path in (
        "/api/admin/lifecycle/",
        "/api/admin/actions/",
        "/api/admin/model-calls/",
        "/api/deliveries/",
        "/api/config/training-policy",
        "/api/config/common-knowledge/trust",
    ):
        assert path in APP


def test_admin_surface_assets_and_session_contract() -> None:
    assert ADMIN_CSS.strip()
    for marker in (
        'id="login-form"',
        'id="admin-shell"',
        'data-section-panel="overview"',
        'data-section-panel="conversation"',
        'data-section-panel="models"',
        'data-section-panel="runtime"',
    ):
        assert marker in ADMIN_HTML
    assert '<link rel="stylesheet" href="/admin.css">' in ADMIN_HTML
    assert '<script src="/admin.js" defer></script>' in ADMIN_HTML
    assert 'credentials: "same-origin"' in ADMIN_JS
    assert 'headers["X-CSRF-Token"]' in ADMIN_JS
    assert "fetch(path" in ADMIN_JS


def test_wallet_automation_admin_surface_shows_reconciliation_state() -> None:
    assert 'id="wallet-automation-failure"' in ADMIN_HTML
    assert "automation.latest_failure" in ADMIN_JS
    assert "failure.requires_reconciliation" in ADMIN_JS
    assert "broadcast_unknown" in ADMIN_JS


def test_public_surface_has_branded_archive_and_captcha_states() -> None:
    for marker in (
        'class="hero"',
        'src="/assets/public-hero.webp?v=__PUBLIC_HERO_VERSION__"',
        'id="archive"',
        'id="contribute"',
        'id="captcha-status"',
        'id="header-state-dot"',
    ):
        assert marker in HTML
    for marker in (
        "publicErrorMessage",
        "at_rest_boundary_unavailable",
        "captcha-image-wrap",
        "is-online",
    ):
        assert marker in APP or marker in HTML


def test_public_surface_localizes_network_failures() -> None:
    assert "public_network_unavailable" in APP
    assert "公开档案暂时无法连接" in APP
    assert "稍后刷新页面" in APP
    assert "publicErrorMessage(error)" in APP


def test_admin_model_resource_controls_are_precise_bounded_and_escaped() -> None:
    for marker in (
        'id="admin-model-form"',
        'id="model-list"',
        'data-section-panel="capabilities"',
        'id="capability-list"',
        'id="capability-status"',
    ):
        assert marker in ADMIN_HTML
    for path in (
        "/api/config/model-resources/${encodeURIComponent(item.group_id)}/keys",
        "/api/config/model-resources/${encodeURIComponent(form.dataset.id)}/update",
        "/api/config/capabilities/${encodeURIComponent(button.dataset.id)}/revoke",
    ):
        assert path in ADMIN_JS
    assert "MODEL_KEY_REQUEST_CONCURRENCY = 4" in ADMIN_JS
    assert "mapWithConcurrency(rows, MODEL_KEY_REQUEST_CONCURRENCY" in ADMIN_JS
    assert "Promise.all(rows.map" not in ADMIN_JS
    assert "SQLITE_INT64_MAX = 9223372036854775807n" in ADMIN_JS
    assert "return parsed.toString()" in ADMIN_JS
    assert "const exactBudget = item.exact_budget || item" in ADMIN_JS
    assert "exactBudget.daily_input_tokens" in ADMIN_JS
    assert "formatMicroUsd(exactBudget.daily_cost_microusd)" in ADMIN_JS
    assert "payload.daily_cost_limit_usd = normalizeBudgetCost" in ADMIN_JS
    for escaped_value in (
        "esc(item.label)",
        "esc(item.group_id)",
        "esc(key.key_id)",
        "esc(item.capability_type)",
        "esc(scope)",
        "esc(item.grant_id)",
    ):
        assert escaped_value in ADMIN_JS


def test_admin_forms_have_labels_hidden_containers_and_submit_fallbacks() -> None:
    for control_id in (
        "admin-message",
        "admin-model-pool",
        "admin-model-label",
        "admin-model-url",
        "admin-model-name",
        "admin-model-keys",
    ):
        assert f'<label for="{control_id}"' in ADMIN_HTML
    assert 'id="admin-model-url" type="url"' in ADMIN_HTML
    assert 'data-channel-field="admin-channel-app-id"' in ADMIN_HTML
    assert 'data-channel-field="admin-channel-smtp-password"' in ADMIN_HTML
    assert "event.submitter || form.querySelector" in ADMIN_JS
    assert "container.hidden = !visible" in ADMIN_JS
    assert "form.reset()" in ADMIN_JS
    assert "event.target.reset()" not in ADMIN_JS


def test_admin_model_and_search_configuration_controls_are_present() -> None:
    for marker in (
        'id="admin-model-test"',
        'id="admin-model-discover"',
        'id="admin-model-options"',
        'data-section-panel="search"',
        'id="admin-search-provider-form"',
        'id="admin-search-provider-test"',
        'id="search-routing-mode"',
    ):
        assert marker in ADMIN_HTML
    for marker in (
        "/api/config/model-resources/test",
        "/api/config/model-resources/models",
        "/api/config/search-providers",
        "model_first",
        "api_first",
        "auto",
    ):
        assert marker in ADMIN_JS or marker in ADMIN_HTML


def test_admin_setup_guide_and_observability_are_present_and_localized() -> None:
    for marker in (
        'id="setup-guide"',
        'id="setup-guide-list"',
        'id="model-observability"',
        'id="wallet-audit-list"',
    ):
        assert marker in ADMIN_HTML
    assert "renderSetupGuide" in ADMIN_JS
    assert "diagnostics.model_observability" in ADMIN_JS
    assert '"/api/admin/wallet-audits?limit=100"' in ADMIN_JS
    assert 'wallet_reward_workflow_manual_intervention: "奖励流程需要人工处理"' in ADMIN_JS
    assert 'wallet_payment_order_awaiting_confirmation: "等待付款确认"' in ADMIN_JS
    assert 'walletAuditActionLabels[row.action] || "其他钱包操作"' in ADMIN_JS
    assert "Promise.allSettled" in ADMIN_JS
    assert "admin_network_unavailable" in ADMIN_JS
    assert "管理台暂时无法连接服务" in ADMIN_JS


def test_public_page_has_search_metadata_crawler_rules_and_versioned_assets() -> None:
    for marker in (
        "<!-- NOYRA_SEO_METADATA -->",
        "public-hero.webp?v=__PUBLIC_HERO_VERSION__",
        'name="twitter:card"',
    ):
        assert marker in HTML
    assert 'name="robots" content="noindex, nofollow"' in ADMIN_HTML


def test_https_public_and_admin_deployment_contract_is_documented() -> None:
    assert "reverse_proxy 127.0.0.1:8765" in CADDY
    assert "proxy_pass http://127.0.0.1:8765" in NGINX
    assert "return 301 https://$host$request_uri" in NGINX
    assert "NOYRA_OPERATOR_TOKEN_FILE" in TOKEN_ROTATION
    assert "systemctl restart" in TOKEN_ROTATION
