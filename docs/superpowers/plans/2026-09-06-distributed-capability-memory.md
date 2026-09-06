# Distributed Capability Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract Capability Memory into an independent daemonless Python CLI whose immutable events synchronize through a private GitHub repository and whose generated root README is a categorized, nested, collapsible browser.

**Architecture:** Add an event authority, deterministic projector, Git synchronization coordinator, and Markdown renderer around the existing proven service before moving the package to a top-level standalone project. Keep LanceDB, FastEmbed, FTS, generations, and model data local and derived; make the Supermind plugin a pinned self-bootstrap launcher and JSON protocol client only after migration equivalence passes.

**Tech Stack:** Python 3.11–3.14, standard-library JSON/Git subprocesses, LanceDB 0.38.0, FastEmbed 0.8.0, filelock 3.32.5, pytest 9.1.1, uv, Git, GitHub CLI.

**Spec:** `docs/product/2026-09-06-distributed-capability-memory-design.md`

## Global Constraints

- Only the independent Python CLI may create events, materialize memory, update indexes, or render the browser.
- The private Git repository stores immutable canonical JSON events and generated Markdown; event JSON is authoritative.
- LanceDB files, vectors, FTS indexes, models, runtimes, locks, credentials, and source artifacts never enter the sync repository.
- The root README is the default browser and uses nested GitHub-native `<details>` and `<summary>` blocks under the six stable top-level categories.
- GitHub access uses the existing `gh`, Git credential helper, or SSH agent; the product never stores tokens.
- Repository initialization accepts only an empty repository or one with a compatible `memory.json`, and verifies that GitHub reports it private.
- Offline writes after successful initialization return `pending_sync`; they do not claim cross-device freshness.
- Concurrent event changes to the same entity produce an explicit conflict; timestamp or last-writer-wins resolution is forbidden.
- Generated Markdown is derived, carries an event-set digest, and is never parsed back into authority.
- The CLI is process-based and opens no daemon, HTTP listener, local port, custom HTML application, or GitHub Pages site.
- Complete hybrid retrieval, the existing one-repair policy, sanitization, evidence gates, and fail-closed authoritative-history rules remain unchanged.
- There is no lexical-only, vector-only, stale-generation, embedded-plugin, or other degraded fallback.
- The embedded writer is removed only after real migration, replay, retrieval, render, and second-checkout equivalence pass.
- New state lives under `<resolved Codex home>/supermind/memory/`; the legacy `<resolved Codex home>/supermind/capability-memory/` tree is never reused or overwritten during migration.

---

## File responsibility map

New behavior is first built under `plugins/supermind/` so every intermediate commit keeps the current release testable. Task 9 moves the complete Python project without changing imports.

| Final path | Responsibility |
| --- | --- |
| `capability-memory/pyproject.toml` | Standalone package metadata, `supermind-memory` console script, frozen runtime and test dependencies. |
| `capability-memory/src/supermind_memory/event_model.py` | Canonical event types, strict decoding, sanitization, hashes, repository marker types. |
| `capability-memory/src/supermind_memory/event_store.py` | Safe immutable event-file persistence, discovery, duplicate and corruption checks. |
| `capability-memory/src/supermind_memory/replay.py` | Parent-graph validation, deterministic replay, tombstones, and explicit conflicts. |
| `capability-memory/src/supermind_memory/projection.py` | Convert replayed authority into the existing seven-table LanceDB materialization and compare digests. |
| `capability-memory/src/supermind_memory/migration.py` | Journaled export of the current validated embedded store into initial events. |
| `capability-memory/src/supermind_memory/git_client.py` | Argument-safe Git and GitHub CLI execution plus private-repository verification. |
| `capability-memory/src/supermind_memory/sync.py` | Local lock, fetch/merge/replay/render/commit/push transaction and `pending_sync`. |
| `capability-memory/src/supermind_memory/renderer.py` | Deterministic README, category, capability, demand, and Mermaid generation. |
| `capability-memory/src/supermind_memory/protocol.py` | Versioned JSON envelopes, sync states, operation IDs, and stable exit-code mapping. |
| `capability-memory/src/supermind_memory/config.py` | Standalone data paths, checkout configuration, device identity, and protocol compatibility. |
| `capability-memory/src/supermind_memory/cli.py` | Standalone command parsing and orchestration; no plugin knowledge. |
| `capability-memory/tests/` | Unit, integration, migration, Git-sync, rendering, protocol, and cross-checkout evidence. |
| `plugins/supermind/scripts/capability-memory` | Small bootstrap/locator that installs and executes one pinned standalone CLI revision. |
| `plugins/supermind/vendor/supermind_capability_memory-<version>-py3-none-any.whl` | Built standalone CLI artifact carried only for deterministic self-bootstrap. |
| `plugins/supermind/tool.lock.json` | Exact wheel path, package version, protocol range, and SHA-256. |
| `plugins/supermind/skills/supermind/SKILL.md` | Plugin workflow contract for CLI health, configuration, and no-fallback behavior. |
| `scripts/verify.sh` | Validate both distributions, frozen locks, release tree, protocol compatibility, and all tests. |

---

### Task 1: Canonical event and repository marker contract

**Files:**
- Create: `plugins/supermind/src/supermind_memory/event_model.py`
- Create: `plugins/supermind/schemas/v1/event.schema.json`
- Create: `plugins/supermind/schemas/v1/memory.schema.json`
- Modify: `plugins/supermind/src/supermind_memory/redaction.py`
- Test: `plugins/supermind/tests/unit/test_event_model.py`

**Interfaces:**
- Consumes: `redact_text(value: str) -> str` and sanitized payload values.
- Produces: `MemoryMarker`, `AuthorityEvent`, `EntityConflict`, `canonical_json(value: object) -> bytes`, `AuthorityEvent.create(...) -> AuthorityEvent`, and `AuthorityEvent.from_bytes(raw: bytes) -> AuthorityEvent`.

- [ ] **Step 1: Write failing canonicalization and secret-boundary tests**

```python
def test_authority_event_is_canonical_hashed_and_round_trips():
    event = AuthorityEvent.create(
        event_id="01JTEST0000000000000000001",
        device_id="device-a",
        entity_type="capability",
        entity_id="login",
        operation="registered",
        parent_event_ids=(),
        occurred_at="2026-09-06T12:00:00Z",
        payload={"name": "登录", "stack": ["python"]},
    )
    encoded = event.to_bytes()
    assert encoded.endswith(b"\n")
    assert AuthorityEvent.from_bytes(encoded) == event
    assert b'"content_hash"' in encoded


def test_secret_is_removed_before_event_hash_and_payload():
    event = AuthorityEvent.create(
        event_id="01JTEST0000000000000000002",
        device_id="device-a",
        entity_type="evidence",
        entity_id="evidence-1",
        operation="observed",
        parent_event_ids=(),
        occurred_at="2026-09-06T12:00:00Z",
        payload={"supporting_uri": "https://example.test/?access_token=secret-value"},
    )
    assert b"secret-value" not in event.to_bytes()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_event_model.py -q`

Expected: FAIL because `supermind_memory.event_model` does not exist.

- [ ] **Step 3: Implement frozen strict event types and canonical hashing**

```python
@dataclass(frozen=True)
class AuthorityEvent:
    schema_version: int
    event_id: str
    device_id: str
    entity_type: str
    entity_id: str
    operation: str
    parent_event_ids: tuple[str, ...]
    occurred_at: str
    payload: Mapping[str, JSONValue]
    content_hash: str

    @classmethod
    def create(cls, *, event_id: str, device_id: str, entity_type: str,
               entity_id: str, operation: str, parent_event_ids: Sequence[str],
               occurred_at: str, payload: Mapping[str, JSONValue]) -> "AuthorityEvent":
        clean = sanitize_json(payload)
        unsigned = event_document(1, event_id, device_id, entity_type, entity_id,
                                  operation, parent_event_ids, occurred_at, clean)
        digest = hashlib.sha256(canonical_json(unsigned)).hexdigest()
        return cls(1, event_id, device_id, entity_type, entity_id, operation,
                   tuple(sorted(parent_event_ids)), occurred_at, clean, digest)
```

Reject unknown/missing keys, non-UTC timestamps, malformed IDs, duplicate parents, unsupported entity/operation pairs, non-finite numbers, oversized/deep payloads, and hash mismatch. `MemoryMarker` must require format `1`, event schema `[1]`, renderer version, repository ID, and default branch. Write matching JSON Schema documents with `additionalProperties: false`.

- [ ] **Step 4: Add adversarial decode cases**

```python
@pytest.mark.parametrize("mutation", [
    lambda value: value.update({"unexpected": True}),
    lambda value: value.update({"occurred_at": "2026-09-06 12:00"}),
    lambda value: value.update({"content_hash": "0" * 64}),
    lambda value: value.update({"parent_event_ids": ["same", "same"]}),
])
def test_invalid_event_document_is_rejected(valid_event_document, mutation):
    mutation(valid_event_document)
    with pytest.raises(EventValidationError):
        AuthorityEvent.from_bytes(json.dumps(valid_event_document).encode())
```

- [ ] **Step 5: Run tests and commit**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_event_model.py plugins/supermind/tests/unit/test_redaction.py -q
git diff --check
git add plugins/supermind/src/supermind_memory/event_model.py plugins/supermind/src/supermind_memory/redaction.py plugins/supermind/schemas plugins/supermind/tests/unit/test_event_model.py
git commit -m "feat: define immutable capability memory events"
```

Expected: focused tests PASS; schemas and Python decoder agree on every required field.

---

### Task 2: Safe immutable event store and deterministic replay

**Files:**
- Create: `plugins/supermind/src/supermind_memory/event_store.py`
- Create: `plugins/supermind/src/supermind_memory/replay.py`
- Test: `plugins/supermind/tests/unit/test_event_store.py`
- Test: `plugins/supermind/tests/unit/test_replay.py`

**Interfaces:**
- Consumes: `AuthorityEvent` and an owned checkout root.
- Produces: `EventStore.append(event: AuthorityEvent) -> Path`, `EventStore.load_all() -> tuple[AuthorityEvent, ...]`, `ReplayResult(entities, heads, conflicts, digest)`, and `replay(events: Sequence[AuthorityEvent]) -> ReplayResult`.

- [ ] **Step 1: Write failing append safety and duplicate tests**

```python
def test_append_uses_device_month_and_never_overwrites(tmp_path, event):
    store = EventStore(tmp_path)
    path = store.append(event)
    assert path.relative_to(tmp_path).as_posix() == (
        "events/v1/device-a/2026-09/01JTEST0000000000000000001.json"
    )
    assert store.append(event) == path
    altered = replace(event, content_hash="f" * 64)
    with pytest.raises(EventCollisionError):
        store.append(altered)


def test_symlinked_event_tree_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "events").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafeEventPath):
        EventStore(tmp_path).load_all()
```

- [ ] **Step 2: Verify store tests are RED, then implement atomic no-follow writes**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_event_store.py -q`

Expected: FAIL because `EventStore` is absent.

Use `os.open` with `O_CREAT | O_EXCL | O_NOFOLLOW`, write canonical bytes, `fsync` the file and parent directory, validate every parent is a real directory inside the checkout, and treat byte-identical existing files as idempotent success.

- [ ] **Step 3: Write failing replay, tombstone, and conflict tests**

```python
def test_replay_is_independent_of_input_order(event_chain):
    forward = replay(event_chain)
    reverse = replay(tuple(reversed(event_chain)))
    assert forward.digest == reverse.digest
    assert forward.entities == reverse.entities


def test_sibling_updates_create_conflict(base_event, update_event):
    sibling = replace_event(update_event, event_id="01JSIBLING", payload={"name": "B"})
    result = replay((base_event, update_event, sibling))
    assert result.conflicts == (
        EntityConflict("capability", "login", (update_event.event_id, sibling.event_id)),
    )
    assert ("capability", "login") not in result.entities


def test_tombstone_removes_entity_from_materialized_state(base_event, tombstone_event):
    assert ("capability", "login") not in replay((base_event, tombstone_event)).entities
```

- [ ] **Step 4: Implement parent-graph replay**

Build an index by event ID, verify every parent exists and belongs to the same entity, reject cycles, find heads per `(entity_type, entity_id)`, and materialize only a single unambiguous head. Compute the event-set digest from sorted `(event_id, content_hash)` pairs, never timestamps or input order. A resolution event must list every conflicting head as parents before it becomes the new single head.

- [ ] **Step 5: Run tests and commit**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_event_store.py plugins/supermind/tests/unit/test_replay.py -q
git diff --check
git add plugins/supermind/src/supermind_memory/event_store.py plugins/supermind/src/supermind_memory/replay.py plugins/supermind/tests/unit/test_event_store.py plugins/supermind/tests/unit/test_replay.py
git commit -m "feat: replay distributed memory events deterministically"
```

---

### Task 3: Project replay into the existing local search store

**Files:**
- Create: `plugins/supermind/src/supermind_memory/projection.py`
- Modify: `plugins/supermind/src/supermind_memory/repository.py`
- Modify: `plugins/supermind/src/supermind_memory/health.py`
- Test: `plugins/supermind/tests/integration/test_projection.py`
- Test: `plugins/supermind/tests/integration/test_health.py`

**Interfaces:**
- Consumes: `ReplayResult`, `CapabilityRepository`, and `EmbeddingProvider`.
- Produces: `ProjectionDigest`, `project_authority(result, repository, embeddings) -> ProjectionDigest`, and `compare_projection(result, repository) -> ProjectionComparison`.

- [ ] **Step 1: Write a failing full-entity projection test**

```python
def test_projection_reconstructs_all_authoritative_tables(empty_repository, embeddings, authority_events):
    replayed = replay(authority_events)
    digest = project_authority(replayed, empty_repository, embeddings)
    assert digest.event_set == replayed.digest
    assert {item.id for item in empty_repository.list_capabilities()} == {"login"}
    assert {item.id for item in empty_repository.list_evidence("login")} == {"ev-login"}
    assert {item.id for item in empty_repository.list_requirement_observations()} == {"demand-1"}
    assert compare_projection(replayed, empty_repository).equivalent is True
```

- [ ] **Step 2: Verify RED and add replacement-transaction repository API**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_projection.py -q`

Expected: FAIL because projection APIs do not exist.

Add `CapabilityRepository.replace_authority(snapshot: AuthoritySnapshot, vectors: Mapping[str, Sequence[float]]) -> None`. It must write all seven authoritative tables to a journaled temporary database, validate requirement history, atomically activate it, and preserve no rows absent from the replay.

- [ ] **Step 3: Implement typed projection and digest comparison**

```python
def project_authority(result: ReplayResult, repository: CapabilityRepository,
                      embeddings: EmbeddingProvider) -> ProjectionDigest:
    snapshot = authority_snapshot(result.entities, conflicts=result.conflicts)
    vectors = {
        capability.id: embeddings.embed_query(capability_search_text(capability))
        for capability in snapshot.capabilities
    }
    repository.replace_authority(snapshot, vectors)
    comparison = compare_projection(result, repository)
    if not comparison.equivalent:
        raise ProjectionBlocked("projection_mismatch", comparison.differences)
    return ProjectionDigest(result.digest, comparison.materialized_digest)
```

Conversion must reuse existing dataclass parsers and schema validation rather than deserialize raw payloads directly into Arrow rows.

Unconflicted entities are projected normally. For a conflicted entity, retain its last common
ancestor only as diagnostic history, persist the conflicting head IDs in local metadata, and exclude
the entity from search eligibility until a resolution event names every head as a parent. A conflict
must not make unrelated capabilities unavailable.

- [ ] **Step 4: Make health bind the active generation to the event-set digest**

Persist `authority_event_set_digest` in local metadata and the generation manifest. Health fails closed when the replay digest, materialized digest, or active generation's authority digest differs.

Until Task 8 routes all writers through events, enable this check only when the new repository
configuration contains `authority_mode: "events-v1"`. Task 8 removes the temporary legacy-authority
branch as part of the event-first cutover; it is an implementation staging guard, not a released
fallback.

- [ ] **Step 5: Run projection and existing repository/health tests, then commit**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_projection.py plugins/supermind/tests/integration/test_repository.py plugins/supermind/tests/integration/test_health.py -q
git diff --check
git add plugins/supermind/src/supermind_memory/projection.py plugins/supermind/src/supermind_memory/repository.py plugins/supermind/src/supermind_memory/health.py plugins/supermind/tests/integration/test_projection.py plugins/supermind/tests/integration/test_health.py
git commit -m "feat: derive local search state from memory events"
```

---

### Task 4: Journaled exporter for the current real memory

**Files:**
- Create: `plugins/supermind/src/supermind_memory/migration.py`
- Modify: `plugins/supermind/src/supermind_memory/repository.py`
- Test: `plugins/supermind/tests/integration/test_migration.py`

**Interfaces:**
- Consumes: a healthy schema-v2 `CapabilityRepository`, a `device_id`, and an empty `EventStore`.
- Produces: `MigrationReport(source_digest, event_set_digest, entity_counts, equivalent)` and `export_embedded_store(repository, event_store, device_id) -> MigrationReport`.

- [ ] **Step 1: Write failing complete-history migration tests**

```python
def test_export_replay_preserves_all_rows_and_demand_history(populated_repository, empty_event_store, embeddings):
    report = export_embedded_store(populated_repository, empty_event_store, "migration-device")
    replayed = replay(empty_event_store.load_all())
    rebuilt = fresh_repository()
    project_authority(replayed, rebuilt, embeddings)
    assert report.equivalent is True
    assert authority_rows(rebuilt) == authority_rows(populated_repository)


def test_export_refuses_incomplete_authoritative_history(corrupt_v2_repository, empty_event_store):
    with pytest.raises(MigrationBlocked, match="authoritative_store_corrupt"):
        export_embedded_store(corrupt_v2_repository, empty_event_store, "migration-device")
    assert empty_event_store.load_all() == ()
```

- [ ] **Step 2: Verify RED, then expose stable authoritative snapshots**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_migration.py -q`

Expected: FAIL because the exporter and `authority_rows` snapshot do not exist.

Add `CapabilityRepository.authority_snapshot() -> AuthoritySnapshot` under the writer lock after schema and demand-history validation. Sort each entity collection by stable ID.

- [ ] **Step 3: Implement deterministic export without fabricated evidence**

Create one initial event for every current entity. Preserve capability, evidence, relationship, demand, reuse, and historical audit IDs in payloads. Where the old event table is descriptive rather than a full state transition, export it as `entity_type="audit"`; do not infer missing parents or successful outcomes. Write a migration journal containing source digest, intended event IDs, completed paths, and final replay digest; validate or resume it on retry.

- [ ] **Step 4: Add crash-resume and idempotency tests**

Inject a write failure after the third file, rerun export, and assert the completed event set and digest equal a clean export. Altering an already written event must return `migration_event_collision` rather than replacing it.

- [ ] **Step 5: Run tests and commit**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_migration.py plugins/supermind/tests/integration/test_repository.py -q
git diff --check
git add plugins/supermind/src/supermind_memory/migration.py plugins/supermind/src/supermind_memory/repository.py plugins/supermind/tests/integration/test_migration.py
git commit -m "feat: export embedded capability memory authority"
```

---

### Task 5: Private GitHub repository initialization

**Files:**
- Create: `plugins/supermind/src/supermind_memory/git_client.py`
- Modify: `plugins/supermind/src/supermind_memory/config.py`
- Test: `plugins/supermind/tests/unit/test_git_client.py`
- Test: `plugins/supermind/tests/integration/test_repository_init.py`

**Interfaces:**
- Consumes: a `CommandRunner.run(argv: Sequence[str], cwd: Path | None) -> CompletedCommand`, existing Git/gh authentication, and `MemoryMarker`.
- Produces: `GitRepositoryRef(host, owner, name, clone_url, default_branch, repository_id)`, `GitClient`, `GitHubClient`, and `initialize_repository(request: InitRequest, paths: MemoryPaths) -> RepositoryConfig`.

- [ ] **Step 1: Write failing argument-vector and privacy tests**

```python
def test_git_commands_never_use_shell(fake_runner, checkout):
    GitClient(fake_runner).fetch(checkout, "origin")
    assert fake_runner.calls == [("git", "fetch", "--prune", "origin")]
    assert fake_runner.shell_was_used is False


@pytest.mark.parametrize("visibility", ["PUBLIC", "INTERNAL", None])
def test_init_rejects_non_private_repository(fake_gh, tmp_path, visibility):
    fake_gh.repository_visibility = visibility
    with pytest.raises(RepositoryInitBlocked, match="repository_not_private"):
        initialize_repository(connect_request("owner/memory"), memory_paths(tmp_path))
```

- [ ] **Step 2: Verify RED and implement safe command runner**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_git_client.py plugins/supermind/tests/integration/test_repository_init.py -q`

Expected: FAIL because Git/GitHub clients are absent.

Use `subprocess.run(tuple(argv), shell=False, check=False, text=False, env=credential_passthrough_env())`. Redact stdout/stderr before including them in diagnostics. Never pass credentials as arguments or constructed environment values.

Extend `MemoryPaths` with exact owned locations: `root=<data-home>/supermind/memory`,
`config=root/config.json`, `checkout=root/repository`, `database=root/derived/database`,
`generations=root/derived/generations`, `model_cache=root/model-cache`, and
`locks=root/locks`. `RepositoryConfig` stores only repository ID, `owner/name`, HTTPS web URL,
clone URL, branch, device ID, protocol version, authority mode, and last successfully checked remote
head.

- [ ] **Step 3: Implement connect-existing and create-private flows**

`gh repo view owner/name --json id,nameWithOwner,isPrivate,defaultBranchRef,sshUrl,url` must report `isPrivate: true`. Creation uses `gh repo create owner/name --private --confirm`, followed by an independent `repo view` privacy check. Clone into a newly created Supermind-owned checkout; accept only empty repositories or a valid marker whose repository ID equals GitHub's returned ID.

- [ ] **Step 4: Add unrelated, dirty, symlink, and marker mismatch tests**

Use local bare Git repositories and a fake GitHub metadata runner. Assert initialization rejects a non-empty unmarked repo, a dirty attached checkout, symlinked `.git`/event paths, and a copied marker with another repository ID.

- [ ] **Step 5: Run tests and commit**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_git_client.py plugins/supermind/tests/integration/test_repository_init.py -q
git diff --check
git add plugins/supermind/src/supermind_memory/git_client.py plugins/supermind/src/supermind_memory/config.py plugins/supermind/tests/unit/test_git_client.py plugins/supermind/tests/integration/test_repository_init.py
git commit -m "feat: initialize private capability memory repositories"
```

---

### Task 6: Incremental Git synchronization and offline commits

**Files:**
- Create: `plugins/supermind/src/supermind_memory/sync.py`
- Modify: `plugins/supermind/src/supermind_memory/git_client.py`
- Test: `plugins/supermind/tests/integration/test_sync.py`

**Interfaces:**
- Consumes: `EventStore`, `replay`, `project_authority`, `render_repository` from Task 7 through an injected `RenderPort`, `GitClient`, and the memory writer lock.
- Produces: `SyncState` (`committed`, `synced`, `pending_sync`), `SyncReport`, and `SyncCoordinator.mutate(create_event: Callable[[ReplayResult], AuthorityEvent]) -> SyncReport`.

- [ ] **Step 1: Write failing two-checkout merge and conflict tests**

```python
def test_two_devices_merge_different_entities(remote, device_a, device_b):
    device_a.register(capability_event("login", device="a"))
    device_b.register(capability_event("upload", device="b"))
    device_a.sync()
    device_b.sync()
    assert entity_ids(device_b.replay()) == {"login", "upload"}


def test_two_devices_do_not_silently_merge_sibling_updates(remote, device_a, device_b):
    seed_both(remote, base_capability_event("login"), device_a, device_b)
    device_a.update(update_event("login", "A"))
    device_b.update(update_event("login", "B"))
    with pytest.raises(SyncBlocked, match="authority_conflict"):
        device_b.sync()
    assert device_b.search("login").matches == ()
    assert device_b.search("unrelated verified capability").status == "complete"
```

- [ ] **Step 2: Verify RED and implement immutable-tree merge**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_sync.py -q`

Expected: FAIL because `SyncCoordinator` is absent.

Before any mutation, re-read GitHub metadata and require the configured repository ID and private
visibility to match initialization. After fetch, compare local and remote event paths. Copy only a
missing, validated event into the merge worktree. Equal path/equal hash is idempotent; equal
path/different hash is corruption. Never invoke Git's content merge for event JSON and never force
push.

- [ ] **Step 3: Implement bounded non-fast-forward retry**

```python
for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
    fetched = self._fetch_if_reachable()
    if fetched:
        self._merge_remote_events()
    report = self._replay_project_render_commit(create_event)
    if not fetched:
        return replace(report, sync_state=SyncState.PENDING_SYNC)
    pushed = self.git.push(self.checkout, self.branch)
    if pushed:
        return replace(report, sync_state=SyncState.SYNCED)
raise SyncBlocked("push_retry_exhausted", self.git.redacted_attempts)
```

If the remote is unavailable after a prior successful initialization, commit locally and return `pending_sync`. Before an online mutation, publish or reconcile every pending local commit first.

- [ ] **Step 4: Add offline recovery, privacy-loss, no-force-push, and corrupt-event tests**

Assert an offline event is locally searchable, later reaches a second checkout after sync, a
repository changed to public blocks the next mutation before event creation, no recorded command
contains `--force`, and a remote hash mismatch blocks before projection or render.

- [ ] **Step 5: Run tests and commit**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_sync.py plugins/supermind/tests/unit/test_git_client.py -q
git diff --check
git add plugins/supermind/src/supermind_memory/sync.py plugins/supermind/src/supermind_memory/git_client.py plugins/supermind/tests/integration/test_sync.py
git commit -m "feat: synchronize capability events through git"
```

---

### Task 7: Generated collapsible README and Markdown catalog

**Files:**
- Create: `plugins/supermind/src/supermind_memory/renderer.py`
- Modify: `plugins/supermind/src/supermind_memory/explorer.py`
- Test: `plugins/supermind/tests/unit/test_renderer.py`
- Test: `plugins/supermind/tests/integration/test_render_repository.py`

**Interfaces:**
- Consumes: a conflict-free `ReplayResult`, the existing taxonomy, evidence-derived lifecycle rules, and a checkout root.
- Produces: `RenderManifest`, `render_files(result: ReplayResult) -> Mapping[PurePosixPath, bytes]`, `write_rendered_repository(root, files) -> RenderManifest`, and `validate_render(root, event_set_digest) -> RenderManifest`.

- [ ] **Step 1: Write a failing root README structure test**

```python
def test_root_readme_is_nested_foldable_human_catalog(replayed_library):
    files = render_files(replayed_library)
    readme = files[PurePosixPath("README.md")].decode()
    assert readme.count("<details>") == readme.count("</details>")
    assert "<summary>代码与组件 · 2 项 · 1 项已验证</summary>" in readme
    assert "<summary>登录与身份服务 · 推荐复用 · 当前可用</summary>" in readme
    assert "[打开完整能力卡](capabilities/login.md)" in readme
    assert "vector_score" not in readme
```

- [ ] **Step 2: Verify RED and implement deterministic rendering**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_renderer.py -q`

Expected: FAIL because `renderer.py` does not exist.

Render all six stable categories in taxonomy order, then capabilities by normalized human name and ID. Collapse tools/integrations by default. Every capability summary includes human name, maturity, availability, and purpose; its body includes problem, fit, evidence count, reuse count, source description, and detail link.

- [ ] **Step 3: Render every required repository view**

Return an exact file map for root `README.md`, `catalog/README.md`, six category pages, `capabilities/<safe-id>.md`, `demands/open.md`, `demands/resolved.md`, `relationships.md`, and `.supermind/render-manifest.json`. Capability pages include contract, constraints, evidence, economics, dependencies, alternatives, consumers, conflicts, and demand links. `relationships.md` uses stable Mermaid node IDs derived from hashes, never raw identifiers.

- [ ] **Step 4: Add escaping, stale output, deletion, and digest tests**

```python
def test_untrusted_labels_cannot_inject_html_or_mermaid(replayed_with_hostile_label):
    files = render_files(replayed_with_hostile_label)
    combined = b"\n".join(files.values()).decode()
    assert "<script>" not in combined
    assert "click " not in files[PurePosixPath("relationships.md")].decode()


def test_validate_render_rejects_stale_manifest(rendered_checkout):
    manifest = rendered_checkout / ".supermind/render-manifest.json"
    manifest.write_text(manifest.read_text().replace("event_set_digest", "stale_digest"))
    with pytest.raises(RenderBlocked, match="render_manifest_invalid"):
        validate_render(rendered_checkout, "expected-digest")
```

The writer removes obsolete generated capability pages only when they are listed in the prior valid manifest; it must never delete untracked user files.

- [ ] **Step 5: Integrate renderer into the sync transaction**

Replace Task 6's injected test renderer with `render_files` plus `write_rendered_repository`. Refuse commit/push until `validate_render` returns the current event-set digest.

- [ ] **Step 6: Run tests and commit**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/unit/test_renderer.py plugins/supermind/tests/integration/test_render_repository.py plugins/supermind/tests/integration/test_explorer.py plugins/supermind/tests/integration/test_sync.py -q
git diff --check
git add plugins/supermind/src/supermind_memory/renderer.py plugins/supermind/src/supermind_memory/explorer.py plugins/supermind/src/supermind_memory/sync.py plugins/supermind/tests/unit/test_renderer.py plugins/supermind/tests/integration/test_render_repository.py plugins/supermind/tests/integration/test_sync.py
git commit -m "feat: render a foldable github capability browser"
```

---

### Task 8: Versioned CLI protocol and event-first mutations

**Files:**
- Create: `plugins/supermind/src/supermind_memory/protocol.py`
- Modify: `plugins/supermind/src/supermind_memory/cli.py`
- Modify: `plugins/supermind/src/supermind_memory/service.py`
- Modify: `plugins/supermind/src/supermind_memory/workflow.py`
- Modify: `plugins/supermind/src/supermind_memory/config.py`
- Test: `plugins/supermind/tests/integration/test_cli.py`
- Test: `plugins/supermind/tests/integration/test_event_first_service.py`

**Interfaces:**
- Consumes: `SyncCoordinator`, existing typed CLI inputs, existing search/read APIs, and repository initialization.
- Produces: `ProtocolEnvelope`, protocol version `1`, `status`, `sync`, `render`, `open`, and repository-aware `init`; all mutations return event-set digest and sync state.

- [ ] **Step 1: Write failing stable-envelope tests**

```python
def test_status_uses_versioned_protocol(cli_runner, configured_memory):
    response = cli_runner("status", "--format", "json")
    assert response.exit_code == 0
    assert response.json.keys() >= {
        "protocol_version", "operation_id", "status", "sync_state",
        "event_set_digest", "generation", "result",
    }
    assert response.json["protocol_version"] == 1


def test_register_persists_event_before_projection(cli_runner, fail_projection):
    response = cli_runner("register", "--input", "capability.json", "--format", "json")
    assert response.exit_code == 3
    assert response.json["code"] == "projection_failed"
    assert response.json["sync_state"] == "committed"
    assert event_file_count() == 1
```

- [ ] **Step 2: Verify RED and implement protocol envelopes**

Run: `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_cli.py -q -k 'versioned_protocol or persists_event_before_projection'`

Expected: FAIL because responses are raw dataclass JSON without protocol metadata.

```python
@dataclass(frozen=True)
class ProtocolEnvelope:
    protocol_version: int
    operation_id: str
    status: str
    sync_state: str
    event_set_digest: str | None
    generation: str | None
    result: object | None
    code: str | None = None
    message: str | None = None
    attempts: tuple[str, ...] = ()
```

Serialize one envelope on stdout for success and stderr for failure. Keep invalid input at exit `2`; configuration, authority, sync, projection, retrieval, rendering, and runtime blocks exit `3`. `pending_sync` is a successful local mutation state and must be visible in the envelope.

- [ ] **Step 3: Add and wire standalone commands**

Extend parsing with:

```text
init --repo <owner/name> --project-root <path> --format json
init --create-private [--name supermind-memory] --project-root <path> --format json
status --format json
sync --format json
render --format json
open --format json
migrate --repo <owner/repository> --legacy-data-home <path> --format json
resolve-conflict --input <json> --format json
```

`open` calls sync, validates rendering, resolves the stored HTTPS repository URL, and invokes `gh repo view <owner/name> --web` as an argument vector. It must never start a local process that listens on a socket.

`resolve-conflict` requires the entity type, entity ID, every current conflicting head ID, and the
complete chosen resulting payload. It creates a resolution event whose parents are exactly those
heads; omission or staleness blocks without changing authority.

- [ ] **Step 4: Route every service mutation through an event transaction**

Change register, record-use, unmet-demand observation/linking, discovery persistence, and lifecycle changes to create `AuthorityEvent` payloads and call `SyncCoordinator.mutate`. Remove direct authoritative table writes from public service paths. Read/search operations first replay pending local authority and assert projection/generation digests.

After those public paths pass, require `authority_mode="events-v1"` for service construction and
delete the temporary legacy-authority health branch introduced in Task 3. The old database remains
accessible only to the explicit migration reader, never to ordinary service or search construction.

- [ ] **Step 5: Add no-direct-write and command injection tests**

Spy on `CapabilityRepository.upsert_capability`, `append_evidence`, `append_relationship`, and demand writes; public service calls must reach them only from `project_authority`. Pass repository names, paths, labels, and source URIs containing shell metacharacters and assert recorded subprocess calls preserve them as single arguments and create no sentinel file. Add a conflict-resolution test proving a stale or incomplete head set fails and an exact head set restores the entity to search eligibility.

- [ ] **Step 6: Run CLI/service/workflow tests and commit**

```bash
uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_cli.py plugins/supermind/tests/integration/test_event_first_service.py plugins/supermind/tests/integration/test_service.py plugins/supermind/tests/e2e/test_login_reuse.py -q
git diff --check
git add plugins/supermind/src/supermind_memory/protocol.py plugins/supermind/src/supermind_memory/cli.py plugins/supermind/src/supermind_memory/service.py plugins/supermind/src/supermind_memory/workflow.py plugins/supermind/src/supermind_memory/config.py plugins/supermind/tests/integration/test_cli.py plugins/supermind/tests/integration/test_event_first_service.py
git commit -m "feat: expose distributed memory through the cli"
```

---

### Task 9: Extract the standalone package and reduce the plugin to bootstrap

**Files:**
- Create: `capability-memory/pyproject.toml`
- Create: `capability-memory/requirements.lock`
- Create: `capability-memory/uv.lock`
- Create: `plugins/supermind/tool.lock.json`
- Create: `plugins/supermind/vendor/supermind_capability_memory-0.2.0-py3-none-any.whl`
- Move: `plugins/supermind/src/` to `capability-memory/src/`
- Move: `plugins/supermind/tests/` to `capability-memory/tests/`
- Move: `plugins/supermind/evaluation/` to `capability-memory/evaluation/`
- Move: `plugins/supermind/schemas/` to `capability-memory/schemas/`
- Move: `plugins/supermind/model.lock.json` to `capability-memory/model.lock.json`
- Delete: `plugins/supermind/pyproject.toml`
- Delete: `plugins/supermind/requirements.lock`
- Delete: `plugins/supermind/uv.lock`
- Modify: `plugins/supermind/scripts/capability-memory`
- Modify: `plugins/supermind/skills/supermind/SKILL.md`
- Modify: `scripts/verify.sh`
- Test: `plugins/supermind/tests/test_launcher.py`

**Interfaces:**
- Consumes: immutable `tool.lock.json`, `uv`, Python 3.11–3.14, and the standalone console script.
- Produces: installed `supermind-memory`, protocol range `>=1,<2`, and a plugin launcher that never imports memory implementation modules.

- [ ] **Step 1: Add the standalone console-script package and move sources with history**

Set the package entry point:

```toml
[project.scripts]
supermind-memory = "supermind_memory.cli:main"
```

Use `git mv` for source, tests, evaluation data, schemas, and model lock. Move the package metadata to `capability-memory/pyproject.toml`, delete the old plugin lock files after generating the standalone `uv.lock` and `requirements.lock`, and update all test and data-path references from `plugins/supermind` to `capability-memory`.

- [ ] **Step 2: Write failing plugin isolation/bootstrap tests**

```python
def test_plugin_contains_no_memory_implementation(plugin_root):
    assert not (plugin_root / "src").exists()
    assert not (plugin_root / "pyproject.toml").exists()


def test_launcher_installs_exact_locked_wheel(fake_uv, plugin_launcher, tool_lock):
    result = plugin_launcher("status", "--format", "json")
    assert result.exit_code == 0
    assert fake_uv.installed_wheel.name == tool_lock["artifact"]
    assert sha256(fake_uv.installed_wheel) == tool_lock["sha256"]
    assert result.json["protocol_version"] in range(1, 2)
```

- [ ] **Step 3: Verify RED and implement pinned self-bootstrap**

Run: `uv run --project capability-memory pytest plugins/supermind/tests/test_launcher.py -q`

Expected: FAIL until the launcher resolves and installs the locked standalone tool.

Build the standalone wheel with `uv build --project capability-memory --wheel`, copy that exact artifact into `plugins/supermind/vendor/`, and record its SHA-256 in `tool.lock.json`. The launcher validates the lock shape and wheel hash, installs the wheel into a Supermind-owned versioned tool directory under the resolved Codex data home, writes an ownership marker atomically, and invokes only that installed executable. It may retry provisioning, but it must not execute plugin source or another `supermind-memory` from `PATH` as a fallback. A test-only absolute executable override is accepted only when `SUPERMIND_MEMORY_TESTING=1`.

- [ ] **Step 4: Update the Skill contract**

Keep mandatory `init` and `health`. Add initialization handling: when `init` returns `memory_repository_unconfigured`, ask the user to connect an existing private repo or create one, then call the chosen CLI form. State that all mutations and Explorer requests use the standalone CLI and that missing/incompatible CLI blocks without fallback.

- [ ] **Step 5: Update release verification and locks**

Validate both project manifests, the console script, schema JSON, exact expected files, plugin absence of implementation source, the wheel's lock digest and contents, frozen exports, launcher executable bit, protocol compatibility, secrets, and symlinks. Run the standalone full suite plus plugin launcher tests.

- [ ] **Step 6: Run both distribution gates and commit**

```bash
uv lock --check --project capability-memory
uv run --project capability-memory pytest capability-memory/tests plugins/supermind/tests -q
scripts/verify.sh
git diff --check
git add capability-memory plugins/supermind scripts/verify.sh
git commit -m "refactor: extract capability memory as a standalone cli"
```

Expected: the plugin tree has no Python memory implementation; all existing behavior passes from the standalone package.

---

### Task 10: Real self-bootstrap, second-checkout equivalence, and cutover

**Files:**
- Create: `capability-memory/tests/e2e/test_distributed_bootstrap.py`
- Create: `capability-memory/tests/e2e/test_no_daemon.py`
- Modify: `README.md`
- Modify: `plugins/supermind/.codex-plugin/plugin.json`
- Modify: `docs/product/2026-09-06-distributed-capability-memory-design.md`
- Modify: `scripts/verify.sh`

**Interfaces:**
- Consumes: the current real embedded store, a user-selected private GitHub repository at execution time, the standalone CLI, and the pinned plugin launcher.
- Produces: verified cutover evidence, a synchronized private memory repository, and user documentation for initialization/search/update/open/recovery.

- [ ] **Step 1: Write the cross-checkout acceptance test**

```python
def test_bootstrap_clone_rebuild_and_readme_are_equivalent(private_bare_remote, embedded_store):
    first = cli_home("first", embedded_store=embedded_store)
    first.run("migrate", "--repo", private_bare_remote.url)
    second = cli_home("second")
    second.run("init", "--repo", private_bare_remote.url)
    assert second.run_json("health")["status"] == "healthy"
    assert second.authority_digest == first.authority_digest
    assert second.known_search("手机号登录") == first.known_search("手机号登录")
    readme = second.checkout.joinpath("README.md").read_text()
    assert "<details>" in readme and "登录" in readme
```

- [ ] **Step 2: Add no-daemon process evidence**

Run every CLI command under a test that snapshots child processes and listening sockets before and after exit. Assert no surviving child PID and no new listening TCP/Unix socket owned by the command. The test must cover `init`, `sync`, `search`, `register`, `render`, and `open` with the opener replaced by a recording runner.

- [ ] **Step 3: Run the automated migration rehearsal**

```bash
uv run --project capability-memory pytest capability-memory/tests/e2e/test_distributed_bootstrap.py capability-memory/tests/e2e/test_no_daemon.py -q
```

Expected: a fresh second checkout rebuilds an equivalent healthy local index and README from event files only.

- [ ] **Step 4: Perform the real private-repository cutover**

Resolve the exact private repository selected during initialization, then run `supermind-memory migrate --repo "$SUPERMIND_MEMORY_REPOSITORY" --legacy-data-home "$SUPERMIND_LEGACY_DATA_HOME" --format json` with those task-specific variables set to the selected `owner/repository` and validated legacy data root. Record the returned source digest, event-set digest, entity counts, generation, rendered manifest digest, commit ID, remote repository ID, and `synced` state. Clone into a temporary second data home, rebuild, compare the same digests, execute the known login requirement search, and verify the root README on GitHub contains balanced nested category/capability folds.

Do not delete the old embedded store. Mark it read-only in the migration journal and retain its exact path until one later release has passed. The plugin must already route exclusively to the standalone CLI after this step.

- [ ] **Step 5: Update product-facing documentation and manifest copy**

README usage must state:

```text
Capability Memory is an independent local Python CLI. During first use, Supermind connects an
existing private GitHub repository or creates one. Updates are committed locally and synchronized
incrementally; LanceDB and embeddings stay local. “打开能力库” synchronizes and opens the private
repository, whose root README is the categorized, collapsible browser.
```

Update plugin long description/capabilities without presenting GitHub as a hosted database. Mark the design `Approved and implemented` only after the real cutover evidence passes.

- [ ] **Step 6: Run the complete release gate**

```bash
uv run --project capability-memory pytest capability-memory/tests plugins/supermind/tests -q
uv run --project capability-memory pytest capability-memory/tests/e2e/test_login_reuse.py capability-memory/tests/e2e/test_distributed_bootstrap.py -q
scripts/verify.sh
git diff --check
git status --short
```

Expected: all tests PASS, verification prints `Supermind release tree verified.`, and only intended Task 10 files are modified.

- [ ] **Step 7: Commit the verified cutover**

```bash
git add README.md capability-memory/tests/e2e plugins/supermind/.codex-plugin/plugin.json docs/product/2026-09-06-distributed-capability-memory-design.md scripts/verify.sh
git commit -m "feat: complete distributed capability memory bootstrap"
```

---

## Final review gate

After Task 10, inspect the complete branch against the approved spec and require all of the following before delivery:

- the plugin contains no memory writer or embedded fallback;
- every public mutation produces one or more immutable events before projection;
- two checkouts converge on the same event-set and materialized digests;
- same-entity concurrency is blocked as an explicit conflict;
- offline writes are visibly `pending_sync` and later synchronize;
- private visibility is independently verified on initialization and mutation;
- no credential, model, vector, LanceDB file, runtime, or source artifact appears in the remote repository;
- the GitHub root README directly exposes all six categories and nested capability folds;
- generated views match their manifest and are not authoritative input;
- complete hybrid retrieval and existing hardening tests still pass; and
- process/socket evidence proves the implementation is daemonless.
