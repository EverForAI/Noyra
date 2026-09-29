# Noyra Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 从根因修复复审报告 F01–F18，逐模块建立可测试、可回滚、可审计的生产安全与运行可靠性闭环。

**Architecture:** 先以统一配置合同和身份/静态加密门禁收紧生产启动边界；随后把 retention、完整性和 SQLite 空间压力建模为可恢复的数据生命周期；再统一 Provider health 的状态、unknown 和 probe lease 语义；最后把钱包链重组状态、外部发布证据和 Compose 部署合同闭环。每个模块只改变其边界内的持久化和接口行为，并用迁移/兼容测试保护现有数据。

**Tech Stack:** Python 3.12、Pydantic、SQLite/WAL、pytest、Ruff、mypy、GitHub Actions、Docker Compose。

**Spec:** `docs/audit/2026-09-29-post-remediation-reaudit.md`

## Global Constraints

- 不修改或暴露真实服务器、钱包、API 密钥、数据库和远程仓库。
- 自动付款仍只受单笔上限、日限额和紧急暂停保护；不新增默认白名单、人工确认、月限额或订单笔数限制。
- append-only 证据不能通过普通 retention DELETE 破坏；未知链上结果不得隐式重试。
- 每个模块必须先有失败测试，再实现，再运行模块回归和 `git diff --check`。
- 每个模块通过后单独提交；不推送。

## 模块顺序

1. **生产安全配置与身份边界（F01–F05）**：production 强制 at-rest、keyring/卷证明和显式 genesis；preflight 清理继承的 NOYRA_*；operator token 复用安全读取器；runtime export 在 production 默认关闭。
2. **数据生命周期、迁移和存储压力（F06–F08、F10、F17、F18）**：移除 append-only route 删除冲突；实现可恢复 keyset cursor 和真实 cutoff；建立长期表分类与清理合同；增强 retention integrity；在可回收页面/配额压力下提供可验证、受锁保护的恢复路径；为 optional persistent features 提供 marker/fingerprint。
3. **Provider health 和故障切换（F09、F11、F12）**：完整校验 bucket/state 双向集合；返回统计窗口；把 unknown 纳入 reconcile/health 语义；将 half-open probe token 与完成请求 CAS 绑定。
4. **钱包链重组与发布/部署门禁（F13–F16）**：新增 reconcile_required/reorg durable projection 和冻结同一 logical payment 的动作；定义签名/版本化 external-gates 证据；补齐 Compose/production listener 合同和真实环境验收文档。

## 每模块验收

- 新增的根因回归测试先在未修复代码上失败，并记录失败原因。
- 模块测试、相关迁移/完整性测试、`python -m compileall src tests`、Ruff、mypy（若配置启用）和 `git diff --check` 通过。
- 检查数据库 schema marker、触发器、hash、append-only 约束和错误路径没有被绕过。
- 模块成功后提交，提交信息注明覆盖的 F 编号。
- 全部模块完成前，不宣称可以无人值守启用真实自动付款；外部门禁保持待验状态。

## 任务清单

### Task 1: 生产配置与身份门禁

**Files:**
- Modify: `src/noyra/service.py`
- Modify: `scripts/preflight-production.py`
- Modify: `src/noyra/core/credentials.py` / token reader call site
- Modify: `deploy/noyra.env.example`
- Test: `tests/test_service.py`, preflight tests, credential tests

- [ ] 为 at-rest、genesis、inherited environment、token file 和 production export 写失败测试。
- [ ] 实现 production-only validation 和安全文件读取复用。
- [ ] 验证 development/test 兼容行为不被收紧。
- [ ] 运行配置/preflight/credential 模块测试并提交。

### Task 2: Retention、迁移和 SQLite 压力

**Files:**
- Modify: `src/noyra/core/retention.py`, `src/noyra/core/integrity.py`, `src/noyra/core/storage.py`, `src/noyra/core/storage_lifecycle.py`, `src/noyra/core/database.py`
- Test: retention, integrity, storage lifecycle, migration tests

- [ ] 写 route append-only、cursor resume、canonical retention state、quota recovery 和 feature registry 失败测试。
- [ ] 让清理只处理明确可回收分类，记录真实 keyset/cutoff。
- [ ] 增加受锁保护且有空间预算的回收路径；禁止并发在线 VACUUM。
- [ ] 增加 optional feature marker/fingerprint 并验证旧库迁移和恢复。
- [ ] 运行模块回归并提交。

### Task 3: Provider health/failover

**Files:**
- Modify: `src/noyra/core/provider_health.py`, `src/noyra/core/integrity.py`, model/search routing call sites
- Test: provider health, search routing, model gateway, integrity tests

- [ ] 写 state 缺失/hash 损坏、窗口投影、unknown outcome 和 stale probe completion 失败测试。
- [ ] 双向校验 bucket/state provider identity。
- [ ] 绑定 probe token/CAS，明确 unknown 的 reconcile 结果和统计策略。
- [ ] 运行模块回归并提交。

### Task 4: Wallet reorg、external gates 和 Compose

**Files:**
- Modify: wallet execution/workflow/economy schema and API projection
- Modify: `.github/workflows/release.yml`, `scripts/build-release-evidence.py`, deployment docs/compose
- Test: wallet execution/workflow, release gate, deployment contract tests

- [ ] 写 receipt 消失/区块 hash 改变后的 durable reconcile_required 失败测试。
- [ ] 实现 append-only incident/compensation evidence 与同 logical payment 冻结。
- [ ] 定义版本化 external-gates schema、签名验证和固定 gate IDs。
- [ ] 统一 Compose/production listener profile 合同。
- [ ] 运行模块回归并提交。

### Task 5: 全量验证与交付状态

- [ ] 运行完整 pytest、Ruff、mypy、compileall、pip check、git diff --check。
- [ ] 检查工作区仅包含已提交内容，确认没有密钥或生产数据。
- [ ] 逐项更新审计报告的修复状态；仍需外部证据的项目明确标记。
- [ ] 不推送，向用户报告提交列表和剩余外部门禁。

