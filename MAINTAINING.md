# Maintenance and response expectations

The project maintainer is **Jaxon Grey**, GitHub **EverForAI**. This records the public maintainer identified by the repository; it is not a claim about the number of people who may contribute.

General bug reports, feature requests, and pull requests have no fixed response time. Work is scheduled by user impact, a reproducible explanation, and available maintainer capacity. Progress, requests for more information, and integration decisions are recorded in the relevant issue or PR.

The main development line is the maintenance focus. When reporting a problem, include the exact commit or release, operating system, installation mode, expected behavior, and a small reproduction. A change being committed, its CI passing, its PR being merged, and its deployment on a running instance are distinct steps; reference the relevant commit and deployment when reporting results.

Security reports use the private channel and acknowledgement policy in [SECURITY.md](SECURITY.md), including its existing seven-day acknowledgement expectation. Never open a public issue with vulnerability details or real credentials.

Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md). Architectural refactors follow the [module boundaries and invariant matrix](docs/development/architecture-boundaries.md): preserve observable behavior, review one module at a time, and keep migration history and operational safety boundaries explicit.

## 中文说明

公开维护者为 Jaxon Grey（EverForAI）。普通问题与 PR 没有固定响应时限，按影响、复现情况和维护容量安排，进展在对应 Issue 或 PR 中记录。主线开发版本是维护重点；反馈请附具体提交或发行版本、系统、安装方式和最小复现。

提交完成、CI 通过、合并完成和服务器部署分别记录，不能用其中一步代替其余步骤。安全问题遵循 SECURITY.md 已有的私密渠道和七天确认约定。
