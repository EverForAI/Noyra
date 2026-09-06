# Gate 2 外部资源与存储压力修复报告

日期：2026-08-19

分支：`codex/gate2-external-storage`

Gate 1 基线：`2cef43d` (`docs: record final Gate 1 verification`)

## 结论

Gate 2 规划中的 R2-P2-01、R2-P2-02、R2-P2-03、R2-P2-04、R2-P2-05、
R2-P2-06、R2-P2-07、R2-P2-08、R2-P2-16、R2-P2-19 和 R2-P2-20 已完成
根因级代码修复与专项故障注入。修复集中在四个共同根因：外部调用缺少统一绝对截止时间和
连接身份、云队列缺少 lease/CAS fencing、SQLite 与外部对象存储之间缺少可恢复提交协议，以及
共享存储根和压力维护缺少主体边界与写放大 admission gate。

本阶段没有进入 Gate 3。并行审查已经收口，最终全量回归、静态门禁和 Gate 2 checkpoint
已完成；本报告仍不把 Gate 2 表述为发布就绪，因为生产账户演练和用户决定延期的发布门禁
仍未执行。

## 已实施

### R2-P2-01 / R2-P2-02：统一 HTTP deadline、响应边界与 DNS pinning

- model、world、search、browser 和 transport 的外部 HTTP 调用统一使用 absolute wall-clock
  deadline。DNS、建连、等待响应头和逐块读取共同消耗同一个时间预算，不再把 socket idle
  timeout 误当成整次操作上限。
- 统一 bounded reader 在完整缓冲前限制 header bytes、body bytes、content encoding 和总时间；
  超限响应按调用语义映射为确定失败或 outcome unknown，不会被当成普通可重试错误。
- sync/async HTTP transport 在同一次连接内解析、校验公网地址，并把实际 TCP 连接绑定到该组
  已验证 IP；TLS SNI 和证书校验仍使用原始 hostname，关闭 SafeWebReader 原先“预检查一次、
  HTTPX 再解析一次”的 DNS-to-connect TOCTOU。
- 同步和异步默认 DNS resolver 共用全局最多 4 个有界隔离槽；异步路径不再把不可取消的
  `getaddrinfo` 放入 event loop 默认 executor，resolver 超时不会形成无界线程/排队并阻塞其他
  `to_thread` 工作。
- SafeWebReader 仍保留 peer-address 复核作为纵深防御，但安全性不再依赖事后 peer 检查。
- model POST 明确请求 identity encoding；429 保留已知限流重试语义，而 408/409/425/5xx、
  声明或流式响应超限都按 outcome unknown 处理，禁止在没有 provider idempotency 合同的情况下
  自动 failover 造成重复推理与计费。
- 同步 HTTP 的 deadline worker 在超时返回前执行 request-specific response-close hook，并处理
  “worker 尚未进入 response context” 的竞态；embedding 与 common-knowledge 流式响应不会把
  尚未关闭的连接留到调用方返回之后。超时清理失败不会覆盖原始 timeout 结果。

### R2-P2-03：S3 readiness、稳定 provider identity、deadline 与 circuit breaker

- botocore 配置显式 connect/read timeout、关闭 SDK 自身额外 retry，并由 Noyra 的 operation
  deadline 统一约束 put/get/head/delete/readiness 的完整尝试序列。
- 阻塞 SDK 调用使用单 provider 单槽和全局最多 4 个隔离 worker；超时 worker 被隔离，新的调用
  不会与同一 provider 的迟到调用重叠，也不会形成无界线程增长。
- readiness 从“client 构造成功”改为真实 `head_bucket()` 探测，并带 TTL cache、最近检查时间、
  错误码和 provider ID；启动与 `/health` 使用同一 readiness 状态。
- provider identity 绑定 canonical endpoint、region、显式 account ID、bucket 和 prefix。切换同名
  S3-compatible endpoint 后不会复用旧 provider 的 verified replica。
- 连续失败会打开 circuit；冷却后必须通过 readiness probe 才恢复业务调用，避免黑洞 endpoint
  在每个 maintenance tick 重复占用资源。maintenance tick 会调用 readiness；冷却到期后执行单次
  half-open `head_bucket`，成功后自动恢复，不依赖有人访问 `/health`。
- 大对象也使用单次 `put_object`，不调用会自行建立 s3transfer 线程池的 `upload_fileobj`；GET
  超时后的 response body close 使用独立的全局 4 槽、单 provider 1 槽清理 worker，避免卡住的
  read 占用 SDK 槽时连接体永远无法关闭。
- 当配置了 account ID 时，put/get/head/readiness 请求携带 `ExpectedBucketOwner`，把账户绑定从
  本地环境声明推进到 S3 服务端请求合同；兼容端点若不接受该参数会在 readiness/业务调用处失败
  关闭。仍不把这等同于生产账户权限演练或自定义 endpoint 的 DNS pinning。
- `.env.example` 与部署环境模板新增 account ID、connect/read/operation timeout、readiness TTL、
  circuit threshold 和 cooldown 配置。

### R2-P2-04：archive transfer claim、lease 与 CAS fencing

- schema 43 为 `archive_transfer_queue` 增加 `claim_token`、`lease_owner` 和
  `lease_expires_at`，并用 trigger 约束 claim/status 一致性、identity 不可变和合法状态迁移。
- worker 通过短事务 CAS claim 队列项；上传成功、失败和 dead transition 都必须同时匹配
  transfer ID、claim token 和 owner。迟到 worker 无法覆盖当前 owner 的结果，也不能把已验证
  replica 降回 failed。
- 过期 uploading lease 会恢复到可重试状态；恢复和 replica 更新同时限定当前 cloud provider
  identity，切换 provider 后不会错误修改另一 provider 的 replica。
- 上传队列的 provider 写入仍在 SQLite 事务外执行，最终状态发布使用 rowcount fencing；这避免
  长事务包住网络 IO，同时保持 single-winner 语义。

### R2-P2-19：event/observation archive staging、replay 与 orphan GC

- schema 43 新增 append-only `archive_staging_manifests`，状态为
  `prepared -> stored -> committed`，或 `prepared/stored -> abandoned -> removed`。
- 归档先持久化 immutable source selection 和 plaintext hash，再写 provider、读回并验证 stored
  bytes，最后在发布 segment pointer 的同一个短事务中把 manifest 标记 committed。
- finalize 前重新校验 subject、archive format、source-state canonical hash、item count、时间范围、
  每个 event payload hash 或 observation content hash，以及当前 source row 是否仍与 staging
  selection 一致。内容被替换、格式损坏或 selection 漂移时 fail closed。
- 启动/维护可 replay `prepared` 和 `stored` manifest；未引用 provider object 由 orphan GC 清理，
  committed pointer 或仍存活的 manifest 会阻止删除。
- 归档选择先用 SQLite `length(CAST(... AS BLOB))` 做源字节 admission，并在 provider 写入前检查
  解压后封装大小；超大首条不会先进入 Python/Provider。维护和启动路径接入有界 orphan GC，覆盖
  未引用最终对象及 aged `.awrite/.arestore/.qwrite` 临时文件，同时保留 committed/live manifest
  和队列引用对象。
- `mark_abandoned()` 使用 CAS。只有 worker 实际赢得 abandoned transition 才允许删除对象；若并发
  finalizer 已提交，replay worker 的旧快照不会删除现已成为权威的 archive object。
- `core.archive_dead_letter` integrity check 升级到 v2，按当前 subject 在预算和 checkpoint 内校验
  staging ledger 的 canonical source、状态/时间/存储元数据、对象 key、segment 和 source pointer；
  发现 split-commit 或篡改时在完整性门禁中报告。

### R2-P2-05 / R2-P2-20：主体边界与 storage-key 分域

- event 和 observation 冷归档根改为 `subject/<storage_key>/cold`；EventStore、ObservationStore
  的 archive cache 按主体隔离，不再复用共享 provider/root。
- 多主体数据库中发现旧共享 `subject/cold` 数据时 fail closed，避免把历史对象猜测归属给任一
  主体；这些字节计入 shared usage，等待显式迁移或人工处置。
- subject-scoped scanner 只把当前 storage key 的私有目录计入该主体，同时把无法归属的
  `training_raw` 和 legacy archive 文件计入 shared/training usage，避免未归属文件从 quota 中消失。
- capability、transport、search、embedding、cognitive resource mutator 和 secret/API-key 读取均
  强制当前 `subject_id`；HTTP 路由不再仅凭全局资源 ID 修改其他主体的配置。
- training provenance 在同一写事务内读取 canonical event，校验 subject、event type、privacy、
  payload hash 和时间，并由 schema trigger 再次阻止跨主体 event reference。
- 既有损坏数据库若缺少 subject storage key，不会在 service 构造阶段偷偷重建 ownership；cloud
  archive 使用只读 unavailable placeholder，让 startup integrity 流程报告并隔离损坏状态。
- Source/Observation/Claim、Transport、Embedding/Search/Cognitive resource 的读取、密钥读取和
  修订 API 均要求 `subject_id` 并使用 SQL 主体谓词；跨主体 ID 在库边界直接返回 not found。
- managed embedding secret 的完整性错误不再回退到环境凭据；delivery dispatcher 在进入 sending
  和加载 secret 前后都重查 delivery/interaction/transport 的主体一致性，损坏引用 fail closed，
  不产生外部发送副作用。

### R2-P2-06 / R2-P2-16：存储压力 admission 与 tracked maintenance

- maintenance 的第一步是只读 free-space/quota preflight。低于安全 free-space floor 或任一 quota
  已超限时，只允许 cache/export 删除，不启动 archive staging、model compression 或 snapshot
  compaction 等临时写放大操作。
- 每个写放大阶段开始前重新扫描，防止前一阶段消耗剩余空间后，后续阶段仍依据过期判断继续写。
- storage maintenance、cloud tick 和 reassess 移入 service tracked worker，并传入 runtime lease
  checkpoint；pause/quarantine/shutdown 会在阶段边界阻止继续提交，service 不会在 worker 未排空
  时释放主体锁。
- cache 与 usage 扫描使用 no-follow 目录遍历，跳过 symlink/reparse point，避免压力清理跨出受管根。
- export artifact pruning 先以 CAS 写入 durable `artifact_pruning` intent，再删除文件，最后把
  SQLite row 收敛为 `artifact_pruned`。删除失败保留可重试 intent；删除后 finalize 前崩溃时，
  下一次 maintenance 会根据 tombstone 完成收敛，不再留下数据库永久指向已删除文件的状态。
- `StorageMaintenanceResult.write_amplification_allowed` 独立于 cognition admission，覆盖
  subject/training/workspace 三个 quota 域；training/workspace 超限也会禁止 cloud staging。
- maintenance telemetry/event 在真实 `SQLITE_FULL`/`SQLITE_IOERR` 下返回保守的 fail-closed pressure
  结果，其他 SQLite 错误仍向上抛出，避免把异常吞成正常运行。

### R2-P2-07：bounded snapshot compaction

- snapshot compaction 使用 keyset 分批读取，不在 `BEGIN IMMEDIATE` 中全量 fetch/parse/compress。
- 有总字节预算时先读取 SQLite 侧 `length(CAST(state_json AS BLOB))`，超过预算的 TEXT 不会先物化
  到 Python；通过预算后才读取正文并做 hash/JSON 校验。
- candidate 解析和压缩在事务外完成；最终短事务重新核对 source row 的 identity/hash 并通过 CAS
  发布 archive row，两个并发 compactor 最多一个获胜。
- 每次 maintenance tick 最多处理 2,048 行、64 MB source bytes；超出预算的剩余历史留给后续
  tick，避免多年历史造成 OOM 或长时间 writer starvation。
- checkpoint 贯穿 scan、build 和 publish，runtime epoch 失效后不会提交过期 compaction。

### R2-P2-08：migration rollback image 生命周期

- 当前 schema version 为 43。v42 升级会幂等探测并补齐 queue fencing columns，再安装 transfer、
  staging 和 cross-subject provenance trigger；已有 uploading row 被保留，旧 claim 字段初始化为空。
- verified pre-migration backup 仍在任何迁移 DDL 前创建；迁移、optional feature 或 policy 初始化
  任一点失败仍恢复完整 backup。
- rollback image cleanup 移到完整初始化成功之后，并明确放在 migration rollback exception handler
  之外。cleanup 即使在删除部分文件后异常，也不会尝试从已经不存在的 backup 回滚当前 v43 数据库。
- 普通 unlink/permission failure 是 best effort：当前数据库保持 v43 authoritative，未删除 backup
  留给操作者观察并在后续启动重试清理。

## 兼容性与迁移

- schema 42 数据库自动迁移到 43；claim columns 使用列探测补齐，支持开发期修复过的 marker，
  不依赖一次性 `ALTER TABLE` 必然成功。
- 新增 S3 account ID 是稳定 provider identity 的必要输入；配置 S3 却缺失该值时启动 fail closed。
- 自定义 S3 endpoint 必须使用 HTTPS；provider identity canonicalization 拒绝 URL credential、query
  和 fragment。
- 旧单主体共享 archive root 只有在归属可证明时才可兼容；多主体歧义不会自动搬迁或猜测。
- subject-scoped mutator 是内部 API 加固，已有测试和 service 调用已传递当前主体。直接调用这些
  store API 的外部集成需要同步补充 `subject_id`。
- export job 在 `artifact_pruning` 过渡态时不应提供下载；该状态是可恢复 intent，不是 completed
  artifact 的可用证明。

## 新增验收

新增四个 Gate 2 专项文件：

- `tests/test_gate2_external_boundaries.py`：S3 provider identity、真实 readiness cache/failure、阻塞
  SDK absolute deadline、隔离 worker，以及同步 HTTP 超时 response cleanup hook。
- `tests/test_gate2_archive_integrity.py`：v42 -> v43、migration cleanup/rollback、主体 archive root、
  shared legacy usage、staging 内容校验，以及 concurrent finalizer 与 replay deletion race。
- `tests/test_gate2_storage_pressure.py`：snapshot row/byte budget、critical pressure 禁止写放大、每 tick
  compaction budget 和并发 CAS。
- `tests/test_gate2_subject_boundaries.py`：跨主体 world、transport、secret、embedding/search/model
  resource 读取和修订拒绝。

此外，`tests/test_gate2_archive_integrity.py` 包含 staging integrity registry v2、源字节 admission、
orphan/temp GC 和 migration/queue 故障注入；`tests/test_service.py` 与 `tests/test_transports.py`
包含 managed embedding fallback 和 forged delivery side-effect fence 回归。

迁移故障注入特别覆盖两类不同边界：

- cleanup 在删除 current backup 后抛出异常时，不调用 `_restore_migration_backup`，已完成的 v43
  schema 和 staging table 保持权威；
- backup unlink 因权限失败时，初始化仍成功、`PRAGMA quick_check` 为 `ok`、backup 保留，后续正常
  启动自动清理。

## 验证记录

最终 checkpoint 前的验证记录（包含最后一次超时清理回归）：

- Gate 2 当前专项组合（archive/external/storage/subject boundaries）：**70 passed**；
- 关联 service、transport、storage lifecycle 回归组合：**114 passed**；
- 完整 `pytest -q`：**903 passed, 3 skipped, 104 subtests passed**。3 个 skip 均为 Windows
  环境不具备 POSIX symlink 的条件性合同，不换算为通过；
- `tests/test_m42_p1_02_integrity_runtime.py`：**124 passed**，确认 integrity registry v2 与既有
  报告合同兼容；
- `ruff check src tests`、`ruff format --check src tests`、strict `mypy src tests`、`compileall -q
  src tests` 和 `git diff --check` 全部通过。

Windows 条件性 symlink/POSIX skip 继续按既有策略保留，不会被换算为通过。

## 验收合同说明

本阶段不改写 `tests/contracts/remediation_acceptance.json`。该文件明确绑定
`audit_baseline: 87e059a` 和 `docs/audit/2026-08-15-full-readonly-audit.md`，使用旧 P1/P2 编号与
2026-08-18 验证快照；把本次 R2 Gate 2 发现直接追加进去会混合两个审计基线，并使历史
`verified` 证据看起来像覆盖了本次组合缺陷。

本次机器可读验收证据以独立 Gate 2 测试文件和本报告记录。若后续确实需要统一 JSON 矩阵，应
创建绑定 `docs/audit/2026-08-18-full-reaudit.md` 与 Gate 2 checkpoint 的新 contract，或显式升级
format/baseline，而不是静默覆盖旧文件。

## 残余风险与明确边界

1. model/world/search/browser 的 DNS-to-connect pinning 已关闭；boto/botocore 自定义 S3 endpoint
   仍未接入 Noyra 自定义 pinned connector。`ExpectedBucketOwner` 可阻断账户错配，但不能消除
   DNS rebinding；后续若支持不可信 endpoint，应提供可审计 pinned resolver/connector 或受信 allowlist。
2. Python 无法强制终止卡死在第三方 SDK/系统调用里的线程。当前做到每 provider 最多 1 个、
  全局最多 4 个 daemon worker，且迟到调用不能覆盖 durable queue state；极端情况下进程退出前
  仍可能保留这 4 个隔离 worker。同步 HTTP 的 embedding/common-knowledge 路径另有超时关闭
  response 的补偿钩子，但不能替代对任意第三方阻塞调用的强制终止能力。
3. SQLite 与 provider object 无法组成单一原子事务。staging manifest、CAS finalize、replay 和
   orphan/temp GC 把已知中断点变成可恢复状态，但真实对象存储的权限、版本化、eventual
   consistency、ExpectedBucketOwner 支持和 provider-specific failure 仍需在目标生产账户做演练。
4. 多主体 legacy `subject/cold` 歧义被保守隔离，不会自动修复。上线多主体数据库前需要显式
   inventory、按 segment/subject 证明归属并迁移到 storage-key root；无法证明的对象应保持隔离。
5. storage quota 是应用层 admission，不替代操作系统/卷级 reserve。若卷在一次已获准操作中被
   其他进程突然耗尽，仍可能出现 ENOSPC；当前合同保证失败可恢复，并避免压力已知时继续写放大。
6. S3 custom endpoint 的 DNS pinning、真实 provider 权限/一致性和无法强制终止的第三方调用仍是
   明确残余边界；完整性 registry 目前校验 SQLite ledger，不替代 provider 对象内容和生产账户演练。

## 延期到最终发布候选的门禁

以下事项按用户此前决定继续延期，不计为 Gate 2 活动代码缺陷，但最终发布前必须再次提醒：

- P2-14：生产主机 BitLocker/LUKS 恢复演练和 key custody 记录；
- P3-03：多日 affect calibration/soak；
- P3-04：完整 LongMemEval S/M、答案级 QA 评测和趋势曲线；
- P3-05：正式 MSI/MSIX、签名证书、clean Windows VM、升级及崩溃回滚；
- P3-08：真实 GitHub tag release、Sigstore 证书、完整 SBOM 和离线 provenance 验证。

真实 S3 账户的端到端权限、超时、恢复和 provider consistency 演练同样需要生产候选环境证据，
本地 injected-client 测试不能替代该验收。

## Gate 决策

本报告只记录 Gate 2，不自动进入 Gate 3。主任务已完成并行审查收口、最终全量测试、静态门禁、
`git diff --check`，并创建 Gate 2 checkpoint；随后向用户详细汇报 Gate 2 已关闭项、残余风险和
延期门禁，等待用户决定是否进入 Gate 3。
