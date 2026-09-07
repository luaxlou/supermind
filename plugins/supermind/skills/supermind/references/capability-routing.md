# Capability routing

Supermind owns product direction and continuity. Skills and tools provide specialized task methods.
Route from the current need and the capabilities actually available in the environment, rather than
from the size or label of the request.

## Route by need

- Use an applicable brainstorming method when product intent, feature boundaries, or competing
  user-visible outcomes require a decision.
- Use a planning method when the target behavior is clear and coordinated implementation steps must
  be defined.
- Use a systematic debugging method when observed behavior has an unknown cause.
- Use an execution or delegation method when an approved plan is ready and the method fits the
  environment.
- Use review and verification methods before claiming that the requested result works.
- Use an integration or delivery method when completed repository work needs handoff or release.
- Use domain-specific skills and tools whenever their own triggers match the task.

Capabilities may come from Codex, Superpowers, another plugin, or the project itself. Prefer the most
specific applicable capability and follow its instructions once selected.

## Inspect Capability Memory

Use the local Capability Memory launcher's `inspect` command when the user asks to see, explain,
filter, or map reusable capabilities. Return its Markdown directly and preserve source, evidence,
economics, and lifecycle details.

- “查看能力库”, “show the capability library”, or equivalent: `inspect --view overview --format
  markdown`; use `--view table` when the user asks for catalog rows or filters.
- “打开能力库”, “open the capability library”, or equivalent: `open --format json` synchronizes,
  regenerates and validates the expanded, categorized README tree, then opens the private GitHub
  repository. `render --format json` refreshes generated Markdown without opening the browser.
- “清理某分组”: use `remove --category <root> --category <subgroup> --format json` to preview
  exact members and references. Only after human authorization apply with `--confirm
  --expected-digest <preview-digest>`. Add `--exclude-future` to prevent automatic re-import of
  that category. This removes library entries, not installed Skills or source code. A stale preview
  or referenced entry blocks deletion. Preserve history; never force-delete dependent records.
- “修改能力名称或说明”: use `describe --input <json> --format json` with a `capabilities` array
  of `{id, name, summary}` objects. Preserve the original meaning and scope; translation does not
  turn a business-specific implementation into a generic abstraction. The command leaves contracts,
  source identity and verification claims unchanged and synchronizes the generated Markdown.
- “显示登录能力”, “show the login capability”, or equivalent: resolve the capability identifier
  from the latest complete search or table view, then use `inspect --view detail --capability-id
  <id> --format markdown`.
- “画出能力关系”, “map capability relationships”, or equivalent: `inspect --view graph --format
  markdown`, adding category, lifecycle, stack, or capability filters when requested.
- A request to explain a reuse choice: `inspect --view decision --input <decision.json> --format
  markdown`.
- “Show unmet capability needs” or equivalent: `list-demands --format json`. After a verified
  implementation satisfies one, use `link-demand --input <link.json> --format json`, where the
  document contains exactly `observation_id` and `capability_id`.

`inspect` is read-only; `open` and `render` may commit and synchronize generated views.
Never substitute source-file scanning for these views, and never hide a
Capability Memory exit `3` behind a hand-written summary.

Decision views are recommendations only. Assess business-independent abstraction and positive net
value, then obtain explicit human confirmation for every specific reuse or adaptation before acting.
Library membership, verification, or a previous approval never authorizes another use.

Decision views use the same contract gate as `begin-design`: a declared exact contract is a reuse candidate,
a declared partial contract requires adaptation, and a zero-fit candidate is rejected even when its
semantic similarity or reuse score places it first. With no declared contract terms, verified,
positive-value candidates remain eligible under the other gates. Runtime, platform, license, stack,
category, current verification, source availability, and lifecycle are evaluated by that same
decision engine; the Explorer never upgrades a workflow rejection to Reuse.

Every mandatory contract clause must be covered, including text following a recognized version
range. Metadata dimensions are checked together. Related candidates need successful, current
relationship evidence and the same eligibility proof as direct matches. Safe absolute paths and
canonical `file:` URIs share availability checks.

Capability Memory remains mandatory before recurring-module design. Authority changes permit up to
three consistency restarts independently of the one retrieval repair and complete hybrid retry.
Activation evaluates the real semantic and hybrid routes and records measured thresholds in the
generation manifest. Secrets are sanitized before embeddings, identities, history, and diagnostics.

Demand history preserves the serialized event types `unmet_observed` and `implementation_linked`.
Each observation requires its creation event; each linked observation requires a link event naming
the same capability. Missing v2 demand tables or events is `authoritative_store_corrupt`, never an
empty-library result. A valid journal can restore proven history; migration cannot invent it.

## Use Superpowers when applicable

Superpowers is an optional capability provider. When installed, skills such as
`superpowers:brainstorming`, `superpowers:writing-plans`, `superpowers:systematic-debugging`,
`superpowers:executing-plans`, `superpowers:requesting-code-review`, and
`superpowers:verification-before-completion` may satisfy the needs above. Their availability does not
make Superpowers a prerequisite, a fixed workflow, or Supermind's execution runtime.

## Skip unnecessary routing

Work directly when the outcome, affected behavior, and implementation path are already clear and the
change is local. Examples include a precise copy change, an established configuration adjustment, or
a small addition inside an existing feature contract.

Do not use brainstorming merely because work is new, visible, or called a feature. Technical
uncertainty calls for inspection, planning, or debugging; it is not automatically a product decision.

## Return control to Supermind

After every specialized task, reassess the product outcome. Absorb relevant results into product
state, decide whether more work is needed, and route again only when a new trigger appears.
