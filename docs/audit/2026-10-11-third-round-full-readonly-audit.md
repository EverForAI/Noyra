# Noyra 第三轮全项目只读审计

审计日期：2026-10-11（Asia/Shanghai）

分支：`codex/admin-optimization-20261009`

审计开始时交付提交：`3e818eceeb48eec120a601f7de076aeaf8327e1e`

被审计应用提交：`d6f689ff3e1d380fbb143b6eda0be7986ced9eac`

数据库合同：schema 92

本轮遵循最新要求，只读检查代码、测试、工作流、依赖锁和既有证据；没有修改应用、配置、依赖、测试或生产数据。聊天删除功能继续关闭，`ConversationErasure.request()` 继续抛出 `private_deletion_disabled` 拒绝新请求，没有重新增加删除会话、删除原文或清除记忆的入口。只读探针使用临时数据库、合成凭据和本地模拟服务，未连接真实供应商、QQ、云端、钱包或生产服务器。

## 结论

本轮确认 **8 项顶层问题或验收缺口：3 项 P1、4 项 P2、1 项 P3；未确认 P0**。其中 A01–A06 是当前应用或部署边界的实现问题，E01 是发布所需的真实环境证据缺口，E02 是本地验证环境与锁文件不完全一致的质量缺口。它们不表示已经发生凭据窃取、消息泄露、资金损失或生产中断；报告中的确定性结果只表示满足触发条件后，隔离探针稳定复现了相应行为。

上一轮已经修复并在当前应用提交上复核的模型、浏览器、钱包、前端登录/公开视图和 Windows 备份问题，不在本报告重复列为未修复。上一轮确认的依赖漏洞已经将三组锁中的 urllib3 固定为 2.8.0；本轮没有证据表明锁文件又回退。全根 Ruff 0.16.2 检查和格式检查也在当前树通过。真实 Linux/systemd、LUKS、双机迁移、KMS/链、HTTPS 代理和长期 soak 仍不能由 Windows 本地回归替代。

| 编号 | 问题 | 风险 | 修复风险 | 建议 |
| --- | --- | --- | --- | --- |
| A01 | SMTP 登录发生在最终发送授权复核之前 | P1 | 中 | 立即修复；真实邮件启用前阻断 |
| A02 | botocore DEBUG 日志可记录会话令牌、签名和请求信息 | P1（条件性） | 中 | 立即修复或禁用云 DEBUG |
| A03 | GitHub 升级元数据只有 socket 空闲超时，没有整次操作截止时间 | P2 | 中 | 近期修复 |
| A04 | common knowledge 同步在租约失效后仍可继续下一页外发读取 | P2 | 中至高 | 近期修复，暂停/迁移前完成 |
| A05 | HTTP 请求体使用 idle timeout，慢速 drip 可超过整请求预算并占用 worker | P2 | 低至中 | 近期修复；公网直连前完成 |
| A06 | SMTP DNS/连接操作没有总截止时间，取消会等待后台线程退出 | P2 | 中 | 近期修复 |
| E01 | 当前应用 SHA 未获得八项真实环境签名门禁 | P1，发布阻断 | 操作中至高 | 生产发布前必须补齐 |
| E02 | 本地验证 venv 与 requirements-dev.lock 有 8 个版本差异 | P3 | 低 | 可暂缓；下一次整合验证前固定环境 |

P0 表示有充分证据支持的紧急严重损失或广泛可利用问题，本轮未确认；P1 表示外发凭据、私密输入或生产发布边界可能失效；P2 表示在配置、并发、慢网络或暂停竞态下影响正确性、可用性或停止语义；P3 表示验证可重复性缺口。修复风险描述修改本身可能带来的回归、终态和兼容风险，不是当前问题的严重程度。触发概率采用条件描述，不虚构线上事故百分比。

## 审计范围与方法

本轮沿服务 HTTP 入口、SMTP/通讯发送、日志记录工厂、升级检查、公共知识同步、请求读取、鉴权/会话、钱包/预算、导出/恢复、迁移、保留、前端异步状态和 CI/发布工作流复核。重点查看“检查之后是否等待”“真正写请求头之前是否再次授权”“取消/暂停后的终态是否可结算”“第三方日志是否绕过脱敏”“外部证据是否绑定同一 SHA”。

当前受跟踪清单为 `src/noyra` 208 个文件、`tests` 220 个文件。既有应用回归证据对应应用提交 `d6f689f`：2276 passed、56 skipped、259 subtests passed、0 failed，耗时 4333.37 秒；全根 Ruff check/format（605 个文件）、Mypy 396 文件、Node 69 项和 pip check 通过。当前审计新增专项回归为 122 passed（`tests=122`）和 119 passed、6 skipped（`tests=125`），无失败。跳过项是本机缺少 POSIX root、服务账户、Linux 所有权或其他主机条件，不能当作 Linux 门禁通过。

新增探针日志和证据摘要位于已忽略的 `.runtime/third-reaudit-3e818ec-probes.log`、`.runtime/third-reaudit-3e818ec-evidence.json`；它们只保存布尔值、计数、错误类型和耗时，不保存秘密。探针脚本为 `.runtime/third-reaudit-3e818ec-probes.py`，不属于应用交付。

| 模块 | 本轮覆盖与证据 | 不能外推的结论 |
| --- | --- | --- |
| 服务、会话与身份 | Bearer/角色、cookie/CSRF、持久失败预算、subject ownership、入站账号验证；当前专项及服务源码 | 不证明真实 HTTPS/代理头及平台签名已通过 |
| 模型、embedding、预算与资源 | 最终发送钩子、密钥/组撤销、费用估算和预留、unknown、逐次重试；上一轮整仓加本轮发送/聚合预算回归 | 不证明真实模型延迟、质量、价格或供应商计费 |
| API/浏览器搜索及公共知识 | 发送 trace、路由版本、响应限额、终态、分页/签名/key trust；A04 两种 probe | 公共知识读取不等于私密查询；不把公开 feed 的外发上升为正文泄露 |
| 通讯、入站与私密关联 | token 锁/刷新、配置/凭据快照、账号准入、SMTP、unknown；A01/A06 及投递/身份专项 | 无真实 QQ/邮件、无手机同步体验验证 |
| 公开页面、投稿与验证码 | 转义、四视图世代、UTF-8 容量/字符数、事务内当前配置、并发、验证码模式；现有 Node 与本轮投稿专项 | 无公网负载/攻击测试；A05 只实测合成登录 slow body |
| 私密聊天、记忆及删除取消 | 关闭新请求、保留原文/来源、主体归属、旧 schema 迁移和历史收据恢复；disabled/schema71 专项 | 不重新开放清除功能，不推断历史跨机旧备份已具备完整删除合同 |
| 钱包、采集、付款与奖励 | 策略/实际 worker 同步、费用/nonce、receipt/reorg、unknown 预留；上一轮整仓加发送和费用专项 | 无真实付款、RPC/KMS 或资金安全认证 |
| 生命周期、睡眠与完整性 | gate/epoch、后台上下文、失效事务、硬超时、关闭/排空；公共同步和 SMTP 例外单独记录 | 不评判主体意识或人格质量，不保证全部 worker 可立即停止 |
| schema、保留、容量与日志 | schema92、不可变证据、路由计数、清理游标、保留设置 CAS、脱敏工厂；保留/用量/日志专项 | 必须保留账务和审计，不能删除证据来解决容量；云 SDK 日志另有 A02 |
| 归档、导出、训练与备份 | S3 SDK 队列/池、密文、路径、主体归属、同意撤销、快照、流式备份身份；复用相同树整仓及导出专项 | 无真实云端和低内存/大备份演练；不把主机同账户写权限假设自动当成远程漏洞 |
| 迁移、root 激活与升级 | 单活、回滚证明、schema71、SHA/签名、runner、metadata/read timeout；专项与源码/工作流 | Linux root、双机和真实 runner 排空仍待 E01 |
| 构建、依赖及发布 | 四个 workflow、锁 hash、三组扫描旧证据、当前固定 Ruff、解释器差异、外部门禁拒绝合同 | 不重新在线查询漏洞库，不把不同工具版本结果称为严格锁环境结果 |

这是按入口、跨模块合同和高风险边界进行的全面复核，不是逐行形式化证明；没有直接反例的模块只能写“本轮未确认新增问题”。新确认的 A01/A03–A06 没有充分历史对比证据，不能直接称为上一轮修复引入的回归。部分网络/超时遗漏也可能是长期存在的未覆盖边界。

## 详细发现

### A01：SMTP 登录发生在最终发送授权复核之前

- **风险等级：P1。** SMTP 认证是一次独立外部交换，依服务器选择的认证方式传递用户名、密码或认证派生材料。当前路径在登录后才执行 `authorize_send()`；暂停运行、停用通道或更换密码之后，仍可能对旧端点开始认证。消息正文随后可能被拒绝，但认证交换无法撤回。探针确认的是 `login()` 在撤销之后被调用，没有用假客户端证明真实服务器选择了哪种 AUTH 机制。
- **修复风险：中。** 把检查移到登录前需要保持 TLS/证书/公共 DNS 固定、SMTP 真实终态和 `unknown` 语义；若把已建立的 SMTP 连接一律改成取消，会错误丢失已经发送或服务器已接受的结果。
- **根因：** `src/noyra/interaction/transport.py:2023-2085` 的 `_send_email()` 依次执行连接、peer 校验、STARTTLS、`client.login()`，然后才调用 `authorize_send()`。相比之下，HTTP 通讯路径在请求头 trace 点有最终复核；SMTP 没有等价的登录前复核。
- **影响：** 配置了 SMTP 用户名/密码的 email transport。密码更换、通道停用和运行租约失效都会影响这个窗口；不使用 SMTP 认证或已经完成发送的邮件不触发该具体问题。它不表示密码会被发往任意第三方，端点仍是管理员配置并经 DNS/peer 检查的服务器。
- **触发条件：** 任务先通过 `before_send()`；连接或 TLS 建立期间控制状态发生变化；后台继续执行 `login()`。本轮在 fake SMTP 的 connect 窗口执行真实 gate 失效、真实通道停用或合成凭据替换；稳定观察到 `login_after_withdrawal=1`，而 `message_sends=0`。TLS 窗口按相同源码顺序推断，尚未单独故障注入。
- **触发概率：** 平稳运行时低；管理员在连接慢、凭据轮换、停用或暂停期间操作时为条件性并发窗口。没有真实 SMTP 时序，无法量化线上比例。
- **证据：** `.runtime/third-reaudit-3e818ec-probes.log` 中 `smtp_lease`、`smtp_disable`、`smtp_credential` 三行。探针使用合成用户名、密码和 loopback DNS 结果，没有真实网络。
- **建议：立即修复。** 连接、STARTTLS 和登录之间以及登录前都执行固定授权快照的复核；撤销后不得再开始凭据交换。登录成功后再失效时，应按“已交换、尚未发送”处理并保存真实失败/未知终态。增加租约、停用、凭据替换、TLS、登录失败和连接异常回归。

### A02：botocore DEBUG 日志可记录会话令牌、签名和请求信息

- **风险等级：P1（条件性）。** 云归档是可选配置，且通常使用 INFO；当 Noyra 或 `botocore` 日志被设为 DEBUG 时，SDK 日志可以包含临时会话令牌、Authorization 签名、访问密钥标识和对象 URL。凭据材料进入更多日志读取者的范围，违反秘密保护合同。探针没有发现 secret key 明文，不据此声称日志读取者能签署任意 AWS 请求或扩大 IAM 权限。
- **修复风险：中。** 第三方 logger 的全局降噪不能破坏云归档诊断；需要在 SDK 边界清除敏感字段、限制日志级别或统一结构化安全出口，并验证多 handler、异常和 SDK history recorder。
- **根因：** `src/noyra/core/runtime_logging.py:62` 的 LogRecordFactory只处理 `noyra*`、`httpx*` 和 `httpcore*`，没有处理 `botocore*`、`boto3*` 或 `urllib3*`。`src/noyra/service.py` 启动时允许用户把 root logging 调到 DEBUG。当前 botocore 的 `auth.py:437-441` 记录 CanonicalRequest、StringToSign 和 Signature，`endpoint.py:265` 记录请求对象；这些记录绕过 Noyra 的脱敏工厂。
- **影响：** 启用 S3/cloud profile、临时 credentials 或 DEBUG logger 的实例。探针没有发现合成 secret key 明文进入这两个 handler，但发现 access id、session token、Authorization、签名和对象路径；S3 对象本身仍按加密归档合同上传，日志问题不等于归档明文泄露。
- **触发条件：** cloud profile 已安装并创建 boto3 client；日志级别为 DEBUG 或单独启用 `botocore` DEBUG；执行一次 S3 请求。满足条件后实际 botocore client 使用 mock endpoint，`mock_endpoint_sends=1`、`session_token_in_log=true`、`authorization_in_log=true`、`signature_in_log=true`。
- **触发概率：** 默认 INFO 且未单独打开 botocore DEBUG 时低；调试 cloud 故障、临时启用详细日志或自定义 handler 时明显升高。日志是否被非服务账户读取取决于 journald/采集/备份权限，无法由本机估计。
- **证据：** `.runtime/third-reaudit-3e818ec-probes.log` 的 `botocore_debug` 与 `botocore_actual_endpoint_debug`；只输出布尔值，不输出日志原文或凭据。
- **建议：立即修复或在 cloud 正式启用前阻断 DEBUG。** 默认拒绝 botocore/boto3/urllib3 的原始请求日志；若需要诊断，只输出服务名、方法、状态、耗时和脱敏 endpoint origin。增加临时 session token、Authorization、错误响应、history recorder 和多 handler 验收，并单独评估既有生产日志是否需要轮换凭据。

### A03：GitHub 升级元数据只有 socket 空闲超时

- **风险等级：P2。** 升级检查读取元数据时，`urlopen(timeout=8/10/15)` 的 timeout 是 socket 连接/读空闲超时，不是整个 body 或多个请求的总截止时间。慢速 drip 可以让单个检查持续超过界面和服务预期，稳定占用 HTTP worker；stable 通道还会连续读取 release、commit、workflow 和 evidence。
- **修复风险：中。** 必须把总预算传到每次读取和多个 stable 请求，同时保留严格 JSON、响应大小、SHA、签名和错误码；过短的统一预算会误报正常 GitHub 网络，过长又不能解决资源占用。
- **根因：** `src/noyra/core/upgrade.py:171-195` 在 development-main 直接 `urlopen(..., timeout=8)` 后 `read(65_537)`；`src/noyra/core/release_assurance.py:112-123` 和 `:174-179` 使用 10/15 秒的同类模式。HTTP handler 在 `src/noyra/service.py:3082-3102` 同步调用 `owner.upgrade_manager.check_version()`，没有把 inbound request 的总截止时间传给 outbound fetch。
- **影响：** 已鉴权的管理台升级检查、升级 root runner 使用的远端元数据读取。它不会绕过稳定版本签名、固定 SHA 或 runner 文件检查，也不是任意远程代码执行。
- **触发条件：** 远端或代理返回状态后，以低于 socket idle timeout 的间隔持续发送小片 body；管理台或 runner 同时等待检查。loopback slow-drip fixture 在单字节间隔 650ms 时，名义 10 秒 timeout 下实际耗时约 11.1 秒并成功完成，证明没有 wall-clock 截止。
- **触发概率：** GitHub 正常响应时低；代理故障、恶意中间层或网络极慢时条件性发生。公网是否允许管理端点以及 worker 负载决定实际影响，无法量化。
- **建议：近期修复。** 建立跨多个 metadata 请求的绝对 deadline；每次 `read()` 使用剩余时间，超时返回固定 `upgrade_remote_connect_failed`/`upgrade_unavailable`，并保证 response 关闭。增加 development/stable 慢 drip、超限 body、429 和签名验证回归。

### A04：common knowledge 同步在租约失效后仍继续下一页读取

- **风险等级：P2。** common knowledge 是签名的公开协作数据，不包含私密聊天正文或 provider secret；但暂停、隔离或迁移后继续向 peer 发起下一页读取会违反停止/资源预算语义，并在恢复、排队和网络压力下浪费 worker。签名、可信 key 和导入准入仍会复核，当前没有证据表明撤销 key 的指导被接受。
- **修复风险：中至高。** 同步是多页流程，必须在每次 discovery/feed 前后检查租约，并区分已经完成的公共读取、已验证但未导入的事件和可安全结算的游标。粗暴回滚 cursor 会造成重复读取；任意过期写旁路会削弱迁移保护。
- **根因：** `src/noyra/knowledge/common.py:1052-1158` 的 `sync_peer()` 没有内部 checkpoint；外层 `sync_due()` 在入口、peer 之前和成功之后复核，却没有在 discovery 与 feed 各页之间复核。`src/noyra/knowledge/sync.py:98-163` 的 HTTP client 自带 body deadline，但它不是 runtime lease 检查，也没有最终发送头授权 hook。数据库事务仍使用当前租约提交围栏，所以本轮外发之后没有成功导入。
- **影响：** 已开始的同步 peer 的 discovery/feed 请求和后续本地验证工作。影响是公开协议资源与停止语义；不是私密输入外发或凭据泄露。若 trusted key 同时被撤销，`import_package()` 仍要求 key active，不能把本问题写成已接受恶意知识。
- **触发条件：** discovery 已开始；运行 gate 随后失效；同步继续调用 feed。合成 client 的结果为 `feed_after_invalidation=1`、`imported=0`、最终捕获 `OperationInvalidated`；HTTP client fixture 还显示实际请求 trace 未安装，失效后仍能返回 payload。
- **触发概率：** peer 有多页数据、同步时间较长且恰逢暂停/迁移时为条件性；单页、无待同步数据时低。没有生产 peer 数量和网络时序，无法量化。
- **建议：近期修复，暂停/迁移前完成。** 在 discovery、每一页 feed、验证/导入和游标提交之间复核 lease；为公共读取定义允许的短暂 drain 边界，并保证失效后不启动新页。终态记录必须不绕过签名、trusted key、subject ownership 和 cursor CAS。增加暂停、peer disable、key revoke、分页、超时和重启恢复回归。

### A05：HTTP 请求体只有 idle timeout，慢速 drip 可超过总请求预算

- **风险等级：P2。** `request_timeout_seconds` 通过 `socket.settimeout()` 限制每次读的空闲时间，不限制从请求头到完整 body 的总时间。攻击者或故障代理可以持续每隔一小段时间发送一个字节，使一个 worker 长时间占用；服务有 32 个 worker 上限，因此并发慢请求会造成管理台和公开端点不可用。
- **修复风险：低至中。** 增加请求开始时间和剩余 deadline 需要兼容合法大 JSON、错误响应和 keep-alive；过短的总预算会误伤正常上传，过长又不能防 slowloris。现有 `max_request_bytes` 和鉴权不能替代时间预算。
- **根因：** `src/noyra/service.py:821` 配置 `request_timeout_seconds`，`_Handler.setup()`/请求接收路径在约 `:2547` 设置 socket idle timeout；`_request_json()` `:8808` 按 Content-Length 直接读取 body，没有传入绝对截止时间。`BoundedThreadingHTTPServer` 在 `:1296` 虽限制 worker 数量，但满载时只返回忙状态。
- **影响：** 需要读取请求体的登录、管理台 POST/PUT、webhook 和公开写端点；响应大小和 JSON 结构仍有其他边界。探针只验证合成 `/admin/session`，不表示所有端点同样容易达到上限。
- **触发条件：** 每个 body 字节间隔小于 idle timeout、总 body 接收时间超过配置值；本地配置 1 秒，每 40ms 发送一个字节，约 2 秒慢 drip 最终得到 401，`remained_open_past_timeout=true`。401 正确拒绝了合成无效凭据；问题在于拒绝前接收 body 占用的总时间未受该配置限制。
- **触发概率：** 普通浏览器低；公网直连、没有反向代理 body timeout 或遭遇 slowloris 时中等，具体比例无法估计。受保护内网且有代理总请求 timeout 时风险降低。
- **建议：近期修复，公网直连前完成。** 为 header、body 和 handler bridge 使用统一绝对 deadline；超时关闭连接并释放 worker，保留最大字节和 CSRF/鉴权合同。验收慢头、慢 body、最大合法 body、keep-alive、并发 32+ 和 webhook 认证失败。

### A06：SMTP DNS/连接没有总截止时间，取消会等待后台线程

- **风险等级：P2。** `_run_blocking()` 用 `asyncio.to_thread()` 执行 SMTP 同步库，取消时会等待真实 worker 退出。`SMTP_TIMEOUT_SECONDS=20` 只覆盖 SMTP socket 的单次操作；公共 DNS 解析和整个连接/TLS/登录链路没有统一总预算。慢解析或慢 drip 会延迟停机、资源释放和 delivery 终态。
- **修复风险：中。** 不能强制杀 Python 线程；需要把绝对 deadline、可中断 DNS/连接或受控 worker 池、关闭 socket 和 `unknown` 终态结合起来。过早标记失败会导致重复邮件，过晚又会拖住关闭。
- **根因：** `src/noyra/interaction/transport.py:1023-1047` 的 `_run_blocking()` 在取消路径循环等待 worker；`:2048-2065` 先做同步 `_PublicDNSBackend._public_addresses()`、`connect()`、STARTTLS 和 login；这些操作没有统一 deadline 传递。
- **影响：** email delivery worker、服务关闭和暂停 drain。它不改变已经发送邮件的真实性，也不会自动导致重复发送；重复风险取决于调用方是否错误地把未知结果重新排队。
- **触发条件：** DNS 或 SMTP server 在每个 idle timeout 内持续响应；任务被取消或服务关闭。按实际 20 秒 socket timeout 的 loopback slow greeting，每字节间隔 850ms，取消后仍等待约 **23.7 秒**，完整 worker 约 **23.8 秒**。先前缩短为 120ms 的同机制探针也观察到约 422ms 取消等待，两者均保留；不会把缩短参数冒充为生产 20 秒实验。没有制造长期阻塞或真实服务停机。
- **触发概率：** 正常 SMTP 低；外部邮件服务器故障、DNS 异常或停机排空时为条件性，无法量化。
- **建议：近期修复。** 为解析、连接、TLS、认证和发送共享绝对截止时间；超时后关闭 client、释放 worker 并准确记录 failed/unknown。增加取消、服务关闭、DNS 慢、登录前撤销和发送后断连回归，不能以强制线程终止伪造成功。

## E01：真实环境八项门禁仍未完成

- **风险等级：P1，发布阻断。修复风险：操作中至高。** 本机证据不能确认 Ubuntu/systemd、加密卷、备份恢复、双机迁移、signer/KMS、reorg/nonce、HTTPS 代理和 24/72 小时 soak 在当前应用 SHA 上通过。
- **根因：** `scripts/verify_external_gates.py` 固定要求八个 gate、独立 reviewer、同一 40 位 SHA、Ed25519 签名和 72 小时新鲜度；当前只有 pending/unsigned 模板。`.github/workflows/external-gates.yml` 和 `docs/release/external-gates.md` 的合同没有被真实环境证据满足。
- **影响及触发条件：** 生产升级、自动迁移、自动付款或长期公网运行前，缺证据会使发布验证拒绝；若绕过验证直接部署，真实环境特有的问题仍未知。旧服务器健康、重启或 8 小时 soak 不能替代 schema 92 当前 SHA。
- **触发概率：** 缺少证据是确定的；真实事故概率无法估计，因为没有当前目标环境样本。
- **建议：生产发布前必须补齐。** 固定最终安装 SHA，按八门禁执行，使用不同执行人和 reviewer，保存脱敏证据并签名。禁止手填 `passed`、复用旧 SHA、把 unsigned pending 模板当作通过或在本轮只读审计中部署。

## E02：本地 venv 与开发锁版本不完全一致

- **风险等级：P3。** 当前工作区的验证解释器不是严格由 `requirements-dev.lock` 重建的环境；比较发现 8 个版本差异：`anyio`、`coverage`、`cryptography`、`idna`、`pydantic`、`pydantic-core`、`pytest`、`ruff`。这会降低“本机专项在锁定依赖上通过”的可重复性，但 `pip check` 通过，不是已确认的产品漏洞。
- **修复风险：低。** 在隔离目录按锁文件重建并重新执行必要静态/专项测试；不应为了对齐而修改源代码、放宽哈希或直接改生产环境。
- **根因及证据：** `.runtime/third-reaudit-3e818ec-environment-diff.json` 由锁文件和 `importlib.metadata` 只读比较生成。当前实际 Ruff 为 0.16.9，锁定版本为 0.16.2；本轮另从 uv 缓存调用固定 Ruff 0.16.2，605 文件 check/format 均通过。
- **影响及触发条件：** 仅影响本机验证可重复性；CI 工作流明确按锁文件安装，生产安装脚本也使用受哈希约束的锁。若将本机专项结果直接宣称为锁定环境结果，则结论过强。
- **触发概率：** 当前差异确定存在；是否改变测试结果无法从现有证据推断。作为发布质量问题低，作为严格可复现实验缺口确定。
- **建议：可暂缓，但下一次整合回归前修复。** 使用干净锁定 venv 重跑 focused/cross、Mypy、Node、pip check 和必要的全仓回归，报告同时保留当前非锁环境证据；不要把版本差异改写成应用修复。

## 未发现新增阻断的模块与边界

- **聊天删除：** 新删除入口仍关闭；没有清除窗口、原文、记忆、派生域或恢复标记。此前私密删除六类完整合同阻断继续有效，不能因为本轮审计而恢复删除功能。
- **模型、embedding、API 搜索和钱包：** 当前应用提交上的最终发送授权、策略版本、费用/nonce/unknown 和资源撤销专项没有在本轮发现超出历史报告的新直接反例。本轮没有真实付费调用、签名或链上广播。
- **日志旧问题：** Noyra/httpx/httpcore 新日志出口已按上轮合同脱敏；本轮新增的是 botocore/boto3/urllib3 第三方 logger 范围，不应把 A02 说成旧修复完全失效。
- **依赖锁：** 当前锁文件固定 urllib3 2.8.0；既有证据中的三条 2.7.0 漏洞属于上一轮基线，不应在本轮重新列为当前锁漏洞。在线漏洞数据库未来变化仍需在发布时重新扫描。
- **前端会话和公开视图：** 上轮 generation/action owner 修复仍在当前应用树；本轮没有重新发现迟到旧响应覆盖新登录的同一反例。

## 建议修复顺序与安全边界

1. **先处理 A01/A06（SMTP 边界）。** 把凭据交换、正文发送、取消、unknown 和服务关闭分别定义清楚；先做合成 SMTP 回归，确认撤销后不再 login，已发送结果不被伪造为未发送，再提交。
2. **处理 A02（云日志）。** 在云 SDK logger 边界建立最小诊断合同，验证临时令牌、签名、请求对象、异常和多 handler；在 cloud DEBUG 没有通过前不要启用生产调试日志。
3. **处理 A03/A05（总截止时间）。** 先建立可复用的 wall-clock deadline，再接入升级多请求和 HTTP body；避免分别添加互相冲突的超时。对合法大请求和 GitHub 429/超限进行回归。
4. **处理 A04（公共同步租约）。** 为每一页读取和游标提交加入 lease checkpoint；只允许明确的公共 drain，保留签名、trusted key、subject ownership 和 cursor CAS。暂停/迁移回归通过后再进入长期运行。
5. **固定锁环境复验 E02。** 以干净 requirements-dev.lock 环境重新执行必要专项和静态门禁，保留旧环境与新环境的真实差异。
6. **最后执行 E01。** 以最终实际安装 SHA 在隔离 Linux/代理/testnet/KMS 环境完成八项门禁，并由独立 reviewer 签名。没有 E01，不能宣称生产发布或全部优化完成。

每个模块的阻断条件是：任何未授权的凭据/正文/查询/签名请求、丢失已发生费用或 unknown、跨主体写入、错误恢复游标、无限 worker 占用或证据 SHA 不一致。发现其中任一项应停在当前模块，不以“最终健康”为理由继续推进。

## 验证证据与限制

本轮新增专项命令和结果：

| 检查 | 结果 |
| --- | --- |
| transport/浏览器/钱包/删除/发布/迁移 focused tests | 122 passed，0 failed |
| 账户、身份、预算、帖子、用量、保留、SMTP、迁移 cross tests | 119 passed，6 skipped，0 failed |
| 固定 Ruff 0.16.2 `check .` | 通过 |
| 固定 Ruff 0.16.2 `format --check .` | 605 files already formatted |
| 真实 botocore mock endpoint 日志探针 | 会话令牌、Authorization、签名和对象路径均可见；未连接外网 |
| SMTP 撤销/停用/凭据替换探针 | 三种场景均 `login_after_withdrawal=1`，正文发送为 0 |
| GitHub slow-drip body 探针 | 名义 10 秒 idle timeout 下耗时约 11.1 秒，证明没有 wall-clock 总截止 |
| common knowledge 租约探针 | 失效后继续 feed 1 次；无公共网络，导入计数 0 |
| HTTP slow body 探针 | 配置 1 秒、约 2 秒 drip 仍最终处理，超过整请求预算 |
| SMTP 原生 20 秒 idle timeout/取消探针 | 工作线程约 23.8 秒结束；取消调用等待约 23.7 秒才返回 |
| `pip check` | 通过；不等于严格锁环境一致 |

既有整仓证据、锁 SHA 和外部验收合同见：

- [第三轮修复执行记录](../roadmap/2026-10-10-dispatch-audit-remediation.md)
- [外部验收交接](../release/2026-10-10-dispatch-remediation-external-acceptance.md)
- [第二轮修复后审计](2026-10-10-post-remediation-full-readonly-audit.md)
- [全项目只读审计](2026-10-10-full-project-readonly-audit.md)

本报告没有进行真实 SMTP、S3、GitHub、QQ/微信、KMS、区块链、Linux systemd 或公网代理验收；没有读取生产密钥、发送第三方消息、付费调用或交易。没有任何本地回归结果可以替代 E01 的签名证据，也没有足够生产样本计算 A01–A06 的线上事故概率。

本地测试明细：

```text
focused:
test_runtime_logging.py, test_transport_dispatch_fencing.py,
test_browser_dispatch_fencing.py, test_wallet_dispatch_fencing.py,
test_private_erasure_disabled.py, test_release_assurance.py,
test_legacy_schema71_upgrade.py, test_m42_p1_06_export_ownership.py,
test_m42_p3_02_common_knowledge_sync.py
122 passed in 205.53s

cross:
test_admin_session_durable.py, test_identity_challenges.py,
test_gate1_aggregate_budget.py, test_wallet_fee_admission.py,
test_public_post_capacity_contract.py, test_usage_projection.py,
test_retention.py, test_migration_storage_boundary.py,
test_m42_p2_04_p2_05_delivery_reconciliation.py
119 passed, 6 skipped in 284.89s
```

两批均为 `.venv/Scripts/python.exe -u -m pytest -q -p no:xdist`、UTF-8 环境，不是锁环境新建安装。日志/完整 XML 为 `.runtime/third-reaudit-3e818ec-focused.log/.xml` 和 `.runtime/third-reaudit-3e818ec-cross.log/.xml`。复用的整仓日志/完整 XML 为 `.runtime/dispatch-full-d6f689f.log/.xml`；较早 `7ec7574` 的整仓因发现 Windows 备份问题中断于 61%，四个失败标记仍保留，不能改写成该次整仓通过。后续 `d6f689f` 完整回归才是上述 2276 通过证据。

当前交付 HEAD 与应用提交的 diff 只有三份旧文档，应用/测试/工作流/锁一致；新增本报告不会改变受测应用树。`.runtime` 证据不随 Git 克隆，需独立保留。本报告不以修改旧报告或删除失败日志的方式改变历史结论。

审计收尾没有发现仍在运行的本轮测试或 loopback probe server；临时夹具已自动清理。报告是唯一新增的受跟踪文件，既有报告和修复记录保持历史原貌。当前源码问题仍未修复，E01 仍是发布阻断，不能将“审计完成”表述成“系统全部通过生产验收”。
