from __future__ import annotations

import json
import multiprocessing
import os
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pytest
from conftest import KeywordEmbeddingProvider
from filelock import FileLock

import supermind_memory.health as health_module
from supermind_memory.bootstrap import Bootstrap
from supermind_memory.config import MemoryPaths
from supermind_memory.decision import ReuseDecisionEngine
from supermind_memory.health import HealthManager
from supermind_memory.repository import CapabilityRepository
from supermind_memory.schema import EMBEDDING_DIMENSION, TABLE_SCHEMAS
from supermind_memory.search import CapabilitySearch
from supermind_memory.types import (
    ArtifactType,
    Capability,
    CapabilityMemoryBlocked,
    Event,
    Evidence,
    Lifecycle,
    Relationship,
    RequirementProfile,
    SearchStatus,
)


class DeterministicEmbeddingProvider(KeywordEmbeddingProvider):
    """Use the shared deterministic semantic fixture for the real quality gate."""


class PermanentlyFailingEmbeddingProvider:
    def __init__(self):
        self.attempts = 0

    def embed_documents(self, texts):
        raise RuntimeError("embedding model is unavailable")

    def embed_query(self, text):
        self.attempts += 1
        raise RuntimeError("embedding model is unavailable")


class RecordingEmbeddingProvider(DeterministicEmbeddingProvider):
    def __init__(self):
        self.documents = ()

    def embed_documents(self, texts):
        self.documents += tuple(texts)
        return super().embed_documents(texts)


@pytest.fixture
def paths(tmp_path) -> MemoryPaths:
    return MemoryPaths.from_codex_home(tmp_path)


@pytest.fixture
def repo(paths) -> CapabilityRepository:
    return CapabilityRepository.open(paths.database)


@pytest.fixture
def health(paths, repo) -> HealthManager:
    return HealthManager(paths, repo, DeterministicEmbeddingProvider())


@pytest.fixture
def bootstrap(paths, repo, health) -> Bootstrap:
    return Bootstrap(paths, repo, DeterministicEmbeddingProvider(), health_manager=health)


def test_missing_store_is_created_and_activated(bootstrap, paths):
    report = bootstrap.initialize(project_root=Path.cwd())

    assert report.healthy is True
    assert report.active_generation is not None
    assert (paths.generations / report.active_generation).is_dir()
    assert _active_generation(paths) == report.active_generation


def test_event_mode_binds_generation_and_rejects_non_capability_drift(paths, repo):
    from supermind_memory.projection import project_authority
    from supermind_memory.replay import replay

    repo.initialize()
    result = replay(())
    project_authority(result, repo, DeterministicEmbeddingProvider())
    manager = HealthManager(paths, repo, DeterministicEmbeddingProvider(),
                            authority_mode="events-v1", expected_authority_digest=result.digest)
    report = manager.ensure_healthy()
    manifest = json.loads((paths.generations / report.active_generation / "manifest.json").read_text())
    assert manifest["authority_event_set_digest"] == result.digest
    assert manifest["materialized_digest"] == repo.get_metadata("authority_materialized_digest")
    repo.set_metadata("unauthorized", {"change": True})
    assert any("projection_mismatch" in failure for failure in manager.check().failures)
    with pytest.raises(CapabilityMemoryBlocked, match="projection_mismatch"):
        manager.ensure_healthy()
    assert _active_generation(paths) == report.active_generation
    with pytest.raises(RuntimeError, match="projection_mismatch"):
        repo.active_generation()


def test_event_mode_rejects_wrong_replay_digest(paths, repo):
    from supermind_memory.projection import project_authority
    from supermind_memory.replay import replay

    repo.initialize()
    result = replay(())
    project_authority(result, repo, DeterministicEmbeddingProvider())
    manager = HealthManager(paths, repo, DeterministicEmbeddingProvider(),
                            authority_mode="events-v1", expected_authority_digest="f" * 64)
    with pytest.raises(CapabilityMemoryBlocked, match="authority_digest_mismatch"):
        manager.ensure_healthy()
    assert not (paths.root / "active-generation.json").exists()


def test_event_mode_repairs_generation_with_wrong_authority_digest(paths, repo):
    from supermind_memory.projection import project_authority
    from supermind_memory.replay import replay

    repo.initialize()
    result = replay(())
    project_authority(result, repo, DeterministicEmbeddingProvider())
    manager = HealthManager(paths, repo, DeterministicEmbeddingProvider(),
                            authority_mode="events-v1", expected_authority_digest=result.digest)
    initial = manager.ensure_healthy()
    path = paths.generations / initial.active_generation / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["authority_event_set_digest"] = "f" * 64
    path.write_text(json.dumps(manifest))
    assert any("authority_generation_stale" in failure for failure in manager.check().failures)
    repaired = manager.ensure_healthy()
    assert repaired.healthy
    assert repaired.active_generation != initial.active_generation


def test_new_event_set_invalidates_generation_even_with_identical_capabilities(paths, repo):
    from dataclasses import asdict
    from supermind_memory.projection import project_authority
    from supermind_memory.replay import replay
    from test_projection import authority

    repo.initialize()
    base = authority("capability", "login", asdict(_capability("login")))
    original = replay((base,))
    provider = DeterministicEmbeddingProvider()
    project_authority(original, repo, provider)
    manager = HealthManager(paths, repo, provider, authority_mode="events-v1", expected_authority_digest=original.digest)
    initial = manager.ensure_healthy()
    update = authority("capability", "login", asdict(_capability("login")),
                       event_id="updated", parents=(base.event_id,), operation="updated")
    current = replay((base, update))
    project_authority(current, repo, provider)
    manager = HealthManager(paths, repo, provider, authority_mode="events-v1", expected_authority_digest=current.digest)
    assert any("authority_generation_stale" in failure for failure in manager.check().failures)
    with pytest.raises(RuntimeError, match="authority_generation_stale"):
        repo.active_generation()
    rebuilt = manager.ensure_healthy()
    assert rebuilt.active_generation != initial.active_generation
    assert rebuilt.healthy


def test_ensure_healthy_repairs_a_completely_missing_store(paths, repo):
    health = HealthManager(paths, repo, DeterministicEmbeddingProvider())

    report = health.ensure_healthy()

    assert report.healthy is True
    assert report.repairs == ("initialize_store",)


def test_damaged_derived_index_is_rebuilt_without_losing_records(
    bootstrap, health, paths, repo
):
    bootstrap.initialize(project_root=Path.cwd())
    capability = _capability("capability-preserved")
    repo.upsert_capability(capability, [0.5] * EMBEDDING_DIMENSION)
    indexed = health.ensure_healthy()
    damaged_generation = indexed.active_generation
    table = _generation_repo(paths, damaged_generation)._table("capabilities")
    table.update(where="id = 'capability-preserved'", values={"search_text": "damaged"})
    index = next(index for index in table.list_indices() if index.index_type == "FTS")
    table.drop_index(index.name)

    report = health.ensure_healthy()

    assert report.repairs == ("rebuild_search_indexes",)
    assert report.active_generation != damaged_generation
    assert repo.get_capability(capability.id) == capability
    active_table = _generation_repo(paths, report.active_generation)._table("capabilities")
    assert any(index.index_type == "FTS" for index in active_table.list_indices())
    assert active_table.search(
        "OAuth", query_type="fts", fts_columns="search_text"
    ).limit(1).to_list()[0]["id"] == capability.id
    assert active_table.to_arrow().to_pylist()[0]["embedding_generation"] == report.active_generation


def test_incompatible_database_schema_is_migrated_without_losing_records(
    bootstrap, health, paths, repo
):
    bootstrap.initialize(project_root=Path.cwd())
    capability = _capability("capability-migrated")
    repo.upsert_capability(capability, [0.25] * EMBEDDING_DIMENSION)
    evidence = _evidence(capability.id)
    relationship = _relationship(capability.id)
    event = _event(capability.id)
    repo.append_evidence(evidence)
    repo.append_relationship(relationship)
    repo.append_event(event)
    repo.set_metadata("source", {"revision": 1})
    health.ensure_healthy()
    rows = repo._table("capabilities").to_arrow().to_pylist()
    old_schema = TABLE_SCHEMAS["capabilities"].append(
        pa.field("legacy_index_hint", pa.string(), nullable=True)
    )
    repo._database.drop_table("capabilities")
    repo._database.create_table(
        "capabilities",
        data=pa.Table.from_pylist(
            [{**row, "legacy_index_hint": "v0"} for row in rows],
            schema=old_schema,
        ),
    )

    report = health.ensure_healthy()

    assert report.healthy is True
    assert report.repairs == ("migrate_schema",)
    assert repo.get_capability(capability.id) == capability
    assert repo.list_evidence(capability.id) == (evidence,)
    assert repo.list_relationships(capability.id) == (relationship,)
    assert repo.list_events(capability.id) == (event,)
    assert {row["key"]: row["value"] for row in repo._rows("metadata")}["source"] == (
        '{"revision":1}'
    )
    assert repo._table("capabilities").schema.equals(
        TABLE_SCHEMAS["capabilities"], check_metadata=False
    )


def test_schema_migration_overwrites_each_table_with_one_complete_arrow_commit(tmp_path):
    calls = []

    class RecordingDatabase:
        def create_table(self, *args, **kwargs):
            calls.append((args, kwargs))
            return object()

    repository = CapabilityRepository(
        RecordingDatabase(),
        tmp_path / "database",
        tmp_path / "locks" / "writer.lock",
    )
    rows = [{"key": "source", "value": '{"revision":1}'}]

    repository._overwrite_table_unlocked("metadata", TABLE_SCHEMAS["metadata"], rows)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("metadata",)
    assert kwargs["mode"] == "overwrite"
    assert kwargs["data"].schema.equals(TABLE_SCHEMAS["metadata"])
    assert kwargs["data"].to_pylist() == rows
    assert "schema" not in kwargs


def test_interrupted_migration_is_recovered_from_fsynced_five_table_journal(
    bootstrap, health, paths, repo
):
    bootstrap.initialize(project_root=Path.cwd())
    expected = _seed_every_authoritative_table(repo, "interrupted")
    health.ensure_healthy()
    for table_name in TABLE_SCHEMAS:
        _add_legacy_column(repo, table_name)

    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_interrupt_migration_after_first_commit,
        args=(str(paths.database),),
    )
    process.start()
    process.join(timeout=30)

    assert process.exitcode == 23
    assert (paths.runtime / "migration-journal.json").is_file()

    report = health.ensure_healthy()

    assert report.healthy is True
    assert not (paths.runtime / "migration-journal.json").exists()
    assert _authoritative_rows(repo) == expected
    for table_name, expected_schema in TABLE_SCHEMAS.items():
        assert repo._table(table_name).schema.equals(expected_schema, check_metadata=False)


def test_search_keeps_reading_old_generation_while_migration_is_committing(
    bootstrap, health, paths, repo
):
    bootstrap.initialize(project_root=Path.cwd())
    repo.upsert_capability(_capability("search-during-migration"), [0.0] * EMBEDDING_DIMENSION)
    indexed = health.ensure_healthy()
    _add_legacy_column(repo, "capabilities")
    _add_legacy_column(repo, "evidence")
    context = multiprocessing.get_context("spawn")
    first_commit = context.Event()
    resume = context.Event()
    process = context.Process(
        target=_pause_migration_after_first_commit,
        args=(str(paths.database), first_commit, resume),
    )
    process.start()
    assert first_commit.wait(timeout=15)

    result = CapabilitySearch(repo, DeterministicEmbeddingProvider()).search(
        RequirementProfile(
            id="requirement-migration",
            project_id="project-a",
            intent="OAuth login",
        )
    )

    assert result.status is SearchStatus.COMPLETE
    assert result.generation == indexed.active_generation
    assert result.matches[0].capability_id == "search-during-migration"
    resume.set()
    process.join(timeout=30)
    assert process.exitcode == 0


def test_unrecoverable_model_failure_retries_three_repairs_and_blocks(paths, repo):
    embeddings = PermanentlyFailingEmbeddingProvider()
    repo.initialize()
    health = HealthManager(paths, repo, embeddings)

    with pytest.raises(CapabilityMemoryBlocked) as error:
        health.ensure_healthy()

    assert error.value.code == "embedding_unavailable"
    assert len(error.value.attempts) == 3
    assert all("embedding model is unavailable" in attempt for attempt in error.value.attempts)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("confidence", float("nan")),
        ("confidence", -0.1),
        ("integration_effort", float("inf")),
        ("integration_effort", -0.1),
        ("benefit", float("nan")),
        ("benefit", -0.1),
        ("failure_risk", float("inf")),
        ("failure_risk", -0.1),
        ("metric_value", float("nan")),
    ],
)
def test_health_rejects_invalid_authoritative_evidence_numbers(
    bootstrap,
    health,
    repo,
    field,
    value,
):
    bootstrap.initialize(project_root=Path.cwd())
    evidence = _evidence("invalid-evidence")
    repo.append_evidence(evidence)
    rows = repo._table("evidence").to_arrow().to_pylist()
    rows[0][field] = value
    repo._overwrite_table_unlocked(
        "evidence",
        TABLE_SCHEMAS["evidence"],
        rows,
    )

    report = health.check()

    assert report.healthy is False
    assert any("source_unavailable" in failure for failure in report.failures)
    assert any("evidence" in failure for failure in report.failures)


def test_failed_repair_never_replaces_the_active_generation(bootstrap, paths, repo):
    repo.initialize()
    repo.upsert_capability(_capability("stable"), [0.5] * EMBEDDING_DIMENSION)
    first = bootstrap.initialize(project_root=Path.cwd())
    previous_pointer = (paths.root / "active-generation.json").read_bytes()
    previous_files = _tree_contents(paths.generations / first.active_generation)

    class ActivationFailingHealthManager(HealthManager):
        def _activate_generation(self, generation):
            raise RuntimeError("activation interrupted")

    failing = ActivationFailingHealthManager(paths, repo, DeterministicEmbeddingProvider())

    with pytest.raises(CapabilityMemoryBlocked):
        failing.rebuild_indexes()

    assert (paths.root / "active-generation.json").read_bytes() == previous_pointer
    assert _active_generation(paths) == first.active_generation
    assert _tree_contents(paths.generations / first.active_generation) == previous_files
    assert tuple(path.name for path in paths.generations.iterdir()) == (first.active_generation,)


def test_search_reads_the_active_generation_and_reports_it(bootstrap, paths, repo):
    repo.initialize()
    repo.upsert_capability(_capability("active-search"), [0.0] * EMBEDDING_DIMENSION)
    report = bootstrap.initialize(project_root=Path.cwd())

    result = CapabilitySearch(repo, DeterministicEmbeddingProvider()).search(
        RequirementProfile(
            id="requirement-1",
            project_id="project-a",
            intent="OAuth login",
        )
    )

    assert result.status is SearchStatus.COMPLETE
    assert result.matches[0].capability_id == "active-search"
    assert result.generation == report.active_generation


def test_health_managed_search_without_active_generation_fails_closed(paths, repo):
    repo.initialize()
    repo.upsert_capability(_capability("not-activated"), [0.0] * EMBEDDING_DIMENSION)
    HealthManager(paths, repo, DeterministicEmbeddingProvider())

    result = CapabilitySearch(repo, DeterministicEmbeddingProvider()).search(
        RequirementProfile(id="requirement-1", project_id="project-a", intent="OAuth login")
    )

    assert result.status is SearchStatus.FAILED
    assert result.matches == ()


def test_search_fails_closed_when_authoritative_source_outgrows_active_generation(
    bootstrap, repo
):
    repo.initialize()
    repo.upsert_capability(_capability("activated"), [0.0] * EMBEDDING_DIMENSION)
    bootstrap.initialize(project_root=Path.cwd())
    repo.upsert_capability(_capability("not-indexed"), [0.0] * EMBEDDING_DIMENSION)

    result = CapabilitySearch(repo, DeterministicEmbeddingProvider()).search(
        RequirementProfile(id="requirement-1", project_id="project-a", intent="OAuth login")
    )

    assert result.status is SearchStatus.FAILED
    assert result.matches == ()


def test_missing_authoritative_table_blocks_without_superseding_active_generation(
    bootstrap, health, paths, repo
):
    repo.initialize()
    repo.upsert_capability(_capability("only-authority"), [0.0] * EMBEDDING_DIMENSION)
    first = bootstrap.initialize(project_root=Path.cwd())
    pointer = (paths.root / "active-generation.json").read_bytes()
    repo._database.drop_table("capabilities")

    with pytest.raises(CapabilityMemoryBlocked) as error:
        health.ensure_healthy()

    assert error.value.code == "authoritative_store_corrupt"
    assert (paths.root / "active-generation.json").read_bytes() == pointer
    assert _active_generation(paths) == first.active_generation


@pytest.mark.parametrize("missing", [
    ("requirement_events",), ("requirement_observations",),
    ("requirement_observations", "requirement_events"),
])
def test_v2_store_missing_requirement_events_is_corrupt(bootstrap, health, paths, repo, missing):
    bootstrap.initialize(project_root=Path.cwd())
    repo.set_metadata("authoritative_schema_version", 2)
    health.ensure_healthy()
    pointer = (paths.root / "active-generation.json").read_bytes()
    for name in missing:
        repo._database.drop_table(name)
    before = {name: repo._rows(name) for name in repo._database.list_tables().tables}

    report = health.check()

    assert report.healthy is False
    assert any("authoritative_store_corrupt" in item for item in report.failures)
    with pytest.raises(CapabilityMemoryBlocked) as error:
        health.ensure_healthy()
    assert error.value.code == "authoritative_store_corrupt"
    assert (paths.root / "active-generation.json").read_bytes() == pointer
    assert {name: repo._rows(name) for name in repo._database.list_tables().tables} == before


@pytest.mark.parametrize("fault", ["creation", "link", "wrong-capability"])
def test_v2_demand_history_requires_creation_and_matching_link_events(
    bootstrap, health, paths, repo, fault,
):
    bootstrap.initialize(project_root=Path.cwd())
    repo.upsert_capability(_capability("implemented-login"), [0.0] * EMBEDDING_DIMENSION)
    _seed_linked_demand(repo)
    health.ensure_healthy()
    pointer = (paths.root / "active-generation.json").read_bytes()
    if fault == "wrong-capability":
        repo._table("requirement_events").update(
            where="id = 'demand-linked'", values={"capability_id": "another-implementation"},
        )
    else:
        missing_id = "demand-observed" if fault == "creation" else "demand-linked"
        repo._table("requirement_events").delete(f"id = '{missing_id}'")
    before = {name: repo._rows(name) for name in TABLE_SCHEMAS}

    report = health.check()

    assert report.healthy is False
    assert any("authoritative_store_corrupt" in item for item in report.failures)
    with pytest.raises(CapabilityMemoryBlocked) as error:
        health.ensure_healthy()
    assert error.value.code == "authoritative_store_corrupt"
    assert (paths.root / "active-generation.json").read_bytes() == pointer
    assert {name: repo._rows(name) for name in TABLE_SCHEMAS} == before


def test_pre_marker_v2_missing_creation_event_cannot_be_marked_or_repaired(
    bootstrap, health, repo,
):
    bootstrap.initialize(project_root=Path.cwd())
    _seed_linked_demand(repo)
    repo._table("metadata").delete("key = 'authoritative_schema_version'")
    repo._table("requirement_events").delete("id = 'demand-observed'")
    before = {name: repo._rows(name) for name in TABLE_SCHEMAS}

    with pytest.raises(CapabilityMemoryBlocked) as error:
        health.ensure_healthy()

    assert error.value.code == "authoritative_store_corrupt"
    assert repo.get_metadata("authoritative_schema_version") is None
    assert {name: repo._rows(name) for name in TABLE_SCHEMAS} == before


def _seed_linked_demand(repo, capability_id="implemented-login"):
    repo._table("requirement_observations").add([{
        "id": "demand-login", "requirement": json.dumps({
            "id": "required-login", "project_id": "project-a", "intent": "OAuth login",
        }), "status": "linked", "observed_at": "2026-09-06T00:00:00Z",
        "linked_capability_id": capability_id,
    }])
    repo._table("requirement_events").add([
        {"id": "demand-observed", "observation_id": "demand-login", "event_type": "unmet_observed",
         "occurred_at": "2026-09-06T00:00:00Z", "capability_id": None, "reason": "no reusable login"},
        {"id": "demand-linked", "observation_id": "demand-login", "event_type": "implementation_linked",
         "occurred_at": "2026-09-06T01:00:00Z", "capability_id": capability_id, "reason": "verified login"},
    ])


def test_corrupt_authoritative_audit_record_blocks_without_replacing_pointer(
    bootstrap, health, paths, repo
):
    repo.initialize()
    repo.upsert_capability(_capability("audit-owner"), [0.0] * EMBEDDING_DIMENSION)
    repo.append_relationship(_relationship("audit-owner"))
    first = bootstrap.initialize(project_root=Path.cwd())
    pointer = (paths.root / "active-generation.json").read_bytes()
    repo._table("relationships").update(
        where="id = 'relationship-1'",
        values={"compatibility": "not-json"},
    )

    with pytest.raises(CapabilityMemoryBlocked) as error:
        health.ensure_healthy()

    assert error.value.code == "source_unavailable"
    assert (paths.root / "active-generation.json").read_bytes() == pointer
    assert _active_generation(paths) == first.active_generation


def test_multi_table_migration_rolls_back_every_table_when_a_later_table_fails(
    bootstrap, health, repo, monkeypatch
):
    bootstrap.initialize(project_root=Path.cwd())
    repo.upsert_capability(_capability("rollback"), [0.0] * EMBEDDING_DIMENSION)
    repo.append_evidence(_evidence("rollback"))
    health.ensure_healthy()
    _add_legacy_column(repo, "capabilities")
    _add_legacy_column(repo, "evidence")
    original_capability_schema = repo._table("capabilities").schema
    original_evidence_schema = repo._table("evidence").schema
    real_overwrite = repo._overwrite_table_unlocked

    def fail_evidence(table_name, schema, rows):
        if table_name == "evidence" and schema.equals(
            TABLE_SCHEMAS["evidence"], check_metadata=False
        ):
            raise RuntimeError("later table migration failed")
        return real_overwrite(table_name, schema, rows)

    monkeypatch.setattr(repo, "_overwrite_table_unlocked", fail_evidence)

    with pytest.raises(CapabilityMemoryBlocked):
        health.ensure_healthy()

    assert repo._table("capabilities").schema.equals(
        original_capability_schema, check_metadata=False
    )
    assert repo._table("evidence").schema.equals(
        original_evidence_schema, check_metadata=False
    )


def test_failed_model_gate_does_not_invoke_any_index_search(
    bootstrap, paths, repo, monkeypatch
):
    bootstrap.initialize(project_root=Path.cwd())
    capability_table = repo._table("capabilities")
    search_calls = 0

    class SearchTrackingTable:
        def __getattr__(self, name):
            return getattr(capability_table, name)

        def search(self, *args, **kwargs):
            nonlocal search_calls
            search_calls += 1
            return capability_table.search(*args, **kwargs)

    original_table = repo._table
    monkeypatch.setattr(
        repo,
        "_table",
        lambda name: SearchTrackingTable() if name == "capabilities" else original_table(name),
    )
    failing = HealthManager(paths, repo, PermanentlyFailingEmbeddingProvider())

    with pytest.raises(CapabilityMemoryBlocked):
        failing.ensure_healthy()

    assert search_calls == 0


def test_failed_generation_manifest_does_not_validate_or_search_indexes(
    bootstrap, paths, repo
):
    first = bootstrap.initialize(project_root=Path.cwd())
    manifest_path = paths.generations / first.active_generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checks"]["fts"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    class ArtifactTrackingHealthManager(HealthManager):
        artifact_checks = 0

        def _validate_generation_artifact(self, generation, failures):
            self.artifact_checks += 1
            return super()._validate_generation_artifact(generation, failures)

    health = ArtifactTrackingHealthManager(paths, repo, DeterministicEmbeddingProvider())

    report = health.check()

    assert report.healthy is False
    assert health.artifact_checks == 0


def test_finite_wrong_vector_digest_is_unhealthy_and_repaired(bootstrap, health, paths, repo):
    bootstrap.initialize(project_root=Path.cwd())
    repo.upsert_capability(_capability("wrong-vector"), [0.0] * EMBEDDING_DIMENSION)
    indexed = health.ensure_healthy()
    table = _generation_repo(paths, indexed.active_generation)._table("capabilities")
    table.update(where="id = 'wrong-vector'", values={"vector": [0.25] * EMBEDDING_DIMENSION})

    unhealthy = health.check()
    repaired = health.ensure_healthy()

    assert unhealthy.healthy is False
    assert any("artifact_digest" in failure for failure in unhealthy.failures)
    assert repaired.active_generation != indexed.active_generation
    repaired_row = _generation_repo(paths, repaired.active_generation)._rows("capabilities")[0]
    expected_text = repo._capability_row(_capability("wrong-vector"), [0.0] * EMBEDDING_DIMENSION)["search_text"]
    expected_vector = health.embedding_provider.embed_documents([expected_text])[0]
    assert repaired_row["vector"] == pytest.approx(expected_vector)


def test_fts_health_probe_requires_a_known_record_to_be_returned(
    bootstrap, health, paths, repo, monkeypatch
):
    bootstrap.initialize(project_root=Path.cwd())
    repo.upsert_capability(_capability("known-fts-record"), [0.0] * EMBEDDING_DIMENSION)
    indexed = health.ensure_healthy()
    generation_database = paths.generations / indexed.active_generation / "database"
    real_open = CapabilityRepository.open

    class EmptyQuery:
        def limit(self, _limit):
            return self

        def to_list(self):
            return []

    class EmptyFtsTable:
        def __init__(self, table):
            self._wrapped = table

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def search(self, *args, **kwargs):
            if kwargs.get("query_type") == "fts":
                return EmptyQuery()
            return self._wrapped.search(*args, **kwargs)

    def open_with_empty_fts(database_path, writer_lock_path=None):
        opened = real_open(database_path, writer_lock_path=writer_lock_path)
        if Path(database_path) == generation_database:
            real_table = opened._table
            opened._table = lambda name: (
                EmptyFtsTable(real_table(name)) if name == "capabilities" else real_table(name)
            )
        return opened

    monkeypatch.setattr(CapabilityRepository, "open", staticmethod(open_with_empty_fts))

    report = health.check()

    assert report.healthy is False
    assert any("known record" in failure for failure in report.failures)


def test_fts_probe_uses_a_deterministic_indexable_token_for_punctuation_name(
    bootstrap, health, repo
):
    bootstrap.initialize(project_root=Path.cwd())
    repo.upsert_capability(
        replace(_capability("punctuation-name"), name="+++"),
        [0.0] * EMBEDDING_DIMENSION,
    )

    report = health.ensure_healthy()

    assert report.healthy is True


def test_fts_probe_token_is_not_injected_into_embedding_input(paths, repo):
    repo.initialize()
    capability = _capability("embedding-base-text")
    repo.upsert_capability(capability, [0.0] * EMBEDDING_DIMENSION)
    embeddings = RecordingEmbeddingProvider()
    health = HealthManager(paths, repo, embeddings)

    report = health.ensure_healthy()

    assert len(embeddings.documents) > 1  # Candidate plus the literal evaluation corpus.
    assert all("cmprobe" not in document for document in embeddings.documents)
    generation_repo = _generation_repo(paths, report.active_generation)
    row = generation_repo._rows("capabilities")[0]
    probe = next(token for token in row["search_text"].split() if token.startswith("cmprobe"))
    assert embeddings.documents[0] in row["search_text"]
    assert generation_repo._table("capabilities").search(
        probe,
        query_type="fts",
        fts_columns="search_text",
    ).limit(1).to_list()[0]["id"] == capability.id


def test_terminal_fts_repair_failure_code_overrides_initial_missing_runtime(paths, repo):
    class FtsFailingHealthManager(HealthManager):
        def _check_fts(self, failures, repository):
            failures.append("fts_unavailable: forced terminal FTS failure")
            return False

    health = FtsFailingHealthManager(paths, repo, DeterministicEmbeddingProvider())

    with pytest.raises(CapabilityMemoryBlocked) as error:
        health.ensure_healthy()

    assert error.value.code == "fts_unavailable"
    assert len(error.value.attempts) == 3


def test_generation_repository_is_used_for_vector_and_fts_checks(paths, repo):
    observed = []

    class ObservingHealthManager(HealthManager):
        def _check_vectors(self, failures, repository):
            observed.append(("vector", repository._database_path))
            return super()._check_vectors(failures, repository)

        def _check_fts(self, failures, repository):
            observed.append(("fts", repository._database_path))
            return super()._check_fts(failures, repository)

    health = ObservingHealthManager(paths, repo, DeterministicEmbeddingProvider())
    report = health.ensure_healthy()
    generation_database = paths.generations / report.active_generation / "database"

    assert ("vector", generation_database) in observed
    assert ("fts", generation_database) in observed


def test_activation_manifest_records_passing_versioned_retrieval_evaluation(
    bootstrap,
    paths,
):
    project = paths.root / "evaluation-project"
    project.mkdir(parents=True)
    report = bootstrap.initialize(project)
    manifest = json.loads(
        (
            paths.generations / report.active_generation / "manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert manifest["evaluation"]["dataset"] == "retrieval-evaluation-v1"
    assert manifest["evaluation"]["passed"] is True
    assert manifest["evaluation"]["metrics"] == {
        "filter_accuracy": 1.0,
        "hard_negative_accuracy": 1.0,
        "semantic_recall": 1.0,
        "hybrid_recall": 1.0,
    }


def test_generation_activation_fails_when_semantic_route_is_unusable(health, paths, repo):
    previous = health.ensure_healthy().active_generation
    pointer_before = (paths.root / "active-generation.json").read_bytes()

    class QueryFailingEmbeddingProvider(DeterministicEmbeddingProvider):
        def embed_documents(self, texts):
            return [DeterministicEmbeddingProvider.embed_query(self, text) for text in texts]

        def embed_query(self, text):
            if text == "capability memory health probe":
                return super().embed_query(text)
            raise RuntimeError("semantic queries are unavailable")

    manager = HealthManager(paths, repo, QueryFailingEmbeddingProvider())
    with pytest.raises(CapabilityMemoryBlocked, match="retrieval evaluation"):
        manager.rebuild_indexes()

    # The unchanged original provider can still use the old generation. The
    # failing replacement provider cannot claim that provider's evaluation.
    assert health.check().active_generation == previous
    assert not manager.check().healthy
    assert (paths.root / "active-generation.json").read_bytes() == pointer_before


def test_activation_manifest_records_thresholds_and_hybrid_metrics(health, paths):
    generation = health.rebuild_indexes()
    manifest = json.loads((paths.generations / generation / "manifest.json").read_text())

    assert manifest["evaluation"]["thresholds"] == {
        "semantic_recall": 1.0,
        "hybrid_recall": 1.0,
        "hard_negative_accuracy": 1.0,
        "filter_accuracy": 1.0,
    }
    assert set(manifest["evaluation"]["metrics"]) == set(
        manifest["evaluation"]["thresholds"]
    )
    assert manifest["evaluation"]["digest"] == manifest["evaluation_dataset_digest"]
    assert manifest["evaluation"]["version"] == 1
    assert manifest["evaluation"]["provider"].endswith("DeterministicEmbeddingProvider")


@pytest.mark.parametrize(
    "fault",
    (
        "documents-exception", "documents-count", "documents-dimension", "documents-nan",
        "query-dimension", "query-nan", "query-exception", "lexical-exception",
        "missing-vector-results", "missing-hybrid-results", "candidate-smoke-exception",
        "missing-dataset", "constant-vectors", "inverted-query", "empty-lexical-results",
        "reversed-hybrid", "decision-build",
    ),
)
def test_every_retrieval_evaluation_fault_preserves_pointer_and_cleans_temporary_stores(
    health, paths, repo, monkeypatch, fault,
):
    repo.initialize()
    repo.upsert_capability(_capability("actual-generation-row"), [1.0] * EMBEDDING_DIMENSION)
    health.ensure_healthy()
    pointer = paths.root / "active-generation.json"
    pointer_before = pointer.read_bytes()
    generations_before = set(paths.generations.iterdir())
    provider = health.embedding_provider
    original_query = provider.embed_query
    original_hybrid = CapabilityRepository.hybrid_search
    original_decide = ReuseDecisionEngine.decide

    def documents(texts):
        if any("Reusable OAuth 2.0" in text for text in texts):
            if fault == "documents-exception":
                raise RuntimeError("evaluation document model failed")
            if fault == "documents-count":
                return []
            if fault == "documents-dimension":
                return [[0.0] for _ in texts]
            if fault == "documents-nan":
                return [[float("nan")] * EMBEDDING_DIMENSION for _ in texts]
            if fault == "constant-vectors":
                return [[1.0] + [0.0] * (EMBEDDING_DIMENSION - 1) for _ in texts]
        return [DeterministicEmbeddingProvider.embed_query(provider, text) for text in texts]

    def query(text):
        if text != "capability memory health probe":
            if fault == "query-dimension":
                return [0.0]
            if fault == "query-nan":
                return [float("nan")] * EMBEDDING_DIMENSION
            if fault == "query-exception":
                raise RuntimeError("evaluation query model failed")
            if fault == "constant-vectors":
                return [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)
            if fault == "inverted-query":
                return [-value for value in original_query(text)]
        return original_query(text)

    def hybrid(repository, *args, **kwargs):
        target = repository._generation_reader(kwargs["generation"])
        is_fixture = target._database_path.parent.name.startswith("run-")
        if fault == "candidate-smoke-exception" and not is_fixture:
            raise RuntimeError("actual candidate hybrid route failed")
        if fault == "lexical-exception" and is_fixture:
            table = target._table("capabilities")
            original_search = table.search

            def search(*args, **kwargs):
                if kwargs.get("query_type") == "fts":
                    raise RuntimeError("lexical route failed")
                return original_search(*args, **kwargs)

            monkeypatch.setattr(table, "search", search)
        routes = original_hybrid(repository, *args, **kwargs)
        if is_fixture and fault == "missing-vector-results":
            return [], routes[1], routes[2]
        if is_fixture and fault == "missing-hybrid-results":
            return routes[0], routes[1], []
        if is_fixture and fault == "empty-lexical-results":
            return routes[0], [], routes[2]
        if is_fixture and fault == "reversed-hybrid":
            return routes[0], routes[1], list(reversed(routes[2]))
        return routes

    def decide(self, requirement, result, **kwargs):
        decision = original_decide(self, requirement, result, **kwargs)
        if fault == "decision-build":
            return replace(decision, action="build", selected_capability_id=None)
        return decision

    monkeypatch.setattr(provider, "embed_documents", documents)
    monkeypatch.setattr(provider, "embed_query", query)
    monkeypatch.setattr(CapabilityRepository, "hybrid_search", hybrid)
    monkeypatch.setattr(ReuseDecisionEngine, "decide", decide)
    if fault == "missing-dataset":
        monkeypatch.setattr(health, "_evaluation_dataset_path", lambda: paths.root / "missing.json")

    def forbidden_activation(generation):
        pytest.fail("evaluation fault reached pointer activation")

    monkeypatch.setattr(health, "_activate_generation", forbidden_activation)
    with pytest.raises(CapabilityMemoryBlocked) as error:
        health.rebuild_indexes()

    expected_code = (
        "retrieval_evaluation_regression"
        if fault in {
            "missing-vector-results", "missing-hybrid-results", "constant-vectors", "inverted-query",
            "empty-lexical-results", "reversed-hybrid", "decision-build",
        }
        else "retrieval_evaluation_unavailable"
    )
    assert error.value.code == expected_code
    assert pointer.read_bytes() == pointer_before
    assert set(paths.generations.iterdir()) == generations_before
    assert not list(paths.generations.rglob("run-*"))


@pytest.mark.parametrize("lifecycle", (Lifecycle.VERIFIED, Lifecycle.RETIRED, Lifecycle.DEGRADED))
def test_nonempty_candidate_generation_runs_hybrid_smoke_before_activation(
    paths, repo, health, monkeypatch, lifecycle,
):
    repo.initialize()
    item = replace(_capability("actual-smoke-capability"), lifecycle=lifecycle)
    repo.upsert_capability(item, [1.0] * EMBEDDING_DIMENSION)
    calls = []
    original_hybrid = CapabilityRepository.hybrid_search
    original_activate = health._activate_generation

    def hybrid(repository, *args, **kwargs):
        target = repository._generation_reader(kwargs["generation"])
        result = original_hybrid(repository, *args, **kwargs)
        calls.append((target._database_path, kwargs["query_text"], result))
        return result

    def activate(generation):
        actual = [call for call in calls if call[0] == paths.generations / generation / "database"]
        assert len(actual) == 1
        assert "cmprobe" not in actual[0][1]
        assert all("actual-smoke-capability" in {row["id"] for row in route} for route in actual[0][2])
        assert all(row["lifecycle"] == lifecycle.value for route in actual[0][2] for row in route)
        original_activate(generation)

    monkeypatch.setattr(CapabilityRepository, "hybrid_search", hybrid)
    monkeypatch.setattr(health, "_activate_generation", activate)
    generation = health.rebuild_indexes()

    assert _active_generation(paths) == generation
    assert len(calls) > 1
    assert not list(paths.generations.rglob("run-*"))


def test_retrieval_evaluation_regression_never_activates_candidate_generation(
    bootstrap,
    paths,
    monkeypatch,
):
    project = paths.root / "evaluation-regression-project"
    project.mkdir(parents=True)
    initial = bootstrap.initialize(project)
    pointer_before = (paths.root / "active-generation.json").read_bytes()

    monkeypatch.setattr(
        bootstrap.health_manager,
        "_evaluate_generation",
        lambda generation: {
            "dataset": "retrieval-evaluation-v1",
            "passed": False,
            "metrics": {
                "semantic_recall": 0.0,
                "hybrid_recall": 1.0,
                "hard_negative_accuracy": 1.0,
                "filter_accuracy": 1.0,
            },
        },
        raising=False,
    )

    with pytest.raises(CapabilityMemoryBlocked, match="evaluation"):
        bootstrap.health_manager.rebuild_indexes()

    assert (paths.root / "active-generation.json").read_bytes() == pointer_before
    assert bootstrap.health_manager.check().active_generation == initial.active_generation


def test_empty_retrieval_evaluation_dataset_is_a_blocking_fault(
    bootstrap,
    paths,
    tmp_path,
    monkeypatch,
):
    project = paths.root / "evaluation-dataset-fault-project"
    project.mkdir(parents=True)
    initial = bootstrap.initialize(project)
    pointer_before = (paths.root / "active-generation.json").read_bytes()
    empty_dataset = tmp_path / "empty-retrieval-evaluation.json"
    empty_dataset.write_text(
        json.dumps(
            {
                "id": "retrieval-evaluation-v1",
                "version": 1,
                "ranking_k": 3,
                "thresholds": {
                    "semantic_recall": 1.0,
                    "hybrid_recall": 1.0,
                    "hard_negative_accuracy": 1.0,
                    "filter_accuracy": 1.0,
                },
                "corpus": [],
                "queries": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        bootstrap.health_manager,
        "_evaluation_dataset_path",
        lambda: empty_dataset,
    )

    with pytest.raises(CapabilityMemoryBlocked, match="evaluation dataset"):
        bootstrap.health_manager.rebuild_indexes()

    assert (paths.root / "active-generation.json").read_bytes() == pointer_before
    assert initial.active_generation is not None


def test_corrupt_authoritative_requirement_observation_fails_health(
    bootstrap,
    health,
    repo,
):
    bootstrap.initialize(project_root=Path.cwd())
    repo._table("requirement_observations").add(
        [
            {
                "id": "corrupt-demand",
                "requirement": "not-json",
                "status": "unmet",
                "observed_at": "2026-09-04T00:00:00Z",
                "linked_capability_id": None,
            }
        ]
    )

    report = health.check()

    assert report.healthy is False
    assert any("source_unavailable" in failure for failure in report.failures)


@pytest.mark.parametrize("escaped_name", ["database", "runtime", "locks", "generations"])
def test_owned_directory_symlink_escape_blocks_without_external_writes(
    tmp_path, escaped_name
):
    paths = MemoryPaths.from_codex_home(tmp_path / "codex")
    paths.root.mkdir(parents=True)
    repository = CapabilityRepository.open(paths.database)
    external = tmp_path / f"external-{escaped_name}"
    external.mkdir()
    (external / "marker").write_text("unchanged", encoding="utf-8")
    escaped_path = getattr(paths, escaped_name)
    if escaped_path.exists():
        escaped_path.rename(paths.root / f"saved-{escaped_name}")
    escaped_path.symlink_to(external, target_is_directory=True)
    health = HealthManager(paths, repository, DeterministicEmbeddingProvider())
    before = _tree_contents(external)

    with pytest.raises(CapabilityMemoryBlocked) as error:
        health.ensure_healthy()

    assert error.value.code == "path_escape"
    assert _tree_contents(external) == before


def test_active_generation_symlink_escape_blocks_without_external_writes(
    bootstrap, health, paths, repo, tmp_path
):
    report = bootstrap.initialize(project_root=Path.cwd())
    generation_path = paths.generations / report.active_generation
    saved = paths.root / "saved-generation"
    generation_path.rename(saved)
    external = tmp_path / "external-generation"
    external.mkdir()
    (external / "marker").write_text("unchanged", encoding="utf-8")
    generation_path.symlink_to(external, target_is_directory=True)
    before = _tree_contents(external)

    with pytest.raises(CapabilityMemoryBlocked) as error:
        health.ensure_healthy()

    assert error.value.code == "path_escape"
    assert _tree_contents(external) == before


def test_root_symlink_escape_blocks_before_creating_any_external_state(tmp_path):
    codex_home = tmp_path / "codex"
    root_parent = codex_home / "supermind"
    root_parent.mkdir(parents=True)
    external = tmp_path / "external-root"
    external.mkdir()
    (external / "marker").write_text("unchanged", encoding="utf-8")
    paths = MemoryPaths.from_codex_home(codex_home)
    repository = CapabilityRepository.open(paths.database)
    paths.root.rename(tmp_path / "saved-root")
    paths.root.symlink_to(external, target_is_directory=True)
    health = HealthManager(paths, repository, DeterministicEmbeddingProvider())
    before = _tree_contents(external)

    with pytest.raises(CapabilityMemoryBlocked) as error:
        health.ensure_healthy()

    assert error.value.code == "path_escape"
    assert _tree_contents(external) == before


def test_contained_model_cache_symlink_is_allowed(bootstrap, health, paths):
    report = bootstrap.initialize(project_root=Path.cwd())
    blob = paths.model_cache / "model-blob"
    blob.write_text("cached", encoding="utf-8")
    (paths.model_cache / "current").symlink_to(blob.name)

    checked = health.check()

    assert report.healthy is True
    assert checked.healthy is True


def test_two_process_writers_are_serialized_without_losing_records(paths, bootstrap):
    bootstrap.initialize(project_root=Path.cwd())
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_write_capability_in_process,
            args=(str(paths.database), capability_id, start, results),
        )
        for capability_id in ("process-a", "process-b")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=30)

    assert [process.exitcode for process in processes] == [0, 0]
    assert sorted(results.get(timeout=5) for _ in processes) == ["process-a", "process-b"]
    reopened = CapabilityRepository.open(paths.database)
    assert tuple(capability.id for capability in reopened.list_capabilities()) == (
        "process-a",
        "process-b",
    )


def test_repeated_two_process_fresh_init_waits_for_transaction_staging(tmp_path):
    context = multiprocessing.get_context("spawn")

    for round_number in range(6):
        codex_home = tmp_path / f"round-{round_number}"
        start = context.Event()
        transaction_ready = context.Event()
        transaction_observed = context.Event()
        transaction_removed = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_initialize_around_transient_transaction,
                args=(
                    str(codex_home),
                    role,
                    start,
                    transaction_ready,
                    transaction_observed,
                    transaction_removed,
                    results,
                ),
            )
            for role in ("writer", "scanner")
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=45)

        assert [process.exitcode for process in processes] == [0, 0]
        reports = sorted(results.get(timeout=5) for _ in processes)
        assert [role for role, _ in reports] == ["scanner", "writer"]
        assert reports[0][1] == reports[1][1]


def test_process_writer_cannot_enter_while_writer_lock_is_held(paths, repo):
    repo.initialize()
    context = multiprocessing.get_context("spawn")
    lock_acquired = context.Event()
    release_lock = context.Event()
    write_attempted = context.Event()
    write_finished = context.Event()
    holder = context.Process(
        target=_hold_writer_lock,
        args=(str(paths.locks / "writer.lock"), lock_acquired, release_lock),
    )
    writer = context.Process(
        target=_write_capability_with_signals,
        args=(str(paths.database), str(paths.locks / "writer.lock"), write_attempted, write_finished),
    )

    holder.start()
    assert lock_acquired.wait(timeout=10)
    writer.start()
    assert write_attempted.wait(timeout=10)
    assert write_finished.wait(timeout=0.25) is False
    release_lock.set()
    holder.join(timeout=10)
    writer.join(timeout=10)

    assert holder.exitcode == 0
    assert writer.exitcode == 0
    assert write_finished.is_set()
    assert repo.get_capability("process-blocked") is not None


def test_twentieth_data_modification_optimizes_every_table_once_and_records_event(
    paths, repo, monkeypatch
):
    repo.initialize()
    optimize_calls = {table_name: 0 for table_name in TABLE_SCHEMAS}
    real_table = repo._table

    class OptimizeSpy:
        def __init__(self, table_name, table):
            self._table_name = table_name
            self._wrapped = table

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def optimize(self):
            optimize_calls[self._table_name] += 1
            return self._wrapped.optimize()

    monkeypatch.setattr(
        repo,
        "_table",
        lambda name: OptimizeSpy(name, real_table(name)),
    )

    for index in range(19):
        capability = replace(_capability("optimized"), name=f"Revision {index}")
        repo.upsert_capability(capability, [float(index)] * EMBEDDING_DIMENSION)

    assert optimize_calls == {table_name: 0 for table_name in TABLE_SCHEMAS}

    repo.upsert_capability(
        replace(_capability("optimized"), name="Revision 19"),
        [19.0] * EMBEDDING_DIMENSION,
    )

    events = repo.list_events("__system__")
    assert optimize_calls == {table_name: 1 for table_name in TABLE_SCHEMAS}
    assert len(events) == 1
    assert events[0].event_type == "indexes_optimized"


def test_active_pointer_replace_is_between_file_and_directory_fsync(
    paths, repo, monkeypatch
):
    operations = []
    real_fsync = health_module.os.fsync
    real_replace = health_module.os.replace
    pointer = paths.root / "active-generation.json"

    def recording_fsync(descriptor):
        operations.append(("fsync", descriptor))
        return real_fsync(descriptor)

    def recording_replace(source, destination):
        if Path(destination) == pointer:
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
            manifest = paths.generations / payload["active_generation"] / "manifest.json"
            assert manifest.is_file()
            operations.append(("replace-pointer", None))
        return real_replace(source, destination)

    monkeypatch.setattr(health_module.os, "fsync", recording_fsync)
    monkeypatch.setattr(health_module.os, "replace", recording_replace)
    report = HealthManager(paths, repo, DeterministicEmbeddingProvider()).ensure_healthy()

    replace_index = operations.index(("replace-pointer", None))
    assert any(kind == "fsync" for kind, _ in operations[:replace_index])
    assert any(kind == "fsync" for kind, _ in operations[replace_index + 1 :])
    assert _active_generation(paths) == report.active_generation


def test_directory_fsync_failure_after_real_pointer_replace_retries_safely(
    paths, repo, monkeypatch
):
    real_fsync = health_module.os.fsync
    real_replace = health_module.os.replace
    pointer = paths.root / "active-generation.json"
    pointer_replaced = False
    failed = False

    def recording_replace(source, destination):
        nonlocal pointer_replaced
        result = real_replace(source, destination)
        if Path(destination) == pointer:
            pointer_replaced = True
        return result

    def fail_first_post_replace_fsync(descriptor):
        nonlocal failed
        if pointer_replaced and not failed:
            failed = True
            raise OSError("simulated directory fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(health_module.os, "replace", recording_replace)
    monkeypatch.setattr(health_module.os, "fsync", fail_first_post_replace_fsync)

    report = HealthManager(paths, repo, DeterministicEmbeddingProvider()).ensure_healthy()

    assert failed is True
    assert _active_generation(paths) == report.active_generation
    assert (paths.generations / report.active_generation / "manifest.json").is_file()


def _active_generation(paths: MemoryPaths) -> str:
    payload = json.loads((paths.root / "active-generation.json").read_text(encoding="utf-8"))
    return payload["active_generation"]


def _generation_repo(paths: MemoryPaths, generation: str) -> CapabilityRepository:
    return CapabilityRepository.open(
        paths.generations / generation / "database",
        writer_lock_path=paths.locks / "writer.lock",
    )


def _tree_contents(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _add_legacy_column(repo: CapabilityRepository, table_name: str) -> None:
    table = repo._table(table_name)
    rows = table.to_arrow().to_pylist()
    old_schema = TABLE_SCHEMAS[table_name].append(
        pa.field("legacy_index_hint", pa.string(), nullable=True)
    )
    repo._database.drop_table(table_name)
    repo._database.create_table(
        table_name,
        data=pa.Table.from_pylist(
            [{**row, "legacy_index_hint": "v0"} for row in rows],
            schema=old_schema,
        ),
    )


def _seed_every_authoritative_table(
    repo: CapabilityRepository,
    capability_id: str,
) -> dict[str, list[dict[str, object]]]:
    repo.upsert_capability(_capability(capability_id), [0.25] * EMBEDDING_DIMENSION)
    repo.append_evidence(_evidence(capability_id))
    repo.append_relationship(_relationship(capability_id))
    repo.append_event(_event(capability_id))
    repo.set_metadata("source", {"revision": 1})
    _seed_linked_demand(repo, capability_id)
    return _authoritative_rows(repo)


def _authoritative_rows(repo: CapabilityRepository) -> dict[str, list[dict[str, object]]]:
    return {
        table_name: repo._table(table_name).to_arrow().to_pylist()
        for table_name in TABLE_SCHEMAS
    }


def _capability(capability_id: str) -> Capability:
    return Capability(
        id=capability_id,
        name="OAuth login",
        summary="Reusable login flow",
        category_path=("Code and components", "Identity and access"),
        facets=("authentication",),
        contract="OAuth callback",
        constraints=(),
        artifact_type=ArtifactType.CODE,
        source_uri="/project/login.py",
        source_revision="abc123",
        content_hash="hash-a",
        owner="team",
        license="MIT",
        stack=("Python",),
        runtime=("CPython",),
        platform=("macOS",),
        dependencies=("authlib",),
        compatibility=("OAuth 2.0",),
        lifecycle=Lifecycle.CANDIDATE,
        confidence=1.0,
        expected_net_value=2.0,
        embedding_generation="generation-1",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
        last_verified_at=None,
    )


def _evidence(capability_id: str) -> Evidence:
    return Evidence(
        id="evidence-1",
        capability_id=capability_id,
        source_project="project-a",
        evidence_type="verification",
        outcome="passed",
        metric_name=None,
        metric_value=None,
        confidence=1.0,
        observed_at="2026-09-04T00:00:00Z",
        supporting_uri=None,
    )


def _relationship(capability_id: str) -> Relationship:
    return Relationship(
        id="relationship-1",
        source_id=capability_id,
        target_id="dependency-1",
        relationship_type="depends_on",
        compatibility=("OAuth 2.0",),
        evidence_ids=("evidence-1",),
    )


def _event(capability_id: str) -> Event:
    return Event(
        id="event-1",
        capability_id=capability_id,
        event_type="verified",
        source_context="project-a",
        occurred_at="2026-09-04T00:00:00Z",
        previous_state=Lifecycle.OBSERVED,
        resulting_state=Lifecycle.CANDIDATE,
        reason="verified during migration fixture",
    )


def _write_capability_in_process(database_path, capability_id, start, results):
    start.wait(timeout=10)
    repository = CapabilityRepository.open(Path(database_path))
    repository.upsert_capability(
        _capability(capability_id),
        [0.5] * EMBEDDING_DIMENSION,
    )
    results.put(capability_id)


def _hold_writer_lock(lock_path, acquired, release):
    with FileLock(lock_path, timeout=30):
        acquired.set()
        release.wait(timeout=10)


def _write_capability_with_signals(database_path, lock_path, attempted, finished):
    repository = CapabilityRepository.open(Path(database_path), writer_lock_path=Path(lock_path))
    attempted.set()
    repository.upsert_capability(
        _capability("process-blocked"),
        [0.5] * EMBEDDING_DIMENSION,
    )
    finished.set()


def _initialize_around_transient_transaction(
    codex_home,
    role,
    start,
    transaction_ready,
    transaction_observed,
    transaction_removed,
    results,
):
    paths = MemoryPaths.from_codex_home(Path(codex_home))
    repository = CapabilityRepository.open(
        paths.database,
        writer_lock_path=paths.locks / "writer.lock",
    )
    health = HealthManager(paths, repository, DeterministicEmbeddingProvider())
    start.wait(timeout=10)
    initial = health.ensure_healthy()
    try:
        if role == "writer":
            with repository.atomic_write():
                transaction_ready.set()
                transaction_observed.wait(timeout=0.5)
            transaction_removed.set()
        else:
            if not transaction_ready.wait(timeout=30):
                raise TimeoutError("transaction staging was not created")
            real_scandir = health_module.os.scandir

            class FrozenEntry:
                def __init__(self, entry):
                    self.path = entry.path
                    self.name = entry.name
                    self._is_symlink = entry.is_symlink()
                    self._is_dir = entry.is_dir(follow_symlinks=False)

                def is_symlink(self):
                    return self._is_symlink

                def is_dir(self, *, follow_symlinks=True):
                    return self._is_dir

            class FrozenScandir:
                def __init__(self, entries):
                    self.entries = entries

                def __enter__(self):
                    return iter(self.entries)

                def __exit__(self, *_):
                    return None

            def coordinate_runtime_scan(directory):
                scanned = real_scandir(directory)
                if Path(directory) != paths.runtime:
                    return scanned
                with scanned as entries:
                    frozen = tuple(FrozenEntry(entry) for entry in entries)
                if any(entry.name.startswith("transaction-") for entry in frozen):
                    transaction_observed.set()
                    if not transaction_removed.wait(timeout=10):
                        raise TimeoutError("transaction staging was not removed")
                return FrozenScandir(frozen)

            health_module.os.scandir = coordinate_runtime_scan

        final = health.ensure_healthy()
        assert initial.active_generation == final.active_generation
        results.put((role, final.active_generation))
    finally:
        repository.close()


def _interrupt_migration_after_first_commit(database_path):
    repository = CapabilityRepository.open(Path(database_path))
    real_overwrite = repository._overwrite_table_unlocked
    commits = 0

    def interrupt(table_name, schema, rows):
        nonlocal commits
        real_overwrite(table_name, schema, rows)
        commits += 1
        if commits == 1:
            os._exit(23)

    repository._overwrite_table_unlocked = interrupt
    with repository._writer_lock():
        repository._migrate_schemas_unlocked()


def _pause_migration_after_first_commit(database_path, first_commit, resume):
    repository = CapabilityRepository.open(Path(database_path))
    real_overwrite = repository._overwrite_table_unlocked
    commits = 0

    def pause(table_name, schema, rows):
        nonlocal commits
        real_overwrite(table_name, schema, rows)
        commits += 1
        if commits == 1:
            first_commit.set()
            if not resume.wait(timeout=20):
                raise TimeoutError("migration test was not resumed")

    repository._overwrite_table_unlocked = pause
    with repository._writer_lock():
        repository._migrate_schemas_unlocked()


@pytest.mark.parametrize("fault", [
    "passed-only", "zero-metrics", "provider", "dataset", "version", "digest",
    "thresholds", "metric-key", "threshold-key", "nan", "infinity", "boolean", "passed-false",
])
def test_final_health_rejects_incomplete_or_forged_evaluation_evidence(health, paths, fault):
    generation = health.rebuild_indexes()
    manifest_path = paths.generations / generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    evidence = manifest["evaluation"]
    if fault == "passed-only":
        manifest["evaluation"] = {"passed": True}
    elif fault == "zero-metrics":
        evidence["metrics"] = {key: 0.0 for key in evidence["metrics"]}
    elif fault in {"provider", "dataset", "digest"}:
        evidence[fault] = "wrong-evidence"
    elif fault == "version":
        evidence["version"] += 1
    elif fault == "thresholds":
        evidence["thresholds"] = {key: 0.0 for key in evidence["thresholds"]}
    elif fault == "metric-key":
        evidence["metrics"].pop("semantic_recall")
    elif fault == "threshold-key":
        evidence["thresholds"].pop("semantic_recall")
    elif fault in {"nan", "infinity", "boolean"}:
        evidence["metrics"]["semantic_recall"] = {"nan": float("nan"), "infinity": float("inf"), "boolean": True}[fault]
    else:
        evidence["passed"] = False
    manifest_path.write_text(json.dumps(manifest))
    before = manifest_path.read_bytes()
    report = health.check()
    assert report.healthy is False
    assert any("evaluation" in failure for failure in report.failures)
    assert manifest_path.read_bytes() == before


@pytest.mark.parametrize("policy", [False, True])
def test_final_relationship_policy_regression_cannot_activate_generation(health, paths, monkeypatch, policy):
    import supermind_memory.search as search_module

    health.rebuild_indexes()
    pointer = paths.root / "active-generation.json"
    before = pointer.read_bytes()
    generations = set(paths.generations.iterdir())
    monkeypatch.setattr(search_module, "relationship_compatible", lambda *args: policy)
    with pytest.raises(CapabilityMemoryBlocked) as failure:
        health.rebuild_indexes()
    assert failure.value.code == "retrieval_evaluation_regression"
    assert pointer.read_bytes() == before
    assert set(paths.generations.iterdir()) == generations
