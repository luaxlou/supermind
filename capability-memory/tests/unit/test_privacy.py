from dataclasses import replace

import pytest

from supermind_memory.event_model import AuthorityEvent, MemoryMarker
from supermind_memory.replay import replay


def record(kind, key, payload):
    return AuthorityEvent.create(event_id='event-' + key, device_id='test',
        entity_type=kind, entity_id=key,
        operation={'capability': 'registered', 'metadata': 'set'}.get(kind, 'observed'),
        parent_event_ids=(), occurred_at='2026-09-07T00:00:00Z', payload=payload)


@pytest.mark.parametrize('value', [
    'file:' + '///tmp/private-note.md',
    '/' + 'Users/person/private/report.md',
    '/' + 'home/person/private/report.md',
    'C:' + chr(92) + r'Users\person\report.md',
    'codex:' + '//threads/private-conversation',
    '%66ile%3A%2F%2F%2Ftmp%2Fprivate-note.md',
])
def test_private_context_detected_without_echoing_value(value):
    from supermind_memory.privacy import private_context_issues
    issues = private_context_issues({'nested': [{'evidence': value}]})
    assert issues
    assert value not in str(issues)


def test_portable_assets_and_project_relative_examples_are_allowed():
    from supermind_memory.privacy import private_context_issues
    assert not private_context_issues({'source': 'https://github.com/example/tool',
        'template': 'Read docs/design.md; configure /opt/app and ${PROJECT_ROOT}. Profile: ordinary input.'})


def test_privacy_baseline_removes_provenance_and_old_versions_not_clean_method():
    from supermind_memory.privacy import plan_privacy_purge
    dirty = record('capability', 'method', {'contract': 'file:' + '///tmp/private.md'})
    clean = AuthorityEvent.create(event_id='event-clean', device_id='test', entity_type='capability',
        entity_id='method', operation='updated', parent_event_ids=(dirty.event_id,),
        occurred_at='2026-09-07T00:01:00Z', payload={'contract': 'independent method'})
    proof = record('evidence', 'proof', {'capability_id': 'method', 'supporting_uri': 'codex:' + '//threads/private'})
    other = record('capability', 'other', {'contract': 'unrelated reusable tool'})
    plan = plan_privacy_purge((dirty, clean, proof, other))
    assert plan.removed == (('evidence', 'proof'),)
    assert set(replay(plan.events).entities) == {('capability', 'method'), ('capability', 'other')}
    assert all(not e.parent_event_ids for e in plan.events)
    assert all('private' not in e.to_bytes().decode() for e in plan.events)


def test_privacy_purge_blocks_clean_capability_dependency_loss():
    from supermind_memory.privacy import plan_privacy_purge
    from supermind_memory.sync import SyncBlocked
    source = record('capability', 'private-source', {'source_uri': 'file:' + '///tmp/source'})
    dependent = record('capability', 'dependent', {'dependencies': ['private-source']})
    with pytest.raises(SyncBlocked, match='privacy_retained_capability_references'):
        plan_privacy_purge((source, dependent))


def test_marker_pins_privacy_policy_and_rejects_unknown_policy():
    marker = MemoryMarker.create(renderer_version='1', repository_id='R_test', default_branch='main')
    strict = replace(marker, privacy_policy='portable-context-v1')
    assert MemoryMarker.from_bytes(strict.to_bytes()) == strict
    with pytest.raises(ValueError):
        replace(marker, privacy_policy='unknown')


def test_strict_event_store_rejects_private_record_before_creating_event_file(tmp_path):
    from supermind_memory.event_store import EventStore, UnsafeEventPath
    from supermind_memory.sync import SyncBlocked
    marker = replace(MemoryMarker.create(renderer_version='1', repository_id='R_test', default_branch='main'),
                     privacy_policy='portable-context-v1')
    (tmp_path / 'memory.json').write_bytes(marker.to_bytes())
    with pytest.raises(SyncBlocked, match='private_context_rejected'):
        EventStore(tmp_path).append(record('metadata', 'secret', {'value': 'file:' + '///tmp/private'}))
    assert not (tmp_path / 'events').exists()
    (tmp_path / 'memory.json').unlink()
    (tmp_path / 'outside').write_bytes(marker.to_bytes())
    (tmp_path / 'memory.json').symlink_to(tmp_path / 'outside')
    with pytest.raises(UnsafeEventPath):
        EventStore(tmp_path).append(record('metadata', 'safe', {'value': 'safe'}))
