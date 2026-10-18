# Noyra 全项目第三轮逐代码只读审计（2026-10-18）

## 结论

本轮基于当前分支 `codex/phase-a-system-disk-storage` 的 HEAD `04624d1`，工作区在审计开始时干净，未修改代码、测试、服务器或 Git 历史。本轮没有确认 P0 或 P1；发现 6 项 P2、3 项 P3。上一轮修复已经关闭了原来最危险的 storage identity、reasoning alias、soak completion、GC durability、strict principal 和 signer receipt 边界，但仍有若干跨入口合同没有完全统一。

最值得优先处理的是：旧 signer state 的 receipt 兼容、retention resume 对 plan identity 和大批量删除的限制、服务启动仍不把 backup readiness 作为准入、环境文件 reasoning alias 与管理 API 语义不一致，以及 stale capability 在管理返回中仍显示为已选择。它们不会把默认部署直接升级为 P0，但会在升级、长期运行或特定配置下造成恢复确认失败、维护任务阻断或“配置看似成功但调用被阻止”。

## 范围与证据

检查范围包括 `src/noyra`、`scripts`、`deploy/systemd`、部署样例、前端管理页面、SQLite/retention、storage identity、升级 runner、soak collector、Provider/reasoning、self-hosted signer、迁移审批及相关测试。重点对照上一轮修复提交 `da44f22`、`117555f`、`da17cb1`、`06c5ccc`、`52f9ff0`、`1383551`、`d618912`，检查新代码的兼容性、跨入口一致性和失败恢复路径。

本地验证证据：

- 当前分支工作区干净，HEAD 为 `04624d1`，相对远端领先 36 个提交。
- `ruff check .`、`ruff format --check .`、`python -m mypy --python-version 3.12 src tests`、`pip check`、`python -m compileall -q src scripts` 均通过。
- 上一轮修复后的全量 pytest 记录为 `2683 passed, 59 skipped, 288 subtests passed`，无失败。
- storage、reasoning、soak、retention、signer、安全配置等专项测试已通过；Ubuntu shell 合同在本地 bash 环境中通过。

这些检查不能证明 GitHub Actions、Docker、Python 3.11 矩阵、真实 Ubuntu/systemd/LUKS、root ownership、真实 Provider、跨机恢复、真实 signer/RPC 或连续 7 天 soak。Windows 上跳过的 POSIX/root/boto3 测试必须在目标环境重新执行。

风险等级定义沿用前两轮：P0 为不可逆数据损坏、严重越权、密钥泄露或普遍无法启动；P1 为明确条件下的数据分叉、恢复失效、升级边界失守或高概率发布阻断；P2 为可恢复但影响功能、证据、运维判断或长期可靠性；P3 为低影响边界错误、诊断或合同清晰度问题。修复风险表示修改本身引入回归的可能性。

## 当前发现

### P2-01：旧 signer state 的 receipt 无法兼容迁移

**位置：** `src/noyra/signer_service.py:164-180`、`361-385`。

**根因：** `SignerState.open()` 对已有 `signer_requests` 表只追加 `transfer_json` 列，历史行的该列保持 `NULL`。新的 `receipt()` 要求解析并校验 `transfer_json`，因此旧版本已广播的 request 即使链上已经有 receipt，也会返回 `receipt request envelope is unavailable`。代码没有保存旧 envelope 的完整 `to/value/data`，也没有版本化迁移或明确的兼容状态。

**触发条件：** signer 在 `1383551` 之前已经创建过 request，随后升级服务并查询旧交易 receipt；或者恢复一个旧 signer SQLite 后执行 reconciliation。

**影响：** 旧交易无法通过 signer receipt API 完成确认，钱包支付/迁移 reconciliation 可能长期停留在 unknown 或需要人工核对。新 request 的 envelope 绑定是正确的，但升级后的历史可观测性被破坏。若运维据此重试广播，风险取决于上层 unknown fencing；signer 自身不会对同一 request 自动重播。

**修复风险：** 中。不能凭 tx hash 反推出完整 envelope；需要在升级前导出可信的旧 request metadata、把不可恢复的历史标为 `legacy_unbound`，并在客户端区分“无回执”和“历史合同不可验证”。

**建议与验收：** 增加 `schema_version`/迁移状态；旧行迁移为明确的 `legacy_unbound`，receipt 返回稳定的不可验证错误而不是通用 400；新行继续强制 envelope binding。升级 fixture 应覆盖旧 9 列表、旧 broadcast、旧 unknown、重复 request 和恢复后的 receipt 查询。

### P2-02：resumable GC 没有把 persisted `plan_id` 与当前 plan 绑定

**位置：** `scripts/deployment-maintenance.py:157-198`、`638-756`。

**根因：** `_load_pending_backup_gc_run()` 读取 pending cursor 时返回数据库中的 `plan_id`，但 `apply_resumable_gc()` 在 resume 时没有比较它与当前重新计算的 `plan["plan_id"]`。每个尚未完成 action 会重新检查 marker/hash，但已经被外部删除的 action 会直接被计数并跳过；旧 plan 的整体策略、保护集合或 policy revision 没有被作为一个不可变合同核对。

**触发条件：** maintenance 进程中断后，管理员改变 retention policy、current/previous release、protected backup、目录内容或运行参数，然后重新执行 `gc --apply`。

**影响：** 部分 action 的 hash 复核仍能阻止明显误删，但 resume 可能继续执行一个不再代表当前保护策略的旧 action 集合，或把已经被外部删除的对象当作已完成而不产生 plan mismatch 诊断。审计记录中的 plan id 与本次实际策略不一致，恢复分析困难。

**修复风险：** 中。严格要求 plan id 相等可能阻止合法的“中断后目录只少了一个文件”的恢复，需要把“同 plan 继续”和“重新规划”分成稳定的显式分支。

**建议与验收：** resume 前强制比较 persisted `plan_id`、policy digest、protected names digest 和 action count；任一变化返回 `backup_retention_resume_plan_changed`，由下一次运行创建新 plan。对象已消失可以作为同 plan 的幂等完成，但必须仍能证明它属于原 action 并记录 `already_absent`。

### P2-03：GC 对超过 1000 个 action 直接阻断，没有真正的批量执行

**位置：** `scripts/deployment-maintenance.py:34`、`648-668`。

**根因：** `MAX_RESUMABLE_GC_ACTIONS = 1_000` 被作为单个 plan 的硬拒绝阈值。虽然 cursor 可以跨进程续跑，但超过阈值的 release/backup 删除不会被分批规划或消费，而是直接返回 `backup_retention_plan_too_large`。

**触发条件：** 长期运行、备份数量增长、release 堆积或 policy 一次产生超过 1000 个删除 action。

**影响：** retention 在高容量实例上无法执行，磁盘压力会持续增长；运维看到的是 blocked，而不是可逐批推进的计划。该项不直接导致误删，属于可恢复但可能造成生产空间耗尽的 P2。

**修复风险：** 中。批量执行需要处理 action 顺序、每批 durable cursor、policy 变化和锁持有时间；设计不当会重复删除或留下不一致状态。

**建议与验收：** 将 1000 改为每批上限而不是全计划上限；持久化 total actions、batch size、next index，单次只消费一批并返回 `partial`/`remaining`。超过批次后重启能继续，policy 或 protected floor 改变时使旧计划失效。测试 1001、10000 action、SIGTERM、DB lock 和磁盘接近满。

### P2-04：服务和部署 setup 仍允许 runtime ready 但 backup 不可用

**位置：** `src/noyra/__main__.py:127-147`、`src/noyra/service.py:10527-10534`、`deploy/systemd/noyra.service` 的 `ExecStartPre`、`src/noyra/deployment_setup.py:360-390`。

**根因：** 本轮已经加入 `storage-check --require-backup`，但 systemd `ExecStartPre` 仍只调用普通 `storage-check`，服务构造也只拒绝 `StorageMode.UNAVAILABLE` 或 data root 不可写；`backup_ready` 仅作为 health component。setup preflight 同样只校验 runtime storage。于是备份目录缺失时服务可以继续 ready。

**触发条件：** backup directory 被删除、backup mount 未挂载、backup identity mismatch、磁盘权限变化，而 data volume 仍然可写。

**影响：** 普通 `/health/ready` 与服务状态会继续报告可运行，但升级前保护、GC、restore 和备份创建会在更晚阶段失败。CLI 有 opt-in 的严格检查，却没有成为生产启动/部署流程的默认明确分层。

**修复风险：** 中。直接要求 backup ready 会破坏有意只运行系统盘的部署；更安全的是把 runtime-only 与 recovery-capable 两种 profile 显式化，并让 upgrade/restore/GC 强制后者。

**建议与验收：** 增加 `NOYRA_REQUIRE_BACKUP_READY` 或 deployment profile；在管理 setup 输出中明确 `runtime_ready=true, backup_ready=false` 是降级而非完整 ready；systemd、upgrade 和 setup 各自测试选择的 profile，不依赖 operator 记住额外 CLI 参数。

### P2-05：环境变量模型组仍拒绝已通过 probe 的非标准 alias

**位置：** `src/noyra/model/resources.py:4015-4108`、`src/noyra/model/capabilities.py:161-224`、`src/noyra/service.py:8404-8557`。

**根因：** 管理 API 可以先对 alias 做 endpoint probe，并把 tested capability 存入数据库；但 `resource_groups_from_env()` 没有数据库上下文，遇到非标准 prefix 且配置了 reasoning effort 时直接抛出 `reasoning effort for model aliases requires an endpoint probe`。环境文件/credential 配置路径无法调用 probe，也无法引用已存在的 capability profile。

**触发条件：** 生产使用 `NOYRA_ECONOMY_MODEL_GROUPS_FILE` 或 systemd credential 配置 vendor alias 和 reasoning effort，而不是通过管理 API 创建资源。

**影响：** 同一个 Provider/model 在 UI 配置路径可以“测试后保存”，在环境配置路径却永远无法启动；部署者可能把它误判为 provider 不支持 reasoning，或为了启动删除 reasoning 配置。能力合同跨入口不一致，定 P2。

**修复风险：** 中。环境启动不能无界调用真实 provider；需要引入预先签名/持久化 capability profile、显式 `require_probe=false` 的安全策略或 setup 阶段 bounded probe。

**建议与验收：** 统一 resource source：环境配置可引用 `capability_revision`/profile digest，启动只接受 subject/endpoint/model 匹配且未过期的 tested profile；没有 profile 时稳定拒绝并给出 setup 命令。测试 UI、env file、systemd credential、profile expiry 和 endpoint change。

### P2-06：stale capability 的 public payload 仍显示旧 selected effort

**位置：** `src/noyra/model/capabilities.py:20-38`、`src/noyra/service.py:481-500`、`src/noyra/web/admin.js:2856-2875`。

**根因：** `StoredCapability.public(selected=...)` 只判断 selected 是否属于 `efforts`，不判断 `status == "known"` 或 selected 是否属于 `tested_efforts`。资源列表因此可以返回 `status="stale"` 但 `selected="high"`；前端会显示候选强度和“需要测试”的状态，而已保存资源的 selected 字段仍看起来像已生效。

**触发条件：** capability TTL 到期、Provider endpoint/model revision 变化或旧 profile 被标为 stale；随后打开管理资源列表或读取资源详情。

**影响：** 运维看到旧强度仍被选择，但实际 `_gateway()` 会阻断 `model_capability_probe_required`，造成“配置存在、服务正常、调用却没有发生”的诊断困难。这与此前第一台服务器 GPT 配置后无调用的现象具有相同的可观测性风险，定 P2。

**修复风险：** 低到中。清除 selected 的展示值可能让用户误以为配置丢失，需要同时返回 `configured_effort`、`effective_effort` 和 stable blocked reason。

**建议与验收：** public payload 对 stale/unknown/untested 返回 `selected=null`，增加 `configured_selected` 与 `effective_status`；管理台显示“已配置但当前未验证”，调用 ledger 记录阻断 reason。测试 TTL 到期、probe refresh、旧配置恢复和 UI reload。

### P3-01：soak 输出轮转存在 append 与截断之间的竞态窗口

**位置：** `scripts/noyra-soak-collector.py:524-563`。

**根因：** snapshot 先用 `O_APPEND|O_NOFOLLOW` 打开并 fsync，超过上限后再用普通 `path.open("wb")` 截断写尾部。两个动作之间没有文件锁或 atomic replace；`O_NOFOLLOW` 只保护第一次 open，不能保护后续的 truncate。

**触发条件：** 两个 timer 实例重叠、管理员/日志工具同时读取或写入 output，或具有同目录写权限的进程替换 output。

**影响：** 可能丢失最近 snapshot、把两个 writer 的记录交错或截断到不完整 JSONL。默认 systemd timer 通常串行，故定 P3；但连续 soak 证据可能出现无法解析的窗口。

**修复风险：** 低到中。加入 flock 或 atomic temp+replace 需要兼容 Windows 测试和 root-owned log directory。

**建议与验收：** 对 append/rotation 使用同一 lock file；rotation 写临时文件、fsync、`os.replace`，并用 `O_NOFOLLOW`/lstat 重新核验目标。测试两个并发 collector、SIGTERM 和 output 替换。

### P3-02：signer HTTP handler 把内部错误统一成 400 且不记录诊断

**位置：** `src/noyra/signer_service.py:450-472`。

**根因：** `do_POST()` 捕获所有非 `SignerError` 异常，返回 `{"error":"request rejected"}` 和 HTTP 400；没有记录安全的错误类型、request route 或 correlation id。RPC 不一致、旧 state envelope 损坏、SQLite 错误和程序 bug 对客户端都表现为同一 400。

**触发条件：** signer state 迁移异常、RPC 返回结构异常、数据库不可写、代码回归或未知 Python exception。

**影响：** 客户端无法区分可重试的服务故障、永久请求错误和 unknown broadcast；运维只能依靠进程是否重启或底层 RPC 日志定位。错误本身不会把 secret 写入响应，但会放大支付/迁移排障成本，定 P3。

**修复风险：** 低。需要避免把异常文本或敏感 payload 写入日志。

**建议与验收：** 为内部异常返回稳定的 500/503 error code，日志只记录 route、exception class、request id 和 sanitized state；保留 `SignerError` 的 400/409 语义。测试 RPC error、DB error、legacy envelope error 和 response body 无 secret。

### P3-03：storage mount source 检查不是一个原子 mount/open 合同

**位置：** `src/noyra/core/storage_mode.py:142-160`、`340-424`；shell runner 的 `findmnt`/目录操作。

**根因：** runtime 先调用 `findmnt` 读取 source，再以普通 `Path`/SQLite 打开目录；mount 可以在两次操作之间被卸载、重新挂载或切换。代码没有使用持有 mount fd 的 `openat`/mount namespace，也没有在数据库打开后再次核验 device identity。

**触发条件：** root 运维并发执行 mount/unmount、自动解锁/云盘重连、错误设备快速替换，或恶意 root 级别操作。

**影响：** 极端竞态下检查通过的 source 与实际打开的 filesystem 不同，可能绕过 intended fail-closed identity。需要 root 级别能力或真实设备故障才容易触发，定 P3/P2 边界；当前没有 Ubuntu 重挂载竞态证据。

**修复风险：** 中到高。fd-based mount namespace 设计复杂，错误处理可能阻断正常启动。

**建议与验收：** 至少在 database open 前后重复 source/device 检查并在变更时退出；Ubuntu 使用 mount namespace/人工快速切换测试，记录不会打开另一份数据库。若产品威胁模型不包含 root 并发操作，应在部署文档明确边界。

## 上一轮修复质量评价

| 上一轮修复 | 质量评价 | 结论 |
|---|---|---|
| storage identity path/source fail-closed | 高 | `da44f22` 修复了最危险的静默 fallback，并给 runner 补权限合同；仍需处理多实例 global identity 和 mount TOCTOU 的产品边界。 |
| reasoning alias/capability | 中高 | `117555f` 统一了 API 保存和调用准入，probe schema 也收紧；环境变量入口和 stale selected 展示仍未统一。 |
| strict dual-review | 中高 | HMAC、时间窗、方法/路径和 trusted CIDR 已明显强于 header trust；它仍是“受信身份系统的双主体断言”，不是自然人证明。 |
| soak completion/state | 高 | `52f9ff0` 修复了进行中行漏记并显式标记 state reset；output rotation 竞态和无界 SQLite activity scan仍是低等级边界。 |
| backup GC/retention | 中 | durable plan、终态记录和 resume 已实现，测试覆盖了中断续跑；plan binding 与 >1000 action 的实际生产可用性还不完整。 |
| signer receipt | 中 | 新 request 的 envelope binding、canonical checks 和 confirmation clamp 正确；旧 signer SQLite 行没有迁移合同，HTTP 诊断仍弱。 |

## A-E 完成质量

### A：系统盘存储与启动 — 约 88%

无独立数据盘模式、identity fail-closed、目录合同和升级 source 校验已形成稳定代码边界。剩余问题是 backup readiness 没有进入默认启动 profile、global identity 不支持多实例隔离，以及真实 Linux mount/reboot 证据尚缺。

### B：升级、schema 与恢复 — 约 84%

schema fingerprint、rollback/recover、目录权限和 durable GC 依赖已完成。signer 独立 state 兼容与 GC plan binding 仍是跨版本恢复边界；真实 systemd/root/断电演练尚未完成。

### C：Provider、reasoning 与 health — 约 80%

Provider failure 分类、health projection、endpoint capability 和 alias 保存/调用已经明显改善。环境资源入口、stale selected 语义和不同兼容网关的 reasoning 参数发现仍未完全实现；真实 Provider 矩阵未验收。

### D：凭据、部署向导与迁移审批 — 约 84%

凭据隔离、self-attested、HMAC principal 和代理清理边界较完整。严格模式仍依赖外部身份中间件，setup 尚未提供完整的低交互 secret/profile/rollback 向导；不能把 HMAC principal 宣传为两名自然人。

### E：retention、SQLite、活动日志与 soak — 约 82%

retention registry、verified floor、resumable action cursor、soak completion/state reset 和 secret-free snapshots 已具备主体能力。大批量 GC、plan binding、output rotation 和真实 7 天连续证据仍需补强。

## 原规划目标与剩余工作

原规划 G1-G11 仍不能全部标记通过。当前代码回归已经通过，但 G1-G8 的真实 Ubuntu/恢复/升级证据、G9 signer/RPC 真实链路、G10 provider 矩阵和 G11 连续 soak 仍需同 SHA 取证。第一台服务器曾长期运行但调用很少，只能证明部分生命周期稳定性，不能证明 Provider、reasoning、backup 和故障窗口完整。

建议剩余顺序：

1. 先定义 runtime-only 与 recovery-capable 两种 storage profile，并让 setup/systemd/upgrade/restore 使用明确语义。
2. 为 signer 旧 state 增加迁移/legacy-unbound 合同；为 GC resume 加 plan digest 绑定并改为真正的批量消费。
3. 统一 API、environment resource 和 frontend 的 capability/stale selected 语义。
4. 加入 soak output lock/atomic rotation、signer 结构化 5xx 日志和 mount source 重复核验。
5. 在 Ubuntu 当前 SHA 执行安装、重启、LUKS/错误设备、旧 state restore、GC 中断、真实 Provider 和至少 7 天 soak；随后执行 GitHub CI/Docker/pip-audit。

## 审计限制

本轮只读检查代码、测试、部署配置和既有记录；没有修改源代码、测试、服务器或 Git 历史。Windows 本地静态检查和 pytest 不能替代 Linux-only 权限、systemd、LUKS、真实 RPC、真实 Provider、跨机 restore 与连续 soak。以上风险等级是代码合同和证据边界判断，不是对生产主机当前状态的远程保证。
