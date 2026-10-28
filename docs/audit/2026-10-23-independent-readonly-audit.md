# 2026-10-23 独立逐代码只读审计

## 审计范围与方法

- 审计时间：2026-10-23（Asia/Shanghai）。
- 工作区：`[私有验证记录]`，分支 `codex/terminal-waits-operability`，HEAD `b0162fd40cb436bbed34e9dc0a15c3a8313d45b1`。
- 对象是审计开始时的**实际工作区**，包括 29 个相对 HEAD 已修改文件及当时已存在的未跟踪文件；不是只看 HEAD，也不表示这些更改已部署。
- 本轮没有改产品代码、测试、配置或旧报告；没有暂存、提交、推送、连接生产服务器、调用真实模型/支付、启动合成 Windows 启动器。唯一新增的仓库文件是本报告；隔离复现输出写入被忽略的 `.runtime/20261022-independent-audit-round2/`。
- 建立了审计前 1,098 个非忽略工作区文件的 SHA-256 基线。最终检查确认它们没有被本审计改写；此前已有的工作区差异均保留。
- 全部 223 个产品 Python 文件完成 AST 解析，解析错误 0，统计到 3,945 个函数定义。`pytest --collect-only` 收集 3,065 项。全仓 Ruff 和全仓 Mypy（452 个源文件）通过。
- 逐行重点追踪了本轮变化涉及的 Windows 包构建/启动、未知模型调用状态机及审计、embedding 配置和向量索引、schema 101 迁移/导出、服务 API 与诊断；并对 SQLite 审计写入、签名身份、来源状态和前端投影做了交叉核对。未运行全仓测试，也未逐人工阅读 223 个模块的每一行；本报告是对选定高风险边界的可复核审计，不是形式化证明或绝无遗漏声明。
- 合成复现没有真实网络或真实密钥。Windows 签名测试仅读取系统 `whoami.exe` 的 Authenticode 证书并将字节放入临时包；没有运行该副本。

## 结论

确认 6 项需要处理的问题：**P1 1 项、P2 4 项、P3 1 项**。修复风险与问题风险分开评级；归因列说明它是现存原生缺口还是此前修复的次生/未闭合问题。

| 编号 | 问题 | 风险 | 修复风险 | 归因 |
|---|---|---:|---:|---|
| R1 | Windows `RequireSignature` 验证签名有效性但不绑定 Noyra 发布者；清单本身未签名 | P1 | 高 | 原生发布信任缺口；库存闭合修复后仍存在 |
| R2 | 官方 Windows 包构建器没有生成生命周期启动器；install 成功但 start 必然失败 | P2 | 中 | 原生构建器/生命周期集成缺口；本轮加固使失败明确化 |
| R3 | 任意格式合法的取消审计记录可关闭失败未知重试围栏，完整性审计仍通过 | P2 | 高 | 此前修复新增“失败后关闭”语义时产生的次生授权缺口 |
| R4 | managed embedding 的运行时授权和完整性验证未绑定所有持久配置字段 | P2 | 中—高 | 原生配置完整性缺口；本轮 desired/effective 展示只比较资源 ID，未补齐 |
| R5 | 未配置 embedding 维度时，供应商默认输出维度变化不会使旧向量过期 | P2 | 中 | 本轮向量空间修复未覆盖的次生边界 |
| R6 | embedding 列表把已停用但仍被内存对象引用的资源标为 `runtime_effective` | P3 | 低 | 本轮运行时状态投影引入的次生显示不一致 |

本次没有发现证据足以建议回滚整轮修复。R3、R4、R5、R6 应补修；R1、R2 是 Windows 发布契约尚未闭环。R2 发布路径在实现真正可启动的包之前不应宣称安装后可运行。

## R1：签名没有绑定 Noyra 发布者

**位置**：`deploy/windows/noyra-lifecycle.ps1:127-136, 261-280`；`scripts/build-windows-package.ps1:85-105`。

**根因**：`-RequireSignature` 和 manifest 的 `signature_required` 只检查 `Get-AuthenticodeSignature.Status -eq Valid`。没有校验 signer thumbprint、证书主体、受信任发布者策略或外部签名 manifest。`noyra-package.json` 是普通 JSON，包内文件 hash 由同一个未签名 manifest 声明；因此 manifest 不能证明它来自 Noyra。清单闭合和逐文件 hash 能检测未按清单重算的篡改，但不建立包的发布者身份。

**触发条件**：安装者以 `-RequireSignature` 安装一个攻击者可提供或替换的包；包中的启动器是任意 Windows 信任链认可的 Authenticode 签名文件，并且攻击者为包重算普通 JSON 清单中的 hash。条件需要攻击者影响安装输入/分发来源，不是未认证 HTTP 请求直接执行。

**隔离复现**：把系统签名的 Microsoft `[私有验证记录]` 复制为合成包的 `noyra.exe`，给它正确 hash，设置 `signature_required=true`，执行实际 lifecycle install 并传 `-RequireSignature`，退出码为 0。证书主体为 Microsoft Windows，thumbprint `58FD671E2D4D200CE92D6E799EC70DF96E6D2664`。合成启动器没有执行。另一个只含 README 的空启动器包也能以严格签名模式 install 成功，说明没有要求启动器清单必须存在。

**影响**：严格签名选项不能表达“由 Noyra 发布者签署”。运营人员可能把“任意有效签名”误当作“Noyra 官方构建”，也无法用该包证明 Python 源码及依赖清单来自经过审核的发布 SHA。当前仓库本身说明 Windows 包仍是基础生命周期实现而非正式签名 MSI/MSIX，因此风险是发布信任属性缺失，不能扩张成已证明远程 RCE。

**修复风险与方向**：高。需设计受信任发布者约束并签署不可变 manifest/包摘要；将官方构建 SHA、签名身份、包库存绑定，并验证可执行文件的签名身份及非 PE 源码清单。更新证书轮换、离线验证、开发包/未签名测试包的明确策略。只再加一条“签名状态为 Valid”不能解决此问题。

## R2：Windows builder 输出不能由 lifecycle 启动

**位置**：`scripts/build-windows-package.ps1:60-68`；`deploy/windows/noyra-lifecycle.ps1:205-221`；`deploy/windows/start-noyra.ps1:1-8`。

**根因**：官方 builder 复制 `src`、PowerShell source launcher、README、pyproject 和 lock 文件，没有生成或纳入 `noyra.exe`/`noyra-desktop.exe`。lifecycle `start` 却只查找这两个 EXE，并不调用包内 `start-noyra.ps1`，也不建立 Python venv。`start-noyra.ps1` 自己要求包根已有 `.venv\Scripts\python.exe`，builder 同样没有创建该环境。

**触发条件**：使用官方 `build-windows-package.ps1` 构建，然后按 `install`、`start` 的 lifecycle 流程操作。该条件必现，不需要真实 provider。

**隔离复现**：实际运行官方 builder；安装器 `install` 返回 0；manifest 和已安装目录均无 EXE；执行 `start` 返回 1，错误为 `Installed package has no launcher: audit-v`。没有执行任何启动器。

**影响**：用户能得到一个表面安装成功、但不能运行的发布包。最近修复选择允许 source-only 包安装、在 start 时失败关闭，消除了靠未清单文件偶然启动的路径，但没有完成正常用户的安装闭环。

**修复风险与方向**：中。先明确支持契约：若是 source checkout，应提供经过明确支持的 Python 安装/venv/bootstrap 启动路径；若是可分发包，应在 builder 中生成/携带真正 launcher 和依赖，并在 install 前验证入口点和运行时兼容性。不要让生产生命周期对“能 install 但必不能 start”的库存返回成功。加入 builder→install→start 的真实端到端测试；签名正例需 Windows 证书/发布 CI 门禁。

## R3：可伪造取消记录关闭未知重试围栏

**位置**：`src/noyra/model/resources.py:3305-3323, 3350-3374, 3425-3433`；`src/noyra/model/ledger.py:835-865, 877-949`；`src/noyra/core/integrity.py:3080-3094, 3163-3185`。

**根因**：路由只检查 subject、action 和 JSON 内 `call_id`，把任何 `model_unknown_retry_cancelled` 记录加入 `cancelled_call_ids`，不区分旧的“prepared、尚未发出”取消与新增加的“retry 已失败、人工确认接受重复风险”关闭事件，也不验证审计记录是否符合 call 当前状态。Core integrity 对基本形状 `{call_id, reason}` 的 cancellation 记录仍认可为合法，没有将其与 prepared/failed 状态和授权代次对应。SQLite 触发器禁止 audit update/delete，但没有限制这一 action 的 insert 只能经受控 operator 事务创建。

**触发条件**：某 `model_calls` 项已经 `unknown → operator retry authorization → retry sent → known failed`，其业务 purpose 仍被 quarantine；一个可以向该 subject SQLite `audit_records` 写入的本地代码/运维路径追加 canonical JSON：`{"call_id":"…","reason":"…"}`，action=`model_unknown_retry_cancelled`。不是匿名远程 HTTP 调用可直接提交该审计动作；需要数据库写入能力或等价的进程内写入缺陷。

**隔离复现**：使用合成 provider 完成两次 mock 请求（unknown + 已知 retry failure）。插入一条 canonical、subject/call_id 正确、旧格式的 cancellation 审计记录后，同一业务新 key 的 route disposition 从 `quarantine` 变成无 fence；`IntegrityRegistry` 的 `core.actions` 返回 `ok`。provider 请求数为 2，未请求第三次真实网络。

**影响**：未知原请求是否执行/计费仍然未知，但后续业务代次可以重新路由并再次请求；可能造成重复执行、额外计费和完整性审计未告警。这直接削弱“未知结果跨 key/跨轮次隔离”。

**修复风险与方向**：高。不同状态转换应使用不同审计 action/版本，并把授权代次、prepared 前是否发出、terminal attempt 状态、核对人、证据哈希和风险接受绑定到一笔事务；route 只接受状态匹配的有效转换。Core integrity 应检查因果顺序、授权存在、一次性消费和 call/attempt 状态，合法的 prepared cancellation 与 failed retry closure 分开验证。追加旧格式兼容规则时必须限定旧记录可适用的状态，不能再次让任意旧形状关闭 failed call。

## R4：managed embedding 配置未形成完整运行时/完整性绑定

**位置**：`src/noyra/model/embedding_resources.py:240-268, 395-449`；`src/noyra/core/integrity.py:3950-3985`；`src/noyra/service.py:4764-4784, 10956-10996`。

**根因**：`call_authorizer()` 对既有 provider 仅比较资源 `status`、`base_url`、`model` 和 API-key fingerprint，没有比较 `dimensions`、timeout、daily call/token/cost limits、单价、circuit threshold/cooldown 等运行参数。`embedding_resources` 没有覆盖这些字段的 state hash/revision；integrity 检查 `_from_row` 数值形状与 secret fingerprint，但不验证创建时持久配置是否被修改。当前 `embedding_runtime_status()` 只按 `config_id` 比较 desired/effective，也把“同一 ID 但运行参数已漂移”报告为 `applied`。

**触发条件**：部署/迁移/管理员维护/同进程异常写入对 active `embedding_resources` 的非身份字段做数据库修改，而当前进程继续持有启动时的旧 `EmbeddingSettings`。公开资源配置 API 当前不提供完整配置编辑，所以这是受限的数据库写入/恢复漂移条件，不是未认证 Web 请求直接触发。

**隔离复现**：只用合成数据库和 key。创建 dimensions=2、daily_call_limit=9 的 active 资源并构造 settings；直接把 SQLite 行改为 dimensions=3、daily_call_limit=0 后，旧 `call_authorizer()` 仍授权；`EmbeddingResourceStore.get()` 读到修改后的值；integrity check `model.embedding_resources` 为 `ok`。service 状态投影因资源 ID 未变也判定 `applied`。

**影响**：运行中的向量维度、费用上限、请求超时及电路策略可能与管理/持久配置不同。尤其预算被持久化调低后，运行内存中的旧额度仍可继续发出 embedding 请求；同时完整性审计未发现 drift。不能说低额预算通过常规管理 API 可绕过，因为当前没有对应编辑 API。

**修复风险与方向**：中—高。增加有版本的持久配置状态 hash/revision，精确绑定 provider 实例所用的所有非秘密字段；每次发出前确认当前 revision/hash 与实例相同，完整性检查重算并核对。避免把 API key 明文纳入状态哈希；密钥仍单独 fingerprint。对已存在没有 hash 的资源设计向前迁移/backfill 和失败关闭策略；不要只在显示层比较 ID。

## R5：dimension 未指定时，供应商默认维度变化不会使旧向量失效

**位置**：`src/noyra/model/embedding.py:106, 146-164, 245-248`；`src/noyra/mind/retrieval.py:147-161, 205-252`。

**根因**：space fingerprint 包含设置中的 `dimensions`，但 OpenAI-compatible provider 允许该字段为 `None`。实际响应维度只写在 `memory_embeddings.dimensions`，不参与 provider identity 或 missing 判定。查询发现 `dimensions != len(query_vector)` 时跳过旧向量，但没有把这种记录标成 missing。

**触发条件**：维度保持未配置，endpoint 和模型 alias 字符串不变，但供应商/部署调整该 alias 的默认输出维度；新查询返回维度 N+1，而库存向量仍为 N。供应商 alias 版本改变是外部条件，本地没有表示/探测实际向量空间的版本号。

**隔离复现**：同一 endpoint/model、dimensions=None、同一 fingerprint。Mock provider A 返回 2 维并索引一条 memory；provider B 改为 3 维。切换后 `missing()` 仍为空，`similarities()` 返回空字典。仅使用 HTTP MockTransport，无真实网络/计费。

**影响**：semantic retrieval 在该集合上会跳过旧向量，且既有启动 rebuild 不会更新它们；搜索依赖 lexical fallback，语义召回持续退化。本地 fallback 降低了数据丢失风险，但并不恢复语义排序。

**修复风险与方向**：中。将实际返回维度或明确供应商模型修订标识作为向量空间的有效身份，且保证查询向量与索引维度发生不一致时把相关记录纳入有界重建/隔离路径。需控制维度探测/重建费用，保留正文 CAS、隐私删除和词法回退。

## R6：停用资源仍会被列表投影标记为 `runtime_effective`

**位置**：`src/noyra/service.py:4764-4784` 与 `src/noyra/model/embedding_resources.py:240-268`。

**根因**：列表对每条记录仅以 `record.config_id == effective_resource_id` 推导 `runtime_effective`，没有要求 `record.status == "active"`。同一响应的 `restart_required` 又只在 `status == active` 时可能为真。实际发出前 authorizer 会拒绝 disabled/revoked 资源，因此显示投影与可用状态不一致。

**触发条件**：本进程启动时已装配 managed embedding A；操作员在运行期间 disable A；重新读取 `/api/config/embedding-resources`。运行对象仍保存 A 的 resource ID，但其 authorizer 已经拒绝新的 provider 请求。

**影响**：API 客户端会同时收到 `status=disabled`、`runtime_effective=true`、`restart_required=false`，可误认为该配置仍被当前服务有效使用，或者不清楚停用已阻止调用但旧对象尚未销毁。服务级 `embedding_runtime_status()` 与列表的 per-item 字段也会表达不一致。

**修复风险与方向**：低。把“内存中 provider identity 被引用”和“该资源当前获准使用”拆成不同字段，或至少让 `runtime_effective` 同时要求 active 且授权有效；补充 disable/revoke 之后列表与服务诊断一致的 HTTP 测试。不要为了让显示一致而放松发出前 authorizer。

## 全仓检查与边界

- 全仓 `ruff check src scripts tests`：通过。
- 全仓 `mypy`：452 个源文件通过。
- 全仓 pytest 收集：3,065 项成功收集；本轮没有执行全部 3,065 项。
- 隔离复现：Windows 构建/安装/启动；Microsoft 有效签名文件严格模式安装；未知调用取消审计；embedding 配置漂移；未指定向量维度变化。相关 JSON 在 `.runtime/20261022-independent-audit-round2/`。
- 未连接真实 release/GitHub 发布、Authenticode 发布者证书、生产数据库、模型供应商、signer 或区块链；没有从结果推断服务器状态。
- 当前实际干净发布是否强制 `RequireSignature`、Windows package 是否真用于生产分发、SQLite 本地写入者边界，都影响现实可利用性；报告按代码承诺和复现触发条件描述，不虚构远程可利用路径。
