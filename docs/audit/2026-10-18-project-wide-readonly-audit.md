# Noyra 全项目逐代码只读审计（2026-10-18）

## 结论摘要

本审计针对当前分支 `codex/phase-a-system-disk-storage` 的完整代码树、部署脚本、systemd 单元、管理端代码、CI 和阶段 A-E 提交进行只读复核。审计没有修改运行代码、服务器、数据库、配置、备份或 Git 历史；本文件是本次审计唯一新增产物。

当前代码已经覆盖了不少原计划中的基础合同，但还不能把 A-E 视为“全部目标完成”或把本地测试视为真实服务器验收。最需要先处理的是专用挂载丢失时可能回退为系统盘、升级恢复单元在真实缺目录场景仍会失败，以及 CI 当前明确存在的 mypy 和 format 门禁失败。它们分别可能造成数据分叉、启动/恢复状态不清和发布阻断。

风险等级含义：P0 为数据不可逆损坏、严重越权或生产阻断；P1 为高概率造成数据/升级/安全合同失败或发布阻断；P2 为可恢复的功能缺口、可观测性不足或长期运维风险；P3 为工程质量、文档或低影响一致性问题。修复风险表示修改本身可能引入的风险，不是问题严重度。

## 审计范围和证据

审计范围包括 `src/noyra`、`scripts`、`deploy/systemd`、`deploy`、`tests`、`.github/workflows`、阶段 A-E 提交和对应计划文档。重点检查了存储检测、安装/升级回滚、SQLite schema 合同、Provider 路由和错误分类、reasoning 配置、迁移审批、备份 GC、retention、soak 采集、健康端点和发布门禁。

本地证据：`ruff check .` 通过；`pip check` 和部署静态合同通过；`ruff format --check .` 失败，报告 14 个文件未格式化；`mypy src tests` 失败，报告 53 个错误，涉及 11 个文件。全量 pytest 在审计结束时仍在运行，最终结果必须以进程退出摘要为准，不能把中途进度当成通过。CI 明确执行 pytest、ruff format 和 mypy，因此这些静态失败会阻断正常 CI/release 门禁。

真实环境证据仅沿用此前记录：第一台服务器出现过无挂载条件跳过、`noyra-upgrade-recover.service` 失败、Provider 404/429/502/unknown、BrokenPipe 日志；第二台服务器的单系统盘容器创建后安装曾失败，随后已清理。此前已经完成的备份错误 keyring、篡改、非空目标、HTTPS signer、Sepolia 转账、nonce/余额/费用等探索性门禁不在本审计中重复执行，也不被扩大解释为当前 HEAD 的全部真实验收。

## 当前发现

### P1-01：专用挂载缺失时可能被识别为系统盘并启动另一份数据库

**位置：** `src/noyra/core/storage_mode.py:177-205`，尤其是 `dedicated_expected` 和 `mode` 分支。

**根因：** 代码只有在数据目录内读到 `.noyra-storage.json` 的 `dedicated_mount` 标记，或检测到 `/etc/noyra/storage.conf` 时，才会把“应当有挂载”作为启动前提。挂载本身被卸载后，目录内标记随挂载一起不可见；没有外部 `storage.conf` 的普通专用挂载会落入 `data_is_mount == False`，随后被判为 `StorageMode.SYSTEM_DISK`。生产代码中没有找到写入 `.noyra-storage.json` 的安装路径，现有 marker 主要由测试构造。

**触发条件：** 使用自定义 `NOYRA_DATA_DIR` 或外部 fstab/云盘挂载；服务启动时挂载失败、设备未解锁或管理员先卸载挂载点；底层目录仍存在且可写。

**影响：** Noyra 可能在系统盘空目录初始化/打开新的 SQLite，旧专用卷上的真实数据仍然存在但不再被服务使用。后续写入会形成数据分叉，用户看到的主体、配置和备份可能与原卷不一致，恢复难度高。当前证据不足以判定默认 Ubuntu 安装一定触发，因为默认单盘路径由 `/etc/noyra/storage.conf` 保护；风险集中在没有外部声明的专用挂载部署。

**修复风险：** 中到高。改变启动准入逻辑可能阻止合法自定义目录启动，必须同时提供迁移/升级兼容和明确的初始化路径。

**修复方向：** 在安装或 `noyra setup` 时把存储身份写入挂载外的 root-owned 声明（例如受保护的 `/etc/noyra/storage.conf` 或 volume identity 文件），对任何曾被声明为 dedicated 的路径 fail closed；或者要求显式 `NOYRA_EXPECT_MOUNT=1` 并对未挂载路径返回稳定 reason code。增加真实 mount/unmount/reboot fixture，断言不会打开空数据库。

### P1-02：升级恢复的“缺目录安全退出”只在测试模式生效

**位置：** `scripts/upgrade-ubuntu-runner.sh:220-233`。

**根因：** `--recover-only` 在缺少 `INSTALL_ROOT`、`upgrade`、`requests` 或 `processing` 时，只有 `TEST_MODE=true` 才调用 `skip_for_unavailable_storage` 并返回 0。真实执行路径随后直接用四个 `[[ -d ]]` 检查退出 1。计划 B1 要求未初始化存储安全退出，但实现没有把该行为带到生产路径。

**触发条件：** 首次安装中断、旧版本升级目录尚未创建、数据卷刚恢复但控制目录不完整，或运维删除了升级控制目录后发生启动。`noyra-upgrade-recover.service` 会因此进入 failed。

**影响：** 服务本身可能仍可运行，但启动历史带有失败单元，`noyra-upgrade.path` 的因果关系和“是否有未完成升级”的判断变得不清楚；自动化会把可恢复的“没有需要恢复的状态”当成异常。第一台服务器曾出现过同类 `noyra-upgrade-recover.service` failed 证据。

**修复风险：** 低到中。只改变 recover-only 的无状态分支，仍需防止把真实损坏状态吞掉。

**修复方向：** 将存储不可用/控制目录不存在与状态文件损坏分开：前者写入可读的 skipped reason 并返回 0，后者保留失败；必要时由 root runner 安全创建缺失的控制目录，创建前检查所有者、模式和符号链接。补充非 `TEST_MODE` 的真实 shell 测试。

### P1-03：CI 的强制静态门禁当前不通过

**位置：** `.github/workflows/ci.yml:45-46`，以及当前分支多个阶段 A-E 文件。

**证据：** `ruff format --check .` 报告 14 个未格式化文件，包括 `scripts/deployment-maintenance.py`、`src/noyra/core/database.py`、`src/noyra/core/recovery_manifest.py`、`src/noyra/core/storage_mode.py`、升级/迁移/Provider/soak 测试等。`mypy src tests` 报告 53 个错误，包含 `storage_mode.py` 的 `os.chown/getpwnam/getuid/getgid` 类型错误、`upgrade.py`、`service.py`、`resources.py` 覆盖签名错误，以及多个阶段新增测试的未标注函数和类型不匹配。

**根因：** 阶段实现没有在提交前完成全根格式化和 strict mypy 收敛；部分类型问题是生产代码 API 契约不一致，部分是测试夹具类型不完整。

**触发条件：** 任意使用仓库标准 CI 或 release workflow 的 push/PR。

**影响：** CI 失败，发布无法按既定流程进入真实验收；同时 mypy 报出的覆盖签名、异常类型和存储权限问题可能掩盖真实运行时缺陷。它不是单纯的本地风格差异，因为工作流把这些命令定义为强制步骤。

**修复风险：** 低到中。格式化风险低；类型修复需避免用无依据的 `# type: ignore` 掩盖合同错误。

**修复方向：** 先逐条修复生产代码类型合同，再修复测试夹具注解，最后执行全根 `ruff format`、`ruff check`、`mypy` 和支持的串行 pytest。每个 ignore 必须有明确原因和最小范围，并重新审计 CI 输出。

### P2-01：备份目录缺失时服务仍可 ready，备份失败会延后暴露

**位置：** `src/noyra/core/storage_mode.py:212`、`src/noyra/service.py:10231-10238`。

**根因：** `detect_storage_layout` 在备份目录不存在时只设置 `reason_code="backup_path_missing"`，但 `writable` 仍按数据目录计算；服务构造只拒绝 `UNAVAILABLE` 或不可写，没有把 `backup_path_missing` 纳入启动准入。这样系统盘服务可以健康运行，但备份/GC 直到实际操作才失败。

**触发条件：** `/var/backups/noyra` 被删除、权限变为不可进入、单盘安装只创建了数据目录，或备份卷未挂载。

**影响：** `/health/ready` 可能显示可用，用户却无法生成恢复点；升级前的备份保护会在较晚阶段失败。若运维没有查看详细 storage reason，容易误判为“已具备可回滚能力”。

**修复风险：** 中。将备份设为 ready 必需会影响已有只运行不备份的部署，需要提供迁移/降级语义。

**修复方向：** 明确区分 `runtime_ready` 与 `backup_ready`，在详细 health 和 setup preflight 中显示；升级、GC 和恢复操作必须强制 `backup_ready`，普通 runtime 是否允许启动由产品策略显式决定，不能依赖隐式 reason code。

### P2-02：reasoning 能力判断是前后端重复的模型名前缀启发式

**位置：** `src/noyra/model/reasoning.py:27-36`、`src/noyra/web/admin.js:2839-2847`、`src/noyra/model/resources.py:282-286`。

**根因：** 后端把 `o1/o3/o4/gpt-5/codex` 前缀固定映射到四个强度和参数名 `reasoning_effort`，前端又复制了一份前缀判断。没有从 provider/model capability 得到真实支持集合、参数名或 profile version；同一模型名在不同兼容网关上的能力差异无法表达。

**触发条件：** 使用非标准兼容网关、模型别名、未来 GPT/o 系列、只支持部分强度的模型，或 provider 使用不同参数名。

**影响：** UI 可能显示强度选择但实际 endpoint 拒绝未知字段，或隐藏了 endpoint 实际支持的能力；用户会看到“保存成功但调用失败”，正是本轮要解决的配置门槛问题。

**修复风险：** 中。能力探测和旧配置兼容需要数据库/Provider 合同迁移。

**修复方向：** 将 capability profile 持久化为 provider/model revision，后端作为唯一判定源；前端只消费 capability API。保存前执行一次 bounded model test，记录支持强度、参数名和 profile version；不支持时强制清除旧值。

### P2-03：soak 采集没有逐调用结果和故障前后窗口

**位置：** `scripts/noyra-soak-collector.py:206-232`，systemd 定时器每五分钟采集。

**根因：** snapshot 记录 live/ready、systemd、内存、目录/数据库大小、Provider 聚合状态和最近 retention；没有读取 `model_calls`/`model_attempts` 的逐次分类、调用耗时/unknown 变化，也没有在 degraded 事件发生时保存前后 15 分钟日志和状态快照。

**触发条件：** Provider 低调用量、短时 429/502/unknown、ready 短暂降级或 retention 失败。

**影响：** 能证明“定时器运行”和粗粒度增长，但不能回答“哪次调用失败、失败前后是否重试、故障窗口发生了什么”。这不足以满足 E3/G11 的长 soak 证据要求，尤其是本轮真实服务器 API 调用很少的情况。

**修复风险：** 中。扩大采集范围会增加 SQLite 读负载和日志容量，必须保持脱敏和上限。

**修复方向：** 增加时间游标和 bounded aggregates：按窗口记录 model call/attempt 的状态、分类、数量、延迟分位数和 unknown；检测状态变化后生成有限窗口快照，严格排除 prompt、token 和自由文本。

### P2-04：备份 retention 已有策略/CLI，但仍不是计划中的完整 registry/API 合同

**位置：** `scripts/deployment-maintenance.py:24-180, 325-434`、`src/noyra/core/retention.py`。

**根因：** 备份 artifact GC 已支持 verified count、年龄、大小、未验证宽限期和最小恢复点，SQLite retention registry 也已存在；但两者仍是不同入口，缺少统一的备份策略持久化 registry、面向管理台的完整 dry-run/API 结果和跨进程可续跑的 backup GC cursor/运行历史。

**触发条件：** 用户同时配置数据库 retention 和备份 GC，或在多个维护进程/重启中断时执行清理。

**影响：** 策略可执行但不够统一，运维需要拼接 CLI 输出才能得到全局“保留了什么、为什么、下次何时重试”；长期磁盘压力下容易误判 retention 已覆盖 backup artifact。

**修复风险：** 中。统一策略会触及现有 CLI、管理 UI 和兼容字段。

**修复方向：** 建立持久化策略/运行记录，把 DB rows 和 backup artifacts 分成明确的 lifecycle class；保留现有 verified replacement 和 protected floor，增加 plan id、cursor、失败阶段、retry_at、protected names 的 API/UI 投影。

### P2-05：dual-review 的审批者是可重复使用的字符串身份

**位置：** `src/noyra/migration/manager.py:182-261, 860-862`，审批记录以 `actor` 字符串去重；管理会话在 `src/noyra/service.py:9000-9005` 使用固定的 `web-operator`/`web-admin` 角色名。

**根因：** `dual_review` 只要求两个不同的 actor 字符串，代码验证非空和长度，但没有把第二个审批绑定到独立认证主体、独立会话或不可伪造的身份凭证。调用层若能提交任意 actor 字符串，就可以由同一操作者制造两个名字。

**触发条件：** 高风险环境启用 `dual_review`，审批 API 的 actor 来自可控请求字段，或管理员复用同一固定角色登录。

**影响：** 产品声称的双人复核可能退化为双字符串确认，审计记录看似满足数量但不能证明独立性。用户要求单人 staging 可自核验并不受影响；这是启用高风险 dual-review 时的安全缺口。

**修复风险：** 中到高。改变认证/会话和部署方式，可能影响现有管理台用户。

**修复方向：** self-attested 与 dual-review 分开定义身份合同。dual-review 只接受服务器端认证主体 ID、不同会话和策略允许的角色，禁止从 JSON 任意覆盖 actor；记录认证事件和 approval binding。保留单人环境的 self-attested 默认路径。

### P3-01：阶段新增代码未统一格式化，且 reasoning 的类型忽略不精确

**位置：** `src/noyra/model/reasoning.py:24` 的 `type: ignore`，以及 `ruff format --check` 列出的 14 个文件。

**根因：** 阶段提交后没有执行仓库统一 formatter；reasoning 的 Literal 返回值通过宽泛 ignore 解决，而 strict mypy 已报告该 ignore 为 unused。

**影响：** 阅读和审查成本增加，CI 失败；没有单独证据表明会导致运行时数据损坏。

**修复风险：** 低。

**修复方向：** 使用精确的 `cast(ReasoningEffort, value)` 或显式分支，运行 formatter，并将格式化作为每阶段提交前门禁。

## 阶段 A-E 完成度评价

### 阶段 A：系统盘存储与启动

完成质量：**部分完成，约 75%**。已实现 `StorageMode`、路径/符号链接检查、system-disk 目录初始化、服务 `storage-check`、安装器移除旧 mountpoint condition、升级目录 owner/mode 合同和 headroom API；阶段测试覆盖普通目录、symlink 和部分 dedicated marker。

已达成：只有系统盘的默认路径可以被识别为 `system_disk`；服务不再简单依赖 `ConditionPathIsMountPoint`；安装器创建 secrets、upgrade 和 processing 目录；升级指针和回滚有安全路径检查。

未达成：专用挂载缺少外部身份时的 fail-closed 合同没有完成；真实第二台单系统盘安装 G1-G8 尚未以当前 HEAD 完成；mypy 尚未通过。完整目标需要补 P1-01、补非测试模式 recover 场景，并完成第二台机器的安装/重启/升级验收。

### 阶段 B：升级、schema 和恢复

完成质量：**部分完成，约 70%**。schema version、DDL fingerprint、persistent feature marker 和旧 release 回滚检查已经进入 Database/installer；upgrade runner 有 processing、锁和 recover-only 状态处理。

已达成：升级失败会尝试恢复 previous 指针并复查 schema；控制目录有 root/service 分层；状态文件包含失败原因。

未达成：P1-02 证明 recover-only 的生产缺目录路径仍失败；当前全量静态门禁也无法证明升级脚本与 Python 合同完整。需要补真实缺目录、进程中断、重启和旧 release readiness 的 shell/Ubuntu 验收。

### 阶段 C：Provider、reasoning、health

完成质量：**部分完成，约 70%**。Provider 错误分类、unknown、预算/attempt ledger、cooldown/half-open、health projection、live/ready/detail reason code、BrokenPipe 捕获和 reasoning 选择器已经存在。

已达成：Provider failure 不再完全依赖原始 HTTP 文本；reasoning 选项能在保存前拒绝明显不支持的模型；健康端点具备基础安全摘要和详细投影。

未达成：P2-02 的 capability profile 仍是重复前缀启发式；需要 endpoint/model capability test、revision/stale 处理和 UI 由 API 驱动。真实服务器上 GPT 配置“无调用”问题不能只凭本地合同判定，还需要保留一次配置测试、调用 ledger 和 delivery 证据。

### 阶段 D：凭据、部署向导、迁移审批

完成质量：**部分完成，约 65%**。已有 root-only credential 文件、Cloudflare 配置 dry-run/rollback、恢复 manifest、迁移 self-attested/dual-review 模式和自核验记录。

已达成：敏感值大多不进入普通 env/SQLite/journal；恢复清单不含 secret；单人测试可以使用 self-attested，不再被独立复核强制阻塞。

未达成：安装向导仍需要外部提供 operator/provider/Cloudflare 等 token，未实现计划中的“最少交互+CSPRNG 生成内部凭据”完整流程；P2-05 说明 dual-review 的独立身份合同不完整。需要把 setup/storage/credential/proxy preflight 统一为一个可回滚流程，并完成重启/重装/恢复演练。

### 阶段 E：retention、SQLite、活动日志、soak

完成质量：**部分完成，约 60%**。已实现 retention registry、90 天 runtime cutoff、backup GC policy、verified replacement/protected floor、空间摘要、soak timer 和 secret-free snapshot。

已达成：已验证备份与未验证备份分开；current/previous/运行进程引用保护存在；SQLite retention 有运行记录和失败阶段；soak 输出受大小限制。

未达成：P2-03/P2-04 仍存在。soak 没有逐调用/故障窗口，backup GC 与 DB retention 没有统一 registry/API/cursor。真实 90 天 cutoff、空间压力、跨进程续跑和至少 7 天 G11 尚未由当前 HEAD 的真实环境证实。

## 原规划目标与验收状态

原规划的 G1-G11 不应标为全部通过。G7/G9 的若干探索性异常和 signer/交易门禁已有用户实测证据；这不等于当前 HEAD 的完整 G7-G9 回归。G1-G6、G8、G10、G11 仍需要当前 SHA 绑定的第二台服务器或专门合同证据。特别是 G11 必须有连续生命周期、Provider、health、增长和未解释重启记录，不能用“运行了很久但调用很少”代替逐调用证据。

建议的剩余顺序：

1. 修复 P1-01、P1-02 和 P1-03，重新跑全根格式、mypy、串行 pytest 和 shell 合同。
2. 完成 capability API、backup/runtime ready 分层、soak 逐调用窗口和统一 retention plan。
3. 完成 dual-review 认证主体绑定；保留 self-attested 作为 staging/local 的默认模式。
4. 在第二台无独立数据盘 Ubuntu 上用最终 SHA 完成 G1-G8，保存 storage/schema/health/backup/rollback 报告。
5. 在同一台测试机运行至少 7 天 soak；发生 degraded 时保存有限的前后窗口证据。
6. 第一台长期运行实例只读确认备份、schema、provider 配置和磁盘余量，验证新备份后再灰度升级；不得把历史日志或旧 SHA 的成功输出当成当前 release 的验收。

## 审计限制

本次是在 Windows 工作区对代码和既有文档/命令证据进行审计，未连接第一台或第二台服务器，未修改任何生产状态，也未声称完成真实部署。Linux-only LUKS、systemd、权限、重启、网络代理、真实 Provider、跨机恢复和 7 天 soak 结论必须在目标 Ubuntu 环境中重新取得证据。全量 pytest 在审计结束时仍有后台进程，最终退出状态应在后续 CI/审计记录中补齐；当前已确认的 mypy/format 失败已经足以阻止把本分支称为发布就绪。
