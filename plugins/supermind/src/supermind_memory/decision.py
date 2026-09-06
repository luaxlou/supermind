"""One typed reuse-decision policy shared by every presentation boundary."""

from __future__ import annotations

import math
from collections.abc import Callable

from supermind_memory.compatibility import ContractFit, contract_fit, metadata_compatible
from supermind_memory.redaction import redact_requirement
from supermind_memory.search import requirement_declares_contract
from supermind_memory.source_resolution import source_available
from supermind_memory.types import (
    CandidateMatch,
    Capability,
    Lifecycle,
    RequirementProfile,
    ReuseDecision,
    SearchResult,
    SearchStatus,
)


CapabilityResolver = Callable[[str], Capability | None]


class ReuseDecisionEngine:
    """Apply all mandatory gates to generation-bound capability snapshots."""

    def decide(
        self,
        requirement: RequirementProfile,
        result: SearchResult,
        *,
        fallback_resolver: CapabilityResolver | None = None,
    ) -> ReuseDecision:
        requirement = redact_requirement(requirement)
        if result.status is not SearchStatus.COMPLETE:
            raise ValueError("reuse decision requires a complete search")
        snapshots = {item.id: item for item in result.capability_snapshots}
        assessed = tuple(
            (
                match,
                capability := snapshots.get(match.capability_id)
                or (
                    fallback_resolver(match.capability_id)
                    if not result.capability_snapshots and fallback_resolver is not None
                    else None
                ),
                (
                    contract_fit(capability, requirement)
                    if capability is not None and result.capability_snapshots
                    else ContractFit(match.contract_fit)
                ),
            )
            for match in result.matches
        )
        selected_assessment = next(
            (
                (match, fit)
                for match, capability, fit in assessed
                if not eligibility_reasons(
                    requirement,
                    match,
                    capability,
                    contract_proof=fit,
                )
            ),
            None,
        )
        declares_contract = requirement_declares_contract(requirement)
        if selected_assessment is None:
            selected = None
            action = "build"
            rationale = (
                "Capability Memory search completed.",
                (
                    "No verified positive-value capability with a positive contract fit "
                    "satisfied the declared contract."
                    if declares_contract
                    else "No verified positive-value capability satisfied the requirement; "
                    "the requirement declared no contract terms."
                ),
            )
        else:
            selected, selected_fit = selected_assessment
            if declares_contract and selected_fit.score < 1.0:
                action = "adapt"
                rationale = (
                    "Capability Memory search completed.",
                    f"Selected {selected.capability_id} for adaptation because partial contract fit "
                    f"({selected_fit.score:.2f}) requires changes before reuse.",
                )
            else:
                action = "reuse"
                rationale = (
                    "Capability Memory search completed.",
                    (
                        f"Selected {selected.capability_id} with exact contract fit (1.00) and current "
                        "evidence and expected value."
                        if declares_contract
                        else f"Selected {selected.capability_id}; the requirement declared no contract terms, "
                        "and the remaining evidence and expected-value gates passed."
                    ),
                )
        return ReuseDecision(
            requirement=requirement,
            search_result=result,
            action=action,
            selected_capability_id=(selected.capability_id if selected else None),
            rationale=rationale,
        )


def eligibility_reasons(
    requirement: RequirementProfile,
    match: CandidateMatch,
    capability: Capability | None,
    *,
    contract_proof: ContractFit | None = None,
) -> tuple[str, ...]:
    reasons = list(match.rejection_reasons)
    if capability is None:
        if requirement_declares_contract(requirement) and match.contract_fit <= 0.0:
            reasons.append("contract mismatch")
        reasons.append("record missing")
        return tuple(dict.fromkeys(reasons))
    fit = contract_proof or contract_fit(capability, requirement)
    if requirement_declares_contract(requirement) and fit.score <= 0.0:
        reasons.append("contract mismatch")
    if not metadata_compatible(capability, requirement):
        reasons.append("typed requirement filters do not match")
    if capability.lifecycle not in {Lifecycle.VERIFIED, Lifecycle.RECOMMENDED}:
        reasons.append(f"lifecycle not reusable: {capability.lifecycle.value}")
    if capability.last_verified_at is None:
        reasons.append("current verification missing")
    if not math.isfinite(capability.expected_net_value) or capability.expected_net_value <= 0:
        reasons.append("expected net value is not positive")
    is_source_available = (
        match.source_available
        if match.source_available is not None
        else source_available(capability, ())
    )
    if not is_source_available:
        reasons.append("source unavailable")
    return tuple(dict.fromkeys(reasons))
