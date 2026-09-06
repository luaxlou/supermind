"""Safely discover reusable capabilities from explicitly authorised metadata."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import stat
import tomllib
from uuid import uuid4
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from supermind_memory.redaction import redact_capability, redact_identifier, redact_text, redact_uri
from supermind_memory.types import ArtifactType, Capability, DiscoveryContext, DiscoveryResult, Lifecycle


_METADATA_LIMIT = 256 * 1024
_FRONTMATTER_LIMIT = 64 * 1024
_FRONTMATTER_LINES = 256
_README_LIMIT = 64 * 1024
_TEST_LIMIT = 64 * 1024
_ROOT = ("Code and components",)
_PLUGIN = ("Tools and integrations", "Plugins and MCP")
_SKILL = ("Tools and integrations", "Codex Skills")
_REGISTRY_KEY = "capability-discovery-source-registry-v1"
_IDENTITY_TOKENS = frozenset(("auth", "authentication", "oauth", "identity", "login", "authorization"))


class MetadataRepository(Protocol):
    def get_metadata(self, key: str) -> object | None: ...
    def set_metadata(self, key: str, value: object) -> None: ...
    def update_metadata(self, key: str, update: Any) -> object: ...


class SourceRegistry:
    """A repository-backed allow-list of the sources discovery may refresh."""

    _KINDS = ("active", "registered", "explicit", "installed")

    def __init__(self, repository: MetadataRepository | None = None) -> None:
        self._repository = repository
        self._sources = self._load()

    def register_active_project(self, path: Path) -> None:
        self._register("active", path)

    def register_registered_project(self, path: Path) -> None:
        self._register("registered", path)

    def register_explicit_project(self, path: Path) -> None:
        self._register("explicit", path)

    def register_installed_codex_home(self, path: Path) -> None:
        self._register("installed", path)

    @property
    def active_projects(self) -> tuple[Path, ...]:
        return self.projects("active")

    def projects(self, kind: str) -> tuple[Path, ...]:
        if kind not in self._KINDS:
            raise ValueError(f"unknown source kind: {kind}")
        return tuple(Path(value["path"]) for value in self._sources if kind in value["kinds"])

    def is_registered(self, path: Path) -> bool:
        candidate = str(path.expanduser().absolute())
        return any(candidate == item["path"] for item in self._sources)

    def identity(self, path: Path) -> str:
        candidate = str(path.expanduser().absolute())
        for item in self._sources:
            if item["path"] == candidate:
                return item["id"]
        return _local_source_identity(candidate)

    def detached(self) -> SourceRegistry:
        """Stage registrations without modifying authoritative repository metadata."""
        registry = SourceRegistry()
        registry._sources = copy.deepcopy(self._load() if self._repository is not None else self._sources)
        return registry

    def metadata(self) -> tuple[str, object]:
        return _REGISTRY_KEY, {"sources": copy.deepcopy(self._sources)}

    def _register(self, kind: str, path: Path) -> None:
        root = _SafeRoot.open(path)
        if root is None:
            return
        try:
            value = str(root.path)
        finally:
            root.close()
        if self._repository is not None:
            def merge(current: object | None) -> object:
                sources = self._normalise(current)
                current_source = next((item for item in sources if item["path"] == value), None)
                if current_source is None:
                    sources.append({"id": uuid4().hex, "path": value, "kinds": [kind]})
                elif kind not in current_source["kinds"]:
                    current_source["kinds"].append(kind)
                    current_source["kinds"].sort()
                sources.sort(key=lambda item: item["path"])
                return {"sources": sources}

            self._sources = self._normalise(self._repository.update_metadata(_REGISTRY_KEY, merge))
        else:
            current_source = next((item for item in self._sources if item["path"] == value), None)
            if current_source is None:
                self._sources.append({"id": _local_source_identity(value), "path": value, "kinds": [kind]})
            elif kind not in current_source["kinds"]:
                current_source["kinds"].append(kind)
                current_source["kinds"].sort()
            self._sources.sort(key=lambda item: item["path"])

    def _load(self) -> list[dict[str, Any]]:
        value = self._repository.get_metadata(_REGISTRY_KEY) if self._repository is not None else None
        return self._normalise(value)

    def _normalise(self, value: object | None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if not isinstance(value, dict):
            return result
        sources = value.get("sources")
        if isinstance(sources, list):
            for item in sources:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("path"), str):
                    continue
                kinds = item.get("kinds")
                if isinstance(kinds, list) and all(kind in self._KINDS for kind in kinds):
                    result.append({"id": item["id"], "path": item["path"], "kinds": sorted(set(kinds))})
            return sorted(result, key=lambda item: item["path"])
        # Migrate the round-two kind-indexed metadata without duplicating a path.
        by_path: dict[str, dict[str, Any]] = {}
        for kind in self._KINDS:
            entries = value.get(kind)
            if not isinstance(entries, list):
                continue
            for item in entries:
                if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("path"), str):
                    source = by_path.setdefault(item["path"], {"id": item["id"], "path": item["path"], "kinds": []})
                elif isinstance(item, str):
                    source = by_path.setdefault(item, {"id": _local_source_identity(item), "path": item, "kinds": []})
                else:
                    continue
                source["kinds"].append(kind)
        return sorted(({**item, "kinds": sorted(set(item["kinds"]))} for item in by_path.values()), key=lambda item: item["path"])


class _SafeRoot:
    """An authorised root held open while children are opened with ``openat``."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self.fd = fd

    @classmethod
    def open(cls, path: Path) -> _SafeRoot | None:
        absolute = path.expanduser().absolute()
        try:
            fd = os.open(absolute, _directory_flags())
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                os.close(fd)
                return None
            return cls(absolute, fd)
        except OSError:
            return None

    def close(self) -> None:
        os.close(self.fd)

    def is_dir(self, parts: tuple[str, ...]) -> bool:
        fd = self._open_dir(parts)
        if fd is None:
            return False
        os.close(fd)
        return True

    def read(self, parts: tuple[str, ...], limit: int) -> bytes | None:
        fd = self._open_file(parts)
        if fd is None:
            return None
        try:
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            return data if len(data) <= limit else None
        except OSError:
            return None
        finally:
            os.close(fd)

    def directories(self, parts: tuple[str, ...]) -> tuple[str, ...]:
        fd = self._open_dir(parts)
        if fd is None:
            return ()
        try:
            with os.scandir(os.dup(fd)) as entries:
                names = [entry.name for entry in entries if entry.is_dir(follow_symlinks=False)]
            return tuple(sorted(name for name in names if self.is_dir((*parts, name))))
        except OSError:
            return ()
        finally:
            os.close(fd)

    def files_named(self, parts: tuple[str, ...], filename: str, max_depth: int = 32) -> Iterator[tuple[str, ...]]:
        if max_depth < 0 or not self.is_dir(parts):
            return
        if self.is_file((*parts, filename)):
            yield (*parts, filename)
        if max_depth:
            for child in self.directories(parts):
                yield from self.files_named((*parts, child), filename, max_depth - 1)

    def file_names(self, parts: tuple[str, ...]) -> tuple[str, ...]:
        fd = self._open_dir(parts)
        if fd is None:
            return ()
        try:
            with os.scandir(os.dup(fd)) as entries:
                names = [entry.name for entry in entries if entry.is_file(follow_symlinks=False)]
            return tuple(sorted(names))
        except OSError:
            return ()
        finally:
            os.close(fd)

    def is_file(self, parts: tuple[str, ...]) -> bool:
        fd = self._open_file(parts)
        if fd is None:
            return False
        os.close(fd)
        return True

    def read_frontmatter(self, parts: tuple[str, ...]) -> bytes | None:
        """Read only through the closing delimiter using the already-open fd."""
        fd = self._open_file(parts)
        if fd is None:
            return None
        try:
            lines: list[bytes] = []
            line = bytearray()
            total = 0
            while total < _FRONTMATTER_LIMIT and len(lines) < _FRONTMATTER_LINES:
                byte = os.read(fd, 1)
                if not byte:
                    value = bytes(line).rstrip(b"\r\n")
                    return b"\n".join((*lines, value)) if len(lines) > 0 and value == b"---" else None
                total += 1
                line.extend(byte)
                if byte != b"\n":
                    continue
                value = bytes(line).rstrip(b"\r\n")
                lines.append(value)
                if len(lines) == 1 and value != b"---":
                    return None
                if len(lines) > 1 and value == b"---":
                    return b"\n".join(lines)
                line.clear()
            return None
        except OSError:
            return None
        finally:
            os.close(fd)

    def _open_dir(self, parts: tuple[str, ...]) -> int | None:
        fd = os.dup(self.fd)
        try:
            for part in parts:
                if not _component(part):
                    raise OSError("unsafe path component")
                child = os.open(part, _directory_flags(), dir_fd=fd)
                os.close(fd)
                fd = child
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("not a directory")
            return fd
        except OSError:
            os.close(fd)
            return None

    def _open_file(self, parts: tuple[str, ...]) -> int | None:
        if not parts or not all(_component(part) for part in parts):
            return None
        parent = self._open_dir(parts[:-1])
        if parent is None:
            return None
        try:
            fd = os.open(parts[-1], _file_flags(), dir_fd=parent)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                return None
            return fd
        except OSError:
            return None
        finally:
            os.close(parent)


class CapabilityDiscovery:
    def __init__(self, source_registry: SourceRegistry | None = None) -> None:
        self._source_registry = source_registry or SourceRegistry()

    def discover_staged(self, context: DiscoveryContext) -> tuple[DiscoveryResult, tuple[str, object]]:
        original = self._source_registry
        staged = original.detached()
        self._source_registry = staged
        try:
            result = self.discover(context)
            return result, staged.metadata()
        finally:
            self._source_registry = original

    def scan_active_project(self, path: Path) -> tuple[Capability, ...]:
        root = _SafeRoot.open(path)
        if root is None:
            return ()
        try:
            return _scan_project(root, self._source_registry.identity(path))
        finally:
            root.close()

    def scan_installed_plugins(self, codex_home: Path) -> tuple[Capability, ...]:
        root = _SafeRoot.open(codex_home)
        if root is None:
            return ()
        try:
            return _scan_installed(root, self._source_registry.identity(codex_home))
        finally:
            root.close()

    def discover(self, context: DiscoveryContext) -> DiscoveryResult:
        self._source_registry.register_active_project(context.project_root)
        self._source_registry.register_installed_codex_home(context.codex_home)
        projects = _unique_paths((context.project_root, *self._source_registry.projects("active"), *self._source_registry.projects("registered"), *self._source_registry.projects("explicit")))
        codex_homes = _unique_paths((context.codex_home, *self._source_registry.projects("installed")))
        capabilities = [item for path in projects for item in self._scan_project_with_identity(path)]
        capabilities.extend(item for path in codex_homes for item in self._scan_installed_with_identity(path))
        sources = [str(path.expanduser().absolute()) for path in projects]
        sources.extend(label for path in codex_homes for label in _installed_source_labels(path))
        return DiscoveryResult(capabilities=_dedupe(capabilities), sources_scanned=tuple(sources))

    def _scan_project_with_identity(self, path: Path) -> tuple[Capability, ...]:
        root = _SafeRoot.open(path)
        if root is None:
            return ()
        try:
            return _scan_project(root, self._source_registry.identity(path))
        finally:
            root.close()

    def _scan_installed_with_identity(self, path: Path) -> tuple[Capability, ...]:
        root = _SafeRoot.open(path)
        if root is None:
            return ()
        try:
            return _scan_installed(root, self._source_registry.identity(path))
        finally:
            root.close()


def _scan_project(root: _SafeRoot, source_identity: str) -> tuple[Capability, ...]:
    readme = _read_readme(root)
    tests = tuple(_test_names(root))
    capabilities: list[Capability] = []
    package = _json(root, ("package.json",))
    if package is not None and _string(package.get("name")):
        summary = _summary(package.get("description"), readme)
        exports = _package_exports(package)
        capabilities.append(_capability(
            name=_string(package["name"]), summary=summary, artifact_type=ArtifactType.CODE,
            category_path=_category(package["name"], summary), source_uri=_uri(root, ("package.json",)),
            logical_source=f"{source_identity}:package:npm:{_string(package['name'])}", revision=_string(package.get("version")) or _git_revision(root) or "unversioned",
            facets=("manifest", *exports, *tests), contract=_contract(exports),
        ))
    pyproject = _toml(root, ("pyproject.toml",))
    project = pyproject.get("project") if isinstance(pyproject, dict) else None
    if isinstance(project, dict) and _string(project.get("name")):
        summary = _summary(project.get("description"), readme)
        exports = _python_exports(root, project, pyproject)
        capabilities.append(_capability(
            name=_string(project["name"]), summary=summary, artifact_type=ArtifactType.CODE,
            category_path=_category(project["name"], summary), source_uri=_uri(root, ("pyproject.toml",)),
            logical_source=f"{source_identity}:package:python:{_string(project['name'])}", revision=_string(project.get("version")) or _git_revision(root) or "unversioned",
            facets=("manifest", *exports, *tests), contract=_contract(exports),
        ))
    manifest, manifest_parts = _plugin_manifest(root, ())
    if manifest is not None:
        capabilities.extend(_plugin_capabilities(root, manifest, manifest_parts, readme, source_identity))
    skill = _skill(root, ("SKILL.md",), f"{source_identity}:project-skill", logical_path=("SKILL.md",))
    if skill is not None:
        capabilities.append(skill)
    return _dedupe(capabilities)


def _scan_installed(root: _SafeRoot, source_identity: str) -> tuple[Capability, ...]:
    capabilities: list[Capability] = []
    chosen: dict[str, tuple[tuple[Any, ...], list[Capability]]] = {}
    cache = ("plugins", "cache")
    if root.is_dir(cache):
        for plugin_parts in _plugin_roots(root, cache):
            manifest, manifest_parts = _plugin_manifest(root, plugin_parts)
            if manifest is None:
                continue
            records = _plugin_capabilities(root, manifest, manifest_parts, "", source_identity)
            identity = _plugin_identity(manifest, manifest_parts, source_identity)
            rank = _version_rank(_plugin_revision(manifest, manifest_parts))
            if identity not in chosen or rank > chosen[identity][0]:
                chosen[identity] = (rank, records)
    for _, records in chosen.values():
        capabilities.extend(records)
    if root.is_dir(("skills",)):
        for parts in root.files_named(("skills",), "SKILL.md"):
            skill = _skill(root, parts, f"{source_identity}:codex-home", logical_path=parts[1:-1])
            if skill is not None:
                capabilities.append(skill)
    return _dedupe(capabilities)


def _plugin_roots(root: _SafeRoot, cache: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    pending = [(cache, 0)]
    while pending:
        parts, depth = pending.pop()
        manifest, _ = _plugin_manifest(root, parts)
        if manifest is not None:
            yield parts
            continue
        if depth < 3:
            pending.extend(((*parts, name), depth + 1) for name in root.directories(parts))


def _plugin_manifest(root: _SafeRoot, base: tuple[str, ...]) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    for suffix in ((".codex-plugin", "plugin.json"), ("plugin.json",)):
        parts = (*base, *suffix)
        manifest = _json(root, parts)
        if manifest is not None and _string(manifest.get("name")):
            return manifest, parts
    return None, ()


def _plugin_capabilities(root: _SafeRoot, manifest: dict[str, Any], manifest_parts: tuple[str, ...], readme: str, source_identity: str) -> list[Capability]:
    identity = _plugin_identity(manifest, manifest_parts, source_identity)
    revision = _plugin_revision(manifest, manifest_parts)
    summary = _summary(manifest.get("description"), readme)
    records = [_capability(
        name=_string(manifest["name"]), summary=summary, artifact_type=ArtifactType.PLUGIN, category_path=_PLUGIN,
        source_uri=_uri(root, manifest_parts), logical_source=f"plugin:{identity}", revision=revision,
        facets=("plugin manifest",), contract="",
    )]
    base = manifest_parts[:-2] if manifest_parts[-2:] == (".codex-plugin", "plugin.json") else manifest_parts[:-1]
    for declared in _skill_directories(manifest):
        relative = _relative_parts(declared)
        if relative is None or not root.is_dir((*base, *relative)):
            continue
        for parts in root.files_named((*base, *relative), "SKILL.md"):
            skill = _skill(root, parts, f"plugin:{identity}", revision, logical_path=parts[len(base):-1])
            if skill is not None:
                records.append(skill)
    return records


def _skill(root: _SafeRoot, parts: tuple[str, ...], owner: str, revision: str = "installed", logical_path: tuple[str, ...] | None = None) -> Capability | None:
    frontmatter = _frontmatter(root.read_frontmatter(parts))
    if frontmatter is None:
        return None
    name = frontmatter["name"]
    metadata = frontmatter.get("metadata")
    metadata_version = metadata.get("version") if isinstance(metadata, dict) else None
    compatibility = frontmatter.get("compatibility")
    return _capability(
        name=name, summary=frontmatter.get("description", ""), artifact_type=ArtifactType.SKILL, category_path=_SKILL,
        source_uri=_uri(root, parts), logical_source=f"{owner}:skill:{'/'.join(logical_path if logical_path is not None else parts[:-1])}",
        revision=_string(metadata_version) or _string(frontmatter.get("version")) or revision,
        facets=("skill frontmatter",), contract="", license=_string(frontmatter.get("license")),
        compatibility=tuple(item for item in compatibility if isinstance(item, str)) if isinstance(compatibility, list) else (),
    )


def _json(root: _SafeRoot, parts: tuple[str, ...]) -> dict[str, Any] | None:
    text = _decode(root.read(parts, _METADATA_LIMIT))
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _toml(root: _SafeRoot, parts: tuple[str, ...]) -> dict[str, Any] | None:
    text = _decode(root.read(parts, _METADATA_LIMIT))
    if text is None:
        return None
    try:
        value = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _frontmatter(data: bytes | None) -> dict[str, Any] | None:
    text = _decode(data)
    if text is None:
        return None
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1, 257)
    except ValueError:
        return None
    parsed = _bounded_yaml(lines[1:end])
    return parsed if parsed is not None and _valid_skill_frontmatter(parsed) else None


def _valid_skill_frontmatter(value: dict[str, Any]) -> bool:
    name = value.get("name")
    if not isinstance(name, str) or not _valid_metadata_text(name):
        return False
    for field in ("description", "version", "license"):
        if field in value and not _bounded_skill_string(value[field], multiline=field == "description"):
            return False
    if "metadata" in value:
        metadata = value["metadata"]
        if not isinstance(metadata, dict):
            return False
        if "version" in metadata and not _bounded_skill_string(metadata["version"]):
            return False
    if "compatibility" in value:
        compatibility = value["compatibility"]
        if not isinstance(compatibility, list) or not all(_bounded_skill_string(item) for item in compatibility):
            return False
    return True


def _bounded_skill_string(value: object, *, multiline: bool = False) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 4096
        and "\x00" not in value
        and "\r" not in value
        and (multiline or "\n" not in value)
    )


def _package_exports(package: dict[str, Any]) -> tuple[str, ...]:
    exports: set[str] = set()
    for key in ("main", "module", "types"):
        value = package.get(key)
        if isinstance(value, str) and _relative_parts(value) is not None:
            exports.add(_normalise_export(value))
    _collect_js_exports(package.get("exports"), exports)
    return tuple(sorted(exports))


def _collect_js_exports(value: object, output: set[str]) -> None:
    if isinstance(value, str):
        if _relative_parts(value) is not None:
            output.add(_normalise_export(value))
    elif isinstance(value, dict):
        for key, target in value.items():
            if isinstance(key, str) and key.startswith("."):
                output.add(_normalise_export(key))
            _collect_js_exports(target, output)


def _python_exports(root: _SafeRoot, project: dict[str, Any], pyproject: dict[str, Any]) -> tuple[str, ...]:
    targets: list[str] = []
    entrypoints = project.get("entry-points")
    if isinstance(entrypoints, dict):
        for group in entrypoints.values():
            if isinstance(group, dict):
                targets.extend(value for value in group.values() if isinstance(value, str))
    scripts = project.get("scripts")
    if isinstance(scripts, dict):
        targets.extend(value for value in scripts.values() if isinstance(value, str))
    source_prefix: tuple[str, ...] = ()
    tool = pyproject.get("tool")
    setuptools = tool.get("setuptools") if isinstance(tool, dict) else None
    package_dir = setuptools.get("package-dir") if isinstance(setuptools, dict) else None
    if isinstance(package_dir, dict) and isinstance(package_dir.get(""), str):
        source_prefix = _relative_parts(package_dir[""]) or ()
    elif isinstance(setuptools, dict):
        packages = setuptools.get("packages")
        finder = packages.get("find") if isinstance(packages, dict) else None
        where = finder.get("where") if isinstance(finder, dict) else None
        if isinstance(where, list) and len(where) == 1 and isinstance(where[0], str):
            source_prefix = _relative_parts(where[0]) or ()
    exports: set[str] = set()
    for target in targets:
        components = target.split(":", 1)[0].split(".")
        if not components or not all(part.isidentifier() for part in components):
            continue
        module_parts = (*source_prefix, *components[:-1], f"{components[-1]}.py")
        init_parts = (*source_prefix, *components, "__init__.py")
        text = _decode(root.read(module_parts, _METADATA_LIMIT))
        if text is None:
            text = _decode(root.read(init_parts, _METADATA_LIMIT))
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and _is_all_assignment(node):
                exports.update(_literal_strings(node.value))
    return tuple(sorted(exports))


def _test_names(root: _SafeRoot) -> Iterator[str]:
    if not root.is_dir(("tests",)):
        return
    for parts in _python_test_files(root, ("tests",)):
        text = _decode(root.read(parts, _TEST_LIMIT))
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                yield node.name
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for method in node.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name.startswith("test_"):
                        yield method.name


def _python_test_files(root: _SafeRoot, directory: tuple[str, ...], depth: int = 32) -> Iterator[tuple[str, ...]]:
    if depth < 0:
        return
    for name in root.file_names(directory):
        if name.startswith("test_") and name.endswith(".py"):
            yield (*directory, name)
    for child in root.directories(directory):
        yield from _python_test_files(root, (*directory, child), depth - 1)


def _read_readme(root: _SafeRoot) -> str:
    for name in ("README.md", "README", "README.txt", "readme.md"):
        value = _decode(root.read((name,), _README_LIMIT))
        if value is not None:
            return value
    return ""


def _capability(*, name: str, summary: str, artifact_type: ArtifactType, category_path: tuple[str, ...], source_uri: str, logical_source: str, revision: str, facets: tuple[str, ...], contract: str, license: str = "", compatibility: tuple[str, ...] = ()) -> Capability:
    logical_source = redact_text(logical_source)
    source_uri = redact_uri(source_uri)
    name = redact_text(name)
    summary = redact_text(summary)
    contract = redact_text(contract)
    revision = redact_text(revision)
    license = redact_text(license)
    facets = tuple(redact_text(value) for value in facets)
    compatibility = tuple(redact_text(value) for value in compatibility)
    facets = tuple(sorted(set(facets)))
    observation = {"name": name, "summary": summary, "category_path": category_path, "facets": facets, "contract": contract, "constraints": ("metadata-only discovery",), "artifact_type": artifact_type.value, "logical_source": logical_source, "source_revision": revision, "license": license, "compatibility": compatibility, "lifecycle": Lifecycle.OBSERVED.value, "confidence": 0.3, "expected_net_value": 0.0}
    content_hash = hashlib.sha256(_canonical(observation).encode("utf-8")).hexdigest()
    identifier = hashlib.sha256(f"{artifact_type.value}:{logical_source}".encode("utf-8")).hexdigest()[:32]
    observed_at = _utc_now()
    return redact_capability(Capability(id=identifier, name=name, summary=summary, category_path=category_path, facets=facets, contract=contract, constraints=("metadata-only discovery",), artifact_type=artifact_type, source_uri=source_uri, source_revision=revision, content_hash=content_hash, owner="", license=license, stack=(), runtime=(), platform=(), dependencies=(), compatibility=compatibility, lifecycle=Lifecycle.OBSERVED, confidence=0.3, expected_net_value=0.0, embedding_generation="", created_at=observed_at, updated_at=observed_at, last_verified_at=None))


def _category(name: object, summary: str) -> tuple[str, ...]:
    tokens = set(re.findall(r"\w+", redact_text(f"{_string(name)} {summary}").casefold(), flags=re.UNICODE))
    return ("Code and components", "Identity and access") if tokens & _IDENTITY_TOKENS else _ROOT


def _skill_directories(manifest: dict[str, Any]) -> tuple[str, ...]:
    value = manifest.get("skills")
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def _plugin_identity(manifest: dict[str, Any], parts: tuple[str, ...], source_identity: str) -> str:
    declared = _string(manifest.get("id"))
    if declared:
        return f"declared:{redact_identifier(declared)}"
    base = parts[:-2] if parts[-2:] == (".codex-plugin", "plugin.json") else parts[:-1]
    publisher = base[2] if len(base) > 2 and base[:2] == ("plugins", "cache") else "local"
    return redact_identifier(f"{source_identity}:cache:{publisher}:{_string(manifest.get('name'))}")


def _plugin_revision(manifest: dict[str, Any], parts: tuple[str, ...]) -> str:
    return redact_text(_string(manifest.get("version")) or (parts[-3] if len(parts) >= 3 and parts[-2:] == (".codex-plugin", "plugin.json") else "installed"))


def _version_rank(value: str) -> tuple[Any, ...]:
    match = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", value)
    if match:
        prerelease = match.group(4)
        pre = tuple((0, int(part)) if part.isdigit() else (1, part.casefold()) for part in (prerelease or "").split(".") if part)
        return (1, int(match.group(1)), int(match.group(2) or 0), int(match.group(3) or 0), 1 if prerelease is None else 0, pre, "")
    return (0, 0, 0, 0, 0, (), value.casefold())


def _relative_parts(value: str) -> tuple[str, ...] | None:
    value = value.strip()
    if value.startswith("./"):
        value = value[2:]
    parts = tuple(part for part in value.split("/") if part)
    return parts if parts and all(_component(part) for part in parts) else None


def _component(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and "/" not in value and "\\" not in value and "\x00" not in value


def _normalise_export(value: str) -> str:
    return value[2:] if value.startswith("./") else value


def _contract(exports: tuple[str, ...]) -> str:
    return f"Public exports: {', '.join(exports)}" if exports else ""


def _is_all_assignment(node: ast.Assign | ast.AnnAssign) -> bool:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets)


def _literal_strings(value: ast.AST | None) -> tuple[str, ...]:
    if not isinstance(value, (ast.List, ast.Tuple)):
        return ()
    return tuple(item.value for item in value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str))


def _bounded_yaml(lines: list[str]) -> dict[str, Any] | None:
    """Parse the small, safe YAML subset used by skill frontmatter.

    It deliberately supports mappings, scalar lists, comments, folded text,
    and unknown keys, while rejecting aliases/tags, duplicate keys, deep or
    oversized structures.  A general YAML loader is unnecessary here.
    """
    useful = [(len(line) - len(line.lstrip(" ")), line) for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if len(useful) > 256:
        return None
    nodes = 0

    def scalar(value: str) -> str | None:
        nonlocal nodes
        nodes += 1
        value = value.strip()
        if nodes > 512 or value.startswith(("!", "&", "*", "<<")):
            return None
        quote = value[:1]
        if quote in {"\"", "'"}:
            if len(value) < 2 or value[-1] != quote:
                return None
            value = value[1:-1]
        elif value.startswith(("[", "{")):
            return None
        return value if _valid_metadata_text(value) else None

    def parse_mapping(index: int, indent: int, depth: int) -> tuple[dict[str, Any] | None, int]:
        if depth > 8:
            return None, index
        output: dict[str, Any] = {}
        while index < len(useful):
            current_indent, line = useful[index]
            if current_indent < indent:
                break
            if current_indent != indent or line.lstrip().startswith("- "):
                return None, index
            key, sep, raw = line.strip().partition(":")
            if not sep or not re.fullmatch(r"[A-Za-z0-9_.-]+", key) or key in output:
                return None, index
            index += 1
            raw = raw.strip()
            if raw in {">", "|"}:
                folded: list[str] = []
                while index < len(useful) and useful[index][0] > indent:
                    folded.append(useful[index][1].strip())
                    index += 1
                joined = (" " if raw == ">" else "\n").join(folded)
                value = scalar(joined) if raw == ">" else (joined if joined and len(joined) <= 4096 and "\x00" not in joined else None)
            elif raw:
                value = scalar(raw)
            elif index < len(useful) and useful[index][0] > indent:
                child_indent, child = useful[index]
                if child.lstrip().startswith("- "):
                    value, index = parse_list(index, child_indent, depth + 1)
                else:
                    value, index = parse_mapping(index, child_indent, depth + 1)
            else:
                value = ""
            if value is None:
                return None, index
            output[key] = value
        return output, index

    def parse_list(index: int, indent: int, depth: int) -> tuple[list[str] | None, int]:
        if depth > 8:
            return None, index
        output: list[str] = []
        while index < len(useful):
            current_indent, line = useful[index]
            if current_indent < indent:
                break
            if current_indent != indent or not line.lstrip().startswith("- "):
                return None, index
            value = scalar(line.lstrip()[2:])
            if value is None:
                return None, index
            output.append(value)
            index += 1
        return output, index

    parsed, position = parse_mapping(0, 0, 0)
    if parsed is None or position != len(useful) or not _valid_metadata_text(_string(parsed.get("name"))):
        return None
    return parsed


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None


def _valid_metadata_text(value: str) -> bool:
    return bool(value) and len(value) <= 4096 and "\x00" not in value and "\n" not in value and "\r" not in value


def _summary(description: object, readme: str) -> str:
    return " ".join(redact_text(_string(description) or _string(readme)).split())[:500]


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _uri(root: _SafeRoot, parts: tuple[str, ...]) -> str:
    return root.path.joinpath(*parts).as_uri()


def _unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    found: dict[str, Path] = {}
    for path in paths:
        absolute = path.expanduser().absolute()
        found[str(absolute)] = absolute
    return tuple(found[key] for key in sorted(found))


def _local_source_identity(path: str) -> str:
    """Fallback only for non-persistent, direct scan calls."""
    return "local-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]


def _installed_source_labels(codex_home: Path) -> tuple[str, ...]:
    root = _SafeRoot.open(codex_home)
    if root is None:
        return (str(codex_home.expanduser().absolute()),)
    try:
        labels = []
        if root.is_dir(("plugins", "cache")):
            labels.append(str(root.path / "plugins" / "cache"))
        if root.is_dir(("skills",)):
            labels.append(str(root.path / "skills"))
        return tuple(labels or [str(root.path)])
    finally:
        root.close()


def _dedupe(capabilities: Iterable[Capability]) -> tuple[Capability, ...]:
    found = {capability.id: capability for capability in capabilities}
    return tuple(found[key] for key in sorted(found))


def _git_revision(root: _SafeRoot) -> str | None:
    """Resolve a direct in-root Git ref without reopening a pathname or parent search."""
    if not root.is_dir((".git",)):
        return None
    head = _decode(root.read((".git", "HEAD"), 1024))
    if head is None:
        return None
    value = head.strip()
    if re.fullmatch(r"[0-9a-f]{40,64}", value):
        return value
    if not value.startswith("ref: "):
        return None
    reference = value.removeprefix("ref: ").strip()
    parts = _relative_parts(reference)
    if parts is None or not reference.startswith("refs/"):
        return None
    revision = _decode(root.read((".git", *parts), 1024))
    if revision is not None and re.fullmatch(r"[0-9a-f]{40,64}", revision.strip()):
        return revision.strip()
    packed = _decode(root.read((".git", "packed-refs"), _METADATA_LIMIT))
    if packed is None:
        return None
    for line in packed.splitlines():
        candidate, separator, packed_ref = line.partition(" ")
        if separator and packed_ref == reference and re.fullmatch(r"[0-9a-f]{40,64}", candidate):
            return candidate
    return None


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
