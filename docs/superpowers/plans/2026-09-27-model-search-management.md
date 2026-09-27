# 模型与搜索管理功能实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在中文管理台中支持未保存模型配置测试、模型列表发现与选择、搜索 API 管理及可持久化的搜索优先级策略。

**Architecture:** 复用现有 OpenAI 兼容模型资源和搜索供应商存储。新增的未保存模型操作只在请求生命周期内创建受限客户端，不写入数据库或密钥目录；搜索策略保存为主体级配置，并由研究路由读取。管理台继续使用现有原生 HTML/JavaScript 和管理员认证。

**Tech Stack:** Python 3.12、`httpx`、Pydantic、SQLite、现有 `http.server` 风格服务、原生 HTML/CSS/JavaScript、pytest。

**Spec:** `docs/superpowers/specs/2026-09-27-model-search-management-design.md`

## Global Constraints

- API 密钥只能通过认证请求提交，不进入浏览器持久存储、数据库明文、日志或响应。
- 所有模型和搜索外部请求继续使用 HTTPS 地址校验、超时、响应大小限制和公网边界。
- 未保存模型测试和模型发现不得创建模型资源或持久化密钥。
- 搜索策略只能是 `model_first`、`api_first` 或 `auto`，无可用资源时必须保留现有降级逻辑并记录原因。
- 管理台可见文案使用中文，保留手动输入模型名称能力。

---

### Task 1: 未保存模型探测与模型发现

**Files:**
- Modify: `src/noyra/model/openai_compatible.py`
- Modify: `src/noyra/service.py`
- Test: `tests/test_service.py`
- Test: `tests/test_model_gateway.py`

**Interfaces:**
- `OpenAICompatibleProvider.probe_unstored(...)` 接收基础地址、模型、密钥并返回脱敏状态与耗时。
- `OpenAICompatibleProvider.list_models_unstored(...)` 接收基础地址、密钥并返回模型标识列表。
- 服务新增 `POST /api/config/model-resources/test` 和 `POST /api/config/model-resources/models`。

- [x] **Step 1: 写失败测试**：认证请求携带未保存表单字段时成功测试；错误密钥返回稳定错误；`GET /models` 返回去重模型标识；供应商不支持时返回 `model_discovery_unsupported`；断言数据库和密钥目录不新增记录。
- [x] **Step 2: 运行测试确认失败**：`pytest tests/test_service.py -k "unstored_model or model_discovery" -q`，预期因路由或方法不存在失败。
- [x] **Step 3: 实现受限 HTTP 方法**：复用 URL 校验、响应上限和客户端超时；聊天探测只发送最小请求；模型发现只解析 OpenAI `data[].id`，不返回原始响应。
- [x] **Step 4: 实现服务路由和稳定错误映射**：认证、JSON 校验、字段校验、审计事件；异常只映射为稳定错误代码。
- [x] **Step 5: 运行聚焦测试**：同一命令应全部通过。
- [x] **Step 6: 提交**：`git add src/noyra/model/openai_compatible.py src/noyra/service.py tests/test_service.py tests/test_model_gateway.py && git commit -m "feat: add unstored model probes and discovery"`

### Task 2: 模型管理台交互

**Files:**
- Modify: `src/noyra/web/admin.html`
- Modify: `src/noyra/web/admin.js`
- Modify: `tests/test_web_contract.py`

**Interfaces:**
- 表单按钮调用 `/api/config/model-resources/test` 与 `/api/config/model-resources/models`。
- 模型发现成功后填充中文下拉框，并允许继续手动编辑模型名称。

- [x] **Step 1: 写失败契约测试**：检查模型表单包含“测试当前配置”“获取模型列表”、模型选择框和中文提示。
- [x] **Step 2: 运行契约测试确认失败**：`pytest tests/test_web_contract.py -q`。
- [x] **Step 3: 修改 HTML**：增加两个按钮、模型选择区域、状态提示和“不保存即可测试”说明。
- [x] **Step 4: 修改 JS**：提交当前字段但不保存；发现成功后按 `id` 选择并写入模型输入；密钥只存在内存字段，不写 `localStorage`；显示稳定中文错误。
- [x] **Step 5: 运行契约测试和 JavaScript 语法检查**：`pytest tests/test_web_contract.py -q` 与 `node --check src/noyra/web/admin.js`。
- [x] **Step 6: 提交**：`git add src/noyra/web/admin.html src/noyra/web/admin.js tests/test_web_contract.py && git commit -m "feat: improve model configuration management UI"`

### Task 3: 搜索供应商管理 API 与管理台

**Files:**
- Modify: `src/noyra/service.py`
- Modify: `src/noyra/web/admin.html`
- Modify: `src/noyra/web/admin.js`
- Test: `tests/test_service.py`
- Test: `tests/test_web_contract.py`

**Interfaces:**
- 复用 `GET/POST /api/config/search-providers` 和撤销路由。
- 管理台增加搜索配置表单，支持 Brave、Bing、Tavily、Serper、测试、停用/启用和撤销显示。

- [x] **Step 1: 写失败测试**：搜索供应商列表脱敏、创建和撤销仍保持稳定；测试接口只验证当前表单密钥而不保存。
- [x] **Step 2: 运行确认失败**：`pytest tests/test_service.py -k "search_provider" -q`。
- [x] **Step 3: 实现搜索供应商测试路由**：复用 provider store 的 URL/密钥边界，响应只返回供应商状态和耗时。
- [x] **Step 4: 增加中文管理台搜索页面**：表单、列表、测试按钮、状态标签和错误提示。
- [x] **Step 5: 运行相关测试和语法检查**。
- [x] **Step 6: 提交**：`git add src/noyra/service.py src/noyra/web/admin.html src/noyra/web/admin.js tests/test_service.py tests/test_web_contract.py && git commit -m "feat: add search provider management"`

### Task 4: 搜索优先级策略持久化与研究路由

**Files:**
- Modify: `src/noyra/core/database.py`
- Create or modify: `src/noyra/research/routing.py`
- Modify: `src/noyra/service.py`
- Modify: `src/noyra/cognition/research.py`
- Modify: `src/noyra/web/admin.html`
- Modify: `src/noyra/web/admin.js`
- Test: `tests/test_research.py`
- Test: `tests/test_service.py`

**Interfaces:**
- 新增主体级 `search_routing_settings` 记录，策略字段为 `model_first`、`api_first`、`auto`。
- `GET/POST /api/config/search-routing` 返回或更新当前策略。
- 研究选择器接收策略并输出最终方法及降级原因，继续写入现有审计记录。

- [x] **Step 1: 写失败测试**：三种策略分别选择预期方法；没有 API 或模型能力时按允许方法降级并记录原因；未认证更新返回 401；数据库重启后策略保持。
- [x] **Step 2: 运行确认失败**：`pytest tests/test_research.py tests/test_service.py -k "search_routing or search_method" -q`。
- [x] **Step 3: 增加数据库表、完整性哈希和主体边界校验**。
- [x] **Step 4: 实现服务读写接口和管理员审计**。
- [x] **Step 5: 接入研究路由，保持 browser/wait 等现有方法和降级规则**。
- [x] **Step 6: 在管理台增加“搜索方式”单选/下拉：优先模型搜索、优先搜索 API、自动选择，并显示当前配置**。
- [x] **Step 7: 运行聚焦测试并提交**：`git add ... && git commit -m "feat: configure research search routing"`。

### Task 5: 全量验证与交付检查

**Files:**
- Modify only files required by failing checks.

- [x] **Step 1: 运行受影响测试**：模型、服务、研究和 Web 契约测试全部通过。
- [ ] **Step 2: 运行格式、静态和语法检查**：`ruff check src tests`、`ruff format --check src tests`、`node --check src/noyra/web/admin.js`。
- [x] **Step 3: 运行完整测试**：`pytest -q`，记录通过数和任何环境相关跳过。
- [ ] **Step 4: 检查 `git diff --check`、敏感信息扫描和工作区状态。
- [ ] **Step 5: 提交最终修复**，仅在新鲜验证通过后报告结果。
