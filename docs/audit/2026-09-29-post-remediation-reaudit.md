# Noyra 全项目只读复审报告（修复后基线）

- 审计日期：2026-09-29
- 审计基线：d2184c9a4a0547d82dd24347841339f933093cb6
- 分支：codex/wake-after-clean-restart
- 范围：src/noyra、tests、scripts、deploy、GitHub Actions、公开/管理前端及部署文档
- 边界：本轮没有连接服务器、读取生产数据库或密钥、调用真实 RPC/KMS/S3，也没有修改业务代码或远端状态。只新增本报告。

## 一、结论

本次复审重新核对了上一份报告 2026-09-29-full-readonly-audit.md 与后续修复提交。A1、A2、A3、A5、A6、A9、A13 的主要实现缺口已经关闭或明显改善；A4、A7、A10、A11、A12、A14、A15、A17、A18 仍有残留，A8 仍为部分修复，另外发现配置、存储压力和 Provider 状态校验等缺口。

当前静态复核没有确认默认 loopback 监听下的无认证远程代码执行，也没有确认绕过认证直接转账的路由。但系统仍不能标记为公网无人值守自动付款生产版。存储保护、身份锚定、链重组结算、Provider unknown/探针语义、长期数据清理和真实发布证据仍是阻断项。按既定产品要求，自动付款只受单笔和日金额上限约束；自动模式不使用白名单、人工确认、月金额上限或订单笔数上限，因此不把这些未启用限制列为缺陷。

审计工作记录显示，曾在本地项目虚拟环境执行 Provider health、搜索路由、钱包执行、release gates 和 fee admission 聚焦测试：91 passed，83.51 秒。该数字是既有审计执行记录，本次报告收尾核对未重新运行整组测试；无论如何，它不能替代真实 signer/KMS、链重组、发送后断连、备份恢复、24 小时 soak 或 GitHub environment reviewer 证据。

## 二、评级标准

| 等级 | 含义 |
|---|---|
| P0 | 远程代码执行、大规模泄露、不可逆主体损坏或无门槛高危副作用，立即隔离 |
| P1 | 认证、主体身份、完整性、资金安全或无人值守运行边界被破坏，发布前修复 |
| P2 | 明显的可靠性、容量、诊断、成本或配置风险，稳定版前修复 |
| P3 | 统计、文档或研究证据缺口，不得宣称已完成 |

修复风险分为低、中、高、极高；触发概率是工程场景的相对判断，不是攻击频率或 SLA 统计。

## 三、已确认的有效控制

- 默认监听 loopback；production profile 强制 loopback 和 Secure Session Cookie。
- Bearer 角色分层、最小长度、占位符拒绝、Session 的 HttpOnly/SameSite/CSRF、登录和请求限速已实现。
- 可信代理网段控制 X-Forwarded-*；公开投影有字段白名单。
- provider、embedding 和 wallet secret 支持受保护文件/systemd credential；生产 provider inline key 已限制。
- public post 有 CAPTCHA、IP 桶、队列、字节上限、磁盘水位和幂等约束；CAPTCHA hash key 支持持久文件。
- provider health/retention DDL 已进入 migration 68/69；迁移有未来版本拒绝、备份和 quick_check。ProviderHealthStore 与 RetentionManager 已改为缺表即失败，不再由构造器隐式建表。
- integrity watchdog、quarantine/safe-pause、wallet admission lease、单笔/日金额限额和紧急暂停已存在。
- 按既定产品要求，自动付款仅由单笔和日金额上限约束；跳过白名单、人工确认、月金额上限和订单笔数上限属于预期策略，不列为安全缺陷。
- 发布使用 SHA pin、锁文件 hash、SBOM、Cosign 步骤和同 SHA wallet gate；external gate 合同仍不完整。

## 四、问题总表

| 编号 | 问题 | 风险 | 修复风险 | 触发概率 | 建议 |
|---|---|---:|---:|---:|---|
| F01 | production profile 未强制 at-rest、keyring 和卷证明 | P1 | 中 | 中-高 | 立即 |
| F02 | 缺失 genesis hash 时静默派生未锚定身份 | P2 | 低 | 中 | 立即 |
| F03 | preflight 继承调用进程 NOYRA_* 变量 | P2 | 低 | 中 | 立即 |
| F04 | operator token file 未使用统一安全读取器 | P2 | 低-中 | 低 | 近期 |
| F05 | developer runtime export 默认打开 | P2 | 低 | 中 | 立即限制 production |
| F06 | retention cursor 不是可恢复游标且 cutoff 元数据不一致 | P3/P2 | 中 | 中 | 近期 |
| F07 | retention 错删 append-only route 表，造成清理批次失败 | P2 | 中 | 高 | 立即 |
| F08 | 多个长期增长表没有统一分类和清理策略 | P2 | 中-高 | 高 | 近期重点 |
| F09 | integrity 未校验 provider health state，缺失 state 可绕过 cooldown | P2 | 中 | 低-中 | 自动付款前 |
| F10 | retention integrity 只检查 hash 长度和 cursor 非空 | P2 | 中 | 低-中 | 近期 |
| F11 | provider health projection 未返回统计窗口 | P3 | 低-中 | 高 | 近期 |
| F12 | unknown 不进入 health/reconcile，half-open probe 未绑定请求 | P2 | 中 | 低-中 | 故障切换前 |
| F13 | 链重组后 confirmed/paid 不降级为 reconcile_required | P1 | 高 | 低-中 | 立即，阻断自动付款 |
| F14 | external-gates 缺少签名、schema、证据引用和稳定注入 | P2/P3 | 中 | 低-中 | 发布前 |
| F15 | 本地测试不能替代真实自动付款生产验收 | P1 门禁 | 高 | 未量化 | 发布前必须完成 |
| F16 | Compose 非 loopback 与 production profile 合同冲突 | P2 | 中 | 中 | 近期 |
| F17 | SQLite 文件超配额时压力门禁可能阻止所需清理 | P2 | 中-高 | 中（接近配额时高） | 立即验证并修复 |
| F18 | 可选持久表缺少独立 feature marker/DDL fingerprint（A14 残留） | P2 | 中 | 中 | 近期 |

## 五、逐项发现

### F01：production profile 未强制 at-rest、keyring 和卷证明

- 根因：ServiceSettings.at_rest_mode 默认 development；运行时只在显式配置 required 时检查 keyring/attestation。production preflight 只检查 listener、cookie、HTTPS origin、代理和反滥用 key/provider key source，没有检查 at-rest mode、keyring 可读性和卷证明。
- 证据：src/noyra/service.py:786、936-943；scripts/preflight-production.py:evaluate；scripts/install-ubuntu.sh:840-850（安装器调用上述 preflight）。
- 影响：若直接以 production profile 启动且遗漏 NOYRA_AT_REST_MODE=required，运行时可能没有启用静态加密和加密备份 keyring 强制检查；SQLite、WAL、secret 和导出文件的离线保护可能不满足生产威胁模型。Ubuntu 安装器还有独立的加密备份流程，但它不等同于服务数据卷加密证明。
- 触发条件：直接部署/恢复生产配置时遗漏 at-rest 环境变量；或使用 preflight/启动路径而没有经过安装器自身配置约束。
- 概率：中-高，手工升级和恢复最易遗漏。
- 风险：P1；修复风险：中。
- 建议：production 在模型校验层固定 required，校验 keyring 来源、权限、版本和 attestation；开发/测试才允许显式降级。立即修复。

### F02：缺失 genesis hash 时静默派生未锚定身份

- 根因：from_env 在 NOYRA_GENESIS_HASH 缺失时用固定结构（project、subject_id、origin）自动计算 hash；production preflight 未要求显式外部身份锚点。
- 证据：src/noyra/service.py:1019-1023；scripts/preflight-production.py 没有 genesis 检查；deploy/noyra.env.example:13 使用需人工替换的示例值。
- 影响：默认 hash 对同一 subject_id 是确定性的，不会因普通重启自动变化；但部署若预期使用独立签发/备份的 genesis anchor，漏配时服务会静默初始化另一锚点，后续跨主机恢复、身份核验或与外部记录对账可能不匹配。
- 触发条件：生产环境删除/遗漏 genesis hash，并且使用独立 genesis anchor 的初始化、迁移、克隆或恢复流程。
- 概率：中；风险 P2；修复风险低。
- 建议：production 要求显式固定的 64 位十六进制 hash，并与数据库现存创世记录对照；自动派生只保留 development/test。立即修复。

### F03：preflight 检查对象可能不是 env 文件

- 根因：preflight --env-file 直接 os.environ.update(values)，不会清理父进程已有但 env 文件未声明的 NOYRA_*。
- 证据：scripts/preflight-production.py 约 99-114 行；install-ubuntu.sh 通过外部进程调用该脚本。
- 影响：旧 profile、at-rest、token、provider 或 URL 可能覆盖检查意图，机器可读 PASS 不代表 systemd 实际使用的配置。
- 触发条件：shell、CI runner、sudo 或 systemd 环境继承了额外 NOYRA_*。
- 概率：中；风险 P2；修复风险低。
- 建议：载入文件前清理所有 NOYRA_*，或检测并拒绝未声明变量，同时报告来源。立即加入安装门禁。

### F04：operator token file 未复用安全凭据读取器

- 根因：service.py 的 _read_operator_token_file 只做 stat/is_file、权限位和 read_text，没有 O_NOFOLLOW、symlink/reparse、owner、nlink、稳定 inode 和打开后核对；core.credentials.read_secret_file 已有这些保护。
- 影响：能替换路径父目录或文件的进程可能诱导服务读取攻击者控制的 operator bearer，且存在 TOCTOU。
- 触发条件：配置 NOYRA_OPERATOR_TOKEN_FILE 且路径含链接、硬链接或在 stat/read 间被替换。
- 概率：低；风险 P2；修复风险低-中。
- 建议：统一调用 read_secret_file；拒绝链接/硬链接、宽权限、错误 owner 和 inode 变化。近期修复。

### F05：developer runtime export 默认打开

- 根因：developer_log_export_enabled 默认 True，from_env 缺省使用 true，deploy/noyra.env.example 也设置 true。
- 证据：src/noyra/service.py 约 780、1123-1124、7244-7246 行；部署模板约 156 行。
- 影响：当 export role token 已配置时，拥有该角色的调用方可下载 private psychology、communications、model I/O 和 external actions；默认开关扩大误配置或令牌泄露后的可访问面与存储成本。该设置本身不绕过 token 认证。
- 触发条件：production 未显式关闭开关、配置 export role token 并调用 runtime export API。
- 概率：中；风险 P2；修复风险低。
- 建议：production 默认 false；只有明确开发 profile 才能打开，preflight 和管理台应显示导出范围。立即限制 production。

### F06：retention cursor 不是可恢复游标

- 根因：run_batch 每次把表 cursor 初始化为 None；_delete_table 不使用上一失败 run 的 cursor/keyset，仍从排序键起点扫描。不同表的 cursor.cutoff 还使用统一 health cutoff，而实际表使用 2 小时或 90 天 cutoff。
- 影响：重启后会重复扫描，诊断中的 cursor 不能证明清理断点，cutoff 元数据也会误导运维。
- 触发条件：批次中断、锁竞争、空间不足后依赖 next_cursor 恢复。
- 概率：中；风险 P3（若仅诊断）至 P2（若用于运维证明）；修复风险中。
- 建议：实现每表 keyset continuation 和真实 cutoff，或删掉“cursor”术语改为结果快照；近期修复。现有事务删除本身会回滚，因此未确认有漏删。

### F07：retention 错把 append-only route 表列为可删除

- 根因：DERIVED_RUNTIME_TABLES 包含 cognitive_route_attempts/outcomes，但 database.py 为两表安装 prevent_*_delete 触发器并明确声明 append-only。
- 证据：src/noyra/core/retention.py 约 66-69、169-174、335-347 行；src/noyra/core/database.py 约 2280-2294 行。
- 影响：只要 route history 超过 retention cutoff，DELETE 触发器就抛 IntegrityError；事务回滚，其他表也可能无法清理，失败日志持续增长，容量风险被放大。
- 触发条件：启用认知路由并产生超过保留期的 route attempts/outcomes。
- 概率：高；风险 P1/P2；修复风险中。
- 建议：立即从删除清单移除，或设计不可变归档/压缩而非 DELETE；增加旧 route fixture，确认清理其他表不被阻断。

### F08：长期增长表缺少统一分类和清理策略

- 根因：当前清单覆盖少数 provider/search/cognitive route 派生表，但 cognitive_route_decisions、model_calls/attempts、research runs、action-deliberation runs、behavior logs 等 append-only 表没有统一 retention registry。
- 影响：长期运行导致 SQLite、WAL、备份、导出和 integrity 扫描持续增长，最终可能耗尽空间并阻断主体写入。
- 触发条件：启用模型、搜索、研究或行为日志并运行数周/月。
- 概率：高；风险 P2；修复风险中-高。
- 建议：逐表标注 core_evidence、required_audit、rebuildable_aggregate、temporary_queue，定义 cutoff、归档、保护谓词、WAL checkpoint 和水位；以 30/90/180 日加速 soak 验证。

### F09：integrity 未校验 provider health state，缺失 state 可绕过 cooldown

- 根因：_check_provider_health 只查询并重算 provider_health_buckets 的 hash，不遍历或校验 provider_health_state，也不核对 bucket 和 state 的 provider 身份集合。route_available 在找不到 state 行时直接返回 True。
- 证据：src/noyra/core/integrity.py:_check_provider_health；src/noyra/core/provider_health.py:_verify_state、route_available。state hash 损坏时 ProviderHealthStore 自身会抛 IntegrityError，但 watchdog 当前不会因此报告该损坏；删除 state 行时 watchdog 也无法发现，路由会把该 Provider 当作可用。
- 影响：丢失 cooldown/breaker 状态可让已积累失败的 Provider 被重新选择；单纯篡改 hash 会在 ProviderHealthStore 访问时失败关闭，但 integrity health 仍可能显示 ok，降低告警和恢复诊断的可信度。
- 触发条件：恢复、人工修复、部分损坏或 state 行删除后只运行 integrity watchdog。
- 概率：低-中；风险 P2；修复风险中。
- 建议：遍历并重算 state hash，双向核对 bucket/state provider identity 与状态时间/probe lease；当存在 bucket 失败数据但 state 缺失时保守熔断并要求显式恢复。自动付款依赖 Provider 自动路由前完成。

### F10：retention integrity 语义校验过弱

- 根因：retention check 只检查 state_hash 长度为 64 和 next_cursor 非空，不重算 hash，也不解析 deleted_by_table_json、cursor、cutoff、failure fields。
- 影响：任意 64 位值或任意 JSON 都可能通过，损坏的断点和失败记录会被误当作可靠证据。
- 触发条件：迁移、恢复、人工修复或部分写入后运行 watchdog。
- 概率：低-中；风险 P2；修复风险中。
- 建议：复用写入端 canonical payload 重算 hash，校验每表 cursor schema、时间、排序键、失败状态和计数。近期修复。

### F11：provider projection 未返回统计窗口

- 根因：list_projection 查询并聚合 provider 的全部保留 buckets，仅依赖 retention 删除旧数据，没有 window_start/window_end 或 bucket_count。
- 影响：failure rate、平均延迟和 p50/p95 反映当前留存周期内的累计数据，但 API 不返回实际统计窗口，运维难以解释数字的时效性；随着 retention 天数增大，读取和聚合成本也会上升。当前默认 health retention 是 30 天，因此不等同于无界统计。
- 触发条件：管理台查看 provider 指标，尤其是调整 health retention 后比较或据此决策。
- 概率：高；风险 P3；修复风险低-中。
- 建议：响应包含 window_start/window_end/bucket_count，或按明确的短窗口同时返回近期和留存全量趋势。近期修复。

### F12：unknown outcome 和 half-open probe 合同不完整

- 根因：model resources.py 和 research/search.py 在 outcome_unknown 时跳过 provider health aggregate；route_available 生成 probe_token，但 record_attempt 没有 token 参数，会清除当前 probe lease。
- 影响：模型调用/搜索动作仍将 unknown 写入各自的业务记录，但 provider health aggregate 不反映这类结果，故障率可能被低估；一个无关的旧请求完成时可能清除当前 half-open token，使额外恢复请求获准。
- 触发条件：timeout-after-send、连接断开、cooldown 到期且存在旧长请求/并发请求。
- 概率：低-中；风险 P2；修复风险中。
- 建议：unknown 持久化为独立 reconcile outcome，不自动当作可重试失败；以 token/CAS 绑定 probe completion，并进行并发故障注入。生产故障切换前完成。

### F13：链重组后 confirmed/paid 不降级

- 根因：verify_confirmed_receipt 在回执消失、区块 hash/effect/confirmations 改变时只抛 WalletChainReorganizationError；workflow 只写 incident，没有更新 execution/order/ledger durable 状态。
- 证据：src/noyra/wallet/execution.py:verify_confirmed_receipt；src/noyra/wallet/workflow.py 的 WalletChainReorganizationError 分支；钱包订单和 ledger 的 append-only 触发器。
- 影响：本地仍显示 confirmed/paid，但链上交易已不 canonical；运营界面和账务投影可能与链上事实不一致，后续对账、退款或重试可能误判。当前流程会记录 chain_reorganization incident，因此问题是已确认状态与链上证据缺少闭环协调，不等于已确认存在自动重复转账路径。
- 触发条件：已确认交易发生链重组或 receipt evidence 改变。
- 概率：低-中，自动付款影响高；风险 P1；修复风险高。
- 建议：增加显式 reconcile_required/reorg_detected 投影与不可变补偿审计/ledger 记录，在同一事务打开 incident 并冻结同一 logical payment 的进一步动作；不要原地改写 append-only settlement evidence。只有观察者重新确认 canonical receipt 或管理员完成可审计对账后才能解除。自动付款前修复。

### F14：external-gates 发布证据合同不完整

- 根因：release.yml 在下载的 wallet gate artifact 根目录中强制寻找 external-gates.json，但当前 quality-gate 只运行 audit-wallet-stage4b.py 并上传其输出；仓库没有生成该外部证据文件的步骤或可信上传/注入接口。即使文件被放入，校验也只检查 commit_sha、status、reviewed_at 新鲜度，没有签名、版本 schema、固定 gate IDs、evidence_refs 或 reviewer 身份验证。
- 证据：.github/workflows/release.yml 的 artifact 上传/下载及 external-gates 校验；scripts/audit-wallet-stage4b.py 只写 wallet gate run/metrics；scripts/build-release-evidence.py 只声明 external_gates_required 和文件名。
- 影响：按现有 workflow 执行 tag 发布时，下载的 artifact 不含此文件，发布 job 会在 evidence check 失败；若手工提供文件，格式错误或未签名的记录仍可能只凭三个字段通过，无法验证 testnet/KMS/backup/soak 的证据真实性。
- 触发条件：任何 tag release；或通过人工路径注入 external-gates.json。
- 概率：高（现有自动 tag release 路径下必然缺文件）；风险 P2/P3；修复风险中。
- 建议：定义版本化 JSON schema、固定 gate IDs、执行人与独立 reviewer、时间窗口、证据引用、失败原因和禁止秘密字段；提供经认证的 evidence artifact 上传接口，并用 pinned verifier 校验签名及 commit。发布前完成。

### F15：本地绿灯不能证明真实自动付款生产门禁

- 根因：fake signer/RPC 单测只能证明确定性状态机；真实验收仍要求独立 signer/KMS、第二观察者 receipt、timeout/response-loss/duplicate/restart、24 小时 soak、backup restore/rollback 和 operator approval。当前基线没有同 SHA external-gates artifact。
- 影响：真实链、RPC、signer、磁盘压力或恢复流程仍可能产生资金、账务和可用性问题。
- 触发条件：把本地测试通过误当成生产启用依据并开启真实 signer/automation。
- 概率：未量化；风险 P1 门禁；修复风险高。
- 建议：external-gates 完整、同 SHA、72 小时内且独立复核前，保持 wallet automation 和 auto publish 关闭。此项必须由目标环境完成。

### F16：Compose 与 production listener 合同冲突

- 根因：docker-compose.yml 为容器网络可达性设置 NOYRA_HOST=0.0.0.0 和 NOYRA_ALLOW_INSECURE_NON_LOOPBACK=true；但唯一 profile 枚举没有 container-internal，且 production profile 无条件拒绝非 loopback。Compose 文档说明 host port 绑定 loopback，未建立代码可验证的容器网络信任合同。
- 影响：显式设置 NOYRA_PROFILE=production 会在启动校验失败；省略 profile 时服务默认 development，虽 Compose host port 限定 loopback，但该运行不享有 production profile 的安全强制条件，运行模式与运维预期不一致。
- 触发条件：按照 docker-compose.yml 部署并将配置设为 production，或误以为未设置 profile 就是生产安全模式。
- 概率：中；风险 P2；修复风险中。
- 建议：将容器内部监听纳入可验证的部署 profile/前置检查，绑定 host loopback 发布和容器隔离证据；或维持服务 loopback 并调整网络架构。Compose 模板、部署文档和 preflight 应共同验证同一配置矩阵。近期修复。

### F17：SQLite 文件超配额时压力门禁可能阻止所需清理

- 根因：StorageUsage.over_quota 按 subject_bytes（物理占用）判断超额，database_reclaimable_bytes 只用于 effective_subject_bytes 报告；超额时 StorageLifecycleManager.maintain 跳过归档、压缩和快照整理，service tick 在 cognition_allowed=false 时提前返回，不进入后续 retention。代码库未发现用于回收 SQLite freelist 的 VACUUM/增量 vacuum 路径。
- 证据：src/noyra/core/storage.py:197-199、240-248；src/noyra/core/storage_lifecycle.py:84-105、228-230；src/noyra/service.py:8690-8703。
- 影响：当数据库物理文件超过 subject quota、但其中存在可复用 freelist 页面时，effective_subject_bytes 可能已低于额度，over_quota 仍报告 subject 超限；压力分支阻止 retention/压缩等维护，服务可能持续处于 storage_pressure，且仅靠 SQLite 内部复用页面不会降低文件物理尺寸。这个路径可能让系统无法自恢复，需要人工扩额或离线维护。
- 触发条件：subject quota 被数据库物理尺寸压过，尤其是大量已删除/可复用 SQLite 页面构成超额，而缓存/导出清理不足以消除物理超额。
- 概率：中；接近 quota 且数据库增长/删除频繁时偏高；风险 P2；修复风险中-高（涉及 SQLite 锁、空间和在线维护安全）。
- 建议：立即增加只读/临时数据库 fixture 测试压力判定及恢复路径；设计受进程锁和可用空间约束的安全回收策略，或让 quota 判定对可回收页面采取明确、保守且有上限的计算，同时确保 retention 在受控压力下能删除派生数据。不得在服务并发访问时贸然执行 VACUUM。

### F18（A14 残留）：可选持久表没有独立 feature marker

- 这不是 ProviderHealthStore 或 RetentionManager 的旧式懒创建问题：两者现在检查正式 migration 68/69 创建的表，缺表即失败。残留位于 `Database._ensure_optional_features()`，其中 `secret_cleanup_queue`、`secret_file_intents` 及相关触发器仍通过启动时幂等 DDL 安装，而 `schema_meta` 只有统一的 `schema_version=69`，没有逐 feature 的版本、DDL fingerprint 或安装来源。
- 根因：历史上为兼容旧库而把加性修复放在统一 optional-feature 事务中，未同步建立可供离线工具读取的 feature registry。
- 影响：在线启动会检查 required tables，因此当前运行时不会因为 marker=69 而静默使用缺表；但只做离线复制、备份恢复、导出审计或使用不启动完整服务的工具时，仅凭 schema marker 无法证明这些可选持久结构及触发器已经安装到预期版本，升级/回滚兼容性和证据链仍不完整。
- 触发条件：离线恢复、只运行迁移子集、跨版本复制数据库，或第三方工具仅按 `schema_meta.schema_version` 判定结构兼容性。
- 触发概率：中；风险 P2（在自动恢复/离线审计场景），修复风险中。建议近期纳入统一 feature registry；在完成前，备份恢复和离线审计必须运行完整 `Database.initialize()` 与 required-table/trigger 检查。

## 六、历史 A1-A18 状态

| 项目 | 状态 | 复核说明 |
|---|---|---|
| A1 Cookie Secure | 主要修复 | production 强制 Secure，仍需真实 HTTPS 代理验收 |
| A2 trusted proxy | 主要修复 | 示例已给 CIDR，目标主机仍需核对 source address |
| A3 明文 listener | 主要修复 | production 拒绝非 loopback，Compose 合同见 F16 |
| A4 cursor | 残留为语义/可观测性问题 | 失败可记录，但不从 cursor 恢复 |
| A5 failure persistence | 主要修复 | 独立失败记录已实现，SQLITE_FULL/locked 属外部门槛 |
| A6 protected rows | 按设计调整 | 返回 None 和 reason，不再伪称 0 |
| A7 retention coverage | 残留且含 F07 | append-only route 表会使清理失败 |
| A8 production preflight | 部分修复 | F01-F03 仍未覆盖 |
| A9 provider inline key | 主要修复 | operator token file 仍有 F04 |
| A10 health window | 残留 | 有百分位但无明确窗口 |
| A11 breaker/unknown | 残留 | F12 和真实故障注入尚未闭环 |
| A12 wallet mapping | 残留 | reorg durable rollback 缺失 |
| A13 CAPTCHA key | 主要修复 | 持久 key 已支持，权限要外部验收 |
| A14 migration | 部分关闭 | provider/retention DDL 已迁移且缺表会失败；但 `_ensure_optional_features()` 仍在 schema marker 之外以幂等 DDL 安装 secret cleanup/intents 等持久表，尚无统一 feature version/DDL fingerprint，离线备份与恢复工具无法只凭 marker 判断全部运行时表结构 |
| A15 integrity | 残留 | provider state、retention canonical semantics 未充分校验 |
| A16 deployment docs | 主要修复 | Compose 与 production 仍有 F16 |
| A17 release evidence | 残留 | external evidence 校验不完整 |
| A18 fault matrix | 部分关闭 | F13 的链重组状态闭环和 F15 的真实环境门禁仍阻断无人值守自动付款 |

## 七、修复优先级

### 立即修复和发布阻断

1. F01、F02、F03、F05：先固定身份、存储和配置检查边界。
2. F13：补上链重组后的对账状态与 append-only 补偿记录；完成前不得开无人值守自动付款。
3. F07、F17：移除错误 retention 删除项，验证并修复存储压力下的恢复路径。
4. F15：external-gates 完成前保持真实钱包自动化关闭。

### 近期稳定版

F04、F06、F08、F09、F10、F11、F12、F14、F16、F18。每项都应先写根因失败测试，再实现最小修复，随后运行模块回归、compileall、Ruff、format、mypy、diff check 和加速 soak。

### 可暂缓但有边界

protected_rows 精确统计和高级历史趋势可暂缓；API 必须继续返回 None 与原因，不能显示成合规证明。公开站点 SEO、视觉和高级 benchmark 不阻断只读预览，但不能替代安全门禁。

## 八、下一代升级方向

1. 建立统一持久化 registry：每张表共享 owner、schema/feature version、DDL fingerprint、integrity check、retention class 和 repair strategy，供迁移、导出、备份和 watchdog 共用。
2. 统一 logical operation identity：模型、搜索、支付和 provider attempt 使用稳定 operation ID；unknown、replacement、probe 和 reconcile 用 lease/CAS 绑定，避免重放产生错误统计或副作用。
3. 把支付 admission、余额/Gas、nonce、receipt、reorg、退款、暂停和双重记账收敛为可证明状态机；unknown 永远进入调查，不隐式重试。
4. 把 production profile 变成单一配置合同：安装、升级、systemd、Compose、代理和 CI 共用同一个 preflight 验证器。
5. 数据最小化与有限窗口观测：原始 model I/O、私密心理和网络响应默认不落盘；聚合数据有明确窗口，必要证据进入加密归档。
6. external-gates 使用签名 JSON/DSSE 或同等格式，证据引用可离线验证，reviewer 身份与提交 SHA 绑定。
7. 将 SQLite 锁竞争、ENOSPC、provider/RPC/KMS/S3 故障、重启、链重组、代理伪造、Windows/Ubuntu at-rest 和 24/72 小时 soak 纳入可重复 release gate。

## 九、审计声明

本报告没有声称完成真实 Ubuntu/systemd、公网 HTTPS、BitLocker/LUKS、KMS/独立 signer、Sepolia 链重组、S3、备份恢复、GitHub environment reviewer 或多日 soak。风险等级是基于当前代码和配置合同的工程判断，不是渗透测试结论。审计结束时工作区除本报告外保持干净，未修改服务器、钱包、数据库、密钥或远端仓库。
