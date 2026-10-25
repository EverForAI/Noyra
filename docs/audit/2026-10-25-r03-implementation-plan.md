# R03 生产修复实施计划

日期：2026-10-25（北京时间）
基线：d471225（包含已提交的 Windows 原型验证，不包含生产 R03 修复）
分支：codex/fix-r03-recovery
范围：Windows 安装生命周期 signer 轮换/恢复事务；每个模块完成后原生审计并单独提交，不推送、不部署。

## 风险与不变量

原生缺陷 P2，修复风险高；支持入口若绕过权威状态，会破坏撤销保证，列为发布阻断。保留现有 detached CMS、Authenticode、清单/hash、路径和重解析点验证，生命周期互斥锁与 DataRoot 保留语义。实际 signer 不能由包内 metadata 自我授权。撤销在完整 PREPARED 权威记录成功持久发布时生效；其后只前滚或明确阻断，不恢复被撤销 signer。兼容 current/previous/trust 文件为投影，不反向覆盖 authority。遵守现有逐版本 verification receipt 与 -RequireSignature 合同，不把已签名模式错误地变成全局不可降级开关；显式允许的 unsigned development 包保持当前语义。所有新协议写入前须验证目标包并完成 bootstrap；marker 存在而 authority 缺失/损坏必须拒绝猜测。

## 模块顺序

1. 权威记录、严格解析、安装绑定及旧安装 adoption/bootstrap；定义无副作用状态读取与 fail-closed 行为。先做格式和迁移测试，未完整接入命令前不得创建生产 marker。
2. install/upgrade 事务：stage→真实验包→PREPARED→receipt→COMMITTED→兼容投影→READY；注入每个边界的异常/进程中断，确保证据和版本目录不被误删。
3. rollback/start/status/stop/uninstall 统一入口：回滚也使用同一意图/世代；pending 时 start/rollback 不得绕过；恢复可重复执行，stop 保持可用；旧受支持调用路径检测协议并拒绝陈旧读取。
4. PowerShell 7 能力门禁、包构建/发布清单/校验和、部署文档和 bootstrap 操作手册；明确管理员直接运行陈旧副本超出文件协议能防护的威胁边界。
5. 全量原生 Windows 回归、独立逐代码审计、更新最终报告，检查提交差异和干净工作区。任一模块未通过审计时停止后续模块并修复该模块。

## 每模块验收

- 先添加/更新回归用例，使用真实 Windows CMS/Authenticode fixture；不导入证书、不运行包内 launcher。
- 测试普通成功、签名策略轮换和撤销、重复/冲突请求、损坏 authority、legacy adoption、路径/锁文件故障、每个持久化边界的异常和进程崩溃。
- 验证 DataRoot 与已引用版本保留，真实 signer 校验不降级；PowerShell 5.1 不伪造签名覆盖，受支持 signed flow 仅在完整 PowerShell 7 通过。
- 模块完成后审计新增和调用代码、运行相关 native suite、确认无未审风险，再独立提交；之后才进入下一模块。

本文件是实施计划，不表示任何产品代码或协议状态已更改。

