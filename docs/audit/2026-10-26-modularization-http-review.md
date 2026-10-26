# HTTP 模块化复审

基线：2e9d257；实施分支：codex/modular-service-boundaries。

## 边界

服务入口保留 NoyraHTTPServer、NoyraService、ServiceSettings 和原 Handler 私有方法外观。按域处理放入 noyra.http_routes：公开读取、管理读取、认知资源、钱包、迁移、导出、协作、搜索和共同知识。GET/POST 分发保留原匹配顺序，不采用会重排前缀路由的新注册机制。

HTTP 流量限制、路径/别名规范化和 GET 的协议入口保持；POST 仍先完成登录/Webhook 例外、静态加密边界、修改准入和恢复控制分流，再在原租约内分发。鉴权/CSRF、actor、签名原始 target、请求/响应 IO、文件下载及静态资源路径仍在原文件，避免移动 __file__ 改变 web 资源位置。启动、线程、数据库/模型/付款执行实现均未调整。

域处理使用显式 owner 和 RequestHandler Protocol，不使用运行时反向导入 service、星号转发、动态执行或大型 Mixin。原 service 的纯投影函数和常量保留兼容别名；public_article_html 的原注入/补丁位置不变。

## 差异审计

将新委派调用展开后逐个比较原 Handler 的 111 个方法，181 个路由分支均恢复为相同的可执行 AST。参数转交保留原 self/owner 和查询上下文；SQL 字符串值不变，只有一个说明性 docstring 的缩进按 cleandoc 归一化。代码证明与映射保存在仓库外本轮证据目录。

拆分中曾发现提前 return 会改变“健康缓存未命中”和“模型资源前缀继续匹配”的行为；在提交前改为显式 handled 布尔协议：原 return 对应 True，原继续执行对应 False。原路由 if/elif 的选择结构保留，不能把 elif 的匹配条件与 handled 判断合并。两份分发文件仅对这一结构的 SIM102 风格提示作有说明的局部豁免，未关闭鉴权、业务断言或类型检查。

## 验证与限制

新增基线属性测试在拆分前 13 项通过，覆盖 256 种有界准入序列、跨主体租约、真实匿名/参与者权限和别名响应。首次 HTTP 综合回归 104 passed、5 failed、1 平台 skip、45 子测试通过；5 个失败均来自上述继续匹配语义，保留原 JUnit。修正后包含全部 5 个失败和搜索/导出的 8 项定向回归通过。另有准入、API 契约和认证输入 41 项通过。公开页面/错误/访问日志最终回归 76 passed、26 子测试通过，架构依赖约束通过。HTTP 相关 34 个源文件 Mypy 通过，修改范围 Ruff/格式通过，前端 152 项通过。

没有将历史通过或 Windows 上的 POSIX skip 声称为跨平台完整通过。已知审计 A01–A06 不因本轮重构而关闭，尚无服务器部署或 GitHub 新提交检查结果。
