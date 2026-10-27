# R01 历史证据、旧库兼容与隔离原型验证报告

日期：2026-10-25（北京时间）。产品基线：5f976f8e6f282d6ef74d4e457ea9d45c515e957b。分支：codex/audit-r01-compat-validation。

## 1. 执行结论

本轮是证据/兼容验证，不是 R01 产品修复。未修改 src、deploy、scripts、依赖、schema 版本或生产数据；新增内容仅为文档和 tests 内的隔离验证。未连接服务器、未访问实际实例数据库、未调用真实模型/支付服务、未推送/合并/部署。

关键结论：**“事件来源能够关联到某个参与者”不等于“该关系最初就是为这个参与者创建”。不能把旧记录的当前实体键回填后称为已证明的原始身份。** 可行的低侵入方向是记录“升级时观察到的绑定”并防止未来改绑，保留明确历史不确定性；不是自动重建历史。

原 R01 仍为 P2，修复风险中至高。本轮没有宣称其已经解决，也未对全项目重新给出不存在 P0/P1 的保证。

## 2. 历史证据分层与真实能力

| 来源 | 实际能支持什么 | 不能支持什么 |
| --- | --- | --- |
| 当前 relationships 字段及 state_hash | 当前权重/边界的状态一致性 | state_hash 不覆盖 entity_key、entity_type、subject_id |
| append-only relationship_revisions | revision、权重与引用事件列表的历史 | 没有保存关系创建时的完整身份元组 |
| EventStore 事件链与 payload_hash | 事件存在、内容/顺序符合记录 | 不能给事件中没有的 relationship_id↔entity_key 绑定补证据 |
| participant_sources / message_links | 某条消息的不可变主体归属 | 来源事件可能只是涉及别人；晚加的归属不能倒推关系创建时身份 |
| Participant 的类型 | 应用内部来源分类；human 保持人类、agent/noyra 映射保持原语义 | 不是对现实身份或是否亲自撰写内容的额外认证 |
| relationship_social_runs | 后续社交时点的 relationship_id↔counterparty 摘要见证 | 不覆盖此前创建或每次权重形成时的身份；不能倒推整个信任史 |
| 旧渠道 counterparty / 昵称 | 历史地址或显示文字 | 不等于跨渠道、跨 namespace 的稳定主体身份 |
| 已归档/撤回的私有来源 | 可记录“不可用/待复核” | 不能为补身份而恢复正文、绕过撤回或猜测内容 |

### 2.1 决定性的“不可区分历史”反例

使用同一数据库副本、同名但不同 entity_id 的 Alice/Bob、同一个 Alice 消息事件、同样的关系 ID、revision ID 和创建时间，构造两个历史：

- 历史 A：原本创建给 Alice，来源为 Alice 事件。
- 历史 B：通过现有合法 RelationshipStore.ensure 创建给 Bob，同样引用 Alice 事件；之后只将 entity_key 改为 Alice。

两个最终数据库的全部 relationships/relationship_revisions 内容逐字段相同；事件链均通过，MindEngine 均通过；只读来源分类均显示“关联一致，但不能证明创建时归属”。没有移除原触发器或改写修订历史。

这说明，仅凭现存字段不可能区分这两段历史。即使把检查扩展为“来源事件匹配 + 事件链通过”，也不能安全地自动认定原始身份，更不能把已有信任自动赋给另一个实体。新修复应解决未来可证明的绑定完整性，而不是假装恢复已丢失的信息。

### 2.2 不能把所有异常来源都当作已确认错绑

关系可以依据涉及第三方的事件形成；同一关系引用多个人的事件不必然是损坏。因此实验分类的 review_required 是调查线索，**不是可直接接入生产自动封禁/修复的判定器**。未来产品必须区分：强身份见证明确矛盾、结构/归属损坏、普通多源/泛化证据不足，避免因修复让正常社交大面积失效。

已有 InteractionIntegrity._verify_social_run 对后续社交记录的 counterparty 与当前关系不一致能够报错，本轮以真实社交流程验证了这一点。缺口不是“所有历史都完全没有保护”，而是创建时绑定及没有此类后续见证的路径仍无完整证据。

## 3. 数据分布：已确认的范围与不能推断的范围

### 3.1 仓库固定哈希历史 fixture

盘点 schema-v6、v16、v21、v27、v31、v71、v71-from-v63，共 7 个归档；先按 manifest 校验归档及解压数据库 SHA-256，再只读统计。**七个原始 fixture 的 relationships 均为 0，且均早于 participant schema。**

因此，不能把这些 fixture 的“升级能执行”当作已验证真实旧关系迁移，更不能用它们估计服务器有多少可确认/缺证据/冲突关系。

为补覆盖，在每个临时解压副本中插入一条明确标为合成的旧关系和 revision，引用原 fixture 的 anchor event，然后走现有真实 Database 升级到当前 schema。验证结果：7/7 的原关系与 revision 内容完整保留，没有改键、重算权重、改修订或自动生成身份真相。

- v6/v16/v21/v27/v31 的合成关系来源，在当前升级后仍存在旧事件链覆盖缺口，分类为需复核；不能假定“新建 event_chain 表”就证明了旧来源。
- 两个 v71 fixture 的 anchor 链可检查，但 anchor 是泛化事件，分类为缺少身份见证。

上述是 fixture/既有升级路径的证据限制，不是本轮新增产品缺陷；没有把它作为新增的生产实例漏洞，更未修复/重建它们的历史链。

### 3.2 现代隔离样本

真实调用 InteractionStore、ParticipantStore、InteractionCognition 的关系创建路径；同时构造泛化、矛盾、缺证据样本。用于检查分类分支的三条独立合成样本分别为：

| 分类 | 合成样本数 | 创建时身份已证明数 |
| --- | --- | --- |
| correlated_not_proven：可关联、未证明原始绑定 | 1 | 0 |
| unattested：没有充分身份见证 | 1 | 0 |
| review_required：存在需调查的矛盾线索 | 1 | 0 |

这个 1/1/1 分布是人为设计的测试覆盖，**不是生产比例**。实际服务器数据分布未知，本轮未请求或下载其私人关系/消息。

## 4. 生产代码入口清点与后续改动范围

AST/SQL 盘点发现 18 个直接操作关系表的函数（10 个文件），并补查动态引用和持久化边界。下面的“需要接入”是未来实施要求，不表示本轮已改动。

| 模块 / 路径 | 现有职责与未来必须接入的边界 |
| --- | --- |
| mind/relationship.py | ensure/_ensure、revise/_revise、get/list/find/revisions、_from_row；创建、读取和更新均须核验完整绑定，不能只给 get 加检查 |
| cognition/interaction.py::_revise_relationship | 目前唯一实际运行时关系形成/权重更新者；同事务写新绑定，区分真实消息归属与泛化事件因果引用 |
| cognition/social.py::_candidate_relationships、_commit、_route | 自己直接 SELECT 并用 _from_row 组装，候选与发送前都要核验；缓存/重放必须绑定同一身份见证，不能沿用旧路由推断 |
| mind/engine.py::verify_integrity | mind.state 的身份见证核验；不能只继续比对旧权重 state_hash |
| cognition/thought.py::_agenda_candidates、_context | 直接 SQL 读取社会权重/昵称，须带来源状态，不能绕过共享检查 |
| cognition/motivation.py::_context | 价值/动机上下文中的关系证据 |
| cognition/self_model.py::_context 与源引用验证 | 自我叙述中的关系证据；保留历史，不把缺证据记录默认为已认证身份 |
| cognition/metacognition.py::_candidates | 关系型策略选择的直接 SQL |
| interaction/integrity.py::_verify_social_run | 保留既有较强的后续 social 快照核验，并与新绑定合同兼容 |
| interaction/projection.py::private_state | 主要是 social run 统计而非完整关系身份；管理员应能理解来源级别，但不得外泄私有正文 |
| mind/validation.py、mind/causal.py、self_model 源引用 | 动态表映射/因果引用，不能因没有字面 SELECT relationships 就漏审 |
| core/runtime_export.py | 新表必须明确按主体/父关系归属，更新真实 schema 版本对应 ownership graph；禁止静默漏导新见证 |
| interaction/erasure.py、core/private_sources.py | 旧隐私 intent 的保守复核不能放松；不恢复撤回正文，不把新见证表变成删除/来源校验旁路 |
| core/database.py、保留合同 | 新 schema、触发器/约束、迁移事务、保留 inventory；禁止新增表默认为可丢弃派生缓存 |
| migration/bundle、subject_payload、备份/恢复 | 全库携带及恢复后的核验；sqlite 副本实验不替代真实加密/跨机迁移与签名/版本匹配 |

只改 RelationshipStore._state_hash 或只加一个触发器，均不足以完成 R01。也不应把所有旧 revision 按当前身份重新散列，从而改写历史。

## 5. 旧库兼容原型：已证实与尚未证实

### 5.1 已在临时 fixture 中验证

原型仅存在于 tests；使用 r01_probe_bindings 名称，不加入产品 MIGRATIONS，不改 schema marker。

- 新增 observed baseline，明确 proven_at_creation=0；不改任何旧关系/修订/权重/边界字段。
- BEGIN IMMEDIATE 后重新比对完整旧行指纹与来源分类；来源状态或关系身份在盘点后改变，拒绝提交，不冻结未经复核的新值。
- 同事务创建见证表、不可变触发器与写入；在建表后、写行后注入失败可整体回滚；重试幂等，不覆盖已有不同基线。
- 直接在子进程写行后 os._exit 模拟崩溃，重开后无半完成表/记录，能够重新执行。
- identity subject/type/key 变更被拒绝，原正常权重修订和 no_contact 边界仍可保存。
- 原型见证不能 UPDATE/DELETE，也不能把 proven_at_creation 改成 1。
- SQLite backup/restore 后原关系/修订保持一致，见证触发器仍拒绝改绑。
- 现有诊断导出能导出旧关系/修订；加入未分类新见证表后，export 正确 fail closed，而不是静默丢表。
- 将临时副本标记为高于 CURRENT_SCHEMA_VERSION 的 schema，旧运行时拒绝启动，验证正式 rollout 必须使用版本门槛。

### 5.2 不能据此声称已完成的工作

原型只证明增量方案的若干事务/兼容性质，不证明产品修复已实现。尚未接入真实新版本 schema、全部读取者、管理说明、导出 ownership、保留策略、加密备份或跨机迁移；也没有做断电/文件系统故障注入或生产长期运行。当前产品依旧没有 R01 身份绑定保护。

新表直接丢进旧 schema 的原型会让现有导出拒绝，这是**设计验收发现的必要集成点**，不是本轮引入运行时故障，因为原型未进入产品代码。实施时必须将 schema、读取核验及导出/保留合同作为一个可交付模块完成，不能把原型当成可部署补丁。

## 6. 推荐的兼容决策（待下一轮授权实施）

建议不追求把所有旧关系“补证成已验证”，而采用清晰分层：

1. **新关系：记录创建时完整绑定。** 与关系在同一事务保存 subject/relationship/type/key 的不可变见证；表示软件记录的原始绑定，不是额外的现实身份认证，不要求审查人类是否使用 AI 撰稿。
2. **普通旧关系：记录升级时观察基线。** 保留原 ID、昵称、权重、修订和限制边界，不新增信任、不自动改绑，明确其不证明更早的归属。原来无法区分的静默历史损坏不能靠迁移神奇消除，必须如实保留该历史风险。
3. **强证据矛盾/结构损坏：逐关系隔离和解释。** 不能将 A 的信任搬给 B，不自动修复键或新建关系继承旧权重；阻止依赖该矛盾身份的自动外联。普通多源事件只作为线索，不直接等同于强错绑或一刀切停机。
4. **晚到的新证据：不能改写旧起点。** 记录新的确认时点及来源；若主体决定新建另一段关系，应为新的关联，不把旧权重自动迁移过去。
5. **版本和回退：只允许完整迁移前的备份恢复或受控前滚。** 新库不能交给旧二进制继续运行；不能删除新表就宣称完成回退。
6. **部署前预检：只读统计实际实例的分类和受影响范围。** 本轮无生产统计；未来部署前应在用户授权后执行摘要预检，不将私人正文上传或当作调试日志。

该方向兼顾根因与兼容性：固定未来身份归属，避免制造“历史已经认证”的假象，同时不因旧记录天然缺证据而清空权重或让所有社交失效。它是推荐的风险接受/兼容策略，不是已经生效的行为。

## 7. 验证结果与实施门槛

R01 专项当前 39 passed，覆盖真实创建路径、不可区分历史、单字段改绑、同昵称/不同类型、agent/noyra 映射、泛化/混合来源、晚归属、停用、隐私撤回与归档、只读/限量拒绝、7 代历史 schema、事务故障/崩溃重试、备份恢复、导出拒绝与旧版门槛。

### 最终执行结果

| 验证 | 结果 |
| --- | --- |
| R01 专项 | 39 passed；68.96 秒 |
| R01 + mind/social/participant 身份 + 原有导出/隐私 + 上轮 R05 及社交修复回归 | 177 passed、32 subtests passed；422.19 秒；无跳过 |
| 全仓库 Ruff check / format | 通过；775 个格式检查文件 |
| Mypy src/tests | 通过；475 个文件 |
| compileall / pip check / diff check | 通过 |
| 固定源代码复核 | 626 个代码、测试、脚本及部署文件哈希一致，回归中未修改 |
| 产品差异复核 | src/deploy/scripts/工作流/pyproject 与 5f976f8 无变化；没有 schema 或依赖修改 |

R01 专项包含在组合回归中，不能将两组数字相加为独立用例数。JUnit 还包含 subtests 记录；报告数字遵循 pytest 的原始分类。原合并候选工作区及 R06/R02/R04、R05 分支引用未改变。本轮归档/支付/网络均无真实外部调用。

本地环境为 Windows / Python 3.12.10；未执行 GitHub 全矩阵、完整全项目 pytest 或生产验收。本轮是验证产物交付，不能将其通过视为产品 R01 已修复。

### 下一步建议

**先按第 6 节确认兼容策略，再进入 R01 单独产品实施；不要直接执行历史“可信回填”，也不要同时推进 R03。** 下一轮按“创建/迁移/身份核验 → 所有消费者与发送前核验 → 导出/保留/备份恢复及说明 → 固定版本集成回归”推进，每段独立复核。正式交付前不能缺少任何强制边界。

证据目录：[私有验证记录]。主要文件：historical-inventory.json、各历史版本 compatibility.json、synthetic-distribution.json、direct-sql-consumers.json、compatibility.xml、regression.xml、frozen-source.json。归档原件哈希保持原 manifest；私人消息只存在于临时合成测试，不进入报告或统计输出。
