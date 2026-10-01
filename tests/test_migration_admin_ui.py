from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
ADMIN_HTML = (ROOT / "src" / "noyra" / "web" / "admin.html").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "src" / "noyra" / "web" / "admin.js").read_text(encoding="utf-8")


def test_migration_console_exposes_audited_pre_cutover_cancellation() -> None:
    assert 'data-migration-cancel="${esc(item.task_id)}"' in ADMIN_JS
    assert 'data-migration-cancel]' in ADMIN_JS
    assert (
        '/cancel`, { method: "POST", body: JSON.stringify({ reason: reason.trim() }) }'
        in ADMIN_JS
    )
    assert "请输入取消迁移的原因" in ADMIN_JS
    assert 'id="migration-task-list"' in ADMIN_HTML
