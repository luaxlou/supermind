# Engineering Execution Specification

## Purpose

记录 Supermind 在 Codex 平台上的工程执行职责，承接已明确的上游成果。来源为 README.md 与插件 Supermind SKILL.md；这里约束执行行为，不声称存在 ChatGPT 自动同步功能。

## Requirements

### Requirement: Execute confirmed engineering scope
Supermind SHALL 基于用户已明确的需求、设计、现有契约及验收依据推进获授权的实现、调试、验证、交付和运维。

#### Scenario: Implement a confirmed change
- **WHEN** 当前范围的目标和依据足够明确，且用户已授权实施
- **THEN** 执行工程工作，不要求全产品补齐正式需求文档

### Requirement: Return unresolved design decisions upstream
Supermind SHALL 将未定业务规则、系统边界、接口契约或用户体验的事实、问题与影响交回上游，不自行承担需求分析和设计。

#### Scenario: Encounter an upstream gap
- **WHEN** 实施依赖尚未明确的业务或设计决定
- **THEN** 交接具体缺口，暂停依赖部分，并继续独立且已授权的工作
