# 第六轮审计 E03 修复记录

授权范围：仅 E03。起点 `b74d289`，分支 `codex/admin-optimization-20261009`，schema92。

## 根因与安全边界

A02 的 `95f244b` 已将公共地址策略拒绝规范化为 `PublicEndpointRejected`（`httpcore.ConnectError` 子类），保留 `PublicAddressRejected` 原因。但 `tests/test_m42_p2_06_p2_07_provider_security.py` 的三处旧断言仍期待 `OSError`。第六轮整仓为2432通过、56跳过、259子测试通过、1失败，原样单项复验仍失败；这不是公网地址保护失效或随机时间预算问题。

修复只调整该测试的异常合同，并加强原始策略拒绝原因检查。保留现有私网拒绝、真实TCP目的地址固定、TLS原hostname、重定向阻断、不可重试分类以及发送前/已发送费用边界。应用源码、schema、依赖、配置、CI/release脚本和其他测试均不改变。A03/A06/A07/A08/E01保持审计记录，不纳入本次修复；聊天删除仍关闭。没有推送、部署或调用真实供应商、平台、云及资金账户。

## 实现与模块复审

三处断言现在要求精确的 `PublicEndpointRejected`，而不是任意连接失败；每处还检查 `__cause__` 为 `PublicAddressRejected`。这样DNS错误或普通连接故障不能冒充策略拒绝。原有连接器地址列表和单次请求/禁止redirect断言全部保留。

最小tracked变更为一个测试文件：新增两个异常导入，替换三个旧异常断言，新增三次cause检查。没有删除、跳过或宽化失败测试，没有修改实现迎合旧断言。

模块回归：`test_m42_p2_06_p2_07_provider_security.py`、`test_public_http_errors.py`、`test_gate2_external_boundaries.py` 合并运行 **68通过、0失败/错误/跳过，65.52秒**，日志/XML为 `.runtime/e03-network-regression.log/.xml`。覆盖同步/异步DNS和地址策略、默认/注入resolver、TLS pinning、redirect、发送前错误分类、未知结果、账务及超时边界。

全根Ruff check通过；format **626文件通过**；Mypy **408文件通过**；`admin.js`/`app.js`语法检查和Node前端 **69通过、0失败/跳过**。初次format检查发现补丁产生混合换行，仅格式化该测试文件后全部通过；不是产品缺陷，也没有改变测试逻辑。

## 整仓门禁与最终交付

模块修复提交后，对其精确SHA启动一份整仓Python串行回归。结果尚待运行结束后补入，本段不声明整仓通过。

历史审计报告及其1项失败证据保留，最终修复结果以本记录为准。本次不解除其他应用问题或真实环境发布证据阻断。
