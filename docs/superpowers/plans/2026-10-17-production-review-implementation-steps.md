# Noyra 生产复盘修复实施步骤

> 配套文档：`docs/superpowers/plans/2026-10-17-production-review-remediation.md`
> 目标：把复盘结论拆成可逐项实现、测试、发布和回滚的工程任务。
> 默认工作流：先在代码仓库完成测试和静态检查，再在第二台单系统盘 Ubuntu 验收，最后评估第一台长期运行实例的升级。

## 0. 开工前冻结和记录

1. 新建修复分支，例如 `codex/production-review-remediation`；记录基线 commit、Python 版本和测试环境。
2. 保存当前生产只读基线：当前 release SHA、schema marker、数据库结构指纹、`systemctl show noyra`、`/health/live`、`/health/ready`、磁盘容量、备份列表和 Provider 配置摘要。禁止保存 API key、token、密码、私钥和完整请求内容。
3. 把已经完成的真实门禁登记为历史证据，不重复执行破坏性测试：错误 keyring、篡改备份、非空恢复目标、残留清理、HTTPS 认证、signer 身份、nonce、Sepolia 转账、余额/费用/nonce 门禁。
4. 明确代码阶段和服务器阶段的退出条件。任何阶段测试失败，停在该阶段修复，不把失败带到下一阶段。

仓库基线命令：

```bash
git status --short --branch
git rev-parse HEAD
python -m pytest -q
python -m ruff check src tests scripts
git diff --check
```

## 1. 阶段 A：单系统盘存储合同

### A1. 定义存储模式和安全路径 API

**修改：**新增 `src/noyra/core/storage_mode.py`；补充 `src/noyra/core/at_rest.py`、`src/noyra/service.py` 的调用。

**实现：**

1. 定义 `StorageMode`：`system_disk`、`dedicated_mount`、`unavailable`。
2. 用 `findmnt` 等价的受控检测区分真实挂载、普通目录、symlink、挂载丢失；禁止通过 `Path.is_dir()` 把空目录误认为原数据卷。
3. 定义 `StorageLayout`，包含数据目录、备份目录、upgrade 控制目录、模式、owner/mode、设备标识和可写性；输出不得包含 secret。
4. 提供 root-only 的 `ensure_system_disk_layout()`，只创建不存在的目录，不覆盖非空目录、symlink 或与记录设备不一致的目录。
5. 为 dedicated mount 保存一个非敏感 marker，例如设备 UUID/文件系统身份；挂载丢失时返回 `unavailable`，不自动在挂载点下创建新数据库。
6. 把磁盘 headroom 检查做成纯函数，区分服务当前运行所需空间、备份临时空间和升级临时空间。

**测试：**新建 `tests/test_storage_mode.py`，覆盖普通系统盘目录、真实挂载模拟、symlink、挂载丢失、非空目录、设备身份变化、权限不足和空间不足。

### A2. 删除 mountpoint 启动硬条件

**修改：**`deploy/systemd/noyra.service`、安装器生成的 drop-in、`scripts/setup-ubuntu-single-disk-storage.sh`、相关 contract tests。

**实现：**

1. 删除 `ConditionPathIsMountPoint=/var/lib/noyra`；改为 `ExecStartPre` 调用只读安全路径检查。
2. `noyra.service` 继续使用 `ReadWritePaths=/var/lib/noyra`，但由安装器保证叶子目录归 `noyra` 所有。
3. `setup-ubuntu-single-disk-storage.sh` 不再写入 mountpoint condition；它只负责显式创建 LUKS 镜像、挂载、marker 和切换配置。
4. 服务启动时若 storage mode 为 `unavailable`，退出稳定状态码并让 systemd 日志给出 `storage_unavailable`；不得初始化空 SQLite。
5. `/health/ready` 返回 `storage_mode` 和稳定 reason code；匿名响应只保留 `status/service`，operator 详情再显示路径模式和设备摘要。

**测试：**更新 `tests/test_storage.py`、`tests/test_m42_p2_18_subject_storage.py`、`tests/test_gate3_contract_ops.py`，新增 systemd 文本合同，确认没有 mountpoint condition，确认系统盘模式可启动。

### A3. 统一安装器目录初始化和权限边界

**修改：**`scripts/install-ubuntu.sh`、`scripts/lib/control-layout.sh`、`scripts/upgrade-ubuntu-runner.sh`、`src/noyra/core/at_rest.py`。

**实现：**

1. 安装器在备份前做路径和空间预检，随后以 root 创建：
   - `/var/lib/noyra`：`noyra:noyra 0700`；
   - `/var/backups/noyra`：root 管理，按备份流程需要设置安全 mode；
   - `upgrade`：`root:noyra 0750`；
   - `upgrade/requests`：`noyra:noyra 0700`；
   - `upgrade/processing`：`root:root 0700`。
2. `noyra` 服务只写数据叶子和 request 目录，不对 root 管理的 `upgrade` 根目录执行 chmod/chown。
3. `_harden_private_paths()` 只处理服务账户拥有的目录；root-owned control path 由安装器和 runner 验证。
4. 所有路径拒绝 symlink、hardlink alias、目录穿越和未知控制文件。
5. 安装失败时按既有 on-error/on-exit 逻辑恢复旧布局和旧服务状态。

**测试：**运行 `tests/test_upgrade_storage_boundary.py`、`tests/test_storage.py`、`tests/test_m42_p2_14_at_rest.py`；新增 runuser 真实权限测试，确认服务账户能创建备份而不能修改 upgrade 根目录。

### A4. 阶段 A 本地验收和提交

```bash
python -m pytest -q \
  tests/test_storage.py \
  tests/test_storage_mode.py \
  tests/test_upgrade_storage_boundary.py \
  tests/test_upgrade_deployment.py \
  tests/test_m42_p2_14_at_rest.py
python -m ruff check src/noyra/core/storage_mode.py src/noyra/core/at_rest.py src/noyra/service.py tests
git diff --check
```

完成后提交：`fix: support system-disk deployments safely`。

## 2. 阶段 B：升级、schema 和恢复流程

### B1. 让 recover-only 在未初始化存储时安全退出

**修改：**`scripts/upgrade-ubuntu-runner.sh`、`deploy/systemd/noyra-upgrade-recover.service`、`deploy/systemd/noyra-upgrade.path`。

**实现：**

1. runner 启动先读取 storage mode；`unavailable` 时写入受保护的 `storage_unavailable` 状态或明确跳过码，不返回 generic exit 1。
2. 缺失目录只允许 root 初始化；普通 recover-only 不在可能是错误挂载点的目录下创建数据。
3. `noyra-upgrade.path` 在目录未准备好时不触发无效升级任务。
4. recover-only 必须幂等：`idle`、`completed`、`failed`、`interrupted` 重复执行都返回 0；只有 `queued/running/rolling_back` 转为 `interrupted`。
5. 管理 API 显示 `upgrade_interrupted`、`storage_unavailable`、`upgrade_install_failed` 的具体建议。

**测试：**更新 `tests/test_upgrade_deployment.py`、`tests/test_upgrade_manager.py`，新增缺目录、错误 owner、symlink、重启中断和重复 recover 测试。

### B2. 统一 schema marker、结构指纹和回滚

**修改：**`src/noyra/core/database.py`、`src/noyra/core/upgrade.py`、`scripts/install-ubuntu.sh`、历史迁移 fixture。

**实现：**

1. 每个迁移阶段同时校验 schema version、DDL fingerprint 和 persistent feature marker。
2. marker 写入与迁移状态写入使用可恢复事务；异常时 marker 不得领先于实际结构。
3. 新 release 启动时若 marker/指纹不一致，返回 `schema_contract_mismatch`，不继续提供写服务。
4. 升级失败按“备份验证 → 新 release 安装 → migrate → readiness → current 切换”顺序执行；任何一步失败恢复 previous。
5. 回滚后再次检查旧 release 的 schema 合同和 `/health/ready`，记录 rollback reason。

**测试：**运行 `tests/test_legacy_schema71_upgrade.py`、`tests/test_migration_schema.py`、`tests/test_upgrade_manager.py`；新增 71→80、marker ahead、DDL mismatch、迁移中断和旧版本恢复 fixture。

### B3. 阶段 B 验收

```bash
python -m pytest -q \
  tests/test_legacy_schema71_upgrade.py \
  tests/test_migration_schema.py \
  tests/test_upgrade_manager.py \
  tests/test_upgrade_deployment.py \
  tests/test_upgrade_storage_boundary.py
```

不得在第一台生产数据库上人为制造 schema 故障；所有故障注入使用临时数据库、临时 release 和离线备份。

## 3. 阶段 C：Provider 错误、健康状态和 reasoning

### C1. 建立 Provider 错误分类合同

**修改：**`src/noyra/model/openai_compatible.py`、`src/noyra/model/gateway.py`、`src/noyra/model/ledger.py`、`src/noyra/model/resources.py`。

**实现：**

1. 定义稳定分类：`provider_auth_failed`、`provider_http_404`、`provider_rate_limited`、`provider_http_5xx`、`provider_connect_failed`、`provider_timeout`、`provider_response_invalid`、`provider_outcome_unknown`。
2. 保存 HTTP status、bounded latency、provider request id（如安全）和分类；不得保存 key、完整 URL query、原始响应或 prompt。
3. 404、鉴权错误、模型不支持等确定性错误默认不重试、不继续消耗同一逻辑调用预算。
4. 超时或发送后断开归为 unknown；发送前连接失败才归为可重试 connect failed。
5. 保持 `model_calls`/`model_attempts` 的幂等 fence，unknown 永不自动重发。

**测试：**扩展 `tests/test_model_gateway.py`、`tests/test_gate1_unknown_model.py`、`tests/test_m42_p2_06_p2_07_provider_security.py`，每类错误至少一个请求、ledger、预算和日志断言。

### C2. 修复 group、key 和 provider health 投影

**修改：**`src/noyra/core/provider_health.py`、`src/noyra/model/resources.py`、相关 service route 和 UI。

**实现：**

1. 计算 group health 时只把当前 `active` 且未过期 cooldown 的 key 视为可用。
2. group 没有可用 key 时不能显示 healthy；显示 `cooldown`、`disabled` 或 `configuration_error`。
3. 配置、模型或 endpoint 变更后增加新的 resource revision，并清理或标记旧 key health 为 stale，避免旧失败污染新配置。
4. cooldown 半开只发起一个 probe；probe 结果成功才恢复 active，失败更新到期时间。
5. 管理 API 同时返回 group 状态、key 聚合状态、最近错误分类、cooldown_until 和 last_success_at，永不返回 key 内容。

**测试：**运行 `tests/test_provider_health.py`、`tests/test_search_routing_failover.py`，新增 active-only、all-cooldown、revision-change 和 single-probe 测试。

### C3. 添加模型能力与思考强度

**修改：**模型资源 schema、`src/noyra/model/types.py`、gateway request、`src/noyra/web/admin.js`/`admin.html`、数据库迁移。

**实现：**

1. 增加 capability profile：`reasoning_supported`、强度枚举、Provider 参数名、profile version。
2. 配置页面先根据模型能力显示强度选择；不支持时隐藏并持久化 `null`。
3. 调用时把低/中/高/极高映射为 Provider 允许的参数；不支持的 endpoint 不发送未知字段。
4. 将归一化 reasoning profile 写入 model call metadata，兼容旧记录。
5. 测试 endpoint/model 不匹配时给出清晰错误，而不是保存后才在自治循环中失败。

**测试：**新增 `tests/test_reasoning_profile.py`；更新 `tests/test_model_gateway.py`、资源 API/UI contract tests，覆盖支持、 不支持、未知能力和 provider 参数映射。

### C4. 健康检查 reason code 和 BrokenPipe 降噪

**修改：**`src/noyra/service.py`、`src/noyra/autonomy/loop.py`、`src/noyra/core/operator_controls.py`、日志测试。

**实现：**

1. 为每个健康 component 返回 `{status, reason_code, since, checked_at}`；reason code 使用固定枚举。
2. `/health/live` 只检查 HTTP 线程；`/health/ready` 只检查启动准入依赖；详细 `/health` 返回 operator 诊断。
3. Provider 暂时不可用、反思 pending、钱包队列 attention 不应把 live 误报失败；只有合同规定的 ready 依赖影响 ready 状态。
4. 记录 degraded 状态变更事件，不为每次探针重复刷整段日志。
5. 在 JSON 写出和 request handler 层捕获 `BrokenPipeError`/`ConnectionResetError`，只保留计数或 debug 级别记录。
6. `ReflectionCognitionPending` 进入有界 backoff/cooldown，并在 health 中显示 `reflection_pending`，不伪装成服务退出。

**测试：**扩展 `tests/test_gate3_contract_ops.py`、`tests/test_provider_health.py`、`tests/test_reflection_cognition.py`；新增 health matrix、匿名/管理员投影、BrokenPipe 和反思熔断测试。

### C5. 阶段 C 本地验收

```bash
python -m pytest -q \
  tests/test_model_gateway.py \
  tests/test_gate1_unknown_model.py \
  tests/test_provider_health.py \
  tests/test_reasoning_profile.py \
  tests/test_reflection_cognition.py \
  tests/test_gate3_contract_ops.py
python -m ruff check src/noyra/model src/noyra/core/provider_health.py src/noyra/service.py src/noyra/web tests
```

## 4. 阶段 D：凭据、部署向导和迁移审批

### D1. 凭据生成和恢复说明

**修改：**`scripts/install-ubuntu.sh`、`deploy/noyra.env.example`、systemd credential units、`src/noyra/deployment_setup.py`、文档。

**实现：**

1. 首次安装只询问必要的管理员凭据和备份口令；其他内部 token 用 CSPRNG 生成。
2. 使用 root-only 文件或 systemd `LoadCredential`；禁止把 secret 放到 argv、普通 env、SQLite 或 journal。
3. 安装完成输出一次性恢复清单：keyring、credential 文件、服务器身份、数据库备份位置和重装注意事项；清单不含 secret 原文。
4. 管理 UI 显示“已配置/待验证/验证失败/待轮换”，提供轮换和重新测试操作。
5. 验证重启、备份恢复和新 release 仍能读取 credential；缺少 credential 时给出稳定原因码。

**测试：**运行 `tests/test_migration_credentials.py`、`tests/test_wallet_keystore.py`、`tests/test_deployment_setup.py`；增加 argv/journal/SQLite redaction 检查。

### D2. 部署模式和 dry-run

**修改：**沿用已有 `src/noyra/deployment_setup.py`、`scripts/install-ubuntu.sh`、`docs/deployment/setup-modes.md`。

**实现：**

1. `noyra setup --mode local|public|cloudflare` 先执行 storage、credentials、service、health 和 proxy preflight。
2. `--dry-run` 只渲染变更和检查项，不写文件、不 reload/restart、不生成 token。
3. public/cloudflare 模式原子更新 env、Caddy 和 systemd credential；失败恢复备份。
4. local 模式输出 SSH tunnel 命令和本地 health 入口，保持 Noyra 监听 `127.0.0.1:8765`。

**测试：**运行 `tests/test_deployment_setup.py`、`tests/test_deployment_contract.py`；覆盖重复执行、已有代理、替换失败和 rollback。

### D3. self-attested 与 dual-review

**修改：**迁移 policy/store/runtime/API/UI、`tests/test_migration_policy.py`、`tests/test_migration_proposals.py` 和相关 migration tests。

**实现：**

1. 增加 `approval_mode=self_attested|dual_review`，默认按 profile：staging/local 使用 self-attested，生产高风险动作可配置 dual-review。
2. self-attested 仍需操作者、目标身份、manifest digest、测试门禁和时间戳，不能绕过 wallet/backup/target health gate。
3. dual-review 要求第二个独立 approval；同一操作者不可伪造两个身份。
4. UI 显示模式、缺失门禁、cooldown、回滚状态和下一步操作；API 返回 stable error code。

**测试：**运行 `tests/test_migration_policy.py tests/test_migration_proposals.py tests/test_migration_end_to_end.py tests/test_migration_recovery.py`。

## 5. 阶段 E：备份、GC、retention 和活动日志

### E1. 备份 retention registry

**修改：**`scripts/deployment-maintenance.py`、`src/noyra/core/retention.py`、`tests/test_deployment_maintenance.py`、`tests/test_retention.py`。

**实现：**

1. 分开记录数据库 retention 和 backup artifact retention，避免用一个计数混淆两者。
2. 备份策略至少包含 `keep_verified_count`、`max_age_days`、`max_bytes`、`unverified_grace_days` 和最小可恢复点数量。
3. GC 继续保护 current、previous、运行进程引用和 release 关联备份；所有删除先输出 plan。
4. 未验证备份不作为恢复点；超过宽限期进入 `unverified_backup_attention`，默认不静默删除。
5. GC 预览和应用都记录 backup name、size、sha256、reason code，不记录密钥。
6. 单系统盘 headroom 不足时拒绝新备份/升级操作但保持旧服务运行。

**测试：**扩展 `tests/test_deployment_maintenance.py`，覆盖验证替代品、未验证宽限期、年龄/大小上限、current/previous 保护和空间不足。

### E2. retention 真实 cutoff 和 SQLite 空间

**实现：**

1. 构造最早事件超过 90 天、刚好 cutoff、未到 cutoff 三组 fixture。
2. 验证每张可回收表的 protected predicate、keyset cursor、失败重试和跨进程续跑。
3. 验证 append-only 证据、仍被引用的 model call、审计和恢复索引不会被删除。
4. 记录 deleted/protected/skipped、failure_stage、retry_at 和 cursor；执行后检查 `freelist_count` 和数据库增长。
5. 只在明确的维护窗口执行 checkpoint/VACUUM，不把 VACUUM 作为每次 retention 的副作用。

**测试：**运行 `tests/test_retention.py tests/test_evidence_capacity.py tests/test_management_integrity_audits.py tests/test_storage_lifecycle.py`。

### E3. 活动日志和长 soak 观测

**修改：**`src/noyra/service.py`、`src/noyra/web/admin.js`、`src/noyra/web/admin.html`、runtime log API 文档。

**实现：**

1. 管理台添加活动日志导航和五个 tab：运行事件、生命周期、模型与搜索、行为执行、管理审计。
2. 复用已有 bounded runtime log API，按游标分页；禁止前端一次拉全历史。
3. 支持时间范围、类别、状态、失败/等待筛选、暂停刷新和手动刷新。
4. 每条显示本地时间、原始 UTC、来源、结果、reason code 和关联记录入口；不显示 prompt、secret 和自由文本私密理由。
5. 增加 soak collector：每 5 分钟记录 live/ready、NRestarts、内存、磁盘、DB/backup size、Provider 状态和最近 retention。

**测试：**更新管理 UI contract、runtime log pagination、redaction 和权限测试；新增 7 天采集器的时间推进 fixture。

## 6. 第二台单系统盘服务器实施顺序

代码阶段 A/B/C 通过后，才在第二台服务器执行以下顺序。每一步失败都停止，不跳到下一步：

1. 只读记录系统版本、磁盘布局、空闲空间和 SSH 会话信息。
2. 使用新 release 的安装器执行 dry-run，确认 storage mode 为 `system_disk`，确认预计备份/升级空间充足。
3. 执行安装；验证 `systemctl is-active noyra`、`/health/live`、`/health/ready`、数据库可写和 upgrade 目录 owner/mode。
4. 通过管理页面配置一个已知可用的 Provider，先执行连接/模型测试；确认成功记录和错误分类。
5. 分别配置一个不匹配模型和错误 token，只验证页面原因码、group/key 状态和预算不被无界消耗。
6. 配置支持 reasoning 的模型，验证低/中/高/极高映射；再配置不支持模型，确认强度控件隐藏且请求不带未知字段。
7. 生成并验证备份；只在临时目标执行已完成过的恢复异常门禁回归，不重复破坏生产数据。
8. 提交一个离线升级请求；验证备份、schema marker、readiness、release pointer 和回滚状态。
9. 重启服务器后验证 service、recover、upgrade.path 和 health；确认不再出现 mountpoint condition skip。
10. 运行至少 7 天 soak；每天记录一次摘要，出现 degraded 时保存前后 15 分钟日志和状态快照。

建议验收命令：

```bash
systemctl show noyra noyra-upgrade-recover.service --no-pager \
  -p LoadState -p ActiveState -p SubState -p Result -p NRestarts
findmnt -T /var/lib/noyra
findmnt -T /var/backups/noyra
curl --fail http://127.0.0.1:8765/health/live
curl --fail http://127.0.0.1:8765/health/ready
stat -Lc '%A %a %U:%G %n' \
  /var/lib/noyra /var/lib/noyra/upgrade \
  /var/lib/noyra/upgrade/requests /var/lib/noyra/upgrade/processing
```

## 7. 第一台服务器发布步骤

第一台服务器当前服务曾因系统盘条件被跳过，不能直接作为修复验证的唯一环境。发布必须按以下顺序：

1. 在第二台单系统盘服务器完成 G1-G8；保存 release SHA 和验收报告。
2. 在第一台服务器只读确认磁盘余量、备份可恢复、当前数据库 schema 和 provider 配置；不清理未验证旧备份。
3. 生成新的加密备份并验证 restore marker；备份验证失败时停止。
4. 使用新 release 执行升级；安装器先准备 system-disk layout，再停止旧服务。
5. 新服务通过 `/health/live`、`/health/ready` 和 operator readiness 后才切换 current。
6. 检查 `noyra-upgrade-recover.service` 为成功或明确 skipped，不得为 failed。
7. 检查 provider 资源状态和 reasoning 配置；不要把历史 404 记录误判成新配置失败。
8. 观察至少 24 小时后再执行已验证备份 GC；GC 仅使用 preview plan 中列出的文件。
9. 任一 readiness、schema、权限或 provider 配置合同失败，恢复 previous release，不在生产上临时改权限或手动删库。

## 8. 发布报告和停止条件

每个阶段结束必须提交一份短报告，包含：目标 SHA、测试命令和结果、数据库 schema/DDL 指纹、服务器 storage mode、health 摘要、失败原因、备份文件名和回滚指针。

出现以下任一情况必须停止发布：

- 系统盘模式仍被 mountpoint condition 阻止；
- 服务账户可以修改 root-owned upgrade 控制根；
- schema marker 与结构指纹不一致；
- Provider 测试失败却显示 active/healthy；
- unknown 外部结果自动重发；
- anonymous health 暴露 subject、volume、key fingerprint 或 credential 信息；
- 备份验证标记与实际 SHA 不一致；
- retention 删除了受保护引用或 GC 没有可读 plan；
- rollback 后旧 release 未通过 readiness。

## 9. 最终执行顺序

```text
A1 存储 API
 -> A2 systemd 条件
 -> A3 安装器权限
 -> A4 本地合同测试
 -> B1 recover-only
 -> B2 schema/rollback
 -> C1 Provider 错误
 -> C2 health projection
 -> C3 reasoning
 -> C4 health reason code
 -> D1 credentials
 -> D2 setup modes
 -> D3 migration approval
 -> E1 backup GC
 -> E2 retention
 -> E3 activity log/soak
 -> 第二台服务器 G1-G8
 -> 第一台服务器灰度升级
 -> 7 天长 soak
 -> 发布
```

已完成的真实门禁不重复执行；新测试只覆盖修复后的行为、回归风险和之前没有证据的边界。
