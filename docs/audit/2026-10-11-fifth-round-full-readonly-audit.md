# Noyra 第五轮全项目只读审计

审计日期：2026-10-11（Asia/Shanghai）

分支：`codex/admin-optimization-20261009`

被审计应用提交：`649f3a042ae5eafd721714c6982c213e2145d9d6`

数据库合同：schema **92**

## 授权、范围与结论

本轮按最新的人类请求执行只读审计。没有修复应用、测试、配置或依赖，没有读写生产数据库、日志、凭据或账本；仅新增本报告。审计探针和日志存于忽略的 `.runtime`，使用临时数据库、合成数据、模拟连接和本地 loopback。没有真实供应商调用、平台消息、云账户访问、资金交易、推送、合并或部署。依赖漏洞扫描只查询公开漏洞数据，不调用项目供应商。

审计开始时分支与工作区干净。受跟踪清单包含 `src/noyra` **213** 个文件和 `tests` **224** 个文件；同时复核发布/安装/观察脚本、CI、依赖锁、Docker、代理模板、既有审计与修复证据。采用入口和跨模块合同审阅、差异审计、隔离反例、当前静态门禁、前端回归及跨模块 Python 回归。

**当前确认 6 项未关闭的问题或验收缺口：1 项 P1、4 项 P2、1 项 P3，未确认 P0。** 其中 A02/A03/A04 是当前提交重新复现的实现缺陷，新增 A05 为低风险的异常令牌响应问题，E01 是真实环境发布证据缺口，E02 是 CI 对 cloud 安全回归的覆盖缺口。既有编号沿用第四轮，避免把同一问题重复计数。

**第四轮 A01 的本地修复已复核有效。** 当前应用不再把私密搜索 query 或原始控制字符交给访问日志 handler；没有发现此次日志修复引起阻断性连锁问题。生产是否已安装此提交、代理/CDN/日志转发是否遵守同一合同及历史日志状况，没有实测，仍包含在 E01 的环境边界中。

聊天删除继续关闭。历史恢复兼容代码仍保留，目的是尊重已有持久化意图，不把“保留兼容代码”推断成新删除已开放，也不把完整删除功能未交付再次列为当前需求缺陷。

| 编号 | 当前问题 | 风险等级 | 修复风险 | 建议 |
| --- | --- | --- | --- | --- |
| A02 | 公共 DNS/连接异常未归一，模型误记 unknown、公共知识漏记失败 | P2 | 中 | 近期修复；网络故障或 unknown 积累时优先 |
| A03 | embedding 不完整响应头在外层超时后仍占用全部同步 HTTP 工作槽 | P2，可用性 | 中至高 | 近期修复；持续超时或慢端点场景下优先 |
| A04 | 同游标公共知识同步中，迟到失败覆盖较新的成功状态 | P2 | 中 | 近期修复；能确保同 peer 单任务时可短暂暂缓 |
| A05 | 非 ASCII 登录令牌或 Bearer 导致未捕获异常与连接断开 | P3，错误响应/排障 | 低 | 可暂缓，下一轮鉴权健壮性改进时修复 |
| E01 | 未取得当前精确 SHA 的八项真实环境签名验收证据 | P1，发布阻断 | 操作中至高 | 正式发布及生产自动化前必须完成 |
| E02 | CI 没有保证实际 cloud 签名、日志与 history 安全测试执行 | P2，质量门禁 | 低 | 下次 cloud 变更或 cloud 发布前补齐 |

P0 指有充分证据支持的紧急严重损失或广泛可利用问题；P1 指隐私、凭据、资金或生产发布安全边界可能失效；P2 指特定网络、并发、配置条件下的正确性、可用性或验证缺口；P3 指低风险的输入错误响应、易用性和工程质量问题。修复风险独立描述修改引入兼容、账务、时序及运行故障的可能性。E01 的 P1 不表示当前软件已经发生严重事故。

**概率口径：** 本轮没有生产事故统计或负载样本，不提供虚构的百分比。各项明确区分“触发条件满足后的确定性反例”和“触发条件在生产出现的未知频率”，并列出降低或升高风险的实际因素。

## 证据适用范围

上一轮整仓回归的应用提交为 `5c9a3a78a9840ebc5285e6ab4881afc3780a2f36`。从该提交到本轮应用提交，产品差异仅为 HTTP 日志源头、`service.py` 的日志 handler、代理示例，以及对应测试/部署文档；schema、网络后端、embedding、公共知识、模型账本、导出、迁移与 CI 没有改变。本轮通过 `git diff` 核对该范围，而不是仅凭旧报告判断。

本轮重新运行当前提交的合成反例、静态检查、前端回归、依赖扫描和关键跨模块合同。没有机械重复上一轮耗时 4492 秒的整仓运行，旧整仓结果只作为明确标注旧 SHA 的补充证据。

本报告不是逐行形式化证明，也没有实测生产规模、真实浏览器视觉/手机交互、模型回复质量或公网部署。下表“未确认新增问题”仅限所列入口和本地合同；未执行的环境专项不写成通过。

| 模块 | 本轮审阅与验证重点 | 结论及限制 |
| --- | --- | --- |
| HTTP、鉴权与会话 | 固定路由日志、请求解析重置、Bearer/角色、cookie/CSRF、登录失败预算、请求总截止、版本别名 | A01 本地关闭；新增低风险 A05，未确认鉴权绕过。实际 TLS/代理链未验收 |
| 管理台与公开前端 | 会话/视图版本、旧响应和写入延续、转义、草稿 revision、资源展示及错误提示 | Node 69 项通过；未进行真实浏览器视觉及移动体验验收 |
| 私密聊天、记忆与入站身份 | subject/窗口归属、已验证 principal 跨窗口历史、新删除拒绝、空恢复不改数据库、启动和备份恢复保留 | 删除禁用合同保留；未清除任何历史聊天或记忆 |
| 模型、路由和账本 | 密钥与整组撤销、实际发送前复核、重试、总池预算、预留、价格/usage 来源、unknown 对账 | A02 保留；未发现因此产生真实计费或预算绕过 |
| embedding | 授权、lease、输入/向量/usage 边界、调用总预算、真实 worker 退出 | A03 保留；不把调用者 timeout 当作线程退出 |
| 搜索、浏览器与网络能力 | 公共 DNS 固定、TLS 主机名、发送时授权、取消、容量及响应预算 | 未确认新增授权绕过；共享错误合同见 A02 |
| 公共知识 | 签名及可信 key、peer 配置、lease、导入/游标事务、失败记录、同游标终态 | 原导入/游标原子性修复保留；A02/A04 保留 |
| 通讯和 SMTP | 入站绑定、凭据快照、token 获取、发送围栏、SMTP AUTH 多轮和总寿命 | 已有围栏和 SMTP 修复合同保持；没有实发邮件或 QQ 消息 |
| 钱包与采集 | 最终 worker 策略、unknown、额度/费用预留、nonce/reorg、只读 RPC、签名器边界 | 本地围栏回归覆盖；真实资金/签名器/KMS/链证明未取得 |
| 生命周期、完整性与任务排空 | admission epoch、暂停/迁移/隔离、单进程所有权、写入围栏、超时后真实任务 | 未确认新增状态越权；共享 worker 容量问题单列 A03 |
| schema、保留和容量 | schema71→92、registry、受保护账务/审计、CAS 策略、路由证据、投稿字符和 UTF-8 容量 | 本地合同保留；不通过删除受保护证据缓解容量 |
| 导出、训练、备份与归档 | subject ownership、受管路径、取消/发布、同意版本、快照、摘要、恢复、cloud 日志/history | 实际 boto3 合成签名路径本地验证；CI 缺口为 E02；真实存储/恢复为 E01 |
| 迁移与升级 | 单活 epoch、目标证明、配置 revision、恢复/回滚、SHA/签名、release 元数据总预算 | 未确认新增证据绕过；双机/Linux root 与真实切换未实测 |
| 构建、依赖与发布 | 三组 hash 锁、已安装版本、全根 Ruff、Mypy、Node、在线漏洞扫描、外部门禁 | 本地结果见下文；E01/E02 保留，不以本地成功代替正式发布验收 |

### 当前提交的验证结果

解释器为 `.runtime/locked-venv/Scripts/python.exe`，Python **3.12.10**，实际导入路径为本工作区 `src/noyra`。75 个锁定包版本差异为 **0**；`pip check` 通过。Ruff **0.16.2**、Mypy **1.20.2**、HTTPX **0.28.1**、httpcore **1.0.9**、boto3/botocore **1.43.72**、urllib3 **2.8.0**；Node **v24.15.0**。环境证据：`.runtime/fifth-readonly-environment.json`。

- 全根 `ruff check .` 通过；`ruff format --check .` **619 个文件**通过。
- `mypy src tests` **405 个文件**通过。
- `admin.js`、`app.js` 语法检查通过；Node 前端 **69 项通过，0 失败、0 跳过**。
- 基础锁、基础+cloud 锁、dev 锁本轮分别在线扫描，均为 `No known vulnerabilities found`、退出码 0。cloud/dev 扫描有 Windows cache 写入权限警告，但扫描完成；没有改变锁或略过结果。已知漏洞扫描不是未知漏洞不存在的证明。
- 当前提交 **27 个测试文件**的跨模块 Python 串行合同回归：**342 passed，0 failed/errors/skipped**，耗时 **718.28 秒**。包括访问日志和部署、实际 boto3 签名/log/history/兼容、公共知识、模型授权/总池预算、模型/搜索/浏览器/通讯/SMTP/钱包发送围栏、schema71、聊天/删除禁用、训练同意/导出所有权/快照、保留/证据容量/投稿、迁移围栏、release/请求 deadline 和工作流合同。实际 S3 200/403 两项执行且通过，没有被 `importorskip` 跳过。日志/XML 和 `.runtime/fifth-readonly-result.json` 已生成，工具退出码 0。**此为跨模块专项，不是当前整仓回归。**
- 原第四轮探针在当前提交重新执行：A02/A03/A04 均复现；A01 的私密 query 与原始 ESC 均不再进入捕获日志。
- 新非 ASCII 鉴权探针：普通无效令牌 401；合成 Unicode 登录令牌/Latin-1 Bearer 均断开连接并出现未捕获 TypeError；后续健康请求仍 200。

本轮三组锁 SHA256：

| 锁 | SHA256 |
| --- | --- |
| `requirements.lock` | `81d242aa1cbb5b4a5c3c0354e6b1f5ef37d901809fe01700bc14a1f1fd06b78f` |
| `requirements-cloud.lock` | `4aafa2bedfc749e3c82964ffe3fa8807e7b4cfd8017fe88d70dbdd2b0fd5e7b0` |
| `requirements-dev.lock` | `e93467e673d93a4464b909526798a98551cede0b035403946b680cf694c7ef31` |

### 旧 SHA 的整仓证据

`5c9a3a7` 整仓串行运行 **2329 passed、56 skipped、259 subtests passed，0 failures/errors**，耗时 **4492.06 秒**。2385 个顶层测试；JUnit 包含子测试后计 2644，不作为 2644 个独立顶层测试。日志 `.runtime/fourth-readonly-5c9a3a7-full.log/.xml`，摘要 `.runtime/fourth-readonly-result.json`。**这不是 `649f3a0` 的整仓结果。**

56 项旧整仓跳过包含：4 项 Windows 8.3 别名、21 项 POSIX 权限/所有权/路径专项、2 项本机不支持的 symlink 构造、1 项 POSIX root 安装器、6 项独立服务账户迁移存储、20 项真实 Linux root/release、2 项 Bash 安装器恢复。没有缺 boto3 的跳过项。当前专项的跳过口径单独记录，不把 Windows 的通过数当成 Linux 验收。

A01 修复时的 **151 项不同测试**是多个批次证据；其中一次关联运行曾因 Windows 超长 pytest 参数编号出现 4 个 setup/teardown 错误，短编号修正后的专项与服务行为通过。此历史保留于 `docs/audit/2026-10-11-a01-remediation.md`，不改写成当时一次整仓零失败。

## 已关闭项：A01 日志源头修复复核

**原风险 P1；本地状态已关闭；生产安装和历史日志核查未完成。**

根因原为把默认 HTTP handler 的原始请求/错误参数交给 logger，私密搜索词通过相对 query 进入日志，且原始控制字符未转义。当前 `http_access_logging.py:47` 仅返回白名单方法、常量路由模板与数值状态，动态 ID/query 不作为日志字段；`service.py` 的请求/错误日志使用固定事件，解析标记逐请求重置。该分类仅用于日志，不决定路由、鉴权、CSRF 或资源账务。

原始 A01 反例当前结果：不存在的合成聊天查询仍返回 **404**，`private_search_phrase_in_log=false`、`auth_header_in_log=false`；未鉴权健康请求仍返回 **HTTP 200**，`raw_escape_in_log=false`。测试还覆盖无需下游 LogRecordFactory 的源头安全、中文/URL 编码、版本别名、动态路径、错误/超长请求和 keepalive 解析。

nginx 示例显式覆盖 HTTP/HTTPS access log，只记允许的方法、状态和耗时；两个虚拟主机原始 error log 关闭，减少了详细错误文本，这是有文档的诊断取舍。Caddy 示例与生成器没有启用 access log。未运行真实 `nginx -t`、Caddy 或代理集成，不声称 CDN、默认虚拟主机、debug/trace 和日志转发已遵守合同。历史日志也未读取、删除或改写。

## 未关闭项的详细记录

### A02：公共 DNS/连接错误类型未归一

**风险：P2。修复风险：中。建议近期修复，网络故障或 unknown 积累时优先。置信度：高（源码和当前反例）。**

**根因：** `core/http.py:240` 起的默认解析/公共地址验证抛出 `OSError`；同步 `_PublicDNSSyncBackend:332` 和异步 `_PublicDNSAsyncBackend:476` 只对特定 `TimeoutError` 做连接超时映射，普通 `OSError/socket.gaierror` 直接穿透。全部候选连接失败后，两者还把 httpcore 的 typed connect error 包回普通 `OSError`。HTTPX 不会把这类普通异常自动映射成消费者期待的 `httpx.ConnectError/HTTPError`。

**影响：** 模型 provider 未识别发送前失败，`gateway.py:317` 起进入 `provider_contract_failure` 兜底，call/attempt 为 unknown、预留保留；会占用可用额度并增加人工对账。保留预留仍是安全兜底，没有证明实际计费。`knowledge/sync.py:189` 起未转换该异常，`common.py:1161` 的正常失败结算不执行；`sync_due()` 仅捕获协议异常，当前轮可能提前中断后续 peer。embedding、只读钱包 RPC 也有只捕获 HTTPX 的源码路径，但本轮未独立复现其完整账本/调度后果，不另外计数。升级等有 `OSError` 捕获的消费者不能一概说成失败失控。

**触发条件与当前反例：**

1. 合成 resolver 返回 loopback，公共地址保护在 TCP 前拒绝。模型输出 `ProviderCallError/provider_contract_failure`，call/attempt 都 unknown；预留 **316 microusd**、实际成本 null，TCP 尝试数 **0**。
2. 同样的公共知识同步两次均抛 `OSError`，sync run 数 **0**，next schedule 未变、last error null。
3. 合成 DNS 失败同步/异步为 `gaierror`、`is_httpx_error=false`；模拟全部地址连接失败为 `OSError`、`is_httpx_error=false`。公共示例地址连接函数被替换，没有真的连接它。

**触发概率：** 正常 DNS/连接时低；DNS 故障、域名被拦截/重绑定到私有地址、所有候选地址拒绝或超时，则对应类型不匹配是确定性行为。模型和公共知识的完整反例验证了私有解析拒绝，其他 DNS/连接错误实测了类型边界，上层相同路径后果为有依据推断。没有线上发生频率。

**修复安全边界：** 从公共 backend 的发送前异常契约修复，区分非法端点、DNS/连接失败、连接总超时与发送后的读写失败；保留 cause、公网地址验证、固定连接及 TLS 主机名。不得将任意 `OSError` 全部算成零费用/可重试，否则可能释放真正未知预留并重复付费调用或副作用。非法端点不应作为普通瞬时网络失败无限重试。

**修复风险与验收：** 异常类型影响所有消费者、账本终态和重试策略，风险中。同步/异步、空/私有/非法地址、多候选连接和总超时均须测试；可证明未发送才释放预留，已经发送的未知结果继续保留。公共知识应记失败并退避、继续其他 peer；模型、embedding、搜索、升级和钱包交叉回归不得改变身份/预算/TLS 合同。

**暂缓条件：** 短期稳定网络、很少 unknown 且有对账监测时可暂缓。不要通过删账本或强制清零 unknown 缓解。

### A03：embedding 不完整响应头占用全部同步工作槽

**风险：P2，可用性。修复风险：中至高。建议近期修复，持续超时或慢端点场景优先。置信度：高。**

**根因：** `model/embedding.py:276` 默认 `PublicDNSHTTPTransport()` 没有传入原始 I/O 的 `remaining` 总预算。发送 trace 仅在发送请求头前检查；`SyncHTTPTimeoutHook` 到完整响应头返回后才注册 response。对端在 idle timeout 内持续滴送不完整头，httpcore 可反复读取，每次 idle timeout 未到；外层 `call_sync_http_with_deadline()` 虽返回超时，仍无法在没有 response 时关闭该读头工作。`core/http.py` 全局 gate 仅 4 槽，按真实 worker 退出释放，这是正确的容量上限，但消费者没有保证退出时限。

**影响：** 4 个这种 embedding 调用即可占用全部同步 HTTP 槽，使共享 gate 的 release 元数据、公共知识和只读钱包等操作等待或超时。不是无限线程增长；阻塞可持续到远端结束、断开、解析器上限或实际工作退出。此时请求已发出，embedding 用量未知终态合理，不得为回收槽位把它误记成未发送。

**触发条件与当前反例：** 使用真实 HTTPX/httpcore/parser/socket，仅在该探针进程替换 DNS 和 TLS 为本地 loopback。本地端点读到请求后每 25ms 发一个字节，保持响应头不完整。4 次 1 秒调用均返回 `embedding_timeout`（约 1000–1016ms），实际本地请求数 **4**、仍活着的 `noyra-sync-http` worker **4**；随后无关同步操作未执行，得到 `TimeoutError`。停止合成服务器后 worker 数 **0**，证明清理完成。

**触发概率：** 正常完整响应头时低；单次慢连接只占一个槽，4 个符合时序的调用同时存活时可确定耗尽容量。故障/恶意配置端点、并发重试和持续间歇字节增大概率；实际供应商出现该模式的频率未知。本地 DNS/TLS 替换仅用于隔离，不是绕过生产公共地址保护的证明。

**影响范围边界：** 默认公共知识与 release 元数据当前已经传 `remaining`，不能将它们自身误报为缺预算；它们仍可能因共享槽耗尽被阻塞。只读钱包 RPC 默认 transport 有同类未接总预算源码路径，本轮未对它单独滴头验证；外部注入 transport 的预算合同也需后续修复审阅，不计独立缺陷。

**修复安全边界：** 一次调用的 deadline 应覆盖等待槽、DNS/TCP/TLS、请求写入、不完整头与正文每次原始 I/O；worker 实际退出后才释放槽。不得超时即提前放槽、杀线程或关闭共享客户端，否则失去真实并发上限或干扰其他请求。发送后的 unknown 和预留保持；授权/lease 在最终发送时仍须有效。

**修复风险与验收：** 涉及原始网络流、客户端共享寿命和调用/线程双终态，风险中至高。应证明 4 次滴头超时后 worker、连接和 gate 在总预算内排空，下一无关操作能运行；补测 DNS/TLS/正文、排队和取消，核对已发送 unknown、不发送零用量、无重复请求及授权撤销合同。

**暂缓条件：** 可在可信稳定端点、低并发下短暂观察；出现重复 timeout 或共享槽拥塞时应优先处理。不通过扩大 gate 掩盖工作寿命问题。

### A04：迟到同步失败覆盖同游标的新成功

**风险：P2。修复风险：中。建议近期修复，单任务约束下可短暂暂缓。置信度：高。**

**根因：** `knowledge/common.py:1057` 的 `sync_peer()` 保存 peer 快照，成功完成传入 `expected_peer=peer_row`，失败 `:1175` 完成没有传同样的来源快照。`_finish_peer_sync():1833` 对失败只检查当前 cursor 是否等于该次 `cursor_after`，没有完成代次或 attempt identity。并发成功为 unchanged/空 feed 时游标不变，旧失败因此仍能写当前 peer。

**影响：** 较新成功的 remote ETag 被清空，last error 被改为 timeout，last-sync/下次调度被迟到失败改写；健康信息与重试节奏错误，可能增加无必要同步。本轮没有证明错误签名内容被导入、游标倒退或可信 key 撤销失效；导入与游标的原子性合同仍在。

**触发条件与当前反例：** 一个慢失败在 discovery 中挂起；第二次同步先完成成功、游标仍为 0 且写入 ETag；第一次随后 timeout。真实 store 的合成交错输出：`success_etag_present=true`、`stale_failure_removed_etag=true`、原 success error null、最终 error 为 `common_knowledge_peer_timeout`、`schedule_changed=true`。交错由本地客户端控制，没有外部 peer 调用。

**触发概率：** 无同 peer 重叠时低；后台定时与手工同步或多个任务重叠、较新成功不推进游标且较旧失败晚到时，夹具确定复现。真实重叠频率未知；游标前进时已有检查会挡住这一具体反例。

**修复安全边界：** 以持久化 attempt/revision 或严格等价的 CAS 标识完成所有权，失败和成功都不能覆盖较新的配置/完成代次。每次历史结果仍须准确记录；拒绝迟到覆盖不应反过来回滚已经提交的 import/cursor。不能用跨网络长期数据库事务锁解决，也不能取消 key/lease 的当前性验证。

**修复风险与验收：** 风险中，涉及调度状态与历史记录一致性。至少验证成功后迟到失败、失败后迟到成功、304/空 feed、cursor 已前进、peer 停用/换 key/改端点、暂停及恢复；较旧任务只能留下自己的结果，不更新较新 peer 状态，原导入和游标原子性保持。

**暂缓条件：** 只有能保证同一 peer 无重叠同步时可短暂暂缓；界面上的“按钮禁用”本身不能证明后台也无并发。

### A05：非 ASCII 令牌引发未捕获异常与连接断开

**风险：P3，错误响应和排障。修复风险：低。可暂缓至下一轮鉴权健壮性改进。置信度：高（真实本地 HTTP 反例）。**

**根因：** `service.py:8616` 的登录校验仅判断 token 是字符串，就将不可信 Unicode 字符串传入 `hmac.compare_digest(str, str)`；该函数只支持 ASCII 字符串，非 ASCII 会抛 TypeError。`_authorized():8532` 对解析后的 Authorization header 也直接做字符串比较；header 中 Latin-1 非 ASCII 字符同样触发。请求调度没有把这一输入错误转换成规范的鉴权失败响应。A01 仅改变日志方法，这些比较分支没有改变，因此不是此次日志修复引入的回归。

**影响：** 错误令牌含中文、全角/其他非 ASCII 字符或非 ASCII Bearer 时，客户端收到连接断开/网络异常，管理台可能提示无法连接而非令牌无效，后台标准错误会生成未捕获异常 traceback，增加排障噪声。本轮没有证明日志泄露输入令牌、服务进程退出、鉴权绕过、有效会话越权或写入成功。健康探针仍为 200，范围限于该请求的错误处理。

**触发条件与当前反例：** 本地真实服务配置合成 ASCII 管理令牌。POST `/admin/session`：普通合成无效 ASCII 令牌得到 **401/unauthorized**；含合成中文字符的 JSON token 得到 **RemoteDisconnected**。GET `/api/admin/conversations` 携带合成 Latin-1 Bearer 同样 **RemoteDisconnected**。进程内只捕获异常并输出类型/布尔值，`uncaught_non_ascii_type_error=true`；之后 `/health/live` **200**。没有把原始 token、traceback 或真实凭据写入审计日志。

**触发概率：** 全 ASCII 输入无此问题；有效 JSON token 含非 ASCII 且至少配置一个登录凭据时，在该比较分支确定复现。复制粘贴混入全角字符、误输入中文或构造异常 header 会提高概率；真实操作者发生频率未知。输入方不需要成功鉴权，但反例没有突破身份边界。

**修复安全边界与风险：** 风险低。比较前执行严格类型/ASCII 形状检查或采用明确的固定编码合同，对非法输入按既有 401/错误码拒绝。不要 trim、Unicode normalize、大小写折叠或“修正”令牌，否则改变凭据等价关系；保持有效 ASCII constant-time 比较、登录限速、cookie/CSRF 与角色合同。不要用吞掉所有鉴权异常的方式掩盖数据库完整性错误。

**验收标准与建议：** 空/非字符串/中文/全角/Latin-1 输入得到确定的非成功响应、无未捕获 traceback；有效令牌及无效 ASCII 行为保持，失败计数不被绕过，cookie/CSRF 和 Bearer 角色授权不扩大。此项可暂缓，A02/A03/A04 的正确性和可用性问题优先。

### E01：当前 SHA 的真实环境发布证据未取得

**风险：P1，发布阻断/验收缺口。修复风险：操作中至高。正式发布及生产自动化前必须完成。置信度：当前可用证据缺口明确；未知外部系统不作断言。**

**根因：** 当前工作区及本聊天可用证据没有提供对应 `649f3a042ae5eafd721714c6982c213e2145d9d6` 的八门禁真实执行、独立审阅、Ed25519 签名产物。`artifacts/release` 的历史记录、`output` 的 pending 模板及旧服务器 health/重启/8h soak 不覆盖本提交。没有访问 GitHub 私有运行、服务器或外部证据库，不能声称所有外部地点都不存在证据。

**影响：** 无法证明当前代码和 schema92 在真实 root/systemd、LUKS、双机、签名器、链和代理环境中符合安全合同。依现有合同，缺失/错误 SHA/过期/未签名的证据应阻断正式发布或生产自动化。这是应保持的 fail-closed 行为，不是建议把门禁放宽以消除错误提示。

**触发条件与概率：** 试图正式发布或启用要求同 SHA 证据的生产能力，且证据缺失/不匹配时，合同检查将确定拒绝。若有人绕过门禁，真实损失概率不能从本地测试估计，取决于部署与故障场景；本轮没有发生发布或损失。

**必需证据：**

| 门禁 | 当前尚未提供的实际证明 |
| --- | --- |
| `ubuntu_systemd` | 干净安装/升级、启动/停止/重启、readiness、回滚及 root/service-account 边界 |
| `encrypted_volume` | 实际加密卷、权限、密钥可用性、未解锁启动保护与恢复 |
| `backup_restore` | 备份生成、认证/完整性、干净主机恢复、摘要与 schema 合同 |
| `migration_fence` | 两主机、源写围栏、目标证明、恢复健康、切换及失败回滚 |
| `signer_kms` | 独立签名器/KMS 绑定、拒绝访问、轮换和恢复 |
| `reorg_nonce` | 隔离测试链上的 nonce 冲突、broadcast unknown、确认超时与重组对账 |
| `https_proxy` | TLS、可信代理头、鉴权/限速及全链日志隐私；A01 的模板语法/实际请求也在此验收 |
| `soak` | 有限 24/72h 的存储/WAL/供应商/钱包/生命周期观察；不能以 8h 或有失败样本当成零失败 |

**修复安全边界与风险：** 在一次性隔离环境执行，由不同执行者/审阅者确认当前精确 SHA，公开证据只放非秘密引用，独立签名。发布检查 freshness 为 72h；已安装的有效 SHA 不因单纯时间经过就停机。不要伪造 passed、沿用旧 SHA、跳过独立审阅或修改生产数据来凑证据。涉及 root、存储、跨机和测试链，操作风险中至高，应有预先可审阅的恢复步骤。

**验收标准与建议：** 八项齐全、通过、签名和 SHA 验证、独立保护环境及 artifact 下载复验；拒绝 wrong-key/tamper/replay/中断和未授权路径。可以继续隔离开发，不能以本报告解除正式发布阻断。相关合同为 `core/external_gates.py`、`core/release_assurance.py`、`scripts/verify_external_gates.py`、`.github/workflows/external-gates.yml` 与 `release.yml`。

### E02：CI 没保证实际 cloud 安全回归执行

**风险：P2，质量门禁缺口。修复风险：低。建议在下次 cloud 变更或 cloud 发布前补齐。置信度：高（workflow、锁与测试源码）。**

**根因：** `.github/workflows/ci.yml` 的测试矩阵只安装 dev 锁并以 `--no-deps` 安装项目，dev 锁没有 boto3；`tests/test_runtime_logging.py:112` 的实际 S3 签名/日志/history 测试用 `pytest.importorskip("boto3")`。cloud-profile 作业虽安装基础+cloud 并断言 boto3 版本，但只导入和 `pip check`，没有运行这些安全测试。release quality gate 同样仅安装 dev 锁；扫描 cloud 依赖也不会把它安装供 pytest 使用。

**影响：** 新 cloud 代码/SDK 变更可能在 CI 和 release gate 都绿时跳过真正的签名、200/403、额外 handler 和 history 回归。本地当前 dev+cloud 环境通过，不能证明 CI 执行了同样的案例。未证明发生真实云外泄，既有脱敏实现也没有因此自动失效。

**触发条件与概率：** 干净 workflow runner 只有所声明的 dev 包、没有 boto3 时，两项实际 signing 测试会确定跳过；cloud job 也确定不主动运行它们。未知共享 runner 若预装 boto3 可能改变情况，但不能依赖未声明的偶然环境。遗漏导致真实泄漏的概率未知，取决于后续修改；这属于持续验证缺口。

**修复安全边界：** 单独在 hash 锁定 dev+cloud 环境运行合成 SDK 测试，明确检查 import、200/403 实际路径以及未跳过；拒绝缺依赖假通过。禁止真实 AWS 凭据、bucket、计费网络或放宽秘密日志断言。CI 修复不应把 cloud 变成所有生产安装的强制依赖。

**修复风险与验收：** 低风险，主要为 CI 耗时、依赖组合与缓存。干净 runner 必须记录实际两项及兼容测试通过、0 skip；缺 boto3 时 job 失败而非绿色跳过。对支持 Python 版本/依赖组合保留常规质量门禁。本轮没有修改 workflow。

**暂缓条件：** 暂无 cloud 变更、每次修改都在锁定环境人工执行并审核日志时可短期暂缓；cloud 正式发布前仍应进入自动门禁。

## 后续顺序与审计结束条件

本轮没有实施任何修复。后续如获授权，建议先 A02（建立共享错误合同），再 A03（预算和真实 worker 退出），再 A04（完成代次），每项都先补触发反例、核对账务/身份/终态边界，再实现、回归和复审。A05 可作为后续独立低风险输入校验模块，E02 可作为独立低风险 CI 模块，E01 在最终候选提交确定后取得真实环境证据；源码或正式发布 SHA 改变后不能沿用旧 SHA 证明。

A02 和 A03 都涉及共享网络层，但错误分类与寿命控制目标不同，避免在一个大改动中同时释放 unknown、改变重试、扩大 gate 或更换 TLS 合同。A04 不应顺带扩大公共知识导入权限。聊天删除不属于此次修复建议，继续保持禁用。

本轮结束时只应跟踪本报告，应用/测试/配置/依赖 diff 为零；完整隔离日志保留，不删历史证据。审计文档提交不代表应用发布完成，也不消除 A02/A03/A04/A05/E01/E02。

### 本轮证据索引

- `.runtime/fifth-readonly-environment.json`、`.runtime/fifth-readonly-pip-check.log`：解释器、导入路径、75 包和锁摘要。
- `.runtime/fifth-readonly-ruff.log`、`.runtime/fifth-readonly-format.log`、`.runtime/fifth-readonly-mypy.log`：全根静态门禁。
- `.runtime/fifth-readonly-node.log`：69 项 Node 回归；两份 JS 语法检查退出码均 0。
- `.runtime/fifth-readonly-audit-base.log`、`fifth-readonly-audit-cloud.log`、`fifth-readonly-audit-dev.log`：本轮在线依赖扫描。
- `.runtime/fifth-readonly-649f3a0-probes.log`：A02 模型/知识、A04 交错、A01 原反例复验。
- `.runtime/fifth-readonly-649f3a0-http-probes.log`：同步/异步 DNS、模拟连接失败与 A03 真实本地滴头/排空。
- `.runtime/fifth-readonly-login-unicode-probe.py/.log`：新增 A05 的合成登录/Bearer/服务继续健康反例。
- 探针源码复用已审阅的 `.runtime/fourth-readonly-probes.py` 和 `fourth-readonly-http-probes.py`，没有覆盖旧日志。
- `.runtime/fifth-readonly-649f3a0-contracts.log/.xml`：当前提交跨模块串行合同回归，最终结果以已完成文件为准。
- `.runtime/fifth-readonly-result.json`、`.runtime/fifth-readonly-evidence-sha256.json`：本轮完成的结果摘要和隔离证据摘要，保留其应用 SHA，不因后续文档提交改写。
