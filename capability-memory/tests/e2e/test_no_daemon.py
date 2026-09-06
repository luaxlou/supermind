"""Each real CLI invocation exits without children or listening sockets."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

# Also executable as a fresh Python process, with only external ports replaced.
TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from conftest import KeywordEmbeddingProvider
from e2e.test_distributed_bootstrap import (
    RecordingRunner, login_payload, login_requirement, release_acceptance_objects, run_cli,
)
from integration.test_sync import _run_git


def process_table():
    process = subprocess.Popen(
        ["ps", "-axo", "pid=,ppid=,pgid="], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    return {int(pid): (int(parent), int(group)) for pid, parent, group in (
        line.split() for line in stdout.splitlines()
    ) if int(pid) != process.pid}


def descendants(table, parent):
    found = set()
    pending = {parent}
    while pending:
        pending = {pid for pid, (ppid, _) in table.items() if ppid in pending} - found
        found.update(pending)
    return found


def owned_sockets():
    lsof = shutil.which("lsof")
    if lsof is None:
        return None
    sockets = set()
    for selectors in (("-iTCP", "-sTCP:LISTEN"), ("-U",)):
        result = subprocess.run(
            [lsof, "-a", "-p", str(os.getpid()), *selectors, "-Fn"],
            capture_output=True, text=True,
        )
        assert result.returncode in (0, 1), result.stderr
        # Darwin reports connected anonymous socket pairs as ->0x...; Lance's
        # async runtime uses those for wakeups, and they are not listeners.
        sockets.update(line for line in result.stdout.splitlines()
                       if line.startswith("n") and not line.startswith("n->"))
    return sockets


def worker():
    home, bare, *arguments = sys.argv[1:]
    runner = RecordingRunner(Path(bare))
    before_children = descendants(process_table(), os.getpid())
    before_sockets = owned_sockets()
    envelope = run_cli(Path(home), runner, KeywordEmbeddingProvider(), *arguments)
    after_children = descendants(process_table(), os.getpid())
    after_sockets = owned_sockets()
    assert not after_children - before_children
    if before_sockets is not None:
        assert not after_sockets - before_sockets, sorted(after_sockets - before_sockets)
    print(json.dumps({
        "envelope": envelope, "opened": runner.opened,
        "children_before": sorted(before_children), "children_after": sorted(after_children),
        "sockets_checked": before_sockets is not None,
    }))


def test_all_public_command_boundaries_exit_without_daemons(tmp_path):
    bare = tmp_path / "memory.git"
    _run_git("init", "--bare", "--initial-branch=main", str(bare))
    source = tmp_path / "login.py"
    source.write_text("def login(phone): return phone\n")
    registration = tmp_path / "registration.json"
    registration.write_text(json.dumps(login_payload(source)))
    requirement = tmp_path / "requirement.json"
    requirement.write_text(json.dumps(login_requirement()))
    commands = (
        ("init", "--repo", "owner/memory"),
        ("register", "--input", str(registration)),
        ("sync",), ("search", "--input", str(requirement)), ("render",), ("open",),
    )
    for command in commands:
        baseline = process_table()
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(tmp_path / "home"), str(bare), *command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise AssertionError(f"{command[0]} failed to exit")
        assert process.returncode == 0, (command, stdout, stderr)
        remaining = process_table()
        assert not {pid for pid, (_, group) in remaining.items() if group == process.pid}
        assert not (descendants(remaining, os.getpid()) - descendants(baseline, os.getpid()))
        evidence = json.loads(stdout)
        assert evidence["children_after"] == evidence["children_before"]
        if shutil.which("lsof"):
            assert evidence["sockets_checked"]
        if command[0] == "open":
            assert evidence["opened"] == [["gh", "repo", "view", "owner/memory", "--web"]]
        else:
            assert evidence["opened"] == []


if __name__ == "__main__":
    worker()
