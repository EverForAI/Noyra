# main 集成与跨平台门禁记录

用户明确授权推送本分支并合并到 main。本记录不表示生产部署或解除真实环境发布门禁。

## 历史集成

远端 main 起点为 `b9ee0c56ed4da1a21d2d500525f4fe0ffc673ea3`，审计分支为 `7b842c0`。此前 main 的部署/升级修复以 squash 提交集成，与本分支保留的原提交历史分叉。

`git diff --ignore-space-at-eol --exit-code origin/main 5e46b09` 返回0：本轮管理台优化实施前的部署基线和当前 main 的全部文件仅有行尾差异。由此确认远端 main 的部署修复均已在当前审计分支内。历史集成提交 `b8ac0d9` 以两个父提交连接分支，文件树与 `7b842c0` 完全相同，未丢弃 main 的独有功能修复；不是对未知冲突直接采用单侧内容。

PR：<https://github.com/EverForAI/Noyra/pull/14>。main 保护要求 cloud-profile、Docker、Ubuntu/Windows 的Python3.11/3.12四组回归，保护不放宽，自动合并只在必需检查通过后执行。

## 首次远端门禁结果与最小修复

`b8ac0d9` 的前端、Docker和cloud检查通过。Ubuntu3.12的整仓测试为 **2446 passed、43 skipped、2473.45秒**，安装器备份/权限专项及Ruff随后通过，但Linux Mypy失败：`migration/recovery.py` 的Windows专用 `ctypes.WinDLL/WinError/get_last_error`、`msvcrt.open_osfhandle` 和 `os.O_BINARY` 被Linux类型合同拒绝；Ubuntu3.11同样在Mypy失败。

根因是 `_open_backup()` 使用 `os.name` 做运行时分支，Mypy不能以此排除Windows专有接口；之前Windows默认Mypy通过不足以证明Linux类型门禁通过。这个问题早于本次历史集成，历史集成未改变代码；不把它归为E03测试断言修复引入。

修复只添加 `sys` 导入，将平台分支改为Mypy支持的 `sys.platform != "win32"`，对应标准CPython平台选择保持原义。Windows的CreateFileW共享拒写/拒删除、句柄关闭、fd转换以及POSIX的只读/O_NOFOLLOW逻辑不变。没有增加type-ignore、Any绕过或放宽备份校验，也未改变schema、鉴权、主体、聊天删除或账务。

本地验证：`mypy --platform linux src tests` 和 `mypy --platform win32 src tests` 均 **408文件通过**；`tests/test_migration_recovery.py` **14通过、20.63秒**，覆盖流式摘要、句柄/路径身份、在途修改、事务复核、proof与epoch。该文件Ruff check/format通过。新SHA必须重新取得GitHub必需检查；不得用第一次部分通过或本地结果冒充远端全绿。

第七轮只读审计报告记录的是 `f749a3a` 基线及当时本地验证，保持历史不改。后续当前跨平台门禁结果以本记录与PR真实运行状态为准。P1外部八门禁和其余审计问题继续保留，合并源代码不等于发布或生产验收。
