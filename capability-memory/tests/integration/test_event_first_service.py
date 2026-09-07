from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from supermind_memory.event_store import EventStore
from supermind_memory.discovery import CapabilityDiscovery, SourceRegistry
from supermind_memory.config import MemoryPaths
from supermind_memory.projection import project_authority
from supermind_memory.replay import replay
from supermind_memory.repository import CapabilityRepository
from supermind_memory.renderer import RepositoryRenderer
from supermind_memory.service import CapabilityMemory
from supermind_memory.sync import SyncReport, SyncState
from supermind_memory.types import (
    ArtifactType, Capability, DiscoveryContext, DiscoveryResult, Evidence, HealthReport, Lifecycle,
    RequirementProfile, ReuseResult,
)


class Healthy:
    def ensure_healthy(self):
        return HealthReport(True, "generation-test", (), "2026-09-06T12:00:00Z")

    def check(self):
        return self.ensure_healthy()


class LocalEventTransactions:
    def __init__(self, root: Path, repository, embeddings) -> None:
        self.store = EventStore(root)
        self.repository = repository
        self.embeddings = embeddings
        self.config = SimpleNamespace(
            device_id="device-test", last_checked_remote_head="a" * 40,
            repository="owner/memory", web_url="https://github.com/owner/memory",
        )
        self.paths = SimpleNamespace(checkout=root)
        self.transaction_count = 0

    def mutate(self, create_event):
        return self.mutate_batch(lambda current: (create_event(current),))

    def mutate_batch(self, create_events):
        self.transaction_count += 1
        current = replay(self.store.load_all())
        events = tuple(create_events(current))
        for event in events:
            self.store.append(event)
        result = replay(self.store.load_all())
        project_authority(result, self.repository, self.embeddings)
        return SyncReport(SyncState.COMMITTED, "b" * 40, result.digest, "a" * 40)


def _capability(source: Path) -> Capability:
    return Capability(
        abstraction_status="abstracted",
        id="auth.login", name="Login", summary="Reusable login",
        category_path=("code", "Identity"), facets=("login",),
        contract="login -> session", constraints=(), artifact_type=ArtifactType.CODE,
        source_uri=source.as_uri(), source_revision="abc", content_hash="a" * 64,
        owner="test", license="MIT", stack=("python",), runtime=("python",),
        platform=("local",), dependencies=(), compatibility=(),
        lifecycle=Lifecycle.CANDIDATE, confidence=0.9, expected_net_value=4.0,
        embedding_generation="test", created_at="2026-09-06T12:00:00Z",
        updated_at="2026-09-06T12:00:00Z", last_verified_at=None,
    )


@pytest.fixture
def event_memory(tmp_path, embeddings):
    paths = MemoryPaths.from_codex_home(tmp_path / "codex")
    repository = CapabilityRepository.open(paths.database, writer_lock_path=paths.locks / "writer.lock")
    repository.initialize()
    paths.checkout.mkdir(parents=True)
    transactions = LocalEventTransactions(paths.checkout, repository, embeddings)
    project_authority(replay(()), repository, embeddings)
    memory = CapabilityMemory(
        bootstrap=SimpleNamespace(), discovery=SimpleNamespace(), repository=repository,
        search_engine=SimpleNamespace(), embedding_provider=embeddings,
        health_manager=Healthy(), codex_home=tmp_path,
        sync_coordinator=transactions, authority_mode="events-v1",
    )
    yield memory, transactions
    memory.close()


def test_quality_gate_blocks_incomplete_admission_without_events(event_memory, tmp_path):
    from supermind_memory.types import ValueInputs
    memory, transactions = event_memory
    item = replace(_capability(tmp_path / "login.py"), summary=" ")
    before = transactions.store.load_all()
    assessment = memory.evaluate(item, ValueInputs(3, 10, 1, 1, 1, 1, 1))
    assert assessment.accepted is False
    assert "summary_missing" in assessment.reasons
    with pytest.raises(ValueError, match="summary_missing"):
        memory.register(item, ())
    assert transactions.store.load_all() == before


def test_library_audit_is_read_only(event_memory):
    memory, transactions = event_memory
    before = transactions.store.load_all()
    assert memory.audit()["capabilities"] == []
    assert transactions.store.load_all() == before


def test_register_uses_events_without_calling_direct_repository_writers(event_memory, tmp_path, monkeypatch):
    memory, transactions = event_memory
    source = tmp_path / "login.py"
    source.write_text("def login(): pass\n")
    for name in ("upsert_capability", "append_evidence", "append_relationship"):
        monkeypatch.setattr(memory.repository, name, lambda *args, _name=name, **kwargs: pytest.fail(f"direct writer called: {_name}"))

    proof = Evidence(
        id="proof-login", capability_id="auth.login", source_project="project-a",
        evidence_type="verification", outcome="passed", metric_name=None,
        metric_value=None, confidence=1.0, observed_at="2026-09-06T12:00:00Z",
        supporting_uri=None,
    )
    stored = memory.register(_capability(source), (proof,))
    assert transactions.transaction_count == 1
    demand = memory.observe_unmet_requirement(
        RequirementProfile("req-login", "project-b", "Need login")
    )
    linked = memory.link_requirement_observation(demand.id, stored.id)
    reused = memory.record_use(ReuseResult(stored.id, "project-b", True, 0.2, 3.0))

    assert stored.id == "auth.login"
    assert linked.linked_capability_id == stored.id
    assert reused.lifecycle is Lifecycle.RECOMMENDED
    events = transactions.store.load_all()
    assert {event.entity_type for event in events} == {
        "capability", "evidence", "demand", "reuse_outcome", "audit", "metadata",
    }
    assert replay(events).digest == memory.protocol_state()["event_set_digest"]


def test_service_rejects_non_event_authority_mode(tmp_path):
    with pytest.raises(ValueError, match="unsupported_authority_mode"):
        CapabilityMemory(
            bootstrap=SimpleNamespace(), discovery=SimpleNamespace(), repository=SimpleNamespace(),
            search_engine=SimpleNamespace(), embedding_provider=SimpleNamespace(),
            health_manager=Healthy(), codex_home=tmp_path, authority_mode="legacy",
        )


def test_discovery_commits_all_capabilities_in_one_transaction(event_memory, tmp_path):
    memory, transactions = event_memory
    source = tmp_path / "login.py"
    source.write_text("def login(): pass\n")
    first = _capability(source)
    second = replace(first, id="auth.logout", name="Logout")
    discovered = DiscoveryResult((first, second), (str(tmp_path),))
    memory.discovery_engine = SimpleNamespace(discover=lambda _: discovered)
    result = memory._discover_and_persist(DiscoveryContext(tmp_path, tmp_path / "codex"))
    assert {item.id for item in result.capabilities} == {first.id, second.id}
    assert transactions.transaction_count == 1
    assert len(transactions.store.load_all()) == 2


def test_init_commits_source_registry_metadata_and_replays_without_direct_write(event_memory, tmp_path, monkeypatch):
    memory, transactions = event_memory
    memory.discovery_engine = CapabilityDiscovery(SourceRegistry(memory.repository))
    memory.bootstrap = SimpleNamespace(initialize=lambda _: None)
    monkeypatch.setattr(memory.repository, "update_metadata", lambda *args: pytest.fail("direct registry write"))
    assert memory.initialize(tmp_path).healthy
    memory._assert_event_projection()
    assert transactions.transaction_count == 1
    metadata_events = [event for event in transactions.store.load_all() if event.entity_type == "metadata"]
    assert len(metadata_events) == 1
    source_registry = SourceRegistry(memory.repository)
    assert tmp_path in source_registry.active_projects
    assert source_registry.identity(tmp_path)


def test_service_rejects_missing_event_coordinator(tmp_path):
    with pytest.raises(ValueError, match="event_authority_unavailable"):
        CapabilityMemory(
            bootstrap=SimpleNamespace(), discovery=SimpleNamespace(), repository=SimpleNamespace(),
            search_engine=SimpleNamespace(), embedding_provider=SimpleNamespace(),
            health_manager=Healthy(), codex_home=tmp_path, authority_mode="events-v1",
        )


def test_open_validates_render_then_preserves_repository_as_one_argv_item(
    event_memory, tmp_path, monkeypatch,
):
    memory, transactions = event_memory
    sentinel = tmp_path / "must-not-exist"
    hostile_repository = f"owner/memory;touch {sentinel}"
    transactions.config.repository = hostile_repository
    transactions.config.web_url = "https://github.com/owner/memory"
    result = replay(transactions.store.load_all())
    RepositoryRenderer().render(result, transactions.paths.checkout)
    report = SyncReport(SyncState.SYNCED, "b" * 40, result.digest, "a" * 40)
    def render():
        RepositoryRenderer().render(result, transactions.paths.checkout)
        return report

    (transactions.paths.checkout / "README.md").write_text("stale browser")
    monkeypatch.setattr(transactions, "render_repository", render, raising=False)

    calls = []

    class Runner:
        def run(self, argv, *, cwd=None):
            calls.append((tuple(argv), cwd))
            return SimpleNamespace(returncode=0)

    memory.command_runner = Runner()

    opened = memory.open_browser()

    assert calls == [(("gh", "repo", "view", hostile_repository, "--web"), None)]
    assert opened["url"] == transactions.config.web_url
    assert not sentinel.exists()
