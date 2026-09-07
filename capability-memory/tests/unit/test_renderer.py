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


def test_root_readme_uses_grouped_bilingual_tables():
    login = _capability("login", "登录与身份服务", Lifecycle.RECOMMENDED)
    upload = replace(
        login,
        id="upload",
        name="文件上传",
        lifecycle=Lifecycle.VERIFIED,
        summary="统一安全文件上传。",
        category_path=("Code and components", "Files", "Uploads"),
    )

    files = render_files(replay((_event(upload, "evt-upload"), _event(login, "evt-login"))))

    readme = files[PurePosixPath("README.md")].decode()
    assert "<details>" not in readme
    assert "### 代码（code）" in readme
    assert "[代码（code）]" not in readme
    assert readme.count('<th width="20%">名称</th>') == 2
    assert '<th width="25%">英文标识</th>' in readme
    assert '<th width="10%">状态</th>' in readme
    assert '<th width="45%">说明</th>' in readme
    assert '<td nowrap>待抽象</td>' in readme
    assert '<td>login</td>' in readme
    assert "抽象状态" not in readme
    assert "##### 身份认证（Authentication）" in readme
    assert '<td nowrap><a href="capabilities/login.md">登录与身份服务</a></td>' in readme
    assert "0 项" not in readme.split("## 待解决")[0]
    assert "##### Files / Uploads" in readme
    assert '<td nowrap><a href="capabilities/upload.md">文件上传</a></td>' in readme
    auth_group = readme.split("##### 身份认证（Authentication）", 1)[1].split("#####", 1)[0]
    assert "login.md" in auth_group
    assert "upload.md" not in auth_group
    category = files[PurePosixPath("catalog/code.md")].decode()
    assert '<a href="../capabilities/upload.md">文件上传</a>' in category
    assert "<details>" not in category
    assert "每次复用或适配都必须经人确认" in readme
    assert "具体业务实现仅作为来源" in readme
    assert "vector_score" not in readme


def test_legacy_capability_is_pending_abstraction_with_canonical_category():
    from supermind_memory.projection import authority_snapshot
    from supermind_memory.types import AbstractionStatus
    original = _event(_capability("legacy", "业务登录", Lifecycle.RECOMMENDED), "legacy-event")
    payload = original.to_document()["payload"]
    payload.pop("abstraction_status", None)
    payload["category_path"][0] = "Code and components"
    event = AuthorityEvent.create(event_id="legacy-event", device_id="test", entity_type="capability",
        entity_id="legacy", operation="registered", parent_event_ids=(),
        occurred_at="2026-09-06T12:00:00Z", payload=payload)
    result = replay((event,))
    item = authority_snapshot(result.entities, event_set_digest=result.digest).capabilities[0]
    assert item.category_path[0] == "code"
    assert item.abstraction_status is AbstractionStatus.PENDING
    assert item.lifecycle is Lifecycle.RECOMMENDED
    readme = render_files(result)[PurePosixPath("README.md")].decode()
    assert "代码（code）" in readme
    assert "待抽象来源" in readme
    assert "待抽象" in readme
    assert "可独立接入" in readme
    assert "Code and components" not in readme
    assert PurePosixPath("catalog/code.md") in render_files(result)


def test_render_files_returns_every_required_repository_view():
    capability = _capability("login:oauth", "OAuth 登录", Lifecycle.VERIFIED)

    files = render_files(replay((_event(capability, "evt-oauth"),)))

    assert set(files) == {
        PurePosixPath("README.md"),
        PurePosixPath("catalog/README.md"),
        PurePosixPath("catalog/code.md"),
        PurePosixPath("catalog/product.md"),
        PurePosixPath("catalog/design.md"),
        PurePosixPath("catalog/engineering.md"),
        PurePosixPath("catalog/tools.md"),
        PurePosixPath("catalog/data.md"),
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


def test_root_readme_keeps_internal_digests_in_manifest_not_human_page():
    capability = _capability("login", "登录与身份服务", Lifecycle.VERIFIED)

    result = replay((_event(capability, "evt-login"),))
    files = render_files(result)
    readme = files[PurePosixPath("README.md")].decode()

    assert "权威事件集" not in readme
    assert result.digest not in readme
    assert result.digest in files[PurePosixPath(".supermind/render-manifest.json")].decode()
    assert "2026-09-06T12:00:00Z" not in readme
    assert "2026-09-06" in readme
    assert "待解决复用需求" in readme
    assert "最近变化" in readme
    recent = readme.split("## 最近变化", 1)[1]
    assert "- [登录与身份服务](capabilities/login.md)" in recent
    assert "|" not in recent
    assert "关系概览" not in readme
    assert "```mermaid" not in readme
    assert PurePosixPath("relationships.md") in files
