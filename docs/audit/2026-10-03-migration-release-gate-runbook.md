# Noyra 迁移发布门禁运行手册（2026-10-03）

这份手册描述如何在真实环境验收迁移代码。它不能用本地 pytest、临时 SQLite 或伪造 JSON 代替。迁移默认关闭；只有全部门禁在同一个 commit SHA 上通过，并由独立 reviewer 审核后，才允许在管理台开启迁移。

## 发布前固定条件

1. 源主机和目标主机都运行同一 release SHA，Ubuntu/systemd 安装器完成，服务用户、目录权限和 systemd hardening 与仓库版本一致。
2. 两台主机的数据盘均已挂载并通过 at-rest 检查；备份 keyring 由受保护的 systemd credential 或等价的 KMS 注入，不写入请求、日志或普通配置导出。
3. 目标 agent 使用 HTTPS origin、独立 target identity、Ed25519 公钥和随机 session token。token 只保存在目标主机受限文件中，源端通过受限 secret 文件读取。
4. 所有测试记录使用待发布 commit 的完整 40 位 SHA；证据 bundle 不得包含 API key、token、密码、私钥、助记词或钱包材料。

## 八项外部门禁

### 1. Ubuntu/systemd

在全新或清理后的 Ubuntu 主机执行标准安装，检查 `noyra`、migration runner 和 target agent 的 unit、用户、权限、`ProtectSystem`、`NoNewPrivileges`、`ReadWritePaths` 及重启恢复。记录安装器版本、`systemctl cat`、`systemctl is-active`、`systemctl is-enabled` 和精确 SHA。

### 2. 加密卷

在源、目标主机分别验证 LUKS/dm-crypt 映射、挂载点、数据库路径和 at-rest health。卸载或使用错误密钥时必须 fail closed，不能让服务回退到未加密目录。证据只保留设备类型、映射 ID 的脱敏摘要和检查时间。

### 3. 备份恢复

使用真实生产格式的加密备份，在隔离的目标 restore root 恢复。验证 keyring generation、密钥指纹、SQLite quick check、subject identity、schema 和事件链 tip；故意使用错误 keyring、篡改一个 chunk、恢复到非空目录，均应拒绝。

### 4. migration fence

创建一个人工审批迁移任务并在源端执行 cutover。记录并验证：

- source epoch 与任务绑定，fence 前后版本不变；
- 新 admission、checkpoint、钱包签名和服务重启都被拒绝；
- 在途操作结束后才生成 snapshot；
- 目标 restore、health 签名和 activation digest 与任务/manifest 完全一致；
- 网络中断、进程重启、重复提交和旧 epoch 重放都保持 fail closed；
- operator rollback 只在 source epoch 未变化时清除 fence。

必须完成一次真实 source→target→rollback 和一次 source→target→commit。不要把数据库中的 `committed` 单独作为成功证据。

### 5. signer/KMS

如果部署使用独立 signer/KMS，确认迁移请求只携带 signer 引用和公钥指纹，签名操作在独立边界完成；源主机日志、artifact、审计记录和 target restore 中不得出现私钥。故意让 signer 超时、拒绝请求和返回错误签名，迁移及付款都必须停止并产生可审计的失败状态。

### 6. nonce/reorg

在 Sepolia 或专用测试链使用真实 signer，覆盖 nonce 冲突、低余额、Gas 超限、长时间未确认、替换交易和链重组。确认付款状态不会把未知广播误报为失败或成功，重试不会重复扣款，暂停和单笔/日限额始终生效。

### 7. HTTPS proxy

通过生产反向代理访问公开页、管理台和 target agent。验证 TLS、Host/Origin、Secure/HttpOnly/SameSite cookie、CSP、HSTS、请求体上限、登录失败限速和管理 token 轮换。测试明文回源、跨站请求、重定向和代理超时均不会绕过认证或泄露内部端口。

### 8. soak

至少运行 24 小时（正式发布建议 72 小时），覆盖正常认知循环、provider 故障切换、搜索冷却恢复、wallet worker 空闲、健康检查、日志轮转和数据库 retention。记录 CPU、内存、磁盘、WAL、失败率、平均响应时间和迁移/付款审计增长；确认没有未界定的后台增长或泄密日志。

## 生成和提交证据

独立 reviewer 将八项门禁的脱敏引用、执行人、复核人、开始/结束时间写入 `external-gates.json`，使用发布密钥签名。`scripts/verify_external_gates.py` 会强制检查：完整 gate ID 集合、同 SHA、72 小时新鲜度、reviewer 不得与执行人相同、每项证据引用、Ed25519 签名和敏感字段扫描。

通过 GitHub Actions 的 `external-gates` workflow，以 `release_sha` 指定待发布 commit，并把签名 bundle 作为受保护 environment 的 artifact 上传。随后 release workflow 会查找该 SHA 的成功运行、下载 artifact 并再次验证。下载失败、artifact 缺失、签名错误或 SHA 不一致都会直接阻止 release。

## 开放迁移前的最后检查

- 在管理台确认迁移开关仍为关闭，审批模式为人工批准。
- 确认 source 和 target 的 rollback 联系人、恢复时限和 operator token 轮换时间已记录。
- 只在八项 gate 的签名 artifact 已绑定待发布 SHA 后，才建立正式 release tag。
- 任一外部门禁过期、撤销或无法复核时，立即关闭迁移和自动付款，并保留源端 fence/target activation 的审计记录以便恢复。
