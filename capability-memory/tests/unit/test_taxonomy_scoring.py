from __future__ import annotations

import pytest

from dataclasses import replace

from supermind_memory.lifecycle import next_lifecycle
from supermind_memory.scoring import expected_net_value, reuse_score
from supermind_memory.taxonomy import TOP_LEVEL_CATEGORIES, validate_category_path
from supermind_memory.types import ArtifactType, Capability, Evidence, Lifecycle, ReuseScoreInputs, ValueInputs


def candidate() -> Capability:
    return Capability(
        id="capability-1",
        name="Login",
        summary="OAuth login",
        category_path=("Code and components", "Identity and access"),
        facets=(),
        contract="OAuth callback",
        constraints=(),
        artifact_type=ArtifactType.CODE,
        source_uri="/project/login.py",
        source_revision="abc123",
        content_hash="hash-a",
        owner="team",
        license="MIT",
        stack=("Python",),
        runtime=(),
        platform=(),
        dependencies=(),
        compatibility=(),
        lifecycle=Lifecycle.CANDIDATE,
        confidence=1.0,
        expected_net_value=1.0,
        embedding_generation="generation-1",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
        last_verified_at=None,
    )


def evidence(evidence_type: str, outcome: str, project: str = "project-a") -> Evidence:
    return Evidence(
        id=f"{evidence_type}-{outcome}-{project}",
        capability_id="capability-1",
        source_project=project,
        evidence_type=evidence_type,
        outcome=outcome,
        metric_name=None,
        metric_value=None,
        confidence=1.0,
        observed_at="2026-09-04T00:00:00Z",
        supporting_uri=None,
    )


def test_one_time_generic_code_with_negative_net_value_stays_observed():
    value = expected_net_value(
        ValueInputs(
            expected_reuse_count=1,
            benefit_per_reuse=4,
            extraction_cost=3,
            integration_cost=1,
            verification_cost=1,
            maintenance_cost=1,
            failure_risk=1,
        )
    )

    assert value == -3


def test_reuse_score_uses_the_specified_weights():
    score = reuse_score(
        ReuseScoreInputs(
            contract_fit=1.0,
            requirement_fit=0.5,
            reliability=0.5,
            historical_benefit=1.0,
            integration_cost=0.5,
            maintenance_risk=0.5,
        )
    )

    assert score == pytest.approx(0.65)


@pytest.mark.parametrize("field", ["contract_fit", "maintenance_risk"])
def test_reuse_score_rejects_non_normalized_inputs(field: str):
    inputs = ReuseScoreInputs(1.0, 1.0, 1.0, 1.0, 0.0, 0.0)
    invalid_inputs = inputs.__class__(**{**inputs.__dict__, field: 1.01})

    with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
        reuse_score(invalid_inputs)


def test_verified_capability_requires_independent_reuse_before_recommended():
    records = [evidence("verification", "passed"), evidence("reuse", "success")]

    assert next_lifecycle(candidate(), records) == Lifecycle.VERIFIED

    records.append(evidence("reuse", "success", project="project-b"))
    assert next_lifecycle(candidate(), records) == Lifecycle.RECOMMENDED


def test_failed_verification_revokes_prior_verification_and_degrades():
    records = [
        evidence("verification", "passed"),
        evidence("reuse", "success", project="project-b"),
        evidence("verification", "failed"),
    ]

    assert next_lifecycle(candidate(), records) == Lifecycle.DEGRADED


def test_prior_recommended_lifecycle_without_current_evidence_is_not_recommended():
    previously_recommended = replace(candidate(), lifecycle=Lifecycle.RECOMMENDED)

    assert next_lifecycle(previously_recommended, []) == Lifecycle.CANDIDATE


def test_source_recovery_requires_new_verification_before_recommendation():
    records = [
        evidence("verification", "passed"),
        evidence("reuse", "success", project="project-b"),
        evidence("source_availability", "unavailable"),
        evidence("source_availability", "available"),
    ]

    assert next_lifecycle(candidate(), records) == Lifecycle.DEGRADED


def test_non_positive_value_capability_remains_observed_despite_verification():
    records = [evidence("verification", "passed"), evidence("reuse", "success", project="project-b")]
    non_valuable = replace(candidate(), expected_net_value=0.0)

    assert next_lifecycle(non_valuable, records) == Lifecycle.OBSERVED


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        ([evidence("source_availability", "unavailable")], "source unavailable"),
        ([evidence("content_hash", "changed")], "content hash changed"),
        ([evidence("reuse", "failed")], "latest reuse failed"),
    ],
)
def test_stale_or_failed_evidence_degrades_capability(records, expected):
    assert next_lifecycle(candidate(), records) == Lifecycle.DEGRADED, expected


def test_reverification_after_content_hash_change_restores_verified_lifecycle():
    records = [evidence("content_hash", "changed"), evidence("verification", "passed")]

    assert next_lifecycle(candidate(), records) == Lifecycle.VERIFIED


def test_unknown_top_level_category_is_rejected():
    with pytest.raises(ValueError, match="unknown top-level category"):
        validate_category_path(("Miscellaneous", "Login"))


def test_category_path_requires_a_primary_category():
    with pytest.raises(ValueError, match="at least one category"):
        validate_category_path(())


def test_top_level_categories_are_the_six_stable_roots():
    assert TOP_LEVEL_CATEGORIES == (
        "Code and components",
        "Product and business",
        "Design and experience",
        "Engineering and methods",
        "Tools and integrations",
        "Data and intelligence",
    )
