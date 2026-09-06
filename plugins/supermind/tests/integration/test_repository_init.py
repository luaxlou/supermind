from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from supermind_memory.config import MemoryPaths, RepositoryConfig
from supermind_memory.event_model import MemoryMarker
from supermind_memory.git_client import (
    CompletedCommand,
    InitRequest,
    RepositoryInitBlocked,
    SubprocessCommandRunner,
    initialize_repository,
)


class MetadataRunner:
    def __init__(self, visibility):
        self.visibility = visibility

    def run(self, argv, cwd=None):
        assert tuple(argv[:3]) == ("gh", "repo", "view")
        metadata = {
            "id": "R_private",
            "nameWithOwner": "owner/memory",
            "isPrivate": self.visibility == "PRIVATE",
            "defaultBranchRef": {"name": "main"},
            "sshUrl": "git@github.com:owner/memory.git",
            "url": "https://github.com/owner/memory",
        }
        return CompletedCommand(0, json.dumps(metadata).encode(), b"")


@pytest.mark.parametrize("visibility", ["PUBLIC", "INTERNAL", None])
def test_connect_rejects_a_repository_not_reported_private(tmp_path, visibility):
    request = InitRequest(
        repository="owner/memory",
        device_id="device-a",
        runner=MetadataRunner(visibility),
    )

    with pytest.raises(RepositoryInitBlocked, match="repository_not_private"):
        initialize_repository(request, MemoryPaths.from_codex_home(tmp_path))


class LocalGitHubRunner:
    def __init__(
        self,
        bare_repository: Path,
        *,
        repository_id: str = "R_private",
        reported_repository: str = "owner/memory",
        private: bool = True,
        writable: bool = True,
        branch: str | None = "main",
        rest_branch: str | None = None,
    ) -> None:
        self.bare_repository = bare_repository
        self.repository_id = repository_id
        self.reported_repository = reported_repository
        self.private = private
        self.writable = writable
        self.branch = branch
        self.rest_branch = branch if rest_branch is None else rest_branch
        self.calls: list[tuple[str, ...]] = []
        self._git = SubprocessCommandRunner()

    def run(self, argv, cwd=None):
        call = tuple(argv)
        self.calls.append(call)
        if call[:3] == ("gh", "repo", "create"):
            return CompletedCommand(0, b"", b"")
        if call[:3] == ("gh", "repo", "view"):
            metadata = {
                "id": self.repository_id,
                "nameWithOwner": self.reported_repository,
                "isPrivate": self.private,
                "defaultBranchRef": None if self.branch is None else {"name": self.branch},
                "sshUrl": str(self.bare_repository),
                "url": f"https://github.com/{self.reported_repository}",
            }
            return CompletedCommand(0, json.dumps(metadata).encode(), b"")
        if call[:2] == ("gh", "api"):
            if call[-1] == ".default_branch":
                value = self.rest_branch or ""
                return CompletedCommand(0, f"{value}\n".encode(), b"")
            return CompletedCommand(0, b"true\n" if self.writable else b"false\n", b"")
        return self._git.run(call, cwd)


def _run_git(*argv: str, cwd: Path | None = None) -> bytes:
    result = subprocess.run(
        ("git", *argv), cwd=cwd, shell=False, check=False, text=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def _bare_repository(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    _run_git("init", "--bare", "--initial-branch=main", str(bare))
    return bare


def _populate_repository(
    tmp_path: Path,
    bare: Path,
    files: dict[str, bytes | tuple[str, str]],
) -> str:
    seed = tmp_path / "seed"
    _run_git("init", "--initial-branch=main", str(seed))
    for relative, value in files.items():
        target = seed / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, tuple):
            kind, link_target = value
            assert kind == "symlink"
            target.symlink_to(link_target)
        else:
            target.write_bytes(value)
    _run_git("add", "--all", cwd=seed)
    _run_git(
        "-c", "user.name=Supermind Test", "-c", "user.email=supermind@example.invalid",
        "commit", "-m", "seed", cwd=seed,
    )
    _run_git("remote", "add", "origin", str(bare), cwd=seed)
    _run_git("push", "-u", "origin", "main", cwd=seed)
    return _run_git("rev-parse", "HEAD", cwd=seed).decode().strip()


def _marker(repository_id: str = "R_private", branch: str = "main") -> bytes:
    return MemoryMarker.create(
        renderer_version="0.1.0",
        repository_id=repository_id,
        default_branch=branch,
    ).to_bytes()


def _request(runner: LocalGitHubRunner, *, create_private: bool = False) -> InitRequest:
    return InitRequest(
        repository="owner/memory",
        device_id="John's Work Laptop",
        create_private=create_private,
        runner=runner,
    )


def test_connect_initializes_a_genuinely_empty_remote_and_writes_private_config(tmp_path):
    bare = _bare_repository(tmp_path)
    runner = LocalGitHubRunner(bare)
    paths = MemoryPaths.from_codex_home(tmp_path / "data home")

    config = initialize_repository(_request(runner), paths)

    remote_head = _run_git("--git-dir", str(bare), "rev-parse", "refs/heads/main").decode().strip()
    remote_marker = _run_git("--git-dir", str(bare), "show", "main:memory.json")
    assert MemoryMarker.from_bytes(remote_marker).repository_id == "R_private"
    assert config == RepositoryConfig(
        repository_id="R_private",
        repository="owner/memory",
        web_url="https://github.com/owner/memory",
        clone_url=str(bare),
        branch="main",
        device_id="john-s-work-laptop",
        protocol_version=1,
        authority_mode="events-v1",
        last_checked_remote_head=remote_head,
    )
    assert RepositoryConfig.read(paths.config) == config
    assert paths.checkout.is_dir()


def test_connect_empty_uses_rest_default_branch_when_no_ref_exists(tmp_path):
    bare = _bare_repository(tmp_path)
    runner = LocalGitHubRunner(bare, branch=None, rest_branch="main")
    paths = MemoryPaths.from_codex_home(tmp_path / "data-home")

    config = initialize_repository(_request(runner), paths)

    assert config.branch == "main"
    assert ("gh", "api", "repos/owner/memory", "--jq", ".default_branch") in runner.calls
    assert _run_git("--git-dir", str(bare), "show", "main:memory.json") == _marker()


def test_create_private_empty_uses_rest_default_branch_when_no_ref_exists(tmp_path):
    bare = _bare_repository(tmp_path)
    runner = LocalGitHubRunner(bare, branch=None, rest_branch="main")
    paths = MemoryPaths.from_codex_home(tmp_path / "data-home")

    config = initialize_repository(_request(runner, create_private=True), paths)

    assert config.branch == "main"
    assert runner.calls[0] == (
        "gh", "repo", "create", "owner/memory", "--private", "--confirm",
    )
    assert ("gh", "api", "repos/owner/memory", "--jq", ".default_branch") in runner.calls


def test_create_uses_private_flag_then_independently_checks_metadata_and_access(tmp_path):
    bare = _bare_repository(tmp_path)
    runner = LocalGitHubRunner(bare)

    initialize_repository(
        _request(runner, create_private=True),
        MemoryPaths.from_codex_home(tmp_path / "data-home"),
    )

    assert runner.calls[:3] == [
        ("gh", "repo", "create", "owner/memory", "--private", "--confirm"),
        (
            "gh", "repo", "view", "owner/memory", "--json",
            "id,nameWithOwner,isPrivate,defaultBranchRef,sshUrl,url",
        ),
        ("gh", "api", "repos/owner/memory", "--jq", ".permissions.push"),
    ]


def test_create_rejects_when_the_independent_metadata_check_is_not_private(tmp_path):
    bare = _bare_repository(tmp_path)
    runner = LocalGitHubRunner(bare, private=False)

    with pytest.raises(RepositoryInitBlocked, match="repository_not_private"):
        initialize_repository(
            _request(runner, create_private=True),
            MemoryPaths.from_codex_home(tmp_path / "data-home"),
        )

    assert runner.calls[:2] == [
        ("gh", "repo", "create", "owner/memory", "--private", "--confirm"),
        (
            "gh", "repo", "view", "owner/memory", "--json",
            "id,nameWithOwner,isPrivate,defaultBranchRef,sshUrl,url",
        ),
    ]


def test_connect_accepts_a_compatible_marked_repository(tmp_path):
    bare = _bare_repository(tmp_path)
    expected_head = _populate_repository(tmp_path, bare, {"memory.json": _marker()})
    paths = MemoryPaths.from_codex_home(tmp_path / "data-home")

    config = initialize_repository(_request(LocalGitHubRunner(bare)), paths)

    assert config.last_checked_remote_head == expected_head
    assert (paths.checkout / "memory.json").read_bytes() == _marker()


def test_connect_rejects_a_nonempty_unmarked_repository(tmp_path):
    bare = _bare_repository(tmp_path)
    _populate_repository(tmp_path, bare, {"README.md": b"unrelated\n"})

    with pytest.raises(RepositoryInitBlocked, match="repository_marker_missing"):
        initialize_repository(
            _request(LocalGitHubRunner(bare)),
            MemoryPaths.from_codex_home(tmp_path / "data-home"),
        )


def test_connect_rejects_a_remote_with_only_non_branch_refs(tmp_path):
    bare = _bare_repository(tmp_path)
    head = _populate_repository(tmp_path, bare, {"README.md": b"unrelated\n"})
    _run_git("--git-dir", str(bare), "tag", "retained-tag", head)
    _run_git("--git-dir", str(bare), "update-ref", "-d", "refs/heads/main")

    with pytest.raises(RepositoryInitBlocked, match="repository_not_empty"):
        initialize_repository(
            _request(LocalGitHubRunner(bare)),
            MemoryPaths.from_codex_home(tmp_path / "data-home"),
        )


def test_connect_rejects_a_dirty_attached_checkout(tmp_path):
    bare = _bare_repository(tmp_path)
    _populate_repository(tmp_path, bare, {"memory.json": _marker()})
    paths = MemoryPaths.from_codex_home(tmp_path / "data-home")
    paths.root.mkdir(parents=True)
    _run_git("clone", str(bare), str(paths.checkout))
    (paths.checkout / "untracked.txt").write_text("local work")

    with pytest.raises(RepositoryInitBlocked, match="repository_checkout_dirty"):
        initialize_repository(_request(LocalGitHubRunner(bare)), paths)


def test_connect_rejects_a_clean_but_stale_attached_checkout(tmp_path):
    bare = _bare_repository(tmp_path)
    _populate_repository(tmp_path, bare, {"memory.json": _marker()})
    paths = MemoryPaths.from_codex_home(tmp_path / "data-home")
    paths.root.mkdir(parents=True)
    _run_git("clone", str(bare), str(paths.checkout))
    updater = tmp_path / "updater"
    _run_git("clone", str(bare), str(updater))
    (updater / "README.md").write_text("new remote content\n")
    _run_git("add", "README.md", cwd=updater)
    _run_git(
        "-c", "user.name=Supermind Test", "-c", "user.email=supermind@example.invalid",
        "commit", "-m", "remote update", cwd=updater,
    )
    _run_git("push", "origin", "main", cwd=updater)

    with pytest.raises(RepositoryInitBlocked, match="repository_checkout_stale"):
        initialize_repository(_request(LocalGitHubRunner(bare)), paths)


def test_connect_rejects_an_attached_checkout_with_a_symlinked_git_path(tmp_path):
    bare = _bare_repository(tmp_path)
    _populate_repository(tmp_path, bare, {"memory.json": _marker()})
    paths = MemoryPaths.from_codex_home(tmp_path / "data-home")
    paths.root.mkdir(parents=True)
    _run_git("clone", str(bare), str(paths.checkout))
    real_git = paths.root / "relocated-git"
    (paths.checkout / ".git").rename(real_git)
    (paths.checkout / ".git").symlink_to(real_git, target_is_directory=True)

    with pytest.raises(RepositoryInitBlocked, match="repository_symlink_unsafe"):
        initialize_repository(_request(LocalGitHubRunner(bare)), paths)


def test_connect_rejects_a_tracked_symlink_in_the_event_tree(tmp_path):
    bare = _bare_repository(tmp_path)
    _populate_repository(tmp_path, bare, {
        "memory.json": _marker(),
        "events/v1/device-a/2026-09/event.json": ("symlink", "../../../../../outside.json"),
    })

    with pytest.raises(RepositoryInitBlocked, match="repository_symlink_unsafe"):
        initialize_repository(
            _request(LocalGitHubRunner(bare)),
            MemoryPaths.from_codex_home(tmp_path / "data-home"),
        )


def test_connect_rejects_a_marker_copied_from_another_repository(tmp_path):
    bare = _bare_repository(tmp_path)
    _populate_repository(tmp_path, bare, {"memory.json": _marker("R_other")})

    with pytest.raises(RepositoryInitBlocked, match="repository_id_mismatch"):
        initialize_repository(
            _request(LocalGitHubRunner(bare)),
            MemoryPaths.from_codex_home(tmp_path / "data-home"),
        )


def test_connect_rejects_metadata_for_a_different_repository(tmp_path):
    bare = _bare_repository(tmp_path)
    runner = LocalGitHubRunner(bare, reported_repository="owner/other")

    with pytest.raises(RepositoryInitBlocked, match="repository_identity_mismatch"):
        initialize_repository(
            _request(runner), MemoryPaths.from_codex_home(tmp_path / "data-home"),
        )


def test_connect_rejects_read_only_access(tmp_path):
    bare = _bare_repository(tmp_path)

    with pytest.raises(RepositoryInitBlocked, match="repository_not_writable"):
        initialize_repository(
            _request(LocalGitHubRunner(bare, writable=False)),
            MemoryPaths.from_codex_home(tmp_path / "data-home"),
        )


def test_connect_rejects_an_ambiguous_default_branch(tmp_path):
    bare = _bare_repository(tmp_path)

    with pytest.raises(RepositoryInitBlocked, match="repository_branch_ambiguous"):
        initialize_repository(
            _request(LocalGitHubRunner(bare, branch=None)),
            MemoryPaths.from_codex_home(tmp_path / "data-home"),
        )


def test_connect_rejects_multiple_origin_urls_on_an_attached_checkout(tmp_path):
    bare = _bare_repository(tmp_path)
    _populate_repository(tmp_path, bare, {"memory.json": _marker()})
    paths = MemoryPaths.from_codex_home(tmp_path / "data-home")
    paths.root.mkdir(parents=True)
    _run_git("clone", str(bare), str(paths.checkout))
    _run_git("remote", "set-url", "--add", "origin", str(tmp_path / "other.git"), cwd=paths.checkout)

    with pytest.raises(RepositoryInitBlocked, match="repository_remote_ambiguous"):
        initialize_repository(_request(LocalGitHubRunner(bare)), paths)


def test_connect_rejects_a_checkout_outside_the_exact_owned_layout(tmp_path):
    bare = _bare_repository(tmp_path)
    paths = MemoryPaths.from_codex_home(tmp_path / "data-home")
    outside = tmp_path / "outside-checkout"

    with pytest.raises(RepositoryInitBlocked, match="repository_paths_invalid"):
        initialize_repository(
            _request(LocalGitHubRunner(bare)), replace(paths, checkout=outside),
        )

    assert not outside.exists()


def test_connect_rejects_a_different_origin_push_url_on_an_attached_checkout(tmp_path):
    bare = _bare_repository(tmp_path)
    _populate_repository(tmp_path, bare, {"memory.json": _marker()})
    paths = MemoryPaths.from_codex_home(tmp_path / "data-home")
    paths.root.mkdir(parents=True)
    _run_git("clone", str(bare), str(paths.checkout))
    _run_git(
        "remote", "set-url", "--add", "--push", "origin", str(tmp_path / "other.git"),
        cwd=paths.checkout,
    )

    with pytest.raises(RepositoryInitBlocked, match="repository_remote_ambiguous"):
        initialize_repository(_request(LocalGitHubRunner(bare)), paths)
