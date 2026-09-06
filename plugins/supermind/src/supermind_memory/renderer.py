"""Deterministic GitHub-native rendering of an authoritative event replay."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote

from supermind_memory.event_model import canonical_json
from supermind_memory.explorer import escape_markdown, escape_mermaid_label, stable_mermaid_node_id
from supermind_memory.projection import AuthoritySnapshot, authority_snapshot
from supermind_memory.replay import ReplayResult
from supermind_memory.taxonomy import TOP_LEVEL_CATEGORIES
from supermind_memory.types import Capability, Evidence, Lifecycle, Relationship

RENDERER_VERSION = "1"
MANIFEST_PATH = PurePosixPath(".supermind/render-manifest.json")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CATEGORIES = {
    "Code and components": ("代码与组件", "code-and-components", "用于直接复用到产品实现中的模块、组件和库。"),
    "Product and business": ("产品与业务", "product-and-business", "沉淀可重复使用的产品机制、业务规则和领域方案。"),
    "Design and experience": ("设计与体验", "design-and-experience", "复用经过验证的交互、视觉和用户体验方案。"),
    "Engineering and methods": ("工程与方法", "engineering-and-methods", "复用工程实践、交付流程和质量保障方法。"),
    "Tools and integrations": ("工具与集成", "tools-and-integrations", "连接可复用的工具、服务和系统集成。"),
    "Data and intelligence": ("数据与智能", "data-and-intelligence", "复用数据资产、检索能力和智能化组件。"),
}
LIFECYCLE_LABEL = {
    Lifecycle.OBSERVED: "已发现", Lifecycle.CANDIDATE: "候选",
    Lifecycle.VERIFIED: "已验证", Lifecycle.RECOMMENDED: "推荐复用",
    Lifecycle.DEGRADED: "需维护", Lifecycle.RETIRED: "已退役",
}
_FIXED_RENDER_PATHS = frozenset({
    PurePosixPath("README.md"),
    PurePosixPath("catalog/README.md"),
    *(PurePosixPath(f"catalog/{value[1]}.md") for value in CATEGORIES.values()),
    PurePosixPath("demands/open.md"),
    PurePosixPath("demands/resolved.md"),
    PurePosixPath("relationships.md"),
})


class RenderBlocked(RuntimeError):
    def __init__(self, code: str, details: Sequence[str] = ()) -> None:
        self.code, self.details = code, tuple(details)
        super().__init__(code + (f": {self.details[-1]}" if self.details else ""))


@dataclass(frozen=True)
class RenderManifest:
    renderer_version: str
    event_set_digest: str
    files: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", dict(sorted(self.files.items())))

    def to_document(self) -> dict[str, object]:
        return {"event_set_digest": self.event_set_digest, "files": dict(self.files),
                "renderer_version": self.renderer_version}

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_document()) + b"\n"


class RepositoryRenderer:
    """Production render port used by the synchronization transaction."""

    def render(self, result: ReplayResult, checkout: Path) -> None:
        write_rendered_repository(checkout, render_files(result))
        validate_render(checkout, result.digest)


def render_files(result: ReplayResult) -> Mapping[PurePosixPath, bytes]:
    """Return the exact derived repository file map for a conflict-free replay."""
    if result.conflicts:
        raise RenderBlocked("authority_conflict", [f"{x.entity_type}/{x.entity_id}" for x in result.conflicts])
    try:
        snapshot = authority_snapshot(
            result.entities, conflicts=result.conflicts,
            diagnostic_ancestors=result.diagnostic_ancestors, event_set_digest=result.digest,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RenderBlocked("authority_render_invalid", (str(error),)) from error
    files = _render_snapshot(snapshot, result.digest)
    hashes = {path.as_posix(): hashlib.sha256(data).hexdigest()
              for path, data in sorted(files.items(), key=lambda pair: pair[0].as_posix())}
    files[MANIFEST_PATH] = RenderManifest(RENDERER_VERSION, result.digest, hashes).to_bytes()
    return dict(sorted(files.items(), key=lambda pair: pair[0].as_posix()))


def write_rendered_repository(root: Path, files: Mapping[PurePosixPath, bytes]) -> RenderManifest:
    """Write generated files and remove only output owned by a prior valid manifest."""
    root = Path(root)
    normalized = _normalize_files(files)
    manifest = _manifest_from_bytes(normalized[MANIFEST_PATH])
    expected = {path.as_posix(): hashlib.sha256(data).hexdigest()
                for path, data in normalized.items() if path != MANIFEST_PATH}
    if (manifest.renderer_version != RENDERER_VERSION
            or not _SHA256.fullmatch(manifest.event_set_digest)
            or dict(manifest.files) != dict(sorted(expected.items()))):
        raise RenderBlocked("render_manifest_invalid")
    prior = _read_valid_manifest(root)
    for path in normalized:
        _safe_target(root, path)
    ordered = sorted((path for path in normalized if path != MANIFEST_PATH), key=lambda x: x.as_posix())
    for path in (*ordered, MANIFEST_PATH):
        data = normalized[path]
        _atomic_write(root, path, data)
    if prior is not None:
        for relative in sorted(set(prior.files) - set(manifest.files)):
            target = _safe_target(root, PurePosixPath(relative))
            if target.is_file():
                target.unlink()
    return validate_render(root, manifest.event_set_digest)


def validate_render(root: Path, event_set_digest: str) -> RenderManifest:
    """Require current renderer, event digest, and exact manifest-bound file hashes."""
    try:
        path = _safe_target(Path(root), MANIFEST_PATH)
        manifest = _manifest_from_bytes(path.read_bytes())
    except (OSError, RenderBlocked) as error:
        raise RenderBlocked("render_manifest_invalid", (str(error),)) from error
    if (manifest.renderer_version != RENDERER_VERSION
            or manifest.event_set_digest != event_set_digest
            or not _manifest_files_match(Path(root), manifest)):
        raise RenderBlocked("render_manifest_invalid")
    return manifest


def _render_snapshot(snapshot: AuthoritySnapshot, digest: str) -> dict[PurePosixPath, bytes]:
    capabilities = tuple(sorted(snapshot.capabilities, key=lambda x: (x.name.casefold(), x.name, x.id)))
    evidence: dict[str, list[Evidence]] = {}
    for item in snapshot.evidence:
        evidence.setdefault(item.capability_id, []).append(item)
    relationships = tuple(sorted(snapshot.relationships,
        key=lambda x: (x.relationship_type.casefold(), x.source_id, x.target_id, x.id)))
    demands = tuple(sorted(snapshot.requirement_observations, key=lambda x: x.id))
    files: dict[PurePosixPath, bytes] = {
        PurePosixPath("README.md"): _root_readme(capabilities, evidence, relationships, demands, digest),
        PurePosixPath("catalog/README.md"): _catalog_readme(capabilities),
        PurePosixPath("demands/open.md"): _demand_page(
            "Open reuse demands", tuple(x for x in demands if x.status.casefold() not in {"resolved", "linked"})),
        PurePosixPath("demands/resolved.md"): _demand_page(
            "Resolved reuse demands", tuple(x for x in demands if x.status.casefold() in {"resolved", "linked"})),
        PurePosixPath("relationships.md"): _relationships_page(capabilities, relationships, demands),
    }
    for category in TOP_LEVEL_CATEGORIES:
        _, slug, _ = CATEGORIES[category]
        members = tuple(x for x in capabilities if x.category_path[0] == category)
        files[PurePosixPath(f"catalog/{slug}.md")] = _category_page(category, members, evidence)
    for capability in capabilities:
        files[_capability_path(capability.id)] = _capability_page(
            capability, tuple(sorted(evidence.get(capability.id, ()), key=lambda x: x.id)),
            relationships, demands)
    return files


def _root_readme(
    capabilities: tuple[Capability, ...], evidence: Mapping[str, list[Evidence]],
    relationships: tuple[Relationship, ...], demands: tuple[Any, ...], digest: str,
) -> bytes:
    open_demands = tuple(x for x in demands if x.status.casefold() not in {"resolved", "linked"})
    lines = ["# Supermind Capability Memory", "",
             "按价值与复用依据组织的能力库。事件记录是唯一权威来源；本页由事件自动生成。", "",
             f"- 权威事件集：`{digest}`",
             "- 同步状态：当前页面与本提交的权威事件集一致",
             "- [完整目录](catalog/README.md) · [待解决需求](demands/open.md) · [关系图](relationships.md)", ""]
    for category in TOP_LEVEL_CATEGORIES:
        label, slug, value = CATEGORIES[category]
        members = tuple(x for x in capabilities if x.category_path[0] == category)
        verified = sum(x.lifecycle is Lifecycle.VERIFIED for x in members)
        lines += ["<details>", f"<summary>{label} · {len(members)} 项 · {verified} 项已验证</summary>",
                  "", value, "", f"[查看分类页](catalog/{slug}.md)", ""]
        for item in members:
            lines += _capability_fold(item, evidence.get(item.id, ()), "")
        lines += ["</details>", ""]
    lines += ["## 待解决复用需求", "", f"当前有 {len(open_demands)} 项未解决需求。", "",
              "[查看待解决需求](demands/open.md)", "", "## 最近变化", ""]
    recent = sorted(capabilities, key=lambda x: (x.updated_at, x.id), reverse=True)[:5]
    lines += ([f"- {escape_markdown(x.name)}：{LIFECYCLE_LABEL[x.lifecycle]}（{escape_markdown(x.updated_at)}）" for x in recent]
              or ["- 暂无变化。"])
    lines += ["", "## 关系概览", "", "```mermaid", "flowchart LR"]
    related_ids = {identifier for relation in relationships for identifier in (relation.source_id, relation.target_id)}
    for item in capabilities:
        if item.id in related_ids:
            lines.append(f'{stable_mermaid_node_id(item.id)}["{escape_mermaid_label(_bounded(item.name))}"]')
    for relation in relationships:
        lines.append(f"{stable_mermaid_node_id(relation.source_id)} --> {stable_mermaid_node_id(relation.target_id)}")
    if not relationships:
        lines.append('empty["暂无已声明关系"]')
    lines.append("```")
    return _document(lines)


def _catalog_readme(capabilities: tuple[Capability, ...]) -> bytes:
    lines = ["# 完整能力目录", "", "按六个稳定大类浏览全部能力。", ""]
    for category in TOP_LEVEL_CATEGORIES:
        label, slug, value = CATEGORIES[category]
        count = sum(x.category_path[0] == category for x in capabilities)
        lines.append(f"- [{label} · {count} 项]({slug}.md)：{value}")
    return _document(lines)


def _category_page(category: str, capabilities: tuple[Capability, ...], evidence: Mapping[str, list[Evidence]]) -> bytes:
    label, _, value = CATEGORIES[category]
    lines = [f"# {label}", "", value, "", "[返回总览](../README.md)", ""]
    for item in capabilities:
        lines += _capability_fold(item, evidence.get(item.id, ()), "../")
    if not capabilities:
        lines.append("暂无沉淀能力。")
    return _document(lines)


def _capability_fold(item: Capability, evidence: Sequence[Evidence], prefix: str) -> list[str]:
    availability = "不可用" if item.lifecycle in {Lifecycle.DEGRADED, Lifecycle.RETIRED} else "当前可用"
    reuse_count = sum(x.evidence_type.casefold() in {"reuse", "reuse_outcome", "reused"} for x in evidence)
    return ["<details>",
            f"<summary>{escape_markdown(item.name)} · {LIFECYCLE_LABEL[item.lifecycle]} · {availability}</summary>", "",
            f"- 解决什么：{escape_markdown(item.summary)}", f"- 适合什么：{escape_markdown(item.contract)}",
            f"- 复用依据：{len(evidence)} 条证据，{reuse_count} 次复用记录。",
            f"- 来源：{_source_description(item)}",
            f"- [打开完整能力卡]({prefix}{_capability_path(item.id).as_posix()})", "", "</details>", ""]


def _capability_page(item: Capability, evidence: tuple[Evidence, ...], relationships: tuple[Relationship, ...], demands: tuple[Any, ...]) -> bytes:
    related = tuple(x for x in relationships if item.id in {x.source_id, x.target_id})
    dependency_types = {"dependency", "depends_on", "depends-on"}
    alternative_types = {"alternative", "alternative_to", "alternative-to"}
    consumer_types = {"consumer", "consumed_by", "consumed-by", *dependency_types}
    dependencies = tuple(x.target_id for x in related if x.source_id == item.id and x.relationship_type.casefold() in dependency_types)
    alternatives = tuple(x.target_id if x.source_id == item.id else x.source_id for x in related if x.relationship_type.casefold() in alternative_types)
    consumers = tuple(x.source_id for x in related if x.target_id == item.id and x.relationship_type.casefold() in consumer_types)
    related_demands = tuple(x.requirement.intent for x in demands if x.linked_capability_id == item.id)
    availability = "不可用" if item.lifecycle in {Lifecycle.DEGRADED, Lifecycle.RETIRED} else "当前可用"
    lines = [f"# {escape_markdown(item.name)}", "", escape_markdown(item.summary), "",
             "## 状态与来源", "", f"- 生命周期：{LIFECYCLE_LABEL[item.lifecycle]}", f"- 可用性：{availability}",
             f"- 类型：{escape_markdown(item.artifact_type.value)}", f"- 来源：{_source_description(item)}",
             f"- 来源版本：{escape_markdown(item.source_revision)}", "", "## 契约", "", escape_markdown(item.contract), "",
             "## 约束", "", *_bullets(item.constraints), "", "## 验证证据", "", *_evidence_lines(evidence), "",
             "## 复用经济性", "", f"- 预期净价值：{item.expected_net_value:.2f}",
             f"- 复用记录：{sum(x.evidence_type.casefold() in {'reuse', 'reuse_outcome', 'reused'} for x in evidence)} 次", "",
             "## 依赖", "", *_bullets(dependencies), "", "## 替代方案", "", *_bullets(alternatives), "",
             "## 使用方", "", *_bullets(consumers), "", "## 冲突", "", "- 无未解决冲突。", "",
             "## 相关需求", "", *_bullets(related_demands), "", "[返回能力库](../README.md)"]
    return _document(lines)


def _demand_page(title: str, demands: tuple[Any, ...]) -> bytes:
    lines = [f"# {title}", ""]
    if not demands:
        lines.append("暂无记录。")
    for item in demands:
        lines += [f"## {escape_markdown(item.requirement.intent)}", "", f"- 状态：{escape_markdown(item.status)}",
                  f"- 契约：{escape_markdown(item.requirement.contract or '—')}",
                  f"- 关联能力：{escape_markdown(item.linked_capability_id or '—')}", ""]
    return _document(lines)


def _relationships_page(capabilities: tuple[Capability, ...], relationships: tuple[Relationship, ...], demands: tuple[Any, ...]) -> bytes:
    capability_map = {x.id: x for x in capabilities}
    node_keys = set(capability_map)
    for relation in relationships:
        node_keys.update((relation.source_id, relation.target_id))
    lines = ["# 能力关系", "", "```mermaid", "flowchart LR"]
    for identifier in sorted(node_keys):
        label = capability_map[identifier].name if identifier in capability_map else identifier
        lines.append(f'{stable_mermaid_node_id(identifier)}["{escape_mermaid_label(_bounded(label))}"]')
    for demand in demands:
        key = f"demand:{demand.id}"
        node = stable_mermaid_node_id(key, prefix="demand")
        lines.append(f'{node}["{escape_mermaid_label(_bounded(demand.requirement.intent))}"]')
        if demand.linked_capability_id:
            lines.append(f"{node} -.->|implements| {stable_mermaid_node_id(demand.linked_capability_id)}")
    styles = {"dependency": ("-->", "depends on"), "depends_on": ("-->", "depends on"),
              "depends-on": ("-->", "depends on"), "alternative": ("-.->", "alternative"),
              "alternative_to": ("-.->", "alternative"), "alternative-to": ("-.->", "alternative"),
              "composition": ("==>", "composes"), "composed_of": ("==>", "composes"),
              "composed-of": ("==>", "composes"), "consumer": ("-->", "consumed by"),
              "consumed_by": ("-->", "consumed by"), "consumed-by": ("-->", "consumed by")}
    for relation in relationships:
        edge, label = styles.get(relation.relationship_type.casefold(), ("-->", "related"))
        lines.append(f"{stable_mermaid_node_id(relation.source_id)} {edge}|{label}| {stable_mermaid_node_id(relation.target_id)}")
    return _document([*lines, "```"])


def _normalize_files(files: Mapping[PurePosixPath, bytes]) -> dict[PurePosixPath, bytes]:
    result = {}
    for raw, data in files.items():
        path = PurePosixPath(raw)
        if path.is_absolute() or not path.parts or ".." in path.parts or type(data) is not bytes:
            raise RenderBlocked("render_path_invalid", (str(raw),))
        result[path] = data
    if MANIFEST_PATH not in result:
        raise RenderBlocked("render_manifest_invalid")
    return result


def _manifest_from_bytes(raw: bytes) -> RenderManifest:
    try:
        document = json.loads(raw.decode("utf-8"))
        if type(document) is not dict or set(document) != {"renderer_version", "event_set_digest", "files"}:
            raise ValueError("unexpected fields")
        if (type(document["renderer_version"]) is not str
                or type(document["event_set_digest"]) is not str
                or not _SHA256.fullmatch(document["event_set_digest"])
                or type(document["files"]) is not dict):
            raise ValueError("invalid field types")
        for relative, digest in document["files"].items():
            path = PurePosixPath(relative)
            if (type(relative) is not str or type(digest) is not str
                    or not _SHA256.fullmatch(digest) or path.is_absolute()
                    or ".." in path.parts or not _is_renderer_owned_output(path)):
                raise ValueError("invalid file entry")
        return RenderManifest(document["renderer_version"], document["event_set_digest"], document["files"])
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RenderBlocked("render_manifest_invalid", (str(error),)) from error


def _read_valid_manifest(root: Path) -> RenderManifest | None:
    path = root / MANIFEST_PATH
    if not path.is_file() or path.is_symlink():
        return None
    try:
        manifest = _manifest_from_bytes(path.read_bytes())
        return manifest if (manifest.renderer_version == RENDERER_VERSION
                            and _manifest_files_match(root, manifest)) else None
    except (OSError, RenderBlocked):
        return None


def _manifest_files_match(root: Path, manifest: RenderManifest) -> bool:
    for relative, digest in manifest.files.items():
        try:
            target = _safe_target(root, PurePosixPath(relative))
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                return False
        except (OSError, RenderBlocked):
            return False
    return True


def _safe_target(root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RenderBlocked("render_path_invalid", (relative.as_posix(),))
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise RenderBlocked("render_path_unsafe", (relative.as_posix(),))
    target = root.joinpath(*relative.parts)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise RenderBlocked("render_path_unsafe", (relative.as_posix(),))
    return target


def _atomic_write(root: Path, relative: PurePosixPath, data: bytes) -> None:
    target = _safe_target(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _capability_path(identifier: str) -> PurePosixPath:
    return PurePosixPath("capabilities", quote(identifier, safe="-._~") + ".md")


def _is_renderer_owned_output(path: PurePosixPath) -> bool:
    if path in _FIXED_RENDER_PATHS:
        return True
    if len(path.parts) != 2 or path.parts[0] != "capabilities":
        return False
    filename = path.parts[1]
    if not filename.endswith(".md") or filename == ".md":
        return False
    encoded = filename[:-3]
    return quote(unquote(encoded), safe="-._~") == encoded


def _source_description(item: Capability) -> str:
    kind = {"code": "代码实现", "service": "服务实现", "api": "接口能力"}.get(item.artifact_type.value, "受控能力来源")
    return f"{kind}（{escape_markdown(item.owner)}）"


def _bounded(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")[:160]


def _bullets(values: Sequence[str]) -> list[str]:
    return [f"- {escape_markdown(value)}" for value in values] or ["- 无。"]


def _evidence_lines(values: Sequence[Evidence]) -> list[str]:
    return ([f"- {escape_markdown(x.evidence_type)}：{escape_markdown(x.outcome)}（{escape_markdown(x.source_project)}）" for x in values]
            or ["- 暂无验证证据。"])


def _document(lines: Sequence[str]) -> bytes:
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")
