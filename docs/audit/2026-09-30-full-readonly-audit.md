# Noyra 全项目只读审计报告（2026-09-30）

## 1. 审计范围、基线与边界

- **审计日期**：2026-09-30（北京时间）。
- **审计类型**：代码、数据库迁移与持久化、HTTP 与认证、外部 provider、钱包执行、数据保留、部署脚本、CI/CD、公开投影和文档的只读审计。
- **审计基线**：分支 `codex/wake-after-clean-restart`，提交 `f418c7b5a18cc52a70d5aaa82cbed01b3fc856a4`。
- **变更边界**：本次仅新增本报告；不修改业务代码、测试、配置、数据库、服务器状态或远程仓库。
- **检查对象**：`src/noyra`、`tests`、`scripts`、`deploy`、`.github/workflows`、`docs` 和项目元数据。
- **外部边界**：没有连接真实生产服务器、真实 GitHub 仓库设置、KMS、S3、主网、外部 signer、真实 RPC 故障注入或多日 soak 环境。
- **概率说明**：下文“触发概率”是基于默认配置、代码路径和运维场景的工程相对判断，不是攻击频率、统计学频率或 SLA 预测。

本报告把问题分成三类：已经由当前代码确认的开放问题；尚未被本地证据证明、但必须作为发布门禁的验证缺口；以及需要产品方确认的合同问题。已经修复或当前代码已有充分控制的问题不重复列为缺陷。

## 2. 总体结论

Noyra 当前已经具备较完整的主体运行时、SQLite 持久化、加密存储、完整性 watchdog、管理 API、模型与搜索 provider、钱包限额和公开投影。代码中没有发现默认 loopback 配置下的已确认 P0 级远程代码执行，也没有发现无认证即可触发转账的路径。HTTP transport 已统一加入公开 DNS 校验、禁止重定向、地址限制、连接固定、响应大小和 deadline 约束；公开 projection 采用白名单；钱包链重组已经有 reconciliation 降级路径。

仍然不能把当前 HEAD 标记为“已完成公网无人值守自动付款生产版”。最重要的剩余风险是：

1. production 仍允许 operator token 从普通环境变量读取；模型池 JSON 也允许把 API key 直接写入配置文本，绕过生产 preflight 的 managed-secret 检查。
2. provider half-open 探测已经生成 lease token，但路由调用只得到布尔值，模型和搜索调用没有把 token 传回 `record_attempt()`；旧请求可能因此错误释放或延迟释放恢复探测租约。
3. `outcome_unknown` 在 provider health 聚合中仍以 `success=False` 计入普通失败和错误桶，可能把“结果未知”误判成可重试失败并提前触发 breaker。
4. retention 只删除少量明确注册的表；诊断把 `unclassified` 固定为空，无法证明所有长期增长表都已经得到生命周期分类。旧 cursor 复用、持久 run 统计和返回值不一致也会影响清理可信度。
5. Release workflow 要求同 SHA 的 `external-gates.json`，但当前 workflow 没有生成或上传它的步骤；没有额外的受信任 artifact 注入时，自动 tag release 会被门禁拒绝。
6. 自动付款、真实链重组、广播后断连、nonce 冲突、签名器/KMS 和备份恢复仍缺真实环境验收证据。这是发布门禁缺口，不应误写成“正常路径已经全部可靠”。

## 3. 风险分级和修复风险定义

### 3.1 问题风险

| 等级 | 含义 | 处理要求 |
|---|---|---|
| P0 | 直接远程代码执行、大规模机密泄露、不可逆主体损坏或无门槛高危副作用 | 立即隔离并停止相关能力 |
| P1 | 认证、资金安全、主体完整性或无人值守运行边界可能被破坏 | 发布前修复并完成故障注入 |
| P2 | 明显的安全、可靠性、容量、成本或运维风险，有绕行方案 | 稳定版前修复 |
| P3 | 统计、文档、产品合同或研究证据缺口，不直接突破安全边界 | 排期处理；不能对外宣称已完成 |

### 3.2 修复风险

- **低**：默认值、启动门禁、文档或局部统计，兼容性影响小。
- **中**：涉及 API、路由状态、迁移或并发，需要兼容旧数据和回滚测试。
- **高**：涉及钱包状态机、异步 worker、密钥、完整性或跨文件/数据库原子性，需要故障注入和恢复演练。
- **极高**：跨存储、云归档、发布和所有权模型的统一重构。

## 4. 信任边界与数据流

1. **公开访问面**：匿名读取公开状态、日记、行为、互动和审核通过的帖子；只能输出显式白名单字段。
2. **管理访问面**：Bearer 角色令牌或管理 Session；可修改配置、生命周期、钱包、导出和运行防护。
3. **主体运行时**：单主体 SQLite、生命周期/睡眠、认知循环、异步工作和完整性 watchdog。
4. **外部 provider**：模型、embedding、搜索、通讯、S3 归档和链 RPC；这些系统的超时和结果可能是未知的。
5. **机密边界**：私有文件、systemd credentials、钱包 keystore、备份 keyring 和令牌文件。
6. **发布边界**：GitHub Actions、同 SHA gate、wheel、SBOM、Sigstore 和 external gate artifact。

系统的主要复杂性来自多个持久和非持久状态源同时存在：SQLite 行、环境/秘密文件、内存限速桶、provider breaker、异步 worker、云副本和外部链状态。凡是跨越这些边界的操作，都需要 epoch、lease、幂等键和 reconciliation 证据。

## 5. 当前已确认的有效控制

以下项目在当前 HEAD 中已经存在，因此不作为本轮开放问题重复报告：

- production 要求显式 genesis hash、at-rest required、Secure session cookie，并自动关闭 developer log export；默认要求 loopback，只有受控 `container_internal` 配置可非 loopback。
- operator token 文件使用 `read_secret_file()` 的安全读取器；Bearer 角色和最小长度校验存在。
- Admin Session 具备 HttpOnly、SameSite、CSRF、TTL、数量上限和 HTTP 安全响应头；可信代理 CIDR 才能启用转发来源解析。
- preflight 会清理父进程遗留的 `NOYRA_*` 环境变量，避免验证结果被外部 shell 污染。
- 模型、搜索、embedding、world、browser 和 wallet RPC 使用统一的有界 HTTP transport；默认禁止重定向并限制私网/非 global 地址。
- 公开 projection 对 state、diary、behavior、interactions 和 public posts 使用字段白名单；goals、projects、diagnostics 等读取需要 token。
- 完整性 registry 当前包含 core、mind、sleep、interaction、wallet、world、learning、cognition、model、knowledge 以及 operations provider health/retention 检查；当前 schema 版本为 71。
- retention 对失败路径已有独立补偿插入；在数据库完全不可写时只能返回内存错误，这是基础设施故障的剩余限制。
- provider projection 已返回窗口、桶数量、失败率、平均/p50/p95 延迟和最近成功时间。
- 钱包已经定义余额不足、Gas 过高、Nonce 冲突、确认超时、链重组、RPC 不可用、广播未知和 reconciliation 等原因码；receipt 变化会进入 reconciliation。
- 自动付款策略符合当前产品要求：单笔上限、日限额、总开关和 emergency pause；recipient allowlist 可选且默认不启用，人工确认和月限额不是本项目当前缺陷。

## 6. 问题总表

| 编号 | 问题 | 风险 | 修复风险 | 触发概率 | 建议时机 |
|---|---|---:|---:|---:|---|
| F01 | production operator token 仍可从 inline 环境变量回退 | P2 | 低 | 中 | 立即限制生产 |
| F02 | 模型池 JSON 的 inline `api_keys` 绕过 managed-secret preflight | P2 | 中 | 中 | 立即限制生产 |
| F03 | provider half-open probe token 没有贯穿调用链 | P2 | 中 | 低至中 | 近期稳定版 |
| F04 | `outcome_unknown` 混入普通 failure/error_counts | P2 | 中 | 低至中 | 近期稳定版 |
| F05 | retention 未动态治理全部长期增长表，诊断固定声称无未分类表 | P2 | 中高 | 高 | 近期重点 |
| F06 | retention 成功 cursor 被盲目复用，旧时间戳 backfill 可能被跳过 | P2/P3 | 中 | 低至中 | 近期 |
| F07 | retention run 持久统计与返回值在清理历史后不一致 | P3 | 低至中 | 中 | 近期 |
| F08 | additive DDL/索引/触发器没有逐项 feature marker | P3/P2 | 中 | 中 | 下一次 schema 变更前 |
| F09 | 公网管理台登录失败限速是进程内状态 | P2/P3 | 中 | 低至中 | 近期 |
| F10 | release workflow 缺少 `external-gates.json` 的完整注入/上传链路 | P2/P3 | 中 | 高 | 下一次真实发布前 |
| F11 | 真实自动付款、链重组、KMS、备份恢复和 soak 尚未外部验收 | P1 发布门禁 | 高 | 未量化 | 自动付款/发布前 |
| F12 | benchmark helper 仍直接使用 `urllib.request.urlopen()` | P3/P2 条件性 | 中 | 低 | 暂缓或在开放入口前 |
| F13 | 公共 projection 与产品公开合同仍需业务复核 | P3 | 低至中 | 取决于产品决策 | 上线前确认 |

## 7. 逐项问题记录

### F01：production operator token 仍可从 inline 环境变量回退

- **状态**：已确认开放问题。
- **风险等级**：P2（生产机密暴露和运维边界风险）。
- **修复风险等级**：低。
- **根因**：`ServiceSettings.from_env()` 先尝试 `NOYRA_OPERATOR_TOKEN_FILE`，但在文件不存在或读取为空时仍执行 `or os.getenv("NOYRA_OPERATOR_TOKEN")`。统一安全读取器保护了文件路径，却没有在 production profile 禁止 inline fallback。
- **影响**：令牌可能出现在普通 EnvironmentFile、systemd 环境、进程检查、诊断快照、shell 历史或错误收集路径中；泄露后可直接调用管理 API。它还使“生产秘密必须来自服务器秘密文件/系统凭据”的运维约定无法由代码强制。
- **触发条件**：生产部署未设置 token file，或文件读取失败而只设置了 `NOYRA_OPERATOR_TOKEN`；攻击者随后获得主机配置或进程环境读取能力。
- **触发概率**：中。当前模板和历史运维步骤容易保留 inline 变量，新主机部署尤其容易发生。
- **证据**：`src/noyra/service.py` 的 `operator_token_file` 解析及第 1047 行附近的 inline fallback；生产 preflight 没有检查 operator token 的 source。
- **当前缓解**：token 有最小长度/占位符校验；文件读取器会校验绝对路径、权限和所有权；systemd 可通过 credentials 提供文件。
- **建议**：production profile 只允许 file 或 systemd credential；若 file 未配置或不可读应启动失败；保留 inline 只给 development/private preview，并在 health/preflight 显示来源类型而不显示值。
- **时机**：立即限制生产；修复可与下一次配置门禁一起完成。
- **验证方法**：分别用 file、credential、inline-only、file+inline、缺失 file 五种 fixture 运行 preflight；production 只允许前两种，并检查 token 不出现在导出和日志。

### F02：模型池 JSON 中的 inline `api_keys` 绕过 managed-secret preflight

- **状态**：已确认开放问题。
- **风险等级**：P2。
- **修复风险等级**：中。
- **根因**：`resource_groups_from_env()` 在 `NOYRA_*_MODEL_GROUPS_FILE/CREDENTIAL` 未设置时直接读取 `NOYRA_*_MODEL_GROUPS_JSON`；每个 group 允许 `api_keys` 数组，并与 `api_key_files`、`api_key_credentials` 合并。`preflight-production.py` 只检查普通 `NOYRA_MODEL_API_KEY` 和 `NOYRA_EMBEDDING_API_KEY` 的 source，没有解析 group JSON 内的 key。
- **影响**：生产 `.env`、JSON 配置或管理导出可能包含模型密钥；密钥暴露范围扩大，轮换、撤销和审计也无法按服务器秘密文件处理。
- **触发条件**：配置认知资源池时把 `api_keys` 直接写进 `NOYRA_DEEP_MODEL_GROUPS_JSON` 或同类变量，并通过了当前 preflight。
- **触发概率**：中。管理台配置体验和兼容旧部署会诱导用户采用最短的 JSON 形式。
- **证据**：`src/noyra/model/resources.py` 的 `raw_keys = normalized.pop("api_keys", [])` 与 `resolved_keys = [*raw_keys]`；`scripts/preflight-production.py` 只遍历 `MODEL`、`EMBEDDING` 的单键环境变量。
- **当前缓解**：管理台持久化 key 时会写入受保护的 secret file/intents；file/credential 路径使用安全读取器；模型 key 在数据库中使用 `SecretStr` 和引用。
- **建议**：生产禁止 group JSON 的 `api_keys` 非空；只接受 `api_key_files` 或 `api_key_credentials`，并让 preflight 解码 JSON、对每个 group 做 source 检查；迁移工具应把旧 inline key 转移到私有文件后再启动。
- **时机**：立即限制 production；兼容迁移可在稳定版完成。
- **验证方法**：用嵌套 group JSON、file、credential、混合 source 和 malformed JSON fixture 验证 preflight 与启动行为；确认错误指出 group/provider，而不是只报 generic settings error。

### F03：provider half-open probe token 没有贯穿调用链

- **状态**：已确认开放问题。
- **风险等级**：P2（故障恢复和统计可信度）。
- **修复风险等级**：中。
- **根因**：`ProviderHealthStore.route_available()` 在 cooldown 到期时生成 `probe_token` 并把状态改为 `half_open`，但返回类型仍是 `bool`。`research/search.py` 和 `model/resources.py` 的调用方只按布尔值继续请求；调用完成时 `record_attempt(..., probe_token=...)` 没有拿到对应 token。
- **影响**：请求完成后无法可靠确认它是否拥有当前 probe lease。实现会保留 lease 到超时或由不匹配 token 的旧请求触发保护分支，可能造成恢复探测延迟、错误的 half-open 状态和不准确的 breaker 统计。
- **触发条件**：provider 从 cooldown 恢复；同一 provider 有旧的 in-flight 请求、超时请求或多个 worker 并发竞争；其中一个请求晚于 probe 完成。
- **触发概率**：低至中。需要 provider 故障恢复和并发/超时组合，但长期运行中并非罕见。
- **证据**：`src/noyra/core/provider_health.py` 的 `route_available()` 返回 `bool`，而 `record_attempt()` 接受可选 `probe_token`；`src/noyra/research/search.py` 第 308 行附近和 `src/noyra/model/resources.py` 第 3308、2811 行附近没有 token 传递。
- **当前缓解**：状态哈希包含 probe token；不匹配 token 的完成不会覆盖新 claim；lease 最终会超时。
- **建议**：返回不可伪造的 `RoutePermit(available, probe_token, state_revision)`；所有物理 attempt 必须绑定 permit；没有 permit 的完成不能清除 half-open claim；对 permit 过期记录显式 `probe_expired` 统计。
- **时机**：近期稳定版，在启用多 provider 自动切换前完成。
- **验证方法**：构造 cooldown、两个并发 probe、旧请求晚到、probe 成功、probe 失败和进程重启场景，检查只有持有 token 的 attempt 能结束当前 claim。

### F04：`outcome_unknown` 混入普通 failure/error_counts

- **状态**：已确认开放问题。
- **风险等级**：P2；在按失败率自动切换时可升级为 P1 运行风险。
- **修复风险等级**：中。
- **根因**：`ProviderHealthStore.record_attempt()` 接收 `success: bool` 和 `error_code`，无独立的 outcome 枚举；无论错误是已知失败还是未知结果，都执行 `failure_count += int(not success)`、`attempt_count += 1`，并把错误分类计入 `error_counts`。模型/搜索调用对 `outcome_unknown` 的 durable ledger 语义较谨慎，但 provider health 投影仍把它压成普通失败。
- **影响**：失败率和 breaker 可能被未知结果提前推高；运营人员无法区分“请求肯定失败”和“外部 provider 可能已经执行但结果丢失”。自动 failover 可能重复产生 billable model call，或在搜索场景错误地切换 provider。
- **触发条件**：HTTP POST 在 provider 已接收后连接中断、read timeout、响应超限或服务端 5xx；调用方用 `success=False` 记录。
- **触发概率**：低至中，取决于网络质量和 provider 稳定性；公网 API 长期运行必然会偶发。
- **证据**：`src/noyra/core/provider_health.py` 第 286–295 行附近的失败计数逻辑；`src/noyra/model/openai_compatible.py` 明确将 read/write/remote protocol 和 5xx 标为 `outcome_unknown=True`。
- **当前缓解**：模型路由遇到 `ProviderCallError.outcome_unknown` 会停止普通 failover，并有显式的 unknown retry authorization；provider 端不会保存请求正文或 token。
- **建议**：将统计 outcome 拆为 `success/known_failure/unknown`；failure rate 只使用已知失败，另设 unknown rate 和 unknown breaker policy；模型和搜索的恢复必须以 logical request/idempotency 绑定。
- **时机**：近期稳定版；自动付款前虽不直接阻塞，但 provider 路由依赖它时必须完成。
- **验证方法**：对 connect failure、read timeout、5xx、401、429、schema error 和正常成功注入 fixture，核对三个计数、路由决策、冷却和 projection 字段。

### F05：retention 未动态治理全部长期增长表，诊断固定声称无未分类表

- **状态**：已确认开放问题。
- **风险等级**：P2（容量、可用性和恢复时间）。
- **修复风险等级**：中高。
- **根因**：`RETENTION_REGISTRY` 明确删除 provider health buckets、search provider uses、部分 runtime/legacy 表和 retention_runs，并把若干证据表标记 preserve；但 `retention_registry_diagnostics()` 固定返回 `"unclassified": ()`，没有将 `sqlite_master` 中的实际持久表与 registry 做动态差集。
- **影响**：新增或历史未登记的 `events`、`audit_records`、`wallet_balance_snapshots`、支付执行尝试、storage usage samples、browser reservations、CAPTCHA/rate events、revision/history 和 incident 表可能无限增长。SQLite、WAL、备份、完整性扫描和导出会越来越慢，磁盘耗尽时会影响主体可用性。
- **触发条件**：启用认知循环、模型/搜索、公开投稿或钱包后持续运行数周至数月；registry 增加新表但没有同步 retention policy。
- **触发概率**：高。只要功能持续产生事件就会增长。
- **证据**：`src/noyra/core/retention.py` 的删除 registry 与固定空 `unclassified` 返回；`src/noyra/core/database.py` 包含大量持久表定义。
- **当前缓解**：部分表有 quota、批量上限和存储 boundary；核心 evidence 表不会被自动删除。
- **建议**：启动/CI 动态核对全部持久表，要求每张表声明 `core_evidence/required_audit/rebuildable_aggregate/temporary_queue`；未分类表应使 preflight 失败或明确进入保护区。为每类定义保留周期、压缩/归档、WAL checkpoint 和 vacuum 策略。
- **时机**：近期重点；开放公网投稿和长期认知运行前必须完成。
- **验证方法**：从当前 schema 生成 sqlite_master inventory，与 registry 做差集；用加速时钟生成 30/90/180 天 fixture，验证行数、磁盘、WAL、导出和完整性耗时。

### F06：retention 成功 cursor 被盲目复用，旧时间戳 backfill 可能被跳过

- **状态**：已确认设计风险。
- **风险等级**：P2/P3。
- **修复风险等级**：中。
- **根因**：`run_batch()` 读取最近一次成功或失败 run 的 `next_cursor`，只要 cursor 结构有效就复制到本次批次；cursor 含按时间和 ID 排序的 keyset 位置，但代码没有同时验证旧 cutoff、registry 版本和数据回填方向。
- **影响**：当系统时钟、保留周期或历史数据发生变化时，旧 cursor 的 `>` 条件可能跳过后来插入但时间更早的 backfill 行，形成“统计显示已清理、实际仍有旧数据”的容量和合规误差。
- **触发条件**：导入旧时间戳数据、修改 retention days/hours、时钟回拨、从备份恢复后继续执行或 registry 排序键发生变化。
- **触发概率**：低至中；正常单调时钟运行较少触发，但恢复/迁移时概率上升。
- **证据**：`src/noyra/core/retention.py` 第 385–403 行复制 previous cursor，第 527 行以后 `_delete_table()` 以 keyset `>` 配合当前 cutoff 删除。
- **当前缓解**：cursor 按表保存且有 JSON/结构校验；重复运行仍有边界上限。
- **建议**：cursor 必须绑定 `cutoff`、registry version、排序键版本和数据 epoch；cutoff 改变或检测到旧时间戳 backfill 时丢弃 cursor，重新从头扫描；报告中显示是否发生 reset。
- **时机**：近期；备份恢复与 retention 可靠性验收前完成。
- **验证方法**：先运行批次再插入更早时间戳记录，改变 cutoff、恢复备份并重启，验证所有应删除记录最终被处理。

### F07：retention run 持久统计与返回值在清理历史后不一致

- **状态**：已确认开放问题。
- **风险等级**：P3。
- **修复风险等级**：低至中。
- **根因**：成功事务先插入本次 `retention_runs`，然后删除超出 `run_history` 的旧 run，并只更新内存中的 `deleted["retention_runs"]` 和 cursor；已经写入数据库的本次行没有反映“清理了多少 retention_runs”。返回值与持久行的 `deleted_by_table_json`/`state_hash` 因此可能不同。
- **影响**：管理台、完整性检查、审计和自动化监控可能读取到不同的删除计数；state hash 只证明插入时的 payload，不能证明最终事务结果的完整摘要。
- **触发条件**：retention history 超过 `run_history`，并在同一批次执行历史清理。
- **触发概率**：中；默认长期运行后会稳定触发。
- **证据**：`src/noyra/core/retention.py` 第 434–471 行先 INSERT 再 prune，随后只修改内存变量。
- **当前缓解**：核心删除操作和 history prune 在同一事务内；行本身有 state hash。
- **建议**：先计算 prune 结果再构造最终 payload，或在同一事务内更新本次 row 的计数、cursor 和 hash；返回值直接从最终持久行构造。增加“成功记录最终内容与返回值相等”的断言。
- **时机**：近期稳定版。
- **验证方法**：设置 `run_history=1`，执行多次批次，比较 API 返回、数据库行、state hash 和 integrity projection。

### F08：additive DDL、索引和触发器没有逐项 feature marker

- **状态**：已确认恢复/离线证明缺口；不是当前已证实的启动迁移破坏。
- **风险等级**：P3，涉及恢复和离线工具时可升级为 P2。
- **修复风险等级**：中。
- **根因**：`persistent_features` 已为 `secret_cleanup`、`secret_file_intents` 等功能记录 marker 和 DDL fingerprint，但 `_ensure_optional_features()` 以及后续大量 additive `CREATE TABLE/INDEX/TRIGGER`、ALTER 列修复没有为每个可选结构记录独立 feature version/fingerprint。
- **影响**：数据库 marker 可能显示 schema 版本正确，但实际 sqlite_master 与运行时所需结构不一致。离线导出、旧版本恢复、部分组件初始化和灾难恢复难以判断是否完成了同一结构合同。
- **触发条件**：从旧备份恢复、只启动部分组件、升级中断后重启、手工复制数据库或运行与 schema marker 不匹配的 runtime。
- **触发概率**：中；普通连续升级较少触发，恢复/迁移频繁时上升。
- **证据**：`src/noyra/core/database.py` 的 `CURRENT_SCHEMA_VERSION=71`、`persistent_features` 处理与大量 additive DDL 分散在启动路径。
- **当前缓解**：迁移有版本号、SQLite backup、quick check 和失败恢复；多数 DDL 幂等。
- **建议**：把所有持久结构放入正式 migration，或为 optional feature 增加 feature version、DDL fingerprint、owner 和 restore preflight；禁止构造器静默改变持久 schema。
- **时机**：下一次 schema 版本变更前完成；现有生产升级应增加离线 manifest 检查。
- **验证方法**：从空库、旧库、只初始化核心组件和完整组件四条路径启动，对比 sqlite_master、marker、export manifest 和 backup restore 结果。

### F09：公网管理台登录失败限速是进程内状态

- **状态**：已确认架构限制。
- **风险等级**：P2/P3。
- **修复风险等级**：中。
- **根因**：`Service` 用 `_admin_login_failures: dict[str, deque[float]]` 和 `_rate_lock` 在单进程内保存失败桶；重启、滚动升级或多实例部署不会共享该状态。
- **影响**：攻击者可以通过重启窗口或多实例轮换获得新的失败预算；负载均衡下每个实例分别计数，整体尝试次数高于配置值。该问题不会绕过 token 校验，但会削弱公网暴露时的防暴力能力和监控可信度。
- **触发条件**：管理台公网开放；服务重启、多个 worker/副本或反向代理将请求分散到不同进程。
- **触发概率**：低至中，取决于部署拓扑；单进程 loopback 管理台概率低。
- **证据**：`src/noyra/service.py` 第 1422 行附近创建内存字典，第 1702–1725 行读写失败桶。
- **当前缓解**：有独立失败预算、普通请求限速、Session TTL、CSRF 和 token 校验；默认服务单进程且 loopback。
- **建议**：公网 profile 使用反向代理/WAF 或 SQLite/Redis 共享的带 TTL 失败桶；按 token fingerprint 与可信 client identity 组合计数；把重启/多实例行为加入 preflight 与监控。
- **时机**：管理台正式公网开放前完成；内网单实例可暂缓。
- **验证方法**：多 worker、滚动重启、可信代理来源和伪造 X-Forwarded-For 测试；确认整体预算而非单实例预算生效。

### F10：release workflow 缺少 `external-gates.json` 的完整注入/上传链路

- **状态**：已确认发布流程缺口。
- **风险等级**：P2/P3；当前更常见结果是发布失败，错误的 artifact 注入也会成为供应链风险。
- **修复风险等级**：中。
- **根因**：`.github/workflows/release.yml` 的 release job 在 `artifacts/release/stage4b4/<sha>/external-gates.json` 找不到文件时直接失败，并要求 `EXTERNAL_GATES_PUBLIC_KEY`；quality-gate 只上传 wallet gate evidence，没有生成或上传 external gate 文件的步骤。
- **影响**：没有额外可信 artifact 注入机制时，自动 tag release 一定无法通过 external gate；如果以后用临时手工文件绕过，可能破坏同 SHA、签名、新鲜度和 reviewer 证据链。
- **触发条件**：推送匹配 `v*` 的 tag；quality gate 完成但没有同 SHA external-gates artifact。
- **触发概率**：高。workflow 路径本身缺少生成步骤。
- **证据**：`.github/workflows/release.yml` 第 56–65 行生成/下载 wallet evidence，第 84–98 行直接读取 `external-gates.json`；仓库中没有相应 upload-artifact 步骤。
- **当前缓解**：`scripts/verify_external_gates.py` 校验 schema、SHA、freshness、reviewer、固定 gate IDs、evidence refs 和 Ed25519 signature。
- **建议**：明确 external gate 的受信来源：独立 workflow 生成并上传不可变 artifact，或受保护 environment 注入同 SHA artifact；release job 只下载、验证和消费，禁止工作流内随意生成“通过”记录。把缺失/过期/签名不符分别显示为可操作错误。
- **时机**：下一次真实发布前必须完成。
- **验证方法**：隔离仓库演练 tag、正常 artifact、错 SHA、过期、缺签名、错误 reviewer 和缺 gate ID，确认仅合法 artifact 能发布。

### F11：真实自动付款、链重组、KMS、备份恢复和 soak 尚未外部验收

- **状态**：开放发布门禁；不是已证实的代码漏洞。
- **风险等级**：P1（自动付款启用场景）。
- **修复风险等级**：高。
- **根因**：本地单元/集成测试和一次 Sepolia 正常路径只能覆盖确定性模拟或健康网络；当前没有外部 artifact 证明 signer/KMS、真实 RPC timeout、广播后断连、nonce 冲突、receipt 延迟、reorg、重启恢复、备份恢复和多日数据库增长组合。
- **影响**：未知结果可能导致重复广播、订单长期悬挂、错误暂停或账本与链状态暂时不一致；恢复/升级可能暴露未覆盖的迁移或密钥问题。正常转账成功不能推出异常路径安全。
- **触发条件**：启用自动付款后出现余额不足、Gas 突升、nonce 被外部钱包占用、广播响应丢失、receipt 消失/变更、signer/KMS 暂不可用、服务重启或磁盘接近上限。
- **触发概率**：未量化。测试网正常路径概率低；主网、公网 RPC 和长期无人值守环境中异常不可忽略。
- **证据**：钱包代码已有原因码和 `mark_reconcile_required()`，但本审计环境没有真实链、KMS、备份或多日 soak 证据；release workflow 也把这些作为 external gate 输入。
- **当前缓解**：默认自动付款关闭；有单笔/日限额、总开关、emergency pause、admission lease、logical idempotency、receipt recheck 和 reconciliation 状态。
- **建议**：把自动付款设为独立 release gate：fake RPC/signer 故障矩阵 + Sepolia 小额 smoke + 干净主机备份恢复 + 多日 soak。未知结果不得自动换 logical payment ID 重发；暂停应阻止新 admission 并等待/标记已有 lease。
- **时机**：自动付款或主网前必须完成；不能以本地 pytest 替代。
- **验证方法**：对每个原因码核对订单、execution attempt、ledger、incident、daily counter、pause 状态、receipt 和最终余额；保存带 commit SHA、环境、时间和 reviewer 的不可变 artifact。

### F12：benchmark helper 仍直接使用 `urllib.request.urlopen()`

- **状态**：已确认的条件性设计风险。
- **风险等级**：P3；若未来接入管理员或外部可控 URL，可升级 P2。
- **修复风险等级**：中。
- **根因**：`src/noyra/mind/benchmarks.py` 直接创建 `Request` 并调用 `urlopen()`，未使用项目统一的 public DNS/pinned connect/no-redirect transport。
- **影响**：一旦 benchmark URL 由管理员配置、公开 API 或模型输出间接控制，可能重新引入 DNS rebinding、私网探测、重定向、无界响应或环境代理问题。当前未发现它直接暴露为 HTTP service route。
- **触发条件**：未来把 benchmark helper 接到外部输入、管理台配置或自动研究计划，并允许访问任意 URL。
- **触发概率**：低；取决于未来功能演进。
- **证据**：`src/noyra/mind/benchmarks.py` 第 19–20、131 行附近导入并调用 `urlopen`；browser/world/embedding/wallet 等其他路径已经使用统一 transport。
- **当前缓解**：当前调用面不是已确认的公网任意 URL 代理；benchmark 是内部 helper。
- **建议**：在扩大调用面前迁移到统一 transport，或把 URL 限制为固定、经过 allowlist 和 capability 验证的资源；加入 SSRF/DNS rebinding 回归测试。
- **时机**：当前可暂缓；任何公开/管理员 URL 配置上线前必须完成。
- **验证方法**：以 loopback、私网、IPv6、重定向、超大响应和 DNS 变化 fixture 运行 benchmark；确认与统一 transport 使用同一拒绝策略。

### F13：公共 projection 与产品公开合同仍需业务复核

- **状态**：待产品确认，不应直接定性为泄密漏洞。
- **风险等级**：P3。
- **修复风险等级**：低至中。
- **根因**：公共 projection 的代码白名单已经存在，但“公开 state/diary/behavior/interactions 的哪些字段、计数和时间范围属于公开合同”主要由产品决策确定，而非安全代码能够单独推断。
- **影响**：如果产品后来认为某个生命周期状态、行为摘要、互动记录或 provenance 不应公开，当前 API 可能造成隐私、品牌或用户预期不一致；反之过度收紧会破坏公开站点和 SEO 内容。
- **触发条件**：公开站点上线、增加新 projection 字段、改变 anonymization 或引入新互动 channel。
- **触发概率**：取决于产品决策；每次 projection 扩展都会重新触发复核。
- **证据**：`src/noyra/interaction/projection.py` 的 `state()`、公共 diary/behavior/interactions 和 moderated posts 读取路径；当前未发现字段白名单绕过。
- **当前缓解**：goals/projects/diagnostics/runtime logs 需要 read token；public posts 只返回审核内容、公开作者标签和 provenance。
- **建议**：形成版本化 public contract：字段、最大行数/字节数、时间窗口、匿名化、删除/撤回语义和缓存策略；每次 schema 变更运行响应快照和人工隐私复核。
- **时机**：公开站点正式上线前确认；不需要阻塞内部开发。
- **验证方法**：对匿名、read token、revoked post、private channel、新增字段和超大数据量执行 contract tests，检查响应只包含批准字段。

## 8. 已关闭问题与避免重复报告

本次复核明确关闭或降级以下历史结论：

- operator token file 未使用统一读取器：当前已使用 `read_secret_file()`；剩余问题是 production inline fallback。
- preflight 被父进程 `NOYRA_*` 污染：当前会清理后重新载入 env。
- provider projection 缺少窗口字段：当前已有 `window_start`、`window_end` 和 `bucket_count`。
- provider/retention 表完全未进入 migration：当前 `CURRENT_SCHEMA_VERSION=71` 且 retention 初始化会拒绝缺失 schema；剩余问题是所有 additive 结构的 feature marker/恢复证明不完整。
- secret cleanup 完全没有 feature marker：当前已具备 marker 和 DDL fingerprint。
- retention 会删除 append-only cognitive route 证据：当前 registry 已将保护类别显式 preserve；剩余问题是其他增长表和动态差集。
- chain reorg 后 confirmed 永不降级：当前 receipt verification 与 `mark_reconcile_required()` 已有降级路径。
- 默认不启用 recipient allowlist、没有人工确认、没有月限额：这些符合当前产品要求，不是缺陷。

## 9. 立即修复和近期顺序

### 9.1 立即限制生产风险

1. production 禁止 inline operator token，preflight 必须校验 token source（F01）。
2. production 禁止 model group JSON 中的 inline `api_keys`，提供迁移到 file/credential 的工具（F02）。
3. 在自动付款开启前，将 F11 作为不可跳过的 external release gate；没有真实证据就保持自动付款关闭。

### 9.2 第一阶段：数据生命周期和可恢复性

1. 动态核对 retention registry 与 `sqlite_master`，为所有表分类并修复 F05。
2. 让 cursor 绑定 cutoff、registry version 和数据 epoch，修复 F06。
3. 让 retention 的最终持久行、state hash 和返回值来自同一最终 payload，修复 F07。
4. 为 additive DDL 增加 feature marker/fingerprint，修复 F08。

### 9.3 第二阶段：provider 路由和故障语义

1. 用 RoutePermit 贯穿 probe claim 和 attempt completion，修复 F03。
2. 拆分 known failure 与 unknown outcome，修复 F04。
3. 增加 provider half-open、unknown、failover 和 cooldown 的并发故障注入。

### 9.4 第三阶段：公网管理与发布

1. 公网多实例使用共享失败桶或边缘 WAF，修复 F09。
2. 完成 external-gates artifact 的受信生成/上传/下载链路，修复 F10。
3. 形成 public projection contract，完成 F13 的产品确认。

### 9.5 可暂缓

- F12 在 benchmark 未接受外部 URL 前可以暂缓，但必须登记为 capability gate。
- F07 的展示精度可以在不把 retention 作为合规证据的情况下排在 F05/F06 之后，但不能无限期忽略。

## 10. 真实环境验收清单

在发布或开启自动付款前，必须产生可离线验证、绑定 commit SHA 的证据：

1. **生产安装**：干净 Ubuntu 主机上安装、升级、回滚、LUKS 挂载、systemd credentials、权限和 preflight。
2. **HTTPS 管理台**：Caddy/Nginx、trusted proxy CIDR、Secure cookie、HSTS/CSP、失败限速和多 worker 行为。
3. **秘密生命周期**：模型/search/embedding/operator token 的 file/credential、轮换、撤销、导出脱敏和旧 key 清理。
4. **provider 故障注入**：401、403、429、5xx、连接超时、read timeout、响应超限、未知结果、cooldown、half-open 和恢复探测。
5. **钱包故障注入**：余额不足、Gas 高、nonce 冲突、广播后断连、确认超时、receipt 消失/变更、链重组、签名器不可用、重启恢复。
6. **备份恢复**：WAL、schema/feature marker、密钥轮换、旧版本拒绝、归档副本和完整性扫描。
7. **长期运行**：至少覆盖多日的认知循环、provider 聚合、retention、WAL、磁盘增长、导出和 watchdog 周期检查。
8. **发布供应链**：受保护 tag、production reviewer、external-gates 签名/新鲜度、SBOM、Cosign 离线验证和 artifact 复现。
9. **公开站点**：移动端、慢网络、屏幕阅读器、响应大小、CAPTCHA/队列、SEO/社交预览和公共 contract 快照。

## 11. 下一代升级方向

### 11.1 统一 Runtime Ownership、Epoch 和 Lease

把数据库、迁移、HTTP handler、provider worker、导出、归档和钱包 admission 统一到可验证的 epoch/fence token。所有 await 返回后重新检查 epoch、生命周期、quarantine 和 operation lease；旧 worker 的迟到写入只能进入隔离记录。

### 11.2 配置与秘密 Control Plane

用版本化配置对象替代分散的 env/template 语义：profile、secret source、配置 revision、actor、摘要 diff、rollback pointer 和双 key overlap。管理台展示“可运行”和“安全可运行”两种状态，preflight 生成机器可读报告。

### 11.3 Provider Router 2.0

统一 model、embedding、search 的 priority、weight、capability、cooldown、half-open permit 和 unknown 状态；只保存固定枚举和聚合，不保存 URL、token、请求正文或完整响应。logical request 与物理 attempt 分离，unknown 只能进入显式 reconcile。

### 11.4 Wallet Payment Safety Kernel

把付款做成可恢复账本状态机：`proposed → admitted → signing → broadcast_unknown/broadcasted → confirming → confirmed/failed/reorged/reconcile_required`。单笔/日限额在 admission CAS 中执行；replacement/nonce bump 保持同一 logical payment；reorg 只能回到 reconciliation。

### 11.5 Data Lifecycle Registry

每张表声明 owner、schema version、hash contract、repair strategy 和 retention class；核心证据、必要审计、可重建聚合和临时队列分开治理。retention 支持 dry-run、estimate、run、verify，每表独立 cursor、失败记录和存储 quota。

### 11.6 Integrity Registry 2.0

将每张持久表的完整性规则、版本、修复策略和处置等级统一注册；启动轻检查与周期深检查使用同一 manifest。统计损坏可重建时应安全降级，核心证据损坏时应 quarantine 并保留 evidence ID。

### 11.7 管理台与公开站点产品化

管理台用中文向导显示 provider 健康、钱包暂停/限额、retention、密钥来源和危险操作的幂等键；公开站点采用静态壳、渐进加载、图像优化、无障碍标签、响应预算、OG/Twitter 预览和移动端优先布局。

### 11.8 灾难恢复与发布证据

为每个 schema/feature 版本生成离线 manifest；备份恢复演练覆盖密钥轮换和旧版本拒绝；发布使用受保护 tag、最小 job 权限和不可变 external gate。把“代码测试通过”和“真实 testnet/KMS/backup/soak 通过”分开显示。

## 12. 审计验证记录与限制

本轮已执行或复核：

- `python -m compileall -q src scripts tests`：通过。
- `python -m ruff check src scripts tests`：通过。
- `python -m ruff format --check src scripts tests`：通过，全部文件已格式化。
- `git diff --check`：通过。
- 使用项目 `.venv` 启动完整 pytest：`1515 passed, 24 skipped, 259 subtests passed in 2960.96s`。跳过项集中在 Windows 不可移植的 POSIX 权限、symlink 和 8.3 alias 合同。系统 Python 因缺少 `eth_account` 产生的 collection error 不作为项目代码失败证据。
- 静态复核 production settings、preflight、model resources、provider health、retention、database migration、integrity registry、wallet execution、benchmark helper 和 release workflow。

未执行：真实服务器、真实 KMS/S3/RPC/signer、真实链重组、主网付款、公网压力、多实例部署、长时间 soak、GitHub 仓库保护规则和 production environment reviewer 的实际验证。因此本报告的 P1 发布门禁和“未量化”概率不能被本地测试结果替代。

## 13. 审计声明

本报告是基于提交 `f418c7b5a18cc52a70d5aaa82cbed01b3fc856a4` 的代码、配置、文档和本地验证的只读审计。风险等级表达工程后果，触发概率表达相对工程判断；它们不是对现实攻击频率、第三方 provider SLA 或资金损失概率的统计承诺。任何修复完成后都必须重新运行针对性测试、迁移/恢复演练、故障注入和发布证据验证；在 external gate 缺失时，不应把自动付款或无人值守生产能力标记为已完成。
