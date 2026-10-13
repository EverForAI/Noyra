# 最近公开页面审计修复的最终复审

日期：2026-10-13（Asia/Shanghai）。

依据：`docs/audit/2026-10-13-public-second-pass-readonly-audit.md`；执行记录：`docs/roadmap/2026-10-13-public-audit-remediation.md`。

分支：`codex/public-audit-remediation-20261013`。起点：`bb250b97e138b1237f9fe8f60a9e00bdc1e93039`。本轮应用与测试提交：`f280fb189351c323e4fce8652e3766e254c77cd4`；schema 94。最终文档提交会改变 HEAD，但不改变本轮被测试的源码。真实发布必须针对最终完整提交重新生成外部证据，不能将应用提交或旧服务器记录当成最终发布 SHA。

## 完成情况

最近审计九项中的 **A01–A04、C01–C04 已实现并完成模块验收；E01 仅补足了本地 Linux、合成 systemd 和资源证据，真实外部发布验收仍未完成**。本轮没有以本地合成测试代替签名外部门禁，也没有降低原有发布条件。

每个大模块先记录根因、安全边界和验收目标，再实施、回归、复审和本地提交。各次有失败的首次运行保留真实记录，后续补正或环境修复的复验分别登记。聊天/记忆删除和公开证据模块没有新增。本轮没有生产部署、真实平台消息、付费供应商调用、资金操作或推送合并。

| 项目 | 根因修复与实际行为 | 验收与复审 | 提交 |
| --- | --- | --- | --- |
| A01 | 恢复标准 DOCTYPE，语法帮助移至投稿正文控件旁 | 实际 HTTP 与 Chromium 标准模式、head/SEO/帮助位置、375px 无横向溢出；保留投稿和草稿合同 | `993ea9a` |
| A03 | 未配置 sitemap 的动态404显式 no-store | sitemap/阅读错误缓存矩阵；静态 robots 缓存保持 | `993ea9a` |
| A02 | 专用持久 AES-SIV 路由 keyring；owner 启动后初始化，旧 key 验证兼容 | 独立 owner、进程重启、轮换、篡改、主体、损坏与撤回；匿名请求不生成 key | `c15259f`，最终稳定 slot 由 `3a572a0` 补齐 |
| A04 | readingReturn 绑定具体文章导航链，无关视图或文章清除 | 直返、菜单、其他文章、迟到响应与草稿隔离 | `983ff1e` |
| C01 | 有界恢复保留目标、明确部分恢复，按钮继续当前游标 | 1条/空页、多次20请求分步恢复200条，不跳项、不伪装完整 | `983ff1e` |
| C02 | schema94增量 slot；根索引只读主体/类别高水位，不扫描全部源正文 | 25000有效日记、250分片全部可读且无重复；增长/撤回/重演/主体及迁移/导出 | `3a572a0` |
| C03 | 冷段物理读/解密/JSON/hash移至可终止、OS限制内存的 spawn 子进程 | Windows/Linux内存限额、部分IPC阻塞、deadline/实际退出释放、local-only/hash及无目录写入；精确SHA合成RSS实测 | `7fdfaa2` |
| C04 | 钱包只读列表同源高分辨率时钟，结合父请求剩余期限 | 40项投影/dispatch/automation回归；不改支付、签名、策略或 asyncio 时钟 | `f280fb1` |
| E01 | 补 Linux/root、五组安装回滚、精确SHA合成systemd重启与RSS | 真实生产硬件、HTTPS代理、LUKS/跨机恢复、长时间soak及独立签名八门禁仍待执行 | 本报告记录，不标成全部通过 |

## 存储、密钥与恢复边界

专用 `secrets/public-sitemap-keyring.json` 在主体所有权门禁通过后、接受HTTP请求前初始化。配置外部 keyring 时只读加载。最多四把互不重复的32字节 key，第一把生成新路由，其余验证旧路由；HKDF派生 AES-SIV key并认证版本头及描述符。确定性认证加密没有使用固定 AES-GCM nonce。key与操作令牌、归档密钥、验证码密钥分离；没有进入日志、URL或仓库。

现有文件损坏、权限不合格、链接或未知 key均拒绝，不静默再生。滚动轮换需所有owner先具备新旧key，再切换首key；直接移除旧key会使旧URL返回400。没有替管理员选择无限兼容窗口。完整加密 data-root备份包括默认key；数据库导出不包含秘密文件，必须另行保管并在恢复时绑定。

schema94新增 `public_sitemap_slots` 与六类源表INSERT/UPDATE触发器，slot和源写入同一事务。范围固定100slot，不随新增数据重编号；撤回保留空slot，各分片重新验证当前源资格、主体和hash，索引行本身不授权公开正文。迁移回填的分配步骤 MATERIALIZED，避免读己写导致编号跳跃；重演只追加缺项。UPDATE/DELETE索引被拒绝，普通保留清理不会重排旧路由。所有权图94把索引按主体导出，不公开其他主体内容。

schema93回填与schema71迁移已复验。DDL指纹为 `181b72baa819d54d6d18c80411b610abdd2c3c6cee18661aeadb3a190a339f9b`，结构指纹为 `762feef35a9e0593168f010ea4f980d3425bf55db788baf71bc587a9c9333e2e`。恢复应保留数据库索引与路由key；完全丢失slot后，仅凭源记录不能恢复历史插入顺序。离线重建必须轮换路由key并要求重新抓索引，不能承诺旧范围仍指向原记录。源码和已发布URL不能证明未知历史事实。

## 冷读期限与资源复审

父进程在有界SQL快照中提供namespace、manifest和获准公开事件ID。子进程只用内存最小目录映射表，不开启live数据库；沿原只读目录解析、local-only读取并认证整段所有事件。只返回from/to/hash、最多2MB；父进程继续验证keyring代次、事件链与payload hash。密钥材料仅经过私有进程管道，不输出秘密或私有正文。

Windows Job限制512MiB进程提交内存；POSIX RLIMIT_AS限制512MiB地址空间。两种OS会计不同，不能宣称整个服务RSS固定128MB。无法安装限额则拒绝读取。每次准入请求一个worker，最多两worker。部分IPC帧不能使HTTP线程无限阻塞：发送/接收由有界exchange线程执行，父方按原2秒剩余预算等待；timeout终止worker并最多两次100ms回收等待。内核不可中断IO仍未退出时保留准入slot，实际回收后才释放，不允许用替代worker无限扩张资源。

主进程的热SQL/JSON检查点、spawn进程创建和任意OS调度延迟仍有真实平台边界。该实现不是所有操作的绝对硬实时保证；namespace上限为10000主体/绑定，单段/累计逻辑读、SQL步数与响应预算保持。合法大段在内存/期限不足时会拒绝，不返回伪造成功或跳过完整性校验。

## 验证记录

以下模块数量有重叠，不能相加称为唯一测试总数。锁定Windows环境为Python3.11.16、pytest9.0.3；Linux专项为WSL Ubuntu24.04/root/Python3.12.3及固定开发锁。

| 验证 | 结果 | 证据/范围 |
| --- | --- | --- |
| 唯一整仓 pytest | 2515通过、56跳过、285子测试通过、0失败/错误；4826.29秒 | `.runtime/remediation-full.log`、`.runtime/remediation-full.xml`；应用/测试SHA `f280fb1`；XML共2856 tests |
| 全根 Ruff0.16.2 check/format | 通过，655文件格式通过 | 全仓根目录，不只src/tests/scripts |
| 默认 Mypy | 423文件通过 | src/tests |
| JS语法与Node合同 | 115通过、0失败 | `.runtime/remediation-node.log` |
| pip check/锁漏洞扫描 | 通过；base、base+cloud、dev无已知漏洞 | 三份锁分别扫描；仅当前数据库的已知漏洞范围 |
| R01 | 17通过、26子测试通过 | `.runtime/audit-r01.xml`及实际浏览器 |
| R02 | 55通过、26子测试；HTTP owner重建另1通过 | `.runtime/audit-r02.xml`、`audit-r02-restart.xml` |
| R03 | 公开Node38通过 | 1条/空页/分步继续与导航代次 |
| R04 | 初轮67通过、1失败；capacity补正后3通过；schema71 23通过 | `.runtime/audit-r04-final.xml`、`audit-r04-capacity-final.xml`、`audit-r04-schema71-final.xml` |
| R05 Windows | 98通过、26子测试通过 | `.runtime/audit-r05-final.xml`；实际OS分配拒绝/退出释放 |
| R05 Linux | 59通过、26子测试通过 | `.runtime/audit-r05-linux.xml` |
| R06 | 40通过 | `.runtime/audit-r06-wallet.xml` |
| Linux root初轮/复验 | 初轮127通过、7失败；环境修复后相关69全部通过、0跳过 | `.runtime/remediation-linux-root.xml`、`remediation-linux-root-recheck.xml` |
| Ubuntu五组shell安装/存储/控制/升级/迁移回滚 | 全部exit0 | 合成目录，预期非空目录拒绝诊断不算失败 |
| 精确SHA真实systemd、合成HTTP数据 | 重启通过，3条保存路由仍可读，index相同 | `.runtime/remediation-systemd-rss.json`、`remediation-systemd-smoke/remote-acceptance.log` |
| 最后实际Chromium375×812 | CSS1Compat/doctype/head/帮助位置通过，无水平溢出 | `output/playwright/remediation-final-home.png` |

R04首次新增增长测试因fixture ID短于合同8字符失败，补正fixture后通过，没有放宽生产验证。R05首次两条测试因在Windows使用POSIX私有pipe方法、期待错误的详情字段失败，补正测试后通过。Linux初轮失败来自venv位于/root(700)，降权账号不可执行及未安装应用导致子进程import失败；将临时venv置可读路径、按现有源码安装后完整重验相关69项。不能抹掉这些历史或把两次拼成一次Linux134零失败。

pip-audit首次因Windows中文路径stdout非UTF8出现UnicodeDecodeError；启用Python UTF8后重试成功。此项是验证工具编码故障，不是依赖漏洞。

整仓运行期间应用/测试没有变化，仅起草本报告和执行记录；没有重复启动整仓。56跳过涉及Windows8.3、POSIX权限、symlink、Linux/root与Bash专项等，不计作通过。Linux复验补足部分权限/安装/迁移边界，不把未逐项运行的Windows跳过宣称全部闭合。整仓XML的2856包含普通通过、跳过和子测试，不能另称2856个普通通过。整仓结束后没有在跑的测试。

### 精确SHA合成systemd与资源实测的实际含义

从 `git archive f280fb189351c323e4fce8652e3766e254c77cd4` 建立隔离fixture，真实systemd transient unit以nobody运行NoyraHTTPServer。没有把它当成完整生产NoyraService配置、真实HTTPS代理或LUKS部署。fixture包含20个约1.05MB私有冷事件及一个公开生命周期事件。

8请求、4并发实测2次200（1.986/1.991秒），6次准入503（0.059–0.110秒）；最多2worker。拒绝符合两slot合同，不标成全部请求200。峰值父RSS111780KiB、child RSS151952KiB、child VMS165104KiB。随后单请求200、slot正常释放；私有标记不出现，公开仅from/to。live/ready/health均200。重启后3条保存路由200，index相同。

首次fixture继承/etc升级目录读取权限导致失败，改为注入隔离UpgradeManager；一次WSL无前台进程退出使临时服务停止，保持临时会话后复验。两者均为fixture环境处理，没有修改生产权限或应用安全门禁。验收结束后unit stop、Result=success，fixture目录已删除，临时WSL等待进程及浏览器session已关闭。

## E01尚未关闭的部分与后续执行合同

| 待验收部分 | 当前风险与条件概率 | 后续验收、安全边界 |
| --- | --- | --- |
| 生产硬件/磁盘/完整服务负载 | P2；合成RSS不能推定真实负载，概率未测 | 最终安装SHA，完整NoyraService与实际硬件测worker峰值、RSS、延迟、WAL和内存余量 |
| HTTPS反向代理与缓存 | P2；本地HTTP未经过生产代理，概率未测 | 真实TLS/代理头/身份/rate-limit；未配置404→配置后no-store，不得沿用负缓存 |
| 物理LUKS及清洁主机备份/迁移 | P2；WSL临时目录不是加密卷，概率未测 | 未解锁拒启动、权限、schema71→94、key+slot恢复、错key/篡改/中断/回滚 |
| 长时间soak与真实云/QQ/独立signer/testnet | P2；本轮没有实际外部证据，概率未测 | 隔离真实环境与明确授权，保留所有失败探针/unknown，不用结尾smoke覆盖历史失败 |
| 最终SHA独立签名八门禁 | P2发布阻断；缺项时不得声称通过 | 遵守`docs/release/external-gates.md`，不同执行/复核人、Ed25519与72小时新鲜度 |

最终文档提交后生成新的pending模板：

```text
python scripts/prepare-external-validation.py --commit HEAD --output <新的私有证据目录>/external-gates-pending.json
```

模板仍须全部pending，无签名，不代表门禁完成。八项要求为ubuntu_systemd、encrypted_volume、backup_restore、migration_fence、signer_kms、reorg_nonce、https_proxy、soak；现有发布合同的soak为24/72小时，历史8小时观察不替代它，更不能复用其他SHA。

执行时额外核验本轮keyring/slot重启、双owner兼容、轮换、撤回、恢复与大冷段/慢IO/两slot准入。所有源/目标安装SHA和锁hash要记录；不得将操作令牌、API key、归档key或私密正文放进签名JSON。没有真实环境与独立复核结果前，E01保持发布阻断。

## 修复质量判断

本轮按根因处理了模板解析、路由生命周期、导航状态、索引扫描、冷读进程隔离和只读时钟；未通过隐藏错误、增加无限循环、取消hash/权限/预算或把失败改成功来满足测试。逐模块复审没有确认同类源码阻断被重新引入。

改变schema、持久key和spawn冷读增加了运维合同：需保留slot/key、规划memory headroom和重启/兼容期，并接受超预算时503。这些已明确记录，不能写成所有规模或所有环境均已无风险。本次整仓零失败及模块/静态/浏览器回归支持八项本地修复结论，未确认由本次修复引入的新代码阻断；E01仍限制发布结论，本地修复完成不等于生产发布已完成。
