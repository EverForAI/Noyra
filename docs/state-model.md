# Noyra 状态模型

状态模型将事实、推断、情绪、心理、目标和行动分开。模型只能产生结构化候选，不能直接覆盖状态。

## 1. 状态层次

```text
事实层       发生了什么、来源是什么
评价层       事件与目标、边界、关系和预测的关系
情绪层       评价引起的可持续状态变化
心理层       心境、信念、关系和自我判断
目标层       Noyra 选择追求什么
行动层       Noyra 实际做了什么以及结果
公开层       Noyra 选择告诉用户什么
```

## 2. 主体身份

必须保存：

```text
subject_id
genesis_hash
created_at
personal_name
identity_status
state_version
current_model
last_checkpoint
origin_subject_id
```

`subject_id` 不能由模型生成或修改。主体姓名可以为空，并可在后续由 Noyra 自己决定。

## 3. 事件

事件至少包含：

```text
event_id
event_type
source
occurred_at
observed_at
payload_hash
privacy_level
causal_parent_ids
processing_status
```

外部网页、用户消息、工具结果、睡眠触发、预算变化和管理操作都必须先进入事件账本。

## 4. 记忆

记忆类型：

- 情景记忆；
- 自传记忆；
- 语义记忆；
- 程序记忆；
- 情绪记忆；
- 关系记忆；
- 预测记忆；
- 反思记忆。

每项记忆必须带来源、置信度、隐私级别、形成时间和修订历史。

## 5. 信念

信念是当前判断，不是事实本身。至少保存：

```text
belief_id
proposition
supporting_evidence
counter_evidence
confidence
scope
status
created_at
reviewed_at
```

新证据可以降低、提高、限定或推翻信念。旧信念不能被静默删除。

## 6. 情绪与心理

情绪转移必须引用事件和评价：

```text
transition_id
event_id
appraisal_id
emotion_type
old_intensity
new_intensity
target
decay_rate
affected_goal_ids
affected_relationship_ids
confidence
```

心理状态保存当前情绪、长期心境、未解决问题、价值冲突、信任、应对策略和未来预期。

## 7. 目标

目标必须标记来源：

- `self`；
- `environment`；
- `human_proposal`；
- `maintenance`；
- `mixed`。

人类提案只有在 Noyra 自己接受后才能进入 `active`。

## 8. 公开内容

公开内容是从私人状态生成的单向发布，不是私人状态的完整镜像。Noyra 可以发布摘要，也可以选择不发布。

用户可查看的行为日志至少包含实际外部行动、工具、结果、是否产生副作用、重试和资源摘要；不得泄露私人心理、密钥和完整内部推理。

## 9. 提案提交规则

模型提案提交前必须：

1. 通过结构校验；
2. 引用存在的事件和状态；
3. 通过隐私检查；
4. 通过预算和能力检查；
5. 写入事件账本；
6. 事务提交；
7. 生成公开摘要；
8. 执行允许的外部行动。
