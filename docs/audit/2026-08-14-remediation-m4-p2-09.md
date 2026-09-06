# Noyra P2-09 修复复核

日期：2026-08-14  
范围：自我修改根据错误的元认知策略调整设置。

## 修复内容

`ControlledSelfModification` 使用显式 `STRATEGY_TO_SETTING` 映射，并在初始化时
校验设置键存在且没有重复目标。思考策略现在只映射到
`max_thought_no_change_streak`，研究策略映射到 `research_interval_seconds`；目标
复查不再错误修改思考阈值。

未建立专门的目标复查自我修改参数，因此 `goal_review` 不会被自动映射到其他设置。
这保持了自我修改的最小权限边界，避免为了覆盖率而引入新的行为漂移。

## 验证

* 新增映射契约测试；
* Ruff、Mypy、专项测试和全量部署审计在提交前执行。
