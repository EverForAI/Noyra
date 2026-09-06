# Noyra P2-04 修复复核

日期：2026-08-14  
范围：running export 取消无效，shutdown 等待运行中的导出。

## 修复内容

取消现在覆盖 queued 和 running 状态，并写入 `cancel_requested`。worker 完成后会在
发布 artifact 前重新读取 durable 状态；若任务已取消，临时 ZIP 被删除，绝不会将
半成品写成 completed。异常收尾同样只更新仍为 running 的任务，避免覆盖 cancelled。

服务关闭会把未完成任务标记为 `cancelled/service_shutdown`，取消待运行 futures，
不再无限等待导出线程；正在执行的导出会在自己的收尾检查中清理临时文件。

## 已知边界

第三方压缩或 SQLite 读取内部无法被强制异步打断；本阶段保证状态、artifact 发布和
重启恢复正确，后续可为导出迭代器增加 cooperative cancellation 检查。

## 验证

* 新增 running export cancellation 回归测试；
* 全量测试、静态检查和部署专项审计在提交前执行。
