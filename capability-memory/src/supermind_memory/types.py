"""Immutable domain records for capability memory."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Lifecycle(str, Enum):
    OBSERVED = "observed"
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    RECOMMENDED = "recommended"
    DEGRADED = "degraded"
    RETIRED = "retired"


class ArtifactType(str, Enum):
    CODE = "code"
    TEMPLATE = "template"
    SKILL = "skill"
    PLUGIN = "plugin"
    TOOL = "tool"
    API = "api"
    DATASET = "dataset"
    SERVICE = "service"


class SearchStatus(str, Enum):
    COMPLETE = "complete"
    FAILED = "failed"


class ViewType(str, Enum):
    OVERVIEW = "overview"
    TABLE = "table"
    DETAIL = "detail"
    DECISION = "decision"
    GRAPH = "graph"


@dataclass(frozen=True)
class RequirementProfile:
    id: str
    project_id: str
    intent: str
    contract: str = ""
    category_hint: tuple[str, ...] = ()
    stack: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    quality_requirements: tuple[str, ...] = ()
    runtime: tuple[str, ...] = ()
    platform: tuple[str, ...] = ()
    license: tuple[str, ...] = ()


@dataclass(frozen=True)
class Capability:
    id: str
    name: str
    summary: str
    category_path: tuple[str, ...]
    facets: tuple[str, ...]
    contract: str
    constraints: tuple[str, ...]
    artifact_type: ArtifactType
    source_uri: str
    source_revision: str
    content_hash: str
    owner: str
    license: str
    stack: tuple[str, ...]
    runtime: tuple[str, ...]
    platform: tuple[str, ...]
    dependencies: tuple[str, ...]
    compatibility: tuple[str, ...]
    lifecycle: Lifecycle
    confidence: float
    expected_net_value: float
    embedding_generation: str
    created_at: str
    updated_at: str
    last_verified_at: str | None


@dataclass(frozen=True)
class Evidence:
    id: str
    capability_id: str
    source_project: str
    evidence_type: str
    outcome: str
    metric_name: str | None
    metric_value: float | None
    confidence: float
    observed_at: str
    supporting_uri: str | None
    integration_effort: float = 0.0
    benefit: float = 0.0
    failure_risk: float = 0.0


@dataclass(frozen=True)
class Relationship:
    id: str
    source_id: str
    target_id: str
    relationship_type: str
    compatibility: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Event:
    id: str
    capability_id: str
    event_type: str
    source_context: str
    occurred_at: str
    previous_state: Lifecycle | None
    resulting_state: Lifecycle
    reason: str


@dataclass(frozen=True)
class CandidateMatch:
    capability_id: str
    vector_score: float
    lexical_score: float
    contract_fit: float
    requirement_fit: float
    reliability: float
    historical_benefit: float
    integration_cost: float
    maintenance_risk: float
    reuse_score: float
    rejection_reasons: tuple[str, ...] = ()
    source_available: bool | None = None


@dataclass(frozen=True)
class SearchResult:
    status: SearchStatus
    matches: tuple[CandidateMatch, ...]
    generation: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    capability_snapshots: tuple[Capability, ...] = ()

    def __post_init__(self) -> None:
        from supermind_memory.redaction import redact_text

        if self.error_message is not None:
            object.__setattr__(self, "error_message", redact_text(self.error_message))
        if self.status is SearchStatus.FAILED:
            if self.matches:
                raise ValueError("failed search cannot contain matches")
            if self.capability_snapshots:
                raise ValueError("failed search cannot contain capability snapshots")
            if not self.error_code:
                raise ValueError("failed search requires an error code")
        elif self.status is SearchStatus.COMPLETE and (
            self.error_code is not None or self.error_message is not None
        ):
            raise ValueError("complete search cannot contain error fields")
        snapshot_ids = tuple(item.id for item in self.capability_snapshots)
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("search capability snapshots must have unique ids")
        match_ids = {item.capability_id for item in self.matches}
        if any(identifier not in match_ids for identifier in snapshot_ids):
            raise ValueError("search capability snapshot must belong to a match")

    @property
    def is_no_match(self) -> bool:
        return self.status is SearchStatus.COMPLETE and not self.matches


@dataclass(frozen=True)
class HealthReport:
    healthy: bool
    active_generation: str | None
    repairs: tuple[str, ...]
    checked_at: str
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValueInputs:
    expected_reuse_count: float
    benefit_per_reuse: float
    extraction_cost: float
    integration_cost: float
    verification_cost: float
    maintenance_cost: float
    failure_risk: float


@dataclass(frozen=True)
class ReuseScoreInputs:
    contract_fit: float
    requirement_fit: float
    reliability: float
    historical_benefit: float
    integration_cost: float
    maintenance_risk: float


@dataclass(frozen=True)
class InspectFilter:
    category: tuple[str, ...] = ()
    lifecycle: tuple[Lifecycle, ...] = ()
    stack: tuple[str, ...] = ()
    capability_id: str | None = None


@dataclass(frozen=True)
class DiscoveryContext:
    project_root: Path
    codex_home: Path


@dataclass(frozen=True)
class DiscoveryResult:
    capabilities: tuple[Capability, ...]
    sources_scanned: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationResult:
    capability: Capability | None
    expected_net_value: float
    accepted: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ReuseResult:
    capability_id: str
    project: str
    succeeded: bool
    integration_effort: float
    benefit: float
    failure_reason: str | None = None


@dataclass(frozen=True)
class ReuseDecision:
    requirement: RequirementProfile
    search_result: SearchResult
    action: str
    selected_capability_id: str | None
    rationale: tuple[str, ...]


@dataclass(frozen=True)
class RequirementObservation:
    id: str
    requirement: RequirementProfile
    status: str
    observed_at: str
    linked_capability_id: str | None = None


@dataclass(frozen=True)
class RequirementEvent:
    id: str
    observation_id: str
    event_type: str
    occurred_at: str
    capability_id: str | None = None
    reason: str = ""


class CapabilityMemoryBlocked(RuntimeError):
    """Raised when a required capability-memory invariant cannot be repaired."""

    def __init__(self, code: str, message: str, attempts: tuple[str, ...]):
        from supermind_memory.redaction import redact_text

        message = redact_text(message)
        attempts = tuple(redact_text(attempt) for attempt in attempts)
        self.code = code
        self.message = message
        self.attempts = attempts
        super().__init__(message)
