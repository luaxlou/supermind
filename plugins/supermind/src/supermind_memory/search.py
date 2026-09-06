"""Complete hybrid capability retrieval and reuse ranking."""

from __future__ import annotations

import math
from collections.abc import Sequence

from supermind_memory.compatibility import (
    contract_fit,
    metadata_compatible as _metadata_compatible,
    relationship_compatible,
)
from supermind_memory.embeddings import EmbeddingProvider
from supermind_memory.redaction import redact_capability, redact_requirement, redact_text
from supermind_memory.repository import CapabilityRepository, SearchHealthToken
from supermind_memory.scoring import reuse_score
from supermind_memory.source_resolution import source_available
from supermind_memory.types import (
    CandidateMatch,
    Capability,
    Lifecycle,
    RequirementProfile,
    ReuseScoreInputs,
    SearchResult,
    SearchStatus,
)


class CapabilitySearch:
    """Search capabilities only when semantic and lexical retrieval both succeed."""

    def __init__(self, repository: CapabilityRepository, embeddings: EmbeddingProvider) -> None:
        self._repository = repository
        self._embeddings = embeddings

    def search(
        self,
        requirement: RequirementProfile,
        limit: int = 10,
        *,
        health_token: SearchHealthToken | None = None,
    ) -> SearchResult:
        try:
            requirement = redact_requirement(requirement)
            if limit <= 0:
                raise ValueError("search limit must be positive")
            snapshot_reader = getattr(self._repository, "search_snapshot", None)
            if snapshot_reader is None:
                search_generation = None
                available_capabilities = self._repository.list_capabilities()
            else:
                search_generation, available_capabilities = snapshot_reader()
            if health_token is not None and search_generation != health_token.generation:
                raise RuntimeError("search generation changed after the health check")
            capabilities = {
                capability.id: capability
                for capability in map(redact_capability, available_capabilities)
                if _metadata_compatible(capability, requirement)
            }
            if not capabilities:
                return SearchResult(
                    status=SearchStatus.COMPLETE,
                    matches=(),
                    generation=search_generation,
                )
            query_text = _requirement_text(requirement)
            query_vector = self._embeddings.embed_query(query_text)
            # The final limit is a decision-output bound, not a retrieval-quality
            # shortcut. Exhaust this already metadata-prefiltered bounded set so
            # unusable high-similarity rows cannot hide a valid lower-ranked row.
            pool_limit = len(capabilities)
            search_arguments = {
                "query_text": query_text,
                "vector": query_vector,
                "where": _id_filter(tuple(capabilities)),
                "limit": pool_limit,
            }
            if snapshot_reader is not None:
                search_arguments["generation"] = search_generation
            vector_rows, lexical_rows, hybrid_rows = self._repository.hybrid_search(
                **search_arguments,
            )
            vector_scores = _rank_scores(vector_rows)
            lexical_scores = _rank_scores(lexical_rows)
            matches = []
            for row in hybrid_rows:
                item = capabilities.get(row["id"])
                if item is None or not _metadata_compatible(item, requirement):
                    continue
                matches.append(
                    self._candidate(
                        item,
                        requirement,
                        vector_scores.get(item.id, 0.0),
                        lexical_scores.get(item.id, 0.0),
                    )
                )
            matched_ids = {match.capability_id for match in matches}
            for capability_id in self._relationship_expansion(
                tuple(matched_ids),
                capabilities,
                requirement,
            ):
                if capability_id in matched_ids:
                    continue
                item = capabilities[capability_id]
                candidate = self._candidate(item, requirement, 0.0, 0.0)
                if candidate.rejection_reasons:
                    continue
                matches.append(candidate)
                matched_ids.add(capability_id)
            matches.sort(
                key=lambda match: (
                    bool(match.rejection_reasons),
                    -match.reuse_score,
                    match.capability_id,
                )
            )
            selected = tuple(matches[:limit])
            return SearchResult(
                status=SearchStatus.COMPLETE,
                matches=selected,
                generation=search_generation,
                capability_snapshots=tuple(
                    capabilities[match.capability_id] for match in selected
                ),
            )
        except Exception as exc:
            return SearchResult(
                status=SearchStatus.FAILED,
                matches=(),
                error_code="search_failed",
                error_message=redact_text(str(exc)),
            )

    def _candidate(
        self,
        capability: Capability,
        requirement: RequirementProfile,
        vector_score: float,
        lexical_score: float,
    ) -> CandidateMatch:
        evidence = self._repository.list_evidence(capability.id)
        successful = [item for item in evidence if item.outcome in {"passed", "success"}]
        fit = contract_fit(capability, requirement)
        is_source_available = source_available(capability, evidence)
        rejection_reasons = list(_candidate_rejections(capability, is_source_available))
        if requirement_declares_contract(requirement) and fit.score == 0.0:
            rejection_reasons.append("contract mismatch")
        requirement_fit = (vector_score + lexical_score) / 2.0
        historical_benefit = _bounded_mean(item.benefit for item in successful)
        integration_cost = _bounded_mean(item.integration_effort for item in evidence)
        maintenance_risk = _bounded_mean(item.failure_risk for item in evidence)
        score = reuse_score(
            ReuseScoreInputs(
                contract_fit=fit.score,
                requirement_fit=requirement_fit,
                reliability=capability.confidence,
                historical_benefit=historical_benefit,
                integration_cost=integration_cost,
                maintenance_risk=maintenance_risk,
            )
        )
        return CandidateMatch(
            capability_id=capability.id,
            vector_score=vector_score,
            lexical_score=lexical_score,
            contract_fit=fit.score,
            requirement_fit=requirement_fit,
            reliability=capability.confidence,
            historical_benefit=historical_benefit,
            integration_cost=integration_cost,
            maintenance_risk=maintenance_risk,
            reuse_score=score,
            rejection_reasons=tuple(rejection_reasons),
            source_available=is_source_available,
        )

    def _relationship_expansion(
        self,
        seed_ids: tuple[str, ...],
        capabilities: dict[str, Capability],
        requirement: RequirementProfile,
    ) -> tuple[str, ...]:
        relationship_reader = getattr(self._repository, "list_relationships", None)
        if relationship_reader is None:
            return ()
        expanded: set[str] = set()
        for seed_id in sorted(seed_ids):
            for relationship in relationship_reader(seed_id):
                endpoint_evidence = tuple(
                    item
                    for endpoint_id in sorted(
                        {relationship.source_id, relationship.target_id}
                    )
                    for item in self._repository.list_evidence(endpoint_id)
                )
                if not relationship_compatible(
                    relationship,
                    endpoint_evidence,
                    requirement,
                ):
                    continue
                related_id = (
                    relationship.target_id
                    if relationship.source_id == seed_id
                    else relationship.source_id
                )
                item = capabilities.get(related_id)
                if item is not None and _metadata_compatible(item, requirement):
                    expanded.add(related_id)
        return tuple(sorted(expanded))


def _requirement_text(requirement: RequirementProfile) -> str:
    return " ".join(
        value
        for value in (
            requirement.intent,
            requirement.contract,
            *requirement.category_hint,
            *requirement.stack,
            *requirement.constraints,
            *requirement.quality_requirements,
            *requirement.runtime,
            *requirement.platform,
            *requirement.license,
        )
        if value
    )


def _id_filter(capability_ids: tuple[str, ...]) -> str:
    values = ", ".join(f"'{_sql_literal(capability_id)}'" for capability_id in capability_ids)
    return f"id IN ({values})"


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _rank_scores(rows: Sequence[dict[str, Any]]) -> dict[str, float]:
    return {row["id"]: 1.0 / rank for rank, row in enumerate(rows, start=1)}


def _contract_fit(capability: Capability, requirement: RequirementProfile) -> float:
    """Compatibility float retained for the existing health-check boundary."""
    return contract_fit(capability, requirement).score


def requirement_declares_contract(requirement: RequirementProfile) -> bool:
    """Return whether contract compatibility is an explicit decision gate."""
    return bool(
        requirement.contract.strip()
        or any(constraint.strip() for constraint in requirement.constraints)
    )


def _candidate_rejections(
    capability: Capability,
    is_source_available: bool | None = None,
) -> tuple[str, ...]:
    reasons = []
    if capability.lifecycle not in {Lifecycle.VERIFIED, Lifecycle.RECOMMENDED}:
        reasons.append(f"lifecycle not reusable: {capability.lifecycle.value}")
    if capability.last_verified_at is None:
        reasons.append("current verification missing")
    if not math.isfinite(capability.expected_net_value) or capability.expected_net_value <= 0:
        reasons.append("expected net value is not positive")
    if is_source_available is None:
        is_source_available = source_available(capability, ())
    if not is_source_available:
        reasons.append("source unavailable")
    return tuple(reasons)


def _bounded_mean(values) -> float:
    materialized = list(values)
    if not materialized:
        return 0.0
    return max(0.0, min(1.0, sum(materialized) / len(materialized)))
