# 钱包、供应商健康与公开站点升级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在不破坏现有钱包、模型、搜索和公开接口兼容性的前提下，完成受限自动付款控制、供应商健康路由、聚合数据保留清理，以及治愈科技风公开站点升级。

**Architecture:** 采用追加式 SQLite 迁移。钱包和供应商领域逻辑分别由现有 store/engine 扩展，服务层只输出脱敏投影；统计写入按 UTC 时间桶聚合，清理任务按固定批次执行。公开站点继续使用原生 HTML/CSS/JavaScript，首屏与社交分享图片作为静态 WebP/PNG 资源。

**Tech Stack:** Python 3.12、SQLite、Pydantic、现有 HTTP 服务、pytest、原生 HTML/CSS/JavaScript、WebP/PNG 图片资源。

**Spec:** `docs/superpowers/specs/2026-09-28-wallet-provider-public-upgrade-design.md`

## Global Constraints

- 自动付款默认关闭；启用后只受总开关、单笔上限、日限额、余额/费用检查和紧急暂停保护，白名单仍为可选且默认关闭。
- 钱包状态生命周期保持兼容，细分原因只使用固定白名单代码，未知错误不得进入公开投影。
- 健康统计不得写入 API 密钥、请求正文、响应正文或带凭据 URL；重复尝试按幂等标识只计数一次。
- 清理默认保留聚合健康数据 30 天、普通遥测 7 天、普通审计 365 天，关键钱包安全事件长期保留；每次最多清理 500 行。
- 管理台所有新增操作必须认证、写理由和审计；公开接口不泄露余额、密钥、内部异常栈。
- 所有页面文案保持中文，支持 375/430/768px 断点、键盘焦点、WCAG AA 对比度和 reduced-motion。

---

### Task 1: 钱包策略、原因码与追加迁移

**Files:**
- Modify: `src/noyra/core/database.py`
- Modify: `src/noyra/wallet/economy_types.py`
- Modify: `src/noyra/wallet/economy.py`
- Modify: `src/noyra/wallet/execution.py`
- Modify: `src/noyra/core/wallet_schema.py`
- Create: `tests/test_wallet_automation_controls.py`

**Interfaces:**
- `PaymentPolicyInput.automation_enabled: bool = False`.
- `PaymentPolicyRecord.automation_enabled: bool`.
- `WALLET_PAYMENT_REASON_CODES` 为固定 `frozenset[str]`，包含设计文档中的九个代码。
- `WalletEconomyStore.get_policy()` 和 `update_policy()` 继续返回旧字段，并返回新开关。
- `WalletPaymentExecutionEngine` 写入 `reason_code`（同时兼容读取 `error_code`），并提供 `reason_projection(execution)`。

- [x] **Step 1: 写失败测试**：创建默认策略断言 `automation_enabled is False`；开启自动模式但开关关闭时拒绝新的自动预留；紧急暂停返回 `wallet_automation_paused`；版本冲突和重复幂等更新不产生第二条审计；九个原因码可投影且未知码被归一为 `unknown`。
- [x] **Step 2: 运行测试确认失败**：`pytest tests/test_wallet_automation_controls.py -q`，预期缺少列、字段或稳定错误。
- [x] **Step 3: 增加迁移**：将 `CURRENT_SCHEMA_VERSION` 提升一个版本；为 `wallet_payment_policies` 追加 `automation_enabled INTEGER NOT NULL DEFAULT 0 CHECK (automation_enabled IN (0,1))`；为执行表追加 `reason_code TEXT`，保留旧 `error_code`；迁移后为旧主体填充 0，并重算策略状态哈希。
- [x] **Step 4: 实现领域校验**：更新 Pydantic 输入、状态哈希字段和 `_write_policy`；自动付款路径同时检查 mode、开关、暂停、限额与白名单开关；所有链错误映射到固定原因码。
- [x] **Step 5: 运行聚焦测试**：`pytest tests/test_wallet_automation_controls.py tests/test_wallet_economy.py tests/test_wallet_execution.py -q`。
- [x] **Step 6: 提交**：`git add src/noyra/core/database.py src/noyra/core/wallet_schema.py src/noyra/wallet/economy_types.py src/noyra/wallet/economy.py src/noyra/wallet/execution.py tests/test_wallet_automation_controls.py; git commit -m "feat: add wallet automation controls and reason codes"`

### Task 2: 钱包管理 API 与管理台状态面板

**Files:**
- Modify: `src/noyra/service.py`
- Modify: `src/noyra/service_contract.py`
- Modify: `src/noyra/web/admin.html`
- Modify: `src/noyra/web/admin.js`
- Modify: `src/noyra/web/admin.css`
- Create: `tests/test_wallet_automation_api.py`

**Interfaces:**
- `GET /api/v1/admin/wallet-automation` 返回 `automation_enabled`、`mode`、`emergency_paused`、限额、活动执行数、按 `reason_code` 聚合的待处理数量。
- `POST /api/v1/admin/wallet-automation` 接收 `expected_version`、`automation_enabled`、`mode`、`reason`、`idempotency_key`。
- `POST /api/v1/admin/wallet-automation/pause` 与 `/resume` 接收理由和期望版本，返回新策略版本。
- 管理台状态横幅使用中文标签“自动付款已开启/已关闭”“紧急暂停”“需要处理”，并在高风险状态下使用可读文本和 `role=status`。

- [x] **Step 1: 写失败路由契约测试**：未认证返回 401；版本不符返回 409；启用和暂停写审计；重复幂等返回相同版本；状态投影不含私钥、余额明细或异常栈。
- [x] **Step 2: 运行测试确认失败**：`pytest tests/test_wallet_automation_api.py -q`。
- [x] **Step 3: 实现服务投影与路由**：复用现有操作令牌/会话认证、`WalletEconomyStore` 和 `OperatorControlService`；请求字段严格校验；错误只映射稳定 HTTP 错误。
- [x] **Step 4: 实现管理台**：首屏加载状态，按钮调用上述 API；按钮在网络请求期间禁用；成功后刷新状态，失败展示中文原因。
- [x] **Step 5: 运行测试与语法检查**：`pytest tests/test_wallet_automation_api.py tests/test_web_contract.py -q`；`node --check src/noyra/web/admin.js`。
- [x] **Step 6: 提交**：`git add src/noyra/service.py src/noyra/service_contract.py src/noyra/web/admin.html src/noyra/web/admin.js src/noyra/web/admin.css tests/test_wallet_automation_api.py; git commit -m "feat: expose wallet automation controls in admin"`

### Task 3: 统一供应商健康聚合与故障切换

**Files:**
- Modify: `src/noyra/core/database.py`
- Create: `src/noyra/core/provider_health.py`
- Modify: `src/noyra/model/gateway.py`
- Modify: `src/noyra/model/resources.py`
- Modify: `src/noyra/research/provider.py`
- Modify: `src/noyra/research/routing.py`
- Modify: `src/noyra/cognition/research.py`
- Create: `tests/test_provider_health.py`

**Interfaces:**
- `ProviderHealthStore.record_attempt(subject_id, provider_kind, provider_id, attempt_id, success, latency_ms, error_code)` 幂等写入 UTC 小时桶。
- `ProviderHealthStore.list_projection(subject_id, provider_kind)` 返回 provider id、状态、失败率、平均响应时间、最近成功时间、冷却截止时间。
- `ProviderHealthStore.begin_probe(...)` 每个冷却供应商只允许一个半开探测。
- 路由选择统一使用 `priority`、`weight`、健康状态过滤、单次切换和冷却恢复；未知结果不自动重复副作用。
- 既有模型和搜索字段缺失时使用旧顺序作为优先级默认值。

- [x] **Step 1: 写失败测试**：同一 `attempt_id` 重复记录只计一次；失败率和平均延迟正确；最低优先级可用供应商胜出；可重试失败切换；冷却期只有一个半开探测；未知失败不切换。
- [x] **Step 2: 运行测试确认失败**：`pytest tests/test_provider_health.py -q`。
- [x] **Step 3: 添加聚合表和哈希**：新增 `provider_health_buckets` 与 `provider_health_probes`，包含主体、kind、provider、时间桶、计数、延迟、最近成功/失败、状态哈希和唯一尝试索引。
- [x] **Step 4: 实现健康存储和路由适配**：模型 gateway 与搜索 executor 在现有结果事务内调用 `record_attempt`；只对明确可重试错误尝试下一资源；冷却结束用探测锁恢复。
- [x] **Step 5: 增加服务读投影**：`GET /api/v1/admin/provider-health?kind=model|search` 返回脱敏聚合；`POST /api/v1/admin/provider-health/{provider_id}/probe` 触发一次受限探测。
- [x] **Step 6: 运行聚焦测试**：`pytest tests/test_provider_health.py tests/test_model_gateway.py tests/test_research.py tests/test_service.py -k "provider or routing" -q`。
- [x] **Step 7: 提交**：`git add src/noyra/core/database.py src/noyra/core/provider_health.py src/noyra/model/gateway.py src/noyra/model/resources.py src/noyra/research/provider.py src/noyra/research/routing.py src/noyra/cognition/research.py src/noyra/service.py tests/test_provider_health.py; git commit -m "feat: add provider health aggregation and failover"`

### Task 4: 供应商优先级和健康管理台

**Files:**
- Modify: `src/noyra/web/admin.html`
- Modify: `src/noyra/web/admin.js`
- Modify: `src/noyra/web/admin.css`
- Modify: `tests/test_web_contract.py`

**Interfaces:**
- 模型和搜索列表显示优先级、权重、状态、失败率、平均响应和最近成功。
- 表单支持数字优先级、权重、冷却秒数；保存后刷新健康投影。
- 管理台无密钥展示，健康数据只显示聚合字段。

- [x] **Step 1: 写失败契约测试**：HTML 存在中文优先级、权重、状态字段、健康刷新按钮和无障碍状态容器。
- [x] **Step 2: 实现 HTML/JS/CSS**：复用现有模型/搜索配置组件，加入健康表格和错误空状态；移动端改为可滚动卡片。
- [x] **Step 3: 运行验证**：`pytest tests/test_web_contract.py -q`；`node --check src/noyra/web/admin.js`。
- [x] **Step 4: 提交**：`git add src/noyra/web/admin.html src/noyra/web/admin.js src/noyra/web/admin.css tests/test_web_contract.py; git commit -m "feat: show provider health and priority in admin"`

### Task 5: 聚合保留配置与分批清理

**Files:**
- Modify: `src/noyra/core/database.py`
- Create: `src/noyra/core/retention.py`
- Modify: `src/noyra/service.py`
- Modify: `deploy/noyra.env.example`
- Create: `tests/test_retention.py`

**Interfaces:**
- `RetentionSettings.from_env(environ)` 读取并校验五个保留配置，默认值为设计文档规定值。
- `RetentionManager.run_batch(subject_id, now, batch_size=500)` 返回 `deleted_by_table`、`protected_rows`、`failed_reason`、`next_cursor`。
- `GET /api/v1/admin/retention` 返回配置、估算可清理数量、最近运行结果；`POST /api/v1/admin/retention/run` 执行一批并写审计。
- 删除只针对无外键引用的成功遥测和过期健康桶；关键钱包安全审计和存在外键引用的记录永不直接删除。

- [x] **Step 1: 写失败测试**：配置默认/边界校验；每批不超过 500；重复运行可继续；外键引用保护；关键钱包安全审计不删除。
- [x] **Step 2: 运行测试确认失败**：`pytest tests/test_retention.py -q`。
- [x] **Step 3: 实现设置、表与索引**：新增 `retention_runs`；对 provider bucket、model/search usage 和成功行为日志使用时间索引；失败清理保留原因。
- [x] **Step 4: 接入维护循环**：在服务周期维护点调用一次 `run_batch`，异常只记录安全审计，不影响主循环。
- [x] **Step 5: 实现管理 API**：输出清理计数和保留边界，不输出原始 payload。
- [x] **Step 6: 运行测试与提交**：`pytest tests/test_retention.py tests/test_storage.py -q`；`git add ...; git commit -m "feat: add bounded retention cleanup"`

### Task 6: 公开站点治愈科技视觉和图片资源

**Files:**
- Create: `src/noyra/web/assets/public-hero-calm.webp`
- Create: `src/noyra/web/assets/public-social-calm.png`
- Modify: `src/noyra/web/index.html`
- Modify: `src/noyra/web/styles.css`
- Modify: `site/index.html`
- Modify: `site/assets/site.css`
- Modify: `website/content.mjs`
- Modify: `website/build.mjs`
- Create: `tests/test_public_visual_contract.py`

**Interfaces:**
- 图片尺寸分别为首屏 1536×1024 和社交图 1200×630；首屏图明确 `width/height/fetchpriority`，社交图加入 `og:image` 与 `twitter:image`。
- 首屏使用薄荷、雾蓝、暖金和微光网络，左侧保留文案安全区；窄屏用 `object-position` 或移动资源避免遮挡。
- 页面提供 `prefers-reduced-motion`、可见焦点、验证码失败替代文本和低流量加载策略。

- [x] **Step 1: 生成资源**：按 imagegen 技能生成治愈科技首屏和社交分享图；转换为 WebP/PNG 并检查尺寸、文件大小。
- [x] **Step 2: 写失败静态测试**：检查两个页面的分享元数据、图片尺寸属性、中文可访问标签和 reduced-motion CSS。
- [x] **Step 3: 修改页面与样式**：调整 hero 高度、卡片层次、对比度、移动布局和验证码错误态；公开档案保留现有数据接口。
- [x] **Step 4: 运行验证**：`pytest tests/test_public_visual_contract.py tests/test_public_preview_profile.py -q`；用 `node --check site/assets/site.js` 和现有构建脚本检查。
- [x] **Step 5: 提交**：`git add src/noyra/web site website tests/test_public_visual_contract.py; git commit -m "feat: refresh public site with calm tech visuals"`

### Task 7: 全量验证、差异审计与本地交付

**Files:**
- Modify only files required by failing checks.

- [x] **Step 1: 运行受影响测试**：`pytest tests/test_wallet_automation_controls.py tests/test_wallet_automation_api.py tests/test_provider_health.py tests/test_retention.py tests/test_public_visual_contract.py tests/test_web_contract.py -q`。
- [x] **Step 2: 运行静态检查**：`ruff check src tests`、`ruff format --check src tests`、`node --check src/noyra/web/admin.js`。
- [x] **Step 3: 运行完整测试**：`pytest -q`，将环境相关失败分离为真实回归与已有问题。
- [x] **Step 4: 检查安全与差异**：`git diff --check`、敏感字段扫描、迁移版本检查、图片尺寸检查。
- [x] **Step 5: 提交最终修复**：每个修复单独提交；确认 `git status --short` 为空且不执行 push。
