# Noyra 全项目第二轮逐代码只读审计（2026-10-18）

## 结论

本轮审计基于当前分支 `codex/phase-a-system-disk-storage` 的 HEAD `f2129c5`。审计发现的代码问题随后已按模块修复；本文件保留发现、风险和外部验收边界，并在末尾记录修复结果。没有确认 P0；原审计发现包括 1 项条件性 P1、8 项 P2、3 项 P3。

最需要优先处理的是：存储身份文件与当前数据路径不匹配时可能被静默忽略、升级 runner 的身份校验弱于运行时、未知模型别名的 reasoning probe 结果无法进入正式配置、soak 对进行中的 attempt 可能永久漏记，以及严格双人复核仍依赖受信代理正确清理并声明 principal。它们分别影响数据分叉风险、升级边界、配置可用性、长期证据完整性和审批身份保证。

上一轮 A-E 整改已经完成了大量基础工作；本轮又补齐了审计发现的主要代码缺口。代码完成度已明显提高，但生产验收仍受 Ubuntu、CI、真实 Provider 和连续 soak 证据约束。

## 范围、方法与证据

审计覆盖 `src/noyra`、`scripts`、`deploy/systemd`、安装/升级脚本、Provider/reasoning、迁移审批、SQLite/retention、backup GC、soak collector、前端配置、测试和 CI workflow。重点追踪了上一轮审计中已经修复的每项问题，检查修复是否在所有调用路径保持同一安全合同，并寻找修复之间产生的新边界差异。

本地证据如下：

- `git status --short --branch`：工作区干净；HEAD 为 `f2129c5`。
- `ruff check .`：通过。
- `ruff format --check .`：通过（680 个文件已格式化）。
- `python -m mypy --python-version 3.12 src tests`：通过（435 个 source files）。
- `pip check`：通过。
- `python -m compileall -q src scripts`：通过。
- 针对 reasoning、storage、deployment maintenance、soak 的测试：55 passed、1 skipped。
- 修复后的全量 pytest：2683 passed、59 skipped、288 subtests passed；没有失败。跳过项均为 Windows 不具备的 POSIX/root/symlink 条件或未安装可选 `boto3`。
- 上一轮完整测试记录为 2666 passed、59 skipped、285 subtests passed、1 failed；失败是 Windows 本地 HTTP 测试的 `ConnectionAbortedError`，该用例单独连续运行 10 次通过。因此不能把完整 pytest 宣称为全绿。

当前没有把 GitHub CI、Docker 构建、真实 Ubuntu 当前 SHA 安装/重启、LUKS/错误设备、真实 Provider 矩阵、跨机恢复或连续 7 天 soak 当作已完成证据。服务器上此前的探索性命令只说明那些服务器在当时的版本和状态下通过了相应门禁，不能替代当前 HEAD 的同 SHA 验收。

## 风险等级定义

- **P0**：不可逆数据损坏、严重越权、密钥/凭据泄露或生产系统普遍无法启动。
- **P1**：在明确部署条件下可能造成数据分叉、恢复能力失效、升级边界失守或高概率发布阻断。
- **P2**：可恢复但会造成配置功能失效、证据不完整、运维误判或长期可靠性下降。
- **P3**：低概率边界错误、诊断质量或合同清晰度问题，通常不直接影响核心数据安全。

“修复风险”描述修改本身引入回归的可能性，与发现的严重度分开评估。

## 当前发现

### P1-01：存储身份路径不匹配时可能被静默忽略

**位置：** `src/noyra/core/storage_mode.py:211-257`、`260-302`、`340-424`。

**根因：** `_external_mount_identity(data_root)` 和 `_external_backup_mount_identity(backup_root)` 已经严格检查文件类型、root ownership、权限、字段集合和身份格式，但在身份文件声明的路径与当前传入路径不一致时直接返回 `None`（约 252-253、300-301 行）。调用方把 `None` 同“没有身份文件”处理。于是，若没有同时存在的 `storage.conf`、fstab 期望或显式环境声明，当前目录可能按普通系统盘目录继续分类，而不是报告身份冲突。

**触发条件：** 修改 `NOYRA_DATA_DIR`/backup root、旧 identity 文件残留、多个实例共用 `/etc/noyra/storage-identity.conf`、路径迁移后未同步 identity，或维护脚本与 runtime 使用了不同的 root。

**影响：** 原本声明为 dedicated mount 的卷与服务实际使用路径脱钩。服务可能打开系统盘上的另一份 SQLite，或在错误的 backup 目录生成“成功”备份，形成数据分叉、错误恢复点和系统盘空间消耗。该项是条件性 P1：只有把 identity 当作唯一的 dedicated 期望声明且允许自定义路径时触发；默认安装器生成的固定路径通常还会由其他条件保护。

**修复风险：** 中到高。把路径不匹配改为 fail closed 可能阻止旧的自定义部署，必须提供迁移、实例隔离和清晰的错误码。

**建议：** identity 存在但路径不匹配时返回稳定的 `storage_identity_path_mismatch` 或 `backup_storage_identity_path_mismatch`，禁止退化为无 identity；identity 应记录实例 ID 和规范化绝对路径，安装器、runtime、upgrade runner、maintenance 使用同一解析函数。补充 stale identity、改 root、多个实例和重启后错误挂载的测试。

**验收：** 正确路径/正确 source 成功；路径参数改变、identity 旧路径、错误 source、identity 被删除或替换时均明确失败，且不会创建或打开另一份数据库。

### P2-01：升级 runner 的 identity 文件安全校验弱于 Python runtime 和安装器

**位置：** `scripts/upgrade-ubuntu-runner.sh:53-87`，对照 `src/noyra/core/storage_mode.py:211-257` 和 `scripts/install-ubuntu.sh` 的 identity 校验。

**根因：** shell runner 检查了 symlink、字段数量、字段值、mountpoint 和 source，但没有等价检查 identity 文件的 root owner、group/other writable 位以及 `st_nlink == 1`。因此升级恢复边界与 runtime/installer 的信任边界不一致。

**触发条件：** `/etc/noyra/storage-identity.conf` 被错误权限文件替换、被硬链接、被非 root 用户可写，随后执行 `noyra-upgrade-recover` 或升级 runner。

**影响：** root runner 可能依据不应被信任的声明判断数据卷，先创建升级控制状态或写恢复标记；后续安装器可能再失败，造成错误卷上的状态污染和难以解释的升级失败。当前代码尚无证据表明一定会静默升级错误数据库，但合同差异本身是风险。

**修复风险：** 中。shell 中补充 `stat`/`lstat` 检查时必须兼容加密映射设备重启后的稳定 source 表示。

**建议与验收：** 抽出统一的 root-only identity validator，或让 runner 调用只读 Python helper；覆盖 owner、mode、nlink、绝对路径、source、旧 `storage.conf` 兼容和错误设备。任何检查失败都应在创建状态文件前退出并留下稳定 reason code。

### P2-02：未知模型别名的 reasoning probe 成功后仍无法保存正式配置

**位置：** `src/noyra/model/reasoning.py:59-69`、`src/noyra/model/capabilities.py:161-224`、`src/noyra/model/resources.py:271-287`、`src/noyra/service.py:8293-8403`。

**根因：** capability registry 已能按 endpoint/model 保存 probe 结果，且 `record_probe_success()` 接受未知模型的 candidate effort。但 `CognitiveResourceGroupInput.validate_keys()` 在正式保存时仍直接调用静态 `reasoning_profile(self.model, self.provider_type)`；非 `o1/o3/o4/gpt-5/codex` 前缀的 alias 得到 `supported=False`，即使同一 endpoint 的 probe 已成功，也会在 Pydantic 校验阶段被拒绝。

**触发条件：** OpenAI-compatible 网关使用 vendor alias、部署自定义模型名，用户先通过 `/api/config/model-resources/test` 成功测试某个 reasoning effort，再提交资源配置。

**影响：** 用户可以测试能力，却不能保存测试结果；配置页面的“可测试”与“可保存”语义不一致。该缺口不会直接导致未授权调用，但阻断了本轮 reasoning 易用性目标。

**修复风险：** 中。保存校验需要读取 endpoint-scoped capability，并正确处理 stale/unknown/revision；过度放行会把未测试参数写入生产路由。

**建议与验收：** 将静态 profile 明确标为 heuristic；正式保存要求匹配的 endpoint/model profile 为 `known`、未过期且 `tested_efforts` 包含所选值。未知 alias 在 bounded probe 成功后可保存，失败、过期、参数名不匹配和 provider 变更必须清除或阻止旧值。增加 alias、partial efforts、profile expiry 和旧配置迁移测试。

### P2-03：soak collector 对尚未完成的 attempt 可能永久漏记最终结果

**位置：** `scripts/noyra-soak-collector.py:294-351`。

**根因：** attempt 查询和游标排序使用 `COALESCE(started_at, completed_at)`；写入游标时又优先使用 `attempts[-1][1]`，即 `started_at`。如果一次采集发生在 attempt 进行中，行已经有 `started_at` 但没有 `completed_at`，游标会推进到这个 started 时间和 ID。下一次该行完成时，排序键仍然是同一个 started 时间和 ID，因而不会再被读取。

**触发条件：** timer 在模型调用进行中采样，下一次采样前该调用完成；长请求、429 重试或 Provider 超时更容易触发。

**影响：** soak 快照永久保留旧的 in-progress/空错误状态，耗时分位数和最终失败分类缺失。调用量少时单次长调用反而更容易成为证据盲点，削弱 G11 长 soak 结论。

**修复风险：** 中。需要改变游标合同并兼容已写入的 state 文件，避免重复计数和无限重读。

**建议与验收：** 对未完成 rows 不推进最终游标，或使用不可变 attempt ID 加独立 `updated_at`/completion sequence；每轮重读有限数量的 in-progress rows。测试“两次采集之间完成”的长运行 fixture，确认最终状态、耗时和 error 只计一次且不会漏掉。

### P2-04：soak state 损坏或权限错误会静默从头开始

**位置：** `scripts/noyra-soak-collector.py:362-369`、`372-383`、`566-604`。

**根因：** `_read_state()` 对 `OSError`、JSON 错误、超大文件和非 dict 一律返回 `{}`，没有把 reset 原因写入 snapshot 或 journal。collector 随后使用默认窗口和空 cursor，进程仍可能返回成功。

**触发条件：** state 文件被截断、权限改变、目录暂时不可写、磁盘恢复后出现半写文件，或管理员手工编辑 state。

**影响：** 采集可能重复统计旧窗口，也可能因默认窗口变化漏掉历史；操作人员只能看到 timer 在运行，无法知道连续证据已经断链。重复快照还会放大日志和统计误差。

**修复风险：** 低到中。需要保留“不信任损坏 state”的安全行为，同时显式记录证据断点。

**建议与验收：** 返回带 `state_reset_reason` 的状态，向 snapshot 和 journal 写入脱敏 reason；对权限错误与 JSON 损坏分别分类。state reset 后应从有限窗口重建，并在报告中标记连续性中断，而不是伪装成正常续跑。

### P2-05：严格 dual-review 仍不能证明两名不同的自然人

**位置：** `src/noyra/service.py:9104-9144`、`src/noyra/migration/manager.py` 的审批绑定逻辑。

**根因：** 严格模式下 Bearer/session 请求最终使用 `_trusted_reviewer_principal()`；该函数只验证 socket peer 在 `trusted_proxy_cidrs` 内，并信任 `X-Noyra-Authenticated-Principal` header。仓库内的代理示例没有证明会覆盖并清理用户自带的同名 header，也没有内置 IdP、签名 assertion 或 mTLS principal。

**触发条件：** 管理员把前置代理加入 trusted CIDR，但代理透传或未覆盖用户可控的 principal header；同一操作者可通过代理伪造两个 principal 值。

**影响：** 严格模式能证明“来自配置为可信代理声明的两个 principal 字符串”，不能证明两名自然人。默认 `trusted_proxy_cidrs` 为空时会 fail closed，这是降低风险的重要边界，但不能把当前实现宣传成完整的职责分离保证。

**修复风险：** 中到高。引入外部身份源、签名 assertion 或代理清理合同会影响部署和现有管理台登录。

**建议与验收：** 明确两种产品语义：local/staging 使用 self-attested/session；production dual-review 只接受服务器端认证主体、不同会话和策略允许的角色。代理必须在边界清除入站 header 并重新签发 principal，或使用 mTLS/签名 token；缺少可验证主体时保持拒绝。测试同 token、同 session、不同 session、伪造 header、错误 proxy CIDR 和真实两个主体。

### P2-06：`storage-check`/readiness 的成功语义没有把 backup capability 纳入退出码

**位置：** `src/noyra/__main__.py` storage-check 路径、`src/noyra/service.py:2803-2810` 及 storage layout 的 `runtime_ready`/`backup_ready`。

**根因：** runtime readiness 只要求数据目录可用；backup readiness 单独投影为 component。系统盘模式下这是有意允许服务先启动，但 CLI 名称和普通健康检查容易被操作者理解为“运行与恢复均已准备好”。

**触发条件：** `/var/backups/noyra` 缺失、权限不可用、备份卷未挂载或 identity mismatch，而数据目录仍可写。

**影响：** `/health/ready` 和 `storage-check` 可以成功，直到备份、升级前保护、GC 或 restore 才失败。服务本身可以继续提供 API，但回滚能力的缺失暴露得太晚。

**修复风险：** 中。若直接把 backup 设为启动必需，会破坏合法的只运行系统盘部署；需要先定义产品策略。

**建议与验收：** 保留 runtime/backup 两个明确状态，增加 `storage-check --require-backup` 或稳定 JSON/退出码；升级、GC、restore 已经必须强制 backup ready 的路径应统一返回 reason code。管理台要直接显示“服务可运行但没有恢复能力”，避免绿色单一状态。

### P2-07：backup GC 删除后仍可能缺少 durable applied/failed 审计记录

**位置：** `scripts/deployment-maintenance.py:95-140`、`519-544`。

**根因：** `--apply` 删除前以 `required=True` 写入 planned，这一安全边界已修复；删除失败时写 failed、删除成功后写 applied 均使用默认 `required=False`。数据库锁、只读、磁盘满或进程中断时，文件操作可能已发生，但对应的结束状态没有持久化，命令仍可能在日志之外返回或在异常处理后缺少完整 run history。

**触发条件：** 删除阶段之后 SQLite 暂时不可写、进程被杀、数据库损坏或只读挂载。

**影响：** 删除安全本身受 planned 记录保护，但恢复/审计人员无法确认实际删除数量、失败阶段和 retry 状态；长期 retention 运行可能出现“文件已删、数据库说 planned”的不一致。

**修复风险：** 中。让 applied/failed 也强制 durable 可能把维护任务变成 blocked，需要提供 root-only fallback journal 和可重试状态。

**建议与验收：** 删除后必须写并核验终态；写失败时返回非零、写 root-only append-only fallback，并把计划标记为 `completion_unknown`，禁止报告 `applied`。测试 DB lock、SIGTERM、空间不足和恢复重试。

### P2-08：backup retention 的 cursor 是审计字段，尚未形成真正可续跑的跨进程 cursor

**位置：** `src/noyra/core/backup_retention.py:269-274`、`scripts/deployment-maintenance.py:513-544`。

**根因：** plan 中生成 `next_name`/`pending_names`，但每次 `gc_plan()` 仍重新扫描目录、重新计算 inventory/hash，`apply_gc()` 一次性处理全部计划项；中断后没有读取上次 cursor 并从剩余项继续。

**触发条件：** 备份数量大、hash 扫描时间长、维护进程被重启或一次 apply 超过维护窗口。

**影响：** 每次重试成本高，计划 ID 和历史 cursor 看起来可恢复但实际没有续跑语义；长时间空间压力下可能反复扫描、重复审计，且无法精确报告剩余任务。

**修复风险：** 中。真正的 resumable GC 要处理计划过期、文件变更、protected floor 和并发维护。

**建议与验收：** 持久化 plan item 状态和 fingerprint，按 bounded batch 消费 cursor；每批重新核验 hash、current/previous/protected 引用和恢复点下限。中断后从未完成项继续，文件变化则使计划失效并重新规划。

### P3-01：signer receipt confirmation 在 RPC 暂时不一致时可能为负数

**位置：** `src/noyra/signer_service.py:382-390`。

**根因：** 返回 `latest - block + 1` 前没有检查 `latest >= block`。正常链上状态不会出现该值，但 RPC 节点在传播、重组或返回不一致快照时可能暂时出现。

**触发条件：** `eth_getTransactionReceipt` 已返回较高 block，而随后 `eth_blockNumber` 来自落后节点或不同视图。

**影响：** 客户端收到负 confirmations，可能拒绝 receipt、错误计算确认状态或把 RPC 不一致传播成业务错误。属于低概率健壮性问题，不是已确认的签名越权。

**修复风险：** 低。可返回 0 并带 `receipt_pending_consistency`，或重新读取一次同一 RPC 视图。

**建议与验收：** 对 latest 小于 block、等于 block、大于 block 和 RPC 重试分别测试，禁止负数进入公开模型。

### P3-02：receipt API 没有把回执绑定到已登记的请求 envelope

**位置：** `src/noyra/signer_service.py:354-381` 及 `/v1/receipt` 路由。

**根因：** receipt 校验了 tx hash、from、chain、canonical block 和 status，但没有核对此前 broadcast 请求保存的 `to`、value、nonce、asset type 或 request ID。只要是该 signer 地址发出的任意交易，调用方就可以查询到回执。

**触发条件：** 上层把 receipt 当成“本次请求的 transfer 已完成”证明，而不是当成“该地址某笔交易的链上观察结果”。

**影响：** 可能把另一笔交易的成功回执错误关联到当前业务任务。客户端当前通常持有 tx hash，故影响取决于上层是否丢失 request binding；定为 P3 合同清晰度问题。

**修复风险：** 中。需要保存并过期清理 request envelope，兼容恢复和重复查询。

**建议与验收：** broadcast 返回 request ID/tx hash 后，receipt 可选要求 request ID 并核对 envelope；若允许任意 hash 查询，应在 API 文档和审计字段中明确它只是地址级观察。

### P3-03：capability probe 的响应解析没有执行完整 schema 约束

**位置：** OpenAI-compatible capability probe 的 strict JSON schema 请求与响应解析路径。

**根因：** provider 被要求返回 strict schema，客户端随后主要检查 JSON 可解析、`ok=true` 和允许的字段，但没有对额外字段、字段类型和 schema version 做完整验证。

**触发条件：** 兼容网关忽略 strict schema、返回额外敏感字段、把布尔/字符串类型混用，或 provider 版本变化。

**影响：** capability 记录可能接受比合同更宽的响应，导致 UI/保存 gate 对真实能力的判断不够确定。当前不直接导致 secret 写入或任意代码执行，定为 P3。

**修复风险：** 低到中。收紧 parser 可能拒绝已有 provider 的兼容响应，需要保留明确的版本迁移。

**建议与验收：** 对响应运行与生产请求一致的 Pydantic/JSON schema 校验，拒绝未知字段或明确记录 `provider_schema_relaxed`；把 schema version 和 raw response 摘要分开保存，禁止保存自由文本。

## 未列为确认缺陷、但必须补证的环境风险

这些项目不能只凭 Windows 工作区代码审计判定为 bug，但它们是发布前必须完成的证据：

1. Ubuntu 上用当前 SHA 完成系统盘安装、重启、服务 readiness、升级 rollback、缺目录 recover-only 和错误设备 identity。
2. Ubuntu systemd sandbox 中真实运行 `noyra-soak.service`，确认 `/var/log/noyra` owner/mode、state atomic replace、timer exit 0 和 cursor 推进。Windows 的 `systemd-analyze verify` 不能替代此项。
3. GitHub Actions 的 Python 3.11/3.12、Windows/Ubuntu、Docker、pip-audit 和 frontend jobs。当前本地 `mypy --python-version 3.12` 通过，但普通 `mypy` 在 Windows 解析到不匹配的 numpy 3.12 stub；这更像本机解释器/依赖解析差异，不能写成 CI 已通过或代码必错。
4. 真实 Provider 的 GPT/o、DeepSeek、Qwen、非标准 alias、429/502/401、structured output 和 reasoning partial-effort 矩阵。
5. 备份卷卸载、错误设备、跨机 restore、磁盘接近满、GC 中断和至少 7 天低调用量 soak。运行数周但调用极少只能证明进程曾长期存在，不能证明每个调用路径和故障窗口。

## 上一轮审计发现的复核

| 上一轮问题 | 本轮状态 | 评价 |
|---|---|---|
| dedicated data mount 丢失后可能回退 system disk | **已修复代码边界** | `da44f22` 让 identity 与当前 data/backup path 不匹配时显式失败；正确系统盘模式仍保留。 |
| upgrade runner 未核对 data source | **已修复代码边界** | `da44f22` 补齐 root owner、权限、nlink 和 source 合同；shell runner 合同通过。 |
| soak state 与 systemd 写权限冲突 | **已修复路径，未完成真实 sandbox 证据** | 默认 state 已移到 `/var/log/noyra`；需要 Ubuntu 实跑确认安装目录 owner/mode 和 atomic replace。 |
| soak 超过 2000 行永久漏页 | **已修复代码边界** | `52f9ff0` 按 completion keyset 续读，未完成行只计入 in-progress，完成后进入最终汇总。 |
| reasoning 依赖重复模型名前缀 | **已修复保存与调用边界** | `117555f` 以 endpoint/model 的未过期 tested capability 作为 alias 的保存和调用准入，probe 响应拒绝额外字段。 |
| dual-review actor 可任意伪造 | **已收紧断言边界** | `da17cb1`、`06c5ccc` 要求可信代理网络和短时效 HMAC principal assertion；仍不能证明两个自然人。 |
| backup GC 删除前没有 durable plan | **已修复代码边界** | `52f9ff0` 删除前落 durable plan，按 action 更新 cursor，终态 applied/failed 写入失败即阻断。 |
| retention policy/plan 缺失 | **已修复代码边界** | `52f9ff0` 引入受限 action cursor、重启续跑、hash/marker 重检和多 pending run 拒绝。 |
| ancestor reparse 检查不完整 | **已修复** | `e552cde` 已改为逐层检查并补祖先 symlink 测试。 |
| signer receipt confirmation/请求绑定 | **已修复代码边界** | `1383551` 绑定已广播 envelope，校验 from/to/value/nonce/data/canonical block，并把 RPC 落后时 confirmations 夹为 0。 |
| CI 静态门禁曾失败 | **本地门禁通过，CI 仍未由本轮执行** | ruff/format/py3.12 mypy/pip check/compileall 和全量 pytest 已通过；远端矩阵仍需实际运行。 |

## A-E 五个模块的完成质量评价

### 阶段 A：系统盘存储与启动 — 约 90%

系统盘模式、路径/reparse 检查、数据卷 identity、缺失/错误 dedicated mount 的 fail-closed 行为已经形成，服务不再仅依赖 `ConditionPathIsMountPoint`。本轮已补 identity path mismatch、升级 runner owner/mode/nlink/source parity 和 `storage-check --require-backup`。代码目标基本达成；第二台 Ubuntu 的当前 SHA 安装、重启、错误设备和磁盘重装恢复仍未闭环。

### 阶段 B：升级、schema 与恢复 — 约 86%

schema fingerprint、旧 release 合同、processing/锁、previous 指针恢复和 recover-only 基础实现质量较高。升级 runner 的信任边界已与 runtime 对齐到关键 owner/mode/nlink/source 检查；真实 systemd/root 场景尚未验证。阶段目标可以认为“代码主体完成、生产恢复演练未完成”。

### 阶段 C：Provider、reasoning 与 health — 约 84%

错误分类、预算/attempt ledger、cooldown/half-open、分层 health 和 capability registry 是有效改进。reasoning 保存、调用和 probe 现在统一使用 endpoint/model tested capability；真实 GPT、DeepSeek、Qwen 和兼容网关矩阵仍未由当前 SHA 证明。此前第一台服务器换成 GPT 后“无调用”的现象，仍需依靠配置测试、调用 ledger、provider HTTP 状态和 delivery 记录闭环。

### 阶段 D：凭据、部署向导与迁移审批 — 约 84%

root-only credential 文件、secret-free manifest、self-attested 默认路径和服务器端 session/token binding 已降低了部署和测试门槛。strict dual-review 现在要求可信代理网络、root-managed HMAC key 和 request-bound assertion；它仍不能证明两个自然人。安装向导尚未把 storage、credential、proxy、rollback 合并成真正的低交互回滚流程。单人 staging 可以自核验的要求已经实现。

### 阶段 E：retention、SQLite、活动日志与 soak — 约 86%

retention registry、verified backup floor、protected references、容量摘要和 bounded soak 快照已经具备工程基础。本轮已补 in-flight completion cursor、state reset reason、durable GC terminal state 和 resumable action cursor；真实 90 天 cutoff、磁盘压力、GC 中断恢复和 7 天 soak 仍必须在 Ubuntu 上以当前 SHA 重新取得。

## 原规划目标的当前判定

原规划 G1-G11 不应标为全部通过。G7/G9 的备份异常、HTTPS signer、Sepolia transfer、nonce/余额/费用等有用户实测记录，但这些记录来自具体服务器和具体时点，不能自动升级为当前 HEAD 的完整回归。G1-G6、G8、G10、G11 需要同 SHA 的 Ubuntu/CI/Provider 证据；尤其 G11 需要连续生命周期、健康、Provider、增长、retention 和未解释重启记录，不能用“服务运行很久但调用很少”替代。

## 修复优先级与安全边界

1. **A：存储与升级边界已完成代码修复。** 保持系统盘合法模式可启动，dedicated 模式缺失或错卷 fail closed；剩余是 Ubuntu 安装、重启和错误设备取证。
2. **B/C：reasoning 与 Provider 合同已完成代码修复。** 保存 gate 只接受 endpoint-scoped、未过期且已测试的 capability；静态 prefix 只用于候选 UI；剩余是真实 Provider 矩阵。
3. **D/E：审批、soak、GC 与 retention 已完成代码修复。** 仍需真实 systemd sandbox、GC 中断和连续 soak 证据。
4. 每个模块已执行针对性测试、ruff、format、Python 3.12 mypy、串行全量 pytest；Python 3.11 CI、Docker、pip-audit 和 Ubuntu 真实环境仍需独立运行。没有这些证据，不把修复标成生产完成。

## 本轮修复结果

本轮修复按安全边界分模块完成，并分别提交到当前分支：

| 模块 | 提交 | 修复内容 |
|---|---|---|
| A：存储身份与升级边界 | `da44f22` | identity path mismatch 显式 fail closed；升级 runner 补 owner、权限、nlink、source 校验；增加 `storage-check --require-backup`。 |
| B/C：reasoning 与 Provider 合同 | `117555f` | endpoint/model capability 作为 alias 保存和调用准入；tested effort 过期/缺失时阻断；probe 响应拒绝额外字段；增加 alias 与 strict schema 回归。 |
| D：严格迁移复核 | `da17cb1`, `06c5ccc` | principal assertion 改为可信代理 HMAC-SHA256、短时效、方法/路径绑定；strict mode 强制 HMAC key 与 trusted proxy CIDRs；Nginx/Caddy 样例清除客户端伪造头。 |
| E：soak、retention、GC | `52f9ff0` | completion-based activity cursor、in-progress 统计、state reset reason、持久化 resumable GC action cursor、终态审计强制写入。 |
| signer：回执边界 | `1383551` | receipt 仅查询已广播 request，校验 transfer envelope 与链上交易字段，RPC 高度落后时 confirmations 不下溢。 |

模块验证结果：相关专项测试 31 passed、1 skipped；signer 专项 17 passed；reasoning/服务/安全配置专项通过；ruff、format、Python 3.12 mypy、pip check、compileall 通过；Ubuntu shell 合同脚本通过；全量 pytest 为 2683 passed、59 skipped、288 subtests passed。

这些结果证明当前 Windows 工作区的代码合同已经收敛，不能替代 Linux-only 安装、权限、systemd、LUKS、真实 Provider 和连续 soak 验收。审计发现的原代码问题已进入修复提交；剩余工作是目标环境取证和未执行的远端 CI/Docker/Provider 矩阵。

## 审计限制

审计阶段只读检查代码、测试、CI 配置、提交历史和既有审计记录；随后修复阶段按模块修改源代码、测试和部署文档并逐模块提交，没有修改服务器或推送远端。Linux-only 的 LUKS、mount source、systemd sandbox、root ownership、重启、网络代理、真实 Provider 和跨机 restore 结论必须在目标 Ubuntu 上重新取得。文档中的风险等级表达代码合同和证据边界，不是对任何生产主机当前状态的远程保证。
