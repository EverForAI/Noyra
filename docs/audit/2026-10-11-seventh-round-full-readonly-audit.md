# Noyra 第七轮全项目逐代码级只读审计

审计日期：2026-10-11（Asia/Shanghai）  
分支：`codex/admin-optimization-20261009`  
审计基线：`f749a3aca51a3a9f8fd4530d954028c4b731b5a1`  
数据库合同：schema **92**

## 1. 授权范围、结论和口径

本轮只读审计针对当前工作区和当前提交执行。除本报告外，没有修改应用源码、跟踪中的测试、配置、依赖、数据库或部署文件；隔离探针和日志只写入被 `.gitignore` 忽略的 `.runtime`。没有访问生产主机，没有调用真实模型、搜索、QQ、云、签名器、区块链或资金账户，没有推送、合并或部署。聊天记录删除功能继续关闭，本轮没有恢复、扩展或模拟删除聊天及记忆。

审计清单覆盖 864 个跟踪文件，其中 `src/noyra` 213 个、`tests` 227 个、`scripts` 87 个、`deploy` 17 个、`.github` 4 个、`docs` 194 个；`src/tests/scripts` 的 425 个 Python 文件全部成功解析。源码体量为 135,643 行、3,694 个函数，这是清单计数，不表示每行均经过人工证明。按下述领域审阅实际调用链、SQL、事务、异常和边界条件，并结合整仓静态检查、回归和反例；不声称穷尽所有未知缺陷。

当前确认 **7 项未关闭问题或验收缺口：1 项 P1、5 项 P2、1 项 P3；没有足够证据支持 P0**。E03 已在上一轮修复并关闭；A03、A06、A07、A08、E01 继续保留；本轮新确认 A09、A10。

| 编号 | 问题 | 风险等级 | 修复风险 | 建议 | 当前归因 |
| --- | --- | --- | --- | --- | --- |
| A03 | embedding 超时后不完整响应头仍占用共享同步 HTTP 工作槽 | P2，可用性 | 中至高 | 近期优先；出现慢端点或槽位堵塞时立即安排 | 旧缺陷，未由 E03 引入 |
| A06 | 公共知识同步完成围栏反复扫描同主体全部历史 | P2，增长型可用性 | 中 | 大历史或多 peer 环境优先 | A04 引入的性能回归 |
| A07 | 记忆向量没有绑定内容版本和 embedding 资源空间 | P2，认知正确性 | 中 | 切换模型/资源或修订已索引内容前修复 | 旧缺陷，早于 E03 |
| A08 | 非 ASCII 图片版本参数和部分入站凭据触发未捕获 TypeError | P3，错误处理 | 低至中 | 可安排在输入健壮性模块 | 旧缺陷，A05 范围之外 |
| A09 | 私密聊天消息投影对每条消息扫描模型调用历史 | P2，管理台可用性 | 中 | 近期修复，历史增长前完成 | 早于 E03 的旧缺陷 |
| A10 | 通用主体 ID 合同与迁移 wire/enrollment 合同不一致 | P2，迁移兼容性 | 中至高 | 迁移启用前必须决策并修复/明确拒绝 | 本轮新确认 |
| E01 | 当前精确 SHA 的八项真实环境签名验收证据未取得 | P1，发布阻断 | 操作中至高 | 正式发布前必须完成 | 环境验收缺口 |

**概率口径。** 本报告不编造生产百分比。对每项分别说明：在给定代码条件下反例是否确定成立，以及这些条件在真实部署中的频率目前是否未知。性能数值来自 Windows 隔离数据库/loopback 探针，不是生产 SLA。测试通过不等于真实 Linux、外部平台或长时间稳定性已经通过。

## 2. 本轮方法与验证

- 基线、分支、schema 和受跟踪文件清单由 `.runtime/seventh-readonly-inventory.json` 固定。
- `.runtime/locked-venv/Scripts/python.exe` 为 Python 3.12.10；75 个锁定包版本差异为 0，`pip check` 通过。
- 全根 Ruff check 通过，Ruff format 检查通过（627 个文件已格式化）；Mypy 408 个文件通过。
- `admin.js`、`app.js` 语法检查通过；Node 前端 69 项通过、0 失败、0 跳过。
- base、cloud、dev 三组锁的 pip-audit 均返回 `No known vulnerabilities found`。这是公开漏洞数据库当前结果，不是未知漏洞不存在的证明。
- 本轮新增边界回归：私密聊天、删除禁用、schema71 迁移、认证输入、HTTP/运行时日志、cloud 兼容、CI 合同和迁移围栏等合计 **125 项通过、0 失败、0 跳过，350.14 秒**；日志为 `.runtime/seventh-boundary-regression.log`，退出码为 0。
- 不重复启动耗时整仓回归。上一轮对精确应用/测试 SHA `eea7e1b` 的干净工作区证据仍适用于当前源码和测试树：**2433 passed、56 skipped、259 subtests passed、0 failures/errors，4698.89 秒，退出码 0**。当前 `f749a3a` 相对该 SHA 仅增加审计结果文档，`src`、`tests`、`scripts`、`deploy`、`.github` 树相同；不能把这写成在文档提交上重新运行了一次整仓。
- 56 个跳过均是 Windows 无法提供的 POSIX 权限、Linux root/服务账户、Bash、symlink 或 8.3 夹具；不代表这些真实环境门禁通过。

Ruff 的文件数量来自工具输出，包含全根扫描到的历史探针/构建脚本，不能与受跟踪 Python 文件数相加。静态检查日志为 `.runtime/seventh-ruff-check.log`、`seventh-ruff-format.log`、`seventh-mypy.log`；Node 日志为 `seventh-node.log`，三组漏洞扫描日志为 `seventh-deps-base.log`、`seventh-deps-cloud.log`、`seventh-deps-dev.log`。环境清单首次读取 Git 中文文件名时曾遇到 GBK 解码错误，启用 `PYTHONUTF8=1` 后完成；这是忽略审计工具的问题，没有修改产品。

| 依赖锁 | 当前 SHA256 |
| --- | --- |
| `requirements.lock` | `81d242aa1cbb5b4a5c3c0354e6b1f5ef37d901809fe01700bc14a1f1fd06b78f` |
| `requirements-cloud.lock` | `4aafa2bedfc749e3c82964ffe3fa8807e7b4cfd8017fe88d70dbdd2b0fd5e7b0` |
| `requirements-dev.lock` | `e93467e673d93a4464b909526798a98551cede0b035403946b680cf694c7ef31` |

## 3. 覆盖矩阵

| 领域 | 代码/合同重点 | 结论 |
| --- | --- | --- |
| HTTP、服务和鉴权 | 总预算、并发 gate、DNS/TLS、Bearer、CSRF、Unicode 输入、访问日志 | typed 网络拒绝和管理输入目标保持；A03、A08 保留 |
| 管理台和公开页 | `admin.js` 请求 generation、刷新、消息状态投影、公开图版本 | Node 69 项通过；A09 是消息状态查询放大，A08 是公开图非法输入 |
| 私密交流 | subject/conversation/principal 归属、原文保持、删除关闭 | 删除仍拒绝；本轮没有增加删除路径 |
| 模型、资源和账本 | lease、重试、unknown、预算、账单和模型调用 | 未发现本轮新增预算或计费绕过 |
| embedding/记忆 | revision、向量 integrity、provider namespace、rebuild | A07 反例成立；A03 的原始 I/O 生命周期成立 |
| 公共知识 | cursor/ETag/ABA、迟到结果、同步历史、SQL 计划 | 状态围栏有效，但 A06 线性历史成本成立 |
| 生命周期、学习和完整性 | admission epoch、后台任务、检查预算和状态转换 | 当前边界回归通过；未做生产 soak |
| 钱包和采集 | dispatch、nonce、unknown、RPC deadline | 代码合同审阅通过；A03 类 RPC 原始 I/O 未另作反例，不扩大结论 |
| schema、保留、导出和恢复 | schema71→92、触发器、容量、训练/归档所有权 | schema 迁移/恢复测试通过；聊天删除和历史来源限制保持 |
| 迁移与升级 | fence、恢复、activation、主体 ID、发布 SHA | A10 合同不一致；E01 外部证明缺失 |
| CI、锁和依赖 | 全根静态检查、三组 lock、Node、SDK 门禁 | 本地静态/依赖通过，Linux/远程八门禁未执行 |

## 4. 未关闭问题详述

### A03：embedding 超时后原始读头 worker 不退出

**风险：P2 可用性；修复风险：中至高；置信度：高。建议近期优先。**

`src/noyra/model/embedding.py` 创建默认 `PublicDNSHTTPTransport()` 时没有把 raw-I/O `remaining` 回调传入。`src/noyra/core/http.py` 的 `SyncHTTPTimeoutHook` 只有在完整响应头返回后才注册 response；不完整响应头阶段由 httpcore/socket 持续读取。外层 `call_sync_http_with_deadline()` 可以按调用者预算返回，但四槽 `_SYNC_HTTP_GATE` 只在 worker 真正退出时释放，因此调用者的 timeout 不是底层工作终止证明。

隔离探针 `.runtime/seventh-http-header-lifetime-probe.py/.log` 用四个真实 loopback socket，每 20ms 续发一个不完整响应头字节。仅在独立探针进程将 DNS/TLS 替换为 loopback，实际 HTTPX/httpcore/parser/socket 保留，故不是生产公网/TLS 保护失效的反例。四个 embedding 调用约 2,015ms 后都返回 `embedding_timeout`，调用者返回后仍有 **4 个** `noyra-sync-http` worker；一个无关同步操作得到 `TimeoutError`，清理服务器后 worker 为 0。字段 `all_sent_before_deadline=false` 实际记录的是 1.5 秒同步屏障等待结果，不能夸大为所有请求均在预算前发送；稳定结论是 caller 返回后 worker 仍活且可耗尽共享槽。早期顺序探针只有 3 个 worker且无关操作成功，该真实结果保存在 `seventh-readonly-http-probes.log`，说明时序影响结果，但不否定并发反例。

影响是无关的 knowledge/RPC/signer 等同步消费者可能排队超时。已发送 embedding 的 unknown 预留、最终授权复核和公网地址固定必须继续保留；本轮没有证明重复发送、额外扣费、SSRF 或进程崩溃。

触发条件是已配置端点持续发送少量不完整响应头、并发达到四个且调用预算先返回。给定条件下确定可耗尽；正常完整响应时不触发此反例，供应商/网络故障频率未知。修复必须把 DNS、连接、TLS、每次 raw read/write、正文和取消都纳入不可变请求级预算，并保证 worker 有界退出；不能扩容 gate、提前释放槽、杀线程或自动重试已发送调用来掩盖问题。验收应覆盖四个慢头、后一无关调用、连接/解析/TLS/正文/取消、发送前 known-unsent 与发送后 unknown 账务。中至高修复风险来自共享 transport、并发请求级 deadline、取消/终态时序；不能将一条请求的 mutable deadline 污染共享 client。

### A06：公共知识完成代次反复扫描全部同主体历史

**风险：P2 增长型可用性；修复风险：中；置信度：高。建议在大历史或长期运行前修复。**

`src/noyra/knowledge/common.py:1069` 的启动快照和 `:1688` 的 `_assert_sync_peer()` 以 `COUNT(*) WHERE peer_id=? AND subject_id=?` 表示完成代次。同步过程会在 checkpoint、逐事件导入及结束时重复复核。`core/database.py` 现有索引以 `subject_id` 为前导，而 `common_knowledge_sync_runs` 是 append-only，因此 SQLite 先扫描同主体全部记录，再过滤 peer。

`.runtime/seventh-peer-generation-probe.log` 在 schema92 临时库中加入合法签名完成记录：无关历史 0/1,000/10,000/60,000/120,000 行时，COUNT 中位数约 0.005/0.072/5.687/33.17/76.901ms，平均 VM 步数约 1,800/6,800/51,800/301,800/601,800；20 个事件同步在 120,000 行时执行 47 次 COUNT、约 4,138ms。执行计划明确只使用 `subject_id` 索引。临时加 peer/subject 覆盖索引可降低无关 peer 放大，但本 peer 自身历史仍增长，故仅加索引不是完整修复。

影响是同步在 `BEGIN IMMEDIATE` 中延长 writer 锁持有，可能拖慢正常 tick、其他 peer 和认知/钱包/投递工作。给定大历史条件下扫描是确定的；达到生产影响的规模取决于历史量、频率、磁盘和并发，暂无生产统计。修复应使用持久化、有序、可恢复的 completion token 或严格索引，并保留同游标、固定时钟、ETag ABA、迟到成功/失败历史和 import/cursor 原子性；不能直接删历史、绕过 fence 或把失败记录伪装成成功。

修复风险来自新代次的初始化、所有完成 writer、恢复和 CAS/ABA 顺序。验收需要分别扩大本 peer 和其他 peer 的历史，核对查询工作量不随累计完成数线性增长；覆盖同游标/相同 ETag/固定时钟、迟到成功/失败、跨 store 并发、回滚及旧 schema 初始化。由 `9edb36a` 的 A04 修复引入，本次 E03 未触及该路径。

### A07：向量未绑定内容版本和 embedding 资源空间

**风险：P2 认知检索正确性；修复风险：中；置信度：高。建议在切换模型/资源或修订已索引内容前修复。**

`src/noyra/mind/retrieval.py:80,110,142,184` 的索引写入、missing 和读取没有绑定生成该向量时的内容 hash；`mind/memory.py:190-222` 修订内容后也没有使旧向量失效。`model/embedding.py:222` 的 provider 名称仅为 `openai-compatible:{model}`，未包含资源空间/endpoint/维度。schema 中没有源内容 hash 和非秘密 embedding namespace 的联合合同。因此内容被修订或同名模型切换 endpoint/维度后，向量自身哈希仍有效，但已不代表当前内容或空间。

`.runtime/seventh-memory-revision-probe.log` 的合法 sequential revise 和 revise-during-embedding 反例显示当前 revision 为 2、missing 为空、integrity rows 仍通过，但相对于最新内容的旧 energy similarity 为 1.0（期望为 0.0），旧 batch 可写回。`.runtime/seventh-embedding-space-probe.log` 显示相同模型别名在不同 endpoint/维度时名称相同，2→3 维变化没有触发 rebuild；同维不同资源也可继续使用旧空间。探针只使用 mock provider，没有真实付费调用。

影响是模型切换或内容更新后静默返回陈旧/错误相似度，属于认知正确性而非简单缓存延迟。触发在合法修订、embedding 在途或资源 endpoint/维度改变时确定；这些操作的真实频率未知。修复需绑定规范化内容 hash、维度、provider/resource 的非秘密 namespace，读写均做 revision/CAS，旧索引采用显式 stale/missing 策略，并限制重建的预算、并发和实际费用记录；不能批量无预算重建、删除聊天/记忆或把 integrity 通过误称语义新鲜。

修复风险来自旧 schema 向量失效策略、在途生成的写回和重建成本，不可把真实已消费调用记为未发送。验收应包含顺序修订、生成中修订、同模型别名不同 endpoint、2→3 维与同维异空间、跨主体隔离、旧索引恢复、无资源回退和有界收费重建。没有确认 E03 引入该问题。

### A08：非 ASCII 不可信输入触发未捕获 TypeError

**风险：P3 错误处理和排障；修复风险：低至中；置信度：高；可暂缓至输入健壮性模块。**

`src/noyra/service.py:9429` 的公开 hero `v` 参数以及 `src/noyra/interaction/inbound.py` Telegram/Feishu 分支将未经格式/字节验证的字符串送入 `hmac.compare_digest`。ASCII 错误输入能正常返回 200 或认证失败，但 percent-encoded 中文/重音字符会触发 TypeError，loopback 连接出现 `RemoteDisconnected`；合成 Telegram/Feishu Unicode 凭据同样得到 TypeError。后续 health 仍为 200，没有证据表明进程永久崩溃，但非法输入得到错误的 5xx/断连而非稳定 400/401。

证据在 `.runtime/seventh-nonascii-boundary-probe.log`。A05 只覆盖管理登录、Bearer、actor、CSRF 和退出五处，上一轮没有改这些旧入口。触发条件是构造请求或误编码输入，给定输入确定触发；真实频率未知。修复要按各协议验证 ASCII/bytes/长度后再 constant-time 比较，非法输入映射原有 400/401，保留时间窗、签名、去重和绑定，不做全局粗暴吞异常，也不能用 Unicode normalize 把非法值变成有效值。

公开图版本不匹配应保持原 `no-cache`，可以先验证预期的16位小写hex；不能把它当作凭据鉴权。验收覆盖正确/错误 ASCII、Unicode、空/超长/重复 v 参数、有效平台签名与非法凭据，以及随后正常请求可用。低至中修复风险在于误改平台签名字节协议或合法输入，而不是对 TypeError 统一返回“成功”。

### A09：私密聊天消息投影对每条消息扫描模型调用历史

**风险：P2 管理台可用性；修复风险：中；置信度：高；建议近期修复。**

`src/noyra/interaction/conversations.py:294-304` 为返回的每条消息单独执行：

```sql
SELECT status FROM model_calls
WHERE subject_id=? AND substr(purpose,1,?)=?
ORDER BY created_at DESC, call_id DESC LIMIT 1
```

`substr(purpose,...)` 的前缀比较在当前索引下不能做按消息定位；查询计划只显示按 `subject_id` 使用 `idx_runtime_logs_model_calls_keyset`。接口默认返回50条、允许最多100条，条数上限没有限制 SQL 扫描工作。没有找到匹配调用或匹配较老时，工作量趋近“消息数 × 该主体模型调用历史”。`src/noyra/web/admin.js:806-807` 默认在活动、可见、无搜索/状态过滤的聊天中约每10秒轮询（失败退避，历史页不轮询），会重复此工作。

`.runtime/seventh-conversation-capacity-probe.log` 使用当前 Database、IdentityStore、ConversationStore、InteractionStore、50 条合法合成消息及 0/1,000/10,000/60,000/120,000 条无关失败调用。每次都是 50 次状态查询；VM 步数/耗时为 11,000/16.083ms、311,000/27.503ms、3,011,000/527.265ms、18,011,000/3028.918ms、36,011,000/6081.921ms。所有状态均正确为 `None`，因此这是确定的查询放大，不是当前数据丢失。`git blame` 显示该投影来自 `461a6565`（早于 E03）。

影响是聊天窗口打开、历史翻页和刷新变慢，多个请求叠加还会占用数据库/HTTP 处理时间；不能直接把本机6秒写成生产固定延迟。120,000行低于默认250,000受保护证据容量上限，不能以“不合法历史量”排除。给定缺失/较老消息调用和同主体大历史时，扫描增长确定；正常新消息快速匹配时较轻，真实消息分布、增长周期和多人并发频率未知。无模型资源、被拒绝/尚未生成调用的消息均可能没有匹配记录。

修复应增加显式 interaction↔model call 关系或可证明的批量查询，保持 subject 隔离、精确 purpose 语义、最新调用排序和旧数据迁移策略；不能删状态字段、伪造 model call、把私密原文关系与回复调用关系混为一谈。验收需用无关历史 0/10万/百万、50/100 条消息、并发轮询及缺失/迟到状态验证查询计划和总时间预算。

修复风险为中，涉及历史调用归属、重试的最新状态语义和索引/迁移；粗糙 purpose 前缀拆分可能串联其他消息，批量扫描若仍反复遍历全历史也不能称容量问题已关闭。百万级验收可采用明确调整容量的隔离库，不推断默认生产可写百万行。

### A10：通用主体 ID 与迁移 wire/enrollment ID 合同不一致

**风险：P2 迁移兼容性；修复风险：中至高；置信度：高（接受与拒绝的合同冲突已确认）；建议在不兼容主体启用迁移前修复，默认 `Noyra-0001` 且迁移关闭时可暂缓。**

根合同 `src/noyra/core/identity.py:10-27` 接受 3–128 个 ASCII 字符（字母、数字、`_`、`-`，首字符不能是符号，并排除 Windows 设备名）。`ServiceSettings:795,955` 加上8字符下限并复用该校验；`MigrationPolicy.default():52` 也复用 `validate_subject_id`。但 `src/noyra/migration/agent.py:75,343,434,1349` 与 `src/noyra/migration/activation_contract.py:48` 强制 `Noyra-[A-Za-z0-9_-]{1,120}`，意味着总长度最多126且必须有 `Noyra-` 前缀。正常 enrollment、binding、restore 即会拒绝；即使走到 `http_executor.py:475-497`，从任务通用 `subject_id` 构造的 activation request 仍会拒绝。

`.runtime/seventh-migration-identity-probe.log` 对四个纯合成 ID 显示：`Noyra-0001` 在服务、启用策略、target enrollment 和 activation 全部接受；`audit-subject-001` 及长度 127/128 的 `Noyra-...` 均被 target enrollment 和 activation 拒绝，但被通用身份、ServiceSettings 和 MigrationPolicy 接受。没有真实迁移、网络或密钥调用。

影响是合法已运行主体无法通过目标 enrollment/恢复/激活，启用迁移策略却没有可执行的完整路径。HTTP executor 在 `:353` 获得 source fence 后才传包并调用 restore，因此缺少早期限制的执行路径还可能在隔离/传包之后才失败；该下游影响来自代码时序审阅，**未做端到端迁移或持续停机反例**，不宣称本轮已出现源停机。

这不是安全边界放宽理由：迁移 wire 合同可能有意限制为 Noyra 前缀，但这种限制没有在通用主体合同或迁移文档中明确。触发条件是主体使用无前缀通用格式或127–128长度，并进入迁移；给定条件的合同拒绝确定，部署中这类 ID 的比例未知。修复需先作兼容性决策：若迁移应支持所有合法主体，应抽取共享安全 validator 并保留字符/路径/长度边界，在 agent、activation、payload、恢复和测试中一致；若迁移明确只支持前缀，应在策略启用/提案前拒绝并在文档/API 显式报告，不要等到 source fence 或远端副作用之后才发现不兼容。

修复风险来自特权 root 激活、身份与genesis/签名绑定、旧备份/历史兼容和两机 wire 合同；不可仅放宽一个 regex。验收应覆盖8/126/127/128长度、无前缀、Windows保留名/分隔符拒绝、目标 enrollment、binding、activation、恢复和旧数据，并确认不兼容请求在源围栏/传包前退出。不得通过重命名持久主体、修改主体历史或放宽路径/加密绑定解决。对应前缀校验至少可追溯到 `ae15b827`（2026-10-02 enrollment）与 `8cabe7f8`（2026-10-05 activation），早于 E03。

### E01：精确 SHA 的真实环境八门禁证据未取得

**风险：P1 发布阻断；修复风险：操作中至高；置信度：证据缺口明确。**

本机 `artifacts/release` 的141个JSON、`output` 的26个JSON中，没有找到绑定 `f749a3aca51a3a9f8fd4530d954028c4b731b5a1` 的八项真实证据、独立审阅和签名产物，本聊天也未提供。没有访问远程证据库或GitHub私有运行，不能断言所有外部地点均无证据。本机 `.runtime` 证据不能替代 Ubuntu/systemd、加密卷、备份恢复、双机 migration fence、signer/KMS、测试链 nonce/reorg、HTTPS proxy 和规定时长 soak。`core/external_gates.py`、`core/release_assurance.py` 与 `.github/workflows/release.yml` 应保持同SHA、签名、独立审阅和适用的新鲜度校验。

缺少证明会阻止正式发布自动化，但不表示已发生生产事故。正式发布时需在隔离环境取得当前候选 SHA 的逐门禁证据、签名、独立审阅和新鲜度；不能复用旧 SHA、旧 8 小时 soak、伪造 `passed` 或放宽门禁。真实执行仍受环境授权限制，本轮不解除该阻断。

触发条件是发布流程或受保护的生产自动付款启用时缺少有效的同SHA证据，既有保护应确定拒绝；绕过之后的事故概率未知。本项不是所有开发功能都无法启动，也不是有效安装仅因时间流逝就应停机。修复操作风险来自隔离主机/root权限、双机切换、恢复、测试链/签名器及凭据处置，应先准备恢复方案，使用非秘密artifact引用、Ed25519签名、不同执行者/审阅者和当前候选SHA；发布记录72小时新鲜度与已安装运行时的时间语义不能混淆。

| 门禁 | 验收目标 |
| --- | --- |
| `ubuntu_systemd` | 当前SHA实际安装/升级、服务启动/停止/重启、权限、失败回滚 |
| `encrypted_volume` | 真加密映射、未解锁启动保护、密钥可用和解锁恢复 |
| `backup_restore` | 加密备份认证、摘要、隔离恢复、schema与服务可用性 |
| `migration_fence` | 双机单活、source drain、目标证明、失回包、取消/回滚 |
| `signer_kms` | 独立签名器/KMS隔离、拒绝访问、轮换和恢复 |
| `reorg_nonce` | 隔离测试链nonce冲突、unknown、回执和重组对账 |
| `https_proxy` | 实TLS、可信代理头、鉴权/限速、全链日志隐私 |
| `soak` | 既定24/72h合同及逐样本、存储/WAL/生命周期证据；不是旧8h结果 |

## 5. E03 修复质量和回归归因

E03 的范围是 A02 typed 公网地址异常合同遗漏的三个旧测试断言。`eea7e1b` 只修改 `tests/test_m42_p2_06_p2_07_provider_security.py`：将三处宽泛 `OSError` 期待改为精确 `PublicEndpointRejected`，并检查其 `PublicAddressRejected` cause；DNS pinning、TLS hostname、redirect 阻断、单次发送、不可重试及发送前/已发送账务断言全部保留。应用源码、schema、依赖、配置和发布脚本没有变化。

网络专项 68 项通过，Ruff/Mypy/Node 通过；在干净、精确 `eea7e1b` 工作区运行的整仓为 2433 passed、56 skipped、259 subtests passed、0 failures/errors，耗时 4698.89 秒。当前 `f749a3a` 与该提交的应用/测试树一致，因此这些是可复用的精确 SHA 证据，不是文档提交上的新整仓运行。

**质量判断：良好且边界正确。** 修复了测试合同根因，没有改实现迎合旧断言，没有删除、跳过或宽化失败测试，没有改变公网策略、发送、计费或安全行为；没有证据表明 E03 引入新的应用连锁问题、鉴权扩大、隐私删除、重复外发或数据损坏。E03 本身是低风险测试修复，整仓门禁已闭环。

本轮确认的 A03、A06、A07、A08、A09、A10 都不是 E03 引入：A03/A07/A08 代码早于 E03，A06 由更早 A04 的 COUNT 设计引入，A09 由 `461a6565` 引入，A10 是迁移与通用身份的历史合同分叉。E03 没有掩盖这些问题，也没有把聊天删除重新打开。其“无新增连锁问题”结论受 Windows 隔离环境和未执行 E01 外部门禁限制，不能扩大为生产零风险。

## 6. 优先级和暂缓边界

1. **常规优先处理 A03，再处理 A09/A06**：固定 raw-I/O 退出合同、消息投影查询预算和有界持久化完成代次，分别专项复审，不混成一个容量补丁。
2. **A07 在embedding切换/内容修订频繁时提前**：需要内容hash/空间namespace、CAS和有界重建，必须覆盖在途/切换/成本边界。
3. **A10 在不兼容主体启用迁移前提前处理**：先明确支持范围并在源围栏/远端副作用前识别；默认主体和迁移关闭可暂缓，不因其特权路径复杂就单方面放宽合同。
4. **A08 可随输入健壮性模块处理**，但公开页面和 webhook 对不可信 Unicode 输入应保持稳定 4xx。
5. **E01 在任何正式发布、生产自动化或真实钱包/平台启用前必须完成**。它不能由本地测试或审计文档替代。

本报告不实施上述修复。聊天删除继续保持关闭；保留原文、其他窗口事实和历史来源限制，不猜测来源、不绕过触发器、不用伪装成功的删除或重建解决容量问题。

## 7. 证据索引

| 文件 | 用途 |
| --- | --- |
| `.runtime/seventh-readonly-inventory.json` | 当前 SHA、schema、环境、锁和源码清单 |
| `.runtime/seventh-boundary-regression.log/.xml` | 本轮 125 项边界回归及退出记录 |
| `.runtime/e03-eea7e1b-full.log/.xml` | 精确 `eea7e1b` 的整仓结果 |
| `.runtime/seventh-http-header-lifetime-probe.py/.log` | A03 并发不完整响应头和 worker 寿命 |
| `.runtime/seventh-peer-generation-probe.log` | A06 查询计划、历史增长和 20 事件同步 |
| `.runtime/seventh-memory-revision-probe.log` | A07 内容修订/在途向量反例 |
| `.runtime/seventh-embedding-space-probe.log` | A07 embedding 空间/维度反例 |
| `.runtime/seventh-nonascii-boundary-probe.log` | A08 公开图和入站 Unicode 输入反例 |
| `.runtime/seventh-conversation-capacity-probe.py/.log` | A09 50 条消息 × 12 万模型调用的查询放大 |
| `.runtime/seventh-migration-identity-probe.py/.log` | A10 通用主体与迁移合同冲突 |
| `docs/audit/2026-10-11-e03-remediation.md` | E03 最小 diff、专项回归和精确 SHA 整仓证据 |

报告提交只包含本文件；不表示任何代码问题已修复，也不解除 E01 或其他外部环境限制。
