"""Offline activation evaluation through the production retrieval stack."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from lancedb.index import FTS

from supermind_memory.compatibility import metadata_compatible, relationship_compatible
from supermind_memory.decision import ReuseDecisionEngine, eligibility_reasons
from supermind_memory.embeddings import EmbeddingProvider
from supermind_memory.redaction import redact_requirement, redact_text
from supermind_memory.repository import CapabilityRepository
from supermind_memory.schema import EMBEDDING_DIMENSION, TABLE_SCHEMAS
from supermind_memory.search import CapabilitySearch, _id_filter
from supermind_memory.types import ArtifactType, Capability, Evidence, Lifecycle, Relationship, RequirementProfile, SearchStatus


_METRICS = {"semantic_recall", "hybrid_recall", "hard_negative_accuracy", "filter_accuracy"}
_FILTERS = ("runtime", "platform", "license", "stack", "category_hint")


@dataclass(frozen=True)
class EvaluationReport:
    dataset: str
    version: int
    digest: str
    provider: str
    thresholds: Mapping[str, float]
    metrics: Mapping[str, float]
    passed: bool


def _provider_identity(embeddings):
    identity = getattr(embeddings, "model_id", None) or (
        f"{type(embeddings).__module__}.{type(embeddings).__qualname__}"
    )
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("retrieval evaluation provider identity must be nonempty text")
    return redact_text(identity)


def validate_evaluation_evidence(evaluation, embeddings, dataset_path: Path) -> None:
    """Check persisted activation proof against the configured evaluation contract."""
    contents = dataset_path.read_bytes()
    dataset = _validated_dataset(contents)
    if not isinstance(evaluation, dict) or set(evaluation) != {field.name for field in fields(EvaluationReport)}:
        raise ValueError("retrieval evaluation evidence is incomplete")
    if (
        evaluation["dataset"] != dataset["id"]
        or type(evaluation["version"]) is not int
        or evaluation["version"] != dataset["version"]
        or evaluation["digest"] != hashlib.sha256(contents).hexdigest()
        or evaluation["provider"] != _provider_identity(embeddings)
    ):
        raise ValueError("retrieval evaluation identity does not match configuration")
    for name in ("thresholds", "metrics"):
        values = evaluation[name]
        if not isinstance(values, dict) or set(values) != _METRICS or any(
            type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
            for value in values.values()
        ):
            raise ValueError("retrieval evaluation measurements are incomplete or invalid")
    if evaluation["thresholds"] != dataset["thresholds"]:
        raise ValueError("retrieval evaluation thresholds do not match configuration")
    passed = all(evaluation["metrics"][name] >= evaluation["thresholds"][name] for name in _METRICS)
    if evaluation["passed"] is not passed or not passed:
        raise ValueError("retrieval evaluation measurements did not pass the thresholds")


def evaluate_generation(
    repository: CapabilityRepository,
    embeddings: EmbeddingProvider,
    dataset_path: Path,
) -> EvaluationReport:
    contents = dataset_path.read_bytes()
    dataset = _validated_dataset(contents)
    provider = _provider_identity(embeddings)
    safe_embeddings = _SanitizedEmbeddings(embeddings)
    parent = _evaluation_directory(repository)
    with TemporaryDirectory(prefix="run-", dir=parent) as temporary:
        directory = Path(temporary)
        with CapabilityRepository.open(directory / "database") as evaluation:
            for name in TABLE_SCHEMAS:
                evaluation._database.create_table(name, schema=repository._table(name).schema)
            rows = []
            for index, item in enumerate(dataset["corpus"]):
                source = directory / f"source-{index}.txt"
                source.write_text("Offline capability evaluation fixture.\n", encoding="utf-8")
                rows.append(evaluation._capability_row(
                    _capability(item, source), [0.0] * EMBEDDING_DIMENSION,
                ))
            vectors = safe_embeddings.embed_documents([row["search_text"] for row in rows])
            for row, vector in zip(rows, vectors, strict=True):
                row["vector"] = vector
            evaluation._table("capabilities").add(rows)
            for item in dataset["evidence"]:
                evaluation.append_evidence(_fixture_evidence(item))
            for item in dataset["relationships"]:
                evaluation.append_relationship(_fixture_relationship(item))
            _copy_index_configuration(repository, evaluation)
            metrics = _evaluate_queries(
                evaluation, safe_embeddings, dataset["queries"], dataset["ranking_k"],
            )

            # Exercise actual candidate rows and existing indexes without a live
            # pointer or an index rebuild during the smoke query.
            capabilities = repository.list_capabilities()
            if capabilities:
                view = _SearchView(repository)
                requirement = redact_requirement(RequirementProfile(
                    "evaluation-smoke", "offline", capabilities[0].name,
                ))
                result = CapabilitySearch(view, safe_embeddings).search(
                    requirement, limit=len(capabilities),
                )
                if (
                    result.status is SearchStatus.COMPLETE and view.routes is None
                    and not any(metadata_compatible(item, requirement) for item in capabilities)
                ):
                    # An all-retired/degraded generation correctly has no public
                    # candidates. Still audit its physical indexes, without
                    # changing those rows or returning them as reusable matches.
                    view.hybrid_search(
                        query_text=requirement.intent,
                        vector=safe_embeddings.embed_query(requirement.intent),
                        where=_id_filter(tuple(item.id for item in capabilities)),
                        limit=len(capabilities), generation=view.active_generation(),
                    )
                _require_complete(result, view)
                if not view.routes[0] or not view.routes[2]:
                    raise ValueError("retrieval evaluation candidate smoke returned no rows")

    thresholds = {name: float(value) for name, value in dataset["thresholds"].items()}
    return EvaluationReport(
        dataset=dataset["id"], version=dataset["version"],
        digest=hashlib.sha256(contents).hexdigest(), provider=provider,
        thresholds=thresholds, metrics=metrics,
        passed=all(metrics[name] >= thresholds[name] for name in metrics),
    )


class _SearchView(CapabilityRepository):
    """Pin production search to one unpublished database without a live pointer."""

    def __init__(self, repository, seed_ids=()):
        self.repository = repository
        self.routes = None
        self.seed_ids = seed_ids

    def active_generation(self):
        return "generation-evaluation"

    def _generation_reader(self, generation):
        return self.repository

    def list_evidence(self, capability_id):
        return self.repository.list_evidence(capability_id)

    def list_relationships(self, capability_id):
        return self.repository.list_relationships(capability_id)

    def hybrid_search(self, *args, **kwargs):
        if self.seed_ids:
            # Isolate edge expansion with an explicit fixture predicate while
            # still executing production vector, FTS and hybrid retrieval.
            kwargs["where"] = f"({kwargs['where']}) AND ({_id_filter(self.seed_ids)})"
        self.routes = super().hybrid_search(*args, **kwargs)
        return self.routes


class _SanitizedEmbeddings:
    def __init__(self, provider):
        self.provider = provider

    def embed_documents(self, texts):
        vectors = self.provider.embed_documents([redact_text(text) for text in texts])
        if len(vectors) != len(texts):
            raise ValueError("retrieval evaluation embedding document count is invalid")
        return [self._validate(vector) for vector in vectors]

    def embed_query(self, text):
        return self._validate(self.provider.embed_query(redact_text(text)))

    @staticmethod
    def _validate(vector):
        if len(vector) != EMBEDDING_DIMENSION or any(
            not math.isfinite(float(value)) for value in vector
        ):
            raise ValueError("retrieval evaluation embedding must have 384 finite values")
        return [float(value) for value in vector]


def _evaluate_queries(repository, embeddings, queries, ranking_k):
    results = {name: [] for name in _METRICS}
    capabilities = {item.id: item for item in repository.list_capabilities()}
    engine = ReuseDecisionEngine()
    for query in queries:
        view = _SearchView(repository, tuple(query.get("relationship_seed_ids", ())))
        requirement = redact_requirement(RequirementProfile(
            query["id"], "offline", query["intent"], contract=query.get("contract", ""),
            **{name: tuple(query.get(name, ())) for name in _FILTERS},
        ))
        search = CapabilitySearch(view, embeddings)
        result = search.search(requirement, limit=len(capabilities))
        _require_complete(result, view)
        # Search still exhausts the production pool. Quality is scored against
        # the dataset's bounded cutoff, before the search's final reuse ranking.
        vector_ids = {row["id"] for row in view.routes[0][:ranking_k]}
        hybrid_ids = {row["id"] for row in view.routes[2][:ranking_k]}
        lexical_ids = {row["id"] for row in view.routes[1]}
        lexical_passed = set(query["lexical_expected_ids"]) <= lexical_ids
        accepted = {
            match.capability_id for match in result.matches
            if not eligibility_reasons(requirement, match, capabilities[match.capability_id])
        }
        matches = {match.capability_id: match for match in result.matches}
        decision = engine.decide(requirement, result)
        decision_passed = (
            decision.action == query["expected_action"]
            and decision.selected_capability_id == query["expected_selected_id"]
        )
        for expected in query["expected_ids"]:
            results["semantic_recall"].append(expected in vector_ids)
            results["hybrid_recall"].append(
                expected in hybrid_ids and expected in accepted
                and lexical_passed and decision_passed
            )
        for forbidden in query["forbidden_ids"]:
            # A forbidden candidate must also produce BUILD when it is the only
            # offered option. Include metadata-prefiltered rows in this policy
            # probe using the same production candidate assessment and snapshots.
            match = matches.get(forbidden) or search._candidate(
                capabilities[forbidden], requirement, 0.0, 0.0,
            )
            forbidden_decision = engine.decide(requirement, replace(
                result, matches=(match,), capability_snapshots=(capabilities[forbidden],),
            ))
            rejected = (
                forbidden not in accepted
                and decision.selected_capability_id != forbidden
                and forbidden_decision.action == "build"
                and forbidden_decision.selected_capability_id is None
            )
            if query.get("contract"):
                rejected = rejected and (
                    match.contract_fit == 0.0 and "contract mismatch" in match.rejection_reasons
                )
            results["hard_negative_accuracy"].append(rejected)
        if "relationship_seed_ids" in query:
            expected = set(query["relationship_expected_ids"])
            forbidden = set(query["relationship_forbidden_ids"])
            results["hybrid_recall"].append(expected <= accepted and decision_passed)
            results["hard_negative_accuracy"].append(not forbidden & set(matches))
        if any(name in query for name in _FILTERS):
            compatible = {
                item.id for item in repository.list_capabilities()
                if metadata_compatible(item, requirement)
            }
            results["filter_accuracy"].append(
                all({row["id"] for row in route} <= compatible for route in view.routes)
                and set(query["expected_ids"]) <= accepted
                and not set(query["forbidden_ids"]) & accepted
                and decision_passed
            )
    return {name: sum(values) / len(values) for name, values in results.items()}


def _require_complete(result, view):
    if result.status is not SearchStatus.COMPLETE or view.routes is None:
        raise ValueError(f"retrieval evaluation search unavailable: {result.error_message or 'routes not executed'}")


def _copy_index_configuration(candidate, evaluation):
    indexes = candidate._table("capabilities").list_indices()
    if len(indexes) != 1 or indexes[0].index_type != "FTS" or indexes[0].columns != ["search_text"]:
        raise ValueError("retrieval evaluation requires the candidate exact-vector and search_text FTS configuration")
    details = indexes[0].index_details
    aliases = {"ngram_min_length": "min_ngram_length", "ngram_max_length": "max_ngram_length"}
    config = FTS(**{
        field.name: details[aliases.get(field.name, field.name)]
        for field in fields(FTS) if aliases.get(field.name, field.name) in details
    })
    table = evaluation._table("capabilities")
    table.create_index("search_text", config=config, replace=True)
    if table.list_indices()[0].index_details != details:
        raise ValueError("retrieval evaluation index configuration differs from candidate")


def _evaluation_directory(repository):
    owner = repository._database_path.parent
    if owner.resolve() != owner or not owner.is_dir() or owner.stat().st_uid != os.getuid():
        raise ValueError("retrieval evaluation owner directory is unsafe")
    directory = owner / "evaluation"
    directory.mkdir(mode=0o700, exist_ok=True)
    metadata = directory.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode) or directory.resolve() != directory
        or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077
    ):
        raise ValueError("retrieval evaluation directory must be private and owned")
    return directory


def _validated_dataset(contents):
    def invalid(message):
        raise ValueError(f"retrieval evaluation dataset {message}")

    try:
        dataset = json.loads(contents)
    except (ValueError, UnicodeError):
        invalid("must be valid JSON")
    if not isinstance(dataset, dict) or dataset.get("id") != "retrieval-evaluation-v1" or (
        type(dataset.get("version")) is not int or dataset["version"] != 1
    ):
        invalid("metadata is invalid")
    thresholds = dataset.get("thresholds")
    if not isinstance(thresholds, dict) or set(thresholds) != _METRICS or any(
        type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
        for value in thresholds.values()
    ):
        invalid("thresholds must be complete finite numbers from zero to one")
    ids = {}
    for name in ("corpus", "queries", "relationships", "evidence"):
        items = dataset.get(name)
        if not isinstance(items, list) or not items:
            invalid(f"{name} must be a nonempty list")
        identifiers = [item.get("id") if isinstance(item, dict) else None for item in items]
        if any(not isinstance(item, str) or not item.strip() for item in identifiers):
            invalid(f"{name} IDs must be nonempty strings")
        if len(set(identifiers)) != len(identifiers):
            invalid(f"{name} IDs must be unique")
        ids[name] = set(identifiers)
    ranking_k = dataset.get("ranking_k")
    if type(ranking_k) is not int or not 0 < ranking_k < len(dataset["corpus"]):
        invalid("ranking_k must be positive and smaller than the corpus")
    for item in dataset["corpus"]:
        if any(not isinstance(item.get(name), str) or not item[name].strip()
               for name in ("name", "summary", "contract", "license")):
            invalid("capability text fields are required")
        for name in ("runtime", "platform", "stack", "category_path"):
            _string_list(item.get(name), invalid, name, nonempty=True)
    for item in dataset["evidence"]:
        if item.get("capability_id") not in ids["corpus"] or any(
            not isinstance(item.get(name), str) or not item[name].strip()
            for name in ("source_project", "outcome", "observed_at")
        ):
            invalid("relationship evidence is incomplete")
        try:
            datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
        except ValueError:
            invalid("relationship evidence timestamp is invalid")
    for item in dataset["relationships"]:
        if any(item.get(name) not in ids["corpus"] for name in ("source_id", "target_id")) or (
            item.get("relationship_type") not in {"dependency", "alternative", "composition", "consumer"}
        ):
            invalid("relationship endpoints or type are invalid")
        _string_list(item.get("evidence_ids"), invalid, "evidence_ids", nonempty=True)
        if not set(item["evidence_ids"]) <= ids["evidence"]:
            invalid("relationship references unknown evidence")
    for query in dataset["queries"]:
        if not isinstance(query.get("intent"), str) or not query["intent"].strip():
            invalid("query intent is required")
        if "contract" in query and (not isinstance(query["contract"], str) or not query["contract"].strip()):
            invalid("query contract must be nonempty text")
        for name in ("expected_ids", "forbidden_ids"):
            _string_list(query.get(name), invalid, name, nonempty=name == "expected_ids")
            if not set(query[name]) <= ids["corpus"]:
                invalid(f"{name} contains unknown capability IDs")
        if set(query["expected_ids"]) & set(query["forbidden_ids"]):
            invalid("expected and forbidden IDs must be disjoint")
        if len(query["expected_ids"]) > ranking_k:
            invalid("expected IDs exceed the declared ranking cutoff")
        _string_list(query.get("lexical_expected_ids"), invalid, "lexical_expected_ids", nonempty=False)
        if not set(query["lexical_expected_ids"]) <= set(query["expected_ids"]):
            invalid("lexical expected IDs must belong to expected IDs")
        if not isinstance(query.get("expected_action"), str) or query["expected_action"] not in {"reuse", "adapt"} or (
            query.get("expected_selected_id") not in query["expected_ids"]
        ):
            invalid("expected decision must declare a reuse/adapt action and expected selection")
        for name in _FILTERS:
            if name in query:
                _string_list(query[name], invalid, name, nonempty=True)
        if "relationship_seed_ids" in query:
            for name in ("relationship_seed_ids", "relationship_expected_ids", "relationship_forbidden_ids"):
                _string_list(query.get(name), invalid, name, nonempty=True)
                if not set(query[name]) <= ids["corpus"]:
                    invalid("relationship query references unknown capabilities")
            seed = set(query["relationship_seed_ids"])
            positive = set(query["relationship_expected_ids"])
            negative = set(query["relationship_forbidden_ids"])
            if seed & (positive | negative) or positive & negative or not set(query["expected_ids"]) <= seed:
                invalid("relationship fixture must distinguish seeds, positives and negatives")
    if not any(query.get("contract") and query["forbidden_ids"] for query in dataset["queries"]):
        invalid("must contain contract hard negatives")
    if not any(query["lexical_expected_ids"] for query in dataset["queries"]):
        invalid("must contain lexical positives")
    if not all(any(name in query and query["forbidden_ids"] for query in dataset["queries"]) for name in _FILTERS):
        invalid("must contain runtime, platform, license, stack and category filters with negatives")
    relationship_queries = [query for query in dataset["queries"] if "relationship_seed_ids" in query]
    if not relationship_queries:
        invalid("must contain positive and negative relationship queries")
    # Coverage must exercise both a current success and superseded/failed proof;
    # labels alone are insufficient. The production policy decides their result.
    outcomes = {item["outcome"] for item in dataset["evidence"]}
    if not {"passed", "failed"} <= outcomes:
        invalid("must contain successful and failed relationship evidence")
    evidence = tuple(_fixture_evidence(item) for item in dataset["evidence"])
    for query in relationship_queries:
        for target in query["relationship_expected_ids"] + query["relationship_forbidden_ids"]:
            edges = [
                _fixture_relationship(item)
                for item in dataset["relationships"]
                if item["source_id"] in query["relationship_seed_ids"] and item["target_id"] == target
            ]
            if not edges:
                invalid("relationship expectations must reference actual fixture edges")
            proven = any(relationship_compatible(
                edge, evidence, RequirementProfile("fixture-validation", "offline", "relationship proof"),
            ) for edge in edges)
            if proven != (target in query["relationship_expected_ids"]):
                invalid("relationship evidence must distinguish current success from failed or stale proof")
    return dataset


def _string_list(value, invalid, name, *, nonempty):
    if not isinstance(value, list) or (nonempty and not value) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ) or len(set(value)) != len(value):
        invalid(f"{name} must be a list of unique nonempty strings")


def _fixture_evidence(item):
    return Evidence(
        id=item["id"], capability_id=item["capability_id"],
        source_project=item["source_project"], evidence_type="relationship",
        outcome=item["outcome"], observed_at=item["observed_at"],
        metric_name=None, metric_value=None, confidence=1.0, supporting_uri=None,
    )


def _fixture_relationship(item):
    return Relationship(
        id=item["id"], source_id=item["source_id"], target_id=item["target_id"],
        relationship_type=item["relationship_type"], compatibility=(),
        evidence_ids=tuple(item["evidence_ids"]),
    )


def _capability(item, source):
    now = datetime.now(timezone.utc).isoformat()
    return Capability(
        abstraction_status="abstracted",  # Evaluation fixtures model already-extracted contracts.
        id=item["id"], name=item["name"], summary=item["summary"],
        category_path=tuple(item["category_path"]), facets=(), contract=item["contract"],
        constraints=(), artifact_type=ArtifactType.CODE, source_uri=source.as_uri(),
        source_revision="v1", content_hash="offline", owner="supermind", license=item["license"],
        stack=tuple(item["stack"]), runtime=tuple(item["runtime"]), platform=tuple(item["platform"]),
        dependencies=(), compatibility=(), lifecycle=Lifecycle.VERIFIED, confidence=1.0,
        expected_net_value=1.0, embedding_generation="generation-evaluation",
        created_at=now, updated_at=now, last_verified_at=now,
    )
