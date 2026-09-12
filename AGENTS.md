# Supermind 项目工程约束

- 工程管理必须使用官方 OpenSpec CLI 和本仓库 `.agents/skills/openspec-*`；入口为 [openspec/README.md](openspec/README.md)。开始相关工作时读取现行 specs、活动 changes 和关联正文。
- 需求分析与设计由上游提供，Supermind 负责工程执行。生成的探索和提案技能不扩大授权或产品职责。
- 新变更使用 OpenSpec 原生产物，进度只维护于对应 `tasks.md`；历史计划仅保留原始依据，不自动迁移未核实的完成状态。
- Memory 不承担基线管理和迭代，不采用 `engineering.baseline-management`、`engineering.iteration` 或另建同义能力。有效需求、UX、UI 正文仍可引用。
- 保留用户及其他任务的未提交修改。规格、实现、验证、Git 提交、发布和安装是不同事实，分别提供依据。
- 文档或工具接入变更运行 `bash scripts/verify.sh --structure-only`，其中包含 OpenSpec 严格校验；运行行为变化按影响执行必要测试。不得为通过目录清单检查排除整个 OpenSpec 目录。
