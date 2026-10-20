# Noyra 全项目第五轮逐代码只读审计（2026-10-19）

## 结论

本轮以当前工作区为基线，复核了上一轮修复后的全部 Python 运行时、部署脚本、systemd 单元、前端配置入口、GC/retention、capability profile、signer、soak、迁移和测试合同。工作区存在上一轮未提交修改；本轮没有修改源代码、测试、服务器或 Git 历史，只新增本审计文档。

没有确认 P0。发现 1 项 P1、4 项 P2、3 项 P3（其中 P3-03 按用户既定要求继续暂缓）。相比上一轮的 1 个确定性 P1 和 6 个 P2，存储 profile、preview 门禁、backup root、signer 错误码、soak rotation、GC marker 副作用和 capability profile 基础校验已经明显改善，但新的 capability 导入启动边界和 retention sidecar 的长期运维语义仍需处理。

本轮验证证据：

- Ruff、format、mypy、compileall：全部通过；
- 重点专项回归：`203 passed, 1 skipped`；第五轮后新增的 reasoning、GC、signer、wallet、storage、deployment setup、soak、迁移激活组合测试为 `115 passed, 1 skipped`；
- `pytest --lf -q`：通过；
- 完整 Windows pytest 最近一次：`2696 passed, 59 skipped, 288 subtests passed, 1 failed`。唯一失败是长时间全量运行中的 `test_approved_source_task_runs_real_bundle_cli_bridge_and_root_activation[True]`，同一测试单独运行、整文件运行和 `pytest --lf` 均通过，属于当前测试环境下的时序稳定性风险，尚未满足跨平台发布门禁证据；
- 未执行真实 Ubuntu root/systemd/LUKS、真实 provider/RPC、Docker、Python 3.11、跨机恢复和连续 soak，因此这些风险不能从本轮 Windows 结果中推断为已关闭。

风险定义：P0 表示不可逆数据损坏、严重越权、密钥泄露或普遍无法启动；P1 表示明确条件下的启动完整性、恢复、升级或发布门禁失守；P2 表示可恢复但会影响功能、审计证据、运维判断或长期可靠性；P3 表示低影响的诊断、兼容性或实现边界问题。每项同时标明修复风险。

## 当前发现

### P1-01：环境 capability 导入发生在启动 integrity/ownership 门禁之前

**位置：** `src/noyra/service.py:10700-10780,10895-10920`、`src/noyra/model/capabilities.py:324-455`。

**根因：** `NoyraService.from_env()` 在构造服务、但尚未执行 `service.boot()` 的完整启动恢复、process ownership、integrity quarantine 和 admission gate 前，调用 `_import_environment_capabilities()`。该方法解析受保护 profile 文件并直接对 `model_capability_profiles` 执行写事务。随后才由调用方进入 `boot()`。此前环境资源 proposal 只放入 `_pending_cognitive_resource_proposals`，在 `_complete_boot_after_integrity()` 后才同步；capability profile 导入没有遵循同一边界。

**触发条件：** 设置 `NOYRA_MODEL_CAPABILITY_PROFILES_FILE`，数据库存在 integrity finding、恢复状态未完成、旧实例仍在执行，或调用方只构造 `NoyraService.from_env()` 尚未完成 `boot()`。

**影响：** 受保护文件内容可以在数据库完成完整完整性审计前改变 durable state；导入失败会在 boot 之前留下部分初始化路径，增加恢复、审计和双实例排障复杂度。当前 process lock 已在构造阶段建立，因此不等同于任意第二实例写库，但它破坏了“所有持久写入都在 integrity/admission 边界之后”的启动合同。

**修复风险：** 中到高。需要把原始 profile payload 仅保存在内存 pending state，在 `_complete_boot_after_integrity()` 完成后再导入；导入失败必须让 boot fail closed，并保证事务回滚。改错可能让环境资源在启动时永远无法出现，或在 integrity pause 模式下被提前写入。

**建议与验收：** 增加 `_pending_capability_profiles`，from_env 只读取、结构化校验和缓存；boot 完成 startup integrity 后再调用 `CapabilityProfileStore.import_profiles()`。测试 integrity pause、启动异常、重复 boot、process lock、导入文件篡改和事务回滚。

### P2-01：GC manifest 每批都从头扫描，超大计划仍有 O(n²) 维护成本

**位置：** `scripts/deployment-maintenance.py:548-610,965-1015`。

**根因：** manifest 已从 SQLite JSON cursor 移到 sidecar 文件，这是方向正确的改进，但 `_read_gc_manifest_batch()` 每执行一批都会从文件开头读取到结尾，只把当前批保存在内存；同时每次 resume 还会通过 `_gc_manifest_matches_current_plan()` 再完整扫描一次 manifest 和当前目录。1000 项批量在 100000 项计划上会反复读取约 100 个完整文件，维护时间接近平方增长。

**触发条件：** 10000 以上 release/backup action，或者服务重启后多次续跑同一计划。

**影响：** 内存上限得到改善，但长时间 I/O、重复 hash 和 SQLite progress 写入会使 retention 任务无法在维护窗口内完成，磁盘压力仍可能持续累积。这个问题不会直接导致误删，但可能造成“安全地不清理”最终耗尽空间。

**修复风险：** 中。需要引入 manifest offset/index 或按批独立文件，并保持 digest、action order 和 crash resume 合同；索引写错会导致跳过或重复删除。

**建议与验收：** cursor 保存 byte offset、batch digest 和 action index；每批从 offset 读取，最终再做一次全量 digest 校验。对 1001、10000、100000 action 测量每批 I/O，测试 SIGTERM、重启、SQLite lock 和部分 marker 缺失。

### P2-02：失败 GC 的 sidecar manifest 会在下一次运行被删除，损失复盘证据

**位置：** `scripts/deployment-maintenance.py:154-208,522-620,948-1015`。

**根因：** `_cleanup_orphan_gc_manifests()` 将所有没有 `status='planned'` 数据库 run 的 manifest 视为 orphan 并删除，包括 `failed` run。数据库只保留 `plan_id`、cursor digest/count 和 failure code，不保留完整 action manifest；因此失败任务下一次 maintenance 运行后无法再从本机重建精确删除计划。

**触发条件：** GC 在删除中途失败、进程被终止后被标记 failed，随后下一次 `gc --apply` 或其他 maintenance 进程启动。

**影响：** 误删排查、人工恢复判断和审计复核缺少原始 action 列表。虽然已执行 action 的数据库 deleted_count 保留，但无法知道完整 protected/release/backup action 顺序，恢复证据不完整。

**修复风险：** 低到中。保留 failed manifest 会增加磁盘占用，需要明确 retention/GC 对 manifest 自身的保留期限和安全清理合同。

**建议与验收：** `failed` run 的 manifest 至少保留到对应审计记录过期或下一次显式 purge；数据库保存 manifest path、size、digest 和 failure reason。只有 `applied` run 或显式审计保留期到期后才删除。测试失败、重启、恢复和审计导出。

### P2-03：环境资源同步无法恢复同 label 的 disabled/revoked durable resource

**位置：** `src/noyra/service.py:10825-10895`、`src/noyra/model/resources.py:796-820`。

**根因：** `_sync_cognitive_resources()` 对所有状态调用 `cognitive_resources.list()`，按 `(pool,label)` 找到记录后，只比较 route、预算、密钥和 limits；匹配后直接 `continue`，没有要求 `current.status == "active"`，也没有为 disabled/revoked resource 执行 enable 或创建新 group。于是环境文件声明的 active group 可能对应 durable disabled/revoked group。

**触发条件：** 管理员曾在 UI 停用或删除同 label 资源，随后重启一个仍声明该 group 的环境配置；或者恢复旧数据库后环境同步运行。

**影响：** 服务可以启动，但 routed gateway 的 active pool 看不到该资源，模型调用被记录为不可用或根本不发生。配置文件和管理页面显示“已配置”，实际调用为零，复现了之前 GPT 配置后的诊断困难。

**修复风险：** 中。自动启用会覆盖管理员的显式 revoke 意图，自动创建会产生新 key 生命周期和审计事件；必须区分 operator disable、environment desired state 和 permanent revoke。

**建议与验收：** 为 environment source 增加 durable source ownership/fingerprint。disabled 资源由显式 policy 决定是恢复 active 还是 fail closed；revoked 资源只能新建并产生审计事件，不能静默复用。测试 active/disabled/revoked、key cleanup pending 和重启同步。

### P2-04：capability profile 有内部导入方法，但没有管理 API 的导出/导入合同

**位置：** `src/noyra/model/capabilities.py:87-118,324-455`、`src/noyra/service.py:4537-4580,10895-10920`、`src/noyra/web/admin.js:2890-2910`。

**根因：** `StoredCapability.export()` 和 `CapabilityProfileStore.import_profiles()` 已实现，但 `/api/config/model-capabilities` 只返回 `public()`；响应没有直接提供可保存的 `provider_type/base_url/model` profile 文件结构，也没有导入/导出操作。用户必须手工拼接 API 查询值、profile 字段和环境 JSON，且前端没有导出按钮或状态。

**触发条件：** 新服务器部署、数据库恢复、systemd credential 配置 vendor alias 或 profile 轮换。

**影响：** capability provisioning 在代码层存在，产品流程仍需要人工编辑内部格式。误抄 revision/digest、漏字段或导入过期 profile 会导致启动失败；操作门槛和支持成本仍高，环境入口并未真正达到可用的 provisioning 合同。

**修复风险：** 中。导出内容不能包含 API key；导入必须继续验证权限、endpoint、digest、revision 和 expiry。

**建议与验收：** 增加受保护的 authenticated export endpoint/CLI，返回完整非敏感 profile 文件；增加受保护 import/preflight endpoint 或 setup 子命令，导入前只读验证并显示 diff。测试 API、CLI、systemd credential、profile expiry 和 endpoint mismatch。

### P2-05：完整测试存在长运行迁移激活时序抖动，发布门禁尚未稳定

**位置：** `src/noyra/migration/activation.py:1020-1090`、`tests/test_migration_activation.py:524-563`。

**根因：** full pytest 长时间运行中，`TargetActivationBridge` 的 60 秒测试等待曾在 local-wallet case 触发 `activation_runner_timeout`；同一测试单独运行和整文件运行均通过。测试依赖后台线程写 request、根激活 runner 处理文件、systemd fixture 回调和 Windows 临时文件 I/O，长 suite 资源压力会放大 polling race。

**触发条件：** 完整 Windows pytest、机器负载较高、并发临时目录 I/O 或后台线程调度延迟。

**影响：** 当前全量测试不能稳定证明 release candidate 通过；CI 可能出现偶发红灯，团队可能误判迁移功能为代码回归或忽略真实时序问题。真实 Linux/systemd 下尚没有同 SHA 的长压力证据。

**修复风险：** 中。单纯增大 timeout 会掩盖真正的 runner 丢失；应先增强 runner/request/status 的可观测性和测试隔离，再决定 deadline。

**建议与验收：** 为失败测试保留 request/status/runner trace，给每次 activation 使用独立 process/thread executor 和 deterministic barrier；增加连续 20 次 local-wallet activation 压测，要求 full suite 重跑无偶发失败。保留 60 秒产品 deadline，不用无限放宽。

### P3-01：GC manifest 版本常量未参与 cursor/schema 合同

**位置：** `scripts/deployment-maintenance.py:35,180-210,900-940`。

**根因：** 定义了 `GC_MANIFEST_VERSION = 1`，但 cursor 使用的实际版本是 `4`，manifest 文件本身没有版本头或格式标记。未来修改 action JSON 字段时只能依赖 digest/解析错误发现格式变化。

**触发条件：** 后续 maintenance 版本改变 action schema，恢复旧 sidecar manifest 或混用不同 release 的 runner。

**影响：** 通常会 fail closed，但诊断只能得到 manifest integrity/invalid，无法明确指出格式版本不兼容；人工恢复和升级排障成本增加，定 P3。

**修复风险：** 低。为 sidecar 首行增加格式版本会改变 digest 和现有恢复逻辑，需要兼容旧 version 4 manifest。

**建议与验收：** 把 `GC_MANIFEST_VERSION` 写入 sidecar header 和 cursor，读取时先验证 version，再验证 action digest；保留旧 manifest 的兼容错误码。

### P3-02：完整 pytest 的偶发迁移激活失败没有稳定复现证据

**风险等级：** P3，若进入必需 CI gate 可升为 P2  
**修复风险：** 低到中  
**状态：** 当前保留

同一失败测试单独运行、整文件运行和 `pytest --lf` 均通过，因此不能把它认定为产品逻辑确定性错误；但发布门禁不能仅凭“重跑后通过”关闭。需要保留全量运行时的 runner 状态和线程调度证据。

### P3-03：storage mount source 检查不是原子 mount/open 合同（明确暂缓）

**状态：** 按用户要求不修复。`findmnt` source 检查和数据库打开之间仍存在 root 并发 mount/unmount 的 TOCTOU 窗口；本轮不把它当作已关闭问题。

## 上一轮修复质量评价

| 修复主题 | 评价 | 当前结论 |
|---|---|---|
| preview/storage profile | 高 | 已消除上一轮确定性 P1，production 直接构造和 test preview 的默认语义统一；真实 systemd/env-file 矩阵仍需 Ubuntu 验证。 |
| backup root 统一 | 高 | CLI、storage-check、setup 和 service 已使用同一 env 变量；setup 的 env parser 仍需真实安装流程验证。 |
| GC marker 与大计划 | 中 | marker 预检查副作用已修复，manifest 已脱离 SQLite；每批全量扫描和 failed manifest 生命周期仍未达到长期生产级。 |
| reasoning capability | 中高 | digest 现在绑定 endpoint/model/revision，stale selected 语义清楚，导入有严格校验；API/CLI provisioning 未接通，同 label disabled/revoked 状态仍有缺口。 |
| signer receipt/error | 高 | legacy state、machine-readable code、500 诊断和客户端分流已覆盖，真实旧 signer DB/RPC 仍需 Linux 演练。 |
| soak rotation | 中高 | 合作 collector 的 inode/lock/atomic replace 合同完整，Windows 与外部写入边界未在真实并发环境验证。 |

## A-E 复评

- A 存储与启动：约 92%。系统盘、backup profile、路径一致性已稳定；P3-03 和真实 Ubuntu mount/reboot 证据仍未关闭。
- B 升级、schema、恢复：约 86%。GC sidecar 和 signer 兼容性提升明显；manifest 生命周期、全量 I/O 和真实断电/恢复仍需补强。
- C Provider/reasoning/health：约 88%。profile digest、stale 展示和导入校验完成；产品化 export/import 和 resource state ownership 仍欠缺。
- D 凭据、部署、迁移审批：约 86%。凭据隔离和迁移合同稳定；全量 activation 时序证据和真实 Linux/systemd 仍未完成。
- E retention、SQLite、soak：约 84%。分批 sidecar、marker 只读检查和 rotation 已完成主体修复；sidecar 复盘保留和大计划效率仍需收尾。

## 剩余优先级

1. 把 capability profile 导入移到 boot integrity/admission 之后，并接通 authenticated export/import API 或 CLI。
2. 为 environment resource 增加 source ownership，明确 disabled/revoked 的恢复策略。
3. 优化 GC manifest 的 offset/index 续读，保留 failed manifest 到审计保留期结束。
4. 采集 activation runner 的完整失败证据，解决 full-suite 时序抖动后再宣称 CI 全绿。
5. 在 Ubuntu root/systemd、真实 signer/RPC、Docker、Python 3.11 和跨机 restore 上重新执行同 SHA 验收。
6. P3-03 保留为后续独立 mount namespace/fd-based 设计，不在本轮扩大修改范围。

## 审计限制

本轮只读检查代码、测试、文档和既有测试结果，除本文件外没有修改代码或 Git 历史。工作区仍有未提交修改；完整 Windows pytest 曾出现 1 个长运行时序失败，虽然定向重跑通过，因此当前不能把发布门禁标记为无条件绿。真实 Linux 权限、systemd、LUKS、provider、RPC、Docker、Python 3.11 和连续 soak 仍需要目标环境证据。
