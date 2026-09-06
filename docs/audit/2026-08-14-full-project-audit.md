# Noyra 全项目审计报告

审计日期：2026-08-14

审计范围：M1-M40 当前代码、部署文件、数据库 schema、测试、运行导出、训练数据导出、存储归档、模型资源路由、公开 HTTP 服务和长期运行路径。

本报告是审计记录，不代表本次已经修复问题。审计期间没有修改业务代码。

## 1. 审计方法与结论

已执行：

- `253 passed`
- Ruff check 和 format check
- Mypy strict，143 个源文件
- `compileall`
- 全量覆盖率：85%
- `pip check`：通过
- OSV 批量依赖扫描：当前环境发现 `pip 25.0.1` 和开发依赖 `pytest 8.4.2` 存在已知漏洞；运行时直接依赖 `httpx 0.28.1`、`pydantic 2.13.4` 未发现 OSV 命中
- 公开 GitHub 仓库和 README/API 资料对比

结论：

- 未确认存在立即可利用的 P0 远程代码执行或默认外网金融副作用路径。
- 当前版本不应直接作为“长期无人值守生产版”部署。P1 问题主要集中在存储生命周期、模型池预算隔离、训练数据完整性、长期运行熔断、管理面安全和能力/项目功能未接通。
- Noyra 的主体连续性、因果心理、追加式修订和 Supervisor 边界具有明显研究特色；但记忆检索实际效果、外部行动能力、生态连接和生产级运维能力仍低于 OpenClaw、Hermes、OpenHands 及成熟记忆层项目。

风险定义：P0 表示必须立即停止发布；P1 表示阻断生产部署；P2 表示应在近期修复；P3 表示质量、可维护性或产品完善项。修复风险表示实施修复时引入回归的风险，不是修复后的残余风险。

## 2. P1 问题

| 编号 | 问题与位置 | 影响 | 修复风险 | 建议 |
|---|---|---|---|---|
| P1-01 | 训练导出声明支持 model IO，但 `model_calls` 只保存 `request_hash` 和响应，不保存提示词/输入；训练导出只从 `events` 读取可训练事件。位置：`src/noyra/model/ledger.py`、`src/noyra/core/training_export.py`。 | 无法复现完整轨迹，也无法用导出包训练提示词到动作的模型；`include_model_io=true` 不能实现其名称承诺。 | 高 | 增加策略控制的输入/输出记录表，默认只存哈希；显式同意后保存脱敏内容，支持加密、保留期限和流式导出；增加端到端断言。 |
| P1-02 | 长期存储没有真正的冷数据迁移。`StorageLifecycleManager` 只清 cache 和压缩旧 snapshot；事件、model calls、行为日志、训练 provenance、memory access 和 archive metadata 永久留在 SQLite。云归档只是复制，不删除本地数据。位置：`src/noyra/core/storage_lifecycle.py`、`src/noyra/core/archive.py`。 | 数年运行后 SQLite 仍会持续增长，最终超过 subject quota；达到 quota 后 cognition 被暂停，但没有释放主因数据的路径。 | 高 | 设计不可变事件段归档、可验证索引、保留窗口和本地 tombstone；云端确认后才释放本地冷段；导出和恢复必须支持段级校验。 |
| P1-03 | runtime/training export 将全部行、全部文件和最终 ZIP 同时放入内存。位置：`src/noyra/core/runtime_export.py`、`src/noyra/core/training_export.py`。 | 长期数据库或 workspace 较大时，导出会出现数倍内存峰值，4 核 8 GB 云服务器可能 OOM；HTTP 响应还要求一次性生成完整 body。 | 中 | 改为 SQLite 游标 + 分块 JSONL + 临时文件/流式 ZIP，增加最大导出大小、断点续传和失败可恢复任务。 |
| P1-04 | 认知资源组的每日预算被聚合为整个 pool 预算，单个 group 的 gateway 使用 `_aggregate_limits(pool)`；因此日限额为 0 的组也可能使用其它组的额度。位置：`src/noyra/model/resources.py`。 | 供应商组限额和用户预算失真，单个组可能消耗整个池的额度；API key 轮询可能意外突破预期的供应商限制。 | 中 | 明确“组预算”和“池预算”两层语义；每次 attempt 同时检查 pool、group、key 限额；增加并发和跨组预算测试。 |
| P1-05 | 疲劳/资源压力是全局字段。经济池耗尽、深度池仍可用时，模块会把全局 `resource_pressure` 设为 1 并触发整机睡眠；不同模块又用可变的 `gateway.limits` 读取预算。位置：`src/noyra/sleep/fatigue.py`、`src/noyra/cognition/*`。 | 经济池故障会阻断深度认知；某个池的成功调用又可能覆盖另一个池的压力，导致睡眠和预算判断不一致。 | 高 | 将压力拆为 economy/deep/search/browser/embedding 资源维度；工作流只因所需池不可用而降级；所有账本查询显式带 `resource_pool`。 |
| P1-06 | legacy model gateway 的 unknown call 没有重新提交或人工 reconcile 路径。`ModelGateway` 对已有 `unknown` call 直接抛 `ModelCallStateError`。位置：`src/noyra/model/gateway.py`、`src/noyra/model/ledger.py`。 | 网络超时后一个固定幂等键可能永久卡住工作流，长期运行会出现目标停滞。 | 中 | 增加 unknown 的显式重试策略、供应商请求 ID reconcile、人工确认和新的 attempt；所有未知结果必须进入 waiting/circuit breaker。 |
| P1-07 | 长期循环只有固定 error backoff，没有连续失败熔断、任务隔离或 supervisor escalation；`LongRunResilience` 没有接入服务主循环。位置：`src/noyra/autonomy/loop.py`、`src/noyra/service.py`。 | 永久错误会每隔 30 秒重复，持续写 `autonomy_error`，加速存储增长；健康端点仍可能显示 `ok`。 | 中 | 增加按 workflow 的失败计数、指数退避上限、熔断/隔离、人工通知、最后成功 tick 和 backlog 指标；健康检查必须包含 loop liveness。 |
| P1-08 | 能力存储和文件工具存在，但没有能力授权的 HTTP/UI 管理面；当前支持的配置面板只有模型、搜索和训练策略。 | 用户无法通过受支持的部署流程授予 filesystem/project capability，软件 prototype、文件项目和本地协作无法实际运行。 | 中 | 增加独立的 capability 管理 API/UI：范围、到期、side effect、approval、撤销、使用账本和二次确认；默认仍关闭高风险能力。 |
| P1-09 | `software_prototype`、`prediction_record`、`self_experiment`、`collaboration_request` 只在 proposal 层允许；M26 实际只执行 `research_note` 和 `knowledge_collection`。位置：`src/noyra/cognition/execution.py`、`docs/implementation/m26-project-execution.md`。 | README/验收矩阵把 M25-M26 描述为完整，但自主项目不能真正创建软件、预测记录、实验或协作请求。 | 高 | 为每种 phase 定义独立执行器、沙箱、验收证据、资源预算和恢复状态；在实现前将验收矩阵拆成“规划支持”和“已执行”。 |
| P1-10 | 社交认知只把消息写入本地 `interactions` mailbox，`status='sent'` 不代表任何外部服务已送达。位置：`src/noyra/cognition/social.py`、`src/noyra/interaction/store.py`。 | “主动联系”和“请求人类帮助”目前没有 Telegram、Email、论坛或 A2A 等真实传输；主体的外部行动能力被高估。 | 高 | 增加 transport adapter、送达/失败/unknown 状态、幂等和重试账本；把“意图创建”和“外部送达”明确分层。 |
| P1-11 | 管理面只有一个静态 bearer token，配置、通信、训练策略、完整心理日志和导出共用同一权限；服务本身只提供 HTTP，TLS 依赖外部配置。位置：`src/noyra/service.py`、`docs/deployment/ubuntu.md`。 | token 泄露会同时暴露私人心理和模型输出，并允许改模型端点、搜索资源和训练政策；错误地绑定 `0.0.0.0` 会把凭证明文暴露在网络。 | 高 | 强制生产反向代理/TLS 或内置 TLS；拆分 read/operator/export/break-glass token，支持轮换、过期、限速和真实 actor 审计；默认拒绝非 loopback 明文监听。 |
| P1-12 | S3/local archive 没有强制加密；S3 `put` 只写 checksum metadata，没有 SSE-KMS 或客户端加密。位置：`src/noyra/core/archive.py`。 | 快照可能包含私人心理、关系和运行状态；云桶或备份泄露即明文泄露。 | 中 | 支持客户端 envelope encryption，或强制 SSE-KMS、密钥 ID、bucket policy 和恢复前解密校验；密钥不能进入主体数据库或模型上下文。 |
| P1-13 | 事件 payload 只有自校验 hash，没有事件级 append-only trigger；通用 `causal_parent_ids` 不验证父事件存在或属于同一 subject。多张表的 nullable foreign key 也没有复合 subject 约束。位置：`src/noyra/core/events.py`、`src/noyra/core/database.py`、`src/noyra/core/actions.py`。 | 数据库中的 bug 或误用可能制造跨主体证据、错误关联或修改历史 payload；当前完整性审计无法证明事件历史未被重写。 | 高 | 增加 subject-scoped validation、复合 FK/触发器、事件段 Merkle root 和外部锚定；为所有关联 ID 建立 ownership check。 |
| P1-14 | embedding index 和 entity graph 是独立库组件，未接入 `CognitionCycle`；生产 `MemoryStore` 没有 embedding provider，entity/causal retrieval 权重在 `recall` 中没有参与评分。位置：`src/noyra/cognition/cycle.py`、`src/noyra/mind/retrieval.py`、`src/noyra/mind/entities.py`。 | 当前长期记忆主要是 salience/confidence/字符串扫描，不具备验收矩阵宣称的完整混合检索、实体和因果召回；数据越多越容易漏召回。 | 高 | 接入独立 embedding resource pool，建立 FTS5/BM25、向量、entity、temporal、causal 融合；对 LoCoMo、LongMemEval、BEAM 等做回归基准。 |
| P1-15 | memory integration 的 `revert` 会恢复源记忆，但不会将输出 synthesis memory 归档或标为 reverted；它仍可能被召回。位置：`src/noyra/mind/integration.py`。 | 回滚后旧合成结论继续影响后续认知，出现“账本已回滚、召回未回滚”的状态漂移。 | 中 | 回滚必须原子地关闭输出记忆、写反向 provenance，并让检索过滤已撤回整合。 |
| P1-16 | 训练和运行导出、远程模型上下文只有基于字段名的弱 secret redaction；自由文本中的 API key、cookie、PII 或网页泄露内容不一定被识别。 | 私人消息、网页内容和心理上下文可能被远程供应商或导出包带走；`include_*` 开关不能替代 DLP。 | 高 | 采用 secret detector/DLP、来源分级、字段和文本双层脱敏；模型输入捕获必须显式同意并加密；对供应商记录 retention/region/terms。 |
| P1-17 | `LongRunResilience.audit()` 只检查 SQLite、外键、事件 payload hash、snapshot 和 dead letter，不调用 mind/sleep/project/model/resource/interaction 等各自 `verify_integrity()`。 | “P0/P1 release gate”并不是全系统完整性门，部分域损坏可能未被发现。 | 中 | 建立统一 IntegrityRegistry，逐域执行、记录版本和计数，任何一项失败都进入安全暂停；将其接入启动和定期 watchdog。 |
| P1-18 | 没有 `LICENSE`、`SECURITY.md` 或漏洞披露流程；项目同时宣称开源和希望形成生态。 | 默认版权状态会限制再分发和贡献；没有安全报告入口会增加供应链和漏洞处理风险。 | 低 | 选择 MIT/Apache-2.0 等许可证，补充 SECURITY.md、威胁模型、维护者和发布签名。 |

## 3. P2 问题

| 编号 | 问题 | 影响与建议 |
|---|---|---|
| P2-01 | `BrowserSearchExecutor` 使用 `>` 而不是 `>=` 检查 hourly limit。 | 配置为 N 次时允许第 N+1 次；修正并增加边界测试。 |
| P2-02 | 浏览器搜索和部分资源计数采用“先读后写”两步检查。 | 并发调用可超额；统一放入同一事务或原子计数器。 |
| P2-03 | `RoutedModelGateway` 的 group `weight` 只参与排序，不做加权选择或轮询。 | 多供应商分组不能按用户配置的权重分流；实现 deterministic weighted round-robin 或明确删除该字段。 |
| P2-04 | S3 transfer 使用同步 boto3 和 `time.sleep`，会阻塞 asyncio cognition loop。 | 慢云存储会延迟唤醒和健康 tick；移到线程/async worker，并设置总超时。 |
| P2-05 | secret 文件写入使用单次 `os.write`，没有处理 partial write。 | 极端情况下 key 文件可能被截断；使用 `write_all` 循环并校验长度。 |
| P2-06 | `TrainingDatasetBuilder._episodes` 对混合 naive/aware timestamp 会抛 `TypeError`；EventStore 允许调用者传任意时间字符串。 | 导出可能因一条异常事件失败；统一强制带时区的 UTC timestamp。 |
| P2-07 | `ProjectWorkspace.write`、archive restore 和本地 archive 都存在检查到写入之间的 symlink/TOCTOU 窗口。 | 受并发或本地恶意进程影响时可能越界写；使用目录句柄、`O_NOFOLLOW`/原子安全 API 或隔离 worker。 |
| P2-08 | 项目执行器对未预期异常只向上抛，execution 可能永久保持 `executing`。 | 重启后重复或卡住 phase；所有 workflow 需要 unknown/quarantine 转换和人工 reconcile。 |
| P2-09 | `RuntimeLogExporter` 的 offset 无上限，runtime logs 的 UNION/ORDER BY 在大表上会全表排序。 | 管理面请求可能消耗大量 CPU/锁；增加 cursor pagination、最大 offset 和时间范围索引。 |
| P2-10 | `ThreadingHTTPServer` 没有连接/线程上限，slow client 可占用线程。 | 错误暴露到公网时可被低成本 DoS；保持 loopback，或在 reverse proxy 限流并增加 bounded server。 |
| P2-11 | `port=0` 时 desktop launcher 仍用配置端口打开浏览器；IPv6 `::1` URL 也未加方括号。 | 测试/动态端口和 IPv6 桌面启动失败；使用实际 bound address 生成 URL。 |
| P2-12 | 当前 `.venv` 的 pip 25.0.1 和 pytest 8.4.2 命中 OSV 漏洞；pyproject 只定义范围，没有 lock/hash。 | 开发和安装链不可复现；升级到修复版本，生成 lock/SBOM，CI 加 pip-audit/OSV gate。 |
| P2-13 | Docker 基础镜像和安装依赖未按 digest 固定，installer 会无版本升级 pip。 | 构建结果随时间漂移；固定镜像 digest、依赖 hash 和签名发布。 |
| P2-14 | 没有完整 migration matrix、并发、断电、真实 Windows/Ubuntu 服务、网络抖动和大数据导出测试；总体覆盖率 85%，但 project execution 71%、browser 69%、resource pool 77%、training export 76%。 | 单元测试通过不能代表跨进程和多年运行可靠；增加故障注入、property-based、stress 和恢复测试。 |
| P2-15 | UTC 日界线未在配置文档中强调，调用预算和睡眠按 UTC 日期重置。 | 中国用户会在本地 08:00 看到预算重置；提供明确 timezone 或坚持 UTC 并在 UI 显示。 |
| P2-16 | 完整日志、训练包和模型响应缺少异步任务、进度、取消和断点机制。 | 大导出容易超时；加入 job table、状态面板和分块下载。 |

## 4. P3 问题

- 公开 API 缺少 OpenAPI schema、分页 cursor、版本化路径和稳定错误码。
- 状态面板查询次数较多，公开行为/目标/项目视图缺少时间范围和增量更新。
- `/health` 只说明进程能读数据库，不展示 last successful tick、provider health、storage trend 和 pending unknown actions。
- 事件/域 integrity 报告没有统一 JSON schema 和可视化 diff。
- 发布流程没有 changelog、签名 artifact、SBOM、可验证 release provenance。
- UI 仍是开发期 dashboard，缺少 capability、unknown action、storage pressure、provider circuit 和 migration 状态视图。

## 5. 与类似项目的对比

以下为 2026-08-14 的 GitHub 代码/README 快照，星标只作生态规模参考，不代表技术质量。

| 项目 | 主要强项 | Noyra 当前优势 | Noyra 当前短板 |
|---|---|---|---|
| [OpenClaw](https://github.com/openclaw/openclaw) | Gateway、消息渠道、插件/skills、设备节点、pairing、sandbox 和成熟安装生态 | 主体身份、睡眠、情绪因果、追加式账本更明确 | 没有 Telegram/Discord/Slack 等真实渠道、插件生态、设备节点、成熟安全暴露 runbook |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | 自我改进 skills、FTS5 会话记忆、cron、子代理、TUI、Telegram/Discord/Slack/WhatsApp 等渠道、多个执行后端 | 不把人类消息当命令；Supervisor 能约束目标、证据、预算和权限 | 实际任务完成能力、工具面、运行生态、skill 学习闭环明显更弱；Noyra 的心理模型尚未被基准验证 |
| [Letta](https://github.com/letta-ai/letta) / [letta-code](https://github.com/letta-ai/letta-code) | stateful agent、memory blocks、persona、持续学习和本地/云运行 | Noyra 的身份与模型解耦、事件/快照/因果证据更适合研究连续主体 | Noyra 没有 Letta 的成熟 agent SDK、工具集、服务化和 memory 运行时生态 |
| [OpenHands](https://github.com/All-Hands-AI/OpenHands) | 编程 agent、云/VM/Docker 后端、自动化、开发者控制面 | Noyra 更关注主体性而非开发任务 | Noyra 只有研究/知识 phase 可执行，没有代码沙箱、构建、测试和 artifact pipeline |
| [AutoGPT](https://github.com/Significant-Gravitas/AutoGPT) | 可视化 agent builder、scheduled runs、marketplace、成本/运行观测 | Noyra 的目标治理、睡眠和证据边界更严格 | Noyra 缺少成熟工作流编排、可视化构建、用户生态和生产运维 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | durable execution、checkpoint、HITL、stateful workflow、trace/deployment 生态 | Noyra 有针对人工主体的心理/身份语义 | Noyra 自己实现 loop/recovery，缺少成熟 graph runtime、interrupt、trace 和分布式执行 |
| [Mem0](https://github.com/mem0ai/mem0) | 多级记忆、entity linking、时间检索、BM25+semantic 融合和公开 benchmark | Noyra 保留原始记忆、修订、证据和隐私层级，适合主体连续性 | Noyra 当前没有运行时 embedding 接入、entity/temporal ranking，也没有 LoCoMo/LongMemEval/BEAM 成绩 |
| [MemOS](https://github.com/MemTensor/MemOS) | memory OS、异步 ingestion、memory cubes、反馈修正、多模态和 OpenClaw/Hermes 插件 | Noyra 本地 SQLite 和主体边界更简单、可审计 | Noyra 缺少可扩展 memory service、异步 pipeline、跨主体隔离/共享策略和 memory viewer |

总体判断：Noyra 在“人工主体内核”的研究方向上有差异化，但在实际可用智能体能力上仍处于 early alpha。它不是 OpenClaw/Hermes 的超集，也不是当前记忆系统的性能领先者；它的领先点是身份、睡眠、情绪、目标和证据的统一建模，必须通过实际长期运行和记忆基准证明，而不能由第一人称文本或模块数量证明。

## 6. 最值得借鉴的技术

1. 借鉴 LangGraph 的 durable workflow/checkpoint/interrupt 语义，把每个 cognition workflow 变成可恢复状态机。
2. 借鉴 OpenClaw/Hermes 的 provider/channel/plugin contract，但保留 Noyra 的 Supervisor 和 capability boundary。
3. 借鉴 Letta 的 stateful memory blocks 和 agent runtime，补充主体专属 immutable identity 与 revision ledger。
4. 借鉴 Mem0/MemOS 的 FTS/BM25、向量、entity、temporal、feedback retrieval，并用 Noyra 的 evidence IDs、privacy levels 和 causal links 约束写入。
5. 借鉴 OpenHands 的 sandboxed worker，把软件项目放在独立 VM/container/WASI worker，禁止认知模型直接接触宿主 shell。
6. 借鉴成熟开源项目的 release engineering：许可证、SECURITY.md、SBOM、签名 artifact、dependabot/OSV、可复现构建和公开 benchmark。

## 7. 建议的修复顺序

### 发布前阻断项

1. 修复 P1-02/P1-03：事件分段归档、流式导出、配额恢复和导出 job。
2. 修复 P1-04/P1-05/P1-06/P1-07：池级预算、unknown reconcile、资源维度疲劳、熔断和 loop liveness。
3. 修复 P1-11/P1-12/P1-13/P1-16：TLS/RBAC、at-rest encryption、主体边界、完整性根和 DLP。
4. 修复 P1-01：把训练数据从“事件摘要包”升级为可复现、可脱敏、可追溯的 episode/trajectory 数据集。

### 下一版本核心能力

1. 完成 embedding resource pool、FTS5、entity/temporal/causal retrieval 的实际接入并建立基准。
2. 完成 capability 管理面和项目 phase adapter；至少支持安全的软件原型 worker。
3. 完成外部 transport adapter、送达状态和帮助请求通道。
4. 引入统一 workflow state machine、故障注入和多月 soak test。

### 生态与研究质量

1. 发布 MIT/Apache-2.0 许可证、SECURITY.md、威胁模型和贡献指南。
2. 发布 Noyra-specific benchmarks：目标稳定性、重复/空转率、记忆 precision/recall、睡眠前后状态一致性、预算误差、恢复成功率和存储增长曲线。
3. 与 Mem0/MemOS/Letta 做相同数据集对比；与 OpenClaw/Hermes 做工具完成率、消息渠道覆盖和长期运行成本对比。

