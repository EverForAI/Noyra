# 第五轮审计选定问题修复记录

授权范围：A02、A04、A05、E02。起点 `398059d`，schema92；分支 `codex/admin-optimization-20261009`。

按用户要求，逐模块确认根因、限定安全边界、回归与复审后本地提交。A03 和 E01 仅保留记录；聊天删除继续禁用。不推送、部署、使用真实模型/搜索/平台/云凭据或进行资金交易。

## 顺序和验收合同

1. **A02，共享网络错误合同。** DNS/公共地址拒绝/连接失败仅在发送前转换为 httpcore/HTTPX typed errors，保留 cause、公网固定及 TLS。非法端点不可重试；可证明未发送的模型/embedding 用量为零，发送后或未分类异常仍 unknown。验证同步/异步、默认/注入 DNS、多地址、账本与公共知识退避/其他 peer；不改变 A03 的工作槽/原始 I/O 寿命。
2. **A04，同步完成所有权。** peer 快照和持久化完成记录作为 CAS，失败与成功都必须基于未被替代的状态更新 peer；迟到结果保留独立历史，不能覆盖较新状态。导入/游标事务、lease/key 验证保持；不跨网络持有数据库事务。覆盖固定时钟/相同结果的 ABA、304/空 feed、配置/游标变化与真实交错。
3. **A05，鉴权输入。** 不可信凭据先检查类型/ASCII，再做原有 constant-time 比较；不 trim/normalize/扩大权限。非法 Unicode 登录/Bearer/CSRF 返回原有非成功合同，限速、cookie、角色、有效 ASCII 保持。
4. **E02，cloud 自动回归。** hash 锁定 dev+cloud 的隔离 CI 作业及 release 前门禁必须执行实际合成签名/log/history 200/403 和兼容测试，缺依赖或跳过不能绿色通过；不接真实 AWS、不强制基础部署安装 cloud。

各模块通过针对性回归后再提交。最后运行共享边界关联回归、全根 Ruff/Mypy/前端门禁，核对实际文件范围和干净工作区。真实 GitHub runner/Linux/生产验收须明确为未运行，不以本地模拟代替。

## 模块结果

### A02：完成

根因是共享网络 backend 把 DNS/地址策略拒绝和多地址连接耗尽抛为普通 `OSError`，消费者不能识别发送前失败。现在在 TCP/DNS 边界转换为 httpcore ConnectError/ConnectTimeout，由 HTTPX 正常映射；地址策略拒绝用独立 cause 保留不可重试信息。模型账本记录未发送失败、零 token/成本，embedding ConnectTimeout 同样为已知未发送；读写和未分类异常仍 unknown、保留预算且不重试。未改 worker/gate 寿命，A03 继续记录。

复审：错误文本不包含 endpoint/凭据；公共地址校验、实际连接 IP 固定、TLS 原始 hostname、多地址预算和发送前授权保持。公共知识失败正常记录/退避，坏 peer 不阻塞另一个 peer；未增加吞掉任意异常的兜底。

锁定环境关联回归 **204 项通过、172 子测试通过**（`.runtime/selected-a02-final.xml`）；补充专项 **37 项通过**（`.runtime/selected-a02-extended.xml`），含同步/异步多地址成功、TLS pinning 和只发送一次。变更的 5 个 Python 文件 Ruff/格式及 Mypy 通过。初次运行的 2 项失败为测试夹具复用同 publisher 导致 UNIQUE 拒绝、旧测试仍要求 OSError；调整为独立 publisher 和新 typed contract 后通过。早期系统解释器 Mypy 被其额外 numpy stub 阻断，最终证据来自 `.runtime/locked-venv/Scripts/python.exe`。以上是模块回归，不声称新提交整仓测试完成。

其余模块结果在实际验证后追加。保留原审计作为历史证据，不改写旧 SHA 的结果。
