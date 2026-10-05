# 复审修复后的最终提交验收

本文用于 B12 真机验收。B01–B11 的代码与本地组合验证记录见 `docs/audit/2026-10-05-followup-remediation.md`。本手册不是通过证明。迁移默认关闭；开启默认人工批准。生产自动付款和 policy_auto 迁移需要同 SHA 的独立签名证据。

## 固定待验版本

在所有修复提交结束后执行（输出路径不存在时创建，避免覆盖历史证据）：

```bash
python scripts/verify-committed.py --commit HEAD --scope all --output output/final-committed-verification.json
python scripts/prepare-external-validation.py --commit HEAD --output output/final-external-pending.json
```

两台验收服务器必须安装该 JSON 中的完整 `commit_sha`，记录实际 `.noyra-source-sha`、schema（本轮为 80）、Python/依赖锁摘要、UTC 时间及源/目标标识。后续代码改变则重新确定 SHA；不能复用旧 SHA 证据。schema 80 增加证据计数表，回退旧代码需恢复升级前加密备份，不能只切回旧二进制。

## 所需环境及保护

- 两台隔离的 Ubuntu/systemd 主机、真实 LUKS 数据卷、足够的目标 staging 空间与独立备份密钥。使用测试主体，先核实干净主机可以恢复加密备份。
- 有效模型/搜索 API、目标预配的同一凭据文件和归档密钥；模型可以提出建议，目标登记、允许范围、费用上限和人工决策属于管理员。
- 使用测试钱包与测试链；外部 signer/KMS 和本地钱包两条路线分别验收。本地钱包必须显式 opt-in 和 task 授权。
- 原生 HTTPS 反向代理。agent 监听本机 8876，经其独立连接令牌认证；管理台经 HTTPS、operator 会话和 CSRF。不要将 signer、云账户、源端管理员令牌放进迁移申请。

目标 agent 配置在 `/etc/noyra/migration/agent.env`（参考 `deploy/migration-agent.env.example`），成本缺失会拒绝观察。目标运行时、agent、root activation 若使用 systemd credentials，分别配置所需 `LoadCredential`；这些 unit 不会自动共享 credential namespace。root activation 必须能解析目标 operator 令牌以访问 `/api/v1/admin/readiness`。目标身份切换配置由 root 控制器写入任务专属 EnvironmentFile 并通过 migration-target.conf 引用；不要追加覆盖主体、genesis 或 target_id 的后置 systemd drop-in。回滚同时恢复原 drop-in 引用。

备份保留主体数据库、内容、源 epoch 和 fence；机器本地迁移请求/状态队列及 root-only 激活/回滚目录不进入可移植备份。活动 fence 若不能由服务账户读取，备份会失败而不会丢弃隔离。升级前应完成或明确回滚未决迁移；故障恢复保留原目标的 root 控制目录，不把普通主体备份当作 root 激活日志副本。

## 八项必须执行的验收

| 门禁 | 实际操作和故障注入 | 通过证据 |
| --- | --- | --- |
| ubuntu_systemd | 新安装、旧版本升级至 schema 80、进程重启/关机恢复、安装失败回滚；确认 source SHA 和 systemd 配置一致 | 安装日志、就绪探针、schema、升级前后备份与回滚结果；无重复主体进程 |
| encrypted_volume | 检查真实 LUKS 挂载、服务账户读写权限；在测试副本模拟未挂载、密钥缺失、权限扩大 | 不安全卷/权限时拒绝准入；恢复正确环境后可用；不能用模拟 attestation 替代实际加密卷 |
| backup_restore | 使用有效 API 配置、cold 事件/观察、工作区、空目录和必要历史生成认证备份；干净主机恢复并逐项读取 | 恢复身份/schema/内容摘要，真实冷对象可解密；篡改和错误密钥拒绝，不静默丢失配置 |
| migration_fence | 按下表执行正常切换、拒绝冷却、传输中断、丢失激活回执、双端重启及回滚 | 同一时刻最多一个可写主体；任务/epoch/manifest/recipient 与回执一致；本地钱包与外部 signer 均完整跑通 |
| signer_kms | 独立 signer 实际签领域隔离挑战，验证错误地址、错误任务、过期或拒绝凭据、密钥轮换、超时 | EIP-191 签名恢复地址与批准钱包一致；请求不能成为链上交易；未证明则不激活 |
| reorg_nonce | 真实测试链小额付款；制造 nonce 竞争、广播响应丢失、长时间 pending；用可控链制造重组并重查 | unknown 不自动重付，nonce 预留正确，单笔/日限额与暂停生效；重组状态/必要审计正确 |
| https_proxy | 公网和手机访问公开页/管理台；错误令牌、登录限速、会话过期、CSRF、转发头和证书检查 | 匿名 health 只含 status/service；认证诊断有权限边界；agent 连接认证且无任意代理/重定向 |
| soak | 用下方只读收集器连续运行 72 小时，跨日、故障切换/冷却恢复、容量满、消息投递及付款结算 | 磁盘/WAL/内存/聚合保留趋势；满容量停止新工作但完成已有投递；空闲不持续产生心跳训练记录 |

## 双机迁移故障矩阵

| 场景 | 必须观察的结果 |
| --- | --- |
| 资源充足但认知不建议/不高度信任 | 无迁移申请 |
| 签名错误、过期、超预算、地区不符或容量不足 | 候选不可执行，模型不能绕过 |
| 人工拒绝，修改原因再申请 | 目标级冷却继续生效；管理员设置的间隔保留 |
| 批准后策略变更/目标撤销/出现未决付款 | 执行前拒绝，不能沿用旧批准绕过新条件 |
| recipient 公钥/PoP/密文/完整文件清单错绑 | 拒绝恢复或激活，保留原目标数据 |
| 目标缺 API 密钥、归档密钥或 signer 身份不符 | 拒绝激活；不能删除历史配置来制造成功 |
| 正常迁移 | 来源先持久隔离，目标完整恢复后激活；目标预先运行不同主体/不同 genesis，确认 root 生成的 EnvironmentFile 覆盖身份配置；检查数据库外文件和真实钱包 |
| 来源收到激活成功前断网/超时，再重启来源 | 来源仍不可写；不能推断远端未激活 |
| 回滚时目标不可达 | 保持隔离；恢复网络后显式重试回滚 |
| 目标确认持久停用，再放行迟到 activate | 迟到激活被永久撤销记录拒绝，来源可以恢复 |
| root 在各目录/数据库切换点崩溃 | 重启恢复旧目录、数据库和 runtime 配置；无半新半旧状态 |
| 本地钱包模式 | 授权地址和任务匹配，recipient 加密包中携带 keystore/解锁文件/RPC；目标解密与实际签名一致；来源保留但受 fence 控制，不声称磁盘安全擦除 |

上述故障只在隔离环境实施。保留操作时间线、双端审计、脱敏回执和文件摘要，不在证据 JSON 中包含密钥、令牌、密码、原始钱包或数据库文件。

## 外部 signer 身份挑战协议

目标调用 signer 的 `POST /migration-identity`。请求为 `{signer_id, challenge}`，`challenge` 是含 domain、nonce、task_id、target_id、manifest_digest、target_identity、signer_id 的规范 JSON 字符串。signer 校验其中 domain 为 `noyra-migration-wallet/v1`，对该消息执行 EIP-191 personal-sign，返回 `signature`；Noyra 恢复地址并核对配置钱包。实现与边界以 `HTTPSWalletSigner.prove_migration_identity` 为准。该协议不使用交易签名或付款授权。

## 收集与关闭

在验收主机源码目录，使用 tmux 保持会话：

```bash
bash scripts/remote-acceptance.sh preflight /var/tmp/noyra-final-preflight
bash scripts/remote-acceptance.sh smoke /var/tmp/noyra-final-smoke
bash scripts/remote-acceptance.sh soak 259200 /var/tmp/noyra-final-soak
```

收集器只读取状态，不能替代故障注入和双机测试。每项填写实际执行者、独立复核者、起止时间、非敏感证据引用；未做项保持 pending，失败保持 failed。独立审核与 Ed25519 签名流程见 `docs/release/external-gates.md`。发布时签名证据须在 72 小时新鲜窗口内。

全部门禁真实通过后，可以由部署者在管理台开启已验收功能；默认配置不会自动变成开启。发生新代码修复则重建相同版本的验收证据，不能只把 pending 改成 passed。
