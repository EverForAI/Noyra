# R01 / R03 / R05：方案、安全边界与验证设计

日期：2026-10-25（北京时间）。固定产品基线：8a629d49401fdc83d583a2341bd0886e69886ed0。

## 1. 授权、顺序与交付边界

用户授权先完成三项设计，再单独实施 R05；R01、R03 不进入产品实现。复用干净的独立工作树，新分支 codex/audit-r05-shutdown；保留原修复分支及原合并候选，不推送、不合并、不部署、不修改生产数据库或凭据。

| 项目 | 问题风险 | 修复风险 | 当前安排 |
| --- | --- | --- | --- |
| R05 向量硬超时后的关闭无限等待 | P2 | 中至高 | 设计与安全验证后单独实施 |
| R01 关系身份缺少不可变来源证据 | P2 | 中至高 | 完成方案与验证矩阵；历史兼容策略验证前不实施 |
| R03 Windows signer 轮换的多记录恢复缺口 | P2 | 高 | 完成方案与验证矩阵；信任撤销/兼容协议确定前不实施 |

所有模块都要求：原反例、正常路径、故障路径、持久状态和所有权审计。先冻结源代码，再运行验收；运行中不修改被验收源文件。若修改，重新执行受影响验证。单项验收后提交，不能把三项混合实现后才审计。

## 2. R05 设计（本次唯一实施项）

### 2.1 根因与已确认入口

EmbeddingGateway 的 hard timeout 只结束调用者等待，不能终止 Python 工作线程。close(wait=True) 默认无限等待；CognitionCycle.aclose 在事件循环内直接执行该阻塞关闭。外层 asyncio.wait_for 无法及时调度取消。服务正常退出路径虽然先关闭 admission，但同步 service.close 只等 operation lease，未同时核对硬超时后仍存活的 embedding worker。

在本基线上重跑原审计的真实 OpenAIEmbeddingProvider 本地授权阻塞探针：embedding_hard_timeout；账本 unknown；worker 仍存活；外层关闭 timeout=0.05 秒，但要等另一个线程 0.5 秒后释放，实际约 0.515 秒才返回且没有 TimeoutError；网络请求为 0。

### 2.2 安全选择：有限等待，未排空则显式失败并保留所有权

不尝试强杀 Python 线程，不以“不要等了”冒充安全退出。选择保守 fail-closed 策略：

1. gateway 开始关闭即拒绝新的 embed，拒绝发生在预算授权和账本创建之前；已接入调用仍须完成原有结算。
2. 将完整 embed 调用计入活动集合（包含授权、线程提交、响应校验、账本结算和 accounting 回调），不能只统计 provider 线程；否则线程已退出但调用者仍在写账本时会虚报排空。
3. 同步 close 默认不等待；显式 wait=True 且未提供 timeout 时使用有限默认值 1 秒。timeout 只允许 0–300 秒的有限数（上限同时避免条件等待的底层溢出）；0 表示立即检查，不提供无限等待模式。
4. 增加 async aclose，以事件循环可取消的短间隔轮询排空；不用 asyncio.to_thread 搬运无限等待，不创建默认线程池遗留 join。默认 1 秒只约束 embedding 排空，不是服务整体停机期限，也不改变供应商请求超时。
5. caller 提供的 ThreadPoolExecutor 由 caller 管理；gateway 不 shutdown、不擅自取消该 executor 的任务。移除已无真实 owned executor 的遗留无限 shutdown 分支。
6. gateway 同步/异步关闭以 bool 明确返回是否排空；未排空不是成功。CognitionCycle 将 False 转为明确的 embedding_shutdown_incomplete 错误，在所有组件成功关闭前不标记 complete；可取消、可重试、并发关闭串行化。
7. embedding 先排空，再关闭研究/网页/模型客户端，避免失败重试时把其他组件先关一半。
8. NoyraService.run 现有 finally 在 cognition 关闭失败时不会走到 kernel.close；为同步 service.close 补相同的排空守卫，不能被 finally/异常抑制悄悄释放进程锁。
9. 仍有 worker 时保留运行锁；工作最终退出后允许重试 cleanup。daemon worker 不表示能够安全释放所有权。生产进程退出/外部管理器终止时由 OS 回收锁；不承诺能够在进程内杀死任意自定义 provider 或使借用 executor 的解释器退出有界。

### 2.3 账本、迟到回调与运行围栏

保留现有硬超时 unknown 及预算预留，不伪造为未发送、不自动重试、不让迟到结果覆盖 unknown。业务写入继续通过复制到 worker 的 OperationLease 与 Database.transaction 的 commit scope 受 epoch 约束；terminal accounting 的现有窄作用域不扩大。服务开始 drain 后，真实 provider 授权后再次检查 lease，迟到响应的维度观测等事务应被拒绝。

此方案不改变 SubjectKernel 的通用所有权机制，不给任意第三方线程授予绕过围栏的能力；未受支持地直接操作 SQLite 或自行创建脱管线程不在 gateway 能强制隔离的能力范围内。测试必须覆盖真实 provider 的授权、HTTP/响应回调，以及自定义 provider 的迟到事务。

### 2.4 实现范围与不变量

计划改动：model/embedding_gateway.py、model/errors.py、cognition/cycle.py、service.py 的同步关闭守卫；新增定向回归。无 schema、预算规则、供应商超时、关系/迁移/Windows 信任格式变化。

- G1：关闭后新调用不会新增账本、费用预留或外部调用。
- G2：已接入调用、daemon worker、借用 executor future 任一未完成时均不报告排空。
- G3：异步关闭等待期间 heartbeat 能继续；外层取消有效。
- G4：关闭超时/取消可重试；CognitionCycle 不提前报告 complete。
- G5：任何受支持的 service 关闭入口都不在未排空时释放运行锁。
- G6：旧 worker 在 admission drain 后不能提交新的主体业务变更；unknown 记录保持不变。
- G7：正常关闭、没有 embedding 的关闭、外部 executor 的所有权与线程启动失败清理不退化。

### 2.5 验证矩阵与实施门槛

| 用例 | 验证重点 |
| --- | --- |
| 原审计真实 provider 授权器卡住 | hard timeout 后 heartbeat、外层取消和有限排空均有效；无网络 |
| provider 响应/维度回调卡住 | 未排空前保留锁；drain 后不能落业务事务；unknown 不被迟到结果冲掉 |
| 自定义 provider 卡住及迟到写入 | 关闭失败明确；真实进程锁竞争被拒；旧 lease 写入被拒 |
| caller 正在账本结算/accounting，worker 已退出 | 仍返回未排空；不提前释放锁 |
| 关闭与进入/授权/提交并发 | 接入或拒绝必须可解释；没有新调用在 closed 后创建用量 |
| 借用 executor 的执行与排队 future | 等待有限；不接管 executor；完成后可重试关闭 |
| 正常完成/非法输入/worker 启动或 executor 提交失败 | 活动计数归零；不会永久误判忙碌 |
| 重复/并发/取消关闭 | complete 只在实际完成后设置；重试不跳过 embedding 守卫 |
| Service.run 异步退出及 Service.close 同步退出 | 错误后进程锁仍持有；排空后 cleanup 才释放 |
| 关闭参数 0、默认、负值、NaN/Infinity | 有限合同清晰；非法参数不改变状态 |

实施门槛：以上设计不需改变所有权/费用持久合同；原反例可本地无网络复现；入口清点包含同步 close。以上已满足，可以进入 R05 产品实现。若实际实现要求绕过 epoch、抹去 unknown、强杀线程或扩大到其他关闭子系统，停止重新设计。

## 3. R01 设计（本次不实施）

### 3.1 根因与威胁边界

relationships 与 relationship_revisions 的状态散列只覆盖社会权重和 boundaries；合法 entity_key / entity_type / subject_id 没有独立身份来源见证。具有本地库写权限的错误将 A 改为 B 时，读取、mind.state 和 social 路由仍可能将 A 的权重用于 B。不是匿名用户已经获得写库能力的证明，也不声称能抵抗拥有根权限者重写所有历史。

### 3.2 拟议数据/读取合同

- 新建关系时，在同一事务记录不可变身份绑定：subject_id、relationship_id、entity_type、entity_key、来源类别、证据引用和规范化摘要；有来源事件时核验同主体及对应实体，不能只保存任意事件 ID。
- 独立于已有权重修订建立身份见证，不通过重算旧 state_hash 篡改历史。数据库写入边界阻止更换绑定；读取与 mind.state 同时核验，避免只靠触发器。
- 昵称不是主体身份；昵称变更另有明确修订规则，不能借改名变更实体归属。
- 清点 RelationshipStore 的 get/list/create/update、MindEngine、social context、私有消息路由、导出、恢复、迁移、保留/删除等消费者。来源实体被撤销或删除时不能自动把信任转移给替代者。

### 3.3 旧数据策略：先证据盘点，再决定兼容行为

将历史分为：可追溯并能确认原始实体；证据冲突；仅能观察当前值但无法证明历史。后者最多记作 legacy-observed-baseline，不能称为原始来源证明。不得自动清空关系、重建关系、给当前 entity_key 继承全部信任，或将当前值重新散列后当作已验证历史。

建议保留所有旧权重/修订供追溯；冲突或不能确认归属的关系不用于自动跨实体信任授予，给出明确可解释状态。是否以及如何限制缺证据的旧关系继续参与正常社交，需先用迁移夹具和只读统计量化影响，再确定兼容策略；不能无提示地让大量既有关系失效。

### 3.4 验证矩阵

1. Alice/Bob 同昵称、不同类型、不同主体；单字段实体键/类型/主体变化必须拒绝或检出。
2. 正常权重修订、昵称变更、撤销与删除不破坏身份见证；并发创建/更新维持事务一致性。
3. 旧库三类证据分别迁移；冲突不得自动背书；中断后重试幂等；历史 state_hash 与修订不改写。
4. Mind.state 与 social 路由对不一致给出相同结论；读缓存、导出/恢复/迁移不能绕过。
5. 外键/保留删除规则及旧证据不可用路径不扩大私有正文访问；普通用户无新增维护/修复权限。

实施条件：完成旧数据证据分布和所有读取者清单；明确旧库兼容策略、迁移版本及回退/前滚方案；迁移夹具通过。当前为“设计与验证方案完成，实施条件尚未全部满足”，本次不改 schema 或产品代码。

## 4. R03 设计（本次不实施）

### 4.1 根因与 R02 的界限

当前顺序是 receipt → trust → previous → current，只有单文件原子发布，没有整组持久提交/恢复。A→B 且撤销 A 后，current 替换失败可能留下旧 current、已撤销 A 的 trust 和被覆盖的 previous。R02 只拒绝目录/链接及防止移入目录，不解决这一事务问题。

### 4.2 拟议状态协议与待决策点

推荐采用受 lifecycle 锁保护的持久 generation/intent 协议：PREPARED → COMMITTED → CLEANED；任何阶段重启均先恢复，不能边猜测边 start/rollback。

- PREPARED 保存经过核验的新包、实际 signer、目标信任策略、旧/新选择、恢复所需摘要和意图 ID；先准备恢复信息，再改变权威选择。引用路径必须为受控版本段，不能让 intent 成为任意文件操作接口。
- 优先评估“单个 generation 状态文件同时绑定 selected、previous 和信任策略摘要”作为提交点；独立 current/previous/trust 若继续存在，应明确是兼容镜像还是权威数据，不能出现两个可冲突的真相。
- 新意图与旧未完成意图冲突必须明确拒绝；同意图重试幂等；intent 损坏/伪造/路径不安全时 fail closed，不能任意恢复旧信任。
- 撤销必须先有明确语义。若操作者要求立即撤销 A，即使 B 激活失败，也不能恢复 A；可以进入明确的阻断/待恢复状态并保留恢复信息。若信任轮换只在整组提交时生效，应明确记录该约定，不能把它伪装成立即撤销。
- 兼容旧安装与旧控制入口、状态迁移后的降级/回滚路径尚需验证；不得通过永久信任 A+B、无条件恢复旧 allowlist、忽略真实 signer 或放宽签名校验换取可启动。

### 4.3 故障注入矩阵

对 A→A、A→B（保留 A）、A→B（撤销 A）、严格/旧版安装、同版本重试分别检查：

1. receipt、intent、trust、previous、current/权威 generation 每一步写入前后失败。
2. 文件允许读但不允许替换、拒绝读、权限/I/O 错误、进程 kill/重启。
3. 恢复前再次崩溃、重复恢复、错误包/错误意图重试、intent 缺失/损坏/非法路径。
4. start/rollback/status 在 pending/quarantine 下的行为一致；旧入口不绕过新状态。
5. 已撤销真实 signer 仍被拒；包内声明不自授权；签名原始字节、回执、R02 路径类型检查保持有效。
6. 当前/previous/运行版本不被清理；DataRoot 不被安装失败或回滚删除。

实施条件：撤销时点语义、权威提交格式、旧入口兼容和崩溃恢复模型明确；原生 Windows 的每个失败点均有可执行测试。当前为“设计与验证方案完成，关键协议决策和原型验证未完成”，本次不改 Windows 协议。

## 5. 交付与后续

先提交本设计，再实施 R05；实施/审计结果写入独立记录。R05 本地通过后保持工作区干净并汇报，不自动开始 R01/R03。下一步优先做 R01 历史来源与迁移兼容验证；R03 保持独立，不与 R01 混改。

R05 的完整服务退出保证限定为受支持生命周期与可信 provider 接口；不能用该修复承诺所有异步客户端、任意插件或整个进程退出都具有统一硬期限。既有 Windows 长路径条件不在本次修改范围；测试使用仓库外短临时根，不屏蔽断言。

设计依据：本地代码、原审计探针与 Python 的 executor 生命周期合同（官方文档：`https://docs.python.org/3.12/library/concurrent.futures.html`）。
