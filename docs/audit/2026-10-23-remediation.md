# 2026-10-23 分模块修复记录

本记录对应 `2026-10-23-independent-readonly-audit.md`。修复前先把原工作区完整提交为 `f19f84d`，之后按模块逐项修复；每个已完成模块均在提交前运行了针对性测试、静态检查和代码审阅。当前没有部署生产，也没有推送远端。

## 安全边界

- 不执行真实模型、支付、signer 或生产数据库操作。
- Windows 启动验证只使用临时合成包；不执行未经审计的合成 launcher。
- 不把 API key、签名私钥或供应商凭据写入仓库。
- schema 迁移只增加前向迁移，不改写历史 fingerprint；所有旧库回归必须通过。
- 未实现风险不通过放宽隔离、跳过审计或自动接受未知结果来“修复”。

## 模块 0：审计基线

- 基线提交：`f19f84d chore: checkpoint audited worktree before remediation`。
- 审计前工作区干净。
- 基线包含上一轮修复、旧审计证据和 2026-10-23 独立审计报告。

## 模块 1：Windows 发布库存和启动契约

提交：`b2efa08 fix: close Windows package publication and launch contract`，文档提交：`755cdb0 docs: document Windows launcher trust contract`。

实施：

- package manifest 必须声明 `noyra.exe` 或 `noyra-desktop.exe`；安装和启动只接受声明的 launcher。
- builder 要求显式 `LauncherPath`，并写入 launcher；不再制造“安装成功但必然不能 start”的官方包。
- 严格签名需要外部可信 publisher thumbprint allowlist；`Authenticode Valid` 不再单独作为 Noyra 发布身份。
- package 清单闭合、reparse point、hash、大小写冲突和复制后验证护栏保留。

验证：Windows package/process focused suite 13 passed；PowerShell AST parse、Ruff、`git diff --check` 通过。模块审阅确认：没有执行合成 launcher；没有真实证书发布流程，因此干净 VM、真实 Noyra launcher、证书轮换仍是外部门禁。

## 模块 2：未知调用重试围栏

提交：`44ad320 fix: bind unknown retry closure to audited transition`。

实施：

- “prepared 尚未发出”的旧 `model_unknown_retry_cancelled` 与“failed 重试后人工关闭”的新 `model_unknown_retry_closed` 分离。
- 路由只有在 call 状态、error code、授权记录和对应 closure action 同时匹配时才释放业务围栏。任意旧形状 cancellation 不再释放已发出且已知失败的 retry。
- 新 closure 继续要求供应商核对依据哈希和显式重复风险接受；审计验证 action 类型和 payload 形状。
- 保留一次性授权、并发事务和历史账本；没有改写未知调用结果。

验证：未知调用/三种业务 purpose、gate1、管理 API、integrity focused tests 通过；canonical forged cancellation 复现保持 quarantine 或被 integrity 拒绝；旧 prepared-cancellation 路径保留。

## 模块 3：embedding 空间、配置漂移与维度变化

提交：`f18e766 fix: bind embedding runtime to durable configuration`。

实施：

- schema 102 为 `embedding_resources` 增加 `config_hash`，以完整非秘密配置字段生成 fingerprint；旧库通过幂等列添加和 backfill 迁移。
- active settings、发出前 authorizer、完整性审计均比较配置 fingerprint 和所有预算/timeout/circuit/dimension 字段。
- 未配置维度的 provider 在收到实际响应后记录 observed dimension，并把它纳入 provider space identity；查询后不匹配的旧向量进入 bounded missing/rebuild 路径。
- desired/effective 诊断保留；停用资源不会在管理列表显示为 `runtime_effective`。

验证：embedding resource/resilience/retrieval/private-erasure 55 passed；服务运行时状态测试 2 passed；新增 endpoint/dimension/key、默认维度变化、配置漂移和 schema 迁移测试通过；全仓 Ruff/Mypy 通过。

## 最终回归边界

- 已完成模块在各自提交前均做局部审计和测试。
- 最终还需运行完整全仓 pytest、前端 Node、部署 shell、Windows CI 和发布外部门禁；本轮没有宣称这些全部已完成。
- 真实 Authenticode publisher 证书、官方可执行 launcher、签名包发布、clean VM upgrade/rollback 仍必须在 Windows release gate 完成。
- 目前工作区在最后一次模块提交后保持干净；后续若继续修复，应从最新提交开始，不要混入基线提交。
