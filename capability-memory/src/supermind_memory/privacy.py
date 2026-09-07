"""Portable-context admission and explicit privacy-history cleanup.

Reports identify records and issue classes, never matched private values.
This is a deterministic privacy boundary, not a semantic abstraction proof.
"""
from collections.abc import Mapping, Sequence
from html import unescape
import re
from urllib.parse import unquote

from supermind_memory.event_model import AuthorityEvent
from supermind_memory.replay import replay


POLICY = 'portable-context-v1'
_PATTERNS = (
    ('local_file_uri', re.compile(r'(?<![\w])file\s*:\s*/', re.I)),
    ('private_conversation', re.compile(r'codex\s*:\s*/{2}(?:threads|chats)/', re.I)),
    ('personal_directory', re.compile(r'(?<![\w:/])/(?:Users|home|private/var/folders|var/folders)/[^\s<>"\']+', re.I)),
    ('personal_directory', re.compile(r'[a-z]:[\\/]+Users[\\/]+', re.I)),
)


def private_context_issues(value) -> tuple[str, ...]:
    found = set()
    if isinstance(value, str):
        text = value
        for _ in range(3):
            decoded = unescape(unquote(text)).replace('\\/', '/')
            if decoded == text:
                break
            text = decoded
        found.update(kind for kind, pattern in _PATTERNS if pattern.search(text))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            found.update(private_context_issues(key))
            found.update(private_context_issues(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(private_context_issues(item))
    return tuple(sorted(found))


def assert_portable(events: Sequence[AuthorityEvent]) -> None:
    from supermind_memory.sync import SyncBlocked
    if any(private_context_issues(e.to_document()) for e in events):
        raise SyncBlocked('private_context_rejected', ('Remove local provenance from synchronized records; use a portable asset.',))


def audit_privacy(coordinator, *, git_history=False) -> dict:
    # Loading through the owned authority store preserves normal integrity checks.
    events = coordinator.store.load_all()
    current = replay(events)
    report = lambda items: [dict(entity_type=e.entity_type, entity_id=e.entity_id,
                               issues=private_context_issues(e.to_document()))
                            for e in items if private_context_issues(e.to_document())]
    result = {'policy': POLICY, 'event_set_digest': current.digest,
            'historical_event_findings': report(events),
            'current_record_findings': report(current.entities.values()),
            'git_history_scanned': False}
    if git_history:
        from supermind_memory.sync import SyncBlocked
        def git(*args):
            command = coordinator.git._runner.run(('git', *args), cwd=coordinator.paths.checkout)
            if command.returncode:
                raise SyncBlocked('privacy_history_scan_failed')
            return command.stdout
        blobs = git('cat-file', '--batch-all-objects', '--batch-check=%(objectname) %(objecttype)').decode().splitlines()
        affected = []
        count = 0
        for row in blobs:
            oid, kind = row.split()
            if kind != 'blob':
                continue
            count += 1
            data = git('cat-file', 'blob', oid).decode('utf-8', errors='replace')
            # JSON events may encode path separators; parse when possible.
            import json
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                pass
            if private_context_issues(data):
                affected.append(oid)
        result.update(git_history_scanned=True, local_blob_count=count,
                      affected_local_blob_count=len(affected),
                      local_commit_count=int(git('rev-list', '--count', '--all')))
    return result


def plan_privacy_purge(events: Sequence[AuthorityEvent]):
    from supermind_memory.purge import PurgePlan
    from supermind_memory.sync import SyncBlocked
    current = replay(events)
    if current.conflicts:
        raise SyncBlocked('authority_conflict')
    removed = {key for key, e in current.entities.items() if private_context_issues(e.to_document())}

    def references(value, ids):
        if isinstance(value, str):
            return value in ids
        if isinstance(value, Mapping):
            return any(references(x, ids) for x in value.values())
        if isinstance(value, (tuple, list)):
            return any(references(x, ids) for x in value)
        return False

    while True:
        ids = {identifier for _, identifier in removed}
        incoming = {key for key, e in current.entities.items()
                    if key not in removed and references(e.payload, ids)}
        if any(kind == 'capability' for kind, _ in incoming):
            raise SyncBlocked('privacy_retained_capability_references',
                              tuple(f'{kind}/{key}' for kind, key in sorted(incoming)))
        if not incoming:
            break
        removed.update(incoming)
    invalidated = {e.payload.get('capability_id') for key, e in current.entities.items()
                   if key in removed and e.entity_type == 'evidence'}
    retained = []
    for key, e in sorted(current.entities.items()):
        if key in removed:
            continue
        payload = e.to_document()['payload']
        if e.entity_type == 'capability' and e.entity_id in invalidated:
            if 'lifecycle' in payload:
                payload['lifecycle'] = 'candidate'
                payload['last_verified_at'] = None
                payload['abstraction_status'] = 'in_progress'
        retained.append(AuthorityEvent.create(event_id='baseline-' + e.content_hash,
            device_id=e.device_id, entity_type=e.entity_type, entity_id=e.entity_id,
            operation=e.operation, parent_event_ids=(), occurred_at=e.occurred_at, payload=payload))
    assert_portable(retained)
    replay(retained)
    return PurgePlan(current.digest, tuple(sorted(removed)), tuple(retained))
