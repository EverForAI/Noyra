# Noyra 全面只读审计报告

审计日期：2026-08-15  
审计基线：`main` / `87e059a fix: audit historical causal timestamps`  
审计类型：全面、只读、证据驱动审计。除新增本报告外，没有修改生产代码、测试、配置、数据库或部署文件。

## 1. 执行结论

Noyra 已经形成了较完整的研究型人工主体运行时：身份与模型解耦、SQLite 持久状态、事件与修订账本、目标和情绪因果、反思睡眠、模型资源池、搜索和通讯资源、项目层、训练数据导出、冷归档、桌面入口及 Ubuntu/Docker 部署骨架均已存在。当前 291 项测试和全部静态质量门通过，说明代码基线具有较好的工程纪律。

但本次审计不能支持“已经适合多年、无人值守、24 小时生产运行”的结论。审计确认了以下核心阻断点：

- 睡眠请求与遗留 `prepared` action 可以形成稳定死锁，认知和睡眠都不再推进。
- M40 完整性 watchdog 没有接入启动或周期运行；它还会把正常冷归档事件误报成 P0 数据损坏，并漏检多数认知域。
- 完整运行导出会漏掉大量修订和子历史表；训练导出仍有无界内存集合、系统临时盘和并发同意撤回问题。
- 导出线程在服务释放主体进程锁后仍可继续写主体数据库，破坏单一运行时所有权边界。
- 语义记忆召回仍对全部向量执行 Python 侧 O(ND) 扫描，嵌入调用没有独立预算和使用账本。
- 冷归档在未配置密钥时完全停用；云端验证后不能释放本地冷段，单密钥模式也不支持安全轮换。
- 项目执行完整性只校验修订自身哈希，没有证明当前执行、结果哈希、artifact 字节和验收证据一致；若干“成功”仍是模型自述或占位结果。
- `requires_approval` 只检查一个任意非空字符串，并不代表真实、可验证、一次性的批准。

本次确认：

| 等级 | 数量 | 结论 |
|---|---:|---|
| P0 | 0 | 未确认当前默认配置下可直接造成远程代码执行、大规模不可逆主体损坏或未授权高危外部副作用的缺陷 |
| P1 | 13 | 生产部署前必须修复或明确关闭相关能力 |
| P2 | 18 | 会明显削弱可靠性、安全、成本控制或可运维性，应在稳定版前完成 |
| P3 | 8 | 产品、研究验证、生态和工程成熟度缺口 |

“未发现 P0”不等于安全认证。本次没有执行真实公网渗透、真实第三方通讯合同测试、Ubuntu 断电恢复、磁盘打满、多月 soak 或恶意本地进程竞争测试。

对 Git 历史进一步做因果归属后，31 项 P1/P2 中有 28 项属于相关能力首次实现时就存在的原生问题，只有 3 项属于后续修复直接引入的新故障：P1-03、P1-06、P2-12。28 项原生问题中，13 项后来被修过但只修到部分路径，属于“修复不完整/残余问题”；它们不能被误称为修复制造的次生问题。8 项 P3 是成熟度或验证缺口，不纳入原生 bug 与次生 bug 的二分统计。详细提交证据见 7.1 节。

## 2. 分级和修复风险定义

| 风险等级 | 定义 | 处理要求 |
|---|---|---|
| P0 | 可直接导致不可恢复主体损坏、大规模私密数据泄露、远程代码执行或未授权高危外部副作用 | 立即停止发布和运行，先隔离再修复 |
| P1 | 在合理条件下导致长期停滞、主体所有权/完整性/同意/隐私边界破坏，或阻断 24/7 运行 | 生产部署前修复；必须有回归和故障注入证据 |
| P2 | 有明显可靠性、安全、成本、性能或运维影响，但存在可行绕行方案 | 近期修复并纳入稳定版门禁 |
| P3 | 产品完整性、研究可信度、文档、可观察性或生态成熟度不足 | 排期修复，不能宣传为已完成能力 |

修复风险表示“实施修复时引入新回归或数据迁移事故的概率和影响”，不是缺陷本身的严重度：

| 修复风险 | 含义 |
|---|---|
| 低 | 局部接口或投影调整，不改变持久状态语义 |
| 中 | 涉及状态机、并发、兼容接口或有限迁移，需要专项回归 |
| 高 | 涉及主体历史、跨表归属、同意快照、加密归档、长期存储或运行时所有权，必须分阶段迁移和恢复演练 |

## 3. 审计范围、方法和限制

### 3.1 范围

- `src/noyra` 全部 Python 运行时，重点覆盖 core、autonomy、cognition、mind、sleep、model、world、research、capability、interaction、knowledge 和 service。
- SQLite schema 1-32、初始化、迁移、触发器、修订链、事件链、主体边界和进程锁。
- 运行日志导出、训练数据记录与导出、后台任务、取消、关机和临时空间。
- 记忆检索、FTS5、embedding、实体/时间/因果评分、记忆生命周期。
- 自主目标、情绪、睡眠、元认知、自我修改和自主项目执行。
- 搜索、浏览、通讯 transport、SMTP、外部能力和 common knowledge。
- Windows 桌面入口、Ubuntu systemd、Docker/Compose、CI、依赖锁、OpenAPI 和发布资料。

### 3.2 已执行验证

| 检查 | 当前结果 |
|---|---|
| `python -m pytest --cov=noyra --cov-report=term-missing` | `291 passed`，总覆盖率 `82%`，耗时 206.84 秒 |
| `ruff check src tests` | 通过 |
| `ruff format --check src tests` | 通过，164 个文件格式正确 |
| `mypy src tests` | 通过，164 个源文件无类型错误 |
| `compileall -q src tests` | 通过 |
| `pip check` | 通过，无 broken requirements |
| `pip-audit -r requirements.lock` | 未发现当前已知漏洞 |
| `scripts/audit-deployment.ps1` | 32 项通过；专项覆盖率 `70.11%`，门槛 `69.9%` |
| `git diff --check` | 通过 |
| Docker build | 未执行；本机只有 Docker client，daemon 未运行 |

另执行了三个仅使用临时目录的最小复现，没有改动仓库：

1. 创建一个 goal 后导出 runtime ZIP，`goal_revisions.jsonl` 实际为 0 行。
2. 正常归档一个旧事件后执行 `LongRunResilience.audit()`，报告错误产生 `event_hash:*` P0。
3. 同时存在疲劳睡眠条件和一个 `prepared` action 时连续 tick，均返回 `sleep_requested`，生命周期保持 `active`，active hook 从未运行。

### 3.3 限制

- 没有真实 Ubuntu 主机、systemd、Docker daemon、S3、Telegram、飞书、QQ、微信或 SMTP 账户，因此只验证了代码合同和模拟适配器。
- 没有执行断电、文件系统损坏、磁盘满、网络分区、DNS rebinding、本地 symlink 竞争和多月 soak。
- 没有使用真实私人主体数据；隐私结论来自数据流和访问控制分析。
- 本报告中的“全部问题”指本次范围和证据下确认的问题，不表示形式化证明仓库不存在其他缺陷。

## 4. 风险总表

### 4.1 P1

| 编号 | 置信度 | 位置 | 问题摘要 | 主要影响 | 修复风险 |
|---|---|---|---|---|---|
| P1-01 | 高 | `autonomy/loop.py:65-85`；`sleep/engine.py:646-655`；`core/actions.py:237-275` | 疲劳触发睡眠时，遗留 `prepared` action 造成稳定死锁 | 永久不睡眠、不认知，且日志持续伪称 `sleep_requested` | 中 |
| P1-02 | 高 | `core/resilience.py:26-172`；`service.py:1791-1858`；`core/runtime.py:31-44` | 完整性 watchdog 未接入启动/周期运行，registry 漏掉大多数认知域 | 域损坏不会触发安全暂停，M40 release gate 实际不存在 | 高 |
| P1-03 | 高 | `core/resilience.py:88-97`；`core/event_archive.py:62-90` | 正常冷归档后 watchdog 把 `{}` tombstone 与原 payload hash 比较并误报 P0 | 一旦归档，完整性门必然错误失败，掩盖真实损坏 | 中 |
| P1-04 | 高 | `core/runtime_export.py:84-125,214-260` | runtime export 的通用归属算法被同名 `_id` 覆盖，漏导大量历史表 | “完整详细运行日志”并不完整，事故分析和训练回放缺证据 | 高 |
| P1-05 | 高 | `core/storage.py:139-205`；`core/training_export.py:261-307` | 训练策略更新存在 lost update，导出期间撤回同意不能阻止发布 | 同意历史错误；撤回后在途私密导出仍可能完成 | 高 |
| P1-06 | 高 | `core/export_jobs.py:67-79,182-217`；`service.py:1849-1858` | 关机/取消不等待 running export，主体锁释放后 worker 仍可写库 | 两个进程同时写同一主体；取消任务仍写 export/audit 记录 | 高 |
| P1-07 | 高 | `core/training_export.py:247-315,337-445`；`docker-compose.yml` | 训练导出仍保留无界 `seen`/episode 列表，并使用系统临时目录 | 多年数据 OOM；Windows C: 或 Compose 64 MB `/tmp` 被打满 | 中高 |
| P1-08 | 高 | `interaction/projection.py:49-165,193-293`；`service.py:521-525` | 未认证 `/api/state` 暴露目标、使命、项目 deliverable 和元认知理由 | 私人规划和心理倾向被公开投影泄露 | 低中 |
| P1-09 | 高 | `mind/retrieval.py:127-146`；`mind/memory.py:259-392` | semantic recall 每次加载并计算全部 embedding，之后才截断候选 | O(ND) CPU/RAM/SQLite 开销，长年记忆会拖垮 tick | 高 |
| P1-10 | 高 | `model/embedding.py:47-91`；`mind/retrieval.py:96-146` | 远程 embedding 无独立调用/token/费用预算和 ledger，recall 同步阻塞 | 费用不可控；供应商异常可长期阻塞认知；无法准确计入疲劳 | 高 |
| P1-11 | 高 | `core/storage_lifecycle.py:54-100`；`core/archive.py:398-544`；`core/event_archive.py:93-142` | 冷存储不能保证多年空间受控或密钥可恢复 | 默认不归档；云验证不释放本地段；换 key 后旧段不可读 | 高 |
| P1-12 | 高 | `cognition/execution.py:136-250,532-719`；`core/resilience.py:146-172` | 项目执行完整性和成功验收不足，且不在 release gate 中 | artifact/结果可与账本分离，项目可被错误标记成功 | 高 |
| P1-13 | 高 | `capability/store.py:156-190` | `requires_approval` 只要求任意非空 `approval_id` | UI 的“每次批准”不是安全边界，无法审计、过期或防重放 | 高 |

### 4.2 P2

| 编号 | 置信度 | 位置 | 问题摘要 | 主要影响 | 修复风险 |
|---|---|---|---|---|---|
| P2-01 | 高 | `core/training_export.py:621-655` | workspace export 从整个 workspace root 递归读取，而非当前 subject root | 重用 data dir 时可能把另一主体项目文件打入训练包 | 中 |
| P2-02 | 高 | `core/actions.py:278-313`；`core/database.py:101-118` | unknown reconciliation 原地改写 behavior log 时间、状态和解释，且日志无自身哈希 | 用户可见历史不是 append-only，无法证明最初 unknown 记录 | 中 |
| P2-03 | 高 | `research/search.py:287-305`；`research/browser.py:169-189`；`interaction/transport.py:664-675` | 部分 HTTP 适配器先缓冲完整响应再检查大小，transport 无响应上限 | 恶意或异常上游可造成内存峰值和线程占用 | 中 |
| P2-04 | 高 | `interaction/transport.py:154-168,683-714` | SMTP 只拒绝字面私网 IP，不固定公共 DNS；超时按 failed 重试 | DNS rebinding/内网连接风险；投递成功后超时可能重复发信 | 中高 |
| P2-05 | 高 | `interaction/transport.py:619-628,716-745` | unknown delivery 没有查询、人工 reconcile 或 provider-status 恢复路径 | 消息永久停在 unknown，用户无法安全决定是否重发 | 中 |
| P2-06 | 高 | `model/embedding_resources.py:134-167`；`service.py:1746-1761` | disable/revoke 不会停止已构造的 embedding provider，撤销删除也无 repair queue | 控制面状态与实际调用不一致；密钥删除失败不可修复 | 中高 |
| P2-07 | 高 | `model/embedding.py:20-26,66-90`；`model/config.py:33-45` | 模型/embedding 自定义 endpoint 没有公共 DNS pinning；embedding 无流式响应上限 | 可连接私网目标；异常响应导致内存放大 | 高 |
| P2-08 | 高 | `knowledge/common.py:174-240` | 同一 `package_id` 不同内容时 `INSERT OR IGNORE` 后仍创建 import 关联 | 审核记录可绑定到与本次验签 envelope 不同的已存内容 | 中 |
| P2-09 | 高 | `knowledge/common.py:242-309,321-345`；全仓调用关系 | accept/usable 未接入认知或 HTTP；读取时也不重新验证签名/信任状态 | “共同知识可用”目前是孤立存储能力，元数据篡改不易发现 | 中高 |
| P2-10 | 高 | `core/database.py:3356-3364,3726-3762` | 检查“数据库版本比运行时新”之前先执行当前 schema；升级前无自动备份 | 旧运行时会先改新库再拒绝；迁移事故只能依赖手工冷备 | 高 |
| P2-11 | 高 | `cognition/self_modification.py:439-462,519-528` | thought 阈值效果仍错误映射到 `goal_review`；“simulation”只检查值不同 | 自我修改采集错误 outcome，且没有真实下游行为模拟 | 中 |
| P2-12 | 高 | `cognition/projects.py:1569-1585`；`cognition/types.py:405` | 项目截止时间从创建时连续计时，包含排队、暂停和睡眠；允许极小正数 | 未实际执行的项目也会被自动 abandoned | 中 |
| P2-13 | 中高 | `cognition/execution.py:532-627`；`ProjectWorkspace.write()` | 多文件 prototype 非事务提交；目录 symlink 检查到写入仍有 TOCTOU | 失败后残留半成品；本地竞争进程可能突破路径边界 | 高 |
| P2-14 | 高 | `core/database.py`；`model/resources.py`；`interaction/transport.py` | 热 SQLite 和多数 secret 文件未应用级加密 | 云盘快照、主机备份或磁盘泄露会暴露私密心理和凭据 | 高 |
| P2-15 | 高 | `pyproject.toml:29-32`；`requirements.lock`；`Dockerfile`；`scripts/install-ubuntu.sh` | 默认 Docker/Ubuntu 安装不包含 `boto3` cloud extra | 配置 S3 后只会记录 cloud disabled，M39 默认不可用 | 低中 |
| P2-16 | 高 | `core/runtime_export.py:75-151`；`core/training_export.py:540-599` | 大导出长期持有 SQLite read snapshot，阻止 WAL checkpoint；legacy 同步路由仍存在 | 导出期间 WAL 可持续增长，HTTP worker 长时间占用 | 中高 |
| P2-17 | 高 | `interaction/projection.py:297-372`；`service.py:1642-1647` | runtime logs 使用大 UNION + 全局 ORDER BY + offset，最大 offset 100,000 | 大库查询 CPU/临时空间随历史增长，易拖慢管理面 | 中 |
| P2-18 | 中高 | `service.py:110-115`；`cognition/execution.py:317-331`；`core/archive.py:250-267` | `subject_id` 允许路径分隔符，多个文件布局直接拼接该值 | 错误配置可让 workspace/archive queue 离开预期主体目录 | 中 |

### 4.3 P3

| 编号 | 置信度 | 位置/范围 | 问题摘要 | 修复风险 |
|---|---|---|---|---|
| P3-01 | 高 | `cognition/execution.py:532-719` | 软件原型不 build/test，预测固定 0.5，自我实验只记录 baseline | 中高 |
| P3-02 | 高 | `knowledge/common.py`；service/UI | 共同知识没有自动发现、同步、撤销传播、subject evaluation workflow | 高 |
| P3-03 | 中高 | affect/goal/project policy | 情绪到行为主要是手工线性权重和阈值，尚无长期稳定性或因果消融验证 | 高 |
| P3-04 | 高 | memory/retrieval | 没有 LoCoMo、LongMemEval 等可复现实验，也没有长期 precision/recall 曲线 | 中 |
| P3-05 | 高 | `desktop.py`；部署文档 | 桌面版仍是 Python CLI + 浏览器，不是可签名、可自动升级的 Windows 安装包 | 中 |
| P3-06 | 高 | service/UI | 缺少完整 pause/resume/reset、prepared/unknown reconcile、integrity report 和 archive-key health 操作面 | 中高 |
| P3-07 | 高 | `docs/api/openapi.yaml`；`service.py` | OpenAPI 只列一部分接口，未由运行时代码生成或契约测试锁定 | 低中 |
| P3-08 | 高 | CI/release | lockfile 无 hashes，GitHub Actions 未固定 commit SHA，无签名 release/provenance | 中 |

## 5. P1 详细证据和修复边界

### P1-01：睡眠请求可被 `prepared` action 永久阻塞

`AutonomyLoop._tick_once()` 在 `should_sleep` 为真时先调用 `SleepEngine.start()`，但把 `SleepStateConflictError` 完全 suppress，随后无条件返回 `sleep_requested`。`SleepEngine.start()` 又要求不存在 `prepared` 或 `executing` action。启动恢复只把 `executing` 变成 `unknown`，保留 `prepared` 以便原 workflow 恢复。

最小复现连续执行两个 tick：两次都得到 `sleep_requested`，生命周期仍为 `active`，active hook 调用次数为 0，`prepared` action 保持 1。由于 active hook 不再执行，原 workflow 没机会恢复或取消 action，系统形成自维持死锁。

修复应把“进入睡眠”和“清空可恢复工作”设计成明确状态机：只有 sleep run 成功创建才返回 `sleep_requested`；若存在 prepared work，应先恢复/取消/隔离，并设置有界 deadline。不能简单把 prepared action 自动标记失败，因为它可能代表尚未开始、可以安全恢复的合法工作。修复风险：中。

### P1-02：M40 watchdog 是未接线且不完整的库代码

`LongRunResilience` 仅被测试直接构造；生产 service、kernel boot 和 autonomy loop 均没有调用。启动只执行 `SubjectKernel.verify_continuity()`，它主要检查 identity 与最新 checkpoint 的对应关系。

当前 `_domain_checks()` 覆盖 action、mind、memory block、entity、sleep、interaction、transport、capability、world 和 outcome，但仓库中已经存在而未纳入的完整性检查包括 consciousness、goal governance、action deliberation、research、metacognition、motivation、self model、thought、self modification、autonomous projects、project executions、model resources 和 memory embeddings。

因此历史报告中“M40 complete”和“任何域失败安全暂停”不符合当前运行路径。修复需要统一 registry 版本、同一只读快照、启动轻检查、周期分片深检查、明确 P0/P1 -> safe pause 的转换，并避免 watchdog 本身在大库上全量占用内存。修复风险：高。

### P1-03：正常事件冷归档会制造虚假 P0

归档后 `events.payload_json` 被合法替换为 `{}`，原 `payload_hash` 保留，真实 payload 位于加密段。`LongRunResilience.audit()` 却直接计算 `content_hash(json.loads(payload_json))`，没有调用 archive-aware 的 `EventStore.payload_from_row()`。

最小复现归档 1 个旧事件后，报告为：`p0=[event_hash:<id>]`、`event_hashes=failed`。这不是边界条件，而是任何启用 90 天冷归档的长期实例都会遇到的确定性错误。

修复必须基于 archive manifest、segment hash、key id、event payload hash 三层验证，且在 key 暂不可用时区分 `degraded/key_unavailable` 与 `corrupt`。修复风险：中。

### P1-04：完整运行导出会系统性漏历史

`_ownership_map()` 对每个含 `subject_id` 的表取“第一个以 `_id` 结尾的列”，再以列名作为全局字典 key。多个表共享 `revision_id`、`decision_id`、`transition_id`、`review_id` 等列名时，后表覆盖前表的 ID 集合。对子表，`_cursor()` 又使用第一个碰到的同名列过滤。

确认的典型错误包括：

- `goal_revisions` 用 `revision_id` 对比 `self_modification_revisions` 的 ID，实际导出 0 行。
- `interaction_decisions` 用 `decision_id` 对比 `metacognitive_decisions`。
- project、memory、belief、source、sleep、relationship、strategy、mission 等大量 revision/transition 表通常为空。
- common knowledge 等没有直接 `subject_id` 的表可能完全遗漏。

此外它先把所有主体 ID 装入 Python set，再构造单个 `IN (?,...)`，既不是流式归属，也会在大历史上超过 SQLite variable limit。

修复不能继续猜列名，应维护显式、版本化的 export ownership graph，按父表 JOIN/EXISTS 过滤，逐表 cursor 分页，并用 manifest 声明 skipped/unsupported 表。迁移前需对每个 schema 32 表建立行数对账测试。修复风险：高。

### P1-05：训练同意的并发和撤回语义不成立

`TrainingStore.update_policy()` 在写事务之外读取 current policy。两个并发请求可都基于版本 N 构造完整字段值，后写者覆盖前写者对其他字段的修改；数据库版本最终可能为 N+2，但两条 audit 都声称 N -> N+1。

训练导出只在开始时读取一次 policy，并在长 read snapshot 中输出数据。撤回 `export_enabled`、private psychology、conversation 或 workspace 同意时，已经在途的导出不会重新检查 policy version，仍可发布 artifact。

修复应在一个 `BEGIN IMMEDIATE` 内读取、compare-and-swap、写 policy event，并给更新 API 增加 expected version。导出必须持有不可变 consent lease 或在发布前再次核对 policy/version；撤回应能取消未发布 artifact。对已经写入磁盘但未发布的内容要有销毁和审计语义。修复风险：高。

### P1-06：导出 worker 可越过主体所有权边界

`ExportJobManager.close()` 将 DB 中 queued/running 标为 cancelled 后调用 `shutdown(wait=False)`。Python 无法取消已经运行的线程；service 随后关闭 gateway、delivery 并释放 process lock。旧 worker 仍可能读取主体状态，并由 exporter 写 `audit_records` 或 `training_exports`。新进程此时可以取得锁并启动，形成旧 worker 与新主体运行时并发写库。

worker 完成 exporter 内部记录后，`_run()` 才看到 job 已 cancelled 并删除 ZIP，因此取消记录与 export audit 也会互相矛盾。1 GB 上限是在 ZIP 完成后才检查，无法防止临时磁盘先耗尽。

修复应使用可协作取消 token、分块检查、发布两阶段提交和关机 deadline；在释放主体锁前必须等待 worker 退出或把 export 隔离到不具备主体写权限的独立进程。artifact 大小和临时空间限制必须在写入过程中执行。修复风险：高。

### P1-07：训练导出仍不适合多年数据

虽然 JSONL writer 是增量写入，但 `seen` 每个 unique event 保存一个 hash；episode 只有在事件间隔大于 30 分钟时 flush。一个持续每 30 分钟内产生事件的实例可能把多年 event IDs/types 保留在单个 Python list，最终还会把这个超大 episode 一次序列化。

内部 `TemporaryDirectory()` 未指定 Noyra data volume。Windows 会优先使用系统临时目录，违背保护 C: 空间的要求；Compose 把 `/tmp` 限制为 64 MB，正常导出就可能失败。临时文件还会同时保存 events、episodes、trajectories 和派生视图，峰值远大于最终 ZIP。

修复应按事件数量和时间双阈值切 episode，使用磁盘/SQLite 去重而非无界 set，把临时目录放入受 quota 管理的 data volume，实时检查 temp/output/WAL 三类预算，并支持分片 manifest。修复风险：中高。

### P1-08：公共状态越过私人心理边界

`/api/goals` 和 `/api/projects` 已要求 read token，但无认证 `/api/state` 仍返回 active goal title、mission title/commitment/confidence、current project title/deliverable、最新 metacognitive strategy/reason、consciousness workflow/reason 等字段。它们足以重建主体正在想什么、做什么和为何选择，和“用户只能看公开日记，私人心理不公开”的边界冲突。

修复应建立正式 `PublicSubjectStateV1` schema，只保留身份名、生命周期粗粒度、是否在线、经主体明确选择的公开摘要和公开日记计数。详细 state 应迁移到 read-auth endpoint；不能依赖前端隐藏。修复风险：低中。

### P1-09/P1-10：语义记忆扩展性和预算边界不足

每次 semantic recall 先远程 embed query，再从 `memory_embeddings` 读取当前 provider 的全部向量，逐个 JSON parse、hash 和 cosine；候选数 768 的限制发生在全量评分之后。因此首版“hybrid retrieval”提高了相关性，但没有向量索引的扩展性。

远程 embedding 不进入 model ledger、没有独立 daily request/token/cost budget、没有 provider health/circuit，也不记录每次查询的用量。`MemoryStore.recall()` 是同步路径，远程 HTTP 可直接阻塞 async cognition tick。

修复建议：embedding 独立 resource pool + ledger + budget + circuit；query embedding 可短期 cache；候选先由 FTS/entity/time/recency 取有限集合，再对候选计算向量，或接入受控 ANN/向量扩展。必须用 10k/100k/1m memories benchmark 验证 latency、RAM、召回率和成本。修复风险：高。

### P1-11：长期存储仍会停止主体

冷事件和 observation 只有设置 `NOYRA_ARCHIVE_ENCRYPTION_KEY` 才启用；默认桌面部署没有归档。归档把大字段 tombstone，但 SQLite 文件不会因此立即缩小，local encrypted segments 还会增加新占用。云上传验证只删除 staging queue 文件，不删除 `subject/cold` 原件，也没有 cloud read-through/restore cache，所以云存储不能释放主体盘。

每个 local archive provider 只接受一个当前 key，读取时要求 segment key id/fingerprint 与当前 key 相同；没有 keyring、envelope encryption 或 rewrap 流程。换 key 会令旧事件和 observation 不可读。其余高增长表，例如 interactions、behavior logs、consciousness frames、model metadata、access histories 和 revision tables，也没有分层保留计划。

修复需要先定义“不可删除主体元数据”和“可迁移大 payload”清单，再实现多 key keyring、启动 key health、云端双校验、read-through restore、本地 tombstone/GC、freelist/WAL 观察和恢复演练。不能直接 VACUUM 或删除历史表来追求空间数字。修复风险：高。

### P1-12：项目成功和完整性证据不足

execution revision hash 只覆盖 execution id、status、result hash、reason 和时间。`verify_integrity()` 没有：

- 比较最新 revision 与 mutable execution 当前行；
- 重算 `result_hash`；
- 校验 acceptance evidence、research/action/model foreign evidence；
- 读取 artifact 字节并比对 hash/path/workspace ownership；
- 验证 phase/project 状态与 execution 结果的因果一致性。

该检查也未纳入 resilience registry。功能层面，software prototype 的 validation 是模型返回文本，不 build/test；prediction 固定 0.5；self experiment 只记录基线和假设即成功；collaboration request 只证明 intent 被记录。

修复应先定义每种 output type 的可执行验收器和 evidence schema，再把 artifact content digest、validator version、stdout/status、source IDs 和 acceptance verdict 写入 append-only ledger。未经验证只能是 `produced` 或 `awaiting_validation`，不能是 succeeded。修复风险：高。

### P1-13：批准机制是占位符

当 grant 的 `requires_approval` 为真时，authorization 只检查 `approval_id and approval_id.strip()`。没有 approval 表、issuer、签名、grant/action/resource 绑定、过期时间、nonce 或一次性消费。任何内部调用方传入 `"x"` 即通过；同时 UI 也没有真正创建批准记录的流程。

修复必须先明确产品语义：若 Noyra 不应由人类逐次批准，就删除此开关，避免虚假安全承诺；若保留，用 append-only approval grant 绑定 exact action hash/resource/cost，短时有效并原子消费。修复风险：高。

## 6. P2/P3 说明和修复要点

### 6.1 数据、网络和迁移

- P2-01：训练 workspace 必须从 `workspace/<subject_id>` 起步，并在 manifest 标记 subject/path policy。当前 opt-in 不等于允许跨主体读取。
- P2-02：behavior log 应记录 `unknown` 原事件，再追加 reconciliation 事件；公开投影可以显示 derived latest status，但不能覆盖原时间和解释。
- P2-03 至 P2-07：所有外部资源应共用“URL canonicalization、公共 DNS pinning、流式 byte limit、总 timeout、usage ledger、circuit、secret cleanup”基础合同。SMTP 需要投递状态查询或明确 at-least-once 风险。
- P2-08/P2-09：import 前若 package id 已存在，必须逐字段恒等比较；不相同应 quarantine 为 collision。每次 usable 前验证签名、key 状态、package 状态和 import acceptance。subject acceptance 需要真实 cognition workflow，而非由 HTTP operator 代替。
- P2-10：构造 `Database` 的第一步应只读检查 marker。迁移前使用 SQLite backup API 生成同卷原子备份，逐版本 migration 有 checksum 和 recovery marker；测试真实 v6-v31 fixture、每步崩溃和 rollback。

### 6.2 自我修改、项目和文件边界

- P2-11：`max_thought_no_change_streak` 的观察必须映射回 `think`。simulation 至少要回放一段固定历史，比较 workflow 选择、预算、停滞和睡眠触发差异；仅验证取值范围不应称作模拟。
- P2-12：deadline 应基于累计 active execution time，而不是 wall-clock since creation；排队、paused、blocked、sleep 应暂停计时，并设最小可执行时长。
- P2-13：prototype 应写入 staging tree，全部验证成功后原子发布 manifest。Linux 使用 dirfd/openat/O_NOFOLLOW，Windows 使用等价 reparse-point 检查或隔离 worker。
- P2-18：subject id 应限制为稳定 ASCII slug，文件系统路径使用独立不可控 storage key。身份显示名不应直接成为目录名。

### 6.3 部署、查询和验证

- P2-14：应用级数据库加密与跨平台部署复杂，短期至少强制专用系统用户、0600/0700、加密卷、加密备份和 secret manager；威胁模型必须明确宿主 root 不在防护范围内。
- P2-15：发布两个经过锁定和 CI 验证的安装 profile：base 与 cloud。Ubuntu installer/Docker 若配置 S3 但没有 cloud extra，应启动失败而不是悄悄降级。
- P2-16：大导出使用短 read transaction + keyset cursor 或 SQLite backup snapshot，避免阻止 WAL checkpoint；legacy 同步 endpoint 应移除或只返回创建 job 的兼容响应。
- P2-17：运行日志改为 `(occurred_at, category, record_id)` cursor pagination，分别走索引后做有限 merge，不使用大 offset 和全历史全局排序。

### 6.4 研究和产品成熟度

P3 项目不一定都是传统 bug，但它们决定 Noyra 是否能证明“比任务型智能体更接近长期人工主体”：

- 自主项目必须以真实 artifact 和可复验结果为中心，不能用模型的第一人称 validation 代替验收。
- 共同知识需要独立于主体人格的 advisory cache、版本兼容、撤销传播和来源声誉，但不得自动写入私人记忆、目标或使命。
- 情绪模型需要因果消融：相同经历在关闭/开启某情绪通道时，目标选择、持续性、风险和社交行为应出现可解释差异，同时不造成失控。
- 记忆需要公开 benchmark 和长期运行曲线；当前架构的 provenance 优势不能替代检索质量数据。
- Windows 第一版至少需要固定数据目录、安装/卸载、开机启动、日志、升级备份、代码签名和崩溃恢复，而不只是打开浏览器。
- 运维面必须让 operator 看见 prepared/unknown、integrity、archive key、migration、WAL、storage trend 和 export progress，同时不能暴露私人心理正文。

## 7. 对此前“已修复/已完成”声明的复核

| 既有声明 | 当前复核 |
|---|---|
| M40 watchdog / integrity registry complete | 不成立。库类存在，但生产未调用；archive-aware 验证错误；大量域缺席 |
| runtime export 完整且流式 | 部分成立。ZIP 行写入是流式，但 ownership 错误导致历史漏导，ID 集合不是流式 |
| training export 流式并支持取消 | 部分成立。文件写入增量，但去重/episode 无界；running worker 不可真正取消 |
| P1-09 项目执行闭环已完成 | 仅完成安全边界版 adapter；真实验收、artifact integrity 和恢复仍不足 |
| P1-14 高级 hybrid memory 已完成 | 相关性特征已接入；向量扫描、embedding budget 和 benchmark 未完成 |
| M39 cloud archive complete | 上传队列和 S3 adapter 已实现；默认安装缺 cloud 依赖，且云端不能释放/回读本地 cold segments |
| P2-09 self-modification mapping 已修复 | formation mapping 已修复，但 observation mapping 仍错误，修复不完整 |
| 公开/私人投影边界已修复 | goals/projects 详细端点已鉴权，但 public state 仍暴露等价规划字段 |

仍然确认有效的改进包括：模型 IO capture 使用 live policy getter；事件链已使用追加顺序；HTTP thread/request limits；分角色 token；默认 loopback；模型经济/深度池隔离；search/browser/embedding 资源分类；secret cleanup 在 model/search/transport 中使用；事件和 action revision hardening；world fetch 的公共地址和流式大小限制；通讯 adapter 及 delivery ledger；FTS/entity/temporal/causal retrieval 首版。

### 7.1 问题来源与次生问题分析

这里的“原生”不是指问题必须存在于仓库第一个提交，而是指它从对应能力首次实现时就存在；“严格次生”是指后续修复直接产生了此前不存在的故障；“修复不完整”表示原问题被部分缓解但根因或相邻路径仍在，不算新的次生 bug。归因依据是当前代码的 `git blame`、相关提交前后版本对比和既有 remediation 文档，不按提交标题中的 `feat` 或 `fix` 字样机械判断。

| 来源类别 | 数量 | 编号 | 结论 |
|---|---:|---|---|
| 原生且未被相关修复有效覆盖 | 15 | P1-01、P1-04、P1-13；P2-01、P2-02、P2-03、P2-05、P2-07、P2-08、P2-09、P2-10、P2-14、P2-15、P2-17、P2-18 | 缺陷从相应能力首版即存在；后续提交没有针对该根因完成修复 |
| 原生但修复不完整 | 13 | P1-02、P1-05、P1-07、P1-08、P1-09、P1-10、P1-11、P1-12；P2-04、P2-06、P2-11、P2-13、P2-16 | 根因不是修复制造的，但此前“已修复/已完成”结论覆盖过宽 |
| 严格次生问题 | 3 | P1-03、P1-06、P2-12 | 后续修复改变了原有行为，并直接引入新的确定性故障 |
| 能力或研究成熟度缺口 | 8 | P3-01 至 P3-08 | 不是传统回归，不归咎于某次修复 |

因此，如果只回答“原生还是修复引发”这个二分问题：P1/P2 共 31 项中，28 项的根因是原生，3 项是修复引发。不能把 13 项“修复不完整”统计成 13 个新回归，否则会夸大修复造成的问题数量。

#### 7.1.1 三项严格次生问题

| 编号 | 直接因果链 | 为什么是严格次生 |
|---|---|---|
| P1-03 | `7312659` 创建 `LongRunResilience`，当时事件 payload 仍在热表中；`5f72ced` 为解决长期存储问题，把冷 payload 替换为 `{}` tombstone，但没有同步修改 watchdog | 归档前 hash 检查可以成立；归档后正常数据必然被误报为 P0。新故障由归档修复与旧 verifier 不兼容直接产生 |
| P1-06 | `fb073b6` 引入后台导出，最初 `close()` 使用 `shutdown(wait=True)`，虽然关机可能被拖住，但释放主体锁前 worker 已退出；`df267bb` 为修复取消/关机阻塞改成 `shutdown(wait=False)` | 该改动让 running worker 在 service 释放主体锁后继续写主体数据库。旧问题是“关机等待”，新问题变成“主体所有权被越过”，属于明确修复回归 |
| P2-12 | `d4537c5` 为修复项目可无限持续的问题，新增 `created_at + estimated_duration_hours` 硬截止 | 修复前没有自动误弃项目；修复后排队、暂停、睡眠时间都被计入执行周期，且极小正数可触发未执行项目被 abandoned，属于新状态机错误 |

#### 7.1.2 原生问题被部分修复后留下的残余

| 编号 | 原生根因/首次相关实现 | 后续修复及未覆盖边界 | 归属结论 |
|---|---|---|---|
| P1-02 | `7312659` 首次加入 watchdog 时就未接入生产启动/周期路径，检查域也不完整 | `b8e52c8`、`cac23be` 增加部分 domain checks，但仍未接线且 registry 继续漏域 | 原生，完整性加固不完整；不是加固制造的新问题 |
| P1-05 | `8c63279` 的 policy 更新从事务外读取当前值；`6f6239c` 的导出只在开始读取一次同意 | `cac23be` 增加 consent-aware 导出、审计和分页，但未加入 CAS 或发布前撤回复核 | 原生，并发与在途撤回语义只修了一部分 |
| P1-07 | 首版训练导出就存在随历史增长的内存/临时空间峰值 | `ba96883`、`cac23be` 改为文件流式写入，但保留无界 `seen`、无事件数上限的 episode，并继续使用系统临时目录 | 原生 O(N) 风险被显著缓解但未关闭；Compose `/tmp` 冲突是未做部署联调的残余 |
| P1-08 | goals、mission、consciousness、projects 加入公共 state 时即输出私人规划字段 | `a58b358` 只保护 `/api/goals`、`/api/projects`、`/api/outcomes`，没有收紧返回等价字段的 `/api/state` | 原生隐私边界问题，端点级修复不完整 |
| P1-09 | `6f6239c`/`699e4ac` 的 semantic provider 从首版起就全量读取和计算向量 | `abf8b6c` 只约束主 recall 的候选及因果扫描，remediation 文档也明确保留 semantic 全量扫描 | 原生扩展性问题，候选集修复未覆盖向量索引 |
| P1-10 | `699e4ac` 引入远程 embedding 时没有 usage ledger、预算和 circuit | `6533609` 建立独立资源池和配置面，但没有补调用/token/费用账本，也仍在同步 recall 路径 | 原生资源治理缺口，资源池隔离不等于预算闭环 |
| P1-11 | `947a064` 起的归档/配额能力没有形成可释放本地空间的完整冷热生命周期 | `b67be86`、`f27c96d`、`fd1e898` 等增加加密段、观察归档和压缩，但单 key、无云 read-through、本地原件不 GC、热表覆盖不足仍在 | 原生长期存储问题被分段缓解；密钥和云恢复边界未完成 |
| P1-12 | `66e2181`/`b731e3b` 的项目执行首版把模型文本或占位结果当作足够验收证据 | `c023fa5` 加强 unknown outcome 和 workspace 写入，`cac23be` 加部分 revision integrity，但未建立 artifact/validator/evidence 的端到端证明 | 原生执行验收问题，安全加固和功能闭环都未完成 |
| P2-04 | `72e9cf9` 的 SMTP 首版只验证字面 IP，超时语义也未区分 unknown | `c3311fe` 的公共 DNS pinning 只用于 HTTP transport，没有覆盖 SMTP 连接与投递幂等 | 原生通讯边界，跨协议修复不完整 |
| P2-06 | `6533609` 的 embedding provider 在构造后没有随 disable/revoke 失效 | `240d2f4` 的 secret cleanup queue 覆盖 model/search/transport，但未覆盖 embedding 的 provider 生命周期和删除修复 | 原生 embedding 控制面问题，统一 secret 修复漏接一个资源域 |
| P2-11 | `7312659` 的 self-modification 首版同时存在 formation 与 outcome 反向映射错误，simulation 也只是值比较 | `eb8df7d` 只把候选形成路径改为 `STRATEGY_TO_SETTING`，`_outcomes_since()` 仍把 thought threshold 映射为 `goal_review`，simulation 未增强 | 原生，且是可以直接由修复 diff 证明的半修复 |
| P2-13 | 多文件 prototype 和 workspace 首版就没有事务提交，也存在检查到写入间竞争窗口 | `c023fa5`/`07b4ee8` 加路径和 symlink hardening，但没有 staging + atomic publish，也无法消除本地竞争进程 TOCTOU | 原生文件事务问题，路径加固不完整 |
| P2-16 | `e187146` 的 runtime export 和早期 training export 已经在大读取期间持有 read transaction | `a3e830e`、`ba96883`、`cac23be` 解决全量内存聚合时继续用长 read snapshot，并保留 legacy 同步 API | 原生 WAL/同步路由问题，流式修复没有解决数据库快照寿命 |

#### 7.1.3 其余原生 P1/P2 问题

| 编号 | 首次相关实现 | Git 历史结论 |
|---|---|---|
| P1-01 | `71db65e` 的睡眠静默条件 + `b10ca4a` 的 autonomy sleep 分支 | `SleepStateConflictError` 被 suppress 后仍无条件返回 `sleep_requested` 的组合从首版即存在；后续 workflow recovery 没有制造它 |
| P1-04 | `e187146` | `_ownership_map()` 从 runtime export 首版起就以同名 `_id` 作全局 key；`a3e830e` 的流式重构原样保留该算法 |
| P1-13 | `df5c7bc` | 任意非空 `approval_id` 即通过的逻辑从 approval 检查首次实现起就存在，没有真实 approval ledger 修复 |
| P2-01 | `7312659` | workspace training export 首版即从整个 configured root 遍历，而不是 subject 子目录 |
| P2-02 | `aac47e9` | `reconcile_unknown()` 从主体 kernel 首版起就原地更新 behavior log；后加 action revision 没有改变 behavior log 的非 append-only 语义 |
| P2-03 | `8f3413a`、`3b8f7cc`、`72e9cf9` | search/browser/transport 首版 HTTP 路径就存在先缓冲后限长或没有限长；不是近期网络修复造成 |
| P2-05 | `72e9cf9` | delivery `unknown` 首版只有状态记录，没有查询或人工 reconcile 工作流 |
| P2-07 | `b543edf`、`699e4ac` | model/embedding 自定义 endpoint 首版只校验 HTTPS；`c3311fe` 修的是 transport，不是这两个客户端 |
| P2-08 | `f9151de` | package collision 处理从 common knowledge 首版起就是 `INSERT OR IGNORE` 后继续关联 |
| P2-09 | `f9151de` | common knowledge 首版即停在签名、隔离和存储层，没有接入 cognition；后续 UI review 只增加可见性 |
| P2-10 | `aac47e9`/`b543edf` | 初始化先执行当前 schema、之后才比较版本的顺序，以及无自动迁移备份，来自最早数据库初始化/迁移设计 |
| P2-14 | 各热存储和 secret 能力首版 | 热 SQLite 与多数 secret 文件从创建时就没有应用级 at-rest encryption；冷归档加密没有造成这一缺口 |
| P2-15 | `947a064` | S3 adapter 首版作为 optional extra，但默认 Docker/Ubuntu 安装路径没有选择 cloud extra；锁定依赖修复没有改变该 profile |
| P2-17 | `e187146` | runtime logs 首版就是不断扩大的 `UNION ALL + ORDER BY + OFFSET` 查询 |
| P2-18 | `cd6d0bb` 及后续路径消费者 | `subject_id` 首版只校验非空；workspace/archive 之后直接复用了这个不安全标识，没有某次修复把合法 ID 变成路径穿越值 |

#### 7.1.4 P3 的来源解释

P3-01 至 P3-08 分别是项目真实验收、共同知识自动同步、情绪因果验证、记忆 benchmark、Windows 安装包、完整运维面、OpenAPI 契约和供应链 provenance 的成熟度缺口。它们可以说明“对应能力尚未达到稳定产品或可复现实验标准”，但不能据此声称某个修复引入了回归。后续实现这些能力时仍可能产生新问题，因此应按第 8 节门禁逐项落地。

#### 7.1.5 对此前修复结论的影响

- 真正应标记为“修复引发新故障”的只有 P1-03、P1-06、P2-12；这三项需要先回到引入提交前后的行为合同，修复新回归时保留原修复目标。
- P1-02、P1-05、P1-07、P1-08、P1-09、P1-10、P1-11、P1-12、P2-04、P2-06、P2-11、P2-13、P2-16 应把历史状态从“已修复”改成“部分缓解/未关闭”，不能再以已有回归测试作为完整关闭证据。
- 其余原生问题主要是此前范围遗漏、跨模块集成未审或能力首版合同过弱。它们说明审计门需要扩大，不说明现有全部修复都不可信。
- 这一区分直接决定修复策略：严格次生项优先做最小兼容修复；原生项需要补合同；修复不完整项需要保留已有正确改进，只补缺失路径，避免整体回滚再次制造问题。

## 8. 建议修复顺序和发布门

为避免“越修问题越多”，不建议同时重构所有模块。每项按一个故障合同、一个小提交、一个审计复核推进。

### Gate A：先恢复运行时真实性和隐私

1. 修复 P1-01 prepared-action/sleep 死锁，增加 restart + fatigue + prepared work 状态机测试。
2. 修复 P1-08 public state schema，建立未认证 endpoint 的字段 allowlist 快照测试。
3. 决定并实现 P1-13 approval 的真实语义；在完成前 UI 不得宣称“每次批准”。

### Gate B：修复导出和同意

1. 为 schema 32 建立显式 export ownership graph，逐表生成 expected/exported row count 对账测试。
2. 修复 training policy CAS、in-flight revocation、workspace subject scope。
3. 把 training temp 移入 data volume，增加分片、实时 byte budget、取消点和 Windows C:/Compose `/tmp` 回归。
4. 关机必须等待或隔离 export worker，在释放 process lock 前证明没有主体写者。

### Gate C：让完整性门真正可信

1. 先修 archive-aware event validation，确保正常归档不会报错。
2. 建立完整 registry 和统一报告 schema，把所有已有 `verify_integrity()` 纳入。
3. 加入 project execution/artifact、model resource、embedding 和 common knowledge 新检查。
4. 先以只报警模式运行 soak，再启用 P0/P1 自动 safe pause；避免 false positive 直接停掉主体。

### Gate D：长期存储、记忆和项目质量

1. 设计 keyring、云 read-through、本地 cold GC 和恢复演练，然后再允许云端释放本地段。
2. embedding 独立 budget/ledger/circuit，并改为候选集向量评分或 ANN。
3. 为每种 project output 建立可执行 validator 和 artifact digest，取消占位成功。
4. 修复迁移备份、真实历史 fixture、cloud 安装 profile、SMTP/unknown reconciliation。

### 每项修复的固定门禁

1. 修复前先提交失败复现测试或只读诊断证据。
2. 明确不可改变的主体数据、权限、隐私和兼容边界。
3. 只修改一个 ownership/state-machine 合同，不顺带重构相邻模块。
4. 执行局部测试、全量 291+ 测试、Ruff、Mypy、compileall、pip check、pip-audit 和 deployment audit。
5. 对 P1 修复追加 crash/cancel/concurrency/large-data 测试；对迁移/归档修复追加 restore 演练。
6. 复核数据库前后 row count、revision chain、event chain、artifact hash 和公开 API 字段 diff。

## 9. 部署判定

当前版本可用于：

- 本地、受监督、使用测试数据的研究开发；
- 短期功能验证；
- 继续完善主体状态、认知和交互模型。

当前版本不应被判定为：

- 可多年无人值守运行的稳定版；
- 已具备可靠完整训练/运行数据导出的版本；
- 已通过私人心理数据安全认证的公网服务；
- 已完成真实自主项目验收闭环或可证明高级记忆性能的版本。

生产候选的最低条件是：全部 P1 关闭、P2-01/P2-04/P2-05/P2-06/P2-10/P2-14/P2-15/P2-16 关闭，完成真实 Ubuntu 72 小时故障注入 soak、Windows 桌面 72 小时运行、备份恢复、归档 key 丢失/轮换演练、大数据导出和至少一种真实通讯渠道合同测试。

## 10. 只读声明

本次审计没有修复任何上述问题，也没有改变运行时行为。临时诊断全部位于系统临时目录并在命令结束后删除。仓库的唯一预期变更是本报告：`docs/audit/2026-08-15-full-readonly-audit.md`。
