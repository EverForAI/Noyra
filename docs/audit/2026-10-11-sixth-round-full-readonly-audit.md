# Noyra 第六轮全项目代码级只读审计

审计日期：2026-10-11（Asia/Shanghai）

分支：`codex/admin-optimization-20261009`

被审计应用提交：`3f9193477fb93b7acee92e1c3c8b497491c745e8`

数据库合同：schema **92**

## 授权、结论和证据口径

本轮按最新人类请求执行全项目代码级只读审计，并评价紧邻本轮的 A02、A04、A05、E02 修复。没有修改应用、测试、配置、依赖或生产数据；仅新增本报告。隔离探针及回归日志存于忽略的 `.runtime`，使用临时数据库、合成身份/凭据/签名、模拟连接或本地 loopback。没有使用真实供应商、平台、云账户、签名器或资金，没有推送、合并、部署，也没有启用聊天删除。依赖扫描查询公开漏洞数据库。

**当前确认 6 项未关闭的问题或验收缺口：1 项 P1、4 项 P2、1 项 P3，未确认 P0。** 其中 A03、E01 为前轮保留；A06 是上一轮 A04 修复引入的增长型性能回归；E03 是上一轮 A02 改变异常合同却遗漏旧测试更新的全仓门禁失败；A07、A08 是本轮新确认的旧缺陷。沿用前轮编号，避免把同一问题重复计数。

**上一轮四项修复的功能目标在本地证据中有效，但不能评价为完全无回归或验收闭环完成。** A02 改善了发送前错误分类，A04 挡住迟到完成覆盖，A05 规范了管理登录/Bearer/CSRF 非 ASCII 输入，E02 补齐实际 cloud SDK 测试门禁。A04 使用反复 `COUNT(*)` 作为完成代次，带来了已经复现的长期历史扫描放大；A02 的关联批次遗漏一处旧合同测试，当前整仓及单项复验均确定失败。功能正确性验证较充分，容量验证和全仓门禁整合不足。没有确认上一轮新增鉴权绕过、隐私删除、计费释放、重复外部发送或数据损坏。此结论受本地边界和未执行真实环境验收限制。

| 编号 | 当前问题或验收缺口 | 风险等级 | 修复风险 | 建议 | 与上一轮修复的关系 |
| --- | --- | --- | --- | --- | --- |
| A03 | embedding 超时后不完整响应头仍占用共享同步 HTTP 工作槽 | P2，可用性 | 中至高 | 近期优先；反复慢端点时立即安排 | 已知保留，未由上一轮引入 |
| A06 | 公共知识完成围栏反复扫描同主体全部同步历史 | P2，增长型可用性 | 中 | 大历史/多 peer 环境优先；小历史可短暂暂缓 | **由 A04 提交 `9edb36a` 引入** |
| A07 | 记忆向量没有绑定内容版本和 embedding 资源空间 | P2，认知检索正确性 | 中 | 切换 embedding 资源或修订已索引内容前修复 | 新确认旧缺陷，早于上一轮 |
| A08 | 非 ASCII 图片版本参数/部分入站凭据仍触发未捕获 TypeError | P3，错误响应/排障 | 低至中 | 可暂缓至下一次输入健壮性模块 | 新确认旧缺陷，A05 未覆盖这些入口 |
| E01 | 当前精确 SHA 的八项真实环境签名验收证据未取得 | P1，发布阻断 | 操作中至高 | 正式发布/生产自动化前必须完成 | 已知保留；不是代码修复回归 |
| E03 | 网络 typed error 改动遗漏旧测试更新，整仓与发布全门禁失败 | P2，交付/回归合同 | 低 | **立即修复测试合同，发布前必须通过** | **由 A02 提交 `95f244b` 引入** |

P0 表示有充分证据支持的紧急严重损失或广泛可利用问题；P1 包括生产发布安全证据缺口；P2 指特定配置、负载、网络或并发条件下的正确性/可用性；P3 指低风险错误处理。**修复风险独立于问题严重性。** E01 的 P1 不表示当前已经发生生产事故。

**概率口径：** 没有生产故障统计和负载样本，不编造百分比。每项区分“给定条件后的确定性反例”和“条件在真实部署出现的未知频率”。性能数字来自本机隔离数据库，不是生产 SLA，也不能反推生产已经受影响。

## 审计覆盖、方法与限制

受跟踪清单共 **862 个文件**：`src/noyra` **213** 个、`tests` **227** 个、`scripts` **87** 个、`deploy` **17** 个、`.github` **4** 个、`docs` **192** 个，另有根目录锁和构建文件。枚举源码入口、存储合同及消费者，逐代码块审阅高风险路径，结合差异归因、隔离反例、静态门禁、前端回归和当前提交 Python 整仓回归。

“代码级”表示结论落实到实际条件分支、事务、索引和调用链；**不表示每个文件每一行都经过形式化证明，也不声称穷尽所有缺陷。** 历史文档仅用于定位及比对，当前确认问题以当前代码和重新运行的探针为准。没有把旧 SHA 的结果移称为当前整仓通过。

| 领域 | 实际审阅重点 | 当前结论与验证边界 |
| --- | --- | --- |
| 服务、HTTP、鉴权与日志 | 请求解析/总预算、并发容量、源头访问日志、Bearer/角色、持久会话、CSRF、失败限速、Unicode 输入、版本路由 | A02/A05 目标保留；新增旧边界 A08。没有确认权限扩大。真实 HTTPS/代理链在 E01 |
| 管理台及公开页面 | `admin.js`/`app.js` 的请求 generation、退出失效、视图切换、草稿 revision、预算精度、转义、验证码与公开图版本缓存 | Node 69 项通过，公开图异常输入单列 A08；没有进行完整手机/真实浏览器视觉验收 |
| 私密交流与入站身份 | 窗口/主体/principal 归属、授权历史、QQ 显式同意、入站签名与去重、撤销路由、删除禁用、旧删除恢复 | 新删除仍拒绝，未清除聊天或记忆。兼容恢复不等同新功能开放；真实平台专项未执行 |
| 模型、资源、账本及路由 | 发送前密钥/整组/lease 复核、每次重试准入、预留及未知用量、价格来源、总池预算、结构化结果、证据容量 | A02 的 known-unsent/unknown 边界保留；未确认新增预算绕过或未知费用被释放 |
| embedding 与记忆检索 | gateway 总预算/worker、资源授权、向量维度/哈希、候选上限、内容修订、provider namespace、missing/rebuild/完整性 | 保留 A03，新增旧缺陷 A07；有限性与向量自身哈希不足以证明语义新鲜性 |
| 搜索、浏览器、世界源与能力 | 公网 DNS 固定、TLS hostname、实际发送复核、取消/排空、响应容量、源可信度、路径授权与相对句柄 | 没有确认新增 SSRF/路径授权绕过；同步工作槽被慢连接占用的影响仍在 A03 |
| 公共知识 | 签名/可信 key、peer 配置与调度、逐事件 import/cursor 原子性、同游标/ABA、迟到失败历史、SQL 执行计划 | A04 的终态覆盖目标关闭；其反复历史计数带来 A06 |
| 通讯、SMTP 与投递 | 平台 token 获取、凭据快照、推送同意/revision、最终发送围栏、unknown、SMTP AUTH 多轮及原始 I/O 截止 | 未确认新增错误发送/重发。部分入站非法 Unicode 为 A08；没有发送实际平台消息/邮件 |
| 生命周期、疲劳及完整性 | admission epoch、暂停/隔离、单进程所有权、后台任务排空、startup/periodic 检查预算、状态与行为历史区分 | 本地围栏和现有检查合同保留；状态频率和认知质量不由无负载单元测试证明 |
| 钱包及采集 | 最终 signer dispatch、权限/费用额度预留、nonce/重组/unknown、链与地址绑定、只读 RPC、结果对账 | 本地合成合同覆盖；真实 signer/KMS/链为 E01。RPC 默认原始 I/O 总预算与 A03 类似，未单独滴头复现 |
| schema、保留与容量 | schema71→92、迁移注册/触发器、受保护证据、row counter、公共投稿 UTF-8/字符限制、WAL/导出快照 | 没有通过删账本或审计缓解容量；本轮未改 schema。生产规模和 Linux root 检查不能替代 |
| 导出、训练与归档 | 主体所有权图、同意版本、快照、取消/发布原子合同、工作/归档容量、密钥和实际 SDK 日志/history | E02 本地门禁有效；实际 cloud 使用、凭据轮换和存储可恢复性仍待环境验收 |
| 备份、迁移与升级 | 加密/manifest/tar 边界、路径/临时文件、备份恢复、源 epoch/目标证据、失回包保持围栏、撤销/恢复、安装 root 边界 | 未确认新增恢复复活或切换证明绕过；只读代码及合成回归，未执行 root 安装/双机迁移 |
| 构建、依赖、CI 与发布 | 三组 hash 锁、安装版本、全根 Ruff/Mypy、实际 SDK JUnit 必需案例、release 外部签名/SHA/freshness | 静态/依赖通过，整仓旧期待失败列 E03；远程 GitHub runner/artifact/保护环境没有执行，不解除 E01 |

## 当前提交验证证据

环境为 `.runtime/locked-venv/Scripts/python.exe`，Python **3.12.10**、Windows 11；导入路径是本工作区 `src/noyra`。75 个锁定包版本差异 **0**，`pip check` 通过。Ruff **0.16.2**、Mypy **1.20.2**、HTTPX **0.28.1**、httpcore **1.0.9**、boto3/botocore **1.43.72**、urllib3 **2.8.0**、Node **v24.15.0**。环境及清单见 `.runtime/sixth-readonly-environment.json`。

- 全根 `ruff check .` 通过，`ruff format --check .` **625 个文件**通过。
- 默认 `mypy src tests` **408 个文件**通过；日志 `.runtime/sixth-readonly-mypy.log`。
- `admin.js`、`app.js` 语法检查通过；Node 前端 **69 通过、0 失败、0 跳过**；日志 `.runtime/sixth-readonly-node.log`。
- 基础、基础+cloud、dev 三组锁均在线扫描成功，最终为 `No known vulnerabilities found`，退出码 0。cloud 首次公开漏洞响应不是有效 JSON，重试完成；最初未启用 `PYTHONUTF8` 的扫描遇到中文路径解码错误，启用后完成。记录这些环境/数据源失败，不算应用缺陷，也不把“没有已知漏洞”称为不存在未知漏洞。
- **当前提交整仓 Python 串行回归：2432 passed、56 skipped、259 subtests passed、1 failed、0 errors，退出码1，耗时4765.44秒（1小时19分25秒）。** 仅启动一份整仓任务，日志 `.runtime/sixth-readonly-3f91934-full.log`，JUnit `.runtime/sixth-readonly-3f91934-full.xml`，退出记录 `.runtime/sixth-readonly-full-exit.txt`。2489个顶层测试；JUnit把259个子测试也计入，总数2748，不写成2748个独立顶层测试。未用历史的 `5c9a3a7` 整仓成功替代本提交。
- 唯一失败为 `tests/test_m42_p2_06_p2_07_provider_security.py::test_public_dns_is_pinned_for_model_and_embedding_redirects_and_retries`：旧 `OSError` 期待不再匹配 A02 的 `PublicEndpointRejected`。整仓结束后原样单项复验仍为 **1 failed，2.43秒，退出码1**，不是时间预算偶发失败，不能称串行复验通过。另在忽略探针中仅替换三处旧期待，保留其余断言并校验原cause，完整逻辑通过；这是根因诊断，不是受跟踪测试已修或整仓通过。详见 E03。
- 重新运行真实存储/客户端探针：A02 的同步/异步 DNS 和模拟连接故障为 typed `ConnectError`；私网地址模型失败为 known-unsent、0 实际 token/成本、0 TCP 尝试；公共知识两次私网拒绝均留下失败完成历史和退避；迟到失败不移除新成功 ETag、不改变新调度；A01 私密 query/原始 ESC 不进入访问日志。
- 当前隔离反例确认 A03、A06、A07、A08；E03由整仓及单项回归确定确认。不是自动修复，也没有往受跟踪测试添加新用例。临时数据库及服务器完成清理。

56项跳过均有本地环境原因，没有因为缺 boto3 而跳过的案例：

| 跳过类型 | 数量 |
| --- | ---: |
| 实际Linux root/release和服务账户合同 | 20 |
| 独立非特权服务账户迁移存储 | 6 |
| POSIX权限、所有权、marker、token和路径合同 | 21 |
| POSIX root安装器验证 | 1 |
| Windows 8.3别名不可构造 | 4 |
| 本机/Windows symlink夹具不可构造 | 2 |
| Bash安装器恢复 | 2 |

跳过不是验收成功，不将Windows回归移称Linux root通过。实际合成cloud SDK200/403两项均执行且通过。

三组锁摘要保持不变：

| 锁 | SHA256 |
| --- | --- |
| `requirements.lock` | `81d242aa1cbb5b4a5c3c0354e6b1f5ef37d901809fe01700bc14a1f1fd06b78f` |
| `requirements-cloud.lock` | `4aafa2bedfc749e3c82964ffe3fa8807e7b4cfd8017fe88d70dbdd2b0fd5e7b0` |
| `requirements-dev.lock` | `e93467e673d93a4464b909526798a98551cede0b035403946b680cf694c7ef31` |

## 未关闭问题详述

### A03：embedding 超时后原始读头 worker 不退出，共享同步容量被耗尽

**风险等级：P2，可用性。修复风险：中至高。置信度：高。建议：近期优先修复；出现反复慢端点/超时或工作槽堵塞时立即安排。**

**根因与代码：** `model/embedding.py:276` 默认 `PublicDNSHTTPTransport()` 没有传入 raw stream 的 `remaining` 总预算。发送 trace 只在发送请求头前检查；`SyncHTTPTimeoutHook` 要到完整响应头返回后才能注册 response。对端在每次 idle timeout 内发少量不完整头，httpcore 持续 read；外层 `call_sync_http_with_deadline()` 虽按时返回，却不能关闭尚未注册 response 的读头工作。`core/http.py:29` 的全局四槽 gate 按真实 worker 退出释放，这是必要的真实容量边界；消费者没有保证原始工作在预算内退出。

**影响：** 调用者得到 timeout，真实连接/worker 仍活着；四槽耗尽后，不相关的同步 HTTP 操作排队超时，影响 release/common 等共用执行边界。发送后的 embedding 必须保留 unknown 预留，不能借此释放预算。本轮没有证明多扣款、重复发送、进程崩溃或私网访问。

**触发条件与反例：** 在独立探针进程替换 DNS/TLS 为 loopback，真实 HTTPX/httpcore/parser/socket 读取本地端点每 25ms 一字节的不完整头。4 次 1 秒调用返回 `embedding_timeout`，耗时约 1000–1016ms；实际请求 **4**、仍存活 `noyra-sync-http` worker **4**。无关同步操作未运行并得到 TimeoutError。停止合成服务器后 worker 为 **0**。日志 `.runtime/sixth-readonly-http-probes-corrected.log`。

**触发概率：** 正常完整头时低；慢连接一次只占一槽，四个符合时序的调用在途时可确定耗尽。故障/恶意已配置公网端点、并发调用和持续间歇字节提高概率；真实供应商此模式频率未知。本地 DNS/TLS 替换仅隔离 parser 测试，不是生产地址保护失效的证据。

**相关范围：** `wallet/rpc.py:86`、`wallet/local_rpc.py:66` 默认 transport 同样不带 `remaining`；wallet 外层 deadline/response hook 不等于原始不完整头退出保证。本轮对这两处只进行了代码审阅，未单独执行钱包滴头反例，不扩写成已经复现资金故障。

**修复安全边界与验收：** 总预算须到 DNS/TCP/TLS/每次原始 read/write，而不只到 response 正文。共享 client 要使用请求级预算，不能把单次 deadline 写成所有并发请求共享的 mutable 字段。保留公网 IP 固定、原 hostname TLS、最终授权复核、gate 真实退出和未知计费；不能靠扩槽、提前释放 gate、杀线程、自动重试已发送调用掩盖问题。验收应包含四个滴头超时后的有限排空、后一无关操作成功、排队/DNS/TLS/正文/取消及 known-unsent/unknown 账务。修复风险来自共享网络流和双终态时序，不能顺便改变重试/计费合同。

**归因：** 前轮已知并明确未授权修复。上一轮 embedding 仅补 ConnectTimeout 分类，没有改 raw I/O 寿命，因此不是上一轮新回归。

### A06：A04 完成代次围栏引入反复全历史计数，长期增长拖慢同步

**风险等级：P2，增长型可用性。修复风险：中。置信度：高。建议：已有大历史/多 peer 或同步耗时上升时优先；小历史可短暂暂缓，但应在长期运行前解决。**

**根因与代码：** `knowledge/common.py:1068` 的启动快照和 `:1688` 的 `_assert_sync_peer()` 均用 `COUNT(*) WHERE peer_id=? AND subject_id=?` 表示完成代次。检查不仅在开始/结束，也在网络 checkpoint、逐事件导入等处执行。`core/database.py:4224` 只有 `(subject_id, occurred_at DESC, sync_id)` 索引，没有 peer 前导键；SQLite 因而扫描**同主体全部完成记录**后过滤 peer。`common_knowledge_sync_runs` 是 append-only，正常运行和迟到失败都会累积，不能按年龄直接删除。

**影响：** 围栏功能本身正确，但一次同步的成本约随检查次数 × 同主体历史行数增长，无关 peer 的历史也放大当前 peer。逐事件检查在 `BEGIN IMMEDIATE` 事务中执行，会延长 writer 锁持有时间；`service.py:10457` 正常活跃 tick 在后续钱包/投递/认知前等待 `sync_due()`，因此可拖慢相关工作。没有在反例中复现状态覆盖、重复导入或数据损坏，也没有实测生产影响。

**当前反例：** 使用真实 schema92、store 和合法合成 hash 完成记录，给**另一个 peer**增加历史，保留所有触发器。查询计划为 `SEARCH ... USING INDEX idx_common_knowledge_sync_runs_subject (subject_id=?)`。

| 其他 peer 的历史行数 | 当前 COUNT 的中位耗时 | 空同步耗时 | SQLite VM 步数平均值 |
| ---: | ---: | ---: | ---: |
| 0 | 0.005ms | 130.244ms | 1,800 |
| 1,000 | 0.080ms | 148.397ms | 6,800 |
| 10,000 | 6.496ms | 173.533ms | 51,800 |
| 60,000 | 42.025ms | 342.461ms | 301,800 |
| 120,000 | 87.183ms | 542.344ms | 601,800 |

在 12 万条无关历史下，一次 **20 个签名事件**同步执行了 **47 次 COUNT**，耗时 **5032.401ms**，结果 succeeded/cursor20。只在该临时数据库加入比较用 peer/subject 覆盖索引后，同一 COUNT 约 **0.033ms**、低于1000 VM steps；没有修改项目 schema。VM 统计按1000步采样并含准备开销，不能把小值解读为零 SQL 工作。性能数字受本机同时回归负载影响，**执行计划、工作量随历史线性增长及无关 peer 放大**才是稳健证据。日志 `.runtime/sixth-peer-generation-probe.log`。

**触发概率：** 给定大量同主体完成历史，此查询扫描是确定的；是否达到对部署有影响的规模/时间，取决于 peer 数、同步频率、数据盘和并发写负载，目前未知。既有默认间隔允许较频繁同步，历史长期单调增长；小数据库短测很难暴露。签名/权限保护不改变本地历史扫描成本。

**修复边界：** 保留同游标、固定时钟、相同 ETag 的 ABA 围栏，保留迟到失败历史和 import/cursor 原子事务。可设计持久化单调 completion token 或具有严格顺序/索引保证的最后完成身份，让复核工作量与历史大小解耦；所有 writer/恢复/迁移路径必须保持同一合同。peer/subject 索引可缓解无关历史放大，但 COUNT 仍随本 peer 自身历史增长，**单加索引不能宣称彻底完成容量修复**。不能删除完成记录或绕过历史触发器来凑速度。

**修复风险与验收：** 中风险，涉及持久化围栏、schema/旧数据初始化、CAS 和恢复。应在10万/百万级本 peer 及其他 peer 历史下证明有界检查；复测迟到成功/失败、304/空 feed、固定时钟ABA、cursor/key/配置/停用变化、进程重启/独立 store、schema71→当前及导出备份恢复。不跨网络持有写事务，不弱化可信 key 复核。

**明确归因：** `git diff 398059d..3f91934` 显示完成 COUNT、逐 checkpoint 和事务内比较均由 **`9edb36a` 的 A04 修复**新增；数据库索引未随之调整。先前记录已经提到“COUNT 依赖 subject 索引”，但没有规模反例。这是本轮唯一确认由紧邻上一轮修复引发的应用行为回归；另有 A02 遗漏测试更新的门禁问题 E03。原 A04 的覆盖错误确实已修好，不能因此把原修复全部称作无效。

### A07：语义向量缺少内容/资源版本绑定，修订或换资源后长期使用旧索引

**风险等级：P2，认知检索正确性。修复风险：中。置信度：高。建议：在切换 embedding 资源/维度或修改已索引记忆内容前修复；配置和内容固定时可短暂暂缓。**

**根因与代码：** `mind/retrieval.py:80` 在 embedding 返回后仅检查主体；SELECT 虽取到 `content_hash` 却未比较提供的内容。`rebuild_missing():123` 在网络计算后写向量，不复核读取的内容是否仍为当前版本。`missing():110` 只检查是否存在或 `e.provider != provider.name`；schema `core/database.py:2978` 没有记忆内容 hash/revision 或资源空间 fingerprint。`mind/memory.py:167` 的合法 revise 更新内容与 current_revision，没有标记该向量失效。

同时 `model/embedding.py:222` 的 name 只有 `openai-compatible:{model}`，`EmbeddingGateway.name` 原样转发，未包含 endpoint、resource ID、指定维度或空间版本。不同供应商/资源使用相同模型别名时被认为同一索引。`similarities():166` 校验向量哈希和长度，但不确认与当前内容/空间一致；`verify_integrity():228` 及 `core/integrity.py:3312` 只验证向量自身合法，不能发现以上错配。

**影响：** 当前记忆正文没有回退或删除，主体过滤仍在；错误的是派生语义排序。旧内容向量会让新内容对旧主题得高分，真正相关记忆排名下降；切换同名不同维度资源会丢失所有这些语义分数。`cognition/cycle.py:353` 的活跃周期确实调用 `rebuild_missing()`（每批最多32条），但现有 missing 条件认为没有缺失而不更新，不能靠继续等待周期自行恢复。同维度不同空间甚至会用不相容坐标计算 cosine，不能以“维度一样”证明兼容。词法/其他权重仍工作，不能把该项夸大成全记忆失效或跨主体泄漏。

**触发条件与隔离反例：**

1. 合成 provider 将 energy 文本映射 `[1,0]`、无关文本映射 `[0,1]`。真实 MemoryStore 创建/索引后，合法 revise 把同一记忆内容改成无关主题、revision2。`missing=[]`、`rebuild=0`；energy 查询仍得 **1.0**，按当前内容应为 **0.0**，向量完整性仍验证1行。把合法 revise 放在 embedding 回调期间同样复现 stale write。日志 `.runtime/sixth-memory-revision-probe.log`。
2. 实际 OpenAIEmbeddingProvider + MockTransport：资源A/model别名/2维建索引，切资源B/同别名/3维。两个 name 相同，`missing=[]`、`rebuild=0`、`semantic_scores={}`，完整性仍验证1行。没有连接真实供应商。
3. 再换资源C/同别名/同2维但不同空间。旧向量得 **0.0**，按新空间重建后同文本自相似应 **1.0**，仍 missing/rebuild 为0。日志 `.runtime/sixth-embedding-space-probe.log`。

**触发概率：** 没有索引或无相关变更时不触发。对已索引内容执行上述 revise、或启动时切同名模型资源/维度，夹具确定复现；内容在 embedding 等待中变更的频率取决于并发。普通整合/巩固中仅调整 salience/status 不必重算内容向量；不把所有 revision 更新都算作触发条件。真实部署使用 embedding 及切换频率未知。

**修复安全边界：** 派生索引需保存其源内容 hash 和非秘密 embedding 空间身份；资源空间至少考虑实际模型、endpoint/resource 语义及维度，不能把API key原文放进索引。读取、计算、写入要用当前内容/空间的 CAS；迟到结果不能覆盖更近的新向量，查询只能使用匹配索引。仅 salience/access/status 变化应避免无意义付费重嵌入。旧索引缺少来源的升级策略须显式标记 stale，不能猜来源。按有界批次/现有预算重建，并保留已发送计算的实际费用及 unknown 预留，即使最终向量被弃用。

**修复风险与验收：** 中风险：schema兼容、历史索引失效、恢复/导出和重建成本。验收应覆盖顺序/在途 revise、旧worker迟到、相同/不同模型名、同维/异维/资源切换、restart、预算/费用/取消/lease、跨主体、旧schema迁移。不得修改记忆原文/修订证据、移除删除禁用、删除受保护历史或放宽私密授权。

**归因：** `git blame/log` 显示相应索引/修订逻辑在上一轮前已存在，`398059d..HEAD` 没有修改 `mind/retrieval.py`、`mind/memory.py`、embedding provider name 或 schema。属于新发现的旧缺陷，**不是 A02/A04/A05/E02 引发**。

### A08：公开缓存参数与入站鉴别的其他 compare_digest 仍不接受非 ASCII

**风险等级：P3，错误响应/健壮性。修复风险：公开缓存低，入站鉴别低至中。置信度：公开HTTP/两适配器高，其余分支为代码证据。建议：可暂缓至下一次输入健壮性模块；异常请求/连接错误增多时提前。**

**根因与代码：** `service.py:9429` 直接比较 `parse_qs` 解码后的公开 hero `v` 参数与 ASCII hash；Python `hmac.compare_digest(str,str)` 对非 ASCII 抛 TypeError。`interaction/inbound.py:376` Telegram header、`:441` Feishu verification token 同样直接比较未经ASCII约束的字符串。服务 webhook `:9063` 只将 ValueError/NotFoundError 转为400，未捕获此 TypeError。微信 GET challenge `service.py:9156` 也有同形比较，`:9187` 未覆盖 TypeError。

同类静态分支还包括 `interaction/inbound.py:210` 通用 HMAC 的候选签名、`:252` 飞书签名、`:271` 微信明文签名、`:684` 微信加密回调签名，以及 `:792` 解密后的微信账户字符串比较。这些分支各有时间窗、配置或解密前提，不表示任意请求都能走到；本轮没有逐一执行平台端到端反例，仍并入同一输入健壮性问题而不重复计数。

**影响：** 对这些合法编码但非法业务输入没有确定的非成功响应，公开图片/相应 webhook 可断开连接；会干扰排障，并可能使发送方重试。未突破签名/凭据校验、未接受消息、未改变角色。没有复现单次导致服务停机，也不能仅凭该项证明持续DoS。

**触发条件与证据：** 本地真实服务：hero `v=wrong` 正常200/no-cache；percent-encoded 中文或 Latin-1 字符解码成非ASCII时 `RemoteDisconnected`，捕获2次 TypeError，随后 health/live200。Telegram/Feishu 适配器使用合成非法Unicode输入分别抛 TypeError，而非 InboundAuthenticationError。当前日志 `.runtime/sixth-nonascii-boundary-probe.log`。微信相似分支本轮只静态确认，没有实测真实平台。

**触发概率：** 原生成hash和正规平台ASCII token不触发；构造请求或误编码/误粘贴输入可确定触发。公开图片无需鉴权；webhook需到对应已配置渠道比较分支。真实异常输入频率未知。

**修复安全边界与验收：** 公开hash缓存可先验证16位小写hex，不匹配使用原no-cache；不要通过Unicode规范化把非法输入变成匹配。入站按各平台协议验证格式/bytes后constant-time比较，非法输入映射原401/400，保留时间窗、签名、去重/绑定和密钥配置边界，不吞掉真正完整性异常。不能全局粗暴替换所有 compare_digest。补测ASCII正常/错误、Unicode/空/长度/重复参数、平台签名bytes路径及健康连续可用。

**归因与 A05 的区别：** 上一轮A05明确只改管理登录、Bearer、actor和CSRF/退出五处；这些目标有效。公开图比较 `git blame` 归于更早 `e36cf643`，入站逻辑也在上一轮前；上轮diff没有改它们。**这是范围之外的旧同类缺陷，不是新回归；不能把A05的局部关闭写成整个项目所有字符串比较已无问题。**

### E01：精确 SHA 的八项真实环境签名发布证明未取得

**风险等级：P1，发布阻断/验收缺口。修复风险：操作中至高。置信度：可用证据缺口明确，未知外部状态不作推断。建议：正式发布或启用受门禁生产自动化前必须完成，隔离开发可继续。**

**根因：** 当前工作区/本聊天没有提供绑定 `3f9193477fb93b7acee92e1c3c8b497491c745e8` 的八门禁真实执行、独立审阅和签名产物。既有历史 `artifacts/release`/`output`、旧服务器重启/8h soak不覆盖当前代码及schema。没有访问远程服务器、GitHub私有运行或外部证据库，不能断言所有外部地点都不存在证据。

**影响：** 本地无法证明实际systemd/root、加密卷、双机、签名器/KMS、链、TLS代理和长观察合同。现有 `core/external_gates.py`、`core/release_assurance.py`、release workflow 应拒绝缺失、错SHA、过期/未签名证据，这是应保持的保护，不是应删掉的错误提示。

| 门禁 | 仍需实际证据 |
| --- | --- |
| `ubuntu_systemd` | 当前候选干净安装/升级、启动/停止/重启、readiness、失败回滚、root/服务账户边界 |
| `encrypted_volume` | 实际加密映射/权限、密钥可用、未解锁启动保护、解锁与恢复 |
| `backup_restore` | 冷备份、认证/摘要、干净环境恢复、schema和服务可用性 |
| `migration_fence` | 双机源写围栏、目标证明/恢复/健康、切换、失回包、持久取消和失败回滚 |
| `signer_kms` | 独立签名器/KMS绑定、拒绝访问、轮换及恢复 |
| `reorg_nonce` | 隔离测试链nonce冲突、广播unknown、超时、确认/重组对账 |
| `https_proxy` | 实际TLS、可信代理头、鉴权/限速及代理/CDN/日志转发全链隐私 |
| `soak` | 既定24/72h合同的存储/WAL/供应商/钱包/生命周期证据及逐样本结果，不能以旧8h/失败样本替代 |

**触发条件与概率：** 正式发布/受门禁功能启用而缺同SHA证据时，现有保护应确定拒绝。绕过后的事故概率无法由本地推算，依赖环境/故障；本轮未发生生产操作。八项真实环境仍未运行，不能从整仓绿色推导通过。

**修复安全边界与验收：** 在隔离一次性主机/测试链/受控资源执行，有明确恢复步骤；不同执行者与审阅者独立确认，artifact只含非秘密引用，Ed25519验证当前候选SHA、签名、全部门禁及72h新鲜度。已安装有效SHA不因仅时间流逝停机。不得伪造passed、沿用旧SHA、放宽独立审批、启用真实付款或修改生产数据凑证据。最终候选源码SHA变化后重新绑定相应证明。

**归因：** 前轮保留的环境限制。E02补cloud测试并没有取得E01，也没有移除阻断；二者不可混称全部发布验收完成。

### E03：A02 typed 网络异常变更遗漏旧测试合同，当前整仓门禁失败

**风险等级：P2，交付与回归可用性。修复风险：低。置信度：高。建议：立即修复测试合同；在测试更新并重新取得当前 SHA 的整仓绿色前，禁止把 CI/release 质量门称为通过。**

**根因与代码：** A02 提交 `95f244b` 将公共地址策略拒绝从普通 `OSError("...non-public...")` 规范化为 `PublicEndpointRejected`（其上层包装为 `httpcore.ConnectError`，并保留原始 OSError cause），以便消费者区分发送前的不可重试拒绝。该变更更新了 `tests/test_gate2_external_boundaries.py` 的局部断言，却遗漏了 `tests/test_m42_p2_06_p2_07_provider_security.py:311,313,321` 三个旧 `pytest.raises(OSError, match="non-public")` 断言。

**证据：** 当前提交整仓实际结果为 2432 passed、56 skipped、259 subtests passed、1 failed、0 errors，失败唯一为 `test_public_dns_is_pinned_for_model_and_embedding_redirects_and_retries`；结束后的同一测试单独运行仍为 1 failed/2.43秒。隔离诊断只将这三处期待替换为 `httpcore.ConnectError` 并检查 `PublicEndpointRejected` 及其原始 OSError cause，三次策略拒绝、DNS pinning 和 redirect 阻断全部通过；该探针在 `.runtime/sixth-old-expectation-probe.py/.log`，没有修改 tracked 测试。

**影响：** 现有产品网络安全行为仍拒绝私网解析、固定连接地址、阻断重定向；这个失败是回归门禁不一致，导致 CI 的 `pytest`、发布前质量门和任何依赖全仓零失败的自动流程会拒绝当前提交。它不能被解释为策略回退，也不能用“测试期望旧异常”直接忽略：A02 的新异常类型需要所有消费者、测试和文档统一合同。没有证据表明该项造成鉴权绕过、私网访问、重复发送或账务错误。

**触发条件与概率：** 每次从当前 SHA 执行整仓 pytest 都确定失败；仅运行 A02 的局部测试或未执行该文件时不会暴露。修复测试期待本身的代码风险低，但必须重新跑受影响网络/模型/embedding回归和当前 SHA 的整仓门禁，避免把断言机械改成宽泛 `OSError` 而掩盖真正错误。

**修复安全边界与验收：** 只更新测试到公开 typed error 合同，保留 cause、不可重试分类、真实地址 pinning、TLS hostname、redirect 禁止和发送前计费/unknown边界；不要把实现改回普通 OSError，也不要捕获所有 `ConnectError` 作为策略拒绝。验收为该测试通过、相关 `test_public_http_errors`/`test_gate2_external_boundaries` 通过、全根 Ruff/Mypy/Node及当前 SHA 整仓回归重新通过；真实 Linux/外部八门禁仍单独记录。

**归因：** 明确由上一轮 A02 的 `95f244b` 引入，属于测试合同遗漏，不是 A03/A06/A07/A08 的应用行为问题。

## 上一轮修复质量与回归归因

评价范围为第五轮报告后 **`398059d..3f91934`**，四个模块提交依次为 `95f244b`（A02）、`9edb36a`（A04）、`998a069`（A05）、`3f91934`（E02）。差异共14个文件、1104行增加/69行删除，包含修复记录及测试；schema和依赖锁未改。历史其他优化不混入本次因果判断。

| 模块 | 原目标复核 | 安全/连锁边界 | 质量评价 |
| --- | --- | --- | --- |
| A02 | 默认/注入DNS、私网策略、同步/异步ConnectError和ConnectTimeout归一；公共知识失败记录/退避恢复；已知未发送模型记零成本 | TLS/IP固定、不可重试策略拒绝、已发送/未分类unknown保留、仅一次发送和撤销复核未放宽 | 根因修复有效、分类边界清晰，但遗漏旧测试更新，E03阻断整仓门禁；A03寿命问题原本另列，仍未解决 |
| A04 | 同游标/固定时钟ABA、迟到成功/失败、配置/key/cursor及独立store的完成所有权保护 | stale failure保留历史、不覆盖较新状态；import/cursor事务原子性保持；重叠任务保守失效是记录过的设计取舍 | 功能目标有效，但容量设计不足；新增A06可重现，不能称无回归闭环。无需因此回退到迟到覆盖错误 |
| A05 | 管理登录、Bearer、actor、CSRF/退出Unicode规范拒绝，有效ASCII角色及失败限速保留 | 不trim/normalize/扩大权限；无有效cookie被错误Bearer破坏、非法CSRF不写入；Token不输出 | 修复范围内有效、变更集中。本轮A08属于其他旧入口，提示后续输入审计应覆盖所有不可信compare_digest，非本轮引入 |
| E02 | CI/release装hash锁dev+cloud，实际合成SDK签名200/403+兼容必需3案例；缺项/跳过/重复/失败拒绝 | 基础runtime仍可不装cloud；无真实云访问，签名/日志/history测试使用合成数据；release仍依赖质量/外部门禁 | 本地门禁合同有效，补了真实执行证据约束。GitHub runner与artifact上载尚未远程运行，不能宣称端到端CI验收 |

旧修复记录的模块结果：A02关联204项+172子测试、补充37项；A04关联57项及schema31项；A05关联47项及重叠专项19项/原服务4项；E02实际SDK12项与合同14项/合并26项。最终四模块组合112项、0失败/跳过、260.48秒，应用内容为 `b5a1ca5`，随后仅追加同模块证据记录。**这些批次有重叠，不相加为独立总数，也不是当时新提交的整仓结果。** 初次夹具/旧期待失败和系统解释器stub问题在原记录中保留，本轮未抹去。

**明确的新旧归因：**

- **上一轮引入：A06、E03。** A06为应用性能回归，diff与当前SQL执行计划/合法大历史反例一致；E03为A02改变异常合同后的测试遗漏，整仓与单项均确定失败。
- **上一轮前已知且未修：A03、E01。** 继续记录，不把“仍存在”误报为修复引发。
- **本轮首次确认但更早存在：A07、A08。** 对应代码不在上一轮diff中，历史归属明确；新增发现不等同新增回归。
- 原A02/A04/A05/E02功能目标本地有效；A02附带E03门禁不一致，A04附带A06，E02远程执行尚有环境边界。没有足够证据支持“全部问题已经修完”或“上一轮没有任何连锁问题”。

**总体判断：** 上一轮采用小范围根因修复、真实反例、账务/权限保护和模块组合回归，功能修复质量较好；公共知识围栏的长期容量覆盖不足，网络异常变更未统一所有既有测试合同。**这一轮修复质量尚未达到完整验收闭环**：须解决A06并补齐容量证据，修复E03并取得当前候选整仓绿色，才可评价为本地闭环通过。当前没有确认灾难性连锁回归，但还不能替代真实环境发布证明。测试通过衡量既有断言，不覆盖本轮新反例，也不自动证明生产性能。

## 后续修复建议和保持只读的边界

本报告不实施修复。后续如获得授权，建议先E03（低风险测试合同与门禁恢复），再A03（原始I/O/真实退出）、A06（有界持久化代次）、A07（内容/空间绑定及有界索引重建），A08作为独立低风险输入模块；完成候选后取得E01。若近期必须切embedding，A07应提前；若同步历史已大或writer明显受压，A06应提前。

每模块先固定本报告反例，明确最小安全边界，再实现、关联回归、复审和独立提交。不将A03与账务重试/slot扩容混改，不以删历史缓解A06，不以删记忆/聊天或无预算批量付费重建缓解A07，不全局吞TypeError缓解A08。聊天删除继续禁用，历史恢复和其他窗口原文/事实继续保留。正式发布缺证据时保持拒绝，不用文档结论替代证明。

## 证据索引和审计交付

| 当前证据 | 用途 |
| --- | --- |
| `.runtime/sixth-readonly-environment.json` | 精确SHA、schema、解释器、75包/锁摘要和受跟踪清单 |
| `.runtime/sixth-readonly-3f91934-full.log` / `.xml` | 当前整仓Python实际终态，须结合最终结果段 |
| `.runtime/sixth-readonly-failure-recheck.log` / `.xml`、`sixth-old-expectation-probe.py` / `.log` | E03原样单项确定失败及隔离typed合同诊断；不代替整仓绿色 |
| `.runtime/sixth-readonly-mypy.log` / `sixth-readonly-node.log` | 408文件Mypy、69项前端回归 |
| `.runtime/sixth-deps-base.log` / `sixth-deps-dev.log` / `sixth-deps-cloud-retry.log` | 三组公开漏洞扫描最终成功；首次cloud失败另存原日志 |
| `.runtime/sixth-readonly-http-probes-corrected.log` | typed网络错误、A03不完整头/工作槽和清理 |
| `.runtime/sixth-readonly-behavior-probes.log` | A02账务/公共知识、A04迟到失败、A01访问日志当前复核 |
| `.runtime/sixth-peer-generation-probe.py` / `.log` | A06合法合成历史、查询计划、20签名事件及临时比较索引 |
| `.runtime/sixth-memory-revision-probe.py` / `.log` | A07顺序/在途合法内容修订的旧向量 |
| `.runtime/sixth-embedding-space-probe.py` / `.log` | A07真实provider类、模拟2/3维及同维不同资源空间 |
| `.runtime/sixth-nonascii-boundary-probe.py` / `.log` | A08真实公开HTTP、入站适配器异常及后续健康 |
| `docs/audit/2026-10-11-selected-audit-remediation.md` | 上一轮真实批次/失败历史和授权边界，作为对照而非新回归结果 |

探针准备过程中出现过隔离夹具重复endpoint的UNIQUE拒绝，以及旧HTTP探针自身未初始化变量的异常；修改的只是忽略探针，完成后重跑得到本报告证据。旧HTTP探针的容量时序也有一次未饱和，当前修正记录真实4 worker仍活、无关调用被阻断，不能宣称任何任意timing都确定饱和。没有改产品去迎合结果。

审计提交只包含本报告；应用/测试/配置/依赖与被审计提交保持完全一致。报告提交本身不代表发布候选已取得E01，也不解除A03/A06/A07/A08/E03。本轮未实施代码修复，聊天删除继续关闭。
