# 独立审计 P1/P2 修复记录

日期：2026-10-20。对应报告：`2026-10-19-independent-code-audit.md`。本轮范围为该报告的 2 项 P1 与 11 项 P2；问题编号不与其他轮次混用。

## 工作区与实施边界

- 基线 HEAD 为 `c67526d6b28f995823936687d9c59bb03b612463`；开始修复时已有未提交实现与审计文件，本轮保留这些修改。本文件记录的是当前工作区的修复结果，不能仅用 HEAD 代表最终代码。
- 最终 src/scripts/deploy/tests/.github 选定源码与部署测试文件共 560 个，按原审计相同算法计算的内容指纹为 `5367c74c566b24547b46f96f68ab9032e5aae16280f0adcf06a3f88b74e7f403`；明细保存在 `output/20261020-audit-source-manifest.json`。该清单不包含文档、日志或顶层示例配置。
- 不修改真实服务器、不请求真实 provider、不触发链上转账、不自动重新广播未知交易。
- 不通过放宽完整性、owner、挂载身份、撤销或资金保留条件使回归通过。
- 本轮未提交、推送或合并 GitHub；保留工作区结果供审核。原报告的 P3 事项不计入本轮完成范围。

## 实施顺序与安全边界

1. 先处理 capability 身份和持久化哈希，避免继续写出坏状态；bootstrap 与显式导入共享解析，但使用不同的 revision 生命周期规则。
2. 接着修复 root GC 与服务 UID 的权限合同，同时处理旧游标兼容、冲突计划终止和批次验证成本。
3. 对 signer HTTP/RPC 增加有界 I/O，再验证历史 envelope 迁移；保留未知结果与请求幂等语义。
4. 统一 storage profile，收紧 setup 备份写入前的存储、文件和发布检查。
5. 修复资源运行查询和环境同步的撤销语义。
6. 对每条新边界做定向回归、跨入口回归与实际 POSIX UID 检查，再执行静态与全量门禁。

## 按问题的实现与复核

| 编号 | 根因修复 | 主要回归与保留的边界 |
| --- | --- | --- |
| P1-01 | 导入已有 capability 时先确定最终 profile_id，再用最终行计算哈希。先验证所有候选，在同一事务导入，旧行先校验完整性。 | 跨数据库 revision 1→2、稳定 ID、再次读取、原子回滚、同 revision 冲突；正常读写不自动重签损坏记录。 |
| P1-02 | GC 证据迁入同设备的 .noyra-maintenance；POSIX 目录要求 root:数据组/0750，文件要求 root/0600、单硬链接、精确命名和类型。服务只检查元数据，不 chmod 或读取 root 文件。 | 实际 root 维护与 UID/GID 65534 的健康检查、硬化、备份和恢复；旧证据迁移、错误 mode、硬链接拒绝。证据不进入可移植备份。 |
| P2-01 | signer 最多 16 个请求工作线程；容量满时拒绝连接并尽力返回 503。请求读取使用共同的 30 秒绝对时限和总字节上限；校验 framing 与完整 body，响应后关闭连接。 | 未认证慢请求、超时后容量释放、短 body 不 dispatch；不改变主服务默认读取合同。 |
| P2-02 | signer RPC 使用受限公共 DNS 传输、无环境代理和重定向；流式读取最多 256 KiB，限制绝对总时长，校验 RPC id，拒绝压缩响应。超时清理响应并在实际发送前再次校验取消状态。 | 超大/压缩/错误 id 响应、私网 DNS 拒绝、慢响应清理、超时后不重发和保留 unknown；不自动释放未知交易状态。 |
| P2-03 | setup 在 mkdir 前后验证 backup 挂载与身份；拒绝路径中的 symlink/reparse、非私有根目录、已有目标 symlink/hardlink。备份及元数据先写私有临时文件并 fsync，再无覆盖地发布；已有备份也必须保持私有权限。 | 缺失挂载时不创建回退目录；目标/元数据/祖先链接不覆盖 sentinel；同内容重复备份和不同操作模式保留独立不可变元数据；源文件 mode 不变。 |
| P2-04 | runtime、storage-check 和 setup local 共用 resolve_storage_profile；production 的空白/未设值归一为 recovery_capable，其他 profile 归一为 runtime_only。 | production/development/public_preview 与 unset/空白/显式值的入口矩阵；非法显式值仍拒绝。 |
| P2-05 | 修正旧清单的 action 计数；每批重建当前计划时可合法续跑。policy/protected set 或待删对象变化时，旧 run 以 plan_superseded 终止并保留证据，再生成新计划。 | 每批重建的 release+backup 混合计划、策略改变、新保护集、中断恢复、持久游标与 marker 清理；不继续执行已经失效的删除授权。 |
| P2-06 | 新 v5 证据用 ordinal 索引和反向哈希链；当前 next_index/next_sha256 一起持久化。后续批次只查所需动作并验证链，release 集合和 backup 字典每次调用只构建一次。 | 1001 动作分批、索引篡改、完整结束哨兵、旧游标单次转换、相对数据库路径；保留当前目录计划检查与删除前核对。首次建索引及旧格式迁移仍需一次全量处理。 |
| P2-07 | active 查询把生命周期过滤下推 SQL，在一致读事务内 fetchmany 获取运行候选；环境同步按 subject/pool/label 精确查找。 | 管理页大小为 1 时仍可找到后面的 active 资源与既有同 label group；管理接口分页继续保留。 |
| P2-08 | 环境同步查询全部 key 的状态和指纹。临时 cooldown 仍属于当前密钥；拟配置的指纹若已 revoked，明确报 environment_model_resource_key_revoked。 | 撤销后再启动不复活密钥、不把缺少可用 key 静默视为成功；无关历史 revoked key 不阻挡替换密钥。 |
| P2-09 | 静态 capability 文件作为 bootstrap；先验证文件和已有记录，已有可信 revision 更新时保留新状态。显式导入仍拒绝回滚。 | 导入→在线 probe→重新启动的完整流程；已有环境资源可保留更新的已验证探测，新建资源仍要求精确 revision pin。 |
| P2-10 | 普通用户单元夹具准确模拟其自身证据 owner；真实 root 与服务 UID 合同用独立集成测试。root 专用存储测试明确标记，CI 增加 sudo GC/at-rest/storage 测试。 | 普通用户拒绝提供可信挂载身份、拒绝特权初始化；root 测试验证真实 owner/mode。生产代码仍要求 root owner。 |
| P2-11 | 历史 signer 请求逐行验证 transfer_json、transfer hash、chain、nonce、gas、fee 后才标为 bound；同时兼容旧表没有 envelope_status 与曾误标的完整记录。 | 14 种旧表/破损 envelope 组合、重复打开幂等；无法验证的记录保持 legacy_unbound，不重放交易。 |

## 持久化与恢复注意事项

### 已被旧版写坏的 capability

正常启动、bootstrap 和显式导入仍会拒绝 state hash mismatch。本轮新增离线工具 `scripts/repair-capability-profile.py`，只允许恢复被精确独立证据核对的一条记录：

这里的“独立证据”指来源不依赖损坏行本身的可信导出，不要求另找独立核验人；部署者可以自行准备证据并执行恢复。

1. 停止对应实例，以数据库所属账号操作，并使用已修复版本的 Python 环境。
2. 提供独立可信的单条 profile export，及准确的 subject、profile_id、原有坏 state_hash。不可从损坏数据库的内容重新拼装所谓“可信导出”。
3. 在数据根目录之外建立当前账号所有的 0700 证据目录；输入文件使用私有权限。指定一个不存在的 evidence 文件路径。
4. 工具获取与运行时相同的数据库锁，验证现有 schema 而不初始化或迁移数据库。只有全部存量字段与可信导出完全一致，才先持久记录授权证据，再更新该条记录的哈希。
5. 锁冲突、healthy record、身份不符、导出不符、坏 hash pin 不符、schema 不符、证据落盘失败时均拒绝操作。成功后仍应重新启动并执行正常健康检查。

工具参数为 `--database`、`--subject`、`--profile-file`、`--expected-profile-id`、`--expected-state-hash` 和 `--evidence-path`。它不提供批量重签或自动信任未知记录的模式。

### 旧配置备份的权限

新版 setup 会拒绝链接、非预期 owner 和权限过宽的既有备份。遇到旧版保留的宽权限备份时，应先核对 owner、链接数、内容摘要和所在私有目录，再由备份 owner 收紧该备份文件的权限；新代码不会为了复用旧备份而静默覆盖或修改配置源文件。

### GC 证据与恢复

- 新证据属于当前机器。可移植备份不携带特权证据；恢复后若数据库仍有 planned run 但本地证据缺失，先以 machine_local_evidence_unavailable 终止旧计划，再按当前路径与保护集重新规划。
- v2/v3 内嵌游标及 v4 清单会经完整验证后转换一次为 v5，不丢弃旧的已完成动作数。失败或 superseded 的证据保留，applied/orphan 证据按既有规则清理。
- SQLite 审计连接在单线程 root CLI 中临时使用数据库 owner 的有效 UID/GID，避免生成服务账号不能访问的 WAL/SHM；退出连接后恢复 root，再执行特权文件操作。
- 每次 GC 调用仍只处理有界 action batch；partial 状态通过后续同一维护入口继续消费。本轮没有新增后台定时删除任务，也未改变安装器原有调用时机。
- failed 证据的长期容量与归档治理属于原报告 P3-01，本轮不删除失败证据来缩小目录。

## 验证结果

验证环境为 Windows/Python 3.12.10 与 WSL Ubuntu/Python 3.12.3。以下批次存在测试重叠，不能相加作为独立测试数。

| 验证批次 | 实际结果 | 证据 |
| --- | --- | --- |
| Windows 主要定向回归 | 231 passed，10 skipped | output/20261020-audit-targeted-final.log |
| Windows 新增兼容性回归 | 119 passed，6 skipped | output/20261020-audit-compatibility-final.log |
| Windows 最后备份/离线恢复/启动边界 | 66 passed，1 skipped | output/20261020-audit-final-boundaries.log |
| Windows 最终 signer 模块与 bootstrap 启动 | 45 passed | output/20261020-audit-signer-final.log |
| Linux root：GC、at-rest、storage、profile | 122 passed，5 skipped | output/20261020-audit-root-final.log |
| Linux 普通用户：受影响模块及首轮全量失败模块 | 270 passed，19 skipped | output/20261020-audit-focused-linux.log |
| Linux 最后备份/恢复权限与路径 | 66 passed | output/20261020-audit-boundaries-linux.log |
| Linux 最终 signer 模块 | 44 passed | output/20261020-audit-signer-linux.log |
| Linux GC 相对数据库路径 | 1 passed | 本机终端执行记录 |
| Windows 最终 GC 模块 | 30 passed，3 skipped | output/20261020-audit-gc-final.log |
| Windows 最终 TCP 过载两种连接复核 | 2 passed | output/20261020-audit-overload-final.log |
| 前端 | 116 passed | output/20261020-audit-frontend.log |
| Ruff / 格式 | check 通过；692 文件格式检查通过 | 本机终端执行记录 |
| Mypy | 437 文件通过，显式 Python 3.12 target | output/20261020-audit-mypy-final.log |
| compileall / pip check / git diff --check | 通过 | 本机终端执行记录 |

上述最终定向批次及静态检查均已完成。真实 POSIX 的 root、专用挂载身份测试在普通用户运行时按条件跳过，并已由 root 批次覆盖；实际 Windows ACL/8.3 支持依赖平台。两个平台的首轮全量也包含未安装可选 boto3 时的跳过，不将其计作云依赖兼容性验证。

### 全量与复测的区别

Windows 首轮全量为 **2759 passed、2 failed、61 skipped、288 subtests passed**，耗时约 2 小时 19 分钟，保存在 output/20261020-audit-full-windows.log。两个失败为旧 bootstrap 测试替身不接收新关键字，以及 signer 过载测试收到 Windows TCP reset。前者已修正签名；后者改用实际工作线程占用/释放的同步信号，并覆盖未发送请求和已发送请求两种连接。预 HTTP 阶段拒绝已在写入的对端时，503 是尽力发送，操作系统也可能直接复位 TCP；回归同时验证拒绝、过期关闭和容量恢复。修正后 Windows signer+启动 45 项、Linux signer 44 项均通过。

Linux 首轮全量为 **2763 passed、12 failed、48 skipped、288 subtests passed**，耗时约 1 小时 57 分钟，原始失败完整保存在 output/20261020-audit-full-linux.log。10 个失败属于新 bootstrap 测试替身/配置文件权限和 root 专用存储夹具合同，已修正夹具并以普通用户、root 分别复核；生产权限规则没有放宽。

另外 2 个首轮失败分别是反思上下文没有得到预期事件，以及钱包准入夹具得到 rejected 状态。相关完整模块随后纳入 Linux 270 项通过的批次。未取得足够证据把它们归因为本轮改动，也不据后续通过宣称已证明所有时钟边界安全。首轮结果仍保留为未全绿。

最终验收依据为与修复合同直接对应的模块回归、真实 UID 测试、补强后的边界测试和静态检查；不能把“全量后修了夹具，再复测相关模块”写成“最终工作区单次全量全部通过”。

## 证据的实际限制

- Linux 验证使用本机 WSL Ubuntu 的隔离 mount namespace、临时数据和真实 UID；未把它等同于生产 Ubuntu systemd、LUKS、断电或跨机迁移验收。
- Windows 无法执行的真实 POSIX owner/mount/ACL 条件分别由 Linux 或平台专用门禁覆盖，跳过项需单独列出。
- GC 批次验证成本的改进有实现与回归支持，不声称已完成十万级历史目录的性能压测；失败证据增长仍遵循未纳入本轮的 P3 范围。
- 这份记录确认上述特定问题的代码和测试结果，不保证整个项目不存在其他问题。
