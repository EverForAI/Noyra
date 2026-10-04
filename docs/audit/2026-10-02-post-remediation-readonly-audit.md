# Noyra 修复后全项目只读审计报告

- 审计日期：2026-10-02（Asia/Shanghai）
- 审计基线：`344ca1d`（修复提交链：`9091fd2`、`8952c46`、`253db90`、`ad7b476`、`2f00035`、`344ca1d`，并包含此前 `2bf9f92`、`b3de3fa`、`dfffb9d`、`4435cd8`）
- 分支：`codex/wake-after-clean-restart`
- 当前 schema：`77`（`src/noyra/core/database.py:291`）
- 审计边界：只读检查代码、测试、脚本、部署单元、OpenAPI 和已有文档；本轮只新增本报告，不修改业务代码、配置、数据库、服务器或远程仓库。

## 1. 结论

当前版本的基础运行时、认证、静态加密、完整性 watchdog、provider 健康、钱包限额、公开投影和迁移控制面已经具备较多安全控制。生产 operator token 和模型组密钥的生产门禁、schema 77 的 runtime export ownership graph、provider unknown/half-open 统计以及 retention 表 inventory 已经有对应实现和测试，旧审计中这些已经修复的结论不再重复列为开放问题。

按审计计划已完成 M01–M08 的代码级修复，并为每个模块建立了回归测试和独立提交。迁移控制面现在通过 coordinator 执行 cutover，runner/target agent 会实际校验并恢复 artifact，emergency recovery 绑定已登记且内容哈希验证的备份，retention cursor 绑定本轮 cutoff，cutover 的 task 与 epoch 在同一事务内完成，identity 文件按 descriptor/owner/link/权限合同读取，Windows package gate 会对不完整 PowerShell runtime 失败并给出稳定诊断，外部 release evidence verifier 要求同 SHA、签名、独立 reviewer 和全部 gate。

M09 仍是发布门禁，而不是可由本地代码测试替代的问题：真实 Ubuntu/systemd、加密盘、备份恢复、两主机迁移 fence、独立 signer/KMS、真实 RPC reorg/nonce、HTTPS/proxy 和 24/72 小时 soak 的签名 evidence 尚未取得。因此不能宣称自动付款、无人值守迁移或长期公网部署已经生产验收；在 external-gates artifact 通过前，migration 默认关闭，自动付款和 policy-auto migration 必须保持关闭或人工审批。

## 2. 评级定义

### 问题风险

| 等级 | 含义 |
|---|---|
| P0 | 远程代码执行、大规模机密泄露、不可逆主体损坏或无门槛高危副作用 |
| P1 | 认证、资金安全、主体完整性、单活约束或关键能力发布边界可能被破坏 |
| P2 | 明显的可靠性、容量、状态一致性、配置或运维风险，有明确绕行方案 |
| P3 | 可观测性、文档、兼容性或外部证据缺口，不直接突破安全边界 |

### 修复风险

低表示局部校验或文档变更；中表示涉及 API、数据库状态机或并发；高表示涉及钱包、密钥、完整性、跨主机恢复或多事务切换；极高表示需要统一重构存储、发布和所有权模型。

### 触发概率

概率是基于代码路径、默认配置和运维场景的工程判断，不是攻击频率、第三方 SLA 或资金损失统计。

## 3. 已确认有效的控制

- production 的 operator token 使用 `read_env_secret(... allow_inline=False)`，并且 `scripts/preflight-production.py:170-178` 拒绝 inline operator token。
- `scripts/preflight-production.py:28-71` 解析 model group JSON，production 拒绝非空 inline `api_keys`；model resource 也在 `src/noyra/model/resources.py:3761-3766` 做运行时拒绝。
- `src/noyra/core/runtime_export.py:496-565` 已覆盖 schema 73-77；`tests/test_runtime_export_schema75.py` 和 runtime export focused suite 验证当前图存在且迁移表可分类。
- `ProviderHealthStore` 已使用 `RoutePermit`、独立 `unknown` 计数、probe token 和 state/bucket hash；model route 会把 unknown 隔离并停止普通 failover（`src/noyra/core/provider_health.py:264-337,401-440`；`src/noyra/model/resources.py:2782-2829,2895-2907`）。
- retention 会对 `sqlite_master` inventory 与 durable baseline 做差集检查（`src/noyra/core/retention.py:203-239`；`src/noyra/core/integrity.py:1607-1626`），storage pressure tick 仍会运行 bounded retention（`src/noyra/service.py:9538-9543`）。
- migration 默认关闭、manual approval 默认路径、目标注册与 Ed25519 attestation、加密传输、epoch 唯一性和 operator 审计均存在；target agent 默认 loopback、`noyra` 用户、`NoNewPrivileges`、`UMask=0077` 和受限写路径（`deploy/systemd/noyra-migration-agent.service`）。
- local wallet approval 已使用数据库审计事件消费，而非仅依赖进程内 set（`src/noyra/migration/wallet.py:177-220`）。
- 钱包自动付款已有余额不足、Gas、nonce、确认超时、广播未知、链重组等原因码和日限额/单笔限额/紧急暂停控制；真实链和 signer 证据仍需外部门禁。

## 4. 问题总表

| 编号 | 问题 | 风险 | 修复风险 | 触发概率 | 状态 | 建议 |
|---|---|---:|---:|---:|---|
| M01 | 管理台 cutover API 永远拒绝执行 | P1 | 高 | 高（每次真实 cutover） | 已修复（`2bf9f92`） | 已通过 coordinator 接入；仍需真实两主机 evidence |
| M02 | root migration runner 只验证 proof，不执行迁移动作 | P1 | 高 | 高（启用 runner 时） | 已修复（`b3de3fa`） | 已执行本地 fence/restore/health；真实跨主机仍需 gate |
| M03 | target agent restore/health 只处理 manifest，未恢复真实数据 | P1 | 高 | 高（使用 agent 时） | 已修复（`dfffb9d`） | 已接入 artifact hash、SQLite quick_check、subject/host binding |
| M04 | emergency recovery 不验证真实备份登记，source epoch 未绑定当前源 | P1 | 高 | 低至中 | 已修复（`4435cd8`） | 已登记备份 registry、内容校验和 runtime epoch 绑定 |
| M05 | retention cursor 未绑定本轮实际 cutoff | P2 | 中 | 中 | 已修复（`9091fd2`） | 已覆盖时间推进、旧记录和 cutoff 变化 |
| M06 | cutover task 与 epoch completion 分属不同事务 | P2 | 高 | 低至中 | 已修复（`8952c46`） | 已通过共享事务 API 合并写入并保持幂等 |
| M07 | migration identity file 的 owner、hardlink 和 TOCTOU 合同不足 | P2 | 中 | 低 | 已修复（`253db90`） | 已使用 no-follow descriptor、fstat、owner/mode/nlink 校验 |
| M08 | Windows package gate 在当前审计环境失败 | P2/P3 发布门禁 | 中 | 高（当前 CI/本机） | 已修复代码门禁（`ad7b476`） | 当前不完整 runtime 仍应失败；需标准 Windows runner 证据 |
| M09 | 真实环境发布证据仍缺失 | P1 发布门禁 | 高 | 未量化 | 未完成，保持阻断 | 生成同 SHA、签名且独立复核的 external-gates artifact |

## 5. 详细发现

### M01：管理台 cutover API 永远拒绝执行（已修复）

- **状态**：已修复（`2bf9f92`）。HTTP cutover 现在调用 coordinator 的 prepare/commit 路径，错误仍 fail closed。
- **风险等级**：P1，关键能力发布阻断。
- **修复风险等级**：高。
- **原根因与修复**：旧实现认证后固定返回 409，未调用 coordinator。`2bf9f92` 现在解析有界 proof，调用 `owner.migration_cutover.prepare()`/`commit()`，并把 task/epoch 状态和稳定错误码返回给管理台。
- **影响**：OpenAPI `docs/api/openapi.yaml:464-480` 宣称存在可调用的 cutover，但管理台无法完成任何切换。operator 看见的是稳定拒绝，而不是一个执行中的迁移状态。
- **触发条件**：任何已批准、目标已恢复并尝试通过管理台切换的任务。
- **触发概率**：高；只要使用真实迁移就必然触发。
- **证据**：`tests/test_migration_service.py` 覆盖 authenticated coordinator path、proof 校验和 rollback；`tests/test_migration_cutover.py` 覆盖重复提交、旧 epoch 和失败关闭。
- **当前缓解**：默认关闭迁移、manual approval、domain 层 proof 校验、admission lease 和 epoch 检查防止误切换。
- **建议**：保持现有 bounded proof、幂等和 task/subject/policy/epoch 绑定；真实两主机 executor 与路由切换仍由 M09 external gate 验证。
- **时机**：代码修复已完成；真实两主机 cutover/rollback 仍由 M09 门禁决定是否可发布。
- **验证方法**：HTTP authenticated success、缺字段、签名错误、重复 prepare/commit、并发 commit、旧 policy、旧 epoch 和 rollback fault injection。

### M02：root migration runner 只验证 proof，不执行真实迁移（已修复）

- **状态**：已修复（`b3de3fa`）。runner 现在执行固定数据根下的 artifact digest/size 校验、source fence、SQLite restore 和 health 检查。
- **风险等级**：P1，数据连续性和单活发布阻断。
- **修复风险等级**：高。
- **原根因与修复**：旧 runner 只把调用者提交的报告升级为 completed。`b3de3fa` 新增 `MigrationExecutor`，在固定数据根下实际执行 artifact digest/size 校验、source epoch fence、SQLite restore/quick_check 和 health/subject 校验，报告由观察结果生成。
- **影响**：可以产生形式正确的“completed”状态，却没有证明目标真的拥有 Noyra 数据，也没有证明旧源已停止写入。依赖此状态的上层 cutover 可能把证明当成事实。
- **触发条件**：安装并执行 `scripts/noyra-migration-runner.sh` 的 restore/health/fence 动作。
- **触发概率**：高；每次 runner 调用都会走验证路径。
- **证据**：`tests/test_migration_runner.py` 覆盖真实 artifact、fence、restore、health、坏 hash 和失败清理；加密备份路径要求显式 keyring，避免伪造成功。
- **当前缓解**：输入字段有界、拒绝 secret 字段、路径和 request id 受限、systemd runner 使用 root/`UMask=0077`；这些保护的是执行边界，不是真实迁移完成度。
- **建议**：继续把 runner 作为唯一 privileged orchestration boundary；跨主机复制、真实加密备份 keyring、source admission 和 rollback 由 M09 环境演练验证。
- **时机**：代码修复已完成；跨主机和真实加密备份仍需 M09 external gate。
- **验证方法**：两主机 disposable encrypted volume 演练，注入 source restart、copy interruption、checksum mismatch、restore failure、health timeout、fence failure 和 rollback，检查旧 source mutation 被拒绝。

### M03：target agent restore/health 没有恢复真实数据（已修复）

- **状态**：已修复（`dfffb9d`）。agent 接收实际 artifact bytes，验证 digest/size，并通过 SQLite 或现有 backup manager restore 后执行 quick_check、subject 和 host binding。
- **风险等级**：P1。
- **修复风险等级**：高。
- **原根因与修复**：旧 agent 只持久化 manifest 且 host binding 常量为真。`dfffb9d` 现在接收实际 artifact bytes，验证 digest/size，按 sqlite 或现有 backup manager 恢复到隔离目录，并执行 quick_check、subject identity 和 restore-root host binding。
- **影响**：收到 manifest 不等于收到加密备份；health report 也不证明 service、database、integrity watchdog 或 subject identity 已在目标运行。
- **触发条件**：使用 agent 的 receive → restore → health 流程。
- **触发概率**：高。
- **证据**：`tests/test_migration_agent.py` 覆盖 artifact 接收、坏 digest、restore、quick_check、subject mismatch 和 host binding；agent CLI 测试覆盖权限和认证边界。
- **当前缓解**：HMAC session、nonce replay store、incoming 文件/字节配额、TTL 清理、symlink 拒绝和加密存储要求降低了攻击面。
- **建议**：保持 chunk/最终 hash、隔离 restore root 和实际 health 检查；真实加密备份、schema/genesis/event-chain 和服务启动验证由 M09 gate 覆盖。
- **时机**：代码修复已完成；真实双主机 restore/health 仍需 M09 external gate。
- **验证方法**：真实 artifact、断点续传、坏 chunk、截断文件、旧 schema、错误 subject、服务启动失败和 health probe 超时矩阵。

### M04：emergency recovery 不验证真实备份登记，source epoch 未绑定当前源（已修复）

- **状态**：已修复（`4435cd8`）。新增 durable backup registry，恢复请求必须匹配当前 subject、schema、genesis、key metadata、文件大小和 hash，并绑定当前 runtime epoch。
- **风险等级**：P1，主体连续性边界。
- **修复风险等级**：高。
- **原根因与修复**：旧实现只校验 backup id 格式且现场生成 recovery epoch。`4435cd8` 新增 durable `migration_backup_registry`，登记时读取并哈希真实文件；恢复时校验 subject、genesis、schema、key generation、size/hash/state hash，并从 runtime state 绑定当前 source epoch，再获取 epoch lease。
- **影响**：在 emergency recovery 被打开且目标签名可用时，未知 backup ID 也可创建 recovery task/epoch；签名证明的是请求字段一致性，不是备份对象存在或源主机确实失效。
- **触发条件**：启用 `emergency_recovery`，提交格式正确且目标签名有效的请求。
- **触发概率**：低至中，取决于是否启用应急模式以及 operator/target 认证材料是否被滥用。
- **证据**：`tests/test_migration_recovery.py` 与 `tests/test_migration_end_to_end.py` 覆盖 registry 登记、未知/篡改 backup 拒绝、subject/schema/hash mismatch、当前 runtime epoch 和幂等恢复。
- **当前缓解**：emergency recovery 独立开关、allowlisted active attested target、目标签名、task idempotency、active epoch uniqueness 和审计已存在。
- **建议**：继续要求 registry 记录与真实备份文件保持一致；source failure detector、跨主机目标证明和 emergency mode 的实际启用仍需 M09 external gate。
- **时机**：代码修复已完成；emergency recovery 仍须在真实备份/目标证据通过后才可启用。
- **验证方法**：未知 backup、跨 subject backup、hash/epoch mismatch、过期证据、源仍健康、重复 recovery、目标撤销和审计回放测试。

### M05：retention cursor 未绑定本轮实际 cutoff，可能跳过后来变旧的记录（已修复）

- **状态**：已修复（`9091fd2`）。cursor provenance 包含本轮 per-table cutoff、sort-key version 和 data epoch；cutoff 变化会丢弃旧 cursor。
- **风险等级**：P2。
- **修复风险等级**：中。
- **原根因与修复**：旧 cursor provenance 未绑定本轮 cutoff。`9091fd2` 将 per-table cutoff、sort-key version 和 data epoch 纳入 provenance；cutoff 或 epoch 变化时拒绝复用旧 cursor，并始终把当前 cutoff 写入本轮 payload。
- **影响**：T1 批次保存排序键 K 后，T2 cutoff 前移；新进入旧数据范围但排序键不大于 K 的记录可能被 `>` K 排除，长期漏删，容量和备份增长。
- **触发条件**：批次受限、时间推进、插入历史时间记录、导入/backfill 或恢复后继续清理。
- **触发概率**：中；正常顺序写入较少，恢复和 backfill 场景明显增加。
- **证据**：`tests/test_retention.py` 覆盖时间推进、backfill 旧记录、cutoff 变化和 cursor reset。
- **当前缓解**：cursor 结构、schema/data epoch 和 registry inventory 有校验，删除事务失败会回滚。
- **建议**：保持 cutoff/data epoch 绑定，并在恢复/backfill soak 中监测 bounded retention 是否最终处理全部过期记录。
- **时机**：代码修复已完成；应在发布前继续执行恢复/backfill 场景的 soak。
- **验证方法**：先小批量运行，再推进时钟、插入排序键较小的旧记录、改变保留周期、重启和恢复备份，确认最终所有目标记录均被处理。

### M06：cutover task 状态和 epoch completion 不在同一事务（已修复）

- **状态**：已修复（`8952c46`）。task CAS、epoch CAS 和审计写入现在共用一个事务连接，并通过 admission lease 保护。
- **风险等级**：P2；发生在切换边界时可升级为 P1。
- **修复风险等级**：高。
- **原根因与修复**：旧实现分别提交 task 和 epoch 两个事务。`8952c46` 增加 `*_in_transaction` API，coordinator 在 admission lease 保护下用同一连接完成 task CAS、epoch CAS 和 audit append。
- **影响**：若第二个事务因锁、完整性、进程中断或状态竞争失败，数据库可能留下 task=`committed` 但 epoch 仍 active，导致管理台、单活 fence 和恢复流程看到不一致的终态。
- **触发条件**：commit 过程中进程退出、SQLite busy/IO 错误、epoch 被并发 revoke 或完整性检查失败。
- **触发概率**：低至中；长时间运行和故障注入时显著。
- **证据**：`tests/test_migration_end_to_end.py` 覆盖共享事务成功、重复 commit、并发 revoke 和注入失败后的可重试状态。
- **当前缓解**：每一步都有 `assert_current()`、CAS 状态转换、admission lease 和 append-only audit。
- **建议**：保持共享事务边界；真实 SQLite busy、进程中断和重启恢复仍需 M09 故障注入 evidence。
- **时机**：代码修复已完成；仍需真实 SQLite busy/进程中断故障注入作为 M09 的一部分。
- **验证方法**：在 task transition 成功后、epoch completion 前注入异常，重启后运行 reconciler，验证唯一合法终态和幂等重试。

### M07：migration identity file 防护未覆盖 owner、hardlink 和 stat/open TOCTOU（已修复）

- **状态**：已修复（`253db90`）。identity 使用 `O_NOFOLLOW|O_CLOEXEC` descriptor 读取，fstat 校验 regular/owner/mode/nlink，解析内容来自同一打开句柄。
- **风险等级**：P2。
- **修复风险等级**：中。
- **原根因与修复**：旧实现先 stat 再按路径读取，未覆盖 hardlink/owner/TOCTOU。`253db90` 使用 `O_NOFOLLOW|O_CLOEXEC` 打开 descriptor，fstat 校验 regular、owner、private mode、nlink=1，并从同一 descriptor 读取和解析 JSON。
- **影响**：若攻击者能修改 identity 文件所在目录或替换文件，agent 可能加载错误 target identity/private signing key；正常 systemd 安装的 root-owned 目录会显著降低该概率，但代码合同本身未完整表达。
- **触发条件**：identity 路径目录权限错误、硬链接替换、并发维护或手工部署 agent。
- **触发概率**：低；需要本地文件写权限或不安全部署。
- **证据**：`tests/test_migration_agent_cli.py` 覆盖 symlink、hardlink、owner/mode 和 descriptor 读取；`tests/shell/test-migration-install.sh` 覆盖安装器目录/文件权限合同。
- **当前缓解**：agent 以 `noyra` 运行、identity 读取路径受 systemd 约束、私钥 fingerprint 必须匹配 public key、session token 需 HMAC 认证。
- **建议**：保持安装器 root:noyra、0600/0640 和父目录私有权限；Windows fallback 继续保持明确的兼容性诊断。
- **时机**：代码修复已完成；安装器和标准系统权限仍需部署验收。
- **验证方法**：权限、owner、hardlink、替换竞态、reparse/symlink、错误 fingerprint 和重启加载测试。

### M08：Windows package gate 在当前审计环境失败（代码门禁已修复）

- **状态**：代码门禁已修复（`ad7b476`）。脚本在 Resolve-Path 前检查 Management module/cmdlet，缺失时稳定退出 78 并给出诊断；当前不完整 runtime 仍会被拒绝。
- **风险等级**：P2/P3，平台发布门禁。
- **修复风险等级**：中。
- **根因/证据**：全量 `.venv\Scripts\python.exe -m pytest -q` 结果为 `1723 passed, 24 skipped, 2 failed, 259 subtests passed`；失败均为 `tests/test_m42_p3_05_windows_package.py`，调用 `scripts/build-windows-package.ps1` 时当前缓存的 `pwsh` 报 `Microsoft.PowerShell.Management` 内置模块无法加载。直接以相同 runtime 调脚本也复现 `Resolve-Path` 模块加载错误。脚本预期使用 `Resolve-Path`、`Test-Path`、`Copy-Item` 等标准 cmdlet（`scripts/build-windows-package.ps1:12-70`）。
- **影响**：当前环境不能证明 Windows install/upgrade/rollback 和 hash tamper gate；发布流水线若使用同一损坏 runtime，会阻断或产生不完整证据。
- **触发条件**：在当前 Codex PowerShell runtime 或等价缺少 Management module 的环境执行 Windows package tests。
- **触发概率**：当前环境高；在标准 Windows PowerShell/完整 pwsh 环境未量化。
- **当前缓解**：Linux/跨平台 Python tests、Ruff、mypy、compileall 和 migration focused tests 通过；失败没有修改工作区。
- **建议**：在标准 Windows PowerShell 7/Windows runner 重新执行并保存证据；未取得绿色 Windows runner 证据前，不宣称全平台发布门禁通过。
- **时机**：下一次 release 前解决或明确为环境阻断。
- **验证方法**：干净 Windows runner 运行两个失败测试、完整 package lifecycle、tamper rejection、`pwsh -NoProfile` module inventory 和 `git diff --check`。

### M09：真实环境发布证据仍然缺失

- **状态**：外部发布门禁未关闭，不是单一代码漏洞。
- **风险等级**：P1 发布门禁。
- **修复风险等级**：高。
- **根因**：本地单测使用 fake signer/RPC、临时数据库和 mock target；当前审计无法连接真实 Ubuntu/systemd、Cloudflare/Caddy、独立 signer/KMS、S3、Sepolia reorg 环境或两台隔离主机。
- **影响**：不能从本地绿灯推断真实密钥轮换、发送后断连、nonce/reorg、LUKS/权限、备份恢复、迁移 fence 或公网多实例限速安全。
- **触发条件**：把本地测试通过直接当成自动付款、policy-auto migration 或公网长期运行的批准依据。
- **触发概率**：未量化；产品上线时必然需要面对。
- **证据**：本报告工具限制；`docs/security/threat-model.md:96-101` 也明确真实两主机 restore/fencing/rollback rehearsal 是 release gate。
- **当前缓解**：代码默认关闭 migration，wallet automation 有限额与 emergency pause，unknown 和 reorg 有显式状态/incident 路径。
- **建议**：为同一 commit 生成可离线验证的 external-gates artifact，包含 staging chain、独立 signer/KMS、备份恢复、reorg/timeout、两主机迁移、24/72 小时 soak、reviewer 和证据引用；缺失或过期就阻断发布。
- **时机**：开启真实自动付款、policy-auto migration 或公网长期运行前必须完成。
- **验证方法**：真实环境执行 release gate，保留脱敏日志、receipt、backup hash、epoch transition、rollback 和 storage/provider telemetry，并由独立 reviewer 复核。

## 6. 当前验证记录

- 迁移、retention、provider、service、runner、agent、recovery、external-gates 和 Windows package focused suite：`75 passed`。
- 此前修复批次的迁移/retention focused suite：`80 passed`；M07/M08 focused suite：`25 passed` 和 `3 passed`。
- `.venv\Scripts\python.exe -m ruff check src scripts tests`、`compileall`、`mypy` 和 shell syntax checks 已在修复链中通过；最终全量命令需在本工作区重新执行并记录。
- 本轮未执行真实服务器命令、未读取生产数据库或密钥、未生成或伪造 external-gates evidence。

## 7. 修复优先级与完成状态

1. **迁移执行边界（已完成代码修复）**：M01、M02、M03、M04 已分别通过 coordinator、runner executor、target artifact restore 和 verified backup registry 修复；迁移仍默认关闭，真实跨主机 evidence 未取得前不得开启无人值守模式。
2. **一致性与生命周期（已完成代码修复）**：M05、M06 已覆盖 cutoff 绑定与 crash-consistent cutover；后续发布仍应执行真实故障注入和长时 soak。
3. **部署与平台门禁（已完成代码修复）**：M07、M08 已完成 descriptor 安全读取和 Windows prerequisite preflight；M08 的标准 Windows runner 仍是外部验证项。
4. **发布验收（未完成）**：M09 必须在目标 release SHA 上生成签名 evidence，包含八个 gate、独立 reviewer、时间窗口和脱敏引用；缺任何 gate 都必须 fail closed。

## 8. 下一代升级方向

1. 建立统一 durable resource registry：每张表、artifact、backup、epoch、provider route 都登记 owner、schema/feature version、content hash、retention class、repair strategy 和 evidence refs。
2. 统一 logical operation identity：model/search/payment/migration 使用 operation id、lease、CAS 和 reconciliation；`unknown` 永远进入调查状态，不创建新 idempotency key 进行盲重试。
3. 将迁移编排收敛为 source admission、backup manifest、encrypted transfer、target restore、health witness、epoch fence、cutover 和 rollback 的明确状态机；每个状态都必须由动作结果产生。
4. 把 wallet settlement 继续收敛为 `proposed → admitted → signing → broadcast_unknown/broadcasted → confirming → confirmed/failed/reorged/reconcile_required`，链重组后禁止继续把旧 confirmed 当作付款事实。
5. 将 provider/model/search 的 priority、capability、cooldown、half-open permit 和 unknown 统计统一为一个路由内核，并保留短窗口聚合与必要审计。
6. 将 production profile、安装器、systemd、Compose、反向代理和 CI 统一到同一份可执行 configuration contract；不允许“本地健康但生产合同缺字段”。
7. 发布证据采用签名 JSON/DSSE 或等价格式，绑定 commit SHA、gate IDs、执行人、独立 reviewer、时间窗口和 evidence refs，拒绝秘密字段和不可验证的手工声明。

## 9. 审计限制声明

本报告是代码和仓库证据审计，不是渗透测试，也不是对真实链、真实密钥、真实云主机或公网配置的认证。风险等级和触发概率是工程判断。M01–M08 的“已修复”只表示代码与本地回归测试已覆盖对应根因；M09 的真实环境发布门禁仍未通过。除上述修复提交和本报告/计划文档外，本轮没有修改生产配置、数据库、服务器或远程仓库。
