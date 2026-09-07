# Noyra 全面审计修复闭环

审计基线：`925f1cf7bbe60aeb624136300db77a143c5ec3d9`  
修复分支：`codex/candidate-20260822`  
原始审计：`2026-09-05-comprehensive-audit.md`

## 2026-09-06 状态更正与延期决定

2026-09-07：用户已授权修复以下两项，后续实现、合同与验证记录见
`2026-09-07-p1-remediation.md`。以下延期决定保留为历史记录，不能用于判定新提交的修复状态。

用户决定暂不修复文件目录替换竞态和代币手续费准入，先规划公开研究预览。
**延期不代表风险消失或问题关闭。以下更正优先于原关闭矩阵中的“已修复”状态。**

- AUD-12：重新打开。Windows 路径检查与实际 open/replace 之间仍有目录替换窗口；
  POSIX 已有句柄保护也不能直接证明所有祖先目录竞态均已关闭。
  风险 P1，修复风险高。首发建议不授予文件读写能力，不在不可信进程可修改的目录使用文件工具。
- AUD-05（关联 AUD-04/AUD-14/AUD-19）：手续费准入部分重新打开。
  token 执行缺少原生手续费资产或快照时仍可跳过检查，且没有完整扣除原生支付订单的占用。
  风险 P1，修复风险高。需核对原生费用单位、余额新鲜度、资金预留和 retry 入口。
  “所有 reserved token 订单应何时预留费用”属于后续设计决策，不将未实现的方案写作既定合同。
  首发建议不配置 signer、不启用自动支付、不使用真实资金。

以上为静态复核发现，尚未增加本轮故障注入测试。`7fdcd8f` 的既有门禁通过不能证明这些边界安全。
本次仅记录状态，不修改运行配置；建议的禁用边界需要在发布准备中实际核验。
公开源码与研究预览不构成生产验收，正式稳定发布仍需关闭上述问题并完成外部验收。

发布规划：`../superpowers/plans/2026-09-06-research-preview-release.md`。

## 修复原则

- 原始审计报告和缺陷复现探针保持不变。探针中的“通过”代表缺陷可复现，不作为安全门禁。
- 支付默认仍关闭；真实 signer、真实资金和真实 testnet 不在本地修复测试中启用。
- 未知广播只允许查询和追加事故证据。任何重新签名必须由显式 operator retry 发起，并复用原 nonce/交易意图。
- 历史迁移不把无法证明的时间或回执字段静默重签为可信；旧库升级必须使用指纹绑定的人工批准。

## 缺陷关闭矩阵

| 条目 | 修复状态 | 关键实现/回归 |
| --- | --- | --- |
| AUD-01 | 已修复 | 既有订单确认和执行重新检查当前 mode、allowlist、预算、余额与异常策略 |
| AUD-02 | 已修复 | unknown retry 检查 emergency pause；工作流只轮询 unknown，不隐式重发 |
| AUD-03 | 已修复 | signer 外发位于 runtime lease `external_side_effect_scope` 内 |
| AUD-04/05 | 已修复 | 余额绑定唯一 spending 地址，并扣除在途订单；`min_balance=0` 也不能超额预留 |
| AUD-06 | 已修复 | `authorized_at` 持久化并用于日/月预算归属 |
| AUD-07 | 已修复 | 异常快照会阻断新预留和执行 |
| AUD-08 | 已修复 | 回执要求入块、区块哈希、确认数；token 终态要求 effect hash |
| AUD-09 | 已修复 | nonce 由 signer pending authority 提供；仅无 tx 的 pre-broadcast failure 可复用 nonce |
| AUD-10 | 已修复 | 订单/执行时间纳入 state hash；schema 61 迁移拒绝未授权旧时间历史 |
| AUD-11/16 | 已修复 | GET/list 纯读；过期维护使用显式、有界的 operator tick |
| AUD-12/18 | 已修复 | no-follow、目录身份重检和 max+1 有界句柄读取；写入使用临时文件原子替换 |
| AUD-13 | 已修复 | 金额聚合改为 Python 任意精度累加，避免 SQLite int64 SUM 溢出 |
| AUD-14 | 已修复 | 账本余额按 network/asset/account 分维度返回和过滤 |
| AUD-15 | 已修复 | 队列 SQL 在 LIMIT 前过滤可执行状态 |
| AUD-17 | 已修复 | 直接内核构造在整个初始化期间持有探测锁，异常路径释放 |
| AUD-19 | 已修复 | 正常执行要求 signer fee quote 或显式受控覆盖，保留 gas/费率硬上限 |
| AUD-20 | 已修复 | release workflow 依赖同 SHA 的 smoke、pressure、full gate，并始终保留证据 |
| AUD-21 | 已修复 | README 状态更新到 schema 62，并链接最新冻结/审计边界 |

## 尚未由本地代码证明的交付缺口

### GAP-01 自主钱包闭环

已提供显式、受界限的运行时自动工作流：从开放的自主项目协助请求创建赏金；默认不自动发布，只有明确环境开关才允许自动发布；发布和付款仍经过普通 moderation、策略、预算、signer 和运行时 lease。它不把人类消息直接变成任务，也不绕过人工证据审核。

完整产品关闭仍需要受控部署验收：配置独立 signer 后，验证需求、发布、入站提交、审核、预留、转账、终局、暂停和重启恢复可以跨进程持久续跑且不重复花费。

### GAP-02 外部 testnet/KMS/备份/长期 soak

本地测试不能证明真实链的 gas、nonce、mempool、重组、signer 故障、密钥保管、备份介质恢复或 24 小时遥测。发布 job 因此要求同 SHA 的 `external-gates.json`，且拒绝缺失、失败或超过 72 小时的证据。在该证据产生前不得发布或接入真实资金。

### GAP-03 signer 部署认证与跨服务语义

代码支持 HTTPS、bearer token、固定转账 envelope、nonce/fee/receipt 查询和明确的 unknown 分类；部署仍必须落实 mTLS 或等效请求认证、最小权限账户、稳定幂等日志、密钥轮换与审计，并验证代理 4xx/5xx/超时分别代表拒绝还是未知，不能只根据 HTTP 状态码猜测是否广播。

## 验收要求

在提交修复后，以干净工作树运行：

```powershell
python scripts/audit-wallet-stage4b4.py --scope targeted --profile smoke
python scripts/audit-wallet-stage4b4.py --scope targeted --profile pressure
python scripts/audit-wallet-stage4b4.py --scope full --profile soak
```

每次运行的 `run.json`、命令日志和压力指标必须保留在对应 SHA 的 `artifacts/release/stage4b4/<sha>/` 目录。任何失败都保留证据并阻止发布。
