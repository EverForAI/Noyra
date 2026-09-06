# Noyra 全项目二次全面审计报告

审计日期：2026-08-18  
审计基线：`main` / `87e059a`，加上当前工作树中尚未提交的 M42 修复与文档变更  
审计类型：只读、代码与运行合同审计；本次没有修改运行时代码、数据库或部署状态。

## 1. 执行结论

当前代码的局部修复质量较高：全量测试、静态检查和已有 M42 合同均能通过。但本次组合式复核发现，仍不能把运行时称为“可长期无人值守生产版”。主要剩余风险不在某个单独算法，而在跨边界时序：主体进程锁、SQLite 初始化、异步任务、文件秘密、云归档、环境配置和 HTTP 管理面没有由同一个所有权/epoch/租约模型统一约束。

本次确认的活动问题为 **P1 12 项、P2 20 项、P3 4 项**。没有确认当前默认配置下可直接远程代码执行的 P0；但 P1 中有多项会破坏主体所有权、同意、完整性或外部副作用边界，发布前必须处理。

最重要的结论：

1. “先检查 active/持锁，再 `await`，最后写库”的模式不是安全状态机。暂停、重置、完整性隔离和关机都可能在 await 期间失效。
2. 构造器仍有迁移、恢复和时钟修复等持久写入，而进程所有权要到构造完成后才取得；第二实例可以在未拥有主体时修改活库。
3. SQLite 与 secret 文件、归档对象、导出文件之间没有统一的事务/意图日志/租约；崩溃、取消或重启后会出现晚写、重复外部调用或未引用密钥。
4. 历史修复大量增加了单项合同测试，但缺少“重启 + 两主体 + 慢网络 + 取消 + 磁盘满 + 完整性隔离”的组合故障矩阵，所以“已 verified”不能等同于组合路径已安全。

## 2. 范围与验证

### 2.1 覆盖范围

- `src/noyra` 全部运行时：core、service、autonomy、cognition、mind、model、world、research、interaction、knowledge、sleep、部署入口。
- SQLite schema/迁移/触发器、主体归属、进程锁、恢复、导出、训练同意、冷归档、云归档、HTTP/API、前端和安装脚本。
- 既有审计及验收资料：`docs/audit/2026-08-15-full-readonly-audit.md`、`docs/implementation/m42-remediation-status.md`、`tests/contracts/remediation_acceptance.json`。

### 2.2 本次实测

| 检查 | 结果 |
|---|---|
| 全量 `pytest -q` | **746 passed, 3 skipped, 104 subtests passed**（3 个 skip 为 Windows 上 POSIX symlink/加密文件系统能力） |
| `scripts/audit-deployment.ps1`（串行复核） | **194 passed, 1 skipped，70.29% focused coverage**；All checks passed |
| 失败专项的串行复核 | 4 个完整性/关机测试全部通过；并行运行时的超时失败归因于资源争用，不作为产品缺陷 |
| Ruff check / format | 通过 |
| Strict Mypy | 通过 |
| Compileall / pip check | 通过 |
| `pip-audit -r requirements.lock`（UTF-8 环境） | 无已知漏洞 |
| `git diff --check` | 通过 |
| 审计专项最小复现 | 同意重启回滚、运行导出凭据泄露、跨主体 training record、模型池预算竞态、取消投递继续发送、完整性隔离期间 HTTP 写入均复现 |

### 2.3 限制

没有真实 S3/SMTP/第三方通讯账户、生产主机断电、clean Windows VM、真实 GitHub tag release 或多日 soak。这些正是用户已经决定留到最终发布候选阶段的门禁，不在本次活动缺陷计数中重复报错。

## 3. 分级定义

| 等级 | 缺陷风险 | 处理要求 |
|---|---|---|
| P0 | 直接远程代码执行、大规模泄露、不可逆主体损坏或未授权高危副作用 | 立即隔离 |
| P1 | 破坏主体所有权/完整性/同意/隐私，或会让 24/7 运行停滞、重复副作用 | 发布前修复并做故障注入 |
| P2 | 明显可靠性、安全、成本、性能或运维风险，有绕行方案 | 稳定版前修复 |
| P3 | 产品、文档、研究证据或工程成熟度缺口 | 排期；不得宣称已完成 |

“修复风险”表示改动本身引入回归或迁移事故的风险，不是问题严重度。`Ultra` 用于跨域状态/并发/加密/迁移和需要一次性设计的改动；`Max` 用于边界收敛、局部 API、前端和文档改动。

## 4. 风险总表

### 4.1 P1

| 编号 | 证据位置 | 问题 | 影响 | 修复风险 | 建议模式 |
|---|---|---|---|---|---|
| R2-P1-01 | `core/runtime.py:28-40`；`core/database.py:4197-4200,4368-4396`；`cognition/projects.py:140-158,2340-2382`；`service.py:2494-2498` | 取得进程所有权前已迁移/修复 DB、写 policy/identity，并恢复 project execution clock | 第二实例可改正在运行的活库；迁移失败可能替换 DB/WAL；旧实例的执行 clock 会被外部 `recover` 打断 | 高 | Ultra |
| R2-P1-02 | `cognition/cycle.py:322-475`；`operator_controls.py:86-205`；`model/gateway.py:86-100` | pause/reset 只有入口状态检查，没有 operation epoch/lease；await 返回后仍可提交 | 暂停后模型提案、目标、记忆、artifact 或 execution clock 继续落盘；reset 后旧协程污染新状态 | 高 | Ultra |
| R2-P1-03 | `service.py:450-493,2595-2620`；`cycle.py:326-332`；`service.py:2733-2756`；`embedding_gateway.py:174-203` | HTTP daemon handler、`to_thread`、provider worker 没有统一 shutdown drain | 主体锁释放后旧线程仍写 SQLite/文件或产生收费；新实例与旧 worker 双写 | 高 | Ultra |
| R2-P1-04 | `service.py:2494-2509,2602-2603`；POST 路由 `1114-1214` | startup integrity 已发现 corrupt/pause pending 时，HTTP 仍启动且认证 mutation 未隔离 | 恢复门未通过时仍可改配置、授权、投递和 common knowledge，进一步污染待修复数据库 | 高 | Ultra |
| R2-P1-05 | `service.py:2570-2593`；`.env.example:177-184`；`deploy/noyra.env.example:38-45` | 环境默认值每次 boot 强制覆盖 durable training policy | 用户 API 撤回记录/导出同意后，重启恢复为模板值，造成 consent drift 和隐私违规 | 高 | Ultra |
| R2-P1-06 | `core/at_rest.py:589-598,1315-1440`；`core/storage.py:110-129`；`archive.py:106-116` | at-rest 只验证 `data_root`，不验证 subject/workspace 等持久子根；Windows attestation 不验证 owner；archive keyring 不检查权限 | junction/symlink/嵌套挂载可把私密数据写到未验证卷；普通账户可伪造 attestation 或读取归档密钥 | 高 | Ultra |
| R2-P1-07 | `interaction/transport.py:196-240,292-305`；`research/provider.py:45-131`；`model/resources.py:173-320` | secret 文件与 SQLite 双写无 durable intent/outbox；崩溃点没有双向 reconciliation | 未引用的 API key/SMTP 凭据长期留盘并进入 backup；撤销后文件删除失败只能覆盖部分场景 | 高 | Ultra |
| R2-P1-08 | `core/archive.py:287-311,339-370`；`event_archive.py` 读路径 | keyring revision 要求每个 subject 从 generation 1 连续递增；segment fingerprint 冲突分支被 `continue` 静默跳过 | 新主体或长期休眠主体从 gen N 开始会无法读取合法冷数据；损坏 metadata 可能被误判为可用 | 高 | Ultra |
| R2-P1-09 | `core/runtime_export.py:25-33,805-823` | 导出 sanitizer 不识别通用 `token`、`session_token`、`cookie` | 已授权 runtime ZIP 外传时泄露 webhook/session/API 凭据 | 中 | Max |
| R2-P1-10 | `interaction/transport.py:683-690` | `_deliver` 捕获 `CancelledError` 后标记 unknown 但不重新抛出 | 取消后继续发送队列中的后续消息；SMTP 的后台线程还可能在关机后完成外部副作用 | 高 | Ultra |
| R2-P1-11 | `model/gateway.py:135-162`；`model/ledger.py:115-194,486-514`；`model/resources.py:2099-2114` | group 预算预检查与 ledger 授权分离，授权事务未同时校验 aggregate pool limit | 两个不同 resource group 并发时可突破共享池 token/cost/attempt 硬上限；实测 pool limit=1 却成功 2 次 | 高 | Ultra |
| R2-P1-12 | `model/resources.py:1822-1826,1905-2003`；`model/ledger.py:407-412` | model outcome unknown 后新选 key 并生成新物理 idempotency key 自动重发 | 未知是否已收费/产生副作用时可重复调用，绕过显式 retry/reconcile 门禁 | 高 | Ultra |

### 4.2 P2

| 编号 | 证据位置 | 问题 | 影响 | 修复风险 | 建议模式 |
|---|---|---|---|---|---|
| R2-P2-01 | `model/openai_compatible.py:97-159`；`world/source.py:319-384`；`core/archive.py:635-697` | model/world/S3 外部调用没有统一绝对 wall-clock deadline；S3 重试和 `to_thread` 可长时间阻塞 | 对端持续滴流即可占住 cognition tick，shutdown/pause 延迟；云黑洞 endpoint 可反复重试 | 高 | Max；云 worker 结合 Ultra |
| R2-P2-02 | `world/source.py:304-368` | SafeWebReader 先 resolver 检查公网，再由普通 HTTPX 重新解析，存在 DNS-to-connect TOCTOU | rebinding 到内网时请求已发出，事后 peer 检查不能撤销 GET 副作用或探测 | 中高 | Ultra |
| R2-P2-03 | `service.py:2390-2406`；`archive.py:600-608,712-730` | boto client 构造成功即 `ready`；provider id 不含 endpoint/region/account；无连接探测和 circuit | health 虚报 ready；切换同名 S3-compatible endpoint 后误认旧 verified replica，GC/read-through 出错 | 高 | Ultra |
| R2-P2-04 | `core/archive.py:1137-1253` | archive transfer queue 无 worker token/lease/rowcount fencing | 多 scheduler 重复上传；晚到失败者可把早先 verified 降回 failed，阻塞 GC | 高 | Ultra |
| R2-P2-05 | `core/storage.py:883-970`；多处 service resource mutator | training record 可由 caller 以另一 subject 的 event_id 创建；HTTP 资源 mutator 只按全局 ID 写，不校验当前 subject | integrity 发现前已形成跨主体 provenance/配置修改；多主体数据库会越权或污染导出 | 高 | Ultra |
| R2-P2-06 | `core/storage_lifecycle.py:68-110,123-150` | 低剩余磁盘时先归档/压缩/写 WAL，只有最后 `_assess` 才清理或禁止 cognition | ENOSPC 时紧急路径反而写放大，可能无法写 safe-pause/审计事件 | 高 | Ultra 或 Max（若接入统一维护 worker则 Ultra） |
| R2-P2-07 | `core/snapshots.py:90-147` | snapshot compaction 在 `BEGIN IMMEDIATE` 中全量 fetch/parse/压缩 | 多年历史会 OOM，并长时间阻塞所有 writer；压力期放大故障 | 高 | Ultra |
| R2-P2-08 | `core/database.py:4426-4445`；`core/storage.py:253-290`；`core/at_rest.py:826-843` | migration `.pre-migration-*.bak` 成功后不清理、不计 quota，并会进入 backup | 旧心理历史副本长期存在，隐私保留期和磁盘预算失真 | 中 | Max |
| R2-P2-09 | `service.py:670-722`；`core/operator_controls.py:382-395,516-567`；`core/storage.py:253-343` | 未认证 `/health` 每次执行 quick_check、WAL、全树 rglob/stat；Docker 每 30 秒调用 | 监控/攻击者可放大 I/O 和 SQLite 负载，且暴露运行元数据 | 中 | Max |
| R2-P2-10 | `service.py:2237-2242`；`interaction/projection.py:743-759`；`interaction/store.py:164-172` | JSON 响应没有字节预算；公开交互先 LIMIT 后过滤，diary/common-knowledge 也可一次物化大集合 | 约 100 MB 级响应可打满 worker/内存；旧公开记录会永久漏页 | 高 | Ultra |
| R2-P2-11 | `web/app.js:63-107,137-165,531-583` | 前端没有 request generation/AbortController；导出每 250ms 轮询且 `blob()` 全量缓冲 | 切 tab 后迟到的 mailbox/private response 可覆盖 public 视图；默认 120/min 限流使长导出必 429；1GB ZIP 占满浏览器内存 | 中 | Max |
| R2-P2-12 | `service.py:158-188,2000-2031`；`deploy/noyra.env.example:11-16` | token 只校验长度，不拒绝示例占位符，也不要求角色互异 | 忘记编辑模板时已知 bearer token 可认证；token 复用破坏最小权限 | 中 | Max |
| R2-P2-13 | `scripts/install-ubuntu.sh:38-45` | Ubuntu installer 原地更新同一 venv，未停服、分版本、原子切换或回滚；cloud 降 base 不卸载 extra | pip 中途失败形成混合版本，旧进程继续执行半升级环境 | 高 | Ultra |
| R2-P2-14 | `service.py:632-635,2187-2204` | socket timeout 是每次读的 idle timeout，不是整请求 deadline；慢 drip 可长期占用 worker | 非 loopback opt-in 部署可被 slowloris 耗尽 32 个线程 | 中 | Max |
| R2-P2-15 | `deploy/systemd/noyra.service:13-15`；`docker-compose.yml:8-37` | 永久配置/完整性失败会 `Restart=always`/`unless-stopped` 重启；Docker 日志无宿主上限 | 重启风暴、日志膨胀，掩盖根因并耗尽宿主磁盘 | 中 | Max |
| R2-P2-16 | `service.py:2742-2750`；`storage_lifecycle.py:68-105` | storage maintenance 在 async cognition tick 内同步扫描、归档、压缩和逐文件清理 | 大目录或慢磁盘阻塞 pause/shutdown/heartbeat，且没有取消 deadline | 中高 | Max；与 worker ownership 合并时 Ultra |
| R2-P2-17 | `core/archive.py:359-370` | 已记录 key ID 的 fingerprint 不一致时 `continue` 而非失败 | 归档 metadata 篡改可能绕过 key binding，直到后续读取才暴露 | 中高 | Ultra |
| R2-P2-18 | 当前 `git status`；140 个 tracked 文件改动、89 个 untracked 文件（含本报告）、约 27,556 行新增 | 修复成果仍是脏工作树，审计报告基线 commit 不包含实际代码 | 中断、误操作或磁盘损坏会丢失修复；无法从 commit 重放本次测试和发布 | 中 | Max（先做 checkpoint commit/分支） |
| R2-P2-19 | `core/event_archive.py:346-426`；`world/observation_archive.py:56-135` | 冷归档先写 provider 文件，再提交 SQLite 引用 | 崩溃/并发冲突会留下永久无引用加密对象，quota 和 orphan GC 失真 | 中高 | Max |
| R2-P2-20 | `core/storage.py:246-290`；`core/storage_lifecycle.py:38-58,190-218` | 主体 quota 扫描共享 root，未按 subject storage key 分域 | 一个主体的 workspace/archive/export 增长会触发另一个主体的 pressure、prune 和告警 | 中高 | Max |

### 4.3 P3

| 编号 | 证据位置 | 问题 | 影响 | 修复风险 | 建议模式 |
|---|---|---|---|---|---|
| R2-P3-01 | `service_contract.py:43-184`；`service.py:636-669,2187-2204`；`tests/test_m42_p3_07_api_contract.py:95-123` | API 合同只做声明到文档的单向检查，未反向枚举 429/503/411/413 等全局错误 | 客户端无法可靠实现重试、at-rest 不可用和请求过大处理 | 低中 | Max |
| R2-P3-02 | `web/index.html`；`web/app.js` | 后端已有 pause/resume/reset、unknown reconcile、training policy、delivery reconcile、common knowledge trust/import/sync，但 UI 未覆盖 | 运维必须手工 curl，容易误用高权限 token；不能称为完整控制面 | 中 | Max |
| R2-P3-03 | `docs/deployment/ubuntu.md:253-275` 等部署文档仍保留旧的“无 WSL/P1-02 未完成/P1-11 open”描述 | 文档会让操作者错误判断验收状态和升级方式 | 低 | Max |
| R2-P3-04 | `core/runtime_export.py:586-611`；`core/training_export.py:1955-2054` | 不带 `ExportControl` 的兼容 publication 路径仍有 rename 与数据库提交之间的崩溃窗口 | 直接调用兼容 API 时可能留下已发布文件但没有 durable success row；生产 job 路径已有清理，故不扩大为 P2 | 中 | Max |

## 5. 关键证据与复现摘要

以下复现均使用临时目录/临时 SQLite，不修改仓库：

1. **同意回滚**：API 将 `record_enabled/export_enabled` 改为 `False` 后关闭，再按同一模板启动；结果从 `False/False` 变回 `True/True`，policy version 增加。
2. **导出脱敏**：事件 payload 中的 `token`, `session_token`, `cookie` 解压 runtime ZIP 后仍包含原文。
3. **跨主体 training record**：主体 B 在禁用训练记录时产生 event，调用 `TrainingStore.record_event(subject A, event B)` 成功写入 A 的 provenance row。
4. **模型池预算竞态**：两个 group barrier race，共享 pool `daily_attempts=1`，最终成功授权 2 次。
5. **未知模型结果**：第一次 provider outcome unknown；等待 cooldown 后同一 logical request 产生第二个物理 idempotency key，并再次调用 provider。
6. **取消投递**：取消 `deliver_pending` 后任务正常返回，第一条为 unknown，但第二条仍被发送并完成。
7. **完整性隔离**：startup report 为 corrupt/pause pending 时，合法 admin token POST interaction 仍返回 `201` 并持久化。
8. **迁移原子性**：`Database._ensure_optional_features()` 在 transaction context 中使用 `executescript()`；注入后续失败时，SQLite 已隐式提交的前置 DDL/数据不会随外层 rollback 撤销。这是 R2-P1-01 的放大器，不能只靠现有 migration backup 解决。

这些结果解释了为什么现有 746 项测试仍能全绿：测试主要验证每个修复合同的正常路径，缺少上述跨组件组合和取消/重启时序。

现有 `tests/contracts/remediation_acceptance.json` 的“verified”状态仍可作为各历史问题的局部证据，但不能覆盖本报告新增的组合缺陷；在 R2-P1-01 至 R2-P1-12 关闭前，不应把矩阵总数直接解释为生产就绪。

## 6. 共同根因分析

### 6.1 架构边界

主体进程锁目前更像“主循环运行许可”，而不是数据库、文件、HTTP worker、云任务和恢复动作的统一 fencing token。`Database()`、构造器 recovery、HTTP server construction 和部分 store 初始化都发生在 lock 之前；因此“没有 ownership 就不能写”的原则无法成立。

### 6.2 状态管理

生命周期状态、integrity 状态、training consent、环境配置和外部任务状态分散在多张表/内存对象中，没有单一的 epoch。入口检查只能证明某一时刻允许执行，不能证明 await 后仍允许提交。未知结果也没有统一的 logical request 状态机。

### 6.3 异步与工具调度

HTTP daemon thread、`asyncio.to_thread`、embedding daemon thread、S3 boto 调用、delivery SMTP worker 和导出队列各自管理生命周期。没有 task registry、admission gate、绝对 deadline、取消协议和“先 drain 后释放主体锁”的统一规则。

### 6.4 权限与数据一致性

文件秘密、SQLite row、云 replica 和归档 keyring 是多个独立真相源；缺少 outbox/lease/CAS/双向 reconciliation。subject_id 也没有在所有可写入口形成复合外键或当前主体条件，导致跨主体污染只能靠事后 integrity 检查发现。

### 6.5 安全基础设施复用

项目已有公共 DNS pinning、bounded response 和 redaction helper，但不同模块仍自行实现 resolver、正则脱敏、输出序列化和路径检查，形成“一个边界修好、另一个边界绕过”的残余风险。

### 6.6 验收方法

验收矩阵以 issue 为中心，证明“某个合同存在”，却没有把重启、双实例、慢流、取消、磁盘满、键轮换和多主体组合起来。API 合同还是单向的，文档/实现/运行时错误状态因此容易漂移。

## 7. 修复顺序与模式建议

### Gate 0：先建立所有权和提交安全（全部 Ultra）

1. **R2-P1-01**：在任何可写 DB 打开、迁移、恢复和构造器 repair 前取得 canonical data-root/DB ownership lock；构造器改为纯读，所有 recovery 延迟到 startup integrity 通过后。
2. **R2-P1-02/R2-P1-04**：引入 runtime epoch + operation lease + integrity quarantine admission gate。每个 await 返回和最终事务都校验 epoch/active/subject；隔离时只开放 health、导出备份和明确的修复接口。
3. **R2-P1-03/R2-P1-10**：建立统一 task registry，停止接纳新请求，等待 HTTP handler、delivery、cloud、embedding、export worker；在 drain 完成前不得释放主体锁。不能取消的外部副作用必须隔离到可终止 worker，并显式标记 unknown。
4. **R2-P1-05**：确定 policy authority。已有主体以 SQLite durable consent 为准；环境值只用于首次 bootstrap，持续托管必须显式开关并禁止 API 假装可撤回。

### Gate 1：安全与幂等（Ultra）

5. **R2-P1-06/R2-P1-07**：统一 private-root/reparse/device 检查和 keyring ACL；为 secret 文件建立 durable intent/outbox 和启动双向扫描。
6. **R2-P1-08/R2-P2-17**：重做 archive keyring revision 为可证明的全局/主体 ledger，允许 catch-up，任何 fingerprint 冲突立即 fail closed；补 key loss/rotation/新主体/跳代测试。
7. **R2-P1-11/R2-P1-12**：把 group + aggregate pool reservation 放进同一 `BEGIN IMMEDIATE`；logical request 在 unknown 时 quarantine，只有 operator reconcile/显式 retry 才生成新物理尝试。
8. **R2-P1-09**：统一 runtime/training/log redaction，至少覆盖 token/session/cookie/private key，并做过度脱敏回归（Max，可与 Gate 1 一起完成）。

### Gate 2：外部资源与存储压力（Ultra/Max）

9. **R2-P2-01/R2-P2-02/R2-P2-03**：所有 HTTP 统一 absolute deadline、bounded reader 和 pinned DNS；S3 设置 botocore connect/read/retry 上限，readiness 以最近验证为准，provider identity 包含 endpoint/region/account。
10. **R2-P2-04/R2-P2-05/R2-P2-19**：archive queue 使用 claim token/lease/rowcount CAS；所有 mutator 和 provenance 采用 subject-scoped composite FK/trigger/API 校验；归档采用 staging manifest + commit/finalize + orphan GC。
11. **R2-P2-06/R2-P2-07/R2-P2-16/R2-P2-20**：先做 free-space emergency preflight，再做低写放大维护；compaction 分批、事务外构建、短事务 CAS；quota 按 subject storage key 分域；maintenance 移出 cognition loop 并可取消（Max，若共用 worker registry则 Ultra）。

### Gate 3：HTTP、部署和可运维性（以 Max 为主）

12. **R2-P2-09/R2-P2-10/R2-P2-14**：health 分为轻量 liveness/readiness/deep diagnostics；所有输出有 byte budget；公开查询改为 SQL 过滤 + keyset cursor；请求头/体使用绝对 deadline。
13. **R2-P2-11/R2-P3-02**：前端引入 request generation/AbortController、轮询退避/Retry-After、下载流式或受控上限，并补齐高风险操作确认。
14. **R2-P2-12/R2-P2-15/R2-P3-01**：拒绝模板占位 token、要求角色互异，补齐全局错误合同和失败熔断/日志上限。
15. **R2-P2-13**：Ubuntu 使用版本化 venv + 原子指针 + 停服/健康检查/回滚；此项跨部署状态，实施建议 Ultra。
16. **R2-P2-18/R2-P3-03/R2-P3-04**：先创建审计 checkpoint 分支/提交，再同步文档；不要在这 140 个 tracked 改动和 89 个 untracked 文件上继续叠加不可回滚修复；兼容导出接口要么纳入同一 publication control，要么明确限制为恢复工具。

## 8. 用户已决定延后的最终发布门禁

以下不是本次新增活动缺陷，继续保持 `implemented/open`，等功能稳定后再做；但最终发布前必须全部完成：

- P2-14：真实生产主机的 BitLocker/LUKS 恢复演练和 key custody 记录；
- P3-03：多日 affect calibration/soak；
- P3-04：完整 LongMemEval S/M、答案级 QA 评测和趋势曲线；
- P3-05：正式签名 MSI/MSIX、clean Windows VM、升级/崩溃回滚；
- P3-08：真实 GitHub tag release、Sigstore 证书、离线 provenance 验证，并在发布 workflow 中补质量门、版本一致性和完整 SBOM/镜像扫描。

这些门禁不能用本地 happy-path 测试替代，也不建议现在提前做以免返工。

## 9. 发布前的关闭判据

每个 P1/P2 必须同时具备：

1. 针对触发条件的回归测试和故障注入；
2. Windows 与 WSL/Linux 的跨平台运行证据（适用时）；
3. 重启、取消、第二 owner、磁盘满和 unknown outcome 的组合测试；
4. 数据/密钥迁移的备份、恢复和 rollback 记录；
5. 更新 `remediation_acceptance.json`、API/OpenAPI、部署文档和 release checklist；
6. 干净分支/提交、可复现依赖和可定位的测试报告。

在 Gate 0 未完成前，不建议继续扩展认知能力或打开非 loopback 监听；否则新增功能会继续叠加到尚未封闭的所有权和异步边界上。
