"""Mandatory Capability Memory hooks for Supermind product work."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from supermind_memory.decision import ReuseDecisionEngine
from supermind_memory.service import CapabilityMemory
from supermind_memory.types import (
    Capability,
    CapabilityMemoryBlocked,
    Evidence,
    RequirementProfile,
    ReuseDecision,
    ReuseResult,
    SearchStatus,
    ValueInputs,
)


class SupermindWorkflow:
    """Apply fail-closed capability reuse at Supermind workflow boundaries."""

    def __init__(self, memory: CapabilityMemory) -> None:
        self.memory = memory

    def begin_design(
        self,
        project_root: Path,
        requirement: RequirementProfile,
    ) -> ReuseDecision:
        """Refresh all known sources and complete retrieval before design begins."""
        self.memory.initialize(project_root)
        self.memory.refresh_sources(project_root)
        health = self.memory.health_check()
        if not health.healthy:
            code = (
                health.failures[0].partition(":")[0]
                if health.failures
                else "health_unavailable"
            )
            raise CapabilityMemoryBlocked(
                code,
                "capability memory is unhealthy before design",
                health.failures,
            )

        result = self.memory.search(requirement)
        if result.status is not SearchStatus.COMPLETE:
            message = result.error_message or "capability search did not complete"
            raise CapabilityMemoryBlocked(
                result.error_code or "search_failed",
                message,
                (message,),
            )

        decision = ReuseDecisionEngine().decide(
            requirement,
            result,
            fallback_resolver=self.memory.get,
        )
        if decision.action == "build":
            self.memory.observe_unmet_requirement(requirement)
        return decision

    def complete_implementation(
        self,
        capability: Capability,
        inputs: ValueInputs,
        evidence: Sequence[Evidence],
    ) -> Capability | None:
        """Evaluate verified work and register it only when expected value is positive."""
        evaluation = self.memory.evaluate(capability, inputs)
        if not evaluation.accepted or evaluation.capability is None:
            return None
        return self.memory.register(evaluation.capability, evidence)

    def complete_reuse(self, result: ReuseResult) -> Capability:
        """Record every reuse outcome and return the resulting capability state."""
        return self.memory.record_use(result)
