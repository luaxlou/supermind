from __future__ import annotations

import json
import subprocess
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

import pytest

from supermind_memory.config import MemoryPaths, RepositoryConfig
from supermind_memory.embeddings import EmbeddingProvider
from supermind_memory.event_model import AuthorityEvent
from supermind_memory.event_store import EventStore
from supermind_memory.git_client import (
    CompletedCommand,
    GitClient,
    GitHubClient,
    InitRequest,
    SubprocessCommandRunner,
    initialize_repository,
)
from supermind_memory.projection import project_authority
from supermind_memory.replay import ReplayResult, replay
from supermind_memory.repository import CapabilityRepository
from supermind_memory.search import CapabilitySearch
from supermind_memory.sync import SyncBlocked, SyncCoordinator, SyncState
from supermind_memory.types import (
    ArtifactType,
    Capability,
    Lifecycle,
    RequirementProfile,
    SearchStatus,
)


class LocalGitHubRunner:
    def __init__(self, bare_repository: Path) -> None:
        self.bare_repository = bare_repository
        self.online = True
        self.private = True
        self.repository_id = "R_private"
        self.calls: list[tuple[str, ...]] = []
        self.before_next_push = None
        self.reject_pushes = False
        self._git = SubprocessCommandRunner()

    def run(self, argv, cwd=None):
        call = tuple(argv)
        self.calls.append(call)
        if call[0] == "gh":
            if not self.online:
                return CompletedCommand(1, b"", b"network unavailable")
            if call[:3] == ("gh", "repo", "view"):
                metadata = {
                    "id": self.repository_id,
                    "nameWithOwner": "owner/memory",
                    "isPrivate": self.private,
                    "defaultBranchRef": {"name": "main"},
                    "sshUrl": str(self.bare_repository),
                    "url": "https://github.com/owner/memory",
                }
                return CompletedCommand(0, json.dumps(metadata).encode(), b"")
            if call[:2] == ("gh", "api"):
                return CompletedCommand(0, b"true\n", b"")
        if call[:2] == ("git", "push"):
            if self.before_next_push is not None:
                callback, self.before_next_push = self.before_next_push, None
                callback()
            if self.reject_pushes:
                return CompletedCommand(1, b"", b"non-fast-forward")
        return self._git.run(call, cwd)


class FakeRenderPort:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def render(self, result: ReplayResult, checkout: Path) -> None:
        self.calls.append(result.digest)
        capabilities = sorted(
            entity_id
            for (entity_type, entity_id) in result.entities
            if entity_type == "capability"
        )
        (checkout / "README.md").write_text("\n".join(capabilities) + "\n")
        manifest = checkout / ".supermind" / "render-manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"event_set_digest": result.digest}, sort_keys=True) + "\n")


class Device:
    def __init__(
        self,
        *,
        paths: MemoryPaths,
        runner: LocalGitHubRunner,
        repository: CapabilityRepository,
        embeddings: EmbeddingProvider,
    ) -> None:
        self.paths = paths
        self.runner = runner
        self.repository = repository
        self.renderer = FakeRenderPort()
        self.store = EventStore(paths.checkout)
        self.coordinator = SyncCoordinator(
            paths=paths,
            config=initialize_repository(
                InitRequest("owner/memory", paths.root.parent.parent.name, runner=runner), paths,
            ),
            git=GitClient(runner),
            github=GitHubClient(runner),
            repository=repository,
            embeddings=embeddings,
            renderer=self.renderer,
        )
        project_authority(replay(self.store.load_all()), repository, embeddings)

    def mutate(self, event: AuthorityEvent):
        return self.coordinator.mutate(lambda _: event)

    def sync(self):
        return self.coordinator.synchronize()

    def replay(self) -> ReplayResult:
        return replay(self.store.load_all())


def _run_git(*argv: str, cwd: Path | None = None) -> bytes:
    result = subprocess.run(
        ("git", *argv), cwd=cwd, shell=False, check=False, text=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def _capability(identifier: str, source: Path, summary: str = "Reusable capability") -> Capability:
    return Capability(
        id=identifier,
        name=identifier.title(),
        summary=summary,
        category_path=("Code and components", identifier),
        facets=(identifier,),
        contract=f"Provide {identifier}",
        constraints=(),
        artifact_type=ArtifactType.CODE,
        source_uri=str(source),
        source_revision="abc123",
        content_hash="a" * 64,
        owner="test",
        license="MIT",
        stack=("python",),
        runtime=("python",),
        platform=("local",),
        dependencies=(),
        compatibility=(),
        lifecycle=Lifecycle.VERIFIED,
        confidence=0.9,
        expected_net_value=1.0,
        embedding_generation="test",
        created_at="2026-09-06T12:00:00Z",
        updated_at="2026-09-06T12:00:00Z",
        last_verified_at="2026-09-06T12:00:00Z",
    )


def _capability_event(
    identifier: str,
    *,
    device: str,
    source: Path,
    event_id: str | None = None,
    parent: str | None = None,
    summary: str = "Reusable capability",
) -> AuthorityEvent:
    capability = _capability(identifier, source, summary)
    payload = asdict(capability)
    payload["artifact_type"] = capability.artifact_type.value
    payload["lifecycle"] = capability.lifecycle.value
    return AuthorityEvent.create(
        event_id=event_id or f"{device}-{identifier}-created",
        device_id=f"device-{device}",
        entity_type="capability",
        entity_id=identifier,
        operation="updated" if parent else "registered",
        parent_event_ids=() if parent is None else (parent,),
        occurred_at="2026-09-06T12:00:00Z",
        payload=payload,
    )


@pytest.fixture
def devices(tmp_path, embeddings):
    bare = tmp_path / "remote.git"
    _run_git("init", "--bare", "--initial-branch=main", str(bare))
    source = tmp_path / "source.py"
    source.write_text("def reusable(): return True\n")
    with ExitStack() as stack:
        items = []
        for name in ("a", "b"):
            paths = MemoryPaths.from_codex_home(tmp_path / f"data-{name}")
            repository = stack.enter_context(CapabilityRepository.open(
                paths.database, writer_lock_path=paths.locks / "writer.lock",
            ))
            repository.initialize()
            items.append(Device(
                paths=paths,
                runner=LocalGitHubRunner(bare),
                repository=repository,
                embeddings=embeddings,
            ))
        yield bare, source, items[0], items[1]


def test_two_devices_merge_changes_to_different_entities(devices):
    _, source, device_a, device_b = devices

    report_a = device_a.mutate(_capability_event("login", device="a", source=source))
    report_b = device_b.mutate(_capability_event("upload", device="b", source=source))

    assert report_a.sync_state is SyncState.SYNCED
    assert report_b.sync_state is SyncState.SYNCED
    assert {
        entity_id
        for entity_type, entity_id in device_b.replay().entities
        if entity_type == "capability"
    } == {"login", "upload"}


def test_two_devices_expose_sibling_updates_as_a_search_safe_conflict(devices, embeddings):
    _, source, device_a, device_b = devices
    base = _capability_event("login", device="seed", source=source)
    unrelated = _capability_event("upload", device="seed", source=source)
    device_a.mutate(base)
    device_a.mutate(unrelated)
    device_b.sync()

    device_b.runner.online = False
    pending = device_b.mutate(_capability_event(
        "login", device="b", source=source, event_id="b-login-update",
        parent=base.event_id, summary="Device B",
    ))
    assert pending.sync_state is SyncState.PENDING_SYNC
    device_b.runner.online = True
    device_a.mutate(_capability_event(
        "login", device="a", source=source, event_id="a-login-update",
        parent=base.event_id, summary="Device A",
    ))

    with pytest.raises(SyncBlocked, match="authority_conflict"):
        device_b.sync()

    replayed = device_b.replay()
    assert replayed.conflicts[0].entity_id == "login"
    checked_remote = _run_git(
        "--git-dir", str(devices[0]), "rev-parse", "refs/heads/main",
    ).decode().strip()
    assert RepositoryConfig.read(device_b.paths.config).last_checked_remote_head == checked_remote
    assert device_b.coordinator.git.status(device_b.paths.checkout) == ()
    search = CapabilitySearch(device_b.repository, embeddings)
    conflicted = search.search(RequirementProfile(
        "req-login", "project", "login", category_hint=("Code and components", "login"),
    ))
    unrelated_result = search.search(RequirementProfile(
        "req-upload", "project", "upload", category_hint=("Code and components", "upload"),
    ))
    assert conflicted.status is SearchStatus.COMPLETE
    assert conflicted.matches == ()
    assert unrelated_result.status is SearchStatus.COMPLETE


def test_offline_mutation_is_searchable_then_reaches_another_checkout(devices, embeddings):
    _, source, device_a, device_b = devices
    device_a.runner.online = False

    report = device_a.mutate(_capability_event("login", device="a", source=source))

    assert report.sync_state is SyncState.PENDING_SYNC
    local = CapabilitySearch(device_a.repository, embeddings).search(RequirementProfile(
        "req-login", "project", "login", category_hint=("Code and components", "login"),
    ))
    assert local.status is SearchStatus.COMPLETE
    assert tuple(match.capability_id for match in local.matches) == ("login",)

    device_a.runner.online = True
    assert device_a.sync().sync_state is SyncState.SYNCED
    assert device_b.sync().sync_state is SyncState.SYNCED
    assert ("capability", "login") in device_b.replay().entities


def test_online_mutation_publishes_a_pending_commit_before_creating_the_next_event(devices):
    bare, source, device_a, _ = devices
    first = _capability_event("login", device="a", source=source)
    device_a.runner.online = False
    device_a.mutate(first)
    device_a.runner.online = True
    observed = []

    def create_after_pending_is_published(result):
        path = f"events/v1/{first.device_id}/2026-09/{first.event_id}.json"
        observed.append(_run_git("--git-dir", str(bare), "show", f"main:{path}"))
        return _capability_event("upload", device="a", source=source)

    report = device_a.coordinator.mutate(create_after_pending_is_published)

    assert observed == [first.to_bytes()]
    assert report.sync_state is SyncState.SYNCED


def test_privacy_loss_blocks_before_event_creation(devices):
    _, source, device_a, _ = devices
    device_a.runner.private = False
    callback_calls = 0

    def forbidden_callback(result):
        nonlocal callback_calls
        callback_calls += 1
        return _capability_event("login", device="a", source=source)

    with pytest.raises(SyncBlocked, match="repository_not_private"):
        device_a.coordinator.mutate(forbidden_callback)

    assert callback_calls == 0
    assert device_a.store.load_all() == ()


def test_repository_id_change_blocks_before_event_creation(devices):
    _, source, device_a, _ = devices
    device_a.runner.repository_id = "R_replaced"
    callback_calls = 0

    def forbidden_callback(result):
        nonlocal callback_calls
        callback_calls += 1
        return _capability_event("login", device="a", source=source)

    with pytest.raises(SyncBlocked, match="repository_identity_mismatch"):
        device_a.coordinator.mutate(forbidden_callback)

    assert callback_calls == 0


def test_offline_mutation_requires_a_healthy_existing_projection(devices):
    _, source, device_a, _ = devices
    device_a.repository.set_metadata("unexpected", True)
    device_a.runner.online = False
    callback_calls = 0

    def forbidden_callback(result):
        nonlocal callback_calls
        callback_calls += 1
        return _capability_event("login", device="a", source=source)

    with pytest.raises(SyncBlocked, match="local_projection_unhealthy"):
        device_a.coordinator.mutate(forbidden_callback)

    assert callback_calls == 0


def test_symlinked_git_path_blocks_before_event_creation(devices):
    _, source, device_a, _ = devices
    git_path = device_a.paths.checkout / ".git"
    real_git = device_a.paths.checkout.parent / "relocated-git"
    git_path.rename(real_git)
    git_path.symlink_to(real_git, target_is_directory=True)
    callback_calls = 0

    def forbidden_callback(result):
        nonlocal callback_calls
        callback_calls += 1
        return _capability_event("login", device="a", source=source)

    with pytest.raises(SyncBlocked, match="repository_symlink_unsafe"):
        device_a.coordinator.mutate(forbidden_callback)

    assert callback_calls == 0


def test_invalid_local_event_is_rejected_without_dirtying_checkout(devices):
    _, source, device_a, _ = devices
    event = _capability_event(
        "login", device="a", source=source, event_id="orphan-local",
        parent="missing-parent", summary="Orphan",
    )
    project_calls = []
    render_calls = len(device_a.renderer.calls)

    def recording_projector(result, repository, provider):
        project_calls.append(result.digest)
        return project_authority(result, repository, provider)

    device_a.coordinator.projector = recording_projector

    with pytest.raises(SyncBlocked, match="authority_corrupt"):
        device_a.mutate(event)

    assert project_calls == []
    assert len(device_a.renderer.calls) == render_calls
    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()


def _commit_remote_event(bare: Path, worktree: Path, event: AuthorityEvent) -> None:
    _run_git("clone", str(bare), str(worktree))
    EventStore(worktree).append(event)
    _run_git("add", "--all", cwd=worktree)
    _run_git(
        "-c", "user.name=Remote Device", "-c", "user.email=remote@example.invalid",
        "commit", "-m", "remote event", cwd=worktree,
    )
    _run_git("push", "origin", "main", cwd=worktree)


def test_non_fast_forward_push_fetches_unions_and_retries_without_force(devices, tmp_path):
    bare, source, device_a, _ = devices
    remote_event = _capability_event("upload", device="racer", source=source)
    device_a.runner.before_next_push = lambda: _commit_remote_event(
        bare, tmp_path / "racer", remote_event,
    )

    report = device_a.mutate(_capability_event("login", device="a", source=source))

    assert report.sync_state is SyncState.SYNCED
    assert {
        entity_id for entity_type, entity_id in device_a.replay().entities
        if entity_type == "capability"
    } == {"login", "upload"}
    assert all("--force" not in call for call in device_a.runner.calls)


def test_push_retry_exhaustion_is_bounded_and_never_forces(devices):
    _, source, device_a, _ = devices
    device_a.runner.calls.clear()
    device_a.runner.reject_pushes = True

    with pytest.raises(SyncBlocked, match="push_retry_exhausted"):
        device_a.mutate(_capability_event("login", device="a", source=source))

    pushes = [call for call in device_a.runner.calls if call[:2] == ("git", "push")]
    assert len(pushes) == 3
    assert all("--force" not in call for call in device_a.runner.calls)


def test_same_event_path_with_different_remote_blob_blocks_before_project_or_render(
    devices, tmp_path,
):
    bare, source, device_a, _ = devices
    event = _capability_event("login", device="a", source=source)
    device_a.runner.online = False
    device_a.mutate(event)
    device_a.runner.online = True
    corrupt = tmp_path / "corrupt"
    _run_git("clone", str(bare), str(corrupt))
    relative = Path("events", "v1", event.device_id, "2026-09", f"{event.event_id}.json")
    target = corrupt / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(event.to_bytes().replace(b"Reusable capability", b"Corrupt capability"))
    _run_git("add", "--all", cwd=corrupt)
    _run_git(
        "-c", "user.name=Corrupt Device", "-c", "user.email=corrupt@example.invalid",
        "commit", "-m", "corrupt collision", cwd=corrupt,
    )
    _run_git("push", "origin", "main", cwd=corrupt)
    project_calls = []
    render_calls = len(device_a.renderer.calls)

    def recording_projector(result, repository, provider):
        project_calls.append(result.digest)
        return project_authority(result, repository, provider)

    device_a.coordinator.projector = recording_projector

    with pytest.raises(SyncBlocked, match="event_path_collision"):
        device_a.sync()

    assert project_calls == []
    assert len(device_a.renderer.calls) == render_calls


def test_invalid_remote_event_hash_blocks_before_project_or_render(devices, tmp_path):
    bare, source, device_a, _ = devices
    corrupt = tmp_path / "corrupt-unique"
    _run_git("clone", str(bare), str(corrupt))
    event = _capability_event("login", device="remote", source=source)
    relative = Path("events", "v1", event.device_id, "2026-09", f"{event.event_id}.json")
    target = corrupt / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(event.to_bytes().replace(b'"content_hash":"', b'"content_hash":"f'))
    _run_git("add", "--all", cwd=corrupt)
    _run_git(
        "-c", "user.name=Corrupt Device", "-c", "user.email=corrupt@example.invalid",
        "commit", "-m", "invalid event hash", cwd=corrupt,
    )
    _run_git("push", "origin", "main", cwd=corrupt)
    project_calls = []
    render_calls = len(device_a.renderer.calls)

    def recording_projector(result, repository, provider):
        project_calls.append(result.digest)
        return project_authority(result, repository, provider)

    device_a.coordinator.projector = recording_projector

    with pytest.raises(SyncBlocked, match="authority_corrupt"):
        device_a.sync()

    assert project_calls == []
    assert len(device_a.renderer.calls) == render_calls


def test_remote_symlinked_marker_is_rejected(devices, tmp_path):
    bare, _, device_a, _ = devices
    corrupt = tmp_path / "symlink-marker"
    _run_git("clone", str(bare), str(corrupt))
    marker = corrupt / "memory.json"
    raw_marker = marker.read_text().strip()
    marker.unlink()
    marker.symlink_to(raw_marker)
    _run_git("add", "--all", cwd=corrupt)
    _run_git(
        "-c", "user.name=Corrupt Device", "-c", "user.email=corrupt@example.invalid",
        "commit", "-m", "symlink marker", cwd=corrupt,
    )
    _run_git("push", "origin", "main", cwd=corrupt)

    with pytest.raises(SyncBlocked, match="repository_marker_invalid"):
        device_a.sync()


def test_missing_parent_in_remote_union_blocks_without_dirtying_checkout(devices, tmp_path):
    bare, source, device_a, _ = devices
    orphan = _capability_event(
        "login", device="remote", source=source, event_id="orphan-update",
        parent="missing-parent", summary="Orphan",
    )
    _commit_remote_event(bare, tmp_path / "orphan", orphan)
    project_calls = []
    render_calls = len(device_a.renderer.calls)

    def recording_projector(result, repository, provider):
        project_calls.append(result.digest)
        return project_authority(result, repository, provider)

    device_a.coordinator.projector = recording_projector

    with pytest.raises(SyncBlocked, match="authority_corrupt"):
        device_a.sync()

    assert project_calls == []
    assert len(device_a.renderer.calls) == render_calls
    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()


def test_local_sibling_event_is_stored_as_conflict_but_not_pushed_as_success(devices):
    bare, source, device_a, _ = devices
    base = _capability_event("login", device="a", source=source)
    device_a.mutate(base)
    sibling = _capability_event(
        "login", device="a", source=source, event_id="stale-sibling",
        summary="Stale sibling",
    )

    with pytest.raises(SyncBlocked, match="authority_conflict"):
        device_a.mutate(sibling)

    assert device_a.replay().conflicts[0].entity_id == "login"
    assert device_a.repository.list_capabilities() == ()
    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()
    remote_paths = _run_git(
        "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main", "--", "events",
    ).decode().splitlines()
    assert all("stale-sibling.json" not in path for path in remote_paths)
