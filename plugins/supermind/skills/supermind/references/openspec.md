# 直接使用 OpenSpec

OpenSpec 是 Supermind 工程管理的必备依赖，使用官方 CLI 及其生成的 Codex skills 承接规格、变更、任务和归档。Memory 不再提供基线管理或迭代方法，不调用 `engineering.baseline-management` 和 `engineering.iteration`，也不重新登记同义能力。 必备依赖的落地形式是官方工具与技能，不是 Memory 中的能力条目。工具不可用时修复接入或交接具体阻塞，不用旧方法或自制流程冒充 OpenSpec；纯解释和无需变更记录的局部操作不要求初始化项目。

Supermind 使用官方 OpenSpec CLI 承接已明确的规格、组织变更、读取实施任务和同步归档。官方项目：https://github.com/Fission-AI/OpenSpec 。CLI 与模板以实际安装版本的帮助和 instructions 输出为准，不复制 OpenSpec 引擎、内置模板或另建兼容命令。

## 接入与恢复

先读取用户授权、项目约束、现有规格和任务入口，检查 `openspec --version`。工具缺失且用户已授权接入时，从官方 npm 包 `@fission-ai/openspec` 安装；先核对 npm 的 version、engines，记录实际安装版本。失败则报告具体原因，不能把手工创建同名目录称为已经接入。

已有 `openspec/` 时直接沿用，读取配置；用 `openspec list --json` 找活动变更，用 `openspec list --specs` 找现行规格。两者用途不同，不能只列变更就声称已读现行规格。读取相关正文和实际源码；有多个候选变更时根据当前任务选择，确实歧义再问用户。

新项目初始化只在已授权的目标目录进行。先查看 `openspec init --help`，使用官方 CLI 的 Codex 集成（`openspec init --tools codex`）。检查生成文件的差异和已有项目指引，保留有效约束，不顺带初始化其他项目或修改全局工作流配置。Codex 集成生成 `.agents/skills/openspec-*`；执行 propose、apply、sync 或 archive 时读取对应官方 SKILL.md 并按其指令操作。CLI 的 instructions 提供当前 schema 上下文，两者结合使用，不在 Supermind 复制一套官方技能。

## 按官方指令工作

创建明确变更时使用 `openspec new change <name>`；随后用 `openspec status --change <name> --json` 读取依赖与产物状态，按 `openspec instructions <artifact> --change <name> --json` 返回的模板、上下文、规则和依赖生成产物。读取已有依赖正文，不凭记忆伪造 CLI schema。命令不支持时查该版本帮助，禁止猜测参数反复重试。

proposal 与 delta specs 承接用户已确定的目的、范围和行为；design 引用已有 UX/UI、系统设计，并记录范围内必要的工程实现决定。遇到未定业务规则、系统边界、接口契约或体验时，标明缺口交回上游，不借 OpenSpec explore 或 propose 自动扩展 Supermind 的职责。

实施前读取 `openspec instructions apply --change <name> --json` 指定的上下文和任务。按实际完成情况更新任务，保留阻断原因及恢复条件。已有授权持续有效，工具提示确认不要求重复询问已明确的同一动作。

## 单一进度与既有材料

现行 specs 保存已纳入的规格，changes 保存待实施差异；已确认但未完成的目标不能冒充当前实现。规格是行为依据，代码和运行证据证明实现，两者不符时记录差异。

新采用项目以 change 的 tasks.md 维护该变更的实施进度；迭代若存在，只引用变更及共同范围，不复制逐项任务。项目已规定其他唯一任务入口时先沿用：在变更中引用该入口，不生成第二份可变进度。切换到 tasks.md 必须属于明确的迁移范围，旧记录保留历史及新入口；不得默默双写。

需求、UX、UI 保留专业职责和必要正文引用，不要求新增三套独立基线生命周期或总版本号。共享架构和视觉规范继续引用权威位置。既有基线文档仍按有效范围使用，迁移时区分现行规格、未来变更和历史依据，不批量搬入 specs 宣称全部实现。

## 核对、同步与归档

无行为变化的纯工具或文档工作按官方 schema 的 `skip_specs` 规则表达，不编造业务需求来通过校验。使用该版本的 `openspec validate` 核对规格结构和差异；校验通过仅证明文档符合工具规则。功能验证遵守项目约束及用户指定范围，不自动引入 TDD、单元测试、Mock 或全量回归。

归档前核对任务实际完成、所需验证证据及遗留项，并重新读取现行 specs，检查其他变更是否已经修改同一规则。遇到语义冲突先处理，不使用旧差异覆盖新决定。使用官方 sync 指引或 `openspec archive <name>` 同步并归档，随后读取实际 diff 和归档产物。不要使用跳过校验参数掩盖未完成；仅整理历史且未完成时明确保留状态，不声称交付。

归档、Git 提交、远端同步、发布和部署是不同事实。代码证据绑定提交或相关工作区文件摘要；进度勾选、文档校验和归档成功都不能代替运行验收。软件版本沿用项目的版本源和交付约定。

## 与能力库的关系

OpenSpec 是用户选定的直接工程工具，使用它不以私有能力库初始化、评分或登记为前提。项目规格、变更和证据留在项目仓库；能力库继续用于其他可复用工程资产，不存储一份项目进度。旧基线管理与迭代方法已退出正常目录及复用候选，历史材料仅供追溯，不再作为执行入口。
