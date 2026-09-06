# Capability Memory Safety and Consistency Hardening

**Date:** 2026-09-06  
**Status:** Approved and implemented

**Product:** Supermind Capability Memory  
**Builds on:** `docs/product/2026-09-04-capability-memory-design.md`

## Purpose

Close the seven independently reproduced safety and consistency defects that remained after the
initial Capability Memory release. The result must preserve the existing provider-neutral user
experience while making every reuse decision, repair attempt, activation, secret boundary, and
authoritative history transition conservative and auditable.

This is a hardening release. It does not add a degraded retrieval mode, make Superpowers a runtime
dependency, or redefine the product's value-first reuse policy.

## User-visible outcome

Before Supermind recommends reuse, it can prove that every mandatory requirement is compatible,
that relationship evidence is successful and current, that the source is available, and that the
answer belongs to a healthy checked generation. If proof is incomplete, Supermind chooses `adapt`
or `build`; it never upgrades uncertainty into `reuse`.

When retrieval fails at runtime, Supermind spends one repair attempt on rebuilding and validating
the index and then reruns one complete hybrid query. Consistency contention cannot consume that
repair opportunity. If the repaired query still fails, the workflow blocks with structured,
redacted diagnostics instead of falling back to a weaker search.

## Design principles

1. **Proof before reuse.** Exact fit means complete clause coverage, not token similarity.
2. **Separate failure budgets.** Consistency restarts and retrieval repair are different state
   transitions with independent bounded budgets.
3. **One policy at every boundary.** Search, Workflow, Explorer, and CLI consume the same typed
   compatibility result.
4. **Sanitize before derivation.** Secrets are removed before hashing, embedding, persistence,
   diagnostics, or event creation.
5. **Authoritative history is not rebuildable cache.** Missing demand history after schema-v2
   activation is corruption, not an additive initialization case.
6. **No degradation.** Semantic, lexical, filtering, evidence, and consistency gates remain one
   complete retrieval contract.

## 1. Contract compatibility

### External compatibility

Existing free-text `contract` and `constraints` remain readable. New producers may optionally emit
typed clauses, but existing records do not require an immediate migration. The compatibility engine
normalizes both forms into the same internal clause representation.

### Internal clause model

Each recognized clause contains:

- subject or capability dimension;
- operator such as equality, inclusion, exclusion, minimum, maximum, or version range;
- normalized value;
- polarity;
- mandatory status;
- source text for safe explanation after redaction.

The parser must retain unconsumed mandatory text as an explicit unresolved clause. Recognizing a
version range does not permit the rest of the sentence to disappear.

### Fit rules

- `1.0` requires every mandatory requirement clause to have a compatible capability clause and no
  contradiction.
- `0 < fit < 1` means at least one mandatory clause is supported, none is contradicted, and one or
  more clauses remain unresolved or require integration work.
- `0` means an explicit contradiction, disjoint version range, excluded value, or mutually
  exclusive assignment.
- Semantic similarity and reuse score only rank candidates inside the result of these gates; they
  cannot change the fit classification.

Examples that must not reuse:

- `must use AES-256` versus `must not use AES-256`;
- `platform Linux` versus `platform Windows`;
- `Python >=3.12,<4 with AES-256 encryption` versus only `Python >=3.12,<4`;
- disjoint version ranges.

## 2. Relationship and metadata compatibility

Relationship expansion remains available for dependencies, alternatives, compositions, and
consumers, but a relationship contributes a candidate only when:

- its referenced evidence exists;
- the evidence outcome is successful and current;
- the related capability passes the same lifecycle, verification, expected-value, source, contract,
  runtime, platform, license, stack, and category gates as a directly retrieved capability.

Compatibility dimensions use conjunction across requested dimensions. A Python/Windows capability
does not satisfy a Python/Linux requirement merely because one token overlaps. Within a dimension,
the requirement's documented alternatives may use set intersection; across dimensions, all
mandatory dimensions must pass.

## 3. Retrieval and repair state machine

The service coordinates two bounded counters:

- **consistency restarts:** restart the complete query when generation authority changes;
- **retrieval repair:** at most one rebuild-and-validate transition after a complete hybrid route
  returns `FAILED`.

The counters are independent. Authority churn before a retrieval failure cannot eliminate the
repair followed by its required retry. After repair, the service validates health and executes one
complete vector + FTS + exact-filter query against the new authority. Further failure returns a
blocked structured result containing all redacted attempts.

There is no vector-only, FTS-only, stale-generation, or empty-library fallback.

## 4. Retrieval evaluation gate

Every candidate generation is evaluated before activation using a versioned offline dataset with:

- positive semantic matches;
- lexical and semantic hard negatives;
- contract contradictions;
- runtime, platform, license, stack, and category filters;
- relationship evidence cases;
- threshold metadata for recall, hard-negative rejection, filter precision, and hybrid ranking.

Evaluation invokes the same semantic provider, candidate-generation tables, FTS route, fusion, and
filter/decision logic used by production hybrid retrieval. Synthetic identifier lookup alone is not
sufficient. Tests may inject a deterministic provider, but production activation uses the configured
pinned provider and fails closed if it cannot run.

The generation manifest records dataset version and digest, threshold values, measured metrics,
provider/model identity, and pass/fail status. A fault, missing threshold, empty dataset, or metric
regression leaves the prior generation active.

## 5. Secret boundary

One sanitizer is applied before any requirement or discovered metadata reaches:

- hashes or content identities derived from text;
- lexical search text;
- embedding input;
- authoritative capability, observation, evidence, relationship, or event rows;
- structured errors, repair attempts, logs, CLI output, or Explorer output.

Coverage includes assignment syntax, quoted JSON/YAML values, authorization headers, private keys,
JWTs, high-entropy tokens, and sensitive URI query or fragment parameters such as `access_token`,
`api_key`, `password`, and `client_secret`. Requirement identifiers and project identifiers are
sanitized when they cross persistence or diagnostic boundaries. The same sanitized value feeds both
storage and derivation so a secret cannot survive indirectly in embedding input or a user-visible
hash.

Redaction is deterministic and idempotent. Safe local filesystem paths and ordinary content hashes
remain intact.

## 6. Authoritative demand history

Schema-v2 initialization distinguishes proven migrations from corruption:

- a proven pre-v2 store may receive both demand tables through the recorded migration;
- a store already marked v2 must contain both requirement observations and requirement events.

The durable metadata marker is `authoritative_schema_version=2`. Proven v1 means all five legacy
tables exist, both demand tables are absent, and the marker is absent. A complete pre-marker v2
store may adopt the marker after its existing history validates. Any partial table set or invalid
marker is corruption. Fresh initialization, v1 migration, and marker adoption all use the same
journaled transition, with the marker written only after both demand tables validate.

Migration journals record complete Arrow snapshots and explicit absent-table slots. Recovery checks
snapshot hashes, row counts, schema state, and demand invariants before restoring any table, and
removes only tables proven absent in the original snapshot. Reopening after a crash restores that
snapshot before ordinary initialization proceeds.

If either authoritative table is missing after v2 activation, health blocks activation and attempts
recovery only from a valid transaction journal or migration snapshot. It never silently recreates
an empty table. Health also verifies that every observation has its creation event and that links to
verified implementations have corresponding events.

The existing serialized names remain `unmet_observed` for creation and `implementation_linked` for
linking. A link event must name the same capability as the observation. Recovery and marker adoption
preserve those rows; they never infer missing events or rewrite historical names.

## 7. Source resolution compatibility

One safe resolver handles both canonical `file:` URIs and legacy absolute local paths. It rejects
relative paths, escaping symlinks where ownership boundaries apply, malformed file URIs, and paths
outside the configured source authority. Search and service availability checks use this same
resolver so they cannot disagree.

Remote sources continue to require affirmative current availability evidence.

## Data and API compatibility

- Existing serialized requirement and capability payloads remain readable.
- Optional typed clauses and evaluation metadata are additive and versioned.
- Existing plain absolute source paths retain support through canonical normalization.
- A schema version increase is required only if persisted shapes change; migrations must remain
  additive, journaled, crash-safe, and fail closed on incomplete authoritative history.
- Error codes remain structured and CLI retrieval/bootstrap failures continue to exit with code 3.

## Verification strategy

Implementation follows strict red-green-refactor cycles. Every reproduced defect receives a focused
test that first fails for the observed reason:

1. mixed version and mandatory-text clauses cannot become exact;
2. consistency churn cannot consume the repair retry;
3. failed or stale relationship evidence cannot expand candidates;
4. cross-dimension partial token overlap cannot pass compatibility;
5. an unusable semantic provider prevents generation activation;
6. manifests expose thresholds and measured hybrid metrics;
7. JSON passwords, OAuth URI tokens, identifiers, and raw queries are sanitized before embeddings
   and persistence;
8. missing v2 demand history blocks or recovers without silent empty recreation;
9. safe legacy absolute paths and canonical file URIs produce the same availability result.

The release gate requires all focused tests, the complete plugin test suite, end-to-end login reuse,
release-tree validation, `git diff --check`, a clean worktree, and an independent whole-branch review.

## Acceptance criteria

- All nine adversarial cases above pass through public service/workflow boundaries.
- No explicit or unresolved mandatory clause is ignored when computing exact fit.
- Exactly one retrieval repair remains available regardless of prior consistency restarts.
- Only successful, current relationship evidence can expand a candidate.
- Generation activation exercises and thresholds the real semantic + hybrid path.
- No supplied synthetic secret appears in embedding input, persistent rows, diagnostics, or output.
- Missing authoritative v2 history cannot produce a healthy report through empty recreation.
- Safe absolute paths remain compatible with `file:` URIs.
- No degraded search path exists.
- Superpowers remains optional.

## Non-goals

- Replacing the local LanceDB/FastEmbed architecture.
- Requiring all existing capabilities to adopt a new contract authoring language immediately.
- General-purpose natural-language theorem proving.
- Network-based evaluation or telemetry.
- Weakening fail-closed behavior to improve apparent availability.
