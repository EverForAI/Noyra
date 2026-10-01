from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
ADMIN_HTML = (ROOT / "src" / "noyra" / "web" / "admin.html").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "src" / "noyra" / "web" / "admin.js").read_text(encoding="utf-8")


def test_migration_console_exposes_audited_pre_cutover_cancellation() -> None:
    assert 'data-migration-cancel="${esc(item.task_id)}"' in ADMIN_JS
    assert "data-migration-cancel]" in ADMIN_JS
    assert (
        '/cancel`, { method: "POST", body: JSON.stringify({ reason: reason.trim() }) }' in ADMIN_JS
    )
    assert "请输入取消迁移的原因" in ADMIN_JS
    assert 'id="migration-task-list"' in ADMIN_HTML


def test_migration_console_confirms_high_risk_modes_and_shows_evidence() -> None:
    assert "confirm_policy_auto" in ADMIN_JS
    assert "confirm_local_wallet_transfer" in ADMIN_JS
    assert 'window.confirm("策略自动模式会允许无需逐次人工批准的迁移' in ADMIN_JS
    assert "本地钱包迁移会在受保护通道中转移密钥" in ADMIN_JS
    assert "item.evidence" in ADMIN_JS
    assert "policy_revision" in ADMIN_JS
    assert "active_epoch" in ADMIN_JS
