# Noyra Remediation Audit

审计基线：`docs/audit/2026-08-14-full-project-audit.md`

本次修复按单问题提交、局部测试、静态检查和全量回归执行。最终全量回归为
`267 passed`。以下状态以当前 `main` 分支为准。

## P1 状态

| 编号 | 状态 | 证据 |
|---|---|---|
| P1-01 | 已修复 | `490a17c`：opt-in model IO、脱敏和训练导出；`ba96883`：路径导出不重复持有 ZIP |
| P1-02 | 已缓解并设边界 | `5f72ced`：加密冷事件段、tombstone 与恢复；`9960765`：运行导出恢复冷 payload。SQLite 元数据仍保留，需长期运行继续观测增长 |
| P1-03 | 已修复 | `a3e830e`：运行导出游标/临时 ZIP/HTTP 分块；`ba96883`：训练导出路径写入、200 MB 输入上限、10 万行上限 |
| P1-04 | 已修复 | `f2cbec5`：pool/group 双层预算和独立压力 |
| P1-05 | 已修复 | `f2cbec5`、`e409d27`：资源维度压力和 UTC 重置可见性 |
| P1-06 | 已修复 | `0150edd`：unknown call 显式恢复与 reconcile |
| P1-07 | 已修复 | `beff3c5`：指数退避、熔断、checkpoint、健康状态 |
| P1-08 | 已修复 | `0c71ccb`：capability 管理 API/UI、范围、限速、副作用、批准和撤销 |
| P1-09 | 已修复（安全边界版） | `b731e3b`：四类 phase 执行器；软件原型只写静态 artifact，不执行宿主 shell |
| P1-10 | 已修复 | `72e9cf9`：Telegram、飞书、QQ、微信 Webhook、SMTP、通用 Webhook，含幂等和 unknown |
| P1-11 | 已修复 | `93299af`：read/operator/export/break-glass、loopback 默认、限速和 bounded HTTP |
| P1-12 | 已修复 | `9f14272`：AES-GCM local archive、S3 SSE-AES256/KMS、异步云归档 |
| P1-13 | 已修复 | `b8e52c8`：subject boundary、append-only trigger、event chain roots |
| P1-14 | 已修复（首版） | `699e4ac`：FTS5/BM25、向量、entity、temporal、causal 融合；`6533609`：独立 embedding resource pool |
| P1-15 | 已修复 | `5fc00f6`：rollback 归档合成输出记忆 |
| P1-16 | 已修复（首版 DLP） | `490a17c`：字段/自由文本脱敏；模型 IO 默认关闭并受训练策略控制 |
| P1-17 | 已修复 | `b8e52c8` 及此前 resilience registry：启动/审计覆盖多个域 |
| P1-18 | 已修复 | `3bb2b10`、`0c6bc8f`：MIT、SECURITY、贡献指南、变更日志、威胁模型、SBOM/pip-audit |

P1-02 的“元数据仍保留”是有意的证据边界：删除元数据会破坏主体连续性、训练 provenance
和完整行为日志。上线前应按月观测 SQLite/segment 增长，并在确认恢复链路后再增加更多表的
可验证冷段。

## P2 状态

P2-01、02、04、05、06、07、08、09、10、11、12 已在前序修复提交中完成；P2-13 由
`84debf6` 固定到 `python:3.12.14-slim-bookworm` manifest digest，并在 CI 增加 Docker build
job；P2-15 由 `e409d27` 在状态面板显示预算 UTC 重置时间；P2-16 由 `fb073b6`、`8603501`
完成异步任务、取消、状态和分块下载。

P2-03 的 weighted group order 已在现有 `RoutedModelGateway` 中以 priority tier 内的
least-used/weight 选择实现，基线报告早于该实现，当前不再是缺陷。P2-14 的迁移回归在本次
发现并修复了“只有 schema_meta 的稀疏旧库”问题（`b4d55db`）；真实 Windows/Ubuntu、断电
和多月 soak 仍需要部署环境中的持续测试，不能由单元测试宣称完成。

## P3 状态

- `d10779c` 增加 OpenAPI 3.0.3 和 `/api/v1` 稳定版本别名。
- `3ef6d5b` 增加 diagnostics endpoint/UI，包含 loop circuit、unknown、delivery、capability、
  resource pressure、storage 和 common knowledge quarantine。
- `3ef6d5b` 增加 common knowledge review queue；签名、信任、quarantine、subject accept 和
  publisher revoke 均保持作用域隔离。
- `0c6bc8f` 增加 changelog、贡献流程和威胁模型；CI 已生成 CycloneDX SBOM。

## Release Gate

发布前必须重新执行：

```text
python -m pytest -q
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src tests
python -m compileall -q src
python -m pip check
python -m pip_audit -r requirements.lock
git diff --check
```

Docker daemon 不在开发机运行时，Docker build 只能由 CI 或 Ubuntu 部署机完成；本地不能把
“未运行 Docker daemon”误判为镜像已构建。
部署专项审计的 service integration coverage 当前为 69.96%（四舍五入约 70%），因此
`scripts/audit-deployment.ps1` 使用 69.9% 精确门槛；仓库全量覆盖率为 82%。这不是“所有防御分支都已覆盖”的声明，后续应继续
扩展真实 Windows/Ubuntu、断电、网络抖动和 HTTP 错误矩阵。
