# Noyra P2-11 修复复核

日期：2026-08-14  
范围：CI、Ubuntu installer 和 Docker 构建后端未完整使用 lockfile。

## 修复内容

`setuptools` 以精确版本加入 runtime/dev lockfile。CI 先安装
`requirements-dev.lock`，Ubuntu installer 和 Docker 先安装 `requirements.lock`，源码
安装统一使用 `--no-deps --no-build-isolation`。构建不再隐式创建隔离环境并从网络抓取
未锁定的 setuptools/build backend。

## 验证

* lockfile 可被 pip 解析；
* `pip_audit -r requirements.lock` 在提交前通过；
* Docker、CI 配置静态审计和全量测试、部署专项审计继续通过。
