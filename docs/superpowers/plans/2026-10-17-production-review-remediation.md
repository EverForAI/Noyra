# Noyra 生产复盘与升级修复规划

> 日期：2026-10-17
> 范围：2026-08 至 2026-10 的两台 Ubuntu 测试服务器、部署/升级/迁移、Provider、钱包、备份恢复、健康检查、retention 和长期运行记录。
> 当前阶段：完成证据复盘，尚未修改生产服务器、配置、数据库或本计划之外的代码。

## 1. 目标与原则

这次复盘的目标不是继续堆叠一次性门禁，而是把真实部署中暴露的阻塞点变成可重复、可解释、可恢复的产品能力：普通轻量服务器可以安装并启动；模型配置失败时用户能立即知道原因；升级、备份和恢复不会依赖隐含的人工经验；长期运行中的健康状态、预算、Provider 和数据生命周期都有可观察的解释。

实施遵循以下原则：

- 默认支持只有系统盘的 Ubuntu；独立数据盘是容量和隔离优化，不是启动前提。
- 所有失败都返回稳定原因码、下一步建议和可追溯记录，不用泛化的“服务不可用”。
- 生产动作保持显式、可回滚、幂等；探索性测试可以由部署者本人完成并记录。
- 不把 API key、Bearer token、钱包私钥、密码、原始请求或私密内容写入日志、普通导出和备份元数据。
- 兼容已有数据库、发布目录、配置文件和已完成的迁移证据；先加回归测试，再改安装器和运行时。
- 任何“结果未知”的外部副作用都不得自动重发；诊断、重试和恢复必须区分确定失败与不确定结果。

## 2. 已确认的运行事实

### 2.1 第一台服务器的服务停机原因

服务器 `ser799545842233` 的 `/var/lib/noyra` 和 `/var/backups/noyra` 都落在系统盘 `/dev/vda1`，没有独立挂载。当前 release 为 `9845807aeb6439e0aafb273359032b1477a31df4`。

`noyra.service` 的 drop-in 仍包含：

```ini
[Unit]
ConditionPathIsMountPoint=/var/lib/noyra
```

因此重启后 systemd 记录：

```text
noyra.service was skipped because of an unmet condition check
ConditionResult=no
```

当时服务不是崩溃，而是根本没有启动，模型 API 当然不会产生调用。系统盘目录权限也不适合直接作为运行数据目录：`/var/lib/noyra` 为 `root:root 755`，`/var/backups/noyra` 为 `root:root 700`。

### 2.2 升级恢复与数据盘硬依赖

`noyra-upgrade-recover.service` 在 2026-10-08 和 2026-10-15 启动失败；检查时以下路径不存在：

```text
/var/lib/noyra/upgrade
/var/lib/noyra/upgrade/requests
/var/lib/noyra/upgrade/processing
/opt/noyra/upgrade/source
```

升级 runner 会对这些路径进行严格的 owner、mode、非 symlink 检查，所以数据目录未挂载时直接退出 1。该失败会留下 failed 单元状态，却没有向用户解释“可选存储尚未初始化”。

### 2.3 历史 schema 迁移故障

2026-10-07 曾发生 schema 71 到 80 的迁移不一致：数据库 marker 已写成 schema 80，但结构指纹与新版本要求不符。180 秒 readiness 超时后安装器恢复旧 release，服务恢复。仓库已有对应修复文档和回归测试，这个历史故障不应在生产数据库上重复演练，应把同类验证固定在离线 fixture 和安装器测试中。

### 2.4 Provider 和模型调用

历史数据库显示 DeepSeek/Qwen 曾经有少量成功调用；切换 GPT 系列后没有正常调用。主要记录为：

```text
deepseek-v4-flash-0731  provider_http_404  310
gpt-oss-120b            provider_http_404  353
gpt-oss-120b            budget_exhausted   159
qwen3.8-flash           succeeded           10
qwen3.8-flash           provider_outcome_unknown 58
```

其中 NVIDIA endpoint 为 `https://integrate.api.nvidia.com/v1`。配置界面显示“已启用”并不等于可调用：多个 group 被禁用或进入 cooldown，部分 key 仍在 cooldown，而 group 健康投影显示 healthy，存在状态不一致。

### 2.5 健康检查和自治循环

2026-10-14 曾出现长时间：

```text
/health/live  = 200
/health       = 503
```

随后还出现 `/health/ready = 503`。`/health/live` 200 证明进程存活；`/health` 还检查自治循环、完整性、at-rest、operator、钱包采集、secret cleanup 和云归档，因此 503 不等于进程崩溃。

恢复备份中的快照显示：

```text
circuit_status=closed
consecutive_failures=3
last_error_type=ReflectionCognitionPending
```

secret cleanup 没有 pending/failed 项，钱包采集为 2 次 succeeded。`ReflectionCognitionPending` 表示反思调用没有得到可用结果，应进入有界重试/冷却，不能让用户只能看到无原因的 503。日志中的 `BrokenPipeError` 来自探针提前断开连接，应降噪处理。

### 2.6 retention 和备份增长

数据库配置 `NOYRA_EVENT_PAYLOAD_RETENTION_DAYS=90`。截至复盘时，最早事件约 53 天，retention 已执行 17 次，全部无失败，但每次删除数量为 0；这只能证明调度运行，尚未跨过真实 cutoff。

备份目录从约 3.6 MB 的早期备份增长到单个约 79.5 MB，目录总量约 736 MB。GC 预览成功，但只计划删除一个具备验证标记的旧备份：

```json
{"backups":["noyra-github-4d988c5-20261001022257-20261006T122934Z.noyra-backup"],"releases":[],"status":"preview"}
```

现有策略只处理已验证备份；未验证历史备份会长期占用空间，需要告警、隔离和受控治理。

## 3. 已完成的探索性验证

以下项目已经由部署者在真实服务器上测试通过，不应重复消耗生产时间：

| 领域 | 已验证项目 | 结果 |
|---|---|---|
| 备份恢复 | 错误 keyring 拒绝、篡改备份认证失败、非空目标拒绝、原文件不变、测试残留清理、服务恢复 | 通过 |
| HTTPS signer | 未授权 401、错误 token 401、正确 token 身份证明 200、未知路由 404 | 通过 |
| signer 能力 | 身份证明、Sepolia pending nonce、实际转账、receipt confirmation | 通过 |
| 钱包安全门禁 | nonce 冲突、余额不足、费用上限不足 | 通过 |
| 部署和发布 | release SHA 核对、服务 readiness、signer 重启、备份库存检查 | 通过 |
| 升级故障 | schema 71→80 历史故障定位和旧版本恢复 | 已定位并已有代码修复 |
| 长期运行 | 多周进程生命周期、health 探针、数据库调用和 retention 记录 | 发现上述服务、Provider、健康和 retention 问题 |

探索性验证可以由部署者本人完成并写入验收记录。产品不应把“必须有独立核验人”作为所有环境的硬门槛；高风险生产部署可以由策略选择双人复核，但 staging、单人维护和本地部署应支持自核验模式，并记录操作者、时间、目标 SHA、门禁结果和原因。

## 4. 产品需求汇总

### 4.1 单系统盘自动运行

- 安装器检测独立挂载是否存在；没有时创建系统盘回退目录。
- 回退目录由 root 创建，明确转交 `noyra` 所需的最小 owner/mode；备份目录与运行目录分开管理。
- 运行服务只要求“路径安全且可写”，不要求 `mountpoint`。
- 独立数据盘挂载后可以迁移或切换到更大容量，并保留同一数据合同。
- 启动诊断显示当前存储模式：`system_disk`、`dedicated_mount` 或 `unavailable`。
- 卸载或磁盘丢失时 fail closed，禁止把空目录误当成原数据目录。

### 4.2 密码与密钥易用性

- 首次安装只要求用户设置少量核心秘密；其余凭据由安装器生成随机值并保存到 root-only credential 文件。
- 使用 systemd `LoadCredential` 或受保护文件传递 secret，不把 secret 放入命令行、普通环境文件、SQLite 或日志。
- 管理页面显示凭据状态、来源、最后验证时间和轮换入口，不显示 secret 原文。
- 提供“导出恢复包/恢复密钥说明”的一次性流程，明确哪些密码在重装后必须保留。
- 单系统盘和独立数据盘都必须有一致的备份 keyring、凭据路径和恢复说明。

### 4.3 Provider 配置和思考强度

- Provider 保存前执行 endpoint、模型名称、鉴权、最小请求和响应 schema 的实际测试。
- 将 401/403、404、429、5xx、超时、连接失败、响应格式错误、结果未知分为稳定错误码。
- 404 等确定性配置错误不应继续消耗普通模型预算；未知结果不得自动重发。
- group 健康由当前可用 key、cooldown、最近探针和确定性错误共同计算；group 与 key 状态不能互相矛盾。
- 配置页面显示模型、endpoint、资源池、可用状态、错误原因、cooldown 到期时间和最近成功时间。
- 为支持 reasoning 的模型增加能力声明和思考强度选项，例如低、中、高、极高；不支持的模型隐藏该选项。
- 调用时把思考强度映射为 Provider 兼容参数；不支持或未知能力时不发送该参数，并显示“未设置”。
- 记录请求使用的 reasoning profile 版本和归一化值，不记录 key 或私密提示词。

### 4.4 健康检查、日志与长 soak

- `/health/live` 只表示进程可响应。
- `/health/ready` 表示存储、数据库、完整性、必需凭据和启动依赖满足；Provider 暂时不可用不应把进程存活误报为崩溃。
- `/health` 提供受保护的详细诊断，包含每个 component 的稳定 `reason_code`、状态、首次发生和最近更新时间。
- 每次由 degraded 变更触发结构化事件；不输出自由文本私密内容。
- 捕获 `BrokenPipeError`、连接重置等探针断开，降低日志噪声。
- 增加活动日志页：运行事件、生命周期、模型与搜索、行为执行、管理审计分栏，支持时间范围、状态、类别、分页和手动刷新。
- 长 soak 报告至少覆盖进程重启次数、健康状态区间、调用成功率、Provider 错误、cooldown、数据库大小、备份大小和 retention 结果。

### 4.5 升级与 schema

- 升级 runner 和 recover service 在 system disk 模式下自动准备目录。
- 目录权限由 root 安装/升级流程维护；`noyra` 服务用户不得对 root 管理的 upgrade 根目录执行 chmod。
- schema marker、结构指纹、可选持久化 feature marker 必须在同一事务/可恢复阶段完成。
- readiness 超时、迁移异常、重启中断都必须保留可读 status 和恢复建议。
- 升级失败自动恢复旧 release，并验证旧 release readiness；恢复失败要阻止进一步启动并给出明确原因。
- 把 schema 71→80、marker 与实际 DDL 不一致、进程中断、旧备份恢复等全部固定为离线回归测试。

### 4.6 备份、恢复与 retention

- 备份和数据库 retention 使用明确的两套策略和不同指标。
- 备份默认保留最近若干个已验证恢复点，同时受最大年龄和磁盘占用上限约束。
- 未验证备份不自动当作恢复点；超过宽限期应进入“待人工确认”告警。
- 备份生成、校验、验证标记、GC 删除都写入无 secret 的审计记录。
- restore 先验证 AEAD、manifest、路径边界、非空目标和数据库完整性，再替换目标。
- 为跨 90 天 cutoff 的 retention 提供可控测试 fixture，并验证 cursor 续跑、失败恢复、SQLite 空间处理和受保护引用。
- 单系统盘模式必须有磁盘 headroom 预检；备份空间不足时不停止当前健康服务。

### 4.7 迁移审批和易用性

- 策略提供 `self_attested` 和 `dual_review` 两种模式；staging/单人默认允许 self-attested，组织可选择 dual review。
- 自核验记录操作者身份、目标身份、manifest digest、测试门禁、时间和结果。
- 独立复核不是产品普遍启动前提；高风险钱包转移、生产自动迁移可以独立开启双人复核。
- UI 明确显示当前审批模式、尚未完成的门禁、cooldown、回滚状态和需要用户采取的动作。

## 5. 分阶段实施计划

### 阶段 A：存储和启动合同（P0）

**目标：** 让只有系统盘的新用户能完成安装、启动、升级和备份。

**主要改动：**

- 新增 storage mode 检测和安全回退目录初始化模块。
- 删除 `ConditionPathIsMountPoint=/var/lib/noyra` 这一硬条件，改为启动前安全路径检查。
- 安装器统一设置 `/var/lib/noyra`、`/var/backups/noyra`、`upgrade` 子目录 owner/mode。
- `noyra-upgrade-recover.service`、`noyra-upgrade.path` 和 upgrade runner 处理目录缺失、未挂载和迁移中的显式状态。
- 将 root 管理边界从服务用户的 `_harden_private_paths` 中移除，服务用户只处理自己可写的叶子目录。

**建议文件：**

- `scripts/install-ubuntu.sh`
- `scripts/upgrade-ubuntu-runner.sh`
- `deploy/systemd/noyra.service*`
- `deploy/systemd/noyra-upgrade*.service`
- `src/noyra/core/at_rest.py`
- 新增 `src/noyra/core/storage_mode.py`

**测试与门禁：**

- 只有系统盘的全新安装 fixture：服务启动、数据库初始化、`/health/live` 和 `/health/ready` 成功。
- 独立挂载 fixture：仍使用挂载目录且不误用系统盘。
- 挂载丢失 fixture：服务安全降级，不能创建空目录覆盖原数据。
- upgrade 目录缺失、权限错误、symlink、进程中断、recover-only 幂等测试。
- 安装器 shell contract、root/non-root、路径边界和 `git diff --check`。

### 阶段 B：Provider、reasoning 和健康诊断（P0/P1）

**目标：** 用户配置一个模型后能在一次测试中知道“能否调用以及为什么不能调用”。

**主要改动：**

- Provider capability schema：模型能力、reasoning 支持、支持的强度集合、参数名和版本。
- 配置保存前的 dry-run/test contract；明确显示实际 endpoint、模型和响应验证。
- Provider error classifier 和预算策略；确定性错误短路，未知结果隔离。
- 统一 group/key/provider health projection，清除配置变更后的旧 cooldown 或标记为 stale。
- `/health` 和 `/health/ready` 返回 component reason code；健康事件使用结构化日志。
- `/health` handler 安静处理 BrokenPipe，并保留 status 统计。

**建议文件：**

- `src/noyra/model/resources.py`
- `src/noyra/core/provider_health.py`
- `src/noyra/model/ledger.py`
- `src/noyra/service.py`
- `src/noyra/web/admin.js`
- `src/noyra/web/admin.html`

**测试与门禁：**

- 404/401/429/5xx/timeout/unknown/schema error 各有独立测试。
- cooldown 半开探针只允许一次，不重复计费。
- group 有 active key、只有 cooldown key、全部 disabled、配置刚变更四种投影。
- reasoning 支持/不支持模型的 UI、请求参数和持久化测试。
- health reason code 与 HTTP 状态映射测试；非 operator 仍只看到安全摘要。

### 阶段 C：密码、部署和迁移体验（P1）

**目标：** 减少部署者需要记忆和手工复制的密码数量。

**主要改动：**

- 安装向导生成凭据、写入 root-only 文件、显示一次性恢复说明。
- systemd credentials 统一 signer、Provider、Cloudflare 和 Noyra 管理 token 的来源。
- 增加 `noyra setup` 的 local/system-disk/public/Cloudflare 模式检查和 dry-run。
- self-attested/dual-review 策略在迁移 UI 和 API 中可配置。
- 自核验报告可导出不含 secret 的 manifest、SHA、门禁和时间。

**测试与门禁：**

- fresh install 无交互或最少交互完成。
- 重启、重装、恢复时凭据路径和 keyring 行为明确。
- secret 不出现在 argv、journal、SQLite、普通导出和错误响应。
- self-attested 可完成 staging 迁移；dual-review 仍能阻止未授权高风险动作。

### 阶段 D：备份 retention、SQLite 生命周期和观测（P1/P2）

**目标：** 长期运行不会让备份和事件无限增长，且清理可解释、可恢复。

**主要改动：**

- 备份 retention registry：数量、年龄、空间水位、已验证替代品和未验证宽限期。
- `deployment-maintenance.py` 与安装器统一发布；旧未验证备份输出告警而非静默遗漏。
- retention dry-run API/CLI、最近运行摘要、失败阶段、cursor 和 protected rows。
- 长期表分类、压缩冷 payload、必要时 checkpoint/VACUUM 策略。
- 追加 disk usage、backup growth、retention lag、provider health 指标。

**测试与门禁：**

- 90 天 cutoff fixture 实际删除和不删除边界。
- retention 中断后 cursor 续跑，失败记录可诊断。
- GC 保留 current/previous/运行进程引用/最新恢复点。
- 未验证备份不被错误删除，也不会无限期静默占满磁盘。

### 阶段 E：长 soak 和发布（P2）

**目标：** 在修复后的合同上重新验证真实部署，而不是重复旧门禁。

- 第一台服务器仅做只读基线和升级前备份，不直接拿生产状态做破坏性测试。
- 第二台单系统盘服务器做全新安装、Provider 测试、备份恢复、升级、重启和迁移流程。
- 至少 7 天 soak：每 5 分钟记录 live/ready、每次模型调用结果分类、Provider 状态、DB/backup 大小、NRestarts、内存和磁盘水位。
- 发生错误时保留前后 15 分钟结构化日志和状态快照，不保存 secret 或私密内容。
- 发布报告包含目标 SHA、测试者、门禁结果、失败原因、已知限制和回滚指针。

## 6. 验收门禁矩阵

| Gate | 验收内容 | 通过标准 |
|---|---|---|
| G1 系统盘安装 | 无独立数据盘安装并启动 | service active，数据库可写，live/ready 成功 |
| G2 可选挂载 | 独立数据盘挂载和卸载 | 正确使用挂载；丢失时不覆盖原数据 |
| G3 升级恢复 | 中断、schema、旧 release 回滚 | 状态可读，旧版本 ready，recover 幂等 |
| G4 Provider | 正确配置、错误配置、cooldown、预算 | 一次测试给出稳定原因；不误重试未知结果 |
| G5 Reasoning | 支持与不支持模型 | UI、请求、存储和回放一致 |
| G6 健康与日志 | live/ready/详细 health 和活动日志 | 状态和 reason code 一致，BrokenPipe 不刷屏 |
| G7 备份恢复 | 正常、篡改、错误 key、非空目标 | 失败不破坏原数据；验证标记准确 |
| G8 Retention/GC | 90 天 retention、备份 GC、空间压力 | 受保护数据不删，清理可恢复可解释 |
| G9 钱包与 signer | 认证、nonce、余额、费用、转账、receipt | 已完成的真实门禁全部回归通过 |
| G10 迁移审批 | self-attested 和 dual-review | 模式按策略生效，审计信息完整 |
| G11 长 soak | 7 天生命周期、Provider、健康、增长 | 无未解释重启；错误和增长在预算内 |

## 7. 发布和回滚策略

1. 每个阶段先提交纯函数/合同测试，再修改安装器、systemd 或运行时。
2. 每个 release 记录 source SHA、schema marker、DDL fingerprint、安装器版本和验证标记。
3. 生产升级前生成并验证加密备份；健康检查通过后才切换 `current`。
4. 新版本 readiness 失败自动恢复 `previous`；恢复后再次验证服务、数据库和关键路径。
5. 单系统盘模式必须先检查磁盘余量；余量不足时拒绝升级并保持旧服务。
6. 失败原因、恢复动作和下一步建议进入管理日志；不要求用户从 journal 原文推断。
7. 发布报告由实际部署者签署即可；需要双人复核的环境通过策略开启，不成为所有用户的默认阻塞。

## 8. 需要明确保留的已知限制

- 历史 `/health` 503 的单一根因无法从旧日志完全还原；修复方向是新增持久化 reason code，不继续反复猜测。
- retention 尚未跨过真实 90 天 cutoff，当前只能证明调度执行成功；真实删除必须通过 fixture 和后续 soak 验证。
- Provider 的 502/429/unknown 不能仅凭客户端日志归因到供应商余额、网络或模型推理速度；必须保留有限的耗时、状态和响应契约信息。
- 长期运行有模型调用并不等于用户私密交流一定有可见回复；调用成功、结果落库、交付成功必须分开统计。
- 独立数据盘仍然有容量、隔离和恢复价值，但不应再是普通用户启动 Noyra 的必要条件。

## 9. 实施顺序结论

先完成阶段 A，使系统盘部署能够启动并可升级；再完成阶段 B，解决“配置完成但实际不可调用”和“健康状态无法解释”的问题；随后简化凭据和迁移审批体验；最后完善备份、retention、活动日志和长 soak。完成 G1 至 G8 后再进行公开发布，G9 和 G10 作为高风险功能门禁，G11 作为发布后的持续运行证据。
