from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
ADMIN_HTML = (ROOT / "src" / "noyra" / "web" / "admin.html").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "src" / "noyra" / "web" / "admin.js").read_text(encoding="utf-8")


def test_public_post_moderation_has_an_independent_admin_section() -> None:
    assert 'data-section="public-posts"' in ADMIN_HTML
    assert 'data-section-panel="public-posts"' in ADMIN_HTML
    assert 'id="public-post-status-filter"' in ADMIN_HTML
    assert 'value="pending_review"' in ADMIN_HTML
    assert 'id="public-post-review-list"' in ADMIN_HTML
    assert 'id="load-more-public-posts"' in ADMIN_HTML


def test_wallet_acquisition_admin_surface_is_observable_and_read_only() -> None:
    for marker in (
        'data-section="wallet"',
        'data-section-panel="wallet"',
        'id="wallet-graph-list"',
        'id="wallet-budget-network"',
        'id="wallet-acquisition-list"',
        'id="wallet-enqueue-form"',
        'id="run-wallet-acquisitions"',
        'id="wallet-observation-health-list"',
        'id="wallet-observation-group"',
        'id="refresh-wallet-observation-health"',
    ):
        assert marker in ADMIN_HTML
    for marker in (
        "/api/config/wallet-networks?limit=100",
        "/api/config/wallet-balances?limit=100",
        "/api/admin/wallet-acquisitions?limit=100",
        "/api/admin/wallet-acquisition-budget?network_id=",
        "/api/admin/wallet-acquisitions/run",
        "/api/config/wallet-observation-health?group_by=",
        "/attempts",
        'data-wallet-acquisition-action="retry"',
        "wallet_acquisition_unknown_retry_not_allowed",
        "wallet-acquisition-reason",
    ):
        assert marker in ADMIN_JS


def test_wallet_execution_admin_surface_can_start_and_manage_transfers() -> None:
    for marker in (
        'id="wallet-execution-list"',
        'id="recover-wallet-executions"',
        'id="refresh-wallet-executions"',
    ):
        assert marker in ADMIN_HTML
    for marker in (
        'data-wallet-order-action="execute"',
        "/api/admin/wallet-orders/${encodeURIComponent(button.dataset.orderId)}/execute",
        'data-wallet-execution-action="receipt"',
        'data-wallet-execution-action="retry"',
        'data-wallet-execution-action="refund"',
    ):
        assert marker in ADMIN_JS


def test_public_post_moderation_uses_filtered_cursor_pagination() -> None:
    assert 'let path = "/api/admin/public-posts?limit=50"' in ADMIN_JS
    assert 'if (filter !== "all") path += `&status=${encodeURIComponent(filter)}`' in ADMIN_JS
    assert "&cursor=${encodeURIComponent(publicPostCursor)}" in ADMIN_JS
    assert "`${last.created_at}|${last.post_id}`" in ADMIN_JS
    assert "loadPublicPosts({ append: true })" in ADMIN_JS


def test_public_post_moderation_actions_are_safe_and_retryable() -> None:
    for marker in (
        'data-public-post-action="publish"',
        'data-public-post-action="reject"',
        'data-public-post-action="archive"',
        "reasonInput?.value.trim()",
        'data-public-post-reason maxlength="2000" required',
        "expected_status: expectedStatus",
        "idempotency_key: idempotencyKey",
        'button.textContent = "处理中…"',
        "control.disabled = true",
        'card.querySelectorAll("button, input, textarea")',
        "`${postId}:${expectedStatus}:${operation}:${reason}`",
        "publicPostActionKeys.delete(actionKey)",
    ):
        assert marker in ADMIN_JS
    assert "esc(row.title)" in ADMIN_JS
    assert "esc(row.content)" in ADMIN_JS
    assert "esc(row.author_label)" in ADMIN_JS
