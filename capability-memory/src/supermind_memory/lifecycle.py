"""Evidence-driven lifecycle transitions for capability records."""

from collections.abc import Sequence

from supermind_memory.types import Capability, Evidence, Lifecycle


_SUCCESSFUL_OUTCOMES = frozenset({"success", "successful", "passed", "pass"})
_UNAVAILABLE_OUTCOMES = frozenset({"unavailable", "missing", "deleted"})
_CHANGED_OUTCOMES = frozenset({"changed", "mismatch", "stale"})
_FAILED_OUTCOMES = frozenset({"failed", "failure", "error"})


def _normalized(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _is_type(record: Evidence, *names: str) -> bool:
    return _normalized(record.evidence_type) in names


def next_lifecycle(capability: Capability, evidence: Sequence[Evidence]) -> Lifecycle:
    """Derive lifecycle state solely from the ordered evidence supplied."""
    if capability.lifecycle is Lifecycle.RETIRED:
        return Lifecycle.RETIRED

    source_available = True
    verification_project: str | None = None
    requires_reverification = False
    latest_reuse_failed = False
    independent_successful_reuse = False

    for record in evidence:
        outcome = _normalized(record.outcome)
        if _is_type(record, "source", "source_availability"):
            if outcome in _UNAVAILABLE_OUTCOMES:
                source_available = False
                verification_project = None
                requires_reverification = True
                independent_successful_reuse = False
            elif outcome in _SUCCESSFUL_OUTCOMES | {"available"}:
                source_available = True
        elif _is_type(record, "content_hash", "hash") and outcome in _CHANGED_OUTCOMES:
            requires_reverification = True
            verification_project = None
            independent_successful_reuse = False
        elif _is_type(record, "verification"):
            if outcome in _SUCCESSFUL_OUTCOMES:
                requires_reverification = False
                verification_project = record.source_project
                independent_successful_reuse = False
            elif outcome in _FAILED_OUTCOMES:
                requires_reverification = True
                verification_project = None
                independent_successful_reuse = False
        elif _is_type(record, "reuse", "reuse_outcome"):
            latest_reuse_failed = outcome in _FAILED_OUTCOMES
            if (
                outcome in _SUCCESSFUL_OUTCOMES
                and verification_project is not None
                and record.source_project != verification_project
            ):
                independent_successful_reuse = True

    if not source_available or requires_reverification or latest_reuse_failed:
        return Lifecycle.DEGRADED
    if capability.expected_net_value <= 0:
        return Lifecycle.OBSERVED
    if verification_project is None:
        return Lifecycle.CANDIDATE
    if independent_successful_reuse:
        return Lifecycle.RECOMMENDED
    return Lifecycle.VERIFIED
