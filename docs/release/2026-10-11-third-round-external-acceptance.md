# 第三轮审计修复的外部验收交接

本页补充 [八项签名门禁](external-gates.md)，不替代真实环境证据。此前服务器和旧 SHA 的通过状态不能用于本轮；只有最终实际安装的完整 SHA、独立执行/复核和 Ed25519 签名满足合同后才可发布。

## 必须覆盖的本轮变化

| 门禁 | 新增正常与拒绝路径 |
| --- | --- |
| ubuntu_systemd | 真实 SMTP 的 DNS、连接、STARTTLS、AUTH 多轮挑战、DATA 和取消；停止/迁移/凭据轮换后不得开始新的认证或正文写入；总预算内停止慢读，不耗尽 asyncio executor。保留已发 unknown 和 Message-ID，对账前不重发。 |
| encrypted_volume | LUKS 未解锁启动保护、服务账户和秘密权限；新 deadline/日志组件不能向明文临时目录输出秘密或聊天内容。 |
| backup_restore | schema71 到当前、当前备份清洁主机恢复、数据库完整性及主体/账本一致；聊天删除仍不可用，历史收据和防复活保护保留。 |
| migration_fence | 公共知识 discovery 后、分页间、签名验证后和导入/游标提交前迁移；源失效后不发新 GET、不导入失效结果、不倒退或跳过游标，目标重启可恢复。 |
| signer_kms | 继续验证旧合同的 signer 身份、拒签、轮换与 unknown；共享网络组件改动不能放宽钱包预算、策略或最终外发围栏。 |
| reorg_nonce | 原 nonce/reorg/unknown 预留及对账合同完整；不存在因超时重试自动释放预留或重复付款。 |
| https_proxy | 请求行、请求头、JSON、webhook 慢速 drip 和正常合法大请求；反向代理预算与应用一致。升级 development/stable 慢 DNS、连接、头部、body、重定向共享绝对截止时间；404/429/403、SHA、签名和发布时效合同保持。 |
| soak | 最终安装 SHA 24/72 小时，记录 worker/线程峰值、CPU/RSS、WAL、磁盘、健康、完整性和 unknown；注入 SMTP 与公共知识超时/撤销。任何失败探针保留失败，结尾 smoke 不覆盖历史失败。 |

另需在隔离云账户以合成标记验证 S3 INFO/DEBUG、多 handler、异常和 history recorder。新日志不得包含凭据、Authorization、签名、对象标识或原始响应。检查生产历史日志属于另行获授权的环境操作；本地修复不能证明过去从未暴露，也未执行清理或轮换。

## 交付与签名

最终提交后运行：

```text
python scripts/prepare-external-validation.py --commit HEAD --output <新的私有证据目录>/external-gates-pending.json
```

模板全部保持 pending，不能人工直接改为 passed 代替执行。执行者记录安装 SHA、三份锁文件哈希、UTC 时间、正常/拒绝路径、脱敏证据引用；由不同复核者核验并签名，发布时满足 72 小时新鲜度。文档提交同样会改变 SHA，模板必须在最终提交后生成。既有已验收运行版本不会仅因超过 72 小时而停止。

本轮未授权部署、真实平台消息、付费调用或交易；上述真实操作不在本地修复中执行。未取得八项合格证据前，E01 保持发布阻断。
