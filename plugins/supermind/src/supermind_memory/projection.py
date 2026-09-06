"""Typed, disposable projection of reviewed authority into the local store.

Payloads use domain record fields, not Arrow rows. A demand contains
``observation`` and its complete ``events`` history; reuse outcomes are Evidence
records, and historical audits are Event records. Metadata contains ``value``.
Conflict ancestors are diagnostics only and never enter searchable tables.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any

import pyarrow as pa

from supermind_memory.embeddings import EmbeddingProvider
from supermind_memory.event_model import AuthorityEvent, EntityConflict, canonical_json
from supermind_memory.replay import EntityKey, ReplayResult
from supermind_memory.repository import CapabilityRepository, _EVIDENCE_ORDER_KEY, validate_evidence
from supermind_memory.schema import AUTHORITATIVE_SCHEMA_VERSION_KEY, EMBEDDING_DIMENSION, SCHEMA_VERSION, TABLE_SCHEMAS
from supermind_memory.types import Capability, CapabilityMemoryBlocked, Event, Evidence, Relationship, RequirementEvent, RequirementObservation


AUTHORITY_DIGEST_KEY = "authority_event_set_digest"
MATERIALIZED_DIGEST_KEY = "authority_materialized_digest"
CONFLICTS_KEY = "authority_conflicts"
_RESERVED_METADATA = {AUTHORITY_DIGEST_KEY, MATERIALIZED_DIGEST_KEY, CONFLICTS_KEY,
                      AUTHORITATIVE_SCHEMA_VERSION_KEY, _EVIDENCE_ORDER_KEY}


@dataclass(frozen=True)
class AuthoritySnapshot:
    capabilities: tuple[Capability, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    relationships: tuple[Relationship, ...] = ()
    events: tuple[Event, ...] = ()
    requirement_observations: tuple[RequirementObservation, ...] = ()
    requirement_events: tuple[RequirementEvent, ...] = ()
    # Values are canonical JSON strings, making the snapshot deeply immutable.
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ProjectionDigest:
    event_set: str
    materialized_digest: str


@dataclass(frozen=True)
class ProjectionComparison:
    equivalent: bool
    differences: tuple[str, ...]
    materialized_digest: str


class ProjectionBlocked(CapabilityMemoryBlocked):
    def __init__(self, code: str, differences: tuple[str, ...]):
        super().__init__(code, f"{code}: " + ", ".join(differences), differences)


def _record(payload: dict[str, Any], record_type: type, table: str) -> Any:
    """Adapt domain JSON to the existing strict row/dataclass parser."""
    expected = {field.name for field in fields(record_type)}
    if set(payload) != expected:
        raise ValueError(f"{table} payload fields do not match {record_type.__name__}")
    row = dict(payload)
    json_fields = {
        "capabilities": ("category_path", "facets", "constraints", "stack", "runtime", "platform", "dependencies", "compatibility"),
        "relationships": ("compatibility", "evidence_ids"),
        "requirement_observations": ("requirement",),
    }
    for name in json_fields.get(table, ()):
        expected_type = dict if name == "requirement" else list
        if type(row[name]) is not expected_type:
            raise ValueError(f"{table}.{name} must be a {expected_type.__name__}")
        row[name] = canonical_json(row[name]).decode()
    parser_names = {"capabilities": "capability", "relationships": "relationship",
                    "requirement_observations": "requirement_observation", "requirement_events": "requirement_event",
                    "evidence": "evidence", "events": "event"}
    record = getattr(CapabilityRepository, f"_{parser_names[table]}_from_row")(row)
    if isinstance(record, Evidence):
        validate_evidence(record)
    return record


def authority_snapshot(
    entities: Mapping[EntityKey, AuthorityEvent], *, conflicts: Sequence[EntityConflict] = (),
    diagnostic_ancestors: Mapping[EntityKey, AuthorityEvent] | None = None,
    event_set_digest: str,
) -> AuthoritySnapshot:
    records: dict[str, list[Any]] = {name: [] for name in TABLE_SCHEMAS if name != "metadata"}
    metadata: dict[str, object] = {}
    conflicted = {(item.entity_type, item.entity_id) for item in conflicts}
    for key, event in sorted(entities.items()):
        if key != (event.entity_type, event.entity_id):
            raise ValueError("replay entity key does not match event")
        if key in conflicted:
            continue
        payload = event.to_document()["payload"]
        if event.entity_type == "metadata":
            if set(payload) != {"value"} or event.entity_id in _RESERVED_METADATA:
                raise ValueError("metadata payload uses invalid fields or a reserved projection key")
            metadata[event.entity_id] = payload["value"]
            continue
        if event.entity_type == "demand":
            if set(payload) != {"observation", "events"} or type(payload["events"]) is not list:
                raise ValueError("demand payload requires observation and complete events")
            observation = _record(payload["observation"], RequirementObservation, "requirement_observations")
            if observation.id != event.entity_id:
                raise ValueError("demand payload id does not match entity id")
            records["requirement_observations"].append(observation)
            for value in payload["events"]:
                history = _record(value, RequirementEvent, "requirement_events")
                if history.observation_id != observation.id:
                    raise ValueError("demand history belongs to another observation")
                records["requirement_events"].append(history)
            continue
        table, record_type = {
            "capability": ("capabilities", Capability), "evidence": ("evidence", Evidence),
            "reuse_outcome": ("evidence", Evidence), "relationship": ("relationships", Relationship),
            "audit": ("events", Event),
        }[event.entity_type]
        record = _record(payload, record_type, table)
        if record.id != event.entity_id:
            raise ValueError(f"{event.entity_type} payload id does not match entity id")
        records[table].append(record)
    metadata[AUTHORITY_DIGEST_KEY] = event_set_digest
    metadata[CONFLICTS_KEY] = [
        {"entity_type": item.entity_type, "entity_id": item.entity_id, "event_ids": sorted(item.event_ids),
         "ancestor": ancestor.to_document() if (ancestor := (diagnostic_ancestors or {}).get((item.entity_type, item.entity_id))) else None}
        for item in sorted(conflicts, key=lambda item: (item.entity_type, item.entity_id))
    ]
    metadata[AUTHORITATIVE_SCHEMA_VERSION_KEY] = SCHEMA_VERSION
    evidence_ids = sorted(item.id for item in records["evidence"])
    metadata[_EVIDENCE_ORDER_KEY] = {"next": len(evidence_ids) + 1,
                                      "sequences": {identifier: index for index, identifier in enumerate(evidence_ids, 1)}}
    return AuthoritySnapshot(
        **{name: tuple(sorted(items, key=lambda item: item.id)) for name, items in records.items()},
        metadata=tuple((key, canonical_json(value).decode()) for key, value in sorted(metadata.items())),
    )


def snapshot_rows(snapshot: AuthoritySnapshot, vectors: Mapping[str, Sequence[float]]) -> dict[str, list[dict[str, Any]]]:
    repository = CapabilityRepository
    for evidence in snapshot.evidence:
        validate_evidence(evidence)
    if set(vectors) != {capability.id for capability in snapshot.capabilities}:
        raise ValueError("vectors must exactly cover projected capabilities")
    for vector in vectors.values():
        if len(vector) != EMBEDDING_DIMENSION or any(not math.isfinite(float(value)) for value in vector):
            raise ValueError("projection vectors must have 384 finite values")
    rows = {
        "capabilities": [repository._capability_row(item, vectors[item.id]) for item in snapshot.capabilities],
        "evidence": [repository._evidence_row(item) for item in snapshot.evidence],
        "relationships": [repository._relationship_row(item) for item in snapshot.relationships],
        "events": [repository._event_row(item) for item in snapshot.events],
        "requirement_observations": [repository._requirement_observation_row(item) for item in snapshot.requirement_observations],
        "requirement_events": [repository._requirement_event_row(item) for item in snapshot.requirement_events],
        "metadata": [{"key": key, "value": value} for key, value in snapshot.metadata],
    }
    for row in rows["capabilities"]:
        repository._capability_from_row(row)
    for table, values in rows.items():
        key = "key" if table == "metadata" else "id"
        if len({row[key] for row in values}) != len(values):
            raise ValueError(f"duplicate projection IDs in {table}")
        for row in values:
            for field in TABLE_SCHEMAS[table]:
                value = row[field.name]
                if value is None and not field.nullable:
                    raise ValueError(f"{table}.{field.name} cannot be null")
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError(f"{table}.{field.name} must be finite")
        arrow = pa.Table.from_pylist(values, schema=TABLE_SCHEMAS[table])
        arrow.validate(full=True)
        if table == "capabilities" and any(
            not math.isfinite(value) for vector in arrow.column("vector").to_pylist() for value in vector
        ):
            raise ValueError("projection vectors must remain finite in float32 storage")
        rows[table] = arrow.to_pylist()
    repository._validate_requirement_history(rows["requirement_observations"], rows["requirement_events"])
    return rows


def _materialized_tables(rows: Mapping[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    tables = {}
    for name, values in rows.items():
        if name == "capabilities":
            values = [{key: value for key, value in row.items() if key not in {"vector", "search_text"}} for row in values]
        if name == "metadata":
            values = [row for row in values if row["key"] != MATERIALIZED_DIGEST_KEY]
        tables[name] = sorted(values, key=lambda row: row["key" if name == "metadata" else "id"])
    return tables


def materialized_digest(rows: Mapping[str, list[dict[str, Any]]]) -> str:
    return hashlib.sha256(canonical_json(_materialized_tables(rows))).hexdigest()


def _snapshot(result: ReplayResult) -> AuthoritySnapshot:
    return authority_snapshot(result.entities, conflicts=result.conflicts,
                              diagnostic_ancestors=result.diagnostic_ancestors, event_set_digest=result.digest)


def compare_projection(result: ReplayResult, repository: CapabilityRepository) -> ProjectionComparison:
    snapshot = _snapshot(result)
    expected = snapshot_rows(snapshot, {item.id: [0.0] * EMBEDDING_DIMENSION for item in snapshot.capabilities})
    with repository._writer_lock():
        actual = {name: repository._rows(name) for name in TABLE_SCHEMAS}
        expected_tables, actual_tables = _materialized_tables(expected), _materialized_tables(actual)
        differences = tuple(name for name in TABLE_SCHEMAS if expected_tables[name] != actual_tables[name])
        digest = materialized_digest(actual)
        if repository._get_metadata_unlocked(MATERIALIZED_DIGEST_KEY) != digest:
            differences = tuple(dict.fromkeys((*differences, "metadata")))
    return ProjectionComparison(not differences, differences, digest)


def project_authority(result: ReplayResult, repository: CapabilityRepository, embeddings: EmbeddingProvider) -> ProjectionDigest:
    snapshot = _snapshot(result)
    vectors = {item.id: embeddings.embed_query(str(repository._capability_row(item, [0.0] * EMBEDDING_DIMENSION)["search_text"]))
               for item in snapshot.capabilities}
    repository.replace_authority(snapshot, vectors)
    comparison = compare_projection(result, repository)
    if not comparison.equivalent:
        raise ProjectionBlocked("projection_mismatch", comparison.differences)
    return ProjectionDigest(result.digest, comparison.materialized_digest)
