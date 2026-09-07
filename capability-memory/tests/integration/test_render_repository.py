from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import PurePosixPath

import pytest

from supermind_memory.event_model import AuthorityEvent
from supermind_memory.renderer import RenderBlocked, render_files, validate_render, write_rendered_repository
from supermind_memory.replay import replay
from supermind_memory.types import ArtifactType, Capability, Lifecycle


def _result(identifier: str | None):
    if identifier is None:
        return replay(())
    capability = Capability(
        abstraction_status="abstracted",
        id=identifier, name=identifier.title(), summary="Reusable module",
        category_path=("code", "Modules"), facets=("module",),
        contract="Provide a module", constraints=(), artifact_type=ArtifactType.CODE,
        source_uri="private source", source_revision="abc", content_hash="a" * 64,
        owner="test", license="MIT", stack=("python",), runtime=("python",),
        platform=("local",), dependencies=(), compatibility=(),
        lifecycle=Lifecycle.VERIFIED, confidence=0.9, expected_net_value=1.0,
        embedding_generation="test", created_at="2026-09-06T12:00:00Z",
        updated_at="2026-09-06T12:00:00Z", last_verified_at="2026-09-06T12:00:00Z",
    )
    payload = asdict(capability)
    payload["artifact_type"] = capability.artifact_type.value
    payload["lifecycle"] = capability.lifecycle.value
    event = AuthorityEvent.create(
        event_id=f"evt-{identifier}", device_id="device-render", entity_type="capability",
        entity_id=identifier, operation="registered", parent_event_ids=(),
        occurred_at="2026-09-06T12:00:00Z", payload=payload,
    )
    return replay((event,))


def test_validate_render_rejects_stale_manifest(tmp_path):
    result = _result("login")
    write_rendered_repository(tmp_path, render_files(result))
    manifest = tmp_path / ".supermind/render-manifest.json"
    manifest.write_text(manifest.read_text().replace("event_set_digest", "stale_digest"))

    with pytest.raises(RenderBlocked, match="render_manifest_invalid"):
        validate_render(tmp_path, result.digest)


def test_writer_deletes_only_obsolete_files_owned_by_valid_prior_manifest(tmp_path):
    first = _result("login")
    write_rendered_repository(tmp_path, render_files(first))
    user_file = tmp_path / "capabilities/notes.md"
    user_file.write_text("keep me\n")

    second = _result(None)
    manifest = write_rendered_repository(tmp_path, render_files(second))

    assert not (tmp_path / "capabilities/login.md").exists()
    assert user_file.read_text() == "keep me\n"
    assert manifest.event_set_digest == second.digest


def test_invalid_prior_manifest_never_authorizes_deletion(tmp_path):
    first = _result("login")
    write_rendered_repository(tmp_path, render_files(first))
    manifest = tmp_path / ".supermind/render-manifest.json"
    manifest.write_text("{}\n")

    write_rendered_repository(tmp_path, render_files(_result(None)))

    assert (tmp_path / "capabilities/login.md").is_file()


def test_prior_manifest_cannot_claim_schema_event_or_user_paths_for_deletion(tmp_path):
    first = _result("login")
    write_rendered_repository(tmp_path, render_files(first))
    protected = {
        "schemas/capability.json": b"schema\n",
        "events/v1/device/2026-09/event.json": b"event\n",
        "notes/private.md": b"user\n",
    }
    manifest_path = tmp_path / ".supermind/render-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, content in protected.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest["files"][relative] = hashlib.sha256(content).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")

    write_rendered_repository(tmp_path, render_files(_result(None)))

    for relative, content in protected.items():
        assert (tmp_path / relative).read_bytes() == content


def test_rendering_is_byte_identical_and_manifest_valid(tmp_path):
    result = _result("login")
    first = render_files(result)
    second = render_files(result)
    assert first == second

    manifest = write_rendered_repository(tmp_path, first)

    assert validate_render(tmp_path, result.digest) == manifest
    assert manifest.files["README.md"]


def test_taxonomy_migration_removes_only_owned_legacy_category_page(tmp_path):
    result = _result("login")
    write_rendered_repository(tmp_path, render_files(result))
    modern = tmp_path / "catalog/code.md"
    legacy = tmp_path / "catalog/code-and-components.md"
    modern.rename(legacy)
    manifest_path = tmp_path / ".supermind/render-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["renderer_version"] = "4"
    manifest["files"]["catalog/code-and-components.md"] = manifest["files"].pop("catalog/code.md")
    manifest_path.write_text(json.dumps(manifest))
    note = tmp_path / "catalog/my-notes.md"
    note.write_text("keep this")
    write_rendered_repository(tmp_path, render_files(result))
    assert modern.is_file()
    assert not legacy.exists()
    assert note.read_text() == "keep this"


def test_invalid_new_manifest_blocks_before_overwriting_repository(tmp_path):
    existing = tmp_path / "README.md"
    existing.write_text("user state\n")
    files = dict(render_files(_result("login")))
    manifest_path = PurePosixPath(".supermind/render-manifest.json")
    document = json.loads(files[manifest_path])
    document["renderer_version"] = "unsupported-version"
    files[manifest_path] = json.dumps(document).encode()

    with pytest.raises(RenderBlocked, match="render_manifest_invalid"):
        write_rendered_repository(tmp_path, files)

    assert existing.read_text() == "user state\n"
