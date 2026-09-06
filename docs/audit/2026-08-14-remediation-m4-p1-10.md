# Noyra P1-10 修复复核

日期：2026-08-14  
范围：元认知决策在 workflow 异常后无法完成而永久阻塞认知循环。

## 修复边界

`MetacognitiveControl.run_due()` 在返回未完成决策前检查其实际存续时间。超过
`metacognitive_pending_timeout_seconds` 的决策不会被假装为成功或失败，而是写入一次
不可变的 `unknown` outcome，来源为该决策自身，原因码为
`workflow_timeout_quarantined`。随后选择器可以继续选择新的策略。

默认超时为 1,800 秒，可通过 `NOYRA_METACOGNITIVE_PENDING_TIMEOUT_SECONDS` 调整，
范围限制为 60 秒至 7 天。未超时的 pending decision 仍保持原有恢复语义，不会被重复选择。

## 风险控制

* outcome 使用现有 append-only 表、哈希和策略画像更新路径；
* `unknown` 不增加 productive/failed/stagnant 计数，避免把工作流异常误学成能力或失败；
* 超时阈值只释放选择器，不会自动重试外部行动或模型调用；
* 时钟必须带时区，异常时间格式仍会显式失败。

## 验证

* 新增 stale decision quarantine 回归测试；
* 元认知专项测试、Ruff、Mypy 通过；
* 提交前继续执行全量测试、依赖审计和 Ubuntu/部署专项审计。
