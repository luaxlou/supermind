"""Deterministic parent-graph replay for authority events."""

from __future__ import annotations

import hashlib
import heapq
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeAlias

from supermind_memory.event_model import AuthorityEvent, EntityConflict, canonical_json


EntityKey: TypeAlias = tuple[str, str]


class ReplayError(ValueError):
    """Raised when an event set cannot be replayed safely."""


@dataclass(frozen=True)
class ReplayResult:
    entities: Mapping[EntityKey, AuthorityEvent]
    heads: Mapping[EntityKey, tuple[str, ...]]
    conflicts: tuple[EntityConflict, ...]
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "entities", MappingProxyType(dict(self.entities)))
        object.__setattr__(self, "heads", MappingProxyType(dict(self.heads)))
        object.__setattr__(self, "conflicts", tuple(self.conflicts))


def replay(events: Sequence[AuthorityEvent]) -> ReplayResult:
    """Validate and replay an event set without consulting traversal or wall-clock order."""
    index = _index_events(events)
    _validate_parent_graph(index)
    _reject_silent_conflict_joins(index)

    grouped: dict[EntityKey, set[str]] = {}
    referenced: dict[EntityKey, set[str]] = {}
    for event in index.values():
        key = _entity_key(event)
        grouped.setdefault(key, set()).add(event.event_id)
        referenced.setdefault(key, set()).update(event.parent_event_ids)

    heads = {
        key: tuple(sorted(event_ids - referenced[key]))
        for key, event_ids in sorted(grouped.items())
    }
    entities: dict[EntityKey, AuthorityEvent] = {}
    conflicts: list[EntityConflict] = []
    for key, event_ids in heads.items():
        if len(event_ids) > 1:
            conflicts.append(EntityConflict(*key, event_ids))
            continue
        head = index[event_ids[0]]
        if head.operation != "tombstoned":
            entities[key] = head

    digest = hashlib.sha256(
        canonical_json(sorted((event.event_id, event.content_hash) for event in index.values()))
    ).hexdigest()
    return ReplayResult(entities, heads, tuple(conflicts), digest)


def _index_events(events: Sequence[AuthorityEvent]) -> dict[str, AuthorityEvent]:
    index: dict[str, AuthorityEvent] = {}
    for event in events:
        if not isinstance(event, AuthorityEvent):
            raise ReplayError("replay accepts only AuthorityEvent values")
        existing = index.get(event.event_id)
        if existing is None:
            index[event.event_id] = event
        elif existing.content_hash != event.content_hash:
            raise ReplayError(f"event ID collision: {event.event_id}")
    return index


def _validate_parent_graph(index: Mapping[str, AuthorityEvent]) -> None:
    for event in index.values():
        for parent_id in event.parent_event_ids:
            parent = index.get(parent_id)
            if parent is None:
                raise ReplayError(f"missing parent {parent_id} for event {event.event_id}")
            if _entity_key(parent) != _entity_key(event):
                raise ReplayError(
                    f"parent {parent_id} and event {event.event_id} must belong to the same entity"
                )

    remaining_parents = {
        event_id: len(event.parent_event_ids) for event_id, event in index.items()
    }
    children: dict[str, list[str]] = {event_id: [] for event_id in index}
    for event in index.values():
        for parent_id in event.parent_event_ids:
            children[parent_id].append(event.event_id)

    ready = [event_id for event_id, count in remaining_parents.items() if count == 0]
    heapq.heapify(ready)
    visited = 0
    while ready:
        event_id = heapq.heappop(ready)
        visited += 1
        for child_id in children[event_id]:
            remaining_parents[child_id] -= 1
            if remaining_parents[child_id] == 0:
                heapq.heappush(ready, child_id)
    if visited != len(index):
        raise ReplayError("parent graph contains a cycle")


def _reject_silent_conflict_joins(index: Mapping[str, AuthorityEvent]) -> None:
    for event in index.values():
        if len(event.parent_event_ids) > 1 and event.operation != "resolved":
            raise ReplayError(
                f"event {event.event_id} must be an explicit resolution event to join multiple heads"
            )


def _entity_key(event: AuthorityEvent) -> EntityKey:
    return event.entity_type, event.entity_id
