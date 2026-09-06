"""Command-line boundary for the local capability-memory service."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, NoReturn

from supermind_memory.bootstrap import Bootstrap
from supermind_memory.config import MemoryPaths, RepositoryConfig, resolve_codex_home
from supermind_memory.discovery import CapabilityDiscovery, SourceRegistry
from supermind_memory.embeddings import FastEmbedProvider, _load_model_lock
from supermind_memory.explorer import CapabilityExplorer
from supermind_memory.health import HealthManager
from supermind_memory.event_store import EventStore
from supermind_memory.git_client import (
    GitClient, GitHubClient, InitRequest, SubprocessCommandRunner, initialize_repository,
)
from supermind_memory.projection import project_authority
from supermind_memory.protocol import ProtocolEnvelope, operation_id
from supermind_memory.replay import replay
from supermind_memory.repository import CapabilityRepository
from supermind_memory.search import CapabilitySearch
from supermind_memory.service import CapabilityMemory
from supermind_memory.sync import SyncCoordinator
from supermind_memory.types import (
    ArtifactType,
    Capability,
    CapabilityMemoryBlocked,
    CandidateMatch,
    DiscoveryContext,
    Evidence,
    InspectFilter,
    Lifecycle,
    RequirementProfile,
    ReuseResult,
    SearchResult,
    SearchStatus,
    ValueInputs,
    ViewType,
)
from supermind_memory.workflow import SupermindWorkflow


class InvalidInput(ValueError):
    """A user-controlled argument or input document is invalid."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise InvalidInput(message)


def build_service(data_home: Path | None = None) -> CapabilityMemory:
    """Construct one service graph with shared locked-model and health components."""
    codex_home = (
        data_home.expanduser().resolve()
        if data_home is not None
        else resolve_codex_home()
    )
    paths = MemoryPaths.from_codex_home(codex_home)
    _prepare_safe_paths(paths)
    model_lock = _load_model_lock()
    attempts: list[str] = []
    embeddings = None
    for attempt in range(1, 4):
        try:
            embeddings = FastEmbedProvider(model_lock.name, paths.model_cache)
            break
        except Exception as error:
            attempts.append(f"attempt {attempt}: {type(error).__name__}: {error}")
    if embeddings is None:
        raise CapabilityMemoryBlocked(
            "embedding_unavailable",
            "locked embedding model remains unavailable after 3 attempts",
            tuple(attempts),
        )
    repository: CapabilityRepository | None = None
    try:
        repository = CapabilityRepository.open(
            paths.database,
            writer_lock_path=paths.locks / "writer.lock",
        )
        config = RepositoryConfig.read(paths.config) if paths.config.is_file() else None
        coordinator = None
        authority_digest = None
        runner = SubprocessCommandRunner()
        if config is not None:
            replayed = replay(EventStore(paths.checkout).load_all())
            project_authority(replayed, repository, embeddings)
            authority_digest = replayed.digest
            coordinator = SyncCoordinator(
                paths=paths, config=config, git=GitClient(runner), github=GitHubClient(runner),
                repository=repository, embeddings=embeddings,
            )
        else:
            raise CapabilityMemoryBlocked(
                "repository_not_initialized", "events-v1 repository is not initialized", ()
            )
        health = HealthManager(
            paths, repository, embeddings,
            authority_mode="events-v1" if config is not None else None,
            expected_authority_digest=authority_digest,
        )
        health.ensure_healthy()
        return CapabilityMemory(
            bootstrap=Bootstrap(
                paths,
                repository,
                embeddings,
                health_manager=health,
            ),
            discovery=CapabilityDiscovery(SourceRegistry(repository)),
            repository=repository,
            search_engine=CapabilitySearch(repository, embeddings),
            embedding_provider=embeddings,
            health_manager=health,
            codex_home=codex_home,
            sync_coordinator=coordinator,
            authority_mode="events-v1",
            command_runner=runner,
        )
    except BaseException:
        if repository is not None:
            repository.close()
        raise


def _prepare_safe_paths(paths: MemoryPaths) -> None:
    """Create owned roots without following a symlink into another tree."""
    try:
        parent = paths.root.parent
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise OSError(f"unsafe capability-memory parent: {parent}")
        for path in (
            paths.root,
            paths.database,
            paths.runtime,
            paths.model_cache,
            paths.locks,
            paths.generations,
        ):
            if path.is_symlink():
                raise OSError(f"capability-memory path must not be a symlink: {path}")
            path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise OSError(f"unsafe capability-memory directory: {path}")
    except OSError as error:
        raise CapabilityMemoryBlocked(
            "unsafe_runtime_path",
            str(error),
            (f"runtime path validation: {error}",),
        ) from error


_DEFAULT_SERVICE_FACTORY = build_service


def main(
    argv: Sequence[str] | None = None,
    service_factory: Callable[[], CapabilityMemory] = build_service,
) -> int:
    """Run one CLI operation and return its stable process exit code."""
    invocation = tuple(sys.argv[1:] if argv is None else argv)
    request_id = operation_id()
    protocol_json = "--format" in invocation and "json" in invocation
    try:
        arguments = _parser().parse_args(invocation)
        data_home = (
            Path(arguments.data_home).expanduser().resolve()
            if arguments.data_home is not None
            else None
        )
    except (InvalidInput, OSError, RuntimeError, ValueError) as error:
        payload = _invalid_payload(error)
        if protocol_json:
            payload = _protocol_failure(request_id, payload)
        return _emit_failure(2, payload)

    memory: CapabilityMemory | None = None
    result: object | None = None
    markdown: str | None = None
    rendered_output: str | None = None
    failure: tuple[int, Mapping[str, object]] | None = None
    try:
        with _suppress_upstream_output():
            try:
                try:
                    if service_factory is _DEFAULT_SERVICE_FACTORY:
                        # Resolve the global name so tests and embedders can replace the factory.
                        if arguments.command in {"init", "migrate"}:
                            _initialize_repository_command(arguments, data_home)
                        memory = build_service(data_home=data_home)
                    else:
                        memory = service_factory()
                except CapabilityMemoryBlocked:
                    raise
                except Exception as error:
                    raise RuntimeError(str(error) or type(error).__name__) from error
                result, markdown = _dispatch(arguments, memory, data_home)
                if markdown is not None:
                    rendered_output = markdown + (
                        "" if not markdown or markdown.endswith("\n") else "\n"
                    )
                else:
                    rendered_output = _json_text(
                        _protocol_success(request_id, memory, result)
                        if protocol_json else result
                    )
            except CapabilityMemoryBlocked as error:
                failure = (3, _blocked_payload(error.code, error.message, error.attempts))
            except (InvalidInput, json.JSONDecodeError, KeyError, ValueError) as error:
                failure = (2, _invalid_payload(error))
            except Exception as error:
                failure = (
                    3,
                    _blocked_payload(
                        "capability_runtime_failed",
                        str(error) or type(error).__name__,
                        (f"{type(error).__name__}: {error}",),
                    ),
                )
            finally:
                if memory is not None:
                    try:
                        memory.close()
                    except Exception as error:
                        failure = (
                            3,
                            _blocked_payload(
                                "resource_close_failed",
                                str(error) or type(error).__name__,
                                (f"{type(error).__name__}: {error}",),
                            ),
                        )
    except Exception as error:
        failure = (
            3,
            _blocked_payload(
                "capability_runtime_failed",
                str(error) or type(error).__name__,
                (f"{type(error).__name__}: {error}",),
            ),
        )

    if failure is not None:
        if protocol_json:
            failure = (failure[0], _protocol_failure(request_id, failure[1], memory))
        return _emit_failure(*failure)
    try:
        sys.stdout.write(rendered_output or "")
    except Exception as error:
        return _emit_failure(
            3,
            _blocked_payload(
                "command_output_failed",
                str(error) or type(error).__name__,
                (f"{type(error).__name__}: {error}",),
            ),
        )
    return 0


def _emit_failure(exit_code: int, payload: Mapping[str, object]) -> int:
    try:
        rendered = _json_text(payload)
    except Exception:
        rendered = (
            '{"attempts":["error serialization failed"],'
            '"code":"command_output_failed",'
            '"message":"failed to serialize the CLI error response",'
            '"status":"blocked"}\n'
        )
        exit_code = 3
    try:
        sys.stderr.write(rendered)
    except Exception:
        pass
    return exit_code


@contextmanager
def _suppress_upstream_output() -> Iterator[None]:
    """Keep Python and native dependency chatter out of the CLI protocol streams."""
    internal_stdout = io.StringIO()
    internal_stderr = io.StringIO()
    saved_stdout: int | None = None
    saved_stderr: int | None = None
    sink = None
    stdout_redirected = False
    stderr_redirected = False
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        sink = tempfile.TemporaryFile()
        os.dup2(sink.fileno(), 1)
        stdout_redirected = True
        os.dup2(sink.fileno(), 2)
        stderr_redirected = True
    except OSError:
        if stdout_redirected and saved_stdout is not None:
            os.dup2(saved_stdout, 1)
        if stderr_redirected and saved_stderr is not None:
            os.dup2(saved_stderr, 2)
        if saved_stdout is not None:
            os.close(saved_stdout)
        if saved_stderr is not None:
            os.close(saved_stderr)
        if sink is not None:
            sink.close()
        with redirect_stdout(internal_stdout), redirect_stderr(internal_stderr):
            yield
        return
    try:
        with redirect_stdout(internal_stdout), redirect_stderr(internal_stderr):
            yield
    finally:
        if saved_stdout is not None:
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)
        if saved_stderr is not None:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
        if sink is not None:
            sink.close()


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(prog="capability-memory")
    parser.add_argument("--data-home", help="Codex data home used for capability-memory state")
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init")
    initialize.add_argument("--project-root", required=True)
    repository = initialize.add_mutually_exclusive_group()
    repository.add_argument("--repo")
    repository.add_argument("--create-private", action="store_true")
    initialize.add_argument("--name", default="supermind-memory")
    _add_format(initialize)

    discover = commands.add_parser("discover")
    source = discover.add_mutually_exclusive_group(required=True)
    source.add_argument("--input")
    source.add_argument("--project-root")
    discover.add_argument("--codex-home")
    _add_format(discover)

    for command in ("search", "evaluate", "register", "record-use", "record-outcome"):
        operation = commands.add_parser(command)
        operation.add_argument("--input", required=True)
        _add_format(operation, markdown=command == "search")

    begin_design = commands.add_parser("begin-design")
    begin_design.add_argument("--project-root", required=True)
    begin_design.add_argument("--input", required=True)
    _add_format(begin_design)

    for command in ("complete-implementation", "complete-reuse"):
        operation = commands.add_parser(command)
        operation.add_argument("--input", required=True)
        _add_format(operation)

    list_demands = commands.add_parser("list-demands")
    _add_format(list_demands)
    link_demand = commands.add_parser("link-demand")
    link_demand.add_argument("--input", required=True)
    _add_format(link_demand)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--view", required=True, choices=[item.value for item in ViewType])
    inspect.add_argument("--input")
    inspect.add_argument("--capability-id")
    inspect.add_argument("--category", action="append", default=[])
    inspect.add_argument(
        "--lifecycle",
        action="append",
        choices=[item.value for item in Lifecycle],
        default=[],
    )
    inspect.add_argument("--stack", action="append", default=[])
    _add_format(inspect, markdown=True)

    rebuild = commands.add_parser("rebuild")
    _add_format(rebuild)
    health = commands.add_parser("health")
    _add_format(health)
    for command in ("status", "sync", "render", "open"):
        operation = commands.add_parser(command)
        _add_format(operation)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--repo", required=True)
    migrate.add_argument("--legacy-data-home", required=True)
    _add_format(migrate)
    resolve = commands.add_parser("resolve-conflict")
    resolve.add_argument("--input", required=True)
    _add_format(resolve)
    return parser


def _add_format(parser: argparse.ArgumentParser, *, markdown: bool = False) -> None:
    choices = ("json", "markdown") if markdown else ("json",)
    parser.add_argument("--format", choices=choices, default="json")


def _dispatch(
    arguments: argparse.Namespace,
    memory: CapabilityMemory,
    data_home: Path | None,
) -> tuple[object, str | None]:
    command = arguments.command
    if command == "init":
        health = memory.initialize(Path(arguments.project_root))
        coordinator = getattr(memory, "sync_coordinator", None)
        if coordinator is None:
            return health, None
        return {
            "health": health,
            "repository": coordinator.config,
        }, None
    if command == "discover":
        if arguments.input:
            context = _discovery_context(_read_object(arguments.input))
        else:
            codex_home = (
                Path(arguments.codex_home)
                if arguments.codex_home
                else data_home or memory.codex_home
            )
            context = DiscoveryContext(Path(arguments.project_root), codex_home)
        return memory.discover(context), None
    if command == "search":
        requirement = _requirement(_read_object(arguments.input))
        result = memory.search(requirement)
        if result.status is SearchStatus.FAILED:
            message = result.error_message or "capability search did not complete"
            raise CapabilityMemoryBlocked(
                result.error_code or "search_failed",
                message,
                (message,),
            )
        if arguments.format == "markdown":
            return result, CapabilityExplorer(memory.repository).decision(requirement, result)
        return result, None
    if command == "evaluate":
        payload = _read_object(arguments.input)
        return memory.evaluate(
            _capability(_object_field(payload, "capability", "candidate")),
            _value_inputs(_object_field(payload, "inputs", "value_inputs")),
        ), None
    if command == "register":
        payload = _read_object(arguments.input)
        evidence = _list_field(payload, "evidence")
        return memory.register(
            _capability(_object_field(payload, "capability")),
            tuple(_evidence(item) for item in evidence),
        ), None
    if command in {"record-use", "record-outcome"}:
        return memory.record_use(_reuse_result(_read_object(arguments.input))), None
    if command == "begin-design":
        return SupermindWorkflow(memory).begin_design(
            Path(arguments.project_root),
            _requirement(_read_object(arguments.input)),
        ), None
    if command == "complete-implementation":
        payload = _read_object(arguments.input)
        return SupermindWorkflow(memory).complete_implementation(
            _capability(_object_field(payload, "capability", "candidate")),
            _value_inputs(_object_field(payload, "inputs", "value_inputs")),
            tuple(_evidence(item) for item in _list_field(payload, "evidence")),
        ), None
    if command == "complete-reuse":
        return SupermindWorkflow(memory).complete_reuse(
            _reuse_result(_read_object(arguments.input))
        ), None
    if command == "list-demands":
        return memory.list_requirement_observations(), None
    if command == "link-demand":
        payload = _read_object(arguments.input)
        return memory.link_requirement_observation(
            _string(payload, "observation_id"),
            _string(payload, "capability_id"),
        ), None
    if command == "rebuild":
        return memory.rebuild(), None
    if command == "health":
        report = memory.health_check()
        if not report.healthy:
            code = (
                report.failures[0].partition(":")[0]
                if report.failures
                else "health_unavailable"
            )
            raise CapabilityMemoryBlocked(
                code,
                "capability memory is unhealthy",
                report.failures,
            )
        return report, None
    if command == "status":
        return memory.status(), None
    if command == "sync":
        return memory.sync(), None
    if command == "render":
        return memory.render(), None
    if command == "open":
        return memory.open_browser(), None
    if command == "migrate":
        return memory.migrate(Path(arguments.legacy_data_home), arguments.repo), None
    if command == "resolve-conflict":
        return memory.resolve_conflict(_read_object(arguments.input)), None
    if command == "inspect":
        return _inspect(arguments, memory)
    raise InvalidInput(f"unknown command: {command}")


def _inspect(
    arguments: argparse.Namespace,
    memory: CapabilityMemory,
) -> tuple[object, str | None]:
    view = ViewType(arguments.view)
    filters = InspectFilter(
        category=tuple(arguments.category),
        lifecycle=tuple(Lifecycle(value) for value in arguments.lifecycle),
        stack=tuple(arguments.stack),
        capability_id=arguments.capability_id,
    )
    explorer = CapabilityExplorer(memory.repository)
    if view is ViewType.OVERVIEW:
        output = explorer.overview()
    elif view is ViewType.TABLE:
        output = explorer.table(filters)
    elif view is ViewType.DETAIL:
        if not arguments.capability_id:
            raise InvalidInput("inspect detail requires --capability-id")
        output = explorer.detail(arguments.capability_id)
    elif view is ViewType.GRAPH:
        output = explorer.graph(filters)
    else:
        if not arguments.input:
            raise InvalidInput("inspect decision requires --input")
        payload = _read_object(arguments.input)
        output = explorer.decision(
            _requirement(_object_field(payload, "requirement")),
            _search_result(_object_field(payload, "result", "search_result")),
        )
    if arguments.format == "markdown":
        return {"status": "complete", "view": view, "output": output}, output
    return {"status": "complete", "view": view, "output": output}, None


def _read_object(source: str) -> dict[str, Any]:
    try:
        if source == "-":
            value = json.load(sys.stdin, parse_constant=_reject_json_constant)
        else:
            path = Path(source).expanduser()
            if not path.is_file():
                raise InvalidInput(f"input file does not exist: {source}")
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle, parse_constant=_reject_json_constant)
    except InvalidInput:
        raise
    except OSError as error:
        raise InvalidInput(f"cannot read input file: {error}") from error
    if not isinstance(value, dict):
        raise InvalidInput("input document must be a JSON object")
    return value


def _initialize_repository_command(arguments: argparse.Namespace, data_home: Path | None) -> RepositoryConfig:
    codex_home = data_home or resolve_codex_home()
    paths = MemoryPaths.from_codex_home(codex_home)
    runner = SubprocessCommandRunner()
    repository = arguments.repo
    create_private = getattr(arguments, "create_private", False)
    if create_private:
        owner_result = runner.run(("gh", "api", "user", "--jq", ".login"))
        if owner_result.returncode != 0:
            raise CapabilityMemoryBlocked("github_identity_unavailable", "cannot resolve GitHub owner", ())
        try:
            owner = owner_result.stdout.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as error:
            raise InvalidInput("GitHub owner is not valid UTF-8") from error
        repository = f"{owner}/{arguments.name}"
    if not repository:
        if not paths.config.exists():
            raise CapabilityMemoryBlocked(
                "memory_repository_unconfigured", "choose a private memory repository", (),
            )
        return RepositoryConfig.read(paths.config)
    device_seed = hashlib.sha256(str(paths.root).encode()).hexdigest()[:24]
    try:
        return initialize_repository(
            InitRequest(repository, f"device-{device_seed}", create_private, runner), paths,
        )
    except Exception as error:
        if isinstance(error, CapabilityMemoryBlocked):
            raise
        raise CapabilityMemoryBlocked("repository_initialization_failed", str(error), (str(error),)) from error


def _requirement(payload: Mapping[str, Any]) -> RequirementProfile:
    return RequirementProfile(
        id=_string(payload, "id"),
        project_id=_string(payload, "project_id"),
        intent=_string(payload, "intent"),
        contract=_optional_string(payload, "contract", ""),
        category_hint=_strings(payload, "category_hint"),
        stack=_strings(payload, "stack"),
        constraints=_strings(payload, "constraints"),
        quality_requirements=_strings(payload, "quality_requirements"),
        runtime=_strings(payload, "runtime"),
        platform=_strings(payload, "platform"),
        license=_strings(payload, "license"),
    )


def _capability(payload: Mapping[str, Any]) -> Capability:
    values = _exact_dataclass_fields(payload, Capability)
    for name in (
        "category_path", "facets", "constraints", "stack", "runtime",
        "platform", "dependencies", "compatibility",
    ):
        values[name] = _strings(values, name)
    values["artifact_type"] = ArtifactType(values["artifact_type"])
    values["lifecycle"] = Lifecycle(values["lifecycle"])
    values["confidence"] = _number(values, "confidence")
    values["expected_net_value"] = _number(values, "expected_net_value")
    return Capability(**values)


def _evidence(payload: Mapping[str, Any]) -> Evidence:
    values = _exact_dataclass_fields(payload, Evidence)
    metric_value = values.get("metric_value")
    if metric_value is not None:
        metric_value = _number(values, "metric_value")
    return Evidence(
        id=_string(values, "id"),
        capability_id=_string(values, "capability_id"),
        source_project=_string(values, "source_project"),
        evidence_type=_string(values, "evidence_type"),
        outcome=_string(values, "outcome"),
        metric_name=_nullable_string(values, "metric_name"),
        metric_value=metric_value,
        confidence=_number(values, "confidence"),
        observed_at=_string(values, "observed_at"),
        supporting_uri=_nullable_string(values, "supporting_uri"),
        integration_effort=_number(values, "integration_effort", default=0.0),
        benefit=_number(values, "benefit", default=0.0),
        failure_risk=_number(values, "failure_risk", default=0.0),
    )


def _value_inputs(payload: Mapping[str, Any]) -> ValueInputs:
    values = _exact_dataclass_fields(payload, ValueInputs)
    return ValueInputs(
        expected_reuse_count=_number(values, "expected_reuse_count"),
        benefit_per_reuse=_number(values, "benefit_per_reuse"),
        extraction_cost=_number(values, "extraction_cost"),
        integration_cost=_number(values, "integration_cost"),
        verification_cost=_number(values, "verification_cost"),
        maintenance_cost=_number(values, "maintenance_cost"),
        failure_risk=_number(values, "failure_risk"),
    )


def _reuse_result(payload: Mapping[str, Any]) -> ReuseResult:
    values = _exact_dataclass_fields(payload, ReuseResult)
    succeeded = values.get("succeeded")
    if not isinstance(succeeded, bool):
        raise InvalidInput("succeeded must be a boolean")
    return ReuseResult(
        capability_id=_string(values, "capability_id"),
        project=_string(values, "project"),
        succeeded=succeeded,
        integration_effort=_number(values, "integration_effort"),
        benefit=_number(values, "benefit"),
        failure_reason=_nullable_string(values, "failure_reason"),
    )


def _discovery_context(payload: Mapping[str, Any]) -> DiscoveryContext:
    return DiscoveryContext(
        project_root=Path(_string(payload, "project_root")),
        codex_home=Path(_string(payload, "codex_home")),
    )


def _search_result(payload: Mapping[str, Any]) -> SearchResult:
    values = _exact_dataclass_fields(payload, SearchResult)
    matches = _list_field(values, "matches")
    raw_snapshots = values.get("capability_snapshots", [])
    if not isinstance(raw_snapshots, list) or not all(
        isinstance(item, Mapping) for item in raw_snapshots
    ):
        raise InvalidInput("capability_snapshots must be an array of objects")
    return SearchResult(
        status=SearchStatus(_string(values, "status")),
        matches=tuple(
            _candidate_match(item)
            for item in matches
        ),
        generation=_nullable_string(values, "generation"),
        error_code=_nullable_string(values, "error_code"),
        error_message=_nullable_string(values, "error_message"),
        capability_snapshots=tuple(_capability(item) for item in raw_snapshots),
    )


def _candidate_match(payload: Mapping[str, Any]) -> CandidateMatch:
    values = _exact_dataclass_fields(payload, CandidateMatch)
    return CandidateMatch(
        capability_id=_string(values, "capability_id"),
        vector_score=_number(values, "vector_score"),
        lexical_score=_number(values, "lexical_score"),
        contract_fit=_number(values, "contract_fit"),
        requirement_fit=_number(values, "requirement_fit"),
        reliability=_number(values, "reliability"),
        historical_benefit=_number(values, "historical_benefit"),
        integration_cost=_number(values, "integration_cost"),
        maintenance_risk=_number(values, "maintenance_risk"),
        reuse_score=_number(values, "reuse_score"),
        rejection_reasons=_strings(values, "rejection_reasons"),
        source_available=_optional_boolean(values, "source_available"),
    )


def _exact_dataclass_fields(payload: Mapping[str, Any], record_type: type) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise InvalidInput(f"{record_type.__name__} must be a JSON object")
    allowed = {field.name for field in fields(record_type)}
    extras = set(payload) - allowed
    if extras:
        raise InvalidInput(f"unexpected {record_type.__name__} fields: {', '.join(sorted(extras))}")
    return dict(payload)


def _object_field(payload: Mapping[str, Any], *names: str) -> Mapping[str, Any]:
    for name in names:
        value = payload.get(name)
        if isinstance(value, Mapping):
            return value
    raise InvalidInput(f"missing object field: {' or '.join(names)}")


def _list_field(payload: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    value = payload.get(name)
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise InvalidInput(f"{name} must be an array of objects")
    return value


def _string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise InvalidInput(f"{name} must be a string")
    return value


def _optional_string(payload: Mapping[str, Any], name: str, default: str) -> str:
    value = payload.get(name, default)
    if not isinstance(value, str):
        raise InvalidInput(f"{name} must be a string")
    return value


def _optional_boolean(payload: Mapping[str, Any], name: str) -> bool | None:
    value = payload.get(name)
    if value is not None and not isinstance(value, bool):
        raise InvalidInput(f"{name} must be a boolean or null")
    return value


def _nullable_string(payload: Mapping[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is not None and not isinstance(value, str):
        raise InvalidInput(f"{name} must be a string or null")
    return value


def _strings(payload: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = payload.get(name, ())
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise InvalidInput(f"{name} must be an array of strings")
    return tuple(value)


def _number(
    payload: Mapping[str, Any],
    name: str,
    *,
    default: float | None = None,
) -> float:
    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidInput(f"{name} must be a number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise InvalidInput(f"{name} must be a finite real number") from error
    if not math.isfinite(result):
        raise InvalidInput(f"{name} must be a finite real number")
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise InvalidInput(f"non-finite JSON number is not allowed: {value}")


def _invalid(error: BaseException) -> int:
    return _emit_failure(2, _invalid_payload(error))


def _invalid_payload(error: BaseException) -> Mapping[str, object]:
    from supermind_memory.redaction import redact_text

    return {
        "status": "invalid",
        "code": "invalid_input",
        "message": redact_text(str(error) or type(error).__name__),
    }


def _blocked_payload(
    code: str,
    message: str,
    attempts: Sequence[str],
) -> Mapping[str, object]:
    from supermind_memory.redaction import redact_text

    return {
        "status": "blocked",
        "code": redact_text(code),
        "message": redact_text(message),
        "attempts": tuple(redact_text(attempt) for attempt in attempts),
    }


def _protocol_metadata(memory: CapabilityMemory | None, result: object | None = None) -> tuple[str, str | None, str | None]:
    sync_state = getattr(result, "sync_state", None)
    digest = getattr(result, "event_set_digest", None)
    generation = getattr(result, "generation", None)
    if memory is not None:
        state = getattr(memory, "protocol_state", None)
        if callable(state):
            try:
                metadata = state()
            except Exception:
                metadata = {}
            sync_state = sync_state or metadata.get("sync_state")
            digest = digest or metadata.get("event_set_digest")
            generation = generation or metadata.get("generation")
    return (getattr(sync_state, "value", sync_state) or "unchanged", digest, generation)


def _protocol_success(request_id: str, memory: CapabilityMemory, result: object) -> ProtocolEnvelope:
    sync_state, digest, generation = _protocol_metadata(memory, result)
    return ProtocolEnvelope.complete(request_id, result, sync_state=sync_state,
                                     event_set_digest=digest, generation=generation)


def _protocol_failure(
    request_id: str, payload: Mapping[str, object], memory: CapabilityMemory | None = None,
) -> ProtocolEnvelope:
    sync_state, digest, generation = _protocol_metadata(memory)
    return ProtocolEnvelope.failure(
        request_id, str(payload["status"]), str(payload["code"]), str(payload["message"]),
        tuple(str(item) for item in payload.get("attempts", ())), sync_state=sync_state,
        event_set_digest=digest, generation=generation,
    )


def _write_json(stream: Any, value: object) -> None:
    stream.write(_json_text(value))


def _json_text(value: object) -> str:
    return (
        json.dumps(
            _json_value(value),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        serialized = {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
        if isinstance(value, SearchResult) and not value.capability_snapshots:
            serialized.pop("capability_snapshots")
        if isinstance(value, CandidateMatch) and value.source_available is None:
            serialized.pop("source_available")
        return serialized
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidInput("command result contains a non-finite number")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
