# 第一实例运行问题修复与模块复审

日期：2026-10-27。基线 444b5f6。分支 codex/overnight-health-remediation。

## 生产现场保护

已使用原发布版本内置 EncryptedBackupManager 冷备份并恢复到隔离目录验证，未启动恢复副本。归档：/var/backups/noyra/noyra-pre-health-repair-20261027T021630Z.noyra-backup，90,811,170 字节，SHA-256 490eb599d27f787ee2b210cc1f82ba9f693542fb0b80ab11f73d69a9939c3908。验证时间 2026-10-27T02:21:58Z，单主体、992 条模型调用、27,058 个事件，SQLite quick_check 通过。冷备份短暂停服后已自动恢复，PID 151192。原发布包没有预期的 deployment-maintenance.py，首次验证入口不可用；后续直接通过同版本备份管理器完成恢复验证，不以归档存在冒充验证通过。验证现场没有执行生产数据写入。

## 模块一：撤销历史与秘密清理意图的完整性契约

### 根因、影响与修复边界

启动清理在保留 revoked 资源历史行的同时，把其 create 意图标为 removed；原校验不区分活动资源与已撤销资源，拒绝任何关联资源行的 removed/create。现场持续告警由该状态冲突触发，不是 142 个独立物理损坏。本轮不删除资源历史，不改哈希，不清空守护器状态，不修改 SQLite 迁移。

仅当同一主体、资源类型、资源 ID、秘密引用的 delete 意图已为 removed，且保留资源已 revoked 时，允许 removed/create 与资源历史同时存在。此前的主体边界、引用绑定和指纹验证保持执行；活动/disabled 资源、删除证据缺失或不匹配仍不通过。无资源行的已移除创建意图保持原语义。

### 验证与模块复审

在修复前，真实 cognitive/search/embedding/transport 四域的配置→撤销→启动清理均复现同一告警；修正后四域通过。活动资源错误标 removed 和缺少完成删除证据的反向用例均保持 corrupt。缺失证据探针仅在临时测试库明确移除 append-only 删除触发器后构造，不改变生产触发器。

相关秘密意图、清理、管理完整性和完整性加固回归：118 passed、2 subtests passed；Ruff 通过。逐行复审确认新增接受分支要求已撤销状态和精确完成删除证据，不允许通过删除历史行或跳过原校验恢复。产品改动仅在 _check_secret_file_intents 的一个条件分支，无新增写入或状态变更。后续集成静态检查另记。

原始证据保存在仓库外 D:/metagrowh/.user-temp/noyra-overnight-remediation-20261027-wepi327e/。


## 模块二：只读沙箱中的 WAL 观测

### 根因与实现

在生产服务器新建私有 mount namespace，把数据目录只对探针重挂只读，复现 WAL/SHM 均不存在时 mode=ro 连接返回 SQLITE_CANTOPEN；退出 namespace 后生产挂载仍为 rw。没有修改主机挂载、采样器沙箱或数据库 journal_mode。

新增监测汇总由持有主体所有权的运行时提供，采样器通过仅限真实 loopback 对端的私有读密钥请求。密钥位于 cache/monitoring，使用私有目录、0600 原子独占创建，不进入持久主体备份；链接、硬链接和异常内容被拒绝。密钥不可用时禁用此认证路径并记录无秘密告警，正常运行时仍可启动。该密钥只在 /api/v1/admin/monitoring/soak 生效，不能获得管理、导出或普通读权限。

汇总按 subject_id 限定，只返回模型状态/耗时、供应商聚合、保留批次和完整性摘要。数据库读取仍为 mode=ro + query_only，保留 WAL 可见性；设置有限忙等待、执行截止和每页 2,000 条上限。分页内部 ID 只进入私有游标状态，不复制到 soak 日志。采样器禁用环境代理和重定向，限制 loopback origin、消息长度与输入结构。

现有就绪语义不变：alert 模式不自动暂停主体。就绪失败新增安全 reason_code，采样器记录原因；完整性报告摘要单独保留，避免把 ready 等同于所有完整性检查通过。数据库读取失败新增 sqlite_errorname，不记录原始异常正文、路径、密钥或模型内容。

### 复审重点

真实对端地址用于本机密钥判定，不信任 X-Forwarded-For。普通 read/operator 凭据沿用原鉴权；本机密钥未并入其他角色。无新增外网请求或生产数据目录写权限。沙箱完全保持 ProtectSystem=strict 与原 ReadWritePaths=/var/log/noyra。启动仍先检查主体所有权；监测缓存目录只在首次创建时初始化，不递归硬化整个已有 cache。

原有脚本直接读取能力保留作旧运行时兼容回退，并明确标记数据来源和 runtime_error；安装后的 main 默认优先通过运行时接口读取。接口由统一路由清单和 OpenAPI 文档登记，匿名不可访问。原有匿名成功健康响应保持，503 仅增加安全分类字段。

### 验证（阶段结果）

Windows 综合回归 117 passed、2 平台 skipped、19 subtests passed，覆盖整个 test_service.py、架构属性和依赖边界；Linux 监测/采样器回归 36 passed，包含实际 root mount namespace：原 mode=ro 直读失败，而受认证的运行时汇总成功。最终 Windows 监测回归 42 passed、2 平台 skipped；新增游标/代理/重定向/硬链接/密钥降级边界定向测试通过。全库 Ruff 与 Mypy 通过。

## 已有能力核验与后续部署

100 秒总超时、20 秒连接超时已存在，不重复修改。现有终态等待维护覆盖预览、WAL 可见性、计划哈希、审计、幂等、并发和未知结果预算保留；本轮相关回归 27 passed。维护入口严格检查 schema，不能对生产 schema99 直接执行最新版本写维护；应先按标准备份/升级流程迁移，再预览计划、逐批维护。未知模型调用与资金记录不随终态等待清理而确认或重放。

完整性修正函数还在生产 mode=ro/query_only 事务中作新旧对照：安装版报 removed/create 错误，修正版通过 24 条意图。只在独立 Python 探针内替换函数，未写生产源码、数据库、告警状态或 hash。生产仍运行原版本 c9e2d9ccfcf3；本轮代码修复是否已部署必须以最终交付和服务器版本核查为准。


## 最终模块复审与交付边界

完整性模块提交 `492a759`，观测模块提交 `e96e794`，均保持提交后工作区干净。提交后复核：monitoring/soak 定向回归 49 passed、2 platform skips；Linux WSL root mount namespace 回归（包含最终 root 读取服务账号 0600 监测密钥实现）37 passed；超时与终态等待 27 passed；服务健康/令牌/睡眠/关闭定向 6 passed、1 platform skip；Windows 监测与 API/公开/采样器组合回归 42 passed、2 platform skips；完整服务/架构/依赖组合阶段 117 passed、2 platform skips、19 subtests。全库 Ruff 与严格 Mypy 均通过。

模块二后未改变 production data、部署单元或密钥权限；最后一次 Linux 验证重新覆盖了实际只读挂载。公开清单最终为 1,047 public files、1,046 checksums、202 exclusions，源码 ZIP 导出/校验通过。仍未部署到第一台服务器，原因是 PR #22 尚未合并且生产端仍是 c9e2d9ccfcf3；部署应使用合并后包含 492a759/e96e794 的版本，先按已验证备份与回退边界升级，再只读核验版本、监测 token 生成、健康 reason_code 和 soak source=runtime。
