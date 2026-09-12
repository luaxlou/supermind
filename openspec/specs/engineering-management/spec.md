# Engineering Management Specification

## Purpose

记录 Supermind 项目采用官方 OpenSpec 的工程管理约束，来源为用户确认决定及插件 references/openspec.md。规格描述规则；实际接入由项目配置、官方生成技能和 CLI 结果证明。

## Requirements

### Requirement: Use official OpenSpec integration
The project SHALL 使用官方 OpenSpec CLI、项目配置及生成的 Codex skills 管理规格、变更、任务与归档。

#### Scenario: Resume engineering management
- **WHEN** 需要恢复规格变更或实施工作
- **THEN** 读取项目配置、现行 specs、活动 changes 和官方 instructions

#### Scenario: Required tool is unavailable
- **WHEN** OpenSpec 工具不可用
- **THEN** 修复接入或交接具体阻塞，不用旧方法或自制兼容流程冒充接入

### Requirement: Maintain one progress record per change
The project SHALL 在相应 OpenSpec 变更的 tasks.md 中维护唯一进度，并引用有效上游正文，不建立三套基线生命周期或第二份迭代账本。

#### Scenario: Continue an existing change
- **WHEN** 恢复既有变更
- **THEN** 根据实际证据更新该变更 tasks.md，历史计划保持历史用途

### Requirement: Exclude superseded Memory methods
Supermind SHALL 不再调用或重新登记基线管理与迭代能力，包括 engineering.baseline-management 和 engineering.iteration；历史与有效上游正文继续保留。

#### Scenario: Select an engineering management tool
- **WHEN** 需要管理规格或组织实施
- **THEN** 直接使用 OpenSpec，不把 Memory 检索、评分或能力登记作为前置条件

### Requirement: Distinguish delivery evidence
The project SHALL 分别记录文档校验、实现、运行验证、提交、发布和安装事实。

#### Scenario: Validate project specifications
- **WHEN** OpenSpec validate 通过
- **THEN** 仅声明规格结构通过，不据此宣称运行验证或插件发布完成
