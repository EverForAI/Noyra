# 2026-10-22 独立审计修复与验证报告

## 1. 结论、范围与交付状态

本轮依据同日独立只读审计的 R1–R9 实施修复，并落实用户指定的模型连接超时 20 秒、单次请求总超时 100 秒。原始审计报告保持不变；本文件另行记录修复方案、风险、验证和未覆盖边界。

- 工作区：`[私有验证记录]`。
- 分支：`codex/terminal-waits-operability`。
- 基线 HEAD：`b0162fd40cb436bbed34e9dc0a15c3a8313d45b1`。
- 本轮仅本地修改和离线验证：没有提交、推送、合并、部署或连接生产；没有调用真实模型、支付或修改生产数据库。
- 本次覆盖的是审计已确认的 9 项问题，并非宣称穷尽全项目缺陷，也不是完整全仓测试或真实供应商八项门禁验收。
- 最终验证汇总见第 4 节。修复过程中的失败日志不删除；用后续指定测试明确闭环，而不是把所有中间日志冒称全绿。

## 2. 修复清单与风险分级

“问题风险”沿用原报告；“修复风险”指改动实施与上线风险，不是说修复以后仍然存在同等级漏洞。

| 编号 | 问题风险 | 修复风险 | 来源 | 本轮措施 |
|---|---|---|---|---|
| T0 | 可靠性/可用性调整 | 中 | 用户指定调整 | 默认连接 20 秒、单次 HTTP 总时限 100 秒，草稿测试和模型发现也继承统一默认 |
| R1 | P1 | 高 | 原生问题 | 业务轮次身份持久化复用；按整个业务轮次串行；跨旧键查询未知/未完成调用与未解除授权 |
| R2 | P2 | 中 | 原生问题 | 向量绑定正文哈希；检索排除旧绑定；外部计算后短事务 CAS 写回；旧向量有界重建 |
| R3 | P2，条件性 | 中 | 原生问题 | 保存前后共用输出额度函数，推理/completion-token 类型 4096，普通类型 64 |
| R4 | P2 | 高 | 原生问题 | 入口、尝试准入、启动、实际发送授权重复检查生命周期；未发与已发分别结算 |
| R5 | P2，规模相关 | 中 | 上轮终态收口带入的性能放大 | schema100 增加等待修订 task_id 索引，保留完整收口与完整性验证 |
| R6 | P2 | 中 | 原生问题 | 拒绝脏树并从指定 commit 的 archive 构建，包/锁文件/部署输入固定到快照 |
| R7 | P3 | 中 | 原生问题 | 在最终 release 路径创建 venv，只切指针，不搬迁新环境 |
| R8 | P2，配置条件性 | 中 | 原生问题 | 轮换前确定 FILE-only 单一凭据源；不改配置；失败/可捕获中断恢复旧 token |
| R9 | P2，Windows 限定 | 中 | 原生问题 | PID+启动时间+已安装可执行路径身份记录，固定进程句柄后控制，拒绝旧 PID-only |

### 2.1 T0：20 秒连接、100 秒总时限

**原行为与触发条件**：模型正式调用默认总时限为 60 秒；供应商处理慢但仍可能返回时，客户端提前结束等待，无法证明远端未执行，因而可能进入 unknown 隔离。草稿探测/模型发现另有 30 秒覆盖，单改默认不足以保持一致。

**修改**：

- `../../src/noyra/model/config.py`：`timeout_seconds=100`、`connect_timeout_seconds=20`。
- `../../src/noyra/model/openai_compatible.py`：去掉草稿测试与发现的局部 30 秒覆盖；实际 HTTP 客户端与绝对请求 deadline 使用共享设置。
- 连接等待属于 100 秒内的阶段，不是先等 20 秒再额外等 100 秒。100 秒是单次 HTTP 尝试的总时限，不是整个业务任务跨重试、退避和多个资源的统一时限。
- 显式传入不同超时的调用者仍采用其显式值；没有添加未接线的环境变量或后台选项。

**影响与保留边界**：允许超过 60 秒但未超过 100 秒的正式尝试继续等待；慢请求可能多占用连接/并发名额。没有增加默认重试次数，也不把超时直接改判为未执行。超过 100 秒仍可能 unknown；历史 unknown 不自动改写或重放。

**验证**：正式客户端默认 read=100、connect=20；草稿/保存后 mock 探测与既有 HTTP 有界读取/超时回归。不以这些离线检查冒称真实供应商长耗时验收。

### 2.2 R1：未知结果不能因计数、日期、换键或新增资源绕过

**根因**：账本以幂等键隔离，但业务层会按调用次数/日期生成新键；路由物理键又包含资源和密钥选择信息。原请求未知后，新键被当作新任务继续发送。

**触发与影响**：同一观察、睡眠反思或交互轮次的请求已发出而结果不明，下一 tick、跨日、重启或资源变化触发重试，可能重复推理、计费或产生冲突结果。

**根因修复**：

1. 新增 `../../src/noyra/model/request_identity.py`，把业务目的限定为明确的三种轮次格式；未完成状态复用账本中的原逻辑键。只有已结算的调用推进业务代次，不再用当天全局调用计数构造未完成身份。
2. `../../src/noyra/cognition/cycle.py`、`../../src/noyra/cognition/reflection.py`、`../../src/noyra/cognition/interaction.py` 接入共享身份。
3. `../../src/noyra/model/resources.py` 对业务轮次按 purpose 取得进程内/进程间锁；路由检查覆盖不同逻辑键的 prepared/executing/unknown，以及尚未取消的已消耗重试授权。
4. 显式人工重试仍固定到原 physical call、group、key。历史无路由后缀的 legacy 调用也隔离；授权后只允许原 provider/model/pool 恢复，不因新增资源而换路。
5. 不把管理员每次主动测试等非业务请求全部合并成一个身份。
6. 不把同 purpose 的全部成功/普通失败历史纳入 256 条未完成路由限额，避免长期使用后被错误永久隔离；同时确保排在 300 多条结算历史之后的 unknown 仍可发现。schema100 另加 `(subject_id, purpose, resource_pool)` 索引，避免新的业务身份查找/计数随主体全部调用历史线性增长。

**防次生问题验证**：三个业务 purpose 的旧键/跨日/重启、实际睡眠反思链、不同业务轮次隔离、并发换键单一发送者、已知失败可推进、显式一次性授权、legacy 授权、已消耗失败授权不能因新代次复用、301 条已结算历史之后的新 unknown。未放宽 unknown 预留和人工对账规则。

**剩余边界**：损坏/分叉的未知身份仍失败关闭，需要人工确认；没有清空历史账本。此项涉及费用与幂等，部署前仍须保留备份和真实门禁。

### 2.3 R2：记忆向量与输入正文版本绑定

**根因**：向量只绑定 memory_id/provider，内容修订不会使其失效；重建只找“缺失”，且计算结果写回时不核对输入是否仍是当前正文。

**触发与影响**：记忆改写/归档或计算期间并发修订后，旧语义向量仍参与当前内容的相似度排序，可能错误召回与错误关联。

**根因修复**：

- schema100 新增 `memory_embedding_inputs(memory_id, content_hash)`；外键绑定 `memory_embeddings`，删除向量时级联删除输入绑定，不改变原 embedding 表列布局。
- `../../src/noyra/mind/retrieval.py` 在外部计算前检查主体/active/正文哈希，计算后在短事务中再次 CAS 验证；向量和输入哈希原子写入。
- `missing()` 包括无输入绑定、正文哈希变化；相似度查询 JOIN 当前 active 正文，只接受哈希相同的绑定，继续保留候选数量、向量大小和索引边界。
- 旧向量不伪造输入版本、不全部删除、不启动无上限付费重算，而是视为 stale，由现有有界重建逐批补齐。
- `../../src/noyra/core/runtime_export.py` 新增 schema100 明确导出归属：输入绑定通过其直接主体归属的 embedding 父表筛选，保持 fail-closed 的完整表清单校验。

**防次生问题验证**：正文更新后旧向量排除与重建、计算期间修订的 CAS、错误主体/旧输入不先花费、99→100 迁移、已有向量 stale、跨主体导出不串数据、私密记忆擦除连带删除输入绑定、规模检索计划。

**上线影响**：升级初期未重建的旧向量不参与语义打分，语义召回质量可能暂时下降；优先保证不以旧含义解释新正文。没有在迁移事务中调用 embedding 供应商。

### 2.4 R3：保存前后测试保持同一输出额度

**根因**：草稿测试对推理/completion-token 模型给 4096，保存后固定 64。推理 token 消耗后可能没有足够额度输出 JSON，从而保存前成功、保存后失败。

**修复**：共享 `connectivity_probe_output_tokens()`；两个入口均按同一模型/思考配置选择 4096 或 64。没有修改真实模型能力判定、没有通过放宽 JSON 验证掩盖空响应。

**风险和影响**：4096 是输出上限，不代表每次必然消耗这么多；正式测试仍受资源预算与额度预留约束，较小预算可能明确拒绝。一次主动测试不因此增加重试/故障转移次数。

**验证**：同一 mock 供应商仅在额度充足时返回合法 JSON，保存前/后都收到 4096；既有普通模型、reasoning、wire schema、生命周期和 API 分类测试保留。

### 2.5 R4：生命周期授权落到发送与每次重试

**根因**：仅入口查生命周期；异步建连、供应商等待或重试退避之后，首次实际发送可能已经深睡眠/暂停。

**修复**：

- `../../src/noyra/model/ledger.py` 统一策略：普通请求只允许 active；sleep_reflection 只允许 reflective_sleep。无 runtime owner 的独立账本维持原独立用法。
- 尝试准入、尝试启动、发送回调全部调用同一策略；发送授权用短 `BEGIN IMMEDIATE` 与生命周期转换序列化，并在发请求头前再次检查。此授权是线性化点，不跨网络持有数据库写锁。
- `../../src/noyra/model/gateway.py` 区分：第二次尝试尚未准入则不制造 reservation；已经准备但确定未发则 cancelled、call failed；已发请求收到有效响应后仍允许正确结算。
- 路由层原样保留 `ModelLifecycleBlockedError`，记录本地 deferred 原因，不改成 provider in-progress、unknown 或供应商故障；不因本地暂停惩罚密钥/健康统计。

**验证**：发送前进入 paused/deep_sleep 无 HTTP 请求；反思合法 reflective_sleep；重试退避时暂停阻止第二次发送并正确结束 call；响应已经在途时暂停仍结算；路由层显示真实拒绝原因且不生成供应商失败统计。

**边界**：线性化授权之后已进入发送阶段的请求不可能被可靠“撤回”；本修复不承诺撤销远端已接收请求。不能把这类结果强行记为未执行。

### 2.6 R5：等待修订增加正确索引而不删除完整性检查

**根因**：上轮新增终态等待收口会按 task_id 查询修订，原表只有 revision_id 主键，形成每次热写全历史扫描。

**修复**：schema100 增加 `idx_waiting_cognitive_task_revisions_task ON waiting_cognitive_task_revisions(task_id)`。完整任务终态收口、修订哈希与证据验证不变。

**验证与风险**：迁移后 EXPLAIN 使用 task_id 索引；既有等待任务/终态一致性和规模检索回归保留。首次建索引需要磁盘与迁移时间，不能在生产无备份直接试跑；索引会有少量后续写入与存储成本。

### 2.7 R6：发布身份与实际构建输入一致

**根因**：手工安装从工作树 pip 构建，却直接以 HEAD 申报版本和继承门禁身份；tracked 修改、untracked/ignored 额外代码可能混入不同内容。

**修复**：新增 `../../scripts/lib/release-source.sh`。安装有 commit 身份时必须 HEAD 一致且无 tracked/untracked 脏改，再用 `git archive` 固定 commit 快照；后续包、锁文件、unit、脚本来自快照。拒绝不支持的 submodule/快照 symlink，不把未校验额外输入混进同一 SHA。清理快照后安装报告器仍保留可用路径。

**验证**：dirty tracked、untracked、错误 SHA、submodule 拒绝；ignored 文件不进快照；快照建立后原工作树变化不污染已固定输入；安装器预检仍早于停止旧服务。

**操作影响**：工作区有本轮未提交改动或未跟踪审计文档时，不能直接冒充现有 HEAD 发布；应在后续授权的提交/发布流程中整理。无 Git 来源仍不能继承 commit 门禁身份。没有对本轮工作区进行自动清理或提交。

### 2.8 R7：新 venv 不再从 staging 搬迁

**根因**：pip 生成 console shebang 指向创建时解释器路径，移动整个 venv 后旧 staging 路径消失。

**修复**：`../../scripts/install-ubuntu.sh` 在最终 release 目录创建未发布环境；构建、导入、备份校验通过后才选 current/previous，失败清理未发布目录；不移动新 venv。

**验证**：最终目录经 current 符号链接调用真实 pip 成功，覆盖长路径/含空格情况下 distlib 的 shell trampoline；静态契约仍检查打包入口预检、停旧服务、发布与指针切换顺序。

**边界**：原安装器用于收编更早 legacy 环境的兼容路径仍在；本轮不擅自重构已有生产 legacy 环境。R7 修复针对正常新 release 创建/发布路径。

### 2.9 R8：先确定凭据源，再原子轮换与恢复

**根因**：旧脚本不识别 CREDENTIAL/inline 来源就写 FILE 配置并重启，可能制造多源冲突并中断服务。

**修复**：

- `../../scripts/rotate-operator-token.sh` 先锁定轮换，Python 解析配置但不 source；校验绝对常规文件、无 symlink、root ownership、权限、nlink、重复项、FILE 路径一致和旧 token 格式。
- 只支持 FILE-only 来源。CREDENTIAL/inline/重复或不匹配在更改 token 和重启前拒绝；没有假装实现 systemd credential 轮换。
- 不改配置文件；原子替换 token，保存原所有者与模式。原来 stopped 的服务保持 stopped。
- restart 失败恢复旧 token 并尝试恢复服务；HUP/INT/TERM 与 EXIT 中断也走恢复逻辑。无法恢复时保留受保护恢复副本并明确告警，不打印 token。

**验证**：Linux root 离线文件权限与 mock systemctl；credential/duplicate/mismatch 零修改、成功、restart 失败、原服务 inactive、发布后 TERM 中断恢复。不会调用真实 systemctl 管理 Noyra。

**边界**：SIGKILL/断电无法由 shell trap 捕获，不承诺断电事务或自动恢复正在管理的 credential 源。root 可控制的配置路径假定为可信管理边界；不把脚本当成抵御恶意 root 的沙箱。

### 2.10 R9：不再仅凭 PID 文件终止进程

**根因**：Windows PID 复用时，旧数字 PID 记录可能指向无关程序；Stop-Process 按 PID 重新查找还存在验证/执行时间窗。

**修复**：`../../deploy/windows/noyra-lifecycle.ps1` 使用 JSON 身份记录：format/process_id/UTC 启动 ticks/受允许安装 launcher 路径/version。拒绝旧数字 PID-only、损坏/超大/reparse 记录。固定 Process.Handle 后比较身份，对同一 Process 对象关闭/等待/必要时 Kill，不重新按 PID 查找；启动/停止/卸载由文件锁串行，已存在匹配实例时不重复启动。

**验证**：8 个 Windows mock 场景：身份匹配、PID 复用、错误路径、不存在、旧 PID-only、损坏记录、重复启动、强制结束分支。没有终止真实用户进程；打包安装/升级/回滚原测试保持。

**操作影响**：已有 Windows 数字 PID 文件不能自动猜测转换。必须人工确认旧实例身份后再按明确流程启用新记录；安全失败关闭优于误杀其他程序。

## 3. 数据契约、升级与回滚注意

schema 从 99 升至 100，只增加一个输入哈希绑定表和两个索引；**99 及更早历史 DDL 指纹不改、不放宽验证**。

- schema100 DDL 指纹：`e056dd1ba603c0d6f44444b24c668e4008ed3701c36155dbb5e742e328f7616a`。
- schema100 structure 指纹：`47b11a9ef31e674e8d9610620d7b6acaf9e33aa263f1383c095edc9411a965a8`。
- 上线前冷备份并验证可恢复、检查磁盘空间，按现有维护/升级互锁和 readiness 流程发布。
- **旧 99 代码不能靠只切程序指针、手工把 schema marker 改回 99 来安全使用 100 库。** 如须退回，应遵循已验证的备份恢复/维护流程；本轮没有自动降级生产数据或替用户丢弃新数据。
- migration/export/隐私擦除等契约均纳入相应回归；没有把新表从完整导出清单静默排除。
- 历史未知调用保留未知和审计证据；没有批量改 failed、取消预留、支付或重发。

## 4. 验证结果与中间失败闭环

以下各组有重叠，**不把重复执行相加成唯一测试总量**。离线测试不等于外部门禁。

| 检查组 | 结果 | 证据（目录见下） |
|---|---|---|
| Windows：新增核心/私密擦除/有界检索/进程身份 | 70 passed | `final-supplement.log/xml` |
| Windows：最终 schema100、身份隔离、生命周期、导出契约 | 35 passed | `final-schema100.log/xml` |
| Windows：曾失败的服务/发布顺序 5 项复验 | 5 passed，3 subtests passed | `service-deployment-v2.log/xml` |
| Linux：最终 schema100 核心、索引查询计划、契约、HTTP 导出 | 34 passed | `linux-schema100-final.log/xml` |
| Linux：最终源码快照、token 轮换、安装权限与维护 CLI | 31 passed，1 skipped | `linux-operability-final.log/xml` |
| 本轮较广 Windows 回归 | 269 passed，27 skipped，175 subtests passed | `expanded-v2.log/xml`；较早批次，后续核心变更另作上述复验 |
| 前端回归 | 132 passed | `frontend.log` |
| Ruff | 全部通过，473 个文件格式检查通过 | `ruff-final.log`、`ruff-format-final.log` |
| mypy | 452 个源/测试文件无问题 | `mypy-complete.log` |
| Shell 语法 | 49 个脚本通过 | `preservation-and-shell-final.json` |
| Git diff / 证据保全 | diff --check 通过，原 22 个证据及原审计报告不变 | `final-verification.json` |

以上日志均在：`../../.runtime/20261022-audit-remediation`。

另保留两组不能冒称整体全绿的较广运行：

- `integration.log/xml`：168 passed、5 failed、5 skipped、19 subtests passed。失败对应导出归属、推理额度及旧 staging 文本契约，根因和契约修订后由最终服务/发布顺序 5 项复验全部通过。
- `linux-final-v5.log/xml`：238 passed、1 failed、1 skipped、3 subtests passed。唯一失败是测试环境没有安装本项目，子进程不能 `python -m noyra.cognitive_wait_maintenance`；不是维护命令执行后错误修改数据。后来在独立 venv `pip install --no-deps --no-build-isolation -e .`，不再依赖会泄入隔离夹具的 PYTHONPATH，由最终 operability 组复验。同一较广批次之后增加的业务身份索引，由上述 Windows 35 项和 Linux 34 项最终 schema100 组验证。

因此可表达为：本轮已确认失败均有明确原因与后续复验闭环；不能表达为“全仓全部测试一次最终完整运行全部通过”。

### 4.1 修复过程中捕获并处理的回归

1. 新增输入绑定表后最初未登记 schema100 runtime-export ownership，导致异步导出 failed。已增加正式父表归属规则和双主体导出测试，不放松表清单校验。
2. 每次尝试准入检查生命周期后，退避期间暂停会在第二次 reservation 之前抛错；最初 gateway 未收口此位置。已统一记录 `model_lifecycle_blocked` 并把测试改为真实账本状态：只有一次已失败 attempt，不制造不存在的第二 reservation。
3. 路由层通用 ModelCallStateError 原本可能把新的生命周期拒绝映射为 in-progress。已新增专门处理并验证原始错误/无 HTTP/无 provider 健康失败记录。
4. 对同 purpose 历史范围扩大后，不能把正常结算历史累积进 256 条未完成路由上限。已限制到当前路由、未完成状态及未解除授权，并补 301 条历史之后 unknown 的回归。
5. Windows 导出两项在内容已正确完成后仍超出旧测试 10 秒：同环境一次离线对照中，未修改 HEAD/schema99 为 12.484 秒，schema100 为 14.515 秒，均完成。Windows 测试现使用有界 30 秒并把轮询降至每 0.5 秒一次以遵守原有 120 次/分钟限流，保留所有内容/认证/脱敏断言；没有修改生产超时来迎合测试，也没有据单次测量声称无性能差异。
6. Linux 初次环境缺 eth_account 和 pytest-asyncio，后来从本地 wheel cache 按现有锁与哈希离线补齐；没有改依赖锁。显式 PYTHONPATH 泄入安装权限隔离夹具使其导入真实源码而不是最小 mock，已在最终测试启动环境移除此额外变量，并在独立 venv 安装本项目以支持真正的 CLI 子进程。
7. 新测试曾错误假设存在 provider_health_attempts；当前健康存储只保留有界 buckets/state，已改验证实际 buckets，无需给产品新增无用表。长路径 pip 测试支持 shebang 与 shell trampoline 两种合法形式。最后修改 Shell helper 后出现的 CRLF 已恢复为 LF，并将拒绝路径测试从“任意非零”收紧为预期退出码 1，避免语法/加载错误被误当成成功拒绝。

中间失败日志全部保留在 `../../.runtime/20261022-audit-remediation`，应以最终指定运行与对应闭环结果判断，不把 earlier failed 日志删除来制造全绿。

### 4.2 证据保全

- 原 22 个 `output/20261020-audit-*` 文件逐一与只读审计保存的 SHA-256 比对一致。
- `../../docs/audit/2026-10-22-independent-readonly-audit.md` SHA-256 保持 `9038ec29feb400654d9b2f9df5da7b778d2367e4f5bfb35987be85b6de60cb6a`。
- 没有暂存、提交、推送、合并或生产部署；最终 diff 仅包含本轮修复、相关测试与新文档。

## 5. 最终建议与质量边界

建议保留本轮根因修复，并在后续得到发布授权后，经提交身份固定、CI、备份/恢复核验和真实外部门禁再上线。不建议为了避开 schema100 或测试变动整体回滚 unknown 隔离、生命周期派发检查、向量输入绑定等保护。

代码修改、回归覆盖和失败闭环可作高质量交付依据；**这不是“绝无次生问题”的数学保证，也不等于已经生产验收通过**。尚未完成：真实模型 60–100 秒慢响应、真实 SSH/系统服务升级、真实 systemd credential 轮换、真实付款/打赏/渠道投递及完整八项外部门禁。未进行全仓全部 pytest 的一次最终完整重跑。

针对较高改动风险的 R1/R4，生产验证应优先确认：同一业务轮次未知结果不因跨日/换资源自动重发；睡眠/暂停前后没有新增未授权发送；合法反思与已发送结果仍能结算。R2/R5 应监测迁移耗时、初期 stale 向量数量、受限重建与等待收口查询耗时；这些指标不能通过清空历史来“恢复正常”。
