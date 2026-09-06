from __future__ import annotations

import json
import stat
import subprocess
from dataclasses import fields
from pathlib import Path

import pytest

from supermind_memory.config import RepositoryConfig
from supermind_memory.git_client import (
    CommandFailed,
    CompletedCommand,
    GitClient,
    GitHubClient,
    SubprocessCommandRunner,
    GitRepositoryRef,
)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []
        self.shell_was_used = False

    def run(self, argv, cwd=None):
        self.calls.append((tuple(argv), cwd))
        return CompletedCommand(0, b"", b"")


def test_git_fetch_uses_an_argument_vector_without_shell_text(tmp_path):
    runner = RecordingRunner()
    checkout = tmp_path / "memory checkout"

    GitClient(runner).fetch(checkout, "origin")

    assert runner.calls == [(('git', 'fetch', '--prune', 'origin'), checkout)]
    assert runner.shell_was_used is False


def test_git_index_reset_preserves_worktree_through_an_argument_vector(tmp_path):
    runner = RecordingRunner()
    checkout = tmp_path / "memory checkout"

    GitClient(runner).reset_index(checkout)

    assert runner.calls == [
        (("git", "reset", "--mixed", "HEAD", "--", "."), checkout),
    ]
    assert runner.shell_was_used is False


def test_subprocess_runner_disables_shell_and_preserves_captured_bytes(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, b"output", b"warning")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = SubprocessCommandRunner(env={"PATH": "/usr/bin", "GH_CONFIG_DIR": "/auth"})

    result = runner.run(("git", "status", "--short"), tmp_path)

    assert seen == {
        "argv": ("git", "status", "--short"),
        "kwargs": {
            "cwd": tmp_path,
            "shell": False,
            "check": False,
            "text": False,
            "env": {"PATH": "/usr/bin", "GH_CONFIG_DIR": "/auth"},
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        },
    }
    assert result == CompletedCommand(0, b"output", b"warning")


def test_command_failure_redacts_captured_credentials(tmp_path):
    credential = "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890"
    class FailingRunner:
        def run(self, argv, cwd=None):
            return CompletedCommand(
                1,
                b"",
                f"Authorization: Bearer {credential}".encode(),
            )

    with pytest.raises(CommandFailed) as raised:
        GitClient(FailingRunner()).fetch(tmp_path, "origin")

    assert credential not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


def test_github_metadata_and_write_access_use_exact_argument_vectors():
    class GitHubRunner(RecordingRunner):
        def run(self, argv, cwd=None):
            self.calls.append((tuple(argv), cwd))
            if tuple(argv[:3]) == ("gh", "repo", "view"):
                return CompletedCommand(
                    0,
                    json.dumps({
                        "id": "R_private",
                        "nameWithOwner": "owner/memory",
                        "isPrivate": True,
                        "defaultBranchRef": {"name": "main"},
                        "sshUrl": "git@github.com:owner/memory.git",
                        "url": "https://github.com/owner/memory",
                    }).encode(),
                    b"",
                )
            return CompletedCommand(0, b"true\n", b"")

    runner = GitHubRunner()

    repository = GitHubClient(runner).inspect_writable_private("owner/memory")

    assert repository.repository_id == "R_private"
    assert runner.calls == [
        ((
            "gh", "repo", "view", "owner/memory", "--json",
            "id,nameWithOwner,isPrivate,defaultBranchRef,sshUrl,url",
        ), None),
        (("gh", "api", "repos/owner/memory", "--jq", ".permissions.push"), None),
    ]


def test_repository_config_persists_only_non_secret_fields_with_private_permissions(tmp_path):
    path = tmp_path / "nested" / "config.json"
    config = RepositoryConfig(
        repository_id="R_private",
        repository="owner/memory",
        web_url="https://github.com/owner/memory",
        clone_url="git@github.com:owner/memory.git",
        branch="main",
        device_id="work-laptop",
        protocol_version=1,
        authority_mode="events-v1",
        last_checked_remote_head="a" * 40,
    )

    config.write(path)

    assert json.loads(path.read_bytes()) == {
        "authority_mode": "events-v1",
        "branch": "main",
        "clone_url": "git@github.com:owner/memory.git",
        "device_id": "work-laptop",
        "last_checked_remote_head": "a" * 40,
        "protocol_version": 1,
        "repository": "owner/memory",
        "repository_id": "R_private",
        "web_url": "https://github.com/owner/memory",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert RepositoryConfig.read(path) == config


def test_repository_reference_has_only_the_stable_remote_identity_fields():
    assert tuple(field.name for field in fields(GitRepositoryRef)) == (
        "host", "owner", "name", "clone_url", "default_branch", "repository_id",
    )
