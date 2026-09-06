# Noyra M3 存储生命周期与归档边界修复审计

日期：2026-08-14  
范围：P1-05、P1-06，以及 P2-01、P2-02、P2-03、P2-15 的可安全修复部分。  
状态：M3a 已通过质量门；P1-02 的观察内容和全量热数据分段迁移仍是后续 M3b 阻断项。

## 已完成修复

### 存储计量和导出 artifact

`StorageUsageScanner` 现在将 `exports/` 和 `secrets/` 纳入 subject quota；symlink、权限异常、并发文件消失不会直接让 scanner 抛出维护异常。`StorageLifecycleManager` 在 subject quota 告警时按完成时间清理旧 export artifact，并把数据库记录标记为 `artifact_pruned`。`ExportJobManager` 增加单 artifact 1 GB 上限，避免单个导出无限占满磁盘。

### 归档密钥元数据

事件冷段新增 archive format、key ID 和 key fingerprint。密钥本身仍不进入数据库；读取时会检查当前 key 与 segment metadata 是否匹配。缺少密钥、密钥错误或格式不支持现在统一表现为 `IntegrityError`，不会伪装成普通 JSON 读取错误。旧 segment 的空 metadata 保持兼容，但只能依赖已有外部 key 读取。

### 云端事件冷段复制

`CloudArchiveCoordinator` 除 snapshot 外，现在可以把本地加密的 event payload segment 作为 opaque encrypted bytes 加入 cloud transfer queue。云端校验的是加密对象的 hash，服务不会把事件明文交给云 provider；本地 segment 暂不因上传成功而删除，恢复路径仍需后续 M3b 完善。

### 搜索资源和浏览器 quota

维护 tick 增加 search provider uses 和 browser reservations 的可配置默认 365 天 retention，防止高频计数表永久增长。浏览器搜索现在先准备 action，再写 reservation；目标/项目校验失败不会消耗 quota，quota 不足会把 prepared action 显式取消。

## 回归证据

新增测试覆盖：

* subject quota 统计 exports/secrets；
* 旧 export artifact 在 quota 告警时被安全清理；
* 冷事件 segment 保存 format/key metadata；
* 错误 key、缺失 key 会被完整性错误捕获；
* cloud coordinator 上传本地加密 event segment；
* browser action prepare 失败不产生 reservation；
* schema 从低版本重复迁移时不会因 optional column 重复而失败。

当前验证结果：

| 检查 | 结果 |
|---|---|
| 全量 pytest | 280 passed |
| Ruff check / format | 通过 |
| Mypy | 通过 |
| Compileall | 通过 |
| Pip audit | 未发现已知漏洞 |
| 部署专项审计 | 32 passed；service coverage 69.92% |

## 尚未完成的 M3b

以下问题不能因为本阶段通过就标记为已解决：

1. `observations.content`、model response、behavior logs、interactions 和 training provenance 仍可能持续增长；它们需要可恢复的 segment 迁移和 materialize-on-read 设计。
2. event segment 上传到云端后仍保留本地副本，尚未实现 cloud verified 后的 tombstone、恢复和 quota 释放。
3. search/browser 旧明细目前按 retention 删除，没有完整的日聚合 archive；若未来需要长期训练 provenance，应先落聚合再删除明细。
4. revoked secret 删除失败仍缺少统一 repair queue。

M3a 的修改不改变主体事件链、训练 consent 或 action revision 语义。进入 M3b 前必须先设计 observation/model payload 的统一 segment manifest、恢复演练和 quota 释放顺序，禁止直接把 SQLite 大表做不可逆 DELETE。
