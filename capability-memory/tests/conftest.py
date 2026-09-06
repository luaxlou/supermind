"""Shared test configuration for capability memory."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

import pytest

from supermind_memory.schema import EMBEDDING_DIMENSION
from supermind_memory.repository import shutdown_repository_runtime


@pytest.fixture(scope="session", autouse=True)
def close_opened_repositories_at_session_end():
    yield
    shutdown_repository_runtime()


class KeywordEmbeddingProvider:
    """Small bilingual semantic embedding used only by integration tests."""

    _CONCEPTS = (
        (
            "login",
            "sign in",
            "authentication",
            "oauth",
            "federated",
            "identity",
            "third-party",
            "登录",
            "登陆",
            "邮箱",
            "第三方",
            "身份",
            "认证",
        ),
        ("jwt", "token"),
        ("session", "cookie"),
        ("typescript",),
        ("python",),
        ("chart", "renderer", "图表"),
    )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        normalized = re.sub(r"[^\w\u3400-\u9fff]+", " ", text.casefold())
        vector = [0.0] * EMBEDDING_DIMENSION
        for index, vocabulary in enumerate(self._CONCEPTS):
            vector[index] = float(sum(term in normalized for term in vocabulary))
        magnitude = math.sqrt(sum(value * value for value in vector))
        return [value / magnitude for value in vector] if magnitude else vector


@pytest.fixture
def embeddings() -> KeywordEmbeddingProvider:
    return KeywordEmbeddingProvider()


@pytest.fixture
def event_transactions():
    """Use real replay/projection for service tests without network transport."""
    from types import SimpleNamespace
    from supermind_memory.event_store import EventStore
    from supermind_memory.projection import project_authority
    from supermind_memory.replay import replay
    from supermind_memory.sync import SyncReport, SyncState

    class Transactions:
        def __init__(self, paths, repository, provider):
            self.paths = paths
            self.repository = repository
            self.embeddings = provider
            paths.checkout.mkdir(parents=True, exist_ok=True)
            self.store = EventStore(paths.checkout)
            self.config = SimpleNamespace(
                device_id="service-test", repository="owner/memory",
                web_url="https://github.com/owner/memory", last_checked_remote_head="a" * 40,
            )
            project_authority(replay(()), repository, provider)

        def mutate(self, factory):
            return self.mutate_batch(lambda current: (factory(current),))

        def mutate_batch(self, factory):
            existing = self.store.load_all()
            additions = tuple(factory(replay(existing)))
            result = replay((*existing, *additions))
            project_authority(result, self.repository, self.embeddings)
            for event in additions:
                self.store.append(event)
            return SyncReport(SyncState.COMMITTED, "b" * 40, result.digest, "a" * 40)

    return Transactions


@pytest.fixture(params=[
    'password: !!str "first scalar material second scalar material"',
    "password: &credential 'first scalar material second scalar material'",
    'password: &credential !!str "first scalar material second scalar material"',
    "password: !<tag:yaml.org,2002:str> &credential first scalar material second scalar material",
    "password: first scalar material second scalar material",
    "password: first scalar material\n  second scalar material",
    "password:\n  first scalar material\n  second scalar material",
    'password: &credential\n  !!str "first scalar material\n    second scalar material"',
    "password: !!str\n  &credential 'first scalar material\n    second scalar material'",
    "password: !!str\n  &credential first scalar material\n    second scalar material",
    "parent:\n  password: !!str first scalar material\n    second scalar material",
    "items:\n  - password: &credential first scalar material\n      second scalar material",
    '{password: !!str "first scalar material second scalar material", mode: safe}',
    "{password: &credential first scalar material second scalar material, mode: safe}",
    "{password: !!str &credential 'first scalar material\n second scalar material', mode: safe}",
    'password: "first scalar material\nsecond scalar material"',
    "parent:\n  password: !!str &credential 'first scalar material\nsecond scalar material'",
    "password: &credential\n# property comment\n  !!str |\n    first scalar material\n    second scalar material",
    "[password: first scalar material second scalar material, mode: safe]",
    '[password: !!str "first scalar material second scalar material", mode: safe]',
    "[password: &credential first scalar material second scalar material, mode: safe]",
    "[password: 'first scalar material second scalar material', mode: safe]",
    "? password\n: first scalar material second scalar material",
    '? password\n: !!str "first scalar material second scalar material"',
    "? password\n: &credential first scalar material second scalar material",
    "? password\n: 'first scalar material second scalar material'",
    "items:\n  - ? password\n    : &credential !!str first scalar material\n      second scalar material\n    mode: safe",
    "[? password: !!str first scalar material second scalar material, mode: safe]",
    "[? password\n: !!str first scalar material second scalar material, mode: safe]",
    "[? password # key comment\n: &credential first scalar material second scalar material, mode: safe]",
    "{? password: &credential first scalar material second scalar material, mode: safe}",
    "? password\n# key comment\n\n: !!str first scalar material second scalar material",
    "password: 'first scalar material\rsecond scalar material'\rsecret: !!str first scalar material second scalar material\rmode: safe",
    "?\n  password\n: first scalar material second scalar material\nmode: safe",
    "?\n  password\n: !!str first scalar material second scalar material\nmode: safe",
    "?\n  password\n: &credential first scalar material second scalar material\nmode: safe",
    "?\n  password\n: 'first scalar material second scalar material'\nmode: safe",
    "?\n  password\n: |\n  first scalar material\n  second scalar material\nmode: safe",
    "?\n  password\n: !!str &credential >\n  first scalar material\n  second scalar material\nmode: safe",
    "[?\n  password\n: first scalar material second scalar material, mode: safe]",
    '[?\n  password\n: !!str "first scalar material second scalar material", mode: safe]',
    "[?\n  password\n: &credential first scalar material second scalar material, mode: safe]",
    "[?\n  password\n: 'first scalar material second scalar material', mode: safe]",
    "items:\n  - ?\n      password\n    : &credential !!str first scalar material\n      second scalar material\n    mode: safe",
    "items: [\n  ?\n    password\n  : &credential first scalar material second scalar material, mode: safe\n]",
    "? # explicit key\n\n  # key comment\n  password\n: !!str first scalar material second scalar material\nmode: safe",
    "[? # explicit key\n  # key comment\n  password\n: &credential first scalar material second scalar material, mode: safe]",
    "?\r  password\r: !!str first scalar material second scalar material\rmode: safe",
    "[?\r\n  password\r\n: &credential first scalar material second scalar material, mode: safe]",
])
def yaml_scalar_secret(request):
    return request.param + "\nscope: public\n", ("first scalar material", "second scalar material")
