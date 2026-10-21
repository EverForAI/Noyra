# Noyra 全项目整改后逐代码只读审计（2026-10-18）

## 结论

审计对象为 `codex/phase-a-system-disk-storage`，HEAD `cadaf67130157fd1b5032f075457314fb5504ada`。本轮没有发现 P0；发现 1 项条件性 P1、6 项 P2、1 项 P3。A-E 阶段的基础实现和静态质量门禁比上一轮明显改善，但还不宜作为生产发布完成：备份卷身份没有持久化，soak timer 的状态文件路径与 systemd 写权限冲突，升级恢复与运行时对数据卷身份的判断不一致。

发现的风险等级表示问题发生时的影响，不代表发生概率；“修复风险”表示修复本身引入回归的可能性。P1-01 仅在把备份目录配置为独立挂载时触发；仅用系统盘的默认安装不会触发该项。

## 范围与证据

检查了当前分支相对 `main` 的 15 个提交和 65 个变更文件，重点覆盖 Ubuntu 安装与升级、storage identity、systemd 服务与 timer、SQLite schema/retention、backup GC、model provider/reasoning、migration approval、前端配置和相关回归测试。结合上一轮审计逐项确认旧发现是已修复、部分修复还是仍存在。

本地证据：`ruff check .`、`ruff format --check .`、`mypy --python-version 3.12 src tests`、`pip check` 均通过；针对性 pytest 为 79 passed；前端 Node 测试为 116 passed；所选 5 个 shell 合同脚本均报告通过。`systemd-analyze verify` 能解析 soak service，但 Windows/WSL checkout 的权限映射产生 executable/world-writable 警告，不能把该结果当成已安装 Ubuntu 上的权限验收。

完整 pytest（2718 项）曾启动，但在约 11% 处被中止；截断输出中途出现过一次 `F`，无法可靠定位到具体测试，随后单独重跑 `tests/test_committed_verification.py` 为 4 passed，因此没有足够证据把完整套件标为通过，也没有足够输出给该间歇性失败定级。针对性 pytest 79 passed，`audit-m41.py --scope targeted --profile smoke` 为 289 passed、7 subtests passed；前端 Node 测试 116 passed。未运行 pip-audit（Windows 本地 pip-audit 因系统编码在导入 `pip_api` 时失败）、Docker CI、目标 Ubuntu 真实安装、重启、独立挂载故障、真实 provider 或连续 soak。未连接或修改任一生产服务器。历史服务器日志只作为背景，不作为当前 SHA 的验收证据。

## 当前发现

### P1-01：独立备份挂载丢失时可能回退到系统盘目录

**位置：** `src/noyra/core/storage_mode.py:55-80, 310-363`；`scripts/install-ubuntu.sh:204-240, 582-617`；`src/noyra/service.py:2787-2816`。

**根因：** 数据目录有持久化的 `/etc/noyra/storage-identity.conf`，并会核对 mount source；备份目录没有对应的持久身份。`backup_ready` 仅判断路径存在且为目录，虽然 `backup_device` 会被报告，但不与安装时记录的设备比较。安装器的 device/inode 检查只保护单次安装过程，没有持久到后续启动或维护任务。

**触发条件：** 运维把 `/var/backups/noyra` 或 `--backup-dir` 配置为单独文件系统挂载点；该卷未挂载或挂载失败，但系统盘上的挂载点目录仍存在；随后执行备份、升级前备份或 GC。

**影响：** 备份可能成功写入系统盘目录，管理员却以为数据在独立备份卷上；GC 也可能扫描错误文件集。系统盘空间被备份消耗后还可能影响 Noyra 和系统升级。详细 readiness 会将“存在的普通目录”报告为 backup ready，无法提示它已不是预期设备。

**风险等级：** P1（条件性，影响恢复能力及系统盘容量）。

**修复风险：** 中。需要为独立 backup mount 增加可选的 root-owned identity、源设备核对及单盘合法模式，同时兼容已部署的普通目录。

**建议与验证：** 明确 backup volume 是可选 mount 还是普通目录；若配置为 mount，则在 mount 外保存身份并对缺失/错设备 fail closed。增加卸载、错设备挂载、重启后验证及确保不在系统盘生成“成功”备份的 Ubuntu 测试。

### P2-01：升级 runner 只核对数据目录是挂载点，没有核对挂载源

**位置：** `scripts/upgrade-ubuntu-runner.sh:53-75, 95-100, 185-204`；对照 `src/noyra/core/storage_mode.py:335-346` 和 `scripts/install-ubuntu.sh:582-600`。

**根因：** Python runtime 和安装器在有 `storage-identity.conf` 时会比较实际 `findmnt` source 与身份文件；root upgrade runner 的 `storage_identity_mount_available()` 仅检查身份文件存在时路径是否为 mountpoint。错误设备挂到 `/var/lib/noyra` 时，runner 仍可能先创建/检查升级控制目录或在该卷上恢复状态，之后调用安装器才因 source mismatch 失败。

**触发条件：** 专用数据卷身份文件存在，但错误卷或临时卷挂载到了 `/var/lib/noyra`；启动 upgrade 或 `--recover-only`。

**影响：** 正常安装器会在后续阶段拦住错设备，当前代码没有证据表明它会静默升级错误数据库；但 runner 和 runtime 的合同不一致，会让状态写到错误卷、恢复误标记或产生难以追踪的升级失败。

**风险等级：** P2。

**修复风险：** 中。Shell 需安全读取并校验身份格式/source，并兼容加密映射设备重启后的稳定命名。

**建议与验证：** runner 在任何目录创建、状态写入或恢复前复用与 runtime 等价的 mount-source 检查；用真实 root 模式 shell 测试覆盖正确设备、缺失挂载和错误设备，而不只检查脚本中包含 identity 文件路径。

### P2-02：soak service 的状态文件被 systemd 写保护

**位置：** `deploy/systemd/noyra-soak.service:16,24`；`scripts/noyra-soak-collector.py:331-351, 527-554`；安装启用点 `scripts/install-ubuntu.sh:1313`；合同测试 `tests/test_upgrade_deployment.py:80-97`。

**根因：** unit 设置 `ProtectSystem=strict`，唯一的 `ReadWritePaths` 是 `/var/log/noyra`；collector 默认却把 cursor/fault state 写到 `/var/lib/noyra/soak-state.json`。`main()` 先追加 JSONL，再调用 `_write_state()`；其临时文件创建和 `os.replace()` 未捕获只读文件系统错误。安装器默认启用每五分钟运行的 timer。当前 unit 测试还把 `ReadWritePaths` 精确断言为仅 `/var/log/noyra`，没有覆盖 state 文件写入边界。

**触发条件：** 使用该 unit 在 systemd 的 `ProtectSystem=strict` sandbox 中运行 collector。

**影响：** JSONL snapshot 可能先写入，但 service 随后以失败退出，cursor 与故障起始时间无法持久化；每次又按默认 15 分钟窗口查询，逐调用统计重叠，短期故障窗口也无法按预期跨采样恢复。soak 文件不是空的，但不能视为完整的连续调用证据。

**风险等级：** P2（soak/可观测性失效，不直接阻断 Noyra 主服务）。

**修复风险：** 低到中。可将 state 放到允许写入的专用目录或扩展最小写路径；需要同步处理目录 owner、隐私和 atomic replace。

**建议与验证：** 让 state 路径与 unit 写权限一致，并增加真实 sandbox 执行测试：确认 service exit 0、state cursor 每次推进、fault window 能跨采样收敛。不能只用 `configparser` 读取 unit 验证。

### P2-03：soak 活动量超过上限后没有续页，会永久漏掉窗口尾部调用

**位置：** `scripts/noyra-soak-collector.py:23, 249-283, 322-327, 536-553`。

**根因：** `model_calls` 和 `model_attempts` 各自按时间升序 `LIMIT 2000`。`limited=true` 能显示这一轮达到上限，但持久游标在 snapshot 时间而不是最后一条数据库行；下一轮从新 snapshot 时间开始，不会回读同一时间窗口内未取到的后续行。

**触发条件：** 两次采集之间任一表新增达到或超过 2000 行（当前 timer 每 5 分钟运行）。

**影响：** soak 报表的状态/错误计数和耗时分位数不完整，超过上限的尾部记录会被永久跳过。`limited` 提供了不完整提示，但没有恢复机制。

**风险等级：** P2（高调用量下的审计证据不完整）。

**修复风险：** 低到中。应使用稳定的 `(timestamp, id)` keyset 分页并设置每轮总预算，避免长查询拖累主库。

**建议与验证：** 用超过 2000 行且时间戳相同/相邻的 fixture，跨多次采集检查最终汇总完整、无重复，并确保查询被明确限制在资源预算内。

### P2-04：reasoning profile 仍用模型名前缀推定候选能力

**位置：** `src/noyra/model/reasoning.py:57-66`；`src/noyra/model/capabilities.py:161-210`；`src/noyra/model/openai_compatible.py:197-212`；`src/noyra/service.py:8293-8310, 8375-8403`；`src/noyra/web/admin.js:2852-2882`。

**根因：** capability 已按 endpoint/model 持久化 revision 和逐强度 probe 结果，前端也区分候选强度与 tested 强度，后端保存配置时要求所选强度已经测试。这一改进避免了直接把未测试强度写入正式资源。但可候选的模型/强度仍来自 `o1/o3/o4/gpt-5/codex` 前缀启发式，参数固定为 `reasoning_effort`；未匹配前缀的 endpoint alias 无法进入测试流程。profile 的 `efforts` 仍保存全部候选，`tested_efforts` 才表示该 endpoint 真正测过的强度。

**触发条件：** provider 使用模型别名或非标准前缀；兼容网关支持不同的强度集合或参数名；部署者把 `efforts` 理解为已验证集合。

**影响：** UI 可能隐藏 endpoint 实际支持的功能；候选字段与已验证字段语义容易被误读。当前保存 gate 能阻止未测强度，因此风险主要是配置能力缺失/信息歧义，不是已确认的未测参数必然进入正式调用。

**风险等级：** P2。

**修复风险：** 中。需设计能力发现协议、缓存失效和不同 OpenAI-compatible 参数兼容，避免不安全地探测真实用户输入或产生无界费用。

**建议与验证：** 将“候选能力”明确标为 heuristic，并允许用户对未知 alias 发起受限测试；将 endpoint 权威支持结果与静态候选拆成不同字段。验证 partial efforts、模型别名、过期 profile、structured production request 和旧配置迁移。

### P2-05：dual-review 的身份绑定代表 session/role，不保证两名不同的人

**位置：** `src/noyra/service.py:2169-2180, 9084-9102, 9165-9175`；`src/noyra/migration/manager.py:182-193, 245-296`；测试 `tests/test_migration_manager.py:181-208`。

**根因：** 身份绑定已从 JSON actor 字符串改为服务端生成的 token-role hash 或 session-id hash，解决了随意改 actor 及同一 bearer token 重复审批。但每次浏览器 session 使用独立随机 session ID，同一操作者可用同一账号创建两个 session；admin 与 operator token 也会形成不同 role binding。系统没有独立人类账户或外部身份提供方。

**触发条件：** 部署启用 `dual_review`，并把它解释为两名自然人审批保证；单个操作者可登录多次或持有多个角色凭据。

**影响：** 代码能证明两次请求来自不同 session/role binding，不能证明是两个人。会留下形式上的双审批记录，但不满足严格职责分离或法规要求。

**风险等级：** P2（只影响把 dual-review 用作强独立复核的部署；self-attested 流程不受影响）。

**修复风险：** 中到高。真正的多人保证需要不同主体账户、身份提供方或外部审批系统，改变现有认证/部署合同。

**建议与验证：** 产品与文档区分“不同 session 确认”和“独立身份双人审批”；若承诺后者，应绑定不可由同一凭据创建的 principal ID，并测同账号多 session、跨 role、token 轮换和撤销。

### P2-06：backup GC 在无法写 retention history 时仍可删除文件且不告警

**位置：** `scripts/deployment-maintenance.py:94-134, 503-528`；对照 `src/noyra/core/retention.py:694-728`。

**根因：** `_record_backup_gc_run()` 捕获 `sqlite3.DatabaseError` 后直接 `return`；apply 仍继续，apply 后的记录失败也不会改变命令结果。安全删除仍会按 hash 和保护 floor 检查，但“计划/删除/失败”的持久审计记录并非 apply 的成功前提。

**触发条件：** GC 正在运行时数据库忙、只读、空间不足或 retention 表不可用。

**影响：** 备份删除可能成功但没有对应的 durable run history，管理员无法仅凭 SQLite 记录重建当时的删除决策；这会削弱灾难恢复审计。代码注释称后续诊断会暴露，但本函数本身不产生警告或失败结果。

**风险等级：** P2（审计与可追溯性缺失；当前删除安全检查仍保留）。

**修复风险：** 中。若要求审计记录必须 durable，需定义 DB 不可用时是停止删除还是写入 root-only journal，避免磁盘压力时 retention 永久停摆。

**建议与验证：** 明确记录失败是否阻止 apply；至少向 stderr/journal 输出稳定错误并让结果状态可见。测试数据库锁、readonly、full-disk 模拟及删除后 run history 对账。

### P3-01：storage reparse 检查没有检查已经存在路径的全部祖先

**位置：** `src/noyra/core/storage_mode.py:116-135`。

**根因：** `_reject_reparse_path()` 从目标开始 `lstat()`，遇到第一个存在且是目录的路径就立即停止。目标路径已存在时，父目录上的符号链接不会被逐层检查；`missing` 列表也没有在找到祖先后用于反向校验。

**触发条件：** 使用可配置 data/backup 路径，且路径祖先包含符号链接；尤其是祖先可被非 root 管理的部署布局。

**影响：** 路径真实位置可能与调用者指定的存储位置不同，削弱“拒绝 symlink/reparse path”的合同。默认 `/var/lib/noyra` 的父目录通常由 root 管理，降低了默认安装的可利用性。

**风险等级：** P3（受控默认路径下较低；自定义目录需关注）。

**修复风险：** 低到中。路径归一化和逐级 `lstat` 需兼容不存在的父目录与 Windows reparse point。

**建议与验证：** 从目标逐级检查至可信根目录，或以受信 root dirfd 配合 `openat`/`O_NOFOLLOW` 建立路径；补目标本身、父级 symlink、缺失多级父目录和竞态测试。

## 明确不列为缺陷的设计项

`provider_health_attempts` 出现在 retention registry 和 estimate 输出，但当前 schema 不创建该表；provider health 测试明确断言它不存在，retention 也在估算和删除前检查表是否存在并跳过。结合 registry 注释和测试，这更像有意保留的可选/未来数据合同，而不是会导致运行错误的残留。它可能让诊断输出包含一个恒为 0 的预留项，建议在 UI/文档标明 optional；本轮不把它计入风险发现。

## 上一轮发现复核

| 上一轮问题 | 当前状态 | 证据与剩余项 |
|---|---|---|
| 专用数据挂载丢失后被误认成系统盘 | 基本修复 | installer 写外部 storage identity；runtime 对缺失/错 source fail closed。未声明的遗留手工 mount 仍需迁移身份文件。 |
| upgrade recover 缺目录只在 test mode 安全退出 | 已修复于当前代码 | production `--recover-only` 对不可用 storage/控制目录缺失返回 skipped；真实 Ubuntu 重启验证尚缺。 |
| ruff format / mypy 门禁失败 | 已修复 | 本轮本地两项均通过，CI workflow 仍需远端实际执行。 |
| backup 目录缺失时 ready 容易被误解 | 部分修复 | health 已单列 backup component；runtime readiness 可以继续 200。mounted backup 丢失但底层目录存在时仍会假 ready，见 P1-01。 |
| reasoning 靠重复模型前缀判断 | 部分修复 | endpoint/model profile、逐强度 probe、保存校验和前端 API 已有；静态候选与 alias 缺口见 P2-04。 |
| soak 没有逐调用统计和故障窗口 | 部分修复 | 加入 bounded model activity/fault window；systemd state 写入路径和超量续页问题见 P2-02/P2-03。 |
| retention/backup GC 不统一 | 部分修复 | policy、plan 和 backup run table 已有；执行成功但 audit record 写失败不阻止删除，见 P2-06。`provider_health_attempts` 是测试覆盖的可选预留项，不列作现行缺陷。 |
| dual-review actor 字符串可伪造 | 部分修复 | service 现在生成 token/session binding；不同 session 不等于不同人，见 P2-05。 |
| reparse 检查不完整 | 仍存在 | 现有函数只检查到首个存在目录，见 P3-01。 |

## A-E 阶段质量评价

以下百分比是代码目标覆盖度的工程判断，不是统计测量、真实部署验收或生产可用性承诺。

| 阶段 | 质量判断 | 已完成部分 | 未闭环部分 |
|---|---|---|---|
| A：系统盘存储 | **85%，基本达成** | 默认系统盘布局、独立数据挂载身份、缺失/错数据盘 fail closed、headroom 和目录权限合同。 | 独立备份卷身份缺失；upgrade runner 不核对 data source；第二台 Ubuntu 当前 SHA 的完整安装/重启仍未验收。 |
| B：升级/schema/recovery | **85%，基本达成** | schema fingerprint、release rollback/recovery 状态、恢复缺目录分支和控制目录权限。 | runner/runtime mount-source 合同不一致；需真实 root/systemd 的错误设备与断电/重启演练。 |
| C：provider/reasoning/health | **75%，部分达成** | 错误分类、provider health、endpoint-scoped profile、按强度 probe、保存 gate 和分层 readiness。 | reasoning 能力初始集合仍靠模型名前缀；无真实 provider 矩阵及当前 production request 形态验证。 |
| D：credentials/setup/migration | **80%，部分达成** | 凭据隔离、self-attested、服务器端身份绑定、恢复 manifest 和部署配置回滚基础。 | dual-review 不能证明独立自然人；完整低门槛 setup/凭据生成/重装恢复流程仍缺真实环境演练。 |
| E：retention/SQLite/soak | **65%，有重要缺口** | DB retention cursor、备份 retention policy/plan/history、soak snapshots、容量与 provider 诊断。 | timer state 写权限冲突、活动数据超限丢页、backup GC history 可缺失、registry 残留表；当前代码没有真实连续 soak 证据。 |

## 发布与修复顺序

当前不建议把 A-E 标记为生产闭环，也不建议仅凭本地通过的单测直接灰度到长期运行实例。优先顺序：

1. 修正 P1-01 的独立备份身份合同，并为单盘和独立备份盘分别定义预期行为。
2. 统一升级 runner、安装器与 runtime 的 storage-source 验证；在 Ubuntu root shell 下验证错卷 fail closed。
3. 修复 soak state 的 sandbox 写路径，再实现活动查询 keyset 续页；部署后先检查 timer 最近运行结果，再开始新的 soak 计时。
4. 明确 endpoint capability 的候选/已测试语义；完善 dual-review 的身份保证边界和 backup GC 审计失败策略。
5. 处理路径祖先与 registry 残留项，补真实 Ubuntu 安装、重启、独立卷拔除、备份恢复及至少 7 天 soak 的同 SHA 证据。

## 本轮整改结果

本轮按模块逐项修复并在每个模块后提交本地 commit；未推送远端，也未连接或修改生产服务器。

| 审计项 | 整改结果 | 本地提交 |
|---|---|---|
| P1-01 独立备份挂载身份 | 已修复。增加 root-owned backup storage identity；运行时、安装器和 root maintenance 都核对 mount source，系统盘普通目录仍保持合法。 | `e552cde`, `362663c` |
| P2-01 upgrade runner source parity | 已修复。runner 在创建控制状态和恢复前解析并比对 data mount source，新增正确/错误设备 shell 合同。 | `9cd7c50` |
| P2-02 soak state sandbox | 已修复。默认 cursor/fault state 改到 `/var/log/noyra/soak-state.json`，与 unit 的 `ReadWritePaths` 一致。 | `73d2468` |
| P2-03 soak keyset pagination | 已修复。model calls/attempts 使用独立 `(timestamp,id)` 游标，超过 2000 行时跨采样续读并保留各表游标。 | `73d2468`, `3e4434f` |
| P2-04 reasoning alias/capability | 已修复。候选能力与 endpoint probe 结果分离；未知 alias 可以选择受限候选并发起 probe，probe 使用与生产一致的严格 JSON schema；只有测试成功的强度可保存。 | `4aa5270` |
| P2-05 dual-review principal semantics | 已修复为显式可选严格模式。默认 self-attested/session 模式保持兼容；开启 `NOYRA_MIGRATION_REQUIRE_INDEPENDENT_REVIEW=true` 后，只有受信代理提供的 authenticated principal 才能形成审批绑定，同一 principal 无法满足第二票。 | `8f4bd83` |
| P2-06 backup GC audit durability | 已修复。apply 删除前必须成功写入 durable planned run；数据库缺失、表缺失、只读或锁错误都会 fail closed。 | `77b5f1a` |
| P3-01 ancestor reparse check | 已修复。路径检查逐层走到根目录，不在第一个已存在目录处提前停止，并覆盖祖先 symlink 测试。 | `e552cde` |

本轮收尾复核还修正了 8 项随 schema 95/99、新 capability registry 和 self-attested 默认模式变化而过期的测试夹具/断言，并将 schema 80 升级夹具改为显式复制旧列，避免 `SELECT *` 把当前 schema 误当成历史 schema。迁移测试修复分别提交为 `4238b2b` 和 `e3eb6b6`。

最终针对审计项的 pytest 子集为 131 passed、2 subtests passed；受影响的完整测试文件集合为 131 passed、2 subtests passed；M41 smoke gate 为 289 passed、7 subtests passed。全仓静态复核通过：9 个 shell 合同、`ruff check .`、`ruff format --check .`、`mypy --python-version 3.12 src tests`、`pip check` 和 `compileall`。全量 pytest 运行完成，结果为 2666 passed、59 skipped、285 subtests passed、1 failed；唯一失败是 Windows 本地 HTTP 测试 `test_retired_erasure_http_routes_cannot_mutate_chat` 在 DELETE 请求期间遇到 `ConnectionAbortedError`。该用例随后单独连续运行 10 次全部通过，故目前表现为全量运行中的间歇性测试/本机 socket 异常，不能据此宣称全量 pytest 全绿，也没有证据将其归因为生产行为回归。

审计中列出的 P1/P2/P3 代码问题均已按上表完成修复并提交。A-E 的代码整改与本地合同门禁已完成；真实 Ubuntu/systemd 安装重启、独立卷拔除、真实 provider 矩阵、跨机恢复和连续 soak 仍未在本机完成，因此 A-E 尚不能标记为生产环境验收闭环，也不能以本地测试替代这些证据。

### 整改后的剩余边界

1. 严格双人复核依赖可信反向代理提供 `X-Noyra-Authenticated-Principal`，并要求正确配置 `NOYRA_TRUSTED_PROXY_CIDRS`；没有外部身份源时应继续使用 self-attested/session 语义，不应把它宣传成自然人独立性证明。
2. 独立备份挂载身份只在安装器创建或验证；已有手工部署若要获得 fail-closed 行为，需要通过安装器重跑或人工建立受保护的 identity 文件并完成一次 Ubuntu 验收。
3. 本地 Windows/WSL 无法替代真实 systemd sandbox、LUKS/挂载重启、错误设备和 provider 结构化响应矩阵；这些验证必须在目标 Ubuntu 上记录同 SHA 证据。

本文件记录审计与整改状态；代码、测试和安装脚本的变更已经分别提交到当前本地分支，远端分支保持不变。
