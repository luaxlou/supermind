"""Conservative, typed compatibility proof for search and reuse decisions."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from supermind_memory.redaction import redact_text
from supermind_memory.types import (
    Capability,
    Evidence,
    Lifecycle,
    Relationship,
    RequirementProfile,
)


@dataclass(frozen=True)
class ContractClause:
    subject: str
    operator: str
    value: str
    polarity: int
    source: str


@dataclass(frozen=True)
class ContractFit:
    score: float
    unresolved: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


_CONTRACT_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "must",
        "shall",
        "should",
        "required",
        "requires",
        "require",
        "supports",
        "support",
        "with",
    }
)
_DIMENSION_SUBJECTS = frozenset({"license", "platform", "runtime", "stack"})
_SUCCESSFUL_OUTCOMES = frozenset({"passed", "pass", "success", "successful"})
_SUPPORTED_RELATIONSHIP_TYPES = frozenset(
    {
        "dependency",
        "depends_on",
        "depends-on",
        "alternative",
        "alternative_to",
        "alternative-to",
        "composition",
        "composed_of",
        "composed-of",
        "consumer",
        "consumed_by",
        "consumed-by",
    }
)
_PLATFORM_MARKERS = frozenset(
    {"android", "browser", "darwin", "ios", "linux", "macos", "unix", "web", "windows"}
)
_RUNTIME_MARKERS = frozenset(
    {
        "bun",
        "deno",
        "dotnet",
        "go",
        "java",
        "jvm",
        "node",
        "nodejs",
        "php",
        "python",
        "ruby",
        "rust",
    }
)
_LICENSE_MARKERS = frozenset(
    {"agpl", "apache", "bsd", "gpl", "lgpl", "mit", "mpl", "proprietary"}
)
_VERSION_SEQUENCE = re.compile(
    r"(?:>=|<=|==|>|<)\s*v?\d+(?:\.\d+)*"
    r"(?:\s*,\s*(?:>=|<=|==|>|<)\s*v?\d+(?:\.\d+)*)*"
)


def contract_fit(capability: Capability, requirement: RequirementProfile) -> ContractFit:
    required = parse_contract((requirement.contract, *requirement.constraints))
    if not required:
        return ContractFit(0.5)
    offered = parse_contract(
        (capability.contract, *capability.constraints, *capability.compatibility)
    )
    conflicts = find_conflicts(required, offered)
    if conflicts:
        return ContractFit(0.0, conflicts=conflicts)
    coverage = tuple(best_coverage(clause, offered) for clause in required)
    unresolved = tuple(
        clause.source
        for clause, value in zip(required, coverage, strict=True)
        if value < 1.0
    )
    score = max(0.0, min(1.0, sum(coverage) / len(coverage)))
    return ContractFit(score, unresolved=unresolved)


def metadata_compatible(
    capability: Capability,
    requirement: RequirementProfile,
) -> bool:
    if capability.lifecycle in {Lifecycle.DEGRADED, Lifecycle.RETIRED}:
        return False
    if requirement.category_hint:
        wanted_category = tuple(_normalized(value) for value in requirement.category_hint)
        offered_prefix = tuple(
            _normalized(value)
            for value in capability.category_path[: len(requirement.category_hint)]
        )
        if offered_prefix != wanted_category:
            return False
    compatibility = tuple(_normalized(value) for value in capability.compatibility)
    if requirement.stack:
        wanted = {_normalized(value) for value in requirement.stack}
        offered = {_normalized(value) for value in (*capability.stack, *compatibility)}
        if not wanted & offered:
            return False
    if requirement.runtime:
        wanted = {_normalized(value) for value in requirement.runtime}
        offered = {_normalized(value) for value in (*capability.runtime, *compatibility)}
        if not wanted <= offered:
            return False
    if requirement.platform:
        wanted = {_normalized(value) for value in requirement.platform}
        offered = {_normalized(value) for value in (*capability.platform, *compatibility)}
        if not wanted <= offered:
            return False
    if requirement.license:
        acceptable = {_normalized(value) for value in requirement.license}
        if _normalized(capability.license) not in acceptable:
            return False
    return True


def relationship_compatible(
    relationship: Relationship,
    evidence: Sequence[Evidence],
    requirement: RequirementProfile,
) -> bool:
    if _normalized(relationship.relationship_type) not in _SUPPORTED_RELATIONSHIP_TYPES:
        return False
    if not relationship.evidence_ids:
        return False
    endpoints = {relationship.source_id, relationship.target_id}
    endpoint_evidence = tuple(item for item in evidence if item.capability_id in endpoints)
    resolved = {item.id: item for item in endpoint_evidence}
    latest: dict[tuple[str, str, str], Evidence] = {}
    for item in endpoint_evidence:
        key = (
            item.capability_id,
            _normalized(item.evidence_type),
            _normalized(item.source_project),
        )
        current = latest.get(key)
        if current is None or (redact_text(item.observed_at), item.id) > (
            redact_text(current.observed_at),
            current.id,
        ):
            latest[key] = item
    for evidence_id in relationship.evidence_ids:
        item = resolved.get(evidence_id)
        if item is None:
            return False
        key = (
            item.capability_id,
            _normalized(item.evidence_type),
            _normalized(item.source_project),
        )
        if latest.get(key) != item:
            return False
        if _normalized(item.outcome) not in _SUCCESSFUL_OUTCOMES:
            return False
    return _relationship_dimensions_compatible(relationship.compatibility, requirement)


def _relationship_dimensions_compatible(
    compatibility: Sequence[str],
    requirement: RequirementProfile,
) -> bool:
    if not compatibility:
        return True
    required = {
        "stack": {_normalized(value) for value in requirement.stack},
        "runtime": {_normalized(value) for value in requirement.runtime},
        "platform": {_normalized(value) for value in requirement.platform},
        "license": {_normalized(value) for value in requirement.license},
    }
    offered = _relationship_dimensions(compatibility, required)
    for dimension, wanted in required.items():
        if not wanted:
            continue
        available = offered[dimension]
        if dimension in {"stack", "license"}:
            if not wanted & available:
                return False
        elif not wanted <= available:
            return False
    return True


def _relationship_dimensions(
    compatibility: Sequence[str],
    required: dict[str, set[str]],
) -> dict[str, set[str]]:
    dimensions = {name: set() for name in _DIMENSION_SUBJECTS}
    for raw_value in compatibility:
        value = _normalized(raw_value)
        explicit = re.match(
            r"^(stack|runtime|platform|license)\s*(?::|=|\bis\b|\s)\s*(.+)$",
            value,
        )
        if explicit is not None:
            dimensions[explicit.group(1)].add(explicit.group(2).strip())
            continue
        words = set(re.findall(r"[a-z0-9]+", value))
        if words & _PLATFORM_MARKERS:
            dimensions["platform"].add(value)
        if words & _LICENSE_MARKERS:
            dimensions["license"].add(value)
        if words & _RUNTIME_MARKERS:
            dimensions["runtime"].add(value)
            dimensions["stack"].add(value)
        for dimension, wanted in required.items():
            if value in wanted:
                dimensions[dimension].add(value)
    return dimensions


def _normalized(value: str) -> str:
    return " ".join(redact_text(value).casefold().split())


def parse_contract(values: Sequence[str]) -> tuple[ContractClause, ...]:
    clauses: list[ContractClause] = []
    for value in values:
        safe_value = redact_text(value)
        for raw_clause in re.split(
            r"[;\n]+|\s+and\s+(?=(?:must|shall|should|returns?|creates?|refreshes?|rotates?|uses?|supports?|requires?|platform|runtime|stack|license)\b)",
            safe_value,
            flags=re.IGNORECASE,
        ):
            pending = [raw_clause]
            while pending:
                raw = _strip_conjunction(pending.pop(0))
                if not raw:
                    continue
                parts = re.split(r"\s+with\s+", raw, maxsplit=1, flags=re.IGNORECASE)
                raw = parts[0].strip()
                if len(parts) == 2 and parts[1].strip():
                    pending.insert(0, parts[1])
                version, remaining = _consume_version_clause(raw)
                if version is not None:
                    clauses.append(version)
                    pending[0:0] = remaining
                    continue
                clauses.append(_parse_non_version_clause(raw))
    return tuple(clauses)


def find_conflicts(
    required: Sequence[ContractClause],
    offered: Sequence[ContractClause],
) -> tuple[str, ...]:
    return tuple(
        clause.source
        for clause in required
        if any(_clauses_conflict(clause, candidate) for candidate in offered)
    )


def best_coverage(
    required: ContractClause,
    offered: Sequence[ContractClause],
) -> float:
    return max((_clause_coverage(required, candidate) for candidate in offered), default=0.0)


def _strip_conjunction(value: str) -> str:
    return re.sub(r"^\s*(?:and|with)\s+", "", value, flags=re.IGNORECASE).strip(" ,")


def _consume_version_clause(
    raw: str,
) -> tuple[ContractClause | None, tuple[str, ...]]:
    match = _VERSION_SEQUENCE.search(raw)
    if match is None:
        return None, ()
    # Consume only the adjacent runtime and its modifiers. Earlier mandatory
    # text remains a separate clause, including unfamiliar conjunctions.
    subject = re.search(
        r"(?:(?:must|shall|should)\s+)?(?:not\s+)?"
        r"(?:(?:uses?|requires?|required|supports?|runtime|version)\s+)?"
        r"(?P<name>[a-z][\w.+-]*)\s*$", raw[:match.start()], re.IGNORECASE,
    )
    start = subject.start() if subject is not None else match.start()
    prefix = re.sub(r"(?:\s+(?:and|with)|,)\s*$", "", raw[:start], flags=re.IGNORECASE)
    source = " ".join(raw[start:match.end()].split())
    constraints = _version_constraints(match.group(0))
    value = ",".join(
        f"{operator}{'.'.join(str(part) for part in version)}"
        for operator, version in constraints
    )
    name = subject.group("name").casefold() if subject is not None else "version"
    if name in _CONTRACT_STOP_WORDS:
        name = "version"
    clause = ContractClause(
        subject=name,
        operator="range",
        value=value,
        polarity=_polarity(source),
        source=source,
    )
    return clause, tuple(value for value in (
        _strip_conjunction(prefix), _strip_conjunction(raw[match.end():]),
    ) if value)


def _parse_non_version_clause(raw: str) -> ContractClause:
    source = " ".join(raw.split())
    normalized = source.casefold()
    polarity = _polarity(normalized)
    assignment = re.search(
        r"^(.+?)\s+(?:must\s+be|shall\s+be|is|=)\s+([\w.+-]+)\s*$",
        normalized,
    )
    if assignment is not None:
        return ContractClause(
            subject=" ".join(sorted(_tokens(assignment.group(1)))),
            operator="=",
            value=_canonical_contract_token(assignment.group(2)),
            polarity=polarity,
            source=source,
        )
    ordered_tokens = tuple(
        _canonical_contract_token(token)
        for token in re.findall(r"[\w\u3400-\u9fff]+", normalized)
        if token not in _CONTRACT_STOP_WORDS and token not in {"not", "without"}
    )
    if (
        ordered_tokens
        and ordered_tokens[0] in _DIMENSION_SUBJECTS
        and len(ordered_tokens) > 1
    ):
        return ContractClause(
            subject=ordered_tokens[0],
            operator="=",
            value=" ".join(ordered_tokens[1:]),
            polarity=polarity,
            source=source,
        )
    tokens = tuple(sorted(ordered_tokens))
    return ContractClause(
        subject=" ".join(tokens),
        operator="contains",
        value="",
        polarity=polarity,
        source=source,
    )


def _polarity(value: str) -> int:
    return (
        -1
        if re.search(r"\b(?:must|shall|should)?\s*not\b|\bwithout\b", value, re.IGNORECASE)
        else 1
    )


def _tokens(value: str) -> set[str]:
    return {
        _canonical_contract_token(token)
        for token in re.findall(r"[\w\u3400-\u9fff]+", value.casefold())
        if token not in _CONTRACT_STOP_WORDS
    }


def _canonical_contract_token(token: str) -> str:
    return {
        "returns": "return",
        "returned": "return",
        "uses": "use",
        "used": "use",
        "creates": "create",
        "created": "create",
        "rotates": "rotate",
        "tokens": "token",
    }.get(token, token)


def _clauses_conflict(required: ContractClause, offered: ContractClause) -> bool:
    if required.operator == "range" and offered.operator == "range":
        if required.subject != offered.subject:
            return False
        overlaps = _ranges_overlap(
            _version_constraints(required.value),
            _version_constraints(offered.value),
        )
        if required.polarity != offered.polarity:
            return overlaps
        return required.polarity > 0 and not overlaps
    if required.operator == "=" and offered.operator == "=":
        return required.subject == offered.subject and (
            required.value != offered.value or required.polarity != offered.polarity
        )
    required_tokens = _clause_tokens(required)
    offered_tokens = _clause_tokens(offered)
    return required.polarity != offered.polarity and (
        required_tokens <= offered_tokens or offered_tokens <= required_tokens
    )


def _clause_coverage(required: ContractClause, offered: ContractClause) -> float:
    if required.polarity != offered.polarity:
        return 0.0
    if required.operator == "range":
        if offered.operator != "range" or required.subject != offered.subject:
            return 0.0
        required_range = _version_constraints(required.value)
        offered_range = _version_constraints(offered.value)
        if not _ranges_overlap(required_range, offered_range):
            return 0.0
        return 1.0 if _equivalent_version_ranges(required_range, offered_range) else 0.5
    if required.operator == "=":
        return 1.0 if (
            offered.operator == "="
            and required.subject == offered.subject
            and required.value == offered.value
        ) else 0.0
    return 1.0 if _clause_tokens(required) <= _clause_tokens(offered) else 0.0


def _clause_tokens(clause: ContractClause) -> frozenset[str]:
    return frozenset(_tokens(f"{clause.subject} {clause.value}"))


def _version_constraints(
    value: str,
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    return tuple(
        (operator, tuple(int(part) for part in version.split(".")))
        for operator, version in re.findall(r"(>=|<=|==|>|<)\s*v?(\d+(?:\.\d+)*)", value)
    )


def _ranges_overlap(
    first: tuple[tuple[str, tuple[int, ...]], ...],
    second: tuple[tuple[str, tuple[int, ...]], ...],
) -> bool:
    constraints = (*first, *second)
    exact = next((version for operator, version in constraints if operator == "=="), None)
    if exact is not None:
        return all(_version_satisfies(exact, item) for item in constraints)

    lower: tuple[int, ...] | None = None
    lower_inclusive = True
    upper: tuple[int, ...] | None = None
    upper_inclusive = True
    for operator, boundary in constraints:
        if operator in {">", ">="}:
            comparison = _compare_versions(boundary, lower) if lower is not None else 1
            if comparison > 0:
                lower = boundary
                lower_inclusive = operator == ">="
            elif comparison == 0:
                lower_inclusive = lower_inclusive and operator == ">="
        elif operator in {"<", "<="}:
            comparison = _compare_versions(boundary, upper) if upper is not None else -1
            if comparison < 0:
                upper = boundary
                upper_inclusive = operator == "<="
            elif comparison == 0:
                upper_inclusive = upper_inclusive and operator == "<="
    if lower is None or upper is None:
        return True
    comparison = _compare_versions(lower, upper)
    return comparison < 0 or (
        comparison == 0 and lower_inclusive and upper_inclusive
    )


def _compare_versions(first: tuple[int, ...], second: tuple[int, ...]) -> int:
    width = max(len(first), len(second))
    left = (*first, *((0,) * (width - len(first))))
    right = (*second, *((0,) * (width - len(second))))
    return (left > right) - (left < right)


def _version_satisfies(
    version: tuple[int, ...],
    constraint: tuple[str, tuple[int, ...]],
) -> bool:
    operator, boundary = constraint
    comparison = _compare_versions(version, boundary)
    return {
        ">=": comparison >= 0,
        ">": comparison > 0,
        "<=": comparison <= 0,
        "<": comparison < 0,
        "==": comparison == 0,
    }[operator]


def _equivalent_version_ranges(
    first: tuple[tuple[str, tuple[int, ...]], ...],
    second: tuple[tuple[str, tuple[int, ...]], ...],
) -> bool:
    def normalized(
        constraints: tuple[tuple[str, tuple[int, ...]], ...],
    ) -> tuple[tuple[str, tuple[int, ...]], ...]:
        values = []
        for operator, version in constraints:
            compact = tuple(version)
            while len(compact) > 1 and compact[-1] == 0:
                compact = compact[:-1]
            values.append((operator, compact))
        return tuple(sorted(values))

    return normalized(first) == normalized(second)
