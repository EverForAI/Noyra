# 2026-10-24 全项目独立只读代码审计：8401984

## 1. 审计对象、结论和操作边界

- 审计日期：2026-10-24，Asia/Shanghai。
- 仓库：`E:\我的项目\Noyra\开发版`。
- 分支：`codex/participant-collaboration`。
- 当前审计提交：`8401984f694a65e174311793bb13f169da8db743`。
- 上一轮修复的比较基线：`560ec7fbca31f9de150cfbaa7aa3dfa1d4fb9f0d`。上一轮包括 `ad76560`、`24aa46b`、`30c43a1`、`9b455aa`、`646e240` 及对应文档提交。
- 开始时工作区干净。本次不修产品代码，不改已有测试、配置和审计结论，不暂存、提交、推送、合并或部署。仓库只新增本报告。
- 复现仅使用临时数据库、合成账户、FakeProvider 和本地调用；没有真实模型费用、消息交付、签名、支付或生产数据操作。Windows 复现只执行安装验证，未启动复制的 EXE。
- GitHub 和服务器状态未读取；本报告不表示某版本已经合并或部署。

本轮确认 **8 个需要处理的问题：P1 两项、P2 六项，未确认 P0 或独立的 P3**。另有一项已复现、但业务语义尚不足以直接判定为缺陷的观察，单列为 O01，不混入已确认问题数。

最紧急的问题是 **合法业务行为能被完整性监控误判为数据库损坏**。上一轮新增的上下文冲突事件会触发该问题；更早的协作申请撤回事件也有同一注册缺口。真实产品路径复现了完整性结果从 `ok` 变为 `corrupt`，以及暂停策略将主体从 `active` 转为 `paused`。

上一轮修复的整体质量评价为 **中**。凭据事务复核、冻结原始路由、未知调用保留和主动社交禁用无授权记忆等方向有明确价值，原问题的主要路径也有回归证据；但新增事件未接入完整性验证、不可用路由没有退出候选、以及现有 CI 测试未同步，说明跨模块验收尚未闭环。

**最终建议：保留上一轮修复，补齐本报告列出的缺口；不建议整轮回滚。当前提交不宜直接作为已通过发布验收的版本。** C01 和 C08 应先解决；Windows 严格签名发布还受 C06、C07 限制。没有执行回滚。

## 2. 方法、覆盖和未覆盖边界

### 2.1 本轮重新建立的清单

本轮重新计算了 1,124 个 Git 跟踪文件的 SHA-256、字节数和物理行数，并对 Python 文件执行 AST 分析。产品 Python 源码共有 **228 个文件、147,366 个物理行、3,997 个函数定义、691 个类定义**；全部 Python 文件 AST 解析错误为 0。统计不包含虚拟环境、缓存、生成的 pyc 和未跟踪运行数据。

| 产品区域 | Python 文件 | 物理行数 | 本轮重点核对内容 |
| --- | ---: | ---: | --- |
| 根服务与部署入口 | 9 | 15,374 | HTTP 认证、角色、请求上限、写入门禁、资源状态、部署/升级入口 |
| autonomy | 4 | 690 | 活跃循环、睡眠冲突、暂停与恢复 |
| capability | 7 | 1,209 | 精确 grant 复核、文件句柄、读写发布边界 |
| cognition | 22 | 20,111 | 交互和社交上下文、未知调用恢复、认知策略分派、上下文长度 |
| core | 59 | 42,325 | Schema、事务、完整性事件注册、暂停策略、导出归属、存储/备份、HTTP 期限 |
| interaction | 24 | 12,792 | 账户、来源归属、会话、撤回、投递入队和重查、公开投影、删除 |
| knowledge | 3 | 2,463 | 同步客户端期限、签名数据入口和异常处理 |
| learning | 4 | 901 | 行为/研究结果的归属和评估入口 |
| migration | 24 | 12,280 | 迁移围栏、payload 库存、传输/激活和恢复边界 |
| mind | 17 | 6,944 | 记忆召回、来源、向量索引、关系与情感的收件人范围 |
| model | 18 | 10,254 | 网关、资源路由、调用/尝试账本、未知重试、配置和 embedding |
| research | 6 | 2,115 | 搜索准入、DNS/响应边界、结果恢复和隐私化记录 |
| sleep | 6 | 1,999 | 状态推进、反思边界和未完成操作围栏 |
| wallet | 16 | 14,586 | 悬赏提交/撤回、付款状态、预留、执行和 signer/RPC 边界 |
| world | 9 | 3,323 | 来源抓取、公开 URL、观察和状态推进 |

此外检查了运行时前端、CI/发布工作流、Windows builder/lifecycle、Ubuntu 安装与恢复脚本、容器和服务单元的关键契约。没有只看上一轮 diff。

人工深审采用调用链、SQL 和状态机选段阅读，辅以全仓结构扫描、类型检查和跨模块测试。**本轮没有人工逐行读完上述全部 147,366 行，不能把全项目范围审计解读成每一行均已证明正确。** 自主项目全部策略分支、云厂商实机、全部 Linux/root 文件系统竞争、全部迁移故障组合仍未穷尽。后端全套 3,171 项没有完整跑完；实际执行结果见第 6 节。

### 2.2 独立性与证据原则

- 问题是否存在，以当前真实代码路径和隔离复现为准；历史报告和聊天没有作为“已正确”的证明。
- 归因使用 `git diff`、`git blame`，必要时把 `560ec7f` 的社交模块载入隔离进程作行为对照。
- 部分合成数据构造复用了现有测试 fixture；断言、故障触发和证据采集由本轮独立脚本完成，产品数据库/网关/路由/完整性逻辑未被替换成预计算结果。
- 隐私复现使用明显的合成标记，并检查实际模型请求和本地 outgoing 记录；不会把“提示词包含标记”夸张成已经发生真实外发。
- 未能确认业务契约的事项单列观察，不为了增加问题数量而下确定结论。

## 3. 分级和问题总表

### 3.1 问题风险与修复风险

- **P0**：已经确认可造成严重不可逆损失或全面权限失守，需要立即处置。
- **P1**：核心运行/信任边界存在显著错误，应阻断受影响路径的发布。
- **P2**：特定条件下存在实质性的隐私、状态、可靠性、兼容或验收错误，需要修复。
- **P3**：局部解释或体验问题，通常不单独阻断核心服务。

修复风险另行分级：**低**为局部契约或测试更正；**中**涉及跨模块来源、调度、异常/历史状态；**高**涉及未知结果处置、历史共享授权、包信任或难以回退的恢复语义。问题严重程度与修复难度互不替代。

“原生/存量”在本报告中表示**相对于上一轮开始时的 560ec7f 已经存在**，不意味着一定来自最初版本；若它由更早一轮修复引入，会另行注明。

| 编号 | 已确认问题 | 风险 | 修复风险 | 归因 |
| --- | --- | --- | --- | --- |
| C01 | 新增/存量业务审计事件没有接入完整性验证，合法行为被报损坏并可暂停主体 | P1 | 中 | 混合：上下文冲突分支是 `646e240` 新次生问题；撤回/绑定分支为基线已有问题，其中撤回事件来自更早修复 `28ca58d` |
| C02 | 没有新身份归属的旧交互仍可把他人私密全局记忆带入回应 | P2 | 中至高 | 原生/存量隐私缺口；上一轮主动社交修复未覆盖直接交互的旧记录分支 |
| C03 | 主动社交仍把其他联系人的情感状态放入当前收件人的生成上下文 | P2 | 中 | 原生/存量来源隔离缺口，未因禁用记忆召回而消除 |
| C04 | 已不可用的社交路由仍占据第一候选，其他有效联系人得不到选择机会 | P2 | 中 | `24aa46b` 的新增路由复核与旧候选条件之间形成的次生退化 |
| C05 | 主动社交授权重试遇到上下文变化时，连续抛出未处理的 ModelCallConflictError | P2 | 中至高 | 原生/存量恢复缺口；上一轮只为交互模块补齐此处理 |
| C06 | Windows 严格签名安装使用单个受信指纹时必然发生 Count 属性异常 | P2 | 低至中 | 基线已有；由更早的 `b2efa08` 发布者加固引入，非上一轮新引入 |
| C07 | Windows 严格模式仍没有认证完整包清单，保留可信 launcher 即可更换其他包文件并重算清单 | P1 | 高 | 原生遗留的完整包信任缺口，更早修复只闭合了 launcher 发布者约束 |
| C08 | 当前分支两项后端测试确定失败，CI 验收不能视为通过 | P2 | 低 | 混合：入站调度测试未随 `30c43a1` 同步；导出库存断言遗漏则来自更早 schema 104 集成 |

C07 的 P1 仅针对 Windows 分发包来源认证；不表示 Linux 已遭入侵，也不表示已经证明匿名 HTTP 可执行代码。C08 是验收阻断，不应伪装成已经发现付款或数据泄露。

## 4. 逐项发现

### C01：合法审计事件会被完整性监控当作损坏

**问题风险：P1。修复风险：中。置信度：高。**

**代码证据**

- [上下文冲突事件写入](<E:/我的项目/Noyra/开发版/src/noyra/model/ledger.py:742>)：`quarantine_prepared_context()` 写入 `model_request_context_conflict`。
- [交互恢复触发点](<E:/我的项目/Noyra/开发版/src/noyra/cognition/interaction.py:132>)：捕获请求指纹冲突后调用上述方法。
- [申请撤回事件写入](<E:/我的项目/Noyra/开发版/src/noyra/interaction/collaboration.py:572>)：写入 `participant_application_withdrawn`。
- [渠道来源绑定事件写入](<E:/我的项目/Noyra/开发版/src/noyra/interaction/participant_http.py:106>)：写入 `participant_source_bound`。
- [完整性事件集合](<E:/我的项目/Noyra/开发版/src/noyra/core/integrity.py:1155>)、[未知事件拒绝分支](<E:/我的项目/Noyra/开发版/src/noyra/core/integrity.py:3119>)：上述事件不在 core、management、wallet 的已验证契约中，最终抛 `IntegrityError`。
- [暂停策略](<E:/我的项目/Noyra/开发版/src/noyra/core/integrity.py:4923>)：存在 finding 且策略为 `pause` 时，调用 `kernel.safe_pause()`。

**根因**：业务写入者与全局审计验证器分别维护事件契约。新增事件没有同步注册验证、引用归属和合法 payload，单模块的“写入成功/队列隔离成功”就会在深度完整性检查时变成整体故障。

**触发条件**

1. 某交互调用结果未知，管理员授权重试；期间情感等上下文变化。新补强逻辑隔离 prepared 请求并写入冲突事件。
2. 用户通过正常接口撤回自己的协作申请。
3. 管理员正常绑定渠道身份，写入绑定事件。该分支按同一静态契约确认；本轮动态复现覆盖前两类。

随后执行 `core.actions` 的 manual/periodic deep 完整性检查即可触发。轻量启动检查不一定马上遇到该事件，因此不能以“还能启动”排除问题。

**影响**：账本可以保持完好，但监控报告 `corrupt` / `core.actions:integrity_error`。在 `pause` 模式下主体被暂停；在 `alert` 模式下仍产生错误的严重告警。事件持久保留，重启或关闭原 prepared 调用不会自动消除未注册的事件，正常恢复也可能持续受阻。

**独立复现**

```json
{
  "before_integrity": "ok",
  "retry_result": "interaction_model_context_changed",
  "after_integrity": "corrupt",
  "findings": ["core.actions:integrity_error"],
  "policy_result": "paused",
  "lifecycle_before": "active",
  "lifecycle_after": "paused",
  "provider_requests": 1
}
```

另一个独立库：正常创建协作机会/申请后，执行真实撤回逻辑，`core.actions` 从 `ok` 变成 `corrupt`。没有伪造或手插异常审计行。

**归因**：冲突事件全部写入代码来自 `646e240`，属于上一轮新增次生问题。撤回事件在 `560ec7f` 已存在，来自更早修复 `28ca58d`；绑定事件也是基线已有缺口。本项归为同一个“写入与验证契约脱节”根因，避免重复计数。

**根因修复方向与安全边界**：为每种合法事件增加明确的 action、actor、payload、同主体引用和 purpose 验证；不能仅把所有未知事件放行。校验必须兼容事件写入后调用继续转成终态的合法历史。保留既有未知尝试、预算和审计，不通过删记录、关闭完整性监控或把策略改为 alert 绕过故障。

**修复回归要求**：三个真实写入入口之后运行完整 `core.actions` 和模型验证；非法 actor/payload、跨主体 call、错 purpose 仍失败；prepared 关闭后再检查仍通过；pause 策略不误停正常实例，真实损坏仍可暂停。

### C02：旧交互仍能召回他人的私密记忆

**问题风险：P2。修复风险：中至高。置信度：高。**

**代码证据**

- [旧交互全局召回分支](<E:/我的项目/Noyra/开发版/src/noyra/cognition/interaction.py:297>)：只有 `who is not None` 时不召回；`who is None` 时按消息文本召回全局记忆。
- [记忆候选读取](<E:/我的项目/Noyra/开发版/src/noyra/mind/memory.py:305>)：按 subject、active、类型和候选 ID 读取，没有面向当前联系人的分享授权条件。
- [回应提交](<E:/我的项目/Noyra/开发版/src/noyra/cognition/interaction.py:151>)：将模型回应写回原 channel/counterparty。

**根因**：把“没有新联系人归属记录”解释成“可以使用全局记忆”。这是来源信息不足，不能证明所有全局记忆都可向这位旧联系人披露。长度截断解决了检索异常，但没有给旧记录补足收件人授权。

**触发条件**：升级前或通过受信旧入口存入、尚待处理的外部交互没有 `participant_message_links`；库内有与该消息检索词相关的其他联系人私密记忆。无需修改数据库、关闭约束或绕过新账户隔离。

**影响**：其他人的私密记忆进入为旧联系人生成回应的模型上下文，可能被复述回该联系人的消息中。影响有条件，不表示所有新注册账户都能直接读取全局记忆。

**复现**：通过真实账户身份写入 Bob 私信，正常创建以该私密事件为来源的 `privacy_level='private'` episodic memory；随后创建合法无新身份关联的旧 webhook 邀请。真实 `InteractionCognition.run_due()` 配合本地合成响应器，把提示词中存在的合成标记回写为回应：

```json
{
  "legacy_recipient_has_identity": false,
  "result": "interaction_accepted",
  "private_memory_in_model_context": true,
  "synthetic_secret_in_reply_to_alice": true
}
```

最后一个字段检查的是实际本地 outgoing 记录。没有启动外部发送器。

**归因**：该条件在 `560ec7f` 已存在，`f6dceae` 处理长消息和“先召回再清空”时保留了此兼容分支；上一轮没有新增它。上一轮 `9b455aa` 修复了主动社交的记忆召回，不能据此推断直接回应的所有旧记录也已隔离。

**修复方向与边界**：旧身份缺失时同样先确定允许的来源集合。若没有明确逐收件人授权，跳过全局召回；合法且已经授权的原会话历史可以保留。不要按昵称、counterparty 文本或 entity 前缀猜测历史归属，也不要删除旧记忆以消除复现。

**回归要求**：有归属/无归属、升级前已排队消息、混合来源/私密/无归属记忆、明确会话授权、跨联系人查询；在候选检索之前隔离，避免记录虚假的使用或付费 embedding。确认旧消息仍能获得无泄露的正常回应。

### C03：主动社交中的其他联系人情感状态仍未隔离

**问题风险：P2。修复风险：中。置信度：高。**

**代码证据**

- [全局情感查询](<E:/我的项目/Noyra/开发版/src/noyra/cognition/social.py:192>)：按 subject 和 target_type 读取 `human/agent/entity/subject`，未限定为当前联系人。
- [当前收件人的提示词 payload](<E:/我的项目/Noyra/开发版/src/noyra/cognition/social.py:288>)：把全部所选情感行放入 `affect`。同一 payload 还包含全局 active goal 的标题；本轮动态复现确认情感行，不据此声称每个目标标题都属于秘密。
- [更严格的直接回应情感筛选](<E:/我的项目/Noyra/开发版/src/noyra/cognition/interaction.py:278>)：已经有按当前主体/联系人/交互目标收缩的实现，可作为语义对照。

**根因**：禁用了未经授权的 memory recall，但没有统一定义“面向一个收件人的生成上下文”。全局情感快照仍被当作普通内部控制信息，包含其他联系人的 target_id、emotion_type、强度和效价。

**触发条件**：Alice 是当前主动社交对象，Bob 等其他对象的情感状态进入全局强度前 12 项。当前 Alice 无须有读取 Bob 私人状态的授权。

**影响**：私人的关系/情感信息进入对 Alice 生成内容的提示词。提示词中的保密文字不能替代来源过滤，输出 schema 也不检查这些信息是否可披露。不是直接公开一个查询接口，但存在可复述到外发草稿的通路。

**复现**：使用真实 `MindEngine.process_event()` 创建另一联系人的合成情感，再运行真实主动社交，合成模型响应器仅回显自己实际收到的标记：

```json
{
  "result": "relationship_social_contact",
  "unrelated_affect_in_prompt": true,
  "unrelated_affect_can_reach_reply": true
}
```

**归因**：查询和 payload 来自存量代码；上一轮删除全局记忆召回没有引入该情感查询，也没有覆盖它，因此属于原生/存量缺口。

**修复方向与边界**：先构造当前联系人允许的上下文，再加入情感和可分享的目标信息。可以保留经过约束的主体总体决策压力，避免携带其他联系人身份和私人细节。不要为了隐私修复清空整个心理状态、禁用关系模块，或仅要求模型自行保密。

**回归要求**：其他人/当前人/主体自身情感，human/agent/noyra 类型，强度排序变化，多联系人和旧联系人；检查实际 prompt 和 outgoing 草稿。对 goal title、event metadata 等同类字段做同一策略的来源审查，避免修完 memory 又留下另一个输入通道。

### C04：不可用社交路由仍占据第一候选

**问题风险：P2。修复风险：中。置信度：高。**

**代码证据**

- [先记录历史 channel、再尝试获取路由](<E:/我的项目/Noyra/开发版/src/noyra/cognition/social.py:233>)：路由验证失败只得到 `route=None`，channel 仍保留。
- [候选准入与首项选择](<E:/我的项目/Noyra/开发版/src/noyra/cognition/social.py:260>)：检查的是 `channels`，然后固定取 `candidates[0]`，没有把已知不能发送的路由从发送候选中移出。
- [提交时正确失败关闭](<E:/我的项目/Noyra/开发版/src/noyra/cognition/social.py:480>)：没有冻结 route 或路由已失效时拒绝提交。
- [完成时间决定再次评审](<E:/我的项目/Noyra/开发版/src/noyra/cognition/social.py:163>)：没有 social run 被提交时，仍视为到期。

**根因**：上一轮把“冻结且重新验证路由”接到了提交边界，但调度候选仍沿用“过去有这个渠道”这一旧标准。发送权限与可运行性没有形成一致的选择条件。

**触发条件**：排序第一、熟悉度较高的联系人所对应 binding/transport/entity 已停用或撤销；后面仍有有效联系人；模型为第一位生成 contact/request_help。

**影响**：不再错误发送是正确的，但无效第一候选反复产生 `relationship_social_rejected`，其他联系人不能获选。合成复现中第二轮复用缓存，因此不是每个 tick 都新增付费调用；后续时段仍可能消耗新的社交额度。影响主动社交流程，不应扩大为整个认知系统都已死锁。

**复现**：建立 Alice 和 Bob 两条合法来源；提高 Alice 熟悉度并停用其 binding，确认 Bob 的真实 `_route()` 仍可成功。连续运行两次社交：

```json
{
  "selected_disabled_contact": true,
  "selected_route_missing": true,
  "other_contact_route_valid": true,
  "results": ["relationship_social_rejected", "relationship_social_rejected"],
  "model_requests": 1,
  "completed_social_runs": false,
  "still_due": true
}
```

**归因**：`24aa46b` 新增 `_route()` 和提交前严格复核，但没有更新候选准入；是上一轮修复后出现的可运行性退化。不能靠撤掉来源复核来恢复发送。

**修复方向与边界**：明确区分可联系的候选与只能等待/尊重距离的状态，发送候选必须有已验证冻结路由；不可运行首项应让其他独立联系人获得机会。非发送决定可不依赖发送路由，但不应被技术性故障伪装成 Noyra 的自主拒绝。继续坚持原房间、原 transport/binding，绝不能自动换 bot/会话。

**回归要求**：停用发生在模型调用前/期间/提交前；恢复、撤销、删会话；首项失效但后项有效；wait/respect_distance；已缓存成功响应和服务重启。验证不多发、不改投、不制造虚假拒绝。

### C05：社交 prepared 重试遇到上下文变化会持续抛异常

**问题风险：P2。修复风险：中至高。置信度：高。**

**代码证据**

- [社交异常处理](<E:/我的项目/Noyra/开发版/src/noyra/cognition/social.py:130>)：只捕获预算、provider、structured output 和 `ModelCallStateError`，遗漏独立的 `ModelCallConflictError`。
- [异常类型关系](<E:/我的项目/Noyra/开发版/src/noyra/model/errors.py:33>)：Conflict 与 State 是并列子类，捕获 State 不会捕获 Conflict。
- [持久请求指纹比较](<E:/我的项目/Noyra/开发版/src/noyra/model/ledger.py:49>)：同一幂等身份对应不同 request_hash 时拒绝。
- [策略结果记录](<E:/我的项目/Noyra/开发版/src/noyra/cognition/cycle.py:576>)：社交调用异常时到不了后续的策略结果记录。

**根因**：prepared 调用要求延续原始请求，而新一轮社交会用当前情感、目标、事件等重新构造输入。路由相同不代表请求指纹相同；异常及恢复状态没有被该模块完整建模。

**触发条件**：社交调用结果未知，管理员授权一次重试；收件路线保持相同，但其间心理状态或上下文变化。

**影响**：没有重复 provider 调用，但该社交恢复持续抛异常，prepared 状态保留，运行周期可能进入错误退避，策略结果不能按正常路径完成。这是恢复可用性问题，不是应通过自动换幂等键绕过的理由。

**复现**：裸网关和真实资源路由各自执行 unknown → 正常写入新的主体情感 → 管理员授权 retry → 连续运行两次社交：

```json
{
  "repeated_results": ["ModelCallConflictError", "ModelCallConflictError"],
  "call_status": "prepared",
  "model_requests": 1
}
```

**归因**：该社交异常分支在 `560ec7f` 已存在。上一轮 `646e240` 为直接交互添加了冲突隔离，未覆盖主动社交；不把“此次还没修到”误记为“此次新增”。

**修复方向与边界**：明确冻结请求/隔离冲突/人工关闭的恢复流程，令冲突有稳定可审计的终止或等待状态，不重复占用策略执行。新增审计必须同步解决 C01。保留未知证据和费用；不把 prepared 直接改为成功，不自动重算新 key 或释放原预算。

**回归要求**：裸/路由两种网关、输入未变/已变、重启、显式关闭、独立联系人继续、预算和调用快照保持；所有恢复事件通过完整性验证。

### C06：单个可信发布者指纹使 Windows 严格安装失败

**问题风险：P2。修复风险：低至中。置信度：高。**

**代码证据**

- [Normalize-Thumbprints 的返回值](<E:/我的项目/Noyra/开发版/deploy/windows/noyra-lifecycle.ps1:46>)：函数输出经 PowerShell pipeline 枚举，0 项/1 项不保证调用端获得数组。
- [调用端及 Count 检查](<E:/我的项目/Noyra/开发版/deploy/windows/noyra-lifecycle.ps1:133>)：直接赋给 `$manifestThumbprints`、`$externalThumbprints`，严格模式下读取 `.Count`。

**根因**：函数内部用 `@(...)` 并不保证跨函数 pipeline 输出后仍保留数组形状。调用端没显式收集数组；单个字符串在当前 StrictMode 下没有可用的 Count 属性。

**触发条件**：按严格签名契约传入一个合法的 `TrustedPublisherThumbprints`。这是正常配置，不需要恶意包、错误证书或缺少 PowerShell 模块。

**影响**：正确的签名包也在复制前失败，用户无法按最常用的单发布者策略安装。包和数据未被破坏，不能将其描述为签名绕过。

**复现**：使用系统 `whoami.exe` 作为合成的已签名 launcher，外部允许其实际证书指纹，构造闭合清单；只执行 install：

```text
System.Management.Automation.PropertyNotFoundException
The property 'Count' cannot be found on this object.
at Verify-Package ... noyra-lifecycle.ps1:138
```

同一个可信 launcher、相同签名验证条件，使用两个合成允许项时可以进入并完成验证，排除了缺失 Authenticode、证书无效或 launcher 不在库存的解释。没有启动该程序。

**归因**：代码来自更早的 `b2efa08`，在本轮比较基线已存在；是更早修复的次生问题，非上一轮新引入。

**修复方向与边界**：在调用边界保持 0/1/多项的数组契约，去重/规范化后再比较。不能通过增加虚构可信证书或取消严格模式来规避。不要影响真正缺少外部信任策略时的明确拒绝。

**回归要求**：0、1、2、重复、空白/小写指纹；正确和错误签名；无信任策略、manifest 与外部策略不一致；install、start、upgrade、rollback 均验证一致。

### C07：Windows 严格模式没有认证完整包清单

**问题风险：P1（Windows 发布路径）。修复风险：高。置信度：高。**

**代码证据**

- [普通 JSON 清单和逐文件 hash](<E:/我的项目/Noyra/开发版/deploy/windows/noyra-lifecycle.ps1:117>)：manifest 提供文件清单、版本及各项 hash，但没有签名/外部可信摘要验证。
- [只对指定可执行扩展名检查 Authenticode](<E:/我的项目/Noyra/开发版/deploy/windows/noyra-lifecycle.ps1:164>)：launcher 发布者受到约束，非 PE 源码和其他文件没有同一包来源证明。
- [builder 清单生成](<E:/我的项目/Noyra/开发版/scripts/build-windows-package.ps1:118>)：写入普通 JSON，没有为完整清单建立由发布者认证的绑定。

**根因**：自述 hash 可证明文件与清单一致，不能证明清单来自受信发布者。认证了一个 launcher 的签名，不等于认证它旁边所有源码、脚本、配置和依赖库存。

**触发条件**：安装者依赖严格签名参数判断完整 Windows 包来源；分发包可被替换；替换者保留允许发布者的原 launcher，改变其他清单文件并重算 hash。需要影响本地导入/分发包，不是普通远程用户直接触发。

**影响**：严格模式仍接受来源未经认证的完整文件集合。实际能否执行被修改文件，取决于真实 Noyra launcher/运行时加载方式；本轮没有把此事实扩张成已经证明任意代码执行或提权。Linux 的 Git/发布证据链不因该 Windows 问题自动失效。

**复现**：在 C06 的单项数组问题之外，使用两项有效形状的外部允许列表。两个合成包的 launcher SHA-256 完全相同、证书完全相同；第二包修改一份无害 Python 文本并重算普通清单，两次严格 install 都成功：

```json
{
  "same_trusted_launcher": true,
  "changed_non_pe_source": true,
  "strict_install_original": "accepted",
  "strict_install_rewritten": "accepted",
  "executable_started": false
}
```

这里用 Microsoft 系统文件仅作为“已被测试安装策略明确允许的发布者”夹具，不声称它是 Noyra 官方 launcher。

**归因**：属于存量的完整包信任缺口。更早的 `b2efa08` 增加发布者指纹和 launcher 库存是有价值的加固，但没有完整包认证；上一轮未修改此模块。历史文档中已经承认过这一外部门禁不足，本报告重新用当前代码复现并保留为未关闭事项。

**修复方向与边界**：让完整 manifest/包摘要由外部受信发布身份签名，并在使用该库存之前验证；绑定发布 SHA、文件库存和允许的签名身份，设计证书轮换和离线验证。不要从包自身读取“自己信任自己”的公钥，不跳过已有 hash、路径和 reparse-point 检查。

**回归要求**：保留 launcher 而重写源码/脚本/DLL/配置/清单全部被拒绝；伪造发行号、签名错误、签名主体不符、旧合法版本 rollback、发布者轮换；真实 Noyra launcher 和干净 Windows VM 端到端验收。当前原生测试 5 passed 不等同于这些门禁通过。

### C08：两项现有后端测试失败，验收未闭环

**问题风险：P2。修复风险：低。置信度：高。**

**第一处：入站调度测试替身不符合新增网关契约**

- [旧测试替身](<E:/我的项目/Noyra/开发版/tests/test_inbound.py:310>) 使用 `cast(ModelGateway, object())`，没有任何网关属性。
- [新的调度读取](<E:/我的项目/Noyra/开发版/src/noyra/cognition/interaction.py:255>) 需要 `requires_explicit_prepared_retry`，这对实际 ModelGateway/RoutedModelGateway 是合法契约。
- 真实测试报 `AttributeError: 'object' object has no attribute 'requires_explicit_prepared_retry'`。

**归因**：`30c43a1` 改变了 `_due_interaction()` 的依赖契约，但没有同步现有测试，属于上一轮次生验收遗漏。没有证据说明实际生产网关缺少该字段，不能把测试替身失败当作生产构造必然崩溃。

**第二处：runtime export 跳过表清单没有跟随 schema 104**

- [过期断言](<E:/我的项目/Noyra/开发版/tests/test_m42_p1_04_runtime_export.py:670>) 只允许历史 skipped 集合。
- [当前安全导出契约](<E:/我的项目/Noyra/开发版/src/noyra/core/runtime_export.py:696>) 正确跳过 `participant_credentials`、`participant_sessions`、`participant_api_credentials`、`participant_auth_limits`。
- 测试因为这四张额外的 skipped 表而失败。

**归因**：schema 104 的 `6baff1c` 集成遗漏，在 `560ec7f` 已存在，非上一轮新增。

**触发条件和影响**：运行当前 CI 同样执行的标准 pytest 入口即会包含这些测试；本地已单独重跑，结果为 **2 failed in 6.46s**。在这两个失败关闭之前，不能报告“所有 CI 质量门禁通过”。没有查询 GitHub，因此不据此描述某个远端 run 或 PR 的当前状态。

**根因修复方向与边界**：用真实的最小网关/FakeProvider 或符合接口的替身测试调度；更新并强化完整导出库存断言，显式验证认证表不进入诊断包。不能为了让断言通过而导出密码、会话或 bearer，也不要给正式代码加入专门迁就 `object()` 的弱化路径。

**回归要求**：两项测试及同文件组合、schema 103→104 导出归属、跨主体碰撞和空表，CI 支持的 Python/OS 组合。修复测试不替代 C01 等产品问题的回归。

## 5. 上一轮修复的独立质量评价

### 5.1 原目标的复核结果

| 上一轮目标 | 当前证据 | 保留/补修建议 |
| --- | --- | --- |
| B01 凭据恢复/退出竞争 | 原会话、CSRF、账户、到期和实体状态在写事务内复核；本轮回归通过 | 保留；没有发现要求回滚事务边界的证据 |
| B02 冻结社交来源与目的地 | 新消息不会让原响应改投另一房间/机器人；撤销后的提交会拒绝 | 保留冻结和复核；补 C04 候选可运行性 |
| B03 未知调用让出队首 | 保留 call/attempt/费用证据，独立交互可继续；授权恢复测试通过 | 保留；C01 必须补齐新增事件的完整性契约；C08 同步测试 |
| B04 主动社交全局记忆隔离 | 私密/混合/旧无归属 memory 不再被主动社交召回；运行时 embedding 接口保留 | 保留；补 C02 的直接旧交互和 C03 的其他私人状态通道 |
| B05 entity 前缀兼容 | 新来源归属与旧文本标识不再混为一类，现有旧标识回归通过 | 保留，不恢复前缀猜测 |
| 集成追审的 prepared 上下文冲突 | 直接交互会隔离，避免换 key 重发，但新事件使核心审计误报 | 保留失败关闭原则；C01 修复后再作为完整集成验收通过；社交另补 C05 |

### 5.2 质量等级及理由

**整体：中。**

- 根因方向较好：没有用删除未知账本、释放模糊费用、重新命名请求或改投渠道来制造成功；凭据授权也进入了真正需要的写事务。
- 针对性测试有价值：本轮重新运行新增回归和原有 social 测试，45 项通过；说明几个原问题的主要路径确实得到改善。
- 集成质量不足：新审计事件没有进入完整性注册器；新增路由检查没有同步调度准入；旧测试的网关契约也没有同步。这些都跨越“模块内部复审”的边界，足以否定当前版本已经没有验收阻断项。
- 不能把所有新发现归咎于上一轮：C02、C03、C05、Windows 两项以及一部分 C01/C08 已在基线存在。它们影响当前版本，但不证明上一轮新写了这些缺陷。

**最终回滚建议：整轮保留，定点补修。** 全盘回滚会重新打开旧会话发凭据、响应改投会话、未知交互堵队首和主动社交记忆泄露等已经修正的路径。C01 的错误事件不能靠直接删除处理，C04 也不能靠撤掉路由授权复核处理。若当前版本尚未部署，应先停在本地验收阶段；本报告没有推断生产正在使用该提交。

## 6. 实际验证结果与限制

### 6.1 静态与前端

- `ruff check src scripts tests`：通过。
- `ruff format --check src scripts tests`：484 个文件已符合格式。
- `mypy src tests`：463 个源文件通过。
- 产品/脚本/测试 Python AST：解析错误 0；原始逐文件清单见证据目录。
- Node 前端测试：**152 passed，0 failed**。这是 Node 测试，不是真实浏览器或实机操作验收。
- pytest 收集：**3,171 项**。

### 6.2 后端组合

以下组合相互不重叠；失败数量按唯一测试计，额外单项复跑不重复累加。

| 组合 | 结果 |
| --- | --- |
| 上一轮新增回归 + 原有 relationship_social | 45 passed |
| 完整性运行时 + wallet economy + migration transfer + research + capability | 180 passed，7 subtests passed |
| HTTP 日志 + 公网连接错误边界 | 61 passed |
| kernel、旧 schema 71、归档/导出/训练同意/归属/检索/embedding | 143 passed，1 failed |
| 渠道身份/入站、审计配置、安装器、完整性加固、交互、内在思考 | 91 passed，1 failed，13 skipped，2 subtests passed |
| Windows package 原生测试 | 5 passed |
| 自主项目/认知/目标/睡眠/世界/学习/公共知识/迁移端到端/删除补充组合 | 140 passed，33 subtests passed |

上述七组累计 **665 passed、2 failed、13 skipped，另 42 个 subtests 通过**。13 项 skip 来自当前 Windows 环境不具备 Linux root/Bash 实机条件，不能记作已通过。

另单独复跑 C08 两项：**2 failed in 6.46s**，失败原因均已确定。全量 pytest 尝试在约 35% 时中断，另一次 fail-fast 尝试也未完成；不能把这些中断前的通过输出当作全仓通过。本报告以完整结束的组合与独立复现为依据。

### 6.3 本地依赖环境

本地解释器为 Python 3.12.10。`pip check` 报当前虚拟环境安装的 certifi 为 `2026.7.22`，低于项目声明的 `certifi>=2026.10.19`。这是本地环境不完全符合依赖声明的事实，本轮没有安装或升级依赖，未把它计作已确认的产品逻辑缺陷。测试结果应连同这一环境限制理解。

没有查询在线漏洞库、验证锁文件下载内容、运行干净 Docker 构建，或验证 GitHub 最新 run。没有 Linux/root 真机、真实供应商超时、真实钱包、真实支付/打赏、真实 Noyra Windows launcher 或八项外部门禁结论。

## 7. 一项需要明确语义的观察（不计入确认问题）

### O01：社交未知重试失败后，换房间可形成新的请求身份

**行为已复现；是否构成同一业务请求被绕过尚需明确。潜在风险 P2；若修改恢复语义，修复风险高。**

当前主动社交 purpose 带小时和 `route_key`；同小时的 unknown、executing 和异路由 prepared 会被拦截，但一次授权重试已变为 failed 后，该旧行不在 `unfinished` 查询里。收到同一联系人的新房间消息后，新 route_key 会形成另一个 purpose。裸网关与真实资源路由都复现：先有一个 unknown 尝试，人工授权的重试明确失败，未出现 retry_closed 事件，再来新房间消息会产生第三次合成 provider 调用并提交新的社交决定。

基线 560ec7f 的同小时对照没有产生第三次调用：裸网关因输入冲突抛异常，路由网关继续失败。原调用/原未知尝试仍保留，并没有被新代码删除。

**没有直接判为确定缺陷的原因**：新房间的独立邀请可以合理地被定义为新的交流业务；旧调用的未知结果不应天然禁止主体处理所有新消息。反过来，若设计要求“同一小时未完成的主动社交评审”是同一业务，则新 route_key 的确需要延续其恢复围栏。必须先定义这一边界，不能仅依据 provider 调用次数增加就断言出现了重复执行。

建议明确社交评审的稳定业务身份与独立新邀请的区分，再补一组原 unknown、授权重试失败、关闭授权、跨小时、同/异房间的测试。不要直接全局阻塞新邀请，也不要默认更换路线等于原结果已核对。本项不是要求自动修复的确认清单。

## 8. 建议修复顺序和安全边界

1. **先修 C01，同时修 C08 的测试契约**：恢复合法事件与完整性验证闭环，得到可信的 CI 基线。保留真实损坏的失败关闭能力和认证表的导出排除。
2. **处理 C04、C05 的社交调度/恢复**：可用候选、冻结路由、缓存恢复和 prepared 冲突一次设计清楚，防止“能拒绝”但“不能继续”。不能给未知结果自动换请求身份。
3. **处理 C02、C03 的收件人上下文**：以统一来源策略检查直接/主动、旧/新联系人、memory/affect/goal/event 字段；不改写历史归属，不删除人格记忆。
4. **修 C06 的 Windows 数组契约**：覆盖单发布者和生命周期各动作；不弱化外部可信发布者约束。
5. **闭合 C07 的完整包认证**：属于发布协议设计，应独立模块推进，保留现有库存/路径/签名防护；完成前不把 Windows 严格安装称作完整包来源认证。
6. 每个模块完成后，既运行本模块回归，也运行其真实消费者：审计写入者接完整性、路由接调度/投递、上下文接模型请求/输出归属、Schema 接导出库存。最后再做 CI 支持平台的集成验收。

O01 先明确业务契约，避免把未经确认的语义观察变成新的全局封锁。

## 9. 证据和只读完整性

本轮独立证据目录：`D:\metagrowh\.user-temp\noyra-readonly-audit-8401984-20261024`。

| 文件 | 内容 |
| --- | --- |
| `inventory.json` | 全部跟踪文件 hash、字节/行数、Python AST 和模块清单 |
| `reproduce.py` / `proof.log` / `proof-results.json` | C01—C05 及 O01 的独立产品路径复现；其中证据 key `C06_unknown_retry_route_*` 对应报告 O01 |
| `windows-single-publisher.json` | C06 的原生异常类型和真实调用栈 |
| `windows-proof.ps1` / `windows-package-proof.json` | C07 的两份同可信 launcher、不同源码包的严格安装结果 |
| `confirmed-ci-failures.log` | C08 两项独立重跑的完整错误摘要 |
| `tracked-files-verification.json` | 收尾时逐文件 hash 和 Git 状态核对 |

测试分组日志及收集/前端日志另位于同一用户临时目录的 `noyra-audit-8401984-*.log`。本地临时证据不自动上传 GitHub，也没有混入源码提交。

收尾核对结果：所有 **1,124 个原有跟踪文件的 SHA-256 均未改变**；HEAD 仍为 `8401984f694a65e174311793bb13f169da8db743`；Git 状态只有本报告为新增未跟踪文档。`tracked-files-verification.json` 已记录 `changed_tracked_files=[]`、`head_unchanged=true`、`only_added_report=true`。
