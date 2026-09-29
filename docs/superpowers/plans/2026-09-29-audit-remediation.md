# Noyra 审计问题修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 按只读审计报告的根因依赖关系修复 A1–A18，建立可验证的生产安全、数据生命周期、Provider 故障切换、钱包状态机和发布门禁闭环。

**Architecture:** 先把生产配置和机密来源收敛到显式 profile，再把 retention、schema marker 和 integrity registry 统一为同一份持久化合同；Provider 与钱包只在这些基础稳定后增加状态机和故障注入验收，最后同步部署文档与 CI 门禁。每个 Gate 都以失败测试开始，以针对性测试、回归测试和独立提交结束。

**Tech Stack:** Python 3.12、SQLite、`pytest`、Ruff、HTTP 服务、systemd/Caddy/Nginx 示例、GitHub Actions。

**Spec:** `docs/audit/2026-09-29-full-readonly-audit.md`

## Global Constraints

- 只修改代码、测试、部署模板和文档，不访问或修改生产服务器、钱包、数据库或远端仓库。
- 自动付款必须继续受自动付款总开关、单笔上限、日限额和紧急暂停保护；白名单保持可选，不能变为默认强制项。
- API 密钥生产环境必须来自受权限保护的文件或 systemd credential；内联环境变量只能在显式 development/test profile 中启用。
- 所有行为变更必须遵循 TDD：先写一个能证明根因的失败测试，再实现最小修复，再运行模块回归测试。
- 每个 Gate 完成后运行 `python -m compileall -q src`、`ruff check .`、`ruff format --check .`、相关 pytest，并单独提交；不推送 GitHub。
- 真实 Provider、RPC、KMS、S3、Ubuntu、GitHub 保护规则和多日 soak 需要保留为外部验收项，不能用本地单元测试冒充。

---

### Gate 0：生产安全与配置闭环

#### Task 0.1：生产 profile 的监听和 Session Cookie 安全门禁（A1、A3、A8）

**Files:**
- Modify: `src/noyra/service.py`（`ServiceSettings`、监听安全校验、Session Cookie 配置）
- Modify: `deploy/noyra.env.example`
- Modify: `scripts/install-ubuntu.sh`
- Create: `scripts/preflight-production.py`
- Test: `tests/test_service_security_profile.py`

**Interfaces:**
- `ServiceSettings.from_env()` 读取 `NOYRA_PROFILE=development|test|production`。
- `ServiceSettings.validate_listener_security()` 在 production 中拒绝“非 loopback + 明文”和 `NOYRA_ALLOW_INSECURE_NON_LOOPBACK=true`。
- production profile 默认 `admin_session_cookie_secure=True`，且当配置了公网管理 URL 时不能显式降级。
- `scripts/preflight-production.py` 输出 JSON `{status, checks:[{id,status,reason}]}`，缺项时以非零退出。

- [x] **Step 1: Write the failing tests**

```python
def test_production_rejects_insecure_non_loopback():
    settings = ServiceSettings(
        host="0.0.0.0", profile="production", allow_insecure_non_loopback=True
    )
    with pytest.raises(ValueError, match="production.*non-loopback"):
        settings.validate_listener_security()


def test_production_sets_secure_admin_cookie_by_default():
    settings = ServiceSettings(profile="production")
    assert settings.admin_session_cookie_secure is True


def test_preflight_reports_missing_trusted_proxy_cidr(tmp_path):
    result = run_preflight(tmp_path, {"NOYRA_PROFILE": "production", "NOYRA_HOST": "127.0.0.1"})
    assert result.returncode != 0
    assert any(item["id"] == "trusted_proxy_cidrs" for item in result.json["checks"])
```

- [x] **Step 2: Run the tests and verify the expected failure**

Run: `python -m pytest -q tests/test_service_security_profile.py`

Expected: FAIL because the current settings do not have a production profile or preflight contract.

- [x] **Step 3: Implement the smallest production-profile change**

Add a profile parser and make production fail closed for insecure listener combinations. Keep development/test behavior compatible. Set the production cookie default to secure and make the preflight evaluate listener, HTTPS URL, trusted proxy, token, at-rest and secret-source requirements without mutating state.

- [x] **Step 4: Run focused and existing service tests**

Run: `python -m pytest -q tests/test_service_security_profile.py tests/test_service.py`

Expected: all pass except the existing platform-specific skip.

- [x] **Step 5: Commit**

```bash
git add src/noyra/service.py scripts/preflight-production.py deploy/noyra.env.example scripts/install-ubuntu.sh tests/test_service_security_profile.py
git commit -m "fix: enforce production listener and session security"
```

#### Task 0.2：机密来源强制化（A9）

**Files:**
- Modify: `src/noyra/core/credentials.py`
- Modify: `src/noyra/service.py`
- Modify: `deploy/noyra.env.example`
- Test: `tests/test_credentials.py` and `tests/test_service_security_profile.py`

**Interfaces:**
- `read_env_secret(name, *, allow_inline=None, profile=None)` 默认在 production 禁止 inline fallback。
- development/test 可以通过 `NOYRA_ALLOW_INLINE_SECRETS=true` 显式启用 inline fallback。
- preflight 必须报告每个 provider secret 的实际来源类别，但绝不输出 secret 值。

- [x] **Step 1: Add failing tests for production rejection and explicit development compatibility.**
- [x] **Step 2: Run the focused tests and verify they fail for the current fallback behavior.**
- [x] **Step 3: Implement source selection with explicit profile and non-secret diagnostics.**
- [x] **Step 4: Run credential, provider, service and preflight tests.**
- [x] **Step 5: Commit with `git commit -m "fix: require file-backed provider secrets in production"`.**

#### Task 0.3：CAPTCHA/IP 哈希密钥稳定化（A13）

**Files:**
- Modify: `src/noyra/service.py` and the public-post/CAPTCHA component where the process-random hash key is created
- Modify: `deploy/noyra.env.example`
- Test: `tests/test_public_posts.py` or a focused `tests/test_captcha_key.py`

**Interfaces:**
- `NOYRA_PUBLIC_HASH_KEY_FILE` supplies a stable 32-byte-or-longer secret in production.
- `PublicPostStore`/CAPTCHA hashing accepts an injected key and never generates a new production key during process startup.
- Development/test may generate an ephemeral key only when explicitly selected.

- [x] **Step 1: Add a restart-style failing test showing the same IP/challenge hashes differ with the current process-random key.**
- [x] **Step 2: Run it and confirm the failure.**
- [x] **Step 3: Load and validate the persistent key through the existing credential reader; reject missing production key.**
- [x] **Step 4: Test stable hashes, key permission errors, and public post regression behavior.**
- [x] **Step 5: Commit with `git commit -m "fix: persist public anti-abuse hash key"`.**

### Gate 1：数据生命周期、Schema 与完整性合同

#### Task 1.1：Retention 真实游标、失败持久化与 protected_rows（A4、A5、A6）

**Files:**
- Modify: `src/noyra/core/retention.py`
- Modify: `src/noyra/core/database.py` migrations for retention metadata
- Test: `tests/test_retention.py`

**Interfaces:**
- Retention run stores per-table cursor JSON `{table: {cursor, cutoff, deleted, protected}}` instead of one mixed cursor.
- A failure is written in a short independent transaction with stage, error class, retry time and failure count.
- `protected_rows` is computed from explicit retention predicates; if no predicate exists the API reports `protected_rows=None` with a reason rather than zero.

- [x] **Step 1: Add failing tests for multi-table cursor continuation, durable failure rows and protected-row counts.**
- [x] **Step 2: Run `python -m pytest -q tests/test_retention.py` and verify those tests fail.**
- [x] **Step 3: Implement per-table cursor and independent failure recording without changing protected/core tables.**
- [x] **Step 4: Run the retention suite plus database migration tests; verify restart continuation and rollback behavior.**
- [x] **Step 5: Commit with `git commit -m "fix: make retention runs resumable and durable"`.**

#### Task 1.2：Retention 覆盖范围和数据分类（A7）

**Files:**
- Modify: `src/noyra/core/retention.py`
- Modify: `src/noyra/core/database.py`
- Create or modify: `src/noyra/core/retention_registry.py`
- Modify: `tests/test_retention.py`

**Interfaces:**
- Every persistent table is classified as `core_evidence`, `required_audit`, `rebuildable_aggregate`, or `temporary_queue`.
- Each reclaimable table has a retention period, ordering key, independent cursor and protected predicate.
- Dry-run and run projections list table, cutoff, candidate count, protected count and deletion result.

- [x] **Step 1: Add fixture-backed failing tests for model calls, action attempts, behavior logs, research runs and provider/search aggregates.**
- [x] **Step 2: Verify the new tests fail because those tables are not in the current delete registry.**
- [x] **Step 3: Add the registry and route deletion through it, preserving immutable evidence and wallet audit rows.**
- [x] **Step 4: Run retention, export, integrity and storage-health tests; inspect generated projection for every table class.**
- [x] **Step 5: Commit with `git commit -m "fix: classify and retain all growing runtime tables"`.**

#### Task 1.3：正式 Schema/feature marker 和 Integrity checks（A14、A15）

**Files:**
- Modify: `src/noyra/core/database.py`
- Modify: `src/noyra/core/provider_health.py`
- Modify: `src/noyra/core/retention.py`
- Modify: `src/noyra/core/integrity.py`
- Test: `tests/test_management_integrity_audits.py`, `tests/test_provider_health.py`, `tests/test_retention.py`

**Interfaces:**
- Provider health and retention tables are created by migrations, not constructors.
- Schema marker includes feature versions and DDL fingerprints for those tables.
- Integrity registry exposes explicit checks for provider health state/buckets and retention runs/cursors.
- Older databases migrate deterministically; future versions fail closed before worker startup.

- [x] **Step 1: Add failing migration and integrity tests from an empty database, schema 67 database, and partially initialized database.**
- [x] **Step 2: Verify they fail because constructors currently create tables and registry has no explicit checks.**
- [x] **Step 3: Move DDL into migrations, add feature markers and versioned registry checks.**
- [x] **Step 4: Run migration, export, integrity, provider and retention suites, including rollback/quick-check tests.**
- [x] **Step 5: Commit with `git commit -m "fix: version runtime tables and integrity checks"`.**

### Gate 2：Provider 健康与故障切换

#### Task 2.1：健康统计窗口、失败率和恢复语义（A10、A11）

**Files:**
- Modify: `src/noyra/core/provider_health.py`
- Modify: provider routing modules under `src/noyra/model` and `src/noyra/search`
- Modify: management health API in `src/noyra/service.py`
- Test: `tests/test_provider_health.py` and focused routing tests

**Interfaces:**
- Health projection reports bounded windows, attempts, success/failure/timeout/429/5xx/auth/schema/unknown counts, failure rate, average and percentile latency, last success, breaker state and cooldown.
- Logical requests keep one trace/idempotency identity across failover; unknown outcomes enter reconcile and are not silently retried as a new side effect.
- Half-open recovery permits one probe and records its result.

- [x] **Step 1: Add failing tests for bounded windows, percentile fields, unknown outcome and one-probe cooldown recovery.**
- [x] **Step 2: Run provider/routing tests and verify missing fields or wrong transitions fail.**
- [x] **Step 3: Implement bounded aggregates and explicit outcome classification without storing URLs, tokens or full response bodies.**
- [x] **Step 4: Run provider, model, search and service health tests, then perform deterministic fault-injection tests for timeout, 429, 5xx, auth, schema and unknown.**
- [x] **Step 5: Commit with `git commit -m "fix: harden provider health and failover evidence"`.**

### Gate 3：钱包状态机与自动付款验收

#### Task 3.1：链状态原因码映射（A12）

**Files:**
- Modify: `src/noyra/wallet/execution.py`
- Modify: `src/noyra/wallet/workflow.py`
- Modify: `src/noyra/service.py` wallet projections
- Test: `tests/test_wallet_execution.py`, `tests/test_wallet_observation_breakdown.py`, `tests/test_wallet_observation_health.py`

**Interfaces:**
- Receipt, RPC and signer failures map to explicit `insufficient_balance`, `gas_too_high`, `nonce_conflict`, `confirmation_timeout`, `chain_reorg`, or `reconcile_required` where evidence supports it; otherwise remain `unknown` and require reconciliation.
- A logical payment ID remains stable across replacement/nonce bump; no new external side effect is created solely by an unknown receipt.
- Management projection shows current state, reason, age, retry/reconcile action and pause status.

- [x] **Step 1: Add failing fake-RPC/signer tests for each reason and unknown receipt path.**
- [x] **Step 2: Run wallet tests to confirm current coarse codes fail the assertions.**
- [x] **Step 3: Implement classification and state transitions with explicit evidence requirements.**
- [x] **Step 4: Run all wallet execution, fee admission, observation, economy, acquisition and release-gate tests.**
- [x] **Step 5: Commit with `git commit -m "fix: map wallet outcomes to explicit chain states"`.**

#### Task 3.2：自动付款组合故障验收（A18）

**Files:**
- Modify: `src/noyra/wallet/economy.py`
- Modify: `src/noyra/wallet/execution.py`
- Modify: `src/noyra/service.py`
- Test: `tests/test_wallet_automation_controls.py`, `tests/test_wallet_fee_admission.py`, `tests/test_wallet_release_gates.py`

**Interfaces:**
- Admission atomically checks global automation switch, emergency pause, per-order cap, daily amount/order limit, minimum balance and gas policy.
- Concurrent admission cannot exceed daily limits; retry/replacement uses the same logical payment identity.
- The management projection shows automation enabled/paused, limits, last failure state and reconciliation requirement.

- [x] **Step 1: Add failing concurrent and combined-fault tests (pause+retry, gas high+balance low, nonce conflict+restart, timeout+reorg).**
- [x] **Step 2: Verify failure against the current state machine.**
- [x] **Step 3: Implement only the missing atomic transitions and evidence persistence.**
- [x] **Step 4: Run the complete wallet suite and deterministic Sepolia-independent fake chain gate.**
- [x] **Step 5: Commit with `git commit -m "test: close automatic payment fault matrix"`.**

### Gate 4：部署文档、反向代理与发布供应链

#### Task 4.1：代理示例和能力矩阵同步（A2、A16）

**Files:**
- Modify: `deploy/caddy/noyra.Caddyfile.example`
- Modify: `deploy/nginx/noyra.conf.example`
- Modify: `docs/deployment/ubuntu.md`
- Modify: `README.md`, `README.en.md`, relevant admin/public docs
- Test: `tests/test_deployment_contract.py`

**Interfaces:**
- Examples declare HTTPS, trusted proxy CIDR, cookie security, public/admin host separation and loopback upstream together.
- A contract test checks every documented environment key and endpoint against the current runtime settings/API.

- [x] **Step 1: Add failing contract tests for missing proxy CIDR, stale endpoint and stale secret instructions.**
- [x] **Step 2: Run them and verify the current docs fail.**
- [x] **Step 3: Update examples and docs to the current capability matrix, including production preflight.**
- [x] **Step 4: Run documentation contract tests and installer syntax checks.**
- [x] **Step 5: Commit with `git commit -m "docs: align deployment contracts with runtime"`.**

#### Task 4.2：Release workflow 最小权限和生产证据门禁（A17）

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: release gate scripts under `scripts/`
- Test: `tests/test_release_workflow_contract.py`

**Interfaces:**
- Jobs request only the permissions they use; publishing is isolated behind protected environment/tag gates.
- Release artifact includes SHA, schema/feature markers, test evidence and offline-verifiable signatures.
- External evidence is marked as missing rather than inferred from local tests.

- [x] **Step 1: Add failing YAML/contract tests for top-level write permissions and missing production evidence declarations.**
- [x] **Step 2: Verify current workflow fails the contract.**
- [x] **Step 3: Split permissions by job, require protected release environment and add evidence manifest fields.**
- [x] **Step 4: Run workflow contract, gate runner and artifact verification tests.**
- [x] **Step 5: Commit with `git commit -m "ci: tighten release permissions and evidence gates"`.**

## Final verification gate

- [x] Run the full project test suite with the project environment.
- [x] Run `python -m compileall -q src`, `ruff check .`, `ruff format --check .`, deployment shell syntax checks and `git diff --check`.
- [x] Review `git diff` and `git status --short`; confirm no server files, secrets, runtime database or generated artifacts changed.
- [x] Reconcile every A1–A18 row against a test or an explicitly documented external validation requirement.
- [x] Commit only after fresh verification; do not push.
