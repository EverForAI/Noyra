# Noyra 全项目独立只读审计（80bec09）

审计日期：2026-10-25（Asia/Shanghai）。代码基线：`80bec0942225f4b38923a0cc9f8a2b29cf2afdf6`（简称 `80bec09`）。执行环境、逐文件 SHA-256、函数位置和回归结果在本报告末尾列出。

## 1. 结论与范围

本次从当前代码的入口、状态和恢复路径重新检查，未将上轮报告或“测试已通过”当作问题结论。确认 **6 项问题：5 项 P2、1 项 P3**。其中 5 项涉及产品行为，1 项属于测试基础设施；没有将环境差异或未完成的实机门禁混算为产品漏洞。

本次没有确认 P0/P1 问题；这不代表已经证明不存在其他问题。常规回归通过与定向复现发现缺陷可以同时成立：本报告的定向案例检查了原用例未覆盖的状态组合。

本次为只读审计。产品代码、既有测试、部署脚本、依赖文件和 Git HEAD 保持原样；仅新增本报告。验证程序及其数据库、签名证书、包目录和日志位于仓库外的临时证据目录。没有向生产实例执行操作，也没有推送、合并或部署。Windows 测试使用内存 CMS 证书和未执行的系统签名 launcher，不导入证书库。

### 1.1 检查方法与覆盖边界

- 对 **1,137 个跟踪文件**建立内容哈希与字节/行数清单；其中 `src/` **240 个文件、156,288 行文本**。对其中的 src Python 代码，通过 AST 建立 **4,005 个函数、691 个类**及起止位置索引，部署、工作流及前端另作清单。
- 按“入口→身份/策略→持久化事务→外部 I/O→结果结算→中断恢复→展示/导出”审阅关键调用链。范围覆盖运行内核、完整性、模型/向量、认知/睡眠、账户/渠道、公开页面、钱包/协作、备份/归档/保留、迁移、安装/升级、前端和 CI。
- 重新执行全部 **238 个后端测试文件**，并重新执行静态检查及前端测试；不是复用上一轮运行日志。确认问题另以隔离程序复现。
- “全面”在此指项目范围和主要边界覆盖，不等于每个分支都已实机执行，也不声称人工逐字核对了全部 156,288 行。静态索引、调用链审阅、定向复现和自动化回归的作用分别记录。
- 未实测 Linux root/systemd/LUKS、真实供应商计费、链上支付、真实跨机迁移或长期 soak；对应跳过项和门禁不能算作已通过。

### 1.2 等级定义

**问题风险**：P0 为需要立即停用/隔离的严重风险；P1 为高影响且现实入口明确的安全或一致性问题；P2 为有具体触发条件的功能、完整性、恢复或可用性缺陷；P3 为局部验证质量或低影响问题。

**修复风险**与问题严重性分开：低表示改动局部、无持久格式变化；中表示涉及共享调用者、状态或平台契约；中至高/高表示涉及来源证据迁移、运行所有权、信任撤销或多记录恢复，错误修复可能产生更大影响。

## 2. 问题总表

| ID | 问题风险 | 修复风险 | 确认问题 | 触发条件摘要 |
| --- | --- | --- | --- | --- |
| R01 | P2 | 中至高 | 关系实体身份没有不可变来源证据 | 数据库写入缺陷/误操作改变合法实体键 |
| R02 | P2 | 中 | Windows 文件发布可将目录视为目的容器并虚假成功 | current/previous/trust 的文件路径实际为目录 |
| R03 | P2 | 高 | Windows 失败轮换遗留新信任和错误 previous | 更换实际 signer，信任发布后 current 替换失败 |
| R04 | P2 | 中 | 运行时与迁移主体 ID 合同不一致 | 合法自定义 ID 或长度 127/128 的合法 ID |
| R05 | P2 | 中至高 | 向量硬超时后关闭流程仍可无限阻塞 | 工作线程超时后未退出，调用认知关闭 |
| R06 | P3 | 低 | Windows 本地化 junction 夹具解码异常 | PYTHONUTF8=1 且 cmd 输出非 UTF-8 |

上述触发均在本地隔离环境证实。R01 不是“匿名用户已能改库”的证明；R02/R03 不是“不受信任代码被运行”的证明；R05 不表示每次网络超时都会阻塞关闭。风险描述以已证实的能力与前提为限。

## 3. 逐项发现

### R01：关系身份改变后仍通过 mind.state，既有信任可转移至另一个实体

**问题风险：P2。修复风险：中至高。置信度：高。** 修复涉及旧库证据迁移及社交读取者；错误回填可能把已经错绑的当前身份固化为可信历史。

**位置**

- [关系状态散列](<E:/我的项目/Noyra/开发版/src/noyra/mind/relationship.py:365>)、[从持久行返回实体身份](<E:/我的项目/Noyra/开发版/src/noyra/mind/relationship.py:339>)。
- [mind.state 关系与最新修订核验](<E:/我的项目/Noyra/开发版/src/noyra/mind/engine.py:374>)。
- [按当前实体选择社交历史](<E:/我的项目/Noyra/开发版/src/noyra/cognition/social.py:291>)。

**根因**

关系 `state_hash` 和 `relationship_revisions.state_hash`只覆盖 trust、affinity、conflict、familiarity 与 boundaries。`entity_key`、`entity_type` 和关系所属主体等身份字段没有独立不可变见证。当前 schema 对关系修订设有保护，但没有阻止关系实体键被改成另一个合法值。完整性检查比较的当前状态/修订散列都不含实体键，因此无法检测这一变化。

**触发条件**

本地主体数据库发生写入错误、维护误操作或具有库写权限的代码错误，将已有关系的实体键改为同主体的另一个合法实体；该实体已有可回应的消息。无需删除触发器或修改修订历史。普通公开账户没有因此获得数据库写权限。

**复现与证据**

在临时主体中通过账户 API 存储层创建 Alice 和 Bob 两个 human 实体，各写入真实私有消息；只为 Alice 建立关系并形成 trust=0.9、affinity=0.8、familiarity=0.9 的有效修订。随后只执行一次 `UPDATE relationships SET entity_key=<Bob>`。

- 前后字段差异只有 `entity_key`。
- `core.schema_contract` 与实际 `mind.state` 均报告 `ok`，没有 findings。
- 社交上下文显示 `display_name=Alice`，却将 Bob 的实体键、消息和可用路由与 Alice 的 trust=0.9 组合。
- 模型请求数为 0；不依靠真实模型行为推断该结果。

证据：`relationship_identity_probe.py`、`relationship_identity_probe.json`。

**影响**

主体可能将针对 A 的熟悉度、信任和联系边界应用到 B，破坏“区分不同主体”的基础。这是身份与社会状态完整性缺口；本次未证明由此发生了私有正文泄露或外部投递。

**建议修复与安全边界**

为关系身份建立不可变、同主体绑定的来源证据，并在读取及 `mind.state` 中核验。合法显示名更新如需开放，应进入明确修订合同，不能与实体身份改写混为一谈。旧记录不能仅按当前实体键重新散列后宣称已经证明原始归属；需要历史证据或明确的历史基线类别。不能通过“发现不一致即自动重建关系”转移已有信任。

**验收**

单字段实体键变化必须被写入边界拒绝或被完整性检查发现；相同昵称的不同主体、正常关系修订、旧库升级和只读导出均保持正确。不得将 A 的历史权重自动赋予 B。

**归因**：相对上一轮修复前的 `0dea2dd` 为存量问题；本轮没有修改关系身份/散列合同。Git 边界历史只能证明该实现早已存在，不将浅历史根提交误写成确切引入时间。

### R02：Windows 文件发布函数接受目录，安装/升级可能报告成功但未保存正确状态

**问题风险：P2。修复风险：中。置信度：高。** 修复影响初装、升级、重试及回滚共用的 Windows 文件发布路径，需保持类型校验与原子替换的一致性。

**位置**：[Write-PointerAtomic](<E:/我的项目/Noyra/开发版/deploy/windows/noyra-lifecycle.ps1:70>)、[Write-PublisherTrustPolicy](<E:/我的项目/Noyra/开发版/deploy/windows/noyra-lifecycle.ps1:100>)、[读取指针时将非普通文件视为不存在](<E:/我的项目/Noyra/开发版/deploy/windows/noyra-lifecycle.ps1:62>)。

**根因**

两个“原子写”辅助函数都把临时文件交给 `Move-Item -Destination <固定文件路径> -Force`，没有确认目的地的文件类型。在 PowerShell 中，若目的地已是目录，此操作会把临时文件放进该目录并返回成功，未替换应有的固定文件。读取方遇到目录则返回 null/空策略，进一步隐藏了已损坏状态。

**触发条件**

安装根下 `current.txt`、`previous.txt` 或 `trusted-publishers.json` 因先前故障、误建目录或维护操作变为目录。与正常安装根和版本包的校验可以同时成立，不需降低包签名要求。

**复现与证据**

使用真实 CMS 签名和系统 Authenticode fixture，未执行 launcher：

1. `trusted-publishers.json` 预先为目录：严格安装返回成功，current 已发布；目录内只有 `.trusted-publishers.<随机值>.tmp`，后续不显式传 pins 的同版本重试却因无外部可信 publisher 而失败。
2. `current.txt` 预先为目录：安装返回成功，但 status 的 `current_version=null`，目录内残留 `.current.txt.<随机值>.tmp`。
3. `previous.txt` 预先为目录：从 a1 升级 a2 返回成功，随后 rollback 返回 `No usable previous version is available.`。

证据：`windows_state_probe.ps1`、`windows-state-final.json` 中 `audit_policy_directory`、`audit_current_directory`、`audit_previous_directory`。

**影响**

管理者得到成功结果，却没有可读取的当前版本、信任策略或回滚版本。严格校验仍拒绝缺少信任的后续操作，因此本次证明的是可用性及状态准确性缺陷，不是不受信任代码执行。

**建议修复与安全边界**

固定状态路径应采用明确拒绝目录/重解析点的文件发布合同；原子操作本身不能具有“移动进目录”的语义。发布前核对所有目的地类型，发布后校验固定路径的类型与内容，异常时报告真实状态。不能递归删除这些目录来“修复”，不能沿链接覆盖任意目标，不能因信任文件读取失败而降为免验签。

**验收**

三种目录场景均须明确失败且保持原安装状态；正常初装、升级、同版本重试和回滚仍通过；另覆盖重解析点及发布过程中文件类型变化。

**归因**：指针写入语义是存量问题；publisher 文件写入在 `56a89b54` 已存在，早于此次五模块修复。上一轮修复新增的安装回执已有目录拒绝，但没有覆盖上述两个共享写入函数。

### R03：Windows 轮换 signer 后 current 发布失败，会留下不可用的旧选择和损坏的回滚记录

**问题风险：P2。修复风险：高。置信度：高。** 修复横跨信任撤销、版本选择及中断恢复；错误补偿既可能锁死合法安装，也可能重新授权已撤销签名者。

**位置**：[安装末段的信任→previous→current 顺序](<E:/我的项目/Noyra/开发版/deploy/windows/noyra-lifecycle.ps1:484>)、[rollback 验签与指针切换](<E:/我的项目/Noyra/开发版/deploy/windows/noyra-lifecycle.ps1:510>)。

**根因**

回执、publisher 信任、previous 和 current 分别写入，只有单文件临时替换，没有整组安装状态的提交/恢复记录。新信任在读取旧 current、切换指针之前持久化；任何后续错误都只清理 staging，不恢复或明确协调已写入的信任/指针。同版本重试能帮助后续完成安装，但不能保证失败返回时的旧安装仍一致。

**触发条件**

当前是 signer A 的有效安装；升级包由 signer B 签名，明确新 allowlist 包含 B 及 fixture 启动器签名者、不含 A；新 trust 写入后，current 的替换因文件占用、权限、I/O 错误或进程中断失败。普通不更换 signer 的重复安装不会充分暴露这一后果。

**复现与证据**

先安装 A/a1，再升级 A/a2（previous=a1）。以允许读、禁止替换的原生 Windows 文件句柄占用 current，然后升级到 B/b3，传入授权 B 及 fixture 启动器签名者、不含 A 的策略。

- upgrade 返回失败；current 仍为 a2。
- previous 已被改成 a2，原 a1 回滚选择丢失。
- 保存的 publisher 列表不再含 A。
- 后续 rollback 因旧包实际 signer 不受信任而失败。

使用禁止读的锁也能复现“信任已变但旧 current 仍选中”的中间状态；正式证据采用允许读的锁，确保失败确实发生在替换阶段。证据：`windows-state-final.json` 的 `audit_rotation`，以及 `windows-state-readlock.json`。

**影响**

一次未完成升级改变了仍选中版本的信任条件，并损坏回滚选择。当前已运行进程不一定立即停止，但之后 start/rollback 会按新策略拒绝旧版本。通过补传 pins 或同包重试可能恢复；这属于需要手工介入的失败恢复缺陷，不是永久数据丢失的证据。

**建议修复与安全边界**

对整组安装状态设计持久提交/恢复协议，在正常进程和中断重启时都能确定当前可信、已选中的版本及 previous。应在最终选择前准备恢复信息，并在未完成 intent 存在时由受控恢复路径处理。不能永久保留已撤销 signer 来换取可回滚，也不能无条件恢复旧 allowlist、绕过操作者明确的撤销意图；信任撤销与失败恢复必须一并设计。

**验收**

真实 A→B 轮换在回执、trust、previous、current 每一失败点及模拟进程中断后均能恢复到明确一致状态；恢复幂等；实际 signer 撤销继续有效；不能删除或覆盖被引用/运行中的版本。

**归因**：属于上一轮前已经存在的多记录发布缺口；本轮保留了该先后顺序。`b16fb1a` 改进重试和锁，未引入 trust 先于 current 的设计，也未完成其整体恢复。

### R04：合法实例主体 ID 到迁移边界被拒绝

**问题风险：P2。修复风险：中。置信度：高。** 修复需同步源端、目标端和激活协议的边界，且保持已有主体身份与文件系统约束。

**位置**：[运行内核 ID 合同](<E:/我的项目/Noyra/开发版/src/noyra/core/identity.py:10>)、[ServiceSettings 使用同一验证器](<E:/我的项目/Noyra/开发版/src/noyra/service.py:1059>)、[Agent 主体正则](<E:/我的项目/Noyra/开发版/src/noyra/migration/agent.py:75>)、[enroll](<E:/我的项目/Noyra/开发版/src/noyra/migration/agent.py:338>)、[激活合同](<E:/我的项目/Noyra/开发版/src/noyra/migration/activation_contract.py:48>)。

**根因**

运行时、身份存储及 MigrationPolicy 接受 3–128 字符的安全 ASCII 主体 ID，不要求 `Noyra-` 前缀。Agent 与激活合同另写了 `Noyra-[A-Za-z0-9_-]{1,120}`，既强制前缀，又把总长度缩为最多 126。双方都没有把这一差异作为实例启用迁移前的一致合同处理。

**触发条件与复现**

同一临时数据库及相同有效请求中：

| 主体 ID | IdentityStore / ServiceSettings / MigrationPolicy | Agent enroll | 激活合同 |
| --- | --- | --- | --- |
| Noyra-0001 | 接受 | 接受 | 接受 |
| alice_001 | 接受 | 拒绝 | 拒绝 |
| Noyra- 后接 122 个 x（总长 128） | 接受 | 拒绝 | 拒绝 |

证据：`migration_subject_probe.py`、`migration_subject_probe.json`。此复现不发送跨机请求、不切换服务。

**影响**

可合法配置和运行的实例，无法按当前目标 Agent/激活协议完成迁移；紧急恢复也会遇到同类主体绑定限制。默认 Noyra-0001 不受此条件影响。没有证据表明会把数据交给错误主体；系统是拒绝操作。

**建议修复与安全边界**

共享同一个安全主体 ID 合同，清点 enroll、binding proof、manifest、restore/activate、接收方和 CLI 的所有副本。保留 Windows 设备名拒绝与路径隔离，不能只放宽为任意字符串。不要通过改写现有主体 ID、强制加前缀或重算身份材料解决迁移问题。

**验收**

合法自定义值、126/127/128 边界、保留设备名及非法字符做源端→Agent→激活的合同一致性回归；既有签名、MAC、摘要及所有权绑定不退化。

**归因**：迁移合同中的窄正则至少在 `a1a16710` 已存在；非上一轮五模块修复引入。

### R05：向量硬超时后，认知关闭可以阻塞事件循环并绕过异步超时

**问题风险：P2。修复风险：中至高。置信度：高。** 修复会改变 worker、费用账本、关闭期限及运行锁的生命周期；仅提前返回可能留下迟到线程越过停机所有权边界。

**位置**：[EmbeddingGateway 硬超时后的存活线程](<E:/我的项目/Noyra/开发版/src/noyra/model/embedding_gateway.py:203>)、[close 的无限条件等待](<E:/我的项目/Noyra/开发版/src/noyra/model/embedding_gateway.py:185>)、[CognitionCycle.aclose 直接调用阻塞关闭](<E:/我的项目/Noyra/开发版/src/noyra/cognition/cycle.py:528>)。

**根因**

gateway 用 join(timeout) 返回硬超时并将用量留为 unknown，但线程可能继续运行。正常认知关闭在事件循环线程里调用 `embedding_gateway.close(wait=True)`，未传 timeout。close 在 worker/future 退出前无限等待；事件循环本身被阻塞，外层 asyncio.wait_for 不能按时获得执行机会。daemon 属性不能消除显式无限等待。若使用自有 executor，close 还会先调用 shutdown(wait=True)，这一步也不会被后续条件等待的 timeout 限制。

**触发条件**

底层向量 provider、SDK 或授权回调在 gateway 超时后仍阻塞，随后执行认知关闭/服务停机。正常已完成的向量调用或及时停止的超时工作不触发。本次不声称所有常见 HTTP 超时都有该后果。

**复现与证据**

使用真实 `OpenAIEmbeddingProvider` 配一个等待 Event 的本地授权回调；回调释放后抛权限错误，MockTransport 断言没有网络请求。gateway timeout 为 0.02 秒：

- 返回 `embedding_hard_timeout`，账本状态 unknown，worker 仍存活。
- 对真实 CognitionCycle 执行 `await asyncio.wait_for(cycle.aclose(), timeout=0.05)`。
- 只有另一个线程约 0.5 秒后释放 Event，关闭才返回；外层并未抛 TimeoutError。
- 实测关闭耗时约 0.5 秒；网络请求列表为空。若不释放，代码路径没有自身退出上限。

另以符合 RawEmbeddingProvider 接口的阻塞适配器得到同类结果。证据：`embedding_builtin_shutdown_probe.py/json`、`embedding_shutdown_probe.py/json`。

**影响**

停机或重启可能超过预期，异步取消失去时限。systemd 等外部管理器可能最终强制结束进程。unknown 费用记录本身保留正确；缺陷位于生命周期收尾，不应通过将未知调用误标成未发送来修复。

**建议修复与安全边界**

关闭需要明确的有限等待合同，避免在事件循环内阻塞；同步关闭与 worker/future 生命周期应统一，并阻止关闭后接入新的工作。不能只套一层 asyncio.wait_for，也不能把无上限等待搬到默认线程池后就认为解决。超时后仍可能返回的线程必须保留 epoch/所有权围栏，避免释放运行锁后旧线程继续提交主体变更；已发生的用量结算与 unknown 证据继续保留。

**验收**

真实 provider 授权器、响应处理及自定义 provider 分别卡住时，事件循环 heartbeat、关闭 deadline 和进程锁释放规则仍正确；迟到结果不产生新业务提交，unknown 账本不被抹去。正常关闭和可恢复的短等待仍成功。

**归因**：无限等待及调用点属于既有基线实现，非上一轮新增。

### R06：本地化 Windows junction 测试夹具存在后台解码异常

**问题风险：P3。修复风险：低。置信度：高。** 改动可局限于测试夹具的输出捕获与诊断解码，不需要变更产品格式或业务逻辑。

**位置**：[junction 辅助函数](<E:/我的项目/Noyra/开发版/tests/test_m42_p2_01_training_workspace.py:162>)；[CI 的 UTF-8 模式设置](<E:/我的项目/Noyra/开发版/.github/workflows/ci.yml:12>)为 PYTHONUTF8=1。

**根因及触发条件**

辅助函数以 `subprocess.run(..., capture_output=True, text=True)` 读取 cmd.exe / mklink 的本地化输出，未规定正确编码或字节处理。Python UTF-8 模式与非 UTF-8 的系统输出组合会令 subprocess 读线程抛 UnicodeDecodeError；父线程仍可能拿到成功 returncode。

**独立复现**

显式设置 PYTHONUTF8=1、将 PytestUnhandledThreadExceptionWarning 视为错误，运行该文件：**3 failed、3 passed**。三项失败分别是 workspace 根链接、foreign directory 链接及 subject root 链接用例。证据：`junction-warning-probe.log/xml`。这不是把普通产品用例失败算入全量默认结果；它是有意收紧警告策略的定向复现。

**影响**

当前本机全量测试会带后台线程警告；严格警告环境直接失败，子进程诊断输出也可能不完整。用例仍在检查实际 junction 行为，不能据此宣称产品跨主体隔离已经失效。

**建议修复与验收**

夹具以 bytes 捕获输出，按明确的本地代码页/容错诊断规则转为错误文本，或采用不依赖本地化输出的 API；继续以 returncode 和实际文件系统结果作断言。不要屏蔽所有线程警告。中文 Windows + UTF-8 模式 + warning-as-error 下相关 6 项应全部通过，并保留真实链接拒绝断言。

**归因**：存量测试夹具问题，非上一轮新增；本次将其作为低风险验证缺口单列。

## 4. 全项目审阅覆盖及未形成确认问题的检查

| 范围 | 代码链与重点 | 本轮结果/边界 |
| --- | --- | --- |
| HTTP/公开账户 | service Handler、角色令牌/会话/CSRF、participant_http、accounts、协作归属与幂等 | 未确认公开提权或账户跨主体读取；不把在途请求的一般授权时序自动算作绕过 |
| 社交与关系 | 收件人索引、撤回 scope、发送/提交门禁、关系修订与 mind.state | R01；撤回和冻结路由路径的现有改进可保留 |
| 内核/完整性 | admission/epoch、ProcessLock、schema 合同、审计动作、事件链与读取预算 | R01 为数据身份盲点；不是 schema 指纹漂移 |
| 模型/向量 | 请求身份、重试、unknown 隔离、预算、提供商响应/超时、向量 worker 收尾 | R05；默认连接 20 秒/总等待 100 秒仍在模型配置中 |
| 自主认知 | supervisor、目标治理、研究、元认知、项目执行、自修改允许清单和提交 CAS | 未确认新的目标强制入口；局部/合成测试不等同于长期自主行为验收 |
| 心智/世界 | 目标来源、关系、记忆整合/巩固、因果、信念、预测、world source | R01；目标的 created/legacy_baseline 证据类别及历史边界保持明确 |
| 睡眠/调度 | 睡眠状态转换、反思高水位、等待任务终态、错误退避、恢复 | 未确认新的终态任务自动重放；未运行长期 soak |
| 搜索/网页/渠道 | HTTPS/DNS 地址固定、请求/响应预算、签名回调、发送与状态核对 | 未确认 SSRF 绕过；未连接真实第三方渠道作投递验收 |
| 钱包/协作 | 悬赏有效状态、提交归属、策略限制、nonce/广播/receipt、签名边界 | 未确认新的实际支付绕过；悬赏到期由 get_bounty 的有效状态处理，故不报“apply 缺少单独 expires 检查” |
| 存储/导出 | 导出所有权图、凭据排除、归档/备份认证、恢复路径、保留与回收 intent | 未确认新的越权导出或原始密钥泄露；真实 Linux 权限及加密卷门禁待对应平台 |
| 迁移/恢复 | 目标登记、Agent、激活合同、epoch/fencing、manifest 和所有权 | R04；未做跨机/systemd 实机迁移 |
| 安装/升级 | Ubuntu 脚本、Windows lifecycle、publisher 信任/回执/指针、发布证据 | R02/R03；没有降低签名要求，Linux root 路径本机未执行 |
| 前端/CI/依赖 | 动态 HTML 转义、账号与管理入口、脚本/测试、锁文件与构建 | R06；当前依赖环境差异另列，不据版本号推断 CVE |

以下内容没有被算成确认缺陷：合法证书撤销后旧版本被拒绝（预期安全行为）；目标旧记录只能证明升级时基线（已明确的信任边界）；没有真实用户/支付活动（实机门禁覆盖条件）；对外上下文未开放全局私有记忆（既有隔离策略）。R03 的缺陷是升级失败后的状态组合，不是要求撤销策略失效。

## 5. 修复建议与对现有改动的处理

本次不实施修复。建议后续分模块处理：

1. Windows 固定状态文件的类型与原子发布合同（R02），再设计并验证整组 trust/previous/current 的中断恢复（R03）；两者有不同根因，不能只加重试掩盖不一致。
2. 关系身份见证与历史兼容（R01）：先确定旧证据边界，再迁移及接入读取/完整性消费者。
3. 迁移 ID 合同统一（R04）：保持已有身份不变，覆盖源端和目标端全部边界。
4. 向量关闭协议（R05）：同时约束 deadline、迟到结算和运行所有权释放。
5. 测试编码（R06）：保持失败可见，不降低警告门禁。

未发现需要整体回滚上一轮修复的证据。上一轮的合法审计合同、邀请撤回、收件人索引、同版本验证重试和目标来源保护均有可保留的具体行为；本次发现的产品问题是其他存量合同/异常路径缺口。标准测试通过不能替代这些补充验收，也不能据“全部测试绿”宣布项目没有问题。

## 6. 验证结果与限制

本节均为 **80bec09 上本轮重新执行**的结果。后端按 3 个互不重叠的分片执行，238 个测试文件各分配一次；分片与当前测试文件集合核对无遗漏、无重复。三组退出码均为 0，总墙钟时间约 59 分钟。不是沿用上轮修复的计数。

| 验证 | 本轮结果 |
| --- | --- |
| 后端分片 0 | 918 passed、7 skipped、291 subtests passed |
| 后端分片 1 | 1,152 passed、28 skipped、5 subtests passed |
| 后端分片 2 | 1,080 passed、54 skipped、6 subtests passed、3 warnings |
| 后端合计 | **3,150 passed、89 skipped、302 subtests passed，0 failed / 0 errors**；子测试单独计数，不重复并入主测试 |
| 前端 Node 测试 | **152 passed、0 failed、0 skipped** |
| Ruff check / Ruff format --check | 均通过；760 个文件已符合格式 |
| Mypy | 通过；470 个源文件，无问题 |
| Python compileall | 通过 |
| admin.js / app.js / participant.js 语法 | 三项均通过 |
| pip check | 退出码 0；仅表明已安装包元数据的一致性，见下述环境差异 |
| 六项定向复现 | 六项均实际复现，并对报告中的异常结果作断言；reproduction-assertions.json 全部为 reproduced_and_asserted |
| R06 的严格警告定向复现 | 预期复现失败：3 failed、3 passed；与上方默认全量结果分开记录 |

本轮还从 JUnit 核对了上一轮修复相关用例：contracts 14 项、goal_origin 16 项、social 4 项、social_index 6 项、原生 Windows 验签回归 1 项，均通过且未跳过。Windows 签名回归的单个 Pytest 用例内部覆盖多个 PowerShell 状态场景，不能把“1 项”误解为只检查一个分支。junction 文件在默认门禁下为 6 项通过并产生上述 3 条后台线程告警；warning-as-error 下的 3 项失败即 R06，不是新的产品隔离故障。

**跳过范围**：89 项并非全部由同一种平台原因造成。JUnit 的原始原因涉及 POSIX 权限/属主、Linux root 与独立服务 UID、Bash/原生 POSIX 文件系统，以及本机符号链接能力；另有 **4 项 Windows 8.3 别名测试**因当前测试卷不生成短名而跳过。完整逐原因计数保存在 validation-summary.json。这些分支未执行，不能据本轮通过结果声称 Linux 安装/权限/存储门禁或 8.3 兼容已验收。

**执行环境及依赖限制**：Windows、Python 3.12.10、Node.js 24.15.0，测试启用 PYTHONUTF8=1。实际安装的 certifi 是 **2026.7.22**，而 pyproject.toml 声明 `certifi>=2026.10.19`、requirements.lock 固定 `2026.10.19`。本次未更改环境依赖；pip check 的 0 退出码不覆盖工作区声明与已安装 editable 元数据之间的差异，因此这里的回归不代表完整锁定依赖环境的最终验收。未运行在线依赖/CVE 扫描，也未把无依赖冲突当作无已知漏洞的证明。

**实机边界**：没有执行生产部署、真实模型计费、真实链上转账、真实渠道投递、跨服务器所有权切换、Linux root/systemd/LUKS 或长期 soak。模型和支付的模拟用例、签名包隔离复现与常规回归，不能替代项目既有 8 项门禁的真实环境验收。

## 7. 证据索引与只读复核

原始证据目录：`D:/metagrowh/.user-temp/noyra-audit-80bec09-20261025T035021/`。

| 证据 | 内容 |
| --- | --- |
| inventory.json / functions.json | 逐跟踪文件哈希、字节/行数、模块与 Python 类/函数起止位置 |
| manifest.json / results.json / validation-summary.json / pytest-0/1/2.log/xml | 本轮独立全量分片清单、退出码、原始结果；每文件只分配一次 |
| static-frontend-results.json 及对应 log | 全仓库静态、前端、编译和环境一致性检查 |
| findings.json / reproduction-assertions.json | 确认问题、风险、修复风险及证据映射；六项复现的结果断言均成功 |
| relationship_identity_probe.py/json | 单字段实体键变化、完整性 ok、上下文及路由错绑 |
| windows_state_probe.ps1 / windows-state-final.json | 原生 Windows 签名验证下的三种目录目的地与失败轮换状态 |
| migration_subject_probe.py/json | 正常 ID 对照、自定义和长度边界的跨模块合同差异 |
| embedding_builtin_shutdown_probe.py/json | 真实 provider 本地授权阻塞、unknown 结算及异步关闭超时失效 |
| junction-warning-probe.log/xml | UTF-8 + warning-as-error 下的 3 个夹具失败 |

最终复核基线为 `80bec0942225f4b38923a0cc9f8a2b29cf2afdf6`：

- 对 inventory.json 中全部 **1,137 个既有跟踪文件**重新计算 SHA-256：**0 项变化、0 项缺失**；不是只检查 Git diff，也不是只检查 src/。
- Git HEAD 保持上述 SHA，生成报告前工作区状态为空；全量测试运行器另核对 src/tests/deploy/scripts 快照，结果为 `SOURCE_CHANGES=[]`。
- 仓库仅新增 `docs/audit/2026-10-25-full-independent-readonly-audit-80bec09.md`。没有修改产品代码、既有测试、部署配置或依赖声明；未暂存、提交、推送、合并或部署。
- 所有复现脚本、SQLite 测试库、fixture 包、临时签名材料、完整日志和机器可读核对结果均保存在上述仓库外证据目录。
