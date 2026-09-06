"""Incremental immutable-event synchronization through a private Git repository."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol

from filelock import FileLock

from supermind_memory.config import MemoryPaths, RepositoryConfig
from supermind_memory.embeddings import EmbeddingProvider
from supermind_memory.event_model import AuthorityEvent, EventValidationError, MemoryMarker
from supermind_memory.event_store import EventStore, EventStoreError
from supermind_memory.migration import export_embedded_store, MigrationReport
from supermind_memory.git_client import (
    CommandFailed,
    GitClient,
    GitHubClient,
    RepositoryInitBlocked,
    validate_memory_paths,
)
from supermind_memory.projection import authority_snapshot, compare_projection, project_authority
from supermind_memory.replay import ReplayError, ReplayResult, replay
from supermind_memory.repository import CapabilityRepository
from supermind_memory.redaction import redact_text
from supermind_memory.renderer import RepositoryRenderer
from supermind_memory.types import CapabilityMemoryBlocked


MAX_PUSH_ATTEMPTS = 3
_EVENT_PATH = re.compile(
    r"events/v1/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}/\d{4}-\d{2}/"
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\.json\Z"
)


class SyncState(str, Enum):
    COMMITTED = "committed"
    SYNCED = "synced"
    PENDING_SYNC = "pending_sync"


@dataclass(frozen=True)
class SyncReport:
    sync_state: SyncState
    commit_id: str
    event_set_digest: str
    remote_head: str | None
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FileImage:
    payload: bytes
    mode: int


class SyncBlocked(CapabilityMemoryBlocked):
    def __init__(
        self, code: str, attempts: tuple[str, ...] = (), *,
        sync_state: SyncState | None = None, event_set_digest: str | None = None,
        commit_id: str | None = None,
    ) -> None:
        self.code = code
        self.attempts = tuple(redact_text(item) for item in attempts)
        self.sync_state = sync_state
        self.event_set_digest = event_set_digest
        self.commit_id = commit_id
        detail = f": {self.attempts[-1]}" if self.attempts else ""
        super().__init__(code, f"{code}{detail}", self.attempts)


class RenderPort(Protocol):
    def render(self, result: ReplayResult, checkout: Path) -> None: ...


class SyncCoordinator:
    def __init__(
        self,
        *,
        paths: MemoryPaths,
        config: RepositoryConfig,
        git: GitClient,
        github: GitHubClient,
        repository: CapabilityRepository,
        embeddings: EmbeddingProvider,
        renderer: RenderPort | None = None,
        projector: Callable[
            [ReplayResult, CapabilityRepository, EmbeddingProvider], object
        ] = project_authority,
    ) -> None:
        self.paths = paths
        self.config = config
        self.git = git
        self.github = github
        self.repository = repository
        self.embeddings = embeddings
        self.renderer = renderer or RepositoryRenderer()
        self.projector = projector
        self.store = EventStore(paths.checkout)
        self._operation_lock = paths.locks / "sync.lock"

    def mutate(self, create_event: Callable[[ReplayResult], AuthorityEvent]) -> SyncReport:
        return self.mutate_batch(lambda current: (create_event(current),))

    def mutate_batch(
        self,
        create_events: Callable[[ReplayResult], Sequence[AuthorityEvent]],
    ) -> SyncReport:
        self._validate_owned_paths()
        with FileLock(str(self._operation_lock), timeout=30):
            current = self._validate_local_checkout()
            online = self._validate_remote_metadata()
            if online and self.git.fetch_branch(self.paths.checkout, "origin", self.config.branch):
                online = self._reconcile_pending()
                current = replay(self.store.load_all())
            else:
                self._require_healthy_projection(current)
                return self._commit_mutation(current, create_events, online=False)
            return self._commit_mutation(current, create_events, online=online)

    def synchronize(self) -> SyncReport:
        self._validate_owned_paths()
        with FileLock(str(self._operation_lock), timeout=30):
            current = self._validate_local_checkout()
            online = self._validate_remote_metadata()
            if not online or not self.git.fetch_branch(
                self.paths.checkout, "origin", self.config.branch,
            ):
                self._require_healthy_projection(current)
                return self._report(
                    SyncState.PENDING_SYNC, current, self.git.head(self.paths.checkout),
                )
            if not self._reconcile_pending():
                result = replay(self.store.load_all())
                return self._report(
                    SyncState.PENDING_SYNC, result, self.git.head(self.paths.checkout),
                )
            result = replay(self.store.load_all())
            return self._report(SyncState.SYNCED, result, self.git.head(self.paths.checkout))

    def migrate(self, legacy: CapabilityRepository) -> tuple[MigrationReport, SyncReport]:
        """Journal the export outside authority, then commit it under the mutation lock."""
        migration_report = None

        def create_events(current: ReplayResult) -> Sequence[AuthorityEvent]:
            nonlocal migration_report
            stage = self.paths.root / "migration-stage"
            if stage.is_symlink():
                raise SyncBlocked("migration_stage_unsafe")
            stage.mkdir(exist_ok=True)
            staged_store = EventStore(stage)
            migration_report = export_embedded_store(legacy, staged_store, self.config.device_id)
            events = staged_store.load_all()
            if current.entities and current.digest != replay(events).digest:
                raise SyncBlocked("migration_target_not_empty")
            return events

        report = self.mutate_batch(create_events)
        assert migration_report is not None
        return migration_report, report

    def render_repository(self) -> SyncReport:
        """Regenerate and commit the deterministic browser from event authority."""
        self._validate_owned_paths()
        with FileLock(str(self._operation_lock), timeout=30):
            current = self._validate_local_checkout()
            online = self._validate_remote_metadata()
            if online and self.git.fetch_branch(
                self.paths.checkout, "origin", self.config.branch,
            ):
                online = self._reconcile_pending()
                current = replay(self.store.load_all())
            else:
                online = False
                self._require_healthy_projection(current)

            remote_head = self.config.last_checked_remote_head
            if remote_head is None:
                raise SyncBlocked("repository_remote_head_missing")
            commit_id = self._materialize_commit(
                current,
                current,
                (),
                remote_head,
                "Render Supermind capability browser",
            )
            report = self._report(SyncState.COMMITTED, current, commit_id)
            if not online:
                return replace(report, sync_state=SyncState.PENDING_SYNC)
            if not self.git.push(self.paths.checkout, "origin", self.config.branch):
                return replace(report, sync_state=SyncState.PENDING_SYNC)
            self._record_remote_head(commit_id)
            return replace(report, sync_state=SyncState.SYNCED, remote_head=commit_id)

    def resolve_conflict(
        self, *, entity_type: str, entity_id: str, expected_head_ids: Sequence[str],
        payload: Mapping[str, object], event_id: str, occurred_at: str,
    ) -> SyncReport:
        """Resolve exactly the currently observed sibling heads, never a stale subset."""
        self._validate_owned_paths()
        with FileLock(str(self._operation_lock), timeout=30):
            previous = self._validate_local_checkout()
            online = self._validate_remote_metadata()
            incoming: tuple[AuthorityEvent, ...] = ()
            remote_head = self.config.last_checked_remote_head
            current = previous
            if online and self.git.fetch_branch(self.paths.checkout, "origin", self.config.branch):
                remote_ref = f"refs/remotes/origin/{self.config.branch}"
                remote_head = self.git.ref_head(self.paths.checkout, remote_ref)
                self._record_remote_head(remote_head)
                current, incoming = self._merge_remote_events(remote_ref)
            if remote_head is None:
                raise SyncBlocked("repository_remote_head_missing")
            key = (entity_type, entity_id)
            heads = current.heads.get(key, ())
            conflict = next((item for item in current.conflicts if (item.entity_type, item.entity_id) == key), None)
            if conflict is None or tuple(sorted(expected_head_ids)) != tuple(sorted(heads)):
                raise SyncBlocked("conflict_heads_stale")
            event = AuthorityEvent.create(
                event_id=event_id, device_id=self.config.device_id,
                entity_type=entity_type, entity_id=entity_id, operation="resolved",
                parent_event_ids=heads, occurred_at=occurred_at, payload=payload,
            )
            result = replay((*self.store.load_all(), *incoming, event))
            commit_id = self._materialize_commit(
                previous, result, (*incoming, event), remote_head,
                f"Resolve Supermind memory conflict {entity_type}/{entity_id}",
            )
            report = self._report(SyncState.COMMITTED, result, commit_id)
            if not online:
                return replace(report, sync_state=SyncState.PENDING_SYNC)
            if not self.git.push(self.paths.checkout, "origin", self.config.branch):
                return replace(report, sync_state=SyncState.PENDING_SYNC)
            self._record_remote_head(commit_id)
            return replace(report, sync_state=SyncState.SYNCED, remote_head=commit_id)

    def _validate_owned_paths(self) -> None:
        try:
            validate_memory_paths(self.paths)
        except RepositoryInitBlocked as error:
            raise SyncBlocked("repository_paths_invalid", (str(error),)) from error

    def _validate_local_checkout(self) -> ReplayResult:
        if self.paths.config.is_symlink():
            raise SyncBlocked("repository_config_unsafe")
        try:
            persisted = RepositoryConfig.read(self.paths.config)
        except Exception as error:
            raise SyncBlocked("repository_not_initialized") from error
        if persisted != self.config:
            raise SyncBlocked("repository_config_changed")
        if self.paths.checkout.is_symlink() or not self.paths.checkout.is_dir():
            raise SyncBlocked("repository_checkout_unsafe")
        git_path = self.paths.checkout / ".git"
        if git_path.is_symlink():
            raise SyncBlocked("repository_symlink_unsafe")
        if not git_path.is_dir():
            raise SyncBlocked("repository_git_path_unsafe")
        if (self.paths.checkout / "memory.json").is_symlink():
            raise SyncBlocked("repository_symlink_unsafe")
        if self.git.status(self.paths.checkout):
            raise SyncBlocked("repository_checkout_dirty")
        if self.git.origin_urls(self.paths.checkout) != (self.config.clone_url,):
            raise SyncBlocked("repository_remote_ambiguous")
        if self.git.origin_push_urls(self.paths.checkout) != (self.config.clone_url,):
            raise SyncBlocked("repository_remote_ambiguous")
        if self.git.branch(self.paths.checkout) != self.config.branch:
            raise SyncBlocked("repository_branch_ambiguous")
        try:
            marker = MemoryMarker.from_bytes((self.paths.checkout / "memory.json").read_bytes())
            if (
                marker.repository_id != self.config.repository_id
                or marker.default_branch != self.config.branch
            ):
                raise SyncBlocked("repository_marker_mismatch")
            return replay(self.store.load_all())
        except SyncBlocked:
            raise
        except (OSError, EventValidationError, EventStoreError, ReplayError) as error:
            raise SyncBlocked("authority_corrupt", (str(error),)) from error

    def _validate_remote_metadata(self) -> bool:
        try:
            remote = self.github.inspect_writable_private(self.config.repository)
        except CommandFailed:
            return False
        except RepositoryInitBlocked as error:
            raise SyncBlocked(str(error)) from error
        if (
            remote.repository_id != self.config.repository_id
            or remote.name_with_owner != self.config.repository
            or remote.clone_url != self.config.clone_url
            or remote.default_branch != self.config.branch
            or remote.web_url != self.config.web_url
        ):
            raise SyncBlocked("repository_identity_mismatch")
        return True

    def _require_healthy_projection(self, result: ReplayResult) -> None:
        comparison = compare_projection(result, self.repository)
        if not comparison.equivalent:
            raise SyncBlocked("local_projection_unhealthy", comparison.differences)

    def _reconcile_pending(self) -> bool:
        for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
            remote_ref = f"refs/remotes/origin/{self.config.branch}"
            remote_head = self.git.ref_head(self.paths.checkout, remote_ref)
            self._record_remote_head(remote_head)
            local_head = self.git.head(self.paths.checkout)
            if local_head == remote_head:
                self._record_remote_head(remote_head)
                return True
            previous = replay(self.store.load_all())
            result, incoming = self._merge_remote_events(remote_ref)
            commit_id = self._materialize_commit(
                previous,
                result,
                incoming,
                remote_head,
                "Synchronize Supermind memory events",
            )
            if result.conflicts:
                conflicts = tuple(
                    f"{item.entity_type}/{item.entity_id}" for item in result.conflicts
                )
                raise SyncBlocked("authority_conflict", conflicts)
            if self.git.push(self.paths.checkout, "origin", self.config.branch):
                self._record_remote_head(commit_id)
                return True
            if attempt == MAX_PUSH_ATTEMPTS:
                break
            if not self.git.fetch_branch(self.paths.checkout, "origin", self.config.branch):
                return False
        raise SyncBlocked("push_retry_exhausted", self.git.redacted_attempts)

    def _merge_remote_events(
        self, remote_ref: str,
    ) -> tuple[ReplayResult, tuple[AuthorityEvent, ...]]:
        try:
            marker_entries = self.git.tree_entries(
                self.paths.checkout, remote_ref, "memory.json",
            )
            if (
                set(marker_entries) != {"memory.json"}
                or marker_entries["memory.json"][0] != "100644"
            ):
                raise SyncBlocked("repository_marker_invalid")
            marker = MemoryMarker.from_bytes(
                self.git.show_file(self.paths.checkout, remote_ref, "memory.json"),
            )
        except SyncBlocked:
            raise
        except (CommandFailed, EventValidationError, RepositoryInitBlocked) as error:
            raise SyncBlocked("repository_marker_invalid", (str(error),)) from error
        if (
            marker.repository_id != self.config.repository_id
            or marker.default_branch != self.config.branch
        ):
            raise SyncBlocked("repository_marker_mismatch")
        local = self.git.tree_entries(self.paths.checkout, "HEAD", "events")
        remote = self.git.tree_entries(self.paths.checkout, remote_ref, "events")
        invalid_paths = sorted(
            path
            for entries in (local, remote)
            for path, (mode, _) in entries.items()
            if mode != "100644" or not _EVENT_PATH.fullmatch(path)
        )
        if invalid_paths:
            raise SyncBlocked("authority_corrupt", tuple(invalid_paths))
        collisions = sorted(
            path for path in local.keys() & remote.keys() if local[path][1] != remote[path][1]
        )
        if collisions:
            raise SyncBlocked("event_path_collision", tuple(collisions))
        local_events = self.store.load_all()
        incoming: list[AuthorityEvent] = []
        for path in sorted(remote.keys() - local.keys()):
            try:
                event = AuthorityEvent.from_bytes(
                    self.git.show_file(self.paths.checkout, remote_ref, path),
                )
            except (EventValidationError, EventStoreError, OSError, ValueError) as error:
                raise SyncBlocked("authority_corrupt", (str(error),)) from error
            expected = Path(
                "events", "v1", event.device_id, event.occurred_at[:7], f"{event.event_id}.json",
            ).as_posix()
            if expected != path:
                raise SyncBlocked("authority_corrupt", (path,))
            incoming.append(event)
        try:
            result = replay((*local_events, *incoming))
        except ReplayError as error:
            raise SyncBlocked("authority_corrupt", (str(error),)) from error
        return result, tuple(incoming)

    def _commit_mutation(
        self,
        current: ReplayResult,
        create_events: Callable[[ReplayResult], Sequence[AuthorityEvent]],
        *,
        online: bool,
    ) -> SyncReport:
        if current.conflicts:
            raise SyncBlocked("authority_conflict")
        events = tuple(create_events(current))
        if any(not isinstance(event, AuthorityEvent) for event in events):
            raise SyncBlocked("authority_event_invalid")
        try:
            result = replay((*self.store.load_all(), *events))
        except (EventStoreError, ReplayError) as error:
            raise SyncBlocked("authority_corrupt", (str(error),)) from error
        remote_head = self.config.last_checked_remote_head
        if remote_head is None:
            raise SyncBlocked("repository_remote_head_missing")
        commit_id = self._materialize_commit(
            current,
            result,
            events,
            remote_head,
            f"Record {len(events)} Supermind memory event(s)",
        )
        report = self._report(SyncState.COMMITTED, result, commit_id)
        if result.conflicts:
            raise SyncBlocked("authority_conflict", report.conflicts)
        if not online:
            return replace(report, sync_state=SyncState.PENDING_SYNC)
        for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
            if self.git.push(self.paths.checkout, "origin", self.config.branch):
                self._record_remote_head(commit_id)
                return replace(report, sync_state=SyncState.SYNCED, remote_head=commit_id)
            if attempt == MAX_PUSH_ATTEMPTS:
                break
            if not self.git.fetch_branch(self.paths.checkout, "origin", self.config.branch):
                return replace(report, sync_state=SyncState.PENDING_SYNC)
            remote_ref = f"refs/remotes/origin/{self.config.branch}"
            remote_head = self.git.ref_head(self.paths.checkout, remote_ref)
            self._record_remote_head(remote_head)
            previous = replay(self.store.load_all())
            result, incoming = self._merge_remote_events(remote_ref)
            commit_id = self._materialize_commit(
                previous,
                result,
                incoming,
                remote_head,
                "Reconcile Supermind memory event batch",
            )
            if result.conflicts:
                conflicts = tuple(
                    f"{item.entity_type}/{item.entity_id}" for item in result.conflicts
                )
                raise SyncBlocked("authority_conflict", conflicts)
            report = self._report(SyncState.COMMITTED, result, commit_id)
        raise SyncBlocked("push_retry_exhausted", self.git.redacted_attempts)

    def _materialize_commit(
        self,
        previous: ReplayResult,
        result: ReplayResult,
        additions: tuple[AuthorityEvent, ...],
        remote_head: str,
        message: str,
    ) -> str:
        self._preflight_projection(result)
        render_changes = self._stage_render(result, additions)
        self._validate_owned_paths()
        render_before = self._live_images(render_changes)
        event_paths = tuple(self._event_path(event) for event in additions)
        event_existed = tuple(path.exists() for path in event_paths)
        try:
            for event in additions:
                self.store.append(event)
        except Exception as error:
            for path, existed in zip(event_paths, event_existed, strict=True):
                if not existed:
                    path.unlink(missing_ok=True)
            raise SyncBlocked("materialization_failed", (str(error),)) from error
        try:
            self.projector(result, self.repository, self.embeddings)
        except Exception as error:
            try:
                self._restore_projection(previous, error)
                commit_id = self.git.commit_all(
                    self.paths.checkout, self.config.branch, remote_head,
                    f"Persist Supermind memory authority before projection: {message}",
                )
            except Exception as persistence_error:
                for path, existed in zip(event_paths, event_existed, strict=True):
                    if not existed:
                        path.unlink(missing_ok=True)
                self.git.reset_index(self.paths.checkout)
                raise SyncBlocked(
                    "materialization_recovery_failed", (str(error), str(persistence_error)),
                ) from error
            raise SyncBlocked(
                "projection_failed", (str(error),), sync_state=SyncState.COMMITTED,
                event_set_digest=result.digest, commit_id=commit_id,
            ) from error
        try:
            self._apply_images(render_changes)
            return self.git.commit_all(
                self.paths.checkout,
                self.config.branch,
                remote_head,
                message,
            )
        except Exception as error:
            recovery_errors: list[str] = []
            try:
                self._restore_images(render_before)
            except Exception as recovery_error:
                recovery_errors.append(str(recovery_error))
            for path, existed in zip(event_paths, event_existed, strict=True):
                if not existed:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as recovery_error:
                        recovery_errors.append(str(recovery_error))
            try:
                self.git.reset_index(self.paths.checkout)
            except Exception as recovery_error:
                recovery_errors.append(str(recovery_error))
            try:
                self._restore_projection(previous, error)
            except Exception as recovery_error:
                recovery_errors.append(str(recovery_error))
            if recovery_errors:
                raise SyncBlocked(
                    "materialization_recovery_failed",
                    (str(error), *recovery_errors),
                ) from error
            raise SyncBlocked("materialization_failed", (str(error),)) from error

    def _preflight_projection(self, result: ReplayResult) -> None:
        try:
            authority_snapshot(
                result.entities,
                conflicts=result.conflicts,
                diagnostic_ancestors=result.diagnostic_ancestors,
                event_set_digest=result.digest,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SyncBlocked("authority_projection_invalid", (str(error),)) from error

    def _stage_render(
        self,
        result: ReplayResult,
        additions: tuple[AuthorityEvent, ...],
    ) -> dict[str, _FileImage | None]:
        self._validate_owned_paths()
        with tempfile.TemporaryDirectory(prefix=".sync-stage-", dir=self.paths.root) as temporary:
            stage = Path(temporary) / "repository"

            def ignore_git(directory: str, names: list[str]) -> set[str]:
                return {".git"} if Path(directory) == self.paths.checkout else set()

            shutil.copytree(
                self.paths.checkout,
                stage,
                symlinks=True,
                ignore=ignore_git,
            )
            for event in additions:
                EventStore(stage).append(event)
            before = _snapshot_files(stage)
            try:
                self.renderer.render(result, stage)
            except Exception as error:
                raise SyncBlocked("render_failed", (str(error),)) from error
            after = _snapshot_files(stage)
            authority_paths = {
                path for path in before.keys() | after.keys() if _is_authority_path(path)
            }
            if any(before.get(path) != after.get(path) for path in authority_paths):
                raise SyncBlocked("renderer_modified_authority", tuple(sorted(authority_paths)))
            return {
                path: after.get(path)
                for path in sorted(before.keys() | after.keys())
                if not _is_authority_path(path) and before.get(path) != after.get(path)
            }

    def _live_images(
        self, changes: dict[str, _FileImage | None],
    ) -> dict[str, _FileImage | None]:
        images: dict[str, _FileImage | None] = {}
        for relative in changes:
            target = self.paths.checkout / relative
            if target.is_symlink():
                raise SyncBlocked("repository_symlink_unsafe", (relative,))
            images[relative] = (
                _read_image(target)
                if target.exists() and not target.is_dir()
                else None
            )
        return images

    def _apply_images(self, images: dict[str, _FileImage | None]) -> None:
        _replace_images(self.paths.checkout, images)

    def _restore_images(self, images: dict[str, _FileImage | None]) -> None:
        _replace_images(self.paths.checkout, images)

    def _restore_projection(self, previous: ReplayResult, original: Exception) -> None:
        try:
            if not compare_projection(previous, self.repository).equivalent:
                project_authority(previous, self.repository, self.embeddings)
            if not compare_projection(previous, self.repository).equivalent:
                raise RuntimeError("restored projection does not match prior event set")
        except Exception as recovery_error:
            raise SyncBlocked(
                "projection_recovery_failed",
                (str(original), str(recovery_error)),
            ) from original

    def _event_path(self, event: AuthorityEvent) -> Path:
        return self.paths.checkout / Path(
            "events", "v1", event.device_id, event.occurred_at[:7], f"{event.event_id}.json",
        )

    def _record_remote_head(self, remote_head: str) -> None:
        self._validate_owned_paths()
        self.config = replace(self.config, last_checked_remote_head=remote_head)
        self.config.write(self.paths.config)

    def _report(
        self,
        state: SyncState,
        result: ReplayResult,
        commit_id: str,
    ) -> SyncReport:
        return SyncReport(
            sync_state=state,
            commit_id=commit_id,
            event_set_digest=result.digest,
            remote_head=self.config.last_checked_remote_head,
            conflicts=tuple(
                f"{item.entity_type}/{item.entity_id}" for item in result.conflicts
            ),
        )


def _snapshot_files(root: Path) -> dict[str, _FileImage]:
    images: dict[str, _FileImage] = {}
    for directory, directories, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in directories:
            path = base / name
            if path.is_symlink():
                raise SyncBlocked(
                    "repository_symlink_unsafe",
                    (path.relative_to(root).as_posix(),),
                )
        for name in filenames:
            path = base / name
            relative = path.relative_to(root).as_posix()
            images[relative] = _read_image(path)
    return images


def _read_image(path: Path) -> _FileImage:
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise SyncBlocked("repository_symlink_unsafe", (str(path),))
    return _FileImage(path.read_bytes(), stat.S_IMODE(details.st_mode))


def _is_authority_path(relative: str) -> bool:
    parts = Path(relative).parts
    return relative == "memory.json" or bool(parts and parts[0] == "events")


def _apply_image(root: Path, relative: str, image: _FileImage | None) -> None:
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise SyncBlocked("render_path_invalid", (relative,))
    parent = root
    for part in path.parts[:-1]:
        parent /= part
        if parent.exists() or parent.is_symlink():
            if parent.is_symlink() or not parent.is_dir():
                raise SyncBlocked("repository_symlink_unsafe", (relative,))
        else:
            parent.mkdir(mode=0o755)
    target = root / path
    if target.is_symlink():
        raise SyncBlocked("repository_symlink_unsafe", (relative,))
    if image is None:
        target.unlink(missing_ok=True)
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, image.mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(image.payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _replace_images(root: Path, images: dict[str, _FileImage | None]) -> None:
    ordered = sorted(images, key=lambda item: (len(Path(item).parts), item), reverse=True)
    for relative in ordered:
        target = root / relative
        if target.is_symlink():
            raise SyncBlocked("repository_symlink_unsafe", (relative,))
        if target.is_file():
            target.unlink()
        elif target.is_dir():
            # Only an empty transaction-owned topology node may be removed. A
            # concurrent untracked file makes recovery fail closed and is kept.
            target.rmdir()
    for relative in reversed(ordered):
        image = images[relative]
        if image is not None:
            _apply_image(root, relative, image)
