# Capability Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-initializing, evidence-backed capability memory that Supermind must search before designing reusable modules and can print as a human-readable catalog.

**Architecture:** A daemonless Python package embedded in the Supermind plugin owns domain rules, LanceDB persistence, FastEmbed inference, hybrid retrieval, lifecycle evaluation, repair, discovery, and Markdown/Mermaid rendering. A thin CLI and bootstrap script expose a storage-independent service to the Supermind skill; structured records remain authoritative and embeddings and search indexes are rebuildable generations.

**Tech Stack:** Python 3.11–3.14, LanceDB 0.38.0, FastEmbed 0.8.0, FileLock 3.32.5, pytest 9.1.1, Bash, Markdown, Mermaid

**Spec:** `docs/product/2026-09-04-capability-memory-design.md`

## Global Constraints

- Use `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, 384 dimensions, with pinned library and model revisions.
- Store data beneath the resolved Codex data home in a Supermind-owned directory; never repurpose `HOME` or `CODEX_HOME`.
- Run locally in-process without a permanent database service and without uploading indexed content.
- Do not traverse the entire user disk; index only the active project, previously registered sources, installed Skills/plugins, and explicitly added sources.
- A search returns either a complete result or a distinct failure. Never represent infrastructure failure as an empty match list.
- Repair missing or invalid runtime, model, schema, and indexes automatically. If repair fails, stop the affected work; there is no reduced-quality retrieval mode.
- A recommendation requires contract compatibility, live source, verification evidence, and positive expected net value.
- Promote a capability to `recommended` only after an independent successful reuse.
- Writing index records is automatic. Moving code outside the active project, installing global Skills/plugins, publishing, and uploading remain approval-gated.
- Follow test-driven development for every behavior change and commit after every task passes its focused tests.

## File Map

```text
plugins/supermind/
|-- pyproject.toml                         Package metadata and direct dependency pins
|-- uv.lock                                Complete reproducible Python resolution
|-- requirements.lock                      Runtime installation lock exported from uv
|-- model.lock.json                        Pinned embedding model identity and revision
|-- scripts/capability-memory              Self-provisioning launcher
|-- src/supermind_memory/
|   |-- __init__.py                        Public package surface and version
|   |-- types.py                           Domain enums, records, results, and blocked error
|   |-- config.py                          Codex-home and runtime path resolution
|   |-- taxonomy.py                        Stable categories and path validation
|   |-- scoring.py                         Net-value and reuse-score calculations
|   |-- lifecycle.py                       Evidence-driven state transitions
|   |-- schema.py                          Arrow/LanceDB schemas and schema version
|   |-- repository.py                      Structured persistence and event transactions
|   |-- embeddings.py                      Embedding protocol and pinned FastEmbed provider
|   |-- search.py                          Hybrid retrieval, filters, RRF, and value reranking
|   |-- health.py                          Health invariants, repair, generation activation, locks
|   |-- discovery.py                       Installed capability and active-project discovery
|   |-- service.py                         Storage-independent Capability Memory operations
|   |-- workflow.py                        Mandatory Supermind design and completion hooks
|   |-- explorer.py                        Markdown tables/cards and Mermaid rendering
|   `-- cli.py                             Typed JSON/Markdown command-line boundary
`-- tests/
    |-- conftest.py                        Isolated data-home and deterministic embeddings
    |-- unit/test_types_config.py          Domain serialization and path resolution
    |-- unit/test_taxonomy_scoring.py      Categories, value, ranking, and lifecycle
    |-- integration/test_repository.py     LanceDB records, evidence, relations, events
    |-- integration/test_search.py         Hybrid multilingual retrieval and filtering
    |-- integration/test_health.py         Bootstrap, migration, locking, rebuild, blocking
    |-- integration/test_discovery.py      Project, Skill, and plugin discovery boundaries
    |-- integration/test_service.py        Discover/search/evaluate/register/reuse orchestration
    |-- integration/test_explorer.py       Catalog, detail, decision, and Mermaid views
    |-- integration/test_cli.py            CLI result and exit-code contract
    `-- e2e/test_login_reuse.py             Empty-store-to-cross-project acceptance scenario
```

Final-review hardening appends `decision.py`, `redaction.py`, the versioned
`evaluation/retrieval-v1.json` dataset, and authoritative requirement-observation/event tables
(schema version 2). `RequirementProfile` appends runtime/platform/license filters;
`CandidateMatch` appends optional current source availability; `SearchResult` appends optional
generation-bound capability snapshots. JSON readers accept older payloads with these fields absent,
and writers omit empty or unknown appended values where needed to preserve the original wire form.

Existing files modified during integration:

```text
README.md
plugins/supermind/.codex-plugin/plugin.json
plugins/supermind/skills/supermind/SKILL.md
plugins/supermind/skills/supermind/references/capability-routing.md
scripts/verify.sh
```

---

### Task 1: Package foundation and domain contract

**Files:**
- Create: `plugins/supermind/pyproject.toml`
- Create: `plugins/supermind/uv.lock`
- Create: `plugins/supermind/requirements.lock`
- Create: `plugins/supermind/model.lock.json`
- Create: `plugins/supermind/src/supermind_memory/__init__.py`
- Create: `plugins/supermind/src/supermind_memory/types.py`
- Create: `plugins/supermind/src/supermind_memory/config.py`
- Create: `plugins/supermind/tests/conftest.py`
- Create: `plugins/supermind/tests/unit/test_types_config.py`

**Interfaces:**
- Produces: `resolve_codex_home(env: Mapping[str, str] | None = None) -> Path`
- Produces: `MemoryPaths.from_codex_home(codex_home: Path) -> MemoryPaths`
- Produces: enums `Lifecycle`, `ArtifactType`, `SearchStatus`, and `ViewType`
- Produces: immutable dataclasses `RequirementProfile`, `Capability`, `Evidence`, `Relationship`, `Event`, `CandidateMatch`, `SearchResult`, `HealthReport`, `ValueInputs`, `ReuseScoreInputs`, `InspectFilter`, `DiscoveryContext`, `DiscoveryResult`, `EvaluationResult`, `ReuseResult`, and `ReuseDecision`
- Produces: `CapabilityMemoryBlocked(code: str, message: str, attempts: tuple[str, ...])`

- [ ] **Step 1: Write failing domain and path tests**

```python
def test_resolve_codex_home_prefers_existing_codex_home(tmp_path):
    assert resolve_codex_home({"CODEX_HOME": str(tmp_path)}) == tmp_path.resolve()

def test_search_failure_cannot_contain_matches():
    with pytest.raises(ValueError, match="failed search cannot contain matches"):
        SearchResult(status=SearchStatus.FAILED, matches=(sample_match(),), error_code="index_unhealthy")

def test_complete_empty_search_is_a_no_match():
    result = SearchResult(status=SearchStatus.COMPLETE, matches=())
    assert result.is_no_match is True
```

- [ ] **Step 2: Run the focused tests and verify the expected import failure**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_types_config.py -q`

Expected: FAIL because `supermind_memory.types` and `supermind_memory.config` do not exist.

- [ ] **Step 3: Add the package manifest and minimal domain implementation**

Use these direct dependencies in `pyproject.toml`:

```toml
[project]
name = "supermind-capability-memory"
version = "0.1.0"
requires-python = ">=3.11,<3.15"
dependencies = [
  "fastembed==0.8.0",
  "filelock==3.32.5",
  "huggingface-hub==1.30.0",
  "lancedb==0.38.0",
]

[dependency-groups]
dev = ["pytest==9.1.1"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
markers = ["model: downloads and exercises the pinned embedding model"]
```

Implement `SearchResult.__post_init__` so `FAILED` requires an error code and forbids matches, while
`COMPLETE` forbids error fields. Resolve the Codex home from the supplied mapping first and otherwise
from `Path.home() / ".codex"`. `MemoryPaths` must expose `root`, `database`, `runtime`, `model_cache`,
`locks`, and `generations` beneath `<codex-home>/supermind/capability-memory`.

Create `model.lock.json` with model
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, Hugging Face revision
`e8f8c211226b894fcb81acc59f3b34ba3efd5f42`, dimension `384`, and license `apache-2.0`.

Use these exact immutable domain shapes; timestamps are UTC ISO-8601 strings and tuple fields are
serialized as canonical JSON at the repository boundary:

```python
class Lifecycle(str, Enum):
    OBSERVED = "observed"
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    RECOMMENDED = "recommended"
    DEGRADED = "degraded"
    RETIRED = "retired"

class ArtifactType(str, Enum):
    CODE = "code"
    TEMPLATE = "template"
    SKILL = "skill"
    PLUGIN = "plugin"
    TOOL = "tool"
    API = "api"
    DATASET = "dataset"
    SERVICE = "service"

class SearchStatus(str, Enum):
    COMPLETE = "complete"
    FAILED = "failed"

class ViewType(str, Enum):
    OVERVIEW = "overview"
    TABLE = "table"
    DETAIL = "detail"
    DECISION = "decision"
    GRAPH = "graph"

@dataclass(frozen=True)
class RequirementProfile:
    id: str
    project_id: str
    intent: str
    contract: str = ""
    category_hint: tuple[str, ...] = ()
    stack: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    quality_requirements: tuple[str, ...] = ()

@dataclass(frozen=True)
class Capability:
    id: str
    name: str
    summary: str
    category_path: tuple[str, ...]
    facets: tuple[str, ...]
    contract: str
    constraints: tuple[str, ...]
    artifact_type: ArtifactType
    source_uri: str
    source_revision: str
    content_hash: str
    owner: str
    license: str
    stack: tuple[str, ...]
    runtime: tuple[str, ...]
    platform: tuple[str, ...]
    dependencies: tuple[str, ...]
    compatibility: tuple[str, ...]
    lifecycle: Lifecycle
    confidence: float
    expected_net_value: float
    embedding_generation: str
    created_at: str
    updated_at: str
    last_verified_at: str | None

@dataclass(frozen=True)
class Evidence:
    id: str
    capability_id: str
    source_project: str
    evidence_type: str
    outcome: str
    metric_name: str | None
    metric_value: float | None
    confidence: float
    observed_at: str
    supporting_uri: str | None
    integration_effort: float = 0.0
    benefit: float = 0.0
    failure_risk: float = 0.0

@dataclass(frozen=True)
class Relationship:
    id: str
    source_id: str
    target_id: str
    relationship_type: str
    compatibility: tuple[str, ...]
    evidence_ids: tuple[str, ...]

@dataclass(frozen=True)
class Event:
    id: str
    capability_id: str
    event_type: str
    source_context: str
    occurred_at: str
    previous_state: Lifecycle | None
    resulting_state: Lifecycle
    reason: str

@dataclass(frozen=True)
class CandidateMatch:
    capability_id: str
    vector_score: float
    lexical_score: float
    contract_fit: float
    requirement_fit: float
    reliability: float
    historical_benefit: float
    integration_cost: float
    maintenance_risk: float
    reuse_score: float
    rejection_reasons: tuple[str, ...] = ()

@dataclass(frozen=True)
class SearchResult:
    status: SearchStatus
    matches: tuple[CandidateMatch, ...]
    generation: str | None = None
    error_code: str | None = None
    error_message: str | None = None

@dataclass(frozen=True)
class HealthReport:
    healthy: bool
    active_generation: str | None
    repairs: tuple[str, ...]
    checked_at: str
    failures: tuple[str, ...] = ()

@dataclass(frozen=True)
class ValueInputs:
    expected_reuse_count: float
    benefit_per_reuse: float
    extraction_cost: float
    integration_cost: float
    verification_cost: float
    maintenance_cost: float
    failure_risk: float

@dataclass(frozen=True)
class ReuseScoreInputs:
    contract_fit: float
    requirement_fit: float
    reliability: float
    historical_benefit: float
    integration_cost: float
    maintenance_risk: float

@dataclass(frozen=True)
class InspectFilter:
    category: tuple[str, ...] = ()
    lifecycle: tuple[Lifecycle, ...] = ()
    stack: tuple[str, ...] = ()
    capability_id: str | None = None

@dataclass(frozen=True)
class DiscoveryContext:
    project_root: Path
    codex_home: Path

@dataclass(frozen=True)
class DiscoveryResult:
    capabilities: tuple[Capability, ...]
    sources_scanned: tuple[str, ...]

@dataclass(frozen=True)
class EvaluationResult:
    capability: Capability | None
    expected_net_value: float
    accepted: bool
    reasons: tuple[str, ...]

@dataclass(frozen=True)
class ReuseResult:
    capability_id: str
    project: str
    succeeded: bool
    integration_effort: float
    benefit: float
    failure_reason: str | None = None

@dataclass(frozen=True)
class ReuseDecision:
    requirement: RequirementProfile
    search_result: SearchResult
    action: str
    selected_capability_id: str | None
    rationale: tuple[str, ...]
```

- [ ] **Step 4: Lock dependencies and make the tests pass**

Run:

```bash
uv lock --project plugins/supermind
uv export --project plugins/supermind --frozen --no-dev --no-emit-project --format requirements-txt --output-file plugins/supermind/requirements.lock
uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_types_config.py -q
```

Expected: dependency resolution succeeds and all focused tests pass.

- [ ] **Step 5: Commit the package contract**

```bash
git add plugins/supermind/pyproject.toml plugins/supermind/uv.lock plugins/supermind/requirements.lock plugins/supermind/model.lock.json plugins/supermind/src/supermind_memory plugins/supermind/tests
git commit -m "feat: define capability memory domain contract"
```

### Task 2: Taxonomy, economics, and lifecycle rules

**Files:**
- Create: `plugins/supermind/src/supermind_memory/taxonomy.py`
- Create: `plugins/supermind/src/supermind_memory/scoring.py`
- Create: `plugins/supermind/src/supermind_memory/lifecycle.py`
- Create: `plugins/supermind/tests/unit/test_taxonomy_scoring.py`

**Interfaces:**
- Consumes: `Capability`, `Evidence`, `Lifecycle`, and `CandidateMatch` from Task 1
- Produces: `TOP_LEVEL_CATEGORIES: tuple[str, ...]`
- Produces: `validate_category_path(path: tuple[str, ...]) -> None`
- Produces: `expected_net_value(inputs: ValueInputs) -> float`
- Produces: `reuse_score(inputs: ReuseScoreInputs) -> float`
- Produces: `next_lifecycle(capability: Capability, evidence: Sequence[Evidence]) -> Lifecycle`

- [ ] **Step 1: Write failing value and lifecycle tests**

```python
def test_one_time_generic_code_with_negative_net_value_stays_observed():
    value = expected_net_value(ValueInputs(
        expected_reuse_count=1,
        benefit_per_reuse=4,
        extraction_cost=3,
        integration_cost=1,
        verification_cost=1,
        maintenance_cost=1,
        failure_risk=1,
    ))
    assert value == -3

def test_verified_capability_requires_independent_reuse_before_recommended():
    evidence = [verification(project="a"), successful_reuse(project="a")]
    assert next_lifecycle(candidate(), evidence) == Lifecycle.VERIFIED

    evidence.append(successful_reuse(project="b"))
    assert next_lifecycle(candidate(), evidence) == Lifecycle.RECOMMENDED

def test_unknown_top_level_category_is_rejected():
    with pytest.raises(ValueError, match="unknown top-level category"):
        validate_category_path(("Miscellaneous", "Login"))
```

- [ ] **Step 2: Run the tests and verify missing rule modules cause failure**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_taxonomy_scoring.py -q`

Expected: FAIL because taxonomy, scoring, and lifecycle functions are undefined.

- [ ] **Step 3: Implement the six stable roots and deterministic rules**

Define these exact top-level values:

```python
TOP_LEVEL_CATEGORIES = (
    "Code and components",
    "Product and business",
    "Design and experience",
    "Engineering and methods",
    "Tools and integrations",
    "Data and intelligence",
)
```

Calculate expected value from the formula in the spec. Keep every reuse-score input normalized to
`0.0..1.0` and use explicit weights: contract fit `0.35`, requirement fit `0.20`, reliability `0.20`,
historical benefit `0.15`, integration cost `-0.05`, maintenance risk `-0.05`. Reject inputs outside
the range. Degrade capabilities whose source is unavailable, content hash changed without
reverification, or latest reuse evidence failed. Require current verification plus a successful
reuse from a different project for `RECOMMENDED`.

- [ ] **Step 4: Run all unit tests**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/unit -q`

Expected: all unit tests pass.

- [ ] **Step 5: Commit the decision rules**

```bash
git add plugins/supermind/src/supermind_memory plugins/supermind/tests/unit
git commit -m "feat: add capability taxonomy and value rules"
```

### Task 3: LanceDB structured repository

**Files:**
- Create: `plugins/supermind/src/supermind_memory/schema.py`
- Create: `plugins/supermind/src/supermind_memory/repository.py`
- Create: `plugins/supermind/tests/integration/test_repository.py`

**Interfaces:**
- Consumes: domain dataclasses from Task 1 and category validation from Task 2
- Produces: `SCHEMA_VERSION = 1` and `EMBEDDING_DIMENSION = 384`
- Produces: `CapabilityRepository.open(database_path: Path) -> CapabilityRepository`
- Produces: repository methods `initialize`, `upsert_capability`, `get_capability`, `list_capabilities`, `append_evidence`, `append_relationship`, `append_event`, `list_evidence`, `list_relationships`, `list_events`, and `set_metadata`

- [ ] **Step 1: Write a failing repository round-trip test**

```python
def test_repository_round_trips_capability_and_audit_records(tmp_path, capability, vector):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    repo.upsert_capability(capability, vector)
    repo.append_evidence(verification(project="project-a"))
    repo.append_event(discovered_event(capability.id))

    assert repo.get_capability(capability.id) == capability
    assert repo.list_evidence(capability.id)[0].source_project == "project-a"
    assert repo.list_events(capability.id)[0].event_type == "discovered"
```

- [ ] **Step 2: Run the repository test and verify it fails before persistence exists**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_repository.py -q`

Expected: FAIL because `CapabilityRepository` is unavailable.

- [ ] **Step 3: Implement explicit Arrow schemas and idempotent writes**

Create tables `capabilities`, `evidence`, `relationships`, `events`, and `metadata`. Store tuple and
nested fields as canonical JSON strings with sorted keys. The capabilities table must include a
`vector` fixed-size float list of length 384 and `search_text`. Use stable string identifiers and
`merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(rows)` for
capability upserts. Evidence and events are append-only with unique event identifiers. Repository
reads convert rows back into Task 1 dataclasses and validate category paths.

- [ ] **Step 4: Run repository and unit tests**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/unit plugins/supermind/tests/integration/test_repository.py -q`

Expected: all selected tests pass, including repeated initialization and upsert idempotency.

- [ ] **Step 5: Commit the repository**

```bash
git add plugins/supermind/src/supermind_memory plugins/supermind/tests/integration/test_repository.py
git commit -m "feat: persist capability records in lancedb"
```

### Task 4: Local embeddings and complete hybrid retrieval

**Files:**
- Create: `plugins/supermind/src/supermind_memory/embeddings.py`
- Create: `plugins/supermind/src/supermind_memory/search.py`
- Create: `plugins/supermind/tests/integration/test_search.py`
- Modify: `plugins/supermind/tests/conftest.py`

**Interfaces:**
- Consumes: `CapabilityRepository`, `RequirementProfile`, `CandidateMatch`, `SearchResult`, and scoring functions
- Produces: protocol `EmbeddingProvider` with `embed_documents(texts: Sequence[str]) -> list[list[float]]` and `embed_query(text: str) -> list[float]`
- Produces: `FastEmbedProvider(model_name: str, cache_dir: Path)`
- Produces: `CapabilitySearch.search(requirement: RequirementProfile, limit: int = 10) -> SearchResult`

- [ ] **Step 1: Write failing hybrid and multilingual retrieval tests**

```python
def test_chinese_login_intent_finds_english_oauth_capability(search):
    result = search.search(requirement(intent="为用户提供邮箱和 OAuth 登录", stack=("TypeScript",)))
    assert result.status is SearchStatus.COMPLETE
    assert result.matches[0].capability_id == "auth.oauth-login"

def test_exact_jwt_constraint_beats_semantically_close_session_module(search):
    result = search.search(requirement(intent="JWT login", constraints=("JWT",)))
    assert result.matches[0].capability_id == "auth.jwt-login"

def test_search_error_is_not_reported_as_no_match(broken_repository, embeddings):
    result = CapabilitySearch(broken_repository, embeddings).search(requirement(intent="login"))
    assert result.status is SearchStatus.FAILED
    assert result.is_no_match is False
```

- [ ] **Step 2: Run the search tests and verify the hybrid engine is missing**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_search.py -q`

Expected: FAIL because embedding and search classes do not exist.

- [ ] **Step 3: Implement deterministic test embeddings and production FastEmbed**

Add a real deterministic `KeywordEmbeddingProvider` fixture that maps the bilingual test vocabulary
into 384 dimensions without network access. In production, download and open only the locked model
revision:

```python
snapshot_dir = snapshot_download(
    repo_id=model_lock.name,
    revision=model_lock.revision,
    cache_dir=str(cache_dir),
)
TextEmbedding(
    model_name=model_lock.name,
    specific_model_path=snapshot_dir,
    local_files_only=True,
)
```

Mark the real provider test with `@pytest.mark.model`; it must assert the locked revision directory
is used, both languages return 384 values, and the provider performs no network request after the
snapshot is present.

Build `search_text` from name, summary, contract, category path, facets, stack, constraints, and
dependencies. Create the FTS index on `search_text`. Query LanceDB with explicit vector and text
inputs using hybrid mode and reciprocal rank fusion. Apply metadata compatibility filters before
reranking, exclude `DEGRADED` and `RETIRED`, calculate the Task 2 reuse score, and return a complete
empty result only after both vector and lexical retrieval complete successfully.

- [ ] **Step 4: Run search integration tests and the pinned model smoke test**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_search.py -m "not model" -q
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_search.py -m model -q
```

Expected: deterministic hybrid tests pass; the model test downloads or reuses the pinned model and
returns a 384-dimensional embedding for both Chinese and English input.

- [ ] **Step 5: Commit retrieval**

```bash
git add plugins/supermind/src/supermind_memory plugins/supermind/tests
git commit -m "feat: add multilingual hybrid capability search"
```

### Task 5: Health, repair, generations, and writer safety

**Files:**
- Create: `plugins/supermind/src/supermind_memory/health.py`
- Create: `plugins/supermind/src/supermind_memory/bootstrap.py`
- Create: `plugins/supermind/tests/integration/test_health.py`

**Interfaces:**
- Consumes: `MemoryPaths`, `CapabilityRepository`, `EmbeddingProvider`, `HealthReport`, and `CapabilityMemoryBlocked`
- Produces: `HealthManager.check() -> HealthReport`
- Produces: `HealthManager.ensure_healthy() -> HealthReport`
- Produces: `HealthManager.rebuild_indexes() -> str`
- Produces: `Bootstrap.initialize(project_root: Path) -> HealthReport`

- [ ] **Step 1: Write failing repair and fail-closed tests**

```python
def test_missing_store_is_created_and_activated(bootstrap, paths):
    report = bootstrap.initialize(project_root=Path.cwd())
    assert report.healthy is True
    assert (paths.generations / report.active_generation).exists()

def test_damaged_derived_index_is_rebuilt_without_losing_records(health, repo):
    capability_id = repo.list_capabilities()[0].id
    damage_search_index(paths)
    report = health.ensure_healthy()
    assert report.repairs == ("rebuild_search_indexes",)
    assert repo.get_capability(capability_id) is not None

def test_unrecoverable_model_failure_blocks_instead_of_searching(health):
    health = make_health_manager(embedding_provider=PermanentlyFailingEmbeddingProvider())
    with pytest.raises(CapabilityMemoryBlocked) as error:
        health.ensure_healthy()
    assert error.value.code == "embedding_unavailable"
```

- [ ] **Step 2: Run health tests and verify missing recovery behavior**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_health.py -q`

Expected: FAIL because health and bootstrap components do not exist.

- [ ] **Step 3: Implement atomic generations and bounded automatic repair**

Persist `active-generation.json` only after runtime, model, schema, source, vector, and FTS checks
all pass. Build repairs in a new generation directory, fsync its metadata, then replace the active
pointer atomically. Use `FileLock(paths.locks / "writer.lock", timeout=30)` around mutations. Retry
only the repair operation up to three attempts with recorded error messages; after the third failure
raise `CapabilityMemoryBlocked`. Do not invoke search from a failed health result. Optimize indexes
after 20 data-modification operations and record the operation as an event.

- [ ] **Step 4: Run health tests including concurrent writers**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_health.py -q`

Expected: initialization, migration, corruption repair, atomic activation, two-process writer
serialization, and explicit blocking tests all pass.

- [ ] **Step 5: Commit health and recovery**

```bash
git add plugins/supermind/src/supermind_memory plugins/supermind/tests/integration/test_health.py
git commit -m "feat: enforce healthy capability indexes"
```

### Task 6: Bounded automatic discovery

**Files:**
- Create: `plugins/supermind/src/supermind_memory/discovery.py`
- Create: `plugins/supermind/tests/integration/test_discovery.py`

**Interfaces:**
- Consumes: `Capability`, `ArtifactType`, `Lifecycle`, `MemoryPaths`, and repository APIs
- Produces: `SourceRegistry.register_active_project(path: Path) -> None`
- Produces: `CapabilityDiscovery.scan_active_project(path: Path) -> tuple[Capability, ...]`
- Produces: `CapabilityDiscovery.scan_installed_plugins(codex_home: Path) -> tuple[Capability, ...]`
- Produces: `CapabilityDiscovery.discover(context: DiscoveryContext) -> DiscoveryResult`

- [ ] **Step 1: Write failing source-boundary tests**

```python
def test_discovery_reads_active_project_and_installed_skill_metadata(tmp_path, discovery):
    project = make_project(tmp_path / "active", readme="OAuth login module", package_name="auth-kit")
    installed = make_installed_skill(tmp_path / "codex", name="debug-helper")
    result = discovery.discover(DiscoveryContext(project_root=project, codex_home=installed))
    assert {item.name for item in result.capabilities} == {"auth-kit", "debug-helper"}

def test_discovery_does_not_scan_unregistered_sibling(tmp_path, discovery):
    active = make_project(tmp_path / "active", readme="Active capability")
    make_project(tmp_path / "private-sibling", readme="Must not be indexed")
    result = discovery.scan_active_project(active)
    assert "Must not be indexed" not in serialize(result)
```

- [ ] **Step 2: Run discovery tests and verify scanners are missing**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_discovery.py -q`

Expected: FAIL because source registry and discovery scanners do not exist.

- [ ] **Step 3: Implement manifest-first bounded discovery**

For the active project, inspect only root README files, package manifests, plugin manifests,
`SKILL.md` frontmatter, public module exports, and test names. For installed capabilities, inspect
plugin manifests and their declared Skill directories beneath the resolved Codex plugin cache.
Never follow symlinks outside an authorized source root. Register discovered Skills under `Tools and
integrations / Codex Skills`, plugins under `Tools and integrations / Plugins and MCP`, and code
packages under `Code and components` with an inferred second-level category only when evidence is
unambiguous. Otherwise leave the record `OBSERVED` at the top-level category.

- [ ] **Step 4: Run discovery and repository tests**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_discovery.py plugins/supermind/tests/integration/test_repository.py -q`

Expected: tests pass and sibling/out-of-root content never appears in serialized results.

- [ ] **Step 5: Commit discovery**

```bash
git add plugins/supermind/src/supermind_memory/discovery.py plugins/supermind/tests/integration/test_discovery.py
git commit -m "feat: discover reusable capabilities safely"
```

### Task 7: Capability Memory orchestration service

**Files:**
- Create: `plugins/supermind/src/supermind_memory/service.py`
- Create: `plugins/supermind/tests/integration/test_service.py`

**Interfaces:**
- Consumes: bootstrap, discovery, repository, search, scoring, and lifecycle APIs from Tasks 1–6
- Produces: `CapabilityMemory.initialize(project_root: Path) -> HealthReport`
- Produces: `CapabilityMemory.discover(context: DiscoveryContext) -> DiscoveryResult`
- Produces: `CapabilityMemory.search(requirement: RequirementProfile) -> SearchResult`
- Produces: `CapabilityMemory.evaluate(candidate: Capability, inputs: ValueInputs) -> EvaluationResult`
- Produces: `CapabilityMemory.register(capability: Capability, evidence: Sequence[Evidence]) -> Capability`
- Produces: `CapabilityMemory.record_use(result: ReuseResult) -> Capability`
- Produces: `CapabilityMemory.get(capability_id: str) -> Capability | None`
- Produces: `CapabilityMemory.refresh_sources(project_root: Path) -> DiscoveryResult`
- Produces: `CapabilityMemory.rebuild() -> HealthReport`
- Produces: `CapabilityMemory.health_check() -> HealthReport`

- [ ] **Step 1: Write the failing login lifecycle integration test**

```python
def test_login_moves_from_candidate_to_recommended_after_independent_reuse(memory):
    memory.initialize(PROJECT_A)
    candidate = memory.evaluate(login_capability(PROJECT_A), positive_value_inputs()).capability
    registered = memory.register(candidate, [verification(project="project-a")])
    assert registered.lifecycle is Lifecycle.VERIFIED

    updated = memory.record_use(successful_reuse(capability_id=registered.id, project="project-b"))
    assert updated.lifecycle is Lifecycle.RECOMMENDED

def test_search_refuses_to_run_when_health_cannot_be_restored(memory):
    memory.health_manager = UnrecoverableHealthManager()
    with pytest.raises(CapabilityMemoryBlocked):
        memory.search(requirement(intent="login"))
```

- [ ] **Step 2: Run service tests and verify orchestration is absent**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_service.py -q`

Expected: FAIL because `CapabilityMemory` is not implemented.

- [ ] **Step 3: Implement health-gated operations and evidence transactions**

Call `ensure_healthy()` before every search and mutation. In `register`, write capability, evidence,
and lifecycle event under one writer lock; generate the embedding before acquiring the lock. In
`record_use`, append outcome evidence, recompute economics and lifecycle, update the capability, and
append one transition event. Reject registration when expected net value is non-positive. Reject a
reuse result whose project is missing or whose capability source is unavailable.

- [ ] **Step 4: Run service, search, and lifecycle tests**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/unit plugins/supermind/tests/integration/test_search.py plugins/supermind/tests/integration/test_service.py -q`

Expected: all selected tests pass with exactly one event per committed state transition.

- [ ] **Step 5: Commit the service**

```bash
git add plugins/supermind/src/supermind_memory/service.py plugins/supermind/tests/integration/test_service.py
git commit -m "feat: orchestrate capability discovery and reuse"
```

### Task 8: Human-readable Capability Explorer

**Files:**
- Create: `plugins/supermind/src/supermind_memory/explorer.py`
- Create: `plugins/supermind/tests/integration/test_explorer.py`

**Interfaces:**
- Consumes: repository lists, `SearchResult`, `Capability`, `Evidence`, and `Relationship`
- Produces: `CapabilityExplorer.overview() -> str`
- Produces: `CapabilityExplorer.table(filters: InspectFilter) -> str`
- Produces: `CapabilityExplorer.detail(capability_id: str) -> str`
- Produces: `CapabilityExplorer.decision(requirement: RequirementProfile, result: SearchResult) -> str`
- Produces: `CapabilityExplorer.graph(filters: InspectFilter) -> str`

- [ ] **Step 1: Write failing snapshot assertions for all four views**

```python
def test_overview_groups_capabilities_by_stable_top_level_category(explorer):
    output = explorer.overview()
    assert "| Category | Total | Candidate | Verified | Recommended |" in output
    assert "| Code and components | 3 | 1 | 1 | 1 |" in output

def test_decision_explains_why_login_was_selected(explorer, login_result):
    output = explorer.decision(requirement(intent="OAuth 登录"), login_result)
    assert "auth.oauth-login" in output
    assert "Contract fit" in output
    assert "Expected net value" in output
    assert "Source" in output

def test_graph_is_valid_mermaid_with_dependency_and_replacement_edges(explorer):
    output = explorer.graph(InspectFilter(category=("Code and components",)))
    assert output.startswith("```mermaid\nflowchart LR\n")
    assert "-->|depends on|" in output
    assert "-.->|replaces|" in output
```

- [ ] **Step 2: Run explorer tests and verify renderers are missing**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_explorer.py -q`

Expected: FAIL because `CapabilityExplorer` does not exist.

- [ ] **Step 3: Implement deterministic Markdown and Mermaid rendering**

Sort categories in the fixed taxonomy order, then capabilities by lifecycle priority, descending
reuse score, and stable identifier. Escape Markdown table cells and Mermaid node labels. Overview
shows counts and an ASCII bar. Detail shows contract, constraints, source revision, evidence,
economics, maturity, and timestamps. Decision prints candidates, selected action, score components,
and rejection reasons. Graph emits dependencies, alternatives, compositions, replacements, and
consumers with stable node identifiers.

- [ ] **Step 4: Run explorer tests twice to prove stable output**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_explorer.py -q
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_explorer.py -q
```

Expected: both runs pass with byte-identical expected strings.

- [ ] **Step 5: Commit the explorer**

```bash
git add plugins/supermind/src/supermind_memory/explorer.py plugins/supermind/tests/integration/test_explorer.py
git commit -m "feat: visualize the capability library"
```

### Task 9: Self-provisioning CLI boundary

**Files:**
- Create: `plugins/supermind/src/supermind_memory/cli.py`
- Create: `plugins/supermind/scripts/capability-memory`
- Create: `plugins/supermind/tests/integration/test_cli.py`

**Interfaces:**
- Consumes: `CapabilityMemory`, `CapabilityExplorer`, and all Task 1 JSON-serializable records
- Produces: `main(argv: Sequence[str] | None = None, service_factory: Callable[[], CapabilityMemory] = build_service) -> int`
- Produces commands: `init`, `discover`, `search`, `evaluate`, `register`, `record-use`, `inspect`, `rebuild`, and `health`
- Produces exit codes: `0` complete, `2` invalid input, `3` capability infrastructure blocked

- [ ] **Step 1: Write failing CLI contract tests**

```python
def test_search_command_emits_typed_json(run_cli, requirement_file, memory_factory):
    result = run_cli(["search", "--input", requirement_file, "--format", "json"], memory_factory)
    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["status"] == "complete"

def test_blocked_health_returns_exit_three_and_attempts(run_cli, broken_memory_factory):
    result = run_cli(["health"], broken_memory_factory)
    payload = json.loads(result.stderr)
    assert result.exit_code == 3
    assert payload["code"] == "embedding_unavailable"
    assert len(payload["attempts"]) == 3
```

- [ ] **Step 2: Run CLI tests and verify commands are unavailable**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_cli.py -q`

Expected: FAIL because the CLI module and launcher do not exist.

- [ ] **Step 3: Implement JSON input, JSON/Markdown output, and bootstrap**

Use `argparse` with one subparser per command and a global `--data-home` option for isolated testing.
Accept structured input from an explicit file path or
stdin, never from shell-evaluated text. Emit operation results as JSON by default; `inspect` and
`search --format markdown` use Capability Explorer. Serialize `CapabilityMemoryBlocked` to stderr
with its code, message, and attempts and return `3`.

The executable launcher must resolve its own plugin root, resolve the Codex data home without
overwriting environment variables, create `<memory-root>/runtime/venv` with `python3 -m venv`, install
`requirements.lock` when its SHA-256 changes, and execute `python -m supermind_memory.cli`. If Python,
venv creation, locked installation, or command execution fails, print a structured blocking error
and return `3`; do not call a reduced implementation.

- [ ] **Step 4: Run CLI and clean-environment bootstrap tests**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_cli.py -q
memory_test_home="$(mktemp -d)"
plugins/supermind/scripts/capability-memory --data-home "$memory_test_home" init --project-root "$PWD" --format json
```

Expected: CLI tests pass; the clean environment provisions dependencies, initializes a healthy
generation, prints `"healthy": true`, and exits `0`.

- [ ] **Step 5: Commit the CLI and launcher**

```bash
git add plugins/supermind/src/supermind_memory/cli.py plugins/supermind/scripts/capability-memory plugins/supermind/tests/integration/test_cli.py
git commit -m "feat: add self-provisioning capability memory cli"
```

### Task 10: Supermind integration and end-to-end acceptance

**Files:**
- Create: `plugins/supermind/tests/e2e/test_login_reuse.py`
- Create: `plugins/supermind/src/supermind_memory/workflow.py`
- Modify: `plugins/supermind/src/supermind_memory/cli.py`
- Modify: `plugins/supermind/skills/supermind/SKILL.md`
- Modify: `plugins/supermind/skills/supermind/references/capability-routing.md`
- Modify: `plugins/supermind/.codex-plugin/plugin.json`
- Modify: `README.md`
- Modify: `scripts/verify.sh`

**Interfaces:**
- Consumes: launcher commands and exit codes from Task 9
- Produces: `SupermindWorkflow.begin_design(project_root: Path, requirement: RequirementProfile) -> ReuseDecision`
- Produces: `SupermindWorkflow.complete_implementation(capability: Capability, inputs: ValueInputs, evidence: Sequence[Evidence]) -> Capability | None`
- Produces: `SupermindWorkflow.complete_reuse(result: ReuseResult) -> Capability`
- Produces: mandatory Supermind workflow hooks for initialization, pre-design search, post-verification evaluation, reuse evidence, inspection, and blocking
- Produces: release verification covering package tests and plugin structure

- [ ] **Step 1: Write the failing end-to-end login scenario**

```python
def test_login_capability_is_discovered_reused_visualized_and_invalidated(workflow, memory, explorer, projects):
    memory.initialize(projects.a)
    login = workflow.complete_implementation(
        login_capability(projects.a),
        positive_value_inputs(),
        [verification(project="project-a")],
    )
    assert login.lifecycle is Lifecycle.VERIFIED

    decision_b = workflow.begin_design(projects.b, oauth_requirement(projects.b, language="zh"))
    assert decision_b.search_result.matches[0].capability_id == login.id
    recommended = workflow.complete_reuse(successful_reuse(login.id, projects.b))
    assert recommended.lifecycle is Lifecycle.RECOMMENDED

    decision_c = workflow.begin_design(projects.c, oauth_requirement(projects.c, language="zh"))
    decision = explorer.decision(decision_c.requirement, decision_c.search_result)
    assert "Code and components / Identity and access" in decision
    assert "Expected net value" in decision

    projects.a.remove_source(login.source_uri)
    memory.refresh_sources(projects.a)
    assert login.id not in {match.capability_id for match in memory.search(oauth_requirement(projects.c)).matches}

    damage_search_index(memory.paths)
    assert workflow.begin_design(projects.c, oauth_requirement(projects.c)).search_result.status is SearchStatus.COMPLETE

    make_repair_impossible(memory)
    with pytest.raises(CapabilityMemoryBlocked):
        workflow.begin_design(projects.c, oauth_requirement(projects.c))
```

- [ ] **Step 2: Run the end-to-end test and confirm the skill is not yet wired**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/e2e/test_login_reuse.py -q`

Expected: FAIL because `SupermindWorkflow` and the mandatory design hook do not exist.

- [ ] **Step 3: Wire the CLI into the Supermind skill contract**

Implement `SupermindWorkflow` so `begin_design` initializes, refreshes registered sources, health
checks, and performs one complete search before returning a decision. It must propagate
`CapabilityMemoryBlocked` and never return a build decision after failed retrieval.
`complete_implementation` evaluates and registers positive-value candidates and returns `None` for
non-positive value. `complete_reuse` records outcome evidence and returns the updated capability.
Expose these operations as `begin-design`, `complete-implementation`, and `complete-reuse` CLI
commands.

Update `SKILL.md` so every invocation initializes and health-checks Capability Memory. Before
designing a module likely to recur, require a complete `search` call and stop on exit `3`. After
implementation verification, call `evaluate` and register positive-value candidates. After every
reuse attempt, call `record-use`. Link Capability Explorer instructions from capability routing and
teach natural-language requests such as “查看能力库”, “显示登录能力”, and “画出能力关系” to invoke
the corresponding `inspect` view. Keep Superpowers optional and provider-neutral.

Update README and plugin metadata to advertise automatic capability reuse and human-readable
inspection. Extend `scripts/verify.sh` with the complete release-tree file list, executable-bit check
for the launcher, locked-dependency check, full pytest command, and end-to-end test.

- [ ] **Step 4: Run full verification and inspect real output**

Run:

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests -q
./scripts/verify.sh
plugins/supermind/scripts/capability-memory inspect --view overview --format markdown
plugins/supermind/scripts/capability-memory inspect --view graph --format markdown
git diff --check
```

Expected: all tests and release checks pass; overview prints the six categories; graph output begins
with a Mermaid flowchart; no command reports a degraded or skipped search.

- [ ] **Step 5: Reinstall the self-hosted plugin and verify Codex sees it**

Run:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/plugin-creator/scripts/update_plugin_cachebuster.py" plugins/supermind
codex plugin add supermind@supermind
codex plugin list
```

Expected: `supermind@supermind` is `installed, enabled`, its version contains one current
`+codex.local-<timestamp>` suffix, and its installed source is the current Supermind marketplace.

- [ ] **Step 6: Commit the integrated product capability**

```bash
git add README.md plugins/supermind scripts/verify.sh
git commit -m "feat: make capability reuse a default Supermind behavior"
```

## Final Verification

- [ ] Run `uv run --project plugins/supermind pytest plugins/supermind/tests -q` and confirm zero failures.
- [ ] Run `./scripts/verify.sh` and confirm Skill, plugin, lockfile, executable, and release-tree validation pass.
- [ ] Run `git diff --check` and confirm no whitespace errors.
- [ ] Run `git status --short` and confirm only intentionally uncommitted plan-tracking edits remain.
- [ ] Start a new Codex task with `$supermind`, request a design containing login, and confirm it performs and explains a complete Capability Memory search before proposing implementation.
