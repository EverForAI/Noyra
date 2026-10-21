# 第四轮审计修复记录（2026-10-19）

本轮按 `docs/audit/2026-10-18-fourth-readonly-audit.md` 的顺序修复，P3-03 按用户要求暂缓。工作区仍未提交或推送。

| 审计项 | 结果 | 根因修复 |
|---|---|---|
| P1-01 preview profile 回归 | 已关闭 | preview 测试显式选择 `runtime_only`；`ServiceSettings` 直接构造也按 profile 给 production 默认 `recovery_capable`。 |
| P2-01 orphan marker 副作用 | 已关闭 | resume compatibility 改为纯读；只有 cursor 已 durable 推进后才清理 marker，并保留目录 fsync。 |
| P2-02 大计划 JSON cursor | 已关闭 | action manifest 流式写入受保护 sidecar 文件；SQLite cursor 只保存 digest、count、index；每批流式读取并校验完整 manifest。 |
| P2-03 capability provisioning | 已关闭 | 增加受保护 capability profile 文件导入，绑定 subject、endpoint、model、revision、digest 和 expiry；启动不探测 provider。 |
| P2-04 同 label 环境更新 | 已关闭 | 环境同步现在比较完整 route、预算、limits、pricing 和 key fingerprints；变化时 fail closed，不静默保留旧 durable route。 |
| P2-05 backup root 分叉 | 已关闭 | storage-check、setup、service 和 CLI 默认都读取 `NOYRA_BACKUP_ROOT`；setup 还从 env 文件解析同一路径。 |
| P2-06 production 直接构造默认 | 已关闭 | `ServiceSettings` validator 统一 production 的 recovery-capable 默认。 |
| P3-01 soak rotation | 已关闭 | append/rotation 共享锁，加入 fd/inode 重核验、atomic replace 和 POSIX 目录 fsync。 |
| P3-02 signer 错误状态 | 已关闭 | `SignerError.code` 成为状态码依据；HTTP 返回稳定 code，未知异常返回 500 和无敏感内容 request id；客户端识别 legacy receipt 与 nonce/broadcast 冲突。 |
| P3-03 mount/open TOCTOU | 暂缓 | 未修改。 |

验证结果：

- `ruff check .`：通过；
- `ruff format --check .`：683 个文件通过；
- `python -m mypy --python-version 3.12 src tests`：通过；
- `python -m compileall -q src scripts`：通过；
- 专项回归：`203 passed, 1 skipped`；
- `pytest --lf -q`：通过；
- 完整 Windows pytest：`2696 passed, 59 skipped, 288 subtests passed, 1 failed`。唯一失败是长时间全量运行中的 `test_approved_source_task_runs_real_bundle_cli_bridge_and_root_activation[True]`，同一测试单独运行、整文件运行（33 passed, 1 skipped）和 `pytest --lf` 均通过，判定为测试环境时序抖动，尚未宣称跨平台门禁完全通过。

当前剩余风险是该迁移激活测试在完整长运行下的偶发超时，以及 P3-03 的 mount/open 原子性边界。Linux root/systemd、真实 provider/RPC、Docker、Python 3.11 矩阵和跨机恢复仍需 CI 或目标 Ubuntu 验证。
