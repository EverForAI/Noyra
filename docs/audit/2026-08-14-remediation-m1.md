# Noyra M1 数据契约与完整性修复审计

日期：2026-08-14  
基线：`559ec69`  
类型：修复后只读复核  
范围：schema marker、事件链顺序、因果时间边界、ActionLedger/behavior log 完整性、核心 action 主体归属。

## 修复内容

### 1. 正式化 schema version 29

此前 migration 28 由 `_ensure_optional_features()` 隐式执行，但 `schema_meta` 仍报告 27。现已：

* 将 `CURRENT_SCHEMA_VERSION` 提升为 29；
* migration 28 作为正式迁移执行；
* 新增正式 migration 29；
* `_ensure_optional_features()` 不再偷偷推进 schema 版本；
* 启动时检查必需表和虚拟表是否存在；
* runtime/training export 读取同一版本常量。

旧数据库仍采用 additive migration；不会删除旧表或重排主体历史。

### 2. 事件链改为按追加序列校验

`event_chain_roots.sequence_number` 现在是校验顺序的唯一依据。`verify_chain()` 通过 chain roots join events 并按 sequence 校验，不再按业务 `occurred_at` 排序。

对于没有旧 chain roots 的历史数据库，首次建立根时使用 SQLite 插入 rowid 作为一次性 bootstrap 顺序；之后链根为 append-only。业务时间仍可用于查询和展示，不再影响链完整性验证。

### 3. 因果时间保护

实时追加且未显式指定业务时间的事件，如果其因果父事件位于未来，会被拒绝。显式历史导入仍允许时间倒置，因为回放/补录事件可能是在事后记录过去发生的事实；这避免把合法导入和运行时钟错误混为一谈。

### 4. ActionLedger revision 和主体边界

新增 `action_revisions` append-only 表。prepare、start、finish、cancel、recover、reconcile 每次状态转换都写 revision，revision 保存状态 hash、序号、原因和时间。启动时会为旧 action 建立一次性 `legacy baseline`，不改变旧 action 状态。

新增 `ActionLedger.verify_integrity()`，检查：

* action JSON、状态和 revision 序列；
* 最新 revision hash 是否等于当前 action；
* terminal action 是否有且只有一个 behavior log；
* behavior log 的 action、subject 和 result status 是否一致。

该检查已经接入 `LongRunResilience._domain_checks()`。

数据库触发器同时阻止：

* action 的 goal/project/phase 跨主体或跨 project 关联；
* action revision 修改或删除；
* behavior log 与 action 状态、主体不一致。

## 回归证据

新增 `tests/test_integrity_hardening.py`，覆盖：

* schema marker 与已安装功能一致；
* 迟到事件不会破坏事件链；
* 实时事件不能引用未来因果父事件；
* action revision 可以发现状态篡改；
* SQLite 触发器拒绝跨主体 action goal。

复核结果：

| 检查 | 结果 |
|---|---|
| M1 专项测试 | 5 passed |
| 全量测试 | 272 passed |
| Ruff check | 通过 |
| Ruff format check | 通过 |
| Mypy | 通过 |
| Compileall | 通过 |
| Pip check | 通过 |
| Pip audit | 未发现已知漏洞 |
| 部署专项审计 | 31 passed；service coverage 69.96% |

## 修复边界与残余风险

本阶段只完成核心 action 关系和完整性契约，未声称所有跨 subject 外键已经复合化。interaction delivery、model resource、project execution 等其他域仍需在后续阶段分别增加主体约束和 integrity check。

因果时间保护对显式历史导入保持兼容，因此历史导入工具必须在导入 manifest 中标记 `historical_replay`，并由导入审计记录来源和操作者。未来可增加独立的 causal anomaly report，而不能静默把导入时间当作实时发生时间。

Action revision 的 legacy baseline 只证明“当前状态从该版本开始受保护”，不能重建旧版本的完整转换历史；这是保持旧主体连续性和避免伪造历史之间的明确边界。

## 进入下一阶段的条件

M1 已通过当前自动化质量门，可以进入 M2 训练同意与导出修复。M2 不应修改事件链或 action revision 语义；训练策略迁移必须使用独立 migration、分页游标和导出快照，并继续运行本阶段的完整性测试。
