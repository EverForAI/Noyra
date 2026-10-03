# Noyra 全项目只读审计报告（2026-10-03 复审）

## 1. 结论

本报告是对当前工作区 `codex/wake-after-clean-restart`、提交 `12d2077e76fb5199a0c655e3ef4e3c92ece44591` 的只读复审。当前数据库 schema 常量为 78。审计覆盖运行时所有权与生命周期、HTTP/管理台、完整性与 at-rest、数据保留、模型/搜索 provider、钱包/付款、迁移控制面与目标 agent、安装器/systemd、升级脚本和 GitHub Actions 发布门禁。

代码层已有大量有效控制：管理认证、secret 文件门禁、公开投影白名单、完整性 quarantine、钱包限额/暂停、迁移默认关闭和人工审批、分块传输、目标证明、签名 release gate 均有实现与定向测试。本轮仍发现 8 项需要在生产开放迁移/自动付款前处理的具体缺口，其中 4 项是迁移的 P1 集成风险。它们不是“测试没写全”这么简单，而是执行链与声明合同之间仍有断点。

本轮没有连接远程服务器、GitHub Actions、Cloudflare/HTTPS 代理、真实 signer/KMS、真实区块链或两台真实主机；因此外部发布证据只能标记为未知，不能推断为已通过。审计不修改业务代码、测试、部署配置、生产数据或远程仓库。

## 2. 基线、范围与方法

- **审计时间**：2026-10-03（Asia/Shanghai）。
- **代码基线**：`12d2077e76fb5199a0c655e3ef4e3c92ece44591`。
- **分支**：`codex/wake-after-clean-restart`。
- **schema**：78（`src/noyra/core/database.py`）。
- **检查方式**：静态阅读源码、systemd/安装脚本、OpenAPI/管理台脚本、测试与既有审计；执行 Ruff、mypy、compileall 和迁移定向测试；启动全量 pytest 作为回归观察（最终结果以本报告验证记录为准）。
- **未覆盖**：真实 Ubuntu 权限、真实双机切换/断电/网络中断、真实 LUKS/备份恢复、KMS/signer、真实 RPC nonce/reorg、HTTPS 代理、GitHub 受保护环境和 24/72 小时 soak。

## 3. 评级口径

| 项目风险 | 含义 |
|---|---|
| P0 | 无门槛远程代码执行、大规模机密泄漏、不可逆主体损坏或高危副作用 |
| P1 | 认证/资金/主体完整性/单活边界被破坏，或关键能力会错误报告完成/确定不可用 |
| P2 | 可靠性、容量、配置、状态一致性或运维风险，有明确绕行 |
| P3 | 文档、可观测性、兼容性或外部证据缺口，不直接突破安全边界 |

修复风险：低（局部校验/文案）、中（API/配置/持久化合同）、高（运行时 fence、钱包、密钥、跨主机恢复/切换）、极高（重构跨主机所有权与一致性）。触发概率是工程判断；确定性路径标为 100%，没有真实环境数据的标为未量化。

## 4. 问题总表

| 编号 | 问题 | 风险 | 修复风险 | 触发条件与概率 | 建议 |
|---|---|---:|---:|---|---|
| A01 | 标准安装创建的 migration/source 目录不可由 Noyra 服务写入 | P1 | 中 | 标准安装后服务启动或同步 runtime epoch；100%（权限合同成立时） | 立即修复 |
| A02 | 管理台 cutover 按钮不提交后端必需的 proof | P1 | 中 | 在管理台点击“执行切换”；100% | 立即修复 |
| A03 | active migration epoch 与 admission 及在途操作没有排空/统一 fence | P1 | 高 | cutover 与认知、写入或外部调用并发；竞态发生时高，精确概率未量化 | 立即修复 |
| A04 | target activation 只落 JSON marker，没有启动目标服务或切换数据/入口 | P1 | 极高 | HTTP executor 返回 activation 后进行真实切换；100%（当前 agent 行为） | 立即修复 |
| A05 | 迁移 artifact 实际是 raw SQLite，提案却声明 encrypted backup；钱包迁移未接入 cutover | P1 | 高 | 依赖 secrets、本地钱包或 signer rebind 的迁移；概率取决于部署，功能触发时确定 | 立即修复 |
| A06 | `require_encrypted_storage` 仅保存配置，不验证目标盘加密 | P1 | 高 | 目标盘未加密但 agent 仍接收/恢复 artifact；概率取决于主机配置，未量化 | 立即修复 |
| A07 | 目标入站默认配额 64 MiB，与 manifest 可接受范围不一致 | P2 | 中 | artifact（含 manifest/chunk）超过配额；达到阈值后 100% | 立即修复 |
| A08 | 多处哈希校验用 `read_bytes()`，大 artifact 会造成内存峰值/OOM | P2 | 中 | SQLite/备份接近允许上限；概率随数据增长，未量化 | 尽快修复，可在迁移开启前完成 |
| A09 | 当前 SHA 的真实生产发布证据无法从本地确认 | P1 发布门禁 | 高 | 发布、开放自动付款或无人值守迁移；证据缺失时 workflow 必然阻断，实际状态未知 | 发布前必须处理 |
| A10 | 全量 pytest 在 Windows 上有 7 个 kernel lock 文件清理失败 | P2 | 中 | boot 后未显式 close 的 kernel 测试在 TemporaryDirectory 清理时触发 WinError 32；当前复现为 7 项 | 立即修复测试/生命周期合同 |
| A11 | release workflow 合同测试仍要求已删除的 continue-on-error | P2 | 低 | 全量 pytest 执行该断言；100% | 立即修复测试合同 |

## 5. 逐项发现

### A01：标准安装的 migration/source 权限与运行时写入不匹配

- **证据**：`scripts/install-ubuntu.sh:895-899` 以 `root:noyra 0750` 创建 `migration/source`；`deploy/systemd/noyra.service` 以 `User=noyra`, `Group=noyra` 运行；`src/noyra/core/runtime.py:323-341` 由服务进程在该目录 `mkstemp`、fsync、`os.replace` 写 epoch。
- **根因**：目录所有者 root，组 noyra 只有 `r-x`，没有写位。安装脚本虽然初始化 epoch 文件，但没有使服务用户能够原子替换它；也没有把写操作委托给受限 root runner。
- **影响**：启动恢复、生命周期变化或每次 `_sync_migration_source_epoch()` 可能因 PermissionError 失败，造成服务无法启动/重启或迁移源 epoch 永远不同步。
- **触发条件**：使用标准 Ubuntu 安装并运行以 `noyra` 身份的服务。
- **概率**：100%（只要执行该写路径；本地 Windows 不代表 POSIX 权限通过）。
- **修复风险**：中。需选择 `noyra` 可写但 root 保护仍成立的目录合同，或定义 root runner IPC，并补充真实 POSIX/systemd 测试；不能简单把整个 migration 根目录改成 0777。
- **时机**：立即修复。迁移和正常重启都依赖它。

### A02：管理台 cutover 请求没有 proof

- **证据**：`src/noyra/web/admin.js:1158-1160` 对 `/api/v1/admin/migration/tasks/{id}/cutover` 发送 `JSON.stringify({})`；`src/noyra/service.py:4492-4497` 明确要求 `payload["proof"]` 为对象，否则返回 `verified_target_restore_and_health_proof_required`。
- **根因**：后端已经改为 proof-bound cutover，但 UI 仍是旧的空 payload；界面没有显示/收集 target restore、health digest、manifest 和 target signature，也没有“从已验证任务加载 proof”的调用。
- **影响**：管理员点击按钮必然得到 400/409，UI 不能完成已经准备好的迁移；若文案仍提示成功，会造成错误运维判断。
- **触发概率**：100%（通过当前管理台按钮执行时）。
- **修复风险**：中。必须保持 proof 不可由普通文本框伪造，优先由后端返回短期、任务绑定的可提交票据，UI 只提交票据/显示摘要。
- **时机**：立即修复；在修复前隐藏或禁用 cutover 按钮比让它看似可用更安全。

### A03：epoch fence 没有排空 admission 在途操作

- **证据**：`src/noyra/service.py:1571-1590` 的 `_migration_http_source_fence()` 只查询 active epoch 并生成 hash；没有 `admission.invalidate()`、`begin_drain()` 或 `wait_for_drain()`。更严重的是，`RuntimeAdmissionGate` 的 ownership check 会把 active epoch 判为“不再拥有运行时”；因此普通 HTTP POST（`service.py:4152-4162`）在进入 cutover/rollback handler 前就可能被拒绝，`src/noyra/migration/cutover.py:128-145` 的 `admission.begin("migration-cutover")` 也会在 prepare 已取得 active epoch 后再次失败。
- **根因**：数据库 epoch、内存 admission gate、在途 lease 是三套状态；获取 epoch 不会自动使已经发出的 lease 失效，也不会等待认知/支付/模型外部调用结束。失败路径的 `_migration_http_source_unfence()`（`service.py:1592-1596`）只是保留 active epoch，未提供显式 drain/恢复协议。
- **影响**：当前实现首先表现为 cutover/rollback 的管理请求在 active epoch 后被 admission 拒绝，迁移无法完成；若绕过该检查，仍存在快照与最后一次源端写入/外部副作用重叠的竞态。旧 lease 可能在 epoch 获取后继续到达提交边界，极端情况下形成目标缺少最后写入或源/目标都对外工作。
- **触发条件**：迁移切换与认知循环、钱包付款、provider 调用或 HTTP mutation 并发。
- **概率**：管理请求被拒绝是 100%（按当前 active-epoch ownership check）；在途操作竞态的发生率未量化，但只要存在并发窗口就可触发。
- **修复风险**：高。应先进入 migration-specific drain 状态，拒绝新 admission，等待已有 lease 到达安全边界，再原子写 durable fence/epoch，并对外部 side effect 设置终止/unknown 合同。
- **时机**：立即修复；在真实双机演练前保持迁移关闭。

### A04：target activation 不是服务接管

- **证据**：`src/noyra/migration/agent.py:679-724` 的 `activate()` 只在 `data_root/activations/{task_id}.json` 写入 `{status:"active"}`；仓库中没有 Noyra service、systemd 或入口代理消费该目录来加载 `restore_root/noyra.sqlite3`、启动服务或切换流量。`src/noyra/migration/http_executor.py:235-263` 将这个返回值包装成 `target_activation_digest`。
- **根因**：activation 的协议被实现成“发布 marker”，但没有目标运行时接管器（systemd unit、服务启动参数、流量/域名切换、旧实例停止和健康后的 ownership handoff）。
- **影响**：控制面可记录 `committed` 且 receipt 校验通过，而目标主机实际上没有对外服务，或仍使用自己的旧数据库；源端可能已被 fence，最终表现为停机。管理台会把 marker 成功误报为迁移完成。
- **概率**：100%（按当前 agent 实现，除非部署者另行编写未受仓库合同约束的 watcher）。
- **修复风险**：极高。必须定义目标服务的 systemd/socket/入口切换合同，activation 必须返回“新实例已启动、读取指定 artifact、health/ownership 已确认”的证明；失败需可回滚。
- **时机**：立即修复；在完成前不得宣传迁移已可用。

### A05：数据与钱包迁移没有按提案合同执行（当前仍为阻断项）

- **证据**：服务 wiring `src/noyra/service.py:1501-1510` 仍使用 `SQLiteArtifactProvider`；该 provider 生成 raw SQLite。当前 `HTTPMigrationExecutor` 在 target lookup、source fence 和 snapshot 之前以 `recipient_encrypted_bundle_unavailable` 失败关闭；target agent 的持久化 HTTP/CLI `receive`、`restore` 也在写入前拒绝旧格式和尚未接线的新格式。提案已明确标记 `migration_execution_ready=false`。独立的 `src/noyra/migration/bundle.py` 只提供经过测试的文件封装基础设施，尚未接入 recipient enrollment、私钥 provisioning、target decrypt/restore、钱包/凭据 proof 或 receipt。`WalletMigration` 的 `plan/apply_external_signer/apply_local_transfer`（`src/noyra/migration/wallet.py:74-160`）仍未进入服务 cutover wiring。
- **根因**：迁移 artifact provider、secret/config/wallet binding 和 target service restore 是独立模块，没有统一的 migration bundle/receipt；提案层描述超前于执行层。
- **影响**：API 密钥、provider secret、备份 key、钱包 keystore 或 signer 绑定不会随迁移按设计恢复；目标可能启动但不能认知、搜索、付款，或误用本地钱包。raw SQLite 还可能包含需要静态加密保护的数据。
- **概率**：依赖配置的部署中高；一旦迁移触发则缺失是确定的。
- **修复风险**：高。需把配置/凭据引用、wallet mode、external signer rebind 或显式二次批准的 local transfer 纳入同一 proof/receipt；禁止把秘密放进 SQLite artifact 或普通审计日志。
- **时机**：立即修复；在完成前仅允许演练，不允许生产迁移。

### A06：目标加密要求没有被验证

- **证据**：`MigrationAgent.__init__` 接受并保存 `require_encrypted_storage`（`src/noyra/migration/agent.py:125-175`），但 代码中该字段只被保存，没有被 restore/validate/activate 使用；systemd unit 只声明 `RequiresMountsFor=/var/lib/noyra`，没有 LUKS/attestation 检查。
- **根因**：加密要求是配置元数据，没有绑定到主机实际卷证明；`encrypted_volume`/类似 target registration 字段不能证明 restore root 所在文件系统已加密。
- **影响**：敏感 artifact 可能被接收到未加密盘并保留在 incoming/restore 目录；攻击者离线取得目标盘即可读取数据。该风险与“传输加密”不同，HTTPS/HMAC 不能替代 at-rest。
- **概率**：未量化，取决于目标运维配置；当前代码不会主动阻断。
- **修复风险**：高。需要启动时验证受信的 LUKS/volume attestation，且将证明摘要绑定 manifest/target proof；失败必须在 receive 前拒绝。
- **时机**：立即修复。

### A07：入站配额合同不一致

- **证据**：`MigrationAgent` 默认 `max_incoming_bytes=64*1024*1024`（`agent.py:129-165`），同时允许最大 1 GiB；manifest/传输代码没有把生产数据容量、磁盘预留和目标配置统一起来。`receive_chunk()` 在达到 `max_incoming_bytes` 时拒绝。
- **根因**：协议上限、默认 agent 配额和安装器/磁盘容量没有单一配置源；服务无法在 proposal 阶段根据实际 artifact 预留目标空间。
- **影响**：数据库或 encrypted backup 超过 64 MiB 时迁移失败；失败通常发生在已经 fence/传输了一部分之后，增加恢复窗口和残留清理压力。
- **概率**：达到阈值后 100%；当前数据规模未知，增长是常见运维路径。
- **修复风险**：中。采用显式 per-target quota、manifest preflight、磁盘可用空间与 reserved bytes 检查，并保证失败在 source fence 前可预测。
- **时机**：迁移开启前立即修复或至少将配额写入目标注册合同。

### A08：完整 artifact 哈希读入内存（资源切片已修复，目标 agent 仍有其他读入点）

- **证据**：`SQLiteArtifactProvider` 和 `_manifest()` 已改为固定块大小的 `_stream_digest()`，并有回归测试证明不会调用 `Path.read_bytes()`（`fb197ae`）。target agent 的 `_verify_artifact()` 及分块组装路径仍存在一次性 `read_bytes()`，因此本项只完成 executor/provider 资源切片，不能宣称整个 A08 完成。
- **根因**：哈希实现使用一次性 bytes，而不是固定块大小的 streaming hash；同一文件还可能被重复读取。
- **影响**：大数据库会产生多个 artifact 大小级别的瞬时内存分配，导致高延迟、OOM 或服务被 systemd 杀死；迁移失败后可能触发复杂 rollback。
- **概率**：未量化，随 artifact 增长；在小数据库中不触发。
- **修复风险**：中。使用 `hashlib.file_digest` 或固定块循环、限制并发和在 manifest 阶段复用 digest；补充大文件资源测试。
- **时机**：迁移开放前完成；低于当前容量时可短暂暂缓但不应忽略。

### A09：真实发布证据属于外部未知门禁

- **证据范围**：本地可见 `.github/workflows/release.yml`、`external-gates.yml` 和 `scripts/verify_external_gates.py` 的同 SHA/签名/时间窗校验；本轮没有访问 GitHub artifact、受保护环境或生产主机，无法判断当前 SHA 是否已有有效 evidence。
- **根因**：真实双机、Ubuntu/systemd 权限、LUKS、备份恢复、signer/KMS、RPC reorg/nonce、HTTPS proxy 和 soak 不可能由本地 fake 测试证明。
- **影响**：本地 pytest、Ruff、mypy 通过不能等同于生产自动付款、无人值守迁移或公网长期服务已验收；缺 evidence 时 release workflow 应阻断，强行绕过会失去安全门禁。
- **概率**：未量化；证据缺失时发布路径确定阻断。
- **修复风险**：高，需真实环境演练、脱敏日志、同 SHA 签名和独立 reviewer。
- **时机**：任何生产发布或开放高风险能力之前；保持 migration 默认关闭、自动付款受限。


### A10：Windows kernel lock 文件未在测试生命周期结束时释放

- **证据**：全量 pytest 结果为 `8 failed, 1733 passed, 24 skipped, 259 subtests passed`；其中 7 个 `tests/test_kernel.py` 用例在 `TemporaryDirectory.cleanup()` 处因 `noyra.sqlite3.lock` 仍被占用而失败（WinError 32）。测试的 `tearDown()` 只清理临时目录，没有对 boot 后的 `SubjectKernel` 调用 `close()`。
- **根因**：`ProcessLock` 在 Windows 使用持有中的文件句柄/字节范围锁；`SubjectKernel.close()` 是显式释放边界，测试没有保存并关闭所有已 boot 的 kernel。依赖 `__del__` 在 tearDown 之后释放是不可靠的；生产调用方若忘记 close 也有相同生命周期风险。
- **影响**：主分支全量测试不再是绿色；Windows runner 可能残留临时目录和锁文件，掩盖真正失败。长期运行的嵌入式调用若不 close，可能阻止重启/第二实例获取所有权。
- **触发条件**：Windows 上创建并 `boot()` kernel 后，在对象仍存活时清理数据库目录。
- **概率**：当前测试套件中确定发生（7 项）；生产概率取决于调用方生命周期。
- **修复风险**：中。给 `SubjectKernel` 增加明确 context-manager/close 约束，测试 fixture 在 tearDown 统一关闭所有 kernel，再验证 `__del__` 仅作为兜底；不要通过删除 lock 文件绕过 Windows 锁。
- **时机**：立即修复，作为 CI 绿灯和重启可靠性门禁。

### A11：发布 workflow 与合同测试不一致

- **证据**：`.github/workflows/release.yml` 的 external gate 下载步骤已移除 `continue-on-error: true`，这是 fail-closed 的合理实现；但 `tests/test_release_workflow_contract.py:61` 仍断言下载步骤包含 `continue-on-error: true`。全量 pytest 中该断言必然失败。
- **根因**：workflow 安全修复后没有同步更新测试断言；测试仍描述旧的“允许下载失败后再进入 verifier”合同，与现在的“下载失败立即失败”行为相反。
- **影响**：全量测试持续红；后续维护者可能为了让测试通过而重新引入吞错，削弱外部 gate 发布安全。CI 结果不能作为发布质量信号。
- **概率**：100%（每次执行该测试）。
- **修复风险**：低。将测试改成断言下载失败不会被 continue-on-error 掩盖，并覆盖 verifier 的稳定错误路径；仅改测试合同，不放宽 workflow。
- **时机**：立即修复。
## 6. 已确认有效的控制

- 管理 operator token、provider secret 有受保护文件/systemd credential 路径和生产 inline secret 拒绝逻辑。
- 公开页面使用白名单投影；管理诊断与钱包/迁移路由需要认证。
- lifecycle/integrity quarantine、at-rest gate、SQLite WAL/quick-check 和恢复边界已接线。
- 钱包有自动付款总开关、单笔/日限额、紧急暂停和余额/Gas/nonce/确认超时/重组等状态；真实 signer/链证据仍属外部门禁。
- 迁移默认关闭，人工审批为默认，目标注册、challenge/attestation、proof/receipt、分块传输和 rollback 失败关闭路径已存在；上述 A01–A08 表示它们仍未组成可生产接管的完整链。
- release workflow 对 wallet gate、同 SHA external gate、签名和 reviewer 有强制检查；本地无法替代远端证据。

## 7. 修复优先顺序

1. **先恢复 CI 可信度（A10/A11）**：修复测试 fixture 的显式 close 和 workflow 合同断言，确保后续每个安全修复都有绿色回归基线。
2. **再修 A01 与 A03（所有权和 fence）**：统一 POSIX 权限合同，先拒绝新 admission、排空在途 lease，再写 durable epoch；用真实 systemd 用户做重启与并发故障注入。
3. **再修 A04（目标接管）**：定义 target service/systemd、恢复数据库路径、入口流量切换和 rollback receipt；activation 必须证明实例已服务。
4. **再修 A05/A06（数据、凭据、钱包和静态加密）**：建立受保护 migration bundle，外部 signer 默认只重绑，local wallet transfer 需要显式开关/二次审批，目标盘证明绑定 proof。
5. **修 A02（管理台合同）**：后端生成短期 task-bound cutover ticket，UI 只提交 ticket 并显示证据摘要；补齐错误状态。
6. **修 A07/A08（容量与资源）**：preflight 配额/磁盘，流式哈希，断点清理和压力测试。
7. **完成 A09 外部发布门禁**：同一 SHA 完成真实 Ubuntu/systemd、双机、LUKS/备份、signer/KMS、链、HTTPS 和 soak，并由独立 reviewer 签署；之前迁移保持关闭。
## 8. 验证记录与限制

- 迁移定向测试：交接摘要记录为 129 passed；迁移相关 Ruff、mypy、compileall 通过。
- 全量 pytest（Windows）最终结果：**8 failed, 1733 passed, 24 skipped, 259 subtests passed**，耗时 49 分 34 秒。失败为 7 个 `tests/test_kernel.py` 的 Windows lock 清理错误，以及 1 个 `tests/test_release_workflow_contract.py` 的旧合同断言；详见 A10/A11。
- 本报告只新增审计文档，不提交、不推送，不修改生产环境。

## 9. 审计结论

当前代码适合继续做受控开发和本地/测试环境演练，不适合把迁移标记或本地测试当作真实主机接管证据。A01–A06 是开放迁移前的阻断项；A07–A08 是容量/可靠性阻断项；A10/A11 先阻断 CI 质量门；A09 是所有生产发布的外部门禁。只有在上述问题按顺序修复并取得同 SHA 的真实环境证据后，才能把自动付款和迁移能力从演练状态提升到生产状态。







