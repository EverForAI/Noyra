# R03 Windows 恢复协议原型验证报告

日期：2026-10-25（北京时间）
验证分支：`codex/audit-r03-protocol-validation`
产品基线：`b069cd3ea7793390b4a18a8791c7de70a48e6647`
验证性质：只在 `tests/` 中验证恢复协议原型；本轮没有修改生产代码、部署脚本、工作流、依赖、生产数据库或服务器。

## 1. 结论

R03 的核心协议方向已经通过原生 Windows 故障注入验证：可以用一个持久的权威状态记录绑定版本选择、previous、信任策略、严格签名要求和事务意图；在新策略已经持久化为 `PREPARED` 后，恢复可以前滚或明确阻断，不能把已经撤销的 signer 自动恢复为可回滚信任。兼容文件可以继续保留，但只能作为镜像，不能再作为权威事实来源。

验证同时发现一个阻止直接生产落地的关键集成条件：**旧版生命周期入口不会识别新 marker 和权威状态，仍会读取旧的 `current.txt`、`previous.txt`、`trusted-publishers.json`。** 在原型已经接受“撤销 A”的 `PREPARED` 状态后，未改造的旧入口仍能使用旧镜像把 A 版本选为回滚目标。仅增加状态文件和恢复脚本不能解决这个问题，必须把新协议接入唯一受信生命周期入口，并对旧入口建立明确的版本门槛或拒绝路径。

因此，本轮结果是：

- 原生协议设计：**可继续进入生产实现设计**。
- 当前原型：**不可部署、不可直接复制到生产**。
- 当前产品 R03：**仍未修复**，原有问题仍在产品基线中。
- 下一步优先级：先实现统一受信入口和 bootstrap/旧版本门控，再实现生产状态协议、真实升级/恢复和 rollback 集成；每个模块单独审计和提交。

## 2. 风险分级与归因

| 编号 | 问题或边界 | 当前风险 | 修复风险 | 归因 | 当前结论 |
| --- | --- | --- | --- | --- | --- |
| R03-BASE | signer 轮换时多文件发布中断，会留下旧 current、被覆盖的 previous 和已撤销的旧信任策略 | P2 | 高 | 原生存量问题；早于本轮原型 | 必须修复 |
| R03-GATE | 旧 lifecycle 入口绕过新 marker/authority，可能复活已撤销 signer 的旧版本 | 对新协议落地而言 P1；对当前未接入协议的基线属于集成缺口 | 高 | 原生旧入口能力，被本轮验证暴露；不是本轮产品次生问题 | 未解决前禁止生产启用新协议 |
| R03-BOOT | marker 已写入、authority 尚未写入时，系统按 fail-closed 阻断，但原型还没有受控的 bootstrap 修复路径 | P2 可用性风险 | 中至高 | 原型限制；不是生产代码次生问题 | 生产实现必须提供人工/受控恢复流程 |
| R03-DUR | 已验证进程异常退出和文件句柄故障，尚未证明断电、文件系统元数据落盘和存储设备故障下的完整持久性 | P2 验证缺口 | 高 | 验证边界 | 进入生产前需做断电/重启或明确写入合同 |
| R03-AUTH | envelope 的 SHA-256 用于损坏检测，不是对本机管理员的防篡改认证，也没有外部单调计数器 | 在信任本机管理员的威胁模型下 P3 限制；若把管理员纳入攻击者模型则上升为 P1 | 高 | 协议边界，不是本轮引入 | 必须写入威胁模型，不能过度宣称 |
| R03-PS5 | Windows PowerShell 5.1 无法加载产品所需 CMS assembly；完整签名矩阵只在 PowerShell 7 执行 | P2 发布/运维边界 | 中 | 既有运行时要求，已有发布文档约束 | 生产门禁必须拒绝不完整运行时 |

“次生问题”在本报告中专指本轮原型或未来接入方式造成的新回归。R03-BASE 是原生问题；R03-GATE 是原有旧入口与新协议之间的原生集成缺口，本轮只读验证把它证明出来，没有把它写入产品，因此不属于本轮次生问题；R03-BOOT、R03-DUR、R03-AUTH 是原型范围和威胁模型边界，不能伪装成已完成的生产能力。

## 3. 原生 R03 问题复现

原产品仍使用独立的回执、trust、previous 和 current 文件，每个文件虽然各自采用临时文件和原子替换，但没有整组状态的持久提交点。安装末段的逻辑顺序是先写回执，再写 publisher trust，再写 previous，最后写 current；任意后续步骤失败都可能停在中间状态。

验证使用真实签名包和原生 Windows 文件共享语义完成以下路径：

1. 安装 signer A 的 `a1`。
2. 升级到 signer A 的 `a2`，使 `previous=a1`。
3. 用允许读、禁止替换的句柄占用 `current.txt`。
4. 尝试升级到 signer B 的 `b3`，同时把信任策略改成 B 加系统 pin，并撤销 A。
5. 升级失败后检查持久状态。

结果为：`current` 仍为 `a2`，`previous` 被改成 `a2`，原来的 `a1` 回滚选择丢失，保存的信任策略已经不再包含 A，后续原产品 rollback 因旧包真实 signer 已不受信而失败。数据目录和已引用版本没有被删除。

这正是原审计记录的 P2 高修复风险问题：失败的升级改变了仍处于选中状态的版本所需的信任条件，并破坏了回滚记录。它不是单个文件写入是否原子的缺陷，而是多文件状态缺少同一提交世代和恢复意图的缺陷。

## 4. 原型协议和撤销语义

原型将以下内容放入单个权威 `r03-state.json`：

- 安装根和数据根绑定摘要；
- `generation`；
- `phase`；
- 当前版本和 previous 版本；
- 实际信任 pin 集合；
- 是否强制签名验证；
- 完整事务意图：事务 ID、目标版本、manifest 摘要、previous 和下一世代号。

`current.txt`、`previous.txt`、`trusted-publishers.json` 仍被写入，是为了兼容旧工具，但在新入口中只允许作为镜像。marker `r03-protocol-required` 一旦建立，authority 缺失、损坏、字段多余、路径不安全、安装绑定不匹配或摘要错误，都直接失败，不从兼容镜像猜测恢复。

状态转移如下：

| 状态 | 含义 | 是否允许 start/rollback | 恢复动作 |
| --- | --- | --- | --- |
| `ready` | 权威选择和信任策略一致 | 允许，前提是重新验包 | 无 |
| `prepared` | 目标包和新信任策略已核验，撤销语义已经持久化，尚未完成激活 | 阻断 | 重新验包后前滚；候选损坏时保持阻断 |
| `committed` | 新选择已提交，兼容镜像尚未全部修复 | 阻断 | 只修复镜像，再回到 `ready` |

本原型采用的撤销语义是：**durable `PREPARED` 成功后立即生效。** 在此之前，失败命令保留旧策略；在此之后，即使新包损坏、恢复进程崩溃或镜像修复失败，也不得把已撤销 signer A 放回有效信任集合。原运行进程不会被原型自动终止；协议只禁止新的受支持 start/rollback，并要求恢复路径前滚或明确阻断。

同一事务 ID 只能以相同版本、manifest 摘要、pin、严格性和基础世代重试；不同未完成意图、旧世代、相同版本但不同策略的变更均拒绝。相同版本的策略变更需要另一个明确操作，避免借升级重写当前保护条件。

## 5. 原生验证范围和结果

最终执行命令使用 pytest 运行原型及既有相关 Windows 验证：

```text
pytest tests/test_audit_r03_protocol_validation.py \
  tests/test_audit_6a742e5_windows.py \
  tests/test_audit_r02_windows_state.py \
  tests/test_m42_p3_05_windows_package.py \
  tests/test_windows_process_identity.py -q --tb=short \
  -W error::pytest.PytestUnhandledThreadExceptionWarning \
  --junitxml=native-validation.xml
```

结果：**19 passed，0 failed，0 skipped，270.597 秒**。其中 R03 的 pytest 场景内部执行了以下原生 Windows 验证：

- 108 个故障矩阵场景：18 个故障点 × 3 种策略（A→A、A→B 保留 A、A→B 撤销 A）× 2 种故障模式（显式异常、进程崩溃）；
- 4 个 legacy adoption 中断点；
- 10 个真实锁文件场景，覆盖 authority、receipt、trust、previous、current 以及读共享/禁止共享语义；
- 6 个文件、目录、junction 和非法目标类型场景；
- 9 个 authority 缺失、损坏、超限、摘要错误、路径穿越、多余字段、缺失 intent、错误安装绑定、标量 pin 等场景；
- 恢复本身连续崩溃后重试，并验证重复恢复幂等；
- 接受意图后候选包被篡改或损坏时保持 revoked/blocked，不回退到旧信任；
- stale generation、不同事务、同一事务内容冲突和相同版本策略歧义均拒绝；
- manifest/launcher 的签名不能自我授权；实际 signer 检查仍由现有产品 `Verify-Package` 完成；
- 跨进程锁与旧 lifecycle 的锁竞争；
- development adoption 明确允许时可用，已经接受签名包后不能通过 development 标志降低严格性；
- 未执行 launcher，未导入证书：两项计数均为 0。

关键结果均记录在工作区外的证据文件中：

- `[私有验证记录]`
- `[私有验证记录]`
- `[私有验证记录]`
- `[私有验证记录]`

## 6. 旧入口绕过：正式落地的阻断条件

这是本轮最重要的新发现。测试过程先让原型在 `PREPARED` 状态接受 B 并撤销 A，然后调用未修改的旧 lifecycle：

- 新原型的 `authorize-start` 正确阻断并要求恢复；
- 新原型的 `authorize-rollback` 正确阻断；
- 未修改的旧 lifecycle 忽略 marker 和 authority，仍从旧镜像读到 `a2` 和 A 的信任；
- 旧 lifecycle rollback 能把 `a1` 选为 current，证明旧入口可以绕过新协议的撤销状态；
- 原型恢复后会重新把 authority 和兼容镜像修复为 B 的权威结果。

这不是“再多写一个状态文件”能够修好的问题。正式实现必须满足：

1. start、rollback、upgrade、recover、status 都经过同一个受信入口和同一个状态读取器；
2. 旧版本入口在检测到 marker、authority 或协议版本后，必须拒绝执行并返回可诊断错误；
3. 安装器必须在启用协议前完成入口门控，不能先写 marker 再留下可绕过的旧脚本；
4. 服务启动器、systemd/Windows service、手工生命周期命令和恢复任务不能各自维护一套选择逻辑；
5. 任何回滚选择都必须以 authority 的 generation、trust、真实 signer 和运行时版本门槛为准；
6. 旧二进制被本机管理员直接复制并执行时，文件协议本身不能保证阻止它；如果这个威胁模型需要防御，必须增加 OS 权限、签名入口或外部防回滚/版本门控，并把它单独写进安全目标。

在这些条件完成前，不能把原型声称为可部署修复，也不能通过永久保留 A+B、无条件恢复旧 allowlist、跳过真实 signer 校验来“保证可回滚”。

## 7. Bootstrap、持久性和运行时边界

### 7.1 bootstrap 中断

原型会先写 marker，再写 authority。若在 marker 已落盘而 authority 尚未落盘时进程中断，后续读取会 fail closed，不会从旧镜像猜测状态。这保护了撤销和权限边界，但原型没有实现供运维使用的受控 bootstrap 修复命令。生产实现必须记录 bootstrap 阶段、保留可验证的迁移证据，并提供只允许恢复到明确旧状态或明确阻断状态的操作流程，不能让普通 retry 重新导入任意旧 trust。

### 7.2 断电持久性

原型使用临时文件、`Flush(true)`、已有文件替换 helper、真实共享句柄和跨进程 lock；本轮验证了进程异常退出、读锁/写替换失败和重试，但没有模拟真实断电、存储设备缓存丢失或 NTFS 元数据恢复。因而结果证明的是“在本测试模型的进程和文件操作故障下保持协议不变量”，不是对所有断电场景的完整持久性证明。生产前应做受支持 Windows 主机上的重启/断电实验，或把底层持久性合同、文件系统要求和恢复证据明确化。

### 7.3 PowerShell 版本

PowerShell 7.6.5 执行了完整签名矩阵。Windows PowerShell 5.1.22000.2538 的能力探针显示，产品所需的 `System.Security.Cryptography.Pkcs` assembly 不可用；在该主机上签名 adoption 在写入 marker/state 前失败，旧选择保持不变。没有使用 framework 类型或替代 shim 伪造 PS5 的签名覆盖。现有发布门禁已要求完整 PowerShell 7，因此生产安装器和运行时应在能力检查不通过时明确拒绝，而不是将错误归因成包内容或签名损坏。

### 7.4 authority 摘要的边界

authority envelope 中的 SHA-256 只用于检测损坏，不能认证一个拥有本机管理员权限的人，也不能单独提供跨重装或跨文件系统的单调防回滚。这个边界在本机管理员被信任的安装模型下可以接受，但必须进入威胁模型和管理后台解释；若未来要求防御恶意本机管理员，需要独立的 OS ACL、签名/密钥保护或外部不可回退计数器，不能把摘要字段误称为防篡改认证。

## 8. 安全审计结论

本轮没有发现由原型测试代码引入到产品的 P0/P1 问题，因为原型没有进入 `src/`、`deploy/`、`scripts/`、`.github/` 或依赖。原型自身通过了预定的不变量验证，但暴露了 R03 正式落地的 P1 级入口门控风险和 P2 级 bootstrap/持久性实施缺口。原生 R03 多文件事务缺陷仍为 P2、高修复风险。

原型方向可保留，生产代码不能直接拷贝。建议保持以下安全边界：

- 先做统一受信生命周期入口和旧入口拒绝，再做生产 authority；
- 先把 bootstrap、recover、status 和 start/rollback 的入口合同写成可执行测试，再接入真实 installer/service；
- 以 durable `PREPARED` 作为撤销生效点，永不以“恢复可启动”为理由自动恢复已撤销 signer；
- 每个写入/替换/恢复步骤都必须有正常、异常、崩溃、重复执行和真实 signer 验证；
- 兼容镜像只允许由 authority 投影，不能让镜像反向覆盖 authority；
- 每个模块完成后单独审计和提交，完成后再进入下一个模块。

## 9. 下一步建议

建议按以下顺序进入生产实现，仍然一次只做一个模块：

1. **入口门控与 bootstrap 模块**：定义协议版本、marker、authority 缺失/损坏行为，改造所有 lifecycle/service/launcher/recovery 入口，拒绝旧入口绕过；先复现并关闭 R03-GATE。
2. **权威状态与事务核心模块**：把 generation、intent、trust、current、previous 和严格性绑定，完成 PREPARED/COMMITTED/READY 及幂等、冲突拒绝、安装绑定。
3. **真实升级/恢复模块**：把现有包验证、回执、版本目录、兼容镜像和 recovery service 接入核心；不运行未经验证的 launcher，不删除被引用版本和数据目录。
4. **rollback/start/status 模块**：所有选择统一读取 authority，pending/blocked 状态显式可见，实际 signer 重新验证；补齐旧版本和服务启动门禁。
5. **原生 Windows 持久性与发布门禁模块**：在完整 PowerShell 7 下加入重启/断电验证、能力预检、安装迁移和可诊断运维手册。
6. **单独模块审计、提交和总体验收**：先跑受影响 native matrix，再跑既有 Windows 回归，确认生产 diff、签名和工作区状态后提交。

本轮没有自动开始上述生产实现；下一步应由用户单独授权 R03 生产模块 1 的实现。
