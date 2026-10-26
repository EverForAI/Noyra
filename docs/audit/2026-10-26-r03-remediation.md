# R03 Windows signer 轮换与恢复事务修复报告

日期：2026-10-26（北京时间）
分支：`codex/fix-r03-recovery`
修复基线：`d471225`（R03 原型验证提交）
范围：Windows 安装生命周期的权威状态、signer 轮换、撤销、恢复、回滚、启动门控和发布包集成。

## 1. 最终结论

R03 已完成生产实现修复，覆盖受支持的 Windows PowerShell 7 lifecycle 入口：`install`、`upgrade`、`rollback`、`start`、`stop`、`recover`、`status` 和 `uninstall`。修复将版本选择、previous、publisher trust、严格签名要求、事务意图和 generation 绑定到 `r03-state.json`；`current.txt`、`previous.txt`、`trusted-publishers.json` 只保留为由权威状态生成的兼容投影。

核心语义是：

- 真实包和实际 signer 先由既有 `Verify-Package` 验证；包内 publisher metadata 不能自我授权。
- 已有 legacy 安装在受支持 lifecycle 首次接触时，先验证 current/previous 包，再写入 authority 和 marker；验证失败不改变旧状态。
- 旧安装的 `DataRoot` 和版本目录不被安装/恢复失败删除。
- 新事务先写带安装绑定、prior state、目标版本、目标 digest、pins 和严格性的一次性 transition record；再将旧兼容投影置为不可被旧入口正常读取的 pending 值；最后持久写入 `PREPARED` authority。
- durable `PREPARED` 成功后，新 trust/revocation 生效；之后只允许前滚或明确阻断，不恢复被撤销 signer。
- `PREPARED` 之前发生失败时，受支持 recovery 使用 transition record 恢复 prior authority 和兼容投影；`PREPARED` 之后的失败保留新 trust 和 pending 状态，恢复只前滚。
- `COMMITTED` 后若兼容投影修复失败，`status` 仍以 authority 为准，`recover` 可重复执行并最终回到 `ready`。
- pending/blocked 状态下，受支持 `start`、`rollback`、新 install/upgrade 都不能绕过恢复；`stop` 仍可执行；`uninstall` 要求 ready 并保留 DataRoot。
- `status` 显示 `protocol_version`、`generation`、`recovery_phase`、`recovery_target_version`、`recovery_kind` 和 `recovery_required`，避免后台只读旧镜像而误报。

原 R03 的多文件中断缺陷和新协议入口绕过风险已在受支持实现中关闭。原型测试代码没有被复制成生产入口；生产实现使用独立的签名包 helper，并已加入 Windows package input 和公开文件校验清单。

## 2. 问题、根因和修复归因

| 编号 | 原问题 | 根因 | 修复结果 | 归因 |
| --- | --- | --- | --- | --- |
| R03-BASE | A→B 撤销 A 时 current 发布失败，可能留下旧 current、被覆盖的 previous 和已撤销旧 trust | 四个独立文件各自原子替换，没有同一 generation/intent 提交点 | 单一 authority + transition + PREPARED/COMMITTED/READY 恢复协议；已覆盖 receipt、trust、previous、current 失败 | 原生问题，已修复 |
| R03-GATE | 新协议状态存在时旧 lifecycle 仍能读取旧镜像并回滚 A | 旧入口不知道 marker/authority | 所有受支持入口统一读 authority；transition 阶段将旧 current/previous/trust 置为不可正常读取；启动/回滚/恢复均受同一锁和状态门控 | 原生集成缺口，本轮修复关闭；不是次生问题 |
| R03-BOOT | marker/authority/迁移中断时缺少受控恢复 | bootstrap 只有 fail-closed，没有 prior-state 恢复证据 | transition record 保存验证过的 prior state；`recover` 可恢复 pre-acceptance；损坏或安装绑定不匹配仍拒绝猜测 | 原型/生产缺口，已修复受支持路径 |
| R03-STATUS | 后台只读取兼容镜像，可能显示错误 current/previous | status 没有权威读取合同 | status 优先 authority，并暴露 phase、target、kind、generation | 原生问题，已修复 |
| R03-RESIDUAL | 断电、设备缓存、恶意本机管理员仍不是文件协议能完全解决的威胁 | 文件摘要不是外部防回滚认证；没有模拟真实断电 | 已明确写入发布文档和威胁边界；不宣称已解决 | 明确残余边界，不是本轮次生问题 |

## 3. 模块实施与模块审计

### 模块 1：authority、transition、bootstrap

新增 `deploy/windows/noyra-r03-recovery.ps1`，实现：

- exact-field authority envelope、SHA-256 损坏检测、安装根/DataRoot 绑定；
- generation、phase、intent、kind、目标版本、manifest digest、previous、pins 和 strict 绑定；
- transition record 保存已验证 prior state，用于 PREPARED 前恢复；
- marker 存在但 authority 缺失、authority 损坏、安装绑定错误、字段多余、pins 非法、路径不安全时 fail closed；
- legacy adoption 验证 current/previous 实际包后才发布 authority；
- same intent 幂等，stale/conflicting intent 和相同版本策略歧义拒绝。

审计结果：未发现 P0/P1 回归；发现并修正了空 pins 的 PowerShell 数组退化、PS5 签名测试参数误用、原型状态文件与生产状态文件同名污染等问题。

### 模块 2：install/upgrade transaction

生命周期升级顺序变为：

1. 验证输入包和实际 signer；
2. 验证/复用版本目录，保存完整 manifest bytes；
3. 为已有选择创建 transition prior-state；
4. 暂时中和 legacy current/previous/trust 投影，防止旧入口选包；
5. 持久发布 `PREPARED` authority；
6. 记录 verification receipt；
7. 发布 `COMMITTED` authority；
8. 从 authority 修复兼容投影并回到 `READY`。

审计结果：失败不会把新包 metadata 当 trust；新 signer 撤销旧 signer 后，旧 signer 不会因为恢复可启动而重新进入 pins；版本目录和 DataRoot 保留。

### 模块 3：rollback/start/status/recover/uninstall

- rollback 目标来自 authority 的 previous，不再读取旧 previous 作为真相；
- start 目标来自 authority current，重新验证实际包和签名；
- status 在 committed/prepared/transition 中显示 authority/恢复状态；
- recover 幂等，支持 receipt、mirror、authority 写入失败后的重试；
- stop 保持可用，uninstall 禁止绕过 pending recovery。

审计结果：兼容文件被手动篡改或损坏不改变 status 的 authority 结果；被撤销 signer 的回滚被拒绝，current 不改变。

### 模块 4：package/release integration

- Windows package builder 已将 `noyra-r03-recovery.ps1` 纳入签名包输入和 manifest inventory；
- `docs/release/windows-package-gate.md` 增加 R03 authority、transition、恢复、PS7 和威胁边界说明；
- `docs/release/public-files.txt` 与 `public-files.sha256` 已更新；
- 未引入新的依赖或证书导入行为。

## 4. 最终验证

### 原生 R03 验证

命令：

```text
pytest tests/test_audit_r03_protocol_validation.py -q \
  --tb=short -W error::pytest.PytestUnhandledThreadExceptionWarning
```

结果：**2 passed，0 failed**，约 3 分 20 秒。

内部验证覆盖：

- 108 个原生 fault/crash matrix：18 个故障点 × 3 个 signer 策略 × 2 个故障模式；
- 真实 CMS/Authenticode 包验证；
- authority、receipt、trust、previous、current 的状态目标类型和共享句柄；
- pre-acceptance transition 恢复与 post-PREPARED 前滚；
- commit 后兼容镜像修复失败与幂等恢复；
- signer 撤销、候选包损坏、stale/conflicting intent、安装绑定和字段污染；
- legacy adoption、rollback/start/status/recover；
- launcher 执行数为 0，证书导入数为 0。

### 既有 Windows 回归

命令：

```text
pytest tests/test_audit_6a742e5_windows.py \
  tests/test_audit_r02_windows_state.py \
  tests/test_m42_p3_05_windows_package.py \
  tests/test_windows_process_identity.py -q \
  --tb=short -W error::pytest.PytestUnhandledThreadExceptionWarning
```

结果：**17 passed，0 failed**。

### 静态验证

- 所有新增/修改 PowerShell 文件 AST parse 通过；
- Ruff check 通过；
- Ruff format check 通过；
- Mypy `src tests` 通过（478 files）；
- `compileall` 通过；
- `pip check` 通过；
- `git diff --check` 通过。

证据目录：`D:/metagrowh/.user-temp/noyra-r03-production-20261026/`。其中保存原生 JUnit、PS7 matrix 和 PS5 runtime boundary 记录。

## 5. 残余风险和不应过度宣称的边界

1. 本轮证明的是受支持完整 PowerShell 7 lifecycle 的进程崩溃、文件句柄故障和恢复不变量，不是突然断电、NTFS 元数据回滚或存储设备缓存丢失的完整证明。发布前仍建议在目标 Windows 主机做重启/断电演练。
2. authority envelope 的 SHA-256 是损坏检测，不是防御恶意本机管理员的签名，也不是外部单调 anti-rollback counter。
3. 一个拥有本机管理员权限的人可以复制旧代码并直接执行；文件协议无法单独阻止任意旧副本。正式部署必须只使用签名包内 lifecycle、受支持 PowerShell 7 和受控服务/入口；若威胁模型包括恶意本机管理员，需要额外 OS ACL、代码签名入口或外部版本门控。
4. Windows PowerShell 5.1 不提供产品 CMS 所需 assembly，完整 signed flow 仍只支持 PowerShell 7；PS5 会在状态变更前失败，不伪造通过。

这些是明确的运行时/威胁模型边界，不是本轮引入的次生问题，也不构成“R03 已完全防御所有本机管理员或断电故障”的宣称。

## 6. 最终质量评价和后续建议

本轮 R03 修复质量评价：**高**。理由是修复已从单文件补丁提升为 authority/transition/恢复协议，真实 signer 验证保持不变，覆盖了正常、异常、进程崩溃、锁文件、损坏状态、重复恢复和兼容镜像篡改路径；每个实现模块均执行了独立静态/原生回归审计。

建议下一步：

1. 保持当前分支工作区干净并提交本轮 R03 修复；
2. 不在本轮自动推送、合并或部署；
3. 在合并前让 GitHub Windows 2022 gate 运行完整 package/CI；
4. 合并前在实际目标 Windows 主机用受支持 PowerShell 7 做一次 disposable install→upgrade→signer rotation→recover→rollback rehearsal；
5. 如果未来要把“断电持久性”或“恶意本机管理员防护”纳入目标，再单独立项，不要把它们混入当前 R03 修复。

### 公开清单统计勘误（2026-10-26）

此前“528 项中有 240 项不匹配”的核查把二进制文件当作 UTF-8 文本并处理换行，产生 16 项误报。按基线 `fd007df` 的 Git blob 原始字节复核，实际为 304 项一致、224 项不一致，无缺失或重复。其中 223 项沿用此前失效校验值，另 1 项是本轮新增 `noyra-r03-recovery.ps1` 的记录值与最终提交内容不一致。因此，之前“本轮变更涉及条目均已核验”的表述撤回；这属于清单最终提交复核缺失。完整根因、清单范围修复与验收见[公开清单修复复审](2026-10-26-public-manifest-remediation.md)。本文前述 R03 功能验证与该发布清单问题应分别依据各自证据评估。
