# Noyra M3b 观察内容冷归档修复审计

日期：2026-08-14  
范围：长期存储增长中的 observations.content、运行导出恢复、世界完整性检查和云端 opaque segment 复制。

## 修复内容

新增 `ObservationContentArchive` 和 schema version 32：

* 90 天以上（由维护配置控制）的观察正文可以分批压缩、加密并写入 `subject/cold/observations/`；
* SQLite 只保留观察元数据、content hash、archive key 和 archived_at；
* `ObservationStore.get()`、`mark()`、`WorldIntegrity` 和 runtime export 在读取时 materialize 正文；
* 归档段保存 format、key ID、key fingerprint 和 compressed hash；
* 错误/缺失密钥会以完整性错误失败，不返回空正文；
* CloudArchiveCoordinator 可以把本地加密观察段复制到 cloud transfer queue；
* 本地正文不会在云上传后立即删除，恢复和 tombstone 仍保持可回滚边界。

## 关键安全边界

* observation `record_hash` 仍基于正文 hash，不因正文搬离 SQLite 而改变主体证据；
* `content=''` 只允许与非空 `content_archive_key` 同时出现，读取路径必须 materialize；
* 归档对象以 opaque encrypted bytes 上传，不把观察正文交给 cloud provider；
* 旧数据库通过 idempotent optional feature 安装列和 segment 表，不重写历史 observation；
* 导出失败或 key 缺失不会删除主体 metadata。

## 验证结果

新增世界测试覆盖正文归档、读取 materialization、quota maintenance 和 content hash。当前全量验证：

* `281 passed`
* Ruff、Mypy、compileall 通过
* `pip-audit -r requirements.lock` 未发现已知漏洞
* 部署专项 `32 passed`，service coverage `69.92%`

## 剩余 M3b 工作

观察正文已进入可恢复冷段，但 model response/request、behavior logs、interactions 和训练 provenance 仍未统一迁移。云端上传目前不释放本地空间；下一步必须实现统一 segment manifest、恢复下载、校验后 tombstone 和 quota 释放，不能直接删除热数据。
