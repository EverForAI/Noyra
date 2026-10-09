# Noyra 全项目只读审计报告（2026-10-04 复核版）

## 1. 审计结论

本次复核以分支 `codex/wake-after-clean-restart` 的 HEAD
`fcf5b3b5f19924045c5fa5ec6ad7a57434780442` 为基线，数据库 schema 为 79。
代码层的迁移边界、运行时导出 ownership graph、迁移证明绑定和目标地址解析约束已经具备相应实现；本次没有确认 P0 问题。

仍有两类需要在发布前处理的事项：

1. **A01：CI 格式门禁确定失败**。干净 checkout 中有 24 个已跟踪 Python 文件未通过 `ruff format --check .`，因此 CI 不能形成绿灯。
2. **A02：真实环境发布证据缺失**。本地无法证明 Ubuntu/systemd、加密卷、备份恢复、双机迁移、独立 signer/KMS、真实链、HTTPS 代理和长期运行门禁已经通过；这不是可由本地单元测试替代的代码结论。

此外，长期运行还有两个容量与运维风险：模型/研究/行为记录目前全部保留，缺少完整的生命周期合同（A03）；冷 payload 压缩在 retention 失败路径之外执行，压缩失败不会写入 `retention_runs`（A04）。依赖漏洞扫描在当前 Windows 环境无法启动（A05），以及工作区存在用户未跟踪文件，污染“完整工作区”静态检查基线（A06），分别属于验证和发布卫生问题。

本报告只读审计并只新增本文件；没有修改业务代码、测试、部署配置、生产数据或远程仓库。已有未跟踪的 Pelican 页面、测试、计划和 `output/` 均原样保留。

## 2. 范围、基线与方法

- **时间**：2026-10-04，Asia/Shanghai。
- **代码基线**：`codex/wake-after-clean-restart`，`fcf5b3b5f19924045c5fa5ec6ad7a57434780442`。
- **数据基线**：`src/noyra/core/database.py` 的 `CURRENT_SCHEMA_VERSION = 79`。
- **审计范围**：`src/noyra` 的运行时、HTTP/管理台、身份与导出、完整性和加密存储、retention、provider/search、钱包与自动付款、迁移控制面/agent/target activation、升级协调；`scripts/`、`deploy/systemd/`、Docker、CI/release 工作流、相关测试及发布门禁文档。
- **方法**：静态阅读源码和配置；核对迁移、钱包、导出、保留策略和升级路径；执行测试、静态类型、格式、编译和依赖检查。
- **无法执行的范围**：当前会话没有 GitHub Actions、真实 Ubuntu/systemd、LUKS 设备、第二台迁移主机、独立 signer/KMS、真实链节点、Cloudflare/Caddy 或 24/72 小时 soak 环境，因此不能把这些外部事实推断为已通过。

## 3. 风险评级口径

| 等级 | 含义 |
| --- | --- |
| P0 | 无门槛远程执行、大规模机密泄漏、不可逆主体损坏或重大未授权资金损失。 |
| P1 | 关键服务/恢复能力不可用，或资金、主体完整性、单活所有权边界可能失效。 |
| P2 | 容量、配置、状态一致性、CI 质量门或需要人工恢复的可靠性问题。 |
| P3 | 文档、运维可观测性、验证覆盖或低影响兼容性问题。 |

“修复风险”表示实施修复本身可能引入的风险：低为局部格式/类型/验证改动；中为跨模块配置或持久化合同；高为导出隔离图、跨主机恢复、钱包/密钥及所有权状态机。触发概率是基于代码路径和常见部署的工程估计，不是生产遥测统计；没有部署数据的项目明确标为“未知”。

## 4. 问题总表

| ID | 问题 | 风险 | 修复风险 | 触发概率 | 建议 |
| --- | --- | --- | --- | --- | --- |
| A01 | 已跟踪 Python 文件未通过 Ruff formatter，CI 格式门禁失败 | P2 | 低 | 每次 CI 运行 100% | 立即修复 |
| A02 | 当前 SHA 缺少真实外部 release-gate 证据 | P1（发布阻断） | 高 | 证据缺失时 release workflow 100% 阻断；实际环境故障概率未知 | 立即作为发布阻断处理 |
| A03 | 模型、尝试、研究和行为记录没有统一删除/归档生命周期 | P2 | 中至高 | 持续运行数月后高，估计大于 50% 会造成可观测增长 | 长期试运行前修复 |
| A04 | retention 冷 payload 压缩失败不会形成 `retention_runs` 失败记录 | P2 | 中 | 正常运行低；磁盘压力或损坏 payload 时中 | 尽快修复 |
| A05 | 当前 Windows 环境无法完成 pip-audit | P3（证据缺口） | 低 | 当前环境 100%；依赖真实 CI 才能确定漏洞状态 | 在 Ubuntu CI 补证据 |
| A06 | 工作区有未跟踪页面、测试、计划和生成物，污染完整工作区检查 | P3 | 低 | 当前已发生 | 发布前区分 clean checkout 与本地工作区 |

## 5. 详细发现

### A01：Ruff 格式门禁在干净 checkout 中失败

- **风险等级**：P2，工程质量和 CI 发布门失效。
- **修复风险等级**：低。格式化通常不改变语义，但应在格式化后重新运行测试和 diff 检查。
- **根因**：`.github/workflows/ci.yml` 明确执行 `python -m ruff format --check .`；当前分支有 24 个已跟踪 Python 文件不符合 formatter 结果。完整工作区额外包含未跟踪文件，因此工作区检查显示 25 个文件，但干净 Git checkout 仍有 24 个已提交文件失败。
- **代码证据**：CI 工作流第 39 行；未格式化文件包括 `scripts/noyra-migration-agent.py`、`scripts/noyra-migration-runner.py`、`src/noyra/core/admission.py`、`database.py`、`runtime.py`、`service.py`、`src/noyra/migration/` 多个模块及对应测试。
- **影响**：每次 CI 的格式 job 都会失败；绿灯发布、合并和后续外部门禁无法建立可靠基线。它目前没有被证明会直接造成线上运行时崩溃。
- **触发条件**：在干净 checkout 执行 CI 或本地 `ruff format --check .`。
- **触发概率**：100%。这是确定性门禁失败，不是抽样问题。
- **建议**：立即对已跟踪文件运行 Ruff formatter，审阅纯格式 diff，然后依次运行 `ruff check`、全量 pytest、compileall 和 mypy。格式修复完成前不要把 CI 作为发布通过依据。

### A02：当前 SHA 的真实 release gates 尚未形成可验证证据

- **风险等级**：P1（发布阻断）；不等同于已确认线上故障。
- **修复风险等级**：高。需要真实主机、隔离环境、独立复核和签名证据，不能用单元测试替代。
- **根因**：`docs/release/external-gates.md` 要求对**同一 release SHA**提供签名 evidence，覆盖 `ubuntu_systemd`、`encrypted_volume`、`backup_restore`、`migration_fence`、`signer_kms`、`reorg_nonce`、`https_proxy` 和 `soak`。本次审计没有连接这些外部系统，也没有拿到对应的成功 artifact。
- **影响**：无法证明真实 systemd 生命周期、LUKS 重启/恢复、跨主机单活切换、独立 signer/KMS 拒绝与轮换、nonce/reorg 状态机、HTTPS 代理认证限速以及 24/72 小时存储水位。若直接开放无人值守迁移或自动付款，可能把未验证的环境假设带入资金和主体状态机。
- **触发条件**：发布当前 SHA、开启自动付款、把迁移审批改为无人值守、或把服务作为长期公网实例运行。
- **触发概率**：在缺少 evidence 时，release workflow 被阻断的概率为 100%；真实环境是否存在故障目前未知。
- **建议**：立即保留为发布阻断。按文档在真实环境产生同 SHA、带独立 reviewer 和 Ed25519 签名的 gate artifact；在 artifact 被 workflow 验证前，迁移保持默认关闭/人工审批，自动付款保持限额和紧急暂停保护。

### A03：高频运行记录缺少完整的数据生命周期合同

- **风险等级**：P2，容量、备份、完整性扫描和恢复窗口风险。
- **修复风险等级**：中至高。涉及外键、审计证据、导出和恢复合同，不能用简单全表删除替代。
- **根因**：`src/noyra/core/retention.py:120-138` 将 `model_calls`、`model_attempts`、`research_search_runs`、`action_deliberation_runs` 和 `behavior_logs` 登记为 `preserve`，没有 cutoff；`src/noyra/model/ledger.py` 会为模型调用和每次尝试写入记录。现有 `compress_cold_payloads()` 只压缩冷的 request/response payload，不删除 ledger 行、attempt 行或研究/行为历史。
- **影响**：模型、搜索、研究和行为持续运行数月后，SQLite 行数、索引、备份大小、完整性扫描时间和恢复耗时持续增长。当前“必要审计记录”和“可聚合历史”没有逐表定义，难以判断哪些数据可安全压缩、归档或删除；这会使长期运行的磁盘水位和恢复窗口不可预测。
- **触发条件**：启用模型、搜索、研究或行为功能，并以真实调用量持续运行数月。
- **触发概率**：持续使用下出现明显增长的概率高，估计大于 50%；达到磁盘耗尽的具体时间未知，取决于调用速率、payload 大小和卷容量。
- **建议**：长期试运行前定义逐表生命周期合同：永久保存的付款、安全、迁移和故障证据；可聚合的指标；可压缩的冷 payload；可归档/删除的临时记录。为有外键的表设计父子联动、摘要替代和恢复校验，并用加速 soak 验证数据库、WAL、备份和完整性扫描水位。不要直接删除全部模型调用历史。

### A04：retention 的压缩失败没有写入失败运行记录

- **风险等级**：P2，故障可观测性和重试状态不完整。
- **修复风险等级**：中。需要调整持久化边界，避免压缩成功/失败与 retention 游标出现不一致。
- **根因**：`RetentionManager.run_batch()` 在 `src/noyra/core/retention.py:456-467` 先调用 `ModelLedger.compress_cold_payloads()`，随后才在约第 521 行进入包围删除和 `retention_runs` 写入的 `try`。如果压缩因磁盘满、损坏 JSON、数据库锁或其他异常失败，异常会在建立 `run_id`、插入失败记录之前向服务层传播；约第 588 行的失败处理无法覆盖这段代码。
- **影响**：该次失败不会形成 `retention_runs` 的 `failed_reason`、`failure_stage`、`retry_at` 和 `failure_count`。管理台和运维人员看不到完整失败原因，周期任务会重复失败；在磁盘压力时，恰恰最需要的故障记录也可能缺失。
- **触发条件**：启用冷 payload 压缩且出现磁盘满、数据库锁/损坏、异常 payload 或压缩实现错误。
- **触发概率**：正常运行低；在磁盘压力、异常数据或恢复操作期间为中等。没有生产故障率数据，不能给出更精确数字。
- **建议**：将压缩纳入同一失败记录边界，或为压缩建立独立但可关联的 retention run 阶段；无论压缩失败还是删除失败，都记录阶段、错误类别、重试时间和幂等状态。增加磁盘满、损坏 payload、锁冲突和重试后成功的回归测试。

### A05：依赖漏洞扫描在当前审计环境未完成

- **风险等级**：P3，依赖安全状态的证据缺口；不是已确认漏洞。
- **修复风险等级**：低。应在兼容的 Linux/容器环境执行既有扫描，不应因本地编码问题修改依赖约束。
- **根因**：本次在 Windows 环境运行 `pip-audit` 时，`pip_api` 启动系统 pip 发生非 UTF-8 解码错误，扫描程序在产生依赖报告前退出。
- **影响**：本地审计不能确认 lock 文件依赖是否有已知漏洞，也不能把“扫描失败”解释为“无漏洞”。CI Ubuntu 任务仍是应保存的权威证据来源。
- **触发条件**：在当前 Windows Python/系统编码组合中执行 pip-audit，或 CI 依赖扫描失败却被误当作通过。
- **触发概率**：当前环境 100%；实际依赖漏洞概率未知。
- **建议**：在 Ubuntu CI、官方容器或与发布环境一致的隔离环境运行 `pip-audit -r requirements.lock`（或仓库既有等价命令），保存同 SHA 的完整输出；扫描失败时让门禁失败，不要静默跳过。

### A06：本地工作区的未跟踪内容污染完整审计基线

- **风险等级**：P3，发布卫生和验证可重复性问题。
- **修复风险等级**：低，但涉及用户文件，不能擅自删除或覆盖。
- **根因**：`git status --short` 当前显示未跟踪的 `docs/superpowers/plans/2026-10-03-pelican-bicycle-animation.md`、`output/`、`site/pelican-bike.html` 和 `tests/test_pelican_bicycle_page.py`。这些文件不属于当前 HEAD，但会被未限定路径的格式、mypy 或测试命令看到。
- **影响**：完整工作区和干净 Git checkout 的检查结果不同；例如完整 mypy 额外受到未跟踪 Pelican 测试缺少返回类型注解的影响，完整 Ruff 格式统计也多出未跟踪文件。若误把这些结果用于 release 决策，可能错误修复用户文件或遗漏已提交代码的问题。
- **触发条件**：在工作区根目录直接运行 `ruff format --check .`、`mypy src tests` 或打包脚本。
- **触发概率**：当前已发生；只要未跟踪文件继续存在，复现概率接近 100%。
- **建议**：审计和发布记录必须注明是 clean checkout 还是本地工作区；CI 使用 Git checkout 作为权威基线。用户决定之前保留这些未跟踪文件，不用删除来“制造干净状态”；如需发布，使用临时干净 checkout 或明确的路径清单。

## 6. 已确认有效的控制（不重复列为未修复问题）

- 迁移默认关闭，并具备目标注册、PoP、recipient X25519 密钥、加密 bundle、分块 hash、restore、activation、rollback 和 admission fence 代码路径。
- 迁移 receipt 已绑定 recipient、volume、credential、wallet/signer proof 摘要；目标身份和 endpoint origin 参与 attestation challenge。
- 迁移 endpoint 在连接前解析并固定已验证地址，默认阻断 loopback、link-local、保留/多播、未指定和云元数据地址；私网地址需要显式 allowlist。
- 运行时 schema 79 已有显式 export ownership graph，跨主体导出边界采用 fail closed 设计。
- 钱包状态机包含余额不足、Gas 过高、Nonce 冲突、确认超时、链重组等状态，以及单笔/日限额和紧急暂停保护。
- 管理台具有 token、会话、CSRF、限速、安全 cookie 和安全响应头控制；provider/search 具有健康统计、故障切换和 cooldown 路径。
- Docker/Ubuntu 入口默认保持本机/loopback；Cloudflare/Caddy 配置会校验 `NOYRA_HOST=127.0.0.1`。

上述代码控制仍需 A02 的真实环境 evidence 才能成为可发布的运行保证。

## 7. 本次验证记录

| 检查 | 结果 | 解释 |
| --- | --- | --- |
| 全量 pytest | `1815 passed, 25 skipped, 259 subtests passed` | 当前工作区测试通过；未跟踪文件仍应与提交基线区分。 |
| Ruff check | 通过 | 规则检查通过；不代表 formatter 门禁通过。 |
| Ruff format check | 干净 checkout 的已跟踪 Python 文件仍有 24 个未格式化 | CI 的确定性阻断，见 A01。完整工作区统计为 25 个。 |
| compileall | 通过 | 未发现 Python 字节码编译错误。 |
| pip check | 通过 | 当前环境依赖元数据无冲突；不等同于漏洞扫描。 |
| mypy（已跟踪代码/测试） | `Success: no issues found in 342 source files` | 排除了未跟踪的 Pelican 测试；完整工作区结果不能作为 HEAD 结论。 |
| pip-audit | 未完成 | Windows `pip_api` 非 UTF-8 解码错误；见 A05。 |

## 8. 建议处理顺序

1. 先在干净 checkout 格式化并重新验证 A01，确保 CI 的基础质量门恢复绿灯。
2. 在真实 Ubuntu 和隔离主机上按 `external-gates.md` 逐项产生 A02 的同 SHA 签名 evidence；在证据完成前保持迁移默认关闭/人工审批，不开放无人值守自动付款。
3. 设计并评审 A03 的逐表 retention 合同，再实现压缩、聚合、归档和删除；用加速 soak 验证容量、WAL、备份和恢复。
4. 将 A04 的压缩阶段纳入失败记录边界，补充异常、重试、磁盘满和锁冲突测试。
5. 在 Ubuntu CI/容器中补齐 A05 的依赖扫描证据，并把扫描失败保持为门禁失败。
6. 发布前用 clean checkout 复核 A06，保留用户未跟踪文件并单独记录其验证结果。

在上述顺序完成前，不能把“本地测试通过”表述为“生产迁移、自动付款或长期公网部署已验收”。
