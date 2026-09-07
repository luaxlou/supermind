# Supermind 使用指南

[返回项目介绍](../README.md)

## 如何使用

直接描述目标，例如：

> 用 Supermind 给这个产品增加再次下单功能。

Supermind 会判断下一步是直接处理、与你确认产品选择，还是调用需求讨论、规划、调试、执行、审查、验证或发布能力。能力可以来自 Codex、Superpowers、其他插件或项目自身。

## 能力库如何工作

能力库由独立的本地 Python 命令行工具管理，不需要常驻服务。首次使用时，连接已有的 GitHub 私有仓库，或创建一个新的私有仓库。更新会生成本地提交，并向该仓库增量同步。

事件记录是重建能力库的依据；LanceDB 索引、向量、模型和运行环境保留在本地 Codex 数据目录。可复用代码和其他资产仍保留在原始来源中。

说“打开能力库”即可同步并打开私有仓库。根目录 README 就是浏览入口：按大类和子类分组，用表格列出能力，再通过链接查看详情和证据。也可以说“显示登录能力”或“画出能力关系”。

### 分类与状态

顶级分类为代码（`code`）、产品（`product`）、设计（`design`）、工程（`engineering`）、工具（`tools`）和数据（`data`）。每类都有说明，帮助判断什么适合沉淀和复用。

目录及能力保留中文名称和稳定的英文标识。每个子类使用四列表格：名称、英文标识、状态、说明。首页不展示内部哈希或关系图；最近变化使用列表。

能力状态与验证结果分开管理：待抽象（`pending`）、抽象中（`in_progress`）、已抽象（`abstracted`）、不提取（`not_extracting`）。新发现的业务实现默认待抽象，即使它在原项目中已经通过验证。

业务登录流程只是来源材料；完成业务解耦、有清晰配置边界和通用契约的实现，才可能成为可复用能力。不能靠翻译、改名或修改状态假装完成抽象。

### 检索与复用规则

- 设计可能重复使用的模块前，必须完成健康检查和完整检索。检索不可用时明确阻断，不用新建模块或替代检索掩盖故障。
- 向量、关键词或混合检索失败后，最多重建并验证一次索引，再完整重试一次；仍失败则报告诊断。一致性重试另计，最多三次，不能消耗检索修复次数。
- 已声明契约完全匹配时可建议复用，部分匹配时可建议适配，完全不匹配时拒绝；相似度高不能替代契约匹配。
- 每项强制条款都需要证据。版本范围匹配不能证明加密等其他要求；关联能力也必须具备当前有效证据并通过相同检查。
- 技术栈、运行环境、平台、许可证、分类、生命周期、当前验证、来源可用性、抽象完成情况和正向收益共同决定候选资格。
- 未声明契约时，也必须通过其他准入条件。最终复用或适配始终需要对具体能力、版本、用途和改动范围进行人工确认。
- 替换索引必须通过真实语义检索、关键词检索、混合排序、契约、关系和精确过滤评测，并记录数据集、阈值、测量值与提供者标识。
- 敏感信息在哈希、向量化、存储和诊断前脱敏。安全绝对路径与 `file:` 地址使用同一套来源检查。

### 能力库建设与项目自举

建设能力库的规则由 Supermind 项目的 Python 运行时承载，随插件锁定版本一起分发，不是仅靠修改技能提示词。

评估、登记及复用判断共用基础质量检查：名称、摘要、契约、来源、版本和内容标识不能为空；代码能力不能只指向包或插件的清单文件。

`supermind-memory audit --format json` 只读审查当前能力库；加上 `--capability-id <英文标识>` 可限定范围。它报告来源可用性、本地源码与记录版本是否一致、验证证据是否缺失，不会自动改写能力、批准抽象或执行复用。

必要性、摘要准确性、通用边界和具体复用仍需人工审查确认。详见[能力库建设与自举验收](product/capability-memory-bootstrap.md)。

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

正常发布顺序为：修改项目 → 验证 → 发布远端 → 更新本地插件 → 验证安装结果。开发目录安装仅用于开发测试。

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
