# 管理台一键升级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在管理台提供可断线的一键版本检查与升级，并保持现有备份、健康检查和回滚边界。

**Architecture:** 服务端的 `UpgradeManager` 负责读取版本源、持久化脱敏任务状态并生成固定格式的升级请求。root-owned systemd runner 负责从 GitHub 获取源码并调用现有安装器；HTTP 层只负责认证和提交任务。管理台通过两个受保护 API 轮询状态。

**Tech Stack:** Python 3.11+, stdlib `urllib`, SQLite/filesystem state, systemd, Bash, vanilla JavaScript/CSS, pytest.

**Spec:** `docs/superpowers/specs/2026-10-01-admin-upgrade-design.md`

## Global Constraints

- 不把令牌、环境文件、API 密钥或完整命令行写入状态、审计或日志。
- 不改变现有安装器的加密备份、安装锁、原子切换、就绪检查和失败回滚行为。
- HTTP 请求不执行 root 安装动作；升级必须可在浏览器和 SSH 断开后继续。
- 同一时间最多一个升级任务，重复请求必须幂等。

---

### Task 1: Upgrade manager and HTTP contract

**Files:**
- Create: `src/noyra/core/upgrade.py`
- Modify: `src/noyra/service.py`
- Modify: `src/noyra/service_contract.py`
- Test: `tests/test_upgrade_manager.py`
- Test: `tests/test_service.py`

- [x] Write failing tests for clean-source validation, version check projection, persistent status, idempotent start, and secret redaction.
- [x] Implement `UpgradeManager` with explicit paths/configuration, bounded GitHub metadata parsing, atomic JSON writes, and a single process lock.
- [x] Add authenticated GET/POST routes and stable error responses; call existing `audit_admin_event` without sensitive values.
- [x] Run focused backend tests. Keep the implementation in the working tree for review; deployment, commit, and push are outside this request.

### Task 2: Detached Ubuntu runner and install integration

**Files:**
- Create: `scripts/upgrade-ubuntu-runner.sh`
- Create: `deploy/systemd/noyra-upgrade.service`
- Modify: `scripts/install-ubuntu.sh`
- Modify: `docs/deployment/ubuntu.md`
- Test: `tests/shell/test-upgrade-runner.sh`

- [x] Write shell tests for fixed source path, no user shell input, lock handling, and status transitions.
- [x] Implement root runner that consumes one request, fetches the requested SHA, verifies a clean worktree and fast-forward main, invokes the existing installer, and writes only redacted progress.
- [x] Install and enable the runner unit from the installer without changing the application service contract; restore prior runner/unit files on failed install.
- [x] Document management-led upgrades and recovery/status commands as emergency fallback only.
- [x] Run shell syntax and focused tests. Keep the implementation in the working tree for review; deployment, commit, and push are outside this request.

### Task 3: Management UI

**Files:**
- Modify: `src/noyra/web/admin.html`
- Modify: `src/noyra/web/admin.js`
- Modify: `src/noyra/web/admin.css`
- Test: `tests/test_web_contract.py`

- [x] Add the Chinese version card, check/upgrade buttons, confirmation dialog, status labels, and bounded log summary.
- [x] Implement request handlers and polling with cancellation on logout and session expiry.
- [x] Add responsive styles and accessible live status text.
- [x] Run web contract tests and `node --check`.

### Task 4: Full verification and documentation

**Files:**
- Modify: `docs/deployment/setup-modes.md`
- Modify: `README.md`
- Test: existing focused and deployment tests

- [x] Run focused pytest, shell syntax/tests, ruff, compileall, JavaScript syntax, and diff checks.
- [x] Review status/log payloads for secret-shaped fields and verify route inventory.
- [x] Update user-facing deployment instructions. Keep the implementation in the working tree for review; deployment, commit, and push are outside this request.
