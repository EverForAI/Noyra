# Noyra P2-13 修复复核

日期：2026-08-14  
范围：多表关联只依赖单列 foreign key，跨 subject 关系主要靠代码检查。

## 修复内容

数据库初始化现在幂等安装 subject-scoped triggers，覆盖元认知、模型调用、研究、
记忆、关系、项目、通讯投递、自我修改、embedding 和实体关系等高风险关联。每个
触发器同时约束 insert 与 subject/reference update，要求父对象存在且拥有同一
`subject_id`；可空引用保留可空语义。

这些约束是代码层 ownership check 的底线，不改变合法历史数据，也不把跨主体的共同
知识 publisher/import 关系错误限制为同一主体。

## 验证

* 新增跨主体元认知 outcome 必须被 SQLite 拒绝的测试；
* 现有 288 项全量测试、Ruff、Mypy、compileall、依赖和部署专项审计通过。
