"""Integration coverage for bounded capability discovery.

Each test protects the scanner from broadening its authority: a regression that
walks arbitrary directories, follows an escaping symlink, or treats malformed
metadata as trustworthy must make one of these tests fail.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from supermind_memory.discovery import CapabilityDiscovery, SourceRegistry
from supermind_memory.repository import CapabilityRepository
from supermind_memory.types import ArtifactType, DiscoveryContext, Lifecycle


@pytest.fixture
def discovery() -> CapabilityDiscovery:
    return CapabilityDiscovery(SourceRegistry())


def make_project(
    root: Path,
    *,
    readme: str | None = None,
    package_name: str | None = None,
) -> Path:
    root.mkdir(parents=True)
    if readme is not None:
        (root / "README.md").write_text(readme, encoding="utf-8")
    if package_name is not None:
        (root / "package.json").write_text(
            json.dumps({"name": package_name}),
            encoding="utf-8",
        )
    return root


def make_installed_skill(codex_home: Path, *, name: str) -> Path:
    plugin = codex_home / "plugins" / "cache" / "example-plugin" / "1.0.0"
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "example-plugin", "skills": "./skills"}), encoding="utf-8")
    skill = plugin / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: {name}\ndescription: Safe helper\n---\n\nBody is not indexed.\n", encoding="utf-8")
    return codex_home


def serialize(capabilities) -> str:
    return "\n".join(
        " ".join((item.name, item.summary, item.source_uri, *item.facets, *item.category_path))
        for item in capabilities
    )


def test_discovered_secret_identity_and_metadata_are_sanitized_before_hashing(tmp_path, discovery):
    project = make_project(tmp_path / "structured-secrets")
    manifest = project / "package.json"
    observations = []
    for secret in ("first-credential", "second-credential"):
        manifest.write_text(json.dumps({
            "name": f"login:client_secret={secret}",
            "description": f'login {{"password":"{secret}"}} https://host.test/#access_token={secret}',
        }), encoding="utf-8")
        observations.append(discovery.scan_active_project(project)[0])
    first, second = observations
    assert first.id == second.id
    assert first.content_hash == second.content_hash
    assert "credential" not in repr([asdict(item) for item in observations])


def test_discovery_sanitizes_before_summary_truncation_and_category_derivation(tmp_path, discovery):
    project = make_project(tmp_path / "long-structured-secret")
    manifest = project / "package.json"
    observed = []
    for value in ("login ", "plain "):
        manifest.write_text(json.dumps({
            "name": "generic-kit",
            "description": 'helper {"password":"' + value * 150 + '"}',
        }), encoding="utf-8")
        observed.append(discovery.scan_active_project(project)[0])
    assert observed[0].summary == observed[1].summary == 'helper {"password":"[REDACTED]"}'
    assert observed[0].category_path == observed[1].category_path == ("Code and components",)
    assert observed[0].content_hash == observed[1].content_hash


def test_plugin_identity_is_sanitized_before_deriving_distinct_skill_ids(tmp_path, discovery):
    codex_home = make_installed_skill(tmp_path / "codex", name="first")
    plugin = codex_home / "plugins" / "cache" / "example-plugin" / "1.0.0"
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.write_text(json.dumps({
        "id": "plugin:client_secret=identity-credential", "name": "example-plugin", "skills": "./skills",
    }), encoding="utf-8")
    second = plugin / "skills" / "second" / "SKILL.md"
    second.parent.mkdir()
    second.write_text("---\nname: second\ndescription: Safe helper\n---\n", encoding="utf-8")
    records = discovery.scan_installed_plugins(codex_home)
    assert {item.name for item in records} == {"example-plugin", "first", "second"}
    assert len({item.id for item in records}) == 3
    assert "identity-credential" not in repr(records)


def test_secret_revision_rotation_cannot_change_selected_plugin(tmp_path, discovery):
    codex_home = tmp_path / "codex"
    manifests = []
    for cache_name in ("copy-a", "copy-b"):
        manifest = codex_home / "plugins" / "cache" / cache_name / "1.0.0" / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifests.append(manifest)

    def write_versions(versions):
        for manifest, version in zip(manifests, versions, strict=True):
            manifest.write_text(json.dumps({
                "id": "stable-plugin", "name": "stable-plugin", "version": version,
            }), encoding="utf-8")

    write_versions(("token=zz-revision-credential", "token=aa-revision-credential"))
    first = discovery.scan_installed_plugins(codex_home)[0]
    write_versions(("token=aa-revision-credential", "token=zz-revision-credential"))
    second = discovery.scan_installed_plugins(codex_home)[0]
    assert first.source_revision == second.source_revision == "token=[REDACTED]"
    assert first.source_uri == second.source_uri
    assert first.id == second.id
    assert first.content_hash == second.content_hash


def test_discovery_reads_active_project_and_installed_skill_metadata(tmp_path, discovery):
    """Removing either allowed metadata reader loses a reusable capability."""
    project = make_project(tmp_path / "active", readme="OAuth login module", package_name="auth-kit")
    installed = make_installed_skill(tmp_path / "codex", name="debug-helper")

    result = discovery.discover(DiscoveryContext(project_root=project, codex_home=installed))

    assert {item.name for item in result.capabilities} == {"auth-kit", "example-plugin", "debug-helper"}
    assert {item.artifact_type for item in result.capabilities} == {
        ArtifactType.CODE,
        ArtifactType.PLUGIN,
        ArtifactType.SKILL,
    }
    assert {item.lifecycle for item in result.capabilities} == {Lifecycle.OBSERVED}
    assert set(result.sources_scanned) == {str(project.resolve()), str((installed / "plugins" / "cache").resolve())}


def test_ephemeral_discovery_ids_are_stable_across_fresh_instances(tmp_path):
    """Random in-memory source IDs make identical discovery contexts look new on every run."""
    project = make_project(tmp_path / "active", package_name="stable-kit")
    installed = make_installed_skill(tmp_path / "codex", name="stable-helper")
    context = DiscoveryContext(project_root=project, codex_home=installed)

    first = CapabilityDiscovery().discover(context)
    second = CapabilityDiscovery().discover(context)

    assert len(first.capabilities) == 3
    assert {(item.artifact_type, item.name): item.id for item in first.capabilities} == {
        (item.artifact_type, item.name): item.id for item in second.capabilities
    }


def test_discovery_does_not_scan_unregistered_sibling(tmp_path, discovery):
    """A recursive parent-directory walk would leak the sibling's README."""
    active = make_project(tmp_path / "active", readme="Active capability", package_name="active-kit")
    make_project(tmp_path / "private-sibling", readme="Must not be indexed", package_name="private-kit")

    result = discovery.scan_active_project(active)

    assert "Must not be indexed" not in serialize(result)
    assert "private-kit" not in {item.name for item in result}


def test_discovery_ignores_symlinked_readme_that_escapes_active_project(tmp_path, discovery):
    """Following an escaped README symlink would disclose outside content."""
    private = tmp_path / "private.md"
    private.write_text("outside secret capability", encoding="utf-8")
    project = make_project(tmp_path / "active", package_name="safe-kit")
    (project / "README.md").symlink_to(private)

    result = discovery.scan_active_project(project)

    assert "outside secret capability" not in serialize(result)
    assert {item.name for item in result} == {"safe-kit"}


def test_discovery_ignores_symlinked_declared_skill_that_escapes_plugin_cache(tmp_path, discovery):
    """A declared skills directory must still be contained after resolution."""
    codex_home = tmp_path / "codex"
    private_skill = tmp_path / "private" / "SKILL.md"
    private_skill.parent.mkdir(parents=True)
    private_skill.write_text("---\nname: outside-skill\ndescription: secret\n---", encoding="utf-8")
    plugin = codex_home / "plugins" / "cache" / "safe-plugin" / "1.0.0"
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "safe-plugin", "skills": "./skills"}), encoding="utf-8")
    skills = plugin / "skills"
    skills.mkdir()
    (skills / "outside").symlink_to(private_skill.parent, target_is_directory=True)

    result = discovery.scan_installed_plugins(codex_home)

    assert "outside-skill" not in serialize(result)
    assert [item.name for item in result] == ["safe-plugin"]


def test_discovery_does_not_read_nested_private_source_files(tmp_path, discovery):
    """Broad source-file traversal would turn implementation comments into metadata."""
    project = make_project(tmp_path / "active", readme="Public component", package_name="public-kit")
    secret = project / "src" / "internal" / "notes.py"
    secret.parent.mkdir(parents=True)
    secret.write_text('"""PRIVATE_DEPLOYMENT_PASSWORD"""', encoding="utf-8")

    result = discovery.scan_active_project(project)

    assert "PRIVATE_DEPLOYMENT_PASSWORD" not in serialize(result)


def test_discovery_skips_malformed_manifest_without_falling_back_to_its_text(tmp_path, discovery):
    """Treating invalid JSON as text would invent a capability from corrupt metadata."""
    project = make_project(tmp_path / "active")
    (project / "package.json").write_text('{"name": "not-a-valid-capability"', encoding="utf-8")

    result = discovery.scan_active_project(project)

    assert result == ()


def test_discovery_preserves_unicode_manifest_metadata(tmp_path, discovery):
    """ASCII-only normalization would lose valid source metadata."""
    project = make_project(tmp_path / "active", readme="可复用组件", package_name="数据-工具")

    result = discovery.scan_active_project(project)

    assert [item.name for item in result] == ["数据-工具"]
    assert "可复用组件" in result[0].summary


def test_discovery_uses_manifest_first_plugin_and_skill_categories(tmp_path, discovery):
    """A plugin manifest must yield a plugin record and only its declared skill metadata."""
    codex_home = make_installed_skill(tmp_path / "codex", name="debug-helper")
    plugin = codex_home / "plugins" / "cache" / "example-plugin" / "1.0.0"
    (plugin / "unlisted" / "ignored").mkdir(parents=True)
    (plugin / "unlisted" / "ignored" / "SKILL.md").write_text(
        "---\nname: unlisted\ndescription: private\n---", encoding="utf-8"
    )

    result = discovery.scan_installed_plugins(codex_home)
    by_name = {item.name: item for item in result}

    assert set(by_name) == {"example-plugin", "debug-helper"}
    assert by_name["example-plugin"].artifact_type is ArtifactType.PLUGIN
    assert by_name["example-plugin"].category_path == ("Tools and integrations", "Plugins and MCP")
    assert by_name["debug-helper"].category_path == ("Tools and integrations", "Codex Skills")


def test_root_skill_reader_does_not_recursively_authorize_nested_skills(tmp_path, discovery):
    """Turning the root-skill reader into a tree walk would expose private skills."""
    project = make_project(tmp_path / "active", package_name="safe-kit")
    (project / "SKILL.md").write_text("---\nname: root-skill\ndescription: public\n---", encoding="utf-8")
    nested = project / "private" / "SKILL.md"
    nested.parent.mkdir()
    nested.write_text("---\nname: private-skill\ndescription: private\n---", encoding="utf-8")

    result = discovery.scan_active_project(project)

    assert {item.name for item in result} == {"safe-kit", "root-skill"}


def test_plugin_and_declared_skill_identity_survive_cache_version_upgrade(tmp_path, discovery):
    """Including a cache path or version in IDs duplicates one upgraded plugin."""
    codex_home = tmp_path / "codex"
    for version in ("1.0.0", "2.0.0"):
        plugin = codex_home / "plugins" / "cache" / "acme" / "toolbox" / version
        manifest = plugin / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps({"id": "acme.toolbox", "name": "Toolbox", "version": version, "skills": "skills"}),
            encoding="utf-8",
        )
        skill = plugin / "skills" / "helper" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: helper\ndescription: Use it\n---", encoding="utf-8")

    result = discovery.scan_installed_plugins(codex_home)
    by_name = {item.name: item for item in result}

    assert set(by_name) == {"Toolbox", "helper"}
    assert by_name["Toolbox"].source_revision == "2.0.0"
    assert by_name["helper"].source_revision == "2.0.0"


def test_readme_changes_change_the_canonical_content_hash(tmp_path, discovery):
    """Hashing only the raw manifest misses a changed observed summary."""
    project = make_project(tmp_path / "active", readme="First public summary", package_name="kit")
    before = discovery.scan_active_project(project)[0]
    (project / "README.md").write_text("Second public summary", encoding="utf-8")

    after = discovery.scan_active_project(project)[0]

    assert before.id == after.id
    assert before.content_hash != after.content_hash
    assert after.summary == "Second public summary"


def test_source_registry_persists_active_and_managed_sources_across_instances(tmp_path):
    """An in-memory registry loses the sources a later discovery run must refresh."""
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    active = make_project(tmp_path / "active", package_name="active-kit")
    registered = make_project(tmp_path / "registered", package_name="registered-kit")
    explicit = make_project(tmp_path / "explicit", package_name="explicit-kit")
    first = SourceRegistry(repo)
    first.register_active_project(active)
    first.register_registered_project(registered)
    first.register_explicit_project(explicit)

    result = CapabilityDiscovery(SourceRegistry(repo)).discover(
        DiscoveryContext(project_root=active, codex_home=tmp_path / "empty-codex")
    )

    assert {item.name for item in result.capabilities} == {"active-kit", "registered-kit", "explicit-kit"}


def test_discovery_reads_independent_codex_home_skills(tmp_path, discovery):
    """Ignoring CODEX_HOME/skills drops installed skills that have no plugin wrapper."""
    codex_home = tmp_path / "codex"
    skill = codex_home / "skills" / "solo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: solo\ndescription: Independent skill\n---", encoding="utf-8")

    result = discovery.scan_installed_plugins(codex_home)

    assert [(item.name, item.artifact_type) for item in result] == [("solo", ArtifactType.SKILL)]


def test_authoring_is_not_misclassified_as_identity_access(tmp_path, discovery):
    """Substring matching turns 'authoring' into a false authentication signal."""
    project = make_project(tmp_path / "active", readme="Document authoring workflow", package_name="authoring-kit")

    result = discovery.scan_active_project(project)

    assert result[0].category_path == ("Code and components",)


def test_authentication_token_is_classified_as_identity_access(tmp_path, discovery):
    """Removing the controlled authentication token loses an unambiguous category."""
    project = make_project(tmp_path / "active", readme="Authentication callback handler", package_name="login-kit")

    assert discovery.scan_active_project(project)[0].category_path == (
        "Code and components",
        "Identity and access",
    )


def test_python_public_exports_are_limited_to_manifest_entrypoint_all(tmp_path, discovery):
    """A source crawl would expose private names instead of the entrypoint's __all__."""
    project = make_project(tmp_path / "active")
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'python-kit'\nversion = '1.2.3'\n[project.entry-points.'supermind']\nplugin = 'python_kit.api'\n",
        encoding="utf-8",
    )
    entry = project / "python_kit" / "api.py"
    entry.parent.mkdir()
    entry.write_text("__all__ = ['public_api']\n_PRIVATE = 'do not expose'\n", encoding="utf-8")

    result = discovery.scan_active_project(project)

    assert result[0].facets == ("manifest", "public_api")
    assert result[0].source_revision == "1.2.3"


def test_invalid_utf8_manifest_is_skipped_instead_of_replaced(tmp_path, discovery):
    """Replacing malformed bytes invents metadata that was never valid UTF-8."""
    project = make_project(tmp_path / "active")
    (project / "package.json").write_bytes(b'{"name": "bad\xff"}')

    assert discovery.scan_active_project(project) == ()


def test_unreadable_manifest_is_isolated_without_losing_other_metadata(tmp_path, discovery, monkeypatch):
    """One inaccessible manifest must be skipped instead of aborting the scan."""
    project = make_project(tmp_path / "active", package_name="unreadable-kit")
    (project / "pyproject.toml").write_text("[project]\nname = 'still-visible'\nversion = '1'\n", encoding="utf-8")
    real_open = os.open

    def deny_package(path, flags, mode=0o777, *, dir_fd=None):
        if path == "package.json" and dir_fd is not None:
            raise PermissionError("denied")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", deny_package)

    assert [item.name for item in discovery.scan_active_project(project)] == ["still-visible"]


def test_js_export_keys_and_targets_are_normalized_without_reading_source(tmp_path, discovery):
    """The public contract comes from manifest exports, not implementation files."""
    project = make_project(tmp_path / "active")
    (project / "package.json").write_text(
        json.dumps({"name": "js-kit", "version": "1", "exports": {".": "./dist/index.js", "./api": {"import": "./dist/api.js"}}}),
        encoding="utf-8",
    )
    private = project / "src" / "private.js"
    private.parent.mkdir()
    private.write_text("OUTSIDE_PUBLIC_EXPORTS", encoding="utf-8")

    result = discovery.scan_active_project(project)[0]

    assert result.facets == (".", "api", "dist/api.js", "dist/index.js", "manifest")
    assert "OUTSIDE_PUBLIC_EXPORTS" not in serialize((result,))


def test_unversioned_package_uses_its_project_git_revision(tmp_path, discovery):
    """A package without a version still needs provenance from repository metadata."""
    project = make_project(tmp_path / "active", package_name="git-kit")
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "tests@example.invalid"),
        ("git", "config", "user.name", "Tests"),
        ("git", "add", "package.json"),
        ("git", "commit", "-qm", "fixture"),
    ):
        subprocess.run(command, cwd=project, check=True)
    expected = subprocess.run(("git", "rev-parse", "HEAD"), cwd=project, check=True, text=True, capture_output=True).stdout.strip()

    assert discovery.scan_active_project(project)[0].source_revision == expected


def test_git_revision_is_read_from_authorized_git_fds_without_spawning_git(tmp_path, discovery, monkeypatch):
    """`git -C` can reopen or search paths outside the already-authorized root."""
    project = make_project(tmp_path / "active", package_name="fd-git-kit")
    git = project / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    revision = "a" * 40
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text(revision + "\n", encoding="utf-8")

    def forbidden_git(*args, **kwargs):
        raise AssertionError("git subprocess must not run")

    monkeypatch.setattr(subprocess, "run", forbidden_git)

    assert discovery.scan_active_project(project)[0].source_revision == revision


def test_git_revision_skips_head_replaced_by_out_of_root_symlink(tmp_path, discovery, monkeypatch):
    """A path reopen after checking `.git` would disclose an outside Git revision."""
    project = make_project(tmp_path / "active", package_name="git-race-kit")
    git = project / ".git"
    git.mkdir()
    (git / "HEAD").write_text("b" * 40 + "\n", encoding="utf-8")
    private = tmp_path / "private-head"
    private.write_text("c" * 40 + "\n", encoding="utf-8")
    real_open = os.open
    replaced = False

    def replace_head(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if path == "HEAD" and dir_fd is not None and not replaced:
            replaced = True
            (git / "HEAD").unlink()
            (git / "HEAD").symlink_to(private)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_head)

    assert discovery.scan_active_project(project)[0].source_revision == "unversioned"


def test_discovery_opens_children_relative_to_an_authorized_directory_fd(tmp_path, discovery, monkeypatch):
    """Path-based child reads can be redirected after the root was checked."""
    project = make_project(tmp_path / "active", package_name="safe-kit")
    real_open = os.open
    child_open_fds: list[int | None] = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        child_open_fds.append(dir_fd)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", recording_open)

    assert [item.name for item in discovery.scan_active_project(project)] == ["safe-kit"]
    assert any(fd is not None for fd in child_open_fds)


def test_discovery_skips_a_manifest_replaced_by_an_escaping_symlink(tmp_path, discovery, monkeypatch):
    """A check-then-open implementation would read the outside replacement."""
    project = make_project(tmp_path / "active", package_name="safe-kit")
    private = tmp_path / "private.json"
    private.write_text('{"name": "outside-secret"}', encoding="utf-8")
    real_open = os.open
    replaced = False

    def replace_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if path == "package.json" and dir_fd is not None and not replaced:
            replaced = True
            (project / "package.json").unlink()
            (project / "package.json").symlink_to(private)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_before_open)

    assert discovery.scan_active_project(project) == ()


def test_plugin_directory_enumeration_scans_open_directory_fds(tmp_path, discovery, monkeypatch):
    """Enumerating a pathname after validation reintroduces a directory-swap race."""
    codex_home = make_installed_skill(tmp_path / "codex", name="helper")
    real_scandir = os.scandir
    scanned: list[object] = []

    def recording_scandir(path):
        scanned.append(path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", recording_scandir)

    assert {item.name for item in discovery.scan_installed_plugins(codex_home)} == {"example-plugin", "helper"}
    assert scanned and all(isinstance(item, int) for item in scanned)


def test_skill_frontmatter_accepts_bounded_yaml_metadata_and_uses_metadata_version(tmp_path, discovery):
    """Rejecting ordinary YAML metadata drops valid installed skills."""
    codex_home = tmp_path / "codex"
    skill = codex_home / "skills" / "yaml" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\n# public skill metadata\nname: yaml-helper\ndescription: >\n  A folded\n  description\nlicense: MIT\nmetadata:\n  version: 2.4.0\ncompatibility:\n  - codex\ncustom:\n  maintained: true\n---\nbody\n",
        encoding="utf-8",
    )

    result = discovery.scan_installed_plugins(codex_home)

    assert [(item.name, item.summary, item.source_revision) for item in result] == [
        ("yaml-helper", "A folded description", "2.4.0")
    ]


def test_valid_skill_frontmatter_capability_round_trips_through_repository(tmp_path, discovery):
    """Accepted public fields must already have the repository's concrete value types."""
    codex_home = tmp_path / "codex"
    skill = codex_home / "skills" / "roundtrip" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: roundtrip-helper\ndescription: Safe helper\nversion: '1.0'\nlicense: MIT\nmetadata:\n  version: '2.0'\ncompatibility:\n  - codex\n  - macos\n---\n",
        encoding="utf-8",
    )
    capability = discovery.scan_installed_plugins(codex_home)[0]
    repository = CapabilityRepository.open(tmp_path / "memory.lance")
    repository.initialize()

    repository.upsert_capability(capability, [0.0] * 384)

    assert repository.get_capability(capability.id) == capability


def test_skill_frontmatter_accepts_paired_quoted_scalars_with_flow_characters(tmp_path, discovery):
    """Rejecting flow punctuation inside quotes drops valid scalar metadata."""
    codex_home = tmp_path / "codex"
    skill = codex_home / "skills" / "quoted" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: \"[quoted-helper]\"\ndescription: '{safe: text}'\nlicense: \"[MIT]\"\nmetadata:\n  version: '{2.0}'\ncompatibility:\n  - \"[codex]\"\n---\n",
        encoding="utf-8",
    )

    result = discovery.scan_installed_plugins(codex_home)

    assert [(item.name, item.summary, item.source_revision, item.license, item.compatibility) for item in result] == [
        ("[quoted-helper]", "{safe: text}", "{2.0}", "[MIT]", ("[codex]",))
    ]


def test_skill_frontmatter_reads_only_header_and_supports_literal_description(tmp_path, discovery):
    """Reading the skill body defeats metadata-only discovery and rejects large bodies."""
    skill = tmp_path / "codex" / "skills" / "header" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: header-only\ndescription: |\n  first line\n  second line\n---\n" + ("body\n" * 80_000), encoding="utf-8")

    result = discovery.scan_installed_plugins(tmp_path / "codex")

    assert [(item.name, item.summary) for item in result] == [("header-only", "first line\nsecond line")]


@pytest.mark.parametrize(
    "frontmatter",
    (
        "---\nname: helper\nname: duplicate\n---",
        "---\nname: !dangerous helper\n---",
        "---\nname:\n  too: \n    deeply: \n      nested: \n        structure: \n          is: \n            rejected: \n              here: value\n---",
    ),
)
def test_skill_frontmatter_rejects_unsafe_or_ambiguous_yaml(tmp_path, discovery, frontmatter):
    """Duplicate keys, tags, and excessive structure make metadata untrustworthy."""
    skill = tmp_path / "codex" / "skills" / "unsafe" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(frontmatter, encoding="utf-8")

    assert discovery.scan_installed_plugins(tmp_path / "codex") == ()


@pytest.mark.parametrize(
    "frontmatter",
    (
        "name: [not, a, string]",
        "name: wrong-shape\ndescription: {nested: value}",
        'name: "unterminated',
        "name: wrong-shape\nversion: [1, 2]",
        "name: wrong-shape\nlicense: [MIT, Apache-2.0]",
    ),
)
def test_skill_frontmatter_rejects_flow_collections_and_unpaired_quotes(tmp_path, discovery, frontmatter):
    """Treating unsupported YAML syntax as text admits metadata with the wrong type."""
    skill = tmp_path / "codex" / "skills" / "invalid-scalar" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\n{frontmatter}\n---\n", encoding="utf-8")

    assert discovery.scan_installed_plugins(tmp_path / "codex") == ()


@pytest.mark.parametrize(
    "invalid_fields",
    (
        "description:\n  detail: nested",
        "description:\n  - first\n  - second",
        "version:\n  major: one",
        "license:\n  - MIT",
        "metadata:\n  - version",
        "metadata:\n  version:\n    major: two",
        "metadata:\n  version:\n    - two",
        "compatibility: codex",
        "compatibility:\n  codex: supported",
    ),
)
def test_skill_frontmatter_rejects_wrong_public_field_types(tmp_path, discovery, invalid_fields):
    """Coercing or ignoring a wrong public-field shape admits an invalid Capability."""
    skill = tmp_path / "codex" / "skills" / "wrong-shape" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: wrong-shape\n{invalid_fields}\n---\n", encoding="utf-8")

    assert discovery.scan_installed_plugins(tmp_path / "codex") == ()


def test_registry_namespaces_equal_package_names_from_distinct_persistent_sources(tmp_path):
    """A package name alone merges different projects into one capability."""
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    first = make_project(tmp_path / "first", package_name="shared-kit")
    second = make_project(tmp_path / "second", package_name="shared-kit")
    registry = SourceRegistry(repo)
    registry.register_registered_project(second)

    result = CapabilityDiscovery(registry).discover(DiscoveryContext(project_root=first, codex_home=tmp_path / "codex"))

    items = [item for item in result.capabilities if item.name == "shared-kit"]
    assert len(items) == 2
    assert len({item.id for item in items}) == 2


def test_plugin_skill_id_uses_owner_and_relative_path_not_cache_version(tmp_path, discovery):
    """A cache-version segment in skill identity makes every upgrade a new skill."""
    ids = []
    for version in ("1.0.0", "2.0.0"):
        codex_home = tmp_path / version / "codex"
        plugin = codex_home / "plugins" / "cache" / "acme" / "toolbox" / version
        manifest = plugin / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"id": "acme.toolbox", "name": "Toolbox", "version": version, "skills": "skills"}), encoding="utf-8")
        skill = plugin / "skills" / "helper" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: display-name-can-change\ndescription: helper\n---", encoding="utf-8")
        ids.append(next(item.id for item in discovery.scan_installed_plugins(codex_home) if item.artifact_type is ArtifactType.SKILL))

    assert ids[0] == ids[1]


def test_stale_source_registry_instances_merge_persisted_mutations(tmp_path):
    """A stale in-memory snapshot must not erase another registry's source."""
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    first = SourceRegistry(repo)
    stale = SourceRegistry(repo)
    registered = make_project(tmp_path / "registered", package_name="registered")
    explicit = make_project(tmp_path / "explicit", package_name="explicit")

    first.register_registered_project(registered)
    stale.register_explicit_project(explicit)

    refreshed = SourceRegistry(repo)
    assert refreshed.projects("registered") == (registered.absolute(),)
    assert refreshed.projects("explicit") == (explicit.absolute(),)


def test_promoting_one_registered_path_to_active_preserves_its_source_identity(tmp_path):
    """Separate kind lists give one source two identities after promotion."""
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    project = make_project(tmp_path / "project", package_name="kit")
    registry = SourceRegistry(repo)
    registry.register_registered_project(project)
    before = registry.identity(project)

    registry.register_active_project(project)

    assert registry.identity(project) == before
    stored = repo.get_metadata("capability-discovery-source-registry-v1")
    assert stored["sources"] == [{"id": before, "kinds": ["active", "registered"], "path": str(project.absolute())}]


def test_untrusted_same_named_plugins_from_distinct_installed_sources_do_not_merge(tmp_path):
    """Cache publisher/name is not global identity across separate installed sources."""
    repo = CapabilityRepository.open(tmp_path / "memory.lance")
    repo.initialize()
    homes = []
    for label in ("one", "two"):
        home = tmp_path / label / "codex"
        manifest = home / "plugins" / "cache" / "publisher" / "tool" / "1" / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": "same-name", "version": "1"}), encoding="utf-8")
        homes.append(home)
    registry = SourceRegistry(repo)
    registry.register_installed_codex_home(homes[1])

    result = CapabilityDiscovery(registry).discover(DiscoveryContext(project_root=make_project(tmp_path / "project"), codex_home=homes[0]))

    assert len([item for item in result.capabilities if item.name == "same-name"]) == 2


def test_mixed_standard_and_nonstandard_plugin_versions_choose_deterministically(tmp_path, discovery):
    """Comparing heterogeneous version fragments must not raise TypeError."""
    codex_home = tmp_path / "codex"
    for version in ("1.0.0", "release-candidate"):
        plugin = codex_home / "plugins" / "cache" / "acme" / "mixed" / version
        manifest = plugin / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"id": "acme.mixed", "name": "Mixed", "version": version}), encoding="utf-8")

    result = discovery.scan_installed_plugins(codex_home)

    assert len(result) == 1
    assert result[0].source_revision == "1.0.0"


def test_test_name_reader_does_not_open_non_test_files_and_keeps_only_legal_nodes(tmp_path, discovery, monkeypatch):
    """Reading every file leaks private content and AST walking invents nested tests."""
    project = make_project(tmp_path / "active", package_name="test-kit")
    tests = project / "tests"
    tests.mkdir()
    (tests / "notes.txt").write_text("PRIVATE_NOTES", encoding="utf-8")
    (tests / "test_shape.py").write_text(
        "def test_top_level(): pass\n\ndef helper():\n    def test_nested(): pass\n\nclass TestPublic:\n    def test_method(self): pass\n\nclass Helper:\n    def test_not_a_test_class(self): pass\n",
        encoding="utf-8",
    )
    real_open = os.open
    opened: list[str] = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        if isinstance(path, str):
            opened.append(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", recording_open)

    result = discovery.scan_active_project(project)[0]

    assert "notes.txt" not in opened
    assert {"test_top_level", "test_method"}.issubset(result.facets)
    assert "test_nested" not in result.facets
    assert "test_not_a_test_class" not in result.facets


def test_python_entrypoint_honours_declared_src_root_without_source_walk(tmp_path, discovery):
    """Ignoring build configuration misses the one manifest-declared public module."""
    project = make_project(tmp_path / "active")
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'src-kit'\nversion = '1'\n[project.entry-points.'supermind']\nplugin = 'pkg.api'\n[tool.setuptools]\npackage-dir = {'' = 'src'}\n",
        encoding="utf-8",
    )
    api = project / "src" / "pkg" / "api.py"
    api.parent.mkdir(parents=True)
    api.write_text("__all__ = ['declared_api']\n", encoding="utf-8")
    (project / "private.py").write_text("__all__ = ['private']\n", encoding="utf-8")

    assert discovery.scan_active_project(project)[0].facets == ("declared_api", "manifest")


def test_python_entrypoint_honours_setuptools_find_where_root(tmp_path, discovery):
    """The controlled setuptools find root is another manifest-declared source root."""
    project = make_project(tmp_path / "active")
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'find-kit'\nversion = '1'\n[project.entry-points.'supermind']\nplugin = 'pkg.api'\n[tool.setuptools.packages.find]\nwhere = ['src']\n",
        encoding="utf-8",
    )
    api = project / "src" / "pkg" / "api.py"
    api.parent.mkdir(parents=True)
    api.write_text("__all__ = ['found_api']\n", encoding="utf-8")

    assert discovery.scan_active_project(project)[0].facets == ("found_api", "manifest")


def test_discovery_reports_both_cache_and_standalone_skill_sources_and_real_timestamp(tmp_path, discovery):
    """A partial source report obscures one installed input and epoch timestamps are not observations."""
    codex_home = make_installed_skill(tmp_path / "codex", name="wrapped")
    solo = codex_home / "skills" / "solo" / "SKILL.md"
    solo.parent.mkdir(parents=True)
    solo.write_text("---\nname: solo\ndescription: independent\n---", encoding="utf-8")
    project = make_project(tmp_path / "active", package_name="time-kit")

    result = discovery.discover(DiscoveryContext(project_root=project, codex_home=codex_home))

    assert str((codex_home / "plugins" / "cache").absolute()) in result.sources_scanned
    assert str((codex_home / "skills").absolute()) in result.sources_scanned
    assert datetime.fromisoformat(next(item for item in result.capabilities if item.name == "time-kit").created_at).tzinfo is UTC
    assert not next(item for item in result.capabilities if item.name == "time-kit").created_at.startswith("1970")


@pytest.mark.parametrize("metadata_surface", ("package", "readme", "skill"))
def test_metadata_secrets_are_redacted_before_capability_hashing(
    tmp_path,
    discovery,
    metadata_surface,
):
    first_secret = "ghp_" + ("A1b2" * 10)
    second_secret = "ghp_" + ("Z9y8" * 10)
    first_entropy_secret = "N7vQ2mX9pL4sT8wZ1cR6yK3dF5hJ0uB2gE9a"
    second_entropy_secret = "Q4nV8xM1pR6tY2wK9cD5sH7jL0bF3uA6eZ8g"
    project = make_project(tmp_path / "secret-project")

    def write_surface(secret, entropy_secret):
        description = f"Reusable helper token={secret} session={entropy_secret}"
        if metadata_surface == "package":
            (project / "package.json").write_text(
                json.dumps({"name": "safe-kit", "description": description}),
                encoding="utf-8",
            )
        elif metadata_surface == "readme":
            (project / "package.json").write_text(
                json.dumps({"name": "safe-kit"}),
                encoding="utf-8",
            )
            (project / "README.md").write_text(description, encoding="utf-8")
        else:
            (project / "SKILL.md").write_text(
                f"---\nname: safe-skill\ndescription: {description}\n---\n",
                encoding="utf-8",
            )

    write_surface(first_secret, first_entropy_secret)
    first = discovery.scan_active_project(project)[0]
    write_surface(second_secret, second_entropy_secret)
    second = discovery.scan_active_project(project)[0]

    assert "[REDACTED]" in first.summary
    assert first_secret not in serialize((first,))
    assert first_entropy_secret not in serialize((first,))
    assert second_secret not in serialize((second,))
    assert second_entropy_secret not in serialize((second,))
    assert first.content_hash == second.content_hash
