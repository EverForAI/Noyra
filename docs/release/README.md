# 公开源码清单与发布校验

本目录的 `public-files.txt`、`public-files.sha256` 与 `public-files-excluded.txt` 描述**当前 Git 提交的人工审核源码包**。2026 年 9 月的发布报告保留当时的时间、数量和审查范围，不能用来代表当前版本。

## 范围与字节约定

- 允许清单逐个列出可以进入 `noyra-source.zip` 的路径；新增路径需要人工审核。
- 排除清单逐个列出不进入该源码包的已跟踪路径及理由，不支持通配符。
- 每个 Git 跟踪路径必须有且只有一种归属；缺失、重复或新增未审核路径会使校验失败。
- 排除清单控制的是本工具生成的源码包。排除文件仍可能已经存在于 Git 历史、GitHub 仓库或 GitHub 自动生成的源码归档中；它不是保密或删除机制。
- SHA-256 对 Git blob 的原始字节计算，不读取工作区内容，不手工替换换行，不把图片和 gzip 当作文本解码。Windows 的 PowerShell 工作区 CRLF 不参与此校验。
- 校验值覆盖全部允许路径，唯独不对 `public-files.sha256` 自哈希。该文件由 Git 提交固定；发行包整体再由正式发布的 `SHA256SUMS` 与签名覆盖。
- 源码包不允许符号链接或子模块；工具同时检查路径穿越、大小写冲突、Windows 保留名称等跨平台问题。

## 更新流程

工具只使用 Python 3.11+ 标准库和 Git，无需运行 Noyra，也不接触数据库、服务器或凭据。

先审核本次变更，更新两份路径清单，并按明确路径暂存所有准备提交的源码及清单。然后运行：

```sh
python scripts/public-file-manifest.py generate --index
git add -- docs/release/public-files.sha256
python scripts/public-file-manifest.py check --index
git commit -m "Update reviewed public source inventory"
python scripts/public-file-manifest.py check
```

`generate --index` 从暂存区的不可变 object ID 读取内容，仅写工作区校验表，不自动暂存、不批准新文件。生成后暂存区任何受覆盖内容再次变化，都必须重新生成、暂存并校验。默认 `check` 检查 HEAD，不检查尚未提交的实验性改动；检查待提交内容必须显式使用 `--index`。

## 精确提交与实际归档校验

```sh
python scripts/public-file-manifest.py check --revision HEAD
python scripts/public-file-manifest.py export --revision HEAD --output noyra-source.zip
python scripts/public-file-manifest.py verify-export --revision HEAD --archive noyra-source.zip
```

也可以将 `HEAD` 替换为明确的提交 SHA 或标签；所选提交须包含本格式的三份清单。早期发布仍按当时规则核验，本工具不自动迁移或认可旧版本。输出目录需事先存在；已有归档不会被覆盖。测试用输出应放在仓库外或已忽略的 `dist/` 中。

导出器只复制所选快照中允许的原始 blob，保留 Git 执行位，固定 ZIP 条目顺序和时间，绕过 `.gitattributes` 的 `export-ignore` / `export-subst` 转换。导出后再次核对实际 ZIP 的路径、重复条目、文件类型、执行位和每个文件的精确字节。校验表自身也会与 Git 中的内容比较。

源码包内不含 `.git`；使用 `verify-export` 对照提交时，需要一个含对应提交的 Git checkout。下载包整体还应核对发行页的签名校验表。GitHub 自动生成的 Source code ZIP/TAR 不属于这份人工允许清单的产物。

## 完整候选的发布边界

提交前的原生钩子仍使用 `--staged`，只检查当前暂存内容。发布包含多个提交的候选时，
需要使用可信已审基线覆盖整个引入范围；单独的 `--revision HEAD` 只证明一次提交的窄范围。

```sh
python scripts/check-publication-boundary.py --base REVIEWED_BASE_COMMIT --revision CANDIDATE_COMMIT
```

先把两个占位符换成真实的已审基线和候选提交。工具验证完整祖先关系，并检查全部新可达
提交的快照；较早提交或合并侧历史里的内容，不能因最后一次只改了别的文件而漏过。
已审基线里同路径的旧文本继续按历史豁免处理，不因此扫描全部历史或重写旧记录。

普通 PR 和 main 推送自动从 GitHub 事件确定范围。手动 quality 可以填写 `publication_base`
完整 SHA，或使用仓库变量 `PUBLICATION_REVIEW_BASE`；**标签发布前必须配置该变量为
已审的严格祖先 SHA**。缺失时会提前拒绝，不自动以候选自身或最近父提交充当已审基线。
此修复不会远程创建变量或自动认可任意基线。

完整历史、失败语义、合并/重命名与基线信任要求见
[候选范围检查合同](../development/publication-range-guard.md)。

## CI 与正式发布

普通 CI 在 Linux 和 Windows 上分别运行清单校验、实际导出和归档复核；现有 Python 测试矩阵运行专门的回归测试。发布质量门禁再次检查标签提交，正式发布任务从同一个提交生成 `dist/noyra-source.zip` 并复核，再将它和其他发行产物一起纳入现有 `SHA256SUMS`、签名与发布附件流程。

任一阶段失败时应修复路径审核或校验值，不能通过跳过检查、自动允许所有新增文件或按工作区文本重算来消除告警。

## 公开材料边界

运行源码、网页资源、部署模板、脚本、测试及合成历史数据库夹具继续纳入人工审核范围。原始机器证据、运行日志、Git bundle 和本地生成目录不随当前源码快照跟踪；私有审计与完整验证记录在仓库外保存。公开摘要不把未完成事项改写为已修复，也不把源码验证等同于生产部署。

普通删除或取消跟踪不会清除 Git 历史、旧 PR 引用、缓存和已下载副本。本次材料整理不是历史擦除，也不承诺降低完整历史克隆的体积。

本地提交钩子与现有 CI 共同检查当前快照的发布边界，安装方式和范围见 [贡献指南](../../CONTRIBUTING.md#publication-boundary)。清单工具仍负责精确 Git blob 的允许、排除和归档一致性，两者不能互相替代。

## Backup-key administration compatibility

Rotation now requires an explicit saved request ID and the expected source
generation/hash; the old implicit command/API fails before mutation. Python
`BackupKeyring.rotate` returns operation metadata rather than secret keyring
material. Scripts must migrate according to the
[backup-key command and exit-code contract](../development/backup-key-commands.md).
Initialization, backup creation/restoration and runtime key reading are unchanged.
Unknown outcomes must resume the same request. Windows durable rotation reports
unsupported before mutation; it does not silently downgrade the durability claim.
