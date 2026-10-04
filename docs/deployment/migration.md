# Noyra 可信迁移

迁移功能默认关闭。开启后默认使用人工审批；只有注册目标、目标完成一次性 Ed25519 challenge、目标使用加密存储并通过版本和资源检查时，才会生成可执行提案。

管理台的“迁移与庇护所”页面用于设置策略、查看目标和处理提案。策略更新采用 revision compare-and-swap，旧页面提交会被拒绝。拒绝记录按目标和原因类别进入冷却期，不能通过修改提案文字或更换幂等键绕过。

钱包迁移优先使用 `external_signer_rebind`：迁移包只保存 signer 标识和指纹，目标重新绑定自己的系统凭据，不复制私钥。本地钱包迁移必须同时启用 `local_wallet_transfer_enabled`，并对具体任务和地址执行第二次批准。

紧急恢复是独立模式，只允许恢复到已注册并列入 allowlist 的 standby 目标，并要求可验证的备份和源主机故障证据。系统不会扫描公网、自动登录任意 SSH 主机，也不会把模型或云厂商密钥交给迁移提供商。

发现异常时可在管理台关闭迁移策略；关闭会阻止新候选、提案和执行。迁移切换采用单活 epoch，旧 epoch 被撤销后继续写入会失败。切换前失败保留源主机权威，切换后失败必须走显式回滚。

## 管理台操作顺序

1. 在“迁移与庇护所”中保持迁移关闭，先登记目标的 HTTPS 地址、Ed25519 公钥、版本 SHA、系统架构，并确认目标卷已加密。
2. 点击“发起验证”，把 nonce、过期时间和 source epoch 交给目标侧 agent。目标 agent 只用自己的私钥对固定格式的 challenge 签名；签名内容不包含 API key、钱包私钥或其他凭据。
3. 把签名提交回“目标验证”。只有 attestation 成功后目标才从 pending 变为 active，未验证目标不能进入提案、任务或紧急恢复。
4. 开启迁移时保持人工审批，检查提案中的目标、原因、收益、风险、证据和过期时间，再批准。拒绝会按目标和原因进入冷却期。
5. 任务完成传输、恢复和验证后才执行切换。切换会终止源实例的 admission epoch；失败时只允许明确填写原因并回滚。

## 接口边界

- `GET /api/admin/migration/policy`、`PUT /api/admin/migration/policy`：读取或按 revision 更新策略。
- `POST /api/admin/migration/targets`：登记 pending 目标。
- `POST /api/admin/migration/targets/{id}/challenge` 和 `/attest`：一次性 challenge 验证。
- `GET /api/admin/migration/targets`、`/proposals`、`/tasks`：只返回当前 subject 的脱敏状态。
- `POST /api/admin/migration/proposals/{id}/approve|reject`：人工决策并写入审计。
- `POST /api/admin/migration/tasks/{id}/cutover|rollback`：执行受状态机和 epoch fencing 保护的切换。
- `POST /api/admin/migration/recovery`：仅用于 allowlist、已 attested 的 standby 和有界的备份/故障证据。

所有接口都要求 operator 会话和 CSRF token。请求失败时，源实例保持权威，不会因为候选服务器可用就自动迁移。
