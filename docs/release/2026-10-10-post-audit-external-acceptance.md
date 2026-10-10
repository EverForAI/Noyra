# 第二轮审计修复的外部验收交接

被整仓审查的应用提交：`c764cbef887adf21044cb86cc83717854d19570d`；schema 92。之后仅修正测试夹具和补充文档。实际发布使用最终完整HEAD，必须重新生成该SHA的模板，不复用应用基线的pending文件。

本轮只实施本地修复和合成回归，没有部署到服务器、发送真实通讯消息、调用付费资源或进行资金交易。聊天删除继续关闭。当前没有八项独立签名环境证据，**E01 仍为发布阻断**，不能把这份文档或本地测试作为环境通过记录。

## 固定版本和准备材料

1. 源、目标测试机使用同一个完整SHA，记录安装路径、`.noyra-source-sha`、三组依赖锁摘要、Ubuntu/systemd/Python版本和UTC窗口。
2. 使用隔离测试身份、非生产数据和受限凭据文件；操作令牌、API Key、私钥和助记词不进入证据JSON或交付日志。供应商模拟用于故障注入，真实付费调用及平台消息另行授权。
3. 完成独立提交本地验证后，使用下列命令生成待验收模板。文件必须不存在，防止覆盖旧证据：

```bash
python scripts/prepare-external-validation.py \
  --commit HEAD \
  --output output/external-gates-pending-final.json
```

模板不带签名且状态为pending，验证器必须拒绝它。这只是准备工作。若最终发布提交不同，必须重新绑定最终SHA并执行该版本的验收；不复制旧SHA的passed状态。

## 顺序与本轮重点

下表补充[八项签名证据合同](external-gates.md)和[迁移门禁运行手册](../audit/2026-10-03-migration-release-gate-runbook.md)，没有替换原有否定路径。

| 顺序 / gate ID | 本轮关注边界 | 必须保留的证据和拒绝路径 |
| --- | --- | --- |
| 1 / `ubuntu_systemd` | 安装、升级、恢复runner、服务用户与停止期间外发围栏 | 新装/升级/重启/回滚的同SHA证明；连接等待期间暂停、迁移围栏和优雅关闭，确认没有新的请求头外发，已发成功/unknown仍真实结算。不能只看进程active。 |
| 2 / `encrypted_volume` | 真实LUKS和未解锁启动保护 | mount source、dm-crypt类型、权限、服务账户访问及只读at-rest结果；未解锁/错误密钥拒绝，解锁后恢复。不要记录解锁口令。 |
| 3 / `backup_restore` | 流式摘要、注册快照与固定文件身份 | 大型真实格式备份的峰值RSS及数据库写锁等待；恢复后identity/schema/quick-check/哈希；校验期间替换、同长度改写、truncate及注册变动拒绝；错误keyring/chunk/reparse拒绝。Linux POSIX和Windows共享句柄分别验收，不把本机Windows通过当作Linux通过。 |
| 4 / `migration_fence` | 发送前租约复核、两机epoch和编辑CAS | source→target→rollback及commit；在途成功/失败/unknown的费用保留；两个后台窗口策略revision冲突，草稿不覆盖新策略；旧epoch、目标证明篡改、重复提交、中断恢复拒绝。 |
| 5 / `signer_kms` | 独立签名、轮换及费用保护 | 超时/拒签/错签/旧key拒绝；仅成本限制且价格未知时不调用模型，明确免费/已知价格和组合限额按合同准入；新授权必须持同SHA验收证明。不能用模型估算成本代替供应商账单。 |
| 6 / `reorg_nonce` | 真实测试链和unknown资金预留 | nonce冲突、广播未知、确认超时、reorg及明确重试；没有实际链证据不能填passed。使用测试资产，保持审计和余额预留完整。 |
| 7 / `https_proxy` | 管理会话世代、草稿及投稿事务 | TLS/Host/Origin/cookie/CSRF/限速；慢网退出重登后旧写刷新不能污染提示/表单/按钮；两个窗口修改迁移策略CAS；降低投稿字节/队列/访客限额后旧请求不能绕过，幂等重放和原内容保留；密钥撤销/停用后等待中的模型、embedding和搜索不外发。 |
| 8 / `soak` | 修复后的完整性、容量和准确统计 | 24小时最低、72小时正式发布建议；CPU/RSS/磁盘/WAL/数据库及路由审计增长，供应商健康、认知睡醒和费用/unknown记录；任意探针失败按失败判定，终点smoke不能抹掉此前失败。记录原因、修复和重新验收窗口。 |

可读探针在隔离Ubuntu测试机执行：

```bash
umask 077
bash scripts/remote-acceptance.sh preflight /var/tmp/noyra-c764cbe-preflight
bash scripts/remote-acceptance.sh smoke /var/tmp/noyra-c764cbe-smoke
# 仅在已批准的隔离环境启用长观察；当前交接没有启动新soak。
bash scripts/remote-acceptance.sh soak 86400 /var/tmp/noyra-c764cbe-soak-24h
```

这些脚本不会覆盖两机签名、KMS、真实链和浏览器代理验收。公开健康200也不能单独证明后台受鉴权完整性、账本、外发围栏或记忆可用性。

## 签名和发布条件

每项保存执行人、独立复核人、起止UTC、失败注入结果和脱敏证据引用；顶层记录包含最终SHA、复核时间及Ed25519签名。执行人不得自审。按 `external-gates.md` 将真实证据交给受保护workflow，发布时检查72小时新鲜度、精确SHA和签名。

在E01通过前保持既有生产门禁；不以关闭完整性保护、修改passed字段、删除失败日志或更换SHA来解除阻断。这份交接不授权部署或生产操作。
