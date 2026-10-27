# 第一实例运行问题修复与模块复审

日期：2026-10-27。基线 444b5f6。分支 codex/overnight-health-remediation。

## 生产现场保护

已使用原发布版本内置 EncryptedBackupManager 冷备份并恢复到隔离目录验证，未启动恢复副本。归档：/var/backups/noyra/noyra-pre-health-repair-20261027T021630Z.noyra-backup，90,811,170 字节，SHA-256 490eb599d27f787ee2b210cc1f82ba9f693542fb0b80ab11f73d69a9939c3908。验证时间 2026-10-27T02:21:58Z，单主体、992 条模型调用、27,058 个事件，SQLite quick_check 通过。冷备份短暂停服后已自动恢复，PID 151192。原发布包没有预期的 deployment-maintenance.py，首次验证入口不可用；后续直接通过同版本备份管理器完成恢复验证，不以归档存在冒充验证通过。

## 模块一：撤销历史与秘密清理意图的完整性契约

### 根因、影响与修复边界

启动清理在保留 revoked 资源历史行的同时，把其 create 意图标为 removed；原校验不区分活动资源与已撤销资源，拒绝任何关联资源行的 removed/create。现场持续告警由该状态冲突触发，不是 142 个独立物理损坏。本轮不删除资源历史，不改哈希，不清空守护器状态，不修改 SQLite 迁移。

仅当同一主体、资源类型、资源 ID、秘密引用的 delete 意图已为 removed，且保留资源已 revoked 时，允许 removed/create 与资源历史同时存在。此前的主体边界、引用绑定和指纹验证保持执行；活动/disabled 资源、删除证据缺失或不匹配仍不通过。无资源行的已移除创建意图保持原语义。

### 验证与模块复审

在修复前，真实 cognitive/search/embedding/transport 四域的配置→撤销→启动清理均复现同一告警；修正后四域通过。活动资源错误标 removed 和缺少完成删除证据的反向用例均保持 corrupt。缺失证据探针仅在临时测试库明确移除 append-only 删除触发器后构造，不改变生产触发器。

相关秘密意图、清理、管理完整性和完整性加固回归：118 passed、2 subtests passed；Ruff 通过。逐行复审确认新增接受分支要求已撤销状态和精确完成删除证据，不允许通过删除历史行或跳过原校验恢复。产品改动仅在 _check_secret_file_intents 的一个条件分支，无新增写入或状态变更。后续集成静态检查另记。

原始证据保存在仓库外 D:/metagrowh/.user-temp/noyra-overnight-remediation-20261027-wepi327e/。
