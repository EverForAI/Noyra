# 配置与 HTTP 传输边界复审

在已提交 HTTP 与数据库模块后，分离 service_configuration.py 与 http_transport.py。service.py 保留兼容类和入口、组件装配、服务生命周期及运行协调。

## 等价与边界

- ServiceSettings 的完整 AST、字段/验证器与配置 JSON Schema 均和基线一致，旧 noyra.service.ServiceSettings 导入为同一对象的兼容别名。仅定义所在模块变化。
- token 验证、可信代理主体校验与 _AdminSession 转入 http_routes/auth.py；三个定义完整 AST 一致，service 保留原名称别名。
- 原 Handler 工厂整体移入 http_transport，内层类、鉴权/CSRF、Webhook、请求/响应 IO 和租约顺序不改。原服务工厂显式注入以 service.py 为基准的 web_root，避免 __file__ 位置变化导致页面资源丢失。
- 公开文章渲染器通过具名 Protocol 注入，兼容包装在每次调用时解析原 service.public_article_html，保留测试/集成已有注入位置。原文章错误处理测试已经复跑。
- 展开全部域委派、归一化显式 web_root 后，111 个 Handler 方法/181 个路由分支的可执行 AST 与原版一致。
- NoyraService 整个类 AST 相同，启动锁、调度、线程排空、取消和 kernel.close 顺序未重写。类型导入不形成传输层到 service 的运行时反向依赖；依赖规则已纳入测试。

## 验证

- 配置/from_env、服务启动、登录会话、安全 Cookie、公开站点元数据：10 passed，1 个 Windows 上的 POSIX 权限检查按原声明 skip，6 子测试通过。
- 安全配置、认证输入、HTTP 限额、文章与兼容渲染器、在途请求排空及异步超时：61 passed，26 子测试通过。
- 全库 Ruff 通过；852 文件格式通过；严格 Mypy 534 个源文件通过；555 个 Python 文件 AST 无语法错误。
- 在离线/no-deps/no-build-isolation 条件下构建实际 wheel；49 个拆分模块逐个与 wheel 内字节相等。隔离解包后，新的 Python 子进程只从该 wheel 导入 noyra，真实启动 HTTP 并验证首页、CSS、公开状态和 liveness 全部 200，未从源码工作树回退导入。

本机配置 JSON Schema 对照出现基线已有的 Path 默认值不能 JSON 序列化提示；新旧生成结果相同，没有修改配置默认值来消除提示。该提示不计作产品修复。

仓库外证据包括 configuration-transport-equivalence.json、configuration-transport-service.xml、transport-boundary-final.xml、final-static.json、wheel-inventory.json 与 wheel-runtime-smoke.stdout。

## 结论

此模块可以保留。公共服务入口与生命周期仍在原类，HTTP 协议、配置和领域处理已有明确模块边界。本轮没有扩展管理权限、改变迁移 schema、部署服务器或改写正在检查的 PR。最终全局复审、维护说明及公开清单结果见交付报告。
