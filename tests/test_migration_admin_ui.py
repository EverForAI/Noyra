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


def test_migration_console_does_not_offer_unproven_cutover_submission() -> None:
    assert "等待安全证明" in ADMIN_JS
    assert "disabled title=\"等待加密迁移包和目标恢复证明接线\"" in ADMIN_JS
    assert "/cutover`, { method: \"POST\", body: JSON.stringify({})" not in ADMIN_JS


def test_migration_console_confirms_high_risk_modes_and_shows_evidence() -> None:
    assert "confirm_policy_auto" in ADMIN_JS
    assert "confirm_local_wallet_transfer" in ADMIN_JS
    assert 'window.confirm("策略自动模式会允许无需逐次人工批准的迁移' in ADMIN_JS
    assert "本地钱包迁移会在受保护通道中转移密钥" in ADMIN_JS
    assert "item.evidence" in ADMIN_JS
    assert "policy_revision" in ADMIN_JS
    assert "active_epoch" in ADMIN_JS


def test_migration_console_exposes_emergency_recovery_proof_form() -> None:
    assert 'id="migration-recovery-form"' in ADMIN_HTML
    assert 'id="migration-recovery-task-id"' in ADMIN_HTML
    assert 'id="migration-recovery-standby-target-id"' in ADMIN_HTML
    assert 'id="migration-recovery-backup-id"' in ADMIN_HTML
    assert 'id="migration-recovery-manifest-digest"' in ADMIN_HTML
    assert 'id="migration-recovery-restore-report-digest"' in ADMIN_HTML
    assert 'id="migration-recovery-health-report-digest"' in ADMIN_HTML
    assert 'id="migration-recovery-target-signature"' in ADMIN_HTML
    assert 'id="migration-recovery-source-failure-evidence"' in ADMIN_HTML
    assert 'id="migration-recovery-status"' in ADMIN_HTML
    assert "/api/v1/admin/migration/recovery" in ADMIN_JS
    assert "migration_recovery_rejected" in ADMIN_JS
    assert "紧急恢复已提交" in ADMIN_JS
