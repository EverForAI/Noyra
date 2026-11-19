# 管理令牌与浏览器会话的权限边界

以下是当前 HTTP 管理入口的能力矩阵。路径名称中的 `admin` 不表示同一种
权限：具体路由分别要求 `read`、`operator` 或 `export`。Bearer 令牌的
能力包含关系自管理角色拆分时即存在，不能仅按名称把 export 视为低权限
的第三方下载凭据。

| Bearer 配置 | `read` 路由 | `operator` 路由及写入 | `export` 作业/下载 | `/admin/session` 登录 |
| --- | --- | --- | --- | --- |
| `NOYRA_READ_TOKEN` | 允许 | 拒绝 | 拒绝 | 拒绝，401 |
| `NOYRA_OPERATOR_TOKEN` | 允许 | 允许 | 拒绝 | 允许 |
| `NOYRA_EXPORT_TOKEN` | 允许 | 拒绝 | 允许，但另受导出开关/同意限制 | 拒绝，401 |
| `NOYRA_BREAK_GLASS_TOKEN` | 允许 | 允许 | 允许，但另受导出开关/同意限制 | 拒绝，403 |
| 兼容 `NOYRA_ADMIN_TOKEN` | 允许 | 允许 | 允许，但另受导出开关/同意限制 | 允许 |

匿名或错误令牌不能访问上述管理路由；公开站点和公开 API 有独立的可见性
合同。角色令牌必须互异、至少 32 字符，并通过现有启动校验。新部署应将
兼容 admin token 留空。所有令牌都属于受信管理凭据，不得放进公开前端、
查询字符串、普通日志、源码或不受信第三方集成。

## 可读不等于公开

`read` 包含详细运行状态、运行日志、用量、投递及私人对话等管理投影。
它们可以包含私人内容；`read` 不是匿名公开档案的别名，也不是按单个对象
签发的访问许可。读取角色不获得修改模型/连接/付款策略等 operator 能力。
例如 export token 可读取 `/api/v1/state/details` 和对话，但不能读取需要
operator 的 `/api/v1/config/transports`、训练策略配置或迁移策略。

`export` 是受信的批量数据权限：开发者运行档案可以包含主体私人数据，
训练档案另受训练同意配置限制。它同时包含 `read`，并非限定一个作业或
一份文件的下载令牌。向第三方提供它会同时开放这些管理读取和导出能力。
若需要单份文件共享，应由受信操作者先导出并检查内容，再通过独立受控
渠道交付；当前 API 没有按作业签发的分享令牌。

所有能导出的 Bearer（包括 break-glass 和兼容 admin）都不能越过
`NOYRA_DEVELOPER_LOG_EXPORT_ENABLED=false` 或训练导出禁用门禁。
这些开关限制新建导出，不撤销令牌的 `read` 能力，也不自动删除已经生成的
档案；既有档案的读取继续要求 export 权限，并受现有过期/清理规则约束。
运行档案、训练同意和文件下载仍受 at-rest、主体归属、任务状态等既有检查。

## 会话与 Bearer 的区别

浏览器登录仅接受 operator 和兼容 admin token。创建的 HttpOnly 会话只满足
`read` / `operator` 路由；写入另需有效 `X-CSRF-Token`。即使来自 admin
登录，会话 cookie 也不能调用 export 作业、下载或兼容导出入口；导出需要
相应 Bearer。read 和 export token 不用于管理台登录。

break-glass 只接受直接 Bearer 使用，不能交换成浏览器会话；拒绝登录会写入
`admin_break_glass_rejected_session` 审计。它应离线保存，仅按明确故障理由
和最小范围使用。当前通用鉴权不会为每一次 Bearer GET 自动要求理由或生成
专门的 break-glass 审计报告；业务操作按各自实现留痕。持有该 token 不等于
已完成[安全模型](../security-model.md#5-隐私)要求的故障访问记录和事后复核。

## 验证范围

`tests/test_management_role_boundaries.py` 通过真实本机 HTTP 验证两个 API
路径别名、不同角色的读取/写入/导出、会话 CSRF、break-glass 登录拒绝、
导出开关和实际私人档案下载。权限包含关系不代表所有管理 GET 共用一个
权限等级，也不宣称未来新增路由可以跳过各自的角色和数据投影审查。
