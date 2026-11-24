# 来源分类契约 / Source Provenance Contract

**范围 / Scope:** 05a (A-02), source-level semantics and legacy unknown handling. This document does not
claim that downstream privileged consumers have already enforced these classifications.

## 中文

### 语义

来源分类仅描述一条记录显式引用的事件类型，不判断内容真假、作者身份、指令优先级或是否可执行。
当前仅识别两个精确的直接来源组合：

- `interaction_received` 或 `human_message` 且 `source` 为 `human`：`human_interaction`。
- `world_observation` 且 `source` 为 `safe_web_reader`：`external_observation`。
- 其他事件类型、来源字符串不匹配、找不到事件或跨主体引用：`unknown`。

`direct_references_classified` 只表示此记录列出的每个直接事件引用都可解析且匹配已知组合；它**不表示完整的跨周期祖先链已证明**。
解析无效、来源缺失、旧记录引用无法解析或遇到未知事件类型时，不补造标签、不回填成可信来源，结果为 `unknown`；已撤回的私密来源不会被报告为已分类。已知来源和未知/撤回来源混合时为 `partial`。

本契约没有 `trusted` 分类，也不提供授权接口。人类输入和外部观察即使来源可识别，仍是非权威内容；分类器不是信任凭证或安全分类器。事件因果父项、记忆到记忆派生边和关系字段目前不自动递归展开。关系身份绑定只能证明记录关联，不会把 `display_name` 变成指令或可信事实。

### 兼容与实施边界

读取采用原有 `source_event_ids_json`，不改写或删除历史记忆、信念或关系记录，不新增 schema 迁移，也不改变当前动机、睡眠、求助或授权消费路径。无法分类的旧数据保持 `unknown`。05b 才会单独选择少数需要约束的消费入口，并为允许的结构化影响定义合同；普通交流、普通记忆和项目复核不应因此被整体阻断。

实现位于 [`src/noyra/core/source_provenance.py`](../../src/noyra/core/source_provenance.py)。它只返回来源描述和解析状态，不返回信任或授权结论。

## English

### Semantics

A provenance category describes only the event types explicitly referenced by a record. It does not
establish truth, author identity, instruction priority, or permission to act. The classifier recognizes
only these exact direct-source pairs:

- `interaction_received` or `human_message` with `source` `human`:
  `human_interaction`.
- `world_observation` with `source` `safe_web_reader`: `external_observation`.
- Any other type/source pair, missing event, or cross-subject reference: `unknown`.

`direct_references_classified` means only that every listed direct event reference resolves and matches
one recognized pair. It does **not** prove complete cross-period ancestry. Invalid or absent legacy
references, unresolved IDs, and unrecognized event types remain `unknown`; no label is invented and no
legacy record is upgraded to trusted. Withdrawn private sources are not reported as classified. A mix of
recognized and unknown or withdrawn references is `partial`.

There is no `trusted` category or authorization API. Identifiable human input and external observations
remain non-authoritative content. This classifier is neither a trust credential nor a safety classifier.
It does not recursively follow event causal parents, memory-to-memory derivation edges, or relationship
fields. A verified relationship binding proves association only; it does not make `display_name` an
instruction or trusted fact.

### Compatibility and implementation boundary

The classifier reads existing `source_event_ids_json`; it does not rewrite or delete historical memories,
beliefs, or relationships, add a schema migration, or change current motivation, sleep, assistance, or
authorization consumers. Unclassifiable legacy data stays `unknown`. Step 05b will separately select the
few consumers that need constraints and define their allowed structured influence. Ordinary interaction,
ordinary memory, and project review must not be globally blocked by provenance labels.

Implementation: [`src/noyra/core/source_provenance.py`](../../src/noyra/core/source_provenance.py). The
result contains source descriptions and resolution status only, never a trust or authorization decision.