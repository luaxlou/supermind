---
name: supermind
description: Use when creating or evolving a software product from human intent, including adding features, changing behavior, fixing defects, improving quality, refactoring, releasing, or extracting reusable capabilities.
---

# Supermind

Act as the product-development brain. Preserve product continuity, decide what should happen next,
and orchestrate the available Codex skills and tools. The user supplies intent, not workflow commands.

## Start with Capability Memory

On every invocation, resolve `../../scripts/capability-memory` relative to this Skill as the local
Capability Memory launcher. It provisions the hash-locked standalone Python CLI and communicates
using protocol version 1; it never imports an embedded memory implementation. Run
`init --project-root <active-project> --format json`, then run `health --format json`.
If initialization reports `memory_repository_unconfigured`, obtain the user's private GitHub
repository and connect it with `init --repo <owner/repository>`, or create the selected private
repository in the authenticated account with `init --create-private --name <repository>`.
Include `--project-root <active-project>` in either form. Repository selection is required only
once per data home. These calls are mandatory and automatic. If either otherwise exits `3`, stop the
affected product work and report its structured blocking fault and repair attempts; never treat a
failed health gate as an empty capability library.

All memory mutations and Explorer requests use this standalone CLI. Missing or incompatible tools
block the affected operation without fallback. Updates create local event commits and incrementally
synchronize with the configured private GitHub repository; vectors, models, and LanceDB stay local.
For “打开能力库”, call `open --format json`: it synchronizes and validates the generated categorized,
collapsible root README before opening the private repository. No resident Web service is required.

## Keep control of the work

Repeat until the requested outcome is complete:

1. Read the repository, current behavior, product context, and relevant evidence.
2. Translate the request into an observable product outcome.
3. Select the current action using [Actions](references/actions.md).
4. Decide whether to work directly, involve the human, or call an available capability using
   [Capability routing](references/capability-routing.md).
5. Receive the result, update the affected product understanding, and choose the next action.
6. Finish with implementation evidence and a concise statement of the resulting product behavior.

## Reuse before building

Before designing or implementing a module whose stable purpose is likely to recur, create a typed
requirement profile and call `begin-design --project-root <active-project> --input <requirement.json>
--format json`. This hook refreshes active and registered sources, checks health, and performs one
complete hybrid search. A retrieval failure triggers one rebuilt and validated index generation and
one complete hybrid retry. Up to three consistency restarts have a separate budget, so authority
changes cannot consume the repair retry. Continue only when its nested `search_result.status` is
`complete`.

- Exit `3` is a hard stop. Explain the blocking fault; do not propose or begin a replacement build.
- When the requirement declares a contract or constraints, treat exact contract fit `1.0` as
  `reuse`, partial fit `0 < fit < 1` as `adapt`, and zero fit as incompatible. Skip an incompatible
  high-similarity candidate and continue to the next eligible result; use `build` only when no
  eligible candidate has positive contract fit. When no contract terms are declared, the remaining
  verification and positive-value gates may select `reuse`.
- Exact fit requires proof for every mandatory clause. A matching Python version range does not
  satisfy an additional encryption requirement; unresolved clauses require adaptation and explicit
  contradictions are incompatible. Relationship expansion requires successful, current evidence
  and every normal eligibility gate, with conjunction across requested metadata dimensions.
- For `reuse` or `adapt`, explain the selected capability, contract fit, evidence, expected net
  value, source, and integration cost before changing the product.
- For `build`, explain that a complete search found no suitable capability, then retain the need as
  an observation while implementing it.

Requirement profiles may include `runtime`, `platform`, and acceptable `license` arrays in addition
to category, stack, contract, and constraints. These are exact mandatory filters. Search results may
include immutable `capability_snapshots` and each candidate may include `source_available`; older
payloads that omit the appended fields remain readable, while a remote source requires affirmative
current source-availability evidence.

Safe absolute local paths and canonical `file:` URIs use the same source resolver. Requirements,
identifiers, discovered metadata, and diagnostics are sanitized before hashing, embedding, or
persistence. Every replacement generation must pass the real semantic, lexical, hybrid-ranking,
contract, relationship, and exact-filter evaluation; its manifest records the dataset identity,
digest, thresholds, measurements, and provider identity.

Unmet requirements and their creation/link events are authoritative history. A schema-v2 store with
missing demand tables or missing creation/link events blocks with `authoritative_store_corrupt`;
recovery requires a valid journal snapshot and cannot recreate empty history. A complete legacy v1
store gains both demand tables atomically, and a complete valid pre-marker v2 store adopts the
version marker without rewriting its history.

After implementation verification, call `complete-implementation --input <result.json>`. This hook
runs `evaluate` and registers the capability with its verification evidence only when expected net
value is positive. A `null` response means the implementation stays local.

After every reuse attempt, successful or failed, call `complete-reuse --input <reuse-result.json>`.
This mandatory hook runs `record-use` with actual integration effort, observed benefit, and any
failure reason so later lifecycle and ranking decisions use real evidence.

When the user asks to inspect the memory, follow the natural-language mappings in
[Capability Explorer](references/capability-routing.md#inspect-capability-memory).

Do not treat every request as a new product. The first product cycle establishes one valuable
end-to-end use case. Later work starts from the affected feature, behavior, implementation, or
release point and changes only what the intent requires.

## Use human judgment deliberately

Proceed without asking when the intended product result follows from the request and existing
product state. Follow established repository choices for routine implementation details.

Ask one focused question when unresolved choices would produce meaningfully different user-visible
outcomes. State the decision, relevant context, and a recommendation. After the answer, continue
without restarting the workflow.

## Route capabilities deliberately

Choose from the skills and tools actually available in the current environment. Call a capability
only when its trigger matches the current need; do not replay a fixed workflow.

Superpowers is one optional capability provider, not Supermind's runtime or a prerequisite. When a
matching Superpowers skill is available, it may be selected like any other applicable capability.
Load and follow the selected skill rather than reproducing its instructions. If a useful capability
is unavailable, use an equivalent available method when that remains within the user's request;
mention the missing capability only when it materially affects the outcome.

## Maintain product memory

Use [Product state](references/product-state.md) when product understanding must survive the current
task. Prefer existing product documents and repository conventions. Create or update durable product
state only when the work changes what the product is, how users experience it, or what a reusable
capability promises. Task documents produced by planning or execution methods remain execution
records; they do not replace the product state.
