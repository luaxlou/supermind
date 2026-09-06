from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path

import pytest

from supermind_memory.bootstrap import Bootstrap
from supermind_memory.config import MemoryPaths
from supermind_memory.discovery import CapabilityDiscovery, SourceRegistry
from supermind_memory.explorer import CapabilityExplorer
from supermind_memory.health import HealthManager
from supermind_memory.repository import CapabilityRepository
from supermind_memory.schema import TABLE_SCHEMAS
from supermind_memory.search import CapabilitySearch
from supermind_memory.service import CapabilityMemory
from supermind_memory.types import (
    ArtifactType,
    Capability,
    CapabilityMemoryBlocked,
    Evidence,
    Lifecycle,
    Relationship,
    RequirementProfile,
    ReuseResult,
    SearchStatus,
    ValueInputs,
)
from supermind_memory.workflow import SupermindWorkflow


@dataclass(frozen=True)
class Projects:
    a: Path
    b: Path
    c: Path


@pytest.fixture
def projects(tmp_path: Path) -> Projects:
    roots = tuple(tmp_path / name for name in ("project-a", "project-b", "project-c"))
    for root in roots:
        root.mkdir()
    (roots[0] / "package.json").write_text(
        json.dumps({"name": "login-module", "description": "Reusable OAuth login"}),
        encoding="utf-8",
    )
    (roots[0] / "saml-provider.json").write_text(
        json.dumps({"name": "directory-provider", "protocol": "SAML"}),
        encoding="utf-8",
    )
    return Projects(*roots)


@pytest.fixture
def memory(tmp_path: Path, embeddings) -> CapabilityMemory:
    codex_home = tmp_path / "codex"
    paths = MemoryPaths.from_codex_home(codex_home)
    repository = CapabilityRepository.open(
        paths.database,
        writer_lock_path=paths.locks / "writer.lock",
    )
    repository.initialize()
    health = HealthManager(paths, repository, embeddings)
    service = CapabilityMemory(
        bootstrap=Bootstrap(paths, repository, embeddings, health_manager=health),
        discovery=CapabilityDiscovery(SourceRegistry(repository)),
        repository=repository,
        search_engine=CapabilitySearch(repository, embeddings),
        embedding_provider=embeddings,
        health_manager=health,
        codex_home=codex_home,
    )
    yield service
    service.close()


@pytest.fixture
def workflow(memory: CapabilityMemory) -> SupermindWorkflow:
    return SupermindWorkflow(memory)


@pytest.fixture
def explorer(memory: CapabilityMemory) -> CapabilityExplorer:
    return CapabilityExplorer(memory.repository)


def test_login_capability_is_discovered_reused_visualized_and_invalidated(
    workflow: SupermindWorkflow,
    memory: CapabilityMemory,
    explorer: CapabilityExplorer,
    projects: Projects,
) -> None:
    memory.initialize(projects.a)
    login = workflow.complete_implementation(
        _login_capability(projects.a),
        _positive_value_inputs(),
        [
            _verification(
                project="project-a",
                integration_effort=1.0,
                failure_risk=1.0,
            )
        ],
    )
    assert login is not None
    assert login.lifecycle is Lifecycle.VERIFIED

    incompatible = workflow.complete_implementation(
        _incompatible_identity_capability(projects.a),
        _positive_value_inputs(),
        [
            _verification(
                project="project-a",
                capability_id="auth.saml-directory",
            )
        ],
    )
    assert incompatible is not None
    incompatible = workflow.complete_reuse(
        ReuseResult(
            capability_id=incompatible.id,
            project="project-x",
            succeeded=True,
            integration_effort=0,
            benefit=4,
        )
    )
    assert incompatible.lifecycle is Lifecycle.RECOMMENDED

    decision_b = workflow.begin_design(
        projects.b,
        _oauth_requirement(projects.b, language="zh"),
    )
    assert decision_b.search_result.status is SearchStatus.COMPLETE
    assert decision_b.search_result.matches[0].capability_id == login.id, (
        tuple(
            (match.capability_id, match.reuse_score, match.contract_fit, match.requirement_fit)
            for match in decision_b.search_result.matches
        )
    )
    incompatible_match = next(
        match
        for match in decision_b.search_result.matches
        if match.capability_id == incompatible.id
    )
    assert incompatible_match.requirement_fit > 0.5
    assert incompatible_match.contract_fit == 0
    assert incompatible_match.rejection_reasons == ("contract mismatch",)
    assert decision_b.selected_capability_id == login.id
    assert decision_b.action == "reuse"

    partial = workflow.begin_design(projects.b, _partial_oauth_requirement(projects.b))
    assert partial.selected_capability_id == login.id
    assert partial.action == "adapt"
    assert any("partial contract fit" in reason for reason in partial.rationale)
    assert "Adapt capability: auth.login" in explorer.decision(
        partial.requirement,
        partial.search_result,
    )

    recommended = workflow.complete_reuse(_successful_reuse(login.id, projects.b))
    assert recommended.lifecycle is Lifecycle.RECOMMENDED

    decision_c = workflow.begin_design(
        projects.c,
        _oauth_requirement(projects.c, language="zh"),
    )
    decision = explorer.decision(decision_c.requirement, decision_c.search_result)
    assert "Code and components / Identity and access" in decision
    assert "Expected net value" in decision

    Path(login.source_uri.removeprefix("file://")).unlink()
    memory.refresh_sources(projects.a)
    matches = memory.search(_oauth_requirement(projects.c)).matches
    assert login.id not in {match.capability_id for match in matches}

    _damage_search_index(memory)
    repaired = workflow.begin_design(projects.c, _oauth_requirement(projects.c))
    assert repaired.search_result.status is SearchStatus.COMPLETE

    memory.repository._database.drop_table("capabilities")
    with pytest.raises(CapabilityMemoryBlocked):
        workflow.begin_design(projects.c, _oauth_requirement(projects.c))


def test_non_positive_implementation_stays_local(
    workflow: SupermindWorkflow,
    memory: CapabilityMemory,
    projects: Projects,
) -> None:
    registered = workflow.complete_implementation(
        _login_capability(projects.a),
        ValueInputs(
            expected_reuse_count=1,
            benefit_per_reuse=1,
            extraction_cost=1,
            integration_cost=1,
            verification_cost=1,
            maintenance_cost=1,
            failure_risk=1,
        ),
        [_verification(project="project-a")],
    )

    assert registered is None
    assert memory.get("auth.login") is None


def test_hardened_login_reuse_preserves_proof_repair_secrets_and_demand_history(
    workflow, memory, projects, embeddings, monkeypatch,
):
    embedding_inputs = []
    embed_documents = embeddings.embed_documents
    embed_query = embeddings.embed_query

    def record_documents(texts):
        embedding_inputs.extend(texts)
        return embed_documents(texts)

    def record_query(text):
        embedding_inputs.append(text)
        return embed_query(text)

    monkeypatch.setattr(embeddings, "embed_documents", record_documents)
    monkeypatch.setattr(embeddings, "embed_query", record_query)
    wanted = RequirementProfile(
        id="login-password=identifier-secret-1234567890",
        project_id="project-client_secret=project-secret-1234567890",
        intent='OAuth login {"password":"correct-horse-battery-staple"} '
        "https://example.test/callback?access_token=oauth-secret-1234567890",
        contract="requires Python >=3.12,<4 with AES 256 encryption",
        category_hint=("Code and components", "Identity and access"),
        runtime=("CPython",), stack=("Python",), license=("MIT",),
    )
    unmet = workflow.begin_design(projects.b, wanted)
    assert unmet.action == "build"
    observation, = memory.list_requirement_observations()
    assert observation.status == "unmet"
    assert [event.event_type for event in memory.list_requirement_events(observation.id)] == ["unmet_observed"]

    incomplete = replace(
        _login_capability(projects.a), id="auth.incomplete",
        name="OAuth login", summary="OAuth login",
        contract="supports Python >=3.12,<4", compatibility=(),
    )
    complete = replace(
        _login_capability(projects.a), id="auth.complete", name="Encrypted boundary",
        summary="Session token renderer integration", facets=(), compatibility=(),
        contract="supports Python >=3.12,<4 with AES 256 encryption",
    )
    related = replace(complete, id="auth.failed-relationship", name="Related boundary")
    for candidate in (incomplete, complete, related):
        registered = workflow.complete_implementation(
            candidate, _positive_value_inputs(),
            [_verification("project-a", capability_id=candidate.id)],
        )
        assert registered is not None
        assert registered.lifecycle is Lifecycle.VERIFIED
    memory.repository.append_evidence(replace(
        _verification("relationship-project", capability_id=complete.id),
        id="failed-relationship-proof", evidence_type="integration", outcome="failed",
    ))
    memory.repository.append_relationship(Relationship(
        id="failed-login-relation", source_id=complete.id, target_id=related.id,
        relationship_type="dependency", compatibility=(), evidence_ids=("failed-relationship-proof",),
    ))

    hybrid = memory.repository.hybrid_search
    rebuild = memory.health_manager.rebuild_indexes
    routes_seen = []
    repairs = []

    def churn_then_fail_hybrid(**kwargs):
        routes = hybrid(**kwargs)
        routes_seen.append(kwargs["generation"])
        if len(routes_seen) <= 2:
            memory.repository.set_metadata("e2e-authority-change", len(routes_seen))
        if len(routes_seen) == 3:
            raise RuntimeError("injected complete hybrid failure")
        # Keep real retrieval; isolate expansion by omitting this candidate
        # from the direct routes, as a limited retrieval result would do.
        return tuple([row for row in route if row["id"] != related.id] for route in routes)

    def record_rebuild():
        generation = rebuild()
        repairs.append(generation)
        return generation

    with monkeypatch.context() as patch:
        patch.setattr(memory.repository, "hybrid_search", churn_then_fail_hybrid)
        patch.setattr(memory.health_manager, "rebuild_indexes", record_rebuild)
        decision = workflow.begin_design(projects.b, wanted)

    assert decision.search_result.status is SearchStatus.COMPLETE
    assert decision.action == "reuse"
    assert decision.selected_capability_id == complete.id
    matches = {match.capability_id: match for match in decision.search_result.matches}
    assert matches[incomplete.id].contract_fit == pytest.approx(0.5)
    assert matches[complete.id].contract_fit == 1.0
    assert matches[incomplete.id].requirement_fit > matches[complete.id].requirement_fit
    assert related.id not in matches
    assert len(routes_seen) == 4
    assert routes_seen[0] == routes_seen[1] == routes_seen[2]
    assert repairs == [decision.search_result.generation]
    assert routes_seen[3] == repairs[0] != routes_seen[2]

    paths = memory.health_manager.paths
    manifest = json.loads((paths.generations / repairs[0] / "manifest.json").read_text())
    evaluation = manifest["evaluation"]
    assert evaluation["thresholds"] == {
        "semantic_recall": 1.0, "hybrid_recall": 1.0,
        "hard_negative_accuracy": 1.0, "filter_accuracy": 1.0,
    }
    assert evaluation["metrics"] == evaluation["thresholds"]
    assert evaluation["passed"] is True
    assert evaluation["version"] == 1
    assert evaluation["digest"] == hashlib.sha256(
        (Path(__file__).parents[2] / "evaluation" / "retrieval-v1.json").read_bytes()
    ).hexdigest()
    assert evaluation["provider"] == "conftest.KeywordEmbeddingProvider"

    linked = memory.link_requirement_observation(observation.id, complete.id)
    assert linked.status == "linked"
    assert linked.linked_capability_id == complete.id
    events = memory.list_requirement_events(observation.id)
    assert [event.event_type for event in events] == ["unmet_observed", "implementation_linked"]
    assert [event.capability_id for event in events] == [None, complete.id]
    history_before = {name: memory.repository._rows(name) for name in ("requirement_observations", "requirement_events")}
    assert memory.health_manager.ensure_healthy().healthy is True
    with CapabilityRepository.open(paths.database, paths.locks / "writer.lock") as reopened:
        reopened.initialize()
        assert {name: reopened._rows(name) for name in history_before} == history_before

    source = projects.a / "package.json"
    decisions = []
    for reference in (str(source), source.as_uri()):
        stored = memory.get(complete.id)
        memory.repository.upsert_capability(replace(stored, source_uri=reference), [0.0] * 384)
        outcome = workflow.begin_design(projects.c, wanted)
        decisions.append((outcome.action, outcome.selected_capability_id))
    assert decisions == [("reuse", complete.id), ("reuse", complete.id)]

    authoritative = json.dumps({name: memory.repository._rows(name) for name in TABLE_SCHEMAS})
    output = json.dumps(asdict(decision))
    assert embedding_inputs
    for secret in (
        "identifier-secret-1234567890", "project-secret-1234567890",
        "correct-horse-battery-staple", "oauth-secret-1234567890",
    ):
        assert all(secret not in text for text in embedding_inputs)
        assert secret not in authoritative
        assert secret not in output


def _login_capability(project: Path) -> Capability:
    source = project / "package.json"
    return Capability(
        id="auth.login",
        name="OAuth login",
        summary="Reusable OAuth login with a verified session contract",
        category_path=("Code and components", "Identity and access"),
        facets=("authentication", "login"),
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
        confidence=0.1,
        expected_net_value=0.0,
        embedding_generation="",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
        last_verified_at=None,
    )


def _incompatible_identity_capability(project: Path) -> Capability:
    source = project / "saml-provider.json"
    return Capability(
        id="auth.saml-directory",
        name="Federated authentication sign-in provider",
        summary="Third-party identity login with strong operational evidence",
        category_path=("Code and components", "Identity and access"),
        facets=("oauth", "callback", "session", "login", "access"),
        contract="SAML assertion provisions directory identity",
        constraints=(),
        artifact_type=ArtifactType.CODE,
        source_uri=source.as_uri(),
        source_revision="def456",
        content_hash="hash-saml-directory",
        owner="directory-team",
        license="MIT",
        stack=("Python",),
        runtime=("CPython",),
        platform=(),
        dependencies=(),
        compatibility=("SAML 2.0",),
        lifecycle=Lifecycle.OBSERVED,
        confidence=1.0,
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


def _verification(
    project: str,
    capability_id: str = "auth.login",
    integration_effort: float = 0.0,
    failure_risk: float = 0.0,
) -> Evidence:
    return Evidence(
        id=f"verification-{project}-{capability_id}",
        capability_id=capability_id,
        source_project=project,
        evidence_type="verification",
        outcome="passed",
        metric_name=None,
        metric_value=None,
        confidence=1.0,
        observed_at="2026-09-04T01:00:00Z",
        supporting_uri=None,
        integration_effort=integration_effort,
        failure_risk=failure_risk,
    )


def _successful_reuse(capability_id: str, project: Path) -> ReuseResult:
    return ReuseResult(
        capability_id=capability_id,
        project=project.name,
        succeeded=True,
        integration_effort=0.25,
        benefit=4.0,
    )


def _oauth_requirement(project: Path, language: str = "en") -> RequirementProfile:
    intent = "设计支持第三方登录的身份认证" if language == "zh" else "OAuth login"
    return RequirementProfile(
        id=f"requirement-{project.name}-{language}",
        project_id=project.name,
        intent=intent,
        contract="OAuth callback creates a session",
        category_hint=("Code and components", "Identity and access"),
        stack=("Python",),
    )


def _partial_oauth_requirement(project: Path) -> RequirementProfile:
    return RequirementProfile(
        id=f"requirement-{project.name}-partial",
        project_id=project.name,
        intent="设计支持第三方登录的身份认证",
        contract="OAuth callback creates a session and refreshes access tokens",
        category_hint=("Code and components", "Identity and access"),
        stack=("Python",),
    )


def _damage_search_index(memory: CapabilityMemory) -> None:
    generation = memory.health_check().active_generation
    assert generation is not None
    paths = memory.health_manager.paths
    generation_repository = CapabilityRepository.open(
        paths.generations / generation / "database",
        writer_lock_path=paths.locks / "writer.lock",
    )
    table = generation_repository._table("capabilities")
    index = next(index for index in table.list_indices() if index.index_type == "FTS")
    table.drop_index(index.name)
    generation_repository.close()
