from __future__ import annotations

from dataclasses import fields

import pytest

from supermind_memory import config
from supermind_memory.config import MemoryPaths, resolve_codex_home
from supermind_memory.types import (
    CandidateMatch,
    Capability,
    DiscoveryContext,
    DiscoveryResult,
    EvaluationResult,
    Event,
    Evidence,
    HealthReport,
    InspectFilter,
    Relationship,
    RequirementEvent,
    RequirementObservation,
    RequirementProfile,
    ReuseDecision,
    ReuseResult,
    ReuseScoreInputs,
    SearchResult,
    SearchStatus,
    ValueInputs,
)


def sample_match() -> CandidateMatch:
    return CandidateMatch(
        capability_id="capability-1",
        vector_score=0.9,
        lexical_score=0.8,
        contract_fit=0.7,
        requirement_fit=0.6,
        reliability=0.95,
        historical_benefit=1.0,
        integration_cost=0.2,
        maintenance_risk=0.1,
        reuse_score=0.85,
    )


def test_resolve_codex_home_prefers_existing_codex_home(tmp_path):
    assert resolve_codex_home({"CODEX_HOME": str(tmp_path)}) == tmp_path.resolve()


def test_search_failure_cannot_contain_matches():
    with pytest.raises(ValueError, match="failed search cannot contain matches"):
        SearchResult(
            status=SearchStatus.FAILED,
            matches=(sample_match(),),
            error_code="index_unhealthy",
        )


def test_complete_empty_search_is_a_no_match():
    result = SearchResult(status=SearchStatus.COMPLETE, matches=())

    assert result.is_no_match is True


def test_failed_search_requires_an_error_code():
    with pytest.raises(ValueError, match="failed search requires an error code"):
        SearchResult(status=SearchStatus.FAILED, matches=())


@pytest.mark.parametrize("error_field", ["error_code", "error_message"])
def test_complete_search_cannot_contain_error_fields(error_field):
    with pytest.raises(ValueError, match="complete search cannot contain error fields"):
        SearchResult(status=SearchStatus.COMPLETE, matches=(), **{error_field: "unexpected"})


def test_resolve_codex_home_falls_back_to_the_user_codex_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))

    assert resolve_codex_home({}) == (tmp_path / ".codex").resolve()


def test_memory_paths_are_exact_owned_children_of_the_capability_memory_root(tmp_path):
    root = (tmp_path / "codex-home").resolve()
    paths = MemoryPaths.from_codex_home(root)

    assert paths.root == root / "supermind" / "memory"
    assert {
        "config": paths.config,
        "checkout": paths.checkout,
        "database": paths.database,
        "runtime": paths.runtime,
        "model_cache": paths.model_cache,
        "locks": paths.locks,
        "generations": paths.generations,
    } == {
        "config": paths.root / "config.json",
        "checkout": paths.root / "repository",
        "database": paths.root / "derived" / "database",
        "runtime": paths.root / "derived" / "runtime",
        "model_cache": paths.root / "model-cache",
        "locks": paths.root / "locks",
        "generations": paths.root / "derived" / "generations",
    }


def test_domain_dataclasses_are_frozen_and_keep_their_declared_fields():
    expected_fields = {
        RequirementProfile: ("id", "project_id", "intent", "contract", "category_hint", "stack", "constraints", "quality_requirements", "runtime", "platform", "license"),
        Capability: ("id", "name", "summary", "category_path", "facets", "contract", "constraints", "artifact_type", "source_uri", "source_revision", "content_hash", "owner", "license", "stack", "runtime", "platform", "dependencies", "compatibility", "lifecycle", "confidence", "expected_net_value", "embedding_generation", "created_at", "updated_at", "last_verified_at", "abstraction_status"),
        Evidence: ("id", "capability_id", "source_project", "evidence_type", "outcome", "metric_name", "metric_value", "confidence", "observed_at", "supporting_uri", "integration_effort", "benefit", "failure_risk"),
        Relationship: ("id", "source_id", "target_id", "relationship_type", "compatibility", "evidence_ids"),
        Event: ("id", "capability_id", "event_type", "source_context", "occurred_at", "previous_state", "resulting_state", "reason"),
        CandidateMatch: ("capability_id", "vector_score", "lexical_score", "contract_fit", "requirement_fit", "reliability", "historical_benefit", "integration_cost", "maintenance_risk", "reuse_score", "rejection_reasons", "source_available"),
        SearchResult: ("status", "matches", "generation", "error_code", "error_message", "capability_snapshots"),
        RequirementObservation: ("id", "requirement", "status", "observed_at", "linked_capability_id"),
        RequirementEvent: ("id", "observation_id", "event_type", "occurred_at", "capability_id", "reason"),
        HealthReport: ("healthy", "active_generation", "repairs", "checked_at", "failures"),
        ValueInputs: ("expected_reuse_count", "benefit_per_reuse", "extraction_cost", "integration_cost", "verification_cost", "maintenance_cost", "failure_risk"),
        ReuseScoreInputs: ("contract_fit", "requirement_fit", "reliability", "historical_benefit", "integration_cost", "maintenance_risk"),
        InspectFilter: ("category", "lifecycle", "stack", "capability_id"),
        DiscoveryContext: ("project_root", "codex_home"),
        DiscoveryResult: ("capabilities", "sources_scanned"),
        EvaluationResult: ("capability", "expected_net_value", "accepted", "reasons"),
        ReuseResult: ("capability_id", "project", "succeeded", "integration_effort", "benefit", "failure_reason"),
        ReuseDecision: ("requirement", "search_result", "action", "selected_capability_id", "rationale", "human_confirmation_required", "abstraction_review_required", "execution_authorized"),
        MemoryPaths: ("root", "config", "checkout", "database", "model_cache", "locks", "generations"),
    }

    assert {
        record: tuple(field.name for field in fields(record))
        for record in expected_fields
    } == expected_fields
    assert all(record.__dataclass_params__.frozen for record in expected_fields)
