# Noyra 第四轮全项目只读审计

审计日期：2026-10-11（Asia/Shanghai）

分支：`codex/admin-optimization-20261009`

被审计应用提交：`5c9a3a78a9840ebc5285e6ab4881afc3780a2f36`

数据库合同：schema **92**

本轮按最新的人类指令执行只读审计。应用、测试、配置、依赖锁及生产数据没有修改；仅新建本报告。隔离探针放在忽略的 `.runtime` 目录，使用临时数据库、合成内容和本地模拟网络；没有连接真实供应商、通讯平台、云账户、签名器或生产服务器，没有资金交易、付费调用、推送、合并或部署。聊天删除继续关闭，原文和记忆保留。

## 结论与风险口径

本轮确认 **6 项顶层问题或验收缺口：2 项 P1、4 项 P2，未确认 P0**。A01–A04 是有源码和隔离反例支持的实现问题；E01 是继续存在的真实环境发布证据缺口；E02 是可选云依赖安全回归未进入自动门禁的质量问题。

确认缺陷不等于已经发生私密内容外泄、真实费用损失或线上中断。本轮没有访问生产日志或账本，也没有事故统计。以下概率均为条件判断；“条件满足后确定复现”描述本地反例，不是虚构的线上发生率。普通配置、并发程度、网络时序和日志可访问范围会影响实际风险。

| 编号 | 问题 | 风险等级 | 修复风险 | 处理建议 |
| --- | --- | --- | --- | --- |
| A01 | 访问日志记录私密搜索词，且保留原始控制字符 | P1，条件性隐私风险 | 低至中 | 立即修复，使用私密聊天搜索前优先处理 |
| A02 | 公共 DNS/连接错误类型未归一，模型误记 unknown，公共知识漏记失败 | P2 | 中 | 近期修复，频繁网络故障时优先 |
| A03 | embedding 不完整响应头在调用超时后仍占用全部同步 HTTP 工作槽 | P2 | 中至高 | 近期修复，慢端点或持续超时前完成 |
| A04 | 相同游标的迟到公共知识失败覆盖较新的成功状态 | P2 | 中 | 近期修复；可在确保单任务同步时短暂缓解 |
| E01 | 当前精确 SHA 的八项真实环境签名门禁尚未取得 | P1，发布阻断 | 操作中至高 | 发布前必须补齐，不能由本地回归替代 |
| E02 | CI 未保证执行 cloud SDK 实际签名、日志及 history 安全回归 | P2，验证缺口 | 低 | 下次 cloud 相关变更进入发布前补齐 |

P0 为有充分证据支持的紧急严重损失或广泛可利用问题；P1 为隐私、凭据、资金或生产发布边界可能失效；P2 为特定网络、配置、并发或验证条件下的正确性和可用性问题；P3 为较低风险的工程质量问题。本轮没有另列 P3。修复风险描述修改可能造成的兼容、计费、终态和跨模块回归风险，与当前缺陷等级分开评估。

## 审计范围、方法与证据

受跟踪清单包括 `src/noyra` **212** 个文件、`tests` **223** 个文件；复核还覆盖 shell/发布脚本、四个工作流、三组依赖锁、Docker 和既有审计/修复证据。采用入口与跨模块合同审阅、实际 HTTP 日志探针、合成 DNS/连接失败、真实 httpcore 不完整响应头、同步交错、全根静态门禁、前端 Node 回归、依赖扫描和当前应用整仓串行回归。

这不是对全部代码逐行形式化证明，也没有实测生产规模的延迟、负载或模型质量。下表的“未确认新增问题”只限本轮审阅与本地验证覆盖。没有把 Windows 跳过项当作 Linux 通过，没有把上一轮的通过数冒充当前提交结果，也没有把源码与 Node 检查当作真实浏览器视觉或手机 QQ 体验验收。

| 模块 | 重点复核 | 当前结论及范围限制 |
| --- | --- | --- |
| HTTP 服务、鉴权和会话 | Bearer/角色、cookie/CSRF、登录失败预算、请求绝对截止、异步 bridge/关闭、访问日志 | 请求接收的新总预算保留；新增 A01。真实反向代理/TLS 仍属 E01 |
| 私密聊天、身份和记忆 | 窗口主体归属、创建者/已验证入站关联、跨窗口历史、来源、删除禁用及旧任务恢复 | 新删除请求仍拒绝；没有清除历史聊天/记忆。私密搜索日志问题单列 A01 |
| 模型、资源与费用 | 密钥/整组撤销、路由冻结、发送前复核、每次重试、预留、usage 来源、unknown 与对账 | 新增 A02；没有发现因此发生实际供应商计费或绕过额度 |
| embedding | 发送前授权、向量/用量大小、独立额度、hard timeout、真实线程和连接排空 | 新增 A03；A02 的同类异常路径有源码支持，未另作账本探针 |
| 搜索、浏览器和网络能力 | 公共地址固定、TLS 主机名、发送 trace、能力版本、取消、响应大小与超时 | 未确认新增私密查询外发授权绕过；共享 HTTP 错误合同见 A02 |
| 公开页面、投稿和验证码 | 转义、异步视图隔离、字符/UTF-8 字节容量、事务内当前策略、验证码模式 | 本轮未确认新增问题；没有进行公网攻击/容量压测 |
| 通讯、入站和 SMTP | 账号验证、App Secret token 锁、凭据快照、SMTP AUTH 多轮与发送围栏、总预算、unknown | 上一轮 SMTP 修复保留；真实邮件/QQ 仍未验收 |
| 公共知识 | discovery/feed 签名、key trust、租约、peer 配置、导入和游标原子性、完成结算 | 原游标推进修复保留；新增 A02 的失败漏记和 A04 的同游标竞态 |
| 钱包、采集和付款 | 策略与实际 worker 同步、最终字节写入围栏、nonce/reorg、费用预留、未知结果 | 未做真实交易。部分同步网络消费者仍未使用原始 I/O 总预算，影响范围见 A03 |
| 生命周期、睡眠、完整性 | gate/epoch、暂停/隔离/迁移失效、事务提交、后台任务排空、检查超时 | 本轮未确认新增状态越权；不能用线程外层 timeout 推断线程退出 |
| schema、保留与容量 | schema71→92、注册表、保护账务/审计、CAS 设置、计数/清理游标、用量时钟和价格来源 | 本轮未确认新增破坏证据的清理路径；不通过删除证据缓解容量 |
| 导出、训练、归档和备份 | subject ownership、同意版本、取消/发布、受管路径、快照、流式摘要、日志与 history | 云 SDK 当前脱敏本地验证覆盖；CI 缺口见 E02；真实 LUKS/云恢复属 E01 |
| 迁移、root 激活和升级 | 单活、租约、配置版本、SHA/签名、备份回滚、元数据绝对预算及错误码 | 本轮未确认新签名绕过；真实双机/root runner 不能由本机模拟替代 |
| 构建、依赖与 CI/发布 | 精确锁版本、hash 安装、全根 Ruff、Mypy、前端、漏洞扫描、真实门禁 | 本地静态及三组扫描通过；E01/E02 保留；不把扫描通过等同无未知漏洞 |

### 当前验证结果

- 精确解释器：`.runtime/locked-venv/Scripts/python.exe`，Python 3.12.10。本次复核 75 个锁定包版本差异为 0，锁 hash 与既有锁环境证据匹配；`pip check` 通过。证据 `.runtime/fourth-readonly-environment.json`。
- 全根 `ruff check .` 通过；`ruff format --check .` **615 文件**通过；`mypy src tests` **403 文件**通过。
- `admin.js` 与 `app.js` 的 Node 语法检查通过；前端 Node **69 项通过，0 失败、0 跳过**。
- 三组锁分别在线扫描：基础、基础+cloud、dev 均为 `No known vulnerabilities found`，退出码均为 0。初次调用受 Windows 中文路径的 `pip_api` 解码错误阻断，启用仅该进程的 UTF-8 环境后完成；原失败日志保留。部分扫描有 Windows cache 写入权限警告，未修改锁文件或绕过扫描结果。
- 当前应用整仓串行回归 **2329 passed、56 skipped、259 subtests passed，0 failures、0 errors**，耗时 **4492.06 秒（1小时14分52秒）**。收集 2385 个顶层测试；JUnit 将子测试也计入，总数为 2644，不将它误写为 2644 个独立顶层测试。实际 boto3 200/403 签名、日志和 history 两项均执行且通过，没有跳过。日志为 `.runtime/fourth-readonly-5c9a3a7-full.log`，JUnit 为同名前缀的 `.xml`，结果摘要为 `.runtime/fourth-readonly-result.json`。

测试从 2026-10-11 08:51:38（Asia/Shanghai）开始，完整日志和 XML 已生成；恢复中断对话时确认没有仍在运行的 Python 测试进程，未重新启动整仓回归。旧工具 session 已失效，退出结果以已完成日志与 XML 的零 failure/error 为证，不将 session 无法再次读取误报为测试失败。

56 项跳过按 XML 原因归并如下：

| 跳过原因类别 | 数量 | 未得到的证明 |
| --- | --- | --- |
| 当前卷不生成 Windows 8.3 路径别名 | 4 | 8.3 别名兼容与相关重解析点边界 |
| POSIX 权限、所有权、mode、marker、token、symlink、filesystem 合同 | 21 | POSIX 专项，不能由 Windows 文件 API 通过替代 |
| 当前主机不可用的 symlink 构造 | 2 | 两项链接路径专项 |
| 需要 POSIX root 模拟安装器 verifier | 1 | 安装器权限隔离的真实 root 路径 |
| 需要 POSIX root 与独立非特权服务账户 | 6 | 迁移存储的服务账户权限边界 |
| 需要真实 Linux root 和服务账户权限 | 20 | release/upgrade 等主机专项 |
| Bash 安装器恢复 | 2 | shell 恢复专项 |

上表合计 56。没有缺 boto3 的跳过项；E02 仍是 CI 声明缺口，不因本地 dev+cloud 环境通过而消失。整仓零失败说明现有测试合同通过，A01–A04 的新隔离反例说明这些触发条件尚未被现有回归充分覆盖，两者不矛盾。

上一轮 `d6f689f` 的 2276 passed、56 skipped、259 subtests passed 属于旧提交历史。当前提交已有的隔离检出验证文件 `.runtime/third-fix-committed-verification.json` 虽标记 `scope=all`，实际 pytest 只列四个测试文件、30 个测试；不能将该字段解读为当前整仓零失败。

隔离反例证据：`.runtime/fourth-readonly-probes.py/.log` 与 `.runtime/fourth-readonly-http-probes.py/.log`。日志只保存错误类型、布尔值、计数和耗时；合成凭据或原始访问日志不写入审计文档。embedding 头部探针第一次采用 150ms 时尚未进入 HTTP 请求，不能用于证明慢响应头；调整到 1 秒后实际本地请求数为 4，才作为 A03 证据。

## 详细发现

### A01：访问日志记录私密搜索词，且保留原始控制字符

**风险等级：P1，条件性隐私风险。修复风险：低至中。建议立即修复。**

**根因：** `src/noyra/web/admin.js:794-797` 把私密聊天搜索框内容放入 GET 的 `query` 参数。`src/noyra/service.py:9582` 的 `log_message()` 对默认 handler 的 request line 原样格式化，记录包括 query 的相对 request target。`src/noyra/core/runtime_logging.py:42-74` 对 HTTPX/httpcore 做了字段白名单，对普通 Noyra 文本仅脱敏绝对 URL 和秘密样式文本；相对路径中的普通私密查询不匹配。自定义 logger 同时跳过标准库默认 `log_message()` 的控制字符转义。这是日志内容契约缺口，鉴权并未失效。

**影响：** 操作者输入私密姓名、经历、研究或聊天片段进行搜索时，内容可能从加密数据卷上的聊天记录转入 journald、文件日志、日志转发或日志备份。日志读者的范围、保留时间和存储位置可能不同于聊天数据库。百分号编码不是隐私保护。探针没有发现 Bearer token 进入日志，也没有证明远程匿名者可以读取这些日志。

**部署扩展边界：** `deploy/nginx/noyra.conf.example` 没有显式无 query 的 access-log 合同，会继承主机的日志设置。若主机沿用包含完整 request 的常见格式，私密 GET 参数也可能进入代理日志；本轮未运行真实 nginx，不把该条件写成已实测生产泄露。仅修应用 logger 不足以证明整条部署链安全，须同时检查代理/日志转发，或为私密搜索改用不含词语的 URL 合同。

**触发条件与反例：** 本地真实 HTTP 服务启用既有 `install_log_safety()`，使用合成 Bearer token 请求 `/api/admin/conversations/synthetic/messages?query=fourth_private_chat_phrase`。不存在的合成窗口返回 404，但捕获的 INFO 访问日志仍包含搜索词：`private_search_phrase_in_log=true`、`auth_header_in_log=false`。是否命中实际聊天不是日志发生的前提；handler 会记录请求 target。

**触发概率：** 日志为 INFO 且使用搜索时，符合该文本条件的记录是确定性行为。形成实际隐私暴露还须搜索词敏感且日志存储/读取范围超出预期；不搜索、日志不记录 INFO 或仅输入无敏感关键词时，该具体风险显著降低。没有生产日志权限清单，不能量化真实泄露率。

**同根因子问题 A01b：原始控制字符（P2，修复风险低）。** 未鉴权的本地原始 socket 请求在健康路由 query 中放入合成 ESC 控制序列，返回 `HTTP/1.0 200 OK`，日志捕获 `raw_escape_in_log=true`。入口将外部不可信控制字符保留到日志；如果后续终端/日志查看器直接解释该序列，显示可被改变，影响阅读和排障。探针只证明字符未转义，没有执行 shell、改变账务审计或证明生产 journal 查看器一定解释该序列。正常浏览器 URL 编码通常降低直接触发，直接网络请求可构造；记录行为在夹具中确定，显示影响取决于查看器。和私密 query 同在日志源头修复，规范化后的方法/路径/异常字段仍须拒绝或可视化转义控制字符。

**修复边界与风险：** 优先在访问日志源头按字段白名单记录方法、规范化路由/路径、状态和耗时，丢弃 query，避免依赖越来越长的秘密词正则。对携带 webhook token 或其他私有标识的 path 也要定义记录合同。不得为便于诊断恢复原始参数，也不要直接关闭全部安全/运行诊断；不要未获授权删除历史日志或轮换凭据。

**验收标准：** 真实授权/未授权请求、存在/不存在窗口、中文/URL 编码关键词、任意敏感 query 与 webhook 路径，在默认和额外 handler 中均不出现私密词/令牌；原始 ESC/控制字符不会交给日志 handler。日志仍可识别路由、状态和失败类型。后续历史日志核查是独立环境工作。

### A02：公共 DNS/连接错误未归一，造成错误 unknown 与失败漏记

**风险等级：P2。修复风险：中。建议近期从公共网络后端修复。**

**根因：** `src/noyra/core/http.py:240-298` 的解析/公共地址验证以 `OSError` 拒绝失败或私有地址；同步 backend `:342-391` 和异步 backend `:483-538` 未把这些错误统一成 httpcore 的连接异常。所有地址连接失败后，又将 `httpcore.ConnectError/ConnectTimeout` 包成普通 `OSError`。HTTPX 只映射 httpcore 合同中的异常，普通 `OSError`、`socket.gaierror` 直接穿透。调用者普遍捕获 `httpx.ConnectError`、`httpx.TimeoutException` 或 `httpx.HTTPError`，因此进入错误的兜底分支。

**影响：**

- 模型 `openai_compatible.py:137-170` 未识别这种发送前失败，`gateway.py:317-335` 将其当作 provider contract failure 记为 unknown，保留 token/费用估算；影响资源可用额度、未知调用提示和对账工作。保留预算是兜底安全行为，但对可证明未连接/未发出的情形过于保守。本轮没有实际计费。
- 公共知识 `knowledge/sync.py:189-202` 未转换该异常；`common.py:1161-1185` 不进入失败记录，`sync_due()` 只捕获协议错误，当前轮可能直接中断并跳过后续 peer。`next_sync_at` 未推进，后续轮继续尝试但不留下正常失败合同。
- embedding 和只读钱包 RPC 存在相同只捕获 HTTPX 异常的源码路径；没有用独立账本/RPC探针确认其全部上层后果，不把推断计作额外已复现问题。升级已有 `OSError` 捕获，不应声称所有消费者均崩溃。

**触发条件与反例：**

1. 自定义解析器返回 loopback，实际公共地址验证在 TCP 前拒绝。模型网关结果为 `provider_contract_failure`，call/attempt 都是 unknown；合成预留为 316 microusd、实际成本 null，未进行公共连接。
2. 相同拒绝条件执行两次真实公共知识同步：异常均为 `OSError`、同步记录数 0、下次调度不变、最后错误 null。
3. 合成 NXDOMAIN 类型错误同步/异步均直接成为 `gaierror`，不是 HTTPX error；合成所有公共地址连接失败最终成为 `OSError`，不是 HTTPX error。没有尝试连接示例公共地址。

**触发概率：** 公共域名正常、连接正常时低；DNS 故障、被拦截/重绑定到私有地址、全部候选地址连接拒绝/超时时，在对应分支为确定性类型不匹配。前两项账本/同步后果实测了私有解析拒绝，普通 DNS/连接故障实测了类型边界，上层后果按相同分支推断；没有线上发生率。

**修复边界与风险：** 在网络 backend 区分发送前的 DNS、非法端点、连接失败和连接超时，以稳定的 typed error 向 HTTPX/业务层传播；保留 cause 和公共地址/TLS 固定。非法端点通常不可重试，普通暂时连接失败才可在预算内重试。不得将任意 `OSError` 都改成“零费用且可重试”，因为请求写入后失败、响应丢失或第三方自定义异常仍可能有实际副作用；误释放 unknown 或自动重试会引入重复调用/付款风险。

**验收标准：** 同步与异步 DNS、私有/空/非法地址、多地址连接失败、连接总超时均有正确业务错误码；可证明发送前失败记零实际用量并按策略释放预留，已发送读/写失败仍 unknown。公共知识失败被记录和退避且不阻断其他 peer；模型、embedding、搜索、升级与只读钱包的交叉回归保持原身份/预算/重试合同。

### A03：embedding 不完整响应头在调用超时后仍占住全部同步 HTTP 工作槽

**风险等级：P2，可用性风险。修复风险：中至高。建议近期修复。**

**根因：** `model/embedding.py:274-276` 默认创建 `PublicDNSHTTPTransport()`，没有传其可选的 `remaining` 原始 I/O 剩余预算。`SyncHTTPTimeoutHook` 到完整 response 进入上下文后才 register。HTTPX/httpcore 为接收不完整响应头可执行多次 socket read，每次都使用 idle timeout；对端持续 drip 就可长期不触发 idle timeout。外层 `call_sync_http_with_deadline()` 能按时给调用者返回 timeout，但 hook 此时没有 response 可关闭，后台线程仍读头部。全局 `_SYNC_HTTP_GATE` 只有四个槽，直到实际线程退出才释放。

**影响：** 四次符合条件的 embedding 请求即可耗尽所有同步 HTTP worker，使共享该 gate 的元数据读取、公共知识和钱包只读请求等等待/失败。线程上限仍生效，故不是无限增长；它防止无限增长但未保证容量及时回收。此次探针中请求头/正文已经发到本地端点，`embedding_timeout` 作为未知用量是合理终态，不能为了回收资源把它改为未发送。没有发生真实付款或私密输入外发。

**触发条件与反例：** 使用真实 HTTPX/httpcore header parser 和 socket，只有 DNS 与 TLS 握手替换为本地 loopback 夹具。端点收到四个合成 embedding 请求后，每 25ms 返回一个头部字节、始终不结束 headers。每次名义 1 秒调用在约 1000–1016ms 返回 `embedding_timeout`；随后 `live_workers_after_timeouts=4`、`actual_loopback_requests=4`，一个无网络的独立同步操作都无法进入，`unrelated_sync_operation_ran=false`。停止模拟端点后线程数归零，临时资源清理通过。

**触发概率：** 正常完成响应头的端点不触发。偶发慢头部可能造成短时占用；持续 drip/故障端点并重复调用至少四次时，隔离反例确定耗尽容量。生产 TLS/代理行为、并发和 header 大小限额决定持续时长；未量化线上比例。第一次 150ms 探针尚未实际发请求，已排除作为此结论的证据。

**影响范围说明：** release/common 新客户端已传 `remaining`，不能说上一轮原始读 deadline 修复全失效。`wallet/rpc.py:89`、`wallet/local_rpc.py:65`、`wallet/execution.py:325` 的默认 transport 同样未传原始 I/O 总预算，属于同一源码模式；本轮实际慢头部只复现 embedding。外层支付 trace 的取消/截止检查仍在，不能由“线程未退出”推断“超时后新转账字节仍会发送”。

**修复边界与风险：** 将每请求的绝对 deadline 传至 DNS、TCP、TLS 和每次原始读写，并处理连接池复用时的旧 deadline 闭包；为关闭/取消绑定尚未形成 response 的连接资源。四槽应持续绑定实际 worker，不能在调用超时后提前释放而让迟到线程无限增长；不要在另一线程粗暴关闭共有 client 影响其他请求。继续保护最终授权 trace、已发 unknown、钱包 nonce/对账和 TLS/DNS 固定。

**验收标准：** 实际 raw header drip 在预算内结束 worker并释放槽；DNS、TCP、TLS、发送、headers 和 body 各阶段的迟到/取消都不产生新外发；连续四次失败后独立同步操作可开始；正常连接池复用、重试、并发、发送结果未知和多消费者回归通过。不能只检查调用者耗时或 fake Response.close。

### A04：相同游标的迟到公共知识失败覆盖较新的成功状态

**风险等级：P2。修复风险：中。建议近期修复。**

**根因：** 上一轮防止旧失败倒退新游标的修复，在 `common.py:1832-1864` 的完成事务中仅比较当前 `row.cursor` 与该结果的 `cursor_after`。失败路径不比较同步尝试世代或 peer 状态修订。较新成功若没有新增事件，游标保持不变；旧失败因此通过比较，并覆盖 `remote_etag`、`last_error_code`、完成时间和下一次调度。已有并发回归 `tests/test_common_sync_fencing.py` 让较新成功推进游标，未覆盖无新事件/304 的场景。

**影响：** 管理台和后续同步认为最后一次是失败，成功 etag 丢失，退避/调度被旧结果改写，造成冗余 discovery、错误故障提示和成功状态不可靠。没有证据表明私密内容泄露、签名验证失效、可信 key 绕过或游标倒退。导入/事件/游标的原子事务修复仍保留。

**触发条件与反例：** 同一 active peer 的两个同步交错；较早请求未结束，较新请求完成且没有新事件，游标仍为 0。较新成功设置 etag 并清空错误；较早任务随后超时。实际 store 探针得到 `success_etag_present=true`、`stale_failure_removed_etag=true`、`success_error=null`、`after_error=common_knowledge_peer_timeout`、`schedule_changed=true`。

**触发概率：** 严格单任务串行同步时低；后台同步、手动同步或多个入口交错时存在竞态。公共知识通常没有新事件，游标不变并不罕见；若按上述完成顺序交错，探针确定覆盖。未测量生产同 peer 并发频率，不推断是每次同步必发。

**修复边界与风险：** 引入同步尝试 generation/token 或独立 state revision 的完成 CAS，允许该任务自身已提交的分页游标变化，同时拒绝已被更新成功/配置修订取代的完成结果。失败审计可以保留该次真实失败，但不得宣称它是覆盖较新结果的当前状态。避免简单比较入口 state_hash：任务自己的合法分页会修改 state_hash；也不能为了通过 CAS 重读最新世代冒充旧任务。

**验收标准：** 无新事件、304、推进游标、部分导入、较新成功/失败、peer disable/key revoke/迁移失效均交错验证；较旧完成不覆盖当前 etag、错误或调度；每次实际运行仍有真实审计，原子性和签名/subject ownership 不变。

### E01：当前精确 SHA 的八项真实环境签名门禁仍未取得

**风险等级：P1，发布证据阻断。修复/操作风险：中至高。生产发布前必须完成。**

**根因：** 当前只有 `.runtime/third-fix-external-gates-5c9a3a7.pending.json`，八项 status 均为 pending、签名/独立复核未完成。本地 Windows 测试不能提供 Ubuntu/systemd、LUKS、双机迁移、隔离 KMS/签名器、真实链 reorg/nonce、HTTPS 代理或长期 soak 的环境证据。

**影响：** 无法称当前应用已经完成生产发布验收，也不能以旧服务器版本、旧 SHA、孤立健康 200、模板或最终 smoke 代替八项证据。发布和生产自动化的拒绝门禁仍在；本轮未证明门禁被绕过或已部署版本正在故障。

**触发条件与概率：** 在缺少绑定实际发布完整 SHA、八项正常/拒绝路径、独立执行/复核和 Ed25519 签名的证据时尝试发布，该缺口确定存在，合格验证器应拒绝。真实环境出故障的概率未知，不能用 pending 推断 Linux/LUKS/KMS 必有缺陷。

**处理边界与修复风险：** 按 `docs/release/2026-10-11-third-round-external-acceptance.md` 和 `docs/release/external-gates.md` 在隔离环境执行。风险来自停机、恢复、双机单活切换、密钥与链状态操作，应由具备环境权限的执行者和独立复核者完成。不得伪造签名、将 pending 手工改 passed、降低新鲜度、使用付费/资金操作冒充已获授权。既有已验收运行版本不会仅因 72 小时新鲜度过期而自动停止；该时间是新发布验收窗口。

**验收标准：** `ubuntu_systemd`、`encrypted_volume`、`backup_restore`、`migration_fence`、`signer_kms`、`reorg_nonce`、`https_proxy`、`soak` 全部真实完成；保存脱敏证据引用、三锁 hash、UTC、独立身份和签名，验证器对正确 SHA 通过，对旧 SHA/缺项/过期/篡改拒绝。审计文档提交也会改变 HEAD；若发布包括文档的新 HEAD，证据须绑定该实际发布 SHA，不能以被审计应用 SHA 自动替代。

### E02：CI 未保证执行实际 cloud SDK 安全回归

**风险等级：P2，验证缺口。修复风险：低。建议下次 cloud 相关变更发布前完成。**

**根因：** `tests/test_runtime_logging.py:112` 的实际 boto3 签名/200与403/日志及 history 用例通过 `pytest.importorskip("boto3")` 避免让 base 安装必须引入 cloud 包。`.github/workflows/ci.yml` 的完整 test 矩阵安装 dev 锁，该锁不含 boto3，在按锁建立的洁净环境中实际 SDK 用例会被跳过；cloud-profile job 虽安装基础+cloud 锁，却仅 import boto3 和 `pip check`，不执行 pytest。release 工作流的质量验证也使用 dev profile。可选依赖划分正确，但没有对应的可靠实际 SDK 自动回归；不能依赖 runner 偶然预装未声明包。本轮没有读取实际 GitHub job 日志。

**影响：** 当前修复在本地 dev+cloud 锁环境中有实际 SDK 探针和测试，不应声称现有脱敏已经失败。不过未来 botocore 升级、history recorder 或 client 创建修改可能在 CI 绿色时重新暴露日志/签名，mock/stub 的云测试不能覆盖实际 SDK 内部事件格式。

**触发条件与概率：** 当前标准 CI 配置下，若环境缺 boto3，实际 SDK 用例确定跳过；cloud-profile 的声明步骤确定不运行这些测试。形成安全回归需后续相关变更或依赖行为改变，条件性发生，无法量化；本地当前通过降低已知当前缺陷的疑虑，不能消除以后门禁缺口。

**修复边界与风险：** 在锁定 dev+cloud 的独立 job 执行实际 SDK 安全/兼容测试，使用合成凭据和 mock endpoint，不访问真实 S3；确保关键用例未跳过且结果进入发布 required checks。保持基础运行 profile 可独立安装，不把 cloud 改为所有用户必需依赖，也不直接用无 hash 的临时最新版依赖修补环境。

**验收标准：** CI 实际执行 200/403 签名、多个日志 handler、异常和 history，确认无敏感标记；未安装 cloud 或关键测试 skipped 时，该专门门禁失败；base/cloud 的 hash 安装、pip check 与 Docker profile 均正常。无需真实云费用即可验收该缺口。

## 上轮修复及历史问题复核

| 既有问题 | 本轮复核结论 |
| --- | --- |
| 第三轮 A01/A06，SMTP 认证和总预算 | AUTH 多轮/机制回退和 DATA 写入前有授权复核；共享有界 DNS、真实 worker 槽与 socket shutdown 保留。没有将这些问题重新列为未修复 |
| 第三轮 A02，云 SDK 原始日志/history | botocore/boto3/urllib3/s3transfer 字段白名单和独立 history 出口保留；新增 E02 是自动回归缺口，不是旧脱敏故障重现 |
| 第三轮 A03/A05，升级 metadata 和入站 drip | release/raw I/O remaining、inbound read1 总预算和异步 bridge 继承保持；A03 本轮是尚未接入该合同的 embedding 等消费者 |
| 第三轮 A04，公共知识暂停/分页及游标原子性 | checkpoint、HTTP 发头/发体围栏、package/import/provenance/event/cursor 单事务保持；本轮 A04 是同游标完成竞态 |
| 第三轮 E02，锁环境差异 | 精确锁解释器可用，本次复核 75 包版本差异 0；本轮没有重新出现依赖版本不一致 |
| 历史模型/通讯/搜索/钱包外发围栏 | 真正网络发头前的固定授权和运行租约复核保留，钱包支付 body 前截止检查也保留；未确认新的真实付款外发绕过 |
| 历史前端旧响应/登录状态、公开视图 | generation/AbortController 和 DOM 退出清理保留，Node 合同覆盖；不以本次源码检查承诺所有设备视觉体验 |
| 小容量投稿、用量分类、保留/证据容量、soak 误判 | 当前字符/字节合同、usage来源分类、受保护证据和 `soak=degraded` 非零退出仍在；不能使用用户旧版本的 `probe_failures=1 soak=passed` 作为新版本证明 |
| 私密聊天删除与历史恢复 | `ConversationErasure.request()` 继续固定拒绝 `private_deletion_disabled`，未重启删除能力。历史已执行任务/标记的防复活恢复保护保留，不能把停用入口解释为移除安全恢复逻辑 |
| 依赖已知漏洞与全根格式门禁 | 三组锁本次扫描通过，全根 Ruff 和格式通过；未用局部 src/tests/scripts 的通过替代全根结果 |

## 建议的后续修复顺序

本节仅是可审阅方案，不代表本轮实施授权，也未修改代码。

1. **A01 日志隐私**：先缩小访问日志字段。这是直接隐私风险且修改面相对小；用真实 HTTP handler 和多个日志 handler 验证后独立提交。
2. **A02 公共网络错误合同**：从 backend 统一发送前异常类型，再让业务层采用正确终态。先明确“可证明未发”和“已发未知”，完成模型/embedding/知识/只读钱包/升级交叉审计后提交。
3. **A03 实际同步 I/O 排空**：在已有总预算原语上补齐消费者和每请求连接资源，覆盖真实慢头部与池复用；复审钱包最终字节、unknown、暂停/关闭和 gate 容量，禁止靠提前释放槽隐藏线程。
4. **A04 同步结算世代**：在网络错误和停止语义稳定后处理并发完成状态；维持原子导入/游标和独立失败审计，不以重读当前世代伪装旧任务。
5. **E02 自动 cloud 回归**：建立实际 SDK job 并接入发布门禁，完全使用模拟 endpoint；当前可与前述本地模块安排衔接。
6. **最终本地整合与 E01 环境验收**：每模块均按根因复现→最小修复→针对性回归→跨模块审计→单独提交。最终固定实际 SHA 后完成真实八门禁；本地结果与环境结果分别报告，不宣称“所有生产问题已解决”。

A01 应立即处理；A02/A03 在持续网络异常、未知调用增多或启用慢 embedding/RPC 时提升优先级；A04 可在确保同 peer 单任务的前提下短暂延后，但这只是减少触发条件。E02 可等到下一次 cloud 变更，届时发布前必须补齐。E01 可以在本地开发阶段等待环境安排，不能暂缓到发布之后。

修复仍须遵守现有身份隔离、预算和 unknown、schema71 迁移、导出所有权、同意撤销、单活恢复、防复活合同；聊天删除明确不在范围内。没有新实现授权时继续只读，不把报告建议自动执行为代码修改。

## 可重复验证与证据索引

本次实际使用以下命令范围，全部应用验证绑定上述 `5c9a3a7` 完整 SHA；审计文档提交后的代码树与该应用树相同，不据此将真实外部签名证据自动绑定新 HEAD。

```text
.runtime/locked-venv/Scripts/python.exe -m pytest -vv -p no:xdist --junitxml=.runtime/fourth-readonly-5c9a3a7-full.xml
.runtime/locked-venv/Scripts/python.exe -m ruff check .
.runtime/locked-venv/Scripts/python.exe -m ruff format --check .
.runtime/locked-venv/Scripts/python.exe -m mypy src tests
.runtime/locked-venv/Scripts/python.exe -m pip check
node --check src/noyra/web/admin.js
node --check src/noyra/web/app.js
node --test tests/frontend/admin-optimization.test.cjs
.runtime/locked-venv/Scripts/python.exe -m pip_audit -r requirements.lock --no-deps --disable-pip
.runtime/locked-venv/Scripts/python.exe -m pip_audit -r requirements.lock -r requirements-cloud.lock --no-deps --disable-pip
.runtime/locked-venv/Scripts/python.exe -m pip_audit -r requirements-dev.lock --no-deps --disable-pip
```

pip-audit 完成调用使用 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8` 的进程环境。整仓日志和 XML 可离线复核；漏洞库查询会随时间变化，不能保证未来得到相同扫描结果。

| 证据文件（均为忽略的本地审计产物） | 证明范围 |
| --- | --- |
| `fourth-readonly-5c9a3a7-full.log/.xml`、`fourth-readonly-result.json` | 当前应用整仓、实际 cloud 用例和逐类跳过原因 |
| `fourth-readonly-ruff.log`、`fourth-readonly-format.log`、`fourth-readonly-mypy.log`、`fourth-readonly-pip-check.log` | 全根静态门禁及安装一致性 |
| `fourth-readonly-node.log` | 69 项前端合同；不证明真实浏览器视觉 |
| `fourth-readonly-audit-base.log`、`fourth-readonly-audit-cloud.log`、`fourth-readonly-audit-dev.log` | 本次三组在线漏洞扫描；同名前缀 `initial-error.log` 保留初次解码失败 |
| `fourth-readonly-environment.json` | 75 包版本、三锁 SHA256 与既有环境比较 |
| `fourth-readonly-probes.py/.log` | A01/A01b、A02 模型/知识状态、A04 同游标交错 |
| `fourth-readonly-http-probes.py/.log` | A02 网络异常类型、A03 实际慢头部及四槽耗尽和清理 |
| `third-fix-external-gates-5c9a3a7.pending.json` | E01 的 pending 模板，不能作为 passed 证据 |

本轮最终变更仅本报告。没有为了让审计测试通过修改夹具或应用，也没有对发现的问题实施修复。
