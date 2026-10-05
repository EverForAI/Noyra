# Noyra 全项目只读审计报告（2026-10-05）

## 1. 审计结论

本次审计覆盖当前提交的各主要模块，采用重点源码审阅、跨模块合同核对、故障复现和测试证据复核，并核对了 GitHub `main` 的合并状态和质量流水线。这不是逐行形式化验证，也不能保证不存在尚未发现的问题。没有确认 P0 问题；迁移加密 bundle、目标身份与 endpoint 绑定、recipient PoP、钱包/凭据/signer 证明绑定、来源 fence、回滚状态机、loopback 默认监听、管理台会话与 CSRF 控制均已存在。

当前仍有以下事项不能表述为“生产验收完成”：

1. **管理台紧急暂停入口失效**。暂停/解除暂停接口均确定返回 400，不能修改付款策略。这是 P1 代码缺陷，不能归因于缺乏真实环境数据。
2. **钱包开关错误转换非布尔值**。管理员请求中 `automation_enabled: "false"` 会被当作 `true` 保存；JSON `false` 本身没有这个问题。
3. **同一发布 SHA 没有外部环境证据**。`6a1bf2a4f07063228456bc3dde054d579d887110` 的 GitHub quality 和 Pages 工作流通过，但 `external-gates` 对该 SHA 的运行数量为 0。Ubuntu/systemd、LUKS 重启恢复、备份恢复、双机迁移、独立 signer/KMS、真实链 reorg/nonce、HTTPS 代理和 24/72 小时 soak 仍没有本次审计可验证的证据。
4. **retention 生命周期仍不完整**。模型调用/尝试、研究搜索和行为日志被永久保留，且冷 payload 压缩没有持久游标；压缩失败也不会落入 `retention_runs` 失败记录。
5. **网页升级和高风险能力开关没有消费同 SHA 的发布证据**。正式 tag 发布会验证签名的 `external-gates.json`，网页升级却直接部署官方 `main`。现有认证和限额仍有效；这是发布保证的加固缺口，不是公网用户绕过认证。

共记录 13 项：2 项 P1（其中 1 项是实际代码缺陷，1 项是外部验收阻断）、8 项 P2、3 项 P3。具体修复风险、影响、触发条件及验证要求见下文。钱包配置还确认了“失败响应但策略已经提交”的事务问题（A13），应与开关、暂停入口一起修复。

本报告是只读审计；唯一新增内容是本文件。没有修改业务代码、测试、部署配置、生产数据或远程仓库。工作区中原有的未跟踪 Pelican 页面、计划、测试和 `output/` 保持不变。

## 2. 范围、基线与方法

- **时间**：2026-10-05，Asia/Shanghai。
- **代码基线**：本地分支 `codex/wake-after-clean-restart`，HEAD `43f41a7d3806c6a6124b466240d610880e71d049`；工作树代码与 `origin/main` 合并提交 `6a1bf2a4f07063228456bc3dde054d579d887110` 一致。PR #6 已合并。
- **数据库基线**：`src/noyra/core/database.py` 的 `CURRENT_SCHEMA_VERSION = 79`。
- **审计范围**：运行时、HTTP/管理台、身份与导出、完整性和 at-rest、retention、模型与搜索 provider、钱包和自动付款、迁移控制面/agent/target activation、升级 runner、Ubuntu/systemd、Docker、反向代理模板、CI/release 工作流和发布文档。
- **方法**：静态阅读源码和配置；检查跨模块状态边界；执行针对性测试、Ruff、mypy、compileall、站点合同和依赖一致性检查；使用 GitHub API 只读核对 PR、质量流水线和外部门禁运行记录。复现使用自动清理的临时数据库，不触及生产数据。
- **无法执行的范围**：本会话没有真实 Ubuntu/systemd、LUKS 设备、第二台迁移主机、独立 signer/KMS、真实链节点、Cloudflare/Caddy 生产代理或长期 soak 环境。因此这些事实不能由本地测试推断为已通过。

### 模块覆盖记录

| 模块 | 本轮重点检查 | 可支持的结论与限制 |
| --- | --- | --- |
| core / autonomy / cognition / mind / sleep / learning / world | admission/epoch、恢复、认知与模型调用边界、M41 targeted audit、同 SHA 全量 CI | 相关测试通过；不能证明长期真实认知运行结果或所有模型输出情况 |
| HTTP / 管理台 / 公开交互 | 认证、session/CSRF、线程/请求限制、钱包 HTTP 入口、验证码与页面合同 | 找到 A11-A13；UI 合同通过不能替代按钮的状态变化测试 |
| model / research / provider | secret 文件、ledger 预算、路由、健康状态、冷 payload 消费 | 找到 A02-A04/A08；真实 provider 可用性及成本计费需实测 |
| storage / export / integrity | schema 79 ownership graph、cold payload、压缩/解压预算、失败持久化、归档与备份边界 | 找到 A02-A04/A07/A08；真实 LUKS 和恢复由 A01 覆盖 |
| wallet | 配置/签名器、付款限额、未知交易恢复、Nonce/reorg、自动化开关和紧急入口 | 实际 HTTP 复现 A11-A13；没有操作任何真实钱包 |
| migration | 注册/PoP、recipient 加密传输、binding receipt、fence、activation/rollback、人工批准与拒绝间隔 | 针对性测试通过；双机单活、KMS 和重启故障尚缺真实证据 |
| capability / project execution | 文件路径授权、symlink/reparse 边界、生成内容接受规则 | 本轮未确认新的越权问题；未对所有模型生成程序做形式化证明 |
| deployment / upgrade / CI / site | Ubuntu 安装器、root runner、systemd/Docker/代理、tag 发布与 main 升级区别、静态站点 | 找到 A05-A07/A09/A10；quality 与 Pages 工作流成功，external gates 未提交 |

历史报告和计划用于识别已修复项，不能作为当前问题成立的唯一依据。新增缺陷以当前代码路径或本轮复现为依据；旧服务器命令输出仅用于说明 A06 的既有复现。

## 3. 风险评级口径

| 等级 | 含义 |
| --- | --- |
| P0 | 无门槛远程执行、重大机密泄漏、不可逆主体损坏或高概率未授权资金损失。 |
| P1 | 关键发布/恢复边界未建立，或可能影响资金、主体所有权、单活迁移和生产可用性。 |
| P2 | 容量、批处理、配置、状态一致性和运维可靠性问题，通常需要持续运行或特定故障才能触发。 |
| P3 | 文档、验证可重复性、诊断质量或低影响兼容性问题。 |

“修复风险”表示实施修复本身的风险：低为局部入口或文档改动；中为共享读取边界、游标及持久化协议；高为迁移、资金和恢复合同。触发概率区分条件满足后的确定性结果与日常发生频率；没有生产遥测时不编造统计百分比。

## 4. 问题总表

| ID | 问题 | 风险 | 修复风险 | 触发概率 | 建议 |
| --- | --- | --- | --- | --- | --- |
| A11 | 管理台紧急暂停/解除暂停接口必定失败 | P1（代码缺陷） | 低至中 | 合法、非重复请求下 100% | 立即修复，真实付款前必需 |
| A01 | 同一 SHA 缺少可验证的外部 release-gate 证据 | P1（发布阻断） | 高 | 当前 SHA 工作流记录为 0；缺证据时 tag 发布必定阻断，真实故障概率未知 | 立即作为发布阻断 |
| A12 | 钱包开关用 `bool()` 接受错误类型并可能反转意图 | P2 | 低 | `"false"` 字符串请求下 100%；正常 UI 布尔值不触发 | 与 A11 一起立即修复 |
| A13 | 钱包策略与操作幂等审计分开提交，失败响应可能已有副作用 | P2 | 中 | 第二事务失败时确定发生；日常频率未知 | 与 A11/A12 一起修复 |
| A02 | 高频模型/研究/行为记录没有可执行生命周期合同 | P2 | 中至高 | 有新增调用时行数确定增长；耗尽时间未知 | 小规模短期试运行可暂缓，长期运行前处理 |
| A03 | 冷 payload 压缩没有跨批次游标 | P2 | 中 | 旧记录数超过单批上限时 100% 复现 | 本轮代码修复中立即处理 |
| A04 | 压缩失败不写入 `retention_runs` | P2 | 中 | 正常运行低；磁盘、锁或坏 payload 时中 | 本轮代码修复中立即处理 |
| A05 | 网页升级及高风险功能开关没有消费同 SHA 发布证据 | P2 | 中至高 | 使用 main 升级时不验证该证据；误开放概率未知 | 生产自动化开放前加固 |
| A06 | 升级审计文档没有准备所需的开发工具环境 | P3 | 低 | 未准备开发 venv 时必定失败 | 建议立即改善；不阻断运行时 |
| A07 | 版本 release 目录没有明确的保留/清理策略 | P2 | 中 | 多次升级后概率高；耗尽卷的时间未知 | 长期运行前处理 |
| A08 | 解压文本默认无输出上限 | P2 | 中 | 高压缩比记录读取时确定绕过输出预算；实际 OOM 概率未知 | 本轮代码修复中立即加边界 |
| A09 | 公开投稿只有视觉验证码，没有非视觉替代流程 | P3 | 中 | 无法读取图片的访客必定受阻 | 公共产品验收前修复，受控试运行可暂缓 |
| A10 | 本地工作区未跟踪文件污染根目录验证 | P3 | 低 | 当前已发生；未清理时接近 100% | 发布时区分 clean checkout |

## 5. 详细发现

### A11：管理台紧急暂停/解除暂停入口失效

- **风险等级**：P1，资金操作的紧急控制路径失效；本轮未发生真实转账或资金损失。
- **修复风险等级**：低至中。根因集中在共享 HTTP 入口的 payload 合同，但修复必须确认认证、CSRF、策略版本和其他政策字段仍正确。
- **根因与证据**：`src/noyra/web/admin.js:1675` 的暂停按钮调用 `/api/v1/admin/wallet-automation/pause`。`src/noyra/service.py:6015` 的 `_set_wallet_automation_pause()` 将 `allowed_network_ids`、`allowed_asset_ids`、各种限额等当前字段追加到 payload，再调用 `_update_wallet_automation()`；后者只消费 expected version、reason、幂等 key、两个布尔开关和 mode，`:5988` 的剩余字段检查因此抛出 `ValueError("unknown fields")`。pause 和 resume 以及 v1/兼容别名使用同一实现。
- **复现结果**：在 loopback 临时实例中，以有效管理员 token、正确 policy version 和新幂等 key 调用 pause/resume，二者均返回 `HTTP 400 {"error":"invalid_wallet_automation"}`，`emergency_paused` 没有改变；没有加载 signer、没有访问区块链。与现有 UI 相同的最小 payload 也会由服务器追加这些字段。
- **影响**：管理员在需要阻止后续签名/广播时不能通过明显的紧急按钮暂停付款。底层付款引擎仍检查数据库中的暂停策略，因此不是整个暂停机制失效；缺陷在按钮/API 到策略写入这一段。已经广播的交易即使正确暂停也不能撤回，修复验收应区分这两种状态。
- **触发条件**：有权限的管理员点击暂停或解除暂停，策略版本有效，幂等 key 未被处理。
- **触发概率**：上述条件满足时确定失败；不依赖 Linux、LUKS、真实链或生产负载。重复幂等命中可能返回旧响应，但不会证明本次暂停成功。
- **建议**：立即修复并在真实付款前验收。让暂停入口只传递该共享入口支持的字段，或直接走统一且原子的策略更新函数；保护所有无关字段。增加真实 HTTP + 管理会话/CSRF 的 pause/resume、版本冲突、重复请求和付款引擎暂停拒绝测试。当前测试仅覆盖暂停字段/投影与引擎边界，没有验证此按钮对应的 HTTP 状态变化，CI 绿灯不能替代这条用例。

### A12：钱包开关的非布尔输入被错误转换

- **风险等级**：P2，已认证管理客户端的配置意图可能被反转；不是匿名攻击。
- **修复风险等级**：低。应修复入口类型合同而非改变正常布尔值行为。
- **根因与证据**：`src/noyra/service.py:5979` 和 `:5982` 先用 `bool(payload.pop(...))` 处理 `emergency_paused`/`automation_enabled`，再交给严格的 `PaymentPolicyInput`。错误输入已经被转成合法布尔值，Pydantic 无法再发现原始类型错误。Python 中 `bool("false")` 为 `True`，`bool(None)` 为 `False`。
- **复现结果**：同一无 signer 临时实例中发送 `automation_enabled: "false"`，接口返回 200，响应为 `automation_enabled=true`。该验证没有开启真实付款；当前策略仍为 disabled。
- **影响**：脚本、通讯工具或未来 UI 将布尔值序列化为字符串时，可能在意图关闭时开启自动付款；`emergency_paused: null` 也可能错误解除暂停。正常管理台使用 JSON 布尔值不触发，单笔/日限额仍然有效。
- **触发条件**：有效管理员请求带非布尔的 automation/pause 值和正确策略版本；实际付款还需要 automatic mode、signer 和合法订单。
- **触发概率**：对于已复现的 `"false"` 请求为 100%；实际客户端发生这种序列化错误的频率未知。
- **建议**：与 A11 同模块立即修复；接受且仅接受 `type(value) is bool`，明确拒绝 string、number、null、list、object。对非法请求验证策略版本、状态和审计数据都不发生变化，并验证 JSON `false`/`true` 的正常语义。

### A13：钱包策略与幂等审计不在同一事务

- **风险等级**：P2，管理操作的状态/响应和重试语义不一致。
- **修复风险等级**：中；涉及策略版本、审核记录、幂等 key 和并发请求，应在 store 边界统一合同。
- **根因与证据**：`src/noyra/service.py:5990` 调用 `WalletEconomyStore.update_policy()`，后者在 `src/noyra/wallet/economy.py:606` 的独立事务提交策略及 `wallet_payment_policy_updated` 审计；HTTP 入口随后在第二事务写 `wallet_automation_updated` 幂等记录。第二事务失败时，外层捕获异常并返回 400，第一事务无法回滚。幂等检查也不与最终写入原子绑定。
- **复现结果**：在临时实例中只对 `wallet_automation_updated` 注入异常，请求返回 400；策略却从 `automation_enabled=false/version=1` 变为 `true/version=2`，幂等审计行数为 0。没有加载 signer，disabled 付款模式保持不变，因此未产生资金操作。
- **影响**：客户端以为开关未修改，实际上已修改；相同 key 重试没有成功结果可重放，旧 expected version 又会冲突。底层策略审计仍存在，不能描述为全部审计丢失；缺的是该管理操作的完整幂等成功记录和一致的响应语义。
- **触发条件**：钱包策略事务成功，但随后幂等审计事务因数据库锁、磁盘/IO 错误或其他异常失败。
- **触发概率**：故障注入下确定复现；日常发生频率未知，在存储压力和并发管理请求下更需关注。
- **建议**：让策略变更、版本检查、幂等请求摘要和审计记录在同一事务提交；相同 key/相同请求重放结果，不同请求明确冲突。加入第二阶段故障、并发请求、旧版本和重复 key 用例，不仅断言 HTTP 状态码。

### A01：同一 SHA 的真实 release-gate 证据缺失

- **风险等级**：P1，发布阻断，不等同于已确认线上故障。
- **修复风险等级**：高；需要真实主机、隔离测试、独立 reviewer 和签名证据。
- **根因与证据**：`docs/release/external-gates.md:9` 固定要求 `ubuntu_systemd`、`encrypted_volume`、`backup_restore`、`migration_fence`、`signer_kms`、`reorg_nonce`、`https_proxy`、`soak` 八项 gate。`.github/workflows/release.yml:73` 起的流程校验精确 SHA、签名、freshness 和独立 reviewer。本次通过 GitHub API 核对，最新合并 SHA 的 quality job 和 Pages job 均成功，但 external-gates 对该 SHA `total_count=0`。这证明尚未在既定工作流提交该 SHA 的证据，不证明部署者从未在其他地方做过测试。
- **影响**：不能证明 LUKS 设备重启恢复、systemd 升级/回滚、双机单活 fence、独立 signer/KMS 隔离、真实链 nonce/reorg、HTTPS 代理和 24/72 小时存储水位。直接开放无人值守迁移或自动付款会把未验证的环境假设带入资金和主体状态机。
- **触发条件**：发布当前 SHA、将迁移改为 `policy_auto`/应急无人值守、打开生产自动付款或长期公网运行。
- **触发概率**：在没有 artifact 时，release workflow 阻断概率为 100%；真实环境故障概率未知。
- **建议**：立即保留为发布阻断。准备真实环境并产生同 SHA、独立 reviewer 和 Ed25519 签名的 artifact；验收前采用测试网、有限预算和人工迁移审批。解除这一项主要需要真实测试与证据，不是笼统追加功能代码；A03-A08 中的代码和运维改进是另行列出的事项。

### A02：高频记录没有可执行的生命周期合同

- **风险等级**：P2，容量、备份、完整性扫描和恢复窗口风险。
- **修复风险等级**：中至高；涉及外键、审计证据、导出和恢复契约。
- **根因与证据**：`src/noyra/core/retention.py:120` 起将 `model_calls`、`model_attempts`、`research_search_runs`、`action_deliberation_runs` 和 `behavior_logs` 标记为 `preserve` 且没有 cutoff。`src/noyra/model/ledger.py:91` 和 `:213` 为调用和尝试写入行。现有清理保留这些表的行和索引，冷 payload 压缩并不使历史行数有界。注册表已经存在，因此缺口是对高频证据实施有界保留/归档的策略，而不是完全没有表分类。
- **影响**：长期模型/搜索/认知运行会持续增长 SQLite 行、索引、备份、完整性扫描时间和恢复耗时；“必要审计证据”和“可聚合历史”没有逐表定义，磁盘水位不可预测。
- **触发条件**：启用模型、搜索、研究或行为功能并持续运行数月。
- **触发概率**：只要产生新的模型调用或尝试，相关行数增长就是确定的；“几天内耗尽”的概率和达到容量上限的时间无法从静态代码推算，需要真实调用量、单行大小和磁盘容量。
- **建议**：小规模、有容量监控的几天试运行可暂缓，长期运行前补齐分类表的归档/聚合/删除合同；保护付款、安全、迁移和因果/幂等证据，避免直接删除全部历史。验收应覆盖外键、重复调用幂等性、旧数据导出/恢复和加速 soak 水位。

### A03：冷 payload 压缩没有跨批次游标

- **风险等级**：P2，维护任务不完整并导致容量增长。
- **修复风险等级**：中；需要为压缩阶段引入独立游标或可复用的 keyset 条件，并补充回归测试。
- **根因与证据**：`src/noyra/model/ledger.py:413` 的 `compress_cold_payloads()` 每次执行 `ORDER BY created_at, call_id LIMIT ?`，已经压缩、很小或不可压缩的旧记录仍满足查询条件；没有持久游标或处理标记。`src/noyra/core/retention.py:463` 和 `src/noyra/core/storage_lifecycle.py:146` 都调用同一实现，因此两个入口都会受影响。
- **复现结果**：在临时 SQLite 中插入两条超过 90 天的旧模型记录，连续三次以 `batch_size=1` 运行 retention，结果为 `compression_runs=[1, 0, 0]`；第一条被压缩，第二条始终没有被处理。该复现未改变仓库或生产数据。
- **影响**：维护任务反复检查首批旧记录，后续符合条件的冷记录无法得到压缩，降低容量回收效果。压缩本身不是加密；本项不据此声称 API 密钥泄漏或 at-rest 保护失效。
- **触发条件**：旧终态模型记录数量大于 `batch_size`，且维护任务重复运行。
- **触发概率**：一旦超过批次上限为 100% 复现；在真实调用量较低时不触发。
- **建议**：为压缩阶段建立 `(created_at, call_id)` 游标或处理标记。只排除压缩前缀仍会被小型/不可压缩记录阻塞，不能作为完整修复。验证跨多批推进、不可压缩首批、相同时间戳、中断恢复和新增旧记录；保证哈希、账本和预算语义不变。

### A04：压缩失败不写入 retention 失败记录

- **风险等级**：P2，故障可观测性和重试状态不完整。
- **修复风险等级**：中；需要调整持久化边界，避免压缩游标和删除游标不一致。
- **根因与证据**：`src/noyra/core/retention.py:463` 在生成 `run_id` 和 `:521` 的失败处理 `try` 之前执行压缩。`src/noyra/service.py:9823` 的周期调用只记录异常类别；手动入口 `:5145` 没有独立覆盖该阶段。另一个边界不一致是 `:9704` 的“deletion-only”低空间恢复调用仍进入带压缩写入的 `run_batch()`；它没有复用 storage lifecycle 的写入空间预检。
- **复现结果**：在临时数据库中将 `compress_cold_payloads` 注入 `RuntimeError`，`failure_run_added=0`；也就是压缩抛错后没有新增 `retention_runs` 行。
- **影响**：管理台缺少该次失败运行；失败发生在删除之前，后续可回收聚合也不会被清理。磁盘压力下压缩 UPDATE/WAL 可能先失败，导致本应帮助恢复空间的任务停止；周期服务只给出类别日志，手动请求可能没有结构化错误响应。真实数据库完全不可写时也不能保证把失败再写进同一数据库，需要日志/状态回退。
- **触发条件**：冷 payload 压缩遇到磁盘满、锁冲突、损坏数据或实现异常。
- **触发概率**：正常运行低；资源压力或恢复操作时中等，精确比例未知。
- **建议**：让删除恢复与压缩写入分别受控，把压缩纳入可追踪的失败阶段；磁盘压力时跳过写放大阶段并继续安全的回收操作。补充压缩异常、空间不足、锁冲突和恢复重试测试；数据库不可写时返回结构化失败并留有脱敏日志。

### A05：网页升级和高风险功能开关没有消费同 SHA 发布证据

- **风险等级**：P2，发布保证与流程加固缺口。文档已经承认环境变量是操作员声明，不能把有权限的部署者修改配置称为越权漏洞。
- **修复风险等级**：中至高；需协调稳定版/开发版升级、证据有效范围和高风险功能开关，避免离线重启或正常升级被意外阻断。
- **根因与证据**：`src/noyra/core/upgrade.py:166` 查询 `/commits/main`；`scripts/upgrade-ubuntu-runner.sh:321` 验证 main SHA 后，`:360` 直接调用安装器，没有消费同 SHA 的 quality 状态、签名 gate artifact 或正式 release。`src/noyra/service.py:217` 的字符串检查仅覆盖自动生成悬赏的环境配置；`:5933` 管理台付款开关不校验外部证据，`:9782` 附近的 `wallet_rewards.execute_ready()` 也独立于这项环境配置执行。
- **影响**：正式 tag 发布被外部门禁阻断，并不能阻止管理台升级同一 main SHA；升级后原有付款/迁移策略可以继续存在。部署者容易把“官方 main + 健康检查通过”当成“当前版本已通过真实资金/迁移验收”。固定官方仓库、认证、CSRF、root 文件权限、限额与暂停保护仍然存在，本项不是无认证任意部署或转账。
- **触发条件**：通过网页部署没有完整外部证据的 main 提交，并据此开启或沿用高风险自动化策略。
- **触发概率**：现有 main 升级路径不验证该证据是确定事实；是否导致误开放或实际故障未知。
- **建议**：稳定升级通道消费已验证发布及证据摘要，开发 main 通道明确展示未完成的验收项；管理台展示当前证据对应 SHA 和功能覆盖范围。需要机器强制时，应定义独立的、签名的功能启用授权。当前 release evidence 的 72 小时新鲜度是发布时规则，不应直接变成线上每 72 小时停机/停止付款；本地钱包模式也应保留，不能借此强制所有部署使用 KMS。

### A06：升级审计脚本依赖不存在的开发 venv

- **风险等级**：P3，运维入口和文档的环境前提不完整；运行时和网页升级没有因此失效。
- **修复风险等级**：低；可在文档和入口预检中准备独立审计环境，或将静态检查与开发质量审计拆开。
- **根因与证据**：`scripts/audit-deployment.sh:33` 允许 `NOYRA_PYTHON` 覆盖，否则回退到仓库 `.venv/bin/python`，随后要求 pytest/ruff/mypy/coverage；`scripts/audit-release.sh:4` 硬编码开发 venv。`scripts/install-ubuntu.sh:817` 安装最小运行时依赖。`docs/deployment/ubuntu.md:536` 与 `remote-validation.md:71` 的部署指令没有在此之前准备相应开发工具。用户之前提供的服务器输出已复现缺解释器和缺 pytest；本轮未重新连接服务器。
- **影响**：管理台一键升级本身有独立 root runner，但运维按照文档执行审计时会失败，容易误判为代码故障或在生产机临时安装开发依赖，扩大变更面。
- **触发条件**：在按文档的标准 Ubuntu 主机上直接执行 `sudo scripts/audit-deployment.sh` 或 `scripts/audit-release.sh`。
- **触发概率**：未单独准备开发审计环境时必定失败；已经安装 dev venv 的开发机不受影响。
- **建议**：尽快改进入口和文档，缺依赖时给出真实可执行的隔离环境准备命令；不要把生产 venv 改成开发工具环境。验收包括纯运行时安装、独立 audit venv、非仓库当前目录调用和错误提示。

### A07：release 目录没有明确保留和清理策略

- **风险等级**：P2，反复升级后的磁盘耗尽和回滚不可用风险。
- **修复风险等级**：中；清理必须保留当前版本、上一可回滚版本、冷备份引用和正在运行的进程依赖。
- **根因与证据**：`scripts/install-ubuntu.sh:815` 创建 staging venv，`:849` 发布 release；`:768` 为旧部署生成冷备份。成功版本和备份没有数量/年龄保留政策或可用空间预检。`src/noyra/core/storage.py:354` 观察的是数据目录所在文件系统，不覆盖独立的 `/opt` 或 `/var/backups` 文件系统。
- **影响**：每次升级复制 Python venv 和依赖，长期会占用系统盘；磁盘压力可能在下一次升级、备份或服务启动前触发，且旧 release 越多越难判断可安全删除的版本。
- **触发条件**：持续执行多次一键升级，尤其是频繁开发版本升级。
- **触发概率**：升级次数达到系统盘容量阈值前为高；耗尽时间取决于 venv 大小和系统盘容量。
- **建议**：长期运行前增加 root-only、锁保护且可预览的 release/backup 保留策略；至少保护 current、previous、活动进程及明确留存的恢复点。备份删除应在存在已验证替代恢复点后执行。停止旧服务前分别检查数据、release 和备份文件系统的空间。验收必须覆盖独立系统盘/数据盘、删除失败、并发升级和恢复点保护。

### A08：`decompress_text` 默认无输出大小上限

- **风险等级**：P2，异常持久化数据导致进程内存/CPU 压力。
- **修复风险等级**：中；需要为不同 payload 类型建立上限，过低会破坏合法历史数据恢复。
- **根因与证据**：`src/noyra/core/payload_codec.py:38` 的 `decompress_text(value, max_bytes=None)` 在 `:57` 直接无上限解压。`src/noyra/core/integrity.py:3746`、`src/noyra/model/ledger.py:673` 和 `src/noyra/core/runtime_export.py:713` 等调用不传上限。SQLite 行预算只统计压缩字段的存储长度，不能替代解压输出预算。
- **复现结果**：在临时数据库中生成约 2 MiB、哈希一致的模型 request 并压缩后，用 `max_bytes_per_check=4096`、`max_value_bytes=4096` 单独执行 `periodic_deep/model.ledger`；结果 `status=ok`、`bytes_examined=3014`、`findings=0`。同一压缩文本显式调用 `decompress_text(..., max_bytes=4096)` 会正确拒绝。这验证了实际检查调用遗漏预算，而不是 zlib 上限 API 本身无效。
- **影响**：带有高压缩比的异常模型 payload 可在完整性检查或读取历史时产生远大于数据库字段的临时内存和 CPU；在恢复/审计边界会造成服务不可用，而不是干净地报告 `PayloadLimitError`。
- **触发条件**：数据库中存在异常或篡改的 `noyra-zlib-b64:` 文本，并触发模型读取、导出或 integrity check。
- **触发概率**：正常受控写入下灾难性膨胀的概率低；高压缩比记录被读取时，上限遗漏可确定复现。生产中是否存在这种记录及造成 OOM 的概率未知，不把此项描述成已证明的匿名远程攻击。
- **建议**：为模型 request/response、事件、研究和导出分别定义最大解压字节；所有读取路径显式传递 `max_bytes`，超过上限返回完整性 finding 并保持 fail closed。补充高压缩比、截断流和恢复导出测试。

### A09：公开投稿缺少非视觉验证码替代流程

- **风险等级**：P3，无障碍和公开投稿可用性问题。
- **修复风险等级**：中；替代方式必须保持防滥用能力，不能直接将答案放入文本。
- **根因与证据**：`src/noyra/web/index.html:40` 只有图片验证码和文本答案输入；`src/noyra/web/app.js:194` 要求 PNG data URL；`src/noyra/interaction/posts.py:471` 返回图片挑战，`:587` 的模式仅为 letters/digits/alphanumeric，没有音频或其他可访问挑战流程。图片 alt 说明用途，但没有提供可以完成任务的非视觉方法。
- **影响**：无法辨认图片的视障访客不能独立完成投稿；现有 label、live status 和键盘输入不能解决验证码的感官障碍。
- **触发条件**：访客使用屏幕阅读器或无法读取验证码图像，并尝试公开投稿。
- **触发概率**：该用户条件下确定受阻；用户群中占比未知。本轮未执行屏幕阅读器或浏览器无障碍验收。
- **建议**：公共产品验收前增加非视觉挑战或可访问的受控替代提交流程；沿用相同 TTL、限速、尝试次数和单次消费限制。验证键盘/屏幕阅读器与反滥用边界，不以把答案写入 alt 的方式修复。受控内部试运行可暂缓。

### A10：未跟踪用户文件污染根目录验证

- **风险等级**：P3，审计和发布结果不可重复。
- **修复风险等级**：低；文件属于用户，不能擅自删除。
- **根因与证据**：`git status --short` 显示已有未跟踪的 `docs/audit/2026-10-04-full-project-readonly-audit-followup.md`、Pelican 计划、`output/`、页面和测试。未限定路径的 `ruff format --check .`、mypy 或测试会把它们纳入结果；本次完整工作区 formatter 也曾命中未跟踪计划文件。
- **影响**：完整工作区与 clean checkout 的质量结果不同，可能误把用户实验文件当成发布代码，或掩盖提交基线的真实状态。
- **触发条件**：在工作区根目录运行未限定路径的静态检查、打包或发布脚本。
- **触发概率**：当前已发生；文件继续存在时接近 100%。
- **建议**：发布和审计记录明确区分 clean checkout 与本地工作区；CI 的结果作为提交基线权威证据。保留用户文件，不通过删除它们制造“干净状态”。

## 6. 已确认有效的控制（不重复列为问题）

- 迁移默认关闭；启用时未另选模式会进入人工审批，具备拒绝冷却、目标 allowlist、trust 评估和 proposal 过期。
- 目标注册使用 HTTPS、endpoint origin、短期 challenge、Ed25519 PoP 和 recipient X25519；bundle 使用 AEAD、manifest/分块 hash、nonce 和 AAD。
- 迁移回执绑定 target volume、credential、wallet 和 signer proof；local wallet transfer 需要显式 opt-in，默认优先 external signer rebind。
- 来源 executor 有 preflight、source fence、cutover、activation、rollback 和恢复路径；真实双机/LUKS/KMS 仍需 A01 证据。
- 管理台有 bearer/session、CSRF、cookie TTL、失败限速、状态审计和安全响应头；服务有请求体、响应、并发和网络 deadline 边界。
- provider/search 支持 secret 文件、健康统计、失败率/延迟窗口、冷却和路由模式；API key 不进入普通配置导出。
- 钱包底层具有全局开关、单笔/日/月限额、余额和 Gas admission、Nonce/确认/reorg 状态及暂停策略检查；管理台紧急暂停入口仍有 A11 缺陷，不能当作已验收控制。
- Ubuntu/systemd 和 Docker 默认 loopback；systemd 使用 `NoNewPrivileges`、`ProtectSystem`、`ReadWritePaths` 和严格 umask；升级 runner 使用 root-owned checkout、固定官方 `main` SHA、安装器备份/健康检查/回滚。
- 当前合并 SHA 的 GitHub `quality`（Ubuntu/Windows、Python 3.11/3.12、Docker、cloud-profile）和 Pages workflow 已通过。

这些代码控制不能替代 A01 的真实环境 evidence。

## 7. 本次验证记录

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| GitHub PR #6 | 已合并 | merge commit `6a1bf2a4f07063228456bc3dde054d579d887110` |
| GitHub quality | 通过 | [run 37220164057](https://github.com/EverForAI/Noyra/actions/runs/37220164057) 的 6 个 job 成功；含全量 pytest、Ruff、mypy、pip-audit、Docker 和 cloud-profile |
| GitHub deploy-pages | 通过 | [run 37220164038](https://github.com/EverForAI/Noyra/actions/runs/37220164038) 成功 |
| GitHub external-gates | 0 次运行 | 当前 SHA 没有通过既定工作流提交的外部 evidence |
| targeted M41 audit | `285 passed, 7 subtests passed` | 本地针对性运行；约 12 分钟 |
| migration/upgrade/retention targeted tests | `89 passed, 1 skipped` | Windows 跳过 POSIX ownership 检查 |
| tracked Python Ruff check | 通过 | 356 个已跟踪 Python 文件 |
| tracked Python formatter | 通过 | 356 个文件已格式化 |
| tracked Python/Markdown formatter | 通过 | 521 个已跟踪输入；排除了用户未跟踪计划 |
| tracked source/test mypy | 通过 | `Success: no issues found in 342 source files` |
| compileall | 通过 | `src` 编译无错误 |
| site contract | 通过 | 4 个双语页面、链接/资源/安全合同通过 |
| pip check | 通过 | 依赖元数据无冲突；不等于漏洞扫描结论 |
| retention compression repro | 发现缺陷 | `compression_runs=[1,0,0]`，第二条旧记录未被推进 |
| retention failure repro | 发现缺陷 | 注入压缩异常后 `failure_run_added=0` |
| 解压预算复现 | 发现缺陷 | 约 2 MiB 解压输出通过 4 KiB ledger audit；报告仅统计 3014 字节 |
| 钱包紧急按钮 HTTP 复现 | 发现缺陷 | pause/resume 均返回 400，策略不变 |
| 钱包布尔类型 HTTP 复现 | 发现缺陷 | `automation_enabled:"false"` 返回 200 并保存为 true |
| 钱包操作事务故障复现 | 发现缺陷 | 返回 400，但策略开启且版本递增；操作幂等记录未写入 |

本轮没有再次运行完整本地 pytest；全量提交基线使用同 SHA GitHub CI 证据，针对新增疑点使用临时故障复现补证。Windows 跳过的 POSIX 检查不能标成当地已执行。本轮没有浏览器截图、真实移动设备/屏幕阅读器或真机迁移测试，前端部分是源码与合同复核。复現时使用临时数据库和测试用 token，不含真实钱包或 provider 密钥。

2026-10-04 复核报告中的格式、mypy 和依赖扫描证据缺口已由当前已跟踪文件检查及同 SHA GitHub CI 消除；schema 79 ownership graph、目标 endpoint 绑定和 DNS 固定地址连接已经存在，本轮没有将这些旧问题重复列为未修复项。A02 仍是既有容量策略缺口；A03、A04、A08、A11、A12、A13 是本轮明确的本地代码复现。

## 8. 建议处理顺序

1. **立即修复 A11、A12、A13**：先恢复紧急暂停入口，严格校验布尔输入，统一策略与幂等审计事务；以 HTTP 到数据库再到付款引擎的状态变化作为验收证据。
2. **修复 A03、A04**：建立压缩游标、阶段失败记录和低空间删除恢复边界，覆盖多批、中断和故障注入。
3. **修复 A08**：把模型历史/完整性/导出解压输出纳入显式预算，并复核各认知消费者的兼容性。
4. **处理 A02、A07**：设计长期证据归档和 release/backup 保留合同，再做容量、恢复和并发 GC 验证。短期受控试运行不应为了容量而删除必要证据。
5. **改进 A05、A06、A09**：明确 main/稳定升级的验收状态，准备可执行的部署审计工具环境，补齐公开投稿非视觉流程。
6. **并行准备、最终完成 A01**：真实环境可以提前准备；签名验收结果必须对应代码修复完成后的最终发布 SHA。跑完八项门禁并独立复核后，再决定高风险能力是否开放。
7. **发布前处理 A10**：用 clean checkout 复核，不删除或自动提交用户实验文件。

目前准确的状态是“自动化质量检查通过，但审计发现仍有确定的代码缺陷，且生产外部验收未完成”。CI 绿灯不能解释为所有用户流程正确，也不能解释为资金与迁移功能已具备生产验收保证。

