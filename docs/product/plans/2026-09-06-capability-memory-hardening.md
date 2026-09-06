# Capability Memory Safety Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all seven independently reproduced Capability Memory safety and consistency defects without adding a degraded search path or breaking existing serialized inputs.

**Architecture:** Extract contract/metadata compatibility, source resolution, and retrieval evaluation into focused modules consumed by the existing Search, Service, Health, Workflow, Explorer, and CLI boundaries. Preserve immutable search snapshots and authoritative LanceDB records while making compatibility proof conservative, retries independently bounded, activation dependent on a real hybrid evaluation, and all text sanitized before derivation.

**Tech Stack:** Python 3.11–3.14, frozen dataclasses, LanceDB, PyArrow, FastEmbed, pytest, filelock.

**Spec:** `docs/product/2026-09-06-capability-memory-hardening-design.md`

## Global Constraints

- Existing free-text `contract` and `constraints` remain readable.
- Exact fit `1.0` requires complete proof for every mandatory clause and no contradiction.
- Consistency restarts and the one retrieval-repair attempt have independent bounded budgets.
- Relationship expansion requires successful, current evidence and every normal candidate gate.
- Candidate generation activation exercises the production semantic + FTS + hybrid route and records dataset version, digest, thresholds, metrics, and provider identity.
- Secrets are sanitized before hashing, embedding, persistence, diagnostics, events, and output.
- A schema-v2 store missing authoritative demand history fails closed unless a valid journal or migration snapshot restores it.
- Canonical `file:` URIs and safe legacy absolute local paths share one resolver.
- There is no vector-only, FTS-only, stale-generation, or empty-library fallback.
- Superpowers remains optional.
- CLI retrieval/bootstrap failures continue to exit with code 3.

---

## File responsibility map

| File | Responsibility after this plan |
|---|---|
| `src/supermind_memory/compatibility.py` | Parse clauses and return typed contract, metadata, and relationship compatibility proofs. |
| `src/supermind_memory/source_resolution.py` | Resolve safe local source references and evaluate local/remote availability consistently. |
| `src/supermind_memory/retrieval_evaluation.py` | Load the versioned evaluation corpus, execute production hybrid retrieval, calculate thresholds, and return manifest data. |
| `src/supermind_memory/search.py` | Orchestrate retrieval/ranking using the three focused policies; it no longer owns their parsing rules. |
| `src/supermind_memory/service.py` | Coordinate independent consistency and repair state transitions and sanitize service boundaries. |
| `src/supermind_memory/redaction.py` | Provide deterministic, idempotent text/URI/identifier sanitization. |
| `src/supermind_memory/repository.py` | Journal schema migration and preserve authoritative requirement history. |
| `src/supermind_memory/health.py` | Validate authoritative schema/history and gate generation activation with retrieval evaluation. |

---

### Task 1: Conservative contract proof and relationship eligibility

**Files:**
- Create: `plugins/supermind/src/supermind_memory/compatibility.py`
- Modify: `plugins/supermind/src/supermind_memory/search.py`
- Modify: `plugins/supermind/src/supermind_memory/decision.py`
- Test: `plugins/supermind/tests/integration/test_search.py`
- Test: `plugins/supermind/tests/integration/test_explorer.py`

**Interfaces:**
- Consumes: `RequirementProfile`, `Capability`, `Relationship`, and successful/current `Evidence` values.
- Produces: `ContractFit(score: float, unresolved: tuple[str, ...], conflicts: tuple[str, ...])`, `contract_fit(capability: Capability, requirement: RequirementProfile) -> ContractFit`, `metadata_compatible(capability: Capability, requirement: RequirementProfile) -> bool`, and `relationship_compatible(relationship: Relationship, evidence: Sequence[Evidence], requirement: RequirementProfile) -> bool`.

- [ ] **Step 1: Add failing mixed-clause contract tests**

Add literal, table-driven cases that call the public `CapabilitySearch.search` boundary:

```python
@pytest.mark.parametrize(
    ("required", "offered", "expected_fit"),
    [
        (
            "requires Python >=3.12,<4 with AES 256 encryption",
            "supports Python >=3.12,<4",
            0.5,
        ),
        ("must use AES 256", "must not use AES 256", 0.0),
        ("platform Linux", "platform Windows", 0.0),
    ],
)
def test_every_mandatory_contract_clause_contributes_to_fit(
    repository, embeddings, required, offered, expected_fit
):
    stored = reusable_capability(contract=offered)
    repository.upsert_capability(stored, embeddings.embed_query(stored.contract))
    result = CapabilitySearch(repository, embeddings).search(
        requirement(contract=required)
    )
    assert result.status is SearchStatus.COMPLETE
    assert result.matches[0].contract_fit == pytest.approx(expected_fit)
```

The fixture must use a real existing local source and current verification evidence so only contract proof controls the assertion.

- [ ] **Step 2: Verify the mixed-clause test is RED**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_search.py::test_every_mandatory_contract_clause_contributes_to_fit -q
```

Expected: FAIL because the equivalent version-range branch returns `1.0` without accounting for the AES clause.

- [ ] **Step 3: Implement clause parsing with explicit consumption**

Create immutable internal types and retain unparsed mandatory text:

```python
@dataclass(frozen=True)
class ContractClause:
    subject: str
    operator: str
    value: str
    polarity: int
    source: str


@dataclass(frozen=True)
class ContractFit:
    score: float
    unresolved: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


def contract_fit(capability: Capability, requirement: RequirementProfile) -> ContractFit:
    required = parse_contract((requirement.contract, *requirement.constraints))
    offered = parse_contract(
        (capability.contract, *capability.constraints, *capability.compatibility)
    )
    conflicts = find_conflicts(required, offered)
    if conflicts:
        return ContractFit(0.0, conflicts=conflicts)
    coverage = tuple(best_coverage(clause, offered) for clause in required)
    unresolved = tuple(
        clause.source for clause, value in zip(required, coverage, strict=True) if value < 1.0
    )
    return ContractFit(sum(coverage) / len(coverage), unresolved=unresolved)
```

Split conjunctions including trailing `with ...`; version parsing returns both the consumed span and the remaining clause. Normalize negation, assignments, and ranges without discarding residual tokens. Clamp only after every required clause contributes.

- [ ] **Step 4: Verify contract tests are GREEN**

Run the command from Step 2 plus existing structured contract cases:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_search.py -q -k 'contract_fit or mandatory_contract_clause or structured_contract'
```

Expected: PASS.

- [ ] **Step 5: Add failing relationship evidence and conjunction tests**

Add public-search cases for all four supported relationship types. Each case uses either failed evidence or cross-dimension incompatibility:

```python
def test_failed_relationship_evidence_never_expands_candidate(repository, embeddings):
    seed, related, relation = relationship_fixture(outcome="failed")
    persist_relationship_fixture(repository, embeddings, seed, related, relation)
    result = CapabilitySearch(repository, embeddings).search(requirement(intent=seed.name))
    assert related.id not in {match.capability_id for match in result.matches}


def test_relationship_compatibility_requires_every_dimension(repository, embeddings):
    seed, related, relation = relationship_fixture(
        compatibility=("python", "windows"), outcome="passed"
    )
    persist_relationship_fixture(repository, embeddings, seed, related, relation)
    wanted = requirement(intent=seed.name, runtime=("python",), platform=("linux",))
    result = CapabilitySearch(repository, embeddings).search(wanted)
    assert related.id not in {match.capability_id for match in result.matches}
```

- [ ] **Step 6: Verify relationship cases are RED**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_search.py -q -k 'failed_relationship_evidence or relationship_compatibility_requires_every_dimension'
```

Expected: FAIL because evidence membership ignores outcome and compatibility uses any-token overlap.

- [ ] **Step 7: Centralize metadata and relationship proof**

Move `_metadata_compatible` and relationship policy to `compatibility.py`. Resolve referenced evidence IDs across both relationship endpoints. Require every ID to resolve to an outcome in `{"passed", "pass", "success", "successful"}`. An item is current only when it is the latest `(observed_at, id)` value for its `(capability_id, evidence_type, source_project)` tuple; a later failed item therefore invalidates an older passing proof. Parse relationship compatibility into dimension-keyed sets; all populated required dimensions must pass. Make `search.py` and `decision.py` consume the typed proof and delete duplicate permissive helpers.

- [ ] **Step 8: Run Task 1 tests and commit**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_search.py plugins/supermind/tests/integration/test_explorer.py -q
git diff --check
git add plugins/supermind/src/supermind_memory/compatibility.py plugins/supermind/src/supermind_memory/search.py plugins/supermind/src/supermind_memory/decision.py plugins/supermind/tests/integration/test_search.py plugins/supermind/tests/integration/test_explorer.py
git commit -m "fix: require complete capability compatibility proof"
```

Expected: focused tests PASS and commit succeeds.

---

### Task 2: Independent repair budget and unified source resolution

**Files:**
- Create: `plugins/supermind/src/supermind_memory/source_resolution.py`
- Modify: `plugins/supermind/src/supermind_memory/search.py`
- Modify: `plugins/supermind/src/supermind_memory/service.py`
- Modify: `plugins/supermind/src/supermind_memory/decision.py`
- Test: `plugins/supermind/tests/integration/test_service.py`
- Test: `plugins/supermind/tests/integration/test_search.py`

**Interfaces:**
- Consumes: `HealthManager.rebuild_indexes() -> str`, `HealthManager.check() -> HealthReport`, `CapabilitySearch.search(...) -> SearchResult`, source URI/path strings, and source availability evidence.
- Produces: `ResolvedSource(kind: str, path: Path | None, canonical_uri: str)`, `resolve_source(value: str) -> ResolvedSource | None`, `source_available(capability: Capability, evidence: Sequence[Evidence]) -> bool`, and a service search state machine with independent `MAX_CONSISTENCY_RESTARTS = 3` and `MAX_RETRIEVAL_REPAIRS = 1`.

- [ ] **Step 1: Add a failing independent-budget service test**

```python
def test_consistency_restarts_do_not_consume_retrieval_repair_retry(memory_fixture):
    searcher = SequenceSearcher(
        authority_changes=2,
        results=(failed_search("vector route failed"), complete_search()),
    )
    memory = memory_fixture(searcher=searcher)
    result = memory.search(requirement(intent="login"))
    assert result.status is SearchStatus.COMPLETE
    assert searcher.complete_search_calls == 2
    assert memory.health_manager.rebuild_calls == 1
```

Use a deterministic in-memory collaborator whose returned structures match the real `SearchResult` and health token shapes; assert the public service result rather than mock existence.

- [ ] **Step 2: Verify repair-budget test is RED**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_service.py::test_consistency_restarts_do_not_consume_retrieval_repair_retry -q
```

Expected: FAIL with `concurrent_mutation` after rebuild and before the required complete retry.

- [ ] **Step 3: Implement explicit search state transitions**

Replace the shared loop with independent counters:

```python
consistency_restarts = 0
repair_used = False
attempts: list[str] = []
while consistency_restarts <= MAX_CONSISTENCY_RESTARTS:
    token = self.repository.search_health_token(active_generation)
    result = self.search_engine.search(requirement, health_token=token)
    if not self.repository.search_health_token_matches(token):
        consistency_restarts += 1
        attempts.append(f"consistency restart {consistency_restarts}")
        continue
    if result.status is SearchStatus.COMPLETE:
        return result
    if repair_used:
        return failed_after_repair(result, attempts)
    repair_used = True
    active_generation = rebuild_and_validate_once(attempts)
```

After rebuild, always return to the search state; do not increment the consistency counter unless authority validation actually fails. Redact each diagnostic before constructing `CapabilityMemoryBlocked`.

- [ ] **Step 4: Verify retry tests are GREEN**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_service.py -q -k 'retrieval_repair or consistency_restarts'
```

Expected: PASS, including exhaustion diagnostics and one-rebuild limits.

- [ ] **Step 5: Add failing source-equivalence tests**

```python
@pytest.mark.parametrize("as_uri", [False, True])
def test_safe_absolute_path_and_file_uri_share_availability(
    tmp_path, repository, embeddings, as_uri
):
    source = tmp_path / "capability.py"
    source.write_text("def login(): pass", encoding="utf-8")
    reference = source.as_uri() if as_uri else str(source)
    stored = reusable_capability(source_uri=reference)
    persist_reusable(repository, embeddings, stored)
    result = CapabilitySearch(repository, embeddings).search(requirement(intent="login"))
    assert result.matches[0].source_available is True
```

Also add malformed URI, relative path, remote-without-evidence, and local symlink boundary cases.

- [ ] **Step 6: Verify source-equivalence test is RED**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_search.py::test_safe_absolute_path_and_file_uri_share_availability -q
```

Expected: the plain absolute-path case FAILS as unavailable.

- [ ] **Step 7: Implement one resolver and replace both policies**

```python
@dataclass(frozen=True)
class ResolvedSource:
    kind: str
    path: Path | None
    canonical_uri: str


def resolve_source(value: str) -> ResolvedSource | None:
    parsed = urlsplit(value)
    if not parsed.scheme and Path(value).is_absolute():
        path = Path(value)
        return ResolvedSource("local", path, path.as_uri())
    if parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
        path = Path(unquote(parsed.path))
        return ResolvedSource("local", path, path.as_uri())
    if parsed.scheme in {"https", "http", "git", "ssh"}:
        return ResolvedSource("remote", None, value)
    return None
```

Validate local paths with `lstat`/resolved ownership rules already used by discovery, reject relative or malformed values, and require a regular existing file. Route `search.py`, `service.py`, and `decision.py` through the same `source_available` function.

- [ ] **Step 8: Run Task 2 tests and commit**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_service.py plugins/supermind/tests/integration/test_search.py plugins/supermind/tests/integration/test_explorer.py -q
git diff --check
git add plugins/supermind/src/supermind_memory/source_resolution.py plugins/supermind/src/supermind_memory/search.py plugins/supermind/src/supermind_memory/service.py plugins/supermind/src/supermind_memory/decision.py plugins/supermind/tests/integration/test_service.py plugins/supermind/tests/integration/test_search.py
git commit -m "fix: isolate retrieval repair and source resolution"
```

Expected: focused tests PASS and commit succeeds.

---

### Task 3: Real semantic and hybrid generation evaluation

**Files:**
- Create: `plugins/supermind/src/supermind_memory/retrieval_evaluation.py`
- Modify: `plugins/supermind/src/supermind_memory/health.py`
- Modify: `plugins/supermind/evaluation/retrieval-v1.json`
- Test: `plugins/supermind/tests/integration/test_health.py`
- Test: `plugins/supermind/tests/integration/test_search.py`

**Interfaces:**
- Consumes: candidate generation repository, configured `EmbeddingProvider`, `CapabilitySearch`, and the versioned JSON corpus.
- Produces: `EvaluationReport(dataset: str, version: int, digest: str, provider: str, thresholds: Mapping[str, float], metrics: Mapping[str, float], passed: bool)` and `evaluate_generation(repository: CapabilityRepository, embeddings: EmbeddingProvider, dataset_path: Path) -> EvaluationReport`.

- [ ] **Step 1: Add failing semantic-provider and manifest tests**

```python
def test_generation_activation_fails_when_semantic_route_is_unusable(paths, repository):
    provider = QueryFailingEmbeddingProvider(valid_documents=True)
    manager = HealthManager(paths, repository, provider)
    previous = manager.check().active_generation
    with pytest.raises(CapabilityMemoryBlocked, match="retrieval evaluation"):
        manager.rebuild_indexes()
    assert manager.check().active_generation == previous


def test_activation_manifest_records_thresholds_and_hybrid_metrics(healthy_manager):
    generation = healthy_manager.rebuild_indexes()
    manifest = read_manifest(healthy_manager.paths, generation)
    assert manifest["evaluation"]["thresholds"] == {
        "semantic_recall": 1.0,
        "hybrid_recall": 1.0,
        "hard_negative_accuracy": 1.0,
        "filter_accuracy": 1.0,
    }
    assert set(manifest["evaluation"]["metrics"]) == set(
        manifest["evaluation"]["thresholds"]
    )
```

- [ ] **Step 2: Verify evaluation tests are RED**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_health.py -q -k 'semantic_route_is_unusable or thresholds_and_hybrid_metrics'
```

Expected: unusable semantic queries still activate and thresholds are missing from the manifest result.

- [ ] **Step 3: Expand the versioned corpus**

Change `retrieval-v1.json` to provide literal evaluation capabilities and queries:

```json
{
  "id": "retrieval-evaluation-v1",
  "version": 1,
  "thresholds": {
    "semantic_recall": 1.0,
    "hybrid_recall": 1.0,
    "hard_negative_accuracy": 1.0,
    "filter_accuracy": 1.0
  },
  "corpus": [
    {
      "id": "eval-oauth-login",
      "name": "OAuth login",
      "summary": "Reusable OAuth 2.0 authorization-code login",
      "contract": "supports OAuth 2.0 authorization code",
      "runtime": ["python"],
      "platform": ["linux"],
      "license": "Apache-2.0"
    }
  ],
  "queries": [
    {
      "id": "semantic-login",
      "intent": "sign users in with delegated authorization",
      "expected_ids": ["eval-oauth-login"],
      "forbidden_ids": []
    }
  ]
}
```

Retain explicit contract hard negatives and filter cases. Dataset validation rejects missing thresholds, duplicate IDs, empty lists, unknown expected IDs, and non-finite values.

- [ ] **Step 4: Execute the production hybrid route in an isolated evaluation store**

`evaluate_generation` builds a temporary evaluation database using the candidate generation's schema/index configuration and the configured provider. It embeds corpus documents through `embed_documents`, runs `CapabilitySearch.search` for every query through `embed_query` and `repository.hybrid_search`, and separately asserts vector and hybrid recall, forbidden-result rejection, contract contradictions, and exact filters. The temporary store is created under a verified owned evaluation directory and removed on success or failure; it never becomes the active pointer.

Return manifest-ready data:

```python
return EvaluationReport(
    dataset=dataset["id"],
    version=int(dataset["version"]),
    digest=sha256(dataset_path.read_bytes()).hexdigest(),
    provider=embeddings.identity,
    thresholds=validated_thresholds,
    metrics=metrics,
    passed=all(metrics[name] >= validated_thresholds[name] for name in metrics),
)
```

Set the provider identity to `getattr(embeddings, "model_id", None) or f"{type(embeddings).__module__}.{type(embeddings).__qualname__}"`; never serialize constructor arguments or environment values.

- [ ] **Step 5: Integrate the gate before pointer activation**

Replace `HealthManager._evaluate_generation` helper-only checks with the new evaluator. Serialize the full report into `manifest["evaluation"]`. Evaluation exceptions become `retrieval_evaluation_unavailable`; threshold misses become `retrieval_evaluation_regression`. Neither path calls `_activate_generation`.

- [ ] **Step 6: Verify evaluation tests are GREEN and commit**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_health.py plugins/supermind/tests/integration/test_search.py -q
git diff --check
git add plugins/supermind/src/supermind_memory/retrieval_evaluation.py plugins/supermind/src/supermind_memory/health.py plugins/supermind/evaluation/retrieval-v1.json plugins/supermind/tests/integration/test_health.py plugins/supermind/tests/integration/test_search.py
git commit -m "fix: gate generations on real hybrid retrieval"
```

Expected: focused tests PASS, prior pointer remains active for every evaluation fault, and commit succeeds.

---

### Task 4: Complete secret sanitization before derivation

**Files:**
- Modify: `plugins/supermind/src/supermind_memory/redaction.py`
- Modify: `plugins/supermind/src/supermind_memory/search.py`
- Modify: `plugins/supermind/src/supermind_memory/service.py`
- Modify: `plugins/supermind/src/supermind_memory/discovery.py`
- Test: `plugins/supermind/tests/unit/test_redaction.py`
- Test: `plugins/supermind/tests/integration/test_search.py`
- Test: `plugins/supermind/tests/integration/test_service.py`
- Test: `plugins/supermind/tests/integration/test_discovery.py`

**Interfaces:**
- Consumes: arbitrary text/URI/identifier fields from requirements, discovered metadata, capabilities, evidence, relationships, events, and errors.
- Produces: `redact_text(value: str) -> str`, `redact_uri(value: str) -> str`, `redact_identifier(value: str) -> str`, and idempotent typed redactors. Every embedding call receives only redacted text.

- [ ] **Step 1: Add failing literal secret-boundary tests**

```python
@pytest.mark.parametrize(
    "value",
    [
        '{"password":"correct-horse-battery-staple"}',
        "https://example.test/callback?access_token=oauth-secret-1234567890",
        "project:client_secret=identifier-secret-1234567890",
    ],
)
def test_structured_and_uri_secrets_are_redacted_idempotently(value):
    once = redact_text(value)
    twice = redact_text(once)
    assert once == twice
    assert "secret-1234567890" not in once
    assert "correct-horse-battery-staple" not in once
```

Add an embedding spy at the real `CapabilitySearch` boundary and assert none of the supplied secrets occur in any captured query/document text. Also assert stored requirement observations, evidence, events, and diagnostics contain no secret.

- [ ] **Step 2: Verify redaction tests are RED**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_redaction.py plugins/supermind/tests/integration/test_search.py plugins/supermind/tests/integration/test_service.py -q -k 'structured_and_uri_secrets or embedding_receives_only_redacted or requirement_identifiers_are_redacted'
```

Expected: quoted JSON values, OAuth URI values, identifiers, or raw query text remain visible.

- [ ] **Step 3: Implement structured, URI, and identifier redaction**

Order sanitizers from structured to generic so replacements do not expose fragments:

```python
def redact_uri(value: str) -> str:
    parsed = urlsplit(value)
    query = [
        (key, REDACTION if key.casefold() in SENSITIVE_KEYS else redact_text(item))
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    fragment = REDACTION if any(
        key.casefold() in SENSITIVE_KEYS for key, _ in parse_qsl(parsed.fragment)
    ) else redact_text(parsed.fragment)
    return urlunsplit(parsed._replace(query=urlencode(query), fragment=fragment))


def redact_identifier(value: str) -> str:
    return stable_safe_identifier(redact_text(redact_uri(value)))
```

Support single/double-quoted JSON/YAML assignments, bearer/basic tokens, PEM blocks, JWTs, cloud/API token formats, and high-entropy values. Preserve safe absolute paths and ordinary hashes. Typed redactors sanitize every string field before persistence or diagnostic construction.

- [ ] **Step 4: Sanitize before embeddings and hashes**

At the beginning of `CapabilitySearch.search`, create `safe_requirement = redact_requirement(requirement)` and use it for `_requirement_text`, compatibility, candidate reasons, and snapshots. In registration/discovery, redact capabilities before computing content-derived hashes, repository rows, `search_text`, and `embed_documents`. Redact error attempt strings at append time, not only when serializing the final exception.

- [ ] **Step 5: Verify Task 4 tests and commit**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_redaction.py plugins/supermind/tests/integration/test_search.py plugins/supermind/tests/integration/test_service.py plugins/supermind/tests/integration/test_discovery.py -q
git diff --check
git add plugins/supermind/src/supermind_memory/redaction.py plugins/supermind/src/supermind_memory/search.py plugins/supermind/src/supermind_memory/service.py plugins/supermind/src/supermind_memory/discovery.py plugins/supermind/tests/unit/test_redaction.py plugins/supermind/tests/integration/test_search.py plugins/supermind/tests/integration/test_service.py plugins/supermind/tests/integration/test_discovery.py
git commit -m "fix: sanitize capability data before derivation"
```

Expected: all supplied secrets are absent from captured embedding input, storage, diagnostics, and output; focused tests PASS.

---

### Task 5: Preserve authoritative demand history and verify the release

**Files:**
- Modify: `plugins/supermind/src/supermind_memory/schema.py`
- Modify: `plugins/supermind/src/supermind_memory/repository.py`
- Modify: `plugins/supermind/src/supermind_memory/health.py`
- Modify: `plugins/supermind/tests/integration/test_repository.py`
- Modify: `plugins/supermind/tests/integration/test_health.py`
- Modify: `plugins/supermind/tests/e2e/test_login_reuse.py`
- Modify: `plugins/supermind/skills/supermind/SKILL.md`
- Modify: `plugins/supermind/skills/supermind/references/capability-routing.md`
- Modify: `README.md`
- Modify: `docs/product/2026-09-06-capability-memory-hardening-design.md`
- Modify: `scripts/verify.sh`

**Interfaces:**
- Consumes: existing schema metadata, migration journal/snapshot, `requirement_observations`, `requirement_events`, and all Task 1–4 public behavior.
- Produces: an explicit authoritative schema-state marker, crash-safe pre-v2 migration, v2 history validation, and end-to-end proof of the complete hardening contract.

- [ ] **Step 1: Add failing authoritative-history tests**

```python
def test_v2_store_missing_requirement_events_is_corrupt(healthy_manager):
    healthy_manager.repository._database.drop_table("requirement_events")
    report = healthy_manager.check()
    assert report.healthy is False
    assert any("authoritative_store_corrupt" in item for item in report.failures)


def test_proven_v1_store_migrates_both_demand_tables_atomically(v1_store, paths):
    repository = CapabilityRepository.open(v1_store, paths.writer_lock)
    repository.initialize()
    assert {"requirement_observations", "requirement_events"} <= set(
        repository._database.list_tables().tables
    )
    assert repository.get_metadata("authoritative_schema_version") == 2
```

Add crash injection after each table replacement and marker write. Reopening must either restore the old v1 snapshot or expose a complete v2 store; it must never report healthy with one empty recreated history table.

- [ ] **Step 2: Verify history tests are RED**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_repository.py plugins/supermind/tests/integration/test_health.py -q -k 'v2_store_missing_requirement_events or proven_v1_store_migrates_both_demand_tables'
```

Expected: the missing v2 table is silently initialized and reported healthy, or the explicit version marker is absent.

- [ ] **Step 3: Implement proven migration and history invariants**

Write `authoritative_schema_version` only inside the existing journaled migration after both demand tables validate. A legacy store qualifies for v1 migration only when the old required table set is complete, both demand tables are absent, and no v2 marker exists. Any partial demand-table set or v2 marker with a missing table raises `authoritative_store_corrupt` before `_initialize_unlocked`.

Extend `_check_source`:

```python
events_by_observation = group_requirement_events(events)
for observation in observations:
    event_types = {event.event_type for event in events_by_observation[observation.id]}
    if "observed" not in event_types:
        raise ValueError(f"requirement observation {observation.id} has no creation event")
    if observation.status == "linked" and "linked" not in event_types:
        raise ValueError(f"requirement observation {observation.id} has no link event")
```

Recovery may restore a valid migration snapshot or transaction journal; it never fabricates missing authoritative rows.

- [ ] **Step 4: Verify repository/health tests are GREEN**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_repository.py plugins/supermind/tests/integration/test_health.py -q
```

Expected: PASS, including crash recovery and schema v1/v2 cases.

- [ ] **Step 5: Extend the end-to-end scenario**

In `test_login_reuse.py`, add one scenario that:

1. rejects a high-similarity capability missing a mandatory encryption clause;
2. accepts a lower complete compatible capability;
3. survives two authority changes followed by one repaired hybrid failure;
4. confirms a failed relationship cannot enter the result;
5. confirms the active manifest contains semantic/hybrid thresholds and metrics;
6. confirms a synthetic secret is absent from captured embedding input and authoritative rows;
7. confirms the linked unmet-demand observation has both observed and linked events;
8. confirms plain absolute and `file:` sources make the same reuse decision.

Use literal fixtures and public Workflow/Service/CLI boundaries; do not assert source text or mock existence.

- [ ] **Step 6: Update product and routing documentation**

Document the conservative mixed-clause rule, independent retry budgets, successful/current relationship evidence, real hybrid activation gate, pre-derivation redaction, v2 authoritative-history corruption behavior, and shared source resolver. Keep Capability Memory mandatory before design and Superpowers optional.

- [ ] **Step 7: Run the complete verification gate**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests -q
./scripts/verify.sh
git diff --check
git status --short
```

Expected: all plugin tests and E2E tests PASS; Skill/plugin/release-tree validation passes; no whitespace errors; only intended Task 5 files are uncommitted.

- [ ] **Step 8: Commit the integrated hardening release**

```bash
git add plugins/supermind/src/supermind_memory/schema.py plugins/supermind/src/supermind_memory/repository.py plugins/supermind/src/supermind_memory/health.py plugins/supermind/tests/integration/test_repository.py plugins/supermind/tests/integration/test_health.py plugins/supermind/tests/e2e/test_login_reuse.py plugins/supermind/skills/supermind/SKILL.md plugins/supermind/skills/supermind/references/capability-routing.md README.md docs/product/2026-09-06-capability-memory-hardening-design.md scripts/verify.sh
git commit -m "fix: preserve capability demand history"
git status --short
```

Expected: commit succeeds and the worktree is clean.

---

## Final review requirements

After Tasks 1–5 pass their task-level specification and quality reviews:

1. Generate a whole-branch review package from the plan's merge base through HEAD.
2. Dispatch an independent final reviewer on the most capable available model.
3. Require adversarial confirmation of the seven original residual defects, not only suite output.
4. Permit one consolidated final fix wave and one scoped re-review according to SDD.
5. Run `./scripts/verify.sh` again on the reviewed HEAD.
6. Refresh the locally installed `supermind@supermind` only after the review is clean and verify the installed cache byte-for-byte against the worktree release tree.
