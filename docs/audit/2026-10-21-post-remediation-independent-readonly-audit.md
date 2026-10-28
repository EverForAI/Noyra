# 修复后独立只读审计（2026-10-21）

## 0. 最终结论

**整体修复质量：中。最终建议：保留本轮修复主体，不整体回滚；权限 readiness 的新实现必须定点重修，不能把当前版本视为已全面收敛。**

本轮独立确认 **5 个根因级问题：P2 三项、P3 两项。其中原生问题四项，上一轮修复引入的次生问题一项。** F02 还包含一个优先级较低的本地读取表现，合并归入同一类终态收口问题，不重复计数。本轮未确认新的 P0/P1；这不等于证明项目不存在 P0/P1，也不等于“测试全绿即可宣布安全”。

- **不建议整体回滚。** XML/身份签名约束、文件发布 fencing、余额 RPC 重验、传输失败原子性和迁移连接取消等修改有清晰的正向价值。本轮重新运行回归后，没有证据支持将这些修改一并撤销。
- **F01 是明确的次生可用性回归。** 合法且权限正确的大目录可以通过严格健康检查，却被新增的 HTTP 权限快照门长期拒绝。它不是泄密后门，而是支持规模、扫描预算和快照寿命不相容。
- **对于已有大目录或慢权限扫描的 required-mode 部署，当前版本不能直接放行上线。** 优先定点修复 F01，而不是恢复每请求全目录扫描。若必须应急回退权限项，需要同时有入口并发、频率和探测合并控制，不能裸退回原有昂贵全扫路径。
- F02–F05 在修复前基线中也能复现，**不能归咎于本轮修复**，也不能靠回滚本轮提交消除。
- 本轮只新增本报告及证据文件；**没有修复源码、改变测试断言、提交、重置或执行回滚。**

## 1. 独立性、版本与审计方法

### 1.1 比较对象

| 项目 | 值 |
|---|---|
| 工作区 | `E:\我的项目\Noyra\开发版` |
| 修复前基线 | `cbdfaa04764e98ab8a979da3e19a1e803a95efe2` |
| 本轮被审计 HEAD | `170d476486c909f0e79384c0a931db12672a8c18` |
| 分支 | `codex/audit-boundary-remediation` |
| 上轮修复范围 | `f79ac1f`、`68c4eb5`、`c888e13`；另有测试对齐 `f5065b8` 和记录提交 `170d476` |

结论由本轮重新读代码、基线 diff、真实临时文件/数据库、独立探针和新回归结果构成，没有以旧审计报告或旧通过日志代替本轮验证。历史测试耗时只用于分片负载调度，不作为通过证据。基线只用于“原生/次生”归因，不作为现有实现正确性的保证。

基线和 HEAD 探针在仓库外的独立快照运行，不切换用户工作区。没有读取真实 `.env`、`.runtime` 或私钥；未调用真实资金转账、外部生产签名账户、云存储账户或第三方模型服务。

### 1.2 实际覆盖，不夸大“逐行”

- tracked 文件盘点：945 个。631 个代码、测试、部署、前端和依赖相关文件做了前后 SHA-256 比对。
- 462 个 Python 文件完成 AST 解析和敏感调用索引：生产源码 219 个、脚本 20 个、测试/辅助文件 223 个。
- 生产 Python 源码共 **143,475 行**。
- 对 48 个文件的关键函数、修改片段或风险边界进行了人工重点审阅；精确范围和“只是声明/片段”的情况记录在 coverage 中。
- 针对上轮改动，重点沿授权 → 外部发送/文件发布 → 返回 → 终态记账 → pause/restart 恢复链复查，而不只看新增的 `assert`。
- 新跑 Linux 全量测试、Windows 边界回归、真实非特权 Linux 用户补充测试、前端测试、类型/风格检查和三个事务回滚 shell 夹具。

**重要边界：这不是对全部 143,475 行人工逐行证明正确，也没有获得全项目路径/分支覆盖率。** 自动 AST/sink 扫描、读到一个文件的部分函数、测试命中一个模块，不能冒称该文件所有代码均已人工逐行通过。用户要求的“逐代码级别”在本报告中体现为具体函数、分支、状态、触发步骤与版本对照；未审完的每一行不会被伪装成已审完。

覆盖索引：
coverage.json（原始证据私下保留）

### 1.3 风险口径

**问题风险**和**修复风险**分别评价，互不替代：

| 问题级别 | 本报告的含义 |
|---|---|
| P0 | 无条件或极易触发的灾难性安全/资金/完整性损害，需要立即停止相关运行 |
| P1 | 严重的安全、数据或核心业务故障，优先处理，通常阻断发布 |
| P2 | 特定但合理条件下影响可用性、状态一致性或资源边界，需要安排修复 |
| P3 | 边缘路径、较低可达性或有限影响，允许按把握程度后置 |

| 修复风险 | 含义 |
|---|---|
| R1 低 | 小范围、行为边界明确，不改变持久化/恢复/共享执行语义 |
| R2 中 | 涉及共享基础设施、并发/取消、状态终结或运行契约，需跨调用者回归 |
| R3 高 | 涉及持久化结构、迁移协议、密钥/资金状态或大规模数据转换，需专项验证与回退设计 |

本报告中的 R 级别不是“修起来有多难”，也不是把高问题风险机械映射成高修复风险。

## 2. 问题总表

| ID | 已确认问题 | 问题风险 | 建议修复风险 | 来源 | 证据强度 |
|---|---|---|---|---|---|
| F01 | 权限快照工作预算与合法目录规模/快照寿命冲突，健康存储长期被 HTTP 门拒绝 | P2 | R2 中 | **次生**，`c888e13` 引入 | 高：真实权限遍历、真实文件、旧/新版本对照 |
| F02 | 工具 action 在异步取消或本地读取租约失效时未收口，遗留 executing | P2；本地读子情形 P3 | R2 中；仅本地读收口可较小 | **原生**，基线同样复现 | 高：真实 DB/action；取消 reader 为受控阻塞夹具 |
| F03 | model.ledger 检查把流式 cursor 再全量聚合，绕开物化预算的目的 | P2 | R2 中 | **原生**，基线同样复现 | 高：有效 ledger 数据与真实检查器；未声称已复现 OOM |
| F04 | 共享同步 HTTP helper 的排队/等待预算不收紧到父 deadline | P3 | R2 中 | **原生**，基线同样复现 | 高：真实线程/信号量时序；未声称已发生晚到资金发送 |
| F05 | 已有审计文档代码块不满足锁定 Ruff 格式，完整 CI 格式门失败 | P3 | **R1 低** | **原生**，基线文件字节相同 | 高：锁定版本整仓库执行与基线文本复核 |

## 3. 详细发现

### F01 — 合法大目录在新的 HTTP 权限快照机制下长期不可用

**风险：P2。修复风险：R2 中。归因：本轮次生。置信度：高。**

**位置**

- `E:\我的项目\Noyra\开发版\src\noyra\core\at_rest.py:634–687`：`request_health()`、`_monitor_permissions()`。
- `E:\我的项目\Noyra\开发版\src\noyra\core\at_rest.py:1462–1495`：预算限定的 `_private_permission_error()`。
- `E:\我的项目\Noyra\开发版\src\noyra\service.py:2942` 及 `9422–9430`：HTTP readiness 和使用该门的 API 入口。

**根因**

新增后台 monitor 要求一次扫描使用 `checked_at + 4.0` 的预算；POSIX 分支另外在第 100,001 个枚举条目拒绝继续。HTTP 只接受从**扫描开始时间**算起不超过五秒的结果。后台每次从头全扫，失败后没有续扫状态；因此总量超限或每次扫描都超预算时，后续请求再多也不能完成一次合格扫描。

这些条件没有与其他容量契约统一：备份默认文件数是 100,000，配置可允许更大；权限统计还包含目录和根等条目，不等于“文件数”。即使文件数低于默认备份上限，目录加文件也可能越过权限门的新上限。Windows 分支同样新增了条目上限，并且 ACL 子进程仍受其独立超时约束，不能把注入的四秒理解成 Windows 全程四秒硬截止。

**触发条件**

1. `AtRestConfig.mode == "required"`。
2. volume/keyring 逻辑有效，文件拥有者和 0700/0600 等权限均正确。
3. 被计入权限审计的总条目数超 100,000；或正常存储上的每次全量审计都不能在相容时间内形成有效快照。
4. 请求进入新的 `request_health()` / `require_request_ready()` 路径。

development 模式不受这一新门影响。不是任何大小的目录都会失效，也不是所有 HTTP 路由都走这一门。

**影响**

- `/health/ready` 与使用该门的 API 持续/反复返回不可用，即使严格的 `health()` 仍认为存储健康。
- 可阻断已存在较多文件/目录的升级部署、启动后的 readiness 或 API 使用；健康探测的不同视图还可能给运维造成矛盾信号。
- 不会因此放宽权限；这是 fail-closed 的**可用性回归**，不能描述成权限被绕过或密钥泄漏。

**本轮复现**

探针只替代 volume/keyring 的外部条件，**没有替代权限遍历、stat、目录扫描和文件模式检查**。

- 初始真实文件树：100,002 个 0600 文件，加 0700 根；HEAD 严格健康为 true，新 request 样本为 pending/expired 或 work budget 拒绝。基线严格健康同样为 true，而旧服务没有这个新增快照拒绝机制。
- 补充真实混合树：**98,002 个文件 + 2,000 个子目录 + 根 = 100,003 条目**。文件数低于默认备份上限，严格健康仍为 true，HTTP 快照门仍拒绝。
- 将底层扫描 deadline 单独给到未来 600 秒，仍返回 `private permission audit exceeds its work budget`，证明**计数上限本身**就足以触发，不依赖本机扫描恰好慢于四秒。
- 混合树 HEAD 严格检查约 4.57 秒；较早全文件树约 6.19 秒。这些时间受机器和并行测试 IO 影响，**不是生产容量 benchmark**。

证据：
mixed-tree-head.json（原始证据私下保留）、
mixed-tree-baseline.json（原始证据私下保留）、
count-head.json（原始证据私下保留）、
probe-head.json（原始证据私下保留）。

**归因依据**

`cbdfaa0` 没有 `request_health()`、五秒权限快照或这里的后台条目/时间门。`c888e13` 新增该机制并将部分 HTTP 门切换过来。相同合法私有树在基线严格检查通过，在 HEAD 也通过严格检查，却被新门拒绝；不是根据“刚改过所以有罪”猜测归因。

**根因修复建议及次生风险**

- 保留“请求不各自递归全扫”和 singleflight 的目标，统一支持规模、条目口径、扫描预算、刷新间隔与新鲜度定义。
- 大树支持若采用分片/增量验证，需要扫描代次、变更失效、完整快照发布和未完成状态，不能把“扫过一部分”当成“整树健康”。若采用可配置完整扫描预算，也必须同步配置可完成的快照契约并有启动容量预检。
- 不应仅把五秒改成很长、忽略 error、提高计数后宣布修复；这些做法可能放大陈旧权限授权窗口，且仍保留规模错配。
- 增加 below-limit/at-limit/above-limit、目录多但文件少、慢磁盘、扫描途中权限变更、Windows ACL 慢启动和普通 WAL 创建/删除的联合回归。
- **不建议裸回滚成每请求严格递归全扫**，否则会重新暴露旧的昂贵请求路径。该项需要定点重修，而非以整体回滚替代设计修正。

### F02 — 取消/失效后的 action 终态记账不完整

**风险：P2，附带的本地读取表现为 P3。修复风险：R2 中。归因：原生。置信度：高。**

**位置**

- `E:\我的项目\Noyra\开发版\src\noyra\capability\tools.py:178–217`：`fetch_document()` 的 start、await、异常和 finish。
- 同文件 `:67–102`：`read_text()` 的执行与终结。
- `E:\我的项目\Noyra\开发版\src\noyra\core\actions.py:166–177,247–286`：重复 start 限制、启动恢复。
- `E:\我的项目\Noyra\开发版\src\noyra\cognition\cycle.py:433`、`deliberation.py:431,681–697`：生产调用和 open action 门。
- `E:\我的项目\Noyra\开发版\src\noyra\core\runtime.py:258–324`：restart 恢复与 pause/resume 的区别。

**根因与两个表现**

1. **异步 web 读取（P2）**：action 已切成 executing 后等待 `reader.fetch()`，这里只捕获 `Exception`。`asyncio.CancelledError` 不在这个捕获范围内，因此取消直接穿透，既没有 failed/cancelled/unknown 终态，也没有对应终态行为日志。本轮加上的窄 `accounting_scope()` 修复了普通失效/异常收口的一部分，但不覆盖这个取消分支。
2. **本地 read_text（P3）**：真实文件读完后如果租约失效，最后 `actions.finish()` 仍以普通事务运行，可能被当前租约提交门拒绝。成功/失败记账都没有像新 write/web 分支一样进行窄终态收口，最终仍是 executing。

第二项在当前源码中没有找到内部生产调用者，不能把它夸大成现有远程文件泄漏通道；它是公开工具/兼容路径的状态一致性问题。两项统一归在“边界异常之后终态收口不完整”，不重复计算为两个高优先级漏洞。

**触发条件**

- 先有合法授权并进入 executing；然后取消挂起中的 web task，或在本地读取期间使当前 lease 失效。
- 普通成功读取、起始前授权失败、已有的 write 原子发布成功场景不等于这个触发。
- 生产 web 路径确实由 cognition cycle 和 action deliberation 调用，不只是无人使用的测试接口。

**影响**

- 永久保留 executing，重复同一幂等 action 不能正常重新 start。
- deliberation 的 `_has_open_action()` 会将 prepared/executing/unknown 视为未结清，遗留记录可能阻挡后续行为决策。
- pause/resume 不会自动执行启动恢复；重启可把 executing 转成 unknown，但 unknown 仍需显式核对，**不是重启即全部恢复正常**。
- 这次证据证明的是状态悬挂和可用性受损；没有证明越权读取、重复外部发布或资金重复支付。

**本轮复现与归因**

- 真实数据库、真实 CapabilityGrant 和 ActionLedger；reader 是能受控等待的夹具，在执行中 `task.cancel()`，两版本最后均留下 `web_read / executing`。
- 真实授权文件读取，读取返回前 invalidate gate，两版本均抛 `OperationInvalidated`，留下 `filesystem_read / executing`。
- `cbdfaa0` 和 `170d476` 的同一探针输出一致，故判为原生遗漏，不是上一轮引入的次生问题。

证据：
extra-head.json（原始证据私下保留）、
extra-baseline.json（原始证据私下保留）、
probe-head.json（原始证据私下保留）、
probe-baseline.json（原始证据私下保留）。

**根因修复建议及次生风险**

- 显式处理取消，仅对已经 start 的这一 action 做窄终态结算，然后重新抛出取消；不能吞掉取消并继续发起操作。
- 对本地读取失效区分“未向调用者交付内容”和“结果已知但记账跨 fence”，采用适当失败/取消终态，不能把过期执行结果直接当正常成功返回。
- 不把整个工具执行包进 `accounting_scope()`；授权、capability use、新 action、新网络请求不得绕过租约。
- 回归需要检查取消前、发送/读取后、终态提交时、pause/resume、幂等重试、手工核对，且不得增加第二次外部请求。
- 根因修复不需要回滚现有 web/write 的授权与发布防线，反而应复用它们的窄记账原则。

### F03 — 模型账本完整性检查的内存聚合缺乏相应预算

**风险：P2。修复风险：R2 中。归因：原生。置信度：高（代码与预算旁路）；实际 OOM 概率依历史量和部署资源而定。**

**位置**

- `E:\我的项目\Noyra\开发版\src\noyra\core\integrity.py:143–152,200–246`：row 计量及 cursor 的物化限制。
- 同文件 `:3826–3899`：`_check_model_ledger()` 的 `call_records`、`attempts_by_call`。
- `E:\我的项目\Noyra\开发版\src\noyra\core\payload_codec.py:63–89`：plain/compressed 内容预算的不同计量。

**根因**

cursor 的 row/byte 物化限制主要在 `fetchall()/fetchmany()` 生效；流式 `fetchone()/__iter__()` 为支持大历史扫描不按相同规则拒绝所有累计行。这个设计本身不必然有错。

但 `_check_model_ledger()` 流式读取后，又把**所有 call record** 放进 `call_records`，把**所有 attempt record** 放进按 call 分组的列表，到全量装完后才验证关系。这样虽然表面没调用 `fetchall()`，实际 retained state 仍随全历史量线性增长，且这些 Python 集合没有单独条目/字节预算。普通未压缩内容也不能依靠“扩展后的 compressed bytes”预算兜住这个集合。

这不是“流式扫描多于 max_rows 就一定错”的泛化结论；问题落在**流式后重新无界保存完整历史**的具体实现。

**触发条件**

- 执行包含 `model.ledger` 的 deep/manual 完整性审计。
- 模型 call/attempt 历史持续增长，尤其是大量短请求/短记录，未先耗尽压缩展开字节预算。
- 数据可以完全有效，不需要破坏 hash 或直接写入恶意 SQLite 文件。

**影响**

- 完整性审计的工作集随历史增长，设置的物化限额不能控制这里的聚合集合。
- 可能形成内存峰值、检查被硬截止反复中断，资源紧张时可能影响服务或触发 OS 内存回收。
- 子进程与 hard deadline 是实际存在的缓解，不能说系统毫无限制；但是**时长限制不等于保留对象数量/内存限制**。
- 没有证明能够伪造 hash 通过验证，也没有在本轮主动耗尽内存触发 OOM，不能把这个问题夸大成已实现的数据完整性绕过或单请求远程 OOM。

**本轮复现与归因**

用 ModelLedger 正常创建 40 个有效、短小的 prepared calls；设置：

```text
max_rows_per_check = 2
max_bytes_per_check = 4096
max_value_bytes = 4096
check_ids = ("model.ledger",)
```

两版本均返回：

```text
status = ok
rows_examined = 40
bytes_examined = 8540
model_calls = 40
model_attempts = 0
```

`bytes_examined` 是检查器计量值，不是实测 RSS；这个输出用于证明设定的物化预算并未约束这里的 retained aggregation，再结合具体字典/列表的 O(N) 构造确认根因。没有将“读过 8540 bytes”谎称为“精确测得占用了 8540 bytes 内存”。

基线与 HEAD 均复现，相关聚合逻辑不是本轮修改，因此判为原生。

证据：
extra-head.json（原始证据私下保留）、
extra-baseline.json（原始证据私下保留）。

**根因修复建议及次生风险**

- 优先将 call/attempt 关系检查改为有序、分组、限定工作集的验证，或给这两个集合引入独立 retained-state 预算；预算到达应明确 degraded/incomplete，而不是跳过记录后返回 ok。
- 不能把全局 streaming cursor 简单改成“超过两万行就全部失败”。这会给事件链和其他本来正确的大历史流式验证制造新的可用性回归。
- 保留断链、孤立 attempt、顺序、状态组合和 hash 验证；任何优化都不能只检查前 N 条。
- 回归应包含很多短记录、较少大记录、压缩/不压缩混合、合法超大历史、损坏记录在尾部、审计中取消/硬超时。

### F04 — 同步 HTTP 共享 helper 不遵守更短的父操作预算

**风险：P3。修复风险：R2 中。归因：原生。置信度：高。**

**位置**

- `E:\我的项目\Noyra\开发版\src\noyra\core\http.py:179–234`：`call_sync_http_with_deadline()`。
- `E:\我的项目\Noyra\开发版\src\noyra\core\deadline.py:25–43`：父操作预算的读出和收紧逻辑。

**根因**

helper 虽会 `copy_context()` 给工作线程，但入口的 `_SYNC_HTTP_GATE.acquire(timeout=timeout)` 和后续 `outcome.get(timeout=remaining)` 只看显式参数及本地耗时，没有把它与当前 `operation_deadline()` 剩余预算取最小值。**传播 deadline 的 ContextVar 并不等于 helper 自己按它限时等待。**

**触发条件**

- 当前父操作剩余预算比 helper 的 timeout 短。
- 共享的四个同步 HTTP worker 已被使用，或工作函数在父预算过期后仍未返回。
- 无嵌套短预算、且 transport 自己及时检查父预算的常规调用不一定受影响。

**影响**

- 请求线程/调用者可能在其父预算已经耗尽后继续排队或等待，真实时长契约不一致。
- 某些 worker 会自行再次检查 deadline 并拒绝发送，因此不能从本探针推导“暂停后必然晚到发钱”或“已有父预算的所有 transport 都继续发送”。
- 共享 worker 数仍有四个的上界，不是无限建线程问题。
- 本轮将其列为 P3：确定的是父预算一致性缺陷，尚无证据支持升级成独立的高危外部副作用漏洞。

**本轮复现与归因**

使用真实四个 worker 占满信号量，在父操作预算 0.05 秒内调用 `helper(timeout=1)`，占位任务在 0.35 秒释放。两版本都在约 0.351–0.352 秒才返回，并且普通 callback 是在约 0.351 秒才开始，未按 0.05 秒拒绝启动。

这是线程/计时隔离探针，callback 不发网络请求；它验证 helper 语义，不是假装做过生产 RPC 的晚到发送实验。相关 helper 在基线与 HEAD 相同，本轮新增迁移 transport 对它的使用扩大了需要统一验证的调用范围，但**没有凭此把原生根因改判为次生**。

证据：
deadline-head.json（原始证据私下保留）、
deadline-baseline.json（原始证据私下保留）。

**根因修复建议及次生风险**

入口即将有效总预算收紧为显式 timeout 与父剩余预算的最小值；gate 等待、启动前检查、结果等待使用同一绝对截止时间。到期仍需正确通知 timeout hook，且 slot 必须由真实退出的 worker 释放，不能超时后提前释放从而破坏线程上界。

虽然代码可能只改几行，helper 被余额、签名、embedding、common knowledge 和 migration 等调用，故综合修复风险按 R2，而不是只按补丁长度判 R1。需检查 timeout 到 unknown/failed 的映射及子 worker 不晚发、不泄漏连接/slot。

### F05 — 既有审计文档的 Python 代码块使完整 CI 格式门失败

**风险：P3。修复风险：R1 低。归因：原生。置信度：高。**

**位置**

- `E:\我的项目\Noyra\开发版\docs\audit\2026-10-20-independent-code-audit-current-worktree.md:285–292`：Python fenced code block。
- `E:\我的项目\Noyra\开发版\.github\workflows\ci.yml:48`：`python -m ruff format --check .`。
- `E:\我的项目\Noyra\开发版\requirements-dev.lock:1471`：锁定 `ruff==0.16.2`。

**根因**

已有 Markdown 审计文档内的 Python 示例不符合当前锁定 formatter 规则。该版本 Ruff 的整仓库格式检查会处理这个 Markdown 代码块；因此只检查 `src tests scripts` 全部通过，不等于 CI 的 `.` 检查通过。

**触发条件**

使用项目锁定依赖执行 CI 中的完整 `ruff format --check .`。不是运行 Noyra 服务才触发，也不需要某种权限/网络攻击。

**影响**

完整质量工作流的 format step 失败，可导致 PR/发布门阻塞。纯文档示例格式不改变服务安全、资金或持久化语义，所以按 P3，而不是声称存在运行期高危问题。

**本轮复现与归因**

- Linux 使用锁定 **Ruff 0.16.2** 对本轮源码快照整仓库检查：lint 通过，format 返回 **exit 1**，明确指出上述唯一文件，`1 file would be reformatted, 694 files already formatted`。
- Windows 整仓库也同样指出该文件，但 Windows Ruff 是 0.16.4；故以 Linux 锁定版本为正式结论依据，不能以两个工具版本相近就冒称均完全锁定。
- 该文档在 `cbdfaa0` 与 HEAD **字节相同**；将基线原文交给锁定 formatter 的 stdin check，同样 exit 1。故是原生质量门问题，不是上轮源码修复的新回归，也不是本次新报告引入。
- 本次审计证据脚本以 `.py.txt` 保存；没有为了存证向项目新增未格式化的可执行 Python 模块。

证据：
ruff-pinned-all-format.log（原始证据私下保留）、
doc-format-pinned-baseline.json（原始证据私下保留）、
tool-versions.json（原始证据私下保留）。

**根因修复建议及次生风险**

下一轮仅按锁定 formatter 整理该示例代码块，保持演示语义不变；重跑整仓库 format check。不应为一处文档格式全局关闭 Markdown 格式检查、放宽 CI 或修改历史探针结论。变更局部且无运行期行为变化，R1 低。本轮只读，不自动格式化该文件。

## 4. 上轮修复的逐类保留/回滚判断

| 上轮修改类别 | 本轮判断 | 理由与保留条件 |
|---|---|---|
| 统一 DTD-free、建树期有界 XML | **保留** | 编码感知解析替代前缀正则判断；节点/深度/文本预算在建树时执行。保留正常 CDATA；本轮相关回归通过 |
| 钱包迁移 identity challenge 严格字段/domain/canonical JSON | **保留** | 当前客户端绑定字段兼容，拒绝任意文本签名方向正确；没有确认它重新变成任意签名 oracle |
| 旧 TransferSession 最后一块/恢复前缀修正 | **保留** | `min(next_chunk * chunk_bytes, byte_size)` 与最后短块语义一致，有额外 receipt 校验 |
| 加密 receive 验证后临时文件原子替换 | **保留** | 不再在认证失败时破坏旧目标；临时文件 0600、校验在 publish 前 |
| runtime migration 私有目录创建模式 | **保留** | 新建父/子目录与 required 私有树规则一致；不建议撤回为更宽模式 |
| 文件 publish 与租约短 commit scope | **保留** | fsync/准备不持长 gate，短 replace 与 pause 串行化；before/after 发布用例重新通过 |
| web 源/能力在发送边界和结果返回时重验 | **保留并补 F02** | 防线有效；取消终态遗漏是原生问题，不需要撤回发送前重验 |
| 余额 RPC 配置重读、发送 trace、失效 unknown | **保留** | 两阶段查询重新核验，already-claimed attempt 的 accounting 窄化，不在 bypass 中新建/续租/发布余额快照 |
| 迁移 transport 总时长与可中断 socket 清理 | **保留并补 F04** | 新总时长/关闭实验通过；保留连接取消能力，另统一父预算，不应连同这些防线一起撤销 |
| migration agent header/body 时限、并发、framing | **保留** | worker 上界、一次连接一次请求和拒绝歧义 framing 改善边界，不是已确认回归来源 |
| HTTP 权限 singleflight/短快照 | **不能按现状作为最终完成项；定点重修 F01** | singleflight 方向保留；规模/时间契约不相容。大树/慢扫描 required 部署在修前不放行，不建议裸退旧全扫 |
| readiness 测试 mock 契约对齐 | **保留** | 与新接口相符；本轮独立运行通过。通过本身不能替代 F01 的容量测试 |

**明确的回滚结论：不整体回滚，不因原生遗漏回滚其他正确修复。** 权限项应做前向、定点根因修正；发生实质 readiness 故障又不能及时前向修时，才考虑受控局部回退，且必须防止旧的每请求昂贵扫描重新成为攻击/过载放大器。本轮未执行任何一种回滚。

### 为什么整体只评“中”，不是“高”或“低”

- **不是低：** 修复触及正确的外部副作用边界、身份签名范围和认证前资源预算，原子发布与 unknown 收口有真实实现，也有较系统的新增回归。本轮 Linux 全量 Python 测试没有失败；但完整格式门仍有 F05，不能宣称所有质量门全绿。
- **不是高：** 权限项把“单请求便宜”与“系统始终有机会完成一次合法大目录审计”两个要求分开处理得不充分，引入了真实的条件性 readiness 回归。部分同族终态/预算路径仍有遗漏，测试未覆盖容量、取消与父预算组合。
- **中：** 总体净收益应保留，但还不能称为全面、无次生影响地收敛。
- F02–F05 是原生问题，**没有因为它们存在就机械扣成“上轮新引入五个漏洞”**。本次降低上轮质量评价的主要证据是 F01 以及相关测试盲区。

## 5. 本轮新验证结果与异常解释

### 5.1 结果总表

| 验证 | 本轮结果 | 说明 |
|---|---|---|
| Linux Python 3.12.3 全量 | **2924 passed / 17 skipped / 0 failed；另 288 subtests passed** | 独立源码快照；2941 collected nodes；219 个测试文件；4 个隔离分片约 2533 秒 |
| Windows Python 3.12.10 重点边界 | **198 passed / 28 skipped / 0 failed** | 发布 fencing、签名、余额、文件竞态、迁移 HTTP、at-rest、升级边界、web 合约；约 277 秒 |
| Linux nobody 真实非特权用户原始补跑 | **103 passed / 33 skipped / 9 failed** | 九项均在访问本机 `/etc/noyra/storage-identity.conf` 时因父目录不可穿越而失败，原始失败完整保留，不算产品断言通过 |
| nobody 首次主机配置隔离 | **110 passed / 33 skipped / 2 failed** | 剩余两项在 `/etc/noyra/upgrade-channel` 的同类主机配置访问失败；日志也保留 |
| nobody 完整主机配置隔离后 | **112 passed / 33 skipped / 0 failed** | 仅外置 pytest plugin 将三个主机配置查找改到不存在的临时控制路径；真实 nobody、真实权限/文件操作不模拟；源码、测试断言和超时未改 |
| Node v24.15.0 前端 | **116 passed / 0 failed / 0 skipped** | `node --check` 两个应用脚本通过；三组 frontend suites |
| 静态站点 | **4 个双语页面检查通过** | 链接、资源、fragment、语言与安全合约；不是完整视觉验收 |
| Ruff（src/tests/scripts） | **lint/format 通过；462 files already formatted** | 只 check，没有执行格式化写入 |
| Ruff（整仓库，锁定 0.16.2） | **lint 通过；format 失败（F05）** | 唯一失败为基线已有 Markdown 示例；不把局部检查冒称完整 CI 全绿 |
| Windows mypy | **442 source files 通过** | 显式类型目标 Python 3.12，cache 在仓库外 |
| Linux mypy | **442 source files 通过** | 项目默认类型目标 Python 3.11；运行解释器仍为 3.12 |
| shell 事务回滚夹具 | **3/3 通过** | control-layout、upgrade-components、migration-components；是隔离夹具，不是真实部署回滚 |

这些验证有交叉，**不能将 2924、198 和 112 相加当成互不重复的独立用例数量**。Windows 的 28 个和 Linux 的 17 个 skip 都没有伪报为通过；Linux cloud/boto3、Windows/PowerShell 和角色约束等平台/依赖未满足的部分是覆盖边界。

工具版本见 `E:\我的项目\Noyra\开发版\docs\audit\2026-10-21-independent-readonly-evidence\tool-versions.json`。Linux 使用锁定 pytest 9.0.3 / Ruff 0.16.2；Windows 为 pytest 9.1.1 / Ruff 0.16.4，因此 Windows 作为补充平台兼容证据，不冒称完全锁定环境。关键运行依赖的 httpx/httpcore/cryptography/pydantic/eth-account 两侧版本相同。

原始 nobody 失败说明测试会受到宿主已有控制目录权限影响。为了验证真实非特权行为，没有 chmod、删除或读取本机受保护的配置，而是把三个配置查找隔离到不存在的 `/var/tmp` 路径。**这是测试环境输入隔离，不是修改生产源码/断言后把失败“刷绿”。** 最终隔离结果与原始失败分开保存、分开报告。

另有一轮独立 probe 的初始夹具错误：web grant 错写 `domains`，项目正确拒绝，随后按真实契约改为 `hosts`。该初始错误日志保留；它不是产品漏洞，也不是源代码修复。

### 5.2 主要证据入口

- 机器可读汇总（原始证据私下保留）
- Linux 新全量分片清单（原始证据私下保留）
- Windows 边界结果（原始证据私下保留）
- 前端结果（原始证据私下保留）
- nobody 原始失败（原始证据私下保留）
- nobody 隔离后结果（原始证据私下保留）
- 明确列出的主机隔离 plugin（原始证据私下保留）

四个 Linux 分片均有本轮 `.log` 和 JUnit `.xml`；Windows 与 nobody 最终运行也有 XML。原始结果留存，未把旧轮次 log 拷贝过来充当新执行结果。

## 6. 排除的候选、保留的取舍与未证明事项

以下情况不凑成额外漏洞：

1. **HTTP gzip 炸弹候选被排除。** `E:\我的项目\Noyra\开发版\src\noyra\core\http.py:634–636` 在 body 读取前拒绝非 identity `Content-Encoding`，不能忽略这个校验再声称存在已确认解压炸弹。
2. **同一 action 并发重复执行未证实。** start 的事务和 prepared→executing 条件有约束；不能只看到幂等键就声称可双发。
3. **余额查询的 request_count 保守递增不是实际资金超额支付。** 余额 RPC 和支出执行引擎不是同一条资金发送路径。
4. **短权限快照带来检测窗口是实际行为变化。** 根普通 mode/owner 变化会对比 signature，嵌套权限和某些 Windows ACL 变化依赖刷新。五秒不是零窗口；但没有独立证明超出该明确缓存取舍的权限绕过，故不另报“第二个 F01 安全漏洞”。若产品要求所有嵌套 ACL 变化必须即时生效，该契约本身需另行强化。
5. **challenge 未携带过期字段不自动等于任意签名漏洞。** 必须结合 migration domain、绑定字段、nonce 和目标验证判断；本轮没有把合法 migration 证明需求误当成任意外部授权。
6. **迁移请求有总时长限制，不代表所有父 deadline 已统一；反之 F04 也不抹杀独立总时长和 socket abort 的有效性。** 两个层次分别验证。
7. **测试通过不证明实盘安全。** 真实 RPC 链变化、链重组、真实 cloud 凭证、断电持久化、实际公网反向代理和攻击流量不由这轮离线回归覆盖。
8. **没有执行新的在线漏洞库扫描。** 依赖锁、CI 安装/审计入口做了静态盘点，但不能据此声称当前全部依赖没有 CVE。Python 3.11 只有类型目标检查，未在该版本解释器重跑全量。

## 7. 人工重点审阅的边界图

| 边界 | 人工重点 | 覆盖限制 |
|---|---|---|
| 运行权/持久化 | admission、runtime pause/resume/recovery、database transaction、ActionLedger | 不等同于全部数据库迁移 DDL 的逐行证明 |
| 文件/静态加密 | 私有树、keyring、volume、backup restore、文件句柄相对读写与 publish | 没有真实 BitLocker/LUKS 生产磁盘、断电或海量生产数据验收 |
| HTTP/迁移 | DNS/响应边界、sync worker、abort、agent HMAC/nonce、transfer、bundle/subject restore 关键分支 | 不等同于所有迁移状态组合和部署拓扑的穷尽证明 |
| 模型/钱包 | gateway 异常、模型账本预算、embedding 请求、余额 acquisition、签名/广播/unknown | 不调用实盘资金或真实模型；keystore 只重点看 KDF 等片段 |
| 交互/公开/隐私 | WeChat XML、发送取消分支、erasure 恢复片段、public HTML 转义与读取预算 | 不把读过预算声明冒称整个 public archive/erasure 模块人工审完 |
| 认知/知识 | cycle/deliberation 的 fetch/commit/open-action 链、self-modification apply、knowledge sync HTTP | 其他认知算法以机械扫描和全量回归为主，不宣称逐行证明算法语义 |
| 前端/部署 | app/admin 动态渲染入口、frontend 合约、部署权限/子进程/回滚入口 | 没有做完整 UI 视觉或真实系统安装；shell rollback 为夹具 |

更细的每文件记录以 coverage 为准。没有人工 focus 的文件，仍可能经过 AST/静态扫描和测试，但不是“无问题证书”。

## 8. 下一轮修复应如何安排（本轮不执行）

1. **先 F01。** 在不放回昂贵每请求扫描的前提下，解决合法支持容量/扫描预算/快照有效期的整体契约；先补独立反例，再实现。
2. **再 F02。** 显式取消收口 + 本地读终态隔离；只结算既有 action，保留 cancellation 传播和 lease 对新副作用的禁止。
3. **随后 F03。** 给模型检查器 retained state 加正确上限或真正改为有界关系验证；不全局误伤大历史流式审计。
4. **F05 可在下一轮以低风险文档整理单独处理；F04 可按用户 P3 策略后置。** 该项根因确定，但影响共享 helper，需给足所有调用者超时映射回归，不能因为补丁短就盲修。

每项独立提交、独立失败复现/修复证明、保留原始日志；回归既检查“该拒绝的拒绝”，也检查“合法慢/大/重试操作仍可完成”。不要为通过而删除断言、扩大测试时限或把 uncertain 自动改成 succeeded。

## 9. 只读完整性与交付

- 审计前后的 631 个文件 SHA-256 **完全一致**。
- HEAD 仍为 `170d476486c909f0e79384c0a931db12672a8c18`；tracked diff 为空。
- 原先的 22 个 `output/20261020-audit-*` 未跟踪文件保持原状，未提交、覆盖或删除。
- 本轮只新增本报告与其证据目录；探针临时数据和测试快照放在仓库外。
- 不创建修复提交、不自动回滚，不把审计文档作为生产源码变更。

证据：
manifest-before.json（原始证据私下保留）、
manifest-after.json（原始证据私下保留）。

复现脚本集中在证据目录，以 `.py.txt` 原样文本保存，避免只读审计证据成为项目新增可执行模块或被全仓库 lint 当成生产代码。复现时复制到仓库外并恢复 `.py` 后缀：`independent_probes.py.txt`、`mixed_tree_probe.py.txt`、`extra_probes.py.txt`、`deadline_probe.py.txt`；host isolation plugin 同理。它们的参数是**隔离源码快照根目录**，依赖与项目锁一致的 Linux Python 环境；创建/删除的测试数据位于脚本自己的 `/var/tmp` 随机临时目录。混合树探针会创建约十万条目，应在独立测试机或有空间的隔离环境运行，不能对真实 data_root 执行。

**结语：保留修复主体，整体质量为中；先定点解决 F01，再补齐 F02/F03，F05 做低风险文档整理，F04 按 P3 策略安排。没有证据支持一次性回滚整轮，也没有证据支持宣称所有问题已经消失。**
