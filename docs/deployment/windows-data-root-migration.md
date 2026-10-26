# Windows 保留数据根与旧安装迁移

本说明对应 F01 修复。托管启动的 `DataRoot` 是主体数据的绝对根目录，并与 `InstallRoot` 互不包含。生命周期只给子进程设置数据根，不修改操作者 PowerShell 的环境。标准运行时在创建目录或 SQLite 前复核相同契约。

## 新安装

先选择实际受保护的数据卷与目录，完成原有的卷加密、外部备份密钥和凭据配置。数据根需要没有主体残留；初始凭据文件和备份 keyring 可放在独立的受保护目录，不要通过在数据根预置旧 secrets 的方式绕过初始化检查。签名和可信发布者参数仍按发布规程提供。

完成 install 后，只有确实创建全新主体时，首次启动才使用：

```powershell
# 使用同一份当前生命周期入口；以下变量应事先填写为经过核对的绝对路径。
& $Lifecycle -Action start -InstallRoot $InstallRoot -DataRoot $DataRoot -InitializeDataRoot
```

以后启动不加 `-InitializeDataRoot`。已有数据库、零字节数据库、仅存 WAL、存在旧秘密目录或其他主体残留时，该开关不会重建/覆盖它们。没有数据库的普通 start 会失败，不会自动转到 `.runtime/data` 创建新主体。

升级和回滚继续传同一个 `InstallRoot` / `DataRoot`；改变版本工作目录不改变主体数据库。`-InitializeDataRoot` 只用于 start，不接受于 install/upgrade/recover/uninstall。

## 状态与阻断

```powershell
& $Lifecycle -Action status -InstallRoot $InstallRoot -DataRoot $DataRoot
& $Lifecycle -Action stop -InstallRoot $InstallRoot -DataRoot $DataRoot
```

status 增加 `data_root_status` 和 `legacy_data_paths`。`ready` 只表示数据目录的启动前置条件满足；主体身份、schema、事件链、存储加密和运行所有权仍由原有运行时门禁校验，不能把 ready 当作全部完整性审计已通过。

- `database-missing`：目标库不存在；先排查旧位置，不能直接把 InitializeDataRoot 当作修复。
- `invalid-database`：库为空、为目录或链接；保留现场，不重建。
- `initialization-not-empty`：目标没有库但存在其他数据；需要人工判断。
- `legacy-data-detected`：安装树中存在 `.runtime` 或 `noyra.sqlite3*`；先处理下述迁移。
- `unsafe-install-tree` / `unsafe-path` / `inspection-unavailable`：链接、不可读路径或无法安全检查；不跟随、不删除。

安装树检查包含隐藏目录、未选中的版本和 SQLite sidecar；不读取秘密内容。扫描拒绝链接和超过 100,000 个条目的树，无法确认安全时停止。已发现旧数据时，start/install/upgrade/rollback/recover/uninstall 均在接管状态或改版本前拒绝；stop/status 仍可用。卸载在停止后再检查一次，然后才允许递归删除安装树。

新进程记录 `noyra-process/v2` 包含绑定的数据根。旧 v1 记录仍按 PID、启动时间、可执行路径三项核对后允许停止；已经运行的 v1 实例不会被新 start 静默认作目录已绑定。PID-only 或无法核实的进程仍须人工确认，不能按猜测终止。

## 旧安装迁移：先确定哪一份数据属于原主体

此修复只识别并保护旧数据，不自动选取、合并或移动真实安装。执行实际迁移前需要维护窗口、可验证备份和具体路径。不能把下列文字当成在任意目录执行的批量搬迁脚本。

1. 保留安装、数据和进程现状，读取 status。列出原来指定的 DataRoot、每个旧版本的 `.runtime/data`，以及以前额外配置过的 NOYRA_DATA_DIR；后者可能完全在安装树外，扫描无法推断任意历史环境变量。记录主体 ID、genesis hash、数据库 schema 及事件链/快照锚点。若有多份不同数据，逐份只读比较，不按文件时间或大小自动选择，不合并库。
2. 使用匹配原实例的生命周期参数执行 stop，确认所有相关版本进程均退出。不要仅凭无 PID 文件认定停机；核对进程身份，并由备份工具获得源数据库进程锁。不要在仍有运行进程时复制、移动 DB 或删除 WAL/SHM。仅把 WAL 删除以获得“干净数据库”会损失已提交数据。
3. 将权威数据根完整备份到安装树外的受保护卷，保留独立备份 keyring 和历史密钥。可以使用现有加密 backup/restore-backup 工具，它持有源库锁、通过 SQLite 备份 API 生成一致数据库并校验还原；先核对该工具当前实际保留的子目录。外置钱包 keystore、密码文件和自定义归档/导出目录不一定在根内，须单独保护；不要以为单独复制 noyra.sqlite3 已覆盖整个主体。
4. 还原到**原生命周期所绑定的 DataRoot 路径**。不要顺手改 R03 绑定的安装/数据路径，也不要重写 R03 installation/hash 或删除权威状态文件。如果 DataRoot 已有另一份 DB，停止并审查；不覆盖。如果只有旧 PID/锁文件，停机后先将该目录作为整体保留到保护区，再按备份工具的 absent/empty target 条件还原。备份中若带有旧机器或旧路径的 PID 记录，先证明该进程已退出，再将该记录随迁移证据另存；复制来的 PID 文件不能证明新位置的运行进程身份，不应靠修改 PID/路径字段来放行。
5. 在首次启动前，对还原结果核对 subject_id、genesis_hash、事件链锚点、备份承诺保留的源文件/秘密及其校验值，做 SQLite quick_check。只读 SQLite 检查使用 `mode=ro`，不要使用会忽略 WAL 的 immutable 方式检查仍有 WAL 的原库，也不要调用会初始化/迁移的 Database 构造器来“探测”。备份恢复验证与身份核对均通过后，才批准后续启动。
6. 将旧版本内的整个 `.runtime` 及其他被发现的遗留主体数据移动到**安装树外、受保护且明确的存档位置**，保留到迁移验收完成；不是删除。移动前打印并核对解析后的绝对源/目标，源确实是已备份的旧根，目标不在 InstallRoot 中；不要混合 shell 拼接递归移动。所有旧版本均应检查，不能只处理 current。
7. 使用普通 start（不加 InitializeDataRoot）启动当前版本；确认运行库路径、原主体、连续性和原有完整性门禁。然后在隔离复本上验证升级/回滚仍使用同一根，且卸载只移除代码、保留数据。原数据存档、历史密钥和备份在验收前都不删除。

若从多份数据中无法证明哪份权威、发现身份冲突、备份无法验证、密钥缺失或仍有运行进程，应停止实际迁移并针对该实例制定恢复方案。本轮代码修复不声称已经迁移任何现存服务器或 Windows 实例。

## 启动器与发行验收

生命周期向子进程设置 `NOYRA_DATA_DIR`、`NOYRA_MANAGED_INSTALL_ROOT`、`NOYRA_MANAGED_DATA_ROOT` 和 `NOYRA_MANAGED_INITIALIZE`。这四项为生命周期提供的启动契约，不应在用户全局环境中手工长期配置。标准桌面/服务入口复核后采用保留根；自定义启动器必须透传这些变量并调用标准入口，不能自行选择相对目录。

本地回归会运行真实的环境报告子进程及标准 NoyraService 的临时数据库构造，证明工作目录变化不更换主体。该测试不替代实际签名发布启动器在目标 Windows/PowerShell 7、BitLocker/ACL 条件下的 install→start→upgrade→rollback→uninstall 实机验收。源码直接启动而没有托管契约时，仍按原有 NOYRA_DATA_DIR 配置，属于另一种显式部署方式。
