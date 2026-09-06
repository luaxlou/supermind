"""Validate the standalone distribution and its immutable plugin lock."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins/supermind"
PACKAGE = ROOT / "capability-memory"


def main() -> None:
    lock = json.loads((PLUGIN / "tool.lock.json").read_text())
    assert set(lock) == {"version", "artifact", "sha256", "protocol"}
    assert lock["protocol"] == ">=1,<2"
    manifest = tomllib.loads((PACKAGE / "pyproject.toml").read_text())
    assert manifest["project"]["version"] == lock["version"]
    assert manifest["project"]["scripts"] == {"supermind-memory": "supermind_memory.cli:main"}
    for path in ("src", "pyproject.toml", "requirements.lock", "uv.lock"):
        assert not (PLUGIN / path).exists(), f"embedded runtime remains: {path}"
    artifact = PLUGIN / "vendor" / lock["artifact"]
    assert artifact.parent == PLUGIN / "vendor" and artifact.suffix == ".whl"
    assert not artifact.is_symlink()
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == lock["sha256"]
    with zipfile.ZipFile(artifact) as wheel:
        names = wheel.namelist()
        assert len(names) == len(set(names)), "duplicate wheel entries"
        for source in (PACKAGE / "src/supermind_memory").rglob("*.py"):
            relative = source.relative_to(PACKAGE / "src").as_posix()
            assert wheel.read(relative) == source.read_bytes(), f"stale wheel source: {relative}"
        for source in [PACKAGE / "model.lock.json", *(PACKAGE / "schemas").rglob("*.json"),
                       *(PACKAGE / "evaluation").rglob("*.json")]:
            relative = source.relative_to(PACKAGE).as_posix()
            assert wheel.read("supermind_memory/data/" + relative) == source.read_bytes()
            json.loads(source.read_bytes())
        assert all(not name.startswith(("/", "../")) and "/../" not in name for name in names)
        assert not any(".lance/" in name or "model-cache" in name for name in names)
    print("Standalone CLI and pinned plugin wheel verified.")


if __name__ == "__main__":
    main()
