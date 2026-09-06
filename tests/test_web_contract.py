from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
APP = (ROOT / "src" / "noyra" / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "src" / "noyra" / "web" / "index.html").read_text(encoding="utf-8")
ADMIN_HTML = (ROOT / "src" / "noyra" / "web" / "admin.html").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "src" / "noyra" / "web" / "admin.js").read_text(encoding="utf-8")
ADMIN_CSS = (ROOT / "src" / "noyra" / "web" / "admin.css").read_text(encoding="utf-8")


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


def test_operator_surface_exposes_controls_and_confirmation() -> None:
    assert 'id="operator-controls"' in HTML
    assert 'id="knowledge-trust-form"' in HTML
    assert 'id="knowledge-import-form"' in HTML
    assert 'id="knowledge-peer-form"' in HTML
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
