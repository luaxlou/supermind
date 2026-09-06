from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest
from fastembed.common.model_management import ModelManagement
from huggingface_hub.errors import LocalEntryNotFoundError
from lancedb.index import FTS

from supermind_memory.decision import ReuseDecisionEngine
from supermind_memory.embeddings import FastEmbedProvider
from supermind_memory.repository import CapabilityRepository
from supermind_memory.retrieval_evaluation import evaluate_generation
from supermind_memory.schema import EMBEDDING_DIMENSION
from supermind_memory.search import CapabilitySearch
from supermind_memory.source_resolution import resolve_source, source_available
from supermind_memory.types import (
    ArtifactType,
    Capability,
    Evidence,
    Lifecycle,
    Relationship,
    RequirementProfile,
    SearchStatus,
)


def requirement(
    intent: str,
    *,
    contract: str = "",
    category_hint: tuple[str, ...] = (),
    stack: tuple[str, ...] = (),
    constraints: tuple[str, ...] = (),
    runtime: tuple[str, ...] = (),
    platform: tuple[str, ...] = (),
    license: tuple[str, ...] = (),
) -> RequirementProfile:
    return RequirementProfile(
        id="requirement-1",
        project_id="project-a",
        intent=intent,
        contract=contract,
        category_hint=category_hint,
        stack=stack,
        constraints=constraints,
        runtime=runtime,
        platform=platform,
        license=license,
    )


def test_embedding_receives_only_redacted_requirement_and_contract(search, embeddings, monkeypatch):
    queries = []
    lexical = []

    class RecordingEmbeddings:
        def embed_query(self, text):
            queries.append(text)
            return embeddings.embed_query(text)

    original_hybrid = search._repository.hybrid_search

    def hybrid(**kwargs):
        lexical.append(kwargs["query_text"])
        return original_hybrid(**kwargs)

    search._embeddings = RecordingEmbeddings()
    monkeypatch.setattr(search._repository, "hybrid_search", hybrid)
    wanted = requirement(
        'login {"password":"query-credential"}',
        contract='OAuth callback creates an authenticated session with password="contract-credential"',
    )
    result = search.search(wanted)
    assert result.status is SearchStatus.COMPLETE
    assert queries and lexical
    exposed = repr((queries, lexical, asdict(result)))
    assert "query-credential" not in exposed
    assert "contract-credential" not in exposed
    assert queries == lexical


def test_contract_parser_redacts_before_extracting_clause_values():
    from supermind_memory.compatibility import parse_contract

    clauses = parse_contract(('must use AES 256 with password="parser-credential"',))
    assert "parser-credential" not in repr(clauses)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("'Authorization': Bearer tiny", "tiny"),
        ('"Authorization": Basic dTpw', "dTpw"),
        ("'Authorization':\n  Bearer tiny", "tiny"),
        ('"Authorization":\n  Basic dTpw', "dTpw"),
        ("Authorization:\n  Bearer tiny", "tiny"),
        ("Authorization: Basic\r\n\tdTpw", "dTpw"),
        ("'Authorization': Basic\n  dTpw", "dTpw"),
        ('"Authorization": Bearer\n  tiny', "tiny"),
        ("password: |\n  block search credential\n  additional block material\nscope: public", "block search credential"),
        ("client_secret: >\n  folded search credential\n  additional folded material\nscope: public", "folded search credential"),
        ("password: &credential |\n  block search credential\n  additional block material\nscope: public", "block search credential"),
        ("password: &credential >\n  folded search credential\n  additional folded material\nscope: public", "folded search credential"),
        ("password: !!str |\n  block search credential\n  additional block material\nscope: public", "block search credential"),
        ("password: !!str >\n  folded search credential\n  additional folded material\nscope: public", "folded search credential"),
        ("password: &credential\n  !!str |\n    block search credential\n    additional block material\nscope: public", "block search credential"),
        ("password: &credential\n  !!str >\n    folded search credential\n    additional folded material\nscope: public", "folded search credential"),
        ("password: !!str\n  &credential |\n    block search credential\n    additional block material\nscope: public", "block search credential"),
        ("password: !!str\n  &credential >\n    folded search credential\n    additional folded material\nscope: public", "folded search credential"),
    ],
)
def test_structured_credentials_never_cross_real_search_embedding_boundary(search, embeddings, text, secret):
    captured = []

    class RecordingEmbeddings:
        def embed_query(self, value):
            captured.append(value)
            return embeddings.embed_query(value)

    search._embeddings = RecordingEmbeddings()
    result = search.search(requirement("login\n" + text))
    assert result.status is SearchStatus.COMPLETE
    assert captured
    assert secret not in repr(captured)
    assert "additional block material" not in repr(captured)
    assert "additional folded material" not in repr(captured)


def capability(
    capability_id: str,
    name: str,
    summary: str,
    *,
    contract: str,
    category_path: tuple[str, ...] = ("Code and components", "Identity and access"),
    stack: tuple[str, ...] = ("TypeScript",),
    compatibility: tuple[str, ...] = (),
    constraints: tuple[str, ...] = (),
    lifecycle: Lifecycle = Lifecycle.VERIFIED,
    confidence: float = 0.8,
    runtime: tuple[str, ...] = ("Node.js",),
    platform: tuple[str, ...] = (),
    license: str = "MIT",
    dependencies: tuple[str, ...] = (),
) -> Capability:
    return Capability(
        id=capability_id,
        name=name,
        summary=summary,
        category_path=category_path,
        facets=("authentication",),
        contract=contract,
        constraints=constraints,
        artifact_type=ArtifactType.CODE,
        source_uri=f"/capabilities/{capability_id}",
        source_revision="abc123",
        content_hash=f"hash-{capability_id}",
        owner="identity-team",
        license=license,
        stack=stack,
        runtime=runtime,
        platform=platform,
        dependencies=dependencies,
        compatibility=compatibility,
        lifecycle=lifecycle,
        confidence=confidence,
        expected_net_value=1.0,
        embedding_generation="generation-1",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
        last_verified_at="2026-09-04T00:00:00Z",
    )


def add(repo: CapabilityRepository, embeddings, item: Capability) -> None:
    document = " ".join(
        (item.name, item.summary, item.contract, *item.facets, *item.stack, *item.constraints)
    )
    repo.upsert_capability(item, embeddings.embed_documents([document])[0])


def vector(first: float, second: float = 0.0) -> list[float]:
    return [first, second, *([0.0] * (EMBEDDING_DIMENSION - 2))]


@pytest.fixture
def evaluation_repository(tmp_path):
    with CapabilityRepository.open(tmp_path / "candidate" / "database") as repository:
        repository.initialize()
        repository._table("capabilities").create_index("search_text", config=FTS(), replace=True)
        yield repository


def evaluation_dataset_path():
    return Path(__file__).resolve().parents[2] / "evaluation" / "retrieval-v1.json"


@pytest.mark.parametrize("fault", ("constant", "inverted", "empty-lexical", "reversed-hybrid"))
def test_retrieval_evaluation_rejects_nondiscriminative_or_incomplete_routes(
    evaluation_repository, embeddings, monkeypatch, fault,
):
    class FaultyEmbeddings:
        def embed_documents(self, texts):
            if fault == "constant":
                return [vector(1.0) for _ in texts]
            return embeddings.embed_documents(texts)

        def embed_query(self, text):
            if fault == "constant":
                return vector(1.0)
            return [-value for value in embeddings.embed_query(text)]

    provider = embeddings if fault in {"empty-lexical", "reversed-hybrid"} else FaultyEmbeddings()
    original_hybrid = CapabilityRepository.hybrid_search

    def hybrid(self, *args, **kwargs):
        routes = original_hybrid(self, *args, **kwargs)
        if fault == "reversed-hybrid":
            return routes[0], routes[1], list(reversed(routes[2]))
        return routes[0], [], routes[2]

    if fault in {"empty-lexical", "reversed-hybrid"}:
        monkeypatch.setattr(CapabilityRepository, "hybrid_search", hybrid)
    report = evaluate_generation(evaluation_repository, provider, evaluation_dataset_path())

    assert report.passed is False
    metric = "hybrid_recall" if fault in {"empty-lexical", "reversed-hybrid"} else "semantic_recall"
    assert report.metrics[metric] < 1.0


@pytest.mark.parametrize("fault", ("always-build", "ignore-eligibility", "wrong-adaptation-action"))
def test_retrieval_evaluation_rejects_incorrect_shared_decision_behavior(
    evaluation_repository, embeddings, monkeypatch, fault,
):
    original = ReuseDecisionEngine.decide
    calls = []

    def decide(self, requirement, result, **kwargs):
        calls.append(requirement.id)
        decision = original(self, requirement, result, **kwargs)
        if fault == "always-build":
            return replace(decision, action="build", selected_capability_id=None)
        if fault == "ignore-eligibility" and len(result.matches) == 1:
            return replace(decision, action="reuse", selected_capability_id=result.matches[0].capability_id)
        if fault == "wrong-adaptation-action" and decision.action == "adapt":
            return replace(decision, action="reuse")
        return decision

    monkeypatch.setattr(ReuseDecisionEngine, "decide", decide)
    report = evaluate_generation(evaluation_repository, embeddings, evaluation_dataset_path())

    assert calls
    assert report.passed is False
    metric = "hard_negative_accuracy" if fault == "ignore-eligibility" else "hybrid_recall"
    assert report.metrics[metric] < 1.0


def test_retrieval_evaluation_rejects_nonmatching_real_fts_configuration(
    evaluation_repository, embeddings,
):
    evaluation_repository._table("capabilities").create_index(
        "search_text", config=FTS(base_tokenizer="raw"), replace=True,
    )

    report = evaluate_generation(evaluation_repository, embeddings, evaluation_dataset_path())

    assert report.passed is False
    assert report.metrics["hybrid_recall"] < 1.0


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("thresholds",), None),
        (("thresholds", "semantic_recall"), None),
        (("thresholds", "semantic_recall"), float("nan")),
        (("thresholds", "hybrid_recall"), float("inf")),
        (("thresholds", "filter_accuracy"), True),
        (("thresholds", "filter_accuracy"), -0.1),
        (("thresholds", "filter_accuracy"), 1.1),
        (("version",), True),
        (("ranking_k",), None),
        (("ranking_k",), 0),
        (("ranking_k",), True),
        (("ranking_k",), 7),
        (("corpus",), []),
        (("queries",), []),
        (("corpus", 1, "id"), "eval-oauth-login"),
        (("queries", 1, "id"), "semantic-login"),
        (("queries", 0, "expected_ids"), []),
        (("queries", 0, "expected_ids"), ["unknown"]),
        (("queries", 0, "expected_ids"), ["eval-oauth-login", "eval-oauth-login"]),
        (("queries", 0, "forbidden_ids"), ["unknown"]),
        (("queries", 0, "forbidden_ids"), ["eval-oauth-login"]),
        (("queries", 0, "lexical_expected_ids"), None),
        (("queries", 0, "lexical_expected_ids"), ["eval-password-login"]),
        (("queries", 0, "expected_action"), None),
        (("queries", 0, "expected_action"), []),
        (("queries", 0, "expected_action"), "build"),
        (("queries", 0, "expected_selected_id"), "eval-password-login"),
        (("queries", 0, "runtime"), []),
        (("corpus", 0, "platform"), []),
    ],
)
def test_retrieval_evaluation_rejects_invalid_dataset_before_embedding_or_writes(
    tmp_path, evaluation_repository, path, replacement,
):
    dataset = json.loads(evaluation_dataset_path().read_text())
    target = dataset
    for part in path[:-1]:
        target = target[part]
    if replacement is None:
        target.pop(path[-1])
    else:
        target[path[-1]] = replacement
    invalid_path = tmp_path / "invalid-evaluation.json"
    invalid_path.write_text(json.dumps(dataset), encoding="utf-8")

    class NoEmbedding:
        def embed_documents(self, texts):
            pytest.fail("invalid dataset reached document embeddings")

        def embed_query(self, text):
            pytest.fail("invalid dataset reached query embeddings")

    with pytest.raises(ValueError, match="retrieval evaluation dataset"):
        evaluate_generation(evaluation_repository, NoEmbedding(), invalid_path)
    assert not (evaluation_repository._database_path.parent / "evaluation").exists()


@pytest.mark.parametrize("model_id", (None, "local-test-model-v1"))
def test_retrieval_evaluation_uses_production_search_sanitizes_embeddings_and_records_identity(
    tmp_path, evaluation_repository, embeddings, monkeypatch, model_id,
):
    secret = "Gh7j9K2s5N8v1M4q6R0t3W9y"
    dataset = json.loads(evaluation_dataset_path().read_text())
    dataset["corpus"][0]["summary"] += f" password={secret}"
    dataset["queries"][0]["intent"] += f" password={secret}"
    dataset_path = tmp_path / "credential-evaluation.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    documents, queries, searches, routes, decisions = [], [], [], [], []

    class RecordingProvider:
        api_key = secret

        def __repr__(self):
            pytest.fail("provider credentials must not be serialized")

        def embed_documents(self, texts):
            documents.extend(texts)
            return embeddings.embed_documents(texts)

        def embed_query(self, text):
            queries.append(text)
            return embeddings.embed_query(text)

    provider = RecordingProvider()
    provider.model_id = model_id
    original_search = CapabilitySearch.search
    original_hybrid = CapabilityRepository.hybrid_search
    original_decide = ReuseDecisionEngine.decide

    def search(self, requirement, *args, **kwargs):
        searches.append(requirement.id)
        return original_search(self, requirement, *args, **kwargs)

    def hybrid(self, *args, **kwargs):
        result = original_hybrid(self, *args, **kwargs)
        routes.append((kwargs["query_text"], result))
        return result

    def decide(self, requirement, result, **kwargs):
        decision = original_decide(self, requirement, result, **kwargs)
        decisions.append((requirement.id, len(result.matches), decision.action, decision.selected_capability_id))
        return decision

    monkeypatch.setattr(CapabilitySearch, "search", search)
    monkeypatch.setattr(CapabilityRepository, "hybrid_search", hybrid)
    monkeypatch.setattr(ReuseDecisionEngine, "decide", decide)
    report = evaluate_generation(evaluation_repository, provider, dataset_path)

    assert report.passed is True
    assert report.digest == hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    assert report.version == 1
    assert report.provider == (model_id or f"{RecordingProvider.__module__}.{RecordingProvider.__qualname__}")
    assert secret not in repr(report)
    assert len(documents) == len(dataset["corpus"])
    assert len(queries) == len(searches) == len(routes) == len(dataset["queries"])
    assert searches == [query["id"] for query in dataset["queries"]]
    for query in dataset["queries"]:
        main_decisions = [item for item in decisions if item[0] == query["id"] and item[1] > 1]
        assert [item[2:] for item in main_decisions] == [(query["expected_action"], query["expected_selected_id"])]
        negative_decisions = [item for item in decisions if item[0] == query["id"] and item[1] == 1]
        assert [item[2:] for item in negative_decisions] == [("build", None)] * len(query["forbidden_ids"])
    assert all(secret not in text and "cmprobe" not in text for text in documents + queries)
    assert "password=[REDACTED]" in documents[0]
    assert "password=[REDACTED]" in queries[0]
    assert all(all(route is not None for route in result) for _, result in routes)
    assert not list((evaluation_repository._database_path.parent / "evaluation").iterdir())
    assert evaluation_repository.list_capabilities() == ()


def test_retrieval_evaluation_copies_candidate_schema_and_nondefault_index_configuration(
    evaluation_repository, embeddings, monkeypatch,
):
    import pyarrow as pa
    import supermind_memory.retrieval_evaluation as evaluation_module

    candidate = evaluation_repository
    schema = candidate._table("metadata").schema.append(pa.field("candidate_extension", pa.string()))
    candidate._database.drop_table("metadata")
    candidate._database.create_table("metadata", schema=schema)
    candidate._table("capabilities").create_index(
        "search_text", config=FTS(stem=False, remove_stop_words=False, with_position=True), replace=True,
    )
    expected_index = candidate._table("capabilities").list_indices()[0]
    observed = []
    original = evaluation_module._evaluate_queries

    def evaluate(repository, provider, queries, ranking_k):
        assert repository._table("metadata").schema.equals(schema)
        assert repository._table("capabilities").list_indices()[0].index_details == expected_index.index_details
        observed.append(repository._database_path)
        return original(repository, provider, queries, ranking_k)

    monkeypatch.setattr(evaluation_module, "_evaluate_queries", evaluate)
    report = evaluate_generation(candidate, embeddings, evaluation_dataset_path())

    assert report.passed is True
    assert len(observed) == 1
    assert not observed[0].exists()
    assert candidate._table("capabilities").list_indices()[0].index_uuid == expected_index.index_uuid


def test_retrieval_evaluation_rejects_symlinked_temporary_parent_without_external_writes(
    tmp_path, evaluation_repository, embeddings,
):
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "marker"
    marker.write_text("untouched", encoding="utf-8")
    (evaluation_repository._database_path.parent / "evaluation").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="private and owned"):
        evaluate_generation(evaluation_repository, embeddings, evaluation_dataset_path())

    assert list(external.iterdir()) == [marker]
    assert marker.read_text() == "untouched"


@pytest.mark.parametrize("fault", ("documents", "query", "indexes"))
def test_retrieval_evaluation_closes_and_removes_temporary_store_on_failure(
    evaluation_repository, embeddings, monkeypatch, fault,
):
    from supermind_memory.repository import repository_runtime_state

    before = repository_runtime_state()[0]

    def fail(*args, **kwargs):
        raise RuntimeError("evaluation fixture failure")

    if fault in {"documents", "query"}:
        monkeypatch.setattr(embeddings, f"embed_{fault}", fail)
    else:
        table = evaluation_repository._table("capabilities")
        monkeypatch.setattr(table, "list_indices", fail)

    with pytest.raises((ValueError, RuntimeError), match="evaluation fixture failure"):
        evaluate_generation(evaluation_repository, embeddings, evaluation_dataset_path())

    assert repository_runtime_state()[0] == before
    assert not list((evaluation_repository._database_path.parent / "evaluation").iterdir())


@pytest.fixture
def search(tmp_path, embeddings) -> CapabilitySearch:
    repo = CapabilityRepository.open(tmp_path / "search.lance")
    repo.initialize()
    add(
        repo,
        embeddings,
        capability(
            "auth.oauth-login",
            "OAuth login",
            "Email sign in with an OAuth provider",
            contract="OAuth callback creates an authenticated session",
        ),
    )
    add(
        repo,
        embeddings,
        capability(
            "auth.jwt-login",
            "JWT login",
            "Issue a signed token after authentication",
            contract="Returns a JWT bearer token",
            constraints=("JWT",),
        ),
    )
    add(
        repo,
        embeddings,
        capability(
            "auth.session-login",
            "Session authentication",
            "Login sign in authentication with a server session cookie",
            contract="Returns an opaque session cookie",
        ),
    )
    add(
        repo,
        embeddings,
        replace(
            capability(
                "auth.degraded-oauth",
                "OAuth login legacy",
                "Email OAuth authentication login",
                contract="OAuth callback",
                lifecycle=Lifecycle.DEGRADED,
                confidence=1.0,
            ),
            summary="OAuth OAuth OAuth login authentication",
        ),
    )
    return CapabilitySearch(repo, embeddings)


def test_chinese_login_intent_finds_english_oauth_capability(search):
    result = search.search(requirement("为用户提供邮箱和 OAuth 登录", stack=("TypeScript",)))

    assert result.status is SearchStatus.COMPLETE
    assert result.matches[0].capability_id == "auth.oauth-login"


def test_pure_chinese_intent_finds_english_capability_only_through_query_vector(
    tmp_path, embeddings, monkeypatch
):
    repo = CapabilityRepository.open(tmp_path / "cross-language.lance")
    repo.initialize()
    add(
        repo,
        embeddings,
        capability(
            "visualization.chart",
            "Chart renderer",
            "Render analytical charts",
            contract="Produces an SVG chart",
            stack=(),
        ),
    )
    add(
        repo,
        embeddings,
        capability(
            "auth.federated-identity",
            "Federated identity",
            "Third-party authentication service",
            contract="Returns an authenticated principal",
            stack=(),
        ),
    )

    result = CapabilitySearch(repo, embeddings).search(
        requirement("为企业用户接入第三方身份认证")
    )

    assert result.status is SearchStatus.COMPLETE
    assert result.matches[0].capability_id == "auth.federated-identity"
    assert result.matches[0].vector_score > 0.0
    assert result.matches[0].lexical_score == 0.0

    monkeypatch.setattr(
        embeddings,
        "embed_query",
        lambda text: [0.0] * EMBEDDING_DIMENSION,
    )
    zeroed = CapabilitySearch(repo, embeddings).search(
        requirement("为企业用户接入第三方身份认证")
    )
    assert zeroed.matches[0].capability_id != "auth.federated-identity"


def test_exact_jwt_constraint_beats_semantically_close_session_module(search):
    result = search.search(requirement("login", constraints=("JWT",)))

    assert result.status is SearchStatus.COMPLETE
    assert result.matches[0].capability_id == "auth.jwt-login"
    assert result.matches[0].lexical_score > 0.0


def test_incompatible_and_unhealthy_candidates_are_filtered_before_scoring(tmp_path, embeddings):
    repo = CapabilityRepository.open(tmp_path / "filtered.lance")
    repo.initialize()
    add(
        repo,
        embeddings,
        capability(
            "auth.python-login",
            "OAuth login",
            "OAuth authentication for Python",
            contract="OAuth callback",
            stack=("Python",),
            confidence=1.0,
        ),
    )
    add(
        repo,
        embeddings,
        capability(
            "auth.typescript-login",
            "OAuth login",
            "OAuth authentication for TypeScript",
            contract="OAuth callback",
            confidence=0.6,
        ),
    )
    add(
        repo,
        embeddings,
        capability(
            "auth.retired-login",
            "OAuth login",
            "OAuth authentication for TypeScript",
            contract="OAuth callback",
            lifecycle=Lifecycle.RETIRED,
            confidence=1.0,
        ),
    )

    result = CapabilitySearch(repo, embeddings).search(
        requirement("OAuth login", stack=("TypeScript",))
    )

    assert [match.capability_id for match in result.matches] == ["auth.typescript-login"]


def test_required_stack_rejects_candidate_with_no_declared_stack_or_compatibility(
    tmp_path,
    embeddings,
):
    repo = CapabilityRepository.open(tmp_path / "missing-stack.lance")
    repo.initialize()
    add(
        repo,
        embeddings,
        capability(
            "auth.unknown-stack",
            "OAuth login",
            "Authentication with no declared implementation stack",
            contract="OAuth callback",
            stack=(),
        ),
    )

    result = CapabilitySearch(repo, embeddings).search(
        requirement("OAuth login", stack=("TypeScript",))
    )

    assert result.status is SearchStatus.COMPLETE
    assert result.matches == ()


@pytest.mark.parametrize(
    ("capability_stack", "capability_compatibility", "capability_category", "wanted_stack", "wanted_category"),
    [
        (("Μ",), (), ("Code and components", "Identity and access"), ("μ",), ()),
        (
            ("Python",),
            ("Straße",),
            ("Code and components", "Identity and access"),
            ("STRASSE",),
            (),
        ),
        (
            (),
            (),
            ("Code and components", "Straße", "Identity and access"),
            (),
            ("CODE AND COMPONENTS", "STRASSE"),
        ),
    ],
    ids=("greek-stack", "sharp-s-compatibility", "unicode-category-prefix"),
)
def test_unicode_metadata_casefolds_before_all_retrieval_routes(
    tmp_path,
    embeddings,
    capability_stack,
    capability_compatibility,
    capability_category,
    wanted_stack,
    wanted_category,
):
    repo = CapabilityRepository.open(tmp_path / "unicode-metadata.lance")
    repo.initialize()
    item = capability(
        "auth.unicode-login",
        "OAuth login",
        "Authentication adapter",
        contract="OAuth callback",
        category_path=capability_category,
        stack=capability_stack,
        compatibility=capability_compatibility,
    )
    add(repo, embeddings, item)

    result = CapabilitySearch(repo, embeddings).search(
        requirement(
            "OAuth login",
            stack=wanted_stack,
            category_hint=wanted_category,
        )
    )

    assert result.status is SearchStatus.COMPLETE
    assert [match.capability_id for match in result.matches] == [item.id]


def test_capability_enumeration_failure_fails_closed(embeddings):
    class BrokenEnumerationRepository:
        def list_capabilities(self):
            raise OSError("capability enumeration unavailable")

        def hybrid_search(self, *args, **kwargs):
            return (), (), ()

    result = CapabilitySearch(BrokenEnumerationRepository(), embeddings).search(
        requirement("OAuth login")
    )

    assert result.status is SearchStatus.FAILED
    assert result.is_no_match is False
    assert "enumeration unavailable" in result.error_message


def test_successful_empty_compatible_enumeration_is_complete_no_match(embeddings):
    class EmptyEnumerationRepository:
        def list_capabilities(self):
            return ()

        def hybrid_search(self, *args, **kwargs):
            raise AssertionError("retrieval must not run without compatible IDs")

    result = CapabilitySearch(EmptyEnumerationRepository(), embeddings).search(
        requirement("OAuth login")
    )

    assert result.status is SearchStatus.COMPLETE
    assert result.is_no_match is True


@pytest.mark.parametrize("incompatibility", ["category", "stack", "compatibility"])
def test_exact_metadata_prefilter_prevents_incompatible_candidates_from_crowding_all_routes(
    tmp_path, embeddings, incompatibility
):
    class RecordingRepository(CapabilityRepository):
        routes: tuple[list[dict[str, Any]], ...] | None = None

        def hybrid_search(self, *args, **kwargs):
            self.routes = super().hybrid_search(*args, **kwargs)
            return self.routes

    repo = RecordingRepository.open(tmp_path / f"crowd-{incompatibility}.lance")
    repo.initialize()
    for index in range(25):
        wrong_category = (
            "Code and components",
            "Legacy",
            "Identity and access",
        )
        wrong_stack = (
            ("NotTypeScript",)
            if incompatibility == "stack"
            else (("Python",) if incompatibility == "compatibility" else ("TypeScript",))
        )
        item = capability(
            f"crowder-{index:02d}",
            "OAuth login OAuth login",
            "OAuth login authentication OAuth login",
            contract="OAuth login callback",
            category_path=(
                wrong_category
                if incompatibility == "category"
                else ("Code and components", "Identity and access")
            ),
            stack=wrong_stack,
            compatibility=("NotTypeScript",) if incompatibility == "compatibility" else (),
            confidence=1.0,
        )
        repo.upsert_capability(item, vector(1.0))
    legitimate = capability(
        "auth.compatible-login",
        "OAuth adapter",
        "Identity integration",
        contract="OAuth callback",
        stack=("Python",) if incompatibility == "compatibility" else ("TypeScript",),
        compatibility=("TypeScript",) if incompatibility == "compatibility" else (),
        confidence=0.5,
    )
    repo.upsert_capability(legitimate, vector(0.0, 1.0))

    result = CapabilitySearch(repo, embeddings).search(
        requirement(
            "OAuth login",
            category_hint=("Code and components", "Identity and access"),
            stack=("TypeScript",),
        ),
        limit=1,
    )

    assert result.status is SearchStatus.COMPLETE
    assert result.matches[0].capability_id == legitimate.id
    assert repo.routes is not None
    for route in repo.routes:
        assert [row["id"] for row in route] == [legitimate.id]


def test_search_error_is_not_reported_as_no_match(embeddings):
    class BrokenRepository:
        def list_capabilities(self):
            raise OSError("index unavailable")

        def hybrid_search(self, *args, **kwargs):
            raise OSError("index unavailable")

    result = CapabilitySearch(BrokenRepository(), embeddings).search(requirement("login"))

    assert result.status is SearchStatus.FAILED
    assert result.is_no_match is False
    assert result.error_code == "search_failed"


def test_search_failure_redacts_secret_from_exposed_diagnostics(embeddings):
    secret = "xoxb-" + "123456789012-123456789012-abcdefghijklmnopqrstuvwx"

    class BrokenRepository:
        def list_capabilities(self):
            raise OSError(f"credential rejected: {secret}")

    result = CapabilitySearch(BrokenRepository(), embeddings).search(requirement("login"))

    assert result.status is SearchStatus.FAILED
    assert secret not in result.error_message
    assert "[REDACTED]" in result.error_message


@pytest.mark.parametrize("failed_route", ["vector", "fts", "hybrid"])
def test_failure_in_each_retrieval_route_returns_failed(
    tmp_path, embeddings, failed_route
):
    repo = CapabilityRepository.open(tmp_path / f"broken-{failed_route}.lance")
    repo.initialize()
    add(
        repo,
        embeddings,
        capability(
            "auth.login",
            "Login",
            "Authentication",
            contract="Authenticated principal",
            stack=(),
        ),
    )
    real_table = repo._table("capabilities")

    class FaultingTable:
        def __getattr__(self, name):
            return getattr(real_table, name)

        def create_index(self, *args, **kwargs):
            return real_table.create_index(*args, **kwargs)

        def search(self, *args, **kwargs):
            if kwargs.get("query_type") == failed_route:
                raise OSError(f"{failed_route} route unavailable")
            return real_table.search(*args, **kwargs)

    repo._table = lambda name: FaultingTable() if name == "capabilities" else real_table

    result = CapabilitySearch(repo, embeddings).search(requirement("login"))

    assert result.status is SearchStatus.FAILED
    assert result.is_no_match is False
    assert failed_route in result.error_message
    del repo._table
    del real_table
    repo.close()


def test_candidate_match_exposes_every_weighted_reuse_score_component(embeddings):
    item = capability(
        "auth.jwt-login",
        "JWT login",
        "Issue a bearer token",
        contract="JWT bearer token",
        stack=(),
        confidence=0.8,
    )
    item = replace(item, source_uri=Path(__file__).as_uri())
    evidence = Evidence(
        id="reuse-1",
        capability_id=item.id,
        source_project="project-b",
        evidence_type="reuse",
        outcome="success",
        metric_name=None,
        metric_value=None,
        confidence=1.0,
        observed_at="2026-09-04T00:00:00Z",
        supporting_uri=None,
        integration_effort=0.2,
        benefit=0.6,
        failure_risk=0.1,
    )

    class FixedRankRepository:
        def list_capabilities(self):
            return (item,)

        def hybrid_search(self, *args, **kwargs):
            return (
                [{"id": item.id}, {"id": "other"}],
                [{"id": "other"}, {"id": item.id}],
                [{"id": item.id}],
            )

        def get_capability(self, capability_id):
            return item if capability_id == item.id else None

        def list_evidence(self, capability_id):
            return (evidence,)

    result = CapabilitySearch(FixedRankRepository(), embeddings).search(
        requirement("token login", contract="JWT bearer", constraints=("JWT",))
    )

    match = result.matches[0]
    assert match.vector_score == pytest.approx(1.0)
    assert match.lexical_score == pytest.approx(0.5)
    assert match.contract_fit == pytest.approx(1.0)
    assert match.requirement_fit == pytest.approx(0.75)
    assert match.reliability == pytest.approx(0.8)
    assert match.historical_benefit == pytest.approx(0.6)
    assert match.integration_cost == pytest.approx(0.2)
    assert match.maintenance_risk == pytest.approx(0.1)
    assert match.reuse_score == pytest.approx(0.735)
    assert match.rejection_reasons == ()


def test_affirmative_current_source_evidence_makes_remote_capability_eligible(embeddings):
    item = replace(
        capability(
            "auth.remote-oauth",
            "Remote OAuth",
            "Hosted OAuth implementation",
            contract="OAuth callback creates an authenticated session",
        ),
        source_uri="https://capabilities.example.invalid/oauth",
    )
    source_evidence = Evidence(
        id="remote-source-current",
        capability_id=item.id,
        source_project="project-a",
        evidence_type="source_availability",
        outcome="available",
        metric_name=None,
        metric_value=None,
        confidence=1.0,
        observed_at="2026-09-04T00:00:00Z",
        supporting_uri=None,
    )

    class RemoteRepository:
        def list_capabilities(self):
            return (item,)

        def hybrid_search(self, *args, **kwargs):
            rows = ([{"id": item.id}], [{"id": item.id}], [{"id": item.id}])
            return rows

        def list_evidence(self, capability_id):
            return (source_evidence,)

    result = CapabilitySearch(RemoteRepository(), embeddings).search(
        requirement("OAuth login", contract=item.contract)
    )

    assert result.status is SearchStatus.COMPLETE
    assert result.matches[0].source_available is True
    assert ReuseDecisionEngine().decide(requirement("OAuth login", contract=item.contract), result).action == "reuse"


@pytest.mark.parametrize("as_uri", [False, True])
def test_safe_absolute_path_and_file_uri_share_availability(
    tmp_path,
    embeddings,
    as_uri,
):
    repository = CapabilityRepository.open(tmp_path / f"source-{as_uri}.lance")
    repository.initialize()
    source = tmp_path / "capability.py"
    source.write_text("def login(): pass\n", encoding="utf-8")
    reference = source.as_uri() if as_uri else str(source)
    stored = replace(
        capability(
            "auth.source-equivalence",
            "OAuth login",
            "Reusable OAuth authentication",
            contract="OAuth callback creates an authenticated session",
        ),
        source_uri=reference,
    )
    add(repository, embeddings, stored)

    result = CapabilitySearch(repository, embeddings).search(
        requirement("login", contract=stored.contract)
    )

    assert result.matches[0].source_available is True


@pytest.mark.parametrize(
    "reference",
    (
        "file://[malformed",
        "relative/capability.py",
        "https://capabilities.example.invalid/without-evidence",
    ),
    ids=("malformed-uri", "relative-path", "remote-without-evidence"),
)
def test_unsafe_or_unproven_sources_fail_closed_without_failing_search(
    tmp_path,
    embeddings,
    reference,
):
    repository = CapabilityRepository.open(tmp_path / "unsafe-source.lance")
    repository.initialize()
    stored = replace(
        capability(
            "auth.unsafe-source",
            "OAuth login",
            "Reusable OAuth authentication",
            contract="OAuth callback creates an authenticated session",
        ),
        source_uri=reference,
    )
    add(repository, embeddings, stored)

    result = CapabilitySearch(repository, embeddings).search(
        requirement("login", contract=stored.contract)
    )

    assert result.status is SearchStatus.COMPLETE
    assert result.matches[0].source_available is False
    assert result.matches[0].rejection_reasons == ("source unavailable",)


def test_local_source_crossing_a_symlink_boundary_is_unavailable(
    tmp_path,
    embeddings,
):
    repository = CapabilityRepository.open(tmp_path / "symlink-source.lance")
    repository.initialize()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "capability.py").write_text("def login(): pass\n", encoding="utf-8")
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (trusted / "boundary").symlink_to(outside, target_is_directory=True)
    stored = replace(
        capability(
            "auth.symlink-source",
            "OAuth login",
            "Reusable OAuth authentication",
            contract="OAuth callback creates an authenticated session",
        ),
        source_uri=(trusted / "boundary" / "capability.py").as_uri(),
    )
    add(repository, embeddings, stored)

    result = CapabilitySearch(repository, embeddings).search(
        requirement("login", contract=stored.contract)
    )

    assert result.status is SearchStatus.COMPLETE
    assert result.matches[0].source_available is False
    assert result.matches[0].rejection_reasons == ("source unavailable",)


@pytest.mark.parametrize(
    "reference_kind",
    ("absolute", "file-uri", "encoded-file-uri"),
)
def test_local_source_rejects_parent_segment_immediately_after_symlink(
    tmp_path,
    reference_kind,
):
    trusted = tmp_path / "trusted-parent"
    trusted.mkdir()
    (trusted / "capability.py").write_text("trusted = True\n", encoding="utf-8")
    outside = tmp_path / "outside-parent"
    outside.mkdir()
    (trusted / "boundary").symlink_to(outside, target_is_directory=True)
    unsafe = trusted / "boundary" / ".." / "capability.py"
    references = {
        "absolute": str(unsafe),
        "file-uri": unsafe.as_uri(),
        "encoded-file-uri": unsafe.as_uri().replace("/../", "/%2e%2e/"),
    }
    item = replace(
        capability(
            "auth.parent-segment-source",
            "OAuth login",
            "Reusable OAuth authentication",
            contract="OAuth callback creates an authenticated session",
        ),
        source_uri=references[reference_kind],
    )

    assert source_available(item, ()) is False


def test_final_source_component_type_swap_is_nonblocking_and_unavailable(tmp_path):
    source = tmp_path / "swapped-source.py"
    source.write_text("available = True\n", encoding="utf-8")
    plugin_root = Path(__file__).parents[2]
    script = f'''\
import os
from supermind_memory import source_resolution
from supermind_memory.types import ArtifactType, Capability, Lifecycle

source = {str(source)!r}
real_open = os.open
swapped = False

def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
    global swapped
    if path == "swapped-source.py" and dir_fd is not None and not swapped:
        swapped = True
        os.unlink(path, dir_fd=dir_fd)
        os.mkfifo(path, dir_fd=dir_fd)
    return real_open(path, flags, mode, dir_fd=dir_fd)

source_resolution.os.open = swapping_open
item = Capability(
    id="swapped-source", name="Swapped source", summary="", category_path=("Code and components",), facets=(), contract="", constraints=(),
    artifact_type=ArtifactType.CODE, source_uri=source, source_revision="", content_hash="", owner="", license="", stack=(), runtime=(),
    platform=(), dependencies=(), compatibility=(), lifecycle=Lifecycle.VERIFIED, confidence=1.0, expected_net_value=1.0,
    embedding_generation="", created_at="", updated_at="", last_verified_at="2026-09-06T00:00:00Z",
)
print(source_resolution.source_available(item, ()))
'''
    environment = {**os.environ, "PYTHONPATH": str(plugin_root / "src")}

    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert completed.stdout.strip() == "False"


@pytest.mark.parametrize(
    "reference",
    (
        "file:///tmp/capability%ZZ.py",
        "https://example.invalid:not-a-port/capability",
        "file:///tmp/capability.py?download=1",
        "file:///tmp/capability.py#fragment",
        "file:///tmp/capability%00.py",
        "file://example.invalid/tmp/capability.py",
    ),
    ids=(
        "invalid-percent-escape",
        "invalid-remote-port",
        "file-query",
        "file-fragment",
        "decoded-nul",
        "non-local-file-authority",
    ),
)
def test_malformed_source_references_do_not_resolve(reference):
    assert resolve_source(reference) is None


@pytest.mark.parametrize(
    "source_kind",
    ("final-symlink", "directory", "unreadable-file", "fifo"),
)
def test_unsafe_or_non_regular_final_source_component_is_unavailable(
    tmp_path,
    source_kind,
):
    source = tmp_path / "final-source"
    restore_permissions = False
    if source_kind == "final-symlink":
        target = tmp_path / "real-source.py"
        target.write_text("available = True\n", encoding="utf-8")
        source.symlink_to(target)
    elif source_kind == "directory":
        source.mkdir()
    elif source_kind == "unreadable-file":
        source.write_text("available = True\n", encoding="utf-8")
        source.chmod(0)
        restore_permissions = True
        if os.access(source, os.R_OK):
            source.chmod(0o600)
            pytest.skip("test process can read mode-000 files")
    else:
        os.mkfifo(source)
    item = replace(
        capability(
            f"auth.{source_kind}",
            "OAuth login",
            "Reusable OAuth authentication",
            contract="OAuth callback creates an authenticated session",
        ),
        source_uri=str(source),
    )

    try:
        assert source_available(item, ()) is False
    finally:
        if restore_permissions:
            source.chmod(0o600)


@pytest.mark.parametrize(
    ("required", "offered", "expected_fit"),
    [
        (
            "requires Python >=3.12,<4 with AES 256 encryption",
            "supports Python >=3.12,<4",
            0.5,
        ),
        (
            "requires AES 256 encryption with Python >=3.12,<4",
            "supports Python >=3.12,<4",
            0.5,
        ),
        ("requires AES 256 encryption and Python >=3.12,<4", "supports Python >=3.12,<4", 0.5),
        ("requires AES 256 encryption, Python >=3.12,<4", "supports Python >=3.12,<4", 0.5),
        ("requires AES 256 encryption, and Python >=3.12,<4", "supports Python >=3.12,<4", 0.5),
        ("requires Python >=3.12,<4", "supports Python >=3.12,<4; must not use Python >=3.12,<4", 0.0),
        ("requires >=3.12,<4", "supports >=3.12,<4", 1.0),
        ("must use AES 256", "must not use AES 256", 0.0),
        ("platform Linux", "platform Windows", 0.0),
    ],
)
def test_every_mandatory_contract_clause_contributes_to_fit(
    tmp_path, embeddings, required, offered, expected_fit
):
    repository = CapabilityRepository.open(tmp_path / "mandatory-contract.lance")
    repository.initialize()
    source = tmp_path / "available.py"
    source.write_text("available = True\n", encoding="utf-8")
    stored = replace(
        capability(
            "contract.mandatory",
            "Contract proof",
            "Reusable contract implementation",
            contract=offered,
            stack=(),
            runtime=(),
        ),
        source_uri=source.as_uri(),
    )
    repository.upsert_capability(stored, embeddings.embed_query(stored.contract))
    repository.append_evidence(
        Evidence(
            id="current-verification",
            capability_id=stored.id,
            source_project="project-a",
            evidence_type="verification",
            outcome="passed",
            metric_name=None,
            metric_value=None,
            confidence=1.0,
            observed_at="2026-09-04T01:00:00Z",
            supporting_uri=source.as_uri(),
        )
    )

    result = CapabilitySearch(repository, embeddings).search(
        requirement("contract proof", contract=required)
    )

    assert result.status is SearchStatus.COMPLETE
    assert result.matches[0].contract_fit == pytest.approx(expected_fit)
    action = ReuseDecisionEngine().decide(requirement("contract proof", contract=required), result).action
    assert (action == "reuse") is (expected_fit == 1.0)


@pytest.mark.parametrize(
    ("required_contract", "offered_contract", "expected_fit"),
    [
        (
            "must use AES 256 encryption",
            "must not use AES 256 encryption",
            0.0,
        ),
        (
            "authentication mode must be oauth",
            "authentication mode must be password",
            0.0,
        ),
        (
            "requires Python >=3.12,<4",
            "supports Python >=3.9,<3.11",
            0.0,
        ),
        (
            "requires Python >=3.12,<4",
            "supports Python >=3.11,<4",
            0.5,
        ),
        (
            "requires Python >=3.12,<4",
            "supports Python >=3.12,<4",
            1.0,
        ),
        (
            "requires Python >3.11,<4",
            "supports Python >3.12,<3.13",
            0.5,
        ),
        (
            "must return JWT bearer; must rotate refresh tokens",
            "returns a JWT bearer token",
            0.5,
        ),
        (
            "JWT bearer",
            "Returns a JWT bearer token",
            1.0,
        ),
    ],
    ids=(
        "negated-clause",
        "mutually-exclusive-value",
        "incompatible-version-range",
        "overlapping-non-equivalent-version-range",
        "equivalent-version-range",
        "overlapping-open-version-range",
        "missing-mandatory-clause",
        "proven-compatible",
    ),
)
def test_structured_contract_fit_rejects_explicit_incompatibility_before_similarity(
    embeddings,
    required_contract,
    offered_contract,
    expected_fit,
):
    item = capability(
        "contract.test",
        "OAuth authentication adapter",
        "OAuth authentication adapter with very high semantic similarity",
        contract=offered_contract,
        stack=(),
    )

    class ContractRepository:
        def list_capabilities(self):
            return (item,)

        def hybrid_search(self, *args, **kwargs):
            return ([{"id": item.id}], [{"id": item.id}], [{"id": item.id}])

        def list_evidence(self, capability_id):
            return ()

    result = CapabilitySearch(ContractRepository(), embeddings).search(
        requirement("OAuth authentication", contract=required_contract)
    )

    assert result.status is SearchStatus.COMPLETE
    assert result.matches[0].contract_fit == expected_fit
    assert (
        "contract mismatch" in result.matches[0].rejection_reasons
    ) is (expected_fit == 0.0)


def test_final_limit_is_applied_after_reuse_eligibility(tmp_path, embeddings):
    repo = CapabilityRepository.open(tmp_path / "eligible-before-limit.lance")
    repo.initialize()
    for index in range(24):
        add(
            repo,
            embeddings,
            replace(
                capability(
                    f"candidate-{index:02d}",
                    "OAuth OAuth login",
                    "OAuth authentication",
                    contract="OAuth callback creates a session",
                    lifecycle=Lifecycle.CANDIDATE,
                    confidence=1.0,
                ),
                last_verified_at=None,
            ),
        )
    exact = replace(
        capability(
            "auth.verified-exact",
            "Identity adapter",
            "Reusable sign-in",
            contract="OAuth callback creates a session",
            confidence=0.5,
        ),
        source_uri=(tmp_path / "verified.py").as_uri(),
    )
    (tmp_path / "verified.py").write_text("verified = True\n", encoding="utf-8")
    repo.upsert_capability(exact, vector(0.0, 1.0))

    result = CapabilitySearch(repo, embeddings).search(
        requirement(
            "OAuth login",
            contract="OAuth callback creates a session",
        ),
        limit=1,
    )

    assert result.status is SearchStatus.COMPLETE
    assert [match.capability_id for match in result.matches] == [exact.id]


@pytest.mark.parametrize(
    ("filter_name", "wanted", "wrong", "right"),
    [
        ("runtime", ("Python 3.12",), ("Node.js",), ("Python 3.12",)),
        ("platform", ("linux",), ("windows",), ("linux",)),
        ("license", ("Apache-2.0",), "GPL-3.0", "Apache-2.0"),
    ],
)
def test_typed_compatibility_filters_are_applied_before_all_retrieval_routes(
    tmp_path,
    embeddings,
    filter_name,
    wanted,
    wrong,
    right,
):
    class RecordingRepository(CapabilityRepository):
        routes = None

        def hybrid_search(self, *args, **kwargs):
            self.routes = super().hybrid_search(*args, **kwargs)
            return self.routes

    repo = RecordingRepository.open(tmp_path / f"typed-{filter_name}.lance")
    repo.initialize()
    source = tmp_path / f"{filter_name}.py"
    source.write_text("available = True\n", encoding="utf-8")
    field = "license" if filter_name == "license" else filter_name
    add(
        repo,
        embeddings,
        replace(
            capability(
                "wrong",
                "OAuth OAuth login",
                "OAuth authentication",
                contract="OAuth callback",
                **{field: wrong},
            ),
            source_uri=source.as_uri(),
        ),
    )
    exact = replace(
        capability(
            "exact",
            "Identity adapter",
            "Sign-in",
            contract="OAuth callback",
            **{field: right},
        ),
        source_uri=source.as_uri(),
    )
    add(repo, embeddings, exact)

    result = CapabilitySearch(repo, embeddings).search(
        requirement("OAuth login", **{filter_name: wanted}),
        limit=1,
    )

    assert result.status is SearchStatus.COMPLETE
    assert [match.capability_id for match in result.matches] == [exact.id]
    assert repo.routes is not None
    assert all([row["id"] for row in route] == [exact.id] for route in repo.routes)


@pytest.mark.parametrize(
    "relationship_type",
    ("dependency", "alternative", "composition", "consumer"),
)
def test_evidence_backed_compatible_relationships_expand_search_candidates(
    tmp_path,
    embeddings,
    relationship_type,
):
    class SeedOnlyRetrievalRepository(CapabilityRepository):
        def hybrid_search(self, *args, **kwargs):
            routes = super().hybrid_search(*args, **kwargs)
            return tuple(
                [row for row in route if row["id"] == "auth.seed"]
                for route in routes
            )

    repo = SeedOnlyRetrievalRepository.open(
        tmp_path / f"relation-{relationship_type}.lance"
    )
    repo.initialize()
    source = tmp_path / "available.py"
    source.write_text("available = True\n", encoding="utf-8")
    seed = replace(
        capability(
            "auth.seed",
            "OAuth login",
            "OAuth authentication",
            contract="OAuth callback",
            runtime=("Python 3.12",),
        ),
        source_uri=source.as_uri(),
    )
    related = replace(
        capability(
            "auth.related",
            "Credential boundary",
            "Opaque identity primitive",
            contract="OAuth callback",
            runtime=("Python 3.12",),
        ),
        source_uri=source.as_uri(),
    )
    add(repo, embeddings, seed)
    add(repo, embeddings, related)
    evidence = Evidence(
        id="relation-proof",
        capability_id=seed.id,
        source_project="project-a",
        evidence_type="verification",
        outcome="passed",
        metric_name=None,
        metric_value=None,
        confidence=1.0,
        observed_at="2026-09-04T00:00:00Z",
        supporting_uri=source.as_uri(),
    )
    repo.append_evidence(evidence)
    repo.append_relationship(
        Relationship(
            id=f"relation-{relationship_type}",
            source_id=seed.id,
            target_id=related.id,
            relationship_type=relationship_type,
            compatibility=("Python 3.12",),
            evidence_ids=(evidence.id,),
        )
    )

    result = CapabilitySearch(repo, embeddings).search(
        requirement("OAuth login", runtime=("Python 3.12",)),
        limit=2,
    )

    assert result.status is SearchStatus.COMPLETE
    assert {match.capability_id for match in result.matches} == {seed.id, related.id}


def relationship_search_result(
    tmp_path,
    embeddings,
    *,
    relationship_type="dependency",
    compatibility=(),
    outcome="passed",
    evidence_endpoint="seed",
    superseded_outcome=None,
    required_contract="",
    related_changes=None,
):
    class SeedOnlyRetrievalRepository(CapabilityRepository):
        def hybrid_search(self, *args, **kwargs):
            routes = super().hybrid_search(*args, **kwargs)
            return tuple(
                [row for row in route if row["id"] == "auth.seed"]
                for route in routes
            )

    repository = SeedOnlyRetrievalRepository.open(tmp_path / "relationship-proof.lance")
    repository.initialize()
    source = tmp_path / "relationship-source.py"
    source.write_text("available = True\n", encoding="utf-8")
    seed = replace(
        capability(
            "auth.seed",
            "OAuth login",
            "OAuth authentication",
            contract=required_contract or "OAuth callback",
            runtime=("python",),
            platform=("linux",),
        ),
        source_uri=source.as_uri(),
    )
    related = replace(
        capability(
            "auth.related",
            "Credential boundary",
            "Opaque identity primitive",
            contract=required_contract or "OAuth callback",
            runtime=("python",),
            platform=("linux",),
        ),
        source_uri=source.as_uri(),
    )
    if related_changes:
        related = replace(related, **related_changes)
    add(repository, embeddings, seed)
    add(repository, embeddings, related)
    evidence_capability = seed if evidence_endpoint == "seed" else related
    proof = Evidence(
        id="relationship-proof-a",
        capability_id=evidence_capability.id,
        source_project="project-a",
        evidence_type="verification",
        outcome=outcome,
        metric_name=None,
        metric_value=None,
        confidence=1.0,
        observed_at="2026-09-04T00:00:00Z",
        supporting_uri=source.as_uri(),
    )
    repository.append_evidence(proof)
    if superseded_outcome is not None:
        repository.append_evidence(
            replace(
                proof,
                id="relationship-proof-z",
                outcome=superseded_outcome,
            )
        )
    repository.append_relationship(
        Relationship(
            id=f"relation-{relationship_type}",
            source_id=seed.id,
            target_id=related.id,
            relationship_type=relationship_type,
            compatibility=compatibility,
            evidence_ids=(proof.id,),
        )
    )
    result = CapabilitySearch(repository, embeddings).search(
        requirement(
            seed.name,
            contract=required_contract,
            runtime=("python",),
            platform=("linux",),
        ),
        limit=2,
    )
    return result, seed, related


@pytest.mark.parametrize(
    "relationship_type",
    ("dependency", "alternative", "composition", "consumer"),
)
def test_failed_relationship_evidence_never_expands_candidate(
    tmp_path, embeddings, relationship_type
):
    result, _, related = relationship_search_result(
        tmp_path,
        embeddings,
        relationship_type=relationship_type,
        outcome="failed",
    )

    assert result.status is SearchStatus.COMPLETE
    assert related.id not in {match.capability_id for match in result.matches}


@pytest.mark.parametrize(
    "relationship_type",
    ("dependency", "alternative", "composition", "consumer"),
)
def test_relationship_compatibility_requires_every_dimension(
    tmp_path, embeddings, relationship_type
):
    result, _, related = relationship_search_result(
        tmp_path,
        embeddings,
        relationship_type=relationship_type,
        compatibility=("python", "windows"),
    )

    assert result.status is SearchStatus.COMPLETE
    assert related.id not in {match.capability_id for match in result.matches}


def test_relationship_evidence_can_resolve_from_the_related_endpoint(
    tmp_path, embeddings
):
    result, seed, related = relationship_search_result(
        tmp_path,
        embeddings,
        evidence_endpoint="related",
    )

    assert result.status is SearchStatus.COMPLETE
    assert {match.capability_id for match in result.matches} == {seed.id, related.id}


def test_newer_failed_relationship_evidence_invalidates_older_passing_proof(
    tmp_path, embeddings
):
    result, _, related = relationship_search_result(
        tmp_path,
        embeddings,
        superseded_outcome="failed",
    )

    assert result.status is SearchStatus.COMPLETE
    assert related.id not in {match.capability_id for match in result.matches}


@pytest.mark.parametrize(
    "gate",
    (
        "missing-verification",
        "unavailable-source",
        "non-positive-value",
        "non-reusable-lifecycle",
        "contract-mismatch",
    ),
)
def test_relationship_expansion_requires_every_normal_candidate_gate(
    tmp_path, embeddings, gate
):
    related_changes = {
        "missing-verification": {"last_verified_at": None},
        "unavailable-source": {"source_uri": (tmp_path / "missing.py").as_uri()},
        "non-positive-value": {"expected_net_value": 0.0},
        "non-reusable-lifecycle": {"lifecycle": Lifecycle.CANDIDATE},
        "contract-mismatch": {"contract": "platform Windows"},
    }[gate]
    required_contract = "platform Linux" if gate == "contract-mismatch" else ""
    result, _, related = relationship_search_result(
        tmp_path,
        embeddings,
        required_contract=required_contract,
        related_changes=related_changes,
    )

    assert result.status is SearchStatus.COMPLETE
    assert related.id not in {match.capability_id for match in result.matches}


def test_dependency_field_without_relationship_evidence_does_not_expand_candidate(
    tmp_path,
    embeddings,
):
    class SeedOnlyRetrievalRepository(CapabilityRepository):
        def hybrid_search(self, *args, **kwargs):
            routes = super().hybrid_search(*args, **kwargs)
            return tuple(
                [row for row in route if row["id"] == "auth.seed"]
                for route in routes
            )

    repo = SeedOnlyRetrievalRepository.open(tmp_path / "unbacked-dependency.lance")
    repo.initialize()
    source = tmp_path / "available.py"
    source.write_text("available = True\n", encoding="utf-8")
    seed = replace(
        capability(
            "auth.seed",
            "OAuth login",
            "OAuth authentication",
            contract="OAuth callback",
            dependencies=("auth.unbacked",),
        ),
        source_uri=source.as_uri(),
    )
    unbacked = replace(
        capability(
            "auth.unbacked",
            "Unbacked dependency",
            "No relationship evidence",
            contract="OAuth callback",
        ),
        source_uri=source.as_uri(),
    )
    add(repo, embeddings, seed)
    add(repo, embeddings, unbacked)

    result = CapabilitySearch(repo, embeddings).search(
        requirement("OAuth login"),
        limit=2,
    )

    assert result.status is SearchStatus.COMPLETE
    assert [match.capability_id for match in result.matches] == [seed.id]


def test_fastembed_does_not_attempt_download_when_explicitly_offline(
    tmp_path, monkeypatch
):
    import supermind_memory.embeddings as embedding_module

    attempts = 0

    def missing_snapshot(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            raise AssertionError("remote snapshot fallback attempted while offline")
        raise LocalEntryNotFoundError("snapshot is not cached")

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(embedding_module, "snapshot_download", missing_snapshot)

    with pytest.raises(LocalEntryNotFoundError, match="not cached"):
        FastEmbedProvider(
            model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            cache_dir=tmp_path / "models",
        )


def test_unlocked_model_is_rejected_before_any_download(tmp_path, monkeypatch):
    import supermind_memory.embeddings as embedding_module

    def forbidden(*args, **kwargs):
        raise AssertionError("download attempted before lock validation")

    monkeypatch.setattr(embedding_module, "snapshot_download", forbidden)
    monkeypatch.setattr(embedding_module, "TextEmbedding", forbidden)

    with pytest.raises(ValueError, match="not the locked embedding model"):
        FastEmbedProvider("unlocked/model", tmp_path / "models")


@pytest.mark.model
def test_fastembed_uses_locked_snapshot_and_stays_local_after_download(tmp_path, monkeypatch):
    provider = FastEmbedProvider(
        model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        cache_dir=tmp_path / "models",
    )
    expected_snapshot = provider.snapshot_dir
    assert expected_snapshot.name == "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"

    import supermind_memory.embeddings as embedding_module

    real_snapshot_download = embedding_module.snapshot_download
    real_text_embedding = embedding_module.TextEmbedding

    def cached_snapshot_download(*args, **kwargs):
        assert kwargs["revision"] == "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
        assert kwargs["local_files_only"] is True
        return real_snapshot_download(*args, **kwargs)

    def guarded_text_embedding(*args, **kwargs):
        assert kwargs["local_files_only"] is True
        model_path = kwargs["specific_model_path"]
        assert model_path.endswith("e8f8c211226b894fcb81acc59f3b34ba3efd5f42")
        return real_text_embedding(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("network or FastEmbed source download attempted")

    monkeypatch.setattr(embedding_module, "snapshot_download", cached_snapshot_download)
    monkeypatch.setattr(embedding_module, "TextEmbedding", guarded_text_embedding)
    monkeypatch.setattr(ModelManagement, "download_files_from_huggingface", forbidden)
    monkeypatch.setattr(ModelManagement, "retrieve_model_gcs", forbidden)
    monkeypatch.setattr("httpx.Client.send", forbidden)
    cached_provider = FastEmbedProvider(
        model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        cache_dir=tmp_path / "models",
    )

    assert cached_provider.snapshot_dir == expected_snapshot
    assert len(cached_provider.embed_query("为用户提供邮箱登录")) == 384
    assert len(cached_provider.embed_documents(["OAuth email login"])[0]) == 384

    # The cached locked model must pass the actual activation corpus with every
    # network boundary above disabled, including bounded rankings and decisions.
    with CapabilityRepository.open(tmp_path / "evaluation-candidate" / "database") as candidate:
        candidate.initialize()
        candidate._table("capabilities").create_index("search_text", config=FTS(), replace=True)
        report = evaluate_generation(candidate, cached_provider, evaluation_dataset_path())
        assert report.passed, report.metrics


def test_final_yaml_scalars_never_cross_search_or_diagnostics(search, embeddings, monkeypatch, yaml_scalar_secret):
    payload, secrets = yaml_scalar_secret
    queries, lexical = [], []
    original = search._repository.hybrid_search

    class RecordingEmbeddings:
        def embed_query(self, text):
            queries.append(text)
            return embeddings.embed_query(text)

    def hybrid(**kwargs):
        lexical.append(kwargs["query_text"])
        return original(**kwargs)

    search._embeddings = RecordingEmbeddings()
    monkeypatch.setattr(search._repository, "hybrid_search", hybrid)
    result = search.search(requirement("login\n" + payload))
    assert result.status is SearchStatus.COMPLETE
    assert queries and lexical
    assert all(secret not in repr((queries, lexical, result)) for secret in secrets)

    def failed(**kwargs):
        raise RuntimeError(payload)

    monkeypatch.setattr(search._repository, "hybrid_search", failed)
    failure = search.search(requirement("login"))
    assert failure.status is SearchStatus.FAILED
    assert all(secret not in repr(failure) for secret in secrets)


@pytest.mark.parametrize("outcome", ["failed", "error", "unknown", "pending", ""])
def test_final_remote_availability_latest_nonaffirmative_blocks_reuse(tmp_path, embeddings, outcome):
    with CapabilityRepository.open(tmp_path / "database") as repository:
        repository.initialize()
        item = replace(capability("remote-login", "OAuth login", "Hosted OAuth login", contract="OAuth login"),
                       source_uri="https://example.invalid/login")
        add(repository, embeddings, item)
        proof = Evidence("source-passed", item.id, "project", "source_availability", "passed",
                         None, None, 1.0, "2026-09-06T00:00:00Z", None)
        repository.append_evidence(proof)
        repository.append_evidence(replace(proof, id="source-latest", outcome=outcome,
                                           observed_at="2026-09-06T01:00:00Z"))
        wanted = requirement("OAuth login", contract=item.contract)
        result = CapabilitySearch(repository, embeddings).search(wanted)
        assert result.status is SearchStatus.COMPLETE
        assert result.matches[0].source_available is False
        assert ReuseDecisionEngine().decide(wanted, result).action == "build"


@pytest.mark.parametrize("policy", [False, True])
def test_final_evaluation_detects_broken_relationship_policy(evaluation_repository, embeddings, monkeypatch, policy):
    import supermind_memory.search as search_module

    monkeypatch.setattr(search_module, "relationship_compatible", lambda *args: policy)
    report = evaluate_generation(evaluation_repository, embeddings, evaluation_dataset_path())
    assert report.passed is False
    assert min(report.metrics.values()) < 1.0


@pytest.mark.parametrize("coverage", ["relationships", "stack", "category_hint"])
def test_final_evaluation_requires_relationship_stack_category_coverage(tmp_path, evaluation_repository, embeddings, coverage):
    dataset = json.loads(evaluation_dataset_path().read_text())
    if coverage == "relationships":
        dataset.pop("relationships", None)
        dataset.pop("evidence", None)
    else:
        for query in dataset["queries"]:
            query.pop(coverage, None)
    path = tmp_path / "incomplete.json"
    path.write_text(json.dumps(dataset))
    with pytest.raises(ValueError, match="retrieval evaluation dataset"):
        evaluate_generation(evaluation_repository, embeddings, path)


@pytest.mark.parametrize("fault", ["no-current-success", "irrelevant-failure", "stale-no-successor"])
def test_final_evaluation_requires_discriminative_relationship_evidence(tmp_path, evaluation_repository, embeddings, fault):
    dataset = json.loads(evaluation_dataset_path().read_text())
    if fault == "no-current-success":
        for evidence in dataset["evidence"]:
            if evidence["id"] in {"edge-jwt-pass", "edge-python-pass"}:
                evidence["outcome"] = "failed"
    elif fault == "irrelevant-failure":
        for evidence in dataset["evidence"]:
            evidence["outcome"] = "passed"
        dataset["evidence"].append({"id": "unrelated-failure", "capability_id": "eval-legacy-python",
                                    "source_project": "unused", "outcome": "failed", "observed_at": "2026-09-06T00:00:00Z"})
    else:
        dataset["evidence"] = [item for item in dataset["evidence"] if item["id"] != "edge-aes-current"]
    path = tmp_path / "nondiscriminative-relationships.json"
    path.write_text(json.dumps(dataset))
    with pytest.raises(ValueError, match="retrieval evaluation dataset"):
        evaluate_generation(evaluation_repository, embeddings, path)
