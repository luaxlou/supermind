"""The plugin can only execute its pinned, owned standalone tool."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "plugins/supermind/scripts/capability-memory").exists())


@pytest.fixture
def launch(tmp_path):
    plugin = tmp_path / "plugin"
    shutil.copytree(ROOT / "plugins/supermind/scripts", plugin / "scripts")
    (plugin / "vendor").mkdir()
    wheel = plugin / "vendor/supermind_capability_memory-0.2.0-py3-none-any.whl"
    wheel.write_bytes(b"locked test wheel")
    lock = {"version": "0.2.0", "artifact": wheel.name,
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "protocol": ">=1,<2"}
    (plugin / "tool.lock.json").write_text(json.dumps(lock))
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "python3").symlink_to(sys.executable)
    uv = binary / "uv"
    uv.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
with open(os.environ["FAKE_UV_LOG"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_UV_FAIL"):
    sys.exit(1)
if "venv" in sys.argv:
    target = pathlib.Path(sys.argv[-1]) / "bin"
    target.mkdir(parents=True)
    (target / "python").symlink_to(sys.executable)
else:
    target = pathlib.Path(sys.argv[sys.argv.index("--python") + 1]).parent / "supermind-memory"
    target.write_text("#!" + sys.executable + "\\nimport json, os, sys\\nprint(json.dumps({{'protocol_version': 1, 'arguments': sys.argv[1:], 'pythonpath': os.environ.get('PYTHONPATH')}}))\\n")
    target.chmod(0o755)
''')
    uv.chmod(0o755)
    environment = {**os.environ, "PATH": str(binary), "CODEX_HOME": str(tmp_path / "data"),
                   "FAKE_UV_LOG": str(tmp_path / "uv.log")}
    environment.pop("SUPERMIND_MEMORY_EXECUTABLE", None)
    environment.pop("SUPERMIND_MEMORY_TESTING", None)

    def run(*args, env=None):
        return subprocess.run([str(plugin / "scripts/capability-memory"), *args],
                              env={**environment, **(env or {})}, cwd=tmp_path,
                              capture_output=True, text=True)

    run.plugin, run.wheel, run.lock, run.environment = plugin, wheel, lock, environment
    run.tool = tmp_path / "data/supermind/tools/capability-memory" / f"0.2.0-{lock['sha256']}"
    run.log = tmp_path / "uv.log"
    return run


def test_installs_exact_locked_wheel_and_reuses_owned_tool(launch):
    result = launch("status", "--format", "json", env={"PYTHONPATH": "/untrusted"})
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["protocol_version"] == 1
    assert payload["pythonpath"] is None
    calls = [json.loads(line) for line in launch.log.read_text().splitlines()]
    assert calls[1][-1] == str(launch.wheel)
    assert calls[0][-1] == str(launch.tool)
    assert (launch.tool / ".supermind-owned.json").is_file()
    assert launch("health").returncode == 0
    assert len(launch.log.read_text().splitlines()) == 2


@pytest.mark.parametrize("change,code", [
    ({"artifact": "../escape.whl"}, "invalid_tool_lock"),
    ({"version": "../escape"}, "invalid_tool_lock"),
    ({"sha256": "not-a-hash"}, "invalid_tool_lock"),
    ({"protocol": ">=2,<3"}, "incompatible_protocol"),
    ({"extra": 1}, "invalid_tool_lock"),
])
def test_invalid_locks_block_before_uv(launch, change, code):
    (launch.plugin / "tool.lock.json").write_text(json.dumps({**launch.lock, **change}))
    result = launch("status")
    assert result.returncode == 3
    assert json.loads(result.stderr)["code"] == code
    assert not launch.log.exists()


def test_hash_mismatch_blocks_without_fallback(launch):
    launch.wheel.write_bytes(b"changed")
    result = launch("status")
    assert result.returncode == 3
    assert json.loads(result.stderr)["code"] == "artifact_hash_mismatch"
    assert not launch.log.exists()


def test_missing_uv_never_uses_path_cli(launch):
    binary = Path(launch.environment["PATH"])
    (binary / "uv").unlink()
    fallback = binary / "supermind-memory"
    fallback.write_text("#!/bin/sh\nexit 19\n")
    fallback.chmod(0o755)
    result = launch("status")
    assert result.returncode == 3
    assert json.loads(result.stderr)["code"] == "uv_unavailable"


def test_symlink_wheel_blocks_even_with_matching_hash(launch, tmp_path):
    outside = tmp_path / "outside.whl"
    launch.wheel.rename(outside)
    launch.wheel.symlink_to(outside)
    assert launch("status").returncode == 3
    assert not launch.log.exists()


def test_unowned_tool_is_not_modified_or_executed(launch):
    launch.tool.mkdir(parents=True)
    sentinel = launch.tool / "unrelated"
    sentinel.write_text("preserve")
    assert launch("status").returncode == 3
    assert sentinel.read_text() == "preserve"
    assert not launch.log.exists()


@pytest.mark.parametrize("target", ["supermind", "supermind/tools", "supermind/tools/capability-memory"])
def test_managed_parent_symlinks_block(launch, tmp_path, target):
    link = tmp_path / "data" / target
    link.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link.symlink_to(outside, target_is_directory=True)
    assert launch("status").returncode == 3
    assert not launch.log.exists()


@pytest.mark.parametrize("corruption", ["marker", "executable", "symlink"])
def test_untrusted_cache_blocks(launch, corruption):
    assert launch("status").returncode == 0
    marker = launch.tool / ".supermind-owned.json"
    executable = launch.tool / "bin/supermind-memory"
    if corruption == "marker":
        marker.write_text("{}")
    elif corruption == "executable":
        executable.write_text("#!/bin/sh\nexit 0\n")
    else:
        executable.unlink()
        executable.symlink_to(sys.executable)
    assert launch("status").returncode == 3
    assert len(launch.log.read_text().splitlines()) == 2


def test_failed_install_never_gets_ownership_marker(launch):
    result = launch("status", env={"FAKE_UV_FAIL": "1"})
    assert result.returncode == 3
    assert json.loads(result.stderr)["code"] == "tool_provision_failed"
    assert not (launch.tool / ".supermind-owned.json").exists()


def test_override_requires_testing_and_absolute_path(launch, tmp_path):
    override = tmp_path / "override"
    override.write_text("#!/bin/sh\nexit 17\n")
    override.chmod(0o755)
    assert launch("status", env={"SUPERMIND_MEMORY_EXECUTABLE": str(override)}).returncode == 3
    assert launch("status", env={"SUPERMIND_MEMORY_TESTING": "1",
                                 "SUPERMIND_MEMORY_EXECUTABLE": "override"}).returncode == 3
    assert launch("status", env={"SUPERMIND_MEMORY_TESTING": "1",
                                 "SUPERMIND_MEMORY_EXECUTABLE": str(override)}).returncode == 17
    assert not launch.log.exists()


def test_relative_data_home_and_argument_passthrough(launch, tmp_path):
    result = launch("--data-home", "other home", "inspect", "some-id", "--format", "json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["arguments"] == [
        "--data-home", str(tmp_path / "other home"), "inspect", "some-id", "--format", "json"]


def test_plugin_contains_no_memory_implementation():
    plugin = ROOT / "plugins/supermind"
    assert not (plugin / "src").exists()
    assert not (plugin / "pyproject.toml").exists()
