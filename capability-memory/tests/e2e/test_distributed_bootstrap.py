"""Offline acceptance rehearsal through the production JSON CLI boundary."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
import gc
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from integration.test_sync import LocalGitHubRunner, _capability, _run_git
from supermind_memory import cli
from supermind_memory.config import MemoryPaths
from supermind_memory.event_store import EventStore
from supermind_memory.replay import replay
from supermind_memory.repository import CapabilityRepository
from supermind_memory.types import Evidence


@pytest.fixture(autouse=True)
def release_acceptance_objects():
    yield
    # Release collected CLI graphs before the shared session fixture closes
    # LanceDB's process-global event loop and its native resources.
    gc.collect()


class RecordingRunner(LocalGitHubRunner):
    """Run actual local Git; metadata and the external browser are recorded doubles."""

    def __init__(self, bare):
        super().__init__(bare)
        self.opened = []

    def run(self, argv, cwd=None):
        if tuple(argv[:3]) == ("gh", "repo", "view") and "--web" in argv:
            self.opened.append(tuple(argv))
        return super().run(argv, cwd)


def run_cli(home, runner, embeddings, *arguments):
    output = io.StringIO()
    errors = io.StringIO()
    with patch.object(cli, "SubprocessCommandRunner", return_value=runner), patch.object(
        cli, "FastEmbedProvider", return_value=embeddings,
    ), redirect_stdout(output), redirect_stderr(errors):
        code = cli.main(["--data-home", str(home), *arguments, "--format", "json"])
    envelope = json.loads(output.getvalue() or errors.getvalue())
    assert code == 0, envelope
    return envelope


def login_payload(source):
    capability = replace(
        _capability("phone-login", source), name="手机号登录",
        summary="Reusable mobile phone authentication and login session",
        facets=("authentication", "login", "phone"),
        contract="Phone verification creates a session",
    )
    evidence = Evidence(
        "login-proof", capability.id, "source-project", "verification", "passed",
        None, None, 1.0, "2026-09-06T12:00:00Z", None,
    )
    return {"capability": asdict(capability), "evidence": [asdict(evidence)]}


def login_requirement():
    return {"id": "phone-demand", "project_id": "new-project", "intent": "手机号登录"}


def test_bootstrap_clone_rebuild_and_readme_are_equivalent(tmp_path, embeddings):
    bare = tmp_path / "memory.git"
    _run_git("init", "--bare", "--initial-branch=main", str(bare))
    runner = RecordingRunner(bare)
    source = tmp_path / "login.py"
    source.write_text("def login(phone): return phone\n")
    payload = login_payload(source)
    legacy_home = tmp_path / "legacy"
    # This is the embedded layout at 3a69857, before standalone MemoryPaths.
    legacy_root = legacy_home / "supermind" / "capability-memory"
    legacy_database = legacy_root / "database"
    legacy_writer_lock = legacy_root / "locks" / "writer.lock"
    with CapabilityRepository.open(legacy_database, legacy_writer_lock) as legacy:
        legacy.initialize()
        legacy.upsert_capability(cli._capability(payload["capability"]), [0.0] * 384)
        legacy.append_evidence(cli._evidence(payload["evidence"][0]))
        original = legacy.authority_snapshot()

    first_home, second_home = tmp_path / "first", tmp_path / "second"
    migrated = run_cli(
        first_home, runner, embeddings, "migrate", "--repo", "owner/memory",
        "--legacy-data-home", str(legacy_home),
    )
    assert migrated["sync_state"] == "synced"
    assert migrated["result"]["migration"]["equivalent"] is True
    first = MemoryPaths.from_codex_home(first_home)
    second = MemoryPaths.from_codex_home(second_home)
    tracked = _run_git("--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main").decode().splitlines()
    assert any(name.startswith("events/v1/") for name in tracked)
    assert all(not any(part in name.lower() for part in (
        ".lance", "model-cache", "generations/", "database/", ".venv/",
    )) for name in tracked)
    assert not second.database.exists()
    run_cli(second_home, runner, embeddings, "init", "--repo", "owner/memory")
    assert run_cli(second_home, runner, embeddings, "health")["result"]["healthy"] is True

    first_replay = replay(EventStore(first.checkout).load_all())
    second_replay = replay(EventStore(second.checkout).load_all())
    assert first_replay.digest == second_replay.digest == migrated["event_set_digest"]
    with CapabilityRepository.open(first.database) as a, CapabilityRepository.open(second.database) as b:
        assert a.authority_snapshot() == b.authority_snapshot() == original
        assert a.get_metadata("authority_materialized_digest") == b.get_metadata("authority_materialized_digest")
        assert a.get_metadata("authority_materialized_digest")
    requirement = tmp_path / "requirement.json"
    requirement.write_text(json.dumps(login_requirement(), ensure_ascii=False))
    searches = [run_cli(home, runner, embeddings, "search", "--input", str(requirement))["result"] for home in (first_home, second_home)]
    assert all(result["status"] == "complete" for result in searches)
    assert searches[0]["matches"] == searches[1]["matches"]
    assert searches[1]["matches"][0]["capability_id"] == "phone-login"
    assert searches[1]["matches"][0]["vector_score"] > 0
    readme = (second.checkout / "README.md").read_text()
    assert "手机号登录" in readme
    assert readme.count("<details>") == readme.count("</details>") >= 7
    assert readme == (first.checkout / "README.md").read_text()
    manifests = [json.loads((paths.checkout / ".supermind/render-manifest.json").read_text()) for paths in (first, second)]
    assert manifests[0] == manifests[1]
    with CapabilityRepository.open(legacy_database, legacy_writer_lock) as legacy:
        assert legacy.authority_snapshot() == original
    assert not MemoryPaths.from_codex_home(legacy_home).root.exists()
