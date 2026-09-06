# Task 7 implementer report

- Baseline: `b7e0fc1d3f0af2fe0468567cedf6df258efa2a87`
- Commit: `6aef5c3` (`feat: orchestrate capability discovery and reuse`)
- Round 1 follow-up: `fix: harden capability memory transactions`

## Delivered

- Added the `CapabilityMemory` orchestration surface for initialize, discover, refresh, search,
  evaluate, register, record-use, get, rebuild, and health-check operations.
- Every search and mutation enters through the fail-closed health gate. An unhealthy report is
  converted to `CapabilityMemoryBlocked`; it is never returned as an empty search result.
- Registration generates its embedding outside the writer lock, then persists the capability,
  effective evidence, and one lifecycle event under one lock without re-entering repository locks.
- Record-use validates project/source/economics, appends one outcome evidence record, recomputes
  evidence-backed net value and lifecycle, updates the capability, and appends exactly one event.
- Discovery scans and embeds outside the persistence lock, keeps Task 6 source identities, preserves
  evaluated state for unchanged observations, and records deterministic invalidation evidence for
  changed or removed sources.
- Registration rejects non-positive net value, invalid evidence metrics, duplicate/global evidence
  ID conflicts, and attempts to revive an authoritatively retired capability.

## TDD evidence

The service integration test was created before `service.py`. The prescribed initial run failed at
collection with `ModuleNotFoundError: supermind_memory.service`. The login lifecycle and health-block
tests then passed after the minimum service path was implemented.

Further tests were added before each corresponding fix and observed failing for:

- absent discover/refresh/get and rebuild/health operations;
- a replaced health manager being bypassed by initialize;
- unhealthy reports being allowed through instead of blocking;
- re-registration discarding persisted verification evidence;
- unchanged refresh resetting a verified capability to observed;
- empty source URIs being treated as the current directory;
- retired capabilities being revived by stale input;
- post-rebuild unhealthy state being returned instead of blocked;
- removed/changed sources not being invalidated;
- globally conflicting evidence IDs being silently skipped after promotion;
- NaN reuse economics reaching lifecycle promotion; and
- non-local sources being considered live without affirmative availability evidence.

Each focused red was rerun after its minimal production fix and passed.

## Review

An independent read-only review identified source invalidation, global evidence-ID collision,
non-finite economics, fail-open remote-source liveness, and event-reason issues. All findings were
addressed before the final verification and commit.

## Verification

- `uv run --project plugins/supermind pytest plugins/supermind/tests/integration/test_service.py -q`
  — 24 passed
- `uv run --project plugins/supermind pytest plugins/supermind/tests/unit plugins/supermind/tests/integration/test_search.py plugins/supermind/tests/integration/test_service.py -q`
  — 69 passed
- `uv run --project plugins/supermind pytest plugins/supermind/tests -q -m 'not model'`
  — 172 passed, 1 deselected (exit 0 on the final standalone run)
- `git diff --cached --check` — passed before commit
- `uv run --project plugins/supermind python -m compileall -q plugins/supermind/src/supermind_memory`
  — passed

One earlier combined verification invocation completed all 172 tests but hit an intermittent
LanceDB/libc++ mutex exception during interpreter teardown. The required full non-model suite was
immediately rerun standalone against the same tree and exited cleanly with the result above.

## Official review round 1 fixes

- Added an fsync-backed transaction journal, complete database/DML snapshot, commit marker,
  byte-for-byte rollback, and reopen recovery for capability/evidence/event mutations.
- Health now validates every authoritative Evidence numeric field. Record-use repeats the full
  historical validation under the writer lock and rejects non-finite or non-positive aggregates
  before its first write.
- Search is bound to a checked generation plus a digest of all authoritative tables. It verifies
  both after retrieval, discards raced results, repairs, and retries at most three times. Complete
  no-match results retain their checked generation.
- Unchanged discovery records are returned exactly as persisted, including their vector and all
  timestamps, with no DML or authoritative fingerprint change.
- Generated audit identifiers now include deterministic operation epochs and source/revision
  identity. Every generated audit append checks global ID collisions, failed attempts reuse the
  same ID after rollback, and later real operations receive a new ID.
- Repository and service context managers now own deterministic close. Generation readers are
  reused. Table borrows delay connection close, and the package shutdown owner closes registered
  connections before stopping and joining LanceDB's process-global background loop. Forked
  children clear inherited connection ownership.

Round 1 TDD reds covered transaction rollback, reopen recovery, invalid Evidence values, locked
historical revalidation, non-positive reuse aggregate, generation-preserving no-match, a concurrent
search barrier, and unchanged discovery DML/vector preservation.

## Round 1 verification

- Focused health + service: 73 passed per run, 20 consecutive runs, every process exit 0.
- Full non-model: 187 passed and 1 deselected per run, 20 consecutive runs, every process exit 0.
- `git diff --check` — passed.
- `uv run python -m compileall -q src tests` from `plugins/supermind` — passed.

Before explicit lifecycle shutdown, all Python assertions passed but LanceDB 0.38.0 intermittently
aborted during interpreter teardown with `recursive_mutex lock failed`. LanceDB's sync wrapper has
no close method, while its owned `AsyncConnection` does; its global background event loop has no
shutdown hook. The final implementation closes the owned async connections and deterministically
stops/joins that loop. The two 20-run gates above were performed only after this fix.

## Official review round 2 fixes

- Removed the non-positive aggregate rejection from record-use. A valid low-value or failed reuse
  now atomically appends its Evidence, persists the finite aggregate (including zero/negative),
  applies lifecycle degradation rules, and appends exactly one Event.
- Added repository-owned monotonic Evidence commit sequences in side metadata without changing the
  frozen Evidence dataclass contract. Registration, record-use, invalidation, source availability,
  lifecycle derivation, and last-verification selection now use commit order rather than caller
  timestamps. Sequence allocation is covered by the same rollback journal as the Evidence append.
- Replaced weak table tokens with strong, reusable LanceTable handles. Repository close clears all
  table and generation-reader references before closing its AsyncConnection and propagates close
  errors. Filesystem inode markers reopen cached handles after an external table replacement.
- Added explicit `shutdown_repository_runtime()` and `repository_runtime_state()` ownership APIs.
  Ordinary repository/service close leaves LanceDB's process-global loop available for later
  repositories; only the explicit process owner shutdown closes registered repositories, the
  embedding executor, and the background loop. Forked children reset inherited ownership.

Round 2 TDD reds covered negative aggregate persistence, future-dated Evidence ordering for both
source availability and lifecycle, table-handle reuse, independent runtime shutdown, and atomic
rollback of commit-sequence allocation.

## Round 2 verification

- Focused repository + health + service: 92 passed.
- Full non-model: 193 passed, 1 deselected.
- Full non-model stability: 5 consecutive runs, all exit 0.
- Production-style subprocess runtime lifecycle (no pytest/conftest inside the child): 20
  consecutive natural exits, all exit 0. Each child verified repository close left registry/table
  counts at zero while the runtime remained reusable, then explicit shutdown stopped and closed the
  LanceDB loop.
- `git diff --check` and `uv run python -m compileall -q src tests` — passed.

## Official review round 3 fix

- Repository close now invokes `LanceTable._table.close()` for every strongly cached real table
  handle and verifies it is closed before clearing table caches or closing the connection.
- A native table-close failure propagates immediately. The repository, table cache, generation
  reader cache, native connection, and global registry remain owned so the same close can be safely
  retried. Runtime shutdown therefore cannot report zero resources or stop the process-global loop
  after a failed table close.
- Generation readers use the same ordered close protocol. Table replacement and external inode
  refresh also close the previous native table before evicting its cached wrapper.
- The natural-exit subprocess retains a real LanceTable wrapper beyond repository creation and
  verifies atexit closes the native table, then the connection, then stops and closes the global
  LanceDB loop. Marker-file validation makes atexit assertion failures visible to the parent even
  though Python normally only prints atexit exceptions.

Round 3 TDD reds directly demonstrated the old native table remained open after repository close,
native table-close failures were swallowed by cache eviction, generation-reader state was lost,
and the atexit verification marker was absent.

## Round 3 verification

- Focused repository + health + service: 96 passed.
- Full non-model: 197 passed, 1 deselected.
- Full non-model stability: 5 consecutive runs, all exit 0.
- Explicit and natural-atexit production-style subprocess lifecycle tests: 20 consecutive runs,
  all exit 0.
- `git diff --check` and `uv run python -m compileall -q src tests` — passed.
