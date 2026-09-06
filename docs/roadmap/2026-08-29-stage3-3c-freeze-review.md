# Stage 3 完整封板验收报告

日期：2026-08-30
分支：`codex/candidate-20260822`
Stage 2 基线：`noyra-freeze-stage2-20260824` / `b1df850`
已验证代码快照：`95c0d61 style: format stage3 changes`
冻结标签：`noyra-freeze-stage3-20260830`

## 1. 封板结论

Stage 3 本地代码门禁通过，可以冻结并进入 Stage 4-A 钱包/代币只读登记模块。

本结论仅表示 Stage 3 代码、合同、持久化和本地故障注入门禁通过。它不代表生产发布认证，也不替代真实 Ubuntu 主机、第三方通讯账户、72 小时 soak、真实断电/网络分区或生产恢复演练。这些仍属于后续发布门禁。

## 2. 冻结范围

Stage 3 从 Stage 2 冻结提交 `b1df850` 之后开始，包括：

- 原生通讯入站校验、绑定、去重、回调目标和投递账本。
- 公开帖子审核、容量、频率、完整性和管理界面。
- 认知资源、密钥生命周期、路由历史、预算、撤销和管理端控制。
- 能力授权与范围校验、公开 HTTPS 读取策略及操作员撤销持久性。
- 认知调度互斥、逻辑/物理模型调用预算折叠。
- 资源、完整性、存储、归档、诊断读取的有界化和流式化。
- 数据库初始化原子性、进程锁冲突分类及 Windows 验收稳定化。
- OpenAPI、管理台合同、大整数精确预算字段及移动端界面。

3-C 工作树起点、分步差异和当时的验证记录见 `docs/roadmap/2026-08-29-stage3-working-tree-baseline.md`。

## 3. 残留差异收口

- `.env` 示例和 Ubuntu 文档已与公开 HTTPS 策略对齐。
- delivery/self-modification 列表的有界 `fetchmany` 差异已提交。
- Windows 锁冲突验收和 public-post 测试类型收口已提交。
- Ruff 格式门禁首次发现 23 个 Stage 3 文件未格式化；已仅做机械格式化，并在该提交后重跑全量回归。

## 4. 3-D 专项验收

| 门禁 | 结果 |
|---|---:|
| 长历史、资源路由与完整性 | **171 passed, 172 subtests passed** |
| 存储压力、清理与生命周期 | **37 passed** |
| 归档恢复、租约回收、重试与完整性 | **53 passed** |
| 原生入站和传输回调恢复 | **64 passed** |
| 能力撤销、认知重启和预算边界 | **33 passed, 14 subtests passed** |
| 服务和管理 API | **38 passed** |
| at-rest/锁故障、认知和公开帖子收口 | **71 passed, 6 skipped, 7 subtests passed** |
| M41 确定性 large 历史 | **284 passed, 7 subtests passed** |

M41 `large` 使用 10,000 条确定性历史。100,000 条 `soak` 证据已存在于项目 M41 验证记录中；本次冻结不把它伪装成新的多日生产 soak。

## 5. 最终全量与静态门禁

机械格式化提交后的最终结果：

| 门禁 | 结果 |
|---|---:|
| `python -m pytest -q -p no:cacheprovider` | **1055 passed, 8 skipped, 251 subtests passed** |
| `ruff check .` | **passed** |
| `ruff format --check .` | **337 files formatted** |
| `mypy src tests` | **232 source files, no issues** |
| `compileall -q src tests` | **passed** |
| `pip check` | **no broken requirements** |
| `pip-audit --no-deps --disable-pip -r requirements.lock` | **no known vulnerabilities** |
| `node --check` for `src/noyra/web/*.js` | **passed** |
| `git diff --check` | **passed** |

8 个 skip 均是 Windows 上不可执行的 POSIX symlink/文件系统合同，不是未分类失败。

## 6. 冻结规则

- 使用不可移动、不覆盖的附注标签。
- Stage 4 必须从标签后的新提交开始。
- Stage 4-A 只允许链、资产、地址登记和只读余额边界，不包含私钥、签名或真实转账。
- 真实付款必须在后续通过签名器隔离、幂等账本、限额、Gas、异常恢复和小额测试网门禁。
