"""Journaled conversion from embedded schema-v2 authority to immutable events."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from supermind_memory.event_model import AuthorityEvent, EventValidationError, canonical_json
from supermind_memory.event_store import (
    MAX_EVENT_FILE_BYTES,
    EventStore,
    EventStoreError,
)
from supermind_memory.projection import AuthoritySnapshot, _RESERVED_METADATA, authority_snapshot
from supermind_memory.replay import ReplayError, replay
from supermind_memory.repository import CapabilityRepository
from supermind_memory.types import CapabilityMemoryBlocked, Evidence


_JOURNAL_NAME = ".supermind-migration-v1.json"
_OCCURRED_AT = "1970-01-01T00:00:00Z"
_MIGRATION_ORIGIN = "migration-v1"
_JOURNAL_FIELDS = {
    "format",
    "source_digest",
    "device_id",
    "intended_event_ids",
    "intended_content_hashes",
    "completed_paths",
    "final_replay_digest",
}


@dataclass(frozen=True)
class MigrationReport:
    source_digest: str
    event_set_digest: str
    entity_counts: Mapping[str, int]
    equivalent: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entity_counts",
            MappingProxyType(dict(sorted(self.entity_counts.items()))),
        )


class MigrationBlocked(CapabilityMemoryBlocked):
    """Raised when legacy authority cannot be exported without changing it."""


def export_embedded_store(
    repository: CapabilityRepository,
    event_store: EventStore,
    device_id: str,
) -> MigrationReport:
    """Export a validated legacy snapshot, resuming only its exact journal."""
    try:
        source = repository.authority_snapshot()
    except (RuntimeError, TypeError, ValueError) as error:
        raise MigrationBlocked(
            "authoritative_store_corrupt",
            f"authoritative_store_corrupt: {error}",
            (str(error),),
        ) from None

    try:
        source_document = _snapshot_document(source)
        source_digest = hashlib.sha256(canonical_json(source_document)).hexdigest()
        events = _initial_events(source, source_digest)
        planned = replay(events)
        projected = authority_snapshot(
            planned.entities,
            conflicts=planned.conflicts,
            diagnostic_ancestors=planned.diagnostic_ancestors,
            event_set_digest=planned.digest,
        )
    except (EventValidationError, ReplayError, KeyError, TypeError, ValueError) as error:
        raise MigrationBlocked(
            "migration_source_unrepresentable",
            f"migration_source_unrepresentable: {error}",
            (str(error),),
        ) from None

    oversized = next(
        (event for event in events if len(event.to_bytes()) > MAX_EVENT_FILE_BYTES),
        None,
    )
    if oversized is not None:
        raise _blocked(
            "migration_source_unrepresentable",
            f"event {oversized.event_id} exceeds {MAX_EVENT_FILE_BYTES} bytes",
        )

    equivalent = _snapshot_document(projected) == source_document
    if not equivalent:
        raise _blocked(
            "migration_source_unrepresentable",
            "event envelope changes the canonical legacy authority snapshot",
        )

    journal_path = event_store.root / _JOURNAL_NAME
    intended_ids = tuple(sorted(event.event_id for event in events))
    intended_hashes = {event.event_id: event.content_hash for event in events}
    expected_paths = {
        _relative_event_path(event).as_posix(): event for event in events
    }
    target_events = _load_target_events(event_store)
    journal = _load_or_create_journal(
        journal_path,
        source_digest=source_digest,
        device_id=device_id,
        intended_ids=intended_ids,
        intended_hashes=intended_hashes,
        final_digest=planned.digest,
        target_events=target_events,
    )
    _validate_target_events(_load_target_events(event_store), events)

    completed = set(journal["completed_paths"])
    if not completed <= set(expected_paths):
        raise _blocked("migration_journal_invalid", "journal contains an unknown event path")
    present_paths = {
        _relative_event_path(event).as_posix() for event in _load_target_events(event_store)
    }
    if not completed <= present_paths:
        raise _blocked("migration_journal_invalid", "journal records an event file that is missing")

    for event in events:
        relative = _relative_event_path(event).as_posix()
        try:
            path = event_store.append(event)
        except EventStoreError as error:
            raise MigrationBlocked(
                "migration_event_collision",
                f"migration_event_collision: {error}",
                (str(error),),
            ) from None
        actual_relative = path.relative_to(event_store.root).as_posix()
        if actual_relative != relative:
            raise _blocked("migration_journal_invalid", "event store returned an unexpected path")
        if relative not in completed:
            completed.add(relative)
            journal["completed_paths"] = sorted(completed)
            _write_journal(journal_path, journal)

    loaded = _load_target_events(event_store)
    _validate_target_events(loaded, events)
    replayed = replay(loaded)
    if replayed.digest != planned.digest:
        raise _blocked("migration_projection_mismatch", "completed event digest differs from the plan")
    journal["final_replay_digest"] = replayed.digest
    _write_journal(journal_path, journal)
    counts = Counter(event.entity_type for event in events)
    return MigrationReport(source_digest, replayed.digest, counts, equivalent)


def _initial_events(
    snapshot: AuthoritySnapshot,
    source_digest: str,
) -> tuple[AuthorityEvent, ...]:
    specifications: list[tuple[str, str, str, dict[str, object]]] = []
    for capability in snapshot.capabilities:
        specifications.append(("capability", capability.id, "registered", asdict(capability)))
    for evidence in snapshot.evidence:
        entity_type = "reuse_outcome" if _is_reuse(evidence) else "evidence"
        operation = "recorded" if entity_type == "reuse_outcome" else "observed"
        specifications.append((entity_type, evidence.id, operation, asdict(evidence)))
    for relationship in snapshot.relationships:
        specifications.append(("relationship", relationship.id, "declared", asdict(relationship)))
    for event in snapshot.events:
        specifications.append(("audit", event.id, "observed", asdict(event)))
    histories: dict[str, list[object]] = {
        observation.id: [] for observation in snapshot.requirement_observations
    }
    for event in snapshot.requirement_events:
        histories[event.observation_id].append(event)
    for observation in snapshot.requirement_observations:
        specifications.append(
            (
                "demand",
                observation.id,
                "observed",
                {
                    "observation": asdict(observation),
                    "events": [asdict(event) for event in histories[observation.id]],
                },
            )
        )
    for key, raw_value in snapshot.metadata:
        specifications.append(("metadata", key, "set", {"value": json.loads(raw_value)}))

    built = []
    for entity_type, entity_id, operation, payload in sorted(
        specifications, key=lambda item: (item[0], item[1])
    ):
        identity = hashlib.sha256(
            canonical_json([source_digest, entity_type, entity_id])
        ).hexdigest()
        built.append(
            AuthorityEvent.create(
                event_id=f"migration-{identity}",
                device_id=_MIGRATION_ORIGIN,
                entity_type=entity_type,
                entity_id=entity_id,
                operation=operation,
                parent_event_ids=(),
                occurred_at=_OCCURRED_AT,
                payload=_plain_json(payload),
            )
        )
    return tuple(sorted(built, key=lambda event: event.event_id))


def _snapshot_document(snapshot: AuthoritySnapshot) -> dict[str, object]:
    return {
        "capabilities": [asdict(item) for item in snapshot.capabilities],
        "evidence": [asdict(item) for item in snapshot.evidence],
        "relationships": [asdict(item) for item in snapshot.relationships],
        "events": [asdict(item) for item in snapshot.events],
        "requirement_observations": [
            asdict(item) for item in snapshot.requirement_observations
        ],
        "requirement_events": [asdict(item) for item in snapshot.requirement_events],
        "metadata": [
            [key, json.loads(value)]
            for key, value in snapshot.metadata
            if key not in _RESERVED_METADATA
        ],
    }


def _is_reuse(evidence: Evidence) -> bool:
    normalized = (
        evidence.evidence_type.strip().casefold().replace("-", "_").replace(" ", "_")
    )
    return normalized in {"reuse", "reuse_outcome"}


def _plain_json(value: object) -> dict[str, object]:
    plain = json.loads(json.dumps(value, ensure_ascii=False))
    if type(plain) is not dict:
        raise TypeError("event payload must be an object")
    return plain


def _relative_event_path(event: AuthorityEvent) -> Path:
    return Path(
        "events",
        "v1",
        event.device_id,
        event.occurred_at[:7],
        f"{event.event_id}.json",
    )


def _load_or_create_journal(
    path: Path,
    *,
    source_digest: str,
    device_id: str,
    intended_ids: Sequence[str],
    intended_hashes: Mapping[str, str],
    final_digest: str,
    target_events: Sequence[AuthorityEvent],
) -> dict[str, object]:
    try:
        journal = _read_journal(path)
    except FileNotFoundError:
        if target_events:
            raise _blocked("migration_target_not_empty", "migration target is not empty")
        journal = {
            "format": 1,
            "source_digest": source_digest,
            "device_id": device_id,
            "intended_event_ids": list(intended_ids),
            "intended_content_hashes": dict(sorted(intended_hashes.items())),
            "completed_paths": [],
            "final_replay_digest": None,
        }
        _write_journal(path, journal)
        return journal

    if set(journal) != _JOURNAL_FIELDS or journal.get("format") != 1:
        raise _blocked("migration_journal_invalid", "migration journal fields are invalid")
    if (
        journal.get("source_digest") != source_digest
        or journal.get("device_id") != device_id
        or journal.get("intended_event_ids") != list(intended_ids)
        or journal.get("intended_content_hashes") != dict(sorted(intended_hashes.items()))
        or type(journal.get("completed_paths")) is not list
        or any(type(item) is not str for item in journal["completed_paths"])
        or len(journal["completed_paths"]) != len(set(journal["completed_paths"]))
        or journal.get("final_replay_digest") not in (None, final_digest)
    ):
        raise _blocked("migration_journal_invalid", "migration journal does not match this export")
    return journal


def _validate_target_events(
    actual: Sequence[AuthorityEvent], expected: Sequence[AuthorityEvent],
) -> None:
    expected_by_id = {event.event_id: event.content_hash for event in expected}
    for event in actual:
        if expected_by_id.get(event.event_id) != event.content_hash:
            raise _blocked(
                "migration_event_collision",
                f"target event differs from migration plan: {event.event_id}",
            )


def _load_target_events(event_store: EventStore) -> tuple[AuthorityEvent, ...]:
    try:
        return event_store.load_all()
    except (EventStoreError, EventValidationError) as error:
        raise MigrationBlocked(
            "migration_event_collision",
            f"migration_event_collision: target event set is invalid: {error}",
            (str(error),),
        ) from None


def _read_journal(path: Path) -> dict[str, object]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise _blocked("migration_journal_invalid", "migration journal is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise _blocked("migration_journal_invalid", f"migration journal is invalid: {error}")
    if type(value) is not dict:
        raise _blocked("migration_journal_invalid", "migration journal is not an object")
    return value


def _write_journal(path: Path, document: Mapping[str, object]) -> None:
    root = path.parent
    metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _blocked("migration_journal_invalid", "event-store root is not a real directory")
    temporary = root / f".{path.name}-{uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o644)
    try:
        raw = canonical_json(document) + b"\n"
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("short migration journal write")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _blocked(code: str, detail: str) -> MigrationBlocked:
    return MigrationBlocked(code, f"{code}: {detail}", (detail,))
