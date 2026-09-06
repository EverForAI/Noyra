# Noyra P2-07 修复复核

日期：2026-08-14  
范围：通讯 transport 只校验字面 IP，hostname 可能解析到内网或发生 DNS rebinding。

## 修复内容

新增 `PublicDNSAsyncHTTPTransport`。每次 TCP 连接前解析 hostname，拒绝 loopback、
private、link-local、保留地址和其他非 global 地址；连接使用该次解析得到的具体 IP，
而 TLS SNI/HTTP Host 仍使用原始 hostname。这样不会在校验后再次由系统 DNS 选择另一
地址。HTTP 客户端继续关闭环境代理并禁止重定向。

配置阶段的 endpoint 校验仍保留，运行时连接又增加一次解析级防护。没有公有地址或
解析失败时投递进入现有 failed 状态，不会访问内网目标。

## 验证

* 新增私有地址拒绝测试；
* 既有 transport mock 投递测试继续通过；
* 全量测试、静态检查、依赖审计和 Ubuntu/部署专项审计在提交前执行。
