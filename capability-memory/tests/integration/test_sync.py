from __future__ import annotations

import json
import subprocess
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

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
from supermind_memory.renderer import RepositoryRenderer, validate_render
from supermind_memory.repository import CapabilityRepository
from supermind_memory.search import CapabilitySearch
from supermind_memory.service import CapabilityMemory
from supermind_memory.sync import SyncBlocked, SyncCoordinator, SyncState
from supermind_memory.types import (
    ArtifactType,
    Capability,
    Lifecycle,
    HealthReport,
    ReuseResult,
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
        self.fail_fetches = False
        self.disconnect_after_rejected_push = False
        self.fail_commit_tree = False
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
                if self.disconnect_after_rejected_push:
                    self.fail_fetches = True
                return CompletedCommand(1, b"", b"non-fast-forward")
        if call[:2] == ("git", "fetch") and self.fail_fetches:
            return CompletedCommand(1, b"", b"network unavailable")
        if "commit-tree" in call and self.fail_commit_tree:
            return CompletedCommand(1, b"", b"commit failed")
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
        abstraction_status="abstracted",
        id=identifier,
        name=identifier.title(),
        summary=summary,
        category_path=("code", identifier),
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


def test_purge_rewrites_history_and_blocks_old_device(devices):
    bare, source, a, b = devices
    a.mutate(_capability_event("login", device="a", source=source))
    a.mutate(_capability_event("upload", device="a", source=source))
    b.sync()
    a.paths.generations.mkdir(parents=True, exist_ok=True)
    (a.paths.generations / "old-payload").write_text("upload")
    (a.paths.root / "migration-stage").mkdir()
    (a.paths.root / "migration-stage" / "old-payload").write_text("upload")
    old_head = a.coordinator.git.head(a.paths.checkout)
    preview = a.coordinator.purge(("upload",))
    assert preview["applied"] is False
    assert a.coordinator.git.head(a.paths.checkout) == old_head
    result = a.coordinator.purge(("upload",), confirm=True, expected_digest=preview["event_set_digest"],
                                 expected_head=preview["remote_head"])
    assert result["applied"] is True
    assert set(a.replay().entities) == {("capability", "login")}
    assert _run_git("rev-list", "--count", "main", cwd=bare).strip() == b"1"
    assert b"upload" not in _run_git("ls-tree", "-r", "--name-only", "main", cwd=bare)
    assert a.repository.get_capability("upload") is None
    assert not a.paths.generations.exists()
    assert not (a.paths.root / "migration-stage").exists()
    with pytest.raises(SyncBlocked, match="history_epoch_mismatch"):
        b.sync()
    assert _run_git("rev-list", "--count", "main", cwd=bare).strip() == b"1"


def test_purge_rejects_stale_preview_without_changes(devices):
    _, source, a, _ = devices
    a.mutate(_capability_event("login", device="a", source=source))
    before = a.coordinator.git.head(a.paths.checkout)
    with pytest.raises(SyncBlocked, match="purge_preview_stale"):
        a.coordinator.purge(("login",), confirm=True, expected_digest="bad", expected_head=before)
    assert a.coordinator.git.head(a.paths.checkout) == before


def test_purge_push_rejection_preserves_local_and_remote(devices):
    bare, source, a, _ = devices
    a.mutate(_capability_event("login", device="a", source=source))
    preview = a.coordinator.purge(("login",))
    a.runner.reject_pushes = True
    with pytest.raises(SyncBlocked, match="purge_push_rejected"):
        a.coordinator.purge(("login",), confirm=True, expected_digest=preview["event_set_digest"],
                           expected_head=preview["remote_head"])
    assert a.coordinator.git.head(a.paths.checkout) == preview["remote_head"]
    assert _run_git("rev-parse", "main", cwd=bare).strip().decode() == preview["remote_head"]
    assert a.repository.get_capability("login") is not None
    assert not (a.paths.root / "purge-pending.json").exists()


def test_purge_remote_race_preserves_new_remote_commit(devices):
    bare, source, a, b = devices
    a.mutate(_capability_event("login", device="a", source=source))
    preview = a.coordinator.purge(("login",))
    a.runner.before_next_push = lambda: b.mutate(_capability_event("upload", device="b", source=source))
    with pytest.raises(SyncBlocked, match="purge_push_rejected"):
        a.coordinator.purge(("login",), confirm=True, expected_digest=preview["event_set_digest"],
                           expected_head=preview["remote_head"])
    assert _run_git("rev-parse", "main", cwd=bare).strip().decode() == b.coordinator.git.head(b.paths.checkout)
    assert a.coordinator.git.head(a.paths.checkout) == preview["remote_head"]


def test_purge_preserves_user_note_inside_catalog(devices):
    _, source, a, _ = devices
    a.mutate(_capability_event("login", device="a", source=source))
    note = a.paths.checkout / "catalog" / "personal-note.md"
    note.parent.mkdir(exist_ok=True)
    note.write_text("Do not delete")
    _run_git("add", ".", cwd=a.paths.checkout)
    _run_git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "note", cwd=a.paths.checkout)
    _run_git("push", "origin", "main", cwd=a.paths.checkout)
    preview = a.coordinator.purge(("login",))
    with pytest.raises(SyncBlocked, match="purge_unowned_file"):
        a.coordinator.purge(("login",), confirm=True, expected_digest=preview["event_set_digest"],
                           expected_head=preview["remote_head"])
    assert note.read_text() == "Do not delete"


def test_service_reuse_blocks_stale_payload_after_remote_reconciliation(devices):
    _, source, device_a, device_b = devices
    original = _capability_event("login", device="a", source=source)
    device_a.mutate(original)
    incoming = _capability_event("login", device="b", source=source,
                                parent=original.event_id, summary="NEW REMOTE CONTRACT")
    device_b.mutate(incoming)
    memory = CapabilityMemory(
        bootstrap=SimpleNamespace(), discovery=SimpleNamespace(), repository=device_a.repository,
        search_engine=SimpleNamespace(), embedding_provider=device_a.coordinator.embeddings,
        health_manager=SimpleNamespace(ensure_healthy=lambda: HealthReport(
            True, "test", (), "2026-09-06T12:00:00Z")),
        codex_home=device_a.paths.root.parent.parent, sync_coordinator=device_a.coordinator,
    )
    reuse = ReuseResult("login", "consumer", True, 0.1, 2.0)
    with pytest.raises(SyncBlocked, match="authority_changed"):
        memory.record_use(reuse)
    assert device_a.store.load_all() == tuple(sorted((original, incoming), key=lambda event: event.event_id))
    assert device_a.repository.get_capability("login").summary == "NEW REMOTE CONTRACT"
    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()
    assert not device_a.replay().conflicts
    updated = memory.record_use(reuse)
    assert updated.summary == "NEW REMOTE CONTRACT"
    assert len([event for event in device_a.store.load_all() if event.entity_type == "reuse_outcome"]) == 1


def test_sync_transaction_uses_and_validates_production_repository_renderer(devices):
    _, source, device_a, _ = devices
    device_a.coordinator = SyncCoordinator(
        paths=device_a.paths,
        config=device_a.coordinator.config,
        git=device_a.coordinator.git,
        github=device_a.coordinator.github,
        repository=device_a.repository,
        embeddings=device_a.coordinator.embeddings,
    )

    assert isinstance(device_a.coordinator.renderer, RepositoryRenderer)

    report = device_a.mutate(_capability_event("login", device="a", source=source))

    assert "登录" not in (device_a.paths.checkout / "README.md").read_text()
    assert "Login" in (device_a.paths.checkout / "README.md").read_text()
    assert validate_render(device_a.paths.checkout, report.event_set_digest).event_set_digest == report.event_set_digest

    rendered = device_a.coordinator.render_repository()

    assert rendered.sync_state is SyncState.SYNCED
    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()


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
        "req-login", "project", "login", category_hint=("code", "login"),
    ))
    unrelated_result = search.search(RequirementProfile(
        "req-upload", "project", "upload", category_hint=("code", "upload"),
    ))
    assert conflicted.status is SearchStatus.COMPLETE
    assert conflicted.matches == ()
    assert unrelated_result.status is SearchStatus.COMPLETE


def test_conflict_resolution_requires_exact_current_heads_and_restores_entity(devices):
    _, source, device_a, device_b = devices
    base = _capability_event("login", device="seed", source=source)
    device_a.mutate(base)
    device_b.sync()
    device_b.runner.online = False
    device_b.mutate(_capability_event(
        "login", device="b", source=source, event_id="b-login-update",
        parent=base.event_id, summary="Device B",
    ))
    device_b.runner.online = True
    device_a.mutate(_capability_event(
        "login", device="a", source=source, event_id="a-login-update",
        parent=base.event_id, summary="Device A",
    ))
    with pytest.raises(SyncBlocked, match="authority_conflict"):
        device_b.sync()
    conflicted = device_b.replay()
    heads = conflicted.heads[("capability", "login")]
    before = len(device_b.store.load_all())
    chosen = _capability("login", source, "Resolved implementation")
    payload = asdict(chosen)
    payload["artifact_type"] = chosen.artifact_type.value
    payload["lifecycle"] = chosen.lifecycle.value

    with pytest.raises(SyncBlocked, match="conflict_heads_stale"):
        device_b.coordinator.resolve_conflict(
            entity_type="capability", entity_id="login", expected_head_ids=heads[:1],
            payload=payload, event_id="resolution-stale", occurred_at="2026-09-06T13:00:00Z",
        )
    assert len(device_b.store.load_all()) == before

    report = device_b.coordinator.resolve_conflict(
        entity_type="capability", entity_id="login", expected_head_ids=heads,
        payload=payload, event_id="resolution-exact", occurred_at="2026-09-06T13:00:00Z",
    )

    assert report.sync_state is SyncState.SYNCED
    assert device_b.replay().conflicts == ()
    assert device_b.repository.get_capability("login").summary == "Resolved implementation"


def test_offline_mutation_is_searchable_then_reaches_another_checkout(devices, embeddings):
    _, source, device_a, device_b = devices
    device_a.runner.online = False

    report = device_a.mutate(_capability_event("login", device="a", source=source))

    assert report.sync_state is SyncState.PENDING_SYNC
    local = CapabilitySearch(device_a.repository, embeddings).search(RequirementProfile(
        "req-login", "project", "login", category_hint=("code", "login"),
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


def test_connectivity_loss_during_pending_reconciliation_continues_offline_without_repush(
    devices,
):
    bare, source, device_a, _ = devices
    first = _capability_event("login", device="a", source=source)
    second = _capability_event("upload", device="a", source=source)
    device_a.runner.online = False
    first_report = device_a.mutate(first)
    first_commit = first_report.commit_id
    device_a.runner.online = True
    device_a.runner.reject_pushes = True
    device_a.runner.disconnect_after_rejected_push = True
    device_a.runner.calls.clear()

    report = device_a.mutate(second)

    assert report.sync_state is SyncState.PENDING_SYNC
    pushes = [call for call in device_a.runner.calls if call[:2] == ("git", "push")]
    assert len(pushes) == 1
    assert _run_git("merge-base", "--is-ancestor", first_commit, report.commit_id,
                    cwd=device_a.paths.checkout) == b""
    remote_paths = _run_git(
        "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main", "--", "events",
    ).decode().splitlines()
    assert remote_paths == []

    device_a.runner.reject_pushes = False
    device_a.runner.fail_fetches = False
    assert device_a.sync().sync_state is SyncState.SYNCED
    assert len(_run_git(
        "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main", "--", "events",
    ).decode().splitlines()) == 2


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


def test_invalid_typed_payload_fails_before_append_and_next_mutation_recovers(devices):
    _, source, device_a, _ = devices
    valid = _capability_event("login", device="a", source=source)
    invalid_payload = dict(valid.payload)
    invalid_payload.pop("name")
    invalid = AuthorityEvent.create(
        event_id="invalid-payload",
        device_id="device-a",
        entity_type="capability",
        entity_id="login",
        operation="registered",
        parent_event_ids=(),
        occurred_at="2026-09-06T12:00:00Z",
        payload=invalid_payload,
    )

    with pytest.raises(SyncBlocked, match="authority_projection_invalid"):
        device_a.mutate(invalid)

    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()
    assert device_a.store.load_all() == ()
    assert device_a.mutate(valid).sync_state is SyncState.SYNCED


def test_local_projector_failure_commits_event_before_projection_and_next_sync_recovers(devices):
    _, source, device_a, _ = devices
    event = _capability_event("login", device="a", source=source)

    def failing_projector(result, repository, provider):
        repository.set_metadata("partial-projector-write", True)
        raise RuntimeError("projector failed")

    device_a.coordinator.projector = failing_projector
    with pytest.raises(SyncBlocked, match="projection_failed") as failure:
        device_a.mutate(event)

    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()
    assert device_a.store.load_all() == (event,)
    assert failure.value.sync_state is SyncState.COMMITTED
    assert failure.value.event_set_digest == replay((event,)).digest
    assert device_a.repository.get_metadata("partial-projector-write") is None
    device_a.coordinator.projector = project_authority
    assert device_a.sync().sync_state is SyncState.SYNCED


def test_batch_append_failure_rolls_back_every_event_in_the_command(devices, monkeypatch):
    _, source, device_a, _ = devices
    events = (
        _capability_event("login", device="a", source=source),
        _capability_event("upload", device="a", source=source),
    )
    original_append = device_a.coordinator.store.append
    appended = 0

    def fail_second_append(event):
        nonlocal appended
        appended += 1
        if appended == 2:
            raise RuntimeError("second append failed")
        return original_append(event)

    monkeypatch.setattr(device_a.coordinator.store, "append", fail_second_append)

    with pytest.raises(SyncBlocked, match="materialization_failed"):
        device_a.coordinator.mutate_batch(lambda _: events)

    assert device_a.store.load_all() == ()
    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()


def test_migration_commit_failure_preserves_staged_journal_and_retries_cleanly(devices):
    _, source, device_a, device_b = devices
    legacy = device_b.repository
    legacy.upsert_capability(_capability("login", source), [0.0] * 384)
    before = legacy.authority_snapshot()
    device_a.runner.fail_commit_tree = True

    with pytest.raises(SyncBlocked, match="materialization_failed"):
        device_a.coordinator.migrate(legacy)

    assert device_a.store.load_all() == ()
    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()
    journal = device_a.paths.root / "migration-stage" / ".supermind-migration-v1.json"
    assert journal.is_file()
    journal_bytes = journal.read_bytes()
    device_a.runner.fail_commit_tree = False
    migrated, report = device_a.coordinator.migrate(legacy)
    assert report.sync_state is SyncState.SYNCED
    assert migrated.equivalent
    assert journal.read_bytes() == journal_bytes
    assert legacy.authority_snapshot() == before
    assert not (device_a.paths.checkout / journal.name).exists()
    assert device_a.repository.get_capability("login") is not None
    assert device_a.coordinator.migrate(legacy)[0] == migrated


def test_migration_reconciles_remote_before_enforcing_empty_target(devices):
    _, source, device_a, device_b = devices
    device_b.mutate(_capability_event("remote", device="b", source=source))
    with pytest.raises(SyncBlocked, match="migration_target_not_empty"):
        device_a.coordinator.migrate(device_a.repository)
    assert device_a.repository.get_capability("remote") is not None
    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()


def test_embedding_failure_commits_authority_then_projection_recovers_on_sync(devices):
    _, source, device_a, _ = devices
    event = _capability_event("login", device="a", source=source)
    healthy_embeddings = device_a.coordinator.embeddings

    class FailingEmbeddings:
        def embed_query(self, text):
            raise RuntimeError("embedding failed")

        def embed_documents(self, texts):
            raise RuntimeError("embedding failed")

    device_a.coordinator.embeddings = FailingEmbeddings()
    with pytest.raises(SyncBlocked, match="projection_failed"):
        device_a.mutate(event)

    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()
    assert device_a.store.load_all() == (event,)
    assert device_a.repository.list_capabilities() == ()
    device_a.coordinator.embeddings = healthy_embeddings
    assert device_a.sync().sync_state is SyncState.SYNCED


def test_local_renderer_partial_failure_never_touches_live_checkout_and_recovers(devices):
    _, source, device_a, _ = devices
    event = _capability_event("login", device="a", source=source)
    healthy_renderer = device_a.renderer

    class PartialRenderer:
        def render(self, result, checkout):
            (checkout / "partial-render.md").write_text("partial\n")
            raise RuntimeError("renderer failed")

    device_a.coordinator.renderer = PartialRenderer()
    with pytest.raises(SyncBlocked, match="render_failed"):
        device_a.mutate(event)

    assert not (device_a.paths.checkout / "partial-render.md").exists()
    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()
    assert device_a.store.load_all() == ()
    device_a.coordinator.renderer = healthy_renderer
    assert device_a.mutate(event).sync_state is SyncState.SYNCED


def test_git_commit_failure_restores_index_authority_projection_and_next_mutation(devices):
    _, source, device_a, _ = devices
    event = _capability_event("login", device="a", source=source)
    device_a.runner.fail_commit_tree = True

    with pytest.raises(SyncBlocked, match="materialization_failed"):
        device_a.mutate(event)

    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()
    assert device_a.store.load_all() == ()
    assert device_a.repository.list_capabilities() == ()
    device_a.runner.fail_commit_tree = False
    assert device_a.mutate(event).sync_state is SyncState.SYNCED


def test_commit_failure_restores_a_rendered_file_replaced_by_a_directory(devices):
    _, source, device_a, _ = devices

    class TopologyRenderer:
        nested = False

        def render(self, result, checkout):
            catalog = checkout / "catalog"
            if self.nested:
                catalog.unlink()
                catalog.mkdir()
                (catalog / "item.md").write_text("nested\n")
            else:
                catalog.write_text("original\n")

    renderer = TopologyRenderer()
    device_a.coordinator.renderer = renderer
    device_a.mutate(_capability_event("login", device="a", source=source))
    pending_event = _capability_event("upload", device="a", source=source)
    renderer.nested = True
    device_a.runner.fail_commit_tree = True

    with pytest.raises(SyncBlocked, match="materialization_failed"):
        device_a.mutate(pending_event)

    catalog = device_a.paths.checkout / "catalog"
    assert catalog.is_file()
    assert catalog.read_text() == "original\n"
    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()
    assert tuple(event.entity_id for event in device_a.store.load_all()) == ("login",)

    device_a.runner.fail_commit_tree = False
    assert device_a.mutate(pending_event).sync_state is SyncState.SYNCED
    assert (catalog / "item.md").read_text() == "nested\n"


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


def test_remote_projector_failure_commits_incoming_authority_and_next_sync_recovers(
    devices, tmp_path,
):
    bare, source, device_a, _ = devices
    event = _capability_event("login", device="remote", source=source)
    _commit_remote_event(bare, tmp_path / "remote-projector", event)

    def failing_projector(result, repository, provider):
        repository.set_metadata("partial-projector-write", True)
        raise RuntimeError("projector failed")

    device_a.coordinator.projector = failing_projector
    with pytest.raises(SyncBlocked, match="projection_failed"):
        device_a.sync()

    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()
    assert device_a.store.load_all() == (event,)
    assert device_a.repository.get_metadata("partial-projector-write") is None
    device_a.coordinator.projector = project_authority
    assert device_a.sync().sync_state is SyncState.SYNCED
    assert device_a.store.load_all() == (event,)


def test_invalid_remote_typed_payload_remains_visible_as_corruption_without_dirtying_local(
    devices, tmp_path,
):
    bare, source, device_a, _ = devices
    valid = _capability_event("login", device="remote", source=source)
    invalid_payload = dict(valid.payload)
    invalid_payload.pop("name")
    invalid = AuthorityEvent.create(
        event_id="invalid-remote-payload",
        device_id="device-remote",
        entity_type="capability",
        entity_id="login",
        operation="registered",
        parent_event_ids=(),
        occurred_at="2026-09-06T12:00:00Z",
        payload=invalid_payload,
    )
    _commit_remote_event(bare, tmp_path / "remote-invalid-payload", invalid)

    for _ in range(2):
        with pytest.raises(SyncBlocked, match="authority_projection_invalid"):
            device_a.sync()
        assert device_a.coordinator.git.status(device_a.paths.checkout) == ()
        assert device_a.store.load_all() == ()


def test_remote_renderer_partial_failure_never_touches_live_checkout_and_next_sync_recovers(
    devices, tmp_path,
):
    bare, source, device_a, _ = devices
    event = _capability_event("login", device="remote", source=source)
    _commit_remote_event(bare, tmp_path / "remote-renderer", event)
    healthy_renderer = device_a.renderer

    class PartialRenderer:
        def render(self, result, checkout):
            (checkout / "partial-render.md").write_text("partial\n")
            raise RuntimeError("renderer failed")

    device_a.coordinator.renderer = PartialRenderer()
    with pytest.raises(SyncBlocked, match="render_failed"):
        device_a.sync()

    assert not (device_a.paths.checkout / "partial-render.md").exists()
    assert device_a.coordinator.git.status(device_a.paths.checkout) == ()
    assert device_a.store.load_all() == ()
    device_a.coordinator.renderer = healthy_renderer
    assert device_a.sync().sync_state is SyncState.SYNCED
    assert device_a.store.load_all() == (event,)


def test_replaced_memory_root_blocks_before_creating_an_outside_lock_or_event(devices):
    _, source, device_a, _ = devices
    original_config = device_a.paths.config.read_bytes()
    outside_root = device_a.paths.root.parent / "outside-memory"
    device_a.paths.root.rename(outside_root)
    device_a.paths.root.symlink_to(outside_root, target_is_directory=True)

    with pytest.raises(SyncBlocked, match="repository_paths_invalid"):
        device_a.mutate(_capability_event("login", device="a", source=source))

    assert not (outside_root / "locks" / "sync.lock").exists()
    assert (outside_root / "config.json").read_bytes() == original_config
    assert EventStore(outside_root / "repository").load_all() == ()


def test_replaced_locks_directory_blocks_before_creating_an_outside_lock_or_event(devices):
    _, source, device_a, _ = devices
    original_config = device_a.paths.config.read_bytes()
    outside_locks = device_a.paths.root.parent / "outside-locks"
    device_a.paths.locks.rename(outside_locks)
    device_a.paths.locks.symlink_to(outside_locks, target_is_directory=True)

    with pytest.raises(SyncBlocked, match="repository_paths_invalid"):
        device_a.mutate(_capability_event("login", device="a", source=source))

    assert not (outside_locks / "sync.lock").exists()
    assert device_a.paths.config.read_bytes() == original_config
    assert device_a.store.load_all() == ()


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
