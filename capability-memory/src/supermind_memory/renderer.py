"""Deterministic GitHub-native rendering of an authoritative event replay."""

from __future__ import annotations

import hashlib
from html import escape as escape_html
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from supermind_memory.event_model import canonical_json
from supermind_memory.explorer import escape_markdown, escape_mermaid_label, stable_mermaid_node_id
from supermind_memory.projection import AuthoritySnapshot, authority_snapshot
from supermind_memory.replay import ReplayResult
from supermind_memory.taxonomy import TOP_LEVEL_CATEGORIES
from supermind_memory.types import AbstractionStatus, Capability, Evidence, Lifecycle, Relationship

RENDERER_VERSION = "12"
MANIFEST_PATH = PurePosixPath(".supermind/render-manifest.json")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CATEGORIES = {
    "code": ("代码", "code", "收录可独立接入其他项目的模块、组件和库，例如认证、文件上传和消息通知。复用的是通用接口与实现，不是整段原业务。"),
    "product": ("产品", "product", "收录可跨产品使用的机制和规则，例如审批、订阅和权限模型。需要分离具体客户、业务流程和运营配置。"),
    "design": ("设计", "design", "收录交互模式、视觉规范和设计模板，例如表单校验与列表详情布局。需能适应不同产品内容和品牌。"),
    "engineering": ("工程", "engineering", "收录开发、测试、发布和运维方法，例如契约测试与回滚流程。需说明适用条件，避免依赖某个项目的路径或环境。"),
    "tools": ("工具", "tools", "收录经过评估的自动化工具和系统连接能力，例如数据导入与服务适配。安装过某个插件不等于沉淀了可复用能力。"),
    "data": ("数据", "data", "收录数据处理、检索和模型应用能力，例如清洗流程与语义检索。需要明确输入输出、数据权限和质量要求。"),
}
SUBCATEGORY_LABELS = {
    "AI orchestration": "智能编排", "Capability memory": "能力管理",
    "Authentication": "身份认证", "Client adapters": "客户端适配",
    "Phone OTP and sessions": "短信登录", "Codex Skills": "Codex 技能",
    "Plugins and MCP": "插件与 MCP",
}
_FIXED_RENDER_PATHS = frozenset({
    *(PurePosixPath(f"catalog/{slug}.md") for slug in ("code-and-components", "product-and-business",
      "design-and-experience", "engineering-and-methods", "tools-and-integrations", "data-and-intelligence")),
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
    visible = tuple(item for item in capabilities if item.abstraction_status is not AbstractionStatus.NOT_EXTRACTING and item.lifecycle is not Lifecycle.RETIRED)
    files: dict[PurePosixPath, bytes] = {
        PurePosixPath("README.md"): _root_readme(visible, evidence, relationships, demands, digest),
        PurePosixPath("catalog/README.md"): _catalog_readme(visible),
        PurePosixPath("demands/open.md"): _demand_page(
            "Open reuse demands", tuple(x for x in demands if x.status.casefold() not in {"resolved", "linked"})),
        PurePosixPath("demands/resolved.md"): _demand_page(
            "Resolved reuse demands", tuple(x for x in demands if x.status.casefold() in {"resolved", "linked"})),
        PurePosixPath("relationships.md"): _relationships_page(capabilities, relationships, demands),
    }
    for category in TOP_LEVEL_CATEGORIES:
        _, slug, _ = CATEGORIES[category]
        members = tuple(x for x in visible if x.category_path[0] == category)
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
    lines = ["# Supermind 能力库", "",
             "Supermind 是协助你开发和改进软件的 AI 工具。这个仓库是它的能力库："
             "记录开发过程中值得保留的实现和方法，帮助后续项目减少重复工作。", "",
             "Supermind 会自动发现和评估能力。你可以在下面按用途浏览，点击名称查看说明、来源和验证记录，"
             "也可以直接让 Supermind 查询、修改或清理条目。内容保存在本地，并同步到这个私有 GitHub 仓库。", "",
             "每次复用或适配都必须经人确认。", "",
             f"## 能力（{len(capabilities)} 项）", ""]
    for category in TOP_LEVEL_CATEGORIES:
        label, slug, value = CATEGORIES[category]
        members = tuple(x for x in capabilities if x.category_path[0] == category)
        if not members:
            continue
        lines += [f"### {label}（{slug}）", "", value, ""]
        lines += _capability_table(members)
    lines.append("")
    lines.append("")
    lines += ["## 待解决复用需求", "", f"当前有 {len(open_demands)} 项未解决需求。", "",
              "[查看待解决需求](demands/open.md)", "", "## 最近变化", ""]
    recent = sorted(capabilities, key=lambda x: (x.updated_at, x.id), reverse=True)[:5]
    if recent:
        lines += [f"- [{escape_markdown(x.name)}]({_capability_path(x.id)})（{escape_markdown(x.updated_at.split('T')[0])}）" for x in recent]
    else:
        lines.append("暂无变化。")
    return _document(lines)


def _capability_table(capabilities: tuple[Capability, ...], prefix: str = "") -> list[str]:
    """Group two-column name and description tables by subcategory."""
    lines = []
    groups: dict[tuple[str, ...], list[Capability]] = {}
    for item in capabilities:
        groups.setdefault(item.category_path[1:], []).append(item)
    for path, members in sorted(groups.items()):
        categories = []
        for group in path:
            label = SUBCATEGORY_LABELS.get(group, group)
            categories.append(f"{label}（{group}）" if label != group else group)
        title = " / ".join(categories) or "未分类（uncategorized）"
        lines += [f"##### {escape_markdown(title)}", "", '<table width="100%">',
                  '<thead><tr><th width="35%">名称</th><th width="65%">说明</th></tr></thead>', '<tbody>']
        for item in members:
            lines += ['<tr>',
                      f'<td nowrap><a href="{escape_html(prefix + str(_capability_path(item.id)))}">{_html_text(item.name)}</a></td>',
                      f'<td><sub>{_html_text(item.summary)}</sub></td>', '</tr>']
        lines += ['</tbody></table>', '']
    return lines


def _catalog_readme(capabilities: tuple[Capability, ...]) -> bytes:
    lines = ["# 完整能力目录", "", "按六个稳定大类浏览全部能力。", ""]
    for category in TOP_LEVEL_CATEGORIES:
        label, slug, value = CATEGORIES[category]
        count = sum(x.category_path[0] == category for x in capabilities)
        lines += [f"## [{label}（{slug}）]({slug}.md)", "", value, "", f"当前收录 {count} 项。", ""]
    return _document(lines)


def _category_page(category: str, capabilities: tuple[Capability, ...], evidence: Mapping[str, list[Evidence]]) -> bytes:
    label, slug, value = CATEGORIES[category]
    lines = [f"# {label}（{slug}）", "", value, "", "[返回总览](../README.md)", ""]
    if capabilities:
        lines += _capability_table(capabilities, "../")
    if not capabilities:
        lines.append("暂无沉淀能力。")
    return _document(lines)


def _contract_body(value: str) -> str:
    """Preserve document structure while keeping arbitrary HTML inert."""
    lines = []
    in_code = False
    for line in value.splitlines():
        if re.fullmatch(r"```[a-zA-Z0-9_-]*", line):
            lines.append(line)
            in_code = not in_code
            continue
        if in_code:
            lines.append(line)
            continue
        if line.startswith("# "):
            continue
        link = re.fullmatch(r"\[([^\[\]]+)\]\((https?://[^\s)]+)\)", line)
        if link:
            label, target = link.groups()
            try:
                parsed = urlsplit(target)
                safe = parsed.hostname and not parsed.username and not parsed.password
            except ValueError:
                safe = False
            if safe:
                lines.append(f"[{escape_markdown(label)}]({quote(target, safe=':/?=&%#-._~')})")
                continue
        lines.append(escape_markdown(line))
    if in_code:
        lines.append("```")
    return "\n".join(lines).strip()


def _capability_page(item: Capability, evidence: tuple[Evidence, ...], relationships: tuple[Relationship, ...], demands: tuple[Any, ...]) -> bytes:
    related = tuple(x for x in relationships if item.id in {x.source_id, x.target_id})
    dependency_types = {"dependency", "depends_on", "depends-on"}
    dependencies = tuple(x.target_id for x in related if x.source_id == item.id and x.relationship_type.casefold() in dependency_types)
    body = _contract_body(item.contract)
    lines = [f"# {escape_markdown(item.name)}", "", escape_markdown(item.summary), ""]
    if item.source_uri.startswith(("https://", "http://")):
        lines += [_source_description(item), ""]
    if body:
        if not re.search(r"^## ", body, re.MULTILINE):
            lines += ["## 使用说明", ""]
        lines += [body, ""]
    for heading, values in (("使用条件", item.constraints),
                            ("依赖", tuple(dict.fromkeys((*item.dependencies, *dependencies))))):
        if values:
            lines += [f"## {heading}", "", *_bullets(values), ""]
    lines += ["[返回能力库](../README.md)"]
    return _document(lines)


def _demand_page(title: str, demands: tuple[Any, ...]) -> bytes:
    lines = [f"# {title}", ""]
    if not demands:
        lines.append("暂无记录。")
    for item in demands:
        lines += [f"## {escape_markdown(item.requirement.intent)}", "",
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
        return manifest if (manifest.renderer_version in {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", RENDERER_VERSION}
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


def _html_text(value: str) -> str:
    text = escape_html(value).replace("://", ":&#47;&#47;")
    for char in "[]`":
        text = text.replace(char, f"&#{ord(char)};")
    return text


def _source_description(item: Capability) -> str:
    kind = {"code": "代码实现", "service": "服务实现", "api": "接口能力"}.get(item.artifact_type.value, "受控能力来源")
    source = urlsplit(item.source_uri)
    if source.scheme in {"https", "http"} and source.hostname and not source.username and not source.password:
        url = quote(item.source_uri, safe=":/?=&%#-._~")
        return f"[官方项目]({url})"
    return f"{kind}（{escape_markdown(item.owner)}）"


def _bounded(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")[:160]


def _bullets(values: Sequence[str]) -> list[str]:
    return [f"- {escape_markdown(value)}" for value in values] or ["- 无。"]


def _evidence_lines(values: Sequence[Evidence]) -> list[str]:
    lines = []
    for item in values:
        metric = f"；{item.metric_name}: {item.metric_value}" if item.metric_name else ""
        lines.append(f"- {escape_markdown(item.evidence_type)}：{escape_markdown(item.outcome + metric)}")
    return lines


def _document(lines: Sequence[str]) -> bytes:
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")
