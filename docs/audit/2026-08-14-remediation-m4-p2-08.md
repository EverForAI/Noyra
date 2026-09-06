# Noyra P2-08 修复复核

日期：2026-08-14  
范围：自主项目 collaboration request 只写 web mailbox，没有进入配置的外部通讯通道。

## 修复内容

项目执行器继续创建 `web`/`help_request` 交互，保留主体先表达求助意图的语义。
`DeliveryDispatcher` 现在会将这类待发送交互路由到一个按
`channel, label` 排序选出的 active transport，并创建现有的 delivery 记录。外部
发送仍然经过 queued/sending/delivered/failed/unknown 状态机、幂等键和重试上限。

transport 的 `settings` 可提供 `recipient`、`chat_id`、`target` 或 `to`；Telegram 和
SMTP 邮件在缺少收件人时明确失败，避免把 `web-user` 当成真实地址。没有 active
transport 时不会创建 delivery，web mailbox 仍是可靠 fallback。

## 验证

* 新增 web help request 到 Feishu webhook 的投递回归测试；
* transport 专项测试、Ruff、Mypy、全量测试和 Ubuntu/部署专项审计在提交前执行。
