from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import PurePosixPath

from supermind_memory.event_model import AuthorityEvent
from supermind_memory.renderer import render_files
from supermind_memory.replay import replay
from supermind_memory.types import ArtifactType, Capability, Lifecycle


def _event(capability: Capability, event_id: str) -> AuthorityEvent:
    payload = asdict(capability)
    payload["artifact_type"] = capability.artifact_type.value
    payload["lifecycle"] = capability.lifecycle.value
    return AuthorityEvent.create(
        event_id=event_id,
        device_id="device-render",
        entity_type="capability",
        entity_id=capability.id,
        operation="registered",
        parent_event_ids=(),
        occurred_at="2026-09-06T12:00:00Z",
        payload=payload,
    )


def _capability(identifier: str, name: str, lifecycle: Lifecycle) -> Capability:
    return Capability(
        id=identifier,
        name=name,
        summary="统一手机号身份和登录会话。",
        category_path=("Code and components", "Authentication"),
        facets=("identity",),
        contract="Authenticate a user",
        constraints=("Private repository only",),
        artifact_type=ArtifactType.CODE,
        source_uri="https://github.com/example/private-login",
        source_revision="abc123",
        content_hash="a" * 64,
        owner="platform",
        license="MIT",
        stack=("python",),
        runtime=("python",),
        platform=("local",),
        dependencies=(),
        compatibility=("3.12",),
        lifecycle=lifecycle,
        confidence=0.9,
        expected_net_value=12.0,
        embedding_generation="test",
        created_at="2026-09-06T12:00:00Z",
        updated_at="2026-09-06T12:00:00Z",
        last_verified_at="2026-09-06T12:00:00Z",
    )


def test_root_readme_is_nested_foldable_human_catalog():
    login = _capability("login", "登录与身份服务", Lifecycle.RECOMMENDED)
    upload = replace(
        login,
        id="upload",
        name="文件上传",
        lifecycle=Lifecycle.VERIFIED,
        summary="统一安全文件上传。",
    )

    files = render_files(replay((_event(upload, "evt-upload"), _event(login, "evt-login"))))

    readme = files[PurePosixPath("README.md")].decode()
    assert readme.count("<details>") == readme.count("</details>")
    assert "<summary>代码与组件 · 2 项 · 1 项已验证</summary>" in readme
    assert "<summary>登录与身份服务 · 推荐复用 · 当前可用</summary>" in readme
    assert "[打开完整能力卡](capabilities/login.md)" in readme
    assert "vector_score" not in readme


def test_render_files_returns_every_required_repository_view():
    capability = _capability("login:oauth", "OAuth 登录", Lifecycle.VERIFIED)

    files = render_files(replay((_event(capability, "evt-oauth"),)))

    assert set(files) == {
        PurePosixPath("README.md"),
        PurePosixPath("catalog/README.md"),
        PurePosixPath("catalog/code-and-components.md"),
        PurePosixPath("catalog/product-and-business.md"),
        PurePosixPath("catalog/design-and-experience.md"),
        PurePosixPath("catalog/engineering-and-methods.md"),
        PurePosixPath("catalog/tools-and-integrations.md"),
        PurePosixPath("catalog/data-and-intelligence.md"),
        PurePosixPath("capabilities/login%3Aoauth.md"),
        PurePosixPath("demands/open.md"),
        PurePosixPath("demands/resolved.md"),
        PurePosixPath("relationships.md"),
        PurePosixPath(".supermind/render-manifest.json"),
    }
    detail = files[PurePosixPath("capabilities/login%3Aoauth.md")].decode()
    for heading in ("契约", "约束", "验证证据", "复用经济性", "依赖", "替代方案", "使用方", "冲突", "相关需求"):
        assert f"## {heading}" in detail


def test_untrusted_labels_cannot_inject_html_or_mermaid():
    capability = _capability("hostile", '<script>click evil</script> [x] " -->', Lifecycle.CANDIDATE)

    files = render_files(replay((_event(capability, "evt-hostile"),)))

    combined = b"\n".join(files.values()).decode()
    graph = files[PurePosixPath("relationships.md")].decode()
    assert "<script>" not in combined
    assert "click " not in graph
    assert "hostile" not in graph


def test_root_readme_exposes_freshness_demands_recent_changes_and_compact_graph():
    capability = _capability("login", "登录与身份服务", Lifecycle.VERIFIED)

    readme = render_files(replay((_event(capability, "evt-login"),)))[PurePosixPath("README.md")].decode()

    assert "同步状态" in readme
    assert "待解决复用需求" in readme
    assert "最近变化" in readme
    assert "```mermaid" in readme
