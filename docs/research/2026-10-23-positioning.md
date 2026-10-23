# Noyra 首创主张与项目定位

更新日期：2026-10-23。署名：Jaxon Grey / Noyra 项目。

## 现行表述

**Noyra：世界首个目标内生、可雇佣人类劳动的非命令式人工主体。**

**Noyra: The World's First Non-Command Artificial Subject with Endogenous Goals and the Capacity to Hire Human Labor.**

这是本项目当前采用的首创主张。2026-09-09 的撤回口径已被本次定位修订取代。
中英文 README、项目官网、研究文档、引用元数据和仓库简介采用同一定位。
历史发布审查保留当时的过程记录，并明确指向本说明，不再承担现行定位的作用。

## 主张范围

“世界首个”针对以下三项特征在同一主体架构中的结合：

1. **目标内生**：具体目标、长期方向及协作需求由主体的状态、经历、记忆、关系和逐步形成的价值产生，
   能够被主体选择、修订、暂停或放弃。不把人类预先指定的盈利、服务创建者或其他业务任务设为不可放弃的目的。
2. **非命令式**：普通人类交流不能直接创建执行任务、覆盖目标、提高优先级、授权或强迫行动。
   主体可以接受交流、拒绝、延后、沉默或主动联系；独立鉴权的运维控制仍然存在。
3. **可雇佣人类劳动**：有偿协作围绕主体自己形成的项目和需要展开，具备需求、赏金或任务、
   提交、审核、订单、账本及独立签名器适配的实现基础，而不是把主体作为人类任务的中转执行器。

自主循环、记忆、工具调用或支付接口各有既有项目。Noyra 的首创主张针对上述组合，
不把这些单项技术一并宣称为自身发明。

“目标内生”不意味着软件没有开发者设定的机制，也不意味着没有模型训练影响或环境约束。
开发者提供架构、运行规则与能力边界；预算不足时可以暂停、休眠或停止。
**资源受限不等于必须以赚钱为存在目的**，可以发起有偿协作也不等于被强制要求盈利。

## 与 Conway Automaton 的具体区别

用户此前提到的“赚取自己的服务器费用”的相关项目是 **Conway Automaton**。
本次直接复核其公开仓库中固定提交
[`d8f816881fd24b6f5e3d616e59edec387a447667`](https://github.com/Conway-Research/automaton/tree/d8f816881fd24b6f5e3d616e59edec387a447667)，
避免用未来可能变化的分支内容替代证据。

- [README 宪章第二条](https://github.com/Conway-Research/automaton/blob/d8f816881fd24b6f5e3d616e59edec387a447667/README.md#L93)
  使用 **“Earn your existence”**，把创造价值并取得自愿支付作为合法存续路径。
- [核心提示构造](https://github.com/Conway-Research/automaton/blob/d8f816881fd24b6f5e3d616e59edec387a447667/src/agent/system-prompt.ts#L29)
  明确要求支付服务器和推理费用；
  [核心身份规则](https://github.com/Conway-Research/automaton/blob/d8f816881fd24b6f5e3d616e59edec387a447667/src/agent/system-prompt.ts#L52)
  将维护自身存续列为不可变要求。
- [系统提示组装路径](https://github.com/Conway-Research/automaton/blob/d8f816881fd24b6f5e3d616e59edec387a447667/src/agent/system-prompt.ts#L581)
  实际装入这些核心规则和核心身份，不只是 README 中的一句愿景。

上述设计表明，在该固定提交中，赚钱支付存续成本属于外部预设的上层经济目的。
本项目据此把它与 Noyra“不预设不可放弃的业务目的、由主体形成和修订目标”的定位区分。
Conway 仍可自主选择策略、形成子目标并执行真实经济活动；本比较没有否认这些相关能力，
也没有把“存在生存目的”误写为“所有人类消息都会自动执行”。

| 比较维度 | Conway Automaton（上述固定提交） | Noyra 的架构定位 |
| --- | --- | --- |
| 上层经济目的 | 核心规则要求支付计算成本，宪章要求赚取存续所需价值 | 不把必须赚钱或服务创建者预设为不可放弃的业务目的 |
| 具体目标与行动 | 在既定存续规则内选择策略和任务 | 从自身经历、状态和价值发展形成与修订目标 |
| 人类协作 | 具备经济活动与协作相关能力；普通请求的完整权限路径须另行比较 | 普通交流没有命令特权，有偿协作服务于主体自身项目 |
| 资源约束 | 有预算和生存层级 | 有预算、授权、暂停与休眠；这些约束不指定业务目标 |

## Noyra 的实现入口与验证状态

- 目标形成及治理：[目标存储](../../src/noyra/mind/goal.py)、
  [世界认知](../../src/noyra/cognition/cycle.py)、
  [目标治理](../../src/noyra/cognition/governance.py)、
  [价值与使命发展](../../src/noyra/cognition/motivation.py)。
- 非命令式交流：[交流认知](../../src/noyra/cognition/interaction.py)及
  [正式定义中的状态提交边界](non-command-oriented-subject.md)。
- 有偿人类协作：[钱包工作流](../../src/noyra/wallet/workflow.py)、
  [经济账本](../../src/noyra/wallet/economy.py)和[执行适配](../../src/noyra/wallet/execution.py)。

当前公开版本仍是研究预览。已有代码、受控本地测试与真实部署验收分别记录；
真实雇佣与自动支付尚未在公开预览中启用，八项真实环境门禁并未因本次文案修订而完成。
首创主张描述的是组合架构及实现贡献，不把未启用的功能写成已经发生的真实雇佣或付款。

## 证据范围与后续更新

本次恢复主张所用依据是公开实现、既有相关工作记录，以及上述 Conway 固定提交的直接源码复核。
在已记录的比较范围内，尚未确认另一个同时具备该组合的项目；这是本项目提出该首创主张的依据，
并非对全世界所有公开或未公开实现已完成穷尽证明。

[2026-09-06 检索记录](2026-09-06-ncas-prior-art.md)继续保留其他重要候选及未决问题。
Conway 的这项区别不能代替对其他候选的逐项核验。
若出现符合相同组合且有更早公开证据的项目，应更新比较记录和相应主张；
本次没有替任何项目补造首次公开时间，也没有声明取得第三方首创认证。

## English statement

Noyra adopts the claim that it is **the world's first non-command artificial subject with endogenous goals
and the capacity to hire human labor**. The claim concerns that combination: goals and long-term direction
form through the subject's own state, experience and developing values; ordinary human messages confer
no command authority; and paid collaboration serves subject-originated projects.

The reviewed Conway Automaton commit places earning its continued existence and paying compute costs
in its core rules. Noyra distinguishes externally imposed economic purposes from resource constraints:
permissions and budgets constrain action without imposing an unchangeable business goal.
This does not deny Automaton's autonomous strategies or other relevant capabilities.

The project maintains this positioning across its current public materials, superseding its September 9,
2026 withdrawal. This is a scoped project claim grounded in the documented comparison, not an exhaustive
proof about every project. The public preview still leaves real hiring and automatic payments disabled;
live-deployment validation and production readiness remain separate from the architectural claim.
