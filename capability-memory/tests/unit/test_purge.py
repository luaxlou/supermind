import pytest

from supermind_memory.event_model import AuthorityEvent
from supermind_memory.replay import replay


def event(kind, identifier, payload):
    return AuthorityEvent.create(event_id=f"evt-{identifier}", device_id="test",
        entity_type=kind, entity_id=identifier,
        operation={"capability": "registered", "metadata": "set"}.get(kind, "observed"), parent_event_ids=(),
        occurred_at="2026-09-06T12:00:00Z", payload=payload)


def test_purge_removes_associated_records_but_preserves_unrelated_state():
    from supermind_memory.purge import plan_purge
    events = (event("capability", "login", {"name": "Login"}),
              event("capability", "adapter", {"name": "Adapter"}),
              event("evidence", "proof", {"capability_id": "adapter"}),
              event("demand", "need", {"linked_capability_id": "adapter"}),
              event("audit", "link", {"observation_id": "need"}),
              event("metadata", "preferences", {"value": "keep"}))
    plan = plan_purge(events, ("adapter",))
    assert plan.removed == (("audit", "link"), ("capability", "adapter"), ("demand", "need"),
                            ("evidence", "proof"))
    assert set(replay(plan.events).entities) == {("capability", "login"), ("metadata", "preferences")}
    assert all(not e.parent_event_ids for e in plan.events)


def test_purge_blocks_dependency_from_retained_capability():
    from supermind_memory.purge import plan_purge
    from supermind_memory.sync import SyncBlocked
    with pytest.raises(SyncBlocked, match="purge_retained_capability_references"):
        plan_purge((event("capability", "login", {"dependencies": ["adapter"]}),
                    event("capability", "adapter", {})), ("adapter",))


def test_purge_rejects_unknown_targets():
    from supermind_memory.purge import plan_purge
    from supermind_memory.sync import SyncBlocked
    with pytest.raises(SyncBlocked, match="purge_target_missing"):
        plan_purge((event("capability", "login", {}),), ("missing",))


def test_purge_does_not_silently_delete_shared_records():
    from supermind_memory.purge import plan_purge
    from supermind_memory.sync import SyncBlocked
    with pytest.raises(SyncBlocked, match="purge_shared_record"):
        plan_purge((event("capability", "login", {}), event("capability", "adapter", {}),
                    event("audit", "shared", {"capability_ids": ["login", "adapter"]})), ("adapter",))
