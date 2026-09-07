"""Safe Git and GitHub process boundaries for memory repository initialization."""

from __future__ import annotations

import json
import os
import re
import subprocess
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from supermind_memory.config import MemoryPaths, RepositoryConfig
from supermind_memory.event_model import EventValidationError, MemoryMarker
from supermind_memory.redaction import redact_text


_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38})/[A-Za-z0-9_.-]{1,100}\Z")
_BRANCH = re.compile(r"(?!.*(?:\.\.|//|@\{|\\|\s))(?!/)(?!.*[/\.]\Z)[A-Za-z0-9._/-]+\Z")


@dataclass(frozen=True)
class CompletedCommand:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], cwd: Path | None = None) -> CompletedCommand: ...


def credential_passthrough_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy the caller environment without manufacturing credential values."""
    return dict(os.environ if source is None else source)


class SubprocessCommandRunner:
    def __init__(self, *, env: Mapping[str, str] | None = None) -> None:
        self._env = env

    def run(self, argv: Sequence[str], cwd: Path | None = None) -> CompletedCommand:
        result = subprocess.run(
            tuple(argv),
            cwd=cwd,
            shell=False,
            check=False,
            text=False,
            env=credential_passthrough_env(self._env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return CompletedCommand(result.returncode, result.stdout, result.stderr)


class CommandFailed(RuntimeError):
    pass


class RepositoryInitBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class GitRepositoryRef:
    host: str
    owner: str
    name: str
    clone_url: str
    default_branch: str
    repository_id: str

    @property
    def name_with_owner(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def web_url(self) -> str:
        return f"https://{self.host}/{self.name_with_owner}"


@dataclass(frozen=True)
class InitRequest:
    repository: str
    device_id: str
    create_private: bool = False
    runner: CommandRunner | None = field(default=None, repr=False, compare=False)


def _diagnostic(result: CompletedCommand) -> str:
    raw = result.stderr or result.stdout
    return redact_text(raw.decode("utf-8", errors="replace"))


def _require_success(result: CompletedCommand, operation: str) -> CompletedCommand:
    if result.returncode != 0:
        raise CommandFailed(f"{operation}: {_diagnostic(result)}")
    return result


class GitClient:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner
        self._redacted_attempts: list[str] = []

    @property
    def redacted_attempts(self) -> tuple[str, ...]:
        return tuple(self._redacted_attempts)

    def fetch(self, checkout: Path, remote: str) -> None:
        _require_success(
            self._runner.run(("git", "fetch", "--prune", remote), cwd=checkout),
            "git_fetch_failed",
        )

    def fetch_branch(self, checkout: Path, remote: str, branch: str) -> bool:
        result = self._runner.run((
            "git", "fetch", "--prune", remote,
            f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
        ), cwd=checkout)
        if result.returncode == 0:
            return True
        self._redacted_attempts.append(f"git_fetch_failed: {_diagnostic(result)}")
        return False

    def clone(self, clone_url: str, checkout: Path) -> None:
        _require_success(
            self._runner.run(
                ("git", "clone", "--origin", "origin", "--", clone_url, str(checkout)),
                cwd=checkout.parent,
            ),
            "git_clone_failed",
        )

    def origin_urls(self, checkout: Path) -> tuple[str, ...]:
        result = _require_success(
            self._runner.run(("git", "remote", "get-url", "--all", "origin"), cwd=checkout),
            "git_remote_inspection_failed",
        )
        return tuple(line for line in _lines(result.stdout) if line)

    def origin_push_urls(self, checkout: Path) -> tuple[str, ...]:
        result = _require_success(
            self._runner.run(
                ("git", "remote", "get-url", "--push", "--all", "origin"), cwd=checkout,
            ),
            "git_remote_inspection_failed",
        )
        return tuple(line for line in _lines(result.stdout) if line)

    def branch(self, checkout: Path) -> str:
        result = _require_success(
            self._runner.run(("git", "symbolic-ref", "--quiet", "--short", "HEAD"), cwd=checkout),
            "git_branch_inspection_failed",
        )
        return result.stdout.decode("utf-8", errors="strict").strip()

    def status(self, checkout: Path) -> tuple[str, ...]:
        result = _require_success(
            self._runner.run(
                ("git", "status", "--porcelain=v1", "--untracked-files=all"), cwd=checkout,
            ),
            "git_status_failed",
        )
        return _lines(result.stdout)

    def has_head(self, checkout: Path) -> bool:
        result = self._runner.run(("git", "rev-parse", "--verify", "HEAD"), cwd=checkout)
        if result.returncode == 0:
            return True
        if result.returncode == 128:
            return False
        raise CommandFailed(f"git_head_inspection_failed: {_diagnostic(result)}")

    def head(self, checkout: Path) -> str:
        result = _require_success(
            self._runner.run(("git", "rev-parse", "--verify", "HEAD"), cwd=checkout),
            "git_head_inspection_failed",
        )
        revision = result.stdout.decode("ascii", errors="strict").strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
            raise RepositoryInitBlocked("repository_head_invalid")
        return revision

    def tracked_paths(self, checkout: Path) -> tuple[str, ...]:
        result = _require_success(
            self._runner.run(("git", "ls-tree", "-r", "--name-only", "HEAD"), cwd=checkout),
            "git_tree_inspection_failed",
        )
        return _lines(result.stdout)

    def tracked_modes(self, checkout: Path) -> tuple[tuple[str, str], ...]:
        result = _require_success(
            self._runner.run(
                ("git", "ls-files", "--stage", "--", "memory.json", "events"), cwd=checkout,
            ),
            "git_mode_inspection_failed",
        )
        modes = []
        for line in _lines(result.stdout):
            metadata, separator, path = line.partition("\t")
            fields = metadata.split()
            if not separator or len(fields) != 3:
                raise RepositoryInitBlocked("repository_tree_invalid")
            modes.append((fields[0], path))
        return tuple(modes)

    def add_marker(self, checkout: Path) -> None:
        _require_success(
            self._runner.run(("git", "add", "--", "memory.json"), cwd=checkout),
            "git_add_marker_failed",
        )

    def commit_marker(self, checkout: Path) -> None:
        _require_success(
            self._runner.run((
                "git", "-c", "user.name=Supermind Memory",
                "-c", "user.email=supermind-memory@users.noreply.github.com",
                "commit", "-m", "Initialize Supermind memory repository",
            ), cwd=checkout),
            "git_commit_marker_failed",
        )

    def push_branch(self, checkout: Path, remote: str, branch: str) -> None:
        _require_success(
            self._runner.run(("git", "push", "--set-upstream", remote, branch), cwd=checkout),
            "git_push_marker_failed",
        )

    def remote_head(self, checkout: Path, remote: str, branch: str) -> str | None:
        result = _require_success(
            self._runner.run(
                ("git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"), cwd=checkout,
            ),
            "git_remote_head_failed",
        )
        lines = _lines(result.stdout)
        if not lines:
            return None
        if len(lines) != 1:
            raise RepositoryInitBlocked("repository_branch_ambiguous")
        revision, separator, ref = lines[0].partition("\t")
        if not separator or ref != f"refs/heads/{branch}" or not re.fullmatch(r"[0-9a-f]{40,64}", revision):
            raise RepositoryInitBlocked("repository_remote_head_invalid")
        return revision

    def remote_refs(self, checkout: Path, remote: str) -> tuple[str, ...]:
        result = _require_success(
            self._runner.run(("git", "ls-remote", remote), cwd=checkout),
            "git_remote_ref_check_failed",
        )
        return _lines(result.stdout)

    def tree_entries(self, checkout: Path, ref: str, path: str) -> dict[str, tuple[str, str]]:
        result = _require_success(
            self._runner.run(("git", "ls-tree", "-r", "-z", ref, "--", path), cwd=checkout),
            "git_tree_inspection_failed",
        )
        entries: dict[str, tuple[str, str]] = {}
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            metadata, separator, raw_path = record.partition(b"\t")
            fields = metadata.split()
            if not separator or len(fields) != 3:
                raise RepositoryInitBlocked("repository_tree_invalid")
            try:
                mode, kind, object_id = (field.decode("ascii") for field in fields)
                decoded_path = raw_path.decode("utf-8")
            except UnicodeDecodeError as error:
                raise RepositoryInitBlocked("repository_tree_invalid") from error
            entries[decoded_path] = (mode, object_id)
            if kind != "blob":
                raise RepositoryInitBlocked("repository_tree_invalid")
        return entries

    def show_file(self, checkout: Path, ref: str, path: str) -> bytes:
        result = _require_success(
            self._runner.run(("git", "show", f"{ref}:{path}"), cwd=checkout),
            "git_object_read_failed",
        )
        return result.stdout

    def ref_head(self, checkout: Path, ref: str) -> str:
        result = _require_success(
            self._runner.run(("git", "rev-parse", "--verify", ref), cwd=checkout),
            "git_ref_inspection_failed",
        )
        revision = result.stdout.decode("ascii", errors="strict").strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
            raise RepositoryInitBlocked("repository_head_invalid")
        return revision

    def is_ancestor(self, checkout: Path, ancestor: str, descendant: str) -> bool:
        result = self._runner.run(
            ("git", "merge-base", "--is-ancestor", ancestor, descendant), cwd=checkout,
        )
        if result.returncode in (0, 1):
            return result.returncode == 0
        raise CommandFailed(f"git_ancestry_check_failed: {_diagnostic(result)}")

    def commit_all(
        self,
        checkout: Path,
        branch: str,
        remote_head: str,
        message: str,
    ) -> str:
        _require_success(
            self._runner.run(("git", "add", "--all", "--", "."), cwd=checkout),
            "git_stage_failed",
        )
        tree = _require_success(
            self._runner.run(("git", "write-tree"), cwd=checkout),
            "git_write_tree_failed",
        ).stdout.decode("ascii").strip()
        local_head = self.head(checkout)
        if self.is_ancestor(checkout, remote_head, local_head):
            parents = (local_head,)
        elif self.is_ancestor(checkout, local_head, remote_head):
            parents = (remote_head,)
        else:
            parents = (local_head, remote_head)
        parent_tree = self.ref_head(checkout, f"{parents[0]}^{{tree}}")
        if len(parents) == 1 and tree == parent_tree:
            commit_id = parents[0]
        else:
            parent_args = tuple(item for parent in parents for item in ("-p", parent))
            commit_id = _require_success(
                self._runner.run((
                    "git", "-c", "user.name=Supermind Memory",
                    "-c", "user.email=supermind-memory@users.noreply.github.com",
                    "commit-tree", tree, *parent_args, "-m", message,
                ), cwd=checkout),
                "git_commit_failed",
            ).stdout.decode("ascii").strip()
        _require_success(
            self._runner.run((
                "git", "update-ref", f"refs/heads/{branch}", commit_id, local_head,
            ), cwd=checkout),
            "git_update_ref_failed",
        )
        return commit_id

    def push(self, checkout: Path, remote: str, branch: str) -> bool:
        result = self._runner.run((
            "git", "push", remote,
            f"refs/heads/{branch}:refs/heads/{branch}",
        ), cwd=checkout)
        if result.returncode == 0:
            return True
        self._redacted_attempts.append(f"git_push_failed: {_diagnostic(result)}")
        return False

    def reset_index(self, checkout: Path) -> None:
        """Reset staging to HEAD without changing or deleting working-tree files."""
        _require_success(
            self._runner.run(("git", "reset", "--mixed", "HEAD", "--", "."), cwd=checkout),
            "git_index_reset_failed",
        )


def _lines(raw: bytes) -> tuple[str, ...]:
    try:
        return tuple(raw.decode("utf-8").splitlines())
    except UnicodeDecodeError as error:
        raise RepositoryInitBlocked("repository_command_output_invalid") from error


class GitHubClient:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def inspect_writable_private(self, repository: str) -> GitRepositoryRef:
        if not _REPOSITORY.fullmatch(repository):
            raise RepositoryInitBlocked("repository_identity_invalid")
        metadata_result = _require_success(
            self._runner.run((
                "gh", "repo", "view", repository, "--json",
                "id,nameWithOwner,isPrivate,defaultBranchRef,sshUrl,url",
            )),
            "github_metadata_failed",
        )
        try:
            metadata = json.loads(metadata_result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RepositoryInitBlocked("repository_metadata_invalid") from error
        if not isinstance(metadata, dict) or metadata.get("isPrivate") is not True:
            raise RepositoryInitBlocked("repository_not_private")
        if metadata.get("nameWithOwner") != repository:
            raise RepositoryInitBlocked("repository_identity_mismatch")
        permission = _require_success(
            self._runner.run((
                "gh", "api", f"repos/{repository}", "--jq", ".permissions.push",
            )),
            "repository_permission_check_failed",
        )
        if permission.stdout.strip() != b"true":
            raise RepositoryInitBlocked("repository_not_writable")
        branch_ref = metadata.get("defaultBranchRef")
        branch = branch_ref.get("name") if isinstance(branch_ref, dict) else None
        if branch is None or branch == "":
            branch_result = _require_success(
                self._runner.run((
                    "gh", "api", f"repos/{repository}", "--jq", ".default_branch",
                )),
                "repository_default_branch_check_failed",
            )
            try:
                branch = branch_result.stdout.decode("utf-8", errors="strict").strip()
            except UnicodeDecodeError as error:
                raise RepositoryInitBlocked("repository_branch_ambiguous") from error
        if not isinstance(branch, str) or not _BRANCH.fullmatch(branch):
            raise RepositoryInitBlocked("repository_branch_ambiguous")
        repository_id = metadata.get("id")
        clone_url = metadata.get("sshUrl")
        web_url = metadata.get("url")
        parsed = urlparse(web_url) if isinstance(web_url, str) else None
        if (
            not isinstance(repository_id, str) or not repository_id
            or not isinstance(clone_url, str) or not clone_url
            or parsed is None or parsed.scheme != "https" or parsed.hostname != "github.com"
            or parsed.path.rstrip("/") != f"/{repository}"
        ):
            raise RepositoryInitBlocked("repository_metadata_invalid")
        owner, name = repository.split("/", 1)
        return GitRepositoryRef(
            host="github.com",
            owner=owner,
            name=name,
            clone_url=clone_url,
            default_branch=branch,
            repository_id=repository_id,
        )

    def create_private(self, repository: str) -> GitRepositoryRef:
        if not _REPOSITORY.fullmatch(repository):
            raise RepositoryInitBlocked("repository_identity_invalid")
        _require_success(
            self._runner.run((
                "gh", "repo", "create", repository, "--private", "--confirm",
            )),
            "repository_create_failed",
        )
        return self.inspect_writable_private(repository)


def initialize_repository(request: InitRequest, paths: MemoryPaths) -> RepositoryConfig:
    validate_memory_paths(paths)
    runner = request.runner or SubprocessCommandRunner()
    github = GitHubClient(runner)
    repository = (
        github.create_private(request.repository)
        if request.create_private
        else github.inspect_writable_private(request.repository)
    )
    device_id = _stable_device_id(paths.config, repository, request.device_id)
    git = GitClient(runner)
    paths.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlink(paths.root)
    if paths.root.resolve() != paths.root.absolute():
        raise RepositoryInitBlocked("repository_paths_invalid")
    attached = paths.checkout.exists() or paths.checkout.is_symlink()
    if attached:
        _validate_attached_checkout(git, paths.checkout, repository)
        git.fetch(paths.checkout, "origin")
    else:
        try:
            git.clone(repository.clone_url, paths.checkout)
        except CommandFailed as error:
            raise RepositoryInitBlocked(str(error)) from error
        _validate_checkout_identity(git, paths.checkout, repository)

    empty = not git.has_head(paths.checkout)
    if empty:
        if git.remote_refs(paths.checkout, "origin"):
            raise RepositoryInitBlocked("repository_not_empty")
        marker = MemoryMarker.create(
            renderer_version="0.1.0",
            repository_id=repository.repository_id,
            default_branch=repository.default_branch,
        )
        (paths.checkout / "memory.json").write_bytes(marker.to_bytes())
        git.add_marker(paths.checkout)
        git.commit_marker(paths.checkout)
        git.push_branch(paths.checkout, "origin", repository.default_branch)
    else:
        _validate_repository_tree(git, paths.checkout, repository)

    remote_head = git.remote_head(paths.checkout, "origin", repository.default_branch)
    if remote_head is None:
        raise RepositoryInitBlocked("repository_remote_head_missing")
    if attached and git.head(paths.checkout) != remote_head:
        raise RepositoryInitBlocked("repository_checkout_stale")
    config = RepositoryConfig(
        repository_id=repository.repository_id,
        repository=repository.name_with_owner,
        web_url=repository.web_url,
        clone_url=repository.clone_url,
        branch=repository.default_branch,
        device_id=device_id,
        protocol_version=1,
        authority_mode="events-v1",
        last_checked_remote_head=remote_head,
    )
    config.write(paths.config)
    return config


def _validate_attached_checkout(
    git: GitClient,
    checkout: Path,
    repository: GitRepositoryRef,
) -> None:
    if checkout.is_symlink() or not checkout.is_dir():
        raise RepositoryInitBlocked("repository_checkout_unsafe")
    git_path = checkout / ".git"
    if git_path.is_symlink():
        raise RepositoryInitBlocked("repository_symlink_unsafe")
    if not git_path.is_dir():
        raise RepositoryInitBlocked("repository_git_path_unsafe")
    if git.status(checkout):
        raise RepositoryInitBlocked("repository_checkout_dirty")
    _validate_checkout_identity(git, checkout, repository)


def validate_memory_paths(paths: MemoryPaths) -> None:
    root = paths.root.absolute()
    expected = {
        "config": root / "config.json",
        "checkout": root / "repository",
        "database": root / "derived" / "database",
        "model_cache": root / "model-cache",
        "locks": root / "locks",
        "generations": root / "derived" / "generations",
        "runtime": root / "derived" / "runtime",
    }
    if root.name != "memory" or root.parent.name != "supermind":
        raise RepositoryInitBlocked("repository_paths_invalid")
    for name, expected_path in expected.items():
        if getattr(paths, name).absolute() != expected_path:
            raise RepositoryInitBlocked("repository_paths_invalid")
    owned_directories = {
        root.parent,
        root,
        paths.checkout,
        paths.database.parent,
        paths.database,
        paths.model_cache,
        paths.locks,
        paths.generations,
        paths.runtime,
    }
    for path in owned_directories:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir() or path.resolve() != path.absolute():
                raise RepositoryInitBlocked("repository_paths_invalid")
    if paths.config.is_symlink():
        raise RepositoryInitBlocked("repository_paths_invalid")


def _validate_checkout_identity(
    git: GitClient,
    checkout: Path,
    repository: GitRepositoryRef,
) -> None:
    urls = git.origin_urls(checkout)
    push_urls = git.origin_push_urls(checkout)
    if urls != (repository.clone_url,) or push_urls != (repository.clone_url,):
        raise RepositoryInitBlocked("repository_remote_ambiguous")
    if git.branch(checkout) != repository.default_branch:
        raise RepositoryInitBlocked("repository_branch_ambiguous")


def _validate_repository_tree(
    git: GitClient,
    checkout: Path,
    repository: GitRepositoryRef,
) -> None:
    tracked = git.tracked_paths(checkout)
    if "memory.json" not in tracked:
        raise RepositoryInitBlocked("repository_marker_missing")
    for mode, path in git.tracked_modes(checkout):
        if mode == "120000" and (path == "memory.json" or path == "events" or path.startswith("events/")):
            raise RepositoryInitBlocked("repository_symlink_unsafe")
    _reject_repository_symlinks(checkout)
    try:
        marker = MemoryMarker.from_bytes((checkout / "memory.json").read_bytes())
    except (OSError, EventValidationError) as error:
        raise RepositoryInitBlocked("repository_marker_invalid") from error
    if marker.repository_id != repository.repository_id:
        raise RepositoryInitBlocked("repository_id_mismatch")
    if marker.default_branch != repository.default_branch:
        raise RepositoryInitBlocked("repository_branch_mismatch")


def _reject_repository_symlinks(checkout: Path) -> None:
    marker = checkout / "memory.json"
    if marker.is_symlink():
        raise RepositoryInitBlocked("repository_symlink_unsafe")
    events = checkout / "events"
    if not events.exists() and not events.is_symlink():
        return
    _reject_symlink(events)
    for root, directories, files in os.walk(events, followlinks=False):
        base = Path(root)
        for name in (*directories, *files):
            _reject_symlink(base / name)


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise RepositoryInitBlocked("repository_symlink_unsafe")


def _sanitize_device_id(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    sanitized = re.sub(r"[^a-z0-9._-]+", "-", normalized).strip("._-")[:64].rstrip("._-")
    if not sanitized:
        raise RepositoryInitBlocked("device_id_invalid")
    return sanitized


def _stable_device_id(path: Path, repository: GitRepositoryRef, requested: str) -> str:
    if path.is_symlink():
        raise RepositoryInitBlocked("repository_config_unsafe")
    if path.exists():
        try:
            current = RepositoryConfig.read(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise RepositoryInitBlocked("repository_config_invalid") from error
        if (
            current.repository_id != repository.repository_id
            or current.repository != repository.name_with_owner
            or current.branch != repository.default_branch
            or current.clone_url != repository.clone_url
            or current.web_url != repository.web_url
        ):
            raise RepositoryInitBlocked("repository_config_mismatch")
        return current.device_id
    return _sanitize_device_id(requested)
