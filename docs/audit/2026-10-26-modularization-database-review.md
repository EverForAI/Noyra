# 数据库定义模块化复审

基线：2e9d257；本模块在 HTTP 模块 9a6a6e6 之后实施。

## 范围与不变边界

core/schema/base.py 保存基础 schema 与版本，contracts.py 保存指纹、持久特性对象和规范化工具，evidence.py 保存证据哈希函数，九个历史迁移片段由 migrations.py 按原插入顺序汇总。core/database.py 保留 Database 类及历史导出别名，现有 imports、字典补丁和版本补丁仍作用在原兼容入口。

没有增加 schema 版本、调整事务、移动 BEGIN/COMMIT、改变只读/WAL 或修复高风险迁移/钱包算法。Database 类的 69 个方法和全部类 AST 与基线相同；八个工具函数的 AST 相同。106 条历史迁移的每个 SQL/表达式求值字节、字典键插入顺序、基础 SQL、版本 107 及全部指纹均一致。历史上 v56/v55、v107/v106 的定义插入顺序也保留，未顺便排序。

原文件的 E501 豁免仅继承到含超长历史 SQL 的两个片段，原因是保持已审阅 SQL 字节；其余新增文件正常校验。没有通过改写迁移 SQL、重算业务指纹或改变异常预期让测试通过。

## 验证

- 独立 AST/实际定义对照：Database、八个工具函数、106 个迁移、插入顺序及指纹全部一致；兼容别名指向新定义的同一对象。
- 历史 schema 71、R01 旧库/隐私、wallet schema 59/60 和 private erasure 综合回归：123 passed，一项子进程导入错误。该错误来自本机 venv 的 editable 安装仍指向旧工作区，子进程没有继承 pytest 的 pythonpath。设置仅当前验证进程的 PYTHONPATH 指向本工作树后，原进程崩溃恢复用例通过；未修改产品或测试断言。
- 原钱包 CLI 子进程解码有 GBK 警告；按现有 CI 的 UTF-8 环境重新运行该用例，通过且无该警告。
- Schema 结构/触发器/标记/严格准入/规范化契约 10 项通过；新增依赖和 SQL 冻结属性 3 项通过。
- 本模块 15 个源文件 Mypy、Ruff、格式通过。

原始结果在本轮仓库外证据目录，包括 database-equivalence.json、database-regression.xml、database-crash-corrected.xml、database-cli-corrected.xml 和 database-schema-contract.xml。复跑只处理实际发现的环境问题，不将首次失败或告警删除，也不重复累加通过数。

## 结论

此模块达到“定义分离，执行不变”的目标，可以保留。连接、迁移备份/恢复及可选特性修复仍集中在 Database，是后续有具体需求时可继续拆分的边界；本轮不以行数目标重写这些高风险流程。没有新的生产数据操作、服务器部署或外部门禁结论。
