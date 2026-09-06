"""Incremental immutable-event synchronization through a private Git repository."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol

from filelock import FileLock

from supermind_memory.config import MemoryPaths, RepositoryConfig
from supermind_memory.embeddings import EmbeddingProvider
from supermind_memory.event_model import AuthorityEvent, EventValidationError, MemoryMarker
from supermind_memory.event_store import EventStore, EventStoreError
from supermind_memory.git_client import (
    CommandFailed,
    GitClient,
    GitHubClient,
    RepositoryInitBlocked,
)
from supermind_memory.projection import compare_projection, project_authority
from supermind_memory.replay import ReplayError, ReplayResult, replay
from supermind_memory.repository import CapabilityRepository
from supermind_memory.redaction import redact_text


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


class SyncBlocked(RuntimeError):
    def __init__(self, code: str, attempts: tuple[str, ...] = ()) -> None:
        self.code = code
        self.attempts = tuple(redact_text(item) for item in attempts)
        detail = f": {self.attempts[-1]}" if self.attempts else ""
        super().__init__(f"{code}{detail}")


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
        renderer: RenderPort,
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
        self.renderer = renderer
        self.projector = projector
        self.store = EventStore(paths.checkout)
        self._operation_lock = paths.locks / "sync.lock"

    def mutate(self, create_event: Callable[[ReplayResult], AuthorityEvent]) -> SyncReport:
        with FileLock(str(self._operation_lock), timeout=30):
            current = self._validate_local_checkout()
            online = self._validate_remote_metadata()
            if online and self.git.fetch_branch(self.paths.checkout, "origin", self.config.branch):
                online = self._reconcile_pending()
                current = replay(self.store.load_all())
            else:
                self._require_healthy_projection(current)
                return self._commit_mutation(current, create_event, online=False)
            return self._commit_mutation(current, create_event, online=True)

    def synchronize(self) -> SyncReport:
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
            result = self._merge_remote_events(remote_ref)
            self.projector(result, self.repository, self.embeddings)
            self.renderer.render(result, self.paths.checkout)
            commit_id = self.git.commit_all(
                self.paths.checkout,
                self.config.branch,
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

    def _merge_remote_events(self, remote_ref: str) -> ReplayResult:
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
        try:
            for event in incoming:
                self.store.append(event)
        except (EventStoreError, OSError) as error:
            raise SyncBlocked("authority_corrupt", (str(error),)) from error
        return result

    def _commit_mutation(
        self,
        current: ReplayResult,
        create_event: Callable[[ReplayResult], AuthorityEvent],
        *,
        online: bool,
    ) -> SyncReport:
        if current.conflicts:
            raise SyncBlocked("authority_conflict")
        event = create_event(current)
        if not isinstance(event, AuthorityEvent):
            raise SyncBlocked("authority_event_invalid")
        try:
            result = replay((*self.store.load_all(), event))
            self.store.append(event)
        except (EventStoreError, OSError, ReplayError) as error:
            raise SyncBlocked("authority_corrupt", (str(error),)) from error
        self.projector(result, self.repository, self.embeddings)
        self.renderer.render(result, self.paths.checkout)
        remote_head = self.config.last_checked_remote_head
        if remote_head is None:
            raise SyncBlocked("repository_remote_head_missing")
        commit_id = self.git.commit_all(
            self.paths.checkout,
            self.config.branch,
            remote_head,
            f"Record Supermind memory event {event.event_id}",
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
            result = self._merge_remote_events(remote_ref)
            self.projector(result, self.repository, self.embeddings)
            self.renderer.render(result, self.paths.checkout)
            commit_id = self.git.commit_all(
                self.paths.checkout,
                self.config.branch,
                remote_head,
                f"Reconcile Supermind memory event {event.event_id}",
            )
            if result.conflicts:
                conflicts = tuple(
                    f"{item.entity_type}/{item.entity_id}" for item in result.conflicts
                )
                raise SyncBlocked("authority_conflict", conflicts)
            report = self._report(SyncState.COMMITTED, result, commit_id)
        raise SyncBlocked("push_retry_exhausted", self.git.redacted_attempts)

    def _record_remote_head(self, remote_head: str) -> None:
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
