# Noyra 全项目第六轮逐代码只读审计（2026-10-19）

## 结论

本轮以 2026-10-19 修复后的当前工作区为基线，重点复核上一轮唯一 P1-01：环境 capability profile 是否在启动 integrity/ownership 门禁前写入 SQLite。该问题已经关闭，本轮没有发现 P0 或 P1。

当前仍有 4 项 P2、3 项 P3（P3-03 按用户要求暂缓）。剩余问题集中在 retention sidecar 的长期效率和失败证据、environment resource 的 disabled/revoked 状态合同、capability provisioning 产品入口，以及完整 Windows suite 的迁移激活时序稳定性。它们都没有被升级为 P1，因为当前代码有 fail-closed 或可重试边界，且没有确认数据越权、不可逆损坏或普遍无法启动。

## 复核范围与证据

复核了当前工作区的 `src/noyra/service.py`、capability profile store、resource sync、deployment maintenance、storage profile、signer、wallet client、soak collector、migration activation、systemd/deployment 配置和相关测试。

验证证据：

- `ruff check .`：通过；
- `ruff format --check .`：685 个文件通过；
- `python -m mypy --python-version 3.12 src tests`：通过；
- `python -m compileall -q src scripts`：通过；
- P1-01 定向测试：`test_capability_profile_import_is_deferred_until_boot` 通过；
- targeted suite：`36 passed, 1 skipped`；
- `pytest --lf -q`：通过；
- 重点组合 suite：`115 passed, 1 skipped`；
- 完整 Windows pytest 最近一次：`2696 passed, 59 skipped, 288 subtests passed, 1 failed`。唯一失败是完整长运行中的 migration activation local-wallet 时序超时；同一测试单独运行、整文件运行和 `pytest --lf` 均通过，因此当前作为 P3/P2 级稳定性风险保留；
- 真实 Ubuntu root/systemd/LUKS、Python 3.11、Docker、真实 signer/RPC/provider、跨主机恢复和连续 soak 尚未以当前工作区 SHA 取证。

## P1-01 关闭复核

**问题：** 环境 capability profile 在 `NoyraService.from_env()` 阶段直接调用 `CapabilityProfileStore.import_profiles()`，早于 ownership、startup integrity、quarantine 和 admission gate。

**修复：** `from_env()` 现在只通过 `_read_environment_capabilities()` 读取、解析并缓存非敏感 payload 到 `_pending_capability_profiles`。`boot()` 在完成 `kernel.acquire_ownership()`、`integrity.run_startup()`、pause/quarantine 判断、`recover_after_integrity()` 和受控恢复后，才在 `_complete_boot_after_integrity()` 中执行 `import_profiles()`。pause 模式有 findings 时直接 return，不会导入；导入异常发生在 boot 事务边界内并触发 fail closed。

**证据：** 新增服务测试在 `from_env()` 后断言 `import_profiles` 调用次数为 0，调用 `boot()` 后才为 1；静态检查和 targeted suite 通过。

**结论：** P1-01 已关闭。当前没有发现其他 P1。

## 当前 P2 问题

### P2-01：GC manifest 每批从头扫描，超大计划存在 O(n²) I/O

**位置：** `scripts/deployment-maintenance.py` 的 `_read_gc_manifest_batch()` 和 `_gc_manifest_matches_current_plan()`。

**根因：** manifest 已移出 SQLite 并按批返回 action，但每次批处理仍从文件开头重新计算完整 SHA-256 和 action count；resume 前还会再次完整扫描 manifest 与当前目录。

**触发条件：** 10000 以上 release/backup action，或维护任务被中断多次。

**影响：** 内存已受限，但磁盘读取和 hash 计算可能随批次数平方增长，retention 可能超出维护窗口，导致磁盘压力持续增加。没有直接误删路径，风险为 P2。

**修复风险：** 中。引入 byte offset/index 后必须保留 action 顺序、完整 digest 和 crash resume 合同。

**建议：** cursor 保存 manifest offset、batch digest、action index；每批从 offset 读取，最终一次完整 digest 校验。对 1001、10000、100000 actions 做 I/O 和重启测试。

### P2-02：失败 GC manifest 的证据保留期不足

**位置：** `_cleanup_orphan_gc_manifests()`。

**根因：** 当前将没有 `status='planned'` 的 manifest 都视为 orphan 并删除，包含 `failed` run。数据库保留 failure code 和 deleted_count，但不保留完整 action 顺序。

**触发条件：** GC 中途失败，随后下一次 maintenance 启动。

**影响：** 误删排查和恢复审计缺少原始计划，长期运维证据不完整。不会直接导致错误删除，定 P2。

**修复风险：** 低到中。保留 sidecar 会增加空间，需要定义 purge 期限。

**建议：** failed manifest 至少保留到 retention audit 保留期结束；数据库记录 manifest path/size/digest；仅 applied 或明确过期的 manifest 才允许删除。

### P2-03：environment resource 无法明确恢复 disabled/revoked 同名资源

**位置：** `NoyraService._sync_cognitive_resources()`、`CognitiveResourceStore.list()`。

**根因：** 同 label 的 durable resource 被找到后，代码比较配置并继续；没有把 `status` 纳入匹配，也没有区分 operator disabled、permanent revoked 和 environment desired state。

**触发条件：** 管理员停用/删除同名资源后重启带有该环境 group 的服务，或恢复旧数据库后启动。

**影响：** service 可启动但 active pool 为空，模型调用不发生；配置看似存在而实际调用为零，形成 P2 级可观测性和配置语义问题。

**修复风险：** 中。自动 enable 可能覆盖管理员意图，自动 recreate 会改变密钥生命周期。

**建议：** 为 environment source 增加 ownership/fingerprint；disabled 由显式策略决定恢复或 fail closed；revoked 只能审计后新建，不能静默复用。

### P2-04：capability profile 仍缺少完整的管理 API/CLI provisioning 入口

**位置：** `StoredCapability.export()`、`CapabilityProfileStore.import_profiles()` 与 `/api/config/model-capabilities`。

**根因：** 内部 export/import 已具备 endpoint/model/revision/digest/expiry 校验，但 API 只返回 public payload，前端没有导出或导入操作，用户仍需手工拼装受保护 JSON。

**触发条件：** 新服务器、数据库恢复、systemd credential profile 轮换、vendor alias 首次部署。

**影响：** provisioning 容易误抄字段、误用过期 profile 或写错 endpoint，导致服务启动 fail closed；产品易用性和支持成本仍不达标，定 P2。

**修复风险：** 中。导出必须保持无 API key，导入必须保持权限和 digest 合同。

**建议：** 增加 authenticated export endpoint/CLI 和 import/preflight 命令，显示 profile diff、expiry 和 endpoint identity；补 API/CLI/systemd credential 测试。

## 当前 P3 问题

### P3-01：GC manifest 版本常量没有成为文件格式合同

`GC_MANIFEST_VERSION` 未写入 sidecar header，实际 cursor version 与 manifest format 没有明确绑定。未来 action schema 变化时会得到泛化的 integrity 错误，而不是明确版本不兼容。修复风险低，建议为 manifest 增加 version header 和兼容错误码。

### P3-02：完整 Windows suite 的 migration activation 偶发 timeout

长时间 full pytest 曾出现一次 local-wallet activation timeout；相同测试单独、整文件和 `pytest --lf` 均通过。当前没有确定性产品逻辑复现，保留为 P3；若该 suite 进入必需 CI gate，则升级为 P2。应保留 request/status/runner trace，使用 deterministic barrier 做 20 次连续 activation 压测。

### P3-03：storage mount source 检查不是原子 mount/open 合同（暂缓）

`findmnt` source 检查和普通路径打开之间仍存在 root 并发 mount/unmount 的 TOCTOU 窗口。按用户要求暂不修复，不计入 P1 清零结论。

## 上一轮修复质量评价

| 修复主题 | 评价 | 当前结论 |
|---|---|---|
| P1-01 capability import boundary | 高 | 写入已延后到 integrity/ownership/quarantine 之后，定向测试证明 boot 前数据库不变。 |
| storage profile/backup root | 高 | preview、production direct construction、storage-check、setup 和 service 的路径与默认语义已统一；真实 Ubuntu env/systemd 矩阵仍需验证。 |
| GC marker/sidecar | 中 | marker precheck 已纯读，sidecar 脱离 SQLite，1000+ actions 可分批；offset 续读和 failed evidence retention 仍欠缺。 |
| reasoning capability | 中高 | digest 绑定 endpoint/model/revision，导入校验严格，stale selected 清晰；产品化 export/import API 和 durable resource state ownership 未完成。 |
| signer receipt/error | 高 | legacy state、machine-readable code、500 diagnostics 和 wallet client 分流已覆盖；真实旧 DB/RPC 仍需 Linux 演练。 |
| soak rotation | 中高 | 合作 collector 的 lock/inode/atomic replace/fsync 合同完整；Windows 和外部写入并发证据不足。 |

## A-E 复评

- A 存储与启动：约 95%。P1 启动写入边界已关闭；P3-03 和真实 Ubuntu mount/reboot 证据仍待完成。
- B 升级、schema、恢复：约 87%。GC 和 signer 进展稳定；manifest 长期效率、失败证据和断电恢复仍需加强。
- C Provider/reasoning/health：约 89%。profile digest、stale semantics、延迟导入完成；API provisioning 和 disabled/revoked resource ownership 未完成。
- D 凭据、部署、迁移审批：约 87%。凭据与审批边界稳定；长 suite activation 稳定性和真实 Linux/systemd 仍需验证。
- E retention、SQLite、soak：约 85%。sidecar、marker、rotation 主体完成；offset/index 和失败 manifest retention 尚未收尾。

## 结论与后续顺序

P1 级别问题已清零。下一步按 P2 顺序处理：GC offset/index 与 failed manifest retention、environment resource ownership、capability authenticated export/import；然后处理 P3 版本合同和 full-suite activation 稳定性。P3-03 继续作为独立的 mount namespace/fd-based 设计任务。

本轮是只读审计与 P1 复审记录，未提交、未推送，也未修改服务器。真实 Linux root/systemd/LUKS、Python 3.11、Docker、provider/RPC、跨机 restore 与连续 soak 仍需目标环境证据。
