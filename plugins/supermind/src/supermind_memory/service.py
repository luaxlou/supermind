"""Storage-independent orchestration for Capability Memory operations."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from supermind_memory.bootstrap import Bootstrap
from supermind_memory.config import MemoryPaths
from supermind_memory.discovery import CapabilityDiscovery
from supermind_memory.embeddings import EmbeddingProvider
from supermind_memory.event_model import AuthorityEvent, canonical_json
from supermind_memory.health import HealthManager
from supermind_memory.git_client import SubprocessCommandRunner
from supermind_memory.lifecycle import next_lifecycle
from supermind_memory.repository import CapabilityRepository, validate_evidence
from supermind_memory.projection import compare_projection
from supermind_memory.replay import ReplayResult, replay
from supermind_memory.renderer import validate_render
from supermind_memory.sync import SyncBlocked, SyncCoordinator, SyncReport, SyncState
from supermind_memory.redaction import (
    redact_capability,
    redact_evidence,
    redact_identifier,
    redact_requirement,
    redact_reuse_result,
    redact_text,
    redact_uri,
)
from supermind_memory.schema import EMBEDDING_DIMENSION
from supermind_memory.scoring import expected_net_value
from supermind_memory.search import CapabilitySearch
from supermind_memory.source_resolution import resolve_source, source_available
from supermind_memory.taxonomy import validate_category_path
from supermind_memory.types import (
    Capability,
    CapabilityMemoryBlocked,
    DiscoveryContext,
    DiscoveryResult,
    EvaluationResult,
    Event,
    Evidence,
    HealthReport,
    Lifecycle,
    RequirementProfile,
    RequirementEvent,
    RequirementObservation,
    ReuseResult,
    SearchResult,
    SearchStatus,
    ValueInputs,
)


MAX_CONSISTENCY_RESTARTS = 3
MAX_RETRIEVAL_REPAIRS = 1


class CapabilityMemory:
    """Coordinate health, discovery, persistence, and evidence-driven reuse."""

    def __init__(
        self,
        *,
        bootstrap: Bootstrap,
        discovery: CapabilityDiscovery,
        repository: CapabilityRepository,
        search_engine: CapabilitySearch,
        embedding_provider: EmbeddingProvider,
        health_manager: HealthManager,
        codex_home: Path,
        sync_coordinator: SyncCoordinator | None = None,
        authority_mode: str = "events-v1",
        command_runner: object | None = None,
    ) -> None:
        if authority_mode != "events-v1":
            raise ValueError("unsupported_authority_mode")
        if sync_coordinator is None:
            raise ValueError("event_authority_unavailable")
        self.bootstrap = bootstrap
        self.discovery_engine = discovery
        self.repository = repository
        self.search_engine = search_engine
        self.embedding_provider = embedding_provider
        self.health_manager = health_manager
        self.codex_home = codex_home.expanduser().absolute()
        self.sync_coordinator = sync_coordinator
        self.authority_mode = authority_mode
        self.command_runner = command_runner
        self._last_sync_report: SyncReport | None = None
        self._project_root: Path | None = None

    def __enter__(self) -> "CapabilityMemory":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.repository.close()

    def initialize(self, project_root: Path) -> HealthReport:
        self._ensure_healthy()
        self.bootstrap.initialize(project_root)
        self._project_root = project_root.expanduser().resolve()
        self._discover_and_persist(
            DiscoveryContext(
                project_root=self._project_root,
                codex_home=self.codex_home,
            )
        )
        return self._ensure_healthy()

    def search(self, requirement: RequirementProfile) -> SearchResult:
        self._assert_event_projection()
        requirement = redact_requirement(requirement)
        attempts: list[str] = []
        retrieval_attempt = 0
        consistency_restarts = 0
        retrieval_repairs = 0
        while consistency_restarts <= MAX_CONSISTENCY_RESTARTS:
            report = self._ensure_healthy()
            token = None
            try:
                token = self.repository.search_health_token(report.active_generation)
                result = self.search_engine.search(requirement, health_token=token)
            except Exception as error:
                result = SearchResult(
                    SearchStatus.FAILED,
                    (),
                    error_code="search_failed",
                    error_message=str(error),
                )
            if token is None or not self.repository.search_health_token_matches(token):
                consistency_restarts += 1
                attempts.append(
                    f"consistency restart {consistency_restarts}: authoritative state changed"
                )
                continue
            if result.status is SearchStatus.COMPLETE and result.generation != token.generation:
                consistency_restarts += 1
                attempts.append(
                    f"consistency restart {consistency_restarts}: search returned the wrong generation"
                )
                continue
            if result.status is SearchStatus.COMPLETE:
                return result

            retrieval_attempt += 1
            attempts.append(
                redact_text(
                    f"retrieval attempt {retrieval_attempt}: "
                    f"{result.error_code or 'search_failed'}: "
                    f"{result.error_message or 'retrieval failed'}"
                )
            )
            if retrieval_repairs >= MAX_RETRIEVAL_REPAIRS:
                raise _blocked(
                    result.error_code or "search_failed",
                    "complete hybrid retrieval failed after one repair and retry",
                    attempts,
                )
            retrieval_repairs += 1
            repair_error = None
            try:
                replacement_generation = self.health_manager.rebuild_indexes()
                repaired = self.health_manager.check()
            except CapabilityMemoryBlocked as error:
                repair_error = _blocked(
                    error.code,
                    "complete hybrid retrieval repair failed",
                    (*attempts, *error.attempts),
                )
            except Exception as error:
                attempts.append(redact_text(f"repair failed: {error}"))
                repair_error = _blocked(
                    "repair_failed",
                    "complete hybrid retrieval repair failed",
                    attempts,
                )
            # Raise outside the handler so neither the cause nor context can
            # retain a provider exception containing raw credentials.
            if repair_error is not None:
                raise repair_error
            if (
                not repaired.healthy
                or repaired.active_generation != replacement_generation
            ):
                attempts.extend(redact_text(failure) for failure in repaired.failures)
                raise _blocked(
                    repaired.failures[0].partition(":")[0]
                    if repaired.failures
                    else "repair_failed",
                    "replacement generation did not pass health validation",
                    attempts,
                )
            attempts.append("repair: activated a validated replacement generation")
        raise _blocked(
            "concurrent_mutation",
            "capability memory changed during each bounded search attempt",
            attempts,
        )

    def discover(self, context: DiscoveryContext) -> DiscoveryResult:
        self._assert_event_projection()
        self._ensure_healthy()
        return self._discover_and_persist(context)

    def refresh_sources(self, project_root: Path) -> DiscoveryResult:
        self._ensure_healthy()
        return self._discover_and_persist(
            DiscoveryContext(
                project_root=project_root.expanduser().resolve(),
                codex_home=self.codex_home,
            )
        )

    def get(self, capability_id: str) -> Capability | None:
        self._assert_event_projection()
        return self.repository.get_capability(capability_id)

    def observe_unmet_requirement(
        self,
        requirement: RequirementProfile,
    ) -> RequirementObservation:
        """Persist demand separately after a complete build decision."""
        requirement = redact_requirement(requirement)
        canonical = json.dumps(
            asdict(requirement),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        observation_id = "requirement-" + hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()[:32]
        if self.sync_coordinator is not None:
            return self._observe_demand_event_first(observation_id, requirement)
        now = _utc_now()
        with self.repository.atomic_write():
            existing = self.repository.get_requirement_observation(observation_id)
            if existing is not None:
                return existing
            observation = RequirementObservation(
                id=observation_id,
                requirement=requirement,
                status="unmet",
                observed_at=now,
            )
            event = RequirementEvent(
                id=f"{observation_id}-observed",
                observation_id=observation_id,
                event_type="unmet_observed",
                occurred_at=now,
                reason="complete capability search produced a build decision",
            )
            self.repository._append_once(
                "requirement_observations",
                self.repository._requirement_observation_row(observation),
            )
            self.repository._append_once(
                "requirement_events",
                self.repository._requirement_event_row(event),
            )
            self.repository._record_dml_unlocked()
        return observation

    def list_requirement_observations(self) -> tuple[RequirementObservation, ...]:
        self._assert_event_projection()
        return self.repository.list_requirement_observations()

    def list_requirement_events(
        self,
        observation_id: str,
    ) -> tuple[RequirementEvent, ...]:
        self._assert_event_projection()
        return self.repository.list_requirement_events(observation_id)

    def link_requirement_observation(
        self,
        observation_id: str,
        capability_id: str,
    ) -> RequirementObservation:
        """Atomically satisfy one unmet demand with a current verified capability."""
        observation_id = redact_identifier(observation_id)
        capability_id = redact_identifier(capability_id)
        if self.sync_coordinator is not None:
            return self._link_demand_event_first(observation_id, capability_id)
        self._ensure_healthy()
        now = _utc_now()
        with self.repository.atomic_write():
            observation = self.repository.get_requirement_observation(observation_id)
            if observation is None:
                raise ValueError(f"unknown requirement observation: {observation_id}")
            capability = self.repository.get_capability(capability_id)
            if (
                capability is None
                or capability.lifecycle not in {Lifecycle.VERIFIED, Lifecycle.RECOMMENDED}
                or capability.last_verified_at is None
            ):
                raise ValueError("requirement observations require a current verified capability")
            if observation.status == "linked":
                if observation.linked_capability_id != capability_id:
                    raise ValueError("requirement observation is already linked")
                return observation
            linked = replace(
                observation,
                status="linked",
                linked_capability_id=capability_id,
            )
            event = RequirementEvent(
                id=f"{observation_id}-linked-{hashlib.sha256(capability_id.encode()).hexdigest()[:16]}",
                observation_id=observation_id,
                event_type="implementation_linked",
                occurred_at=now,
                capability_id=capability_id,
                reason="verified implementation satisfied observed demand",
            )
            (
                self.repository._table("requirement_observations")
                .merge_insert("id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute([self.repository._requirement_observation_row(linked)])
            )
            self.repository._append_once(
                "requirement_events",
                self.repository._requirement_event_row(event),
            )
            self.repository._record_dml_unlocked()
        return linked

    def rebuild(self) -> HealthReport:
        self._ensure_healthy()
        self.health_manager.rebuild_indexes()
        return self._ensure_healthy()

    def health_check(self) -> HealthReport:
        return self.health_manager.check()

    def protocol_state(self) -> dict[str, object | None]:
        """Expose protocol metadata without treating derived rows as authority."""
        digest = None
        if self.sync_coordinator is not None:
            digest = replay(self.sync_coordinator.store.load_all()).digest
        try:
            generation = self.health_manager.check().active_generation
        except Exception:
            # Protocol metadata must remain serializable after the service closes.
            generation = None
        return {
            "sync_state": self._last_sync_report.sync_state.value if self._last_sync_report else "unchanged",
            "event_set_digest": digest,
            "generation": generation,
        }

    def status(self) -> dict[str, object | None]:
        self._assert_event_projection()
        health = self.health_manager.check()
        state = self.protocol_state()
        coordinator = self._require_sync_coordinator()
        return {
            "healthy": health.healthy,
            "failures": health.failures,
            "repository": coordinator.config.repository,
            "repository_url": coordinator.config.web_url,
            **state,
        }

    def sync(self) -> SyncReport:
        coordinator = self._require_sync_coordinator()
        self._last_sync_report = coordinator.synchronize()
        self._bind_authority_digest(self._last_sync_report.event_set_digest)
        self._assert_event_projection()
        return self._last_sync_report

    def render(self) -> object:
        coordinator = self._require_sync_coordinator()
        self._last_sync_report = coordinator.render_repository()
        self._bind_authority_digest(self._last_sync_report.event_set_digest)
        self._assert_event_projection()
        manifest = validate_render(
            coordinator.paths.checkout, self._last_sync_report.event_set_digest,
        )
        return {"sync": self._last_sync_report, "manifest": manifest}

    def open_browser(self) -> dict[str, str]:
        coordinator = self._require_sync_coordinator()
        self.render()
        runner = self.command_runner or SubprocessCommandRunner()
        completed = runner.run(("gh", "repo", "view", coordinator.config.repository, "--web"))
        if completed.returncode != 0:
            raise CapabilityMemoryBlocked(
                "browser_open_failed", "failed to open the private capability repository", ()
            )
        return {"repository": coordinator.config.repository, "url": coordinator.config.web_url}

    def resolve_conflict(self, payload: object) -> SyncReport:
        if not isinstance(payload, dict) or set(payload) != {
            "entity_type", "entity_id", "head_event_ids", "payload",
        }:
            raise ValueError("conflict resolution requires entity_type, entity_id, head_event_ids, and payload")
        if (not isinstance(payload["entity_type"], str)
                or not isinstance(payload["entity_id"], str)
                or not isinstance(payload["head_event_ids"], list)
                or not all(isinstance(item, str) for item in payload["head_event_ids"])
                or not isinstance(payload["payload"], dict)):
            raise ValueError("conflict resolution input has invalid field types")
        identity = hashlib.sha256(canonical_json(payload)).hexdigest()
        report = self._require_sync_coordinator().resolve_conflict(
            entity_type=payload["entity_type"], entity_id=payload["entity_id"],
            expected_head_ids=payload["head_event_ids"], payload=payload["payload"],
            event_id=f"resolution-{identity}", occurred_at=_utc_now(),
        )
        self._last_sync_report = report
        self._bind_authority_digest(report.event_set_digest)
        return report

    def migrate(self, legacy_data_home: Path, repository_name: str) -> object:
        coordinator = self._require_sync_coordinator()
        if repository_name != coordinator.config.repository:
            raise ValueError("migration repository does not match configured private repository")
        legacy_paths = MemoryPaths.from_codex_home(legacy_data_home)
        with CapabilityRepository.open(
            legacy_paths.database, writer_lock_path=legacy_paths.locks / "writer.lock",
        ) as legacy:
            report, self._last_sync_report = coordinator.migrate(legacy)
        self._bind_authority_digest(self._last_sync_report.event_set_digest)
        return {"migration": report, "sync": self._last_sync_report}

    def evaluate(self, candidate: Capability, inputs: ValueInputs) -> EvaluationResult:
        candidate = redact_capability(candidate)
        value = expected_net_value(inputs)
        if not math.isfinite(value) or value <= 0:
            return EvaluationResult(
                capability=None,
                expected_net_value=value,
                accepted=False,
                reasons=("expected net value must be positive",),
            )
        evaluated = replace(
            candidate,
            expected_net_value=value,
            lifecycle=next_lifecycle(
                replace(candidate, expected_net_value=value),
                (),
            ),
            updated_at=_utc_now(),
        )
        return EvaluationResult(
            capability=evaluated,
            expected_net_value=value,
            accepted=True,
            reasons=(),
        )

    def register(
        self,
        capability: Capability,
        evidence: Sequence[Evidence],
    ) -> Capability:
        capability = redact_capability(capability)
        evidence = tuple(redact_evidence(item) for item in evidence)
        self._ensure_healthy()
        if not math.isfinite(capability.expected_net_value) or capability.expected_net_value <= 0:
            raise ValueError("capability expected net value must be positive")
        if any(item.capability_id != capability.id for item in evidence):
            raise ValueError("all evidence must reference the registered capability")
        for item in evidence:
            validate_evidence(item)

        if self.sync_coordinator is not None:
            return self._register_event_first(capability, evidence)

        vector = self._embed_capability(capability)
        now = _utc_now()

        with self.repository.atomic_write():
            global_evidence = {
                item.id: item
                for item in (
                    self.repository._evidence_from_row(row)
                    for row in self.repository._rows("evidence")
                )
            }
            supplied: dict[str, Evidence] = {}
            for item in evidence:
                duplicate = supplied.get(item.id)
                if duplicate is not None and duplicate != item:
                    raise ValueError(f"conflicting duplicate evidence id: {item.id}")
                supplied[item.id] = item
                persisted = global_evidence.get(item.id)
                if persisted is not None and persisted != item:
                    raise ValueError(f"evidence id already exists: {item.id}")
            previous = self.repository.get_capability(capability.id)
            ordered_evidence = self.repository._ordered_evidence_unlocked(
                capability.id,
                tuple(evidence),
            )
            lifecycle_basis = (
                replace(capability, lifecycle=previous.lifecycle)
                if previous is not None
                else capability
            )
            stored = replace(
                capability,
                lifecycle=next_lifecycle(lifecycle_basis, ordered_evidence),
                updated_at=now,
                last_verified_at=_last_verification(ordered_evidence),
            )
            event = Event(
                id=self._next_audit_id_unlocked(
                    "registered",
                    stored.id,
                    f"{stored.source_revision}:{','.join(sorted(supplied))}",
                    table_name="events",
                ),
                capability_id=stored.id,
                event_type="registered",
                source_context=_source_context(ordered_evidence, stored.source_uri),
                occurred_at=now,
                previous_state=(
                    previous.lifecycle if previous is not None else capability.lifecycle
                ),
                resulting_state=stored.lifecycle,
                reason="capability registered with current evidence",
            )
            stored = self._upsert_capability_unlocked(stored, vector)
            for item in evidence:
                self.repository._append_once(
                    "evidence",
                    self.repository._evidence_row(item),
                )
            self._append_audit_once_unlocked("events", self.repository._event_row(event))
            self.repository._record_dml_unlocked()
        return stored

    def record_use(self, result: ReuseResult) -> Capability:
        result = redact_reuse_result(result)
        self._ensure_healthy()
        project = result.project.strip()
        if not project:
            raise ValueError("reuse project is required")
        if any(
            not math.isfinite(value) or value < 0
            for value in (result.integration_effort, result.benefit)
        ):
            raise ValueError("reuse economics must be finite non-negative values")

        if self.sync_coordinator is not None:
            return self._record_use_event_first(result, project)

        now = _utc_now()
        with self.repository.atomic_write():
            capability = self.repository.get_capability(result.capability_id)
            if capability is None:
                raise ValueError(f"unknown capability: {result.capability_id}")
            existing_evidence = self.repository._ordered_evidence_unlocked(
                capability.id
            )
            for item in existing_evidence:
                validate_evidence(item)
            if not source_available(capability, existing_evidence):
                raise ValueError(f"capability source is unavailable: {capability.source_uri}")

            outcome = Evidence(
                id=self._next_audit_id_unlocked(
                    "reuse",
                    capability.id,
                    f"{project}:{result.succeeded}:{result.integration_effort}:{result.benefit}",
                    table_name="evidence",
                ),
                capability_id=capability.id,
                source_project=project,
                evidence_type="reuse",
                outcome="success" if result.succeeded else "failed",
                metric_name=None,
                metric_value=None,
                confidence=1.0,
                observed_at=now,
                supporting_uri=None,
                integration_effort=result.integration_effort,
                benefit=result.benefit,
                failure_risk=0.0 if result.succeeded else 1.0,
            )
            evidence = self.repository._ordered_evidence_unlocked(
                capability.id,
                (outcome,),
            )
            net_value = _evidence_net_value(evidence)
            if not math.isfinite(net_value):
                raise ValueError("aggregate evidence economics must be finite")
            provisional = replace(
                capability,
                expected_net_value=net_value,
                updated_at=now,
            )
            updated = replace(
                provisional,
                lifecycle=next_lifecycle(provisional, evidence),
            )
            event = Event(
                id=outcome.id.replace("reuse-", "reused-", 1),
                capability_id=updated.id,
                event_type="reused",
                source_context=project,
                occurred_at=now,
                previous_state=capability.lifecycle,
                resulting_state=updated.lifecycle,
                reason=(
                    "independent reuse succeeded"
                    if result.succeeded
                    and capability.lifecycle is not Lifecycle.RECOMMENDED
                    and updated.lifecycle is Lifecycle.RECOMMENDED
                    else (
                        "reuse succeeded"
                        if result.succeeded
                        else result.failure_reason or "reuse failed"
                    )
                ),
            )
            vector = self._stored_vector_unlocked(capability.id)
            updated = self._upsert_capability_unlocked(updated, vector)
            self._append_audit_once_unlocked(
                "evidence",
                self.repository._evidence_row(outcome),
            )
            self._append_audit_once_unlocked("events", self.repository._event_row(event))
            self.repository._record_dml_unlocked()
        return updated

    def _embed_capability(self, capability: Capability) -> list[float]:
        document = str(
            self.repository._capability_row(
                capability,
                [0.0] * EMBEDDING_DIMENSION,
            )["search_text"]
        )
        vectors = self._embed_documents((document,))
        if len(vectors) != 1:
            raise ValueError("embedding provider must return exactly one vector")
        vector = [float(value) for value in vectors[0]]
        if len(vector) != EMBEDDING_DIMENSION or any(not math.isfinite(value) for value in vector):
            raise ValueError(f"embedding must contain {EMBEDDING_DIMENSION} finite values")
        return vector

    def _require_sync_coordinator(self) -> SyncCoordinator:
        if self.sync_coordinator is None:
            raise CapabilityMemoryBlocked(
                "event_authority_unavailable",
                "events-v1 authority requires a configured synchronization coordinator",
                (),
            )
        return self.sync_coordinator

    def _assert_event_projection(self, result: ReplayResult | None = None) -> None:
        coordinator = self._require_sync_coordinator()
        current = result or replay(coordinator.store.load_all())
        comparison = compare_projection(current, coordinator.repository)
        if not comparison.equivalent:
            raise CapabilityMemoryBlocked(
                "projection_mismatch", "local projection does not match events-v1 authority",
                comparison.differences,
            )

    def _event(
        self, entity_type: str, entity_id: str, operation: str,
        payload: object, current: ReplayResult,
    ) -> AuthorityEvent:
        coordinator = self._require_sync_coordinator()
        parents = current.heads.get((entity_type, entity_id), ())
        plain = json.loads(json.dumps(payload, ensure_ascii=False))
        identity = hashlib.sha256(canonical_json([
            coordinator.config.device_id, entity_type, entity_id, operation, list(parents), plain,
        ])).hexdigest()
        return AuthorityEvent.create(
            event_id=f"evt-{identity}", device_id=coordinator.config.device_id,
            entity_type=entity_type, entity_id=entity_id, operation=operation,
            parent_event_ids=parents, occurred_at=_utc_now(), payload=plain,
        )

    def _commit_event(self, entity_type: str, entity_id: str, operation: str, payload: object) -> SyncReport:
        return self._commit_events(((entity_type, entity_id, operation, payload),))

    def _commit_events(
        self, specifications: Sequence[tuple[str, str, str, object]],
    ) -> SyncReport:
        coordinator = self._require_sync_coordinator()
        try:
            report = coordinator.mutate_batch(
                lambda current: tuple(
                    self._event(entity_type, entity_id, operation, payload, current)
                    for entity_type, entity_id, operation, payload in specifications
                )
            )
        except SyncBlocked as error:
            if error.sync_state is not None and error.event_set_digest and error.commit_id:
                self._last_sync_report = SyncReport(
                    error.sync_state, error.commit_id, error.event_set_digest,
                    coordinator.config.last_checked_remote_head,
                )
            raise
        self._last_sync_report = report
        self._bind_authority_digest(report.event_set_digest)
        return report

    def _bind_authority_digest(self, digest: str) -> None:
        binder = getattr(self.health_manager, "bind_authority_digest", None)
        if callable(binder):
            binder(digest)

    def _register_event_first(self, capability: Capability, evidence: Sequence[Evidence]) -> Capability:
        existing = self.repository.get_capability(capability.id)
        known = tuple(self.repository.list_evidence(capability.id))
        by_id = {item.id: item for item in known}
        for item in evidence:
            if item.id in by_id and by_id[item.id] != item:
                raise ValueError(f"evidence id already exists: {item.id}")
        ordered = tuple(sorted({item.id: item for item in (*known, *evidence)}.values(), key=lambda x: (x.observed_at, x.id)))
        basis = replace(capability, lifecycle=existing.lifecycle) if existing else capability
        now = _utc_now()
        stored = replace(capability, lifecycle=next_lifecycle(basis, ordered), updated_at=now,
                         last_verified_at=_last_verification(ordered),
                         created_at=existing.created_at if existing else capability.created_at)
        operation = "updated" if existing else "registered"
        self._commit_events((
            *(("evidence", item.id, "observed", asdict(item))
              for item in evidence if item.id not in by_id),
            ("capability", stored.id, operation, asdict(stored)),
        ))
        projected = self.repository.get_capability(stored.id)
        if projected is None:
            raise CapabilityMemoryBlocked("projection_failed", "capability projection is missing", ())
        return projected

    def _record_use_event_first(self, result: ReuseResult, project: str) -> Capability:
        capability = self.repository.get_capability(result.capability_id)
        if capability is None:
            raise ValueError(f"unknown capability: {result.capability_id}")
        existing = self.repository.list_evidence(capability.id)
        if not source_available(capability, existing):
            raise ValueError(f"capability source is unavailable: {capability.source_uri}")
        now = _utc_now()
        digest = hashlib.sha256(canonical_json([
            capability.id, project, result.succeeded, result.integration_effort, result.benefit,
            now,
        ])).hexdigest()[:32]
        outcome = Evidence(
            id=f"reuse-{digest}", capability_id=capability.id, source_project=project,
            evidence_type="reuse", outcome="success" if result.succeeded else "failed",
            metric_name=None, metric_value=None, confidence=1.0, observed_at=now,
            supporting_uri=None, integration_effort=result.integration_effort,
            benefit=result.benefit, failure_risk=0.0 if result.succeeded else 1.0,
        )
        updated = replace(capability, lifecycle=next_lifecycle(capability, (*existing, outcome)),
                          expected_net_value=_evidence_net_value((*existing, outcome)),
                          updated_at=now)
        self._commit_events((
            ("reuse_outcome", outcome.id, "recorded", asdict(outcome)),
            ("capability", updated.id, "updated", asdict(updated)),
        ))
        projected = self.repository.get_capability(updated.id)
        if projected is None:
            raise CapabilityMemoryBlocked("projection_failed", "reuse projection is missing", ())
        return projected

    def _observe_demand_event_first(
        self, observation_id: str, requirement: RequirementProfile,
    ) -> RequirementObservation:
        existing = self.repository.get_requirement_observation(observation_id)
        if existing is not None:
            return existing
        now = _utc_now()
        observation = RequirementObservation(observation_id, requirement, "unmet", now)
        event = RequirementEvent(
            id=f"{observation_id}-observed", observation_id=observation_id,
            event_type="unmet_observed", occurred_at=now,
            reason="complete capability search produced a build decision",
        )
        self._commit_event("demand", observation_id, "observed",
                           {"observation": asdict(observation), "events": [asdict(event)]})
        return self.repository.get_requirement_observation(observation_id) or observation

    def _link_demand_event_first(self, observation_id: str, capability_id: str) -> RequirementObservation:
        observation = self.repository.get_requirement_observation(observation_id)
        capability = self.repository.get_capability(capability_id)
        if observation is None:
            raise ValueError(f"unknown requirement observation: {observation_id}")
        if capability is None or capability.lifecycle not in {Lifecycle.VERIFIED, Lifecycle.RECOMMENDED} or capability.last_verified_at is None:
            raise ValueError("requirement observations require a current verified capability")
        if observation.status == "linked":
            if observation.linked_capability_id != capability_id:
                raise ValueError("requirement observation is already linked")
            return observation
        linked = replace(observation, status="linked", linked_capability_id=capability_id)
        history = list(self.repository.list_requirement_events(observation_id))
        now = _utc_now()
        history.append(RequirementEvent(
            id=f"{observation_id}-linked-{hashlib.sha256(capability_id.encode()).hexdigest()[:16]}",
            observation_id=observation_id, event_type="implementation_linked",
            occurred_at=now, capability_id=capability_id,
            reason="verified implementation satisfied observed demand",
        ))
        self._commit_event("demand", observation_id, "linked",
                           {"observation": asdict(linked), "events": [asdict(x) for x in history]})
        return self.repository.get_requirement_observation(observation_id) or linked

    def _embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        try:
            return self.embedding_provider.embed_documents(documents)
        except Exception as error:
            failure = _blocked(
                "embedding_unavailable",
                "document embedding failed",
                (redact_text(str(error)),),
            )
        # Leave the handler before raising so callers cannot inspect a raw
        # provider error through the otherwise suppressed __context__ chain.
        raise failure from None

    def _ensure_healthy(self) -> HealthReport:
        report = self.health_manager.ensure_healthy()
        if report.healthy:
            return report
        code = report.failures[0].partition(":")[0] if report.failures else "health_unavailable"
        raise CapabilityMemoryBlocked(
            code,
            "capability memory remains unhealthy after repair",
            report.failures,
        )

    def _discover_and_persist(self, context: DiscoveryContext) -> DiscoveryResult:
        metadata = None
        if isinstance(self.discovery_engine, CapabilityDiscovery):
            discovered, metadata = self.discovery_engine.discover_staged(context)
        else:
            discovered = self.discovery_engine.discover(context)
        discovered = replace(
            discovered,
            capabilities=tuple(redact_capability(item) for item in discovered.capabilities),
            sources_scanned=tuple(redact_uri(source) for source in discovered.sources_scanned),
        )
        if self.sync_coordinator is not None:
            return self._persist_discovery_events(discovered, context, metadata)
        documents = tuple(
            str(
                self.repository._capability_row(
                    capability,
                    [0.0] * EMBEDDING_DIMENSION,
                )["search_text"]
            )
            for capability in discovered.capabilities
        )
        vectors = self._embed_documents(documents)
        if len(vectors) != len(discovered.capabilities):
            raise ValueError("embedding provider returned the wrong document count")
        materialized = tuple(_validated_vector(vector) for vector in vectors)

        stored: list[Capability] = []
        discovered_ids = {capability.id for capability in discovered.capabilities}
        refresh_roots = _refresh_roots(context, discovered.sources_scanned)
        mutated = False
        with self.repository.atomic_write():
            for capability, vector in zip(
                discovered.capabilities,
                materialized,
                strict=True,
            ):
                existing = self.repository.get_capability(capability.id)
                if (
                    existing is not None
                    and existing.content_hash == capability.content_hash
                    and existing.source_revision == capability.source_revision
                ):
                    stored.append(existing)
                    continue
                elif existing is not None:
                    capability = self._invalidate_unlocked(
                        replace(
                            capability,
                            confidence=existing.confidence,
                            expected_net_value=existing.expected_net_value,
                            embedding_generation=existing.embedding_generation,
                            last_verified_at=existing.last_verified_at,
                        ),
                        existing,
                        evidence_type="content_hash",
                        supporting_uri=capability.source_uri,
                        identity=capability.content_hash,
                        reason="discovered source content changed",
                    )
                stored.append(self._upsert_capability_unlocked(capability, vector))
                mutated = True

            for existing in self.repository.list_capabilities():
                if existing.id in discovered_ids or not _missing_scanned_source(
                    existing,
                    refresh_roots,
                ):
                    continue
                invalidated = self._invalidate_unlocked(
                    existing,
                    existing,
                    evidence_type="source_availability",
                    supporting_uri=existing.source_uri,
                    identity="unavailable",
                    reason="discovered source is unavailable",
                )
                self._upsert_capability_unlocked(
                    invalidated,
                    self._stored_vector_unlocked(existing.id),
                )
                mutated = True
            if mutated:
                self.repository._record_dml_unlocked()
        return DiscoveryResult(
            capabilities=tuple(stored),
            sources_scanned=discovered.sources_scanned,
        )

    def _persist_discovery_events(
        self, discovered: DiscoveryResult, context: DiscoveryContext,
        metadata: tuple[str, object] | None = None,
    ) -> DiscoveryResult:
        stored: list[Capability] = []
        specifications = []
        if metadata is not None:
            key, value = metadata
            if self.repository.get_metadata(key) != value:
                specifications.append(("metadata", key, "set", {"value": value}))
        discovered_ids = {item.id for item in discovered.capabilities}
        refresh_roots = _refresh_roots(context, discovered.sources_scanned)
        for capability in discovered.capabilities:
            existing = self.repository.get_capability(capability.id)
            if (existing is not None and existing.content_hash == capability.content_hash
                    and existing.source_revision == capability.source_revision):
                stored.append(existing)
                continue
            candidate = capability
            if existing is not None:
                candidate = replace(
                    capability, lifecycle=Lifecycle.DEGRADED,
                    confidence=existing.confidence,
                    expected_net_value=existing.expected_net_value,
                    embedding_generation=existing.embedding_generation,
                    created_at=existing.created_at, last_verified_at=None,
                )
            specifications.append((
                "capability", candidate.id, "updated" if existing else "registered", asdict(candidate)
            ))
            stored.append(candidate)
        for existing in self.repository.list_capabilities():
            if existing.id in discovered_ids or not _missing_scanned_source(existing, refresh_roots):
                continue
            degraded = replace(existing, lifecycle=Lifecycle.DEGRADED,
                               updated_at=_utc_now(), last_verified_at=None)
            specifications.append(("capability", degraded.id, "updated", asdict(degraded)))
        if specifications:
            self._commit_events(specifications)
        return DiscoveryResult(tuple(self.repository.get_capability(item.id) or item for item in stored), discovered.sources_scanned)

    def _invalidate_unlocked(
        self,
        candidate: Capability,
        previous: Capability,
        *,
        evidence_type: str,
        supporting_uri: str,
        identity: str,
        reason: str,
    ) -> Capability:
        now = _utc_now()
        prior_invalidations = [
            item
            for item in self.repository.list_evidence(candidate.id)
            if item.evidence_type == evidence_type
            and item.supporting_uri == supporting_uri
            and item.outcome
            == ("unavailable" if evidence_type == "source_availability" else "changed")
        ]
        if (
            previous.lifecycle is Lifecycle.DEGRADED
            and prior_invalidations
            and evidence_type == "source_availability"
        ):
            return previous
        invalidation_id = self._next_audit_id_unlocked(
            "invalidation",
            candidate.id,
            f"{evidence_type}:{identity}:{candidate.source_revision}",
            table_name="evidence",
        )
        invalidation = Evidence(
            id=invalidation_id,
            capability_id=candidate.id,
            source_project=str(self._project_root or "capability-memory"),
            evidence_type=evidence_type,
            outcome="unavailable" if evidence_type == "source_availability" else "changed",
            metric_name=None,
            metric_value=None,
            confidence=1.0,
            observed_at=now,
            supporting_uri=supporting_uri,
        )
        evidence = self.repository._ordered_evidence_unlocked(
            candidate.id,
            (invalidation,),
        )
        lifecycle_basis = replace(candidate, lifecycle=previous.lifecycle)
        updated = replace(
            candidate,
            lifecycle=next_lifecycle(lifecycle_basis, evidence),
            updated_at=now,
            last_verified_at=None,
        )
        self._append_audit_once_unlocked(
            "evidence",
            self.repository._evidence_row(invalidation),
        )
        if previous.lifecycle is not updated.lifecycle:
            event = Event(
                id=invalidation_id.replace("invalidation-", "invalidated-", 1),
                capability_id=updated.id,
                event_type="invalidated",
                source_context=invalidation.source_project,
                occurred_at=now,
                previous_state=previous.lifecycle,
                resulting_state=updated.lifecycle,
                reason=reason,
            )
            self._append_audit_once_unlocked("events", self.repository._event_row(event))
        return updated

    def _next_audit_id_unlocked(
        self,
        operation: str,
        capability_id: str,
        identity: str,
        *,
        table_name: str,
    ) -> str:
        operation = redact_text(operation)
        capability_id = redact_identifier(capability_id)
        identity = redact_text(identity)
        rows = self.repository._rows(table_name)
        epoch = 1 + sum(
            row["capability_id"] == capability_id
            and str(row["id"]).startswith(f"{operation}-")
            for row in rows
        )
        digest = hashlib.sha256(
            f"{operation}:{capability_id}:{epoch}:{identity}".encode("utf-8")
        ).hexdigest()[:24]
        return f"{operation}-{digest}"

    def _append_audit_once_unlocked(
        self,
        table_name: str,
        row: dict[str, object],
    ) -> None:
        for existing in self.repository._rows(table_name):
            if existing["id"] != row["id"]:
                continue
            if existing != row:
                raise ValueError(f"global audit id collision: {row['id']}")
            return
        self.repository._append_once(table_name, row)

    def _upsert_capability_unlocked(
        self,
        capability: Capability,
        vector: Sequence[float],
    ) -> Capability:
        capability = redact_capability(capability)
        validate_category_path(capability.category_path)
        existing = self.repository.get_capability(capability.id)
        if existing is not None:
            capability = replace(capability, created_at=existing.created_at)
        (
            self.repository._table("capabilities")
            .merge_insert("id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute([self.repository._capability_row(capability, vector)])
        )
        return capability

    def _stored_vector_unlocked(self, capability_id: str) -> list[float]:
        for row in self.repository._rows("capabilities"):
            if row["id"] == capability_id:
                return [float(value) for value in row["vector"]]
        raise ValueError(f"unknown capability: {capability_id}")


def _last_verification(evidence: Sequence[Evidence]) -> str | None:
    verified = [
        item.observed_at
        for item in evidence
        if item.evidence_type.strip().casefold().replace("-", "_") == "verification"
        and item.outcome.strip().casefold() in {"passed", "pass", "success", "successful"}
    ]
    return verified[-1] if verified else None


def _source_context(evidence: Sequence[Evidence], fallback: str) -> str:
    return evidence[-1].source_project if evidence else fallback


def _evidence_net_value(evidence: Sequence[Evidence]) -> float:
    return sum(
        item.benefit - item.integration_effort - item.failure_risk
        for item in evidence
    )


def _refresh_roots(
    context: DiscoveryContext,
    sources_scanned: Sequence[str],
) -> tuple[Path, ...]:
    roots = {
        context.project_root.expanduser().absolute(),
        context.codex_home.expanduser().absolute() / "plugins" / "cache",
        context.codex_home.expanduser().absolute() / "skills",
    }
    roots.update(Path(source).expanduser().absolute() for source in sources_scanned)
    return tuple(sorted(roots, key=str))


def _missing_scanned_source(capability: Capability, roots: Sequence[Path]) -> bool:
    resolved = resolve_source(capability.source_uri)
    if resolved is None or resolved.kind != "local" or resolved.path is None:
        return False
    absolute = resolved.path
    if not any(absolute == root or absolute.is_relative_to(root) for root in roots):
        return False
    return not source_available(capability, ())


def _blocked(
    code: str,
    message: str,
    attempts: Sequence[str],
) -> CapabilityMemoryBlocked:
    return CapabilityMemoryBlocked(
        redact_text(code),
        message,
        tuple(redact_text(attempt) for attempt in attempts),
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _validated_vector(vector: Sequence[float]) -> list[float]:
    materialized = [float(value) for value in vector]
    if len(materialized) != EMBEDDING_DIMENSION or any(
        not math.isfinite(value) for value in materialized
    ):
        raise ValueError(f"embedding must contain {EMBEDDING_DIMENSION} finite values")
    return materialized
