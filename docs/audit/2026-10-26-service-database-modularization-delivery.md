# 服务与数据库模块化交付与最终复审

日期：2026-10-26。基线：2e9d2572e559ae7b449e7eafae4ee6964b0a9583。实施分支：codex/modular-service-boundaries。

## 目标与结论

本轮处理 service.py 与 core/database.py 职责集中造成的维护风险：明确配置、HTTP 协议、领域路由、数据库定义与运行所有权的边界，补可执行不变量，并公开维护与响应预期。实施采取先固化行为、再机械搬移、逐模块复审和提交的顺序。

原结构性风险为 P2；一次性改写 HTTP/事务/生命周期的风险高。本轮把改动限制为显式依赖和兼容外观下的结构搬移，迁移执行与生命周期算法保持。最终复审没有确认本轮引入的阻断问题；不能据此证明不存在任何问题或替代后续 CI。

| 文件/范围 | 基线 | 完成后 |
| --- | ---: | ---: |
| src/noyra/service.py | 11,815 行 | 2,955 行 |
| src/noyra/core/database.py | 11,209 行 | 3,901 行 |
| 配置定义 | 原 service 内 | service_configuration.py，586 行 |
| HTTP 框架/IO/会话/准入 | 原 service 内 | http_transport.py，1,472 行 |
| HTTP 领域处理 | 原内嵌 Handler | http_routes 包，最大单文件 700 行 |
| 历史迁移定义 | 原约 6,700 行字典 | 9 个冻结片段，最大单文件 857 行 |

减少原文件行数不是唯一指标。新协议、兼容委派和模块导入会增加一些总行数，换取独立边界和可检查依赖；没有用大型 Mixin 或动态执行源码实现表面拆分。

## 分模块实施

1. **计划与行为基线**：9fac9d8、3e95364。在产品改动前固定 106 条迁移的 SHA-256、基础 schema，以及真实准入 gate 的 256 种长度四操作序列、跨主体租约和真实 HTTP 权限/别名性质。基线 13 项通过。
2. **HTTP 领域**：9a6a6e6。公开/管理读取、认知资源、钱包、迁移、导出、协作、搜索和共同知识分域；保留匹配优先级、鉴权与租约语义。详见 [HTTP 复审](2026-10-26-modularization-http-review.md)。
3. **数据库定义**：d5ba347。分离 SQL/版本、迁移、指纹和哈希工具，原 Database 类保持。详见 [数据库复审](2026-10-26-modularization-database-review.md)。
4. **配置与传输边界**：2521e36。明确包内资源目录和渲染器依赖，保留 service 兼容入口；NoyraService 生命周期完整 AST 不变。详见 [传输复审](2026-10-26-modularization-transport-review.md)。
5. **维护规则和最终交付**：README 双语入口、MAINTAINING.md、CONTRIBUTING.md、架构边界/性质矩阵及本文；最终提交 SHA 以 Git 交付记录为准，避免文档自引用。

## 独立差异检查

- 111 个原 Handler 方法、181 个路由分支展开委派后，可执行 AST 相同。对路由 handled 布尔协议作显式逆变换以保留“继续匹配”；只有一个说明 docstring 的空白作 cleandoc 归一化，SQL/普通字符串值保持。
- 255 个搬移入口的参数位置、名称和关键字转交核对一致。
- Database 的整个类 AST、69 个方法未改；八个工具函数 AST 相同。106 条迁移的实际字符串、插入顺序、基础 schema、版本 107 和指纹完全一致。兼容导出指向新定义的同一对象，旧测试对版本和 MIGRATIONS 的补丁仍生效。
- ServiceSettings 类 AST 与 JSON Schema 相同；会话结构和 token 校验函数 AST 相同；NoyraService 整个类 AST 相同。
- Handler 工厂位置变化通过显式 web_root 与动态兼容渲染器注入处理；真实 wheel 运行验证没有回退到工作树导入。
- 新增直接依赖测试约束领域/配置/传输模块不得在运行时反向导入 service，schema 定义不得依赖 Database 外观或创建 SQLite 连接。该静态规则的覆盖范围不是所有间接依赖的完整证明，入口导入和实际运行提供补充验证。

## 实施期间发现与关闭的问题

HTTP 首轮综合测试出现 5 项失败：拆出分支后提前返回，破坏了健康缓存未命中与部分模型资源前缀的继续匹配行为。问题在未提交实现中发现，修正为明确的 handled 协议，保留原 if/elif 选择优先级。包含全部 5 个失败和搜索/导出的 8 项定向复测通过，后续传输/权限/文章集成也通过。没有通过放宽断言或改变鉴权来消除失败。原 JUnit 保留。

数据库回归有一项子进程误导入旧 editable 工作区，表现为缺少 relationship_identity 模块。将当前验证进程的 PYTHONPATH 明确指向本工作树后，真实进程崩溃/恢复测试通过。原钱包 CLI 的 GBK 解码警告也在与 CI 一致的 UTF-8 环境下重跑确认消除。没有修改产品源码或断言掩盖环境问题。

## 验证结果

以下批次有重叠，不能直接相加：

| 范围 | 结果 |
| --- | --- |
| HTTP 首轮综合 | 104 passed，5 failed，1 skip；45 子测试通过；5 失败已全部定向复测通过 |
| 修正后定向路由/搜索/导出 | 8 passed |
| 运行准入/API 契约/认证边界 | 41 passed |
| 公开错误、访问日志、文章/公开归档 | 76 passed；26 子测试通过 |
| 历史库、R01、钱包、隐私删除 | 123 passed，1 环境失败；该项修正运行环境后通过 |
| Schema 契约 | 10 passed |
| 最终配置/启动/会话 | 10 passed，1 skip；6 子测试通过 |
| 最终传输/认证/文章/排空与超时 | 61 passed；26 子测试通过 |
| 最终跨模块架构性质与依赖 | 16 passed |
| 前端 | 152 passed |
| 全库 Ruff / 格式 | 通过 / 852 文件通过 |
| 严格 Mypy | 534 个源文件通过 |
| Python AST | 555 个文件无语法错误 |
| 实际 wheel | 49 个拆分模块字节核对通过；隔离真实 HTTP 四个接口 200 |

按 JUnit 的 testcase 标识去重，并用同用例较晚启动的验证替换先前结果：本轮共运行 373 个不同 pytest 用例，最终 372 passed、1 skipped、0 未解决失败。子测试不重复计入这一总数。唯一 skip 是 Windows 不适用的 POSIX token 文件权限用例。此计数跨模块阶段及复测，并非声称最终提交执行了整个项目所有测试或 GitHub CI。

最终产品 Python 文件已在上述验证后冻结，仅继续整理 Markdown 和公开清单。最终架构测试及隔离 wheel 同时覆盖配置、HTTP 与数据库的新布局。没有新增对外服务、真实模型/付款调用或生产数据操作。

## 维护能力与后续边界

[架构维护说明](../development/architecture-boundaries.md) 写明请求准入顺序、路由继续匹配语义、模块职责、不变量测试归属及后续改动要求。README 明确公开维护者和普通问题/PR 无固定响应时限，安全报告仍使用 SECURITY.md 原有私密渠道与七天确认约定。

本轮不混修独立审计的 A01–A06，原有未完成的运行环境/供应商/资金/外部门禁结论没有改变。高风险的迁移编排、生命周期/排空算法继续保持原执行结构；以后有具体目标时再按模块验证，不以压行数推动重写。

本轮工作均在独立分支本地提交，未推送、合并或部署，未改写正在检查的 #22。建议把此重构作为单独可审查的集成变更运行 CI，再安排部署验收。回退应按提交依赖逆序进行；没有新增数据库版本需要反向数据迁移。

## 可追溯证据

实施计划见 [roadmap](../roadmap/2026-10-26-service-database-modularization.md)。原始对照、失败与成功 JUnit、静态结果、wheel 及 smoke 输出保存在仓库外本轮目录：D:/metagrowh/.user-temp/noyra-modularization-429xlran。

关键文件：http-extraction-map.json、http-ast-equivalence.json、http-forwarding-equivalence.json、database-equivalence.json、configuration-transport-equivalence.json、final-regression-summary.json、final-static.json、wheel-inventory.json、wheel-runtime-smoke.stdout。

公开源清单按最终 Git index 生成和校验，提交后再次独立核对 Git blob SHA-256，并导出/校验实际源码 ZIP。最终范围为 1,043 个公开文件、1,042 个校验项、202 个排除文件；具体提交与最终校验结果以交付记录为准。
