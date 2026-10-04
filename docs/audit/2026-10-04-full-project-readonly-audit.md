# Noyra 全项目只读审计报告（2026-10-04）

## 1. 结论

本次审计检查工作区分支 `codex/wake-after-clean-restart`，HEAD 为 `cc60841`，当前数据库 schema 版本为 79。确认有 5 项需要跟进：schema 79 的运行时导出所有权图缺失，导致当前 schema 的数据导出不可用；严格类型检查失败；高频运行记录没有可执行的保留期限；迁移目标 HTTPS 地址未与目标身份或连接地址绑定；真实主机和发布门禁证据在本地不可核实。

最直接的功能阻断是运行时导出：全量 pytest 有 29 项失败，失败均落在运行时导出路径，错误为“schema 79 没有 ownership graph”；其余测试结果为 1,780 passed、25 skipped、259 subtests passed。没有确认 P0 问题。迁移模块当前已具有加密传输、目标恢复、目标激活、绑定证明和 admission drain 的代码路径；这些代码层进展不能替代双主机、LUKS、systemd、KMS 和链上故障的真实验收。

本报告只写审计文档，没有修改业务代码、测试、部署配置、生产数据或远程仓库。工作区已有未跟踪的 Pelican 页面、测试、计划和 `output/`，均保留且不作为 `cc60841` 已提交内容。全量 pytest 在该工作区运行；严格 mypy 的本地结果还包括未跟踪测试产生的错误，报告中将其与已跟踪代码错误分开说明。

## 1.1 修复进度（2026-10-04）

本轮已按独立模块提交以下代码修复：`2d8f6a9` 为 schema 79 增加显式 runtime export ownership graph；`884624e` 修复已跟踪迁移代码/测试的 strict mypy 问题；`f6cee70` 将迁移目标 enrollment 绑定到规范化 HTTPS origin，并在连接前解析地址、默认阻断 loopback、link-local、保留/多播/未指定及云元数据地址，私有网络仅可通过显式 allowlist 放行；`67de724` 在 retention 批次中对冷 terminal model payload 做有界压缩，保留 model ledger 行、哈希、预算和恢复所需证据。

修复后的本地证据：schema/export 聚焦测试通过；迁移聚焦测试通过；retention/storage/model/integrity 回归通过（195 passed、172 subtests passed）；`mypy src tests --exclude tests/test_pelican_bicycle_page.py` 通过（342 files）；Ruff 与 compileall 通过。完整工作区的 mypy 仍会报告用户未跟踪 Pelican 测试缺少返回类型，该文件未被修改。

N01-N04 的代码层问题已分别处理，但 N05 的真实 release gates 仍未关闭；真实 Ubuntu/systemd、LUKS、双机迁移、KMS、链、代理与 soak 仍必须在对应环境产生同一 SHA 的外部证据。

## 2. 范围、基线与方法

- **时间**：2026-10-04，Asia/Shanghai。
- **基线**：`codex/wake-after-clean-restart`，`cc60841`；`src/noyra/core/database.py` 的 `CURRENT_SCHEMA_VERSION = 79`。
- **审计范围**：`src/noyra` 的运行时、HTTP/管理台、身份和导出、完整性与加密存储、保留策略、provider/search、钱包与自动付款、迁移控制面/agent/target activation、升级协调；`scripts/`、`deploy/systemd/`、`.github/workflows/`、相关测试及既有审计/发布门禁文档。
- **方法**：静态阅读源码、安装器、systemd、CI/release 工作流、测试和最近迁移提交；执行全量 pytest、Ruff、compileall 和 mypy。
- **未执行**：没有连接 GitHub Actions、Cloudflare、生产服务器、真实独立 signer/KMS、真实链、或两台隔离主机；没有做公网渗透、真实 LUKS/恢复、断电/分区和 24/72 小时 soak。

## 3. 评级口径

| 等级 | 风险含义 |
|---|---|
| P0 | 无门槛远程执行、大规模机密泄漏、不可逆主体损坏或重大未授权资金损失。 |
| P1 | 关键服务/数据恢复能力不可用，或资金、主体完整性、单活所有权边界可能失效。 |
| P2 | 容量、配置、状态一致性、CI 质量门或需人工恢复的可靠性问题。 |
| P3 | 文档、运维可观测性或低影响兼容性问题。 |

修复风险表示实施修复本身可能引入的风险：低为局部注解/配置校验；中为跨模块配置或持久化合同；高为导出隔离图、跨主机恢复、钱包/密钥及所有权状态机。概率是基于代码路径和常见部署的工程估计，不是生产遥测统计。除注明条件概率外，低/中/高分别表示小于 5%、5% 至 50%、大于 50% 的估计区间；缺少部署数据时明确写“未知”。

## 4. 问题总表

| ID | 发现 | 风险 | 修复风险 | 触发概率 | 建议 |
|---|---|---:|---:|---|---|
| N01 | Schema 79 没有运行时导出 ownership graph，当前版本导出全部失败 | P1 | 高 | 请求导出时 100% | 立即修复，先阻断发布 |
| N02 | CI 使用的严格 mypy 检查当前失败 | P2 | 低 | 每次运行 mypy 100% | 立即修复并纳入提交前门禁 |
| N03 | 高频运行/调用记录没有按周期归档或清理 | P2 | 中至高 | 持续使用数月后高，估计大于 50% | 长期试运行前完成容量与保留合同 |
| N04 | 迁移 target endpoint 只校验 HTTPS 格式，没有验证 endpoint 归属或约束解析地址 | P2 | 中 | 仅在目标配置错误、DNS/托管被接管时；现实概率未知、估计低 | 开放无人值守迁移前修复 |
| N05 | 当前 SHA 的真实部署与发布门禁 evidence 未从本地验证 | P1 发布门禁 | 高 | 若证据缺失，发布时 100% 被 workflow 阻断 | 生产发布/开放高风险功能前完成真实验收 |

## 5. 详细发现

### N01：Schema 79 的运行时导出图未更新

- **风险等级**：P1，数据导出和可迁移性阻断。
- **修复风险等级**：高。ownership graph 决定跨主体数据的筛选边界；简单复制上一版本图而不审阅新增表，可能导致漏导或跨主体数据泄漏。
- **根因与证据**：数据库 schema 已是 79（`src/noyra/core/database.py:291`），但 `src/noyra/core/runtime_export.py` 的 `_OWNERSHIP_GRAPHS` 最高只有 78（约第 525–571 行）。`_ownership_graph()` 对没有登记的版本直接抛出 `RuntimeError`（约第 879–884 行）。迁移相关提交增加了 schema 79 的 recipient key 字段/持久化合同，却没有为版本 79 添加导出所有权图。
- **影响**：当前 schema 数据库调用 `RuntimeLogExporter.export()` / `export_to_path()` 会立即失败。完整导出、管理台导出任务和依赖运行时导出的恢复/数据携带工作流无法完成；迁移使用的独立 SQLite artifact provider 不经过这个 exporter，因此本审计不把它说成迁移执行链的直接阻断。这不是某张表漏数据，而是整份运行时导出被 fail closed。全量测试 29 个失败都沿着此路径，测试输出代表错误为 `runtime export ownership graph is unavailable for schema 79`。
- **触发条件**：数据库迁移到 schema 79 后发起任何 runtime export。
- **触发概率**：条件满足后 100%。所有由当前代码初始化的数据库都采用 schema 79。
- **建议**：立即添加 schema 79 的显式 ownership graph，逐表确定 subject/parent/global/derived 归属，新增迁移表也必须有单主体导出测试、跨主体引用拒绝测试和未登记表拒绝测试；之后重跑全量 pytest 和 schema 导出测试。完成前不要把导出/恢复视为可用，也不要发布当前版本。

### N02：严格类型检查使 CI 门禁失败

- **风险等级**：P2，工程质量门失效；本身没有确认运行时崩溃。
- **修复风险等级**：低。
- **根因与证据**：`.github/workflows/ci.yml` 执行 `python -m mypy src tests`（第 40 行）。当前本地执行报告 12 项错误：1 项在源码 `src/noyra/migration/targets.py:392`（mypy 无法解析 `base64.binascii` 属性），10 项在已跟踪的迁移测试注解/类型兼容（`test_migration_agent.py`、`test_migration_binding_proofs.py`、`test_migration_activation.py`），另 1 项来自未跟踪的 `tests/test_pelican_bicycle_page.py`。因此干净 CI checkout 至少仍会因源码及已跟踪测试中的错误失败；本地第 12 项不属于 HEAD。
- **影响**：CI 的 mypy job 确定失败，无法形成完整绿灯；开发者可能忽略类型门禁，或为了让 CI 通过而错误关闭 strict 检查。
- **触发条件**：CI 或本地执行 `mypy src tests`。
- **触发概率**：当前代码条件下 100%。
- **建议**：立即修复源码异常类型引用并为迁移测试补齐严格类型；保持 CI 检查强度不变。不要把未跟踪的 Pelican 测试算作已提交问题，也不要在没有审阅前改写/删除用户未跟踪文件。

### N03：运行时记录增长没有受统一保留周期约束

- **风险等级**：P2，容量与恢复时间风险。
- **修复风险等级**：中至高。多类记录有外键、审计和导出依赖，不能通过全表删除处理。
- **根因与证据**：当前保留注册表 `src/noyra/core/retention.py:104–163` 对 provider health、provider attempts、search usage 和 retention run history 配置了删除周期；同一注册表将 `model_calls`、`model_attempts`、`research_search_runs`、`action_deliberation_runs` 和 `behavior_logs` 标记为 `preserve`，没有 cutoff。`ModelLedger.prepare_call()` / `authorize_attempt()` 会对每次模型调用和尝试写行（`src/noyra/model/ledger.py:91`、`213`）。服务定期调用 retention（`src/noyra/service.py:9803–9818`），但这些保留表不会被该任务压缩或清理。
- **影响**：长期运行时，调用/尝试、研究和行为历史持续累积，增大 SQLite、备份、完整性扫描、导出时长和恢复窗口。当前保留“必要审计”没有逐表说明哪些记录必须永久保存、哪些可聚合或冷归档；这也与项目此前减少逐次 API 调用记录和控制存储增长的目标不一致。
- **触发条件**：启用模型、搜索、研究或行为记录，并持续运行数月。
- **触发概率**：正常持续使用下高（估计大于 50% 会造成可观测增长）；实际耗尽磁盘的时间未知，取决于调用速率和存储配额。
- **建议**：在长期试运行前补齐逐表数据生命周期合同：区分账务/故障恢复必须保留的最小证据、可聚合指标、可压缩历史与可删除临时数据；对有外键的记录先定义冷归档/父子联删或摘要替代，并用加速 soak 验证磁盘、WAL、备份和完整性扫描水位。保持必要付款、安全和迁移审计不可变，不要直接删除所有模型记录。

### N04：迁移目标 endpoint 未绑定到网络目的地身份

- **风险等级**：P2，迁移目标配置与目标身份脱节。
- **修复风险等级**：中。需要 endpoint enrollment/attestation 合同、DNS/IP 处理和部署兼容方案。
- **根因与证据**：`TargetRegistry.register()` 检查 HTTPS scheme、hostname、用户名/密码、path/query/fragment（`src/noyra/migration/targets.py:75–84`），但没有校验解析出的地址是否为 loopback/private/link-local/metadata 地址，也没有将 HTTPS origin 放入目标签名 challenge。`HTTPMigrationExecutor._target()` 只重复 URL 格式验证（`src/noyra/migration/http_executor.py:882–895`），随后 `UrllibHTTPTransport.request()` 会连接该 URL（约第 152–190 行）；重定向已禁用，TLS 仍由标准证书验证。请求 Authorization header 携带的是用 per-target secret 计算的 HMAC 签名，不是原始 token。
- **影响**：管理员误登记、已信任域名被接管或 DNS/托管控制失陷时，source 可能向未经目标身份绑定的 HTTPS 主机发出 HMAC 认证请求并暴露迁移元数据；攻击者不能从该签名直接取得原始 token。recipient 加密使其不能解密迁移 bundle，目标签名校验也阻止其伪造最终恢复/激活 receipt；主要剩余影响是针对源端内网的 HTTPS SSRF/探测、请求元数据暴露和迁移拒绝服务。
- **触发条件**：恶意/错误 target URL 被登记，或受信域名解析/托管在迁移后被攻击者控制；攻击者须控制可通过有效 TLS 证书访问的目的地。
- **触发概率**：低，但无法用本地测试估算。需要 operator 配置错误或 endpoint/DNS/证书托管边界失陷，不是匿名用户可直接触发的公开 SSRF。
- **建议**：在开放自动或无人值守迁移前修复：将 target origin 纳入签名 enrollment，明确支持的公网/私网范围；禁止默认访问 loopback、link-local、云 metadata 与未批准的私网段，对需要内网部署的地址采用显式 allowlist；解析/重连策略需防 DNS rebinding；保留 HMAC secret 不出机的合同，并补充 endpoint 变更、内网地址和证书错误测试。

### N05：真实部署/发布证据状态未知

- **风险等级**：P1 发布门禁，不是已证实的代码漏洞。
- **修复风险等级**：高，须真实环境、受保护 workflow 和独立 reviewer。
- **根因与证据**：仓库要求当前 release SHA 具有真实 `ubuntu_systemd`、`encrypted_volume`、`backup_restore`、`migration_fence`、`signer_kms`、`reorg_nonce`、`https_proxy` 和 `soak` evidence（`docs/release/external-gates.md`）。release workflow 会查询同 SHA 的成功 external-gates run 并验证签名、时间窗和所有 gate（`.github/workflows/release.yml`）；本次只读本地审计没有访问 GitHub Actions、生产服务器或真实设备，无法验证 `cc60841` 或后续 release SHA 的证据状态。
- **影响**：本地 fake/temp 测试不能证明真实 LUKS/systemd 权限、双机单活、独立 KMS、链重组、HTTPS 代理或长期运行正确。若 evidence 缺失，发布流程会按设计失败；若有人绕过，生产高风险能力就缺少所需证据。
- **触发条件**：发布当前 SHA，或准备在生产开启自动付款、无人值守迁移/公网长期运行。
- **触发概率**：远端证据现状未知；若任一必需 gate 缺失/过期，release 失败概率为 100%。
- **建议**：在生产发版或开启高风险功能前，按 release gate runbook 在隔离真实环境完成并独立复核所有项目；证据绑定同一个完整 commit SHA。迁移应保持默认关闭/人工审批，自动付款继续受单笔、日限额与紧急暂停保护，直到对应 gate 通过。

## 6. 已确认存在的有效控制

以下内容与上一轮审计相比已有代码层进展，不应再以旧报告中的原始缺陷状态重复列为“当前未修复”：

- Migration 默认关闭，target 登记具备签名挑战和 recipient X25519 key fingerprint/PoP；bundle 使用 recipient 加密和分块传输。
- target agent 有加密卷检查、preflight/入站配额、分块 hash、restore、health 和 activation/rollback 路径；activation 通过受限 runner 与服务接管合同实现。
- migration receipt 已绑定 recipient key、volume、credential、wallet/signer proof 摘要；local wallet 仍需任务绑定的一次性人工批准。
- source 迁移 cutover 有 admission fence，并会使旧 lease 失效、等待在途操作 drain；UI 不再把缺少 proof 的 cutover 显示成可直接执行。
- artifact digest 使用流式计算，避免一次性把整个大文件读入内存。
- 钱包有明确的余额、Gas、nonce、确认超时、链重组/对账状态；自动付款保留额度和紧急暂停控制。
- 管理认证、会话/CSRF、请求体和线程限制、provider failover/cooldown、聚合健康信息和显式保留注册表均存在实现。

这些事实只说明源码存在对应控制和测试；不代表其在真实主机、反向代理、两台设备或独立 signer 上已通过发布 gate。

## 7. 建议处理顺序

1. **修复 N01**：为 schema 79 安全扩展 runtime export ownership graph；先跑全部 runtime-export、跨主体隔离和历史 schema 测试，再跑全量测试。不能靠删除严格 schema 检查绕过。
2. **修复 N02**：保持 CI strict mypy，修正源码和已跟踪测试的类型问题；确认干净 checkout 的 CI 命令通过。
3. **处理 N03**：先做逐表关系/必要审计梳理，再制定归档、聚合和保留期；容量增长需在长时间试运行前可测量、可告警、可恢复。
4. **处理 N04**：收紧 target 地址和 enrollment 绑定，保持显式允许可部署的内网目标，加入 DNS/endpoint 变更故障测试。
5. **完成 N05**：在具体 release SHA 上执行八项真实门禁并独立签署；缺失证据时继续保持能力关闭。

## 8. 验证结果与限制

- **全量 pytest**：**29 failed, 1780 passed, 25 skipped, 259 subtests passed**，耗时 55 分 23 秒。失败均由 schema 79 缺少 runtime export ownership graph 引起；代表性测试覆盖运行时导出、管理台导出任务、跨主体隔离、历史 schema export 和钱包/研究数据导出。
- `.venv\Scripts\python.exe -m ruff check .`：通过，`All checks passed!`。
- `.venv\Scripts\python.exe -m compileall -q src scripts`：通过。
- `.venv\Scripts\python.exe -m mypy src tests`：失败，12 项错误。已跟踪源码/测试有 11 项；未跟踪 `tests/test_pelican_bicycle_page.py` 另有 1 项。由于 GitHub Actions 在干净 checkout 上也执行该 mypy 命令，已跟踪错误足以使 CI 失败。
- 没有连接或更改远程 GitHub、服务器、Cloudflare、数据库或真实链。当前 SHA 的真实 release evidence 状态未知。

## 9. 审计边界

风险等级、修复风险和触发概率是本地源码审查后的工程判断。对未接入的真实主机、外部 signer/KMS、真实网络链路、真实双机迁移、生产负载和远端 CI 状态，本报告不推断通过或失败。全量测试结果针对当前工作区，其中包含用户已有未跟踪文件；这些文件没有被本报告修改。
