"""Capability-memory startup orchestration."""

from __future__ import annotations

from pathlib import Path

from supermind_memory.config import MemoryPaths
from supermind_memory.embeddings import EmbeddingProvider
from supermind_memory.health import HealthManager
from supermind_memory.repository import CapabilityRepository
from supermind_memory.types import HealthReport


class Bootstrap:
    """Initialize owned storage and refuse startup until every health gate passes."""

    def __init__(
        self,
        paths: MemoryPaths,
        repository: CapabilityRepository,
        embedding_provider: EmbeddingProvider,
        *,
        health_manager: HealthManager | None = None,
    ) -> None:
        self.paths = paths
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.health_manager = health_manager or HealthManager(
            paths,
            repository,
            embedding_provider,
        )

    def initialize(self, project_root: Path) -> HealthReport:
        project_root = project_root.expanduser().resolve()
        if not project_root.is_dir():
            raise ValueError(f"project root is not a directory: {project_root}")
        return self.health_manager.ensure_healthy()
