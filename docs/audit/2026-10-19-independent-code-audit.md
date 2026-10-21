# Noyra 当前工作区独立代码审计

审计日期：2026-10-19。审计方式：只读检查现有实现，在临时目录、临时数据库和本机回环端口中复现问题，仅新增本报告。

## 1. 结论与证据边界

本次从当前源码、调用者、持久化合同、部署权限和运行结果重新判断问题，不沿用历史报告的问题编号、完成百分比或“已经关闭”结论。下列编号仅属于本报告。

确认 **2 项 P1、11 项 P2、3 项 P3，未确认 P0**。最先处理的两个问题是：

1. 更新已有模型 capability profile 时，数据库保留旧 `profile_id`，却写入按新 `profile_id` 计算的状态哈希，造成应用自己写出无法通过校验的持久记录。
2. root 维护程序把 root:root、0600 的 GC 清单写进服务账号的数据根目录；服务的生产存储检查不接受该文件，导致健康检查失败、写接口被拒绝，重启还会因 chmod 权限错误失败。

这不是仅凭代码形状推测的结论。第一项已用两个数据库、连续两次导入复现；第二项已在 WSL Ubuntu 下创建真实 root-owned 清单，再降权为 UID 65534，复现读取边界失败与 `PermissionError`。此外，Ubuntu 非 root 运行已复现 3 个现有测试失败，同样的测试以 root 运行全部通过。

“未确认 P0”仅说明本轮证据没有达到该级别，不能作为所有代码不存在严重漏洞的证明。仓库的 `src/noyra` 与顶层 Python scripts 约有 147,277 行；本轮执行了全仓文件清单与静态门禁，对下列关键路径进行了逐函数、调用链和跨模块合同检查，并运行自动化测试。**没有把文件清单扫描或测试通过冒充为人工完整阅读每一行的证明。**

### 1.1 被审计的版本

- Git HEAD：`c67526d6b28f995823936687d9c59bb03b612463`。
- 分支：`main`，本地比 `origin/main` 领先 45 个提交。
- 实际审计对象：上述 HEAD 加审计开始时已经存在的 26 个 tracked 文件改动，以及当时的配置和测试；不能只用 HEAD SHA 代表这些未提交实现。
- `src/scripts/deploy/tests/.github` 下选定源码、测试和部署文件共 557 个，其内容清单 SHA-256 为 `1e00efbcb8e25f53e4f5241883e633fb7d5b3f07af856fab8ee71e01c4fbae23`。清单按路径排序，以 UTF-8 路径、NUL 和文件 SHA-256 字节串累计计算；选取扩展名为 `.py/.js/.css/.html/.sh/.ps1/.yml/.yaml/.toml/.service/.path/.timer`，排除 `__pycache__`。
- 本报告没有修复实现、重写测试、提交、推送或操作真实服务器。

### 1.2 分级标准

| 等级 | 本报告中的含义 |
| --- | --- |
| P0 | 已确认严重越权、私密数据或资金失控、不可逆的大范围数据破坏，需要立即阻断 |
| P1 | 在受支持流程中可复现持久状态失效、主要服务不可用，或重要安全边界失效 |
| P2 | 有明确条件的可用性、恢复、容量、配置一致性或安全加固缺陷；通常可由运维恢复 |
| P3 | 产品入口、文档或长期运维治理欠缺，当前影响较低 |

修复风险独立于问题风险：低表示可局部修改并用窄范围回归验证；中表示涉及多个调用者、状态机或兼容性；高表示涉及已存在的损坏状态、权限边界或金融历史，不能只修改一个判断后批量“重新签哈希”。

### 1.3 检查范围

| 领域 | 主要检查对象与边界 |
| --- | --- |
| 启动、数据库、完整性 | `service.py` 构造/from_env/boot，`core/runtime.py`、`database.py`、`integrity.py`、`locking.py`、`admission.py`；检查启动写入、ownership、恢复、quarantine、事务与状态哈希 |
| 存储、备份、升级 | `at_rest.py`、`storage_mode.py`、`backup_retention.py`、`retention.py`、`deployment_setup.py`、maintenance、Ubuntu installer 与 systemd；检查路径身份、权限、失败恢复、删除计划 |
| 模型与外部 I/O | `model/config.py`、`capabilities.py`、`resources.py`、gateway/ledger、embedding、`core/http.py`、research/world；检查预算、状态、reasoning、网络响应和失败分类 |
| 钱包、签名器、迁移 | signer HTTP/RPC/SQLite、钱包执行与最终 dispatch、migration trust/policy/bundle/cutover/activation；检查幂等、未知结果、历史兼容、签名证明和特权边界 |
| 公共页面与管理端 | HTTP authentication、cookie/CSRF、public projection、前端状态与测试；检查公开与私有投影、会话代际和配置能力入口 |
| 运维与发布 | soak collector、retention、CI、Docker/systemd、凭据读取及日志脱敏；检查计量游标、权限、跨平台验证与默认配置 |

## 2. 问题总表

| 编号 | 问题 | 风险 | 修复风险 | 证据强度 |
| --- | --- | --- | --- | --- |
| P1-01 | profile 升级导入写出错误状态哈希 | P1 | 中；存量修复中高 | 独立临时库实测 |
| P1-02 | root GC 清单破坏服务数据目录的权限合同 | P1 | 中高 | Ubuntu 真实 UID/文件权限实测 |
| P2-01 | signer HTTP 未限制连接工作线程与请求总时长 | P2 | 中 | 回环连接实测与源码 |
| P2-02 | signer RPC 仍使用无响应大小上限的普通 HTTPX 请求 | P2 | 中 | 3 MB mock 响应实测与源码 |
| P2-03 | setup 配置备份绕过挂载身份及目标文件安全检查 | P2 | 中 | 临时目录与 symlink 实测 |
| P2-04 | production 的 storage profile 默认值在 CLI/setup/runtime 间不一致 | P2 | 低中 | CLI 与 Settings 对比实测 |
| P2-05 | GC 正常续跑被计数错误拒绝，冲突计划也没有退出入口 | P2 | 中 | 小批量续跑与策略变更实测 |
| P2-06 | GC 限制 action 数量仍不能限制每批总工作量 | P2 | 中 | 调用链与复杂度分析 |
| P2-07 | 资源运行时把管理分页第一页当作完整资源集合 | P2 | 中 | 缩小页上限的边界实测 |
| P2-08 | 环境同步只比较 key 指纹，仍会接受已撤销的 key | P2 | 低中 | 真实 store 生命周期实测 |
| P2-09 | 固定 profile 文件每次启动导入，与在线 probe 修订冲突 | P2 | 中 | 导入/probe/再导入实测 |
| P2-10 | Linux 普通用户质量门禁存在 3 个确定性失败 | P2 | 低 | Ubuntu non-root/root 对照实测 |
| P2-11 | signer 升级把有完整 envelope 的历史请求误标成 legacy_unbound | P2 | 中高 | 旧表升级实测 |
| P3-01 | failed manifest 永久保留，没有容量和归档清理合同 | P3 | 中 | 当前清理逻辑与入口检查 |
| P3-02 | capability export 只有内部函数，缺少可操作的导出入口 | P3 | 中 | API/CLI/UI 调用点检查 |
| P3-03 | README 把当前 schema 写成 62，实际已为 99 | P3 | 低 | 文档与常量直接对照 |

P3-03 是本报告的文档问题编号，与其他报告的同名编号无对应关系。

## 3. P1 详细发现

### P1-01：profile 升级导入写出错误状态哈希

**位置：** [capabilities.py:410](E:/我的项目/Noyra/开发版/src/noyra/model/capabilities.py:410)、[capabilities.py:423](E:/我的项目/Noyra/开发版/src/noyra/model/capabilities.py:423)、[capabilities.py:443](E:/我的项目/Noyra/开发版/src/noyra/model/capabilities.py:443)，校验入口为 [capabilities.py:160](E:/我的项目/Noyra/开发版/src/noyra/model/capabilities.py:160)。

**根因：** `import_profiles()` 为每个待导入 state 无条件生成新的 `profile_id`，并把它纳入 `content_hash(state)`。如果 endpoint/model 已存在且 revision 增大，SQL 走 `ON CONFLICT ... DO UPDATE`，更新 capability、revision、source、时间和 state_hash，却不更新数据库原有的 `profile_id`。最终行内容与 state_hash 所对应的内容不一致。

**触发条件：** 同一个 subject/provider/endpoint/model 先有 revision 1，再导入 revision 2 或更高。首次导入、相同 revision 的幂等导入不会暴露问题。

**影响：** 导入事务成功提交并返回 1，但下一次 `.get()` 或后续 probe 会因 `IntegrityError: model capability profile hash mismatch` 失败。若文件由服务启动导入，后续环境资源同步读取同一条 profile，可使该次启动直接失败；普通配置查询也可能异常终止。业务写入自行造成持久状态失效，故为 P1。

**复现：** 临时 source 库生成 revision 1 并导出，target 导入成功并能读取；source 再 probe 生成 revision 2，target 再导入返回 1，紧接着读取即出现上述 IntegrityError。使用的模型名为测试 alias，没有调用真实 provider。

**修复方向：** 更新时沿用已有 profile_id，并按最终落库行计算哈希；或者事务中明确替换整条身份记录，但必须证明没有外部引用。前者影响面较小。存量错误行需要单独的可信恢复设计：根据受验证的原始 profile 文件与当前行身份判断，不应对任意不可信行直接重算哈希。

**修复风险：** 新写入路径中等；存量恢复中高。验收至少覆盖首次导入、同 revision 幂等、revision 递增、回滚拒绝、同 revision 不同内容拒绝、批次中后项失败的原子回滚，并在每次成功后同时验证原始行 hash 与 `.get()`。

### P1-02：root GC 清单破坏服务数据目录的权限合同

**位置：** [deployment-maintenance.py:538](E:/我的项目/Noyra/开发版/scripts/deployment-maintenance.py:538)、[deployment-maintenance.py:603](E:/我的项目/Noyra/开发版/scripts/deployment-maintenance.py:603)、[at_rest.py:1386](E:/我的项目/Noyra/开发版/src/noyra/core/at_rest.py:1386)、[at_rest.py:1465](E:/我的项目/Noyra/开发版/src/noyra/core/at_rest.py:1465)、[at_rest.py:1485](E:/我的项目/Noyra/开发版/src/noyra/core/at_rest.py:1485)、[noyra.service:12](E:/我的项目/Noyra/开发版/deploy/systemd/noyra.service:12)。

**根因：** maintenance CLI 要求 Linux root。`_gc_manifest_path()` 把 sidecar 放在 `noyra.sqlite3` 同目录；`_write_gc_manifest()` 通过 root 的 mkstemp 创建文件并设为 0600。因此文件属于 root。另一方面，required at-rest 模式将数据根目录中除明确列出的 privileged control/lost+found 以外的所有文件都视为服务私有状态；`_harden_private_paths()` 对它们 chmod，`_posix_metadata_error()` 要求 owner 等于服务 UID。GC manifest 不在特权布局合同中。

**触发条件：** 正常 root maintenance 创建 sidecar 后，服务执行 at-rest 检查；最稳定的触发是超过 1000 个 action 后返回 partial、GC 失败保留 manifest、或进程中断留下清单。升级脚本在服务已恢复运行后才执行 GC，也存在运行中短暂触发窗口。

**影响：** 运行中 `require_ready()` 返回私有目录 owner 错误，相关健康与写入接口不可用；重启时 service UID 无权 chmod root-owned 0600 清单，`prepare()` 直接抛出 `PermissionError`。保留 failed manifest 的证据策略会使该状态持续存在。不是 root 恶意修改系统的假设，而是两个受支持组件按各自正常规则制造冲突。

**实测：** WSL Ubuntu 的 root 在临时、服务 UID 所有的目录中创建同名模式的 root-owned 0600 manifest；子进程降权至 UID/GID 65534 后：

```text
require_ready_permissions= private storage is not owned by the service account: .noyra.sqlite3.backup-retention-....manifest
prepare_hardening= PermissionError [Errno 1] Operation not permitted
```

**修复方向：** 明确 root maintenance evidence 的独立命名空间和权限合同；优先避免直接混入普通服务私有文件集。若放在数据卷内特权子目录，必须像已有 upgrade/migration control 一样验证精确 owner/mode/device/link count，并处理备份、恢复、失去挂载后的识别和遗留清单迁移。不能用“忽略所有隐藏文件”或无条件 chown 取消安全边界。

**修复风险：** 中高。验收必须在真实 POSIX 上交叉运行 root GC 与非 root 服务，覆盖 planned/failed/applied/孤儿/临时 sidecar、1001 个 action、服务在线健康、失败后重启，以及备份恢复后的权限和证据归属。

## 4. P2 详细发现

### P2-01：signer HTTP 未限制连接工作线程与请求总时长

**位置：** [signer_service.py:454](E:/我的项目/Noyra/开发版/src/noyra/signer_service.py:454)、[signer_service.py:470](E:/我的项目/Noyra/开发版/src/noyra/signer_service.py:470)、[signer_service.py:495](E:/我的项目/Noyra/开发版/src/noyra/signer_service.py:495)。可对照主服务的 [service.py:1464](E:/我的项目/Noyra/开发版/src/noyra/service.py:1464) 与 [request_io.py](E:/我的项目/Noyra/开发版/src/noyra/core/request_io.py)。

**根因：** signer 直接使用标准 `ThreadingHTTPServer`，没有工作线程 semaphore，也没有 socket/header/body 的绝对期限。`MAX_BODY_BYTES` 只约束已声明的字节数，不能限制等待请求行、header 或不完整 body 的时间。认证发生在 header 解析之后。

**触发条件：** 客户端建立连接后只发送部分 header；或者有凭据的客户端声明合法 Content-Length 后慢速/停止发送 body。若通过 HTTPS tunnel 暴露 origin，外部连接是否能触发取决于代理自身的转发行为和限制，本报告没有验证真实 Cloudflare 实例。

**影响与证据：** 本机回环开 6 个未完成 header 的连接，服务新增 6 个 request thread，socket timeout 为 None。更多连接没有应用层上限，可耗尽线程、内存或 FD，影响 nonce、广播和回执查询。没有证明可越权签名或盗取密钥，因此定 P2。

**修复风险：** 中。应复用或提取已有有界 HTTP 基础设施，分别约束认证前读取、body 总时长和工作线程；过载明确返回安全响应。测试未授权慢 header、合法长度慢 body、并发上限、正常转账、客户端断开及服务关闭，避免把 RPC 执行到一半误当成“安全未发送”。

### P2-02：signer RPC 没有应用层响应大小、绝对时限与 DNS 连接边界

**位置：** [signer_service.py:64](E:/我的项目/Noyra/开发版/src/noyra/signer_service.py:64)、[signer_service.py:72](E:/我的项目/Noyra/开发版/src/noyra/signer_service.py:72)、[signer_service.py:78](E:/我的项目/Noyra/开发版/src/noyra/signer_service.py:78)、[core/http.py:691](E:/我的项目/Noyra/开发版/src/noyra/core/http.py:691)。

**根因：** `HTTPRPC` 使用普通 `httpx.Client.post()`，完整缓冲响应后 `.json()`。它没有使用项目已有的流式 byte limit、绝对 operation deadline 或 PublicDNS transport。`timeout=15` 是 HTTPX 各阶段/闲置超时，不等于整个 RPC 的 15 秒总时限；URL 字面值检查也不能固定 DNS 实际连接地址。

**触发条件：** 配置的 RPC 上游异常、被入侵或 DNS 结果变化，返回超大 JSON、持续 drip 数据，或解析到非公网地址。

**影响与实测：** mock RPC 返回含 3,000,000 字节 padding 的 JSON，客户端完整接收 3,000,068 字节并正常返回 `0x1`。代码中不存在可阻断更大响应的应用层上限。慢响应会延长关键 signer 操作并增加未知广播状态；未固定 DNS 还使该组件弱于主运行时的外部网络边界。未对真实 RPC 施加压力。

**修复风险：** 中，广播分支需要特别谨慎。应使用统一有界 reader、total deadline 和 public address pinning，保证在 `eth_sendRawTransaction` 可能已发出后仍保留 unknown，而不是因新 timeout 路径释放付款状态。测试大体积、压缩响应、慢 body、DNS 私网结果、RPC id 不匹配与发送后超时。

### P2-03：setup 配置备份绕过挂载身份和目标文件安全检查

**位置：** [deployment_setup.py:207](E:/我的项目/Noyra/开发版/src/noyra/deployment_setup.py:207)、[deployment_setup.py:214](E:/我的项目/Noyra/开发版/src/noyra/deployment_setup.py:214)、[deployment_setup.py:541](E:/我的项目/Noyra/开发版/src/noyra/deployment_setup.py:541)、[deployment_setup.py:981](E:/我的项目/Noyra/开发版/src/noyra/deployment_setup.py:981)。

**根因：** public/cloudflare setup 解析了配置中的 backup root，但没有像 runtime storage preflight 一样验证该路径的 mount identity；`create_backup()` 直接 `mkdir(..., exist_ok=True)`、按固定文件名 `copy2()`、写 metadata。它不拒绝 backup root 的 symlink/reparse 祖先，不验证 root owner/mode，不拒绝已经存在的目标 symlink/hardlink，也没有原子发布与 fsync 合同。

**触发条件：** 专用备份盘缺失但路径仍可创建；或者管理员配置到了共享可写/链接目录，并且目标名已被放置成链接。正常 root-only、无链接目录不构成已证明的任意文件写漏洞，攻击条件不能省略。

**影响与实测：** 临时测试中，原本不存在的“预期挂载点”被直接创建并写入备份；把计算好的 `.bak` 文件替换为指向另一个临时 sentinel 的 symlink 后，再次 `create_backup()` 会覆盖 sentinel。真实 public/cloudflare 分支可能备份 env 与 tunnel credential，意味着备份落在错误介质或沿链接写错文件。

**修复风险：** 中。应统一配置备份与 deployment storage 的身份/权限检查，使用不可跟随链接、单链接的受控临时文件与原子发布，并定义备份存储缺失时停止变更的行为。回归 public/cloudflare 的成功、失败回滚、只读 dry-run、已存在备份、缺盘和链接攻击；不要为了修复备份路径改变原配置文件的权限。

### P2-04：production 的 storage profile 默认值在入口间不一致

**位置：** [service.py:1092](E:/我的项目/Noyra/开发版/src/noyra/service.py:1092)、[__main__.py:153](E:/我的项目/Noyra/开发版/src/noyra/__main__.py:153)、[deployment_setup.py:401](E:/我的项目/Noyra/开发版/src/noyra/deployment_setup.py:401)。

**根因：** `ServiceSettings` 在未指定 storage profile 时，将 production 解析为 recovery_capable；storage-check 和 setup local 无论 NOYRA_PROFILE 都默认 runtime_only。环境变量为空字符串时的处理也不同：Settings 将其视为未指定，CLI/setup 视为无效。

**触发条件：** 旧环境文件设置了 `NOYRA_PROFILE=production`，但没有新变量 `NOYRA_STORAGE_PROFILE`，且 backup root 不可用；或新变量为空。

**影响与实测：** 对同一个 runtime ready、backup missing 的布局，production 下 `storage-check` 输出 `backup_required=false` 并以 0 退出；production Settings 的默认值却是 recovery_capable，runtime 会拒绝该布局。预检与实际启动的结果相反，错误暴露太晚。

**修复风险：** 低中。抽取同一 profile 解析规则，CLI/setup/runtime 共用；保留显式 runtime_only 的降级部署选择。测试 development/test/production 与 absent/empty/invalid/显式两种 profile 的矩阵，不能用强制所有部署都要求备份的方式掩盖差异。

### P2-05：GC 正常续跑被误拒绝，冲突计划也无法退出或重建

**位置：** [deployment-maintenance.py:992](E:/我的项目/Noyra/开发版/scripts/deployment-maintenance.py:992)、[deployment-maintenance.py:1036](E:/我的项目/Noyra/开发版/scripts/deployment-maintenance.py:1036)、[deployment-maintenance.py:1068](E:/我的项目/Noyra/开发版/scripts/deployment-maintenance.py:1068)、[install-ubuntu.sh:1405](E:/我的项目/Noyra/开发版/scripts/install-ubuntu.sh:1405)。

**根因：** `_gc_manifest_matches_current_plan()` 遇到仍然存在、且仍在当前删除计划里的 backup action 时直接 `continue`，跳过循环尾部的 `count += 1`；最终却要求 count 等于 manifest 的全部 action_count。因此只要重建后的计划需要走该校验，且还有 backup 等待删除，合法计划也会被误拒绝。另一个生命周期缺口是：resume 对 policy/protected digest/当前计划的拒绝发生在记录 failed 的 try/except 之前，旧 run 仍为 planned，下次又选中同一个 run；CLI 没有带审计的 cancel/supersede/replan 入口。每次 installer 只执行一次 GC，partial run 也没有专门的持续消费单元。

**触发条件：** 上一轮已经删除部分 release/backup，CLI 根据当前目录重建了不同 plan_id，且仍有 backup action；即使策略未改也会触发计数缺陷。管理员调整 retention policy，或新的备份/发布改变 protected set、增加候选时，还会遇到没有取消/替代入口的问题。

**影响与实测：** 复现 A：batch 上限降为 1，计划包含一个 release、一个 backup；第一轮删除 release 后返回 partial，再按现状生成不含该 release 的计划，compatibility 返回 False、resume 抛 `backup_retention_resume_plan_changed`，backup 仍存在。复现 B：两个 action 的第一轮返回 partial；随后换 protected set，连续两次调用都抛相同错误，数据库状态始终 planned。对真正不兼容的新计划严格拒绝是正确行为，但现有代码既误拒绝合法续跑，也没有合法终止旧计划的操作路径，retention 会持续停摆。

**修复风险：** 中。先修复 manifest action 计数与完成/剩余 action 的兼容性判断，再设计审计化终止/替代状态，保留旧 manifest、已完成动作和 failure reason，基于当前保护集重新生成计划；不得强行沿用旧 action 或清空审计行。验收必须在每批之间真正重建计划，覆盖 release 与 backup 混合、中断、策略变更、新 release、新 backup、多次 supersede 和 crash 重试，不能只把最初的 plan 字典再次传入测试。

### P2-06：GC 每批限数仍不能限制总工作量

**位置：** [deployment-maintenance.py:633](E:/我的项目/Noyra/开发版/scripts/deployment-maintenance.py:633)、[deployment-maintenance.py:672](E:/我的项目/Noyra/开发版/scripts/deployment-maintenance.py:672)、[deployment-maintenance.py:889](E:/我的项目/Noyra/开发版/scripts/deployment-maintenance.py:889)、[deployment-maintenance.py:1110](E:/我的项目/Noyra/开发版/scripts/deployment-maintenance.py:1110)。

**根因：** 每个 batch 都从头扫描整个 manifest、解析每个 JSON action、重算完整 digest；resume 还可能全量对比当前计划。单 action 校验也重复执行 release 列表成员搜索、构建整个 backup_details 字典，取备份详情时再次线性搜索。1000 仅限制执行动作数量，并未约束扫描和验证成本。

**触发条件：** 大量历史 release/backup、超大计划或频繁中断续跑。

**影响：** N 个 action 分成 ceil(N/1000) 批时，manifest 扫描量随 N×ceil(N/1000) 增长；单动作反复扫描还会引入另一层二次成本。可能超出维护窗口并延迟空间回收。这里是代码复杂度分析，本轮没有声称完成 100,000 action 的性能压测。

**修复风险：** 中。需要可验证的 batch offset/index、一次构建且受界的查询索引或流式 merge，并继续验证 action 身份、顺序、总 digest 和 crash cursor。只给 reader 加 seek 不足以解决整个调用链的重复扫描。

### P2-07：运行时把管理分页第一页当成完整资源集合

**位置：** [resources.py:91](E:/我的项目/Noyra/开发版/src/noyra/model/resources.py:91)、[resources.py:796](E:/我的项目/Noyra/开发版/src/noyra/model/resources.py:796)、[resources.py:835](E:/我的项目/Noyra/开发版/src/noyra/model/resources.py:835)、[service.py:10828](E:/我的项目/Noyra/开发版/src/noyra/service.py:10828)。

**根因：** `list()` 默认只取 1000 行，且排序结果包含所有生命周期状态。`active()` 先取一页再在 Python 中筛 active；环境同步同样只读取一次 `.list()` 来判断既有资源。分页是管理接口的正确边界，但内部调用者把截断结果当作全集。

**触发条件：** 某池前 1000 条排在前面的资源为 disabled/revoked，仍有 active 资源排在后面；或待同步的同 label durable group 位于第一页以外。

**影响与实测：** 将 MAX_RESOURCE_PAGE_SIZE 缩为 1 的边界测试中，第一条 disabled、第二条 active；`active()` 返回空列表，而按 ID 直接读取第二条仍为 active。生产对应阈值为 1000。环境同步也可能误判不存在而尝试 configure，最终产生 label 冲突和启动失败。

**修复风险：** 中。至少把 active 过滤下推到 SQL；内部查找应使用 subject/pool/label 的精确查询，真正需要遍历时使用有界分页，不可简单取消所有 LIMIT。回归 1001 条、混合状态、两个 pool、同 label 历史及现有管理分页行为。

### P2-08：环境同步接受已经撤销的 key 指纹

**位置：** [service.py:10862](E:/我的项目/Noyra/开发版/src/noyra/service.py:10862)、[service.py:10896](E:/我的项目/Noyra/开发版/src/noyra/service.py:10896)、[resources.py:895](E:/我的项目/Noyra/开发版/src/noyra/model/resources.py:895)、[resources.py:2110](E:/我的项目/Noyra/开发版/src/noyra/model/resources.py:2110)。

**根因：** group 级状态已有显式处理，但 current_keys 集合只收集 fingerprint，不检查 key.status。`keys()` 包含 revoked 历史行。撤销不会删除历史 fingerprint，所以同一环境密钥仍被判定为完全匹配。

**触发条件：** 管理员只撤销 group 中的 key，group 自身保持 active；重启时环境还声明该 key。

**影响与实测：** 用真实 store configure→revoke_key→环境同步，调用正常返回，group 状态 active，`select_key()` 返回 None。没有出现撤销绕过或重新启用，但环境资源同步缺少明确冲突信号，配置存在而无模型请求的问题仍可发生。管理投影的 available_key_count 仍能显示 0，不能将影响夸大成所有诊断均错误。

**修复风险：** 低中。比较环境声明与 durable key 的有效生命周期，明确区分 active、cooldown、revoked；revoked 需要告知操作员，不能自动补回秘密或取消撤销。覆盖单 key、多 key 部分撤销、cooldown、secret cleanup pending 和重启。

### P2-09：在线 probe 更新后，旧 profile 文件使重启失败

**位置：** [service.py:10756](E:/我的项目/Noyra/开发版/src/noyra/service.py:10756)、[service.py:11028](E:/我的项目/Noyra/开发版/src/noyra/service.py:11028)、[capabilities.py:416](E:/我的项目/Noyra/开发版/src/noyra/model/capabilities.py:416)。

**根因：** `NOYRA_MODEL_CAPABILITY_PROFILES_FILE` 的内容每次进程启动都导入，沒有 bootstrap 已消费状态或 source revision 管理；在线 probe 会递增 durable revision，静态 root-owned 文件不会自动更新。下次导入文件因 revision rollback 被拒绝，即使数据库记录合法且更新鲜。

**触发条件：** 初次部署导入 revision 1；操作员在界面重新测试同一 endpoint/model，使数据库变成 revision 2；静态文件仍为 revision 1，随后重启。

**影响与实测：** 导入、在线 probe、再导入旧文件，最后一步稳定得到 `ValueError: model capability profile revision rollback`。拒绝回滚是正确的，但当前运行流程没有说明静态文件只作 bootstrap 还是持续 desired state，使正常重新测试变成下次启动故障。这与 P1-01 的更新哈希错误是两个独立根因。

**修复风险：** 中。明确 source ownership：可采用一次性、可审计 bootstrap，或显式持久 desired profile；不得直接允许旧 revision 覆盖新记录。测试在线 probe 后重启、文件被更新/移除、不同 endpoint、过期 profile 和环境 group 的 revision/digest 绑定。

### P2-10：Linux 普通用户质量门禁存在 3 个确定性失败

**位置：** [test_deployment_maintenance.py:13](E:/我的项目/Noyra/开发版/tests/test_deployment_maintenance.py:13)、[test_deployment_maintenance.py:453](E:/我的项目/Noyra/开发版/tests/test_deployment_maintenance.py:453)、[test_deployment_maintenance.py:506](E:/我的项目/Noyra/开发版/tests/test_deployment_maintenance.py:506)、[test_deployment_maintenance.py:628](E:/我的项目/Noyra/开发版/tests/test_deployment_maintenance.py:628)、[deployment-maintenance.py:573](E:/我的项目/Noyra/开发版/scripts/deployment-maintenance.py:573)、[ci.yml:27](E:/我的项目/Noyra/开发版/.github/workflows/ci.yml:27)。

**根因：** 测试在当前用户下创建 manifest；`_helper(root_markers=True)` 只把 marker reader 的 metadata 模拟成 root，没有覆盖新增的 manifest owner 检查。清理现在在查看 planned/failed 状态前就要求 UID 0，因此普通 Linux 用户生成的测试清单均被拒绝。Windows 分支不检查 UID，无法发现这个问题。

**实测：** 在 WSL Ubuntu 以 nobody 运行下列测试，结果 **3 failed，21 deselected**；同样测试以 root 运行 **3 passed，21 deselected**：

- `test_gc_manifest_cleanup_preserves_failed_and_pending_evidence`
- `test_gc_runtime_failure_leaves_manifest_for_next_maintenance_run`
- `test_gc_consumes_more_than_one_thousand_actions_in_batches`

共同失败码为 `backup_retention_orphan_manifest_unsafe`。CI 的普通 pytest step 没有 sudo，因此代码中的验证配置与测试 fixture 确定存在不兼容；本轮没有远程触发 GitHub Actions，不把本地对照结果冒充具体 GitHub run 的结论。

**修复风险：** 低。测试应准确模拟 privileged evidence，另保留真实 root/non-root 集成场景。不能仅把生产 owner 检查放宽为“任意当前用户”来使测试通过，否则会削弱 root helper 的安全边界。

### P2-11：signer 升级误分类已有 envelope 的历史请求

**位置：** [signer_service.py:170](E:/我的项目/Noyra/开发版/src/noyra/signer_service.py:170)、[signer_service.py:186](E:/我的项目/Noyra/开发版/src/noyra/signer_service.py:186)、[signer_service.py:396](E:/我的项目/Noyra/开发版/src/noyra/signer_service.py:396)。

**根因：** 从有 `transfer_json`、没有 `envelope_status` 的旧表升级时，ALTER TABLE 为所有历史行填入默认 `legacy_unbound`。后续 UPDATE 只处理 NULL 或不属于 bound/legacy_unbound 的值，因此刚填入的默认值永远不会根据已有 transfer_json 重新分类。

**触发条件：** 从已经开始保存完整 envelope 的 signer 版本升级，历史请求包含正确的 transfer_json、transfer_hash 和广播记录。

**影响与实测：** 构造该旧版表并插入完整、hash 匹配的测试 envelope，`SignerState.open()` 后得到 `has_valid_envelope=True`、`envelope_status=legacy_unbound`。receipt 会在验证 envelope 前直接以 legacy_unbound 拒绝，原本可核验的历史交易不能正常对账。没有证明重复广播或资金被释放，故为 P2。

**修复风险：** 中高。迁移必须实际 parse、校验 chain/nonce/transfer_hash 等绑定后才标 bound；不能仅以字段非 NULL 当作可信。覆盖最老无 envelope 库、完整有效 envelope、JSON 损坏、hash 不匹配、已存在状态列、迁移中断与重复启动，不得重播交易来“补证据”。

## 5. P3 详细发现

### P3-01：failed manifest 没有容量和归档清理合同

**位置：** [deployment-maintenance.py:553](E:/我的项目/Noyra/开发版/scripts/deployment-maintenance.py:553)、[retention.py:899](E:/我的项目/Noyra/开发版/src/noyra/core/retention.py:899)。

**根因：** failed 清单被永久保留，数据库 backup_retention_runs 也没有对应的保留期、归档证明或显式 purge 协议；当前 retention 的 run_history 清理的是另一张 retention_runs 表。清单没有自己的总字节预算或按失败数量告警。

**触发条件与影响：** 大计划反复失败时，证据文件和审计行持续增长。保留失败证据本身是正确的保护，本项是容量治理残余风险，不是要求立即自动删除；P1-02 的 UID 冲突另计。持续时间长、计划大时仍可能耗尽数据卷。

**修复风险：** 中。设计 archive/export→确认归档→到期 purge 的独立操作，保留内容 digest、大小、run 状态和 purge audit；在实现前至少显示文件数量、总大小与最老失败时间。禁止把 failed 再次当 orphan 静默删除。

### P3-02：capability export 没有可操作的管理入口

**位置：** [capabilities.py:92](E:/我的项目/Noyra/开发版/src/noyra/model/capabilities.py:92)、[service.py:4537](E:/我的项目/Noyra/开发版/src/noyra/service.py:4537)、[service.py:8562](E:/我的项目/Noyra/开发版/src/noyra/service.py:8562)、[m24-consciousness-routing.md:23](E:/我的项目/Noyra/开发版/docs/implementation/m24-consciousness-routing.md:23)。

**根因：** `StoredCapability.export()` 提供所需结构，但 production API 与 probe 只返回 public projection，没有 export 路由、CLI 子命令或 UI 按钮；实际调用 `.export()` 的现成示例主要在测试。文档却要求从 authenticated probe 导出 profile 文件进行新机 provisioning。

**触发条件与影响：** 首次配置环境 reasoning profile、新服务器导入、profile 轮换。操作员必须手工重建结构或调用内部 Python store，容易漏字段和弄错 revision/digest，也加剧 P2-09 的文件生命周期困惑。已有受保护文件导入能力可供高级操作员使用，所以本项按较低产品风险 P3，而不是数据安全 P1。

**修复风险：** 中。增加认证导出/预检入口，显示 endpoint/model/revision/expiry、保持无 key；产品测试应覆盖真实“probe→导出→新库导入→启动”流程，而不只验证内部方法 round-trip。

### P3-03：README 当前 schema 与代码不符

**位置：** [README.md:69](E:/我的项目/Noyra/开发版/README.md:69)、[database.py:313](E:/我的项目/Noyra/开发版/src/noyra/core/database.py:313)。

**根因：** 顶层“当前能力”描述仍写当前 schema 为 62，而 `CURRENT_SCHEMA_VERSION` 已为 99，前者没有明确标为仅描述历史发布。

**触发条件与影响：** 新部署者根据 README 选择旧备份、排查 schema 升级，或评估当前钱包/迁移能力时，会误解当前版本。运行时数据库版本拒绝规则仍在，未证明因此自动降级写库。

**修复风险：** 低。把历史发行版说明与当前开发版本明确标注，统一中英文入口，并对“当前 schema”增加生成或检查；不要把历史验收证据同步改写成当前 SHA 已通过。

## 6. 验证记录

### 6.1 静态与前端门禁

| 检查 | 本轮结果 |
| --- | --- |
| `python -m ruff check .` | 通过 |
| `python -m ruff format --check .` | 687 个文件通过 |
| `python -m compileall -q src scripts` | 通过 |
| `python -m pip check` | 通过 |
| `pip_audit`：`requirements.lock` + `requirements-cloud.lock` | 使用 UTF-8 环境后通过；漏洞数据库未返回已知漏洞 |
| `pip_audit`：`requirements-dev.lock` | 使用 UTF-8 环境后通过；漏洞数据库未返回已知漏洞 |
| `python -m mypy --python-version 3.12 src tests` | 435 个文件通过 |
| 默认 `python -m mypy src tests` | 受本机环境阻塞：全局 NumPy stub 含 Python 3.12 type statement，而项目默认 target 为 3.11；不将其直接记为已证明的产品缺陷 |
| Node 语法检查与 `node --test tests/frontend/*.test.cjs` | Node v24.15.0，116 个测试通过 |

### 6.2 Python 与权限环境

- Windows 全量 `python -m pytest -q`：**2704 passed，59 skipped，288 subtests passed**，耗时 5872.00 秒（1:37:51），退出码 0。
- 59 项跳过包括 POSIX 权限/root/systemd 相关合同、Windows 8.3 别名环境限制，以及当前 Windows 环境没有安装 boto3 的 3 个 cloud 相关测试；不能据此宣称相应分支已通过。依赖 lock 漏洞扫描通过也不等于这些 cloud 运行测试已执行。
- WSL Ubuntu / Python 3.12.3 / UID 65534，3 项 GC 清单测试：3 failed。
- 相同 WSL / UID 0 / 相同测试：3 passed。
- 新的独立复现没有修改仓库测试或产品源码，均使用临时数据和虚构凭据。
- 全量测试绿色也不能关闭上文通过额外跨组件复现确认的问题：现有测试尚未覆盖这些状态组合。
- 默认 mypy 的 NumPy stub 阻塞仅发生在当前 Windows 工具环境；WSL Python 没有安装 mypy。本轮没有为了得到绿色结果修改依赖、安装工具或调低项目门禁。

### 6.3 复现摘要

| 编号 | 操作序列 | 观察值 |
| --- | --- | --- |
| P1-01 | revision 1 导入并读取 → revision 2 导入 → 再读取 | 第二次导入返回 1，随后 state hash mismatch |
| P1-02 | root 写清单 → 子进程 setuid/setgid 65534 → 权限检查/hardening | owner 不符；chmod PermissionError |
| P2-01 | 6 个回环连接只发送半个 header | 6 个 request thread，socket timeout=None |
| P2-02 | mock RPC 返回带 3 MB padding 的 JSON | 缓冲 3,000,068 bytes，正常返回 result |
| P2-03 | 不存在的 backup root；既有 .bak 替换成测试 symlink | 自动创建目录；链接指向的 sentinel 被覆盖 |
| P2-04 | production，无 storage profile，backup missing | storage-check exit 0；Settings 默认 recovery_capable |
| P2-05 | batch=1，release+backup → partial → 重建只剩 backup 的计划；另测 protected set 改变 | 合法续跑兼容性 False；多次 plan_changed，run 仍 planned |
| P2-07 | page size 降为 1，disabled 排前、active 排后 | active()=[]，直接 get 仍为 active |
| P2-08 | configure → revoke_key → 同环境 proposal 同步 | 同步返回；group active；select_key=None |
| P2-09 | 文件 revision 1 导入 → 在线 probe revision 2 → 再导入旧文件 | revision rollback |
| P2-11 | 带 transfer_json 的旧表 → SignerState.open | 完整 envelope 被标为 legacy_unbound |

容量、DNS 变化、真实 tunnel 行为与性能上限的影响有源码依据，但本轮没有在真实上游或生产主机施加压力。对页大小与 batch 大小的缩减是边界复现，不是声称生产阈值本身发生改变。

## 7. 修复顺序与重新验收建议

1. **先阻止写出坏状态和正常启动失败：P1-01、P1-02。** 两个问题分别定义数据与权限边界，修复独立提交。尤其不能用放宽完整性/owner 检查来绕过症状。
2. **恢复 Linux 质量门禁：P2-10，并增加跨 UID 集成覆盖。** 普通测试模拟与 privileged 集成各自保留，保证 Windows 通过不能掩盖 POSIX 合同。
3. **补 signer 的网络与迁移边界：P2-01、P2-02、P2-11。** 共享 bounded I/O 基础设施与有证据的 envelope 迁移；保持未知付款不自动重试、不释放资金。
4. **统一部署和配置生命周期：P2-03、P2-04、P2-09。** 对所有入口使用同一解析/权限合同，明确 bootstrap 与持续配置的差别。
5. **修复资源选择与同步：P2-07、P2-08。** 精确查询、SQL 状态过滤、撤销意图不被重启覆盖。
6. **完成 retention 的运行合同：P2-05、P2-06、P3-01。** 明确 supersede/续跑，再优化总工作量，最后落实证据容量与归档清理。
7. **补产品入口和文档：P3-02、P3-03。** 用可操作流程验证 provisioning；文档表达不能替代代码与环境测试。

重新验收应包括 profile revision 真正递增的导入测试、root GC 与 service UID 同时运行、失败 sidecar 留存后的服务重启、Ubuntu 非 root suite、真实受限 RPC/代理请求，以及配置文件更新后的二次重启。没有必要重跑已无关的真实链上转账来验证纯 profile 哈希或目录权限修复。

## 8. 未升级为已确认漏洞的边界与限制

- root、同 UID 恶意进程以及管理员并发挂载/卸载不等于普通远程攻击者；本轮没有把这些威胁假设泛化成任意代码执行漏洞。路径检查与实际 open 不是原子的 mount namespace/fd 合同，仍应作为后续隔离设计明确说明。
- profile 导入的未来时间、超长 TTL 和未知正整数版本目前没有完整策略校验，但输入来自受保护的操作员文件；尚未证明在当前参数解析下产生越权后果，不计入 P0/P1，也没有据此宣称未来格式兼容。
- 管理 token、API key、真实消息、钱包私钥和生产数据库没有进入报告；复现使用独立临时数据。
- WSL 的 UID/权限复现证明了 POSIX 合同冲突，不代表已完成真实 Ubuntu systemd、LUKS、断电或跨机迁移验收。
- 本轮没有读取 GitHub 实际 CI 状态、部署生产代码或触发真实 provider/RPC/转账；没有将外部环境未验收简单计作产品缺陷。
- 新报告不对整个项目给出完成百分比。修复完成度应按确定的安全与功能合同逐项给证据，不能由“没有旧错误码”或“单平台测试通过”推导。
