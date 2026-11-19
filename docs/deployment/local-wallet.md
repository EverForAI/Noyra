# 本地钱包运行指南

Noyra 的钱包模式由 `NOYRA_WALLET_MODE` 选择：`disabled`（默认）、
`local`（进程内加载加密 Ethereum V3 keystore）或 `external`（现有的
HTTPS signer）。旧部署若只设置 `NOYRA_WALLET_SIGNER_ENDPOINT` 和
`NOYRA_WALLET_SIGNER_ID`，仍按 external 兼容处理；显式 `disabled` 会覆盖
遗留变量。

## External signer 的入口和本机监听

`NOYRA_WALLET_SIGNER_ENDPOINT` 是 Noyra 客户端访问的 HTTPS 入口，必须保持
无内嵌用户名/密码、无 query/fragment。客户端的生产传输层还会校验 DNS 解析地址，
拒绝私网、回环等非公网目标；不会跟随重定向到其他地址。

独立的 `noyra-signer.service` 只监听 `http://127.0.0.1:8780`。它是同机反向代理
或隧道的 origin 地址，`SignerHTTPServer.url` 描述的也是这个本机监听。
二者是不同网络边界，并不要求使用同一个 URL：

```text
Noyra external 客户端
  -> https://<受信任证书对应的签名入口主机>
  -> 受运维配置的反向代理/隧道
  -> http://127.0.0.1:8780
  -> 独立签名进程
```

即使签名进程与 Noyra 在同一台主机，也不要把 origin 的 HTTP URL 填入
`NOYRA_WALLET_SIGNER_ENDPOINT`，或把它转换成 `https://127.0.0.1`；两者均会被
external 校验拒绝。需要直接由 Noyra 进程持有 keystore 时应单独评估 `local`
钱包模式，它与独立签名进程具有不同的密钥隔离边界，不能混用配置字段。

代理应保留协议路径、JSON 正文和 `Authorization: Bearer ...` 头，不应返回要求客户端
跳转的入口。身份校验和转账鉴权仍在签名进程执行。证书校验、公网 DNS 限制和 bearer
鉴权不能通过关闭校验、添加 HTTP 回环例外或打印凭据来解决配置问题。

代码提供签名进程和客户端协议，并不自动配置公网 DNS、TLS 证书、代理或隧道。
启用前分别检查 origin 的本机可达性与 HTTPS 入口的证书/路由/鉴权；检查记录只保留
成功/失败类别，不记录 bearer token、钱包密码、RPC 凭据或完整请求。设置 systemd
凭据及从旧单元升级时参见[签名凭据迁移说明](signer-credentials.md)。

迁移优先使用 `external` signer。可以同时设置
`NOYRA_WALLET_SIGNER_ADDRESS`，让进程在签名前拒绝与 signer 绑定地址不一致的
交易；该变量只保存公开地址，不是密钥。迁移不会把 signer bearer token、私钥或
密码写入迁移清单。`local` 钱包迁移必须在策略中显式开启，并使用绑定到任务和
钱包地址的一次性审批；源密钥在迁移提交前保留，失败时可回滚。

## 创建 keystore

请在受保护的运维主机上运行交互式命令。密码不会出现在命令行、环境变量或
日志中：

```powershell
python -m noyra wallet-setup --path C:\noyra\secrets\wallet.json
```

将密码文件放在只有服务账户可读的目录，并通过 systemd credentials、容器 secret
或等价的受保护挂载提供。`NOYRA_WALLET_PASSWORD_FILE` 只填写文件路径；不要
把密码写入 `.env`。`NOYRA_WALLET_RPC_URLS_JSON` 是链 ID 到 HTTPS origin 的
JSON 映射，例如 `{"1":"https://rpc.example"}`。不接受明文 HTTP、URL 中的
凭据、查询参数或重复链键。

本地模式把私钥解密到 Noyra 进程内存。进程被攻破时，攻击者可能取得密钥；
external 模式把密钥留在隔离 signer 服务，适合更高隔离要求。请保存加密
keystore 的离线备份并演练恢复；密码丢失无法恢复私钥。

## 经济边界

支付策略由经鉴权的管理入口配置；奖励订单从持久策略取得支付模式，
完成任务的模型不能在提交内容中把订单自行切换成 `automatic`。
自动付款表示在既有授权额度内无需逐笔人工确认的任务奖励。

| 策略约束 | `automatic` | `conditional_confirmation` |
| --- | --- | --- |
| 逐笔确认 | 不要求 | 预留前要求确认 |
| 单笔金额 `per_order_limit` | 必须大于 0，达到上限允许，超过拒绝 | 非零时执行，0 表示不设此上限 |
| 每日金额 `daily_limit` | 必须大于 0，达到上限允许，超过拒绝 | 非零时执行，0 表示不设此上限 |
| 月金额 `monthly_limit` | 不执行 | 非零时执行 |
| 日/月笔数 `daily_order_limit` / `monthly_order_limit` | 不执行 | 非零时执行 |
| 历史 `automatic_max_amount` | 仅保留兼容，不作为第二个隐含上限 | 不作为限额 |
| 历史余额阈值、观测时效、异常条件 | 不作为自动预留的附加策略条件 | 按配置执行 |

上述自动限额语义延续既定产品规则；保存非零月金额或笔数参数不会让它们
成为自动付款的限制。管理界面的自动付款开关、当前模式、紧急暂停和生产
激活证据仍控制自动执行；`disabled` 拒绝新预留及新发送。已预留订单在签名
发送前会重新校验当前策略，关闭自动付款、降额或暂停均可阻止尚未发送的
自动订单；已发出的交易仍须按未知结果/回执流程处理。

**不执行某项限额不等于不计入统计。** 日/月汇总按主体、网络和资产分别计算，
包含所有支付模式下处于 `reserved`、`signing`、`broadcast`、`unknown`、
`confirmed` 的订单。周期按预留授权时间及 UTC 日/月边界确定；旧记录缺少
授权时间时使用更新时间、创建时间依次回退。切换支付模式不清空这段历史，
先前自动订单也会占用后续条件确认订单的日/月金额及笔数额度。等待确认
不占用额度，取消或失败后不再占用；未知结果不释放预算。

两种模式都要求网络/资产已注册且有效、唯一有效支出地址、有效金额及收款
地址，并保留审计和账本幂等。已有余额观测仍约束可预留金额，不能用
`min_balance=0` 忽略已知余额；执行层继续检查 nonce、Gas 和可支付费用。
历史 `allowed_network_ids` / `allowed_asset_ids` 仅保留兼容和审计；若显式启用
收款地址白名单，两个模式都必须满足该名单。
