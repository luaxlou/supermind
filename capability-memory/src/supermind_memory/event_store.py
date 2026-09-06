"""Append-only filesystem transport for canonical authority events."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from supermind_memory.event_model import AuthorityEvent


MAX_EVENT_FILE_BYTES = 1_048_576

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


class EventStoreError(ValueError):
    """Base error for an invalid or unsafe event store."""


class EventCollisionError(EventStoreError):
    """Raised when an event path already contains different bytes."""


class UnsafeEventPath(EventStoreError):
    """Raised when an event path could escape or violate the store layout."""


class EventFileTooLarge(EventStoreError):
    """Raised when an event crosses the raw file transport limit."""


class EventStore:
    """Store immutable events below an owned checkout root."""

    def __init__(self, checkout_root: Path | str) -> None:
        self.root = Path(checkout_root)

    def append(self, event: AuthorityEvent) -> Path:
        raw = event.to_bytes()
        _check_size(len(raw))
        relative = _event_relative_path(event)
        target = self.root / relative

        root_fd = self._open_root()
        try:
            parent_fd = _open_directory_chain(root_fd, relative.parts[:-1], create=True)
            try:
                self._append_at(parent_fd, relative.name, raw, target)
            finally:
                os.close(parent_fd)
        finally:
            os.close(root_fd)
        return target

    def load_all(self) -> tuple[AuthorityEvent, ...]:
        root_fd = self._open_root()
        try:
            events_fd = _open_child_directory(root_fd, "events", missing_ok=True)
            if events_fd is None:
                return ()
            try:
                version_fd = _open_child_directory(events_fd, "v1", missing_ok=True)
                if version_fd is None:
                    return ()
                try:
                    loaded = _load_version(version_fd)
                finally:
                    os.close(version_fd)
            finally:
                os.close(events_fd)
        finally:
            os.close(root_fd)
        return tuple(sorted(loaded, key=lambda event: (event.event_id, event.content_hash)))

    def _open_root(self) -> int:
        try:
            root_stat = self.root.lstat()
        except FileNotFoundError as error:
            raise UnsafeEventPath(f"checkout root does not exist: {self.root}") from error
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise UnsafeEventPath(f"checkout root is not a real directory: {self.root}")
        try:
            return os.open(self.root, _DIRECTORY_FLAGS)
        except OSError as error:
            raise UnsafeEventPath(f"cannot safely open checkout root: {self.root}") from error

    @staticmethod
    def _append_at(parent_fd: int, name: str, raw: bytes, target: Path) -> None:
        try:
            file_fd = os.open(name, _WRITE_FLAGS, 0o644, dir_fd=parent_fd)
        except FileExistsError:
            existing = _read_file_at(parent_fd, name)
            if existing == raw:
                return
            raise EventCollisionError(f"event path already has different content: {target}")
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise UnsafeEventPath(f"event target is not a regular file: {target}") from error
            raise

        try:
            _write_all(file_fd, raw)
            os.fsync(file_fd)
        except BaseException:
            os.close(file_fd)
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            raise
        else:
            os.close(file_fd)
            os.fsync(parent_fd)


def _event_relative_path(event: AuthorityEvent) -> Path:
    return Path(
        "events",
        "v1",
        event.device_id,
        event.occurred_at[:7],
        f"{event.event_id}.json",
    )


def _open_directory_chain(root_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            child_fd = _open_child_directory(current_fd, part, missing_ok=True)
            if child_fd is None:
                if not create:
                    raise FileNotFoundError(part)
                try:
                    os.mkdir(part, 0o755, dir_fd=current_fd)
                except FileExistsError:
                    pass
                os.fsync(current_fd)
                child_fd = _open_child_directory(current_fd, part, missing_ok=False)
                assert child_fd is not None
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_child_directory(parent_fd: int, name: str, *, missing_ok: bool) -> int | None:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafeEventPath(f"event path component is not a real directory: {name}") from error
        raise


def _load_version(version_fd: int) -> list[AuthorityEvent]:
    loaded: list[AuthorityEvent] = []
    for device_name in _directory_names(version_fd):
        device_fd = _required_child_directory(version_fd, device_name)
        try:
            for month_name in _directory_names(device_fd):
                month_fd = _required_child_directory(device_fd, month_name)
                try:
                    for filename in _directory_names(month_fd):
                        if not filename.endswith(".json"):
                            raise UnsafeEventPath(f"unexpected entry in event month: {filename}")
                        raw = _read_file_at(month_fd, filename)
                        event = AuthorityEvent.from_bytes(raw)
                        if (
                            device_name != event.device_id
                            or month_name != event.occurred_at[:7]
                            or filename != f"{event.event_id}.json"
                        ):
                            raise UnsafeEventPath(f"event does not match its storage path: {filename}")
                        loaded.append(event)
                finally:
                    os.close(month_fd)
        finally:
            os.close(device_fd)
    return loaded


def _directory_names(directory_fd: int) -> tuple[str, ...]:
    with os.scandir(directory_fd) as entries:
        return tuple(sorted(entry.name for entry in entries))


def _required_child_directory(parent_fd: int, name: str) -> int:
    child_fd = _open_child_directory(parent_fd, name, missing_ok=False)
    assert child_fd is not None
    return child_fd


def _read_file_at(parent_fd: int, name: str) -> bytes:
    try:
        file_fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafeEventPath(f"event entry is not a regular file: {name}") from error
        raise
    try:
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise UnsafeEventPath(f"event entry is not a regular file: {name}")
        _check_size(file_stat.st_size)
        raw = bytearray()
        while len(raw) <= MAX_EVENT_FILE_BYTES:
            chunk = os.read(file_fd, min(65_536, MAX_EVENT_FILE_BYTES + 1 - len(raw)))
            if not chunk:
                return bytes(raw)
            raw.extend(chunk)
        raise EventFileTooLarge(f"event file exceeds {MAX_EVENT_FILE_BYTES} bytes: {name}")
    finally:
        os.close(file_fd)


def _check_size(size: int) -> None:
    if size > MAX_EVENT_FILE_BYTES:
        raise EventFileTooLarge(f"event file exceeds {MAX_EVENT_FILE_BYTES} bytes")


def _write_all(file_fd: int, raw: bytes) -> None:
    remaining = memoryview(raw)
    while remaining:
        written = os.write(file_fd, remaining)
        if written == 0:
            raise OSError("event write made no progress")
        remaining = remaining[written:]
