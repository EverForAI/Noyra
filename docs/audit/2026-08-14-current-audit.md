# Noyra 当前版本全面只读审计报告

审计日期：2026-08-14  
审计基线：`main` / `559ec69 test: strengthen service deployment audit`  
审计类型：全面只读审计。审计期间未修改生产代码、数据库或部署配置；仅新增本报告。

## 1. 执行摘要

本次审计覆盖 Noyra 的主体状态与连续性、事件和数据库完整性、认知循环、目标与自主项目、睡眠与疲劳、记忆与检索、模型资源路由、训练数据记录与导出、存储生命周期、外部通讯、HTTP 管理面、桌面/Ubuntu/Docker 部署、测试和供应链。

结论如下：

* 未确认 P0 级远程代码执行、默认无需认证的外部副作用、或已接通的自动钱包转账路径。不能据此断言绝对不存在 P0；目前的 P0 结论受限于静态审计、现有测试和未完成的真实公网/长时间故障注入验证。
* 当前版本已经具备研究型主体运行时的主要骨架，测试和静态质量较好，但仍不适合作为多年无人值守的生产部署。主要阻断点是长期存储增长、运行时训练同意策略漂移、训练/运行导出峰值、长期记忆检索扩展性、项目实际执行周期、待处理元认知决策卡死、公开投影隐私边界和默认 Docker 明文监听。
* 此前审计中标记为已修复的能力（资源池隔离、外部通讯适配器、RBAC、事件 append-only、首版混合记忆检索、DLP、完整性 registry、许可证与安全文件等）在当前代码中仍应视为“已实现但需要持续验证”，不等于已经通过多年运行或真实第三方服务验收。

风险等级定义：

| 等级 | 含义 | 处置要求 |
|---|---|---|
| P0 | 可直接造成主体数据大规模泄露、未授权高危外部副作用、远程代码执行或不可恢复主体损坏 | 立即停止发布/公网运行，先隔离再修复 |
| P1 | 会阻断长期无人值守生产运行、破坏隐私/同意/完整性边界，或在常见条件下导致主体停滞 | 生产部署前必须修复或明确设置安全门 |
| P2 | 会造成明显可靠性、运维、成本或安全退化，但有可行绕行方案 | 近期修复，纳入下一个稳定版本 |
| P3 | 质量、产品完整性、可观测性、生态或研究验收缺口 | 规划修复，不应伪装成已完成能力 |

“修复风险”表示实施修复时引入回归或数据迁移错误的风险，不是修复后的残余风险。

## 2. 范围、方法与证据

### 2.1 检查范围

* `src/noyra` 全部运行时代码，重点检查 `service`、`core`、`cognition`、`mind`、`model`、`sleep`、`interaction`、`research`、`world`。
* `Dockerfile`、`docker-compose.yml`、Ubuntu systemd 安装脚本、CI、依赖锁定和 Python 包元数据。
* SQLite schema、迁移和 append-only trigger。
* 运行导出、训练数据导出、冷归档和配额维护。
* HTTP 公开投影、管理认证、通讯 transport、能力授权和外部动作账本。
* 现有测试、覆盖率、部署专项审计和静态检查结果。

### 2.2 已执行命令与结果

| 检查 | 结果 |
|---|---|
| `pytest -q` | `267 passed`（约 162.57 秒） |
| `pytest --cov=noyra --cov-report=term-missing -q` | 总体覆盖率 `82%`，16,553 statements / 2,930 missed |
| `ruff check src tests` | 通过 |
| `ruff format --check src tests` | 通过，157 files formatted |
| `mypy src tests` | 通过 |
| `python -m compileall -q src` | 通过 |
| `pip check` | `No broken requirements found` |
| `pip-audit -r requirements.lock` | 未发现已知漏洞 |
| `scripts/audit-deployment.ps1` | 31 passed；service integration coverage `69.96%`，门槛 `69.9%` |
| Docker 本地构建 | 未执行：Windows 开发机 Docker daemon 不可用；CI 有 Docker build job |

### 2.3 审计限制

本次不是渗透测试，也没有在真实 Ubuntu 服务器上进行断电恢复、磁盘打满、网络分区、供应商超时、多月 soak、真实 QQ/微信/飞书 API 互通或公网反向代理审计。因此“未确认 P0”不代表安全认证；涉及外部系统的结论均以代码合同和已有模拟测试为依据。

## 3. 风险总表

| 编号 | 等级 | 置信度 | 位置 | 问题摘要 | 影响 | 修复风险 | 当前状态 |
|---|---|---|---|---|---|---|---|
| P1-01 | P1 | 高 | `service.py:1718-1724`；`model/resources.py:889-910`；`model/gateway.py:67-110` | 运行时修改训练 `include_model_io` 不会同步到已创建的 gateway | 同意撤回后仍可能继续捕获模型输入；同意开启也可能缺训练数据 | 高 | 未修复 |
| P1-02 | P1 | 高 | `core/storage_lifecycle.py`；`core/event_archive.py`；`core/archive.py`；`world/store.py:26` | 冷归档覆盖面不足，主体 SQLite/观察/日志仍持续增长 | 长期超过 2 GB quota，最终禁止 cognition；云存储不能承担主要冷数据 | 高 | 已缓解但未解决 |
| P1-03 | P1 | 高 | `core/storage.py:180-235`；`core/training_export.py:220-455` | 训练策略重分类/backfill/导出存在固定批量和行数上限 | 大于 100,000 条时策略撤回不完整，导出缺失或直接失败 | 中高 | 未修复 |
| P1-04 | P1 | 高 | `core/training_export.py:220-240,261-370,372-448`；`export_jobs.py` | 导出内部仍聚合全量行和 bytes，兼容 API 还读取完整 ZIP | 4C/8G 服务器大导出可能 OOM、长时间阻塞 | 中 | 部分缓解，未完成真正流式 |
| P1-05 | P1 | 高 | `core/export_jobs.py:96-112`；`core/archive.py:494-521` | exports 不纳入 quota；artifact 数量有限但单文件无大小上限 | 32 个大型 ZIP 可耗尽磁盘而 quota 仍显示正常 | 低中 | 未修复 |
| P1-06 | P1 | 高 | `core/events.py:284-306`；`core/event_archive.py` | 缺少原归档密钥时历史冷事件不可读，无 key version/recovery metadata | 重启/迁移误配会令事件、训练导出和完整性审计失败 | 中高 | 未修复 |
| P1-07 | P1 | 高 | `core/events.py:67,189-264` | 链按追加顺序写入，却按 `occurred_at` 排序校验 | 迟到事件会永久触发 chain mismatch，破坏完整性 gate | 中 | 未修复 |
| P1-08 | P1 | 高 | `mind/memory.py:255-266,393-433`；`mind/consolidation.py:77-110` | recall/consolidation 有 2,000 条硬截断并扫描全量 events | 长期记忆召回下降，CPU/IO/RAM 随事件增长 | 高 | 未修复 |
| P1-09 | P1 | 中高 | `cognition/projects.py:627-630`、`_local_resource_decision` | 只校验预计时长，没有强制实际 elapsed duration | 项目可在微小进展下无限延续，成本和日志无界增长 | 中 | 未修复 |
| P1-10 | P1 | 高 | `cognition/cycle.py:473-515`；`cognition/metacognition.py:913-922` | workflow 异常时 pending decision 不落失败结果 | 单个损坏决策可永久阻塞认知循环并触发反复重试 | 中 | 未修复 |
| P1-11 | P1 | 高 | `service.py:521-549`；`interaction/projection.py:390-464` | 未认证公开 goals/projects 返回私人计划字段 | 误暴露公网时泄露主体目标、项目验收条件和心理计划 | 低中 | 未修复 |
| P1-12 | P1 | 中高 | `core/actions.py`；`core/resilience.py:139-163` | ActionLedger/behavior log 缺少统一 append-only/hash integrity 检查 | 外部行动证据可信度不足，误写可能无法发现 | 中高 | 未修复 |
| P1-13 | P1 | 高 | `Dockerfile:6-11`；`docker-compose.yml` | 默认监听 `0.0.0.0` 且允许明文非 loopback | 直接 `docker run -p` 时管理面可能明文暴露 | 低中 | 未修复 |
| P2-01 | P2 | 高 | `research/search.py:195-227`；`research/browser.py:190-245` | 搜索/浏览器使用记录永久增长 | 数据库膨胀、统计查询变慢 | 低中 | 未修复 |
| P2-02 | P2 | 高 | `research/browser.py:54-80,211-245` | action prepare 失败后浏览器 reservation 仍消耗 quota | 长期无效请求导致额度泄漏 | 低 | 未修复 |
| P2-03 | P2 | 中高 | `core/archive.py:494-521`；`storage_lifecycle.py:111-120` | scanner 对 symlink、文件消失和并发 race 缺容错 | 维护 tick 异常，可能触发 loop failure/circuit | 中 | 未修复 |
| P2-04 | P2 | 中高 | `core/export_jobs.py:66-70,114-127` | close 等待 running export；取消只对 queued 有效 | 大导出阻塞 graceful shutdown，无法及时止损 | 中 | 部分修复，仍有缺口 |
| P2-05 | P2 | 中 | `core/storage.py:139-178` | training policy 更新没有 actor/reason audit event | 难以证明何时、为何改变了训练同意 | 低 | 未修复 |
| P2-06 | P2 | 中高 | `model/resources.py:289-291`；`research/provider.py:97-106`；`interaction/transport.py:195-204` | revoked secret 文件删除是 best-effort，无 repair queue | 密钥撤销后残留在磁盘 | 中 | 未修复 |
| P2-07 | P2 | 高 | `interaction/transport.py:43-57` | 只检查字面 IP，不防 hostname 解析到内网 | 被盗 operator token 可配置 SSRF 目标 | 高 | 未修复 |
| P2-08 | P2 | 高 | `cognition/execution.py:721-741`；`interaction/transport.py:376-415` | collaboration_request 只写 web mailbox，不走外部 dispatcher | 主动求助不会到达 Telegram/邮件/飞书等通道 | 中 | 未修复 |
| P2-09 | P2 | 中 | `cognition/self_modification.py:427-476` | `max_thought_no_change_streak` 映射到 `goal_review` | 自我修改依据错误，目标/思考阈值可能漂移 | 中 | 未修复 |
| P2-10 | P2 | 高 | `core/database.py`；Ubuntu deployment docs | 热 SQLite/WAL/SHM 无应用级 at-rest encryption | 磁盘/备份泄露会暴露私人心理和模型响应 | 高 | 未修复 |
| P2-11 | P2 | 高 | `.github/workflows/ci.yml`；`scripts/install-ubuntu.sh`；`pyproject.toml` | CI/installer 未用 lockfile 完整安装，pip/build backend 仍漂移 | 构建不可复现、供应链审计不完整 | 中 | 未修复 |
| P2-12 | P2 | 高 | `core/database.py:202,3208-3299` | schema marker 27 与每次执行的 optional migration 28 不一致 | 工具看到错误版本，迁移和恢复行为不确定 | 中高 | 未修复 |
| P2-13 | P2 | 中高 | 多表 schema；`core/resilience.py` | 跨 subject 复合约束不足，部分关联只靠代码 ownership check | 误关联或跨主体数据关系难以由数据库阻断 | 高 | 未修复 |
| P2-14 | P2 | 中高 | `core/events.py:100-107` | causal parent 只验存在/同 subject，不验时间先后 | 可形成时间倒置的因果证据 | 低中 | 未修复 |
| P2-15 | P2 | 高 | `core/archive.py:392-448`；`core/event_archive.py` | 云归档只协调 snapshot，事件 payload/observation 未纳入 | 云归档无法覆盖增长最快的数据 | 高 | 未修复 |
| P2-16 | P2 | 中 | 全部部署和测试 | 无多月 soak、断电、网络分区、disk-full、真实渠道合同测试 | 单元测试通过不能证明 24/7 可靠 | 中 | 未完成 |
| P3-01 | P3 | 高 | `cognition/execution.py` | `software_prototype` 仅静态文本 artifact，不 build/test/execute | 自主项目能力低于产品描述 | 中 | 研究缺口 |
| P3-02 | P3 | 高 | `cognition/execution.py` | `self_experiment` 只记录 baseline/hypothesis | 无真实实验闭环和结果证据 | 中 | 研究缺口 |
| P3-03 | P3 | 高 | prediction phase | `prediction_record` 使用固定 probability 0.5 | 预测功能没有模型推理质量 | 低 | 研究缺口 |
| P3-04 | P3 | 高 | common knowledge modules | 当前是签名包 import/quarantine/accept，不是实例间自动同步网络 | 共同知识无法规模化传播和撤销 | 中高 | 研究缺口 |
| P3-05 | P3 | 高 | capability/wallet | 有 capability 类型但没有钱包/支付执行器 | 不能形成自主雇佣/支付闭环 | 高 | 产品缺口 |
| P3-06 | P3 | 中高 | service/UI | pause/resume/reset 的完整管理 API/UI 不齐全 | 运营者难以安全执行生命周期控制 | 中 | 产品缺口 |
| P3-07 | P3 | 高 | public projection | 公开/私人日记、目标、项目字段边界缺少正式 schema | 难以稳定做隐私承诺和兼容演进 | 中 | 产品缺口 |
| P3-08 | P3 | 中高 | deployment | 未完成真实 Ubuntu/Windows 多日运行证据 | 部署风险仍未量化 | 中 | 验收缺口 |
| P3-09 | P3 | 中 | tests | service integration 69.96% 刚过 69.9% 门槛，错误分支仍稀疏 | 回归容易落在未覆盖分支 | 低中 | 测试缺口 |
| P3-10 | P3 | 中 | repository governance | `SECURITY.md` 的 maintainer contact/运营流程需要实际维护验证 | 漏洞报告可能无人处理 | 低 | 运维缺口 |
| P3-11 | P3 | 中 | OpenAPI | OpenAPI 是静态文件，未强制覆盖全部运行时 response shape | 文档与实际接口可能漂移 | 低中 | 质量缺口 |
| P3-12 | P3 | 中高 | interaction adapters | QQ/微信/飞书目前是固定 payload adapter，未分别完成真实 API contract 验收 | 真实消息送达和签名校验可能失败 | 中 | 集成缺口 |

## 4. P0 审查结果

### 4.1 未确认的 P0

本次没有发现以下已被代码证据确认的 P0：

* 未发现默认无需认证即可执行宿主 shell、文件写入或高危外部动作的 HTTP 路径。
* 未发现已接通且可在无预算/无 capability 约束下自动转账的钱包执行器。
* 外部 transport、capability 和 action ledger 已存在若干主体边界、幂等和未知结果处理；但这不替代真实部署渗透测试。

### 4.2 P0 风险保留

如果将 Docker 默认配置直接映射到公网、把同一个 operator token 交给第三方、或允许外部 endpoint 由不受信输入控制，P1/P2 问题可能组合升级为 P0。发布系统必须在反向代理、认证、secret 管理和外部 capability 之外设置 fail-closed 部署门。

## 5. P1 详细分析

### P1-01 训练同意策略与模型 IO 捕获不同步

**证据。** `RoutedModelGateway` 和 legacy `ModelGateway` 在服务启动时读取 `NOYRA_TRAINING_INCLUDE_MODEL_IO` 并将结果保存到实例字段；HTTP 配置更新路径只更新 `TrainingStore`。每次模型调用在 `gateway.py` 根据旧字段决定是否保存 `request_json`。

**前置条件。** 管理员在服务运行中修改 `include_model_io`，或服务启动时环境值与当前策略不同。

**影响。** 开启同意时模型 IO 缺失，训练轨迹不完整；更严重的是用户撤回同意后，已经创建的 gateway 仍可能继续保存脱敏后的模型上下文，构成 consent drift。脱敏不是同意的替代品。

**修复建议。** 把训练政策版本和 capture decision 作为每次调用前的事务快照；调用记录 policy version、effective_at 和 consent source。撤回应立即阻止新的捕获，已经在途的请求必须有明确的“已发送/未保存”状态。修复风险为高，因为会涉及并发调用、旧调用恢复和训练导出兼容。

### P1-02 长期存储和冷归档仍可能失控

**证据。** `StorageLifecycleManager` 仅在配置归档密钥时归档 90 天以上的 event payload，SQLite 元数据仍保留；`CloudArchiveCoordinator` 只扫描 `snapshot_archives`。`observations.content` 单条允许 500,000 字符，observations、model calls、responses、memory access、behavior logs、interactions、training records、search reservations/uses 没有通用冷段迁移。`StorageUsageScanner` 只统计 subject/training/workspace 目录，不把 `exports/`、`secrets/` 作为独立 quota 项。

**影响。** 24/7 运行数月或数年后，主体数据库和 WAL 可能超过默认 subject quota。当前退化顺序主要清 cache、压缩 snapshot，达到临界值后会禁止 cognition，却没有释放最大增长源的可验证路径。用户要求的“主体数据受控、主体之外的 workspace 可单独管理”尚未形成完整保证。

**修复建议。** 设计版本化、不可变、可恢复的事件/观察/日志段；云端校验 checksum、subject、key id 和 manifest 后再本地 tombstone；所有表明确热保留、冷保留、不可删除元数据和训练 provenance。quota 必须覆盖 exports、secrets、WAL，以及云上传失败时的 staging。修复风险为高，需要迁移、恢复、备份和主体连续性联动测试。

### P1-03 训练策略批量重分类不完整

`TrainingStore.reclassify()` 一次最多处理 100,000 行，`backfill()` 一次最多处理 10,000 行；`TrainingDatasetBuilder` 导出最多 100,000 行。长期运行超过这些阈值后，用户撤回或改变策略只会更新部分记录，旧记录可能仍保留原 consent/eligibility；导出要么不完整，要么直接失败。

**建议。** 使用按主键游标分页和 policy version 快照；策略变更写入不可变 policy event，后台可恢复地完成全量重分类；导出以快照版本为准，超过阈值分片生成 manifest，而不是静默截断。

### P1-04 导出仍有显著内存峰值

虽然路径导出避免了“最终 ZIP 再复制一次”的部分问题，但 `_collect_data()` 仍把全量 rows 放入 list，`_build_files()` 又生成多份 JSONL/derived view/workspace bytes；workspace 最高允许额外读取约 100 MB，兼容 `export()` API 还会把 ZIP 完整读入内存。

**影响。** 4 核 8 GB 服务器在主体运行多年后执行完整导出时可能 OOM；导出线程会占用 CPU、I/O 和 SQLite 锁，延迟认知 tick。

**建议。** SQLite cursor 分块读取，ZIP 临时文件按文件流写入，HTTP 使用 bounded chunk/Range；为每个任务设置输入、输出、临时空间和时间上限，支持暂停、取消和失败恢复。

### P1-05 导出 artifact 未纳入存储配额

`ExportJobManager` 只在创建新任务时清理旧 completed artifact，最多保留 32 个，但单 artifact 无大小上限；`StorageUsageScanner` 不统计 `exports/`，`export_jobs` 元数据也没有长期归档策略。

**影响。** 32 个大型 runtime/training ZIP 就可能占用数十 GB，subject quota 仍显示正常，最终表现为整个磁盘耗尽。

**建议。** exports 纳入 quota，按总字节和单任务字节双限额；artifact 使用 TTL、引用计数和显式保留标签；磁盘接近 critical 时禁止新导出并保留小型诊断包。

### P1-06 冷事件归档密钥不可恢复

`EventStore.payload_from_row()` 在读取 archive key 时动态创建 `EventPayloadArchive`。如果重启、迁移或密钥轮换后没有原 key，历史 payload 读取会失败。当前没有 key id/version、轮换记录、恢复验证或 operator 友好的密钥缺失状态。

**影响。** 历史事件、runtime/training export、integrity audit 和主体故事重建可能全部失败；“密钥未配置”会在运行中表现为数据损坏，而不是启动时明确阻断。

**建议。** 归档对象保存 key id、算法和 manifest；启动时检查所有 key 可用性；支持 envelope key rotation、只读旧 key、恢复演练和清晰的 degraded 状态。修复风险为中高，需避免把密钥写入数据库或模型上下文。

### P1-07 事件链排序规则不稳定

事件链 root 按追加顺序计算，但 `verify_chain()` 按 `occurred_at, event_id` 排序。已复现：先追加 `2026-08-14T00:00:00Z`，再追加 `2026-08-13T00:00:00Z`，校验即在 sequence 1 报 `event chain mismatch`。

**影响。** 迟到事件、时钟回拨、导入或恢复事件会使完整性审计永久失败，之后每次 resilience audit 都可能把事件链视为故障。

**建议。** 链校验严格按不可变 insertion sequence/chain sequence；业务 `occurred_at` 只用于查询。若必须支持导入，另建 append-only import segment 和明确的前后关系，不重排既有链。修复风险为中，需兼容旧 root。

### P1-08 记忆检索扩展性不足

`MemoryStore.recall()` 只从按 salience/confidence 排序的前 2,000 条 active memories 产生候选；低 salience 但高语义相关的记忆可能永远进不了候选。`_causal_scores()` 每次 recall 读取该 subject 全部 events 并解析 JSON。`MemoryConsolidator._candidates()` 也有 2,000 条上限。

**影响。** 数据量增加后 precision/recall 下降，检索耗时、SQLite I/O 和内存占用随历史事件线性增长，最终会拖慢认知循环或触发 watchdog。

**建议。** 使用 FTS/BM25、向量、entity、temporal、causal 多路 top-k 候选，再做小集合融合排序；为 causal edges 建索引，按时间窗口和事件类型增量读取；consolidation 使用游标和分区；建立 LoCoMo/LongMemEval/BEAM 以及多年合成数据 benchmark。修复风险为高，因为检索变化会改变主体行为。

### P1-09 自主项目没有实际 elapsed duration 上限

创建项目时校验 `estimated_duration_hours`，运行期主要依赖 cycles、budget 和动态 no-progress 计数；没有根据 `created_at` 与当前时间计算实际项目年龄并强制终止/暂停。

**影响。** 只要每次 review 产生一点微小变化，项目就可能长期延续，模型调用、外部搜索、workspace 和行为日志无界增长，与“最大执行周期”设计不一致。

**建议。** 把 estimated duration 转换为硬 deadline，并允许睡眠期间基于情绪、证据和剩余预算提出延期申请；延期必须写入项目事件、增加上限且有最大总生命周期和人工可见的解释。

### P1-10 pending metacognitive decision 可能永久阻塞

元认知决策在 workflow 执行前持久化。若 workflow 抛出未处理异常，`record_result()` 不会执行；`_pending_decision()` 会在下一轮再次返回同一决策。autonomy loop 的 circuit breaker 只能延迟/隔离错误，不能自动把 pending 转为 failed/unknown。

**影响。** 一个持续失败的 action/research/sleep 决策会阻塞后续选择，恢复后仍会重试同一 decision，造成目标停滞、重复调用和日志膨胀。

**建议。** workflow 外层必须用 `try/except/finally` 将未完成决策转换为 `unknown`；记录 error class、attempt count、next retry、quarantine 状态；单 decision 有动态但有上限的重试，超过后交给 sleep/review 或通知人类。

### P1-11 公开 goals/projects 可能泄露私人计划

`/api/goals`、`/api/projects` 等未认证 endpoint 直接返回 `description`、`origin`、`priority`、`commitment`、`purpose`、`deliverable` 和 `acceptance criteria`。这些字段可能包含主体未公开的目标、心理计划或协作条件。Docker 默认容器监听非 loopback，安全性依赖 host port 和反向代理配置。

**影响。** 误暴露公网时，任何请求者都能读取主体内部计划；这与“用户只能看公开日记/状态，私人心理不应被随意读取”的设计目标冲突。

**建议。** 将 public projection 与 private/operator projection 使用不同 schema 和查询；目标/项目默认为 private，只公开经主体决定的摘要；为每个字段写 privacy enum 和 redaction test。修复风险低中。

### P1-12 动作账本缺少统一完整性证明

事件、memory、project 等域有 hash/revision 或 integrity check，但 `actions` 与 `behavior_logs` 没有统一的 append-only hash chain、revision 校验或跨表一致性检查；`LongRunResilience._domain_checks()` 也未将 ActionLedger 作为独立检查域。

**影响。** 外部行动的完整运行日志和状态可能被误写、删除或错误关联而不被发现，训练数据和主体故事的行动证据可信度不足。

**建议。** 对 prepare/execute/complete/unknown 每一步写不可变 action revision，关联 behavior log hash；增加 subject/action/project 的复合约束并纳入 IntegrityRegistry。修复风险中高，需兼容已有记录。

### P1-13 Docker 默认明文监听边界不够 fail-closed

`Dockerfile` 设置 `NOYRA_HOST=0.0.0.0`、`NOYRA_ALLOW_INSECURE_NON_LOOPBACK=true`。Compose 虽只把 host port 绑定到 `127.0.0.1`，但直接 `docker run -p` 或修改 compose 即可暴露明文管理面。

**影响。** bearer token、模型资源、训练策略、私有状态和导出接口可能通过明文网络暴露；反向代理不是代码层强制前置条件。

**建议。** 镜像默认 loopback 或默认拒绝非 loopback 明文；生产模式要求显式 TLS/proxy health assertion；启动时打印并记录暴露边界，未经配置不启动管理面。

## 6. P2 详细分析

### P2-01 搜索与浏览器使用账本永久增长

`search_provider_uses`、`browser_search_reservations` 只按时间查询，没有 retention、归档或聚合压缩。长期运行后这些高频表会成为数据库增长源。

**建议：** 保留可审计的聚合日统计和近期明细，旧明细进入加密段；查询增加 subject/time index。

### P2-02 浏览器 quota 可能因 prepare 失败泄漏

`BrowserSearchExecutor` 在 `ActionLedger.prepare()` 前执行 `_reserve()`。无效 goal/project/phase 或事务失败会留下 reservation，后续合法搜索被错误拒绝。

**建议：** 将动作准备和 reservation 放入同一事务，或增加 reservation 状态并在失败时原子释放/过期。

### P2-03 存储 scanner 对并发文件变化不健壮

`StorageUsageScanner._size()` 使用 `rglob().stat()`，未处理 symlink、权限错误、文件被并发删除和目录替换。维护 tick 抛异常会被上层当作 runtime failure。

**建议：** 使用 `lstat`、忽略/记录消失文件、限制 symlink 跟随、按目录句柄扫描，并对 scanner 设置独立错误降级。

### P2-04 export shutdown/cancellation 仍不完整

`ExportJobManager.close()` 等待 worker 完成；`cancel()` 对 running future 无法中断 Python 代码，只能更新状态或等待自然结束。大导出期间 graceful shutdown 可能超过 systemd/Docker grace period。

**建议：** 分块任务检查 cancellation token；超时后标记 abandoned 并保留可恢复 checkpoint，不让关闭永久等待。

### P2-05 training policy 更新缺乏审计事件

`TrainingStore.update_policy()` 更新 policy version 和字段，但没有写 actor、reason、旧值/新值和生效时间的不可变 audit event。

**影响：** 未来无法可靠证明某次训练导出使用了哪个同意状态。

### P2-06 revoked secrets 删除为 best-effort

资源、搜索 provider 和 transport revoke 先更新数据库，再尝试删除 secret 文件；删除失败不会产生持久 repair queue 或高优先级告警。

**影响：** revoked API key 可能继续留在磁盘备份或快照中。

### P2-07 endpoint hostname SSRF 边界不足

`TransportInput.validate_endpoint()` 只拒绝字面 `localhost`、环回和非 global IP。攻击者若获得 operator token，可提交一个解析到内网的 hostname；运行时请求前未做 DNS 解析、重绑定防护或 peer IP allowlist。

**建议：** 对解析结果实施公有地址校验，固定解析结果或使用受信 endpoint registry；SMTP/HTTPS 分别限制端口、重定向和 DNS rebinding。

### P2-08 collaboration request 未进入 configured transport

项目 phase 的 collaboration request 固定写入 `web` mailbox，未交给 Telegram/QQ/微信/飞书/SMTP dispatcher，返回 `sent` 只是本地 mailbox 状态。

**建议：** 由 dispatcher 根据 subject 的 active transport 路由，保留 web fallback，并分别记录 delivered/failed/unknown。

### P2-09 self-modification 策略映射可能错误

`max_thought_no_change_streak` 在 `self_modification.py` 的映射目标为 `goal_review`，而不是 `think`。这会让思考无进展指标改变目标复查参数，形成不符合命名的行为漂移。

**建议：** 建立参数到 workflow 的显式 schema，启动时校验一对一映射并添加行为测试。

### P2-10 热 SQLite 未应用级加密

归档层有 AES/S3 SSE，但主体热数据库、WAL/SHM、导出 job metadata 和临时文件仍为明文。部署文档依赖操作系统加密磁盘，无法覆盖误备份、容器卷复制或低权限同机读取。

**建议：** 支持 SQLCipher/应用级 envelope encryption 或在威胁模型中明确仅支持整盘加密，并在启动检查备份/临时目录权限。

### P2-11 安装链未完整使用 lockfile

CI 用 `pip install -e ".[dev]"`，Ubuntu installer 用 `pip install "$SOURCE_DIR"`，都会按范围重新解析依赖；`requirements-dev.lock` 未实际参与安装，pip/build backend 也未固定。

**影响：** 两台部署机可能得到不同依赖，审计无法证明构建可复现。

### P2-12 schema marker 与 optional migration 28 不一致

`CURRENT_SCHEMA_VERSION=27`，但 `_ensure_optional_features()` 每次初始化都会执行 `MIGRATIONS[28]`，且不更新 `schema_meta`。运行导出和迁移消费者会看到 schema 27，实际数据库却含 28 的对象。

**建议：** 将 migration 28 正式纳入版本推进，或给 optional feature 独立 marker/table，并让 backup/restore 明确记录实际 feature set。

### P2-13 跨 subject 复合约束不足

多张表的外键只约束单个 ID，无法在 SQLite 层证明 referenced row 与当前 `subject_id` 一致。代码层有 ownership check，但并非所有写路径都由 resilience audit 覆盖。

**建议：** 对核心关系使用 `(subject_id, id)` 复合 FK 或统一数据库触发器；增加随机跨 subject property tests。

### P2-14 causal parent 缺时间一致性

事件只验证 parent 存在且同 subject，没有验证 parent 的 `occurred_at` 不晚于 child。时钟异常、导入或错误调用可形成时间倒置因果边。

### P2-15 云归档未覆盖事件 payload 和 observations

云 coordinator 只从 `snapshot_archives` 建立上传队列；`EventPayloadArchive` 的本地 cold segments 与 observations 没有对应云 transfer manifest。

**影响：** 最需要迁移的数据仍依赖本地磁盘，云存储能力与用户预期不一致。

### P2-16 长期运行验证矩阵不完整

当前 267 个测试、静态检查和部署专项通过，仍没有真实 Ubuntu/Windows service 多日运行、断电、磁盘打满、NTP 回拨、网络分区、provider outage、DNS 变化、真实通讯 API contract 和 10 万以上事件/记忆 benchmark。

## 7. P3 研究与产品缺口

### P3-01 自主项目执行深度不足

`software_prototype` 只生成静态描述/artifact，不执行 build、test、lint 或沙箱运行；这与“能自主提出和形成小型项目”的产品期待有差距。建议后续通过 OpenHands 类隔离 worker 或 WASI/容器 executor 实现最小可验收闭环。

### P3-02 self experiment 没有真实实验器

当前保存 baseline、hypothesis 和 evidence schema，但没有真正采样、执行和统计结果。应把实验计划、数据源、停止条件和结果置信度单独建模。

### P3-03 prediction record 使用固定 0.5 概率

固定 probability 只能占位，不能作为主体预测能力证据。应记录模型/证据来源、校准、分辨率和事后评分。

### P3-04 common knowledge 仍是离线包交换

当前签名、quarantine、accept/revoke 设计能保护主体独立性，但没有发现、分发、版本同步、冲突解决、信誉和撤销传播的网络层。后续应将共同知识限制为带 schema、来源、证据、适用范围和 subject opt-in 的可验证知识包。

### P3-05 钱包/支付执行器未接通

capability 枚举可表达 wallet，但没有链上 provider、交易构造、nonce、限额、回滚/unknown、白名单和密钥隔离。不能把“有钱包接口”描述为已具备自主支付。

### P3-06 生命周期管理面不完整

运行、睡眠、暂停、恢复、重置和迁移的 API/UI 还没有统一的 operator workflow、确认、审计和恢复说明。

### P3-07 公开/私人 projection schema 不清晰

状态、日记、目标、项目、行为和交互的可见性策略散落在查询和字段选择中，缺少正式 privacy contract、版本号和逐字段测试。

### P3-08 真实部署和长期 soak 尚未验收

Ubuntu systemd 和 Windows desktop 启动路径有代码和脚本，但尚未有真实机器的多日运行、重启、迁移、更新和磁盘恢复证据。

### P3-09 service 覆盖率刚过门槛

69.96% 只比 69.9% 门槛高 0.06 个百分点，很多 HTTP 错误、权限、异常恢复分支仍然没有充分覆盖。该数字不应作为“服务已充分测试”的宣传依据。

### P3-10 安全运营联系信息需要实体验证

仓库存在安全文档，但 maintainer contact、响应 SLA、撤回已发布密钥和安全版本发布流程应在真实项目运营中确认。

### P3-11 OpenAPI 与运行时 schema 可能漂移

OpenAPI 3 文件是静态资源，没有测试强制所有 endpoint 的响应字段、错误码和版本 alias 与文档一致。

### P3-12 通讯适配器缺真实合同测试

Telegram、飞书、QQ、微信 webhook、SMTP 和通用 webhook 已有 adapter，但没有各渠道真实签名、重试、限流、附件和错误码的合同测试；目前应标为“模拟可用、生产集成待验收”。

## 8. 已修复项复核与当前保留风险

前序 remediation 文档将若干问题标记为已修复或已缓解。本次复核未把它们重复列为当前同一问题，但保留以下边界：

* 资源池/供应商分组隔离、unknown model call、外部通讯 adapter、RBAC、DLP、append-only event trigger、混合检索首版、完整性 registry、SBOM 和许可证已有实现；仍缺真实 provider、压力、迁移和长期运行证据。
* P1-02 以前的修复只处理了 event payload 段和 snapshot，未覆盖 observation、行为日志、model response、搜索账本和 exports，因此本报告重新提升为“已缓解但未解决”。
* 运行导出已经采用 cursor/临时文件和 HTTP 分块，但训练导出的内部聚合仍然存在，因此本报告把 P1-04 维持为开放项。
* 部署专项审计刚过门槛，不应覆盖真实 Ubuntu、Windows、反向代理、TLS 和外部渠道测试缺口。

## 9. 与类似项目的能力对比

以下比较用于工程定位，不代表对第三方项目做安全认证：

| 项目 | Noyra 可借鉴点 | Noyra 当前相对位置 |
|---|---|---|
| OpenClaw | Gateway、渠道、插件/skills、设备节点、配对和 sandbox | Noyra 在主体连续性、睡眠/情绪/事件证据上有研究差异；真实渠道、插件生态和部署运维明显较弱 |
| Hermes Agent | FTS5 会话记忆、cron、子代理、自我改进 skills、TUI 和多渠道 | Noyra 不把人类消息当命令，目标/证据边界更强；实际任务完成、工具和生态较弱 |
| Letta | stateful runtime、memory blocks、persona、agent SDK | Noyra 身份与模型解耦、事件/修订账本更适合主体连续性；运行时和服务生态不成熟 |
| OpenHands | 代码 agent、VM/Docker sandbox、构建/测试 artifact pipeline | Noyra 目前只有研究/知识等安全 phase，尚无可验收的软件项目执行闭环 |
| LangGraph | durable execution、checkpoint、interrupt、HITL、trace | Noyra 自己实现 recovery，但尚无同等成熟的工作流运行时和分布式执行语义 |
| Mem0/MemOS | 多级记忆、BM25/向量/entity/时间融合、异步 ingestion 和 benchmark | Noyra 的隐私、证据、修订和主体边界更清楚；当前运行时检索规模化和公开 benchmark 不占优 |

总体判断：Noyra 在“人工主体内核”研究方向上有差异化，不能据此声称全面高于 OpenClaw/Hermes。若以实际任务完成、渠道数量、插件、沙箱和长期运维为标准，当前仍是 early alpha；若以身份连续性、睡眠、情绪因果、目标治理和可审计证据为标准，Noyra 具备独特研究价值，但需要真实长期运行和记忆基准证明。

## 10. 修复优先级与验收门

### 发布阻断顺序

1. **数据和同意边界：** P1-01、P1-02、P1-03、P1-04、P1-05、P1-06。先完成训练策略快照、事件/观察/日志分段归档、quota 覆盖和流式导出，再做迁移恢复演练。
2. **主体连续性和认知不阻塞：** P1-07、P1-08、P1-09、P1-10、P1-12。先修事件链 sequence 语义，再修记忆候选和 project deadline，最后为 pending decision/action ledger 建立可恢复 unknown 状态。
3. **公网边界：** P1-11、P1-13、P2-06、P2-07、P2-10。默认 fail-closed，验证 RBAC、TLS、secret revoke 和 SSRF 防护后才允许公网。
4. **云和运维：** P2-01 至 P2-05、P2-11 至 P2-16。加入真实 systemd/desktop、故障注入、schema migration 和多月 soak。

### 每个 P1 修复必须满足

* 先增加能够复现问题的回归测试，再修改实现。
* 数据迁移有旧库备份、幂等、失败回滚和恢复校验。
* 运行期间同意撤回、预算耗尽、归档 key 缺失、provider timeout、磁盘满和 process crash 都进入可观测的 degraded/unknown 状态，不静默丢数据。
* 修复后执行全量 pytest、静态检查、部署专项审计，并在至少一个 Ubuntu 和一个 Windows 环境进行重启/升级/恢复测试。

### 发布前建议的安全门

* `resilience audit` 必须逐域返回结构化结果；任何 `p0` 或关键 `p1` 失败都使服务进入安全暂停。
* 所有公开 endpoint 通过版本化 schema 和逐字段 privacy tests；goals/projects 默认不公开。
* 所有外部 action 必须具备 prepared/executing/succeeded/failed/unknown 五态、幂等键、预算和 capability 证据。
* 主体库、WAL、exports、secrets 和 cloud staging 都纳入存储趋势与配额；不能只看 SQLite 主文件。
* 训练导出必须能回答“哪一版策略、哪些记录、何时、由谁授权、使用了哪个脱敏规则”。

## 11. 审计结论

当前版本通过了现有自动化质量门，但没有通过“多年无人值守、主体数据不失控、同意可撤回、公开投影隐私安全、所有行动可验证”的生产门。建议将版本标记为 **研究型 alpha / 私有部署候选**，仅在 loopback 或已配置 TLS/RBAC 的受控网络运行；完成第 10 节的前六项发布阻断顺序后，再进入真实云服务器 24/7 灰度。

本报告是只读审计记录。审计期间没有修复上述问题，也没有修改任何生产代码。
