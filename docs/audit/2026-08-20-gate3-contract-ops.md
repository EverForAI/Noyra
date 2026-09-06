# Gate 3 合同与运维边界修复报告

日期：2026-08-20

分支：`codex/gate3-contract-ops`

基线：Gate 2 checkpoint `18cb666`

## 本轮范围

本轮只处理 P3-01、P2-09、P2-12 和 P2-15，保持 Gate 2 的状态、并发、归档和外部资源合同不变。

## 已修复

### P3-01：API 全局错误合同

- `service_contract.py` 增加共享 HTTP 边界错误集合：429、503，以及 JSON POST 的 411、413。
- 每条 versioned API route 暴露 `effective_responses`，将 handler 专属响应和共享边界响应合并，避免只验证 happy path。
- OpenAPI 增加 `x-global-error-responses`，明确这些状态由共享 HTTP 层继承，不再要求每个 operation 重复复制同一语义。
- 增加 `Retry-After`：429 为 60 秒，503 为 5 秒。
- API 合同测试反向检查 runtime route、OpenAPI 全局错误声明和 inherited response 集合。

### P2-09：健康检查 I/O 放大

- `/health` 的 readiness projection 增加可配置 TTL 缓存，默认 5 秒，避免未认证探针每次触发 SQLite quick check、目录扫描、secret cleanup、storage scan 和完整 operator projection。
- 新增 `/health/live`，只返回固定 liveness projection，不访问数据库、磁盘或外部 provider。
- 新增 `/health/ready`，只检查启动准入相关状态：生命周期、at-rest、integrity quarantine 和已记录的 cloud readiness。
- 深度运维诊断继续保留在认证的 `/api/v1/admin/health`，不把私密诊断暴露给 liveness 探针。

### P2-12：Bearer token 配置

- 启动时拒绝已知 placeholder、空 token 和重复 role token。
- 所有配置的 role token 必须互异，覆盖 read/operator/export/break-glass 和 legacy admin token。
- 非 loopback listener 至少需要一个 bearer token，避免显式开启非回环监听后意外匿名运行。
- 部署模板改为空 token，并提示生成随机值；桌面部署文档改为运行时生成 token，不再提供可直接复制的已知 bearer 值。

### P2-15：重启风暴与日志增长

- systemd 从 `Restart=always` 改为 `Restart=on-failure`，配置 `RestartPreventExitStatus=78`、启动速率上限和 journald 日志速率上限。
- 服务配置/依赖 profile 错误使用 `EX_CONFIG=78` 退出，永久配置错误不会被 supervisor 无限重启隐藏。
- Docker Compose 改为 `restart: on-failure:5`，并限制 json-file 日志为 10 MB x 3 文件。

## 验证

- Gate 3 专项测试：`6 passed`。
- P3-07 API 合同回归：`5 passed`。
- P3-06 operator controls、service、at-rest、provider security 组合：`77 passed, 1 skipped`。
- Ruff、Ruff format、strict mypy、compileall、git diff check：通过。

## 注意事项

- `/health` 保留原有 readiness 语义，只增加 TTL；现有客户端不会因默认路径语义改变而失效。
- `/health/live` 不代表 SQLite、at-rest 或 cloud ready；部署探针应使用 liveness 和 readiness 两个探针分别配置。
- `/health/ready` 使用启动后已验证的 cloud readiness 状态，不在每次探针请求中重新发起 S3 网络探测。
- systemd 的 `EX_CONFIG` 门禁只覆盖服务启动配置/依赖 profile 错误；运行期未知异常仍由 on-failure 策略处理并受启动速率限制。

## 后续补充：P2-13 Ubuntu 原子升级与回滚

- Ubuntu 安装改为 `releases/<id>` 独立 virtualenv，staging 全部验证成功后才发布。
- `current`/`previous` 使用同卷原子 symlink 替换；systemd 从 `current` 启动。
- installer 使用 root-only `flock`，升级前停服和加密冷备份，切换后要求
  `/health/ready`；失败会恢复原指针、profile 和原服务状态。
- `--rollback` 提供显式代码回滚；数据库迁移仍按 forward-only 边界处理，必要时由运维者
  恢复安装器保留的升级前备份。
- 详细实现和验证证据见 `2026-08-20-p2-13-ubuntu-atomic-upgrade.md`。

## 本轮未纳入

- P2-11 前端 request generation、AbortController、导出流式下载；
- P3-02 运维控制面 UI；
- P3-03/P3-04/P3-05/P3-08 最终发布验收证据。
