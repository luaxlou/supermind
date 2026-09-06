import json
from pathlib import Path

import pytest

from supermind_memory.event_model import (
    AuthorityEvent,
    EventValidationError,
    MemoryMarker,
    canonical_json,
)


def _deep_value():
    value = "too deep"
    for _ in range(17):
        value = [value]
    return value


def _event(**overrides):
    values = {
        "event_id": "01JTEST0000000000000000099",
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


def _rehash(document):
    unsigned = {key: value for key, value in document.items() if key != "content_hash"}
    import hashlib

    document["content_hash"] = hashlib.sha256(canonical_json(unsigned)).hexdigest()
    return document


def test_authority_event_is_canonical_hashed_and_round_trips():
    event = AuthorityEvent.create(
        event_id="01JTEST0000000000000000001",
        device_id="device-a",
        entity_type="capability",
        entity_id="login",
        operation="registered",
        parent_event_ids=(),
        occurred_at="2026-09-06T12:00:00Z",
        payload={"name": "登录", "stack": ["python"]},
    )

    encoded = event.to_bytes()

    assert encoded.endswith(b"\n")
    assert AuthorityEvent.from_bytes(encoded) == event
    assert b'"content_hash"' in encoded
    assert encoded == canonical_json(json.loads(encoded)) + b"\n"


def test_secret_is_removed_before_event_hash_and_payload():
    event = AuthorityEvent.create(
        event_id="01JTEST0000000000000000002",
        device_id="device-a",
        entity_type="evidence",
        entity_id="evidence-1",
        operation="observed",
        parent_event_ids=(),
        occurred_at="2026-09-06T12:00:00Z",
        payload={"supporting_uri": "https://example.test/?access_token=secret-value"},
    )

    assert b"secret-value" not in event.to_bytes()
    assert event.payload == {"supporting_uri": "https://example.test/?access_token=[REDACTED]"}


@pytest.fixture
def valid_event_document():
    return json.loads(
        AuthorityEvent.create(
            event_id="01JTEST0000000000000000003",
            device_id="device-a",
            entity_type="capability",
            entity_id="login",
            operation="registered",
            parent_event_ids=(),
            occurred_at="2026-09-06T12:00:00Z",
            payload={"name": "Login"},
        ).to_bytes()
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unexpected": True}),
        lambda value: value.update({"occurred_at": "2026-09-06 12:00"}),
        lambda value: value.update({"content_hash": "0" * 64}),
        lambda value: value.update({"parent_event_ids": ["same", "same"]}),
    ],
)
def test_invalid_event_document_is_rejected(valid_event_document, mutation):
    mutation(valid_event_document)

    with pytest.raises(EventValidationError):
        AuthorityEvent.from_bytes(json.dumps(valid_event_document).encode())


@pytest.mark.parametrize(
    ("entity_type", "operation"),
    [("capability", "observed"), ("unknown", "registered")],
)
def test_event_rejects_unsupported_entity_operation_pairs(entity_type, operation):
    with pytest.raises(EventValidationError):
        AuthorityEvent.create(
            event_id="01JTEST0000000000000000004",
            device_id="device-a",
            entity_type=entity_type,
            entity_id="login",
            operation=operation,
            parent_event_ids=(),
            occurred_at="2026-09-06T12:00:00Z",
            payload={},
        )


def test_event_normalizes_malformed_parent_identifiers_to_a_validation_error():
    with pytest.raises(EventValidationError):
        AuthorityEvent.create(
            event_id="01JTEST0000000000000000006",
            device_id="device-a",
            entity_type="capability",
            entity_id="login",
            operation="registered",
            parent_event_ids=("parent-a", 7),  # type: ignore[arg-type]
            occurred_at="2026-09-06T12:00:00Z",
            payload={},
        )


def test_marker_requires_complete_v1_repository_identity():
    marker = MemoryMarker.create(
        renderer_version="1.0.0",
        repository_id="12345",
        default_branch="main",
    )

    assert marker.to_document() == {
        "default_branch": "main",
        "event_schema_versions": [1],
        "format": 1,
        "renderer_version": "1.0.0",
        "repository_id": "12345",
    }
    assert MemoryMarker.from_bytes(marker.to_bytes()) == marker

    document = marker.to_document()
    del document["default_branch"]
    with pytest.raises(EventValidationError):
        MemoryMarker.from_bytes(json.dumps(document).encode())


@pytest.mark.parametrize(
    "payload",
    [
        {"value": float("inf")},
        {"value": _deep_value()},
        {"value": list(range(1_024))},
    ],
)
def test_event_rejects_nonfinite_or_unbounded_payloads(payload):
    with pytest.raises(EventValidationError):
        AuthorityEvent.create(
            event_id="01JTEST0000000000000000005",
            device_id="device-a",
            entity_type="capability",
            entity_id="login",
            operation="registered",
            parent_event_ids=(),
            occurred_at="2026-09-06T12:00:00Z",
            payload=payload,
        )


def test_decoder_rejects_a_rehashed_document_with_unredacted_secret(valid_event_document):
    valid_event_document["payload"] = {"token": "secret-value"}
    unsigned = {key: value for key, value in valid_event_document.items() if key != "content_hash"}
    import hashlib

    valid_event_document["content_hash"] = hashlib.sha256(canonical_json(unsigned)).hexdigest()

    with pytest.raises(EventValidationError, match="unsanitized"):
        AuthorityEvent.from_bytes(canonical_json(valid_event_document))


def test_authoritative_payload_is_deeply_immutable_after_creation():
    event = _event(payload={"nested": {"items": ["stable"]}})
    encoded = event.to_bytes()

    with pytest.raises((AttributeError, TypeError)):
        event.payload["nested"]["items"].append("mutated")

    assert event.to_bytes() == encoded
    assert AuthorityEvent.from_bytes(encoded) == event


def test_decoder_rejects_duplicate_members_even_when_last_member_hashes_cleanly():
    raw = _event().to_bytes().replace(
        b'"payload":',
        b'"payload":{"token":"secret-value"},"payload":',
        1,
    )

    assert b"secret-value" in raw
    with pytest.raises(EventValidationError, match="duplicate"):
        AuthorityEvent.from_bytes(raw)


@pytest.mark.parametrize("field", ["schema_version"])
def test_event_decoder_rejects_boolean_version_values(valid_event_document, field):
    valid_event_document[field] = True

    with pytest.raises(EventValidationError):
        AuthorityEvent.from_bytes(canonical_json(_rehash(valid_event_document)))


@pytest.mark.parametrize(
    "document",
    [
        {"format": True, "event_schema_versions": [1], "renderer_version": "1.0.0", "repository_id": "123", "default_branch": "main"},
        {"format": 1, "event_schema_versions": [True], "renderer_version": "1.0.0", "repository_id": "123", "default_branch": "main"},
    ],
)
def test_marker_decoder_rejects_boolean_version_values(document):
    with pytest.raises(EventValidationError):
        MemoryMarker.from_bytes(canonical_json(document))


def test_event_rejects_more_parents_than_the_schema_limit():
    with pytest.raises(EventValidationError, match="too many"):
        _event(parent_event_ids=tuple(f"parent-{index}" for index in range(1_025)))


def test_schemas_declare_the_decoder_limits_and_marker_whitespace_contract():
    schema_root = Path(__file__).parents[2] / "schemas" / "v1"
    event_schema = json.loads((schema_root / "event.schema.json").read_text())
    marker_schema = json.loads((schema_root / "memory.schema.json").read_text())

    assert event_schema["properties"]["schema_version"] == {"type": "integer", "const": 1}
    assert event_schema["properties"]["parent_event_ids"]["maxItems"] == 1_024
    assert event_schema["properties"]["payload"]["x-supermind-limits"] == {
        "max_depth": 16,
        "max_items": 1_024,
        "max_utf8_bytes": 262_144,
    }
    assert marker_schema["properties"]["format"] == {"type": "integer", "const": 1}
    assert marker_schema["properties"]["event_schema_versions"]["items"] == {"type": "integer", "const": 1}
    assert marker_schema["properties"]["renderer_version"]["pattern"] == "^\\S(?:.*\\S)?$"
    assert marker_schema["properties"]["repository_id"]["pattern"] == "^\\S(?:.*\\S)?$"
