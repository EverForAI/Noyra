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

收款地址只需是有效地址，不要求预先出现在网络/资产 allowlist。网络和资产仍
必须已注册且有效；金额、余额、nonce、Gas、单笔/日/月限额、频率、审计、账本
幂等和紧急暂停继续生效。数据库中的旧 allowlist 字段保留用于兼容和审计，
不会单独拒绝合法收款人。
