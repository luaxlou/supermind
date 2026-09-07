"""Arrow schemas and storage constants for capability memory."""

from __future__ import annotations

import pyarrow as pa


SCHEMA_VERSION = 2
AUTHORITATIVE_SCHEMA_VERSION_KEY = "authoritative_schema_version"
LEGACY_TABLE_NAMES = frozenset({"capabilities", "evidence", "relationships", "events", "metadata"})
DEMAND_TABLE_NAMES = frozenset({"requirement_observations", "requirement_events"})
EMBEDDING_DIMENSION = 384


CAPABILITIES_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("summary", pa.string(), nullable=False),
        pa.field("category_path", pa.string(), nullable=False),
        pa.field("facets", pa.string(), nullable=False),
        pa.field("contract", pa.string(), nullable=False),
        pa.field("constraints", pa.string(), nullable=False),
        pa.field("artifact_type", pa.string(), nullable=False),
        pa.field("source_uri", pa.string(), nullable=False),
        pa.field("source_revision", pa.string(), nullable=False),
        pa.field("content_hash", pa.string(), nullable=False),
        pa.field("owner", pa.string(), nullable=False),
        pa.field("license", pa.string(), nullable=False),
        pa.field("stack", pa.string(), nullable=False),
        pa.field("runtime", pa.string(), nullable=False),
        pa.field("platform", pa.string(), nullable=False),
        pa.field("dependencies", pa.string(), nullable=False),
        pa.field("compatibility", pa.string(), nullable=False),
        pa.field("lifecycle", pa.string(), nullable=False),
        pa.field("confidence", pa.float64(), nullable=False),
        pa.field("expected_net_value", pa.float64(), nullable=False),
        pa.field("embedding_generation", pa.string(), nullable=False),
        pa.field("created_at", pa.string(), nullable=False),
        pa.field("updated_at", pa.string(), nullable=False),
        pa.field("last_verified_at", pa.string(), nullable=True),
        pa.field("abstraction_status", pa.string(), nullable=False),
        pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIMENSION), nullable=False),
        pa.field("search_text", pa.string(), nullable=False),
    ]
)

EVIDENCE_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("capability_id", pa.string(), nullable=False),
        pa.field("source_project", pa.string(), nullable=False),
        pa.field("evidence_type", pa.string(), nullable=False),
        pa.field("outcome", pa.string(), nullable=False),
        pa.field("metric_name", pa.string(), nullable=True),
        pa.field("metric_value", pa.float64(), nullable=True),
        pa.field("confidence", pa.float64(), nullable=False),
        pa.field("observed_at", pa.string(), nullable=False),
        pa.field("supporting_uri", pa.string(), nullable=True),
        pa.field("integration_effort", pa.float64(), nullable=False),
        pa.field("benefit", pa.float64(), nullable=False),
        pa.field("failure_risk", pa.float64(), nullable=False),
    ]
)

RELATIONSHIPS_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("source_id", pa.string(), nullable=False),
        pa.field("target_id", pa.string(), nullable=False),
        pa.field("relationship_type", pa.string(), nullable=False),
        pa.field("compatibility", pa.string(), nullable=False),
        pa.field("evidence_ids", pa.string(), nullable=False),
    ]
)

EVENTS_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("capability_id", pa.string(), nullable=False),
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("source_context", pa.string(), nullable=False),
        pa.field("occurred_at", pa.string(), nullable=False),
        pa.field("previous_state", pa.string(), nullable=True),
        pa.field("resulting_state", pa.string(), nullable=False),
        pa.field("reason", pa.string(), nullable=False),
    ]
)

METADATA_SCHEMA = pa.schema(
    [
        pa.field("key", pa.string(), nullable=False),
        pa.field("value", pa.string(), nullable=False),
    ]
)

REQUIREMENT_OBSERVATIONS_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("requirement", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("observed_at", pa.string(), nullable=False),
        pa.field("linked_capability_id", pa.string(), nullable=True),
    ]
)

REQUIREMENT_EVENTS_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("observation_id", pa.string(), nullable=False),
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("occurred_at", pa.string(), nullable=False),
        pa.field("capability_id", pa.string(), nullable=True),
        pa.field("reason", pa.string(), nullable=False),
    ]
)


TABLE_SCHEMAS: dict[str, pa.Schema] = {
    "capabilities": CAPABILITIES_SCHEMA,
    "evidence": EVIDENCE_SCHEMA,
    "relationships": RELATIONSHIPS_SCHEMA,
    "events": EVENTS_SCHEMA,
    "metadata": METADATA_SCHEMA,
    "requirement_observations": REQUIREMENT_OBSERVATIONS_SCHEMA,
    "requirement_events": REQUIREMENT_EVENTS_SCHEMA,
}
