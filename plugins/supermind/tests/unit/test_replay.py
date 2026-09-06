import pytest

from supermind_memory.event_model import AuthorityEvent, EntityConflict
from supermind_memory.replay import ReplayError, replay


def _event(**overrides) -> AuthorityEvent:
    values = {
        "event_id": "01JBASE00000000000000000001",
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
def base_event() -> AuthorityEvent:
    return _event()


@pytest.fixture
def update_event(base_event: AuthorityEvent) -> AuthorityEvent:
    return _event(
        event_id="01JPRIMARY0000000000000001",
        operation="updated",
        parent_event_ids=(base_event.event_id,),
        occurred_at="2026-09-06T13:00:00Z",
        payload={"name": "Login v2"},
    )


@pytest.fixture
def tombstone_event(update_event: AuthorityEvent) -> AuthorityEvent:
    return _event(
        event_id="01JTOMBSTONE00000000000001",
        operation="tombstoned",
        parent_event_ids=(update_event.event_id,),
        occurred_at="2026-09-06T14:00:00Z",
        payload={},
    )


@pytest.fixture
def event_chain(base_event: AuthorityEvent, update_event: AuthorityEvent):
    evidence = _event(
        event_id="01JEVIDENCE000000000000001",
        entity_type="evidence",
        entity_id="evidence-login",
        operation="observed",
        payload={"capability_id": "login", "kind": "test"},
    )
    return base_event, evidence, update_event


def test_replay_is_independent_of_input_order(event_chain):
    forward = replay(event_chain)
    reverse = replay(tuple(reversed(event_chain)))

    assert forward.digest == reverse.digest
    assert forward.entities == reverse.entities
    assert forward.heads == reverse.heads
    assert forward.conflicts == reverse.conflicts


def test_replay_materializes_single_heads_and_pins_digest(base_event, update_event):
    result = replay((update_event, base_event))

    assert result.entities == {("capability", "login"): update_event}
    assert result.heads == {("capability", "login"): (update_event.event_id,)}
    assert result.conflicts == ()
    assert result.digest == "d446bbc1f60b683f70cb277c27b56a4ed34b03603a589c31135f29090b00d411"


def test_sibling_updates_create_conflict(base_event, update_event):
    sibling = _event(
        event_id="01JSIBLING000000000000001",
        operation="updated",
        parent_event_ids=(base_event.event_id,),
        occurred_at="2026-09-01T01:00:00Z",
        payload={"name": "B"},
    )

    result = replay((base_event, update_event, sibling))

    assert result.conflicts == (
        EntityConflict("capability", "login", (update_event.event_id, sibling.event_id)),
    )
    assert result.heads == {
        ("capability", "login"): (update_event.event_id, sibling.event_id),
    }
    assert ("capability", "login") not in result.entities
    assert result.diagnostic_ancestors == {("capability", "login"): base_event}
    with pytest.raises(TypeError):
        result.diagnostic_ancestors[("capability", "login")] = update_event


def test_diagnostic_ancestor_after_prior_merge_uses_deepest_common_node(base_event):
    left = _event(event_id="left", parent_event_ids=(base_event.event_id,), operation="updated")
    right = _event(event_id="right", parent_event_ids=(base_event.event_id,), operation="updated")
    merged = _event(event_id="merged", parent_event_ids=("left", "right"), operation="resolved")
    a = _event(event_id="a", parent_event_ids=("merged",), operation="updated")
    b = _event(event_id="b", parent_event_ids=("merged",), operation="updated")
    result = replay((b, merged, right, a, base_event, left))
    assert result.diagnostic_ancestors == {("capability", "login"): merged}
    assert not result.entities


def test_resolution_must_name_every_conflicting_head(base_event, update_event):
    sibling = _event(
        event_id="01JSIBLING000000000000001",
        operation="updated",
        parent_event_ids=(base_event.event_id,),
        payload={"name": "B"},
    )
    partial = _event(
        event_id="01JRESOLUTION0000000000001",
        operation="resolved",
        parent_event_ids=(update_event.event_id,),
        payload={"name": "Incomplete"},
    )

    result = replay((partial, sibling, update_event, base_event))

    assert result.conflicts == (
        EntityConflict("capability", "login", (partial.event_id, sibling.event_id)),
    )
    assert ("capability", "login") not in result.entities


def test_resolution_of_every_head_materializes_resolved_event(base_event, update_event):
    sibling = _event(
        event_id="01JSIBLING000000000000001",
        operation="updated",
        parent_event_ids=(base_event.event_id,),
        payload={"name": "B"},
    )
    resolution = _event(
        event_id="01JRESOLUTION0000000000001",
        operation="resolved",
        parent_event_ids=(update_event.event_id, sibling.event_id),
        payload={"name": "Merged"},
    )

    result = replay((resolution, sibling, base_event, update_event))

    assert result.conflicts == ()
    assert result.entities == {("capability", "login"): resolution}
    assert result.heads == {("capability", "login"): (resolution.event_id,)}


def test_non_resolution_event_cannot_silently_join_conflicting_heads(base_event, update_event):
    sibling = _event(
        event_id="01JSIBLING000000000000001",
        operation="updated",
        parent_event_ids=(base_event.event_id,),
        payload={"name": "B"},
    )
    silent_merge = _event(
        event_id="01JMERGE000000000000000001",
        operation="updated",
        parent_event_ids=(update_event.event_id, sibling.event_id),
        payload={"name": "Merged"},
    )

    with pytest.raises(ReplayError, match="resolution event"):
        replay((base_event, update_event, sibling, silent_merge))


def test_tombstone_removes_entity_from_materialized_state(
    base_event, update_event, tombstone_event
):
    result = replay((base_event, tombstone_event, update_event))

    assert ("capability", "login") not in result.entities
    assert result.heads == {("capability", "login"): (tombstone_event.event_id,)}


def test_replay_rejects_missing_parent(base_event):
    orphan = _event(
        event_id="01JORPHAN00000000000000001",
        operation="updated",
        parent_event_ids=("01JMISSING0000000000000001",),
    )

    with pytest.raises(ReplayError, match="missing parent"):
        replay((base_event, orphan))


def test_replay_rejects_parent_from_another_entity(base_event):
    wrong_entity = _event(
        event_id="01JOTHER000000000000000001",
        entity_id="upload",
        operation="updated",
        parent_event_ids=(base_event.event_id,),
    )

    with pytest.raises(ReplayError, match="same entity"):
        replay((base_event, wrong_entity))


def test_replay_rejects_parent_cycle():
    first = _event(
        event_id="01JCYCLE-A0000000000000001",
        operation="updated",
        parent_event_ids=("01JCYCLE-B0000000000000001",),
    )
    second = _event(
        event_id="01JCYCLE-B0000000000000001",
        operation="updated",
        parent_event_ids=(first.event_id,),
    )

    with pytest.raises(ReplayError, match="cycle"):
        replay((first, second))


def test_replay_handles_long_valid_chain_without_python_recursion():
    chain = []
    parent_ids = ()
    for index in range(1_100):
        event = _event(
            event_id=f"long-chain-{1_099 - index:04d}",
            operation="registered" if index == 0 else "updated",
            parent_event_ids=parent_ids,
            payload={"revision": index},
        )
        chain.append(event)
        parent_ids = (event.event_id,)

    result = replay(tuple(reversed(chain)))

    assert result.entities == {("capability", "login"): chain[-1]}


def test_replay_deduplicates_identical_events(base_event):
    assert replay((base_event, base_event)) == replay((base_event,))


def test_replay_rejects_same_event_id_with_different_valid_content(base_event):
    collision = _event(payload={"name": "Different"})
    assert collision.event_id == base_event.event_id
    assert collision.content_hash != base_event.content_hash

    with pytest.raises(ReplayError, match="event ID collision"):
        replay((base_event, collision))


def test_empty_replay_has_canonical_empty_event_set_digest():
    result = replay(())

    assert result.entities == {}
    assert result.heads == {}
    assert result.conflicts == ()
    assert result.digest == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
