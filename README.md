# Supermind

Turn product intent into working software.

Supermind is an autonomous product-development orchestrator for Codex. Describe the result you want; Supermind reads the current product and codebase, preserves product continuity, searches its local Capability Memory before rebuilding reusable work, and combines the available skills and tools until the result is complete.

Superpowers is one capability provider Supermind can use. It is neither the product's runtime nor a prerequisite.

## What it does

- Starts a new product with one valuable end-to-end use case.
- Adds features without rebuilding the whole product story.
- Changes behavior while keeping affected features and contracts aligned.
- Fixes defects, improves measurable quality, refactors, releases, and extracts reusable capabilities.
- Selects the appropriate Codex skills and tools for the current need, including Superpowers skills when available.
- Automatically discovers capability candidates across active and registered projects and installed Skills and plugins.
- Requires a business-independent abstraction and positive net benefit before proposing reuse; concrete business implementations are source material, not automatically reusable capabilities.
- Requires explicit human confirmation for every specific reuse or adaptation. Search results and lifecycle recommendations never authorize execution.
- Requires a healthy, complete capability search before designing recurring modules; an unrecoverable search blocks the work instead of silently rebuilding it.
- Repairs a failed vector/keyword/hybrid retrieval once, validates the replacement generation, and retries the complete hybrid search once before blocking with both diagnostics.
- Keeps up to three authority-consistency restarts separate from that single retrieval repair.
- Ranks reuse by contract fit, verification evidence, expected benefit, integration cost, and maintenance risk.
- Reuses exact declared-contract matches, adapts partial matches, and rejects zero-fit candidates
  even when semantic similarity ranks them highly.
- Requires proof for every mandatory clause, including encryption clauses after version ranges;
  related candidates need successful, current evidence and every normal eligibility gate.
- Applies runtime, platform, license, stack, category, lifecycle, current-verification, source-availability, and positive-value gates through one decision engine used by workflow and inspection.
- Sanitizes secrets in text, URIs, and identifiers before hashes, persistence, diagnostics, or
  embeddings; evaluates the real semantic and hybrid routes before activation and records the
  dataset digest, thresholds, measured metrics, and provider identity in each generation manifest.
- Uses one availability policy for safe absolute local paths and canonical `file:` URIs.
- Learns from every reuse outcome and exposes the library as readable category, detail, decision, and Mermaid relationship views.
- Uses brainstorming only when a real product decision remains unresolved.
- Brings you in for every reuse approval and for choices that create meaningfully different product outcomes.

## Use it

You do not choose a workflow. State your intent:

```text
Use Supermind to add repeat ordering to this product.
```

Supermind decides whether to work directly, involve you in a product decision, or call an available capability for brainstorming, planning, debugging, execution, review, verification, or delivery. Those capabilities may come from Codex, Superpowers, another plugin, or a project-specific skill.

Capability Memory is an independent local Python CLI with no resident service. On first use,
connect an existing private GitHub repository or create one. Updates create local commits and
synchronize incrementally with that repository. Immutable events are the authority; the local
LanceDB index, embeddings, models, and runtime remain beneath the resolved Codex data home.
Reusable artifacts remain at their original source locations.

Ask “打开能力库” to synchronize and open the private repository. Its root README is the browser:
read the expanded category → subcategory → capability tree, then follow a capability link for evidence.
Existing entries remain candidates pending abstraction assessment; inclusion is not approval to reuse.
Ask “显示登录能力” or “画出能力关系” for a focused card or Mermaid relationship graph.

The plugin installs its pinned CLI automatically. For direct use after installation:

```bash
supermind-memory init --repo owner/private-memory
supermind-memory status --format json
supermind-memory audit --format json
supermind-memory sync --format json
supermind-memory render --format json
supermind-memory open --format json
```

Manage a category through the same CLI. Preview is read-only and returns exact members, references,
and an event digest. Repeat `--category` from the root down to the desired subgroup:

```bash
supermind-memory remove --category "tools" --category "Codex Skills" --format json
```

After human confirmation, repeat with `--confirm --expected-digest <preview-digest>`.
Add `--exclude-future` to persist an automatic-discovery exclusion. Removal writes recoverable
tombstone events and synchronizes indexes and README; it never uninstalls Skills or deletes sources.
Changed authority requires a new preview; references block removal without cascading deletion.

Use `describe --input <json> --format json` to edit capability names and summaries. The document
contains a `capabilities` array of objects with `id`, `name`, and `summary`. This updates display
text only, preserving source contracts and verification claims, and synchronizes the generated pages.
The library homepage uses Chinese labels, a project introduction and populated category trees;
internal hashes and relationship diagrams remain outside the homepage.
Directory labels retain their English identifiers alongside Chinese names. Capability names show
their stable IDs in both the homepage tree and detail cards; translation never changes identifiers.
The current homepage and category pages use tables under their category descriptions and abstraction
groups. Each subgroup has four columns: 名称、英文标识、状态、说明.

### 能力库建设与项目自举

能力库建设由 Supermind 项目的 Python 运行时承载，随插件锁定版本一同分发，
不是仅靠修改 Skill 提示词。评估、登记及复用判断共用基础质量检查：名称、摘要、
契约、来源、版本和内容标识不能为空，代码能力不能只指向包或插件的清单文件。

`supermind-memory audit --format json` 只读审查当前能力库；可用
`--capability-id <英文标识>` 限定范围。它报告来源可用性、本地源码与记录版本是否一致、
以及验证证据是否缺失，不会自动改写能力、批准抽象或执行复用。
必要性、摘要准确性、通用边界和具体复用仍需人工审查确认。
详见[能力库建设与自举验收](docs/product/capability-memory-bootstrap.md)。

Categories are 代码（code）、产品（product）、设计（design）、工程（engineering）、工具（tools）、
数据（data）. Each category explains what belongs there and when it is reusable. Within a category,
the homepage separates extracted capabilities from source implementations awaiting abstraction.
Abstraction status (`pending`, `in_progress`, `abstracted`, `not_extracting`) is independent of
verification. Existing business implementations default to pending, never automatically extracted.
Use `reorganize --format json` to migrate historical records and discovery exclusions. Use
`set-abstraction --input <json> --format json` with `capability_id`, `status`, `rationale`, and,
for an extracted capability, separate `source_ids` and successful `evidence_ids`. Abstraction must
represent a real implementation with a general contract, not a translation or rename. Old CLI
versions must be upgraded before writing the reorganized library.

Use `init --create-private --name private-memory` to create a private repository in the authenticated
GitHub account. GitHub CLI authentication and Git must be available. Offline updates are explicitly
`pending_sync`; a later `sync` reconciles them. Concurrent changes to the same entity require an
explicit conflict resolution. Missing tools, invalid authority, and incompatible protocols block
the operation without a replacement storage path.

Complete build decisions are stored as unmet requirement observations, never as positive
capabilities. Use `capability-memory list-demands --format json` to inspect them and
`capability-memory link-demand --input <json> --format json` with `observation_id` and
`capability_id` to link a later current verified implementation.

Demand observations and their creation/link events are authoritative records. A fresh device rebuilds
its local search index and generated Markdown from the private repository's event files. To migrate
an existing embedded library, run `supermind-memory migrate --repo owner/private-memory
--legacy-data-home <codex-data-home> --format json`. Migration validates and journals the export;
retain the original embedded store until the migrated library has been verified.

## Install

Add this public marketplace and install Supermind:

```bash
codex plugin marketplace add luaxlou/supermind --ref main
codex plugin add supermind@supermind
```

Start a new Codex task after installation, then invoke `$supermind` or ask Codex to use Supermind.

Superpowers is optional. If installed, its task-method skills become additional capabilities Supermind can route to when their triggers match.

## Project structure

```text
.agents/plugins/marketplace.json   Public marketplace entry
plugins/supermind/                 Codex Plugin
  .codex-plugin/plugin.json        Plugin manifest
  scripts/capability-memory        Self-provisioning local launcher
  scripts/bootstrap.py             Locked standalone-tool provisioning
  skills/supermind/                Supermind Skill
  tool.lock.json                   Wheel hash and protocol compatibility
  vendor/                          Pinned standalone distribution
capability-memory/                 Independent Python CLI package
  src/supermind_memory/             Capability Memory runtime
  tests/                           Unit, integration, and end-to-end evidence
scripts/verify.sh                  Local release checks
```
