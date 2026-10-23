# 2026-10-23 最终修复后独立只读审计

## 范围与结论

审计对象是分支 `codex/terminal-waits-operability` 在提交 `b57ee85` 后的本地代码；未连接生产、未读取真实密钥、未调用真实模型/支付或 signer。审计采用静态逐文件复核、目标路径追踪、SQLite schema/export 复现、Windows 包门禁测试和全仓静态检查。工作区内容由本轮修复产生，审计过程不修改产品数据。

结论：前一轮修复**可以保留**，没有证据支持整体回滚。整体质量评定为**高**，但不能把本地测试等同于真实发布门禁。此前 6 个审计发现的最终状态如下：

| 编号 | 最终状态 | 风险 | 修复风险 | 归因 |
|---|---|---:|---:|---|
| R1 Windows 发布者/包信任 | 代码侧已加固；外部发布门禁未完成 | P1 | 高 | 原生缺口，部分由前轮修复闭合 |
| R2 Windows 启动器契约 | 已修复 | P2 | 中 | 原生缺口，前轮修复加固 |
| R3 未知重试关闭围栏 | 已修复至本地信任边界 | P2 | 高 | 前轮新增关闭语义的次生缺口 |
| R4 embedding 配置漂移 | 已修复 | P2 | 中高 | 原生缺口 |
| R5 默认 embedding 维度漂移 | 已修复并持久化 | P2 | 中 | 前轮修复未闭合的次生缺口 |
| R6 停用 embedding 仍显示有效 | 已修复 | P3 | 低 | 前轮修复的次生显示问题 |

## 审计方法与证据

- 代码范围：Windows package/lifecycle、`model/ledger.py`、`model/resources.py`、embedding provider/resource/store、`core/database.py`、`core/runtime_export.py`、integrity、service/cognition wiring。
- 结构校验：schema 103 新库、102 -> 103 迁移、strict schema contract、runtime export ownership graph 均实际执行。
- 定向测试：`test_audit_20261022_remediation.py`、`test_runtime_export_schema75.py`、`test_schema_contract.py` 共 43 passed；embedding resource/resilience 共 15 passed；Windows package 5 passed；前端 Node 133 passed。额外 service/provider-security 过滤回归 26 passed；dimension round-trip 通过隔离 MockTransport 脚本验证。
- 静态检查：Ruff、Ruff format、Mypy 223 个源文件、compileall、`git diff --check` 通过。
- 全仓 pytest 收集 3,068 项。首次无 `PYTHONPATH` 运行在收集阶段因 `scripts` 导入路径失败；随后设置 `PYTHONPATH=.` 重跑，但在约 8% 进度时因预计执行时间过长而中断。中断前未见失败，但**全仓 pytest 未完成，不能记作通过**。

## R1：Windows 包信任

**根因**：包内 manifest 是普通 JSON，生命周期会校验文件 hash、launcher 声明和 Authenticode publisher thumbprint，但 manifest 本身没有被代码验证为由 Noyra 发布者签署。攻击者若只能篡改包而不能取得受信发布者证书，launcher allowlist 可阻断替换 launcher；但仅凭 launcher 不能证明所有 `.py`、配置和其它包文件来自同一发布。

**影响**：严格签名模式已经不再接受“任意 Valid 证书”或未声明 launcher，但完整包来源认证仍依赖外部签名发布流程。触发条件是攻击者能够替换分发包并保留/复用一个受信 launcher。

**修复与建议**：保留 `TrustedPublisherThumbprints`、manifest launcher inventory、逐文件 hash、reparse-point 防护和 clean VM 门禁。真正发布前必须增加并验证签名的 manifest/checksum bundle、真实 Noyra launcher、证书轮换和 clean VM upgrade/rollback；当前没有凭空引入未定义的签名私钥或发布协议。

## R2：Windows 启动器契约

**根因**：旧 builder 可生成没有真实 launcher 的包，而 lifecycle 后续只能安装不能启动。当前 builder 要求显式 `LauncherPath`，manifest 必须声明 `noyra.exe` 或 `noyra-desktop.exe`，start 只执行 manifest 声明的 launcher。

**影响/触发条件**：未传 launcher、launcher 不是真实文件、manifest 漏列或包被篡改时，构建/安装/start 在边界处失败；不会再产生“安装成功但必然无法启动”的官方包。

**修复风险与结论**：中风险修改已通过 Windows package/process suite；代码修复保留。仍需真实 Noyra launcher 和 clean Windows VM 外部门禁。

## R3：未知模型调用重试关闭

**根因**：旧逻辑把 prepared retry 的取消和已发出 retry 失败后的人工关闭混用。当前分别使用 `model_unknown_retry_cancelled` 与 `model_unknown_retry_closed`；路由还要求 call 状态、原始授权、error code 和对应 action 一致，关闭操作要求 evidence hash 与显式接受重复执行风险。

**影响/触发条件**：已发出的未知调用不能因为旧 cancellation 形状而解除 quarantine；一次性授权不会变成无限重试。针对三类 business purpose、并发和重启路径的测试保持 quarantine 行为。

**残余边界**：具备 SQLite 写权限的本地代码仍可伪造格式正确的 audit row。完整性检查能验证 payload/哈希格式和主体归属，不能证明哈希对应的外部供应商证据真实存在。该能力位于本地数据库写权限信任边界内；进一步关闭需要受控 operator audit 存储或外部签名证据，不能用匿名 HTTP 保护假装解决。当前不回滚本修复，也不把该本地信任边界夸大为远程可利用漏洞。

## R4：managed embedding 配置漂移

**根因**：旧 runtime 只按资源 ID 识别 provider，未绑定 endpoint、model、dimensions、timeout、budget 和 circuit policy 等持久字段。现已在 schema 102 的 `config_hash` 中覆盖这些非秘密字段；active settings、发出前 authorizer 和 integrity check 均重算并比较。API key 仍用独立 fingerprint，不进入配置哈希。

**影响/触发条件**：直接修改 SQLite 配置、旧进程继续运行或 secret 与配置不一致时，服务在启动、完整性检查或发出前拒绝，而不是继续使用过期运行参数。旧库通过 backfill 迁移；runtime export graph 对 102/103 均有显式继承条目。

**修复风险与结论**：中高。涉及 schema migration、strict contract 和 startup fail-closed；已通过 strict schema、integrity、embedding resilience、runtime export 回归，保留。

## R5：供应商默认维度漂移

**根因**：`dimensions=None` 时，供应商 alias 的实际向量维度过去只存在 provider 进程内；重启后丢失，且旧向量无法被稳定标识为另一向量空间。现新增 schema 103 的 `observed_dimensions`，provider 首次有效响应后通过受控 recorder 持久化；provider identity、`missing()` 和相似度查询使用持久观察值，变更后进入有界重建路径。

**影响/触发条件**：相同 endpoint/model alias 的供应商改变默认维度时，旧向量不再和新空间混用；重启会读取观察值，避免每次重启都把整个索引误判为缺失。配置显式 dimensions 时仍由 response contract 拒绝错误维度。

**修复风险与结论**：中。新增前向迁移和 runtime callback；没有把观察值混入 operator configuration hash，避免正常探测被误判为配置篡改。已通过 102 -> 103 strict migration、embedding identity、resilience 和人工 restart round-trip 复核，保留。

## R6：停用资源的后台投影

**根因**：列表投影过去只比较 `config_id`，没有要求资源仍为 active。当前 `runtime_effective` 同时要求 active、ID 匹配和 service runtime 的 effective 标志；发出前 authorizer 仍独立拒绝 disabled/revoked。

**影响/触发条件**：运行中停用资源后，旧内存对象可能仍保留其 ID，但管理 API 不再把它显示成有效资源，且不会因为显示修复而放松实际调用授权。

**修复风险与结论**：低；已在 f18e766 中修复，保留。

## 次生问题审查

本轮重点复查了 schema 版本、DDL/structure fingerprint、runtime export ownership graph、embedding service wiring、secret-free API projection 和 retry route fence。发现并修复了一个直接可复现的次生问题：CURRENT_SCHEMA_VERSION 已进入 102，但 runtime export 未注册 schema 102 graph，导致新库导出失败；提交 `6f2997a` 增加 102 graph 继承。随后 schema 103 为观察维度增加同样的 graph 继承。

没有发现需要回滚 b2efa08、44ad320、f18e766 的证据。没有发现 migration 回退、跨主体 export、secret 写入 SQLite、停用资源实际发出调用或旧 retry cancellation 释放已发出失败重试的回归。

## 未完成的真实门禁

以下不是本地代码测试已证明的事项：真实 Noyra Windows launcher 和 Authenticode publisher、签名 manifest/checksum bundle、证书轮换、clean VM 安装/升级/回滚/crash recovery、真实 GitHub release workflow、生产部署和真实供应商调用。它们应在发布前单独作为阻断门禁，不应以本地合成 `whoami.exe` 测试代替。

## 提交与工作区

本轮相关提交：`6f2997a`（schema 102 export graph）、`b57ee85`（schema 103 observed embedding dimensions）、`4e53d36`（格式检查修整）；前序修复提交见 `docs/audit/2026-10-23-remediation.md`。最终审计文档本身需在本次任务结束前单独提交；推送、合并 main、生产升级均不属于本轮已执行动作。
