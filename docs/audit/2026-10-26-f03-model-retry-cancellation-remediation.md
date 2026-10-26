# F03 重试等待取消修复与复审（2026-10-26）

基线：`b76a12c3cc11e9bfbed00dfa924aaa0b7a2a467a`；分支：`codex/fix-f03-model-retry-cancellation`。本轮只处理独立审计中的 F03，保留已完成 F01；F02 继续暂缓，F04 不在本轮范围。

## 问题、根因与修复边界

F03 为原生 P2，修复风险中。ModelGateway 的供应商请求 await 具有取消结算，但已知可重试失败及结构化输出无效之后的两个退避 await 位于该处理范围之外。此时上一尝试已落盘、下一尝试还没有发送，取消会使逻辑调用继续保持 executing，completed_at 为空。同物理幂等键后续请求被拒绝，启动恢复还可能将已知无效输出误归为 unknown。

先新增两条隔离回归，再运行修复前代码：两条均失败于“预期 failed，实际 executing”，确认缺陷及测试切入点，随后实施最小变更。

## 模块与提交

- `b9843ea`：状态设计、修复顺序、安全边界和验收计划。
- `8f493bd`：生产最小实现、13 项直接取消/计费/重启回归和模块复审。
- `a03d7e2`：15 项路由/运行屏障/人工未知重试/并发集成验证及运维说明。
- 最后提交：最终验证、复审记录和发布清单更新；确切 SHA 在交付记录提供，避免在被校验文档内自引用。

每个模块验证及复审后提交。没有为当前问题重写数据库账本、路由协议或全局恢复策略。

## 生产变更与状态语义

生产代码只修改 `src/noyra/model/gateway.py`：两个退避入口改为调用同一个 `_retry_after_settlement(call_id, completed_attempts)`，该方法执行原有退避，捕获 CancelledError 后用已有 finish_call 写 failed / model_retry_cancelled 及完成时间，再原样传播取消。

| 取消位置 | 结果 | 尝试与预算 |
|---|---|---|
| 已知失败后等待下一次发送 | failed / model_retry_cancelled | 保留已结算失败；未知用量仍保留原预算 |
| 已收到无效结构化响应后等待重试 | failed / model_retry_cancelled | 已付费/估计用量记录不变；不伪造可缓存成功输出 |
| 下一次请求已进入执行 | 保持 unknown / model_call_cancelled | 请求可能已计费，保留未知预约，不自动重放 |
| 供应商直接返回结果未知 | 保持原有 unknown 与人工重试约束 | 不进入新的退避取消分支 |

这里 failed 描述逻辑调用未产生可交付结构化结果，不意味着没有发生过成功推理或费用。attempt、token、费用、保留金额、供应商 request ID 和原始终结时间均不由本修复改写。

现有 finish_call 的同步 accounting_scope 事务可在 lease 已失效时完成必要结算，仍保留私密来源删除等原有语义。收尾没有额外 await、没有新供应商发送或普通主体写入；重复取消不会在该事务中另开异步中断窗口。数据库写入失败仍可见，不能吞掉后声称收尾成功。

## 模块一复审

13 项直接测试覆盖两个原始分支、已知/未知/估计用量、单次及重复取消、第二次退避的累计费用、请求中取消、正常重试与缓存、写入失败可见。

- 在可控退避中真实执行 task.cancel，调用落为 failed，完成时间存在，response_json/response_hash 不伪造。
- 所有既有 attempt 按完整行比较不变；预算前后相等；付费无效输出明确保留 7+3 token 和测试定价的 13 microusd。
- 未知用量失败仍保留 reserved 值；没有将失败调用视为“可释放全部预算”。
- 后续同物理幂等键请求不发送；调用已终结后 recover_interrupted 不再次改分类。
- 第二次退避取消保留第一次付费输出及第二次未知用量预约，不提前授权第三次。
- 请求执行中取消仍为 unknown，原保留不变；正常两次尝试成功后可缓存且不重发。
- 模拟 finish_call 写入失败时，数据库错误显式传播，上一尝试账目保留；不返回一个已成功取消结算的假象。

## 模块二复审

新增 15 项集成测试，使本轮专用测试共 28 项。

- 暂停 epoch、完整性隔离、关闭排空、迁移屏障失效分别覆盖两条退避分支；在失效许可下取消可完成终态账目，无下一次供应商发送，lease 最后释放。
- 外层 asyncio.wait_for 超时驱动取消，两分支均写调用终态。
- 通过真实 CognitiveResourceStore 和 RoutedModelGateway 选择两组资源、各两把测试密钥，仅用 FakeProvider 避免外部费用。取消的当前任务只发送一次，不切备用 key/group；逻辑锁释放，后续不同的明确请求可用；路由完整性校验通过。
- 先制造真实 unknown 调用，再执行现有 prepare_unknown_retry 人工授权：取消该重试的退避后，旧 unknown attempt、原授权审计与预算保留均不变。同逻辑请求仍隔离，旧授权不会变成无限发送许可；没有生成伪造的关闭/对账证据。
- 一条调用在退避期间取消，另一条已成功调用的完整记录与所有尝试不被改写；没有跨调用串用 call_id。

模块二没有修改生产路由。测试证明原有隔离与锁清理能够承接最小修复，无需为解决 F03 扩大实现范围。

## 最终回归结果

本机 Windows / Python 3.12.10 的实际结果：

| 验证范围 | 结果 | 证据与边界 |
|---|---|---|
| 模型网关、资源授权、wire 契约、F03 专用回归 | 117 passed；另 175 subtests passed | F03 新增 28 项包含在 117 内，不重复计数；subtests 数来自 pytest 完成输出 |
| 外部发送屏障、gate0 运行所有权/关闭、R05 嵌入排空 | 80 passed | 未更改相关生产代码，验证取消收尾不会使过期操作继续发送 |
| 不同 pytest 用例合计 | 197 passed，0 failed，0 skipped | 仓库外 JUnit 去重核对；不将子测试冒充额外独立用例 |
| 全库 Ruff 与格式 | 通过 | 795 个格式检查文件 |
| 严格 Mypy | 483 个源文件通过 | src、tests 与公开清单工具 |
| 生产差异复审 | gateway.py 增 17 行、替换 2 行 | 只增共享 helper 及两处退避调用点 |

最终验证命令：

```sh
python -m pytest -q tests/test_model_gateway.py tests/test_model_authorization.py tests/test_model_wire_contract.py tests/test_model_retry_cancellation.py
python -m pytest -q tests/test_external_dispatch_fencing.py tests/test_gate0_runtime_boundaries.py tests/test_audit_r05_embedding_shutdown.py
python -m ruff check .
python -m ruff format --check .
python -m mypy src tests scripts/public-file-manifest.py
```

没有重复整个项目的所有运行时/前端/Windows 安装测试；执行范围针对本次唯一生产差异和其相关屏障。供应商由隔离夹具、MockTransport 等替代，没有真实付费请求；不能由此声称实机账单对账已完成。

JUnit 与静态检查原始结果保存在仓库外目录 `D:/metagrowh/.user-temp/noyra-f03-validation-kQCpRP`，文件为 model-regression.xml、runtime-boundaries.xml、test-summary.json、ruff-check.txt、ruff-format.txt、mypy.txt。JUnit 中没有单独输出的 subtests_passed 字段，175 个子测试以 pytest 完成输出为据，不从该字段 0 推断未执行。

公开范围最终为 981 个文件、980 个 SHA-256 条目、202 个明确排除路径。提交前按 Git 暂存区生成并校验；最后提交后以 HEAD 和另一套 Git 原始 blob 哈希/ZIP 比对独立核验。最后提交 SHA 与末次结果在交付记录中提供，避免文档自引用。

## 原生/次生与差异审查

F03 已存在于最早公开提交的退避路径，本轮属于定向补齐原生缺口。生产差异为一个共享 helper 和两个调用点；数据库 schema、ModelLedger、预算公式、未知重试审计、资源路由实现、发送前授权、计费结算、Windows F01/R03、迁移及钱包实现均未修改。

复审和测试没有发现本次需要继续扩大的阻断问题。不把通过的定向测试表述为证明不存在任何次生缺陷；实际供应商、长时间运行、突然进程退出及数据库不可写仍有各自边界。

## 运维与历史记录

状态说明已更新到 [轻量运行观测](../deployment/runtime-observation.md)。新失败码是本地取消原因，不应统计成供应商身份/密钥错误，也不代表费用为零。

本轮不批量修改已有 executing/unknown，也不把“有一次成功 attempt”当作可以取消历史未知的证据。旧记录可能已发送下一次请求，需要按既有恢复/供应商对账和人工重试流程处理。

进程在最后尝试结算与调用终结之间被强杀或断电，仍可能进入保守启动恢复；这不是协作式 CancelledError，本轮不以新 schema 或历史重分类来推测解决。账目写入故障也不会被静默隐藏。

同一个物理幂等键的 failed 调用不自动重发；普通已知失败之后的新逻辑路由请求仍按既有规则运行。含历史未知和未消费人工重试授权的请求继续隔离；不能通过取消等待来释放旧未知责任。

## 交付范围与下一步

源码修复、针对性验证与模块提交完成后，保持本地工作区干净。本次不推送、不合并、不部署，不执行真实供应商调用或付费。合并时应让远端 Linux/Windows 原有门禁验证同一提交；本地 Windows Python 验证不能当作远端 CI 已成功。

F02 保持暂缓。后续可单独统一 F04 的生产签名策略与原型验证契约，先决定规则，再实施，不应混入本次模型取消修复。
