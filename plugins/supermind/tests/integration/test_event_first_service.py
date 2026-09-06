from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from supermind_memory.event_store import EventStore
from supermind_memory.config import MemoryPaths
from supermind_memory.projection import project_authority
from supermind_memory.replay import replay
from supermind_memory.repository import CapabilityRepository
from supermind_memory.renderer import RepositoryRenderer
from supermind_memory.service import CapabilityMemory
from supermind_memory.sync import SyncReport, SyncState
from supermind_memory.types import (
    ArtifactType, Capability, Evidence, HealthReport, Lifecycle,
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

    def mutate(self, create_event):
        current = replay(self.store.load_all())
        event = create_event(current)
        self.store.append(event)
        result = replay(self.store.load_all())
        project_authority(result, self.repository, self.embeddings)
        return SyncReport(SyncState.COMMITTED, "b" * 40, result.digest, "a" * 40)


def _capability(source: Path) -> Capability:
    return Capability(
        id="auth.login", name="Login", summary="Reusable login",
        category_path=("Code and components", "Identity"), facets=("login",),
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
        "capability", "evidence", "demand", "reuse_outcome",
    }
    assert replay(events).digest == memory.protocol_state()["event_set_digest"]


def test_service_rejects_non_event_authority_mode(tmp_path):
    with pytest.raises(ValueError, match="unsupported_authority_mode"):
        CapabilityMemory(
            bootstrap=SimpleNamespace(), discovery=SimpleNamespace(), repository=SimpleNamespace(),
            search_engine=SimpleNamespace(), embedding_provider=SimpleNamespace(),
            health_manager=Healthy(), codex_home=tmp_path, authority_mode="legacy",
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
    monkeypatch.setattr(memory, "sync", lambda: report)

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
