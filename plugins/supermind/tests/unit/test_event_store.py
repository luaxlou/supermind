from pathlib import Path

import pytest

from supermind_memory.event_model import AuthorityEvent
from supermind_memory.event_store import (
    MAX_EVENT_FILE_BYTES,
    EventCollisionError,
    EventFileTooLarge,
    EventStore,
    UnsafeEventPath,
)


def _event(**overrides) -> AuthorityEvent:
    values = {
        "event_id": "01JTEST0000000000000000001",
        "device_id": "device-a",
        "entity_type": "capability",
        "entity_id": "login",
        "operation": "registered",
        "parent_event_ids": (),
        "occurred_at": "2026-09-06T12:00:00Z",
        "payload": {"name": "Login"},
    }
    values.update(overrides)
    return AuthorityEvent.create(**values)


@pytest.fixture
def event() -> AuthorityEvent:
    return _event()


def test_append_uses_device_month_and_never_overwrites(tmp_path: Path, event: AuthorityEvent):
    store = EventStore(tmp_path)

    path = store.append(event)

    assert path.relative_to(tmp_path).as_posix() == (
        "events/v1/device-a/2026-09/01JTEST0000000000000000001.json"
    )
    assert path.read_bytes() == event.to_bytes()
    assert store.append(event) == path

    altered = _event(payload={"name": "Different"})
    with pytest.raises(EventCollisionError):
        store.append(altered)
    assert path.read_bytes() == event.to_bytes()


def test_symlinked_event_tree_is_rejected(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "events").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeEventPath):
        EventStore(tmp_path).load_all()


def test_symlinked_month_is_rejected_without_writing_outside(tmp_path: Path, event: AuthorityEvent):
    outside = tmp_path / "outside"
    outside.mkdir()
    device = tmp_path / "events" / "v1" / "device-a"
    device.mkdir(parents=True)
    (device / "2026-09").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeEventPath):
        EventStore(tmp_path).append(event)
    assert tuple(outside.iterdir()) == ()


def test_load_all_is_deterministic_and_decodes_canonical_events(tmp_path: Path):
    later = _event(
        event_id="01JTEST0000000000000000002",
        occurred_at="2026-10-06T12:00:00Z",
        payload={"name": "Later"},
    )
    earlier = _event()
    store = EventStore(tmp_path)
    store.append(later)
    store.append(earlier)

    assert store.load_all() == (earlier, later)


def test_load_rejects_oversized_file_before_event_decoding(tmp_path: Path):
    path = tmp_path / "events" / "v1" / "device-a" / "2026-09" / "oversized.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{" + b" " * MAX_EVENT_FILE_BYTES)

    with pytest.raises(EventFileTooLarge):
        EventStore(tmp_path).load_all()


def test_append_rejects_oversized_serialized_event_before_creating_file(tmp_path: Path):
    event = _event(payload={f"field-{index}": "x" * 2_000 for index in range(600)})
    assert len(event.to_bytes()) > MAX_EVENT_FILE_BYTES

    with pytest.raises(EventFileTooLarge):
        EventStore(tmp_path).append(event)
    assert not (tmp_path / "events").exists()
