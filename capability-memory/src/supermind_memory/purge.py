"""Explicit, preview-bound removal of capability records and their dependants."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from filelock import FileLock

from supermind_memory.event_model import AuthorityEvent
from supermind_memory.replay import replay
from supermind_memory.sync import SyncBlocked


@dataclass(frozen=True)
class PurgePlan:
    digest: str
    removed: tuple[tuple[str, str], ...]
    events: tuple[AuthorityEvent, ...]


def plan_purge(events: Sequence[AuthorityEvent], identifiers: tuple[str, ...]) -> PurgePlan:
    current = replay(events)
    if current.conflicts:
        raise SyncBlocked("authority_conflict")
    targets = set(identifiers)
    if not targets or any(("capability", key) not in current.entities for key in targets):
        raise SyncBlocked("purge_target_missing")
    removed = {("capability", key) for key in targets}

    def references(value, ids):
        if isinstance(value, str):
            return value in ids
        if isinstance(value, Mapping):
            return any(references(item, ids) for item in value.values())
        if isinstance(value, (tuple, list)):
            return any(references(item, ids) for item in value)
        return False

    while True:
        ids = {identifier for _, identifier in removed}
        incoming = {key for key, e in current.entities.items()
                    if key not in removed and references(e.payload, ids)}
        if any(kind == "capability" for kind, _ in incoming):
            raise SyncBlocked("purge_retained_capability_references",
                              tuple(f"{kind}/{key}" for kind, key in sorted(incoming)))
        kept_ids = {key for kind, key in current.entities if kind == "capability"} - targets
        if any(references(current.entities[key].payload, kept_ids) for key in incoming):
            raise SyncBlocked("purge_shared_record")
        if not incoming:
            break
        removed.update(incoming)
    # A clean baseline has no tombstones or earlier descriptions of removed entries.
    retained = tuple(AuthorityEvent.create(
        event_id="baseline-" + e.content_hash, device_id=e.device_id,
        entity_type=e.entity_type, entity_id=e.entity_id, operation=e.operation,
        parent_event_ids=(), occurred_at=e.occurred_at, payload=e.to_document()["payload"],
    ) for key, e in sorted(current.entities.items()) if key not in removed)
    replay(retained)
    return PurgePlan(current.digest, tuple(sorted(removed)), retained)


def execute_purge(coordinator, identifiers, *, confirm=False, expected_digest=None, expected_head=None, privacy=False):
    """Publish a clean baseline with an exact remote lease; never union old history."""
    from dataclasses import replace
    from supermind_memory.event_model import MemoryMarker
    from supermind_memory.event_store import EventStore
    from supermind_memory.projection import project_authority
    from supermind_memory.repository import CapabilityRepository
    c = coordinator
    c._validate_owned_paths()

    def git(*args, cwd=None):
        result = c.git._runner.run(("git", *args), cwd=cwd or c.paths.checkout)
        if result.returncode:
            raise SyncBlocked("purge_git_failed", (result.stderr.decode(errors="replace"),))
        return result.stdout.decode().strip()

    with FileLock(str(c._operation_lock), timeout=30):
        current = c._validate_local_checkout()
        if not c._validate_remote_metadata() or not c.git.fetch_branch(c.paths.checkout, "origin", c.config.branch):
            raise SyncBlocked("purge_requires_online")
        remote = c.git.ref_head(c.paths.checkout, f"refs/remotes/origin/{c.config.branch}")
        if remote != c.git.head(c.paths.checkout):
            raise SyncBlocked("purge_requires_synchronized_checkout")
        if privacy:
            from supermind_memory.privacy import plan_privacy_purge
            plan = plan_privacy_purge(c.store.load_all())
        else:
            plan = plan_purge(c.store.load_all(), identifiers)
        cache_paths = (c.paths.generations, c.paths.root / "migration-stage")
        active_pointer = c.paths.root / "active-generation.json"
        for path in (*cache_paths, active_pointer):
            if path.is_symlink():
                raise SyncBlocked("purge_cache_path_unsafe")
        result = {"applied": False, "privacy_cleanup": privacy, "event_set_digest": plan.digest, "remote_head": remote,
                  "removed": [f"{kind}/{key}" for kind, key in plan.removed],
                  "retained_capabilities": [e.entity_id for e in plan.events if e.entity_type == "capability"],
                  "local_history_paths": [str(path) for path in cache_paths if path.exists()],
                  "history_policy": "replace Git history with a current-state baseline; omit tombstoned entities"}
        if not confirm:
            return result
        if expected_digest != plan.digest or expected_head != remote:
            raise SyncBlocked("purge_preview_stale")
        # Extra refs or user files need a separate explicit scope, not silent deletion.
        refs = git("ls-remote", "--heads", "--tags", "origin").splitlines()
        if len(refs) != 1 or refs[0].split() != [remote, f"refs/heads/{c.config.branch}"]:
            raise SyncBlocked("purge_extra_remote_refs")
        from supermind_memory.renderer import render_files
        owned = {"memory.json", *(str(path) for path in render_files(current))}
        for path in c.git.tracked_paths(c.paths.checkout):
            if path not in owned and not path.startswith("events/"):
                raise SyncBlocked("purge_unowned_file", (path,))
        if git("ls-files", "--others", "--ignored", "--exclude-standard"):
            raise SyncBlocked("purge_ignored_files_present")
        allowed_refs = {f"refs/heads/{c.config.branch}", f"refs/remotes/origin/{c.config.branch}", "refs/remotes/origin/HEAD"}
        if set(git("for-each-ref", "--format=%(refname)").splitlines()) - allowed_refs:
            raise SyncBlocked("purge_extra_local_refs")
        stage = Path(tempfile.mkdtemp(prefix=".purge-stage-", dir=c.paths.root))
        pending = c.paths.root / "purge-pending.json"
        old = stage.with_name(stage.name.replace(".purge-stage-", ".purge-old-"))
        try:
            baseline = replay(plan.events)
            marker = MemoryMarker.from_bytes((c.paths.checkout / "memory.json").read_bytes())
            epoch = hashlib.sha256((remote + baseline.digest).encode()).hexdigest()
            (stage / "memory.json").write_bytes(replace(marker, history_epoch=epoch, privacy_policy="portable-context-v1" if privacy else marker.privacy_policy).to_bytes())
            store = EventStore(stage)
            for event in plan.events:
                store.append(event)
            c.renderer.render(baseline, stage)
            # Prove the remaining records still form a valid, usable projection before publishing.
            with tempfile.TemporaryDirectory(prefix=".purge-check-", dir=c.paths.root) as check:
                with CapabilityRepository.open(Path(check) / "database") as candidate:
                    project_authority(baseline, candidate, c.embeddings)
            git("init", f"--initial-branch={c.config.branch}", cwd=stage)
            git("remote", "add", "origin", c.config.clone_url, cwd=stage)
            git("add", "--all", cwd=stage)
            git("-c", "user.name=Supermind", "-c", "user.email=supermind@localhost", "commit",
                "-m", "Start clean capability baseline", cwd=stage)
            new_head = git("rev-parse", "HEAD", cwd=stage)
            # Journal contains paths and commit IDs, not removed payloads. A partial operation
            # blocks ordinary sync so an older checkout cannot restore purged records.
            with pending.open("x") as stream:
                json.dump({"stage": str(stage), "previous": str(old), "head": new_head,
                           "expected_remote": remote}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            c.git._runner.run(("git", "push", f"--force-with-lease=refs/heads/{c.config.branch}:{remote}",
                               "origin", f"HEAD:refs/heads/{c.config.branch}"), cwd=stage)
            observed = git("ls-remote", "origin", f"refs/heads/{c.config.branch}", cwd=stage).split()
            if not observed or observed[0] != new_head:
                pending.unlink()
                raise SyncBlocked("purge_push_rejected")
            # From here failures retain the journal; never restore/push the old authority.
            os.replace(c.paths.checkout, old)
            os.replace(stage, c.paths.checkout)
            c.projector(baseline, c.repository, c.embeddings)
            c._record_remote_head(new_head)
            for path in cache_paths:
                if path.exists():
                    shutil.rmtree(path)
            active_pointer.unlink(missing_ok=True)
            shutil.rmtree(old)
            pending.unlink()
            result.update(applied=True, event_set_digest=baseline.digest, remote_head=new_head, sync_state="synced")
            return result
        finally:
            if not pending.exists() and stage.exists():
                shutil.rmtree(stage)
