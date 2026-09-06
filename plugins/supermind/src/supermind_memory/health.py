"""Fail-closed health checks and atomic derived-index generations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shutil
import stat
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout
from lancedb.index import FTS

from supermind_memory.config import MemoryPaths
from supermind_memory.embeddings import EmbeddingProvider
from supermind_memory.redaction import redact_text
from supermind_memory.repository import CapabilityRepository, validate_evidence
from supermind_memory.retrieval_evaluation import evaluate_generation, validate_evaluation_evidence
from supermind_memory.schema import EMBEDDING_DIMENSION, SCHEMA_VERSION, TABLE_SCHEMAS
from supermind_memory.types import (
    CapabilityMemoryBlocked,
    HealthReport,
)


_CHECK_NAMES = ("runtime", "model", "schema", "source", "vector", "fts")


class HealthManager:
    """Validates every retrieval dependency before activating a generation."""

    def __init__(
        self,
        paths: MemoryPaths,
        repository: CapabilityRepository,
        embedding_provider: EmbeddingProvider,
        *,
        authority_mode: str | None = None,
        expected_authority_digest: str | None = None,
    ) -> None:
        self.paths = paths
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.authority_mode = authority_mode
        self._pointer_path = paths.root / "active-generation.json"
        self._writer_lock_path = paths.locks / "writer.lock"
        self.repository.configure_generation_reads(
            paths.root, required=True, authority_mode=authority_mode,
            expected_authority_digest=expected_authority_digest,
        )

    def check(self) -> HealthReport:
        lock_path_failure = self._writer_lock_safety_failure()
        if lock_path_failure is not None:
            return self._failed_report(lock_path_failure)
        try:
            with FileLock(str(self._writer_lock_path), timeout=30):
                return self._check_unlocked()
        except Timeout as error:
            return self._failed_report(
                f"writer_lock_timeout: writer lock timed out after 30 seconds: {error}"
            )

    def _check_unlocked(self) -> HealthReport:
        path_failure = self._path_safety_failure()
        if path_failure is not None:
            return self._failed_report(path_failure)
        failures, generation = self._collect_failures(include_generation=True)
        return HealthReport(
            healthy=not failures,
            active_generation=generation,
            repairs=(),
            checked_at=_utc_now(),
            failures=tuple(failures),
        )

    def ensure_healthy(self) -> HealthReport:
        lock_path_failure = self._writer_lock_safety_failure()
        if lock_path_failure is not None:
            self._block_unsafe_path(self._failed_report(lock_path_failure))

        try:
            with FileLock(str(self._writer_lock_path), timeout=30):
                current = self._check_unlocked()
                if current.healthy:
                    return current
                self._block_unsafe_path(current)
                generation = self._repair_with_retries(current)
                repaired = self._check_unlocked()
                if not repaired.healthy:
                    raise CapabilityMemoryBlocked(
                        self._blocking_code(repaired),
                        "capability memory remains unhealthy after repair",
                        repaired.failures,
                    )
                return HealthReport(
                    healthy=True,
                    active_generation=generation,
                    repairs=(self._repair_action(current),),
                    checked_at=repaired.checked_at,
                    failures=(),
                )
        except Timeout as error:
            message = f"writer lock timed out after 30 seconds: {error}"
            raise CapabilityMemoryBlocked("writer_lock_timeout", message, (message,)) from error

    def rebuild_indexes(self) -> str:
        """Force one new fully validated derived-index generation."""
        lock_path_failure = self._writer_lock_safety_failure()
        if lock_path_failure is not None:
            self._block_unsafe_path(self._failed_report(lock_path_failure))
        try:
            with FileLock(str(self._writer_lock_path), timeout=30):
                initial = self._check_unlocked()
                self._block_unsafe_path(initial)
                return self._repair_with_retries(initial)
        except Timeout as error:
            message = f"writer lock timed out after 30 seconds: {error}"
            raise CapabilityMemoryBlocked("writer_lock_timeout", message, (message,)) from error

    def _repair_with_retries(self, report: HealthReport) -> str:
        errors: list[str] = []
        for attempt in range(1, 4):
            try:
                return self._rebuild_indexes_locked()
            except Exception as error:
                errors.append(redact_text(f"attempt {attempt}: {error}"))
        code = self._blocking_code(report, errors)
        message = f"capability memory repair failed after 3 attempts: {errors[-1]}"
        raise CapabilityMemoryBlocked(code, message, tuple(errors))

    def _rebuild_indexes_locked(self) -> str:
        self._ensure_owned_directories()
        self.repository._recover_transaction_unlocked()
        self.repository._recover_incomplete_migration_unlocked()
        self.repository._migrate_schemas_unlocked()
        self.repository._ensure_evidence_order_unlocked()
        generation = f"generation-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex}"
        generation_dir = self.paths.generations / generation
        generation_dir.mkdir(parents=False)
        try:
            self._write_runtime_metadata(generation_dir)
            pre_index_failures, _ = self._collect_failures(include_generation=False)
            if pre_index_failures:
                raise RuntimeError("; ".join(pre_index_failures))

            self._build_generation_database(generation_dir, generation)
            artifact_failures: list[str] = []
            self._validate_generation_artifact(generation, artifact_failures)
            if artifact_failures:
                raise RuntimeError("; ".join(artifact_failures))
            evaluation = self._evaluate_generation(generation)
            if not evaluation["passed"]:
                raise RuntimeError(
                    "retrieval_evaluation_regression: candidate generation did not meet "
                    "the versioned quality thresholds"
                )

            source_digest, source_count = self._source_fingerprint()
            manifest = {
                "artifact_digest": self._generation_artifact_digest(generation),
                "checks": {name: True for name in _CHECK_NAMES},
                "created_at": _utc_now(),
                "generation": generation,
                "model_lock_digest": self._model_lock_digest(),
                "schema_version": SCHEMA_VERSION,
                "source_count": source_count,
                "source_digest": source_digest,
                "vector_mode": "exact",
                "evaluation": evaluation,
                "evaluation_dataset_digest": evaluation["digest"],
            }
            # TEMPORARY Task 3 staging guard; Task 8 requires event authority.
            if self.authority_mode == "events-v1":
                self.repository._check_authority_binding()
                manifest["authority_event_set_digest"] = self.repository.get_metadata("authority_event_set_digest")
                manifest["materialized_digest"] = self.repository.get_metadata("authority_materialized_digest")
            self._write_json_fsynced(generation_dir / "manifest.json", manifest)
            _fsync_tree(generation_dir)
            _fsync_directory(self.paths.generations)
            self._activate_generation(generation)
            return generation
        except BaseException:
            if not self._pointer_names(generation):
                shutil.rmtree(generation_dir, ignore_errors=True)
            raise

    def _collect_failures(
        self,
        *,
        include_generation: bool,
    ) -> tuple[list[str], str | None]:
        failures: list[str] = []
        generation: str | None = None

        live_healthy = self._check_runtime(failures)
        if live_healthy:
            live_healthy = self._check_model(failures)
        if live_healthy:
            live_healthy = self._check_schema(failures)
        if live_healthy:
            live_healthy = self._check_source(failures)
        if include_generation:
            generation = self._check_generation(failures, verify_artifact=live_healthy)
        return failures, generation

    def _check_runtime(self, failures: list[str]) -> bool:
        if sys.version_info < (3, 11) or sys.version_info >= (3, 15):
            failures.append("runtime_incompatible: Python must be >=3.11,<3.15")
            return False
        metadata_path = self.paths.runtime / "runtime.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            expected = {
                "format": 1,
                "implementation": platform.python_implementation(),
                "python": platform.python_version(),
            }
            if metadata != expected:
                raise ValueError("runtime metadata does not match the current interpreter")
            return True
        except Exception as error:
            failures.append(redact_text(f"runtime_unavailable: {error}"))
            return False

    def _check_model(self, failures: list[str]) -> bool:
        try:
            vector = self.embedding_provider.embed_query("capability memory health probe")
            if len(vector) != EMBEDDING_DIMENSION or any(
                not math.isfinite(float(value)) for value in vector
            ):
                raise ValueError(
                    f"embedding probe must return {EMBEDDING_DIMENSION} finite values"
                )
            self._model_lock_digest()
            return True
        except Exception as error:
            failures.append(redact_text(f"embedding_unavailable: {error}"))
            return False

    def _check_schema(
        self,
        failures: list[str],
        repository: CapabilityRepository | None = None,
    ) -> bool:
        checked_repository = repository or self.repository
        try:
            if (
                checked_repository is self.repository
                and checked_repository._migration_journal_path.exists()
            ):
                raise ValueError("incomplete schema migration must be recovered")
            if checked_repository._authoritative_schema_state_unlocked() != "v2":
                raise ValueError("authoritative schema marker requires journaled migration")
            existing = set(checked_repository._database.list_tables().tables)
            missing = set(TABLE_SCHEMAS) - existing
            if missing:
                raise ValueError(f"missing tables: {', '.join(sorted(missing))}")
            for table_name, expected in TABLE_SCHEMAS.items():
                actual = checked_repository._table(table_name).schema
                if not actual.equals(expected, check_metadata=False):
                    raise ValueError(f"{table_name} schema does not match version {SCHEMA_VERSION}")
            return True
        except Exception as error:
            message = str(error)
            failures.append(redact_text(
                message if message.startswith("authoritative_store_corrupt:") else f"schema_unavailable: {message}"
            ))
            return False

    def _check_source(self, failures: list[str]) -> bool:
        try:
            self._source_fingerprint()
            for row in self.repository._rows("evidence"):
                evidence = self.repository._evidence_from_row(row)
                validate_evidence(evidence)
            self.repository._read_evidence_order_unlocked(require_complete=True)
            for row in self.repository._rows("relationships"):
                self.repository._relationship_from_row(row)
            for row in self.repository._rows("events"):
                self.repository._event_from_row(row)
            self.repository._validate_requirement_history_unlocked()
            for row in self.repository._rows("metadata"):
                json.loads(row["value"])
            self.repository._check_authority_binding()
            return True
        except Exception as error:
            message = str(error)
            failures.append(redact_text(
                message if message.startswith(("authoritative_store_corrupt:", "projection_mismatch:", "authority_digest_mismatch:")) else f"source_unavailable: {message}"
            ))
            return False

    def _check_vectors(
        self,
        failures: list[str],
        repository: CapabilityRepository,
    ) -> bool:
        try:
            rows = repository._rows("capabilities")
            for row in rows:
                vector = row["vector"]
                if len(vector) != EMBEDDING_DIMENSION or any(
                    not math.isfinite(float(value)) for value in vector
                ):
                    raise ValueError(f"capability {row['id']} has an invalid vector")
            repository._table("capabilities").search(
                [0.0] * EMBEDDING_DIMENSION,
                query_type="vector",
                vector_column_name="vector",
            ).limit(1).to_list()
            return True
        except Exception as error:
            failures.append(redact_text(f"vector_unavailable: {error}"))
            return False

    def _check_fts(
        self,
        failures: list[str],
        repository: CapabilityRepository,
    ) -> bool:
        try:
            table = repository._table("capabilities")
            indexes = tuple(table.list_indices())
            fts_index = next((
                index
                for index in indexes
                if index.index_type == "FTS" and "search_text" in index.columns
            ), None)
            if fts_index is None:
                raise ValueError("search_text FTS index is missing")
            rows = repository._rows("capabilities")
            if (
                getattr(fts_index, "num_indexed_rows", None) != len(rows)
                or getattr(fts_index, "num_unindexed_rows", None) != 0
            ):
                raise ValueError("search_text FTS index does not cover every record")
            for row in rows:
                capability = repository._capability_from_row(row)
                expected = repository._capability_row(
                    capability,
                    [0.0] * EMBEDDING_DIMENSION,
                )["search_text"]
                expected = f"{expected} {_fts_probe_token(capability.id)}"
                if row["search_text"] != expected:
                    raise ValueError(f"capability {capability.id} has stale search text")
            if rows:
                known = repository._capability_from_row(rows[0])
                results = table.search(
                    _fts_probe_token(known.id),
                    query_type="fts",
                    fts_columns="search_text",
                ).limit(max(1, len(rows))).to_list()
                if not any(result.get("id") == known.id for result in results):
                    raise ValueError(
                        f"known record {known.id} was not returned by the search_text FTS index"
                    )
            return True
        except Exception as error:
            failures.append(redact_text(f"fts_unavailable: {error}"))
            return False

    def _check_generation(
        self,
        failures: list[str],
        *,
        verify_artifact: bool,
    ) -> str | None:
        try:
            failure_count = len(failures)
            pointer = _read_json_no_follow(self._pointer_path)
            generation = pointer["active_generation"]
            if (
                not isinstance(generation, str)
                or not generation.startswith("generation-")
                or Path(generation).name != generation
            ):
                raise ValueError("active generation is invalid")
            manifest_path = self.paths.generations / generation / "manifest.json"
            manifest = _read_json_no_follow(manifest_path)
            if manifest.get("generation") != generation:
                raise ValueError("generation manifest does not match pointer")
            try:
                self.repository._check_authority_binding(manifest)
            except RuntimeError as error:
                failures.append(redact_text(str(error)))
            if manifest.get("schema_version") != SCHEMA_VERSION:
                failures.append(
                    redact_text(f"schema_version_mismatch: expected {SCHEMA_VERSION}, got {manifest.get('schema_version')}")
                )
            if manifest.get("checks") != {name: True for name in _CHECK_NAMES}:
                failures.append("generation_manifest_invalid: health checks are incomplete")
            if manifest.get("model_lock_digest") != self._model_lock_digest():
                failures.append("generation_manifest_invalid: model lock changed")
            if manifest.get("vector_mode") != "exact":
                failures.append("generation_manifest_invalid: vector mode is not exact")
            if manifest.get("evaluation_dataset_digest") != self._evaluation_dataset_digest():
                failures.append("generation_manifest_invalid: retrieval evaluation dataset changed")
            evaluation = manifest.get("evaluation")
            validate_evaluation_evidence(evaluation, self.embedding_provider, self._evaluation_dataset_path())
            if evaluation["digest"] != manifest.get("evaluation_dataset_digest"):
                raise ValueError("retrieval evaluation dataset digests disagree")
            if verify_artifact and len(failures) == failure_count:
                source_digest, source_count = self._source_fingerprint()
                if (
                    manifest.get("source_digest") != source_digest
                    or manifest.get("source_count") != source_count
                ):
                    failures.append("source_generation_stale: structured records changed")
                else:
                    artifact_digest = self._generation_artifact_digest(generation)
                    if manifest.get("artifact_digest") != artifact_digest:
                        failures.append(
                            "artifact_digest_mismatch: generation vector or search text changed"
                        )
                    else:
                        self._validate_generation_artifact(generation, failures)
            return generation
        except Exception as error:
            code = "active_generation_missing" if isinstance(error, FileNotFoundError) else "generation_manifest_invalid"
            failures.append(redact_text(f"{code}: {error}"))
        return None

    def _source_fingerprint(self) -> tuple[str, int]:
        return self.repository._structured_source_fingerprint()

    def _build_generation_database(self, generation_dir: Path, generation: str) -> None:
        capabilities = self.repository.list_capabilities()
        with CapabilityRepository.open(
            generation_dir / "database",
            writer_lock_path=self._writer_lock_path,
        ) as generation_repository:
            generation_repository._initialize_unlocked()
            template_rows = []
            for capability in capabilities:
                row = generation_repository._capability_row(
                    replace(capability, embedding_generation=generation),
                    [0.0] * EMBEDDING_DIMENSION,
                )
                template_rows.append(row)
            embedding_texts = [str(row["search_text"]) for row in template_rows]
            vectors = (
                self.embedding_provider.embed_documents(embedding_texts)
                if template_rows
                else []
            )
            if len(vectors) != len(template_rows):
                raise ValueError("embedding provider returned the wrong document count")
            for capability, row, vector in zip(
                capabilities, template_rows, vectors, strict=True
            ):
                if len(vector) != EMBEDDING_DIMENSION or any(
                    not math.isfinite(float(value)) for value in vector
                ):
                    raise ValueError(f"capability {capability.id} produced an invalid vector")
                row["vector"] = [float(value) for value in vector]
                row["search_text"] = (
                    f"{row['search_text']} {_fts_probe_token(capability.id)}"
                )
            if template_rows:
                generation_repository._table("capabilities").add(template_rows)
            generation_repository._table("capabilities").create_index(
                "search_text",
                config=FTS(),
                replace=True,
            )

    def _validate_generation_artifact(
        self,
        generation: str,
        failures: list[str],
    ) -> None:
        try:
            with CapabilityRepository.open(
                self.paths.generations / generation / "database",
                writer_lock_path=self._writer_lock_path,
            ) as generation_repository:
                if not self._check_schema(failures, generation_repository):
                    return
                expected = tuple(
                    replace(capability, embedding_generation=generation)
                    for capability in self.repository.list_capabilities()
                )
                actual = generation_repository.list_capabilities()
                if actual != expected:
                    failures.append(
                        "source_unavailable: generation does not exactly mirror "
                        "authoritative records"
                    )
                    return
                if not self._check_vectors(failures, generation_repository):
                    return
                self._check_fts(failures, generation_repository)
        except Exception as error:
            failures.append(redact_text(f"generation_artifact_unavailable: {error}"))

    def _model_lock_digest(self) -> str:
        path = Path(__file__).resolve().parents[2] / "model.lock.json"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _evaluation_dataset_path(self) -> Path:
        return Path(__file__).resolve().parents[2] / "evaluation" / "retrieval-v1.json"

    def _evaluation_dataset_digest(self) -> str:
        return hashlib.sha256(self._evaluation_dataset_path().read_bytes()).hexdigest()

    def _evaluate_generation(self, generation: str) -> dict[str, object]:
        try:
            with CapabilityRepository.open(
                self.paths.generations / generation / "database",
                writer_lock_path=self._writer_lock_path,
            ) as generation_repository:
                return asdict(evaluate_generation(
                    generation_repository,
                    self.embedding_provider,
                    self._evaluation_dataset_path(),
                ))
        except Exception as error:
            raise RuntimeError(
                f"retrieval_evaluation_unavailable: retrieval evaluation failed: {error}"
            ) from error

    def _generation_artifact_digest(self, generation: str) -> str:
        with CapabilityRepository.open(
            self.paths.generations / generation / "database",
            writer_lock_path=self._writer_lock_path,
        ) as generation_repository:
            rows = [
                {
                    "id": row["id"],
                    "search_text": row["search_text"],
                    "vector": [float(value) for value in row["vector"]],
                }
                for row in generation_repository._rows("capabilities")
            ]
        payload = json.dumps(
            sorted(rows, key=lambda row: row["id"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _path_safety_failure(self) -> str | None:
        try:
            root = self.paths.root.absolute()
            expected = {
                "database": root / "database",
                "runtime": root / "runtime",
                "model_cache": root / "model-cache",
                "locks": root / "locks",
                "generations": root / "generations",
            }
            _reject_symlink(root)
            if root.resolve() != root:
                raise ValueError("memory root resolves through a symlink")
            for name, expected_path in expected.items():
                actual = getattr(self.paths, name).absolute()
                if actual != expected_path:
                    raise ValueError(f"{name} is outside the owned memory root")
                _reject_symlink(actual)
                if actual.resolve() != actual:
                    raise ValueError(f"{name} resolves through a symlink")
            for path in (
                root,
                *expected.values(),
                self._pointer_path,
                expected["runtime"] / "migration-journal.json",
                expected["runtime"] / "transaction-journal.json",
                expected["locks"] / "writer.lock",
            ):
                _reject_symlink(path)
            if root.exists() and not root.is_dir():
                raise ValueError("memory root is not a directory")
            _reject_escape_symlink_tree(root)

            pointer = self._pointer_payload_if_present()
            if pointer is not None:
                generation = pointer.get("active_generation")
                if (
                    not isinstance(generation, str)
                    or not generation.startswith("generation-")
                    or Path(generation).name != generation
                ):
                    raise ValueError("active generation is invalid")
                generation_dir = expected["generations"] / generation
                _reject_symlink_tree(generation_dir)
            return None
        except Exception as error:
            return f"path_escape: {error}"

    def _writer_lock_safety_failure(self) -> str | None:
        try:
            root = self.paths.root.absolute()
            locks = self.paths.locks.absolute()
            if (
                locks != root / "locks"
                or self._writer_lock_path.absolute() != locks / "writer.lock"
            ):
                raise ValueError("writer lock is outside the owned memory root")
            for path in (root, locks, self._writer_lock_path):
                _reject_symlink(path)
            if root.resolve() != root:
                raise ValueError("memory root resolves through a symlink")
            if locks.resolve() != locks:
                raise ValueError("locks resolves through a symlink")
            return None
        except Exception as error:
            return f"path_escape: {error}"

    @staticmethod
    def _failed_report(failure: str) -> HealthReport:
        return HealthReport(
            healthy=False,
            active_generation=None,
            repairs=(),
            checked_at=_utc_now(),
            failures=(redact_text(failure),),
        )

    def _pointer_payload_if_present(self) -> dict[str, object] | None:
        try:
            return _read_json_no_follow(self._pointer_path)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeError, ValueError):
            # Malformed pointer content is a repairable generation error, not a
            # filesystem-containment violation. _check_generation reports it.
            return None

    @staticmethod
    def _block_unsafe_path(report: HealthReport) -> None:
        failures = tuple(
            failure for failure in report.failures if failure.startswith("path_escape:")
        )
        if failures:
            raise CapabilityMemoryBlocked(
                "path_escape",
                "capability memory path validation failed closed",
                failures,
            )

    def _ensure_owned_directories(self) -> None:
        for path in (
            self.paths.root,
            self.paths.database,
            self.paths.runtime,
            self.paths.model_cache,
            self.paths.locks,
            self.paths.generations,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _write_runtime_metadata(self, generation_dir: Path) -> None:
        metadata = {
            "format": 1,
            "implementation": platform.python_implementation(),
            "python": platform.python_version(),
        }
        self._write_json_fsynced(self.paths.runtime / "runtime.json", metadata)
        self._write_json_fsynced(generation_dir / "runtime.json", metadata)

    def _activate_generation(self, generation: str) -> None:
        payload = {"active_generation": generation, "activated_at": _utc_now()}
        temporary = self.paths.root / f".active-generation-{uuid4().hex}.tmp"
        try:
            self._write_json_fsynced(temporary, payload)
            os.replace(temporary, self._pointer_path)
            _fsync_directory(self.paths.root)
        finally:
            temporary.unlink(missing_ok=True)

    def _pointer_names(self, generation: str) -> bool:
        try:
            payload = _read_json_no_follow(self._pointer_path)
            return payload.get("active_generation") == generation
        except Exception:
            return False

    @staticmethod
    def _write_json_fsynced(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _repair_action(report: HealthReport) -> str:
        codes = {failure.partition(":")[0] for failure in report.failures}
        if "schema_version_mismatch" in codes or "schema_unavailable" in codes:
            return "migrate_schema"
        if "active_generation_missing" in codes and "runtime_unavailable" in codes:
            return "initialize_store"
        return "rebuild_search_indexes"

    @staticmethod
    def _blocking_code(report: HealthReport, errors: list[str] | None = None) -> str:
        terminal_codes = (
            "path_escape",
            "embedding_unavailable",
            "runtime_unavailable",
            "schema_unavailable",
            "source_unavailable",
            "vector_unavailable",
            "fts_unavailable",
            "artifact_digest_mismatch",
            "retrieval_evaluation_regression",
            "retrieval_evaluation_unavailable",
            "authoritative_store_corrupt",
            "projection_mismatch",
            "authority_digest_mismatch",
            "authority_generation_stale",
        )
        for error in reversed(errors or ()):
            for code in terminal_codes:
                if f"{code}:" in error:
                    return code
        codes = [failure.partition(":")[0] for failure in report.failures]
        if "embedding_unavailable" in codes:
            return "embedding_unavailable"
        return codes[0] if codes else "repair_failed"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fts_probe_token(capability_id: str) -> str:
    digest = hashlib.sha256(capability_id.encode("utf-8")).hexdigest()[:16]
    return f"cmprobe{digest}"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in root.rglob("*"):
        if path.is_file():
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        elif path.is_dir():
            directories.append(path)
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)


def _read_json_no_follow(path: Path) -> dict[str, object]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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


def _reject_symlink(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise ValueError(f"owned path must not be a symlink: {path}")


def _reject_symlink_tree(root: Path) -> None:
    _reject_symlink(root)
    if not root.exists():
        return
    if not root.is_dir():
        raise ValueError(f"generation path is not a directory: {root}")
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise ValueError(f"generation contains a symlink: {entry.path}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))


def _reject_escape_symlink_tree(root: Path) -> None:
    _reject_symlink(root)
    if not root.exists():
        return
    contained_root = root.resolve()
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink():
                    try:
                        target = path.resolve(strict=True)
                    except FileNotFoundError as error:
                        raise ValueError(f"owned path contains a broken symlink: {path}") from error
                    if not target.is_relative_to(contained_root):
                        raise ValueError(f"owned path contains an escaping symlink: {path}")
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(path)
