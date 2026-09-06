# 短期部署与运行验收报告

日期：2026-08-20

候选版本：`0d29a9857880`（工作树未修改）

## 测试范围

本次使用 E 盘临时目录创建隔离的 WSL2 Ubuntu 24.04.4 测试发行版
`Noyra-ShortTest-2404`，没有使用原有 `Ubuntu-24.04` 的 `/opt/noyra`、`/var/lib/noyra`
或主体数据。测试发行版中创建了 8 GiB 稀疏文件，使用 loop device、LUKS2、ext4，挂载到
`/var/lib/noyra`，并以 `NOYRA_AT_REST_MODE=required` 运行。

本次没有配置模型、Embedding、搜索、S3 或其他第三方 API Key；认知功能保持关闭，因此
没有外部模型调用或 API 费用。

## 资源与清理

| 项目 | 结果 |
|---|---|
| 临时 WSL 导出包峰值 | 约 2.07 GiB，已删除 |
| 临时测试发行版峰值 | 约 4.93 GiB，存放在 E 盘，已注销 |
| LUKS 测试文件逻辑大小 | 8 GiB；稀疏文件实际占用约 221 MiB，已删除 |
| E 盘可用空间 | 测试前约 779 GiB，清理后约 779 GiB |
| C 盘可用空间 | 测试前约 39.14 GiB，WSL 关闭后约 38.76 GiB |
| 原 `Ubuntu-24.04` | 保留，未注销；测试结束时为 stopped |
| 测试目录 | 临时测试根目录已删除；本机路径在公开准备时脱敏 |

C 盘出现约 0.38 GiB 的净变化，未发现属于本次测试的残留文件。原有 WSL VHDX 未删除或
压缩，避免误伤用户已有环境；E 盘测试目录和测试 WSL 已确认清理。

## 已通过的项目

### 1. 隔离环境与加密卷

- Ubuntu 24.04.4、Python 3.12.3、systemd 255 正常运行。
- LUKS2/AES-XTS loop device 成功建立，Noyra 能识别 `dm-crypt LUKS mapping`。
- `/health/ready` 报告 `at_rest.ready=true`、`volume.backend=luks`、`integrity.status=ok`。
- 数据库成功初始化到 schema 43，SQLite quick check 为 `ok`。

### 2. systemd 与健康检查

- 安装 profile `base` 成功完成，服务以 `noyra` 用户运行。
- `/health/live` 返回 200。
- `/health/ready` 返回 200，生命周期为 `active`。
- `/health` 深度诊断返回 integrity、WAL、存储、导出和 at-rest 状态均为 `ok`。
- cognition、S3 和云 readiness 未配置，状态为关闭/未配置，符合本次测试预期。

### 3. 崩溃恢复

对测试服务发送 `SIGKILL` 后，systemd 自动重新拉起服务：

- `NRestarts` 从 0 增加到 1；
- 当前 release 指针未改变；
- 恢复后 `/health/ready` 返回 200；
- 没有发现第二个 Noyra 服务进程。

### 4. 回滚

在测试发行版内建立第二个独立 release，切换 `current`/`previous` 后执行
`install-ubuntu.sh --rollback`：

- `current` 恢复到原 release；
- `previous` 指向测试 release；
- systemd 保持 active/running；
- `/health/ready` 返回 200。

这验证了指针切换和显式代码回滚合同，但由于下面记录的备份问题，不能把它等同于完整的
正常升级、升级前加密备份和 readiness 验收通过。

### 5. 三小时短 soak

时间：2026-08-20 08:37:53Z 至 11:38:08Z（北京时间 16:37:53 至 19:38:08）。

- 共 330 个采样周期；
- 每周期检查 `/health/live` 和 `/health/ready`；
- 发现 1 次单点探针失败，但没有连续 3 次失败；
- 脚本最终结果为 `soak=passed`；
- 测试结束时 systemd 为 active/running，`NRestarts=0`。

这一次单点失败的原始采样证据随临时测试环境一并删除，不能据此推断具体原因。它不构成
已确认的服务故障，但应在下一次 soak 中保留失败响应、curl 错误、systemd 状态和时间关联，
否则无法判断是短暂启动窗口、WSL 调度抖动还是应用响应异常。

## 发现的问题

### F-01：标准 ext4 的 `lost+found` 阻断 required at-rest 启动

- 风险等级：**P1，部署可用性阻断**；不表现为数据泄露，但会使服务无法启动。
- 复现：新建 LUKS2 + ext4 卷挂载 `/var/lib/noyra` 后，ext4 自动创建 root-owned 的
  `/var/lib/noyra/lost+found`。服务启动时 `_harden_private_paths()` 尝试以 `noyra` 用户
  修改该目录权限，收到 `PermissionError: Operation not permitted`，systemd 反复重启。
- 影响：生产上按文档使用标准 ext4 加密卷时，P2-14 的 required 模式可能在首次启动直接
  失败。当前实现只有删除该系统目录后才能继续，不应要求运维者手工删除它。
- 临时处置：仅在隔离测试卷删除 `lost+found` 后继续验证；没有修改源码，也没有操作原有
  Ubuntu 数据。
- 修复风险：**中高**。修复涉及 at-rest 私有路径遍历、root-owned 系统目录和 symlink/
  reparse 边界；错误放宽可能导致真实私有文件被跳过。建议先写回归测试覆盖 `lost+found`、
  root-owned 非 Noyra 条目、符号链接和跨设备路径，再实现最小的系统目录处理策略。
- GPT-5.6-sol 建议：**ultra**。这是存储安全边界和启动准入的根因修复，需完整跨平台、
  权限和故障注入回归；不建议只用简单补丁。

### F-02：Ubuntu 原子升级的升级前备份被 backup-keyring 权限检查阻断

- 风险等级：**P1，发布/升级阻断**；不会静默覆盖旧 release，但会使 required at-rest
  环境无法完成正常升级。
- 复现：安装器以 root 运行升级前 `python -m noyra backup`。安装器创建的 keyring 为
  `root:noyra`、`0640`，但备份进程有效 UID 为 root；`validate_keyring_path()` 将 root
  视为文件 owner，并要求 owner 模式 `0600`，因此报错：
  `backup keyring owned by the service account must use mode 0600`。
- 影响：升级脚本在切换 release 前停止服务，但在加密冷备份阶段失败；本次没有发生指针
  切换或数据破坏，旧服务最终可恢复。这会阻止生产升级，并可能留下需要运维检查的停服
  窗口。
- 修复风险：**中高**。不能简单把 keyring 改成全局可读，也不能绕过权限校验。需要统一
  “root 执行安装器、noyra 执行备份读取”与 keyring owner/group/mode 合同，并补充真实
  systemd/installer 测试。
- GPT-5.6-sol 建议：**ultra**。涉及外部密钥文件、root/service-account 权限分界和升级
  回滚；应先确定权限模型，再做最小实现和故障注入。

### F-03：三小时 soak 中出现一次非连续探针失败

- 风险等级：**P3，低风险观察项**。
- 影响：没有造成连续故障、自动中止或最终不 ready；但原始失败原因没有在清理前单独固化，
  使本轮不能完成根因分类。
- 修复风险：**低**。优先改进验收脚本证据记录，不改变服务运行逻辑。
- GPT-5.6-sol 建议：**max**；只有当后续证据指向服务真实竞态时，才升级到 ultra。

### F-01/F-02 修复记录（2026-08-20）

- F-01 根因修复：at-rest 与加密备份现在共用严格的 POSIX 根级条目枚举；仅当
  `lost+found` 是根目录直属、同设备、真实目录、root:root、0700 且无 reparse/link 时，
  才将其识别为文件系统恢复元数据并排除。其它 root-owned 条目、错误类型、错误模式、
  符号链接和跨设备条目仍 fail closed，不会被泛化忽略。
- F-01 运维取舍：标准 `lost+found` 内的 fsck 恢复内容仍被视为文件系统恢复区，不进入
  Noyra 备份；如该目录出现恢复文件，应由运维单独检查和保全，不能把 Noyra 备份当作其副本。
- F-02 根因修复：安装器保留 `root:noyra 0640` keyring 合同，改为用 `runuser` 以
  `noyra` 身份生成升级前加密备份；root 创建受控 staging、回收其权限后校验并以
  `root:root 0600` 原子发布到 root-only 备份目录，成功和失败路径都会清理 staging；
  staging 使用 root-owned marker/lock 和精确 UID/GID、模式、链接数、设备校验，清理失败时
  保持服务停止并 fail closed，不会在备份目录仍对 `noyra` 可穿越时恢复服务。
- F-02 边界加固：自定义备份目录必须由 root 预先控制，最终目录为 root:root 0700，祖先目录
  拒绝 symlink、dot traversal 和 group/other 可写路径；默认目录只允许在 root-owned
  `/var/backups` 下创建。备份 keyring 也拒绝 hard link，避免安装器的权限收口意外改变另一路径
  的 inode。
- 回归证据：Windows 本地聚焦测试 `29 passed, 6 skipped`；完整回归 `914 passed,
  9 skipped, 104 subtests passed`；Ubuntu 24.04 聚焦回归 `35 passed`，且
  `bash -n scripts/install-ubuntu.sh` 通过。另以真实 POSIX UID/GID 完成服务账户启动准入和
  加密备份验证：`noyra` 可在 root:root 0700 `lost+found` 存在时进入 ready，并可读取
  root:noyra 0640 keyring 写入受控 staging；最终备份收口为 root:root 0600、备份目录恢复
  root:root 0700。完整 `install-ubuntu.sh` 升级矩阵仍留待下一轮隔离 LUKS 验收。
- 额外边界脚本 `tests/shell/test-install-ubuntu-backup-boundary.sh` 在 Ubuntu 24.04
  以真实非 root 服务账户验证了 staging 写入、父目录 fsync、最终文件收口、父目录权限恢复、带 marker
  的残留 staging 识别条件，以及无 marker 的 operator 目录不满足清理条件。

### F-02 修复后的针对性 WSL2 验证（2026-08-21）

验证发行版：`Noyra-Targeted-240821`（由现有 `Ubuntu-24.04` 导出后导入 E 盘的临时副本）。
`/var/lib/noyra` 使用临时 LUKS2/AES-XTS loopback 文件，保留 ext4 自动创建的
`root:root`、`0700` `lost+found`。本轮不使用任何模型、搜索、S3 或其它 API key。

- 首次按修复代码执行正常升级时发现 F-02 的残余根因：staging 的 `1730` 模式允许服务账户
  写入文件，但不允许备份库用 `O_RDONLY|O_DIRECTORY` 打开父目录执行 fsync，真实安装器在
  `/var/backups/noyra/.noyra-staging.*` 收到 `PermissionError`，升级前备份被阻断；指针未切换，
  staging 清理后备份目录恢复 `root:root 0700`。该发现仍是 **P1 升级阻断**，修复风险为**中**，
  建议 `max`，因为边界明确且改动局部。
- 根因修复：staging 改为 `root:noyra 1770`，仅在备份写入窗口存在；marker、lock 仍为
  `root:root 0600`，备份文件校验后立即收口到 `root:root 0600` 并移入 root-only 目录；增加了
  非 root 账户实际打开并 fsync staging 父目录的回归断言。
- 修复后正常升级通过：`runuser` 备份成功，`root:root 0600`、单 hardlink、同设备备份约
  `2.7 MiB`；`current=targeted-upgrade`、`previous=targeted-initial`，systemd 启动后
  `/health/ready` 返回 200，at-rest 报告 `backend=luks`、`ready=true`。
- 修复后显式 rollback 通过：指针恢复为 `current=targeted-initial`、`previous=targeted-upgrade`，
  readiness 返回 200，服务保持 active。
- readiness 失败注入（临时将 `NOYRA_PORT` 改为非法值）通过 fail-closed 路径：安装器报告
  `New release failed readiness; restoring the previous code pointer`，旧指针恢复、staging
  清理、备份目录仍为 `root:root 0700`，失败 release 保留供 root 运维取证；恢复有效配置后服务
  可重新 ready。WSL 临时 loop/mount 在 shell 结束时可能被清理，属于测试夹具生命周期，不是
  安装器路径安全结果。

## 未完成或未能完全验收的项目

1. 本轮已取得修复后隔离 LUKS 正常升级、加密备份、readiness 失败指针恢复和显式 rollback
   现场证据；这仍不替代 clean production restore 和真实生产主机演练。
2. 三小时 soak 的历史单点探针失败没有保留到清理后的持久证据目录；本轮针对性验证没有重复
   三小时 soak，后续若进入发布候选阶段应保留每次失败的响应、curl 错误、systemd 状态和时间关联。
3. 安装器已拒绝非 root 可写的自定义备份路径并在关键步骤复核目录 inode/设备，但仍使用
  shell 路径操作而不是 Linux `openat2`/dirfd；具备 root 或等价挂载权限的并发操作者仍可
  制造极窄的目录替换竞态。这不是普通 `noyra` 服务账户可利用的路径，保留为发布前
  Ultra 级主机加固项，不宣称任意 root 级并发威胁已由脚本完全消除。
4. 若在创建 root-owned staging marker 之前硬杀安装器，可能留下无 marker 的 root-only
  临时目录；它不会被 `noyra` 访问或当作备份发布，但需要后续 root 运维清理。marker/lock
  已在服务账户获得目录权限前创建，正常升级中断不会留下可写残留。
5. 本次是本机 WSL staging，不是生产主机验收；仍不关闭 clean production restore、
  Windows 签名 MSI/MSIX、真实 GitHub tag/Sigstore/provenance 等发布门禁。

## 结论

原始短部署测试表明，服务在隔离 LUKS 测试卷上最终可以运行，systemd、健康检查、崩溃恢复、
指针回滚和三小时 soak 的主要路径通过；但当时不能宣称 P2-13/P2-14 已完全验收。随后本轮
已完成 F-01/F-02 的代码级根因修复和回归验证；正式生产升级矩阵、LUKS 演练和发布门禁仍按
上列清单保留，不能把本轮代码测试当作生产验收替代品。

## F-01/F-02 修复后的两小时隔离部署验收（2026-08-21）

本轮按用户要求重新在 E 盘执行短期部署，不修改源码、测试或安装脚本。测试发行版为从
现有 `Ubuntu-24.04` 导出后导入的临时 `Noyra-ShortTest-240821-2h`，发行版 VHDX、导出包、
证据目录和 1 GiB LUKS2 测试文件均位于 E 盘的临时测试根目录（本机路径在公开准备时脱敏）。原有
`Ubuntu-24.04` 未被挂载、写入或注销。测试没有配置模型、Embedding、搜索、S3 或任何外部
API key，cognition 保持关闭。

### 通过项目

- 临时 Ubuntu 24.04.4、Python 3.12.3、systemd running；LUKS2/AES-XTS loop 文件成功
  建立并挂载到 `/var/lib/noyra`，ext4 自动生成的 `root:root 0700 lost+found` 保留在卷内。
- `base` 首次安装、systemd 启动和服务账户运行通过；`/health/live`、`/health/ready`、
  `/health` 均返回 200，at-rest 报告 `backend=luks`、`ready=true`，SQLite schema 43、
  quick check 和 integrity 状态均为 `ok`。
- 对主服务发送 `SIGKILL` 后，systemd 将 `NRestarts` 从 0 增至 1，使用新 PID 恢复，
  `/health/ready` 返回 200，未出现并行 Noyra 主进程。
- 正常升级 `short-initial -> short-upgrade` 返回 0。`runuser` 以 `noyra` 生成加密冷备份，
  最终备份为 `root:root 0600`、单 hardlink、同设备，大小约 2.52 MiB；指针为
  `current=short-upgrade`、`previous=short-initial`，服务恢复 ready。
- 显式 rollback 返回 0，指针恢复为 `current=short-initial`、`previous=short-upgrade`，
  服务 active，readiness 返回 200。
- 两小时 soak 于北京时间 08:51:56 至 10:51:01 运行 120 个 60 秒采样周期；每次
  `/health/live` 和 `/health/ready` 均为 HTTP 200，systemd 始终 active，`NRestarts=0`，
  `failures=0`。结束时服务仍 ready。

### 本轮发现与边界

1. **T-01：测试环境第一次启动使用了无效的归档 AES-256 key。**
   - 风险等级：**P3，测试配置错误**；影响为服务以 `EX_CONFIG`（status 78）拒绝启动，
     不影响数据安全，也不代表产品运行期故障。
   - 根因：测试夹具第一次生成 key 时使用了普通 Base64/非 canonical base64url 值，而运行时
     合同要求 32 字节 canonical base64url。
   - 修复风险：**低**；只需在测试/部署配置生成阶段使用项目文档规定的生成命令，不能放宽
     运行时密钥校验。GPT-5.6-sol 建议：**max**。
2. **T-02：测试夹具第一次启动沿用了导入发行版中旧的 `/opt/noyra` 状态，且数据卷尚未挂载。**
   - 风险等级：**P3，测试隔离错误**；影响为 at-rest 正确拒绝启动并产生 systemd 重启/失败记录，
     没有写入原有 Ubuntu 数据。
   - 根因：临时发行版导出包含原服务安装状态；夹具在首次安装前未清理旧部署并未先挂载 LUKS 卷。
   - 修复风险：**低**；测试流程应先停止并清理临时发行版服务状态，再创建和挂载测试卷；
     不能通过把 `NOYRA_AT_REST_MODE` 改成 development 来掩盖问题。GPT-5.6-sol 建议：**max**。
3. **T-03：WSL2 loop/LUKS 资源在清理 shell 结束时存在短暂的内核引用残留。**
   - 风险等级：**P3，测试夹具生命周期项**；不会暴露 Noyra 数据，但要求按顺序卸载、关闭
     mapper、detach loop 后再注销发行版。
   - 处置：本轮在后续清理中确认 `/var/lib/noyra` 已卸载、mapper 已关闭、loop 已 detach；
     不属于 Noyra 产品缺陷。GPT-5.6-sol 建议：**max**，无需产品代码修复。
4. **T-04：升级和 rollback 的第一次 readiness 轮询出现短暂 connection refused。**
   - 风险等级：**P3，预期停服窗口观察项**；安装器在切换期间先停止旧进程，随后等待新进程
     ready；日志中的一次 `curl: (7) Failed to connect` 发生在该有界停服窗口内，后续均获得
     HTTP 200，安装和 rollback 返回 0。
   - 影响：日志可能让操作员误以为升级失败，但本次没有指针错误、数据损坏或最终不可用。
   - 修复风险：**低**；如需改善只应调整安装器日志级别/重试输出，不应放宽 readiness 门禁。
     GPT-5.6-sol 建议：**max**。

### 资源清理与验收结论

测试主体于 10:51:02 完成清理；服务已停止并禁用，数据卷已卸载，临时 LUKS 文件已删除，
临时发行版已注销，E 盘测试目录和导出包已删除。清理后应再次确认 `wsl --list --verbose`
中不存在 `Noyra-ShortTest-240821-2h`，并确认原 `Ubuntu-24.04` 仍为原状态。两小时 soak
证明当前候选版本在本机隔离 WSL/LUKS 环境的基本部署、systemd、健康检查、崩溃恢复、升级、
回滚和稳定性路径通过；它不替代生产主机恢复演练、clean Windows VM、签名 MSI/MSIX、真实
GitHub tag/Sigstore/provenance 或多日 soak。
