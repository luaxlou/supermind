import json

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
