"""LanceDB-backed structured storage for capability memory domain records."""

from __future__ import annotations

import atexit
import hashlib
import json
import math
import os
import shutil
import stat
import threading
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, TYPE_CHECKING
from uuid import uuid4

import lancedb
import lancedb.background_loop as lancedb_background
import pyarrow as pa
import pyarrow.ipc as ipc
from filelock import FileLock
from lancedb.index import FTS
from lancedb.rerankers import RRFReranker
from lancedb.table import LanceTable

from supermind_memory.redaction import (
    redact_capability,
    redact_event,
    redact_evidence,
    redact_identifier,
    redact_requirement,
    redact_requirement_observation,
    redact_requirement_event,
    redact_relationship,
)
from supermind_memory.schema import (
    AUTHORITATIVE_SCHEMA_VERSION_KEY,
    DEMAND_TABLE_NAMES,
    EMBEDDING_DIMENSION,
    LEGACY_TABLE_NAMES,
    SCHEMA_VERSION,
    TABLE_SCHEMAS,
)
from supermind_memory.taxonomy import validate_category_path
from supermind_memory.types import (
    ArtifactType,
    Capability,
    Event,
    Evidence,
    Lifecycle,
    Relationship,
    RequirementEvent,
    RequirementObservation,
    RequirementProfile,
)

if TYPE_CHECKING:
    from supermind_memory.projection import AuthoritySnapshot


_REPOSITORY_REGISTRY: set["CapabilityRepository"] = set()
_REPOSITORY_REGISTRY_LOCK = threading.RLock()
_EVIDENCE_ORDER_KEY = "evidence-commit-order-v1"
_RUNTIME_SHUT_DOWN = False


def _register_repository(repository: "CapabilityRepository") -> None:
    with _REPOSITORY_REGISTRY_LOCK:
        _REPOSITORY_REGISTRY.add(repository)


def repository_runtime_state() -> tuple[int, int, bool]:
    """Expose owned resource counts for lifecycle diagnostics."""
    with _REPOSITORY_REGISTRY_LOCK:
        return (
            len(_REPOSITORY_REGISTRY),
            sum(len(repository._tables) for repository in _REPOSITORY_REGISTRY),
            _RUNTIME_SHUT_DOWN,
        )


def shutdown_repository_runtime() -> None:
    """Close owned connections before stopping LanceDB's process-global loop."""
    global _RUNTIME_SHUT_DOWN
    if _RUNTIME_SHUT_DOWN:
        return
    with _REPOSITORY_REGISTRY_LOCK:
        repositories = tuple(_REPOSITORY_REGISTRY)
    for repository in reversed(repositories):
        repository.close()
    with _REPOSITORY_REGISTRY_LOCK:
        if _REPOSITORY_REGISTRY:
            raise RuntimeError("repository runtime still owns open connections")
    executor = getattr(lancedb_background, "_EMBEDDING_EXECUTOR", None)
    if executor is not None:
        executor.shutdown(wait=True)
    loop_owner = lancedb_background.LOOP
    if loop_owner.thread.is_alive():
        loop_owner.loop.call_soon_threadsafe(loop_owner.loop.stop)
        loop_owner.thread.join()
    if not loop_owner.loop.is_closed():
        loop_owner.loop.close()
    _RUNTIME_SHUT_DOWN = True


def _clear_repository_registry_after_fork() -> None:
    global _RUNTIME_SHUT_DOWN
    with _REPOSITORY_REGISTRY_LOCK:
        _REPOSITORY_REGISTRY.clear()
        _RUNTIME_SHUT_DOWN = False


atexit.register(shutdown_repository_runtime)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_clear_repository_registry_after_fork)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_tuple(value: str) -> tuple[str, ...]:
    return tuple(json.loads(value))


def _strict_authority_json(value: object, expected_type: type | None, label: str) -> object:
    """Decode canonical legacy JSON without accepting lossy normalization."""
    if type(value) is not str:
        raise RuntimeError(f"authoritative_store_corrupt: {label} is not JSON text")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON member {key}")
            result[key] = item
        return result

    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-finite JSON value {constant}")

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as error:
        raise RuntimeError(
            f"authoritative_store_corrupt: {label} contains invalid JSON: {error}"
        ) from error
    if expected_type is not None and type(decoded) is not expected_type:
        raise RuntimeError(
            f"authoritative_store_corrupt: {label} must encode a {expected_type.__name__}"
        )
    if _canonical_json(decoded) != value:
        raise RuntimeError(
            f"authoritative_store_corrupt: {label} is not canonical lossless JSON"
        )
    return decoded


def validate_evidence(evidence: Evidence) -> None:
    """Validate persisted evidence economics at every authoritative boundary."""
    if not math.isfinite(evidence.confidence) or not 0.0 <= evidence.confidence <= 1.0:
        raise ValueError("evidence confidence must be finite and between 0 and 1")
    values = (evidence.integration_effort, evidence.benefit, evidence.failure_risk)
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("evidence economics must be finite non-negative values")
    if evidence.metric_value is not None and not math.isfinite(evidence.metric_value):
        raise ValueError("evidence metric value must be finite")


@dataclass(frozen=True)
class SearchHealthToken:
    generation: str
    authority_digest: str
    authority_count: int


class CapabilityRepository:
    """Owns conversion between capability domain records and LanceDB tables."""

    _OPTIMIZE_EVERY = 20

    def __init__(self, database: Any, database_path: Path, writer_lock_path: Path) -> None:
        self._database = database
        self._database_path = database_path
        self._requested_database_path = database_path
        self._writer_lock_path = writer_lock_path
        self._maintenance_state_path = database_path.parent / "runtime" / "dml-maintenance.json"
        self._migration_journal_path = database_path.parent / "runtime" / "migration-journal.json"
        self._transaction_journal_path = database_path.parent / "runtime" / "transaction-journal.json"
        self._generation_root = database_path.parent
        self._active_generation_required = False
        self._authority_mode: str | None = None
        self._expected_authority_digest: str | None = None
        self._connection_lock = threading.RLock()
        self._tables: dict[str, LanceTable] = {}
        self._table_markers: dict[str, tuple[int, int]] = {}
        self._generation_readers: dict[str, CapabilityRepository] = {}

    @classmethod
    def open(
        cls,
        database_path: Path,
        writer_lock_path: Path | None = None,
    ) -> "CapabilityRepository":
        requested_path = database_path.expanduser().absolute()
        lock_path = (
            writer_lock_path.expanduser().absolute()
            if writer_lock_path is not None
            else requested_path.parent / "locks" / "writer.lock"
        )
        if _RUNTIME_SHUT_DOWN:
            raise RuntimeError("repository runtime has been shut down")
        _require_recovery_paths(requested_path, lock_path)
        resolved_path = requested_path.resolve()
        repository = cls(lancedb.connect(str(resolved_path)), resolved_path, lock_path)
        repository._requested_database_path = requested_path
        _register_repository(repository)
        repository._recover_journals_on_open()
        return repository

    def __enter__(self) -> "CapabilityRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Release cached tables before closing the native connection."""
        with self._connection_lock:
            readers = tuple(self._generation_readers.values())
            for reader in readers:
                reader.close()
            self._generation_readers.clear()
            for table in self._tables.values():
                native_table = table._table
                native_table.close()
                if native_table.is_open():
                    raise RuntimeError("native LanceDB table remained open after close")
            self._tables.clear()
            self._table_markers.clear()
            self._close_connection_unlocked()

    def _close_connection_unlocked(self) -> None:
        connection = getattr(self._database, "_conn", None)
        if connection is not None and getattr(connection, "is_open", lambda: False)():
            connection.close()
            if connection.is_open():
                raise RuntimeError("native LanceDB connection remained open after close")
        with _REPOSITORY_REGISTRY_LOCK:
            _REPOSITORY_REGISTRY.discard(self)

    def initialize(self) -> None:
        _require_recovery_paths(self._requested_database_path, self._writer_lock_path)
        with self._writer_lock():
            self._recover_transaction_unlocked()
            self._initialize_unlocked()

    @contextmanager
    def atomic_write(self) -> Iterator[None]:
        """Commit related authoritative-table writes or restore their exact snapshot."""
        with self._writer_lock():
            self._recover_transaction_unlocked()
            staging = self._prepare_transaction_unlocked()
            try:
                yield
                self._commit_transaction_unlocked(staging)
            except BaseException:
                self._rollback_transaction_unlocked(staging)
                raise

    def _initialize_unlocked(self) -> None:
        self._migrate_schemas_unlocked()

    def _authoritative_schema_state_unlocked(self) -> str:
        existing = set(self._database.list_tables().tables)
        if not existing and (self._generation_root / "active-generation.json").exists():
            raise RuntimeError("authoritative_store_corrupt: activated store has no tables")
        for table_name in existing & TABLE_SCHEMAS.keys():
            self._validate_original_schema(table_name, self._table(table_name).schema)
        metadata = self._rows("metadata") if "metadata" in existing else []
        return self._authoritative_schema_state(existing, metadata)

    @staticmethod
    def _validate_original_schema(table_name: str, actual: pa.Schema) -> None:
        """Prove authoritative fields exist before any row normalization."""
        for expected in TABLE_SCHEMAS[table_name]:
            # These two capability fields are rebuildable index data; legacy
            # migrations already regenerate them from authoritative records.
            if table_name == "capabilities" and expected.name in {"vector", "search_text"}:
                continue
            indices = actual.get_all_field_indices(expected.name)
            if not indices and expected.nullable:
                continue
            if len(indices) != 1 or actual.field(indices[0]).type != expected.type:
                raise RuntimeError(
                    f"authoritative_store_corrupt: {table_name} has an invalid "
                    f"original field {expected.name}"
                )

    @staticmethod
    def _authoritative_schema_state(
        existing: set[str], metadata: list[dict[str, Any]],
    ) -> str:
        markers = [
            row["value"] for row in metadata
            if row["key"] == AUTHORITATIVE_SCHEMA_VERSION_KEY
        ]
        if markers:
            try:
                version = json.loads(markers[0])
            except (TypeError, ValueError) as error:
                raise RuntimeError("authoritative_store_corrupt: invalid schema marker") from error
            if len(markers) != 1 or type(version) is not int or version != SCHEMA_VERSION:
                raise RuntimeError("authoritative_store_corrupt: invalid schema marker")
            if existing != set(TABLE_SCHEMAS):
                raise RuntimeError("authoritative_store_corrupt: v2 authoritative tables are incomplete")
            return "v2"
        if not existing:
            return "empty"
        if existing == LEGACY_TABLE_NAMES:
            return "v1"
        if existing == set(TABLE_SCHEMAS):
            return "unmarked_v2"
        raise RuntimeError("authoritative_store_corrupt: unproven authoritative table set")

    def _migrate_schemas_unlocked(self) -> None:
        """Replace incompatible tables while preserving their authoritative rows."""
        _require_recovery_paths(self._requested_database_path, self._writer_lock_path)
        self._recover_incomplete_migration_unlocked()
        state = self._authoritative_schema_state_unlocked()
        existing = set(self._database.list_tables().tables)
        migrations: list[
            tuple[str, Any, Any, list[dict[str, Any]], list[dict[str, object]]]
        ] = []
        for table_name, expected_schema in TABLE_SCHEMAS.items():
            if table_name not in existing:
                migrations.append((table_name, expected_schema, None, [], []))
                continue
            table = self._table(table_name)
            if table.schema.equals(expected_schema, check_metadata=False):
                continue
            original_rows = table.to_arrow().to_pylist()
            migrated_rows = self._rows_for_schema(table_name, original_rows)
            migrations.append(
                (
                    table_name,
                    expected_schema,
                    table.schema,
                    original_rows,
                    migrated_rows,
                )
            )
        if not migrations and state == "v2":
            self._validate_requirement_history_unlocked()
            return

        expected_rows = {
            table_name: self._rows(table_name) if table_name in existing else []
            for table_name in TABLE_SCHEMAS
        }
        for table_name, _, _, _, migrated_rows in migrations:
            expected_rows[table_name] = migrated_rows
        self._validate_requirement_history(
            expected_rows["requirement_observations"], expected_rows["requirement_events"],
        )
        self._write_migration_journal_unlocked()
        try:
            for table_name, expected, original_schema, original_rows, migrated_rows in migrations:
                self._replace_table_unlocked(
                    table_name,
                    expected,
                    original_rows,
                    migrated_rows,
                )
            self._validate_all_migrated_tables_unlocked(expected_rows)
            self._validate_requirement_history_unlocked()
            if state != "v2":
                self._set_metadata_unlocked(AUTHORITATIVE_SCHEMA_VERSION_KEY, SCHEMA_VERSION)
            if self._authoritative_schema_state_unlocked() != "v2":
                raise RuntimeError("authoritative_store_corrupt: schema marker was not committed")
            _fsync_tree(self._database_path)
            self._clear_migration_journal_unlocked()
        except BaseException:
            self._recover_incomplete_migration_unlocked()
            raise

    def _validate_requirement_history_unlocked(self) -> None:
        self._validate_requirement_history(
            self._rows("requirement_observations"), self._rows("requirement_events"),
        )

    @classmethod
    def _validate_requirement_history(
        cls,
        observation_rows: list[dict[str, Any]],
        event_rows: list[dict[str, Any]],
    ) -> None:
        observations = tuple(cls._requirement_observation_from_row(row) for row in observation_rows)
        events_by_observation: dict[str, list[RequirementEvent]] = {item.id: [] for item in observations}
        for row in event_rows:
            event = cls._requirement_event_from_row(row)
            if event.observation_id not in events_by_observation:
                raise RuntimeError("authoritative_store_corrupt: requirement event references an unknown observation")
            events_by_observation[event.observation_id].append(event)
        for observation in observations:
            if observation.status not in {"unmet", "linked"}:
                raise RuntimeError("authoritative_store_corrupt: requirement observation has an invalid status")
            if (observation.status == "linked") is (observation.linked_capability_id is None):
                raise RuntimeError("authoritative_store_corrupt: requirement observation has an invalid link state")
            events = events_by_observation[observation.id]
            if not any(event.event_type == "unmet_observed" for event in events):
                raise RuntimeError("authoritative_store_corrupt: requirement observation has no creation event")
            if observation.status == "linked" and not any(
                event.event_type == "implementation_linked"
                and event.capability_id == observation.linked_capability_id
                for event in events
            ):
                raise RuntimeError("authoritative_store_corrupt: requirement observation has no matching link event")

    def configure_generation_reads(
        self, root: Path, *, required: bool = True,
        authority_mode: str | None = None, expected_authority_digest: str | None = None,
    ) -> None:
        self._generation_root = root.expanduser().absolute()
        self._active_generation_required = required
        self._authority_mode = authority_mode
        self._expected_authority_digest = expected_authority_digest

    def replace_authority(self, snapshot: AuthoritySnapshot, vectors: Mapping[str, Sequence[float]]) -> None:
        """Validate a complete candidate, then activate it under the recovery journal.

        Lock order is writer lock, then connection lock. Readers honoring the
        writer lock see one complete projection; interruption between directory
        renames restores the fsynced seven-table snapshot on the next open.
        """
        from supermind_memory.projection import MATERIALIZED_DIGEST_KEY, materialized_digest, snapshot_rows

        rows = snapshot_rows(snapshot, vectors)
        digest = materialized_digest(rows)
        rows["metadata"].append({"key": MATERIALIZED_DIGEST_KEY, "value": _canonical_json(digest)})
        with self._writer_lock():
            self._recover_transaction_unlocked()
            # Explicit event replay replaces a disposable projection. Recover
            # pending filesystem work, but never require the old rows to be a
            # valid authority source. Ordinary initialize/migration stays strict.
            self._recover_incomplete_migration_unlocked()
            runtime = self._transaction_journal_path.parent
            runtime.mkdir(parents=True, exist_ok=True)
            candidate_root = Path(tempfile.mkdtemp(prefix="projection-", dir=runtime))
            staging = None
            try:
                staging = self._prepare_transaction_unlocked(projection_candidate=candidate_root)
                with CapabilityRepository.open(candidate_root / "database") as candidate:
                    for name, schema in TABLE_SCHEMAS.items():
                        candidate._overwrite_table_unlocked(name, schema, rows[name])
                    for name, schema in TABLE_SCHEMAS.items():
                        if not candidate._table(name).schema.equals(schema, check_metadata=False):
                            raise RuntimeError(f"authoritative_store_corrupt: projection schema is invalid for {name}")
                    candidate._validate_requirement_history_unlocked()
                    if candidate._authoritative_schema_state_unlocked() != "v2":
                        raise RuntimeError("authoritative_store_corrupt: projection schema is invalid")
                    actual = {name: candidate._rows(name) for name in TABLE_SCHEMAS}
                    if materialized_digest(actual) != digest:
                        raise RuntimeError("projection_mismatch: candidate changed during serialization")
                _fsync_tree(candidate_root)
                with self._connection_lock:
                    self.close()
                    os.replace(self._database_path, candidate_root / "previous")
                    os.replace(candidate_root / "database", self._database_path)
                    _fsync_directory(self._database_path.parent)
                    _fsync_directory(candidate_root)
                    self._database = lancedb.connect(str(self._database_path))
                    _register_repository(self)
                    self._commit_transaction_unlocked(staging)
            except BaseException:
                if staging is not None:
                    # Respect an already durable commit marker if cleanup failed.
                    self._recover_transaction_unlocked()
                raise
            finally:
                if not self._transaction_journal_path.exists() and candidate_root.exists():
                    shutil.rmtree(candidate_root)

    def authority_snapshot(self) -> AuthoritySnapshot:
        """Return one validated, stable-ID snapshot of legacy schema-v2 authority."""
        from supermind_memory.projection import AuthoritySnapshot, _RESERVED_METADATA

        with self._writer_lock():
            try:
                if self._authoritative_schema_state_unlocked() != "v2":
                    raise RuntimeError(
                        "authoritative_store_corrupt: migration source is not schema v2"
                    )
                for name, schema in TABLE_SCHEMAS.items():
                    if not self._table(name).schema.equals(schema, check_metadata=False):
                        raise RuntimeError(
                            f"authoritative_store_corrupt: {name} schema is not canonical v2"
                        )
                raw_rows = {name: self._rows(name) for name in TABLE_SCHEMAS}
                self._validate_requirement_history(
                    raw_rows["requirement_observations"],
                    raw_rows["requirement_events"],
                )
                self._read_evidence_order_unlocked(require_complete=True)
                self._validate_authority_json_unlocked(raw_rows, _RESERVED_METADATA)

                collections = {
                    "capabilities": tuple(
                        sorted(
                            (self._capability_from_row(row) for row in raw_rows["capabilities"]),
                            key=lambda item: item.id,
                        )
                    ),
                    "evidence": tuple(
                        sorted(
                            (self._evidence_from_row(row) for row in raw_rows["evidence"]),
                            key=lambda item: item.id,
                        )
                    ),
                    "relationships": tuple(
                        sorted(
                            (self._relationship_from_row(row) for row in raw_rows["relationships"]),
                            key=lambda item: item.id,
                        )
                    ),
                    "events": tuple(
                        sorted(
                            (self._event_from_row(row) for row in raw_rows["events"]),
                            key=lambda item: item.id,
                        )
                    ),
                    "requirement_observations": tuple(
                        sorted(
                            (
                                self._requirement_observation_from_row(row)
                                for row in raw_rows["requirement_observations"]
                            ),
                            key=lambda item: item.id,
                        )
                    ),
                    "requirement_events": tuple(
                        sorted(
                            (
                                self._requirement_event_from_row(row)
                                for row in raw_rows["requirement_events"]
                            ),
                            key=lambda item: item.id,
                        )
                    ),
                }
                for name, records in collections.items():
                    identifiers = tuple(item.id for item in records)
                    if len(identifiers) != len(set(identifiers)):
                        raise RuntimeError(
                            f"authoritative_store_corrupt: duplicate IDs in {name}"
                        )
                for evidence in collections["evidence"]:
                    validate_evidence(evidence)

                metadata_rows = raw_rows["metadata"]
                metadata_keys = tuple(str(row["key"]) for row in metadata_rows)
                if len(metadata_keys) != len(set(metadata_keys)):
                    raise RuntimeError(
                        "authoritative_store_corrupt: duplicate metadata keys"
                    )
                metadata = tuple(
                    sorted(
                        (
                            (str(row["key"]), row["value"])
                            for row in metadata_rows
                            if row["key"] not in _RESERVED_METADATA
                        ),
                        key=lambda item: item[0],
                    )
                )
                snapshot = AuthoritySnapshot(**collections, metadata=metadata)
                self._validate_snapshot_rows_lossless_unlocked(
                    snapshot, raw_rows, _RESERVED_METADATA,
                )
                return snapshot
            except RuntimeError:
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"authoritative_store_corrupt: invalid authoritative row: {error}"
                ) from error

    @staticmethod
    def _validate_authority_json_unlocked(
        rows: Mapping[str, list[dict[str, Any]]], reserved_metadata: set[str],
    ) -> None:
        list_fields = {
            "capabilities": (
                "category_path", "facets", "constraints", "stack", "runtime",
                "platform", "dependencies", "compatibility",
            ),
            "relationships": ("compatibility", "evidence_ids"),
        }
        for table, fields_to_check in list_fields.items():
            for row in rows[table]:
                for field_name in fields_to_check:
                    _strict_authority_json(
                        row[field_name], list, f"{table}.{field_name}",
                    )
        tuple_fields = (
            "category_hint", "stack", "constraints", "quality_requirements",
            "runtime", "platform", "license",
        )
        for row in rows["requirement_observations"]:
            requirement = _strict_authority_json(
                row["requirement"], dict, "requirement_observations.requirement",
            )
            assert isinstance(requirement, dict)
            for field_name in tuple_fields:
                if field_name in requirement and type(requirement[field_name]) is not list:
                    raise RuntimeError(
                        "authoritative_store_corrupt: "
                        f"requirement_observations.requirement.{field_name} must encode a list"
                    )
        for row in rows["metadata"]:
            if row["key"] not in reserved_metadata:
                _strict_authority_json(row["value"], None, f"metadata.{row['key']}")

    @staticmethod
    def _validate_snapshot_rows_lossless_unlocked(
        snapshot: AuthoritySnapshot,
        raw_rows: Mapping[str, list[dict[str, Any]]],
        reserved_metadata: set[str],
    ) -> None:
        def capability_row(item: Capability) -> dict[str, object]:
            row = asdict(item)
            for field_name in (
                "category_path", "facets", "constraints", "stack", "runtime",
                "platform", "dependencies", "compatibility",
            ):
                row[field_name] = _canonical_json(row[field_name])
            row["artifact_type"] = item.artifact_type.value
            row["lifecycle"] = item.lifecycle.value
            return row

        def relationship_row(item: Relationship) -> dict[str, object]:
            row = asdict(item)
            row["compatibility"] = _canonical_json(item.compatibility)
            row["evidence_ids"] = _canonical_json(item.evidence_ids)
            return row

        def event_row(item: Event) -> dict[str, object]:
            row = asdict(item)
            row["previous_state"] = (
                item.previous_state.value if item.previous_state is not None else None
            )
            row["resulting_state"] = item.resulting_state.value
            return row

        def observation_row(item: RequirementObservation) -> dict[str, object]:
            return {
                "id": item.id,
                "requirement": _canonical_json(asdict(item.requirement)),
                "status": item.status,
                "observed_at": item.observed_at,
                "linked_capability_id": item.linked_capability_id,
            }

        rebuilt = {
            "capabilities": [capability_row(item) for item in snapshot.capabilities],
            "evidence": [asdict(item) for item in snapshot.evidence],
            "relationships": [relationship_row(item) for item in snapshot.relationships],
            "events": [event_row(item) for item in snapshot.events],
            "requirement_observations": [
                observation_row(item) for item in snapshot.requirement_observations
            ],
            "requirement_events": [
                asdict(item) for item in snapshot.requirement_events
            ],
            "metadata": [
                {"key": key, "value": value} for key, value in snapshot.metadata
            ],
        }
        authoritative = {}
        for table, values in raw_rows.items():
            if table == "capabilities":
                values = [
                    {
                        key: value
                        for key, value in row.items()
                        if key not in {"vector", "search_text"}
                    }
                    for row in values
                ]
            if table == "metadata":
                values = [row for row in values if row["key"] not in reserved_metadata]
            key = "key" if table == "metadata" else "id"
            authoritative[table] = sorted(values, key=lambda row: row[key])
            rebuilt[table] = sorted(rebuilt[table], key=lambda row: row[key])
        if authoritative != rebuilt:
            differing = sorted(
                table for table in authoritative
                if authoritative[table] != rebuilt[table]
            )
            raise RuntimeError(
                "authoritative_store_corrupt: typed snapshot changes raw authority rows: "
                + ", ".join(differing)
            )

    def _check_authority_binding(self, manifest: Mapping[str, object] | None = None) -> None:
        if self._authority_mode != "events-v1":
            return
        from supermind_memory.projection import AUTHORITY_DIGEST_KEY, MATERIALIZED_DIGEST_KEY, materialized_digest

        digest = self._get_metadata_unlocked(AUTHORITY_DIGEST_KEY)
        if not isinstance(digest, str) or len(digest) != 64 or digest != self._expected_authority_digest:
            raise RuntimeError("authority_digest_mismatch: replay and projection authority digests differ")
        actual = materialized_digest({name: self._rows(name) for name in TABLE_SCHEMAS})
        if self._get_metadata_unlocked(MATERIALIZED_DIGEST_KEY) != actual:
            raise RuntimeError("projection_mismatch: materialized authority changed")
        if manifest is not None and (
            manifest.get(AUTHORITY_DIGEST_KEY) != digest or manifest.get("materialized_digest") != actual
        ):
            raise RuntimeError("authority_generation_stale: generation authority binding differs")

    def upsert_capability(self, capability: Capability, vector: Sequence[float]) -> None:
        capability = redact_capability(capability)
        validate_category_path(capability.category_path)
        if len(vector) != EMBEDDING_DIMENSION:
            raise ValueError(f"vector must contain {EMBEDDING_DIMENSION} values")
        with self._writer_lock():
            # Discovery supplies the timestamp for this observation; refreshes
            # may update ``updated_at`` but never rewrite first discovery time.
            existing = self.get_capability(capability.id)
            if existing is not None:
                capability = replace(capability, created_at=existing.created_at)
            self._table("capabilities").merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(
                [self._capability_row(capability, vector)]
            )
            self._record_dml_unlocked()

    def get_capability(self, capability_id: str) -> Capability | None:
        capability_id = redact_identifier(capability_id)
        for row in self._rows("capabilities"):
            if row["id"] == capability_id:
                return self._capability_from_row(row)
        return None

    def list_capabilities(self) -> tuple[Capability, ...]:
        return tuple(sorted((self._capability_from_row(row) for row in self._rows("capabilities")), key=lambda capability: capability.id))

    def append_evidence(self, evidence: Evidence) -> None:
        evidence = redact_evidence(evidence)
        validate_evidence(evidence)
        with self.atomic_write():
            self._ordered_evidence_unlocked(evidence.capability_id, (evidence,))
            self._append_once("evidence", self._evidence_row(evidence))
            self._record_dml_unlocked()

    def append_relationship(self, relationship: Relationship) -> None:
        with self._writer_lock():
            self._append_once("relationships", self._relationship_row(relationship))
            self._record_dml_unlocked()

    def append_event(self, event: Event) -> None:
        with self._writer_lock():
            self._append_once("events", self._event_row(event))
            self._record_dml_unlocked()

    def list_evidence(self, capability_id: str) -> tuple[Evidence, ...]:
        capability_id = redact_identifier(capability_id)
        records = tuple(
            self._evidence_from_row(row)
            for row in self._rows("evidence")
            if row["capability_id"] == capability_id
        )
        order = self._read_evidence_order_unlocked(require_complete=False)
        return tuple(
            sorted(
                records,
                key=lambda evidence: (order.get(evidence.id, 2**63), evidence.id),
            )
        )

    def _ordered_evidence_unlocked(
        self,
        capability_id: str,
        additional: Sequence[Evidence] = (),
    ) -> tuple[Evidence, ...]:
        state = self._ensure_evidence_order_unlocked(additional)
        by_id = {
            evidence.id: evidence
            for evidence in (
                self._evidence_from_row(row)
                for row in self._rows("evidence")
                if row["capability_id"] == capability_id
            )
        }
        for evidence in additional:
            if evidence.capability_id == capability_id:
                by_id.setdefault(evidence.id, evidence)
        sequences = state["sequences"]
        return tuple(sorted(by_id.values(), key=lambda evidence: sequences[evidence.id]))

    def _ensure_evidence_order_unlocked(
        self,
        additional: Sequence[Evidence] = (),
    ) -> dict[str, Any]:
        current = self._get_metadata_unlocked(_EVIDENCE_ORDER_KEY)
        if current is None:
            state: dict[str, Any] = {"next": 1, "sequences": {}}
        else:
            state = self._validated_evidence_order_state(current)
        sequences = dict(state["sequences"])
        next_sequence = int(state["next"])
        identifiers = [str(row["id"]) for row in self._rows("evidence")]
        identifiers.extend(evidence.id for evidence in additional)
        changed = current is None
        for identifier in identifiers:
            if identifier in sequences:
                continue
            sequences[identifier] = next_sequence
            next_sequence += 1
            changed = True
        updated = {"next": next_sequence, "sequences": sequences}
        if changed:
            self._set_metadata_unlocked(_EVIDENCE_ORDER_KEY, updated)
        return updated

    def _read_evidence_order_unlocked(self, *, require_complete: bool) -> dict[str, int]:
        current = self._get_metadata_unlocked(_EVIDENCE_ORDER_KEY)
        if current is None:
            if require_complete and self._rows("evidence"):
                raise ValueError("evidence commit order is missing")
            return {}
        state = self._validated_evidence_order_state(current)
        sequences = state["sequences"]
        if require_complete:
            missing = {
                str(row["id"])
                for row in self._rows("evidence")
                if str(row["id"]) not in sequences
            }
            if missing:
                raise ValueError(
                    "evidence commit order is incomplete: " + ", ".join(sorted(missing))
                )
        return sequences

    @staticmethod
    def _validated_evidence_order_state(value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("sequences"), dict):
            raise ValueError("evidence commit order is invalid")
        next_sequence = value.get("next")
        raw_sequences = value["sequences"]
        if not isinstance(next_sequence, int) or isinstance(next_sequence, bool):
            raise ValueError("evidence commit sequence is invalid")
        if any(
            not isinstance(identifier, str)
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            for identifier, sequence in raw_sequences.items()
        ):
            raise ValueError("evidence commit sequence is invalid")
        sequences = {str(key): int(sequence) for key, sequence in raw_sequences.items()}
        if len(set(sequences.values())) != len(sequences):
            raise ValueError("evidence commit sequences are not unique")
        if next_sequence <= max(sequences.values(), default=0):
            raise ValueError("evidence next commit sequence is stale")
        return {"next": next_sequence, "sequences": sequences}

    def list_relationships(self, capability_id: str) -> tuple[Relationship, ...]:
        capability_id = redact_identifier(capability_id)
        records = (
            self._relationship_from_row(row)
            for row in self._rows("relationships")
            if row["source_id"] == capability_id or row["target_id"] == capability_id
        )
        return tuple(sorted(records, key=lambda relationship: relationship.id))

    def list_events(self, capability_id: str) -> tuple[Event, ...]:
        capability_id = redact_identifier(capability_id)
        records = (self._event_from_row(row) for row in self._rows("events") if row["capability_id"] == capability_id)
        return tuple(sorted(records, key=lambda event: event.id))

    def list_requirement_observations(self) -> tuple[RequirementObservation, ...]:
        return tuple(
            sorted(
                (
                    self._requirement_observation_from_row(row)
                    for row in self._rows("requirement_observations")
                ),
                key=lambda item: (item.observed_at, item.id),
            )
        )

    def get_requirement_observation(
        self,
        observation_id: str,
    ) -> RequirementObservation | None:
        return next(
            (
                self._requirement_observation_from_row(row)
                for row in self._rows("requirement_observations")
                if row["id"] == observation_id
            ),
            None,
        )

    def list_requirement_events(
        self,
        observation_id: str,
    ) -> tuple[RequirementEvent, ...]:
        return tuple(
            sorted(
                (
                    self._requirement_event_from_row(row)
                    for row in self._rows("requirement_events")
                    if row["observation_id"] == observation_id
                ),
                key=lambda item: (item.occurred_at, item.id),
            )
        )

    def set_metadata(self, key: str, value: object) -> None:
        with self._writer_lock():
            self._set_metadata_unlocked(key, value)
            self._record_dml_unlocked()

    def get_metadata(self, key: str) -> object | None:
        """Return one canonical metadata value without exposing storage rows."""
        return self._get_metadata_unlocked(key)

    def update_metadata(self, key: str, update: Callable[[object | None], object]) -> object:
        """Atomically read, merge, and write one metadata value under the writer lock."""
        with self._writer_lock():
            current = self._get_metadata_unlocked(key)
            value = update(current)
            if value == current:
                return value
            self._set_metadata_unlocked(key, value)
            self._record_dml_unlocked()
            return value

    def hybrid_search(
        self,
        query_text: str,
        vector: Sequence[float],
        where: str,
        limit: int,
        generation: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Run both retrieval paths and reciprocal-rank fusion over the same prefilter."""
        if len(vector) != EMBEDDING_DIMENSION:
            raise ValueError(f"query vector must contain {EMBEDDING_DIMENSION} values")
        generation = generation if generation is not None else self.active_generation()
        if generation is None:
            table = self._table("capabilities")
            with self._writer_lock():
                table.create_index("search_text", config=FTS(), replace=True)
        else:
            if not generation.startswith("generation-") or Path(generation).name != generation:
                raise RuntimeError("search generation is invalid")
            table = self._generation_reader(generation)._table("capabilities")
        vector_rows = (
            table.search(list(vector), query_type="vector", vector_column_name="vector")
            .where(where, prefilter=True)
            .limit(limit)
            .to_list()
        )
        lexical_rows = (
            table.search(query_text, query_type="fts", fts_columns="search_text")
            .where(where, prefilter=True)
            .limit(limit)
            .to_list()
        )
        hybrid_rows = (
            table.search(query_type="hybrid", vector_column_name="vector", fts_columns="search_text")
            .vector(list(vector))
            .text(query_text)
            .where(where, prefilter=True)
            .rerank(RRFReranker())
            .limit(limit)
            .to_list()
        )
        return vector_rows, lexical_rows, hybrid_rows

    def search_snapshot(self) -> tuple[str | None, tuple[Capability, ...]]:
        generation = self.active_generation()
        if generation is None:
            return None, self.list_capabilities()
        return generation, self._generation_reader(generation).list_capabilities()

    def active_generation(self) -> str | None:
        pointer_path = self._generation_root / "active-generation.json"
        self._validate_managed_paths_for_read()
        try:
            pointer = _read_json_no_follow(pointer_path)
        except FileNotFoundError:
            if self._active_generation_required:
                raise RuntimeError("capability search has no active healthy generation")
            return None
        generation = pointer.get("active_generation")
        if (
            not isinstance(generation, str)
            or not generation.startswith("generation-")
            or Path(generation).name != generation
        ):
            raise RuntimeError("active generation pointer is invalid")
        generation_dir = self._managed_generations_root() / generation
        _require_real_tree(generation_dir, self._generation_root)
        manifest = _read_json_no_follow(generation_dir / "manifest.json")
        if manifest.get("generation") != generation or not all(
            manifest.get("checks", {}).get(name) is True
            for name in ("runtime", "model", "schema", "source", "vector", "fts")
        ):
            raise RuntimeError("active generation manifest is invalid")
        if not (generation_dir / "database").is_dir():
            raise RuntimeError("active generation database is missing")
        self._check_authority_binding(manifest)
        if self._active_generation_required:
            source_digest, source_count = self._structured_source_fingerprint()
            model_lock_path = Path(__file__).resolve().parents[2] / "model.lock.json"
            if (
                manifest.get("schema_version") != SCHEMA_VERSION
                or manifest.get("vector_mode") != "exact"
                or manifest.get("model_lock_digest")
                != hashlib.sha256(model_lock_path.read_bytes()).hexdigest()
                or manifest.get("source_digest") != source_digest
                or manifest.get("source_count") != source_count
            ):
                raise RuntimeError("active generation is stale for authoritative records")
            generation_repository = self._generation_reader(generation)
            if manifest.get("artifact_digest") != _artifact_digest(generation_repository):
                raise RuntimeError("active generation artifact digest does not match")
        return generation

    def _generation_reader(self, generation: str) -> "CapabilityRepository":
        with self._connection_lock:
            reader = self._generation_readers.get(generation)
            if reader is None:
                reader = CapabilityRepository.open(
                    self._managed_generations_root() / generation / "database",
                    writer_lock_path=self._writer_lock_path,
                )
                self._generation_readers[generation] = reader
            return reader

    def search_health_token(self, generation: str | None) -> SearchHealthToken:
        """Bind one checked generation to all authoritative rows used by search."""
        if generation is None or self.active_generation() != generation:
            raise RuntimeError("checked search generation is no longer active")
        digest, count = self._authoritative_fingerprint()
        return SearchHealthToken(generation, digest, count)

    def search_health_token_matches(self, token: SearchHealthToken) -> bool:
        try:
            if self.active_generation() != token.generation:
                return False
            digest, count = self._authoritative_fingerprint()
            return digest == token.authority_digest and count == token.authority_count
        except Exception:
            return False

    def _validate_managed_paths_for_read(self) -> None:
        if not self._active_generation_required:
            return
        root = self._generation_root.absolute()
        uses_derived_layout = self._database_path == (root / "derived" / "database").resolve()
        expected_database = root / "derived" / "database" if uses_derived_layout else root / "database"
        runtime = root / "derived" / "runtime" if uses_derived_layout else root / "runtime"
        generations = root / "derived" / "generations" if uses_derived_layout else root / "generations"
        for path in (
            root,
            expected_database,
            runtime,
            root / "model-cache",
            root / "locks",
            generations,
            root / "active-generation.json",
        ):
            if path.is_symlink():
                raise RuntimeError(f"managed repository path is a symlink: {path}")
        if root.resolve() != root or self._database_path != expected_database.resolve():
            raise RuntimeError("managed repository database escapes the memory root")
        if expected_database.exists():
            _require_real_tree(expected_database, root)

    def _managed_generations_root(self) -> Path:
        root = self._generation_root
        if self._database_path == (root / "derived" / "database").resolve():
            return root / "derived" / "generations"
        return root / "generations"

    def _structured_source_fingerprint(self) -> tuple[str, int]:
        rows = []
        for capability in self.list_capabilities():
            row = self._capability_row(capability, [0.0] * EMBEDDING_DIMENSION)
            row.pop("vector")
            row.pop("search_text")
            rows.append(row)
        canonical = json.dumps(
            sorted(rows, key=lambda row: row["id"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest(), len(rows)

    def _authoritative_fingerprint(self) -> tuple[str, int]:
        tables: dict[str, list[dict[str, object]]] = {}
        for table_name in TABLE_SCHEMAS:
            rows = self._rows(table_name)
            if table_name == "capabilities":
                rows = [
                    {
                        key: value
                        for key, value in row.items()
                        if key not in {"vector", "search_text"}
                    }
                    for row in rows
                ]
            key = "key" if table_name == "metadata" else "id"
            tables[table_name] = sorted(rows, key=lambda row: str(row[key]))
        canonical = json.dumps(
            tables,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        count = sum(len(rows) for rows in tables.values())
        return hashlib.sha256(canonical).hexdigest(), count

    def _table(self, name: str) -> LanceTable:
        with self._connection_lock:
            table = self._tables.get(name)
            marker = self._table_marker(name)
            if table is None or self._table_markers.get(name) != marker:
                self._close_cached_table_unlocked(name)
                table = self._database.open_table(name)
                self._tables[name] = table
                self._table_markers[name] = marker
            else:
                try:
                    table.checkout_latest()
                except Exception:
                    self._close_cached_table_unlocked(name)
                    table = self._database.open_table(name)
                    self._tables[name] = table
                    self._table_markers[name] = marker
            return table

    def _close_cached_table_unlocked(self, name: str) -> None:
        table = self._tables.get(name)
        if table is None:
            return
        native_table = table._table
        native_table.close()
        if native_table.is_open():
            raise RuntimeError("native LanceDB table remained open after close")
        self._tables.pop(name)
        self._table_markers.pop(name, None)

    def _table_marker(self, name: str) -> tuple[int, int]:
        metadata = (self._database_path / f"{name}.lance").stat()
        return metadata.st_dev, metadata.st_ino

    def _rows(self, table_name: str) -> list[dict[str, Any]]:
        return self._table(table_name).to_arrow().to_pylist()

    def _append_once(self, table_name: str, row: dict[str, object]) -> None:
        self._table(table_name).merge_insert("id").when_not_matched_insert_all().execute([row])

    def _set_metadata_unlocked(self, key: str, value: object) -> None:
        self._table("metadata").merge_insert("key").when_matched_update_all().when_not_matched_insert_all().execute(
            [{"key": key, "value": _canonical_json(value)}]
        )

    def _get_metadata_unlocked(self, key: str) -> object | None:
        for row in self._rows("metadata"):
            if row["key"] == key:
                return json.loads(row["value"])
        return None

    def _rows_for_schema(
        self,
        table_name: str,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, object]]:
        if table_name == "capabilities":
            migrated = []
            for row in rows:
                capability = self._capability_from_row(row)
                vector = row.get("vector", [0.0] * EMBEDDING_DIMENSION)
                if len(vector) != EMBEDDING_DIMENSION:
                    vector = [0.0] * EMBEDDING_DIMENSION
                migrated.append(self._capability_row(capability, vector))
            return migrated

        schema = TABLE_SCHEMAS[table_name]
        migrated = []
        for row in rows:
            normalized: dict[str, object] = {}
            for field in schema:
                if field.name in row:
                    normalized[field.name] = row[field.name]
                elif field.nullable:
                    normalized[field.name] = None
                else:
                    raise ValueError(
                        f"cannot migrate {table_name}: required field {field.name} is missing"
                    )
            migrated.append(normalized)
        return migrated

    def _replace_table_unlocked(
        self,
        table_name: str,
        schema: Any,
        original_rows: list[dict[str, Any]],
        migrated_rows: list[dict[str, object]],
    ) -> None:
        key = "key" if table_name == "metadata" else "id"
        original_keys = sorted(row[key] for row in original_rows)
        self._overwrite_table_unlocked(table_name, schema, migrated_rows)
        replacement = self._table(table_name)
        replacement_rows = replacement.to_arrow().to_pylist()
        if not replacement.schema.equals(schema, check_metadata=False):
            raise RuntimeError(f"{table_name} migration produced the wrong schema")
        if sorted(row[key] for row in replacement_rows) != original_keys:
            raise RuntimeError(f"{table_name} migration did not preserve record identifiers")

    def _overwrite_table_unlocked(
        self,
        table_name: str,
        schema: Any,
        rows: list[dict[str, object]],
    ) -> None:
        data = pa.Table.from_pylist(rows, schema=schema)
        self._close_cached_table_unlocked(table_name)
        self._database.create_table(
            table_name,
            data=data,
            mode="overwrite",
        )

    def _write_migration_journal_unlocked(self) -> None:
        runtime = self._migration_journal_path.parent
        runtime.mkdir(parents=True, exist_ok=True)
        staging = runtime / f"migration-{uuid4().hex}"
        staging.mkdir()
        tables: dict[str, dict[str, object] | None] = {}
        try:
            existing = set(self._database.list_tables().tables)
            for table_name in TABLE_SCHEMAS:
                if table_name not in existing:
                    tables[table_name] = None
                    continue
                snapshot_path = staging / f"{table_name}.arrow"
                arrow_table = self._table(table_name).to_arrow()
                with pa.OSFile(str(snapshot_path), "wb") as sink:
                    with ipc.new_file(sink, arrow_table.schema) as writer:
                        writer.write_table(arrow_table)
                _fsync_file(snapshot_path)
                tables[table_name] = {
                    "file": snapshot_path.name,
                    "rows": len(arrow_table),
                    "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
                }
            _fsync_directory(staging)
            payload = {
                "format": 2,
                "staging": staging.name,
                "tables": tables,
            }
            temporary = runtime / f".{self._migration_journal_path.name}-{uuid4().hex}.tmp"
            _write_json_fsynced(temporary, payload)
            os.replace(temporary, self._migration_journal_path)
            _fsync_directory(runtime)
        except BaseException:
            if not self._migration_journal_path.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    def _recover_incomplete_migration_unlocked(self) -> None:
        if self._migration_journal_path.exists() or self._migration_journal_path.is_symlink():
            _require_recovery_paths(self._requested_database_path, self._writer_lock_path)
        try:
            payload = _read_json_no_follow(self._migration_journal_path)
        except FileNotFoundError:
            return
        if payload.get("format") not in {1, 2} or set(payload.get("tables", {})) != set(TABLE_SCHEMAS):
            raise RuntimeError("migration journal is invalid")
        staging_name = payload.get("staging")
        if (
            not isinstance(staging_name, str)
            or not staging_name.startswith("migration-")
            or Path(staging_name).name != staging_name
        ):
            raise RuntimeError("migration staging path is invalid")
        staging = self._migration_journal_path.parent / staging_name
        _require_real_directory(staging, self._migration_journal_path.parent)

        snapshots: dict[str, pa.Table] = {}
        for table_name, metadata in payload["tables"].items():
            if metadata is None and payload["format"] == 2:
                continue
            if not isinstance(metadata, dict):
                raise RuntimeError(f"migration snapshot for {table_name} is invalid")
            filename = metadata.get("file")
            if filename != f"{table_name}.arrow":
                raise RuntimeError(f"migration snapshot for {table_name} is invalid")
            snapshot_path = staging / filename
            if snapshot_path.is_symlink():
                raise RuntimeError(f"migration snapshot for {table_name} is a symlink")
            contents = snapshot_path.read_bytes()
            if hashlib.sha256(contents).hexdigest() != metadata.get("sha256"):
                raise RuntimeError(f"migration snapshot for {table_name} is corrupt")
            with pa.memory_map(str(snapshot_path), "r") as source:
                snapshots[table_name] = ipc.open_file(source).read_all()
            if len(snapshots[table_name]) != metadata.get("rows"):
                raise RuntimeError(f"migration snapshot for {table_name} has the wrong row count")
            self._validate_original_schema(table_name, snapshots[table_name].schema)

        self._authoritative_schema_state(
            set(snapshots), snapshots["metadata"].to_pylist() if "metadata" in snapshots else [],
        )
        if DEMAND_TABLE_NAMES <= set(snapshots):
            self._validate_requirement_history(
                self._rows_for_schema("requirement_observations", snapshots["requirement_observations"].to_pylist()),
                self._rows_for_schema("requirement_events", snapshots["requirement_events"].to_pylist()),
            )
        existing = set(self._database.list_tables().tables)
        for table_name in TABLE_SCHEMAS:
            if table_name not in snapshots:
                if table_name in existing:
                    self._close_cached_table_unlocked(table_name)
                    self._database.drop_table(table_name)
                continue
            snapshot = snapshots[table_name]
            self._close_cached_table_unlocked(table_name)
            self._database.create_table(table_name, data=snapshot, mode="overwrite")
        for table_name, snapshot in snapshots.items():
            restored = self._table(table_name).to_arrow()
            if not restored.schema.equals(snapshot.schema, check_metadata=False):
                raise RuntimeError(f"failed to restore {table_name} schema from migration journal")
            if restored.to_pylist() != snapshot.to_pylist():
                raise RuntimeError(f"failed to restore {table_name} rows from migration journal")
        _fsync_tree(self._database_path)
        self._clear_migration_journal_unlocked()

    def _validate_all_migrated_tables_unlocked(
        self,
        expected_rows: dict[str, list[dict[str, object]]],
    ) -> None:
        existing = set(self._database.list_tables().tables)
        if existing != set(TABLE_SCHEMAS):
            raise RuntimeError("migration did not preserve all authoritative tables")
        for table_name, expected_schema in TABLE_SCHEMAS.items():
            table = self._table(table_name)
            if not table.schema.equals(expected_schema, check_metadata=False):
                raise RuntimeError(f"{table_name} migration produced the wrong schema")
            if table.to_arrow().to_pylist() != expected_rows[table_name]:
                raise RuntimeError(f"{table_name} migration did not preserve complete rows")

    def _clear_migration_journal_unlocked(self) -> None:
        _require_recovery_paths(self._requested_database_path, self._writer_lock_path)
        try:
            payload = _read_json_no_follow(self._migration_journal_path)
        except FileNotFoundError:
            return
        staging_name = payload.get("staging")
        self._migration_journal_path.unlink()
        _fsync_directory(self._migration_journal_path.parent)
        if isinstance(staging_name, str) and Path(staging_name).name == staging_name:
            shutil.rmtree(self._migration_journal_path.parent / staging_name, ignore_errors=True)
            _fsync_directory(self._migration_journal_path.parent)

    def _record_dml_unlocked(self) -> None:
        count = self._dml_count_unlocked() + 1
        self._write_dml_count_unlocked(count)
        if count < self._OPTIMIZE_EVERY:
            return

        for table_name in self._database.list_tables().tables:
            self._table(table_name).optimize()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._append_once(
            "events",
            self._event_row(
                Event(
                    id=f"indexes-optimized-{uuid4().hex}",
                    capability_id="__system__",
                    event_type="indexes_optimized",
                    source_context="capability-memory",
                    occurred_at=now,
                    previous_state=None,
                    resulting_state=Lifecycle.OBSERVED,
                    reason=f"automatic maintenance after {self._OPTIMIZE_EVERY} data modifications",
                )
            ),
        )
        self._write_dml_count_unlocked(0)

    def _recover_journals_on_open(self) -> None:
        if not _has_recovery_journal(self._database_path):
            return
        _require_recovery_paths(self._requested_database_path, self._writer_lock_path)
        self._writer_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._writer_lock_path), timeout=30):
            self._recover_transaction_unlocked()
            self._recover_incomplete_migration_unlocked()

    def _prepare_transaction_unlocked(self, *, projection_candidate: Path | None = None) -> Path:
        runtime = self._transaction_journal_path.parent
        runtime.mkdir(parents=True, exist_ok=True)
        transaction_id = uuid4().hex
        staging = runtime / f"transaction-{transaction_id}"
        staging.mkdir()
        shutil.copytree(self._database_path, staging / "database")
        if self._maintenance_state_path.exists():
            shutil.copy2(self._maintenance_state_path, staging / "dml-maintenance.json")
        snapshot_metadata = self._transaction_snapshot_metadata(
            staging, derived_projection=projection_candidate is not None,
        )
        _fsync_tree(staging)
        temporary = runtime / f".{self._transaction_journal_path.name}-{uuid4().hex}.tmp"
        _write_json_fsynced(
            temporary,
            {
                "format": 2,
                "transaction": transaction_id,
                "staging": staging.name,
                "snapshot": snapshot_metadata,
                **({
                    "projection_candidate": projection_candidate.name,
                    "snapshot_kind": "derived-projection-v1",
                } if projection_candidate is not None else {}),
            },
        )
        os.replace(temporary, self._transaction_journal_path)
        _fsync_directory(runtime)
        return staging

    def _commit_transaction_unlocked(self, staging: Path) -> None:
        _fsync_tree(self._database_path)
        _write_json_fsynced(
            staging / "commit.json",
            {"transaction": staging.name.removeprefix("transaction-")},
        )
        _fsync_directory(staging)
        self._clear_transaction_unlocked(staging)

    def _rollback_transaction_unlocked(self, staging: Path) -> None:
        snapshot = staging / "database"
        if not snapshot.is_dir() or snapshot.is_symlink():
            raise RuntimeError("transaction database snapshot is unavailable")
        journal = _read_json_no_follow(self._transaction_journal_path)
        actual = self._transaction_snapshot_metadata(
            staging, validate=True, allow_unmarked=journal.get("format") == 1,
            derived_projection=self._is_projection_snapshot_unlocked(journal),
        )
        if journal.get("format") == 2 and json.dumps(actual, sort_keys=True) != json.dumps(
            journal.get("snapshot"), sort_keys=True,
        ):
            raise RuntimeError("transaction snapshot hashes, counts or schemas are corrupt")
        self.close()
        shutil.rmtree(self._database_path, ignore_errors=True)
        shutil.copytree(snapshot, self._database_path)
        maintenance_snapshot = staging / "dml-maintenance.json"
        if maintenance_snapshot.is_file() and not maintenance_snapshot.is_symlink():
            self._maintenance_state_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(maintenance_snapshot, self._maintenance_state_path)
        else:
            self._maintenance_state_path.unlink(missing_ok=True)
        _fsync_tree(self._database_path)
        _fsync_directory(self._database_path.parent)
        self._database = lancedb.connect(str(self._database_path))
        self._tables = {}
        self._table_markers = {}
        _register_repository(self)
        self._clear_transaction_unlocked(staging)

    @classmethod
    def _transaction_snapshot_metadata(
        cls, staging: Path, *, validate: bool = False, allow_unmarked: bool = False,
        derived_projection: bool = False,
    ) -> dict[str, Any]:
        """Read and prove the entire rollback image before touching the live store."""
        _require_real_tree(staging, staging.parent)
        snapshot = staging / "database"
        _require_real_directory(snapshot, staging)
        files = {}
        for path in sorted(staging.rglob("*")):
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise RuntimeError("transaction snapshot contains a nonregular file")
            files[path.relative_to(staging).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        tables = {}
        if derived_projection:
            # A prior disposable projection can contain broken schemas/history
            # or unreadable tables. Prove its exact bytes for rollback without
            # promoting those old bytes to valid authority. New candidates are
            # independently validated before activation.
            return {"files": files, "tables": tables}
        with cls.open(snapshot) as saved:
            if validate:
                allowed_states = {"v2", "unmarked_v2"} if allow_unmarked else {"v2"}
                if saved._authoritative_schema_state_unlocked() not in allowed_states:
                    raise RuntimeError("authoritative_store_corrupt: transaction snapshot is not schema v2")
            for name in saved._database.list_tables().tables:
                arrow = saved._table(name).to_arrow()
                tables[name] = {
                    "rows": len(arrow),
                    "schema": hashlib.sha256(arrow.schema.serialize().to_pybytes()).hexdigest(),
                }
            if validate:
                saved._validate_requirement_history_unlocked()
        return {"files": files, "tables": tables}

    def _recover_transaction_unlocked(self) -> None:
        if self._transaction_journal_path.exists() or self._transaction_journal_path.is_symlink():
            _require_recovery_paths(self._requested_database_path, self._writer_lock_path)
        try:
            payload = _read_json_no_follow(self._transaction_journal_path)
        except FileNotFoundError:
            return
        if type(payload.get("format")) is not int or payload["format"] not in {1, 2}:
            raise RuntimeError("transaction journal is invalid")
        self._projection_candidate_unlocked(payload)
        self._is_projection_snapshot_unlocked(payload)
        staging_name = payload.get("staging")
        transaction_id = payload.get("transaction")
        if (
            not isinstance(staging_name, str)
            or not isinstance(transaction_id, str)
            or staging_name != f"transaction-{transaction_id}"
            or Path(staging_name).name != staging_name
        ):
            raise RuntimeError("transaction journal is invalid")
        staging = self._transaction_journal_path.parent / staging_name
        _require_real_directory(staging, self._transaction_journal_path.parent)
        try:
            committed = _read_json_no_follow(staging / "commit.json")
        except FileNotFoundError:
            self._rollback_transaction_unlocked(staging)
            return
        if committed != {"transaction": transaction_id}:
            raise RuntimeError("transaction commit marker is invalid")
        self._clear_transaction_unlocked(staging)

    def _clear_transaction_unlocked(self, staging: Path) -> None:
        _require_recovery_paths(self._requested_database_path, self._writer_lock_path)
        journal = _read_json_no_follow(self._transaction_journal_path)
        candidate = self._projection_candidate_unlocked(journal)
        if candidate is not None and candidate.exists():
            shutil.rmtree(candidate)
            _fsync_directory(candidate.parent)
        self._transaction_journal_path.unlink(missing_ok=True)
        _fsync_directory(self._transaction_journal_path.parent)
        shutil.rmtree(staging, ignore_errors=True)
        _fsync_directory(self._transaction_journal_path.parent)

    def _projection_candidate_unlocked(self, journal: Mapping[str, object]) -> Path | None:
        name = journal.get("projection_candidate")
        if name is None:
            return None
        if not isinstance(name, str) or not name.startswith("projection-") or Path(name).name != name:
            raise RuntimeError("transaction projection candidate is invalid")
        candidate = self._transaction_journal_path.parent / name
        if candidate.exists() or candidate.is_symlink():
            _require_real_tree(candidate, candidate.parent)
        return candidate

    def _is_projection_snapshot_unlocked(self, journal: Mapping[str, object]) -> bool:
        if "snapshot_kind" not in journal:
            return False
        if (
            journal["snapshot_kind"] != "derived-projection-v1"
            or journal.get("format") != 2
            or self._projection_candidate_unlocked(journal) is None
        ):
            raise RuntimeError("transaction projection snapshot kind is invalid")
        return True

    def _dml_count_unlocked(self) -> int:
        try:
            payload = json.loads(self._maintenance_state_path.read_text(encoding="utf-8"))
            return int(payload["operations_since_optimize"])
        except FileNotFoundError:
            return 0
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, UnicodeError):
            return 0

    def _write_dml_count_unlocked(self, count: int) -> None:
        self._maintenance_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._maintenance_state_path.with_name(
            f".{self._maintenance_state_path.name}-{uuid4().hex}.tmp"
        )
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"operations_since_optimize": count}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._maintenance_state_path)
        descriptor = os.open(self._maintenance_state_path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        self._validate_managed_paths_for_read()
        self._writer_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._writer_lock_path), timeout=30):
            yield

    @staticmethod
    def _capability_row(capability: Capability, vector: Sequence[float]) -> dict[str, object]:
        capability = redact_capability(capability)
        row = asdict(capability)
        for field in ("category_path", "facets", "constraints", "stack", "runtime", "platform", "dependencies", "compatibility"):
            row[field] = _canonical_json(row[field])
        row["artifact_type"] = capability.artifact_type.value
        row["lifecycle"] = capability.lifecycle.value
        row["vector"] = [float(value) for value in vector]
        row["search_text"] = " ".join(
            (
                capability.name,
                capability.summary,
                capability.contract,
                *capability.category_path,
                *capability.facets,
                *capability.stack,
                *capability.constraints,
                *capability.dependencies,
                *capability.compatibility,
            )
        )
        return row

    @staticmethod
    def _capability_from_row(row: dict[str, Any]) -> Capability:
        category_path = _json_tuple(row["category_path"])
        validate_category_path(category_path)
        return Capability(
            id=row["id"],
            name=row["name"],
            summary=row["summary"],
            category_path=category_path,
            facets=_json_tuple(row["facets"]),
            contract=row["contract"],
            constraints=_json_tuple(row["constraints"]),
            artifact_type=ArtifactType(row["artifact_type"]),
            source_uri=row["source_uri"],
            source_revision=row["source_revision"],
            content_hash=row["content_hash"],
            owner=row["owner"],
            license=row["license"],
            stack=_json_tuple(row["stack"]),
            runtime=_json_tuple(row["runtime"]),
            platform=_json_tuple(row["platform"]),
            dependencies=_json_tuple(row["dependencies"]),
            compatibility=_json_tuple(row["compatibility"]),
            lifecycle=Lifecycle(row["lifecycle"]),
            confidence=row["confidence"],
            expected_net_value=row["expected_net_value"],
            embedding_generation=row["embedding_generation"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_verified_at=row["last_verified_at"],
        )

    @staticmethod
    def _evidence_row(evidence: Evidence) -> dict[str, object]:
        return asdict(redact_evidence(evidence))

    @staticmethod
    def _evidence_from_row(row: dict[str, Any]) -> Evidence:
        return Evidence(**row)

    @staticmethod
    def _relationship_row(relationship: Relationship) -> dict[str, object]:
        relationship = redact_relationship(relationship)
        return {
            **asdict(relationship),
            "compatibility": _canonical_json(relationship.compatibility),
            "evidence_ids": _canonical_json(relationship.evidence_ids),
        }

    @staticmethod
    def _relationship_from_row(row: dict[str, Any]) -> Relationship:
        return Relationship(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            relationship_type=row["relationship_type"],
            compatibility=_json_tuple(row["compatibility"]),
            evidence_ids=_json_tuple(row["evidence_ids"]),
        )

    @staticmethod
    def _event_row(event: Event) -> dict[str, object]:
        event = redact_event(event)
        return {
            **asdict(event),
            "previous_state": event.previous_state.value if event.previous_state is not None else None,
            "resulting_state": event.resulting_state.value,
        }

    @staticmethod
    def _event_from_row(row: dict[str, Any]) -> Event:
        return Event(
            id=row["id"],
            capability_id=row["capability_id"],
            event_type=row["event_type"],
            source_context=row["source_context"],
            occurred_at=row["occurred_at"],
            previous_state=Lifecycle(row["previous_state"]) if row["previous_state"] is not None else None,
            resulting_state=Lifecycle(row["resulting_state"]),
            reason=row["reason"],
        )

    @staticmethod
    def _requirement_observation_row(
        observation: RequirementObservation,
    ) -> dict[str, object]:
        observation = redact_requirement_observation(observation)
        requirement = redact_requirement(observation.requirement)
        return {
            "id": observation.id,
            "requirement": _canonical_json(asdict(requirement)),
            "status": observation.status,
            "observed_at": observation.observed_at,
            "linked_capability_id": observation.linked_capability_id,
        }

    @staticmethod
    def _requirement_observation_from_row(
        row: dict[str, Any],
    ) -> RequirementObservation:
        payload = json.loads(row["requirement"])
        for field in (
            "category_hint",
            "stack",
            "constraints",
            "quality_requirements",
            "runtime",
            "platform",
            "license",
        ):
            payload[field] = tuple(payload.get(field, ()))
        return RequirementObservation(
            id=row["id"],
            requirement=RequirementProfile(**payload),
            status=row["status"],
            observed_at=row["observed_at"],
            linked_capability_id=row["linked_capability_id"],
        )

    @staticmethod
    def _requirement_event_row(event: RequirementEvent) -> dict[str, object]:
        return asdict(redact_requirement_event(event))

    @staticmethod
    def _requirement_event_from_row(row: dict[str, Any]) -> RequirementEvent:
        return RequirementEvent(**row)


def _has_recovery_journal(database_path: Path) -> bool:
    return any(
        os.path.lexists(database_path.parent / "runtime" / name)
        for name in ("transaction-journal.json", "migration-journal.json")
    )


def _require_recovery_paths(database_path: Path, writer_lock_path: Path) -> None:
    """Validate caller paths and owned trees before recovery can mutate them."""
    runtime = database_path.parent / "runtime"
    for path in (database_path, runtime, writer_lock_path):
        # Missing components before ".." can hide symlinks from lexical probes.
        if ".." in path.parts:
            raise RuntimeError(f"unsafe migration recovery path contains parent traversal: {path}")
        for component in (path, *path.parents):
            if component.is_symlink():
                raise RuntimeError(f"unsafe migration recovery path is a symlink: {component}")
    canonical_database = database_path.resolve()
    canonical_lock = writer_lock_path.resolve()
    owner = canonical_lock.parent.parent
    if canonical_lock != owner / "locks" / "writer.lock" or not (
        canonical_database.parent == owner
        or canonical_database.is_relative_to(owner / "generations")
        or canonical_database == owner / "derived" / "database"
        or canonical_database.is_relative_to(owner / "derived" / "generations")
    ):
        raise RuntimeError("unsafe migration recovery lock ownership")
    if writer_lock_path.exists() and not writer_lock_path.is_file():
        raise RuntimeError("unsafe migration recovery lock is not a file")
    for directory in (database_path, runtime):
        if directory.exists():
            _require_real_tree(directory, database_path.parent)


def _read_json_no_follow(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            payload = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def _write_json_fsynced(path: Path, payload: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"refusing to fsync symlink: {path}")
        if path.is_file():
            _fsync_file(path)
        elif path.is_dir():
            directories.append(path)
    for directory in sorted(directories, key=lambda candidate: len(candidate.parts), reverse=True):
        _fsync_directory(directory)


def _require_real_directory(path: Path, parent: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"unsafe migration staging directory: {path}")
    if not path.resolve().is_relative_to(parent.resolve()):
        raise RuntimeError(f"migration staging directory escapes runtime: {path}")


def _require_real_tree(root: Path, parent: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"active generation path is unsafe: {root}")
    if not root.resolve().is_relative_to(parent.resolve()):
        raise RuntimeError(f"active generation escapes the memory root: {root}")
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise RuntimeError(f"active generation contains a symlink: {entry.path}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))


def _artifact_digest(repository: CapabilityRepository) -> str:
    rows = [
        {
            "id": row["id"],
            "search_text": row["search_text"],
            "vector": [float(value) for value in row["vector"]],
        }
        for row in repository._rows("capabilities")
    ]
    payload = json.dumps(
        sorted(rows, key=lambda row: row["id"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
