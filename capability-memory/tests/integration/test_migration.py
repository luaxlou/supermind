"""Legacy schema-v2 authority exports into immutable replay events."""

from __future__ import annotations

from dataclasses import asdict, replace
import json

import pytest

from supermind_memory.event_model import AuthorityEvent, canonical_json
from supermind_memory.event_store import MAX_EVENT_FILE_BYTES, EventStore
from supermind_memory.migration import MigrationBlocked, export_embedded_store
from supermind_memory.projection import project_authority
from supermind_memory.replay import replay
from supermind_memory.repository import CapabilityRepository
from supermind_memory.types import (
    ArtifactType,
    Capability,
    Event,
    Evidence,
    Lifecycle,
    Relationship,
    RequirementEvent,
    RequirementObservation,
    RequirementProfile,
)


def _capability(capability_id: str) -> Capability:
    return Capability(
        capability_id, "OAuth login", "Reusable login flow",
        ("Code and components", "Identity and access"), ("authentication",),
        "OAuth callback", (), ArtifactType.CODE, "/project/login.py", "abc123", "hash-a",
        "team", "MIT", ("Python",), ("CPython",), ("macOS",), ("authlib",),
        ("OAuth 2.0",), Lifecycle.CANDIDATE, 1.0, 2.0, "generation-1",
        "2026-09-04T00:00:00Z", "2026-09-04T00:00:00Z", None,
    )


def _evidence(capability_id: str) -> Evidence:
    return Evidence(
        "evidence-1", capability_id, "project-a", "verification", "passed",
        None, None, 1.0, "2026-09-04T00:00:00Z", None,
    )


def _relationship(capability_id: str) -> Relationship:
    return Relationship(
        "relationship-1", capability_id, "dependency-1", "depends_on",
        ("OAuth 2.0",), ("evidence-1",),
    )


def _event(capability_id: str) -> Event:
    return Event(
        "event-1", capability_id, "verified", "project-a", "2026-09-04T00:00:00Z",
        Lifecycle.OBSERVED, Lifecycle.CANDIDATE, "verified during migration fixture",
    )


def _raw_authority_rows(repository: CapabilityRepository) -> dict[str, list[dict[str, object]]]:
    reserved = {
        "authoritative_schema_version",
        "evidence-commit-order-v1",
        "authority_event_set_digest",
        "authority_materialized_digest",
        "authority_conflicts",
    }
    result = {}
    for table in (
        "capabilities", "evidence", "relationships", "events", "metadata",
        "requirement_observations", "requirement_events",
    ):
        rows = repository._rows(table)
        if table == "capabilities":
            rows = [
                {key: value for key, value in row.items() if key not in {"vector", "search_text"}}
                for row in rows
            ]
        if table == "metadata":
            rows = [row for row in rows if row["key"] not in reserved]
        key = "key" if table == "metadata" else "id"
        result[table] = sorted(rows, key=lambda row: row[key])
    return result


@pytest.fixture
def populated_repository(tmp_path):
    with CapabilityRepository.open(tmp_path / "source" / "database") as repository:
        repository.initialize()
        capability = _capability("login")
        repository.upsert_capability(capability, [0.0] * 384)
        # Commit order is intentionally different from stable-ID order. The
        # exported authority is required to use the projection's deterministic
        # stable-ID ordering, never timestamps or local order metadata.
        repository.append_evidence(replace(_evidence("login"), id="z-verification"))
        repository.append_evidence(
            replace(_evidence("login"), id="a-reuse", evidence_type="reuse", outcome="success")
        )
        repository.append_relationship(_relationship("login"))
        repository.append_event(_event("login"))
        repository.set_metadata("source-registry", {"revision": 7})
        observation = RequirementObservation(
            "demand-1",
            RequirementProfile("requirement-1", "project-a", "login"),
            "linked",
            "2026-09-06T12:00:00Z",
            "login",
        )
        history = (
            RequirementEvent(
                "demand-created", "demand-1", "unmet_observed", observation.observed_at,
            ),
            RequirementEvent(
                "demand-linked", "demand-1", "implementation_linked",
                "2026-09-06T12:01:00Z", "login", "implemented",
            ),
        )
        with repository.atomic_write():
            repository._append_once(
                "requirement_observations", repository._requirement_observation_row(observation),
            )
            for event in history:
                repository._append_once(
                    "requirement_events", repository._requirement_event_row(event),
                )
        yield repository


@pytest.fixture
def empty_event_store(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    return EventStore(root)


def test_export_replay_preserves_all_rows_and_demand_history(
    tmp_path, populated_repository, empty_event_store, embeddings,
):
    assert tuple(
        item.id for item in populated_repository.list_evidence("login")
    ) == ("z-verification", "a-reuse")
    source = populated_repository.authority_snapshot()
    source_rows = _raw_authority_rows(populated_repository)
    assert tuple(item.id for item in source.evidence) == ("a-reuse", "z-verification")

    report = export_embedded_store(
        populated_repository, empty_event_store, "migration-device",
    )
    replayed = replay(empty_event_store.load_all())
    with CapabilityRepository.open(tmp_path / "rebuilt" / "database") as rebuilt:
        rebuilt.initialize()
        project_authority(replayed, rebuilt, embeddings)

        assert report.equivalent is True
        assert rebuilt.authority_snapshot() == source
        assert _raw_authority_rows(rebuilt) == source_rows
        assert tuple(item.id for item in rebuilt.list_evidence("login")) == (
            "a-reuse", "z-verification",
        )

    entities = {(event.entity_type, event.entity_id): event for event in empty_event_store.load_all()}
    assert entities[("demand", "demand-1")].to_document()["payload"] == {
        "events": [
            asdict(RequirementEvent(
                "demand-created", "demand-1", "unmet_observed", "2026-09-06T12:00:00Z",
            )),
            asdict(RequirementEvent(
                "demand-linked", "demand-1", "implementation_linked",
                "2026-09-06T12:01:00Z", "login", "implemented",
            )),
        ],
        "observation": json.loads(json.dumps(asdict(source.requirement_observations[0]))),
    }
    assert entities[("audit", "event-1")].to_document()["payload"] == asdict(_event("login"))
    assert entities[("reuse_outcome", "a-reuse")].to_document()["payload"] == asdict(
        replace(_evidence("login"), id="a-reuse", evidence_type="reuse", outcome="success")
    )
    assert not any(event.entity_id == "evidence-commit-order-v1" for event in entities.values())
    assert not any(event.entity_id == "authoritative_schema_version" for event in entities.values())


def test_export_preserves_non_ascii_metadata_raw_rows(
    tmp_path, populated_repository, empty_event_store, embeddings,
):
    populated_repository.set_metadata("localized", {"label": "登录能力"})
    source_rows = _raw_authority_rows(populated_repository)

    export_embedded_store(populated_repository, empty_event_store, "caller-a")
    with CapabilityRepository.open(tmp_path / "localized-rebuilt" / "database") as rebuilt:
        rebuilt.initialize()
        project_authority(replay(empty_event_store.load_all()), rebuilt, embeddings)

        assert _raw_authority_rows(rebuilt) == source_rows
        assert rebuilt.authority_snapshot() == populated_repository.authority_snapshot()


def test_export_refuses_incomplete_authoritative_history(
    populated_repository, empty_event_store,
):
    populated_repository._table("requirement_events").delete("id = 'demand-created'")

    with pytest.raises(MigrationBlocked, match="authoritative_store_corrupt"):
        export_embedded_store(populated_repository, empty_event_store, "migration-device")

    assert empty_event_store.load_all() == ()
    assert not (empty_event_store.root / ".supermind-migration-v1.json").exists()


@pytest.mark.parametrize(
    ("table", "column", "raw"),
    [
        ("metadata", "value", '{"revision":1,"revision":2}'),
        ("capabilities", "facets", '{"authentication":true}'),
        ("capabilities", "facets", '["authentication" ]'),
    ],
)
def test_export_rejects_lossy_or_noncanonical_legacy_json(
    populated_repository, empty_event_store, table, column, raw,
):
    if table == "metadata":
        row = {"key": "source-registry", "value": raw}
        key = "key"
    else:
        row = next(
            item for item in populated_repository._rows(table) if item["id"] == "login"
        )
        row[column] = raw
        key = "id"
    populated_repository._table(table).merge_insert(key).when_matched_update_all().execute([row])

    with pytest.raises(MigrationBlocked, match="authoritative_store_corrupt") as blocked:
        export_embedded_store(populated_repository, empty_event_store, "caller-a")

    assert blocked.value.code == "authoritative_store_corrupt"
    assert empty_event_store.load_all() == ()
    assert not (empty_event_store.root / ".supermind-migration-v1.json").exists()


def test_export_preflights_the_event_file_limit_before_writing(
    populated_repository, empty_event_store,
):
    populated_repository.set_metadata(
        "oversized", {f"field-{index}": "x" * 2_000 for index in range(600)},
    )

    with pytest.raises(MigrationBlocked, match="migration_source_unrepresentable") as blocked:
        export_embedded_store(populated_repository, empty_event_store, "migration-device")

    assert blocked.value.code == "migration_source_unrepresentable"
    assert empty_event_store.load_all() == ()
    assert not (empty_event_store.root / ".supermind-migration-v1.json").exists()


@pytest.mark.parametrize("metadata_key", ["contains spaces", "contains/slash"])
def test_export_classifies_unrepresentable_legacy_entity_ids(
    populated_repository, empty_event_store, metadata_key,
):
    populated_repository.set_metadata(metadata_key, {"preserve": True})

    with pytest.raises(MigrationBlocked, match="migration_source_unrepresentable") as blocked:
        export_embedded_store(populated_repository, empty_event_store, "caller-a")

    assert blocked.value.code == "migration_source_unrepresentable"
    assert empty_event_store.load_all() == ()


def test_export_classifies_event_container_limit_without_lossy_chunking(
    populated_repository, empty_event_store,
):
    populated_repository.set_metadata("many-values", list(range(1_025)))

    with pytest.raises(MigrationBlocked, match="migration_source_unrepresentable") as blocked:
        export_embedded_store(populated_repository, empty_event_store, "caller-a")

    assert blocked.value.code == "migration_source_unrepresentable"
    assert empty_event_store.load_all() == ()


def test_export_classifies_source_text_the_event_privacy_envelope_would_change(
    populated_repository, empty_event_store,
):
    row = next(
        item for item in populated_repository._rows("capabilities") if item["id"] == "login"
    )
    row["summary"] = "password=unredacted-legacy-material"
    populated_repository._table("capabilities").merge_insert("id").when_matched_update_all().execute([row])

    with pytest.raises(MigrationBlocked, match="migration_source_unrepresentable") as blocked:
        export_embedded_store(populated_repository, empty_event_store, "caller-a")

    assert blocked.value.code == "migration_source_unrepresentable"
    assert empty_event_store.load_all() == ()


def test_export_classifies_long_demand_history_without_lossy_chunking(
    populated_repository, empty_event_store,
):
    populated_repository._table("requirement_events").add(
        [
            populated_repository._requirement_event_row(
                RequirementEvent(
                    f"history-{index:04d}", "demand-1", "reviewed",
                    "2026-09-06T12:02:00Z", reason="preserve this history",
                )
            )
            for index in range(1_023)
        ]
    )

    with pytest.raises(MigrationBlocked, match="migration_source_unrepresentable") as blocked:
        export_embedded_store(populated_repository, empty_event_store, "caller-a")

    assert blocked.value.code == "migration_source_unrepresentable"
    assert empty_event_store.load_all() == ()


def test_export_report_counts_each_current_entity_type(populated_repository, empty_event_store):
    report = export_embedded_store(populated_repository, empty_event_store, "migration-device")

    assert dict(report.entity_counts) == {
        "audit": 1,
        "capability": 1,
        "demand": 1,
        "evidence": 1,
        "metadata": 1,
        "relationship": 1,
        "reuse_outcome": 1,
    }
    journal = json.loads(
        (empty_event_store.root / ".supermind-migration-v1.json").read_text()
    )
    assert journal["source_digest"] == report.source_digest
    assert journal["final_replay_digest"] == report.event_set_digest
    assert journal["intended_event_ids"] == sorted(
        event.event_id for event in empty_event_store.load_all()
    )
    assert len(journal["completed_paths"]) == 7


def test_initiating_device_does_not_change_canonical_migration_events(
    tmp_path, populated_repository,
):
    roots = (tmp_path / "caller-a", tmp_path / "caller-b")
    for root in roots:
        root.mkdir()
    stores = tuple(EventStore(root) for root in roots)

    first = export_embedded_store(populated_repository, stores[0], "caller-a")
    second = export_embedded_store(populated_repository, stores[1], "caller-b")

    assert first == second
    assert tuple(event.to_bytes() for event in stores[0].load_all()) == tuple(
        event.to_bytes() for event in stores[1].load_all()
    )
    assert {event.device_id for event in stores[0].load_all()} == {"migration-v1"}
    assert json.loads((roots[0] / ".supermind-migration-v1.json").read_text())["device_id"] == "caller-a"
    assert json.loads((roots[1] / ".supermind-migration-v1.json").read_text())["device_id"] == "caller-b"


@pytest.mark.parametrize(
    ("device_id", "case"),
    [
        ("", "empty"),
        ("caller with spaces", "spaces"),
        ("x" * 129, "overlength"),
        (17, "number"),
        (None, "null"),
        (["caller-a"], "array"),
        ("client_secret=private-device-material", "sensitive-invalid"),
    ],
    ids=["empty", "spaces", "overlength", "number", "null", "array", "sensitive-invalid"],
)
def test_invalid_initiating_device_is_rejected_before_target_access(
    populated_repository, empty_event_store, monkeypatch, device_id, case,
):
    target_accesses = []

    def unexpected_target_access(*_args, **_kwargs):
        target_accesses.append(case)
        raise AssertionError("migration target was accessed")

    monkeypatch.setattr(empty_event_store, "load_all", unexpected_target_access)
    monkeypatch.setattr(empty_event_store, "append", unexpected_target_access)

    with pytest.raises(MigrationBlocked) as raised:
        export_embedded_store(populated_repository, empty_event_store, device_id)

    blocked = raised.value
    assert blocked.code == "migration_input_invalid"
    assert blocked.message == (
        "migration_input_invalid: initiating device must be a stable identifier"
    )
    assert blocked.attempts == ("initiating device must be a stable identifier",)
    assert "private-device-material" not in str(blocked)
    assert "private-device-material" not in repr(blocked.attempts)
    assert target_accesses == []
    assert list(empty_event_store.root.iterdir()) == []


def test_sensitive_initiating_device_is_sanitized_in_journal(
    populated_repository, empty_event_store,
):
    initiating_device = "password:private-device-material"

    export_embedded_store(populated_repository, empty_event_store, initiating_device)

    journal_path = empty_event_store.root / ".supermind-migration-v1.json"
    raw_journal = journal_path.read_text(encoding="utf-8")
    journal = json.loads(raw_journal)
    assert journal["device_id"].startswith("redacted-")
    assert initiating_device not in raw_journal
    assert "private-device-material" not in raw_journal
    assert {event.device_id for event in empty_event_store.load_all()} == {"migration-v1"}


def test_interrupted_export_resumes_to_the_same_event_set_as_a_clean_export(
    tmp_path, populated_repository, empty_event_store, monkeypatch,
):
    append = empty_event_store.append
    writes = 0

    def fail_after_third_event(event):
        nonlocal writes
        if writes == 3:
            raise OSError("injected event write failure")
        writes += 1
        return append(event)

    monkeypatch.setattr(empty_event_store, "append", fail_after_third_event)
    with pytest.raises(OSError, match="injected event write failure"):
        export_embedded_store(populated_repository, empty_event_store, "migration-device")
    assert len(empty_event_store.load_all()) == 3
    partial_journal = json.loads(
        (empty_event_store.root / ".supermind-migration-v1.json").read_text()
    )
    assert len(partial_journal["completed_paths"]) == 3
    assert partial_journal["final_replay_digest"] is None

    monkeypatch.setattr(empty_event_store, "append", append)
    resumed = export_embedded_store(
        populated_repository, empty_event_store, "migration-device",
    )
    event_bytes = tuple(event.to_bytes() for event in empty_event_store.load_all())

    clean_root = tmp_path / "clean-checkout"
    clean_root.mkdir()
    clean_store = EventStore(clean_root)
    clean = export_embedded_store(populated_repository, clean_store, "migration-device")

    assert resumed == clean
    assert event_bytes == tuple(event.to_bytes() for event in clean_store.load_all())
    assert len(event_bytes) == 7


def test_completed_export_is_idempotent(populated_repository, empty_event_store):
    first = export_embedded_store(
        populated_repository, empty_event_store, "migration-device",
    )
    paths = tuple(
        empty_event_store.root
        / "events"
        / "v1"
        / event.device_id
        / event.occurred_at[:7]
        / f"{event.event_id}.json"
        for event in empty_event_store.load_all()
    )
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    second = export_embedded_store(
        populated_repository, empty_event_store, "migration-device",
    )

    assert second == first
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before


def test_altered_existing_migration_event_is_a_collision(
    populated_repository, empty_event_store,
):
    export_embedded_store(populated_repository, empty_event_store, "migration-device")
    original = empty_event_store.load_all()[0]
    altered = AuthorityEvent.create(
        event_id=original.event_id,
        device_id=original.device_id,
        entity_type=original.entity_type,
        entity_id=original.entity_id,
        operation=original.operation,
        parent_event_ids=original.parent_event_ids,
        occurred_at=original.occurred_at,
        payload={"changed": True},
    )
    path = (
        empty_event_store.root
        / "events"
        / "v1"
        / original.device_id
        / original.occurred_at[:7]
        / f"{original.event_id}.json"
    )
    path.write_bytes(altered.to_bytes())

    with pytest.raises(MigrationBlocked, match="migration_event_collision") as blocked:
        export_embedded_store(populated_repository, empty_event_store, "migration-device")

    assert blocked.value.code == "migration_event_collision"
    assert path.read_bytes() == altered.to_bytes()


@pytest.mark.parametrize("damage", ["malformed_json", "bad_hash", "oversized", "wrong_path"])
def test_raw_target_corruption_is_reported_as_a_migration_collision(
    populated_repository, empty_event_store, damage,
):
    export_embedded_store(populated_repository, empty_event_store, "caller-a")
    original = empty_event_store.load_all()[0]
    path = (
        empty_event_store.root
        / "events"
        / "v1"
        / original.device_id
        / original.occurred_at[:7]
        / f"{original.event_id}.json"
    )
    if damage == "malformed_json":
        path.write_bytes(b"{")
    elif damage == "bad_hash":
        document = original.to_document()
        document["content_hash"] = "f" * 64
        path.write_bytes(canonical_json(document) + b"\n")
    elif damage == "oversized":
        path.write_bytes(b"{" + b" " * MAX_EVENT_FILE_BYTES)
    else:
        wrong = path.with_name("wrong-event-id.json")
        path.rename(wrong)

    with pytest.raises(MigrationBlocked, match="migration_event_collision") as blocked:
        export_embedded_store(populated_repository, empty_event_store, "caller-a")

    assert blocked.value.code == "migration_event_collision"
