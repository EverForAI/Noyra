# Noyra 2026-10-20 独立审计问题修复与复核记录

日期：2026-10-20  
对应原始审计：`docs/audit/2026-10-20-independent-readonly-code-audit.md`  
范围：原报告 IA-P1-01 和 IA-P2-01 至 IA-P2-08，共 9 项；另补充修复全量回归发现的 1 项已有会话撤销边界缺陷。

## 1. 实施边界与结论口径

本轮从原报告对应的实际工作区继续实施。Git HEAD 为 `c67526d6b28f995823936687d9c59bb03b612463`；工作区原本已有大量未提交修改。此次修复不是对干净 HEAD 的独立补丁，不将历史修改、历史测试或历史报告算作本轮新增工作。

实施顺序为模型通信契约 → 密钥轮换和目录权限 → 运行时存储观测 → 睡眠反思边界与长期退避 → 安装包资源、部署期限及回滚。各模块分别完成边界设计、定向回归和代码复查，再进行跨模块回归。原 9 项修复均已实施并取得对应专项回归；全量验证另暴露并修复 1 项会话撤销缺陷。完整回归仍保留 8 次失败记录，均已在对应隔离/较低负载条件下复验通过，详见第 4 节，不宣称首次全量零失败。

原始审计报告保持原样。本轮没有重写生产数据库、备份格式或 keyring 格式，也没有放宽授权/幂等检查或改变模型的本地字段类型；没有连接生产服务器、发起付费模型请求、广播钱包交易、执行真实双机迁移、推送或部署。

## 2. 问题与修复映射

| 原编号 | 问题风险 | 修复风险 | 根因与修复 | 验证入口 |
| --- | --- | --- | --- | --- |
| IA-P1-01 | P1 | 中 | 本地 Pydantic Schema 原样发送给严格结构化输出接口。新增独立 wire 转换层，对副本递归补齐 object required、删除 schema 默认值，保留 nullable、引用和本地默认值；真实调用和探测共享编码器。 | `tests/test_model_wire_contract.py` |
| IA-P2-01 | P2 | 中 | 推理模型沿用普通模型的 token/temperature 参数。已识别推理模型改用 max_completion_tokens 并省略 temperature；探测与正式调用使用相同构造路径，并为推理探测提供足够的输出预算上限。 | `tests/test_model_wire_contract.py`，既有 reasoning/Gateway 测试 |
| IA-P2-02 | P2 | 中 | 密钥轮换读改写未被跨进程序列化。对稳定的旁路锁文件先加锁、再读取当前代、保留旧密钥并原子发布；异常路径释放锁。 | 真实进程争用、失败后重试、真实数据库备份前后恢复 |
| IA-P2-03 | P2 | 中 | readiness 使用启动时存储快照。每次过期后重新检测挂载身份；共享缓存最长 5 秒，返回实际观测时间；recovery_capable 将备份可用性纳入 readiness。 | 真实 HTTP readiness 下挂载缺失、恢复、身份不匹配 |
| IA-P2-04 | P2 | 低至中 | glob 吞掉目录读取错误。改为显式 iterdir，让权限异常进入既有 unavailable 路径，禁止把不可读目录解释成空清单。 | Linux root 建目录 + nobody 读取的真实权限验证 |
| IA-P2-05 | P2 | 中 | 相邻反思窗口以前一轮 completed_at 为下界，漏掉睡眠期间到达的事件。睡眠开始时原子持久化事件链序号边界及互动 ID 锚点，下一轮从上次已集成边界继续。 | 连续睡眠、相同/倒退/超前时间戳、旧记录兼容、数据库关闭重开 |
| IA-P2-06 | P2 | 低 | setup 从源码相对路径加载未打包模板。模板作为 package data 发布，使用 importlib.resources 加载。 | 实际构建 wheel，在源码目录外通过隔离解释器执行 setup dry-run |
| IA-P2-07 | P2 | 低至中 | setup 子进程、DNS 和健康检查没有足够期限。命令、整体操作、DNS、curl 与回滚分别设限，超时清理子进程树并返回脱敏错误。 | 真实睡眠子进程、后代进程清理、注入健康检查超时后恢复文件及服务调用 |
| IA-P2-08 | P2 | 低 | 先求指数再截断导致 1025 次失败时溢出。先根据最大退避算饱和指数，再计算受限指数；保留真实失败计数。 | 真实 run_forever 和持久计数 0、4、1023、1024、10^12 |
| SUP-P2-01（补充） | P2 | 低 | 系统时间回拨后，退出及容量淘汰直接写入早于 created_at 的 revoked_at，违反数据库 CHECK，导致请求断开或创建失败。两处改为 MAX(当前时间, created_at)，保证撤销生效并保留行内时间顺序。 | 两种路径均取得修复前确定性失败/修复后回归 |

## 3. 安全边界与兼容性复查

### 3.1 模型通信

- 通用转换仅作用于发往供应商的 Schema 副本，不修改调用请求原对象或本地默认值。对 ModelSearchResults 另补充了其公开 JSON Schema 声明，本地字段仍为 tuple[dict[str, Any], ...]，旧的缺少 title/snippet、带历史附加字段的响应仍可读取。该声明会改变此输出的新请求摘要；尚未完成的旧请求若遇到摘要不一致，继续由既有 ModelCallStateError 机制拒绝复用，研究层按已有无结果路径处理，而不绕过幂等/授权核验或重复使用旧调用结果。其他输出的请求原始 Schema 保持一致。
- 必填的 nullable 属性仍允许 null；数组、嵌套对象、联合类型及递归引用均递归处理。明确声明的 dictionary additionalProperties 保持原语义，避免将任意字典悄悄缩成空对象。任意 dictionary 本身不因此成为严格上游支持的 Schema；本轮进一步扫描实际调用点的全部 18 类生产输出，发现 ModelSearchResults 的任意字典还需专门声明 title/url/snippet 的既有字段契约，已补齐其 JSON Schema，同时保持本地 dict 验证及旧响应读取兼容。18 类输出现均通过专项契约检查。
- 已识别的 o1/o3/o4、gpt-5、codex 前缀走推理模型参数路径。普通模型保持 max_tokens 和 temperature；第三方自定义别名仅设置 reasoning_effort 时，继续保留原有参数编码。仅凭支持思考强度不能推定代理同时支持哪一种 token 字段。
- 探测阶段先验证 reasoning_effort 合法性。推理探测输出上限为 4096，而非原来的 8；这是可用性修复，也意味着探测的最大费用预算比原先更高，实际使用量取决于模型响应。

### 3.2 备份与密钥

- 轮换锁文件为 keyring 同级的 `<filename>.rotation.lock`。锁文件不会在轮换后删除或原子替换，避免锁到旧 inode 导致互斥失效。
- 并发的第二次轮换快速报 RuntimeOwnershipError；它不会宣称成功。重试时重新读取已经发布的最新代，因此不会覆盖丢失中间代密钥。
- 轮换写入失败后原 keyring 仍可使用，锁被释放，后续重试可以成功。原加密格式、保留密钥数量约束及原子写入/fsync 流程保持一致。
- 不放宽备份目录的 root 私有权限。普通服务账号读不到时显示 unavailable，不谎报空目录，也不允许据此清理。

### 3.3 readiness

- 存储状态在短 TTL 内共享；TTL 为配置的 health_cache_ttl_seconds 与 5 秒的较小值。它提供有界时效的运行中观测，不是零延迟的挂载变化通知。
- 探测使用已有有界 findmnt 调用；错误转为不可用。并发刷新争用也失败关闭，不沿用启动时健康结果。
- 返回的 storage/backup checked_at 对应实际观测。备份丢失在 recovery_capable 下使 readiness 失败；standard 仍允许可选备份降级，保留系统盘部署边界。liveness 继续表示进程存活。

### 3.4 睡眠反思

- 边界事件 `sleep_reflection_boundary` 与 sleep_runs 创建处于同一事务，ID 根据 sleep_id 确定，避免部分写入和重复启动生成重复边界。
- 事件使用持久事件链序号划分区间；互动持久保存稳定 ID，再解析当前行序号。不会因为相同时间戳、时钟回拨或未来时间戳漏掉新到记录。
- 仅上次已有 reflection_event_id 的运行推进下界。集成前失败不消费窗口，集成后失败不重复消费已经提交的反思。
- 保留原来的 64 条事件、8 条互动和上下文长度上限；窗口覆盖不代表无限量全历史回放。删除/正在删除的私有对话继续受原过滤约束。
- 不增加数据库 schema 版本。旧睡眠记录没有边界时沿用 started_at 兼容路径，将最近一个旧窗口与新边界衔接；不自动重放所有历史遗漏，也不改写既有反思。

### 3.5 安装与长期运行

- wheel 包内模板与 deploy 示例内容一致，并验证安装后可读取；dry-run 使用受控 DNS 和命令适配器，不修改真实 systemd。
- 原生命令一般上限 120 秒、apt-get 上限 300 秒、操作命令/DNS 共享 600 秒期限；DNS 每个域名最多等待 5 秒。curl 连接/总请求分别为 5/10 秒。
- 健康检查普通失败最多尝试 3 次；超时进入失败及回滚路径。回滚独立获得 120 秒预算，不因主操作已耗尽而被直接跳过。回滚失败明确报告，不伪装为恢复成功。
- POSIX 按新会话进程组清理后代；Windows 使用 taskkill 进程树并回收进程。超时错误只包含可执行文件名，不带命令参数、token 或捕获输出。
- 自定义 CommandRunner 保留原接口；其同步实现须自行遵守期限，SetupRunner 对返回前后期限及 TimeoutExpired 做处理。真实部署默认使用带进程超时的 SubprocessRunner。
- 失败计数只影响退避的受限指数，不被截断或清零；熔断及最大退避设置保留。

### 3.6 补充：会话撤销与时钟回拨

全量回归捕获退出接口的真实 IntegrityError 与连接中断；单次重跑通过，不能据此排除缺陷。随后将时钟从会话创建时的 2000 秒回拨至 1990 秒，在合法的会话容量及认证配置下，退出与容量淘汰两条路径都稳定触发同一 CHECK 约束。该两处原始 SQL 在修复前冻结副本中已经存在，因此属于本轮额外暴露的原生缺陷。

修复仅约束撤销时间下界：MAX(now, created_at)。不修改会话过期时间、签名/hash、角色、CSRF 或数据库约束，不重新激活任何会话。回归同时检查旧会话确已失效、新建会话仍有效、持久撤销时间满足已有约束。仅测试夹具设定虚拟时间，未修改宿主机或服务器系统时钟。

## 4. 验证结果

### 4.1 已完成的最终版本专项与静态检查

| 检查 | 本轮结果 |
| --- | --- |
| Linux 真实文件系统：两份新增回归文件 + 认证输入 + 持久会话 | **71 passed**，109.41 秒 |
| Windows：两份新增回归文件 | **47 passed，1 skipped**，126.90 秒；跳过项为 Linux root/nobody 场景 |
| 18 类生产输出契约与既有检索模块 | **57 passed**，74.59 秒 |
| 全部生产调用点的 Schema 补充扫描 | **18 类输出，0 项契约缺口**；实际受控 HTTP 验证同时进入新增回归 |
| 项目前端三个 test.cjs 文件 | **116 passed，0 failed** |
| Ruff（src/scripts/tests） | 通过 |
| Mypy（src/tests） | **440 个 source files 通过** |
| git diff --check | 通过；仅有既有 CRLF/LF 规范化提示 |

以上存在覆盖交集，不能直接相加为不重复测试数。新增回归文件目前包含 **48 个参数化测试实例**；Windows 不支持的真实 root/nobody 权限场景在 Linux 下实际执行。

### 4.2 完整回归

完整清单为 **218 个 test_*.py 文件**，分成四组独立 pytest 进程，无遗漏、无重复文件。四组均运行至结束：

| 分组 | 文件数 | passed | failed | skipped | subtests passed | 耗时 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 55 | 707 | 1 | 9 | 52 | 1273.89 秒 |
| 2 | 55 | 694 | 5 | 6 | 186 | 1291.83 秒 |
| 3 | 55 | 631 | 1 | 2 | 17 | 1344.15 秒 |
| 4 | 53 | 826 | 1 | 0 | 33 | 1588.51 秒 |
| 合计 | 218 | **2858** | **8** | **17** | **288** | 并行执行约 26.5 分钟 |

**这轮完整回归不是全绿。** 8 项失败的原因对照及复验如下，保留初次失败计数：

| 初次失败项 | 原因与对照 | 后续结果 |
| --- | --- | --- |
| test_wallet_fee_admission...refunded_unknown；test_wallet_economy...automatic_payment | 全量中表现为订单拒绝、悬赏尚未开放；本轮没有改动相关钱包实现。单独运行包含两个失败项及相邻参数项，不改代码/断言。此前全量还实际捕获了墙钟回拨，因此不将时间敏感失败简单忽略。 | **3 passed**，10.93 秒。此结果表明单次失败未稳定重现，不构成并发/时钟敏感性已被彻底证明消除。 |
| test_private_erasure_disabled...service_boot_and_restore | root 测试实例生成的迁移目录不满足安装服务账号权限边界；修复前代码副本同样失败。使用真实普通用户、隔离宿主 /etc/noyra 配置后，原测试不改断言。 | **1 passed**，包含在 cold-native-isolated-account.log 中。未放宽权限检查。 |
| test_public_archive...public_lifecycle...False/True；...cold_segment_pages...；test_public_cold_reader...large_private... | 首次四组并行及 Windows 挂载源码路径下冷进程启动/读取超过既有期限。前两项在修复前源码副本同样失败。将相同代码复制到 Linux 原生目录，在较低负载、独立账号/配置下复跑，未放宽内存/时间期限。 | **4 passed**，35.45 秒，见 cold-final-native-current.log。另有 OS 内存限制测试单独 **1 passed**。 |
| test_migration_storage_boundary...installed_permissions... | 真实 nobody 子进程无法读取宿主机已有的 /etc/noyra 父目录。独立挂载命名空间绑定空配置目录后，仍使用真实 root/nobody、文件模式、加密及恢复。 | **1 passed**，12.50 秒，见 migration-boundary-isolated-host.log。宿主配置未被修改。 |

上述复验覆盖全部 8 个首次失败用例，不能把它们加回首次完整运行的 passed 数量，也不能据此宣称零时序风险。17 个跳过实例涉及缺少 boto3、真实 Windows ACL/PowerShell、Windows 特有路径契约或当前 root 身份与特定普通用户 fixture 不匹配；不把跳过当作通过。

全量控制器结束时还记录过临时 tmpfs busy 的卸载清理错误；它发生在四个 pytest 已完成之后，不影响上表统计。后续检查没有遗留占用进程或对应挂载。该临时控制器及其清理逻辑没有进入产品代码。

全量临时数据放在隔离 tmpfs 中以减少重复数据库 fsync 的等待；未修改产品同步落盘设置。第 4.1 节的 71 项使用真实 Linux 文件系统，其中实际执行跨进程密钥轮换、加密备份恢复、目录权限拒绝、超时子进程清理等测试。tmpfs 回归不被当作断电持久性证明。

测试子进程统一 PYTHONPATH 指向当前源码，控制器另外打印核对实际导入路径；wheel 验证使用隔离解释器导入构建产物。早期未统一子进程源码的迁移失败、临时测试目录初始化错误，以及为分组或修复补充缺陷而主动中断的运行，均不计入最终全量通过统计。未把历史日志当作本轮通过结果。

本轮日志及隔离验证脚本保存在仓库外：`D:/metagrowh/.user-temp/noyra-nine-fixes-ieGkWh`。

### 4.3 版本指纹

采用原审计相同算法，对原 560 个源码清单文件加本轮 4 个新增实现/测试文件，共 564 个文件计算内容指纹：

```text
bbc040524f8b57e159e0fd06733b5e2f714091a4466910d4efbcaac25c2d1106
```

完整路径和文件哈希清单见证据目录中的 `accepted-source-manifest.json`。报告文件未纳入该源码指纹。

## 5. 变更保护及验收范围

对修复前冻结的 871 个已有项目文件逐个比较内容哈希，仅下列 11 个既有文件发生本轮预期变化，其余 860 个保持一致：

- pyproject.toml
- src/noyra/model/openai_compatible.py
- src/noyra/core/at_rest.py
- src/noyra/core/backup_retention.py
- src/noyra/service.py
- src/noyra/cognition/reflection.py
- src/noyra/cognition/research.py
- src/noyra/sleep/engine.py
- src/noyra/autonomy/loop.py
- src/noyra/deployment_setup.py
- tests/test_deployment_setup.py

新增实现/测试为 wire.py、包内 systemd 模板、test_model_wire_contract.py、test_audit_20261020_regressions.py，以及本报告。原审计文件 SHA-256 保持为 `f4cc61ac0373d6dba054def5cdd56ce7d4243db21de4efccb058b952f2961fee`。

本轮验证确认原报告 9 项及补充会话撤销缺陷的对应触发路径已有修复与回归防护；全部 564 个源码文件在最终验证期间哈希保持一致。完整回归中的时序和宿主配置敏感失败已经保留，不把单独复验通过等同于所有环境下永久可靠。真实供应商、真实重启/卸载、跨机迁移及长时间 soak 仍需在后续实机验收中确认；短时受控测试不替代这些结果。
