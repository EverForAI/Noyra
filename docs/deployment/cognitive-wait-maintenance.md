# 认知等待生命周期与历史收口

## 不可混淆的三件事

1. `waiting_cognitive_tasks` 表示业务用途的等待状态，不是供应商执行队列或账单。
2. 睡眠周期 `complete` 可以来自降级反思；不能由此认定模型成功。
3. 取消等待不会改变 `model_calls` / `model_attempts` 的 unknown，不结算或释放预算，
   不证明未计费，不授权重试，也不唤醒主体。

## 永久收口

- `ObservationStore.mark()` 把观察标为 analyzed/rejected 时，在同一事务中取消对应
  `world_cognition:<observation_id>` 的 waiting。
- `SleepEngine.commit_reflection()` 接受反思计划（包括有界降级计划）后，反思子任务已经结束，
  同一事务取消对应等待；这不表示整个睡眠周期已经结束。
- 睡眠转到 complete/failed 时也执行收口，覆盖旧版本留下的记录和失败路径。
- 登记资源不可用时重新检查父业务；即使晚到的失败发生在业务结束之后，也不新建或复活等待。
- 取消保留 task、retry_count、next_retry_at 和历史；更新当前状态哈希并追加 cancelled 修订。
  使用 parent_sleep_complete / parent_sleep_failed / sleep_reflection_committed /
  parent_observation_analyzed / parent_observation_rejected，不冒用 resource_recovered。
- 终态更新与等待修订任一失败都回滚；不覆盖不一致证据。单条等待超过 1,000 条修订时拒绝自动收口，
  需要专门核验，不静默截断历史后写入。

## 观测口径

详细认知池状态：

- pending_wait_count：waiting 中未发现父业务终结证据的记录；包含尚需人工核对的父业务缺失项。
- recorded_wait_count：数据库原始 waiting 数量。
- terminal_parent_wait_count：仍为 waiting，但有明确业务/反思子任务终结证据的历史残留。
- missing_parent_wait_count：识别到睡眠/观察用途，但找不到同主体父记录；不自动取消。
- cancelled_wait_count：保留在库的历史取消记录。

这些字段不参与模型授权或未知结果隔离。轻量总览提供另一份最多 100 条的 waiting/cancelled
创建顺序样本，不是全历史总量；标注截断。未知调用提醒继续独立保留。

## 历史工具：默认只读

工具：`python -m noyra.cognitive_wait_maintenance`，使用当前发布的 Python 与依赖。
本次开发不自动对生产执行。实际执行前需要确认版本、备份可恢复、服务切换结果及本批预览。

只读预览示例（SUBJECT_ID 必须替换为已核对的主体，不自动猜选）：

```sh
python -m noyra.cognitive_wait_maintenance \
  --database /var/lib/noyra/noyra.sqlite3 \
  --subject-id SUBJECT_ID --limit 50
```

- mode=ro + query_only + BEGIN 一致性快照，包含 WAL；不使用 immutable、不创建数据库、不迁移。
- 每批最多扫描 100 条 waiting，默认 100；可减小到 1。
- SQL 工作预算每阶段 2 秒、锁等待 200ms；不是操作系统 IO 的硬实时承诺。
- 只为明确终结的睡眠/观察用途生成候选；核验等待哈希/修订序列及父状态证据。
- 返回候选、标准取消原因、plan_hash、next_cursor；不读取密钥、模型响应或归档内容文件。

**应用需要另行明确确认，不能只复制本页命令就执行：**

```sh
python -m noyra.cognitive_wait_maintenance \
  --database /var/lib/noyra/noyra.sqlite3 \
  --subject-id SUBJECT_ID --limit 50 \
  --apply --expect-plan PREVIEW_PLAN_HASH --reason '已核对本批终态等待与备份'
```

应用阶段不初始化数据库，严格校验既有 schema；使用 BEGIN IMMEDIATE 串行化写入，
重新计算同批计划。任何候选状态变化、哈希不符、完整性问题或查询超限都会拒绝并回滚整批。
仅修改已预览的 task_id：同一 purpose 在另一池、另一页的记录不会被顺带修改。
同事务追加私有 operator 审计事件，记录计划哈希、理由和 task_id。

同一旧计划应用后会被判为过期，不会追加第二次取消修订；重新预览没有候选时应用是空操作。
后续页必须把预览返回的 next_cursor 同时用于下一页的预览和应用：`--after-task-id CURSOR`。
计划绑定数据库绝对路径、主体、分页参数与本批状态；不可跨机器/路径复用。
分页期间更早的有效任务可能后来终结；完成分页后可从空游标重新预览，不做无界循环。

退出码 2 表示本批未提交；stderr 为脱敏错误码，区分预览过期、参数、完整性、schema 与数据库不可用。
不要为了成功而删除审计触发器、跳过哈希核验、扩大自动重试或改写 unknown。
