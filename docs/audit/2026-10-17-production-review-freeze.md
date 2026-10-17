# Noyra 生产复盘开工前冻结记录

> 冻结日期：2026-10-17
> 冻结用途：为生产复盘修复工作建立可追溯的代码、文档和验收基线。
> 本记录不包含任何 API key、token、密码、钱包私钥、完整请求、私密内容或生产数据库副本。

## 代码基线

冻结前工作区信息：

```text
branch: codex/admin-audit-compatibility-20261014
HEAD: ea195fe526b2cb89924711c2b6c41a0f80bdd686
origin/main: 9845807aeb6439e0aafb273359032b1477a31df4
origin branch: origin/codex/admin-audit-compatibility-20261014
```

冻结前工作区无已修改或已暂存文件；仅有以下两份复盘规划处于未跟踪状态，连同本记录纳入本次文档提交：

- `docs/superpowers/plans/2026-10-17-production-review-remediation.md`
- `docs/superpowers/plans/2026-10-17-production-review-implementation-steps.md`
- `docs/audit/2026-10-17-production-review-freeze.md`

当前分支相对于 `origin/main` 的已有提交将整体纳入本次主线合并，包含 self-hosted signer、凭据文件边界、安装器组件恢复和 signer recipient checksum 修复。它们不是本次新实现，但已经是待合并基线的一部分。

## 生产证据基线

第一台服务器 `ser799545842233` 的复盘基线：

- 当前 release source SHA：`9845807aeb6439e0aafb273359032b1477a31df4`。
- `/var/lib/noyra` 和 `/var/backups/noyra` 位于系统盘，没有独立挂载。
- `noyra.service` 仍有 `ConditionPathIsMountPoint=/var/lib/noyra`，重启后曾被 systemd 跳过。
- `noyra-upgrade-recover.service` 曾因 upgrade 控制目录不存在而失败。
- 历史 schema 71→80 不一致已定位，旧版本恢复路径已有专门证据和回归方向。
- Provider 历史包含 404、429、502、unknown 和 budget exhausted；Qwen 有少量成功调用。
- `/health/live` 曾为 200，而 `/health` 或 `/health/ready` 出现 503；`BrokenPipeError` 是探针断开噪声。
- 数据库 retention 已运行但尚未跨过 90 天 cutoff；备份目录约 736 MB，GC 预览只计划删除一个已验证旧备份。

第二台服务器的用途冻结为：无独立数据盘的全新安装、升级、Provider、备份恢复和长 soak 验收。它不用于保存生产数据，也不改变第一台服务器的现有状态。

## 已完成门禁，不重复执行

以下真实测试已经完成并记录为通过：错误 keyring、篡改备份、非空恢复目标、残留清理、HTTPS 401/200/404、signer 身份、pending nonce、Sepolia 转账、receipt confirmation、nonce 冲突、余额不足、费用上限不足、release SHA、服务 readiness 和 signer 重启。

后续只对修复后的行为增加回归测试和必要的第二台服务器验收，不在第一台服务器重复制造这些故障。

## 开工边界

在阶段 A 的代码和测试通过前：

1. 不修改第一台服务器的 systemd、配置、数据库、挂载和 Provider。
2. 不对第一台服务器执行 `gc --apply`，不手动删除未验证旧备份。
3. 不在生产数据库上注入 schema 故障，不重置生产 cooldown，不重发 unknown 外部请求。
4. 不把独立复核要求作为 staging 或单人测试的阻塞条件；高风险生产动作仍由策略决定是否启用双人复核。
5. 所有新实现先在仓库测试、临时数据库、临时 release 或第二台测试机完成。

## 冻结后的下一步

1. 提交并推送本冻结记录和两份规划。
2. 将当前分支已有 signer/部署提交与文档一起 fast-forward 合并到 `main`。
3. 从阶段 A 的 storage mode 和 system-disk contract tests 开始实现。
4. 阶段 A 通过后才进入升级恢复、Provider、health、凭据、retention 和长 soak。

## 复核命令

冻结提交后应满足：

```bash
git status --short --branch
git log --oneline --decorate -1
git diff --check HEAD^ HEAD
git merge-base --is-ancestor origin/main HEAD
```

生产复盘修复不以“文档已提交”作为功能完成标志；必须依次通过实施步骤文档中的 G1-G11 门禁，并在第二台单系统盘服务器上完成 G1-G8 后，才评估第一台服务器升级。
