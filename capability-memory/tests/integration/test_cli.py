from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import KeywordEmbeddingProvider

from supermind_memory.cli import build_service, main
from supermind_memory.sync import SyncBlocked, SyncState
from supermind_memory.types import (
    ArtifactType,
    Capability,
    CapabilityMemoryBlocked,
    CandidateMatch,
    DiscoveryResult,
    EvaluationResult,
    HealthReport,
    Lifecycle,
    RequirementObservation,
    RequirementProfile,
    SearchResult,
    SearchStatus,
)


NOW = "2026-09-04T00:00:00+00:00"


def capability() -> Capability:
    return Capability(
        id="auth.oauth-login",
        name="OAuth login",
        summary="Reusable OAuth login",
        category_path=("Code and components", "Authentication"),
        facets=("oauth",),
        contract="login(input) -> session",
        constraints=("OAuth 2.1",),
        artifact_type=ArtifactType.CODE,
        source_uri=Path(__file__).as_uri(),
        source_revision="abc123",
        content_hash="deadbeef",
        owner="platform",
        license="MIT",
        stack=("python",),
        runtime=("python3",),
        platform=("linux",),
        dependencies=(),
        compatibility=("python3",),
        lifecycle=Lifecycle.VERIFIED,
        confidence=0.9,
        expected_net_value=8.0,
        embedding_generation="generation-1",
        created_at=NOW,
        updated_at=NOW,
        last_verified_at=NOW,
    )


class FakeRepository:
    def list_capabilities(self):
        return (capability(),)

    def get_capability(self, capability_id):
        return capability() if capability_id == capability().id else None

    def list_evidence(self, capability_id):
        return ()

    def list_relationships(self, capability_id):
        return ()


class FakeMemory:
    def __init__(self) -> None:
        self.repository = FakeRepository()
        self.closed = False
        self.calls: list[tuple[str, object]] = []

    def close(self) -> None:
        self.closed = True

    def initialize(self, project_root):
        self.calls.append(("init", project_root))
        return HealthReport(True, "generation-1", (), NOW)

    def discover(self, context):
        self.calls.append(("discover", context))
        return DiscoveryResult((capability(),), (str(context.project_root),))

    def refresh_sources(self, project_root):
        self.calls.append(("refresh-sources", project_root))
        return DiscoveryResult((capability(),), (str(project_root),))

    def get(self, capability_id):
        return self.repository.get_capability(capability_id)

    def search(self, requirement):
        self.calls.append(("search", requirement))
        return SearchResult(
            SearchStatus.COMPLETE,
            (
                CandidateMatch(
                    capability_id=capability().id,
                    vector_score=0.9,
                    lexical_score=0.8,
                    contract_fit=1.0,
                    requirement_fit=0.9,
                    reliability=0.9,
                    historical_benefit=0.8,
                    integration_cost=0.1,
                    maintenance_risk=0.1,
                    reuse_score=0.85,
                ),
            ),
            "generation-1",
        )

    def evaluate(self, candidate, inputs):
        self.calls.append(("evaluate", (candidate, inputs)))
        return EvaluationResult(replace(candidate, expected_net_value=8.0), 8.0, True, ())

    def register(self, item, evidence):
        self.calls.append(("register", (item, evidence)))
        return item

    def record_use(self, result):
        self.calls.append(("record-use", result))
        return replace(capability(), lifecycle=Lifecycle.RECOMMENDED)

    def rebuild(self):
        self.calls.append(("rebuild", None))
        return HealthReport(True, "generation-2", ("rebuild_search_indexes",), NOW)

    def health_check(self):
        self.calls.append(("health", None))
        return HealthReport(True, "generation-1", (), NOW)

    def status(self):
        self.calls.append(("status", None))
        return {"healthy": True}

    def sync(self):
        self.calls.append(("sync", None))
        return {"sync_state": "synced"}

    def render(self):
        self.calls.append(("render", None))
        return {"rendered": True}

    def open_browser(self):
        self.calls.append(("open", None))
        return {"url": "https://github.com/owner/memory"}

    def migrate(self, legacy_data_home, repository):
        self.calls.append(("migrate", (legacy_data_home, repository)))
        return {"equivalent": True}

    def resolve_conflict(self, payload):
        self.calls.append(("resolve-conflict", payload))
        return {"sync_state": "synced"}


def run_cli(arguments, memory):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(arguments, service_factory=lambda: memory)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def write_json(tmp_path: Path, name: str, payload: object) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_status_uses_versioned_protocol():
    memory = FakeMemory()

    exit_code, stdout, stderr = run_cli(["status", "--format", "json"], memory)

    response = json.loads(stdout)
    assert exit_code == 0
    assert stderr == ""
    assert response.keys() >= {
        "protocol_version", "operation_id", "status", "sync_state",
        "event_set_digest", "generation", "result",
    }
    assert response["protocol_version"] == 1


def test_projection_failure_reports_committed_event_state_in_protocol(tmp_path):
    class ProjectionFailureMemory(FakeMemory):
        def register(self, item, evidence):
            raise SyncBlocked(
                "projection_failed", ("projector unavailable",),
                sync_state=SyncState.COMMITTED, event_set_digest="a" * 64,
                commit_id="b" * 40,
            )

        def protocol_state(self):
            return {
                "sync_state": "committed", "event_set_digest": "a" * 64,
                "generation": "generation-1",
            }

    input_path = write_json(
        tmp_path, "registration-projection-failure.json",
        {"capability": capability_json(), "evidence": []},
    )

    exit_code, stdout, stderr = run_cli(
        ["register", "--input", input_path, "--format", "json"],
        ProjectionFailureMemory(),
    )

    response = json.loads(stderr)
    assert exit_code == 3
    assert stdout == ""
    assert response["code"] == "projection_failed"
    assert response["sync_state"] == "committed"
    assert response["event_set_digest"] == "a" * 64


def test_repository_commands_use_versioned_json_dispatch(tmp_path):
    memory = FakeMemory()
    resolution = write_json(tmp_path, "resolution.json", {
        "entity_type": "capability", "entity_id": "auth.login",
        "head_event_ids": ["head-a", "head-b"], "payload": capability_json(),
    })
    commands = (
        ["status", "--format", "json"], ["sync", "--format", "json"],
        ["render", "--format", "json"], ["open", "--format", "json"],
        ["migrate", "--repo", "owner/memory", "--legacy-data-home", str(tmp_path), "--format", "json"],
        ["resolve-conflict", "--input", resolution, "--format", "json"],
    )

    for command in commands:
        code, stdout, stderr = run_cli(command, memory)
        assert code == 0, command
        assert stderr == ""
        assert json.loads(stdout)["protocol_version"] == 1

    assert [name for name, _ in memory.calls] == [
        "status", "sync", "render", "open", "migrate", "resolve-conflict",
    ]




def capability_json() -> dict[str, object]:
    item = capability()
    return {
        "id": item.id,
        "name": item.name,
        "summary": item.summary,
        "category_path": list(item.category_path),
        "facets": list(item.facets),
        "contract": item.contract,
        "constraints": list(item.constraints),
        "artifact_type": item.artifact_type.value,
        "source_uri": item.source_uri,
        "source_revision": item.source_revision,
        "content_hash": item.content_hash,
        "owner": item.owner,
        "license": item.license,
        "stack": list(item.stack),
        "runtime": list(item.runtime),
        "platform": list(item.platform),
        "dependencies": [],
        "compatibility": list(item.compatibility),
        "lifecycle": item.lifecycle.value,
        "confidence": item.confidence,
        "expected_net_value": item.expected_net_value,
        "embedding_generation": item.embedding_generation,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "last_verified_at": item.last_verified_at,
    }


def test_search_emits_typed_json_and_closes_service(tmp_path):
    requirement = write_json(
        tmp_path,
        "requirement.json",
        {"id": "req-1", "project_id": "project-a", "intent": "OAuth login"},
    )
    memory = FakeMemory()

    exit_code, stdout, stderr = run_cli(["search", "--input", requirement], memory)

    assert exit_code == 0
    assert stderr == ""
    assert json.loads(stdout) == {
        "error_code": None,
        "error_message": None,
        "generation": "generation-1",
        "matches": [
            {
                "capability_id": "auth.oauth-login",
                "contract_fit": 1.0,
                "historical_benefit": 0.8,
                "integration_cost": 0.1,
                "lexical_score": 0.8,
                "maintenance_risk": 0.1,
                "rejection_reasons": [],
                "reliability": 0.9,
                "requirement_fit": 0.9,
                "reuse_score": 0.85,
                "vector_score": 0.9,
            }
        ],
        "status": "complete",
    }
    assert memory.closed is True


def test_search_markdown_uses_explorer(tmp_path):
    requirement = write_json(
        tmp_path,
        "requirement.json",
        {"id": "req-1", "project_id": "project-a", "intent": "OAuth login"},
    )
    exit_code, stdout, stderr = run_cli(
        ["search", "--input", requirement, "--format", "markdown"], FakeMemory()
    )

    assert exit_code == 0
    assert stderr == ""
    assert stdout.startswith("# Reuse decision\n")
    assert "auth.oauth-login" in stdout


def test_all_mutating_commands_decode_typed_inputs(tmp_path):
    memory = FakeMemory()
    project = tmp_path / "project"
    project.mkdir()
    discovery = write_json(
        tmp_path,
        "discovery.json",
        {"project_root": str(project), "codex_home": str(tmp_path / "codex")},
    )
    evaluation = write_json(
        tmp_path,
        "evaluation.json",
        {
            "capability": capability_json(),
            "inputs": {
                "expected_reuse_count": 3,
                "benefit_per_reuse": 5,
                "extraction_cost": 1,
                "integration_cost": 1,
                "verification_cost": 1,
                "maintenance_cost": 1,
                "failure_risk": 0.1,
            },
        },
    )
    registration = write_json(
        tmp_path,
        "registration.json",
        {"capability": capability_json(), "evidence": []},
    )
    reuse = write_json(
        tmp_path,
        "reuse.json",
        {
            "capability_id": capability().id,
            "project": "project-b",
            "succeeded": True,
            "integration_effort": 1,
            "benefit": 5,
        },
    )

    invocations = (
        ["init", "--repo", "owner/memory", "--project-root", str(project)],
        ["discover", "--input", discovery],
        ["evaluate", "--input", evaluation],
        ["register", "--input", registration],
        ["record-use", "--input", reuse],
        ["rebuild"],
        ["health"],
    )
    for invocation in invocations:
        exit_code, stdout, stderr = run_cli(invocation, memory)
        assert exit_code == 0, invocation
        assert json.loads(stdout), invocation
        assert stderr == "", invocation

    assert [name for name, _ in memory.calls] == [
        "init",
        "discover",
        "evaluate",
        "register",
        "record-use",
        "rebuild",
        "health",
    ]


def test_workflow_commands_decode_inputs_and_apply_mandatory_hooks(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    requirement = write_json(
        tmp_path,
        "workflow-requirement.json",
        {"id": "req-1", "project_id": "project-a", "intent": "OAuth login"},
    )
    implementation = write_json(
        tmp_path,
        "workflow-implementation.json",
        {
            "capability": capability_json(),
            "inputs": {
                "expected_reuse_count": 3,
                "benefit_per_reuse": 5,
                "extraction_cost": 1,
                "integration_cost": 1,
                "verification_cost": 1,
                "maintenance_cost": 1,
                "failure_risk": 0.1,
            },
            "evidence": [
                {
                    "id": "verification-project-a",
                    "capability_id": capability().id,
                    "source_project": "project-a",
                    "evidence_type": "verification",
                    "outcome": "passed",
                    "metric_name": None,
                    "metric_value": None,
                    "confidence": 1,
                    "observed_at": NOW,
                    "supporting_uri": None,
                }
            ],
        },
    )
    reuse = write_json(
        tmp_path,
        "workflow-reuse.json",
        {
            "capability_id": capability().id,
            "project": "project-b",
            "succeeded": True,
            "integration_effort": 1,
            "benefit": 5,
        },
    )

    design_memory = FakeMemory()
    design_code, design_stdout, design_stderr = run_cli(
        [
            "begin-design",
            "--project-root",
            str(project),
            "--input",
            requirement,
        ],
        design_memory,
    )
    implementation_memory = FakeMemory()
    implementation_code, implementation_stdout, implementation_stderr = run_cli(
        ["complete-implementation", "--input", implementation],
        implementation_memory,
    )
    reuse_memory = FakeMemory()
    reuse_code, reuse_stdout, reuse_stderr = run_cli(
        ["complete-reuse", "--input", reuse],
        reuse_memory,
    )

    assert (design_code, implementation_code, reuse_code) == (0, 0, 0)
    assert design_stderr == implementation_stderr == reuse_stderr == ""
    design = json.loads(design_stdout)
    assert design["action"] == "reuse"
    assert design["selected_capability_id"] == capability().id
    assert [name for name, _ in design_memory.calls] == [
        "init",
        "refresh-sources",
        "health",
        "search",
    ]
    assert json.loads(implementation_stdout)["id"] == capability().id
    assert [name for name, _ in implementation_memory.calls] == ["evaluate", "register"]
    assert json.loads(reuse_stdout)["lifecycle"] == "recommended"
    assert [name for name, _ in reuse_memory.calls] == ["record-use"]


def test_begin_design_failed_retrieval_returns_blocked_without_a_build_decision(tmp_path):
    class FailedSearchMemory(FakeMemory):
        def search(self, requirement):
            self.calls.append(("search", requirement))
            return SearchResult(
                status=SearchStatus.FAILED,
                matches=(),
                error_code="fts_unavailable",
                error_message="text index repair failed",
            )

    project = tmp_path / "project"
    project.mkdir()
    requirement = write_json(
        tmp_path,
        "failed-workflow-requirement.json",
        {"id": "req-1", "project_id": "project-a", "intent": "OAuth login"},
    )
    memory = FailedSearchMemory()

    exit_code, stdout, stderr = run_cli(
        [
            "begin-design",
            "--project-root",
            str(project),
            "--input",
            requirement,
        ],
        memory,
    )

    assert exit_code == 3
    assert stdout == ""
    assert json.loads(stderr) == {
        "attempts": ["text index repair failed"],
        "code": "fts_unavailable",
        "message": "text index repair failed",
        "status": "blocked",
    }
    assert [name for name, _ in memory.calls] == [
        "init",
        "refresh-sources",
        "health",
        "search",
    ]


def test_begin_design_cli_reports_partial_contract_fit_as_adapt(tmp_path):
    class PartialFitMemory(FakeMemory):
        def search(self, requirement):
            result = super().search(requirement)
            return replace(
                result,
                matches=(replace(result.matches[0], contract_fit=0.5),),
            )

    project = tmp_path / "project"
    project.mkdir()
    requirement = write_json(
        tmp_path,
        "partial-contract-requirement.json",
        {
            "id": "req-partial",
            "project_id": "project-a",
            "intent": "OAuth login with refresh",
            "contract": "OAuth callback creates a session and refreshes access tokens",
        },
    )

    exit_code, stdout, stderr = run_cli(
        [
            "begin-design",
            "--project-root",
            str(project),
            "--input",
            requirement,
        ],
        PartialFitMemory(),
    )

    decision = json.loads(stdout)
    assert exit_code == 0
    assert stderr == ""
    assert decision["action"] == "adapt"
    assert decision["selected_capability_id"] == capability().id
    assert any("partial contract fit (0.50)" in reason for reason in decision["rationale"])


def test_inspect_supports_all_explorer_views(tmp_path):
    memory = FakeMemory()
    requirement_and_result = write_json(
        tmp_path,
        "decision.json",
        {
            "requirement": {"id": "req-1", "project_id": "project-a", "intent": "login"},
            "result": {
                "status": "complete",
                "matches": [],
                "generation": "generation-1",
            },
        },
    )

    commands = (
        ["inspect", "--view", "overview", "--format", "markdown"],
        ["inspect", "--view", "table", "--format", "markdown"],
        ["inspect", "--view", "detail", "--capability-id", capability().id, "--format", "markdown"],
        ["inspect", "--view", "graph", "--format", "markdown"],
        [
            "inspect",
            "--view",
            "decision",
            "--input",
            requirement_and_result,
            "--format",
            "markdown",
        ],
    )
    for command in commands:
        exit_code, stdout, stderr = run_cli(command, memory)
        assert exit_code == 0, command
        assert stdout, command
        assert stderr == "", command


def test_cli_lists_unmet_demand_and_links_it_to_verified_capability(tmp_path):
    observation = RequirementObservation(
        id="requirement-observed",
        requirement=RequirementProfile(
            "req-1",
            "project-a",
            "OAuth login",
            runtime=("Python 3.12",),
        ),
        status="unmet",
        observed_at=NOW,
    )

    class DemandMemory(FakeMemory):
        def list_requirement_observations(self):
            return (observation,)

        def link_requirement_observation(self, observation_id, capability_id):
            assert (observation_id, capability_id) == (
                observation.id,
                capability().id,
            )
            return replace(
                observation,
                status="linked",
                linked_capability_id=capability_id,
            )

    link = write_json(
        tmp_path,
        "link-demand.json",
        {
            "observation_id": observation.id,
            "capability_id": capability().id,
        },
    )

    list_code, list_stdout, list_stderr = run_cli(["list-demands"], DemandMemory())
    link_code, link_stdout, link_stderr = run_cli(
        ["link-demand", "--input", link], DemandMemory()
    )

    assert (list_code, link_code) == (0, 0)
    assert list_stderr == link_stderr == ""
    assert json.loads(list_stdout)[0]["status"] == "unmet"
    assert json.loads(link_stdout)["linked_capability_id"] == capability().id


def test_blocked_health_returns_exit_three_and_attempts():
    class BrokenMemory(FakeMemory):
        def health_check(self):
            raise CapabilityMemoryBlocked(
                "embedding_unavailable",
                "embedding model unavailable",
                ("attempt 1", "attempt 2", "attempt 3"),
            )

    memory = BrokenMemory()
    exit_code, stdout, stderr = run_cli(["health"], memory)

    assert exit_code == 3
    assert stdout == ""
    assert json.loads(stderr) == {
        "attempts": ["attempt 1", "attempt 2", "attempt 3"],
        "code": "embedding_unavailable",
        "message": "embedding model unavailable",
        "status": "blocked",
    }
    assert memory.closed is True


def test_failed_search_is_blocked_not_a_successful_empty_result(tmp_path):
    class FailedSearchMemory(FakeMemory):
        def search(self, requirement):
            return SearchResult(
                SearchStatus.FAILED,
                (),
                error_code="vector_search_failed",
                error_message="vector index unavailable",
            )

    requirement = write_json(
        tmp_path,
        "requirement.json",
        {"id": "req-1", "project_id": "project-a", "intent": "OAuth login"},
    )
    memory = FailedSearchMemory()

    exit_code, stdout, stderr = run_cli(["search", "--input", requirement], memory)

    assert exit_code == 3
    assert stdout == ""
    assert json.loads(stderr) == {
        "attempts": ["vector index unavailable"],
        "code": "vector_search_failed",
        "message": "vector index unavailable",
        "status": "blocked",
    }
    assert memory.closed is True


def test_service_construction_failure_is_infrastructure_blocked():
    def broken_factory():
        raise ValueError("locked model metadata is invalid")

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(["health"], service_factory=broken_factory)

    assert exit_code == 3
    assert stdout.getvalue() == ""
    payload = json.loads(stderr.getvalue())
    assert payload["status"] == "blocked"
    assert payload["code"] == "capability_runtime_failed"
    assert "locked model metadata is invalid" in payload["message"]


def test_service_operation_oserror_is_infrastructure_blocked():
    class BrokenMemory(FakeMemory):
        def health_check(self):
            raise OSError("database read failed")

    exit_code, stdout, stderr = run_cli(["health"], BrokenMemory())

    assert exit_code == 3
    assert stdout == ""
    payload = json.loads(stderr)
    assert payload["status"] == "blocked"
    assert payload["code"] == "capability_runtime_failed"


def test_close_failure_blocks_before_any_success_output():
    class CloseFailingMemory(FakeMemory):
        def close(self):
            raise OSError("native connection close failed")

    exit_code, stdout, stderr = run_cli(["health"], CloseFailingMemory())

    assert exit_code == 3
    assert stdout == ""
    payload = json.loads(stderr)
    assert payload["status"] == "blocked"
    assert payload["code"] == "resource_close_failed"
    assert "native connection close failed" in payload["message"]


def test_derived_non_finite_result_is_invalid_json_instead_of_escaping(tmp_path):
    class OverflowingMemory(FakeMemory):
        def evaluate(self, candidate, inputs):
            value = inputs.expected_reuse_count * inputs.benefit_per_reuse
            return EvaluationResult(None, value, False, ("not finite",))

    evaluation = write_json(
        tmp_path,
        "overflow-result.json",
        {
            "capability": capability_json(),
            "inputs": {
                "expected_reuse_count": 1e308,
                "benefit_per_reuse": 1e308,
                "extraction_cost": 0,
                "integration_cost": 0,
                "verification_cost": 0,
                "maintenance_cost": 0,
                "failure_risk": 0,
            },
        },
    )
    memory = OverflowingMemory()

    exit_code, stdout, stderr = run_cli(["evaluate", "--input", evaluation], memory)

    assert exit_code == 2
    assert stdout == ""
    assert json.loads(stderr)["code"] == "invalid_input"
    assert memory.closed is True


def test_result_serialization_failure_is_blocked_and_never_escapes(tmp_path):
    class UnserializableMemory(FakeMemory):
        def initialize(self, project_root):
            return object()

    project = tmp_path / "project"
    project.mkdir()
    memory = UnserializableMemory()

    exit_code, stdout, stderr = run_cli(
        ["init", "--repo", "owner/memory", "--project-root", str(project)],
        memory,
    )

    assert exit_code == 3
    assert stdout == ""
    assert json.loads(stderr)["code"] == "capability_runtime_failed"
    assert memory.closed is True


def test_serializer_infrastructure_value_error_is_blocked(monkeypatch):
    def broken_dumps(*args, **kwargs):
        raise ValueError("serializer backend unavailable")

    monkeypatch.setattr("supermind_memory.cli.json.dumps", broken_dumps)

    exit_code, stdout, stderr = run_cli(["health"], FakeMemory())

    assert exit_code == 3
    assert stdout == ""
    assert json.loads(stderr)["status"] == "blocked"


def test_upstream_output_is_suppressed_before_blocked_json():
    def noisy_factory():
        print("download 40%", file=sys.stdout)
        print("retrying upstream", file=sys.stderr)
        raise CapabilityMemoryBlocked(
            "embedding_unavailable",
            "locked model unavailable",
            ("attempt 1", "attempt 2", "attempt 3"),
        )

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(["health"], service_factory=noisy_factory)

    assert exit_code == 3
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue())["attempts"] == [
        "attempt 1",
        "attempt 2",
        "attempt 3",
    ]


def test_file_descriptor_upstream_noise_is_suppressed(capfd):
    def noisy_factory():
        os.write(1, b"native stdout noise\n")
        os.write(2, b"native stderr noise\n")
        raise CapabilityMemoryBlocked(
            "embedding_unavailable",
            "locked model unavailable",
            ("attempt 1", "attempt 2", "attempt 3"),
        )

    exit_code = main(["health"], service_factory=noisy_factory)
    captured = capfd.readouterr()

    assert exit_code == 3
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == "embedding_unavailable"


def test_unhealthy_report_is_blocked_and_preserves_repair_attempts():
    class UnhealthyMemory(FakeMemory):
        def health_check(self):
            return HealthReport(
                False,
                None,
                (),
                NOW,
                (
                    "embedding_unavailable: attempt 1",
                    "embedding_unavailable: attempt 2",
                    "embedding_unavailable: attempt 3",
                ),
            )

    exit_code, stdout, stderr = run_cli(["health"], UnhealthyMemory())

    assert exit_code == 3
    assert stdout == ""
    payload = json.loads(stderr)
    assert payload["code"] == "embedding_unavailable"
    assert len(payload["attempts"]) == 3


@pytest.mark.parametrize(
    "arguments",
    (
        ["search"],
        ["search", "--input", "{\"intent\": \"shell text\"}"],
        ["inspect", "--view", "detail"],
        ["unknown"],
    ),
)
def test_invalid_input_is_json_on_stderr_and_exit_two(arguments):
    exit_code, stdout, stderr = run_cli(arguments, FakeMemory())

    assert exit_code == 2
    assert stdout == ""
    payload = json.loads(stderr)
    assert payload["status"] == "invalid"
    assert payload["code"] == "invalid_input"


def test_explicit_stdin_is_supported(monkeypatch):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('{"id":"req-stdin","project_id":"project-a","intent":"login"}'),
    )
    memory = FakeMemory()

    exit_code, stdout, stderr = run_cli(["search", "--input", "-"], memory)

    assert exit_code == 0
    assert json.loads(stdout)["status"] == "complete"
    assert stderr == ""


def test_typed_input_rejects_string_in_place_of_boolean(tmp_path):
    reuse = write_json(
        tmp_path,
        "invalid-reuse.json",
        {
            "capability_id": capability().id,
            "project": "project-b",
            "succeeded": "false",
            "integration_effort": 1,
            "benefit": 5,
        },
    )
    memory = FakeMemory()

    exit_code, stdout, stderr = run_cli(["record-use", "--input", reuse], memory)

    assert exit_code == 2
    assert stdout == ""
    assert json.loads(stderr)["code"] == "invalid_input"
    assert memory.calls == []


@pytest.mark.parametrize(
    ("command", "payload"),
    (
        (
            "evaluate",
            {
                "capability": capability_json(),
                "inputs": {
                    "expected_reuse_count": "3",
                    "benefit_per_reuse": 5,
                    "extraction_cost": 1,
                    "integration_cost": 1,
                    "verification_cost": 1,
                    "maintenance_cost": 1,
                    "failure_risk": 0.1,
                },
            },
        ),
        (
            "register",
            {
                "capability": {**capability_json(), "confidence": float("inf")},
                "evidence": [],
            },
        ),
        (
            "register",
            {
                "capability": capability_json(),
                "evidence": [
                    {
                        "id": "evidence-1",
                        "capability_id": capability().id,
                        "source_project": "project-a",
                        "evidence_type": "verification",
                        "outcome": "success",
                        "metric_name": None,
                        "metric_value": None,
                        "confidence": True,
                        "observed_at": NOW,
                        "supporting_uri": None,
                    }
                ],
            },
        ),
        (
            "inspect",
            {
                "requirement": {"id": "req-1", "project_id": "project-a", "intent": "login"},
                "result": {
                    "status": "complete",
                    "generation": "generation-1",
                    "matches": [
                        {
                            "capability_id": capability().id,
                            "vector_score": 0.9,
                            "lexical_score": 0.8,
                            "contract_fit": 1.0,
                            "requirement_fit": 0.9,
                            "reliability": 0.9,
                            "historical_benefit": 0.8,
                            "integration_cost": 0.1,
                            "maintenance_risk": 0.1,
                            "reuse_score": True,
                        }
                    ],
                },
            },
        ),
    ),
)
def test_all_typed_numeric_records_reject_non_real_or_non_finite_values(
    tmp_path,
    command,
    payload,
):
    source = write_json(tmp_path, f"{command}.json", payload)
    arguments = [command, "--input", source]
    if command == "inspect":
        arguments = ["inspect", "--view", "decision", "--input", source]
    memory = FakeMemory()

    exit_code, stdout, stderr = run_cli(arguments, memory)

    assert exit_code == 2
    assert stdout == ""
    assert json.loads(stderr)["code"] == "invalid_input"
    assert memory.calls == []


def test_overflowing_json_integer_is_invalid_not_blocked(tmp_path):
    payload = {
        "capability_id": capability().id,
        "project": "project-b",
        "succeeded": True,
        "integration_effort": 10**4000,
        "benefit": 5,
    }
    source = write_json(tmp_path, "overflow.json", payload)

    exit_code, stdout, stderr = run_cli(
        ["record-use", "--input", source],
        FakeMemory(),
    )

    assert exit_code == 2
    assert stdout == ""
    assert json.loads(stderr)["code"] == "invalid_input"


def test_global_data_home_is_forwarded_to_default_factory(tmp_path, monkeypatch):
    memory = FakeMemory()
    observed: list[Path] = []

    def fake_build_service(data_home=None, **_kwargs):
        observed.append(data_home)
        return memory

    monkeypatch.setattr("supermind_memory.cli.build_service", fake_build_service)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(["--data-home", str(tmp_path), "health"])

    assert exit_code == 0
    assert observed == [tmp_path.resolve()]


def test_model_acquisition_is_attempted_three_times_before_blocking(tmp_path, monkeypatch):
    attempts = []

    def failing_provider(*args, **kwargs):
        attempts.append("attempt")
        raise OSError("model host unavailable")

    monkeypatch.setattr("supermind_memory.cli.FastEmbedProvider", failing_provider)

    with pytest.raises(CapabilityMemoryBlocked) as captured:
        build_service(tmp_path)

    assert captured.value.code == "embedding_unavailable"
    assert len(captured.value.attempts) == 3
    assert attempts == ["attempt", "attempt", "attempt"]




























def test_build_service_rejects_model_cache_symlink_before_loading_model(tmp_path, monkeypatch):
    memory_root = tmp_path / "supermind" / "memory"
    memory_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (memory_root / "model-cache").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        "supermind_memory.cli.FastEmbedProvider",
        lambda *args, **kwargs: pytest.fail("model provider must not touch an escaping path"),
    )

    with pytest.raises(CapabilityMemoryBlocked) as captured:
        build_service(tmp_path)

    assert captured.value.code == "unsafe_runtime_path"
    assert not tuple(outside.iterdir())


def test_build_service_requires_initialized_event_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "supermind_memory.cli.FastEmbedProvider",
        lambda *args, **kwargs: KeywordEmbeddingProvider(),
    )

    with pytest.raises(CapabilityMemoryBlocked) as captured:
        build_service(tmp_path)

    assert captured.value.code == "repository_not_initialized"


def test_plain_init_reports_missing_repository_configuration(tmp_path, capsys):
    code = main([
        "--data-home", str(tmp_path), "init", "--project-root", str(tmp_path),
        "--format", "json",
    ])
    assert code == 3
    assert json.loads(capsys.readouterr().err)["code"] == "memory_repository_unconfigured"


def test_init_without_project_root_checks_health_without_discovery():
    memory = FakeMemory()
    code, stdout, stderr = run_cli(["init", "--repo", "owner/memory", "--format", "json"], memory)
    assert code == 0, stderr
    assert json.loads(stdout)["result"]["healthy"] is True
    assert memory.calls == [("health", None)]


def test_plain_init_reuses_existing_repository_configuration(tmp_path, monkeypatch):
    from argparse import Namespace
    from supermind_memory.cli import _initialize_repository_command
    from supermind_memory.config import MemoryPaths, RepositoryConfig

    paths = MemoryPaths.from_codex_home(tmp_path)
    paths.config.parent.mkdir(parents=True)
    paths.config.touch()
    existing = object()
    monkeypatch.setattr(RepositoryConfig, "read", lambda _: existing)
    assert _initialize_repository_command(Namespace(repo=None, create_private=False), tmp_path) is existing
