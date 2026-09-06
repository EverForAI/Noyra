# Gate 1 安全与幂等修复报告

日期：2026-08-19

分支：`codex/gate1-security-idempotency`

Gate 0 基线：`2f897e6` (`docs: record final Gate 0 verification`)
Gate 1 实现提交：`55f2607`、`38ac9e6`、`f7545f3`

## 结论

Gate 1 规划中的 R2-P1-06、R2-P1-07、R2-P1-08、R2-P1-09、R2-P1-11、
R2-P1-12 和 R2-P2-17 已完成代码修复、故障注入回归和静态检查。本阶段只关闭安全、
密钥历史、secret 双写、共享预算和 outcome-unknown 幂等边界；没有进入 Gate 2。

最终发布门禁仍按既定决定保持延期，包括：正式 MSI/MSIX 与签名证书、clean Windows VM、
真实 GitHub tag release、Sigstore/provenance、生产主机恢复演练、多日 soak、完整 S/M 数据和
答案级评测。

## 已实施

### R2-P1-06：私有根、reparse/device 与密钥文件权限

- `validate_private_root` 和 `validate_private_file` 统一拒绝 symlink、Windows reparse point、
  非普通条目和设备边界跨越；新建私有树后统一收紧权限。
- at-rest 检查从历史固定目录扩大到整个持久私有树，subject、workspace、training、export、
  cache、secret 及其后代使用同一权限和设备边界合同。
- Windows ACL 加固和审计覆盖整棵私有树；volume attestation 要求 SYSTEM/Administrators
  特权 owner，并拒绝普通 service account 修改 attestation。
- archive keyring 通过统一 `validate_keyring_path` 验证路径、文件类型、owner 和 ACL；构造时
  同时验证 key ID、AES-256 material、fingerprint、active/retired 状态和唯一 active key。

### R2-P1-07：secret 文件 durable intent 与双向恢复

- schema version 从 41 升至 42，新增 `secret_file_intents`，在任何 secret 文件发布前持久化
  create intent，在 resource revoke 的同一个 SQLite 事务内持久化 delete intent。
- transport、search、cognitive 和 embedding 四类 secret store 均接入：
  `prepared -> file_ready -> committed`，以及 `pending/failed -> removed` 删除流程。
- cognitive group 的增补 key（`add_keys`）同样先建立 create intent，再发布文件并在资源事务
  完成后提交；部分失败会清理 final/temp 文件并关闭对应 intent。
- create publish 的完整异常窗口都在补偿边界内：写入、fsync、rename、`mark_file_ready` 或
  resource transaction 任一点失败，都会删除 final/temp 文件并将 intent 收敛为
  `removed`；删除失败时保留无 secret 内容的 `failed` 证据供启动恢复。
- SQLite trigger 与应用状态机共同禁止非法跳转、operation/state 混用、跨主体 intent
  复用、secret reference 跨 resource/intent alias、intent identity/operation 重绑定和
  terminal `removed` 复活；intent 行本身 append-only，`INSERT OR REPLACE` 也不能覆盖
  历史 intent；不存在的 intent 更新 fail closed。
- 启动 reconciliation 同时处理：有 intent 无 row、有 row 无 intent、revoked row 遗留文件、
  active row 缺失文件、孤儿 final/temp 文件、旧 cleanup queue 和 bounded scan 饥饿问题。
- 旧 `secret_cleanup_queue` 重试路径也在 unlink 前执行全局 owner 反查；伪造 queue reference
  指向 active resource 时只会进入 `failed`，不会绕过 intent 保护删除有效凭据。
- reconciliation 在每次 unlink 前反查四类资源的全局 reference owner；即使数据库 trigger 被
  删除或旧版本数据库含有恶意 intent，也会先将 intent 标记 failed，绝不删除另一资源正在使用
  的 secret 文件。
- `/health` 汇总 transport/search/cognitive/embedding 四域 cleanup；保留原
  `embedding_secret_cleanup` 字段用于兼容。
- transport revoke 现在是不可逆终态；凭据已删除后不能把旧 row 重新 enable，恢复必须重新
  configure。
- deep integrity 在既有 transport check 内验证 secret intent 的资源类型、主体归属、reference、
  operation/state、fingerprint、intent 间 alias 和 durable resource 对应关系，不改变 registry
  v1 的外部清单。

### R2-P1-08 / R2-P2-17：archive keyring 全局 ledger

- keyring generation 改为“全局连续 ledger + 主体 catch-up”：全局首次 generation 可从现有
  生产代数 bootstrap，随后新全局代必须严格 `N+1`；新主体和休眠主体可直接接入当前全局代，
  不再错误地要求每个主体从 generation 1 开始或逐代在线。
- migration 42 trigger 阻止全局跳代、主体回退、重复/倒序写入，以及在全局推进后补写过时
  generation；同一当前 generation 的主体 catch-up 仍被允许。
- 同一 generation 在所有主体上必须具有相同 metadata hash、active key 和 legacy key；同一
  key ID 的 fingerprint 在全局历史中永久绑定，复用或冲突立即 fail closed。
- revision 的 canonical JSON、key 数量、唯一 active key、metadata hash、state hash、时间顺序
  均由 runtime 和 integrity verifier 使用同一验证合同。
- segment 的 key ID/fingerprint 不一致不再被跳过；event 与 observation segment 的全局引用
  都必须被当前共享 keyring 解析，否则抛出 integrity/key-unavailable 错误。
- deep integrity 允许合法的主体 generation 跳代，同时独立验证全局 generation 连续性、
  同代冲突、历史 fingerprint 和主体 chronology；报告计数仍只返回当前主体的数据。

### R2-P1-09：统一脱敏

- 新增统一 recursive redaction，覆盖 runtime export、developer log 和 interaction projection。
- 覆盖 token/session token/refresh token、cookie/set-cookie、Authorization/Bearer、API key、
  password/credential、PEM/private key 以及 secret/private-key reference。
- 支持嵌套 dict/list/tuple、JSON string 和 header-like string；保留非 secret 的
  `key_fingerprint`、`encryption_key_fingerprint`、`token_count`、`max_output_tokens`，避免
  过度脱敏破坏诊断。

### R2-P1-11：group 与 aggregate pool 原子预算

- 删除 gateway 的独立 aggregate pool 预检查；group 和 pool attempts/tokens/cost 现在在
  `ModelLedger.authorize_attempt` 的同一个 `BEGIN IMMEDIATE` 中计算和授权。
- physical call 的 `resource_group_id` 以 durable row 为权威，调用者不能在授权时伪造另一
  group；只有显式 `continuation=True` 且上一 attempt 已终止时才允许 call retry。
- 并发不同 group 争抢 pool limit=1 时最多一个获得授权；并发相同 physical call 首次授权也
  只有一个 owner。

### R2-P1-12：outcome unknown 的 durable quarantine

- routed logical request 会扫描所有匹配 physical rows；`unknown`、`executing`、未经授权的
  `prepared`、损坏 route binding 均形成 durable quarantine，禁止自动换 group/key。
- operator API 新增 `outcome/status: retry`。显式 retry 复用同一 call、group、key 和 physical
  idempotency key；一次授权只允许一次 replay。已知失败的 operator reconciliation 会释放
  unknown fence，成功 reconciliation 则从 durable response 恢复，不再调用 provider。若
  operator 在 retry 尚未开始时取消 prepared call，会在同一事务中追加
  `model_unknown_retry_cancelled` supersession audit，避免旧授权把逻辑请求永久锁死。
- logical request 从 route 检查到 provider 完成由进程内 keyed lock 和按完整 request digest
  命名的跨进程 `ProcessLock`
  `ProcessLock` 共同串行；竞争者立即得到 `routed_model_call_in_progress`，不会把
  `ModelCallStateError` 当成可 failover 的 provider 错误。
- audit 授权查询增加 `json_valid(payload_json)`，损坏 JSON 不能授予 retry，也不会通过
  SQLite `json_extract` 触发请求级 DoS。
- route row 数量有硬上限；多重 retry authorization、malformed physical key 或 group binding
  不一致均 fail closed。
- physical route 的 logical idempotency component 改为 URL-safe base64 编码的 v2 prefix；旧版
  raw-delimiter route 只在从最终 pool marker 重建出的完整 logical key 精确相等时兼容，避免
  `foo` 与 `foo:pool:...` 发生前缀别名并错误复用另一请求的成功结果；重复/损坏的 legacy
  delimiter route 会被识别为 malformed quarantine，不会被忽略后自动换 key/group。

## 兼容性与迁移

- 当前 schema：42。升级前继续使用已有的 verified pre-migration backup/restore 流程。
- migration 42 是 `secret_file_intents` 和 archive global-generation trigger 的唯一 schema
  owner；optional feature 初始化会补齐 intent 表和最新版状态/reference/intent-binding/
  identity/append-only trigger，并为每个 SQLite 连接启用 recursive triggers，便于开发期
  同版本恢复且阻断 `INSERT OR REPLACE` 的隐式删除绕过。
- 原有 transport/search/cognitive/embedding secret reference 格式没有改变；启动时会为 legacy
  durable rows 建立基线 intent。
- cognitive resource 的后续 `add_keys` 路径也纳入同一 prepare/file-ready/commit 补偿合同；
  旧版本已存在的 key row 仍按启动 reconciliation 建立基线 intent。
- `/health` 的旧 `embedding_secret_cleanup` 保留；新客户端可读取聚合 `secret_cleanup`。
- model call reconciliation 继续接受 `status` alias，同时新增 `outcome=retry`；OpenAPI 使用专用
  `ModelCallReconciliationInput` 描述 succeeded/failed/retry 合同。

## 验证记录

专项与组合回归：

- archive/at-rest/secret/redaction：**99 passed, 1 skipped**；唯一 skip 是当前 Windows 测试环境
  无法创建测试 symlink 的条件分支；secret intent 最终专项（含 alias、identity、append-only、
  reparse、`add_keys`、legacy cleanup queue 和恢复路径）为 **28 passed**。
- model unknown、archive keyring、secret intent 的最新 Gate 1 组合：**50 passed**（包括
  malformed legacy route quarantine）；其余 aggregate budget/ledger/gateway/operator 回归
  继续通过此前记录的专项集合。
- runtime export/log redaction：**17 passed**。
- service/operator health：**35 passed**。
- Gate 1 最终全量 `pytest -q`：**822 passed, 3 skipped, 104 subtests passed**；3 个 skip
  均为当前 Windows 环境无法提供 POSIX/文件系统 symlink 的条件分支。

静态门禁：

- `ruff check src tests`；
- `ruff format --check src tests`；
- strict `mypy src tests`；
- `compileall`；
- `git diff --check`。

## 残余风险与明确边界

1. 私有 root/ACL/device 验证已阻止非特权账户替换路径；但“验证 pathname 后再
   open/read/replace/unlink”仍不是内核句柄级原子合同。若威胁模型包含能以 service account
   身份并发修改私有目录的本地攻击者，后续应在 POSIX 使用 `openat`/`O_NOFOLLOW`/dirfd，
   Windows 使用 reparse-safe handle。该残余风险不会被本报告表述为已消除。
2. archive keyring 被明确建模为全局共享 keyring，因此任一主体的 key metadata 或 segment
   fingerprint 损坏会使全局验证 fail closed；这是保密优先的设计，但增加了跨主体故障半径。
3. 跨进程 model route lock 按完整 logical-request digest 命名；进程异常退出时 OS 自动释放
   lock，小型 lock 文件可保留复用。锁竞争只会造成保守的临时 `in_progress`，不会允许重复
   provider side effect。
4. secret reconciliation 与 publish 目前共享 durable intent/owner 复查，但还没有把整个
   “prepare -> 文件发布 -> resource commit”序列放进同一个 per-directory OS lock；极端并发
   启动扫描与新发布交错时仍存在 pathname/filesystem TOCTOU。单实例正常启动已由 intent
   保护，若威胁模型包含同一目录的并发 writer，后续应增加共享 `ProcessLock` 并在 unlink
   前再次做 owner/intent CAS。本阶段不把该边界表述为完全消除。
5. secret intent 的 reference binding 是历史永久绑定；删除/撤销后的 reference 不能重新分配
   给另一资源。若未来需要人工复用文件名，必须先设计带迁移证明的新 reference，而不能直接
   删除 journal 行；完整数据库文件被特权管理员任意改写时，trigger 仍可被删除，系统依靠
   全局 owner 反查和 integrity fail-closed 发现并隔离这类损坏。
6. 本阶段没有实现 Gate 2 的 HTTP absolute deadline、pinned DNS、S3 readiness/lease、
   subject-scoped mutator、storage worker、bounded response、前端取消、token placeholder 拒绝和
   installer 原子升级。这些仍按全面复审报告的 Gate 2 顺序保留。
7. 生产主机恢复、多日 soak、正式安装包/签名、真实 release/Sigstore/provenance 和完整答案级
   评测仍是最终发布门禁，不能用本地单机测试替代。

## Gate 决策

本报告只关闭 Gate 1。实现提交和最终验证已完成；Gate 2 未开始，等待用户决定是否进入
Gate 2。
