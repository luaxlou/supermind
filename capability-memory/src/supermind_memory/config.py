"""Configuration and owned filesystem paths for capability memory."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


_DEVICE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


def resolve_codex_home(env: Mapping[str, str] | None = None) -> Path:
    """Return the configured Codex data home without changing environment state."""
    source = env if env is not None else None
    configured = source.get("CODEX_HOME") if source is not None else None
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


@dataclass(frozen=True)
class MemoryPaths:
    """Filesystem locations owned by the capability-memory subsystem."""

    root: Path
    config: Path
    checkout: Path
    database: Path
    model_cache: Path
    locks: Path
    generations: Path

    @classmethod
    def from_codex_home(cls, codex_home: Path) -> "MemoryPaths":
        root = codex_home.expanduser().resolve() / "supermind" / "memory"
        return cls(
            root=root,
            config=root / "config.json",
            checkout=root / "repository",
            database=root / "derived" / "database",
            model_cache=root / "model-cache",
            locks=root / "locks",
            generations=root / "derived" / "generations",
        )

    @property
    def runtime(self) -> Path:
        """Compatibility location for existing local-only health metadata."""
        return self.root / "derived" / "runtime"


@dataclass(frozen=True)
class RepositoryConfig:
    """Non-secret identity and synchronization state for one memory repository."""

    repository_id: str
    repository: str
    web_url: str
    clone_url: str
    branch: str
    device_id: str
    protocol_version: int
    authority_mode: str
    last_checked_remote_head: str | None

    def __post_init__(self) -> None:
        if not _DEVICE_ID.fullmatch(self.device_id):
            raise ValueError("invalid_device_id")
        if self.protocol_version != 1:
            raise ValueError("unsupported_protocol_version")
        if self.authority_mode != "events-v1":
            raise ValueError("unsupported_authority_mode")

    def to_document(self) -> dict[str, object]:
        return {
            "authority_mode": self.authority_mode,
            "branch": self.branch,
            "clone_url": self.clone_url,
            "device_id": self.device_id,
            "last_checked_remote_head": self.last_checked_remote_head,
            "protocol_version": self.protocol_version,
            "repository": self.repository,
            "repository_id": self.repository_id,
            "web_url": self.web_url,
        }

    @classmethod
    def from_bytes(cls, raw: bytes) -> "RepositoryConfig":
        document = json.loads(raw)
        expected = {
            "authority_mode", "branch", "clone_url", "device_id",
            "last_checked_remote_head", "protocol_version", "repository",
            "repository_id", "web_url",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid_repository_config")
        return cls(**document)

    @classmethod
    def read(cls, path: Path) -> "RepositoryConfig":
        return cls.from_bytes(path.read_bytes())

    def write(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(
            self.to_document(), ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8") + b"\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
