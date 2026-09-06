"""Canonical, fail-closed capability source resolution."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from supermind_memory.types import Capability, Evidence


_REMOTE_SCHEMES = frozenset(("git", "http", "https", "ssh"))
_INVALID_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_AVAILABLE_OUTCOMES = frozenset(("available", "pass", "passed", "success", "successful"))


@dataclass(frozen=True)
class ResolvedSource:
    kind: str
    path: Path | None
    canonical_uri: str


def resolve_source(value: str) -> ResolvedSource | None:
    """Classify one absolute local path or supported canonical remote URI."""
    if not value or "\x00" in value:
        return None
    try:
        direct_path = Path(value)
        if direct_path.is_absolute():
            return _resolved_local(direct_path)
        parsed = urlsplit(value)
        scheme = parsed.scheme.casefold()
        if scheme == "file":
            if (
                parsed.netloc.casefold() not in {"", "localhost"}
                or parsed.query
                or parsed.fragment
                or _INVALID_ESCAPE.search(parsed.path)
            ):
                return None
            path = Path(unquote(parsed.path, errors="strict"))
            return _resolved_local(path) if path.is_absolute() else None
        if scheme in _REMOTE_SCHEMES:
            if (
                not parsed.netloc
                or parsed.hostname is None
                or any(character.isspace() for character in value)
                or _INVALID_ESCAPE.search(value)
            ):
                return None
            _ = parsed.port
            return ResolvedSource("remote", None, value)
    except (OSError, UnicodeError, ValueError):
        return None
    return None


def source_available(capability: Capability, evidence: Sequence[Evidence]) -> bool:
    """Return current availability under the shared local/remote evidence policy."""
    reported_available: bool | None = None
    for item in evidence:
        if item.capability_id != capability.id:
            continue
        evidence_type = _normalized(item.evidence_type)
        outcome = _normalized(item.outcome)
        if evidence_type not in {"source", "source_availability"}:
            continue
        # Every current observation supersedes the previous assertion. A failed
        # or unrecognized probe cannot leave an older affirmative proof active.
        reported_available = outcome in _AVAILABLE_OUTCOMES
    if reported_available is False:
        return False
    resolved = resolve_source(capability.source_uri)
    if resolved is None:
        return False
    if resolved.kind == "remote":
        return reported_available is True
    return resolved.path is not None and _regular_file_without_symlinks(resolved.path)


def _resolved_local(path: Path) -> ResolvedSource | None:
    if "\x00" in os.fspath(path) or ".." in path.parts:
        return None
    canonical_path = Path(os.path.abspath(path))
    return ResolvedSource("local", canonical_path, canonical_path.as_uri())


def _regular_file_without_symlinks(path: Path) -> bool:
    parts = path.parts
    if not path.is_absolute() or len(parts) < 2:
        return False
    descriptor = -1
    try:
        descriptor = os.open(parts[0], _directory_flags())
        for component in parts[1:-1]:
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(parts[-1], _file_flags(), dir_fd=descriptor)
        try:
            return stat.S_ISREG(os.fstat(file_descriptor).st_mode)
        finally:
            os.close(file_descriptor)
    except (OSError, ValueError):
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _normalized(value: str) -> str:
    return value.strip().casefold().replace("-", "_").replace(" ", "_")
