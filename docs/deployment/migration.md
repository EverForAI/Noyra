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

## 复核身份语义

单人自核验仍可由一个运营者使用，不要求独立身份服务。兼容 dual_review
保留历史绑定记录，但不声称两次绑定就是两个独立账户。新的 HTTP 复核中，
bearer 和该凭据创建的 cookie 会话共享同一绑定，多开浏览器不能增加复核数。

启用 migration_require_independent_review 时，dual_review 必须由可信认证代理
证明的两个稳定 principal 完成。新证据记录其绑定类型，历史会话或缺少类型
证明的批准不会被补推为独立账户批准。已经由旧兼容模式批准的任务不能直接
在独立模式下执行；取消并按当前要求重新生成提案。未完成任务可继续收集
真实 principal 复核。执行协调器在取围栏、验证和提交前也核对这一要求。
配置独立账户不等于证明两个不同自然人。

本地钱包授权现在只能由任务的 wallet-approval 管理端点产生；私有目标配置
中的 local_wallet_approval 不再作为授权来源。旧批准记录保留，导出前需要
一次明确的新格式确认。有效期内重试返回同一批准，可用当前 approval_id
撤销；取消任务、变更策略/目标/钱包或过期后不能继续读取本地密钥。
具体协议见开发文档 migration-approval-boundaries.md。
