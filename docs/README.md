# Noyra 设计协议

这些文档是 Noyra 运行时的规范来源。实现代码不能通过“模型提示词里写一句话”替代协议。

## 文档顺序

1. [人工主体宪章](charter.md)
2. [状态模型](state-model.md)
3. [生命周期协议](lifecycle.md)
4. [交互协议](interaction.md)
5. [创世体验协议](genesis-experience.md)
6. [评测协议](evaluation.md)
7. [安全与隐私模型](security-model.md)
8. [架构决策记录](adr/0001-runtime-ownership.md)
9. [完整规划书](NOYRA_PROJECT_PLAN.md)

## 规范约定

- `MUST`：实现必须满足，否则不能进入下一阶段。
- `SHOULD`：默认满足，除非记录架构决策说明例外。
- `MAY`：可选能力。
- “模型提案”不是已提交状态；只有通过 Supervisor 验证和事务提交后才生效。
- 任何协议变更都必须提交 ADR、更新测试和创建 Git 记录。
