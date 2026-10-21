# Noyra 全项目第四轮逐代码只读审计（2026-10-18）

## 结论

本轮审计针对当前工作区的全部代码、部署脚本、systemd 单元、前端管理代码、测试和上一轮未提交的修复。当前工作区不是干净的 Git 状态：上一轮修复工作有 19 个已修改文件，本轮只读审计没有修改这些代码，也没有修改服务器或 Git 历史；本文件是唯一新增的审计记录。

没有确认 P0。当前工作区有一个已经复现的 P1 发布阻断：`deploy/noyra.env.example` 的恢复能力默认值在预览测试降级为 `test` 时仍要求备份目录，导致新鲜预览服务无法构造，完整 pytest 失败。除此之外，发现 6 项 P2 和 2 项 P3。用户明确暂不修复的 storage mount source 原子性问题仍记录为 P3-03，但不计入本轮修复质量的负面项，也不提出本轮实施要求。

本轮最需要先处理的是 P1-01 和 P2-01/P2-03：先恢复完整测试门禁，再统一存储 profile 和环境模型 capability 的生命周期。retention 的批量执行已经从“超过 1000 项直接拒绝”改善为分批消费，但当前仍把全部 action 序列化在一个 SQLite JSON 字段中，不能把它描述为完全有界的计划。

## 审计范围与证据

检查范围包括：

- `src/noyra` 全部 Python 运行时、存储、数据库、provider、reasoning、signer、服务和配置入口；
- `scripts` 下安装、升级、maintenance、soak、迁移和生产 preflight；
- `deploy/systemd`、`deploy/noyra.env.example`、`.env.example`、Docker Compose 和 Ubuntu 部署文档；
- 前端 `src/noyra/web/admin.js`；
- 相关回归测试、shell 合同和历史审计记录。

当前工作区规模约为 578 个源代码、脚本和测试文件。上一轮修复后新增或修改的范围包括 signer legacy state、backup GC、storage profile、reasoning capability、soak rotation、部署文档和测试。

验证结果：

- `ruff check .`：通过；
- `ruff format --check .`：通过，682 个文件已格式化；
- `python -m mypy --python-version 3.12 src tests`：通过；
- `python -m compileall -q src scripts`：通过；
- 目标专项测试：通过；包括 signer、reasoning、environment groups、storage、deployment maintenance、deployment setup、soak 等组合共 176 项测试，Windows 上 2 项 POSIX 约束跳过；
- 全量 pytest：`2686 passed, 59 skipped, 288 subtests passed, 1 failed`。失败项是 `tests/test_public_preview_profile.py::test_fresh_preview_has_no_external_economic_or_file_authority[deploy/noyra.env.example]`，失败原因是 `StorageUnavailableError: backup_path_missing`。

Windows 环境不能替代 Ubuntu root/systemd/LUKS、POSIX ownership、boto3、真实 provider、真实 RPC、跨主机 restore 和连续 soak。上述跳过项和环境缺失项仍需要 Linux 目标机复验。

风险等级含义：P0 为不可逆数据损坏、严重越权、密钥泄露或普遍无法启动；P1 为明确条件下的数据分叉、恢复失效、升级边界失守或发布门禁阻断；P2 为可恢复但会影响功能、证据、运维判断或长期可靠性；P3 为低影响的诊断、兼容性或边界合同问题。每项同时标注修复风险，表示修复本身引入回归的可能性。

## 发现的问题

### P1-01：恢复能力 profile 在预览降级时仍强制备份，当前全量测试失败

**风险等级：** P1（发布阻断）  
**修复风险：** 低到中  
**状态：** 当前工作区未修复

**位置：** `deploy/noyra.env.example:4-11`、`tests/test_public_preview_profile.py:39-91`、`src/noyra/service.py:1256-1264,10545-10551`。

**根因：** 部署模板现在显式设置 `NOYRA_PROFILE=production` 和 `NOYRA_STORAGE_PROFILE=recovery_capable`。预览测试为了测试安全模板，把 `NOYRA_PROFILE` 改成 `test`，但没有同步把 `NOYRA_STORAGE_PROFILE` 改成 `runtime_only`。`ServiceSettings.from_env()` 因此仍得到 `recovery_capable`，`NoyraService.__init__()` 在临时目录没有 `/var/backups/noyra` 时拒绝启动。

**触发条件：** 使用生产优先模板创建本地 test/development preview；或者仅切换 `NOYRA_PROFILE` 而保留模板中的 storage profile。

**影响：** 当前提交不能通过完整 pytest；CI quality workflow 会失败。用户按文档把 profile 临时降级为 test 进行本地预览时，也会遇到与 provider、凭据无关的备份目录错误。该问题不破坏已有数据，但直接阻断发布验收。

**建议与验收：** profile 解析必须有单一权威关系：显式 `NOYRA_STORAGE_PROFILE` 时按显式值使用，预览测试应同时设置 `runtime_only`；或让测试模板和生产模板分开。至少增加 `profile=test + recovery_capable + missing backup` 的明确合同测试，并保证 `pytest -q` 重新全绿。

### P2-01：GC resume 的兼容性预检查会删除孤立 marker

**风险等级：** P2  
**修复风险：** 中  
**状态：** 当前工作区仍存在

**位置：** `scripts/deployment-maintenance.py:531-550,553-584,754-770`。

**根因：** `_resume_actions_compatible()` 为判断旧 action 是否已经完成，会调用 `_action_exists()`。当 backup 主文件已经被外部删除但 `.verified.json` marker 仍存在时，`_action_exists()` 会校验 marker 后直接 `unlink()` marker。兼容性检查本应只读，却产生了删除副作用，而且删除没有对应的 progress/audit 更新。

**触发条件：** GC 被中断；管理员或清理工具先删除 backup 主文件但留下 marker；下一次 `gc --apply` 在 resume 前执行 plan compatibility 检查。

**影响：** marker 证据可能在 resume 被拒绝、数据库锁冲突或后续 plan mismatch 前丢失。备份内容本身已不存在，但 marker 仍可用于判断“它属于已规划 action”；删除 marker 会降低恢复分析能力，并使下一次 inventory 把残留状态解释成不同的未验证对象。

**建议与验收：** 增加纯只读 `_action_state()`，兼容性检查只返回 `absent/complete/present/unsafe`，把 marker 清理放到 action 已经被 durable cursor 接受后，并写入 `already_absent` 进度。测试“主文件缺失、marker 保留”、计划变化、数据库锁和进程中断。

### P2-02：GC 虽然分批消费，仍将全部 action 放入单个 JSON cursor

**风险等级：** P2  
**修复风险：** 中  
**状态：** 部分修复

**位置：** `scripts/deployment-maintenance.py:725-745,772-775`。

**根因：** `MAX_RESUMABLE_GC_ACTIONS=1000` 已从“全计划硬上限”改成“单次消费上限”，这是正确方向；但新计划仍先由 `_gc_actions()` 生成完整列表，再把所有 action 写入 `backup_retention_runs.cursor_json`。因此 1001、10000 甚至更大的计划仍会一次性占用 Python 内存、SQLite 行空间和 hash/serialization 时间。

**触发条件：** 长期运行造成大量 release/backup artifacts，或一次 retention policy 产生大规模删除集合。

**影响：** 不再立即返回 `plan_too_large`，但在真正高容量实例上仍可能因单行 JSON 过大、SQLite 写放大或内存压力而维护失败。当前实现满足“可以超过 1000 并分批执行”的浅层合同，尚未满足完全有界的计划存储合同。

**建议与验收：** 将 plan metadata、action manifest 和 progress cursor 分离；cursor 只保存 `next_index`/batch digest，action manifest 按批或按数据库表存储。测试 1001、10000、100000 actions、SIGTERM、SQLite lock、磁盘接近满和跨进程恢复。

### P2-03：环境模型 reasoning profile 的配置合同没有完整的 provisioning 生命周期

**风险等级：** P2，部分部署会表现为启动阻断  
**修复风险：** 中  
**状态：** 部分修复

**位置：** `src/noyra/model/resources.py:203-217,4114-4121`、`src/noyra/service.py:10825-10852`、`src/noyra/model/capabilities.py:30-75`、`src/noyra/web/admin.js:2577-2588`。

**根因：** 环境资源只要填写 `reasoning_effort`，现在必须携带 `capability_revision` 和 `capability_digest`。服务启动时再从本地数据库查找同一 subject/endpoint/model 的 profile，并比对 revision、digest 和 tested effort。这保护了“启动不进行无界 provider probe”的边界，但环境文件本身没有一个独立的 capability profile source；profile 通常只能先通过管理 API probe，再手工把返回字段复制到 JSON。

**触发条件：** 新部署直接用环境文件配置 vendor alias、标准 GPT 模型或已过期 profile；数据库是新建的，或 capability profile 因 endpoint/model 变化而不存在。

**影响：** 服务会以 `environment_model_capability_profile_required` 启动失败；用户无法从错误中直接得到需要执行的 profile provisioning 操作。已存在的资源不被重新同步时还会留下旧的 reasoning 选择，实际调用在 gateway 阶段被阻断，表现为“配置页面有值但没有 API 调用”。

**建议与验收：** 提供正式的 capability export/import 或 setup 命令，输出 endpoint/model/revision/digest/expiry 的非敏感 profile；启动错误返回稳定的 setup 指引。测试新数据库、恢复数据库、profile 过期、endpoint 改变、systemd credential、标准模型和非标准 alias。

### P2-04：环境资源的 capability 更新不会覆盖同 label 的既有资源

**风险等级：** P2  
**修复风险：** 中  
**状态：** 当前工作区仍存在

**位置：** `src/noyra/service.py:10825-10832`。

**根因：** `_sync_cognitive_resources()` 只按 `(pool, label)` 判断资源是否存在，存在就 `continue`。它不会比较环境 proposal 的 endpoint、model、reasoning effort、capability revision 或 digest。环境文件更新 profile 后，旧的 durable resource 继续保留，新的 profile reference 永远不会进入 durable state。

**触发条件：** 同一个 label 的 provider endpoint、model、reasoning effort 或 capability revision 发生变化；重启服务执行环境同步。

**影响：** 配置文件与实际 durable route 分叉。若旧 profile 已 stale，调用会被拒绝；若 endpoint/model 没变但用户期望更换强度，管理 API 和环境文件显示的配置不一致。该问题不会自动越权，但会复现“运行正常、调用为零”的诊断困难。

**建议与验收：** 为环境资源建立 source fingerprint 和 capability reference；同 label 发现 fingerprint 变化时进入显式 audited replace/revoke 流程，禁止静默覆盖。测试 endpoint、model、effort、key reference 和 profile expiry 的四种变化。

### P2-05：setup 检查的 backup root 与运行时 backup root 可能不是同一路径

**风险等级：** P2  
**修复风险：** 中  
**状态：** 当前工作区仍存在

**位置：** `src/noyra/deployment_setup.py:366-390`、`src/noyra/service.py:1312`、`deploy/systemd/noyra.service:15-18`。

**根因：** setup 使用 `SetupOptions.backup_root`（默认 `/var/backups/noyra`）执行 `detect_storage_layout()`；服务运行时使用 `NOYRA_BACKUP_ROOT`。systemd `ExecStartPre` 的 `storage-check` 也没有传 `--backup-dir`，只依赖命令默认值。环境文件把 backup root 配置成自定义路径时，setup、ExecStartPre 和服务构造可能检查三个不同语义。

**触发条件：** 设置 `NOYRA_BACKUP_ROOT` 为自定义目录，或 setup 命令使用 `--backup-root` 与 env 文件不一致。

**影响：** 可能出现 setup 报备份不可用但服务实际可用，或 setup/ExecStartPre 通过后服务构造因真实 backup root 缺失退出。对恢复能力 profile 来说，这会让门禁结果不可靠。

**建议与验收：** 由一个解析函数读取并规范化 data root/backup root，setup、storage-check、service 和 maintenance 全部复用；systemd 显式传递同一配置或保证 env 变量被 storage-check 解析。测试默认路径、绝对自定义路径、identity mismatch 和重启。

### P2-06：直接构造 production `ServiceSettings` 时默认仍是 runtime-only

**风险等级：** P2  
**修复风险：** 低  
**状态：** 当前工作区仍存在

**位置：** `src/noyra/service.py:886-888,1256-1264`。

**根因：** `ServiceSettings` 字段默认值是 `runtime_only`，只有 `from_env()` 在 production profile 且变量缺失时才使用 `recovery_capable`。直接调用 `ServiceSettings(profile="production", ...)` 的库使用者、测试夹具或嵌入式启动器会绕过生产默认，缺失 backup 时仍可构造服务。

**触发条件：** 非 `from_env()` 路径直接构造 production settings，且没有显式填写 `storage_profile`。

**影响：** 同一个 production profile 在不同入口有不同恢复安全语义；生产代码若采用直接构造，会退化成 runtime-only 而没有明显错误。当前多数 native systemd 路径使用 `from_env()`，因此不是普遍启动故障，但属于跨入口安全合同缺口。

**建议与验收：** 在 model validator 中让 production profile 无显式 storage profile 时得到 recovery-capable，或把 profile 解析封装成唯一构造入口。增加直接构造 production settings 的测试。

### P3-01：soak 输出轮转只保护合作进程，仍存在外部替换竞态

**风险等级：** P3  
**修复风险：** 低到中  
**状态：** 主要合同已修复，边界仍存在

**位置：** `scripts/noyra-soak-collector.py:555-628`。

**根因：** 新实现用同一 lock file 串行 append/rotation，并用临时文件加 `os.replace()`，已经解决两个 collector 互相截断的主要问题。但 lock 只约束使用该 lock 的合作进程；外部进程仍可在 `path.is_symlink()` 检查后替换目标，`os.replace()` 不持有目标 inode。Windows 分支也依赖 `msvcrt.locking`，未形成与 POSIX `flock` 等价的跨平台原子合同。

**触发条件：** 具有日志目录写权限的外部进程替换 output，或 Windows 上发生重叠 collector/异常退出。

**影响：** 极端情况下仍可能覆盖外部写入或产生不可解析 JSONL。systemd 默认单 timer、root-owned log directory 使概率低，定 P3。

**建议与验收：** Linux 继续使用 lock + inode recheck；Windows 使用明确的独占创建/替换合同，或把 Windows 轮转标记为不支持。增加两个进程、SIGTERM、替换 output 和 lock 文件权限测试。

### P3-02：signer HTTP 错误状态仍通过错误字符串推断

**风险等级：** P3  
**修复风险：** 低  
**状态：** 主要诊断已修复，状态码合同仍可改进

**位置：** `src/noyra/signer_service.py:496-515`。

**根因：** 未知异常现在安全记录 exception class、route 和 request id，并返回 500；这是明显改进。但 `SignerError` 的 400/409 判断仍通过 `"unknown"`、`"conflict"`、`"legacy_unbound"` 是否出现在错误文本中决定。未来一个普通输入错误只要消息包含这些词，就会得到错误的 409 语义。

**触发条件：** 新增 SignerError message、legacy migration 分支或 RPC 错误文本复用关键词。

**影响：** 客户端可能把永久拒绝误判为可 reconciliation 的冲突，或把冲突误判为普通 400；当前不会泄露 secret，影响主要是重试与诊断。

**建议与验收：** 为 `SignerError` 增加受限的 machine-readable code，HTTP 状态码只基于 code；响应保留不含敏感内容的 request id。测试每个 code 的状态码和未知异常 500。

### P3-03：storage mount source 检查不是原子 mount/open 合同（明确暂缓）

**风险等级：** P3，处于 root 并发 mount 操作时可升至 P2 边界  
**修复风险：** 中到高  
**状态：** 按用户要求暂不修复

`src/noyra/core/storage_mode.py` 仍先读取 `findmnt` source，再由普通路径打开数据库；mount/unmount 在两步之间变化时没有 fd-based mount namespace 合同。本轮只记录，不把它计入已完成修复，也不建议在当前批次贸然实现。

## 上一轮及本轮修复质量评价

| 修复主题 | 评价 | 证据与剩余问题 |
|---|---|---|
| storage identity、系统盘模式 | 中高 | 目录/设备 identity 和 fail-closed 逻辑清晰，专用挂载缺失不会静默使用另一份数据库。当前 profile 跨入口默认值和 backup root 解析仍不统一。 |
| signer receipt envelope | 高但未完全闭环 | 新 request 保存完整 envelope，旧行显式标为 `legacy_unbound`，不会凭 tx hash 猜测交易内容；HTTP 内部异常已分离为 500 并记录安全 request id。客户端和迁移 reconciliation 尚未提供历史不可验证状态的专门操作提示，状态码仍靠字符串。 |
| backup GC plan binding | 中 | plan id 已扩展到 release/action 范围，>1000 actions 已能分批推进，审计 cursor 继续 durable。兼容性检查有 marker 删除副作用，全部 action 仍存一行 JSON，历史 cursor 分支还需要独立迁移测试。 |
| reasoning capability | 中高 | stale payload 已区分 `selected`、`configured_selected`、`effective_status`，管理 UI 能回传 revision/digest，调用准入仍 fail closed。环境入口现在有安全 reference，但 provisioning、过期刷新和同 label 更新还没有完整产品合同。 |
| storage profile / backup readiness | 中低 | runtime-only 与 recovery-capable 的概念清楚，服务构造和 storage-check 能在 recovery profile 下拒绝缺失 backup；当前引入 P1-01 测试回归，setup/systemd 仍可能检查错误 backup root。 |
| soak rotation | 中高 | 合作 collector 之间已经通过 lock 和 atomic replace 避免主要截断竞态，测试通过。外部替换和 Windows 独占语义仍是低风险边界。 |

## A-E 目标完成度复评

- **A 存储与启动：约 84%。** 系统盘支持、专用挂载 identity 和 fail-closed 已具备；recovery profile 的跨入口一致性、custom backup root 和真实 Ubuntu restart 证据尚未关闭。
- **B 升级、schema、恢复：约 82%。** signer legacy state 和 GC resume 有明确进展；完整 action manifest、旧 signer reconciliation、断电/进程中断后的真实 Linux 证据仍不足。
- **C Provider、reasoning、health：约 84%。** endpoint-scoped probe、stale 展示和 UI reference 已改善；环境 provisioning 和同 label durable sync 仍是主要缺口。
- **D 凭据、部署、迁移审批：约 84%。** secret isolation 和严格 principal 边界稳定；setup 仍需要统一 storage/profile/credential 配置源，真实第二台 Ubuntu 的完整合同尚未以当前工作区验证。
- **E retention、SQLite、soak：约 80%。** durable retention 与批量消费方向正确；marker preflight 副作用、unbounded manifest、真实大规模 GC 和连续 soak 仍未关闭。

## 建议修复顺序

1. 先修 P1-01，恢复完整 pytest 和 CI 门禁；同时为 production/test/runtime-only/recovery-capable 建立矩阵测试。
2. 统一 backup root 解析，修复 setup、systemd storage-check 和 service 的路径合同。
3. 将 GC compatibility 检查改为纯读，随后把 action manifest 从单个 JSON cursor 拆出，补 1001/10000 action 中断续跑。
4. 补 capability profile provisioning/export/import，并决定同 label 环境更新采用 replace、revoke 还是显式拒绝。
5. 用 machine-readable signer error code 替代字符串状态码推断，并补旧 state 的客户端 reconciliation 合同。
6. 完善 soak 的外部替换和 Windows 合同；P3-03 继续保留为后续独立设计任务。
7. 在第二台 Ubuntu 和第一台长运行服务器上，以同一 SHA 执行 recovery profile、custom backup root、旧 signer state、1000+ GC、真实 provider reasoning、重启和连续 soak 验收。

## 审计限制

本轮是只读代码审计。除新增本文件外，没有修改源代码、测试、服务器或 Git 历史。未提交的上一轮修复仍在工作区，不能视为已经合并或可发布。Windows 全量测试的 1 个失败必须在继续提交前修复；POSIX/root、LUKS、systemd、boto3、真实 provider/RPC、跨主机 restore 和连续 soak 仍需要目标环境证据。
