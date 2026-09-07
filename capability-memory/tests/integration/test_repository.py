from __future__ import annotations

from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pyarrow as pa
import pytest

from supermind_memory.repository import CapabilityRepository, _REPOSITORY_REGISTRY
from supermind_memory.schema import EMBEDDING_DIMENSION, SCHEMA_VERSION, TABLE_SCHEMAS
from supermind_memory.types import ArtifactType, Capability, Event, Evidence, Lifecycle, Relationship


@pytest.fixture
def capability() -> Capability:
    return Capability(
        abstraction_status="abstracted",
        id="capability-1",
        name="Login",
        summary="OAuth login",
        category_path=("code", "Identity and access"),
        facets=("authentication",),
        contract="OAuth callback",
        constraints=("requires client secret",),
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
        expected_net_value=1.0,
        embedding_generation="generation-1",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
        last_verified_at=None,
    )


@pytest.fixture
def vector() -> list[float]:
    return [0.5] * EMBEDDING_DIMENSION


def verification(capability_id: str, project: str = "project-a") -> Evidence:
    return Evidence(
        id="verification-project-a",
        capability_id=capability_id,
        source_project=project,
        evidence_type="verification",
        outcome="passed",
        metric_name=None,
        metric_value=None,
        confidence=1.0,
        observed_at="2026-09-04T00:00:00Z",
        supporting_uri=None,
        integration_effort=0.1,
        benefit=2.0,
        failure_risk=0.0,
    )


def relationship(capability_id: str) -> Relationship:
    return Relationship(
        id="relationship-1",
        source_id=capability_id,
        target_id="capability-2",
        relationship_type="depends_on",
        compatibility=("OAuth 2.0",),
        evidence_ids=("verification-project-a",),
    )


def discovered_event(capability_id: str) -> Event:
    return Event(
        id="event-1",
        capability_id=capability_id,
        event_type="discovered",
        source_context="project-a",
        occurred_at="2026-09-04T00:00:00Z",
        previous_state=None,
        resulting_state=Lifecycle.CANDIDATE,
        reason="found in source scan",
    )


def test_repository_round_trips_capability_and_audit_records(tmp_path, capability, vector):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    repo.upsert_capability(capability, vector)
    evidence = verification(capability.id)
    relation = relationship(capability.id)
    event = discovered_event(capability.id)
    repo.append_evidence(evidence)
    repo.append_relationship(relation)
    repo.append_event(event)

    assert repo.get_capability(capability.id) == capability
    assert repo.list_evidence(capability.id) == (evidence,)
    assert repo.list_relationships(capability.id) == (relation,)
    assert repo.list_events(capability.id) == (event,)


def test_initialize_and_stable_id_writes_are_idempotent(tmp_path, capability, vector):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    repo.initialize()
    updated = replace(capability, name="Updated login", updated_at="2026-09-04T01:00:00Z")
    evidence = verification(capability.id)
    relation = relationship(capability.id)
    event = discovered_event(capability.id)

    repo.upsert_capability(capability, vector)
    repo.upsert_capability(updated, vector)
    repo.append_evidence(evidence)
    repo.append_evidence(evidence)
    repo.append_relationship(relation)
    repo.append_relationship(relation)
    repo.append_event(event)
    repo.append_event(event)

    assert repo.list_capabilities() == (updated,)
    assert repo.list_evidence(capability.id) == (evidence,)
    assert repo.list_relationships(capability.id) == (relation,)
    assert repo.list_events(capability.id) == (event,)


def test_capability_upsert_preserves_the_first_observed_created_at(tmp_path, capability, vector):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    repo.upsert_capability(capability, vector)

    repo.upsert_capability(
        replace(capability, summary="refreshed", created_at="2026-09-05T00:00:00Z", updated_at="2026-09-05T00:00:00Z"),
        vector,
    )

    stored = repo.get_capability(capability.id)
    assert stored is not None
    assert stored.created_at == capability.created_at
    assert stored.updated_at == "2026-09-05T00:00:00Z"


def test_set_metadata_replaces_a_stable_key_with_canonical_json(tmp_path):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()

    repo.set_metadata("model", {"revision": 2, "name": "mini"})
    repo.set_metadata("model", {"name": "mini", "revision": 3})

    assert [row for row in repo._rows("metadata") if row["key"] == "model"] == [
        {"key": "model", "value": '{"name":"mini","revision":3}'}
    ]


def test_get_metadata_returns_the_canonical_value_or_none(tmp_path):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    repo.set_metadata("source-registry", {"active": ["/project"]})

    assert repo.get_metadata("source-registry") == {"active": ["/project"]}
    assert repo.get_metadata("missing") is None


LEGACY_TABLES = ("capabilities", "evidence", "relationships", "events", "metadata")
DEMAND_TABLES = ("requirement_observations", "requirement_events")


def _legacy_store(tmp_path, capability, vector, *, old_schemas=False):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    for name in LEGACY_TABLES:
        repo._database.create_table(name, schema=TABLE_SCHEMAS[name])
    repo.upsert_capability(capability, vector)
    repo.append_evidence(verification(capability.id))
    repo.append_event(discovered_event(capability.id))
    repo.set_metadata("legacy-project", {"revision": 7})
    if old_schemas:
        for name in LEGACY_TABLES:
            table = repo._table(name).to_arrow()
            repo._overwrite_table_unlocked(
                name, table.schema.append(pa.field("legacy_hint", pa.string())),
                [{**row, "legacy_hint": "preserve-until-migrated"} for row in table.to_pylist()],
            )
    return repo


def test_proven_v1_store_migrates_both_demand_tables_atomically(tmp_path, capability, vector):
    repo = _legacy_store(tmp_path, capability, vector)
    before = {name: repo._rows(name) for name in LEGACY_TABLES}

    repo.initialize()

    assert set(repo._database.list_tables().tables) == set(TABLE_SCHEMAS)
    assert repo.get_metadata("authoritative_schema_version") == 2
    assert repo.list_requirement_observations() == ()
    assert repo._rows("requirement_events") == []
    for name in LEGACY_TABLES[:-1]:
        assert repo._rows(name) == before[name]
    assert repo.get_metadata("legacy-project") == {"revision": 7}
    assert not repo._migration_journal_path.exists()


@pytest.mark.parametrize("missing", [DEMAND_TABLES[:1], DEMAND_TABLES[1:], DEMAND_TABLES])
def test_v2_initialize_never_recreates_missing_demand_tables(tmp_path, missing):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    repo.set_metadata("authoritative_schema_version", 2)
    for name in missing:
        repo._database.drop_table(name)
    before = _tree_bytes(repo._database_path)

    with pytest.raises(RuntimeError, match="authoritative_store_corrupt"):
        repo.initialize()

    assert _tree_bytes(repo._database_path) == before


@pytest.mark.parametrize("remaining", [*DEMAND_TABLES, "missing-legacy"])
def test_partial_no_marker_store_is_not_proven_v1(tmp_path, capability, vector, remaining):
    repo = _legacy_store(tmp_path, capability, vector)
    if remaining == "missing-legacy":
        repo._database.drop_table("events")
    else:
        repo._database.create_table(remaining, schema=TABLE_SCHEMAS[remaining])
    before = _tree_bytes(repo._database_path)

    with pytest.raises(RuntimeError, match="authoritative_store_corrupt"):
        repo.initialize()

    assert _tree_bytes(repo._database_path) == before


@pytest.mark.parametrize("value", ["null", "true", "1", "3", '"2"', "2.0", "not-json"])
def test_invalid_authoritative_schema_marker_blocks_without_mutation(tmp_path, value):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    repo._table("metadata").merge_insert("key").when_matched_update_all().when_not_matched_insert_all().execute(
        [{"key": "authoritative_schema_version", "value": value}]
    )
    before = _tree_bytes(repo._database_path)

    with pytest.raises(RuntimeError, match="authoritative_store_corrupt"):
        repo.initialize()

    assert _tree_bytes(repo._database_path) == before


def test_complete_pre_marker_v2_adopts_marker_without_rewriting_demand_history(tmp_path):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    repo._table("metadata").delete("key = 'authoritative_schema_version'")
    repo._table("requirement_observations").add([{
        "id": "demand-before-marker", "requirement": json.dumps({
            "id": "required-login", "project_id": "project-a", "intent": "OAuth login",
        }), "status": "unmet", "observed_at": "2026-09-06T00:00:00Z", "linked_capability_id": None,
    }])
    repo._table("requirement_events").add([{
        "id": "demand-created", "observation_id": "demand-before-marker",
        "event_type": "unmet_observed", "occurred_at": "2026-09-06T00:00:00Z",
        "capability_id": None, "reason": "complete search found no reusable login",
    }])
    before = {name: repo._rows(name) for name in DEMAND_TABLES}

    repo.initialize()

    assert repo.get_metadata("authoritative_schema_version") == 2
    assert {name: repo._rows(name) for name in DEMAND_TABLES} == before


@pytest.mark.parametrize("crash_after", [*LEGACY_TABLES, *DEMAND_TABLES, "marker"])
def test_proven_v1_migration_crash_recovers_complete_snapshot(
    tmp_path, capability, vector, crash_after,
):
    repo = _legacy_store(tmp_path, capability, vector, old_schemas=True)
    before = {name: repo._table(name).to_arrow() for name in LEGACY_TABLES}
    database = repo._database_path
    repo.close()
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_v1_migration, args=(str(database), crash_after),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 23

    with CapabilityRepository.open(database) as recovered:
        assert set(recovered._database.list_tables().tables) == set(LEGACY_TABLES)
        for name, snapshot in before.items():
            assert recovered._table(name).to_arrow().equals(snapshot)
        assert recovered.get_metadata("authoritative_schema_version") is None
        recovered.initialize()
        assert recovered.get_metadata("authoritative_schema_version") == 2
        assert recovered.get_capability(capability.id) == capability
        assert recovered.list_requirement_observations() == ()
        assert recovered._rows("requirement_events") == []


def _crash_v1_migration(database_path, crash_after):
    repo = CapabilityRepository.open(Path(database_path))
    overwrite = repo._overwrite_table_unlocked
    set_metadata = repo._set_metadata_unlocked

    def replace_then_crash(name, schema, rows):
        overwrite(name, schema, rows)
        if name == crash_after:
            os._exit(23)

    def mark_then_crash(key, value):
        set_metadata(key, value)
        if key == "authoritative_schema_version" and crash_after == "marker":
            os._exit(23)

    repo._overwrite_table_unlocked = replace_then_crash
    repo._set_metadata_unlocked = mark_then_crash
    repo.initialize()


def test_migration_reopen_rejects_symlinked_runtime_without_touching_snapshot(
    tmp_path, capability, vector,
):
    repo = _legacy_store(tmp_path, capability, vector)
    with repo._writer_lock():
        repo._write_migration_journal_unlocked()
    database = repo._database_path
    runtime = repo._migration_journal_path.parent
    repo.close()
    external = tmp_path / "external-snapshot"
    runtime.rename(external)
    runtime.symlink_to(external, target_is_directory=True)
    before = _tree_bytes(external)

    with pytest.raises(RuntimeError, match="unsafe migration"):
        CapabilityRepository.open(database)

    assert _tree_bytes(external) == before


@pytest.mark.parametrize("boundary", [
    "root", "table", "lock-file", "lock-directory", "foreign-lock", "foreign-lock-via-parent-traversal",
])
def test_migration_open_rejects_symlinks_before_external_writes(
    tmp_path, capability, vector, boundary,
):
    original = tmp_path / "original-store"
    repo = _legacy_store(original, capability, vector)
    with repo._writer_lock():
        repo._write_migration_journal_unlocked()
    database = repo._database_path
    journal = repo._migration_journal_path
    repo.close()
    external = tmp_path / "external"
    lock_override = None
    if boundary == "root":
        external = original
        alias = tmp_path / "store-alias"
        alias.symlink_to(original, target_is_directory=True)
        database = alias / "memory.lance"
    elif boundary == "table":
        table = database / "capabilities.lance"
        table.rename(external)
        table.symlink_to(external, target_is_directory=True)
    elif boundary == "lock-file":
        external.mkdir()
        target = external / "private-lock"
        target.write_text("external lock must remain intact", encoding="utf-8")
        lock = original / "locks" / "writer.lock"
        lock.unlink()
        lock.symlink_to(target)
    elif boundary == "lock-directory":
        lock_directory = original / "locks"
        lock_directory.rename(external)
        (external / "writer.lock").write_text("external lock must remain intact", encoding="utf-8")
        lock_directory.symlink_to(external, target_is_directory=True)
    else:
        (external / "locks").mkdir(parents=True)
        lock_override = external / "locks" / "writer.lock"
        lock_override.write_text("external lock must remain intact", encoding="utf-8")
        if boundary == "foreign-lock-via-parent-traversal":
            (external / "generations" / "candidate").mkdir(parents=True)
            database = external / "generations" / "candidate" / ".." / ".." / ".." / "original-store" / "memory.lance"
    before = _tree_bytes(external)
    journal_before = journal.read_bytes()

    with pytest.raises((RuntimeError, OSError), match="unsafe|symlink|symbolic"):
        CapabilityRepository.open(database, writer_lock_path=lock_override)

    assert _tree_bytes(external) == before
    assert journal.read_bytes() == journal_before


def test_migration_open_preserves_ancestor_generation_writer_lock_support(tmp_path, capability, vector):
    repo = _legacy_store(tmp_path / "generations" / "candidate", capability, vector)
    with repo._writer_lock():
        repo._write_migration_journal_unlocked()
    database = repo._database_path
    before = {name: repo._rows(name) for name in LEGACY_TABLES}
    repo.close()

    with CapabilityRepository.open(database, tmp_path / "locks" / "writer.lock") as recovered:
        assert {name: recovered._rows(name) for name in LEGACY_TABLES} == before
        assert not recovered._migration_journal_path.exists()


@pytest.mark.parametrize("boundary", [
    "root", "database", "runtime", "lock-directory", "lock-file", "foreign-lock",
])
def test_open_without_journal_rejects_unsafe_paths_before_creating_database(tmp_path, boundary):
    root = tmp_path / "store"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "marker").write_text("unchanged", encoding="utf-8")
    database = root / "new-database"
    lock_override = None
    if boundary == "root":
        alias = tmp_path / "store-alias"
        alias.symlink_to(external, target_is_directory=True)
        database = alias / "new-database"
    elif boundary == "database":
        database.symlink_to(external, target_is_directory=True)
    elif boundary == "runtime":
        (root / "runtime").symlink_to(external, target_is_directory=True)
    elif boundary == "lock-directory":
        (root / "locks").symlink_to(external, target_is_directory=True)
    elif boundary == "lock-file":
        (root / "locks").mkdir()
        (root / "locks" / "writer.lock").symlink_to(external / "marker")
    else:
        (external / "locks").mkdir()
        lock_override = external / "locks" / "writer.lock"
        lock_override.write_text("external lock must remain intact", encoding="utf-8")
    before_bytes = _tree_bytes(external)
    before_paths = tuple(sorted(path.relative_to(external) for path in external.rglob("*")))

    try:
        with pytest.raises(RuntimeError, match="unsafe|symlink"):
            with CapabilityRepository.open(database, lock_override):
                pass
    finally:
        assert _tree_bytes(external) == before_bytes
        assert tuple(sorted(path.relative_to(external) for path in external.rglob("*"))) == before_paths
        if not database.is_symlink():
            assert not database.exists()


@pytest.mark.parametrize("journal_kind", ["migration", "transaction"])
def test_database_alias_cannot_expose_uncommitted_metadata_or_hide_journal(tmp_path, journal_kind):
    database = tmp_path / "target" / "memory.lance"
    with CapabilityRepository.open(database) as repo:
        repo.initialize()
        repo.set_metadata("recovery-probe", "committed")
        with repo._writer_lock():
            if journal_kind == "migration":
                repo._write_migration_journal_unlocked()
            else:
                repo._prepare_transaction_unlocked()
            repo._set_metadata_unlocked("recovery-probe", "uncommitted")
    journal = database.parent / "runtime" / f"{journal_kind}-journal.json"
    journal_before = journal.read_bytes()
    before = _tree_bytes(database.parent)
    caller = tmp_path / "caller"
    caller.mkdir()
    alias = caller / "database-alias"
    alias.symlink_to(database, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        with CapabilityRepository.open(alias) as reopened:
            assert reopened.get_metadata("recovery-probe") == "committed"

    assert _tree_bytes(database.parent) == before
    assert journal.read_bytes() == journal_before
    with CapabilityRepository.open(database) as recovered:
        assert recovered.get_metadata("recovery-probe") == "committed"
    assert not journal.exists()


def test_missing_component_traversal_cannot_hide_root_alias(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "marker").write_text("unchanged", encoding="utf-8")
    (root / "alias").symlink_to(external, target_is_directory=True)
    database = root / "missing" / ".." / "alias" / "new-database"
    before = _tree_bytes(external)
    before_paths = tuple(sorted(path.relative_to(external) for path in external.rglob("*")))

    try:
        with pytest.raises(RuntimeError, match="unsafe|symlink"):
            with CapabilityRepository.open(database):
                pass
    finally:
        assert _tree_bytes(external) == before
        assert tuple(sorted(path.relative_to(external) for path in external.rglob("*"))) == before_paths
        assert not (root / "missing").exists()


def test_missing_component_traversal_cannot_hide_database_alias(tmp_path):
    root = tmp_path / "store"
    database = root / "memory.lance"
    with CapabilityRepository.open(database) as repo:
        repo.initialize()
        repo.set_metadata("recovery-probe", "committed")
    (root / "database-alias").symlink_to(database, target_is_directory=True)
    requested = root / "missing" / ".." / "database-alias"
    before = _tree_bytes(database)

    with pytest.raises(RuntimeError, match="unsafe|symlink"):
        with CapabilityRepository.open(requested) as reopened:
            assert reopened.get_metadata("recovery-probe") == "committed"

    assert _tree_bytes(database) == before
    assert not (root / "missing").exists()


@pytest.mark.parametrize("journal_kind", ["migration", "transaction"])
@pytest.mark.parametrize("lock_path_kind", ["default", "canonical"])
def test_missing_component_traversal_cannot_replay_external_runtime(
    tmp_path, journal_kind, lock_path_kind,
):
    root = tmp_path / "store"
    database = root / "memory.lance"
    with CapabilityRepository.open(database) as repo:
        repo.initialize()
        repo.set_metadata("recovery-probe", "committed")
        with repo._writer_lock():
            if journal_kind == "migration":
                repo._write_migration_journal_unlocked()
            else:
                repo._prepare_transaction_unlocked()
            repo._set_metadata_unlocked("recovery-probe", "uncommitted")
    external = tmp_path / "external-runtime"
    (root / "runtime").rename(external)
    (root / "runtime").symlink_to(external, target_is_directory=True)
    journal = external / f"{journal_kind}-journal.json"
    journal_before = journal.read_bytes()
    runtime_before = _tree_bytes(external)
    database_before = _tree_bytes(database)
    requested = root / "missing" / ".." / "memory.lance"
    lock = root / "locks" / "writer.lock" if lock_path_kind == "canonical" else None

    try:
        with pytest.raises(RuntimeError, match="unsafe|symlink"):
            with CapabilityRepository.open(requested, lock):
                pass
    finally:
        assert _tree_bytes(external) == runtime_before
        assert journal.read_bytes() == journal_before
        assert _tree_bytes(database) == database_before
        assert not (root / "missing").exists()


def test_missing_component_traversal_in_writer_lock_rejects_before_connect(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    database = root / "new-database"
    requested_lock = root / "missing" / ".." / "locks" / "writer.lock"

    try:
        with pytest.raises(RuntimeError, match="unsafe"):
            with CapabilityRepository.open(database, requested_lock):
                pass
    finally:
        assert tuple(root.iterdir()) == ()


def test_v1_initialization_rejects_foreign_lock_before_acquisition(tmp_path, capability, vector):
    repo = _legacy_store(tmp_path / "original-store", capability, vector)
    database = repo._database_path
    repo.close()
    foreign = tmp_path / "external" / "locks" / "writer.lock"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("external lock must remain intact", encoding="utf-8")
    before = _tree_bytes(tmp_path)

    with pytest.raises(RuntimeError, match="unsafe.*lock ownership"):
        with CapabilityRepository.open(database, foreign) as reopened:
            reopened.initialize()

    assert _tree_bytes(tmp_path) == before


def _broken_demand_schema(table_name, fault):
    field_name = "event_type" if table_name == "requirement_events" else "requirement"
    fields = []
    for field in TABLE_SCHEMAS[table_name]:
        if field.name != field_name:
            fields.append(field)
        elif fault == "wrong-type":
            fields.append(pa.field(field_name, pa.int64(), nullable=False))
    return pa.schema(fields)


@pytest.mark.parametrize("table_name", DEMAND_TABLES)
@pytest.mark.parametrize("fault", ["missing-field", "wrong-type"])
def test_empty_broken_demand_schema_cannot_adopt_v2_marker(tmp_path, table_name, fault):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    repo._table("metadata").delete("key = 'authoritative_schema_version'")
    repo._overwrite_table_unlocked(table_name, _broken_demand_schema(table_name, fault), [])
    before = _tree_bytes(repo._database_path)

    with pytest.raises(RuntimeError, match="authoritative_store_corrupt"):
        repo.initialize()

    assert _tree_bytes(repo._database_path) == before
    assert repo.get_metadata("authoritative_schema_version") is None
    assert not repo._migration_journal_path.exists()


@pytest.mark.parametrize("table_name", DEMAND_TABLES)
@pytest.mark.parametrize("fault", ["missing-field", "wrong-type"])
@pytest.mark.parametrize("journal_format", [1, 2])
def test_migration_open_rejects_empty_broken_snapshot_before_replay(
    tmp_path, table_name, fault, journal_format,
):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    repo._table("metadata").delete("key = 'authoritative_schema_version'")
    repo._overwrite_table_unlocked(table_name, _broken_demand_schema(table_name, fault), [])
    with repo._writer_lock():
        repo._write_migration_journal_unlocked()
    journal = repo._migration_journal_path
    payload = json.loads(journal.read_text())
    payload["format"] = journal_format
    journal.write_text(json.dumps(payload), encoding="utf-8")
    # The live table is valid. Replay must not replace it with a broken snapshot.
    repo._overwrite_table_unlocked(table_name, TABLE_SCHEMAS[table_name], [])
    database = repo._database_path
    repo.close()
    before = _tree_bytes(tmp_path)

    with pytest.raises(RuntimeError, match="authoritative_store_corrupt"):
        CapabilityRepository.open(database)

    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("journal_format", [1, 2])
def test_migration_open_preserves_supported_legacy_snapshot_schemas(
    tmp_path, capability, vector, journal_format,
):
    repo = _legacy_store(tmp_path, capability, vector, old_schemas=True)
    for name in DEMAND_TABLES:
        repo._database.create_table(name, schema=TABLE_SCHEMAS[name])
    for name, optional_fields in (
        ("capabilities", ["vector", "search_text"]),
        ("evidence", ["metric_name", "metric_value", "supporting_uri"]),
    ):
        legacy = repo._table(name).to_arrow().drop_columns(optional_fields)
        repo._overwrite_table_unlocked(name, legacy.schema, legacy.to_pylist())
    before = {name: repo._table(name).to_arrow() for name in TABLE_SCHEMAS}
    with repo._writer_lock():
        repo._write_migration_journal_unlocked()
    journal = repo._migration_journal_path
    payload = json.loads(journal.read_text())
    payload["format"] = journal_format
    journal.write_text(json.dumps(payload), encoding="utf-8")
    repo._table("capabilities").update(where="id = 'capability-1'", values={"name": "uncommitted change"})
    database = repo._database_path
    repo.close()

    with CapabilityRepository.open(database) as recovered:
        assert not recovered._migration_journal_path.exists()
        for name, original in before.items():
            assert recovered._table(name).to_arrow().equals(original)
        recovered.initialize()
        assert recovered.get_metadata("authoritative_schema_version") == 2
        assert recovered.get_capability(capability.id) == capability
        assert recovered.list_evidence(capability.id) == (verification(capability.id),)


def test_update_metadata_reads_and_replaces_one_key_under_the_writer_lock(tmp_path):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    repo.set_metadata("source-registry", {"registered": ["first"]})

    repo.update_metadata(
        "source-registry",
        lambda current: {**current, "registered": [*current["registered"], "second"]},
    )

    assert repo.get_metadata("source-registry") == {"registered": ["first", "second"]}


def test_schema_constants_and_all_arrow_table_schemas_are_exact():
    assert SCHEMA_VERSION == 2
    assert EMBEDDING_DIMENSION == 384
    assert _schema_fields(TABLE_SCHEMAS) == {
        "capabilities": (
            ("id", pa.string(), False), ("name", pa.string(), False), ("summary", pa.string(), False),
            ("category_path", pa.string(), False), ("facets", pa.string(), False), ("contract", pa.string(), False),
            ("constraints", pa.string(), False), ("artifact_type", pa.string(), False),
            ("source_uri", pa.string(), False), ("source_revision", pa.string(), False),
            ("content_hash", pa.string(), False), ("owner", pa.string(), False), ("license", pa.string(), False),
            ("stack", pa.string(), False), ("runtime", pa.string(), False), ("platform", pa.string(), False),
            ("dependencies", pa.string(), False), ("compatibility", pa.string(), False), ("lifecycle", pa.string(), False),
            ("confidence", pa.float64(), False), ("expected_net_value", pa.float64(), False),
            ("embedding_generation", pa.string(), False), ("created_at", pa.string(), False),
            ("updated_at", pa.string(), False), ("last_verified_at", pa.string(), True),
            ("abstraction_status", pa.string(), False),
            ("vector", pa.list_(pa.float32(), 384), False), ("search_text", pa.string(), False),
        ),
        "evidence": (
            ("id", pa.string(), False), ("capability_id", pa.string(), False), ("source_project", pa.string(), False),
            ("evidence_type", pa.string(), False), ("outcome", pa.string(), False), ("metric_name", pa.string(), True),
            ("metric_value", pa.float64(), True), ("confidence", pa.float64(), False), ("observed_at", pa.string(), False),
            ("supporting_uri", pa.string(), True), ("integration_effort", pa.float64(), False),
            ("benefit", pa.float64(), False), ("failure_risk", pa.float64(), False),
        ),
        "relationships": (
            ("id", pa.string(), False), ("source_id", pa.string(), False), ("target_id", pa.string(), False),
            ("relationship_type", pa.string(), False), ("compatibility", pa.string(), False), ("evidence_ids", pa.string(), False),
        ),
        "events": (
            ("id", pa.string(), False), ("capability_id", pa.string(), False), ("event_type", pa.string(), False),
            ("source_context", pa.string(), False), ("occurred_at", pa.string(), False), ("previous_state", pa.string(), True),
            ("resulting_state", pa.string(), False), ("reason", pa.string(), False),
        ),
        "metadata": (("key", pa.string(), False), ("value", pa.string(), False)),
        "requirement_observations": (
            ("id", pa.string(), False), ("requirement", pa.string(), False),
            ("status", pa.string(), False), ("observed_at", pa.string(), False),
            ("linked_capability_id", pa.string(), True),
        ),
        "requirement_events": (
            ("id", pa.string(), False), ("observation_id", pa.string(), False),
            ("event_type", pa.string(), False), ("occurred_at", pa.string(), False),
            ("capability_id", pa.string(), True), ("reason", pa.string(), False),
        ),
    }


@pytest.mark.parametrize("invalid_vector", ([0.5] * 383, [0.5] * 385))
def test_upsert_rejects_vectors_outside_the_fixed_dimension(tmp_path, capability, invalid_vector):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()

    with pytest.raises(ValueError, match="384"):
        repo.upsert_capability(capability, invalid_vector)


def test_capability_search_text_covers_all_retrieval_fields(capability, vector):
    searchable = replace(
        capability,
        constraints=("constraint-marker",),
        dependencies=("dependency-marker",),
    )

    search_text = CapabilityRepository._capability_row(searchable, vector)["search_text"]

    for marker in (
        "Login",
        "OAuth login",
        "OAuth callback",
        "code",
        "Identity and access",
        "authentication",
        "Python",
        "constraint-marker",
        "dependency-marker",
    ):
        assert marker in search_text


def test_repository_rejects_invalid_category_paths_on_write_and_read(tmp_path, capability, vector):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    invalid = replace(capability, id="invalid-category", category_path=("Miscellaneous", "Login"))

    with pytest.raises(ValueError, match="unknown top-level category"):
        repo.upsert_capability(invalid, vector)

    repo._table("capabilities").add([repo._capability_row(invalid, vector)])

    with pytest.raises(ValueError, match="unknown top-level category"):
        repo.get_capability(invalid.id)


def test_audit_append_preserves_first_payload_for_a_stable_id(tmp_path, capability):
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    first_evidence = verification(capability.id)
    first_relationship = relationship(capability.id)
    first_event = discovered_event(capability.id)

    repo.append_evidence(first_evidence)
    repo.append_evidence(replace(first_evidence, outcome="failed"))
    repo.append_relationship(first_relationship)
    repo.append_relationship(replace(first_relationship, relationship_type="supersedes"))
    repo.append_event(first_event)
    repo.append_event(replace(first_event, reason="overwritten"))

    assert repo.list_evidence(capability.id) == (first_evidence,)
    assert repo.list_relationships(capability.id) == (first_relationship,)
    assert repo.list_events(capability.id) == (first_event,)


def test_reopen_recovers_an_uncommitted_multi_table_transaction_byte_for_byte(
    tmp_path,
    capability,
    vector,
):
    database = tmp_path / "memory.lance"
    lock = tmp_path / "locks" / "writer.lock"
    repo = CapabilityRepository.open(database, writer_lock_path=lock)
    repo.initialize()
    before = _tree_bytes(database)

    with repo._writer_lock():
        repo._prepare_transaction_unlocked()
        repo._table("capabilities").add([repo._capability_row(capability, vector)])
        repo._append_once("evidence", repo._evidence_row(verification(capability.id)))
    repo.close()

    recovered = CapabilityRepository.open(database, writer_lock_path=lock)

    assert _tree_bytes(database) == before
    assert recovered.get_capability(capability.id) is None
    assert recovered.list_evidence(capability.id) == ()
    recovered.close()


@pytest.mark.parametrize("fault", ["file-hash", "row-count", "schema-digest", "missing-proof", "missing-file"])
def test_final_transaction_snapshot_integrity_failure_preserves_live_bytes(tmp_path, fault):
    database = tmp_path / "database"
    repo = CapabilityRepository.open(database)
    repo.initialize()
    with repo._writer_lock():
        staging = repo._prepare_transaction_unlocked()
    journal_path = staging.parent / "transaction-journal.json"
    journal = json.loads(journal_path.read_text())
    if fault == "file-hash":
        # A valid extra native-store file is still an unrecorded snapshot mutation.
        (staging / "database" / "unexpected.txt").write_text("unrecorded bytes")
    elif fault == "missing-file":
        next((staging / "database").rglob("*.manifest")).unlink()
    elif fault == "missing-proof":
        journal.pop("snapshot", None)
    else:
        proof = journal.setdefault("snapshot", {}).setdefault("tables", {}).setdefault("metadata", {})
        proof["rows" if fault == "row-count" else "schema"] = 987 if fault == "row-count" else "invalid"
    journal_path.write_text(json.dumps(journal))
    repo._set_metadata_unlocked("live-newer", "keep this state")
    repo.close()
    live_before, recovery_before = _tree_bytes(database), _tree_bytes(staging.parent)
    with pytest.raises((RuntimeError, ValueError, OSError)):
        CapabilityRepository.open(database)
    assert _tree_bytes(database) == live_before
    assert _tree_bytes(staging.parent) == recovery_before


@pytest.mark.parametrize("marked_v2", [False, True])
def test_final_legacy_transaction_journal_retains_valid_recovery(tmp_path, marked_v2):
    database = tmp_path / "database"
    repo = CapabilityRepository.open(database)
    repo.initialize()
    if not marked_v2:
        repo._table("metadata").delete("key = 'authoritative_schema_version'")
    before = _tree_bytes(database)
    with repo._writer_lock():
        staging = repo._prepare_transaction_unlocked()
    journal_path = staging.parent / "transaction-journal.json"
    journal = json.loads(journal_path.read_text())
    journal["format"] = 1
    journal.pop("snapshot", None)
    journal_path.write_text(json.dumps(journal))
    repo._set_metadata_unlocked("uncommitted", True)
    repo.close()
    with CapabilityRepository.open(database) as recovered:
        assert recovered.get_metadata("uncommitted") is None
        assert recovered.get_metadata("authoritative_schema_version") == (2 if marked_v2 else None)
    assert _tree_bytes(database) == before
    assert not journal_path.exists()


def test_repository_reuses_tables_and_close_releases_them_before_connection(
    tmp_path,
) -> None:
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()

    first = repo._table("capabilities")
    second = repo._table("capabilities")
    native = first._table

    assert first is second
    repo.close()
    assert repo._tables == {}
    assert native.is_open() is False
    assert repo._database._conn.is_open() is False


def test_repository_close_propagates_connection_failure(tmp_path, monkeypatch) -> None:
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()

    def fail_close() -> None:
        raise RuntimeError("injected close failure")

    monkeypatch.setattr(repo, "_close_connection_unlocked", fail_close)

    with pytest.raises(RuntimeError, match="injected close failure"):
        repo.close()


def test_repository_retains_table_and_registry_when_native_table_close_fails(
    tmp_path,
    monkeypatch,
) -> None:
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    table = repo._table("capabilities")
    native = table._table
    native_type = type(native)
    real_close = native_type.close

    def fail_selected_table(instance):
        if instance is native:
            raise RuntimeError("injected native table close failure")
        return real_close(instance)

    with monkeypatch.context() as patch:
        patch.setattr(native_type, "close", fail_selected_table)
        with pytest.raises(RuntimeError, match="injected native table close failure"):
            repo.close()

    assert repo._tables["capabilities"] is table
    assert repo in _REPOSITORY_REGISTRY
    assert repo._database._conn.is_open() is True
    repo.close()
    assert native.is_open() is False


def test_table_replacement_closes_the_cached_native_handle(tmp_path) -> None:
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    cached = repo._table("evidence")
    native = cached._table

    repo._overwrite_table_unlocked("evidence", TABLE_SCHEMAS["evidence"], [])

    assert native.is_open() is False
    assert repo._table("evidence") is not cached


def test_generation_reader_is_retained_when_its_native_table_close_fails(
    tmp_path,
    monkeypatch,
) -> None:
    root = CapabilityRepository.open(tmp_path / "root.lance")
    root.initialize()
    generation = CapabilityRepository.open(tmp_path / "generation.lance")
    generation.initialize()
    root._generation_readers["generation-test"] = generation
    table = generation._table("capabilities")
    native = table._table
    native_type = type(native)
    real_close = native_type.close

    def fail_selected_table(instance):
        if instance is native:
            raise RuntimeError("injected generation table close failure")
        return real_close(instance)

    with monkeypatch.context() as patch:
        patch.setattr(native_type, "close", fail_selected_table)
        with pytest.raises(RuntimeError, match="injected generation table close failure"):
            root.close()

    assert root._generation_readers["generation-test"] is generation
    assert generation._tables["capabilities"] is table
    assert root in _REPOSITORY_REGISTRY
    assert generation in _REPOSITORY_REGISTRY
    assert root._database._conn.is_open() is True
    assert generation._database._conn.is_open() is True
    root.close()
    assert native.is_open() is False


def test_failed_evidence_append_does_not_consume_a_commit_sequence(
    tmp_path,
    capability,
    monkeypatch,
) -> None:
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    metadata_before = repo._rows("metadata")

    def fail_append(table_name, row):
        raise RuntimeError("injected evidence append failure")

    monkeypatch.setattr(repo, "_append_once", fail_append)

    with pytest.raises(RuntimeError, match="injected evidence append failure"):
        repo.append_evidence(verification(capability.id))

    assert repo._rows("metadata") == metadata_before


def test_production_runtime_owner_closes_without_pytest_cleanup(tmp_path) -> None:
    script = textwrap.dedent(
        f"""
        from pathlib import Path
        import lancedb.background_loop as background
        from supermind_memory.repository import (
            CapabilityRepository,
            repository_runtime_state,
            shutdown_repository_runtime,
        )

        repo = CapabilityRepository.open(Path({str(tmp_path / 'child.lance')!r}))
        repo.initialize()
        repo._table("capabilities")
        repo.close()
        assert repository_runtime_state() == (0, 0, False)
        reopened = CapabilityRepository.open(Path({str(tmp_path / 'reopened.lance')!r}))
        reopened.initialize()
        reopened.close()
        assert repository_runtime_state() == (0, 0, False)
        shutdown_repository_runtime()
        assert repository_runtime_state() == (0, 0, True)
        assert not background.LOOP.thread.is_alive()
        assert background.LOOP.loop.is_closed()
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        },
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr


def test_atexit_runtime_owner_closes_retained_table_before_natural_exit(
    tmp_path,
) -> None:
    marker = tmp_path / "atexit-marker"
    script = textwrap.dedent(
        f"""
        import atexit
        from pathlib import Path
        import lancedb.background_loop as background
        from supermind_memory.repository import (
            CapabilityRepository,
            repository_runtime_state,
            shutdown_repository_runtime,
        )

        repo = CapabilityRepository.open(Path({str(tmp_path / 'atexit.lance')!r}))
        repo.initialize()
        retained = repo._table("capabilities")
        native = retained._table
        connection = repo._database._conn
        marker = Path({str(marker)!r})

        atexit.unregister(shutdown_repository_runtime)

        def verify_shutdown():
            assert native.is_open() is False
            assert connection.is_open() is False
            assert repository_runtime_state() == (0, 0, True)
            assert not background.LOOP.thread.is_alive()
            assert background.LOOP.loop.is_closed()
            marker.write_text("table-connection-loop", encoding="utf-8")

        atexit.register(verify_shutdown)
        atexit.register(shutdown_repository_runtime)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        },
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert marker.read_text(encoding="utf-8") == "table-connection-loop"


def _schema_fields(schemas):
    return {
        name: tuple((field.name, field.type, field.nullable) for field in schema)
        for name, schema in schemas.items()
    }


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("fault", ["creation", "link", "mismatched-link", "schema", "marker"])
@pytest.mark.parametrize("journal_format", [1, 2])
def test_final_transaction_snapshot_is_validated_before_live_history_replacement(tmp_path, fault, journal_format):
    database = tmp_path / "database"
    repo = CapabilityRepository.open(database)
    repo.initialize()
    observations = [{
        "id": "demand", "requirement": json.dumps({"id": "request", "project_id": "project", "intent": "login"}),
        "status": "linked", "observed_at": "2026-09-06T00:00:00Z", "linked_capability_id": "login",
    }]
    events = [
        {"id": "created", "observation_id": "demand", "event_type": "unmet_observed",
         "occurred_at": "2026-09-06T00:00:00Z", "capability_id": None, "reason": "unmet login"},
        {"id": "linked", "observation_id": "demand", "event_type": "implementation_linked",
         "occurred_at": "2026-09-06T01:00:00Z", "capability_id": "login", "reason": "verified login"},
    ]
    repo._table("requirement_observations").add(observations)
    damaged_events = [dict(row) for row in events]
    if fault == "creation":
        damaged_events.pop(0)
    elif fault == "link":
        damaged_events.pop(1)
    elif fault == "mismatched-link":
        damaged_events[1]["capability_id"] = "different-login"
    repo._table("requirement_events").add(damaged_events)
    if fault == "schema":
        repo._overwrite_table_unlocked("requirement_events", _broken_demand_schema("requirement_events", "missing-field"), [])
    if fault == "marker":
        repo._set_metadata_unlocked("authoritative_schema_version", 3)
    # This snapshot is internally corrupt; live history is repaired before reopen.
    # Recovery must prevalidate even a byte-intact snapshot created from this state.
    with repo._writer_lock():
        staging = repo._prepare_transaction_unlocked()
    if journal_format == 1:
        journal_path = staging.parent / "transaction-journal.json"
        journal = json.loads(journal_path.read_text())
        journal["format"] = 1
        journal.pop("snapshot", None)
        journal_path.write_text(json.dumps(journal))
    repo._overwrite_table_unlocked("requirement_events", TABLE_SCHEMAS["requirement_events"], events)
    repo._set_metadata_unlocked("authoritative_schema_version", 2)
    repo.close()
    live_before = _tree_bytes(database)
    recovery_before = _tree_bytes(staging.parent)
    try:
        opened = CapabilityRepository.open(database)
    except RuntimeError as error:
        assert "corrupt" in str(error) or "snapshot" in str(error)
    else:
        opened.close()
        pytest.fail("corrupt transaction snapshot replaced live history")
    assert _tree_bytes(database) == live_before
    assert _tree_bytes(staging.parent) == recovery_before
