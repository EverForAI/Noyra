# Noyra 全项目只读审计报告

- 审计日期：2026-09-29
- 审计类型：代码、数据库、HTTP、部署、前端、CI/CD 与运行合同的只读审计
- 审计基线：分支 `codex/wake-after-clean-restart`；业务代码提交 `cb04247fafb16c995e33f96724c850a216d35784`；当前报告提交 `18ef7cdf2df8b5bbb4982182515efee20a1a2f9f`
- 审计范围：src/noyra、tests、deploy、scripts、.github/workflows、部署与实现文档
- 变更边界：本审计只新增或更新本报告；没有修改业务代码、数据库、部署配置、服务器状态，也没有推送远端。报告提交相对于业务代码基线只包含文档变更。

## 1. 审计结论

Noyra 已经形成了较完整的主体运行时、加密存储、完整性检查、管理 API、钱包执行、模型与搜索路由、公开站点和发布流水线。当前没有在默认 loopback 监听配置下确认的 P0 级远程代码执行或无认证资金转移问题。已有安全控制覆盖面明显高于普通原型项目，尤其是 at-rest 检查、角色令牌、CSRF、反向代理信任边界、公开投影白名单、钱包 admission lease、完整性隔离和发布制品签名。

但是，系统距离“长期无人值守、可公网暴露、开启自动付款”的生产级闭环仍有几类重要缺口：

1. 生产安全仍依赖管理员手工把模板中的 Cookie、代理网段和密钥来源改对；配置错误时服务可以启动，但安全性已经下降。
2. Retention 已能清理部分 provider/search 聚合数据，却没有覆盖所有高增长运行表，失败也没有留下持久失败记录。
3. Provider 健康数据为了节省存储只保留小时聚合，诊断和故障切换证据不足；breaker 和 unknown 语义必须用故障注入才能证明。
4. 钱包原因码已经定义，但实际 receipt、超时、reorg、nonce 和 signer 错误仍有较多粗粒度状态。自动付款开启前必须完成真实状态机验收。
5. CAPTCHA 的 IP 哈希密钥是进程随机值，重启会改变限速桶，并可能使重启前挑战失效。
6. Provider health、retention 等运行期懒创建表没有统一进入 schema 迁移和完整性 registry 的显式版本合同，给备份、升级、导出和灾难恢复带来漂移风险。
7. 文档、安装脚本、反向代理示例和运行时能力之间仍有漂移；旧文档会把已实现能力描述成未实现，也没有把生产硬门槛自动化。

建议结论：

- 公开只读站点可以继续迭代上线，但必须使用 HTTPS 反向代理，并完成公开内容容量与监控配置。
- 管理台可以公网提供，但应先修复 Cookie Secure 默认、代理信任示例和生产 profile 检查。
- 在 A1、A3、A7、A9、A12、A13、A14、A15 完成前，不建议把系统标记为“无人值守自动付款生产版”。
- A2、A4、A5、A8、A10、A16、A17 应进入近期稳定版；A6 可在不对外宣称合规统计的前提下暂缓。
- 本报告列出的“开放验证项”不是已确认漏洞，但在发布门禁中必须有可复核证据。

## 2. 风险评级方法

### 2.1 问题风险

| 等级 | 含义 | 处理要求 |
|---|---|---|
| P0 | 直接远程代码执行、大规模敏感数据泄露、不可逆主体损坏或无门槛高危外部副作用 | 立即隔离并停止相关能力 |
| P1 | 破坏认证、机密性、主体完整性、资金安全、同意边界，或会让 24/7 运行失控 | 发布前修复并做故障注入 |
| P2 | 明显安全、可靠性、容量、成本或运维风险，有绕行方案 | 稳定版前修复 |
| P3 | 统计、文档、产品体验或研究证据缺口，不直接突破安全边界 | 排期处理，不能宣称已完成 |

### 2.2 修复风险

- 低：局部默认值、文档、启动检查或纯展示字段；有单元测试即可控制。
- 中：需要变更 API/表结构/状态转换，但可以兼容旧数据。
- 高：涉及迁移、钱包状态机、完整性、并发、密钥或外部副作用，需要双写/回滚/故障注入。
- 极高：跨数据库、文件、异步任务、云归档和进程所有权的统一重构。

### 2.3 触发概率

- 高：默认配置或正常长期运行即可触发。
- 中：特定生产拓扑、重启、代理或持续运行后会触发。
- 低：需要并发竞争、恶意输入或特定外部故障。
- 未量化：需要真实 provider、链、云存储或多日 soak 才能给出可信频率。

## 3. 架构与信任边界概览

Noyra 的主要边界如下：

1. 公开访问面：公开状态、公开日记、公开行为和公开帖子；应只返回显式白名单字段。
2. 管理访问面：Bearer 角色令牌或管理 Session；写配置、生命周期、钱包、导出和运行防护。
3. 运行时主体：单主体 SQLite 数据库、生命周期、睡眠、认知循环、完整性 watchdog。
4. 外部 provider：模型、embedding、搜索、通讯、S3 归档和链 RPC。
5. 机密边界：私有文件、systemd credentials、钱包 keystore、备份 keyring。
6. 发布边界：GitHub Actions、同 SHA gate、wheel、SBOM、Sigstore 签名与 Pages。

当前最大的系统性风险不是单个 HTTP 路由，而是这些边界由多个独立状态源维护：SQLite 行、文件秘密、内存 breaker、异步 worker、云副本和配置环境之间还没有一个统一的 ownership/epoch/lease/reconciliation 模型。

## 4. 已确认的安全控制

以下控制在代码或配置中已经存在，本报告不把它们重复列为缺陷：

- 默认 HTTP 监听 127.0.0.1；非 loopback 明文监听需要显式开关。
- Bearer token 有最小长度、占位符检查和角色去重；read/operator/export/break-glass 权限分层。
- Admin Session 使用 HttpOnly、SameSite 和 CSRF；登录失败与请求有速率限制。
- X-Forwarded-For 只有在可信代理网段配置后才参与客户端识别。
- provider key 支持私有文件和 systemd credential；读取时检查绝对路径、symlink/reparse、硬链接、所有权、权限和文件大小。
- 模型、钱包远端默认要求 HTTPS；HTTP transport 禁止重定向、限制响应大小，并对公开 DNS 做解析和地址限制。
- 公开 projection 使用字段白名单；goals/projects、诊断和运行日志需要 read token。
- public post 有 CAPTCHA、IP 桶、队列容量、内容字节上限、磁盘余量和幂等检查。
- integrity registry 当前包含 38 个周期检查；core.actions 覆盖管理和钱包 action audit。
- schema 67 有未来版本拒绝、迁移前 SQLite backup、quick check 和失败恢复路径。
- systemd 启用了 NoNewPrivileges、PrivateTmp、PrivateDevices、ProtectSystem=strict、ReadWritePaths 和 UMask=0077。
- wallet admission lease 防止相同外部副作用被并发重复接纳；钱包 envelope 固定字段并定义余额、Gas、Nonce、确认和 reorg 相关原因码。
- provider health 只保存小时聚合和 breaker 状态，不保存单次 URL、请求/响应、token 或完整错误正文。
- search/model 的 unknown 结果不会静默当作普通 provider failure，以避免未知副作用下自动重试。
- runtime export ownership graph 对未分类 schema 表采取 fail-closed；provider health/retention 表已加入动态 ownership 补充。
- CI 使用 SHA pin、锁文件 hash、pip-audit、SBOM、同 SHA wallet gate、Cosign 签名和发布前 external-gates 文件。

这些控制降低了风险，但不能代替生产 profile、真实故障注入和多日运行证据。

## 5. 问题总表

| 编号 | 问题 | 风险 | 修复风险 | 触发概率 | 建议时机 |
|---|---|---:|---:|---:|---|
| A1 | 管理 Session Cookie 默认未设置 Secure | P1 | 低 | 中 | 立即 |
| A2 | 反向代理示例未同步可信代理网段 | P2 | 低 | 中 | 近期 |
| A3 | 非 loopback 明文监听存在显式逃生开关 | P1 | 中 | 中 | 立即 |
| A4 | Retention cursor 语义误导且不是可恢复游标 | P2 | 低 | 中 | 近期 |
| A5 | Retention 事务失败不会持久化失败记录 | P2 | 低至中 | 中 | 近期 |
| A6 | protected_rows 基本恒为 0 | P3 | 低 | 高 | 可暂缓 |
| A7 | Retention 未覆盖多个长期增长表 | P2 | 中高 | 高 | 近期重点 |
| A8 | 生产配置闭环依赖人工修改 | P2 | 低至中 | 高 | 近期 |
| A9 | 仍允许内联 API key 作为回退 | P2 | 低 | 中 | 立即限制生产 |
| A10 | Provider 健康统计只有小时聚合 | P2 | 中 | 高 | 近期 |
| A11 | Provider breaker/unknown 语义缺专项故障注入证据 | P2（验证风险） | 中 | 未量化 | 自动付款前 |
| A12 | 钱包原因码与真实链状态映射不完整 | P1（自动付款） | 高 | 中 | 自动付款前 |
| A13 | CAPTCHA/IP 哈希密钥每次重启变化 | P2 | 中 | 中 | 近期 |
| A14 | 懒创建运行表未统一纳入 schema marker/迁移 | P2 | 高 | 中 | 近期 |
| A15 | Provider health/retention 没有独立完整性 registry 检查 | P2 | 中高 | 中 | 近期 |
| A16 | 部署文档与代码能力漂移 | P2 | 低 | 高 | 近期 |
| A17 | Release workflow 权限和外部证据仍需生产门禁 | P2/P3 | 中 | 低至中 | 发布前 |
| A18 | 自动付款状态机尚缺多故障组合验收 | P1（自动付款） | 高 | 未量化 | 自动付款前 |

## 6. 详细问题记录

### A1：管理 Session Cookie 默认未设置 Secure

- 风险等级：P1（公网管理台配置风险）
- 修复风险等级：低
- 影响：ServiceSettings.admin_session_cookie_secure 默认是 False，部署模板 NOYRA_ADMIN_SESSION_COOKIE_SECURE=false。如果管理员把管理台放到 HTTPS 反向代理后但忘记显式改值，浏览器仍可能在 HTTP 请求中发送 Session Cookie。错误的代理配置、HTTP 直连、同域降级访问或未来把管理台挂到不安全入口时，会增加会话窃取风险。
- 触发条件：管理 Session 已建立；服务或代理允许 HTTP 请求；Cookie 没有 Secure 属性。
- 触发概率：中。当前文档有提醒，但安装模板允许安全设置保持 false，人工漏改很常见。
- 证据：src/noyra/service.py 的 admin_session_cookie_secure 字段和 Set-Cookie 拼接逻辑；deploy/noyra.env.example 第 36 行。
- 当前缓解：Cookie 仍有 HttpOnly、SameSite 和 CSRF；部署文档要求 HTTPS 时设置 true。
- 修复建议：生产 profile 默认 true；当 public_site_url 或可信 HTTPS 代理模式启用时自动要求 true；开发 loopback profile 保留 false。启动时输出明确的安全门禁结果，而不是只在文档中提醒。
- 修复时机：立即。
- 验证方法：以 production profile 启动，检查 Set-Cookie 同时含 HttpOnly、SameSite 和 Secure；以 HTTP 直接访问管理台时拒绝建立 Session；运行 Secure/非 Secure 双矩阵测试。

### A2：反向代理示例没有同步可信代理网段配置

- 风险等级：P2
- 修复风险等级：低
- 影响：Caddy/Nginx 示例会转发 X-Forwarded-For 和 X-Forwarded-Proto，但示例没有同时给出 NOYRA_TRUSTED_PROXY_CIDRS。服务因此可能把所有请求看成同一个代理地址，导致登录失败限速、公共投稿限速和审计来源失真；如果管理员为了“修复”而信任任意网段，反而会允许客户端伪造来源。
- 触发条件：服务位于反向代理后；未配置可信代理 CIDR，或配置过宽。
- 触发概率：中。
- 证据：deploy/caddy/noyra.Caddyfile.example、deploy/nginx/noyra.conf.example 与 ServiceSettings.trusted_proxy_cidrs。
- 当前缓解：代码只在可信 CIDR 内解析转发头，默认不信任。
- 修复建议：示例中加入 loopback 或明确的代理网段配置；安装验收检查“代理来源、Cookie Secure、HTTPS scheme、限速桶”四项必须成组配置；禁止空值直接进入公网 production profile。
- 修复时机：近期。
- 验证方法：从可信代理与非可信客户端分别发送伪造 X-Forwarded-For，验证限速桶和审计来源；检查 Caddy/Nginx 的实际 source address 与服务配置一致。

### A3：非 loopback 明文监听存在显式逃生开关

- 风险等级：P1
- 修复风险等级：中
- 影响：validate_listener_security() 允许非 loopback host 只要设置 NOYRA_ALLOW_INSECURE_NON_LOOPBACK=true。误配后管理 API、Bearer token、Session Cookie、诊断和可能的公开/私密数据会通过明文 HTTP 暴露。
- 触发条件：NOYRA_HOST 为公网或内网地址；没有 TLS 终止；环境变量被设置为 true。
- 触发概率：中。Compose 内部容器监听 0.0.0.0 是合理的，但用户自行修改宿主端口或 systemd 配置时容易把容器边界误认为公网安全边界。
- 证据：ServiceSettings.validate_listener_security()；docker-compose.yml 容器内监听 0.0.0.0 且设置 true，宿主端口当前只绑定 loopback。
- 当前缓解：默认 false；没有显式开关时非 loopback 启动失败；Bearer token 有最小长度和占位符检查。
- 修复建议：production profile 禁止“非 loopback + 明文”组合；若必须允许，应要求由 systemd/Compose 明确证明仅内部网络可达，或由代理注入已验证的 HTTPS 标志；将 ALLOW_INSECURE_NON_LOOPBACK 限定为 development/container-internal profile。
- 修复时机：立即。
- 验证方法：生产 profile 各尝试 loopback、非 loopback+false、非 loopback+true、代理 HTTPS 四种启动组合；确认只有受控内部 profile 能通过。

### A4：Retention cursor 语义错误或误导

- 风险等级：P2
- 修复风险等级：低
- 影响：RetentionManager.run_batch() 依次清理 provider health bucket、search use、legacy attempt，复用了 rows 变量，并把最后处理表的 ID 写入单一 next_cursor。该函数本身也没有接收 cursor 来恢复执行。运维人员可能以为 cursor 是跨表断点，实际重跑可能重复扫描或无法精确续跑。
- 触发条件：单批次同时命中多个表，或清理失败后依赖 next_cursor 继续运行。
- 触发概率：中，数据积累后很常见；错误更多表现为效率和诊断问题。
- 证据：src/noyra/core/retention.py:133-228。
- 当前缓解：每批次有固定上限；删除在一个事务中完成，重复执行不会重新删除已不存在的行。
- 修复建议：选择一种明确语义：删除 next_cursor，或改成每表独立 cursor（表名、排序键、截止时间、批次版本）；run 记录保存实际起止范围。
- 修复时机：近期。
- 验证方法：构造三个表各自超过批量上限，连续运行直到清空；验证无漏删、无重复计数、重启后可继续，并让管理台显示真实游标语义。

### A5：Retention 事务失败不会持久化失败记录

- 风险等级：P2
- 修复风险等级：低至中
- 影响：retention_runs 的插入与删除操作在同一事务中。任一 SQL/磁盘错误会回滚包含 run 记录的整个事务；异常只返回内存中的 failed_reason。之后 latest() 无法区分“从未运行”和“最近运行失败”，清理可能长期静默停止。
- 触发条件：SQLite locked、磁盘空间不足、哈希校验失败、表结构漂移或删除事务异常。
- 触发概率：中，长期运行和资源紧张时会上升。
- 证据：retention.py:159-265 的单事务和异常返回路径。
- 当前缓解：服务日志会记录 deferred；下一周期会尝试再次执行。
- 修复建议：失败记录使用独立短事务或故障补偿队列；记录失败阶段、错误类别、起止范围和重试时间；管理台健康状态显示最近失败。
- 修复时机：近期。
- 验证方法：注入 locked、ENOSPC 和表完整性错误，确认失败行仍可读、不会伪报成功，并能在下一周期恢复。

### A6：protected_rows 基本恒为 0

- 风险等级：P3
- 修复风险等级：低
- 影响：运行记录有 protected_rows 字段，但当前实现从 0 初始化且没有统计受保护行。管理台或报告如果把它当作容量/合规证明，会产生误导。
- 触发条件：任何 retention run；调用方读取 protected_rows。
- 触发概率：高（统计值总是 0），但直接安全影响低。
- 证据：RetentionManager.run_batch() 中 protected = 0 且没有后续累加。
- 当前缓解：清理表范围较窄，不会删除核心审计表。
- 修复建议：要么实现按表、规则、行数的真实保护统计，要么移除字段并在 API 中明确“当前未统计”。
- 修复时机：可暂缓；如果用于对外合规证据则提升为 P2 并立即修复。
- 验证方法：为受保护与可清理 fixture 建立期望计数，验证报告和管理台一致。

### A7：Retention 覆盖范围不足，多个长期增长表未纳入清理

- 风险等级：P2（容量和可用性）
- 修复风险等级：中高
- 影响：当前明确清理 provider health buckets、search provider uses、legacy attempts 和 retention_runs；model_calls、model_attempts、actions、behavior_logs、research_search_runs、action_deliberation_runs、认知运行记录以及部分审计/路由历史仍可能长期增长。SQLite 数据库、WAL、备份、完整性扫描和导出时间会持续增加。
- 触发条件：认知循环、模型/搜索调用、公开内容或钱包活动持续运行数周以上。
- 触发概率：高，尤其在启用模型和研究功能后。
- 证据：database.py 中大量 append-only 表与 retention.py 的删除清单不一致；管理台只展示少数聚合表。
- 当前缓解：每个功能有局部 quota 或预算；subject/training/workspace 有总容量保护。
- 修复建议：为所有表建立数据分类：原始证据、必要审计、可重建聚合、临时队列。对每类定义保留周期、压缩/归档、不可删除例外、WAL checkpoint 和 vacuum 策略；对外只承诺聚合数据和必要审计。
- 修复时机：近期重点。
- 验证方法：用加速时钟生成 30/90/180 天 fixture，运行 retention 后核对行数、数据库大小、WAL、导出和完整性检查时间；确保关键审计仍可追溯。

### A8：生产配置闭环依赖人工修改

- 风险等级：P2
- 修复风险等级：低至中
- 影响：env example、installer、systemd、Caddy/Nginx 示例和运行时校验没有形成统一的 production profile。管理员需要手工设置 Secure Cookie、trusted proxy、令牌、密钥文件、HTTPS URL、at-rest keyring 等；遗漏时服务可能仍然“能运行”，但处于不安全或不可运维状态。
- 触发条件：新主机部署、升级、恢复或更换反向代理。
- 触发概率：高。
- 证据：deploy/noyra.env.example 中多个空值/不安全默认；scripts/install-ubuntu.sh 主要验证 service 与 /health/ready。
- 当前缓解：启动会拒绝部分明显错误；部署文档给出人工清单。
- 修复建议：增加 production profile 和一次性 preflight：密钥来源、权限、Cookie、代理、HTTPS、at-rest、备份、监听地址、日志上限、钱包开关必须逐项通过；生成机器可读验收报告。
- 修复时机：近期。
- 验证方法：用故意缺一项的 env fixture 运行 installer/preflight，确认失败信息指向具体变量；完整 profile 输出可审计的 PASS 清单。

### A9：仍支持内联 API key 回退

- 风险等级：P2（生产配置风险）
- 修复风险等级：低
- 影响：read_env_secret() 优先读 file/systemd credential，但在两者都没有时返回内联环境变量；模板仍有 NOYRA_MODEL_API_KEY、NOYRA_EMBEDDING_API_KEY、wallet signer token 等变量。API key 可能出现在 systemd 环境、进程诊断、错误收集或 shell 历史中，与“统一接入服务器密钥文件或系统凭据”的目标不一致。
- 触发条件：生产管理员把 key 直接写入 EnvironmentFile，且没有配置 file/credential。
- 触发概率：中至高。
- 当前缓解：secret 不进入 projection/diagnostic；文件读取有严格权限检查。
- 修复建议：production profile 强制 file 或 systemd credential；内联回退仅在 development profile 可用，并在启动日志明确标记 legacy；管理台导入后只保存 fingerprint 与引用，不导出明文。
- 修复时机：立即限制生产；兼容回退可在下一大版本移除。
- 验证方法：生产 profile 只设置 inline key 应启动失败；file/credential 模式成功；日志、诊断、runtime export 和备份中均无 key 明文。

### A10：Provider 健康统计只有小时聚合

- 风险等级：P2
- 修复风险等级：中
- 影响：provider_health_buckets 只保存 attempt/success/failure、延迟总和和最近成功/失败时间，不保存 timeout、4xx、5xx、鉴权失败、限流、格式错误等枚举原因。该设计节省存储和隐私，但故障诊断、自动路由调优、供应商 SLA 对账能力不足。
- 触发条件：多 provider 发生不同类型故障；管理员需要判断切换原因或恢复原因。
- 触发概率：高，provider 运行时间越长越明显。
- 当前缓解：breaker 有 cooldown/half-open；model/search 分开路由；unknown 不静默 failover。
- 修复建议：在小时聚合中增加有限集合的脱敏错误分类、状态码桶、timeout/retry 计数、p50/p95 近似延迟和配置 revision；禁止保存 URL、请求正文和 token。
- 修复时机：近期。
- 验证方法：注入 timeout、401、429、500、invalid JSON、unknown，验证统计分类、breaker 变化和管理台展示一致；确认保留周期后不会无限增长。

### A11：Provider breaker 与 unknown 语义缺专项故障注入证据

- 风险等级：P2（验证风险，暂未定性为已确认漏洞）
- 修复风险等级：中
- 影响：代码已有 cooldown、half-open、priority/weight、durable state 和 probe claim，但尚未用真实并发/故障矩阵充分证明：unknown 始终阻止自动切换；model/search 状态机一致；half-open 只有一个探针；撤销/删除 provider 后旧状态被清理；secret rotation 后不会继续使用旧配置。
- 触发条件：provider 在请求已发出后超时、返回不确定、被禁用、轮换凭据或同时被多个 worker 探测。
- 触发概率：未量化；真实 provider 故障时必然需要。
- 当前缓解：代码对 unknown 做保守处理，健康状态有 state_hash。
- 修复建议：建立故障注入 harness，覆盖 model/search、同步/异步、单 worker/多 worker、重启/恢复、配置 revocation 和 half-open 并发。
- 修复时机：自动付款和无人值守认知启用前必须完成；普通公开站点可暂缓。
- 验证方法：每个场景保存 provider 状态、logical request、attempt 数、最终路由和审计事件；证明“不确定不重试、不重复收费、不静默降级”。

### A12：钱包原因码与真实链状态映射不完整

- 风险等级：P1（自动付款场景）
- 修复风险等级：高
- 影响：execution.py 定义了 insufficient_balance、gas_too_high、nonce_conflict、confirmation_timeout、chain_reorg，但执行路径仍大量使用 signer_rejected、signer_transport_unknown、receipt_lookup_unknown、receipt_invalid、chain_receipt_failed。如果没有明确升级规则，管理员无法区分可重试、必须人工对账、应暂停付款和已确定失败的状态。
- 触发条件：RPC 超时、交易已广播但 receipt 不可查、gas 变化、nonce 被外部钱包占用、receipt 确认不足、链重组。
- 触发概率：中；测试网低，主网或高拥堵链上会显著增加。
- 当前缓解：transfer envelope、admission lease、receipt hash/confirmations 校验、confirmed receipt recheck 和 reorg 异常已存在。
- 修复建议：建立 durable wallet state machine：广播未知、确认超时、链重组、替换交易、最终失败和人工对账是不同状态；定义每个状态的重试、暂停、告警和资金账本动作；禁止把 unknown 当作失败自动重发。
- 修复时机：自动付款前必须完成。
- 验证方法：使用 fake RPC/signer 故障注入余额不足、gas 超限、nonce 冲突、广播后超时、receipt 延迟、receipt 变更和 reorg；核对支付订单、执行记录、账本、incident 和限额状态。

### A13：CAPTCHA/IP 哈希密钥每次重启变化

- 风险等级：P2
- 修复风险等级：中
- 影响：src/noyra/interaction/posts.py 使用进程随机的 _PROCESS_IP_HASH_KEY = secrets.token_bytes(32)，并据此生成 client bucket。重启后同一 IP 变成新 bucket；重启前签发的 CAPTCHA 可能无法按同一来源验证，攻击者也能在重启/升级后重新获得限额。
- 触发条件：服务重启、升级、崩溃恢复或多进程部署。
- 触发概率：中至高，生产升级必然会发生。
- 当前缓解：数据库不保存明文 IP；CAPTCHA 有 TTL、最大尝试次数和全局限速。
- 修复建议：使用服务器私有稳定 secret/systemd credential；设计 key rotation 双 key 兼容窗口和过期清理；仍只保存 HMAC bucket，不保存明文 IP。
- 修复时机：近期；公网开放投稿前优先。
- 验证方法：重启前后用同一模拟 IP 检查 bucket 一致性和限速连续性；轮换 key 时验证旧挑战在规定窗口内可验证且新请求使用新 key。

### A14：懒创建运行表未统一纳入 schema marker/迁移

- 风险等级：P2
- 修复风险等级：高
- 影响：ProviderHealthStore._ensure_tables() 与 RetentionManager.__init__() 在运行期执行 CREATE TABLE IF NOT EXISTS，而 schema marker 仍由 database.py 的正式迁移版本管理。runtime export 还动态补充这些表的 ownership。这样会出现“marker=67 但实际 schema 结构已依赖额外表”的状态，旧版本、备份、恢复、离线审计和升级兼容性难以判断。
- 触发条件：首次启动新功能、从旧版本恢复、只运行部分服务组件、离线复制数据库或使用不匹配的 runtime。
- 触发概率：中。
- 当前缓解：CREATE TABLE IF NOT EXISTS 幂等；运行时在访问 provider health/retention 时会确保表存在。
- 修复建议：将所有持久表放入正式 migration；或为 optional feature 增加独立 schema marker、feature version、DDL fingerprint 和 restore preflight。禁止依赖构造器隐式改变持久 schema。
- 修复时机：近期，下一次 schema 版本变更前完成。
- 验证方法：从 schema 67 空库、旧库、只初始化核心组件和完整组件四种路径启动；比较 sqlite_master、marker、export manifest 和 backup restore 结果。

### A15：Provider health/retention 没有独立完整性 registry 检查

- 风险等级：P2
- 修复风险等级：中高
- 影响：默认 _default_checks() 明确列出 core、mind、sleep、interaction、wallet、world、learning、cognition、model、knowledge 等 check，但没有 provider health 或 retention 专项 check。provider health 在访问时会校验 bucket/state hash，retention_runs 的 hash 与失败状态没有等价的周期扫描。Breaker 状态被篡改时可能影响路由；retention 记录损坏时可能只影响诊断而不触发 quarantine。
- 触发条件：运行表被误写、恢复不完整、迁移/人工修复造成 hash 或时间戳不一致。
- 触发概率：中；数据库损坏或手工修复时升高。
- 当前缓解：provider health route/list 路径有局部验证；SQLite foreign key/quick check 由 registry 覆盖。
- 修复建议：增加 versioned provider.health 和 operations.retention checks，定义 P0/P1/P2 处置；明确哪些派生统计损坏可重建、哪些 breaker 状态必须安全降级。
- 修复时机：近期。
- 验证方法：篡改 bucket/state/retention_runs 后运行 startup_light 与 periodic_deep，确认报告、quarantine、路由和重建策略符合设计。

### A16：部署文档与代码能力漂移

- 风险等级：P2
- 修复风险等级：低
- 影响：docs/deployment/ubuntu.md 和部分实现矩阵仍保留旧的“P1-02 生产调度/safe pause 未完成”或历史 blocker 描述；当前代码已增加 registry、watchdog、provider health、retention、管理员唤醒等能力。管理员会因此漏做真实门禁、重复执行已废弃步骤，或误以为某功能尚未可用。
- 触发条件：新部署、升级、事故恢复、审计或第三方按文档操作。
- 触发概率：高。
- 当前缓解：文档有远程验证脚本和示例命令，但没有自动版本一致性检查。
- 修复建议：每次 release 生成能力矩阵；文档标明适用 commit/schema；把关键部署步骤转为可执行 preflight；旧审计与实现矩阵加入 superseded 标记。
- 修复时机：近期。
- 验证方法：从零按文档部署并执行所有命令；对照当前 service contract、schema version、integrity registry 和管理台 API，确保不存在已废弃或缺失路径。

### A17：Release workflow 权限和外部证据仍需生产门禁

- 风险等级：P2（供应链门槛）/P3（证据缺口）
- 修复风险等级：中
- 影响：.github/workflows/release.yml 在 workflow 顶层授予 contents: write、id-token: write、attestations: write。这些权限对发布是合理的，但如果 tag protection、production environment approval、维护者权限和分支保护不足，受影响的 tag 触发后可发布签名制品。当前仓库没有真实 tag release、离线 attestation 验证和 external-gates 新鲜度证据。
- 触发条件：具备推 tag 权限的账号被滥用；Actions 依赖、生产环境审批或 release 分支策略配置不严；external-gates 文件过期或被错误生成。
- 触发概率：低至中，取决于 GitHub 仓库设置。
- 当前缓解：action SHA pin、同 SHA wallet gate、SBOM、Cosign identity verification、72 小时 external-gates 新鲜度检查。
- 修复建议：将写权限缩小到 release job；启用受保护 tag、required reviewers、trusted release environment；把离线 KMS/testnet/backup/soak 验证存入不可变 artifact，并提供独立验证脚本。
- 修复时机：下一次真实发布前。
- 验证方法：在隔离仓库演练 tag、权限拒绝、审批、artifact 下载和 Cosign/SBOM 离线验证；确认未授权 PR/branch 无法发布。

### A18：自动付款状态机尚缺多故障组合验收

- 风险等级：P1（自动付款场景；当前属于开放验收项）
- 修复风险等级：高
- 影响：钱包模块已通过 Sepolia 余额读取和一次小额转账验证，但这只能证明正常路径。尚无证据证明“自动付款开关、单笔上限、日限额、紧急暂停、wallet incident、链状态和 provider unknown”在重启、并发、超时、余额不足、reorg 组合下始终保持安全。
- 触发条件：自动付款开启且出现外部 RPC/签名器/链状态异常，或管理员在付款中途暂停、轮换密钥、重启服务。
- 触发概率：未量化；正常路径低，异常路径不可忽略。
- 当前缓解：自动化默认关闭；钱包有 admission lease、账本、原因码定义和支付执行记录。
- 修复建议：将自动付款作为独立 release gate：每笔/每日限额在同一事务内 CAS；紧急暂停阻止新 admission 并等待现有 lease；未知结果必须进入 reconciliation；重启恢复不能重复广播。
- 修复时机：自动付款前必须完成。
- 验证方法：运行 fake signer/RPC 故障矩阵和 Sepolia 小额真实 smoke；核对订单、执行尝试、ledger、incident、daily counter、pause 状态和最终余额。

## 7. 立即修复清单

1. 将生产 profile 的 Session Cookie Secure 设为默认硬门槛（A1）。
2. 禁止 production profile 的非 loopback 明文监听（A3）。
3. 生产环境禁止 inline API key，仅允许私有文件或 systemd credential（A9）。
4. 在自动付款前完成钱包原因码、未知结果、确认超时和 reorg 状态机（A12、A18）。
5. 将 provider health、retention 表正式纳入 schema/feature marker 和完整性检查（A14、A15）。
6. 公网投稿前固定 CAPTCHA 哈希密钥并验证重启连续性（A13）。

## 8. 近期稳定版清单

1. 补齐 Caddy/Nginx 的 trusted proxy 示例和安装验收（A2）。
2. 修复 retention failure persistence、cursor 语义和清理覆盖范围（A4、A5、A7）。
3. 增加 provider 错误分类聚合和 breaker 故障注入（A10、A11）。
4. 增加 production preflight 和机器可读部署验收报告（A8）。
5. 同步部署/实现文档和能力矩阵（A16）。
6. 为真实 release 配置 protected tag、production reviewers 和离线 attestation 证据（A17）。

## 9. 可暂缓项目

- protected_rows 统计（A6）：在管理台不把它当作合规证明、并明确标注“未统计”时可以暂缓。
- provider 详细错误分类中的高级延迟分位数：可以先实现枚举错误桶，再在稳定版后加入 p95/窗口化统计。
- 公开站点视觉和 SEO 的细节：不应阻塞安全 profile、钱包状态机和数据保留，但应作为下一代产品化工作流推进。

## 10. 生产启用门槛

在公开页面、管理台、认知循环、搜索和自动付款分别启用前，建议采用分层门槛：

### 10.1 公开页面

- HTTPS、HSTS、CSP、压缩和缓存策略已验证。
- CAPTCHA、稳定 IP HMAC、队列上限、磁盘余量和帖子 retention 已验证。
- 公开 projection 只返回白名单字段，并通过响应大小测试。
- 站点监控包括 4xx/5xx、投稿队列、CAPTCHA 失败率和存储占用。

### 10.2 管理台

- Cookie Secure、可信代理 CIDR、CSRF、登录失败限速和 token rotation 已验证。
- token 不出现在 URL、日志、导出、浏览器 localStorage 或普通配置导出。
- admin Session 有过期、撤销和密钥轮换演练。
- 公开站点与管理台使用独立 origin 或清晰的 CSP/权限边界。

### 10.3 认知和搜索

- model/search provider 的 file credential、priority、cooldown、unknown、revoke 和 recovery 矩阵通过。
- 预算、retry、请求 deadline 和存储 retention 有可读指标。
- 每个 provider 的健康状态可以回答：失败率、平均/近似延迟、最后成功时间、当前 breaker 状态和配置 revision。

### 10.4 自动付款

- 默认总开关为关闭，启用状态和紧急暂停在管理台显著显示。
- 单笔上限、日限额、余额/Gas/Nonce/确认/reorg 状态在同一 durable 状态机中受约束。
- unknown 不自动重发；所有外部副作用都有 logical request、attempt、receipt、ledger 和 reconciliation。
- 完成 Sepolia 正常路径和故障注入；主网前需使用独立小额钱包和独立 signer。

## 11. 下一代升级方向

### 11.1 统一 Runtime Ownership、Epoch 和 Lease

下一代核心应把“主体所有权”从单一进程锁升级为统一 fence token：

- 数据库打开、迁移、恢复、HTTP handler、provider worker、导出、云归档和钱包 admission 都必须携带当前 epoch。
- 每个 await 返回后重新验证 epoch、lifecycle、integrity quarantine 和 operation lease。
- 关机顺序固定为：停止接纳新请求 → 取消可取消任务 → 等待不可取消任务到 deadline → 写入最终状态 → 释放主体锁。
- 旧 worker 即使晚到，也只能写入带旧 epoch 的隔离记录，不能污染新主体。

### 11.2 配置和秘密进入 Control Plane

建立版本化配置对象，而不是把安全语义分散在 env、模板和管理台：

- profile：development、private-preview、production、container-internal。
- secret source：file、systemd credential、外部 KMS；production 禁止 inline。
- 每次配置修改生成 revision、actor、diff 摘要和 rollback 指针。
- 令牌轮换使用双 key overlap 窗口，Session 可以按 revision 全部撤销。
- 生成 preflight report，管理台展示“可运行”和“安全可运行”的区别。

### 11.3 Provider Router 2.0

将 model、embedding、search 统一到一个可观测但不泄密的路由协议：

- provider config revision、优先级、权重、能力标签、冷却时间、half-open probe 和 secret fingerprint。
- 错误分类只使用固定枚举；不保存请求正文、token、完整 URL 或响应。
- 支持有限窗口的 success/failure、timeout、429、5xx、auth、schema、unknown、latency percentile。
- logical request 与物理 attempt 分离；unknown 必须进入 reconcile，而不是生成新的物理 idempotency key。
- route decision、provider outcome 和 failover reason 使用相同 trace ID。

### 11.4 Wallet Payment Safety Kernel

自动付款不应只是“调用 signer”，而应是可恢复的账本状态机：

- proposed → admitted → signing → broadcast_unknown/broadcasted → confirming → confirmed/failed/reorged/reconcile_required。
- 日限额和单笔限额在 admission 事务内 CAS；紧急暂停使新 admission 立即失败。
- 每个 transaction 使用 logical payment ID；replacement/nonce bump 是同一 logical payment 的新 attempt。
- receipt 需绑定 chain ID、block hash、confirmations、effect hash；reorg 后只能回到 reconcile_required。
- 管理台用明显状态显示余额不足、Gas 高、Nonce 冲突、确认超时、链重组和暂停原因。

### 11.5 数据生命周期和可重建存储

把所有表纳入 schema registry 和 retention registry：

- 核心证据：不可删除、可归档、可验证。
- 必要审计：保留合规周期，压缩后仍可检索。
- 可重建聚合：短周期保留，允许重算。
- 临时队列：有明确上限和失败清理。
- retention 每表独立 cursor、独立结果、独立失败记录；支持 dry-run、estimate、run、verify。
- 数据库大小、WAL、备份大小、完整性扫描耗时和导出临时空间纳入统一 quota。

### 11.6 Integrity Registry 2.0

- 每个持久表拥有 owner domain、schema version、hash contract、repair strategy、retention class。
- provider health、retention、public posts 等新增表必须在同一 registry 声明。
- 启动轻检查与周期深检查使用同一版本化 manifest。
- 检查失败按 P0/P1/P2 分类：只读降级、完整 quarantine、可重建统计或人工修复。
- 报告包含数据范围、检查版本、deadline、跳过原因和可复核 evidence ID。

### 11.7 管理台和公开站点产品化

- 管理台以中文向导展示配置状态、健康状态、限额、provider 路由、钱包暂停和 retention。
- 所有危险操作显示当前状态、影响范围、幂等键和恢复方式。
- 公开站点采用静态壳、渐进加载、响应预算、图像优化、无障碍标签、OG/Twitter 预览和移动端优先布局。
- 公开投稿的 CAPTCHA、队列、审核状态和隐私说明应在前端可理解地表达。
- 前端请求引入 AbortController、request generation 和增量下载，避免迟到响应覆盖当前视图。

### 11.8 灾难恢复和发布证据

- 对每个 schema/feature 版本生成可离线验证的 manifest。
- 备份恢复演练覆盖干净主机、密钥轮换、旧版本拒绝、WAL、归档对象和 provider health。
- release 使用受保护 tag、production reviewers、最小化 job 权限和不可变外部 gate artifact。
- 将“代码测试通过”与“真实 testnet/KMS/backup/soak 通过”分开显示，不能把前者当作后者的替代。

## 12. 建议实施顺序

1. Gate 0：生产安全闭环：A1、A3、A8、A9、A13；加入 production preflight。
2. Gate 1：完整性与数据生命周期：A4、A5、A7、A14、A15；完成 schema/retention registry。
3. Gate 2：provider 路由可观测性：A10、A11；完成故障注入和 unknown reconcile。
4. Gate 3：钱包自动付款：A12、A18；完成状态机、限额 CAS、暂停、reorg 和真实 testnet gate。
5. Gate 4：发布与产品化：A2、A16、A17，随后推进公开站点移动端、SEO、无障碍和性能。
6. Gate 5：下一代架构：统一 ownership/epoch/lease、异步 worker drain、配置 control plane 和灾难恢复。

## 13. 验证证据与限制

### 已有或本次确认的证据

- 本次复核开始时工作区干净；当前 HEAD 为 `18ef7cdf2df8b5bbb4982182515efee20a1a2f9f`，其父提交 `cb04247fafb16c995e33f96724c850a216d35784` 是本次业务代码审阅基线。
- 代码静态审阅覆盖 HTTP/auth、public projection、transport SSRF、runtime export、integrity、wallet、provider health、retention、CAPTCHA、数据库 schema、部署和 CI。
- 已确认 schema 当前版本为 67，完整性 registry 版本为 `noyra-integrity-registry/v2`，`periodic_deep` 配置包含 38 个检查。
- 本次只读复核通过：`python -m compileall -q src`、`ruff check .`、`ruff format --check .` 和 `git diff --check`。
- 使用项目 `.venv` 执行管理完整性、钱包自动化、钱包执行和服务测试：89 passed、1 skipped、8 个 subtests passed；唯一跳过项是 Windows 不可移植的 POSIX token-file 权限检查。另行执行 retention、provider health 和公开视觉契约测试，分别为 4、3、2 项通过。
- 系统 Python 环境缺少 `eth_account`，因此部分依赖钱包导入的测试在收集阶段失败；这属于本机测试环境差异，不能作为代码通过证据，也不改变项目 `.venv` 的上述结果。
- 本报告没有访问生产服务器、真实 GitHub 仓库设置、真实 S3/KMS、主网、外部 signer 或多日 soak 环境。

### 未完成的外部验证

以下项目不应在 release note 中宣称“已验证”，直到有独立 artifact：

1. Sepolia/主网之外的真实 provider 故障注入和 unknown reconcile。
2. 钱包 nonce 冲突、receipt timeout、reorg、replacement 和重启恢复。
3. 干净 Ubuntu 主机上的安装、升级、回滚、LUKS/备份恢复。
4. 真实 Caddy/Nginx HTTPS、trusted proxy、Cookie Secure 和限速来源。
5. S3-compatible endpoint 的认证、key rotation、断点续传、GC 和 replica reconciliation。
6. 多日认知循环、磁盘增长、WAL、retention 和完整性扫描耗时。
7. 受保护 tag、production environment reviewer、Cosign/SBOM 离线验证。
8. 管理台在移动端、屏幕阅读器、慢网络和大数据量下的完整可用性。

## 14. 审计声明

本报告是基于当前提交、静态代码、配置、文档和已有测试证据的只读审计。风险等级表示在给定触发条件下的工程风险，不是对现实攻击频率或第三方 provider SLA 的精确统计。未列为已确认问题的开放验证项仍然是生产启用门槛；修复完成后必须重新运行针对性测试、故障注入、迁移/恢复演练和发布证据检查。
