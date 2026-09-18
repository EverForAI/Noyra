# 本地钱包运行指南

Noyra 的钱包模式由 `NOYRA_WALLET_MODE` 选择：`disabled`（默认）、
`local`（进程内加载加密 Ethereum V3 keystore）或 `external`（现有的
HTTPS signer）。旧部署若只设置 `NOYRA_WALLET_SIGNER_ENDPOINT` 和
`NOYRA_WALLET_SIGNER_ID`，仍按 external 兼容处理；显式 `disabled` 会覆盖
遗留变量。

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

