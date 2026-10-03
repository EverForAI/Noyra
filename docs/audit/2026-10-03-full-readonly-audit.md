# Noyra 全项目只读审计报告（2026-10-03）

## 1. 结论

本次审计基于 `codex/wake-after-clean-restart` 分支、提交 `9bcec470dc8a055c78218f03c512392bb817b062`，当前数据库 schema 为 77。与上一轮修复基线 `344ca1d` 相比，代码没有变化；差异只有上一轮审计和计划文档。本次重新检查了认证与 HTTP 边界、运行时完整性和恢复、数据导出与保留、模型/搜索 provider、钱包付款、迁移、安装升级、systemd 与 GitHub Actions 发布流程。

没有发现本次基线上的 P0 级问题，也没有发现一条无需认证即可执行钱包转账的路径。基础安全控制和对应的本地测试较完整。但迁移功能存在数个代码与部署接线缺口：当前安装器部署出的组件无法完成受控的数据传送、目标恢复和源端 fencing；而管理台 cutover 可以把数据库任务标为 `committed`，但没有完成服务实例切换。这些问题会阻断真实迁移，不能把管理台的“已提交”状态视为 Noyra 已安全迁往目标主机。

因此建议保持迁移关闭，直到下面 M01–M04 修复并通过真实双机演练。钱包自动付款、公网长期运行及发布所需的签名外部证据也尚未在本次本地审计中确认；现有 release workflow 将此证据设为发布门禁。

## 2. 基线、范围和方法

- **审计日期**：2026-10-03（Asia/Shanghai）。
- **代码基线**：`9bcec470dc8a055c78218f03c512392bb817b062`；分支 `codex/wake-after-clean-restart`。
- **代码差异复核**：`344ca1d..HEAD` 仅包含 `docs/audit/2026-10-02-post-remediation-readonly-audit.md` 和 `docs/superpowers/plans/2026-10-02-post-audit-closure.md`，没有新的业务代码修改。
- **数据库 schema**：77，见 `src/noyra/core/database.py`。
- **检查范围**：`src/noyra/`、`scripts/`、`deploy/systemd/`、`.github/workflows/`、测试、OpenAPI、威胁模型和既有审计/修复文档；对迁移控制面、root runner、target agent 和安装器之间的调用链进行了交叉核对。
- **边界**：只读代码与文档审计；没有连接生产服务器、真实 signer/KMS、云备份、区块链 RPC、Cloudflare/Caddy 或 GitHub Actions 环境；没有读取生产密钥、数据库或远程服务状态。本报告是仓库证据审计，不是渗透测试。

## 3. 评级口径

### 问题风险等级

| 等级 | 含义 |
|---|---|
| P0 | 可直接导致无需门槛的远程代码执行、大规模机密泄漏、不可逆主体损坏或高危副作用。 |
| P1 | 可能突破认证、资金、主体完整性或单活边界；或者使关键安全功能在真实环境中不可用/错误报告完成。 |
| P2 | 有实际影响的可靠性、容量、配置或运维风险，通常存在明确绕行方案。 |
| P3 | 文档、可观测性、兼容性或低影响产品缺口。 |

### 修复风险等级

- **低**：局部校验、文案或配置调整，兼容性风险小。
- **中**：涉及 API/配置合同、持久化格式或状态转换，需要回归与兼容性验证。
- **高**：涉及运行时写入 fence、钱包、密钥、跨主机数据恢复、原子切换或恢复路径，需要故障注入和真实环境演练。
- **极高**：需要重新设计跨存储、跨主机或发布所有权/一致性模型。

### 触发概率

概率描述的是问题在触发条件成立后的工程判断，不是攻击频率或资金损失统计。对确定性代码路径，直接标为“100%（该路径必现）”；对缺少真实环境证据的问题，明确写“未量化”。

## 4. 审计覆盖摘要

| 子系统 | 检查结论 |
|---|---|
| HTTP、管理认证和公开投影 | 已核对 Bearer/Session、授权路由及公开 projection 白名单。未确认新的匿名管理操作或公开敏感字段泄漏。 |
| 生命周期、完整性、恢复和 at-rest | 已核对启动 admission、完整性 quarantine、存储加密检查、backup/keyring 接线及恢复约束。旧审计中已修复项不重复记为开放问题。 |
| SQLite schema、runtime export、retention | 当前 schema 为 77；旧报告所述 export ownership graph、表 inventory 与 cursor cutoff 修复在当前源码仍可见。本轮未发现新的确定性失败证据。 |
| 模型、搜索与 provider | 已核对 production secret source、provider health/failover 路由和环境门禁；本地静态检查及 mypy 通过。真实服务商故障/长期冷却恢复未在外部环境重演。 |
| 钱包、付款和 RPC | 已核对自动付款开关、单笔/日限额、暂停控制、未知广播与重组状态，以及资金相关 release gate。没有真实链、真实 signer/KMS 或链重组环境证据。 |
| 迁移 | 控制面、target agent、root runner、systemd 安装和传输实现存在未接通的关键路径，详见 M01–M04。 |
| 安装、升级、发布 | 已核对 Ubuntu/systemd 安装与 upgrade runner、同 SHA CI 和 external-gates verifier。当前本机不能证明真实 Ubuntu、HTTPS proxy 和 GitHub protected environment 已验收。 |

## 5. 已确认的有效控制

- Production operator token 和 provider secrets 有 managed file/systemd credential 路径及 inline secret 拒绝逻辑（`src/noyra/core/credentials.py`、`src/noyra/service.py`、`scripts/preflight-production.py`）。
- Production listener、Secure session cookie、at-rest 和完整性监控有 fail-closed 要求；默认部署仍通过 loopback 与受信任 HTTPS proxy 分界。
- 公开页面投影采用字段白名单；管理诊断和私有状态需要授权。
- 钱包付款有自动化总开关、单笔和日限额、紧急暂停以及 `unknown`/确认超时/重组等状态；本地 targeted wallet gate 在本次基线有同 SHA 通过记录。
- 迁移默认关闭，人工审批为缺省模式；目标注册、挑战签名、策略版本、审批审计和 epoch 数据库状态都有验证逻辑。
- Release workflow 要求同 SHA 钱包 gate、签名 external-gates artifact、外部 reviewer 和其他软件供应链证据；这是有效的 fail-closed 发布控制，但不能替代尚未执行的外部演练。

## 6. 问题总表

| 编号 | 问题 | 风险 | 修复风险 | 触发概率 | 时机 |
|---|---|---:|---:|---|---|
| M01 | cutover 只提交控制面状态，没有执行数据传送、目标服务启用或源服务切换 | P1 | 高 | 100%（调用当前 cutover 代码时） | 立即；迁移保持关闭 |
| M02 | 源端 fencing 依赖一个未由安装器/运行时建立的 epoch 文件；即使补文件，marker 也不会阻止服务写入 | P1 | 高 | 100%（标准安装首次 fence）；若手工补齐后仍无法防写 | 立即；迁移保持关闭 |
| M03 | 标准部署的 target agent 没有恢复目录或加密备份 manager，无法完成健康验证 | P1 | 高 | 100%（默认 systemd 配置运行 restore/health） | 立即；迁移保持关闭 |
| M04 | target agent 的 HTTP 接口用单个 Base64 JSON 接收完整 artifact，1 MB 请求上限与迁移 artifact 容量不匹配 | P1 | 中高 | 100%（artifact 编码后超过 1 MB 时） | 立即；迁移保持关闭 |
| G01 | 当前提交的真实环境 release evidence 未能从本地仓库确认 | P1 发布门禁 | 高 | 未量化；发版/开启真实自动付款或无人值守迁移前必须解决 | 发版及启用高风险能力前 |

## 7. 详细发现

### M01：cutover 仅更新数据库状态，不执行实际主机切换

- **状态**：当前代码中可复现的迁移功能/安全边界缺口。
- **风险等级**：P1，迁移状态与实际运行位置可能不一致。
- **修复风险等级**：高。
- **根因**：`CutoverCoordinator.commit()` 通过本地 SQLite 事务把 task 改为 `committed`，并完成 epoch 记录；该方法没有调用 target agent、复制/校验 artifact、停止源服务、启动目标服务、切换入口流量或完成源端 admission fence。HTTP cutover route 只把提交的 proof 交给 coordinator 并返回结果。`RegisteredTargetProvider.provision()` 明确拒绝自动 provisioning；`TransferSession`/`EncryptedTransferSession` 是本机路径到本机路径的文件操作，不是远程传输 client。仓库中没有从服务运行时到 target `/v1/receive`、`/v1/restore`、`/v1/health` 的迁移编排调用链。
- **影响**：管理 API 可以返回 `committed`，但不能据此证明目标主机正在服务 Noyra，或源主机已停止写入。部署者可能把数据库 task 状态误当成已完成迁移，造成服务中断、数据分叉或两个实例同时运行。
- **触发条件**：部署者启用迁移并通过 `/api/admin/migration/tasks/{id}/cutover` 提交目标 proof。
- **触发概率**：100%（当前 `commit()` 路径只变更数据库 task/epoch；真实服务交接不在该调用内）。
- **建议时机**：立即修复；在真实 source→target→cutover→rollback 状态机完成前保持迁移关闭，不允许把 `committed` 当作迁移成功提示。
- **证据**：`src/noyra/migration/cutover.py:99-127`；`src/noyra/service.py:4418-4426`；`src/noyra/migration/providers.py:22-37`；`src/noyra/migration/transfer.py`；target agent HTTP endpoints 位于 `scripts/noyra-migration-agent.py:178-223`，但 service 侧没有相应调用链。

### M02：源端 fence 是文件 marker，不会阻断运行时写入，且标准安装缺少源 epoch 文件

- **状态**：当前实现不满足真实单活 fencing 合同。
- **风险等级**：P1，主体数据一致性与双主保护边界。
- **修复风险等级**：高。
- **根因**：`MigrationExecutor.fence()` 只读取 `/var/lib/noyra/migration/source/epoch`，并在 `/var/lib/noyra/migration/fences/` 写一个 JSON marker。该 marker 不会被 Noyra service 的 admission/write path 读取，也不会停止服务。Ubuntu installer 只创建 migration、requests 和 status 目录，没有创建或更新 `source/epoch`；仓库内也没有同步 `runtime_state.version` 到该文件的生产写入路径。正常安装首次调用时会因 `source` 目录/epoch 文件缺失而拒绝；手工创建文件只能让校验通过，不能让活跃运行时被 fence。
- **影响**：迁移流程无法在标准安装上完成源端 fencing；绕过缺失文件后，旧源仍可继续处理认知/持久化写入，数据库 epoch 并未约束这些写入。若另一个节点已经恢复并启动，就存在并发主体状态分叉风险。
- **触发条件**：启用 root migration runner 并执行 `fence` 或 `restore` action。
- **触发概率**：100%（标准安装首次运行时源 epoch 输入不存在）；若部署者人工创建输入，marker 未接入运行时写入的风险仍为 100%。
- **建议时机**：立即修复；source admission 必须使用运行时实际共享的、原子且持久的 fence/epoch 状态；需证明 fencing 后旧进程不能继续写，再允许 cutover。
- **证据**：`scripts/noyra-migration-runner.py:185-223`；`scripts/install-ubuntu.sh:892-896`；`deploy/systemd/noyra-migration-runner.service:7-20`；服务 tick 的 admission 使用见 `src/noyra/service.py:9573-9672`。静态检索未发现 service 对 runner 的 `fences/*.json` marker 的读取路径。

### M03：标准部署的 target agent 没有恢复目标与备份解密配置

- **状态**：标准 systemd 安装不具备已测试的 agent restore 参数。
- **风险等级**：P1，真实迁移必然无法通过目标健康验证。
- **修复风险等级**：高，涉及解密密钥注入、隔离恢复目录、数据库 ownership 和迁移后启动合同。
- **根因**：`MigrationAgent` 只有收到 `restore_root` 才会写入恢复数据库；加密备份还需要 `backup_manager`。但生产 CLI `load_agent()` 只传入 identity 和 `data_root`，没有 restore-root/backup-manager 配置入口；`noyra-migration-agent.service` 的 `ExecStart` 也只传 identity/data-root/listen。此时 `restore()` 可返回无 `restore_path` 的报告，`validate()` 随后因 `host_binding` 为 false 而拒绝健康状态。对加密备份，CLI 也没有配置 `EncryptedBackupManager`。
- **影响**：测试中显式注入 `restore_root`/`backup_manager` 的成功路径不能代表 systemd 生产实例；标准部署无法把 artifact 解密/恢复到目标目录，也不能给出通过的 target health proof。迁移将停在验证阶段，或迫使操作者尝试不受控的人工绕行。
- **触发条件**：目标主机以仓库提供的 systemd unit 启动 migration agent，并执行 restore/health。
- **触发概率**：100%（在当前默认 CLI/unit 参数下）。
- **建议时机**：立即修复；为生产 agent 提供受保护的恢复目录与 backup keyring/credential 注入，并验证目标服务以正确身份加载恢复数据。密钥不得放入普通迁移请求或审计记录。
- **证据**：`scripts/noyra-migration-agent.py:125-136`；`src/noyra/migration/agent.py:103-119,451-504,557-568`；`deploy/systemd/noyra-migration-agent.service:13`。

### M04：迁移 artifact 的 HTTP 接收上限小于有效迁移容量

- **状态**：接口尺寸合同互相矛盾。
- **风险等级**：P1，真实数据集不能经 shipped target-agent API 传输。
- **修复风险等级**：中高，需设计带认证、重放防护、断点续传和全量 hash 校验的分块协议。
- **根因**：target agent 的 HTTP body 最大 1,000,000 字节，`/v1/receive` 要求将整个 artifact 放入 `artifact_b64` JSON 字段。Base64 本身约增加三分之一体积，因此实际原始 artifact 上限约为 0.75 MB；而 agent 默认入站额度为 64 MB，manifest/transfer 代码又允许远大于此的 artifact。当前协议没有 chunk upload endpoint 或流式 body 路径。
- **影响**：超过约 0.75 MB 的 artifact 会在 JSON 解析/接收前被拒绝。已有运行一段时间的 SQLite 数据库或备份通常远大于该值，无法迁移；当前仓库也没有可替代的远程 chunk transport。
- **触发条件**：通过 target agent `/v1/receive` 传输编码后超过 1 MB 的备份。
- **触发概率**：100%（一旦 artifact 超过该阈值）；新建且极小的数据目录可能暂时不触发。
- **建议时机**：迁移开放前立即修复；采用分块、逐块 digest、单次任务/manifest 绑定、续传校验、配额预留和最终 artifact hash 的协议，并通过中断/重放/磁盘耗尽测试。
- **证据**：`scripts/noyra-migration-agent.py:21,63-69,152-162`；`src/noyra/migration/agent.py:114-118,243-258,628-638`；`src/noyra/migration/transfer.py:46-98,199-260`。

### G01：本地无法确认当前 SHA 的真实环境发布证据

- **状态**：外部发布门禁状态未知；本地工作区未包含当前 SHA 的 `external-gates.json`。由于本次没有访问 GitHub，不能断言远端 Actions artifact 一定不存在。
- **风险等级**：P1 发布门禁，不是单点代码漏洞。
- **修复风险等级**：高，包含真实主机、加密盘、恢复、迁移 fence、signer/KMS、链重组、HTTPS proxy 和 soak 演练。
- **根因**：当前验证只覆盖本机 fake/temp 环境；真实环境的签名 evidence 需绑定精确 commit、所有 gate、独立 reviewer 和有效时间窗。`release.yml` 会拒绝没有同 SHA wallet gate 与 external gate artifact 的 release。
- **影响**：不能把本地测试通过解释为真实付款、跨主机单活迁移、加密恢复、公网代理或长期运行已经验收。尝试发布时会被预期门禁阻断；绕过门禁则会失去这些关键边界的证据。
- **触发条件**：发布当前 SHA，或在真实环境开放自动付款、无人值守迁移、公网长期服务。
- **触发概率**：未量化；是否已有远端 artifact/reviewer 记录须在 GitHub release environment 中核查。若证据缺失，release workflow 将确定失败。
- **建议时机**：任何生产发版或开放高风险能力之前。先修复并演练 M01–M04，再针对同 SHA 完成八项 gate：Ubuntu/systemd、加密卷、备份恢复、migration fence、signer/KMS、reorg/nonce、HTTPS proxy、soak；保存脱敏记录并由独立 reviewer 签署。
- **证据**：`.github/workflows/release.yml:62-145`；`.github/workflows/external-gates.yml:3-57`；`scripts/verify_external_gates.py:18-29,114-180`。

## 8. 修复顺序建议

1. **先定义迁移的完成合同**：明确数据 artifact、目标恢复、目标服务启动、源 admission fence、流量切换、epoch 更新和 rollback 各自的成功证据；不能把数据库 task 状态当作实际 cutover。
2. **实现真实 source fence**：用运行时写入路径实际检查的 epoch/fence，处理在途写入和服务重启；验证旧源在 fence 后无法继续写。
3. **接通 artifact 传输与目标恢复**：实现分块远程协议，给 target agent 安全注入 backup keyring 和隔离 restore root，再验证 subject/schema/integrity/host identity 与目标服务启动。
4. **把 orchestration 接入控制面**：让状态机仅由上述真实动作结果推进；切换和 rollback 做并发、断电、网络中断和重复请求故障注入。
5. **通过真实外部 gate 后才开放能力**：同一 SHA 完成所有签名外部证据；在此之前迁移保持关闭、钱包自动付款不因本地测试而被视作生产验收。

## 9. 本次验证

- `.venv\\Scripts\\python.exe -m pytest tests/test_migration_runner.py tests/test_migration_agent.py tests/test_migration_agent_cli.py tests/test_migration_service.py tests/test_migration_cutover.py -q -p no:cacheprovider`：**29 passed**。这些测试说明各本地单元路径有回归覆盖，但没有覆盖标准 systemd 参数之间的真实接线，也没有证明服务写入被 fence。
- 当前 SHA 的 wallet smoke evidence：`artifacts/release/stage4b4/9bcec470dc8a055c78218f03c512392bb817b062/20261002T173541321985Z/run.json`：**132 passed, 13 skipped**；evidence 记录的工作区干净，压力限额未超出。该文件是本地 gate evidence，不是生产 signer/真实链验收。
- `.venv\\Scripts\\python.exe -m ruff check src/noyra scripts tests`：通过，`All checks passed!`。
- `.venv\\Scripts\\python.exe -m mypy src tests`：通过，333 个源文件无类型错误。
- 本次未重跑完整 pytest 全量套件；此前修复链的全量结果不能替代本次列出的真实环境门禁。本次也未执行公网、服务器、链、KMS、真实双机迁移或 24/72 小时 soak。

## 10. 未发现与限制

在本次源码和测试范围内，没有形成新的 P0 结论。模型、认证、公开投影、存储加密、保留策略、钱包状态机和发布门禁已经有较多本地保护；“未发现”只表示本次检查未取得足够证据确认另一个具体缺陷，不代表所有路径已经通过渗透测试或生产验收。风险等级和触发概率属于工程估计。

本次唯一新增受版本控制的内容应为本报告。没有修改业务代码、测试、生产配置、数据库、服务器或远程仓库。
