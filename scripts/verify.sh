#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  "") run_tests=1 ;;
  --structure-only) run_tests=0 ;;
  *) echo "Usage: scripts/verify.sh [--structure-only]" >&2; exit 2 ;;
esac

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
plugin_root="$project_root/plugins/supermind"
memory_root="$project_root/capability-memory"
skill_root="$plugin_root/skills/supermind"
validator_python="${CODEX_PYTHON:-python3}"
validator=("$validator_python")

"$validator_python" -m json.tool "$project_root/.agents/plugins/marketplace.json" >/dev/null
"$validator_python" -m json.tool "$plugin_root/.codex-plugin/plugin.json" >/dev/null

if ! "$validator_python" -c 'import yaml' 2>/dev/null; then
  if command -v uv >/dev/null 2>&1; then
    validator=(uv run --quiet --with PyYAML python3)
  else
    echo "Validation needs PyYAML. Install it or provide uv, then run this script again." >&2
    exit 1
  fi
fi

"${validator[@]}" "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" "$skill_root"
"${validator[@]}" "${CODEX_HOME:-$HOME/.codex}/skills/.system/plugin-creator/scripts/validate_plugin.py" "$plugin_root"

expected_files='.agents/plugins/marketplace.json
.gitignore
README.md
capability-memory/evaluation/retrieval-v1.json
capability-memory/model.lock.json
capability-memory/pyproject.toml
capability-memory/requirements.lock
capability-memory/schemas/v1/event.schema.json
capability-memory/schemas/v1/memory.schema.json
capability-memory/src/supermind_memory/__init__.py
capability-memory/src/supermind_memory/bootstrap.py
capability-memory/src/supermind_memory/cli.py
capability-memory/src/supermind_memory/compatibility.py
capability-memory/src/supermind_memory/config.py
capability-memory/src/supermind_memory/decision.py
capability-memory/src/supermind_memory/discovery.py
capability-memory/src/supermind_memory/embeddings.py
capability-memory/src/supermind_memory/event_model.py
capability-memory/src/supermind_memory/event_store.py
capability-memory/src/supermind_memory/explorer.py
capability-memory/src/supermind_memory/git_client.py
capability-memory/src/supermind_memory/health.py
capability-memory/src/supermind_memory/lifecycle.py
capability-memory/src/supermind_memory/migration.py
capability-memory/src/supermind_memory/projection.py
capability-memory/src/supermind_memory/protocol.py
capability-memory/src/supermind_memory/purge.py
capability-memory/src/supermind_memory/quality.py
capability-memory/src/supermind_memory/redaction.py
capability-memory/src/supermind_memory/renderer.py
capability-memory/src/supermind_memory/replay.py
capability-memory/src/supermind_memory/repository.py
capability-memory/src/supermind_memory/resources.py
capability-memory/src/supermind_memory/retrieval_evaluation.py
capability-memory/src/supermind_memory/schema.py
capability-memory/src/supermind_memory/scoring.py
capability-memory/src/supermind_memory/search.py
capability-memory/src/supermind_memory/service.py
capability-memory/src/supermind_memory/source_resolution.py
capability-memory/src/supermind_memory/sync.py
capability-memory/src/supermind_memory/taxonomy.py
capability-memory/src/supermind_memory/types.py
capability-memory/src/supermind_memory/workflow.py
capability-memory/tests/conftest.py
capability-memory/tests/e2e/test_distributed_bootstrap.py
capability-memory/tests/e2e/test_login_reuse.py
capability-memory/tests/e2e/test_no_daemon.py
capability-memory/tests/integration/test_cli.py
capability-memory/tests/integration/test_discovery.py
capability-memory/tests/integration/test_event_first_service.py
capability-memory/tests/integration/test_explorer.py
capability-memory/tests/integration/test_health.py
capability-memory/tests/integration/test_migration.py
capability-memory/tests/integration/test_projection.py
capability-memory/tests/integration/test_render_repository.py
capability-memory/tests/integration/test_repository.py
capability-memory/tests/integration/test_repository_init.py
capability-memory/tests/integration/test_search.py
capability-memory/tests/integration/test_service.py
capability-memory/tests/integration/test_sync.py
capability-memory/tests/unit/test_event_model.py
capability-memory/tests/unit/test_event_store.py
capability-memory/tests/unit/test_git_client.py
capability-memory/tests/unit/test_purge.py
capability-memory/tests/unit/test_quality.py
capability-memory/tests/unit/test_redaction.py
capability-memory/tests/unit/test_renderer.py
capability-memory/tests/unit/test_replay.py
capability-memory/tests/unit/test_taxonomy_scoring.py
capability-memory/tests/unit/test_types_config.py
capability-memory/uv.lock
docs/product/2026-09-04-capability-memory-design.md
docs/product/2026-09-06-capability-memory-hardening-design.md
docs/product/2026-09-06-distributed-capability-memory-design.md
docs/product/capability-memory-purge.md
docs/product/capability-memory-bootstrap.md
docs/product/green-planet-login-boundary.md
docs/product/plans/2026-09-04-capability-memory.md
docs/product/plans/2026-09-06-capability-memory-hardening.md
docs/superpowers/plans/2026-09-06-distributed-capability-memory.md
plugins/supermind/.codex-plugin/plugin.json
plugins/supermind/scripts/bootstrap.py
plugins/supermind/scripts/capability-memory
plugins/supermind/skills/supermind/SKILL.md
plugins/supermind/skills/supermind/agents/openai.yaml
plugins/supermind/skills/supermind/references/actions.md
plugins/supermind/skills/supermind/references/capability-routing.md
plugins/supermind/skills/supermind/references/product-state.md
plugins/supermind/tests/test_launcher.py
plugins/supermind/tool.lock.json
plugins/supermind/vendor/supermind_capability_memory-0.3.7-py3-none-any.whl
scripts/verify-distribution.py
scripts/verify.sh'

actual_files="$(
  cd "$project_root"
  find . \
    \( -path './.git' -o -path './.superpowers' -o -path './.worktrees' -o -name .venv -o -name .pytest_cache -o -name __pycache__ \) -prune \
    -o -type f -print \
    | sed 's#^\./##' \
    | sort
)"
if ! diff -u <(printf '%s\n' "$expected_files" | sort) <(printf '%s\n' "$actual_files"); then
  echo "Unexpected or missing files detected." >&2
  exit 1
fi

if find "$project_root" \
  \( -path "$project_root/.git" -o -path "$project_root/.superpowers" -o -path "$project_root/.worktrees" -o -name .venv -o -name .pytest_cache -o -name __pycache__ \) -prune \
  -o -type l -print -quit \
  | grep -q .; then
  echo "Symlinks are not allowed in the release tree." >&2
  exit 1
fi

for reference in actions.md capability-routing.md product-state.md; do
  grep -Fq "references/$reference" "$skill_root/SKILL.md"
done

if [[ ! -x "$plugin_root/scripts/capability-memory" ]]; then
  echo "Capability Memory launcher must be executable." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Release verification requires uv for locked dependency and test checks." >&2
  exit 1
fi

uv lock --check --project "$memory_root"
if ! diff -u \
  <(tail -n +3 "$memory_root/requirements.lock") \
  <(uv export --project "$memory_root" --frozen --no-dev --no-emit-project --format requirements-txt | tail -n +3); then
  echo "requirements.lock does not match the frozen project lock." >&2
  exit 1
fi

"$validator_python" "$project_root/scripts/verify-distribution.py"
# These disjoint partitions cover the complete suite once. Each process owns its
# native database runtime; service/e2e work can run alongside storage integrations.
if [[ "$run_tests" == 1 ]]; then
uv run --project "$memory_root" pytest "$memory_root/tests/integration" \
  --ignore="$memory_root/tests/integration/test_service.py" -q &
integration_pid=$!
uv run --project "$memory_root" pytest "$memory_root/tests/unit" \
  "$memory_root/tests/e2e" "$memory_root/tests/integration/test_service.py" \
  "$plugin_root/tests" -q &
behavior_pid=$!
test_status=0
wait "$integration_pid" || test_status=1
wait "$behavior_pid" || test_status=1
if [[ "$test_status" != 0 ]]; then
  echo "Release tests failed." >&2
  exit 1
fi
fi

if grep -RInE \
  --exclude-dir=.git \
  --exclude-dir=.superpowers \
  --exclude-dir=.worktrees \
  --exclude-dir=.venv \
  --exclude-dir=.pytest_cache \
  --exclude-dir=__pycache__ \
  --exclude=.git \
  --exclude=verify.sh \
  '(/Users/|/home/|[A-Za-z]:\\\\Users\\\\|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|\[TODO:|TBD)' \
  "$project_root"; then
  echo "Private, secret-like, legacy, or unfinished content detected." >&2
  exit 1
fi

if [[ "$run_tests" == 1 ]]; then
  echo "Supermind release tree verified."
else
  echo "Supermind release structure verified (tests not run)."
fi
