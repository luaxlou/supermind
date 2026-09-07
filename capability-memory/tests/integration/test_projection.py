"""Replay projection exercises the real seven-table store and recovery boundary."""

from dataclasses import asdict, replace
import json
import multiprocessing
import os
from pathlib import Path

import pyarrow as pa
import pytest

from supermind_memory.event_model import AuthorityEvent
from supermind_memory.projection import authority_snapshot, compare_projection, project_authority
from supermind_memory.replay import replay
from supermind_memory.repository import CapabilityRepository
from supermind_memory.schema import EMBEDDING_DIMENSION
from supermind_memory.config import MemoryPaths
from supermind_memory.health import HealthManager
from supermind_memory.types import RequirementEvent, RequirementObservation, RequirementProfile
from test_health import _capability, _event, _evidence, _relationship


def authority(entity_type, entity_id, payload, *, event_id=None, parents=(), operation=None):
    operations = {"capability": "registered", "evidence": "observed", "relationship": "declared",
                  "audit": "observed", "demand": "observed", "metadata": "set", "reuse_outcome": "recorded"}
    return AuthorityEvent.create(
        event_id=event_id or f"authority-{entity_id}", device_id="device-a",
        entity_type=entity_type, entity_id=entity_id, operation=operation or operations[entity_type],
        parent_event_ids=parents, occurred_at="2026-09-06T12:00:00Z", payload=json.loads(json.dumps(payload)),
    )


@pytest.fixture
def empty_repository(tmp_path):
    with CapabilityRepository.open(tmp_path / "database") as repository:
        repository.initialize()
        yield repository


@pytest.fixture
def authority_events():
    observation = RequirementObservation("demand-1", RequirementProfile("req-1", "project", "login"),
                                         "linked", "2026-09-06T12:00:00Z", "login")
    history = [RequirementEvent("created", "demand-1", "unmet_observed", observation.observed_at),
               RequirementEvent("linked", "demand-1", "implementation_linked", observation.observed_at, "login")]
    return (
        authority("capability", "login", asdict(_capability("login"))),
        authority("evidence", "ev-login", asdict(replace(_evidence("login"), id="ev-login"))),
        authority("relationship", "relationship-1", asdict(_relationship("login"))),
        authority("audit", "event-1", asdict(_event("login"))),
        authority("metadata", "source", {"value": {"revision": 1}}),
        authority("demand", "demand-1", {"observation": asdict(observation), "events": [asdict(e) for e in history]}),
        authority("reuse_outcome", "reuse-1", asdict(replace(_evidence("login"), id="reuse-1", evidence_type="reuse"))),
    )


def test_projection_reconstructs_all_authoritative_tables(empty_repository, embeddings, authority_events):
    result = replay(authority_events)
    digest = project_authority(result, empty_repository, embeddings)
    assert digest.event_set == result.digest
    assert {item.id for item in empty_repository.list_capabilities()} == {"login"}
    assert {item.id for item in empty_repository.list_evidence("login")} == {"ev-login", "reuse-1"}
    assert {item.id for item in empty_repository.list_relationships("login")} == {"relationship-1"}
    assert {item.id for item in empty_repository.list_events("login")} == {"event-1"}
    assert {item.id for item in empty_repository.list_requirement_observations()} == {"demand-1"}
    assert {item.id for item in empty_repository.list_requirement_events("demand-1")} == {"created", "linked"}
    assert empty_repository.get_metadata("source") == {"revision": 1}
    assert compare_projection(result, empty_repository).equivalent is True


def test_old_projection_without_abstraction_column_is_rebuilt_from_legacy_events(empty_repository, embeddings):
    payload = asdict(_capability("legacy"))
    payload.pop("abstraction_status")
    payload["category_path"] = ["Code and components"]
    result = replay((authority("capability", "legacy", payload),))
    project_authority(result, empty_repository, embeddings)
    table = empty_repository._table("capabilities")
    old_schema = table.schema.remove(table.schema.get_field_index("abstraction_status"))
    rows = empty_repository._rows("capabilities")
    for row in rows:
        row.pop("abstraction_status")
    empty_repository._overwrite_table_unlocked("capabilities", old_schema, rows)
    project_authority(result, empty_repository, embeddings)
    item = empty_repository.get_capability("legacy")
    assert item.abstraction_status.value == "pending"
    assert item.category_path == ("code",)
    assert compare_projection(result, empty_repository).equivalent


def test_replacement_removes_rows_absent_from_replay(empty_repository, embeddings, authority_events):
    project_authority(replay(authority_events), empty_repository, embeddings)
    empty_repository.set_metadata("stale", True)
    result = replay(())
    project_authority(result, empty_repository, embeddings)
    assert empty_repository.list_capabilities() == ()
    assert empty_repository.list_evidence("login") == ()
    assert empty_repository.list_requirement_events("demand-1") == ()
    assert empty_repository.get_metadata("stale") is None
    assert compare_projection(result, empty_repository).equivalent


def test_invalid_demand_history_preserves_previous_projection(empty_repository, embeddings, authority_events):
    result = replay(authority_events)
    project_authority(result, empty_repository, embeddings)
    demand = authority_events[5].to_document()["payload"]
    demand["events"] = demand["events"][1:]
    invalid = replay((authority("demand", "demand-1", demand),))
    with pytest.raises((ValueError, RuntimeError), match="creation event"):
        project_authority(invalid, empty_repository, embeddings)
    assert compare_projection(result, empty_repository).equivalent


def test_conflict_keeps_diagnostic_ancestor_and_unrelated_capability_searchable(empty_repository, embeddings):
    base = authority("capability", "login", asdict(_capability("login")))
    siblings = [authority("capability", "login", asdict(replace(_capability("login"), name=name)),
                          event_id=name, parents=(base.event_id,), operation="updated") for name in ("left", "right")]
    other = authority("capability", "other", asdict(_capability("other")))
    result = replay((base, *siblings, other))
    project_authority(result, empty_repository, embeddings)
    assert {c.id for c in empty_repository.list_capabilities()} == {"other"}
    diagnostics = empty_repository.get_metadata("authority_conflicts")
    assert diagnostics[0]["event_ids"] == ["left", "right"]
    assert diagnostics[0]["ancestor"]["event_id"] == base.event_id
    rows, _, _ = empty_repository.hybrid_search("login", embeddings.embed_query("login"), "id IS NOT NULL", 10)
    assert {row["id"] for row in rows} == {"other"}
    resolution = authority("capability", "login", asdict(_capability("login")),
                           event_id="resolution", parents=("left", "right"), operation="resolved")
    project_authority(replay((base, *siblings, other, resolution)), empty_repository, embeddings)
    assert {c.id for c in empty_repository.list_capabilities()} == {"login", "other"}
    assert empty_repository.get_metadata("authority_conflicts") == []


@pytest.mark.parametrize("table", ["evidence", "metadata", "requirement_events"])
def test_comparison_detects_non_capability_drift(empty_repository, embeddings, authority_events, table):
    result = replay(authority_events)
    project_authority(result, empty_repository, embeddings)
    empty_repository._table(table).delete("true")
    comparison = compare_projection(result, empty_repository)
    assert not comparison.equivalent
    assert table in comparison.differences


@pytest.mark.parametrize("invalid_value", [float("nan"), 1e100])
def test_embedding_failure_preserves_previous_projection(empty_repository, embeddings, authority_events, invalid_value):
    project_authority(replay(()), empty_repository, embeddings)
    class BrokenEmbeddings:
        def embed_documents(self, texts):
            return [[invalid_value] * EMBEDDING_DIMENSION for _ in texts]
    with pytest.raises(ValueError, match="finite"):
        project_authority(replay(authority_events), empty_repository, BrokenEmbeddings())
    assert compare_projection(replay(()), empty_repository).equivalent


def test_projection_embeds_capability_documents_not_queries(empty_repository, embeddings, authority_events):
    class DocumentsOnly:
        def embed_documents(self, texts):
            return embeddings.embed_documents(texts)

        def embed_query(self, text):
            pytest.fail("capability documents cannot use the query embedding route")

    result = replay(authority_events)
    project_authority(result, empty_repository, DocumentsOnly())
    assert compare_projection(result, empty_repository).equivalent


def test_replace_authority_validates_typed_evidence_before_activation(empty_repository, embeddings, authority_events):
    result = replay(authority_events)
    project_authority(result, empty_repository, embeddings)
    snapshot = authority_snapshot(result.entities, event_set_digest=result.digest)
    invalid = replace(snapshot, evidence=(replace(snapshot.evidence[0], confidence=2.0),))
    with pytest.raises(ValueError, match="confidence"):
        empty_repository.replace_authority(invalid, {"login": [0.0] * EMBEDDING_DIMENSION})
    assert compare_projection(result, empty_repository).equivalent


def test_replace_authority_validates_typed_capability_category(empty_repository, embeddings, authority_events):
    result = replay(authority_events)
    project_authority(result, empty_repository, embeddings)
    snapshot = authority_snapshot(result.entities, event_set_digest=result.digest)
    invalid = replace(snapshot, capabilities=(replace(snapshot.capabilities[0], category_path=("unknown",)),))
    with pytest.raises(ValueError):
        empty_repository.replace_authority(invalid, {"login": [0.0] * EMBEDDING_DIMENSION})
    assert compare_projection(result, empty_repository).equivalent


@pytest.mark.parametrize("phase", ["candidate_writing", "old_moved", "candidate_activated", "commit_durable"])
def test_projection_crash_recovers_one_complete_version(empty_repository, embeddings, authority_events, phase):
    old = replay(())
    project_authority(old, empty_repository, embeddings)
    database_path = empty_repository._database_path
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_projection,
        args=(str(database_path), [event.to_bytes() for event in authority_events], phase),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 23
    expected = replay(authority_events) if phase == "commit_durable" else old
    with CapabilityRepository.open(database_path) as reopened:
        assert compare_projection(expected, reopened).equivalent
        reopened._validate_requirement_history_unlocked()
    # A pre-existing connection must refresh replaced table handles as well.
    assert compare_projection(expected, empty_repository).equivalent
    assert not empty_repository._transaction_journal_path.exists()
    assert list(empty_repository._transaction_journal_path.parent.glob("projection-*")) == []


def _crash_projection(database_path, event_bytes, phase):
    from conftest import KeywordEmbeddingProvider
    import supermind_memory.repository as repository_module

    repository = CapabilityRepository.open(Path(database_path))
    original_replace = os.replace
    def replace_then_crash(source, destination):
        original_replace(source, destination)
        if (phase == "old_moved" and Path(source) == Path(database_path)) or (
            phase == "candidate_activated" and Path(destination) == Path(database_path)
        ):
            os._exit(23)
    repository_module.os.replace = replace_then_crash
    if phase == "candidate_writing":
        overwrite = CapabilityRepository._overwrite_table_unlocked
        def overwrite_then_crash(self, name, schema, rows):
            overwrite(self, name, schema, rows)
            if self._database_path.parent.name.startswith("projection-") and name == "evidence":
                os._exit(23)
        CapabilityRepository._overwrite_table_unlocked = overwrite_then_crash
    if phase == "commit_durable":
        repository._clear_transaction_unlocked = lambda staging: os._exit(23)
    project_authority(replay(tuple(AuthorityEvent.from_bytes(value) for value in event_bytes)), repository, KeywordEmbeddingProvider())


@pytest.mark.parametrize("field,value", [("id", "wrong-id"), ("category_path", ["unknown"]), ("artifact_type", "not-a-type")])
def test_invalid_capability_payload_cannot_replace_authority(empty_repository, embeddings, field, value):
    project_authority(replay(()), empty_repository, embeddings)
    payload = asdict(_capability("login"))
    payload[field] = value
    with pytest.raises(ValueError):
        project_authority(replay((authority("capability", "login", payload),)), empty_repository, embeddings)
    assert compare_projection(replay(()), empty_repository).equivalent


def test_schema_numeric_normalization_accepts_integer_json_numbers(empty_repository, embeddings):
    payload = asdict(_capability("login"))
    payload["confidence"] = 1
    payload["expected_net_value"] = 2
    result = replay((authority("capability", "login", payload),))
    project_authority(result, empty_repository, embeddings)
    assert empty_repository.get_capability("login").confidence == 1.0
    assert compare_projection(result, empty_repository).equivalent


@pytest.mark.parametrize("missing_event", ["created", "linked"])
def test_valid_replay_repairs_corrupt_local_demand_history(empty_repository, embeddings, authority_events, missing_event):
    result = replay(authority_events)
    project_authority(result, empty_repository, embeddings)
    empty_repository._table("requirement_events").delete(f"id = '{missing_event}'")
    # Legacy initialization must still reject an invalid authoritative source.
    with pytest.raises(RuntimeError, match="creation event|matching link event"):
        empty_repository.initialize()

    project_authority(result, empty_repository, embeddings)

    assert compare_projection(result, empty_repository).equivalent
    assert {event.id for event in empty_repository.list_requirement_events("demand-1")} == {"created", "linked"}
    root = empty_repository._database_path.parent
    paths = MemoryPaths(
        root=root,
        config=root / "config.json",
        checkout=root / "repository",
        database=root / "derived" / "database",
        model_cache=root / "model-cache",
        locks=root / "locks",
        generations=root / "derived" / "generations",
    )
    health = HealthManager(paths, empty_repository, embeddings,
                           authority_mode="events-v1", expected_authority_digest=result.digest)
    assert health.ensure_healthy().healthy


def test_invalid_replay_cannot_overwrite_a_corrupt_projection(empty_repository, embeddings, authority_events):
    result = replay(authority_events)
    project_authority(result, empty_repository, embeddings)
    empty_repository._table("requirement_events").delete("id = 'linked'")
    before = empty_repository._authoritative_fingerprint()
    demand = authority_events[5].to_document()["payload"]
    demand["events"] = []
    invalid = replay((authority("demand", "demand-1", demand),))
    with pytest.raises(RuntimeError, match="creation event"):
        project_authority(invalid, empty_repository, embeddings)
    assert empty_repository._authoritative_fingerprint() == before
    assert not empty_repository._transaction_journal_path.exists()


@pytest.mark.parametrize("missing_event", ["created", "linked"])
@pytest.mark.parametrize("phase", ["candidate_writing", "old_moved", "candidate_activated", "commit_durable"])
def test_corrupt_projection_replacement_crash_restores_exact_old_state_then_replays(
    empty_repository, embeddings, authority_events, missing_event, phase,
):
    result = replay(authority_events)
    project_authority(result, empty_repository, embeddings)
    empty_repository._table("requirement_events").delete(f"id = '{missing_event}'")
    corrupted_fingerprint = empty_repository._authoritative_fingerprint()
    database_path = empty_repository._database_path
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_projection,
        args=(str(database_path), [event.to_bytes() for event in authority_events], phase),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 23
    with CapabilityRepository.open(database_path) as recovered:
        if phase != "commit_durable":
            assert recovered._authoritative_fingerprint() == corrupted_fingerprint
            with pytest.raises(RuntimeError, match="creation event|matching link event"):
                recovered.initialize()
        project_authority(result, recovered, embeddings)
        assert compare_projection(result, recovered).equivalent
        recovered._validate_requirement_history_unlocked()
    assert not empty_repository._transaction_journal_path.exists()
    assert list(empty_repository._transaction_journal_path.parent.glob("projection-*")) == []


def test_corrupt_projection_rollback_still_requires_exact_snapshot_bytes(empty_repository, embeddings, authority_events):
    result = replay(authority_events)
    project_authority(result, empty_repository, embeddings)
    empty_repository._table("requirement_events").delete("id = 'created'")
    database_path = empty_repository._database_path
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_projection,
        args=(str(database_path), [event.to_bytes() for event in authority_events], "candidate_activated"),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 23
    journal = json.loads(empty_repository._transaction_journal_path.read_text())
    snapshot = empty_repository._transaction_journal_path.parent / journal["staging"] / "database"
    (snapshot / "unrecorded.txt").write_text("unrecorded snapshot content")
    live_files = {path.relative_to(database_path): path.read_bytes() for path in database_path.rglob("*") if path.is_file()}
    with pytest.raises(RuntimeError, match="snapshot hashes"):
        CapabilityRepository.open(database_path)
    assert {path.relative_to(database_path): path.read_bytes() for path in database_path.rglob("*") if path.is_file()} == live_files


def test_corrupt_old_projection_does_not_relax_candidate_schema_validation(empty_repository, embeddings, authority_events, monkeypatch):
    result = replay(authority_events)
    project_authority(result, empty_repository, embeddings)
    empty_repository._table("requirement_events").delete("id = 'created'")
    before = empty_repository._authoritative_fingerprint()
    overwrite = CapabilityRepository._overwrite_table_unlocked
    def write_wrong_schema(self, name, schema, rows):
        if self._database_path.parent.name.startswith("projection-") and name == "capabilities":
            schema = schema.set(schema.get_field_index("vector"), pa.field("vector", pa.list_(pa.float64(), EMBEDDING_DIMENSION), nullable=False))
        overwrite(self, name, schema, rows)
    monkeypatch.setattr(CapabilityRepository, "_overwrite_table_unlocked", write_wrong_schema)
    with pytest.raises(RuntimeError, match="projection schema"):
        project_authority(result, empty_repository, embeddings)
    assert empty_repository._authoritative_fingerprint() == before
    assert not empty_repository._transaction_journal_path.exists()
