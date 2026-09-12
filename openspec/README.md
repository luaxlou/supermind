# Supermind 工程管理入口

本仓库于 2026-09-12 通过官方 OpenSpec 1.13.0 执行 `openspec init --tools codex --profile core --language Chinese --no-animation` 初始化。项目配置为 [config.yaml](config.yaml)，Codex 集成为 [官方生成的技能](../.agents/skills/openspec-apply-change/SKILL.md)。

## 现行规格

- [工程执行边界](specs/engineering-execution/spec.md)：承接上游需求与设计、工程执行、缺口交接。
- [工程记录管理](specs/engineering-management/spec.md)：必备 OpenSpec、唯一任务入口、Memory 职责与交付事实。

这里只纳入已明确的定位和工程管理规则，不代表能力库全部运行契约已迁移。详细定位见 [当前决策](../docs/product/supermind-decision-model.md)，实现入口为 [Supermind Skill](../plugins/supermind/skills/supermind/SKILL.md) 和 `capability-memory/`。历史 `docs/product/plans/`、`docs/superpowers/plans/` 与 `docs/archive/` 不自动转为活动任务。

## 变更与验证

运行 `openspec list --json` 查看活动变更；新工作使用官方 `openspec new change <name>` 和 CLI instructions 创建相应产物，按生成技能实施。每个变更的 `tasks.md` 是其唯一进度记录。

本次初始化和现行规则登记已完成，没有据历史计划推造活动变更。使用以下命令核对项目：

```sh
openspec context --json
openspec list --specs
openspec list --json
openspec validate --all --strict --no-interactive
bash scripts/verify.sh --structure-only
```

CLI 能识别项目和校验通过只证明接入与文档结构有效，不代表业务运行验收。本仓库的接入不会自动更新用户安装的插件，也不会初始化其他使用方项目。
