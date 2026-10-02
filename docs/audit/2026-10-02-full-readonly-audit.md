# Noyra 全项目只读审计报告（2026-10-02）

## 1. 审计范围、基线与限制

- **审计日期**：2026-10-02（北京时间）。
- **审计类型**：代码、SQLite schema 和迁移、运行时完整性、HTTP/认证、provider 路由、模型与搜索、钱包、迁移控制面、升级器、部署脚本、CI/CD、公开投影和文档的只读审计。
- **代码基线**：分支 `codex/wake-after-clean-restart`，提交 `abe54be`（`fix: harden migration service and type boundaries`）。
- **数据库基线**：`src/noyra/core/database.py` 的 `CURRENT_SCHEMA_VERSION = 75`。
- **变更边界**：本次只新增本报告；不修改业务代码、测试、配置、数据库、服务器或远程仓库。
- **外部限制**：没有连接真实 Ubuntu/systemd 主机、Cloudflare/Caddy、KMS 或独立 signer、S3、真实 RPC 故障注入、链重组环境、多实例公网环境、GitHub environment reviewer 或 24/72 小时 soak 环境。
- **概率说明**：触发概率是基于默认配置、代码路径和运维场景的工程判断，不是攻击频率、第三方 SLA 或资金损失的统计预测。

本报告区分三类结论：代码和测试已经直接证明的开放问题；必须通过真实环境才能关闭的发布门禁；以及需要产品确认的合同问题。没有把已经有充分证据的控制重复列为缺陷。

## 2. 总体结论

Noyra 已形成较完整的主体运行时、SQLite 持久化、加密存储、完整性 watchdog、管理认证、provider 健康、搜索/模型路由、钱包限额和迁移策略控制面。没有发现默认 loopback 配置下无需认证即可触发转账的路径，也没有发现本轮新增的 P0 级远程代码执行。

当前仍不能把版本标记为“全量质量门禁通过”或“真实自动付款/自动迁移生产版”。最先需要处理的是：

1. 当前 schema 已是 75，但 runtime export ownership graph 没有 75 条目；全量 pytest 已复现运行时导出失败。
2. 迁移已经有策略、目标注册、attestation、审批、加密传输和 epoch 控制，但 `cutover.prepare()`、`cutover.commit()` 与 runner 的 restore/health/fence 仍明确拒绝执行，因此真实迁移尚未完成。
3. 本地 wallet 的“一次性批准”保存在进程内 set，重启后可重复使用；该问题在启用本地钱包迁移时会破坏 one-time 语义。
4. production 仍允许 operator token 和模型组 JSON 从普通环境变量携带秘密；生产 preflight 没有覆盖这些嵌套配置。
5. provider half-open permit 没有贯穿实际调用链，`outcome_unknown` 仍被聚合成普通 failure；自动故障切换的统计和恢复证据不完整。
6. retention 的表清单、cursor 语义和最终 state hash 仍有边界问题；release workflow 要求 `external-gates.json`，但当前 workflow 没有生成或上传步骤。

## 3. 风险等级与修复风险

### 3.1 问题风险

| 等级 | 含义 | 处理要求 |
|---|---|---|
| P0 | 直接远程代码执行、大规模机密泄露、不可逆主体损坏或无门槛高危副作用 | 立即隔离并停止相关能力 |
| P1 | 认证、资金安全、主体完整性或无人值守边界可能被破坏 | 发布前修复并完成故障注入 |
| P2 | 明显的安全、可靠性、容量、成本或运维风险，有绕行方案 | 稳定版前修复 |
| P3 | 统计、文档、产品合同或研究证据缺口，不直接突破安全边界 | 排期处理，不能对外宣称已完成 |

### 3.2 修复风险

- **低**：配置门禁、文档或局部统计，兼容性影响小。
- **中**：涉及 API、路由状态、迁移或并发，需要兼容旧数据和回滚测试。
- **高**：涉及钱包状态机、异步 worker、密钥、完整性或跨文件/数据库原子性，需要故障注入和恢复演练。
- **极高**：跨存储、云归档、发布和所有权模型的统一重构。

## 4. 信任边界和数据流

1. **公开访问面**：匿名读取公开状态、日记、行为、互动和审核通过的帖子；只应输出显式白名单字段。
2. **管理访问面**：Bearer 角色令牌或管理 Session；可修改配置、生命周期、钱包、升级、迁移和导出。
3. **主体运行时**：单主体 SQLite、生命周期/睡眠、认知循环、异步工作和完整性 watchdog。
4. **外部 provider**：模型、embedding、搜索、通讯、S3 归档和链 RPC；超时、断连和“已发送但无响应”都可能产生未知结果。
5. **机密边界**：systemd credentials、秘密文件、钱包 keystore、备份 keyring、operator token 和 provider key。
6. **迁移边界**：源主机、目标 agent、加密 artifact、备份和单活 epoch；必须防止旧 epoch 继续写入。
7. **发布边界**：GitHub Actions、同 SHA gate、wheel、SBOM、Sigstore 和 external gate artifact。

系统同时使用 SQLite 行、文件、进程内状态、异步 worker、外部链状态和远程 provider 状态。跨边界动作需要稳定 operation ID、epoch、lease、幂等键和 reconciliation 证据。

## 5. 当前已确认的有效控制

- production 要求显式 genesis hash、at-rest `required` 和 Secure session cookie；默认 listener 为 loopback，只有显式 `container_internal` 才允许容器内部非 loopback。
- operator token 文件使用 `read_secret_file()`；Bearer 角色有长度、占位符和相互 distinct 校验。
- Admin Session 具备 HttpOnly、SameSite、CSRF、TTL、数量上限和安全响应头；可信代理 CIDR 才能解析转发来源。
- production preflight 会清理父进程遗留的 `NOYRA_*` 环境变量后再加载 env 文件。
- 模型、搜索、embedding、world、browser 和 wallet RPC 大多使用有界 HTTP transport，具备超时、大小和地址限制。
- 公开 projection 对 state、diary、behavior、interactions 和 moderated posts 使用字段白名单；诊断、goals、projects 和 runtime export 需要认证。
- provider health 的 state/bucket 有 hash；projection 已返回 failure rate、平均延迟、p50/p95、窗口和 bucket 数量。
- 钱包已有余额不足、Gas 过高、Nonce 冲突、确认超时、广播未知、链重组和 reconciliation 原因码；自动付款受总开关、单笔上限、日限额和 emergency pause 保护，allowlist 默认关闭符合当前产品要求。
- 迁移默认关闭，启用后默认人工审批；目标需要注册、加密卷、release SHA、Ed25519 challenge 和允许的目标列表；策略更新使用 revision CAS。
- 迁移 artifact 使用有界 chunk 和 ChaCha20-Poly1305，manifest digest 与 chunk AAD 绑定；旧 epoch 可被撤销并审计。
- 升级管理器只把固定 GitHub SHA 写入 root runner 的 request 文件，并使用幂等键、清洁源码检查和状态投影。

## 6. 问题总表

| 编号 | 问题 | 风险 | 修复风险 | 触发概率 | 时机 |
|---|---|---:|---:|---:|---|
| F01 | production operator token 允许 inline fallback | P2 | 低 | 中 | 立即 |
| F02 | 模型组 JSON 的 inline `api_keys` 绕过生产秘密门禁 | P2 | 中 | 中 | 立即 |
| F03 | provider half-open probe token 未贯穿调用链 | P2 | 中 | 低至中 | 近期 |
| F04 | `outcome_unknown` 混入普通 failure/error 统计 | P2 | 中 | 低至中 | 近期 |
| F05 | retention registry 固定声称无未分类表 | P2 | 中高 | 高 | 近期 |
| F06 | retention cursor 未绑定 cutoff/epoch，backfill 可能被跳过 | P2 | 中 | 低至中 | 近期 |
| F07 | retention 持久统计、state hash 和返回值可能不一致 | P3 | 低至中 | 中 | 近期 |
| F08 | additive DDL 没有逐项 feature marker | P3/P2 | 中 | 中 | 下次 schema 变更前 |
| F09 | 公网管理台登录失败限速和 session 是进程内状态 | P2/P3 | 中 | 低至中 | 公网前 |
| F10 | release workflow 缺少 external-gates 注入链路 | P2/P3 | 中 | 高 | 下一次发布前 |
| F11 | 真实付款、KMS、reorg、备份和 soak 尚无外部证据 | P1 发布门禁 | 高 | 未量化 | 自动付款前 |
| F12 | benchmark helper 仍直接 `urlopen()` | P3/P2 条件性 | 中 | 低 | 可暂缓 |
| F13 | runtime export 缺 schema 75 ownership graph，当前测试失败 | P1/P2 | 中 | 高（已发生） | 立即 |
| F14 | local wallet 一次性 approval 只保存在进程内 | P1/P2 条件性 | 中高 | 低至中 | 启用本地迁移前 |
| F15 | 迁移 cutover/restore/health/fence 仍是拒绝占位实现 | P1 能力门禁 | 高 | 高（任何真实迁移） | 发布迁移前 |
| F16 | migration agent 缺少调用方认证和持久容量配额 | P2 条件性 | 中 | 低至中 | 暴露 agent 前 |
| F17 | CI 静态门禁与全量测试当前不能通过 | P1/P2 发布门禁 | 中 | 高（每次 CI） | 立即 |
| F18 | public projection 公开合同仍需版本化业务确认 | P3 | 低至中 | 每次扩展 | 上线前 |

## 7. 逐项发现

### F01：production operator token 仍可从 inline 环境变量回退

- **状态**：已确认开放问题。
- **风险等级**：P2。
- **修复风险等级**：低。
- **根因**：`src/noyra/service.py:1070-1078` 读取 `NOYRA_OPERATOR_TOKEN_FILE` 后使用 `or os.getenv("NOYRA_OPERATOR_TOKEN")`。production profile 没有强制 token 必须来自文件或 systemd credential。
- **影响**：token 可能出现在 EnvironmentFile、进程环境、诊断快照或 shell 历史中；泄露后可调用 operator API。
- **触发条件**：生产部署只设置 `NOYRA_OPERATOR_TOKEN`，或 token file 读取失败而 inline 变量仍存在。
- **触发概率**：中。历史部署步骤和简化配置容易保留 inline 变量。
- **证据**：`src/noyra/service.py:1038-1045,1070-1095`；`scripts/preflight-production.py` 没有 operator token source 检查。
- **当前缓解**：文件读取器检查绝对路径、权限、所有权和单行内容；systemd 可提供 credential 文件。
- **建议**：production 只允许 file/credential；缺失或不可读时 fail closed。inline 仅限 development/test，并在 health 中只显示 source 类型。
- **时机**：立即限制生产。
- **验证方法**：对 file、credential、inline-only、file+inline、缺失 file 五种 fixture 运行 preflight，确认只有前两种通过，且导出/日志不含 token。

### F02：模型组 JSON 中的 inline `api_keys` 绕过生产秘密门禁

- **状态**：已确认开放问题。
- **风险等级**：P2。
- **修复风险等级**：中。
- **根因**：`src/noyra/model/resources.py:3698-3774` 接受 group JSON 的 `api_keys`、`api_key_files` 和 `api_key_credentials`；生产 preflight 只检查 `NOYRA_MODEL_API_KEY` 与 `NOYRA_EMBEDDING_API_KEY`，不解析嵌套 group JSON。
- **影响**：provider key 可进入 `.env`、JSON 配置或普通导出，扩大泄露、轮换和撤销范围。
- **触发条件**：把 key 直接放进 `NOYRA_DEEP_MODEL_GROUPS_JSON` 或同类变量。
- **触发概率**：中。短 JSON 配置是最容易被管理台或部署者采用的形式。
- **证据**：`raw_keys = normalized.pop("api_keys", [])`、`resolved_keys = [*raw_keys]`；`scripts/preflight-production.py:88-129` 只检查两个普通变量。
- **当前缓解**：管理台秘密意图和 file/credential source 已存在，模型对象使用 `SecretStr`。
- **建议**：production 禁止 group JSON 的 `api_keys` 非空，只允许 file/credential；preflight 应解析每个 group 并报告 provider/group 名称。
- **时机**：立即限制 production，兼容迁移随后完成。
- **验证方法**：覆盖嵌套 group、file、credential、混合 source 和 malformed JSON fixture。

### F03：provider half-open probe token 没有贯穿真实调用链

- **状态**：已确认开放问题。
- **风险等级**：P2。
- **修复风险等级**：中。
- **根因**：`ProviderHealthStore.route_available()` 在 `src/noyra/core/provider_health.py:522-570` 只返回 `bool`，但内部生成 `probe_token`；`record_attempt()` 接收可选 token。模型和搜索调用方没有取得并回传 permit。
- **影响**：旧的 in-flight 请求无法证明自己拥有当前 half-open lease，恢复探测可能延迟、误记或被并发请求干扰。
- **触发条件**：provider cooldown 到期，同时有旧请求、超时或多个 worker 竞争恢复 probe。
- **触发概率**：低至中，长期运行中会发生。
- **证据**：`route_available` 返回类型为 `bool`；`record_attempt(..., probe_token=None)` 的安全分支只能保留 lease；调用方在 `model/resources.py` 与 `research/search.py` 只按布尔值路由。
- **当前缓解**：不匹配 token 的完成不会覆盖新 claim，lease 最终约两分钟过期。
- **建议**：返回不可伪造的 `RoutePermit`，绑定 token、state revision 和 provider identity；只有 permit owner 能结束 probe，过期应有独立统计。
- **时机**：多 provider 自动切换前完成。
- **验证方法**：故障注入两个并发 probe、旧请求晚到、probe 成功/失败、重启和 lease 过期场景。

### F04：`outcome_unknown` 混入普通 failure/error 统计

- **状态**：已确认开放问题。
- **风险等级**：P2；在自动 failover 场景可升级为 P1 运行风险。
- **修复风险等级**：中。
- **根因**：`ProviderHealthStore.record_attempt()` 对所有 `success=False` 执行 `failure_count += 1`，同时把 error code 分类；调用方把发送后断连、read timeout 和部分 5xx 标记为 `outcome_unknown=True`，但没有独立的统计枚举。
- **影响**：failure rate 和 breaker 可能过早升高；运营人员无法区分“确定失败”和“外部可能已执行但响应丢失”，重试/切换可能产生重复计费。
- **触发条件**：provider 已接收请求但连接在响应前断开，或响应超时/超限。
- **触发概率**：低至中；公网 API 长期运行必然会偶发。
- **证据**：`src/noyra/core/provider_health.py:286-295`；`src/noyra/model/openai_compatible.py` 的 `outcome_unknown` 路径。
- **当前缓解**：模型路由对 unknown 会停止普通 failover，并有显式 reconcile/retry 语义。
- **建议**：将 `success/known_failure/unknown` 分开存储；failure rate 只使用 known failure，增加 unknown rate 和 logical request reconcile。
- **时机**：近期稳定版，provider 自动切换前必须完成。
- **验证方法**：对 401、429、5xx、connect timeout、read timeout、schema error 和成功分别核对三个计数、冷却和 projection。

### F05：retention registry 固定声称没有未分类表

- **状态**：已确认开放问题。
- **风险等级**：P2。
- **修复风险等级**：中高。
- **根因**：`retention_registry_diagnostics()` 在 `src/noyra/core/retention.py:190-198` 固定返回 `"unclassified": ()`，没有把 `sqlite_master` 的实际持久表与 `RETENTION_REGISTRY` 做差集。
- **影响**：未来新增或历史未登记的事件、支付尝试、snapshot、CAPTCHA/rate event、incident、revision 和 queue 表可能无限增长，造成 SQLite/WAL、备份、导出和 integrity 扫描膨胀。
- **触发条件**：启用认知、公开投稿、provider、钱包或迁移并运行数周至数月；新增表时遗漏 registry。
- **触发概率**：高。
- **证据**：`src/noyra/core/retention.py:103-179` 的静态 registry 与 `:195` 固定空 tuple；`database.py` 定义了大量持久表。
- **当前缓解**：部分表有 quota、batch size 和存储 pressure 门禁。
- **建议**：启动/CI 动态核对全部表；未分类表应使 production preflight 失败或明确进入 preserve 区；每类声明 cutoff、压缩、归档和 WAL checkpoint 策略。
- **时机**：近期；公开投稿和长期运行前完成。
- **验证方法**：生成 sqlite inventory 差集，用加速时钟验证 30/90/180 日增长、磁盘、WAL、导出和 watchdog 耗时。

### F06：retention cursor 未绑定 cutoff、registry version 和数据 epoch

- **状态**：已确认设计风险。
- **风险等级**：P2/P3。
- **修复风险等级**：中。
- **根因**：`run_batch()` 在 `src/noyra/core/retention.py:385-403` 只校验 cursor 结构便从上一 run 复制 keyset cursor；`_delete_table()` 使用当前 cutoff 加旧 cursor 的 `>` 条件，没有比较 cutoff、registry 版本或 backfill epoch。
- **影响**：时钟回拨、修改保留周期、恢复备份或导入更早时间戳记录后，旧 cursor 可能跳过仍应删除的记录。
- **触发条件**：改变 retention 配置、历史 backfill、数据库恢复或排序键变化。
- **触发概率**：低至中；普通单调时钟下较少，恢复场景会上升。
- **证据**：`src/noyra/core/retention.py:385-403,527-642`。
- **当前缓解**：cursor 有 JSON 结构和排序键检查，删除在事务内执行。
- **建议**：cursor 绑定 cutoff、registry version、sort-key version 和 data epoch；不匹配时从头扫描并标记 reset。
- **时机**：备份恢复和 retention 可靠性验收前完成。
- **验证方法**：先运行批次，再插入旧时间记录、改变 cutoff、恢复备份并重启，确认全部旧记录最终处理。

### F07：retention run 持久统计、state hash 和返回值可能不一致

- **状态**：已确认开放问题。
- **风险等级**：P3。
- **修复风险等级**：低至中。
- **根因**：`run_batch()` 在 `src/noyra/core/retention.py:434-471` 先 INSERT 当前 run，再删除超出 `run_history` 的旧 run，之后只修改内存中的 `deleted["retention_runs"]` 和 cursor。
- **影响**：API 返回值、数据库 `deleted_by_table_json`、state hash 和 integrity projection 可能展示不同的删除计数。
- **触发条件**：retention history 超过 `run_history` 并执行 prune。
- **触发概率**：中；长期运行后稳定触发。
- **证据**：上述插入、prune 和后续内存修改顺序。
- **当前缓解**：删除和历史清理在同一事务，核心数据不会部分提交。
- **建议**：在最终 payload 确定后再写入当前 run，或在同一事务内更新最终 hash；返回值直接从最终持久行构造。
- **时机**：近期稳定版。
- **验证方法**：`run_history=1` 连续执行多次，比较返回值、数据库行、hash 和 integrity 结果。

### F08：additive DDL、索引和触发器没有逐项 feature marker

- **状态**：恢复和离线证明缺口。
- **风险等级**：P3，涉及恢复时可升级为 P2。
- **修复风险等级**：中。
- **根因**：`persistent_features` 已覆盖部分 secret cleanup/intents，但 `Database._ensure_optional_features()` 及后续 additive `CREATE TABLE/INDEX/TRIGGER/ALTER` 仍有结构不对应独立 feature version 或 fingerprint。
- **影响**：`schema_meta` 显示 75 时，sqlite_master 仍可能缺少某个运行时组件需要的索引/触发器；离线复制、部分初始化和恢复工具无法证明结构合同完整。
- **触发条件**：旧备份恢复、升级中断、只启动部分组件、手工复制数据库或第三方工具只看 schema marker。
- **触发概率**：中。
- **证据**：`src/noyra/core/database.py:7375-7585` 与 `CURRENT_SCHEMA_VERSION=75`；optional DDL 与 schema marker 分散。
- **当前缓解**：正式 migration 有版本、备份和 quick check，多数 DDL 幂等。
- **建议**：把持久结构全部放入正式 migration，或为每项记录 owner、feature version、DDL fingerprint 和 restore preflight；禁止构造器静默扩展 schema。
- **时机**：下次 schema 变更前完成。
- **验证方法**：空库、旧库、中断升级、只初始化核心组件和完整组件分别启动，比较 sqlite_master、marker、manifest 和恢复结果。

### F09：公网管理台登录失败限速和 session 是进程内状态

- **状态**：已确认架构限制。
- **风险等级**：P2/P3。
- **修复风险等级**：中。
- **根因**：`Service` 用 `_admin_login_failures` 和 `_admin_sessions` 的进程内字典保存失败桶和 session；重启、多 worker 或多副本不共享。
- **影响**：攻击者可在重启窗口或不同副本获得新的失败预算；负载均衡时整体尝试次数超过单进程配置，session 也会出现不一致。
- **触发条件**：管理台公网开放并使用滚动升级、多 worker 或多实例代理。
- **触发概率**：低至中；单进程 loopback 场景较低。
- **证据**：`src/noyra/service.py` 的 `_admin_login_failures`、`_admin_sessions` 和 `_rate_lock` 初始化及读写。
- **当前缓解**：有失败预算、请求限速、TTL、CSRF、Secure cookie 和安全响应头。
- **建议**：公网 profile 使用共享 SQLite/Redis TTL bucket 或边缘 WAF；按可信 proxy 解析后的 client identity 计数；把滚动重启和多 worker 纳入 preflight。
- **时机**：管理台正式公网开放前；内网单实例可暂缓。
- **验证方法**：多 worker、滚动重启、可信代理链和伪造 `X-Forwarded-For` 测试整体预算。

### F10：release workflow 缺少 external-gates 注入/上传链路

- **状态**：已确认发布流程缺口。
- **风险等级**：P2/P3。
- **修复风险等级**：中。
- **根因**：`.github/workflows/release.yml:62-98` 下载 wallet evidence 后强制要求同 SHA 目录中的 `external-gates.json` 和公钥；quality-gate 只上传 wallet evidence，没有生成或上传 external-gates 的步骤。
- **影响**：按现有自动 tag 路径发布会因缺文件失败；临时手工注入则可能绕过 reviewer、签名、freshness 和 gate ID 合同。
- **触发条件**：推送 `v*` tag，且没有额外受保护 artifact 注入。
- **触发概率**：高。
- **证据**：release workflow 的 upload/download 以及 `external = root / "external-gates.json"`；`scripts/verify_external_gates.py` 只有消费和验证逻辑。
- **当前缓解**：验证器检查 commit SHA、schema、freshness、固定 gate IDs 和 Ed25519 signature。
- **建议**：定义独立受保护 workflow/environment 产生不可变 artifact；release job 只下载和验证，不在同一 job 生成“通过”记录；错误应区分缺失、过期、错 SHA 和签名错误。
- **时机**：下一次真实发布前必须完成。
- **验证方法**：正常 artifact、缺文件、错 SHA、过期、错误 reviewer、错误签名和缺 gate ID 的 tag 演练。

### F11：真实自动付款、KMS、链重组、备份恢复和 soak 尚无外部证据

- **状态**：开放发布门禁，不是本地代码单测已证明的漏洞。
- **风险等级**：P1（开启自动付款时）。
- **修复风险等级**：高。
- **根因**：本地 fake signer/RPC 只能覆盖确定性状态机；当前没有同 SHA 的独立 signer/KMS、广播后断连、nonce 冲突、receipt 延迟/消失、reorg、重启恢复、备份恢复和多日增长 artifact。
- **影响**：可能出现重复广播、订单长期悬挂、账本与链上事实不一致、恢复失败或磁盘压力下写入阻断。
- **触发条件**：开启自动付款后发生余额不足、Gas 突升、外部 nonce 占用、响应丢失、receipt 变化、signer/KMS 不可用、重启或磁盘接近上限。
- **触发概率**：未量化；公网 RPC 和长期无人值守环境不可忽略。
- **证据**：钱包代码虽有 `mark_reconcile_required()` 和原因码，但没有本环境的真实 gate artifact；release workflow 将 testnet/KMS/backup/soak 作为 external gate 输入。
- **当前缓解**：默认自动付款关闭；有单笔/日限额、emergency pause、admission lease、logical idempotency、receipt recheck 和 reconciliation。
- **建议**：保持自动付款关闭，直到 external-gates 完整、同 SHA、独立复核；unknown 不得用新 logical payment ID 自动重发。
- **时机**：自动付款或主网前必须完成。
- **验证方法**：逐个原因码核对 order、execution、ledger、incident、daily counter、pause、receipt 和最终余额，并保存可离线验证 artifact。

### F12：benchmark helper 仍直接使用 `urllib.request.urlopen()`

- **状态**：条件性设计风险。
- **风险等级**：P3；若 URL 未来来自管理台、公开 API 或模型输出，可升级 P2。
- **修复风险等级**：中。
- **根因**：`src/noyra/mind/benchmarks.py:19-20,129-133` 直接调用 `urlopen()`，未使用项目统一的 public DNS、private-IP、no-redirect 和 pinned-connect transport。
- **影响**：未来扩大调用面后可能重新引入 DNS rebinding、私网探测、重定向和无界网络响应。
- **触发条件**：benchmark URL 变成管理员可配、公开输入或自动研究计划输入。
- **触发概率**：低，取决于未来产品演进。
- **证据**：上述导入和 `urlopen` 调用；其他 provider 路径已使用统一 transport。
- **当前缓解**：当前未发现它直接暴露为公网任意 URL 代理；使用 HTTPS、大小限制和 SHA-256 pin。
- **建议**：扩大调用面前迁移统一 transport，或只允许固定 allowlist 资源并加入 SSRF/DNS 回归测试。
- **时机**：当前可暂缓，但应作为 capability gate 登记。
- **验证方法**：loopback、私网、IPv6、重定向、超大响应和 DNS 变化 fixture。

### F13：runtime export 缺少 schema 75 ownership graph，当前测试失败

- **状态**：已复现的阻断问题。
- **风险等级**：P1/P2。
- **修复风险等级**：中。
- **根因**：`src/noyra/core/database.py:234` 的当前 schema 是 75；`src/noyra/core/runtime_export.py:476-504` 的 `_OWNERSHIP_GRAPHS` 没有 75 条目；`_ownership_graph()` 在 `:825-829` 对缺失版本直接抛 `RuntimeError`。
- **影响**：runtime export 及共享 secret redaction gate 不能运行；管理导出、审计导出和 CI 质量门禁可能直接失败。
- **触发条件**：对当前 schema 75 数据库调用 `RuntimeLogExporter.export()` 或运行 gate1 redaction 测试。
- **触发概率**：高，当前已发生。
- **证据**：直接运行 `tests/test_gate1_redaction.py::test_runtime_export_and_runtime_log_projection_share_secret_redaction` 得到 `RuntimeError: runtime export ownership graph is unavailable for schema 75`；全量 pytest 在约 `198 passed, 14 skipped` 后于同一测试失败。
- **当前缓解**：旧 schema graph 和严格缺失即失败策略可防止静默错误导出；但没有 75 graph 时功能不可用。
- **建议**：为 schema 75 生成经过审查的 ownership graph，或明确兼容到最新已验证 graph；补充所有表、optional features 和 secret redaction 的 manifest test。
- **时机**：立即修复并在任何发布前重新运行全量测试。
- **验证方法**：修复后运行 gate1 redaction、runtime export 全套测试、`compileall`、Ruff、format、mypy 和全量 pytest；确认 manifest 的 schema version 与 graph version 一致。

### F14：local wallet 一次性 approval 只保存在进程内

- **状态**：已确认开放问题，当前实际转账路径仍被迁移 cutover 阻断。
- **风险等级**：P1/P2 条件性。
- **修复风险等级**：中高。
- **根因**：`src/noyra/migration/wallet.py:73` 定义 `ClassVar[set[tuple[str,str]]] _used_approval_ids`；`apply_local_transfer()` 在 `:168-171` 只把 approval id 放入内存 set，没有写入数据库或一次性 nonce 表。
- **影响**：进程重启、热升级或多进程部署后同一个 approval id 可再次使用，破坏“第二次批准”和 one-time transfer 语义。若未来 cutover 直接调用该方法，可能导致重复密钥绑定或重复迁移动作。
- **触发条件**：启用 `local_wallet_transfer`，执行一次 approval 后重启服务或启动第二个 worker，再提交同一 approval。
- **触发概率**：低至中；正常单进程不触发，升级/恢复场景会触发。
- **证据**：上述 class variable 和 set membership；`tests/test_migration_wallet.py` 只验证同一进程内重复调用，不验证重启/多进程。
- **当前缓解**：本地钱包迁移默认关闭，需要单独 `local_wallet_transfer_enabled` 和确认；源 key 保留到 commit。
- **建议**：用 subject/task/address/approval digest 的持久 CAS 表或 append-only consume event；消费必须在同一事务中完成，重启和并发均返回 conflict。
- **时机**：启用本地钱包迁移前必须完成。
- **验证方法**：进程重启、多进程并发、数据库恢复和重复 request 测试；确认一次成功后任何重放都拒绝。

### F15：迁移 cutover、restore、health、fence 仍是拒绝占位实现

- **状态**：已确认能力缺口；当前迁移是安全控制平面，不是可执行的完整迁移。
- **风险等级**：P1 能力/发布门禁。
- **修复风险等级**：高。
- **根因**：`src/noyra/migration/cutover.py:31-37` 的 `prepare()` 和 `commit()` 无条件抛出 `verified target restore and health proof is required`；`scripts/noyra-migration-runner.sh:56-67` 对 `restore`、`health`、`fence` 返回 78；HTTP cutover route 也直接返回 `verified_target_restore_and_health_proof_required`。
- **影响**：真实数据传输、目标恢复、健康验证、路由切换和 rollback 前置流程无法闭环；对外宣称“已支持自动迁移”会误导部署者。
- **触发条件**：任何真实迁移任务尝试 prepare/commit，或系统需要从源切换到已登记目标。
- **触发概率**：高；每次真实迁移都会触发。
- **证据**：上述实现和 `tests/test_migration_cutover.py` 对缺 proof 必须拒绝的断言；当前 end-to-end 测试只证明安全拒绝。
- **当前缓解**：默认关闭、人工审批、目标 attestation、加密 artifact、epoch fencing 和显式 rollback 控制已存在，且不会在无 proof 时误切换。
- **建议**：先实现 target restore/health/fence 的端到端证据链：任务绑定 manifest、恢复报告、健康报告、target signature、source epoch 和 CAS；随后再实现 cutover/rollback，禁止以单个字符串 proof 代替验签。
- **时机**：迁移功能发布前必须完成；在此之前管理台应明确显示“控制面已就绪，执行器未启用”。
- **验证方法**：真实 target agent + 加密 backup + restart/partial transfer/cutover failure/rollback/replay/old epoch write 全矩阵，并保存同 SHA artifact。

### F16：migration agent 缺少调用方认证和持久容量配额

- **状态**：条件性安全和可用性风险。
- **风险等级**：P2。
- **修复风险等级**：中。
- **根因**：`scripts/noyra-migration-agent.py` 只绑定 `127.0.0.1`，HTTP `POST /v1/receive|restore|health` 没有 application-level caller authentication、request nonce 或 source authorization；`MigrationAgent.receive()` 把 manifest 写入 `/var/lib/noyra/migration-agent/incoming`，只受单请求 `MAX_BODY=1_000_000` 限制，没有总文件数/总字节配额或过期清理。
- **影响**：能访问该主机 loopback 的其他本地用户、被入侵的本地进程或错误的 SSH reverse tunnel 可伪造接收请求、持续写入 manifest 并耗尽迁移 agent 磁盘；若 agent 被反向代理暴露，风险扩大为远程未授权写入。
- **触发条件**：启用 migration agent、存在多用户主机或将 loopback 端口通过隧道/代理暴露。
- **触发概率**：低至中，取决于部署拓扑；默认未安装 agent 时不触发。
- **证据**：agent service unit 的 loopback listener 和 `POST` handler；`receive()` 的每次写入与目录权限检查；没有 caller token/signature、quota 或 garbage collector。
- **当前缓解**：systemd 使用 `User=noyra`、`PrivateTmp`、`ProtectSystem=strict`、`ReadWritePaths` 和 0700/0600 文件；manifest 禁止 secret 字段并限制单体积。
- **建议**：以 source-target session key 或 signed request 绑定每个 artifact；增加总字节/文件数/TTL quota、过期清理和审计；若需要远程访问，必须在 TLS/认证代理后，不能只靠 loopback。
- **时机**：任何 agent 远程暴露前；仅本机实验可暂缓。
- **验证方法**：非 `noyra` 本地用户、错误签名、重放、并发写满 quota、过期清理和 tunnel 暴露测试。

### F17：CI 静态门禁与全量测试当前不能通过

- **状态**：已复现发布阻断。
- **风险等级**：P1/P2（质量门禁和发布风险）。
- **修复风险等级**：中。
- **根因**：`.github/workflows/ci.yml` 强制执行 pytest、Ruff、format 和 mypy，但当前工作区结果为：targeted 迁移/provider/wallet 测试 `75 passed`；`ruff check .` 有 `scripts/check-site.py` 两个 E501；`ruff format --check .` 报 `docs/superpowers/plans/2026-10-01-deployment-modes.md` 未格式化；`mypy src tests` 报 228 errors/31 files；全量 pytest 在 runtime export schema 75 问题失败。
- **影响**：提交无法获得可信 CI 绿灯；若通过跳过某些 gate 发布，类型错误、格式漂移和运行时导出回归可能进入生产。
- **触发条件**：任何完整 CI 矩阵或本地 release gate。
- **触发概率**：高，每次完整门禁都会触发。
- **证据**：本次执行的四条命令和输出；`.github/workflows/ci.yml:32,38-42`。
- **当前缓解**：针对核心模块的 75 个测试通过，且 CI 明确声明这些工具为门禁。
- **建议**：先修复 F13，再分层清理 production mypy errors 和测试类型错误，修复两个 Ruff E501 与未格式化文档；不要通过降低门禁或忽略错误来恢复绿灯。
- **时机**：立即；在此之前不应把当前提交称为可发布版本。
- **验证方法**：CI 同版本 Python、锁定依赖和全矩阵复跑；结果需保存 commit SHA、平台、artifact 和失败摘要。

### F18：public projection 的公开合同仍需版本化业务确认

- **状态**：产品合同待确认，不应直接定性为泄密漏洞。
- **风险等级**：P3。
- **修复风险等级**：低至中。
- **根因**：代码已有 public state/diary/behavior/interactions/posts 白名单，但公开字段、时间窗口、撤回语义、匿名化和 SEO 缓存策略没有统一的版本化合同。
- **影响**：后续新增字段可能把生命周期状态、行为摘要、互动记录或 provenance 暴露到不符合产品预期的范围；过度收紧又会破坏公开站点。
- **触发条件**：公开站点上线、projection 增加字段、匿名投稿或新互动渠道上线。
- **触发概率**：中至高，取决于迭代频率。
- **证据**：`src/noyra/interaction/projection.py` 的公共 projection 与 service 路由；当前未发现白名单绕过，但缺少单独的版本化 contract artifact。
- **当前缓解**：diagnostics/goals/projects/runtime logs 需要认证；posts 仅返回审核通过内容；HTTP 有响应大小和安全 header。
- **建议**：发布 `public-contract-v1`，固定字段、最大行数/字节数、时间窗口、匿名化、撤回/删除和缓存；schema 变更必须更新快照测试并人工隐私复核。
- **时机**：公开站点正式推广前确认。
- **验证方法**：匿名、read token、撤回、私有 channel、新字段和大数据集响应快照测试。

## 8. 已关闭或降级的问题

本轮复核不再把以下历史项目列为开放缺陷：

- production 显式 genesis hash、at-rest required、Secure cookie 和 loopback/container-internal listener 合同已经存在；仍需真实 HTTPS proxy 验收。
- operator token file 已使用安全读取器；剩余问题是 F01 的 inline fallback。
- preflight 已清理父进程 `NOYRA_*`，不再受 shell 污染。
- 统一 HTTP transport 已覆盖大部分模型、搜索、embedding、world、browser 和 wallet RPC；F12 仅针对 benchmark helper 的条件性风险。
- provider projection 已返回统计窗口；F03/F04 是 permit 和 unknown 语义问题，不是窗口缺失。
- chain reorg 已有 `mark_reconcile_required()`、reconciliation event 和 confirmed 降级路径，不再重复报告“confirmed 永不降级”。仍需 F11 的真实链重组证据。
- storage quota 已改为报告 `effective_subject_bytes`，旧的 freelist 永远 over-quota 结论不再直接成立；仍需压力和恢复演练。
- retention 已把 cognitive route evidence 标为 preserve，不再报告旧版“清理一定删除 append-only route evidence”；F05-F07 是覆盖、cursor 和统计一致性问题。
- migration schema replay hardening、target attestation、policy revision CAS、加密传输和 epoch fencing 已存在；F14-F16 说明 one-time、执行器和 agent perimeter 仍未闭环。
- recipient allowlist 默认关闭、无人工确认、无月限额符合当前产品要求，不是本轮缺陷。

## 9. 立即修复顺序

### 阶段 A：先恢复可验证的质量门禁

1. 为 schema 75 补齐 ownership graph 和 redaction manifest，修复 F13。
2. 修复全量 pytest、Ruff、format；将 production mypy 错误与测试类型错误分层处理，关闭 F17。
3. 不允许通过跳过测试、放宽 `--ignore` 或降低 CI 门禁来取得绿灯。

### 阶段 B：固定生产秘密与资金边界

1. production 禁止 inline operator token（F01）。
2. production 禁止 model group JSON inline key（F02）。
3. 在 F11 external gate 完整前保持自动付款关闭；真实 signer/KMS、reorg、backup/restore 和 soak 作为不可跳过门禁。

### 阶段 C：完成 provider 与数据生命周期

1. RoutePermit 贯穿 half-open probe 和 attempt completion（F03）。
2. 拆分 known failure/unknown（F04）。
3. 动态核对 retention registry，绑定 cursor cutoff/epoch，统一最终 payload/hash/return（F05-F07）。
4. 为 additive DDL 建立 feature marker/fingerprint（F08）。

### 阶段 D：迁移执行器和公网运维

1. 先实现并验收 restore/health/fence/cutover/rollback 端到端（F15）。
2. 把 local wallet approval 改为持久 CAS consume event（F14）。
3. 为 agent 增加 signed session、quota 和 TTL 清理（F16）。
4. 完成 external-gates 受保护 artifact 链路（F10），再考虑管理台公网多实例限速（F09）。

## 10. 真实环境发布门禁

以下证据必须绑定 commit SHA，能够离线验证，并由独立 reviewer 复核：

1. 干净 Ubuntu 主机安装、升级、回滚、LUKS 挂载、systemd credential、权限和 preflight。
2. Caddy/Nginx/Cloudflare HTTPS、Secure cookie、HSTS/CSP、trusted proxy、失败限速和多 worker。
3. provider/model/search/operator secret file/credential 的轮换、撤销、脱敏导出和旧 key 清理。
4. provider 401/403/429/5xx、connect/read timeout、响应超限、unknown、cooldown、half-open 和恢复 probe。
5. 钱包余额不足、Gas 过高、nonce 冲突、广播后断连、确认超时、receipt 消失/变更、reorg、signer 不可用和重启恢复。
6. backup/WAL/schema/feature marker 恢复、密钥轮换、旧版本拒绝、归档副本和完整性扫描。
7. 迁移注册、attestation、加密传输、目标恢复、健康 proof、epoch fencing、cutover、rollback、重复请求和旧 epoch 写入拒绝。
8. 至少 24/72 小时认知循环、provider 聚合、retention、WAL、磁盘增长、导出和 watchdog soak。
9. release tag、production reviewer、external-gates 签名/新鲜度、SBOM、Cosign 和 artifact 可复现性。
10. 公开站点移动端、慢网络、屏幕阅读器、CAPTCHA、响应大小、SEO/社交预览和 public contract snapshot。

## 11. 下一代升级方向

1. **统一持久化 registry**：每张表共享 owner、schema/feature version、DDL fingerprint、integrity check、retention class 和 repair strategy，供 migration/export/backup/watchdog 共用。
2. **统一 logical operation、epoch 和 lease**：模型、搜索、支付、provider attempt、迁移 transfer 使用稳定 operation ID；unknown、replacement、probe 和 reconcile 用 CAS 绑定。
3. **Wallet Safety Kernel**：把付款收敛为 `proposed -> admitted -> signing -> broadcast_unknown/broadcasted -> confirming -> confirmed/failed/reorged/reconcile_required`，禁止 unknown 换新 payment ID 自动重发。
4. **Provider Router 2.0**：统一 model/embedding/search 的 priority、capability、cooldown、half-open permit 和 unknown 状态，只保留必要聚合。
5. **Migration Executor 2.0**：source snapshot、encrypted transfer、target restore、health proof、epoch acquire、proxy switch 和 rollback 每一步都有 durable evidence，未完成步骤不能被 UI 标成完成。
6. **Production configuration contract**：安装器、升级器、systemd、Compose、代理和 CI 复用同一 preflight；管理台同时显示“配置完整”和“安全可运行”。
7. **数据最小化和有限窗口观测**：原始 model I/O、私密心理和网络响应默认不落盘；聚合数据带窗口和清理策略，必要证据进入加密归档。
8. **签名 external gate**：使用版本化 JSON/DSSE，绑定 commit、环境、时间、固定 gate IDs、证据引用、执行人和 reviewer，禁止在 release job 内生成通过记录。
9. **公开产品合同**：公开字段、匿名化、撤回、缓存和移动/无障碍性能纳入版本化 contract tests，不让视觉迭代绕过隐私边界。

## 12. 验证记录和限制

本轮已执行或复核：

- targeted provider/retention/security/upgrade/migration/wallet 测试：`75 passed`。
- `tests/test_gate1_redaction.py::test_runtime_export_and_runtime_log_projection_share_secret_redaction`：失败，错误为 `runtime export ownership graph is unavailable for schema 75`。
- `ruff check .`：失败，`scripts/check-site.py` 有 2 个 E501。
- `ruff format --check .`：失败，`docs/superpowers/plans/2026-10-01-deployment-modes.md` 会被重新格式化。
- `mypy src tests`：失败，当前环境报告 `228 errors in 31 files`，包含 production 文件和大量测试类型错误。
- 全量 pytest 已运行到约 `198 passed, 14 skipped` 后在上述 runtime export 测试失败。
- `git diff --check` 和 `git status --short` 用于确认本报告之外没有工作区修改；本次不修改业务代码、测试、配置、数据库或远程仓库。

未执行真实服务器、KMS/独立 signer、S3、RPC timeout/response-loss、链重组、公网多实例、备份恢复、多日 soak、GitHub environment reviewer 和 Cloudflare 代理验收。因此 P1 发布门禁和“未量化”概率不能被本地测试替代。

## 13. 审计声明

本报告是基于提交 `abe54be` 的代码、配置、文档和本地验证的只读审计。风险等级表达工程后果，触发概率表达相对工程判断，不构成渗透测试、资金安全保证或第三方 SLA 结论。修复任何发现后必须重新运行针对性测试、迁移/恢复演练、故障注入和发布证据验证；在 F13、F15、F17 和 F11 未关闭前，不应把当前版本标记为全量通过、无人值守自动付款生产版或已完成真实自动迁移。

## 14. 修复后复核（2026-10-02）

本节记录审计完成后的代码修复结果；前述章节保留原始审计快照，不把修复后的证据倒填到当时的审计结论中。当前本地分支包含下表所列修复提交，未推送 GitHub，也未修改服务器。

| 编号 | 修复后状态 | 依据 |
|---|---|---|
| F01/F02 | 已关闭（代码与测试） | `5d2db45`；生产 operator、模型组密钥只能来自受保护文件或 systemd credential。 |
| F03/F04 | 已关闭（代码与测试） | `24e71aa`；provider permit、unknown 结果和故障切换状态绑定到持久尝试。 |
| F05/F06/F07 | 已关闭（代码与测试） | `17c57a4`、`95a0b51`、`3a84af5`；retention inventory、cursor epoch/cutoff 和最终 payload/hash 已纳入合同。 |
| F08 | 已关闭（代码与测试） | `1852993`、`92e23f2`、`2a6a96e`；v77 完整 DDL hash 保持兼容，结构 hash 在构造时 fail-closed，trigger-only drift 进入 `core.schema_contract` P0 完整性检查；production 禁止 `integrity_mode=off`。 |
| F09 | 已关闭（代码与测试） | `e1991b0`；管理 session、失败限速和 TTL 状态已持久化。真实多实例/代理仍需外部验收。 |
| F10 | 已关闭（代码与测试） | `f2ae57c`；external-gates 独立签名、同 SHA、受保护环境和稳定缺失错误已接入 release workflow。真实 GitHub environment 仍需验收。 |
| F11 | 外部发布门禁仍开放 | 本地不能证明独立 signer/KMS、真实 RPC 断连/nonce/reorg、备份恢复或 24/72 小时 soak；自动付款必须继续等待绑定 SHA 的 external evidence。 |
| F12 | 已关闭（代码与测试） | `b5a4725`；benchmark 下载复用安全 transport 和地址/重定向边界。 |
| F13 | 已关闭（代码与测试） | `f90dd51`；schema 75 runtime export ownership graph 已补齐并通过 redaction/export 测试。 |
| F14/F15/F16 | 已关闭（代码与测试） | `1a8e890`、`db35d75`、`10a0d50`；local approval durable CAS、target restore/health/fence proof、agent caller authentication、nonce replay protection、quota 和 TTL 已实现。真实跨主机迁移仍需 external evidence。 |
| F17 | 已关闭（本地质量门禁） | `ffb7df0`；mypy、Ruff、format、compileall 和全量 pytest 已通过。 |
| F18 | 已关闭（代码与测试） | `b9cbb43`；public projection 已绑定版本化字段、大小和分页合同。公开站点隐私/无障碍/性能仍需真实浏览器验收。 |

最终本地验证结果：`1725 passed, 24 skipped, 259 subtests passed`；`ruff check .`、`ruff format --check .`、`mypy src tests`、`compileall -q src scripts tests` 和 `git diff --check` 均通过。跳过项仅为当前 Windows 环境不具备的 POSIX 权限、symlink 或 filesystem contract。F11 以及 HTTPS、Cloudflare/Caddy、多实例公网、真实 KMS、链重组、备份恢复和 soak 仍按第 10 节保留为不可由本地单测替代的发布门禁。
