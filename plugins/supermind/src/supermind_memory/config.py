"""Configuration and owned filesystem paths for capability memory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def resolve_codex_home(env: Mapping[str, str] | None = None) -> Path:
    """Return the configured Codex data home without changing environment state."""
    source = env if env is not None else None
    configured = source.get("CODEX_HOME") if source is not None else None
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


@dataclass(frozen=True)
class MemoryPaths:
    """Filesystem locations owned by the capability-memory subsystem."""

    root: Path
    database: Path
    runtime: Path
    model_cache: Path
    locks: Path
    generations: Path

    @classmethod
    def from_codex_home(cls, codex_home: Path) -> "MemoryPaths":
        root = codex_home.expanduser().resolve() / "supermind" / "capability-memory"
        return cls(
            root=root,
            database=root / "database",
            runtime=root / "runtime",
            model_cache=root / "model-cache",
            locks=root / "locks",
            generations=root / "generations",
        )
