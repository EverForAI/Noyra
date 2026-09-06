# NCAS 相关工作与检索边界

检索日期：2026-09-06。整理署名：Jaxon Grey / Noyra 项目。
判据：[NCAS v1.0](non-command-oriented-subject.md)。
本记录整理发布准备期间的限定检索与部分静态源码阅读，不是系统综述、独立复现或首创认证。

## 可支持的结论

本轮未确认一个以现有可见证据同时满足八项判据的其他项目。
**这不证明不存在符合者，也不能据此证明 Noyra 已满足全部判据。**
Ephemera、Eva01、Infero、Conway Automaton 等具有实质相关性，其中多个已经有真实环境接口，
不能把它们一概称为游戏项目、纯文字声明或已被完全排除的反例。

本项目标题中的“首个”是作者的可修订研究主张。未决候选和本项目自己的待验收项都必须保留，
不能将“没有找到符合证据”改写成“找到证据证明不符合”。

## 检索方法与覆盖限制

- 主要来源：GitHub 公共仓库搜索 API、仓库元数据、README、递归文件树及部分固定提交源码。
- 检索既包括确切术语，也包括 digital being、artificial subject、autotelic、intrinsic drive、
  endogenous purpose、own goals、non-command 等近义描述，不能只按 NCAS 名称找同名项目。
- 候选发现主要读取搜索结果前 10 至 25 项，部分按 stars 排序；未穷尽所有分页、低星项目或 fork。
- GitHub 仓库搜索不等于全站代码搜索；加上 `in:readme` 仍不能覆盖未写入 README 的实现。
- 通用搜索引擎尝试返回的结果相关性不足，不能据此声称完成全网或论文数据库的系统性检索。
- 未安装或运行其他项目，没有替它们执行八项统一测试、实网部署或故障注入。
- 仓库 `created_at`、commit 作者日期和首次公开可访问时间不是同一概念；未完成每项能力的历史溯源。
- 2026-09-06 是检索日期，不是 Noyra 的 GitHub 发布日期或可倒填的首发时间。

部分可复查查询及当时返回的数量如下。数量是接口观察，不表示检查过所有命中，也不保证未来不变。

| GitHub 查询 | 当时返回数量 | 解释 |
| --- | ---: | --- |
| `"non-command artificial subject" in:readme` | 0 | 没有同词命中，不等于没有同类实现。 |
| `"artificial subject" in:readme` | 25 | 包含无关课程与研究条目。 |
| `"digital being"` | 86 | 发现 Pippin、Ephemera、Infero 等。 |
| `"intrinsic" "not an assistant" in:readme` | 8 | 发现 Eva01、ai-companion-pi、anima 等。 |
| `"非命令式" in:readme` | 21 | 多为语言、开发流程或其他不相关用法。 |
| `"non-command" "subject" in:readme` | 77 | 有命中但噪声高，不能记成零个仓库。 |

可从 [GitHub 仓库搜索](https://github.com/search?type=repositories) 重复查询。
检索日志原始输出未在本文件中完整归档；以下固定提交链接用于保存关键证据的可追溯入口。

## 重点候选

| 项目与检查入口 | 观察到的相关能力 | 本轮能确认的边界或未决问题 |
| --- | --- | --- |
| [Ephemera AI](https://github.com/ImitationGameLabs/ephemera-ai/tree/9c3a15f8290fdf2370f099c0c270a5f759107b63) | 持续认知循环、事件输入、恢复记忆、主动状态转换及 shell 工具 | README 标记 WIP，但 WIP 不等于不符合。C3 的完整数据流与 C7 的部署级授权、预算仍未完整核验，保留为重要未决候选。 |
| [Eva01](https://github.com/Genesis1231/Eva01/tree/152510e1b10bf38ef4e8605e97177ac6cfdc784f) | 真实视听输入、持久记忆、情绪、工具调用、沉默选择和目标存储 | README 将驱动力系统标为开发中；不足以确认完整 C2。目标存储已经存在，不能写成完全没有自主目标。C3/C7/C8 待完整核验。 |
| [Conway Automaton](https://github.com/Conway-Research/automaton/tree/d8f816881fd24b6f5e3d616e59edec387a447667) | 持续心跳、身份与记忆、实网及经济能力、策略检查、审计与大量测试 | 代码区分 creator 与其他输入，system prompt 描述 creator 提供目标。需追踪普通 creator 请求与管理控制的区别，不能只凭 genesis prompt 或权限层级认定不符合 C3。 |
| [ai-companion-pi](https://github.com/sonopdx/ai-companion-pi/tree/a78e0677ff9d28554997e1d6fe65c9be61337b6f) | 周期唤醒、跨轮记忆、Signal 通信、自发创作和请求唤醒 | 消息 handler 将来信拼接到 Claude prompt，脚本使用 `--dangerously-skip-permissions`；尚未确认满足 C3/C7 的独立边界。定时唤醒本身不是缺陷或排除理由。 |
| [Infero](https://github.com/infero-net/infero/tree/03aa7989462e10f3d446dc8b88860b73640b0af8) | 浏览器/服务器自主循环、持久状态、设备切换、真实 shell 与浏览器行动 | `relay/agent.py` 将输入写入上下文并执行模型选择的代码；未完整核验 C3/C7 及运行时能力范围，不能仅因有 `user_input` 或 shell 就排除。 |
| [Pippin](https://github.com/pippinlovesyou/pippin) | 持续或定时活动、记忆、API 工具和活动生成 | 本轮主要依据 README：onboarding 要求提供 objectives，并描述为追求用户目标。尚未确认独立于外部 objectives 的 C2 路径；未完成全源码核验。 |
| [NOEMA](https://github.com/YucongDuan/NOEMA-SOVEREIGN-1.0.0/tree/d06f2b6a6add51762395dcf74c1326716662c63d) | 声明持久身份、内生目的与受限主体运行时；根目录提供发行 ZIP | 本轮确认了仓库和 ZIP 存在，未重新运行包内代码。关于 MirrorGarden 和对抗测试的先前第三方评测不等于本轮独立复现；C6 未确认。 |
| [BASSK](https://github.com/YucongDuan/Binary-Autopoietic-Semantic-Subject-Kernel) | README 描述内生目标、离线回放、身份与证据链 | README 明确 offline，并写明 external action/network/shell authority 为零；该公开模式未展示 C6 所需的真实信息与人类通信闭环。 |
| [XENOESIS ZERO](https://github.com/YucongDuan/XENOESIS-ZERO-1.0.0) | 目的形成、预测学习、持久状态、证据账本 | README 明确 synthetic causal universe；已展示范围不构成 C6 的真实环境验收。 |
| [anima](https://github.com/dancinlab/anima/tree/4db4a03d182268131c5caa37cad804afc03a8216) | 长驻研究 daemon、状态与记忆实验、真实 HTTP/WebSocket 部署声明 | 仅初读 README 与文件树，未审完活跃运行路径；属于证据不足的候选，不宣布其必然不符合。 |

### 关键源码证据

- Ephemera 的 [live loop 与事件处理](https://github.com/ImitationGameLabs/ephemera-ai/blob/9c3a15f8290fdf2370f099c0c270a5f759107b63/crates/epha-ai/src/agent/epha_ai.rs#L255)
  包含 Active/Dormant/Suspended 循环、事件获取和行动记忆，不应降格为单次聊天模板。
- Eva01 的 [README 驱动力章节](https://github.com/Genesis1231/Eva01/blob/152510e1b10bf38ef4e8605e97177ac6cfdc784f/README.md#L99)
  标记 In Development；[任务存储](https://github.com/Genesis1231/Eva01/blob/152510e1b10bf38ef4e8605e97177ac6cfdc784f/eva/core/tasks.py)
  已有 SQLite 目标记录；二者应同时报告。
- Conway 的 [authority 策略](https://github.com/Conway-Research/automaton/blob/d8f816881fd24b6f5e3d616e59edec387a447667/src/agent/policy-rules/authority.ts)
  和 [creator 目标示例](https://github.com/Conway-Research/automaton/blob/d8f816881fd24b6f5e3d616e59edec387a447667/src/agent/system-prompt.ts#L469)
  是复核 C3 的入口，不是所有请求都会自动执行的证明。
- ai-companion-pi 的 [消息 handler](https://github.com/sonopdx/ai-companion-pi/blob/a78e0677ff9d28554997e1d6fe65c9be61337b6f/scripts/handle_message.sh#L135)
  展示绕过逐次权限确认的启动参数；应进一步核验进程隔离与允许的工具范围。
- Infero 的 [本地 shell 路径](https://github.com/infero-net/infero/blob/03aa7989462e10f3d446dc8b88860b73640b0af8/relay/agent.py#L1033)
  是行动边界检查入口，不是排除全部能力治理的充分证据。

## 统一判定原则与后续证据

1. 名称不同、代码规模小、使用固定规则、需要人类启动或尚处 WIP，都不能单独排除一个先例。
2. `HumanMessage` 是模型协议形式；Noyra 本身也以 `role="user"` 传送不可信消息。需比较的是权限和状态提交路径。
3. Shell 是一种能力，能否满足 C7 取决于授权范围、隔离、预算与审计，而不是是否存在这个词。
4. 管理暂停和资源配置属于判据明确允许的控制，不能对别人的同类控制采用更严格标准。
5. 对重要未决候选和 Noyra 使用相同的场景、判定规则与故障条件；按具体提交和配置保留证据。
6. 新发现的更早实现或相反证据应进入本记录，并相应修订首创主张；不因其影响宣传定位而忽略。

本次尚未完成该统一复现实验，也未完成候选功能首次公开日期的历史核验。
