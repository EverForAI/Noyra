# 第 1 步：公开内容冻结

日期：2026-09-06。公开署名：Jaxon Grey。
GitHub 用户名：EverForAI；仓库名称尚未确定。
源码基线：`454894312906073e22554fda585efd285323afe7`，分支 `codex/candidate-20260822`。
公开文案与作者元数据提交：`34f441d7b39c697eb98eed435e2db565b54fb66b`；本记录的收尾修订另行提交。

本记录对应用户确认的七步发布流程中的第 1 步，只冻结本地文案与署名元数据。
不等于全部仓库内容已可公开，也不等于已发布或已取得首创认证。
在现有开发目录中工作，不另建工作树、不改写历史，不修改业务代码和运行配置。

## 冻结范围

| 文件 | 冻结内容 |
| --- | --- |
| [README](../../README.md) | 标题、署名、作者主张限定、定义摘要、能力状态、两项 P1 延期风险、开发示例边界 |
| [NCAS v1.0](../research/non-command-oriented-subject.md) | 八项判据、代码与测试入口、保留接口、管理控制与真实环境的定义 |
| [相关工作](../research/2026-09-06-ncas-prior-art.md) | 限定搜索范围、来源、未决候选、统一判定原则 |
| [包元数据](../../pyproject.toml) | 作者改为 Jaxon Grey；版本仍为 0.1.0、许可证仍为 Apache-2.0 |
| [发布计划](../superpowers/plans/2026-09-06-research-preview-release.md) | 本步骤进度与后续门禁，未完成项继续保留 |

## 检查清单

- [x] 使用用户确认的标题、公开署名 Jaxon Grey 与 GitHub 用户名 EverForAI；未自行推断邮箱、组织或仓库名。
- [x] 首创定位明确为作者可修订主张，没有把限定检索冒充不存在先例的证明。
- [x] 区分适配实现、本地模拟测试与真实环境验收；明确自主任务发布、赏金、转账、打赏、独立 signer、授权文件读写和受控真实资金接入仍属完整项目规划，首发仅暂不启用相关高风险能力。
- [x] 保留 `human_proposal` 接口事实，不声称输入完全没有任何因果影响。
- [x] 首屏列明两项 P1 延期风险及无 signer、无自动化、无文件授权、无真实资金的首发要求。
- [x] 将 README 中旧阶段数字标为历史验收，去除首页硬编码的个人工作目录。
- [x] 文档链接、包元数据与定向回归完成检查；构建留待后续步骤。
- [x] 文案最终审阅与内容指纹记录。

## 验证范围

本步已核对文档、署名元数据及与定义相关的定向回归；本次结果见下方“验证结果”。
最终干净发布提交上的 targeted smoke、pressure、full soak 属于后续发布验证，未在此替代或宣布通过。
文档列出某个测试，不等于已经执行它。本步不调用真实模型、signer 或资金，不启动服务。

## 验证结果

- `git diff --check`：通过。
- 文档存在性与关键字段检查：标题、Jaxon Grey、EverForAI、NCAS、AUD-12、AUD-05 均存在。
- 本地 Markdown 内联链接：62 处目标存在；本检查不覆盖外部 URL 可达性或页内锚点。
- 使用 `tomllib` 解析确认作者为 Jaxon Grey、版本为 `0.1.0`、许可证为 Apache-2.0；下列 4 个文件指纹全部匹配。
- 冻结收尾时重新运行定向回归：`73 passed in 345.22s`。命令：`.\.venv\Scripts\python.exe -m pytest tests/test_cognition.py tests/test_interaction.py tests/test_goal_governance.py tests/test_action_deliberation.py tests/test_kernel.py tests/test_capability.py`。此前记录的 subtests 数量不作为本次重跑结果。
- 本次未运行构建、秘密扫描或完整 wallet 门禁；它们属于下一步公开安全审查及后续最终提交验证。
- 公开文案与作者元数据已创建本地提交 `34f441d`；未创建标签、GitHub remote 或远端仓库，未推送。

冻结公开文案与包元数据的文件 SHA-256（按本次工作树文件字节计算；流程记录与发布计划不纳入，避免自引用并允许更新后续进度）：

| 文件 | SHA-256 |
| --- | --- |
| `README.md` | `9822cdaf8db1682dc7df8ec6af710390160802cdead817d2020f43bbb02a06c7` |
| `pyproject.toml` | `be348492ae7463c21a40e6ca6d705067baf39da0f7df15bbcb9edeea4564627a` |
| `docs/research/non-command-oriented-subject.md` | `0036d1de340368439676ba11d4e247c93ce50af32c6d131396b1bbd63d0f6633` |
| `docs/research/2026-09-06-ncas-prior-art.md` | `36d627bccc48b958bf0feafb0834dcd1cdb21c0db9df1fad1adcd66158ec578e` |

## 下一步与阻断项

下一步是公开安全审查：扫描拟公开的 tracked 文件与拟推送历史中的秘密和私人数据，
检查示例配置与新数据目录，并整理最终公开文件清单。
README 的路径清理不等于整个仓库及历史已经脱敏。

GitHub 用户名已确认为 EverForAI，个人资料姓名与公开署名为 Jaxon Grey。
仓库名称、私密安全联系渠道尚未提供；未核验账号登录，也未创建 GitHub remote 或远端仓库。
保留 Apache-2.0 正文及第三方版权，不修改 Git 全局身份，不创建虚构邮箱或远端 URL。

当前 `release.yml` 对所有 `v*` 标签进入生产门禁。研究预览流程未拆分前，不创建或推送版本标签，
不绕过真实 testnet/KMS/备份/长期运行的稳定发布要求。
