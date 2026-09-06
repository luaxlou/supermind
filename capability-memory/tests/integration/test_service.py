from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import math
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from supermind_memory.bootstrap import Bootstrap
from supermind_memory.config import MemoryPaths
from supermind_memory.discovery import CapabilityDiscovery, SourceRegistry
from supermind_memory.health import HealthManager
from supermind_memory.repository import CapabilityRepository, SearchHealthToken
from supermind_memory.redaction import redact_relationship
from supermind_memory.schema import EMBEDDING_DIMENSION, TABLE_SCHEMAS
from supermind_memory.search import CapabilitySearch
from supermind_memory.service import CapabilityMemory
from supermind_memory.workflow import SupermindWorkflow
from supermind_memory.types import (
    ArtifactType,
    Capability,
    CapabilityMemoryBlocked,
    DiscoveryContext,
    Evidence,
    HealthReport,
    Lifecycle,
    Relationship,
    RequirementProfile,
    ReuseResult,
    SearchResult,
    SearchStatus,
    ValueInputs,
)


@pytest.fixture
def memory(tmp_path, embeddings, event_transactions) -> CapabilityMemory:
    paths = MemoryPaths.from_codex_home(tmp_path / "codex")
    repository = CapabilityRepository.open(
        paths.database,
        writer_lock_path=paths.locks / "writer.lock",
    )
    repository.initialize()
    coordinator = event_transactions(paths, repository, embeddings)
    from supermind_memory.replay import replay
    health_manager = HealthManager(paths, repository, embeddings, authority_mode="events-v1",
                                   expected_authority_digest=replay(()).digest)
    return CapabilityMemory(
        bootstrap=Bootstrap(
            paths,
            repository,
            embeddings,
            health_manager=health_manager,
        ),
        discovery=CapabilityDiscovery(SourceRegistry(repository)),
        repository=repository,
        search_engine=CapabilitySearch(repository, embeddings),
        embedding_provider=embeddings,
        health_manager=health_manager,
        codex_home=tmp_path / "codex",
        sync_coordinator=coordinator,
    )


@pytest.fixture
def memory_fixture(memory: CapabilityMemory):
    def create(*, searcher: SequenceSearcher) -> CapabilityMemory:
        memory.repository = SequenceRepository(searcher.authority_changes)
        memory.search_engine = searcher
        memory.health_manager = SequenceHealthManager()
        return memory

    return create


class SequenceRepository:
    def __init__(self, authority_changes: int) -> None:
        self.authority_changes = authority_changes
        self.validation_calls = 0

    def search_health_token(self, generation: str | None) -> SearchHealthToken:
        assert generation is not None
        return SearchHealthToken(generation, "authority-digest", 0)

    def search_health_token_matches(self, token: SearchHealthToken) -> bool:
        self.validation_calls += 1
        return self.validation_calls > self.authority_changes


class SequenceHealthManager:
    def __init__(self) -> None:
        self.generation = "generation-before-repair"
        self.rebuild_calls = 0

    def ensure_healthy(self) -> HealthReport:
        return self.check()

    def rebuild_indexes(self) -> str:
        self.rebuild_calls += 1
        self.generation = "generation-after-repair"
        return self.generation

    def check(self) -> HealthReport:
        return HealthReport(
            healthy=True,
            active_generation=self.generation,
            repairs=(),
            checked_at="2026-09-06T00:00:00Z",
        )


class SequenceSearcher:
    def __init__(
        self,
        *,
        authority_changes: int,
        results: tuple[SearchResult, ...],
    ) -> None:
        self.authority_changes = authority_changes
        self.results = results
        self.search_calls = 0
        self.complete_search_calls = 0

    def search(
        self,
        requirement: RequirementProfile,
        *,
        health_token: SearchHealthToken | None = None,
    ) -> SearchResult:
        assert health_token is not None
        self.search_calls += 1
        if self.search_calls <= self.authority_changes:
            return SearchResult(
                SearchStatus.COMPLETE,
                (),
                generation=health_token.generation,
            )
        result = self.results[self.complete_search_calls]
        self.complete_search_calls += 1
        if result.status is SearchStatus.COMPLETE:
            return replace(result, generation=health_token.generation)
        return result


def failed_search(message: str) -> SearchResult:
    return SearchResult(
        SearchStatus.FAILED,
        (),
        error_code="search_failed",
        error_message=message,
    )


def complete_search() -> SearchResult:
    return SearchResult(SearchStatus.COMPLETE, ())


def requirement(intent: str) -> RequirementProfile:
    return RequirementProfile("requirement-sequence", "project-sequence", intent)


def test_requirement_identifiers_are_redacted_before_observation_hash_and_output(memory, tmp_path):
    project = tmp_path / "demand-boundary"
    project.mkdir()
    wanted = RequirementProfile(
        id="request:client_secret=request-credential",
        project_id="https://project.test/?access_token=project-credential",
        intent='login {"password":"demand-credential"}',
    )
    decision = SupermindWorkflow(memory).begin_design(project, wanted)
    observation = memory.list_requirement_observations()[0]
    equivalent = memory.observe_unmet_requirement(replace(
        wanted,
        id="request:client_secret=rotated-request",
        project_id="https://project.test/?access_token=rotated-project",
        intent='login {"password":"rotated-demand"}',
    ))
    assert observation.id == equivalent.id
    events = memory.list_requirement_events(observation.id)
    assert len(events) == 1 and events[0].observation_id == observation.id
    exposed = repr((decision, observation, events, memory.repository._rows("requirement_observations")))
    assert all(secret not in exposed for secret in (
        "request-credential", "project-credential", "demand-credential",
    ))


def test_registration_secret_identifiers_preserve_evidence_relationship_and_event_references(memory, tmp_path):
    source = tmp_path / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    raw_id = "login:client_secret=capability-credential"
    candidate = replace(
        _login_capability(source), id=raw_id, expected_net_value=5.0,
        summary='login {"password":"document-credential"}',
    )
    evidence = replace(
        _verification(raw_id, "project:client_secret=source-credential"),
        id="proof:client_secret=evidence-credential",
        supporting_uri="https://proof.test/#access_token=supporting-credential",
    )
    delegate = memory.embedding_provider
    documents = []

    class RecordingEmbeddings:
        def embed_documents(self, texts):
            documents.extend(texts)
            return delegate.embed_documents(texts)

    memory.embedding_provider = RecordingEmbeddings()
    memory.sync_coordinator.embeddings = memory.embedding_provider
    registered = memory.register(candidate, (evidence,))
    relationship = redact_relationship(Relationship(
        "relation:client_secret=relationship-credential", raw_id, raw_id,
        "composition", ('password="compatibility-credential"',), (evidence.id,),
    ))
    memory._commit_event("relationship", relationship.id, "declared", asdict(relationship))
    stored_evidence = memory.repository.list_evidence(registered.id)
    stored_events = memory.repository.list_events(registered.id)
    relationships = memory.repository.list_relationships(registered.id)
    assert len(stored_evidence) == len(stored_events) == len(relationships) == 1
    assert stored_evidence[0].capability_id == stored_events[0].capability_id == registered.id
    assert relationships[0].evidence_ids == (stored_evidence[0].id,)
    assert memory.get(raw_id) == registered
    assert memory.repository.list_events(raw_id) == stored_events
    assert memory.repository.list_relationships(raw_id) == relationships
    assert memory.register(candidate, (evidence,)).id == registered.id
    exposed = repr((registered, documents, [memory.repository._rows(table) for table in (
        "capabilities", "evidence", "relationships", "events", "metadata",
    )]))
    assert documents
    assert "credential" not in exposed


def test_registration_evidence_ingress_redacts_event_order_metadata(memory, tmp_path):
    registered = _registered_login(memory, tmp_path)
    evidence = replace(
        _verification(registered.id, "project-clean"),
        id="proof:client_secret=ordered-credential",
    )
    memory.register(registered, (evidence,))
    exposed = repr((memory.repository._rows("evidence"), memory.repository._rows("metadata"),
                    memory.sync_coordinator.store.load_all()))
    assert "ordered-credential" not in exposed
    assert memory.rebuild().healthy


def test_repair_diagnostics_are_redacted_when_appended(memory_fixture, monkeypatch):
    from supermind_memory import service

    memory = memory_fixture(searcher=SequenceSearcher(
        authority_changes=0, results=(failed_search("retrieval failed"),),
    ))
    original_blocked = service._blocked
    captured = []

    def blocked(code, message, attempts):
        captured.extend(attempts)
        return original_blocked(code, message, attempts)

    def repair():
        raise ValueError('repair {"password":"diagnostic-credential"}')

    monkeypatch.setattr(service, "_blocked", blocked)
    monkeypatch.setattr(memory.health_manager, "rebuild_indexes", repair)
    with pytest.raises(CapabilityMemoryBlocked) as error:
        memory.search(requirement("login"))
    assert "diagnostic-credential" not in repr(captured)
    assert "diagnostic-credential" not in str(error.value)


def test_explorer_output_sanitizes_original_requirement(memory):
    from supermind_memory.explorer import CapabilityExplorer

    wanted = RequirementProfile("safe-id", "safe-project", 'login {"password":"explorer-credential"}')
    output = CapabilityExplorer(memory.repository).decision(wanted, complete_search())
    assert "explorer-credential" not in output


def test_cli_bootstrap_diagnostic_output_is_sanitized(capsys):
    from supermind_memory.cli import main

    def unavailable_service():
        raise RuntimeError('bootstrap {"password":"cli-credential"}')

    assert main(["health"], service_factory=unavailable_service) == 3
    captured = capsys.readouterr()
    assert "cli-credential" not in captured.out + captured.err


def test_health_provider_failure_diagnostics_are_sanitized(memory, monkeypatch):
    memory.rebuild()

    def unavailable_query(text):
        raise RuntimeError('provider {"password":"health-credential"}')

    monkeypatch.setattr(memory.health_manager.embedding_provider, "embed_query", unavailable_query)
    report = memory.health_check()
    assert report.healthy is False
    assert "health-credential" not in repr(report)


def test_health_repair_diagnostics_are_sanitized_before_classification(memory, monkeypatch):
    captured = []
    original = memory.health_manager._blocking_code

    def unavailable_rebuild():
        raise RuntimeError('rebuild {"password":"health-repair-credential"}')

    def classify(report, errors):
        captured.extend(errors)
        return original(report, errors)

    monkeypatch.setattr(memory.health_manager, "_rebuild_indexes_locked", unavailable_rebuild)
    monkeypatch.setattr(memory.health_manager, "_blocking_code", classify)
    with pytest.raises(CapabilityMemoryBlocked):
        memory.rebuild()
    assert captured
    assert "health-repair-credential" not in repr(captured)


@pytest.mark.parametrize(("style", "indent"), [
    ("|", "  "), (">", "  "), ("&credential |", "  "), ("&credential >", "  "),
    ("!!str |", "  "), ("!!str >", "  "),
    ("&credential\n  !!str |", "    "), ("&credential\n  !!str >", "    "),
    ("!!str\n  &credential |", "    "), ("!!str\n  &credential >", "    "),
])
def test_yaml_block_credentials_are_sanitized_before_embedding_and_persistence(memory, tmp_path, style, indent):
    source = tmp_path / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    payload = f"login\npassword: {style}\n{indent}stored block credential\n{indent}second stored credential\nscope: public"
    captured = []
    delegate = memory.embedding_provider

    class RecordingEmbeddings:
        def embed_documents(self, texts):
            captured.extend(texts)
            return delegate.embed_documents(texts)

    memory.embedding_provider = RecordingEmbeddings()
    memory.sync_coordinator.embeddings = memory.embedding_provider
    candidate = replace(_login_capability(source), expected_net_value=5.0, summary=payload)
    stored = memory.register(candidate, (_verification(candidate.id, "project-safe"),))
    observation = memory.observe_unmet_requirement(RequirementProfile("req-block", "project-safe", payload))
    exposed = repr((captured, stored, observation, [memory.repository._rows(table) for table in (
        "capabilities", "evidence", "events", "requirement_observations", "requirement_events",
    )]))
    assert captured
    assert "stored block credential" not in exposed
    assert "second stored credential" not in exposed


@pytest.mark.parametrize("operation", ("register", "discover"))
def test_public_document_embedding_failure_is_sanitized(memory, tmp_path, operation):
    import traceback

    project = tmp_path / "embedding-provider-failure"
    project.mkdir()
    source = project / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    (project / "package.json").write_text(json.dumps({"name": "login-kit"}), encoding="utf-8")
    memory.rebuild()

    class UnavailableDocuments:
        def embed_documents(self, texts):
            raise RuntimeError('provider failed {"password":"provider-credential"}')

    memory.embedding_provider = UnavailableDocuments()
    memory.sync_coordinator.embeddings = memory.embedding_provider
    with pytest.raises(CapabilityMemoryBlocked) as failure:
        if operation == "register":
            candidate = replace(_login_capability(source), expected_net_value=5.0)
            memory.register(candidate, (_verification(candidate.id, "project-safe"),))
        else:
            memory.discover(DiscoveryContext(project, memory.codex_home))
    exposed = repr(failure.value.__dict__) + "".join(traceback.format_exception(failure.value))
    assert failure.value.code == "embedding_unavailable"
    assert "provider-credential" not in exposed
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None
    assert memory.repository.list_capabilities() == ()


def test_login_moves_from_candidate_to_recommended_after_independent_reuse(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project_a = tmp_path / "project-a"
    project_a.mkdir()
    source = project_a / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    memory.initialize(project_a)
    candidate = memory.evaluate(
        _login_capability(source),
        _positive_value_inputs(),
    ).capability
    assert candidate is not None

    registered = memory.register(candidate, [_verification(candidate.id, "project-a")])

    assert registered.lifecycle is Lifecycle.VERIFIED
    assert len(memory.repository.list_events(registered.id)) == 1

    updated = memory.record_use(
        ReuseResult(
            capability_id=registered.id,
            project="project-b",
            succeeded=True,
            integration_effort=0.25,
            benefit=4.0,
        )
    )

    assert updated.lifecycle is Lifecycle.RECOMMENDED
    assert len(memory.repository.list_evidence(registered.id)) == 2
    events = memory.repository.list_events(registered.id)
    assert len(events) == 2
    assert events[-1].previous_state is Lifecycle.VERIFIED
    assert events[-1].resulting_state is Lifecycle.RECOMMENDED


def test_search_refuses_to_run_when_health_cannot_be_restored(
    memory: CapabilityMemory,
) -> None:
    memory.health_manager = _UnrecoverableHealthManager()

    with pytest.raises(CapabilityMemoryBlocked, match="health cannot be restored"):
        memory.search(
            RequirementProfile(
                id="requirement-1",
                project_id="project-a",
                intent="login",
            )
        )


def test_search_blocks_when_health_gate_reports_failure_instead_of_raising(
    memory: CapabilityMemory,
) -> None:
    memory.health_manager = _ReportingUnhealthyHealthManager()

    with pytest.raises(CapabilityMemoryBlocked) as blocked:
        memory.search(
            RequirementProfile(
                id="requirement-unhealthy",
                project_id="project-a",
                intent="login",
            )
        )

    assert blocked.value.code == "source_unavailable"
    assert blocked.value.attempts == ("source_unavailable: missing source",)


def test_complete_no_match_search_reports_the_checked_generation(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "empty-search-project"
    project.mkdir()
    initialized = memory.initialize(project)

    result = memory.search(
        RequirementProfile(
            id="requirement-empty",
            project_id="project-a",
            intent="something absent",
        )
    )

    assert result.is_no_match is True
    assert result.generation == initialized.active_generation


def test_search_discards_a_result_when_authority_changes_at_a_barrier(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "barrier-search-project"
    project.mkdir()
    source = project / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    memory.initialize(project)
    evaluated = memory.evaluate(_login_capability(source), _positive_value_inputs()).capability
    assert evaluated is not None
    memory.register(evaluated, [_verification(evaluated.id, "project-a")])
    real_search = memory.search_engine
    searched = threading.Event()
    mutated = threading.Event()

    class BarrierSearch:
        def __init__(self) -> None:
            self.calls = 0
            self.tokens = []

        def search(self, requirement, *, health_token=None):
            self.calls += 1
            self.tokens.append(health_token)
            result = real_search.search(requirement, health_token=health_token)
            if self.calls == 1:
                searched.set()
                assert mutated.wait(timeout=10)
            return result

    barrier_search = BarrierSearch()
    memory.search_engine = barrier_search
    outcome = {}

    def run_search() -> None:
        outcome["result"] = memory.search(
            RequirementProfile(
                id="requirement-barrier",
                project_id="project-c",
                intent="OAuth login",
            )
        )

    worker = threading.Thread(target=run_search)
    worker.start()
    assert searched.wait(timeout=10)
    second_source = project / "second.py"
    second_source.write_text("def login_two(): pass\n", encoding="utf-8")
    second = replace(
        evaluated,
        id="auth.login-two",
        source_uri=second_source.as_uri(),
        content_hash="hash-login-two",
    )
    memory._commit_event("capability", second.id, "registered", asdict(second))
    mutated.set()
    worker.join(timeout=20)

    assert not worker.is_alive()
    assert barrier_search.calls == 2
    assert all(token is not None for token in barrier_search.tokens)
    result = outcome["result"]
    assert result.generation == memory.health_check().active_generation


def test_failed_hybrid_retrieval_rebuilds_once_and_retries_complete_search(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "retrieval-repair"
    project.mkdir()
    initial = memory.initialize(project)
    real_health = memory.health_manager

    class RecordingHealth:
        rebuilds = 0

        def ensure_healthy(self):
            return real_health.ensure_healthy()

        def rebuild_indexes(self):
            self.rebuilds += 1
            return real_health.rebuild_indexes()

        def check(self):
            return real_health.check()

    class FailThenCompleteSearch:
        calls = 0

        def search(self, requirement, *, health_token=None):
            self.calls += 1
            if self.calls == 1:
                return SearchResult(
                    SearchStatus.FAILED,
                    (),
                    error_code="fts_unavailable",
                    error_message="FTS route unavailable",
                )
            return SearchResult(
                SearchStatus.COMPLETE,
                (),
                generation=health_token.generation,
            )

    health = RecordingHealth()
    search = FailThenCompleteSearch()
    memory.health_manager = health
    memory.search_engine = search

    result = memory.search(RequirementProfile("repair", "project", "login"))

    assert result.status is SearchStatus.COMPLETE
    assert result.generation != initial.active_generation
    assert health.rebuilds == 1
    assert search.calls == 2


def test_consistency_restarts_do_not_consume_retrieval_repair_retry(
    memory_fixture,
) -> None:
    searcher = SequenceSearcher(
        authority_changes=2,
        results=(failed_search("vector route failed"), complete_search()),
    )
    memory = memory_fixture(searcher=searcher)

    result = memory.search(requirement(intent="login"))

    assert result.status is SearchStatus.COMPLETE
    assert searcher.complete_search_calls == 2
    assert memory.health_manager.rebuild_calls == 1


def test_consistency_restart_exhaustion_blocks_without_retrieval_repair(
    memory_fixture,
) -> None:
    searcher = SequenceSearcher(authority_changes=4, results=())
    memory = memory_fixture(searcher=searcher)

    with pytest.raises(CapabilityMemoryBlocked) as blocked:
        memory.search(requirement(intent="login"))

    assert blocked.value.code == "concurrent_mutation"
    assert blocked.value.attempts == (
        "consistency restart 1: authoritative state changed",
        "consistency restart 2: authoritative state changed",
        "consistency restart 3: authoritative state changed",
        "consistency restart 4: authoritative state changed",
    )
    assert searcher.search_calls == 4
    assert searcher.complete_search_calls == 0
    assert memory.health_manager.rebuild_calls == 0


def test_failed_hybrid_retry_blocks_with_both_attempt_diagnostics(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "retrieval-repair-exhausted"
    project.mkdir()
    memory.initialize(project)
    real_health = memory.health_manager

    class RecordingHealth:
        rebuilds = 0

        def ensure_healthy(self):
            return real_health.ensure_healthy()

        def rebuild_indexes(self):
            self.rebuilds += 1
            return real_health.rebuild_indexes()

        def check(self):
            return real_health.check()

    class AlwaysFailedSearch:
        calls = 0

        def search(self, requirement, *, health_token=None):
            self.calls += 1
            return SearchResult(
                SearchStatus.FAILED,
                (),
                error_code="vector_unavailable",
                error_message=f"vector route failure {self.calls}",
            )

    health = RecordingHealth()
    search = AlwaysFailedSearch()
    memory.health_manager = health
    memory.search_engine = search

    with pytest.raises(CapabilityMemoryBlocked) as blocked:
        memory.search(RequirementProfile("repair-fails", "project", "login"))

    assert health.rebuilds == 1
    assert search.calls == 2
    assert blocked.value.code == "vector_unavailable"
    assert blocked.value.attempts == (
        "retrieval attempt 1: vector_unavailable: vector route failure 1",
        "repair: activated a validated replacement generation",
        "retrieval attempt 2: vector_unavailable: vector route failure 2",
    )


def test_initialize_uses_the_current_health_gate_before_bootstrap(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "blocked-project"
    project.mkdir()
    memory.health_manager = _UnrecoverableHealthManager()

    with pytest.raises(CapabilityMemoryBlocked, match="health cannot be restored"):
        memory.initialize(project)


def test_discover_persists_scanned_capabilities_with_stable_source_identity(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "discover-project"
    project.mkdir()
    (project / "package.json").write_text(
        json.dumps({"name": "auth-kit", "description": "OAuth login"}),
        encoding="utf-8",
    )
    codex_home = tmp_path / "codex"
    memory.initialize(project)

    first = memory.discover(
        DiscoveryContext(project_root=project, codex_home=codex_home)
    )
    second = memory.refresh_sources(project)

    assert len(first.capabilities) == 1
    assert second.capabilities[0].id == first.capabilities[0].id
    assert memory.get(first.capabilities[0].id) is not None
    assert memory.get(first.capabilities[0].id).name == "auth-kit"


def test_initialize_discovers_the_active_project_and_returns_a_current_generation(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "initialize-project"
    project.mkdir()
    (project / "package.json").write_text(
        json.dumps({"name": "initialized-kit", "description": "Login helper"}),
        encoding="utf-8",
    )

    report = memory.initialize(project)

    stored = memory.repository.list_capabilities()
    assert [item.name for item in stored] == ["initialized-kit"]
    assert report.healthy is True
    assert report.active_generation is not None
    assert memory.repository.active_generation() == report.active_generation


def test_secret_never_reaches_persisted_search_text_or_embedding_input(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "secret-embedding-project"
    project.mkdir()
    secret = "sk-proj-" + ("Ab3d" * 12)
    (project / "package.json").write_text(
        json.dumps(
            {
                "name": "safe-kit",
                "description": f"Authentication helper api_key={secret}",
            }
        ),
        encoding="utf-8",
    )
    delegate = memory.embedding_provider

    class RecordingEmbeddings:
        documents = []

        def embed_documents(self, texts):
            self.documents.extend(texts)
            return delegate.embed_documents(texts)

        def embed_query(self, text):
            return delegate.embed_query(text)

    recorder = RecordingEmbeddings()
    memory.embedding_provider = recorder
    memory.sync_coordinator.embeddings = recorder
    memory.health_manager.embedding_provider = recorder

    memory.initialize(project)

    stored = memory.repository._rows("capabilities")[0]
    assert secret not in stored["search_text"]
    assert all(secret not in document for document in recorder.documents)
    assert "[REDACTED]" in stored["search_text"]


def test_capability_source_metadata_never_persists_original_credentials(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "source-metadata-secret"
    project.mkdir()
    memory.initialize(project)
    secret = "xoxb-" + "123456789012-123456789012-abcdefghijklmnopqrstuvwx"
    candidate = memory.evaluate(
        replace(
            _login_capability(project / "login.py"),
            source_uri=f"https://example.invalid/capability?api_key={secret}",
            source_revision=f"token={secret}",
            content_hash=f"token={secret}",
        ),
        _positive_value_inputs(),
    ).capability
    assert candidate is not None

    registered = memory.register(candidate, (_verification(candidate.id, "project-a"),))
    exposed = json.dumps(asdict(memory.get(candidate.id)), default=str)
    returned = json.dumps(asdict(registered), default=str)

    assert secret not in exposed
    assert secret not in returned
    assert "[REDACTED]" in exposed


def test_rebuild_activates_a_new_generation_and_health_check_reports_it(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "rebuild-project"
    project.mkdir()
    initial = memory.initialize(project)

    rebuilt = memory.rebuild()

    assert rebuilt.healthy is True
    assert rebuilt.active_generation is not None
    assert rebuilt.active_generation != initial.active_generation
    assert memory.health_check().active_generation == rebuilt.active_generation


def test_rebuild_blocks_if_post_rebuild_health_is_not_restored(
    memory: CapabilityMemory,
) -> None:
    memory.health_manager = _UnhealthyAfterRebuildHealthManager()

    with pytest.raises(CapabilityMemoryBlocked) as blocked:
        memory.rebuild()

    assert blocked.value.code == "fts_unavailable"


def test_complete_build_decision_persists_unmet_requirement_and_later_links_verified_work(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "unmet-demand-project"
    project.mkdir()
    requirement = RequirementProfile(
        id="req-unmet-auth",
        project_id="project-unmet",
        intent="Create a reusable OAuth login",
        contract="OAuth callback creates a session",
        runtime=("Python 3.12",),
        license=("MIT",),
    )

    decision = SupermindWorkflow(memory).begin_design(project, requirement)

    assert decision.action == "build"
    observations = memory.list_requirement_observations()
    assert len(observations) == 1
    observation = observations[0]
    assert observation.requirement == requirement
    assert observation.status == "unmet"
    assert observation.linked_capability_id is None
    assert memory.repository.list_capabilities() == ()
    assert [event.event_type for event in memory.list_requirement_events(observation.id)] == [
        "unmet_observed"
    ]

    source = project / "login.py"
    source.write_text("verified = True\n", encoding="utf-8")
    candidate = memory.evaluate(
        _login_capability(source),
        _positive_value_inputs(),
    ).capability
    assert candidate is not None
    verified = memory.register(
        candidate,
        [_verification(candidate.id, requirement.project_id)],
    )
    linked = memory.link_requirement_observation(observation.id, verified.id)

    assert linked.status == "linked"
    assert linked.linked_capability_id == verified.id
    assert [event.event_type for event in memory.list_requirement_events(observation.id)] == [
        "unmet_observed",
        "implementation_linked",
    ]


def test_reregister_uses_the_complete_persisted_evidence_history(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "reregister-project"
    project.mkdir()
    source = project / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    memory.initialize(project)
    evaluated = memory.evaluate(_login_capability(source), _positive_value_inputs()).capability
    assert evaluated is not None
    verified = memory.register(
        evaluated,
        [_verification(evaluated.id, "project-a")],
    )
    memory._ensure_healthy()

    reregistered = memory.register(verified, ())

    assert reregistered.lifecycle is Lifecycle.VERIFIED
    assert reregistered.last_verified_at == "2026-09-04T01:00:00Z"


def test_reregister_cannot_revive_an_authoritatively_retired_capability(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "retired-project"
    project.mkdir()
    source = project / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    memory.initialize(project)
    evaluated = memory.evaluate(_login_capability(source), _positive_value_inputs()).capability
    assert evaluated is not None
    retired = memory.register(replace(evaluated, lifecycle=Lifecycle.RETIRED), ())

    reregistered = memory.register(
        replace(retired, lifecycle=Lifecycle.CANDIDATE),
        (),
    )

    assert reregistered.lifecycle is Lifecycle.RETIRED


def test_refresh_preserves_evaluation_when_the_discovered_source_is_unchanged(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "unchanged-project"
    project.mkdir()
    (project / "package.json").write_text(
        json.dumps({"name": "auth-kit", "description": "OAuth login"}),
        encoding="utf-8",
    )
    memory.initialize(project)
    discovered = memory.refresh_sources(project).capabilities[0]
    evaluated = memory.evaluate(discovered, _positive_value_inputs()).capability
    assert evaluated is not None
    verified = memory.register(
        evaluated,
        [_verification(evaluated.id, "project-a")],
    )
    memory._ensure_healthy()
    row_before = next(
        row
        for row in memory.repository._rows("capabilities")
        if row["id"] == verified.id
    )
    fingerprint_before = memory.repository._authoritative_fingerprint()
    events_path = memory.sync_coordinator.paths.checkout / "events"
    events_before = _tree_bytes(events_path)

    refreshed = memory.refresh_sources(project).capabilities[0]

    assert refreshed.lifecycle is Lifecycle.VERIFIED
    assert refreshed.expected_net_value == verified.expected_net_value
    assert refreshed.last_verified_at == verified.last_verified_at
    assert refreshed == verified
    assert next(
        row
        for row in memory.repository._rows("capabilities")
        if row["id"] == verified.id
    ) == row_before
    assert memory.repository._authoritative_fingerprint() == fingerprint_before
    assert _tree_bytes(events_path) == events_before


def test_refresh_degrades_a_capability_when_its_discovered_source_is_removed(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "removed-source-project"
    project.mkdir()
    manifest = project / "package.json"
    manifest.write_text(
        json.dumps({"name": "auth-kit", "description": "OAuth login"}),
        encoding="utf-8",
    )
    memory.initialize(project)
    discovered = memory.repository.list_capabilities()[0]
    evaluated = memory.evaluate(discovered, _positive_value_inputs()).capability
    assert evaluated is not None
    verified = memory.register(
        evaluated,
        [_verification(evaluated.id, "project-a")],
    )
    recommended = memory.record_use(
        ReuseResult(
            capability_id=verified.id,
            project="project-b",
            succeeded=True,
            integration_effort=0.25,
            benefit=4.0,
        )
    )
    assert recommended.lifecycle is Lifecycle.RECOMMENDED
    event_count = len(memory.repository.list_events(recommended.id))
    manifest.unlink()

    memory.refresh_sources(project)

    degraded = memory.get(recommended.id)
    assert degraded is not None
    assert degraded.lifecycle is Lifecycle.DEGRADED
    evidence = memory.repository.list_evidence(recommended.id)
    assert any(
        item.evidence_type == "source_availability" and item.outcome == "unavailable"
        for item in evidence
    )
    events = memory.repository.list_events(recommended.id)
    assert len(events) == event_count + 1
    invalidated = next(item for item in events if item.event_type == "invalidated")
    assert invalidated.previous_state is Lifecycle.RECOMMENDED
    assert invalidated.resulting_state is Lifecycle.DEGRADED


def test_refresh_degrades_a_capability_when_discovered_content_changes(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "changed-source-project"
    project.mkdir()
    manifest = project / "package.json"
    manifest.write_text(
        json.dumps({"name": "auth-kit", "description": "OAuth login"}),
        encoding="utf-8",
    )
    memory.initialize(project)
    discovered = memory.repository.list_capabilities()[0]
    evaluated = memory.evaluate(discovered, _positive_value_inputs()).capability
    assert evaluated is not None
    verified = memory.register(
        evaluated,
        [_verification(evaluated.id, "project-a")],
    )
    manifest.write_text(
        json.dumps({"name": "auth-kit", "description": "Changed contract"}),
        encoding="utf-8",
    )

    memory.refresh_sources(project)

    degraded = memory.get(verified.id)
    assert degraded is not None
    assert degraded.lifecycle is Lifecycle.DEGRADED
    evidence = memory.repository.list_evidence(verified.id)
    assert any(
        item.evidence_type == "content_hash" and item.outcome == "changed"
        for item in evidence
    )


def test_registration_rejects_non_positive_value_without_writing(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "rejected-project"
    project.mkdir()
    source = project / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    memory.initialize(project)
    rejected = _login_capability(source)

    with pytest.raises(ValueError, match="expected net value must be positive"):
        memory.register(rejected, ())

    assert memory.get(rejected.id) is None
    assert memory.repository.list_evidence(rejected.id) == ()
    assert memory.repository.list_events(rejected.id) == ()


def test_registration_rejects_a_globally_conflicting_evidence_id(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    first = _registered_login(memory, tmp_path)
    second_source = tmp_path / "second.py"
    second_source.write_text("def login_two(): pass\n", encoding="utf-8")
    second = replace(
        _login_capability(second_source),
        id="auth.login-two",
        expected_net_value=2.0,
        lifecycle=Lifecycle.CANDIDATE,
    )
    conflicting = replace(
        _verification(second.id, "project-c"),
        id="verification-project-a",
    )

    with pytest.raises(ValueError, match="evidence id already exists"):
        memory.register(second, [conflicting])

    assert memory.get(second.id) is None
    assert memory.repository.list_evidence(first.id) == (
        _verification(first.id, "project-a"),
    )


@pytest.mark.parametrize("project", ["", "   "])
def test_record_use_rejects_a_missing_project_without_mutating_history(
    memory: CapabilityMemory,
    tmp_path: Path,
    project: str,
) -> None:
    registered = _registered_login(memory, tmp_path)
    evidence_before = memory.repository.list_evidence(registered.id)
    events_before = memory.repository.list_events(registered.id)

    with pytest.raises(ValueError, match="reuse project is required"):
        memory.record_use(
            ReuseResult(
                capability_id=registered.id,
                project=project,
                succeeded=True,
                integration_effort=0.25,
                benefit=4.0,
            )
        )

    assert memory.get(registered.id) == registered
    assert memory.repository.list_evidence(registered.id) == evidence_before
    assert memory.repository.list_events(registered.id) == events_before


def test_record_use_rejects_an_unavailable_source_without_mutating_history(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    registered = _registered_login(memory, tmp_path)
    Path(urlsplit(registered.source_uri).path).unlink()
    evidence_before = memory.repository.list_evidence(registered.id)
    events_before = memory.repository.list_events(registered.id)

    with pytest.raises(ValueError, match="source is unavailable"):
        memory.record_use(
            ReuseResult(
                capability_id=registered.id,
                project="project-b",
                succeeded=True,
                integration_effort=0.25,
                benefit=4.0,
            )
        )

    assert memory.get(registered.id) == registered
    assert memory.repository.list_evidence(registered.id) == evidence_before
    assert memory.repository.list_events(registered.id) == events_before


def test_record_use_rejects_source_evidence_that_marks_a_live_path_unavailable(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "unavailable-evidence-project"
    project.mkdir()
    source = project / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    memory.initialize(project)
    evaluated = memory.evaluate(_login_capability(source), _positive_value_inputs()).capability
    assert evaluated is not None
    unavailable = Evidence(
        id="source-unavailable",
        capability_id=evaluated.id,
        source_project="project-a",
        evidence_type="source_availability",
        outcome="unavailable",
        metric_name=None,
        metric_value=None,
        confidence=1.0,
        observed_at="2026-09-04T02:00:00Z",
        supporting_uri=source.as_uri(),
    )
    registered = memory.register(
        evaluated,
        [_verification(evaluated.id, "project-a"), unavailable],
    )
    assert registered.lifecycle is Lifecycle.DEGRADED
    evidence_before = memory.repository.list_evidence(registered.id)

    with pytest.raises(ValueError, match="source is unavailable"):
        memory.record_use(
            ReuseResult(
                capability_id=registered.id,
                project="project-b",
                succeeded=True,
                integration_effort=0.25,
                benefit=4.0,
            )
        )

    assert memory.repository.list_evidence(registered.id) == evidence_before


def test_record_use_treats_an_empty_source_uri_as_unavailable(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "empty-source-project"
    project.mkdir()
    source = project / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    memory.initialize(project)
    evaluated = memory.evaluate(_login_capability(source), _positive_value_inputs()).capability
    assert evaluated is not None
    registered = memory.register(
        replace(evaluated, source_uri=""),
        [_verification(evaluated.id, "project-a")],
    )

    with pytest.raises(ValueError, match="source is unavailable"):
        memory.record_use(
            ReuseResult(
                capability_id=registered.id,
                project="project-b",
                succeeded=True,
                integration_effort=0.25,
                benefit=4.0,
            )
        )


def test_record_use_requires_affirmative_availability_for_a_non_local_source(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "remote-source-project"
    project.mkdir()
    source = project / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    memory.initialize(project)
    evaluated = memory.evaluate(_login_capability(source), _positive_value_inputs()).capability
    assert evaluated is not None
    remote = replace(evaluated, source_uri="https://missing.invalid/login")
    registered = memory.register(
        remote,
        [_verification(remote.id, "project-a")],
    )

    with pytest.raises(ValueError, match="source is unavailable"):
        memory.record_use(
            ReuseResult(
                capability_id=registered.id,
                project="project-b",
                succeeded=True,
                integration_effort=0.25,
                benefit=4.0,
            )
        )


def test_record_use_recomputes_economics_and_degrades_after_failure(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    registered = _registered_login(memory, tmp_path)
    successful = memory.record_use(
        ReuseResult(
            capability_id=registered.id,
            project="project-b",
            succeeded=True,
            integration_effort=0.25,
            benefit=4.0,
        )
    )
    events_before_failure = len(memory.repository.list_events(registered.id))

    failed = memory.record_use(
        ReuseResult(
            capability_id=registered.id,
            project="project-c",
            succeeded=False,
            integration_effort=0.5,
            benefit=0.0,
            failure_reason="contract mismatch",
        )
    )

    assert successful.expected_net_value == 3.75
    assert failed.expected_net_value == 2.25
    assert failed.lifecycle is Lifecycle.DEGRADED
    outcomes = memory.repository.list_evidence(registered.id)
    failed_outcome = next(item for item in outcomes if item.source_project == "project-c")
    assert failed_outcome.outcome == "failed"
    assert failed_outcome.failure_risk == 1.0
    events = memory.repository.list_events(registered.id)
    assert len(events) == events_before_failure + 1
    failure_event = next(item for item in events if item.source_context == "project-c")
    assert failure_event.previous_state is Lifecycle.RECOMMENDED
    assert failure_event.resulting_state is Lifecycle.DEGRADED
    assert failure_event.reason == "contract mismatch"


def test_reuse_failure_evidence_and_event_never_persist_original_secret(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    registered = _registered_login(memory, tmp_path)
    secret = "xoxb-" + "123456789012-123456789012-abcdefghijklmnopqrstuvwx"

    memory.record_use(
        ReuseResult(
            capability_id=registered.id,
            project="project-secret",
            succeeded=False,
            integration_effort=1.0,
            benefit=0.0,
            failure_reason=f"remote rejected token={secret}",
        )
    )

    exposed = " ".join(
        item.reason for item in memory.repository.list_events(registered.id)
    )
    assert secret not in exposed
    assert "[REDACTED]" in exposed


def test_record_use_rejects_non_finite_economics_without_mutation(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    registered = _registered_login(memory, tmp_path)
    evidence_before = memory.repository.list_evidence(registered.id)

    with pytest.raises(ValueError, match="finite non-negative"):
        memory.record_use(
            ReuseResult(
                capability_id=registered.id,
                project="project-b",
                succeeded=True,
                integration_effort=0.25,
                benefit=math.nan,
            )
        )

    assert memory.repository.list_evidence(registered.id) == evidence_before


def test_record_use_persists_non_positive_aggregate_value_and_degrades(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    registered = _registered_login(memory, tmp_path)
    evidence_before = memory.repository.list_evidence(registered.id)
    events_before = memory.repository.list_events(registered.id)

    updated = memory.record_use(
        ReuseResult(
            capability_id=registered.id,
            project="project-b",
            succeeded=False,
            integration_effort=3.0,
            benefit=0.0,
        )
    )

    assert updated.expected_net_value == pytest.approx(-4.0)
    assert updated.lifecycle is Lifecycle.DEGRADED
    assert len(memory.repository.list_evidence(registered.id)) == len(evidence_before) + 1
    assert len(memory.repository.list_events(registered.id)) == len(events_before) + 1


def test_record_use_uses_commit_order_not_future_observation_time_for_availability(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    registered = _registered_login(memory, tmp_path)
    future_available = Evidence(
        id="future-available",
        capability_id=registered.id,
        source_project="project-a",
        evidence_type="source_availability",
        outcome="available",
        metric_name=None,
        metric_value=None,
        confidence=1.0,
        observed_at="2999-01-01T00:00:00Z",
        supporting_uri=registered.source_uri,
    )
    current_unavailable = replace(
        future_available,
        id="current-unavailable",
        outcome="unavailable",
        observed_at="2026-09-04T03:00:00Z",
    )
    memory._commit_event("evidence", future_available.id, "observed", asdict(future_available))
    memory._commit_event("evidence", current_unavailable.id, "observed", asdict(current_unavailable))
    evidence_before = memory.repository.list_evidence(registered.id)

    with pytest.raises(ValueError, match="source is unavailable"):
        memory.record_use(
            ReuseResult(
                capability_id=registered.id,
                project="project-b",
                succeeded=True,
                integration_effort=0.25,
                benefit=4.0,
            )
        )

    assert memory.repository.list_evidence(registered.id) == evidence_before


def test_register_uses_commit_order_not_future_observation_time_for_lifecycle(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "causal-order-project"
    project.mkdir()
    source = project / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    memory.initialize(project)
    evaluated = memory.evaluate(_login_capability(source), _positive_value_inputs()).capability
    assert evaluated is not None
    future_verification = replace(
        _verification(evaluated.id, "project-a"),
        id="future-verification",
        observed_at="2999-01-01T00:00:00Z",
    )
    later_change = replace(
        future_verification,
        id="later-content-change",
        evidence_type="content_hash",
        outcome="changed",
        observed_at="2026-09-04T03:00:00Z",
    )

    registered = memory.register(evaluated, (future_verification, later_change))

    assert registered.lifecycle is Lifecycle.DEGRADED


def test_record_use_revalidates_all_historical_evidence_inside_the_lock(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    registered = _registered_login(memory, tmp_path)
    rows = memory.repository._table("evidence").to_arrow().to_pylist()
    rows[0]["benefit"] = -1.0
    memory.repository._overwrite_table_unlocked(
        "evidence",
        TABLE_SCHEMAS["evidence"],
        rows,
    )
    memory.health_manager = _AlwaysHealthyHealthManager()
    database_before = _tree_bytes(memory.bootstrap.paths.database)

    with pytest.raises(ValueError, match="finite non-negative"):
        memory.record_use(
            ReuseResult(
                capability_id=registered.id,
                project="project-b",
                succeeded=True,
                integration_effort=0.25,
                benefit=4.0,
            )
        )

    assert _tree_bytes(memory.bootstrap.paths.database) == database_before


def test_register_embeds_before_entering_projection_writer_lock(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> None:
    project = tmp_path / "lock-project"
    project.mkdir()
    source = project / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    memory.initialize(project)
    evaluated = memory.evaluate(_login_capability(source), _positive_value_inputs()).capability
    assert evaluated is not None
    memory.health_manager = _AlwaysHealthyHealthManager()
    original_lock = memory.repository._writer_lock
    original_prepare = memory.repository._prepare_transaction_unlocked
    state = {"locked": False, "entries": 0, "embedded": False}
    timeline = []

    class TrackingEmbeddings:
        def embed_documents(self, texts):
            assert state["locked"] is False
            state["embedded"] = True
            timeline.append("embed")
            return memory.search_engine._embeddings.embed_documents(texts)

        def embed_query(self, text):
            return memory.search_engine._embeddings.embed_query(text)

    @contextmanager
    def tracked_lock():
        state["entries"] += 1
        state["locked"] = True
        timeline.append("lock")
        try:
            with original_lock():
                yield
        finally:
            state["locked"] = False

    def tracked_prepare(*args, **kwargs):
        assert state["locked"] is True
        timeline.append("write")
        return original_prepare(*args, **kwargs)

    memory.embedding_provider = TrackingEmbeddings()
    memory.sync_coordinator.embeddings = memory.embedding_provider
    memory.repository._writer_lock = tracked_lock
    memory.repository._prepare_transaction_unlocked = tracked_prepare

    memory.register(evaluated, [_verification(evaluated.id, "project-a")])

    assert state["locked"] is False
    assert state["embedded"] is True
    # Event authority validation may acquire a read lock before projection.
    # The actual projection write must begin after document embedding.
    assert timeline.count("write") == 1
    assert timeline.index("embed") < timeline.index("write")


def test_register_rolls_back_byte_for_byte_when_evidence_write_fails(
    memory: CapabilityMemory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "atomic-register-project"
    project.mkdir()
    source = project / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    memory.initialize(project)
    evaluated = memory.evaluate(_login_capability(source), _positive_value_inputs()).capability
    assert evaluated is not None
    database_before = _tree_bytes(memory.health_manager.paths.database)
    event_files_before = _tree_bytes(memory.sync_coordinator.paths.checkout / "events")
    original_overwrite = CapabilityRepository._overwrite_table_unlocked

    def fail_evidence(repository, table_name, schema, rows):
        if table_name == "evidence":
            raise RuntimeError("injected evidence write failure")
        return original_overwrite(repository, table_name, schema, rows)

    monkeypatch.setattr(CapabilityRepository, "_overwrite_table_unlocked", fail_evidence)

    with pytest.raises(RuntimeError, match="injected evidence write failure"):
        memory.register(
            evaluated,
            [_verification(evaluated.id, "project-a")],
        )

    assert _tree_bytes(memory.health_manager.paths.database) == database_before
    assert _tree_bytes(memory.sync_coordinator.paths.checkout / "events") == event_files_before
    assert memory.get(evaluated.id) is None
    assert memory.repository.list_evidence(evaluated.id) == ()
    assert memory.repository.list_events(evaluated.id) == ()


class _UnrecoverableHealthManager:
    def ensure_healthy(self) -> HealthReport:
        raise CapabilityMemoryBlocked(
            "embedding_unavailable",
            "health cannot be restored",
            ("attempt 1", "attempt 2", "attempt 3"),
        )


class _ReportingUnhealthyHealthManager:
    def ensure_healthy(self) -> HealthReport:
        return HealthReport(
            healthy=False,
            active_generation=None,
            repairs=(),
            checked_at="2026-09-04T00:00:00Z",
            failures=("source_unavailable: missing source",),
        )


class _AlwaysHealthyHealthManager:
    def ensure_healthy(self) -> HealthReport:
        return HealthReport(
            healthy=True,
            active_generation="generation-test",
            repairs=(),
            checked_at="2026-09-04T00:00:00Z",
        )


class _UnhealthyAfterRebuildHealthManager:
    def __init__(self) -> None:
        self.rebuilt = False

    def ensure_healthy(self) -> HealthReport:
        return HealthReport(
            healthy=not self.rebuilt,
            active_generation="generation-test" if not self.rebuilt else None,
            repairs=(),
            checked_at="2026-09-04T00:00:00Z",
            failures=() if not self.rebuilt else ("fts_unavailable: missing index",),
        )

    def rebuild_indexes(self) -> str:
        self.rebuilt = True
        return "generation-broken"

    def check(self) -> HealthReport:
        return self.ensure_healthy()


def _login_capability(source: Path) -> Capability:
    return Capability(
        id="auth.login",
        name="OAuth login",
        summary="Reusable OAuth login",
        category_path=("Code and components", "Identity and access"),
        facets=("authentication",),
        contract="OAuth callback creates a session",
        constraints=(),
        artifact_type=ArtifactType.CODE,
        source_uri=source.as_uri(),
        source_revision="abc123",
        content_hash="hash-login",
        owner="identity-team",
        license="MIT",
        stack=("Python",),
        runtime=("CPython",),
        platform=(),
        dependencies=(),
        compatibility=("OAuth 2.0",),
        lifecycle=Lifecycle.OBSERVED,
        confidence=0.8,
        expected_net_value=0.0,
        embedding_generation="",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
        last_verified_at=None,
    )


def _positive_value_inputs() -> ValueInputs:
    return ValueInputs(
        expected_reuse_count=3,
        benefit_per_reuse=4,
        extraction_cost=1,
        integration_cost=1,
        verification_cost=1,
        maintenance_cost=1,
        failure_risk=1,
    )


def _verification(capability_id: str, project: str) -> Evidence:
    return Evidence(
        id=f"verification-{project}",
        capability_id=capability_id,
        source_project=project,
        evidence_type="verification",
        outcome="passed",
        metric_name=None,
        metric_value=None,
        confidence=1.0,
        observed_at="2026-09-04T01:00:00Z",
        supporting_uri=None,
    )


def _registered_login(
    memory: CapabilityMemory,
    tmp_path: Path,
) -> Capability:
    project = tmp_path / "registered-project"
    project.mkdir()
    source = project / "login.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    memory.initialize(project)
    evaluated = memory.evaluate(_login_capability(source), _positive_value_inputs()).capability
    assert evaluated is not None
    return memory.register(
        evaluated,
        [_verification(evaluated.id, "project-a")],
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_final_yaml_scalars_never_cross_documents_or_persistence(memory, tmp_path, yaml_scalar_secret):
    payload, secrets = yaml_scalar_secret
    source = tmp_path / "login.py"
    source.write_text("def login(): pass\n")
    captured = []
    delegate = memory.embedding_provider

    class RecordingEmbeddings:
        def embed_documents(self, texts):
            captured.extend(texts)
            return delegate.embed_documents(texts)

    memory.embedding_provider = RecordingEmbeddings()
    memory.sync_coordinator.embeddings = memory.embedding_provider
    candidate = replace(_login_capability(source), expected_net_value=5.0, summary="login\n" + payload)
    stored = memory.register(candidate, (_verification(candidate.id, "project-safe"),))
    observation = memory.observe_unmet_requirement(RequirementProfile("req-scalar", "project-safe", payload))
    exposed = repr((captured, stored, observation, [memory.repository._rows(table) for table in TABLE_SCHEMAS]))
    assert captured
    assert all(secret not in exposed for secret in secrets)
    assert "scope: public" in stored.summary


@pytest.mark.parametrize("structured", [False, True])
def test_final_retrieval_repair_failure_has_no_raw_exception_chain(memory, monkeypatch, structured):
    import traceback

    memory.rebuild()

    def failed_search(**kwargs):
        raise RuntimeError("retrieval route failed")

    def failed_rebuild():
        try:
            raise RuntimeError('repair provider password="repair-private-material"')
        except RuntimeError as raw:
            if structured:
                raise CapabilityMemoryBlocked("repair_failed", "repair blocked", (str(raw),)) from raw
            raise

    monkeypatch.setattr(memory.search_engine, "search", lambda *args, **kwargs: failed_search())
    monkeypatch.setattr(memory.health_manager, "rebuild_indexes", failed_rebuild)
    with pytest.raises(CapabilityMemoryBlocked) as failure:
        memory.search(RequirementProfile("repair-request", "project", "login"))
    assert failure.value.code == "repair_failed"
    assert failure.value.attempts
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert "repair-private-material" not in repr(failure.value.__dict__)
    assert "repair-private-material" not in "".join(traceback.format_exception(failure.value))
