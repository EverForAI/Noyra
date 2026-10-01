# 管理台一键升级设计

## 目标

让管理员在 HTTPS 管理台中检查 Noyra 最新版本，并在确认后启动一次可断线、可审计、可回滚的后台升级。管理员不再需要通过 Xshell 手工执行 Git、安装器或 systemd 命令。

## 边界与安全约束

- 版本检查是只读操作；默认检查配置的 GitHub `main`，只返回提交 SHA、短 SHA、提交时间和脱敏的提交标题。
- 升级只能由已认证的管理会话或 operator/admin bearer token 发起，并继续要求现有 CSRF 保护。
- 每次只允许一个升级任务；重复点击返回当前任务，不会启动第二个安装器。
- HTTP 请求不执行 root 命令，也不等待安装完成。服务写入受保护的升级请求文件后，由 root-owned systemd runner 消费。
- runner 固定使用部署目录、固定脚本和固定参数；不接受网页传入的 shell 片段、路径、令牌或 API 密钥。
- 现有 `scripts/install-ubuntu.sh` 继续负责加密备份、安装锁、原子 release 指针切换、就绪检查和失败回滚。
- 状态文件和日志存放在 `/var/lib/noyra/upgrade`，root 写入、noyra 可读取经过脱敏的状态；不保存环境文件、密钥、完整命令行或 HTTP 响应正文。
- 服务重启或 runner 重启后，未完成任务标记为 `interrupted`，旧 release 保持可用；管理员可以重新检查并发起新的升级。

## API

- `GET /api/v1/admin/upgrade/check`：返回当前 release、远端 main 的 commit 信息以及 `update_available`。
- `GET /api/v1/admin/upgrade/status`：返回当前任务的状态、阶段、开始/结束时间、目标 SHA、release、最近脱敏日志行和错误码。
- `POST /api/v1/admin/upgrade`：接受 `{target_sha?, reason, idempotency_key}`。只允许已检查的远端 SHA 或当前 main；返回 `202` 和任务状态。任务执行由 systemd runner 完成。

错误码保持稳定：`upgrade_unavailable`、`upgrade_in_progress`、`upgrade_source_dirty`、`upgrade_target_invalid`、`upgrade_start_failed`、`upgrade_interrupted`。

## 管理台

总览新增“版本与升级”卡片，显示当前版本、最新版本、检查时间和升级状态。按钮分别是“检查最新版本”和“升级到最新版”；升级前显示目标 SHA 与提交时间并要求确认。任务进行中自动轮询状态，完成后显示新 release 和健康检查结果，失败时显示回滚状态与日志摘要。

## 失败处理

创建任务前拒绝脏工作区、缺少部署目录、远端不可达或已有任务。runner 失败时保留 installer 的回滚结果；HTTP 状态明确标识任务失败或中断。任何失败都不会删除旧 release、备份或审计记录。

## 验证

- 单元测试覆盖版本解析、脏工作区拒绝、并发幂等、状态持久化、敏感字段脱敏和 runner 命令固定化。
- HTTP 契约测试覆盖认证、CSRF、202/409/503 响应和版本字段。
- 管理台契约测试和 JavaScript 语法检查覆盖按钮、轮询和状态文案。
- shell 语法检查覆盖 runner 和安装器；现有安装器回滚测试必须继续通过。
