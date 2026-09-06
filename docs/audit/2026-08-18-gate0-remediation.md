# Gate 0 Runtime-Boundary Remediation

日期：2026-08-18  
分支：`codex/gate0-runtime-boundaries`  
基线 checkpoint：`03a58db` (`chore: checkpoint M42 remediation state`)

## 目标

Gate 0 只处理会破坏主体所有权、生命周期 fencing、启动完整性隔离、后台任务排空和训练同意权威源的运行时边界。Gate 1 及发布候选阶段的签名、真实 release、clean Windows VM、soak 和生产恢复演练仍然保持暂停状态。

## 已实施

### R2-P1-01：ownership 早于初始化

- `NoyraService` 在 `AtRestGuard.prepare`、`StorageLayout.create`、`Database`、secret store 和 HTTP/export 构造之前取得 canonical SQLite process lock。
- 构造失败通过同步失败清理包装释放 startup lock；`from_env` 失败也走统一 close。
- `SubjectKernel` 对已有 owner 的第二实例使用只读预检 facade，不执行 schema、WAL、training policy、identity 或 project-clock 写入；获得锁后才 promote 为 writable。
- `NoyraHTTPServer` 不再因“kernel 持锁”而在完整性门禁前启动 export worker。
- cognition project execution clock recovery 延迟到 integrity 通过后的 `bootstrap`。

### R2-P1-02：runtime epoch / operation lease

- 新增 `core/admission.py` 的 `RuntimeAdmissionGate`、`OperationLease`、生命周期控制 scope 和 `commit_scope`。
- pause、safe-pause、reset、quarantine、shutdown 会递增 epoch 并关闭 admission；resume/reset 成功后以新的 lifecycle version 重新开放。
- `Database.transaction()` 在存在当前 lease 时自动进入短事务 commit fence；普通 HTTP mutation 先取得 request lease，恢复接口使用显式 recovery allowlist，路由检查与最终事务之间的 quarantine/pause 竞态会被拒绝。
- cognition cycle、embedding rebuild、model gateway、world fetch、project phase、research/prototype artifact publication 和最终 proposal commit 在 await 返回及 durable commit 前检查 lease。
- provider 已返回但 runtime epoch 失效时，model ledger 仍记录终态，认知 proposal 被丢弃并返回 `cognition_interrupted`；不会写 degraded 伪错误。
- 失效 project execution session 写入零增量 `recover`，不计入 pause/offline 时间。

### R2-P1-03：shutdown drain

- HTTP server 停止接纳新连接，追踪 active handler，并在 `close()` 返回前等待所有 handler 完成；移除 daemon handler 的静默超时释放路径。
- service 统一追踪 `asyncio.to_thread` worker；shutdown 顺序为关闭 admission、停止 listener、等待 HTTP handler/完整性 worker/线程 worker、关闭 cognition/delivery/export，再释放主体锁。
- cloud archive、common knowledge、embedding rebuild、外部 embedding executor 和 SMTP worker 支持 checkpoint/等待，避免外层 await 取消后遗留未登记 writer。
- HTTP delivery lookup 优先提交到 service 主 event loop，避免共享 AsyncClient 跨 loop 使用。
- async handler bridge 在 timeout 时取消真实主循环 task，并在 handler/lock drain 前等待其结束。

### R2-P1-04：startup integrity quarantine

- startup recovery pending 期间默认关闭 runtime admission。
- 完整性 pause/quarantine 期间 HTTP 普通 interaction、配置、授权、训练 policy、普通 export 和 lifecycle mutation 返回 `503 integrity_quarantine`。
- 仅保留明确的 action/model/delivery reconcile/lookup recovery 路由；health、诊断和已存在 export 查询仍可读。
- 完整性恢复、secret repair、resource sync、export ownership 和 cognition bootstrap 全部完成后才重新开放 admission。

### R2-P1-05：durable training consent

- `.env`/`ServiceSettings` 只作为 policy version 1 的首次 bootstrap 默认值。
- 已有主体的 policy version 大于 1 时，重启不会再用环境默认值覆盖 API 撤回或修改。

### R2-P1-10：delivery cancellation

- `_deliver` 在写入 durable `unknown` 后重新抛出原始 `CancelledError`。
- `deliver_pending` 在首个取消后停止处理后续队列项。
- `_finish` 使用 `status='sending'` CAS，迟到 worker 不能把已 reconciled/unknown delivery 改回 delivered。
- SMTP blocking worker 被追踪并在 dispatcher drain 前等待。

## 新增验收

新增 `tests/test_gate0_runtime_boundaries.py`，覆盖：

- 第二 service 构造在第一 owner 持锁时不改变 SQLite/WAL；
- 训练同意撤回后重启仍保持撤回；
- startup integrity quarantine 下 HTTP mutation 返回 503 且 interaction 行数不变；
- operation lease 在 epoch 失效后拒绝提交；
- in-flight HTTP handler 未完成时 `close()` 不返回；
- delivery 取消后第一条为 `unknown`、第二条保持 `queued`。
- 普通 HTTP mutation 在 allowlist 检查后发生 quarantine 时不会落盘。
- pause 失败会以新 epoch 恢复 active admission；paused 状态的 clean integrity report 不会重新开放 admission。
- service/kernel close、boot exception 和 run shutdown 在释放 lock 前等待 active leases；async delivery lookup 不会阻塞 service event loop。
- stale model result、research/prototype artifact 和 project execution clock 不会跨 epoch 提交。

## 验证记录

已通过的专项检查：

- Gate 0 专项回归：`tests/test_gate0_runtime_boundaries.py` **16 passed**，并通过 integrity runtime、training consent、embedding resilience、operator controls、archive/common-knowledge、project execution 和 service shutdown 相关回归；
- 全量 `pytest -q`：**762 passed, 3 skipped, 104 subtests passed**（约 14 分 21 秒）。3 个 skip 是 Windows 环境不支持 POSIX/portable symlink 的既有条件分支。
- `ruff check`；
- `ruff format --check`；
- strict `mypy src tests`；
- `compileall`；
- `git diff --check`。

## 残余风险与边界

1. 低层 `SubjectKernel(database_path)` 为保持旧构造兼容仍有 probe-lock 后再打开数据库的窗口；生产主体路径已由 `NoyraService` 的 pre-acquired canonical lock 覆盖。后续可在不破坏旧 API 的前提下增加 strict-ownership 构造模式。
2. 不可终止的第三方进程若永久不返回，shutdown 会保持主体锁而等待，不会为了“正常退出”释放 fencing；运维层仍需 supervisor 终止整个进程。
3. Gate 0 没有关闭本次审计中列出的 at-rest 子根验证、S3 lease、DNS TOCTOU、跨主体 training provenance、pool budget CAS、导出 sanitizer、安装器原子升级等 Gate 1/P1-P2 项。
4. 真实 SMTP/S3、生产主机断电恢复、多日 soak、签名证书/MSI/MSIX、GitHub tag/Sigstore/provenance 等发布门禁仍按用户决定留到最终发布阶段。

## Gate 决策

本报告只记录 Gate 0，不自动进入 Gate 1。只有在最终全量验证结果确认后，才向用户汇报是否建议切换到 Gate 1；用户未明确批准前不继续处理 Gate 1 项。
