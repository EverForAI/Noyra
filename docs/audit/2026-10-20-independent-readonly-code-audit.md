# Noyra 独立只读代码审计报告

审计日期：2026-10-20（Asia/Shanghai）  
审计对象：当前工作区实际内容，包括已有未提交修改及未跟踪的实现文件  
性质：静态代码审查、隔离复现及选定回归检查；本轮不实施修复

## 1. 审计结论

本轮确认 **9 项问题：P0 0 项、P1 1 项、P2 8 项、P3 0 项**。编号在本报告内独立使用，不与历史报告的同号问题对应。风险分级根据实际触发前提、影响和可恢复性判断；没有为了凑齐等级而增加条目。

最优先的问题是：模型连接探测所用的结构化输出 Schema 与真实认知任务使用的 Schema 不同。探测 Schema 可以通过严格接口验证，而真实认知、互动和睡眠反思模型的 Schema 存在不兼容字段约束。对执行严格结构化输出契约的服务，这会形成“连接测试成功、实际远程认知失败”的路径。

另一个需要优先处理的数据恢复问题是：并发备份密钥轮换缺少跨进程序列化。隔离实验中，两次轮换都成功返回，但后一次写入丢失了前一次的新密钥；在覆盖前可正常恢复的真实 Noyra 数据库备份，覆盖后使用最终 keyring 恢复会报密钥不可用。

| 编号 | 问题风险 | 问题 | 修复风险 | 本轮证据 |
| --- | --- | --- | --- | --- |
| IA-P1-01 | P1 | 真实认知输出 Schema 不符合严格结构化输出契约，探测无法发现 | 中 | 真实模型 Schema 扫描、真实 Gateway 调用加受控 HTTP transport、官方 SDK 实现交叉核对 |
| IA-P2-01 | P2 | 推理模型仍无条件使用 max_tokens 等通用参数 | 中 | 当前请求载荷、官方 SDK 参数契约 |
| IA-P2-02 | P2 | 并发轮换覆盖新密钥，使中间代备份失去恢复密钥 | 中 | Linux 上真实 keyring、真实数据库、加密备份及前后恢复对照 |
| IA-P2-03 | P2 | readiness 使用启动时存储快照，运行中失去挂载仍可报健康 | 中 | 实际 HTTP readiness 路由、受控挂载状态变化 |
| IA-P2-04 | P2 | 无权读取备份目录被当作空目录，retention 显示已生成计划 | 低至中 | Linux 非 root 用户、真实权限拒绝 |
| IA-P2-05 | P2 | 睡眠期间进入的事件落在相邻反思窗口之间 | 中 | 真实 Kernel/SleepEngine、受控时钟、连续两个睡眠周期 |
| IA-P2-06 | P2 | 已安装包不包含 Cloudflare setup 依赖的 systemd 模板 | 低 | 实际构建 wheel，并在隔离安装路径执行 setup dry-run |
| IA-P2-07 | P2 | setup 的子进程和健康检查缺少执行期限，可无限等待并延迟回滚 | 低至中 | 子进程参数捕获、写配置到健康检查及回滚调用链审查 |
| IA-P2-08 | P2 | 连续失败达到 1025 次时，指数退避计算溢出并退出主循环 | 低 | 真实 run_forever 边界复现、持久计数与服务调用链 |

以上数量代表本轮达到确认门槛的问题数量，不代表项目剩余问题总量。下面明确列出了覆盖范围、证据强度和未验证内容。

## 2. 独立性、基线与只读约束

### 2.1 基线

- 仓库：[私有验证记录]。
- HEAD：c67526d6b28f995823936687d9c59bb03b612463。
- HEAD 提交主题：docs: record third readonly project audit。
- 审计开始时已有 30 个已跟踪文件处于修改状态，另有未跟踪的实现、测试、报告和旧输出文件。
- 实际审计对象包含这些工作区内容，不能把结论等同于仅对上述 Git 提交的审计。
- 没有以历史审计编号、历史关闭结论、历史测试输出作为本轮确认问题的依据。本轮重新沿实际调用链定位并构造验证。

源代码清单采用 src、scripts、tests、deploy、.github 下的 Python、JavaScript、CSS、HTML、Shell、PowerShell、YAML、TOML 及 systemd 单元相关文件，共 **560 个文件**：src 223、scripts 90、tests 229、deploy 14、.github 4。该数字是本轮纳入指纹的文件清单规模，不表示逐行穷尽审阅了全部 560 个文件。

指纹算法：按仓库相对路径的默认字典序排序，依次加入 UTF-8 路径、一个 NUL 字节及该文件 SHA-256 的二进制值，最后对整体求 SHA-256。

~~~text
5367c74c566b24547b46f96f68ab9032e5aae16280f0adcf06a3f88b74e7f403
~~~

### 2.2 本轮操作边界

本轮只新增本报告；不修改实现、测试、部署配置及已有报告，不提交或推送，不连接用户生产实例执行操作。复现实验使用临时目录、临时数据库、回环 HTTP、受控 transport 或 Linux 临时文件权限。没有真实模型付费调用、真实钱包签名广播、真实磁盘卸载或生产密钥轮换。

临时验证和日志目录：[私有验证记录]。该目录位于仓库之外。本报告保留关键输出，避免把后续复核完全绑定在临时日志的存续上。

### 2.3 分级口径

**问题风险**反映当前代码的风险：

- P0：无需苛刻前提即可造成大范围严重安全、数据或控制权破坏，应立即阻断相关使用。
- P1：受支持的核心使用路径出现重大功能失效，或存在严重安全、数据完整性问题，应优先修复。
- P2：特定受支持配置、权限、运行时变化、并发或长期运行条件下出现功能错误、误报、可用性或局部恢复风险。
- P3：影响较小、具备明确低成本规避方式的维护性或体验问题。

**修复风险**单独判断：

- 低：修改边界局部，可以通过明确输入输出和少量边界测试验证，一般不需要改持久格式。
- 中：涉及多个调用层、并发时序、状态窗口或跨平台行为，需要对兼容性及失败恢复作专项验证。
- 高：需要改变持久数据、协议或信任边界，回滚复杂，必须另行设计迁移和恢复方案。

“修复风险低”并不表示可以跳过验证；“问题风险高”也不自动意味着修复风险高。

## 3. 详细问题

### IA-P1-01：真实认知任务的 Schema 不符合严格结构化输出契约

**问题风险：P1。修复风险：中。确认程度：代码和协议契约确认，受控调用复现；未对真实上游付费调用。**

**源码定位**

- [gateway.py:117](<../../src/noyra/model/gateway.py#L117>)：把输出类型的 model_json_schema() 直接放入请求。
- [openai_compatible.py:492](<../../src/noyra/model/openai_compatible.py#L492>)：把原始 Schema 放进 response_format，同时声明 strict=true。
- [openai_compatible.py:202](<../../src/noyra/model/openai_compatible.py#L202>)：连接探测另用一份简单、合规的固定 Schema。
- [cognition/types.py:36](<../../src/noyra/cognition/types.py#L36>)、[cognition/types.py:54](<../../src/noyra/cognition/types.py#L54>)：认知输出包含具有默认值的字段。
- [sleep/types.py:131](<../../src/noyra/sleep/types.py#L131>)：睡眠反思计划有多个具有默认值的字段。

**根因**

本地 Pydantic 模型允许省略某些有默认值的字段，其 JSON Schema 相应地不会把这些字段列入 required。严格结构化输出的对象契约要求属性完整地列入 required，真正可空的字段通过类型表达空值。当前适配层只设置 strict 标志，没有递归转换对象、数组、联合类型和 $defs。连接探测又没有复用真实任务输出类型，因此探测无法发现这个差异。

独立扫描发现：

| 输出类型 | 顶层未列入 required 的属性 | 其他问题位置 |
| --- | --- | --- |
| CognitionProposal | affect_impulses、claims、goals | AffectImpulse、CognitionGoalCandidate |
| InteractionCognitionProposal | affect_impulses、response | AffectImpulse |
| SleepReflectionPlan | belief_revisions、contradictions、facts、goal_revisions、memories、personality_candidates、prediction_errors、public_diary_candidate、retry_blocks、unresolved_questions | SleepBeliefRevision、SleepMemory、SleepRetryBlock |

**触发条件**

使用执行严格 JSON Schema 子集验证的兼容接口，并发起上述真实输出类型的调用。宽松代理可能接受这些 Schema，不能据此把影响扩大到所有第三方服务。

**影响**

服务可以通过连接探测，实际远程认知、互动生成和睡眠反思调用却被接口以参数错误拒绝。上层即使有重试或本地 fallback，也不能完成原本的远程认知目标；重试相同结构的请求无法消除根因。该问题不等于整个 HTTP 服务无法启动，也不证明原始事件被删除。

**本轮复现**

使用真实 ModelGateway 和 OpenAICompatibleProvider，以 httpx.MockTransport 模拟只检查严格 Schema 契约的服务器。先执行连接探测，再执行 InteractionCognitionProposal 调用：

~~~json
{"connection_probe_ok":true,"actual_call_error":"provider_http_400","requests":[{"schema":"NoyraCapabilityProbe","rejected":false},{"schema":"InteractionCognitionProposal","rejected":true}],"live_api_used":false}
~~~

这里的 400 来自受控 transport，不是对某次真实 OpenAI 或用户 API 请求的抓包。本轮同时核对了官方 SDK 的递归严格 Schema 转换实现，见第 6 节。

**建议修复边界**

在供应商请求适配边界生成 wire Schema，递归处理对象、引用和联合类型，保持本地模型默认值及验证语义。真正 Optional 字段的 nullable 语义需要保留，不应把所有字段都改成可空，也不应把所有本地模型字段直接改为必填来绕过适配问题。转换必须确定、可复现，并核对其与请求摘要、幂等性及日志语义的关系。

**验收要求**

覆盖三类真实生产输出模型及嵌套定义；连接探测增加能代表真实任务约束的测试；同时验证严格接口和允许旧格式的兼容接口；保留本地内容与授权验证。付费上游 smoke test 可在后续验收阶段单独进行，本轮未执行。

### IA-P2-01：推理模型的生成参数没有完整适配

**问题风险：P2。修复风险：中。确认程度：请求内容和官方契约确认；未实测用户中转服务的兼容行为。**

**源码定位**

- [openai_compatible.py:492](<../../src/noyra/model/openai_compatible.py#L492>)：正式请求无条件使用 max_tokens 和 temperature。
- [openai_compatible.py:202](<../../src/noyra/model/openai_compatible.py#L202>)：探测请求也固定使用 max_tokens=8、temperature=0。
- [reasoning.py:59](<../../src/noyra/model/reasoning.py#L59>)：项目把 o1、o3、o4、gpt-5 和 codex 前缀识别为支持思考强度的模型。

**根因**

思考强度选择已经进入请求，但参数适配仍把所有模型当成同一种 Chat Completions 参数契约。模型是否支持 reasoning_effort、输出预算参数名和采样参数支持情况是不同能力，目前没有完整组合处理。

官方 SDK 的参数契约明确把 max_tokens 标为与 o-series 不兼容，并提供 max_completion_tokens。当前对 o3-mini 的实际载荷是：

~~~json
{"model":"o3-mini","max_tokens":1024,"max_completion_tokens":null,"temperature":0.2,"reasoning_effort":"high"}
~~~

**触发条件**

配置属于项目识别范围的 o-series 模型，并使用遵循该参数契约的服务。第三方代理如果主动翻译或忽略旧参数，实际表现可能不同。

**影响**

即使修复 IA-P1-01，推理模型仍可能在参数验证阶段失败，连接探测也可能失败。仅在界面提供思考强度选项，不能保证真实调用可用。

**证据限定**

本项确定性的接口不兼容证据是 o-series 的 max_tokens 参数。temperature 也是无条件发送，属于同一适配缺口，但本轮没有逐模型证明全部温度限制；不据此声称所有 GPT-5 或所有推理模型都拒绝 temperature。8 token 探测预算的充分性也需要后续按模型验证，本轮不单独列为确定缺陷。

**建议修复边界与验收**

统一正式调用与探测的供应商能力契约，明确输出 token 参数、可发送的采样参数和推理参数。不要在所有 400 后无条件删除参数重试，防止掩盖别的配置错误或改变授权请求。验证普通 Chat 模型、o-series 以及兼容代理覆盖规则；同时检查 reasoning token 是否影响现有预算和记账语义。

### IA-P2-02：并发密钥轮换存在更新丢失，可破坏新备份的恢复能力

**问题风险：P2。修复风险：中。确认程度：Linux 真实文件、真实加密备份及恢复对照复现。**

**源码定位**

- [at_rest.py:207](<../../src/noyra/core/at_rest.py#L207>)：rotate 先读取当前 keyring，再生成下一代并写入，缺少跨进程锁或代际比较交换。
- [at_rest.py:265](<../../src/noyra/core/at_rest.py#L265>)：临时文件原子替换保证单次写入完整，但不保证多次读改写的串行性。
- [at_rest.py:712](<../../src/noyra/core/at_rest.py#L712>)：备份创建读取当时的 keyring 并使用其活动密钥。
- [__main__.py:81](<../../src/noyra/__main__.py#L81>)：命令行直接调用轮换，没有额外序列化边界。

**根因及触发条件**

两个获授权的轮换操作同时读取 generation=1，分别生成自己的 generation=2。A 写入后，B 仍按旧快照写入，它的历史密钥列表只保留 generation=1，缺少 A 新生成的密钥。原子 replace 避免了半写文件，却不能阻止这类覆盖。

需要并发管理员操作、重复自动化或类似的重叠执行，普通串行轮换不触发。若在 A 写入与 B 覆盖之间创建备份，就可能留下只有 A 密钥才能恢复的备份。

**本轮复现**

实验仅在 Linux 临时目录中进行：用 SubjectKernel 创建真实数据库并关闭运行时；以屏障安排两次真实 rotate 都先读到同一代；A 成功后创建加密备份并成功恢复；再允许 B 完成覆盖；最后再次恢复同一备份。

~~~json
{"both_rotations_succeeded":true,"first_generation":2,"second_generation":2,"final_generation":2,"artifact_uses_first_key":true,"first_key_retained":false,"restore_before_second_write":"ok","restore_after_second_write":"BackupKeyUnavailableError"}
~~~

调度屏障只控制先后顺序，没有替换密钥生成、写文件、加密或恢复算法。该顺序是当前缺少锁时允许的执行交错。

**影响**

最终 keyring 可以是结构合法的文件，两次命令也都返回成功，但使用 A 密钥的备份不能通过最终 keyring 恢复。如果没有另存 A 的 keyring/密钥，并且内存副本消失，就失去这些备份对应的恢复能力。历史 generation=1 的备份通常仍有密钥；不能说全部备份都会损坏。

**建议修复边界**

用稳定的旁路锁文件或等价跨进程协议，保护“读取最新代—生成新代—持久化”的整个事务。不要锁即将被 replace 的 keyring inode 后就假定其他进程仍受同一锁约束。保留旧密钥、私有权限、原子写与 fsync 行为；明确竞争失败、重试和 generation 的可观察结果。

**验收要求**

至少覆盖两个真实进程同时轮换、三次以上重叠轮换、轮换期间备份、写入失败和进程中断，并确认所有已成功返回创建的备份仍能用最终 keyring 恢复。不能只验证 JSON 文件可解析。

### IA-P2-03：readiness 在运行中沿用过期存储状态

**问题风险：P2。修复风险：中。确认程度：实际 HTTP 路由复现，挂载变化由受控探测模拟。**

**源码定位**

- [service.py:10540](<../../src/noyra/service.py#L10540>)：服务构造时调用 detect_storage_layout。
- [service.py:10602](<../../src/noyra/service.py#L10602>)：将该结果交给 HTTP owner。
- [service.py:2906](<../../src/noyra/service.py#L2906>)：readiness 从这个已有对象读取状态，并生成当前 checked_at。
- [storage_mode.py:50](<../../src/noyra/core/storage_mode.py#L50>)：存储布局是冻结的数据快照。
- [storage_mode.py:71](<../../src/noyra/core/storage_mode.py#L71>)：backup_ready 只结合已有 reason 与路径存在性，不重新核对当前挂载身份。

**根因及触发条件**

启动时的检测结果被当成持续有效的运行状态。服务运行期间，预期备份卷卸载、身份变化或目录退回系统盘时，原来的挂载目录通常仍然存在；exists/is_dir 继续为真，旧的 backup_reason 仍为空。

**本轮复现**

在临时数据目录启动真实 HTTP server，先提供“备份挂载存在”的探测结果，再模拟该挂载消失而保留目录。新调用 detect_storage_layout 已得到 backup_mount_missing，但管理员 readiness 路由仍返回：

~~~json
{"current_detection":{"backup_ready":false,"backup_reason_code":"backup_mount_missing"},"http_status":200,"reported_backup":{"status":"ok","reason_code":"ready"},"reported_status":"ok"}
~~~

返回的 checked_at 是当前时间，使其看起来像刚完成挂载验证。实验没有卸载任何真实卷。

**影响**

管理员和监控系统无法依靠这个状态及时发现备份存储边界已丢失；类似的缓存也不能持续证明运行数据卷仍符合原来身份与写入条件。本项没有证明特权备份/GC 绕过自己的检查，更没有证明错误磁盘上已经发生删除。

**建议修复边界与验收**

把配置契约与近期观测区分开，采用有期限的重新检测或后台探测，确保 checked_at 指向实际观测时间。探测应有执行期限，避免在每个 HTTP handler 中运行无界外部命令；非 root 服务权限不足时明确报告无法验证。覆盖挂载移除、身份改变、权限变化、恢复挂载及并发读取，且不能让健康检查自身创建或修复目录。

### IA-P2-04：不可读备份目录被误认为空目录

**问题风险：P2。修复风险：低至中。确认程度：真实 Linux 非 root 权限复现。**

**源码定位**

- [backup_retention.py:110](<../../src/noyra/core/backup_retention.py#L110>)，特别是第 121 行：通过 Path.glob 枚举备份。
- [retention.py:619](<../../src/noyra/core/retention.py#L619>)：注释和异常处理希望把无权限情况报告为 unavailable。
- [retention.py:649](<../../src/noyra/core/retention.py#L649>)：只要下层没有抛异常，就把结果标为 planned。

**根因及触发条件**

在本轮实际使用的 Python 3.12 环境里，Path.glob 对无法读取的目录没有向这一调用层抛出 PermissionError，而是产生空结果。上层精心准备的异常处理因此不会执行。

当备份目录自身是 root:root 0700、其父目录允许访问元数据，而服务以普通账号运行时，目录可能通过 exists/is_dir 检查，却不能列举。该布局是合理的特权备份权限边界，不能通过放宽目录权限来“修复”显示问题。

**本轮复现**

root 在 /var/tmp 的隔离目录内创建 0700 备份目录并放入一份非敏感备份文件 fixture；随后使用 runuser -u nobody 运行真实 backup_inventory 与 RetentionManager.retention_plan：

~~~json
{"uid":65534,"exists":true,"is_directory":true,"iterdir_error":"PermissionError","backup_inventory":[],"retention_backup_status":"planned","retention_backup_reason":null,"reported_recovery_points":0}
~~~

真实 iterdir 明确拒绝，但业务计划看起来像成功读取了一个空目录。

**影响**

管理面把“没有权限获取信息”与“目录确实没有备份”混在一起，误导恢复点和保留策略判断。当前结果仍标记 apply_allowed=false，本轮没有发现这一复现会直接允许非特权删除，也没有证明备份实际被删。

**建议修复边界与验收**

使用能传播列举失败的明确枚举边界，区分真正空目录、目录缺失、无权读取与读途中失败。如要从特权维护程序暴露清单，需采用已有受保护的证据边界并校验时效性，避免引入越权读取接口。保留 root 私有目录权限；以真实非 root POSIX 用户测试，不能只 mock exists() 或在 root 下测目录 chmod。

### IA-P2-05：相邻睡眠反思窗口不连续

**问题风险：P2。修复风险：中。确认程度：真实状态机与受控时钟复现。**

**源码定位**

- [reflection.py:110](<../../src/noyra/cognition/reflection.py#L110>)：当前反思只读取 lower_bound 之后、当前 run.started_at 及以前的事件。
- [reflection.py:147](<../../src/noyra/cognition/reflection.py#L147>)：互动记录采用相同的时间边界。
- [reflection.py:347](<../../src/noyra/cognition/reflection.py#L347>)：下一轮 lower_bound 取上一轮 completed_at。
- [sleep/engine.py:305](<../../src/noyra/sleep/engine.py#L305>)：completed_at 表示完成唤醒的时间，晚于上一轮 started_at。
- [test_reflection_cognition.py:412](<../../tests/test_reflection_cognition.py#L412>)：已有测试检查晚到事件不进入当前轮，但没有证明其进入下一轮。

**根因及触发条件**

当前轮把反思上界截在睡眠开始时，下一轮却从上一轮睡眠完成后开始读。由此出现 (previous.started_at, previous.completed_at] 的空白区间。只要事件或互动在该区间进入，就不属于两轮中任一轮的反思窗口。

**本轮复现**

使用真实 Kernel 和 SleepEngine，完整执行第一轮开始、反思、深睡、唤醒、完成，再开始第二轮。事件通过正常 EventStore.append 写入，只控制时钟，没有手工篡改数据库状态。

~~~text
first_started_at            = 2026-10-20T06:47:44.805+00:00
late_event_at               = 2026-10-20T06:47:45.805+00:00
first_completed_at          = 2026-10-20T06:47:46.805+00:00
second_started_at           = 2026-10-20T06:47:47.805+00:00
second_window_lower_bound   = 2026-10-20T06:47:46.805+00:00
late_in_first_context       = false
late_in_second_context      = false
~~~

**影响**

这部分经历不会进入这条睡眠反思上下文整合路径，可能影响长期记忆、经验整合与睡眠后认知连续性。原始事件仍保存在数据库里，普通认知路径也可能读取；不能把它写成原始事件物理丢失或整个系统永远不可见。

**建议修复边界与验收**

为反思建立连续且持久的处理水位，区分反思处理上界与睡眠完成时间。优先考虑稳定事件序列或组合游标，处理同毫秒时间戳和时钟回拨。不要在未分析故障恢复语义前只把 completed_at 文本替换为 started_at。

需要验证睡眠期间新事件在后续轮次被纳入、没有非预期重复整合、相同时间戳、反思失败/重试、中途重启及窗口采样上限。采样本身是否需要覆盖全部事件是另一个产品契约，本项只确认时间窗口之间存在空洞。

### IA-P2-06：Cloudflare setup 依赖未进入发布包的模板

**问题风险：P2。修复风险：低。确认程度：真实 wheel 产物与安装路径复现。**

**源码定位**

- [deployment_setup.py:988](<../../src/noyra/deployment_setup.py#L988>)：从已导入模块的 parents[2] 向下寻找 deploy/systemd/cloudflared-noyra.service.example。
- [pyproject.toml:40](<../../pyproject.toml#L40>)：package-data 包含前端资源等，没有这个模板。
- [install-ubuntu.sh:977](<../../scripts/install-ubuntu.sh#L977>)：原生安装器使用非 editable 的 pip 安装。

**根因及触发条件**

代码把开发源码树的相对布局当成安装后也存在的资源布局。模块进入 site-packages 后，parents[2] 已不再指向仓库根目录；wheel 也未包含该模板。

通过正常安装后的 Python 环境运行 Cloudflare setup 即可触发，源码树内测试可能碰巧成功。

**本轮复现**

把当前 pyproject、README 和 src 复制到仓库之外后构建 wheel，避免生成仓库内 build 或 egg-info。将 wheel 解压至隔离安装目录，以 python -I 从该目录导入实际包，执行 Cloudflare setup dry-run；命令 runner 为无副作用实现。

~~~json
{"imported_from_installed_wheel":true,"wheel_contains_template":false,"lookup_template_exists":false,"result_ok":false,"result_stderr":"NOYRA_SETUP_UNIT_TEMPLATE_MISSING: Cloudflare systemd unit template is unavailable"}
~~~

失败发生在实际 DNS 检查或主机配置变更前。构建出的 wheel 本身合法，pip 构建成功不会检查运行期模板是否齐全。

**影响**

用户已经完成安装，却无法使用所提供的 Cloudflare 配置路径；开发环境成功不能代表发布物可用。该问题不证明手工配置的现有 Cloudflare tunnel 会停止运行。

**建议修复边界与验收**

将模板作为包资源随 wheel 发布，用 importlib.resources 等确定性接口读取，或采用受控渲染器。保持生成单元中的权限、凭据文件读取和回环 origin 约束。增加完全脱离源码工作目录的 wheel 安装测试，并检查 sdist/wheel 中所需资源；不要把任意工作目录中的同名模板当作备用可信输入。

### IA-P2-07：部署 setup 缺少子进程执行期限

**问题风险：P2。修复风险：低至中。确认程度：实现边界确认；没有让真实生产安装任务无限挂起。**

**源码定位**

- [deployment_setup.py:189](<../../src/noyra/deployment_setup.py#L189>)：SubprocessRunner 使用 subprocess.run，没有 timeout。
- [deployment_setup.py:730](<../../src/noyra/deployment_setup.py#L730>)：健康检查 curl 没有 connect-timeout 和 max-time。
- [deployment_setup.py:630](<../../src/noyra/deployment_setup.py#L630>)：先发布配置、reload Caddy、restart Noyra，再执行健康检查；只有返回错误或抛出已处理异常才进入回滚。
- [deployment_setup.py:706](<../../src/noyra/deployment_setup.py#L706>)：命令包装层没有把执行超时转换为 SetupError 的分支。

**根因及触发条件**

setup 没有总体 deadline，具体命令和健康请求也没有自身上限。远端 TLS/HTTP 建连后不返回、回环健康端点接受连接后卡住，或其他外部子进程一直不退出，都可能使父进程一直等待。

**本轮证据**

捕获真实 SubprocessRunner 调用 subprocess.run 的参数：

~~~json
{"timeout":null,"argv":["curl","--fail","http[私有验证记录]"]}
~~~

结合实际健康检查 argv 与 try/rollback 调用链，可以确认缺少有限等待边界。本轮没有故意留下无期限运行的子进程，也没有把静态路径证据描述成已发生的生产挂起。

**影响**

配置可能已经发布、服务已经重启，但 setup 不给出完成或失败结果，回滚也迟迟不能执行；操作人员难以判断是否安全重试。不是所有网络故障都会无限等待，快速失败仍可以走既有回滚。

**建议修复边界与验收**

为命令类别设定合理期限，健康检查采用有限连接/响应时间与有上限的就绪重试，并设置整个 setup 的总体期限。将 TimeoutExpired 转成可归因、脱敏的 SetupError，终止并回收子进程；回滚自己的命令也要有期限，避免回滚再次挂住。验证成功、启动稍慢、TCP 接受后无响应、DNS异常、超时后清理以及部分回滚失败。

### IA-P2-08：指数退避在长时间连续失败后溢出

**问题风险：P2。修复风险：低。确认程度：真实循环方法边界复现，计数累积及服务退出路径确认。**

**源码定位**

- [autonomy/loop.py:231](<../../src/noyra/autonomy/loop.py#L231>)：run_forever 的错误分支在 min 之前执行 math.pow(2, failures-1)。
- [autonomy/loop.py:313](<../../src/noyra/autonomy/loop.py#L313>)：失败计数持久化递增，没有饱和上限。
- [autonomy/loop.py:275](<../../src/noyra/autonomy/loop.py#L275>)：熔断等待不重置计数，冷却后失败继续累加。
- [autonomy/types.py:14](<../../src/noyra/autonomy/types.py#L14>)：配置虽然限制最终 backoff，但没有限制求幂输入。
- [service.py:11119](<../../src/noyra/service.py#L11119>)：服务直接等待 run_forever；异常离开循环后进入服务清理。

**根因及触发条件**

外层 min(max_backoff, base * pow(...)) 必须先计算它的参数，不能阻止 math.pow(2,1024) 自身抛出 OverflowError。已持久化的连续失败计数达到 1025，且当前 tick 再次进入错误分支时触发。长期失败、熔断冷却后的持续探测可以累积到这一状态；成功 tick 会清零，所以并非所有长时间运行实例都会触发。实际需要多久取决于冷却与调度配置，本轮不把它表述成固定运行天数。

**本轮复现**

调用真实 AutonomyLoop.run_forever，以最小 kernel/event 替身和受控失败 tick 隔离数学及异常传播边界，分别提供 1024 与 1025 的 health 计数：

~~~json
{"failures":1024,"result":"returned"}
{"failures":1025,"result":"OverflowError"}
~~~

该实验没有等待上千轮真实冷却，也没有修改任何生产持久状态；持久计数可持续增长的前提来自上述真实读写代码。

**影响**

错误处理逻辑产生新的未捕获异常，使自治服务主循环退出。若 supervisor 重启服务，已有失败计数仍可能保留，后续失败可再次进入相同路径；是否持续重启还取决于之后能否产生成功 tick。

**建议修复边界与验收**

在求幂前截断用于计算等待时间的指数，或根据已达到饱和值直接返回 max_backoff。保留真实失败计数和熔断审计，不应靠清零历史或吞掉全部异常解决。覆盖 0、1、阈值前后、1024、1025 及非常大的计数，同时验证最大等待、冷却、恢复成功清零及停机响应。

## 4. 跨模块风险与建议修复顺序

本轮仅给出修复规划，不实施代码修改。

1. **先固定模型适配契约，处理 IA-P1-01 与 IA-P2-01。** 两项可以共用正式请求与探测的能力描述，但应分别验证 Schema 和生成参数，避免修完一项后仍以 400 失败。保持授权、预算预留、请求摘要和未知结果处理不变。
2. **处理备份密钥轮换事务，解决 IA-P2-02。** 在其他备份优化前，先保证新创建备份的恢复密钥不会因合法并发丢失。需要真实跨进程测试。
3. **修复状态可信度，处理 IA-P2-03 与 IA-P2-04。** 区分配置、实时观测、权限不可用和真正空数据，避免管理面用错误状态指导后续迁移或清理。
4. **修复反思水位，处理 IA-P2-05。** 先设计跨周期、重启及失败时的连续性，再修改取数。此项涉及主体经历整合，应独立审查持久语义。
5. **补齐发布物与部署执行边界，处理 IA-P2-06 与 IA-P2-07。** 在构建后的产物中验证，不依赖源码树；超时和回滚需要成对设计。
6. **以独立局部变更修复 IA-P2-08。** 可以作为小改动单独提交，但必须验证真正的饱和边界；不能仅新增一个从未超过几十次失败的测试。

每个模块建议先写明安全边界、允许改变的行为与不应改变的授权/持久约束，再实现、复核、测试并单独提交。是否推送和部署不属于本次只读审计授权。

## 5. 代码覆盖与验证记录

### 5.1 静态审查覆盖

本轮从源码重新检查了以下主要边界和调用链。表中“未新增确认项”只表示在已审阅范围内没有达到本轮证据门槛的额外问题，不构成安全证明。

| 范围 | 审查重点 | 本轮结论 |
| --- | --- | --- |
| 服务生命周期与 HTTP | 构造、启动锁、停机排空、请求边界、管理员认证、cookie/CSRF、健康状态 | 确认 IA-P2-03；其余已审阅路径未新增确认项 |
| 数据库与持久状态 | SQLite 连接/事务、初始化、schema 备份、进程锁 | 与 IA-P2-02、IA-P2-05、IA-P2-08 的状态链共同审查 |
| 模型与 provider | 授权、预算预留、发送前检查、未知结果、结构化输出、能力探测、路由与健康许可 | 确认 IA-P1-01、IA-P2-01 |
| 签名器和钱包 | nonce、签名包络、广播前持久 pending、回执、费用准入与未知结果 | 已审阅路径未新增确认项，未执行真实签名或广播 |
| 迁移及目标激活 | HMAC/nonce、接收配额、切换 fencing、目标桥接与激活 | 选定迁移测试通过；未验证真实双机完整迁移 |
| 加密备份与保留 | keyring、加密/解密、归档提取、manifest、计划与特权 GC | 确认 IA-P2-02、IA-P2-04 |
| 部署与发布物 | Ubuntu 安装、setup、回滚、模板资源、相关 Shell 脚本 | 确认 IA-P2-06、IA-P2-07 |
| 自治、认知和睡眠 | 自治错误计数、熔断、睡眠状态机、反思窗口 | 确认 IA-P2-05、IA-P2-08 |
| 交互与隐私边界 | 私密会话、公开投影、明确禁用的删除入口、恢复约束 | 已审阅路径未新增确认项 |
| 工具和文件系统 | 工具授权、相对路径、no-follow 文件访问、归档/网络/子进程风险点 | 已审阅路径未新增确认项 |
| 前端及 CI | 管理界面配置契约、前端回归、工作流和打包声明 | 前端回归通过；打包资源问题见 IA-P2-06 |

没有把以下内容列为问题：代码明确禁用的隐私删除能力、文档明确声明仅作元数据记录的钱包网络/资产列表，以及尚未证实的共享回调竞争线索。对已排除的线索不追加风险编号。

### 5.2 本轮实际执行的检查

| 检查 | 命令或范围 | 结果 |
| --- | --- | --- |
| Ruff | python -B -m ruff check --no-cache src scripts tests | 通过 |
| Mypy | python -B -m mypy --cache-dir <临时目录>/mypy-cache --no-incremental src | 217 个 source files 通过；没有检查全部 tests 的类型 |
| 前端测试 | tests/frontend 下 3 个 *.test.cjs | 116 passed，0 failed，0 skipped |
| 边界测试组 | authentication_input_contract、storage_mode、signer_service、retention、model_authorization、migration_fencing、private_erasure_disabled | 151 passed，3 skipped，438.84 秒 |
| 扩展领域测试组 | migration_cutover、migration_activation、migration_binding_proofs、private_conversations、m42_p2_06_p2_07_provider_security、reasoning_profile、reflection_cognition、provider_health、wallet_fee_admission | 140 passed，1 skipped，400.18 秒 |
| Shell 语法 | 对清单中的 48 个 *.sh 执行 bash -n，不执行脚本逻辑 | 全部通过 |
| 发布包构建 | 临时源码副本上 pip wheel --no-deps --no-build-isolation | 成功 |
| 专项隔离验证 | Schema、请求参数、readiness、真实权限、反思窗口、wheel setup、退避、密钥轮换 | 结果见各问题及第 7 节 |

Python 选定测试合计 **291 passed、4 skipped**；加上前端为 **407 项通过**。这不是本轮全量测试结果。4 项跳过都与 Windows 无法提供所需 POSIX 普通用户/文件所有权条件有关；本轮另外执行了真实非 root Linux 备份目录权限实验，但不把它冒充所有跳过项的替代验证。

构建产物：noyra-0.1.0-py3-none-any.whl，SHA-256：

~~~text
ef316ebb13e855bd9dcf85cc19078a3ba98067a30594714ddf80035d94b8eef4
~~~

### 5.3 为什么检查通过仍有本轮确认项

- Schema 的常规 fake provider 测试不等同于严格供应商验证；简单探测模型也不能代表真实输出模型。
- 一般目录测试在 root 或 Windows 下无法暴露 Linux 服务账号真实权限语义。
- 单次顺序密钥轮换测试覆盖不到两个读改写事务交错。
- 检查晚到事件不进入当前轮，不等于证明它会进入下一轮。
- 源码路径里能找到资源，不等于 wheel 内可找到资源。
- 低次数失败测试不会触发 1025 次计数边界。
- 启动健康不等于运行中存储身份持续满足约束。

这些是本轮已复现的覆盖缺口，不据此推定每个问题由哪次历史修复引入。

### 5.4 未完成或不在本轮验证范围内

- 没有声称人工逐行穷尽全部文件；本轮采用全项目范围的清单扫描、关键调用链代码审查和定向复现。
- 没有运行完整 Python 测试集，也没有把仓库内旧 full-suite 日志当作本轮通过证据。
- 没有对用户实际 provider、所有模型版本或代理服务执行付费调用，不能直接还原历史服务器某次 400 的具体响应体。
- 没有实际卸载加密卷、重启用户服务器、覆盖生产 keyring 或执行真实双机迁移。
- 没有执行新的长时间 soak、压测或断电恢复；短时隔离实验不能替代这些验证。
- 没有提供全依赖 CVE 或供应链漏洞清单。依赖锁定、语法和类型检查不等于实时漏洞情报审计。
- 没有证明不存在其他 P0/P1/P2 问题，也没有以本报告代替修复后的专项回归和最终验收。

## 6. 外部接口契约核对记录

本轮针对模型接口使用 OpenAI 官方资料作为外部契约来源。开发者文档页面在本环境返回 403，web 工具未提供可用页面正文；没有把这些失败访问表述为成功读到官方指南。

随后从 OpenAI 官方 GitHub 仓库成功读取官方 Python SDK 页面，取得以下固定提交的源码，并保存在临时证据目录。SDK 是契约交叉核对来源，不是本项目当前使用的运行时依赖。

- 官方仓库：openai/openai-python。
- 固定提交：0e5794dc48b8f8d51f6047f8a0b1814eb1e65850。
- src/openai/lib/_pydantic.py：第 33–34 行说明转换目标是 API strict 标准；第 39–47 行处理定义；第 55–60 行把对象属性全部加入 required 并递归转换。
- src/openai/types/chat/completion_create_params.py：第 117–132 行区分 max_completion_tokens 与 max_tokens，并标明后者不兼容 o-series。
- 不把 SDK 参数说明扩展为对用户代理所有行为、所有温度限制或所有 GPT 型号的实时保证。

可复核来源地址（代码块保留固定提交）：

~~~text
http[私有验证记录]
http[私有验证记录]
~~~

## 7. 证据索引与复核入口

以下均位于仓库外的临时证据目录。本报告已保留关键输出；脚本用于后续精确复跑，不能直接用于生产环境。

| 文件 | 内容 |
| --- | --- |
| contract_probes.py / fresh-contract-probes.log | 三类真实 Schema 的 required 缺口、o3-mini 载荷、setup 的 timeout 参数 |
| schema_e2e.py / fresh-schema-e2e.log | 真实 Gateway、简单探测成功与真实输出模型受拒对照 |
| runtime_probes.py / fresh-runtime-probes.log | 两个睡眠周期的窗口，以及实际 readiness HTTP 响应 |
| linux_permissions_probe.py / fresh-linux-permissions.log | Linux root 建目录、nobody 读取业务计划的权限实验 |
| package_probe.py / fresh-package-probe.log | 从真实 wheel 导入包并执行无副作用 setup dry-run |
| supplemental_probes_valid_db.py / fresh-supplemental-probes-valid-db.log | 1024/1025 退避对照、真实 Kernel 数据库上的密钥轮换和恢复对照 |
| fresh-ruff.log / fresh-mypy.log / fresh-shell-syntax.log | 本轮静态检查 |
| fresh-boundaries.log / fresh-broad-domain-tests.log / fresh-frontend.log | 本轮回归检查 |
| fresh-package-build.log / wheels/ | 构建结果与隔离 wheel |
| official-sdk-chat-params.py / official-sdk-strict-schema.py | 从官方 GitHub 页面提取的固定提交源码 |

补充实验的早期版本使用只含自定义表的 SQLite fixture，不满足 Noyra 恢复验证要求，因此其“覆盖前恢复失败”不用于证明本项影响。最终采用真实 SubjectKernel 数据库，已取得覆盖前恢复成功、覆盖后密钥不可用的有效对照，见 valid-db 日志。该区分用于避免把实验前置条件错误混入产品缺陷。

## 8. 只读审计收尾

本轮新增文件为本报告。审计结尾对 871 个已有项目文件（实现、测试、部署、文档、配置及站点文件）逐一复核内容哈希，均与开始基线一致；560 个纳入源码清单的文件没有内容变化，按相同算法重新计算的整体指纹仍为 5367c74c566b24547b46f96f68ab9032e5aae16280f0adcf06a3f88b74e7f403。报告中的 38 个源码定位链接均验证了文件存在及行号有效；git diff --check 通过。未执行修复、提交、推送或部署。
