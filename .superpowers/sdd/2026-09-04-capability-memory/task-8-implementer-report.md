# Task 8 implementer report

- Baseline: `c5385bd984cca30527b0b9bbfe3db0161ef2f73f`

## Delivered

- Added a pure `CapabilityExplorer` renderer with all five declared views:
  overview, filtered table, capability detail, reuse decision, and Mermaid graph.
- Uses the fixed taxonomy order and a deterministic lifecycle/reuse-score/identifier ordering.
- Detail cards include contract, constraints, source revision, evidence, economics, maturity,
  timestamps, and related capabilities.
- Decision views distinguish failed retrieval from no matches and show candidate score components,
  sources, expected net value, selected action, and rejection reasons.
- Graph rendering covers dependencies, alternatives, compositions, replacements, and consumers.
  Node identifiers are SHA-256-derived and labels are escaped so arbitrary Unicode or fence-like
  input cannot inject Mermaid syntax.
- Markdown table cells escape separators and line breaks. Rendering uses only repository read APIs.

## TDD evidence

- Created `test_explorer.py` before `explorer.py`; its first focused run failed during collection
  with `ModuleNotFoundError: No module named 'supermind_memory.explorer'`.
- The test coverage includes stable category grouping, ordering/filtering, detail fields, missing
  capability IDs, complete and failed searches, all five relationship types, Unicode/special-character
  handling, Mermaid injection resistance, read-only behavior, and repeated byte-identical graph output.

## Verification

- `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_explorer.py -q`
  — 8 passed (run twice; both clean).
- `uv run --project plugins/supermind pytest plugins/supermind/tests -q -m 'not model'`
  — 205 passed, 1 deselected.
- `uv run --project plugins/supermind python -m compileall -q plugins/supermind/src/supermind_memory`
  — passed.
- `git diff --check` — passed.

## Review round 1 fixes

- Added eligibility gating to decisions: only a candidate with no rejection reasons, a persisted
  capability record, and a currently live source can be selected for reuse. A failed/no-match or
  all-ineligible result states a build action and never also emits a reuse action.
- Hardened every Markdown-facing untrusted field into inert, readable text. HTML, Markdown links
  and images, code fences, autolink URLs, and paragraph-leading Markdown syntax are neutralized;
  Unicode remains intact. Mermaid keeps its separately escaped hashed node IDs and labels.
- Changed catalog ordering to fixed taxonomy root first, then lifecycle priority, reuse score, and
  stable capability ID.
- Added durable adversarial tests for all `InspectFilter` dimensions, cross-taxonomy ordering,
  complete no-match, rejected-first/missing/unavailable candidates, Markdown/HTML injection, and
  repeated byte-identical output for every one of the five views.

## Review round 1 verification

- Focused explorer suite: 23 passed twice.
- Full non-model suite: 220 passed, 1 deselected.
- Compile and `git diff --check`: passed.

## Review round 2 fixes

- Restricted reuse selection to persisted `verified` and `recommended` capabilities with no explicit
  rejection reasons. Observed, candidate, degraded, and retired records now produce a build action.
- Removed all source filesystem inspection from the renderer, including `os.open`, path parsing, and
  URI probing. Source availability is represented only through the upstream result/rejection and
  persisted lifecycle state, so rendering cannot block on a FIFO or depend on host filesystem state.
- Decision rejections merge explicit search rejections with structured implicit reasons: missing
  record, degraded/source-unavailable, and non-reusable lifecycle. A build decision never appears
  alongside an empty `Rejections` list.
- Added the complete lifecycle eligibility matrix, missing-record explanation, and a bounded FIFO
  subprocess regression that proves renderer execution does not open source paths.

## Review round 2 verification

- Focused explorer suite: 31 passed twice.
- Full non-model suite: 228 passed, 1 deselected.
- Compile, `git diff --check`, and a renderer source scan for filesystem-probing symbols: passed.

## Review round 3 fix

- Removed the incorrect implication that every degraded capability has an unavailable source.
  Degraded records now report only `lifecycle not reusable: degraded`; `source unavailable` is
  rendered only when the upstream `CandidateMatch` explicitly supplies that rejection reason.

## Review round 3 verification

- Focused explorer suite: 32 passed twice.
- Full non-model suite: 229 passed, 1 deselected.
- Compile and `git diff --check`: passed.
