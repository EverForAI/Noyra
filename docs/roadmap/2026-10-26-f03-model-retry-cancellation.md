# F03 模型重试等待取消修复计划（2026-10-26）

基线：`b76a12c3cc11e9bfbed00dfa924aaa0b7a2a467a`；分支：`codex/fix-f03-model-retry-cancellation`。本轮只修 F03，不处理暂缓的 F02 或 F04，不扩大 F01 的变更。

## 根因与状态设计

ModelGateway 在供应商请求 await 内处理取消，但已知可重试失败及结构化输出无效后的两个 retry delay 位于该 try/except 外。取消落在这些等待中时，上一尝试已结算而调用仍 executing，重启后可能被保守归为 unknown。

修复采用共享的“已结算尝试后的等待”入口：仅包住两个退避等待；捕获 CancelledError 后调用现有 finish_call 记 failed / model_retry_cancelled，写 completed_at，然后原样传播取消。所有已有 attempt、token、费用、未知用量保留和 provider request ID 不改写。

| 阶段 | 取消后的调用 | 计费/副作用边界 |
|---|---|---|
| 已知失败后等待下一次尝试 | failed / model_retry_cancelled | 失败尝试原记录保留；未知用量仍计入保留 |
| 已收到无效结构化响应后等待重试 | failed / model_retry_cancelled | 已返回响应和费用保留；不伪造成功输出 |
| 请求执行中 | 保持 unknown / model_call_cancelled | 保留可能已计费请求，不自动重放 |
| 供应商结果未知 | 保持原有 unknown 与人工授权重试约束 | 不因修 F03 释放未知保留或绕过路由隔离 |

failed 表示这次结构化调用没有可交付结果且取消发生在已知静止阶段，不表示没有付费或 token 使用。状态使用现有枚举，不增加 cancelled schema 状态。

## 模块一：最小实现与直接回归

1. 加两分支可控等待复现，先确认修复前失败原因确为 executing 未终结。
2. 实现共享等待收尾，保留原来的重试次数/随机退避、请求中取消、授权及已知/未知结算逻辑。
3. 验证真实 asyncio task.cancel、重复取消、预算计数和已付费响应、未知用量保留、同键禁止自动重放、重启不重分类、未取消正常重试。
4. Ruff/Mypy/针对性测试通过并复审后提交；同步发布清单与哈希。

## 模块二：路由与运行屏障集成

1. 在 RoutedModelGateway 的真实资源选择路径取消退避，验证只发送一次，无备用 key/group 调用；退出后逻辑锁释放，原有费用保留且调用不进入 unknown。
2. 结合 runtime lease 失效和取消，确认 accounting_scope 可收尾但没有新增网络或普通主体写入。
3. 回归真正请求中取消、已知/未知失败、显式 unknown retry、资源撤销及 epoch 屏障；不把原型/模拟测试等同于实机供应商验证。
4. 如发现为完成 F03 必须处理的集成缺口，先限定根因再修改，不顺手重写路由恢复协议。复审并提交。

## 模块三：最终复审与交付

相关 gateway/authorization/wire/dispatch/运行所有权回归，全库 Ruff/format、严格 Mypy。更新复审记录与清单；提交后独立核对 Git 原始哈希及实际源码 ZIP。保持工作区干净；不推送/合并/部署。

## 安全边界与剩余情况

- 不修改 ledger schema、全局启动恢复规则、已持久化历史记录或预算算法；不扫描历史 unknown 并批量降级。
- 收尾用现有 accounting transaction，期间不 await，重复取消不应打断已开始的同步事务。数据库写入错误仍显式抛出，不能吞掉后声称成功。
- 突然进程死亡/断电仍由现有保守启动恢复处理；本次解决协作式取消，不宣称修复所有崩溃窗口。
- 已有未知调用获人工授权后再次尝试的历史未知 attempt 必须仍保留；取消这次已知静止重试不能消费/伪造人工对账证据。
- 无效结构化响应内容不能在失败调用上变成可缓存成功输出；同物理幂等键仍拒绝再次发送。

修复风险：中。通过缩小生产变更、定向回归、真实 ledger/资源/lease 集成验证控制风险。

## 模块一复审

- 修复前两个真实 task.cancel 复现均失败于 call.status 仍为 executing；修复后同用例通过。
- 生产变更仅限 gateway 两处调用改为共享 `_retry_after_settlement`；该 helper 仅捕获退避中的 CancelledError，用现有 finish_call 事务终结后原样传播取消。无 ledger/schema/预算算法变更。
- 新增 13 项定向回归通过：两条分支、未知用量/估计用量、单次与重复取消、第二次退避累计费用、真正请求中取消、同键拒绝重放、恢复不重分类、正常重试/缓存、写入失败可见。
- Ruff/格式及 gateway/新测试的严格 Mypy 检查通过。复审确认没有额外 await 的收尾窗口，不捕获并隐藏数据库结算失败。
- 路由、lease 与人工未知重试的集成由模块二验证；本模块不宣称已验证实际供应商或所有崩溃情形。
