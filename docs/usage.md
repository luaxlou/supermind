# Supermind 使用指南

Supermind 是 Codex 平台下的工程执行能力，承接已明确的需求与设计，完成实施、调试、验证、交付和运维。需求分析、业务建模、架构与系统设计、UI 与交互设计由用户在 ChatGPT 或其他上游协作中完成。

[返回项目介绍](../README.md)

## 如何使用

> 用 Supermind 按项目已确认的需求和设计实现再次下单功能。

> 用 Supermind 调查现有接口超时，并在既定契约内修复。

> 用 Supermind 评估这次修复形成的工程经验，先不写能力库。

提供当前范围的有效需求、设计约束与验收依据即可，既有文档、明确消息和现行契约都可以作为输入。资料足够时直接推进，不要求额外补写 PRD 或全套设计。Supermind 组织实施步骤和工程工具，按项目约定处理常规技术细节。

发现需要补定业务规则、接口契约、系统边界或用户体验时，Supermind 记录已有事实、具体问题与实施影响，交回用户或 ChatGPT。依赖部分等待新决定，独立的授权工作继续；不代作需求分析或设计，也不声称自动向 ChatGPT 发送了材料。

任务可以只交付工程解释、调查结果或实施计划。纯需求与设计请求不进入工程流程。一般解释、局部修改不要求先初始化能力库。工程能力从真实使用和反馈中积累，用户明确无需验证时不额外安排测试或审查。

## 直接使用 OpenSpec

OpenSpec 是 Supermind 工程管理的必备依赖，使用官方 CLI 及其生成的 Codex skills 承接规格、变更、任务和归档。Memory 不再提供基线管理或迭代方法，不调用 `engineering.baseline-management` 和 `engineering.iteration`，也不重新登记同义能力。

规格与变更管理由官方 OpenSpec CLI 承担。可以直接说：“用 Supermind 通过 OpenSpec 承接这份已确认规格并推进变更。”Supermind 检查工具和现有项目，读取官方生成的 Codex skills 及 CLI instructions，在授权范围内完成产物、实施和归档。

工具安装使用官方包 `@fission-ai/openspec`；本次接入核对版本为 1.13.0，Node.js 要求 >=20.19.0。项目采用其他版本时读取对应帮助与指令，不强制升级。具体行为和旧项目接入边界见 [OpenSpec 接入指引](../plugins/supermind/skills/supermind/references/openspec.md)。

现有项目的唯一进度入口先保留；新变更采用 OpenSpec 原生任务时，不再另建 Supermind 计划。安装工具不会自动迁移其他项目的基线，也不会把尚未实现的目标写为当前能力。官方格式校验、功能验证和发布状态分别记录。

## 能力库如何工作

`begin-design` 保留为现有 CLI 的兼容命令名，Supermind 仅将它用于工程实施中的来源发现和能力检索，不启动设计流程。检索请求中的需求字段描述已明确的工程用途，不代表需求分析能力。

独立的实践步骤与决策原则可以作为“方法”（`method`）保存。分类描述解决的问题，载体描述如何采用：例如调试方法属于 `engineering` 分类、`method` 载体。正文包含触发条件、必要输入、步骤、差异配置、适用边界和完成依据，可由库内共享文档独立承载；模板与可执行技能分别使用 `template` 与 `skill`。

方法的定性收益反馈与实际工时、跨项目验证分开记录，内容推演不等于真实采用。新增类型不会自动提高验证或抽象状态。读写包含 `method` 的能力库需要 Capability Memory 0.3.16 或更新版本；旧类型和已有记录保持原义。

能力库由独立的本地 Python 命令行工具管理，不需要常驻服务。首次使用时，连接已有的 GitHub 私有仓库，或创建一个新的私有仓库。更新会生成本地提交，并向该仓库增量同步。

事件记录是重建能力库的依据；LanceDB 索引、向量、模型和运行环境保留在本地 Codex 数据目录。可复用代码和其他资产仍保留在原始来源中。

说“打开能力库”即可同步并打开私有仓库。根目录 README 就是浏览入口：按大类和子类分组，用表格列出能力，再通过链接查看详情和证据。也可以说“显示登录能力”或“画出能力关系”。

### 场景触发与能力目录

| 当前场景 | 什么时候检索 | 查什么 |
| --- | --- | --- |
| 按既定后端方案接入基础代码 | 自行实现基础代码之前 | 满足既定技术栈和接口的实现基础；方案适用 Go 时评估 Nova Go |
| 建立本地应用运行方式 | 新写进程管理脚本之前 | 满足项目启动、停止、重启需求的能力，包含 Nova CLI |
| 建立单机部署方式 | 新写部署方案或脚本之前 | 单机部署、远端进程和日志能力，包含 Nova CLI |
| 按既定契约实现通用功能 | 自行实现之前 | 满足现行契约的代码与工程实践 |

完整场景见[生命周期与场景](../plugins/supermind/skills/supermind/references/scenarios.md)。已经采用能力后的日常使用沿用现有配置；需求、范围或实际兼容条件发生重大变化时再查询。用户只要求讨论或自行验证时，Supermind 按该范围交付。

独立能力库为历史兼容保留代码、产品、设计、工程、工具和数据分类。Supermind 只新增沉淀与采用工程执行能力，需求和设计方法即使归在工程或工具分类下也不调用。明确的库管理请求可以查看或处理历史记录，定位变化不自动删除私有数据。总览与分类页统一使用“名称、说明”两列；名称采用简洁的“中文名（英文名）”，说明使用较小字号。详情页展示用途、官方入口、触发场景、接入方式和必要边界，不展示状态管理、来源版本或内部评分。

### 检索与复用规则

- 到达具体场景规定的检索时点时，必须完成相关需求检索或使用仍有效的结果。检索不可用时明确阻断，不用新建模块或替代检索掩盖故障。
- 向量、关键词或混合检索失败后，最多重建并验证一次索引，再完整重试一次；仍失败则报告诊断。一致性重试另计，最多三次，不能消耗检索修复次数。
- 已声明契约完全匹配时可建议复用，部分匹配时可建议适配，完全不匹配时拒绝；相似度高不能替代契约匹配。
- 每项强制条款都需要证据。版本范围匹配不能证明加密等其他要求；关联能力也必须具备当前有效证据并通过相同检查。
- 技术栈、运行环境、平台、许可证、分类、生命周期、当前验证、来源可用性、抽象完成情况和正向收益共同决定候选资格。
- 未声明契约时，也必须通过其他准入条件。最终复用或适配始终需要对具体能力、用途和改动范围进行人工确认。
- 替换索引必须通过真实语义检索、关键词检索、混合排序、契约、关系和精确过滤评测，并记录数据集、阈值、测量值与提供者标识。
- 敏感信息在哈希、向量化、存储和诊断前脱敏。安全绝对路径与 `file:` 地址使用同一套来源检查。

### 检查能力库质量

Supermind 提供能力库质量检查，帮助发现信息缺失、来源不可用或源码版本不一致等问题。

评估、登记及复用判断共用基础质量检查：名称、摘要、契约、来源、版本和内容标识不能为空；代码能力不能只指向包或插件的清单文件。

`supermind-memory audit --format json` 只读审查当前能力库；加上 `--capability-id <英文标识>` 可限定范围。它报告来源可用性、本地源码与记录版本是否一致、验证证据是否缺失，不会自动改写能力、批准抽象或执行复用。

必要性、摘要准确性、通用边界和具体复用仍需人工审查确认。

## 常用命令

插件会自动安装锁定版本的命令行工具，也可以直接使用：

```bash
supermind-memory init --repo owner/private-memory
supermind-memory status --format json
supermind-memory audit --format json
supermind-memory sync --format json
supermind-memory render --format json
supermind-memory open --format json
```

使用 `init --create-private --name private-memory` 可在当前 GitHub 账户下创建私有仓库，需要已安装 Git 并完成 GitHub 命令行认证。离线更新明确标记为 `pending_sync`，稍后通过 `sync` 同步；同一条记录的并发修改需要显式解决冲突。缺少工具、数据无效或协议不兼容时阻断操作，不切换到替代存储路径。

### 管理分组

按目录层级重复传入 `--category`，先预览具体成员、引用和事件摘要：

```bash
supermind-memory remove --category "tools" --category "Codex Skills" --format json
```

人工确认后，附加 `--confirm --expected-digest <预览摘要>` 执行。需要排除后续自动发现时，再加 `--exclude-future`。删除会保留可恢复的删除标记，并同步索引和 README；不会卸载技能或删除源代码。记录发生变化时必须重新预览，存在引用时不会级联删除。

### 修改名称、说明与状态

`describe --input <json> --format json` 接收包含 `capabilities` 数组的文档，每项为 `id`、`name`、`summary`。它只更新展示文字，不修改契约、来源或验证声明，并同步生成页面。

`reorganize --format json` 用于迁移历史分类和缺失状态。`set-abstraction --input <json> --format json` 接收 `capability_id`、`status`、`rationale`；标记已抽象时，还需要单独登记实际提取的实现，并提供独立的 `source_ids` 和成功的 `evidence_ids`。旧版本命令行工具必须先升级，才能写入调整后的能力库。

### 未满足的需求与数据迁移

完整检索后决定新建的需求会保存为需求观察，不会冒充有收益的能力。使用 `supermind-memory list-demands --format json` 查看；之后有经过验证的实现时，可用 `supermind-memory link-demand --input <json> --format json`，通过 `observation_id` 和 `capability_id` 建立关联。

需求观察及其创建、关联事件属于权威记录。第二版结构缺少需求表或必要事件时，会报告 `authoritative_store_corrupt`，不会当作空库处理；恢复需要有效日志，不能凭空补造历史。完整的旧版结构可以按迁移规则原子升级。

新设备可以从私有仓库的事件文件重建索引和展示页面。迁移旧的内嵌能力库时使用：

```bash
supermind-memory migrate --repo owner/private-memory --legacy-data-home <Codex数据目录> --format json
```

迁移会验证并记录导出过程；确认新库有效前保留原始库。

## 安装与更新

添加远端插件源并安装：

```bash
codex plugin marketplace add luaxlou/supermind --ref main
codex plugin add supermind@supermind
```

之后更新：

```bash
codex plugin marketplace upgrade supermind
codex plugin add supermind@supermind
```

安装后新建 Codex 任务，通过 `$supermind` 或自然语言调用。Superpowers 可选，安装后可作为额外的能力来源。

源码修改、远端发布与本地插件更新分别按用户授权推进。开发目录安装仅用于明确需要的本地开发。

## 项目结构

```text
.agents/plugins/marketplace.json   远端插件源入口
plugins/supermind/                 Codex 插件
  .codex-plugin/plugin.json        插件清单
  scripts/capability-memory        自动配置运行环境的启动器
  scripts/bootstrap.py             锁定版本的独立工具安装器
  skills/supermind/                Supermind 技能与指引
  tool.lock.json                   安装包哈希及协议兼容约束
  vendor/                          锁定版本的分发包
capability-memory/                 独立 Python 命令行工具
  src/supermind_memory/            能力库运行时
  tests/                          单元、集成和端到端测试
scripts/verify.sh                 本地发布校验
```

## 用户需要时选择验证范围

Supermind 保留 `proportionate-verification` 作为按需能力，不作为默认演进流程。用户需要选择测试范围、修复后复测、审查交接或发布验收时，帮助判断已有证据是否有效、还需哪些检查以及何时可以交付。可以直接要求：“用 Supermind 判断这次改动需要哪些验证，复用仍有效的结果。”

技能保留必要检查，避免相同输入与环境下无理由重跑；结构校验不代表已经验证跨项目提效，实际收益仍需后续使用证据。
