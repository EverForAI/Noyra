# R05 实施、差异审计与验证记录

日期：2026-10-25（北京时间）。基线：8a629d49401fdc83d583a2341bd0886e69886ed0；设计提交：ddf2a32；分支：codex/audit-r05-shutdown。

## 1. 问题与修复范围

- 原问题：R05，P2，修复风险中至高；根因是 hard timeout 不终止 provider 线程，而 CognitionCycle 在事件循环里无上限等待该线程。
- 触发：真实 provider 授权器、响应处理/回调或自定义 provider 卡住，调用已经 hard timeout，随后关闭认知/服务。
- 原影响：关闭可能无限阻塞、外层异步取消失效。直接不等就释放所有权则可能引入更严重的旧工作/新运行实例重叠。
- 本次只实施 R05。R01/R03 的方案见同目录设计文件，未修改其 schema、历史来源或 Windows 信任协议。

## 2. 根因修复

1. Gateway 的关闭与新调用接入共用短临界区。关闭后新的 embed 在预算授权之前以 embedding_gateway_closed 拒绝，不新增用量或外部调用。
2. 追踪完整调用而不只是 provider worker/future；授权、提交前窗口、账本结算和 accounting 均包括在 _active_calls 内，finally 清理，不把“线程退出”误认为“全部用库工作结束”。
3. 同步 close(wait=True) 默认有限等待 1 秒；显式 timeout 允许 0–300 秒，拒绝负值、NaN、Infinity、bool。False 明确表示未排空，不是成功。
4. 新增异步 aclose，用可取消的短 asyncio.sleep 检查 drain；不阻塞事件循环、不将无限等待搬到默认 executor。借用的 ThreadPoolExecutor 仍由原调用者管理。
5. CognitionCycle 先排空 embedding；未排空抛固定错误 embedding_shutdown_incomplete，不提前关闭其他组件、不提前设置 _closed。并发关闭串行化，取消/失败后可重试。
6. 正常 Service.run 关闭失败不会走到 kernel.close。为同步 Service.close 增加同样的实际 worker 排空守卫，不能在 operation lease 已结束但 worker 未退出时释放进程锁；兼容部分初始化还不存在 cognition 的对象。
7. 不修改账本状态转换/预算/unknown 保留逻辑，不扩大 accounting_scope，不改主体 lock/epoch 算法或供应商请求超时；旧工作仍由原 admission/Database.transaction 围栏拒绝业务写入。

## 3. 反例与定向测试

- 在未修改产品的基线上重跑真实 OpenAIEmbeddingProvider 的原探针：网络 0 次；调用 hard timeout；usage=unknown；关闭外层 0.05 秒期限失效，实际等待外部 0.5 秒释放才返回（约 0.515 秒），没有 TimeoutError。
- 产品修复前加入两个关键反例：close 后仍能调用；cycle.close 阻塞导致外层 timeout 不抛异常。结果 2 failed，证明覆盖旧缺陷而非仅验证新接口。
- 修复后专项 22 passed。覆盖：默认有限等待、事件循环 heartbeat/外层取消、授权器和响应维度回调卡住、unknown 不被迟到结果覆盖、真实 Database.transaction 拒绝旧 lease 写入、完整调用结算窗口与线程提交前窗口、借用 executor 的运行/排队 future、线程启动/提交失败、非法输入、非法关闭参数、并发/重复关闭及重试、部分初始化、Service.run/Service.close 与真实 ProcessLock 竞争。
- 服务集成用例在未排空时明确验证竞争者无法获得同一进程锁；释放工作后重试关闭，竞争者才能获得锁。所有测试线程/事件都有 watchdog 和 finally 清理，不制造永久后台阻塞。

## 4. 差异审计

| 检查项 | 结论与边界 |
| --- | --- |
| 是否放弃 unknown 或伪造未发送 | 否；原 hard timeout 仍记 unknown，迟到回调不结算成功 |
| 是否把不等同于已排空 | 否；active caller / worker / future 任一存在即未排空 |
| 是否引入默认线程池无法退出的问题 | 异步 drain 没有 to_thread/run_in_executor；不接管借用 executor |
| 是否过早释放所有权 | 支持的同步/异步服务退出路径在 embedding 未排空时显式失败并保留锁 |
| 是否影响预算/模型请求期限 | 不修改；1 秒是 embedding 收尾期限，不是模型请求总超时 |
| 是否提前报告完成导致重试跳过 | complete 仅全部清理成功才设置；并发失败后仍检查 drain |
| 是否引入身份/Windows 协议变更 | 否；R01/R03 本次仅文档设计 |
| 是否持久格式/依赖变化 | 无 schema、账本格式、签名协议或依赖变化 |

保留行为：正常排空后关闭成功；没有 embedding 时按原组件顺序关闭；已接入但尚未提交的调用不会被误当作已排空；仅拒绝关闭之后的新调用。关闭后不支持复用同一 gateway，需新实例，符合终止生命周期语义。

## 5. 边界与运行处置

- 无限卡住的 Python 线程不能安全强杀。超时后服务显式报未完成并保留锁，是 fail-closed，不是悄悄强行成功；线程退出后重试 cleanup。若进程终止，由 OS 回收锁。
- 不能把本修复解释为所有客户端关闭或整个服务停机都保证 1 秒；其他子系统仍有各自生命周期。借用 executor 的解释器退出行为由 caller 负责。
- 低层 SubjectKernel.close 是直接所有权接口；不应绕过 service 的未排空错误后再手工释放 lock。自定义 provider 不得绕过 Database.transaction/lease 直接修改 SQLite 或创建不受控写线程。
- 原生 Windows 长路径限制为已证实的范围外既有问题。M41 在仓库外短 TEMP_ROOT 运行相同 gate/profile/断言，不修改注册表或掩盖失败。
- 回退前必须先停止旧工作并排空；没有数据库回滚步骤。回退代码会恢复原无限等待风险，不能当作修复。当前尚无生产部署。

## 6. 固定源代码验证

源代码与测试/部署/脚本共 624 个跟踪/新增文件建立 SHA-256 快照；在回归运行期间不修改这些文件。导入路径确认来自本独立工作树的 src，不是原候选工作区。

### 已完成结果

| 检查 | 实际结果 |
| --- | --- |
| R05 专项 | 22 passed |
| R05 + embedding resilience/resources + cognition/reflection + gate0 + 完整 service/sleep + migration runtime/fencing + 上轮 R04/R06/Windows 回归 | 335 passed、1 skipped、46 subtests passed；1255.45 秒 |
| M41 targeted / smoke，同一脚本/用例/断言，仅短临时根 | 291 passed、7 subtests passed；1041.09 秒 |
| 全仓库 Ruff check / format | 通过；770 个格式检查文件 |
| Mypy src/tests | 通过；473 个文件 |
| compileall / pip check / git diff --check | 通过 |
| 冻结源代码复核 | 624 个文件 SHA-256 全部一致；回归过程中未修改 |

唯一跳过项是 Windows 不适用的 POSIX token-file 权限检查，不计为通过。多轮测试存在交集，不能简单相加为独立用例数量。原合并候选工作区仍干净且 HEAD 为 ac17c5d；先前三项修复分支仍指向 8a629d4。未查询/轮询 GitHub、未推送或部署。

## 7. 审计结论与建议

在本次已检查的变更和实际验证边界内，R05 原反例已解决，未确认新增 P0/P1 或新的阻断项。建议保留该修复；本轮模块修复质量评价为高（局部根因闭环、失败显式、所有权实测、全调用窗口与既有回归覆盖），不等于已证明全项目零风险或已通过实机门禁。

R01/R03 仍未修复。建议下一步先对 R01 做历史证据分布盘点与旧库兼容/迁移夹具验证，满足设计门槛后单独实施；不要立刻并行改 Windows signer 恢复协议。R03 先确认信任撤销生效时点和单一权威提交格式，再做原生 Windows 崩溃恢复原型。暂不自动继续下一项，等待用户确认。


证据目录：D:/metagrowh/.user-temp/noyra-r05-20261025/。最终报告应明确本地验证不等于 GitHub 全 OS/Python 矩阵，也不替代供应商计费、链上交易、Linux root/systemd 或长期运行实机门禁。
