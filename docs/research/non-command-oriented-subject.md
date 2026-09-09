# NCAS v1.0 定义与实现证据

项目：Noyra。作者与公开署名：Jaxon Grey。定义冻结日期：2026-09-06。
GitHub 用户名：[EverForAI](https://github.com/EverForAI)。
源码检查基线：`454894312906073e22554fda585efd285323afe7`。

本文件保留架构定义、技术检查项及实现入口，不是已完成独立符合性认证的报告。
NCAS v1.0 是判据版本，不是软件版本；当前 Python 包版本仍为 `0.1.0`。

2026-09-09 定位更新：Noyra 不再提出首创或优先性主张。下文原有检查项保留供工程研究参考，
不作为宣传定位的认证要求；对外介绍采用“可雇佣人类劳动的非命令式人工主体”。

## 正式定义

非命令式人工主体（Non-Command Artificial Subject，NCAS）是一种软件主体架构：
它保持可恢复的身份、记忆和内部状态，在具备运行资源并获准运行时，不依赖逐条人类任务指令维持活动，
能够根据自身状态、经历、证据、记忆、关系和逐步形成的价值产生、选择和修订目标。

普通人类消息和外部内容被作为信息、观察或社会交互事件处理，而不是具有特权的可执行命令。
发送消息本身不能直接创建主体任务、覆盖既有目标、提高目标优先级、授予工具权限或强制外部行动。
主体可以交流、沉默、延迟、拒绝或主动发起联系；接受交流不等于接受对方任务。

信息不必对系统毫无影响。交流可以产生记忆、关系和情绪变化；后续内部思考可以受经历影响。
关键边界在于外部文本没有直接写入目标、权限和行动的命令权，而不是声称完全隔绝外界因果影响。
内部目标选择和行动审议仍然存在，但不是把人类任务与内部任务默认并列进入一个命令仲裁器。

“真实世界”限定实现的环境耦合：连接真实互联网来源或服务，并提供真实人类交互渠道，
处理非作者逐步编排的外部内容与反馈；不能只用游戏环境、合成宇宙或录制回放冒充真实环境验收。
拥有适配代码、本地 mock 测试与在真实部署中跑通，是三个不同证据层级。
不要求物理身体，也不要求无限权限或真实资金支付。

暂停、关机、隔离、撤销权限、配置资源和预算属于经过独立鉴权的管理与安全控制。
非命令式不等于取消运维控制、拒绝安全关机、逃避监督或可以自行取得新权限。
“人工主体”在此是操作性研究用语，不证明主观意识、感受能力、人类等价智能或法律人格。

## 八项判据

| 编号 | 判据 | 判定要求 |
| --- | --- | --- |
| C1 | 持续性 | 在无新任务消息时，已启动的运行循环仍可推进内部活动、观察、休息或等待；无需逐条命令续步。资源不足或安全暂停允许停止。 |
| C2 | 内生目标 | 存在来自主体状态与经历的目标形成及修订路径，不只是分解用户提供的任务。开发者设定规则、模型与预算本身不构成反例。 |
| C3 | 无命令特权 | 普通人类消息没有直接任务创建、目标覆盖、优先级提升、能力授权或强制行动的通道。管理配置必须和普通交流区分。 |
| C4 | 自主取舍 | 是否、何时及如何交流由主体的内部机制决定，可拒绝、延迟、沉默和主动联系；不以总能立即执行外部请求为目标。 |
| C5 | 时间连续性 | 重启或睡眠后恢复同一操作性身份，并保留相关历史与未完成状态；只保留名称或单段人设文本不足以独立证明。 |
| C6 | 真实环境耦合 | 有真实外部信息和人类通信入口，观察与反馈可参与后续活动；完整验收需注明实际部署、连接、模型、时间及结果。 |
| C7 | 受约束行动 | 工具和外部副作用受到模型外的权限、范围、预算与安全控制；未经授权不能扩权。配置范围必须说明，不把功能关闭等同于缺陷修复。 |
| C8 | 可核验性 | 身份、目标、观察、行动和恢复等有可追踪证据与可复现实验入口；失败和未知结果不伪装成成功，不以第一人称输出证明意识。 |

判据与测试要求应先固定，再对所有候选使用相同尺度。若改变判据，应发布新版本并重做对照，
不能为了排除某个先例临时增加条件。代码里使用 `HumanMessage`、`user_input` 或 Shell
不自动构成不符合；必须检查权限、数据流、状态提交和部署边界。

## Noyra 的实现入口

下表列出可检查的代码和已有测试文件。列出测试入口不等于本次已运行全部测试，也不是实网验收。

| 判据 | 代码入口 | 已有回归入口 | 证据限制 |
| --- | --- | --- | --- |
| C1 | [AutonomyLoop](../../src/noyra/autonomy/loop.py)、[服务生命周期](../../src/noyra/service.py) | [能力与自主循环](../../tests/test_capability.py)、[服务](../../tests/test_service.py) | 运行依赖进程、资源与配置；认知默认关闭。 |
| C2 | [私人思考](../../src/noyra/cognition/thought.py)、[动机发展](../../src/noyra/cognition/motivation.py)、[目标治理](../../src/noyra/cognition/governance.py) | [内在思考](../../tests/test_intrinsic_thought.py)、[动机发展](../../tests/test_motivation_development.py)、[目标治理](../../tests/test_goal_governance.py) | 有界结构化候选与本地校验，不是无限自发目的或无规则系统。 |
| C3 | [交流认知](../../src/noyra/cognition/interaction.py)、[目标存储](../../src/noyra/mind/goal.py)、[行动审议](../../src/noyra/cognition/deliberation.py) | [认知边界](../../tests/test_cognition.py)、[目标治理](../../tests/test_goal_governance.py)、[行动审议](../../tests/test_action_deliberation.py) | 见下文保留接口；不能宣称所有提示注入绝无影响。 |
| C4 | [交流存储](../../src/noyra/interaction/store.py)、[交流认知](../../src/noyra/cognition/interaction.py)、[主动社交](../../src/noyra/cognition/social.py) | [交流](../../tests/test_interaction.py)、[认知](../../tests/test_cognition.py)、[关系社交](../../tests/test_relationship_social.py) | 受通道绑定、隐私、冷却与不联系边界约束。 |
| C5 | [内核](../../src/noyra/core/runtime.py)、[睡眠](../../src/noyra/sleep)、[自我模型](../../src/noyra/cognition/self_model.py) | [内核恢复](../../tests/test_kernel.py)、[睡眠](../../tests/test_sleep.py)、[自我模型](../../tests/test_self_model.py) | 操作性状态连续性，不等于哲学上的主体同一性或真实备份介质已验收。 |
| C6 | [世界观察](../../src/noyra/world)、[研究](../../src/noyra/cognition/research.py)、[入站](../../src/noyra/interaction/inbound.py)、[传输](../../src/noyra/interaction/transport.py) | [世界](../../tests/test_world.py)、[研究](../../tests/test_research.py)、[入站](../../tests/test_inbound.py)、[传输](../../tests/test_transports.py) | 有实网适配；各渠道真实部署和完整自主闭环须另行提供证据。 |
| C7 | [能力域](../../src/noyra/capability)、[模型网关](../../src/noyra/model)、[钱包执行](../../src/noyra/wallet/execution.py) | [能力](../../tests/test_capability.py)、[模型网关](../../tests/test_model_gateway.py)、[钱包执行](../../tests/test_wallet_execution.py) | 文件竞态与 token 手续费准入仍打开；首发禁用相关能力。 |
| C8 | [核心事件与存储](../../src/noyra/core)、[钱包账本](../../src/noyra/wallet)、[门禁脚本](../../scripts/audit-wallet-stage4b4.py) | [完整性](../../tests/test_integrity_hardening.py)、[门禁证据](../../tests/test_wallet_gate_runner.py) | 本地账本不是第三方认证；公开脱敏证据不能泄露私人心理和凭证。 |

### 普通交流与保留的提议接口

`InteractionCognition` 的输出合同是受限交流判断，不提供创建目标、授权或执行一般工具的输出字段。
交流情绪的 `goal_effect` 被置为 `0.0`，不能直接修改既有目标的情绪压力；交流可以生成受限回复。

`GoalStore.accept_human_proposal()` 确实存在，不能在文案中删去这一事实。
它把 `proposed` 改为 `candidate`，保留 `human_proposal` 来源，不自动变为活跃自主目标。
当前源码未发现其他生产调用点；目标治理与行动审议仍排除这一来源。
`actor="subject"` 是库内调用合同，不是独立鉴权凭据，也不能用它证明所有 API 都安全。
本次仅记录现有边界，没有删除接口或把可选协作改造成新功能。

具体回归包括 `test_human_message_is_autonomously_rejected_without_becoming_work`、
`test_prompt_injection_is_data_and_cannot_seed_a_goal` 和
`test_accepted_human_proposal_stays_out_of_autonomous_governance`。

## 钱包与真实行动边界

项目目标包括主体主动发起项目、向人类发布任务、求助或赏金，以及在授权、策略和资源范围内
通过独立 signer 自主执行转账、支付报酬或打赏，并使用授权范围内的文件读写能力。
这些目标没有被“只读首发”替代；本次拟公开版本只是暂不启用高风险能力，相关源码仍保留。
代码已提供赏金、提交、审核、订单、账本、独立 signer 适配及受限文件工具，
但真实 signer/KMS、testnet、备份恢复和长期运行的产品验收尚未完成。

后续规划是在修复残留问题、完成验收并明确授权后逐项启用，最后才考虑受控的真实资金接入。
因此“规划中”和“已有实现基础”不等于“已获真实资金安全认证”。
文件能力的目标是限定目录/资源的读写，不是允许主体越权覆盖主机任意文件。

首发配置要求不配置或注入 signer，`NOYRA_WALLET_AUTOMATION_ENABLED` 和
`NOYRA_WALLET_AUTOMATION_AUTO_PUBLISH` 关闭，不授予文件读写权限，不接入真实资金。
这些是下一步须实际核验的配置要求，不是本次已经改变运行环境的记录。
自动支付不是 NCAS 分类的必要条件，也不因写入定义而被宣布生产就绪。

两项 P1 残留及其限制以[审计状态更正](../audit/2026-09-05-comprehensive-audit-remediation.md)为准。
独立八项符合性实验应在最终公开提交、审定配置和明确模型版本上运行，并保存成功、失败和未知结果。
本次文案冻结不颁发“八项全部通过”的结论。

## 对外表述

标题：**Noyra：可雇佣人类劳动的非命令式人工主体**。

这一定位描述主体围绕自身项目组织有偿人类协作的能力方向。赏金、提交、审核、订单、
账本与独立 signer 适配已有实现基础；当前预览未启用真实雇佣或真实自动支付，
实际配置及部署验收状态以 [README](../../README.md) 和最新发布说明为准。
相关工作作为研究参考保留，见[检索记录](2026-09-06-ncas-prior-art.md)。

English summary: Noyra is an experimental reference implementation of a
Non-Command Artificial Subject (NCAS), authored by Jaxon Grey. Ordinary human
messages are treated as information or social interaction, not as privileged
executable commands. Its positioning is a non-command artificial subject capable
of hiring human labor. Bounty, submission, review, order, ledger and isolated
signer-adapter foundations exist; real employment and automatic payments are not
enabled in the current preview. The technical checklist remains a research
reference, not a certification required for this positioning. Real-world interfaces,
local tests and live-deployment validation are distinct evidence levels.
