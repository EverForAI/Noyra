# Noyra 可信迁移

迁移功能默认关闭。开启后默认使用人工审批；只有注册目标、目标完成一次性 Ed25519 challenge、目标使用加密存储并通过版本和资源检查时，才会生成可执行提案。

管理台的“迁移与庇护所”页面用于设置策略、查看目标和处理提案。策略更新采用 revision compare-and-swap，旧页面提交会被拒绝。拒绝记录按目标进入冷却期，不能通过修改提案文字或更换幂等键绕过。

钱包迁移优先使用 `external_signer_rebind`：迁移包只保存 signer 标识和指纹，目标重新绑定自己的系统凭据，不复制私钥。本地钱包迁移必须同时启用 `local_wallet_transfer_enabled`，并对具体任务和地址执行第二次批准。

紧急恢复是独立模式，只允许恢复到已注册并列入 allowlist 的 standby 目标，并要求可验证的备份和源主机故障证据。系统不会扫描公网、自动登录任意 SSH 主机，也不会把模型或云厂商密钥交给迁移提供商。

发现异常时可在管理台关闭迁移策略；关闭会阻止新候选、提案和执行。迁移切换采用单活 epoch，旧 epoch 被撤销后继续写入会失败。切换前失败保留源主机权威，切换后失败必须走显式回滚。

## 管理台操作顺序

1. 在目标安装相同提交的 Noyra；准备目标私密身份文件（Ed25519 签名密钥、X25519 接收密钥与独立连接令牌）。私钥只保留在目标。身份文件为 root:noyra 0640；来源管理台只填写两种公钥和连接令牌。
2. 按 `deploy/migration-agent.env.example` 创建目标 `/etc/noyra/migration/agent.env`，仅配置实际月成本、地区、需要的 signer/归档密钥文件引用。不要复制整个运行时环境文件。费用必须显式配置（免费资源允许显式 0），缺失时观察失败。
3. 目标 agent 的 8876 端口保持 loopback，经受认证 HTTPS 代理提供 `/v1/` 协议；开启目标 activation path unit。来源管理台登记后点击“发起验证”，会通过已保存的私密连接令牌请求目标签名并验证。
4. 目标先预配来源正在使用的 API/通讯凭据和归档密钥。配置/不可变历史会完整迁移；必要凭据缺失或指纹错误时禁止激活。可选私密 `secrets/migration-bindings/<target-id>.json` 只记录 credential_binding 的 references/fingerprints 和 signer_id。
5. 开启迁移时保持人工审批，设置允许目标/地区、费用、容量、信任等级、维护窗口和拒绝间隔。运行时在资源与签名硬条件通过后调用认知资源评估必要性、信任与收益风险；资源可用本身不会生成迁移决定。
6. 检查申请详情后批准。运行时处理已批准任务，也可点击管理台“执行”；本地钱包必须启用可迁移选项并对具体任务二次授权。外部 signer 必须支持身份挑战协议（见最终验收手册）。
7. 来源隔离并 drain 后生成一次加密完整主体包，目标恢复、核对数据/凭据/钱包/存储后由 root 切换。未知远端结果保持来源隔离。回滚必须取得目标签名、持久停用证明，不能通过重启或删除 fence 绕过。

## 接口边界

- `GET /api/admin/migration/policy`、`PUT /api/admin/migration/policy`：读取或按 revision 更新策略。
- `POST /api/admin/migration/targets`：登记 pending 目标。
- `POST /api/admin/migration/targets/{id}/challenge` 和 `/attest`：一次性 challenge 验证。
- `GET /api/admin/migration/targets`、`/proposals`、`/tasks`：只返回当前 subject 的脱敏状态。
- `POST /api/admin/migration/proposals/{id}/approve|reject`：人工决策并写入审计。
- `POST /api/admin/migration/tasks/{id}/cutover|rollback`：执行受状态机和 epoch fencing 保护的切换。
- `POST /api/admin/migration/recovery`：仅用于 allowlist、已 attested 的 standby 和有界的备份/故障证据。

所有接口要求 operator Bearer 或受认证会话；会话写请求需要 CSRF token。开始远端副作用后的失败保留来源 fence，等待显式协调或回滚；不宣称来源仍可写。默认关闭和默认人工批准保持不变。
