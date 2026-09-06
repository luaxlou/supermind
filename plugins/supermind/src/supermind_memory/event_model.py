"""Immutable, canonical authority events for distributed capability memory."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias
from types import MappingProxyType

from supermind_memory.redaction import sanitize_json


JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
FrozenJSONValue: TypeAlias = None | bool | int | float | str | tuple["FrozenJSONValue", ...] | Mapping[str, "FrozenJSONValue"]

_EVENT_FIELDS = frozenset({
    "schema_version", "event_id", "device_id", "entity_type", "entity_id",
    "operation", "parent_event_ids", "occurred_at", "payload", "content_hash",
})
_MARKER_FIELDS = frozenset({
    "format", "event_schema_versions", "renderer_version", "repository_id", "default_branch",
})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_PAYLOAD_DEPTH = 16
# Structural limits are mirrored by the v1 JSON Schema.  The UTF-8 byte limit
# is an input-transport safety boundary and intentionally remains decoder-only.
_MAX_CONTAINER_ITEMS = 1_024
_MAX_PAYLOAD_BYTES = 256 * 1024
_MAX_PARENT_EVENT_IDS = 1_024
_MARKER_TEXT = re.compile(r"\S(?:.*\S)?\Z")

# Operations are intentionally entity-specific: replay must never infer a
# transition from an arbitrary string submitted by a newer or malformed client.
_OPERATIONS: dict[str, frozenset[str]] = {
    "capability": frozenset({"registered", "updated", "tombstoned", "resolved"}),
    "evidence": frozenset({"observed", "updated", "tombstoned", "resolved"}),
    "relationship": frozenset({"declared", "updated", "tombstoned", "resolved"}),
    "demand": frozenset({"observed", "linked", "resolved", "tombstoned"}),
    "reuse_outcome": frozenset({"recorded", "tombstoned"}),
    "metadata": frozenset({"set", "tombstoned"}),
    "audit": frozenset({"observed"}),
}


class EventValidationError(ValueError):
    """Raised when an event or repository marker crosses the authority boundary."""


def canonical_json(value: object) -> bytes:
    """Encode a JSON value in the sole byte representation used for hashing."""
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise EventValidationError("value is not canonical JSON") from error


@dataclass(frozen=True)
class EntityConflict:
    entity_type: str
    entity_id: str
    event_ids: tuple[str, ...]


@dataclass(frozen=True)
class MemoryMarker:
    format: int
    event_schema_versions: tuple[int, ...]
    renderer_version: str
    repository_id: str
    default_branch: str

    def __post_init__(self) -> None:
        versions = tuple(self.event_schema_versions)
        object.__setattr__(self, "event_schema_versions", versions)
        _validate_marker_document(self.to_document())

    @classmethod
    def create(
        cls,
        *,
        renderer_version: str,
        repository_id: str,
        default_branch: str,
    ) -> "MemoryMarker":
        return cls(1, (1,), renderer_version, repository_id, default_branch)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "MemoryMarker":
        document = _decode_document(raw, "memory marker")
        _validate_marker_document(document)
        return cls(
            document["format"],
            tuple(document["event_schema_versions"]),
            document["renderer_version"],
            document["repository_id"],
            document["default_branch"],
        )

    def to_document(self) -> dict[str, object]:
        return {
            "default_branch": self.default_branch,
            "event_schema_versions": list(self.event_schema_versions),
            "format": self.format,
            "renderer_version": self.renderer_version,
            "repository_id": self.repository_id,
        }

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_document()) + b"\n"


@dataclass(frozen=True)
class AuthorityEvent:
    schema_version: int
    event_id: str
    device_id: str
    entity_type: str
    entity_id: str
    operation: str
    parent_event_ids: tuple[str, ...]
    occurred_at: str
    payload: Mapping[str, FrozenJSONValue]
    content_hash: str

    def __post_init__(self) -> None:
        parents = tuple(self.parent_event_ids)
        payload = _thaw_payload(self.payload)
        document = {
            **_event_document(
                self.schema_version, self.event_id, self.device_id, self.entity_type,
                self.entity_id, self.operation, parents, self.occurred_at, payload,
            ),
            "content_hash": self.content_hash,
        }
        _validate_event_document(document, verify_hash=True)
        object.__setattr__(self, "parent_event_ids", parents)
        object.__setattr__(self, "payload", _freeze_payload(payload))

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        device_id: str,
        entity_type: str,
        entity_id: str,
        operation: str,
        parent_event_ids: Sequence[str],
        occurred_at: str,
        payload: Mapping[str, JSONValue],
    ) -> "AuthorityEvent":
        clean = _validate_payload(sanitize_json(payload))
        parents = _normalize_parent_event_ids(parent_event_ids)
        unsigned = _event_document(
            1, event_id, device_id, entity_type, entity_id, operation,
            parents, occurred_at, clean,
        )
        _validate_event_document({**unsigned, "content_hash": "0" * 64}, verify_hash=False)
        digest = hashlib.sha256(canonical_json(unsigned)).hexdigest()
        return cls(
            1, event_id, device_id, entity_type, entity_id, operation,
            parents, occurred_at, clean, digest,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "AuthorityEvent":
        document = _decode_document(raw, "authority event")
        _validate_event_document(document, verify_hash=True)
        return cls(
            document["schema_version"],
            document["event_id"],
            document["device_id"],
            document["entity_type"],
            document["entity_id"],
            document["operation"],
            tuple(document["parent_event_ids"]),
            document["occurred_at"],
            _validate_payload(document["payload"]),
            document["content_hash"],
        )

    def to_document(self) -> dict[str, object]:
        return {
            **_event_document(
                self.schema_version, self.event_id, self.device_id, self.entity_type,
                self.entity_id, self.operation, self.parent_event_ids, self.occurred_at,
                self.payload,
            ),
            "content_hash": self.content_hash,
        }

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_document()) + b"\n"


def _event_document(
    schema_version: int,
    event_id: str,
    device_id: str,
    entity_type: str,
    entity_id: str,
    operation: str,
    parent_event_ids: Sequence[str],
    occurred_at: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "event_id": event_id,
        "device_id": device_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "operation": operation,
        "parent_event_ids": list(parent_event_ids),
        "occurred_at": occurred_at,
        "payload": _thaw_payload(payload),
    }


def _decode_document(raw: bytes, label: str) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise EventValidationError(f"{label} must be UTF-8 bytes")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_members,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, EventValidationError) as error:
        raise EventValidationError(f"invalid {label} JSON: {error}") from error
    if type(value) is not dict:
        raise EventValidationError(f"{label} must be a JSON object")
    return value


def _reject_json_constant(value: str) -> None:
    raise EventValidationError(f"non-finite JSON value: {value}")


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise EventValidationError(f"duplicate JSON member: {key}")
        document[key] = value
    return document


def _validate_event_document(document: Mapping[str, object], *, verify_hash: bool) -> None:
    _require_exact_keys(document, _EVENT_FIELDS, "authority event")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise EventValidationError("unsupported event schema version")
    for field in ("event_id", "device_id", "entity_id"):
        _validate_identifier(field, document[field])
    entity_type = document["entity_type"]
    operation = document["operation"]
    if type(entity_type) is not str or type(operation) is not str or operation not in _OPERATIONS.get(entity_type, ()):
        raise EventValidationError("unsupported entity and operation pair")
    _validate_utc_timestamp(document["occurred_at"])
    parents = document["parent_event_ids"]
    if type(parents) is not list or any(type(item) is not str for item in parents):
        raise EventValidationError("parent_event_ids must be a JSON array of identifiers")
    for parent in parents:
        _validate_identifier("parent_event_id", parent)
    if len(parents) != len(set(parents)):
        raise EventValidationError("parent_event_ids must be unique")
    if len(parents) > _MAX_PARENT_EVENT_IDS:
        raise EventValidationError("parent_event_ids contains too many identifiers")
    if parents != sorted(parents):
        raise EventValidationError("parent_event_ids must be sorted")
    payload = _validate_payload(document["payload"])
    try:
        sanitized_payload = sanitize_json(payload)
    except ValueError as error:
        raise EventValidationError("payload is not valid sanitized JSON") from error
    if sanitized_payload != payload:
        raise EventValidationError("payload contains unsanitized text")
    content_hash = document["content_hash"]
    if type(content_hash) is not str or _HASH.fullmatch(content_hash) is None:
        raise EventValidationError("content_hash must be a lowercase SHA-256 digest")
    if verify_hash:
        unsigned = {key: value for key, value in document.items() if key != "content_hash"}
        expected = hashlib.sha256(canonical_json(unsigned)).hexdigest()
        if content_hash != expected:
            raise EventValidationError("content_hash does not match canonical event content")
    # Ensure a direct constructor cannot retain an invalid nested value.
    _validate_payload(payload)


def _validate_marker_document(document: Mapping[str, object]) -> None:
    _require_exact_keys(document, _MARKER_FIELDS, "memory marker")
    if type(document["format"]) is not int or document["format"] != 1:
        raise EventValidationError("unsupported memory format")
    versions = document["event_schema_versions"]
    if (
        type(versions) is not list
        or len(versions) != 1
        or type(versions[0]) is not int
        or versions[0] != 1
    ):
        raise EventValidationError("memory marker must support exactly event schema version 1")
    for field in ("renderer_version", "repository_id", "default_branch"):
        value = document[field]
        if type(value) is not str or not value or len(value) > 128 or _MARKER_TEXT.fullmatch(value) is None:
            raise EventValidationError(f"{field} must be a non-empty stable string")
    if any(character.isspace() for character in document["default_branch"]):
        raise EventValidationError("default_branch cannot contain whitespace")


def _require_exact_keys(document: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    keys = set(document)
    if keys != expected:
        missing = sorted(expected - keys)
        unknown = sorted(keys - expected)
        raise EventValidationError(f"{label} keys do not match contract; missing={missing}, unknown={unknown}")


def _validate_identifier(field: str, value: object) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise EventValidationError(f"{field} must be a stable identifier")


def _validate_utc_timestamp(value: object) -> None:
    if type(value) is not str or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise EventValidationError("occurred_at must be an RFC 3339 UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EventValidationError("occurred_at must be a valid UTC timestamp") from error


def _normalize_parent_event_ids(parent_event_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(parent_event_ids, (str, bytes)):
        raise EventValidationError("parent_event_ids must be a sequence of identifiers")
    try:
        parents = tuple(parent_event_ids)
    except TypeError as error:
        raise EventValidationError("parent_event_ids must be a sequence of identifiers") from error
    for parent in parents:
        _validate_identifier("parent_event_id", parent)
    if len(parents) != len(set(parents)):
        raise EventValidationError("parent_event_ids must be unique")
    if len(parents) > _MAX_PARENT_EVENT_IDS:
        raise EventValidationError("parent_event_ids contains too many identifiers")
    return tuple(sorted(parents))


def _freeze_payload(payload: Mapping[str, object]) -> Mapping[str, FrozenJSONValue]:
    frozen = _freeze_json_value(_thaw_payload(payload))
    if not isinstance(frozen, Mapping):  # pragma: no cover - payload is validated as an object.
        raise EventValidationError("payload must be a JSON object")
    return frozen


def _freeze_json_value(value: JSONValue) -> FrozenJSONValue:
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _thaw_payload(payload: Mapping[str, object]) -> dict[str, JSONValue]:
    value = _thaw_json_value(payload)
    if type(value) is not dict:
        raise EventValidationError("payload must be a JSON object")
    return value


def _thaw_json_value(value: object) -> JSONValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}  # type: ignore[dict-item]
    if isinstance(value, (list, tuple)):
        return [_thaw_json_value(item) for item in value]
    return value  # type: ignore[return-value]


def _validate_payload(value: object) -> dict[str, JSONValue]:
    if type(value) is not dict:
        raise EventValidationError("payload must be a JSON object")
    _validate_json_value(value, depth=0)
    payload = dict(value)
    if len(canonical_json(payload)) > _MAX_PAYLOAD_BYTES:
        raise EventValidationError("payload is too large")
    return payload  # type: ignore[return-value]


def _validate_json_value(value: object, *, depth: int) -> None:
    if depth > _MAX_PAYLOAD_DEPTH:
        raise EventValidationError("payload is nested too deeply")
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise EventValidationError("payload numbers must be finite")
        return
    if type(value) is list:
        if depth >= _MAX_PAYLOAD_DEPTH:
            raise EventValidationError("payload is nested too deeply")
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise EventValidationError("payload container contains too many values")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if type(value) is dict:
        if depth >= _MAX_PAYLOAD_DEPTH:
            raise EventValidationError("payload is nested too deeply")
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise EventValidationError("payload container contains too many values")
        for key, item in value.items():
            if type(key) is not str:
                raise EventValidationError("payload object keys must be strings")
            _validate_json_value(item, depth=depth + 1)
        return
    raise EventValidationError(f"payload contains unsupported {type(value).__name__}")
