# Noyra 当前工作区独立代码审计

审计日期：2026-10-20。审计对象：`E:\我的项目\Noyra\开发版`。本报告只记录问题及修复建议，未实施修复。

## 1. 审计结论及适用范围

本轮确认 **13 项问题：P1 高风险 2 项、P2 中风险 6 项、P3 低风险 5 项**。其中12项涉及运行/兼容库源码，1项涉及测试隔离；12项有隔离环境中的最小行为复现，1项为能够从调用链直接确认的资源复杂度问题。未确认 P0 问题；这不构成“没有 P0 问题”的保证。

优先处理文件发布与运行租约之间的边界，以及迁移请求的整体时间预算。普通网页读取、钱包余额读取也存在撤销检查缺口，适宜共同梳理最终发送点。两项旧版传输问题没有发现当前生产调用点，已据此降低优先级。签名器问题的成立条件包含调用方持有有效 bearer 权限，不能理解为匿名签名漏洞。

### 1.1 审计基线

- 分支：`main`；HEAD：`c67526d6b28f995823936687d9c59bb03b612463`。
- 审计基线是**包含已有未提交修改和未跟踪源码的当前工作区**，并非只审阅该提交。
- 当前新增的 `core/maintenance_evidence.py`、`model/wire.py`、`resources/`、修复脚本和新增测试均纳入盘点。
- 通过当前源码、调用关系和隔离复现独立形成结论；没有读取既有审计报告来推导发现。因此本报告不判断某项是否“历史首次发现”。
- 审计前对源码、脚本、测试、部署和关键构建配置中的 **589 个文件**建立 SHA-256 基线。最终完整性结果见第 6 节。
- 未运行真实迁移、发布、部署、付款或链上广播；未调用真实模型/余额提供商；复现使用临时数据库、临时目录、临时生成的密钥、模拟 HTTP transport 或本机测试监听器。没有读取生产密钥。

### 1.2 覆盖方式与边界

Python 文件盘点：`src` 218 个 / 143,087 行，`scripts` 20 个 / 5,315 行，`tests` 222 个 / 85,826 行，共 460 个 Python 文件。行数包含注释、空行和声明，并非独立逻辑数。

对全部 Python 文件进行了语法树解析、函数/类盘点及危险操作和安全边界扫描；对权限、租约、外部请求、文件发布、迁移、签名、XML、静态存储权限和动作账本等关键链路进行了逐函数、逐语句追踪，并在能安全构造条件的地方做了行为复现。另检查了前端脚本、Shell/PowerShell、部署单元与 CI 配置。第 7 节区分扫描覆盖和精读覆盖。

**这不是对 143,087 行生产 Python 逐行人工精读完成的声明。** 全量静态盘点、关键路径精读、测试通过分别只能说明各自的覆盖范围；不能把未记录的问题视为已经证明安全。真实 Ubuntu/systemd、Docker、Windows ACL/8.3 别名、Python 3.11 和真实外部服务的完整组合并未全部重建。

### 1.3 两套风险等级

“问题风险”描述当前缺陷在触发条件下的后果；“修复风险”描述实现修补时引入回归、兼容破坏或新的安全缺口的可能性。二者独立，不能用“容易修复”降低问题严重性。

| 问题级别 | 本报告含义 |
| --- | --- |
| P0 严重 | 已有证据支持广泛可触达的系统接管、密钥泄露或重大不可逆损失，应立即隔离。 |
| P1 高 | 破坏关键停止/发布边界，或使重要迁移及关闭流程失去明确的完成上界；可能需要并发或功能启用条件。 |
| P2 中 | 授权撤销失效、输入防护绕过、条件性签名越界或可触发的资源放大，需要排期修复。 |
| P3 低 | 使用范围较小、旧兼容接口或上游信任条件较强的缺陷；仍应修复或明确停用。 |

| 修复等级 | 本报告含义 |
| --- | --- |
| 低 | 局部算法/边界修正，可用少量明确用例覆盖，基本不改变协议。 |
| 中 | 涉及外部接口、并发取消、解析兼容性或文件发布行为，需要调用端和失败路径回归。 |
| 高 | 涉及租约提交原子性、迁移主权/激活状态、失败关闭和安全检查时效，错误修补可能造成更严重的问题。 |

这里的 P0–P3 是项目处置优先级，不等同于 CVSS。条件性发现均在各条目中明确列出。

## 2. 问题总表

| ID | 问题 | 风险 | 修复风险 | 触发/暴露范围 | 证据 |
| --- | --- | --- | --- | --- | --- |
| A01 | 文件写入在运行租约失效后仍可发布，动作终态又无法记账 | P1 | 高 | 已获授权的文件动作与暂停/隔离/迁移封锁并发 | 实际文件 + 临时数据库复现 |
| A02 | 迁移客户端只有空闲超时，缺少整体 deadline；关闭等待无上限 | P1 | 高 | 已启用并开始的迁移，目标或 DNS 持续迟滞 | 本机滴流复现 + 调用链 |
| A03 | 普通网页读取没有最终发送前的授权/租约检查 | P2 | 中 | 排队/建连过程中撤销网页 grant 或使租约失效 | 模拟 transport 复现 |
| A04 | 钱包余额读取在资产撤销/租约失效后继续发出 RPC | P2 | 中 | 已配置的只读余额查询，两个 RPC 之间发生撤销 | 模拟 RPC + 真实元数据存储复现 |
| A05 | 微信 XML 声明过滤可被注释内伪标签绕过，且解析早于鉴权 | P2 | 中 | 启用的 XML 回调入口可达 | 108 字节输入复现 |
| A06 | 迁移目标代理鉴权前没有连接线程上限和请求头期限 | P2 | 中 | 能访问 loopback 代理，或运维将其转发暴露 | 6 个本机连接复现 |
| A07 | 迁移身份证明接口允许签任意个人消息，服务端未约束用途 | P2（条件性） | 中 | 已持有签名器有效 bearer，并存在同地址的其他签名用途 | 临时账户签名/恢复地址复现 |
| A08 | 匿名就绪/部分 API 请求同步触发全私有目录权限遍历 | P2 | 高 | 生产静态存储保护启用，私有目录随时间增长 | 静态调用链和复杂度确认 |
| A09 | 搜索 RSS 的字节级 XML 过滤可被 UTF-16 绕过 | P3 | 低至中 | 能影响可信搜索上游响应或替代 provider | 388 字节输入复现 |
| A10 | 旧传输接口在最后一个短块处中断后无法续传 | P3 | 低 | 使用兼容 TransferSession，暂停恰逢短尾块 | 4,097 字节文件复现 |
| A11 | 旧加密接收接口失败时会破坏旧文件或留下未验证结果 | P3 | 中 | 使用兼容 EncryptedTransferSession.receive | 两类损坏/错误回执复现 |
| A12 | root开发/恢复目录被误判为安装控制目录，导致备份失败 | P3 | 中 | POSIX root运行、普通迁移目录非0700、执行加密备份 | root/nobody对照复现 |
| A13 | 真实服务UID测试未隔离宿主存储身份文件，结果受宿主配置干扰 | P3（测试可靠性） | 低 | root执行权限边界测试，宿主存在受限/etc/noyra | 原样失败与仅内存隔离后通过 |

## 3. 逐项发现

### A01 — 文件发布没有受运行租约约束，失效后仍可写入

**风险：P1；修复风险：高；置信度：高。**

**代码证据与调用链。** `src/noyra/capability/tools.py:235–283` 中，`write_text()`先消耗 grant、把动作置为 executing，再调用 `write_atomic()`；`:260–262` 的 `before_publish` 只进入 `_revalidate_file_target()`。该方法位于 `:285–303`，通过 `CapabilityStore.revalidate_use()`检查 grant 和目标路径，未检查运行租约。`src/noyra/capability/store.py:174–201` 的重新校验采用读取连接，也不会触发数据库提交栅栏。

`src/noyra/capability/filesystem.py:156–205` 中，临时文件刷新后在 `:188` 调用回调，随后 `:191/:193` rename/replace 发布。这里没有与 `RuntimeAdmissionGate` 的租约检查和失效操作建立共同的原子边界。另一方面，`src/noyra/core/database.py:7345` 和 `core/admission.py:368–379` 对持久数据库写入使用 `current_commit_scope()`；于是文件能变化，后续 `actions.finish()`反而因租约失效被拒绝。

**触发条件。** 调用开始时 subject、文件 grant 和路径均合法，动作已经开始；在文件发布前，暂停、隔离或迁移操作使绑定租约失效。无需攻击者控制目标路径，也不依赖符号链接绕过。仅有 grant 撤销与运行租约失效是不同事件：已有 grant 复查不能替代运行租约检查。

**隔离复现。** 临时数据库中创建合法 filesystem_write grant，在有效运行操作上下文内执行实际安全文件写入；只在内存中包裹 `capability.tools.write_atomic`，在调用真实写入函数之前执行 `gate.invalidate()`，然后读取结果：

```text
目标文件内容 = changed-after-pause
调用结果 = OperationInvalidated
actions.status = executing
actions.result_json = null
```

真实 Windows 文件已被发布，失败发生在后续记账。没有修改源码来制造缺口。

**根因。** 文件系统副作用与运行 admission/epoch 的保护仅存在于各自的局部组件，缺少统一的发布线性化点。终态记账又完全沿用普通运行提交检查，没有在失效后以窄范围终结动作。

**影响。** 用户暂停或隔离之后仍可能观察到文件变化；动作账本与实际副作用不一致。`src/noyra/cognition/deliberation.py:681–696` 将 prepared/executing/unknown 视为未完成动作，可能阻止后续决策；`core/runtime.py:264` 的启动恢复不能保证同一运行期间自动解除该状态。没有证据支持越出已授权目录的任意文件写入。

**建议修复。** 给最终 rename/replace 建立租约检查与暂停之间明确的线性化关系，使用已有的适当 gate scope 或等价原子协议，并保留路径/grant 的即时复查。将失败后终态结算限制在动作记账，明确“发布前取消”和“发布后失效”的不同结果；不得把文件写入本身放进 `accounting_scope()`绕开栅栏。

**修复为何高风险。** 锁持有范围过大会阻塞暂停/迁移；只增加一次 `assert_current()`仍可能在检查与 rename 之间留下窗口；无边界放宽数据库提交又会允许失效任务继续改写主体状态。

**必要回归。** 发布前失效时旧文件不变且无新文件；发布后失效时事实和账本一致；授权撤销、路径替换、重复调用、磁盘失败都保留原有安全行为；动作不永久停在 executing。

### A02 — 迁移网络请求缺少整体期限，源端封锁和关闭可被拖住

**风险：P1；修复风险：高；置信度：高。**

**代码证据。** `src/noyra/migration/http_executor.py:192–238` 的 `UrllibHTTPTransport.request()`将 timeout 传给 `_PinnedHTTPSConnection`，然后同步 `request()`、`getresponse()`、`response.read(MAX_RESPONSE_BYTES + 1)`（`:221–225`）。`:240–262` 的同步 `socket.getaddrinfo()`没有整体期限。`:297–311` 对每个已验证地址分别使用完整 timeout 建连，也没有扣减已消耗时间。

执行链在 `http_executor.py:353` 获取 source fence 后继续进行制包、传输、恢复、健康检查和激活。`src/noyra/service.py:1762–1788` 的单个迁移 worker 与 `:1937–1958` 的关闭逻辑相连，其中 `:1950` 对该 worker 调用没有 timeout 的 `join()`。上游 `src/noyra/migration/runtime.py:263–326` 还涉及迁移运行锁。

**触发条件。** 迁移已经启用且通过必要授权/前置检查；远端目标、代理或 DNS 异常。若迟滞发生在 source fence 之后，源端会在等待期间保持封锁。普通未启用迁移的服务不通过这条路径暴露。

**隔离复现。** 本机 HTTP 测试服务每 0.21 秒发送一个字节，共发送 7 字节 JSON。保留 `http.client` 的真实读取逻辑，只用普通 `HTTPConnection`替代 TLS 建连工厂；配置 timeout=1 秒，完整调用仍在 **1.267 秒**后成功返回 `{"x":1}`。这验证的是 socket 空闲超时与请求总时长的区别，没有模拟真实 TLS 部署，也没有执行长时间拒绝服务。

**根因。** 把单次 socket 等待上限当成端到端时间预算；DNS、多地址重试、TLS、响应头和响应体没有共享单调时钟 deadline。最终关闭路径假定 worker 总会自行结束。

**影响。** 持续有少量数据到达即可把等待拉长到明显超过配置值；DNS也没有该层定义的截止时间。迁移 worker 长期被占用，后续迁移无法进入；发生在 fence 后时源端可能长期不可写，关闭流程也没有明确完成上界。未测得实际生产停机时长，不声称仅凭本次 1.267 秒实验已经造成永久挂死。

**建议修复。** 为整个迁移步骤/阶段传播单调时钟 deadline，并在 DNS、逐地址连接、TLS、请求头、流读取及重试中扣减剩余预算。提供可取消且可回收的执行机制与有界关闭等待。若激活请求已发送但应答超时，应进入“目标可能已经激活”的对账状态。

**修复为何高风险。** 超时后直接释放 source fence 可能造成双活；把 worker 改为 daemon 并不能解决激活不确定性或资源回收。必须把网络取消与主权/激活协议一起设计，确认目标状态后才决定恢复源端。

**必要回归。** 逐字节滴流、永不结束的响应、DNS迟滞、多地址失败、TLS停滞、激活后应答丢失、关闭并发；总预算应有可验证的上限，同时不能因超时制造两个可写端。

### A03 — 普通网页读取不会在最终发送前重新检查 grant 和租约

**风险：P2；修复风险：中；置信度：高。**

**代码证据。** `src/noyra/capability/tools.py:131–201` 的 `fetch_document()`在 `:165` 调用一次 `capabilities.use()`，随后 `:179` 交给 `SafeWebReader.fetch()`。`src/noyra/world/source.py:349–448` 在 `:378` 开始 HTTP stream，没有携带 grant/运行租约的最终发送检查。生产入口包括 `src/noyra/cognition/cycle.py:429–455` 和 `cognition/deliberation.py:411–469`。可对比 `research/browser.py` 已有的发送 trace 边界，但这里未使用同类约束。

**触发条件。** 初始网页读取合法，等待连接、DNS或 transport 排队时，grant 被撤销或当前运行租约失效。撤销之前开始准备请求并不等于撤销之后仍可发送。

**隔离复现。** 使用 `httpx.MockTransport`模拟建连阶段，在模拟实际发送之前执行撤销或租约失效，再记录发送时状态。无真实外网请求：

| 场景 | 发送时 grant/租约 | 请求是否有检查 trace | 最终观察 |
| --- | --- | --- | --- |
| 撤销 grant | revoked / valid | 无 | 仍发送，调用和动作均 succeeded |
| 暂停操作 | active / invalid | 无 | 仍发送，finish 抛出 OperationInvalidated，动作 executing |

**根因。** 授权消耗只发生在准备阶段；授权身份没有被传递到 transport 的实际发送边界。失效后的终态记账缺口与 A01 相同，但这里的副作用是网络读取及信息暴露，不是文件修改。

**影响。** 已被撤销的域名仍会接收到网络请求；暂停后仍可能产生流量、请求元数据泄露和结果使用。租约失效还会使动作留下未终结状态。本次没有证明 SSRF 防护、URL allowlist 或响应体限额被绕过。

**建议修复。** 保留本次实际使用的 grant 标识，将当前授权、运行 epoch 和必要源状态校验放到真正发送请求头的边界，并覆盖重定向/重试。对失效后到达的内容拒绝正常业务提交，同时允许窄范围终态记账。修复需维持现有 URL/DNS 校验和费用计数。

**修复风险说明与必要回归。** 适配真实 httpx/httpcore transport 与现有测试 transport 可能产生兼容差异；单纯在 `reader.fetch()`前增加检查仍有建连窗口。分别验证撤销发生在准备前、连接中、发出后，以及暂停后的动作终结；检查失败必须早于新增网络发送。

### A04 — 钱包只读余额 RPC 在撤销后继续执行

**风险：P2；修复风险：中；置信度：高。**

**代码证据。** `src/noyra/wallet/rpc.py:141–211` 的 `read_balance()`在 `:158–171` 一次性获取并检查 asset/address/network 的 active 状态，随后先在 `:205` 发 `eth_chainId`，再在 `:208` 发余额 RPC。两次之间未重新读取元数据、未校验运行租约。`:219–291` 的 `_request()`在 `:238` 打开 httpx stream，没有安装最终发送检查。已有总时长和响应大小限制不能代替授权检查。

生产计划查询入口 `src/noyra/wallet/acquisition.py:2722–2802` 在 `:2742` 调用 `read_balance()`，续租/后续处理在返回后才发生（`:2794`）。兼容 `acquire_balance()`还会另行持久化，本项复现没有证明那个持久化步骤能绕过数据库栅栏。

**触发条件。** 已有合法 operator/计划查询和已登记 RPC；初始检查之后，资产、相关配置状态或运行租约发生变化。复现具体覆盖资产撤销和租约失效，不把其他状态变化冒充已经实测。

**隔离复现。** 使用真实 WalletStore 临时元数据和模拟 transport。在第一个 `eth_chainId`响应处理时分别执行 `gate.invalidate()`或 `store.revoke_asset()`；随后仍观察到第二个请求：

```text
暂停场景：eth_getBalance 在 lease_valid=false 时仍被调用
撤销场景：eth_getBalance 在 asset_status=revoked 时仍被调用
两种场景 read_balance 均正常返回字符串余额 "7"
两次请求均没有发送检查 trace
```

**根因。** 活跃状态快照被当成覆盖整个多请求操作的授权，且 RPC 层只关注地址/方法白名单、超时与解析；运行有效性没有贯穿发送链路。

**影响。** 暂停或撤销之后仍向提供商发起查询，可能继续暴露地址并消耗流量/配额，且上层拿到过期授权背景下的成功结果。本接口为只读余额查询；没有发现此问题直接导致转账、私钥泄露或失效结果成功入库的证据。

**建议修复。** 对每次实际 RPC 发送检查当前租约与本次选定的 asset/address/network 身份及状态，避免撤销后复用旧对象；在结果消费处也拒绝失效结果。保留已有整体时间预算、slot 上限和精确请求计数。失败后的计划结算需要窄范围记账，不能继续业务写入。

**修复风险说明与必要回归。** 一次余额操作会发多个 RPC，不能只在 `read_balance()`入口校验；调整后需保持错误分类和 request_count 语义。验证第一个请求前、两个请求之间、结果返回后分别撤销，以及暂停、容量不足和链 ID 不匹配。

### A05 — 微信 XML 的声明拒绝逻辑可被注释内伪标签绕过

**风险：P2；修复风险：中；置信度：高。**

**代码证据。** `src/noyra/interaction/inbound.py:723–758` 的 `_parse_xml()`用 `:734` 的正则寻找“第一个元素”，只检查该位置之前的字节是否含 `<!DOCTYPE`或 `<!ENTITY`（`:735–738`），然后在 `:740` 调用 `ET.fromstring()`。正则不理解 XML 注释，注释内的 `<fake>`也会被认作第一个元素。声明因此落到检查区域之后，但仍会被真正的 XML 解析器处理。

`inbound.py:644–707` 的 `parse()`在 `:658` 调用解析器；明文签名检查在 `:694`，加密外层签名检查在 `:684`。因此成本已经在知道发送方是否持有正确秘密之前发生。HTTP/body 上限为 1,048,576 字节；节点数 1,024、深度 64、内容长度等树校验发生在解析之后。

**触发条件。** 开启微信 XML 回调，攻击方能到达该入口；声明绕过本身不需要有效回调签名。加密回调也会先解析外层 XML，但这不等于能够伪造其签名或解密。

**最小复现。** 下面 108 字节输入被 `_parse_xml()`接受：

```python
raw = b'<!-- <fake> --><!DOCTYPE xml [<!ENTITY audit "AUDIT_ENTITY_EXPANDED">]><xml><Content>&audit;</Content></xml>'
root = WeChatInboundAdapter._parse_xml(raw)
assert root.findtext("Content") == "AUDIT_ENTITY_EXPANDED"
```

**根因。** 用非结构化正则划分 XML 声明区域，混淆注释文本与真正元素；同时在未经鉴权输入上先建立完整解析树。

**影响。** 违反当前明确的“拒绝 DTD/entity”策略，内部实体确实被展开。恶意结构/实体可能在后置节点、深度或文本上限生效前消耗 CPU/内存。解析器自身的实体放大保护仍存在，不能据此推导任意规模消耗。本次未证明外部实体读取本机文件、外网请求、代码执行或认证绕过后的业务消息入库。

**建议修复。** 使用能在建立树之前明确拒绝 DTD/entity 的 XML 解析方式，或实现能够识别编码、注释和 CDATA 的严格前置扫描。明文流程可先验证不依赖解析树的签名信息；加密回调需保留必要外层提取，但用受限解析器完成。保留官方消息需要的 CDATA。

**修复风险说明与必要回归。** 粗暴拒绝所有 `<!`会误伤正常 CDATA；只移动一段正则仍有编码/注释变体。测试注释伪标签、不同编码、大小写声明、合法 CDATA、明文/加密正常回调、无效签名以及解析前的资源边界。

### A06 — 迁移目标代理缺少鉴权前连接预算

**风险：P2；修复风险：中；置信度：高。**

**代码证据。** `scripts/noyra-migration-agent.py:310–372` 采用 `BaseHTTPRequestHandler`和 HTTP/1.1。`do_POST()`直到 `:334` 才设置 accepted socket 的 10 秒 timeout；此时请求行和请求头早已由基类读取。`do_GET()`（`:350–360`）没有同类设置。`:430` 使用普通 `ThreadingHTTPServer`，`:431` 设置 daemon_threads，`:432` 设置 `server.timeout=5`，均不构成 accepted socket 的请求头 deadline或并发 worker 上限。`:74–80` 的 body read 也没有端到端期限。

默认 systemd 单元 `deploy/systemd/noyra-migration-agent.service:15` 绑定 `127.0.0.1:8876`，已有 systemd 加固。**默认不是公网监听。**

**触发条件。** 能连接本机 loopback 的进程，或运维明确将代理通过反向代理/隧道暴露；对方停留在不完整请求头阶段。HMAC 未执行便已占用线程，因此不需要持有迁移凭据。

**隔离复现。** 在本机随机端口运行同一个 Handler，建立 6 个连接，只发送部分请求头，记录 handler 的 accepted socket：

```text
connections = 6
accepted_workers = 6
accepted_socket_timeouts = [None, None, None, None, None, None]
server.timeout = 0.02
```

设置很短的 `server.timeout`也没有为这些请求建立读取期限。测试随后关闭连接；没有进行大量连接压测。初次测试支架在清理时因缺少假的 `.agent`属性打印异常，该清理异常不属于产品发现，也不是结论依据。

**根因。** 限时放在 HTTP 方法处理阶段，遗漏基类读取请求头之前的阶段；每连接线程没有容量门禁。daemon 线程仅影响进程退出等待，不限制资源占用。

**影响。** 有可达性的未鉴权客户端可以长期占用线程、socket 和相应内存，影响目标迁移服务可用性。默认 loopback 和外部 systemd/反向代理限额会影响可利用范围；本次没有证明公网匿名攻击面，也没有测量生产耗尽阈值。

**建议修复。** 在 `setup()`或更早阶段施加每连接的请求行/头/body 总期限，限制 worker、排队连接及 keep-alive，并统一异常关闭策略；保留 HMAC、body 大小和消息格式检查。反向代理和 systemd 限额可作为额外保护。

**修复风险说明与必要回归。** 单纯设 socket timeout 仍只是空闲时间上限；共享服务端应能承受较慢的合法分块传输。验证慢请求头、滴流 body、GET/POST、持久连接复用、容量拒绝以及正常迁移大块传输。

### A07 — 迁移身份证明接口没有在签名器端限制签名用途

**风险：P2（条件性）；修复风险：中；行为置信度：高，实际跨用途后果取决于部署。**

**代码证据。** `src/noyra/signer_service.py:360–370` 的 `prove_identity()`只检查 signer_id、challenge 为字符串及长度 1–4096，然后在 `:368` 执行 `account.sign_message(encode_defunct(text=challenge))`。HTTP 分发 `:661–671` 的 `/migration-identity`只限制两个字段名，没有验证 challenge 的语义。

正常客户端 `src/noyra/wallet/execution.py:403–434` 自己构造 domain=`noyra-migration-wallet/v1`、task_id、target_id、manifest_digest、target_identity、signer_id 和随机 nonce 的规范 challenge；服务端却不要求这些字段、域或绑定关系。主交易入口的链/资产限制（`signer_service.py:196–220`）不会约束这里的个人消息签名。

**触发条件与信任假设。** 调用者必须拥有有效的签名器 bearer、正确 signer_id 和连接权限。若项目希望隔离签名器把应用的签名能力限制在规定交易/迁移证明之内，这个接口突破了用途约束。若产品有意授予所有 bearer 调用方完整的通用 personal_sign 能力，应将这一点写入契约；当前迁移命名和客户端域隔离并不能实现那种权限收缩。

**隔离复现。** 用临时 `Account.create()`和临时 SignerState，`rpc=None`，直接调用真实引擎：

```text
challenge = "Synthetic unrelated authentication challenge for audit"
prove_identity 接受该无关消息
Account.recover_message 恢复到临时签名器账户地址
```

不需要真实 RPC，不涉及真实私钥。HTTP dispatch 最终直接调用同一方法，但本次没有通过真实部署 bearer 进行网络攻击。

**根因。** 把域隔离当成客户端的自愿约定，缺少私钥所在服务端的强制约束；认证解决“谁能调用”，未解决“该调用方能让私钥签什么”。

**影响。** bearer 调用方可获取同一 EOA 对任意个人消息的签名。若其他认证/授权系统接受该地址的此类签名，可能被跨用途利用。没有证明任意链交易签名、主网转账、密钥导出或匿名认证绕过。

**建议修复。** 改为接收结构化迁移上下文，由签名器自己构造固定版本、固定域的规范消息；严格验证字段、类型、任务/目标/manifest/身份绑定，并按协议需求增加 nonce/有效期。客户端与服务端共同迁移协议，不保留可由任意字符串绕过的新旧双路径。

**修复风险说明与必要回归。** 现有客户端和签名验证依赖 challenge 的字节内容，接口变化可能破坏迁移兼容。测试合法证明、任意非 JSON 文本、错误 domain、字段缺失/额外字段、上下文混用、重放策略及旧版本处理。

### A08 — 匿名请求可以反复触发全私有目录权限扫描

**风险：P2；修复风险：高；置信度：高（静态调用链），性能后果未做生产规模实测。**

**代码证据。** `src/noyra/service.py:2921–2956` 允许匿名访问 `/health/ready`；`:2926–2929` 仅对详细管理员 readiness 路径鉴权，匿名路径在 `:2932` 同样调用 `at_rest.health()`。`:3235–3244` 对其他部分 `/api/`路径在后续路由鉴权之前调用 `_at_rest_available()`；`:9412–9419` 又进入 `guard.require_ready()`。

`src/noyra/core/at_rest.py:571–607` 的 `require_ready()`/`health()`都会调用 `_private_permission_error()`。POSIX 分支 `:1374–1397` 遍历私有路径并建立整个 metadata 列表；`:1452–1476` 递归 `scandir`且对每层目录排序。Windows 分支 `:1880–1964` 使用 PowerShell 枚举条目和审计 ACL。普通 `/health`已有缓存，以上入口没有共用该缓存结果。

**触发条件。** at-rest 保护实际启用，私有路径中存在持续增长的数据/文件；请求能到达 readiness 或相关 API 分发。开发模式不强制权限审计时不能直接套用此影响。服务的 worker 上限和每客户端速率限制会影响并发，但不限制一次请求扫描的目录规模。

**确认方式。** 逐层确认匿名路由、guard、递归遍历和 metadata 收集的同步调用关系。对健康且可遍历的目录，每次检查至少与文件条目数 N 成比例，排序还增加额外成本；POSIX 聚合列表占用 O(N)内存，Windows额外承担进程/ACL枚举成本。没有使用真实数据目录进行压力测试，也没有给出无依据的每秒请求或停机数字。

**根因。** 将应有明确调度和工作预算的全树安全审计放到高频、部分未鉴权的读请求关键路径，并且未复用有时效约束的健康快照。

**影响。** 数据增长后，少量小请求也可能放大成大量元数据访问/ACL枚举；多请求竞争有限 worker，影响 API延迟和 readiness，可能引发负载均衡摘除或重启循环。后两种运维后果取决于实际部署探针策略，未在本机实验中复现。

**建议修复。** 设计有工作预算的后台/增量审计，结果携带 epoch、检查时间、完整性和失效条件；请求只做便宜的关键边界检查与合适时效的快照判断。卷/根目录身份变化、权限修改、无法完成扫描等情况必须使结果失效并按安全策略关闭。

**修复为何高风险。** 直接移除权限审计或长期缓存会引入实际的静态存储安全缺口。需要同时解决安全结果的新鲜度、文件变更、挂载切换和扫描预算，不能把可用性修复变成放行已失效的保护。

**必要回归。** 不同目录规模下单次请求成本有明确上界；并发匿名请求不触发重复全树工作；权限/卷身份变化及时使 readiness 失败；扫描异常、恢复和缓存过期行为可验证。

### A09 — RSS 声明过滤不能识别 UTF-16 中的 DTD/entity

**风险：P3；修复风险：低至中；置信度：高。**

**代码证据。** `src/noyra/research/browser.py:251–278` 的 `_parse()`对原始 `response.content.upper()`检查 ASCII 字节串 `<!DOCTYPE`/`<!ENTITY`（`:253–255`），随后在 `:257` 交给能识别 XML 编码的 ElementTree。UTF-16 中字符被零字节隔开，原始 ASCII 子串匹配失败。

**触发条件。** 搜索上游返回受影响的 RSS。默认端点是固定 HTTPS Bing RSS（`browser.py:29`），不是用户可随意指定的 XML 地址；需可信上游响应受控或替代 provider/transport 交付此类内容。与直接可达的微信回调相比，默认暴露条件更强，因此定为 P3。

**隔离复现。** 将含内部实体的 RSS 编码为 UTF-16，共 388 字节，传入真实解析方法：

```python
xml = ('<?xml version="1.0" encoding="utf-16"?>'
       '<!DOCTYPE rss [<!ENTITY audit "AUDIT_ENTITY_EXPANDED">]>'
       '<rss><channel><item><link>https://example.com/</link>'
       '<title>&audit;</title></item></channel></rss>').encode('utf-16')
results = BrowserSearchExecutor._parse(httpx.Response(200, content=xml), 2)
assert results[0].title == "AUDIT_ENTITY_EXPANDED"
```

**根因。** 声明检测与真正解析使用了不同的字符解释方式。原始网络 body 限额并不能直接约束实体展开后的内存。

**影响。** 内部实体能够进入搜索结果，声明拒绝策略被绕过，存在解析资源放大风险。未证明外部实体读取、远程执行或普通搜索关键词能直接控制 RSS 原文。底层解析器的放大防护仍会限制部分攻击。

**建议修复。** 使用解析前明确拒绝 DTD/entity 的编码感知方案；可以与 A05 共用严格解析组件，但按各入口的协议格式分别验证。不能只把 `.upper()`换成另一种字节搜索。

**修复风险说明与必要回归。** 局部改动较小，但要确认合法编码和正常 RSS 可解析。测试 UTF-8、UTF-16 LE/BE、注释、CDATA、正常 RSS、错误编码和受限解析失败；不得误把 URL规范化/响应限额当成实体防护。

### A10 — 旧版分块传输在最后短块处停止后无法续传

**风险：P3；修复风险：低；置信度：高。**

**代码证据。** `src/noyra/migration/transfer.py:53–115` 的 `TransferSession.send()`在 `:91–103` 达到 stop_after_chunks 后立即返回 complete=False，即使刚写完的就是最后一个短块。`resume()`（`:117–169`）在 `:135` 把目标前缀长度计算为 `next_chunk * chunk_bytes`，默认已记录的所有块都为整块。

**触发条件。** 调用兼容类、文件长度不是块大小的整数倍，停止点恰好包含最后短块，然后拿该回执续传。对当前 `src`和`scripts`的引用检索只发现定义、内部引用和 `migration/__init__.py`导出；没有发现当前生产迁移执行链调用这两个旧传输类。活跃迁移使用 HTTP executor/bundle 链路。

**隔离复现。** 文件 4,097 字节，chunk_bytes=4,096；`send(..., stop_after_chunks=2)`写完所有数据却返回不完整：

```text
receipt.complete = false
destination.size = 4097
resume 计算的 expected_prefix = 8192
异常 = ValueError: transfer resume prefix is invalid
```

**根因。** 回执用块索引表示进度，续传却没有把最后短块映射回真实字节偏移；完成状态只在下一次空读之后生成。

**影响。** 该兼容接口的断点恢复会拒绝自身生成的合法回执，必须重传或额外处理。不能据此声称当前生产 HTTP 迁移不可续传。

**建议修复。** 明确记录/验证真实偏移，或在严格检查回执后按 `min(next_chunk * chunk_bytes, byte_size)`计算尾部进度，并处理 EOF已到达的完成状态；同时校验块大小、块数和 hash 列表的一致性。

**必要回归。** 空文件、单个短块、整块整数倍、最后短块、任意有效中断点、重复 resume 和篡改回执。局部算法修改无需改变当前生产迁移协议。

### A11 — 旧版加密接收不具备失败原子性

**风险：P3；修复风险：中；置信度：高。**

**代码证据。** `src/noyra/migration/transfer.py:260–323` 的 `EncryptedTransferSession.receive()`在 `:285` 用 `destination_path.open("wb")`打开最终目标，然后才在 `:286`起检查 magic、nonce 和加密块。已有文件此时已被截断。`:314–319` 的异常处理无条件尝试删除目标路径；而最终字节数/整文件 hash 校验（`:320–323`）位于这个清理范围之外。

**触发条件。** 调用该兼容接收接口，目标已经存在且输入加密数据损坏；或输入块有效但回执的总长度/整文件摘要不匹配。生产调用范围限制同 A10，未发现当前活跃执行链调用。

**隔离复现。** 两个独立场景均使用 4,097 字节临时数据：

| 场景 | 输入条件 | 异常后实际状态 |
| --- | --- | --- |
| 已有目标内容为 `preexisting-data` | 篡改最后密文字节 | 抛 `encrypted transfer decrypt failed`，原目标已不存在 |
| 有效密文和块 hash | 将回执 artifact_hash 改为错误值 | 抛 `encrypted transfer artifact hash mismatch`，4,097字节未通过完整校验的输出仍留在最终路径 |

**根因。** 没有“私有临时文件→完整验证→原子发布”的阶段分离；异常清理既无法恢复已覆盖的旧文件，又未覆盖全部验证步骤。

**影响。** 使用该接口时，坏输入可破坏原有目标；最终摘要失败却残留输出，也容易被后续流程误认为正式文件。本次损失发生在明确传入的目标路径，不是越目录写入；由于缺少当前生产调用点，维持 P3，而不是把兼容库问题升级为正在发生的生产数据丢失。

**建议修复。** 在目标所在受保护目录创建独占临时文件，所有解密、块摘要、总大小和整体摘要完成后再按明确的覆盖策略原子发布；失败只清理本次临时文件。确需覆盖旧文件时定义 fsync/目录持久性策略，并保留原始异常类别。

**修复风险说明与必要回归。** 成功覆盖行为、同盘原子 rename 和崩溃持久性会影响现有调用者。验证已有目标遇到坏 magic/坏tag/截断/尾随数据/错大小/错hash时保持不变，失败不留正式输出，正常接收可重复且符合既定覆盖策略。

### A12 — root普通运行目录会被误认成特权迁移控制目录

**风险：P3；修复风险：中；置信度：高。**

**代码证据。** `src/noyra/core/at_rest.py:1577–1583` 的 `_installed_migration_control()`在POSIX下仅以“路径恰好是data_root/migration、owner为root、mode不是0700”认定这是安装器特权布局。`core/runtime.py:361–365` 的 `_sync_migration_source_epoch()`却会在普通运行目录下以 `source_root.mkdir(mode=0o750, parents=True)`生成迁移源目录，自动建立的中间`migration`目录通常为0755。root运行时，这正好命中前述安装判断。

`at_rest.py:857–865` 的备份路径据此进入 `_migration_service_paths()`；`:1595–1604`、`:1620`要求完整的root/service分权布局和0750模式，而普通运行目录没有这种安装布局，于是在备份时拒绝。

**触发条件。** POSIX下以root运行开发、测试或临时恢复实例，普通migration目录为0755/0750等非0700模式，随后停止实例并创建加密备份。默认systemd服务使用独立非root账户，本项不表示其正常安装布局必然失败。

**隔离复现。** 原始 `test_service_boot_and_restore_without_intents_keep_chat_and_memory`在root环境重复失败于同一布局检查。另在临时目录生成相同普通迁移结构、真实数据库与临时keyring，分别以root和nobody执行实际备份：

| 用户 | migration模式 | 安装布局判定 | 备份结果 |
| --- | --- | --- | --- |
| uid=0 | 0755 | true | AtRestError: migration control directory permissions are invalid |
| uid=65534 | 0755 | false | succeeded |

后一对照直接调用备份组件，不依赖宿主`/etc/noyra`配置，明确把该缺陷与A13分开。

**根因。** 将所有权和宽泛权限差异当成安装身份，而这些元数据也可能由正常root开发流程自然生成；没有区分“普通root私有数据”与“由安装器建立的特权分权控制树”。

**影响。** root运行场景中，原始数据和聊天/记忆尚在，但备份/恢复验收无法完成；相同目录分类也被私有路径审计使用。未发现数据被删除、备份明文泄露或默认非root生产服务受损。

**建议修复。** 使用明确、可信的安装布局上下文/标记或基于数据根所有者的清晰规则区分两类布局，并让普通运行目录从创建起使用一致的私有权限。遇到真正的特权布局损坏仍必须拒绝，不能以“不是完整布局”简单降级为普通树。

**修复风险说明与必要回归。** 修改启发式可能误把真实root控制目录纳入服务可改写/可遍历范围，故修复风险为中。覆盖root开发运行、非root开发运行、正确安装分权布局、模式被篡改的安装目录、备份和恢复；两个现有安全语义必须同时保留。

### A13 — 权限边界测试没有完整隔离宿主配置

**风险：P3（测试可靠性，不是未鉴权运行漏洞）；修复风险：低；置信度：高。**

**代码证据。** `tests/test_migration_storage_boundary.py:52–131` 使用真实uid=65534子进程、复制源码及临时数据根；`:79–81`只隔离`upgrade_channel`，`:82–85`注释声称宿主配置已隔离，但没有重定向 `src/noyra/core/storage_mode.py:44–46` 的两项全局存储身份路径。子进程启动的 `NoyraService`在 `service.py:10582`仍进入 `storage_mode.py:400–401`读取宿主固定路径。

**触发条件。** 以root运行该测试，宿主存在受限的`/etc/noyra`，测试用的nobody不属于真实服务组。此时甚至不存在storage-identity文件，子进程也可能因无法搜索其父目录而在`lstat()`得到PermissionError。

**隔离复现。** 本轮只读取非敏感的目录/文件元数据：宿主`/etc/noyra`为root:gid989、0750，身份文件本身不存在。原始测试连续两次失败，错误为`storage_identity_unreadable`。不修改源文件、不改变宿主权限，只在父进程测试支架中将该子进程代码的两项存储身份常量重定向到本次临时根下的不存在文件，原测试 **1 passed，6.58秒**。真实UID、目录权限、服务启动、加密备份和恢复逻辑保持不变。

**根因。** 测试隔离只覆盖了旧的全局配置入口，未覆盖新增的存储身份文件；宿主环境成为隐式依赖。

**影响。** 已部署/部分配置过的审计机或开发机可能报与fixture无关的失败，遮蔽真正权限边界结论，或诱使维护者错误放宽宿主权限来“修测试”。未证明生产的fail-closed存储身份检查应被放宽；这个发现针对测试可重复性。

**建议修复。** 在测试子进程中显式注入临时配置路径，完整隔离数据/备份身份及所有机器全局配置，保留真实服务UID与真实文件权限验证。可补充有受限宿主目录时的测试支架自检。

**必要回归。** 在干净宿主和存在受限`/etc/noyra`的宿主上得到一致结果；不读写真实配置；不得通过chmod真实`/etc/noyra`或忽略生产PermissionError来使测试通过。

## 4. 建议修复顺序及共同边界

1. **先明确 A01 的发布线性化与终态记账。** 这决定暂停/隔离在真正副作用处的含义；A03/A04可以共享发送前租约检查和失败后窄范围记账机制，但文件发布与网络发送仍需各自合适的原子边界。
2. **对已启用迁移的部署优先处理 A02/A06。** 客户端整体期限与服务端连接预算应一起验证。若尚未启用迁移，可据实际配置安排优先级，但不能用 daemon线程或自动解封替代协议处理。
3. **处理 A05，并连同 A09统一 XML拒绝策略。** 对外回调的优先级高于固定可信搜索上游；保留 CDATA等正常协议能力。
4. **收紧 A07的签名用途，并先确认服务端权限契约。** 如果 bearer本来就代表通用个人签名授权，应明确文档和密钥用途；如果它应代表迁移/指定交易能力，就要在私钥服务端强制限制。
5. **为 A08设计有时效与失效规则的后台审计。** 需要单独评审安全和可用性的折衷，不能简单去掉检查。
6. **修复或正式停用 A10/A11。** 停用应检查外部使用者；仅删除当前仓库内调用不会自动消除已导出兼容 API的责任。
7. **补上A12的root开发/恢复兼容性，并单独修复A13的测试隔离。** 不通过放宽真实安装目录权限或静态存储检查来使测试变绿。

以上是处置建议，不代表本轮已修改实现或已验证修复有效。每条“必要回归”都是将来修复的验收条件，不是本轮新增的测试文件。

## 5. 验证结果与复现证据

### 5.1 静态、类型与脚本基线

| 检查 | 结果 | 解释 |
| --- | --- | --- |
| Python AST解析 | 460/460成功 | 覆盖src/scripts/tests全部Python文件，不能代替语义审计。 |
| `python -m ruff check --no-cache src tests scripts` | 通过 | 未执行自动修复。 |
| `python -m ruff format --check --no-cache src tests scripts` | 460个文件已符合格式 | 未运行格式写入。 |
| Windows `mypy --python-version 3.12` | 通过，440个源文件 | 覆盖src/tests；临时cache位于仓库外。 |
| Linux默认项目配置 `mypy src tests` | 通过，440个源文件 | 配置目标为3.11，实际解释器为3.12.3；不等同于实际Python3.11运行通过。 |
| Windows默认项目配置mypy | 外部环境阻断 | 安装环境中的numpy stub使用3.12语法，与项目mypy目标3.11冲突，位于site-packages；不列为项目源码缺陷。 |
| JavaScript/MJS `node --check` | 5个文件通过 | 含两个运行界面脚本和网站脚本。 |
| 前端 `node --test tests/frontend/*.test.cjs` | 116通过，0失败 | 没有真实浏览器端到端运行。 |
| PowerShell Parser | 33个ps1文件解析通过 | 没有执行部署脚本。 |
| Bash `bash -n` | 48个sh文件解析通过 | 包含部署、检查和shell测试脚本。 |
| 两项迁移静态shell契约 | 2项通过 | `test-migration-install.sh`、`test-migration-runner.sh`，无真实安装/服务启动。 |

### 5.2 Python行为测试

Windows Python3.12.10定向执行以下文件：

```text
tests/test_signer_service.py
tests/test_migration_transfer.py
tests/test_capability_file_races.py
tests/test_model_wire_contract.py
```

结果为 **90 passed, 13 skipped，95.72秒**。跳过原因：4项测试卷不能生成Windows8.3别名，9项为POSIX权限边界。测试使用 `PYTHONDONTWRITEBYTECODE=1`、`-p no:cacheprovider`和仓库外独占basetemp。

Windows全量测试尝试运行到输出14%后还有若干用例完成，未见失败输出，但因该环境耗时过长主动终止。本报告**不把它记作全量通过**。

Linux第一次复制在不同WSL会话间丢失了 `/tmp`工作目录，导致测试从错误目录运行；该次结果不作为代码结论。随后使用 `/var/tmp`副本并核对218个生产Python文件的SHA-256。第一次顺序执行期间发现root私有解释器/临时根目录使真实nobody测试无法访问，运行在207通过、5跳过、1失败处中断；更正为公共可访问的临时解释器和源码根目录后，该权限用例独立重跑 **1 passed**。这些环境问题没有计入产品缺陷。

Linux验证使用当前工作区的隔离副本，Ubuntu24.04/WSL、Python3.12.3；在可由nobody访问的临时解释器/源码路径下，将全部 **2,883个收集测试节点**按文件分为8个进程批次执行。每个批次内部按pytest默认方式运行，使用独立basetemp；这不是一次未分片的标准CI执行。聚合结果：

```text
2860 passed
6 failed
17 skipped
288 subtests passed（另计，不与普通测试重复相加）
8批次完成，墙钟时间2135.97秒（约35分36秒）
```

JUnit将subtest也记为testcase，因此XML合计为3171；本报告使用pytest的2,883个主节点口径，避免混淆。17项跳过具体为：Windows/PowerShell专用行为11项、root执行环境下要求实际非特权用户的3项、未安装可选boto3的3项；不把这些跳过当成已验证通过。

对6项失败，在8个批次结束后按原样串行重跑，得到 **4 passed / 2 failed**：

| 原失败测试 | 失败现象 | 串行复核和结论 |
| --- | --- | --- |
| `test_export_jobs_are_authenticated_bounded_and_downloadable` | 10秒内仍running | 通过；此前单独重跑也通过。并行负载下超出测试等待预算，未据此新增产品缺陷。 |
| `test_runtime_logs_and_complete_export_require_auth_and_redact_secrets` | 等待后仍running | 通过；保留负载敏感性记录，不宣称原失败无效。 |
| `test_terminal_execution_requires_new_native_balance[0]` | 预期reserved，得到rejected | 通过；尚未证明是何种时间/负载因素，不将猜测写成已确认根因。 |
| `test_public_lifecycle_remains_readable_after_verified_cold_archive[False]` | cold reader deadline exceeded | 通过；存在明确deadline，未据此断言程序应该无限放宽预算。 |
| `test_service_boot_and_restore_without_intents_keep_chat_and_memory` | migration控制目录权限无效 | 原样仍失败；已由root/nobody直接备份对照归因到A12。 |
| `test_installed_permissions_allow_real_service_boot_and_encrypted_backup` | 无权搜索宿主/etc/noyra而失败 | 原样仍失败；仅在内存隔离宿主身份路径后通过，形成A13。 |

因此，**不能把本轮Linux全量分片结果描述为“全量测试通过”**；也不能把原6项全部归咎于环境。A12为可重复源码兼容缺陷，A13为可重复测试隔离缺陷，另4项原样复核通过但其高负载表现仍需在相应修复/性能评审时关注。没有修改测试等待时间或断言来获得上述重跑结果。


### 5.3 行为复现的可信范围

- A01使用真实临时文件发布和真实临时数据库；仅在内存中注入失效事件，结果可直接读回。
- A03/A04用模拟transport在请求准备和发送之间制造确定性的撤销时刻；它们证明代码缺少检查及后续调用仍发生，不是对真实外网提供商的攻击测试。
- A02使用真实本机HTTPsocket读取；替换TLS建连工厂后复现空闲超时性质，不验证证书或真实目标部署。
- A05/A09直接使用项目解析器和极小的实体输入；没有运行大规模实体炸弹。
- A06只建立6个本机测试连接；原始accepted socket没有期限是观测事实，耗尽规模是根据无容量上限的实现作出的风险判断。
- A07使用临时账户，不涉及真实资金；证明的是任意个人消息可签，不是任意交易可签。
- A08是同步调用关系和无工作量预算的静态证明，没有生产性能benchmark。
- A10/A11使用实际兼容库和临时数据，影响范围已限定到该API。
- A12用不同真实UID执行相同临时备份对照；A13只在内存中为测试子进程补齐配置隔离，没有更改生产代码或宿主配置。

所有问题均以本轮当前源码为依据。既有测试通过不能否定本轮新增的反例；这里也没有把每个可疑模式、宽泛except、无攻击链的静态命中都升级为发现。

## 6. 只读完整性与未执行事项

最终复核：初始589个受保护文件的SHA-256全部一致，**0项变化、0项缺失**。相对写报告前保存的Git工作区状态，唯一新增条目为：

```text
?? docs/audit/2026-10-20-independent-code-audit-current-worktree.md
```

原有Git修改/未跟踪条目没有减少或改变状态，HEAD保持不变。589项基线JSON本身的SHA-256为：

```text
43fe67db9d9e3271b244d84971c8606236c7a68159f4be175e5fcf38d8ea4cda
```

该基线检查覆盖前述受保护文件集合，不声称对整个工作区所有二进制/缓存做了前后哈希；Git状态核对补充检查了可见工作区条目。

本轮唯一拟保留的仓库新增物是本审计Markdown。临时验证数据和日志在操作系统临时目录/WSL隔离目录；没有覆盖既有报告，没有修改测试来使其通过。对原有未提交修改保持原状。

本轮没有执行：真实安装/升级脚本、systemd服务操作、主动修改WSL持久账户/生产卷、Docker构建发布、生产密钥加载、真实RPC/模型调用、链上转账、外部漏洞扫描。测试使用既有UID切换和临时目录chown来验证权限边界。也没有联网运行依赖CVE数据库检查，因此不对当前第三方依赖漏洞状态作出保证。这里只审计仓库代码和可安全验证的本地行为。

## 7. 覆盖矩阵

“精读”指表中列明的关键函数/链路逐语句追踪，不代表该目录每行都精读；“扫描/基线”指语法树、函数与危险操作盘点、类型/静态检查和已完成测试覆盖范围。没有新增确认项不等于没有缺陷。

| 生产范围 | Python文件/行数 | 本轮关注方式与确认结果 |
| --- | ---: | --- |
| 顶层服务/入口/契约 | 8 / 14,816 | HTTP路由、readiness、迁移worker/close、signer精读；A02/A07/A08。 |
| autonomy | 4 / 690 | 全目录扫描/类型基线，调度与运行租约的调用关系检查。 |
| capability | 7 / 1,181 | grant use/revalidate、ToolRunner、文件原子写逐语句追踪；A01/A03。 |
| cognition | 22 / 19,892 | cycle/deliberation调用入口和未完成动作阻塞精读；其余扫描/基线。 |
| core | 55 / 41,202 | admission、数据库提交、at_rest、HTTP边界、runtime恢复重点追踪；A01/A08/A12，相关路径对照。 |
| interaction | 20 / 11,227 | 微信解析/鉴权顺序与XML限制逐语句追踪；其余扫描/基线；A05。 |
| knowledge | 3 / 2,463 | 全目录扫描/类型与测试基线。 |
| learning | 4 / 901 | 全目录扫描/类型与测试基线。 |
| migration | 24 / 12,176 | HTTP执行、source fence、恢复/激活连接点、兼容传输重点追踪；A02/A10/A11。 |
| mind | 17 / 6,917 | 全目录扫描/类型与测试基线。 |
| model | 17 / 9,692 | 发送检查与wire契约对照、全目录扫描/类型、wire定向测试。 |
| research | 6 / 2,118 | browser发送trace与RSS解析精读；A09。 |
| sleep | 6 / 1,991 | 全目录扫描/类型与测试基线。 |
| wallet | 16 / 14,518 | RPC、acquisition调用链、execution迁移证明精读及签名器对照；A04/A07。 |
| world | 9 / 3,303 | SafeWebReader请求和内容处理链路精读；A03。 |
| scripts | 20 / 5,315（Python） | Python全盘点，迁移agent重点精读；Shell/PowerShell语法与安全操作扫描；A06。 |
| web/site/website | 5个JS/MJS及页面资产 | JS语法、前端116项测试和危险输出位置扫描；没有真实浏览器端到端结论。 |
| deploy/CI/构建配置 | 非Python | 默认监听地址、服务隔离、凭据来源、检查命令/矩阵、迁移静态契约对照。 |
| tests | 222 / 85,826（Python） | 全盘点，A13测试隔离追踪；按第5节分别报告实际执行、跳过、失败或未完成情况。 |

## 8. 结论边界及不应误读之处

- 未确认远程代码执行、私钥导出、匿名主网付款、跨主体数据库读取或外部实体本机文件读取；报告没有用这些高影响词替代现有证据。
- 允许一次外部请求不等于允许撤销之后继续发送；已有body大小、URL策略、DNS pinning、HTTP HMAC也不能替代最终发送时的运行/授权检查。
- A01的“文件已变化”和“账本未终结”来自同一次复现；A03/A04没有证明能绕过其后所有数据库提交边界。
- A02故障恢复必须考虑目标已激活而源端未收到确认；缩短timeout本身不是完整的迁移修复。
- A06默认loopback可达性限制、A07的有效bearer条件、A09可信上游条件、A10/A11无当前生产调用点，均属于风险分级的一部分，不能在转述时删去。
- 没有为未经复现的宽泛假设另行堆叠“潜在问题”。仍需在修复阶段按各项验收条件增加有意义的回归。

## 附录A：生产Python文件盘点

以下索引覆盖当前工作区全部218个生产Python文件，给出行数、AST函数数（含嵌套/方法）和类数；它是盘点索引，不是每文件人工逐行审阅完成的签名。

规范化源文件指纹的计算：按下表路径字典序，对每个文件拼接 `POSIX相对路径 + "\t" + 文件完整SHA256 + "\n"`，以UTF-8编码后再计算SHA-256。本轮摘要为：

```text
295e0e6877f9a86b46db1d64fa2f5675feb560d4e71fc360282bf86686e7b894
```

| 源文件 | 行数 | 函数数 | 类数 |
| --- | ---: | ---: | ---: |
| `src/noyra/__init__.py` | 3 | 0 | 0 |
| `src/noyra/__main__.py` | 297 | 9 | 1 |
| `src/noyra/autonomy/__init__.py` | 13 | 0 | 0 |
| `src/noyra/autonomy/loop.py` | 410 | 15 | 1 |
| `src/noyra/autonomy/types.py` | 26 | 0 | 2 |
| `src/noyra/autonomy/workflow.py` | 241 | 10 | 2 |
| `src/noyra/capability/__init__.py` | 16 | 0 | 0 |
| `src/noyra/capability/errors.py` | 10 | 0 | 3 |
| `src/noyra/capability/filesystem.py` | 205 | 7 | 0 |
| `src/noyra/capability/integrity.py` | 77 | 3 | 1 |
| `src/noyra/capability/store.py` | 494 | 18 | 1 |
| `src/noyra/capability/tools.py` | 312 | 7 | 1 |
| `src/noyra/capability/types.py` | 67 | 1 | 4 |
| `src/noyra/cognition/__init__.py` | 187 | 0 | 0 |
| `src/noyra/cognition/_integrity.py` | 55 | 6 | 0 |
| `src/noyra/cognition/consciousness.py` | 423 | 14 | 2 |
| `src/noyra/cognition/cycle.py` | 1,302 | 31 | 1 |
| `src/noyra/cognition/deliberation.py` | 847 | 30 | 4 |
| `src/noyra/cognition/epistemic.py` | 682 | 21 | 7 |
| `src/noyra/cognition/execution.py` | 3,267 | 100 | 19 |
| `src/noyra/cognition/governance.py` | 705 | 23 | 4 |
| `src/noyra/cognition/interaction.py` | 468 | 16 | 1 |
| `src/noyra/cognition/memory_integration.py` | 162 | 6 | 1 |
| `src/noyra/cognition/metacognition.py` | 1,235 | 28 | 5 |
| `src/noyra/cognition/motivation.py` | 1,344 | 39 | 6 |
| `src/noyra/cognition/projects.py` | 3,051 | 80 | 8 |
| `src/noyra/cognition/reflection.py` | 667 | 17 | 4 |
| `src/noyra/cognition/research.py` | 973 | 36 | 5 |
| `src/noyra/cognition/self_model.py` | 722 | 18 | 4 |
| `src/noyra/cognition/self_modification.py` | 864 | 22 | 4 |
| `src/noyra/cognition/settings.py` | 342 | 5 | 2 |
| `src/noyra/cognition/social.py` | 535 | 14 | 4 |
| `src/noyra/cognition/supervisor.py` | 160 | 4 | 1 |
| `src/noyra/cognition/thought.py` | 1,236 | 29 | 5 |
| `src/noyra/cognition/types.py` | 665 | 38 | 21 |
| `src/noyra/core/__init__.py` | 193 | 0 | 0 |
| `src/noyra/core/actions.py` | 736 | 16 | 1 |
| `src/noyra/core/admission.py` | 379 | 35 | 3 |
| `src/noyra/core/archive.py` | 3,298 | 106 | 14 |
| `src/noyra/core/at_rest.py` | 2,035 | 90 | 11 |
| `src/noyra/core/backup_retention.py` | 281 | 10 | 1 |
| `src/noyra/core/conversation_schema.py` | 27 | 0 | 0 |
| `src/noyra/core/credentials.py` | 190 | 6 | 1 |
| `src/noyra/core/database.py` | 10,958 | 73 | 1 |
| `src/noyra/core/deadline.py` | 38 | 3 | 0 |
| `src/noyra/core/errors.py` | 56 | 0 | 14 |
| `src/noyra/core/event_archive.py` | 1,049 | 17 | 5 |
| `src/noyra/core/events.py` | 458 | 13 | 1 |
| `src/noyra/core/evidence_capacity.py` | 151 | 5 | 0 |
| `src/noyra/core/evidence_schema.py` | 142 | 3 | 0 |
| `src/noyra/core/export_jobs.py` | 602 | 24 | 4 |
| `src/noyra/core/external_gates.py` | 204 | 5 | 0 |
| `src/noyra/core/http.py` | 716 | 45 | 11 |
| `src/noyra/core/identity.py` | 302 | 13 | 1 |
| `src/noyra/core/integrity.py` | 5,336 | 153 | 16 |
| `src/noyra/core/lifecycle.py` | 237 | 8 | 1 |
| `src/noyra/core/locking.py` | 100 | 5 | 1 |
| `src/noyra/core/maintenance_evidence.py` | 71 | 3 | 0 |
| `src/noyra/core/operator_controls.py` | 766 | 28 | 4 |
| `src/noyra/core/payload_codec.py` | 88 | 4 | 0 |
| `src/noyra/core/private_erasure_schema.py` | 257 | 0 | 0 |
| `src/noyra/core/private_sources.py` | 108 | 4 | 0 |
| `src/noyra/core/provider_health.py` | 800 | 18 | 2 |
| `src/noyra/core/public_archive_schema.py` | 39 | 0 | 0 |
| `src/noyra/core/public_policy.py` | 30 | 0 | 0 |
| `src/noyra/core/public_sitemap_schema.py` | 92 | 0 | 0 |
| `src/noyra/core/recovery_manifest.py` | 142 | 4 | 0 |
| `src/noyra/core/redaction.py` | 237 | 10 | 0 |
| `src/noyra/core/release_assurance.py` | 220 | 12 | 0 |
| `src/noyra/core/release_http.py` | 104 | 4 | 0 |
| `src/noyra/core/request_io.py` | 49 | 4 | 1 |
| `src/noyra/core/resilience.py` | 360 | 7 | 2 |
| `src/noyra/core/resource_policy.py` | 193 | 3 | 0 |
| `src/noyra/core/retention.py` | 1,232 | 29 | 4 |
| `src/noyra/core/retention_policy.py` | 10 | 0 | 0 |
| `src/noyra/core/runtime.py` | 488 | 24 | 1 |
| `src/noyra/core/runtime_export.py` | 1,193 | 17 | 3 |
| `src/noyra/core/runtime_logging.py` | 154 | 7 | 0 |
| `src/noyra/core/secret_cleanup.py` | 851 | 19 | 1 |
| `src/noyra/core/snapshots.py` | 495 | 13 | 2 |
| `src/noyra/core/storage.py` | 1,444 | 60 | 11 |
| `src/noyra/core/storage_lifecycle.py` | 519 | 17 | 2 |
| `src/noyra/core/storage_mode.py` | 521 | 21 | 4 |
| `src/noyra/core/subject_storage.py` | 190 | 8 | 1 |
| `src/noyra/core/training_export.py` | 2,065 | 64 | 10 |
| `src/noyra/core/types.py` | 143 | 9 | 5 |
| `src/noyra/core/upgrade.py` | 524 | 21 | 2 |
| `src/noyra/core/usage_projection.py` | 197 | 4 | 2 |
| `src/noyra/core/usage_schema.py` | 21 | 0 | 0 |
| `src/noyra/core/wallet_schema.py` | 111 | 7 | 1 |
| `src/noyra/deployment_setup.py` | 1,230 | 38 | 7 |
| `src/noyra/desktop.py` | 36 | 2 | 0 |
| `src/noyra/http_access_logging.py` | 76 | 1 | 0 |
| `src/noyra/interaction/__init__.py` | 107 | 0 | 0 |
| `src/noyra/interaction/conversations.py` | 320 | 11 | 2 |
| `src/noyra/interaction/diary.py` | 126 | 6 | 1 |
| `src/noyra/interaction/erasure.py` | 922 | 21 | 2 |
| `src/noyra/interaction/errors.py` | 6 | 0 | 2 |
| `src/noyra/interaction/inbound.py` | 1,656 | 41 | 14 |
| `src/noyra/interaction/integrity.py` | 546 | 4 | 1 |
| `src/noyra/interaction/outbound.py` | 230 | 7 | 7 |
| `src/noyra/interaction/posts.py` | 1,296 | 27 | 8 |
| `src/noyra/interaction/projection.py` | 901 | 16 | 1 |
| `src/noyra/interaction/public_archive.py` | 1,141 | 31 | 7 |
| `src/noyra/interaction/public_cold_reader.py` | 348 | 12 | 5 |
| `src/noyra/interaction/public_contract.py` | 17 | 0 | 0 |
| `src/noyra/interaction/public_display.py` | 81 | 1 | 0 |
| `src/noyra/interaction/public_reading.py` | 217 | 5 | 0 |
| `src/noyra/interaction/sitemap_tokens.py` | 116 | 5 | 1 |
| `src/noyra/interaction/smtp_deadline.py` | 72 | 5 | 1 |
| `src/noyra/interaction/store.py` | 391 | 14 | 1 |
| `src/noyra/interaction/transport.py` | 2,647 | 67 | 9 |
| `src/noyra/interaction/types.py` | 87 | 2 | 5 |
| `src/noyra/knowledge/__init__.py` | 33 | 0 | 0 |
| `src/noyra/knowledge/common.py` | 2,229 | 63 | 9 |
| `src/noyra/knowledge/sync.py` | 201 | 14 | 3 |
| `src/noyra/learning/__init__.py` | 6 | 0 | 0 |
| `src/noyra/learning/_integrity.py` | 16 | 1 | 0 |
| `src/noyra/learning/outcomes.py` | 833 | 22 | 2 |
| `src/noyra/learning/types.py` | 46 | 0 | 2 |
| `src/noyra/migration/__init__.py` | 102 | 0 | 0 |
| `src/noyra/migration/activation.py` | 1,469 | 67 | 8 |
| `src/noyra/migration/activation_contract.py` | 98 | 2 | 0 |
| `src/noyra/migration/agent.py` | 1,905 | 49 | 8 |
| `src/noyra/migration/bundle.py` | 519 | 15 | 0 |
| `src/noyra/migration/credentials.py` | 164 | 7 | 3 |
| `src/noyra/migration/cutover.py` | 763 | 15 | 2 |
| `src/noyra/migration/discovery.py` | 485 | 21 | 7 |
| `src/noyra/migration/executor.py` | 189 | 5 | 3 |
| `src/noyra/migration/fencing.py` | 276 | 11 | 1 |
| `src/noyra/migration/http_executor.py` | 1,184 | 31 | 6 |
| `src/noyra/migration/manager.py` | 1,169 | 34 | 4 |
| `src/noyra/migration/policy.py` | 372 | 16 | 3 |
| `src/noyra/migration/proposals.py` | 265 | 11 | 3 |
| `src/noyra/migration/providers.py` | 37 | 3 | 2 |
| `src/noyra/migration/recovery.py` | 667 | 20 | 3 |
| `src/noyra/migration/runtime.py` | 380 | 9 | 2 |
| `src/noyra/migration/settings.py` | 33 | 1 | 0 |
| `src/noyra/migration/subject_payload.py` | 418 | 10 | 0 |
| `src/noyra/migration/targets.py` | 546 | 14 | 2 |
| `src/noyra/migration/transfer.py` | 332 | 10 | 4 |
| `src/noyra/migration/trust.py` | 440 | 18 | 6 |
| `src/noyra/migration/wallet.py` | 310 | 8 | 3 |
| `src/noyra/migration/wallet_material.py` | 53 | 2 | 0 |
| `src/noyra/mind/__init__.py` | 75 | 0 | 0 |
| `src/noyra/mind/affect_policy.py` | 451 | 18 | 4 |
| `src/noyra/mind/belief.py` | 331 | 16 | 1 |
| `src/noyra/mind/benchmarks.py` | 410 | 11 | 4 |
| `src/noyra/mind/blocks.py` | 221 | 10 | 2 |
| `src/noyra/mind/causal.py` | 229 | 7 | 1 |
| `src/noyra/mind/consolidation.py` | 368 | 8 | 2 |
| `src/noyra/mind/engine.py` | 1,371 | 31 | 1 |
| `src/noyra/mind/entities.py` | 536 | 19 | 4 |
| `src/noyra/mind/errors.py` | 10 | 0 | 3 |
| `src/noyra/mind/goal.py` | 473 | 16 | 1 |
| `src/noyra/mind/integration.py` | 257 | 7 | 3 |
| `src/noyra/mind/memory.py` | 1,154 | 40 | 1 |
| `src/noyra/mind/relationship.py` | 380 | 15 | 1 |
| `src/noyra/mind/retrieval.py` | 279 | 14 | 4 |
| `src/noyra/mind/types.py` | 278 | 4 | 16 |
| `src/noyra/mind/validation.py` | 94 | 2 | 0 |
| `src/noyra/model/__init__.py` | 89 | 0 | 0 |
| `src/noyra/model/capabilities.py` | 521 | 15 | 2 |
| `src/noyra/model/config.py` | 172 | 9 | 2 |
| `src/noyra/model/embedding.py` | 396 | 22 | 8 |
| `src/noyra/model/embedding_gateway.py` | 360 | 15 | 3 |
| `src/noyra/model/embedding_ledger.py` | 990 | 23 | 4 |
| `src/noyra/model/embedding_resources.py` | 458 | 18 | 3 |
| `src/noyra/model/errors.py` | 96 | 3 | 11 |
| `src/noyra/model/fake.py` | 23 | 2 | 1 |
| `src/noyra/model/gateway.py` | 512 | 8 | 1 |
| `src/noyra/model/ledger.py` | 1,052 | 28 | 1 |
| `src/noyra/model/openai_compatible.py` | 516 | 9 | 5 |
| `src/noyra/model/provider.py` | 11 | 1 | 1 |
| `src/noyra/model/reasoning.py` | 75 | 4 | 1 |
| `src/noyra/model/resources.py` | 4,171 | 94 | 10 |
| `src/noyra/model/types.py` | 201 | 8 | 11 |
| `src/noyra/model/wire.py` | 49 | 3 | 0 |
| `src/noyra/research/__init__.py` | 41 | 0 | 0 |
| `src/noyra/research/browser.py` | 360 | 13 | 2 |
| `src/noyra/research/provider.py` | 733 | 24 | 1 |
| `src/noyra/research/routing.py` | 128 | 6 | 0 |
| `src/noyra/research/search.py` | 701 | 16 | 2 |
| `src/noyra/research/types.py` | 155 | 7 | 7 |
| `src/noyra/service.py` | 11,642 | 228 | 9 |
| `src/noyra/service_contract.py` | 798 | 8 | 2 |
| `src/noyra/signer_service.py` | 734 | 33 | 7 |
| `src/noyra/sleep/__init__.py` | 29 | 0 | 0 |
| `src/noyra/sleep/engine.py` | 1,004 | 30 | 1 |
| `src/noyra/sleep/errors.py` | 10 | 0 | 3 |
| `src/noyra/sleep/fatigue.py` | 309 | 16 | 1 |
| `src/noyra/sleep/integrity.py` | 440 | 10 | 1 |
| `src/noyra/sleep/types.py` | 199 | 10 | 9 |
| `src/noyra/wallet/__init__.py` | 174 | 0 | 0 |
| `src/noyra/wallet/acquisition.py` | 2,836 | 59 | 7 |
| `src/noyra/wallet/admin_projection.py` | 272 | 4 | 2 |
| `src/noyra/wallet/config.py` | 163 | 6 | 0 |
| `src/noyra/wallet/dispatch.py` | 93 | 6 | 2 |
| `src/noyra/wallet/economy.py` | 2,013 | 65 | 1 |
| `src/noyra/wallet/economy_types.py` | 315 | 16 | 10 |
| `src/noyra/wallet/execution.py` | 2,737 | 78 | 16 |
| `src/noyra/wallet/keystore.py` | 357 | 13 | 1 |
| `src/noyra/wallet/local.py` | 431 | 16 | 1 |
| `src/noyra/wallet/local_rpc.py` | 151 | 5 | 1 |
| `src/noyra/wallet/rpc.py` | 323 | 14 | 3 |
| `src/noyra/wallet/store.py` | 2,707 | 70 | 3 |
| `src/noyra/wallet/types.py` | 309 | 22 | 9 |
| `src/noyra/wallet/workflow.py` | 1,381 | 32 | 1 |
| `src/noyra/wallet/workflow_types.py` | 256 | 17 | 9 |
| `src/noyra/world/__init__.py` | 28 | 0 | 0 |
| `src/noyra/world/errors.py` | 18 | 0 | 5 |
| `src/noyra/world/genesis.py` | 361 | 13 | 1 |
| `src/noyra/world/integrity.py` | 402 | 7 | 1 |
| `src/noyra/world/observation_archive.py` | 841 | 14 | 2 |
| `src/noyra/world/prediction.py` | 487 | 19 | 1 |
| `src/noyra/world/source.py` | 492 | 23 | 3 |
| `src/noyra/world/store.py` | 533 | 20 | 2 |
| `src/noyra/world/types.py` | 141 | 2 | 9 |

## 附录B：本轮原始证据摘要

这些结果是本轮生成的证据内容，已嵌入文档，不依赖临时目录长期存在。模拟transport的`wire/requests`字段表示测试支架在模拟实际发送点观测的状态。临时随机ID不具有生产含义。

### probes

```json
[
  {
    "probe": "web_pause",
    "wire": [
      {
        "lease_valid": false,
        "grant_status": "active",
        "trace_installed": false
      }
    ],
    "result": "OperationInvalidated",
    "action": {
      "status": "executing",
      "result_json": null
    }
  },
  {
    "probe": "web_revoke",
    "wire": [
      {
        "lease_valid": true,
        "grant_status": "revoked",
        "trace_installed": false
      }
    ],
    "result": "succeeded",
    "action": {
      "status": "succeeded",
      "result_json": "{\"content_hash\":\"eb0404e02f80b43227872b87269deba2912c20865e96479392181201a344f99e\",\"source_id\":\"src_547b09065380477ab8fe2ff8389e733a\"}"
    }
  },
  {
    "probe": "file_pause",
    "content": "changed-after-pause",
    "result": "OperationInvalidated",
    "action": {
      "status": "executing",
      "result_json": null
    }
  }
]
```

### more-probes

```json
[
  {
    "probe": "migration_drip_deadline",
    "configured_timeout": 1,
    "elapsed_seconds": 1.267,
    "returned": {
      "x": 1
    }
  },
  {
    "probe": "agent_header_workers",
    "connections": 6,
    "accepted_workers": 6,
    "accepted_socket_timeouts": [
      null,
      null,
      null,
      null,
      null,
      null
    ],
    "server_timeout": 0.02
  },
  {
    "probe": "transfer_partial_tail",
    "complete": false,
    "actual_size": 4097,
    "expected_prefix": 8192,
    "resume_error": "ValueError: transfer resume prefix is invalid"
  },
  {
    "probe": "encrypted_receive_existing_target",
    "error": "ValueError: encrypted transfer decrypt failed",
    "preexisting_target_survives": false
  },
  {
    "probe": "encrypted_final_integrity_failure",
    "error": "ValueError: encrypted transfer artifact hash mismatch",
    "unverified_output_survives": true,
    "output_bytes": 4097
  },
  {
    "probe": "signer_arbitrary_message",
    "unrelated_challenge_accepted": true
  }
]
```

### wallet-probes

```json
[
  {
    "probe": "wallet_rpc_pause",
    "requests": [
      {
        "method": "eth_chainId",
        "lease_valid": false,
        "asset_status": "active",
        "trace_installed": false
      },
      {
        "method": "eth_getBalance",
        "lease_valid": false,
        "asset_status": "active",
        "trace_installed": false
      }
    ],
    "result": "returned balance 7"
  },
  {
    "probe": "wallet_rpc_revoke_asset",
    "requests": [
      {
        "method": "eth_chainId",
        "lease_valid": true,
        "asset_status": "revoked",
        "trace_installed": false
      },
      {
        "method": "eth_getBalance",
        "lease_valid": true,
        "asset_status": "revoked",
        "trace_installed": false
      }
    ],
    "result": "returned balance 7"
  }
]
```

### xml-probes

```json
{
  "wechat_comment_bypass": {
    "input_bytes": 108,
    "expanded_text": "AUDIT_ENTITY_EXPANDED"
  },
  "rss_utf16_bypass": {
    "input_bytes": 388,
    "results": "(SearchResult(title='AUDIT_ENTITY_EXPANDED', url='https://example.com/', snippet='', rank=1),)"
  }
}
```

### 完整测试与补充权限复现

```json
{
  "collection_nodes": 2883,
  "shards": 8,
  "passed": 2860,
  "failed": 6,
  "skipped": 17,
  "subtests_passed": 288,
  "elapsed_seconds": 2135.97,
  "failure_rechecks": [
    {
      "class": "tests.test_migration_storage_boundary",
      "name": "test_installed_permissions_allow_real_service_boot_and_encrypted_backup",
      "outcome": "failed",
      "message": "AssertionError: Traceback (most recent call last):\n    File \"/var/tmp/noyra-control-boundary-ld4u8jni/src/noyra/core/storage_mode.py\", line 228, in _external_mount_identity\n      metadata = path.lstat()\n                 ^^^^^^^^^^^^\n    File \"/usr/lib/python3.12/pathlib.py\", line 849, in lstat\n      return self.stat(follow_symlinks=False)\n             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n    File \"/usr/lib/python3.12/pathlib.py\", line 842, in stat\n      return os.stat(self, follow_symlinks=follow_symlinks)\n             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n  PermissionError: [Errno 13] Permission denied: '/etc/noyra/storage-identity.conf'\n  \n  The above exception was the direct cause of the following exception:\n  \n  Traceback (most recent call last):\n    File \"<string>\", line 17, in <module>\n    File \"/var/tmp/noyra-control-boundary-ld4u8jni/src/noyra/service.py\", line 869, in wrapped\n      return function(instance, *args, **kwargs)\n             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n    File \"/var/tmp/noyra-control-boundary-ld4u8jni/src/noyra/service.py\", line 10582, in __init__\n      self.storage_contract = detect_storage_layout(settings.data_dir, settings.backup_root)\n                              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n    File \"/var/tmp/noyra-control-boundary-ld4u8jni/src/noyra/core/storage_mode.py\", line 400, in detect_storage_layout\n      external_identity = _external_mount_identity(data)\n                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n    File \"/var/tmp/noyra-control-boundary-ld4u8jni/src/noyra/core/storage_mode.py\", line 232, in _external_mount_identity\n      raise StorageUnavailableError(\"storage_identity_unreadable\") from error\n  noyra.core.storage_mode.StorageUnavailableError: storage_identity_unreadable\n  \nassert 1 == 0\n +  where 1 = CompletedProcess(args=['/var/tmp/noyra-independent-audit-20261020-env/bin/python', '-c', '\\nimport sys\\nfrom pathlib import Path\\nfrom noyra.core.at_rest import (\\n    VolumeEncryptionProbe, VolumeEncryptionStatus, EncryptedBackupManager,\\n)\\nfrom noyra.service import NoyraService, ServiceSettings\\nroot = Path(sys.argv[1])\\nimport noyra.service as service_module\\nread_channel = service_module.upgrade_channel\\nservice_module.upgrade_channel = lambda: read_channel(root/\\'absent-upgrade-channel\\')\\n# The encrypted device probe is simulated and host configuration isolated;\\n# UID, modes, file access, application boot, encryption and restore are real.\\nVolumeEncryptionProbe.probe = lambda *a, **kw: VolumeEncryptionStatus(\\n    True, \\'test\\', \\'fixture\\', \\'volume\\'\\n)\\nservice = NoyraService(ServiceSettings(\\n    data_dir=root/\\'data\\', subject_id=\\'Noyra-installed\\', genesis_hash=\\'a\\'*64,\\n    profile=\\'test\\', port=0, at_rest_mode=\\'required\\', backup_keyring_path=root/\\'keyring.json\\',\\n))\\ntry:\\n    service.boot()\\n    assert service.kernel.admission.accepting, service.integrity.summary()\\n    assert service.at_rest.health()[\\'ready\\']\\nfinally:\\n    service.close()\\nimport base64, hashlib\\nfrom types import SimpleNamespace\\nfrom cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey\\npublic=X25519PrivateKey.generate().public_key().public_bytes_raw()\\nartifact=service.http.migration_http_executor.artifact_resolver(\\n    SimpleNamespace(task_id=\\'task-fixture\\', target_id=\\'target-fixture\\',\\n                    source_epoch=\\'runtime-0\\', subject_id=\\'Noyra-installed\\'),\\n    {\\'recipient_public_key\\':base64.urlsafe_b64encode(public).decode(),\\n     \\'recipient_key_fingerprint\\':hashlib.sha256(public).hexdigest()},\\n)\\nassert artifact.path.is_relative_to(root/\\'data/migration/source/outgoing\\')\\nassert service.at_rest.health()[\\'ready\\']\\nmanager=EncryptedBackupManager(root/\\'data\\',root/\\'keyring.json\\')\\nbackup=root/\\'output/subject.backup\\'\\nmanager.create(backup)\\nrestored=manager.restore(backup,root/\\'output/restored\\')\\nassert (restored/\\'migration/source/epoch\\').is_file()\\nassert (restored/\\'migration/fences\\').is_dir()\\nassert (restored/\\'migration/fences/active.json\\').read_text() == (\\n    \\'{\"subject_id\":\"Noyra-other\",\"task_id\":\"migration-task\",\"status\":\"active\"}\\'\\n)\\nassert not (restored/\\'migration/target-activation\\').exists()\\nassert (root/\\'data/migration/target-activation/state\\').stat().st_uid == 0\\nprint(\\'boot_and_backup_ok\\')\\n', '/var/tmp/noyra-control-boundary-ld4u8jni'], returncode=1, stdout='', stderr='Traceback (most recent call last):\\n  File \"/var/tmp/noyra-control-boundary-ld4u8jni/src/noyra/core/storage_mode.py\", line 228, in _external_mount_identity\\n    metadata = path.lstat()\\n               ^^^^^^^^^^^^\\n  File \"/usr/lib/python3.12/pathlib.py\", line 849, in lstat\\n    return self.stat(follow_symlinks=False)\\n           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\\n  File \"/usr/lib/python3.12/pathlib.py\", line 842, in stat\\n    return os.stat(self, follow_symlinks=follow_symlinks)\\n           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\\nPermissionError: [Errno 13] Permission denied: \\'/etc/noyra/storage-identity.conf\\'\\n\\nThe above exception was the direct cause of the following exception:\\n\\nTraceback (most recent call last):\\n  File \"<string>\", line 17, in <module>\\n  File \"/var/tmp/noyra-control-boundary-ld4u8jni/src/noyra/service.py\", line 869, in wrapped\\n    return function(instance, *args, **kwargs)\\n           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\\n  File \"/var/tmp/noyra-control-boundary-ld4u8jni/src/noyra/service.py\", line 10582, in __init__\\n    self.storage_contract = detect_storage_layout(settings.data_dir, settings.backup_root)\\n                            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\\n  File \"/var/tmp/noyra-control-boundary-ld4u8jni/src/noyra/core/storage_mode.py\", line 400, in detect_storage_layout\\n    external_identity = _external_mount_identity(data)\\n                        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\\n  File \"/var/tmp/noyra-control-boundary-ld4u8jni/src/noyra/core/storage_mode.py\", line 232, in _external_mount_identity\\n    raise StorageUnavailableError(\"storage_identity_unreadable\") from error\\nnoyra.core.storage_mode.StorageUnavailableError: storage_identity_unreadable\\n').returncode"
    },
    {
      "class": "tests.test_service.ServiceTestCase",
      "name": "test_export_jobs_are_authenticated_bounded_and_downloadable",
      "outcome": "passed",
      "message": null
    },
    {
      "class": "tests.test_service.ServiceTestCase",
      "name": "test_runtime_logs_and_complete_export_require_auth_and_redact_secrets",
      "outcome": "passed",
      "message": null
    },
    {
      "class": "tests.test_private_erasure_disabled",
      "name": "test_service_boot_and_restore_without_intents_keep_chat_and_memory",
      "outcome": "failed",
      "message": "noyra.core.at_rest.AtRestError: migration control directory permissions are invalid"
    },
    {
      "class": "tests.test_wallet_fee_admission",
      "name": "test_terminal_execution_requires_new_native_balance[0]",
      "outcome": "passed",
      "message": null
    },
    {
      "class": "tests.test_public_archive",
      "name": "test_public_lifecycle_remains_readable_after_verified_cold_archive[False]",
      "outcome": "passed",
      "message": null
    }
  ],
  "host_isolation_recheck": "1 passed; in-memory redirect of two storage identity constants only",
  "root_backup_probe": {
    "uid": 0,
    "mode": "0o755",
    "classified_as_installed": true,
    "outcome": "AtRestError: migration control directory permissions are invalid"
  },
  "nonroot_backup_probe": {
    "uid": 65534,
    "mode": "0o755",
    "classified_as_installed": false,
    "outcome": "succeeded"
  }
}
```

### 完整性复核

```json
{
  "protected_files": 589,
  "protected_changes": [],
  "git_status_added": [
    "?? docs/audit/2026-10-20-independent-code-audit-current-worktree.md"
  ],
  "git_status_removed": [],
  "head": "c67526d6b28f995823936687d9c59bb03b612463",
  "baseline_sha256": "43fe67db9d9e3271b244d84971c8606236c7a68159f4be175e5fcf38d8ea4cda"
}
```

