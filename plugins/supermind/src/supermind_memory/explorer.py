"""Deterministic, read-only Markdown and Mermaid views of capability memory."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import math
import re
from typing import Protocol

from supermind_memory.decision import ReuseDecisionEngine, eligibility_reasons
from supermind_memory.redaction import redact_requirement, redact_text
from supermind_memory.scoring import reuse_score
from supermind_memory.taxonomy import TOP_LEVEL_CATEGORIES
from supermind_memory.types import (
    Capability,
    CandidateMatch,
    Evidence,
    InspectFilter,
    Lifecycle,
    Relationship,
    RequirementProfile,
    ReuseScoreInputs,
    SearchResult,
    SearchStatus,
)


class ExplorerRepository(Protocol):
    """The deliberately narrow, read-only repository surface used by the explorer."""

    def get_capability(self, capability_id: str) -> Capability | None: ...

    def list_capabilities(self) -> tuple[Capability, ...]: ...

    def list_evidence(self, capability_id: str) -> tuple[Evidence, ...]: ...

    def list_relationships(self, capability_id: str) -> tuple[Relationship, ...]: ...


_LIFECYCLE_PRIORITY = {
    Lifecycle.RECOMMENDED: 0,
    Lifecycle.VERIFIED: 1,
    Lifecycle.CANDIDATE: 2,
    Lifecycle.OBSERVED: 3,
    Lifecycle.DEGRADED: 4,
    Lifecycle.RETIRED: 5,
}
_CATEGORY_PRIORITY = {category: index for index, category in enumerate(TOP_LEVEL_CATEGORIES)}

_RELATIONSHIP_STYLES = {
    "dependency": ("-->", "depends on"),
    "depends_on": ("-->", "depends on"),
    "depends-on": ("-->", "depends on"),
    "alternative": ("-.->", "alternative"),
    "alternative_to": ("-.->", "alternative"),
    "alternative-to": ("-.->", "alternative"),
    "composition": ("==>", "composes"),
    "composed_of": ("==>", "composes"),
    "composed-of": ("==>", "composes"),
    "replacement": ("-.->", "replaces"),
    "replaces": ("-.->", "replaces"),
    "consumer": ("-->", "consumed by"),
    "consumed_by": ("-->", "consumed by"),
    "consumed-by": ("-->", "consumed by"),
}


class CapabilityExplorer:
    """Render repository records without changing any capability-memory state."""

    def __init__(self, repository: ExplorerRepository) -> None:
        self._repository = repository

    def overview(self) -> str:
        """Summarize every taxonomy root with a stable maturity distribution."""
        capabilities = self._repository.list_capabilities()
        lines = [
            "# Capability overview",
            "",
            "| Category | Total | Observed | Candidate | Verified | Recommended | Degraded | Retired |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for category in TOP_LEVEL_CATEGORIES:
            members = [item for item in capabilities if item.category_path[:1] == (category,)]
            counts = {lifecycle: sum(item.lifecycle is lifecycle for item in members) for lifecycle in Lifecycle}
            lines.append(
                "| "
                f"{_markdown_cell(category)} | {len(members)} | {counts[Lifecycle.OBSERVED]} | "
                f"{counts[Lifecycle.CANDIDATE]} | {counts[Lifecycle.VERIFIED]} | "
                f"{counts[Lifecycle.RECOMMENDED]} | {counts[Lifecycle.DEGRADED]} | "
                f"{counts[Lifecycle.RETIRED]} |"
            )
        lines.append("")
        for category in TOP_LEVEL_CATEGORIES:
            total = sum(item.category_path[:1] == (category,) for item in capabilities)
            lines.extend((f"### {_markdown_text(category)}", f"[{('#' * total) or '-'}] {total}", ""))
        return "\n".join(lines)

    def table(self, filters: InspectFilter) -> str:
        """Render the selected catalog rows in deterministic maturity/value order."""
        capabilities = self._filtered_capabilities(filters)
        lines = [
            "# Capability catalog",
            "",
            "| ID | Name | Category | Lifecycle | Reuse score | Expected net value |",
            "| --- | --- | --- | --- | ---: | ---: |",
        ]
        for item, score in capabilities:
            lines.append(
                "| "
                f"{_markdown_cell(item.id)} | {_markdown_cell(item.name)} | "
                f"{_markdown_cell(' / '.join(item.category_path))} | {_markdown_cell(item.lifecycle.value)} | "
                f"{_number(score)} | {_number(item.expected_net_value)} |"
            )
        if not capabilities:
            lines.append("| — | No capabilities match this filter | — | — | — | — |")
        return "\n".join(lines) + "\n"

    def detail(self, capability_id: str) -> str:
        """Render a complete evidence-backed card for one capability."""
        capability = self._repository.get_capability(capability_id)
        if capability is None:
            return f"Capability not found: {_markdown_text(capability_id)}\n"
        evidence = tuple(sorted(self._repository.list_evidence(capability.id), key=lambda item: item.id))
        score = _capability_reuse_score(capability, evidence)
        lines = [
            f"# Capability: {_markdown_text(capability.id)}",
            "",
            "| Field | Value |",
            "| --- | --- |",
            _row("Name", capability.name),
            _row("Category", " / ".join(capability.category_path)),
            _row("Maturity", capability.lifecycle.value),
            _row("Reuse score", _number(score)),
            _row("Contract", capability.contract),
            _row("Constraints", _joined(capability.constraints)),
            _row("Facets", _joined(capability.facets)),
            _row("Source", capability.source_uri),
            _row("Source revision", capability.source_revision),
            _row("Content hash", capability.content_hash),
            _row("Artifact type", capability.artifact_type.value),
            _row("Owner", capability.owner),
            _row("License", capability.license),
            _row("Stack", _joined(capability.stack)),
            _row("Runtime", _joined(capability.runtime)),
            _row("Platform", _joined(capability.platform)),
            _row("Dependencies", _joined(capability.dependencies)),
            _row("Compatibility", _joined(capability.compatibility)),
            _row("Expected net value", _number(capability.expected_net_value)),
            _row("Confidence", _number(capability.confidence)),
            _row("Created at", capability.created_at),
            _row("Updated at", capability.updated_at),
            _row("Last verified at", capability.last_verified_at or "—"),
            "",
            "## Evidence",
            "",
            "| ID | Type | Outcome | Metric | Confidence | Observed at | Supporting source |",
            "| --- | --- | --- | --- | ---: | --- | --- |",
        ]
        if evidence:
            lines.extend(
                "| "
                f"{_markdown_cell(item.id)} | {_markdown_cell(item.evidence_type)} | "
                f"{_markdown_cell(item.outcome)} | {_markdown_cell(_metric(item))} | "
                f"{_number(item.confidence)} | {_markdown_cell(item.observed_at)} | "
                f"{_markdown_cell(item.supporting_uri or '—')} |"
                for item in evidence
            )
        else:
            lines.append("| — | No evidence | — | — | — | — | — |")
        lines.extend(
            (
                "",
                "## Economics",
                "",
                "| Evidence | Integration effort | Benefit | Failure risk |",
                "| --- | ---: | ---: | ---: |",
            )
        )
        if evidence:
            lines.extend(
                "| "
                f"{_markdown_cell(item.id)} | {_number(item.integration_effort)} | "
                f"{_number(item.benefit)} | {_number(item.failure_risk)} |"
                for item in evidence
            )
        else:
            lines.append("| — | 0 | 0 | 0 |")
        relationships = self._relationships_for((capability.id,))
        lines.extend(("", "## Relationships", "", "| Type | Source | Target | Compatibility |", "| --- | --- | --- | --- |"))
        if relationships:
            lines.extend(
                "| "
                f"{_markdown_cell(item.relationship_type)} | {_markdown_cell(item.source_id)} | "
                f"{_markdown_cell(item.target_id)} | {_markdown_cell(_joined(item.compatibility))} |"
                for item in relationships
            )
        else:
            lines.append("| — | No relationships | — | — |")
        return "\n".join(lines) + "\n"

    def decision(self, requirement: RequirementProfile, result: SearchResult) -> str:
        """Explain the complete search result without converting a failure into a no-match."""
        requirement = redact_requirement(requirement)
        lines = ["# Reuse decision", "", "## Requirement", "", _markdown_paragraph(requirement.intent), ""]
        if result.status is SearchStatus.FAILED:
            lines.extend(
                (
                    "## Search failed",
                    "",
                    _markdown_paragraph(result.error_code or "search_failed"),
                    "",
                    _markdown_paragraph(result.error_message or "No error message supplied."),
                    "",
                )
            )
            return "\n".join(lines)
        candidates = tuple(sorted(result.matches, key=lambda item: (-item.reuse_score, item.capability_id)))
        if not candidates:
            lines.extend(("## Selected action", "", "Build a new capability; no reusable candidate matched.", ""))
            return "\n".join(lines)
        lines.extend(
            (
                "## Candidates",
                "",
                "| Candidate | Category | Contract fit | Requirement fit | Reliability | Historical benefit | Integration cost | Maintenance risk | Reuse score | Expected net value | Source |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            )
        )
        snapshots = {item.id: item for item in result.capability_snapshots}
        assessed = tuple(
            (
                match,
                snapshots.get(match.capability_id)
                or (
                    self._repository.get_capability(match.capability_id)
                    if not result.capability_snapshots
                    else None
                ),
            )
            for match in candidates
        )
        for match, capability in assessed:
            expected_value = _number(capability.expected_net_value) if capability else "—"
            source = capability.source_uri if capability else "Unavailable"
            lines.append(
                "| "
                f"{_markdown_cell(match.capability_id)} | "
                f"{_markdown_cell(' / '.join(capability.category_path) if capability else 'Unavailable')} | "
                f"{_number(match.contract_fit)} | "
                f"{_number(match.requirement_fit)} | {_number(match.reliability)} | "
                f"{_number(match.historical_benefit)} | {_number(match.integration_cost)} | "
                f"{_number(match.maintenance_risk)} | {_number(match.reuse_score)} | "
                f"{_markdown_cell(expected_value)} | {_markdown_cell(source)} |"
            )
        rejections = tuple(
            (match.capability_id, reason)
            for match, capability in assessed
            for reason in eligibility_reasons(requirement, match, capability)
        )
        decision = ReuseDecisionEngine().decide(
            requirement,
            result,
            fallback_resolver=self._repository.get_capability,
        )
        lines.extend(("", "## Selected action", ""))
        if decision.selected_capability_id is not None:
            verb = "Adapt" if decision.action == "adapt" else "Reuse"
            lines.append(f"{verb} capability: {_markdown_text(decision.selected_capability_id)}")
        else:
            lines.append("Build a new capability; no suitable reusable candidate.")
        lines.extend(("", "## Rejections", ""))
        if rejections:
            lines.extend(f"- {_markdown_text(identifier)}: {_markdown_text(reason)}" for identifier, reason in rejections)
        else:
            lines.append("- None")
        lines.extend(("", "## Score components", "", "| Candidate | Vector | Lexical | Contract fit | Requirement fit |", "| --- | ---: | ---: | ---: | ---: |"))
        lines.extend(
            "| "
            f"{_markdown_cell(item.capability_id)} | {_number(item.vector_score)} | {_number(item.lexical_score)} | "
            f"{_number(item.contract_fit)} | {_number(item.requirement_fit)} |"
            for item in candidates
        )
        return "\n".join(lines) + "\n"

    def graph(self, filters: InspectFilter) -> str:
        """Render a safe, stable Mermaid graph with relationship expansion."""
        selected = {item.id: item for item, _ in self._filtered_capabilities(filters)}
        relationships = self._relationships_for(tuple(selected))
        node_ids = set(selected)
        node_ids.update(relationship.source_id for relationship in relationships)
        node_ids.update(relationship.target_id for relationship in relationships)
        capabilities = {item.id: item for item in self._repository.list_capabilities()}
        lines = ["```mermaid", "flowchart LR"]
        for identifier in sorted(node_ids):
            capability = capabilities.get(identifier)
            label = f"{capability.name} ({identifier})" if capability else identifier
            lines.append(f'{_mermaid_node_id(identifier)}["{_mermaid_label(label)}"]')
        for relationship in relationships:
            style = _RELATIONSHIP_STYLES.get(relationship.relationship_type.casefold())
            if style is None:
                continue
            edge, label = style
            lines.append(
                f"{_mermaid_node_id(relationship.source_id)} {edge}|{label}| "
                f"{_mermaid_node_id(relationship.target_id)}"
            )
        lines.append("```")
        return "\n".join(lines) + "\n"

    def _filtered_capabilities(self, filters: InspectFilter) -> tuple[tuple[Capability, float], ...]:
        selected = []
        category = tuple(value.casefold() for value in filters.category)
        wanted_stack = {value.casefold() for value in filters.stack}
        wanted_lifecycle = set(filters.lifecycle)
        for capability in self._repository.list_capabilities():
            if filters.capability_id is not None and capability.id != filters.capability_id:
                continue
            if category and tuple(value.casefold() for value in capability.category_path[: len(category)]) != category:
                continue
            if wanted_lifecycle and capability.lifecycle not in wanted_lifecycle:
                continue
            offered_stack = {value.casefold() for value in (*capability.stack, *capability.compatibility)}
            if wanted_stack and not wanted_stack.intersection(offered_stack):
                continue
            evidence = self._repository.list_evidence(capability.id)
            selected.append((capability, _capability_reuse_score(capability, evidence)))
        return tuple(
            sorted(
                selected,
                key=lambda item: (
                    _CATEGORY_PRIORITY[item[0].category_path[0]],
                    _LIFECYCLE_PRIORITY[item[0].lifecycle],
                    -item[1],
                    item[0].id,
                ),
            )
        )

    def _relationships_for(self, capability_ids: Iterable[str]) -> tuple[Relationship, ...]:
        seen: dict[str, Relationship] = {}
        for capability_id in sorted(set(capability_ids)):
            for relationship in self._repository.list_relationships(capability_id):
                seen.setdefault(relationship.id, relationship)
        return tuple(
            sorted(
                seen.values(),
                key=lambda item: (item.relationship_type.casefold(), item.source_id, item.target_id, item.id),
            )
        )


def _capability_reuse_score(capability: Capability, evidence: Iterable[Evidence]) -> float:
    """Use search's normalized public score inputs for deterministic catalog ordering."""
    records = tuple(evidence)
    return reuse_score(
        ReuseScoreInputs(
            contract_fit=0.5,
            requirement_fit=0.5,
            reliability=_bounded(capability.confidence),
            historical_benefit=_mean_bounded(item.benefit for item in records),
            integration_cost=_mean_bounded(item.integration_effort for item in records),
            maintenance_risk=_mean_bounded(item.failure_risk for item in records),
        )
    )


def _bounded(value: float) -> float:
    return min(1.0, max(0.0, value)) if math.isfinite(value) else 0.0


def _mean_bounded(values: Iterable[float]) -> float:
    records = tuple(values)
    return _bounded(sum(records) / len(records)) if records else 0.0


def _markdown_cell(value: object) -> str:
    return _markdown_text(value)


def _markdown_text(value: object) -> str:
    """Render untrusted values as inert text, not Markdown, HTML, or autolinks."""
    text = redact_text(str(value)).replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ⏎ ")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\\", "&#92;").replace("://", ":&#47;&#47;")
    text = text.replace("@", "@&#8203;").replace("www.", "www&#8203;.")
    for character in ("`", "[", "]", "!", "*", "|", "~"):
        text = text.replace(character, f"\\{character}")
    return re.sub(r"(?<!\w)_(?=\S)|(?<=\S)_(?!\w)", r"\\_", text)


def _markdown_paragraph(value: object) -> str:
    """Keep user text out of paragraph-leading Markdown constructs."""
    return "\u200b" + _markdown_text(value)


def _mermaid_node_id(identifier: str) -> str:
    return "cap_" + hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:20]


def _mermaid_label(value: object) -> str:
    text = str(value).replace("&", "&amp;")
    return (
        text.replace("`", "&#96;")
        .replace("\\", "&#92;")
        .replace('"', "&#34;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace("{", "&#123;")
        .replace("}", "&#125;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\r", "")
        .replace("\n", "&#10;")
    )


def _number(value: float) -> str:
    return f"{value:.2f}" if math.isfinite(value) else "—"


def _joined(values: Iterable[str]) -> str:
    return ", ".join(values) if values else "—"


def _metric(item: Evidence) -> str:
    if item.metric_name is None:
        return "—"
    return f"{item.metric_name}: {_number(item.metric_value)}" if item.metric_value is not None else item.metric_name


def _row(field: str, value: object) -> str:
    return f"| {_markdown_cell(field)} | {_markdown_cell(value)} |"
