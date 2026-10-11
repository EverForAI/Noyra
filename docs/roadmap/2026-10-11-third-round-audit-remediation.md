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
