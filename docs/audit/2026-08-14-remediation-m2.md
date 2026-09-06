# Noyra M2 训练同意与导出修复审计

日期：2026-08-14  
范围：P1-01、P1-03、P1-04，以及 P2-05 的训练策略审计记录。  
前置阶段：M1 数据库 schema/事件/action 完整性修复。

## 修复内容

### 1. 运行时训练同意不再依赖启动快照

模型 gateway 新增 live policy getter 和 `enforce_training_policy` 模式。服务创建的 economy/deep/legacy gateway 都启用该模式：

* 每次 model call prepare 都在同一个 SQLite transaction 内重新读取 `training_policies`；
* 未同意时即使 gateway 传入已脱敏请求，也不会写入 `request_json`；
* 同意开启时自动保存脱敏请求；
* 记录 `capture_policy_version`，训练导出可以追溯使用的策略版本；
* policy 查询异常时 gateway getter fail closed；
* 运行时撤回 `include_model_io` 后，新调用不再捕获输入。

保留了非服务、未启用 policy enforcement 的 ModelGateway 兼容行为，避免破坏测试和外部嵌入调用者；服务主路径使用策略托管模式。

### 2. 训练策略批处理不再受单次 100,000 行限制

`TrainingStore` 新增：

* `backfill_all()`：以小批次重复处理所有缺失 provenance；
* `reclassify_all()`：按 `(created_at, record_id)` 稳定游标分页；
* `update_policy()` 现在会完整 drain backfill/reclassify，而不是只处理第一批；
* 训练导出使用 `backfill_all()` 和当前 consent version 快照；
* 每次策略修改写入 `audit_records`，包含 actor、reason、前后 policy version 和变更字段。

### 3. 路径导出改为真正的磁盘流式流程

`export_to_path()` 和兼容的 `export()` 现在通过临时目录生成：

* events、episodes、trajectories、retrieval、goal、sleep、preference、label 和 model IO 均使用增量 JSONL writer；
* writer 只保留文件 hash、字节数和计数，不把所有行聚合在内存；
* workspace 文件逐个清洗并写入临时目录，不再把整个 workspace 聚合为 bytes dict；
* ZIP 从文件路径按 1 MiB 分块写入，目标文件原子替换；
* manifest、文件 hash、quality 和 consent version 在生成后写入；
* 兼容 `export()` 只在最终 API 返回 bytes 时读取完整 ZIP，服务后台路径不会持有完整 ZIP。

旧的 `_collect_data()`/`_build_files()` 保留为内部兼容代码，但服务和公开导出入口已经走流式路径；`max_rows` 不再限制路径导出总行数。

## 回归证据

新增/更新测试覆盖：

* live getter 在关闭、开启、撤回三种状态下控制 model IO capture；
* ledger 在 prepare transaction 内重新检查当前 training policy；
* `capture_policy_version` 与 policy version 对齐；
* 小批次 reclassification 会处理全部记录；
* path export 可以超过旧的 `max_rows` 值并保持 manifest/JSONL 行数一致；
* service helper 可以观察运行时同意策略变化。

当前验证结果：

| 检查 | 结果 |
|---|---|
| 全量 pytest | 277 passed |
| Ruff check / format | 通过 |
| Mypy | 通过 |
| Compileall | 通过 |
| Pip check | 通过 |
| Pip audit | 未发现已知漏洞 |
| 部署专项审计 | 32 passed；service coverage 69.92%（门槛 69.9%） |

## 安全边界与残余风险

* 已经写入数据库的历史 `request_json` 不会因为撤回同意而自动删除；撤回只阻止新的捕获，历史数据处理仍由现有训练政策和导出规则控制。
* 兼容 `export()` API 为了返回 `bytes`，最终仍然需要占用与 ZIP 大小接近的内存；无人值守服务必须使用 `export_to_path()`/后台 export job。
* 流式导出仍在临时磁盘上生成完整 ZIP，临时空间必须纳入后续 M3 quota；磁盘不足时应进入可恢复失败，而不是删除主体数据。
* 去重 fingerprint 集合随导出事件数增长，但不再保留完整 event payload、derived rows 或 ZIP bytes；后续可将 fingerprint set 下沉到临时 SQLite 进一步压低内存。

M2 已通过当前质量门，可进入 M3 存储生命周期修复。M3 不应改变训练同意语义；冷归档、exports quota 和 archive key manifest 必须保留本阶段的 `capture_policy_version` 与导出恢复证据。
