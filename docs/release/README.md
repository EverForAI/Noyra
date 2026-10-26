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

## CI 与正式发布

普通 CI 在 Linux 和 Windows 上分别运行清单校验、实际导出和归档复核；现有 Python 测试矩阵运行专门的回归测试。发布质量门禁再次检查标签提交，正式发布任务从同一个提交生成 `dist/noyra-source.zip` 并复核，再将它和其他发行产物一起纳入现有 `SHA256SUMS`、签名与发布附件流程。

任一阶段失败时应修复路径审核或校验值，不能通过跳过检查、自动允许所有新增文件或按工作区文本重算来消除告警。

## 本次范围复核

运行源码、网页资源、部署模板、脚本、测试及合成历史数据库夹具已纳入源码范围。已有公开审计探针继续保留。原始机器证据、日志、Git bundle、设计输入和本地任务交接记录以逐文件理由排除；公开 Markdown 审计结论仍可查阅。该归档是带明示排除项的源码快照，不声称囊括仓库所有历史证据附件。

修复顺序见 [实施计划](../roadmap/2026-10-26-public-manifest-remediation.md)，错误统计勘误及验收结果见 [修复复审](../audit/2026-10-26-public-manifest-remediation.md)。
