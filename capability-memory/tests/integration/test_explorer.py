from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys

import pytest

from supermind_memory.explorer import CapabilityExplorer
from supermind_memory.repository import CapabilityRepository
from supermind_memory.schema import EMBEDDING_DIMENSION
from supermind_memory.types import (
    ArtifactType,
    CandidateMatch,
    Capability,
    Evidence,
    InspectFilter,
    Lifecycle,
    Relationship,
    RequirementProfile,
    SearchResult,
    SearchStatus,
)


def requirement(intent: str) -> RequirementProfile:
    return RequirementProfile(id="requirement-1", project_id="project-c", intent=intent)


def capability(
    capability_id: str,
    *,
    name: str | None = None,
    category_path: tuple[str, ...] = ("code", "Identity and access"),
    lifecycle: Lifecycle = Lifecycle.CANDIDATE,
    confidence: float = 0.8,
) -> Capability:
    return Capability(
        abstraction_status="abstracted",
        id=capability_id,
        name=name or capability_id,
        summary="OAuth login capability",
        category_path=category_path,
        facets=("authentication",),
        contract="OAuth callback creates an authenticated session",
        constraints=("requires client secret",),
        artifact_type=ArtifactType.CODE,
        source_uri=f"/capabilities/{capability_id}",
        source_revision="abc123",
        content_hash=f"hash-{capability_id}",
        owner="identity-team",
        license="MIT",
        stack=("TypeScript",),
        runtime=("Node.js",),
        platform=("macOS",),
        dependencies=("authlib",),
        compatibility=("OAuth 2.0",),
        lifecycle=lifecycle,
        confidence=confidence,
        expected_net_value=3.5,
        embedding_generation="generation-1",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T01:00:00Z",
        last_verified_at="2026-09-04T01:00:00Z",
    )


def evidence(capability_id: str, evidence_id: str = "evidence-1") -> Evidence:
    return Evidence(
        id=evidence_id,
        capability_id=capability_id,
        source_project="project-a",
        evidence_type="reuse",
        outcome="success",
        metric_name="hours saved",
        metric_value=4.0,
        confidence=0.9,
        observed_at="2026-09-04T02:00:00Z",
        supporting_uri="/evidence/login.md",
        integration_effort=0.2,
        benefit=0.8,
        failure_risk=0.1,
    )


def add(repository: CapabilityRepository, item: Capability) -> None:
    repository.upsert_capability(item, [0.0] * EMBEDDING_DIMENSION)


@pytest.fixture
def explorer(tmp_path) -> CapabilityExplorer:
    repository = CapabilityRepository.open(tmp_path / "explorer.lance")
    repository.initialize()
    source = tmp_path / "oauth-login.ts"
    source.write_text("export const login = true;", encoding="utf-8")
    oauth = replace(
        capability("auth.oauth-login", lifecycle=Lifecycle.RECOMMENDED, confidence=0.9),
        source_uri=source.as_uri(),
    )
    jwt = capability("auth.jwt-login", lifecycle=Lifecycle.VERIFIED, confidence=0.8)
    legacy = capability("auth.legacy-login", lifecycle=Lifecycle.CANDIDATE, confidence=0.6)
    for item in (oauth, jwt, legacy):
        add(repository, item)
    repository.append_evidence(evidence(oauth.id))
    for relationship_type, target in (
        ("dependency", jwt.id),
        ("alternative", legacy.id),
        ("composition", legacy.id),
        ("replacement", legacy.id),
        ("consumer", legacy.id),
    ):
        repository.append_relationship(
            Relationship(
                id=f"relation-{relationship_type}",
                source_id=oauth.id,
                target_id=target,
                relationship_type=relationship_type,
                compatibility=(),
                evidence_ids=("evidence-1",),
            )
        )
    return CapabilityExplorer(repository)


@pytest.fixture
def login_result() -> SearchResult:
    return SearchResult(
        status=SearchStatus.COMPLETE,
        matches=(
            CandidateMatch(
                capability_id="auth.oauth-login",
                vector_score=0.8,
                lexical_score=0.7,
                contract_fit=1.0,
                requirement_fit=0.75,
                reliability=0.9,
                historical_benefit=0.8,
                integration_cost=0.2,
                maintenance_risk=0.1,
                reuse_score=0.82,
            ),
            CandidateMatch(
                capability_id="auth.jwt-login",
                vector_score=0.6,
                lexical_score=0.5,
                contract_fit=0.3,
                requirement_fit=0.55,
                reliability=0.8,
                historical_benefit=0.2,
                integration_cost=0.6,
                maintenance_risk=0.4,
                reuse_score=0.31,
                rejection_reasons=("does not create an authenticated session",),
            ),
        ),
        generation="generation-1",
    )


def test_overview_groups_capabilities_by_stable_top_level_category(explorer):
    output = explorer.overview()

    assert "### 代码（code）" in output
    assert '<th width="35%">名称</th>' in output
    assert '<th width="65%">说明</th>' in output
    assert "Lifecycle" not in output
    assert "Abstraction status" not in output


def test_overview_uses_the_catalog_presentation(explorer):
    assert explorer.overview() == explorer.table(InspectFilter())


def test_table_sorts_by_maturity_then_reuse_score_and_escapes_cells(tmp_path):
    repository = CapabilityRepository.open(tmp_path / "table.lance")
    repository.initialize()
    first = replace(capability("alpha", name="name | with\nnewline", lifecycle=Lifecycle.VERIFIED), confidence=0.2)
    second = replace(capability("beta", lifecycle=Lifecycle.VERIFIED), confidence=0.9)
    for item in (first, second):
        add(repository, item)

    output = CapabilityExplorer(repository).table(InspectFilter())

    assert output.index("beta") < output.index("alpha")
    assert "name | with\nnewline" in output


@pytest.mark.parametrize(
    ("filters", "expected_ids"),
    (
        (InspectFilter(category=("code",)), ("code-verified", "code-candidate")),
        (InspectFilter(lifecycle=(Lifecycle.VERIFIED,)), ("code-verified", "data-verified")),
        (InspectFilter(stack=("PYTHON",)), ("data-verified",)),
        (InspectFilter(capability_id="code-candidate"), ("code-candidate",)),
    ),
    ids=("category", "lifecycle", "stack", "identifier"),
)
def test_table_honors_every_inspect_filter_and_keeps_fixed_taxonomy_order(tmp_path, filters, expected_ids):
    repository = CapabilityRepository.open(tmp_path / "filters.lance")
    repository.initialize()
    records = (
        capability("data-verified", category_path=("data", "Models"), lifecycle=Lifecycle.VERIFIED),
        capability("code-verified", lifecycle=Lifecycle.VERIFIED),
        capability("code-candidate", lifecycle=Lifecycle.CANDIDATE),
    )
    records = (records[0], replace(records[1], stack=("TypeScript",)), replace(records[2], stack=("TypeScript",)))
    add(repository, replace(records[0], stack=("Python",)))
    add(repository, records[1])
    add(repository, records[2])

    output = CapabilityExplorer(repository).table(filters)

    import re
    actual_ids = tuple(re.findall(r'href="capabilities/([^"/]+)\.md"', output))
    assert actual_ids == expected_ids


def test_table_orders_categories_before_lifecycle_priority(tmp_path):
    repository = CapabilityRepository.open(tmp_path / "taxonomy-order.lance")
    repository.initialize()
    add(repository, capability("data-recommended", category_path=("data", "Models"), lifecycle=Lifecycle.RECOMMENDED))
    add(repository, capability("code-candidate", lifecycle=Lifecycle.CANDIDATE))

    output = CapabilityExplorer(repository).table(InspectFilter())

    assert output.index("code-candidate") < output.index("data-recommended")


def test_detail_focuses_on_use_without_audit_metadata(explorer):
    output = explorer.detail("auth.oauth-login")

    assert "Source revision" not in output
    assert "Maturity" not in output
    assert "Abstraction status" not in output
    for expected in (
        "使用说明",
        "使用条件",
        "OAuth callback creates an authenticated session",
    ):
        assert expected in output
    assert "验证记录" not in output
    assert "hours saved" not in output
    assert explorer._repository.list_evidence("auth.oauth-login")


def test_detail_handles_missing_capability_without_mutating_repository(explorer):
    before = explorer.overview()

    output = explorer.detail("missing-capability")

    assert output == "Capability not found: missing-capability\n"
    assert explorer.overview() == before


def test_decision_explains_why_login_was_selected(explorer, login_result):
    output = explorer.decision(requirement("OAuth 登录"), login_result)

    for expected in (
        "auth.oauth-login",
        "Contract fit",
        "Expected net value",
        "Source",
        "Selected action",
        "does not create an authenticated session",
    ):
        assert expected in output


def test_decision_reports_failed_search_without_candidates(explorer):
    result = SearchResult(
        status=SearchStatus.FAILED,
        matches=(),
        error_code="search_failed",
        error_message="index unavailable",
    )

    output = explorer.decision(requirement("OAuth 登录"), result)

    assert "Search failed" in output
    assert "search_failed" in output
    assert "index unavailable" in output
    assert "Candidates" not in output


def test_decision_skips_a_rejected_top_candidate_for_a_live_lower_candidate(explorer, login_result):
    rejected = replace(
        login_result.matches[0],
        capability_id="rejected-candidate",
        rejection_reasons=("contract contains <script>alert(1)</script>",),
        reuse_score=0.99,
    )
    result = replace(login_result, matches=(rejected, login_result.matches[0]))

    output = explorer.decision(requirement("OAuth 登录"), result)

    assert "Reuse capability: auth.oauth-login" in output
    assert "Reuse capability: rejected-candidate" not in output


@pytest.mark.parametrize("candidate_id", ("missing-candidate", "auth.oauth-login"), ids=("missing", "unavailable-source"))
def test_decision_never_reuses_missing_or_unavailable_candidate(explorer, login_result, candidate_id):
    if candidate_id == "auth.oauth-login":
        candidate = replace(login_result.matches[0], rejection_reasons=("source unavailable",))
    else:
        candidate = replace(login_result.matches[0], capability_id=candidate_id)
    result = replace(login_result, matches=(candidate,))

    output = explorer.decision(requirement("OAuth 登录"), result)

    assert "Build a new capability; no suitable reusable candidate." in output
    assert "Reuse capability:" not in output


@pytest.mark.parametrize(
    ("lifecycle", "reused"),
    (
        (Lifecycle.OBSERVED, False),
        (Lifecycle.CANDIDATE, False),
        (Lifecycle.VERIFIED, True),
        (Lifecycle.RECOMMENDED, True),
        (Lifecycle.DEGRADED, False),
        (Lifecycle.RETIRED, False),
    ),
    ids=("observed", "candidate", "verified", "recommended", "degraded", "retired"),
)
def test_decision_only_reuses_verified_or_recommended_lifecycles(tmp_path, lifecycle, reused):
    repository = CapabilityRepository.open(tmp_path / f"{lifecycle.value}.lance")
    repository.initialize()
    item = replace(
        capability("lifecycle-test", lifecycle=lifecycle),
        source_uri=Path(__file__).as_uri(),
    )
    add(repository, item)
    result = SearchResult(
        status=SearchStatus.COMPLETE,
        matches=(
            CandidateMatch(
                capability_id=item.id,
                vector_score=1.0,
                lexical_score=1.0,
                contract_fit=1.0,
                requirement_fit=1.0,
                reliability=1.0,
                historical_benefit=1.0,
                integration_cost=0.0,
                maintenance_risk=0.0,
                reuse_score=1.0,
            ),
        ),
    )

    output = CapabilityExplorer(repository).decision(requirement("reuse it"), result)

    assert ("Reuse capability: lifecycle-test" in output) is reused
    assert ("Build a new capability; no suitable reusable candidate." in output) is not reused
    if not reused:
        assert f"lifecycle not reusable: {lifecycle.value}" in output
    if lifecycle is Lifecycle.DEGRADED:
        assert "source unavailable" not in output
    if not reused:
        assert "## Rejections\n\n- None" not in output


def test_decision_explains_missing_record_and_never_has_empty_rejections(explorer, login_result):
    result = replace(login_result, matches=(replace(login_result.matches[0], capability_id="missing-record"),))

    output = explorer.decision(requirement("OAuth 登录"), result)

    assert "record missing" in output
    assert "Build a new capability; no suitable reusable candidate." in output
    assert "## Rejections\n\n- None" not in output


def test_decision_preserves_explicit_source_unavailable_rejection(explorer, login_result):
    result = replace(
        login_result,
        matches=(replace(login_result.matches[0], rejection_reasons=("source unavailable",)),),
    )

    output = explorer.decision(requirement("OAuth 登录"), result)

    assert "source unavailable" in output
    assert "Reuse capability:" not in output


@pytest.mark.parametrize(
    ("required", "offered", "expected_action"),
    (
        (
            "requires Python >=3.12,<4 with AES 256 encryption",
            "supports Python >=3.12,<4",
            "Adapt capability: contract-proof",
        ),
        (
            "platform Linux",
            "platform Windows",
            "Build a new capability; no suitable reusable candidate.",
        ),
    ),
)
def test_decision_uses_typed_contract_proof_instead_of_untrusted_match_score(
    tmp_path, required, offered, expected_action
):
    repository = CapabilityRepository.open(tmp_path / "decision-contract-proof.lance")
    repository.initialize()
    item = replace(
        capability("contract-proof", lifecycle=Lifecycle.VERIFIED),
        contract=offered,
        source_uri=Path(__file__).as_uri(),
    )
    add(repository, item)
    match = CandidateMatch(
        capability_id=item.id,
        vector_score=1.0,
        lexical_score=1.0,
        contract_fit=1.0,
        requirement_fit=1.0,
        reliability=1.0,
        historical_benefit=1.0,
        integration_cost=0.0,
        maintenance_risk=0.0,
        reuse_score=1.0,
    )
    result = SearchResult(
        status=SearchStatus.COMPLETE,
        matches=(match,),
        capability_snapshots=(item,),
    )
    wanted = RequirementProfile(
        id="requirement-contract-proof",
        project_id="project-c",
        intent="contract proof",
        contract=required,
    )

    output = CapabilityExplorer(repository).decision(wanted, result)

    assert expected_action in output


def test_generation_bound_snapshot_prevents_mutable_record_race(tmp_path):
    repository = CapabilityRepository.open(tmp_path / "decision-race.lance")
    repository.initialize()
    source = tmp_path / "live.py"
    source.write_text("available = True\n", encoding="utf-8")
    verified = replace(
        capability("generation-bound", lifecycle=Lifecycle.VERIFIED),
        source_uri=source.as_uri(),
        expected_net_value=2.0,
        last_verified_at="2026-09-04T00:00:00Z",
    )
    add(repository, replace(verified, lifecycle=Lifecycle.RETIRED))
    match = CandidateMatch(
        capability_id=verified.id,
        vector_score=1.0,
        lexical_score=1.0,
        contract_fit=1.0,
        requirement_fit=1.0,
        reliability=1.0,
        historical_benefit=1.0,
        integration_cost=0.0,
        maintenance_risk=0.0,
        reuse_score=1.0,
    )
    result = SearchResult(
        status=SearchStatus.COMPLETE,
        matches=(match,),
        generation="generation-stable",
        capability_snapshots=(verified,),
    )

    output = CapabilityExplorer(repository).decision(requirement("reuse it"), result)

    assert "Reuse capability: generation-bound" in output
    assert "retired" not in output


def test_central_decision_rejects_unavailable_snapshot_source_even_without_search_hint(
    tmp_path,
):
    repository = CapabilityRepository.open(tmp_path / "missing-source.lance")
    repository.initialize()
    item = replace(
        capability("missing-source", lifecycle=Lifecycle.VERIFIED),
        source_uri=(tmp_path / "absent.py").as_uri(),
        expected_net_value=2.0,
        last_verified_at="2026-09-04T00:00:00Z",
    )
    match = CandidateMatch(
        capability_id=item.id,
        vector_score=1.0,
        lexical_score=1.0,
        contract_fit=1.0,
        requirement_fit=1.0,
        reliability=1.0,
        historical_benefit=1.0,
        integration_cost=0.0,
        maintenance_risk=0.0,
        reuse_score=1.0,
    )
    result = SearchResult(
        SearchStatus.COMPLETE,
        (match,),
        capability_snapshots=(item,),
    )

    output = CapabilityExplorer(repository).decision(requirement("reuse it"), result)

    assert "Build a new capability; no suitable reusable candidate." in output
    assert "source unavailable" in output


def test_decision_rejects_fifo_source_without_blocking_on_it(tmp_path):
    fifo = tmp_path / "capability-source.fifo"
    os.mkfifo(fifo)
    plugin_root = Path(__file__).parents[2]
    script = f'''\
from supermind_memory.explorer import CapabilityExplorer
from supermind_memory.types import ArtifactType, CandidateMatch, Capability, Lifecycle, RequirementProfile, SearchResult, SearchStatus

capability = Capability(
        abstraction_status="abstracted",
        id="fifo-capability", name="FIFO", summary="", category_path=("code",), facets=(), contract="", constraints=(),
    artifact_type=ArtifactType.CODE, source_uri={str(fifo)!r}, source_revision="", content_hash="", owner="", license="", stack=(), runtime=(),
    platform=(), dependencies=(), compatibility=(), lifecycle=Lifecycle.VERIFIED, confidence=1.0, expected_net_value=1.0,
    embedding_generation="", created_at="", updated_at="", last_verified_at="2026-09-04T00:00:00Z",
)
match = CandidateMatch("fifo-capability", 1, 1, 1, 1, 1, 1, 0, 0, 1)
class Repository:
    def get_capability(self, capability_id): return capability
    def list_capabilities(self): return (capability,)
    def list_evidence(self, capability_id): return ()
    def list_relationships(self, capability_id): return ()
print(CapabilityExplorer(Repository()).decision(RequirementProfile("r", "p", "fifo"), SearchResult(SearchStatus.COMPLETE, (match,))))
'''
    environment = {**os.environ, "PYTHONPATH": str(plugin_root / "src")}

    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert "Build a new capability; no suitable reusable candidate." in completed.stdout
    assert "source unavailable" in completed.stdout


def test_decision_reports_complete_no_match_as_build_without_reuse(explorer):
    output = explorer.decision(
        requirement("OAuth 登录"),
        SearchResult(status=SearchStatus.COMPLETE, matches=()),
    )

    assert "Build a new capability; no reusable candidate matched." in output
    assert "Reuse capability:" not in output


def test_untrusted_markdown_and_html_render_as_inert_readable_text(explorer, login_result):
    attack = '<img src=x onerror=alert(1)> [click](https://evil.test) ``` \U0001f600'
    item = explorer._repository.get_capability("auth.oauth-login")
    explorer._repository.upsert_capability(replace(item, name=attack, summary=attack, contract=attack), [0.0] * EMBEDDING_DIMENSION)
    explorer._repository.append_evidence(replace(evidence(item.id, "attack-evidence"), metric_name=attack, supporting_uri=attack))
    result = replace(login_result, matches=(replace(login_result.matches[0], rejection_reasons=(attack,)),))

    outputs = (
        explorer.table(InspectFilter()),
        explorer.detail(item.id),
        explorer.decision(requirement(attack), result),
    )

    for output in outputs:
        assert "<img" not in output
        assert "[click](" not in output
        assert "https://evil.test" not in output
        assert "```" not in output
        assert "😀" in output
        assert "&lt;img" in output


def test_graph_is_valid_mermaid_with_all_stable_relationship_edges(explorer):
    output = explorer.graph(InspectFilter(category=("code",)))

    assert output.startswith("```mermaid\nflowchart LR\n")
    for expected in (
        "-->|depends on|",
        "-.->|alternative|",
        "==>|composes|",
        "-.->|replaces|",
        "-->|consumed by|",
    ):
        assert expected in output
    assert output.endswith("```\n")


def test_graph_escapes_malicious_labels_and_uses_stable_safe_node_identifiers(tmp_path):
    repository = CapabilityRepository.open(tmp_path / "graph.lance")
    repository.initialize()
    dangerous = capability("bad id ``` x", name='bad"] --> evil["node')
    add(repository, dangerous)

    explorer = CapabilityExplorer(repository)
    first = explorer.graph(InspectFilter())
    second = explorer.graph(InspectFilter())

    assert first == second
    assert first.count("```") == 2
    assert "bad id ``` x" not in first
    assert "evil[" not in first
    assert "cap_" in first


@pytest.mark.parametrize(
    "render",
    (
        lambda explorer, result: explorer.overview(),
        lambda explorer, result: explorer.table(InspectFilter()),
        lambda explorer, result: explorer.detail("auth.oauth-login"),
        lambda explorer, result: explorer.decision(requirement("OAuth 登录"), result),
        lambda explorer, result: explorer.graph(InspectFilter()),
    ),
    ids=("overview", "table", "detail", "decision", "graph"),
)
def test_every_view_is_byte_identical_on_repeated_reads(explorer, login_result, render):
    assert render(explorer, login_result) == render(explorer, login_result)
