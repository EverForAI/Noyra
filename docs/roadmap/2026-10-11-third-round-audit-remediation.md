# 2026-10-11 第三轮审计修复执行计划

依据：`docs/audit/2026-10-11-third-round-full-readonly-audit.md`。本计划只覆盖当前审计确认的本地实现问题；聊天删除继续关闭，保留原文、记忆、历史收据和恢复防复活保护。

## 顺序与安全边界

1. **SMTP 外发（A01、A06，P1/P2）**：先修复认证前最终授权复核和 DNS/连接/认证/发送的统一绝对截止时间。边界是既不在撤销后开始新的认证交换，也不把已经发出或结果未知的邮件当成可安全重试；继续保留公共 DNS、TLS 主机名与对端固定、Message-ID、`DeliveryOutcomeUnknown` 和真实终态。
2. **云 SDK 日志（A02，条件 P1）**：在 botocore/boto3/urllib3 日志边界做字段级降噪和脱敏，保留服务、方法、origin、状态、耗时等诊断信息。不得把原始 Authorization、session token、请求头、签名、响应正文或秘密异常交给日志 handler；不清理历史日志、不擅自轮换生产凭据。
3. **升级元数据（A03，P2）与请求体（A05，P2）**：建立可复用的绝对 deadline 原语，并分别约束 GitHub 多请求序列与 HTTP header/body 接收。保留 JSON/大小/鉴权/CSRF/keep-alive 合同，合法请求仍可完成；超时不得留下无界后台工作或在请求结束后继续触发新副作用。
4. **公共知识同步（A04，P2）**：在 discovery、每页 feed、验证、导入、游标提交之间增加租约检查；失效后不启动新页，仍保留签名、可信 key、subject ownership、cursor CAS 和公共数据语义，不把公开同步问题扩大解释为私密内容泄露。
5. **锁环境与最终本地验证（E02，P3）**：建立忽略的精确 `requirements-dev.lock` 环境，使用它执行专项回归、静态检查和必要的整合回归；不为了迁就当前 venv 改锁文件。保留失败历史和环境差异证据。
6. **外部发布交接（E01，P1 发布阻断）**：生成绑定最终应用 SHA 的八项真实环境验收清单和 pending 证据模板。不得伪造签名、Linux/systemd/LUKS、双机迁移、KMS、HTTPS 代理或 soak 结果；未取得独立签名前保持发布阻断。

## 每个模块的完成门槛

- 先用隔离夹具复现根因，再实现最小修复；不绕过授权、事务围栏、签名、计费、unknown 或恢复保护。
- 运行针对性回归、静态检查和跨模块审计，确认失败语义、取消、超时、重试、日志和资源收尾没有回归。
- 复审工作树和差异后单独本地提交，再进入下一模块；提交信息说明问题、边界和验证证据。
- 最终报告明确本地通过范围、跳过的主机条件及 E01 外部阻断；完成前不得宣称全部问题已生产验收。

## 不在本轮改变的合同

- 不增加聊天/记忆删除入口，不清除聊天原文，不修改历史删除原型和恢复防复活逻辑。
- 不推送、合并、部署、真实平台发消息、付费模型/搜索调用或资金交易。
- 不把旧整仓证据冒充新代码的整仓通过；只在有新变更且资源允许时执行对应范围的回归。

## 模块一：SMTP A01/A06 与锁环境 E02

提交 `9136747`：认证前、认证多轮挑战/机制回退的每次实际写入以及 DATA 写入前均核验冻结授权与运行租约。DNS 使用共享有界 resolver；SMTP 从排队前开始计算绝对截止时间，最多四个实际 daemon worker，槽位直到线程真实退出才释放。取消/截止通过 socket shutdown 中断读写，不等待 buffered reader 的 close 锁，也不占用 asyncio 共享 executor。连接完成但计时器先到的窗口由 greeting read 前再次检查封住。已确认 DATA 结果不再等待 QUIT；超时或正文阶段无法证明未发仍记 unknown，不自动重发。

验收：锁环境 101 项通讯/SMTP/对账/身份与删除禁用回归通过（260.31 秒）；公共 HTTP 边界 8 项通过；补充晚连接、真实慢 greeting、DNS 取消和 worker 上限 4 项通过。Ruff check/format、4 文件 Mypy 通过。首次 quiet 运行输出缓冲、未生成完整结果，已中断保留；第二次专项中发现测试将 DATA 阶段撤销期望为 failed，但既有合同应为 unknown，修正期望后上述完整回归通过。没有把中断运行计为通过。

复审：保留 TLS hostname、公共 DNS/peer 固定、Message-ID、unknown 对账、权限错误终止重试和聊天删除关闭。Python 线程/DNS 无法被强杀，迟到 worker 仍保留容量槽位且每次新外发前拒绝取消/过期；已开始的网络写入无法撤回。无 schema 变更，无生产操作。

E02：建立忽略目录 `.runtime/locked-venv`，按 `requirements-dev.lock` 与 `requirements-cloud.lock` 使用 hash 安装，再 no-deps/no-build-isolation editable 安装应用。75 个锁定依赖版本逐一匹配，差异 0，pip check 通过；未修改依赖锁。证据 `.runtime/third-fix-locked-environment.json`、`third-fix-smtp-final.log/xml`、`third-fix-smtp-http.log/xml`、`third-fix-smtp-late.log/xml`。

## 模块二：云 SDK 日志 A02

提交 `8ce137d`：在记录工厂中对 botocore/boto3/urllib3/s3transfer 只保留固定安全事件、日志类别、等级、异常类型与栈位置。未知 SDK 消息不尝试按当前模板猜测敏感字段；原始参数、签名、请求和响应不进入 handler。云客户端创建（含凭据发现）前以及注入真实 SDK client 时安装独立 history recorder 的白名单出口，后续开启 history/增加 handler 仍只能得到固定事件、HTTP 方法和状态。可选 cloud 包没有变成基础 profile 的必需依赖。

实际锁定 botocore 签名、S3 200/403 模拟端点、多 handler、异常、history、归档与外部边界共 42 项通过（7.45 秒）；模块 Ruff/format/Mypy 通过。源码复审保留既有云归档加密、readiness、worker 上限及 unknown 合同；没有清理历史日志或轮换生产凭据。证据 `.runtime/third-fix-cloud-final.log/xml`。历史日志是否存在暴露仍需获授权的环境核查，不能由新日志回归推断过去没有泄露。

## 模块三：升级元数据 A03 与 HTTP 接收 A05

提交 `3c3d38f`：用 ContextVar 传递单调时钟绝对截止时间。HTTP 请求行、headers、JSON、webhook、discard 共用 read1 原始读取包装器，每次 socket 读取消耗剩余预算，慢滴数据不会重置预算。嵌套升级读取及异步 handler bridge 继承该预算；bridge 超时仍保留取消后实际排空/主体锁合同，迟到启动前再复核。普通同步业务函数不被强杀，已经进入的副作用不能因请求超时伪装成未发生。

升级 metadata 改为公共 DNS 固定的有界 HTTPX 读取，stable 序列及最多六次重定向共享预算；DNS、TCP、TLS、每次原始读写（包括不完整响应头）均检查剩余时间，发头/发体 trace 拒绝迟到 worker。保留官方入口、HTTPS、大小、严格 stable JSON、SHA、签名、CI、新鲜度及 404/429/403 错误码。CLI 验证也受 30 秒 metadata 序列预算；不通过缩短整个升级安装服务的 TimeoutStartSec 中断数据库升级/备份。

验收：初轮截止时间/升级/签名 42 通过；增加真实 httpcore 头部慢滴和公共网络边界后 48 通过；错误码 5 通过；最终小范围复审 25 通过；异步取消与正常登录补充 2 通过。服务、安全 profile、持久 session、入站和身份、升级部署/workflow 交叉回归 **140 通过、1 跳过、8 子测试通过**（635.72 秒）。跳过 POSIX token 权限是 Windows 条件限制。9 文件 Ruff/format/Mypy 通过。证据 `.runtime/third-fix-deadlines*`、`third-fix-deadline-raw*`、`third-fix-deadline-cross*`、`third-fix-request-bridge-final*`。

复审：没有修改鉴权/CSRF/消息内容、schema、发布门禁或聊天删除合同；原始网络包装器仅对明确传 remaining 的同步 transport 启用，其他现有消费者保持兼容。主机真实 HTTPS/代理和 Linux 权限仍交由 E01，不以本地 loopback TLS 替身证明生产证书验收。

## 模块四：公共知识同步 A04

根因是外层后台任务的租约检查没有覆盖 discovery、分页、验证和导入内部的等待。现在同步前后、每页前后、逐事件验证后、导入事务和完成记录均复核；实际 HTTP 发头/发体前同时检查剩余预算、运行租约和当前 peer/key。peer 的 endpoint、publisher、key、公钥或 active 状态发生变化，或者其他 worker 已推进游标，旧任务都会停止。HTTP 使用有界 worker 和原始网络剩余时间；自建 client 随实际 worker 退出关闭，不在超时后由主线程强行关闭其他在途请求共享的 client。

安全边界：公开知识仍须验证签名、来源、公钥信任和主体归属，并进入隔离等待主体评价；不扩大为私密聊天同步。package、import、provenance、签名事件及游标推进改为同一事务，继续使用原有运行租约提交围栏。失效不通过 accounting_scope 绕过保护；已开始的公共网络读可短暂排空，不能声称撤回了已发送数据。schema 保持 92。

复审补强：旧失败任务必须携带自己的最后已提交游标，不能重新读取另一个 worker 的新游标并覆盖其成功状态。新增交错同步用例验证成功状态、etag、调度时间和审计记录均不被迟到失败覆盖；验证导入后事务失败不会留下无游标的 package/import。

验收：公共知识、公共 HTTP/外部边界、schema71 升级、聊天删除禁用交叉 **98 项通过**（206.24 秒）；扩大私密来源/导出合同专项 **30 项通过**（80.73 秒）；补强并发结算后最终公共知识 **27 项通过**（61.38 秒）。这些集合有重叠，不相加作为整仓数量。证据 `.runtime/third-fix-knowledge-cross.log/xml`、`third-fix-knowledge-final.log/xml`、`third-fix-knowledge-reviewed.log/xml`。最终全根 Ruff check/format **615 文件通过**，Mypy **403 文件通过**；格式检查曾发现一个混合换行文件，统一格式后复验通过。

## 最终本地结论与外部边界

| 审计项 | 本轮状态 | 仍须注意 |
| --- | --- | --- |
| A01 / A06 | SMTP 授权与总截止时间本地修复、专项通过 | 真实 SMTP/TLS 和 Linux 取消需 E01 |
| A02 | 云 SDK 日志及 history 脱敏本地修复、专项通过 | 不推断历史日志无暴露，不擅自清理或轮换 |
| A03 / A05 | 升级元数据、请求接收与 handler bridge 预算本地修复、专项通过 | 已进入的同步业务副作用不会被强杀或伪装为未发生 |
| A04 | 公共知识停止、导入原子性及游标结算本地修复、专项通过 | 真正双机迁移仍待环境验收 |
| E02 | 精确锁环境建立、75 包差异 0、pip check 通过 | 环境位于忽略目录，不随代码交付 |
| E01 | **仍为发布阻断**，交接清单已准备 | 八项真实环境签名证据尚未取得 |

三组依赖锁分别扫描均为 `No known vulnerabilities found`；扫描有 Windows 缓存写入权限警告，但扫描完成，未修改锁文件。前端 Node **69 项通过**。本轮采用针对变更的专项和交叉回归，不把上一轮整仓 2276 通过、56 跳过的结果算作本轮整仓通过。最终提交再用 `scripts/verify-committed.py` 的隔离检出执行静态门禁和明确列出的边界测试，机器可读结果保存在 `.runtime/third-fix-committed-verification.json`；该结果只覆盖其命令和 tests 列表。

外部执行细节见 `docs/release/2026-10-11-third-round-external-acceptance.md`。最终提交后生成绑定完整 SHA 的 pending 模板；模板不是签名证据，也不会被发布验证器接受为 passed。本轮未推送、合并、部署、调用真实平台/付费供应商或进行交易。聊天删除入口继续关闭，聊天原文和记忆保留。
