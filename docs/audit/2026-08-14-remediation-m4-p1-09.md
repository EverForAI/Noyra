# Noyra P1-09 修复复核

日期：2026-08-14  
范围：自主项目只有预计时长校验，没有实际 elapsed duration deadline。

## 修复边界

项目的硬截止时间由不可变的 `created_at` 加 `estimated_duration_hours` 计算。
每次项目调度和阶段选择前都会检查非终态项目；到达截止时间后，项目通过本地
状态转换进入 `abandoned`，写入 `autonomous_project_bounded` 事件，原因码为
`elapsed_deadline_reached`。终态不能重新激活，因此停机、重启、微小进展和重复
review 都不能绕过生命周期上限。

本阶段不支持自动延长，也不修改旧项目的创立时间或预算。未来若需要延期，必须
另建带最大总生命周期、睡眠证据和独立审计记录的延期协议，而不能复用本地状态转换。

## 验证

* 新增截止时间到达后不再调用模型且项目进入 abandoned 的回归测试；
* 项目专项测试、Ruff、Mypy、全量测试和 Ubuntu/部署专项审计通过。
