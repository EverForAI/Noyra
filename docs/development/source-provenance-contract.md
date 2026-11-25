# 来源分类契约 / Source Provenance Contract

**范围 / Scope:** 05a (A-02) defines source semantics and legacy unknown handling; 05b (A-05/B-06)
applies them to motivation context and help-reply review. Source labels are not trust credentials or
authorization.

## 中文

### 语义

来源分类仅描述一条记录显式引用的事件类型，不判断内容真假、作者身份、指令优先级或是否可执行。
当前仅识别两个精确的直接来源组合：

- `interaction_received` 或 `human_message` 且 `source` 为 `human`：`human_interaction`。
- `assistance_reply` 且 `source` 为 `participant`：`human_assistance_reply`。
- `world_observation` 且 `source` 为 `safe_web_reader`：`external_observation`。
- 其他事件类型、来源字符串不匹配、找不到事件或跨主体引用：`unknown`。

`direct_references_classified` 只表示此记录列出的每个直接事件引用都可解析且匹配已知组合；它**不表示完整的跨周期祖先链已证明**。
解析无效、来源缺失、旧记录引用无法解析或遇到未知事件类型时，不补造标签、不回填成可信来源，结果为 `unknown`；已撤回的私密来源不会被报告为已分类。已知来源和未知/撤回来源混合时为 `partial`。

本契约没有 `trusted` 分类，也不提供授权接口。人类输入和外部观察即使来源可识别，仍是非权威内容；分类器不是信任凭证或安全分类器。事件因果父项、记忆到记忆派生边和关系字段目前不自动递归展开。关系身份绑定只能证明记录关联，不会把 `display_name` 变成指令或可信事实。

### 05b 消费边界

动机评估上下文为当前活跃记忆附加直接来源状态和类别；旧的 `experience` 等未列入精确白名单的来源继续标为 `unknown`，不会被提升为可信。上下文仍可包含这些记忆，以保持普通经历影响内部思考的能力；系统提示明确该标签不验证事实或授予权威。关系上下文保留有界的数值关系状态和身份一致性标记，但移除自由文本 `display_name`，避免把外部可控名称放进长期动机判断。

求助回复进入模型前携带 `human_assistance_reply`、`verification=unverified` 和 `authority=none`。文本分类器命中时仍隔离；无命中只表示没有已知信号，不是安全结论。模型结果仍限于 `continue/wait/end`，理由仍是待复核的模型说明。持久事件记录其 `assistance_reply` 来源事件 ID；该引用保留在事件载荷中，不新增事件因果父边，以免改变既有派生事件与删除审查边界。该处置只允许项目恢复自身复审流程，不授权任务执行、阶段选择、权限变更或付款。撤回/删除检查和既有求助归属校验保持不变。

本轮不递归证明记忆谱系，不把未知旧记忆从日常认知中整体移除，不改变价值/使命的现有渐进约束，也不将分类器升级为信任或安全判定器。

### 兼容与实施边界

读取采用原有 `source_event_ids_json`，不改写或删除历史记忆、信念或关系记录，不新增 schema 迁移。动机上下文标注记忆的直接来源、保留未知旧记忆作为非权威经历，并移除关系显示名。求助复审携带显式的未验证来源标记和因果事件引用；既有撤回、归属及处置门禁保持不变。普通交流、普通记忆和合法项目复审不因来源标签被整体阻断。

实现位于 [`src/noyra/core/source_provenance.py`](../../src/noyra/core/source_provenance.py)。它只返回来源描述和解析状态，不返回信任或授权结论。

## English

### Semantics

A provenance category describes only the event types explicitly referenced by a record. It does not
establish truth, author identity, instruction priority, or permission to act. The classifier recognizes
only these exact direct-source pairs:

- `interaction_received` or `human_message` with `source` `human`:
  `human_interaction`.
- `assistance_reply` with `source` `participant`: `human_assistance_reply`.
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

### 05b consumer boundaries

Motivation context now attaches direct-source status and categories to active memories. Legacy sources
such as unlisted `experience` events remain `unknown`, not trusted. Those memories remain available so
ordinary experience can still inform internal thought; the system prompt explicitly says the label does
not verify claims or grant authority. Bounded numeric relationship state and identity-consistency markers
remain, while free-text `display_name` is omitted from long-horizon motivation context.

Before a help reply reaches the model, it is labeled `human_assistance_reply`,
`verification=unverified`, and `authority=none`. A classifier hit remains quarantined; no hit means only
that no known signal matched, not that content is safe. Model output remains limited to `continue/wait/end`
and its reason remains a reviewable model explanation. The durable consideration event records its `assistance_reply` source event ID in its payload. It does
not add an event causal-parent edge, which would change existing derivative-event and deletion-review
boundaries. This disposition only allows the project to resume its own review path; it does not authorize
execution, phase selection, permission changes, or payment. Existing withdrawal/deletion and
reply-ownership checks remain unchanged.

This step does not prove recursive memory lineage, remove unknown legacy memories from ordinary cognition,
change the existing gradual value/mission constraints, or turn a classifier into a trust or safety oracle.

### Compatibility and implementation boundary

The classifier reads existing `source_event_ids_json`; it does not rewrite or delete historical memories,
beliefs, or relationships or add a schema migration. Motivation context labels direct memory sources
while retaining unknown legacy memories as non-authoritative experience and omits relationship display
names. Help-reply review carries an explicit unverified source label and source-event reference; existing
withdrawal, ownership, and disposition gates remain. Ordinary interaction, ordinary memory, and legitimate
project review are not globally blocked by provenance labels.

Implementation: [`src/noyra/core/source_provenance.py`](../../src/noyra/core/source_provenance.py). The
result contains source descriptions and resolution status only, never a trust or authorization decision.