# Noyra M5 记忆检索扩展性复核

日期：2026-08-14  
范围：记忆 recall/consolidation 的固定 2,000 条截断与因果事件全表扫描。

## 修复内容

`MemoryStore.recall()` 现在先从 FTS、语义相似度、文本 LIKE 和近期高显著性记忆
形成最多 768 个候选 ID，再对候选执行实体、因果、访问、时间和混合相关性评分。
最终 SQL 只读取候选记忆及其访问聚合，不再用固定 2,000 条全量候选作为检索边界。

因果评分只读取候选记忆当前 revision 的来源事件，并按 400 个 ID 分块加载来源及一层
父事件；不会扫描主体全部事件。原有 causal 权重、哈希验证和访问记录保持不变。

`MemoryConsolidator` 去掉静默的 `LIMIT 2000`，通过当前 revision 联接一次性取得来源
事件列表，避免每条记忆再发起 revision 查询。归档、强化、重复合并的状态机未改变。

## 已知边界

语义 embedding provider 仍可能扫描其自身索引来计算相似度；后续可将其替换为有界
向量索引。候选集是有界的，SQLite 参数数量和工作内存不会随主体历史无限增长。

## 验证

* memory lifecycle/integration/semantic 专项测试通过；
* 全量测试 286 项通过；
* Ruff、Mypy、compileall 和 Ubuntu/部署专项审计通过。
