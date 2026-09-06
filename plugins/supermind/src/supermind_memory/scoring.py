"""Deterministic economics and reuse scoring rules."""

from dataclasses import fields

from supermind_memory.types import ReuseScoreInputs, ValueInputs


def expected_net_value(inputs: ValueInputs) -> float:
    """Calculate extraction value after all specified costs and risks."""
    return (
        inputs.expected_reuse_count * inputs.benefit_per_reuse
        - inputs.extraction_cost
        - inputs.integration_cost
        - inputs.verification_cost
        - inputs.maintenance_cost
        - inputs.failure_risk
    )


def reuse_score(inputs: ReuseScoreInputs) -> float:
    """Score a reuse candidate from normalized, explicitly weighted inputs."""
    for field in fields(inputs):
        value = getattr(inputs, field.name)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{field.name} must be between 0.0 and 1.0")

    return (
        inputs.contract_fit * 0.35
        + inputs.requirement_fit * 0.20
        + inputs.reliability * 0.20
        + inputs.historical_benefit * 0.15
        - inputs.integration_cost * 0.05
        - inputs.maintenance_risk * 0.05
    )
