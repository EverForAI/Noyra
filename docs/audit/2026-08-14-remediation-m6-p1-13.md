# Noyra P1-13 修复复核

日期：2026-08-14  
范围：容器默认监听 `0.0.0.0` 并允许明文非 loopback。

## 修复内容

Dockerfile 默认值改为 `NOYRA_HOST=127.0.0.1` 和
`NOYRA_ALLOW_INSECURE_NON_LOOPBACK=false`。单独运行镜像时，服务不会因为镜像环境而
默认暴露明文管理面；显式设置非 loopback 仍会被 `ServiceSettings` 拒绝，除非部署者
明确打开兼容开关。

Compose 是受控例外：容器内监听 `0.0.0.0` 仅为容器网络可达，宿主端口仍强制绑定
`127.0.0.1`，远程访问要求额外 TLS 反向代理。

## 验证

* 部署专项检查确认 Compose loopback 发布和只读根文件系统仍在；
* 服务设置已有非 loopback fail-closed 测试；
* 全量测试、静态检查、依赖审计和部署专项审计在提交前执行。
